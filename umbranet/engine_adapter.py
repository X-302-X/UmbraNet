"""
UmbraNet - адаптер к ядру (Engine API facade).

Единственная точка, через которую UI общается с движком DNS/DPI.
UI НЕ импортирует модули ядра напрямую — только этот файл.

Почему так:
  • Ядро (umbranet/core/*) перенесено из NetDocker и доработано
    использует плоские импорты (import config_utils, from profile_utils ...).
    Чтобы это работало, мы добавляем папку core/ в sys.path — тогда ядро
    живёт «само в себе», как в оригинальном репозитории, и его легко
    обновлять одним копированием.
  • UI завязан только на стабильный контракт этого фасада, а не на
    внутренние имена ядра.

Если ядро по какой-то причине недоступно (например, на не-Windows для
отладки UI) — поднимается заглушка _StubEngine с тем же контрактом.
"""

from __future__ import annotations

import logging
import os
import sys

from core.diagnostics import log_recoverable

log = logging.getLogger("UmbraNet.adapter")

# Признак, что под нами настоящее ядро (а не заглушка)
REAL_ENGINE = False

# ── Добавляем папку ядра и подпапки в sys.path, чтобы плоские импорты работали ──────
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_PACKAGE_DIR)
_CORE_DIR = os.path.join(_APP_DIR, "core")
_DNS_DIR = os.path.join(_CORE_DIR, "dns")
_DPI_DIR = os.path.join(_CORE_DIR, "dpi")

