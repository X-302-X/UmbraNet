"""
Чипы DNS/DPI у сервисов в главном меню.

YouTube в DNS-only не открывается: сервис нельзя включить, курсор —
красный крестик. Combo разрешает всё.

Запуск: python -m pytest tests/test_service_bypass.py
"""

from __future__ import annotations

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

from core.service_profiles import (
    UI_SERVICE_PROFILES,
    service_allowed_in_mode,
    service_bypass,
    service_bypass_map,
    ui_services,
)


def test_chatgpt_is_dns_youtube_is_dpi():
    assert service_bypass("ChatGPT / OpenAI") == "dns"
    assert service_bypass("YouTube") == "dpi"
    assert service_bypass("Discord") == "dpi"


def test_every_ui_service_has_dns_or_dpi():
    missing = []
    for name in UI_SERVICE_PROFILES:
        mode = service_bypass(name)
        if mode not in ("dns", "dpi"):
            missing.append((name, mode))
    assert not missing, f"сервисы без dns/dpi: {missing}"
    mapped = service_bypass_map()
    assert set(mapped) == set(UI_SERVICE_PROFILES)
    assert mapped["YouTube"] == "dpi"
    assert mapped["ChatGPT / OpenAI"] == "dns"


def test_unknown_service_defaults_to_dns():
    assert service_bypass("нет такого") == "dns"
    assert service_bypass("") == "dns"


def test_ui_services_tuple_unchanged():
    """routing.py по-прежнему распаковывает (category, icon, domains)."""
    for name, triple in ui_services().items():
        assert len(triple) == 3, name
        cat, icon, domains = triple
        assert isinstance(cat, str) and cat
        assert isinstance(icon, str) and icon
        assert isinstance(domains, list) and domains


def test_catalog_exposes_bypass_map():
    from umbranet.services_catalog import SERVICE_BYPASS, SERVICES

    assert SERVICE_BYPASS["YouTube"] == "dpi"
    assert SERVICE_BYPASS["ChatGPT / OpenAI"] == "dns"
    assert set(SERVICE_BYPASS) == set(SERVICES)


def test_allowed_in_mode():
    assert service_allowed_in_mode("YouTube", "dns_only") is False
    assert service_allowed_in_mode("YouTube", "dpi_only") is True
    assert service_allowed_in_mode("YouTube", "combo") is True
    assert service_allowed_in_mode("ChatGPT / OpenAI", "dns_only") is True
    assert service_allowed_in_mode("ChatGPT / OpenAI", "dpi_only") is False
    assert service_allowed_in_mode("ChatGPT / OpenAI", "combo") is True


def test_bang_icon_is_gone():
    src = (ROOT / "umbranet" / "widgets" / "service_canvas.py").read_text(encoding="utf-8")
    assert 'Qt.AlignCenter, "!"' not in src
    assert 'Qt.AlignCenter, "?"' not in src
    assert "HELP_TIP_MS" not in src
    assert "_paint_help_mark" not in src
    assert "_cross_cursor" not in src
    assert "def _ban_cursor" in src
    assert "def _locked_tip" in src


def test_locked_service_cannot_be_enabled():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.service_canvas import (
        ROW_H,
        TG_RIGHT,
        TOGGLE_W,
        ServiceCanvas,
    )

    canvas = ServiceCanvas(
        [("Медиа", "🎬", "#4d8dff", "#22d3ee",
          [("YouTube", "▶️"), ("ChatGPT / OpenAI", "🤖")])],
        bypass_map={"YouTube": "dpi", "ChatGPT / OpenAI": "dns"},
    )
    canvas.resize(500, 200)
    canvas.set_app_mode("dns_only")
    assert canvas._locked("YouTube") is True
    assert canvas._locked("ChatGPT / OpenAI") is False
    canvas.set_app_mode("combo")
    assert canvas._locked("YouTube") is False
    canvas.set_app_mode("dpi_only")
    assert canvas._locked("ChatGPT / OpenAI") is True
    assert canvas._locked("YouTube") is False

    canvas.set_app_mode("dns_only")
    row_i = next(i for i, r in enumerate(canvas._rows) if r.get("svc") == "YouTube")
    w = canvas._content_w()
    toggle_x = w - TOGGLE_W - TG_RIGHT
    y = canvas._tops[row_i] + ROW_H // 2 - canvas._offset
    toggled = []
    canvas.serviceToggled.connect(lambda n, on: toggled.append((n, on)))
    QtTest = pytest.importorskip("PySide6.QtTest")
    QtTest.QTest.mouseClick(
        canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
        QtCore.QPoint(int(toggle_x + TOGGLE_W / 2), int(y)),
    )
    assert toggled == [], f"DPI-сервис включился в DNS-режиме: {toggled}"
    canvas.deleteLater()


