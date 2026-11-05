"""Validation and immutable models for external proxy route profiles.

A route profile describes an already lawful and configured outbound proxy.  It
never contains DNS behaviour and is usable by both the health checker and the
local gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

ROUTE_PROTOCOLS = ("http", "socks5")
FAILOVER_MODES = ("economy", "recommended", "continuous")
FAILURE_REACTIONS = ("cautious", "balanced", "fast")
NO_READY_POLICIES = ("block",)

# Bounded because every enabled route can be health-checked and reserved at runtime.
MAX_ROUTE_PROFILES = 8
_MAX_PROFILES = MAX_ROUTE_PROFILES
_MAX_TEXT = 96
_MAX_PROBE_URL = 512
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_host(value: object) -> str | None:
    """Return a safe IPv4/IPv6/DNS endpoint host, never a URL or local path."""
    text = str(value or "").strip().rstrip(".")
    if not text or len(text) > 253 or any(ch.isspace() for ch in text):
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    labels = text.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    return text.lower()


def normalize_probe_url(value: object) -> str | None:
    """Normalize a user-owned HTTPS route-check endpoint without secret material.

    Only a hostname URL is accepted so the probe always uses TLS SNI and can
    verify the certificate. Query strings, fragments and URL credentials are
    rejected: the shared health endpoint must not turn into another secret
    store or leak an access token in diagnostics.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > _MAX_PROBE_URL or any(ch.isspace() for ch in text):
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme.lower() != "https" or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        return None
    host = str(parsed.hostname or "").rstrip(".")
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    path = parsed.path or "/"
    if any(ord(char) < 32 or ord(char) > 127 for char in path):
        return None
    authority = host.lower()
    if port not in (None, 443):
        authority = f"{authority}:{port}"
    return f"https://{authority}{path}"


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """One externally supplied HTTP CONNECT or SOCKS5 exit."""

    id: str
    name: str
    protocol: str
    host: str
    port: int
    enabled: bool = True
    priority: int = 100
    country_hint: str = ""
    username: str = ""
    password: str = ""

    @property
    def endpoint_label(self) -> str:
        return f"{self.host}:{self.port}"

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "RouteProfile":
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            protocol=str(raw["protocol"]),
            host=str(raw["host"]),
            port=int(raw["port"]),
            enabled=bool(raw.get("enabled", True)),
            priority=int(raw.get("priority", 100)),
            country_hint=str(raw.get("country_hint", "")),
            username=str(raw.get("username", "")),
            password=str(raw.get("password", "")),
        )

    def public_mapping(self) -> dict[str, Any]:
        """A serializable profile without credential material for UI/logs."""
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "enabled": self.enabled,
            "priority": self.priority,
            "country_hint": self.country_hint,
            "has_credentials": bool(self.username or self.password),
        }


def _normalize_profile(raw: object, index: int, warnings: list[str]) -> RouteProfile | None:
    if not isinstance(raw, dict):
        warnings.append(f"transport_profiles[{index}] пропущен: ожидался объект")
        return None

    profile_id = str(raw.get("id", "")).strip()
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        warnings.append(f"transport_profiles[{index}] пропущен: некорректный id")
        return None

    name = str(raw.get("name", "")).strip()
    if not name or len(name) > _MAX_TEXT:
        warnings.append(f"transport_profiles[{index}] пропущен: некорректное имя")
        return None

    protocol = str(raw.get("protocol", "")).strip().lower()
    if protocol not in ROUTE_PROTOCOLS:
        warnings.append(f"transport_profiles[{index}] пропущен: protocol должен быть http или socks5")
        return None

    host = normalize_host(raw.get("host"))
    if host is None:
        warnings.append(f"transport_profiles[{index}] пропущен: некорректный host")
        return None

    try:
        port = int(raw.get("port"))
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        warnings.append(f"transport_profiles[{index}] пропущен: port вне диапазона 1..65535")
        return None

    try:
        priority = int(raw.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    if not 0 <= priority <= 1000:
        warnings.append(f"transport_profiles[{index}].priority вне диапазона, использовано 100")
        priority = 100

    country_hint = str(raw.get("country_hint", "")).strip().upper()
    if country_hint and not _COUNTRY_RE.fullmatch(country_hint):
        warnings.append(f"transport_profiles[{index}].country_hint очищен: ожидается код страны из двух букв")
        country_hint = ""

    username = str(raw.get("username", "")).strip()
    password = str(raw.get("password", ""))
    if len(username) > _MAX_TEXT or len(password) > 1024:
        warnings.append(f"transport_profiles[{index}] пропущен: слишком длинные данные авторизации")
        return None
    if bool(username) != bool(password):
        warnings.append(f"transport_profiles[{index}] пропущен: username и password задаются парой")
        return None

    return RouteProfile(
        id=profile_id,
        name=name,
        protocol=protocol,
        host=host,
        port=port,
        enabled=_as_bool(raw.get("enabled", True), True),
        priority=priority,
        country_hint=country_hint,
        username=username,
        password=password,
    )


def sanitize_route_profiles(raw_profiles: object, *, max_profiles: int = _MAX_PROFILES) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize persisted profiles and return safe serializable mappings.

    Credentials are preserved only after structural validation.  The controller
    consumes them only while opening the authenticated standard proxy route and
    never exposes them in public status mappings or logs.
    """
    warnings: list[str] = []
    if not isinstance(raw_profiles, list):
        return [], ["transport_profiles должен быть списком, применён пустой"]

    profiles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_profiles[:max_profiles]):
        profile = _normalize_profile(raw, index, warnings)
        if profile is None:
            continue
        if profile.id in seen_ids:
            warnings.append(f"transport_profiles[{index}] пропущен: повторяющийся id")
            continue
        seen_ids.add(profile.id)
        profiles.append({
            "id": profile.id,
            "name": profile.name,
            "protocol": profile.protocol,
            "host": profile.host,
            "port": profile.port,
            "enabled": profile.enabled,
            "priority": profile.priority,
            "country_hint": profile.country_hint,
            "username": profile.username,
            "password": profile.password,
        })
    if len(raw_profiles) > max_profiles:
        warnings.append(f"transport_profiles: сохранены первые {max_profiles} профилей")
    return profiles, warnings


def profiles_from_config(config: dict[str, Any]) -> list[RouteProfile]:
    """Return enabled and disabled profiles in stable priority/id order."""
    normalized, _warnings = sanitize_route_profiles(config.get("transport_profiles", []))
    profiles = [RouteProfile.from_mapping(item) for item in normalized]
    return sorted(profiles, key=lambda item: (item.priority, item.name.casefold(), item.id))
