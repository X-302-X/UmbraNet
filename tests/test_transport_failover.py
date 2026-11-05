"""Regression coverage for transport hot standby and no-direct behaviour."""

from __future__ import annotations

from collections import defaultdict, deque

from config_utils import CONFIG_VERSION, DEFAULT_CONFIG, apply_config_migrations, sanitize_config
from vpn.controller import TransportController
from vpn.health import RouteProbeResult
from vpn.profiles import RouteProfile, sanitize_route_profiles


def _profile(profile_id: str, *, priority: int = 100, enabled: bool = True) -> RouteProfile:
    return RouteProfile(
        id=profile_id,
        name=profile_id.upper(),
        protocol="socks5",
        host=f"{profile_id}.example.net",
        port=1080,
        priority=priority,
        enabled=enabled,
    )


class _ProbePlan:
    def __init__(self, values: dict[str, list[bool]]):
        self.values = {key: deque(items) for key, items in values.items()}
        self.calls = defaultdict(int)

    def __call__(self, profile: RouteProfile) -> RouteProbeResult:
        self.calls[profile.id] += 1
        queue = self.values[profile.id]
        ok = queue.popleft() if queue else False
        return RouteProbeResult(
            ok=ok,
            stage="ready" if ok else "transport",
            exit_country="NL" if ok else "",
            service_tls_ok=ok,
            error="offline" if not ok else "",
        )


def test_recommended_mode_prepares_active_and_warm_standby():
    probes = _ProbePlan({"primary": [True], "reserve": [True]})
    controller = TransportController(
        [_profile("reserve", priority=20), _profile("primary", priority=10)],
        probe=probes,
    )

    snapshot = controller.run_once()

    assert snapshot["active_id"] == "primary"
    assert controller.active_profile().id == "primary"
    states = {row["id"]: row["state"] for row in snapshot["routes"]}
    assert states == {"primary": "active", "reserve": "warm"}
    assert snapshot["no_ready_policy"] == "block"
    assert probes.calls == {"primary": 1, "reserve": 1}


def test_selected_friendly_exit_is_tried_before_lower_priority_reserves():
    probes = _ProbePlan({"opera-us": [True], "opera-eu": [True]})
    controller = TransportController(
        [_profile("opera-eu", priority=10), _profile("opera-us", priority=20)],
        preferred_profile_id="opera-us",
        probe=probes,
    )

    snapshot = controller.run_once()

    assert snapshot["active_id"] == "opera-us"
    assert controller.active_profile().id == "opera-us"
    assert {row["id"]: row["state"] for row in snapshot["routes"]} == {
        "opera-us": "active", "opera-eu": "warm"
    }


def test_balanced_reaction_switches_only_after_confirmed_active_failure():
    probes = _ProbePlan({
        "primary": [True, False, False],
        "reserve": [True, True, True],
    })
    controller = TransportController(
        [_profile("primary", priority=10), _profile("reserve", priority=20)],
        failure_reaction="balanced",
        probe=probes,
    )

    controller.run_once()
    first_failure = controller.run_once()
    second_failure = controller.run_once()

    assert first_failure["active_id"] == "primary"
    assert next(row for row in first_failure["routes"] if row["id"] == "primary")["state"] == "degraded"
    assert second_failure["active_id"] == "reserve"
    assert controller.active_profile().id == "reserve"
    assert second_failure["switches"][-1]["previous_id"] == "primary"
    assert second_failure["switches"][-1]["active_id"] == "reserve"


def test_no_verified_route_means_block_not_direct():
    probes = _ProbePlan({"primary": [False], "reserve": [False]})
    controller = TransportController(
        [_profile("primary", priority=10), _profile("reserve", priority=20)],
        probe=probes,
    )

    snapshot = controller.run_once()

    assert snapshot["active_id"] == ""
    assert controller.active_profile() is None
    assert snapshot["no_ready_policy"] == "block"
    assert {row["state"] for row in snapshot["routes"]} == {"failed"}


def test_config_sanitizes_new_routing_mode_and_service_selection():
    cfg, warnings = sanitize_config({
        "routing_mode": "not-a-mode",
        "selected_services": ["ChatGPT / OpenAI", "ChatGPT / OpenAI", "", 7],
    })

    assert cfg["routing_mode"] == "dpi"
    assert cfg["selected_services"] == ["ChatGPT / OpenAI", "7"]
    assert any("routing_mode" in warning for warning in warnings)


