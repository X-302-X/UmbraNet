"""Safe URL-subscription contracts for the currently supported proxy routes."""

from __future__ import annotations

import json

from config_utils import CONFIG_VERSION, apply_config_migrations, sanitize_config
from vpn.subscriptions import (
    fetch_transport_subscription,
    materialize_subscription_profiles,
    normalize_subscription_url,
    sanitize_transport_subscriptions,
)


def _profile(profile_id: str = "source-a") -> dict:
    return {
        "id": profile_id,
        "name": "Подписочный сервер",
        "protocol": "socks5",
        "host": "proxy.example.net",
        "port": 1080,
        "enabled": True,
        "priority": 100,
    }


class _Response:
    status = 200
    headers = {"Content-Length": "256"}

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self, _limit: int) -> bytes:
        return self._payload


def test_subscription_url_requires_https_but_preserves_an_opaque_query_token():
    url = normalize_subscription_url("https://S.Saveprox.com:443/YruV74BAYETpJfqA?token=secret")

    assert url == "https://s.saveprox.com/YruV74BAYETpJfqA?token=secret"
    assert normalize_subscription_url("http://s.saveprox.com/sub") is None
    assert normalize_subscription_url("https://user:pass@s.saveprox.com/sub") is None
    assert normalize_subscription_url("https://s.saveprox.com/sub#fragment") is None


def test_explicit_fetch_imports_only_supported_json_profiles_without_displaying_source_token():
    seen: dict[str, object] = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _Response(json.dumps({"transport_profiles": [_profile()]}).encode("utf-8"))

    result = fetch_transport_subscription("https://s.saveprox.com/private-token", opener=opener)

    assert result.profiles[0]["name"] == "Подписочный сервер"
    assert result.label == "s.saveprox.com"
    assert result.message == "Серверов в подписке: 1"
    assert seen == {"url": "https://s.saveprox.com/private-token", "timeout": 15}
    assert "private-token" not in result.message


def test_explicit_fetch_rejects_unsupported_modern_protocol_document_honestly():
    def opener(_request, timeout):
        return _Response(json.dumps({"outbounds": [{"protocol": "vless"}]}).encode("utf-8"))

    result = fetch_transport_subscription("https://s.saveprox.com/token", opener=opener)

    assert result.profiles == []
    assert "HTTP CONNECT/SOCKS5" in result.message
    assert "token" not in result.message


def test_subscription_profiles_receive_source_owned_stable_ids():
    source_id = "subscription-abc123"
    first = materialize_subscription_profiles(source_id, [_profile(), _profile("source-b")])
    second = materialize_subscription_profiles(source_id, [_profile(), _profile("source-b")])

    assert [profile["id"] for profile in first] == [profile["id"] for profile in second]
    assert len({profile["id"] for profile in first}) == 2
    assert all(profile["id"].startswith(source_id + "-") for profile in first)


def test_config_keeps_only_valid_subscription_metadata_and_migrates_v11():
    raw = {
        "transport_profiles": [_profile("subscription-abc123-remote")],
        "transport_subscriptions": [{
            "id": "subscription-abc123",
            "url": "https://s.saveprox.com/opaque-token",
            "profile_ids": ["subscription-abc123-remote"],
            "updated_at": "2026-11-03 12:00 UTC",
        }],
    }
    config, warnings = sanitize_config(raw)
    migrated, _report = apply_config_migrations({"config_version": 11})

    assert config["transport_subscriptions"] == [{
        "id": "subscription-abc123",
        "url": "https://s.saveprox.com/opaque-token",
        "label": "s.saveprox.com",
        "profile_ids": ["subscription-abc123-remote"],
        "updated_at": "2026-11-03 12:00 UTC",
    }]
    assert not [warning for warning in warnings if "transport_subscriptions" in warning]
    assert migrated["config_version"] == CONFIG_VERSION
    assert migrated["transport_subscriptions"] == []


def test_invalid_subscription_metadata_is_dropped_without_echoing_private_url():
    subscriptions, warnings = sanitize_transport_subscriptions([{
        "id": "subscription-invalid",
        "url": "http://s.saveprox.com/private-token",
        "profile_ids": [],
    }])

    assert subscriptions == []
    assert warnings
    assert all("private-token" not in warning for warning in warnings)
