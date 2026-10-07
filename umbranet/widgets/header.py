"""
UmbraNet - верхняя панель контента (PySide6).

Содержит:
  • ModeSwitch — три режима DPI (DNS Only / Combo / DPI Only);
  • ControlBar  — индикатор статуса + Start/Stop/Restart.

Сигналы:
  ModeSwitch.modeChanged(ui_key)
  ControlBar.startClicked / stopClicked / restartClicked
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from umbranet import theme
from umbranet.widgets.glow_cache import paint_widget_glow

# Описания транспортов — используются TransportHelpDialog в dialogs.py
_TRANSPORT_HELP = {
    "udp": ("UDP DNS",
            "Обычный DNS по UDP/53. Самый быстрый, но провайдер видит запросы "
            "открытым текстом и может их подменять. Подходит, если блокировок "
            "по DNS нет."),
    "doh": ("DoH — DNS over HTTPS",
            "DNS-запросы прячутся внутрь обычного HTTPS (порт 443). Провайдер "
            "видит только «соединение с сайтом», не сами запросы. Лучший выбор "
            "против DNS-подмены. Используется по умолчанию."),
    "dot": ("DoT — DNS over TLS",
            "DNS внутри TLS (порт 853). Шифрует запросы, но порт 853 заметен и "
            "его иногда режут. Альтернатива DoH."),
    "doq": ("DoQ — DNS over QUIC",
            "DNS поверх QUIC (UDP/853). Быстрее DoT за счёт QUIC, шифрование как "
            "у DoH/DoT. Требует пакет aioquic."),
    "dnscrypt": ("DNSCrypt",
                 "Шифрованный протокол со штампом sdns://. Прячет и проверяет "
                 "подлинность ответов. Требует пакет pynacl и sdns-штамп в "
                 "активном профиле."),
}

# QWIDGETSIZE_MAX в PySide6 не экспортируется — значение то же.
# Нужно, чтобы после явного setFixedWidth вернуть кнопке ширину по содержимому.
_WIDGET_MAX = 16777215

# Высота кнопки power и половина — радиус для пиллюли
_BTN_H  = 40
_BTN_R  = _BTN_H // 2   # 20px — настоящая пиллюля, углов не видно


def _qss_label(color: str, size: int) -> str:
    """Цвет текста без собственной заливки — иначе у статуса виден чужой фон."""
    return (
        f"color:{color};font-size:{size}px;"
        "background:transparent;border:none;"
    )


def _power_qss(bg1: str, bg2: str,
               hover1: str, hover2: str,
               pressed1: str, pressed2: str,
               fg: str | None = None) -> str:
    """
    QSS для кнопки btn_power.

    Почему border-radius = половина высоты:
      При border-radius < 50% высоты кнопки углы видны как скруглённые прямоугольники
      (именно это и выглядело «странно»). При radius = height/2 получается
      настоящая капсула/пиллюля без видимых углов.

    Почему НЕТ QGraphicsDropShadowEffect (glow):
      Qt рисует glow по прямоугольному bounding box виджета, игнорируя border-radius
      из QSS. Итог — квадратные светящиеся углы поверх скруглённой кнопки.
      Вместо glow используем подсветку через border в :hover/:pressed.
    """
    r = f"{_BTN_R}px"
    return (
        f"QPushButton{{"
        f"  background: {theme.grad(bg1, bg2)};"
        f"  color: {fg or theme.text_on_color(bg1)};"
        "  border: none;"
        f"  border-radius: {r};"
        "  font-size: 13px;"
        "  font-weight: 600;"
        "  padding: 0 20px;"
        "}"
        f"QPushButton:hover{{"
        f"  background: {theme.grad(hover1, hover2)};"
        "  border: none;"
        f"  border-radius: {r};"
        "}"
        f"QPushButton:pressed{{"
        f"  background: {theme.grad(pressed1, pressed2)};"
        "  border: none;"
        f"  border-radius: {r};"
        "}"
        f"QPushButton:disabled{{"
        f"  background: {theme.CARD};"
        f"  color: {theme.MUTED};"
        f"  border: 1px solid {theme.BORDER};"
        f"  border-radius: {r};"
        "}"
    )


def _idle_qss() -> str:
    """QSS для btn_power в состоянии «занято» (set_busy)."""
    r = f"{_BTN_R}px"
    return (
        f"QPushButton{{"
        f"  background: {theme.CARD};"
        f"  color: {theme.MUTED};"
        f"  border: 1px solid {theme.BORDER};"
        f"  border-radius: {r};"
        "  font-size: 13px;"
        "  font-weight: 600;"
        "  padding: 0 20px;"
        "}"
    )


class ModeSwitch(QFrame):
    modeChanged = Signal(str)   # ui_key: blue/black/red

    def __init__(self, active: str = "blue"):
        super().__init__()
        self.active = active
        self._buttons: dict[str, QPushButton] = {}

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._compact = False
        #: Степень ужима кнопок: 0.0 — полные подписи, 1.0 — только значки.
        #: Меняется плавно вслед за шириной окна (set_compression).
        self._t = 0.0
        #: Ширины кнопок с полной подписью, как их посчитал сам Qt (с учётом QSS).
        #: Заполняется перед первым ужимом: шрифтовые метрики врут на 10-15 px, и
        #: без этого на входе в ужим кнопки дёргались бы на эту разницу.
        self._natural: dict[str, int] = {}
        for key, m in theme.MODES.items():
            btn = QPushButton(self.button_label(key))
            # Не используем Qt tooltip на кнопках режима: при быстром клике
            # DNS Only ↔ Combo Qt успевает показать маленькое всплывающее окно,
            # которое выглядит как баг/мигание. Описание режимов оставляем в
            # документации/«О программе», а сами кнопки должны переключаться
            # без всплывающих окон.
            btn.setToolTip("")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setFixedHeight(_BTN_H)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            self._buttons[key] = btn
            lay.addWidget(btn)

        self._restyle()

    # Отступы кнопки из QSS (padding:0 12px) — нужны, чтобы посчитать ширину
    # до фактической раскладки, не трогая текст кнопок.
    _BTN_PADDING = 24

    def _text_px(self, text: str, pixel_size: int = 13) -> int:
        """Ширина текста в пикселях кнопки (в QSS у кнопок font-size:13px)."""
        metrics = getattr(self, "_metrics", None)
        if metrics is None:
            font = QFont(self.font())
            font.setPixelSize(pixel_size)
            metrics = QFontMetrics(font)
            self._metrics = metrics
        return metrics.horizontalAdvance(text)

    def button_label(self, key: str) -> str:
        """Подпись кнопки режима: с названием или только значок (в ужиме)."""
        m = theme.MODES[key]
        return m["emoji"] if getattr(self, "_compact", False) else f"{m['emoji']}  {m['name']}"

    def _btn_track(self, key: str) -> tuple[int, int]:
        """(ширина кнопки с подписью, ширина кнопки со значком) в пикселях."""
        m = theme.MODES[key]
        full = self._text_px(f"{m['emoji']}  {m['name']}") + self._BTN_PADDING
        icons = self._text_px(m["emoji"]) + self._BTN_PADDING
        return full, icons

    def _capture_natural(self):
        """Запоминает ширины кнопок, как их посчитал сам Qt (с QSS и эмодзи).

        Метрики шрифта на эмодзи и на отступах из QSS врут (замерено: 301 против
        фактических 315 px на трёх кнопках режимов). Из-за этого шапка считала,
        что помещается, и раскладка обрезала крайнюю кнопку — как раз «Старт».
        Точные числа можно снять только в полном виде (t = 0): тогда кнопки не
        сжаты и sizeHint равен естественной ширине.
        """
        if self._t > 0.0:
            return
        for key, btn in self._buttons.items():
            self._natural[key] = btn.sizeHint().width()

    def required_widths(self) -> dict:
        """Сколько места нужно переключателю: {"full": с подписями, "icons": только значки}.

        Нужно главному окну, чтобы решить, когда ужимать подписи, НЕ перестраивая
        шапку в два ряда: кнопки не должны уезжать вверх при уменьшении окна.
        """
        self._capture_natural()
        gaps = 6 * max(0, len(self._buttons) - 1)   # spacing раскладки режимов
        full = icons = gaps
        for key in self._buttons:
            f, i = self._btn_track(key)
            full += self._natural.get(key, f)       # точная ширина, если снята
            icons += i
        return {"full": full, "icons": icons}

    def compression(self) -> float:
        """Текущая степень ужима кнопок режимов: 0.0 — подписи, 1.0 — значки."""
        return self._t

    #: Минимум места под саму подпись, при котором она ещё читается («DNS…»).
    _MIN_LABEL_PX = 26

    def set_compression(self, t: float) -> bool:
        """Плавно ужимает кнопки режимов под ширину окна.

        t = 0.0 — полные подписи, t = 1.0 — только значки. Промежуточные значения
        главное окно считает по фактической ширине шапки, поэтому кнопки едут за
        размером окна, а не переключаются скачком (жалоба пользователя: «в
        определённый момент сразу становятся мелкими… дергается»).

        Чтобы ужать кнопку МЕЖДУ полной подписью и значком, подпись укорачивается
        по мере ужима: «DNS Only» → «DNS O…» → «DNS…» → значок. Ширина кнопки
        задаётся явно и меняется на каждом шаге на считаные пиксели, так что
        рывка в раскладке нет ни в один момент.

        Возвращает True, если что-то изменилось.
        """
        t = 0.0 if t <= 0 else (1.0 if t >= 1 else float(t))
        if abs(t - self._t) < 0.004:
            return False
        if self._t <= 0.0 and t > 0.0:
            # Первый шаг ужима: запоминаем ширины, которые кнопки занимали с
            # полными подписями. Считает их Qt (с QSS-отступами), поэтому переход
            # «подписи → ужим» не даёт скачка ни на пиксель.
            for key, btn in self._buttons.items():
                self._natural[key] = btn.sizeHint().width()
        self._t = t
        for key, btn in self._buttons.items():
            m = theme.MODES[key]
            full_label = f"{m['emoji']}  {m['name']}"
            full_w, icon_w = self._btn_track(key)
            full_w = self._natural.get(key, full_w)
            if t <= 0.0:
                # Как было: ширина по содержимому, подпись целиком.
                btn.setText(full_label)
                btn.setMinimumWidth(0)
                btn.setMaximumWidth(_WIDGET_MAX)
                btn.setToolTip("")
                continue
            width = max(icon_w, round(full_w + (icon_w - full_w) * t))
            btn.setFixedWidth(width)
            room = width - self._BTN_PADDING - self._text_px(m["emoji"]) - self._text_px("  ")
            if room >= self._text_px(m["name"]):
                btn.setText(full_label)
                btn.setToolTip("")
            elif room >= self._MIN_LABEL_PX:
                btn.setText(f"{m['emoji']}  "
                            f"{self._metrics.elidedText(m['name'], Qt.ElideRight, room)}")
                # Название режима переезжает в подсказку ровно тогда, когда
                # подпись перестала помещаться целиком.
                btn.setToolTip(m["name"])
            else:
                btn.setText(m["emoji"])
                btn.setToolTip(m["name"])
        self._compact = any(
            btn.text() != f"{theme.MODES[key]['emoji']}  {theme.MODES[key]['name']}"
            for key, btn in self._buttons.items()
        )
        self.updateGeometry()
        self.update()
        return True

    def set_compact(self, compact: bool) -> bool:
        """Крайние состояния ужима: True — только значки, False — полные подписи.

        Оставлено для совместимости; промежуточные значения задаёт
        `set_compression` (именно им пользуется главное окно при изменении размера).
        """
        return self.set_compression(1.0 if compact else 0.0)

    def set_active(self, key: str):
        if key in self._buttons:
            self.active = key
            self._restyle()

    def paintEvent(self, event):
        """Кэшированное свечение под активной кнопкой режима.

        Рисуем ПОСЛЕ super() и ДО детей (кнопки рисуются поверх) — вид 1:1
        с прежним живым QGraphicsDropShadowEffect, но гауссов blur больше
        не пересчитывается на каждый кадр resize.
        """
        super().paintEvent(event)
        btn = self._buttons.get(self.active)
        if btn is not None and btn.isVisible():
            m = theme.MODES[self.active]
            p = QPainter(self)
            paint_widget_glow(p, btn, m["c1"], blur=16, dy=4, alpha=150,
                              host=self, clip_widget=False, radius=12)

    def _select(self, key: str):
        if key == self.active:
            self._buttons[key].setChecked(True)
            return
        self.active = key
        self._restyle()
        self.modeChanged.emit(key)

    def _restyle(self):
        r = "12px"   # ModeSwitch кнопки — скруглённые прямоугольники, не пиллюли
        for key, btn in self._buttons.items():
            m = theme.MODES[key]
            is_active = key == self.active
            btn.setChecked(is_active)
            # Всегда сбрасываем glow перед применением стиля
            btn.setGraphicsEffect(None)
            if is_active:
                btn.setStyleSheet(
                    f"QPushButton{{"
                    f"  background:{theme.grad(m['c1'], m['c2'])};"
                    f"  color:{theme.WHITE};border:none;border-radius:{r};"
                    "  font-size:13px;font-weight:600;padding:0 12px;"
                    "}"
                    f"QPushButton:hover{{border-radius:{r};}}"
                    f"QPushButton:pressed{{border-radius:{r};}}"
                )
                # Свечение активной кнопки рисует paintEvent ModeSwitch —
                # кэш-pixmap вместо живого QGraphicsDropShadowEffect
            else:
                btn.setStyleSheet(
                    f"QPushButton{{"
                    f"  background:{theme.CARD};color:{theme.SUBTEXT};"
                    f"  border:1px solid {theme.BORDER};border-radius:{r};"
                    "  font-size:13px;padding:0 12px;"
                    "}"
                    f"QPushButton:hover{{"
                    f"  color:{theme.TEXT};border-color:{m['c1']};border-radius:{r};"
                    "}"
                    f"QPushButton:pressed{{border-radius:{r};}}"
                )
        self.update()


class ControlBar(QFrame):
    startClicked   = Signal()
    stopClicked    = Signal()
    restartClicked = Signal()

    def __init__(self, running: bool = False, mode: str = "dns_only"):
        super().__init__()
        self._running = running
        self._can_start = True  # C1: Старт заблокирован без целей
        self.setFrameShape(QFrame.NoFrame)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background:transparent;border:none;")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # индикатор статуса.
        # ВНИМАНИЕ: растяжку здесь НЕ ставить — она отрывала плашку статуса
        # от кнопок при широком окне (статус улетал влево). Всё свободное
        # место поглощает ОБЩАЯ растяжка шапки (row.addStretch() в app.py,
        # между ModeSwitch и ControlBar): статус, «Перезапуск» и «Старт» —
        # единый прижатый к правому краю блок, как в шапке Телеграма.
        self._dot = QLabel("●")
        self._dot.setAutoFillBackground(False)
        self._dot.setAttribute(Qt.WA_TranslucentBackground, True)
        self._dot.setStyleSheet(_qss_label(theme.MUTED, 14))
        self._status = QLabel("Остановлен")
        self._status.setAutoFillBackground(False)
        self._status.setAttribute(Qt.WA_TranslucentBackground, True)
        self._status.setStyleSheet(_qss_label(theme.SUBTEXT, 13))
        lay.addWidget(self._dot)
        lay.addWidget(self._status)

        # кнопка Перезапуск
        r_r = "12px"
        self.btn_restart = QPushButton("↻  Перезапуск")
        self.btn_restart.setCursor(Qt.PointingHandCursor)
        self.btn_restart.setFixedHeight(_BTN_H)
        self.btn_restart.setStyleSheet(
            f"QPushButton{{"
            f"  background:{theme.grad(theme.ORANGE, theme.YELLOW)};"
            f"  color:{theme.text_on_color(theme.ORANGE)};"
            f"  border:none;border-radius:{r_r};"
            "  font-size:13px;font-weight:600;padding:0 12px;"
            "}"
            f"QPushButton:hover{{border-radius:{r_r};}}"
            f"QPushButton:pressed{{border-radius:{r_r};}}"
            f"QPushButton:disabled{{"
            f"  background:{theme.CARD};color:{theme.MUTED};"
            f"  border:1px solid {theme.BORDER};border-radius:{r_r};"
            "}"
        )
        self.btn_restart.clicked.connect(self.restartClicked.emit)
        lay.addWidget(self.btn_restart)

        # кнопка Старт / Стоп (пиллюля)
        self.btn_power = QPushButton()
        self.btn_power.setCursor(Qt.PointingHandCursor)
        self.btn_power.setFixedHeight(_BTN_H)
        self.btn_power.setMinimumWidth(140)
        self.btn_power.clicked.connect(self._on_power)
        lay.addWidget(self.btn_power)
        #: Полная подпись кнопки Старт/Стоп. Все состояния (запуск, стоп, генерация)
        #: пишут СЮДА, а показ подгоняет _render_power(): в узком окне подпись
        #: укорачивается и в пределе остаётся один значок (см. set_compression).
        self._power_text = "▶  Старт"
        self._power_t = 0.0
        #: Точные ширины полных подписей (снимаются у самой кнопки, см. _power_widths).
        self._power_naturals: dict[str, int] = {}
        self._natural_restart = 0

        self.set_running(running, mode=mode)

    def _on_power(self):
        if self._running:
            self.stopClicked.emit()
        else:
            self.startClicked.emit()

    # ── ужимание строки без переносов ────────────────────────────────────────
    # Пользователь: при уменьшении окна ничего не должно уезжать вверх/вниз, а
    # при нехватке места подпись должна уступать место значку. Здесь это и живёт:
    #   1) сначала прячется ТЕКСТ статуса (точка-индикатор остаётся — по ней видно
    #      цвет состояния, а сам текст переезжает в подсказку точки);
    #   2) в самом узком окне «Перезапуск» остаётся со значком ↻ (кнопка Старт /
    #      Стоп подпись сохраняет всегда: это главное действие, его не размываем).

    _BTN_PADDING = 24
    _POWER_MIN_W = 140          # как в __init__: минимальная ширина Старт/Стоп
    #: Минимум места под читаемую часть слова. У режимов это 26 px («DNS…»), но у
    #: «Старт/Стоп» одна буква с многоточием («С…») ничего не объясняет: лучше
    #: показать значок ▶, который однозначен. Поэтому порог выше — хватает на
    #: «Ст…».
    _MIN_LABEL_PX = 42

    def _text_px(self, text: str, pixel_size: int = 13) -> int:
        metrics = getattr(self, "_metrics", None)
        if metrics is None:
            font = QFont(self.font())
            font.setPixelSize(pixel_size)
            metrics = QFontMetrics(font)
            self._metrics = metrics
        return metrics.horizontalAdvance(text)

    def _measure_status(self) -> int:
        """Ширина текста статуса, как её видит сам Qt (QLabel знает свой текст)."""
        try:
            return int(self._status.sizeHint().width())
        except Exception:                              # noqa: BLE001
            return self._text_px(self._status.text())

    def _measure_restart_full(self) -> int:
        """Ширина «Перезапуска» с подписью.

        Снимается у самой кнопки: она считает ширину с учётом QSS-отступов и
        значка ↻, а метрики шрифта на этом ошибались на десятки пикселей.
        """
        natural = getattr(self, "_natural_restart", 0)
        if natural:
            return natural
        return self._text_px("↻  Перезапуск") + self._BTN_PADDING

    def required_widths(self) -> dict:
        """Варианты ширины строки статуса и кнопок (px).

        Строка раскладывается в один ряд: точка, статус, «Перезапуск», «Старт».
        Варианты — по порядку уступок при сужении окна:

          full         — статус с текстом, «Перезапуск» и «Старт» с подписями;
          no_status    — только точка статуса (место под текст статуса не нужно);
          restart_icon — плюс «Перезапуск» с одним значком ↻;
          power_icon   — плюс «Старт/Стоп» с одним значком ▶ (последняя уступка).

        Между restart_icon и power_icon кнопка «Старт» ужимается ПЛАВНО: главное
        окно считает степень ужима по фактической ширине (set_compression).
        """
        gap = 10                                   # spacing раскладки
        dot = max(int(self._dot.sizeHint().width()), 14)
        status = self._measure_status()
        restart_full = self._measure_restart_full()
        restart_icon = self._text_px("↻") + self._BTN_PADDING
        power_full, power_icon_w = self._power_widths()
        return {
            "full": dot + gap + status + gap + restart_full + gap + power_full,
            "no_status": dot + gap + restart_full + gap + power_full,
            "restart_icon": dot + gap + restart_icon + gap + power_full,
            "power_icon": dot + gap + restart_icon + gap + power_icon_w,
        }

    # ── ужимание кнопки Старт/Стоп ───────────────────────────────────────────
    # Пользователь: в узком окне кнопку Старт «съедает» край окна. Нужно, чтобы
    # она ужималась так же, как соседний «Перезапуск»: сначала подпись короче,
    # в пределе — один значок. Смысл кнопки при этом не теряется: значок ▶ / ⏹
    # однозначен, а полная подпись и действие описаны в подсказке.

    _POWER_ICON_PAD = 40        # QSS-отступы power-кнопки: padding 0 20px

    def power_label_parts(self) -> tuple[str, str]:
        """(значок, слово) полной подписи: «▶  Старт» → («▶», «Старт»)."""
        text = self._power_text or ""
        head, sep, tail = text.partition("  ")
        if not sep:
            return text.strip(), ""
        return head.strip(), tail.strip()

    def _power_widths(self) -> tuple[int, int]:
        """(ширина с полной подписью, ширина со значком) в пикселях.

        Полная ширина снимается у самой кнопки (Qt считает её вместе с QSS и
        значком) и запоминается для каждой подписи: «▶  Старт», «⏹  Стоп» и
        «🧪  Генерация...» разной длины. Ширина со значком — наша: кнопка
        получает явную ширину, значит её и указываем.
        """
        icon, word = self.power_label_parts()
        measured = self._power_naturals.get(self._power_text)
        # ВАЖНО: у кнопки Старт/Стоп есть минимальная ширина (140 px) — в полном
        # виде она занимает именно её, а не ширину подписи. Без max(...) расчёт
        # «сколько нужно шапке» занижался на ~40 px, и раскладка обрезала крайнюю
        # кнопку — та самая жалоба «Старт съедает край окна».
        full = max(self._POWER_MIN_W,
                   measured or (self._text_px(self._power_text) + self._POWER_ICON_PAD))
        icons = max(_BTN_H, self._text_px(icon) + self._POWER_ICON_PAD)
        if not word:
            full = icons
        return full, icons

    def power_compression(self) -> float:
        """Текущая степень ужима кнопки Старт: 0.0 — полная подпись, 1.0 — значок."""
        return self._power_t

    def _render_power(self) -> None:
        """Показывает подпись кнопки с учётом текущего ужима."""
        icon, word = self.power_label_parts()
        t = self._power_t
        if t <= 0.0 or not word:
            self.btn_power.setText(self._power_text)
            # Подпись показана целиком — снимаем её точную ширину у кнопки.
            self._power_naturals[self._power_text] = self.btn_power.sizeHint().width()
            self.btn_power.setMinimumWidth(self._POWER_MIN_W)
            self.btn_power.setMaximumWidth(_WIDGET_MAX)
            self.btn_power.setToolTip("")
            return

        full_w, icon_w = self._power_widths()
        width = max(icon_w, round(full_w + (icon_w - full_w) * t))
        self.btn_power.setMinimumWidth(0)
        self.btn_power.setFixedWidth(width)

        room = width - self._POWER_ICON_PAD - self._text_px(icon) - self._text_px("  ")
        if room >= self._text_px(word):
            self.btn_power.setText(self._power_text)
        elif room >= self._MIN_LABEL_PX:
            self.btn_power.setText(f"{icon}  {self._metrics.elidedText(word, Qt.ElideRight, room)}")
        else:
            self.btn_power.setText(icon)
        # Полное название действия — в подсказке: в ужатом виде подписи нет.
        self.btn_power.setToolTip(word)

    def set_compression(self, t: float) -> bool:
        """Плавно ужимает кнопку Старт/Стоп. True — что-то изменилось.

        t = 0.0 — полная подпись, t = 1.0 — только значок. Промежуточные значения
        считает главное окно по фактической ширине шапки, поэтому кнопка едет за
        размером окна, а не переключается скачком (как кнопки режимов).
        """
        t = 0.0 if t <= 0 else (1.0 if t >= 1 else float(t))
        if abs(t - self._power_t) < 0.004:
            # Ужатая кнопка ОБЯЗАНА иметь подсказку: промежуточный рендер при
            # t=0 мог снять её, а состояние уже успело «осесть» на том же t
            # (например, после прохода раскладки) — иначе пользователь видит
            # ужатый значок без объяснения, что это.
            if t > 0.0 and not self.btn_power.toolTip():
                self._render_power()
                return True
            return False
        self._power_t = t
        self._render_power()
        self.updateGeometry()
        return True

    def set_compact(self, level: str) -> bool:
        """Ужимает строку до уровня "full" / "no_status" / "restart_icon".

        Возвращает True, если что-то изменилось. Раскладка при этом остаётся
        ОДНОЙ строкой — кнопки никуда не переезжают, меняется только ширина.
        """
        level = level if level in ("full", "no_status", "restart_icon") else "full"
        if level == getattr(self, "_compact_level", None):
            return False
        self._compact_level = level
        # Статус: в узком окне прячем только ТЕКСТ, точка остаётся видимой.
        self._status.setVisible(level == "full")
        self._status_text_hidden = level != "full"
        # Перезапуск: подпись исчезает, значок и действие остаются.
        restart_icon_only = level == "restart_icon"
        self.btn_restart.setText("↻" if restart_icon_only else "↻  Перезапуск")
        if not restart_icon_only:
            # Полная подпись на месте — можно снять точную ширину у самой кнопки.
            self._natural_restart = self.btn_restart.sizeHint().width()
        self.btn_restart.setMinimumWidth(0)
        if restart_icon_only:
            self.btn_restart.setToolTip("Перезапуск")
        else:
            self.btn_restart.setToolTip("")
        self.updateGeometry()
        return True

    # ── публичные методы ──────────────────────────────────────────────────────

    def set_can_start(self, can: bool):
        """Делает кнопку Старт серой если нет выбранных сервисов."""
        self._can_start = bool(can)
        if not getattr(self, "_running", False):
            # Только когда остановлен — Старт может быть серым
            if not self._can_start:
                self.btn_power.setEnabled(False)
                self.btn_power.setToolTip("Выберите хотя бы один сервис в «Маршрутизация» → включите тумблер или добавьте домен")
                self._dot.setStyleSheet(_qss_label(theme.YELLOW, 14))
                self._status.setText("Выберите сервис в «Маршрутизация»")
                self._status.setStyleSheet(_qss_label(theme.YELLOW, 13))
            else:
                self.btn_power.setEnabled(True)
                self.btn_power.setToolTip("")
                # Вернём нейтральный статус если был жёлтый из-за пустого списка
                if self._status.text() == "Выберите сервис в «Маршрутизация»":
                    self._dot.setStyleSheet(_qss_label(theme.MUTED, 14))
                    self._status.setText("Остановлен")
                    self._status.setStyleSheet(_qss_label(theme.SUBTEXT, 13))

    def _sync_status_tooltip(self):
        """Подсказка точки-индикатора повторяет текст статуса.

        Нужна, когда окно узкое и текст статуса спрятан: состояние («DNS запущен»,
        «Выберите сервис…») всё равно можно прочитать, наведя курсор на точку.
        """
        try:
            self._dot.setToolTip(self._status.text())
        except Exception:
            pass

    def set_running(self, running: bool, mode: str = "dns_only", admin_warn: bool = False):
        self._running = running
        # Убираем любой graphics-эффект — glow не совместим с border-radius QSS
        self.btn_power.setGraphicsEffect(None)

        if running:
            color = theme.YELLOW if admin_warn else theme.GREEN
            self._dot.setStyleSheet(_qss_label(color, 14))

            # Определяем текст статуса в зависимости от режима
            if mode == "dns_only":
                status_text = "DNS запущен"
            elif mode == "combo":
                status_text = "DNS + DPI запущены"
            elif mode == "dpi_only":
                status_text = "DPI запущено"
            else:
                status_text = "Работает"

            self._status.setText(
                "Нужны права администратора" if admin_warn else status_text
            )
            self._status.setStyleSheet(_qss_label(color, 13))
            self._power_text = "⏹  Стоп"
            self._render_power()
            self.btn_power.setStyleSheet(_power_qss(
                bg1=theme.RED, bg2=theme.RED,
                hover1=theme.PINK, hover2=theme.PINK,
                pressed1=theme.RED, pressed2=theme.RED,
                fg=theme.text_on_color(theme.RED),
            ))
            self.btn_restart.setEnabled(True)
            self.btn_power.setEnabled(True)
            self.btn_power.setToolTip("")
        else:
            self._dot.setStyleSheet(_qss_label(theme.MUTED, 14))
            # Если старт запрещён — статус жёлтый, иначе обычный
            can = getattr(self, "_can_start", True)
            if not can:
                self._dot.setStyleSheet(_qss_label(theme.YELLOW, 14))
                self._status.setText("Выберите сервис в «Маршрутизация»")
                self._status.setStyleSheet(_qss_label(theme.YELLOW, 13))
            else:
                self._status.setText("Остановлен")
                self._status.setStyleSheet(_qss_label(theme.SUBTEXT, 13))
            self._power_text = "▶  Старт"
            self._render_power()
            self.btn_power.setStyleSheet(_power_qss(
                bg1=theme.GREEN, bg2=theme.GREEN,
                hover1=theme.ACCENT3, hover2=theme.ACCENT3,
                pressed1=theme.GREEN, pressed2=theme.GREEN,
                fg=theme.text_on_color(theme.GREEN),
            ))
            self.btn_restart.setEnabled(False)
            self.btn_power.setEnabled(bool(can))
            if not can:
                self.btn_power.setToolTip("Выберите хотя бы один сервис в «Маршрутизация» → включите тумблер или добавьте домен")
            else:
                self.btn_power.setToolTip("")
        self._sync_status_tooltip()

    def set_busy(self, action: str):
        """Промежуточный статус пока идёт start/stop/restart."""
        self._running = False
        self.btn_power.setGraphicsEffect(None)
        self.btn_power.setEnabled(False)
        self.btn_restart.setEnabled(False)
        labels = {
            "start":   ("▶  Запуск...",     theme.GREEN,  "Запуск..."),
            "stop":    ("⏹  Остановка...",  theme.YELLOW, "Остановка..."),
            "restart": ("↻  Перезапуск...", theme.ORANGE, "Перезапуск..."),
        }
        btn_text, color, status_text = labels.get(action, ("...", theme.MUTED, "..."))
        self._dot.setStyleSheet(_qss_label(color, 14))
        self._status.setText(status_text)
        self._status.setStyleSheet(_qss_label(color, 13))
        self._power_text = btn_text
        self._render_power()
        self.btn_power.setStyleSheet(_idle_qss())
        self._sync_status_tooltip()

    def set_ai_busy(self):
        """Промежуточный статус controlled AI-генерации."""
        self._running = False
        self.btn_power.setGraphicsEffect(None)
        self.btn_power.setEnabled(False)
        self.btn_restart.setEnabled(False)
        self._dot.setStyleSheet(_qss_label(theme.ACCENT3, 14))
        self._status.setText("AI-генерация...")
        self._status.setStyleSheet(_qss_label(theme.ACCENT3, 13))
        self._power_text = "🧪  Генерация..."
        self._render_power()
        self.btn_power.setStyleSheet(_idle_qss())
        self._sync_status_tooltip()

    def set_error(self, message: str):
        """Показывает ошибку запуска."""
        self.btn_power.setGraphicsEffect(None)
        self._dot.setStyleSheet(_qss_label(theme.RED, 14))
        short = (message[:45] + "…") if len(message) > 45 else message
        self._status.setText(f"Ошибка: {short}")
        self._status.setStyleSheet(_qss_label(theme.RED, 12))
        self.btn_power.setEnabled(True)
        self.btn_restart.setEnabled(False)
        self._sync_status_tooltip()
