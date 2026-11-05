"""
UmbraNet — Проверка утечки DNS (DNS leak)
==========================================

Зачем: даже когда мы прописали свой DNS на 127.0.0.1, часть DNS-запросов может
утекать МИМО нашего сервера — чаще всего по IPv6. Тогда заблокированный сервис
резолвится «как обычно» провайдером и не открывается. Источники прямо называют
IPv6-утечку частой причиной «ChatGPT/Gemini не работает» в РФ.

Этот модуль:
  • определяет, есть ли у системы рабочий IPv6;
  • смотрит, на какие DNS реально настроены активные адаптеры (IPv4 и IPv6);
  • выявляет риск утечки (DNS адаптера не указывает на наш локальный сервер);
  • даёт текстовый вердикт + рекомендации для пользователя.

Только Windows. На других ОС возвращает «недоступно», ничего не ломая.
"""

import logging
import sys
import time

from config_utils import _to_bool

log = logging.getLogger("UmbraNet.DnsLeak")

IS_WINDOWS = sys.platform == "win32"

# Адреса, которые означают «DNS указывает на нас» (локальный сервер).
LOCAL_DNS = {"127.0.0.1", "::1"}

# Служебные site-local DNS-заглушки Microsoft (fec0:0:0:ffff::1/2/3). Windows
# прописывает их на ВСЕ интерфейсы по умолчанию, когда реальный IPv6-DNS не
# задан. Они НЕ резолвят (нет такого сервера) → это НЕ утечка, игнорируем.
MS_SITELOCAL_DNS_PREFIX = "fec0:"

# Служебные/виртуальные интерфейсы, которые НЕ нужно проверять на утечку.
SKIP_ADAPTER_KEYWORDS = (
    "loopback", "isatap", "teredo", "bluetooth", "pseudo",
    "виртуальн", "wsl", "vethernet", "tunnel",
)

# Статусы проверки
LEAK_OK = "ok"            # утечки не видно
LEAK_RISK = "risk"        # есть риск утечки
LEAK_UNKNOWN = "unknown"  # не смогли проверить (не Windows / нет данных)


def _is_meaningful_dns(ip: str) -> bool:
    """True, если адрес — это РЕАЛЬНЫЙ DNS (не site-local заглушка Windows)."""
    ip = (ip or "").strip().lower()
    if not ip:
        return False
    if ip.startswith(MS_SITELOCAL_DNS_PREFIX):
        return False  # fec0::ffff:* — пустышка Microsoft
    return True


def _should_skip_adapter(name: str) -> bool:
    n = (name or "").lower()
    return any(kw in n for kw in SKIP_ADAPTER_KEYWORDS)


def has_ipv6_connectivity(ps_runner=None) -> bool:
    """Есть ли у системы глобальный (маршрутизируемый) IPv6-адрес.

    Если IPv6 реально работает, а DNS по IPv6 не на нас — будут утечки.
    """
    if not IS_WINDOWS:
        return False
    run = ps_runner or _default_runner()
    # ищем IPv6 адреса со статусом Preferred, исключая link-local (fe80) и loopback
    cmd = (
        "Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue | "
        "Where-Object {$_.PrefixOrigin -ne 'WellKnown' -and "
        "$_.IPAddress -notlike 'fe80*' -and $_.IPAddress -notlike 'fec0*' -and "
        "$_.IPAddress -ne '::1'} | "
        "Measure-Object | Select-Object -ExpandProperty Count"
    )
    stdout, _stderr, code = run(cmd, timeout=15)
    if code == 0:
        try:
            return int(stdout.strip() or "0") > 0
        except ValueError:
            pass

    # PowerShell не сработал — спрашиваем адреса через netsh (пункт H4). Без этого
    # при заблокированном PowerShell проверка утечек всегда отвечала «IPv6 нет»,
    # то есть молча пропускала реальную утечку.
    if ps_runner is None:
        try:
            import win_shell
        except ImportError:
            return False
        count, res = win_shell.global_ipv6_count()
        if not res.ok:
            log.warning("Проверить IPv6 не удалось: %s", res.human_error)
            return False
        return count > 0
    return False


def _default_runner():
    from process_monitor import _run_ps
    return _run_ps


# ── Кэш последнего результата (защита от частых опросов Health Score) ───────
_LEAK_CACHE: dict = {"ts": 0.0, "key": None, "result": None}
_LEAK_CACHE_TTL = 5.0  # сек, достаточно чтобы Health Score не ддосил TCP-проверку

def _leak_cache_key(config: dict, server_running: bool, dpi_running: bool) -> tuple:
    # ключ = то, что меняет вердикт без сети: адаптеры могут меняться, но их
    # читает get_dns() внутри — кэшируем и их с ttl, считаем что за 5 сек не устареет критично
    return (
        bool(server_running),
        bool(dpi_running),
        str(config.get("dpi_mode", "off")),
        bool(_to_bool(config.get("enable_ipv6", True), True)),
    )

