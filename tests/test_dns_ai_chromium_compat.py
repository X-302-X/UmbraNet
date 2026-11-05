"""Регрессии Chromium: DNS-AI должен не отдавать прямые HTTPS/IPv6-подсказки.

DNS-AI выдаёт специальный IPv4 для доменов обхода, но его AAAA и HTTPS/SVCB
записи могут содержать обычные адреса CDN. Chromium приоритетно использует эти
подсказки, минуя IPv4-результат. Для активного DNS-AI маршрутизируемые запросы
этих типов получают пустой NOERROR и браузер использует A-запись.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for path in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dns")):
    if path not in sys.path:
        sys.path.insert(0, path)

pytest.importorskip("dnslib")
from dnslib import DNSRecord, RCODE

import dns_server
from profile_utils import DNS_AI_PROFILE_ID, XBOX_PROFILE_ID


class _NoCache:
    """В этой ветке кэш не нужен и обращаться к нему нельзя."""

    def get_with_state(self, *_args, **_kwargs):
        raise AssertionError("совместимость Chromium должна ответить до проверки кэша")


def test_dns_ai_browser_compat_covers_direct_hint_record_types():
    profile = {"id": DNS_AI_PROFILE_ID}

    assert dns_server._needs_dns_ai_browser_compat(profile, "AAAA") is True
    assert dns_server._needs_dns_ai_browser_compat(profile, "HTTPS") is True
    assert dns_server._needs_dns_ai_browser_compat(profile, "SVCB") is True
    assert dns_server._needs_dns_ai_browser_compat(profile, "A") is False


def test_dns_ai_routed_aaaa_returns_empty_noerror_before_upstream_or_cache(monkeypatch):
    config = {
        "active_dns_profile": DNS_AI_PROFILE_ID,
        "routed_cache_enabled": True,
        "routed_cache_ttl": 5,
        "stale_cache_ttl": 3600,
        "ipv6_priority_enabled": False,
    }
    resolver = dns_server.UmbraNetResolver([config], cache=_NoCache())
    request = DNSRecord.question("chatgpt.com", "AAAA")

    monkeypatch.setattr(
        dns_server,
        "resolve_via_xbox",
        lambda *_args, **_kwargs: pytest.fail("апстрим не должен вызываться для AAAA-подсказки"),
    )

    reply = resolver._resolve_routed(request, "chatgpt.com", "AAAA", routed=True)

    assert reply.header.rcode == RCODE.NOERROR
    assert reply.rr == []


def test_other_dns_profile_keeps_its_aaaa_and_https_answers():
    profile = {"id": XBOX_PROFILE_ID}

    assert dns_server._needs_dns_ai_browser_compat(profile, "AAAA") is False
    assert dns_server._needs_dns_ai_browser_compat(profile, "HTTPS") is False
    assert dns_server._needs_dns_ai_browser_compat(profile, "SVCB") is False
