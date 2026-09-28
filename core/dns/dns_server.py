"""
UmbraNet - Локальный DNS-сервер с выборочной маршрутизацией

Как работает:
  - Слушает на 127.0.0.1:53 и ::1:53 одновременно по UDP и TCP
  - Для ВЫБРАННЫХ доменов (chatgpt.com и др.) → резолвит через xbox-dns.ru
    и прозрачно проксирует полный upstream DNS-ответ
  - Для ОСТАЛЬНЫХ доменов → обычный DNS провайдера (без изменений)

Проблема без IPv6: браузер делает AAAA запрос через IPv6 стек,
который не попадает к нашему DNS (слушающему только 127.0.0.1).
Решение: слушаем также на ::1 и прописываем оба DNS в Windows.
"""

import base64
import copy
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time

import requests

from core.diagnostics import log_recoverable
from bogus_ips import (
    build_bogus_index_with_cache,
    response_contains_bogus,
)
from config_utils import _to_bool, load_config_file, save_config_file
from dns_cache import DNSCache
from dns_helpers import cap_response_ttl as _cap_response_ttl
from dns_helpers import ordered_upstreams as _ordered_upstreams
from dnslib import QTYPE, RCODE, DNSRecord
from dnslib.server import BaseResolver, DNSLogger, DNSServer
from profile_utils import (
    BUILTIN_DNS_PROFILE,
    get_active_dns_profile,
    get_failover_providers,
)
from fallback_state import get_fallback_state
from provider_health import get_provider_health
from query_log import (
    SOURCE_BLOCKED,
    SOURCE_BOGUS_NX,
    SOURCE_CACHE_FRESH,
    SOURCE_CACHE_STALE,
    SOURCE_ROUTED,
    SOURCE_SERVFAIL,
    SOURCE_SYSTEM,
    QueryLogEntry,
    get_query_log,
)
from routing_utils import is_domain_allowed, is_domain_blocked, is_domain_routed
from upstream_strategy import (
    MODE_FASTEST,
    MODE_PARALLEL,
    get_upstream_stats,
    normalize_mode,
    race_upstreams,
    reorder_for_fastest,
)
from winws_engine import get_winws_engine

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(PROJECT_ROOT, 'umbranet.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("UmbraNet.DNS")

CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")
_CORE_DIR   = PROJECT_ROOT   # нужен BogusUpdater'у для диск-кэша

# Переменная среды `UMBRANET_CONFIG` позволяет увести рабочую настройку в сторону:
# этим пользуются тесты, чтобы прогон не переписывал config.json проекта. Путь
# читается на каждый вызов, а не один раз при импорте, — иначе подмена в тестах
# срабатывала бы слишком поздно.
CONFIG_FILE_ENV = "UMBRANET_CONFIG"


def config_file() -> str:
    """Путь к config.json: переменная среды (тесты), иначе файл проекта."""
    return os.environ.get(CONFIG_FILE_ENV) or CONFIG_FILE

# ── xbox-dns.ru адреса ────────────────────────────────────────────────────────
# Единый источник правды для IP — profile_utils.BUILTIN_DNS_PROFILE.
# Резолв берёт адреса из активного профиля, поэтому здесь дубли не нужны.

def load_config():
    return load_config_file(config_file(), logger=log)


def save_config(cfg):
    return save_config_file(config_file(), cfg, logger=log)


def _question_meta(request) -> tuple:
    """Возвращает (domain, qtype) для логов и маршрутизации."""
    domain = str(request.q.qname).rstrip('.')
    try:
        qtype = QTYPE[request.q.qtype]
    except Exception:
        qtype = str(request.q.qtype)
    return domain, qtype


def _response_summary(response) -> str:
    """Короткое описание ответа апстрима для логов."""
    if response is None:
        return "no response"

    try:
        rcode_name = RCODE[response.header.rcode]
    except Exception:
        rcode_name = str(response.header.rcode)

    preview = []
    for rr in response.rr[:4]:
        try:
            rr_type = QTYPE[rr.rtype]
        except Exception:
            rr_type = str(rr.rtype)
        preview.append(f"{rr_type} {rr.rdata}")

    if preview:
        preview_text = "; ".join(preview)
        if len(response.rr) > 4:
            preview_text += " ..."
    else:
        preview_text = "no-answer"

    return (
        f"rcode={rcode_name}, answer={len(response.rr)}, "
        f"auth={len(response.auth)}, add={len(response.ar)}, {preview_text}"
    )


def _ip_family(server_ip: str) -> int:
    return socket.AF_INET6 if ipaddress.ip_address(server_ip).version == 6 else socket.AF_INET



def _socket_address(server_ip: str, port: int):
    if _ip_family(server_ip) == socket.AF_INET6:
        return (server_ip, port, 0, 0)
    return (server_ip, port)


def _probe_umbranet_dns_running(host: str, port: int) -> bool:
    """Проверяет, слушает ли на host:port СОБСТВЕННЫЙ DNS UmbraNet.

    Отправляет тестовый DNS-запрос и проверяет, что пришёл валидный ответ.
    Это позволяет отличить «наш сервер ещё работает» от «порт занят чужой
    программой» — не полагаясь только на флаг self.running.
    """
    try:
        family = _ip_family(host)
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        try:
            q = DNSRecord.question("localhost", "A")
            sock.sendto(q.pack(), _socket_address(host, port))
            data, _ = sock.recvfrom(512)
            resp = DNSRecord.parse(data)
            # Если ID совпадает и это ответ — слушает наш DNSlib-сервер.
            if resp.header.id == q.header.id and resp.header.qr:
                return True
        except Exception:
            pass
        finally:
            sock.close()
    except Exception:
        pass
    return False


def check_port_available(host: str, port: int) -> tuple:
    """Проверяет, можно ли занять UDP-порт на host:port ДО запуска сервера.

    Возвращает (ok: bool, reason: str). reason пустой при ok=True.
    Проверяем именно UDP (основной транспорт DNS) — пытаемся забиндиться
    на короткое время. Если занято/нет прав — сообщаем по-человечески.

    Особый случай: если на порту работает СОБСТВЕННЫЙ DNS UmbraNet — это не
    конфликт, а штатный restart. Сервер будет остановлен перед новым запуском,
    поэтому считаем порт доступным.
    """
    family = _ip_family(host)

    # Делаем 3 попытки с небольшой паузой — это защищает от ложных
    # срабатываний при быстром рестарте, когда ОС еще не успела освободить порт.
    import time
    for attempt in range(3):
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            # Не ставим SO_REUSEADDR/REUSEPORT — нам нужно увидеть реальный конфликт.
            sock.bind(_socket_address(host, port))
            sock.close()
            return True, ""
        except PermissionError:
            sock.close()
            return False, (
                f"нет прав на порт {port} (нужны права администратора)"
            )
        except OSError as exc:
            sock.close()
            errno = getattr(exc, "errno", None)
            winerr = getattr(exc, "winerror", None)
            if winerr == 10048 or errno in (98, 48):  # EADDRINUSE (linux/mac)
                if attempt < 2:
                    time.sleep(0.2)
                    continue
                # ── Ключевая проверка: это НАШ собственный DNS? ────────
                # После restart / stop→start флаг self.running может быть
                # уже сброшен, но ОС ещё не освободила сокет. Или сервер
                # работает, а preflight запустился по другой причине.
                # В обоих случаях — это не конфликт: старый сервер будет
                # остановлен перед запуском нового.
                if _probe_umbranet_dns_running(host, port):
                    return True, ""
                return False, (
                    f"порт {port} уже занят другой программой "
                    f"(системный DNS-клиент, Pi-hole/AdGuard или второй экземпляр UmbraNet)"
                )
            if winerr == 10013 or errno == 13:  # access denied
                return False, f"нет прав на порт {port} (нужны права администратора)"
            return False, f"не удалось занять {host}:{port}: {exc}"

    return False, "Неизвестная ошибка порта"


def preflight_check(config: dict) -> tuple:
    """Предстартовые проверки: права и доступность порта 53.

    Возвращает (ok: bool, problems: list[str], warnings: list[str]).
    ok=False означает, что запускать бессмысленно — будет ошибка.
    """
    problems = []
    warnings = []

    port = int(config.get("listen_port", 53))

    # 1) Права администратора (порт 53 привилегированный; смена системного DNS
    #    тоже требует прав). На не-Windows is_admin проверяет root.
    try:
        import sys

        from process_monitor import is_admin
        if port < 1024 and sys.platform != "win32" and not is_admin():
            problems.append(
                f"нужны права администратора для порта {port} "
                f"(запустите от имени администратора)"
            )
    except Exception as exc:
        log_recoverable(log, 'Не удалось проверить права для DNS-порта', exc, level=logging.WARNING)

    # 2) IPv4 порт
    host4 = config.get("listen_host", "127.0.0.1")
    ok4, reason4 = check_port_available(host4, port)
    if not ok4:
        problems.append(f"IPv4 {host4}:{port} — {reason4}")

    # 3) IPv6 порт (если включён) — только предупреждение, не блокер
    if _to_bool(config.get("enable_ipv6", True), True):
        host6 = config.get("listen_host6", "::1")
        ok6, reason6 = check_port_available(host6, port)
        if not ok6:
            warnings.append(f"IPv6 {host6}:{port} — {reason6}")

    return (len(problems) == 0), problems, warnings



def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """Читает ровно size байт из TCP сокета."""
    chunks = []
    remaining = size
    while remaining > 0:
        data = sock.recv(remaining)
        if not data:
            raise ConnectionError("Соединение закрыто до получения полного DNS-ответа")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _query_tcp_dns(server_ip: str, request, timeout: float = 4.0):
    """Запрос к DNS-серверу по TCP (используется как fallback при TC=1)."""
    wire = request.pack()
    family = _ip_family(server_ip)
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(_socket_address(server_ip, 53))
        sock.sendall(len(wire).to_bytes(2, "big") + wire)
        resp_len = int.from_bytes(_recv_exact(sock, 2), "big")
        return DNSRecord.parse(_recv_exact(sock, resp_len))
    finally:
        try:
            sock.close()
        except Exception as exc:
            log_recoverable(log, 'Не удалось закрыть TCP-сокет upstream', exc, level=logging.DEBUG)


def _query_udp_dns(server_ip: str, request, timeout: float = 4.0):
    """Запрос к DNS-серверу по UDP с автоматическим переходом на TCP при truncation."""
    family = _ip_family(server_ip)
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(request.pack(), _socket_address(server_ip, 53))
        data, _ = sock.recvfrom(65535)
    finally:
        try:
            sock.close()
        except Exception as exc:
            log_recoverable(log, 'Не удалось закрыть UDP-сокет upstream', exc, level=logging.DEBUG)

    response = DNSRecord.parse(data)
    if getattr(response.header, "tc", 0):
        domain, qtype = _question_meta(request)
        log.info(f"UDP-ответ обрезан (TC=1) для {domain} {qtype}, повторяем по TCP через {server_ip}")
        tcp_response = _query_tcp_dns(server_ip, request, timeout=timeout)
        if tcp_response is not None:
            return tcp_response
    return response


def _query_doh_dns(doh_url: str, request, timeout: float = 5.0):
    """DoH-запрос с GET и POST fallback, сохраняя исходный DNS wire-format."""
    wire = request.pack()

    try:
        b64 = base64.urlsafe_b64encode(wire).rstrip(b'=').decode()
        response = requests.get(
            doh_url,
            headers={"Accept": "application/dns-message"},
            params={"dns": b64},
            timeout=timeout,
        )
        response.raise_for_status()
        return DNSRecord.parse(response.content)
    except Exception as exc:
        log_recoverable(log, 'DoH POST не удался; пробуем GET', exc, level=logging.DEBUG)

    response = requests.post(
        doh_url,
        headers={
            "Content-Type": "application/dns-message",
            "Accept": "application/dns-message",
        },
        data=wire,
        timeout=timeout,
    )
    response.raise_for_status()
    return DNSRecord.parse(response.content)


def _run_upstreams_with_strategy(
    request,
    upstream_list,
    config: dict,
    label_prefix: str,
    timeout: float = 4.0,
    log_level_fail=None,
):
    """Запускает список upstream-серверов в соответствии с config["upstream_mode"].

    upstream_list — список кортежей (family_name, dns_ip).
    label_prefix — префикс для логов и статистики ("xbox-dns" / "system").
    Возвращает первый успешный response или None.
    """
    if not upstream_list:
        return None

    domain, qtype = _question_meta(request)
    mode = normalize_mode(config.get("upstream_mode"))
    if log_level_fail is None:
        log_level_fail = log.warning

    stats = get_upstream_stats()

    # Готовим (label, callable) пары — закрытие фиксирует dns_ip.
    def _make_task(family_name, dns_ip):
        label = f"{label_prefix} UDP {family_name} {dns_ip}"

        def _do():
            t0 = time.monotonic()
            try:
                resp = _query_udp_dns(dns_ip, request, timeout=timeout)
                if resp is not None:
                    log.info(
                        f"[{label}] {domain} {qtype} → {_response_summary(resp)}"
                    )
                    stats.record_win(label, (time.monotonic() - t0) * 1000)
                return resp
            except Exception as exc:
                log_level_fail(
                    f"{label} не ответил для {domain} {qtype}: {exc}"
                )
                stats.record_fail(label)
                return None

        return (label, _do)

    tasks = [_make_task(fn, ip) for fn, ip in upstream_list]

    # ── Режим: PARALLEL — гонка всех сразу
    if mode == MODE_PARALLEL:
        resp, winner, _elapsed = race_upstreams(tasks, timeout=timeout)
        if resp is not None and winner:
            log.debug(f"[parallel] победитель: {winner}")
        return resp

    # ── Режим: FASTEST — обычно по очереди от лидера; раз в N запросов
    #    делаем «прозвон» параллельным режимом, чтобы перетряхнуть рейтинг.
    if mode == MODE_FASTEST:
        if stats.should_probe():
            log.debug(f"[fastest] probe-цикл — гонка всех {len(tasks)} upstream'ов")
            resp, winner, _e = race_upstreams(tasks, timeout=timeout)
            if resp is not None:
                return resp
            # Если probe не дал ответа — продолжаем последовательно.
        # Обычный путь: лидер первый, остальные fallback.
        ordered = reorder_for_fastest(tasks, stats)
        for _label, fn in ordered:
            r = fn()
            if r is not None:
                return r
        return None

    # ── Режим: SEQUENTIAL (по умолчанию, как было исторически)
    for _label, fn in tasks:
        r = fn()
        if r is not None:
            return r
    return None


def resolve_via_xbox_udp(request, config: dict, timeout: float = 4.0, profile: dict = None):
    """Резолвит через DNS-профиль по UDP/TCP, сохраняя полный DNS-ответ апстрима.

    profile=None → активный профиль из конфига. Если передан явно — используется
    он (нужно для failover между несколькими провайдерами).
    Режим опроса (sequential / parallel / fastest) берётся из
    config["upstream_mode"]; см. upstream_strategy.py.
    """
    if profile is None:
        profile = get_active_dns_profile(config)
    qtype = _question_meta(request)[1]
    upstreams = _ordered_upstreams(
        qtype,
        [profile.get("ipv4_primary"), profile.get("ipv4_secondary")],
        [profile.get("ipv6_primary"), profile.get("ipv6_secondary")],
    )
    return _run_upstreams_with_strategy(
        request, upstreams, config,
        label_prefix=profile.get("name", "provider"),
        timeout=timeout,
        log_level_fail=log.warning,
    )


def resolve_via_xbox_doh(request, config: dict, profile: dict = None):
    """Резолвит через DoH DNS-профиля, сохраняя полный DNS-ответ апстрима."""
    domain, qtype = _question_meta(request)
    if profile is None:
        profile = get_active_dns_profile(config)
    doh_url = profile.get("doh_url") or BUILTIN_DNS_PROFILE["doh_url"]
    try:
        response = _query_doh_dns(doh_url, request, timeout=5.0)
        log.info(f"[{profile.get('name')} DoH] {domain} {qtype} → {_response_summary(response)}")
        return response
    except Exception as e:
        log.error(f"{profile.get('name')} DoH не удался для {domain} {qtype}: {e}")
        return None


def _profile_dot_doq_targets(profile: dict, transport: str = "dot"):
    """Возвращает ``(server_ip, hostname)`` для одного secure-транспорта.

    DoT и DoQ могут использовать разные endpoint'ы. Раньше функция всегда
    брала ``dot_*`` перед ``doq_*`` (и наоборот через общий fallback), поэтому
    пользовательский профиль с разными адресами мог отправлять DoQ на DoT-сервер
    или DoT на DoQ-сервер. Для отсутствующих специальных полей сохраняем
    fallback на обычные IPv4/IPv6 адреса профиля.
    """
    is_doq = str(transport).strip().lower() == "doq"
    host_key = "doq_host" if is_doq else "dot_host"
    ip_key = "doq_ip" if is_doq else "dot_ip"
    hostname = profile.get(host_key) or _hostname_from_url(profile.get("doh_url"))
    server_ip = (
        profile.get(ip_key)
        or profile.get("ipv4_primary")
        or profile.get("ipv6_primary")
    )
    return server_ip, hostname


def _hostname_from_url(url: str):
    if not url:
        return None
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).hostname
    except Exception:
        return None


