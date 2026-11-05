"""
Basic probes for future UmbraNet AI strategy autotuning.

Проба — это проверка, работает ли тестовая цель на текущей сети/текущей
DPI-стратегии. Этот модуль пока НЕ переключает стратегии и НЕ создаёт Uz.
Он только даёт базовые измерения для будущего автоподбора.

Текущий уровень:
  • YouTube basic: DNS/TCP/TLS/HTTP для основных узлов;
  • Discord voice_readiness: API/CDN/media + WebSocket gateway + voice regions.

Реальный browser/video probe YouTube будет отдельным следующим уровнем.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

DEFAULT_TIMEOUT = 6.0

# Сколько проверок гоняем одновременно. Проверки независимы (разные хосты) и
# каждая может ждать сеть до таймаута, поэтому последовательный прогон — это
# сумма ожиданий: 11 проверок × 5 секунд = до минуты на ОДИН вариант стратегии.
# Именно это растягивало AI-генерацию на 5 минут при почти пустом результате.
# Параллельный прогон стоит столько, сколько самая медленная проверка.
# Что и как проверяется — не меняется: тот же список хостов, те же таймауты,
# тот же порядок результатов и то же начисление баллов.
PROBE_WORKERS = 6


def _run_parallel(thunks: list) -> list:
    """Выполняет проверки параллельно, СОХРАНЯЯ порядок результатов.

    Порядок важен: по индексу берутся обязательные проверки (gateway/voice),
    и результат должен совпадать с последовательным прогоном до последнего
    поля — иначе поедет и score, и отчёт.
    """
    if not thunks:
        return []
    if len(thunks) == 1:
        return [thunks[0]()]
    workers = max(1, min(PROBE_WORKERS, len(thunks)))
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            return list(pool.map(lambda fn: fn(), thunks))
    except Exception:
        # Пул не поднялся (нехватка ресурсов?) — честный последовательный путь:
        # лучше медленно, чем потерять прогон целиком.
        return [fn() for fn in thunks]


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _result(name: str, ok: bool, **extra) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), **extra}


def resolve_probe(host: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    started = _now_ms()
    try:
        # socket.getaddrinfo не принимает timeout напрямую, но общий timeout
        # процесса не меняем; это быстрый системный DNS-запрос.
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addrs: list[str] = []
        seen: set[str] = set()
        for info in infos:
            addr = str(info[4][0])
            if addr not in seen:
                seen.add(addr)
                addrs.append(addr)
        return _result(
            "dns",
            bool(addrs),
            host=host,
            addresses=addrs[:8],
            ms=round(_now_ms() - started, 1),
        )
    except Exception as exc:
        return _result("dns", False, host=host, error=str(exc), ms=round(_now_ms() - started, 1))


def https_probe(host: str, path: str = "/", method: str = "HEAD",
                timeout: float = DEFAULT_TIMEOUT, *,
                success_from: int = 100, success_to: int = 500) -> dict[str, Any]:
    """Минимальная DNS/TCP/TLS/HTTP проверка без внешних зависимостей.

    По умолчанию любой ответ 1xx–4xx — успех: 403/404 всё равно значат,
    что TLS дошёл. Для картинок Discord нужен настоящий 2xx (см. avatar).
    """
    started = _now_ms()
    method = (method or "HEAD").upper()
    dns = resolve_probe(host, timeout=timeout)
    if not dns.get("ok"):
        return _result(
            "https",
            False,
            host=host,
            path=path,
            stage="dns",
            dns=dns,
            ms=round(_now_ms() - started, 1),
        )

    sock = None
    ssock = None
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        sock = raw
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(raw, server_hostname=host)
        request = (
            f"{method} {path or '/'} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: UmbraNet-Probe/1.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        ssock.sendall(request)
        data = ssock.recv(4096)
        first_line = data.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace") if data else ""
        status = 0
        parts = first_line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
        # Для probes важен факт HTTP-ответа. 403/404 тоже доказывают, что TLS/HTTP
        # дошли до сервера; 5xx считаем слабым, но сетевой путь всё равно есть.
        # Картинки Discord (avatar GET) требуют 2xx — иначе стратегия «зелёная»,
        # а аватарки в клиенте не грузятся.
        ok = success_from <= status < success_to
        return _result(
            "https",
            ok,
            host=host,
            path=path,
            method=method,
            status=status,
            first_line=first_line,
            stage="http" if ok else "http_status",
            dns=dns,
            ms=round(_now_ms() - started, 1),
        )
    except Exception as exc:
        return _result(
            "https",
            False,
            host=host,
            path=path,
            method=method,
            stage="tls_or_http",
            dns=dns,
            error=str(exc),
            ms=round(_now_ms() - started, 1),
        )
    finally:
        for s in (ssock, sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def _read_ws_frame(sock: ssl.SSLSocket, timeout: float) -> tuple[int, bytes]:
    sock.settimeout(timeout)
    header = sock.recv(2)
    if len(header) < 2:
        raise RuntimeError("empty websocket frame")
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    length = b2 & 0x7F
    if length == 126:
        ext = sock.recv(2)
        if len(ext) < 2:
            raise RuntimeError("short websocket frame length")
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = sock.recv(8)
        if len(ext) < 8:
            raise RuntimeError("short websocket frame length")
        length = struct.unpack("!Q", ext)[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    return opcode, payload


def websocket_hello_probe(host: str, path: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """WebSocket handshake + чтение первого server frame.

    Для Discord gateway успешным считается получение JSON HELLO с op=10.
    """
    started = _now_ms()
    dns = resolve_probe(host, timeout=timeout)
    if not dns.get("ok"):
        return _result("websocket", False, host=host, path=path, stage="dns", dns=dns,
                       ms=round(_now_ms() - started, 1))

    sock = None
    ssock = None
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        sock = raw
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: UmbraNet-Probe/1.0\r\n\r\n"
        ).encode("ascii", "ignore")
        ssock.sendall(req)

        response = b""
        while b"\r\n\r\n" not in response and len(response) < 8192:
            chunk = ssock.recv(1024)
            if not chunk:
                break
            response += chunk
        head = response.decode("iso-8859-1", "replace")
        first_line = head.split("\r\n", 1)[0] if head else ""
        if " 101 " not in f" {first_line} ":
            return _result(
                "websocket",
                False,
                host=host,
                path=path,
                stage="upgrade",
                first_line=first_line,
                dns=dns,
                ms=round(_now_ms() - started, 1),
            )

        opcode, payload = _read_ws_frame(ssock, timeout=timeout)
        text = payload.decode("utf-8", "replace")
        parsed = None
        op = None
        heartbeat_interval = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                op = parsed.get("op")
                data = parsed.get("d") if isinstance(parsed.get("d"), dict) else {}
                heartbeat_interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
        except Exception:
            pass
        ok = op == 10
        return _result(
            "websocket",
            ok,
            host=host,
            path=path,
            stage="hello" if ok else "frame",
            opcode=opcode,
            gateway_op=op,
            heartbeat_interval=heartbeat_interval,
            first_line=first_line,
            dns=dns,
            ms=round(_now_ms() - started, 1),
        )
    except Exception as exc:
        return _result(
            "websocket",
            False,
            host=host,
            path=path,
            stage="tls_or_ws",
            dns=dns,
            error=str(exc),
            ms=round(_now_ms() - started, 1),
        )
    finally:
        for s in (ssock, sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def probe_youtube_basic(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Базовая YouTube-проверка без браузерного воспроизведения.

    2026-10-06 (поле): YT Music «вис», хотя www.youtube.com работал. Поэтому
    music.youtube.com и redirector.googlevideo.com — ОБЯЗАТЕЛЬНЫЕ проверки:
    стратегия, которая ломает музыку или медиа-потоки, не должна сохраняться.
    """
    started = _now_ms()
    checks = _run_parallel([
        lambda: https_probe("www.youtube.com", "/generate_204", "GET", timeout),
        lambda: https_probe("music.youtube.com", "/", "GET", timeout),
        lambda: https_probe("redirector.googlevideo.com", "/generate_204", "GET", timeout),
        lambda: https_probe("youtubei.googleapis.com", "/", "HEAD", timeout),
        lambda: https_probe("i.ytimg.com", "/", "HEAD", timeout),
        lambda: https_probe("redirector.googlevideo.com", "/", "HEAD", timeout),
    ])
    # Индексы фиксированы порядком списка выше: music — 1, redirector /generate_204 — 2.
    music = checks[1] if len(checks) > 1 else {}
    media = checks[2] if len(checks) > 2 else {}
    ok_count = sum(1 for c in checks if c.get("ok"))
    music_ok = bool(music.get("ok"))
    media_ok = bool(media.get("ok"))
    # Обязательные: музыка (сайт YT Music) и медиа (googlevideo — треки).
    # Без них стратегия «зелёная», а пользователь слышит вечную загрузку.
    ok = ok_count >= 5 and music_ok and media_ok
    return {
        "service": "youtube",
        "level": "basic",
        "ok": ok,
        "score": round(ok_count / max(len(checks), 1) * 100),
        "required": {
            "music": music_ok,
            "media": media_ok,
        },
        "checks": checks,
        "parallel": True,
        "ms": round(_now_ms() - started, 1),
    }


