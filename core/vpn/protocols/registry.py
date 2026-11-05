"""Deterministic registry for UmbraNet's concrete protocol adapters."""

from __future__ import annotations

from typing import Iterable

from .base import ProtocolAdapter
from .models import ProtocolId, ProtocolStatus


class ProtocolRegistry:
    """Own the adapter set and prohibit accidental protocol aliases."""

    def __init__(self, adapters: Iterable[ProtocolAdapter] = ()):
        indexed: dict[ProtocolId, ProtocolAdapter] = {}
        for adapter in adapters:
            protocol = adapter.descriptor.protocol
            if protocol in indexed:
                raise ValueError(f"Повторяющийся VPN protocol adapter: {protocol.value}")
            indexed[protocol] = adapter
        self._adapters = indexed

    def adapter(self, protocol: ProtocolId | str) -> ProtocolAdapter | None:
        try:
            key = protocol if isinstance(protocol, ProtocolId) else ProtocolId(str(protocol or "").strip().lower())
        except ValueError:
            return None
        return self._adapters.get(key)

    def statuses(self) -> list[ProtocolStatus]:
        return [adapter.inspect() for adapter in self._adapters.values()]

    def public_snapshot(self) -> list[dict[str, object]]:
        return [status.public_mapping() for status in self.statuses()]
