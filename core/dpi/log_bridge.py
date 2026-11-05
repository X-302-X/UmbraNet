"""Bridge real e1-spike events into UmbraNet's structured activity journal."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Protocol

# `umbranet.engine_adapter` and the DNS resolver deliberately use the historical
# flat `query_log` import.  Prefer that exact module first: importing the same
# file as `core.dns.query_log` as well would create a second Python singleton
# and DPI entries would be invisible to the UI.
try:
    from query_log import QueryLogEntry, SOURCE_DPI, SOURCE_DPI_ERROR
except ImportError:  # Package-only tooling/tests without core/dns on sys.path.
    from core.dns.query_log import QueryLogEntry, SOURCE_DPI, SOURCE_DPI_ERROR

try:  # Normal package import.
    from core.dpi.engine_events import DpiEngineEvent
except ImportError:  # Historical flat core/ import layout.
    from engine_events import DpiEngineEvent


class QueryLogWriter(Protocol):
    def add(self, entry: QueryLogEntry) -> None: ...


def _clean_targets(domains: Iterable[object]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        value = str(raw or "").strip().lower().rstrip(".")
        if value and "." in value and value not in seen:
            seen.add(value)
            values.append(value)
    return tuple(values)


def _matches_target(domain: str, targets: tuple[str, ...]) -> bool:
    """Whether an observed SNI belongs to the active DPI host list.

    A selected ``youtube.com`` legitimately covers ``www.youtube.com``; the
    dot boundary prevents a lookalike such as ``notyoutube.com`` from being
    included in the user's activity journal.
    """

    return any(domain == target or domain.endswith("." + target) for target in targets)


class DpiLogBridge:
    """Writes only factual, safe-to-display DPI activity to a QueryLog.

    ``action`` is emitted by the engine after it has made an actual DPI decision
    for a connection.  We intentionally do not treat a configured host-list
    item, a bare DNS lookup, or a generic stdout line as traffic activity.
    """

    def __init__(self, targets: Iterable[object], query_log: QueryLogWriter | None):
        self._targets = _clean_targets(targets)
        self._query_log = query_log

    @property
    def target_count(self) -> int:
        return len(self._targets)

    def _add(self, *, domain: str, source: str, routed: bool,
             rcode: str, note: str) -> None:
        if self._query_log is None:
            return
        self._query_log.add(QueryLogEntry(
            timestamp=time.time(),
            domain=domain,
            qtype="DPI",
            source=source,
            routed=routed,
            rcode=rcode,
            note=note,
        ))

    def started(self) -> None:
        self._add(
            domain="[DPI]",
            source=SOURCE_DPI,
            routed=False,
            rcode="OK",
            note=f"DPI запущен: целей {self.target_count}",
        )

    def stopped(self) -> None:
        self._add(
            domain="[DPI]",
            source=SOURCE_DPI,
            routed=False,
            rcode="OK",
            note="DPI остановлен",
        )

    def start_failed(self) -> None:
        self._add(
            domain="[DPI]",
            source=SOURCE_DPI_ERROR,
            routed=False,
            rcode="ERROR",
            note="DPI не запустился; подробности в e1-spike.log",
        )

    def stop_failed(self) -> None:
        self._add(
            domain="[DPI]",
            source=SOURCE_DPI_ERROR,
            routed=False,
            rcode="ERROR",
            note="DPI не остановился; подробности в e1-spike.log",
        )

    def handle_engine_event(self, event: DpiEngineEvent) -> None:
        """Consume a whitelisted event delivered by ``WinWSEngine``."""

        if event.kind == "action":
            if not _matches_target(event.domain, self._targets):
                return
            self._add(
                domain=event.domain,
                source=SOURCE_DPI,
                routed=True,
                rcode="OK",
                note=f"DPI: {event.action}",
            )
            return
        if event.kind == "error":
            self._add(
                domain="[DPI]",
                source=SOURCE_DPI_ERROR,
                routed=False,
                rcode="ERROR",
                note="DPI-движок сообщил ошибку; подробности в e1-spike.log",
            )
