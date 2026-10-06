"""Виджет лечения «Сети и диагностики» (2026-10-06).

Пожелание пользователя: кнопка «📋 Лог» ведёт на вкладку «Логи» с категорией
«🔧 Починки» (лечение), а кнопка «Уведомления» — переключатель с видимым
состоянием (вкл/выкл), который сохраняется между запусками.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def net_view():
    from umbranet.views.network import NetworkView

    view = NetworkView()
    for _ in range(4):
        APP.processEvents()
    yield view
    # Не гасим модуль с крутящимся QThread (иначе падает следующий модуль).
    for name in ("_health_worker", "_doctor_worker", "_report_worker",
                 "_bogus_worker", "_restore_worker"):
        w = getattr(view, name, None)
        if w is not None and w.isRunning():
            w.wait(3000)


def test_log_button_requests_doctor_category(net_view):
    got: list[str] = []
    net_view.openLogRequested.connect(got.append)
    net_view._btn_open_log.click()
    assert got == ["fixed"], "кнопка «Лог» должна открывать категорию лечения («Починки»)"


def test_notify_toggle_visible_state_and_persistence(net_view):
    import ui_state
    from umbranet.engine_adapter import get_doctor_notify

    old_path = ui_state.state_path()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ui_state.set_state_path(os.path.join(tmp, "umbranet_ui.json"))
            assert get_doctor_notify() is True, "по умолчанию уведомления включены"

            net_view._btn_notify.setChecked(False)
            net_view._toggle_notify()
            assert get_doctor_notify() is False, "галочка не сохранилась"
            assert "выкл" in net_view._btn_notify.text(), "состояние не видно на кнопке"

            net_view._btn_notify.setChecked(True)
            net_view._toggle_notify()
            assert get_doctor_notify() is True
            assert "вкл" in net_view._btn_notify.text()
    finally:
        ui_state.set_state_path(old_path)
