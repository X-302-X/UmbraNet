"""Transactional transport session: health controller, gateway, and Windows PAC."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Callable, Iterable

from .controller import TransportController
from .gateway import LocalTransportGateway
from .profiles import RouteProfile, profiles_from_config
from .protocols import ProtocolRegistry, default_protocol_registry
from .providers import BuiltinProviderCatalog
from .system_proxy import SystemProxyManager

log = logging.getLogger("UmbraNet.TransportRuntime")


@dataclass(frozen=True, slots=True)
class TransportStartResult:
    ok: bool
    message: str
    pac_url: str = ""
    # A manual primary that failed its real readiness probe is not silently
    # replaced.  The core supplies only a verified reserve suggestion; the UI
    # owns the explicit person's decision and may start a new session with it.
    reserve_choice_required: bool = False
    failed_profile_id: str = ""
    failed_profile_name: str = ""
    suggested_profile_id: str = ""
    suggested_profile_name: str = ""


class TransportRuntime:
    """Owns the resources that make selected VPN routes browser-visible.

    Start is transactional: a failed PAC write stops the local gateway and
    controller, so no orphan loopback listener remains.  Stop restores the
    prior user proxy settings before closing the gateway.
    """

    def __init__(self, config: dict, *, proxy_manager: SystemProxyManager | None = None,
                 controller_factory: Callable[..., TransportController] = TransportController,
                 gateway_factory: Callable[..., LocalTransportGateway] = LocalTransportGateway,
                 provider_catalog: BuiltinProviderCatalog | None = None,
                 protocol_registry: ProtocolRegistry | None = None,
                 status_listener: Callable[[], None] | None = None):
        self._config = config
        self._proxy = proxy_manager or SystemProxyManager()
        self._controller_factory = controller_factory
        self._gateway_factory = gateway_factory
        # The catalogue belongs to VPN core, not Routing/UI. At this stage it
        # publishes only conservative readiness information; a provider cannot
        # be selected until its adapter returns a real loopback route.
        self._provider_catalog = provider_catalog or BuiltinProviderCatalog()
        # Protocol capability is owned by VPN core independently from provider
        # catalogues.  The Routing UI does not render it until a protocol has a
        # real adapter and setup flow.
        self._protocol_registry = protocol_registry or default_protocol_registry()
        self._status_listener = status_listener
        self._lock = threading.RLock()
        self._controller: TransportController | None = None
        self._gateway: LocalTransportGateway | None = None
        self._running = False
        self._last_error = ""
        # A finished or cancelled readiness pass must remain visible to both
        # server surfaces.  Keep only the controller's already-sanitised public
        # state; profile endpoints, credentials and probe URLs never enter it.
        self._last_server_controller: dict[str, object] | None = None
        self._last_server_signature: tuple[tuple[object, ...], ...] = ()

    def set_status_listener(self, listener: Callable[[], None] | None) -> None:
        """Attach the payload-free route-health observer to a live controller.

        Factories used by integrations/tests may not accept a new constructor
        keyword.  Attaching through this optional capability keeps those
        factories compatible and preserves the core-to-UI boundary.
        """
        with self._lock:
            self._status_listener = listener
            controller = self._controller
        self._attach_status_listener(controller)

    def _attach_status_listener(self, controller: object | None) -> None:
        setter = getattr(controller, "set_status_listener", None)
        if callable(setter):
            try:
                setter(self._status_listener)
            except Exception:
                log.debug("Could not attach transport status listener", exc_info=True)

    @staticmethod
    def _profile_signature(profiles: Iterable[RouteProfile]) -> tuple[tuple[object, ...], ...]:
        """Stable non-secret identity for deciding whether cached health fits config."""
        return tuple(sorted(
            (
                profile.id,
                profile.name,
                profile.protocol,
                profile.host,
                profile.port,
                profile.enabled,
                profile.priority,
                profile.country_hint,
            )
            for profile in profiles
        ))

    def _remember_server_state(self, controller: TransportController) -> None:
        try:
            snapshot = controller.snapshot()
        except Exception as exc:  # status diagnostics must not mask cleanup
            log.debug("Could not capture VPN server state: %s", exc)
            return
        if isinstance(snapshot, dict):
            self._last_server_controller = snapshot
            routes = snapshot.get("routes", [])
            if isinstance(routes, list):
                self._last_server_signature = tuple(sorted(
                    (
                        str(route.get("id", "")),
                        str(route.get("name", "")),
                        str(route.get("protocol", "")),
                        str(route.get("host", "")),
                        route.get("port"),
                        bool(route.get("enabled", False)),
                        route.get("priority"),
                        str(route.get("country_hint", "")),
                    )
                    for route in routes if isinstance(route, dict)
                ))

    @staticmethod
    def _validated_port(raw: object) -> int:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            return 18777
        return port if 1024 <= port <= 65535 else 18777

    @staticmethod
    def _selected_domains(domains: Iterable[object]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in domains:
            value = str(raw or "").strip().lower().rstrip(".")
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def start(self, domains: Iterable[object]) -> TransportStartResult:
        """Prepare exits, start gateway, then atomically select PAC in Windows."""
        with self._lock:
            if self._running:
                gateway = self._gateway
                return TransportStartResult(True, "транспорт уже активен", gateway.pac_url if gateway else "")

            profiles: list[RouteProfile] = [profile for profile in profiles_from_config(self._config) if profile.enabled]
            if not profiles:
                return TransportStartResult(False, "Не добавлен ни один включённый VPN-сервер")
            selected_domains = self._selected_domains(domains)
            if not selected_domains:
                return TransportStartResult(False, "Не выбраны домены для VPN-маршрута")

            controller = self._controller_factory(
                profiles,
                failover_enabled=bool(self._config.get("transport_failover_enabled", True)),
                failover_mode=str(self._config.get("transport_failover_mode", "recommended")),
                failure_reaction=str(self._config.get("transport_failure_reaction", "balanced")),
                preferred_profile_id=str(self._config.get("transport_primary_profile_id", "") or ""),
                probe_url=str(self._config.get("transport_probe_url", "") or ""),
            )
            self._attach_status_listener(controller)
            gateway: LocalTransportGateway | None = None
            try:
                controller.start()
                replacement = getattr(controller, "startup_replacement_offer", lambda: None)()
                if replacement is not None:
                    # Do not commit PAC/gateway state and do not let the
                    # controller's normal failover become a hidden replacement
                    # for a manual initial selection.  The caller must ask.
                    self._remember_server_state(controller)
                    controller.stop()
                    failed_id = str(self._config.get("transport_primary_profile_id", "") or "")
                    failed_name = failed_id
                    state = self._last_server_controller or {}
                    routes = state.get("routes", []) if isinstance(state, dict) else []
                    for route in routes if isinstance(routes, list) else []:
                        if isinstance(route, dict) and route.get("id") == failed_id:
                            failed_name = str(route.get("name") or failed_id)
                            break
                    return TransportStartResult(
                        False,
                        f"Выбранный VPN-сервер «{failed_name}» не прошёл проверку; доступен проверенный резерв",
                        reserve_choice_required=True,
                        failed_profile_id=failed_id,
                        failed_profile_name=failed_name,
                        suggested_profile_id=replacement.id,
                        suggested_profile_name=replacement.name,
                    )
                gateway = self._gateway_factory(
                    controller,
                    selected_domains,
                    port=self._validated_port(self._config.get("transport_gateway_port", 18777)),
                )
                gateway.start()
                ok, message = self._proxy.apply_pac(gateway.pac_url)
                if not ok:
                    # SystemProxyManager already rolls back partial writes; call
                    # restore defensively for an injected/native implementation
                    # that reported failure after changing a setting.
                    try:
                        self._proxy.restore()
                    except Exception:
                        pass
                    gateway.stop()
                    self._remember_server_state(controller)
                    controller.stop()
                    self._last_error = message
                    return TransportStartResult(False, message)
            except Exception as exc:
                try:
                    if gateway is not None:
                        gateway.stop()
                except Exception:
                    pass
                self._remember_server_state(controller)
                controller.stop()
                self._last_error = str(exc)
                return TransportStartResult(False, str(exc))

            self._controller = controller
            self._gateway = gateway
            self._running = True
            self._last_error = ""
            active = controller.active_profile()
            status = "готовый сервер пока не найден: выбранные VPN-сервисы будут заблокированы до восстановления"
            if not controller.snapshot().get("probe_configured"):
                status = "не задан HTTPS endpoint проверки: выбранные VPN-сервисы будут заблокированы"
            elif active is not None:
                status = f"активный сервер: {active.name}"
            log.info("Transport runtime started (%s, %s)", gateway.pac_url, status)
            return TransportStartResult(True, status, gateway.pac_url)

    def reconfigure(self, config: dict) -> None:
        """Adopt saved settings without replacing a live PAC/gateway session.

        Gateway domains and proxy credentials deliberately stay frozen until an
        explicit Restart: changing either under established browser connections
        must not create a half-old session. Failover policy is safe to apply
        immediately because the controller selects the route for each new
        tunnel and never introduces a direct fallback.
        """
        if not isinstance(config, dict):
            return
        with self._lock:
            self._config = config
            controller = self._controller
            if controller is None and self._last_server_controller is not None:
                if self._profile_signature(profiles_from_config(config)) != self._last_server_signature:
                    # A card was edited/deleted while no session owned it; an
                    # old failed/ready result would no longer be truthful.
                    self._last_server_controller = None
                    self._last_server_signature = ()
        if controller is not None:
            controller.set_policy(
                failover_enabled=bool(config.get("transport_failover_enabled", True)),
                failover_mode=str(config.get("transport_failover_mode", "recommended")),
                failure_reaction=str(config.get("transport_failure_reaction", "balanced")),
            )

    def stop(self) -> tuple[bool, str]:
        """Restore user proxy settings, then release only UmbraNet transport resources."""
        with self._lock:
            gateway = self._gateway
            controller = self._controller
            was_running = self._running
            self._gateway = None
            self._controller = None
            self._running = False

        restore_ok, restore_message = self._proxy.restore()
        if gateway is not None:
            gateway.stop()
        if controller is not None:
            self._remember_server_state(controller)
            controller.stop()
        if not was_running and restore_ok:
            return True, "транспорт не был запущен"
        if not restore_ok:
            self._last_error = restore_message
            return False, restore_message
        self._last_error = ""
        log.info("Transport runtime stopped: %s", restore_message)
        return True, restore_message

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            controller = self._controller
            gateway = self._gateway
            return {
                "running": self._running,
                "last_error": self._last_error,
                "controller": controller.snapshot() if controller is not None else None,
                # Unlike ``controller``, this is intentionally retained after
                # stop/cancel solely for honest server readiness rendering.
                "server_statuses": controller.snapshot() if controller is not None else self._last_server_controller,
                "gateway": gateway.snapshot() if gateway is not None else None,
                "proxy": self._proxy.status(),
                "built_in_providers": self._provider_catalog.public_snapshot(),
                "protocols": self._protocol_registry.public_snapshot(),
            }
