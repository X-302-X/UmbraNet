"""
UmbraNet - главное окно (PySide6 / Qt Widgets).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QEvent, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.widgets.live_resize import LiveResizeFreezer
from umbranet.engine_adapter import (
    count_dpi_targets,
    dpi_strategy_ai_cleanup_runtime,
    dpi_strategy_ai_plan,
    dpi_strategy_ai_run_controlled,
    dpi_strategy_check_all_controlled,
    drain_events,
    get_current_mode,
    get_dpi_targets,
    get_engine,
    get_doctor_notify,
    get_nav_order,
    get_startup_health,
    post_event,
    routing_mode_is_explicit,
    routing_running,
    routing_start_readiness,
    routing_vpn_block_message,
    select_vpn_server_for_next_start,
    set_nav_order,
    start_routing_runtime,
    stop_routing_runtime,
    take_vpn_reserve_choice,
    switch_mode,
    verify_teardown,
)
from umbranet.views.about import AboutView
from umbranet.views.extra import ExtraView
from umbranet.views.log import LogView
from umbranet.views.network import NetworkView
from umbranet.views.routing import RoutingView
from umbranet.views.servers import ServersView
from umbranet.views.settings import SettingsView
from umbranet.views.strategy_lab import StrategyLabView
from umbranet.widgets.header import ControlBar, ModeSwitch
from umbranet.widgets.sidebar import NavItem, Sidebar
from umbranet.widgets.tray import Tray

log = logging.getLogger("UmbraNet.App")

NAV_ITEMS = [
    NavItem("routing",  "Маршрутизация",      "🔀"),
    NavItem("servers",  "Сервера",            "👁️"),
    NavItem("network",  "Сеть и диагностика", "🤖"),
    NavItem("strategy_lab", "AI-стратегии",   "🧪"),
    NavItem("log",      "Логи",               "📑"),
    NavItem("settings", "Настройки",          "⚙"),
    NavItem("about",    "О программе",        "ℹ"),
]
EXTRA_NAV = NavItem("extra", "\u041a\u0443\u0441\u044c", "😺")


def _ordered_nav_items() -> list[NavItem]:
    by_key = {it.key: it for it in NAV_ITEMS}
    order = get_nav_order([it.key for it in NAV_ITEMS])
    return [by_key[k] for k in order if k in by_key]


def _placeholder(title: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(24, 24, 24, 24)
    lbl = QLabel(f"{title}\n\n🚧 раздел в разработке")
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:18px;")
    lay.addWidget(lbl)
    return w


# ── Фоновые потоки для start/stop/restart ─────────────────────────────────────

class _EngineWorker(QThread):
    """Runs the DNS-independent VPN / Combo / DPI lifecycle away from the UI."""
    finished = Signal(str, bool)   # (action, ok)

    def __init__(self, action: str):
        super().__init__()
        self.action = action
        self.message = ""
        # Restart consists of an explicit PAC restore followed by Start. Keep
        # this fact for the GUI so a failed restore cannot be hidden if the
        # subsequent Start also fails before committing a new PAC.
        self.restore_failed = False
        # QThread cannot safely be killed while it owns a route lifecycle.  A
        # real application exit therefore asks it to stop and waits; checks on
        # both sides of Start prevent a late worker from resurrecting e1-spike
        # or the PAC gateway after the shutdown cleanup.
        self._shutdown_requested = False

    def request_shutdown(self):
        self._shutdown_requested = True

    def _stop_after_shutdown_request(self, message: str) -> tuple[bool, str]:
        stopped, stop_message = stop_routing_runtime()
        detail = stop_message or "маршрутизация остановлена"
        return bool(stopped), f"{message}; {detail}"

    def run(self):
        ok = False
        try:
            if self.action == "start":
                if self._shutdown_requested:
                    ok, self.message = self._stop_after_shutdown_request("Выход отменил запуск")
                else:
                    ok, self.message = start_routing_runtime()
                    if self._shutdown_requested:
                        ok, self.message = self._stop_after_shutdown_request("Выход отменил запуск")
            elif self.action == "stop":
                ok, self.message = stop_routing_runtime()
            else:
                stopped, stop_message = stop_routing_runtime()
                self.restore_failed = not stopped
                if self._shutdown_requested:
                    ok = bool(stopped)
                    self.message = stop_message or "Выход отменил перезапуск"
                else:
                    ok, self.message = start_routing_runtime()
                    if self._shutdown_requested:
                        ok, self.message = self._stop_after_shutdown_request("Выход отменил перезапуск")
                if not ok and stop_message:
                    self.message = f"{stop_message}; {self.message}"
        except Exception as exc:
            self.message = str(exc)
            log.error("_EngineWorker(%s) ошибка: %s", self.action, exc)
            ok = False
        finally:
            self.finished.emit(self.action, ok)


class _StartupHealthWorker(QThread):
    """Предстартовая диагностика в фоне — без фризов UI."""
    done = Signal(dict)

    def run(self):
        try:
            self.done.emit(get_startup_health())
        except Exception as exc:  # noqa: BLE001
            self.done.emit({
                "severity": "warning",
                "can_start": True,
                "summary": f"Не удалось выполнить предстартовую проверку: {exc}",
                "problems": [],
                "warnings": [str(exc)],
            })


class _AiGenerationWorker(QThread):
    """Controlled AI-generation runner в фоне."""
    progress = Signal(str)
    done = Signal(dict)

    def __init__(self, mode: str = "quick"):
        super().__init__()
        self.mode = mode

    def request_cancel(self):
        """Просит controlled AI-generation остановиться как можно быстрее."""
        self.requestInterruption()
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def run(self):
        try:
            result = dpi_strategy_ai_run_controlled(
                self.mode,
                on_progress=lambda text: self.progress.emit(str(text)),
                should_cancel=lambda: self.isInterruptionRequested(),
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "stage": "ai_generation", "error": str(exc), "created_id": ""}
        self.done.emit(result)




class _StrategyCheckWorker(QThread):
    """Controlled проверка всех Uz-стратегий."""
    progress = Signal(str)
    done = Signal(dict)

    def request_cancel(self):
        self.requestInterruption()
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def run(self):
        try:
            result = dpi_strategy_check_all_controlled(
                on_progress=lambda text: self.progress.emit(str(text)),
                should_cancel=lambda: self.isInterruptionRequested(),
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "stage": "strategy_check", "error": str(exc), "results": []}
        self.done.emit(result)


class GlowContainer(QWidget):
    """Фон окна: плоская заливка + две статичные туманности по углам.

    Динамический прожектор за курсором удалён по решению юзера
    (коммит в истории — вернуть можно оттуда одним реверсом).

    ПРОИЗВОДИТЕЛЬНОСТЬ:
      - Туманности преeндерены в маленькие QPixmap-спрайты и привязаны
        к углам окна: paintEvent = заливка + два быстрых drawPixmap.
      - Спрайтам размер окна не важен — при resize ничего не пересобирается
        и не «телепортируется», фон едет вместе с краем на каждом шаге.
    """

    RESIZE_SETTLE_MS = 150  # пауза после последнего resize-события, когда размер «устоялся»

    def __init__(self, parent=None):
        super().__init__(parent)
        # Спрайты туманностей: радиальные градиенты преeндерены один раз
        # в маленькие QPixmap. paintEvent = заливка + два быстрых
        # drawPixmap; туманности привязаны к углам и едут с ними на каждом
        # шаге resize.
        self._nebula_tr: QPixmap | None = None
        self._nebula_bl: QPixmap | None = None
        self._sprites_dpr = 0.0                   # DPR, под который собраны
        self._bg_color = QColor(theme.BG)         # базовая плоская заливка
        self._skip_next_freeze = False           # стартовый resize после show() не замораживаем
        self._live_freezer = LiveResizeFreezer(self)  # заморозка контента при живом resize
        # На быстрых вкладках (телеграмизированная «Маршрутизация») заморозка
        # не нужна: контент перерисовывается быстрее, чем тянется рамка,
        # и resize получается ЖИВОЙ — содержимое следует за краем (как в Тесте Б).
        self._freeze_content = True
        self._qwin_filter_installed = False      # фильтр на QWindow (ставится в showEvent)
        # Мы всегда закрашиваем весь rect в paintEvent → Qt может пропустить
        # фазу стирания фона (лишняя полная заливка на каждый repaint).
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        # Живой resize: кэша фона больше нет (спрайты размера окна не
        # требуют), но таймер settle всё ещё нужен — по нему снимается
        # заморозка контента (live_freezer) после последнего шага resize.
        self._resize_settle = QTimer(self)
        self._resize_settle.setSingleShot(True)
        self._resize_settle.setInterval(self.RESIZE_SETTLE_MS)
        self._resize_settle.timeout.connect(self._on_resize_settled)

    # ── отрисовка ────────────────────────────────────────────────────────

    def _rebuild_sprites(self):
        """Преeндерит фоновое свечение в маленький QPixmap-спрайт.

        Живой радиальный градиент — самая дорогая часть фона. Раньше
        туманности жили в кэше размера окна: при resize появлялись плоские
        полосы, а после «устаканивания» кэш пересобирался и пятно
        телепортировалось на новое место — то самое дёргание свечения у
        кнопки «Старт». Теперь оба пятна — готовые спрайты, привязанные
        к углам окна: paintEvent сводится к заливке и двум drawPixmap,
        поэтому перерисовка на каждом шаге resize ничего не стоит и
        ничего не прыгает. Цвета/радиусы — 1:1 со старым фоном
        (пиксель-в-пиксель на том же DPR).
        """
        dpr = self.devicePixelRatioF()

        def sprite(size, build):
            pm = QPixmap(int(size * dpr + 0.5), int(size * dpr + 0.5))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            build(p, size)
            p.end()
            return pm

        # Мягкий цветовой блик в правом верхнем углу (возле «Старт»).
        # Цвета берём из темы: для Neon это прежний розово-лавандовый фон,
        # а для «Ледяного стекла» — более холодное, приглушённое свечение.
        def nebula_tr(p, s):
            grad = QRadialGradient(s * 0.5, s * 0.5, 400)
            pink = QColor(theme.PINK)
            pink.setAlpha(12)
            accent = QColor(theme.ACCENT)
            accent.setAlpha(6)
            grad.setColorAt(0, pink)
            grad.setColorAt(0.5, accent)
            grad.setColorAt(1, QColor(0, 0, 0, 0))
            p.setBrush(grad)
            p.setPen(Qt.NoPen)
            p.drawEllipse(0, 0, s, s)

        # Бирюзовая туманность левого нижнего угла УБРАНА по просьбе пользователя:
        # круглое свечение в углу читалось как случайное пятно и было видно на
        # каждой вкладке (фон окна общий для всего приложения).
        self._nebula_tr = sprite(600, nebula_tr)
        self._nebula_bl = None
        self._sprites_dpr = dpr

    def paintEvent(self, event):
        # Заливка + один blit (розовая туманность в правом верхнем углу).
        # Спрайт не зависит от размера окна, поэтому resize больше не требует
        # пересборки чего-либо.
        dpr = self.devicePixelRatioF()
        if self._nebula_tr is None or self._sprites_dpr != dpr:
            self._rebuild_sprites()          # лениво; по-настоящему — только при смене DPR

        p = QPainter(self)
        w = self.width()
        p.fillRect(self.rect(), self._bg_color)
        p.drawPixmap(w - 400, -200, self._nebula_tr)   # якорь: правый верхний угол
        # Левого нижнего угла в фоне больше нет — см. _rebuild_sprites.
        p.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Живой resize. Фон — спрайты, привязанные к углам: они едут
        # вместе с краем окна на каждом шаге, без полос и без «телепорта»
        # после устаканивания.
        # Заморозку КОНТЕНТА делает фильтр на windowHandle (eventFilter ниже):
        # Resize QWindow прилетает от системы ДО того, как Qt пересчитает
        # геометрию виджета и активирует layout. Сюда — только запасной путь,
        # если фильтр по какой-то причине ещё не установлен.
        if self.isVisible() and self._freeze_content and not self._qwin_filter_installed:
            if not self._live_freezer.active:
                self._live_freezer.begin()
            else:
                self._live_freezer.step()
        self._resize_settle.start()

    def set_live_resize(self, live: bool):
        """Switch between snapshot-freeze and a genuinely live page resize."""
        self._freeze_content = not live
        # Routing is painted cheaply by canvases and must never inherit an
        # in-progress snapshot from a previously shown heavyweight page.  The
        # overlay intentionally stretches its screenshot while dragging, which
        # otherwise looks like the whole Services page is a scaled photograph.
        if live and self._live_freezer.active:
            self._live_freezer.end()

    def showEvent(self, event):
        # Первый показ: вешаем фильтр на QWindow окна. Его событие Resize —
        # самая ранняя точка, где мы знаем об изменении размера (раньше, чем
        # layout успеет переехать), поэтому заморозка контента успевает ДО
        # пересчёта геометрии детей.
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._qwin_filter_installed:
            handle.installEventFilter(self)
            self._qwin_filter_installed = True

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize and obj is self.windowHandle():
            if self._skip_next_freeze:
                self._skip_next_freeze = False   # стартовый resize из showEvent
            elif self.isVisible() and self._freeze_content:
                # Заморозка контента ДО активации layout: снимок окна +
                # оверлей; дети не пересчитываются и не красятся до конца
                # изменения размера (иначе QSS-виджеты красятся на каждый
                # шаг — на ноутбуке это слайд-шоу). Для быстрых вкладок
                # (Маршрутизация после телеграмизации) заморозка выключена:
                # там дешевле перерисовать живьём, и контент идёт за краем.
                if not self._live_freezer.active:
                    self._live_freezer.begin()
                else:
                    self._live_freezer.step()
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        # Окно спрятали/закрыли во время resize — обязательно разморозить,
        # иначе дети останутся с выключенными обновлениями.
        self._live_freezer.end()
        super().hideEvent(event)

    def _on_resize_settled(self):
        """Размер окна устоялся (последнее resize-событие было RESIZE_SETTLE_MS
        назад): снимаем заморозку контента — один relayout под итоговый размер.
        Кэша фона больше нет: спрайтам размер окна не важен, repaint не нужен."""
        self._live_freezer.end()


class _TopBarContainer(QWidget):
    """Контейнер верхней панели, который сообщает о смене своей ширины.

    Зачем. Ширина панели меняется не только от размера окна, но и от разворота
    бокового меню: оно отнимает ~140 px. Ужим подписей считается по фактической
    ширине панели, поэтому пересчёт нужен именно в момент, когда ширина уже
    изменилась, — а этот момент знает сам виджет (resizeEvent), а не окно.
    """

    resized = Signal()

    def resizeEvent(self, event):                       # Qt API: переопределение
        super().resizeEvent(event)
        self.resized.emit()


class MainWindow(GlowContainer):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(theme.APP_NAME)
        # ФИХ #1: устанавливаем размер до show(), без resize() в конструкторе —
        # Qt применит его правильно после полного построения layout.
        #
        # Минимум окна маленький (см. theme.WIN_MIN_*). Почему: при большом
        # минимуме раскладки Windows («половина экрана», «четверть») не могут
        # ужать окно — Qt держит размер и окно «съедает» весь экран. Тяжёлые
        # вкладки, которым тесно в узком окне, получают прокрутку (см. _add_page).
        self.setMinimumSize(theme.WIN_MIN_W, theme.WIN_MIN_H)
        # Геометрия сохраняется не на каждый пиксель перетаскивания, а после паузы.
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(1200)
        self._geometry_timer.timeout.connect(self._save_window_geometry)

        self.engine = get_engine()
        # ФИХ #3: флаг занятости — защита от множественных кликов
        self._busy = False
        self._worker: _EngineWorker | None = None
        self._startup_health_worker: _StartupHealthWorker | None = None
        self._ai_generation_worker: _AiGenerationWorker | None = None
        self._strategy_check_worker: _StrategyCheckWorker | None = None
        self._last_startup_health = {"severity": "ok", "can_start": True, "summary": "Готов к запуску", "problems": [], "warnings": []}
        # `None` forces the first render; later equal readiness values must not
        # reapply button QSS or measure the header on every queue tick.
        self._last_can_start: bool | None = None
        self._watchdog_proc = None
        self._shutdown_started = False
        self._shutdown_worker_timeout = False
        self._exit_fallback_scheduled = False
        # AI-генерация запускается как controlled session: сначала подтверждение,
        # затем Stop, и только после успешной остановки — подготовка плана.
        self._ai_generation_pending = False
        self._strategy_check_pending = False

        # One scheduler owns startup and periodic list refreshes. No GUI-only
        # domain thread, no duplicate startup subscription download.
        self._updates_start_timer = QTimer(self)
        self._updates_start_timer.setSingleShot(True)
        self._updates_start_timer.timeout.connect(self._start_background_updates)
        self._updates_start_timer.start(3000)
        self._notified_release = ""
        self._release_timer = QTimer(self)
        self._release_timer.setInterval(60_000)
        self._release_timer.timeout.connect(self._check_program_updates)
        self._release_timer.start()
        self._updates_start_timer.timeout.connect(self._check_program_updates)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Боковое меню всегда открывается СВЁРНУТЫМ (значки): так вкладкам больше
        # места, а развернуть панель человек может сам кнопкой внизу. Решение
        # пользователя от 15.09.2026: автоматического сворачивания по ширине окна
        # нет вообще — панель не меняется сама, пока её не попросят.
        self.sidebar = Sidebar(_ordered_nav_items(), active_key="routing", expanded=False)
        self.sidebar.navigate.connect(self._on_navigate)
        # Разворот боковой панели отнимает у шапки ~140 px ширины. Без этого
        # пересчёта кнопки оставались в прежнем (широком) виде и «Старт» уезжал за
        # край окна — жалоба пользователя. Панель анимируется, поэтому ширину
        # сообщаем на каждом кадре анимации (layoutWidthChanged).
        #
        # Второй, независимый путь того же пересчёта — сигнал контейнера шапки
        # (_TopBarContainer.resized): контейнер тоже становится у́же. Оба пути
        # оставлены намеренно: они страхуют друг друга, если Qt не пришлёт одно из
        # событий (проверено: каждый по отдельности возвращает кнопку в окно).
        self.sidebar.layoutWidthChanged.connect(self._on_sidebar_width_changed)
        self.sidebar.orderChanged.connect(
            lambda order: set_nav_order(order, [it.key for it in NAV_ITEMS])
        )
        self.sidebar.extraNavRequested.connect(self._show_extra)
        root.addWidget(self.sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        topbar = self._build_topbar()
        right.addWidget(topbar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:transparent;")
        self._views: dict[str, QWidget] = {}
        self._pages: dict[str, int] = {}
        # The shell owns active/inactive transitions.  Pages with a cheap
        # periodic visual status use this to stop their timers while hidden.
        self._active_page_key: str | None = None
        # A view is initially fresh after its own constructor has populated it.
        # Later, engine events invalidate views that are not visible.  This
        # avoids destroying/recreating whole page trees merely because the user
        # returned to an unchanged tab.
        self._stale_view_keys: set[str] = set()
        self._ensure_page("routing")
        # StrategyLab is the one complex scrollable page whose native Qt
        # construction visibly flashed on Windows when deferred until a click.
        # Create its empty shell before MainWindow is ever shown; JSON loading
        # and row construction remain deferred until first activation.
        self._ensure_page("strategy_lab")
        right.addWidget(self.stack, 1)

        right_wrap = QWidget()
        right_wrap.setStyleSheet("background:transparent;")
        right_wrap.setLayout(right)
        root.addWidget(right_wrap, 1)

        self._show("routing")

        self._really_quit = False
        self.tray = None
        if Tray.is_available():
            self.tray = Tray(self, {
                "start":   self._on_start,
                "stop":    self._on_stop,
                "restart": self._on_restart,
                "show":    self._restore_window,
                "quit":    self._quit_app,
            })
            self.tray.set_running(routing_running())
            self.tray.show()

        self._show_timer = QTimer(self)
        self._show_timer.setInterval(800)
        self._show_timer.timeout.connect(self._poll_show_request)
        self._show_timer.start()

        self._event_timer = QTimer(self)
        self._event_timer.setInterval(200)
        self._event_timer.timeout.connect(self._process_engine_events)
        self._event_timer.start()

        self._health_timer = QTimer(self)
        # Предстартовая диагностика может вызывать PowerShell/проверку порта,
        # поэтому не дёргаем её каждые 5 секунд — это давало микрофризы UI.
        self._health_timer.setInterval(30000)
        self._health_timer.timeout.connect(self._update_startup_health)
        self._health_timer.start()
        QTimer.singleShot(1200, self._update_startup_health)

    def showEvent(self, event):
        """ФИХ #1: задаём итоговый размер окна после первого показа.

        Qt к этому моменту уже посчитал sizeHint всех виджетов,
        поэтому resize() здесь работает корректно и не обрезает содержимое.

        Приоритет: сохранённый пользователем размер → затем размер по умолчанию,
        вписанный в текущий экран.
        """
        super().showEvent(event)
        if not hasattr(self, "_initial_resize_done"):
            self._initial_resize_done = True
            self._skip_next_freeze = True   # стартовый resize — без заморозки
            if not self._restore_window_geometry():
                width, height = self._default_window_size()
                self.resize(width, height)
                # Настройки ещё нет (первый запуск) — сохраняем сразу, чтобы
                # следующий запуск уже знал размер окна.
                self._geometry_timer.start()

    # ── глобальная верхняя панель ──
    def _build_topbar(self) -> QWidget:
        bar = _TopBarContainer()
        bar.setStyleSheet(f"background:{theme.SIDEBAR};")
        # Ширина панели изменилась (окно или боковое меню) — пересчитываем ужим.
        bar.resized.connect(self._on_topbar_resized)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(10)

        # Верхняя панель — ВСЕГДА одна строка. Раскладка не перестраивается в
        # «столбик» и ничего не переезжает вверх/вниз при изменении размера окна:
        # при нехватке места ужимаются подписи (см. _apply_window_layout_mode).
        row = QHBoxLayout()
        row.setSpacing(12)

        init_mode = ""
        if routing_mode_is_explicit():
            init_mode = {"vpn": "blue", "combo": "black", "dpi": "red"}.get(
                get_current_mode(), ""
            )
        self.mode_switch = ModeSwitch(active=init_mode)
        self.mode_switch.modeChanged.connect(self._on_mode_change)
        row.addWidget(self.mode_switch)
        row.addStretch()

        self.control = ControlBar(running=routing_running(), mode=get_current_mode())
        self.control.startClicked.connect(self._on_start)
        self.control.stopClicked.connect(self._on_stop)
        self.control.restartClicked.connect(self._on_restart)
        # Header text/button mutations are event-driven; never wait for the
        # 200-ms engine queue drain merely to reconcile its compact layout.
        self.control.visualStateChanged.connect(self._apply_topbar_compact)
        row.addWidget(self.control)
        # Инициализируем серую кнопку Старт если целей нет
        try:
            self._update_start_button()
        except Exception:
            pass
        self._topbar_row = row
        self._topbar_bar = bar        # нужен, чтобы знать доступную ширину строки
        self._topbar_margins = (24, 24)
        lay.addLayout(row)

        self.health_banner = QLabel()
        self.health_banner.setWordWrap(True)
        self.health_banner.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.health_banner.setVisible(False)
        lay.addWidget(self.health_banner)

        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{theme.BORDER};")
        lay.addWidget(line)
        return bar

    def _format_health_details(self, health: dict) -> str:
        items = list(health.get("problems") or []) + list(health.get("warnings") or [])
        if not items:
            return health.get("summary") or "Готов к запуску"
        # В верхней панели показываем компактно, но достаточно понятно.
        return " • ".join(str(x) for x in items[:3])

    def _update_startup_health(self) -> dict:
        """Запускает предстартовую диагностику в фоне и возвращает последний результат.

        2026-10-06: без Старта — полная тишина. Раньше таймер опрашивал Health
        каждые 30 секунд даже в простое («доктор видит что-то, даже когда я не
        нажал старт»). Теперь проверки идут только после Старта и по кнопкам.
        """
        if not routing_running():
            return self._last_startup_health
        if self._startup_health_worker and self._startup_health_worker.isRunning():
            return self._last_startup_health
        self._startup_health_worker = _StartupHealthWorker()
        self._startup_health_worker.done.connect(self._apply_startup_health)
        self._startup_health_worker.start()
        return self._last_startup_health

    def _apply_startup_health(self, health: dict):
        self._last_startup_health = health or self._last_startup_health
        severity = self._last_startup_health.get("severity", "ok")
        if severity == "ok":
            self.health_banner.setVisible(False)
            return

        if severity == "error":
            icon, color = "⛔", theme.RED
            title = "UmbraNet не готов к запуску"
        else:
            icon, color = "⚠", theme.YELLOW
            title = "Есть предупреждения"
        color_rgb = QColor(color)
        bg = f"rgba({color_rgb.red()}, {color_rgb.green()}, {color_rgb.blue()}, 0.12)"

        self.health_banner.setText(
            f"{icon} <b>{title}</b>: {self._format_health_details(self._last_startup_health)}"
        )
        self.health_banner.setStyleSheet(
            f"QLabel{{"
            f"background:{bg}; color:{theme.TEXT};"
            f"border:1px solid {color}; border-radius:10px;"
            f"padding:8px 10px; font-size:12px;"
            f"}}"
        )
        self.health_banner.setVisible(True)


    def _update_mode_hint(self):
        """Подсказки режимов отключены по просьбе пользователя."""
        return


    # ── режим DPI ──
    def _on_mode_change(self, ui_key: str):
        UI_KEY_TO_MODE = {
            "blue": "vpn",
            "black": "combo",
            "red": "dpi",
        }
        ui_mode = UI_KEY_TO_MODE.get(ui_key, "vpn")

        # C1 guard убран с переключения: теперь режимы переключаются всегда,
        # даже если список целей пуст. Блокируется только кнопка Старт
        # (серая + диалог), чтобы пользователь мог листать режимы, но не
        # запустить пустую конфигурацию.

        # Если пытаемся сменить режим пока программа работает - просто останавливаем ее
        if routing_running():
            self._on_stop()

        ok, err = switch_mode(ui_mode)
        if not ok:
            log.warning("Не удалось переключить режим '%s': %s", ui_mode, err)
            actual_key = {"vpn": "blue", "combo": "black", "dpi": "red"}.get(
                get_current_mode(), "blue"
            )
            self.mode_switch.set_active(actual_key)
            # Если причина — пустой hostlist, показываем дружелюбный диалог
            if err and ("цели" in err.lower() or "hostlist" in err.lower() or "маршрутизация" in err):
                self._show_dpi_guard_dialog(err)
        self._update_mode_hint()
        QTimer.singleShot(400, self._update_startup_health)

        # Обновляем UI-компоненты, чтобы они отразили новый глобальный режим
        # VPN, Combo или DPI и соответствующие доступные стратегии.
        self._refresh_views()

    def _show_dpi_guard_dialog(self, custom_text: str | None = None):
        """Диалог C1: объясняет, почему DPI без целей не включается."""
        text = custom_text or (
            "Для режимов <b>Combo / DPI</b> нужны цели.<br><br>"
            "Включите хотя бы один сервис в <b>Маршрутизация</b> "
            "(например, YouTube, Discord, ChatGPT) или добавьте домен вручную "
            "в блоке «Диспетчер задач».<br><br>"
            "Без целей WinWS не должен касаться всего трафика — это защита "
            "от «стрельбы себе в ногу»."
        )
        box = QMessageBox(self)
        box.setWindowTitle("Нужны цели DPI")
        box.setIcon(QMessageBox.Warning)
        box.setTextFormat(Qt.RichText)
        box.setText(text)
        go_btn = box.addButton("Перейти в Маршрутизация", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() == go_btn:
            try:
                self._show("routing")
                self.sidebar.set_active("routing")
            except Exception:
                pass
        # Подсветка Health-баннера
        try:
            self.health_banner.setText(
                "⚠ <b>DPI-цели не выбраны</b>: включите сервисы/домены в «Маршрутизация», затем снова выберите Combo/DPI."
            )
            self.health_banner.setStyleSheet(
                f"QLabel{{background:rgba(251, 191, 36, 0.14); color:{theme.TEXT};"
                f"border:1px solid {theme.YELLOW}; border-radius:10px;"
                f"padding:8px 10px; font-size:12px;}}"
            )
            self.health_banner.setVisible(True)
        except Exception:
            pass

    # ── Новый C1: блок Старта без целей (вместо блока переключения) ──
    def _can_start(self) -> bool:
        """Можно ли нажать Старт? Требует хотя бы один ДОМЕН/сервис в главном меню.

        ВАЖНО: процессы chrome.exe / firefox.exe / msedge.exe — дефолтные,
        их НЕ считаем. Они нужны для per-app маршрутизации, но не должны
        давать зелёный Старт без выбранного сервиса/домена. Иначе кнопка
        никогда не станет серой, т.к. процессы есть всегда.
        """
        try:
            ready, _message = routing_start_readiness()
            return ready
        except Exception:
            return False

    def _show_start_guard_dialog(self):
        """Диалог при попытке старта без выбранных доменов/сервисов."""
        _ready, reason = routing_start_readiness()
        text = (
            "<b>Маршрут ещё не готов к запуску.</b><br><br>"
            f"{reason or 'Выберите сервис и VPN-сервер, или добавьте домен вручную в «Диспетчер задач».'}<br><br>"
            "В VPN и Combo AI-сервисы используют только проверенный VPN-сервер; "
            "при отсутствии готового сервера прямого подключения не будет."
        )
        box = QMessageBox(self)
        box.setWindowTitle("Настройте маршрут")
        box.setIcon(QMessageBox.Warning)
        box.setTextFormat(Qt.RichText)
        box.setText(text)
        go_btn = box.addButton("Перейти в Маршрутизация", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() == go_btn:
            try:
                self._show("routing")
                self.sidebar.set_active("routing")
            except Exception:
                pass
        # Подсветка баннера
        try:
            self.health_banner.setText(
                "⚠ <b>Не выбран ни один сервис</b>: включите хотя бы один сервис в «Маршрутизация», затем нажмите Старт."
            )
            self.health_banner.setStyleSheet(
                f"QLabel{{background:rgba(251, 191, 36, 0.14); color:{theme.TEXT};"
                f"border:1px solid {theme.YELLOW}; border-radius:10px;"
                f"padding:8px 10px; font-size:12px;}}"
            )
            self.health_banner.setVisible(True)
        except Exception:
            pass

    def _update_start_button(self) -> bool:
        """Synchronize Start availability only when readiness actually changed.

        Header geometry is now driven by ``ControlBar.visualStateChanged``.
        This helper deliberately contains no layout work, so an empty engine
        queue cannot repeatedly measure or restyle the top bar.
        """
        try:
            can = self._can_start()
        except Exception:
            return False
        if can == self._last_can_start:
            return False
        self._last_can_start = can
        control = getattr(self, "control", None)
        if control is not None:
            control.set_can_start(can)
        return True

    # ── ФИХ #3 + #6: старт/стоп/рестарт через фоновый поток ──
    def _start_action(self, action: str):
        """Запускает action в фоновом потоке с блокировкой повторных кликов."""
        if self._shutdown_started or self._busy:
            return
        # Каждый Старт/Стоп — новое «поколение» сессии. Фоновые проверки
        # сверяются с ним и не вмешиваются в следующую сессию после быстрого
        # Стоп→Старт.
        self._runtime_gen = getattr(self, "_runtime_gen", 0) + 1
        # Блок Старта без целей — вместо блока переключения режимов
        if action in ("start", "restart") and not self._can_start():
            self._show_start_guard_dialog()
            self._update_start_button()
            return
        # Жизненный цикл запускается только через RoutingRuntime. Он сам
        # проверяет VPN-выходы и DPI-стратегии в рабочем потоке; локальный DNS
        # при этом не стартует и не перезапускается.
        self._busy = True
        self.control.set_busy(action)          # показываем промежуточный статус
        if self.tray:
            self.tray.set_waiting(
                {"start": "Запуск...", "stop": "Остановка...", "restart": "Перезапуск..."}
                .get(action, "...")
            )
        self._worker = _EngineWorker(action)
        self._worker.finished.connect(self._on_action_done)
        self._worker.start()

    def _on_start(self):
        if not self._can_start():
            self._show_start_guard_dialog()
            self._update_start_button()
            return
        self._start_action("start")

    def _on_stop(self):
        self._start_action("stop")

    def _on_restart(self):
        self._start_action("restart")


    def _on_strategy_check_requested(self):
        """Controlled check-all: при необходимости сначала останавливаем UmbraNet."""
        view = self._views.get("strategy_lab")
        if self._busy:
            if hasattr(view, "_generation_busy"):
                view._generation_busy()
            return
        was_running = routing_running()
        self._strategy_check_pending = True
        if hasattr(view, "_check_stop_started"):
            view._check_stop_started(was_running)
        if was_running:
            self._on_stop()
        else:
            self._begin_strategy_check_session()

    def _begin_strategy_check_session(self):
        view = self._views.get("strategy_lab")
        try:
            if self._strategy_check_worker is not None:
                if self._strategy_check_worker.isRunning():
                    if hasattr(view, "_generation_busy"):
                        view._generation_busy()
                    return
                # Worker завершился, но ссылка осталась — чистим.
                self._strategy_check_worker = None
            self._busy = True
            if hasattr(self.control, "set_ai_busy"):
                self.control.set_ai_busy()
            if hasattr(view, "_check_plan_ready"):
                view._check_plan_ready()
            self._strategy_check_worker = _StrategyCheckWorker()
            self._strategy_check_worker.progress.connect(self._on_strategy_check_progress)
            self._strategy_check_worker.done.connect(self._on_strategy_check_done)
            self._strategy_check_worker.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Strategy check start failed: %s", exc)
            self._busy = False
            self._strategy_check_pending = False
            self.control.set_running(routing_running(), mode=get_current_mode())
            if hasattr(view, "_generation_plan_error"):
                view._generation_plan_error(str(exc))

    def _on_strategy_check_progress(self, text: str):
        view = self._views.get("strategy_lab")
        if hasattr(view, "_check_progress"):
            view._check_progress(text)

    def _on_strategy_check_cancel_requested(self):
        try:
            w = self._strategy_check_worker
            if w is not None and w.isRunning():
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
                else:
                    w.requestInterruption()
                dpi_strategy_ai_cleanup_runtime()
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("Strategy check cancel failed: %s", exc)
        self._strategy_check_worker = None
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def _on_strategy_check_done(self, result: dict):
        view = self._views.get("strategy_lab")
        try:
            cleanup = dpi_strategy_ai_cleanup_runtime()
            lines = list(result.get("report_lines") or [])
            stopped = ", ".join(cleanup.get("stopped") or []) or "ничего не осталось"
            errors = "; ".join(cleanup.get("errors") or [])
            lines.append(f"DPI cleanup: {stopped}" + (f" • ошибки: {errors}" if errors else " • OK"))
            result["report_lines"] = lines
            result["cleanup"] = cleanup
        except Exception as exc:
            result["cleanup"] = {"stopped": [], "errors": [str(exc)]}
        # Сбрасываем ссылку на worker (см. комментарий в _on_ai_generation_done).
        self._strategy_check_worker = None
        self._busy = False
        self._strategy_check_pending = False
        self.control.set_running(False, mode=get_current_mode())
        if self.tray:
            self.tray.set_running(routing_running())
        if hasattr(view, "_check_finished"):
            view._check_finished(result)
        self._refresh_current_view()

    def _on_ai_generation_requested(self):
        """Первый шаг controlled AI-generation: подтверждение из вкладки → Stop.

        Реальный исполнитель генерации будет подключён следующим этапом; сейчас
        важно безопасно пройти UX и остановить текущие процессы тем же путём,
        что и кнопка «Стоп».
        """
        view = self._views.get("strategy_lab")
        if self._busy:
            if hasattr(view, "_generation_busy"):
                view._generation_busy()
            return
        was_running = routing_running()
        self._ai_generation_pending = True
        if hasattr(view, "_generation_stop_started"):
            view._generation_stop_started(was_running)
        if was_running:
            self._on_stop()
        else:
            self._begin_ai_generation_session()

    def _begin_ai_generation_session(self):
        """Готовит план и запускает controlled AI-generation worker."""
        view = self._views.get("strategy_lab")
        try:
            plan = dpi_strategy_ai_plan("quick")
            if hasattr(view, "_generation_plan_ready"):
                view._generation_plan_ready(plan)
            if self._ai_generation_worker is not None:
                if self._ai_generation_worker.isRunning():
                    if hasattr(view, "_generation_busy"):
                        view._generation_busy()
                    return
                # Worker завершился, но ссылка осталась — чистим.
                self._ai_generation_worker = None
            self._busy = True
            if hasattr(self.control, "set_ai_busy"):
                self.control.set_ai_busy()
            self._ai_generation_worker = _AiGenerationWorker("quick")
            self._ai_generation_worker.progress.connect(self._on_ai_generation_progress)
            self._ai_generation_worker.done.connect(self._on_ai_generation_done)
            self._ai_generation_worker.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("AI generation start failed: %s", exc)
            self._busy = False
            self.control.set_running(routing_running(), mode=get_current_mode())
            if hasattr(view, "_generation_plan_error"):
                view._generation_plan_error(str(exc))
            self._ai_generation_pending = False

    def _on_ai_generation_progress(self, text: str):
        view = self._views.get("strategy_lab")
        if hasattr(view, "_generation_progress"):
            view._generation_progress(text)

    def _on_ai_generation_cancel_requested(self):
        """Отмена AI-генерации из UI: не закрываем программу, только DPI-session."""
        try:
            w = self._ai_generation_worker
            if w is not None and w.isRunning():
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
                else:
                    w.requestInterruption()
                dpi_strategy_ai_cleanup_runtime()
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("AI generation cancel failed: %s", exc)
        # Если worker уже завершился между кликом и обработкой — всё равно чистим DPI.
        self._ai_generation_worker = None
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def _on_ai_generation_done(self, result: dict):
        view = self._views.get("strategy_lab")
        # Safety net: controlled generation must never leave WinWS running
        # after completion. User will start UmbraNet manually when ready.
        cleanup = {"stopped": [], "errors": []}
        try:
            cleanup = dpi_strategy_ai_cleanup_runtime()
            result["cleanup"] = cleanup
            lines = list(result.get("report_lines") or [])
            stopped = ", ".join(cleanup.get("stopped") or []) or "ничего не осталось"
            errors = "; ".join(cleanup.get("errors") or [])
            lines.append(f"DPI cleanup: {stopped}" + (f" • ошибки: {errors}" if errors else " • OK"))
            result["report_lines"] = lines
            if isinstance(result.get("report"), dict):
                result["report"]["lines"] = lines
                result["report"]["cleanup"] = cleanup
            if cleanup.get("stopped"):
                log.info("AI generation cleanup stopped: %s", cleanup.get("stopped"))
        except Exception as exc:  # noqa: BLE001
            result["cleanup"] = {"stopped": [], "errors": [str(exc)]}
            log.warning("AI generation cleanup failed: %s", exc)
        # Сбрасываем ссылку на worker: без этого isRunning() может кратковременно
        # вернуть True (QThread ещё не обработал finished-сигнал), и повторный
        # запуск генерации попадёт в guard «worker уже запущен» → _busy навсегда
        # останется True → «вечная уборка».
        self._ai_generation_worker = None
        self._busy = False
        self._ai_generation_pending = False
        self._strategy_check_pending = False
        self.control.set_running(False, mode=get_current_mode())
        if self.tray:
            self.tray.set_running(routing_running())
        if hasattr(view, "_generation_finished"):
            view._generation_finished(result)
        self._refresh_current_view()

    def _show_manual_server_reserve_choice(self, proposal: dict[str, str]) -> None:
        """Ask about a verified reserve after a failed manual readiness check."""
        failed = proposal.get("failed_profile_name") or "выбранный VPN-сервер"
        suggested = proposal.get("suggested_profile_name") or "проверенный резерв"
        suggested_id = proposal.get("suggested_profile_id") or ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("VPN-сервер недоступен")
        box.setText(f"Сервер «{failed}» не прошёл проверку перед подключением.")
        box.setInformativeText(
            f"Доступен проверенный резерв «{suggested}». Подключиться к нему? "
            "Без вашего выбора UmbraNet не заменит выбранный сервер."
        )
        reserve_button = box.addButton("Подключиться к резерву", QMessageBox.AcceptRole)
        box.addButton("Не подключаться", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is reserve_button and suggested_id:
            saved, message = select_vpn_server_for_next_start(suggested_id)
            if saved:
                self.control.set_busy("start")
                QTimer.singleShot(0, lambda: self._start_action("start"))
                return
            self.control.set_error(message)
            return
        self.control.set_error("Подключение отменено: выбранный VPN-сервер недоступен")

    def _on_action_done(self, action: str, ok: bool):
        """Вызывается из фонового потока по завершении start/stop/restart."""
        self._busy = False
        # During real application exit the worker may finish after the UI has
        # already started teardown.  It must not arm a watchdog, schedule an AI
        # session, or refresh disappearing widgets at that point.
        if self._shutdown_started:
            return
        running = routing_running()
        self.control.set_running(running, mode=get_current_mode())
        if running:
            # A committed PAC with no verified HTTP/SOCKS exit is intentionally
            # a blocking state. Surface it as yellow so "VPN запущен" never
            # suggests that selected services have a usable unblocked route.
            self.control.set_vpn_blocked(routing_vpn_block_message())
        if self.tray:
            self.tray.set_running(running)
        self._update_start_button()

        if ok and action in ("start", "restart") and running:
            # PAC has already been committed by TransportRuntime. Arm a
            # proxy-only watchdog now: unlike the historical DNS watchdog it
            # never rewrites system DNS after an application crash.
            self._ensure_proxy_watchdog()
        elif action == "stop":
            if ok:
                self._shutdown_watchdog("CLEAN")
            else:
                # A failed in-process restore leaves its atomic snapshot for
                # the watchdog. It is safer to ask for one more restoration
                # attempt than to acknowledge a potentially stale PAC.
                self._handoff_proxy_restore_to_watchdog()
        elif action == "restart" and not ok and bool(
            getattr(self._worker, "restore_failed", False)
        ):
            # The old gateway was closed during Restart, while a failed restore
            # retained the durable PAC snapshot. Since Start did not commit a
            # replacement PAC, delegate that stale-PAC recovery to the
            # independent watchdog exactly as a failed Stop would.
            self._handoff_proxy_restore_to_watchdog()

        if action == "stop" and self._ai_generation_pending:
            if ok:
                # Даём UI короткий тик после остановки, затем готовим план.
                QTimer.singleShot(350, self._begin_ai_generation_session)
            else:
                view = self._views.get("strategy_lab")
                if hasattr(view, "_generation_plan_error"):
                    view._generation_plan_error("Не удалось остановить UmbraNet перед генерацией")
                self._ai_generation_pending = False

        if action == "stop" and self._strategy_check_pending:
            if ok:
                QTimer.singleShot(350, self._begin_strategy_check_session)
            else:
                view = self._views.get("strategy_lab")
                if hasattr(view, "_generation_plan_error"):
                    view._generation_plan_error("Не удалось остановить UmbraNet перед проверкой стратегий")
                self._strategy_check_pending = False

        reserve_choice = take_vpn_reserve_choice() if not ok and action in ("start", "restart") else None
        if reserve_choice:
            # The failed route state was captured by VPN core before its
            # preflight controller stopped, so both server surfaces can render
            # it red while this modal decision is visible.
            self._show_manual_server_reserve_choice(reserve_choice)
        elif not ok and action in ("start", "restart"):
            worker_message = str(getattr(self._worker, "message", "") or "")
            self.control.set_error(worker_message or "Не удалось запустить")
        QTimer.singleShot(300, self._update_startup_health)
        self._invalidate_views()
        self._refresh_current_view()

    def _refresh_views(self):
        """Refresh the active page and defer every hidden page until it is opened."""
        self._invalidate_views()
        self._refresh_current_view()

    def _apply_window_layout_mode(self) -> bool:
        """Подстраивает под ширину окна только подписи верхней панели.

        Боковое меню здесь НЕ трогаем: оно всегда открывается свёрнутым и дальше
        меняется только по кнопке самого пользователя. Автоматического
        сворачивания/разворачивания нет — панель не должна «жить своей жизнью»
        при изменении размера окна.

        Правило для шапки простое и жёсткое: **ничего не переезжает вверх или
        вниз**. Верхняя панель всегда одна строка, у переключателя режимов и у
        кнопок неизменная вертикаль — меняется только ширина, и то по шагам, от
        менее важного к более важному:

          1. прячется ТЕКСТ статуса в шапке (точка-индикатор остаётся, а сам текст
             переезжает в подсказку точки) — это дублирующая надпись, она же есть
             на вкладке «Маршрутизация»;
          2. кнопки режимов VPN / Combo / DPI сбрасывают подписи и остаются только
             значками — примерно половина прежней ширины. Так они перестают
             упираться в «Перезапуск» и «Старт»;
          3. в узком окне «Перезапуск» остаётся со значком ↻;
          4. следом ужимается и «Старт/Стоп»: подпись укорачивается постепенно и в
             пределе остаётся значок ▶ / ⏹. Раньше подпись сохранялась любой ценой, и
             в самом узком окне кнопку «съедал» край окна — то есть главное действие
             становилось недоступным. Значок с подсказкой лучше, чем обрезанная
             кнопка: нажатие и смысл сохраняются (просьба пользователя).

        Возвращает True, если что-то изменилось.
        """
        return self._apply_topbar_compact()

    def _apply_topbar_compact(self) -> bool:
        """Ужимает подписи шапки под доступную ширину (без переносов и сдвигов)."""
        bar = getattr(self, "_topbar_bar", None)
        mode_switch = getattr(self, "mode_switch", None)
        control = getattr(self, "control", None)
        if bar is None or mode_switch is None or control is None:
            return False
        left, right = getattr(self, "_topbar_margins", (24, 24))
        # Зазор между переключателем режимов и блоком кнопок (row.setSpacing) тоже
        # съедает ширину строки — без него рассчитанная «доступная» ширина больше
        # фактической, и раскладка успевает сжать кнопки раньше ужима.
        row_gap = 12
        try:
            spacing = int(self._topbar_row.spacing())
            if spacing >= 0:
                row_gap = spacing
        except (AttributeError, TypeError, ValueError) as exc:
            log.debug("Не удалось узнать зазор шапки: %s", exc)
        available = max(0, bar.width() - left - right - row_gap)
        if available <= 0:
            # Окно ещё не разложено — оставляем полный вариант, пересчитаем на resize.
            return False
        try:
            mode_w = mode_switch.required_widths()
            ctrl_w = control.required_widths()
        except Exception as exc:                        # noqa: BLE001
            log.debug("Замер ширины шапки не удался: %s", exc)
            return False

        # Небольшой запас поверх точных замеров: живой рендер QSS и подсказки
        # курсора могут добавить несколько пикселей к посчитанной ширине. Без
        # запаса раскладка успевала бы сжать подписи на пиксель раньше, чем
        # включается плавный ужим, — и это выглядело бы как рывок.
        margin = 24
        # ── Лестница уступок ────────────────────────────────────────────────
        # Одна и та же последовательность и для расчёта, и для любого добора
        # ужима, поэтому состояние меняется МОНОТОННО: сузили окно — ужали
        # ровно настолько, расширили — вернули подписи в обратном порядке.
        # Раньше пороги считались по метрикам шрифта (они занижали ширину на
        # десятки пикселей) и порядок уступок на разных ширинах расходился: при
        # более узком окне кнопки могли оказаться ШИРЕ, чем при более широком.
        #
        # Ступени, в порядке уступки:
        #   1) прячется текст статуса (остаётся точка);
        #   2) ужимаются подписи режимов VPN / Combo / DPI (плавно, до значков);
        #   3) «Перезапуск» теряет подпись, остаётся ↻;
        #   4) «Старт/Стоп» ужимается плавно, в пределе — только значок ▶ / ⏹.
        need_full = mode_w["full"] + ctrl_w["full"] + margin
        deficit = need_full - available
        # Остаток меньше половины пикселя — это уже «поместилось»: без допуска
        # дробный хвост (0.1 px) отправлял шапку на следующую ступень ужима, и
        # кнопка «Перезапуск» теряла подпись там, где места ей хватало.
        eps = 0.5

        ctrl_level = "full"
        t = 0.0
        power_t = 0.0
        if deficit > eps:
            ctrl_level = "no_status"
            deficit -= max(0, ctrl_w["full"] - ctrl_w["no_status"])
        if deficit > eps:
            span = max(0, mode_w["full"] - mode_w["icons"])
            t = min(1.0, deficit / span) if span else 1.0
            deficit -= span * t
        if deficit > eps:
            ctrl_level = "restart_icon"
            deficit -= max(0, ctrl_w["no_status"] - ctrl_w["restart_icon"])
        if deficit > eps:
            span = max(0, ctrl_w["restart_icon"] - ctrl_w["power_icon"])
            power_t = min(1.0, deficit / span) if span else 1.0
            deficit -= span * power_t

        # Правая часть вкладки «Маршрутизация» прячется ЗАРАНЕЕ — за 50 px до того,
        # как кнопки режимов начнут ужиматься. Так момент исчезновения панели не
        # совпадает с моментом, когда меняются кнопки: два изменения на одном шаге
        # читались бы как рывок. Заодно списки получают всю ширину раньше.
        # Возврат — с запасом 24 px (гистерезис), чтобы на самой границе панель не
        # мигала при дрожании окна мышью.
        modes_start = mode_w["full"] + ctrl_w["no_status"] + margin
        hide_below = modes_start + self.SIDE_PANEL_LEAD_PX
        show_above = hide_below + self.SIDE_PANEL_HYSTERESIS_PX
        hidden_before = getattr(self, "_side_panel_hidden", False)
        hidden_now = available < (show_above if hidden_before else hide_below)
        panel_changed = False
        if hidden_now != hidden_before:
            self._side_panel_hidden = hidden_now
            panel_changed = True
        self._apply_side_panel_mode(hidden_now)

        changed = panel_changed
        try:
            if mode_switch.set_compression(t):
                changed = True
        except Exception as exc:
            log.debug("Плавное ужимание переключателя режимов не удалось: %s", exc)
        try:
            if control.set_compact(ctrl_level):
                changed = True
        except Exception as exc:
            log.debug("Ужимание строки статуса не удалось: %s", exc)
        try:
            if control.set_compression(power_t):
                changed = True
        except Exception as exc:
            log.debug("Ужимание кнопки Старт не удалось: %s", exc)

        if changed:
            log.debug("Шапка подстроена: ужим режимов=%.2f, кнопка Старт=%.2f, статус=%s (доступно %s px)",
                      t, power_t, ctrl_level, available)
        return changed

    def _on_topbar_resized(self):
        """Ширина верхней панели изменилась — пересчитываем ужим подписей.

        Единая точка пересчёта: сюда попадают и изменение размера окна, и
        разворот бокового меню (оно отнимает у шапки ~140 px), и анимация этого
        разворота — по кадру. Раньше пересчёт был только по resizeEvent окна, и
        после разворота панели кнопки оставались в прежнем виде: «Старт» уезжал
        за край окна (жалоба пользователя).
        """
        self._apply_topbar_compact()

    def _on_sidebar_width_changed(self):
        """Ширина боковой панели изменилась — пересчитываем ужим шапки.

        Панель меняет ширину плавно (анимация 180 мс), поэтому этот слот
        вызывается на каждом кадре: кнопки ужимаются вместе с панелью, а не
        рывком после её остановки.
        """
        try:
            self._apply_topbar_compact()
        except Exception as exc:                        # noqa: BLE001
            log.debug("Пересчёт шапки после изменения боковой панели не удался: %s", exc)

    def _make_view(self, key: str) -> QWidget:
        """Create one page and connect only the signals owned by that page.

        Pages remain resident after their first visit.  Their potentially
        expensive initial widget construction is therefore paid once, while a
        normal tab switch only changes the stack index.
        """
        if key == "routing":
            return RoutingView()
        if key == "servers":
            return ServersView()
        if key == "network":
            page = NetworkView()
            page.openLogRequested.connect(self._on_open_log_with_filter)
            return page
        if key == "strategy_lab":
            # Build the complex lazy page as a child of the existing stack.
            # Creating it parentless can make Windows briefly surface a native
            # mini-window before QScrollArea reparents it.
            page = StrategyLabView(self.stack)
            page.generationRequested.connect(self._on_ai_generation_requested)
            page.generationCancelRequested.connect(self._on_ai_generation_cancel_requested)
            page.strategyCheckRequested.connect(self._on_strategy_check_requested)
            page.strategyCheckCancelRequested.connect(self._on_strategy_check_cancel_requested)
            return page
        if key == "log":
            return LogView()
        if key == "settings":
            return SettingsView()
        if key == "about":
            return AboutView()
        item = next((entry for entry in NAV_ITEMS if entry.key == key), None)
        return _placeholder(item.label if item is not None else key)

    def _ensure_page(self, key: str) -> QWidget | None:
        """Create and register a page on its first visit, not at application start."""
        existing = self._views.get(key)
        if existing is not None:
            return existing
        if key not in {item.key for item in NAV_ITEMS}:
            return None
        page = self._make_view(key)
        self._views[key] = page
        self._pages[key] = self.stack.addWidget(self._scrollable_page(page, key=key))
        return page

    def _invalidate_views(self, keys: tuple[str, ...] | None = None) -> None:
        """Mark pages stale; invisible pages refresh only when next opened."""
        if keys is None:
            keys = tuple(item.key for item in NAV_ITEMS)
        self._stale_view_keys.update(keys)

    def _refresh_view(self, key: str) -> bool:
        """Refresh one constructed page and clear its stale mark on success."""
        view = self._views.get(key)
        if view is None or not hasattr(view, "refresh"):
            self._stale_view_keys.discard(key)
            return True
        try:
            view.refresh()
        except Exception as exc:  # noqa: BLE001 - UI refresh must not break navigation
            log.debug("Обновление вкладки %s не удалось: %s", key, exc)
            return False
        self._stale_view_keys.discard(key)
        return True

    def _set_page_active(self, key: str, active: bool) -> None:
        """Notify a constructed page when it enters or leaves the stack.

        The hook is intentionally optional.  Ordinary pages remain plain
        widgets; pages with periodic visual work stop that work while hidden.
        """
        view = self._views.get(key)
        hook_name = "page_activated" if active else "page_deactivated"
        hook = getattr(view, hook_name, None)
        if callable(hook):
            try:
                hook()
            except Exception as exc:  # noqa: BLE001 - navigation must remain usable
                log.debug("Переход активности вкладки %s не удался: %s", key, exc)

    def _apply_page_layout_mode(self):
        """Просит текущую вкладку подстроиться под ширину окна.

        Вкладки, которые умеют прятать второстепенные надписи (сейчас —
        библиотека AI-стратегий), иначе не узнают, что окно стало узким: внутри
        прокрутки их собственная ширина не меняется, и resizeEvent к ним не
        приходит.
        """
        try:
            idx = self.stack.currentIndex()
        except Exception:
            return
        for key, page_idx in self._pages.items():
            if page_idx != idx:
                continue
            view = self._views.get(key)
            hook = getattr(view, "_apply_layout_mode", None)
            if callable(hook):
                try:
                    hook()
                except Exception as exc:
                    log.debug("Подстройка вкладки %s не удалась: %s", key, exc)
            return

    def _refresh_current_view(self):
        """Force-refresh only the visible page after a real state transition."""
        self._update_start_button()
        try:
            idx = self.stack.currentIndex()
            for key, page_idx in self._pages.items():
                if page_idx == idx:
                    self._refresh_view(key)
                    return
        except Exception as exc:  # noqa: BLE001 - a stale view must not break the shell
            log.debug("Не удалось обновить текущую вкладку: %s", exc)

    def _refresh_visible_if_affected(self, keys: tuple[str, ...]) -> None:
        """Refresh a visible affected page without waking unrelated tabs."""
        try:
            visible_index = self.stack.currentIndex()
            for key, page_index in self._pages.items():
                if page_index == visible_index:
                    if key in keys:
                        self._refresh_view(key)
                    return
        except Exception as exc:  # noqa: BLE001 - status invalidation is best-effort
            log.debug("Не удалось обновить связанную с событием вкладку: %s", exc)

    def _on_open_log_with_filter(self, filter_key: str):
        """Кнопка «📋 Лог» в виджете лечения: вкладка «Логи» + категория."""
        log_view = self._ensure_page("log")
        if log_view is not None and hasattr(log_view, "set_filter"):
            log_view.set_filter(filter_key)
        self._on_navigate("log")

    def _on_navigate(self, key: str):
        self._show(key)

    def _show_extra(self):
        """Показывает дополнительную вкладку (сессия, без сохранения в порядок)."""
        if EXTRA_NAV.key in self._pages:
            self.sidebar.set_active(EXTRA_NAV.key)
            self._show(EXTRA_NAV.key)
            return
        page = ExtraView()
        self._views[EXTRA_NAV.key] = page
        idx = self.stack.addWidget(self._scrollable_page(page, key=EXTRA_NAV.key))
        self._pages[EXTRA_NAV.key] = idx
        self.sidebar.add_item(EXTRA_NAV)
        self.sidebar.set_active(EXTRA_NAV.key)
        self._show(EXTRA_NAV.key)

    # ── Размер и положение окна ─────────────────────────────────────────────
    # Настройка живёт в общем состоянии UI (core/ui_state.py): тот же файл, что
    # тема и порядок вкладок, с блокировкой и атомарной записью.
    GEOMETRY_KEY = "window_geometry"

    def _ui_state(self):
        try:
            import ui_state as _ui_state
            return _ui_state
        except Exception:
            return None

    def _default_window_size(self):
        """Размер при первом запуске, вписанный в текущий экран.

        Раньше окно всегда открывалось 1180x760 — на ноутбуке 1366x768 это почти
        весь экран. Теперь берём желаемый размер, но не больше 90% доступной
        области: окно не должно занимать всё место при первом запуске.
        """
        wanted_w, wanted_h = theme.WIN_W, theme.WIN_H
        try:
            screen = self.screen() or QGuiApplication.primaryScreen()
            avail = screen.availableGeometry()
            wanted_w = min(wanted_w, max(theme.WIN_MIN_W, int(avail.width() * 0.9)))
            wanted_h = min(wanted_h, max(theme.WIN_MIN_H, int(avail.height() * 0.9)))
        except Exception as exc:
            log.debug("Не удалось определить размер экрана: %s", exc)
        return wanted_w, wanted_h

    def _save_window_geometry(self) -> bool:
        """Сохраняет размер/позицию (в том числе «развёрнуто на весь экран»)."""
        state = self._ui_state()
        if state is None:
            return False
        try:
            if self.isFullScreen() or self.isMinimized():
                # Положение в этих режимах не осмысленно: сохраняем нормальную геометрию.
                geometry = self.normalGeometry()
                payload = {
                    "w": int(geometry.width()), "h": int(geometry.height()),
                    "x": int(geometry.x()), "y": int(geometry.y()),
                    "maximized": bool(self.isMaximized()),
                    "blob": "",
                }
            else:
                blob = bytes(self.saveGeometry().toBase64()).decode("ascii")
                payload = {
                    "w": int(self.width()), "h": int(self.height()),
                    "x": int(self.x()), "y": int(self.y()),
                    "maximized": bool(self.isMaximized()),
                    "blob": blob,
                }
            ok = state.update_state(**{self.GEOMETRY_KEY: payload})
            log.debug("Геометрия окна сохранена: %sx%s (maximized=%s, ok=%s)",
                      payload["w"], payload["h"], payload["maximized"], ok)
            return bool(ok)
        except Exception as exc:
            log.warning("Не удалось сохранить геометрию окна: %s", exc)
            return False

    def _restore_window_geometry(self) -> bool:
        """Возвращает прежний размер и позицию. True — восстановили."""
        state = self._ui_state()
        if state is None:
            return False
        try:
            data = state.get_value(self.GEOMETRY_KEY, None)
        except Exception:
            data = None
        if not isinstance(data, dict):
            return False

        restored = False
        try:
            blob = str(data.get("blob") or "")
            if blob:
                restored = bool(self.restoreGeometry(QByteArray.fromBase64(blob.encode("ascii"))))
        except Exception as exc:
            log.debug("restoreGeometry не сработал (%s) — пробуем по числам", exc)
        if not restored:
            try:
                width = int(data.get("w") or 0)
                height = int(data.get("h") or 0)
                if width > 0 and height > 0:
                    self.resize(max(theme.WIN_MIN_W, width), max(theme.WIN_MIN_H, height))
                    if data.get("x") is not None and data.get("y") is not None:
                        self.move(int(data["x"]), int(data["y"]))
                    restored = True
            except Exception as exc:
                log.debug("Восстановление геометрии по числам не удалось: %s", exc)

        if not restored:
            return False
        if data.get("maximized"):
            self.showMaximized()
        self._ensure_window_on_screen()
        return True

    def _ensure_window_on_screen(self) -> None:
        """Не даём окну оказаться за пределами экранов.

        Настройка сохраняется между запусками, а мониторов может стать меньше
        (ноутбук без второго монитора, смена разрешения) — тогда прежние
        координаты оставят окно за краем. В таком случае возвращаем его в
        доступную область главного экрана.
        """
        try:
            screens = QGuiApplication.screens()
            if not screens:
                return
            frame = self.frameGeometry()
            for screen in screens:
                if screen.availableGeometry().intersects(frame):
                    # Окно видно хотя бы частично — но убедимся, что хватает заголовка.
                    header = QRect(frame.x(), frame.y(), frame.width(), 40)
                    if screen.availableGeometry().intersects(header):
                        return
            avail = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
            width = min(self.width(), avail.width())
            height = min(self.height(), avail.height())
            self.resize(width, height)
            self.move(
                avail.x() + max(0, (avail.width() - width) // 2),
                avail.y() + max(0, (avail.height() - height) // 3),
            )
            log.info("Окно было за пределами экрана — перенесено в видимую область: %sx%s", width, height)
        except Exception as exc:
            log.debug("Проверка положения окна не удалась: %s", exc)

    # Пользователь меняет размер/положение — сохраняем после паузы
    # (на каждый пиксель перетаскивания писать файл нельзя).
    def resizeEvent(self, event):                       # noqa: N802 - Qt API
        super().resizeEvent(event)
        if getattr(self, "_geometry_timer", None) is not None:
            self._geometry_timer.start()
        self._apply_window_layout_mode()
        self._apply_page_layout_mode()

    def moveEvent(self, event):                         # noqa: N802 - Qt API
        super().moveEvent(event)
        if getattr(self, "_geometry_timer", None) is not None:
            self._geometry_timer.start()

    def _restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _process_engine_events(self):
        # The 200-ms timer drains cross-thread events only.  Start readiness
        # and header geometry update inside the relevant event branches.
        for event in drain_events():
            etype = event.get("type")
            if etype == "status_changed":
                running = bool(event.get("running", False))
                if not self._busy:
                    self.control.set_running(running, mode=get_current_mode())
                    if running:
                        self.control.set_vpn_blocked(routing_vpn_block_message())
                    if self.tray:
                        self.tray.set_running(running)
                self._update_start_button()
                self._update_startup_health()
                self._invalidate_views()
                self._refresh_current_view()
            elif etype == "mode_changed":
                mode = event.get("mode", "vpn")
                ui_key = {"vpn": "blue", "combo": "black", "dpi": "red"}.get(mode, "blue")
                self.mode_switch.set_active(ui_key)
                self._update_mode_hint()
                self._update_start_button()
                self._invalidate_views()
                self._refresh_current_view()
            elif etype == "config_changed":
                self._update_start_button()
                self._invalidate_views()
                self._refresh_current_view()
            elif etype == "transport_status_changed":
                # The core posted no data here: both views re-read their
                # existing sanitized snapshot surface. Do not refresh other
                # tabs or construct hidden ones for a routine health change.
                affected = ("servers", "routing")
                self._invalidate_views(affected)
                self._refresh_visible_if_affected(affected)
            elif etype == "auto_doctor_done":
                msg = event.get("message", "Автодоктор завершён")
                ok = bool(event.get("ok", False))
                log.info("%s", msg)
                # Фоновые уведомления — по галочке «Уведомления» в виджете
                # лечения (2026-10-06: «постоянные уведомления нельзя отключить»).
                if self.tray and get_doctor_notify():
                    try:
                        self.tray.notify(("✅ " if ok else "⚠ ") + msg)
                    except Exception:
                        pass
                self._invalidate_views()
                self._refresh_current_view()
            elif etype == "teardown_audit":
                problems = event.get("problems") or []
                if problems:
                    msg = "После остановки: " + "; ".join(str(p) for p in problems[:2])
                    log.warning("%s", msg)
                    if self.tray and get_doctor_notify():
                        try:
                            self.tray.notify("⚠ " + msg)
                        except Exception:
                            pass
            elif etype == "error":
                msg = event.get("message", "Неизвестная ошибка")
                log.warning("Событие ошибки от движка: %s", msg)

    def _poll_show_request(self):
        try:
            from single_instance import consume_show_request  # type: ignore
            if consume_show_request():
                self._restore_window()
        except Exception:
            pass

    def _stop_lifecycle_workers(self) -> bool:
        """Quiesce workers that could otherwise recreate runtime resources.

        The Start/Restart worker is the critical one: it owns synchronous core
        calls and cannot be safely terminated by Qt.  It receives an explicit
        shutdown request, then gets a bounded chance to return and stop any
        route it might have committed meanwhile.
        """
        self._ai_generation_pending = False
        self._strategy_check_pending = False
        clean = True
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.isRunning():
            try:
                request = getattr(worker, "request_shutdown", None)
                if callable(request):
                    request()
                if not worker.wait(20_000):
                    clean = False
                    log.warning("Lifecycle worker не завершился за 20 секунд при выходе")
            except Exception as exc:
                clean = False
                log.warning("Не удалось дождаться lifecycle worker при выходе: %s", exc)
        health = getattr(self, "_startup_health_worker", None)
        if health is not None and health.isRunning():
            try:
                if not health.wait(3_000):
                    clean = False
                    log.warning("Startup health worker не завершился за 3 секунды при выходе")
            except Exception as exc:
                clean = False
                log.debug("Не удалось дождаться startup health worker: %s", exc)
        self._shutdown_worker_timeout = not clean
        return clean

    def _stop_view_workers(self):
        try:
            nv = self._views.get("network")
            if nv is not None:
                w = getattr(nv, "_diag_worker", None)
                if w is not None and w.isRunning():
                    w.wait(2000)
        except Exception:
            pass
        try:
            rv = self._views.get("routing")
            vpn_exit_list = getattr(rv, "_vpn_exit_list", None)
            if vpn_exit_list is not None and hasattr(vpn_exit_list, "stop_workers"):
                vpn_exit_list.stop_workers()
        except Exception:
            pass

    def _hard_stop_runtime(self) -> bool:
        """Idempotently stop every resource that can retain the install folder.

        The order matters: first prevent workers from creating a new session,
        then stop PAC/VPN/DPI, then sweep detached e1-spike children and audit
        the result.  This is the path for a real application exit, not merely a
        window hide-to-tray action.
        """
        if self._shutdown_started:
            return not self._shutdown_worker_timeout
        self._shutdown_started = True
        for tname in ("_show_timer", "_event_timer", "_health_timer", "_updates_start_timer", "_release_timer"):
            try:
                timer = getattr(self, tname, None)
                if timer is not None:
                    timer.stop()
            except Exception:
                pass

        workers_stopped = self._stop_lifecycle_workers()
        self._stop_view_workers()
        # The scheduler is daemonized, but asking it to stop first prevents a
        # late cache/config write while Windows is releasing this installation.
        try:
            updater = getattr(self.engine, "bogus_updater", None)
            stop_updates = getattr(updater, "stop", None)
            if callable(stop_updates):
                stop_updates()
        except Exception as exc:
            log.debug("Не удалось остановить фоновый updater при выходе: %s", exc)

        proxy_restored = True
        try:
            proxy_restored, proxy_message = stop_routing_runtime()
            if not proxy_restored:
                log.warning("Не удалось штатно вернуть PAC при выходе: %s", proxy_message)
        except Exception as exc:
            proxy_restored = False
            log.warning("Остановка маршрутизации при выходе не удалась: %s", exc)

        # Controlled strategy workers launch temporary e1-spike sessions
        # directly, so cancel them before the final process sweep.
        for attr in ("_ai_generation_worker", "_strategy_check_worker"):
            try:
                worker = getattr(self, attr, None)
                if worker is not None and worker.isRunning():
                    if hasattr(worker, "request_cancel"):
                        worker.request_cancel()
                    else:
                        worker.requestInterruption()
                    if not worker.wait(15_000):
                        workers_stopped = False
                        log.warning("%s не завершился за 15 секунд при выходе", attr)
            except Exception as exc:
                workers_stopped = False
                log.debug("Не удалось остановить %s: %s", attr, exc)

        try:
            cleanup = dpi_strategy_ai_cleanup_runtime()
            if cleanup.get("errors"):
                log.warning("Финальная DPI-очистка: %s", cleanup.get("errors"))
        except Exception as exc:
            log.warning("Финальная DPI-очистка не удалась: %s", exc)
            workers_stopped = False

        try:
            report = verify_teardown()
            if not report.get("ok", False):
                workers_stopped = False
                log.warning("После выхода остались ресурсы: %s", report.get("problems") or report)
        except Exception as exc:
            workers_stopped = False
            log.warning("Не удалось проверить финальную очистку: %s", exc)

        if proxy_restored:
            self._shutdown_watchdog("CLEAN")
        else:
            # A watchdog may remain briefly only to restore a failed PAC
            # rollback; it receives EOF after this process exits and then ends.
            self._handoff_proxy_restore_to_watchdog()
        return bool(workers_stopped and proxy_restored)

    def _ensure_proxy_watchdog(self) -> bool:
        """Arm one pipe watchdog for PAC restoration after an unexpected exit."""
        proc = getattr(self, "_watchdog_proc", None)
        if proc is not None and proc.poll() is None:
            return True
        self._watchdog_proc = None
        try:
            import os
            import subprocess
            import sys
            from watchdog_launcher import watchdog_commands

            core_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
            watchdog_path = os.path.join(core_dir, "watchdog.py")
            if not os.path.exists(watchdog_path):
                raise RuntimeError("watchdog.py не найден")
            launch_errors = []
            for command in watchdog_commands(watchdog_path, sys.executable):
                child = None
                try:
                    child = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if child.stdin is None:
                        raise RuntimeError("stdin watchdog недоступен")
                    child.stdin.write(b"HELLO\nARM_PROXY\n")
                    child.stdin.flush()
                    self._watchdog_proc = child
                    log.info("Запущен watchdog PAC (PID %s, %s)", child.pid, command[0])
                    return True
                except Exception as exc:
                    launch_errors.append(f"{command[0]}: {exc}")
                    if child is not None:
                        try:
                            child.kill()
                            child.wait(timeout=1)
                        except Exception:
                            pass
            raise RuntimeError("; ".join(launch_errors) or "watchdog не запущен")
        except Exception as exc:
            log.warning("Не удалось запустить watchdog PAC: %s", exc)
            return False

    def _handoff_proxy_restore_to_watchdog(self) -> bool:
        """Ask a watchdog to retry PAC restoration after this process exits.

        Usually Start already armed it after the PAC commit. If that launch
        failed or the watchdog itself exited, bootstrap a fresh proxy-only
        watcher before conceding recovery; a failed Stop must not merely leave
        the durable PAC snapshot unattended.
        """
        proc = getattr(self, "_watchdog_proc", None)
        if proc is None or proc.poll() is not None or proc.stdin is None:
            if not self._ensure_proxy_watchdog():
                log.warning("Не удалось передать proxy restore: watchdog недоступен")
                return False
            proc = getattr(self, "_watchdog_proc", None)
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(b"RESTORE_PROXY\n")
            proc.stdin.flush()
            proc.stdin.close()
            self._watchdog_proc = None
            return True
        except Exception as exc:
            log.warning("Не удалось передать proxy restore watchdog: %s", exc)
            return False

    def _shutdown_watchdog(self, command: str = "CLEAN"):
        """Аккуратно завершает watchdog, сообщив ему, что делать НЕ нужно.

        command="CLEAN" — штатный выход: PAC уже возвращён родителем.
        Иначе watchdog увидел бы EOF и выполнил бы лишнее восстановление proxy.
        """
        proc = getattr(self, "_watchdog_proc", None)
        self._watchdog_proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(f"{command}\n".encode("ascii"))
                proc.stdin.flush()
                proc.stdin.close()
            proc.wait(timeout=2)
            log.info("Watchdog завершён штатно (%s)", command)
        except Exception as exc:
            log.debug("shutdown watchdog: %s", exc)
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass

    def _schedule_exit_fallback(self):
        """Force only the already-clean parent process out of a stuck Qt loop."""
        if self._exit_fallback_scheduled or self._shutdown_worker_timeout:
            return
        self._exit_fallback_scheduled = True
        try:
            import os
            import threading

            timer = threading.Timer(2.0, lambda: os._exit(0))
            timer.daemon = True
            timer.start()
        except Exception as exc:
            log.debug("Не удалось запланировать fallback выхода: %s", exc)

    def restart_application(self):
        """Полный перезапуск процесса (нужен, чтобы подтянуть новую тему)."""
        if not schedule_relaunch():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self,
                "Не удалось перезапустить",
                "Тема сохранена. Закройте UmbraNet и откройте его снова.",
            )
            return
        self._really_quit = True
        try:
            self._hard_stop_runtime()
        except Exception as exc:
            log.warning("Остановка runtime перед перезапуском: %s", exc)
        if self.tray:
            try:
                self.tray.hide()
            except Exception:
                pass
        self._schedule_exit_fallback()
        QApplication.quit()

    def _quit_app(self):
        """Tray action for a real, resource-clean application exit."""
        self._really_quit = True
        cleaned = self._hard_stop_runtime()
        if self.tray:
            self.tray.hide()
        # Close the native window too.  With quitOnLastWindowClosed disabled,
        # QApplication.quit() is still required below.
        try:
            self.close()
        except Exception:
            pass
        if cleaned:
            self._schedule_exit_fallback()
        QApplication.quit()

    def hideEvent(self, event):                         # noqa: N802 - Qt API
        super().hideEvent(event)
        # Сворачивание в трей = окно скрылось. Если приложение после этого
        # завершат из трея, размер уже должен быть сохранён.
        if getattr(self, "_initial_resize_done", False):
            self._save_window_geometry()

    def _check_program_updates(self):
        from umbranet.engine_adapter import get_update_checker
        checker = get_update_checker()
        result = checker.result
        if result.state == "available" and result.version != self._notified_release:
            self._notified_release = result.version
            if self.tray:
                self.tray.notify(
                    f"Доступна UmbraNet {result.version}. Откройте «О программе» → «Обновления»."
                )
        checker.check_async()

    def _start_background_updates(self):
        try:
            start = getattr(self.engine, "start_background_updates", None)
            if start is not None:
                start()
        except Exception as exc:
            log.warning("Не удалось запустить фоновые обновления (%s)", type(exc).__name__)

    def closeEvent(self, event):
        # Сохраняем размер/положение в любом случае: и при сворачивании в трей
        # (окно может больше не открыться — тогда размер должен пережить выход),
        # и при настоящем закрытии. Раньше размер не запоминался вообще.
        self._save_window_geometry()
        if self.tray and not self._really_quit:
            event.ignore()
            self.hide()
            self.tray.notify(
                "UmbraNet свёрнут в трей — программа и маршрутизация продолжают работать. "
                "Для полного завершения выберите «Полностью выйти» в меню трея."
            )
        else:
            # A real close must end the event loop explicitly: the application
            # intentionally keeps quitOnLastWindowClosed disabled for tray use.
            self._really_quit = True
            self._hard_stop_runtime()
            event.accept()
            QApplication.quit()

    # Вкладки с живым (незамороженным) resize — «как в хроме»: контент
    # всегда чёткий и сразу едет за краем окна; на слабой машине возможно
    # лёгкое подлагивание — это осознанный выбор юзера (заморозка со
    # растянутым снимком выглядела «странно»: мыло/лесенки). Остальные
    # вкладки — на заморозке-снимке со smooth-растяжкой (проверено юзером:
    # «Сеть и диагностика» — супер).
    # Телеграмизация 2026-09: все вкладки кроме карты теперь paintEvent-чистые
    # (RoundedPanel/Canvas) и не фризят при живом resize — freeze-снимок
    # больше не нужен. Карта исключена по просьбе юзера (сырой виджет).
    LIVE_RESIZE_PAGES = {
        "routing", "servers", "network", "strategy_lab", "log", "settings", "about", "extra",
    }

    # ── Когда вкладке давать прокрутку ──────────────────────────────────────
    # Пороги привязаны к минимальному окну (560x420) и его внутреннему месту:
    # если вкладке нужно больше этого запаса — значит в узком окне её содержимое
    # не поместится, и вместо обрезки даём прокрутку. Значения сверены по факту
    # (minimumSizeHint каждой вкладки):
    #   ширина: «Маршрутизация» 677, «Логи» 916, «AI-стратегии» 965;
    #   высота:  «Маршрутизация» 742, «AI-стратегии» 590.
    # Небольшие вкладки (Настройки 617x150, О программе 399x152) не оборачиваются:
    # у них своя прокрутка, лишняя обёртка только усложняла бы вид.
    SCROLL_MIN_PAGE_W = 620
    SCROLL_MIN_PAGE_H = 400

    #: За сколько пикселей до начала ужима кнопок режимов прячется правая часть
    #: вкладки «Маршрутизация». Изначально было 50 px — пользователь попросил
    #: прятать не так рано, поэтому 25 (вдвое меньше). Панель всё ещё уходит
    #: ДО того, как кнопки режимов начнут ужиматься, — просто вплотную к этому
    #: моменту, а не с большим запасом.
    SIDE_PANEL_LEAD_PX = 25
    #: Запас на возврат: расширились на столько выше порога — панель возвращается.
    SIDE_PANEL_HYSTERESIS_PX = 24

    def _apply_side_panel_mode(self, hidden: bool):
        """Сообщает вкладке «Маршрутизация», прятать ли правую часть.

        Вкладка сама решает, что делать: прячет панель целиком и показывает кнопку,
        которой её можно вызвать поверх списка. Других вкладок это не касается.
        """
        view = getattr(self, "_views", {}).get("routing")
        hook = getattr(view, "set_narrow_mode", None)
        if not callable(hook):
            return
        try:
            hook(bool(hidden))
        except (AttributeError, RuntimeError, TypeError) as exc:
            log.debug("Режим правой панели не применился: %s", exc)

    # Вкладки, которые в прокрутку страницы не берём НИКОГДА.
    # «Маршрутизация» — по прямому указанию пользователя: прокрутка всей вкладки
    # (когда вместе с содержимым уезжает и карточка выбора VPN-выхода) неудобна и
    # мешает. У неё своя прокрутка внутри: длинный список слева прокручивается
    # отдельно, а правая карточка остаётся на месте.
    NEVER_WRAP_IN_PAGE_SCROLL = {"routing"}

    def _scrollable_page(self, page: QWidget, key: str = "") -> QWidget:
        """Оборачивает вкладку в прокрутку, если её содержимое не сжимается.

        Зачем. Окно должно свободно ужиматься до половины/четверти экрана — этого
        требует штатная раскладка Windows. Но у «тяжёлых» вкладок (маршрутизация,
        библиотека AI-стратегий, профили, логи) собственные минимальные размеры
        больше узкого окна: их содержимое физически не сжимается. Раньше это
        распирало минимум всего окна (снап не работал), а без прокрутки ещё и
        молча обрезалось по краям: например, низ «Диспетчера задач» на обычном
        размере окна уходил за границу вкладки на ~50 px, и до него нельзя было
        долистать.

        Решение: вкладку, которой нужно больше SCROLL_MIN_PAGE_W по ширине ИЛИ
        больше SCROLL_MIN_PAGE_H по высоте, кладём в прокручиваемую область.
        Проверяется по факту (minimumSizeHint), поэтому новая тяжёлая вкладка
        попадёт сюда сама. Полосы появляются только когда места действительно не
        хватает (ScrollBarAsNeeded), поэтому на большом окне вкладка выглядит как
        раньше — без полос.
        """
        if key in self.NEVER_WRAP_IN_PAGE_SCROLL:
            return page
        try:
            hint = page.minimumSizeHint()
            # Берём максимум из «подсказки» и явно заданного минимума: некоторые
            # виджеты без раскладки сообщают минимум только через minimumSize(),
            # и без этого такие вкладки проскочили бы мимо прокрутки.
            needed_w = max(hint.width(), page.minimumWidth())
            needed_h = max(hint.height(), page.minimumHeight())
        except Exception:
            needed_w = needed_h = 0
        if needed_w <= self.SCROLL_MIN_PAGE_W and needed_h <= self.SCROLL_MIN_PAGE_H:
            return page

        # This wrapper is itself a native-capable QWidget on Windows. It must
        # never briefly exist without a parent while a lazy complex page is
        # attached, otherwise the first construction can flash as a mini-window.
        area = QScrollArea(self.stack)
        area.setWidgetResizable(True)          # вкладка растягивается по ширине окна
        area.setFrameShape(QScrollArea.NoFrame)
        if key == "servers":
            # The outer page scrollbar is the one that appears when the whole
            # Servers view is too tall.  Reserve its gutter even after its
            # thumb becomes unnecessary; otherwise the viewport widens by one
            # scrollbar and both server columns visibly jump during resize.
            # The shared QSS keeps a disabled no-range handle transparent.
            area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        area.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}"
            + theme.scrollbar_qss()
        )
        area.setWidget(page)
        area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        log.debug(
            "Вкладке %s нужно %sx%s px — включена прокрутка (полосы появляются, "
            "только когда места не хватает)", type(page).__name__, needed_w, needed_h,
        )
        return area

    def _show(self, key: str):
        view = self._ensure_page(key)
        if view is None or key not in self._pages:
            return
        previous_key = self._active_page_key
        if previous_key and previous_key != key:
            self._set_page_active(previous_key, False)
        self.set_live_resize(key in self.LIVE_RESIZE_PAGES)
        self.stack.setCurrentIndex(self._pages[key])
        if previous_key != key:
            self._active_page_key = key
            self._set_page_active(key, True)
        self._apply_window_layout_mode()
        self._apply_page_layout_mode()
        # Switching between unchanged tabs is intentionally render-only.  A
        # refresh is reserved for a first visit (the constructor does that) or
        # for a page invalidated by an actual engine/configuration event.
        if key in self._stale_view_keys:
            self._refresh_view(key)


_RELAUNCH_FLAG = "umbranet.relaunch"


def _relaunch_flag_path() -> str:
    import os
    import tempfile
    return os.path.join(tempfile.gettempdir(), _RELAUNCH_FLAG)


def consume_relaunch_flag() -> bool:
    """True, если этот запуск — перезапуск после смены темы."""
    import os
    path = _relaunch_flag_path()
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def _force_window_foreground(win) -> None:
    """Достаёт окно на передний план после перезапуска (Windows иначе оставляет его на панели)."""
    try:
        win.showNormal()
        win.raise_()
        win.activateWindow()
    except Exception:
        pass
    try:
        import sys
        if sys.platform != "win32":
            return
        import ctypes
        hwnd = int(win.winId())
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def schedule_relaunch() -> bool:
    """Запускает новый процесс UmbraNet после выхода текущего.

    Сторожок на python ждёт наш PID (без .bat и кириллицы в cmd) и открывает
    start.pyw через ShellExecute(SW_SHOWNORMAL) — окно выходит на передний план.
    """
    import os
    import subprocess
    import sys

    exe = sys.executable
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    start = os.path.join(root, "start.pyw")
    target = start if os.path.isfile(start) else os.path.abspath(sys.argv[0] if sys.argv else "")
    if not target or not os.path.isfile(target):
        log.warning("Не найден start.pyw для перезапуска")
        return False
    try:
        with open(_relaunch_flag_path(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as exc:
        log.debug("Флаг перезапуска не записан: %s", exc)
    watcher = (
        "import os,sys,time\n"
        "pid=int(sys.argv[1]); exe,target,root=sys.argv[2],sys.argv[3],sys.argv[4]\n"
        "end=time.time()+45\n"
        "while time.time()<end:\n"
        "    try:\n"
        "        os.kill(pid,0)\n"
        "    except PermissionError:\n"
        "        time.sleep(0.3); continue\n"
        "    except OSError:\n"
        "        break\n"
        "    time.sleep(0.3)\n"
        "time.sleep(0.6)\n"
        "try:\n"
        "    import ctypes\n"
        "    ctypes.windll.shell32.ShellExecuteW(None,'open',exe,'\"'+target+'\"',root,1)\n"
        "except Exception:\n"
        "    import subprocess\n"
        "    subprocess.Popen([exe,target],cwd=root,close_fds=True)\n"
    )
    try:
        flags = 0
        if sys.platform == "win32":
            flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        subprocess.Popen(
            [exe, "-c", watcher, str(os.getpid()), exe, target, root],
            cwd=root,
            close_fds=True,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        log.warning("Не удалось запланировать перезапуск: %s", exc)
        return False


def run():
    import sys

    guard = None
    try:
        from single_instance import (  # type: ignore
            SingleInstance,
            request_show_existing,
        )
        guard = SingleInstance("UmbraNet_SingleInstance_Mutex_v1")
        if guard.already_running():
            request_show_existing()
            print("UmbraNet уже запущен — показываю существующее окно.")
            return
    except Exception:
        guard = None

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10))
    try:
        from umbranet.ru_clipboard import install_ru_clipboard
        install_ru_clipboard(app)
    except Exception:
        pass
    win = MainWindow()
    win.show()
    if consume_relaunch_flag():
        # После смены темы окно иначе остаётся кнопкой на панели задач.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: _force_window_foreground(win))
        QTimer.singleShot(400, lambda: _force_window_foreground(win))
    try:
        code = app.exec()
    finally:
        if guard is not None:
            try:
                guard.release()
            except Exception:
                pass
    sys.exit(code)