def test_disabled_failover_never_promotes_warm_reserve_after_active_failure():
    probes = _ProbePlan({
        "primary": [True, False],
        "reserve": [True, True],
    })
    controller = TransportController(
        [_profile("primary", priority=10), _profile("reserve", priority=20)],
        failover_enabled=False,
        failure_reaction="fast",
        probe=probes,
    )

    controller.run_once()
    snapshot = controller.run_once()

    assert snapshot["failover_enabled"] is False
    assert snapshot["active_id"] == ""
    assert controller.active_profile() is None
    assert next(row for row in snapshot["routes"] if row["id"] == "reserve")["state"] == "warm"


def test_fast_reaction_replaces_active_on_first_failed_probe():
    probes = _ProbePlan({
        "primary": [True, False],
        "reserve": [True, True],
    })
    controller = TransportController(
        [_profile("primary", priority=10), _profile("reserve", priority=20)],
        failure_reaction="fast",
        probe=probes,
    )

    controller.run_once()
    snapshot = controller.run_once()

    assert snapshot["active_id"] == "reserve"
    assert controller.active_profile().id == "reserve"


def test_economy_probes_reserve_only_after_active_route_has_failed():
    probes = _ProbePlan({
        "primary": [True, False],
        # Economy mode validates the reserve again at the moment of takeover;
        # an old successful check is not sufficient evidence for a switch.
        "reserve": [True, True],
    })
    controller = TransportController(
        [_profile("primary", priority=10), _profile("reserve", priority=20)],
        failover_mode="economy",
        failure_reaction="fast",
        probe=probes,
    )

    controller.run_once()
    assert probes.calls == {"primary": 1, "reserve": 1}
    controller.run_once()

    assert controller.active_profile().id == "reserve"
    assert probes.calls == {"primary": 2, "reserve": 2}


def test_route_profile_sanitizer_rejects_malformed_routes_and_hides_credentials_in_status():
    profiles, warnings = sanitize_route_profiles([
        {"id": "wrong id", "name": "bad", "protocol": "http", "host": "proxy.example", "port": 8080},
        {"id": "good", "name": "EU", "protocol": "http", "host": "proxy.example", "port": "8080",
         "username": "user", "password": "secret", "country_hint": "nl"},
    ])

    assert len(profiles) == 1
    profile = RouteProfile.from_mapping(profiles[0])
    assert profile.country_hint == "NL"
    assert profile.public_mapping()["has_credentials"] is True
    assert "username" not in profile.public_mapping()
    assert "password" not in profile.public_mapping()
    assert any("некорректный id" in warning for warning in warnings)


def test_blank_legacy_mode_becomes_dpi_without_losing_transport_policy_migration():
    original = {
        "config_version": 3,
        "dpi_mode": "combo",
        "active_dns_profile": "builtin-dns-ai",
        "user_dns_profiles": [{"id": "custom", "name": "My DNS"}],
    }

    migrated, report = apply_config_migrations(original)

    assert migrated["config_version"] == CONFIG_VERSION
    assert migrated["dpi_mode"] == "dpi_only"
    assert migrated["active_dns_profile"] == "builtin-dns-ai"
    assert migrated["user_dns_profiles"] == original["user_dns_profiles"]
    assert migrated["transport_failover_mode"] == "recommended"
    assert migrated["transport_no_ready_policy"] == "block"
    assert migrated["transport_gateway_port"] == 18777
    assert migrated["transport_primary_profile_id"] == ""
    assert migrated["routing_mode"] == "dpi"
    assert migrated["selected_services"] == []
    assert report["notes"]


def test_transport_primary_profile_is_sanitized_against_saved_profiles():
    profile = {
        "id": "opera-us", "name": "Opera US", "protocol": "socks5",
        "host": "proxy.example", "port": 1080, "enabled": True,
    }
    cfg, warnings = sanitize_config({
        "transport_profiles": [profile],
        "transport_primary_profile_id": "opera-us",
    })

    assert cfg["transport_primary_profile_id"] == "opera-us"
    assert not any("transport_primary_profile_id" in warning for warning in warnings)

    cfg, warnings = sanitize_config({
        "transport_profiles": [profile],
        "transport_primary_profile_id": "removed-profile",
    })
    assert cfg["transport_primary_profile_id"] == ""
    assert any("transport_primary_profile_id" in warning for warning in warnings)


def test_transport_config_sanitization_forces_no_direct_policy():
    cfg, warnings = sanitize_config({
        "transport_failover_mode": "unknown",
        "transport_failure_reaction": "wrong",
        "transport_no_ready_policy": "direct",
        "transport_profiles": "not-a-list",
    })

    assert cfg["transport_failover_mode"] == DEFAULT_CONFIG["transport_failover_mode"]
    assert cfg["transport_failure_reaction"] == DEFAULT_CONFIG["transport_failure_reaction"]
    assert cfg["transport_no_ready_policy"] == "block"
    assert cfg["transport_profiles"] == []
    assert any("transport_no_ready_policy" in warning for warning in warnings)
