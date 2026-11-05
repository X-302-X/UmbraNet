"""Static contracts for responsive, state-correct tab navigation.

These checks deliberately do not import PySide6, so they protect the navigation
policy in the headless CI environment as well as in the Windows GUI build.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "umbranet" / "app.py").read_text(encoding="utf-8")
SERVERS = (ROOT / "umbranet" / "views" / "servers.py").read_text(encoding="utf-8")
SETTINGS = (ROOT / "umbranet" / "views" / "settings.py").read_text(encoding="utf-8")
VPN_EXIT_LIST = (ROOT / "umbranet" / "widgets" / "vpn_exit_list.py").read_text(encoding="utf-8")


def _method(source: str, name: str) -> str:
    marker = f"    def {name}("
    start = source.index(marker)
    next_method = source.find("\n    def ", start + len(marker))
    return source[start:] if next_method < 0 else source[start:next_method]


def test_shell_creates_only_routing_page_initially_and_has_lazy_page_factory():
    main_window = APP[APP.index("class MainWindow"):]
    constructor = _method(main_window, "__init__")
    assert 'self._ensure_page("routing")' in constructor
    assert 'page = ServersView()' not in constructor
    assert 'page = NetworkView()' not in constructor
    assert 'page = StrategyLabView()' not in constructor
    assert 'def _make_view(' in APP
    assert 'def _ensure_page(' in APP


def test_navigation_does_not_unconditionally_refresh_a_tab():
    show = _method(APP, "_show")
    assert 'view = self._ensure_page(key)' in show
    assert 'if key in self._stale_view_keys:' in show
    assert 'self._refresh_view(key)' in show
    assert 'v.refresh()' not in show


def test_real_engine_changes_invalidate_hidden_pages_before_visible_refresh():
    events = _method(APP, "_process_engine_events")
    for event_type in ('"status_changed"', '"mode_changed"', '"config_changed"'):
        block_start = events.index(event_type)
        block_end = events.find("elif etype", block_start + len(event_type))
        block = events[block_start:] if block_end < 0 else events[block_start:block_end]
        assert "self._invalidate_views()" in block
        assert "self._refresh_current_view()" in block


def test_server_polling_keeps_unchanged_rows_and_protocols_intact():
    rebuild_servers = _method(SERVERS, "_rebuild_server_rows")
    rebuild_protocols = _method(SERVERS, "_rebuild_protocol_rows")
    assert "if self._server_rows_key == server_rows_key:" in rebuild_servers
    assert "if self._protocol_rows_key == protocol_rows_key:" in rebuild_protocols
    assert rebuild_servers.index("if self._server_rows_key == server_rows_key:") < rebuild_servers.index("self._clear(self._server_rows)")
    assert rebuild_protocols.index("if self._protocol_rows_key == protocol_rows_key:") < rebuild_protocols.index("self._clear(self._protocol_rows)")


def test_vpn_selector_avoids_repainting_identical_status_rows():
    rebuild = _method(VPN_EXIT_LIST, "_rebuild_rows")
    assert "self._render_key == render_key" in rebuild
    assert rebuild.index("self._render_key == render_key") < rebuild.index("self._canvas.set_rows(self._rows)")


def test_shared_transport_edits_invalidate_other_tabs():
    assert 'post_event({"type": "config_changed", "section": "transport_profiles"})' in SERVERS
    assert 'post_event({"type": "config_changed", "section": "transport_settings"})' in SETTINGS
    assert 'post_event({"type": "config_changed", "section": "backup_restore"})' in SETTINGS
