# -*- coding: utf-8 -*-
"""
UmbraNet — grik: хранение настроек графика пинга.

Свой файл grik/grik_config.json рядом с кодом: модуль grik самодостаточен
(«виджет как карта»), свои настройки не смешивает с общим состоянием UI.

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

import json
import pathlib

_PATH = pathlib.Path(__file__).with_name("grik_config.json")

DEFAULTS = {
    "mode": "bars",        # smooth | angular | bars
    "grid": 5,             # 2..10
    "interval_ms": 2000,   # 1000..15000
    "height": 140,         # 90..240
}


def get_graph_settings() -> dict:
    """Загружает настройки графика пинга (с нормализацией и дефолтами)."""
    out = dict(DEFAULTS)
    try:
        st = json.loads(_PATH.read_text(encoding="utf-8")).get("latency_graph", {})
    except Exception:
        st = {}
    if not isinstance(st, dict):
        st = {}
    mode = str(st.get("mode", out["mode"])).lower()
    out["mode"] = mode if mode in ("smooth", "angular", "bars") else DEFAULTS["mode"]
    try:
        out["grid"] = max(2, min(10, int(st.get("grid", out["grid"]))))
    except Exception:
        pass
    try:
        out["interval_ms"] = max(1000, min(15000, int(st.get("interval_ms", out["interval_ms"]))))
    except Exception:
        pass
    try:
        out["height"] = max(90, min(240, int(st.get("height", out["height"]))))
    except Exception:
        pass
    return out


def set_graph_settings(settings: dict) -> None:
    """Сохраняет настройки графика пинга."""
    cur = get_graph_settings()
    if isinstance(settings, dict):
        cur.update(settings)
    try:
        _PATH.write_text(
            json.dumps({"latency_graph": cur}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass
