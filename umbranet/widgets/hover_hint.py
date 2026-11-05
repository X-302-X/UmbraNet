"""Stable in-app hover hints without Qt's short-lived native tooltip behavior."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel

from umbranet import theme

SHOW_DELAY_MS = 180
HIDE_DELAY_MS = 700
_MAX_HINT_WIDTH = 340


class _HintPopup(QLabel):
    """A non-interactive tooltip window whose lifetime belongs to its badge."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setWordWrap(True)
        self.setFixedWidth(_MAX_HINT_WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            f"QLabel{{background:{theme.CARD_DARK};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:8px 10px;"
            "font-size:12px;}"
        )

    def show_for(self, anchor: QLabel, text: str) -> None:
        self.setText(text)
        self.adjustSize()
        position = anchor.mapToGlobal(QPoint(anchor.width() + 7, anchor.height() + 5))
        screen = QGuiApplication.screenAt(position) or anchor.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = min(position.x(), area.right() - self.width())
            y = position.y()
            if y + self.height() > area.bottom():
                y = max(area.top(), anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 5)
            position = QPoint(max(area.left(), x), y)
        self.move(position)
        self.show()
        self.raise_()


class HoverHintBadge(QLabel):
    """Round question-mark badge with a predictable, movement-tolerant hint."""

    def __init__(self, text: str, parent=None):
        super().__init__("?", parent)
        self._hint_text = str(text or "")
        self._popup = _HintPopup()
        self.destroyed.connect(self._popup.deleteLater)
        self._show_delay = QTimer(self)
        self._show_delay.setSingleShot(True)
        self._show_delay.setInterval(SHOW_DELAY_MS)
        self._show_delay.timeout.connect(self._show_hint)
        self._hide_delay = QTimer(self)
        self._hide_delay.setSingleShot(True)
        self._hide_delay.setInterval(HIDE_DELAY_MS)
        self._hide_delay.timeout.connect(self._hide_hint)
        self.setAccessibleName("Подсказка")
        self.setAccessibleDescription(self._hint_text)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.WhatsThisCursor)
        self.setStyleSheet(
            f"color:{theme.MUTED};font-size:12px;font-weight:800;"
            f"background:{theme.INPUT_BG};border:1px solid {theme.BORDER};border-radius:9px;"
        )

    def enterEvent(self, event):
        self._hide_delay.stop()
        if not self._popup.isVisible():
            self._show_delay.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._show_delay.stop()
        # A short grace period makes a minor cursor movement non-destructive.
        self._hide_delay.start()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self._hide_delay.stop()
        super().mouseMoveEvent(event)

    def _show_hint(self) -> None:
        if self.underMouse():
            self._popup.show_for(self, self._hint_text)

    def _hide_hint(self) -> None:
        if not self.underMouse():
            self._popup.hide()

    def hideEvent(self, event):
        self._show_delay.stop()
        self._hide_delay.stop()
        self._popup.hide()
        super().hideEvent(event)
