"""User-facing server library: built-in servers and custom server import/editing."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.vpn.importers import import_custom_server_file
from core.vpn.profiles import MAX_ROUTE_PROFILES, sanitize_route_profiles
from core.vpn.subscriptions import (
    SubscriptionFetchResult,
    fetch_transport_subscription,
    materialize_subscription_profiles,
    sanitize_transport_subscriptions,
)
from core.vpn.providers import BuiltinProviderCatalog, ProviderState
from umbranet import theme
from umbranet.widgets.hover_hint import HoverHintBadge
from umbranet.engine_adapter import (
    get_engine,
    post_event,
    save_config,
    sync_routing_runtime_config,
    vpn_protocol_capabilities,
    vpn_server_statuses,
)


_PROVIDER_STATE_TEXT = {
    ProviderState.AVAILABLE: "Готов",
    ProviderState.NOT_INSTALLED: "Не установлен",
    ProviderState.CONFIGURATION_REQUIRED: "Подготавливается",
    ProviderState.SECURITY_REVIEW: "Проверка безопасности",
    ProviderState.UNSUPPORTED_PLATFORM: "Только Windows",
    ProviderState.UNAVAILABLE: "Недоступен",
}
_PROVIDER_STATE_COLOR = {
    ProviderState.AVAILABLE: theme.GREEN,
    ProviderState.NOT_INSTALLED: theme.YELLOW,
    ProviderState.CONFIGURATION_REQUIRED: theme.YELLOW,
    ProviderState.SECURITY_REVIEW: theme.MUTED,
    ProviderState.UNSUPPORTED_PLATFORM: theme.MUTED,
    ProviderState.UNAVAILABLE: theme.RED,
}

_PROTOCOL_STATE_TEXT = {
    "available": "Работает сейчас",
    "backend_required": "Нужен backend",
    "integration_required": "Нужен native adapter",
    "unsupported_platform": "Только Windows",
    "unavailable": "Недоступен",
}
_PROTOCOL_STATE_COLOR = {
    "available": theme.GREEN,
    "backend_required": theme.YELLOW,
    "integration_required": theme.YELLOW,
    "unsupported_platform": theme.MUTED,
    "unavailable": theme.RED,
}
_EGRESS_KIND_TEXT = {
    "loopback_proxy": "Локальный proxy",
    "tunnel_interface": "TUN-интерфейс",
}


class _SubscriptionFetchWorker(QThread):
    """Run one user-requested subscription refresh outside the GUI thread."""

    completed = Signal(str, object)  # source id (empty for a new source), SubscriptionFetchResult

    def __init__(self, source_id: str, url: str, parent=None):
        super().__init__(parent)
        self._source_id = source_id
        self._url = url

    def run(self) -> None:
        self.completed.emit(self._source_id, fetch_transport_subscription(self._url))


class _SubscriptionGroup(QWidget):
    """Flat expandable subscription folder that owns no raw URL display."""

    serverSelected = Signal(str)
    refreshRequested = Signal(str)
    deleteRequested = Signal(str)
    expandedChanged = Signal(str, bool)

    def __init__(self, subscription: dict, profiles: list[dict], *, expanded: bool,
                 primary_id: str, statuses: dict[str, dict[str, object]], parent=None):
        super().__init__(parent)
        self._subscription_id = str(subscription.get("id", ""))
        self._label = str(subscription.get("label", "") or "Подписка")
        self._expanded = bool(expanded)
        self.setStyleSheet("background:transparent;border:none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(5)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(7)
        self._toggle = QPushButton()
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setStyleSheet(
            f"QPushButton{{color:{theme.TEXT};background:transparent;border:none;padding:3px 2px;"
            "font-size:12px;font-weight:700;text-align:left;}"
            f"QPushButton:hover{{color:{theme.ACCENT3};}}"
        )
        self._toggle.clicked.connect(self._toggle_expanded)
        header.addWidget(self._toggle, 1)
        count = QLabel(f"{len(profiles)} серверов")
        count.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        header.addWidget(count)
        refresh = QPushButton("↻ Обновить")
        remove = QPushButton("Удалить")
        for button in (refresh, remove):
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedHeight(26)
            button.setStyleSheet(
                f"QPushButton{{background:{theme.INPUT_BG};color:{theme.SUBTEXT};"
                f"border:1px solid {theme.BORDER};border-radius:7px;padding:0 8px;font-size:11px;}}"
                f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.TEXT};}}"
            )
        remove.setStyleSheet(
            f"QPushButton{{background:{theme.INPUT_BG};color:{theme.RED};"
            f"border:1px solid {theme.BORDER};border-radius:7px;padding:0 8px;font-size:11px;}}"
            f"QPushButton:hover{{border-color:{theme.RED};}}"
        )
        refresh.clicked.connect(lambda: self.refreshRequested.emit(self._subscription_id))
        remove.clicked.connect(lambda: self.deleteRequested.emit(self._subscription_id))
        header.addWidget(refresh)
        header.addWidget(remove)
        layout.addLayout(header)

        updated_at = str(subscription.get("updated_at", "") or "")
        meta_text = f"Обновлено: {updated_at}" if updated_at else "Ещё не обновлялась"
        meta = QLabel(meta_text)
        meta.setStyleSheet(f"color:{theme.MUTED};font-size:10px;")
        layout.addWidget(meta)

        self._children = QWidget()
        self._children.setStyleSheet("background:transparent;border:none;")
        children_layout = QVBoxLayout(self._children)
        children_layout.setContentsMargins(12, 0, 0, 0)
        children_layout.setSpacing(3)
        for profile in profiles:
            profile_id = str(profile.get("id", ""))
            readiness = str(statuses.get(profile_id, {}).get("state", "") or "")
            if not profile.get("enabled", True):
                state, color, details = "Отключён", theme.MUTED, "Отключён в подписке"
            elif readiness == "failed":
                state, color, details = "Недоступен", theme.RED, "Проверка готовности не пройдена"
            elif readiness in {"warm", "active"}:
                state, color, details = "Проверен", theme.GREEN, "Готовый сервер подписки"
            elif readiness == "checking":
                state, color, details = "Проверяется", theme.YELLOW, "Проверяется через HTTPS endpoint"
            else:
                state, color, details = "Будет проверен", theme.YELLOW, "Проверяется при запуске VPN"
            row = _ServerRow(
                server_id=profile_id,
                name=str(profile.get("name", profile_id)),
                details=details,
                state=state,
                color=color,
                selectable=bool(profile.get("enabled", True)),
                selected=profile_id == primary_id,
                editable=False,
            )
            row.selected.connect(self.serverSelected)
            children_layout.addWidget(row)
        self._children.setVisible(self._expanded)
        layout.addWidget(self._children)
        self._sync_toggle()

    def _sync_toggle(self) -> None:
        marker = "▾" if self._expanded else "▸"
        self._toggle.setText(f"{marker}  📁  {self._label}")

    def _toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._children.setVisible(self._expanded)
        self._sync_toggle()
        self.expandedChanged.emit(self._subscription_id, self._expanded)


class _ServerRow(QWidget):
    """Flat server list row; only actual actions retain button chrome."""

    selected = Signal(str)
    editRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, *, server_id: str, name: str, details: str, state: str,
                 color: str, selectable: bool, selected: bool = False,
                 editable: bool = False, parent=None):
        super().__init__(parent)
        self._server_id = server_id
        self._selectable = selectable
        self.setCursor(Qt.PointingHandCursor if selectable else Qt.ArrowCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel(name)
        title_color = theme.ACCENT3 if selected else theme.TEXT
        title.setStyleSheet(f"color:{title_color};font-size:13px;font-weight:700;")
        top.addWidget(title)
        top.addStretch(1)
        state_label = QLabel(state)
        state_label.setStyleSheet(f"color:{color};font-size:11px;font-weight:700;")
        top.addWidget(state_label)
        layout.addLayout(top)

        description = QLabel(details)
        description.setWordWrap(True)
        description.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;")
        layout.addWidget(description)

        # A real readiness failure remains stronger than ordinary muted text
        # without returning to a full card around the server row.
        if color == theme.RED:
            underline = QFrame()
            underline.setFixedHeight(2)
            underline.setStyleSheet(f"background:{theme.RED};border:none;border-radius:1px;")
            layout.addWidget(underline)

        if editable:
            actions = QHBoxLayout()
            actions.setContentsMargins(0, 3, 0, 0)
            actions.setSpacing(7)
            edit = QPushButton("Изменить")
            remove = QPushButton("Удалить")
            for button in (edit, remove):
                button.setCursor(Qt.PointingHandCursor)
                button.setFixedHeight(27)
                button.setStyleSheet(
                    f"QPushButton{{background:{theme.INPUT_BG};color:{theme.SUBTEXT};"
                    f"border:1px solid {theme.BORDER};border-radius:7px;padding:0 9px;font-size:11px;}}"
                    f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.TEXT};}}"
                )
            remove.setStyleSheet(
                f"QPushButton{{background:{theme.INPUT_BG};color:{theme.RED};"
                f"border:1px solid {theme.BORDER};border-radius:7px;padding:0 9px;font-size:11px;}}"
                f"QPushButton:hover{{border-color:{theme.RED};}}"
            )
            edit.clicked.connect(lambda: self.editRequested.emit(self._server_id))
            remove.clicked.connect(lambda: self.deleteRequested.emit(self._server_id))
            actions.addWidget(edit)
            actions.addWidget(remove)
            actions.addStretch(1)
            layout.addLayout(actions)

    def mouseReleaseEvent(self, event):
        if self._selectable and event.button() == Qt.LeftButton:
            self.selected.emit(self._server_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _ProtocolRow(QWidget):
    """Flat read-only protocol status row sourced from VPN core."""

    def __init__(self, capability: dict[str, object], parent=None):
        super().__init__(parent)
        state_key = str(capability.get("state", "unavailable") or "unavailable")
        color = _PROTOCOL_STATE_COLOR.get(state_key, theme.MUTED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(3)

        egress = _EGRESS_KIND_TEXT.get(str(capability.get("egress_kind", "") or ""), "Маршрут")
        formats = capability.get("configuration_formats", [])
        format_text = ", ".join(str(item) for item in formats) if isinstance(formats, list) else ""
        description = str(capability.get("description", "") or "")
        message = str(capability.get("message", "") or "")
        details = f"{egress} • {description}"
        if format_text:
            details += f"\nФорматы: {format_text}"
        if message:
            details += f"\n{message}"

        top = QHBoxLayout()
        top.setSpacing(7)
        name = QLabel(str(capability.get("display_name", capability.get("protocol", "VPN protocol"))))
        name.setStyleSheet(f"color:{theme.TEXT};font-size:13px;font-weight:700;")
        top.addWidget(name)
        help_badge = HoverHintBadge(details)
        help_badge.setAccessibleName(f"Справка: {name.text()}")
        top.addWidget(help_badge)
        top.addStretch(1)
        state = QLabel(_PROTOCOL_STATE_TEXT.get(state_key, "Недоступен"))
        state.setStyleSheet(f"color:{color};font-size:11px;font-weight:700;")
        top.addWidget(state)
        layout.addLayout(top)


class ServersView(QWidget):
    """Own server setup outside Settings; Routing only selects a friendly name."""

    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self._catalog = BuiltinProviderCatalog()
        # Status polling is frequent while this page is visible.  Keep a
        # compact render signature so unchanged polling never destroys the
        # rows a person may be reading or hovering.
        self._server_rows_key: tuple[object, ...] | None = None
        self._protocol_rows_key: tuple[str, ...] | None = None
        self._editing_id = ""
        self._suppress = False
        self._subscription_worker: _SubscriptionFetchWorker | None = None
        self._expanded_subscription_ids: set[str] = set()
        self.setStyleSheet("background:transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(12)

        title = QLabel("👁️  Сервера")
        title.setStyleSheet(f"color:{theme.TEXT};font-size:22px;font-weight:700;")
        outer.addWidget(title)
        subtitle = QLabel(
            "Добавьте работающий сервер, проверьте встроенные варианты или посмотрите карту протоколов VPN-движка. "
            "На «Маршрутизации» остаётся только выбор готового маршрута и запуск."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;")
        outer.addWidget(subtitle)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_add_panel(), 0)
        body.addWidget(self._build_server_panel(), 1)
        outer.addLayout(body, 1)
        # Health state is invalidated by the routing runtime's payload-free
        # event.  This view intentionally owns no recurring status timer.
        self.refresh()

    def page_activated(self) -> None:
        """Re-read the retained sanitized status when the library becomes visible."""
        self._rebuild_server_rows()

    @staticmethod
    def _button(text: str, *, accent: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedHeight(34)
        if accent:
            button.setStyleSheet(
                f"QPushButton{{background:{theme.ACCENT};color:{theme.WHITE};border:none;"
                "border-radius:9px;padding:0 13px;font-size:12px;font-weight:700;}"
                f"QPushButton:hover{{background:{theme.ACCENT2};}}"
            )
        else:
            button.setStyleSheet(
                f"QPushButton{{background:{theme.INPUT_BG};color:{theme.TEXT};border:1px solid {theme.BORDER};"
                "border-radius:9px;padding:0 13px;font-size:12px;font-weight:600;}"
                f"QPushButton:hover{{border-color:{theme.ACCENT};}}"
            )
        return button

    @staticmethod
    def _panel(object_name: str) -> tuple[QFrame, QVBoxLayout]:
        """Create one main panel without styling every nested QFrame."""
        panel = QFrame()
        panel.setObjectName(object_name)
        panel.setStyleSheet(
            f"QFrame#{object_name}{{background:{theme.CARD};border:1px solid {theme.BORDER};border-radius:14px;}}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        return panel, layout

    def _build_add_panel(self) -> QWidget:
        panel, layout = self._panel("serverAddPanel")
        panel.setMinimumWidth(320)
        panel.setMaximumWidth(410)
        heading = QLabel("Добавить сервер")
        heading.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:700;")
        layout.addWidget(heading)

        self._add_tabs = QTabWidget()
        self._add_tabs.setObjectName("serverAddTabs")
        self._add_tabs.setStyleSheet(
            f"QTabWidget::pane{{border:1px solid {theme.BORDER};border-radius:9px;top:-1px;}}"
            f"QTabBar::tab{{background:{theme.CARD_DARK};color:{theme.SUBTEXT};border:1px solid {theme.BORDER};"
            "border-bottom:none;border-top-left-radius:8px;border-top-right-radius:8px;padding:7px 9px;}"
            f"QTabBar::tab:selected{{background:{theme.CARD};color:{theme.TEXT};font-weight:600;}}"
        )
        self._add_tabs.addTab(self._build_subscription_import(), "Подписка")
        self._add_tabs.addTab(self._build_file_import(), "Конфигурационный файл")
        self._add_tabs.addTab(self._build_manual_form(), "Вручную")
        layout.addWidget(self._add_tabs)

        self._left_status = QLabel()
        self._left_status.setWordWrap(True)
        self._left_status.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        layout.addWidget(self._left_status)
        layout.addStretch(1)
        return panel

    def _build_subscription_import(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background:transparent;border:none;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        hint = QLabel(
            "Вставьте HTTPS-ссылку подписки и нажмите Enter или «Применить». "
            "UmbraNet один раз обновит её и создаст раскрываемую папку в «Мои подписки». "
            "Сейчас поддерживается только JSON с HTTP CONNECT/SOCKS5 серверами."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;")
        layout.addWidget(hint)
        self._subscription_url = QLineEdit()
        self._subscription_url.setPlaceholderText("https://provider.example/subscription-token")
        self._subscription_url.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};border:1px solid {theme.BORDER};"
            "border-radius:7px;padding:7px;font-family:Consolas;font-size:11px;}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}"
        )
        self._subscription_url.returnPressed.connect(self._apply_subscription_url)
        layout.addWidget(self._subscription_url)
        self._apply_subscription = self._button("✓ Применить", accent=True)
        self._apply_subscription.clicked.connect(self._apply_subscription_url)
        layout.addWidget(self._apply_subscription)
        privacy = QLabel("Ссылка не показывается в статусах и журнале обычного интерфейса.")
        privacy.setWordWrap(True)
        privacy.setStyleSheet(f"color:{theme.MUTED};font-size:10px;")
        layout.addWidget(privacy)
        layout.addStretch(1)
        return page

    def _build_file_import(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background:transparent;border:none;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        hint = QLabel(
            "Выберите конфигурационный файл. Уже поддерживаются JSON-файлы с "
            "HTTP CONNECT/SOCKS5 серверами. Современные протоколы появятся здесь "
            "после добавления их отдельных VPN-adapter-ов."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;")
        layout.addWidget(hint)
        choose = self._button("Выбрать файл", accent=True)
        choose.clicked.connect(self._choose_file)
        layout.addWidget(choose)
        self._file_name = QLabel("Файл не выбран")
        self._file_name.setWordWrap(True)
        self._file_name.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        layout.addWidget(self._file_name)
        layout.addStretch(1)
        return page

    def _build_manual_form(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background:transparent;border:none;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        hint = QLabel("Заполняйте вручную, только если сервер выдан вашим провайдером или принадлежит вам.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(6)
        self._server_name = QLineEdit()
        self._server_name.setPlaceholderText("Например, Мой сервер")
        self._server_protocol = QComboBox()
        self._server_protocol.addItem("HTTP CONNECT", "http")
        self._server_protocol.addItem("SOCKS5", "socks5")
        self._server_host = QLineEdit()
        self._server_host.setPlaceholderText("server.example.net")
        self._server_port = QSpinBox()
        self._server_port.setRange(1, 65535)
        self._server_port.setValue(1080)
        self._server_country = QLineEdit()
        self._server_country.setPlaceholderText("NL")
        self._server_country.setMaxLength(2)
        self._server_login = QLineEdit()
        self._server_password = QLineEdit()
        self._server_password.setEchoMode(QLineEdit.Password)
        self._server_enabled = QCheckBox("Сервер включён")
        self._server_enabled.setChecked(True)
        for label, widget in (
            ("Название", self._server_name),
            ("Тип", self._server_protocol),
            ("Адрес", self._server_host),
            ("Порт", self._server_port),
            ("Страна", self._server_country),
            ("Логин", self._server_login),
            ("Пароль", self._server_password),
            ("", self._server_enabled),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self._new_server = self._button("Новый")
        self._save_server = self._button("Добавить сервер", accent=True)
        self._new_server.clicked.connect(self._new_manual_server)
        self._save_server.clicked.connect(self._save_manual_server)
        buttons.addWidget(self._new_server)
        buttons.addWidget(self._save_server)
        layout.addLayout(buttons)
        return page

    @staticmethod
    def _scroll_rows() -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Keep the vertical scrollbar gutter even when all rows fit.  Otherwise
        # Qt gives those pixels back to the viewport, labels rewrap, and the
        # whole Servers page visibly jumps.  theme.scrollbar_qss() makes the
        # disabled no-range handle transparent, so an empty gutter stays quiet.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}" + theme.scrollbar_qss()
        )
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        rows.addStretch(1)
        scroll.setWidget(holder)
        return scroll, rows

    def _build_server_panel(self) -> QWidget:
        panel, layout = self._panel("serverLibraryPanel")
        heading = QLabel("Серверы и протоколы")
        heading.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:700;")
        layout.addWidget(heading)

        self._server_tabs = QTabWidget()
        self._server_tabs.setObjectName("serverLibraryTabs")
        self._server_tabs.setStyleSheet(
            "QTabWidget::pane{border:none;background:transparent;}"
            f"QTabBar::tab{{background:{theme.CARD_DARK};color:{theme.SUBTEXT};border:1px solid {theme.BORDER};"
            "border-bottom:none;border-top-left-radius:8px;border-top-right-radius:8px;padding:7px 10px;}"
            f"QTabBar::tab:selected{{background:{theme.CARD};color:{theme.TEXT};font-weight:600;}}"
        )

        servers_page = QWidget()
        servers_page.setStyleSheet("background:transparent;border:none;")
        servers_layout = QVBoxLayout(servers_page)
        servers_layout.setContentsMargins(10, 10, 10, 10)
        servers_layout.setSpacing(8)
        self._server_hint = QLabel()
        self._server_hint.setWordWrap(True)
        self._server_hint.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        servers_layout.addWidget(self._server_hint)
        server_scroll, self._server_rows = self._scroll_rows()
        servers_layout.addWidget(server_scroll, 1)
        self._server_tabs.addTab(servers_page, "Серверы")

        protocols_page = QWidget()
        protocols_page.setStyleSheet("background:transparent;border:none;")
        protocols_layout = QVBoxLayout(protocols_page)
        protocols_layout.setContentsMargins(10, 10, 10, 10)
        protocols_layout.setSpacing(8)
        self._protocol_hint = QLabel(
            "Это карта возможностей VPN-движка. Протокол можно добавить или выбрать "
            "только после появления настоящего adapter-а и backend-а."
        )
        self._protocol_hint.setWordWrap(True)
        self._protocol_hint.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        protocols_layout.addWidget(self._protocol_hint)
        protocol_scroll, self._protocol_rows = self._scroll_rows()
        protocols_layout.addWidget(protocol_scroll, 1)
        self._server_tabs.addTab(protocols_page, "Протоколы")

        layout.addWidget(self._server_tabs, 1)
        return panel

    @staticmethod
    def _clear(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _custom_profiles(self) -> list[dict]:
        profiles, _warnings = sanitize_route_profiles(self.engine.config.get("transport_profiles", []))
        return profiles

    def _subscriptions(self) -> list[dict]:
        subscriptions, _warnings = sanitize_transport_subscriptions(
            self.engine.config.get("transport_subscriptions", [])
        )
        return subscriptions

    @staticmethod
    def _manual_profile_state(profile: dict, *, selected: bool,
                              statuses: dict[str, dict[str, object]]) -> tuple[str, str, str]:
        profile_id = str(profile.get("id", ""))
        readiness = str(statuses.get(profile_id, {}).get("state", "") or "")
        if not profile.get("enabled", True):
            return "Отключён", theme.MUTED, "Мой сервер • отключён"
        if readiness == "failed":
            return "Недоступен", theme.RED, "Мой сервер • проверка готовности не пройдена"
        if readiness in {"active", "degraded"}:
            state = "Подключён" if readiness == "active" else "Есть сбои"
            color = theme.GREEN if readiness == "active" else theme.YELLOW
            return state, color, "Мой сервер • активная проверка"
        if readiness == "warm":
            return "Проверен", theme.GREEN, "Мой сервер • готов"
        if readiness == "checking":
            return "Проверяется", theme.YELLOW, "Мой сервер • проверяется через HTTPS endpoint"
        if selected:
            return "Основной", theme.ACCENT3, "Мой сервер • будет проверен перед запуском"
        return "Будет проверен", theme.MUTED, "Мой сервер • доступен для Авто после проверки"

    def _rebuild_server_rows(self) -> None:
        primary_id = str(self.engine.config.get("transport_primary_profile_id", "") or "").strip()
        profiles = self._custom_profiles()
        profile_by_id = {str(profile["id"]): profile for profile in profiles}
        live_statuses = vpn_server_statuses()
        builtin_statuses = self._catalog.statuses()
        subscriptions = self._subscriptions()
        server_rows_key = (
            primary_id,
            tuple(
                (
                    str(profile.get("id", "")),
                    str(profile.get("name", "")),
                    bool(profile.get("enabled", True)),
                    str(profile.get("country_hint", "")),
                )
                for profile in profiles
            ),
            tuple(
                (
                    str(subscription.get("id", "")),
                    str(subscription.get("url", "")),
                    tuple(str(profile_id) for profile_id in subscription.get("profile_ids", [])),
                    str(subscription.get("updated_at", "")),
                )
                for subscription in subscriptions
            ),
            tuple(
                sorted(
                    (
                        str(profile_id),
                        str(status.get("state", "")),
                        str(status.get("name", "")),
                        bool(status.get("live", False)),
                    )
                    for profile_id, status in live_statuses.items()
                    if isinstance(status, dict)
                )
            ),
            tuple(
                (
                    str(status.descriptor.id),
                    str(status.state),
                    str(status.message),
                )
                for status in builtin_statuses
            ),
        )
        if self._server_rows_key == server_rows_key:
            return
        self._server_rows_key = server_rows_key
        self._clear(self._server_rows)

        builtin_title = QLabel("Встроенные")
        builtin_title.setStyleSheet(f"color:{theme.ACCENT3};font-size:11px;font-weight:700;")
        self._server_rows.addWidget(builtin_title)
        for status in builtin_statuses:
            descriptor = status.descriptor
            details = f"Встроенный • {descriptor.region_label}\n{status.message}"
            row = _ServerRow(
                server_id=descriptor.id,
                name=descriptor.name,
                details=details,
                state=_PROVIDER_STATE_TEXT[status.state],
                color=_PROVIDER_STATE_COLOR[status.state],
                selectable=False,
            )
            self._server_rows.addWidget(row)

        subscription_title = QLabel("Мои подписки")
        subscription_title.setStyleSheet(f"color:{theme.ACCENT3};font-size:11px;font-weight:700;margin-top:7px;")
        self._server_rows.addWidget(subscription_title)
        subscription_profile_ids: set[str] = set()
        if not subscriptions:
            empty_subscriptions = QLabel(
                "Пока нет подписок. Вставьте HTTPS-ссылку слева и нажмите «Применить»."
            )
            empty_subscriptions.setWordWrap(True)
            empty_subscriptions.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
            self._server_rows.addWidget(empty_subscriptions)
        for subscription in subscriptions:
            source_id = str(subscription["id"])
            source_profiles = [
                profile_by_id[profile_id]
                for profile_id in subscription.get("profile_ids", [])
                if profile_id in profile_by_id
            ]
            subscription_profile_ids.update(str(profile["id"]) for profile in source_profiles)
            group = _SubscriptionGroup(
                subscription,
                source_profiles,
                expanded=source_id in self._expanded_subscription_ids,
                primary_id=primary_id,
                statuses=live_statuses,
            )
            group.serverSelected.connect(self._select_primary_server)
            group.refreshRequested.connect(self._refresh_subscription)
            group.deleteRequested.connect(self._delete_subscription)
            group.expandedChanged.connect(self._on_subscription_expanded)
            self._server_rows.addWidget(group)

        manual_profiles = [
            profile for profile in profiles if str(profile["id"]) not in subscription_profile_ids
        ]
        manual_title = QLabel("Добавленные вручную")
        manual_title.setStyleSheet(f"color:{theme.ACCENT3};font-size:11px;font-weight:700;margin-top:7px;")
        self._server_rows.addWidget(manual_title)
        if not manual_profiles:
            empty_manual = QLabel("Пока нет добавленных вручную серверов.")
            empty_manual.setWordWrap(True)
            empty_manual.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
            self._server_rows.addWidget(empty_manual)
        for profile in manual_profiles:
            selected = profile["id"] == primary_id
            state, color, details = self._manual_profile_state(
                profile,
                selected=selected,
                statuses=live_statuses,
            )
            if profile.get("country_hint"):
                details += f" • {profile['country_hint']}"
            row = _ServerRow(
                server_id=profile["id"],
                name=profile["name"],
                details=details,
                state=state,
                color=color,
                selectable=bool(profile["enabled"]),
                selected=selected,
                editable=True,
            )
            row.selected.connect(self._select_primary_server)
            row.editRequested.connect(self._edit_server)
            row.deleteRequested.connect(self._delete_server)
            self._server_rows.addWidget(row)
        self._server_rows.addStretch(1)
        self._server_hint.setText(
            "Раскройте подписку, чтобы увидеть её серверы и выбрать основной для следующего запуска VPN. "
            "«Авто» выбирается на странице «Маршрутизация»."
        )

    def _rebuild_protocol_rows(self) -> None:
        capabilities = vpn_protocol_capabilities()
        protocol_rows_key = tuple(repr(capability) for capability in capabilities)
        if self._protocol_rows_key == protocol_rows_key:
            return
        self._protocol_rows_key = protocol_rows_key
        self._clear(self._protocol_rows)
        if not capabilities:
            empty = QLabel("Данные о возможностях VPN-движка пока недоступны.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
            self._protocol_rows.addWidget(empty)
        for capability in capabilities:
            self._protocol_rows.addWidget(_ProtocolRow(capability))
        self._protocol_rows.addStretch(1)

    def _persist(self, message: str, color: str = theme.GREEN) -> None:
        save_config(self.engine.config)
        sync_routing_runtime_config(force=True)
        # Other tabs keep their already-rendered widgets until opened.  Tell
        # the shell that this local edit changed shared transport state.
        post_event({"type": "config_changed", "section": "transport_profiles"})
        self._left_status.setText(message)
        self._left_status.setStyleSheet(f"color:{color};font-size:11px;")
        self._rebuild_server_rows()
        self._rebuild_protocol_rows()

    def _on_subscription_expanded(self, source_id: str, expanded: bool) -> None:
        if expanded:
            self._expanded_subscription_ids.add(source_id)
        else:
            self._expanded_subscription_ids.discard(source_id)

    def _apply_subscription_url(self) -> None:
        self._start_subscription_refresh("", self._subscription_url.text())

    def _refresh_subscription(self, source_id: str) -> None:
        subscription = next((item for item in self._subscriptions() if item["id"] == source_id), None)
        if subscription is not None:
            self._start_subscription_refresh(source_id, str(subscription["url"]))

    def _start_subscription_refresh(self, source_id: str, url: str) -> None:
        if self._subscription_worker is not None and self._subscription_worker.isRunning():
            return
        self._subscription_url.setEnabled(False)
        self._apply_subscription.setEnabled(False)
        self._left_status.setText("⏳ Обновляем подписку…")
        self._left_status.setStyleSheet(f"color:{theme.YELLOW};font-size:11px;")
        worker = _SubscriptionFetchWorker(source_id, url, self)
        worker.completed.connect(self._on_subscription_fetched)
        worker.finished.connect(worker.deleteLater)
        self._subscription_worker = worker
        worker.start()

    def _on_subscription_fetched(self, requested_source_id: str, result: SubscriptionFetchResult) -> None:
        self._subscription_worker = None
        self._subscription_url.setEnabled(True)
        self._apply_subscription.setEnabled(True)
        if not result.profiles:
            self._left_status.setText(result.message)
            self._left_status.setStyleSheet(f"color:{theme.RED};font-size:11px;")
            return

        subscriptions = self._subscriptions()
        existing = next((item for item in subscriptions if item["id"] == requested_source_id), None)
        if existing is None:
            # Re-entering an already known URL acts as a refresh rather than
            # duplicating a token-bearing source record.
            existing = next((item for item in subscriptions if item["url"] == result.url), None)
        source_id = str(existing["id"]) if existing is not None else f"subscription-{uuid.uuid4().hex[:12]}"
        old_profile_ids = set(existing.get("profile_ids", [])) if existing else set()
        remaining_profiles = [
            profile for profile in self._custom_profiles()
            if str(profile["id"]) not in old_profile_ids
        ]
        imported_profiles = materialize_subscription_profiles(source_id, result.profiles)
        normalized_profiles, _warnings = sanitize_route_profiles(remaining_profiles + imported_profiles)
        if len(normalized_profiles) != len(remaining_profiles) + len(imported_profiles):
            self._left_status.setText(
                f"Подписка не изменена: UmbraNet может хранить до {MAX_ROUTE_PROFILES} "
                "HTTP CONNECT/SOCKS5 серверов одновременно."
            )
            self._left_status.setStyleSheet(f"color:{theme.RED};font-size:11px;")
            return

        profile_ids = [str(profile["id"]) for profile in imported_profiles]
        source = {
            "id": source_id,
            "url": result.url,
            "profile_ids": profile_ids,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        if existing is None:
            subscriptions.append(source)
        else:
            subscriptions = [source if item["id"] == source_id else item for item in subscriptions]
        normalized_subscriptions, subscription_warnings = sanitize_transport_subscriptions(subscriptions)
        if len(normalized_subscriptions) != len(subscriptions):
            self._left_status.setText(
                "; ".join(subscription_warnings) or "Не удалось сохранить подписку."
            )
            self._left_status.setStyleSheet(f"color:{theme.RED};font-size:11px;")
            return

        self.engine.config["transport_profiles"] = normalized_profiles
        self.engine.config["transport_subscriptions"] = normalized_subscriptions
        selected_profile_id = str(self.engine.config.get("transport_primary_profile_id", "") or "")
        if selected_profile_id in old_profile_ids and selected_profile_id not in profile_ids:
            self.engine.config["transport_primary_profile_id"] = ""
        self._expanded_subscription_ids.add(source_id)
        self._subscription_url.clear()
        self._persist(f"✓ {result.message}. Подписка обновлена.")

    def _delete_subscription(self, source_id: str) -> None:
        subscription = next((item for item in self._subscriptions() if item["id"] == source_id), None)
        if subscription is None:
            return
        profile_ids = {str(item) for item in subscription.get("profile_ids", [])}
        self.engine.config["transport_profiles"] = [
            profile for profile in self._custom_profiles()
            if str(profile["id"]) not in profile_ids
        ]
        self.engine.config["transport_subscriptions"] = [
            item for item in self._subscriptions() if item["id"] != source_id
        ]
        if str(self.engine.config.get("transport_primary_profile_id", "") or "") in profile_ids:
            self.engine.config["transport_primary_profile_id"] = ""
        self._expanded_subscription_ids.discard(source_id)
        self._persist("Подписка и её серверы удалены.", theme.MUTED)

    def _new_manual_server(self) -> None:
        self._editing_id = ""
        self._server_name.clear()
        self._server_protocol.setCurrentIndex(0)
        self._server_host.clear()
        self._server_port.setValue(1080)
        self._server_country.clear()
        self._server_login.clear()
        self._server_password.clear()
        self._server_enabled.setChecked(True)
        self._save_server.setText("Добавить сервер")
        self._server_name.setFocus()

    def _save_manual_server(self) -> None:
        profile_id = self._editing_id or f"server-{uuid.uuid4().hex[:12]}"
        candidate = {
            "id": profile_id,
            "name": self._server_name.text().strip(),
            "protocol": self._server_protocol.currentData(),
            "host": self._server_host.text().strip(),
            "port": self._server_port.value(),
            "country_hint": self._server_country.text().strip().upper(),
            "priority": 100,
            "username": self._server_login.text().strip(),
            "password": self._server_password.text(),
            "enabled": self._server_enabled.isChecked(),
        }
        current = [profile for profile in self._custom_profiles() if profile["id"] != profile_id]
        normalized, warnings = sanitize_route_profiles(current + [candidate])
        saved = next((profile for profile in normalized if profile["id"] == profile_id), None)
        if saved is None:
            self._left_status.setText("; ".join(warnings) or "Проверьте данные сервера.")
            self._left_status.setStyleSheet(f"color:{theme.RED};font-size:11px;")
            return
        self.engine.config["transport_profiles"] = normalized
        self._editing_id = profile_id
        self._save_server.setText("Сохранить сервер")
        self._persist("✓ Сервер сохранён. Изменение применяется после «Перезапуск».")

    def _edit_server(self, profile_id: str) -> None:
        profile = next((item for item in self._custom_profiles() if item["id"] == profile_id), None)
        if profile is None:
            return
        self._editing_id = profile_id
        self._add_tabs.setCurrentIndex(2)
        self._server_name.setText(profile["name"])
        index = self._server_protocol.findData(profile["protocol"])
        self._server_protocol.setCurrentIndex(index if index >= 0 else 0)
        self._server_host.setText(profile["host"])
        self._server_port.setValue(int(profile["port"]))
        self._server_country.setText(profile.get("country_hint", ""))
        self._server_login.setText(profile.get("username", ""))
        self._server_password.setText(profile.get("password", ""))
        self._server_enabled.setChecked(bool(profile.get("enabled", True)))
        self._save_server.setText("Сохранить сервер")
        self._left_status.setText(f"Редактирование: {profile['name']}")
        self._left_status.setStyleSheet(f"color:{theme.ACCENT3};font-size:11px;")

    def _delete_server(self, profile_id: str) -> None:
        profile = next((item for item in self._custom_profiles() if item["id"] == profile_id), None)
        if profile is None:
            return
        self.engine.config["transport_profiles"] = [
            item for item in self._custom_profiles() if item["id"] != profile_id
        ]
        if str(self.engine.config.get("transport_primary_profile_id", "") or "") == profile_id:
            self.engine.config["transport_primary_profile_id"] = ""
        if self._editing_id == profile_id:
            self._new_manual_server()
        self._persist(f"Сервер «{profile['name']}» удалён.", theme.MUTED)

    def _select_primary_server(self, profile_id: str) -> None:
        profile = next((item for item in self._custom_profiles() if item["id"] == profile_id), None)
        if profile is None or not profile.get("enabled"):
            return
        self.engine.config["transport_primary_profile_id"] = profile_id
        self._persist(f"✓ Основной сервер: {profile['name']}. Применится после «Перезапуск».")

    def _choose_file(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Выберите конфигурационный файл",
            "",
            "JSON (*.json);;Все файлы (*.*)",
        )
        if not path:
            return
        self._file_name.setText(path)
        result = import_custom_server_file(path)
        if not result.profiles:
            self._left_status.setText(result.message)
            self._left_status.setStyleSheet(f"color:{theme.RED};font-size:11px;")
            return

        existing = self._custom_profiles()
        known_ids = {profile["id"] for profile in existing}
        imported: list[dict] = []
        for profile in result.profiles:
            item = dict(profile)
            if item["id"] in known_ids:
                item["id"] = f"server-{uuid.uuid4().hex[:12]}"
            known_ids.add(item["id"])
            imported.append(item)
        normalized, warnings = sanitize_route_profiles(existing + imported)
        if len(normalized) < len(existing) + len(imported):
            self._left_status.setText("; ".join(warnings) or "Не удалось добавить серверы из файла.")
            self._left_status.setStyleSheet(f"color:{theme.RED};font-size:11px;")
            return
        self.engine.config["transport_profiles"] = normalized
        self._persist(f"✓ {result.message}. Добавлено в «Добавленные вручную».")

    def refresh(self) -> None:
        """Refresh displayed server and protocol state without starting a transport."""
        self._rebuild_server_rows()
        self._rebuild_protocol_rows()
