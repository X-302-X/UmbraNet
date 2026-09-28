"""
UmbraNet — контроллер списка DPI-стратегий (логика выбора).

Телеграмизация: рисование строк — DpiStrategyCanvas (один paintEvent),
здесь только бизнес-логика: активация стратегии, перезапуск WinWS.
Список показывает ТОЛЬКО стратегии из реальной папки UmbraNet/strategies.

Внешний API прежний: strategyChanged(str), refresh().

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from umbranet import engine_adapter as ea
from umbranet.widgets.dpi_strategy_canvas import DpiStrategyCanvas

# видимая высота списка — как в старой версии (7 строк + зазоры)
_VIEW_H = 7 * 48 + 6 * 5


class DpiStrategyList(QWidget):
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
        lay.addWidget(self._canvas)

        self._rebuild_rows()

    # ── данные ───────────────────────────────────────────────────────────

    def _strategy_items(self) -> list[dict]:
        """Единый источник списка: engine_adapter читает UmbraNet/strategies."""
        return list(ea.dpi_strategy_items() or [])

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
            # пустое состояние — рисует канвас
            self._rows_data.append({
                "key": "", "name": "Стратегий нет в папке strategies",
                "desc": "", "active": False,
            })
        self._restyle()

    def _restyle(self):
        for row in self._rows_data:
            row["active"] = bool(row["key"]) and row["key"] == self._active
        self._canvas.set_rows(self._rows_data)

    # ── выбор ────────────────────────────────────────────────────────────

    def _select(self, key: str):
        key = str(key or "").strip().lower()
        if not key or key == self._active:
            return

        ok, msg = ea.dpi_strategy_set_active(key)
        if not ok:
            import logging
            logging.getLogger("UmbraNet.DpiStrategyList").warning(
                "Не удалось выбрать стратегию %s: %s", key, msg)
            return

        self._active = key

        # Перезапускаем WinWS, если DPI сейчас активен, чтобы применить стратегию.
        try:
            eng = ea.get_engine()
            current_mode = eng.config.get("dpi_mode", "off")
            if current_mode != "off" and getattr(eng, "winws", None) and eng.winws.is_running():
                from strategy_manager import StrategyManager  # type: ignore
                manager = StrategyManager(ea.get_strategies_dir())
                routed_targets = list(eng.config.get("routed_domains", []) or [])
                routed_targets += list(eng.config.get("subscribed_domains_set", set()) or [])
                args = manager.get_args(
                    key,
                    routed_domains=routed_targets,
                    require_hostlist=True,
                )
                if args:
                    import logging
                    logging.getLogger("UmbraNet.DpiStrategyList").info(
                        "Смена стратегии на %s, перезапуск WinWS", key)
                    eng.winws.restart(args)
                else:
                    import logging
                    logging.getLogger("UmbraNet.DpiStrategyList").warning(
                        "WinWS не перезапущен: %s", manager.last_error
                    )
        except Exception as exc:
            import logging
            logging.getLogger("UmbraNet.DpiStrategyList").error(
                "Ошибка перезапуска WinWS: %s", exc)

        self._restyle()
        self.strategyChanged.emit(key)

    # ── публичное ────────────────────────────────────────────────────────

    def refresh(self):
        self._active = str(ea.get_engine().config.get("dpi_strategy", "uz1")).lower()
        self._rebuild_rows()
