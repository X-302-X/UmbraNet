"""
UmbraNet — скруглённая панель, рисуемая в paintEvent.

Зачем: QFrame + QSS «background + border + border-radius» на правой панели
«Маршрутизации» (300×~700, r18) стоил ~3.5 мс на КАЖДЫЙ кадр — Qt
перегенерировал растровую маску скругления.

ПРОИЗВОДИТЕЛЬНОСТЬ (фикс «resize подёргивается на ноутбуках»):
раньше каждый кадр рисовал AA-путь на всю панель — на слабом CPU это
дорого при живом resize. Теперь панель собирается из готовых деталей:
  • верхняя полоса (radius+2 px, со скруглениями) — кэш-Pixmap по ширине,
  • нижняя полоса — она же, отражённая по вертикали,
  • боковые края центра — узкие AA-честные тайлы (заполняются мозаикой),
  • центр — fillRect.
Сглаживание живёт только при генерации кэша (один раз на ширину);
в кадре — ни одного AA-пути, только заливки и копирование пикселей.

DPI: все кэши рендерятся в физических пикселях с devicePixelRatio —
на экранах с масштабом 125-150% текст и рамки остаются чёткими
(фикс «текст нечёткий, цвет не тот»).

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QBrush, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtWidgets import QWidget

from umbranet import theme

# ширина бокового AA-тайла (в логических пикселях)
_EDGE_W = 3


class RoundedPanel(QWidget):
    """Контейнер, рисующий под детьми скруглённую карточку.

    Дети рисуются ПОВЕРХ — обычный layout, contentsMargins задаёт caller.
    ВАЖНО: у предков панели не должно быть QSS-фона (bare background) —
    он каскадом закрасит отрисовку (в UmbraNet таких предков нет).
    """

    def __init__(
        self,
        bg: str,
        border: str,
        radius: int = 18,
        border_w: float = 1.0,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._bg_s = bg
        self._border_s = border
        self._bg = theme.qc(bg)
        self._border = theme.qc(border)
        self._radius = radius
        self._border_w = border_w

        self._cap_top: QPixmap | None = None    # верхняя полоса (кэш по ширине)
        self._cap_bot: QPixmap | None = None    # она же, отражённая
        self._edge_l: QPixmap | None = None     # левый боковой тайл рамки
        self._edge_r: QPixmap | None = None     # правый боковой тайл рамки
        self._cap_for_w = -1
        self._cap_dpr = -1.0

    # ── генерация кэша деталей (один раз на ширину) ──────────────────────

    def _parts(self, w: int):
        dpr = self.devicePixelRatioF()
        if (self._cap_top is not None and self._cap_for_w == w
                and self._cap_dpr == dpr):
            return self._cap_top, self._cap_bot, self._edge_l, self._edge_r

        bw = self._border_w
        r = self._radius
        cap_h = r + 2
        # эталон: полный скруглённый прямоугольник высотой 2*cap_h + тайл
        ref_h = 2 * cap_h + 16
        pm = QPixmap(max(1, int(w * dpr + 0.5)), int(ref_h * dpr + 0.5))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(bw / 2.0, bw / 2.0, w - bw, ref_h - bw,
                            float(r), float(r))
        p.fillPath(path, QBrush(self._bg))
        pen = QPen(self._border)
        pen.setWidthF(bw)
        p.setPen(pen)
        p.drawPath(path)
        p.end()

        def dcopy(x, y, cw, ch):
            """Вырезка в физических пикселях с сохранением DPR."""
            t = pm.copy(int(x * dpr + 0.5), int(y * dpr + 0.5),
                        max(1, int(cw * dpr + 0.5)), max(1, int(ch * dpr + 0.5)))
            t.setDevicePixelRatio(dpr)
            return t

        self._cap_top = dcopy(0, 0, w, cap_h)
        self._edge_l = dcopy(0, cap_h, _EDGE_W, 8)
        self._edge_r = dcopy(w - _EDGE_W, cap_h, _EDGE_W, 8)
        # нижняя полоса — отражение верхней (рамка симметрична)
        self._cap_bot = self._cap_top.transformed(QTransform(1, 0, 0, -1, 0, 0))
        self._cap_bot.setDevicePixelRatio(dpr)
        self._cap_for_w = w
        self._cap_dpr = dpr
        return self._cap_top, self._cap_bot, self._edge_l, self._edge_r

    # ── отрисовка (каждый кадр — только быстрые операции) ───────────────

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w <= 2 or h <= 2:
            return
        r = self._radius
        cap_h = r + 2
        if h < 2 * cap_h + 2:
            # совсем низкая панель — редкий случай, честный AA-путь
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            bw = self._border_w
            path = QPainterPath()
            path.addRoundedRect(bw / 2.0, bw / 2.0, w - bw, h - bw,
                                float(r), float(r))
            p.fillPath(path, QBrush(self._bg))
            pen = QPen(self._border)
            pen.setWidthF(bw)
            p.setPen(pen)
            p.drawPath(path)
            return

        cap_top, cap_bot, edge_l, edge_r = self._parts(w)
        mid_y = cap_h
        mid_h = h - 2 * cap_h

        p = QPainter(self)
        # центр: заливка фоном + AA-честные боковые края (мозаикой)
        p.fillRect(QRect(_EDGE_W, mid_y, w - 2 * _EDGE_W, mid_h), QBrush(self._bg))
        p.drawTiledPixmap(QRect(0, mid_y, _EDGE_W, mid_h), edge_l)
        p.drawTiledPixmap(QRect(w - _EDGE_W, mid_y, _EDGE_W, mid_h), edge_r)
        # полосы со скруглениями
        p.drawPixmap(0, 0, cap_top)
        p.drawPixmap(0, h - cap_h, cap_bot)

    def set_colors(self, bg: str | None = None, border: str | None = None):
        if bg:
            self._bg = theme.qc(bg)
        if border:
            self._border = theme.qc(border)
        self._cap_top = None
        self._cap_for_w = -1
        self.update()
