"""
UmbraNet — Query Log
=====================

Структурированный журнал DNS- и DPI-активности. В отличие от обычного
``umbranet.log`` и полного ``logs/e1-spike.log`` (они текстовые и нужны для
диагностики), Query Log хранит безопасные пользовательские события как объекты
с полями и предназначен для отображения в GUI и экспорта.

Архитектура:
  - `QueryLog` — потокобезопасный ring-buffer записей фиксированного размера.
  - Подписчики (`subscribe(callback)`) — GUI просто подписывается на новые
    записи и обновляет Treeview через сигнал. Без поллинга файла.
  - `QueryLogEntry` — dataclass, чтобы UI не зависел от сетевых библиотек или
    формата stdout DPI-движка.

Каждый DNS resolve() в dns_server.py создаёт ровно одну запись. Реальные
структурированные события e1-spike добавляет `core.dpi.log_bridge`.
"""

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("UmbraNet.QueryLog")


# Источники ответа — отображаются в колонке "Источник" и используются для
# фильтрации. Эти строки попадают в UI как есть, поэтому короткие и понятные.
SOURCE_CACHE_FRESH = "cache"        # отдали из свежего кэша (0-1 мс)
SOURCE_CACHE_STALE = "stale-cache"  # отдали из stale-окна (optimistic)
SOURCE_ROUTED = "routed"            # пошли в xbox-dns (UDP или DoH)
SOURCE_SYSTEM = "system"            # пошли в fallback DNS провайдера
SOURCE_BOGUS_NX = "bogus-NX"        # bogus-detection вернул NXDOMAIN
SOURCE_BLOCKED = "blocked"        # пользовательская блокировка домена
SOURCE_SERVFAIL = "servfail"        # upstream не ответил
SOURCE_BG_REFRESH = "bg-refresh"    # фоновый refresh stale-записи (служебная)
SOURCE_DPI = "dpi"                  # реальное действие или lifecycle DPI-движка
SOURCE_DPI_ERROR = "dpi-error"      # реальная ошибка DPI-движка или его lifecycle


@dataclass
class QueryLogEntry:
    """Одна запись DNS-журнала.

    Все поля сериализуемы — `asdict()` отдаст готовый dict для CSV-экспорта.
    """
    timestamp: float            # unix time, time.time()
    domain: str                 # доменное имя запроса (без точки в конце)
    qtype: str                  # "A", "AAAA", "HTTPS", и т.д.
    source: str                 # один из SOURCE_* (DNS, DPI или системная диагностика)
    routed: bool                # был ли домен в списке routed_domains
    rcode: str = "NOERROR"      # текстовый код ответа
    latency_ms: int = 0         # сколько занял resolve() (0 для cache)
    answers: list[str] = field(default_factory=list)   # короткий список IP/CNAME для UI
    note: str = ""              # дополнительная пометка (например "fake-IP 212.188.4.10")


class QueryLog:
    """Потокобезопасный ring-buffer с pub/sub.

    Размер задаётся в конструкторе и может меняться через `set_max_size()`
    без потери уже накопленных записей (если уменьшение — обрежем старые).
    """

    DEFAULT_SIZE = 500
    MIN_SIZE = 50
    MAX_SIZE = 5000

    def __init__(self, max_size: int = DEFAULT_SIZE):
        self._lock = threading.Lock()
        self._entries: deque = deque(maxlen=self._clamp_size(max_size))
        self._subscribers: list[Callable[[QueryLogEntry], None]] = []
        # На время паузы новые записи всё равно пишутся в буфер, но
        # подписчики НЕ уведомляются — это даёт GUI «заморозить» список.
        self._paused = False
        self._sequence = 0   # инкрементный id записи, удобно для дедупликации в UI

    @classmethod
    def _clamp_size(cls, size: int) -> int:
        try:
            size = int(size)
        except Exception:
            size = cls.DEFAULT_SIZE
        return max(cls.MIN_SIZE, min(cls.MAX_SIZE, size))

    # ── публичный API записи ────────────────────────────────────────────────
    def add(self, entry: QueryLogEntry):
        """Добавить запись. Можно вызывать из любого потока."""
        with self._lock:
            self._sequence += 1
            entry_id = self._sequence
            self._entries.append((entry_id, entry))
            paused = self._paused
            subs_snapshot = list(self._subscribers)

        if paused:
            return
        # Уведомляем подписчиков ВНЕ lock — иначе подписчик, который пытается
        # читать entries из callback, заблокирует следующий add().
        for cb in subs_snapshot:
            try:
                cb(entry)
            except Exception as exc:
                log.warning("QueryLog subscriber raised: %s", exc)

    # ── публичный API чтения ────────────────────────────────────────────────
    def snapshot(self) -> list[QueryLogEntry]:
        """Копия всех записей, упорядоченная от старой к новой."""
        with self._lock:
            return [e for (_id, e) in self._entries]

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()

    # ── управление ──────────────────────────────────────────────────────────
    def set_max_size(self, size: int):
        size = self._clamp_size(size)
        with self._lock:
            if size == self._entries.maxlen:
                return
            old = list(self._entries)
            self._entries = deque(old[-size:], maxlen=size)

    def set_paused(self, paused: bool):
        with self._lock:
            self._paused = bool(paused)

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    # ── pub/sub ─────────────────────────────────────────────────────────────
    def subscribe(self, callback: Callable[[QueryLogEntry], None]):
        """Подписать callback. Он будет вызываться из ТОГО потока, который
        делает add() — обычно DNS resolver. UI должен через after() прокинуть
        обновление в свой поток.
        """
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass


# ── Глобальный singleton ────────────────────────────────────────────────────
# Один общий QueryLog на всё приложение. dns_server.py пишет, GUI читает.

_instance: QueryLog | None = None
_instance_lock = threading.Lock()


def get_query_log(max_size: int = QueryLog.DEFAULT_SIZE) -> QueryLog:
    """Возвращает (создавая при необходимости) глобальный QueryLog.

    Если уже создан с другим размером — текущий размер сохраняется.
    Для смены размера используй `get_query_log().set_max_size(...)`.
    """
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = QueryLog(max_size=max_size)
    return _instance
