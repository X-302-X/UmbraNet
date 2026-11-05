"""Проверка DNS-endpoint'ов и DoH-клиент с поддержкой HTTP/2.

Модуль используется и резолвером, и интерфейсом. Поэтому проверка профиля
делает настоящий DNS-запрос тем транспортом, который профиль реально использует,
а не открывает произвольный TCP-порт.
"""

from __future__ import annotations

import base64
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode, urlsplit, urlunsplit

from dnslib import DNSRecord

_DEFAULT_QNAME: Final = "google.com"
_MAX_DNS_RESPONSE: Final = 65_535


class DohError(RuntimeError):
    """Базовая ошибка запроса DNS-over-HTTPS."""


class DohProtocolError(DohError):
    """Сервер не согласовал HTTP/2 для защищённого DoH-сеанса."""


class DohHttpError(DohError):
    """HTTP-ответ DoH-сервера не является успешным."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP {status}")


@dataclass(frozen=True)
class ProbeResult:
    """Итог проверки одного DNS-endpoint'а для UI и фоновых задач."""

    ok: bool
    latency_ms: int | None
    detail: str


def _dns_question(name: str = _DEFAULT_QNAME, qtype: str = "A") -> DNSRecord:
    return DNSRecord.question(name, qtype)


def _address(host: str, port: int):
    """Адрес для socket с корректным форматом IPv6."""
    if ":" in host:
        return host, port, 0, 0
    return host, port


