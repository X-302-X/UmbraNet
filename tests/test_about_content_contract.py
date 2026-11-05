"""User-facing About copy must describe the current routing architecture."""

from __future__ import annotations

from pathlib import Path


ABOUT = Path(__file__).resolve().parent.parent / "umbranet" / "views" / "about.py"


def test_about_navigation_lists_the_actual_servers_page():
    source = ABOUT.read_text(encoding="utf-8")

    assert '_help_row("👁️", "Сервера"' in source
    assert "HTTP CONNECT/SOCKS5" in source
    assert "HTTPS-подписке" in source
    assert "Выходы VPN" not in source


def test_about_does_not_present_retired_dns_protocols_as_current_core():
    source = ABOUT.read_text(encoding="utf-8")

    for retired_copy in (
        "Локальный DNS-инструмент",
        "DNS-ядро",
        "DNS-сервер",
        "DoH/DoT/DoQ/DNSCrypt",
        '"dnslib"',
        '"aioquic"',
        '"pynacl"',
    ):
        assert retired_copy not in source

    assert "DPI-обработка выбранных доменов" in source
    assert "Выборочный транспорт" in source
    assert "Режим маршрутизации" in source
