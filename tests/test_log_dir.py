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


def test_legacy_root_logs_are_removed(tmp_path):
    """Хвосты в корне (до появления logs\) программа убирает сама."""
    from core.dpi.winws_engine import remove_legacy_root_logs

    (tmp_path / "umbranet.log").write_text("old", encoding="utf-8")
    (tmp_path / "e1-spike.log").write_text("old", encoding="utf-8")
    (tmp_path / "e1spike_args.json").write_text("{}", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "umbranet.log").write_text("new", encoding="utf-8")
    remove_legacy_root_logs(tmp_path)
    assert not (tmp_path / "umbranet.log").exists()
    assert not (tmp_path / "e1-spike.log").exists()
    assert not (tmp_path / "e1spike_args.json").exists()
    assert (tmp_path / "logs" / "umbranet.log").read_text(encoding="utf-8") == "new"