def _split_doh_url(url: str) -> tuple[str, int, str, str]:
    """Возвращает host, port, authority и request-target безопасного DoH URL."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise DohError("DoH URL должен начинаться с https:// и содержать имя хоста")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise DohError("некорректный порт в DoH URL") from exc
    if not 1 <= port <= 65_535:
        raise DohError("некорректный порт в DoH URL")
    authority = parts.hostname if port == 443 else f"{parts.hostname}:{port}"
    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return parts.hostname, port, authority, target


def _request_target(target: str, wire: bytes, method: str) -> str:
    if method == "POST":
        return target
    encoded = base64.urlsafe_b64encode(wire).rstrip(b"=").decode("ascii")
    separator = "&" if "?" in target else "?"
    return f"{target}{separator}{urlencode({'dns': encoded})}"


def _open_tls(host: str, port: int, server_hostname: str, timeout: float):
    raw = socket.create_connection((host, port), timeout=timeout)
    context = ssl.create_default_context()
    context.set_alpn_protocols(["h2"])
    try:
        tls = context.wrap_socket(raw, server_hostname=server_hostname)
    except Exception:
        raw.close()
        raise
    tls.settimeout(timeout)
    return tls


def _query_http2_payload(
    url: str,
    wire: bytes,
    *,
    timeout: float,
    connect_ip: str | None,
    method: str,
) -> tuple[bytes, str]:
    """Выполняет один DNS-over-HTTPS запрос поверх TLS + HTTP/2."""
    try:
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.events import (
            DataReceived,
            ResponseReceived,
            StreamEnded,
            StreamReset,
        )
    except ImportError as exc:
        raise DohProtocolError("не установлен пакет h2 для HTTP/2") from exc

    hostname, port, authority, target = _split_doh_url(url)
    target_host = connect_ip or hostname
    tls = _open_tls(target_host, port, hostname, timeout)
    try:
        if tls.selected_alpn_protocol() != "h2":
            raise DohProtocolError("сервер не согласовал HTTP/2 (ALPN h2)")

        connection = H2Connection(H2Configuration(client_side=True, header_encoding="utf-8"))
        connection.initiate_connection()
        tls.sendall(connection.data_to_send())

        stream_id = connection.get_next_available_stream_id()
        headers = [
            (":method", method),
            (":scheme", "https"),
            (":authority", authority),
            (":path", _request_target(target, wire, method)),
            ("accept", "application/dns-message"),
            ("user-agent", "UmbraNet/DoH"),
        ]
        body = wire if method == "POST" else b""
        if body:
            headers.append(("content-type", "application/dns-message"))
            headers.append(("content-length", str(len(body))))
        connection.send_headers(stream_id, headers, end_stream=not body)
        if body:
            connection.send_data(stream_id, body, end_stream=True)
        tls.sendall(connection.data_to_send())

        deadline = time.monotonic() + timeout
        response_headers: dict[str, str] = {}
        response_body = bytearray()
        ended = False
        while not ended:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("таймаут ожидания HTTP/2 DoH-ответа")
            tls.settimeout(remaining)
            data = tls.recv(16_384)
            if not data:
                raise ConnectionError("HTTP/2-соединение закрыто до конца DoH-ответа")
            for event in connection.receive_data(data):
                if isinstance(event, ResponseReceived) and event.stream_id == stream_id:
                    response_headers.update(dict(event.headers))
                elif isinstance(event, DataReceived) and event.stream_id == stream_id:
                    response_body.extend(event.data)
                    if len(response_body) > _MAX_DNS_RESPONSE:
                        raise DohError("DoH-ответ превышает допустимый размер")
                    connection.acknowledge_received_data(event.flow_controlled_length, stream_id)
                elif isinstance(event, StreamReset) and event.stream_id == stream_id:
                    raise ConnectionError(f"DoH-поток сброшен сервером: {event.error_code}")
                elif isinstance(event, StreamEnded) and event.stream_id == stream_id:
                    ended = True
            pending = connection.data_to_send()
            if pending:
                tls.sendall(pending)

        try:
            status = int(response_headers.get(":status", "0"))
        except ValueError as exc:
            raise DohError("HTTP/2 DoH-ответ не содержит корректный статус") from exc
        if not 200 <= status < 300:
            raise DohHttpError(status)
        content_type = response_headers.get("content-type", "").lower()
        if "application/dns-message" not in content_type:
            raise DohError(f"ожидался application/dns-message, получен {content_type or 'без Content-Type'}")
        return bytes(response_body), "HTTP/2"
    finally:
        tls.close()


def _query_http1_payload(url: str, wire: bytes, timeout: float) -> tuple[bytes, str]:
    """Совместимый fallback для корректных DoH-серверов без HTTP/2."""
    try:
        import requests
    except ImportError as exc:
        raise DohProtocolError("не установлен requests для HTTP/1.1 DoH fallback") from exc

    encoded = base64.urlsafe_b64encode(wire).rstrip(b"=").decode("ascii")
    headers = {"Accept": "application/dns-message", "User-Agent": "UmbraNet/DoH"}
    response = requests.get(url, headers=headers, params={"dns": encoded}, timeout=timeout)
    if response.status_code in (405, 501):
        response = requests.post(
            url,
            headers={**headers, "Content-Type": "application/dns-message"},
            data=wire,
            timeout=timeout,
        )
    if not 200 <= response.status_code < 300:
        raise DohHttpError(response.status_code)
    content_type = response.headers.get("content-type", "").lower()
    if "application/dns-message" not in content_type:
        raise DohError(f"ожидался application/dns-message, получен {content_type or 'без Content-Type'}")
    return response.content, "HTTP/1.1"


def query_doh_record(
    doh_url: str,
    request: DNSRecord,
    *,
    timeout: float = 5.0,
    connect_ip: str | None = None,
) -> tuple[DNSRecord, str]:
    """Отправляет DNSRecord по DoH и возвращает ответ вместе с транспортом.

    При ``connect_ip`` TCP/TLS-соединение идёт именно на этот IPv4/IPv6, но SNI
    и проверка сертификата остаются привязанными к имени из DoH URL. Это нужно,
    чтобы честно проверить каждый адрес профиля, не отключая TLS-проверку.
    """
    wire = request.pack()
    try:
        payload, transport = _query_http2_payload(
            doh_url, wire, timeout=timeout, connect_ip=connect_ip, method="GET"
        )
    except DohHttpError as exc:
        if exc.status not in (405, 501):
            raise
        payload, transport = _query_http2_payload(
            doh_url, wire, timeout=timeout, connect_ip=connect_ip, method="POST"
        )
    except DohProtocolError:
        if connect_ip:
            raise
        payload, transport = _query_http1_payload(doh_url, wire, timeout)

    try:
        response = DNSRecord.parse(payload)
    except Exception as exc:
        raise DohError("сервер вернул некорректный DNS wire-ответ") from exc
    if response.header.id != request.header.id:
        raise DohError("ID DNS-ответа не совпадает с запросом")
    return response, transport


def probe_doh_endpoint(
    doh_url: str,
    *,
    connect_ip: str | None = None,
    timeout: float = 4.0,
) -> ProbeResult:
    """Проверяет DoH URL или конкретный IP профиля настоящим DNS-запросом."""
    if not doh_url:
        return ProbeResult(False, None, "DoH URL не задан")
    started = time.perf_counter()
    try:
        response, transport = query_doh_record(
            doh_url, _dns_question(), timeout=timeout, connect_ip=connect_ip
        )
        latency = int((time.perf_counter() - started) * 1000)
        target = connect_ip or _split_doh_url(doh_url)[0]
        return ProbeResult(True, latency, f"{transport} · {target} · ответов: {len(response.rr)}")
    except Exception as exc:
        return ProbeResult(False, None, f"DoH: {exc}")


def probe_udp_endpoint(server_ip: str, *, timeout: float = 4.0) -> ProbeResult:
    """Проверяет обычный DNS реальным UDP-запросом, а не TCP-connect'ом."""
    if not server_ip:
        return ProbeResult(False, None, "адрес DNS не задан")
    request = _dns_question()
    family = socket.AF_INET6 if ":" in server_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    started = time.perf_counter()
    try:
        sock.settimeout(timeout)
        sock.sendto(request.pack(), _address(server_ip, 53))
        payload, _ = sock.recvfrom(_MAX_DNS_RESPONSE)
        response = DNSRecord.parse(payload)
        if response.header.id != request.header.id:
            raise DohError("ID DNS-ответа не совпадает с запросом")
        latency = int((time.perf_counter() - started) * 1000)
        return ProbeResult(True, latency, f"UDP/53 · {server_ip} · ответов: {len(response.rr)}")
    except Exception as exc:
        return ProbeResult(False, None, f"UDP DNS: {exc}")
    finally:
        sock.close()


def probe_profile_endpoint(profile: dict, endpoint: str, *, timeout: float = 4.0) -> ProbeResult:
    """Выбирает проверку из самого профиля, а не из глобального режима приложения."""
    doh_url = str((profile or {}).get("doh_url") or "").strip()
    if doh_url:
        return probe_doh_endpoint(doh_url, connect_ip=endpoint or None, timeout=timeout)
    return probe_udp_endpoint(endpoint, timeout=timeout)
