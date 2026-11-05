"""Regression coverage for the Python 3.15 Windows installer preflight."""
import ast
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
from types import SimpleNamespace

from packaging.specifiers import SpecifierSet
import pytest

from tools import check_install_python as preflight

ROOT = Path(__file__).resolve().parents[1]


def check(version=(3, 14, 5), implementation="CPython", bits=64,
          platform_tag="win-amd64", free_threaded=False):
    return preflight.compatibility_error(version, implementation, bits, platform_tag, free_threaded)


@pytest.mark.parametrize("minor", [10, 11, 12, 13, 14, 15])
def test_supported_cpython_windows_versions(minor):
    assert check(version=(3, minor, 5)) == ""


@pytest.mark.parametrize("version", [(3, 9, 20), (3, 16, 0), (4, 0, 0)])
def test_unsupported_version_has_actionable_error(version):
    error = check(version=version)
    assert "3.10–3.15" in error
    assert "Python {0}.{1}".format(*version) in error


@pytest.mark.parametrize("kwargs,reason", [
    ({"bits": 32, "platform_tag": "win32"}, "Windows x64"),
    ({"platform_tag": "win-arm64"}, "ARM64"),
    ({"implementation": "PyPy"}, "CPython"),
    ({"free_threaded": True}, "free-threaded"),
])
def test_incompatible_interpreter_build(kwargs, reason):
    assert reason in check(**kwargs)


def test_preflight_runs_without_app_dependencies_and_uses_old_python_syntax():
    source = (ROOT / "tools" / "check_install_python.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imports == {"platform", "struct", "sys", "sysconfig"}


@pytest.mark.parametrize("version,exit_code", [((3, 15, 0), 0), ((3, 16, 0), 1)])
def test_preflight_cli_exit_status(monkeypatch, capsys, version, exit_code):
    monkeypatch.setattr(preflight, "sys", SimpleNamespace(version_info=version))
    monkeypatch.setattr(preflight, "platform", SimpleNamespace(
        python_implementation=lambda: "CPython",
        python_version=lambda: ".".join(map(str, version)),
    ))
    monkeypatch.setattr(preflight, "struct", SimpleNamespace(calcsize=lambda fmt: 8))
    monkeypatch.setattr(preflight, "sysconfig", SimpleNamespace(
        get_platform=lambda: "win-amd64", get_config_var=lambda key: 0,
    ))
    assert preflight.main() == exit_code
    output = capsys.readouterr().out
    assert "[ОШИБКА]" in output if exit_code else "[OK]" in output


def test_installed_qt_metadata_allows_every_supported_python():
    # The previous Qt pin fails this test at 3.15 even if tests run on Python 3.10.
    packages = ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6")
    try:
        metadata_by_package = {
            package: importlib.metadata.metadata(package)
            for package in packages
        }
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("PySide6 metadata is unavailable in this dependency-light test environment")
    for package, metadata in metadata_by_package.items():
        allowed = SpecifierSet(metadata["Requires-Python"])
        for minor in range(preflight.MIN_PYTHON[1], preflight.MAX_PYTHON[1]):
            assert "3.{0}.5".format(minor) in allowed, (package, minor, str(allowed))


def test_installer_checks_actual_venv_before_pip_and_before_success():
    script = (ROOT / "install.bat").read_text(encoding="utf-8")
    assert 'if exist ".venv\\Scripts\\python.exe" goto :check_venv' in script
    assert script.index('tools\\check_install_python.py') < script.index(' -m venv .venv')
    venv_start = script.index('\n:check_venv\n')
    assert venv_start < script.index('"%PY%" "%APP_DIR%tools\\check_install_python.py"') < script.index(' -m pip install')
    assert script.index(' -m pip check') < script.index('echo   Готово!')
    assert 'from PySide6 import QtCore, QtGui, QtWidgets' in script
    assert '--only-binary=:all:' in script
    assert 'Частая причина: Windows Long Path' not in script
    assert 'rmdir' not in script.lower()  # never delete a user's existing environment


def test_installer_does_not_waste_a_fragile_connection_on_bootstrap_upgrade():
    script = (ROOT / "install.bat").read_text(encoding="utf-8")

    assert 'PIP_NO_CACHE_DIR' not in script
    assert '--no-cache-dir' not in script
    assert 'UMBRANET_UPDATE_BOOTSTRAP' in script
    assert 'Используется установленный pip; обновление bootstrap-пакетов пропущено.' in script
    assert '--retries 2 --timeout 30 --only-binary=:all:' in script
    assert 'pypi.org и files.pythonhosted.org' in script


def stage_installer(tmp_path):
    # Spaces, Cyrillic and parentheses in paths are common on user machines.
    destination = tmp_path / "UmbraNet проверка (x64)"
    destination.mkdir()
    shutil.copy2(ROOT / "install.bat", destination / "install.bat")
    (destination / "tools").mkdir()
    return destination


def run_installer(directory):
    env = dict(os.environ, UMBRANET_INSTALL_NO_PAUSE="1", UMBRANET_SUBST_DONE="1")
    return subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "install.bat"],
        cwd=directory, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows cmd.exe")
def test_broken_existing_venv_stops_without_deleting_it(tmp_path):
    directory = stage_installer(tmp_path)
    (directory / ".venv").mkdir()
    sentinel = directory / ".venv" / "preserve.txt"
    sentinel.write_text("keep")
    result = run_installer(directory)
    assert result.returncode == 1, result.stdout
    assert sentinel.read_text() == "keep"
    assert not (directory / ".venv" / "Scripts").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows cmd.exe")
def test_failed_venv_preflight_stops_before_pip(tmp_path):
    directory = stage_installer(tmp_path)
    # No pip available: rejection must happen at preflight, not at installation.
    venv.EnvBuilder(with_pip=False).create(directory / ".venv")
    (directory / "tools" / "check_install_python.py").write_text(
        "print('PREFLIGHT_REJECTED')\nraise SystemExit(1)\n", encoding="utf-8")
    result = run_installer(directory)
    assert result.returncode == 1, result.stdout
    assert b"PREFLIGHT_REJECTED" in result.stdout
    assert b"No module named pip" not in result.stdout
    assert (directory / ".venv" / "Scripts" / "python.exe").exists()
