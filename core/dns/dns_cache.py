import copy
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("UmbraNet.DNSCache")


@dataclass
class CacheEntry:
    response: object
    created_at: float
    expires_at: float          # обычный TTL ответа (когда запись становится «stale»)
    stale_until: float = 0.0   # до этого момента можно отдавать просроченный ответ
    refreshing: bool = False   # сейчас идёт фоновый refresh? (не запускаем второй)

    def is_fresh(self) -> bool:
        return time.monotonic() < self.expires_at

    def is_stale_but_usable(self) -> bool:
        now = time.monotonic()
        return self.expires_at <= now < self.stale_until

    def is_completely_expired(self) -> bool:
        return time.monotonic() >= self.stale_until

    def clone_with_adjusted_ttl(self):
        """Возвращает копию ответа с TTL, уменьшенным на возраст записи.

        Для stale-ответов TTL уже истёк, поэтому отдаём минимальный 1 сек,
        чтобы клиент не закешировал просроченную запись надолго.
        """
        now = time.monotonic()
        age = int(max(0, now - self.created_at))
        is_stale = now >= self.expires_at
        cloned = copy.deepcopy(self.response)
        for section in (cloned.rr, cloned.auth, cloned.ar):
            for rr in section:
                ttl = int(getattr(rr, "ttl", 0) or 0)
                if is_stale:
                    # stale-ответ: клиент должен переспросить как можно скорее
                    rr.ttl = 1
                elif ttl > 0:
                    rr.ttl = max(1, ttl - age)
        return cloned


