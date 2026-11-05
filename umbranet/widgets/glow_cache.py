# -*- coding: utf-8 -*-
"""
UmbraNet — кэшированное свечение для всегда-видимых мелких виджетов.

Зачем: theme.glow() вешает ЖИВОЙ QGraphicsDropShadowEffect — гауссов blur
пересчитывается при каждой перерисовке виджета. На кнопке активной вкладки
сайдбара и кнопках режимов в шапке эффект висит постоянно: при живом resize
окна на Windows (окно инвалидируется целиком) это лишние миллисекунды
гауссова блюра на каждый кадр — одна из причин подёргивания.

Как: свечение строится ТЕМ ЖЕ QGraphicsDropShadowEffect, но РОВНО ОДИН РАЗ
на размер виджета, и рисуется в paintEvent родителя ПОД виджетом (до того,
как Qt отрисует детей). Силуэт для тени берём РЕАЛЬНЫЙ — grab() самого
виджета: тень повторяет живой эффект пиксель в пиксель, включая
полупрозрачные края (у кнопок фон полупрозрачный, и живой эффект давал
слабую тень — прямоугольный силуэт делал бы её заметно жирнее).
Сам grab из результата вырезается клипом: видна только тень СНАРУЖИ
виджета; под кнопкой потеря — доли процента просвечивания (невидимо).

Кэш живёт, пока жив виджет (WeakKeyDictionary) и обновляется при смене
его размера. Кнопки сайдбара/шапки не меняют размер при resize окна,
поэтому в кадре остаётся только drawPixmap.

DPI: кэш 1× — свечение это блюр без текста и резких краёв, апскейл
на экранах 125–150% невидим (тот же вывод, что у GlowWrap правой панели).

Автор: X-302-X / UmbraNet_Official, GPLv3.
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPixmap, QRegion
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QWidget,
)

# grab-силуэт виджета (по слабой ссылке): (w, h, src_pixmap)
_SRC: "weakref.WeakKeyDictionary[QWidget, tuple[int, int, QPixmap]]" = (
    weakref.WeakKeyDictionary())
# готовая тень: (widget, w, h, color, blur, dy, alpha) -> (pm, pad_x, pad_top)
_SHADOW: dict[tuple, tuple[QPixmap, int, int]] = {}


def _build_shadow(src: QPixmap, color: str, blur: int, dy: int,
                  alpha: int, radius: int | None = None
                  ) -> tuple[QPixmap, int, int]:
    pad_x = blur + 6               # поля под блюр по бокам
    pad_top = blur + 6             # сверху
    pad_bot = blur + 6 + abs(dy)   # снизу (тень смещена на dy)
    w, h = src.width(), src.height()
    W, H = w + 2 * pad_x, h + pad_top + pad_bot

    qc = QColor(color)
    qc.setAlpha(alpha)
    scene = QGraphicsScene()
    if radius is None:
        item = QGraphicsPixmapItem(src)
        item.setPos(float(pad_x), float(pad_top))
    else:
        path = QPainterPath()
        path.addRoundedRect(QRectF(float(pad_x), float(pad_top),
                                   float(w), float(h)),
                            float(radius), float(radius))
        item = QGraphicsPathItem(path)
        item.setBrush(QBrush(qc))
        item.setPen(Qt.NoPen)
    scene.addItem(item)

    eff = QGraphicsDropShadowEffect()
    eff.setBlurRadius(float(blur))
    eff.setColor(qc)
    eff.setOffset(0.0, float(dy))
    item.setGraphicsEffect(eff)

    pm = QPixmap(W, H)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    scene.render(p, QRectF(0.0, 0.0, float(W), float(H)),
                 QRectF(0.0, 0.0, float(W), float(H)))
    p.end()
    return pm, pad_x, pad_top


def paint_widget_glow(p: QPainter, widget: QWidget, color: str,
                      blur: int = 15, dy: int = 2, alpha: int = 80,
                      host: QWidget | None = None,
                      clip_widget: bool = True,
                      radius: int | None = None) -> None:
    """Нарисовать свечение вокруг виджета.

    Вызывать из paintEvent виджета host (по умолчанию — родитель widget):
    тень рисуется ДО детей, сам виджет ляжет поверх.

    radius=None — силуэт тени берём из grab() виджета (точно повторяет
    живой эффект, подходит для полупрозрачных виджетов: кнопки сайдбара).
    radius=R — силуэт = скруглённый прямоугольник R (для НЕпрозрачных
    QSS-кнопок: grab() у QPushButton заливает углы фоном, и силуэт
    получается прямоугольным — а живой эффект на экране имел скругление).

    clip_widget=True — вырезать область виджета из отрисовки (для
    полупрозрачных). clip_widget=False — рисовать пиксмап целиком
    (непрозрачная кнопка перекроет предмет; тень просвечивает сквозь
    AA-кромку ровно как у живого эффекта).
    """
    if widget is None or not widget.isVisible():
        return
    host = host or widget.parentWidget()
    if host is None:
        return

    w, h = widget.width(), widget.height()
    if w <= 0 or h <= 0:
        return

    if radius is None:
        # свежий силуэт (grab), только если размера нет в кэше
        cached = _SRC.get(widget)
        if cached is None or cached[0] != w or cached[1] != h:
            src = widget.grab()
            _SRC[widget] = (w, h, src)
        else:
            src = cached[2]
    else:
        src = QPixmap(w, h)  # заглушка размера (силуэт строится внутри)

    key = (id(widget), w, h, str(color), blur, dy, alpha, radius)
    entry = _SHADOW.get(key)
    if entry is None:
        entry = _build_shadow(src, color, blur, dy, alpha, radius)
        _SHADOW[key] = entry
    pm, pad_x, pad_top = entry

    pos = widget.mapTo(host, QPoint(0, 0))
    x0, y0 = pos.x() - pad_x, pos.y() - pad_top
    if clip_widget:
        # клип: всё поле тени, КРОМЕ самого виджета (его рисует Qt поверх)
        reg = QRegion(x0, y0, pm.width(), pm.height()).subtracted(
            QRegion(pos.x(), pos.y(), w, h))
        p.save()
        p.setClipRegion(reg)
        p.drawPixmap(x0, y0, pm)
        p.restore()
    else:
        p.drawPixmap(x0, y0, pm)
