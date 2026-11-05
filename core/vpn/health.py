"""Strict user-owned HTTPS health probes for HTTP CONNECT and SOCKS5 exits."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import contextlib
import socket
import ssl
import time
from typing import Iterator
from urllib.parse import urlsplit

from .profiles import RouteProfile, normalize_probe_url


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """Validated endpoint used to prove a proxy carries real TLS traffic."""

    url: str
    host: str
    port: int
    path: str

    @property
    def authority(self) -> str:
        return self.host if self.port == 443 else f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class RouteProbeResult:
    """Structured evidence that a route works, rather than a process/port guess."""

    ok: bool
    stage: str
    latency_ms: int | None = None
    exit_country: str = ""
    error: str = ""
    service_tls_ok: bool = False

    def public_mapping(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "latency_ms": self.latency_ms,
            "exit_country": self.exit_country,
            "error": self.error,
            # Retained for existing UI/status consumers: it now means that
            # TLS to the configured user-owned probe endpoint was verified.
            "service_tls_ok": self.service_tls_ok,
        }


def probe_target_from_url(value: object) -> ProbeTarget | None:
    """Return a strict HTTPS target or ``None`` when verification is unavailable."""
    normalized = normalize_probe_url(value)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    return ProbeTarget(
        url=normalized,
        host=str(parsed.hostname),
        port=parsed.port or 443,
        path=parsed.path or "/",
    )


def _recv_until(sock: socket.socket, marker: bytes, *, limit: int) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(min(4096, limit - len(data)))
        if not chunk:
            raise ConnectionError("соединение закрыто до конца заголовка proxy")
        data.extend(chunk)
        if len(data) > limit:
            raise ConnectionError("заголовок proxy слишком большой")
    return bytes(data)


def _http_connect(proxy: RouteProfile, target_host: str, target_port: int, timeout: float) -> socket.socket:
    raw = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    raw.settimeout(timeout)
    try:
        lines = [
            f"CONNECT {target_host}:{target_port} HTTP/1.1",
            f"Host: {target_host}:{target_port}",
            "Proxy-Connection: keep-alive",
        ]
        if proxy.username:
            token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode("utf-8")).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        reply = _recv_until(raw, b"\r\n\r\n", limit=65536)
        status_line = reply.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != 200:
            raise ConnectionError(f"HTTP proxy отклонил CONNECT: {status_line[:200]}")
        return raw
    except Exception:
        raw.close()
        raise


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("SOCKS5 proxy закрыл соединение преждевременно")
        data.extend(chunk)
    return bytes(data)


def _socks5_connect(proxy: RouteProfile, target_host: str, target_port: int, timeout: float) -> socket.socket:
    raw = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    raw.settimeout(timeout)
    try:
        methods = b"\x00\x02" if proxy.username else b"\x00"
        raw.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _recv_exact(raw, 2)
        if version != 5 or method == 255:
            raise ConnectionError("SOCKS5 proxy не принял ни один метод авторизации")
        if method == 2:
            username = proxy.username.encode("utf-8")
            password = proxy.password.encode("utf-8")
            if not username or len(username) > 255 or len(password) > 255:
                raise ConnectionError("некорректные SOCKS5 учётные данные")
            raw.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            auth_version, status = _recv_exact(raw, 2)
            if auth_version != 1 or status != 0:
                raise ConnectionError("SOCKS5 proxy отклонил учётные данные")
        elif method != 0:
            raise ConnectionError("SOCKS5 proxy выбрал неподдерживаемый метод")

        # ATYP=domain deliberately delegates target resolution to the selected
        # exit; the local machine must never resolve the probe endpoint first.
        encoded_host = target_host.encode("idna")
        if not encoded_host or len(encoded_host) > 255:
            raise ConnectionError("некорректное имя цели для SOCKS5")
        raw.sendall(b"\x05\x01\x00\x03" + bytes([len(encoded_host)]) + encoded_host + target_port.to_bytes(2, "big"))
        head = _recv_exact(raw, 4)
        version, reply, _reserved, atyp = head
        if version != 5 or reply != 0:
            raise ConnectionError(f"SOCKS5 proxy отклонил CONNECT (код {reply})")
        address_sizes = {1: 4, 4: 16}
        if atyp == 3:
            address_len = _recv_exact(raw, 1)[0]
        elif atyp in address_sizes:
            address_len = address_sizes[atyp]
        else:
            raise ConnectionError("SOCKS5 proxy вернул неизвестный тип адреса")
        _recv_exact(raw, address_len + 2)
        return raw
    except Exception:
        raw.close()
        raise


@contextlib.contextmanager
def open_route_tunnel(profile: RouteProfile, target_host: str, target_port: int = 443,
                      timeout: float = 6.0) -> Iterator[socket.socket]:
    """Open a tunnel while deliberately passing the target name to the exit."""
    if profile.protocol == "http":
        raw = _http_connect(profile, target_host, target_port, timeout)
    elif profile.protocol == "socks5":
        raw = _socks5_connect(profile, target_host, target_port, timeout)
    else:  # RouteProfile validation makes this unreachable; retain safe failure.
        raise ValueError(f"unsupported route protocol: {profile.protocol}")
    try:
        yield raw
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _read_http_response(tls_sock: ssl.SSLSocket, *, limit: int = 65536) -> bytes:
    response = bytearray()
    while len(response) < limit:
        chunk = tls_sock.recv(min(8192, limit - len(response)))
        if not chunk:
            break
        response.extend(chunk)
        if b"\r\n\r\n" in response:
            break
    return bytes(response)


def _response_status(response: bytes) -> int | None:
    first_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    parts = first_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def probe_http_or_socks_route(profile: RouteProfile, *, probe_url: str = "",
                               timeout: float = 6.0) -> RouteProbeResult:
    """Prove the configured exit reaches a user-owned HTTPS endpoint.

    A missing or invalid endpoint is an explicit unverified state, not a weak
    TCP-only success.  The endpoint hostname travels inside CONNECT/SOCKS5 and
    TLS SNI, therefore no local DNS result can make a route look healthy.
    """
    target = probe_target_from_url(probe_url)
    if target is None:
        return RouteProbeResult(
            ok=False,
            stage="not_verified",
            error="не задан HTTPS endpoint проверки VPN-серверов",
        )

    started = time.monotonic()
    context = ssl.create_default_context()
    try:
        with open_route_tunnel(profile, target.host, target.port, timeout) as raw:
            with context.wrap_socket(raw, server_hostname=target.host) as tls_sock:
                request = (
                    f"GET {target.path} HTTP/1.1\r\n"
                    f"Host: {target.authority}\r\n"
                    "Connection: close\r\n"
                    "User-Agent: UmbraNet-route-health/1\r\n\r\n"
                ).encode("ascii")
                tls_sock.sendall(request)
                response = _read_http_response(tls_sock)
        status = _response_status(response)
        latency = round((time.monotonic() - started) * 1000)
        if status is None:
            return RouteProbeResult(
                ok=False,
                stage="probe_http",
                latency_ms=latency,
                error="endpoint вернул некорректный HTTP-ответ",
            )
        if not 200 <= status < 400:
            return RouteProbeResult(
                ok=False,
                stage="probe_http",
                latency_ms=latency,
                error=f"endpoint проверки ответил HTTP {status}",
            )
        return RouteProbeResult(
            ok=True,
            stage="ready",
            latency_ms=latency,
            service_tls_ok=True,
        )
    except Exception as exc:  # Network errors are expected probe results.
        return RouteProbeResult(
            ok=False,
            stage="transport",
            latency_ms=round((time.monotonic() - started) * 1000),
            error=str(exc),
        )
