"""
UmbraNet - системный трей (нативный QSystemTrayIcon).

В отличие от старой версии (pystray + Pillow), используем встроенный в Qt
QSystemTrayIcon — без лишних зависимостей и без отдельного потока.

Иконка меняет цвет по состоянию DNS-сервера:
  • зелёная  — работает
  • красная  — остановлен
  • жёлтая   — переходное состояние (перезапуск)

Меню: Запустить / Остановить / Перезапустить / Показать окно / Выход.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from umbranet import theme


def _make_icon(color: str, size: int = 64) -> QIcon:
    """Цветной круг с лёгкой обводкой -> QIcon."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    m = size // 8
    p.setBrush(QBrush(QColor(color)))
    p.setPen(QPen(QColor(255, 255, 255, 40), 2))
    p.drawEllipse(m, m, size - 2 * m, size - 2 * m)
    p.end()
    return QIcon(pm)


class Tray:
    """
    Обёртка над QSystemTrayIcon. Создаётся из MainWindow.

    callbacks: dict с ключами start/stop/restart/show/quit -> вызываемые.
    """

    def __init__(self, window, callbacks: dict):
        self._win = window
        self._cb = callbacks
        self._running = False

        self._ic_running = _make_icon(theme.GREEN)
        self._ic_stopped = _make_icon(theme.RED)
        self._ic_waiting = _make_icon(theme.YELLOW)

        self._tray = QSystemTrayIcon(self._ic_stopped, window)
        self._tray.setToolTip(f"{theme.APP_NAME} — DNS остановлен")
        self._tray.activated.connect(self._on_activated)

        self._menu = QMenu()
        self._menu.setStyleSheet(
            f"QMenu{{background:{theme.CARD};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:6px;}}"
            f"QMenu::item{{padding:6px 22px;border-radius:6px;}}"
            f"QMenu::item:selected{{background:{theme.ACCENT};color:{theme.WHITE};}}"
            f"QMenu::item:disabled{{color:{theme.MUTED};}}"
            f"QMenu::separator{{height:1px;background:{theme.BORDER};margin:4px 8px;}}")

        self._act_start = QAction("▶  Запустить DNS", self._menu)
        self._act_start.triggered.connect(lambda: self._cb.get("start", lambda: None)())
        self._act_stop = QAction("■  Остановить", self._menu)
        self._act_stop.triggered.connect(lambda: self._cb.get("stop", lambda: None)())
        self._act_restart = QAction("⟳  Перезапустить", self._menu)
        self._act_restart.triggered.connect(lambda: self._cb.get("restart", lambda: None)())
        self._act_show = QAction("🗗  Показать окно", self._menu)
        self._act_show.triggered.connect(lambda: self._cb.get("show", lambda: None)())
        self._act_quit = QAction("✕  Полностью выйти", self._menu)
        self._act_quit.triggered.connect(lambda: self._cb.get("quit", lambda: None)())

        self._menu.addAction(self._act_start)
        self._menu.addAction(self._act_stop)
        self._menu.addAction(self._act_restart)
        self._menu.addSeparator()
        self._menu.addAction(self._act_show)
        self._menu.addSeparator()
        self._menu.addAction(self._act_quit)
        self._tray.setContextMenu(self._menu)

    # ── публичное API ──
    def show(self):
        self._tray.show()

    def hide(self):
        self._tray.hide()

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def set_running(self, running: bool):
        self._running = running
        self._tray.setIcon(self._ic_running if running else self._ic_stopped)
        self._tray.setToolTip(
            f"{theme.APP_NAME} — DNS {'работает' if running else 'остановлен'}")
        self._act_start.setEnabled(not running)
        self._act_stop.setEnabled(running)
        self._act_restart.setEnabled(running)

    def set_waiting(self, message: str = "Ожидание..."):
        self._tray.setIcon(self._ic_waiting)
        self._tray.setToolTip(f"{theme.APP_NAME} — {message}")

    def notify(self, message: str, title: str | None = None):
        try:
            self._tray.showMessage(title or theme.APP_NAME, message,
                                   QSystemTrayIcon.Information, 4000)
        except Exception:
            pass

    # ── внутреннее ──
    def _on_activated(self, reason):
        # двойной клик / клик по иконке -> показать окно
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._cb.get("show", lambda: None)()
