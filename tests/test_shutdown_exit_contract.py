"""Source-level guards for a real UmbraNet tray exit and folder unlock."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "umbranet" / "app.py"
TRAY = ROOT / "umbranet" / "widgets" / "tray.py"


def _method_source(class_name: str, method_name: str) -> str:
    source = APP.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == method_name:
                return ast.get_source_segment(source, member) or ""
    raise AssertionError(f"{class_name}.{method_name} is missing")


def test_window_cross_explicitly_means_hide_to_tray_not_exit():
    close = _method_source("MainWindow", "closeEvent")

    assert "event.ignore()" in close
    assert "self.hide()" in close
    assert "Полностью выйти" in close
    assert "QApplication.quit()" in close


def test_tray_exit_is_named_as_a_real_exit():
    tray = TRAY.read_text(encoding="utf-8")

    assert 'QAction("✕  Полностью выйти"' in tray


def test_lifecycle_worker_cannot_resurrect_routing_after_shutdown_request():
    worker = _method_source("_EngineWorker", "run")
    request = _method_source("_EngineWorker", "request_shutdown")

    assert "self._shutdown_requested" in request
    assert worker.count("self._shutdown_requested") >= 3
    assert "_stop_after_shutdown_request" in worker
    assert "stop_routing_runtime()" in _method_source("_EngineWorker", "_stop_after_shutdown_request")


def test_real_exit_quiesces_workers_before_final_dpi_sweep_and_audits_result():
    hard_stop = _method_source("MainWindow", "_hard_stop_runtime")

    assert hard_stop.index("self._stop_lifecycle_workers()") < hard_stop.index("stop_routing_runtime()")
    assert hard_stop.index("stop_routing_runtime()") < hard_stop.index("dpi_strategy_ai_cleanup_runtime()")
    assert "verify_teardown()" in hard_stop
    assert "self._shutdown_watchdog(\"CLEAN\")" in hard_stop


def test_tray_exit_closes_native_window_and_has_a_post_cleanup_fallback():
    quit_method = _method_source("MainWindow", "_quit_app")
    fallback = _method_source("MainWindow", "_schedule_exit_fallback")

    assert "self._hard_stop_runtime()" in quit_method
    assert "self.close()" in quit_method
    assert "QApplication.quit()" in quit_method
    assert "threading.Timer" in fallback
    assert "os._exit(0)" in fallback
