"""New explicit VPN / Combo / DPI runtime orchestration tests."""

from __future__ import annotations

from dataclasses import dataclass

from routing_runtime import RoutingRuntime, split_routing_targets


@dataclass
class _TransportSpy:
    start_result: tuple[bool, str] = (True, "PAC ready")
    started_domains: list[str] | None = None
    stop_calls: int = 0
    reconfigured_with: dict | None = None

    def start(self, domains):
        self.started_domains = list(domains)
        ok, message = self.start_result
        return type("Result", (), {"ok": ok, "message": message})()

    def stop(self):
        self.stop_calls += 1
        return True, "PAC restored"

    def reconfigure(self, config):
        self.reconfigured_with = config

    def snapshot(self):
        return {"running": self.started_domains is not None and self.stop_calls == 0}


@dataclass
class _DpiSpy:
    start_result: tuple[bool, str] = (True, "DPI ready")
    started_domains: list[str] | None = None
    stop_calls: int = 0
    reconfigured_with: dict | None = None

    def start(self, domains):
        self.started_domains = list(domains)
        ok, message = self.start_result
        return type("Result", (), {"ok": ok, "message": message})()

    def stop(self):
        self.stop_calls += 1
        return True, "DPI stopped"

    def reconfigure(self, config):
        self.reconfigured_with = config

    def snapshot(self):
        return {"running": self.started_domains is not None and self.stop_calls == 0}


def _config(mode="combo"):
    return {
        "routing_mode": mode,
        "selected_services": ["ChatGPT / OpenAI", "YouTube"],
        "routed_domains": ["openai.com", "chatgpt.com", "api.openai.com", "youtube.com", "googlevideo.com"],
    }


def test_config_without_explicit_mode_uses_dpi_as_the_safe_first_run_default(monkeypatch):
    import umbranet.engine_adapter as adapter

    class _Engine:
        config = {"routing_mode": "", "routed_domains": ["chatgpt.com"], "transport_profiles": []}

    monkeypatch.setattr(adapter, "get_engine", lambda: _Engine())

    assert adapter.get_current_mode() == "dpi"
    ready, message = adapter.routing_start_readiness()

    assert ready, message


def test_engine_config_reload_rebinds_existing_routing_owner_without_replacing_it(monkeypatch):
    import umbranet.engine_adapter as adapter

    old_config = {"routing_mode": "vpn"}
    new_config = {"routing_mode": "combo"}

    class _Runtime:
        def __init__(self):
            self.reconfigured_with = None

        def reconfigure(self, config):
            self.reconfigured_with = config

    class _Engine:
        config = new_config

    runtime = _Runtime()
    monkeypatch.setattr(adapter, "_routing_runtime", runtime)
    monkeypatch.setattr(adapter, "_routing_runtime_config", old_config)
    monkeypatch.setattr(adapter, "get_engine", lambda: _Engine())

    assert adapter.get_routing_runtime() is runtime
    assert runtime.reconfigured_with is new_config
    assert adapter._routing_runtime_config is new_config


def test_forced_runtime_sync_applies_in_place_settings_without_dns_reload(monkeypatch):
    import umbranet.engine_adapter as adapter

    config = {"routing_mode": "vpn", "transport_failure_reaction": "balanced"}

    class _Runtime:
        def __init__(self):
            self.reconfigured_with = None

        def reconfigure(self, value):
            self.reconfigured_with = value

    class _Engine:
        pass

    _Engine.config = config
    runtime = _Runtime()
    monkeypatch.setattr(adapter, "_routing_runtime", runtime)
    monkeypatch.setattr(adapter, "_routing_runtime_config", config)
    monkeypatch.setattr(adapter, "get_engine", lambda: _Engine())

    config["transport_failure_reaction"] = "fast"
    adapter.sync_routing_runtime_config(force=True)

    assert runtime.reconfigured_with is config
    assert adapter._routing_runtime_config is config


def test_vpn_block_message_marks_running_pac_without_verified_exit(monkeypatch):
    import umbranet.engine_adapter as adapter

    class _Runtime:
        def snapshot(self):
            return {
                "running": True,
                "targets": {"vpn": ["chatgpt.com"], "dpi": []},
                "vpn": {"controller": {"active_id": ""}},
            }

    monkeypatch.setattr(adapter, "get_routing_runtime", lambda: _Runtime())

    assert adapter.routing_vpn_block_message() == (
        "VPN: нет готового сервера — выбранные VPN-сервисы заблокированы"
    )


