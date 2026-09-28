"""
UmbraNet — grik: фоновый замер пинга DNS и DPI.

Вынесен из главного меню вместе с графиком (см. grik/ping_graph.py).
Логика замера 1:1 с прежней: DNS пингуется по активному профилю,
DPI — коннектом на 443 порт обходящего домена.

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from umbranet.engine_adapter import probe_doh, probe_host


class PingWorker(QThread):
    """Фоновый замер пинга для DNS и DPI (с обходом)."""
    done = Signal(bool, object, object)  # ok, dns_ms, dpi_ms

    def __init__(self, profile: dict, dns_mode: str, routed_domains: list, measure_dns: bool, measure_dpi: bool):
        super().__init__()
        self.profile = profile
        self.dns_mode = dns_mode
        self.routed_domains = routed_domains
        self.measure_dns = measure_dns
        self.measure_dpi = measure_dpi

    def run(self):
        dns_ms = None
        dpi_ms = None

        # 1. Замеряем DNS пинг
        if self.measure_dns:
            if self.dns_mode == "doh" and self.profile.get("doh_url"):
                ok, ms = probe_doh(self.profile["doh_url"])
            elif self.profile.get("ipv4_primary"):
                ok, ms = probe_host(self.profile["ipv4_primary"])
            else:
                ok, ms = False, None
            if ok:
                dns_ms = ms

        # 2. Замеряем DPI пинг (через TCP 443 к обходящему домену)
        if self.measure_dpi:
            target_domain = "google.com"
            if self.routed_domains:
                # Ищем популярный обходящий домен, чтобы замерить реальный обход
                for d in self.routed_domains:
                    if "google" in d or "youtube" in d or "discord" in d:
                        target_domain = d
                        break
                else:
                    target_domain = self.routed_domains[0]
            
            # Коннект на порт 443 пройдет через WinDivert и применит активную стратегию
            ok, ms = probe_host(target_domain, 443)
            if ok:
                dpi_ms = ms

        self.done.emit(True, dns_ms, dpi_ms)

