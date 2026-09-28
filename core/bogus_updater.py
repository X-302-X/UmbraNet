"""
UmbraNet — фоновое обновление списка bogus-IP провайдеров РФ.

Проблема, которую решаем:
  Список bogus-IP в bogus_ips.py статичен и устаревает — провайдеры
  периодически меняют адреса своих страниц «Доступ ограничен».
  Через 3-6 месяцев часть заглушек перестаёт детектироваться.

Решение:
  Фоновый поток BogusUpdater раз в 24 часа скачивает актуальный JSON
  с GitHub (из самого репозитория UmbraNet). Скачанный список:
    1. Валидируется (битые строки дропаются, не ломая работу)
    2. Мержится со встроенным builtin-списком (builtin — всегда база)
    3. Сохраняется на диск рядом с config.json (кэш для офлайн-запуска)
    4. Применяется горячо — без перезапуска DNS-сервера

При офлайн-запуске / недоступности URL:
  - Используется последний сохранённый диск-кэш
  - Если кэша нет — только builtin (поведение как раньше)
  - Ошибки сети логируются на DEBUG-уровне, не падают на пользователя

Архитектурные решения:
  - Один фоновый daemon-поток — не блокирует запуск и работу DNS
  - Обновление атомарно: новый индекс применяется целиком или не применяется
  - Callback on_update(ips, subnets) вызывается в потоке обновления;
    UmbraNetResolver сбрасывает _bogus_cache — следующий запрос подхватит новое
  - Нет внешних зависимостей кроме urllib (stdlib)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from core.diagnostics import log_recoverable
from urllib.parse import urlsplit

log = logging.getLogger("UmbraNet.BogusUpdater")

# ── Настройки ─────────────────────────────────────────────────────────────────

# URL удалённого списка: raw-файл прямо из репозитория UmbraNet на GitHub.
# Меняем только этот URL, если репо переедет.
REMOTE_URL = (
    "https://raw.githubusercontent.com/X-302-X/UmbraNet/main/bogus_ips_remote.json"
)

# Локальный бандл: файл bogus_ips_remote.json поставляется вместе с программой
# и лежит в корне репо (два уровня выше core/). Это первичный источник —
# работает без сети, всегда актуален на момент установки.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_BUNDLED_FILE = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "bogus_ips_remote.json")
)

# Имя диск-кэша рядом с config.json
CACHE_FILENAME = "bogus_ips_cache.json"

# Интервал фонового обновления (секунды). 86400 = 24 часа.
UPDATE_INTERVAL = 86_400

# Таймаут HTTP-запроса
HTTP_TIMEOUT = 10.0

# Минимальный интервал между попытками при ошибке сети (секунды)
RETRY_INTERVAL = 3_600  # 1 час


# ── Валидация входных данных ──────────────────────────────────────────────────

def _validate_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Парсит строку как IP-адрес. Возвращает объект или None при ошибке."""
    try:
        return ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return None


