"""Managed built-in VPN providers owned exclusively by :mod:`core.vpn`."""

from .base import (
    BuiltinProviderAdapter,
    ProviderDescriptor,
    ProviderStartResult,
    ProviderState,
    ProviderStatus,
)
from .catalog import BuiltinProviderCatalog, CloudflareWarpProvider, OperaProvider

__all__ = [
    "BuiltinProviderAdapter",
    "BuiltinProviderCatalog",
    "CloudflareWarpProvider",
    "OperaProvider",
    "ProviderDescriptor",
    "ProviderStartResult",
    "ProviderState",
    "ProviderStatus",
]
