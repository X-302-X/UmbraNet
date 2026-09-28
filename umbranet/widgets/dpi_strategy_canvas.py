"""
UmbraNet — канвас списка DPI-стратегий (радио-выбор, один paintEvent).

Замена QScrollArea + карточек-QFrame из dpi_strategy_list.py. Логика
выбора стратегии (WinWS-перезапуск и т.д.) остаётся в DpiStrategyList.

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath

from umbranet import theme
from umbranet.widgets.row_canvas import RowCanvas

ROW_H = 48        # как в оригинале
STRIDE = 53       # 48 + зазор 5


class DpiStrategyCanvas(RowCanvas):
    """Рисует строки-стратегии: ● название / описание (до 2 строк)."""

    rowClicked = Signal(str)   # id стратегии

    ROW_H = ROW_H
    STRIDE = STRIDE

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []   # {key,name,desc,active}

        base = self.font()
        self._f_dot = QFont(base); self._f_dot.setPixelSize(10)
        self._f_name = QFont(base); self._f_name.setPixelSize(12); self._f_name.setBold(True)
        self._f_desc = QFont(base); self._f_desc.setPixelSize(10)
        self._fm_desc = QFontMetrics(self._f_desc)

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
        return tuple((r["key"], r["active"]) for r in self._rows)

    # ── отрисовка ──

    def paint_row(self, p: QPainter, i: int, y: int, hover: bool):
        it = self._rows[i]
        active = bool(it.get("active"))
        w = self.width()
        h = ROW_H

        path = QPainterPath()
        path.addRoundedRect(0.5, y + 0.5, w - 1, h - 1, 9, 9)
        if active:
            g = QLinearGradient(0, y, 0, y + h)
            g.setColorAt(0.0, theme.qc(theme.CARD_TOP))
            g.setColorAt(1.0, theme.qc(theme.ROW_BG))
            p.fillPath(path, g)
            p.setPen(self._pen(theme.ACCENT))
        else:
            p.fillPath(path, QBrush(theme.qc(theme.ROW_BG)))
            p.setPen(self._pen(theme.ACCENT if hover else theme.BORDER))
        p.drawPath(path)

        # ● точка
        p.setFont(self._f_dot)
        p.setPen(self._pen(theme.GREEN if active else theme.MUTED))
        p.drawText(10, y + 1, 14, 16, int(Qt.AlignLeft | Qt.AlignVCenter), "●")

        # название
        p.setFont(self._f_name)
        p.setPen(self._pen(theme.TEXT))
        p.drawText(26, y + 1, w - 26 - 10, 16,
                   int(Qt.AlignLeft | Qt.AlignVCenter), it.get("name", ""))

        # описание до 2 строк
        p.setFont(self._f_desc)
        p.setPen(self._pen("#b2b3d6"))
        lines = self._wrap2(it.get("desc", ""), self._fm_desc, w - 20)
        ty = y + 18
        for ln in lines[:2]:
            p.drawText(10, ty, w - 20, 14, int(Qt.AlignLeft | Qt.AlignVCenter), ln)
            ty += 14
