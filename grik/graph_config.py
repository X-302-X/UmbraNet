"""
UmbraNet — grik: окно настроек графика пинга.

Частота обновления, вид графика, сетка, высота. Вынесено из главного
меню вместе с графиком (см. grik/panel.py). Вид окна 1:1 с прежним.

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from umbranet import theme


class _NoWheelComboBox(QComboBox):
    """ComboBox без случайного переключения колесом мыши."""

    def wheelEvent(self, event):
        event.ignore()


class _NoWheelSlider(QSlider):
    """Слайдер настроек без случайного изменения колесом мыши."""

    def wheelEvent(self, event):
        event.ignore()


class GraphConfigDialog(QDialog):
    """Настройки графиков пинга сети."""

    def __init__(self, current: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки графиков")
        self.setMinimumWidth(420)
        self.result = dict(current or {})
        self.setStyleSheet(f"QDialog{{background:{theme.BG};}}")
        self._build(current or {})

    def _build(self, cur: dict):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("📈  Настройки графиков пинга")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:16px;font-weight:700;background:transparent;border:none;")
        root.addWidget(title)

        # Вид графика
        row = QHBoxLayout()
        lbl = QLabel("Вид графика")
        lbl.setFixedWidth(150)
        lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        self._mode = _NoWheelComboBox()
        self._mode.addItem("Биржевые палочки", "bars")
        self._mode.addItem("Плавная линия", "smooth")
        self._mode.addItem("Угловатая линия", "angular")
        idx = self._mode.findData(cur.get("mode", "bars"))
        if idx >= 0:
            self._mode.setCurrentIndex(idx)
        self._mode.setStyleSheet(self._combo_qss())
        row.addWidget(lbl)
        row.addWidget(self._mode, 1)
        root.addLayout(row)

        self._grid_value = QLabel()
        self._grid = self._slider_row(root, "Размер сетки", int(cur.get("grid", 5)), 2, 10, self._grid_value)

        self._interval_value = QLabel()
        self._interval = self._slider_row(root, "Частота обновления", int(cur.get("interval_ms", 2000)) // 500, 2, 30, self._interval_value)

        self._height_value = QLabel()
        self._height = self._slider_row(root, "Высота графиков", int(cur.get("height", 140)), 90, 240, self._height_value)

        self._refresh_labels()
        self._grid.valueChanged.connect(lambda _=0: self._refresh_labels())
        self._interval.valueChanged.connect(lambda _=0: self._refresh_labels())
        self._height.valueChanged.connect(lambda _=0: self._refresh_labels())

        hint = QLabel("Рекомендуется интервал 2 секунды, чтобы не перегружать сетевой стек.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;")
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setFixedHeight(34)
        cancel.setStyleSheet(self._btn_qss(theme.CARD, theme.TEXT, border=True))
        cancel.clicked.connect(self.reject)
        ok = QPushButton("✓ Применить")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setFixedHeight(34)
        ok.setStyleSheet(self._btn_qss(theme.ACCENT, theme.WHITE))
        ok.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        root.addLayout(buttons)

    def _slider_row(self, parent, title: str, value: int, mn: int, mx: int, value_label: QLabel) -> QSlider:
        row = QHBoxLayout()
        lbl = QLabel(title)
        lbl.setFixedWidth(150)
        lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        slider = _NoWheelSlider(Qt.Horizontal)
        slider.setRange(mn, mx)
        slider.setValue(max(mn, min(mx, value)))
        slider.setMinimumHeight(30)
        slider.setStyleSheet(
            f"QSlider{{background:transparent;}}"
            f"QSlider::groove:horizontal{{"
            "height:8px;background:rgba(255,255,255,0.10);"
            f"border:1px solid {theme.BORDER};border-radius:4px;"
            "}"
            f"QSlider::sub-page:horizontal{{"
            f"background:{theme.grad(theme.ACCENT, theme.ACCENT3)};"
            "border-radius:4px;"
            "}"
            f"QSlider::add-page:horizontal{{"
            "background:rgba(255,255,255,0.045);border-radius:4px;"
            "}"
            f"QSlider::handle:horizontal{{"
            "width:20px;height:20px;margin:-7px 0;"
            f"background:{theme.ACCENT3};border:2px solid {theme.WHITE};"
            "border-radius:10px;"
            "}"
            f"QSlider::handle:horizontal:hover{{background:{theme.WHITE};border-color:{theme.ACCENT3};}}"
        )
        value_label.setFixedWidth(70)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value_label.setStyleSheet(f"color:{theme.ACCENT3};font-size:12px;font-family:Consolas;background:transparent;border:none;")
        row.addWidget(lbl)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        parent.addLayout(row)
        return slider

    def _refresh_labels(self):
        self._grid_value.setText(f"{self._grid.value()}x")
        self._interval_value.setText(f"{self._interval.value() * 500} мс")
        self._height_value.setText(f"{self._height.value()} px")

    def _accept(self):
        self.result = {
            "mode": self._mode.currentData(),
            "grid": self._grid.value(),
            "interval_ms": self._interval.value() * 500,
            "height": self._height.value(),
        }
        self.accept()

    def _combo_qss(self):
        return (
            f"QComboBox{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;min-height:32px;}}"
            f"QComboBox:hover{{border-color:{theme.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{theme.CARD};color:{theme.TEXT};"
            f"selection-background-color:{theme.ACCENT};border:1px solid {theme.BORDER};}}"
        )

    def _btn_qss(self, bg, fg, border=False):
        b = f"border:1px solid {theme.BORDER};" if border else "border:none;"
        return f"QPushButton{{background:{bg};color:{fg};{b}border-radius:9px;padding:0 14px;font-weight:600;}}"

