"""
UmbraNet - кастомный тумблер-пилюля (PySide6).

Настоящий кликабельный переключатель с анимированным бегунком.
Поддерживает три состояния отображения:
  • выкл  — серый, бегунок слева;
  • вкл   — зелёный, бегунок справа;
  • partial («частично») — оранжевый, бегунок посередине
    (используется для категории, где включена ЧАСТЬ сервисов).

Сигнал toggled(bool) — как у QCheckBox. Клик из partial трактуется как
«включить всё» (-> True).
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from umbranet import theme


class Toggle(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked: bool = False):
        super().__init__()
        self._checked = checked
        self._partial = False
        self._offset = 1.0 if checked else 0.0  # 0..1 позиция бегунка
        self.setFixedSize(46, 26)
        self.setCursor(Qt.PointingHandCursor)

        self._anim = QPropertyAnimation(self, b"offset")
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)

    # ── свойство для анимации ──
    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, v: float):
        self._offset = v
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    # ── публичное ──
    def isChecked(self) -> bool:
        return self._checked

    def isPartial(self) -> bool:
        return self._partial

    def setChecked(self, checked: bool, animate: bool = True):
        """Устанавливает состояние тумблера.

        animate=False — мгновенная смена без анимации.
        Используется при программном обновлении UI (refresh, blockSignals)
        чтобы 30+ тумблеров не запускали анимации одновременно.
        """
        self._partial = False
        self._checked = checked
        target = 1.0 if checked else 0.0
        if not animate or abs(self._offset - target) < 0.01:
            # Мгновенно — без анимации
            self._anim.stop()
            self._offset = target
            self.update()
        else:
            self._animate_to(target)

    def setPartial(self, animate: bool = True):
        """Промежуточное состояние (оранжевое, бегунок посередине)."""
        self._partial = True
        self._checked = False
        if not animate or abs(self._offset - 0.5) < 0.01:
            self._anim.stop()
            self._offset = 0.5
            self.update()
        else:
            self._animate_to(0.5)

    def _animate_to(self, target: float):
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(target)
        self._anim.start()

    # ── взаимодействие ──
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # из partial или off -> включаем всё; из on -> выключаем
            new_state = not self._checked
            self._partial = False
            self._checked = new_state
            self._animate_to(1.0 if new_state else 0.0)
            self.toggled.emit(new_state)
            # Не пробрасываем клик родителю. Иначе тумблер в заголовке
            # Collapsible одновременно раскрывает/сворачивает папку.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ── отрисовка ──
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        if self._partial:
            track = QColor(theme.ORANGE)
        else:
            # Цвет выключенного состояния задаётся темой, включая светлую палитру.
            off = QColor(theme.MUTED)
            on = QColor(theme.GREEN)
            t = self._offset
            track = QColor(
                int(off.red() + (on.red() - off.red()) * t),
                int(off.green() + (on.green() - off.green()) * t),
                int(off.blue() + (on.blue() - off.blue()) * t),
            )
        p.setBrush(track)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 13, 13)

        # бегунок — сделали крупнее и белее
        d = self.height() - 4
        x = 2 + self._offset * (self.width() - d - 4)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, 2, d, d))
        p.end()
