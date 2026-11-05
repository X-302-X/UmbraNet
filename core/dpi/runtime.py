"""Small DPI runtime boundary used by the new VPN / Combo / DPI session owner."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable, Protocol

try:  # Normal package layout.
    from core.dpi.log_bridge import DpiLogBridge
except ImportError:  # Historical flat core/ import layout.
    from log_bridge import DpiLogBridge

log = logging.getLogger("UmbraNet.DpiRuntime")


class StrategyProvider(Protocol):
    last_error: str

    def get_args(self, strategy_id: str, *, routed_domains: list[str],
                 require_hostlist: bool) -> list[str]: ...


class DpiEngine(Protocol):
    last_error: str

    def start(self, args: list[str]) -> bool: ...
    def stop(self, reset_connections: bool = True) -> bool: ...
    def is_running(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class DpiStartResult:
    ok: bool
    message: str


def _clean_domains(domains: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        value = str(raw or "").strip().lower().rstrip(".")
        if not value or "." not in value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


class DpiRuntime:
    """Starts e1-spike from the selected hostlist without starting local DNS."""

    def __init__(self, config: dict, *, strategy_provider: StrategyProvider | None = None,
                 dpi_engine: DpiEngine | None = None, query_log: object | None = None):
        self._config = config
        self._strategy_provider = strategy_provider
        self._dpi_engine = dpi_engine
        # ``query_log`` is injectable so the boundary remains testable without
        # a DNS server.  Production obtains the shared structured journal on
        # demand, not through a GUI import.
        self._query_log = query_log
        self._running = False
        self._last_error = ""
        self._target_count = 0
        self._log_bridge: DpiLogBridge | None = None
        self._event_engine: object | None = None
        self._event_listener = None

    def _strategy(self) -> StrategyProvider:
        if self._strategy_provider is None:
            from core.dpi.strategy_manager import get_strategy_manager
            self._strategy_provider = get_strategy_manager()
        return self._strategy_provider

    def _engine(self) -> DpiEngine:
        if self._dpi_engine is None:
            from core.dpi.winws_engine import get_winws_engine
            self._dpi_engine = get_winws_engine()
        return self._dpi_engine

    def _shared_query_log(self):
        if self._query_log is not None:
            return self._query_log
        # Must share the exact flat module used by engine_adapter and
        # dns_server.  `core.dns.query_log` is the same file but a different
        # Python module name and therefore owns a different global singleton.
        try:
            from query_log import get_query_log
        except ImportError:
            try:
                from core.dns.query_log import get_query_log
            except ImportError:
                return None
        try:
            self._query_log = get_query_log()
        except Exception as exc:
            log.debug("DPI activity journal is unavailable: %s", exc)
        return self._query_log

    def _new_log_bridge(self, targets: list[str]) -> DpiLogBridge:
        return DpiLogBridge(targets, self._shared_query_log())

    def _detach_engine_events(self) -> None:
        engine = self._event_engine
        listener = self._event_listener
        self._event_engine = None
        self._event_listener = None
        if engine is None or listener is None:
            return
        unsubscribe = getattr(engine, "unsubscribe_events", None)
        if not callable(unsubscribe):
            return
        try:
            unsubscribe(listener)
        except Exception as exc:
            # A broken diagnostic subscription must never make the DPI engine
            # impossible to stop or restart.
            log.debug("Could not detach DPI activity listener: %s", exc)

    def _attach_engine_events(self, engine: DpiEngine, bridge: DpiLogBridge) -> None:
        self._detach_engine_events()
        self._log_bridge = bridge
        listener = bridge.handle_engine_event
        subscribe = getattr(engine, "subscribe_events", None)
        if not callable(subscribe):
            # Narrow compatibility: alternative/older test engines can still
            # run and contribute lifecycle events, just not live action lines.
            return
        try:
            subscribe(listener)
            self._event_engine = engine
            self._event_listener = listener
        except Exception as exc:
            log.debug("Could not subscribe to DPI engine activity: %s", exc)

    def start(self, domains: Iterable[object]) -> DpiStartResult:
        targets = _clean_domains(domains)
        if not targets:
            return DpiStartResult(False, "Не выбраны домены для DPI-маршрута")
        bridge = self._new_log_bridge(targets)
        strategy = self._strategy()
        args = strategy.get_args(
            str(self._config.get("dpi_strategy", "uz1")),
            routed_domains=targets,
            require_hostlist=True,
        )
        if not args:
            message = str(getattr(strategy, "last_error", "") or "DPI-стратегия не подготовила аргументы")
            self._last_error = message
            bridge.start_failed()
            return DpiStartResult(False, message)
        engine = self._engine()
        # Subscribe before process launch: short-lived engine output must not
        # race ahead of the runtime and disappear from the user journal.
        self._attach_engine_events(engine, bridge)
        if not engine.start(args):
            message = str(getattr(engine, "last_error", "") or "e1-spike не запустился")
            self._last_error = message
            bridge.start_failed()
            self._detach_engine_events()
            self._log_bridge = None
            return DpiStartResult(False, message)
        self._running = True
        self._target_count = len(targets)
        self._last_error = ""
        bridge.started()
        log.info("DPI runtime started for %d targets", self._target_count)
        return DpiStartResult(True, f"DPI активен для {self._target_count} доменов")

    def reconfigure(self, config: dict) -> None:
        """Adopt saved strategy settings for the next explicit DPI restart."""
        if isinstance(config, dict):
            self._config = config

    def stop(self) -> tuple[bool, str]:
        engine = self._engine()
        bridge = self._log_bridge
        was_running = self._running
        try:
            stopped = bool(engine.stop(reset_connections=False))
        except TypeError:  # narrow test/downgrade compatibility
            stopped = bool(engine.stop())
        except Exception as exc:
            self._last_error = str(exc)
            if bridge is not None:
                bridge.stop_failed()
            self._detach_engine_events()
            self._log_bridge = None
            return False, self._last_error
        self._running = False
        self._target_count = 0
        if not stopped and bool(getattr(engine, "is_running", lambda: False)()):
            self._last_error = str(getattr(engine, "last_error", "") or "e1-spike не остановился")
            if bridge is not None:
                bridge.stop_failed()
            self._detach_engine_events()
            self._log_bridge = None
            return False, self._last_error
        self._last_error = ""
        if was_running and bridge is not None:
            bridge.stopped()
        self._detach_engine_events()
        self._log_bridge = None
        return True, "DPI остановлен"

    def snapshot(self) -> dict[str, object]:
        engine = self._dpi_engine
        is_running = False
        try:
            is_running = bool(engine and engine.is_running())
        except Exception:
            is_running = self._running
        return {
            "running": self._running and is_running,
            "target_count": self._target_count,
            "last_error": self._last_error,
        }
