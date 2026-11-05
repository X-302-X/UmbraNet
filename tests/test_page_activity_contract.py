"""No-PySide regression contracts for inactive page work.

The shell remains the single authority that activates/deactivates a page while
changing stacked-widget selection. Route-health views receive invalidations from
core events, so they must not retain their former UI polling timers.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "umbranet" / "app.py").read_text(encoding="utf-8")
LOG = (ROOT / "umbranet" / "views" / "log.py").read_text(encoding="utf-8")
ABOUT = (ROOT / "umbranet" / "views" / "about.py").read_text(encoding="utf-8")
SERVERS = (ROOT / "umbranet" / "views" / "servers.py").read_text(encoding="utf-8")
ROUTING = (ROOT / "umbranet" / "views" / "routing.py").read_text(encoding="utf-8")
VPN_EXIT_LIST = (ROOT / "umbranet" / "widgets" / "vpn_exit_list.py").read_text(encoding="utf-8")
STRATEGY_LAB = (ROOT / "umbranet" / "views" / "strategy_lab.py").read_text(encoding="utf-8")


def _method(source: str, name: str) -> str:
    marker = f"    def {name}("
    start = source.index(marker)
    end = source.find("\n    def ", start + len(marker))
    return source[start:] if end < 0 else source[start:end]


def test_shell_transitions_optional_page_activity_hooks_once_per_switch():
    show = _method(APP, "_show")
    hook = _method(APP, "_set_page_active")
    assert 'hook_name = "page_activated" if active else "page_deactivated"' in hook
    assert "self._set_page_active(previous_key, False)" in show
    assert "self.stack.setCurrentIndex(self._pages[key])" in show
    assert "self._set_page_active(key, True)" in show
    assert show.index("self._set_page_active(previous_key, False)") < show.index("self.stack.setCurrentIndex")
    assert show.index("self.stack.setCurrentIndex") < show.index("self._set_page_active(key, True)")


def test_log_live_timer_runs_only_for_an_active_visible_page():
    activate = _method(LOG, "page_activated")
    deactivate = _method(LOG, "page_deactivated")
    tick = _method(LOG, "_tick_live")
    assert "self._live_timer.start()" in activate
    assert "self._live_timer.stop()" in deactivate
    assert "if not self._page_active or not self.isVisible() or self._paused:" in tick


def test_about_poll_stops_with_its_hidden_page():
    activate = _method(ABOUT, "page_activated")
    deactivate = _method(ABOUT, "page_deactivated")
    assert "self._update_poll.start()" in activate
    assert "self._update_poll.stop()" in deactivate


def test_server_status_views_refresh_on_activation_without_polling_timers():
    servers_activate = _method(SERVERS, "page_activated")
    selector_active = _method(VPN_EXIT_LIST, "set_page_active")
    routing_activate = _method(ROUTING, "page_activated")
    routing_deactivate = _method(ROUTING, "page_deactivated")

    assert "self._rebuild_server_rows()" in servers_activate
    assert "self._rebuild_rows()" in selector_active
    assert "QTimer" not in SERVERS
    assert "QTimer" not in VPN_EXIT_LIST
    assert "self._vpn_exit_list.set_page_active(True)" in routing_activate
    assert "self._vpn_exit_list.set_page_active(False)" in routing_deactivate


def test_strategy_lab_populates_after_visible_activation_not_in_constructor():
    init = _method(STRATEGY_LAB, "__init__")
    activate = _method(STRATEGY_LAB, "page_activated")
    deactivate = _method(STRATEGY_LAB, "page_deactivated")
    deferred = _method(STRATEGY_LAB, "_refresh_after_first_show")

    assert "super().__init__(parent)" in init
    assert "self.refresh()" not in init
    assert "QTimer.singleShot(0, self._refresh_after_first_show)" in activate
    assert "self._page_active = True" in activate
    assert "self._page_active = False" in deactivate
    assert "if not self._page_active or self._initial_refresh_done:" in deferred
    assert "self.refresh()" in deferred
    assert "page = StrategyLabView(self.stack)" in APP
    assert "area = QScrollArea(self.stack)" in APP
    assert 'self._ensure_page("strategy_lab")' in APP