def resolve_via_xbox_dot(request, config: dict, timeout: float = 5.0, profile: dict = None):
    """Резолвит через DoT (DNS-over-TLS) профиля."""
    from dns_transports import DOT_PORT, query_dot
    domain, qtype = _question_meta(request)
    if profile is None:
        profile = get_active_dns_profile(config)
    server_ip, hostname = _profile_dot_doq_targets(profile, "dot")
    if not server_ip:
        log.warning(f"DoT: в профиле нет адреса сервера для {domain} {qtype}")
        return None
    port = int(profile.get("dot_port", DOT_PORT) or DOT_PORT)
    verify = _to_bool(config.get("tls_verify", True), True)
    # При проверке сертификата нужен SNI/имя для сопоставления. Если профиль
    # задан только IP-адресом, проверяем IP SAN, а не молча отключаем TLS verify.
    hostname = hostname or (server_ip if verify else None)
    try:
        response = query_dot(
            server_ip, request, timeout=timeout, port=port,
            server_hostname=hostname, verify=verify,
        )
        log.info(f"[{profile.get('name')} DoT] {domain} {qtype} → {_response_summary(response)}")
        return response
    except Exception as e:
        log.error(f"{profile.get('name')} DoT не удался для {domain} {qtype}: {e}")
        return None


