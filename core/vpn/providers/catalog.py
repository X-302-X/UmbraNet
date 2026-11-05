"""Built-in provider catalogue with conservative readiness checks.

The catalogue is deliberately non-networked: opening Settings must never start
an upstream provider, contact a discovery API, or alter Windows proxy settings.
Concrete launch support is admitted only once its security and distribution
checks are complete.
"""

from __future__ import annotations

import os
import shutil
from typing import Callable, Iterable

from .base import (
    BuiltinProviderAdapter,
    ProviderDescriptor,
    ProviderStartResult,
    ProviderState,
    ProviderStatus,
)

_ExecutableLookup = Callable[[str], str | None]


class CloudflareWarpProvider(BuiltinProviderAdapter):
    """Detect the official WARP client without taking ownership of its tunnel."""

    _descriptor = ProviderDescriptor(
        id="builtin-warp",
        name="Cloudflare WARP",
        region_label="Автоматический маршрут",
        description="Официальный бесплатный local proxy для браузеров и proxy-aware приложений.",
        official=True,
        experimental=False,
    )

    def __init__(self, *, windows: bool | None = None,
                 executable_lookup: _ExecutableLookup = shutil.which):
        self._windows = os.name == "nt" if windows is None else bool(windows)
        self._executable_lookup = executable_lookup

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def inspect(self) -> ProviderStatus:
        if not self._windows:
            return ProviderStatus(
                self.descriptor,
                ProviderState.UNSUPPORTED_PLATFORM,
                "Встроенный WARP local proxy доступен в сборке UmbraNet для Windows.",
            )
        if not self._executable_lookup("warp-cli.exe") and not self._executable_lookup("warp-cli"):
            return ProviderStatus(
                self.descriptor,
                ProviderState.NOT_INSTALLED,
                "Официальный Cloudflare WARP ещё не установлен.",
            )
        return ProviderStatus(
            self.descriptor,
            ProviderState.CONFIGURATION_REQUIRED,
            "Официальный WARP найден; UmbraNet подготовит его local proxy в следующем adapter-срезе.",
        )

    def start(self) -> ProviderStartResult:
        status = self.inspect()
        return ProviderStartResult(False, status.message)


class OperaProvider(BuiltinProviderAdapter):
    """Expose honest Opera route metadata without invoking unreviewed launch code."""

    def __init__(self, *, provider_id: str, name: str, region_label: str):
        self._descriptor = ProviderDescriptor(
            id=provider_id,
            name=name,
            region_label=region_label,
            description="Встроенный Opera-маршрут без ручного ввода proxy-параметров.",
            official=False,
            experimental=True,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def inspect(self) -> ProviderStatus:
        return ProviderStatus(
            self.descriptor,
            ProviderState.SECURITY_REVIEW,
            "Ожидает завершения проверки безопасности и поставки компонента.",
        )

    def start(self) -> ProviderStartResult:
        status = self.inspect()
        return ProviderStartResult(False, status.message)


class BuiltinProviderCatalog:
    """Own one immutable list of provider adapters for VPN core and UI."""

    def __init__(self, providers: Iterable[BuiltinProviderAdapter] | None = None):
        source = providers if providers is not None else (
            CloudflareWarpProvider(),
            OperaProvider(provider_id="builtin-opera-eu", name="Opera Europe", region_label="Европа"),
            OperaProvider(provider_id="builtin-opera-america", name="Opera America", region_label="Америка"),
        )
        indexed: dict[str, BuiltinProviderAdapter] = {}
        for provider in source:
            provider_id = provider.descriptor.id
            if provider_id in indexed:
                raise ValueError(f"Повторяющийся встроенный VPN provider: {provider_id}")
            indexed[provider_id] = provider
        self._providers = indexed

    def providers(self) -> tuple[BuiltinProviderAdapter, ...]:
        return tuple(self._providers.values())

    def status(self, provider_id: str) -> ProviderStatus | None:
        provider = self._providers.get(str(provider_id or "").strip())
        return provider.inspect() if provider is not None else None

    def statuses(self) -> list[ProviderStatus]:
        return [provider.inspect() for provider in self._providers.values()]

    def public_snapshot(self) -> list[dict[str, object]]:
        return [status.public_mapping() for status in self.statuses()]