def test_service_checkbox_changes_state_without_knob_animation():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    QtTest = pytest.importorskip("PySide6.QtTest")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.service_canvas import (
        ROW_H,
        TG_RIGHT,
        TOGGLE_W,
        ServiceCanvas,
    )

    canvas = ServiceCanvas(
        [("Медиа", "🎬", "#4d8dff", "#22d3ee", [("YouTube", "▶️")])],
        bypass_map={"YouTube": "dpi"},
    )
    canvas.resize(500, 160)
    canvas.set_app_mode("combo")
    row_i = next(i for i, row in enumerate(canvas._rows) if row.get("svc") == "YouTube")
    x = canvas._content_w() - TOGGLE_W - TG_RIGHT + TOGGLE_W // 2
    y = canvas._tops[row_i] + ROW_H // 2 - canvas._offset
    changed = []
    canvas.serviceToggled.connect(lambda svc, on: changed.append((svc, on)))

    QtTest.QTest.mouseClick(
        canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
        QtCore.QPoint(int(x), int(y)),
    )

    assert changed == [("YouTube", True)]
    assert canvas._on["YouTube"] is True
    assert not canvas.findChildren(QtCore.QVariantAnimation), (
        "Нажатие сервисного чекбокса не должно создавать анимацию движущейся ручки"
    )
    canvas.deleteLater()


def test_category_checkbox_tristate_and_no_knob_animation():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    QtTest = pytest.importorskip("PySide6.QtTest")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.service_canvas import (
        CATEGORY_CONTROL_W,
        HDR_H,
        ServiceCanvas,
    )

    canvas = ServiceCanvas(
        [("Медиа", "🎬", "#4d8dff", "#22d3ee",
          [("YouTube", "▶️"), ("Twitch", "🟣")])],
        bypass_map={"YouTube": "dpi", "Twitch": "dns"},
    )
    canvas.resize(500, 160)
    canvas.set_app_mode("combo")
    canvas.set_service_states({"YouTube": True})
    assert canvas._cat_pos("Медиа") == 0.5

    header_i = next(i for i, row in enumerate(canvas._rows) if row.get("cat") == "Медиа")
    x = canvas._content_w() - CATEGORY_CONTROL_W - 10 + CATEGORY_CONTROL_W // 2
    y = canvas._tops[header_i] + HDR_H // 2 - canvas._offset
    toggled = []

    def apply_category(cat, on):
        toggled.append((cat, on))
        canvas.set_service_states({svc: on for svc in canvas._cat_svcs(cat)})

    canvas.categoryToggled.connect(apply_category)

    QtTest.QTest.mouseClick(
        canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
        QtCore.QPoint(int(x), int(y)),
    )
    assert toggled == [("Медиа", True)]
    assert canvas._cat_pos("Медиа") == 1.0
    assert canvas._on["YouTube"] is True and canvas._on["Twitch"] is True
    assert not canvas.findChildren(QtCore.QVariantAnimation), (
        "Нажатие группового чекбокса не должно создавать анимацию ручки"
    )

    QtTest.QTest.mouseClick(
        canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
        QtCore.QPoint(int(x), int(y)),
    )
    assert toggled[-1] == ("Медиа", False)
    assert canvas._cat_pos("Медиа") == 0.0
    canvas.deleteLater()


def test_toggle_service_guard_in_routing():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.engine_adapter import get_engine
    from umbranet.views.routing import RoutingView

    engine = get_engine()
    old_mode = engine.config.get("dpi_mode")
    old_domains = list(engine.config.get("routed_domains") or [])
    view = RoutingView()
    try:
        engine.config["dpi_mode"] = "off"  # dns_only
        engine.config["routed_domains"] = []
        view._toggle_service("YouTube", True)
        assert "youtube.com" not in engine.config["routed_domains"]
        view._toggle_service("ChatGPT / OpenAI", True)
        assert "chatgpt.com" in engine.config["routed_domains"]
        engine.config["dpi_mode"] = "combo"
        view._toggle_service("YouTube", True)
        assert "youtube.com" in engine.config["routed_domains"]
    finally:
        engine.config["dpi_mode"] = old_mode
        engine.config["routed_domains"] = old_domains
        view.deleteLater()
