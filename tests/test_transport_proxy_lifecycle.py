"""PAC snapshot/restore and transport runtime lifecycle tests without Windows."""

from __future__ import annotations

from dataclasses import dataclass
import io

import watchdog
from vpn.controller import TransportController
from vpn.health import RouteProbeResult
from vpn.runtime import TransportRuntime
from vpn.system_proxy import RegistryValue, SystemProxyManager


class _MemoryRegistry:
    def __init__(self, values: dict[str, RegistryValue] | None = None, fail_write: str = "",
                 fail_once: bool = False):
        self.values = dict(values or {})
        self.fail_write = fail_write
        self.fail_once = fail_once
        self.writes: list[str] = []

    def read(self, name: str) -> RegistryValue:
        return self.values.get(name, RegistryValue(False))

    def write(self, name: str, entry: RegistryValue) -> None:
        if name == self.fail_write:
            if self.fail_once:
                self.fail_write = ""
            raise OSError("registry write failed")
        self.writes.append(name)
        self.values[name] = entry

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


_INITIAL_PROXY = {
    "AutoConfigURL": RegistryValue(True, "https://company.example/proxy.pac", 1),
    "AutoDetect": RegistryValue(True, 1, 4),
    "ProxyEnable": RegistryValue(True, 1, 4),
    "ProxyServer": RegistryValue(True, "corp-proxy:8080", 1),
    "ProxyOverride": RegistryValue(True, "localhost;<local>", 1),
}


def test_system_proxy_refuses_to_change_a_non_windows_host(tmp_path):
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(snapshot_path=snapshot, windows=False)

    ok, message = manager.apply_pac("http://127.0.0.1:18777/proxy.pac")

    assert not ok
    assert "Windows" in message
    assert not snapshot.exists()


def test_system_proxy_apply_preserves_every_original_value_and_restore_returns_it(tmp_path):
    registry = _MemoryRegistry(_INITIAL_PROXY)
    notifications = []
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(
        snapshot_path=snapshot,
        registry=registry,
        notifier=lambda: notifications.append("refresh") or True,
        windows=True,
    )

    ok, message = manager.apply_pac("http://127.0.0.1:18777/proxy.pac")

    assert ok, message
    assert snapshot.exists()
    assert registry.read("AutoConfigURL") == RegistryValue(True, "http://127.0.0.1:18777/proxy.pac", 1)
    assert registry.read("ProxyEnable") == RegistryValue(True, 0, 4)
    assert registry.read("AutoDetect") == RegistryValue(True, 0, 4)
    assert registry.read("ProxyServer") == _INITIAL_PROXY["ProxyServer"]
    assert notifications == ["refresh"]

    ok, message = manager.restore()

    assert ok, message
    assert not snapshot.exists()
    assert registry.values == _INITIAL_PROXY
    assert notifications == ["refresh", "refresh"]


def test_system_proxy_keeps_snapshot_if_windows_refuses_to_refresh_wininet(tmp_path):
    registry = _MemoryRegistry(_INITIAL_PROXY)
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(snapshot_path=snapshot, registry=registry, notifier=lambda: False, windows=True)

    ok, _message = manager.apply_pac("http://127.0.0.1:18777/proxy.pac")

    assert not ok
    assert registry.values == _INITIAL_PROXY
    assert snapshot.exists(), "watchdog должен повторить refresh после сбоя WinINET"


def test_system_proxy_restore_deletes_values_that_were_absent_before_start(tmp_path):
    original = {"AutoDetect": RegistryValue(True, 1, 4)}
    registry = _MemoryRegistry(original)
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(snapshot_path=snapshot, registry=registry, notifier=lambda: True, windows=True)

    assert manager.apply_pac("http://127.0.0.1:18777/proxy.pac")[0]
    ok, _message = manager.restore()

    assert ok
    assert registry.values == original


def test_system_proxy_apply_refuses_non_loopback_url_without_snapshot_or_registry_change(tmp_path):
    registry = _MemoryRegistry(_INITIAL_PROXY)
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(snapshot_path=snapshot, registry=registry, notifier=lambda: True, windows=True)

    ok, message = manager.apply_pac("https://outside.example/proxy.pac")

    assert not ok
    assert "loopback" in message
    assert not snapshot.exists()
    assert registry.values == _INITIAL_PROXY


def test_system_proxy_refuses_loopback_looking_url_that_targets_another_host(tmp_path):
    registry = _MemoryRegistry(_INITIAL_PROXY)
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(snapshot_path=snapshot, registry=registry, notifier=lambda: True, windows=True)

    ok, _message = manager.apply_pac("http://127.0.0.1:18777@outside.example/proxy.pac")

    assert not ok
    assert registry.values == _INITIAL_PROXY
    assert not snapshot.exists()


