"""Friendly VPN-server picker for the Routing page.

Routing presents a person-facing server choice only.  Technical endpoint fields
live in the separate ``👁️ Сервера`` page, and ``Авто`` is a routing policy rather
than a stored server, so it never appears in that page's library.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from core.vpn.providers import BuiltinProviderCatalog, ProviderState
from vpn.profiles import sanitize_route_profiles
from umbranet import theme
from umbranet.engine_adapter import save_config, vpn_server_statuses
from umbranet.widgets.dpi_strategy_canvas import DpiStrategyCanvas

_AUTO_KEY = "__umbranet_auto_server__"
_VIEW_H = 10 * 48 + 9 * 5
_PROVIDER_STATE_TEXT = {
    ProviderState.AVAILABLE: "Готов",
    ProviderState.NOT_INSTALLED: "Не установлен",
    ProviderState.CONFIGURATION_REQUIRED: "Подготавливается",
    ProviderState.SECURITY_REVIEW: "Проверка безопасности",
    ProviderState.UNSUPPORTED_PLATFORM: "Только Windows",
    ProviderState.UNAVAILABLE: "Недоступен",
}


class VpnExitList(QWidget):
    """Choose Auto or one friendly server without exposing transport details."""

    exitChanged = Signal(str)
    serverSetupRequested = Signal()

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._catalog = BuiltinProviderCatalog()
        self._rows: list[dict] = []
        self._render_key: tuple[object, ...] | None = None
        self.setStyleSheet("background:transparent;border:none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._canvas = DpiStrategyCanvas()
        self._canvas.setFixedHeight(_VIEW_H)
        self._canvas.rowClicked.connect(self._select)
        layout.addWidget(self._canvas)

        self._hint = QLabel()
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;")
        layout.addWidget(self._hint)
        self._add_server = QPushButton("＋ Добавить сервер")
        self._add_server.setCursor(Qt.PointingHandCursor)
        self._add_server.setFixedHeight(30)
        self._add_server.setStyleSheet(
            f"QPushButton{{background:{theme.INPUT_BG};color:{theme.TEXT};border:1px solid {theme.BORDER};"
            "border-radius:8px;padding:0 10px;font-size:11px;font-weight:700;}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.ACCENT3};}}"
        )
        self._add_server.clicked.connect(self.serverSetupRequested)
        layout.addWidget(self._add_server)
        self.refresh()

    def set_page_active(self, active: bool) -> None:
        """Refresh once when Routing becomes visible; health changes are pushed."""
        if active:
            self._rebuild_rows()

    def set_config(self, config: dict) -> None:
        if isinstance(config, dict):
            self._config = config
        self.refresh()

    def _profiles(self) -> list[dict]:
        profiles, _warnings = sanitize_route_profiles(self._config.get("transport_profiles", []))
        return profiles

    def _rebuild_rows(self) -> None:
        profiles = self._profiles()
        configured_primary = str(self._config.get("transport_primary_profile_id", "") or "").strip()
        profile_ids = {str(profile["id"]) for profile in profiles}
        manual_primary = configured_primary if configured_primary in profile_ids else ""
        statuses = vpn_server_statuses()
        active_status = next(
            (
                status for status in statuses.values()
                if bool(status.get("live")) and str(status.get("state", "")) in {"active", "degraded"}
            ),
            {},
        )
        active_name = str(active_status.get("name", "") or "")
        active_state = str(active_status.get("state", "") or "")
        auto_desc = "UmbraNet сам выбирает готовый сервер и меняет его при сбое"
        if active_name:
            auto_desc = f"Сейчас: {active_name} • {'подключён' if active_state == 'active' else 'есть сбои'}"

        self._rows = [{
            "key": _AUTO_KEY,
            "name": "Авто",
            "desc": auto_desc,
            "active": not manual_primary,
            "enabled": True,
            "status_color": theme.GREEN if active_state == "active" else (theme.YELLOW if active_state == "degraded" else ""),
        }]
        for status in self._catalog.statuses():
            self._rows.append({
                "key": status.descriptor.id,
                "name": status.descriptor.name,
                "desc": f"Встроенный • {_PROVIDER_STATE_TEXT[status.state]}",
                "active": False,
                "enabled": status.selectable,
            })
        for profile in profiles:
            profile_id = str(profile["id"])
            enabled = bool(profile.get("enabled", True))
            health = statuses.get(profile_id, {})
            readiness = str(health.get("state", "") or "")
            status_color = ""
            underline = False
            if not enabled:
                description = "Отключён в «👁️ Сервера»"
            elif readiness == "failed":
                description = "Недоступен: проверка готовности не пройдена"
                status_color = theme.RED
                underline = profile_id == manual_primary
            elif readiness == "active":
                description = "Подключён сейчас"
                status_color = theme.GREEN
            elif readiness == "degraded":
                description = "Есть сбои; повторная проверка выполняется"
                status_color = theme.YELLOW
            elif readiness == "warm":
                description = "Проверен и готов"
                status_color = theme.GREEN
            elif readiness == "checking":
                description = "Проверяется"
                status_color = theme.YELLOW
            elif readiness == "not_verified":
                description = "Не проверен: нужен HTTPS endpoint проверки"
                status_color = theme.YELLOW
            elif profile_id == manual_primary:
                description = "Выбран вручную; будет проверен перед подключением"
            else:
                description = "Можно выбрать сейчас; готовность проверится перед запуском"
            self._rows.append({
                "key": profile_id,
                "name": str(profile.get("name", profile_id)),
                "desc": description,
                "active": enabled and profile_id == manual_primary,
                "enabled": enabled,
                "status_color": status_color,
                "status_underline": underline,
            })

        if not profiles:
            hint_text = (
                "Пока можно выбрать только «Авто». Добавьте свой сервер или HTTPS-подписку: "
                "его можно выбрать ещё до проверки, а при сбое UmbraNet не использует DIRECT."
            )
        else:
            hint_text = (
                "Любой включённый пользовательский сервер можно выбрать до проверки. «Авто» "
                "сам заменяет сервер при сбое; при ручном выборе UmbraNet предложит готовый резерв."
            )
        render_key = (
            tuple(
                (
                    str(row.get("key", "")),
                    str(row.get("name", "")),
                    str(row.get("desc", "")),
                    bool(row.get("active", False)),
                    bool(row.get("enabled", False)),
                    str(row.get("status_color", "")),
                    bool(row.get("status_underline", False)),
                )
                for row in self._rows
            ),
            hint_text,
            bool(not profiles),
        )
        if self._render_key == render_key:
            return
        self._render_key = render_key
        self._hint.setText(hint_text)
        self._add_server.setVisible(not profiles)
        self._canvas.set_rows(self._rows)

    def _select(self, key: str) -> None:
        key = str(key or "").strip()
        if not key:
            return
        row = next((item for item in self._rows if item["key"] == key), None)
        if row is None or not row.get("enabled"):
            self._hint.setText("Этот сервер пока не готов. Его статус указан на странице «👁️ Сервера».")
            return
        target_id = "" if key == _AUTO_KEY else key
        if target_id == str(self._config.get("transport_primary_profile_id", "") or "").strip():
            return
        self._config["transport_primary_profile_id"] = target_id
        save_config(self._config)
        self._rebuild_rows()
        self.exitChanged.emit(target_id)

    def refresh(self) -> None:
        self._rebuild_rows()
