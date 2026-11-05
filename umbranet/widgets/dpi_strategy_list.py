"""Main Routing-page DPI strategy picker and its shared context actions."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QInputDialog, QMessageBox, QVBoxLayout, QWidget

from umbranet import engine_adapter as ea
from umbranet.widgets.dpi_strategy_canvas import DpiStrategyCanvas
from umbranet.widgets.dpi_strategy_menu import build_dpi_strategy_menu

# Visible height retains the compact seven-row main-menu selector.
_VIEW_H = 7 * 48 + 6 * 5
_LOG = logging.getLogger("UmbraNet.DpiStrategyList")


class DpiStrategyList(QWidget):
    """Choose a strategy by click; expose the full strategy menu on right-click."""

    strategyChanged = Signal(str)

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background:transparent;border:none;")

        self._rows_data: list[dict] = []
        self._active = str(ea.get_engine().config.get("dpi_strategy", "uz1")).lower()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._canvas = DpiStrategyCanvas()
        self._canvas.setFixedHeight(_VIEW_H)
        self._canvas.rowClicked.connect(self._select)
        self._canvas.rowContextRequested.connect(self._show_context_menu)
        lay.addWidget(self._canvas)

        self._rebuild_rows()

    # ── data ────────────────────────────────────────────────────────────

    def _strategy_items(self) -> list[dict]:
        """The adapter remains the single source of strategy metadata."""
        return list(ea.dpi_strategy_items() or [])

    def _strategy_item(self, strategy_id: str) -> dict | None:
        wanted = str(strategy_id or "").strip().lower()
        return next(
            (item for item in self._strategy_items()
             if str(item.get("id", "")).strip().lower() == wanted),
            None,
        )

    def _rebuild_rows(self):
        strategies = self._strategy_items()
        self._rows_data = []
        for strat in strategies:
            key = str(strat.get("id", "")).strip().lower()
            if not key:
                continue
            self._rows_data.append({
                "key": key,
                "name": str(strat.get("name", key)),
                "desc": str(strat.get("description", "") or ""),
                "active": key == self._active,
            })
        if not self._rows_data:
            self._rows_data.append({
                "key": "", "name": "Стратегий нет в папке strategies",
                "desc": "", "active": False,
            })
        self._restyle()

    def _restyle(self):
        for row in self._rows_data:
            row["active"] = bool(row["key"]) and row["key"] == self._active
        self._canvas.set_rows(self._rows_data)

    # ── selection ───────────────────────────────────────────────────────

    def _select(self, key: str):
        key = str(key or "").strip().lower()
        if not key or key == self._active:
            return

        ok, message = ea.dpi_strategy_set_active(key)
        if not ok:
            _LOG.warning("Не удалось выбрать DPI-стратегию %s: %s", key, message)
            return

        self._active = key

        # Apply an explicit selection to a running DPI session without making
        # the right-click path weaker than a normal left-click selection.
        try:
            engine = ea.get_engine()
            current_mode = engine.config.get("dpi_mode", "off")
            if current_mode != "off" and getattr(engine, "winws", None) and engine.winws.is_running():
                from strategy_manager import StrategyManager  # type: ignore
                manager = StrategyManager(ea.get_strategies_dir())
                routed_targets = list(engine.config.get("routed_domains", []) or [])
                routed_targets += list(engine.config.get("subscribed_domains_set", set()) or [])
                args = manager.get_args(key, routed_domains=routed_targets, require_hostlist=True)
                if args:
                    _LOG.info("Смена DPI-стратегии на %s, перезапуск WinWS", key)
                    engine.winws.restart(args)
                else:
                    _LOG.warning("WinWS не перезапущен: %s", manager.last_error)
        except Exception as exc:  # runtime application failure is non-fatal to saved selection
            _LOG.error("Ошибка перезапуска WinWS после выбора стратегии: %s", exc)

        self._restyle()
        self.strategyChanged.emit(key)

    # ── shared context-menu actions ─────────────────────────────────────

    def _show_context_menu(self, strategy_id: str, global_pos) -> None:
        item = self._strategy_item(strategy_id)
        if item is None:
            return
        menu = build_dpi_strategy_menu(
            self,
            is_active=bool(item.get("active")),
            can_duplicate=len(self._strategy_items()) < ea.DPI_STRATEGY_LIMIT,
            activate=lambda: self._select(strategy_id),
            duplicate=lambda: self._duplicate(strategy_id),
            rename=lambda: self._rename(strategy_id),
            edit_description=lambda: self._edit_description(strategy_id),
            show_in_folder=lambda: self._show_in_folder(strategy_id),
            copy_path=lambda: self._copy_path(strategy_id),
            delete=lambda: self._delete(strategy_id),
        )
        menu.exec(global_pos)

    def _duplicate(self, strategy_id: str) -> None:
        ok, message, _new_id = ea.dpi_strategy_duplicate(strategy_id)
        if not ok:
            _LOG.warning("Не удалось создать копию DPI-стратегии %s: %s", strategy_id, message)
        self.refresh()

    def _rename(self, strategy_id: str) -> None:
        item = self._strategy_item(strategy_id)
        if item is None:
            return
        text, accepted = QInputDialog.getText(
            self,
            "Переименовать стратегию",
            "Новое имя:",
            text=str(item.get("name", strategy_id)),
        )
        if not accepted:
            return
        ok, message = ea.dpi_strategy_update_meta(strategy_id, name=text)
        if not ok:
            _LOG.warning("Не удалось переименовать DPI-стратегию %s: %s", strategy_id, message)
        self.refresh()

    def _edit_description(self, strategy_id: str) -> None:
        item = self._strategy_item(strategy_id)
        if item is None:
            return
        text, accepted = QInputDialog.getMultiLineText(
            self,
            "Описание стратегии",
            "Описание:",
            str(item.get("description", "")),
        )
        if not accepted:
            return
        ok, message = ea.dpi_strategy_update_meta(strategy_id, description=text)
        if not ok:
            _LOG.warning("Не удалось изменить описание DPI-стратегии %s: %s", strategy_id, message)
        self.refresh()

    def _strategy_path(self, strategy_id: str) -> Path | None:
        item = self._strategy_item(strategy_id)
        if item is None:
            return None
        directory = Path(ea.get_strategies_dir()).resolve()
        candidates = [directory / f"{strategy_id}.json"]
        raw_path = str(item.get("path", "") or "").strip()
        if raw_path:
            path = Path(raw_path).expanduser()
            candidates.append(path if path.is_absolute() else directory / path.name)
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        return None

    def _show_in_folder(self, strategy_id: str) -> None:
        path = self._strategy_path(strategy_id)
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _copy_path(self, strategy_id: str) -> None:
        path = self._strategy_path(strategy_id)
        item = self._strategy_item(strategy_id) or {}
        QGuiApplication.clipboard().setText(str(path) if path else str(item.get("path", "")))

    def _delete(self, strategy_id: str) -> None:
        item = self._strategy_item(strategy_id)
        if item is None:
            return
        if bool(item.get("active")):
            message = (
                f"Стратегия {strategy_id} сейчас активна.\n\n"
                "Если удалить её, UmbraNet автоматически выберет другую доступную стратегию.\n\n"
                "Продолжить?"
            )
        else:
            message = f"Удалить стратегию {strategy_id} из папки strategies?\n\nФайл будет удалён с диска."
        answer = QMessageBox.warning(
            self,
            "Удалить стратегию",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ok, result_message = ea.dpi_strategy_delete(strategy_id)
        if not ok:
            _LOG.warning("Не удалось удалить DPI-стратегию %s: %s", strategy_id, result_message)
        self.refresh()

    # ── public ──────────────────────────────────────────────────────────

    def refresh(self):
        self._active = str(ea.get_engine().config.get("dpi_strategy", "uz1")).lower()
        self._rebuild_rows()
