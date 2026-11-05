"""Protocol-neutral data contracts for UmbraNet VPN transports.

This module deliberately distinguishes an advertised protocol name from a
working implementation.  A protocol becomes selectable only when its adapter
can create the declared egress type and return it to the routing owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..profiles import RouteProfile


class ProtocolId(str, Enum):
    """Stable protocol identifiers accepted by the VPN adapter registry."""

    HTTP_CONNECT = "http"
    SOCKS5 = "socks5"
    WIREGUARD = "wireguard"
    AMNEZIAWG = "amneziawg"
    SHADOWSOCKS = "shadowsocks"
    VMESS = "vmess"
    VLESS = "vless"
    TROJAN = "trojan"
    HYSTERIA2 = "hysteria2"
    TUIC = "tuic"


class EgressKind(str, Enum):
    """How an adapter hands a ready route to UmbraNet.

    The current routing stack safely accepts only ``LOOPBACK_PROXY``.  A TUN
    interface is intentionally represented separately: adding it to PAC would
    silently create device-wide routing, which is not a substitute for the
    future selective native-app interception layer.
    """

    LOOPBACK_PROXY = "loopback_proxy"
    TUNNEL_INTERFACE = "tunnel_interface"


class ProtocolState(str, Enum):
    """Conservative readiness, never an optimistic protocol-support claim."""

    AVAILABLE = "available"
    BACKEND_REQUIRED = "backend_required"
    INTEGRATION_REQUIRED = "integration_required"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProtocolDescriptor:
    """Non-secret protocol metadata owned by one concrete adapter."""

    protocol: ProtocolId
    display_name: str
    egress_kind: EgressKind
    backend_name: str
    configuration_formats: tuple[str, ...]
    description: str


@dataclass(frozen=True, slots=True)
class ProtocolStatus:
    """Safe, user-facing adapter readiness without config or endpoint data."""

    descriptor: ProtocolDescriptor
    state: ProtocolState
    message: str

    @property
    def selectable(self) -> bool:
        return self.state is ProtocolState.AVAILABLE

    def public_mapping(self) -> dict[str, object]:
        return {
            "protocol": self.descriptor.protocol.value,
            "display_name": self.descriptor.display_name,
            "egress_kind": self.descriptor.egress_kind.value,
            "backend_name": self.descriptor.backend_name,
            "configuration_formats": list(self.descriptor.configuration_formats),
            "description": self.descriptor.description,
            "state": self.state.value,
            "message": self.message,
            "selectable": self.selectable,
        }


@dataclass(frozen=True, slots=True)
class ProtocolStartRequest:
    """Opaque adapter input kept inside VPN core.

    ``configuration`` may contain credentials/keys and is never copied to a
    status, log mapping, or UI result.  Parsers and backends validate it before
    using it; callers never infer a working protocol from successful parsing.
    """

    profile_id: str
    configuration: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ProtocolStartResult:
    """One prepared protocol route plus exact ownership of its cleanup."""

    ok: bool
    message: str
    egress_kind: EgressKind
    loopback_route: RouteProfile | None = None
    stop: Callable[[], None] | None = None

    def close(self) -> None:
        if self.stop is not None:
            self.stop()
            self.stop = None
