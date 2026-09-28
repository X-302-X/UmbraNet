"""
UmbraNet — Bogus IP detection
==============================

Российские провайдеры (МТС, Ростелеком, Билайн, Мегафон и т.д.) при попадании
домена в реестр блокировок RKN возвращают не реальный IP сайта, а свой
"подменный" (stub / заглушку): обычно это страница "Доступ ограничен".

Если UmbraNet увидит такой IP в ответе от системного DNS (fallback) — это
сигнал, что провайдер подменил ответ. В этом случае мы:

  1) пробуем переспросить через альтернативный upstream (DoH/xbox-dns),
  2) если и там фейк — отвечаем клиенту NXDOMAIN, чтобы браузер показал
     честную ошибку, а не висел на странице-заглушке.

Списки IP собраны из публичных источников (UniBlock, GoodbyeDPI, другие
публичные списки, сообщения пользователей). Список можно расширить через
config.json → "bogus_ips_extra" / "bogus_subnets_extra".

ВАЖНО: тут только IP/подсети, известные как "заглушки провайдеров". Реальные
IP сайтов сюда попадать НЕ должны — иначе мы случайно убьём легитимные ответы.
"""

import ipaddress
import logging

from config_utils import _to_bool

log = logging.getLogger("UmbraNet.BogusIPs")


# ── Точечные IP-заглушки (одиночные адреса) ─────────────────────────────────
BUILTIN_BOGUS_IPS = {
    # МТС "Доступ ограничен"
    "212.188.4.10",
    "212.188.4.11",
    "62.231.124.4",
    "62.231.124.5",
    # МТС/партнёрские заглушки, часто встречающиеся при блокировке AI/медиа
    # (например, chatgpt.com может вернуть real IPv6 + эти bogus IPv4).
    "8.6.112.0",
    "8.47.69.0",
    "8.43.85.0",
    "8.43.85.1",
    "8.34.212.0",
    # Билайн (ВымпелКом)
    "83.69.208.117",
    "85.21.79.142",
    "85.21.79.190",
    # Ростелеком (Rostelecom)
    "95.167.13.50",
    "95.167.13.51",
    "212.45.30.131",
    # Мегафон
    "83.149.32.59",
    "83.149.32.60",
    # ER-Telecom (Дом.ру)
    "5.45.81.121",
    "85.143.220.150",
    # ТТК
    "92.241.180.110",
    # Распространённые "0.0.0.0" / loopback подмены провайдеров
    "0.0.0.0",  # nosec B104 - известный bogus-адрес, не bind()
    "127.0.0.1",
    # IPv6 фейки
    "::",
    "::1",
}


# ── Подсети-заглушки (когда провайдер раздаёт фейки из целой подсети) ────────
BUILTIN_BOGUS_SUBNETS = [
    # МТС — подсеть страниц-заглушек
    "212.188.4.0/24",
    # МТС/партнёрские bogus-пулы для AI/медиа заглушек
    "8.6.112.0/24",
    "8.47.69.0/24",
    "8.43.85.0/24",
    "8.34.212.0/24",
    # Билайн "stub" подсеть
    "85.21.79.128/27",
    # Ростелеком "stub" подсеть
    "95.167.13.0/24",
]


def _parse_subnets(values):
    nets = []
    for raw in values or []:
        try:
            nets.append(ipaddress.ip_network(str(raw).strip(), strict=False))
        except Exception as exc:
            log.warning("Не удалось распарсить подсеть %r: %s", raw, exc)
    return nets


def build_bogus_index(config: dict):
    """Собирает финальный набор bogus IP и подсетей с учётом конфига.

    Возвращает (set_of_ipaddress_objects, list_of_ip_network_objects).
    Конфиг может расширить или ПОЛНОСТЬЮ заменить встроенный список:

        "bogus_ips_use_builtin": true/false   (по умолчанию true)
        "bogus_ips_extra":     ["1.2.3.4", ...]
        "bogus_subnets_extra": ["10.0.0.0/24", ...]
    """
    use_builtin = _to_bool(config.get("bogus_ips_use_builtin", True), True)

    raw_ips = set()
    if use_builtin:
        raw_ips.update(BUILTIN_BOGUS_IPS)
    for v in config.get("bogus_ips_extra", []) or []:
        raw_ips.add(str(v).strip())

    parsed_ips = set()
    for raw in raw_ips:
        if not raw:
            continue
        try:
            parsed_ips.add(ipaddress.ip_address(raw))
        except Exception as exc:
            log.warning("Не удалось распарсить bogus IP %r: %s", raw, exc)

    subnets = []
    if use_builtin:
        subnets.extend(_parse_subnets(BUILTIN_BOGUS_SUBNETS))
    subnets.extend(_parse_subnets(config.get("bogus_subnets_extra", [])))

    return parsed_ips, subnets


def response_contains_bogus(response, bogus_ips: set, bogus_subnets: list) -> tuple:
    """Проверяет, есть ли в A/AAAA-ответе хотя бы один bogus-адрес.

    Возвращает (is_bogus, matched_ip_string|None).
    Если в ответе нет A/AAAA записей (например, MX/TXT) — возвращает (False, None).
    """
    if response is None:
        return False, None

    try:
        from dnslib import QTYPE
        a_code = QTYPE.A
        aaaa_code = QTYPE.AAAA
    except Exception:
        a_code, aaaa_code = 1, 28

    for rr in getattr(response, "rr", []) or []:
        rtype = int(getattr(rr, "rtype", 0) or 0)
        if rtype not in (a_code, aaaa_code):
            continue
        ip_str = str(getattr(rr, "rdata", "")).strip()
        if not ip_str:
            continue
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except Exception:
            continue
        if ip_obj in bogus_ips:
            return True, ip_str
        for net in bogus_subnets:
            if ip_obj.version == net.version and ip_obj in net:
                return True, ip_str
    return False, None


def build_bogus_index_with_cache(config: dict, config_dir: str):
    """Расширенная версия build_bogus_index: мержит builtin + конфиг + диск-кэш.

    Используется при старте DNS-сервера — подгружает последний сохранённый
    кэш BogusUpdater'а поверх встроенного списка.

    Возвращает (set_of_ipaddress_objects, list_of_ip_network_objects) —
    тот же формат, что build_bogus_index().
    """
    # Базовый индекс: builtin + пользовательские из конфига
    ips, subnets = build_bogus_index(config)

    # Добавляем из диск-кэша (результат последнего успешного обновления)
    try:
        from bogus_updater import load_cached  # type: ignore
        cached_ips, cached_subnets = load_cached(config_dir)

        for raw in cached_ips:
            try:
                ips.add(ipaddress.ip_address(raw))
            except ValueError:
                pass

        for raw in cached_subnets:
            try:
                net = ipaddress.ip_network(raw, strict=False)
                if net not in subnets:
                    subnets.append(net)
            except ValueError:
                pass

        if cached_ips or cached_subnets:
            log.debug(
                "bogus_ips: применён диск-кэш (+%d IP, +%d подсетей), итого %d IP / %d подсетей",
                len(cached_ips), len(cached_subnets), len(ips), len(subnets),
            )
    except Exception as exc:
        log.debug("bogus_ips: не удалось загрузить диск-кэш: %s", exc)

    return ips, subnets
