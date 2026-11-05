"""Safe lifecycle contract for UmbraNet's built-in VPN providers.

A provider is allowed to expose a loopback HTTP CONNECT or SOCKS5 route only
through this contract.  The generic VPN controller then subjects that route to
the same TLS/HTTPS health proof as every other route.  Provider code never
changes PAC settings directly and never decides whether direct traffic is OK.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ..profiles import RouteProfile


class ProviderState(str, Enum):
    """User-visible readiness state; values contain neither endpoints nor secrets."""

    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"
    CONFIGURATION_REQUIRED = "configuration_required"
    SECURITY_REVIEW = "security_review"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Stable metadata for a built-in route shown in Settings and Routing."""

    id: str
    name: str
    region_label: str
    description: str
    official: bool
    experimental: bool


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """A public provider status intentionally safe for logs, UI, and snapshots."""

    descriptor: ProviderDescriptor
    state: ProviderState
    message: str

    @property
    def selectable(self) -> bool:
        return self.state is ProviderState.AVAILABLE

    def public_mapping(self) -> dict[str, object]:
        return {
            "id": self.descriptor.id,
            "name": self.descriptor.name,
            "region_label": self.descriptor.region_label,
            "description": self.descriptor.description,
            "official": self.descriptor.official,
            "experimental": self.descriptor.experimental,
            "state": self.state.value,
            "message": self.message,
            "selectable": self.selectable,
        }


@dataclass(slots=True)
class ProviderStartResult:
    """Outcome of preparing one local provider route for the generic controller."""

    ok: bool
    message: str
    route: RouteProfile | None = None
    stop: Callable[[], None] | None = None

    def close(self) -> None:
        if self.stop is not None:
            self.stop()
            self.stop = None


class BuiltinProviderAdapter(ABC):
    """Boundary for a managed provider process or official local client.

    A successful ``start`` returns one *loopback* RouteProfile.  It must not
    return a remote endpoint, install a PAC, relax certificate checks, or
    provide a direct fallback.  The caller owns `close()` and tests the route
    through the normal VPN health controller before it can become active.
    """

    @property
    @abstractmethod
    def descriptor(self) -> ProviderDescriptor:
        """Return static, non-sensitive identity shown to the user."""

    @abstractmethod
    def inspect(self) -> ProviderStatus:
        """Return current readiness without starting a network session."""

    @abstractmethod
    def start(self) -> ProviderStartResult:
        """Prepare a local route or return a clear, non-destructive refusal."""
