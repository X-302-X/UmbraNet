"""External rollback-data paths shared by PAC, DNS, and config recovery.

Runtime recovery data must never be written into the application/project tree.
For this workspace the default is the sibling ``uploads`` directory; deployment
or support tooling may override the root with ``UMBRANET_ROLLBACK_ROOT``.
"""

from __future__ import annotations

import os
from pathlib import Path

ROLLBACK_ROOT_ENV = "UMBRANET_ROLLBACK_ROOT"
ROLLBACK_PARTITIONS = frozenset({"proxy", "network", "config"})


def rollback_root() -> Path:
    """Return the external directory that owns all runtime rollback artifacts."""
    override = str(os.environ.get(ROLLBACK_ROOT_ENV, "") or "").strip()
    if override:
        return Path(override).expanduser()
    project_root = Path(__file__).resolve().parents[1]
    return project_root.parent / "uploads" / "UmbraNet" / "rollback"


def rollback_subdir(name: str) -> Path:
    """Return an approved rollback partition without creating it eagerly."""
    partition = str(name or "").strip()
    if partition not in ROLLBACK_PARTITIONS:
        raise ValueError("некорректное имя раздела отката")
    return rollback_root() / partition


def rollback_file(partition: str, filename: str) -> Path:
    """Return one safe recovery-artifact path inside an approved partition."""
    leaf = str(filename or "").strip()
    if not leaf or leaf in {".", ".."} or "/" in leaf or "\\" in leaf:
        raise ValueError("некорректное имя файла отката")
    return rollback_subdir(partition) / leaf
