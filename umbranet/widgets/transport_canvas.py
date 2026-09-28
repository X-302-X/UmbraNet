"""
UmbraNet — канвас списка транспортов DNS (радио-выбор, один paintEvent).

Замена QScrollArea + карточек-QFrame из transport_list.py: строки рисуются
сами, состояния active/normal/unavail и hover — тоже. Логика выбора живёт
в TransportList (контроллере).

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from umbranet import theme
from umbranet.widgets.row_canvas import RowCanvas

ROW_H = 75        # как в оригинале: имя + описание в 2 строки
STRIDE = 81       # 75 + зазор 6


class TransportCanvas(RowCanvas):
    """Рисует строки-транспорты: ● название [бейдж] / описание."""

    rowClicked = Signal(str)   # key транспорта

    ROW_H = ROW_H
    STRIDE = STRIDE

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []   # {key,label,desc,badge,state}

        base = self.font()
        self._f_dot = QFont(base); self._f_dot.setPixelSize(11)
        self._f_name = QFont(base); self._f_name.setPixelSize(12); self._f_name.setBold(True)
        self._f_desc = QFont(base); self._f_desc.setPixelSize(11)
        self._f_badge = QFont(base); self._f_badge.setPixelSize(10)
        self._fm_name = QFontMetrics(self._f_name)
        self._fm_desc = QFontMetrics(self._f_desc)
        self._fm_badge = QFontMetrics(self._f_badge)

    # ── API ──

    def set_rows(self, rows: list[dict]):
        self._rows = list(rows)
        self._rows_count = len(self._rows)
        self._hover = -1
        self._clamp_offset()
        self.update()

    def _row_clicked(self, i: int):
        self.rowClicked.emit(self._rows[i]["key"])

    def _rows_state_key(self):
        return tuple((r["key"], r["state"], r["badge"]) for r in self._rows)

    def _cursor_for_row(self, i: int):
        # недоступный транспорт — «запрещено», как в старой версии
        if self._rows[i].get("state") == "unavail":
            return Qt.ForbiddenCursor
        return Qt.PointingHandCursor

    # ── отрисовка ──

    def paint_row(self, p: QPainter, i: int, y: int, hover: bool):
        it = self._rows[i]
        state = it.get("state", "normal")
        w = self.width()
        h = ROW_H

        # карточка
        path = QPainterPath()
        path.addRoundedRect(0.5, y + 0.5, w - 1, h - 1, 10, 10)
        if state == "active":
            g = QLinearGradient(0, y, 0, y + h)
            g.setColorAt(0.0, theme.qc(theme.CARD_TOP))
            g.setColorAt(1.0, theme.qc(theme.ROW_BG))
            p.fillPath(path, g)
            p.setPen(self._pen(theme.ACCENT))
        elif state == "unavail":
            p.fillPath(path, QBrush(theme.qc(theme.CARD_DARK)))
            p.setPen(self._pen(theme.BORDER))
        else:
            p.fillPath(path, QBrush(theme.qc(theme.ROW_BG)))
            p.setPen(self._pen(theme.ACCENT if hover else theme.BORDER))
        p.drawPath(path)

        # цвета текста по состоянию
        if state == "active":
            c_dot, c_name, c_desc = theme.GREEN, theme.TEXT, "#b2b3d6"
        elif state == "unavail":
            c_dot, c_name, c_desc = theme.BORDER, theme.MUTED, "#7c7d9c"
        else:
            c_dot, c_name, c_desc = theme.MUTED, theme.TEXT, "#b2b3d6"

        # ● точка
        p.setFont(self._f_dot)
        p.setPen(self._pen(c_dot))
        p.drawText(10, y + 4, 16, 18, int(Qt.AlignLeft | Qt.AlignVCenter), "●")

        # название
        p.setFont(self._f_name)
        p.setPen(self._pen(c_name))
        badge = it.get("badge", "")
        badge_w = self._fm_badge.horizontalAdvance(badge) + 8 if badge else 0
        p.drawText(28, y + 4, w - 28 - 10 - badge_w, 18,
                   int(Qt.AlignLeft | Qt.AlignVCenter), it.get("label", ""))

        # бейдж справа («сейчас: …», «замер...», «⚙ нужен aioquic»…)
        if badge:
            p.setFont(self._f_badge)
            p.setPen(self._pen(theme.MUTED if state == "unavail" else theme.ACCENT3))
            p.drawText(w - 10 - badge_w + 4, y + 5, badge_w, 16,
                       int(Qt.AlignRight | Qt.AlignVCenter), badge)

        # описание (до 2 строк)
        p.setFont(self._f_desc)
        p.setPen(self._pen(c_desc))
        lines = self._wrap2(it.get("desc", ""), self._fm_desc, w - 20)
        ty = y + 24
        for ln in lines[:2]:
            p.drawText(10, ty, w - 20, 16, int(Qt.AlignLeft | Qt.AlignVCenter), ln)
            ty += 16
