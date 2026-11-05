"""Safe import of the currently supported custom HTTP/SOCKS server documents.

Modern protocol documents are deliberately not guessed as HTTP/SOCKS routes.
They receive dedicated adapters before becoming usable VPN servers.  This keeps
an imported file from silently becoming a different kind of connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profiles import sanitize_route_profiles

_MAX_IMPORT_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class ServerImportResult:
    profiles: list[dict[str, Any]]
    message: str


def import_custom_server_document(raw: object) -> ServerImportResult:
    """Read accepted UmbraNet HTTP/SOCKS document shapes without side effects.

    Accepted shapes are a profile object, a profile list, or an object holding
    a ``transport_profiles``/``profiles`` list.  No network request, process,
    URL subscription, or configuration write is performed here.
    """
    candidates: object
    if isinstance(raw, dict):
        if isinstance(raw.get("transport_profiles"), list):
            candidates = raw["transport_profiles"]
        elif isinstance(raw.get("profiles"), list):
            candidates = raw["profiles"]
        elif isinstance(raw.get("profile"), dict):
            candidates = [raw["profile"]]
        elif {"id", "name", "protocol", "host", "port"}.issubset(raw):
            candidates = [raw]
        else:
            return ServerImportResult([], "В файле не найден поддерживаемый список серверов.")
    elif isinstance(raw, list):
        candidates = raw
    else:
        return ServerImportResult([], "Файл должен содержать JSON-конфигурацию сервера.")

    profiles, warnings = sanitize_route_profiles(candidates)
    if not profiles:
        detail = "; ".join(warnings[:2])
        return ServerImportResult([], detail or "В файле нет корректных HTTP CONNECT/SOCKS5 серверов.")
    return ServerImportResult(profiles, f"Найдено серверов: {len(profiles)}")


def import_custom_server_file(path: str | Path) -> ServerImportResult:
    """Read one bounded local JSON document for the manual-server screen."""
    file_path = Path(path)
    try:
        if not file_path.is_file() or file_path.stat().st_size > _MAX_IMPORT_BYTES:
            return ServerImportResult([], "Файл недоступен или слишком большой.")
        raw_text = file_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ServerImportResult([], "Не удалось прочитать JSON-конфигурацию сервера.")
    return import_custom_server_document(raw)