def resolve_via_xbox_doq(request, config: dict, timeout: float = 5.0, profile: dict = None):
    """Резолвит через DoQ (DNS-over-QUIC) профиля."""
    from dns_transports import DOQ_PORT, query_doq
    domain, qtype = _question_meta(request)
    if profile is None:
        profile = get_active_dns_profile(config)
    server_ip, hostname = _profile_dot_doq_targets(profile, "doq")
    if not server_ip:
        log.warning(f"DoQ: в профиле нет адреса сервера для {domain} {qtype}")
        return None
    port = int(profile.get("doq_port", DOQ_PORT) or DOQ_PORT)
    verify = _to_bool(config.get("tls_verify", True), True)
    hostname = hostname or (server_ip if verify else None)
    try:
        response = query_doq(
            server_ip, request, timeout=timeout, port=port,
            server_hostname=hostname, verify=verify,
        )
        log.info(f"[{profile.get('name')} DoQ] {domain} {qtype} → {_response_summary(response)}")
        return response
    except Exception as e:
        log.error(f"{profile.get('name')} DoQ не удался для {domain} {qtype}: {e}")
        return None


def resolve_via_xbox_dnscrypt(request, config: dict, timeout: float = 5.0, profile: dict = None):
    """Резолвит через DNSCrypt по sdns:// штампу из профиля (dnscrypt_stamp)."""
    from dnscrypt import query_dnscrypt
    domain, qtype = _question_meta(request)
    if profile is None:
        profile = get_active_dns_profile(config)
    stamp = profile.get("dnscrypt_stamp")
    if not stamp:
        log.warning(f"DNSCrypt: в профиле нет sdns-штампа для {domain} {qtype}")
        return None
    try:
        response = query_dnscrypt(stamp, request, timeout=timeout)
        log.info(f"[{profile.get('name')} DNSCrypt] {domain} {qtype} → {_response_summary(response)}")
        return response
    except Exception as e:
        log.error(f"{profile.get('name')} DNSCrypt не удался для {domain} {qtype}: {e}")
        return None


# Сопоставление режима → ИМЯ функции-резолвера транспорта. Храним имена, а не
# сами функции, чтобы _xbox_transport() резолвил их через globals() в момент
# вызова — это позволяет подменять транспорты в тестах (monkeypatch) и держит
# единый источник правды.
_XBOX_TRANSPORT_NAMES = {
    "udp": "resolve_via_xbox_udp",
    "doh": "resolve_via_xbox_doh",
    "dot": "resolve_via_xbox_dot",
    "doq": "resolve_via_xbox_doq",
    "dnscrypt": "resolve_via_xbox_dnscrypt",
}

# Порядок fallback'а для каждого режима: сначала выбранный, затем остальные.
# Так смена/блокировка одного транспорта не оставляет пользователя без ответа.
_XBOX_FALLBACK_ORDER = {
    "udp": ["udp", "doh", "dot", "doq"],
    "doh": ["doh", "dot", "udp", "doq"],
    "dot": ["dot", "doh", "udp", "doq"],
    "doq": ["doq", "doh", "dot", "udp"],
    # DNSCrypt — выбор продвинутого пользователя для своего профиля; если штамп
    # не задан/не сработал, падаем на DoH как универсальный fallback.
    "dnscrypt": ["dnscrypt", "doh", "dot", "udp"],
}


class _SkipRecord(Exception):
    """Служебный сигнал: ответ получен, но в статистику его писать не нужно."""


def _xbox_transport(name: str):
    """Возвращает функцию-резолвер транспорта по имени режима (через globals,
    чтобы поддерживать monkeypatch в тестах)."""
    fn_name = _XBOX_TRANSPORT_NAMES[name]
    return globals()[fn_name]


def _resolve_via_provider(request, config, profile, transport_order):
    """Пробует один провайдер по цепочке транспортов.

    Возвращает ``(response, transport)``: вызывающему коду нужно знать не только
    «ответили», но и КАКИМ транспортом — это показывается пользователю
    (карточка «Сейчас отвечает»: ответ через запасной транспорт значит, что
    запрос, например, ушёл открытым UDP вместо DoH). При неудаче — ``(None, "")``.
    """
    domain, qtype = _question_meta(request)
    first = transport_order[0]
    for transport in transport_order:
        resolver = _xbox_transport(transport)
        response = resolver(request, config, profile=profile)
        if response is not None:
            if transport != first:
                log.info(
                    f"{profile.get('name')}: {domain} {qtype} — ответ через "
                    f"fallback-транспорт {transport.upper()}"
                )
            return response, transport
    return None, ""


def resolve_via_xbox(request, config: dict, record: bool = True):
    """
    Резолвит через провайдеров с failover. Для каждого провайдера пробуется
    цепочка транспортов (UDP/DoH/DoT/DoQ). Если активный провайдер не отвечает
    ни одним транспортом — НЕЗАМЕТНО переключаемся на следующего провайдера
    (xbox-dns → comss.one → пользовательские). Так падение одного сервиса не
    оставляет пользователя без доступа.
    `record=False` — служебная проба (например предварительный запрос AAAA для
    приоритета IPv6): ответ клиенту не поедет, и записывать его в статистику
    «сейчас отвечает» нельзя, иначе индикатор показывал бы не то, что получил
    пользователь.
    """
    domain, qtype = _question_meta(request)
    mode = str(config.get("xbox_dns_mode", "doh")).strip().lower()
    if mode not in _XBOX_TRANSPORT_NAMES:
        mode = "doh"
    transport_order = _XBOX_FALLBACK_ORDER.get(mode, ["doh", "dot", "udp", "doq"])

    providers = get_failover_providers(config)
    health = get_provider_health()
    primary_id = providers[0].get("id") if providers else None

    for idx, profile in enumerate(providers):
        pid = profile.get("id")
        pname = profile.get("name")
        response, transport = _resolve_via_provider(
            request, config, profile, transport_order
        )
        if response is not None:
            health.record_success(pid, pname)
            # Кто и каким транспортом ответил — для индикатора в «Сети и
            # диагностике». Ничего не решает, только фиксирует факт.
            try:
                if not record:
                    raise _SkipRecord
                get_fallback_state().record_provider_answer(
                    provider_id=pid or "",
                    provider_name=pname or pid or "",
                    transport=transport,
                    preferred_transport=transport_order[0],
                    provider_index=idx,
                    domain=domain,
                    is_primary=(pid == primary_id),
                )
            except _SkipRecord:
                pass
            except Exception as exc:
                log.debug("fallback state (провайдер) не записан: %s", exc)
            if idx > 0:
                log.info(
                    f"{domain} {qtype}: основной провайдер недоступен, "
                    f"ответ получен через запасной «{pname}»"
                )
            return response
        # провайдер не ответил ни одним транспортом
        health.record_failure(pid, pname)
        if pid == primary_id:
            log.warning(
                f"Провайдер «{pname}» не ответил для {domain} {qtype}, "
                f"пробуем запасные провайдеры"
            )

    log.error(f"Ни один провайдер не ответил для {domain} {qtype}")
    return None


def resolve_via_system(request, config: dict, timeout: float = 4.0):
    """Резолвит через обычный DNS, перебирая IPv4/IPv6 fallback серверы.

    Режим опроса (sequential / parallel / fastest) берётся из
    config["upstream_mode"]; см. upstream_strategy.py.
    """
    qtype = _question_meta(request)[1]
    upstreams = _ordered_upstreams(
        qtype,
        [config.get("fallback_dns")],
        [config.get("fallback_dns6")],
    )
    return _run_upstreams_with_strategy(
        request, upstreams, config,
        label_prefix="system DNS",
        timeout=timeout,
        log_level_fail=log.error,
    )


def _proxy_reply_from_upstream(request, upstream):
    """Собирает локальный ответ, максимально точно сохраняя upstream reply."""
    reply = request.reply()
    reply.header.rcode = upstream.header.rcode

    for flag in ("aa", "ra", "rd", "tc", "ad", "cd"):
        if hasattr(upstream.header, flag) and hasattr(reply.header, flag):
            try:
                setattr(reply.header, flag, getattr(upstream.header, flag))
            except Exception as exc:
                log_recoverable(log, 'Не удалось перенести флаг DNS-ответа', exc, level=logging.DEBUG)

    for rr in upstream.rr:
        reply.add_answer(rr)
    for rr in upstream.auth:
        reply.add_auth(rr)
    for rr in upstream.ar:
        reply.add_ar(rr)

    return reply


def _servfail_reply(request):
    """Строит корректный SERVFAIL вместо пустого «успешного» ответа."""
    reply = request.reply()
    reply.header.rcode = getattr(RCODE, "SERVFAIL", 2)
    return reply


def _nxdomain_reply(request):
    """Строит NXDOMAIN-ответ (когда мы намеренно говорим "такого домена нет")."""
    reply = request.reply()
    reply.header.rcode = getattr(RCODE, "NXDOMAIN", 3)
    return reply


def _answers_preview(response, limit: int = 3):
    """Короткий список IP/CNAME для UI, без перегрузки длинного списка."""
    if response is None:
        return []
    out = []
    for rr in getattr(response, "rr", []) or []:
        rdata = str(getattr(rr, "rdata", "")).strip()
        if rdata:
            out.append(rdata)
        if len(out) >= limit:
            break
    return out