def test_system_proxy_rolls_back_partial_pac_write_before_reporting_failure(tmp_path):
    registry = _MemoryRegistry(_INITIAL_PROXY, fail_write="AutoDetect", fail_once=True)
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(snapshot_path=snapshot, registry=registry, notifier=lambda: True, windows=True)

    ok, _message = manager.apply_pac("http://127.0.0.1:18777/proxy.pac")

    assert not ok
    assert registry.values == _INITIAL_PROXY
    assert not snapshot.exists()


def test_system_proxy_keeps_snapshot_when_restore_cannot_complete(tmp_path):
    registry = _MemoryRegistry(_INITIAL_PROXY)
    snapshot = tmp_path / "proxy_snapshot.json"
    manager = SystemProxyManager(snapshot_path=snapshot, registry=registry, notifier=lambda: True, windows=True)
    assert manager.apply_pac("http://127.0.0.1:18777/proxy.pac")[0]
    registry.fail_write = "ProxyServer"

    ok, _message = manager.restore()

    assert not ok
    assert snapshot.exists(), "снимок нужен watchdog для следующей попытки восстановления"


@dataclass
class _ProxySpy:
    apply_ok: bool = True
    applied_url: str = ""
    restore_calls: int = 0

    def apply_pac(self, pac_url: str):
        self.applied_url = pac_url
        return (True, "snapshot saved") if self.apply_ok else (False, "PAC write failed")

    def restore(self):
        self.restore_calls += 1
        return True, "restored"

    def status(self):
        return {"snapshot_exists": bool(self.applied_url), "current_pac_url": self.applied_url}


@dataclass
class _ControllerPolicySpy:
    calls: list[dict] | None = None

    def set_policy(self, **kwargs):
        if self.calls is None:
            self.calls = []
        self.calls.append(kwargs)


def _healthy_controller(profiles, **kwargs):
    return TransportController(
        profiles,
        probe=lambda _route: RouteProbeResult(ok=True, stage="ready", exit_country="NL", service_tls_ok=True),
        **kwargs,
    )


def _runtime_config() -> dict:
    return {
        "transport_failover_enabled": True,
        "transport_failover_mode": "recommended",
        "transport_failure_reaction": "balanced",
        "transport_gateway_port": 18777,
        "transport_profiles": [
            {"id": "eu", "name": "EU", "protocol": "http", "host": "proxy.example", "port": 8080,
             "enabled": True, "priority": 10, "country_hint": "NL", "username": "", "password": ""},
        ],
    }


def test_runtime_passes_user_owned_probe_url_to_new_controller():
    seen: dict[str, object] = {}

    def factory(profiles, **kwargs):
        seen.update(kwargs)
        return _healthy_controller(profiles, **kwargs)

    config = _runtime_config()
    config["transport_probe_url"] = "https://status.example.net/health"
    runtime = TransportRuntime(
        config,
        proxy_manager=_ProxySpy(),
        controller_factory=factory,
    )

    result = runtime.start(["chatgpt.com"])
    try:
        assert result.ok
        assert seen["probe_url"] == "https://status.example.net/health"
    finally:
        runtime.stop()


def test_runtime_reconfigure_updates_live_controller_policy_without_replacing_session():
    runtime = TransportRuntime(_runtime_config(), proxy_manager=_ProxySpy())
    controller = _ControllerPolicySpy()
    runtime._controller = controller
    replacement = _runtime_config()
    replacement.update({
        "transport_failover_enabled": False,
        "transport_failover_mode": "economy",
        "transport_failure_reaction": "fast",
    })

    runtime.reconfigure(replacement)

    assert runtime._config is replacement
    assert controller.calls == [{
        "failover_enabled": False,
        "failover_mode": "economy",
        "failure_reaction": "fast",
    }]


def test_watchdog_armed_for_proxy_restores_proxy_but_never_touches_dns_on_parent_crash():
    session = watchdog.wait_for_parent_session(io.BytesIO(b"HELLO\nARM_PROXY\n"), sleep_fn=lambda _seconds: None)
    calls: list[str] = []

    action = watchdog.handle_session(
        session,
        restore_dns_fn=lambda: calls.append("dns") or True,
        restore_proxy_fn=lambda: calls.append("proxy") or True,
        admin_fn=lambda: True,
    )

    assert session.signal == ""
    assert session.restore_dns is False and session.restore_proxy is True
    assert action == "restore"
    assert calls == ["proxy"]


def test_explicit_proxy_restore_signal_overrides_legacy_dns_default():
    session = watchdog.wait_for_parent_session(
        io.BytesIO(b"HELLO\nRESTORE_PROXY\n"), sleep_fn=lambda _seconds: None)
    calls: list[str] = []

    watchdog.handle_session(
        session,
        restore_dns_fn=lambda: calls.append("dns") or True,
        restore_proxy_fn=lambda: calls.append("proxy") or True,
        admin_fn=lambda: True,
    )

    assert session.signal == watchdog.SIGNAL_RESTORE_PROXY
    assert calls == ["proxy"]


