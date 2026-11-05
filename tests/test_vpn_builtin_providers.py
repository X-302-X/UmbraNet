"""Pure-Python contract tests for the built-in VPN provider catalogue."""

from __future__ import annotations

from vpn.providers import (
    BuiltinProviderCatalog,
    CloudflareWarpProvider,
    OperaProvider,
    ProviderState,
)
from vpn.runtime import TransportRuntime


class _ProxyStatusOnly:
    def status(self):
        return {"snapshot_exists": False, "current_pac_url": ""}


def test_catalog_publishes_expected_one_click_provider_names_without_network_io():
    catalogue = BuiltinProviderCatalog(providers=(
        CloudflareWarpProvider(windows=True, executable_lookup=lambda _name: None),
        OperaProvider(provider_id="builtin-opera-eu", name="Opera Europe", region_label="Европа"),
        OperaProvider(provider_id="builtin-opera-america", name="Opera America", region_label="Америка"),
    ))

    snapshot = catalogue.public_snapshot()

    assert [item["id"] for item in snapshot] == [
        "builtin-warp", "builtin-opera-eu", "builtin-opera-america",
    ]
    assert [item["name"] for item in snapshot] == [
        "Cloudflare WARP", "Opera Europe", "Opera America",
    ]
    assert snapshot[0]["state"] == ProviderState.NOT_INSTALLED.value
    assert all(item["selectable"] is False for item in snapshot)
    assert all("host" not in item and "port" not in item for item in snapshot)


def test_warp_catalogue_never_claims_route_ready_merely_because_cli_exists():
    provider = CloudflareWarpProvider(windows=True, executable_lookup=lambda _name: r"C:\\Program Files\\Cloudflare\\warp-cli.exe")

    status = provider.inspect()
    result = provider.start()

    assert status.state is ProviderState.CONFIGURATION_REQUIRED
    assert result.ok is False
    assert result.route is None


def test_opera_entries_remain_nonselectable_while_security_gate_is_open():
    provider = OperaProvider(provider_id="builtin-opera-eu", name="Opera Europe", region_label="Европа")

    status = provider.inspect()
    result = provider.start()

    assert status.state is ProviderState.SECURITY_REVIEW
    assert status.selectable is False
    assert result.ok is False


def test_duplicate_builtin_provider_ids_are_rejected():
    first = OperaProvider(provider_id="builtin-opera-eu", name="One", region_label="Европа")
    second = OperaProvider(provider_id="builtin-opera-eu", name="Two", region_label="Европа")

    try:
        BuiltinProviderCatalog(providers=(first, second))
    except ValueError as exc:
        assert "Повторяющийся" in str(exc)
    else:
        raise AssertionError("duplicate provider id accepted")


def test_runtime_snapshot_exposes_only_public_builtin_provider_statuses():
    catalogue = BuiltinProviderCatalog(providers=(
        OperaProvider(provider_id="builtin-opera-eu", name="Opera Europe", region_label="Европа"),
    ))
    runtime = TransportRuntime({}, proxy_manager=_ProxyStatusOnly(), provider_catalog=catalogue)

    snapshot = runtime.snapshot()

    assert snapshot["built_in_providers"] == [{
        "id": "builtin-opera-eu",
        "name": "Opera Europe",
        "region_label": "Европа",
        "description": "Встроенный Opera-маршрут без ручного ввода proxy-параметров.",
        "official": False,
        "experimental": True,
        "state": "security_review",
        "message": "Ожидает завершения проверки безопасности и поставки компонента.",
        "selectable": False,
    }]
