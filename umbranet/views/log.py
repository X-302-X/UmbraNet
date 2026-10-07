"""
UmbraNet - раздел «Журнал» (PySide6) — телеграмизирован.

Живой поток DNS-запросов:
  • строка статистики сверху (всего / обход / напрямую / блок / починка / ошибка):
    карточки ужимаются под ширину окна плавно и в пределе остаются «смайлик + цифра»
    (StatCard.set_compression — тот же приём, что у кнопок DNS / Combo / DPI);
  • поиск + фильтры (Все/Обход/Напрямую) + Пауза/Очистить + индикатор LIVE;
  • список строк с цветными бейджами (тип, маршрут, источник) и латентностью;
  • пустое состояние.

ТЕЛЕГРАМИЗАЦИЯ (фикс дёрганья при ресайзе):
  Раньше каждая строка была QFrame с 7 QLabel (200 строк = ~1400 виджетов).
  При ресайзе Qt делал layout.activate() на все 1400 — 50+ мс на ноуте.
  Теперь весь список рисует ОДИН paintEvent (LogCanvas, RowCanvas):
  только видимые ~15 строк за 2-3 мс, кэш, затухающий ползунок.

Архитектура обновлений:
  - Новые записи приходят через QueryLog.subscribe() из DNS-потока.
  - Прокидываются в UI через Qt-сигнал _entry_arrived (thread-safe).
  - refresh() при переключении вкладки НЕ перестраивает DOM —
    только подгружает записи которые пришли пока вкладка была скрыта.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import get_query_log
from umbranet.widgets.log_canvas import LogCanvas
from umbranet.widgets.rounded_panel import RoundedPanel

SOURCE_LABELS = {
    "cache": "кэш", "stale-cache": "stale", "routed": "обход",
    "system": "система", "bogus-NX": "bogus", "blocked": "блок", "servfail": "ошибка",
    "bg-refresh": "фон", "fixed": "починка", "error": "ошибка",
    "check": "проверка", "leak": "утечка",
}

MAX_VISIBLE = 200   # лимит отображения (для canvas — виртуальный скролл, но лимит буфера)
MAX_BUFFER  = 2000  # строк в памяти


def _reason_for(entry) -> str:
    source = getattr(entry, "source", "")
    routed = getattr(entry, "routed", False)
    rcode = getattr(entry, "rcode", "")
    if source == "cache":
        return "свежий кэш"
    if source == "stale-cache":
        return "stale + фон"
    if source == "routed":
        return "домен в обходе" if routed else "secure"
    if source == "system":
        return "fallback/system"
    if source == "blocked":
        return "блоклист"
    if source == "bogus-NX":
        return "bogus-IP"
    if source == "servfail":
        return "не ответил"
    if source == "check":
        return "диагностика"
    if source == "leak":
        return "утечка"
    return rcode or "—"


class StatCard(RoundedPanel):
    """Карточка счётчика в «Логах»: эмодзи + цифра, под ними — подпись.

    Зачем класс (жалоба пользователя: «квадратики съедаются размером окна»).
    Раньше карточка была жёсткой плиткой «цифра сверху, подпись снизу» с
    отступами 14 px: шесть таких плиток требовали ≈870 px, и в узком окне
    раскладка просто сжимала их — подписи обрезались, цифры упирались в рамки.
    Само окно при этом не могло ужаться: плитки распирали минимум вкладки.

    Теперь карточка ужимается ПЛАВНО, как кнопки режимов DNS / Combo / DPI в
    шапке (`ModeSwitch.set_compression` в widgets/header.py):

      t = 0.0 — полный вид: эмодзи + цифра, под карточкой подпись «Обход»;
      t → 1.0 — отступы ужимаются, подпись сначала укорачивается многоточием,
                а потом пропадает совсем. Остаётся ровно то, что просил
                пользователь: СМАЙЛИК И ЦИФРА.

    Цифра не исчезает и не укорачивается никогда: это смысл карточки. Сама
    эмодзи нужна ещё и как «якорь» узнавания — в узком виде подписи нет, а
    значок 📊 / 🚀 / ⛔ сразу говорит, о чём счётчик. Полное название всегда
    лежит в подсказке карточки.
    """

    _PAD_FULL = 14      # боковые отступы в полном виде
    _PAD_MIN = 6        # боковые отступы в ужатом виде
    _GAP_FULL = 6       # зазор между эмодзи и цифрой
    _GAP_MIN = 4
    _LABEL_HIDE_T = 0.72  # с этой степени ужима подпись пропадает совсем

    def __init__(self, emoji: str, label: str, value: str, color: str):
        super().__init__(theme.CARD, theme.BORDER, radius=12)
        self._emoji_text = emoji
        self._label_text = label
        self._t = -1.0                 # «ещё не настроено»
        self._metrics: dict[int, QFontMetrics] = {}

        lay = QVBoxLayout(self)
        self._lay = lay
        lay.setContentsMargins(self._PAD_FULL, 10, self._PAD_FULL, 10)
        lay.setSpacing(2)

        # верхняя строка: эмодзи + цифра (это и есть «минимальный» вид)
        top = QHBoxLayout()
        self._top = top
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(self._GAP_FULL)
        self._emoji = QLabel(emoji)
        self._emoji.setStyleSheet(
            "background:transparent;border:none;font-size:16px;")
        self._value = QLabel(value)
        self._value.setStyleSheet(
            f"color:{color};font-size:22px;font-weight:700;background:transparent;border:none;")
        top.addWidget(self._emoji)
        top.addWidget(self._value)
        top.addStretch(1)
        lay.addLayout(top)

        # подпись: в узком окне пропадает
        self._label = QLabel(label)
        self._label.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(self._label)

        self.setToolTip(f"{emoji} {label}")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.set_compression(0.0, force=True)

    # ── метрики ──
    def _text_px(self, text: str, pixel_size: int = 13) -> int:
        """Ширина текста этим шрифтом. Кэш — по размеру шрифта.

        Карточка меряет текст тремя размерами (16 / 22 / 12), поэтому один общий
        кэш метрик давал бы ширину не тем шрифтом, каким текст рисуется, — и
        карточка ужималась бы до размера, в который цифра не влезает.
        """
        metrics = self._metrics.get(pixel_size)
        if metrics is None:
            font = QFont(self.font())
            font.setPixelSize(pixel_size)
            metrics = QFontMetrics(font)
            self._metrics[pixel_size] = metrics
        return metrics.horizontalAdvance(text)

    def _elide(self, text: str, pixel_size: int, room: int) -> str:
        metrics = self._metrics.get(pixel_size)
        if metrics is None:
            self._text_px(text, pixel_size)
            metrics = self._metrics[pixel_size]
        return metrics.elidedText(text, Qt.ElideRight, room)

    def _top_width(self, text: str) -> int:
        return (self._text_px(self._emoji_text, 16) + self._GAP_FULL
                + self._text_px(text, 22))

    def icon_width(self) -> int:
        """Ширина «эмодзи + цифра» с ужатыми отступами — до неё карточка сжимается."""
        return (self._text_px(self._emoji_text, 16) + self._GAP_MIN
                + self._text_px(self._value.text(), 22) + 2 * self._PAD_MIN)

    def full_width(self) -> int:
        """Ширина полного вида: что шире — верхняя строка или подпись."""
        top = self._top_width(self._value.text())
        label = self._text_px(self._label_text, 12)
        return max(top, label) + 2 * self._PAD_FULL

    def width_for(self, t: float) -> int:
        """Ширина карточки при степени ужима t (для явной ширины в ужатом виде).

        Ниже icon_width() не опускаемся: это размер «эмодзи + цифра» с отступами,
        и меньше него карточка уже обрезала бы цифру.
        """
        if t <= 0.0:
            return 0                       # 0 = «пусть раскладка делит место сама»
        full, icons = self.full_width(), self.icon_width()
        return max(icons, round(full + (icons - full) * t))

    def compression(self) -> float:
        return self._t

    def set_compression(self, t: float, force: bool = False) -> bool:
        """Ужимает карточку. t = 0.0 — полный вид, t = 1.0 — только эмодзи и цифра."""
        t = 0.0 if t <= 0 else (1.0 if t >= 1 else float(t))
        if not force and abs(t - self._t) < 0.004:
            return False
        self._t = t

        pad = round(self._PAD_FULL + (self._PAD_MIN - self._PAD_FULL) * t)
        self._lay.setContentsMargins(pad, 10, pad, 10)
        self._top.setSpacing(round(self._GAP_FULL + (self._GAP_MIN - self._GAP_FULL) * t))

        show_label = t < self._LABEL_HIDE_T
        self._label.setVisible(show_label)
        if show_label:
            room = max(0, self.width() - 2 * pad)
            if room <= 0:
                room = self.full_width() - 2 * pad
            if self._text_px(self._label_text, 12) <= room:
                self._label.setText(self._label_text)
            else:
                self._label.setText(self._elide(self._label_text, 12, room))

        # Минимум карточки — «эмодзи + цифра»: именно до этого размера она и
        # должна ужиматься, чтобы шесть счётчиков влезали в узкое окно.
        self.setMinimumWidth(self.icon_width())
        self.setMaximumWidth(16777215)
        self.updateGeometry()
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Подпись укорачивается по фактической ширине карточки, а не только по
        # расчётной: раскладка может отдать карточке чуть меньше, чем мы посчитали.
        if self._t < self._LABEL_HIDE_T:
            room = max(0, self.width() - 2 * self._lay.contentsMargins().left())
            if room and self._text_px(self._label_text, 12) > room:
                self._label.setText(self._elide(self._label_text, 12, room))
            elif room:
                self._label.setText(self._label_text)


class ChipButton(QPushButton):
    """Кнопка «Логов» (фильтр или действие): значок + слово, ужимается до значка.

    Зачем класс. Строка фильтров задавала минимальную ширину всей вкладки:
    девять кнопок с подписями — это ≈780 px, и вкладка «Логи» физически не
    могла стать уже этого. Из-за неё же не сжимались карточки-счётчики: вкладка
    оставалась широкой, карточки делили её ширину и подписи в них не ужимались,
    а уезжали под край окна.

    Теперь кнопка ужимается тем же приёмом, что кнопки режимов DNS / Combo / DPI
    (см. `ModeSwitch.set_compression`): подпись укорачивается, в пределе остаётся
    только значок. Полное название всегда лежит в подсказке.
    """

    _PAD = 32           # QSS-отступы по бокам, обычный вид (padding 0 16px)
    _PAD_COMPACT = 20   # плотный вид (padding 0 10px)
    _MIN_LABEL_PX = 30  # меньше — показываем только значок

    def __init__(self, emoji: str, label: str):
        super().__init__(f"{emoji}  {label}")
        self._emoji = emoji
        self._label = label
        self._t = -1.0
        self._natural = 0
        self._metrics: dict[int, QFontMetrics] = {}
        self.setMinimumWidth(0)

    def _text_px(self, text: str, pixel_size: int = 13) -> int:
        metrics = self._metrics.get(pixel_size)
        if metrics is None:
            font = QFont(self.font())
            font.setPixelSize(pixel_size)
            metrics = QFontMetrics(font)
            self._metrics[pixel_size] = metrics
        return metrics.horizontalAdvance(text)

    def full_width(self) -> int:
        """Ширина с полной подписью: как её посчитал Qt (с QSS-отступами)."""
        if self._t <= 0.0:
            self._natural = self.sizeHint().width()
        return max(1, self._natural or (self._text_px(f"{self._emoji}  {self._label}")
                                        + self._PAD))

    def icon_width(self, compact: bool = False) -> int:
        """Ширина со значком — до неё кнопка ужимается.

        В плотном виде отступы кнопки меньше (и QSS это учитывает), поэтому и
        минимальная ширина другая — иначе значок обрезался бы.
        """
        pad = self._PAD_COMPACT if compact else self._PAD
        return max(32, self._text_px(self._emoji, 14) + pad)

    def compression(self) -> float:
        return self._t

    def set_label(self, emoji: str, label: str) -> None:
        """Меняет полную подпись (например, «Пауза» → «Продолжить»).

        Ширина кнопки после этого другая, поэтому ужим применяется заново — иначе
        кнопка осталась бы обрезанной по прежнему размеру.
        """
        self._emoji = emoji
        self._label = label
        self._natural = 0
        t = self._t
        self._t = -1.0
        self.set_compression(0.0, force=True)
        if t > 0.0:
            self.set_compression(t, force=True)
        else:
            self._t = 0.0
            self.setText(f"{emoji}  {label}")

    #: С этой степени ужима кнопка переходит на плотные отступы и ужимается до
    #: значка. Порог ближе к концу, чтобы переход не читался как рывок: к этому
    #: моменту подпись уже ужата почти до значка.
    COMPACT_T = 0.7

    def set_compression(self, t: float, force: bool = False) -> bool:
        """Ужимает кнопку: 0.0 — «🚀 Обход», 1.0 — только «🚀»."""
        t = 0.0 if t <= 0 else (1.0 if t >= 1 else float(t))
        if not force and abs(t - self._t) < 0.004:
            return False
        if self._t <= 0.0:
            self._natural = self.sizeHint().width()
        self._t = t
        if t <= 0.0:
            self.setText(f"{self._emoji}  {self._label}")
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self.setToolTip("")
            return True

        compact = t >= self.COMPACT_T
        icons = self.icon_width(compact)
        width = max(icons, round(self._natural + (icons - self._natural) * t))
        self.setFixedWidth(width)
        room = width - (self._PAD_COMPACT if compact else self._PAD) \
            - self._text_px(self._emoji, 14) - self._text_px("  ")
        if room >= self._text_px(self._label):
            self.setText(f"{self._emoji}  {self._label}")
            self.setToolTip("")
        elif room >= self._MIN_LABEL_PX:
            metrics = self._metrics.get(13)
            elided = metrics.elidedText(self._label, Qt.ElideRight, room) if metrics \
                else self._label
            self.setText(f"{self._emoji}  {elided}")
            self.setToolTip(self._label)
        else:
            self.setText(self._emoji)
            self.setToolTip(self._label)
        return True


class LogView(QWidget):
    _entry_arrived = Signal(object)

    _SEARCH_ICON = "🔍"       # значок поля поиска (единственная подпись в ужатом виде)
    _SEARCH_PAD = 12          # боковые отступы поля (QSS padding: 0 12px)
    _SEARCH_SLACK = 6         # запас: эмодзи рисуется шире, чем показывает метрика
    _SEARCH_ICON_MIN_PX = 20  # нижняя граница ширины значка для любого шрифта

    def __init__(self):
        super().__init__()
        self.qlog = get_query_log()
        self._filter  = "all"
        self._search  = ""
        self._paused  = False
        self._rows: list = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        # ── заголовок + LIVE ──
        head = QHBoxLayout()
        title = QLabel("Логи запросов")
        title.setStyleSheet(f"color:{theme.TEXT};font-size:22px;font-weight:700;")
        head.addWidget(title)
        self._live = QLabel("●  LIVE")
        self._live.setStyleSheet(f"color:{theme.GREEN};font-size:12px;font-weight:700;")
        head.addSpacing(10)
        head.addWidget(self._live)
        head.addStretch()
        btn_export = QPushButton("📋 Копировать")
        btn_export.setCursor(Qt.PointingHandCursor)
        btn_export.setFixedHeight(32)
        btn_export.setStyleSheet(self._chip_qss(False))
        btn_export.clicked.connect(self._export_clipboard)
        head.addWidget(btn_export)
        outer.addLayout(head)

        # ── карточки статистики ──
        # Эмодзи подобран по смыслу счётчика и остаётся единственной подписью,
        # когда карточка ужата до предела (см. StatCard).
        stats = QHBoxLayout()
        self._stat_gap = 12
        stats.setSpacing(self._stat_gap)
        self._stat_total   = StatCard("📊", "Всего",    "0", theme.ACCENT2)
        self._stat_routed  = StatCard("🚀", "Обход",    "0", theme.ACCENT)
        self._stat_direct  = StatCard("➡️", "Напрямую", "0", theme.SUBTEXT)
        self._stat_blocked = StatCard("⛔", "Блок",     "0", theme.RED)
        self._stat_fixed   = StatCard("🔧", "Починка",  "0", theme.GREEN)
        self._stat_error   = StatCard("⚠️", "Ошибка",   "0", theme.ORANGE)
        self._stat_cards = [self._stat_total, self._stat_routed, self._stat_direct,
                            self._stat_blocked, self._stat_fixed, self._stat_error]
        for c in self._stat_cards:
            stats.addWidget(c, 1)
        outer.addLayout(stats)

        # ── панель управления ──
        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(f"{self._SEARCH_ICON}  Поиск по домену...")
        self._search_input.setFixedHeight(36)
        self._search_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 12px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        # Поле поиска отдаёт место первым: фильтры ужимаются только до значков, а
        # поле тянулось за остатком. У него был только «свой» минимум Qt, а он
        # меньше, чем нужно значку лупы вместе с отступами, — и Qt, не найдя места,
        # заменял подсказку «🔍» многоточием. Жалоба пользователя: «смайлик лупы из
        # поисковой строки исчезает». Ставим честный минимум: значок + отступы.
        self._search_min_w = self._search_min_width()
        self._search_input.setMinimumWidth(self._search_min_w)
        self._search_input.textChanged.connect(self._on_search)
        ctrl.addWidget(self._search_input, 1)

        # Значки подобраны по смыслу фильтра и остаются единственной подписью,
        # когда кнопка ужата до предела (те же значки, что у карточек-счётчиков).
        self._chips = {}
        for key, emoji, label in [
            ("all", "🌐", "Все"), ("routed", "🚀", "Обход"), ("direct", "➡️", "Напрямую"),
            ("blocked", "⛔", "Блок"), ("fixed", "🔧", "Починки"),
            ("errors", "⚠️", "Ошибки"), ("cache", "💾", "Кэш"),
        ]:
            chip = ChipButton(emoji, label)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setFixedHeight(36)
            chip.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self._chips[key] = chip
            ctrl.addWidget(chip)

        self._btn_pause = ChipButton("⏸", "Пауза")
        self._btn_pause.setCursor(Qt.PointingHandCursor)
        self._btn_pause.setFixedHeight(36)
        self._btn_pause.setStyleSheet(self._chip_qss(False))
        self._btn_pause.clicked.connect(self._toggle_pause)
        ctrl.addWidget(self._btn_pause)

        self._btn_clear = ChipButton("🗑", "Очистить")
        self._btn_clear.setCursor(Qt.PointingHandCursor)
        self._btn_clear.setFixedHeight(36)
        self._btn_clear.setStyleSheet(self._chip_qss(False))
        self._btn_clear.clicked.connect(self._clear)
        ctrl.addWidget(self._btn_clear)
        outer.addLayout(ctrl)
        #: Кнопки строки фильтров — их ужимаем вместе с карточками.
        self._ctrl_buttons = list(self._chips.values()) + [self._btn_pause, self._btn_clear]
        self._ctrl_layout = ctrl

        # ── шапка столбцов ──
        colhead = QFrame()
        colhead.setStyleSheet("background:transparent;")
        chl = QHBoxLayout(colhead)
        chl.setContentsMargins(12, 0, 26, 0)
        chl.setSpacing(10)
        for text, w, stretch in [
            ("Время",   64, 0), ("Домен",    0,   1), ("Тип",    54, 0),
            ("Маршрут", 64, 0), ("Источник", 64,  0),
            ("Причина", 140, 0), ("мс",       56,  0),
        ]:
            lbl = QLabel(text)
            if w:
                lbl.setFixedWidth(w)
            else:
                from PySide6.QtWidgets import QSizePolicy
                lbl.setMinimumWidth(30)
                lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            if text == "мс":
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            elif text in ("Тип", "Маршрут", "Источник"):
                lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(f"color:{theme.MUTED};font-size:11px;font-weight:600;background:transparent;")
            chl.addWidget(lbl, stretch)
        outer.addWidget(colhead)

        # ── ТЕЛЕГРАМИЗИРОВАННЫЙ список (один canvas вместо 1400 виджетов) ──
        # Canvas сам рисует пустое состояние, поэтому отдельный QLabel не нужен
        self._canvas = LogCanvas()
        self._canvas.contextMenuRequested.connect(self._show_canvas_menu)
        self._canvas.domainCopied.connect(lambda d: QGuiApplication.clipboard().setText(d))
        outer.addWidget(self._canvas, 1)

        # ── подписка на новые записи из ядра ──
        self._entry_arrived.connect(self._add_row_ui)
        try:
            self.qlog.subscribe(self._on_entry_from_core)
        except Exception:
            pass

        self._last_entry_ts: float = 0.0
        self._live_blink_state: bool = False
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(600)
        self._live_timer.timeout.connect(self._tick_live)
        self._live_timer.start()

        self._set_filter("all")
        self.refresh()

    # ── ужимание карточек под ширину окна ────────────────────────────────────

    def _available_width(self) -> int:
        """Доступная ширина строки карточек: страница минус её боковые отступы.

        Ширину берём не только у себя: в узком окне вкладка может лежать внутри
        прокрутки и тогда её собственная ширина равна минимуму, по которому
        «узко / не узко» не определить. Родитель-вьюпорт знает настоящий размер.
        """
        width = self.width()
        host = self.parentWidget()
        if host is not None and host.width() > 0:
            width = min(width, host.width())
        margins = self.layout().contentsMargins()
        return max(0, int(width) - margins.left() - margins.right())

    def _search_min_width(self) -> int:
        """Минимальная ширина поля поиска — такая, чтобы значок лупы оставался виден.

        Ширина значка берётся у Qt (на другом шрифте он шире), но не меньше
        _SEARCH_ICON_MIN_PX: у метрики эмодзи бывают занижены. К значку добавляем
        отступы QSS, рамку и небольшой запас — всё это съедает место под текст.
        """
        metrics = QFontMetrics(self._search_input.font())
        icon = max(self._SEARCH_ICON_MIN_PX,
                   metrics.horizontalAdvance(self._SEARCH_ICON))
        return int(icon + 2 * self._SEARCH_PAD + 2 + self._SEARCH_SLACK)

    def _apply_layout_mode(self, force: bool = False):
        """Ужимает карточки-счётчики под ширину окна.

        Полное состояние требует ≈870 px на шесть карточек; в узком окне они
        уезжали под обрез (подписи обрезались, рамки наезжали на цифры). Теперь
        степень ужима едет за шириной окна: сначала отступы и подписи, в конце
        остаётся «смайлик + цифра».
        """
        cards = getattr(self, "_stat_cards", None)
        if not cards:
            return
        avail = self._available_width()
        if avail <= 0:
            return

        cards_full = sum(c.full_width() for c in cards) + self._stat_gap * (len(cards) - 1)
        cards_min = sum(c.icon_width() for c in cards) + self._stat_gap * (len(cards) - 1)

        # Строка фильтров ужимается вместе с карточками: она задаёт минимальную
        # ширину всей вкладки, и без её ужима карточкам просто негде сжиматься —
        # вкладка остаётся широкой, а карточки уезжают под край окна.
        buttons = getattr(self, "_ctrl_buttons", [])
        ctrl = getattr(self, "_ctrl_layout", None)
        if buttons and ctrl is not None:
            ctrl_gaps = ctrl.spacing() * (len(buttons) + 1)   # + поиск
            # Не «минимум Qt», а наш: он гарантирует лупу в поле поиска.
            search_min = self._search_min_w
            ctrl_full = sum(b.full_width() for b in buttons) + ctrl_gaps + search_min
            ctrl_min = sum(b.icon_width() for b in buttons) + ctrl_gaps + search_min
        else:
            ctrl_full = ctrl_min = 0

        # Степень ужима считается ДЛЯ КАЖДОЙ строки отдельно: у карточек и у
        # строки фильтров своя естественная ширина, и ужимать карточки из-за
        # того, что не помещаются фильтры, нельзя — на широком окне они тогда
        # сжимались бы без нужды (проверено: 160 px → 63 px при полностью
        # свободном месте).
        def ramp(need_full: int, need_min: int) -> float:
            if avail >= need_full:
                return 0.0
            if avail <= need_min:
                return 1.0
            return (need_full - avail) / max(1, need_full - need_min)

        t = ramp(cards_full, cards_min)
        t_chips = ramp(ctrl_full, ctrl_min)

        # В ужатом виде ширина карточки задаётся явно: иначе её определяла бы
        # ширина вкладки, а вкладку держат другие строки (таблица логов) — и
        # карточки оставались бы широкими с пустым местом внутри. Ширины раздаём
        # по бюджету: шесть отдельных округлений дают в сумме на 1–2 px больше
        # места, и правый край последней карточки уходил за границу вкладки.
        widths = [c.width_for(t) for c in cards]
        if any(widths):
            budget = max(0, avail - self._stat_gap * (len(cards) - 1))
            while sum(widths) > budget:
                widest = max(range(len(widths)), key=lambda i: widths[i])
                if widths[widest] <= cards[widest].icon_width():
                    break                      # ниже «значок + цифра» не ужимаем
                widths[widest] -= 1

        for c, width in zip(cards, widths, strict=True):
            c.set_compression(t, force=force)
            if width:
                c.setFixedWidth(width)
            else:
                c.setMinimumWidth(0)
                c.setMaximumWidth(16777215)
        for b in buttons:
            b.set_compression(t_chips, force=force)
            compact = t_chips >= b.COMPACT_T or t_chips <= 0
            style = self._chip_qss(b.isChecked(), compact=compact) if b in self._chips.values() \
                else self._chip_qss(False, compact=compact)
            if b.styleSheet() != style:
                b.setStyleSheet(style)

        # Поле поиска в ужатом виде: длинная подсказка «Поиск по домену...»
        # держит минимальную ширину поля, а вместе с ней и всей вкладки.
        short = t_chips >= 0.5
        placeholder = self._SEARCH_ICON if short else f"{self._SEARCH_ICON}  Поиск по домену..."
        if self._search_input.placeholderText() != placeholder:
            self._search_input.setPlaceholderText(placeholder)
            self._search_input.setToolTip("Поиск по домену" if short else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_layout_mode()

    def _chip_qss(self, active: bool, compact: bool = False) -> str:
        """Стиль кнопки-фильтра. В плотном виде отступы меньше: со «штатными» 16 px
        кнопка со значком не могла стать у́же 52 px, и строка фильтров держала
        минимальную ширину всей вкладки."""
        pad = 10 if compact else 16
        if active:
            return (f"QPushButton{{background:{theme.brand_grad()};color:{theme.WHITE};"
                    f"border:none;border-radius:10px;padding:0 {pad}px;font-weight:600;}}")
        return (f"QPushButton{{background:{theme.CARD};color:{theme.SUBTEXT};"
                f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 {pad}px;}}"
                f"QPushButton:hover{{color:{theme.TEXT};border-color:{theme.ACCENT};}}")

    def _tick_live(self):
        if self._paused:
            return
        import time as _time
        now = _time.monotonic()
        active = (self._last_entry_ts > 0) and (now - self._last_entry_ts < 3.0)
        if active:
            self._live_blink_state = not self._live_blink_state
            color = theme.GREEN if self._live_blink_state else "#1a6632"
            self._live.setText("●  LIVE")
            self._live.setStyleSheet(f"color:{color};font-size:12px;font-weight:700;")
        else:
            self._live.setText("○  нет данных")
            self._live.setStyleSheet(f"color:{theme.MUTED};font-size:12px;font-weight:600;")

    # ── приём записей из ядра ──
    def _on_entry_from_core(self, entry):
        self._entry_arrived.emit(entry)

    def _add_row_ui(self, entry):
        import time as _time
        self._last_entry_ts = _time.monotonic()
        if self._paused:
            self._rows.append(entry)
            if len(self._rows) > MAX_BUFFER:
                self._rows = self._rows[-MAX_BUFFER:]
            return
        self._rows.append(entry)
        if len(self._rows) > MAX_BUFFER:
            self._rows = self._rows[-MAX_BUFFER:]
        # если проходит фильтр — обновляем canvas (виртуально, без перестроения всех виджетов)
        if self._passes_filter(entry):
            # canvas рисует только видимые, поэтому просто пересобираем отфильтрованный список
            # оптимизация: инкрементально добавлять, но проще пересобрать (быстро)
            self._update_canvas()
        self._update_stats()

    # ── фильтры ──
    def _passes_filter(self, entry) -> bool:
        if getattr(entry, "source", "") == "bg-refresh":
            return False
        if self._filter == "routed" and not getattr(entry, "routed", False):
            return False
        source = getattr(entry, "source", "")
        if self._filter == "direct" and (getattr(entry, "routed", False) or source in ("fixed", "error")):
            return False
        if self._filter == "blocked" and source not in ("blocked", "bogus-NX"):
            return False
        if self._filter == "fixed" and source != "fixed":
            return False
        if self._filter == "errors" and source not in ("servfail", "bogus-NX", "blocked", "error", "leak"):
            return False
        if self._filter == "cache" and source not in ("cache", "stale-cache"):
            return False
        if self._search:
            hay = " ".join([
                getattr(entry, "domain", ""), getattr(entry, "qtype", ""),
                getattr(entry, "source", ""), getattr(entry, "rcode", ""),
                getattr(entry, "note", ""), ",".join(getattr(entry, "answers", []) or []),
            ]).lower()
            if self._search not in hay:
                return False
        return True

    def _set_filter(self, key: str):
        self._filter = key
        for k, chip in self._chips.items():
            chip.setStyleSheet(self._chip_qss(k == key))
        self._rebuild()

    def set_filter(self, key: str):
        """Публичный выбор категории (фильтра) из других вкладок.

        Например «fixed» — «🔧 Починки» (лечение): кнопка «📋 Лог» в виджете
        автодиагностики ведёт сюда сразу с этой категорией (2026-10-06).
        """
        self._set_filter(key)

    def _on_search(self, text: str):
        self._search = (text or "").lower().strip()
        self._rebuild()

    # ── пауза ──
    def _toggle_pause(self):
        self._paused = not self._paused
        try:
            self.qlog.set_paused(self._paused)
        except Exception:
            pass
        if self._paused:
            self._btn_pause.set_label("▶", "Продолжить")
            self._live.setText("⏸  ПАУЗА")
            self._live.setStyleSheet(f"color:{theme.YELLOW};font-size:12px;font-weight:700;")
        else:
            self._btn_pause.set_label("⏸", "Пауза")
            self._live.setText("●  LIVE")
            self._live.setStyleSheet(f"color:{theme.GREEN};font-size:12px;font-weight:700;")
            self._sync_from_core()
            self._rebuild()

    # ── очистка ──
    def _clear(self):
        try:
            self.qlog.clear()
        except Exception:
            pass
        self._rows.clear()
        self._update_canvas()
        self._update_stats()

    # ── canvas ──
    def _filtered_entries(self):
        # новейшие сверху — как раньше reversed
        shown = [e for e in reversed(self._rows) if self._passes_filter(e)][:MAX_VISIBLE]
        return shown

    def _update_canvas(self):
        shown = self._filtered_entries()
        self._canvas.set_entries(shown)

    def _rebuild(self):
        self._update_canvas()
        self._update_stats()

    def _update_stats(self):
        visible = [e for e in self._rows if getattr(e, "source", "") != "bg-refresh"]
        total   = len(visible)
        routed  = sum(1 for e in visible if getattr(e, "routed", False))
        blocked = sum(1 for e in visible if getattr(e, "source", "") in ("bogus-NX", "blocked"))
        fixed   = sum(1 for e in visible if getattr(e, "source", "") == "fixed")
        errors  = sum(1 for e in visible if getattr(e, "source", "") in ("error", "servfail", "leak"))
        direct  = max(0, total - routed - fixed - errors - blocked)
        self._stat_total._value.setText(str(total))
        self._stat_routed._value.setText(str(routed))
        self._stat_direct._value.setText(str(direct))
        self._stat_blocked._value.setText(str(blocked))
        self._stat_fixed._value.setText(str(fixed))
        self._stat_error._value.setText(str(errors))
        # Цифры стали длиннее/короче — значит изменилась и требуемая ширина
        # карточек: пересчитываем ужим, иначе подпись могла бы упереться в рамку.
        self._apply_layout_mode(force=True)

    # ── контекстное меню для canvas ──
    def _show_canvas_menu(self, idx: int, global_pos):
        entries = self._canvas.entries()
        if not (0 <= idx < len(entries)):
            return
        entry = entries[idx]
        domain = getattr(entry, "domain", "") or ""
        if not domain:
            return
        import webbrowser

        from umbranet.engine_adapter import (
            get_engine,
            is_domain_allowed,
            is_domain_blocked,
            is_domain_routed,
        )
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: {theme.CARD}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 20px 6px 12px; border-radius: 4px; font-size: 12px; }}"
            f"QMenu::item:selected {{ background-color: {theme.ACCENT}; color: {theme.WHITE}; }}"
            f"QMenu::item:disabled {{ color: {theme.MUTED}; }}"
        )
        eng = get_engine()
        cfg = eng.config
        is_routed = is_domain_routed(domain, cfg)
        is_blocked = is_domain_blocked(domain, cfg)
        is_allowed = is_domain_allowed(domain, cfg)

        act_route = QAction("➕ Добавить в обход", self)
        if is_routed:
            act_route.setText("✓ Уже в обходе")
            act_route.setEnabled(False)
        else:
            act_route.triggered.connect(lambda: self._add_to_bypass(domain))
        act_block = QAction("⛔ Заблокировать домен", self)
        if is_blocked:
            act_block.setText("✅ Убрать из блоклиста")
            act_block.triggered.connect(lambda: self._unblock_domain(domain))
        else:
            act_block.triggered.connect(lambda: self._block_domain(domain))
        act_allow = QAction("🟢 Добавить в allowlist", self)
        if is_allowed:
            act_allow.setText("✅ Убрать из allowlist")
            act_allow.triggered.connect(lambda: self._unallow_domain(domain))
        else:
            act_allow.triggered.connect(lambda: self._allow_domain(domain))
        act_copy = QAction("📋 Скопировать домен", self)
        act_copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(domain))
        act_copy_row = QAction("📋 Скопировать строку", self)
        act_copy_row.triggered.connect(lambda: self._copy_row(entry))
        act_open = QAction("🌐 Открыть в браузере", self)
        act_open.triggered.connect(lambda: webbrowser.open(f"https://{domain}"))

        menu.addAction(act_route)
        menu.addAction(act_block)
        menu.addAction(act_allow)
        menu.addSeparator()
        menu.addAction(act_copy)
        menu.addAction(act_copy_row)
        menu.addAction(act_open)
        menu.exec(global_pos)

    def _add_to_bypass(self, domain):
        from umbranet.engine_adapter import get_engine, save_config
        eng = get_engine()
        cfg = eng.config
        routed = cfg.setdefault("routed_domains", [])
        if domain and domain not in routed:
            routed.append(domain)
            save_config(cfg)
            eng.reload_config()
            self._rebuild()

    def _block_domain(self, domain):
        from umbranet.engine_adapter import block_domain
        if block_domain(domain):
            self._rebuild()

    def _unblock_domain(self, domain):
        from umbranet.engine_adapter import unblock_domain
        if unblock_domain(domain):
            self._rebuild()

    def _allow_domain(self, domain):
        from umbranet.engine_adapter import allow_domain
        if allow_domain(domain):
            self._rebuild()

    def _unallow_domain(self, domain):
        from umbranet.engine_adapter import unallow_domain
        if unallow_domain(domain):
            self._rebuild()

    def _copy_row(self, entry):
        ts = time.strftime("%H:%M:%S", time.localtime(getattr(entry, "timestamp", 0)))
        text = (
            f"{ts}\t{getattr(entry, 'domain', '')}\t{getattr(entry, 'qtype', '')}\t"
            f"{SOURCE_LABELS.get(getattr(entry, 'source', ''), getattr(entry, 'source', ''))}\t"
            f"{getattr(entry, 'rcode', '')}\t{getattr(entry, 'latency_ms', '')} мс\t"
            f"{getattr(entry, 'note', '')}"
        )
        QGuiApplication.clipboard().setText(text)

    # ── синхронизация с ядром ──
    def _sync_from_core(self):
        try:
            snap = self.qlog.snapshot()
        except Exception:
            return
        if not snap:
            return
        last_ts = getattr(self._rows[-1], "timestamp", 0) if self._rows else 0
        new = [e for e in snap if getattr(e, "timestamp", 0) > last_ts]
        for e in new:
            self._rows.append(e)
        if len(self._rows) > MAX_BUFFER:
            self._rows = self._rows[-MAX_BUFFER:]

    def _export_clipboard(self):
        lines = ["Время\tДомен\tТип\tМаршрут\tИсточник\tПричина\tмс"]
        for e in self._rows:
            if not self._passes_filter(e):
                continue
            ts = time.strftime("%H:%M:%S", time.localtime(getattr(e, "timestamp", 0)))
            domain = getattr(e, "domain", "")
            qtype = getattr(e, "qtype", "")
            routed = "обход" if getattr(e, "routed", False) else "напрямую"
            source = SOURCE_LABELS.get(getattr(e, "source", ""), getattr(e, "source", ""))
            reason = getattr(e, "note", "") or _reason_for(e)
            latency = str(getattr(e, "latency_ms", 0) or "")
            lines.append(f"{ts}\t{domain}\t{qtype}\t{routed}\t{source}\t{reason}\t{latency}")
        QGuiApplication.clipboard().setText("\n".join(lines))

    def refresh(self):
        self._sync_from_core()
        # если ещё пусто — rebuild покажет empty, иначе обновит canvas/статы
        if not self._rows:
            self._rebuild()
        else:
            self._update_stats()
            # canvas уже обновляется через _add_row_ui / _rebuild, но на вход на вкладку
            # надо убедиться что фильтр применён
            self._update_canvas()
