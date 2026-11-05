"""Headless correctness tests for bounded, batched GUI-journal data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from umbranet.widgets.log_batch import LogBatchBuffer


@dataclass(frozen=True)
class _Entry:
    source: str = "system"
    routed: bool = False


def _expected(rows: list[_Entry]) -> dict[str, int]:
    rows = [entry for entry in rows if entry.source != "bg-refresh"]
    total = len(rows)
    routed = sum(entry.routed for entry in rows)
    blocked = sum(entry.source in {"bogus-NX", "blocked"} for entry in rows)
    fixed = sum(entry.source == "fixed" for entry in rows)
    errors = sum(entry.source in {"error", "servfail", "leak", "dpi-error"} for entry in rows)
    dpi_status = sum(entry.source == "dpi" and not entry.routed for entry in rows)
    return {
        "total": total,
        "routed": routed,
        "blocked": blocked,
        "fixed": fixed,
        "errors": errors,
        "dpi_status": dpi_status,
    }


def test_burst_keeps_last_rows_and_exact_incremental_statistics():
    limit = 2000
    buffer = LogBatchBuffer(limit)
    sources = ("system", "dpi", "blocked", "fixed", "error", "bg-refresh", "servfail")
    inserted = [
        _Entry(source=sources[index % len(sources)], routed=(index % 5 == 0))
        for index in range(5000)
    ]
    for entry in inserted:
        buffer.append(entry)

    expected_rows = inserted[-limit:]
    assert buffer.rows == expected_rows
    assert buffer.stats == _expected(expected_rows)


def test_clear_keeps_live_buffer_references_and_resets_all_counters():
    buffer = LogBatchBuffer(3)
    rows_ref = buffer.rows
    stats_ref = buffer.stats
    buffer.append(_Entry("dpi"))
    buffer.append(_Entry("blocked", routed=True))
    buffer.clear()

    assert buffer.rows is rows_ref
    assert buffer.stats is stats_ref
    assert buffer.rows == []
    assert buffer.stats == _expected([])


def _method(source: str, name: str) -> str:
    marker = f"    def {name}("
    start = source.index(marker)
    end = source.find("\n    def ", start + len(marker))
    return source[start:] if end < 0 else source[start:end]


def test_log_view_batches_hidden_entries_without_per_entry_canvas_or_stats_work():
    source = (Path(__file__).resolve().parents[1] / "umbranet" / "views" / "log.py").read_text(encoding="utf-8")
    add_row = _method(source, "_add_row_ui")
    schedule = _method(source, "_schedule_display_flush")
    deactivate = _method(source, "page_deactivated")
    update_stats = _method(source, "_update_stats")

    assert "LOG_BATCH_MS = 120" in source
    assert "self._append_row(entry)" in add_row
    assert "self._schedule_display_flush()" in add_row
    assert "self._update_canvas()" not in add_row
    assert "self._update_stats()" not in add_row
    assert "not self._page_active" in schedule
    assert "not self.isVisible()" in schedule
    assert "self._batch_timer.stop()" in deactivate
    assert "for e in self._rows" not in update_stats
