"""Dependency-free preflight for install.bat, including an existing .venv.

Keep the syntax compatible with older Python 3 versions so unsupported versions
can print a useful error before pip/venv creation. Do not import app dependencies.
"""
import platform
import struct
import sys
import sysconfig

MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 16)  # exclusive; pinned Qt wheels support up to Python 3.15
SUPPORTED_PYTHON = "3.10–3.15"


def compatibility_error(version, implementation, bits, platform_tag, free_threaded=False):
    """Return a user-facing reason or an empty string for the supported target."""
    if tuple(version[:2]) < MIN_PYTHON or tuple(version[:2]) >= MAX_PYTHON:
        return (
            "Нужен Python {0}. Закреплённые зависимости не поддерживают Python {1}.{2}."
            .format(SUPPORTED_PYTHON, version[0], version[1])
        )
    if implementation != "CPython":
        return "Нужен CPython: для {0} нет проверенного комплекта Qt-зависимостей.".format(implementation)
    if bits != 64 or platform_tag != "win-amd64":
        return "Нужен Python для Windows x64 (AMD64), не 32-bit и не нативный ARM64."
    if free_threaded:
        return "Нужен обычный CPython с GIL, не экспериментальная free-threaded сборка."
    return ""


def main():
    implementation = platform.python_implementation()
    bits = struct.calcsize("P") * 8
    platform_tag = sysconfig.get_platform()
    print("Python {0} | {1} | {2}-bit | {3}".format(
        platform.python_version(), implementation, bits, platform_tag))
    error = compatibility_error(
        sys.version_info, implementation, bits, platform_tag,
        str(sysconfig.get_config_var("Py_GIL_DISABLED")) == "1",
    )
    if error:
        print("[ОШИБКА] " + error)
        return 1
    print("[OK] Интерпретатор подходит для закреплённых зависимостей.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
