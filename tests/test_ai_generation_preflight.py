from __future__ import annotations

import socket
from types import SimpleNamespace

import psutil

import umbranet.engine_adapter as ea


def test_local_port_conflicts_reports_tcp_and_udp_owners(monkeypatch):
    tcp = SimpleNamespace(
        laddr=SimpleNamespace(ip="127.0.0.1", port=53),
        type=socket.SOCK_STREAM,
        status=psutil.CONN_LISTEN,
        pid=401,
    )
    udp = SimpleNamespace(
        laddr=SimpleNamespace(ip="0.0.0.0", port=53),
        type=socket.SOCK_DGRAM,
        status=psutil.CONN_NONE,
        pid=402,
    )
    unrelated = SimpleNamespace(
        laddr=SimpleNamespace(ip="0.0.0.0", port=443),
        type=socket.SOCK_STREAM,
        status=psutil.CONN_LISTEN,
        pid=403,
    )
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": [tcp, udp, unrelated])
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda pid: SimpleNamespace(name=lambda: {401: "dnsproxy.exe", 402: "zapret-dns.exe"}[pid]),
    )

    conflicts, error = ea._dpi_local_port_conflicts((53,))

    assert error == ""
    assert {(row["protocol"], row["port"], row["pid"]) for row in conflicts} == {
        ("TCP", 53, 401),
        ("UDP", 53, 402),
    }
    assert {row["process_name"] for row in conflicts} == {"dnsproxy.exe", "zapret-dns.exe"}


def test_preflight_stops_before_generation_if_dns_port_is_occupied(monkeypatch):
    monkeypatch.setattr(
        ea,
        "_dpi_local_port_conflicts",
        lambda ports=(53,): ([{
            "protocol": "UDP",
            "host": "127.0.0.1",
            "port": 53,
            "pid": 1400,
            "process_name": "dnsproxy.exe",
        }], ""),
    )

    class NoForeignDpi:
        def foreign_processes(self):
            return []

    progress = []
    result = ea._dpi_generation_preflight(NoForeignDpi(), progress.append)

    assert result["abort"] is True
    assert "локального DNS-порта 53" in result["reason"]
    assert "UDP 127.0.0.1:53" in result["reason"]
    assert "dnsproxy.exe (PID 1400)" in result["reason"]
    assert any("остановлена до запуска" in line for line in progress)


def test_preflight_detects_foreign_winws_as_windivert_conflict(monkeypatch):
    monkeypatch.setattr(ea, "_dpi_local_port_conflicts", lambda ports=(53,): ([], ""))

    class ZapretRunning:
        def foreign_processes(self):
            return [(321, r"C:\Zapret\winws.exe")]

    progress = []
    result = ea._dpi_generation_preflight(ZapretRunning(), progress.append)

    assert result["abort"] is True
    assert "WinDivert" in result["reason"]
    assert "PID 321" in result["reason"]
    assert r"C:\Zapret\winws.exe" in result["reason"]
    assert "автоматическое завершение пока не выполняется" in result["reason"]
    assert any("остановлена до запуска" in line for line in progress)
