"""Protocol-adapter foundation for the UmbraNet VPN engine."""

from .base import ProtocolAdapter
from .catalog import (
    AmneziaWgAdapter,
    ExistingProxyAdapter,
    SingBoxProtocolAdapter,
    WireGuardAdapter,
    default_protocol_registry,
)
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

__all__ = [
    "AmneziaWgAdapter",
    "EgressKind",
    "ExistingProxyAdapter",
    "ProtocolAdapter",
    "ProtocolDescriptor",
    "ProtocolId",
    "ProtocolRegistry",
    "ProtocolStartRequest",
    "ProtocolStartResult",
    "ProtocolState",
    "ProtocolStatus",
    "SingBoxProtocolAdapter",
    "WireGuardAdapter",
    "default_protocol_registry",
]
