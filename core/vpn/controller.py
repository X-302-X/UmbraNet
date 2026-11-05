"""Thread-safe active/warm-standby controller for outbound proxy routes."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Callable, Iterable

from .health import RouteProbeResult, probe_http_or_socks_route, probe_target_from_url
from .profiles import FAILOVER_MODES, FAILURE_REACTIONS, RouteProfile

log = logging.getLogger("UmbraNet.TransportController")

RouteProbe = Callable[[RouteProfile], RouteProbeResult]
Clock = Callable[[], float]

_ROUTE_STATES = frozenset({"stopped", "checking", "not_verified", "warm", "active", "degraded", "failed"})
_FAILURE_THRESHOLDS = {"cautious": 3, "balanced": 2, "fast": 1}


@dataclass(slots=True)
class _RouteRuntime:
    profile: RouteProfile
    state: str = "stopped"
    failures: int = 0
    last_probe: RouteProbeResult | None = None
    last_checked_at: float | None = None
    active_since: float | None = None

    def public_mapping(self) -> dict[str, object]:
        result = self.last_probe
        return {
            **self.profile.public_mapping(),
            "state": self.state,
            "failures": self.failures,
            "last_checked_at": self.last_checked_at,
            "active_since": self.active_since,
            "probe": result.public_mapping() if result else None,
        }


@dataclass(frozen=True, slots=True)
class SwitchEvent:
    at: float
    previous_id: str
    active_id: str
    reason: str

    def public_mapping(self, profiles: dict[str, _RouteRuntime]) -> dict[str, object]:
        previous = profiles.get(self.previous_id)
        active = profiles.get(self.active_id)
        return {
            "at": self.at,
            "previous_id": self.previous_id,
            "previous_name": previous.profile.name if previous else self.previous_id,
            "active_id": self.active_id,
            "active_name": active.profile.name if active else self.active_id,
            "reason": self.reason,
        }


class TransportController:
    """Maintains one active route and one or more pre-validated warm routes.

    This controller never returns a direct route.  The local proxy asks
    :meth:`active_profile`; if that returns ``None``, the proxy must deny the
    selected request rather than leaking it through the system connection.
    """

    def __init__(self, profiles: Iterable[RouteProfile] = (), *,
                 failover_enabled: bool = True,
                 failover_mode: str = "recommended",
                 failure_reaction: str = "balanced",
                 preferred_profile_id: str = "",
                 probe_url: str = "",
                 probe: RouteProbe | None = None,
                 clock: Clock | None = None,
                 history_limit: int = 40,
                 status_listener: Callable[[], None] | None = None):
        self._lock = threading.RLock()
        self._profiles: dict[str, _RouteRuntime] = {}
        self._active_id = ""
        self._events: list[SwitchEvent] = []
        self._history_limit = max(1, int(history_limit))
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        # An injected probe is used only by local tests/integrations. Production
        # routes must prove themselves against the configured user-owned URL.
        self._probe = probe or (lambda profile: probe_http_or_socks_route(profile, probe_url=probe_url))
        self._probe_configured = probe is not None or probe_target_from_url(probe_url) is not None
        self._clock = clock or time.monotonic
        self._failover_enabled = bool(failover_enabled)
        self._failover_mode = self._valid_mode(failover_mode, FAILOVER_MODES, "recommended")
        self._failure_reaction = self._valid_mode(failure_reaction, FAILURE_REACTIONS, "balanced")
        # The Routing page can choose a friendly primary profile. It is tried
        # first, while every other enabled profile remains a real failover
        # reserve and the no-DIRECT policy remains unchanged.
        self._preferred_profile_id = str(preferred_profile_id or "").strip()
        # Observers receive no route data: they are only asked to re-read the
        # already-sanitized public snapshot from their own boundary.
        self._status_listener = status_listener
        self.set_profiles(profiles)

    @staticmethod
    def _valid_mode(value: str, choices: tuple[str, ...], default: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in choices else default

    def set_status_listener(self, listener: Callable[[], None] | None) -> None:
        """Set an optional notification hook without exposing route details.

        The hook is deliberately one-way and payload-free.  Its owner must
        obtain any display data through its existing sanitized snapshot API.
        """
        with self._lock:
            self._status_listener = listener

    def _status_signature_locked(self) -> tuple[object, ...]:
        """Fields that affect the public route-health presentation only."""
        return (
            self._running,
            self._active_id,
            tuple(
                (
                    profile_id,
                    runtime.profile.name,
                    runtime.profile.enabled,
                    runtime.state,
                    runtime.failures,
                )
                for profile_id, runtime in self._profiles.items()
            ),
        )

    def _notify_status_changed(self) -> None:
        """Invoke the observer outside the controller lock and never fail health work."""
        with self._lock:
            listener = self._status_listener
        if listener is None:
            return
        try:
            listener()
        except Exception:
            log.debug("Transport status listener failed", exc_info=True)

    def set_policy(self, *, failover_enabled: bool | None = None,
                   failover_mode: str | None = None,
                   failure_reaction: str | None = None) -> None:
        """Update policy atomically; next probe cycle applies it."""
        with self._lock:
            if failover_enabled is not None:
                self._failover_enabled = bool(failover_enabled)
            if failover_mode is not None:
                self._failover_mode = self._valid_mode(failover_mode, FAILOVER_MODES, "recommended")
            if failure_reaction is not None:
                self._failure_reaction = self._valid_mode(failure_reaction, FAILURE_REACTIONS, "balanced")
        self._wake.set()

    def set_profiles(self, profiles: Iterable[RouteProfile]) -> None:
        """Replace configured routes without discarding health of unchanged ids."""
        ordered: dict[str, RouteProfile] = {}
        preferred_id = self._preferred_profile_id
        for profile in sorted(
            profiles,
            key=lambda item: (
                item.id != preferred_id,
                item.priority,
                item.name.casefold(),
                item.id,
            ),
        ):
            if profile.id in ordered:
                continue
            ordered[profile.id] = profile
        now = self._clock()
        with self._lock:
            before = self._status_signature_locked()
            existing = self._profiles
            updated: dict[str, _RouteRuntime] = {}
            for profile_id, profile in ordered.items():
                previous = existing.get(profile_id)
                if previous is not None and previous.profile == profile:
                    updated[profile_id] = previous
                else:
                    updated[profile_id] = _RouteRuntime(
                        profile=profile,
                        state="checking" if profile.enabled else "stopped",
                    )
            self._profiles = updated
            if self._active_id not in self._profiles or not self._profiles[self._active_id].profile.enabled:
                self._active_id = ""
            if not self._active_id:
                for runtime in self._profiles.values():
                    if runtime.state == "active":
                        runtime.state = "warm" if runtime.profile.enabled else "stopped"
                        runtime.active_since = None
            self._trim_events_locked(now)
            changed = before != self._status_signature_locked()
        self._wake.set()
        if changed:
            self._notify_status_changed()

    def start(self, *, interval_seconds: float = 8.0) -> None:
        """Start periodic probing.  A synchronous first pass prepares standby now."""
        interval = max(1.0, float(interval_seconds))
        with self._lock:
            if self._running:
                return
            before = self._status_signature_locked()
            self._running = True
            self._wake.clear()
            changed = before != self._status_signature_locked()
        if changed:
            self._notify_status_changed()
        # Finish the first health pass before a background cycle exists.  This
        # prevents duplicate initial probes and makes Start deterministic.
        self.run_once()
        with self._lock:
            if not self._running:
                return
            self._thread = threading.Thread(
                target=self._loop,
                args=(interval,),
                daemon=True,
                name="UmbraNet-TransportHealth",
            )
            self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """Stop health work and make every route unavailable to the gateway."""
        with self._lock:
            before = self._status_signature_locked()
            self._running = False
            self._active_id = ""
            for runtime in self._profiles.values():
                runtime.state = "stopped"
                runtime.active_since = None
            changed = before != self._status_signature_locked()
        self._wake.set()
        if changed:
            self._notify_status_changed()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(join_timeout)))
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _loop(self, interval_seconds: float) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
            self.run_once()
            self._wake.wait(interval_seconds)
            self._wake.clear()

    def _profiles_to_probe(self) -> list[RouteProfile]:
        with self._lock:
            active = self._active_id
            mode = self._failover_mode
            profiles = [runtime.profile for runtime in self._profiles.values() if runtime.profile.enabled]
        if mode == "economy" and active:
            return [profile for profile in profiles if profile.id == active]
        return profiles

    def run_once(self) -> dict[str, object]:
        """Probe routes once and atomically choose only an already verified route."""
        with self._lock:
            before = self._status_signature_locked()
        candidates = self._profiles_to_probe()
        results: dict[str, RouteProbeResult] = {}
        for profile in candidates:
            try:
                result = self._probe(profile)
            except Exception as exc:  # A third-party driver must not kill failover.
                result = RouteProbeResult(ok=False, stage="controller", error=str(exc))
            results[profile.id] = result

        with self._lock:
            now = self._clock()
            for profile_id, result in results.items():
                runtime = self._profiles.get(profile_id)
                if runtime is None or not runtime.profile.enabled:
                    continue
                runtime.last_probe = result
                runtime.last_checked_at = now
                if result.ok:
                    runtime.failures = 0
                    runtime.state = "active" if profile_id == self._active_id else "warm"
                elif result.stage == "not_verified":
                    # A missing user-owned endpoint is not a transport fault;
                    # it simply cannot grant a route the warm/active status.
                    runtime.failures = 0
                    runtime.state = "not_verified"
                else:
                    runtime.failures += 1
                    if profile_id == self._active_id:
                        threshold = _FAILURE_THRESHOLDS[self._failure_reaction]
                        runtime.state = "failed" if runtime.failures >= threshold else "degraded"
                    else:
                        runtime.state = "failed"

            # Economy does not continuously test reserves, but a failed active
            # triggers an on-demand test before routing can change.
            if self._needs_economy_reserve_probe_locked():
                reserve_profiles = self._reserve_profiles_locked()
            else:
                reserve_profiles = []

        if reserve_profiles:
            reserve_results: dict[str, RouteProbeResult] = {}
            for profile in reserve_profiles:
                try:
                    reserve_results[profile.id] = self._probe(profile)
                except Exception as exc:
                    reserve_results[profile.id] = RouteProbeResult(ok=False, stage="controller", error=str(exc))
            with self._lock:
                now = self._clock()
                for profile_id, result in reserve_results.items():
                    runtime = self._profiles.get(profile_id)
                    if runtime is None or not runtime.profile.enabled:
                        continue
                    runtime.last_probe = result
                    runtime.last_checked_at = now
                    if result.ok:
                        runtime.failures = 0
                        runtime.state = "warm"
                    elif result.stage == "not_verified":
                        runtime.failures = 0
                        runtime.state = "not_verified"
                    else:
                        runtime.failures += 1
                        runtime.state = "failed"

        with self._lock:
            self._select_active_locked(self._clock())
            snapshot = self.snapshot()
            changed = before != self._status_signature_locked()
        if changed:
            self._notify_status_changed()
        return snapshot

    def _needs_economy_reserve_probe_locked(self) -> bool:
        if not self._failover_enabled or self._failover_mode != "economy" or not self._active_id:
            return False
        active = self._profiles.get(self._active_id)
        return bool(active and active.state == "failed")

    def _reserve_profiles_locked(self) -> list[RouteProfile]:
        return [
            runtime.profile
            for profile_id, runtime in self._profiles.items()
            if profile_id != self._active_id and runtime.profile.enabled
        ]

    def _ready_candidates_locked(self) -> list[_RouteRuntime]:
        ready = [
            runtime for runtime in self._profiles.values()
            if runtime.profile.enabled and runtime.last_probe is not None and runtime.last_probe.ok
        ]
        return sorted(
            ready,
            key=lambda item: (
                item.profile.id != self._preferred_profile_id,
                item.profile.priority,
                item.profile.name.casefold(),
                item.profile.id,
            ),
        )

    def _select_active_locked(self, now: float) -> None:
        active = self._profiles.get(self._active_id)
        if active is not None and active.state in {"active", "degraded"}:
            # A short soft degradation is deliberately held to avoid flapping.
            return
        if active is not None and active.state == "failed" and not self._failover_enabled:
            # The user disabled automatic replacement: the failed route becomes
            # unavailable, but a healthy reserve is not selected behind their back.
            active.active_since = None
            self._active_id = ""
            return

        candidates = self._ready_candidates_locked()
        if not candidates:
            if active is not None:
                active.active_since = None
            self._active_id = ""
            return

        chosen = candidates[0]
        previous_id = self._active_id
        if previous_id and previous_id != chosen.profile.id:
            previous = self._profiles.get(previous_id)
            if previous is not None:
                previous.active_since = None
                previous.state = "failed" if previous.last_probe and not previous.last_probe.ok else "warm"
        for runtime in self._profiles.values():
            if runtime.profile.id != chosen.profile.id and runtime.state == "active":
                runtime.state = "warm"
                runtime.active_since = None
        chosen.state = "active"
        if chosen.active_since is None:
            chosen.active_since = now
        self._active_id = chosen.profile.id
        if previous_id != chosen.profile.id:
            reason = "первый проверенный маршрут" if not previous_id else "активный маршрут не прошёл проверку"
            self._events.append(SwitchEvent(now, previous_id, chosen.profile.id, reason))
            self._trim_events_locked(now)
            log.info("Transport route selected: %s → %s (%s)", previous_id or "—", chosen.profile.id, reason)

    def _trim_events_locked(self, _now: float) -> None:
        if len(self._events) > self._history_limit:
            del self._events[:-self._history_limit]

    def startup_replacement_offer(self) -> RouteProfile | None:
        """Suggest a ready reserve after a failed *manual* initial choice.

        The GUI decides whether the person accepts this proposal.  Auto mode has
        no preferred id and therefore never produces an offer: it may select a
        healthy reserve immediately.  This method neither switches policy nor
        exposes a route that has not already passed a probe.
        """
        with self._lock:
            preferred = self._profiles.get(self._preferred_profile_id)
            if not self._preferred_profile_id or preferred is None:
                return None
            if preferred.last_probe is None or preferred.last_probe.ok:
                return None
            candidates = [
                runtime for runtime in self._ready_candidates_locked()
                if runtime.profile.id != self._preferred_profile_id
            ]
            return candidates[0].profile if candidates else None

    def active_profile(self) -> RouteProfile | None:
        """The only route a selected VPN request may use; never a direct fallback."""
        with self._lock:
            runtime = self._profiles.get(self._active_id)
            if runtime is None or runtime.state not in {"active", "degraded"}:
                return None
            return runtime.profile

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            active = self._profiles.get(self._active_id)
            return {
                "running": self._running,
                "failover_enabled": self._failover_enabled,
                "failover_mode": self._failover_mode,
                "failure_reaction": self._failure_reaction,
                "probe_configured": self._probe_configured,
                "no_ready_policy": "block",
                "active_id": self._active_id,
                "active_name": active.profile.name if active else "",
                "routes": [runtime.public_mapping() for runtime in self._profiles.values()],
                "switches": [event.public_mapping(self._profiles) for event in self._events],
            }
