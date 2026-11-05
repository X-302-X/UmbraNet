"""Регрессии отображения DPI-описаний и повторного выбора DNSCrypt."""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from umbranet import engine_adapter as ea  # noqa: E402
from umbranet.widgets import dialogs  # noqa: E402
from umbranet.widgets.dpi_strategy_canvas import DpiStrategyCanvas  # noqa: E402
from umbranet.widgets.transport_canvas import TransportCanvas  # noqa: E402
from umbranet.widgets.transport_list import TransportList  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_dpi_strategy_descriptions_match_dns_route_readability():
    dpi = DpiStrategyCanvas()
    dns = TransportCanvas()
    assert dpi._f_desc.pixelSize() == dns._f_desc.pixelSize() == 11
    dpi.close()
    dns.close()


def test_dnscrypt_resolver_menu_opens_on_every_click_when_active(monkeypatch):
    """Повторный клик по выбранному DNSCrypt снова открывает выбор резолвера."""
    monkeypatch.setattr(ea, "auto_transport_enabled", lambda: False)
    monkeypatch.setattr(ea, "get_transport", lambda: "dnscrypt")
    monkeypatch.setattr(ea, "dnscrypt_available", lambda: True)
    monkeypatch.setattr(ea, "active_has_dnscrypt_stamp", lambda: True)
    monkeypatch.setattr(ea, "transport_available", lambda _key: (True, ""))

    applied = []
    monkeypatch.setattr(
        ea,
        "apply_dnscrypt_resolver",
        lambda name, stamp: applied.append((name, stamp)) or True,
    )

    opened = []

    class FakeResolverDialog:
        def __init__(self, _parent=None):
            opened.append(self)
            self.result = {"name": "Quad9", "stamp": "sdns://test"}

        def exec(self):
            return 1

    monkeypatch.setattr(dialogs, "DnsCryptResolverDialog", FakeResolverDialog)

    widget = TransportList()
    changed = []
    widget.transportChanged.connect(changed.append)
    widget._select("dnscrypt")
    widget._select("dnscrypt")

    assert len(opened) == 2
    assert applied == [("Quad9", "sdns://test"), ("Quad9", "sdns://test")]
    assert changed == ["dnscrypt", "dnscrypt"]
    widget.stop_workers()
    widget.close()
    APP.processEvents()
