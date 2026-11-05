"""Regression guards for the DNS-independent VPN / Combo / DPI lifecycle."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_SOURCE = ROOT / "umbranet" / "app.py"
SETTINGS_SOURCE = ROOT / "umbranet" / "views" / "settings.py"
LOG_VIEW_SOURCE = ROOT / "umbranet" / "views" / "log.py"
SERVERS_VIEW_SOURCE = ROOT / "umbranet" / "views" / "servers.py"
ROUTING_VIEW_SOURCE = ROOT / "umbranet" / "views" / "routing.py"
DPI_PICKER_SOURCE = ROOT / "umbranet" / "widgets" / "dpi_strategy_list.py"
DPI_CANVAS_SOURCE = ROOT / "umbranet" / "widgets" / "dpi_strategy_canvas.py"
STRATEGY_LAB_SOURCE = ROOT / "umbranet" / "views" / "strategy_lab.py"


def _class_method(tree: ast.AST, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == method_name:
                return member
    raise AssertionError(f"{class_name}.{method_name} is missing")


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def test_action_worker_owns_only_the_explicit_routing_runtime():
    """Start/Stop/Restart cannot receive or call the historical DNS engine."""
    source = APP_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP_SOURCE))
    init = _class_method(tree, "_EngineWorker", "__init__")
    run = _class_method(tree, "_EngineWorker", "run")

    assert [arg.arg for arg in init.args.args] == ["self", "action"]
    assert {"start_routing_runtime", "stop_routing_runtime"} <= _called_names(run)
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == "engine"
        for node in ast.walk(run)
    )


def test_application_and_settings_do_not_restart_or_reconfigure_dns_for_routing():
    """Normal lifecycle/config edits must not call old DNS start/stop/reload hooks."""
    app_source = APP_SOURCE.read_text(encoding="utf-8")
    settings_source = SETTINGS_SOURCE.read_text(encoding="utf-8")
    log_view_source = LOG_VIEW_SOURCE.read_text(encoding="utf-8")

    for forbidden in (
        "set_dns_to_localhost",
        "_start_engine_with_retry",
        "self.engine.start()",
        "self.engine.stop()",
        "self.engine._manual_stop_requested",
    ):
        assert forbidden not in app_source
    assert "self.engine.reload_config()" not in settings_source
    assert "eng.reload_config()" not in log_view_source
    assert "sync_routing_runtime_config(force=True)" in settings_source


def test_settings_hide_legacy_dns_controls_without_retiring_config_compatibility():
    """The normal Settings form neither reads nor writes legacy DNS fields."""
    from config_utils import DEFAULT_CONFIG

    source = SETTINGS_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SETTINGS_SOURCE))
    settings_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SettingsView"
    )
    strings = {
        node.value
        for node in ast.walk(settings_class)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    attributes = {
        node.attr
        for node in ast.walk(settings_class)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    legacy_fields = {
        "listen_port", "fallback_dns", "fallback_dns6", "enable_ipv6",
        "ipv6_priority_enabled", "upstream_mode", "routed_cache_enabled",
        "routed_cache_ttl", "routed_reply_ttl", "optimistic_cache_enabled",
        "stale_cache_ttl", "blocked_domains", "allowlist_domains",
    }
    retired_widget_attributes = {
        "_port", "_fb4", "_fb6", "_ipv6", "_ipv6_priority", "_upstream",
        "_preset", "_cache_on", "_cache_ttl", "_reply_ttl", "_optim",
        "_stale_ttl", "_blocked_edit", "_allow_edit", "_filter_status",
    }

    assert legacy_fields <= DEFAULT_CONFIG.keys()
    assert not legacy_fields & strings
    assert not retired_widget_attributes & attributes


def test_routing_and_servers_use_live_resize_without_a_snapshot_overlay():
    """The main Routing and Servers pages must never stretch a frozen screenshot."""
    app_source = APP_SOURCE.read_text(encoding="utf-8")

    assert '"routing", "servers"' in app_source
    assert "if live and self._live_freezer.active:" in app_source
    assert "self._live_freezer.end()" in app_source


def test_main_dpi_picker_and_ai_library_share_the_full_context_menu_contract():
    """The main DPI selector must not become a read-only second-class list."""
    picker_source = DPI_PICKER_SOURCE.read_text(encoding="utf-8")
    canvas_source = DPI_CANVAS_SOURCE.read_text(encoding="utf-8")
    lab_source = STRATEGY_LAB_SOURCE.read_text(encoding="utf-8")

    assert "rowContextRequested" in canvas_source
    assert "self._canvas.rowContextRequested.connect(self._show_context_menu)" in picker_source
    assert "build_dpi_strategy_menu" in picker_source
    assert "build_dpi_strategy_menu" in lab_source
    for action_label in (
        "Создать копию", "Изменить имя", "Изменить описание",
        "Показать в папке", "Скопировать путь", "Удалить",
    ):
        assert action_label in (ROOT / "umbranet" / "widgets" / "dpi_strategy_menu.py").read_text(
            encoding="utf-8"
        )


def test_servers_keep_hidden_scrollbar_gutters_when_content_fits():
    """Neither inner lists nor the outer Servers page may reflow on overflow changes."""
    servers_source = SERVERS_VIEW_SOURCE.read_text(encoding="utf-8")
    app_source = APP_SOURCE.read_text(encoding="utf-8")

    assert "scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in servers_source
    assert 'if key == "servers":' in app_source
    assert "area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in app_source
    assert "disabled no-range handle transparent" in app_source


def test_vpn_picker_links_an_empty_library_to_explicit_subscription_setup():
    """No-server VPN state remains actionable without enabling fake providers."""
    servers_source = SERVERS_VIEW_SOURCE.read_text(encoding="utf-8")
    routing_source = ROUTING_VIEW_SOURCE.read_text(encoding="utf-8")

    assert "self._subscription_url.returnPressed.connect(self._apply_subscription_url)" in servers_source
    assert "self._apply_subscription.clicked.connect(self._apply_subscription_url)" in servers_source
    assert "Мои подписки" in servers_source
    assert "serverSetupRequested" in routing_source
    assert 'show("servers")' in routing_source


def test_adapter_start_and_stop_delegate_only_to_routing_runtime(monkeypatch):
    """The public lifecycle wrappers do not invoke the legacy DNS engine."""
    import umbranet.engine_adapter as adapter

    calls: list[str] = []

    class _Runtime:
        def start(self):
            calls.append("start")
            return type("Result", (), {"ok": True, "message": "PAC ready"})()

        def stop(self):
            calls.append("stop")
            return True, "PAC restored"

    monkeypatch.setattr(adapter, "get_routing_runtime", lambda: _Runtime())

    assert adapter.start_routing_runtime() == (True, "PAC ready")
    assert adapter.stop_routing_runtime() == (True, "PAC restored")
    assert calls == ["start", "stop"]
