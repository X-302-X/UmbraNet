"""External locations for recovery artifacts."""

from __future__ import annotations

from pathlib import Path

import backup_utils
import network_repair
from rollback_paths import rollback_file, rollback_root, rollback_subdir
from vpn.system_proxy import SystemProxyManager


def test_default_rollback_partitions_are_outside_the_project_tree():
    project_root = Path(__file__).resolve().parents[1]
    expected_root = project_root.parent / "uploads" / "UmbraNet" / "rollback"

    assert rollback_root() == expected_root
    assert rollback_subdir("proxy") == expected_root / "proxy"
    assert rollback_subdir("network") == expected_root / "network"
    assert rollback_subdir("config") == expected_root / "config"
    assert rollback_file("network", "subscribed_domains_cache.json.bak") == (
        expected_root / "network" / "subscribed_domains_cache.json.bak"
    )
    assert project_root not in expected_root.parents
    assert SystemProxyManager(windows=False).snapshot_path == expected_root / "proxy" / "proxy_snapshot.json"
    assert Path(network_repair.BACKUP_DIR) == expected_root / "network"
    assert Path(backup_utils.BACKUP_DIR) == expected_root / "config"


def test_rollback_root_override_moves_new_proxy_snapshot_default(monkeypatch, tmp_path):
    rollback_root_override = tmp_path / "recovery"
    monkeypatch.setenv("UMBRANET_ROLLBACK_ROOT", str(rollback_root_override))

    assert rollback_root() == rollback_root_override
    assert SystemProxyManager(windows=False).snapshot_path == rollback_root_override / "proxy" / "proxy_snapshot.json"


def test_proxy_snapshot_file_override_has_priority_over_rollback_root(monkeypatch, tmp_path):
    monkeypatch.setenv("UMBRANET_ROLLBACK_ROOT", str(tmp_path / "recovery"))
    snapshot_override = tmp_path / "watchdog" / "snapshot.json"
    monkeypatch.setenv("UMBRANET_PROXY_SNAPSHOT", str(snapshot_override))

    assert SystemProxyManager(windows=False).snapshot_path == snapshot_override


def test_rollback_partition_name_cannot_escape_its_root():
    for unsafe_name in ("", ".", "..", "../network", "proxy/network", "network\\proxy"):
        try:
            rollback_subdir(unsafe_name)
        except ValueError:
            continue
        raise AssertionError(f"unsafe rollback partition accepted: {unsafe_name!r}")

    for unsafe_filename in ("", ".", "..", "../config.json", "config/state.json", "config\\state.json"):
        try:
            rollback_file("config", unsafe_filename)
        except ValueError:
            continue
        raise AssertionError(f"unsafe rollback filename accepted: {unsafe_filename!r}")
