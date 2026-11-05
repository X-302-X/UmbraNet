"""Local TLS integration tests for strict HTTP CONNECT and SOCKS5 health probes."""

from __future__ import annotations

import base64
from pathlib import Path
import select
import socket
import socketserver
import ssl
import subprocess
import threading

import pytest

from vpn import health
from vpn.controller import TransportController
from vpn.health import probe_http_or_socks_route
from vpn.profiles import RouteProfile


_CERT_HOST = "status.example.test"


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer closed connection prematurely")
        data.extend(chunk)
    return bytes(data)


def _recv_until(sock: socket.socket, marker: bytes, *, limit: int = 65536) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(min(4096, limit - len(data)))
        if not chunk:
            raise ConnectionError("peer closed connection prematurely")
        data.extend(chunk)
        if len(data) > limit:
            raise ConnectionError("request header is too large")
    return bytes(data)


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _writable, _errors = select.select(sockets, [], sockets, 3)
        if not readable:
            return
        for source in readable:
            target = right if source is left else left
            payload = source.recv(65536)
            if not payload:
                return
            target.sendall(payload)


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _TlsEndpoint:
    def __init__(self, certificate: Path, private_key: Path, *, status: int = 200):
        owner = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))

        class _Server(_ThreadedServer):
            def get_request(self):
                raw, address = super().get_request()
                return context.wrap_socket(raw, server_side=True), address

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self):
                request = _recv_until(self.request, b"\r\n\r\n")
                owner.requests.append(request)
                self.request.sendall(
                    f"HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii")
                )

        self.requests: list[bytes] = []
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class _HttpConnectProxy:
    def __init__(self, endpoint_port: int, *, username: str = "", password: str = ""):
        owner = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self):
                request = _recv_until(self.request, b"\r\n\r\n")
                owner.requests.append(request)
                first_line = request.split(b"\r\n", 1)[0].decode("ascii", "replace")
                expected_target = f"CONNECT {_CERT_HOST}:{owner.endpoint_port} HTTP/1.1"
                if first_line != expected_target:
                    self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    return
                if owner.username:
                    expected = base64.b64encode(f"{owner.username}:{owner.password}".encode()).decode()
                    if f"Proxy-Authorization: Basic {expected}".encode() not in request:
                        self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nConnection: close\r\n\r\n")
                        return
                with socket.create_connection(("127.0.0.1", owner.endpoint_port), timeout=3) as upstream:
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    _relay(self.request, upstream)

        self.endpoint_port = endpoint_port
        self.username = username
        self.password = password
        self.requests: list[bytes] = []
        self.server = _ThreadedServer(("127.0.0.1", 0), _Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class _Socks5Proxy:
    def __init__(self, endpoint_port: int, *, username: str = "", password: str = ""):
        owner = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self):
                version, method_count = _recv_exact(self.request, 2)
                methods = _recv_exact(self.request, method_count)
                if version != 5:
                    return
                required_method = 2 if owner.username else 0
                if required_method not in methods:
                    self.request.sendall(b"\x05\xff")
                    return
                self.request.sendall(bytes((5, required_method)))
                if required_method == 2:
                    auth_version = _recv_exact(self.request, 1)[0]
                    username = _recv_exact(self.request, _recv_exact(self.request, 1)[0]).decode()
                    password = _recv_exact(self.request, _recv_exact(self.request, 1)[0]).decode()
                    if auth_version != 1 or (username, password) != (owner.username, owner.password):
                        self.request.sendall(b"\x01\x01")
                        return
                    self.request.sendall(b"\x01\x00")
                version, command, _reserved, atyp = _recv_exact(self.request, 4)
                if version != 5 or command != 1 or atyp != 3:
                    return
                target_host = _recv_exact(self.request, _recv_exact(self.request, 1)[0]).decode("idna")
                target_port = int.from_bytes(_recv_exact(self.request, 2), "big")
                owner.targets.append((target_host, target_port))
                if (target_host, target_port) != (_CERT_HOST, owner.endpoint_port):
                    self.request.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
                    return
                with socket.create_connection(("127.0.0.1", owner.endpoint_port), timeout=3) as upstream:
                    self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                    _relay(self.request, upstream)

        self.endpoint_port = endpoint_port
        self.username = username
        self.password = password
        self.targets: list[tuple[str, int]] = []
        self.server = _ThreadedServer(("127.0.0.1", 0), _Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


@pytest.fixture(scope="module")
def certificate_pair(tmp_path_factory):
    directory = tmp_path_factory.mktemp("probe-certificate")
    certificate = directory / "cert.pem"
    private_key = directory / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
            "-subj", f"/CN={_CERT_HOST}",
            "-addext", f"subjectAltName=DNS:{_CERT_HOST}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return certificate, private_key


def _trust_test_certificate(monkeypatch, certificate: Path) -> None:
    original = ssl.create_default_context
    monkeypatch.setattr(
        health.ssl,
        "create_default_context",
        lambda: original(cafile=str(certificate)),
    )


def test_http_connect_probe_authenticates_and_proves_tls_through_the_exit(certificate_pair, monkeypatch):
    certificate, private_key = certificate_pair
    endpoint = _TlsEndpoint(certificate, private_key)
    proxy = _HttpConnectProxy(endpoint.port, username="alice", password="secret")
    _trust_test_certificate(monkeypatch, certificate)
    profile = RouteProfile("http", "HTTP test", "http", "127.0.0.1", proxy.port,
                           username="alice", password="secret")
    try:
        result = probe_http_or_socks_route(
            profile,
            probe_url=f"https://{_CERT_HOST}:{endpoint.port}/health",
            timeout=3,
        )
        assert result.ok
        assert result.stage == "ready"
        assert result.service_tls_ok is True
        assert endpoint.requests and endpoint.requests[0].startswith(b"GET /health HTTP/1.1")
        assert proxy.requests and f"CONNECT {_CERT_HOST}:{endpoint.port} HTTP/1.1".encode() in proxy.requests[0]
    finally:
        proxy.close()
        endpoint.close()


def test_socks5_probe_authenticates_and_resolves_the_probe_hostname_remotely(certificate_pair, monkeypatch):
    certificate, private_key = certificate_pair
    endpoint = _TlsEndpoint(certificate, private_key)
    proxy = _Socks5Proxy(endpoint.port, username="bob", password="secret")
    _trust_test_certificate(monkeypatch, certificate)
    profile = RouteProfile("socks", "SOCKS test", "socks5", "127.0.0.1", proxy.port,
                           username="bob", password="secret")
    try:
        result = probe_http_or_socks_route(
            profile,
            probe_url=f"https://{_CERT_HOST}:{endpoint.port}/health",
            timeout=3,
        )
        assert result.ok
        assert result.stage == "ready"
        assert proxy.targets == [(_CERT_HOST, endpoint.port)]
        assert endpoint.requests and endpoint.requests[0].startswith(b"GET /health HTTP/1.1")
    finally:
        proxy.close()
        endpoint.close()


def test_controller_promotes_a_profile_only_after_its_real_https_probe_succeeds(certificate_pair, monkeypatch):
    certificate, private_key = certificate_pair
    endpoint = _TlsEndpoint(certificate, private_key)
    proxy = _HttpConnectProxy(endpoint.port, username="alice", password="secret")
    _trust_test_certificate(monkeypatch, certificate)
    profile = RouteProfile("http", "HTTP test", "http", "127.0.0.1", proxy.port,
                           username="alice", password="secret")
    controller = TransportController(
        [profile],
        probe_url=f"https://{_CERT_HOST}:{endpoint.port}/health",
    )
    try:
        snapshot = controller.run_once()
        assert snapshot["probe_configured"] is True
        assert snapshot["active_id"] == "http"
        assert controller.active_profile() == profile
        assert snapshot["routes"][0]["state"] == "active"
        assert snapshot["routes"][0]["probe"]["stage"] == "ready"
    finally:
        controller.stop()
        proxy.close()
        endpoint.close()


def test_tls_certificate_mismatch_keeps_the_proxy_route_unverified(certificate_pair):
    certificate, private_key = certificate_pair
    endpoint = _TlsEndpoint(certificate, private_key)
    proxy = _HttpConnectProxy(endpoint.port)
    profile = RouteProfile("http", "HTTP test", "http", "127.0.0.1", proxy.port)
    try:
        result = probe_http_or_socks_route(
            profile,
            probe_url=f"https://{_CERT_HOST}:{endpoint.port}/health",
            timeout=3,
        )
        assert not result.ok
        assert result.stage == "transport"
        assert result.service_tls_ok is False
    finally:
        proxy.close()
        endpoint.close()
