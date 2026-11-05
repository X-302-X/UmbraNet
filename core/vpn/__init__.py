"""VPN/PAC runtime for UmbraNet.

The package is independent from the passive legacy DNS helpers. It owns saved
VPN exits, their health checks, loopback gateway, PAC generation and exact
system-proxy recovery. The VPN | Combo | DPI coordinator lives in
``core.routing_runtime`` because it also owns DPI.
"""

from .controller import TransportController
from .health import ProbeTarget, RouteProbeResult, probe_http_or_socks_route, probe_target_from_url
from .importers import ServerImportResult, import_custom_server_document, import_custom_server_file
from .providers import (
    BuiltinProviderAdapter,
    BuiltinProviderCatalog,
    CloudflareWarpProvider,
    OperaProvider,
    ProviderDescriptor,
    ProviderStartResult,
    ProviderState,
    ProviderStatus,
)
from .protocols import (
    EgressKind,
    ProtocolAdapter,
    ProtocolDescriptor,
    ProtocolId,
    ProtocolRegistry,
    ProtocolStartRequest,
    ProtocolStartResult,
    ProtocolState,
    ProtocolStatus,
    default_protocol_registry,
)
from .profiles import (
    FAILOVER_MODES,
    FAILURE_REACTIONS,
    MAX_ROUTE_PROFILES,
    NO_READY_POLICIES,
    ROUTE_PROTOCOLS,
    RouteProfile,
    normalize_probe_url,
    sanitize_route_profiles,
)
from .subscriptions import (
    SubscriptionFetchResult,
    fetch_transport_subscription,
    materialize_subscription_profiles,
    normalize_subscription_url,
    sanitize_transport_subscriptions,
    subscription_label,
)
from .runtime import TransportRuntime, TransportStartResult
from .system_proxy import SystemProxyManager, restore_proxy_snapshot

__all__ = [
    "ServerImportResult",
    "import_custom_server_document",
    "import_custom_server_file",
    "BuiltinProviderAdapter",
    "BuiltinProviderCatalog",
    "CloudflareWarpProvider",
    "OperaProvider",
    "ProviderDescriptor",
    "ProviderStartResult",
    "ProviderState",
    "ProviderStatus",
    "EgressKind",
    "ProtocolAdapter",
    "ProtocolDescriptor",
    "ProtocolId",
    "ProtocolRegistry",
    "ProtocolStartRequest",
    "ProtocolStartResult",
    "ProtocolState",
    "ProtocolStatus",
    "default_protocol_registry",
    "FAILOVER_MODES",
    "FAILURE_REACTIONS",
    "MAX_ROUTE_PROFILES",
    "NO_READY_POLICIES",
    "ROUTE_PROTOCOLS",
    "RouteProfile",
    "ProbeTarget",
    "RouteProbeResult",
    "SystemProxyManager",
    "TransportController",
    "TransportRuntime",
    "TransportStartResult",
    "normalize_probe_url",
    "probe_http_or_socks_route",
    "probe_target_from_url",
    "restore_proxy_snapshot",
    "sanitize_route_profiles",
    "SubscriptionFetchResult",
    "fetch_transport_subscription",
    "materialize_subscription_profiles",
    "normalize_subscription_url",
    "sanitize_transport_subscriptions",
    "subscription_label",
]