def check_dns_leak(config: dict, server_running: bool, dpi_running: bool = False, ps_runner=None,
                   dns_getter=None, use_cache: bool = True) -> dict:
    """Главная проверка утечек DNS и обхода DPI. Возвращает словарь:

        {
          "status": "ok"|"risk"|"unknown",
          "title":  короткий вердикт,
          "details": [строки с пояснениями],
          "ipv6_present": bool,
          "can_fix": bool,        # можно ли предложить авто-фикс
          "fix_hint": str,        # что предлагаем
        }
    """
    if not IS_WINDOWS:
        return {
            "status": LEAK_UNKNOWN,
            "title": "Проверка утечки доступна только в Windows",
            "details": [],
            "ipv6_present": False,
            "can_fix": False,
            "fix_hint": "",
            "dns_leak": False,
            "dpi_issue": False,
        }

    # ── Fast cache (Stage 2: не ддосим PowerShell/TCP на каждый health_score) ──
    if use_cache and ps_runner is None and dns_getter is None:
        try:
            now = time.monotonic()
            key = _leak_cache_key(config, server_running, dpi_running)
            cached = _LEAK_CACHE
            if cached["result"] is not None and cached["key"] == key and (now - cached["ts"]) < _LEAK_CACHE_TTL:
                return cached["result"]
        except Exception:
            pass

    run = ps_runner or _default_runner()
    get_dns = dns_getter or _default_dns_getter()

    ipv6_present = has_ipv6_connectivity(run)
    per_adapter = get_dns()  # {adapter: {ipv4:[...], ipv6:[...]}}
    enable_ipv6 = _to_bool(config.get("enable_ipv6", True), True)

    details = []
    dns_risks = []
    dpi_risks = []

    # ── 1) IPv4 DNS активных адаптеров должен указывать на нас (если сервер запущен)
    if server_running:
        for name, dns in per_adapter.items():
            if _should_skip_adapter(name):
                continue
            v4 = [ip for ip in dns.get("ipv4", []) if _is_meaningful_dns(ip)]
            if v4 and not any(ip in LOCAL_DNS for ip in v4):
                dns_risks.append(
                    f"Адаптер «{name}»: IPv4 DNS = {', '.join(v4)} "
                    f"(не наш локальный 127.0.0.1)"
                )

    # ── 2) Реальная IPv6-утечка: есть IPv6 И на адаптере прописан НАСТОЯЩИЙ
    #      IPv6-DNS (не ::1 и не fec0::-заглушка Microsoft).
    #      Site-local заглушки fec0::ffff:* НЕ резолвят → это НЕ утечка.
    if ipv6_present and enable_ipv6:
        for name, dns in per_adapter.items():
            if _should_skip_adapter(name):
                continue
            v6_all = dns.get("ipv6", [])
            # настоящие (не fec0::) IPv6-DNS, не указывающие на нас
            real_v6 = [ip for ip in v6_all
                       if _is_meaningful_dns(ip) and ip.strip().lower() not in LOCAL_DNS]
            if real_v6:
                dns_risks.append(
                    f"Адаптер «{name}»: IPv6-DNS = {', '.join(real_v6)} "
                    f"(не указывает на ::1 — запросы по IPv6 идут мимо обхода)"
                )

    # ── 3) Проверка обхода DPI (DPI Leak Check) ──
    # Stage 2: DPI-проверка теперь 1.5s вместо 2.5s и только при запущенном DPI движке;
    # без кэша health_score ддосил сеть каждый опрос.
    dpi_mode = config.get("dpi_mode", "off")
    if dpi_mode != "off" and server_running:
        if dpi_running:
            # Замеряем пинг/соединение к заблокированному домену
            target = "google.com"
            domains = config.get("routed_domains", [])
            if domains:
                for d in domains:
                    if "google" in d or "youtube" in d or "discord" in d:
                        target = d
                        break
                else:
                    target = domains[0]
            
            import socket
            try:
                # Быстрый TCP коннект на 443 порт (через DPI обход!) — 1.5s достаточно
                s = socket.create_connection((target, 443), timeout=1.5)
                s.close()
                details.append(f"Обход DPI успешно проверен на домене «{target}». Соединение установлено.")
            except Exception:
                dpi_risks.append(
                    f"Сбой DPI обхода на домене «{target}»: Соединение заблокировано или сброшено провайдером. "
                    f"Рекомендуется проверить стратегию DPI (например, переключить режим на «DPI Only» или «Combo»)."
                )
        else:
            dpi_risks.append(
                "Выбран режим с DPI (Combo или DPI Only), но сам DPI-движок сейчас остановлен. "
                "Запустите программу кнопкой «Старт», чтобы активировать обход."
            )

    risks = dns_risks + dpi_risks
    if not risks:
        title = "✅ Утечек DNS/DPI не обнаружено"
        result = {
            "status": LEAK_OK,
            "title": title,
            "details": details,
            "ipv6_present": ipv6_present,
            "can_fix": False,
            "fix_hint": "",
            "dns_leak": False,
            "dpi_issue": False,
        }
        if use_cache:
            try:
                _LEAK_CACHE["ts"] = time.monotonic()
                _LEAK_CACHE["key"] = _leak_cache_key(config, server_running, dpi_running)
                _LEAK_CACHE["result"] = result
            except Exception:
                pass
        return result

    # есть риски или сбои. Авто-исправление имеет смысл только для DNS/IPv6,
    # а не для DPI-ошибок (DPI чинится сменой режима/стратегии/WinWS).
    can_fix_dns = bool(dns_risks) and server_running
    if dns_risks and dpi_risks:
        fix_hint = (
            "Рекомендация: нажмите «Исправить» для ликвидации DNS/IPv6-утечек. "
            "Для DPI-проблемы проверьте запуск DPI-движка или смените стратегию."
        )
    elif dns_risks:
        fix_hint = "Рекомендация: нажмите «Исправить» для ликвидации DNS/IPv6-утечек."
    else:
        fix_hint = "DNS-утечек не видно. Для DPI-проблемы проверьте запуск DPI-движка или смените стратегию."
    result = {
        "status": LEAK_RISK,
        "title": "⚠ Обнаружены утечки DNS или блокировки DPI",
        "details": risks,
        "ipv6_present": ipv6_present,
        "can_fix": can_fix_dns,
        "fix_hint": fix_hint,
        "dns_leak": bool(dns_risks),
        "dpi_issue": bool(dpi_risks),
    }
    if use_cache:
        try:
            _LEAK_CACHE["ts"] = time.monotonic()
            _LEAK_CACHE["key"] = _leak_cache_key(config, server_running, dpi_running)
            _LEAK_CACHE["result"] = result
        except Exception:
            pass
    return result


