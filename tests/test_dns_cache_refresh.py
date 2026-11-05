"""Регрессии очистки DNS-кэшей при смене сессии и профиля."""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dns")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

pytest.importorskip("dnslib")
from dnslib import A, DNSRecord, QTYPE, RR

from dns_cache import DNSCache
from dns_server import UmbraNetDNS, UmbraNetResolver


def _request(domain: str = "chatgpt.com"):
    return DNSRecord.question(domain, "A")


def _response(domain: str = "chatgpt.com", ip: str = "203.0.113.10", ttl: int = 60):
    reply = DNSRecord.question(domain, "A").reply()
    reply.add_answer(RR(domain, QTYPE.A, rdata=A(ip), ttl=ttl))
    return reply


def test_clear_increments_generation_and_rejects_old_background_write():
    cache = DNSCache()
    request = _request()
    old_generation = cache.generation()
    cache.set(request, True, _response())

    cache.clear()

    assert cache.generation() == old_generation + 1
    assert cache.set(
        request,
        True,
        _response(ip="203.0.113.11"),
        expected_generation=old_generation,
    ) is False
    assert cache.get_with_state(request, True) == (None, "miss")


def test_background_refresh_cannot_restore_entry_after_clear(monkeypatch):
    cache = DNSCache()
    request = _request()
    cache.set(request, True, _response())
    resolver = UmbraNetResolver([{"bogus_detection_enabled": False}], cache)
    refresh_started = threading.Event()
    allow_response = threading.Event()
    write_attempted = threading.Event()

    original_set = cache.set

    def observed_set(*args, **kwargs):
        result = original_set(*args, **kwargs)
        write_attempted.set()
        return result

    monkeypatch.setattr(cache, "set", observed_set)

    def upstream_response():
        refresh_started.set()
        assert allow_response.wait(2), "тест не разрешил фоновый ответ"
        return _response(ip="203.0.113.12")

    resolver._spawn_background_refresh(request, True, upstream_response, {})
    assert refresh_started.wait(2), "фоновое обновление не стартовало"

    cache.clear()
    allow_response.set()

    assert write_attempted.wait(2), "фоновое обновление не попыталось записать ответ"
    assert cache.get_with_state(request, True) == (None, "miss")


def test_start_discards_cache_from_previous_session(monkeypatch):
    """Даже неудачный новый запуск не должен хранить ответы прошлой сессии."""
    import dns_server

    cache = DNSCache()
    request = _request()
    cache.set(request, True, _response())

    server = object.__new__(UmbraNetDNS)
    server.running = False
    server.cache = cache
    server.config = {}
    server.last_start_error = ""

    class _FallbackState:
        def reset(self):
            return None

    monkeypatch.setattr(dns_server, "get_fallback_state", lambda: _FallbackState())
    monkeypatch.setattr(dns_server, "preflight_check", lambda _cfg: (False, ["занято"], []))

    assert server.start() is False
    assert cache.get_with_state(request, True) == (None, "miss")


def test_stop_discards_cache_from_finished_session():
    cache = DNSCache()
    request = _request()
    cache.set(request, True, _response())

    server = object.__new__(UmbraNetDNS)
    server.cache = cache
    server.running = True
    server.server4 = None
    server.server4_tcp = None
    server.server6 = None
    server.server6_tcp = None
    server.winws = None
    server.bogus_updater = None
    server.process_tracker = None
    server._resolver = None

    server.stop()

    assert cache.get_with_state(request, True) == (None, "miss")


def test_ui_cache_action_clears_umbranet_before_windows_cache(monkeypatch):
    from umbranet import engine_adapter

    events = []

    class _Engine:
        def clear_dns_cache(self, reason):
            events.append(("umbranet", reason))

    monkeypatch.setattr(engine_adapter, "get_engine", lambda: _Engine())
    import process_monitor

    monkeypatch.setattr(process_monitor, "flush_dns_cache", lambda: events.append(("windows", "")) or True)

    assert engine_adapter.flush_dns_cache() is True
    assert events == [("umbranet", "запрос из интерфейса"), ("windows", "")]
