"""One visual/action contract for DPI strategy context menus."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import QMenu, QWidget

from umbranet import theme


def _rgba(color: str, alpha: int) -> str:
    parsed = QColor(color)
    if not parsed.isValid():
        return "transparent"
    return f"rgba({parsed.red()}, {parsed.green()}, {parsed.blue()}, {alpha})"


def build_dpi_strategy_menu(
    parent: QWidget,
    *,
    is_active: bool,
    can_duplicate: bool,
    activate: Callable[[], None],
    duplicate: Callable[[], None],
    rename: Callable[[], None],
    edit_description: Callable[[], None],
    show_in_folder: Callable[[], None],
    copy_path: Callable[[], None],
    delete: Callable[[], None],
) -> QMenu:
    """Build the same menu for the main DPI picker and AI strategy library."""
    menu = QMenu(parent)
    menu.setStyleSheet(
        f"QMenu {{ background-color: {theme.CARD}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 10px; padding: 5px; }}"
        f"QMenu::item {{ padding: 8px 24px 8px 12px; border-radius: 7px; font-size: 12px; }}"
        f"QMenu::item:selected {{ background-color: {_rgba(theme.ACCENT, 75)}; color: {theme.WHITE}; }}"
        f"QMenu::item:disabled {{ color: {theme.MUTED}; }}"
        f"QMenu::separator {{ height:1px; background:{theme.BORDER}; margin:5px 6px; }}"
    )
    active_action = QAction("✓ Уже активна" if is_active else "✓ Сделать активной", menu)
    active_action.setEnabled(not is_active)
    duplicate_action = QAction("⧉ Создать копию", menu)
    duplicate_action.setEnabled(can_duplicate)
    rename_action = QAction("✏ Изменить имя", menu)
    description_action = QAction("📝 Изменить описание", menu)
    show_action = QAction("📁 Показать в папке", menu)
    copy_action = QAction("📋 Скопировать путь", menu)
    delete_action = QAction("🗑 Удалить", menu)

    active_action.triggered.connect(activate)
    duplicate_action.triggered.connect(duplicate)
    rename_action.triggered.connect(rename)
    description_action.triggered.connect(edit_description)
    show_action.triggered.connect(show_in_folder)
    copy_action.triggered.connect(copy_path)
    delete_action.triggered.connect(delete)

    menu.addAction(active_action)
    menu.addSeparator()
    menu.addAction(duplicate_action)
    menu.addAction(rename_action)
    menu.addAction(description_action)
    menu.addSeparator()
    menu.addAction(show_action)
    menu.addAction(copy_action)
    menu.addSeparator()
    menu.addAction(delete_action)
    return menu
