"""Strict user-owned HTTPS probe contract for generic VPN exits."""

from __future__ import annotations

import socket

from config_utils import CONFIG_VERSION, apply_config_migrations, sanitize_config
from vpn.controller import TransportController
from vpn.health import probe_http_or_socks_route, probe_target_from_url
from vpn.profiles import RouteProfile, normalize_probe_url


def _profile() -> RouteProfile:
    return RouteProfile(
        id="eu",
        name="EU test exit",
        protocol="socks5",
        host="proxy.example.net",
        port=1080,
    )


def test_probe_url_accepts_only_plain_user_owned_https_hostname_urls():
    assert normalize_probe_url("https://Status.Example.NET/health") == "https://status.example.net/health"
    assert normalize_probe_url("https://status.example.net:8443/health") == "https://status.example.net:8443/health"
    assert normalize_probe_url("") == ""

    for unsafe in (
        "http://status.example.net/health",
        "https://user:password@status.example.net/health",
        "https://status.example.net/health?token=secret",
        "https://status.example.net/health#fragment",
        "https://127.0.0.1/health",
        "https://status.example.net/health path",
    ):
        assert normalize_probe_url(unsafe) is None


def test_probe_target_uses_hostname_tls_sni_and_preserves_only_safe_path():
    target = probe_target_from_url("https://status.example.net:8443/health")

    assert target is not None
    assert target.host == "status.example.net"
    assert target.port == 8443
    assert target.path == "/health"
    assert target.authority == "status.example.net:8443"


def test_config_persists_a_valid_probe_url_and_drops_invalid_one():
    cfg, warnings = sanitize_config({"transport_probe_url": "https://status.example.net/health"})

    assert cfg["transport_probe_url"] == "https://status.example.net/health"
    assert not any("transport_probe_url" in warning for warning in warnings)

    cfg, warnings = sanitize_config({"transport_probe_url": "http://status.example.net/health"})
    assert cfg["transport_probe_url"] == ""
    assert any("transport_probe_url" in warning for warning in warnings)


def test_v9_migration_adds_empty_probe_url_without_enabling_a_route():
    migrated, report = apply_config_migrations({"config_version": 9, "routing_mode": "vpn"})

    assert migrated["config_version"] == CONFIG_VERSION
    assert migrated["transport_probe_url"] == ""
    assert any("endpoint проверки VPN" in note for note in report["notes"])


def test_missing_probe_url_performs_no_network_io_and_marks_route_not_verified(monkeypatch):
    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("health check must not open a socket without a user-owned endpoint")

    monkeypatch.setattr(socket, "create_connection", unexpected_network)

    result = probe_http_or_socks_route(_profile())
    controller = TransportController([_profile()])
    snapshot = controller.run_once()

    assert not result.ok
    assert result.stage == "not_verified"
    assert snapshot["probe_configured"] is False
    assert snapshot["active_id"] == ""
    assert controller.active_profile() is None
    route = snapshot["routes"][0]
    assert route["state"] == "not_verified"
    assert route["probe"]["stage"] == "not_verified"
