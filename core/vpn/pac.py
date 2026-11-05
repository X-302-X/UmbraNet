"""PAC generation for selected service domains.

The selected-domain PAC branch deliberately returns one local proxy only.  It
never appends `DIRECT`, so a lost transport cannot silently reveal selected
traffic through the ordinary system route.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable

_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def normalize_selected_domains(domains: Iterable[object]) -> tuple[str, ...]:
    """Return stable DNS domain rules appropriate for PAC and gateway matching."""
    selected: set[str] = set()
    for raw in domains:
        domain = str(raw or "").strip().lower().rstrip(".")
        if not domain:
            continue
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            pass
        else:
            continue
        if _DOMAIN_RE.fullmatch(domain):
            selected.add(domain)
    return tuple(sorted(selected, key=lambda value: (value.count("."), value)))


def host_is_selected(host: object, domains: Iterable[object]) -> bool:
    normalized_host = str(host or "").strip().lower().rstrip(".")
    if not normalized_host:
        return False
    return any(
        normalized_host == domain or normalized_host.endswith("." + domain)
        for domain in normalize_selected_domains(domains)
    )


def _js_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_pac(domains: Iterable[object], *, gateway_host: str = "127.0.0.1",
              gateway_port: int) -> str:
    """Build deterministic PAC source for browser system-proxy clients."""
    if not isinstance(gateway_port, int) or not 1 <= gateway_port <= 65535:
        raise ValueError("gateway_port must be within 1..65535")
    normalized = normalize_selected_domains(domains)
    gateway = f"PROXY {gateway_host}:{gateway_port}"
    lines = [
        "function FindProxyForURL(url, host) {",
        "  host = (host || '').toLowerCase();",
    ]
    for domain in normalized:
        escaped = _js_string(domain)
        lines.append(
            f'  if (host === "{escaped}" || dnsDomainIs(host, ".{escaped}")) return "{gateway}";'
        )
    lines.extend([
        '  return "DIRECT";',
        "}",
        "",
    ])
    return "\n".join(lines)
