"""Команда запуска watchdog без удержания файлов виртуального окружения.

Основное приложение обычно работает из ``.venv``. Если запустить watchdog тем
же ``pythonw.exe``, Windows не даст сразу удалить папку приложения, пока
watchdog возвращает DNS после штатного выхода. Windows Python Launcher ``py``
лежит вне папки UmbraNet, поэтому предпочитаем его, сохраняя прежний
интерпретатор как надёжный fallback.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path


def watchdog_commands(
    watchdog_path: str | Path,
    fallback_python: str,
    *,
    version_info=None,
    find_executable: Callable[[str], str | None] = shutil.which,
) -> list[list[str]]:
    """Возвращает команды запуска watchdog в безопасном порядке.

    ``py.exe -X.Y`` гарантирует ту же версию Python, но не удерживает
    исполняемый файл Python из ``.venv``. Если launcher отключён или не установлен,
    остаётся текущий интерпретатор — защита восстановления DNS важнее удобства
    удаления папки.
    """
    version = version_info or sys.version_info
    python_version = f"{int(version.major)}.{int(version.minor)}"
    path = str(watchdog_path)
    commands: list[list[str]] = []

    launcher = find_executable("py.exe") or find_executable("py")
    if launcher:
        commands.append([launcher, f"-{python_version}", path])

    fallback = [str(fallback_python), path]
    if fallback not in commands:
        commands.append(fallback)
    return commands
