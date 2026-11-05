"""Local PAC/gateway boundary tests without an external network dependency."""

from __future__ import annotations

import socket
import socketserver
import threading

from vpn.controller import TransportController
from vpn.gateway import LocalTransportGateway
from vpn.health import RouteProbeResult
from vpn.pac import build_pac, host_is_selected
from vpn.profiles import RouteProfile


def _recv_until(sock: socket.socket, marker: bytes) -> bytes:
    out = bytearray()
    while marker not in out:
        chunk = sock.recv(4096)
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


class _EchoConnectProxy:
    def __init__(self):
        owner = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self):
                header = _recv_until(self.request, b"\r\n\r\n")
                owner.requests.append(header)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                while True:
                    payload = self.request.recv(65536)
                    if not payload:
                        return
                    self.request.sendall(payload)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.requests: list[bytes] = []
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def _controller_for(proxy_port: int) -> TransportController:
    profile = RouteProfile(
        id="eu",
        name="EU test exit",
        protocol="http",
        host="127.0.0.1",
        port=proxy_port,
        priority=10,
    )
    controller = TransportController(
        [profile],
        probe=lambda _profile: RouteProbeResult(ok=True, stage="ready", exit_country="NL", service_tls_ok=True),
    )
    controller.run_once()
    return controller


def test_pac_selected_branch_has_only_local_proxy_and_other_domains_stay_direct():
    pac = build_pac(["chatgpt.com", "openai.com"], gateway_port=18777)

    assert 'host === "chatgpt.com"' in pac
    assert 'dnsDomainIs(host, ".chatgpt.com")' in pac
    assert 'return "PROXY 127.0.0.1:18777";' in pac
    assert pac.rstrip().endswith('return "DIRECT";\n}')
    assert host_is_selected("api.chatgpt.com", ["chatgpt.com"])
    assert not host_is_selected("notchatgpt.com", ["chatgpt.com"])


def test_gateway_serves_pac_and_relays_selected_connect_only_through_active_proxy():
    upstream = _EchoConnectProxy()
    gateway = LocalTransportGateway(_controller_for(upstream.port), ["chatgpt.com"])
    gateway.start()
    try:
        with socket.create_connection((gateway.host, gateway.port), timeout=3) as pac_client:
            pac_client.sendall(b"GET /proxy.pac HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            pac_reply = _recv_until(pac_client, b"\r\n\r\n")
        assert pac_reply.startswith(b"HTTP/1.1 200 OK")
        assert b"PROXY 127.0.0.1:" in pac_reply

        with socket.create_connection((gateway.host, gateway.port), timeout=3) as client:
            client.sendall(b"CONNECT api.chatgpt.com:443 HTTP/1.1\r\nHost: api.chatgpt.com:443\r\n\r\n")
            reply = _recv_until(client, b"\r\n\r\n")
            assert reply.startswith(b"HTTP/1.1 200 Connection Established")
            client.sendall(b"through-active-route")
            assert client.recv(128) == b"through-active-route"
        assert upstream.requests
        assert b"CONNECT api.chatgpt.com:443 HTTP/1.1" in upstream.requests[0]
        assert gateway.snapshot()["requests"] == 1
    finally:
        gateway.stop()
        upstream.close()


def test_gateway_blocks_selected_target_when_no_verified_route_exists():
    controller = TransportController([])
    gateway = LocalTransportGateway(controller, ["chatgpt.com"])
    gateway.start()
    try:
        with socket.create_connection((gateway.host, gateway.port), timeout=3) as client:
            client.sendall(b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\n")
            reply = _recv_until(client, b"\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 503 Service Unavailable")
        assert gateway.snapshot()["blocked"] == 1
    finally:
        gateway.stop()


def test_gateway_refuses_a_non_selected_target_even_with_an_active_route():
    upstream = _EchoConnectProxy()
    gateway = LocalTransportGateway(_controller_for(upstream.port), ["chatgpt.com"])
    gateway.start()
    try:
        with socket.create_connection((gateway.host, gateway.port), timeout=3) as client:
            client.sendall(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
            reply = _recv_until(client, b"\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403 Forbidden")
        assert not upstream.requests
    finally:
        gateway.stop()
        upstream.close()
