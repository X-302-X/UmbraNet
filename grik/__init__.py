# -*- coding: utf-8 -*-
"""
UmbraNet — grik: график пинга (standalone-виджет, «как карта»).

Папка хранит ВСЁ, что относилось к графикам пинга главного меню:
  • ping_graph.py   — виджет графика (бывший umbranet/widgets/sparkline.py);
  • ping_worker.py  — фоновый замер пинга DNS/DPI (бывший _PingWorker);
  • graph_config.py — окно настроек графика: частота, вид, сетка, высота;
  • config_store.py — хранение настроек (grik/grik_config.json);
  • panel.py        — готовая секция «Пинг сети» с таймером;
  • __main__.py     — запуск отдельно: python -m grik.

Из главного меню («Маршрутизация») график полностью удалён — тики его
таймера во время живого resize окна давали «слайд-шоу» (диагностика
юзера: чем выше частота обновления в настройках, тем сильнее).

UmbraNet_Official / X-302-X, GPLv3.
"""

from grik.config_store import get_graph_settings, set_graph_settings
from grik.graph_config import GraphConfigDialog
from grik.panel import PingGraphPanel
from grik.ping_graph import Sparkline
from grik.ping_worker import PingWorker

# понятный алиас: «график пинга»
PingGraph = Sparkline

__all__ = [
    "Sparkline",
    "PingGraph",
    "PingWorker",
    "GraphConfigDialog",
    "PingGraphPanel",
    "get_graph_settings",
    "set_graph_settings",
]
