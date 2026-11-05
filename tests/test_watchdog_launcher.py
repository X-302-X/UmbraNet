"""Команда watchdog не должна удерживать .venv UmbraNet после выхода."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from watchdog_launcher import watchdog_commands


def test_prefers_windows_python_launcher_outside_venv():
    commands = watchdog_commands(
        r"C:\UmbraNet\core\watchdog.py",
        r"C:\UmbraNet\.venv\Scripts\pythonw.exe",
        version_info=SimpleNamespace(major=3, minor=15),
        find_executable=lambda name: r"C:\Windows\py.exe" if name == "py.exe" else None,
    )

    assert commands == [
        [r"C:\Windows\py.exe", "-3.15", r"C:\UmbraNet\core\watchdog.py"],
        [r"C:\UmbraNet\.venv\Scripts\pythonw.exe", r"C:\UmbraNet\core\watchdog.py"],
    ]


def test_keeps_current_interpreter_as_dns_safety_fallback():
    commands = watchdog_commands(
        "/opt/UmbraNet/core/watchdog.py",
        "/opt/UmbraNet/.venv/bin/python",
        version_info=SimpleNamespace(major=3, minor=10),
        find_executable=lambda _name: None,
    )

    assert commands == [["/opt/UmbraNet/.venv/bin/python", "/opt/UmbraNet/core/watchdog.py"]]
