"""
Прокрутка списков колесом: одно деление — пара строк, а не полсписка.
================================================================================

Было во всех трёх канвасах одно и то же:

    self._scroll(-event.angleDelta().y() * 3 // 2)

Одно деление колеса — 120 единиц angleDelta — прокручивало 180 px. В списке
сервисов шаг строки всего 32 px (service_canvas.ROW_STRIDE), то есть 5.6 строк
за щелчок: «чуть двинул колесо — пронесло на дофига». Заметнее всего это в
главном меню (сервисы — самый длинный список программы).

Стало: прокрутка считается в строках списка — WHEEL_ROWS_PER_NOTCH = 2 строки
за деление, с непрерывным масштабом для плавных тачпадов.

Запуск: python -m pytest tests/test_wheel_scroll.py
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from umbranet.widgets.wheel_scroll import (
    WHEEL_ROWS_PER_NOTCH,
    WHEEL_UNIT,
    wheel_delta_px,
)

APP = QApplication.instance() or QApplication([])


def send_wheel(canvas, notches: float = 1.0, dy_units: int | None = None) -> None:
    """Шлёт канвасу одно событие колеса «на себя» — вниз по списку.

    notches=1.0 — обычный щелчок (120 единиц); dy_units — точная величина для
    проверки плавных тачпадов. Знак выбран так, что offset списка растёт;
    обратное направление (вверх, к началу) проверяется отдельно.
    """
    dy = -WHEEL_UNIT * notches if dy_units is None else dy_units
    event = QWheelEvent(
        QPointF(60, 60), QPointF(60, 60),
        QPoint(0, 0), QPoint(0, int(dy)),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    )
    APP.sendEvent(canvas, event)


# ── 1. Чистая арифметика прокрутки ──────────────────────────────────────────

def test_one_notch_is_configured_rows():
    """Одно деление — ровно WHEEL_ROWS_PER_NOTCH строк любого списка."""
    assert wheel_delta_px(120, 32) == -32 * WHEEL_ROWS_PER_NOTCH      # сервисы
    assert wheel_delta_px(120, 42) == -42 * WHEEL_ROWS_PER_NOTCH      # диспетчер
    assert wheel_delta_px(120, 46) == -46 * WHEEL_ROWS_PER_NOTCH      # логи


def test_much_less_than_the_old_180px_ballistic_scroll():
    """В списке сервисов прокрутка стала в разы меньше прежней.

    Было `angleDelta * 3 // 2` = 180 px; было бы 5.6 строк при шаге 32 px.
    """
    old_px = 120 * 3 // 2
    new_px = -wheel_delta_px(120, 32)
    assert old_px == 180
    assert new_px <= old_px // 2, f"прокрутка всё ещё прыгает: {new_px} px"


def test_direction_follows_the_wheel():
    """Колесо от себя листает вверх (offset уменьшается), на себя — вниз."""
    assert wheel_delta_px(120, 32) < 0
    assert wheel_delta_px(-120, 32) > 0


def test_precision_touchpad_scales_continuously():
    """Плавный тачпад (мелкие delta) прокручивает пропорционально, без рывков."""
    full = abs(wheel_delta_px(120, 32))
    quarter = abs(wheel_delta_px(30, 32))
    assert quarter == full // 4
    # совсем мелкое движение — единицы пикселей, а не целая строка
    assert 0 < abs(wheel_delta_px(15, 32)) < 32


def test_degenerate_inputs_do_nothing():
    """Нулевая/горизонтальная прокрутка и битый шаг строки — не двигаем список."""
    assert wheel_delta_px(0, 32) == 0
    assert wheel_delta_px(120, 0) == 0
    assert wheel_delta_px(120, -5) == 0
    assert wheel_delta_px(120, 32, rows_per_notch=0) == 0


# ── 2. Настоящие канвасы ────────────────────────────────────────────────────

@pytest.fixture
def service_canvas():
    from umbranet.widgets.service_canvas import ROW_STRIDE, ServiceCanvas

    # Длинный каталог: категории × сервисы, чтобы список точно прокручивался.
    catalog = [
        (f"Категория {c}", "🎬", "#4d8dff", "#22d3ee",
         [(f"Сервис {c}-{s}", "▶️") for s in range(12)])
        for c in range(5)
    ]
    canvas = ServiceCanvas(catalog, bypass_map={})
    canvas.resize(500, 200)
    canvas.show()
    APP.processEvents()
    assert canvas._max_offset() > 0, "список короче окна — тест ничего не проверит"
    return canvas, ROW_STRIDE


def test_service_canvas_one_notch_scrolls_two_rows(service_canvas):
    """Главное меню: одно деление колеса — 2 строки, а не 5.6 как раньше."""
    canvas, ROW_STRIDE = service_canvas
    assert canvas._offset == 0
    send_wheel(canvas)
    assert canvas._offset == ROW_STRIDE * WHEEL_ROWS_PER_NOTCH, (
        f"после одного щелчка offset={canvas._offset}, "
        f"а ждём {ROW_STRIDE * WHEEL_ROWS_PER_NOTCH} px"
    )


def test_service_canvas_repeated_notches_are_even(service_canvas):
    """Каждый щелчок добавляет ровно столько же — прокрутка не «ускоряется»."""
    canvas, ROW_STRIDE = service_canvas
    step = ROW_STRIDE * WHEEL_ROWS_PER_NOTCH
    for i in range(1, 4):
        send_wheel(canvas)
        assert canvas._offset == step * i, f"щелчок {i}: offset={canvas._offset}"


def test_service_canvas_clamps_at_the_ends(service_canvas):
    """У краёв список не улетает за пределы содержимого."""
    canvas, _ = service_canvas
    for _ in range(500):
        send_wheel(canvas)
    assert canvas._offset == canvas._max_offset()
    for _ in range(500):
        send_wheel(canvas, notches=-1.0)   # колесо от себя — назад к началу
    assert canvas._offset == 0


def test_manual_canvas_one_notch_scrolls_two_cards():
    """«Диспетчер задач»: то же правило и тот же темп, что у сервисов."""
    from umbranet.widgets.manual_canvas import CARD_STRIDE, ManualCanvas

    canvas = ManualCanvas()
    canvas.set_items([
        {"name": f"example{i}.ru", "key": f"k{i}", "icon": "🌐", "badge": "домен",
         "badge_color": "#8b6dff", "display": f"example{i}.ru"}
        for i in range(30)
    ])
    canvas.resize(400, 200)
    canvas.show()
    APP.processEvents()
    assert canvas._max_offset() > 0
    send_wheel(canvas)
    assert canvas._offset == CARD_STRIDE * WHEEL_ROWS_PER_NOTCH


def test_row_canvas_uses_its_own_stride():
    """Логи и транспорты: строки выше, но темп тот же — 2 строки за деление."""
    from umbranet.widgets.row_canvas import RowCanvas

    class _Canvas(RowCanvas):
        ROW_H = 40
        STRIDE = 50

        def paint_row(self, painter, i, y, width):  # pragma: no cover - не рисуем
            pass

    canvas = _Canvas()
    canvas._rows_count = 40
    canvas.resize(400, 200)
    canvas.show()
    APP.processEvents()
    assert canvas._max_offset() > 0
    send_wheel(canvas)
    assert canvas._offset == 50 * WHEEL_ROWS_PER_NOTCH


def test_horizontal_wheel_does_not_move_lists(service_canvas):
    """Горизонтальная прокрутка (delta по X) список не дёргает."""
    canvas, _ = service_canvas
    event = QWheelEvent(
        QPointF(60, 60), QPointF(60, 60),
        QPoint(0, 0), QPoint(120, 0),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    )
    APP.sendEvent(canvas, event)
    assert canvas._offset == 0
