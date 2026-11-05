"""Проверки DNS-AI как основного профиля и HTTP/2 DoH-пробы без сети."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dns")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

pytest.importorskip("dnslib")
from dnslib import DNSRecord

import config_utils
import endpoint_probe
import profile_utils


def _reply_for(request: DNSRecord) -> bytes:
    return request.reply().pack()


def test_dns_ai_is_first_builtin_and_default_profile():
    profiles = profile_utils.get_builtin_dns_profiles()

    assert profiles[0]["id"] == profile_utils.DNS_AI_PROFILE_ID
    assert profiles[0]["name"] == "DNS-AI"
    assert profiles[0]["doh_url"] == "https://dns.dns-ai.ru/dns-query"
    assert profiles[1]["id"] == profile_utils.XBOX_PROFILE_ID
    assert config_utils.DEFAULT_CONFIG["active_dns_profile"] == profile_utils.DNS_AI_PROFILE_ID


def test_missing_active_profile_uses_dns_ai_default():
    config, warnings = config_utils.sanitize_config({})

    assert config["active_dns_profile"] == profile_utils.DNS_AI_PROFILE_ID
    assert not any("active_dns_profile" in warning for warning in warnings)


def test_doh_query_prefers_http2_and_parses_dns_response(monkeypatch):
    request = DNSRecord.question("example.com", "A")
    calls = []

    def fake_http2(url, wire, *, timeout, connect_ip, method):
        calls.append((url, wire, timeout, connect_ip, method))
        return _reply_for(request), "HTTP/2"

    monkeypatch.setattr(endpoint_probe, "_query_http2_payload", fake_http2)

    response, transport = endpoint_probe.query_doh_record(
        "https://dns.dns-ai.ru/dns-query", request, connect_ip="194.87.196.13"
    )

    assert transport == "HTTP/2"
    assert response.header.id == request.header.id
    assert calls == [
        ("https://dns.dns-ai.ru/dns-query", request.pack(), 5.0, "194.87.196.13", "GET")
    ]


def test_http1_fallback_is_used_only_without_http2(monkeypatch):
    request = DNSRecord.question("example.com", "A")
    calls = []

    def no_http2(*_args, **_kwargs):
        raise endpoint_probe.DohProtocolError("нет HTTP/2")

    def fake_http1(url, wire, timeout):
        calls.append((url, wire, timeout))
        return _reply_for(request), "HTTP/1.1"

    monkeypatch.setattr(endpoint_probe, "_query_http2_payload", no_http2)
    monkeypatch.setattr(endpoint_probe, "_query_http1_payload", fake_http1)

    _response, transport = endpoint_probe.query_doh_record(
        "https://resolver.example/dns-query", request
    )

    assert transport == "HTTP/1.1"
    assert calls == [("https://resolver.example/dns-query", request.pack(), 5.0)]


def test_profile_ip_with_doh_is_checked_through_that_ip(monkeypatch):
    seen = {}

    def fake_doh(url, *, connect_ip, timeout):
        seen.update(url=url, connect_ip=connect_ip, timeout=timeout)
        return endpoint_probe.ProbeResult(True, 12, "HTTP/2 · DNS-AI")

    monkeypatch.setattr(endpoint_probe, "probe_doh_endpoint", fake_doh)

    result = endpoint_probe.probe_profile_endpoint(
        {"doh_url": "https://dns.dns-ai.ru/dns-query"},
        "2a00:b700:5::3:62",
    )

    assert result.ok is True
    assert seen == {
        "url": "https://dns.dns-ai.ru/dns-query",
        "connect_ip": "2a00:b700:5::3:62",
        "timeout": 4.0,
    }


def test_runtime_doh_uses_profile_ip_without_resolving_upstream_name(monkeypatch):
    import dns_server

    profile = dict(profile_utils.DNS_AI_DNS_PROFILE)
    request = DNSRecord.question("chatgpt.com", "A")
    seen = []

    def fake_query(url, current_request, *, timeout, connect_ip):
        seen.append((url, current_request, timeout, connect_ip))
        return current_request.reply()

    monkeypatch.setattr(dns_server, "_query_doh_dns", fake_query)

    response = dns_server.resolve_via_xbox_doh(
        request, {"active_dns_profile": profile["id"]}, profile=profile
    )

    assert response is not None
    assert seen == [
        (profile["doh_url"], request, 5.0, profile["ipv4_primary"])
    ]


def test_upstream_hostname_gets_bootstrap_reply_instead_of_routed_lookup():
    import dns_server

    profile = dict(profile_utils.DNS_AI_DNS_PROFILE)
    request = DNSRecord.question("dns.dns-ai.ru", "A")

    response = dns_server._bootstrap_upstream_reply(request, profile)

    assert response is not None
    assert {str(answer.rdata) for answer in response.rr} == {
        profile["ipv4_primary"], profile["ipv4_secondary"]
    }