def probe_discord_basic(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Discord-проверка с упором на звонки.

    Вечный статус «Подключение» в Discord чаще ломается не на сайте, а на связке:
      • API/gateway WebSocket;
      • voice regions API;
      • CDN/media-домены, через которые клиент получает аватарки и картинки.

    HEAD / на cdn.discordapp.com часто отвечает 403 — TLS есть, а картинка нет.
    Поэтому аватарку качаем по-настоящему: публичный embed/avatars/0.png, только 2xx.

    Полностью проверить реальный звонок без аккаунта/токена невозможно, поэтому
    этот probe называется voice_readiness: он не гарантирует звонок на 100%, но
    отсекает стратегии, при которых Discord UI может открываться, а голосовая
    часть всё равно не готова.
    """
    started = _now_ms()
    checks = _run_parallel([
        lambda: https_probe("discord.com", "/api/v10/gateway", "GET", timeout),
        lambda: https_probe("discord.com", "/api/v10/voice/regions", "GET", timeout),
        lambda: https_probe(
            "cdn.discordapp.com", "/embed/avatars/0.png", "GET", timeout,
            success_from=200, success_to=300,
        ),
        lambda: https_probe("media.discordapp.net", "/", "HEAD", timeout),
        lambda: https_probe("dl.discordapp.net", "/", "HEAD", timeout),
        lambda: websocket_hello_probe("gateway.discord.gg", "/?v=10&encoding=json", timeout),
    ])
    # Индексы фиксированы порядком списка выше: voice/regions — 1, gateway WS — 5,
    # CDN-аватар — 2.
    voice_regions = checks[1] if len(checks) > 1 else {}
    cdn_avatar = checks[2] if len(checks) > 2 else {}
    gateway_ws = checks[5] if len(checks) > 5 else {}
    ok_count = sum(1 for c in checks if c.get("ok"))
    gateway_ok = bool(gateway_ws.get("ok"))
    voice_ok = bool(voice_regions.get("ok"))
    cdn_ok = bool(cdn_avatar.get("ok"))
    # Gateway и voice — звонки. CDN 2xx — аватарки/картинки. Без CDN стратегия
    # выглядит «зелёной», а в Discord пустые квадраты вместо изображений.
    ok = ok_count >= 4 and gateway_ok and voice_ok and cdn_ok
    return {
        "service": "discord",
        "level": "voice_readiness",
        "ok": ok,
        "score": round(ok_count / max(len(checks), 1) * 100),
        "required": {
            "gateway_ws": gateway_ok,
            "voice_regions": voice_ok,
            "cdn_avatar": cdn_ok,
        },
        "checks": checks,
        "parallel": True,
        "ms": round(_now_ms() - started, 1),
    }


def run_basic_probes(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Запускает базовые probes для generation targets YouTube + Discord."""
    started = _now_ms()
    # YouTube и Discord тоже независимы — идём параллельно.
    services = _run_parallel([
        lambda: probe_youtube_basic(timeout=timeout),
        lambda: probe_discord_basic(timeout=timeout),
    ])
    ok_count = sum(1 for s in services if s.get("ok"))
    return {
        "stage": "basic_probes",
        "ok": ok_count == len(services),
        "score": round(sum(int(s.get("score", 0) or 0) for s in services) / max(len(services), 1)),
        "services": services,
        "ms": round(_now_ms() - started, 1),
    }