def test_runtime_applies_pac_only_after_gateway_has_started_and_restores_on_stop():
    proxy = _ProxySpy()
    runtime = TransportRuntime(_runtime_config(), proxy_manager=proxy, controller_factory=_healthy_controller)

    result = runtime.start(["chatgpt.com"])

    assert result.ok
    assert result.pac_url.startswith("http://127.0.0.1:18777/proxy.pac")
    assert proxy.applied_url == result.pac_url
    snapshot = runtime.snapshot()
    assert snapshot["running"] is True
    assert snapshot["gateway"]["running"] is True
    assert snapshot["controller"]["active_id"] == "eu"

    ok, _message = runtime.stop()

    assert ok
    assert proxy.restore_calls == 1
    assert runtime.snapshot()["running"] is False


def test_runtime_rejects_empty_profiles_before_pac_can_change():
    proxy = _ProxySpy()
    config = _runtime_config()
    config["transport_profiles"] = []
    runtime = TransportRuntime(config, proxy_manager=proxy, controller_factory=_healthy_controller)

    result = runtime.start(["chatgpt.com"])

    assert not result.ok
    assert "не добавлен" in result.message.lower()
    assert proxy.applied_url == ""


def test_runtime_rolls_back_gateway_and_controller_when_pac_write_fails():
    proxy = _ProxySpy(apply_ok=False)
    runtime = TransportRuntime(_runtime_config(), proxy_manager=proxy, controller_factory=_healthy_controller)

    result = runtime.start(["chatgpt.com"])

    assert not result.ok
    assert proxy.restore_calls == 1
    assert runtime.snapshot()["running"] is False
    assert runtime.snapshot()["gateway"] is None
    assert runtime.snapshot()["controller"] is None


def test_manual_failed_start_offers_verified_reserve_without_committing_pac():
    config = _runtime_config()
    config["transport_primary_profile_id"] = "manual"
    config["transport_profiles"] = [
        {"id": "manual", "name": "Manual", "protocol": "http", "host": "manual.example", "port": 8080,
         "enabled": True, "priority": 10, "country_hint": "NL", "username": "", "password": ""},
        {"id": "reserve", "name": "Reserve", "protocol": "socks5", "host": "reserve.example", "port": 1080,
         "enabled": True, "priority": 20, "country_hint": "DE", "username": "", "password": ""},
    ]
    proxy = _ProxySpy()

    def mixed_controller(profiles, **kwargs):
        return TransportController(
            profiles,
            probe=lambda route: RouteProbeResult(
                ok=route.id == "reserve",
                stage="ready" if route.id == "reserve" else "connect",
                exit_country="DE" if route.id == "reserve" else "",
                service_tls_ok=route.id == "reserve",
            ),
            **kwargs,
        )

    runtime = TransportRuntime(config, proxy_manager=proxy, controller_factory=mixed_controller)
    result = runtime.start(["chatgpt.com"])

    assert not result.ok
    assert result.reserve_choice_required is True
    assert result.failed_profile_id == "manual"
    assert result.failed_profile_name == "Manual"
    assert result.suggested_profile_id == "reserve"
    assert result.suggested_profile_name == "Reserve"
    assert proxy.applied_url == "", "the UI must choose before PAC becomes active"
    assert runtime.snapshot()["running"] is False
    assert runtime.snapshot()["controller"] is None
    states = {route["id"]: route["state"] for route in runtime.snapshot()["server_statuses"]["routes"]}
    assert states == {"manual": "failed", "reserve": "active"}


def test_auto_start_selects_verified_reserve_without_manual_confirmation():
    config = _runtime_config()
    config["transport_profiles"] = [
        {"id": "first", "name": "First", "protocol": "http", "host": "first.example", "port": 8080,
         "enabled": True, "priority": 10, "country_hint": "NL", "username": "", "password": ""},
        {"id": "reserve", "name": "Reserve", "protocol": "socks5", "host": "reserve.example", "port": 1080,
         "enabled": True, "priority": 20, "country_hint": "DE", "username": "", "password": ""},
    ]

    def mixed_controller(profiles, **kwargs):
        return TransportController(
            profiles,
            probe=lambda route: RouteProbeResult(
                ok=route.id == "reserve",
                stage="ready" if route.id == "reserve" else "connect",
                exit_country="DE" if route.id == "reserve" else "",
                service_tls_ok=route.id == "reserve",
            ),
            **kwargs,
        )

    runtime = TransportRuntime(config, proxy_manager=_ProxySpy(), controller_factory=mixed_controller)
    try:
        result = runtime.start(["chatgpt.com"])
        assert result.ok
        assert result.reserve_choice_required is False
        assert "Reserve" in result.message
        assert runtime.snapshot()["controller"]["active_id"] == "reserve"
    finally:
        runtime.stop()