class DNSCache:
    """In-memory TTL кэш DNS-ответов с поддержкой optimistic (stale-while-revalidate).

    Запись живёт в две стадии:
      [created_at .. expires_at]      — свежая, отдаём как есть
      [expires_at .. stale_until]     — просроченная, но отдаём моментально
                                        и в фоне запрашиваем у апстрима новую
      после stale_until                — выкидываем
    """

    def __init__(self, max_entries: int = 5000, janitor_interval: float = 30.0):
        self._entries = {}
        self._lock = threading.Lock()
        # Поколение отделяет новую DNS-сессию/конфигурацию от уже запущенного
        # stale-refresh. После clear() старый фоновый ответ не имеет права
        # снова положить адрес прежнего провайдера в очищенный кэш.
        self._generation = 0
        # Верхний лимит записей — защита от неограниченного роста
        # (например при route_all или очень долгой работе).
        self.max_entries = max(0, int(max_entries))  # 0 = без лимита
        self._janitor_interval = max(1.0, float(janitor_interval))
        self._janitor = None
        self._janitor_stop = threading.Event()

    @staticmethod
    def _make_key(request, routed: bool):
        qname = str(request.q.qname).rstrip('.').lower()
        qtype = int(request.q.qtype)
        qclass = int(request.q.qclass)
        return qname, qtype, qclass, bool(routed)

    @staticmethod
    def _response_ttl(response) -> int:
        """
        Возвращает TTL для записи кэша.
        Берём минимальный положительный TTL из Answer/Auth/Additional,
        чтобы не держать ответ дольше жизни самой короткой записи.
        """
        ttls = []
        for section in (response.rr, response.auth, response.ar):
            for rr in section:
                ttl = int(getattr(rr, "ttl", 0) or 0)
                if ttl > 0:
                    ttls.append(ttl)
        return min(ttls) if ttls else 0

    def get(self, request, routed: bool):
        """Старое API: возвращает свежий ответ либо None.

        Оставлено для обратной совместимости. Для optimistic-логики используй
        get_with_state(), которое умеет отдавать stale-ответ.
        """
        key = self._make_key(request, routed)
        now = time.monotonic()  # один вызов на весь метод
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if now >= entry.expires_at:
                # обычный get() намеренно не отдаёт stale-ответы —
                # этим занимается get_with_state(), чтобы вызывающий код
                # явно решал, запускать ли фоновое обновление.
                if now >= entry.stale_until:
                    self._entries.pop(key, None)
                return None
        return entry.clone_with_adjusted_ttl()

    def get_with_state(self, request, routed: bool):
        """Возвращает кортеж (response, state):

            state = "fresh"   — ответ свежий, отдавать как есть
            state = "stale"   — ответ просроченный, но в stale-окне:
                                отдаём моментально, вызывающий код должен
                                запустить фоновое обновление
            state = "miss"    — ничего нет в кэше или окно прошло
                                (response = None)
        """
        key = self._make_key(request, routed)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None, "miss"
            if entry.is_completely_expired():
                self._entries.pop(key, None)
                return None, "miss"
            state = "fresh" if entry.is_fresh() else "stale"

        # deepcopy ответа может быть заметным для больших DNS-ответов. Запись
        # после set() не меняется (при refresh создаётся новый объект), поэтому
        # не удерживаем общий lock во время копирования.
        return entry.clone_with_adjusted_ttl(), state

    def mark_refreshing(self, request, routed: bool) -> bool:
        """Атомарно помечает запись как «refresh уже идёт».

        Возвращает True, если это мы первые взялись обновлять (значит,
        вызывающий код должен запустить фоновую задачу), и False — если
        кто-то уже обновляет (значит, ничего не делаем).
        """
        key = self._make_key(request, routed)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.refreshing:
                return False
            entry.refreshing = True
            return True

    def unmark_refreshing(self, request, routed: bool):
        key = self._make_key(request, routed)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.refreshing = False

    def generation(self) -> int:
        """Текущее поколение кэша для безопасного stale-refresh в фоне."""
        with self._lock:
            return self._generation

    def set(
        self,
        request,
        routed: bool,
        response,
        ttl_override=None,
        stale_ttl: int = 0,
        expected_generation: int | None = None,
    ) -> bool:
        """Сохраняет ответ, если поколение кэша не сменилось.

        ``expected_generation`` используют только фоновые обновления. Обычные
        запросы передают ``None`` и всегда могут записать свежий ответ.
        """
        ttl = self._response_ttl(response)
        if ttl_override is not None:
            try:
                ttl_override = int(ttl_override)
            except Exception:
                ttl_override = None
            if ttl_override is not None and ttl_override >= 0:
                ttl = ttl_override if ttl == 0 else min(ttl, ttl_override)
        if ttl <= 0:
            # Если основного TTL нет — кладём минимум на 1 сек,
            # чтобы optimistic-кэш мог хотя бы попытаться отдать stale.
            ttl = 1

        try:
            stale_ttl = max(0, int(stale_ttl))
        except Exception:
            stale_ttl = 0

        now = time.monotonic()
        entry = CacheEntry(
            response=copy.deepcopy(response),
            created_at=now,
            expires_at=now + ttl,
            stale_until=now + ttl + stale_ttl,
            refreshing=False,
        )
        key = self._make_key(request, routed)
        with self._lock:
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                return False
            self._entries[key] = entry
            self._enforce_limit_locked()
        return True

    def _enforce_limit_locked(self):
        """Если записей больше лимита — выбрасываем самые старые по created_at.

        Вызывать только под self._lock. Сначала убираем полностью истёкшие,
        затем (если всё ещё над лимитом) — самые старые живые записи.
        """
        if not self.max_entries or len(self._entries) <= self.max_entries:
            return
        now = time.monotonic()
        # 1) сперва выкидываем полностью истёкшие (бесплатно)
        expired = [k for k, e in self._entries.items() if e.stale_until <= now]
        for k in expired:
            self._entries.pop(k, None)
        if len(self._entries) <= self.max_entries:
            return
        # 2) всё ещё много — выкидываем самые старые по времени создания
        overflow = len(self._entries) - self.max_entries
        oldest = sorted(self._entries.items(), key=lambda kv: kv[1].created_at)[:overflow]
        for k, _ in oldest:
            self._entries.pop(k, None)

    def clear(self):
        """Очищает записи и инвалидирует незавершённые stale-refresh задачи."""
        with self._lock:
            self._entries.clear()
            self._generation += 1

    def prune_expired(self):
        now = time.monotonic()
        with self._lock:
            expired_keys = [k for k, e in self._entries.items() if e.stale_until <= now]
            for k in expired_keys:
                self._entries.pop(k, None)
            return len(expired_keys)

    # ── Фоновый уборщик ──────────────────────────────────────────────────────
    def start_janitor(self):
        """Запускает фоновый поток, периодически чистящий истёкшие записи."""
        current = self._janitor
        if current is not None and current.is_alive():
            # Если stop_janitor() только что вызван, Event будит поток сразу;
            # дождёмся его завершения, чтобы не создавать два уборщика.
            if self._janitor_stop.is_set() and current is not threading.current_thread():
                current.join(timeout=1.0)
            if current.is_alive():
                return
        self._janitor_stop.clear()
        self._janitor = threading.Thread(
            target=self._janitor_loop, daemon=True, name="DNSCacheJanitor"
        )
        self._janitor.start()

    def stop_janitor(self):
        self._janitor_stop.set()
        current = self._janitor
        if current is not None and current is not threading.current_thread():
            current.join(timeout=1.0)
        if current is self._janitor and current is not None and not current.is_alive():
            self._janitor = None

    def _janitor_loop(self):
        while not self._janitor_stop.wait(self._janitor_interval):
            try:
                removed = self.prune_expired()
                if removed:
                    log.debug("[cache-janitor] удалено истёкших записей: %s", removed)
            except Exception as exc:
                log.debug("[cache-janitor] ошибка: %s", exc)

    def __len__(self):
        self.prune_expired()
        with self._lock:
            return len(self._entries)
