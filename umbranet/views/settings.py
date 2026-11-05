"""
UmbraNet - раздел «Настройки» (PySide6).

Раздел настроек с автосохранением:
  • темы оформления и поведение приложения;
  • надёжность VPN: проверка, автозамена и горячий резерв;
  • серверы и их технические данные находятся в отдельной вкладке «Сервера».

Устаревшие параметры DNS сохраняются в конфигурации для совместимости и
диагностики, но не изменяются из обычного интерфейса настроек.
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
    post_event,
    save_config,
    set_developer_mode,
    sync_routing_runtime_config,
)
from core.vpn.profiles import normalize_probe_url
from umbranet.widgets.dialogs import ThemeRestartDialog
from umbranet.widgets.flow_layout import FlowLayout
from umbranet.widgets.hover_hint import HoverHintBadge, SHOW_DELAY_MS
from umbranet.widgets.rounded_panel import RoundedPanel
from umbranet.widgets.toggle import Toggle

TRANSPORT_FAILOVER_LABELS = {
    "economy": "Экономный — резерв проверяется после сбоя",
    "recommended": "Рекомендуемый — один горячий резерв",
    "continuous": "Бесперебойный — все выходы постоянно готовы",
}
TRANSPORT_REACTION_LABELS = {
    "cautious": "Осторожная — только подтверждённый сбой",
    "balanced": "Сбалансированная — рекомендуется",
    "fast": "Быстрая — сразу при первом сбое",
}


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



# Compatibility name retained for focused settings geometry tests. The shared
# badge owns the actual delay and avoids native Qt tooltip auto-dismissal.
HINT_HOVER_DELAY_MS = SHOW_DELAY_MS


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
        #: растёт с сужением окна, а не обрезается — см. tests/test_routing_settings_narrow.py).
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

        # ── Секция 1: Поведение ──
        s2, l2 = _section("⚡  Поведение")
        self._dev_mode = self._toggle_row(
            l2,
            "Режим разработчика — снять блокировку VPN/DPI у сервисов в главном меню",
            get_developer_mode(),
            key="developer_mode",
            autosave=False,
        )
        self._dev_mode.toggled.connect(self._on_developer_mode)
        self._autostart = None
        if autostart_supported():
            self._autostart = self._toggle_row(l2, "Запускать UmbraNet при включении Windows",
                                               autostart_enabled(), key="autostart")
        else:
            note = QLabel("Автозапуск доступен только на Windows")
            note.setStyleSheet(f"color:{theme.MUTED};font-size:12px;background:transparent;border:none;")
            l2.addWidget(note)

        body.addWidget(s2)

        # ── Секция 3: надёжность VPN и горячий резерв ──
        transport_section, transport_lay = _section("🛟  Надёжность VPN и автозамена")
        transport_desc = QLabel(
            "Выбранные VPN-сервисы переключаются только на проверенный резервный сервер. "
            "Если готовых серверов нет, прямое подключение не используется. "
            "Изменение этой политики применяется к активной сессии сразу."
        )
        transport_desc.setWordWrap(True)
        transport_desc.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;"
        )
        transport_lay.addWidget(transport_desc)
        self._transport_probe_url = self._line_field(
            transport_lay,
            "HTTPS-проверка сервера",
            cfg.get("transport_probe_url", ""),
            380,
            hint=(
                "Укажите свой стабильный HTTPS endpoint, который отвечает 2xx/3xx. "
                "UmbraNet проходит к нему через каждый HTTP CONNECT/SOCKS5 выход, "
                "проверяет TLS и только затем считает выход готовым.\n\n"
                "URL не принимает логин, пароль, query-параметры или fragment. Пока "
                "адрес не задан, выбранные VPN-сервисы блокируются без DIRECT."
            ),
        )
        self._transport_probe_url.setPlaceholderText("https://status.example.net/umbranet-health")
        self._transport_failover = self._toggle_row(
            transport_lay,
            "Автоматически заменять недоступный VPN-сервер",
            cfg.get("transport_failover_enabled", True),
            key="transport_failover",
        )
        transport_mode_row = QHBoxLayout()
        transport_mode_row.addWidget(self._form_label("Режим автозамены"))
        self._transport_mode = _NoWheelComboBox()
        for key, label in TRANSPORT_FAILOVER_LABELS.items():
            self._transport_mode.addItem(label, key)
        index = self._transport_mode.findData(cfg.get("transport_failover_mode", "recommended"))
        self._transport_mode.setCurrentIndex(index if index >= 0 else 1)
        self._transport_mode.setFixedHeight(34)
        self._transport_mode.setStyleSheet(self._combo_qss())
        self._transport_mode.currentIndexChanged.connect(lambda _=0: self._autosave())
        transport_mode_row.addWidget(self._transport_mode)
        transport_mode_row.addStretch()
        transport_lay.addLayout(transport_mode_row)
        transport_reaction_row = QHBoxLayout()
        transport_reaction_row.addWidget(self._form_label("Реакция на сбой"))
        self._transport_reaction = _NoWheelComboBox()
        for key, label in TRANSPORT_REACTION_LABELS.items():
            self._transport_reaction.addItem(label, key)
        index = self._transport_reaction.findData(cfg.get("transport_failure_reaction", "balanced"))
        self._transport_reaction.setCurrentIndex(index if index >= 0 else 1)
        self._transport_reaction.setFixedHeight(34)
        self._transport_reaction.setStyleSheet(self._combo_qss())
        self._transport_reaction.currentIndexChanged.connect(lambda _=0: self._autosave())
        transport_reaction_row.addWidget(self._transport_reaction)
        transport_reaction_row.addStretch()
        transport_lay.addLayout(transport_reaction_row)
        no_direct_row = QHBoxLayout()
        no_direct_label = self._form_label("Если резерва нет")
        no_direct_value = QLabel("Блокировать выбранные VPN-сервисы")
        no_direct_value.setStyleSheet(
            f"color:{theme.YELLOW};font-size:13px;font-weight:600;background:transparent;border:none;"
        )
        no_direct_row.addWidget(no_direct_label)
        no_direct_row.addWidget(self._hint_badge(
            "UmbraNet не добавляет DIRECT в PAC-цепочку выбранного VPN-сервиса. "
            "Так при одновременном сбое всех серверов сервис не раскроет прямой маршрут."
        ))
        no_direct_row.addWidget(no_direct_value)
        no_direct_row.addStretch()
        transport_lay.addLayout(no_direct_row)
        body.addWidget(transport_section)


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

    @staticmethod
    def _hint_badge(text: str) -> HoverHintBadge:
        """Общий круглый «?» с устойчивой всплывающей подсказкой."""
        return HoverHintBadge(text)

    def _line_field(self, parent_lay, label, value, width=160, hint: str = "") -> QLineEdit:
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
        if hint:
            row.addWidget(self._hint_badge(hint))
        row.addWidget(inp)
        row.addStretch()
        parent_lay.addLayout(row)
        return inp

    def _toggle_row(self, parent_lay, label, checked, key: str = "",
                    autosave: bool = True) -> Toggle:
        """Строка «подпись + переключатель».

        Высота подписи — МИНИМУМ, а не предел: длинные названия поведения и
        политики VPN могут перенестись на вторую строку. Тогда подпись и карточка
        растут вместе, а текст не обрезается.
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
        probe_url = normalize_probe_url(self._transport_probe_url.text())
        if probe_url is None:
            self._set_status(
                "HTTPS-проверка: нужен URL https://host/path без параметров и учётных данных",
                theme.RED,
            )
            return
        self._transport_probe_url.setText(probe_url)
        cfg["transport_probe_url"] = probe_url
        cfg["transport_failover_enabled"] = self._transport_failover.isChecked()
        cfg["transport_failover_mode"] = self._transport_mode.currentData()
        cfg["transport_failure_reaction"] = self._transport_reaction.currentData()
        # Это не пользовательский переключатель: решение продукта — не
        # выпускать выбранные VPN-сервисы напрямую при потере всех выходов.
        cfg["transport_no_ready_policy"] = "block"

        save_config(cfg)
        # Settings mutate the shared config map in place. Reconfigure only the
        # active routing owner; DNS compatibility code is not restarted here.
        sync_routing_runtime_config(force=True)
        post_event({"type": "config_changed", "section": "transport_settings"})
        if self._autostart is not None:
            try:
                autostart_set(self._autostart.isChecked())
            except Exception:
                pass

        self._set_status("✓ Сохранено", theme.GREEN)
        QTimer.singleShot(2200, lambda: self._set_status("", theme.GREEN))

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
        sync_routing_runtime_config(force=True)
        post_event({"type": "config_changed", "section": "backup_restore"})
        self._load_form_from_cfg(self.engine.config)
        self._set_status("✓ Настройки восстановлены", theme.GREEN)

    def _load_form_from_cfg(self, cfg):
        self._suppress = True
        self._transport_probe_url.setText(str(cfg.get("transport_probe_url", "") or ""))
        self._transport_failover.setChecked(bool(cfg.get("transport_failover_enabled", True)))
        i = self._transport_mode.findData(cfg.get("transport_failover_mode", "recommended"))
        if i >= 0:
            self._transport_mode.setCurrentIndex(i)
        i = self._transport_reaction.findData(cfg.get("transport_failure_reaction", "balanced"))
        if i >= 0:
            self._transport_reaction.setCurrentIndex(i)
        if self._autostart is not None:
            try:
                self._autostart.setChecked(autostart_enabled())
            except Exception:
                pass
        self._suppress = False

    def _reset_defaults(self):
        self._suppress = True
        self._transport_probe_url.setText("")
        self._transport_failover.setChecked(True)
        i = self._transport_mode.findData("recommended")
        if i >= 0:
            self._transport_mode.setCurrentIndex(i)
        i = self._transport_reaction.findData("balanced")
        if i >= 0:
            self._transport_reaction.setCurrentIndex(i)
        self._suppress = False
        self._autosave()
        self._set_status("Значения сброшены", theme.YELLOW)

    def _set_status(self, text, color=None):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color or theme.GREEN};font-size:12px;")

    def refresh(self):
        """Вкладка не требует динамического обновления."""
        return None