def _rcode_name(response):
    if response is None:
        return "NORESP"
    try:
        return RCODE[response.header.rcode]
    except Exception:
        return str(getattr(response.header, "rcode", "?"))


def _log_query(
    request,
    source: str,
    routed: bool,
    started_at: float,
    response=None,
    note: str = "",
    rcode_override: str | None = None,
):
    """Аккуратно записывает событие в QueryLog. Никогда не падает."""
    try:
        domain, qtype = _question_meta(request)
        latency_ms = int(max(0, (time.monotonic() - started_at) * 1000))
        entry = QueryLogEntry(
            timestamp=time.time(),
            domain=domain,
            qtype=qtype,
            source=source,
            routed=bool(routed),
            rcode=rcode_override or _rcode_name(response),
            latency_ms=latency_ms,
            answers=_answers_preview(response),
            note=note or "",
        )
        get_query_log().add(entry)
    except Exception as exc:
        log.debug("query log add failed: %s", exc)


# Потолок одновременных фоновых обновлений кэша (P2-1). Ответ пользователю уже
# отдан из кэша, обновление — дело десятое; но поток на каждый домен плодить
# нельзя: открыл человек страницу с сотней хостов, и вот уже сотня потоков
# одновременно ломится в апстрим. Лишние обновления пропускаем — следующий
# запрос к тому же домену повторит попытку.
_MAX_BG_REFRESHES = 8


class UmbraNetResolver(BaseResolver):
    def __init__(self, config_ref: list, cache: DNSCache, process_tracker=None):
        self._cfg_ref = config_ref
        self.cache = cache
        self.process_tracker = process_tracker
        # Пул фоновых refresh-задач (для optimistic cache).
        # Намеренно daemon=True — поток не помешает выходу программы.
        # Потолок одновременных обновлений (P2-1): см. _MAX_BG_REFRESHES.
        self._refresh_slots = threading.BoundedSemaphore(_MAX_BG_REFRESHES)

    @property
    def config(self):
        return self._cfg_ref[0]

    # ── Bogus IP detection ───────────────────────────────────────────────────
    def _get_bogus_index(self):
        """Лениво пересобирает индекс bogus IP при изменении конфига.

        Чтобы не парсить список IP на каждый запрос, кэшируем результат
        и сравниваем по id(config) — config_ref у нас в reload_config
        полностью заменяется новым dict.

        Использует build_bogus_index_with_cache: builtin + конфиг + диск-кэш
        (результат последнего успешного обновления BogusUpdater'а).
        """
        cfg = self.config
        cached = getattr(self, "_bogus_cache", None)
        if cached is not None and cached[0] is cfg:
            return cached[1], cached[2]
        ips, subnets = build_bogus_index_with_cache(cfg, _CORE_DIR)
        self._bogus_cache = (cfg, ips, subnets)
        return ips, subnets

    def invalidate_bogus_cache(self) -> None:
        """Сбрасывает bogus-кэш резолвера.

        Вызывается BogusUpdater'ом после успешного обновления —
        следующий DNS-запрос перестроит индекс с новым диск-кэшем.
        """
        self._bogus_cache = None
        log.info("Bogus-кэш сброшен — будет перестроен при следующем запросе")

    def _check_bogus(self, response):
        if response is None:
            return False, None
        if not _to_bool(self.config.get("bogus_detection_enabled", True), True):
            return False, None
        ips, subnets = self._get_bogus_index()
        if not ips and not subnets:
            return False, None
        return response_contains_bogus(response, ips, subnets)

    # ── Optimistic cache helpers ─────────────────────────────────────────────
    def _stale_ttl(self) -> int:
        """Окно, в течение которого мы готовы отдать просроченный ответ."""
        if not _to_bool(self.config.get("optimistic_cache_enabled", True), True):
            return 0
        try:
            return max(0, int(self.config.get("stale_cache_ttl", 3600)))
        except Exception:
            return 3600

    def _spawn_background_refresh(self, request, routed, resolver_callable, cache_kwargs):
        """Запускает фоновое обновление кэша, если ещё никто не обновляет.

        resolver_callable() должен вернуть свежий response (или None).
        cache_kwargs — параметры, с которыми сохранять результат в кэш.
        """
        if not self.cache.mark_refreshing(request, routed):
            return  # уже обновляется кем-то другим

        domain, qtype = _question_meta(request)

        if not self._refresh_slots.acquire(blocking=False):
            # Все места заняты (P2-1): под нагрузкой «поток на домен» — это сотни
            # потоков разом в апстрим. Ответ пользователю уже отдан, так что
            # спокойно пропускаем: следующий запрос к этому домену попробует снова.
            self.cache.unmark_refreshing(request, routed)
            log.debug(
                f"[bg-refresh] {domain} {qtype}: заняты все {_MAX_BG_REFRESHES} мест, пропускаем"
            )
            return

        def _worker():
            try:
                fresh = resolver_callable()
                if fresh is None:
                    log.debug(f"[bg-refresh] {domain} {qtype}: upstream вернул None")
                    return
                # Перед тем как закешировать обновлённый ответ,
                # на всякий случай проверим его на bogus — вдруг провайдер
                # успел подменить ответ с момента прошлого refresh.
                is_bogus, ip_str = self._check_bogus(fresh)
                if is_bogus:
                    log.warning(
                        f"[bg-refresh] {domain} {qtype}: получили bogus IP {ip_str}, "
                        f"не обновляем кэш"
                    )
                    return
                self.cache.set(request, routed, fresh, **cache_kwargs)
                log.debug(f"[bg-refresh] {domain} {qtype}: кэш обновлён")
            except Exception as exc:
                log.warning(f"[bg-refresh] {domain} {qtype}: ошибка обновления: {exc}")
            finally:
                self.cache.unmark_refreshing(request, routed)
                self._refresh_slots.release()

        threading.Thread(target=_worker, daemon=True, name=f"refresh:{domain}").start()

    # ── Основная resolve()-логика ────────────────────────────────────────────
    def resolve(self, request, handler):
        domain, qtype = _question_meta(request)
        started = time.monotonic()

        # Пользовательский DNS-блоклист. Проверяем до маршрутизации, чтобы
        # ручная блокировка всегда имела приоритет над routed/route_all.
        if (not is_domain_allowed(domain, self.config)) and is_domain_blocked(domain, self.config):
            log.info(f"[blocked] {domain} {qtype}: пользовательская блокировка → NXDOMAIN")
            _log_query(request, SOURCE_BLOCKED, False, started,
                       rcode_override="NXDOMAIN", note="пользовательская блокировка")
            return _nxdomain_reply(request)

        routed = is_domain_routed(domain, self.config, self.process_tracker)

        if routed:
            return self._resolve_routed(request, domain, qtype, routed)
        return self._resolve_system(request, domain, qtype, routed)

    def _resolve_routed(self, request, domain, qtype, routed):
        started = time.monotonic()
        routed_cache_enabled = _to_bool(self.config.get("routed_cache_enabled", True), True)
        routed_cache_ttl = int(self.config.get("routed_cache_ttl", 5) or 0)
        routed_reply_ttl = int(self.config.get("routed_reply_ttl", 1) or 0)
        stale_ttl = self._stale_ttl()

        cache_kwargs = {"ttl_override": routed_cache_ttl, "stale_ttl": stale_ttl}

        # ── IPv6 приоритет / трюк обхода ──
        if qtype == "A" and _to_bool(self.config.get("ipv6_priority_enabled", False), False):
            # Проверяем, есть ли у домена IPv6 адреса через secure DNS (AAAA)
            aaaa_req = copy.deepcopy(request)
            aaaa_req.q.qtype = 28  # QTYPE AAAA is 28
            aaaa_resp = resolve_via_xbox(aaaa_req, self.config, record=False)
            if aaaa_resp is not None and len(aaaa_resp.rr) > 0:
                # Нашли IPv6 адреса! Возвращаем пустой ответ NOERROR
                # Это заставит клиента использовать только IPv6-адреса,
                # которые идут в обход IPv4 DPI-блокировок провайдера!
                reply = request.reply()
                log.info(f"[{domain} IPv6-приоритет] Скрыли IPv4-адрес, форсируем IPv6")
                _log_query(request, SOURCE_ROUTED, routed, started, response=reply, note="IPv6 приоритет (IPv4 скрыт)")
                return _proxy_reply_from_upstream(request, reply)

        if routed_cache_enabled and routed_cache_ttl > 0:
            cached, state = self.cache.get_with_state(request, routed)
            if state == "fresh" and cached is not None:
                cached = _cap_response_ttl(cached, routed_reply_ttl)
                log.debug(f"[cache fresh] {domain} {qtype} (xbox)")
                _log_query(request, SOURCE_CACHE_FRESH, routed, started, response=cached)
                return _proxy_reply_from_upstream(request, cached)
            if state == "stale" and cached is not None:
                log.debug(f"[cache stale] {domain} {qtype} (xbox) → отдаём + bg-refresh")
                cached = _cap_response_ttl(cached, routed_reply_ttl)
                self._spawn_background_refresh(
                    request, routed,
                    resolver_callable=lambda: resolve_via_xbox(request, self.config),
                    cache_kwargs=cache_kwargs,
                )
                _log_query(request, SOURCE_CACHE_STALE, routed, started, response=cached)
                return _proxy_reply_from_upstream(request, cached)

        upstream = resolve_via_xbox(request, self.config)
        if upstream is not None:
            # Для routed домена доверяем xbox-dns — bogus-проверку не делаем
            # (она нужна именно против подмены провайдером).
            if routed_cache_enabled and routed_cache_ttl > 0:
                self.cache.set(request, routed, upstream, **cache_kwargs)
            upstream = _cap_response_ttl(upstream, routed_reply_ttl)
            _log_query(request, SOURCE_ROUTED, routed, started, response=upstream)
            return _proxy_reply_from_upstream(request, upstream)

        log.error(
            f"xbox-dns не ответил для маршрутизируемого домена {domain} {qtype}; "
            f"возвращаем SERVFAIL"
        )
        _log_query(request, SOURCE_SERVFAIL, routed, started,
                   rcode_override="SERVFAIL", note="xbox-dns не ответил")
        return _servfail_reply(request)

    def _resolve_system(self, request, domain, qtype, routed):
        started = time.monotonic()
        stale_ttl = self._stale_ttl()
        cache_kwargs = {"stale_ttl": stale_ttl}

        # 1) Optimistic cache: проверяем кэш
        cached, state = self.cache.get_with_state(request, routed)
        if state == "fresh" and cached is not None:
            log.debug(f"[cache fresh] {domain} {qtype} (system)")
            _log_query(request, SOURCE_CACHE_FRESH, routed, started, response=cached)
            return _proxy_reply_from_upstream(request, cached)
        if state == "stale" and cached is not None:
            log.debug(f"[cache stale] {domain} {qtype} (system) → отдаём + bg-refresh")
            self._spawn_background_refresh(
                request, routed,
                resolver_callable=lambda: self._system_with_bogus_check(request, domain, qtype),
                cache_kwargs=cache_kwargs,
            )
            _log_query(request, SOURCE_CACHE_STALE, routed, started, response=cached)
            return _proxy_reply_from_upstream(request, cached)

        # 2) Cache miss — идём в апстрим (с bogus-проверкой и fallback на DoH)
        upstream = self._system_with_bogus_check(request, domain, qtype)

        if upstream is None:
            log.error(
                f"Системный DNS не ответил для {domain} {qtype}; возвращаем SERVFAIL"
            )
            _log_query(request, SOURCE_SERVFAIL, routed, started,
                       rcode_override="SERVFAIL", note="system DNS не ответил")
            return _servfail_reply(request)

        # Особый случай: апстрим вернул "bogus" с обоих сторон → NXDOMAIN-маркер
        if getattr(upstream, "_umbranet_bogus_nxdomain", False):
            log.warning(
                f"[bogus] {domain} {qtype}: оба upstream вернули фейковые IP "
                f"(провайдерская подмена) → отвечаем NXDOMAIN"
            )
            bogus_ip = getattr(upstream, "_umbranet_bogus_ip", "")
            _log_query(request, SOURCE_BOGUS_NX, routed, started,
                       rcode_override="NXDOMAIN",
                       note=f"провайдерская заглушка{(' ' + bogus_ip) if bogus_ip else ''}")
            return _nxdomain_reply(request)

        self.cache.set(request, routed, upstream, **cache_kwargs)
        _log_query(request, SOURCE_SYSTEM, routed, started, response=upstream)
        # Считаем такие ответы отдельно: для доменов вне списка маршрутизации
        # это штатный путь, но пользователь должен видеть, какая доля запросов
        # ушла мимо профилей (и, значит, шифрования).
        try:
            get_fallback_state().record_system_answer(domain=domain)
        except Exception as exc:
            log.debug("fallback state (системный DNS) не записан: %s", exc)
        return _proxy_reply_from_upstream(request, upstream)

    def _system_with_bogus_check(self, request, domain, qtype):
        """Резолв через system DNS, но если ответ содержит bogus IP —
        пробуем переспросить через DoH/xbox-dns. Если и там фейк — возвращаем
        специальный маркер, чтобы вышестоящий код отдал клиенту NXDOMAIN.
        """
        upstream = resolve_via_system(request, self.config)
        is_bogus, ip_str = self._check_bogus(upstream)
        if not is_bogus:
            return upstream

        log.warning(
            f"[bogus] {domain} {qtype}: системный DNS вернул {ip_str} "
            f"(похоже на провайдерскую заглушку), пробуем DoH/xbox-dns"
        )
        # Переспрашиваем через xbox-dns (оно само решит UDP vs DoH по режиму).
        retry = resolve_via_xbox(request, self.config)
        is_bogus2, ip_str2 = self._check_bogus(retry)
        if retry is not None and not is_bogus2:
            log.info(
                f"[bogus] {domain} {qtype}: через xbox-dns получили реальный ответ, "
                f"используем его"
            )
            return retry

        # Совсем плохо: оба варианта вернули фейк (или второй вообще не ответил
        # и мы остались с фейком от первого). Помечаем для NXDOMAIN.
        marker_response = retry if retry is not None else upstream
        try:
            marker_response._umbranet_bogus_nxdomain = True
            marker_response._umbranet_bogus_ip = ip_str2 or ip_str or ""
        except Exception as exc:
            log_recoverable(log, 'Не удалось прикрепить метку bogus-IP к DNS-ответу', exc, level=logging.DEBUG)
        return marker_response


