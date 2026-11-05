"""Pure bounded-buffer accounting for the GUI activity journal.

This module deliberately has no Qt dependency.  It makes high-volume journal
semantics testable in headless CI while LogView remains responsible only for
coalescing and painting its visible state.
"""

from __future__ import annotations


_STAT_KEYS = ("total", "routed", "blocked", "fixed", "errors", "dpi_status")


class LogBatchBuffer:
    """Keep a bounded row sequence and O(1) aggregate counters."""

    def __init__(self, max_rows: int):
        self.max_rows = max(1, int(max_rows))
        self.rows: list = []
        self.stats = self._empty_stats()

    @staticmethod
    def _empty_stats() -> dict[str, int]:
        return {key: 0 for key in _STAT_KEYS}

    def clear(self) -> None:
        self.rows.clear()
        self.stats.clear()
        self.stats.update(self._empty_stats())

    def append(self, entry) -> None:
        self.rows.append(entry)
        self._adjust(entry, 1)
        overflow = len(self.rows) - self.max_rows
        if overflow > 0:
            for removed in self.rows[:overflow]:
                self._adjust(removed, -1)
            del self.rows[:overflow]

    def _adjust(self, entry, delta: int) -> None:
        source = str(getattr(entry, "source", "") or "")
        if source == "bg-refresh":
            return
        self.stats["total"] += delta
        routed = bool(getattr(entry, "routed", False))
        if routed:
            self.stats["routed"] += delta
        if source in {"bogus-NX", "blocked"}:
            self.stats["blocked"] += delta
        if source == "fixed":
            self.stats["fixed"] += delta
        if source in {"error", "servfail", "leak", "dpi-error"}:
            self.stats["errors"] += delta
        if source == "dpi" and not routed:
            self.stats["dpi_status"] += delta
