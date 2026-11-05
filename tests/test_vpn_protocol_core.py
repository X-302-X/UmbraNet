"""Protocol-engine foundation contracts: capability is not implementation."""

from __future__ import annotations

import pytest

from vpn.protocols import (
    EgressKind,
    ProtocolDescriptor,
    ProtocolId,
    ProtocolRegistry,
    ProtocolStartRequest,
    ProtocolState,
    default_protocol_registry,
)
from vpn.protocols.catalog import ExistingProxyAdapter
from vpn.runtime import TransportRuntime


class _ProxyStatusOnly:
    def status(self):
        return {"snapshot_exists": False, "current_pac_url": ""}


def test_default_protocol_registry_lists_distinct_real_and_future_boundaries():
    snapshot = default_protocol_registry().public_snapshot()
    by_protocol = {item["protocol"]: item for item in snapshot}

    assert by_protocol["http"]["state"] == ProtocolState.AVAILABLE.value
    assert by_protocol["socks5"]["state"] == ProtocolState.AVAILABLE.value
    assert by_protocol["amneziawg"] == {
        "protocol": "amneziawg",
        "display_name": "AmneziaWG",
        "egress_kind": EgressKind.TUNNEL_INTERFACE.value,
        "backend_name": "официальный AmneziaWG for Windows",
        "configuration_formats": ["AmneziaWG .conf"],
        "description": "Нативный туннель с отдельным Windows service/driver lifecycle.",
        "state": ProtocolState.INTEGRATION_REQUIRED.value,
        "message": "Нужен нативный adapter и selective native-app routing; текущий PAC gateway не подменяет TUN-интерфейс.",
        "selectable": False,
    }
    for protocol in ("shadowsocks", "vmess", "vless", "trojan", "hysteria2", "tuic"):
        assert by_protocol[protocol]["state"] == ProtocolState.BACKEND_REQUIRED.value
        assert by_protocol[protocol]["egress_kind"] == EgressKind.LOOPBACK_PROXY.value
        assert by_protocol[protocol]["selectable"] is False


def test_nonintegrated_protocol_refuses_without_reading_or_exposing_secret_config():
    registry = default_protocol_registry()
    adapter = registry.adapter("vless")
    assert adapter is not None

    result = adapter.start(ProtocolStartRequest("private", {
        "uuid": "secret-uuid",
        "password": "secret-password",
        "server": "private.example",
    }))

    assert result.ok is False
    assert result.loopback_route is None
    assert "secret" not in result.message.lower()
    public = adapter.inspect().public_mapping()
    assert "server" not in public and "password" not in public and "uuid" not in public


def test_tunnel_interface_adapter_can_never_be_mistaken_for_current_loopback_proxy_path():
    registry = default_protocol_registry()
    for protocol in (ProtocolId.WIREGUARD, ProtocolId.AMNEZIAWG):
        adapter = registry.adapter(protocol)
        assert adapter is not None
        assert adapter.descriptor.egress_kind is EgressKind.TUNNEL_INTERFACE
        result = adapter.start(ProtocolStartRequest("profile", {"private_key": "secret"}))
        assert result.ok is False
        assert result.egress_kind is EgressKind.TUNNEL_INTERFACE
        assert result.loopback_route is None


def test_registry_rejects_duplicate_protocol_ownership_and_unknown_lookup_is_safe():
    first = ExistingProxyAdapter(ProtocolId.HTTP_CONNECT, "First")
    second = ExistingProxyAdapter(ProtocolId.HTTP_CONNECT, "Second")

    with pytest.raises(ValueError, match="Повторяющийся"):
        ProtocolRegistry((first, second))

    registry = ProtocolRegistry((first,))
    assert registry.adapter("not-a-vpn-protocol") is None


def test_runtime_exposes_safe_protocol_capabilities_before_any_ui_is_added():
    runtime = TransportRuntime({}, proxy_manager=_ProxyStatusOnly())

    snapshot = runtime.snapshot()
    protocols = {item["protocol"]: item for item in snapshot["protocols"]}

    assert protocols["socks5"]["selectable"] is True
    assert protocols["amneziawg"]["selectable"] is False
    assert protocols["vless"]["state"] == ProtocolState.BACKEND_REQUIRED.value
    assert "host" not in protocols["vless"] and "port" not in protocols["vless"]


def test_protocol_descriptor_is_static_and_contains_no_profile_material():
    descriptor = ProtocolDescriptor(
        protocol=ProtocolId.SOCKS5,
        display_name="SOCKS5",
        egress_kind=EgressKind.LOOPBACK_PROXY,
        backend_name="UmbraNet transport gateway",
        configuration_formats=("UmbraNet JSON profile",),
        description="test",
    )

    assert descriptor.protocol.value == "socks5"
    assert descriptor.configuration_formats == ("UmbraNet JSON profile",)
