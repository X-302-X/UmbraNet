"""
UmbraNet - сворачиваемая секция (PySide6).

Карточка с заголовком и скрываемым телом. В заголовке:
  • стрелка ▸/▾ (сворачивание),
  • иконка (опц.),
  • название,
  • опциональный виджет справа (например тумблер категории) — он ловит
    свои клики сам и НЕ сворачивает секцию.

Тело реализовано через QScrollArea: у неё настоящий viewport с аппаратным
клиппингом, поэтому контент обрезается БЕЗ «призраков»/дублей при анимации
(именно поэтому книжка маршрута, где контент уже лежал в QScrollArea, не
дрожала, а самодельный клиппер давал артефакты).
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.widgets.rounded_panel import RoundedPanel


class _Header(QWidget):
    """Кликабельная шапка секции."""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class Collapsible(RoundedPanel):
    toggled = Signal(bool)

    def __init__(self, title: str, icon: str = "", expanded: bool = False,
                 icon_grad: tuple[str, str] | None = None,
                 right_widget: QWidget | None = None):
        super().__init__(theme.CARD, theme.BORDER, radius=16)
        self._expanded = expanded
        # книжка не должна растягиваться по вертикали сверх своего содержимого —
        # иначе при анимации лишнее место распределяется и заголовок «плавает».
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        # ── шапка ──
        header = _Header()
        header.setCursor(Qt.PointingHandCursor)
        header.setFixedHeight(44)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(10)

        self._arrow = QLabel("▾" if expanded else "▸")
        self._arrow.setFixedWidth(14)
        self._arrow.setAlignment(Qt.AlignCenter)
        self._arrow.setStyleSheet(
            f"color:{theme.ACCENT};font-size:14px;font-weight:700;background:transparent;border:none;")
        hl.addWidget(self._arrow)

        if icon:
            ic = QLabel(icon)
            ic.setFixedWidth(26)
            ic.setAlignment(Qt.AlignCenter)
            ic.setStyleSheet("background:transparent;border:none;font-size:18px;")
            hl.addWidget(ic)

        self._title = QLabel(title)
        self._title.setStyleSheet(
            f"color:{theme.TEXT};font-size:14px;font-weight:700;background:transparent;border:none;")
        hl.addWidget(self._title)
        hl.addStretch()

        if right_widget is not None:
            hl.addWidget(right_widget)

        header.clicked.connect(self._toggle)
        root.addWidget(header, 0, Qt.AlignTop)

        # ── тело: QScrollArea как клиппер (аппаратное обрезание, без призраков)
        self._body = QScrollArea()
        self._body.setWidgetResizable(True)
        self._body.setFrameShape(QFrame.NoFrame)
        self._body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self._body.viewport().setStyleSheet("background:transparent;")

        self._content = QWidget()
        self._content.setStyleSheet("background:transparent;")
        self._body_lay = QVBoxLayout(self._content)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(6)
        self._body.setWidget(self._content)

        root.addWidget(self._body)

        # ── анимация: меняем фиксированную высоту области; контент держит свою
        #    высоту, viewport его обрезает (без сжатия и без дублей) ──
        self._anim = QPropertyAnimation(self._body, b"maximumHeight")
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)
        self._anim.finished.connect(self._on_anim_finished)

        if expanded:
            # Сначала скрываем с нулевой высотой — без мелькания,
            # затем через один event-loop цикл раскрываем до реальной высоты.
            # Порядок важен: setVisible(False) → setMax(0) → setVisible(True)
            # убирает артефакт "мелькнувшего" пустого QScrollArea.
            self._body.setMinimumHeight(0)
            self._body.setMaximumHeight(0)
            self._body.setVisible(False)
            QTimer.singleShot(0, self._open_after_init)
        else:
            self._body.setMinimumHeight(0)
            self._body.setMaximumHeight(0)
            self._body.setVisible(False)

    def _open_after_init(self):
        """Открывает тело после первого event-loop цикла — без мелькания."""
        self._body.setVisible(True)
        self._apply_open()

    # ── API ──
    def add_widget(self, w: QWidget):
        self._body_lay.addWidget(w)

    def add_layout(self, lay):
        self._body_lay.addLayout(lay)

    def _content_h(self) -> int:
        return self._content.sizeHint().height()

    def set_expanded(self, expanded: bool, animate: bool = True):
        self._expanded = expanded
        self._arrow.setText("▾" if expanded else "▸")

        if not animate:
            self._anim.stop()
            self._body.setVisible(expanded)
            self._apply_open() if expanded else self._apply_closed()
            return

        self._anim.stop()
        
        # ── Секрет идеальной плавной анимации без скачков текста ──
        # Отключаем автоматическое изменение размера контента на время анимации.
        # Это предотвращает любые попытки Qt сжать или сместить текст по вертикали!
        self._body.setWidgetResizable(False)
        target = max(self._content_h(), 1)
        self._content.setFixedHeight(target)
        
        if expanded:
            # Сначала устанавливаем height=0, показываем body, затем анимируем.
            # Порядок: setMin(0) → setMax(0) → setVisible(True) → старт анимации.
            # Это убирает мелькание когда body на долю кадра показывался
            # с неограниченной высотой до начала анимации.
            self._body.setMinimumHeight(0)
            self._body.setMaximumHeight(0)
            self._body.setVisible(True)
            self._anim.setStartValue(0)
            self._anim.setEndValue(target)
        else:
            cur = self._body.height()
            self._body.setMinimumHeight(0)
            self._anim.setStartValue(cur)
            self._anim.setEndValue(0)
        self._anim.start()

    def _on_anim_value(self, val):
        # держим min=max в процессе анимации, чтобы layout реально менял высоту
        self._body.setMinimumHeight(int(val))

    def _apply_open(self):
        # раскрыто: высота = высоте контента (фиксируем min=max под контент)
        h = max(self._content_h(), 1)
        self._body.setMinimumHeight(h)
        self._body.setMaximumHeight(h)
        
        # Разблокируем ограничения по высоте для гибкости после завершения
        self._content.setMinimumHeight(0)
        self._content.setMaximumHeight(16777215)

    def refit(self):
        """Пересчитать высоту раскрытой книжки под текущий контент
        (вызывать после программного изменения содержимого, напр. списка)."""
        if self._expanded and not self._anim.state():
            self._apply_open()

    def _apply_closed(self):
        self._body.setMinimumHeight(0)
        self._body.setMaximumHeight(0)
        self._body.setVisible(False)

    def _on_anim_finished(self):
        if self._expanded:
            self._apply_open()
        else:
            self._apply_closed()
            
        # Возвращаем адаптивность контента по ширине после завершения анимации
        self._body.setWidgetResizable(True)

    def is_expanded(self) -> bool:
        return self._expanded

    def _toggle(self):
        self.set_expanded(not self._expanded)
        self.toggled.emit(self._expanded)