def _default_dns_getter():
    from process_monitor import get_current_dns
    return get_current_dns


def fix_ipv6_leak(disable_ipv6=False, ps_runner=None) -> tuple:
    """Устраняет IPv6-утечку одним из способов:

      disable_ipv6=False → направляет IPv6-DNS активных адаптеров на ::1;
      disable_ipv6=True  → сбрасывает IPv6-DNS на авто (чтобы система не слала
                           IPv6 DNS-запросы провайдеру в обход — если IPv6 не
                           нужен, надёжнее выключить его в адаптере вручную).

    Возвращает (ok, message). Требует прав администратора.
    """
    if not IS_WINDOWS:
        return False, "Только Windows"
    from process_monitor import is_admin
    if not is_admin():
        return False, "Требуются права администратора"

    run = ps_runner or _default_runner()
    if disable_ipv6:
        # Полностью отключаем протокол IPv6 на активных адаптерах — это реально
        # прекращает любые IPv6 DNS-запросы (сброс DNS на авто НЕ помогает: тогда
        # система спрашивает IPv6-DNS провайдера и утечка остаётся).
        action = ("Disable-NetAdapterBinding -Name $a.Name "
                  "-ComponentID ms_tcpip6 -ErrorAction Stop; ")
        what = "IPv6 отключён на сетевых адаптерах"
    else:
        # ВАЖНО: у Set-DnsClientServerAddress НЕТ параметра -AddressFamily.
        # Семейство определяется по самому адресу: передаём IPv6 (::1) →
        # меняется именно IPv6-DNS. (-AddressFamily вызывал ошибку
        # «не удалось найти параметр».)
        action = ("Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
                  "-ServerAddresses @('::1') -ErrorAction Stop; ")
        what = "IPv6-DNS направлен на ::1 (обход)"

    cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "$ok=@(); $err=@(); "
        "foreach ($a in $adapters) { try { "
        f"  {action}"
        "  $ok += $a.Name } catch { $err += ($a.Name + ': ' + $_.Exception.Message) } }; "
        "Clear-DnsClientCache; "
        "Write-Output ('OK:' + ($ok -join ',')); "
        "if ($err) { Write-Output ('ERR:' + ($err -join ';')) }"
    )
    stdout, stderr, _code = run(cmd, timeout=25)
    ok_names, err_names = [], []
    for line in stdout.splitlines():
        if line.startswith("OK:"):
            ok_names = [x for x in line[3:].split(",") if x]
        elif line.startswith("ERR:"):
            err_names = [x for x in line[4:].split(";") if x]
    if ok_names:
        msg = what
        if err_names:
            msg += f" (часть адаптеров с ошибкой: {'; '.join(err_names)})"
        return True, msg

    # PowerShell не сработал — делаем то же через netsh (пункт H4).
    if ps_runner is None:
        try:
            import win_shell
        except ImportError:
            win_shell = None
        if win_shell is not None:
            adapters, _list_res = win_shell.list_adapters()
            native_ok, native_err = [], []
            for name in adapters:
                if disable_ipv6:
                    res = win_shell.set_ipv6_enabled(name, False)
                else:
                    res = win_shell.set_dns_servers(name, ipv6=["::1"])
                (native_ok if res.ok else native_err).append(
                    name if res.ok else f"{name}: {res.human_error}")
            if native_ok:
                msg = f"{what}. Выполнено через netsh: PowerShell недоступен или не ответил."
                if native_err:
                    msg += f" (часть адаптеров с ошибкой: {'; '.join(native_err)})"
                return True, msg

    return False, f"Не удалось: {'; '.join(err_names) or stderr or 'нет адаптеров'}"
