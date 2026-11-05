"""Lifecycle boundary for one implemented VPN protocol adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ProtocolDescriptor, ProtocolStartRequest, ProtocolStartResult, ProtocolStatus


class ProtocolAdapter(ABC):
    """Prepare one protocol without touching UmbraNet PAC or direct routing.

    An adapter may expose only the egress declared in its descriptor.  The
    common runtime owns PAC, selected-domain policy, health proof, status
    snapshots, failover, and the eventual stop call.
    """

    @property
    @abstractmethod
    def descriptor(self) -> ProtocolDescriptor:
        """Static, non-secret protocol identity."""

    @abstractmethod
    def inspect(self) -> ProtocolStatus:
        """Return readiness without parsing user secrets or opening a tunnel."""

    @abstractmethod
    def start(self, request: ProtocolStartRequest) -> ProtocolStartResult:
        """Prepare the declared egress or return a non-destructive refusal."""
