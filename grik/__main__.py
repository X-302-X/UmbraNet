# -*- coding: utf-8 -*-
"""
UmbraNet — grik: запуск графика пинга отдельным окном.

    python -m grik        (из корня проекта)

Окно с панелью графиков и её настройками — посмотреть виджет и настроить
его, не встраивая в главное меню. Замер в standalone-режиме пингует
публичный DNS 1.1.1.1.

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from umbranet import theme

from grik.panel import PingGraphPanel


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(
        f"QWidget{{background:{theme.BG};color:{theme.TEXT};font-family:'Segoe UI';}}"
    )

    win = QWidget()
    win.setWindowTitle("UmbraNet — график пинга (grik)")
    win.resize(430, 620)

    lay = QVBoxLayout(win)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(8)

    hint = QLabel("Standalone-режим: замер пингует публичный DNS 1.1.1.1.\n"
                  "Настройки (⚙︎) — частота обновления, вид графика и прочее.")
    hint.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;background:transparent;border:none;")
    hint.setWordWrap(True)
    lay.addWidget(hint)

    panel = PingGraphPanel()
    lay.addWidget(panel)
    lay.addStretch()

    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
