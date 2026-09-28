"""
UmbraNet - сворачиваемое боковое меню (PySide6 / Qt Widgets).

Два состояния с плавной анимацией ширины:
  • свёрнутое  (узкая плашка, только иконки)
  • развёрнутое (иконки + названия)

Активный пункт подсвечивается изящным градиентом с яркой левой гранью-индикатором.
Внизу — мини-карточка статуса ядра и кнопка сворачивания.

Сигнал navigate(key) эмитится при выборе раздела.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QMimeData,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QDrag, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.widgets.glow_cache import paint_widget_glow


@dataclass
class NavItem:
    key: str
    label: str
    emoji: str


class _LogoLabel(QLabel):
    """Логотип сайдбара: клики ловим сами (QLabel сигнала clicked не имеет)."""

    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class NavButton(QFrame):
    """Пункт меню: иконка на ФИКСИРОВАННОЙ позиции слева + скрываемый текст.

    Иконка всегда в одном и том же месте (не «улетает» при анимации ширины),
    меняется только видимость и обрезка текста справа.
    """

    clicked = Signal()

    _MIME = "application/x-umbranet-nav-key"

    def __init__(self, item: NavItem):
        super().__init__()
        self.item = item
        self._active = False
        self._expanded = True
        self._press_pos = QPoint()
        self._drag_started = False
        self._dragging = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(44)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # иконка — фиксированный квадрат слева (позиция не меняется)
        self._icon = QLabel(item.emoji)
        self._icon.setFixedWidth(theme.SIDEBAR_W_COLLAPSED - 24)  # = ширине свёрнутого минуса отступы
        self._icon.setAlignment(Qt.AlignCenter)
        self._icon.setStyleSheet("background:transparent;border:none;font-size:16px;")
        lay.addWidget(self._icon)

        # текст — справа, прячется в свёрнутом виде
        self._label = QLabel(item.label)
        self._label.setStyleSheet("background:transparent;border:none;font-size:13px;")
        lay.addWidget(self._label)

        # хвостовой стретч: иконка+текст всегда прижаты влево, лишнее место
        # уходит вправо. Так иконка НЕ «прыгает» в центр при сворачивании.
        lay.addStretch(1)

        self._render()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._drag_started = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton):
            return super().mouseMoveEvent(event)
        if self._drag_started:
            return
        if (event.position().toPoint() - self._press_pos).manhattanLength() < QApplication.startDragDistance():
            return

        self._drag_started = True
        self.set_dragging(True)

        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(self._MIME, self.item.key.encode("utf-8"))
        drag.setMimeData(mime)
        # Нативный полупрозрачный preview перетаскиваемой вкладки — как в браузере.
        drag.setPixmap(self.grab())
        drag.setHotSpot(self._press_pos)
        try:
            drag.exec(Qt.MoveAction)
        finally:
            self.set_dragging(False)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self._drag_started:
            self.clicked.emit()
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def set_dragging(self, dragging: bool):
        self._dragging = bool(dragging)
        self._render()

    def set_active(self, active: bool):
        self._active = active
        self._render()

    def set_expanded(self, expanded: bool):
        self._expanded = expanded
        self._label.setVisible(expanded)
        self._render()

    def _render(self):
        text_color = theme.WHITE if self._active else theme.SUBTEXT
        weight = "600" if self._active else "500"
        self._icon.setStyleSheet(
            f"background:transparent;border:none;font-size:16px;color:{text_color};")
        self._label.setStyleSheet(
            "background:transparent;border:none;font-size:13px;"
            f"color:{text_color};font-weight:{weight};")

        if self._dragging:
            # Во время drag вкладка становится «приподнятой»: яркий контур + glow.
            self.setStyleSheet(
                f"NavButton{{"
                "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(139, 109, 255, 0.26), stop:1 rgba(52, 220, 240, 0.10));"
                f"  border: 1px solid {theme.ACCENT3};"
                "  border-radius: 12px;"
                "}"
            )
            theme.glow(self, theme.ACCENT3, blur=20, dy=3, alpha=110)
            self._repaint_glow_parent()
            return

        if self._active:
            # Премиальный стиль: мягкий полупрозрачный градиент + яркая неоновая бирюзовая левая грань
            self.setStyleSheet(
                f"NavButton{{"
                "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(139, 109, 255, 0.16), stop:1 rgba(91, 155, 255, 0.03));"
                f"  border-left: 3px solid {theme.ACCENT3};"
                "  border-top-left-radius: 0px;"
                "  border-bottom-left-radius: 0px;"
                "  border-top-right-radius: 10px;"
                "  border-bottom-right-radius: 10px;"
                "}"
            )
            # Мягкое свечение рисует Sidebar в своём paintEvent — кэш-pixmap
            # без живого QGraphicsDropShadowEffect (тот пересчитывал гауссов
            # blur на каждый кадр resize и подёргивал окно на ноутбуках).
            self.setGraphicsEffect(None)
        else:
            self.setGraphicsEffect(None)
            self.setStyleSheet(
                "NavButton{"
                "  background: transparent;"
                "  border-left: 3px solid transparent;"
                "  border-radius: 10px;"
                "}"
                "NavButton:hover{"
                "  background: rgba(255, 255, 255, 0.05);"
                "}"
            )
        self._repaint_glow_parent()

    def _repaint_glow_parent(self):
        """Свечение активной вкладки живёт в paintEvent сайдбара —
        при смене состояния кнопки просим его перерисоваться."""
        pw = self.parentWidget()
        if pw is not None:
            pw.update()


class SidebarStatusCard(QFrame):
    """Мини-виджет статуса ядра внизу бокового меню.

    Отображает текущее состояние DNS-сервера (работает/остановлен) с красивым
    пульсирующим индикатором и лаконичным текстом.
    При сворачивании бокового меню аккуратно сжимается до одного пульсирующего круга.
    """

    def __init__(self):
        super().__init__()
        self.setStyleSheet(
            f"QFrame{{"
            f"  background: {theme.INPUT_BG};"
            f"  border: 1px solid {theme.BORDER};"
            "  border-radius: 10px;"
            "}"
        )
        self.setFixedHeight(50)

        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(10, 4, 10, 4)
        self.lay.setSpacing(8)

        # Пульсирующий кружок
        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._dot.setAlignment(Qt.AlignCenter)
        self._dot.setStyleSheet(f"color:{theme.MUTED};font-size:14px;background:transparent;border:none;")
        self.lay.addWidget(self._dot)

        # Текстовый блок
        self._text_wrap = QWidget()
        self._text_wrap.setStyleSheet("background:transparent;border:none;")
        self._text_lay = QVBoxLayout(self._text_wrap)
        self._text_lay.setContentsMargins(0, 0, 0, 0)
        self._text_lay.setSpacing(1)

        self._title = QLabel("UmbraNet")
        self._title.setStyleSheet(f"color:{theme.SUBTEXT};font-size:10px;font-weight:600;background:transparent;border:none;")

        self._status = QLabel("Остановлено")
        self._status.setStyleSheet(f"color:{theme.MUTED};font-size:11px;font-weight:700;background:transparent;border:none;")

        self._text_lay.addWidget(self._title)
        self._text_lay.addWidget(self._status)
        self.lay.addWidget(self._text_wrap, 1)

        self._expanded = True

    def set_status(self, running: bool):
        color = theme.GREEN if running else theme.MUTED
        status_text = "Работает" if running else "Остановлен"

        self._dot.setStyleSheet(f"color:{color};font-size:14px;background:transparent;border:none;")
        self._status.setStyleSheet(f"color:{color};font-size:11px;font-weight:700;background:transparent;border:none;")
        self._status.setText(status_text)

        if running:
            theme.glow(self._dot, theme.GREEN, blur=10, dy=0, alpha=150)
        else:
            self._dot.setGraphicsEffect(None)

    def set_expanded(self, expanded: bool):
        self._expanded = expanded
        self._text_wrap.setVisible(expanded)
        if expanded:
            self.setMinimumWidth(theme.SIDEBAR_W_EXPANDED - 24)
            self.setMaximumWidth(theme.SIDEBAR_W_EXPANDED - 24)
            self.setFixedHeight(50)
            self.setStyleSheet(
                f"QFrame{{"
                f"  background: {theme.INPUT_BG};"
                f"  border: 1px solid {theme.BORDER};"
                "  border-radius: 10px;"
                "}"
            )
            self.lay.setContentsMargins(10, 4, 10, 4)
        else:
            self.setMinimumWidth(26)
            self.setMaximumWidth(26)
            self.setFixedHeight(26)
            self.setStyleSheet("QFrame{background:transparent;border:none;}")
            self.lay.setContentsMargins(0, 0, 0, 0)


class Sidebar(QFrame):
    navigate = Signal(str)  # key выбранного раздела
    orderChanged = Signal(list)  # новый порядок key после drag&drop
    layoutWidthChanged = Signal()  # изменилась ширина панели (в т.ч. кадр анимации)
    extraNavRequested = Signal()  # служебный сигнал: показать дополнительную страницу

    def __init__(self, items: list[NavItem], active_key: str | None = None,
                 expanded: bool = False):
        """Боковое меню. По умолчанию СВЁРНУТО — так программа и открывается.

        Решение пользователя: при запуске панель всегда в виде значков, чтобы
        вкладкам оставалось больше места; развернуть её человек может сам кнопкой
        внизу панели. Автоматического сворачивания/разворачивания по ширине окна
        НЕТ: панель не должна меняться сама, пока её не попросят.
        """
        super().__init__()
        self._items = list(items)
        self._order = [it.key for it in self._items]
        self._buttons: dict[str, NavButton] = {}
        self._expanded = bool(expanded)
        self._drop_target_index: int | None = None
        self._logo_clicks: list[float] = []
        self._logo_fired = False
        self.setAcceptDrops(True)
        self.active_key = active_key or (items[0].key if items else "")

        self.setStyleSheet(f"Sidebar{{background:{theme.SIDEBAR}; border-right: 1px solid {theme.BORDER};}}")
        self.setMinimumWidth(theme.SIDEBAR_W_EXPANDED)
        self.setMaximumWidth(theme.SIDEBAR_W_EXPANDED)

        root = QVBoxLayout(self)
        self._root = root
        self._nav_start_index = 2  # logo + spacing идут перед вкладками
        root.setContentsMargins(12, 16, 12, 12)
        root.setSpacing(6)

        # Неоновая линия-плейсхолдер показывает, куда встанет вкладка.
        self._drop_indicator = QFrame()
        self._drop_indicator.setMinimumHeight(0)
        self._drop_indicator.setMaximumHeight(0)
        self._drop_indicator.setStyleSheet(
            f"QFrame{{background:{theme.grad(theme.ACCENT, theme.ACCENT3)};"
            "border:none;border-radius:2px;}"
        )
        theme.glow(self._drop_indicator, theme.ACCENT3, blur=14, dy=0, alpha=140)
        self._drop_indicator.setVisible(False)
        self._drop_indicator_anim = QPropertyAnimation(self._drop_indicator, b"maximumHeight", self)
        self._drop_indicator_anim.setDuration(130)
        self._drop_indicator_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._drop_indicator_anim.valueChanged.connect(
            lambda v: self._drop_indicator.setMinimumHeight(int(v))
        )
        self._reorder_anim_group = None

        # ── логотип ──
        self._logo = _LogoLabel()
        self._logo.setTextFormat(Qt.RichText)
        self._logo.setFixedHeight(40)
        self._logo.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        # фиксированный левый отступ в пикселях — чтобы «U» не сдвигалась
        # при смене размера шрифта (раньше использовался &nbsp;, чья ширина
        # зависит от размера шрифта → возникал рывок).
        self._logo.setStyleSheet("background:transparent;border:none;padding-left:6px;")
        self._logo.clicked.connect(self._on_logo_clicked)
        root.addWidget(self._logo)
        root.addSpacing(14)

        # ── пункты ──
        for it in self._items:
            btn = NavButton(it)
            btn.clicked.connect(lambda k=it.key: self._on_click(k))
            self._buttons[it.key] = btn
            root.addWidget(btn)

        root.addStretch()

        # Нижнюю карточку "UmbraNet / Остановлен" убрали: она дублировала
        # верхнюю панель Старт/Стоп и выглядела как отдельная кнопка запуска,
        # хотя не была кликабельной. Реальный статус и управление теперь только
        # в глобальной верхней панели.

        # ── кнопка сворачивания (крупная, оформленная плашка) ──
        self._toggle_btn = QPushButton()
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.setFixedHeight(40)
        self._toggle_btn.clicked.connect(self.toggle)
        self._toggle_btn.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{theme.SUBTEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;font-size:18px;font-weight:700;}}"
            f"QPushButton:hover{{color:{theme.WHITE};border-color:{theme.ACCENT};}}"
        )
        root.addWidget(self._toggle_btn)

        # анимация ширины
        self._anim = QPropertyAnimation(self, b"minimumWidth")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._anim2 = QPropertyAnimation(self, b"maximumWidth")
        self._anim2.setDuration(180)
        self._anim2.setEasingCurve(QEasingCurve.InOutCubic)
        # Каждый кадр анимации ширины — сигнал наружу: главное окно пересчитывает
        # ужим верхней панели. Разворот панели отнимает у шапки ~140 px, и без
        # этого кнопки (в первую очередь «Старт») уезжали за край окна.
        for anim, prop in ((self._anim, b"minimumWidth"), (self._anim2, b"maximumWidth")):
            anim.valueChanged.connect(self._on_width_frame)

        self._refresh_active()
        self._refresh_expanded(animate=False)

    # ── drag&drop порядка вкладок ───────────────────────────────────────────
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(NavButton._MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(NavButton._MIME):
            self._show_drop_indicator(self._drop_index(event.position().toPoint().y()))
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._hide_drop_indicator(animate=True)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(NavButton._MIME):
            event.ignore()
            return
        key = bytes(event.mimeData().data(NavButton._MIME)).decode("utf-8", errors="ignore")
        if key not in self._order:
            event.ignore()
            return

        target = self._drop_index(event.position().toPoint().y())
        old = self._order.index(key)
        order = list(self._order)
        order.pop(old)
        if old < target:
            target -= 1
        target = max(0, min(target, len(order)))
        order.insert(target, key)
        if order != self._order:
            self._animate_nav_reorder(order, key)
            self.orderChanged.emit(list(order))
        else:
            self._hide_drop_indicator(animate=True)
        event.acceptProposedAction()

    def _show_drop_indicator(self, index: int):
        index = max(0, min(index, len(self._order)))
        if self._drop_target_index == index and self._root.indexOf(self._drop_indicator) >= 0:
            return
        first_show = self._root.indexOf(self._drop_indicator) < 0
        self._drop_target_index = index
        self._root.insertWidget(self._nav_start_index + index, self._drop_indicator)
        self._drop_indicator.setVisible(True)
        if first_show:
            self._animate_indicator_height(4)

    def _animate_indicator_height(self, target: int, on_done=None):
        try:
            self._drop_indicator_anim.stop()
            self._drop_indicator_anim.setStartValue(self._drop_indicator.maximumHeight())
            self._drop_indicator_anim.setEndValue(int(target))
            if on_done is not None:
                try:
                    self._drop_indicator_anim.finished.disconnect()
                except Exception:
                    pass
                self._drop_indicator_anim.finished.connect(on_done)
            self._drop_indicator_anim.start()
        except Exception:
            self._drop_indicator.setMinimumHeight(int(target))
            self._drop_indicator.setMaximumHeight(int(target))
            if on_done:
                on_done()

    def _hide_drop_indicator(self, animate: bool = False):
        self._drop_target_index = None

        def _detach():
            idx = self._root.indexOf(self._drop_indicator)
            if idx >= 0:
                self._root.takeAt(idx)
            self._drop_indicator.setVisible(False)
            self._drop_indicator.setParent(None)
            self._drop_indicator.setMinimumHeight(0)
            self._drop_indicator.setMaximumHeight(0)

        if animate and self._root.indexOf(self._drop_indicator) >= 0:
            self._animate_indicator_height(0, _detach)
        else:
            try:
                self._drop_indicator_anim.stop()
            except Exception:
                pass
            _detach()

    def _animate_nav_reorder(self, new_order: list[str], moved_key: str):
        """FLIP-анимация: запоминаем текущие позиции, меняем layout,
        затем плавно доводим виджеты до новых координат."""
        old_geo = {k: b.geometry() for k, b in self._buttons.items()}
        self._hide_drop_indicator(animate=False)
        self._order = list(new_order)
        self._apply_nav_order()
        self._root.activate()
        self.updateGeometry()
        new_geo = {k: b.geometry() for k, b in self._buttons.items()}

        group = QParallelAnimationGroup(self)
        for key, btn in self._buttons.items():
            start = old_geo.get(key)
            end = new_geo.get(key)
            if start is None or end is None or start == end:
                continue
            btn.setGeometry(start)
            btn.raise_()
            anim = QPropertyAnimation(btn, b"geometry", group)
            anim.setDuration(190)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.setStartValue(start)
            anim.setEndValue(end)
            # кэш-glow активной вкладки рисуем мы сами — перерисовываемся
            # на каждом кадре анимации, чтобы свечение ехало за кнопкой
            anim.valueChanged.connect(self.update)
            group.addAnimation(anim)

        if group.animationCount() == 0:
            self._flash_button(moved_key)
            return

        group.finished.connect(lambda k=moved_key: self._flash_button(k))
        self._reorder_anim_group = group
        group.start(QAbstractAnimation.DeleteWhenStopped)

    def _flash_button(self, key: str):
        btn = self._buttons.get(key)
        if btn is None:
            return
        btn.set_dragging(True)
        QTimer.singleShot(220, lambda b=btn: b.set_dragging(False))

    def _drop_index(self, y: int) -> int:
        for idx, key in enumerate(self._order):
            btn = self._buttons.get(key)
            if btn is not None and y < btn.geometry().center().y():
                return idx
        return len(self._order)

    def _apply_nav_order(self):
        # insertWidget переносит существующий виджет внутри layout без пересоздания.
        for i, key in enumerate(self._order):
            btn = self._buttons.get(key)
            if btn is not None:
                self._root.insertWidget(self._nav_start_index + i, btn)

    # ── публичное ──
    def set_active(self, key: str):
        if key in self._buttons:
            self.active_key = key
            self._refresh_active()

    def add_item(self, item: NavItem) -> bool:
        """Добавляет пункт меню в конец списка (без пересоздания остальных)."""
        if item.key in self._buttons:
            return False
        btn = NavButton(item)
        btn.clicked.connect(lambda k=item.key: self._on_click(k))
        btn.set_expanded(self._expanded)
        self._buttons[item.key] = btn
        self._items.append(item)
        self._order.append(item.key)
        self._apply_nav_order()
        return True

    def _on_logo_clicked(self):
        """Обработчик нажатий на логотип «U» в свёрнутой панели."""
        if self._expanded or self._logo_fired:
            return
        now = time.monotonic()
        self._logo_clicks = [t for t in self._logo_clicks if now - t <= 8.0]
        self._logo_clicks.append(now)
        if len(self._logo_clicks) >= 10:
            self._logo_fired = True
            self._logo_clicks.clear()
            self.extraNavRequested.emit()

    def paintEvent(self, event):
        """Фон сайдбара + кэшированное свечение под активной вкладкой.

        Свечение рисуется ПОСЛЕ super() (чтобы QSS-фон его не закрасил)
        и ДО детей (Qt рисует детей после paintEvent родителя — кнопка
        ляжет поверх свечения, как это делал QGraphicsDropShadowEffect).
        """
        super().paintEvent(event)
        btn = self._buttons.get(self.active_key)
        if btn is not None and btn.isVisible() and not getattr(btn, "_dragging", False):
            p = QPainter(self)
            paint_widget_glow(p, btn, theme.ACCENT3, blur=15, dy=2, alpha=80,
                              host=self)

    def set_engine_status(self, running: bool):
        """Обновляет статус ядра в боковой мини-карточке."""
        if hasattr(self, "_status_card"):
            self._status_card.set_status(running)

    def toggle(self):
        """Развернуть/свернуть панель — это делает только сам пользователь."""
        self._expanded = not self._expanded
        self._refresh_expanded(animate=True)

    def set_collapsed(self, collapsed: bool, animate: bool = True) -> bool:
        """Ставит панель в заданное состояние (не переключает). True — изменилось.

        Используется при сборке окна (стартовое состояние — свёрнуто) и в тестах.
        По ходу работы панель никто, кроме пользователя, не переключает.
        """
        collapsed = bool(collapsed)
        if collapsed == (not self._expanded):
            return False
        self._expanded = not collapsed
        self._refresh_expanded(animate=animate)
        return True

    # ── внутреннее ──
    def _on_width_frame(self, _value=None):
        """Сообщает наружу, что ширина панели изменилась (используется шапкой)."""
        self.layoutWidthChanged.emit()

    def _on_click(self, key: str):
        if key != self.active_key:
            self.active_key = key
            self._refresh_active()
            self.navigate.emit(key)

    def _refresh_active(self):
        for k, btn in self._buttons.items():
            btn.set_active(k == self.active_key)

    def _refresh_expanded(self, animate: bool):
        target = theme.SIDEBAR_W_EXPANDED if self._expanded else theme.SIDEBAR_W_COLLAPSED

        # логотип
        if self._expanded:
            self._logo.setText(
                f"<span style='font-size:23px;font-weight:800;color:{theme.WHITE};'>"
                f"Umbra<span style='color:{theme.ACCENT2};'>Net</span></span>"
            )
        else:
            self._logo.setText(
                f"<span style='font-size:23px;font-weight:800;color:{theme.ACCENT};'>U</span>"
            )

        # пункты
        for btn in self._buttons.values():
            btn.set_expanded(self._expanded)

        # статус-карта
        if hasattr(self, "_status_card"):
            self._status_card.set_expanded(self._expanded)

        # стрелка сворачивания (двойной шеврон — крупнее и заметнее)
        self._toggle_btn.setText("«" if self._expanded else "»")

        # ширина (с анимацией или сразу)
        if animate:
            for a in (self._anim, self._anim2):
                a.stop()
                a.setStartValue(self.width())
                a.setEndValue(target)
                a.start()
        else:
            self.setMinimumWidth(target)
            self.setMaximumWidth(target)
            self.layoutWidthChanged.emit()
