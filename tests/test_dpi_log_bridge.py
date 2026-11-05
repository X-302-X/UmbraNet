"""Regression coverage for real e1-spike activity in the in-app journal."""

from __future__ import annotations

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
for path in (str(ROOT), str(CORE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from core.dns.query_log import QueryLog, SOURCE_DPI, SOURCE_DPI_ERROR  # noqa: E402
from core.dpi.engine_events import DpiEngineEvent, parse_engine_event  # noqa: E402
from core.dpi.log_bridge import DpiLogBridge  # noqa: E402
from core.dpi.runtime import DpiRuntime  # noqa: E402


def test_parser_accepts_only_safe_concrete_dpi_actions():
    event = parse_engine_event(
        '{"t":"action","act":"fake+split","class":"tcp","sni":"WWW.YouTube.COM."}'
    )

    assert event == DpiEngineEvent("action", domain="www.youtube.com", action="fake+split")
    assert parse_engine_event('{"t":"action","act":"fake+split","sni":"https://youtube.com/a"}') is None
    assert parse_engine_event('{"t":"action","act":"fake split","sni":"youtube.com"}') is None
    assert parse_engine_event('{"t":"sni","sni":"youtube.com"}') is None
    assert parse_engine_event("not JSON") is None


def test_parser_reports_error_fact_without_copying_raw_engine_message():
    event = parse_engine_event('{"t":"error","msg":"secret --strategy=C:/private.json"}')

    assert event == DpiEngineEvent("error")


def test_bridge_logs_actual_action_only_for_active_dpi_targets():
    journal = QueryLog()
    bridge = DpiLogBridge(["youtube.com"], journal)

    bridge.handle_engine_event(DpiEngineEvent("action", "www.youtube.com", "fake+split"))
    bridge.handle_engine_event(DpiEngineEvent("action", "notyoutube.com", "fake+split"))

    entries = journal.snapshot()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.domain == "www.youtube.com"
    assert entry.qtype == "DPI"
    assert entry.source == SOURCE_DPI
    assert entry.routed is True
    assert entry.note == "DPI: fake+split"


def test_bridge_keeps_lifecycle_and_error_visible_without_raw_engine_text():
    journal = QueryLog()
    bridge = DpiLogBridge(["youtube.com", "googlevideo.com"], journal)

    bridge.started()
    bridge.handle_engine_event(DpiEngineEvent("error"))
    bridge.stopped()

    entries = journal.snapshot()
    assert [(entry.source, entry.note) for entry in entries] == [
        (SOURCE_DPI, "DPI запущен: целей 2"),
        (SOURCE_DPI_ERROR, "DPI-движок сообщил ошибку; подробности в e1-spike.log"),
        (SOURCE_DPI, "DPI остановлен"),
    ]


class _Strategy:
    last_error = ""

    def get_args(self, strategy_id, *, routed_domains, require_hostlist):
        assert strategy_id == "uz1"
        assert routed_domains == ["youtube.com"]
        assert require_hostlist is True
        return ["--hostlist=test"]


class _EventEngine:
    last_error = ""

    def __init__(self):
        self.listeners = []
        self.started = False

    def subscribe_events(self, callback):
        self.listeners.append(callback)

    def unsubscribe_events(self, callback):
        self.listeners.remove(callback)

    def start(self, args):
        self.started = True
        # The runtime must already be subscribed when the child emits an early
        # action line immediately after process launch.
        for callback in list(self.listeners):
            callback(DpiEngineEvent("action", "youtube.com", "fake+split"))
        return True

    def stop(self, reset_connections=True):
        self.started = False
        return True

    def is_running(self):
        return self.started


def test_log_ui_has_a_distinct_dpi_source_and_route_badge():
    view_source = (ROOT / "umbranet" / "views" / "log.py").read_text(encoding="utf-8")
    canvas_source = (ROOT / "umbranet" / "widgets" / "log_canvas.py").read_text(encoding="utf-8")

    assert '"dpi": "DPI"' in view_source
    assert '"dpi-error": "DPI"' in view_source
    assert 'source in ("dpi", "dpi-error")' in canvas_source
    assert '"DPI", theme.ACCENT' in canvas_source


def test_runtime_uses_the_same_flat_query_log_module_as_the_gui(monkeypatch):
    """Avoid the invisible second singleton from core.dns.query_log imports."""
    sentinel = object()
    flat_module = types.ModuleType("query_log")
    flat_module.get_query_log = lambda: sentinel
    monkeypatch.setitem(sys.modules, "query_log", flat_module)

    runtime = DpiRuntime({"dpi_strategy": "uz1"})

    assert runtime._shared_query_log() is sentinel


def test_runtime_subscribes_before_start_and_detaches_after_stop():
    journal = QueryLog()
    engine = _EventEngine()
    runtime = DpiRuntime(
        {"dpi_strategy": "uz1"},
        strategy_provider=_Strategy(),
        dpi_engine=engine,
        query_log=journal,
    )

    assert runtime.start(["youtube.com"]).ok
    assert [(entry.domain, entry.source, entry.note) for entry in journal.snapshot()] == [
        ("youtube.com", SOURCE_DPI, "DPI: fake+split"),
        ("[DPI]", SOURCE_DPI, "DPI запущен: целей 1"),
    ]

    assert runtime.stop() == (True, "DPI остановлен")
    assert engine.listeners == []
    assert journal.snapshot()[-1].note == "DPI остановлен"
