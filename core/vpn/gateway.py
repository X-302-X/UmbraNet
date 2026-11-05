"""Loopback HTTP proxy gateway with a stable PAC endpoint.

The gateway is intentionally limited to selected domains.  It is a boundary:
PAC decides which browser requests reach it, and the gateway independently
rejects every other target so a local process cannot use it as an unrestricted
proxy.
"""

from __future__ import annotations

from http import HTTPStatus
import logging
import select
import socket
import socketserver
import threading
from typing import Iterable
from urllib.parse import urlsplit

from .controller import TransportController
from .health import open_route_tunnel
from .pac import build_pac, host_is_selected, normalize_selected_domains

log = logging.getLogger("UmbraNet.TransportGateway")

_HEADER_LIMIT = 65536
_CONNECT_IDLE_SECONDS = 300.0


class _GatewayDenied(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _read_request(sock: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(min(4096, _HEADER_LIMIT - len(data)))
        if not chunk:
            raise ConnectionError("клиент закрыл соединение до заголовка HTTP proxy")
        data.extend(chunk)
        if len(data) > _HEADER_LIMIT:
            raise ConnectionError("заголовок HTTP proxy слишком большой")
    return bytes(data).split(b"\r\n\r\n", 1)


def _parse_authority(authority: str, default_port: int | None = None) -> tuple[str, int]:
    value = authority.strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError("некорректный IPv6 authority")
        host = value[1:end]
        suffix = value[end + 1:]
        if suffix.startswith(":"):
            port_text = suffix[1:]
        elif suffix:
            raise ValueError("некорректный IPv6 authority")
        else:
            port_text = "" if default_port is None else str(default_port)
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            host = value
            port_text = "" if default_port is None else str(default_port)
        elif not host:
            raise ValueError("в authority нет имени хоста")
    if not host:
        raise ValueError("в authority нет имени хоста")
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        raise ValueError("некорректный порт цели") from None
    if not 1 <= port <= 65535:
        raise ValueError("порт цели вне диапазона 1..65535")
    return host.rstrip("."), port


def _status_bytes(status: HTTPStatus, message: str) -> bytes:
    body = (message.strip() or status.phrase).encode("utf-8", "replace") + b"\n"
    return (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body


def _split_http_request(header: bytes) -> tuple[str, str, str, list[bytes]]:
    lines = header.split(b"\r\n")
    if not lines or not lines[0]:
        raise ValueError("пустой HTTP proxy запрос")
    try:
        method, target, version = lines[0].decode("iso-8859-1").split(" ", 2)
    except ValueError:
        raise ValueError("некорректная стартовая строка HTTP proxy") from None
    if not method.isalpha() or not version.startswith("HTTP/"):
        raise ValueError("некорректная стартовая строка HTTP proxy")
    return method.upper(), target, version, lines[1:]


def _relay(left: socket.socket, right: socket.socket) -> None:
    """Relay a tunnel until either endpoint closes or remains idle too long."""
    left.setblocking(False)
    right.setblocking(False)
    sockets = [left, right]
    try:
        while True:
            readable, _writable, _errors = select.select(sockets, [], sockets, _CONNECT_IDLE_SECONDS)
            if not readable:
                return
            for source in readable:
                target = right if source is left else left
                try:
                    payload = source.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if not payload:
                    return
                view = memoryview(payload)
                while view:
                    try:
                        sent = target.send(view)
                    except (BlockingIOError, InterruptedError):
                        select.select([], [target], [], _CONNECT_IDLE_SECONDS)
                        continue
                    if sent <= 0:
                        return
                    view = view[sent:]
    finally:
        for endpoint in sockets:
            try:
                endpoint.setblocking(True)
            except OSError:
                pass


class LocalTransportGateway:
    """A fixed loopback endpoint for PAC and selected HTTP(S) connections."""

    def __init__(self, controller: TransportController, domains: Iterable[object] = (), *,
                 host: str = "127.0.0.1", port: int = 0, timeout: float = 12.0):
        self._controller = controller
        self._domains = normalize_selected_domains(domains)
        self._host = host
        self._port = int(port)
        self._timeout = max(1.0, float(timeout))
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._requests = 0
        self._blocked = 0
        self._errors = 0

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    @property
    def pac_url(self) -> str:
        return f"http://{self._host}:{self.port}/proxy.pac"

    def set_domains(self, domains: Iterable[object]) -> None:
        with self._lock:
            self._domains = normalize_selected_domains(domains)

    def pac_source(self) -> str:
        return build_pac(self._domains, gateway_host=self._host, gateway_port=self.port)

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            gateway = self

            class _Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True
                request_queue_size = 64

            class _Handler(socketserver.BaseRequestHandler):
                def handle(self) -> None:
                    gateway._handle_client(self.request)

            server = _Server((self._host, self._port), _Handler)
            self._server = server
            self._port = int(server.server_address[1])
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.2},
                daemon=True,
                name="UmbraNet-TransportGateway",
            )
            self._thread.start()
            log.info("Local transport gateway listening on %s:%s", self._host, self._port)

    def stop(self, *, join_timeout: float = 2.0) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            try:
                server.shutdown()
            finally:
                server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(join_timeout)))

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": self._server is not None,
                "host": self._host,
                "port": self._port,
                "pac_url": self.pac_url if self._server is not None else "",
                "domains": list(self._domains),
                "requests": self._requests,
                "blocked": self._blocked,
                "errors": self._errors,
            }

    def _send(self, client: socket.socket, payload: bytes) -> None:
        try:
            client.sendall(payload)
        except OSError:
            pass

    def _handle_client(self, client: socket.socket) -> None:
        client.settimeout(self._timeout)
        try:
            raw_header, tail = _read_request(client)
            method, target, version, lines = _split_http_request(raw_header)
            if method == "GET" and target.split("?", 1)[0] == "/proxy.pac":
                pac = self.pac_source().encode("utf-8")
                self._send(
                    client,
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/x-ns-proxy-autoconfig; charset=utf-8\r\n"
                    b"Cache-Control: no-store\r\n"
                    + f"Content-Length: {len(pac)}\r\nConnection: close\r\n\r\n".encode("ascii") + pac,
                )
                return
            if method == "CONNECT":
                self._handle_connect(client, target, tail)
            else:
                self._handle_http_request(client, method, target, version, lines, tail)
        except _GatewayDenied as exc:
            self._count("blocked")
            self._send(client, _status_bytes(exc.status, exc.message))
        except ValueError as exc:
            self._count("blocked")
            self._send(client, _status_bytes(HTTPStatus.BAD_REQUEST, str(exc)))
        except Exception as exc:  # A bad local client must not stop the gateway.
            self._count("errors")
            log.debug("Transport gateway client failed: %s", exc)
            self._send(client, _status_bytes(HTTPStatus.BAD_GATEWAY, "UmbraNet transport gateway error"))
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _count(self, field: str) -> None:
        with self._lock:
            if field == "requests":
                self._requests += 1
            elif field == "blocked":
                self._blocked += 1
            else:
                self._errors += 1

    def _selected_target(self, host: str) -> bool:
        with self._lock:
            return host_is_selected(host, self._domains)

    def _active_tunnel(self, host: str, port: int):
        if not self._selected_target(host):
            raise _GatewayDenied(HTTPStatus.FORBIDDEN, "цель не выбрана для VPN-маршрута UmbraNet")
        profile = self._controller.active_profile()
        if profile is None:
            raise _GatewayDenied(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "нет готового VPN-сервера; прямое подключение заблокировано",
            )
        return open_route_tunnel(profile, host, port, self._timeout)

    def _handle_connect(self, client: socket.socket, authority: str, tail: bytes) -> None:
        host, port = _parse_authority(authority)
        with self._active_tunnel(host, port) as upstream:
            self._count("requests")
            self._send(client, b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: UmbraNet\r\n\r\n")
            if tail:
                upstream.sendall(tail)
            _relay(client, upstream)

    def _handle_http_request(self, client: socket.socket, method: str, target: str,
                             version: str, lines: list[bytes], tail: bytes) -> None:
        parsed = urlsplit(target)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("proxy ожидает абсолютный http:// или https:// URL")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        host = parsed.hostname
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        forwarded = [f"{method} {path} {version}".encode("iso-8859-1")]
        forwarded.extend(line for line in lines if not line.lower().startswith(b"proxy-connection:"))
        request = b"\r\n".join(forwarded) + b"\r\n\r\n" + tail
        with self._active_tunnel(host, port) as upstream:
            self._count("requests")
            upstream.sendall(request)
            _relay(client, upstream)
