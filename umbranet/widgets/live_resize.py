# -*- coding: utf-8 -*-
"""Живой resize без дёрганья: заморозка контента окна на время изменения размера.

Проблема: при каждом шаге resize Qt перерисовывает окно ЦЕЛИКОМ — все
дочерние виджеты (у UmbraNet это ~170 виджетов с QSS-градиентами и
эффектами, ~15–20 мс на быстром ПК и 50+ мс на ноутбуке). Окно «дёргается».

Решение (классика для тяжёлых UI): как только начался resize —
  1) делаем СНИМОК всего окна (одна дорогая операция, один раз);
  2) запрещаем дочерним виджетам перерисовываться и замораживаем layout
     (контент остаётся в старой геометрии);
  3) поверх детей кладём невидимый для мыши слой-оверлей, который просто
     blit-ит снимок (доли миллисекунды);
  4) когда размер устоялся (нет resize-событий N мс) — снимаем оверлей,
     размораживаем layout, и контент ОДИН раз перестраивается под итог.

Во время перетаскивания оверлей РАСТЯГИВАЕТ снимок под текущий размер —
контент визуально следует за краем окна (лёгкое размытие до отпускания).

Для вкладок, где перерисовка уже дешёвая (телеграмизированная
«Маршрутизация»), заморозку отключают целиком — там resize живой
(см. GlowContainer.set_live_resize в app.py).

Автор: X-302-X / UmbraNet_Official
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget


class _FreezeOverlay(QWidget):
    """Прозрачный для мыши слой, рисующий замороженный снимок контента."""

    def __init__(self, parent: QWidget, snapshot: QPixmap):
        super().__init__(parent)
        self._snapshot = snapshot
        # Оверлей не должен перехватывать мышь (пусть события идут дальше).
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        p = QPainter(self)
        # Снимок РАСТЯГИВАЕТСЯ под текущий размер окна: контент «идёт»
        # следом за краем во время перетаскивания (чуть размытый — в отпущенном
        # состоянии разморозка перерисует его чётко под итоговый размер).
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawPixmap(self.rect(), self._snapshot)
        p.end()


class LiveResizeFreezer:
    """Замораживает контент окна-владельца на время живого resize.

    Использование (в resizeEvent владельца)::

        if visible:
            if not freezer.active:
                freezer.begin()     # снимок + заморозка (один раз)
            else:
                freezer.step()      # подтянуть оверлей под новый размер
    а по завершении (settle-таймер)::

        freezer.end()               # разморозка + один relayout
    """

    def __init__(self, owner: QWidget):
        self._owner = owner
        self._overlay: _FreezeOverlay | None = None
        self._frozen: list[QWidget] = []

    @property
    def active(self) -> bool:
        return self._overlay is not None

    def begin(self):
        """Снимок контента и заморозка. Повторные вызовы игнорируются."""
        if self._overlay is not None:
            return
        owner = self._owner

        # ПОРЯДОК КРИТИЧЕН: сначала выключаем layout, потом делаем снимок.
        # QWidget.render() активирует layout под ТЕКУЩИЙ размер окна — если
        # layout ещё включён, дети успеют переехать под новый размер ещё до
        # заморозки (проверено экспериментом). begin() вызывается из
        # resizeEvent: окно уже нового размера, дети — старой геометрии
        # (LayoutRequest ещё ждёт в очереди), поэтому снимок фиксирует
        # контент «как он выглядел» до начала изменения размера.
        layout = owner.layout()
        if layout is not None:
            layout.setEnabled(False)

        dpr = owner.devicePixelRatioF()
        pm = QPixmap(max(1, int(owner.width() * dpr)), max(1, int(owner.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        owner.render(pm)

        # Дети больше не тратят время на отрисовку — их место закроет оверлей.
        self._frozen = [c for c in owner.children() if isinstance(c, QWidget)]
        for child in self._frozen:
            child.setUpdatesEnabled(False)

        self._overlay = _FreezeOverlay(owner, pm)
        self._overlay.setGeometry(owner.rect())
        self._overlay.raise_()
        self._overlay.show()

    def step(self):
        """Очередной шаг resize: оверлей растёт/уменьшается вместе с окном."""
        if self._overlay is not None:
            self._overlay.setGeometry(self._owner.rect())

    def end(self):
        """Разморозка: вернуть обновления детям, один relayout под итог."""
        if self._overlay is None:
            return
        self._overlay.hide()
        self._overlay.deleteLater()
        self._overlay = None

        for child in self._frozen:
            child.setUpdatesEnabled(True)
        self._frozen = []

        layout = self._owner.layout()
        if layout is not None:
            layout.setEnabled(True)
            layout.invalidate()
            layout.activate()
