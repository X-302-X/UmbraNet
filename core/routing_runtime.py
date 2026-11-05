"""Explicit VPN / Combo / DPI session owner, independent from local DNS."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Callable, Iterable

try:
    from core.dpi.runtime import DpiRuntime
    from core.vpn.runtime import TransportRuntime
    from core.service_profiles import UI_SERVICE_PROFILES, service_bypass
except ImportError:  # Core also supports its historical flat import layout.
    from dpi.runtime import DpiRuntime
    from vpn.runtime import TransportRuntime
    from service_profiles import UI_SERVICE_PROFILES, service_bypass

log = logging.getLogger("UmbraNet.RoutingRuntime")

ROUTING_MODES = ("vpn", "combo", "dpi")


@dataclass(frozen=True, slots=True)
class RoutingTargets:
    vpn_domains: tuple[str, ...]
    dpi_domains: tuple[str, ...]
    selected_services: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingStartResult:
    ok: bool
    message: str
    vpn_active: bool = False
    dpi_active: bool = False
    reserve_choice_required: bool = False
    failed_profile_id: str = ""
    failed_profile_name: str = ""
    suggested_profile_id: str = ""
    suggested_profile_name: str = ""


def _domains(raw: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip().lower().rstrip(".")
        if value and "." in value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _selected_service_names(config: dict, routed: set[str]) -> list[str]:
    explicit = config.get("selected_services", [])
    names = [str(value) for value in explicit] if isinstance(explicit, list) else []
    valid = [name for name in names if name in UI_SERVICE_PROFILES]
    if valid:
        return list(dict.fromkeys(valid))
    # Pre-v6 config only has a flat routed_domains list.  Derive full selected
    # cards, but never infer a partial card: its user-added remainder is VPN.
    return [
        name for name, profile in UI_SERVICE_PROFILES.items()
        if (domains := _domains(profile.get("runtime_domains", []))) and set(domains).issubset(routed)
    ]


def split_routing_targets(config: dict, mode: str | None = None) -> RoutingTargets:
    """Split selected cards into PAC and DPI hostlists with PAC precedence."""
    selected_mode = str(mode or config.get("routing_mode", "vpn")).strip().lower()
    if selected_mode not in ROUTING_MODES:
        selected_mode = "vpn"
    configured_domains = _domains(config.get("routed_domains", []))
    subscription_domains = _domains(config.get("subscribed_domains_set", []))
    routed = list(configured_domains)
    routed.extend(domain for domain in subscription_domains if domain not in routed)
    routed_set = set(routed)
    services = _selected_service_names(config, routed_set)
    service_domains: set[str] = set()
    vpn: list[str] = []
    dpi: list[str] = []

    for name in services:
        domains = _domains(UI_SERVICE_PROFILES[name].get("runtime_domains", []))
        service_domains.update(domains)
        if service_bypass(name) == "dpi":
            dpi.extend(domains)
        else:
            vpn.extend(domains)

    # A manual dispatcher entry follows the global pure mode, never a per-row
    # selector. Combo's manual-domain policy is intentionally deferred until
    # its product rules are specified; only known service cards participate in
    # Combo for now. This avoids silently guessing VPN or DPI for an unknown
    # target and never creates a DIRECT path.
    manual = [domain for domain in routed if domain not in service_domains]
    if selected_mode == "vpn":
        vpn.extend(manual)
    if selected_mode == "dpi":
        dpi = list(routed)
        vpn = []
    elif selected_mode == "vpn":
        vpn = list(routed)
        dpi = []

    vpn_unique = _domains(vpn)
    vpn_set = set(vpn_unique)
    dpi_unique = [domain for domain in _domains(dpi) if domain not in vpn_set]
    return RoutingTargets(tuple(vpn_unique), tuple(dpi_unique), tuple(services))


class RoutingRuntime:
    """Coordinates real VPN/PAC and DPI resources for one selected top-level mode."""

    def __init__(self, config: dict, *, transport_runtime: TransportRuntime | None = None,
                 dpi_runtime: DpiRuntime | None = None,
                 transport_status_listener: Callable[[], None] | None = None):
        self._config = config
        self._transport = transport_runtime or TransportRuntime(config)
        self._dpi = dpi_runtime or DpiRuntime(config)
        self._lock = threading.RLock()
        self.set_transport_status_listener(transport_status_listener)
        self._running = False
        self._vpn_started = False
        self._dpi_started = False
        self._last_error = ""
        self._targets = RoutingTargets((), (), ())

    def set_transport_status_listener(self, listener: Callable[[], None] | None) -> None:
        """Optionally forward route-health changes without importing UI code."""
        setter = getattr(self._transport, "set_status_listener", None)
        if callable(setter):
            try:
                setter(listener)
            except Exception:
                log.debug("Could not configure transport status listener", exc_info=True)

    @property
    def mode(self) -> str:
        value = str(self._config.get("routing_mode", "vpn")).strip().lower()
        return value if value in ROUTING_MODES else "vpn"

    def reconfigure(self, config: dict) -> None:
        """Keep the active owner across config reloads without orphaning PAC.

        An engine reload replaces its config dictionary. The old behaviour
        constructed a second runtime owner while the first still held the
        gateway and proxy snapshot. We retain the owner, update live-safe
        failover policy, and defer target/profile/strategy replacement until
        the user's explicit Restart.
        """
        if not isinstance(config, dict):
            return
        with self._lock:
            self._config = config
            transport = self._transport
            dpi = self._dpi
        for runtime in (transport, dpi):
            update = getattr(runtime, "reconfigure", None)
            if callable(update):
                update(config)

    def start(self) -> RoutingStartResult:
        with self._lock:
            if self._running:
                snapshot = self.snapshot()
                return RoutingStartResult(True, "маршрутизация уже активна",
                                          bool(snapshot["vpn"]["running"]), bool(snapshot["dpi"]["running"]))
            targets = split_routing_targets(self._config, self.mode)
            if not targets.vpn_domains and not targets.dpi_domains:
                return RoutingStartResult(False, "Не выбраны сервисы или домены для маршрутизации")

            vpn_active = False
            dpi_active = False
            if targets.vpn_domains:
                result = self._transport.start(targets.vpn_domains)
                if not result.ok:
                    self._last_error = result.message
                    return RoutingStartResult(
                        False,
                        result.message,
                        reserve_choice_required=bool(getattr(result, "reserve_choice_required", False)),
                        failed_profile_id=str(getattr(result, "failed_profile_id", "") or ""),
                        failed_profile_name=str(getattr(result, "failed_profile_name", "") or ""),
                        suggested_profile_id=str(getattr(result, "suggested_profile_id", "") or ""),
                        suggested_profile_name=str(getattr(result, "suggested_profile_name", "") or ""),
                    )
                vpn_active = True
                self._vpn_started = True
            if targets.dpi_domains:
                result = self._dpi.start(targets.dpi_domains)
                if not result.ok:
                    if vpn_active:
                        self._transport.stop()
                        self._vpn_started = False
                    self._last_error = result.message
                    return RoutingStartResult(False, result.message)
                dpi_active = True
                self._dpi_started = True

            self._targets = targets
            self._running = True
            self._last_error = ""
            parts = []
            if vpn_active:
                parts.append(f"VPN: {len(targets.vpn_domains)}")
            if dpi_active:
                parts.append(f"DPI: {len(targets.dpi_domains)}")
            message = " · ".join(parts) + " доменов"
            log.info("Routing runtime started (%s)", message)
            return RoutingStartResult(True, message, vpn_active, dpi_active)

    def stop(self) -> tuple[bool, str]:
        """PAC is restored before DPI shutdown, preserving the user's proxy first."""
        with self._lock:
            running = self._running
            dpi_started = self._dpi_started
            self._running = False
            self._vpn_started = False
            self._dpi_started = False
            self._targets = RoutingTargets((), (), ())
        errors: list[str] = []
        # Call transport.stop even after an interrupted previous process: it
        # makes Start/Stop a recovery point for a durable PAC snapshot.
        vpn_ok, vpn_message = self._transport.stop()
        if not vpn_ok:
            errors.append(vpn_message)
        if dpi_started:
            dpi_ok, dpi_message = self._dpi.stop()
            if not dpi_ok:
                errors.append(dpi_message)
        if errors:
            self._last_error = "; ".join(errors)
            return False, self._last_error
        self._last_error = ""
        return True, "маршрутизация остановлена" if running else "маршрутизация не была запущена"

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self._running,
            "mode": self.mode,
            "last_error": self._last_error,
            "targets": {
                "vpn": list(self._targets.vpn_domains),
                "dpi": list(self._targets.dpi_domains),
                "services": list(self._targets.selected_services),
            },
            "vpn": self._transport.snapshot(),
            "dpi": self._dpi.snapshot(),
        }