def _validate_subnet(raw: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """Парсит строку как CIDR-подсеть. Возвращает объект или None при ошибке."""
    try:
        return ipaddress.ip_network(str(raw).strip(), strict=False)
    except ValueError:
        return None


def _parse_remote_json(data: bytes, *, strict: bool = False) -> tuple[list[str], list[str]]:
    """
    Разбирает JSON из удалённого источника.
    Возвращает (ips: list[str], subnets: list[str]) — только валидные значения.
    Невалидные строки молча дропаются — не ломаем работу из-за одного битого IP.

    strict=True — включает проверку на «слишком маленький» список (защита от
    битого/куцего JSON с 1 IP, который иначе перезапишет хороший кэш). При
    strict бросает ValueError, если суммарно валидных записей слишком мало.
    """
    # H1 guard: не грузим безлимитно огромный JSON (защита от гигантских файлов)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError(f"Слишком большой bogus JSON: {len(data)} байт (>2 MB)")

    try:
        obj = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Невалидный JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise ValueError("Ожидается JSON-объект верхнего уровня")

    raw_ips = obj.get("bogus_ips", [])
    raw_subnets = obj.get("bogus_subnets", [])

    if not isinstance(raw_ips, list) or not isinstance(raw_subnets, list):
        raise ValueError("bogus_ips и bogus_subnets должны быть массивами")

    valid_ips: list[str] = []
    for raw in raw_ips:
        ip = _validate_ip(str(raw))
        if ip is not None:
            valid_ips.append(str(ip))
        else:
            log.debug("Дропнут невалидный IP из remote: %r", raw)

    valid_subnets: list[str] = []
    for raw in raw_subnets:
        net = _validate_subnet(str(raw))
        if net is not None:
            valid_subnets.append(str(net))
        else:
            log.debug("Дропнута невалидная подсеть из remote: %r", raw)

    if strict:
        # H1: требуем минимум полезного контента — иначе не перезаписываем хороший кэш
        total = len(valid_ips) + len(valid_subnets)
        if total < 10:
            raise ValueError(f"Слишком мало bogus записей: {total} (ожидается ≥10)")
        if len(valid_ips) < 5 and len(valid_subnets) < 2:
            raise ValueError(f"Подозрительно мало IP ({len(valid_ips)}) и подсетей ({len(valid_subnets)})")

    return valid_ips, valid_subnets


# ── Диск-кэш ─────────────────────────────────────────────────────────────────

def _cache_path(config_dir: str) -> str:
    return os.path.join(config_dir, CACHE_FILENAME)


def _save_cache(config_dir: str, ips: list[str], subnets: list[str]) -> bool:
    """Сохраняет актуальный список на диск для офлайн-запуска (атомарно + бэкап)."""
    path = _cache_path(config_dir)
    try:
        payload = {
            "_updated_at": time.time(),
            "bogus_ips": ips,
            "bogus_subnets": subnets,
        }
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            # H1: бэкап предыдущего кэша перед атомарной заменой
            if os.path.exists(path):
                try:
                    bak = f"{path}.bak"
                    # копия, не move — сохраняем предыдущий кэш на случай битья нового
                    import shutil
                    shutil.copy2(path, bak)
                except Exception as exc:
                    log.debug("Не удалось создать bak кэша: %s", exc)
            os.replace(tmp, path)  # атомарная замена файла
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        log.debug("Диск-кэш bogus-IP сохранён: %d IP, %d подсетей", len(ips), len(subnets))
        return True
    except Exception as exc:
        log.warning("Не удалось сохранить кэш bogus-IP: %s", exc)
        return False


def load_cached(config_dir: str) -> tuple[list[str], list[str]]:
    """
    Загружает диск-кэш bogus-IP.
    Возвращает (ips, subnets) или ([], []) если кэша нет или он битый.
    """
    path = _cache_path(config_dir)
    if not os.path.exists(path):
        return [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        ips = [str(x) for x in obj.get("bogus_ips", []) if _validate_ip(str(x))]
        subnets = [str(x) for x in obj.get("bogus_subnets", []) if _validate_subnet(str(x))]
        log.debug(
            "Диск-кэш bogus-IP загружен: %d IP, %d подсетей (возраст: %.0f ч)",
            len(ips), len(subnets),
            (time.time() - float(obj.get("_updated_at", 0))) / 3600,
        )
        return ips, subnets
    except Exception as exc:
        log.warning("Не удалось прочитать кэш bogus-IP (%s): %s", path, exc)
        return [], []


# ── Локальный бандл ──────────────────────────────────────────────────────────

def _read_local_bundled(path: str = LOCAL_BUNDLED_FILE) -> bytes:
    """
    Читает локальный файл bogus_ips_remote.json, поставляемый вместе с программой.
    Бросает FileNotFoundError если файл не найден, OSError при ошибке чтения.
    """
    with open(path, "rb") as f:
        return f.read()


def _load_from_local(path: str = LOCAL_BUNDLED_FILE) -> tuple[list[str], list[str]] | None:
    """
    Загружает список из локального бандла.
    path — путь к файлу (по умолчанию LOCAL_BUNDLED_FILE из корня программы).
    Возвращает (ips, subnets) при успехе или None при любой ошибке.
    """
    try:
        data = _read_local_bundled(path)
        ips, subnets = _parse_remote_json(data)
        if ips or subnets:
            log.debug(
                "BogusUpdater: загружен локальный бандл (%d IP, %d подсетей)",
                len(ips), len(subnets),
            )
            return ips, subnets
        return None
    except FileNotFoundError:
        log.debug("BogusUpdater: локальный бандл не найден (%s)", path)
        return None
    except Exception as exc:
        log.debug("BogusUpdater: ошибка чтения локального бандла: %s", exc)
        return None


# ── HTTP-загрузчик ────────────────────────────────────────────────────────────

def _validate_remote_url(url: str) -> str:
    """Разрешает только абсолютные HTTP(S)-URL для сетевого обновления."""
    value = str(url or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"Некорректный URL обновления: {value!r}") from exc
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Небезопасная схема URL обновления: {value!r}")
    return value


def _fetch_remote(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    """
    Скачивает JSON по URL. Использует только urllib (stdlib).
    Бросает urllib.error.URLError или OSError при ошибке сети/таймауте.
    """
    safe_url = _validate_remote_url(url)
    req = urllib.request.Request(
        safe_url,
        headers={"User-Agent": "UmbraNet/1.0 bogus-ip-updater"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - scheme validated above
        return resp.read(2 * 1024 * 1024 + 1)


# ── Основной класс ────────────────────────────────────────────────────────────

@dataclass
class PeriodicTask:
    name: str
    callback: Callable[[], bool]
    next_due: float = 0.0


class BogusUpdater:
    """
    Фоновый обновлятель bogus-IP и независимых периодических задач.

    Использование:
        updater = BogusUpdater(
            config_dir=os.path.dirname(config_json_path),
            on_update=resolver._invalidate_bogus_cache,
        )
        updater.start()   # запускаем при старте DNS-сервера
        updater.stop()    # останавливаем при остановке

    Параметры:
        config_dir  — директория рядом с config.json (туда пишем кэш)
        on_update   — callback(ips: list[str], subnets: list[str]),
                      вызывается когда получен новый актуальный список.
                      Может быть None — тогда обновление только кэшируется.
        url         — URL удалённого JSON (по умолчанию REMOTE_URL)
        interval    — интервал обновления в секундах (по умолчанию 24 часа)
    """

    def __init__(
        self,
        config_dir: str,
        on_update: Callable[[list[str], list[str]], None] | None = None,
        url: str = REMOTE_URL,
        interval: float = UPDATE_INTERVAL,
    ):
        self.config_dir = config_dir
        self.on_update = on_update
        self.url = url
        self.interval = interval

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_success: float = 0.0
        self._lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._restart_requested = False
        self._tasks = [PeriodicTask("bogus-IP", self._try_update)]

    def start(self) -> None:
        """Запускает фоновый поток обновления."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                # stop() may still be waiting for a bounded network request.
                # Restart once that worker exits, never create two schedulers.
                if self._stop_event.is_set():
                    self._restart_requested = True
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="UmbraNet-BogusUpdater",
                daemon=True,
            )
            self._thread.start()
            log.info("BogusUpdater запущен (интервал: %d ч)", self.interval // 3600)

    def stop(self) -> None:
        """Останавливает фоновый поток (ждёт не более 2 сек)."""
        with self._lock:
            self._restart_requested = False
            self._stop_event.set()
            t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        log.debug("BogusUpdater: остановка запрошена")

    # ── Внутренняя логика ────────────────────────────────────────────────────

    def add_task(self, name: str, callback: Callable[[], bool]) -> None:
        """Register before start; True = 24h, False/exception = retry in 1h.

        Callbacks are synchronous and must bound their network requests. One
        task failing never prevents the remaining tasks from running.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Register tasks before starting the updater")
            if any(task.name == name for task in self._tasks):
                raise ValueError("Duplicate periodic task")
            self._tasks.append(PeriodicTask(name, callback))

    def _run_due(self) -> None:
        for task in self._tasks:
            if self._stop_event.is_set():
                break
            if time.monotonic() < task.next_due:
                continue
            try:
                ok = bool(task.callback())
            except Exception as exc:
                log_recoverable(log, "Ошибка фонового обновления " + task.name,
                                exc, level=logging.WARNING)
                ok = False
            task.next_due = time.monotonic() + (self.interval if ok else RETRY_INTERVAL)

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._run_due()
                delay = min(task.next_due for task in self._tasks) - time.monotonic()
                self._stop_event.wait(max(0.1, min(60.0, delay)))
        finally:
            # Lifecycle transition under the same lock as start/stop. Starting
            # the replacement here avoids a lost restart during DNS reconnect.
            with self._lock:
                self._thread = None
                if self._restart_requested:
                    self._restart_requested = False
                    self._stop_event.clear()
                    self._thread = threading.Thread(
                        target=self._loop, name="UmbraNet-BogusUpdater", daemon=True
                    )
                    self._thread.start()

    def _try_update(self) -> bool:
        # The local fallback is usable, but is NOT a successful network refresh.
        with self._update_lock:
            network_ok, _ = self._update_once()
            return network_ok

    def _update_once(self) -> tuple[bool, bool]:
        """Network -> last disk cache -> bundle. Never overwrite cache offline."""
        network_ok = False
        ips, subnets = [], []
        try:
            data = _fetch_remote(self.url, timeout=HTTP_TIMEOUT)
            ips, subnets = _parse_remote_json(data, strict=True)
            network_ok = True
        except Exception as exc:
            log_recoverable(log, "Bogus-IP: сетевое обновление недоступно", exc)

        if network_ok:
            if not _save_cache(self.config_dir, ips, subnets):
                return False, False
            self._last_success = time.time()
        else:
            ips, subnets = load_cached(self.config_dir)
            if not ips and not subnets:
                ips, subnets = _load_from_local() or ([], [])
                if ips or subnets:
                    # Resolver reads the disk cache, so bootstrap it once.
                    if not _save_cache(self.config_dir, ips, subnets):
                        return False, False
        if not ips and not subnets:
            return False, False
        if self.on_update is not None:
            try:
                self.on_update(ips, subnets)
            except Exception as exc:
                log_recoverable(log, "Bogus-IP: ошибка применения списка", exc,
                                level=logging.ERROR)
                return False, False
        log.debug("Bogus-IP применены: %d IP, %d подсетей; сеть=%s",
                  len(ips), len(subnets), network_ok)
        return network_ok, True

    @property
    def last_updated(self) -> float | None:
        """Unix-timestamp последнего сетевого обновления этой сессии, или None."""
        return self._last_success or None

    def force_update(self) -> bool:
        """Blocking manual refresh; True also when an offline fallback is usable."""
        # Shares a lock with the scheduler: no simultaneous cache writes.
        with self._update_lock:
            _, usable = self._update_once()
            return usable
