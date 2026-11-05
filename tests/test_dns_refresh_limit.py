"""
Тесты потолка фоновых обновлений DNS-кэша (P2-1).
=================================================

Что было не так. У «оптимистичного» кэша есть фоновое обновление: ответ уже
отдан из просроченного кэша, а свежий запрашивается в отдельном потоке. Поток
создавался на КАЖДЫЙ промах: дедупликация была только по одному домену, а
общего потолка не было вовсе. Человек открывает страницу с сотней хостов — и
все сто обновлений уходят в апстрим одновременно: сотня потоков, забитая сеть,
нагрузка на DNS-серверы, а толку ноль (ответ пользователю уже отдан).

Что стало: не больше `_MAX_BG_REFRESHES` (8) одновременных обновлений. Если все
места заняты — обновление пропускается, метка «обновляется» снимается, и
следующий запрос к этому домену попробует снова. Ответ из кэша при этом никто
не теряет.

Запуск: python -m pytest tests/test_dns_refresh_limit.py
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dns"), str(ROOT / "core" / "dpi")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

dnslib = pytest.importorskip("dnslib")
import dns_server as ds
from dnslib import DNSRecord


class _Cache:
    """Кэш-заглушка: следит за метками «обновляется» и считает записи."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refreshing: set = set()
        self.stored: list = []
        # Контракт настоящего DNSCache: refresh привязан к поколению,
        # чтобы очистка кэша не могла быть отменена запоздалым ответом.
        self._generation = 0

    def generation(self) -> int:
        with self.lock:
            return self._generation

    @staticmethod
    def _key(request, routed):
        return (str(request.q.qname), int(request.q.qtype), bool(routed))

    def mark_refreshing(self, request, routed) -> bool:
        key = self._key(request, routed)
        with self.lock:
            if key in self.refreshing:
                return False
            self.refreshing.add(key)
            return True

    def unmark_refreshing(self, request, routed) -> None:
        with self.lock:
            self.refreshing.discard(self._key(request, routed))

    def set(self, request, routed, response, **kwargs):
        with self.lock:
            self.stored.append(self._key(request, routed))
        return True

    def get_with_state(self, request, routed):
        return None, "miss"


class _Refresher:
    """Заглушка апстрима: считает одновременные вызовы и держит их до отмашки."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = 0
        self.max_live = 0
        self.started = 0
        self.gate = threading.Event()
        self.finished = threading.Event()

    def __call__(self):
        with self.lock:
            self.live += 1
            self.started += 1
            self.max_live = max(self.max_live, self.live)
        try:
            self.gate.wait(30.0)   # пока тест не отпустит: держим поток занятым
            return DNSRecord.question("a.example")
        finally:
            with self.lock:
                self.live -= 1

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.live == 0:
                    return True
            time.sleep(0.01)
        return False


@pytest.fixture
def resolver(monkeypatch):
    import threading as _threading

    cache = _Cache()
    obj = ds.UmbraNetResolver([{"optimistic_cache_enabled": True}], cache=cache)
    monkeypatch.setattr(obj, "_check_bogus", lambda response: (False, None))
    assert isinstance(obj._refresh_slots, type(_threading.BoundedSemaphore(1)))
    return obj


def _ask(resolver, refresher, domain: str, routed: bool = False) -> bool:
    """Просит фоновое обновление для домена. True — обновление запущено."""
    request = DNSRecord.question(domain)
    before = refresher.started
    resolver._spawn_background_refresh(request, routed, lambda: refresher(), {})
    # Даём потоку шанс дойти до вызова апстрима (старт потока — миллисекунды).
    for _ in range(50):
        if refresher.started > before:
            return True
        time.sleep(0.004)
    return False


def test_refresh_threads_are_capped(resolver):
    """Сотня доменов разом не поднимает сотню потоков: не больше потолка."""
    refresher = _Refresher()
    limit = ds._MAX_BG_REFRESHES
    try:
        started = 0
        for i in range(limit + 12):
            started += 1 if _ask(resolver, refresher, f"burst{i}.example") else 0

        assert started == limit, (
            f"одновременных обновлений запущено {started}, а потолок {limit} — "
            "под нагрузкой это сотни потоков в апстрим"
        )
        assert refresher.max_live <= limit, (
            f"в апстрим ушло {refresher.max_live} запросов сразу (потолок {limit})"
        )
    finally:
        refresher.gate.set()
        assert refresher.wait_idle(), "фоновые обновления не завершились"


def test_skipped_refresh_can_be_retried(resolver):
    """Пропущенное обновление не «залипает»: после освобождения места оно проходит."""
    refresher = _Refresher()
    limit = ds._MAX_BG_REFRESHES
    try:
        for i in range(limit):
            assert _ask(resolver, refresher, f"hold{i}.example"), "обновление не запустилось"

        # Все места заняты — просьба пропускается...
        assert not _ask(resolver, refresher, "later.example"), "запустили сверх потолка"

        # ...и домен НЕ остаётся помеченным «обновляется»: иначе он повис бы навсегда.
        request = DNSRecord.question("later.example")
        assert resolver.cache.mark_refreshing(request, False) is True, (
            "метка «обновляется» не снята — этот домен больше никогда не обновится"
        )
        resolver.cache.unmark_refreshing(request, False)

        # Освобождаем место и убеждаемся, что повторы снова проходят.
        refresher.gate.set()
        assert refresher.wait_idle(), "обновления не завершились"
    finally:
        refresher.gate.set()

    refresher2 = _Refresher()
    try:
        assert _ask(resolver, refresher2, "after.example"), (
            "после освобождения места новые обновления не запускаются — семафор течёт"
        )
    finally:
        refresher2.gate.set()
        assert refresher2.wait_idle()


def test_same_domain_is_not_refreshed_twice(resolver):
    """Повторная просьба про тот же домен, пока он обновляется, игнорируется."""
    refresher = _Refresher()
    try:
        assert _ask(resolver, refresher, "same.example"), "первое обновление не запустилось"
        assert not _ask(resolver, refresher, "same.example"), "тот же домен обновили дважды"
        assert refresher.started == 1, f"в апстрим ушло {refresher.started} запросов вместо одного"
    finally:
        refresher.gate.set()
        assert refresher.wait_idle()
