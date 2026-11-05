"""
UmbraNet — кэшированное свечение под карточкой-панелью.

Зачем: theme.glow() вешает QGraphicsDropShadowEffect, который пересчитывает
гауссов blur на КАЖДЫЙ кадр. На правой панели «Маршрутизации» (300×~760,
blur=22) это стоило ~3.7 мс на кадр — почти половина всего рендера вкладки.

Как: свечение генерируется РОВНО ОДИН РАЗ тем же QGraphicsDropShadowEffect,
но через offscreen-сцену, и раскладывается на три части:
  верхний «колпачок» (углы + скругление)  — рисуется как есть,
  середина (боковое свечение по вертикали) — РАСТЯГИВАЕТСЯ на любую высоту,
  нижний «колпачок»                        — рисуется как есть.
Тень в середине формы не зависит от высоты панели, поэтому растяжка
корректна. Итог: кэш не пересобирается при resize ВООБЩЕ (панель фиксированной
ширины, высота меняется только у середины).

DPI (важный урок фикса «фон съехал»): кэш здесь НАМЕРЕННО 1×, без
devicePixelRatio. Свечение — гауссов блюр, у него нет текста и резких краёв,
поэтому апскейл Qt'ом на экранах 125–150% невидим — а вид получается
в точности как в одобренной фазе 3. Попытка кэшировать в физических
пикселях провалилась: drawPixmap(цель, pm, источник) у DPR-pixmap берёт
source-rect в ФИЗИЧЕСКИХ пикселях (проверено экспериментом), из-за чего
колпачки вырезались из середины панели и снизу вкладки появлялась
сплошная фиолетовая полоса. Текст и рамки (RowCanvas, RoundedPanel)
кэшируются в физических пикселях — там DPR обязателен; здесь — нет.

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGraphicsPathItem,
    QGraphicsScene,
    QVBoxLayout,
    QWidget,
)

# высота «колпачков»: 3×blur с запасом на смещение dy
_CAP = 72
# эталонная высота панели, для которой генерируется полный образец
_REF_H = 2 * _CAP + 80


class GlowWrap(QWidget):
    """Прозрачная обёртка: рисует кэшированное свечение ПОД внутренней панелью.

    Параметры свечения 1:1 с theme.glow(color, blur, dy, alpha).
    Поля вокруг панели — место под блюр: слева/сверху полные, справа/снизу
    можно поменьше (там обычно край вкладки, тень всё равно обрезалась).
    """

    def __init__(
        self,
        inner: QWidget,
        color: str,
        blur: int = 22,
        dy: int = 6,
        alpha: int = 150,
        radius: int = 18,
        margins: tuple[int, int, int, int] = (40, 40, 28, 24),
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._qcolor = QColor(color)
        self._qcolor.setAlpha(alpha)
        self._blur = blur
        self._dy = dy
        self._radius = radius
        self._margins = margins

        lay = QVBoxLayout(self)
        lay.setContentsMargins(*margins)
        lay.addWidget(inner)
        self._inner = inner

        self.setStyleSheet("background:transparent;border:none;")   # защита от QSS-каскада
        self._cache: QPixmap | None = None
        self._cache_for_w = -1      # кэш строится по ШИРИНЕ панели
        self.regen_count = 0        # диагностика: сколько раз перегенерировали

    # ── генерация свечения (один раз на ширину) ──────────────────────────

    def _regen_cache(self, iw: int):
        self.regen_count += 1
        ml, mt, mr, mb = self._margins
        w = iw + ml + mr
        ih = _REF_H
        h = ih + mt + mb

        # Тот же эффект, что рисовал Qt вокруг панели — но один раз.
        scene = QGraphicsScene()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.0, 0.0, float(iw), float(ih)),
                            float(self._radius), float(self._radius))
        item = QGraphicsPathItem(path)
        item.setBrush(QBrush(self._qcolor))   # перекроется панелью — не важно
        item.setPen(Qt.NoPen)
        scene.addItem(item)

        eff = QGraphicsDropShadowEffect()
        eff.setBlurRadius(float(self._blur))
        eff.setColor(self._qcolor)
        eff.setOffset(0.0, float(self._dy))
        item.setGraphicsEffect(eff)

        # кэш 1×: блюр не имеет резких краёв, DPR здесь не нужен (см. докстринг)
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        scene.render(p, QRectF(0.0, 0.0, float(w), float(h)),
                     QRectF(-ml, -mt, float(w), float(h)))
        p.end()

        self._cache = pm
        self._cache_for_w = iw

    # ── события ──────────────────────────────────────────────────────────

    def _ensure_cache(self):
        iw = self._inner.width()
        if iw <= 0:
            return
        if self._cache is None or self._cache_for_w != iw:
            self._regen_cache(iw)

    def paintEvent(self, event):
        self._ensure_cache()
        pm = self._cache
        if pm is None:
            return
        w, h = self.width(), self.height()
        if h >= 2 * _CAP:
            p = QPainter(self)
            # верх
            p.drawPixmap(QRect(0, 0, w, _CAP), pm,
                         QRect(0, 0, pm.width(), _CAP))
            # середина: полоска из центра эталона, растянутая по высоте
            mid_src = _CAP + (pm.height() - 2 * _CAP) // 2
            p.drawPixmap(QRect(0, _CAP, w, h - 2 * _CAP), pm,
                         QRect(0, mid_src, pm.width(), 8))
            # низ
            p.drawPixmap(QRect(0, h - _CAP, w, _CAP), pm,
                         QRect(0, pm.height() - _CAP, pm.width(), _CAP))
        else:
            # панель ниже колпачков — редкий случай, рисуем как есть
            p = QPainter(self)
            p.drawPixmap(0, 0, pm)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # высота меняется только у растягиваемой середины — кэш жив,
        # но перерисоваться надо (drawPixmap-геометрия другая)
        self.update()
