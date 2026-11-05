"""Contracts for event-driven transport-health presentation.

The controller owns route probing.  It may tell the adapter that a public
health state changed, but no profile data or probe error crosses that callback.
"""

from __future__ import annotations

from pathlib import Path

from routing_runtime import RoutingRuntime
from vpn.controller import TransportController
from vpn.health import RouteProbeResult
from vpn.profiles import RouteProfile


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "umbranet" / "app.py").read_text(encoding="utf-8")
SERVERS = (ROOT / "umbranet" / "views" / "servers.py").read_text(encoding="utf-8")
SELECTOR = (ROOT / "umbranet" / "widgets" / "vpn_exit_list.py").read_text(encoding="utf-8")


def _profile() -> RouteProfile:
    return RouteProfile(
        id="status-test",
        name="Status test",
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
    )


def test_controller_notifies_only_for_a_public_health_state_change():
    notifications: list[None] = []
    controller = TransportController(
        [_profile()],
        probe=lambda _profile: RouteProbeResult(ok=True, stage="ready"),
    )
    controller.set_status_listener(lambda: notifications.append(None))

    controller.run_once()
    controller.run_once()  # last_checked_at changes, public health does not
    controller.stop()

    assert len(notifications) == 2


def test_routing_runtime_attaches_listener_to_injected_transport_when_supported():
    received: list[object] = []

    class Transport:
        def set_status_listener(self, listener):
            received.append(listener)

    listener = lambda: None
    RoutingRuntime({}, transport_runtime=Transport(), dpi_runtime=object(), transport_status_listener=listener)

    assert received == [listener]


def test_adapter_transport_event_is_payload_free_and_coalesced():
    import umbranet.engine_adapter as adapter

    adapter.drain_events()
    adapter._post_transport_status_changed()
    adapter._post_transport_status_changed()
    events = adapter.drain_events()

    assert events == [{"type": "transport_status_changed"}]
    adapter._post_transport_status_changed()
    assert adapter.drain_events() == [{"type": "transport_status_changed"}]


def test_server_surfaces_have_no_recurring_status_poll_and_app_refreshes_only_them():
    assert "QTimer" not in SERVERS
    assert "QTimer" not in SELECTOR
    assert 'elif etype == "transport_status_changed":' in APP
    assert 'affected = ("servers", "routing")' in APP
    assert "self._invalidate_views(affected)" in APP
    assert "self._refresh_visible_if_affected(affected)" in APP
