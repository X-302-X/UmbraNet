"""Explicit, bounded HTTPS subscription retrieval for supported proxy exits.

A subscription URL can contain an access token.  This module therefore never
logs, displays, or returns it as user-facing status text.  It accepts only an
UmbraNet JSON document that describes already supported HTTP CONNECT/SOCKS5
profiles; modern VPN protocol links remain outside this import path until their
own adapters exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .importers import import_custom_server_document
from .profiles import normalize_host

_MAX_SUBSCRIPTION_URL = 2048
_MAX_SUBSCRIPTION_BYTES = 1_000_000
_MAX_SUBSCRIPTIONS = 16
_SUBSCRIPTION_ID_PREFIX = "subscription-"


@dataclass(frozen=True, slots=True)
class SubscriptionFetchResult:
    """One explicit refresh result, without exposing the source URL in text."""

    url: str
    label: str
    profiles: list[dict[str, Any]]
    message: str


def normalize_subscription_url(value: object) -> str | None:
    """Return a safe HTTPS subscription URL, preserving an opaque path/query token."""
    text = str(value or "").strip()
    if not text or len(text) > _MAX_SUBSCRIPTION_URL or any(char.isspace() for char in text):
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        return None
    host = normalize_host(parsed.hostname)
    if host is None or (port is not None and not 1 <= port <= 65535):
        return None
    authority = host if port in (None, 443) else f"{host}:{port}"
    path = parsed.path or "/"
    if any(ord(char) < 32 for char in path):
        return None
    return urlunsplit(("https", authority, path, parsed.query, ""))


def subscription_label(url: object) -> str:
    """A non-secret label for a subscription folder in the Servers view."""
    try:
        host = str(urlsplit(str(url or "")).hostname or "").strip().lower()
    except ValueError:
        host = ""
    return host or "Подписка"


def sanitize_transport_subscriptions(raw_subscriptions: object) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize persisted subscription metadata without fetching the network."""
    if not isinstance(raw_subscriptions, list):
        return [], ["transport_subscriptions должен быть списком, применён пустой"]

    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_subscriptions[:_MAX_SUBSCRIPTIONS]):
        if not isinstance(raw, dict):
            warnings.append(f"transport_subscriptions[{index}] пропущена: ожидался объект")
            continue
        source_id = str(raw.get("id", "")).strip()
        if (not source_id.startswith(_SUBSCRIPTION_ID_PREFIX) or len(source_id) > 64
                or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in source_id)):
            warnings.append(f"transport_subscriptions[{index}] пропущена: некорректный id")
            continue
        if source_id in seen_ids:
            warnings.append(f"transport_subscriptions[{index}] пропущена: повторяющийся id")
            continue
        url = normalize_subscription_url(raw.get("url", ""))
        if url is None:
            warnings.append(f"transport_subscriptions[{index}] пропущена: нужен HTTPS URL")
            continue
        raw_profile_ids = raw.get("profile_ids", [])
        if not isinstance(raw_profile_ids, list):
            warnings.append(f"transport_subscriptions[{index}].profile_ids очищены: ожидался список")
            raw_profile_ids = []
        profile_ids: list[str] = []
        seen_profile_ids: set[str] = set()
        for raw_profile_id in raw_profile_ids:
            profile_id = str(raw_profile_id or "").strip()
            if (not profile_id or len(profile_id) > 64
                    or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in profile_id)
                    or profile_id in seen_profile_ids):
                continue
            seen_profile_ids.add(profile_id)
            profile_ids.append(profile_id)
        updated_at = str(raw.get("updated_at", "") or "").strip()[:40]
        seen_ids.add(source_id)
        result.append({
            "id": source_id,
            "url": url,
            "label": subscription_label(url),
            "profile_ids": profile_ids,
            "updated_at": updated_at,
        })
    if len(raw_subscriptions) > _MAX_SUBSCRIPTIONS:
        warnings.append(f"transport_subscriptions: сохранены первые {_MAX_SUBSCRIPTIONS} подписок")
    return result, warnings


def subscription_profile_id(subscription_id: str, profile: dict[str, Any], index: int) -> str:
    """Create a stable, non-secret profile id owned by a subscription source."""
    identity = "\x1f".join(
        str(profile.get(field, "") or "")
        for field in ("id", "name", "protocol", "host", "port", "username")
    )
    digest = sha256(f"{index}\x1f{identity}".encode("utf-8")).hexdigest()[:14]
    return f"{subscription_id}-{digest}"[:64]


def materialize_subscription_profiles(subscription_id: str, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign source-owned ids so an explicit refresh can replace only its rows."""
    materialized: list[dict[str, Any]] = []
    for index, profile in enumerate(profiles):
        item = dict(profile)
        item["id"] = subscription_profile_id(subscription_id, item, index)
        materialized.append(item)
    return materialized


def _download_subscription(url: str, opener: Callable[..., Any]) -> bytes:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "UmbraNet/1"})
    with opener(request, timeout=15) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if int(status) < 200 or int(status) >= 300:
            raise OSError("subscription endpoint returned non-success status")
        content_length = response.headers.get("Content-Length", "")
        if content_length:
            try:
                if int(content_length) > _MAX_SUBSCRIPTION_BYTES:
                    raise OSError("subscription document is too large")
            except ValueError:
                pass
        payload = response.read(_MAX_SUBSCRIPTION_BYTES + 1)
    if len(payload) > _MAX_SUBSCRIPTION_BYTES:
        raise OSError("subscription document is too large")
    return payload


def fetch_transport_subscription(url: object, *, opener: Callable[..., Any] = urlopen) -> SubscriptionFetchResult:
    """Fetch one subscription only after an explicit user action.

    Network and parse failures are deliberately terse: a private token from the
    URL or an upstream diagnostic must never reach normal UI status or logs.
    """
    normalized = normalize_subscription_url(url)
    if normalized is None:
        return SubscriptionFetchResult("", "", [], "Нужна корректная HTTPS-ссылка подписки.")
    label = subscription_label(normalized)
    try:
        payload = _download_subscription(normalized, opener)
        document = json.loads(payload.decode("utf-8-sig"))
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeError, json.JSONDecodeError, ValueError):
        return SubscriptionFetchResult(
            normalized,
            label,
            [],
            "Не удалось обновить подписку. Проверьте ссылку и соединение.",
        )

    imported = import_custom_server_document(document)
    if not imported.profiles:
        return SubscriptionFetchResult(
            normalized,
            label,
            [],
            "В подписке нет поддерживаемых HTTP CONNECT/SOCKS5 серверов.",
        )
    return SubscriptionFetchResult(
        normalized,
        label,
        imported.profiles,
        f"Серверов в подписке: {len(imported.profiles)}",
    )
