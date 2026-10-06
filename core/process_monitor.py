"""
UmbraNet — Монитор процессов + управление DNS Windows.

Все системные вызовы идут через `win_shell` (пункт H4): у него проверка
доступности PowerShell, деградация на netsh/ipconfig/taskkill/reg и внятный текст
ошибки. Здесь остаётся логика UmbraNet: какой DNS когда ставить.
"""

import logging
import os
import subprocess
import sys
import threading
import time

import psutil

log = logging.getLogger("UmbraNet.ProcessMonitor")

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False
    else:
        return os.geteuid() == 0


# Stage 2: лёгкий кэш на 2 сек — health_score и трекер дёргают часто
_PROC_CACHE = {"ts": 0.0, "data": []}
_PROC_CACHE_TTL = 2.0

def get_running_processes() -> list:
    # быстрый возврат из кэша, чтобы не ддосить psutil каждый опрос health_score
    now = time.monotonic()
    if now - _PROC_CACHE["ts"] < _PROC_CACHE_TTL and _PROC_CACHE["data"]:
        return list(_PROC_CACHE["data"])
    procs = []
    # Забираем только pid и name, потому что exe требует прав админа 
    # на многие процессы и вызов WMI работает очень долго, вызывая зависания UI!
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            procs.append({
                'pid': proc.info['pid'],
                'name': proc.info['name']
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _PROC_CACHE["ts"] = now
    _PROC_CACHE["data"] = list(procs)
    return procs


def is_process_running(process_name: str) -> bool:
    name_lower = process_name.lower()
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == name_lower:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# ── путь к .exe для иконок UI ───────────────────────────────────────────────
# Отдельно от get_running_processes: тот список дёргают часто (health),
# и exe там специально НЕ запрашивается — иначе UI зависает. Здесь путь
# ищем точечно, по имени, с кэшем. UI рисует иконку через QFileIconProvider.
#
# Порядок: кэш → типичные папки установки → App Paths в реестре →
# exe() только у ОДНОГО pid с этим именем (не у всех процессов сразу).

_EXE_LOCK = threading.Lock()
_EXE_CACHE = {}          # lowercase name -> абсолютный путь
_EXE_MISS = {}           # lowercase name -> monotonic, негативный кэш
_EXE_MISS_TTL = 15.0

# (переменная среды, куски пути относительно неё)
_KNOWN_EXES = {
    "chrome.exe": (
        ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
        ("PROGRAMFILES", ("Google", "Chrome", "Application", "chrome.exe")),
        ("PROGRAMFILES(X86)", ("Google", "Chrome", "Application", "chrome.exe")),
    ),
    "msedge.exe": (
        ("PROGRAMFILES(X86)", ("Microsoft", "Edge", "Application", "msedge.exe")),
        ("PROGRAMFILES", ("Microsoft", "Edge", "Application", "msedge.exe")),
    ),
    "firefox.exe": (
        ("PROGRAMFILES", ("Mozilla Firefox", "firefox.exe")),
        ("PROGRAMFILES(X86)", ("Mozilla Firefox", "firefox.exe")),
        ("LOCALAPPDATA", ("Mozilla Firefox", "firefox.exe")),
    ),
    "telegram.exe": (
        ("APPDATA", ("Telegram Desktop", "Telegram.exe")),
        ("LOCALAPPDATA", ("Telegram Desktop", "Telegram.exe")),
    ),
    "discord.exe": (
        ("LOCALAPPDATA", ("Discord", "Discord.exe")),
    ),
    "code.exe": (
        ("LOCALAPPDATA", ("Programs", "Microsoft VS Code", "Code.exe")),
    ),
}


def reset_exe_cache() -> None:
    """Сброс кэша путей. Нужен тестам и не вызывается из UI."""
    with _EXE_LOCK:
        _EXE_CACHE.clear()
        _EXE_MISS.clear()


def _exe_cache_get(key: str):
    with _EXE_LOCK:
        return _EXE_CACHE.get(key)


def _exe_cache_set(key: str, path: str) -> None:
    with _EXE_LOCK:
        _EXE_CACHE[key] = path
        _EXE_MISS.pop(key, None)


def _missed_recently(key: str) -> bool:
    with _EXE_LOCK:
        ts = _EXE_MISS.get(key)
    if ts is None:
        return False
    return (time.monotonic() - ts) < _EXE_MISS_TTL


def _mark_miss(key: str) -> None:
    with _EXE_LOCK:
        _EXE_MISS[key] = time.monotonic()


def _interrupted(interrupt) -> bool:
    if interrupt is None:
        return False
    try:
        return bool(interrupt())
    except Exception:
        return False


def _norm_exe_name(name: str) -> str:
    n = str(name or "").strip().lower()
    if n and not n.endswith(".exe"):
        n += ".exe"
    return n


def _known_exe(name: str):
    """Типичные пути установки — без psutil и без реестра."""
    key = _norm_exe_name(name)
    for env_var, parts in _KNOWN_EXES.get(key, ()):
        root = os.environ.get(env_var) or ""
        if not root:
            continue
        candidate = os.path.join(root, *parts)
        if os.path.isfile(candidate):
            return candidate
    if key == "discord.exe":
        local = os.environ.get("LOCALAPPDATA") or ""
        base = os.path.join(local, "Discord")
        if os.path.isdir(base):
            try:
                for entry in sorted(os.listdir(base), reverse=True):
                    if entry.lower().startswith("app-"):
                        candidate = os.path.join(base, entry, "Discord.exe")
                        if os.path.isfile(candidate):
                            return candidate
            except OSError:
                pass
    return None


def _app_paths_exe(name: str):
    """HKCU/HKLM ...\\App Paths\\name.exe — штатный способ Windows найти программу."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
    except ImportError:
        return None
    key_name = _norm_exe_name(name)
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + key_name
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub) as k:
                try:
                    val, _ = winreg.QueryValueEx(k, "")
                except OSError:
                    val = ""
                if isinstance(val, str):
                    val = os.path.expandvars(val.strip().strip('"'))
                    if val and os.path.isfile(val):
                        return val
                try:
                    folder, _ = winreg.QueryValueEx(k, "Path")
                except OSError:
                    folder = ""
                if isinstance(folder, str) and folder.strip():
                    candidate = os.path.join(
                        os.path.expandvars(folder.strip().strip('"')), key_name
                    )
                    if os.path.isfile(candidate):
                        return candidate
        except OSError:
            continue
    return None


def _cheap_exe(name: str):
    return _known_exe(name) or _app_paths_exe(name)


def _exe_of_pid(pid: int):
    try:
        exe = psutil.Process(pid).exe()
        if exe and os.path.isfile(exe):
            return exe
    except (psutil.Error, OSError, ValueError):
        return None
    return None


def resolve_process_exes(names, interrupt=None) -> dict:
    """Имя процесса → абсолютный путь к .exe (только найденные).

    ``interrupt`` — вызываемый объект без аргументов (например
    QThread.isInterruptionRequested): True = остановиться.
    """
    result = {}
    pending = []
    seen = set()

    for raw in names or []:
        if _interrupted(interrupt):
            return result
        name = str(raw or "").strip()
        if not name:
            continue
        key = _norm_exe_name(name)
        if key in seen:
            path = result.get(next((n for n in result if _norm_exe_name(n) == key), ""))
            if path:
                result[name] = path
            continue
        seen.add(key)

        cached = _exe_cache_get(key)
        if cached and os.path.isfile(cached):
            result[name] = cached
            continue
        path = _cheap_exe(key)
        if path:
            _exe_cache_set(key, path)
            result[name] = path
            continue
        pending.append(name)

    if not pending or _interrupted(interrupt):
        return result

    pid_by_name = {}
    try:
        for proc in get_running_processes():
            n = _norm_exe_name(proc.get("name") or "")
            pid = proc.get("pid")
            if n and pid is not None and n not in pid_by_name:
                pid_by_name[n] = pid
    except Exception:
        pid_by_name = {}

    for name in pending:
        if _interrupted(interrupt):
            break
        key = _norm_exe_name(name)
        if _missed_recently(key):
            continue
        pid = pid_by_name.get(key)
        path = _exe_of_pid(pid) if pid is not None else None
        if path:
            _exe_cache_set(key, path)
            result[name] = path
        else:
            _mark_miss(key)
    return result


def resolve_process_exe(name: str):
    """Путь к .exe одного процесса или None, если не нашли."""
    name = str(name or "").strip()
    if not name:
        return None
    found = resolve_process_exes([name])
    return found.get(name)


def _win_shell():
    """Ленивый импорт модуля системных вызовов (лежит рядом с этим файлом)."""
    import win_shell
    return win_shell


def _console_oem_codepage() -> str:
    """Кодовая страница консоли Windows (на русской — cp866).

    Реализация в `win_shell`: там же, где запуск процессов, — чтобы декодер и
    раннер не разъезжались по двум файлам.
    """
    return _win_shell().OEM_CODEPAGE


def _decode_console(data: bytes) -> str:
    """Безопасно декодирует сырой вывод консольной утилиты Windows."""
    return _win_shell().decode_console(data)


def _run_ps(command: str, timeout: int = 15) -> tuple:
    """
    Запускает команду PowerShell без окна консоли.
    Возвращает (stdout, stderr, returncode).

    Тонкая обёртка над `win_shell.run_ps` (пункт H4): там проверка доступности
    PowerShell, запуск без мелькания окна и разбор кода возврата. Здесь остаётся
    старый интерфейс «кортеж из трёх» — на него опираются и вызовы, и тесты:
    так ни один существующий код не пришлось переписывать ради нового пути.
    Когда PowerShell недоступен, вместо запуска возвращается код -1 и готовый
    текст ошибки — тот же смысл, что раньше давал FileNotFoundError.
    """
    result = _win_shell().run_ps(command, timeout=timeout)
    if result.via == "none":
        return "", result.human_error or "PowerShell недоступен", -1
    return result.stdout, result.stderr, result.code


def _fallback_adapters() -> list:
    """Список активных адаптеров без PowerShell (netsh) — путь деградации."""
    names, _res = _win_shell().list_adapters()
    return names


def set_dns_native(adapters, ipv4=None, ipv6=None, reset: bool = False,
                   reset_ipv4: bool = False, reset_ipv6: bool = False) -> tuple:
    """Ставит/сбрасывает DNS через netsh — путь без PowerShell (пункт H4).

    reset — вернуть оба семейства на DHCP (общий сброс).
    reset_ipv4 / reset_ipv6 — вернуть на DHCP одно семейство: нужно, когда по
    этому семейству внешний DNS ставить нельзя (по IPv6 это была бы утечка).

    Возвращает (ok_adapters, err_adapters): тот же формат, что даёт разбор
    PowerShell-вывода, поэтому вызывающий код не знает, каким путём всё прошло.
    """
    shell = _win_shell()
    ok_names, err_names = [], []
    for name in adapters or []:
        res = shell.reset_dns_servers(name) if reset else \
            shell.set_dns_servers(name, ipv4=ipv4, ipv6=ipv6,
                                  reset_ipv4=reset_ipv4, reset_ipv6=reset_ipv6)
        if res.ok and res.stdout:
            ok_names.append(name)
        else:
            err_names.append(f"{name}: {res.human_error or 'не сработало'}")
    return ok_names, err_names


def get_active_adapters() -> list:
    """Список имён активных сетевых адаптеров.

    Идёт через `win_shell.list_adapters`: сначала PowerShell, при его отсутствии —
    `netsh interface show interface` (пункт H4). Раньше при недоступном PowerShell
    здесь возвращался пустой список, и «нет прав / нет адаптеров / сломан
    PowerShell» выглядели для человека одинаково.
    """
    names, res = _win_shell().list_adapters()
    if not names and not res.ok:
        log.warning("get_active_adapters: %s", res.human_error or res.stderr)
    elif res.note:
        log.info("Адаптеры получены через %s (%s)", res.via, res.note)
    return names


# Stage 2: кэш текущих DNS на 5 сек — health_score + leak check дергают часто
_DNS_CACHE = {"ts": 0.0, "data": {}}


def invalidate_dns_cache() -> None:
    """Сбросить кэш get_current_dns после ЛЮБОЙ смены DNS.

    Иначе аудит после Стопа читает старое значение «127.0.0.1» и ложно
    ругается «системный DNS всё ещё указывает на UmbraNet» (поле 2026-10-06).
    """
    _DNS_CACHE["ts"] = 0.0
    _DNS_CACHE["data"] = {}
_DNS_CACHE_TTL = 5.0

def get_current_dns(use_cache: bool = True) -> dict:
    """
    Возвращает текущие DNS для всех адаптеров (IPv4 и IPv6).
    Формат: {'AdapterName': {'ipv4': [...], 'ipv6': [...]}}
    """
    if use_cache:
        now = time.monotonic()
        if now - _DNS_CACHE["ts"] < _DNS_CACHE_TTL and _DNS_CACHE["data"]:
            return dict(_DNS_CACHE["data"])
    result = {}

    active_filter = (
        "$up = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object -ExpandProperty Name; "
        "$skip = 'loopback|isatap|teredo|bluetooth|pseudo|virtual|wsl|vethernet|tunnel|виртуальн'; "
    )

    # IPv4
    stdout4, _, _ = _run_ps(
        active_filter +
        "Get-DnsClientServerAddress -AddressFamily IPv4 | "
        "Where-Object {$_.ServerAddresses.Count -gt 0 -and $up -contains $_.InterfaceAlias -and $_.InterfaceAlias -notmatch $skip} | "
        "ForEach-Object { $_.InterfaceAlias + '|' + ($_.ServerAddresses -join ',') }"
    )
    if stdout4:
        for line in stdout4.splitlines():
            line = line.strip()
            if '|' in line:
                name, ips = line.split('|', 1)
                name = name.strip()
                if name not in result:
                    result[name] = {'ipv4': [], 'ipv6': []}
                result[name]['ipv4'] = [ip.strip() for ip in ips.split(',') if ip.strip()]

    # IPv6
    stdout6, _, _ = _run_ps(
        active_filter +
        "Get-DnsClientServerAddress -AddressFamily IPv6 | "
        "Where-Object {$_.ServerAddresses.Count -gt 0 -and $up -contains $_.InterfaceAlias -and $_.InterfaceAlias -notmatch $skip} | "
        "ForEach-Object { $_.InterfaceAlias + '|' + ($_.ServerAddresses -join ',') }"
    )
    if stdout6:
        for line in stdout6.splitlines():
            line = line.strip()
            if '|' in line:
                name, ips = line.split('|', 1)
                name = name.strip()
                if name not in result:
                    result[name] = {'ipv4': [], 'ipv6': []}
                result[name]['ipv6'] = [ip.strip() for ip in ips.split(',') if ip.strip()]

    if not result:
        # PowerShell недоступен, упал или вернул пусто — читаем DNS через netsh
        # (пункт H4). Иначе и «DNS нигде не прописан», и «PowerShell заблокирован»
        # выглядели одинаково, и проверка утечек молча считала систему чистой.
        native, res = _win_shell().dns_servers_native()
        if native:
            log.info("Текущий DNS прочитан через %s (PowerShell недоступен или пуст)", res.via)
            result = native
        elif not res.ok:
            log.warning("Текущий DNS получить не удалось: %s", res.human_error)

    if use_cache:
        _DNS_CACHE["ts"] = time.monotonic()
        _DNS_CACHE["data"] = dict(result)
    return result


# ── xbox-dns.ru — Smart DNS прокси ───────────────────────────────────────────
#
# Как работает:
#   Вместо настоящего IP chatgpt.com (104.18.x.x) xbox-dns.ru возвращает
#   IP СВОЕГО прокси-сервера (87.228.47.x). Браузер подключается к их серверу,
#   тот перенаправляет трафик на ChatGPT. ChatGPT видит IP xbox-dns.ru → работает!
#
# Способы подключения:
#   1. Системный DNS (IPv4): см. XBOX_DNS_PRIMARY — работает для ВСЕХ программ
#   2. DoH (браузер): https://xbox-dns.ru/dns-query — только для браузера
#
# Для ПК рекомендуется способ 1 — системный DNS, тогда работает всё!

# Адреса берём из единого источника правды (profile_utils), чтобы при смене
# IP сервиса не править их в нескольких файлах.
from profile_utils import (
    XBOX_DNS_IPV4_PRIMARY as XBOX_DNS_PRIMARY,
)
from profile_utils import (
    XBOX_DNS_IPV4_SECONDARY as XBOX_DNS_SECONDARY,
)
from profile_utils import (
    XBOX_DNS_IPV6_PRIMARY as XBOX_DNS_IPV6_PRI,
)
from profile_utils import (
    XBOX_DNS_IPV6_SECONDARY as XBOX_DNS_IPV6_SEC,
)
from profile_utils import (
    XBOX_DOH_URL,
)

XBOX_DOH_NAME       = 'xbox-dns.ru'

# Фейковые IP которые МТС подставляет вместо настоящих
MTS_FAKE_IPS = {'8.6.112.0', '8.47.69.0', '8.43.85.0', '8.43.85.1', '8.34.212.0'}


def set_dns_profile(ipv4_primary: str, ipv4_secondary: str = "",
                    ipv6_primary: str = "", ipv6_secondary: str = "",
                    profile_name: str = "DNS-профиль") -> tuple:
    """Устанавливает системный DNS на указанный профиль (IPv4 + IPv6)."""
    if not IS_WINDOWS:
        return False, "Только Windows", []
    if not is_admin():
        return False, "Требуются права администратора", []

    ipv4_list = [ip for ip in (ipv4_primary, ipv4_secondary) if ip]
    ipv6_list = [ip for ip in (ipv6_primary, ipv6_secondary) if ip]
    if not ipv4_list and not ipv6_list:
        return False, "Профиль не содержит ни одного DNS-адреса", []

    ipv4_ps = "@(" + ",".join(f"'{v}'" for v in ipv4_list) + ")" if ipv4_list else ""
    ipv6_ps = "@(" + ",".join(f"'{v}'" for v in ipv6_list) + ")" if ipv6_list else ""

    ipv4_action = (
        "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        f"-ServerAddresses {ipv4_ps}; "
    ) if ipv4_list else (
        "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        "-ResetServerAddresses; "
    )

    # ВАЖНО: у Set-DnsClientServerAddress НЕТ параметра -AddressFamily —
    # семейство определяется по самим адресам. Для IPv6 передаём IPv6-адреса.
    # Для сброса ТОЛЬКО IPv6 используем netsh (Reset сбрасывает оба семейства).
    ipv6_action = (
        "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        f"-ServerAddresses {ipv6_ps}; "
    ) if ipv6_list else (
        "netsh interface ipv6 set dnsservers \"$($a.Name)\" source=dhcp | Out-Null; "
    )

    ps_cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "$ok = @(); $err = @(); "
        "foreach ($a in $adapters) { "
        "  try { "
        f"    {ipv4_action}"
        f"    {ipv6_action}"
        "    $ok += $a.Name "
        "  } catch { "
        "    $err += $a.Name + ': ' + $_.Exception.Message "
        "  } "
        "}; "
        "Clear-DnsClientCache; "
        "Write-Output ('OK:' + ($ok -join ',')); "
        "if ($err) { Write-Output ('ERR:' + ($err -join ';')) }"
    )

    stdout, stderr, _code = _run_ps(ps_cmd, timeout=25)
    log.info(f"set_dns_profile stdout: {stdout!r}")

    ok_adapters, err_adapters = [], []
    for line in stdout.splitlines():
        if line.startswith('OK:'):
            ok_adapters = [x for x in line[3:].split(',') if x]
        elif line.startswith('ERR:'):
            err_adapters = [x for x in line[4:].split(';') if x]

    if ok_adapters:
        msg = f"{profile_name} на: {', '.join(ok_adapters)}"
        if err_adapters:
            msg += f"\nОшибки: {'; '.join(err_adapters)}"
        invalidate_dns_cache()  # смена DNS — кэш чтения больше не наш
        return True, msg, ok_adapters

    # PowerShell не сработал — ставим DNS через netsh (пункт H4). Для этого нужен
    # список адаптеров: если и его получить не удалось, честно говорим об этом.
    adapters = _fallback_adapters()
    if adapters:
        native_ok, native_err = set_dns_native(
            adapters, ipv4=ipv4_list or None, ipv6=ipv6_list or None,
            reset_ipv4=not ipv4_list, reset_ipv6=not ipv6_list,
        )
        if native_ok:
            msg = (f"{profile_name} на: {', '.join(native_ok)}\n"
                   f"Выполнено через netsh: PowerShell недоступен или не ответил.")
            if native_err:
                msg += f"\nОшибки: {'; '.join(native_err)}"
            log.info("set_dns_profile выполнен через netsh: %s", native_ok)
            invalidate_dns_cache()  # смена DNS — кэш чтения больше не наш
            return True, msg, native_ok

    if not adapters:
        return False, ("Не удалось: сетевые адаптеры не найдены — \n"
                       "PowerShell не работает, и через netsh список адаптеров получить не удалось"), []
    return False, f"Не удалось: {stderr or 'PowerShell не ответил'}", []


def set_dns_xbox() -> tuple:
    return set_dns_profile(
        XBOX_DNS_PRIMARY,
        XBOX_DNS_SECONDARY,
        XBOX_DNS_IPV6_PRI,
        XBOX_DNS_IPV6_SEC,
        profile_name="xbox-dns.ru",
    )


def set_chrome_doh(doh_url: str = XBOX_DOH_URL) -> tuple:
    """
    Прописывает DoH-провайдер в Chrome через реестр Windows.
    Chrome будет использовать указанный DoH для ВСЕХ DNS-запросов,
    независимо от системного DNS.
    Возвращает (success: bool, message: str)
    """
    if not IS_WINDOWS:
        return False, "Только Windows"

    # Chrome хранит настройки в реестре
    # HKLM\SOFTWARE\Policies\Google\Chrome  (для всех пользователей, нужны права)
    # HKCU\SOFTWARE\Policies\Google\Chrome  (для текущего пользователя, без прав)
    ps_cmd = (
        "$url = '" + doh_url + "'; "
        # Пробуем HKCU (не нужны права администратора)
        "$path = 'HKCU:\\SOFTWARE\\Policies\\Google\\Chrome'; "
        "if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsMode' -Value 'secure' -Type String; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsTemplates' -Value $url -Type String; "
        # Также пробуем HKLM если есть права
        "if ([bool](([System.Security.Principal.WindowsIdentity]::GetCurrent()).groups "
        "    -match 'S-1-5-32-544')) { "
        "  $path2 = 'HKLM:\\SOFTWARE\\Policies\\Google\\Chrome'; "
        "  if (-not (Test-Path $path2)) { New-Item -Path $path2 -Force | Out-Null }; "
        "  Set-ItemProperty -Path $path2 -Name 'DnsOverHttpsMode' -Value 'secure' -Type String; "
        "  Set-ItemProperty -Path $path2 -Name 'DnsOverHttpsTemplates' -Value $url -Type String "
        "}; "
        "Write-Output 'OK'"
    )

    stdout, stderr, _code = _run_ps(ps_cmd, timeout=15)
    log.info(f"set_chrome_doh: {stdout!r} {stderr!r}")

    if 'OK' in stdout:
        return True, f"DoH прописан в Chrome: {doh_url}\nПерезапусти Chrome для применения."

    # PowerShell не сработал — пишем те же ключи через reg (пункт H4). HKCU-ветка
    # доступна без прав администратора, поэтому её и пробуем.
    ok, msg = _doh_reg_write(r"HKCU\SOFTWARE\Policies\Google\Chrome", doh_url)
    if ok:
        return True, (f"DoH прописан в Chrome: {doh_url}\nПерезапусти Chrome для применения.\n"
                      "Выполнено через reg: PowerShell недоступен или не ответил.")
    return False, f"Ошибка записи в реестр: {msg or stderr}"


def _doh_reg_write(path: str, doh_url: str) -> tuple:
    """Пишет пару ключей DoH-политики через `reg add`. Возвращает (ok, сообщение)."""
    shell = _win_shell()
    errors = []
    for name, value in (("DnsOverHttpsMode", "secure"), ("DnsOverHttpsTemplates", doh_url)):
        res = shell.reg_set(path, name, value)
        if not res.ok:
            errors.append(f"{name}: {res.human_error}")
    if errors:
        return False, "; ".join(errors)
    return True, ""


def _doh_reg_delete(path: str) -> tuple:
    """Убирает ключи DoH-политики через `reg delete`. Возвращает (ok, сообщение)."""
    shell = _win_shell()
    errors = []
    for name in ("DnsOverHttpsMode", "DnsOverHttpsTemplates"):
        res = shell.reg_delete(path, name)
        if not res.ok:
            errors.append(f"{name}: {res.human_error}")
    if errors:
        return False, "; ".join(errors)
    return True, ""


def set_edge_doh(doh_url: str = XBOX_DOH_URL) -> tuple:
    """
    Прописывает DoH-провайдер в Microsoft Edge через реестр.
    """
    if not IS_WINDOWS:
        return False, "Только Windows"

    ps_cmd = (
        "$url = '" + doh_url + "'; "
        "$path = 'HKCU:\\SOFTWARE\\Policies\\Microsoft\\Edge'; "
        "if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsMode' -Value 'secure' -Type String; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsTemplates' -Value $url -Type String; "
        "Write-Output 'OK'"
    )

    stdout, stderr, _ = _run_ps(ps_cmd, timeout=15)
    if 'OK' in stdout:
        return True, f"DoH прописан в Edge: {doh_url}"

    ok, msg = _doh_reg_write(r"HKCU\SOFTWARE\Policies\Microsoft\Edge", doh_url)
    if ok:
        return True, (f"DoH прописан в Edge: {doh_url}\n"
                      "Выполнено через reg: PowerShell недоступен или не ответил.")
    return False, f"Ошибка: {msg or stderr}"


def reset_chrome_doh() -> tuple:
    """Убирает DoH-политику Chrome из реестра."""
    if not IS_WINDOWS:
        return False, "Только Windows"

    # Используем отдельные команды для HKCU и HKLM — проще и надёжнее
    ps_cmd = (
        "$paths = @("
        "  'HKCU:\\SOFTWARE\\Policies\\Google\\Chrome',"
        "  'HKLM:\\SOFTWARE\\Policies\\Google\\Chrome'"
        "); "
        "foreach ($p in $paths) { "
        "  if (Test-Path $p) { "
        "    Remove-ItemProperty -Path $p -Name 'DnsOverHttpsMode' -ErrorAction SilentlyContinue; "
        "    Remove-ItemProperty -Path $p -Name 'DnsOverHttpsTemplates' -ErrorAction SilentlyContinue "
        "  } "
        "}; "
        "Write-Output 'OK'"
    )
    stdout, stderr, _ = _run_ps(ps_cmd, timeout=10)
    if 'OK' in stdout:
        return True, "DoH политика Chrome удалена. Перезапусти Chrome."

    ok, msg = _doh_reg_delete(r"HKCU\SOFTWARE\Policies\Google\Chrome")
    if ok:
        return True, ("DoH политика Chrome удалена. Перезапусти Chrome.\n"
                      "Выполнено через reg: PowerShell недоступен или не ответил.")
    if not is_admin():
        # HKLM-ветку без прав всё равно не снять — говорим честно.
        return True, ("DoH политика Chrome удалена для текущего пользователя "
                      "(пользовательская ветка реестра). Перезапусти Chrome.")
    return False, f"Ошибка: {msg or stderr}"


def set_dns_to_localhost(fallback_ipv4: str = '1.1.1.1',
                         fallback_ipv6: str = '2606:4700:4700::1111',
                         enable_ipv6: bool = True) -> tuple:
    """
    Устанавливает системный DNS на локальный UmbraNet и запасные серверы из конфигурации.

    IPv4:
      - основной: 127.0.0.1
      - резерв: fallback_ipv4

    IPv6:
      - если enable_ipv6=True:
          только ::1 (без внешнего fallback, иначе возможна IPv6 DNS-утечка)
      - если enable_ipv6=False:
          ставим только fallback_ipv6, либо сбрасываем IPv6 DNS на авто

    Возвращает (success: bool, message: str, adapters: list)
    """
    if not IS_WINDOWS:
        return False, "Только Windows", []
    if not is_admin():
        return False, "Требуются права администратора", []

    ipv4_servers = ['127.0.0.1']
    if fallback_ipv4 and fallback_ipv4 not in ipv4_servers:
        ipv4_servers.append(fallback_ipv4)

    ipv6_servers = []
    if enable_ipv6:
        # ВАЖНО: для IPv6 не ставим внешний fallback рядом с ::1.
        # Windows может уйти на второй IPv6-DNS провайдера/Google, и это будет
        # реальная IPv6 DNS-утечка мимо UmbraNet. Если локальный ::1 не работает,
        # лучше явно показать проблему, а не тихо протечь наружу.
        ipv6_servers = ['::1']
    elif fallback_ipv6:
        ipv6_servers = [fallback_ipv6]

    ipv4_ps = "@(" + ",".join(f"'{v}'" for v in ipv4_servers) + ")"
    ipv6_ps = "@(" + ",".join(f"'{v}'" for v in ipv6_servers) + ")" if ipv6_servers else ""
    if ipv6_servers:
        # Set-DnsClientServerAddress определяет семейство по адресам (IPv6 → IPv6).
        # Параметра -AddressFamily у Set- НЕТ (он есть только у Get-).
        ipv6_action = (
            "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
            f"-ServerAddresses {ipv6_ps}; "
        )
    else:
        # сброс ТОЛЬКО IPv6-DNS (через netsh, т.к. -ResetServerAddresses сбросил бы оба)
        ipv6_action = (
            "netsh interface ipv6 set dnsservers \"$($a.Name)\" source=dhcp | Out-Null; "
        )

    ps_cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "$ok = @(); $err = @(); "
        "foreach ($a in $adapters) { "
        "  try { "
        "    Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        f"      -ServerAddresses {ipv4_ps}; "
        f"    {ipv6_action}"
        "    $ok += $a.Name "
        "  } catch { "
        "    $err += $a.Name + ': ' + $_.Exception.Message "
        "  } "
        "}; "
        "Clear-DnsClientCache; "
        "Write-Output ('OK:' + ($ok -join ',')); "
        "if ($err) { Write-Output ('ERR:' + ($err -join ';')) }"
    )

    stdout, stderr, code = _run_ps(ps_cmd, timeout=20)
    log.info(f"set_dns_to_localhost: {stdout!r}")

    ok_adapters, err_adapters = [], []
    for line in stdout.splitlines():
        if line.startswith('OK:'):
            ok_adapters = [x for x in line[3:].split(',') if x]
        elif line.startswith('ERR:'):
            err_adapters = [x for x in line[4:].split(';') if x]

    if ok_adapters:
        msg = (
            f"DNS → {ipv4_servers[0]} / "
            f"IPv4 резерв: {fallback_ipv4 or 'нет'} / "
            f"IPv6: {', '.join(ipv6_servers) if ipv6_servers else 'авто'}\n"
            f"Адаптеры: {', '.join(ok_adapters)}"
        )
        if err_adapters:
            msg += f"\nОшибки: {'; '.join(err_adapters)}"
        invalidate_dns_cache()  # смена DNS — кэш чтения больше не наш
        return True, msg, ok_adapters

    # Ни один адаптер не настроен. Покажем человекочитаемую причину:
    # сначала разобранные ERR-строки (теперь они в правильной кодировке),
    # затем stderr PowerShell, и только потом общий текст.
    # PowerShell не справился — ставим DNS через netsh (пункт H4).
    adapters = _fallback_adapters()
    if adapters:
        native_ok, native_err = set_dns_native(
            adapters, ipv4=ipv4_servers, ipv6=ipv6_servers or None,
            reset_ipv6=not ipv6_servers,
        )
        if native_ok:
            msg = (
                f"DNS → {ipv4_servers[0]} / "
                f"IPv4 резерв: {fallback_ipv4 or 'нет'} / "
                f"IPv6: {', '.join(ipv6_servers) if ipv6_servers else 'авто'}\n"
                f"Адаптеры: {', '.join(native_ok)}\n"
                "Выполнено через netsh: PowerShell недоступен или не ответил."
            )
            if native_err:
                msg += f"\nОшибки: {'; '.join(native_err)}"
            log.info("set_dns_to_localhost выполнен через netsh: %s", native_ok)
            return True, msg, native_ok

    if err_adapters:
        reason = '; '.join(err_adapters)
    elif stderr:
        reason = stderr
    elif not adapters:
        reason = "сетевые адаптеры не найдены (PowerShell не работает, и netsh список не дал)"
    else:
        reason = "ни один сетевой адаптер не удалось настроить"
    return False, f"Не удалось: {reason}", []


def reset_dns_to_auto() -> tuple:
    """Сбрасывает DNS на DHCP (IPv4 и IPv6)."""
    if not IS_WINDOWS:
        return False, "Только Windows"
    if not is_admin():
        return False, "Требуются права администратора"

    ps_cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "$ok = @(); "
        "foreach ($a in $adapters) { "
        "  Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex -ResetServerAddresses; "
        "  $ok += $a.Name "
        "}; "
        "Clear-DnsClientCache; "
        "Write-Output ('OK:' + ($ok -join ','))"
    )

    stdout, stderr, _ = _run_ps(ps_cmd, timeout=20)
    for line in stdout.splitlines():
        if line.startswith('OK:'):
            adapters = [x for x in line[3:].split(',') if x]
            return True, f"DNS сброшен на DHCP: {', '.join(adapters)}"

    # PowerShell не сработал — сбрасываем через netsh (пункт H4).
    adapters = _fallback_adapters()
    if adapters:
        native_ok, native_err = set_dns_native(adapters, reset=True)
        if native_ok:
            log.info("reset_dns_to_auto выполнен через netsh: %s", native_ok)
            return True, (f"DNS сброшен на DHCP: {', '.join(native_ok)}\n"
                          "Выполнено через netsh: PowerShell недоступен или не ответил.")
        if native_err:
            return False, f"Ошибка: {'; '.join(native_err)}"
    return False, f"Ошибка: {stderr or 'PowerShell не ответил'}"


def flush_dns_cache() -> bool:
    """Очищает DNS-кэш Windows.

    Через `win_shell`: сначала `Clear-DnsClientCache`, при недоступном PowerShell —
    `ipconfig /flushdns` (пункт H4). Кэш сбрасывается одинаково, а раньше без
    PowerShell очистка молча не происходила — и человек видел «DNS обновился не
    сразу», хотя на самом деле кэш никто не чистил.
    """
    res = _win_shell().flush_dns_cache()
    if not res.ok:
        log.warning("Очистка DNS-кэша не удалась: %s", res.human_error)
    elif res.note:
        log.info("Очистка DNS-кэша выполнена через %s (%s)", res.via, res.note)
    return res.ok


class ProcessMonitor:
    def __init__(self, callback=None):
        self.callback = callback
        self.running = False
        self._thread = None
        self._known_pids = set()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _monitor_loop(self):
        while self.running:
            try:
                current_pids = set()
                for proc in psutil.process_iter(['pid']):
                    try:
                        current_pids.add(proc.info['pid'])
                    except Exception:
                        continue

                if (current_pids != self._known_pids) and self.callback:
                    self.callback(get_running_processes())

                self._known_pids = current_pids
            except Exception as e:
                log.error(f"ProcessMonitor error: {e}")
            time.sleep(3)
