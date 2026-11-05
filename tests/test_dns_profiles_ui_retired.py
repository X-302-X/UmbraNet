"""Regression guard for retiring the DNS-profiles page from the product UI."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_SOURCE = ROOT / "umbranet" / "app.py"


def _imports_profiles_view(source: str) -> bool:
    tree = ast.parse(source, filename=str(APP_SOURCE))
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "umbranet.views.profiles"
        and any(alias.name == "ProfilesView" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_main_window_does_not_create_a_dns_profiles_page():
    """Legacy profile data may remain, but the page must not enter the UI stack."""
    source = APP_SOURCE.read_text(encoding="utf-8")

    assert not _imports_profiles_view(source)
    assert 'self._views["profiles"]' not in source
    assert 'self._pages["profiles"]' not in source
    assert '"profiles"' not in source
