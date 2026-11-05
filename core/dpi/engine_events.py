"""Safe, structured events emitted by the bundled e1-spike DPI engine.

The engine is launched with ``--json``.  Its stdout remains a complete
technical log, but this module deliberately exposes only a small typed subset
that is safe for the in-app activity journal.  In particular, arbitrary engine
messages, packet payloads, IP addresses and command lines are never forwarded
to the GUI.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

_MAX_LINE_CHARS = 16_384
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9+_.-]{0,63}$", re.IGNORECASE)
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DpiEngineEvent:
    """Whitelisted fact reported by e1-spike.

    ``kind`` is intentionally limited to ``action`` and ``error``.  Lifecycle
    entries are recorded by :class:`DpiRuntime`, which knows whether the launch
    or stop operation actually succeeded.
    """

    kind: str
    domain: str = ""
    action: str = ""


def normalise_sni(value: object) -> str:
    """Return a display-safe DNS name from an engine SNI field, or ``""``.

    The function accepts only ordinary ASCII DNS labels.  e1-spike reports SNI
    in its wire/IDNA form, so this preserves the useful identifier while
    rejecting paths, URLs, controls and arbitrary process output.
    """

    if not isinstance(value, str):
        return ""
    domain = value.strip().lower().rstrip(".")
    if not domain or len(domain) > 253 or "." not in domain:
        return ""
    labels = domain.split(".")
    if any(not _LABEL_RE.fullmatch(label) for label in labels):
        return ""
    return domain


def _safe_token(value: object) -> str:
    if not isinstance(value, str):
        return ""
    token = value.strip().lower()
    return token if _TOKEN_RE.fullmatch(token) else ""


def parse_engine_event(line: str | bytes) -> DpiEngineEvent | None:
    """Parse one e1-spike stdout line without ever trusting it as UI text.

    Non-JSON lines, malformed records, oversized lines and unknown event kinds
    are deliberately ignored here.  They are still retained verbatim in
    ``logs/e1-spike.log`` by :mod:`winws_engine` for diagnostics.
    """

    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace")
    elif isinstance(line, str):
        text = line
    else:
        return None
    if not text or len(text) > _MAX_LINE_CHARS:
        return None
    try:
        raw: Any = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    kind = raw.get("t")
    if kind == "action":
        action = _safe_token(raw.get("act"))
        domain = normalise_sni(raw.get("sni"))
        # The event is useful in the activity journal only if it identifies a
        # concrete TLS/QUIC target and a concrete action.  Packet-level records
        # without either fact remain in the technical engine log.
        if action and domain:
            return DpiEngineEvent(kind="action", domain=domain, action=action)
        return None
    if kind == "error":
        # Do not copy the arbitrary ``msg`` field into the UI.  The fact that
        # the real engine emitted an error is enough for the activity journal;
        # the complete diagnostic remains in the local technical log.
        return DpiEngineEvent(kind="error")
    return None
