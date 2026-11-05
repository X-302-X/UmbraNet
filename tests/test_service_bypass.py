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


def test_chatgpt_is_vpn_youtube_is_dpi():
    assert service_bypass("ChatGPT / OpenAI") == "vpn"
    assert service_bypass("YouTube") == "dpi"
    assert service_bypass("Discord") == "dpi"


def test_every_ui_service_has_vpn_or_dpi():
    missing = []
    for name in UI_SERVICE_PROFILES:
        mode = service_bypass(name)
        if mode not in ("vpn", "dpi"):
            missing.append((name, mode))
    assert not missing, f"сервисы без vpn/dpi: {missing}"
    mapped = service_bypass_map()
    assert set(mapped) == set(UI_SERVICE_PROFILES)
    assert mapped["YouTube"] == "dpi"
    assert mapped["ChatGPT / OpenAI"] == "vpn"


def test_unknown_service_defaults_to_vpn():
    assert service_bypass("нет такого") == "vpn"
    assert service_bypass("") == "vpn"


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
    assert SERVICE_BYPASS["ChatGPT / OpenAI"] == "vpn"
    assert set(SERVICE_BYPASS) == set(SERVICES)


def test_allowed_in_mode():
    assert service_allowed_in_mode("YouTube", "vpn") is False
    assert service_allowed_in_mode("YouTube", "dpi") is True
    assert service_allowed_in_mode("YouTube", "combo") is True
    assert service_allowed_in_mode("ChatGPT / OpenAI", "vpn") is True
    assert service_allowed_in_mode("ChatGPT / OpenAI", "dpi") is False
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


def test_individual_service_click_repaints_its_category_checkbox():
    """Галочка «ВСЕ» обновляется сразу, а не после следующего общего refresh()."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    QtTest = pytest.importorskip("PySide6.QtTest")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.service_canvas import ROW_H, TG_RIGHT, TOGGLE_W, ServiceCanvas

    class RecordingCanvas(ServiceCanvas):
        def __init__(self, *args, **kwargs):
            self.updated_regions = []
            super().__init__(*args, **kwargs)

        def update(self, *args):
            self.updated_regions.extend(arg for arg in args if isinstance(arg, QtCore.QRect))
            return super().update(*args)

    canvas = RecordingCanvas(
        [("AI", "🤖", "#4d8dff", "#22d3ee", [("ChatGPT / OpenAI", "🤖")])],
        bypass_map={"ChatGPT / OpenAI": "dns"},
    )
    canvas.resize(500, 160)
    canvas.set_app_mode("combo")
    row_i = next(i for i, row in enumerate(canvas._rows) if row.get("svc") == "ChatGPT / OpenAI")
    point = QtCore.QPoint(
        int(canvas._content_w() - TOGGLE_W - TG_RIGHT + TOGGLE_W // 2),
        int(canvas._tops[row_i] + ROW_H // 2 - canvas._offset),
    )
    expected_header = canvas._category_checkbox_rect("AI")
    canvas.updated_regions.clear()

    QtTest.QTest.mouseClick(canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)

    assert canvas._on["ChatGPT / OpenAI"] is True
    assert expected_header in canvas.updated_regions
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


def test_category_checkbox_toggles_off_when_only_mixed_mode_services_are_locked():
    """После выбора доступных DNS-сервисов второй клик обязан всё снять."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    QtTest = pytest.importorskip("PySide6.QtTest")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.service_canvas import CATEGORY_CONTROL_W, HDR_H, ServiceCanvas

    canvas = ServiceCanvas(
        [("Смешанная", "🧩", "#4d8dff", "#22d3ee",
          [("YouTube", "▶️"), ("ChatGPT / OpenAI", "🤖")])],
        bypass_map={"YouTube": "dpi", "ChatGPT / OpenAI": "dns"},
    )
    canvas.resize(500, 160)
    canvas.set_app_mode("dns_only")
    header_i = next(i for i, row in enumerate(canvas._rows) if row.get("cat") == "Смешанная")
    point = QtCore.QPoint(
        int(canvas._content_w() - CATEGORY_CONTROL_W - 10 + CATEGORY_CONTROL_W // 2),
        int(canvas._tops[header_i] + HDR_H // 2 - canvas._offset),
    )
    toggled = []

    def apply_category(cat, on):
        toggled.append((cat, on))
        # Повторяем правило настоящего RoutingView: включаем только доступные,
        # выключаем все, включая ставшие недоступными в текущем режиме.
        updates = {}
        for svc in canvas._cat_svcs(cat):
            if on and canvas._locked(svc):
                continue
            updates[svc] = on
        canvas.set_service_states(updates)

    canvas.categoryToggled.connect(apply_category)
    QtTest.QTest.mouseClick(canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
    assert toggled == [("Смешанная", True)]
    assert canvas._on["ChatGPT / OpenAI"] is True
    assert canvas._on.get("YouTube", False) is False
    assert canvas._cat_pos("Смешанная") == 1.0

    QtTest.QTest.mouseClick(canvas, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
    assert toggled[-1] == ("Смешанная", False)
    assert canvas._on["ChatGPT / OpenAI"] is False
    assert canvas._cat_pos("Смешанная") == 0.0
    canvas.deleteLater()


def test_toggle_service_guard_in_routing():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.engine_adapter import get_engine
    from umbranet.views.routing import RoutingView

    engine = get_engine()
    old_mode = engine.config.get("routing_mode")
    old_domains = list(engine.config.get("routed_domains") or [])
    view = RoutingView()
    try:
        engine.config["routing_mode"] = "vpn"
        engine.config["routed_domains"] = []
        view._toggle_service("YouTube", True)
        assert "youtube.com" not in engine.config["routed_domains"]
        view._toggle_service("ChatGPT / OpenAI", True)
        assert "chatgpt.com" in engine.config["routed_domains"]
        engine.config["routing_mode"] = "combo"
        view._toggle_service("YouTube", True)
        assert "youtube.com" in engine.config["routed_domains"]
    finally:
        engine.config["routing_mode"] = old_mode
        engine.config["routed_domains"] = old_domains
        view.deleteLater()