def test_vpn_block_message_is_empty_with_verified_exit_or_no_vpn_target(monkeypatch):
    import umbranet.engine_adapter as adapter

    class _Runtime:
        def __init__(self, snapshot):
            self._snapshot = snapshot

        def snapshot(self):
            return self._snapshot

    ready = {
        "running": True,
        "targets": {"vpn": ["chatgpt.com"]},
        "vpn": {"controller": {"active_id": "exit-a"}},
    }
    monkeypatch.setattr(adapter, "get_routing_runtime", lambda: _Runtime(ready))
    assert adapter.routing_vpn_block_message() == ""

    no_vpn = {
        "running": True,
        "targets": {"vpn": []},
        "vpn": {"controller": {"active_id": ""}},
    }
    monkeypatch.setattr(adapter, "get_routing_runtime", lambda: _Runtime(no_vpn))
    assert adapter.routing_vpn_block_message() == ""


def test_combo_sends_ai_to_vpn_and_youtube_to_dpi():
    targets = split_routing_targets(_config())

    assert "openai.com" in targets.vpn_domains
    assert "chatgpt.com" in targets.vpn_domains
    assert "youtube.com" not in targets.vpn_domains
    assert "youtube.com" in targets.dpi_domains
    assert "googlevideo.com" in targets.dpi_domains


def test_reconfigure_keeps_active_session_owner_and_passes_new_policy_to_children():
    transport = _TransportSpy()
    dpi = _DpiSpy()
    runtime = RoutingRuntime(_config(), transport_runtime=transport, dpi_runtime=dpi)
    assert runtime.start().ok
    replacement = _config()
    replacement["transport_failover_enabled"] = False
    replacement["transport_failure_reaction"] = "fast"

    runtime.reconfigure(replacement)

    assert runtime.snapshot()["running"] is True
    assert transport.reconfigured_with is replacement
    assert dpi.reconfigured_with is replacement
    assert runtime.mode == "combo"


def test_vpn_mode_routes_every_selected_domain_through_pac():
    targets = split_routing_targets(_config("vpn"))

    assert set(targets.vpn_domains) == set(_config("vpn")["routed_domains"])
    assert targets.dpi_domains == ()


def test_manual_domain_follows_the_global_pure_mode_without_a_per_row_policy():
    vpn_cfg = _config("vpn")
    vpn_cfg["routed_domains"].append("manual.example")
    vpn_targets = split_routing_targets(vpn_cfg)
    assert "manual.example" in vpn_targets.vpn_domains
    assert "manual.example" not in vpn_targets.dpi_domains

    dpi_cfg = _config("dpi")
    dpi_cfg["routed_domains"].append("manual.example")
    dpi_targets = split_routing_targets(dpi_cfg)
    assert "manual.example" not in dpi_targets.vpn_domains
    assert "manual.example" in dpi_targets.dpi_domains


def test_dpi_mode_routes_every_selected_domain_to_dpi_and_never_starts_pac():
    targets = split_routing_targets(_config("dpi"))

    assert targets.vpn_domains == ()
    assert set(targets.dpi_domains) == set(_config("dpi")["routed_domains"])


def test_combo_defers_manual_and_subscription_targets_until_its_rules_are_specified():
    cfg = _config()
    cfg["routed_domains"].append("manual.example")
    cfg["subscribed_domains_set"] = ["subscription.example"]
    # An old per-domain map cannot revive the removed selector or choose a
    # route behind the user's back.
    cfg["manual_domain_policies"] = {"manual.example": "vpn"}

    targets = split_routing_targets(cfg)

    assert "openai.com" in targets.vpn_domains
    assert "youtube.com" in targets.dpi_domains
    assert "manual.example" not in targets.vpn_domains
    assert "manual.example" not in targets.dpi_domains
    assert "subscription.example" not in targets.vpn_domains
    assert "subscription.example" not in targets.dpi_domains


def test_combo_start_uses_both_runtimes_and_stop_restores_pac_before_dpi():
    transport = _TransportSpy()
    dpi = _DpiSpy()
    runtime = RoutingRuntime(_config(), transport_runtime=transport, dpi_runtime=dpi)

    result = runtime.start()

    assert result.ok
    assert result.vpn_active and result.dpi_active
    assert "openai.com" in transport.started_domains
    assert "youtube.com" in dpi.started_domains

    ok, _message = runtime.stop()

    assert ok
    assert transport.stop_calls == 1
    assert dpi.stop_calls == 1


def test_dpi_failure_rolls_back_pac_before_returning_an_error():
    transport = _TransportSpy()
    dpi = _DpiSpy(start_result=(False, "strategy unavailable"))
    runtime = RoutingRuntime(_config(), transport_runtime=transport, dpi_runtime=dpi)

    result = runtime.start()

    assert not result.ok
    assert "strategy unavailable" in result.message
    assert transport.stop_calls == 1
    assert runtime.snapshot()["running"] is False


