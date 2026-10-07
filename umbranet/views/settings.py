"""
UmbraNet - раздел «Настройки» (PySide6).

Перенос окна настроек из старой версии (3 секции), с автосохранением:
  • DNS-сервер: порт, fallback IPv4/IPv6;
  • Поведение: режим разработчика, IPv6-сервер, автозапуск, стратегия upstream;
  • Routed-кэш: пресет режима + кэш/TTL (слайдеры) + optimistic cache.

Всё сохраняется СРАЗУ при изменении (как в DNS-профилях). Пресет режима
показывает только реальные пресеты; «Пользовательский» появляется в подписи
сам, когда значения не совпадают ни с одним пресетом.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QSizePolicy,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    autostart_enabled,
    autostart_set,
    autostart_supported,
    backup_create,
    backup_list,
    backup_load,
    get_developer_mode,
    get_engine,
    get_routed_preset_map,
    parse_domain_lines,
    save_config,
    set_developer_mode,
    set_filter_lists,
    upstream_modes,
)
from umbranet.widgets.dialogs import ThemeRestartDialog
from umbranet.widgets.flow_layout import FlowLayout
from umbranet.widgets.rounded_panel import RoundedPanel
from umbranet.widgets.slider_field import SliderField
from umbranet.widgets.toggle import Toggle

# Стратегии upstream: ключ ядра -> русская подпись
UPSTREAM_LABELS = {
    "parallel": "Параллельно (быстрее)",
    "fastest": "Самый быстрый",
    "sequential": "По очереди",
}

# Поля пресета кэша (по ним определяется «Пользовательский»)
PRESET_FIELDS = ("routed_cache_enabled", "routed_cache_ttl", "routed_reply_ttl",
                 "optimistic_cache_enabled", "stale_cache_ttl")


def _section(title: str) -> tuple[QWidget, QVBoxLayout]:
    # Телеграмизация: RoundedPanel рисуется в paintEvent без QSS-градиента
    # (QFrame + card_qss стоил ~3 мс/кадр на секцию при ресайзе)
    f = RoundedPanel(theme.CARD, theme.BORDER, radius=14)
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 12, 16, 14)
    lay.setSpacing(10)
    t = QLabel(title)
    t.setStyleSheet(f"color:{theme.TEXT};font-size:14px;font-weight:700;background:transparent;border:none;")
    lay.addWidget(t)
    return f, lay


class _NoWheelComboBox(QComboBox):
    """QComboBox без случайного переключения колесом мыши.

    Когда пользователь прокручивает страницу настроек, курсор часто оказывается
    над combo-box. Стандартный QComboBox меняет выбранный пункт от wheelEvent
    даже без клика. Для настроек это опасно, поэтому колесо игнорируем.
    Открытый выпадающий список при этом продолжает работать штатно.
    """

    def wheelEvent(self, event):
        event.ignore()



class _FormLabel(QLabel):
    """Подпись слева от поля: в широком окне ровно `_LABEL_MAX_W`, в узком — ужимается.

    Обычная QLabel берёт ширину по тексту: подписи встали бы «лесенкой» (у «Порта»
    короче, у «Fallback IPv6» длиннее). Здесь размер подсказки ровно 150 px, поэтому
    поля в форме выровнены как раньше, а минимальная ширина (110 px) позволяет
    ужаться, когда места мало.
    """

    def sizeHint(self) -> QSize:
        """Ровно 150 px — как прежний setFixedWidth, а ужимается уже минимум."""
        hint = super().sizeHint()
        return QSize(max(_LABEL_MAX_W, hint.width()), hint.height())


class _HeadBar(QWidget):
    """Заголовок вкладки с кнопками: в узком окне кнопки уходят на вторую строку.

    Было: «Настройки» и три кнопки в одном ряду. При сужении окна кнопки
    сжимались до обрезков («зервная кс», «станови»), а заголовок обрезался
    («Настрой» вместо «Настройки») — и весь ряд держал минимальную ширину вкладки
    в 617 px, хотя места в узком окне 492. Теперь: пока всё помещается — один ряд,
    как раньше; не помещается — кнопки переносятся под заголовок (раскладкой-потоком,
    поэтому и там переносятся по мере необходимости).
    """

    def __init__(self, title: QLabel, status: QLabel, buttons: list, parent=None):
        super().__init__(parent)
        self._title, self._status = title, status
        self._buttons = list(buttons)
        self._narrow = None

        self._host = QVBoxLayout(self)
        self._host.setContentsMargins(0, 0, 0, 0)
        self._host.setSpacing(8)

        # Политика честно говорит, что высота виджета зависит от ширины (внутри
        # раскладка-поток): без этого второй ряд кнопок мог наехать на первую
        # карточку, когда заголовок попадал в чужую раскладку.
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

        self._line = QHBoxLayout()
        self._line.setContentsMargins(0, 0, 0, 0)
        self._line.setSpacing(10)
        self._line.addWidget(title)
        self._line.addStretch(1)
        self._line.addWidget(status)
        self._host.addLayout(self._line)

        self._flow_holder = QWidget()
        self._flow = FlowLayout(spacing=10)
        self._flow_holder.setLayout(self._flow)
        self._host.addWidget(self._flow_holder)

        self._apply_mode(force=True)

    # ── переключение рядов ──
    def _needed_width(self) -> int:
        """Сколько нужно одному ряду: заголовок + статус + все кнопки + зазоры."""
        need = (self._title.sizeHint().width() + self._status.sizeHint().width()
                + sum(b.sizeHint().width() for b in self._buttons)
                + 10 * (len(self._buttons) + 2) + 4)
        return need

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_mode()

    def _apply_mode(self, force: bool = False) -> None:
        narrow = self.width() < self._needed_width()
        if narrow == self._narrow and not force:
            return
        self._narrow = narrow
        target = self._flow if narrow else self._line
        for btn in self._buttons:
            target.addWidget(btn)          # Qt сам убирает виджет из прежней раскладки
        self._flow_holder.setVisible(narrow)
        # Раскладки пересчитали состав — просим Qt перечитать размеры. Без этого
        # высота остаётся от прежнего ряда, и второй ряд кнопок наезжает на карточку.
        self._line.invalidate()
        self._flow.invalidate()
        self._host.invalidate()
        self.updateGeometry()


#: Минимальная высота подписи строки-переключателя (одна строка текста).
_TOGGLE_LABEL_MIN_H = 22
#: Минимальная высота описания секции (одна строка текста).
_DESC_MIN_H = 28
#: Минимальная ширина поля ввода: до неё поле ужимается в узком окне.
_FIELD_MIN_W = 110
#: Ширина подписи слева от поля: предел (как раньше) и минимум в узком окне.
_LABEL_MAX_W = 150
_LABEL_MIN_W = 110


class SettingsView(QWidget):
    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self._suppress = False  # подавление автосейва при программном обновлении
        #: Подписи строк-переключателей по именам (по ним проверяется, что текст
        #: растёт с сужением окна, а не обрезается — см. tests/test_three_tabs_narrow.py).
        self._toggle_labels: dict[str, QLabel] = {}
        cfg = self.engine.config

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(12)

        # заголовок + статус + кнопки (в узком окне кнопки уходят на вторую строку)
        title = QLabel("Настройки")
        title.setStyleSheet(f"color:{theme.TEXT};font-size:22px;font-weight:700;")
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.GREEN};font-size:12px;")
        backup_btn = self._small_btn("⤓ Резервная копия", theme.ACCENT2)
        backup_btn.clicked.connect(self._backup)
        restore_btn = self._small_btn("↺ Восстановить", theme.ACCENT)
        restore_btn.clicked.connect(self._restore)
        reset_btn = self._small_btn("⟳ Сбросить", theme.BORDER, theme.TEXT)
        reset_btn.clicked.connect(self._reset_defaults)
        head = self._head_bar = _HeadBar(title, self._status,
                                        [backup_btn, restore_btn, reset_btn])
        outer.addWidget(head)

        body = QVBoxLayout()
        body.setSpacing(12)

        # ── Секция 0: Темы ──
        stheme, ltheme = _section("🎨  Темы")
        theme_desc = self._theme_desc = QLabel(
            "Выберите внешний вид UmbraNet. Тема сохраняется сразу и полностью "
            "применится после перезапуска приложения."
        )
        theme_desc.setWordWrap(True)
        theme_desc.setMinimumHeight(_DESC_MIN_H)          # минимум, а не предел: см. _wrapped
        theme_desc.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        theme_desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        ltheme.addWidget(theme_desc)

        theme_row = QHBoxLayout()
        theme_lbl = self._form_label("Тема приложения")
        self._theme_combo = _NoWheelComboBox()
        for key, label in theme.theme_items():
            self._theme_combo.addItem(label, key)
        ti = self._theme_combo.findData(theme.CURRENT_THEME)
        if ti >= 0:
            self._theme_combo.setCurrentIndex(ti)
        self._theme_combo.setFixedHeight(34)
        self._theme_combo.setStyleSheet(self._combo_qss())
        self._theme_combo.currentIndexChanged.connect(lambda _=0: self._on_theme_changed())
        theme_row.addWidget(theme_lbl)
        theme_row.addWidget(self._theme_combo)
        theme_row.addStretch()
        ltheme.addLayout(theme_row)
        body.addWidget(stheme)

        # ── Секция 1: DNS-сервер ──
        s1, l1 = _section("🖧  DNS-сервер")
        self._port = self._line_field(l1, "Порт", str(cfg.get("listen_port", 53)), 90)
        self._fb4 = self._line_field(l1, "Fallback IPv4", cfg.get("fallback_dns", "8.8.8.8"))
        self._fb6 = self._line_field(l1, "Fallback IPv6", cfg.get("fallback_dns6", ""), 240)
        body.addWidget(s1)

        # ── Секция 2: Поведение ──
        s2, l2 = _section("⚡  Поведение")
        self._dev_mode = self._toggle_row(
            l2,
            "Режим разработчика — снять блокировку DNS/DPI у сервисов в главном меню",
            get_developer_mode(),
            key="developer_mode",
            autosave=False,
        )
        self._dev_mode.toggled.connect(self._on_developer_mode)
        self._ipv6 = self._toggle_row(l2, "Включить IPv6 DNS-сервер", cfg.get("enable_ipv6", True),
                                      key="ipv6")
        self._ipv6_priority = self._toggle_row(l2, "Приоритет IPv6 для заблокированных сайтов (трюк обхода)",
                                               cfg.get("ipv6_priority_enabled", False),
                                               key="ipv6_priority")

        self._autostart = None
        if autostart_supported():
            self._autostart = self._toggle_row(l2, "Запускать UmbraNet при включении Windows",
                                               autostart_enabled(), key="autostart")
        else:
            note = QLabel("Автозапуск доступен только на Windows")
            note.setStyleSheet(f"color:{theme.MUTED};font-size:12px;background:transparent;border:none;")
            l2.addWidget(note)

        # стратегия upstream
        up_row = QHBoxLayout()
        up_lbl = self._form_label("Стратегия upstream")
        self._upstream = _NoWheelComboBox()
        for mode in upstream_modes():
            self._upstream.addItem(UPSTREAM_LABELS.get(mode, mode), mode)
        i = self._upstream.findData(cfg.get("upstream_mode", "parallel"))
        if i >= 0:
            self._upstream.setCurrentIndex(i)
        self._upstream.setFixedHeight(34)
        self._upstream.setStyleSheet(self._combo_qss())
        self._upstream.currentIndexChanged.connect(lambda _=0: self._autosave())
        up_row.addWidget(up_lbl)
        up_row.addWidget(self._upstream)
        up_row.addStretch()
        l2.addLayout(up_row)
        body.addWidget(s2)

        # ── Секция 3: Routed-кэш ──
        s3, l3 = _section("🗃  Routed-домены: кэш и TTL")
        preset_row = QHBoxLayout()
        p_lbl = self._form_label("Пресет режима")
        self._preset = _NoWheelComboBox()
        self._presets_map = get_routed_preset_map()
        # ТОЛЬКО реальные пресеты (без «Пользовательский» — он авто-подпись)
        self._preset.addItems(list(self._presets_map.keys()))
        self._preset.setFixedHeight(34)
        self._preset.setStyleSheet(self._combo_qss())
        self._preset.activated.connect(self._on_preset_chosen)  # только по клику юзера
        preset_row.addWidget(p_lbl)
        preset_row.addWidget(self._preset)
        preset_row.addStretch()
        l3.addLayout(preset_row)

        self._cache_on = self._toggle_row(l3, "Внутренний кэш для routed-доменов",
                                          cfg.get("routed_cache_enabled", True), key="cache_on")
        # слайдеры TTL
        self._cache_ttl = SliderField("TTL кэша", int(cfg.get("routed_cache_ttl", 5)), 0, 120)
        self._cache_ttl.valueChanged.connect(lambda _=0: self._autosave())
        l3.addWidget(self._cache_ttl)
        self._reply_ttl = SliderField("TTL ответа", int(cfg.get("routed_reply_ttl", 1)), 0, 60)
        self._reply_ttl.valueChanged.connect(lambda _=0: self._autosave())
        l3.addWidget(self._reply_ttl)
        self._optim = self._toggle_row(l3, "Optimistic cache (мгновенные ответы из «просроченного» кэша)",
                                       cfg.get("optimistic_cache_enabled", True), key="optim")
        self._stale_ttl = SliderField("Stale TTL", int(cfg.get("stale_cache_ttl", 3600)), 0, 86400)
        self._stale_ttl.valueChanged.connect(lambda _=0: self._autosave())
        l3.addWidget(self._stale_ttl)
        body.addWidget(s3)

        # ── Секция 4: DNS-фильтрация ──
        sf, lf = _section("🚦  DNS-фильтрация: blocklist / allowlist")
        desc = self._filter_desc = QLabel(
            "Можно вставлять обычные домены, hosts-формат (0.0.0.0 domain) "
            "и простые AdBlock-правила вида ||domain^. Allowlist имеет приоритет над blocklist."
        )
        desc.setWordWrap(True)
        desc.setMinimumHeight(_DESC_MIN_H)
        desc.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lf.addWidget(desc)

        lists_row = QHBoxLayout()
        self._blocked_edit = self._domain_list_editor(
            "Блокировать (NXDOMAIN)", cfg.get("blocked_domains", [])
        )
        self._allow_edit = self._domain_list_editor(
            "Разрешать всегда (allowlist)", cfg.get("allowlist_domains", [])
        )
        lists_row.addWidget(self._blocked_edit["wrap"], 1)
        lists_row.addWidget(self._allow_edit["wrap"], 1)
        lf.addLayout(lists_row)

        filter_row = QHBoxLayout()
        self._filter_status = QLabel(self._filter_status_text())
        self._filter_status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        filter_row.addWidget(self._filter_status, 1)
        self._filter_save_btn = self._small_btn("✓ Сохранить списки", theme.GREEN)
        self._filter_save_btn.clicked.connect(self._save_filter_lists)
        filter_row.addWidget(self._filter_save_btn)
        lf.addLayout(filter_row)
        body.addWidget(sf)

        body.addStretch()

        # прокрутка со стилизованным скроллбаром
        bodyw = QWidget()
        bodyw.setLayout(body)
        # WA_StaticContents — Qt не перерисовывает весь body при каждом пикселе ресайза
        try:
            bodyw.setAttribute(Qt.WA_StaticContents, True)
        except Exception:
            pass
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())
        scroll.setWidget(bodyw)
        outer.addWidget(scroll, 1)

        self._update_preset_label()

    # ── фабрики строк (с автосейвом) ──
    @staticmethod
    def _form_label(text: str) -> QLabel:
        """Подпись слева от поля.

        Занимает 150 px — как раньше (подписи выровнены), но это ПРЕДЕЛ, а не
        константа: в узком окне подпись ужимается до 110 px. Раньше стоял
        setFixedWidth(150), и подпись вместе с полем держала минимальную ширину
        вкладки: в окне 560 px содержимому достаётся 434, а подписи и поля просили
        больше — отсюда горизонтальная прокрутка.
        """
        lbl = _FormLabel(text)
        lbl.setMinimumWidth(_LABEL_MIN_W)
        lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        return lbl

    def _line_field(self, parent_lay, label, value, width=160) -> QLineEdit:
        row = QHBoxLayout()
        lbl = self._form_label(label)
        inp = QLineEdit(value)
        # Ширина — прежняя как предел, но не как минимум: иначе поле в 240 px
        # (Fallback IPv6) вместе с подписью распирало карточку, и в узком окне
        # появлялась горизонтальная прокрутка. В широком окне вид не меняется.
        inp.setMaximumWidth(width)
        inp.setMinimumWidth(min(width, _FIELD_MIN_W))
        inp.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        inp.setFixedHeight(34)
        inp.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:6px;padding:0 10px;font-family:Consolas;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        inp.editingFinished.connect(self._autosave)  # сохранить при потере фокуса/Enter
        row.addWidget(lbl)
        row.addWidget(inp)
        row.addStretch()
        parent_lay.addLayout(row)
        return inp

    def _domain_list_editor(self, title: str, values: list) -> dict:
        wrap = QFrame()
        wrap.setStyleSheet("QFrame{background:transparent;border:none;}")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lbl = QLabel(title)
        # Перенос по словам: две колонки списков стоят рядом, и в узком окне
        # подпись без переноса задавала минимальную ширину всей карточки.
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        lbl.setStyleSheet(f"color:{theme.TEXT};font-size:13px;font-weight:700;background:transparent;border:none;")
        lay.addWidget(lbl)
        edit = QPlainTextEdit()
        edit.setPlainText("\n".join(values or []))
        edit.setMinimumHeight(130)
        # NoWrap — ширина не вызывает перекомпоновку документа на каждый пиксель ресайза
        try:
            edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        except Exception:
            pass
        edit.setPlaceholderText("example.com\n0.0.0.0 ads.example.com\n||tracker.example.net^")
        # Упрощённый QSS без border-radius маски (прямоугольник красится быстрее)
        edit.setStyleSheet(
            f"QPlainTextEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:6px;padding:8px;"
            "font-family:Consolas;font-size:12px;}" + theme.scrollbar_qss()
        )
        lay.addWidget(edit)
        return {"wrap": wrap, "edit": edit}

    def _toggle_row(self, parent_lay, label, checked, key: str = "",
                    autosave: bool = True) -> Toggle:
        """Строка «подпись + переключатель».

        Высота подписи — МИНИМУМ, а не предел. Раньше здесь стояла жёсткая высота
        22 px («чтобы не считать heightForWidth на каждый пиксель ресайза»), и в
        узком окне длинные подписи («Режим разработчика — снять блокировку …»,
        «Optimistic cache …») переносились на вторую строку и обрезались: строка
        текста просто исчезала. Теперь подпись растёт, карточка растёт следом.
        """
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setWordWrap(True)
        lbl.setMinimumHeight(_TOGGLE_LABEL_MIN_H)
        lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        lbl.setStyleSheet(f"color:{theme.TEXT};font-size:13px;background:transparent;border:none;")
        tg = Toggle(checked)
        if autosave:
            tg.toggled.connect(lambda _=False: self._autosave())
        row.addWidget(lbl, 1)
        row.addWidget(tg)
        parent_lay.addLayout(row)
        if key:
            self._toggle_labels[key] = lbl
        return tg

    # ── стили ──
    def _combo_qss(self) -> str:
        # Радиус 6 вместо 8 — маски меньше, перерисовка при ресайзе быстрее
        return (
            f"QComboBox{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            # min-width 200: столько нужно самому длинному пункту («Параллельно
            # (быстрее)», 150 px) вместе с рамкой и стрелкой — в закрытом виде текст
            # должен читаться целиком, как раньше. Ужимается не поле, а подпись слева
            # (см. _form_label): в узком окне ширину отдаёт она, а не значение.
            f"border:1px solid {theme.BORDER};border-radius:6px;padding:0 10px;min-width:200px;}}"
            f"QComboBox:hover{{border-color:{theme.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{theme.CARD};color:{theme.TEXT};"
            f"selection-background-color:{theme.ACCENT};border:1px solid {theme.BORDER};}}")

    def _small_btn(self, text, bg, fg=None) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        b.setStyleSheet(
            f"QPushButton{{background:{bg};color:{fg or theme.WHITE};"
            "border:none;border-radius:9px;padding:0 14px;font-size:12px;}"
            f"QPushButton:hover{{background:{theme.ACCENT};color:{theme.WHITE};}}")
        return b

    # ── логика ──
    def _on_theme_changed(self):
        if self._suppress or not hasattr(self, "_theme_combo"):
            return
        name = self._theme_combo.currentData()
        if not name or name == theme.CURRENT_THEME:
            return

        dlg = ThemeRestartDialog(theme.theme_label(name), self)
        # exec() в PySide6 возвращает DialogCode; сравнение с dlg.Accepted
        # на части сборок ложно срабатывает как «отмена» — диалог закрывается,
        # перезапуск даже не начинается.
        if not dlg.exec():
            self._suppress = True
            i = self._theme_combo.findData(theme.CURRENT_THEME)
            if i >= 0:
                self._theme_combo.setCurrentIndex(i)
            self._suppress = False
            return

        saved = theme.save_theme_preference(name)
        win = self
        while win is not None and not callable(getattr(win, "restart_application", None)):
            win = win.parentWidget()
        if win is None:
            win = self.window()
        restart = getattr(win, "restart_application", None)
        if callable(restart):
            # Не из этого слота: сначала закрыть диалог и выйти из exec().
            QTimer.singleShot(0, restart)
            return
        self._set_status(
            f"✓ Тема «{theme.theme_label(saved)}» сохранена. Перезапустите UmbraNet",
            theme.ACCENT3,
        )

    def _on_preset_chosen(self, index: int):
        name = self._preset.itemText(index)
        preset = self._presets_map.get(name)
        if not preset:
            return
        self._suppress = True
        self._cache_on.setChecked(bool(preset.get("routed_cache_enabled", True)))
        self._cache_ttl.setValue(int(preset.get("routed_cache_ttl", 5)))
        self._reply_ttl.setValue(int(preset.get("routed_reply_ttl", 1)))
        self._optim.setChecked(bool(preset.get("optimistic_cache_enabled", True)))
        self._stale_ttl.setValue(int(preset.get("stale_cache_ttl", 3600)))
        self._suppress = False
        self._autosave()

    def _current_preset_name(self) -> str | None:
        """Имя пресета, совпадающего с текущими значениями, иначе None."""
        cur = {
            "routed_cache_enabled": self._cache_on.isChecked(),
            "routed_cache_ttl": self._cache_ttl.value(),
            "routed_reply_ttl": self._reply_ttl.value(),
            "optimistic_cache_enabled": self._optim.isChecked(),
            "stale_cache_ttl": self._stale_ttl.value(),
        }
        for name, preset in self._presets_map.items():
            if all(cur[f] == preset.get(f) for f in PRESET_FIELDS):
                return name
        return None

    def _update_preset_label(self):
        name = self._current_preset_name()
        self._preset.blockSignals(True)
        if name:
            i = self._preset.findText(name)
            if i >= 0:
                self._preset.setCurrentIndex(i)
        else:
            # значения «пользовательские» — показываем это как временный заголовок
            self._preset.setCurrentIndex(-1)
            self._preset.setEditable(False)
            self._preset.setPlaceholderText("Пользовательский")
        self._preset.blockSignals(False)

    def _on_developer_mode(self, _on: bool = False):
        if self._suppress:
            return
        set_developer_mode(self._dev_mode.isChecked())
        self._set_status("✓ Сохранено", theme.GREEN)
        QTimer.singleShot(1500, lambda: self._set_status("", theme.GREEN))

    def _autosave(self):
        if self._suppress:
            return
        cfg = self.engine.config
        try:
            cfg["listen_port"] = int(self._port.text())
        except ValueError:
            self._set_status("Порт должен быть числом", theme.RED)
            return
        cfg["fallback_dns"] = self._fb4.text().strip()
        cfg["fallback_dns6"] = self._fb6.text().strip()
        cfg["enable_ipv6"] = self._ipv6.isChecked()
        cfg["ipv6_priority_enabled"] = self._ipv6_priority.isChecked()
        cfg["upstream_mode"] = self._upstream.currentData()
        cfg["routed_cache_enabled"] = self._cache_on.isChecked()
        cfg["optimistic_cache_enabled"] = self._optim.isChecked()
        cfg["routed_cache_ttl"] = self._cache_ttl.value()
        cfg["routed_reply_ttl"] = self._reply_ttl.value()
        cfg["stale_cache_ttl"] = self._stale_ttl.value()

        save_config(cfg)
        self.engine.reload_config()

        if self._autostart is not None:
            try:
                autostart_set(self._autostart.isChecked())
            except Exception:
                pass

        self._update_preset_label()
        self._set_status("✓ Сохранено", theme.GREEN)
        QTimer.singleShot(1500, lambda: self._set_status("", theme.GREEN))

    def _backup(self):
        path = backup_create(self.engine.config)
        self._set_status("✓ Резервная копия создана" if path else "Не удалось создать копию",
                         theme.GREEN if path else theme.RED)

    def _restore(self):
        from umbranet.widgets.dialogs import RestoreBackupDialog
        dlg = RestoreBackupDialog(backup_list(), self)
        if not dlg.exec() or not dlg.result:
            return
        cfg = backup_load(dlg.result)
        if not cfg:
            self._set_status("Не удалось прочитать резервную копию", theme.RED)
            return
        self.engine.config.clear()
        self.engine.config.update(cfg)
        save_config(self.engine.config)
        self.engine.reload_config()
        self._load_form_from_cfg(self.engine.config)
        self._set_status("✓ Настройки восстановлены", theme.GREEN)

    def _load_form_from_cfg(self, cfg):
        self._suppress = True
        self._port.setText(str(cfg.get("listen_port", 53)))
        self._fb4.setText(cfg.get("fallback_dns", "8.8.8.8"))
        self._fb6.setText(cfg.get("fallback_dns6", ""))
        self._ipv6.setChecked(bool(cfg.get("enable_ipv6", True)))
        i = self._upstream.findData(cfg.get("upstream_mode", "parallel"))
        if i >= 0:
            self._upstream.setCurrentIndex(i)
        self._cache_on.setChecked(bool(cfg.get("routed_cache_enabled", True)))
        self._cache_ttl.setValue(int(cfg.get("routed_cache_ttl", 5)))
        self._reply_ttl.setValue(int(cfg.get("routed_reply_ttl", 1)))
        self._optim.setChecked(bool(cfg.get("optimistic_cache_enabled", True)))
        self._stale_ttl.setValue(int(cfg.get("stale_cache_ttl", 3600)))
        if hasattr(self, "_blocked_edit"):
            self._blocked_edit["edit"].setPlainText("\n".join(cfg.get("blocked_domains", []) or []))
            self._allow_edit["edit"].setPlainText("\n".join(cfg.get("allowlist_domains", []) or []))
            self._filter_status.setText(self._filter_status_text())
        if self._autostart is not None:
            try:
                self._autostart.setChecked(autostart_enabled())
            except Exception:
                pass
        self._suppress = False
        self._update_preset_label()

    def _reset_defaults(self):
        self._suppress = True
        self._port.setText("53")
        self._fb4.setText("8.8.8.8")
        self._fb6.setText("2001:4860:4860::8888")
        self._ipv6.setChecked(True)
        i = self._upstream.findData("parallel")
        if i >= 0:
            self._upstream.setCurrentIndex(i)
        self._cache_on.setChecked(True)
        self._cache_ttl.setValue(5)
        self._reply_ttl.setValue(1)
        self._optim.setChecked(True)
        self._stale_ttl.setValue(3600)
        self._suppress = False
        self._autosave()
        self._set_status("Значения сброшены", theme.YELLOW)

    def _set_status(self, text, color=None):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color or theme.GREEN};font-size:12px;")

    # ── DNS-фильтрация ──
    def _filter_status_text(self) -> str:
        cfg = self.engine.config
        return (
            f"Блоклист: {len(cfg.get('blocked_domains', []) or [])} • "
            f"Allowlist: {len(cfg.get('allowlist_domains', []) or [])}"
        )

    def _save_filter_lists(self):
        blocked = parse_domain_lines(
            self._blocked_edit["edit"].toPlainText(),
            include_adblock_exceptions=False,
        )
        allow = parse_domain_lines(self._allow_edit["edit"].toPlainText())
        if set_filter_lists(blocked=blocked, allowlist=allow):
            # normalize visible text after parsing hosts/adblock syntax
            self._blocked_edit["edit"].setPlainText("\n".join(blocked))
            self._allow_edit["edit"].setPlainText("\n".join(allow))
            self.engine.config["blocked_domains"] = blocked
            self.engine.config["allowlist_domains"] = allow
            self._filter_status.setText(self._filter_status_text())
            self._set_status("✓ DNS-фильтры сохранены", theme.GREEN)
        else:
            self._set_status("Не удалось сохранить DNS-фильтры", theme.RED)

    def refresh(self):
        """Обновляет статусы настроек при переключении на вкладку."""
        if hasattr(self, "_filter_status"):
            self._filter_status.setText(self._filter_status_text())
