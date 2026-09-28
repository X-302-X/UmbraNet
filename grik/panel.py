# -*- coding: utf-8 -*-
"""
UmbraNet — grik: панель графиков пинга (DNS + DPI).

Самодостаточный виджет «как карта»: заголовок с кнопками (очистить /
настроить), два графика с подписями «Текущий/Средний», таймер опроса и
фоновый замер пинга. Вынесен ЦЕЛИКОМ из главного меню («Маршрутизация»)
по итогам диагностики лагов: тики таймера графиков во время живого resize
окна превращали перетаскивание края в слайд-шоу (чем выше частота
обновления — тем хуже).

Код отрисовки и замера 1:1 с прежним; меняется только «прописка».

Встраивание (когда решим вернуть):
    from grik import PingGraphPanel
    panel = PingGraphPanel(context_provider=my_provider)
    lay.addWidget(panel)
где my_provider() -> (profile: dict, dns_mode: str, routed_domains: list,
                      measure_dns: bool, measure_dpi: bool)

Запуск отдельно (посмотреть/настроить):  python -m grik

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme

from grik.config_store import get_graph_settings, set_graph_settings
from grik.graph_config import GraphConfigDialog
from grik.ping_graph import Sparkline
from grik.ping_worker import PingWorker


def _default_context():
    """Контекст замера для standalone-режима: пинг публичного DNS."""
    return ({"ipv4_primary": "1.1.1.1", "doh_url": ""}, "udp", [], True, False)


class PingGraphPanel(QWidget):
    """Секция «Пинг сети»: два графика + таймер + окно настроек."""

    def __init__(self, context_provider=None, parent: QWidget | None = None):
        super().__init__(parent)
        self._context_provider = context_provider or _default_context
        self._settings = get_graph_settings()
        self._ping_worker: PingWorker | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ── Заголовок области пинга сети ──
        lat_head = QHBoxLayout()
        self._lat_title = QLabel("📈  Пинг")
        self._lat_title.setStyleSheet(
            f"color:{theme.WHITE};font-size:13px;font-weight:700;background:transparent;border:none;")
        lat_head.addWidget(self._lat_title)
        lat_head.addStretch()

        self._graph_btn = QPushButton("⚙︎")
        self._graph_btn.setCursor(Qt.PointingHandCursor)
        self._graph_btn.setFixedSize(30, 26)
        self._graph_btn.setToolTip("Настройки графиков")
        self._graph_btn.setStyleSheet(
            f"QPushButton{{background:{theme.INPUT_BG};color:{theme.ACCENT3};"
            f"border:1px solid {theme.BORDER};border-radius:8px;"
            "font-family:'Segoe UI Symbol';font-size:15px;font-weight:700;"
            "padding-bottom:1px;}"
            f"QPushButton:hover{{border-color:{theme.ACCENT3};color:{theme.WHITE};background:{theme.CARD};}}"
        )

        self._graph_clear_btn = QPushButton("🧹")
        self._graph_clear_btn.setCursor(Qt.PointingHandCursor)
        self._graph_clear_btn.setFixedSize(30, 26)
        self._graph_clear_btn.setToolTip("Очистить графики")
        self._graph_clear_btn.setStyleSheet(
            f"QPushButton{{background:{theme.INPUT_BG};color:{theme.SUBTEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;"
            "font-size:13px;font-weight:700;padding-bottom:1px;}"
            f"QPushButton:hover{{border-color:{theme.ACCENT3};color:{theme.WHITE};background:{theme.CARD};}}"
        )
        self._graph_clear_btn.clicked.connect(self._clear_ping_graph)
        lat_head.addWidget(self._graph_clear_btn)

        self._graph_btn.clicked.connect(self._open_graph_settings)
        lat_head.addWidget(self._graph_btn)
        lay.addLayout(lat_head)

        h = int(self._settings.get("height", 140))

        # ── 1) Контейнер для Пинга DPI (Стратегия) ──
        self._dpi_graph_container = QWidget()
        self._dpi_graph_container.setStyleSheet("background:transparent;border:none;")
        dpi_g_lay = QVBoxLayout(self._dpi_graph_container)
        dpi_g_lay.setContentsMargins(0, 0, 0, 0)
        dpi_g_lay.setSpacing(4)

        dpi_lbl = QLabel("🛡 Пинг DPI (Стратегия)")
        dpi_lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:10px;font-weight:bold;background:transparent;")
        dpi_g_lay.addWidget(dpi_lbl)

        self._dpi_spark = Sparkline(
            capacity=40,
            mode=self._settings.get("mode", "bars"),
            grid_size=int(self._settings.get("grid", 5)),
        )
        self._dpi_spark.setFixedHeight(h)
        dpi_g_lay.addWidget(self._dpi_spark)

        dpi_row = QHBoxLayout()
        self._dpi_lat_cur = QLabel("Текущий: —")
        self._dpi_lat_cur.setStyleSheet(f"color:{theme.ACCENT};font-size:11px;background:transparent;border:none;")
        self._dpi_lat_avg = QLabel("Сред.: —")
        self._dpi_lat_avg.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;background:transparent;border:none;")
        dpi_row.addWidget(self._dpi_lat_cur)
        dpi_row.addStretch()
        dpi_row.addWidget(self._dpi_lat_avg)
        dpi_g_lay.addLayout(dpi_row)

        lay.addWidget(self._dpi_graph_container)

        # ── 2) Контейнер для Пинга DNS (Маршрут) ──
        self._dns_graph_container = QWidget()
        self._dns_graph_container.setStyleSheet("background:transparent;border:none;")
        dns_g_lay = QVBoxLayout(self._dns_graph_container)
        dns_g_lay.setContentsMargins(0, 0, 0, 0)
        dns_g_lay.setSpacing(4)

        dns_lbl = QLabel("🔌 Пинг DNS (Маршрут)")
        dns_lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:10px;font-weight:bold;background:transparent;")
        dns_g_lay.addWidget(dns_lbl)

        self._dns_spark = Sparkline(
            capacity=40,
            mode=self._settings.get("mode", "bars"),
            grid_size=int(self._settings.get("grid", 5)),
        )
        self._dns_spark.setFixedHeight(h)
        dns_g_lay.addWidget(self._dns_spark)

        dns_row = QHBoxLayout()
        self._dns_lat_cur = QLabel("Текущий: —")
        self._dns_lat_cur.setStyleSheet(f"color:{theme.ACCENT2};font-size:11px;background:transparent;border:none;")
        self._dns_lat_avg = QLabel("Сред.: —")
        self._dns_lat_avg.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;background:transparent;border:none;")
        dns_row.addWidget(self._dns_lat_cur)
        dns_row.addStretch()
        dns_row.addWidget(self._dns_lat_avg)
        dns_g_lay.addLayout(dns_row)

        lay.addWidget(self._dns_graph_container)

        # Таймер опроса пингов
        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(int(self._settings.get("interval_ms", 2000)))
        self._ping_timer.timeout.connect(self._do_ping)
        self._ping_timer.start()
        QTimer.singleShot(400, self._do_ping)

    # ── публичное ──

    def set_mode_visible(self, mode: str):
        """Видимость графиков по режиму приложения: dns_only / dpi_only / combo."""
        h = int(self._settings.get("height", 140))
        if mode == "dns_only":
            self._dns_spark.setFixedHeight(h)
            self._dns_graph_container.setVisible(True)
            self._dpi_graph_container.setVisible(False)
            self._lat_title.setText("📈  Пинг DNS")
        elif mode == "dpi_only":
            self._dpi_spark.setFixedHeight(h)
            self._dns_graph_container.setVisible(False)
            self._dpi_graph_container.setVisible(True)
            self._lat_title.setText("📈  Пинг DPI")
        else:  # combo
            combo_h = max(60, h // 2)
            self._dns_spark.setFixedHeight(combo_h)
            self._dpi_spark.setFixedHeight(combo_h)
            self._dns_graph_container.setVisible(True)
            self._dpi_graph_container.setVisible(True)
            self._lat_title.setText("📈  Пинг сети")

    def stop(self):
        """Остановить опрос (вызывать при скрытии панели из UI)."""
        self._ping_timer.stop()

    def start(self):
        self._ping_timer.start()
        self._do_ping()

    # ── внутреннее (1:1 с прежней логикой главного меню) ──

    def _clear_ping_graph(self):
        """Очищает историю графиков пинга вручную."""
        self._dns_spark.set_data([])
        self._dpi_spark.set_data([])
        self._dns_lat_cur.setText("Текущий: —")
        self._dns_lat_avg.setText("Сред.: —")
        self._dpi_lat_cur.setText("Текущий: —")
        self._dpi_lat_avg.setText("Сред.: —")
        QTimer.singleShot(150, self._do_ping)

    def _open_graph_settings(self):
        dlg = GraphConfigDialog(self._settings, self)
        if not dlg.exec():
            return
        self._settings = dlg.result
        set_graph_settings(self._settings)

        mode = self._settings.get("mode", "bars")
        grid = int(self._settings.get("grid", 5))
        self._dns_spark.set_mode(mode)
        self._dns_spark.set_grid_size(grid)
        self._dpi_spark.set_mode(mode)
        self._dpi_spark.set_grid_size(grid)

        self._ping_timer.setInterval(int(self._settings.get("interval_ms", 2000)))

    def _do_ping(self):
        if self._ping_worker is not None and self._ping_worker.isRunning():
            return

        try:
            profile, dns_mode, routed_domains, measure_dns, measure_dpi = self._context_provider()
        except Exception:
            return

        self._ping_worker = PingWorker(profile, dns_mode, routed_domains, measure_dns, measure_dpi)
        self._ping_worker.done.connect(self._on_ping_done)
        self._ping_worker.start()

    def _on_ping_done(self, ok: bool, dns_ms, dpi_ms):
        if dns_ms is not None:
            self._dns_spark.push(dns_ms)
            self._dns_lat_cur.setText(f"Текущий: {dns_ms} мс")
            avg = self._dns_spark.avg
            self._dns_lat_avg.setText(f"Сред.: {int(avg)} мс" if avg else "Сред.: —")

        if dpi_ms is not None:
            self._dpi_spark.push(dpi_ms)
            self._dpi_lat_cur.setText(f"Текущий: {dpi_ms} мс")
            avg = self._dpi_spark.avg
            self._dpi_lat_avg.setText(f"Сред.: {int(avg)} мс" if avg else "Сред.: —")
