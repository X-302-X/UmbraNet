"""
UmbraNet - раздел «Маршрутизация» (PySide6).

Состав:
  • шапка: режимы (ModeSwitch) + Start/Stop/Restart (ControlBar);
  • категории сервисов с тумблерами (включил -> домены в routed_domains);
  • ручной список доменов/процессов (поиск, добавление, удаление);
  • правая панель активного DNS-профиля.

Вся работа с ядром — через engine_adapter.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QThread, QTimer, Signal

log = logging.getLogger("UmbraNet.RoutingView")
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    get_active_dns_profile,
    get_current_mode,
    get_developer_mode,
    get_engine,
    get_favorite_services,
    is_domain_routed,
    is_protected_process,
    protected_processes,
    save_config,
    set_favorite_services,
)
from umbranet.services_catalog import (
    CATEGORIES,
    SERVICE_BYPASS,
    SERVICES,
    service_allowed_in_mode,
    services_in_category,
)
from umbranet.process_icons import pixmap_for_process
from umbranet.widgets.dialogs import ProcessPickerDialog
from umbranet.widgets.glow_wrap import GlowWrap
from umbranet.widgets.manual_canvas import ManualCanvas
from umbranet.widgets.rounded_panel import RoundedPanel
from umbranet.widgets.service_canvas import ServiceCanvas
from umbranet.widgets.toggle import Toggle
from umbranet.widgets.transport_list import TransportList


class _DnsRestartWorker(QThread):
    """Мягко перезапускает DNS-сервер после изменения маршрутов.

    Системный DNS Windows при этом не трогаем: он уже указывает на 127.0.0.1.
    Нужен именно restart локального сервера, чтобы изменения маршрутизации
    применялись сразу и очищался кэш старых решений.
    """
    done = Signal(bool)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def run(self):
        ok = False
        try:
            self.engine.stop()
            self.engine.reload_config()
            # Если пользователь/AI-генерация успели нажать Stop, пока этот
            # worker выполнялся, нельзя самовольно запускать UmbraNet обратно.
            if getattr(self.engine, "_manual_stop_requested", False):
                ok = True
            else:
                ok = bool(self.engine.start())
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось перезапустить DNS после смены маршрута: %s", exc)
            ok = False
        self.done.emit(ok)


def hex_to_rgba(hex_str: str, alpha: float = 0.35) -> str:
    """Вспомогательный хелпер для конвертации HEX цветов в RGBA с прозрачностью."""
    hex_str = hex_str.lstrip("#")
    if len(hex_str) == 6:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        return f"rgba({r}, {g}, {b}, {alpha})"
    return f"rgba(255, 255, 255, {alpha})"


class CategoryHeader(QWidget):
    """Красивый заголовок категории с неоновой разделительной линией сверху и тумблером.

    Если передан `on_drag` (словарь с ключами begin/move/end), заголовок становится
    «ручкой»: за него можно тянуть, меняя высоту блока (используется «Диспетчером
    задач»). Тогда же включаются курсор изменения размера и подсветка линии при
    наведении — чтобы человек понял, что линию можно тянуть.
    """
    def __init__(self, title: str, emoji: str, color1: str, color2: str,
                 toggle: Toggle | None = None, on_drag: dict | None = None, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;border:none;")
        lay = QVBoxLayout(self)
        # Верхнее поле — НОЛЬ: линия должна быть самой верхней гранью блока.
        # Раньше над ней оставалось 10 px прозрачного (то есть чёрного) фона, и
        # граница блока проходила не по линии (замечание пользователя: «граница
        # диспетчера задач — именно та фиолетовая линия, а не ещё чёрный фон»).
        lay.setContentsMargins(0, 0, 0, 4)
        lay.setSpacing(6)

        # Тонкий разделитель сверху (яркий, длинный и выразительный, в цвете категории)
        self._color1 = color1
        line = QFrame()
        line.setFixedHeight(1)
        self._line = line
        self._set_line_hover(False)
        lay.addWidget(line)

        # ── тяга за заголовок ──
        self._on_drag = on_drag
        self._dragging = False
        self._drag_start_y = 0.0
        if on_drag is not None:
            self.setCursor(Qt.SizeVerCursor)

        # Строка с названием и тумблером (полностью прозрачная на фоне, без дешевого блюра/плашек)
        row = QHBoxLayout()
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(8)

        # Отдельный лейбл для эмодзи с увеличенной шириной до 32px и отступом справа, чтобы значки не обрезались
        icon_lbl = QLabel(emoji)
        icon_lbl.setStyleSheet("font-size: 15px; background: transparent; border: none; padding-right: 3px;")
        icon_lbl.setFixedWidth(32)
        icon_lbl.setAlignment(Qt.AlignCenter)
        row.addWidget(icon_lbl)

        # Название категории
        lbl = QLabel(title.upper())
        lbl.setStyleSheet(f"color: {color1}; font-size: 11px; font-weight: 800; letter-spacing: 1.5px; background: transparent; border: none;")
        row.addWidget(lbl)
        
        # Растяжка между названием и тумблером, чтобы тумблер оставался справа в ряду с другими
        row.addStretch()

        if toggle is not None:
            row.addWidget(toggle)
            
        # Небольшой отступ справа (10px), чтобы тумблеры категорий были гармонично выровнены
        row.addSpacing(10)

        lay.addLayout(row)


    def _set_line_hover(self, hovered: bool):
        """Линия чуть ярче, когда курсор над заголовком: видно, что тут можно тянуть.

        Меняем только прозрачность затухания градиента — толщина и место линии те
        же, поэтому при наведении ничего не сдвигается.
        """
        end = "rgba(255,255,255,0.14)" if hovered else "rgba(255,255,255,0.03)"
        self._hovered = bool(hovered)
        self._line.setStyleSheet(
            f"background: {theme.grad(self._color1, end)}; border: none;")

    def enterEvent(self, event):
        if self._on_drag is not None:
            self._set_line_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._on_drag is not None:
            self._set_line_hover(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if self._on_drag is not None and event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_start_y = event.globalPosition().y()
            self._on_drag["begin"]()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            # Насколько ушли вниз от точки нажатия (вверх — отрицательное значение).
            self._on_drag["move"](int(event.globalPosition().y() - self._drag_start_y))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self._on_drag["end"]()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CategorySection(QWidget):
    """Статичный контейнер категории услуг с красивой шапкой."""
    def __init__(self, title: str, emoji: str, color1: str, color2: str,
                 toggle: Toggle | None = None, header_drag: dict | None = None, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;border:none;")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)

        # Добавляем красивую шапку-разделитель
        self.header = CategoryHeader(title, emoji, color1, color2, toggle, on_drag=header_drag)
        self.lay.addWidget(self.header)

        # Контейнер для строк сервисов
        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background:transparent;border:none;")
        self.content_lay = QVBoxLayout(self.content_widget)
        self.content_lay.setContentsMargins(0, 0, 0, 0)
        self.content_lay.setSpacing(4)
        self.lay.addWidget(self.content_widget)

        # Алиас для 100% обратной совместимости со старым кодом Collapsible
        self._body_lay = self.content_lay

    def add_widget(self, w: QWidget):
        self.content_lay.addWidget(w)

    def add_layout(self, lay):
        self.content_lay.addLayout(lay)

    def refit(self):
        # Пустой метод для обратной совместимости с вызовами в _apply_service_search
        pass

    def is_expanded(self) -> bool:
        # Для совместимости возвращаем True
        return True

    def set_expanded(self, expanded: bool, animate: bool = True):
        # Пустой метод для совместимости
        pass


class _DismissLayer(QWidget):
    """Затемнение поверх содержимого: клик мимо панели закрывает её.

    Появляется только в узком окне, когда правая панель выезжает поверх списка
    (см. RoutingView.set_narrow_mode). Клик по слою — это и есть «клик мимо».
    """

    dismissed = Signal()

    def mousePressEvent(self, event):
        self.dismissed.emit()
        event.accept()


class RoutingView(QWidget):
    # update_subscriptions_async завершается в worker-потоке DNS. Все изменения
    # Qt-виджетов прокидываем через сигнал, чтобы не трогать GUI из worker-а.
    _subscription_done = Signal(bool, int, bool)  # ok, count, clear_input

    def __init__(self):
        super().__init__()
        self._subscription_done.connect(self._on_subscription_done)
        self.engine = get_engine()
        self._route_restart_worker: _DnsRestartWorker | None = None
        self._route_restart_pending = False

        outer = QVBoxLayout(self)
        # справа/снизу нули: это место заняли поля GlowWrap (свечение панели)
        # Правый отступ нулевой: место справа занимает свечение правой панели.
        # Когда панель прячется (узкое окно), отступ возвращается — иначе кнопки
        # «Диспетчера задач» липли бы к самому краю окна (замечено пользователем:
        # «кнопка Добавить слишком близко к краю подходит»).
        #
        # Верхний отступ уменьшен с 18 до 6: пользователь попросил поднять шапку
        # вкладки («Сервисы» + кнопка маршрута) и поиск повыше — интерфейс должен
        # быть плотнее, а освободившиеся пиксели уходят списку сервисов.
        # Поля хранятся в одном месте: при прятанье правой панели правый отступ
        # меняется на равный левому, при возврате — берётся отсюда же.
        self._page_margins = (24, 6, 0, 6)
        outer.setContentsMargins(*self._page_margins)
        outer.setSpacing(14)
        self._outer = outer

        # режимы и Start/Stop теперь в ГЛОБАЛЬНОЙ верхней панели (app.py),
        # доступной со всех вкладок — здесь их больше нет.

        self._favorite_services = get_favorite_services(list(SERVICES.keys()))
        self._service_search = ""

        # ── состояние «узкого окна» ──
        # В узком окне правая часть (маршрут / стратегия и данные профиля DNS)
        # прячется целиком, а её место отдаётся спискам: просьба пользователя —
        # прятать её заранее, когда кнопки режимов вот-вот начнут ужиматься.
        # Вместо неё в шапке вкладки появляется кнопка, по которой панель
        # выезжает ПОВЕРХ списка и закрывается кликом мимо (Esc — тоже закрывает).
        self._narrow = False
        self._overlay_open = False
        self._panel_w = 356      # предварительно; уточняется по sizeHint панели

        # ── высота «Диспетчера задач» (тянется за фиолетовую линию) ──
        # _dispatcher_pref — что выбрал человек (высота СПИСКА в пикселях),
        # _dispatcher_sized — трогали ли его вообще: пока не трогали, высоту
        # задаёт раскладка, как раньше.
        self._dispatcher_pref: int | None = None
        self._dispatcher_sized = False
        self._dispatcher_drag_h: int | None = None

        # ── основная зона: слева списки, справа — место под правую панель ──
        # Панель НЕ в раскладке: она плавает над вкладкой, а место под неё
        # резервирует распорка. Так её можно и держать сбоку (широкое окно), и
        # убрать совсем (узкое), и выехать поверх списка — одной и той же панелью.
        self._right_scroll = self._build_right()
        self._right_scroll.setParent(self)
        self._right_spacer = QWidget()
        self._right_spacer.setStyleSheet("background:transparent;")
        self._right_spacer.setFixedWidth(0)
        # Распорка держит МЕСТО под панель, но панель лежит прямо над ней (та же
        # геометрия) — и распорка старше по стеку (создана позже) значила, что
        # клики и колесо мыши доставались ей, а не панели. Снаружи это выглядело
        # так: в широком окне правую часть «нельзя выбрать», пока не сузишь окно
        # и не откроешь панель поверх списка (там она поднимается через raise_()).
        # Распорке мышь не нужна вообще: она ничего не рисует и ни на что не
        # реагирует — пусть события проходят сквозь неё.
        self._right_spacer.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._left_column = self._build_left()
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._left_column, 1)
        body.addWidget(self._right_spacer)
        self._body_layout = body
        outer.addLayout(body, 1)

        # слой затемнения под выехавшей панелью
        self._backdrop = _DismissLayer(self)
        self._backdrop.setStyleSheet("background: rgba(0, 0, 0, 0.45);")
        self._backdrop.dismissed.connect(self.close_right_panel)
        self._backdrop.hide()

        self.set_narrow_mode(False, force=True)

        saved = self._load_dispatcher_height()
        if saved is not None:
            self._dispatcher_pref = saved
            self._dispatcher_sized = True
            self._apply_dispatcher_height()

        self.refresh()

    # ════════════════ построение ════════════════
    def _build_left(self) -> QWidget:
        """Левая колонка: список сервисов и «Диспетчер задач».

        Колонка целиком НЕ прокручивается — ни сама по себе, ни в составе вкладки.
        Прямая просьба пользователя: полоса прокрутки поперёк колонки выглядела как
        разделитель посередине окна («ползунок, который разделяет главное меню на
        две части») и только мешала.

        Прокручиваются сами списки, у каждого своя: сервисы — `ServiceCanvas`,
        записи — `ManualCanvas`. Оба рисуют содержимое одним `paintEvent` и
        прокручиваются колесом внутри своей рамки; полосы вкладки при этом не
        появляется.
        """
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        # Зазоры 8 вместо 12: шапка вкладки, поиск и список сервисов стоят плотнее
        # друг к другу (просьба пользователя — сделать интерфейс плотнее), а
        # сэкономленные пиксели получает сам список сервисов.
        #
        # Зазоры задаются ЯВНО (spacing = 0 + addSpacing) — так перед «Диспетчером
        # задач» зазора нет совсем: его верхняя граница и есть фиолетовая линия,
        # а не полоса чёрного фона над ней (просьба пользователя).
        lay.setSpacing(0)

        # Шапка вкладки: заголовок и (в узком окне) кнопка, которая открывает
        # правую панель поверх списка — иначе в узком окне до выбора маршрута не
        # добраться, потому что сама панель спрятана.
        #
        # Высота строки ЖЁСТКО ЗАДАНА и одинакова в обоих состояниях. Иначе кнопка
        # (она выше текста заголовка) поднимала бы строку, и при исчезновении панели
        # ВСЁ содержимое вкладки уезжало бы вниз на десяток пикселей — пользователь
        # это заметил: «внутренности сервисов чуток уходят вниз».
        title_host = QWidget()
        title_host.setStyleSheet("background:transparent;")
        title = QLabel("🚀  Сервисы")
        title.setStyleSheet(f"color:{theme.TEXT}; font-size:16px; font-weight:700;")
        self._title_row_h = max(title.sizeHint().height(), 22)
        title_host.setFixedHeight(self._title_row_h)
        title_row = QHBoxLayout(title_host)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)
        title_row.addWidget(title, 0, Qt.AlignVCenter)
        title_row.addStretch()
        self._narrow_toggle = QPushButton("🔌  Маршрут DNS")
        self._narrow_toggle.setCursor(Qt.PointingHandCursor)
        # Высота — как у строки заголовка: кнопка стоит ровно на одном уровне с
        # надписью «Сервисы» и не свисает ниже неё.
        self._narrow_toggle.setFixedHeight(self._title_row_h)
        self._narrow_toggle.setStyleSheet(
            f"QPushButton{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:7px;"
            "padding:0 10px;font-size:12px;font-weight:600;}"
            f"QPushButton:hover{{border-color:{theme.ACCENT3};color:{theme.TEXT};}}"
        )
        self._narrow_toggle.clicked.connect(self.toggle_right_panel)
        self._narrow_toggle.setVisible(False)      # показывается только в узком окне
        title_row.addWidget(self._narrow_toggle, 0, Qt.AlignVCenter)
        lay.addWidget(title_host)
        lay.addSpacing(8)
        self._service_search_input = QLineEdit()
        self._service_search_input.setPlaceholderText("🔍 Найти сервис: ChatGPT, GitHub, Discord...")
        self._service_search_input.setFixedHeight(32)
        self._service_search_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:9px;padding:0 11px;font-size:12px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}"
        )
        self._service_search_input.textChanged.connect(self._on_service_search)
        lay.addWidget(self._service_search_input)

        # ── телеграмизация: весь список сервисов рисует один paintEvent ──
        # (было: QScrollArea + ~150 дочерних виджетов, медленный ресайз)
        catalog = []
        category_colors = CATEGORIES
        if theme.CURRENT_THEME == "glass":
            # В исходном каталоге категории используют неоновые пастельные
            # цвета для тёмного фона; для стеклянной темы берём читаемые тона.
            category_colors = {
                "AI": ("🤖", theme.ACCENT, theme.ACCENT2),
                "Медиа": ("🎬", theme.ACCENT2, theme.ACCENT3),
                "Игры": ("🎮", theme.ORANGE, theme.RED),
                "Работа": ("💼", theme.GREEN, theme.ACCENT2),
                "Разное": ("🧩", theme.PINK, theme.RED),
            }
        for cat, (emoji, c1, c2) in category_colors.items():
            catalog.append((cat, emoji, c1, c2,
                            [(svc, SERVICES[svc][1]) for svc in services_in_category(cat)]))
        self._canvas = ServiceCanvas(catalog, bypass_map=SERVICE_BYPASS)
        self._canvas.set_app_mode(get_current_mode())
        self._canvas.set_developer_mode(get_developer_mode())
        self._canvas.set_favorites(self._favorite_services)
        self._canvas.serviceToggled.connect(self._toggle_service)
        self._canvas.favoriteToggled.connect(self._toggle_favorite)
        self._canvas.categoryToggled.connect(self._toggle_category)
        lay.addSpacing(8)
        lay.addWidget(self._canvas, 1)

        # ручной список (зазора перед ним нет — см. комментарий выше про линию)
        lay.addWidget(self._build_manual())

        # Обёртки в прокрутку здесь нет намеренно — см. докстринг выше.
        return wrap

    def _toggle_favorite(self, svc: str):
        if svc in self._favorite_services:
            self._favorite_services.remove(svc)
        else:
            self._favorite_services.append(svc)
        set_favorite_services(self._favorite_services, list(SERVICES.keys()))
        self._canvas.set_favorites(self._favorite_services)
        self.refresh()

    def _on_service_search(self, text: str):
        self._canvas.apply_search(text)

    def _toggle_category(self, cat: str, on: bool):
        """Включить/выключить все сервисы категории разом.

        В DNS-only DPI-сервисы не включаются (и наоборот). Выключить можно все.
        """
        routed = self.engine.config.setdefault("routed_domains", [])
        mode = get_current_mode()
        unlock = get_developer_mode()
        for svc in services_in_category(cat):
            if on and not unlock and not service_allowed_in_mode(svc, mode):
                continue
            _, _, domains = SERVICES[svc]
            if on:
                for d in domains:
                    if d not in routed:
                        routed.append(d)
            else:
                routed[:] = [r for r in routed if r not in domains]
        self._apply()

    def _build_manual(self) -> QWidget:
        """Секция «Диспетчер задач» — показывает ВСЕ routed_domains,
        включая добавленные через книжку сервисов (AI, Медиа и т.д.).
        Пресетные отображаются с меткой сервиса, ручные — без метки.
        """
        # Название по просьбе пользователя: «Диспетчер задач» (было «Все активные
        # домены и процессы»). Значок блокнота 📋 оставлен на прежнем месте.
        drag_hooks = {
            "begin": self._on_dispatcher_drag_begin,
            "move": self._on_dispatcher_drag_move,
            "end": self._on_dispatcher_drag_end,
        }
        sec = CategorySection("Диспетчер задач", "📋", theme.ACCENT, theme.ACCENT2,
                              header_drag=drag_hooks)
        self._manual_section = sec

        # ── компактная строка добавления ─────────────────────────────
        addrow = QHBoxLayout()
        addrow.setSpacing(8)
        self.add_input = QLineEdit()
        self.add_input.setPlaceholderText("chatgpt.com или chrome.exe")
        self.add_input.setFixedHeight(36)
        self.add_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;font-size:13px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        self.add_input.returnPressed.connect(self._add_typed)

        add_btn = QPushButton("+ Добавить")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setFixedHeight(36)
        add_btn.setStyleSheet(
            f"QPushButton{{background:{theme.GREEN};color:{theme.text_on_color(theme.GREEN)};"
            "border:none;border-radius:8px;padding:0 14px;font-weight:600;font-size:13px;}"
            f"QPushButton:hover{{border-radius:8px;}}")
        add_btn.clicked.connect(self._add_typed)

        pick_btn = QPushButton("🎮 Процесс")
        pick_btn.setCursor(Qt.PointingHandCursor)
        pick_btn.setFixedHeight(36)
        pick_btn.setToolTip("Выбрать запущенный процесс из списка")
        pick_btn.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;font-size:13px;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};}}")
        pick_btn.clicked.connect(self._pick_process)

        # Кнопки «Процесс» и «Добавить» — ОДНОГО размера (просьба пользователя:
        # соседние кнопки разной ширины выглядели неаккуратно). Высота у обеих
        # 36 px, ширину берём по более широкой из двух — так подписи не режутся.
        self._pick_btn = pick_btn
        self._add_btn = add_btn
        _btn_w = max(pick_btn.sizeHint().width(), add_btn.sizeHint().width())
        pick_btn.setFixedWidth(_btn_w)
        add_btn.setFixedWidth(_btn_w)

        addrow.addWidget(self.add_input, 1)
        addrow.addWidget(pick_btn)
        addrow.addWidget(add_btn)
        sec.add_layout(addrow)

        # ── заголовок списка ──────────────────────────────────────────
        # Строка в отдельном виджете: в свёрнутом виде (когда блок уходит «в упор
        # до кнопок») её надо прятать вместе с фильтром и списком — спрятать
        # layout нельзя, только виджет.
        list_head_host = QWidget()
        list_head_host.setStyleSheet("background:transparent;border:none;")
        self._list_head_host = list_head_host
        list_head = QHBoxLayout(list_head_host)
        list_head.setContentsMargins(0, 0, 0, 0)
        list_head.setSpacing(8)
        list_title = QLabel("Активные записи")
        list_title.setStyleSheet(
            f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;border:none;"
        )
        self._manual_count = QLabel("—")
        self._manual_count.setStyleSheet(
            f"color:{theme.ACCENT3};font-size:11px;font-weight:700;background:transparent;border:none;"
        )
        list_head.addWidget(list_title)
        list_head.addStretch()
        list_head.addWidget(self._manual_count)
        # Высота закреплена: иначе на высокой вкладке строка растягивается (и
        # «Активные записи» уезжают от фильтра), а высоту блока считать нельзя.
        list_head_host.setFixedHeight(max(list_head.sizeHint().height(), 16))
        sec.add_widget(list_head_host)

        # ── строка поиска ─────────────────────────────────────────────
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 Фильтр по домену...")
        self._search_input.setFixedHeight(30)
        self._search_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.SUBTEXT};"
            f"border:1px solid {theme.BORDER};border-radius:6px;padding:0 8px;font-size:12px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};color:{theme.TEXT};}}")
        self._search_input.textChanged.connect(self._on_search)
        sec.add_widget(self._search_input)

        # ── список ────────────────────────────────────────────────────
        # ── список: телеграмизация — один paintEvent вместо QScrollArea ──
        self._manual_canvas = ManualCanvas()
        # Минимум для раскладки — 70 (предпочтительная высота 200, см.
        # ManualCanvas.PREFERRED_H): при низком окне список ужимается и
        # прокручивается сам, полосы прокрутки поперёк колонки нет и содержимое
        # не выезжает за нижний край вкладки. Минимум виджета тянет НЕ
        # ограничивает — во время тяги высота задаётся жёстко (см. ниже).
        self._manual_canvas.setMinimumHeight(70)
        # Свёрнут ли блок до шапки и кнопок (см. _set_dispatcher_collapsed).
        self._dispatcher_collapsed = False
        self._manual_canvas.subscriptionRemoved.connect(self._remove_subscription)
        self._manual_canvas.serviceToggled.connect(self._toggle_service)
        self._manual_canvas.itemRemoved.connect(self._remove)
        sec.add_widget(self._manual_canvas)
        return sec

    def _on_search(self, text: str):
        """Фильтрует список по тексту — фильтрацию применяет сам канвас."""
        self._manual_canvas.apply_search(text)


    def _build_right(self) -> QWidget:
        # Рисуемая панель вместо QFrame+QSS (QSS-скругление стоило ~3.5 мс/кадр)
        # Фон панели — ПЛОТНЫЙ, без полупрозрачности. Было «стекло»
        # rgba(10,10,20,0.50): в узком окне панель выезжает ПОВЕРХ списка сервисов,
        # и через неё просвечивали кнопки и карточки (замечание пользователя:
        # «появившаяся часть маршрутов прозрачная, через неё видно кнопки»).
        # Цвет — композит того же стекла на фоне окна, поэтому в широком окне
        # панель выглядит ровно как раньше.
        panel_bg = theme.opaque(theme.CARD_DARK)
        panel = RoundedPanel(panel_bg, theme.ACCENT, radius=18)
        panel.setFixedWidth(300)
        # Кэшированное свечение вместо QGraphicsDropShadowEffect (−3.7 мс/кадр):
        # эффект пересчитывал blur на каждом кадре, GlowWrap рисует готовый pixmap.
        # Поля подобраны так, чтобы панель осталась на прежнем месте, а
        # минимальная высота вкладки не выросла: справа/снизу свечение
        # заканчивается в прежних отступах вкладки (outer справа/снизу = 0).
        wrap = GlowWrap(panel, theme.ACCENT, blur=22, dy=6, radius=18,
                        margins=(32, 14, 24, 12))
        self._right_panel = wrap

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        # ── 0) ТАБЫ ДЛЯ COMBO РЕЖИМА ──
        self._tabs_widget = QWidget()
        self._tabs_widget.setStyleSheet("background:transparent;border:none;")
        self._tabs_lay = QHBoxLayout(self._tabs_widget)
        self._tabs_lay.setContentsMargins(0, 0, 0, 0)
        self._tabs_lay.setSpacing(6)

        self._dns_tab_btn = QPushButton("🔌 DNS")
        self._dns_tab_btn.setCursor(Qt.PointingHandCursor)
        self._dns_tab_btn.setFixedHeight(32)
        
        self._dpi_tab_btn = QPushButton("🛡 DPI")
        self._dpi_tab_btn.setCursor(Qt.PointingHandCursor)
        self._dpi_tab_btn.setFixedHeight(32)
        
        self._tabs_lay.addWidget(self._dns_tab_btn)
        self._tabs_lay.addWidget(self._dpi_tab_btn)
        lay.addWidget(self._tabs_widget)
        
        self._dns_tab_btn.clicked.connect(lambda: self._set_sidebar_tab("dns"))
        self._dpi_tab_btn.clicked.connect(lambda: self._set_sidebar_tab("dpi"))

        self._active_sidebar_tab = "dns"

        # ── 1) ФИКСИРОВАННАЯ КАРТОЧКА-ОКНО ДЛЯ СПИСКОВ (БЕЗ СВОРАЧИВАНИЯ) ──
        # Вложенная карточка панели — тоже плотная (композит стекла на панели).
        self._right_card = RoundedPanel(theme.opaque(theme.CARD_DARK, panel_bg),
                                       theme.BORDER, radius=14)
        card_lay = QVBoxLayout(self._right_card)
        card_lay.setContentsMargins(12, 12, 12, 12)
        card_lay.setSpacing(10)

        # Заголовок карточки
        self._card_title = QLabel("🔌  Маршрут DNS")
        self._card_title.setStyleSheet(f"color:{theme.TEXT};font-size:14px;font-weight:700;background:transparent;border:none;")
        card_lay.addWidget(self._card_title)

        # Разделитель
        self._card_sep = QFrame()
        self._card_sep.setFixedHeight(1)
        self._card_sep.setStyleSheet(f"background:{theme.BORDER};border:none;")
        card_lay.addWidget(self._card_sep)

        # DNS Маршруты (TransportList)
        self._transport_list = TransportList()
        self._transport_list.transportChanged.connect(self._on_transport_change)
        card_lay.addWidget(self._transport_list)

        # DPI Стратегии (DpiStrategyList)
        from umbranet.widgets.dpi_strategy_list import DpiStrategyList
        self._dpi_strategy_list = DpiStrategyList()
        card_lay.addWidget(self._dpi_strategy_list)

        lay.addWidget(self._right_card)

        # ── 2) ДЕТАЛИ DNS ПРОФИЛЯ ──
        self._dns_profile_container = QWidget()
        self._dns_profile_container.setStyleSheet("background:transparent;border:none;")
        dns_prof_lay = QVBoxLayout(self._dns_profile_container)
        dns_prof_lay.setContentsMargins(0, 0, 0, 0)
        dns_prof_lay.setSpacing(8)

        self._prof_title = QLabel("🛡  —")
        self._prof_title.setStyleSheet(f"color:{theme.TEXT};font-size:14px;font-weight:700;background:transparent;border:none;")
        dns_prof_lay.addWidget(self._prof_title)

        self._prof_rows_host = QWidget()
        self._prof_rows_host.setStyleSheet("background:transparent;")
        self._prof_rows_lay = QVBoxLayout(self._prof_rows_host)
        self._prof_rows_lay.setContentsMargins(0, 0, 0, 0)
        self._prof_rows_lay.setSpacing(6)
        dns_prof_lay.addWidget(self._prof_rows_host)

        lay.addWidget(self._dns_profile_container)

        # растяжка — контент панели прижат к верху
        lay.addStretch()

        # Правая часть прокручивается ВНУТРИ СЕБЯ (по просьбе пользователя: это
        # одна из трёх областей, которым прокрутка разрешена — вместе со списком
        # сервисов и «Диспетчером задач»). Если окно ниже карточки «Маршрут DNS»,
        # полоса появляется у правого края, а не поперёк всей вкладки; при
        # достаточной высоте полос нет вовсе.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # Горизонтальную полосу держим выключенной: панель фиксированной ширины
        # (300 px), сжимать её нечего, а полоса вылезала снизу просто потому, что
        # вертикальная забрала 12 px ширины. Панель при этом остаётся видна целиком.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}"
            + theme.scrollbar_qss()
        )
        scroll.setWidget(wrap)
        return scroll

    # ════════════════ высота «Диспетчера задач» ════════════════

    #: Ключ в общем состоянии UI (core/ui_state.py): выбранная человеком высота
    #: списка «Диспетчера задач» — чтобы после перезапуска всё было как оставили.
    DISPATCHER_HEIGHT_KEY = "routing_dispatcher_height"

    # Что может выкинуть общее состояние UI: нет модуля, битый файл, чужой тип.
    # Широкий except здесь запрещён линтером и не нужен — список исчерпывающий.
    _UI_STATE_ERRORS = (ImportError, OSError, ValueError, TypeError, KeyError)

    def _dispatcher_stub_h(self) -> int:
        """Высота блока, когда показаны только линия, название и строка кнопок."""
        try:
            sec = self._manual_section
            header_h = sec.header.height() or sec.header.sizeHint().height()
            return header_h + max(0, sec.lay.spacing()) + self._add_btn.height()
        except (AttributeError, RuntimeError):
            return 70

    def _dispatcher_extras_h(self) -> int:
        """Надпись «Активные записи» + строка фильтра — всё, что прячется вместе."""
        try:
            spacing = max(0, self._manual_section.content_lay.spacing())
            head_h = self._list_head_host.height() or self._list_head_host.sizeHint().height()
            # Зазор перед надписью тоже относится к «начинке»: он исчезает вместе
            # с ней, поэтому без него высота блока считалась бы на 4 px меньше.
            return (spacing + head_h + spacing
                    + self._search_input.height() + spacing)
        except (AttributeError, RuntimeError):
            return 0

    def _dispatcher_collapse_at_h(self) -> int:
        """Высота блока, ниже которой начинка прячется: шапка, кнопки и надпись с фильтром.

        Места для списка уже не остаётся (он бы получил 0 px) — значит, прячем
        начинку целиком, и блок уходит ниже, «в упор до кнопок» (просьба
        пользователя). Запас взят минимальный: чем он больше, тем длиннее
        «мёртвая» полоса, которую надо протянуть мышью, чтобы блок снова раскрылся.
        """
        return self._dispatcher_stub_h() + self._dispatcher_extras_h()

    def _dispatcher_min_block_h(self) -> int:
        """Минимальная высота БЛОКА: линия, название и строка с кнопками."""
        return self._dispatcher_stub_h()

    def _dispatcher_max_block_h(self) -> int:
        """Максимальная высота блока: колонка минус всё, что выше (сервисы в минимуме)."""
        try:
            return max(self._dispatcher_min_block_h(),
                       self._left_column.height() - self._services_block_min_h())
        except (AttributeError, RuntimeError):
            return self._dispatcher_min_block_h() + self._manual_canvas.height()

    def _dispatcher_floor(self) -> int:
        """Минимальная высота списка: 0 — список убирается целиком.

        Раньше низ упирался в 70 px (пара карточек). Пользователь попросил, чтобы
        блок уходил ниже — «в упор до кнопок Процесс и Добавить»: тогда в блоке
        остаются только фиолетовая линия, название и кнопки, а список, надпись
        «Активные записи» и фильтр прячутся.
        """
        return 0

    def _set_dispatcher_collapsed(self, collapsed: bool):
        """Прячет/показывает начинку блока (надпись, фильтр, список)."""
        collapsed = bool(collapsed)
        if getattr(self, "_dispatcher_collapsed", None) == collapsed:
            return
        self._dispatcher_collapsed = collapsed
        for widget in (self._list_head_host, self._search_input, self._manual_canvas):
            widget.setVisible(not collapsed)

    def _dispatcher_chrome_h(self) -> int:
        """Высота части блока, которая не является списком (шапка, кнопки, фильтр)."""
        try:
            if self._dispatcher_collapsed:
                return self._manual_section.height()   # список спрятан — весь блок «шапка»
            return max(0, self._manual_section.height() - self._manual_canvas.height())
        except (AttributeError, RuntimeError):
            return 0

    def _services_block_min_h(self) -> int:
        """Высота всего, что выше «Диспетчера задач», когда сервисы сжаты до минимума.

        Считаем не по текущей раскладке, а по минимумам: при уменьшении окна
        раскладка оказывается «в середине» перестройки, и любые замеры текущих
        высот дают смесь старого и нового — из-за этого блок раньше не ужимался
        и вылезал за нижний край вкладки.
        """
        lay = self._left_column.layout()
        if lay is None:
            return 0
        total = 0
        for i in range(lay.count()):
            item = lay.itemAt(i)
            widget = item.widget()
            if widget is self._manual_section:
                break
            if widget is None:
                # Явная распорка (addSpacing) — раньше роль зазоров играл spacing
                # раскладки, теперь зазоры заданы распорками.
                total += max(0, item.sizeHint().height())
            elif widget is self._canvas:
                total += self._canvas.minimumHeight()
            else:
                total += max(widget.height(), widget.minimumSizeHint().height())
        return total

    def _dispatcher_max_h(self) -> int:
        """Сколько пикселей может занять список записей, не отняв лишнего у сервисов.

        Низ блока прибит к низу колонки, поэтому предел: высота колонки минус
        «несъедобная» часть блока минус всё, что выше (сервисы в минимуме). Это же
        ограничение само работает при уменьшении окна по высоте.
        """
        try:
            block_max = self._left_column.height() - self._services_block_min_h()
        except (AttributeError, RuntimeError):
            return max(0, self._manual_canvas.height())
        if block_max < self._dispatcher_collapse_at_h():
            return 0        # места хватает только на шапку с кнопками
        return max(0, block_max - self._dispatcher_stub_h() - self._dispatcher_extras_h())

    def _apply_dispatcher_height(self):
        """Применяет выбранную высоту (с поправкой на текущее место в колонке).

        Выбранная высота — высота БЛОКА целиком: линия тянется за курсором, и
        именно блок (а не список) человек двигает мышью.
        """
        if not self._dispatcher_sized or self._dispatcher_pref is None:
            return
        target = max(self._dispatcher_min_block_h(),
                     min(int(self._dispatcher_pref), self._dispatcher_max_block_h()))
        self._set_dispatcher_collapsed(target <= self._dispatcher_collapse_at_h())
        if self._dispatcher_collapsed:
            list_h = 0
        else:
            list_h = max(0, target - self._dispatcher_stub_h() - self._dispatcher_extras_h())
        if self._manual_canvas.height() != list_h:
            self._manual_canvas.setFixedHeight(list_h)

    def _on_dispatcher_drag_begin(self):
        # Запоминаем высоту БЛОКА на момент нажатия: движение считается от неё,
        # поэтому линия «держится» за курсор и не прыгает. Высота списка для этого
        # не годится — в свёрнутом виде список спрятан, и тяга шла бы рывком.
        self._dispatcher_drag_h = self._manual_section.height()

    def _on_dispatcher_drag_move(self, dy: int):
        """Тяга за фиолетовую линию: вверх — блок выше, вниз — ниже.

        Низ блока прибит к низу колонки, поэтому движение линии вверх означает
        большую высоту блока. dy — смещение курсора вниз от точки нажатия.
        """
        if self._dispatcher_drag_h is None:
            return
        target = max(self._dispatcher_min_block_h(),
                     min(self._dispatcher_drag_h - int(dy), self._dispatcher_max_block_h()))
        self._dispatcher_sized = True
        self._dispatcher_pref = target
        self._apply_dispatcher_height()

    def _on_dispatcher_drag_end(self):
        self._dispatcher_drag_h = None
        self._save_dispatcher_height()

    def _save_dispatcher_height(self):
        """Выбранная высота блока — в общее состояние UI (не критично, ошибки глотаем)."""
        if not self._dispatcher_sized:
            return
        try:
            import ui_state as _ui_state
            _ui_state.update_state(**{self.DISPATCHER_HEIGHT_KEY: int(self._dispatcher_pref)})
        except self._UI_STATE_ERRORS as exc:                # состояние UI — не повод падать
            log.debug("Высота «Диспетчера задач» не сохранилась: %s", exc)

    def _load_dispatcher_height(self):
        """Возвращает сохранённую высоту БЛОКА (или None), если она выглядит разумной.

        Ноль — не ошибка: это свёрнутый блок (линия, название и кнопки).
        """
        try:
            import ui_state as _ui_state
            value = _ui_state.get_value(self.DISPATCHER_HEIGHT_KEY, None)
        except self._UI_STATE_ERRORS as exc:
            log.debug("Сохранённая высота «Диспетчера задач» не прочиталась: %s", exc)
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if 0 <= value <= 4000 else None

    # ════════════════ узкое окно: правая панель ════════════════

    def is_narrow(self) -> bool:
        """Спрятана ли правая часть из-за узкого окна."""
        return self._narrow

    def _panel_width(self) -> int:
        """Ширина правой части вместе со свечением (её место в раскладке)."""
        hint = self._right_panel.sizeHint().width()
        if hint > 200:
            self._panel_w = hint
        return self._panel_w

    def set_narrow_mode(self, narrow: bool, force: bool = False) -> bool:
        """Прячет/показывает правую часть при сужении окна.

        В узком окне правая часть убирается ЦЕЛИКОМ (и маршрут, и данные профиля):
        её место отдаётся спискам, а сама панель доступна по кнопке в шапке вкладки
        — выезжает поверх списка (см. toggle_right_panel). В широком окне всё как
        раньше: панель стоит справа, место под неё держит распорка раскладки.

        Вызывается главным окном в тот момент, когда кнопки режимов ещё целые, но
        вот-вот начнут ужиматься: так исчезновение панели не совпадает с моментом,
        когда меняются кнопки, и ничего не «дёргается» дважды.
        """
        narrow = bool(narrow)
        if narrow == self._narrow and not force:
            return False
        self._narrow = narrow
        self.close_right_panel()
        if narrow:
            # Правая панель ушла — возвращаем правый отступ, равный левому: списки
            # и кнопки не должны липнуть к краю окна.
            left, top, _right, bottom = self._page_margins
            self._outer.setContentsMargins(left, top, left, bottom)
            # Распорку убираем из раскладки целиком (а не оставляем нулевой):
            # иначе её зазор в 16 px съедал бы ширину у списков.
            self._body_layout.removeWidget(self._right_spacer)
            self._right_spacer.hide()
            self._right_scroll.hide()
            self._narrow_toggle.setVisible(True)
        else:
            self._outer.setContentsMargins(*self._page_margins)
            self._narrow_toggle.setVisible(False)
            self._right_spacer.setFixedWidth(self._panel_width())
            if self._body_layout.indexOf(self._right_spacer) < 0:
                self._body_layout.addWidget(self._right_spacer)
            self._right_spacer.show()
            self._right_scroll.show()
            self._place_right_panel()
        self._sync_right_panel_title()
        return True

    def _place_right_panel(self):
        """Ставит правую часть: в узком окне — поверх списка, в широком — на месте."""
        if not self._right_scroll.isVisible():
            return
        # Ширина всегда одна и та же (панель фиксированной ширины + поля свечения):
        # иначе виджет один раз получил бы «ширину по умолчанию» и застыл с ней.
        w = self._panel_width()
        self._right_scroll.setFixedWidth(w)
        if self._narrow:
            # Поверх содержимого, но НИЖЕ строки заголовка: кнопка «Маршрут DNS»
            # должна оставаться на виду и нажиматься (ею же панель и закрывается).
            margin = 12
            y = self._content_top()
            self._right_scroll.setGeometry(
                max(0, self.width() - w - margin), y, w, max(80, self.height() - y - margin))
        else:
            # На месте из раскладки: там, где стоит распорка.
            area = self._body_layout.geometry()
            self._right_scroll.setGeometry(
                area.right() - w + 1, area.top(), w, max(80, area.height()))
            # Панель плавающая (в раскладке её нет), поэтому её положение нужно
            # подтверждать и в стеке: иначе её перекроет любой сосед, добавленный
            # позже, — и весь её содержимое перестанет нажиматься.
            self._right_scroll.raise_()

    def toggle_right_panel(self):
        """Кнопка в шапке вкладки: показать/спрятать панель поверх списка."""
        if self._overlay_open:
            self.close_right_panel()
        else:
            self.open_right_panel()

    def open_right_panel(self):
        """Открывает правую панель поверх списка (только в узком окне)."""
        if not self._narrow:
            return
        self._overlay_open = True
        top = self._content_top()
        self._backdrop.setGeometry(0, top, self.width(), max(0, self.height() - top))
        self._backdrop.show()
        self._backdrop.raise_()
        self._right_scroll.show()
        self._right_scroll.raise_()
        self._place_right_panel()
        self._narrow_toggle.setText("✕  Скрыть")
        self._narrow_toggle.raise_()

    def close_right_panel(self):
        """Закрывает панель, выехавшую поверх списка (клик мимо, Esc или кнопка)."""
        if not self._overlay_open:
            return
        self._overlay_open = False
        self._backdrop.hide()
        if self._narrow:
            self._right_scroll.hide()
        self._sync_right_panel_title()

    def _content_top(self) -> int:
        """Низ строки заголовка вкладки: выше неё панель и затемнение не заходят."""
        try:
            bottom = self._narrow_toggle.mapTo(self, self._narrow_toggle.rect().bottomLeft()).y()
            return max(0, bottom + 8)
        except (AttributeError, RuntimeError):
            return 44

    def _sync_right_panel_title(self):
        """Текст кнопки повторяет заголовок панели («Маршрут DNS» / «Стратегия DPI»)."""
        try:
            if not self._overlay_open:
                self._narrow_toggle.setText(self._card_title.text())
        except (AttributeError, RuntimeError):
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._overlay_open:
            top = self._content_top()
            self._backdrop.setGeometry(0, top, self.width(), max(0, self.height() - top))
        self._place_right_panel()
        # Высота «Диспетчера задач» выбрана человеком, но она не должна выдавливать
        # список сервисов ниже его минимума, когда окно становится ниже. Считаем
        # дважды: сразу (по минимумам — это уже корректно) и один раз отложенно,
        # когда раскладка окончательно устаканится после изменения размера.
        self._apply_dispatcher_height()
        QTimer.singleShot(0, self._apply_dispatcher_height)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._overlay_open:
            self.close_right_panel()
            event.accept()
            return
        super().keyPressEvent(event)

    def _set_sidebar_tab(self, tab_key: str):
        self._active_sidebar_tab = tab_key
        
        # Обновляем стили кнопок вкладок
        active_style = (
            f"QPushButton{{"
            f"  background: {theme.ACCENT};"
            f"  color: {theme.WHITE};"
            f"  border: 1px solid {theme.ACCENT};"
            "  border-radius: 8px;"
            "  font-weight: bold;"
            "}"
        )
        inactive_style = (
            f"QPushButton{{"
            f"  background: {theme.INPUT_BG};"
            f"  color: {theme.SUBTEXT};"
            f"  border: 1px solid {theme.BORDER};"
            "  border-radius: 8px;"
            "}"
            f"QPushButton:hover{{"
            f"  border-color: {theme.ACCENT3};"
            f"  color: {theme.TEXT};"
            "}"
        )
        
        if tab_key == "dns":
            self._dns_tab_btn.setStyleSheet(active_style)
            self._dpi_tab_btn.setStyleSheet(inactive_style)
            
            # Показываем DNS, скрываем DPI
            self._transport_list.setVisible(True)
            self._dpi_strategy_list.setVisible(False)
            self._card_title.setText("🔌  Маршрут DNS")
            self._dns_profile_container.setVisible(True)
            self._sync_right_panel_title()
        else:
            self._dns_tab_btn.setStyleSheet(inactive_style)
            self._dpi_tab_btn.setStyleSheet(active_style)
            
            # Показываем DPI, скрываем DNS
            self._transport_list.setVisible(False)
            self._dpi_strategy_list.setVisible(True)
            self._card_title.setText("🛡  Стратегия DPI")
            self._dns_profile_container.setVisible(False)
            self._sync_right_panel_title()

    def _on_transport_change(self, mode: str):
        """Транспорт сменился — обновляем конфиг и UI."""
        self.refresh()
        if self.engine.running:
            pass

    # ════════════════ логика ════════════════
    def _apply(self):
        save_config(self.engine.config)
        # Сбрасываем кеш списка — данные изменились
        self._manual_list_key = None
        # Сообщаем шапке что хостлист изменился — Старт может стать серым/активным
        try:
            from umbranet.engine_adapter import post_event
            post_event({"type": "config_changed", "section": "routing"})
        except Exception:
            pass
        # Мгновенно обновляем кнопку Старт в шапке (не ждём 200мс таймера)
        try:
            from PySide6.QtWidgets import QApplication
            for w in QApplication.topLevelWidgets():
                if hasattr(w, "_update_start_button"):
                    w._update_start_button()
                    break
        except Exception:
            pass

        if self.engine.running:
            self._restart_dns_after_route_change()
        else:
            self.engine.reload_config()
            self.refresh()

    def _restart_dns_after_route_change(self):
        """Перезапускает DNS после изменения маршрутов без смены системного DNS."""
        worker = getattr(self, "_route_restart_worker", None)
        if worker is not None and worker.isRunning():
            self._route_restart_pending = True
            return
        self._route_restart_pending = False
        self._route_restart_worker = _DnsRestartWorker(self.engine)
        self._route_restart_worker.done.connect(self._on_route_restart_done)
        self._route_restart_worker.start()

    def _on_route_restart_done(self, ok: bool):
        if not ok:
            log.warning("DNS restart после изменения маршрутов завершился с ошибкой")
        if self._route_restart_pending:
            self._restart_dns_after_route_change()
            return
        self.refresh()

    def _toggle_service(self, svc: str, on: bool):
        if on and not get_developer_mode() and not service_allowed_in_mode(svc, get_current_mode()):
            return
        _, _, domains = SERVICES[svc]
        routed = self.engine.config.setdefault("routed_domains", [])
        if on:
            for d in domains:
                if d not in routed:
                    routed.append(d)
        else:
            routed[:] = [r for r in routed if r not in domains]
        self._apply()

    def _add_typed(self):
        raw_text = (self.add_input.text() or "").strip()
        if not raw_text:
            return

        # ── ПРОВЕРКА НА ПОДПИСКУ (URL) ──
        if raw_text.lower().startswith(("http://", "https://")):
            if self.engine.change_subscription(raw_text):
                self.add_input.setEnabled(False)
                self.add_input.setPlaceholderText("⏳  Загрузка подписки...")

                from umbranet.engine_adapter import update_subscriptions_async

                def on_done(ok, count):
                    self._subscription_done.emit(bool(ok), int(count), True)

                update_subscriptions_async(on_done)
            return  # An existing subscription must not fall through as a domain.

        import re
        tokens = re.split(r'[\s,;\n]+', raw_text)

        cfg = self.engine.config
        added_any = False

        for token in tokens:
            val = token.strip()
            if not val:
                continue

            for pre in ("https://", "http://", "www."):
                if val.lower().startswith(pre):
                    val = val[len(pre):]

            val = val.split("/")[0].strip().rstrip(".")
            if not val:
                continue

            key = "routed_processes" if val.lower().endswith(".exe") else "routed_domains"
            lst = cfg.setdefault(key, [])
            if val not in lst:
                lst.append(val)
                added_any = True

        self.add_input.clear()
        if added_any:
            self._apply()

    def _pick_process(self):
        dlg = ProcessPickerDialog(self)
        if dlg.exec() and dlg.result:
            name = dlg.result
            lst = self.engine.config.setdefault("routed_processes", [])
            if name not in lst:
                lst.append(name)
            self._apply()

    def _remove(self, name: str, key: str):
        # Защищаем дефолтные процессы — их удаление ломает per-app маршрутизацию.
        # Кнопки ✕ у них в списке нет (manual_canvas), это второй барьер.
        if key == "routed_processes" and is_protected_process(name):
            # Тихо игнорируем — не даём удалить, чтобы не было проблем
            return
        if key == "routed_subscriptions":
            self._remove_subscription(name)
            return
        if name in self.engine.config.get(key, []):
            self.engine.config[key].remove(name)
        self._apply()

    def _remove_subscription(self, url: str):
        if self.engine.change_subscription(url, remove=True):
            self.add_input.setEnabled(False)
            self.add_input.setPlaceholderText("⏳  Удаление подписки...")

            from umbranet.engine_adapter import update_subscriptions_async

            def on_done(ok, count):
                self._subscription_done.emit(bool(ok), int(count), False)

            update_subscriptions_async(on_done)

    def _on_subscription_done(self, ok: bool, count: int, clear_input: bool):
        """Применяет результат обновления подписки в GUI-потоке."""
        self.add_input.setEnabled(True)
        if clear_input:
            self.add_input.clear()
        self.add_input.setPlaceholderText("chatgpt.com или chrome.exe")
        if not ok:
            log.warning("Обновление подписок завершилось без записи кэша")
        self._apply()

    # ════════════════ обновление ════════════════
    def refresh(self):
        cfg = self.engine.config

        # телеграмизация: состояния считает один канвас, анимируются
        # только реально изменившиеся тумблеры (никаких 26 виджетов).
        # Сервис включён, если ВСЕ его домены маршрутизируются
        # (поддомены учитывает логика ядра).
        service_states = {}
        for svc in SERVICES:
            _, _, domains = SERVICES[svc]
            service_states[svc] = bool(domains) and all(
                is_domain_routed(d, cfg) for d in domains)
        self._canvas.set_service_states(service_states)

        # ручной список (домены не из пресетов + процессы)
        self._rebuild_manual_list()

        # правая панель
        prof = get_active_dns_profile(cfg)
        self._prof_title.setText(f"🛡  {prof.get('name', '—')}")
        self._transport_list.refresh()
        self._rebuild_profile_rows(prof)

        # Переключение видимости в зависимости от текущего режима
        mode = get_current_mode()
        self._canvas.set_app_mode(mode)
        self._canvas.set_developer_mode(get_developer_mode())
        self._dpi_strategy_list.refresh()

        if mode == "dns_only":
            self._tabs_widget.setVisible(False)
            self._transport_list.setVisible(True)
            self._dpi_strategy_list.setVisible(False)
            self._card_title.setText("🔌  Маршрут DNS")
            self._dns_profile_container.setVisible(True)
            self._sync_right_panel_title()
        elif mode == "dpi_only":
            self._tabs_widget.setVisible(False)
            self._transport_list.setVisible(False)
            self._dpi_strategy_list.setVisible(True)
            self._card_title.setText("🛡  Стратегия DPI")
            self._dns_profile_container.setVisible(False)
            self._sync_right_panel_title()
        elif mode == "combo":
            self._tabs_widget.setVisible(True)
            self._set_sidebar_tab(self._active_sidebar_tab)

    # порядок и подписи параметров профиля для правой панели главного меню
    # (DoT/DoQ детали тут НЕ показываем — они нужны только в редакторе профиля)
    _PROFILE_FIELDS = [
        ("ipv4_primary", "IPv4 основной"),
        ("ipv4_secondary", "IPv4 резерв"),
        ("ipv6_primary", "IPv6 основной"),
        ("ipv6_secondary", "IPv6 резерв"),
        ("doh_url", "DoH URL"),
        ("dnscrypt_stamp", "DNSCrypt"),
    ]

    def _rebuild_profile_rows(self, prof: dict):
        # очистить прежние строки (виджеты-строки удаляются целиком)
        while self._prof_rows_lay.count():
            item = self._prof_rows_lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()
        # добавить все НЕпустые поля как отдельные виджеты-строки
        for key, label in self._PROFILE_FIELDS:
            val = prof.get(key)
            if val in (None, "", 0):
                continue
            row = QWidget(self._prof_rows_host)
            row.setStyleSheet("background:transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            k = QLabel(label)
            k.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
            v = QLabel(str(val))
            v.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:600;background:transparent;border:none;")
            v.setWordWrap(True)
            v.setAlignment(Qt.AlignRight | Qt.AlignTop)
            rl.addWidget(k, 0, Qt.AlignTop)
            rl.addStretch()
            rl.addWidget(v, 1)
            self._prof_rows_lay.addWidget(row)

    # Карта: домен -> имя сервиса (строится один раз из SERVICES)
    @staticmethod
    def _build_domain_to_service() -> dict:
        result = {}
        for svc, (_, _, domains) in SERVICES.items():
            for d in domains:
                result[d] = svc
        return result

    def _ensure_default_processes(self):
        """Гарантирует что chrome/msedge/firefox всегда в списке (защита от случайного удаления)."""
        try:
            cfg = self.engine.config
            procs = cfg.setdefault("routed_processes", [])
            existing = [str(x).lower() for x in procs]
            for need in protected_processes():
                if need not in procs and need.lower() not in existing:
                    procs.append(need)
        except Exception:
            pass

    def _rebuild_manual_list(self, filter_text: str = ""):
        """Показывает диспетчер задач: все домены, процессы и подписки.

        Телеграмизация: данные собираются как раньше (подписки -> домены ->
        процессы, домены пресетов с меткой сервиса), но рисует их один
        paintEvent (ManualCanvas) — ни одной карточки-QFrame на строку.
        Поиск, пустое состояние и «нет совпадений» канвас рисует сам.

        Кешируем последний набор данных — обновляем канвас только если
        содержимое реально изменилось (фильтр в канвасе, не здесь).
        """
        self._ensure_default_processes()
        cfg = self.engine.config
        domain_to_svc = self._build_domain_to_service()

        current_key = (
            tuple(sorted(cfg.get("routed_domains", []) or [])),
            tuple(sorted(cfg.get("routed_processes", []) or [])),
            tuple(sorted(cfg.get("routed_subscriptions", []) or [])),
        )
        if getattr(self, "_manual_list_key", None) == current_key:
            # имена те же, но иконка процесса могла появиться позже
            if hasattr(self, "_manual_canvas"):
                self._manual_canvas.try_fill_process_icons()
            return
        self._manual_list_key = current_key

        # Собираем записи: {name, key, icon, badge, badge_color, display}
        items = []

        # 1) Подписки — краткая подпись «хост / последняя часть пути»
        for sub_url in cfg.get("routed_subscriptions", []) or []:
            try:
                parts = sub_url.split("/")
                short_name = f"{parts[2]} / {parts[-1]}"
            except Exception:
                short_name = sub_url[:30] + "..."
            items.append({"name": sub_url, "key": "routed_subscriptions",
                          "icon": "📋", "badge": "", "badge_color": theme.ACCENT,
                          "display": short_name})

        # 2) Домены (пресетные — с меткой сервиса)
        for d in cfg.get("routed_domains", []) or []:
            svc = domain_to_svc.get(d)
            items.append({"name": d, "key": "routed_domains", "icon": "🌐",
                          "badge": svc or "", "badge_color": theme.ACCENT2 if svc else "",
                          "display": d})

        # 3) Процессы — настоящая иконка .exe, если путь нашёлся, иначе 🎮
        for pr in cfg.get("routed_processes", []) or []:
            item = {"name": pr, "key": "routed_processes", "icon": "🎮",
                    "badge": "", "badge_color": "", "display": pr,
                    # защищённые (chrome/msedge/firefox) не удаляются —
                    # канвас не рисует у них ✕ и не реагирует на клик
                    "protected": is_protected_process(pr)}
            try:
                pm = pixmap_for_process(pr)
            except Exception:
                pm = None
            if pm is not None:
                item["pixmap"] = pm
            items.append(item)

        # Сортировка: подписки (0), домены (1), процессы (2), затем по имени
        prio_map = {"routed_subscriptions": 0, "routed_domains": 1, "routed_processes": 2}
        items.sort(key=lambda it: (prio_map.get(it["key"], 1), (it["display"] or "").lower()))

        if hasattr(self, "_manual_count"):
            domains_n = len(cfg.get("routed_domains", []) or [])
            proc_n = len(cfg.get("routed_processes", []) or [])
            subs_n = len(cfg.get("routed_subscriptions", []) or [])
            self._manual_count.setText(f"{domains_n} дом. • {proc_n} проц. • {subs_n} под.")

        self._manual_canvas.set_items(items)

