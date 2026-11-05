"""Source-level contracts for event-driven Start/header updates.

The 200-ms shell timer may drain cross-thread engine events, but it must not
perform readiness, QSS or header-layout work when that queue is empty.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "umbranet" / "app.py").read_text(encoding="utf-8")
HEADER = (ROOT / "umbranet" / "widgets" / "header.py").read_text(encoding="utf-8")


def _method(source: str, name: str) -> str:
    marker = f"    def {name}("
    start = source.index(marker)
    end = source.find("\n    def ", start + len(marker))
    return source[start:] if end < 0 else source[start:end]


def test_empty_engine_tick_only_drains_events():
    events = _method(APP, "_process_engine_events")
    loop_at = events.index("for event in drain_events():")
    before_loop = events[:loop_at]
    assert "self._update_start_button()" not in before_loop
    assert "self._apply_topbar_compact()" not in before_loop


def test_start_readiness_is_cached_and_does_not_measure_layout():
    update = _method(APP, "_update_start_button")
    assert "if can == self._last_can_start:" in update
    assert "self._last_can_start = can" in update
    assert "control.set_can_start(can)" in update
    assert "_apply_topbar_compact" not in update


def test_real_state_events_still_refresh_start_readiness():
    events = _method(APP, "_process_engine_events")
    for event_type in ('"status_changed"', '"mode_changed"', '"config_changed"'):
        start = events.index(event_type)
        end = events.find("elif etype", start + len(event_type))
        block = events[start:] if end < 0 else events[start:end]
        assert "self._update_start_button()" in block


def test_controlbar_skips_unchanged_qss_and_notifies_layout_once_changed():
    can_start = _method(HEADER, "set_can_start")
    assert "if can == self._can_start:" in can_start
    assert "return False" in can_start
    assert "self.visualStateChanged.emit()" in can_start
    assert "return True" in can_start
    assert "visualStateChanged = Signal()" in HEADER
    assert "self.control.visualStateChanged.connect(self._apply_topbar_compact)" in APP