for p in (_CORE_DIR, _DNS_DIR, _DPI_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


def get_app_root() -> str:
    """Абсолютная папка, где расположен UmbraNet.

    Не зависит от текущей рабочей папки Windows/ярлыка. Это важно для действий
    вроде «Показать стратегию в папке»: приложение должно открывать свой каталог,
    а не Documents/текущий cwd.
    """
    return _APP_DIR


def get_strategies_dir() -> str:
    """Абсолютная папка UmbraNet/strategies."""
    path = os.path.abspath(os.path.join(_APP_DIR, "strategies"))
    os.makedirs(path, exist_ok=True)
    return path


# ── Заглушка ядра (для отладки UI без бэкенда) ──────────────────────────────
class _StubEngine:
    """Имитация UmbraNetDNS с тем же публичным контрактом."""

    def __init__(self):
        self.running = False
        self.last_start_error = ""
        self.config = {
            "xbox_dns_mode": "doh",
            "dpi_mode": "off",
            "fallback_dns": "8.8.8.8",
            "fallback_dns6": "2001:4860:4860::8888",
            "listen_port": 53,
            "listen_host": "127.0.0.1",
            "listen_host6": "::1",
            "enable_ipv6": True,
            "active_dns_profile": "builtin-xbox-dns",
            "user_dns_profiles": [],
            "routed_domains": ["openai.com", "chatgpt.com", "github.com", "claude.ai"],
            "routed_processes": ["chrome.exe", "msedge.exe", "firefox.exe"],
            "route_all": False,
            "ipv6_priority_enabled": False,
            "routed_subscriptions": [],
        }

    def start(self) -> bool:
        self.running = True
        self.last_start_error = ""
        return True

    def stop(self) -> None:
        self.running = False

    def reload_config(self) -> None:
        pass

    def change_subscription(self, url: str, *, remove: bool = False) -> bool:
        urls = self.config.setdefault("routed_subscriptions", [])
        if (url in urls) != remove:
            return False
        if remove:
            urls.remove(url)
        else:
            urls.append(url)
        if remove:
            self.config["subscribed_domains_set"] = set()
        return True

    def add_domain(self, d): self.config["routed_domains"].append(d.strip().lower())
    def remove_domain(self, d):
        if d in self.config["routed_domains"]:
            self.config["routed_domains"].remove(d)

    def add_process(self, p): self.config["routed_processes"].append(p.strip())
    def remove_process(self, p):
        if p in self.config["routed_processes"]:
            self.config["routed_processes"].remove(p)

    def set_dpi_mode(self, mode):
        self.config["dpi_mode"] = mode

    def switch_mode(self, ui_mode: str) -> tuple:
        """Заглушка switch_mode — всегда успешна в stub-режиме."""
        VALID = ("dns_only", "combo", "dpi_only")
        if ui_mode not in VALID:
            return False, f"Неизвестный режим '{ui_mode}'"
        self.config["dpi_mode"] = {
            "dns_only": "off",
            "combo": "combo",
            "dpi_only": "dpi_only",
        }[ui_mode]
        # Выбор режима не запускает заглушку/движок.
        return True, ""

    def access_status(self) -> dict:
        st = "up" if self.running else "unknown"
        return {"state": st, "text": "🟢 Работает (заглушка)" if self.running else "⚪ Не проверялось",
                "active_provider": "stub", "providers": [{"name": "stub", "status": st}]}

    def check_now(self, test_domain="chatgpt.com") -> dict:
        s = self.access_status(); s["ok"] = self.running; return s

    def fallback_status(self) -> dict:
        # Заглушка ядра: отвечать пока нечем — интерфейс покажет «данных нет».
        return {}

    def check_dns_leak(self) -> dict:
        return {"status": "ok", "title": "Утечек нет (заглушка)", "details": [],
                "ipv6_present": False, "can_fix": False, "fix_hint": ""}

    def fix_dns_leak(self, disable_ipv6=False):
        return True, "OK (заглушка)"


# ── Получение движка (singleton) ────────────────────────────────────────────
_engine = None


def get_engine():
    """Возвращает singleton движка: настоящий, если доступен, иначе заглушку."""
    global _engine, REAL_ENGINE
    if _engine is not None:
        return _engine
    try:
        from dns_server import get_instance  # type: ignore  # из core/ через sys.path
        _engine = get_instance()
        REAL_ENGINE = True
        log.info("Подключено НАСТОЯЩЕЕ ядро (core/dns_server)")
    except Exception as exc:  # noqa: BLE001
        _engine = _StubEngine()
        REAL_ENGINE = False
        log.warning("Ядро недоступно (%s) — заглушка _StubEngine", exc)
    return _engine


def is_real_engine() -> bool:
    return REAL_ENGINE


def switch_mode(ui_mode: str) -> tuple:
    """
    Атомарное переключение режимов UmbraNet через адаптер.

    ui_mode:
      'dns_only' — DNS-сервер включён, DPI выключен (синий)
      'combo'    — DNS + DPI в режиме split+fake (чёрный)
      'dpi_only' — DNS + DPI в агрессивном режиме (красный)

    Возвращает (ok: bool, error: str). UI вызывает только этот метод
    для смены режима — не трогает start()/stop()/set_dpi_mode() напрямую.

    C1 guard: раньше Combo/DPI Only без целей блокировались на переключении.
    Теперь переключение разрешено всегда — блокируется только Старт (кнопка
    серая + диалог), чтобы пользователь мог спокойно листать режимы без выбранных
    доменов, но не мог запустить программу впустую.
    """
    # ── C1: переключение режимов теперь ВСЕГДА разрешено ──────────────────
    if ui_mode in ("combo", "dpi_only"):
        try:
            if not get_dpi_targets():
                log.info("switch_mode(%s): hostlist пуст — переключение разрешено, но Старт будет заблокирован", ui_mode)
        except Exception as exc:
            log_recoverable(log, 'Не удалось проверить DPI-цели при переключении режима', exc, level=logging.DEBUG)
    eng = get_engine()
    try:
        ok, err = eng.switch_mode(ui_mode)
        if ok:
            post_event({"type": "mode_changed", "mode": ui_mode})
            # Мы больше не отправляем status_changed отсюда, чтобы не сбивать
            # статус в UI (особенно если параллельно идет остановка сервера).
        else:
            post_event({"type": "error", "message": err})
        return ok, err
    except AttributeError:
        # Ядро старой версии без switch_mode — graceful fallback
        log.warning("switch_mode недоступен в ядре, используем ручное переключение")
        try:
            # fallback тоже разрешает переключение без целей — старт заблокируем отдельно
            MODE_MAP = {"dns_only": "off", "combo": "combo", "dpi_only": "dpi_only"}
            dpi_mode = MODE_MAP.get(ui_mode, "off")
            eng.set_dpi_mode(dpi_mode)
            post_event({"type": "mode_changed", "mode": ui_mode})
            return True, ""
        except Exception as exc:
            post_event({"type": "error", "message": str(exc)})
            return False, str(exc)
    except Exception as exc:
        # Убираем пост в event-queue ошибки при смене режима:
        # мы возвращаем err, и GUI сам решит показывать её или нет, 
        # чтобы избежать всплывающих красных табличек.
        log.error("switch_mode(%s) ошибка: %s", ui_mode, exc)
        return False, str(exc)


def get_current_mode() -> str:
    """
    Возвращает текущий UI-режим: 'dns_only' | 'combo' | 'dpi_only' | 'off'.
    Определяется по конфигу движка.
    """
    try:
        cfg = get_engine().config
        dpi = cfg.get("dpi_mode", "off")
        if dpi == "off":
            return "dns_only"
        if dpi == "combo":
            return "combo"
        if dpi == "dpi_only":
            return "dpi_only"
        return "dns_only"
    except Exception:
        return "dns_only"


# ── DPI hostlist guard (C1: «Не стрелять себе в ногу») ───────────────────────
def _normalize_dpi_targets(cfg: dict) -> list[str]:
    """Возвращает уникальный нормализованный список DPI-целей."""
    raw = list(cfg.get("routed_domains", []) or [])
    raw += list(cfg.get("subscribed_domains_set", set()) or [])
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        s = str(x).strip().lower().rstrip(".")
        if not s or "." not in s or " " in s or "/" in s:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def get_dpi_targets(cfg: dict | None = None) -> list[str]:
    """Публичный helper: список DPI-целей текущего или переданного конфига."""
    try:
        c = cfg if cfg is not None else getattr(get_engine(), "config", {}) or {}
        return _normalize_dpi_targets(c)
    except Exception:
        return []


def count_dpi_targets(cfg: dict | None = None) -> int:
    """Количество уникальных DPI-целей."""
    return len(get_dpi_targets(cfg))


def _dpi_guard_error_text() -> str:
    return (
        "Для режимов Combo / DPI Only нужны цели: включите хотя бы один сервис "
        "или добавьте домен в «Маршрутизация» → «Диспетчер задач». "
        "Без целей WinWS не должен касаться всего трафика."
    )


# ── Вспомогательные функции ядра (через адаптер, с мягким фолбэком) ──────────
def save_config(cfg: dict) -> None:
    try:
        from dns_server import save_config as _s  # type: ignore
        _s(cfg)
    except Exception:
        log.debug("[stub] save_config no-op")


def get_active_dns_profile(cfg: dict) -> dict:
    try:
        from profile_utils import get_active_dns_profile as _g  # type: ignore
        return _g(cfg)
    except Exception:
        return {"id": cfg.get("active_dns_profile", "builtin-xbox-dns"),
                "name": "xbox-dns.ru (заглушка)", "ipv4_primary": "176.99.11.77",
                "ipv4_secondary": "176.99.11.88", "ipv6_primary": "", "ipv6_secondary": "",
                "doh_url": "https://xbox-dns.ru/dns-query", "dot_host": "xbox-dns.ru",
                "dot_port": 853, "doq_host": "xbox-dns.ru", "doq_port": 853, "dnscrypt_stamp": ""}


def get_all_dns_profiles(cfg: dict) -> list:
    try:
        from profile_utils import get_all_dns_profiles as _g  # type: ignore
        return _g(cfg)
    except Exception:
        return [get_active_dns_profile(cfg)]


def make_new_user_dns_profile(existing: list) -> dict:
    """Создаёт заготовку нового пользовательского профиля."""
    try:
        from profile_utils import make_new_user_dns_profile as _g  # type: ignore
        return _g(existing)
    except Exception:
        import time
        return {"id": f"user-{time.time_ns()}", "name": "Новый профиль",
                "ipv4_primary": "", "ipv4_secondary": "", "ipv6_primary": "",
                "ipv6_secondary": "", "doh_url": "", "dot_host": "", "dot_port": 853,
                "doq_host": "", "doq_port": 853, "dnscrypt_stamp": "", "builtin": False}


def sanitize_dns_profile(raw: dict) -> dict:
    """Валидирует/нормализует профиль (IP, URL, порты)."""
    try:
        from profile_utils import sanitize_dns_profile as _g  # type: ignore
        return _g(raw, builtin=False)
    except Exception:
        return raw


def profile_builtin_id() -> str:
    try:
        from profile_utils import BUILTIN_PROFILE_ID  # type: ignore
        return BUILTIN_PROFILE_ID
    except Exception:
        return "builtin-xbox-dns"


def get_profile_by_id(cfg: dict, pid: str):
    try:
        from profile_utils import get_profile_by_id as _g  # type: ignore
        return _g(cfg, pid)
    except Exception:
        for p in get_all_dns_profiles(cfg):
            if p.get("id") == pid:
                return p
        return None


def max_user_profiles() -> int:
    try:
        from profile_utils import MAX_USER_DNS_PROFILES  # type: ignore
        return int(MAX_USER_DNS_PROFILES)
    except Exception:
        return 10


# ── Пинг профилей (TCP к DNS-серверу / HTTP к DoH) ──────────────────────────
def probe_host(host: str, port: int = 53, timeout: float = 2.5):
    """Возвращает (ok: bool, latency_ms: int|None)."""
    import socket
    import time
    if not host:
        return False, None
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
    try:
        t0 = time.perf_counter()
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(address)
        s.close()
        return True, int((time.perf_counter() - t0) * 1000)
    except Exception:
        return False, None


def probe_doh(url: str, timeout: float = 4.0):
    """Возвращает (ok: bool, latency_ms: int|None)."""
    import time
    if not url:
        return False, None
    try:
        import base64

        import requests
        from dnslib import DNSRecord
        req = DNSRecord.question("google.com", "A")
        b64 = base64.urlsafe_b64encode(req.pack()).rstrip(b"=").decode()
        t0 = time.perf_counter()
        resp = requests.get(url, headers={"Accept": "application/dns-message"},
                            params={"dns": b64}, timeout=timeout)
        resp.raise_for_status()
        DNSRecord.parse(resp.content)
        return True, int((time.perf_counter() - t0) * 1000)
    except Exception:
        return False, None


def get_routed_preset_map() -> dict:
    try:
        from routed_presets import get_routed_preset_map as _g  # type: ignore
        return _g()
    except Exception:
        return {}


def fallback_status() -> dict:
    """Что отвечает на DNS-запросы сейчас и были ли запасные пути (H3).

    Один вызов для интерфейса: ядро складывает факты в свой трекер
    (core/dns/fallback_state.py) прямо в resolve-пути, а UI только читает
    готовый снимок и рисует карточку «Сейчас отвечает» в «Сети и диагностике».

    Возвращает пустой словарь, если ядро недоступно (например, заглушка):
    интерфейс тогда честно скажет «данных пока нет», а не нарисует выдумку.
    """
    try:
        from fallback_state import get_fallback_state  # type: ignore
        return get_fallback_state().snapshot()
    except Exception as exc:
        log.debug("fallback_status недоступен: %s", exc)
        return {}


def reset_fallback_status() -> bool:
    """Сбрасывает статистику ответов (вызывается при «Стоп» и в тестах)."""
    try:
        from fallback_state import reset_fallback_state  # type: ignore
        reset_fallback_state()
        return True
    except Exception as exc:
        log.debug("reset_fallback_status не сработал: %s", exc)
        return False


def get_query_log():
    try:
        from query_log import get_query_log as _g  # type: ignore
        return _g()
    except Exception:
        return None


def add_query_log_event(domain: str, qtype: str = "SYS", source: str = "check",
                        rcode: str = "OK", note: str = "", routed: bool = False) -> bool:
    """Добавляет системную запись в журнал запросов.

    Используется для событий диагностики/автопочинки, чтобы пользователь видел
    в «Логах запросов» не только DNS-запросы, но и результат проверки утечек.
    """
    try:
        import time

        from query_log import QueryLogEntry  # type: ignore
        qlog = get_query_log()
        if qlog is None:
            return False
        qlog.add(QueryLogEntry(
            timestamp=time.time(),
            domain=domain,
            qtype=qtype,
            source=source,
            routed=bool(routed),
            rcode=rcode,
            note=note or "",
        ))
        return True
    except Exception as exc:
        log.debug("add_query_log_event failed: %s", exc)
        return False


def is_admin() -> bool:
    import sys
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception as exc:
            log.warning("Ошибка проверки IsUserAnAdmin: %s", exc)
            return False
    else:
        try:
            import os
            return os.geteuid() == 0
        except Exception:
            return False



# ── Предстартовая диагностика (без запуска DNS/DPI) ─────────────────────────
_KNOWN_DNS_CONFLICTS = {
    "adguard.exe": "AdGuard",
    "adguardsvc.exe": "AdGuard Service",
    "yogadns.exe": "YogaDNS",
    "dnscrypt-proxy.exe": "dnscrypt-proxy",
    "simplednscrypt.exe": "Simple DNSCrypt",
    "nextdns.exe": "NextDNS",
    "acrylicservice.exe": "Acrylic DNS Proxy",
    "technitiumdnsserver.exe": "Technitium DNS Server",
    "pihole-ftl.exe": "Pi-hole FTL",
}


def _detect_dns_conflict_processes() -> list[str]:
    """Ищет запущенные DNS-клиенты, которые часто занимают порт 53/перехватывают DNS.

    Это НЕ фатальная ошибка сама по себе: часть программ может быть выключена
    или работать в режиме, который не мешает UmbraNet. Но предупреждение нужно,
    чтобы обычный пользователь понимал вероятную причину конфликта порта.
    """
    found: list[str] = []
    try:
        for proc in get_running_processes():
            name = str(proc.get("name") or "").strip().lower()
            if name in _KNOWN_DNS_CONFLICTS:
                label = _KNOWN_DNS_CONFLICTS[name]
                if label not in found:
                    found.append(label)
    except Exception as exc:
        log_recoverable(log, 'Не удалось проверить конфликтующие DNS-процессы', exc, level=logging.WARNING)
    return found


def _win_shell_module():
    """Импортирует `core/win_shell.py` (пункт H4) для кода интерфейса.

    Модуль лежит вне пакета `umbranet`, поэтому путь до `core/` добавляется явно.
    Все отказы (нет файла, сломанный импорт) возвращаются как None — предупреждение
    в интерфейсе не должно ронять вкладку.
    """
    import sys
    from pathlib import Path

    try:
        core_dir = str(Path(__file__).resolve().parents[1] / "core")
        if core_dir not in sys.path:
            sys.path.insert(0, core_dir)
        import win_shell
        return win_shell
    except (ImportError, OSError) as exc:
        log.debug("Модуль win_shell недоступен: %s", exc)
        return None


def powershell_status_line() -> str:
    """Строка «PowerShell: доступен/нет» для отчёта и интерфейса (пункт H4)."""
    shell = _win_shell_module()
    if shell is None:
        return "PowerShell: статус неизвестен"
    try:
        return shell.status_line()
    except (OSError, ValueError) as exc:
        log.debug("Статус PowerShell недоступен: %s", exc)
        return "PowerShell: статус неизвестен"


def powershell_recheck() -> bool:
    """Проверяет PowerShell заново, минуя кэш проверки (пункт H4).

    Нужно, когда человек разблокировал powershell.exe и не хочет ждать, пока
    истечёт кэш: нажатие на предупреждение проверяет сразу.
    """
    shell = _win_shell_module()
    if shell is None:
        return False
    try:
        return bool(shell.powershell_status(force=True)["available"])
    except (OSError, ValueError) as exc:
        log.debug("Перепроверка PowerShell не удалась: %s", exc)
        return False


def powershell_warning() -> str:
    """Предупреждение для интерфейса: "" — всё хорошо, иначе — что случилось.

    Текст собирается один раз на состояние: причина, чем программа обходится,
    что именно недоступно и что сделать человеку.
    """
    win_shell = _win_shell_module()
    if win_shell is None:
        return ""
    try:
        # На не-Windows (разработка, тесты) предупреждать не о чем: там весь блок
        # системных вызовов не работает по определению.
        if not win_shell.IS_WINDOWS:
            return ""
        # Текст собирается в win_shell — вкладка и отчёт диагностики показывают
        # одно и то же, а не два похожих текста, которые разъезжаются со временем.
        text = win_shell.warning_text()
        return f"⚠  {text}" if text else ""
    except (OSError, ValueError, KeyError) as exc:
        log.debug("Предупреждение о PowerShell недоступно: %s", exc)
        return ""


def get_startup_health() -> dict:
    """Возвращает состояние готовности UmbraNet к запуску.

    Формат:
      {
        "severity": "ok" | "warning" | "error",
        "can_start": bool,
        "summary": str,
        "problems": list[str],
        "warnings": list[str],
        "real_engine": bool,
        "admin": bool,
      }

    Проверки быстрые и не запускают сервер: настоящее ядро, права админа,
    доступность DNS-порта, возможные конфликтующие DNS-программы.
    """
    eng = get_engine()
    problems: list[str] = []
    warnings: list[str] = []

    real = is_real_engine()
    admin = is_admin()

    if not real:
        problems.append(
            "Не подключено настоящее ядро DNS: приложение работает в режиме заглушки. "
            "Проверьте структуру папок и зависимости."
        )

    if not admin:
        problems.append(
            "Нет прав администратора: UmbraNet не сможет прописать системный DNS "
            "и занять порт 53. Запустите через start.bat или от имени администратора."
        )

    # Если нет прав администратора, порт 53 часто даст те же ошибки повторно.
    # Сначала просим UAC, а детальную проверку порта делаем уже с правами.
    # Если сервер уже запущен, его собственный порт будет занят — это нормально.
    if real and admin and not bool(getattr(eng, "running", False)):
        try:
            from core.dns.dns_server import preflight_check  # type: ignore
            ok, port_problems, port_warnings = preflight_check(getattr(eng, "config", {}) or {})
            if not ok:
                problems.extend(port_problems)
            warnings.extend(port_warnings)
        except Exception as exc:
            warnings.append(f"Не удалось выполнить проверку порта 53: {exc}")

    # Проверка наличия winws.exe, если DPI включен
    if real:
        try:
            dpi_mode = eng.config.get("dpi_mode", "off")
            # use_winws больше не спрашиваем: движок один — WinWS
            if dpi_mode != "off":
                from core.dpi.winws_engine import get_winws_engine
                if not get_winws_engine().is_available():
                    problems.append(
                        "Режим DPI включен, но e1-spike.exe не найден в папке bin/. "
                        "Поместите e1-spike.exe в папку bin/ рядом с программой "
                        "или переключитесь в режим 'Только DNS'."
                    )
        except Exception as exc:
            log_recoverable(log, 'Не удалось проверить наличие WinWS', exc, level=logging.WARNING)

    conflicts = _detect_dns_conflict_processes()
    if conflicts:
        warnings.append(
            "Найдены DNS-программы, которые могут конфликтовать с UmbraNet: "
            + ", ".join(conflicts)
        )

    try:
        dpi_mode = str((getattr(eng, "config", {}) or {}).get("dpi_mode", "off")).lower()
        if dpi_mode != "off":
            ok_dpi, reason_dpi = dpi_available()
            if not ok_dpi:
                warnings.append(
                    f"Выбран режим с DPI, но DPI недоступен: {reason_dpi}. "
                    "DNS запустится, но Combo/DPI Only не даст эффекта."
                )
    except Exception as exc:
        log_recoverable(log, 'Не удалось завершить проверку доступности DPI', exc, level=logging.WARNING)

    def _dedupe(items: list[str]) -> list[str]:
        seen = set()
        out = []
        for item in items:
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(item)
        return out

    problems = _dedupe(problems)
    warnings = _dedupe(warnings)

    if problems:
        severity = "error"
        summary = problems[0]
    elif warnings:
        severity = "warning"
        summary = warnings[0]
    else:
        severity = "ok"
        summary = "Готов к запуску"

    return {
        "severity": severity,
        "can_start": not problems,
        "summary": summary,
        "problems": problems,
        "warnings": warnings,
        "real_engine": real,
        "admin": admin,
        "running": bool(getattr(eng, "running", False)),
    }

def set_dns_to_localhost(fallback_ipv4: str = "1.1.1.1",
                         fallback_ipv6: str = "",
                         enable_ipv6: bool = True) -> tuple:
    """Прописывает системный DNS Windows на 127.0.0.1 (наш локальный сервер).

    Вызывается после успешного engine.start() — именно это переключает
    трафик браузера через наш DNS-сервер. Без этого вызова DNS-сервер
    работает, но Windows его игнорирует и продолжает слать запросы
    напрямую к провайдеру.

    Возвращает (ok: bool, message: str, adapters: list).
    """
    try:
        from process_monitor import set_dns_to_localhost as _g  # type: ignore
        return _g(fallback_ipv4=fallback_ipv4,
                  fallback_ipv6=fallback_ipv6,
                  enable_ipv6=enable_ipv6)
    except Exception as exc:
        log.warning("set_dns_to_localhost недоступна: %s", exc)
        return False, str(exc), []


def reset_dns_to_auto():
    try:
        from process_monitor import reset_dns_to_auto as _g  # type: ignore
        return _g()
    except Exception as exc:  # noqa: BLE001
        return False, f"недоступно: {exc}"


def doq_available() -> bool:
    try:
        from dns_transports import doq_available as _g  # type: ignore
        return bool(_g())
    except Exception:
        return False


def dnscrypt_available() -> bool:
    try:
        from dnscrypt import dnscrypt_available as _g  # type: ignore
        return bool(_g())
    except Exception:
        return False


def dpi_available() -> tuple[bool, str]:
    """Доступен ли используемый DPI-движок на этой машине.

    Текущий production-путь запускает ``bin/winws.exe`` напрямую. ``pydivert``
    нужен только старому экспериментальному ``DPIEngine`` и не является
    обязательной зависимостью для WinWS, поэтому проверка только pydivert
    давала ложное предупреждение даже при полностью установленном WinWS.
    Окончательная проверка драйвера и прав выполняется при старте процесса.
    """
    import sys
    if sys.platform != "win32":
        return False, "DPI работает только на Windows"
    try:
        from core.dpi.winws_engine import get_winws_engine  # type: ignore
        engine = get_winws_engine()
        if not engine.is_available():
            return False, f"e1-spike.exe не найден: {engine.exe_path}"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def mode_info(ui_mode: str | None = None) -> dict:
    """Человекочитаемое описание режима DNS Only / Combo / DPI Only."""
    ui_mode = ui_mode or get_current_mode()
    dpi_ok, dpi_reason = dpi_available()
    data = {
        "dns_only": {
            "title": "DNS Only",
            "emoji": "⚙",
            "summary": "Стабильный основной режим",
            "details": "Локальный DNS, маршрутизация доменов, DoH/DoT/DoQ/DNSCrypt, bogus-IP, blocklist/allowlist. DPI выключен.",
            "warning": "",
        },
        "combo": {
            "title": "Combo",
            "emoji": "⚡",
            "summary": "DNS + DPI combo",
            "details": "DNS-функции остаются активны, сверху включается DPI-движок в мягком combo-режиме.",
            "warning": "" if dpi_ok else f"DPI сейчас недоступен: {dpi_reason}. DNS-часть всё равно будет работать.",
        },
        "dpi_only": {
            "title": "DPI Only",
            "emoji": "🛡",
            "summary": "Агрессивный DPI-режим",
            "details": "DNS остаётся для резолва и журнала запросов, DPI работает в агрессивном режиме. Используйте, если DNS Only/Combo не помогает.",
            "warning": "" if dpi_ok else f"DPI сейчас недоступен: {dpi_reason}. Проверьте bin/winws.exe и поставляемые с ним файлы WinDivert.",
        },
    }
    return data.get(ui_mode, data["dns_only"])


# ── Диагностика / системный DNS ─────────────────────────────────────────────
def get_failover_providers(cfg: dict) -> list:
    try:
        from profile_utils import get_failover_providers as _g  # type: ignore
        return _g(cfg)
    except Exception:
        return [get_active_dns_profile(cfg)]


def get_current_dns() -> dict:
    """Текущие DNS-адреса системных адаптеров. {'Adapter': {'ipv4':[], 'ipv6':[]}}"""
    try:
        from process_monitor import get_current_dns as _g  # type: ignore
        return _g()
    except Exception:
        return {}


def flush_dns_cache() -> bool:
    try:
        from process_monitor import flush_dns_cache as _g  # type: ignore
        return bool(_g())
    except Exception:
        return False


def bogus_force_update() -> bool:
    """Принудительно обновляет список bogus-IP прямо сейчас.

    Вызывается из кнопки «Обновить список» в настройках.
    Блокирующий — выполнять в фоновом потоке (QThread) чтобы не морозить UI.
    Возвращает True при успехе, False при ошибке сети / пустом ответе.
    """
    try:
        eng = get_engine()
        updater = getattr(eng, "bogus_updater", None)
        if updater is None:
            return False
        return bool(updater.force_update())
    except Exception as exc:
        log.warning("bogus_force_update ошибка: %s", exc)
        return False


def bogus_last_updated() -> float | None:
    """Unix-timestamp последнего успешного обновления bogus-IP или None."""
    try:
        eng = get_engine()
        updater = getattr(eng, "bogus_updater", None)
        if updater is None:
            return None
        return updater.last_updated
    except Exception:
        return None


def is_domain_routed(domain: str, cfg: dict) -> bool:
    """Маршрутизируется ли домен (через логику ядра, учитывает поддомены)."""
    try:
        from routing_utils import is_domain_routed as _g  # type: ignore
        return bool(_g(domain, cfg))
    except Exception:
        d = (domain or "").rstrip(".").lower()
        for r in cfg.get("routed_domains", []):
            r = str(r).rstrip(".").lower()
            if d == r or d.endswith("." + r):
                return True
        return False




def is_domain_allowed(domain: str, cfg: dict) -> bool:
    """Проверяет пользовательский allowlist DNS-фильтра."""
    try:
        from routing_utils import is_domain_allowed as _g  # type: ignore
        return bool(_g(domain, cfg))
    except Exception:
        d = (domain or "").rstrip(".").lower()
        for a in cfg.get("allowlist_domains", []) or []:
            a = str(a).rstrip(".").lower()
            if a and (d == a or d.endswith("." + a)):
                return True
        return False


def protected_processes() -> tuple[str, ...]:
    """Процессы, которые нельзя удалить из маршрутизации (см. config_utils)."""
    try:
        from config_utils import PROTECTED_PROCESSES  # type: ignore
        return tuple(PROTECTED_PROCESSES)
    except Exception:
        return ("chrome.exe", "msedge.exe", "firefox.exe")


def is_protected_process(name: str) -> bool:
    """Защищён ли процесс от удаления (chrome/msedge/firefox)."""
    try:
        from config_utils import is_protected_process as _g  # type: ignore
        return bool(_g(name))
    except Exception:
        return str(name or "").strip().lower() in {
            "chrome.exe", "msedge.exe", "firefox.exe"}


def parse_domain_lines(text: str, include_adblock_exceptions: bool = True) -> list[str]:
    """Парсит ручной список доменов из plain/hosts/adblock-подобного текста.

    Поддерживает:
      example.com
      0.0.0.0 ads.example.com
      127.0.0.1 tracker.example.net
      ||ads.example.com^
      @@||good.example.com^

    include_adblock_exceptions=False полезен для blocklist: exception-правила
    AdBlock (`@@||domain^`) не должны случайно превращаться в блокировку.
    Комментарии (#, !, //) игнорируются. Возвращает нормализованный список без дублей.
    """
    import re
    domains: list[str] = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!", "//")):
            continue
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        is_exception = line.startswith("@@")
        if is_exception and not include_adblock_exceptions:
            continue
        if is_exception:
            line = line[2:]
        if line.startswith("||"):
            line = line[2:]
            line = line.split("^", 1)[0].split("/", 1)[0]
        else:
            parts = line.split()
            if len(parts) >= 2 and re.match(r"^[0-9a-fA-F:.]+$", parts[0]):
                line = parts[1]
            else:
                line = parts[0]
        line = line.strip("|^*/ ")
        d = normalize_domain(line)
        if not d or "." not in d:
            continue
        if d not in seen:
            seen.add(d)
            domains.append(d)
    return domains


def set_filter_lists(blocked: list[str] | None = None, allowlist: list[str] | None = None) -> bool:
    """Сохраняет ручные DNS-фильтры."""
    try:
        eng = get_engine()
        cfg = eng.config
        def _clean(values):
            out = []
            seen = set()
            for value in values or []:
                d = normalize_domain(value)
                if d and d not in seen:
                    seen.add(d)
                    out.append(d)
            return out

        if blocked is not None:
            cfg["blocked_domains"] = _clean(blocked)
        if allowlist is not None:
            cfg["allowlist_domains"] = _clean(allowlist)
        save_config(cfg)
        eng.reload_config()
        post_event({"type": "config_changed", "section": "dns_filter"})
        return True
    except Exception as exc:
        log.warning("set_filter_lists ошибка: %s", exc)
        return False

def is_domain_blocked(domain: str, cfg: dict) -> bool:
    """Проверяет пользовательский DNS-блоклист."""
    try:
        from routing_utils import is_domain_blocked as _g  # type: ignore
        return bool(_g(domain, cfg))
    except Exception:
        d = (domain or "").rstrip(".").lower()
        for b in cfg.get("blocked_domains", []) or []:
            b = str(b).rstrip(".").lower()
            if b and (d == b or d.endswith("." + b)):
                return True
        return False



def allow_domain(domain: str) -> bool:
    """Добавляет домен в allowlist и сохраняет конфиг."""
    d = normalize_domain(domain)
    if not d:
        return False
    eng = get_engine()
    cfg = eng.config
    allow = cfg.setdefault("allowlist_domains", [])
    if d not in allow:
        allow.append(d)
    save_config(cfg)
    eng.reload_config()
    post_event({"type": "config_changed", "section": "allowlist_domains", "domain": d})
    return True


def unallow_domain(domain: str) -> bool:
    """Удаляет домен из allowlist."""
    d = normalize_domain(domain)
    if not d:
        return False
    eng = get_engine()
    cfg = eng.config
    before = list(cfg.get("allowlist_domains", []) or [])
    cfg["allowlist_domains"] = [x for x in before if str(x).rstrip(".").lower() != d]
    changed = cfg["allowlist_domains"] != before
    if changed:
        save_config(cfg)
        eng.reload_config()
        post_event({"type": "config_changed", "section": "allowlist_domains", "domain": d})
    return changed

def block_domain(domain: str) -> bool:
    """Добавляет домен в DNS-блоклист и сохраняет конфиг."""
    d = normalize_domain(domain) if "normalize_domain" in globals() else (domain or "").strip().lower().rstrip(".")
    if not d:
        return False
    eng = get_engine()
    cfg = eng.config
    blocked = cfg.setdefault("blocked_domains", [])
    if d not in blocked:
        blocked.append(d)
    # Если домен был в обходе — оставляем запись: блокировка имеет приоритет.
    save_config(cfg)
    eng.reload_config()
    post_event({"type": "config_changed", "section": "blocked_domains", "domain": d})
    return True


def unblock_domain(domain: str) -> bool:
    """Удаляет домен из DNS-блоклиста и сохраняет конфиг."""
    d = normalize_domain(domain) if "normalize_domain" in globals() else (domain or "").strip().lower().rstrip(".")
    if not d:
        return False
    eng = get_engine()
    cfg = eng.config
    before = list(cfg.get("blocked_domains", []) or [])
    cfg["blocked_domains"] = [x for x in before if str(x).rstrip(".").lower() != d]
    changed = cfg["blocked_domains"] != before
    if changed:
        save_config(cfg)
        eng.reload_config()
        post_event({"type": "config_changed", "section": "blocked_domains", "domain": d})
    return changed

def is_windows() -> bool:
    try:
        from process_monitor import IS_WINDOWS  # type: ignore
        return bool(IS_WINDOWS)
    except Exception:
        import sys
        return sys.platform == "win32"


# ── Автозапуск (Windows) ────────────────────────────────────────────────────
def autostart_supported() -> bool:
    try:
        from autostart import is_supported  # type: ignore
        return bool(is_supported())
    except Exception:
        return False


def autostart_enabled() -> bool:
    try:
        from autostart import is_enabled  # type: ignore
        return bool(is_enabled())
    except Exception:
        return False


def autostart_set(flag: bool):
    try:
        from autostart import set_enabled  # type: ignore
        return set_enabled(flag)
    except Exception as exc:  # noqa: BLE001
        return False, f"недоступно: {exc}"


# ── Резервные копии конфига ─────────────────────────────────────────────────
def backup_create(cfg: dict):
    try:
        from backup_utils import create_config_backup  # type: ignore
        return create_config_backup(cfg)
    except Exception:  # noqa: BLE001
        return None


def backup_list():
    try:
        from backup_utils import list_config_backups  # type: ignore
        return list_config_backups()
    except Exception:
        return []


def backup_load(path: str):
    try:
        from backup_utils import load_config_backup  # type: ignore
        return load_config_backup(path)
    except Exception:
        return None


# ── Стратегии upstream ──────────────────────────────────────────────────────
def upstream_modes() -> list[str]:
    try:
        from upstream_strategy import ALL_MODES  # type: ignore
        return list(ALL_MODES)
    except Exception:
        return ["sequential", "parallel", "fastest"]


# ── Браузерный DoH (Chrome / Edge) ──────────────────────────────────────────
def set_chrome_doh(doh_url: str | None = None):
    try:
        from process_monitor import set_chrome_doh as _g  # type: ignore
        return _g(doh_url) if doh_url else _g()
    except Exception as exc:  # noqa: BLE001
        return False, f"недоступно: {exc}"


def set_edge_doh(doh_url: str | None = None):
    try:
        from process_monitor import set_edge_doh as _g  # type: ignore
        return _g(doh_url) if doh_url else _g()
    except Exception as exc:  # noqa: BLE001
        return False, f"недоступно: {exc}"


def reset_chrome_doh():
    try:
        from process_monitor import reset_chrome_doh as _g  # type: ignore
        return _g()
    except Exception as exc:  # noqa: BLE001
        return False, f"недоступно: {exc}"


def reset_edge_doh():
    # в ядре отдельной функции нет — пробуем, иначе понятное сообщение
    try:
        from process_monitor import reset_edge_doh as _g  # type: ignore
        return _g()
    except Exception:
        return False, "Сброс DoH для Edge: удалите политику вручную или через Chrome-сброс"


def get_running_processes() -> list:
    """Список запущенных процессов (pid/name). Пусто, если недоступно.

    Путь к .exe здесь специально не отдаём: обход всех процессов с exe
    вешает UI. Для иконок — resolve_process_exe / resolve_process_exes.
    """
    try:
        from process_monitor import get_running_processes as _g  # type: ignore
        return _g()
    except Exception:
        return []


def resolve_process_exe(name: str) -> str | None:
    """Абсолютный путь к .exe процесса по имени, либо None."""
    try:
        from process_monitor import resolve_process_exe as _g  # type: ignore
        path = _g(name)
        return str(path) if path else None
    except Exception:
        return None


def resolve_process_exes(names, interrupt=None) -> dict:
    """Пакетный поиск путей к .exe. interrupt() → True останавливает обход."""
    try:
        from process_monitor import resolve_process_exes as _g  # type: ignore
        found = _g(names, interrupt=interrupt)
        return dict(found) if found else {}
    except Exception:
        return {}


# DoH-URL встроенного профиля xbox-dns.ru (для окна «Тест DNS»).
XBOX_DOH_URL = "https://xbox-dns.ru/dns-query"


def active_doh_url() -> str:
    """DoH-URL активного профиля, иначе xbox-dns.ru."""
    try:
        prof = get_active_dns_profile(get_engine().config)
        return (prof or {}).get("doh_url") or XBOX_DOH_URL
    except Exception:
        return XBOX_DOH_URL


def resolve_system(domain: str, timeout: float = 4.0):
    """Резолв домена через системный DNS. Возвращает (ips|None, ms|error_str)."""
    import socket
    import time
    try:
        t0 = time.perf_counter()
        ips = list({item[4][0] for item in socket.getaddrinfo(domain, None)})
        return ips, int((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        return None, str(exc)


def resolve_doh(domain: str, doh_url: str, timeout: float = 5.0):
    """Резолв домена через DoH (json, затем wireformat). (ips|None, ms|error_str)."""
    import base64
    import time
    try:
        import requests
        from dnslib import DNSRecord
    except Exception as exc:
        return None, f"нет библиотек: {exc}"
    # JSON-форма
    try:
        t0 = time.perf_counter()
        resp = requests.get(doh_url, headers={"Accept": "application/dns-json"},
                            params={"name": domain, "type": "A"}, timeout=timeout)
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            ms = int((time.perf_counter() - t0) * 1000)
            ips = [a["data"] for a in resp.json().get("Answer", []) if a.get("type") == 1]
            if ips:
                return ips, ms
    except Exception as exc:
        log_recoverable(log, 'Диагностический DoH-запрос не удался; пробуем резервный путь', exc, level=logging.DEBUG)
    # wireformat
    try:
        t0 = time.perf_counter()
        req = DNSRecord.question(domain, "A")
        b64 = base64.urlsafe_b64encode(req.pack()).rstrip(b"=").decode()
        resp = requests.get(doh_url, headers={"Accept": "application/dns-message"},
                            params={"dns": b64}, timeout=timeout)
        resp.raise_for_status()
        ms = int((time.perf_counter() - t0) * 1000)
        rec = DNSRecord.parse(resp.content)
        ips = [str(rr.rdata) for rr in rec.rr if rr.rtype == 1]
        return ips, ms
    except Exception as exc:
        return None, str(exc)


def check_port(host: str, port: int = 443, timeout: float = 3.0):
    """True=открыт, False=закрыт(reset), None=таймаут."""
    import socket
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except TimeoutError:
        return None
    except Exception:
        return False


# ── Транспорт DNS (xbox_dns_mode) ───────────────────────────────────────────
# Порядок и метаданные транспортов для UI. Ключ = значение xbox_dns_mode.
TRANSPORTS = ["udp", "doh", "dot", "doq", "dnscrypt"]

TRANSPORT_LABELS = {
    "udp": "UDP DNS",
    "doh": "DoH (HTTPS)",
    "dot": "DoT (TLS)",
    "doq": "DoQ (QUIC)",
    "dnscrypt": "DNSCrypt",
}

# Цепочки fallback (зеркало _XBOX_FALLBACK_ORDER из dns_server.py) — для подсказок.
TRANSPORT_FALLBACK = {
    "udp": ["udp", "doh", "dot", "doq"],
    "doh": ["doh", "dot", "udp", "doq"],
    "dot": ["dot", "doh", "udp", "doq"],
    "doq": ["doq", "doh", "dot", "udp"],
    "dnscrypt": ["dnscrypt", "doh", "dot", "udp"],
}


def get_transport() -> str:
    """Текущий предпочтительный транспорт (xbox_dns_mode)."""
    try:
        mode = str(get_engine().config.get("xbox_dns_mode", "doh")).strip().lower()
        return mode if mode in TRANSPORTS else "doh"
    except Exception:
        return "doh"


def set_transport(mode: str) -> bool:
    """Сохраняет предпочтительный транспорт и перезагружает конфиг."""
    mode = str(mode).strip().lower()
    if mode not in TRANSPORTS:
        return False
    try:
        eng = get_engine()
        eng.config["xbox_dns_mode"] = mode
        save_config(eng.config)
        eng.reload_config()
        return True
    except Exception:
        return False


def transport_available(mode: str) -> tuple[bool, str]:
    """
    (доступен?, причина-если-нет). UDP/DoH/DoT доступны всегда.
    DoQ требует aioquic, DNSCrypt требует pynacl + sdns-штамп в профиле.
    """
    mode = str(mode).strip().lower()
    if mode in ("udp", "doh", "dot"):
        return True, ""
    if mode == "doq":
        if doq_available():
            return True, ""
        return False, "Нужен пакет aioquic (pip install aioquic)"
    if mode == "dnscrypt":
        if not dnscrypt_available():
            return False, "Нужен пакет pynacl (pip install pynacl)"
        # Если PyNaCl есть, DNSCrypt считается доступным для выбора.
        # Отсутствие sdns:// штампа — не блокировка: UI откроет диалог
        # выбора готового DNSCrypt-резолвера и создаст профиль автоматически.
        try:
            prof = get_active_dns_profile(get_engine().config)
            if not (prof or {}).get("dnscrypt_stamp"):
                return True, "Нужно выбрать DNSCrypt-резолвер (sdns://)"
        except Exception as exc:
            log_recoverable(log, 'Не удалось проверить выбранный DNSCrypt-профиль', exc, level=logging.WARNING)
        return True, ""
    return False, "Неизвестный транспорт"


# ── «Авто»-транспорт (выбор самого быстрого) ────────────────────────────────
# Ядро не знает про режим "auto" (его sanitize_config отвергнет), поэтому флаг
# «авто» храним в отдельном UI-файле рядом с config.json. Когда он включён,
# приложение само меряет транспорты и применяет самый быстрый как xbox_dns_mode.
import os as _os

try:                                    # ядро уже добавлено в sys.path выше
    import ui_state as _ui_state
except Exception:                       # pragma: no cover - страховка от смены путей
    _ui_state = None

# Оставлено для совместимости: путь теперь считает ui_state.state_path().
_UI_STATE_FILE = _os.path.join(_APP_DIR, "umbranet_ui.json")
if _ui_state is not None:
    try:
        _UI_STATE_FILE = _ui_state.state_path()
    except Exception as exc:
        log_recoverable(log, 'Не удалось определить путь состояния UI; используем стандартный', exc, level=logging.DEBUG)


# P1-2: файл состояния UI — общий с theme.py, поэтому читаем и пишем только
# через core/ui_state.py (блокировка + атомарная замена). Раньше здесь был
# свой open(...,"w"), который при гонке двух модулей затирал тему, а при краше
# оставлял пустой файл — настройки UI «сбрасывались сами».
def _load_ui_state() -> dict:
    try:
        return _ui_state.load_state()
    except Exception:
        return {}


def _save_ui_state(state: dict) -> None:
    try:
        _ui_state.save_state(state)
    except Exception as exc:
        log_recoverable(log, 'Не удалось сохранить состояние UI', exc, level=logging.WARNING)


def _patch_ui_state(key: str, value) -> None:
    """Меняет один ключ, не затирая остальные (в т.ч. тему из theme.py)."""
    try:
        _ui_state.update_state(**{key: value})
    except Exception as exc:
        log_recoverable(log, 'Не удалось обновить состояние UI', exc, level=logging.WARNING)


_update_checker = None


def get_update_checker():
    """Shared notification-only checker, created from the GUI thread."""
    global _update_checker
    if _update_checker is None:
        from core.update_checker import UpdateChecker
        from umbranet import __version__
        _update_checker = UpdateChecker(
            __version__, include_prereleases=bool(_load_ui_state().get("update_prereleases", False))
        )
    return _update_checker


def set_update_channel(include_prereleases: bool) -> None:
    get_update_checker().set_channel(include_prereleases)
    _patch_ui_state("update_prereleases", bool(include_prereleases))


def auto_transport_enabled() -> bool:
    """«Авто» в маршруте DNS. Для новой установки — включён."""
    return bool(_load_ui_state().get("auto_transport", True))


def set_auto_transport(enabled: bool) -> None:
    _patch_ui_state("auto_transport", bool(enabled))


def get_doctor_notify() -> bool:
    """Показывать ли фоновые уведомления Автодоктора/аудита (2026-10-06).

    Жалоба пользователя: «постоянные уведомления, которые нельзя отключить».
    Галочка живёт в umbranet_ui.json (поле doctor_notify), по умолчанию вкл.
    """
    try:
        import ui_state as _us
        return bool(_us.get_value("doctor_notify", True))
    except Exception:
        return True


def set_doctor_notify(value: bool) -> None:
    """Сохраняет состояние галочки «Уведомления» в виджете лечения."""
    try:
        import ui_state as _us
        _us.update_state(doctor_notify=bool(value))
    except Exception:
        pass


def get_nav_order(default_keys: list[str]) -> list[str]:
    """Возвращает сохранённый порядок вкладок бокового меню.

    Безопасно мержит сохранённый список с текущим набором вкладок:
    удалённые ключи отбрасываются, новые добавляются в конец.
    """
    defaults = [str(x) for x in default_keys]
    saved = _load_ui_state().get("nav_order", [])
    if not isinstance(saved, list):
        saved = []
    out: list[str] = []
    for key in saved:
        key = str(key)
        if key in defaults and key not in out:
            out.append(key)
    for key in defaults:
        if key not in out:
            out.append(key)
    return out


def set_nav_order(order: list[str], valid_keys: list[str] | None = None) -> None:
    """Сохраняет порядок вкладок бокового меню."""
    valid = set(str(x) for x in valid_keys) if valid_keys is not None else None
    clean: list[str] = []
    for key in order or []:
        key = str(key)
        if valid is not None and key not in valid:
            continue
        if key not in clean:
            clean.append(key)
    _patch_ui_state("nav_order", clean)


def get_favorite_services(valid_services: list[str] | None = None) -> list[str]:
    """Список избранных сервисов в главной вкладке."""
    valid = set(valid_services or []) if valid_services is not None else None
    raw = _load_ui_state().get("favorite_services", [])
    if not isinstance(raw, list):
        raw = []
    out: list[str] = []
    for svc in raw:
        svc = str(svc)
        if valid is not None and svc not in valid:
            continue
        if svc not in out:
            out.append(svc)
    return out


def set_favorite_services(services: list[str], valid_services: list[str] | None = None) -> None:
    valid = set(valid_services or []) if valid_services is not None else None
    out: list[str] = []
    for svc in services or []:
        svc = str(svc)
        if valid is not None and svc not in valid:
            continue
        if svc not in out:
            out.append(svc)
    _patch_ui_state("favorite_services", out)


_DEV_MODE: bool | None = None


def get_developer_mode() -> bool:
    """Режим разработчика: снять блокировку DNS/DPI у сервисов в главном меню."""
    global _DEV_MODE
    if _DEV_MODE is None:
        try:
            _DEV_MODE = bool(_load_ui_state().get("developer_mode", False))
        except Exception:
            _DEV_MODE = False
    return bool(_DEV_MODE)


def set_developer_mode(on: bool) -> None:
    global _DEV_MODE
    _DEV_MODE = bool(on)
    _patch_ui_state("developer_mode", _DEV_MODE)


def measure_transports() -> dict:
    """
    Меряет латентность доступных транспортов активного профиля.
    Возвращает {mode: ms} только для тех, что ответили. Используется «Авто».
    """
    prof = get_active_dns_profile(get_engine().config)
    results: dict[str, int] = {}

    ip4 = (prof or {}).get("ipv4_primary")
    if ip4:
        ok, ms = probe_host(ip4, 53)
        if ok and ms is not None:
            results["udp"] = ms

    doh = (prof or {}).get("doh_url")
    if doh:
        ok, ms = probe_doh(doh)
        if ok and ms is not None:
            results["doh"] = ms

    dot_host = (prof or {}).get("dot_host") or (prof or {}).get("ipv4_primary")
    dot_port = (prof or {}).get("dot_port", 853)
    if dot_host:
        ok, ms = probe_host(dot_host, int(dot_port or 853))
        if ok and ms is not None:
            results["dot"] = ms

    return results


def pick_fastest_transport() -> str | None:
    """Самый быстрый из доступных транспортов (по измерению). None — никто не ответил."""
    res = measure_transports()
    if not res:
        return None
    return min(res, key=res.get)


# ── Готовые DNSCrypt-резолверы (проверенные публичные sdns:// штампы) ─────────
# Источник: DNSCrypt/dnscrypt-resolvers (v3 public-resolvers). Штампы проверены
# живыми запросами. Используются диалогом выбора при включении DNSCrypt.
DNSCRYPT_RESOLVERS = [
    {
        "name": "AdGuard DNS",
        "desc": "Блокировка рекламы и трекеров",
        "stamp": "sdns://AQMAAAAAAAAAETk0LjE0MC4xNC4xNDo1NDQzINErR_JS3PLCu_iZEIbq95zkSV2LFsigxDIuUso_OQhzIjIuZG5zY3J5cHQuZGVmYXVsdC5uczEuYWRndWFyZC5jb20",
    },
    {
        "name": "AdGuard Family",
        "desc": "+ блокировка взрослого контента, безопасный поиск",
        "stamp": "sdns://AQMAAAAAAAAAFDk0LjE0MC4xNC4xNTo1NDQzILgxXdexS27jIKRw3C7Wsao5jMnlhvhdRUXWuMm1AFq6ITIuZG5zY3J5cHQuZmFtaWx5Lm5zMS5hZGd1YXJkLmNvbQ",
    },
    {
        "name": "Quad9",
        "desc": "Блокировка вредоносных доменов, без логов",
        "stamp": "sdns://AQMAAAAAAAAADDkuOS45Ljk6ODQ0MyBnyEe4yHWM0SAkVUO-dWdG3zTfHYTAC4xHA2jfgh2GPhkyLmRuc2NyeXB0LWNlcnQucXVhZDkubmV0",
    },
    {
        "name": "Cloudflare",
        "desc": "Быстрый, ориентирован на приватность",
        "stamp": "sdns://AgcAAAAAAAAABzEuMC4wLjEAEmRucy5jbG91ZGZsYXJlLmNvbQovZG5zLXF1ZXJ5",
    },
    {
        "name": "Scaleway (fr.dnscrypt.org)",
        "desc": "Франция, без логов, DNSSEC",
        "stamp": "sdns://AQcAAAAAAAAADjIxMi40Ny4yMjguMTM2IOgBuE6mBr-wusDOQ0RbsV66ZLAvo8SqMa4QY2oHkDJNHzIuZG5zY3J5cHQtY2VydC5mci5kbnNjcnlwdC5vcmc",
    },
]


def active_has_dnscrypt_stamp() -> bool:
    """Есть ли sdns:// штамп у активного профиля."""
    try:
        prof = get_active_dns_profile(get_engine().config)
        return bool((prof or {}).get("dnscrypt_stamp"))
    except Exception:
        return False


def apply_dnscrypt_resolver(name: str, stamp: str) -> bool:
    """
    Подготовить активный профиль к работе через выбранный DNSCrypt-резолвер:
    создаёт пользовательский профиль со штампом и делает его активным.
    Если профиль с таким именем уже есть — переиспользует его.
    Возвращает True при успехе.
    """
    try:
        eng = get_engine()
        cfg = eng.config
        users = cfg.setdefault("user_dns_profiles", [])
        title = f"{name} (DNSCrypt)"

        # уже есть такой профиль?
        existing = next((p for p in users if p.get("name") == title), None)
        if existing is not None:
            existing["dnscrypt_stamp"] = stamp
            cfg["active_dns_profile"] = existing.get("id")
        else:
            if len(users) >= max_user_profiles():
                return False
            prof = make_new_user_dns_profile(users)
            prof["name"] = title[:40]
            prof["dnscrypt_stamp"] = stamp
            users.append(prof)
            cfg["active_dns_profile"] = prof.get("id")

        save_config(cfg)
        eng.reload_config()
        return True
    except Exception:
        return False


# ── Диагностика апстримов (для таблицы во вкладке «Сеть») ────────────────────
def probe_udp_dns(host: str, qname: str = "google.com", qtype: str = "A",
                  port: int = 53, timeout: float = 3.0):
    """UDP DNS-запрос к host. Возвращает (ok, latency_ms|None, comment)."""
    import socket
    import time
    if not host:
        return False, None, "не задан"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
    try:
        from dnslib import DNSRecord
        req = DNSRecord.question(qname, qtype)
        s = socket.socket(family, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.sendto(req.pack(), address)
        data, _ = s.recvfrom(65535)
        s.close()
        resp = DNSRecord.parse(data)
        return True, int((time.perf_counter() - t0) * 1000), f"ответов: {len(resp.rr)}"
    except Exception as exc:
        return False, None, str(exc)


def probe_tcp(host: str, port: int = 53, timeout: float = 2.5):
    """TCP-коннект к host:port. Возвращает (ok, latency_ms|None, comment)."""
    import socket
    import time
    if not host:
        return False, None, "не задан"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
    try:
        t0 = time.perf_counter()
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(address)
        s.close()
        return True, int((time.perf_counter() - t0) * 1000), "порт доступен"
    except Exception as exc:
        return False, None, str(exc)


def probe_doh_diag(url: str, timeout: float = 4.0):
    """DoH-проверка (wireformat). Возвращает (ok, latency_ms|None, comment)."""
    import time
    if not url:
        return False, None, "не задан"
    try:
        import base64

        import requests
        from dnslib import DNSRecord
        req = DNSRecord.question("google.com", "A")
        b64 = base64.urlsafe_b64encode(req.pack()).rstrip(b"=").decode()
        t0 = time.perf_counter()
        resp = requests.get(url, headers={"Accept": "application/dns-message"},
                            params={"dns": b64}, timeout=timeout)
        resp.raise_for_status()
        DNSRecord.parse(resp.content)
        return True, int((time.perf_counter() - t0) * 1000), "DoH доступен"
    except Exception as exc:
        return False, None, str(exc)


def run_diagnostics() -> list:
    """
    Прогоняет полную диагностику апстримов (как в старой версии).
    Возвращает список строк: (source, status, latency_ms|None, comment),
    где status ∈ {OK, FAIL, WARN, INFO}.
    """
    eng = get_engine()
    cfg = eng.config
    prof = get_active_dns_profile(cfg) or {}
    port = int(cfg.get("listen_port", 53) or 53)
    ipv6_on = bool(cfg.get("enable_ipv6", True))

    rows = []

    # ── Local DNS (наш сервер на 127.0.0.1 / ::1) ──
    ok, ms, c = probe_udp_dns("127.0.0.1", qtype="A", port=port)
    rows.append(("Local DNS UDP IPv4", "OK" if ok else "FAIL", ms, c))
    ok, ms, c = probe_tcp("127.0.0.1", port)
    rows.append(("Local DNS TCP IPv4", "OK" if ok else "FAIL", ms, c))

    if ipv6_on:
        ok, ms, c = probe_udp_dns("::1", qtype="AAAA", port=port)
        rows.append(("Local DNS UDP IPv6", "OK" if ok else "FAIL", ms, c))
        ok, ms, c = probe_tcp("::1", port)
        rows.append(("Local DNS TCP IPv6", "OK" if ok else "FAIL", ms, c))
    else:
        rows.append(("Local DNS UDP IPv6", "WARN", None, "IPv6 отключён в настройках"))
        rows.append(("Local DNS TCP IPv6", "WARN", None, "IPv6 отключён в настройках"))

    # ── Активный профиль ──
    if prof.get("ipv4_primary"):
        ok, ms, c = probe_udp_dns(prof["ipv4_primary"], qtype="A")
        rows.append(("Профиль UDP IPv4", "OK" if ok else "FAIL", ms, c))
    else:
        rows.append(("Профиль UDP IPv4", "INFO", None, "не задан"))

    if prof.get("ipv6_primary"):
        ok, ms, c = probe_udp_dns(prof["ipv6_primary"], qtype="AAAA")
        rows.append(("Профиль UDP IPv6", "OK" if ok else "FAIL", ms, c))
    else:
        rows.append(("Профиль UDP IPv6", "INFO", None, "не задан"))

    if prof.get("doh_url"):
        ok, ms, c = probe_doh_diag(prof["doh_url"])
        rows.append(("Профиль DoH", "OK" if ok else "FAIL", ms, c))
    else:
        rows.append(("Профиль DoH", "INFO", None, "не задан"))

    # ── Fallback DNS ──
    if cfg.get("fallback_dns"):
        ok, ms, c = probe_udp_dns(cfg["fallback_dns"], qtype="A")
        rows.append(("Fallback DNS IPv4", "OK" if ok else "FAIL", ms, c))
    else:
        rows.append(("Fallback DNS IPv4", "INFO", None, "не задан"))

    if cfg.get("fallback_dns6"):
        ok, ms, c = probe_udp_dns(cfg["fallback_dns6"], qtype="AAAA")
        rows.append(("Fallback DNS IPv6", "OK" if ok else "FAIL", ms, c))
    else:
        rows.append(("Fallback DNS IPv6", "INFO", None, "не задан"))

    return rows


# ── Шина событий (Push-уведомления из ядра в UI) ─────────────────────────────
#
# Проблема: UI опрашивал access_status() по таймеру — это polling.
# При быстром переключении режимов UI мог показывать устаревший статус
# в течение всего интервала опроса.
#
# Решение: потокобезопасная очередь событий.
# Ядро (или адаптер) кладёт события через post_event().
# UI забирает их через drain_events() — вызывать из QTimer раз в ~200 мс.
#
# Формат события: dict с обязательным полем "type", остальное — произвольно.
# Примеры:
#   {"type": "status_changed", "running": True, "mode": "combo"}
#   {"type": "mode_changed",   "mode": "dns_only"}
#   {"type": "error",          "message": "Нет прав администратора"}

import queue as _queue

_event_queue: _queue.Queue = _queue.Queue(maxsize=256)


def post_event(event: dict) -> None:
    """Кладёт событие в очередь. Вызывается из любого потока."""
    try:
        _event_queue.put_nowait(event)
    except _queue.Full:
        pass  # переполнение — старые события дропаем молча


def drain_events() -> list:
    """Забирает все накопившиеся события. Вызывать из UI-потока (QTimer)."""
    events = []
    while True:
        try:
            events.append(_event_queue.get_nowait())
        except _queue.Empty:
            break
    return events



# ── Мастер диагностики домена ───────────────────────────────────────────────
def normalize_domain(value: str) -> str:
    """Приводит URL/домен из поля ввода к чистому домену."""
    d = (value or "").strip().lower()
    for pref in ("https://", "http://"):
        d = d.removeprefix(pref)
    d = d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if ":" in d and not d.startswith("["):
        host, port = d.rsplit(":", 1)
        if port.isdigit():
            d = host
    d = d.removeprefix("www.")
    return d.rstrip(".")


def get_current_dns_settings(use_cache: bool = True) -> dict:
    """Текущие DNS-адреса Windows по адаптерам. На других ОС — пустой dict.

    use_cache=False — свежее чтение (аудит после Стопа обязан видеть реальное
    состояние, а не кэш пятилетней давности — поле 2026-10-06).
    """
    try:
        from process_monitor import get_current_dns as _g  # type: ignore
        return _g(use_cache=use_cache) or {}
    except Exception:
        return {}


def get_browser_doh_policies() -> dict:
    """Читает DoH-политики Chrome/Edge из реестра Windows."""
    if not is_windows():
        return {}
    try:
        from process_monitor import _run_ps  # type: ignore
        ps = """
$items = @(
  @('Chrome HKCU','HKCU:\\SOFTWARE\\Policies\\Google\\Chrome'),
  @('Chrome HKLM','HKLM:\\SOFTWARE\\Policies\\Google\\Chrome'),
  @('Edge HKCU','HKCU:\\SOFTWARE\\Policies\\Microsoft\\Edge'),
  @('Edge HKLM','HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge')
)
foreach ($it in $items) {
  $name = $it[0]; $path = $it[1]
  if (Test-Path $path) {
    $p = Get-ItemProperty -Path $path -ErrorAction SilentlyContinue
    $mode = [string]$p.DnsOverHttpsMode
    $tpl = [string]$p.DnsOverHttpsTemplates
    if ($mode -or $tpl) { Write-Output ($name + '|' + $mode + '|' + $tpl) }
  }
}
"""
        stdout, _stderr, _code = _run_ps(ps, timeout=8)
        out: dict[str, dict] = {}
        for line in (stdout or "").splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            name, mode, tpl = parts
            browser = "Chrome" if name.startswith("Chrome") else "Edge"
            scope = "HKLM" if "HKLM" in name else "HKCU"
            out.setdefault(browser, {"entries": []})["entries"].append({
                "scope": scope, "mode": mode, "templates": tpl,
            })
        for _browser, data in out.items():
            entries = data.get("entries") or []
            data["mode"] = ", ".join(e.get("mode", "") for e in entries if e.get("mode"))
            data["templates"] = ", ".join(e.get("templates", "") for e in entries if e.get("templates"))
        return out
    except Exception:
        return {}


def _dns_query_ips(server: str, domain: str, qtype: str = "A", port: int = 53,
                   timeout: float = 3.0) -> tuple[list[str], int | None, str]:
    """DNS-запрос к конкретному серверу. Возвращает (ips, ms, error/comment)."""
    import socket
    import time
    try:
        from dnslib import QTYPE, DNSRecord
        req = DNSRecord.question(domain, qtype)
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        address = (server, port, 0, 0) if family == socket.AF_INET6 else (server, port)
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        t0 = time.perf_counter()
        sock.sendto(req.pack(), address)
        data, _ = sock.recvfrom(65535)
        ms = int((time.perf_counter() - t0) * 1000)
        sock.close()
        resp = DNSRecord.parse(data)
        wanted = QTYPE.A if qtype.upper() == "A" else QTYPE.AAAA
        ips = [str(rr.rdata) for rr in resp.rr if int(rr.rtype) == int(wanted)]
        return ips, ms, f"ответов: {len(resp.rr)}"
    except Exception as exc:
        return [], None, str(exc)


def _is_bogus_ip(ip: str, cfg: dict) -> bool:
    try:
        import ipaddress

        from bogus_ips import (  # type: ignore
            build_bogus_index,
            build_bogus_index_with_cache,
        )
        try:
            ips, subnets = build_bogus_index_with_cache(cfg, _CORE_DIR)
        except Exception:
            ips, subnets = build_bogus_index(cfg)
        obj = ipaddress.ip_address(ip)
        if obj in ips:
            return True
        return any(obj.version == net.version and obj in net for net in subnets)
    except Exception:
        return False


def detect_service_blocking_type(service_id: str) -> dict:
    """Service-specific detector для YouTube/Discord/ChatGPT.

    Проверяет несколько endpoint'ов сервиса и агрегирует вердикт. Это важнее,
    чем одиночная проверка youtube.com/discord.com.
    """
    try:
        from blocking_services import detect_service_blocking  # type: ignore
        cfg = get_engine().config
        return detect_service_blocking(
            service_id,
            secure_doh_url=active_doh_url(),
            secure_resolver=lambda d, url: (
                (lambda ips, meta: (ips or [], "" if ips else str(meta)))(*resolve_doh(d, url))
            ),
            bogus_checker=lambda ip: _is_bogus_ip(ip, cfg),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "service_id": service_id,
            "service": service_id,
            "summary": f"Ошибка service detector: {exc}",
            "recommended_mode": "unknown",
            "severity": "error",
            "endpoints": [],
        }


def blocking_service_options() -> list[tuple[str, str]]:
    try:
        from blocking_services import SERVICE_PROFILES  # type: ignore
        return [("domain", "Обычный домен")] + [
            (key, value.get("label", key)) for key, value in SERVICE_PROFILES.items()
        ]
    except Exception:
        return [("domain", "Обычный домен")]


def detect_blocking_type(domain: str) -> dict:
    """Определяет вероятный тип блокировки домена/сервиса.

    Это безопасная диагностика: DNS/TCP/TLS/UDP probes без обхода и без
    модификации трафика. Используется как база для будущего выбора DNS Only /
    Combo / DPI Only.
    """
    try:
        from blocking_detector import detect_blocking  # type: ignore
        cfg = get_engine().config
        return detect_blocking(
            domain,
            secure_doh_url=active_doh_url(),
            secure_resolver=lambda d, url: (
                (lambda ips, meta: (ips or [], "" if ips else str(meta)))(*resolve_doh(d, url))
            ),
            bogus_checker=lambda ip: _is_bogus_ip(ip, cfg),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "domain": normalize_domain(domain),
            "verdict": "unknown",
            "severity": "error",
            "summary": f"Ошибка detector: {exc}",
            "recommended_mode": "unknown",
            "checks": [],
        }


def diagnose_domain(domain: str) -> dict:
    """Мастер диагностики: почему домен может не открываться."""
    d = normalize_domain(domain)
    eng = get_engine()
    cfg = getattr(eng, "config", {}) or {}
    port = int(cfg.get("listen_port", 53) or 53)
    steps: list[dict] = []
    actions: list[str] = []

    def add(status: str, title: str, detail: str = ""):
        steps.append({"status": status, "title": title, "detail": detail})

    if not d:
        return {"domain": d, "summary": "Введите домен для диагностики", "severity": "error", "steps": steps, "actions": ["Введите домен, например chatgpt.com"]}

    health = get_startup_health()
    running = bool(getattr(eng, "running", False))
    if running:
        add("ok", "UmbraNet DNS-сервер запущен", f"Локальный порт: 127.0.0.1:{port}")
    else:
        sev = "error" if not health.get("can_start", True) else "warn"
        add(sev, "UmbraNet DNS-сервер не запущен", health.get("summary", "Нажмите Старт"))
        actions.append("Запустите UmbraNet кнопкой «Старт» от имени администратора.")

    dns_settings = get_current_dns_settings()
    if is_windows():
        if dns_settings:
            adapters_ok = []
            adapters_bad = []
            for name, vals in dns_settings.items():
                v4 = vals.get("ipv4") or []
                v6 = vals.get("ipv6") or []
                if "127.0.0.1" in v4 and (not cfg.get("enable_ipv6", True) or "::1" in v6 or not v6):
                    adapters_ok.append(name)
                else:
                    adapters_bad.append(name)
            if adapters_bad:
                add("warn", "Не все активные адаптеры смотрят на UmbraNet", "Проверьте DNS: " + ", ".join(adapters_bad[:4]))
                actions.append("Нажмите «Старт» ещё раз или «Сбросить системный DNS», затем запустите UmbraNet заново.")
            else:
                add("ok", "Системный DNS указывает на UmbraNet", ", ".join(adapters_ok[:4]) or "127.0.0.1")
        else:
            add("info", "Системные DNS-настройки не прочитаны", "Нет активных адаптеров с ручными DNS или PowerShell не вернул данные.")
    else:
        add("info", "Проверка системного DNS пропущена", "Детальная проверка рассчитана на Windows.")

    policies = get_browser_doh_policies()
    active_doh = active_doh_url()
    if policies:
        bad = []
        good = []
        for br, data in policies.items():
            tpl = data.get("templates", "") or ""
            if active_doh and active_doh in tpl:
                good.append(br)
            else:
                bad.append(f"{br}: {tpl or data.get('mode') or 'policy'}")
        if bad:
            add("warn", "Браузер может обходить системный DNS", "; ".join(bad))
            actions.append("В разделе «DoH в браузере» пропишите активный профиль в Chrome/Edge или отключите Secure DNS в браузере.")
        if good:
            add("ok", "DoH-политика браузера совпадает с активным профилем", ", ".join(good))
    else:
        add("info", "Браузерный DoH не задан политикой UmbraNet", "Если сайт не открывается только в Chrome/Edge — проверьте Secure DNS в настройках браузера.")

    routed = is_domain_routed(d, cfg)
    if routed or cfg.get("route_all"):
        add("ok", "Домен попадает в маршрутизацию UmbraNet", "route_all включён" if cfg.get("route_all") else d)
    else:
        add("warn", "Домен не добавлен в маршрутизацию", "UmbraNet может отдать его через обычный fallback DNS.")
        actions.append(f"Добавьте {d} в «Маршрутизация», если для него нужен обход.")

    local_ips, local_ms, local_err = _dns_query_ips("127.0.0.1", d, "A", port=port)
    if local_ips:
        bogus = [ip for ip in local_ips if _is_bogus_ip(ip, cfg)]
        if bogus:
            add("error", "Локальный DNS вернул bogus-IP провайдера", ", ".join(bogus))
            actions.append("Обновите список bogus-IP и проверьте активный DNS-профиль/DoH.")
        else:
            add("ok", "Локальный DNS отвечает", f"{local_ms} мс → {', '.join(local_ips[:5])}")
    else:
        status = "error" if running else "warn"
        add(status, "Локальный DNS не дал A-ответ", local_err)
        if running:
            actions.append("Проверьте порт 53, firewall и журнал UmbraNet.")

    prof = get_active_dns_profile(cfg) or {}
    doh_url = prof.get("doh_url") or active_doh
    doh_ips, doh_ms = (None, None)
    if doh_url:
        doh_ips, doh_ms = resolve_doh(d, doh_url)
        if doh_ips:
            add("ok", "Активный DoH-профиль отвечает", f"{doh_ms} мс → {', '.join(doh_ips[:5])}")
        else:
            add("warn", "Активный DoH-профиль не ответил", str(doh_ms))
            actions.append("Попробуйте другой DNS-профиль или транспорт в «DNS-профили».")

    sys_ips, sys_ms = resolve_system(d)
    if sys_ips:
        bogus = [ip for ip in sys_ips if _is_bogus_ip(ip, cfg)]
        if bogus:
            add("warn", "Системный DNS отдаёт bogus-IP", f"{', '.join(bogus)} — провайдер подменяет DNS-ответ")
            actions.append("Используйте DoH/DoT/DoQ профиль и убедитесь, что Windows DNS указывает на 127.0.0.1.")
        else:
            add("ok", "Системный резолв работает", f"{sys_ms} мс → {', '.join(sys_ips[:5])}")
    else:
        add("warn", "Системный DNS не резолвит домен", str(sys_ms))

    test_pool = local_ips or (doh_ips if isinstance(doh_ips, list) else []) or (sys_ips if isinstance(sys_ips, list) else [])
    v4 = [ip for ip in test_pool if ":" not in ip][:2]
    if v4:
        opened = False
        closed = False
        details = []
        for ip in v4:
            r = check_port(ip, 443)
            if r is True:
                opened = True; details.append(f"{ip}: открыт")
            elif r is False:
                closed = True; details.append(f"{ip}: закрыт/reset")
            else:
                details.append(f"{ip}: таймаут")
        if opened:
            add("ok", "TCP 443 доступен", "; ".join(details))
        elif closed:
            add("error", "IP/порт 443 недоступен", "; ".join(details))
            actions.append("DNS работает, но соединение к IP блокируется. Тут нужен DPI/VPN/прокси, одного DNS недостаточно.")
        else:
            add("warn", "TCP 443 не подтвердился", "; ".join(details))
    else:
        add("info", "TCP 443 не проверен", "Нет IPv4-адреса для подключения.")

    try:
        leak = eng.check_dns_leak() if running else {"status": "unknown", "title": "DNS не запущен"}
        st = leak.get("status", "unknown")
        if st == "ok":
            add("ok", "DNS-утечек не обнаружено", leak.get("title", "OK"))
        elif st in ("leak", "risk"):
            add("warn", "Возможна DNS/IPv6-утечка", leak.get("title", "Проверьте IPv6"))
            actions.append("Откройте «Проверка DNS-утечки» и примените исправление IPv6/DNS.")
        else:
            add("info", "DNS leak не проверен", leak.get("title", "Неизвестно"))
    except Exception as exc:
        add("info", "DNS leak не проверен", str(exc))

    rank = {"error": 3, "warn": 2, "info": 1, "ok": 0}
    worst = max((rank.get(x["status"], 1) for x in steps), default=0)
    severity = {3: "error", 2: "warning", 1: "info", 0: "ok"}[worst]
    summary = {
        "ok": "Критических проблем не найдено",
        "info": "Критических проблем не найдено, есть информационные заметки",
        "warning": "Найдены предупреждения — см. рекомендации",
        "error": "Найдены проблемы, которые мешают открытию сайта",
    }[severity]

    dedup_actions = []
    seen = set()
    for a in actions:
        if a not in seen:
            seen.add(a); dedup_actions.append(a)
    if not dedup_actions and severity == "ok":
        dedup_actions.append("Если сайт всё равно не открывается — очистите DNS-кэш браузера и попробуйте режим инкогнито.")

    return {"domain": d, "summary": summary, "severity": severity, "steps": steps, "actions": dedup_actions}

def diagnostics_report(rows: list) -> str:
    """Текстовый отчёт диагностики (для «Скопировать отчёт»)."""
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = get_current_mode()
    lines = [
        f"UmbraNet — диагностика апстримов ({ts})",
        f"Режим: {mode}",
        "-" * 48,
    ]
    for source, status, latency, comment in rows:
        lat = f"{latency} мс" if isinstance(latency, int) else "-"
        lines.append(f"{source}: {status} | {lat} | {comment}")
    return "\n".join(lines)


# ── Network Repair Engine ──────────────────────────────────────────────────
# ── Health Score / Full Report ─────────────────────────────────────────────
def _winws_status_for_report() -> dict:
    try:
        # ВАЖНО: настоящий WinWS singleton уже лежит в engine.winws. Не импортируем
        # core.dpi.winws_engine напрямую как отдельный пакет: из-за плоских импортов
        # ядра можно случайно создать второй singleton, у которого process=None.
        # Именно это давало рассинхрон: DPI-блок видел winws_running=True, а Health
        # думал, что WinWS остановлен.
        eng = get_engine()
        w = getattr(eng, "winws", None)
        if w is None:
            from winws_engine import get_winws_engine  # type: ignore
            w = get_winws_engine()
        return w.status() if hasattr(w, "status") else {
            "available": w.is_available(), "running": w.is_running(),
            "exe_path": str(getattr(w, "exe_path", "")), "last_error": "",
        }
    except Exception as exc:
        return {"available": False, "running": False, "last_error": str(exc)}


def health_score() -> dict:
    """Считает общий Health Score 0..100 и список проверок."""
    eng = get_engine()
    cfg = getattr(eng, "config", {}) or {}
    running = bool(getattr(eng, "running", False))
    score = 100
    checks: list[dict] = []
    actions: list[str] = []

    def add(status: str, title: str, detail: str = "", penalty: int = 0, action: str = ""):
        nonlocal score
        score -= max(0, int(penalty or 0))
        checks.append({"status": status, "title": title, "detail": detail, "penalty": penalty})
        if action:
            actions.append(action)

    # H4: если PowerShell недоступен, часть функций не работает — но программа
    # продолжает работать через netsh/ipconfig/taskkill. Это предупреждение, а не
    # ошибка: раньше человек видел «функция сломана» без причины.
    warning = powershell_warning()
    if warning:
        add("warn", "PowerShell недоступен",
            "Очистка кэша, DNS-настройки и закрытие winws.exe работают через "
            "системные утилиты; журнал DNS-запросов и автопочинка — нет.", 10,
            "Разрешите powershell.exe в антивирусе или политике безопасности.")

    try:
        sh = get_startup_health()
        if sh.get("severity") == "error":
            add("error", "Предстартовая проверка", sh.get("summary", "Ошибка"), 25,
                "Запустите от администратора и устраните ошибку из верхнего баннера.")
        elif sh.get("severity") == "warning":
            add("warn", "Предстартовая проверка", sh.get("summary", "Предупреждение"), 8)
        else:
            add("ok", "Предстартовая проверка", "OK")
    except Exception as exc:
        add("warn", "Предстартовая проверка недоступна", str(exc), 5)

    if running:
        add("ok", "DNS-сервер UmbraNet", "Запущен")
    else:
        add("warn", "DNS-сервер UmbraNet", "Остановлен", 15, "Нажмите «Старт» перед автопочинкой и проверкой утечек.")

    dns = get_current_dns_settings()
    enable_ipv6 = bool(cfg.get("enable_ipv6", True))
    if dns:
        bad = []
        for name, vals in dns.items():
            v4 = vals.get("ipv4") or []
            v6 = vals.get("ipv6") or []
            if "127.0.0.1" not in v4:
                bad.append(f"{name}: IPv4 DNS не 127.0.0.1")
            if enable_ipv6 and v6 and "::1" not in v6:
                # fec0::-заглушки Windows не считаем критикой, но Health чуть снижаем.
                real_v6 = [x for x in v6 if not str(x).lower().startswith("fec0:")]
                if real_v6:
                    bad.append(f"{name}: IPv6 DNS не ::1")
        if bad:
            add("warn", "Системный DNS", "; ".join(bad[:3]), 20, "Запустите «Автопочинку сети» уровня 1.")
        else:
            add("ok", "Системный DNS", "Адаптеры смотрят на UmbraNet")
    else:
        add("info", "Системный DNS", "Не удалось прочитать DNS или нет ручных DNS", 5)

    try:
        if running:
            leak = eng.check_dns_leak()
            if leak.get("status") == "ok":
                add("ok", "DNS/DPI утечки", leak.get("title", "OK"))
            else:
                penalty = 25 if leak.get("dns_leak") else 12
                add("warn", "DNS/DPI утечки", leak.get("title", "Есть риск"), penalty,
                    "Если dns_leak=True — запустите автопочинку сети.")
        else:
            add("info", "DNS/DPI утечки", "Не проверено: сервер остановлен", 5)
    except Exception as exc:
        add("info", "DNS/DPI утечки", str(exc), 5)

    policies = get_browser_doh_policies()
    active = active_doh_url()
    if policies:
        bad = []
        for br, data in policies.items():
            tpl = data.get("templates", "")
            if active and active not in tpl:
                bad.append(f"{br}: {tpl or data.get('mode', '')}")
        if bad:
            add("warn", "Браузерный DoH", "; ".join(bad), 10, "Запустите автопочинку уровня 2 или проверьте Secure DNS в браузере.")
        else:
            add("ok", "Браузерный DoH", "Политики не конфликтуют")
    else:
        add("ok", "Браузерный DoH", "Политики UmbraNet/Chrome/Edge не заданы")

    dpi_mode = str(cfg.get("dpi_mode", "off"))
    routed_targets = list(cfg.get("routed_domains", []) or [])
    routed_targets += list(cfg.get("subscribed_domains_set", set()) or [])
    routed_targets = [str(x).strip().lower() for x in routed_targets if str(x).strip() and "." in str(x)]
    no_dpi_targets = (dpi_mode != "off" and len(set(routed_targets)) == 0)
    if no_dpi_targets:
        add(
            "warn", "DPI-цели",
            "Не выбраны сервисы/домены в главном меню — WinWS не должен трогать весь интернет",
            15,
            "Включите нужные сервисы/домены в главном меню, затем запустите DPI/Combo.",
        )

    st = _winws_status_for_report()
    if dpi_mode != "off":
        if not st.get("available"):
            add("error", "WinWS", "winws.exe не найден", 25, "Проверьте папку bin и установку UmbraNet.")
        elif no_dpi_targets:
            add("info", "WinWS", "Не запускается без выбранных DPI-целей — это безопасное поведение")
        elif running and not st.get("running"):
            add("warn", "WinWS", st.get("last_error") or "Не запущен", 20, "Откройте/скопируйте winws.log и проверьте стратегию.")
        elif st.get("last_error"):
            add("warn", "WinWS", st.get("last_error"), 10)
        else:
            add("ok", "WinWS", "Доступен/запущен")
    else:
        add("ok", "WinWS", "DPI выключен")

    try:
        from core.dpi.strategy_manager import get_strategy_manager  # type: ignore
        m = get_strategy_manager()
        if no_dpi_targets:
            add("info", "DPI-стратегия", f"{cfg.get('dpi_strategy', 'uz1')} выбрана, ожидает цели из главного меню")
        else:
            args = m.get_args(
                cfg.get("dpi_strategy", "uz1"),
                routed_domains=routed_targets,
                require_hostlist=(cfg.get("dpi_mode", "off") != "off"),
            )
            if not args:
                add("warn", "DPI-стратегия", getattr(m, "last_error", "args пустые"), 15)
            else:
                add("ok", "DPI-стратегия", f"{cfg.get('dpi_strategy', 'uz1')} ({len(args)} args, целей: {getattr(m, 'last_hostlist_count', 0)})")
    except Exception as exc:
        add("info", "DPI-стратегия", str(exc), 5)

    score = max(0, min(100, score))
    if score >= 85:
        state, title = "ok", "🟢 Всё хорошо"
    elif score >= 60:
        state, title = "warn", "🟡 Есть предупреждения"
    else:
        state, title = "error", "🔴 Нужно исправить"
    return {"score": score, "state": state, "title": title, "checks": checks, "actions": list(dict.fromkeys(actions))}


def full_diagnostics_report() -> str:
    """Полный текстовый отчёт UmbraNet для копирования."""
    import datetime
    from umbranet import __version__
    eng = get_engine()
    cfg = getattr(eng, "config", {}) or {}
    hs = health_score()
    st = _winws_status_for_report()
    lines = [
        "UmbraNet Full Diagnostic Report",
        "=" * 52,
        f"version: {__version__}",
        f"time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"health: {hs.get('score')}/100 — {hs.get('title')}",
        f"real_engine: {is_real_engine()}",
        f"admin: {is_admin()}",
        f"engine_running: {bool(getattr(eng, 'running', False))}",
        powershell_status_line(),
        f"mode: {get_current_mode()} / raw dpi_mode={cfg.get('dpi_mode', 'off')}",
        f"transport: {cfg.get('xbox_dns_mode', '')}",
        f"profile: {(get_active_dns_profile(cfg) or {}).get('name', '')}",
        f"dpi_strategy: {cfg.get('dpi_strategy', 'uz1')}",
        f"dpi_targets_count: {len(set([str(x).strip().lower() for x in (list(cfg.get('routed_domains', []) or []) + list(cfg.get('subscribed_domains_set', set()) or [])) if str(x).strip() and '.' in str(x)]))}",
        "",
        "Health checks:",
    ]
    for c in hs.get("checks", []):
        lines.append(f"- {c.get('status')} | -{c.get('penalty', 0)} | {c.get('title')}: {c.get('detail', '')}")
    if hs.get("actions"):
        lines += ["", "Recommended actions:"]
        lines.extend(f"- {a}" for a in hs.get("actions", []))
    lines += ["", "DNS adapters:"]
    dns = get_current_dns_settings()
    if dns:
        for name, vals in dns.items():
            lines.append(f"- {name}: IPv4={vals.get('ipv4', [])}; IPv6={vals.get('ipv6', [])}")
    else:
        lines.append("- <empty/unavailable>")
    lines += ["", "Browser DoH policies:"]
    policies = get_browser_doh_policies()
    if policies:
        for br, data in policies.items():
            lines.append(f"- {br}: mode={data.get('mode','')} templates={data.get('templates','')}")
    else:
        lines.append("- none")
    lines += [
        "", "WinWS:",
        f"available: {st.get('available')}",
        f"running: {st.get('running')}",
        f"exe: {st.get('exe_path', '')}",
        f"log: {st.get('log_path', '')}",
        f"last_exit_code: {st.get('last_exit_code')}",
        f"last_error: {st.get('last_error', '')}",
        "", "Network repair last report:",
        network_repair_report_text(),
    ]
    return "\n".join(lines)


def network_repair_plan(level: str = "soft") -> dict:
    try:
        from network_repair import repair_plan  # type: ignore
        return repair_plan(level)
    except Exception as exc:
        return {"level": level, "title": "План недоступен", "danger": "unknown", "steps": [str(exc)]}


def network_repair_soft(level: str = "soft") -> dict:
    """Автопочинка сети выбранного уровня: snapshot → repair → verify."""
    eng = get_engine()
    try:
        from network_repair import repair_with_level  # type: ignore
        dpi_running = False
        try:
            winws = getattr(eng, "winws", None)
            dpi_running = bool(winws and winws.is_running())
        except Exception:
            dpi_running = False
        report = repair_with_level(
            level,
            getattr(eng, "config", {}) or {},
            server_running=bool(getattr(eng, "running", False)),
            dpi_running=dpi_running,
        )
        try:
            add_query_log_event(
                "[Автопочинка сети]",
                source="fixed" if report.get("ok") else "error",
                rcode="OK" if report.get("ok") else "FAIL",
                note=(report.get("after") or {}).get("title") or "; ".join(report.get("errors") or []) or "готово",
            )
        except Exception as exc:
            log_recoverable(log, 'Не удалось записать результат восстановления DNS в журнал запросов', exc, level=logging.DEBUG)
        return report
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "steps": [], "before": {}, "after": {}}


def network_restore_latest() -> tuple[bool, str]:
    """Откат сети к состоянию ДО UmbraNet: снапшот → иначе DHCP.

    Используется кнопкой «Откатить сеть» в «Сеть и диагностика», а также
    при остановке/выходе (см. app._hard_stop_runtime).
    """
    try:
        from network_repair import restore_user_dns  # type: ignore
        ok, msg = restore_user_dns()
        try:
            add_query_log_event(
                "[Откат сети]",
                source="fixed" if ok else "error",
                rcode="OK" if ok else "FAIL",
                note=msg,
            )
        except Exception as exc:
            log_recoverable(log, 'Не удалось записать результат сброса DNS в журнал запросов', exc, level=logging.DEBUG)
        return ok, msg
    except Exception as exc:
        return False, str(exc)


def verify_teardown() -> dict:
    """Проверка ПОСЛЕ остановки: не осталось ли хвостов, которые «продолжают работать».

    Зачем: раньше после «Стоп» никто не проверял итог. Пользователь видел, что
    обход всё ещё работает, а «лечение» не находило ничего — потому что смотреть
    было не на что. Аудит отвечает на два вопроса:
      1) не остался ли наш winws.exe (он держит WinDivert и продолжает обработку
         трафика — прямой механизм «Стоп нажат, а обход живёт»);
      2) вернулся ли системный DNS к настройкам пользователя (не остался ли
         захваченным наш 127.0.0.1 — тогда резолв уходит на fallback-резолвер
         и сайты «продолжают работать» уже без движка).
    Чужие winws.exe — только заметка: это не наши хвосты.
    Ничего не чинит сам — только докладывает (лог + журнал запросов + событие UI).
    """
    report = {
        "ok": True,
        "problems": [],
        "notes": [],
        "winws_own": [],
        "winws_foreign": [],
        "dns_localhost_adapters": [],
    }

    # 1) Наши winws.exe.
    try:
        from winws_engine import get_winws_engine  # type: ignore
        w = get_winws_engine()
        own = []
        if w and hasattr(w, "scan_own_processes"):
            own, _scanned = w.scan_own_processes()
        report["winws_own"] = [{"pid": p, "path": str(path)} for p, path in own]
        if own:
            report["ok"] = False
            report["problems"].append(
                "после остановки работает winws.exe нашей установки: "
                + ", ".join(f"PID {p}" for p, _ in own[:3])
                + " — обход мог продолжать работать"
            )
        if w and hasattr(w, "foreign_processes"):
            foreign = w.foreign_processes() or []
            if foreign:
                report["winws_foreign"] = [{"pid": p, "path": str(path)} for p, path in foreign[:5]]
                report["notes"].append(
                    "рядом работают чужие winws.exe (сторонние DPI-программы): "
                    + ", ".join(f"PID {p}" for p, _ in foreign[:3])
                )
    except Exception as exc:
        log_recoverable(log, 'Аудит остановки: не удалось проверить процессы winws', exc, level=logging.WARNING)

    # 2) Системный DNS: не остался ли захваченным. Читаем СВЕЖЕЕ состояние —
    # кэш чтения врёт после недавнего восстановления (поле 2026-10-06).
    try:
        dns = get_current_dns_settings(use_cache=False) or {}
        leftovers = []
        for name, vals in dns.items():
            if not isinstance(vals, dict):
                continue
            servers = [str(s).strip() for s in (vals.get("ipv4") or [])]
            servers += [str(s).strip() for s in (vals.get("ipv6") or [])]
            if any(s in ("127.0.0.1", "::1") for s in servers):
                leftovers.append(str(name))
        report["dns_localhost_adapters"] = leftovers
        if leftovers:
            report["ok"] = False
            report["problems"].append(
                "системный DNS всё ещё указывает на UmbraNet: "
                + ", ".join(leftovers[:3])
                + " — резолв может уходить на fallback-резолвер"
            )
    except Exception as exc:
        log_recoverable(log, 'Аудит остановки: не удалось прочитать системный DNS', exc, level=logging.WARNING)

    return report


def network_snapshot_info() -> dict:
    """Информация о последнем снапшоте сети (для тултипа кнопки отката)."""
    try:
        from network_repair import snapshot_info  # type: ignore
        return snapshot_info()
    except Exception:
        return {"exists": False, "path": "", "time_local": "", "adapters": 0}


def dns_snapshot_before_change() -> dict | None:
    """Снимает снапшот DNS перед первым вмешательством UmbraNet.

    Вызывается ПЕРЕД set_dns_to_localhost: только тогда снапшот содержит
    настоящие DNS пользователя, а не наш 127.0.0.1. Возвращает снапшот или None.
    """
    try:
        from network_repair import ensure_dns_snapshot  # type: ignore
        return ensure_dns_snapshot(getattr(get_engine(), "config", {}) or {})
    except Exception as exc:
        log.debug("dns_snapshot_before_change: %s", exc)
        return None


def network_repair_report_text(report: dict | None = None) -> str:
    try:
        from network_repair import format_report, last_report  # type: ignore
        return format_report(report or last_report())
    except Exception as exc:
        return f"UmbraNet Network Repair Report\nОшибка: {exc}"


def update_subscriptions_async(on_done=None) -> None:
    eng = get_engine()
    if hasattr(eng, "update_subscriptions_async"):
        eng.update_subscriptions_async(on_done)
    elif on_done:
        # В режиме заглушки вызываем on_done сразу напрямую.
        on_done(True, len(eng.config.get("routed_subscriptions", [])) * 123)


def dpi_strategy_ai_plan(mode: str = "quick") -> dict:
    """Технический dry-run план будущей AI-генерации Uz."""
    try:
        from ai_strategy.autotuner import plan_autotune  # type: ignore
        return plan_autotune(getattr(get_engine(), "config", {}) or {}, mode=mode)
    except Exception as exc:
        log.warning("dpi_strategy_ai_plan error: %s", exc)
        return {
            "schema_version": 1,
            "stage": "error",
            "error": str(exc),
            "generation": {},
            "masks": [],
            "candidates": [],
        }


def dpi_strategy_ai_probe_basic(timeout: float = 6.0) -> dict:
    """Базовые probes YouTube/Discord для будущего автоподбора.

    Важно: это синхронная техническая функция. В UI её надо запускать в фоне.
    """
    try:
        from ai_strategy.probes import run_basic_probes  # type: ignore
        return run_basic_probes(timeout=float(timeout or 6.0))
    except Exception as exc:
        log.warning("dpi_strategy_ai_probe_basic error: %s", exc)
        return {
            "stage": "basic_probes",
            "ok": False,
            "score": 0,
            "error": str(exc),
            "services": [],
        }


def _dpi_expand_ai_variant_args(raw_args: list[str], routed_domains: list[str]) -> tuple[list[str], str]:
    """Раскрывает {hostlist}/{bin}/{lists} для временного AI-варианта."""
    try:
        m = _dpi_get_strategy_manager()
        hostlist_arg, _count = m._write_active_hostlist(routed_domains)
        if not hostlist_arg:
            return [], "generation hostlist пуст"
        args = [str(a).strip() for a in raw_args or [] if str(a).strip()]
        if not args:
            return [], "пустые args варианта"
        if "{hostlist}" in " ".join(args):
            args = [a.replace("{hostlist}", hostlist_arg) for a in args]
        else:
            new_args = [hostlist_arg]
            for arg in args:
                new_args.append(arg)
                if arg == "--new":
                    new_args.append(hostlist_arg)
            args = new_args
        bin_dir = m.strategies_dir.parent / "bin"
        lists_dir = m.strategies_dir.parent / "lists"
        final = [a.replace("{bin}", str(bin_dir.absolute())).replace("{lists}", str(lists_dir.absolute())) for a in args]
        unresolved = [a for a in final if "{" in a or "}" in a]
        if unresolved:
            return [], f"неразрешённые плейсхолдеры: {unresolved[:2]}"
        return final, ""
    except Exception as exc:
        return [], str(exc)


def _dpi_probe_check_label(check: dict) -> str:
    host = str(check.get("host") or check.get("name") or "?")
    path = str(check.get("path") or "")
    if path and path != "/":
        host = f"{host}{path}"
    return host


def _dpi_probe_check_status(check: dict) -> str:
    if check.get("ok"):
        status = str(check.get("status") or "OK")
        ms = check.get("ms")
        return f"OK ({status}, {ms} мс)" if ms is not None else f"OK ({status})"
    stage = str(check.get("stage") or "fail")
    err = str(check.get("error") or check.get("first_line") or "")
    if len(err) > 90:
        err = err[:87] + "..."
    return f"FAIL stage={stage}" + (f" • {err}" if err else "")


def _dpi_required_status_from_score(score: dict) -> dict:
    """Достаёт обязательные checks из probe-результата лучшего варианта."""
    out = {}
    probe = score.get("probe") if isinstance(score, dict) else {}
    services = probe.get("services", []) if isinstance(probe, dict) else []
    for service in services or []:
        if not isinstance(service, dict):
            continue
        name = str(service.get("service") or "unknown")
        req = service.get("required") if isinstance(service.get("required"), dict) else {}
        if req:
            out[name] = {str(k): bool(v) for k, v in req.items()}
    return out


def _dpi_probe_failure_summary(score: dict, max_items: int = 8) -> list[str]:
    """Коротко объясняет, почему probe/score не прошёл.

    Возвращает строки вида:
      Discord required voice_regions: FAIL
      Discord media.discordapp.net: FAIL stage=tls_or_http
    """
    out: list[str] = []
    if not isinstance(score, dict):
        return out
    if score.get("error"):
        out.append(f"Ошибка: {score.get('error')}")
        return out[:max_items]
    probe = score.get("probe") if isinstance(score.get("probe"), dict) else {}
    services = probe.get("services", []) if isinstance(probe, dict) else []
    for service in services or []:
        if not isinstance(service, dict):
            continue
        sname = str(service.get("service") or "unknown")
        title = "YouTube" if sname == "youtube" else ("Discord" if sname == "discord" else sname)
        req = service.get("required") if isinstance(service.get("required"), dict) else {}
        for key, val in req.items():
            if not bool(val):
                out.append(f"{title} required {key}: FAIL")
        # Если весь сервис fail — покажем несколько конкретных endpoint'ов.
        if not service.get("ok"):
            for check in service.get("checks") or []:
                if not isinstance(check, dict) or check.get("ok"):
                    continue
                out.append(f"{title} {_dpi_probe_check_label(check)}: {_dpi_probe_check_status(check)}")
                if len(out) >= max_items:
                    return out[:max_items]
    if not out and not score.get("ok"):
        svc = score.get("service_scores") if isinstance(score.get("service_scores"), dict) else {}
        if svc:
            out.append("Низкий score сервисов: " + ", ".join(f"{k}={v}" for k, v in svc.items()))
        elif score.get("score") is not None:
            out.append(f"Низкий score: {score.get('score')}")
    return out[:max_items]


def _dpi_build_generation_report(result: dict, target_analysis: dict | None = None) -> dict:
    """Строит человекочитаемый отчёт генерации из best/scores/probe."""
    target_analysis = target_analysis or {}
    best = result.get("best") if isinstance(result.get("best"), dict) else {}
    scores = result.get("scores") if isinstance(result.get("scores"), list) else []
    reason = str(result.get("error") or result.get("reason") or "")
    reason_map = {
        "below_threshold": "лучший вариант ниже минимального score",
        "required_probes_failed": "обязательные проверки Discord/YouTube не прошли",
        "no_variants": "нет вариантов для проверки",
        "cancelled": "генерация отменена пользователем",
    }
    reason_text = reason_map.get(reason, reason or "—")

    lines: list[str] = []
    gen_count = target_analysis.get("generation_domains_count")
    coverage = target_analysis.get("coverage") if isinstance(target_analysis.get("coverage"), dict) else {}
    if gen_count is not None:
        parts = []
        for sid in ("youtube", "discord"):
            c = coverage.get(sid) if isinstance(coverage.get(sid), dict) else {}
            if c:
                parts.append(f"{c.get('label', sid)}: {c.get('domains_count', 0)}")
        lines.append(f"Список истины: {gen_count} доменов" + (f" ({', '.join(parts)})" if parts else ""))

    if best:
        lines.append(
            "Лучший вариант: "
            f"{best.get('variant_id', '—')} • seed={best.get('seed_id', '—')} • "
            f"mutation={best.get('mutation', '—')} • mask={best.get('mask_id', '—')}"
        )
        lines.append(
            f"Score: {best.get('score', 0)} / raw {best.get('raw_score', 0)} "
            f"(min {result.get('min_score', '—')})"
        )
        svc_scores = best.get("service_scores") if isinstance(best.get("service_scores"), dict) else {}
        if svc_scores:
            lines.append("Сервисы: " + ", ".join(f"{k}={v}" for k, v in svc_scores.items()))

    probe = best.get("probe") if isinstance(best, dict) else {}
    services = probe.get("services", []) if isinstance(probe, dict) else []
    for service in services or []:
        if not isinstance(service, dict):
            continue
        sname = str(service.get("service") or "unknown")
        title = "YouTube" if sname == "youtube" else ("Discord" if sname == "discord" else sname)
        lines.append(f"{title}: {'OK' if service.get('ok') else 'FAIL'} • score {service.get('score', 0)} • {service.get('level', '')}")
        req = service.get("required") if isinstance(service.get("required"), dict) else {}
        if req:
            lines.append("  required: " + ", ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in req.items()))
        for check in (service.get("checks") or [])[:8]:
            if isinstance(check, dict):
                lines.append(f"  - {_dpi_probe_check_label(check)}: {_dpi_probe_check_status(check)}")

    if not result.get("ok"):
        lines.append(f"Причина: {reason_text}")
        failures = _dpi_probe_failure_summary(best, max_items=10)
        if failures:
            lines.append("Проблемы лучшего варианта:")
            for item in failures:
                lines.append(f"  - {item}")
    if scores:
        ok_count = sum(1 for x in scores if isinstance(x, dict) and x.get("ok"))
        lines.append(f"Проверено вариантов: {len(scores)}; прошли обязательные checks: {ok_count}")
    return {
        "reason_text": reason_text,
        "lines": lines,
        "required": _dpi_required_status_from_score(best),
    }


def _dpi_write_ai_strategy_from_variant(variant: dict, score: dict, generation_domains: list[str]) -> tuple[bool, str, str]:
    """Сохраняет лучший AI-вариант как обычную Uz-стратегию."""
    try:
        import json
        m = _dpi_get_strategy_manager()
        existing_items = m.list_strategies(enabled_only=False)
        if len(existing_items) >= DPI_STRATEGY_LIMIT:
            return False, f"Достигнут лимит: максимум {DPI_STRATEGY_LIMIT} стратегий", ""
        # Ворота сохранения (последний рубеж): поломанная стратегия не пишется.
        from ai_strategy.validate import validate_strategy_args  # type: ignore
        vres = validate_strategy_args(
            list(variant.get("args", []) or []),
            bin_dir=m.strategies_dir.parent / "bin",
        )
        if not vres.get("ok"):
            return False, (
                "Отказ сохранения — стратегия структурно поломана: "
                + "; ".join(vres.get("problems") or [])
            ), ""
        n = _dpi_next_strategy_number(existing_items)
        if not n:
            return False, f"Нет свободных Uz-слотов: максимум Uz{DPI_STRATEGY_LIMIT}", ""
        sid = f"uz{n}"
        data = {
            "id": sid,
            "name": f"Uz{n}",
            "description": "AI-сгенерированная DPI-стратегия UmbraNet.",
            "enabled": True,
            "order": _dpi_next_order(m.strategies_dir, len(existing_items)),
            "created_at": _dpi_now_iso(),
            "generation": {
                "mode": "ai_generated",
                "version": 2,
                "variant_id": variant.get("id"),
                "seed_id": variant.get("seed_id"),
                "mutation": variant.get("mutation"),
                "mask_id": variant.get("mask_id"),
                "score": score,
                "service_scores": score.get("service_scores", {}) if isinstance(score, dict) else {},
                "required": _dpi_required_status_from_score(score if isinstance(score, dict) else {}),
                "generation_targets": ["youtube", "discord"],
                "generation_domains_count": len(generation_domains or []),
            },
            "args": list(variant.get("args", []) or []),
            "hostlist": [],
        }
        path = m.strategies_dir / f"{sid}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        post_event({"type": "config_changed", "section": "dpi_strategies"})
        return True, f"AI-стратегия создана: Uz{n}", sid
    except Exception as exc:
        return False, f"Ошибка сохранения AI-стратегии: {exc}", ""


def dpi_strategy_ai_cleanup_runtime() -> dict:
    """Жёстко останавливает временный runtime AI-генерации.

    AI-генерация запускает winws.exe напрямую для временных вариантов. После неё
    ничего не должно продолжать работать само по себе: ни WinWS, ни engine.
    """
    stopped = []
    errors = []
    try:
        from winws_engine import get_winws_engine  # type: ignore
        w = get_winws_engine()
        if w and w.is_running():
            w.stop()
            stopped.append("winws_engine")
        # Если раньше процесс winws.exe оторвался от Python-объекта, обычный
        # w.stop() его не увидит. Пользуемся единым DPI-cleanup из WinWSEngine:
        # он учитывает нашу папку bin/ и не трогает чужие winws.exe.
        # keep_running=False здесь осознанно: это жёсткая зачистка при выходе
        # из программы / по завершении сессии генерации, где убить нужно всё.
        try:
            if w and hasattr(w, "cleanup_orphans"):
                if w.cleanup_orphans(stop_driver=True, keep_running=False):
                    stopped.append("orphan_winws")
        except Exception as exc:
            errors.append(f"orphan_winws: {exc}")
    except Exception as exc:
        errors.append(f"winws_engine: {exc}")
    try:
        eng = get_engine()
        try:
            eng._manual_stop_requested = True
        except Exception as exc:
            log_recoverable(log, 'Не удалось установить флаг ручной остановки движка', exc, level=logging.WARNING)
        w2 = getattr(eng, "winws", None)
        if w2 and hasattr(w2, "is_running") and w2.is_running():
            w2.stop()
            stopped.append("engine.winws")
        if bool(getattr(eng, "running", False)):
            eng.stop()
            stopped.append("engine")
    except Exception as exc:
        errors.append(f"engine: {exc}")
    return {"stopped": stopped, "errors": errors}


def _dpi_local_port_conflicts(ports=(53,)) -> tuple[list[dict], str]:
    """Возвращает локальные TCP LISTEN и UDP bind-сокеты на проверяемых портах.

    Для генерации важен локальный DNS-порт 53. 443 проверять как «занятый
    локальный порт» нельзя: пробы открывают исходящие HTTPS-соединения, а не
    слушают этот порт. Ошибка обзора возвращается отдельно и не маскируется.
    """
    try:
        import socket
        import psutil

        wanted = {int(port) for port in ports}
        connections = psutil.net_connections(kind="inet")
    except Exception as exc:
        return [], str(exc)

    found: list[dict] = []
    seen: set[tuple] = set()
    for conn in connections:
        local = getattr(conn, "laddr", None)
        if not local:
            continue
        try:
            if hasattr(local, "port"):
                port = int(local.port)
                host = str(getattr(local, "ip", "") or "*")
            else:
                port = int(local[1])
                host = str(local[0] or "*")
        except (IndexError, TypeError, ValueError):
            continue
        if port not in wanted:
            continue

        sock_type = getattr(conn, "type", None)
        if sock_type == socket.SOCK_STREAM:
            if str(getattr(conn, "status", "")).upper() != str(psutil.CONN_LISTEN).upper():
                continue
            protocol = "TCP"
        elif sock_type == socket.SOCK_DGRAM:
            protocol = "UDP"
        else:
            continue

        pid = getattr(conn, "pid", None)
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        process_name = ""
        if pid is not None:
            try:
                process_name = str(psutil.Process(pid).name() or "")
            except Exception:
                pass

        key = (protocol, host, port, pid)
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "protocol": protocol,
            "host": host,
            "port": port,
            "pid": pid,
            "process_name": process_name,
        })

    found.sort(key=lambda item: (item["protocol"], item["host"], item["pid"] or 0))
    return found, ""


def _dpi_generation_preflight(winws, progress) -> dict:
    """Проверки ПЕРЕД подбором: чтобы не гонять 5 минут впустую.

    Почему это нужно. Подбор проверяет сайты (YouTube/Discord), то есть зависит
    от двух вещей: (1) работает ли вообще разрешение имён, (2) свободен ли
    WinDivert. Если системный DNS остался на 127.0.0.1, а локальный DNS-сервер
    остановлен (так и есть во время генерации), то ВСЕ проверки умирают на
    первом шаге: winws запускается, варианты «прогоняются», результат — нули,
    а время уходит на таймауты резолвера. Пользователь при этом видит «стратегия
    не найдена», хотя дело не в стратегии.

    Возвращает {"abort": bool, "reason": str, "warnings": [str]}.
    """
    result = {"abort": False, "reason": "", "warnings": []}

    def abort_preflight(reason: str) -> dict:
        result["abort"] = True
        result["reason"] = reason
        for note in result["warnings"]:
            try:
                progress(f"AI-генерация: внимание — {note}")
            except Exception as exc:
                log_recoverable(log, 'Не удалось передать предупреждение AI-генерации в UI', exc, level=logging.DEBUG)
        try:
            progress(f"AI-генерация: остановлена до запуска — {reason}")
        except Exception as exc:
            log_recoverable(log, 'Не удалось передать причину остановки AI-генерации в UI', exc, level=logging.DEBUG)
        return result

    # Проверяем настоящий локальный DNS-bind (TCP/UDP 53) и конкурирующий DPI.
    # WinError 10054 сам по себе не доказывает занятый порт: это сброс уже
    # установленного сетевого соединения. Zapret/WinWS может конфликтовать с
    # UmbraNet через WinDivert, поэтому его проверяем отдельно от портов.
    port_conflicts, port_scan_error = _dpi_local_port_conflicts((53,))
    if port_scan_error:
        result["warnings"].append(
            f"Не удалось проверить занятость локального TCP/UDP-порта 53: {port_scan_error}"
        )
    if port_conflicts:
        owners = []
        for item in port_conflicts:
            host = str(item.get("host") or "*")
            address = f"[{host}]:{item.get('port')}" if ":" in host else f"{host}:{item.get('port')}"
            process_name = str(item.get("process_name") or "")
            pid = item.get("pid")
            owner = process_name or (f"PID {pid}" if pid is not None else "процесс не определён")
            if process_name and pid is not None:
                owner += f" (PID {pid})"
            owners.append(f"{item.get('protocol', '?')} {address} — {owner}")
        reason = (
            "Перед генерацией обнаружена занятость локального DNS-порта 53: "
            + "; ".join(owners)
            + ". Генерация остановлена до запуска. Освободите порт вручную и повторите."
        )
        result["port_conflicts"] = port_conflicts
        return abort_preflight(reason)

    # Zapret обычно конкурирует не за TCP/UDP-порт, а за драйвер WinDivert.
    # На Windows он запускает winws.exe; проверяем и его, и другой e1-spike.exe.
    try:
        foreign = winws.foreign_processes() if hasattr(winws, "foreign_processes") else None
        if foreign:
            names = ", ".join(
                f"PID {pid} ({path or 'путь неизвестен'})" for pid, path in foreign[:5]
            )
            reason = (
                "Перед генерацией обнаружен сторонний DPI-процесс (winws.exe/e1-spike.exe): "
                f"{names}. Он может занимать WinDivert. Генерация остановлена до запуска. "
                "Закройте процесс вручную и повторите; автоматическое завершение пока не выполняется."
            )
            result["dpi_processes"] = foreign
            return abort_preflight(reason)
        if foreign is None:
            result["warnings"].append(
                "Не удалось проверить сторонние winws.exe/e1-spike.exe; проверка WinDivert неполная."
            )
    except Exception as exc:
        log.debug("preflight: обзор чужих DPI-процессов не удался: %s", exc)
        result["warnings"].append(f"Не удалось проверить сторонние DPI-процессы: {exc}")

    # Работает ли разрешение имён вообще. Это решающая проверка.
    try:
        from ai_strategy.probes import resolve_probe  # type: ignore
        dns = resolve_probe("www.youtube.com", timeout=3.0)
        if not dns.get("ok"):
            # Пытаемся спасти прогон: вернуть DNS пользователя из снапшота.
            restored = False
            try:
                ok, msg = network_restore_latest()
                restored = bool(ok)
                if ok:
                    log.info("preflight: DNS восстановлен перед генерацией: %s", msg)
            except Exception as exc:
                log.debug("preflight: восстановить DNS не удалось: %s", exc)
            dns2 = resolve_probe("www.youtube.com", timeout=3.0) if restored else dns
            if not dns2.get("ok"):
                # Сначала отдаём в интерфейс то, что уже успели заметить, — иначе
                # при отмене предупреждения (например о чужом winws.exe) пропадут.
                for note in result["warnings"]:
                    try:
                        progress(f"AI-генерация: внимание — {note}")
                    except Exception as exc:
                        log_recoverable(log, 'Не удалось передать предупреждение AI-генерации в UI', exc, level=logging.DEBUG)
                result["abort"] = True
                result["reason"] = (
                    "Нет разрешения имён: www.youtube.com не резолвится "
                    f"({(dns2 or {}).get('error') or 'таймаут'}). "
                    "Генерация проверяет сайты, без рабочего DNS все варианты получат 0 — "
                    "подбор отменён, чтобы не тратить время. "
                    "Проверьте: 1) системный DNS не должен указывать на 127.0.0.1 при остановленном "
                    "UmbraNet (кнопка «↩ Откатить сеть» на вкладке «Сеть и диагностика»); "
                    "2) рядом не должна работать другая DNS/DPI-программа."
                )
                try:
                    progress(f"AI-генерация: остановлена до старта — {result['reason']}")
                except Exception as exc:
                    log_recoverable(log, 'Не удалось передать причину отмены AI-генерации в UI', exc, level=logging.DEBUG)
                return result
            result["warnings"].append("DNS не отвечал: восстановили настройки из снапшота и продолжили")
    except Exception as exc:
        log.debug("preflight: проверка DNS не удалась: %s", exc)

    # 3. Кто-то слушает порт 53 — это чужая DNS-программа, вероятный конфликт.
    try:
        conflicts = _detect_dns_conflict_processes()
        if conflicts:
            names = ", ".join(str(c) for c in conflicts[:3])
            result["warnings"].append(f"порт 53 занят посторонним процессом: {names}")
    except Exception as exc:
        log.debug("preflight: проверка конфликтов порта 53 не удалась: %s", exc)

    for note in result["warnings"]:
        try:
            progress(f"AI-генерация: внимание — {note}")
        except Exception as exc:
            log_recoverable(log, 'Не удалось передать предупреждение AI-генерации в UI', exc, level=logging.DEBUG)
    return result


def _dpi_probe_all_dns_failed(probe: dict) -> bool:
    """True, если в проверке ВСЕ обращения провалились уже на разрешении имён.

    Признак того, что дело не в стратегии, а в сети: тогда останавливаемся
    сразу, а не досчитываем оставшиеся варианты в ноль.
    """
    services = (probe or {}).get("services") if isinstance(probe, dict) else None
    if not isinstance(services, list) or not services:
        return False
    total = 0
    dns_failed = 0
    for svc in services:
        checks = (svc or {}).get("checks") if isinstance(svc, dict) else None
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            total += 1
            if check.get("stage") == "dns" and not check.get("ok"):
                dns_failed += 1
    return bool(total) and dns_failed == total


def dpi_strategy_ai_run_controlled(mode: str = "quick", on_progress=None, should_cancel=None) -> dict:
    """Первый controlled runner AI-генерации.

    Предполагается, что основной UmbraNet уже остановлен UI-цепочкой. Runner
    напрямую запускает winws.exe для временных вариантов, проверяет basic probes
    YouTube/Discord, считает score и сохраняет только лучший вариант, если он
    достиг порога. Пользовательские процессы/режимы не перезапускаются.
    """
    def progress(text: str):
        try:
            if on_progress:
                on_progress(str(text))
        except Exception as exc:
            log_recoverable(log, 'Не удалось передать прогресс AI-генерации в UI', exc, level=logging.DEBUG)

    def cancelled() -> bool:
        try:
            return bool(should_cancel and should_cancel())
        except Exception:
            return False

    try:
        from ai_strategy.mutations import generate_variants  # type: ignore
        from ai_strategy.validate import validate_strategy_args  # type: ignore
        from ai_strategy.probes import run_basic_probes  # type: ignore
        from ai_strategy.scoring import choose_best, score_variant  # type: ignore
        from ai_strategy.session import session_policy  # type: ignore
        from ai_strategy.targets import analyze_generation_targets  # type: ignore
        from winws_engine import get_winws_engine  # type: ignore
        import time  # тайминги вариантов: видно, что именно съедает минуты

        m = _dpi_get_strategy_manager()
        if len(m.list_strategies(enabled_only=False)) >= DPI_STRATEGY_LIMIT:
            return {"ok": False, "stage": "ai_generation", "error": f"Достигнут лимит: максимум {DPI_STRATEGY_LIMIT} стратегий"}

        policy = session_policy(mode)
        max_variants = int(policy.get("max_variants", 12) or 12)
        min_score = int(policy.get("min_score_to_save", 70) or 70)
        per_timeout = float(min(6, max(2, int(policy.get("per_variant_timeout_sec", 35) or 35) // 7)))
        target_analysis = analyze_generation_targets(getattr(get_engine(), "config", {}) or {})
        generation_domains = list(target_analysis.get("generation_domains", []) or [])
        coverage = target_analysis.get("coverage") if isinstance(target_analysis.get("coverage"), dict) else {}
        cov_text = ", ".join(
            f"{(coverage.get(k) or {}).get('label', k)}={(coverage.get(k) or {}).get('domains_count', 0)}"
            for k in ("youtube", "discord") if isinstance(coverage.get(k), dict)
        )
        progress(
            f"AI-генерация: список истины {len(generation_domains)} доменов"
            + (f" ({cov_text})" if cov_text else "")
        )
        variants = generate_variants(mode=mode, max_variants=max_variants)
        winws = get_winws_engine()
        if not winws.is_available():
            return {"ok": False, "stage": "ai_generation", "error": f"winws.exe не найден: {winws.exe_path}"}
        if not variants:
            return {"ok": False, "stage": "ai_generation", "error": "нет временных вариантов для проверки"}

        # Предполётная диагностика: сеть и WinDivert должны быть в рабочем
        # состоянии ДО того, как мы потратим минуты на прогон вариантов.
        preflight = _dpi_generation_preflight(winws, progress)
        if preflight.get("abort"):
            # Причина уже отправлена в интерфейс из самой проверки.
            return {
                "ok": False,
                "stage": "ai_generation",
                "error": preflight.get("reason"),
                "reason_text": preflight.get("reason"),
                "preflight": preflight,
                "created_id": "",
                "scores": [],
                "target_analysis": target_analysis,
            }

        scores = []
        progress(f"AI-генерация: подготовлено вариантов: {len(variants)}")
        run_started = time.monotonic()
        for idx, variant in enumerate(variants, start=1):
            if cancelled():
                progress("AI-генерация: отменена пользователем")
                return {"ok": False, "stage": "ai_generation", "cancelled": True, "error": "генерация отменена", "created_id": "", "scores": scores}
            vid = str(variant.get("id", f"variant_{idx}"))
            seed_id = str(variant.get("seed_id", "seed"))
            mutation = str(variant.get("mutation", "base"))
            mask_id = str(variant.get("mask_id", "seed_default"))
            progress(
                f"AI-генерация: вариант {idx}/{len(variants)} • "
                f"seed={seed_id} • mutation={mutation} • mask={mask_id}"
            )
            # Структурные ворота (цель: только полностью рабочие стратегии).
            # Поломанный вариант не запускаем и не тратим на него probes.
            vres = validate_strategy_args(list(variant.get("args", []) or []))
            if not vres.get("ok"):
                scores.append({
                    "variant_id": vid,
                    "score": 0,
                    "ok": False,
                    "error": "структурная валидация: " + "; ".join(vres.get("problems") or []),
                })
                progress(f"AI-генерация: {vid} отклонён — {scores[-1]['error']}")
                continue
            args, err = _dpi_expand_ai_variant_args(list(variant.get("args", []) or []), generation_domains)
            if not args:
                scores.append({
                    "variant_id": vid,
                    "score": 0,
                    "ok": False,
                    "error": err or "args не готовы",
                })
                continue
            started = False
            try:
                if cancelled():
                    progress("AI-генерация: отменена пользователем")
                    return {"ok": False, "stage": "ai_generation", "cancelled": True, "error": "генерация отменена", "created_id": "", "scores": scores}
                progress(f"AI-генерация: запуск WinWS для {vid} ({len(args)} args)")
                t_start = time.monotonic()
                started = bool(winws.start(args))
                start_ms = round((time.monotonic() - t_start) * 1000)
                if started:
                    progress(f"AI-генерация: WinWS запущен для {vid} за {start_ms} мс")
                if cancelled():
                    try:
                        winws.stop(reset_connections=False)
                    except Exception as exc:
                        log_recoverable(log, 'Не удалось остановить WinWS после отмены генерации', exc, level=logging.ERROR)
                    progress("AI-генерация: отменена пользователем")
                    return {"ok": False, "stage": "ai_generation", "cancelled": True, "error": "генерация отменена", "created_id": "", "scores": scores}
                if not started:
                    scores.append({
                        "variant_id": vid,
                        "score": 0,
                        "ok": False,
                        "error": winws.last_error or "WinWS не запустился",
                    })
                    continue
                progress(f"AI-генерация: probes YouTube/Discord для {vid}")
                t_probe = time.monotonic()
                probe = run_basic_probes(timeout=per_timeout)
                probe_ms = round((time.monotonic() - t_probe) * 1000)
                if cancelled():
                    progress("AI-генерация: отменена пользователем")
                    return {"ok": False, "stage": "ai_generation", "cancelled": True, "error": "генерация отменена", "created_id": "", "scores": scores}
                scored = score_variant(variant, probe)
                scored["probe"] = probe
                scored["failure_summary"] = _dpi_probe_failure_summary(scored, max_items=6)
                scores.append(scored)
                svc = scored.get("service_scores", {}) if isinstance(scored.get("service_scores"), dict) else {}
                svc_text = ", ".join(f"{k}={v}" for k, v in svc.items()) or "services=—"
                scored["timings"] = {"start_ms": start_ms, "probe_ms": probe_ms}
                hint = ""
                if not int(scored.get("score", 0) or 0):
                    # Нулевой результат — самое время показать настоящую причину
                    # из лога winws.exe, а не оставлять пользователя с догадками.
                    try:
                        hint = winws.error_hint() if hasattr(winws, "error_hint") else ""
                    except Exception:
                        hint = ""
                    if hint:
                        scored["winws_error_hint"] = hint
                progress(
                    f"AI-генерация: вариант {idx}/{len(variants)} score {scored.get('score', 0)} "
                    f"raw {scored.get('raw_score', 0)} • {svc_text} • "
                    f"запуск {start_ms} мс • проверка {probe_ms} мс • всего "
                    f"{round(time.monotonic() - run_started)} с"
                )
                if hint:
                    progress(f"AI-генерация: winws.exe сообщает: {hint}")
                # Если ВСЕ обращения провалились на разрешении имён — дело не в
                # стратегии, а в сети. Не досчитываем оставшиеся варианты в ноль.
                if _dpi_probe_all_dns_failed(probe):
                    dns_abort = (
                        "Проверки не доходят до сайтов: не разрешаются имена. "
                        "Это значит, что системный DNS не работает (например остался на 127.0.0.1 "
                        "при остановленном UmbraNet). Прогон остановлен, чтобы не тратить время: "
                        "верните DNS кнопкой «↩ Откатить сеть» и запустите генерацию снова."
                    )
                    progress(f"AI-генерация: остановлена на варианте {idx}/{len(variants)} — {dns_abort}")
                    result = {
                        "ok": False,
                        "stage": "ai_generation",
                        "error": dns_abort,
                        "reason_text": dns_abort,
                        "dns_failed": True,
                        "preflight": preflight,
                        "best": {},
                        "scores": scores,
                        "min_score": min_score,
                        "created_id": "",
                        "target_analysis": target_analysis,
                    }
                    report = _dpi_build_generation_report(result, target_analysis)
                    result["report"] = report
                    result["report_lines"] = report.get("lines", [])
                    result["reason_text"] = result.get("reason_text") or report.get("reason_text", "")
                    return result
            except Exception as exc:
                scores.append({"variant_id": vid, "score": 0, "ok": False, "error": str(exc)})
            finally:
                try:
                    if started or winws.is_running():
                        if not winws.stop(reset_connections=False):
                            progress(f"AI-генерация: WinWS не остановился для {vid}")
                    # ВАЖНО: зачистка обязана идти ПОСЛЕ КАЖДОГО варианта, а не
                    # только когда stop() вернул False. Раньше здесь стоял
                    # PowerShell по маске пути — его убрали из горячего цикла,
                    # и вместе с ним ушла гарантия чистого запута: зависший
                    # winws.exe (с потерянным Popen-объектом) оставался держать
                    # WinDivert, и все следующие варианты падали на старте.
                    # Теперь зачистка дешёвая: обзор через WinAPI + завершение по
                    # PID, без запуска PowerShell, когда процессов нет.
                    if hasattr(winws, "cleanup_orphans"):
                        sweep = winws.cleanup_orphans(stop_driver=False)
                        if sweep:
                            progress(f"AI-генерация: зачищены зависшие winws для {vid}")
                except Exception as exc:
                    progress(f"AI-генерация: предупреждение cleanup для {vid}: {exc}")

        choice = choose_best(scores, min_score=min_score)
        best = choice.get("best") or {}
        if not choice.get("ok"):
            progress("AI-генерация: рабочий вариант не найден")
            result = {
                "ok": False,
                "stage": "ai_generation",
                "reason": choice.get("reason"),
                "min_score": min_score,
                "best": best,
                "scores": scores,
                "created_id": "",
                "target_analysis": target_analysis,
            }
            report = _dpi_build_generation_report(result, target_analysis)
            result["report"] = report
            result["report_lines"] = report.get("lines", [])
            result["reason_text"] = report.get("reason_text", "")
            return result
        best_id = str(best.get("variant_id", ""))
        best_variant = next((v for v in variants if v.get("id") == best_id), None)
        if not best_variant:
            result = {"ok": False, "stage": "ai_generation", "error": "лучший вариант потерян", "best": best, "scores": scores, "min_score": min_score, "target_analysis": target_analysis}
            report = _dpi_build_generation_report(result, target_analysis)
            result["report"] = report
            result["report_lines"] = report.get("lines", [])
            result["reason_text"] = report.get("reason_text", "")
            return result
        ok, msg, sid = _dpi_write_ai_strategy_from_variant(best_variant, best, generation_domains)
        progress(msg if ok else f"AI-генерация: {msg}")
        result = {
            "ok": bool(ok),
            "stage": "ai_generation",
            "message": msg,
            "created_id": sid,
            "best": best,
            "scores": scores,
            "min_score": min_score,
            "target_analysis": target_analysis,
        }
        if not ok:
            result["error"] = msg
        report = _dpi_build_generation_report(result, target_analysis)
        result["report"] = report
        result["report_lines"] = report.get("lines", [])
        result["reason_text"] = report.get("reason_text", "")
        return result
    except Exception as exc:
        log.warning("dpi_strategy_ai_run_controlled error: %s", exc)
        return {"ok": False, "stage": "ai_generation", "error": str(exc), "created_id": ""}
    finally:
        # Гарантия: после генерации временный WinWS не должен остаться жить.
        dpi_strategy_ai_cleanup_runtime()


def dpi_strategy_check_all_controlled(on_progress=None, should_cancel=None) -> dict:
    """Последовательно проверяет все Uz-стратегии на списке истины.

    Ничего не сохраняет и не меняет активную стратегию. Для каждой стратегии:
      start winws -> probes YouTube/Discord -> score -> stop/cleanup.
    """
    def progress(text: str):
        try:
            if on_progress:
                on_progress(str(text))
        except Exception as exc:
            log_recoverable(log, 'Не удалось передать прогресс проверки стратегии в UI', exc, level=logging.DEBUG)

    def cancelled() -> bool:
        try:
            return bool(should_cancel and should_cancel())
        except Exception:
            return False

    try:
        from ai_strategy.probes import run_basic_probes  # type: ignore
        from ai_strategy.scoring import score_variant  # type: ignore
        from ai_strategy.targets import analyze_generation_targets  # type: ignore
        from winws_engine import get_winws_engine  # type: ignore

        manager = _dpi_get_strategy_manager()
        items = dpi_strategy_items()
        target_analysis = analyze_generation_targets(getattr(get_engine(), "config", {}) or {})
        generation_domains = list(target_analysis.get("generation_domains", []) or [])
        winws = get_winws_engine()
        if not winws.is_available():
            return {"ok": False, "stage": "strategy_check", "error": f"winws.exe не найден: {winws.exe_path}", "results": []}
        if not items:
            return {"ok": False, "stage": "strategy_check", "error": "нет стратегий для проверки", "results": []}

        progress(f"Проверка Uz: список истины {len(generation_domains)} доменов")
        results = []
        total = len(items)
        per_timeout = 5.0
        for idx, item in enumerate(items, start=1):
            if cancelled():
                progress("Проверка Uz: отменена пользователем")
                return {"ok": False, "stage": "strategy_check", "cancelled": True, "error": "проверка отменена", "results": results}
            sid = str(item.get("id", f"uz{idx}"))
            progress(f"Проверка Uz: стратегия {idx}/{total} • {sid}")
            strategy = manager.get_strategy(sid)
            raw_args = list((strategy or {}).get("args", []) or [])
            args, err = _dpi_expand_ai_variant_args(raw_args, generation_domains)
            if not args:
                results.append({"strategy_id": sid, "ok": False, "score": 0, "error": err or "args не готовы"})
                progress(f"Проверка Uz: {sid} пропущена — {err or 'args не готовы'}")
                continue
            started = False
            try:
                progress(f"Проверка Uz: запуск WinWS для {sid} ({len(args)} args)")
                started = bool(winws.start(args))
                if not started:
                    results.append({"strategy_id": sid, "ok": False, "score": 0, "error": winws.last_error or "WinWS не запустился"})
                    continue
                probe = run_basic_probes(timeout=per_timeout)
                pseudo_variant = {
                    "id": sid,
                    "seed_id": "existing_strategy",
                    "mutation": "check_all",
                    "mask_id": "strategy_json",
                    "risk": "medium",
                    "save_priority": 50,
                }
                scored = score_variant(pseudo_variant, probe)
                scored["strategy_id"] = sid
                scored["name"] = item.get("name", sid)
                scored["probe"] = probe
                scored["failure_summary"] = _dpi_probe_failure_summary(scored, max_items=6)
                results.append(scored)
                svc = scored.get("service_scores", {}) if isinstance(scored.get("service_scores"), dict) else {}
                svc_text = ", ".join(f"{k}={v}" for k, v in svc.items()) or "services=—"
                progress(f"Проверка Uz: {sid} score {scored.get('score', 0)} • {svc_text}")
            except Exception as exc:
                results.append({"strategy_id": sid, "ok": False, "score": 0, "error": str(exc)})
            finally:
                try:
                    if started or winws.is_running():
                        if not winws.stop(reset_connections=False):
                            progress(f"Проверка Uz: WinWS не остановился для {sid}")
                    # См. комментарий в AI-генерации: зачистка после КАЖДОЙ
                    # стратегии. Она дешёвая (WinAPI-обзор, без PowerShell), но
                    # именно она не даёт зависшему winws.exe сломать следующий
                    # запуск — без неё проверка стратегий сыпалась подряд.
                    if hasattr(winws, "cleanup_orphans"):
                        if winws.cleanup_orphans(stop_driver=False):
                            progress(f"Проверка Uz: зачищены зависшие winws для {sid}")
                except Exception as exc:
                    progress(f"Проверка Uz: cleanup warning для {sid}: {exc}")

        ordered = sorted(results, key=lambda x: int(x.get("score", 0) or 0), reverse=True)
        best = ordered[0] if ordered else {}
        report_lines = []
        report_lines.append(f"Проверено стратегий: {len(results)} / {total}")
        if best:
            report_lines.append(f"Лучший результат: {best.get('strategy_id', '—')} • score {best.get('score', 0)}")
        for r in ordered:
            svc = r.get("service_scores") if isinstance(r.get("service_scores"), dict) else {}
            report_lines.append(
                f"{r.get('strategy_id', '—')}: score {r.get('score', 0)} • "
                f"Discord {svc.get('discord', '—')} • YouTube {svc.get('youtube', '—')} • "
                f"{'OK' if r.get('ok') else 'FAIL'}"
            )
            failures = r.get("failure_summary") if isinstance(r.get("failure_summary"), list) else []
            if failures:
                report_lines.append("  проблемы:")
                for item in failures[:6]:
                    report_lines.append(f"    - {item}")
            if r.get("error"):
                report_lines.append(f"  ошибка: {r.get('error')}")
        return {
            "ok": True,
            "stage": "strategy_check",
            "results": results,
            "best": best,
            "report_lines": report_lines,
            "target_analysis": target_analysis,
        }
    except Exception as exc:
        log.warning("dpi_strategy_check_all_controlled error: %s", exc)
        return {"ok": False, "stage": "strategy_check", "error": str(exc), "results": []}
    finally:
        dpi_strategy_ai_cleanup_runtime()


# ── DPI Strategy Lab helpers ────────────────────────────────────────────────
DPI_STRATEGY_LIMIT = 30


def _dpi_get_strategy_manager():
    """StrategyManager, привязанный к реальной папке приложения UmbraNet/strategies."""
    from strategy_manager import StrategyManager  # type: ignore
    return StrategyManager(get_strategies_dir())


def _dpi_strategy_num(strategy_id: str) -> int:
    """Номер из id вида uz12. Для нестандартных id уводим в конец."""
    try:
        import re
        m = re.fullmatch(r"uz(\d+)", str(strategy_id or "").strip().lower())
        return int(m.group(1)) if m else 10_000
    except Exception:
        return 10_000


def _dpi_next_strategy_number(existing_items: list[dict]) -> int:
    """Первый свободный UzN в пределах лимита. 0 если свободного слота нет."""
    existing = {str(x.get("id", "")).lower() for x in existing_items}
    for candidate in range(1, DPI_STRATEGY_LIMIT + 1):
        if f"uz{candidate}" not in existing:
            return candidate
    return 0


def _dpi_next_order(strategies_dir, existing_count: int) -> int:
    """Следующий order, чтобы новые/скопированные стратегии вставали в конец."""
    try:
        import json
        max_order = int(existing_count or 0)
        for path in strategies_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                order = raw.get("order")
                if isinstance(order, (int, float)):
                    max_order = max(max_order, int(order))
            except Exception as exc:
                log_recoverable(log, 'Не удалось прочитать порядок стратегии; используется резервный', exc, level=logging.DEBUG)
        return max_order + 1
    except Exception:
        return int(existing_count or 0) + 1


def _dpi_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _dpi_strategy_path(manager, sid: str):
    """Путь к JSON стратегии по id, с fallback для нестандартного регистра/id."""
    import json
    direct = manager.strategies_dir / f"{sid}.json"
    if direct.exists():
        return direct
    for path in manager.strategies_dir.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if str(raw.get("id", "")).strip().lower() == sid:
                return path
        except Exception as exc:
            log_recoverable(log, 'Пропущен нечитаемый файл стратегии при поиске', exc, level=logging.DEBUG)
    return direct


def dpi_strategy_items() -> list[dict]:
    """Список JSON-стратегий из папки strategies для вкладки Strategy Lab.

    Старые стратегии без поля order показываются натурально: uz1, uz2, ..., uz10.
    Новые стратегии, созданные через кнопку «Сгенерировать Uz», получают order
    и поэтому добавляются в конец списка, даже если заняли освободившийся id.
    """
    try:
        import json
        m = _dpi_get_strategy_manager()
        items = []
        for it in m.list_strategies(enabled_only=False):
            sid = str(it.get("id", ""))
            path = _dpi_strategy_path(m, sid.lower())
            args = it.get("args") or []
            order = None
            created_at = ""
            generation = {}
            try:
                raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                order = raw.get("order")
                created_at = str(raw.get("created_at", ""))
                generation = raw.get("generation") if isinstance(raw.get("generation"), dict) else {}
            except Exception as exc:
                log_recoverable(log, 'Не удалось прочитать метаданные стратегии', exc, level=logging.WARNING)
            score_obj = generation.get("score") if isinstance(generation.get("score"), dict) else {}
            service_scores = generation.get("service_scores") if isinstance(generation.get("service_scores"), dict) else {}
            if not service_scores and isinstance(score_obj.get("service_scores"), dict):
                service_scores = score_obj.get("service_scores")
            required = generation.get("required") if isinstance(generation.get("required"), dict) else {}
            items.append({
                "id": sid,
                "name": it.get("name", sid),
                "description": it.get("description", ""),
                "args_count": len(args),
                "enabled": True,
                "path": str(path),
                "active": sid.lower() == str((getattr(get_engine(), "config", {}) or {}).get("dpi_strategy", "uz1")).lower(),
                "order": order,
                "created_at": created_at,
                "generation": generation,
                "ai_generated": bool(generation),
                "score": score_obj.get("score", generation.get("score", "")) if isinstance(score_obj, dict) else generation.get("score", ""),
                "raw_score": score_obj.get("raw_score", "") if isinstance(score_obj, dict) else "",
                "service_scores": service_scores,
                "required": required,
                "seed_id": generation.get("seed_id", ""),
                "mutation": generation.get("mutation", ""),
                "mask_id": generation.get("mask_id", ""),
            })

        def _sort_key(item: dict):
            sid = str(item.get("id", ""))
            order = item.get("order")
            if isinstance(order, (int, float)):
                return (1, int(order), _dpi_strategy_num(sid), sid.lower())
            return (0, _dpi_strategy_num(sid), sid.lower())

        return sorted(items, key=_sort_key)
    except Exception as exc:
        log.warning("dpi_strategy_items error: %s", exc)
        return []


def dpi_strategy_create_next() -> tuple[bool, str, str]:
    """Создаёт новую пустую стратегию UzN. Возвращает (ok, msg, id).

    Сейчас создаётся пустой слот. Позже в него можно будет дописать генерацию
    внутренностей стратегии, но порядок и лимит уже готовы.
    """
    try:
        import json
        m = _dpi_get_strategy_manager()
        existing_items = m.list_strategies(enabled_only=False)
        if len(existing_items) >= DPI_STRATEGY_LIMIT:
            return False, f"Достигнут лимит: максимум {DPI_STRATEGY_LIMIT} стратегий", ""

        n = _dpi_next_strategy_number(existing_items)
        if not n:
            return False, f"Нет свободных Uz-слотов: максимум Uz{DPI_STRATEGY_LIMIT}", ""

        sid = f"uz{n}"
        data = {
            "id": sid,
            "name": f"Uz{n}",
            "description": "Пустая DPI-стратегия. Цели берутся из главного меню UmbraNet.",
            "enabled": True,
            "order": _dpi_next_order(m.strategies_dir, len(existing_items)),
            "created_at": _dpi_now_iso(),
            "generation": {
                "mode": "empty",
                "target": "custom",
                "aggression": "none",
                "version": 1,
            },
            "args": [],
            "hostlist": [],
        }
        path = m.strategies_dir / f"{sid}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        post_event({"type": "config_changed", "section": "dpi_strategies"})
        return True, f"Создана стратегия {data['name']}", sid
    except Exception as exc:
        return False, f"Ошибка создания стратегии: {exc}", ""


def dpi_strategy_duplicate(strategy_id: str) -> tuple[bool, str, str]:
    """Создаёт копию выбранной стратегии. Возвращает (ok, msg, new_id)."""
    try:
        import copy
        import json
        sid = str(strategy_id or "").strip().lower()
        if not sid:
            return False, "Стратегия не выбрана", ""

        m = _dpi_get_strategy_manager()
        existing_items = m.list_strategies(enabled_only=False)
        if len(existing_items) >= DPI_STRATEGY_LIMIT:
            return False, f"Достигнут лимит: максимум {DPI_STRATEGY_LIMIT} стратегий", ""

        src_path = _dpi_strategy_path(m, sid)
        if not src_path.exists():
            return False, f"Файл стратегии не найден: {sid}", ""
        src = json.loads(src_path.read_text(encoding="utf-8"))
        if not isinstance(src, dict):
            return False, f"Некорректный JSON стратегии: {sid}", ""

        n = _dpi_next_strategy_number(existing_items)
        if not n:
            return False, f"Нет свободных Uz-слотов: максимум Uz{DPI_STRATEGY_LIMIT}", ""

        new_id = f"uz{n}"
        new_data = copy.deepcopy(src)
        old_desc = str(src.get("description", "")).strip()
        source_name = str(src.get("name") or sid).strip() or sid
        prefix = f"Копия стратегии {source_name}"
        new_desc = f"{prefix}: {old_desc}" if old_desc else f"{prefix}."

        source_generation = src.get("generation")
        generation = {
            "mode": "copy",
            "source": sid,
            "version": 1,
        }
        if isinstance(source_generation, dict):
            generation["source_generation"] = source_generation

        new_data.update({
            "id": new_id,
            "name": f"Uz{n}",
            "description": new_desc,
            "enabled": bool(src.get("enabled", True)),
            "order": _dpi_next_order(m.strategies_dir, len(existing_items)),
            "created_at": _dpi_now_iso(),
            "generation": generation,
        })
        # Старые/ручные служебные поля не должны притворяться актуальными.
        new_data.pop("updated_at", None)

        dst_path = m.strategies_dir / f"{new_id}.json"
        dst_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        post_event({"type": "config_changed", "section": "dpi_strategies"})
        return True, f"Создана копия {source_name} → Uz{n}", new_id
    except Exception as exc:
        return False, f"Ошибка копирования стратегии: {exc}", ""


def dpi_strategy_delete(strategy_id: str) -> tuple[bool, str]:
    """Удаляет стратегию из папки strategies. Если удалили активную — выбирает первую доступную."""
    try:
        sid = str(strategy_id or "").strip().lower()
        if not sid:
            return False, "Стратегия не выбрана"
        m = _dpi_get_strategy_manager()
        path = _dpi_strategy_path(m, sid)
        if not path.exists():
            return False, f"Файл стратегии не найден: {sid}"
        path.unlink()

        eng = get_engine()
        cfg = getattr(eng, "config", {}) or {}
        if cfg.get("dpi_strategy") == sid:
            rest = m.list_strategies(enabled_only=True)
            if rest:
                cfg["dpi_strategy"] = rest[0]["id"]
            else:
                ok, _msg, new_id = dpi_strategy_create_next()
                cfg["dpi_strategy"] = new_id if ok else "uz1"
            try:
                save_config(cfg)
                eng.config = cfg
            except Exception as exc:
                log_recoverable(log, 'Не удалось сохранить выбор стратегии после удаления', exc, level=logging.ERROR)
        post_event({"type": "config_changed", "section": "dpi_strategies"})
        return True, f"Стратегия {sid} удалена"
    except Exception as exc:
        return False, f"Ошибка удаления стратегии: {exc}"


def dpi_strategy_set_active(strategy_id: str) -> tuple[bool, str]:
    """Делает стратегию активной в config.json."""
    try:
        sid = str(strategy_id or "").strip().lower()
        if not sid:
            return False, "Стратегия не выбрана"
        m = _dpi_get_strategy_manager()
        if not m.get_strategy(sid):
            return False, f"Стратегия не найдена: {sid}"
        eng = get_engine()
        eng.config["dpi_strategy"] = sid
        save_config(eng.config)
        post_event({"type": "config_changed", "section": "dpi_strategy", "id": sid})
        return True, f"Активна стратегия {sid}"
    except Exception as exc:
        return False, f"Ошибка выбора стратегии: {exc}"


def dpi_strategy_update_meta(strategy_id: str, name: str | None = None,
                             description: str | None = None) -> tuple[bool, str]:
    """Меняет имя/описание стратегии в JSON-файле."""
    try:
        import json
        sid = str(strategy_id or "").strip().lower()
        if not sid:
            return False, "Стратегия не выбрана"
        m = _dpi_get_strategy_manager()
        path = _dpi_strategy_path(m, sid)
        if not path.exists():
            return False, f"Файл стратегии не найден: {sid}"
        data = json.loads(path.read_text(encoding="utf-8"))
        if name is not None:
            clean = str(name).strip()
            if not clean:
                return False, "Имя не может быть пустым"
            data["name"] = clean
        if description is not None:
            data["description"] = str(description).strip()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        post_event({"type": "config_changed", "section": "dpi_strategies"})
        return True, "Стратегия обновлена"
    except Exception as exc:
        return False, f"Ошибка обновления стратегии: {exc}"
