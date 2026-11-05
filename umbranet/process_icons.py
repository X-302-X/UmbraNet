"""
UmbraNet — иконки процессов для диспетчера задач и окна выбора.

Ядро (process_monitor.resolve_process_exe) отдаёт путь к .exe.
Здесь путь превращается в QPixmap/QIcon. QPixmap создаём только
в GUI-потоке: воркер диалога приносит пути, не картинки.

Если файла нет, Linux, CI или провайдер вернул пусто — вызывающий
рисует 🎮 (gamepad_icon / эмодзи в канвасе).

Автор: X-302-X / UmbraNet_Official
Лицензия: GPLv3
"""

from __future__ import annotations

import os

from PySide6.QtCore import QFileInfo, QRect, QSize, Qt
from PySide6.QtGui import QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QFileIconProvider

from umbranet import engine_adapter as ea

ICON_PX = 16

_pixmap_cache: dict[str, QPixmap] = {}
_provider: QFileIconProvider | None = None
_gamepad: QIcon | None = None


def reset_icon_cache() -> None:
    """Сброс кэша картинок (тесты)."""
    global _provider, _gamepad
    _pixmap_cache.clear()
    _provider = None
    _gamepad = None


def _icon_provider() -> QFileIconProvider:
    global _provider
    if _provider is None:
        _provider = QFileIconProvider()
    return _provider


def pixmap_for_exe(path: str | None, size: int = ICON_PX) -> QPixmap | None:
    """Иконка файла .exe. None, если файла нет или провайдер пустой."""
    if not path or not os.path.isfile(path):
        return None
    key = f"{os.path.normcase(os.path.abspath(path))}|{size}"
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return None if cached.isNull() else cached
    try:
        icon = _icon_provider().icon(QFileInfo(path))
        pm = icon.pixmap(QSize(size, size))
    except Exception:
        pm = QPixmap()
    _pixmap_cache[key] = pm
    return None if pm.isNull() else pm


def pixmap_for_process(name: str, size: int = ICON_PX) -> QPixmap | None:
    """Иконка по имени процесса (chrome.exe). Промахи не кэшируем — путь может появиться позже."""
    name = (name or "").strip()
    if not name:
        return None
    path = ea.resolve_process_exe(name)
    return pixmap_for_exe(path, size) if path else None


def icon_for_process(name: str, size: int = ICON_PX) -> QIcon:
    """QIcon процесса или 🎮, если настоящей иконки нет."""
    pm = pixmap_for_process(name, size)
    if pm is not None:
        return QIcon(pm)
    return gamepad_icon(size)


def gamepad_icon(size: int = ICON_PX) -> QIcon:
    """Заглушка 🎮 — чтобы в списке выбора у всех строк была иконка и ряды не плясали."""
    global _gamepad
    if _gamepad is not None and size == ICON_PX:
        return _gamepad
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    font = QFont()
    font.setPixelSize(max(10, size - 2))
    p.setFont(font)
    p.drawText(QRect(0, 0, size, size), Qt.AlignCenter, "🎮")
    p.end()
    icon = QIcon(pm)
    if size == ICON_PX:
        _gamepad = icon
    return icon
