"""Conservative protocol catalogue for the VPN engine foundation.

Entries here are not marketing presets.  The state of each entry tells the
runtime and future UI whether it can actually hand off a safe local route.
"""

from __future__ import annotations

from .base import ProtocolAdapter
from .models import (
    EgressKind,
    ProtocolDescriptor,
    ProtocolId,
    ProtocolStartRequest,
    ProtocolStartResult,
    ProtocolState,
    ProtocolStatus,
)
from .registry import ProtocolRegistry


class _StaticProtocolAdapter(ProtocolAdapter):
    """Base for a named capability whose backend is deliberately absent."""

    def __init__(self, descriptor: ProtocolDescriptor, state: ProtocolState, message: str):
        self._descriptor = descriptor
        self._state = state
        self._message = message

    @property
    def descriptor(self) -> ProtocolDescriptor:
        return self._descriptor

    def inspect(self) -> ProtocolStatus:
        return ProtocolStatus(self.descriptor, self._state, self._message)

    def start(self, request: ProtocolStartRequest) -> ProtocolStartResult:
        # ``request`` deliberately is not inspected: a non-integrated adapter
        # must not parse/store keys or pretend a config became usable.
        del request
        return ProtocolStartResult(False, self._message, self.descriptor.egress_kind)


class ExistingProxyAdapter(_StaticProtocolAdapter):
    """Describe the already implemented standard proxy paths.

    HTTP CONNECT/SOCKS5 continue to be started by ``TransportRuntime`` from
    validated RouteProfile objects.  They are marked available because this
    catalogue is a capability map, not an alternate lifecycle entry point.
    """

    def __init__(self, protocol: ProtocolId, display_name: str):
        super().__init__(
            ProtocolDescriptor(
                protocol=protocol,
                display_name=display_name,
                egress_kind=EgressKind.LOOPBACK_PROXY,
                backend_name="UmbraNet transport gateway",
                configuration_formats=("UmbraNet JSON profile",),
                description="Проверенный внешний proxy-маршрут через общий gateway.",
            ),
            ProtocolState.AVAILABLE,
            "Поддерживается текущим VPN transport runtime.",
        )


class AmneziaWgAdapter(_StaticProtocolAdapter):
    """Reserve the native AmneziaWG boundary without claiming PAC support."""

    def __init__(self):
        super().__init__(
            ProtocolDescriptor(
                protocol=ProtocolId.AMNEZIAWG,
                display_name="AmneziaWG",
                egress_kind=EgressKind.TUNNEL_INTERFACE,
                backend_name="официальный AmneziaWG for Windows",
                configuration_formats=("AmneziaWG .conf",),
                description="Нативный туннель с отдельным Windows service/driver lifecycle.",
            ),
            ProtocolState.INTEGRATION_REQUIRED,
            "Нужен нативный adapter и selective native-app routing; текущий PAC gateway не подменяет TUN-интерфейс.",
        )


class SingBoxProtocolAdapter(_StaticProtocolAdapter):
    """Describe a protocol that requires the reviewed sing-box backend layer."""

    def __init__(self, *, protocol: ProtocolId, display_name: str, formats: tuple[str, ...]):
        super().__init__(
            ProtocolDescriptor(
                protocol=protocol,
                display_name=display_name,
                egress_kind=EgressKind.LOOPBACK_PROXY,
                backend_name="sing-box backend",
                configuration_formats=formats,
                description="Требует отдельного проверенного backend-адаптера, который поднимет loopback proxy.",
            ),
            ProtocolState.BACKEND_REQUIRED,
            "Protocol объявлен в архитектуре, но sing-box backend ещё не поставлен и не интегрирован.",
        )


class WireGuardAdapter(_StaticProtocolAdapter):
    """Keep stock WireGuard separate from AmneziaWG and proxy protocols."""

    def __init__(self):
        super().__init__(
            ProtocolDescriptor(
                protocol=ProtocolId.WIREGUARD,
                display_name="WireGuard",
                egress_kind=EgressKind.TUNNEL_INTERFACE,
                backend_name="WireGuard native client or reviewed backend",
                configuration_formats=("WireGuard .conf",),
                description="Нативный TUN-маршрут; ему нужен отдельный selective-routing adapter.",
            ),
            ProtocolState.INTEGRATION_REQUIRED,
            "Нативный WireGuard adapter ещё не реализован; конфиг не будет импортирован как работающий сервер.",
        )


def default_protocol_registry() -> ProtocolRegistry:
    """Return the fixed protocol capability map for one UmbraNet build."""
    return ProtocolRegistry((
        ExistingProxyAdapter(ProtocolId.HTTP_CONNECT, "HTTP CONNECT"),
        ExistingProxyAdapter(ProtocolId.SOCKS5, "SOCKS5"),
        WireGuardAdapter(),
        AmneziaWgAdapter(),
        SingBoxProtocolAdapter(
            protocol=ProtocolId.SHADOWSOCKS,
            display_name="Shadowsocks",
            formats=("Shadowsocks URI", "sing-box JSON"),
        ),
        SingBoxProtocolAdapter(
            protocol=ProtocolId.VMESS,
            display_name="VMess",
            formats=("VMess URI", "sing-box JSON"),
        ),
        SingBoxProtocolAdapter(
            protocol=ProtocolId.VLESS,
            display_name="VLESS",
            formats=("VLESS URI", "sing-box JSON"),
        ),
        SingBoxProtocolAdapter(
            protocol=ProtocolId.TROJAN,
            display_name="Trojan",
            formats=("Trojan URI", "sing-box JSON"),
        ),
        SingBoxProtocolAdapter(
            protocol=ProtocolId.HYSTERIA2,
            display_name="Hysteria2",
            formats=("Hysteria2 URI", "sing-box JSON"),
        ),
        SingBoxProtocolAdapter(
            protocol=ProtocolId.TUIC,
            display_name="TUIC",
            formats=("TUIC URI", "sing-box JSON"),
        ),
    ))