def test_vpn_failure_never_starts_dpi():
    transport = _TransportSpy(start_result=(False, "no verified proxy"))
    dpi = _DpiSpy()
    runtime = RoutingRuntime(_config(), transport_runtime=transport, dpi_runtime=dpi)

    result = runtime.start()

    assert not result.ok
    assert dpi.started_domains is None


def test_manual_reserve_proposal_is_forwarded_without_starting_dpi():
    class _ManualChoiceTransport(_TransportSpy):
        def start(self, domains):
            self.started_domains = list(domains)
            return type("Result", (), {
                "ok": False,
                "message": "manual server unavailable",
                "reserve_choice_required": True,
                "failed_profile_id": "manual",
                "failed_profile_name": "Manual",
                "suggested_profile_id": "reserve",
                "suggested_profile_name": "Reserve",
            })()

    transport = _ManualChoiceTransport()
    dpi = _DpiSpy()
    runtime = RoutingRuntime(_config("combo"), transport_runtime=transport, dpi_runtime=dpi)

    result = runtime.start()

    assert not result.ok
    assert result.reserve_choice_required is True
    assert result.failed_profile_name == "Manual"
    assert result.suggested_profile_id == "reserve"
    assert dpi.started_domains is None


def test_adapter_retains_manual_reserve_choice_once_for_qt_worker_contract(monkeypatch):
    import umbranet.engine_adapter as adapter

    class _Runtime:
        def start(self):
            return type("Result", (), {
                "ok": False,
                "message": "manual server unavailable",
                "reserve_choice_required": True,
                "failed_profile_id": "manual",
                "failed_profile_name": "Manual",
                "suggested_profile_id": "reserve",
                "suggested_profile_name": "Reserve",
            })()

    monkeypatch.setattr(adapter, "get_routing_runtime", lambda: _Runtime())
    monkeypatch.setattr(adapter, "_last_reserve_choice", None)

    ok, message = adapter.start_routing_runtime()

    assert not ok
    assert message == "manual server unavailable"
    assert adapter.take_vpn_reserve_choice() == {
        "failed_profile_id": "manual",
        "failed_profile_name": "Manual",
        "suggested_profile_id": "reserve",
        "suggested_profile_name": "Reserve",
    }
    assert adapter.take_vpn_reserve_choice() is None


def test_adapter_protocol_capabilities_are_a_safe_core_snapshot(monkeypatch):
    import umbranet.engine_adapter as adapter

    class _Runtime:
        def snapshot(self):
            return {
                "vpn": {
                    "protocols": [{
                        "protocol": "vless",
                        "state": "backend_required",
                        "selectable": False,
                    }],
                },
            }

    monkeypatch.setattr(adapter, "get_routing_runtime", lambda: _Runtime())

    assert adapter.vpn_protocol_capabilities() == [{
        "protocol": "vless",
        "state": "backend_required",
        "selectable": False,
    }]


def test_adapter_server_statuses_keep_failed_result_after_preflight_without_calling_it_live(monkeypatch):
    import umbranet.engine_adapter as adapter

    class _Runtime:
        def snapshot(self):
            return {
                "vpn": {
                    "running": False,
                    "server_statuses": {
                        "routes": [{
                            "id": "manual", "name": "Manual", "state": "failed",
                            "host": "private.example", "port": 1080,
                            "probe": {"ok": False, "stage": "connect", "error": "private detail"},
                        }],
                    },
                },
            }

    monkeypatch.setattr(adapter, "get_routing_runtime", lambda: _Runtime())

    statuses = adapter.vpn_server_statuses()

    assert statuses["manual"]["state"] == "failed"
    assert statuses["manual"]["live"] is False
    assert "host" not in statuses["manual"]
    assert "error" not in statuses["manual"]["probe"]


def test_adapter_persists_confirmed_reserve_as_next_manual_server(monkeypatch):
    import umbranet.engine_adapter as adapter

    config = {
        "transport_profiles": [
            {"id": "reserve", "name": "Reserve", "protocol": "socks5", "host": "reserve.example", "port": 1080,
             "enabled": True, "priority": 20, "country_hint": "DE", "username": "", "password": ""},
        ],
    }
    saved: list[dict] = []
    synced: list[bool] = []

    class _Engine:
        pass

    _Engine.config = config
    monkeypatch.setattr(adapter, "get_engine", lambda: _Engine())
    monkeypatch.setattr(adapter, "save_config", lambda value: saved.append(value))
    monkeypatch.setattr(adapter, "sync_routing_runtime_config", lambda *, force=False: synced.append(force))

    ok, _message = adapter.select_vpn_server_for_next_start("reserve")

    assert ok
    assert config["transport_primary_profile_id"] == "reserve"
    assert saved == [config]
    assert synced == [True]