class UmbraNetDNS:
    def __init__(self):
        self.config = load_config()
        self._subscriptions_lock = threading.Lock()
        # Separate the long download lock from short state/commit transitions.
        # Reload and UI source edits must use the same lock as cache publication.
        self._subscription_state_lock = threading.RLock()
        self._subscriptions_generation = 0
        self._subscription_cache_valid = True
        self._cfg_ref = [self.config]
        self.cache = DNSCache()
        # DPI Engine (Обход блокировок на уровне пакетов)
        self.winws = get_winws_engine()
        # Трекер «домен → процесс» для per-app маршрутизации.
        # На не-Windows / без журнала он тихо остаётся пустым.
        try:
            from process_dns_tracker import DnsProcessTracker
            self.process_tracker = DnsProcessTracker()
        except Exception as exc:
            log.warning(f"DnsProcessTracker недоступен: {exc}")
            self.process_tracker = None
        # Фоновый обновлятель bogus-IP (callback ссылается на resolver,
        # который создаётся при start() — поэтому передаём обёртку)
        try:
            from bogus_updater import BogusUpdater  # type: ignore
            self.bogus_updater = BogusUpdater(
                config_dir=_CORE_DIR,
                on_update=self._on_bogus_update,
            )
            self.bogus_updater.add_task("подписки", self._update_subscriptions)
            self.bogus_updater.add_task("доменные списки стратегий", self._update_strategy_domains)
        except Exception as exc:
            log.warning("BogusUpdater недоступен: %s", exc)
            self.bogus_updater = None
        self.last_start_error = ""  # понятная причина неудачного старта (для GUI)
        self.server4 = None       # IPv4 DNS сервер (UDP)
        self.server4_tcp = None   # IPv4 DNS сервер (TCP)
        self.server6 = None       # IPv6 DNS сервер (UDP)
        self.server6_tcp = None   # IPv6 DNS сервер (TCP)
        self.running = False
        self._resolver: UmbraNetResolver | None = None  # ссылка для on_update
        self.load_subscribed_domains()

    def load_subscribed_domains(self) -> None:
        """Load cache only for an enabled, still-valid subscription source set."""
        with self._subscription_state_lock:
            if (not self.config.get("routed_subscriptions") or
                    not self._subscription_cache_valid):
                self.config["subscribed_domains_set"] = set()
                return
            self._load_subscribed_domains_locked()

    def _load_subscribed_domains_locked(self) -> None:
        path = os.path.join(_CORE_DIR, "subscribed_domains_cache.json")
        if not os.path.exists(path):
            self.config["subscribed_domains_set"] = set()
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("кэш подписок должен быть JSON-массивом")
            domains = set()
            for item in data:
                value = str(item).strip().lower().rstrip(".")
                # Сохраняем только похожие на домены значения. Это защищает
                # hostlist/маршрутизацию от случайно повреждённого кэша.
                if value and "." in value and " " not in value and "/" not in value:
                    domains.add(value)
            self.config["subscribed_domains_set"] = domains
            log.info("Загружено %d доменов из закэшированных подписок", len(domains))
        except Exception as exc:
            log.warning("Не удалось загрузить кэш подписок: %s", exc)
            self.config["subscribed_domains_set"] = set()

    def _invalidate_subscription_cache_locked(self) -> bool:
        """Discard an aggregate that may contain domains from removed sources.

        Even if unlink fails, this session must not reload the obsolete file.
        A successful refresh makes the cache valid again.
        """
        self._subscription_cache_valid = False
        self.config["subscribed_domains_set"] = set()
        self.cache.clear()
        path = os.path.join(_CORE_DIR, "subscribed_domains_cache.json")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log_recoverable(log, "Не удалось удалить устаревший кэш подписок", exc,
                            level=logging.WARNING)
            return False
        return True

    def change_subscription(self, url: str, *, remove: bool = False) -> bool:
        """UI source edit, serialized with reload and final cache publication."""
        with self._subscription_state_lock:
            urls = list(self.config.get("routed_subscriptions", []) or [])
            if (url in urls) != remove:
                return False
            if remove:
                urls.remove(url)
            else:
                urls.append(url)
            self.config["routed_subscriptions"] = urls
            self._subscriptions_generation += 1
            if remove:
                self._invalidate_subscription_cache_locked()
            save_config(self.config)
            return True

    def start_background_updates(self) -> None:
        """Shared startup path for GUI and headless DNS; start is idempotent."""
        if self.bogus_updater is not None:
            self.bogus_updater.start()

    @staticmethod
    def _update_strategy_domains() -> bool:
        from core.dpi.domain_updater import update_all_strategies
        from core.dpi.strategy_manager import get_strategy_manager
        return update_all_strategies(get_strategy_manager().strategies_dir)

    def _update_subscriptions(self) -> bool:
        """Synchronous scheduler task; never overlaps a manual download."""
        if not self._subscriptions_lock.acquire(blocking=False):
            return False
        try:
            return self._fetch_subscriptions()
        finally:
            self._subscriptions_lock.release()

    def update_subscriptions_async(self, on_done=None) -> None:
        """Manual refresh; callback(success, count) runs in the worker thread.

        A second request reports False without starting a duplicate download.
        """
        acquired = self._subscriptions_lock.acquire(blocking=False)

        def _worker():
            ok = False
            try:
                if acquired:
                    ok = self._fetch_subscriptions()
            except Exception as exc:
                log_recoverable(log, "Не удалось обновить подписки", exc,
                                level=logging.WARNING)
            finally:
                if acquired:
                    self._subscriptions_lock.release()
            if on_done:
                try:
                    on_done(ok, len(self.config.get("subscribed_domains_set", set()) or []))
                except Exception as exc:
                    log_recoverable(log, "Не удалось сообщить результат обновления подписок", exc)

        try:
            threading.Thread(target=_worker, daemon=True, name="UmbraNet-Subscriptions").start()
        except Exception:
            if acquired:
                self._subscriptions_lock.release()
            raise

    def _fetch_subscriptions(self) -> bool:
        """Bounded fetch; keep the previous cache if any subscription fails."""
        with self._subscription_state_lock:
            config_snapshot = self.config
            generation = self._subscriptions_generation
            urls = list(config_snapshot.get("routed_subscriptions", []) or [])
        original_urls = list(urls)
        # H2: лимит количества подписок
        MAX_URLS = 10
        MAX_CONTENT_BYTES = 5 * 1024 * 1024
        MAX_PER_SUB = 50_000
        MAX_TOTAL = 100_000
        MAX_LINES = 200_000
        if len(urls) > MAX_URLS:
            log.warning("Слишком много подписок (%d), берём первые %d", len(urls), MAX_URLS)
            urls = urls[:MAX_URLS]
        if not urls:
            with self._subscription_state_lock:
                if not self._subscription_snapshot_current(config_snapshot, generation, original_urls):
                    return False
                return self._invalidate_subscription_cache_locked()

        import urllib.request
        compiled_domains = set()
        fetch_failed = False
        successful_fetches = 0

        for index, url in enumerate(urls, 1):
            log.info("Обновление подписки №%d", index)
            if len(compiled_domains) >= MAX_TOTAL:
                log.warning("Достигнут лимит %d доменов, остальные подписки пропущены", MAX_TOTAL)
                break
            try:
                from urllib.parse import urlsplit
                parsed_url = urlsplit(str(url).strip())
                if parsed_url.scheme.lower() not in ("http", "https") or not parsed_url.netloc:
                    log.warning("Пропущен небезопасный URL подписки №%d", index)
                    fetch_failed = True
                    continue
                req = urllib.request.Request(
                    url, headers={"User-Agent": "UmbraNet/1.0 Subscription-Updater"}
                )
                # H2: таймаут 10s, лимит размера
                with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 - http(s) allowlist above
                    # проверяем Content-Length заголовок, если есть
                    clen = resp.headers.get("Content-Length")
                    if clen is not None:
                        try:
                            if int(clen) > MAX_CONTENT_BYTES:
                                log.warning("Подписка №%d слишком большая, пропускаем", index)
                                fetch_failed = True
                                continue
                        except Exception as exc:
                            log_recoverable(log, 'Некорректный Content-Length подписки; проверяем размер тела', exc, level=logging.DEBUG)
                    raw = resp.read(MAX_CONTENT_BYTES + 1)
                    if len(raw) > MAX_CONTENT_BYTES:
                        log.warning("Подписка №%d превышает лимит %d байт, пропускаем", index, MAX_CONTENT_BYTES)
                        fetch_failed = True
                        continue
                    content = raw.decode("utf-8", errors="ignore")
                successful_fetches += 1

                per_sub_count = 0
                valid_for_sub = False
                for idx, line in enumerate(content.splitlines()):
                    if idx >= MAX_LINES:
                        log.warning("Подписка №%d: превышен лимит строк %d, обрезаем", index, MAX_LINES)
                        break
                    if per_sub_count >= MAX_PER_SUB:
                        log.warning("Подписка №%d: превышен лимит %d доменов, обрезаем", index, MAX_PER_SUB)
                        break
                    if len(compiled_domains) >= MAX_TOTAL:
                        break
                    # Keep http(s):// intact; // comments need whitespace.
                    line = line.split("#", 1)[0].strip()
                    if not line or line.startswith("//"):
                        continue
                    line = re.split(r"\s+//", line, maxsplit=1)[0].strip()

                    # 2) Парсим hosts-формат (например, "127.0.0.1 domain.com" или "0.0.0.0 domain.com")
                    parts = line.split()
                    if len(parts) >= 2:
                        first = parts[0]
                        # Если первая часть похожа на IP-адрес (содержит точки/двоеточия, но не буквы)
                        if any(c in first for c in (".", ":")) and not any(c.isalpha() for c in first):
                            val = parts[1]
                        else:
                            val = parts[0]
                    elif len(parts) == 1:
                        val = parts[0]
                    else:
                        continue

                    # 3) Очищаем домен от схем и путей
                    val = val.lower()
                    for pre in ("https://", "http://", "www."):
                        val = val.removeprefix(pre)
                    val = val.split("/")[0].strip()

                    # Reject HTML/error pages rather than replacing a good cache.
                    try:
                        val = val.rstrip(".").encode("idna").decode("ascii")
                    except UnicodeError:
                        continue
                    if (len(val) <= 253 and "." in val and
                            all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                for label in val.split("."))):
                        valid_for_sub = True
                        if val not in compiled_domains:
                            compiled_domains.add(val)
                            per_sub_count += 1
                if not valid_for_sub:
                    fetch_failed = True
                    log.warning("Подписка №%d не содержит допустимых доменов; кэш сохранён", index)
            except Exception as exc:
                fetch_failed = True
                log.warning("Ошибка при загрузке подписки №%d (%s)", index, type(exc).__name__)

        if fetch_failed or successful_fetches != len(urls):
            # Не затираем последний рабочий cache при кратковременном
            # падении сети: подписки должны деградировать в offline-режим.
            log.warning("Кэш подписок сохранён: обновление не завершилось для всех URL")
            return False

        path = os.path.join(_CORE_DIR, "subscribed_domains_cache.json")
        temp_path = f"{path}.{threading.get_ident()}.tmp"
        try:
            # Network and serialization run WITHOUT the state lock: editing
            # subscriptions/reloading config must not wait for a download.
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(list(compiled_domains), f, ensure_ascii=False, indent=2)
            with self._subscription_state_lock:
                if not self._subscription_snapshot_current(config_snapshot, generation, original_urls):
                    log.info("Подписки изменены во время обновления; результат отброшен")
                    return False
                if os.path.exists(path):
                    try:
                        import shutil
                        shutil.copy2(path, f"{path}.bak")
                    except Exception as exc:
                        log_recoverable(log, 'Не удалось создать резервную копию кэша подписок', exc,
                                        level=logging.WARNING)
                # No reload or UI source edit can interleave these two writes.
                os.replace(temp_path, path)
                self.config["subscribed_domains_set"] = compiled_domains
                self._subscription_cache_valid = True
                self.cache.clear()
            log.info("Подписки успешно обновлены. Итого доменов в кэше: %d", len(compiled_domains))
            return True
        except Exception as exc:
            log.error("Не удалось записать кэш подписок: %s", exc)
            return False
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError as exc:
                log_recoverable(log, 'Не удалось удалить временный файл подписок', exc, level=logging.DEBUG)


    def _on_bogus_update(self, ips: list, subnets: list) -> None:
        """Callback от BogusUpdater: сбрасываем bogus-кэш резолвера."""
        if self._resolver is not None:
            try:
                self._resolver.invalidate_bogus_cache()
            except Exception as exc:
                log.debug("_on_bogus_update: ошибка сброса кэша: %s", exc)

    def _subscription_snapshot_current(self, config, generation, urls) -> bool:
        """Caller holds the state lock; generation also detects remove/add ABA."""
        return (self.config is config and self._subscriptions_generation == generation and
                list(self.config.get("routed_subscriptions", []) or []) == urls)

    def reload_config(self):
        with self._subscription_state_lock:
            config = load_config()
            self._subscriptions_generation += 1
            old_sources = set(self.config.get("routed_subscriptions", []) or [])
            new_sources = set(config.get("routed_subscriptions", []) or [])
            if not old_sources.issubset(new_sources):
                self._invalidate_subscription_cache_locked()
            self.config = config
            self.load_subscribed_domains()
            self._cfg_ref[0] = self.config
            self.cache.clear()
        log.info("Конфигурация перезагружена; DNS-кэш очищен")

    def start(self):
        if self.running:
            log.info("Сервер уже запущен")
            return True

        # Новая сессия работы — статистика ответов начинается с нуля, иначе в
        # карточке «Сейчас отвечает» смешались бы прошлый и текущий запуски.
        try:
            get_fallback_state().reset()
        except Exception as exc:
            log.debug("Сброс статистики ответов не удался: %s", exc)

        # ── Предстартовая проверка: права + свободен ли порт 53 ───────────────
        ok, problems, warnings = preflight_check(self.config)
        for w in warnings:
            log.warning(f"Предупреждение перед запуском: {w}")
        if not ok:
            self.last_start_error = "; ".join(problems)
            log.error(f"Не удалось запустить: {self.last_start_error}")
            return False
        self.last_start_error = ""

        # Запускаем трекер процессов (если есть): он начнёт наполнять таблицу
        # домен→процесс из журнала DNS-Client. Маршрутизация по процессам
        # «оживает» по мере прихода событий.
        if self.process_tracker is not None:
            try:
                self.process_tracker.start()
            except Exception as exc:
                log.warning(f"Не удалось запустить DnsProcessTracker: {exc}")

        # Запускаем фоновую чистку кэша (защита от роста при долгой работе).
        try:
            self.cache.start_janitor()
        except Exception as exc:
            log.debug(f"Не удалось запустить cache janitor: {exc}")

        # Запуск DPI-движка, если он включен в конфиге
        dpi_mode = self.config.get("dpi_mode", "off")
        if dpi_mode != "off":
            # Прежний выключатель use_winws здесь больше не спрашивается:
            # обход идёт только через WinWS, а сам ключ (от движка, которого
            # больше нет) вычищается миграцией 0→1 в core/config_utils.py.
            strategy_id = self.config.get("dpi_strategy", "uz1")
            try:
                from core.dpi.strategy_manager import get_strategy_manager
                manager = get_strategy_manager()
                # Единый источник целей DPI — главное меню / routed_domains
                # + закэшированные подписки. Стратегия задаёт только метод.
                routed_targets = list(self.config.get("routed_domains", []) or [])
                routed_targets += list(self.config.get("subscribed_domains_set", set()) or [])
                args = manager.get_args(
                    strategy_id,
                    routed_domains=routed_targets,
                    require_hostlist=True,
                )
                if args:
                    self.winws.start(args)
                else:
                    log.warning(
                        "WinWS не запущен: %s",
                        manager.last_error or f"стратегия {strategy_id} не готова",
                    )
            except Exception as exc:
                log.error(f"Ошибка запуска WinWS: {exc}")

        resolver = UmbraNetResolver(self._cfg_ref, self.cache, self.process_tracker)
        self._resolver = resolver   # нужен для on_update callback BogusUpdater'а
        port = self.config["listen_port"]
        started_any = False

        # ── IPv4 серверы (127.0.0.1:53) ──────────────────────────────────────
        try:
            self.server4 = DNSServer(
                resolver,
                port=port,
                address=self.config["listen_host"],
                logger=DNSLogger(prefix=False)
            )
            threading.Thread(target=self.server4.start, daemon=True).start()
            started_any = True
            log.info(f"IPv4 DNS UDP запущен на {self.config['listen_host']}:{port}")
        except Exception as e:
            log.error(f"Не удалось запустить IPv4 DNS UDP: {e}")
            self.server4 = None

        try:
            self.server4_tcp = DNSServer(
                resolver,
                port=port,
                address=self.config["listen_host"],
                logger=DNSLogger(prefix=False),
                tcp=True,
            )
            threading.Thread(target=self.server4_tcp.start, daemon=True).start()
            started_any = True
            log.info(f"IPv4 DNS TCP запущен на {self.config['listen_host']}:{port}")
        except Exception as e:
            log.warning(f"Не удалось запустить IPv4 DNS TCP: {e}")
            self.server4_tcp = None

        # ── IPv6 серверы (::1:53) ────────────────────────────────────────────
        if _to_bool(self.config.get("enable_ipv6", True), True):
            try:
                self.server6 = DNSServer(
                    resolver,
                    port=port,
                    address=self.config.get("listen_host6", "::1"),
                    logger=DNSLogger(prefix=False)
                )
                threading.Thread(target=self.server6.start, daemon=True).start()
                started_any = True
                log.info(f"IPv6 DNS UDP запущен на {self.config.get('listen_host6', '::1')}:{port}")
            except Exception as e:
                log.warning(f"Не удалось запустить IPv6 DNS UDP (возможно IPv6 отключён): {e}")
                self.server6 = None

            try:
                self.server6_tcp = DNSServer(
                    resolver,
                    port=port,
                    address=self.config.get("listen_host6", "::1"),
                    logger=DNSLogger(prefix=False),
                    tcp=True,
                )
                threading.Thread(target=self.server6_tcp.start, daemon=True).start()
                started_any = True
                log.info(f"IPv6 DNS TCP запущен на {self.config.get('listen_host6', '::1')}:{port}")
            except Exception as e:
                log.warning(f"Не удалось запустить IPv6 DNS TCP (возможно IPv6 отключён): {e}")
                self.server6_tcp = None

        if started_any:
            self.running = True
            # Запускаем фоновое обновление bogus-IP
            if self.bogus_updater is not None:
                try:
                    self.start_background_updates()
                except Exception as exc:
                    log.debug("Не удалось запустить BogusUpdater: %s", exc)
            mode = self.config.get("xbox_dns_mode", "udp")
            profile = get_active_dns_profile(self.config)
            if mode == "udp":
                log.info(
                    f"Активный профиль UDP: {profile.get('ipv4_primary')} / {profile.get('ipv4_secondary')}"
                )
                log.info(
                    f"Активный профиль IPv6: {profile.get('ipv6_primary')} / {profile.get('ipv6_secondary')}"
                )
            else:
                log.info(f"Активный профиль DoH: {profile.get('doh_url')}")
            log.info(f"Активный DNS-профиль: {profile.get('name')}")
            log.info("Локальный DNS слушает запросы клиентов по UDP и TCP")
            return True
        else:
            log.error("Не удалось запустить ни один DNS сервер")
            # Если DPI/WinWS уже успел стартовать, но DNS-серверы не поднялись,
            # обязательно откатываем запуск. Иначе winws.exe остаётся жить один
            # и держит WinDivert/файлы программы после неудачного старта.
            try:
                if hasattr(self, "winws") and self.winws:
                    self.winws.stop()
            except Exception as exc:
                log_recoverable(log, 'Не удалось остановить WinWS после неудачного старта DNS', exc, level=logging.ERROR)
            # До создания DNS-сокетов мы уже могли запустить фоновые helper'ы.
            # Не оставляем их жить после неудачного старта: иначе повторный
            # запуск создавал лишние потоки и stale-состояние трекера.
            try:
                if self.process_tracker is not None:
                    self.process_tracker.stop()
            except Exception as exc:
                log_recoverable(log, 'Не удалось остановить трекер процессов после неудачного старта DNS', exc, level=logging.WARNING)
            try:
                self.cache.stop_janitor()
            except Exception as exc:
                log_recoverable(log, 'Не удалось остановить очистку кэша после неудачного старта DNS', exc, level=logging.WARNING)
            self._resolver = None
            return False

    def stop(self):
        self.running = False
        
        for srv_name in ("server4", "server4_tcp", "server6", "server6_tcp"):
            server = getattr(self, srv_name, None)
            if server:
                try:
                    server.stop()
                    # Принудительно закрываем сокет, чтобы порт освободился мгновенно
                    if hasattr(server, "server") and server.server is not None:
                        server.server.server_close()
                except Exception as exc:
                    log_recoverable(log, 'Не удалось закрыть DNS-сервер; порт может остаться занят', exc, level=logging.ERROR)
                setattr(self, srv_name, None)
        
        if hasattr(self, "winws") and self.winws:
            self.winws.stop()

        # Останавливаем фоновый обновлятель bogus-IP
        if self.bogus_updater is not None:
            try:
                self.bogus_updater.stop()
            except Exception as exc:
                log_recoverable(log, 'Не удалось остановить фоновые обновления', exc, level=logging.WARNING)

        if self.process_tracker is not None:
            try:
                self.process_tracker.stop()
            except Exception as exc:
                log_recoverable(log, 'Не удалось остановить трекер DNS-процессов', exc, level=logging.WARNING)
        try:
            self.cache.stop_janitor()
        except Exception as exc:
            log_recoverable(log, 'Не удалось остановить очистку DNS-кэша', exc, level=logging.WARNING)
        self.running = False
        self.server4 = None
        self.server4_tcp = None
        self.server6 = None
        self.server6_tcp = None
        self._resolver = None
        log.info("DNS-серверы и DPI-движок остановлены")

    def add_domain(self, domain: str):
        domain = domain.strip().lower()
        if domain and domain not in self.config["routed_domains"]:
            self.config["routed_domains"].append(domain)
            save_config(self.config)
            self.cache.clear()
            log.info(f"Домен добавлен: {domain}; DNS-кэш очищен")

    def remove_domain(self, domain: str):
        if domain in self.config["routed_domains"]:
            self.config["routed_domains"].remove(domain)
            save_config(self.config)
            self.cache.clear()
            log.info(f"Домен удалён: {domain}; DNS-кэш очищен")

    def add_process(self, process: str):
        process = process.strip()
        if process and process not in self.config["routed_processes"]:
            self.config["routed_processes"].append(process)
            save_config(self.config)
            # Чистим кэш: ранее «несмаршрутизированные» домены этого процесса
            # должны переоцениться с учётом нового правила.
            self.cache.clear()
            log.info(f"Процесс добавлен: {process}; DNS-кэш очищен")

    def remove_process(self, process: str):
        if process in self.config["routed_processes"]:
            self.config["routed_processes"].remove(process)
            save_config(self.config)
            self.cache.clear()
            log.info(f"Процесс удалён: {process}; DNS-кэш очищен")

    def set_dpi_mode(self, mode: str):
        """
        Устанавливает режим работы DPI.

        mode:
          'off'      — DPI выключен (только DNS; синий режим)
          'combo'    — split + fake (комбо; чёрный режим)
          'dpi_only' — split + fake + disorder (только DPI; красный режим)
        """
        log.info(f"Переключение режима DPI → {mode}")
        self.config["dpi_mode"] = mode
        save_config(self.config)

        # Выбор режима только сохраняет настройки в конфиг.
        # Поскольку интерфейс теперь сам нажимает "Стоп" при смене режима,
        # нам больше не нужно пытаться перезапускать winws.exe на лету,
        # что вызывало зависания UI (main thread block) и race conditions.
    def switch_mode(self, ui_mode: str) -> tuple[bool, str]:
        """
        Атомарное переключение трёх UI-режимов UmbraNet:

          'dns_only' (синий)  — DNS-сервер работает, DPI выключен.
          'combo'    (чёрный) — DNS-сервер работает, DPI в режиме combo.
          'dpi_only' (красный)— DNS-сервер работает (нужен для IP-резолва),
                                DPI в агрессивном режиме.

        Все переключения выполняются атомарно: сначала гарантируется
        корректное состояние DNS, затем меняется DPI.
        Возвращает (ok: bool, error_message: str).

        C1 guard: без целей hostlist не должен уходить в эфир.
        """
        VALID = ("dns_only", "combo", "dpi_only")
        if ui_mode not in VALID:
            return False, f"Неизвестный режим '{ui_mode}'. Допустимые: {VALID}"

        log.info(f"switch_mode → {ui_mode}")

        # ── C1: переключение разрешено всегда — блокируем только Старт ──
        if ui_mode in ("combo", "dpi_only"):
            try:
                routed = list(self.config.get("routed_domains", []) or [])
                routed += list(self.config.get("subscribed_domains_set", set()) or [])
                uniq = set()
                for x in routed:
                    s = str(x).strip().lower().rstrip(".")
                    if not s or "." not in s or " " in s or "/" in s:
                        continue
                    uniq.add(s)
                if not uniq:
                    log.info("switch_mode(%s): hostlist пуст — разрешаем переключение, Старт будет заблокирован", ui_mode)
            except Exception as exc:
                log.debug("hostlist guard check failed: %s", exc)

        try:
            # Выбор режима — это настройка, а не команда «Старт».
            # Если UmbraNet остановлен, просто сохраняем dpi_mode. При следующем
            # запуске start() применит его. Если уже запущен — меняем DPI на ходу.
            if ui_mode == "dns_only":
                self.set_dpi_mode("off")
            elif ui_mode == "combo":
                self.set_dpi_mode("combo")
            elif ui_mode == "dpi_only":
                self.set_dpi_mode("dpi_only")

            return True, ""
        except Exception as exc:
            log.error(f"Ошибка switch_mode({ui_mode}): {exc}")
            return False, str(exc)

    # ── Единый понятный статус для пользователя ──────────────────────────────
    def access_status(self) -> dict:
        """Возвращает простой статус доступа к ИИ-сервисам для UI.

        {
          "state": "up" | "down" | "unknown",
          "text":  человекочитаемая строка,
          "active_provider": имя активного провайдера,
          "providers": [{"name","status"}...],
        }
        Опирается на provider_health, который наполняется по факту запросов.
        """
        health = get_provider_health()
        providers = get_failover_providers(self.config)
        provider_ids = [p.get("id") for p in providers]
        state = health.overall_status(provider_ids)

        active = providers[0] if providers else {"name": "—"}
        snap = health.snapshot()
        prov_list = [
            {"name": p.get("name"), "status": snap.get(p.get("id"), {}).get("status", "unknown")}
            for p in providers
        ]

        if state == "up":
            # уточняем: работаем через основной или через запасной?
            primary_ok = snap.get(provider_ids[0], {}).get("status") == "up" if provider_ids else False
            if primary_ok:
                text = "🟢 Работает"
            else:
                # основной лёг, но запасной выручает
                working = next((p["name"] for p in prov_list if p["status"] == "up"), None)
                text = f"🟢 Работает (через запасной: {working})" if working else "🟢 Работает"
        elif state == "down":
            text = "🔴 Не работает — все провайдеры недоступны"
        else:
            text = "⚪ Ещё не проверялось — откройте ИИ-сервис или нажмите «Проверить»"

        return {
            "state": state,
            "text": text,
            "active_provider": active.get("name"),
            "providers": prov_list,
        }

    def check_now(self, test_domain: str = "chatgpt.com") -> dict:
        """Активная проверка для кнопки «Проверить»: реально резолвит тестовый
        домен через провайдеров (с failover) и обновляет статус здоровья.

        Возвращает тот же словарь, что access_status(), плюс "ok": bool.
        """
        from dnslib import DNSRecord
        try:
            request = DNSRecord.question(test_domain, "A")
            response = resolve_via_xbox(request, self.config)
            ok = response is not None and len(getattr(response, "rr", [])) > 0
        except Exception as exc:
            log.warning(f"check_now: ошибка проверки {test_domain}: {exc}")
            ok = False
        status = self.access_status()
        status["ok"] = ok
        return status

    def check_dns_leak(self) -> dict:
        """Проверяет утечку DNS и DPI обхода. Возвращает словарь-вердикт
        (см. dns_leak.check_dns_leak)."""
        try:
            from dns_leak import check_dns_leak
            # В текущей сборке DPI запускается через WinWS, а не через старое
            # поле self.dpi. Из-за проверки self.dpi диагностика всегда думала,
            # что DPI остановлен, и могла ошибочно показывать «утечку/сбой DPI».
            dpi_on = False
            try:
                if hasattr(self, "winws") and self.winws:
                    dpi_on = bool(self.winws.is_running())
                elif hasattr(self, "dpi"):
                    dpi_on = bool(getattr(self.dpi, "running", False))
            except Exception:
                dpi_on = False
            return check_dns_leak(self.config, server_running=self.running, dpi_running=dpi_on)
        except Exception as exc:
            log.warning(f"check_dns_leak: {exc}")
            return {"status": "unknown", "title": f"Ошибка проверки: {exc}",
                    "details": [], "ipv6_present": False, "can_fix": False,
                    "fix_hint": "", "dns_leak": False, "dpi_issue": False}

    def fix_dns_leak(self, disable_ipv6: bool = False) -> tuple:
        """Устраняет IPv6-утечку.

        disable_ipv6=False (рекомендуется): направляем IPv6-DNS на наш сервер
            (::1). Чтобы на ::1 реально кто-то слушал, ВКЛЮЧАЕМ наш IPv6-сервер
            в конфиге и перезапускаем DNS — иначе ::1 «пустой» и утечка осталась
            бы. Это и была причина «нажал Да, но ничего не изменилось».

        disable_ipv6=True: отключаем IPv6-DNS-сервер в конфиге и сбрасываем
            IPv6-DNS адаптеров — система перестаёт слать IPv6 DNS-запросы через
            наш (выключенный) обход; полезно, если IPv6 у пользователя не нужен.
        """
        try:
            from dns_leak import fix_ipv6_leak
            if not disable_ipv6:
                # 1) гарантируем, что наш IPv6-сервер включён
                if not _to_bool(self.config.get("enable_ipv6", True), True):
                    self.config["enable_ipv6"] = True
                    save_config(self.config)
                    self._cfg_ref[0] = self.config
                # 2) перезапускаем сервер, чтобы он реально слушал ::1
                if self.running:
                    self.stop()
                    if not self.start():
                        return False, "Не удалось перезапустить DNS на ::1"
                # 3) направляем IPv6-DNS адаптеров на ::1
                return fix_ipv6_leak(disable_ipv6=False)
            else:
                # отключаем IPv6-сервер и сбрасываем IPv6-DNS адаптеров
                if _to_bool(self.config.get("enable_ipv6", True), True):
                    self.config["enable_ipv6"] = False
                    save_config(self.config)
                    self._cfg_ref[0] = self.config
                return fix_ipv6_leak(disable_ipv6=True)
        except Exception as exc:
            return False, f"Ошибка: {exc}"


_instance = None

def get_instance():
    global _instance
    if _instance is None:
        _instance = UmbraNetDNS()
    return _instance
