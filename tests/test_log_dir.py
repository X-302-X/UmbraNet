"""Все логи UmbraNet живут в одной папке logs\\ (пожелание 2026-10-05)."""
from pathlib import Path


def test_engine_logs_live_in_logs_dir():
    from core.dpi.winws_engine import WinWSEngine

    eng = WinWSEngine()
    assert eng.log_path.parent.name == "logs"
    assert eng.log_path.name == "e1-spike.log"
    assert eng._args_path.parent.name == "logs"


def test_app_log_dir_is_logs():
    import core.dns.dns_server as dns

    assert Path(dns.LOG_DIR).name == "logs"
    assert Path(dns.LOG_DIR).is_dir()
