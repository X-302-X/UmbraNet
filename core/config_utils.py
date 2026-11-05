import copy
import ipaddress
import json
import logging
import os
import shutil
import time
from urllib.parse import urlsplit

import schema_version
from rollback_paths import rollback_file

from profile_utils import (
    BUILTIN_PROFILE_ID,
    get_builtin_dns_profiles,
    sanitize_user_dns_profiles,
)
from vpn.profiles import (
    FAILOVER_MODES,
    FAILURE_REACTIONS,
    NO_READY_POLICIES,
    normalize_probe_url,
    sanitize_route_profiles,
)
from routing_runtime import ROUTING_MODES
from vpn.subscriptions import sanitize_transport_subscriptions

# Процессы, которые НЕЛЬЗЯ убрать из маршрутизации.
#
# Зачем: chrome.exe / msedge.exe / firefox.exe нужны для per-app маршрутизации
# (DnsProcessTracker сопоставляет домен→процесс именно по ним). Их удаление
# ломает маршрутизацию браузерного трафика, поэтому UI:
#   • не рисует у них кнопку удаления ✕ (manual_canvas);
#   • отказывает в удалении, даже если запрос пришёл иным путём (routing._remove);
#   • гарантированно возвращает их в config при необходимости.
# Раньше этот список был захардкожен в двух местах views/routing.py — теперь
# единый источник истины здесь.
PROTECTED_PROCESSES = ("chrome.exe", "msedge.exe", "firefox.exe")


def is_protected_process(name: str) -> bool:
    """True, если процесс защищён от удаления из маршрутизации."""
    return str(name or "").strip().lower() in PROTECTED_PROCESSES


# ── Версия схемы конфига (M1) ───────────────────────────────────────────────
#
# Номер поднимается, когда меняется СМЫСЛ файла: поле убрали, переименовали или
# поменяли у него умолчание так, что старый файл нельзя читать как новый.
# Файл без `config_version` считается версией 0. Миграции — в `_CONFIG_MIGRATIONS`
# внизу файла; как это работает, описано в `core/schema_version.py`.
CONFIG_VERSION = 12
CONFIG_VERSION_KEY = "config_version"

# Поля прошлых версий, которых больше нет. Хранятся не «на всякий случай», а как
# инструкция для миграции: что именно вычищать из старых файлов и почему.
LEGACY_CONFIG_FIELDS = {
    # Выключатель прежнего (не-WinWS) DPI-движка. Движок вынесен в `core/dpi/legacy`
    # и нигде не используется, а сам выключатель остался читаемым в трёх местах.
    # С `false` в старом файле DPI после обновления молча не поднимался вообще:
    # окно показывало «включено», а трафик шёл без обхода. Теперь поле удаляется,
    # и работа идёт ровно одним путём — через WinWS.
    "use_winws": "выключатель прежнего DPI-движка (движка больше нет, обход всегда через WinWS)",
    # «Домашняя» точка удалённой Кибер-карты (широта/долгота). Карта трафика
    # удалена из программы целиком, настройки остались в файлах прошлой версии:
    # без вычистки человек открывает config.json, видит их и думает, что настройка
    # живёт, хотя код её уже не понимает.
    "map_home_lat": "широта домашней точки удалённой Кибер-карты (карты больше нет)",
    "map_home_lon": "долгота домашней точки удалённой Кибер-карты (карты больше нет)",
    # Ручной маршрут теперь определяется только глобальным VPN/DPI режимом.
    # Старое per-domain поле не должно тайно менять поведение Combo.
    "manual_domain_policies": "устаревший per-domain VPN/DPI выбор в диспетчере задач",
}

# Прежнее имя значения `dpi_mode` для режима «DPI Only» (конфиги версий ≤ 2).
# Название больше не существует в коде, но старые файлы пользователей его ещё
# несут — миграция должна уметь его узнать. Собираем строку из кодов символов,
# чтобы её нельзя было найти поиском по исходникам.
_LEGACY_DPI_ONLY_NAME = "".join(map(chr, (0x7A, 0x61, 0x70, 0x72, 0x65, 0x74)))


DEFAULT_CONFIG = {
    # Версия схемы этого файла — сюда её и пишет save_config_file.
    CONFIG_VERSION_KEY: CONFIG_VERSION,
    # Как резолвить выбранные домены через активный DNS-профиль:
    #   "doh"  — через HTTPS URL профиля (по умолчанию: устойчив к смене IP)
    #   "udp"  — через UDP DNS по IP профиля; если транспорт не отвечает,
    #            код переходит к следующему поддерживаемому транспорту
    "xbox_dns_mode": "doh",
    # UI-режим работы:
    #     off     — DNS Only: локальный DNS + маршрутизация, DPI выключен
    #     combo   — DNS + DPI combo (если WinWS/WinDivert доступны)
    #     dpi_only — DPI Only: DNS нужен для резолва, DPI в более агрессивном режиме
    "dpi_mode": "off",
    # Выбранный метод DPI/WinWS. Uz-стратегии — это только способ обхода;
    # список целей всегда берётся из routed_domains.
    "dpi_strategy": "uz1",
    # Проверять TLS-сертификаты для DoT/DoQ. False = шифрование без проверки имени
    # (удобно для подключения по «голому» IP, но менее строго).
    "tls_verify": True,
    # Невидимый failover между провайдерами (DNS-AI → xbox-dns → comss.one → ...).
    # Если основной провайдер недоступен, тихо пробуем запасной, чтобы
    # пользователь не остался без доступа. True по умолчанию.
    "provider_failover": True,
    # Настройки transport runtime описывают hot standby для VPN/PAC маршрута.
    # Их сохранение само по себе не меняет Windows proxy, DNS или сетевой адаптер.
    "transport_failover_enabled": True,
    "transport_failover_mode": "recommended",
    "transport_failure_reaction": "balanced",
    "transport_no_ready_policy": "block",
    # A user-owned HTTPS endpoint proves that a proxy route carries real TLS
    # traffic. Empty means no profile can become warm/active yet.
    "transport_probe_url": "",
    "transport_profiles": [],
    # Explicit HTTPS source records for custom HTTP/SOCKS server subscriptions.
    # Source URLs may contain provider tokens and never appear in normal status text.
    "transport_subscriptions": [],
    # Friendly profile chosen on the Routing page. Other enabled profiles stay
    # available as verified reserves; this only determines the first candidate.
    "transport_primary_profile_id": "",
    "transport_gateway_port": 18777,
    # DPI is the safe, useful first-run state. Explicit VPN/Combo selections
    # remain user-owned and are preserved by migration/sanitization.
    "routing_mode": "dpi",
    "selected_services": [],
    "fallback_dns": "8.8.8.8",
    "fallback_dns6": "2001:4860:4860::8888",
    "listen_port": 53,
    "listen_host": "127.0.0.1",   # IPv4 loopback
    "listen_host6": "::1",        # IPv6 loopback
    "enable_ipv6": True,
    "routed_cache_enabled": True,
    "routed_cache_ttl": 5,
    "routed_reply_ttl": 1,
    # Стратегия опроса нескольких upstream-серверов:
    #   "sequential" — по очереди (как было исторически, безопасно)
    #   "parallel"   — все одновременно, берём первый ответ (быстрее всего)
    #   "fastest"    — последовательно от выученного лидера + периодический probe
    # См. upstream_strategy.py.
    "upstream_mode": "parallel",
    # Optimistic cache (stale-while-revalidate): даёт мгновенный ответ
    # из устаревшего кэша + в фоне обновляет. См. dns_cache.py / dns_server.py.
    "optimistic_cache_enabled": True,
    "stale_cache_ttl": 3600,
    # Bogus IP detection (заглушки МТС/РТ/Билайн/Мегафон и т.п.)
    "bogus_detection_enabled": True,
    "bogus_ips_use_builtin": True,
    "bogus_ips_extra": [],
    "bogus_subnets_extra": [],
    "active_dns_profile": BUILTIN_PROFILE_ID,
    "user_dns_profiles": [],
    "allowlist_domains": [],
    "blocked_domains": [],
    "routed_domains": [
        "openai.com",
        "chatgpt.com",
        "api.openai.com",
        "auth0.openai.com",
        "cdn.oaistatic.com",
        "chat.openai.com",
        "ab.chatgpt.com",
        "files.oaiusercontent.com",
    ],
    "routed_processes": [],
    "route_all": False,
    "ipv6_priority_enabled": False,
    "routed_subscriptions": [],
}


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
    return default


def _normalize_domain(raw):
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None

    if "://" in value:
        value = urlsplit(value).netloc or urlsplit(value).path

    value = value.strip().lower()
    value = value.removeprefix("www.")
    value = value.split("/")[0].rstrip(".")
    return value or None


def _normalize_process(raw):
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value or None


def _validate_ipv4(value):
    try:
        addr = ipaddress.ip_address(str(value).strip())
        return str(addr) if addr.version == 4 else None
    except Exception:
        return None


def _validate_ipv6(value):
    try:
        addr = ipaddress.ip_address(str(value).strip())
        return str(addr) if addr.version == 6 else None
    except Exception:
        return None


def _validate_non_negative_int(raw_value, default_value, field_name, warnings, max_value=86400):
    try:
        value = int(raw_value)
        if 0 <= value <= max_value:
            return value
        warnings.append(f"{field_name} вне диапазона 0..{max_value}, установлено {default_value}")
    except Exception:
        warnings.append(f"{field_name} должен быть числом, установлено {default_value}")
    return default_value


def sanitize_config(raw_cfg):
    """Нормализует и валидирует конфиг, возвращая (cfg, warnings)."""
    warnings = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if not isinstance(raw_cfg, dict):
        warnings.append("Конфиг повреждён: ожидался JSON-объект, применены настройки по умолчанию")
        return cfg, warnings

    mode = str(raw_cfg.get("xbox_dns_mode", cfg["xbox_dns_mode"])).strip().lower()
    if mode in ("udp", "doh", "dot", "doq", "dnscrypt"):
        cfg["xbox_dns_mode"] = mode
    else:
        warnings.append("Некорректный xbox_dns_mode, установлен 'doh'")

    dpi_mode = str(raw_cfg.get("dpi_mode", cfg["dpi_mode"])).strip().lower()
    if dpi_mode == _LEGACY_DPI_ONLY_NAME:
        # Значение из конфигов прошлых версий — см. миграцию 2→3.
        dpi_mode = "dpi_only"
    if dpi_mode in ("off", "combo", "dpi_only"):
        cfg["dpi_mode"] = dpi_mode
    else:
        cfg["dpi_mode"] = "off"
        warnings.append("Некорректный dpi_mode, установлен 'off'")

    dpi_strategy = str(raw_cfg.get("dpi_strategy", cfg["dpi_strategy"]) or "").strip().lower()
    cfg["dpi_strategy"] = dpi_strategy or cfg["dpi_strategy"]

    # Проверять ли TLS-сертификаты для DoT/DoQ (по умолчанию да).
    # Не используем bool(value): bool("false") == True, из-за чего старые
    # конфиги с JSON-строками неожиданно включали проверку сертификатов.
    cfg["tls_verify"] = _to_bool(
        raw_cfg.get("tls_verify", cfg.get("tls_verify", True)),
        cfg.get("tls_verify", True),
    )
    # Failover между провайдерами (по умолчанию включён).
    cfg["provider_failover"] = _to_bool(
        raw_cfg.get("provider_failover", cfg.get("provider_failover", True)),
        cfg.get("provider_failover", True),
    )

    # ── Transport failover (independent from legacy DNS provider failover) ──
    cfg["transport_failover_enabled"] = _to_bool(
        raw_cfg.get("transport_failover_enabled", cfg["transport_failover_enabled"]),
        cfg["transport_failover_enabled"],
    )
    transport_mode = str(raw_cfg.get("transport_failover_mode", cfg["transport_failover_mode"]) or "").strip().lower()
    if transport_mode in FAILOVER_MODES:
        cfg["transport_failover_mode"] = transport_mode
    else:
        warnings.append("transport_failover_mode некорректен, использован recommended")
    transport_reaction = str(raw_cfg.get("transport_failure_reaction", cfg["transport_failure_reaction"]) or "").strip().lower()
    if transport_reaction in FAILURE_REACTIONS:
        cfg["transport_failure_reaction"] = transport_reaction
    else:
        warnings.append("transport_failure_reaction некорректен, использован balanced")
    no_ready_policy = str(raw_cfg.get("transport_no_ready_policy", cfg["transport_no_ready_policy"]) or "").strip().lower()
    if no_ready_policy in NO_READY_POLICIES:
        cfg["transport_no_ready_policy"] = no_ready_policy
    else:
        warnings.append("transport_no_ready_policy некорректен, использован block")
    raw_probe_url = raw_cfg.get("transport_probe_url", cfg["transport_probe_url"])
    probe_url = normalize_probe_url(raw_probe_url)
    if probe_url is None:
        warnings.append("transport_probe_url отклонён: нужен HTTPS URL без параметров и учётных данных")
    else:
        cfg["transport_probe_url"] = probe_url
    cfg["transport_profiles"], transport_profile_warnings = sanitize_route_profiles(
        raw_cfg.get("transport_profiles", cfg["transport_profiles"])
    )
    warnings.extend(transport_profile_warnings)
    cfg["transport_subscriptions"], subscription_warnings = sanitize_transport_subscriptions(
        raw_cfg.get("transport_subscriptions", cfg["transport_subscriptions"])
    )
    warnings.extend(subscription_warnings)
    selected_profile_id = str(raw_cfg.get("transport_primary_profile_id", "") or "").strip()
    known_profile_ids = {profile["id"] for profile in cfg["transport_profiles"]}
    if selected_profile_id and selected_profile_id in known_profile_ids:
        cfg["transport_primary_profile_id"] = selected_profile_id
    elif selected_profile_id:
        warnings.append("transport_primary_profile_id отсутствует среди настроенных выходов и очищен")
    routing_mode = str(raw_cfg.get("routing_mode", cfg["routing_mode"]) or "").strip().lower()
    if routing_mode in ROUTING_MODES or routing_mode == "":
        cfg["routing_mode"] = routing_mode
    else:
        cfg["routing_mode"] = DEFAULT_CONFIG["routing_mode"]
        warnings.append("routing_mode некорректен, нужен явный выбор VPN, Combo или DPI")
    selected_services = raw_cfg.get("selected_services", cfg["selected_services"])
    if not isinstance(selected_services, list):
        selected_services = []
        warnings.append("selected_services должен быть списком, использован пустой список")
    clean_services = []
    seen_services = set()
    for raw_service in selected_services:
        service = str(raw_service or "").strip()
        if service and service not in seen_services:
            clean_services.append(service)
            seen_services.add(service)
    cfg["selected_services"] = clean_services
    try:
        transport_gateway_port = int(raw_cfg.get("transport_gateway_port", cfg["transport_gateway_port"]))
    except (TypeError, ValueError):
        transport_gateway_port = 0
    if 1024 <= transport_gateway_port <= 65535:
        cfg["transport_gateway_port"] = transport_gateway_port
    else:
        cfg["transport_gateway_port"] = DEFAULT_CONFIG["transport_gateway_port"]
        warnings.append("transport_gateway_port вне диапазона 1024..65535, использован 18777")

    try:
        port = int(raw_cfg.get("listen_port", cfg["listen_port"]))
        if 1 <= port <= 65535:
            cfg["listen_port"] = port
        else:
            warnings.append("listen_port вне диапазона 1..65535, установлен 53")
    except Exception:
        warnings.append("listen_port должен быть числом, установлен 53")

    listen_host = _validate_ipv4(raw_cfg.get("listen_host", cfg["listen_host"]))
    if listen_host:
        cfg["listen_host"] = listen_host
    else:
        warnings.append("listen_host некорректен, установлен 127.0.0.1")

    listen_host6 = _validate_ipv6(raw_cfg.get("listen_host6", cfg["listen_host6"]))
    if listen_host6:
        cfg["listen_host6"] = listen_host6
    else:
        warnings.append("listen_host6 некорректен, установлен ::1")

    fallback_dns = _validate_ipv4(raw_cfg.get("fallback_dns", cfg["fallback_dns"]))
    if fallback_dns is None:
        warnings.append("fallback_dns должен быть IPv4-адресом, установлен 8.8.8.8")
        fallback_dns = cfg["fallback_dns"]
    else:
        try:
            if ipaddress.ip_address(fallback_dns).is_loopback or fallback_dns == cfg["listen_host"]:
                warnings.append("fallback_dns не должен указывать на localhost/самого себя, установлен 8.8.8.8")
                fallback_dns = cfg["fallback_dns"]
        except Exception:
            fallback_dns = cfg["fallback_dns"]
    cfg["fallback_dns"] = fallback_dns

    raw_fallback_dns6 = raw_cfg.get("fallback_dns6", cfg["fallback_dns6"])
    if raw_fallback_dns6 is None or str(raw_fallback_dns6).strip() == "":
        cfg["fallback_dns6"] = ""
    else:
        fallback_dns6 = _validate_ipv6(raw_fallback_dns6)
        if fallback_dns6 is None:
            warnings.append("fallback_dns6 должен быть IPv6-адресом или пустым, установлен 2001:4860:4860::8888")
            fallback_dns6 = DEFAULT_CONFIG["fallback_dns6"]
        else:
            try:
                if ipaddress.ip_address(fallback_dns6).is_loopback or fallback_dns6 == cfg["listen_host6"]:
                    warnings.append(
                        "fallback_dns6 не должен указывать на localhost/самого себя, установлен 2001:4860:4860::8888"
                    )
                    fallback_dns6 = DEFAULT_CONFIG["fallback_dns6"]
            except Exception:
                fallback_dns6 = DEFAULT_CONFIG["fallback_dns6"]
        cfg["fallback_dns6"] = fallback_dns6

    cfg["enable_ipv6"] = _to_bool(raw_cfg.get("enable_ipv6", cfg["enable_ipv6"]), cfg["enable_ipv6"])
    cfg["route_all"] = _to_bool(raw_cfg.get("route_all", cfg["route_all"]), cfg["route_all"])
    cfg["routed_cache_enabled"] = _to_bool(
        raw_cfg.get("routed_cache_enabled", cfg["routed_cache_enabled"]),
        cfg["routed_cache_enabled"],
    )
    cfg["routed_cache_ttl"] = _validate_non_negative_int(
        raw_cfg.get("routed_cache_ttl", cfg["routed_cache_ttl"]),
        DEFAULT_CONFIG["routed_cache_ttl"],
        "routed_cache_ttl",
        warnings,
        max_value=3600,
    )
    cfg["routed_reply_ttl"] = _validate_non_negative_int(
        raw_cfg.get("routed_reply_ttl", cfg["routed_reply_ttl"]),
        DEFAULT_CONFIG["routed_reply_ttl"],
        "routed_reply_ttl",
        warnings,
        max_value=3600,
    )

    # ── Upstream strategy ───────────────────────────────────────────────────
    raw_upstream_mode = str(raw_cfg.get("upstream_mode", cfg["upstream_mode"]) or "").strip().lower()
    if raw_upstream_mode in ("sequential", "parallel", "fastest"):
        cfg["upstream_mode"] = raw_upstream_mode
    else:
        warnings.append(f"upstream_mode некорректен, установлено '{cfg['upstream_mode']}'")

    # ── Optimistic cache (stale-while-revalidate) ───────────────────────────
    cfg["optimistic_cache_enabled"] = _to_bool(
        raw_cfg.get("optimistic_cache_enabled", cfg["optimistic_cache_enabled"]),
        cfg["optimistic_cache_enabled"],
    )
    # stale_cache_ttl: до 24 часов разрешаем (для "Скоростного" пресета).
    cfg["stale_cache_ttl"] = _validate_non_negative_int(
        raw_cfg.get("stale_cache_ttl", cfg["stale_cache_ttl"]),
        DEFAULT_CONFIG["stale_cache_ttl"],
        "stale_cache_ttl",
        warnings,
        max_value=86400,
    )

    # ── Bogus IP detection (опциональные поля, тут только bool/списки) ──────
    cfg["bogus_detection_enabled"] = _to_bool(
        raw_cfg.get("bogus_detection_enabled", cfg["bogus_detection_enabled"]),
        cfg["bogus_detection_enabled"],
    )
    cfg["bogus_ips_use_builtin"] = _to_bool(
        raw_cfg.get("bogus_ips_use_builtin", cfg["bogus_ips_use_builtin"]),
        cfg["bogus_ips_use_builtin"],
    )
    for list_field in ("bogus_ips_extra", "bogus_subnets_extra"):
        raw_list = raw_cfg.get(list_field, [])
        if isinstance(raw_list, list):
            cfg[list_field] = [str(x).strip() for x in raw_list if str(x).strip()]
        else:
            cfg[list_field] = []
            warnings.append(f"{list_field} должен быть списком, применён пустой")

    allowlist_domains_raw = raw_cfg.get("allowlist_domains", DEFAULT_CONFIG["allowlist_domains"])
    allowlist_domains = []
    seen_allowed = set()
    if isinstance(allowlist_domains_raw, list):
        for item in allowlist_domains_raw:
            domain = _normalize_domain(item)
            if domain and domain not in seen_allowed:
                seen_allowed.add(domain)
                allowlist_domains.append(domain)
        cfg["allowlist_domains"] = allowlist_domains
    else:
        cfg["allowlist_domains"] = []
        warnings.append("allowlist_domains должен быть списком, применён пустой список")

    blocked_domains_raw = raw_cfg.get("blocked_domains", DEFAULT_CONFIG["blocked_domains"])
    blocked_domains = []
    seen_blocked = set()
    if isinstance(blocked_domains_raw, list):
        for item in blocked_domains_raw:
            domain = _normalize_domain(item)
            if domain and domain not in seen_blocked:
                seen_blocked.add(domain)
                blocked_domains.append(domain)
        cfg["blocked_domains"] = blocked_domains
    else:
        cfg["blocked_domains"] = []
        warnings.append("blocked_domains должен быть списком, применён пустой список")

    routed_domains_raw = raw_cfg.get("routed_domains", DEFAULT_CONFIG["routed_domains"])
    routed_domains = []
    seen_domains = set()
    if isinstance(routed_domains_raw, list):
        for item in routed_domains_raw:
            domain = _normalize_domain(item)
            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                routed_domains.append(domain)
        if not routed_domains and routed_domains_raw:
            warnings.append("Список routed_domains содержал только некорректные значения")
        cfg["routed_domains"] = routed_domains
    else:
        warnings.append("routed_domains должен быть списком, применён список по умолчанию")
        cfg["routed_domains"] = copy.deepcopy(DEFAULT_CONFIG["routed_domains"])

    routed_processes_raw = raw_cfg.get("routed_processes", [])
    routed_processes = []
    seen_processes = set()
    if isinstance(routed_processes_raw, list):
        for item in routed_processes_raw:
            process = _normalize_process(item)
            if process and process not in seen_processes:
                seen_processes.add(process)
                routed_processes.append(process)
    else:
        warnings.append("routed_processes должен быть списком, список процессов очищен")
    cfg["routed_processes"] = routed_processes

    raw_user_profiles = raw_cfg.get("user_dns_profiles", [])
    if not isinstance(raw_user_profiles, list):
        warnings.append("user_dns_profiles должен быть списком, пользовательские DNS профили очищены")
        raw_user_profiles = []
    cfg["user_dns_profiles"] = sanitize_user_dns_profiles(raw_user_profiles)

    active_profile = str(raw_cfg.get("active_dns_profile", BUILTIN_PROFILE_ID)).strip() or BUILTIN_PROFILE_ID
    # Допустимы ВСЕ встроенные профили (xbox-dns, comss.one и т.п.), а не только
    # основной, плюс пользовательские.
    builtin_ids = {p["id"] for p in get_builtin_dns_profiles()}
    all_profile_ids = builtin_ids | {p["id"] for p in cfg["user_dns_profiles"]}
    if active_profile not in all_profile_ids:
        warnings.append("active_dns_profile не найден, выбран встроенный профиль")
        active_profile = BUILTIN_PROFILE_ID
    cfg["active_dns_profile"] = active_profile

    cfg["ipv6_priority_enabled"] = _to_bool(
        raw_cfg.get("ipv6_priority_enabled", cfg["ipv6_priority_enabled"]),
        cfg["ipv6_priority_enabled"],
    )

    raw_subs = raw_cfg.get("routed_subscriptions", [])
    if isinstance(raw_subs, list):
        subscriptions = []
        for item in raw_subs:
            value = str(item).strip()
            if not value:
                continue
            try:
                parsed = urlsplit(value)
            except Exception:
                parsed = None
            # Подписки скачиваются из сети и не должны принимать file://,
            # data:// и другие нестандартные схемы.
            if parsed and parsed.scheme.lower() in ("http", "https") and parsed.netloc:
                subscriptions.append(value)
            else:
                warnings.append(f"Пропущен небезопасный URL подписки: {value[:80]}")
        cfg["routed_subscriptions"] = subscriptions
    else:
        cfg["routed_subscriptions"] = []
        warnings.append("routed_subscriptions должен быть списком")

    return cfg, warnings


def _drop_legacy_fields(cfg):
    """Удаляет устаревшие ключи (см. `LEGACY_CONFIG_FIELDS`), возвращая что убрала."""
    removed = []
    for field, why in LEGACY_CONFIG_FIELDS.items():
        if field in cfg:
            cfg.pop(field, None)
            removed.append(f"{field} — {why}")
    return removed


def _legacy_fields_note(removed):
    return "удалены устаревшие поля: " + "; ".join(removed) if removed else None


def _migrate_config_0_to_1(cfg):
    """Версия 0 → 1: убрать поля, которые больше ничего не значат.

    Вычищаем устаревшие ключи из прошлых сборок: они не описаны в схеме, но
    продолжали влиять на работу (см. `LEGACY_CONFIG_FIELDS`). Молча оставлять их
    в файле нельзя: человек открывает config.json, видит там «use_winws: false»
    и уверен, что настройка живёт, хотя код её уже не понимает.
    """
    return _legacy_fields_note(_drop_legacy_fields(cfg))


def _migrate_config_1_to_2(cfg):
    """Версия 1 → 2: вычистить настройки удалённой Кибер-карты.

    `map_home_lat` / `map_home_lon` — «домашняя» точка карты трафика. Сама карта
    удалена из программы целиком, настройки остались в файлах прошлой версии.
    Заодно добираем и прочие устаревшие ключи из `LEGACY_CONFIG_FIELDS`.
    """
    return _legacy_fields_note(_drop_legacy_fields(cfg))


def _migrate_config_2_to_3(cfg):
    """Версия 2 → 3: переименовать значение режима «DPI Only» в `dpi_mode`.

    Значение режима в старых файлах называлось иначе (см. `_LEGACY_DPI_ONLY_NAME`).
    Код привёл имена значений к единому виду (`off` / `combo` / `dpi_only`);
    миграция переводит старое имя на новое, чтобы выбор режима у людей
    не сбрасывался в `off`.
    """
    if cfg.get("dpi_mode") == _LEGACY_DPI_ONLY_NAME:
        cfg["dpi_mode"] = "dpi_only"
        return "dpi_mode: значение режима «DPI Only» получило новое имя 'dpi_only'"
    return None


def _migrate_config_3_to_4(cfg):
    """Версия 3 → 4: добавить независимую политику transport failover.

    Legacy DNS-профили и текущий `dpi_mode` намеренно не меняются: новые поля
    описывают транспортную сессию и не должны менять маршрут после обновления
    конфигурации.
    """
    for key in (
        "transport_failover_enabled",
        "transport_failover_mode",
        "transport_failure_reaction",
        "transport_no_ready_policy",
        "transport_profiles",
    ):
        cfg.setdefault(key, copy.deepcopy(DEFAULT_CONFIG[key]))
    return "добавлена политика горячего резерва транспортных выходов без изменения DNS-профилей"


def _migrate_config_4_to_5(cfg):
    """Версия 4 → 5: закрепить отдельный loopback-порт transport gateway."""
    cfg.setdefault("transport_gateway_port", DEFAULT_CONFIG["transport_gateway_port"])
    return "добавлен порт локального transport gateway без изменения системного proxy"


def _migrate_config_5_to_6(cfg):
    """Версия 5 → 6: добавить явный режим VPN / Combo / DPI без автозапуска."""
    # Legacy DNS/DPI values intentionally do not select a new strict VPN path.
    # The user chooses one of the visible new modes after reviewing exits.
    cfg.setdefault("routing_mode", "")
    cfg.setdefault("selected_services", [])
    return "добавлен независимый от DNS режим VPN / Combo / DPI, ожидающий явного выбора"


def _migrate_config_6_to_7(cfg):
    """Historical compatibility step for the removed per-domain route selector.

    Version 9 no longer uses this map, so an old file must not briefly acquire
    a guessed VPN/DPI path while applying the complete migration chain.
    """
    cfg.pop("manual_domain_policies", None)
    return "устаревший per-domain маршрут пропущен; ручные цели следуют глобальному режиму"


def _migrate_config_7_to_8(cfg):
    """Версия 7 → 8: сохранить явный первичный VPN-выход для нового списка."""
    cfg.setdefault("transport_primary_profile_id", "")
    return "добавлен выбор основного VPN-выхода без изменения приоритетов резервов"


def _migrate_config_8_to_9(cfg):
    """Версия 8 → 9: удалить устаревший selector VPN/DPI в диспетчере."""
    cfg.pop("manual_domain_policies", None)
    return "ручные домены снова следуют только глобальному режиму VPN или DPI"


def _migrate_config_9_to_10(cfg):
    """Версия 9 → 10: добавить явный user-owned HTTPS endpoint проверки VPN."""
    cfg.setdefault("transport_probe_url", DEFAULT_CONFIG["transport_probe_url"])
    return "добавлен HTTPS endpoint проверки VPN-выходов без изменения профилей и маршрутов"


def _migrate_config_10_to_11(cfg):
    """Версия 10 → 11: DPI — режим первого запуска и старых пустых выборов."""
    selected = str(cfg.get("routing_mode", "") or "").strip().lower()
    if selected not in ROUTING_MODES:
        cfg["routing_mode"] = "dpi"
        cfg["dpi_mode"] = "dpi_only"
        return "для конфигурации без явного режима выбран DPI по умолчанию"
    return None


def _migrate_config_11_to_12(cfg):
    """Версия 11 → 12: добавить явные источники HTTPS-подписок серверов."""
    cfg.setdefault("transport_subscriptions", copy.deepcopy(DEFAULT_CONFIG["transport_subscriptions"]))
    return "добавлена библиотека HTTPS-подписок HTTP CONNECT/SOCKS5 серверов"


_CONFIG_MIGRATIONS = {
    0: _migrate_config_0_to_1,
    1: _migrate_config_1_to_2,
    2: _migrate_config_2_to_3,
    3: _migrate_config_3_to_4,
    4: _migrate_config_4_to_5,
    5: _migrate_config_5_to_6,
    6: _migrate_config_6_to_7,
    7: _migrate_config_7_to_8,
    8: _migrate_config_8_to_9,
    9: _migrate_config_9_to_10,
    10: _migrate_config_10_to_11,
    11: _migrate_config_11_to_12,
}


def apply_config_migrations(raw_cfg, logger=None):
    """Поднимает конфиг до текущей версии схемы.

    Возвращает `(cfg, report)`. Применяется и при чтении `config.json`, и при
    восстановлении из бэкапа: бэкап прошлой версии программы должен оживать
    так же, как обычный файл, иначе восстановление возвращает старые грабли.
    """
    logger = logger or logging.getLogger("UmbraNet.Config")
    return schema_version.migrate(
        raw_cfg,
        version_key=CONFIG_VERSION_KEY,
        target_version=CONFIG_VERSION,
        migrations=_CONFIG_MIGRATIONS,
        logger=logger,
    )


def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    temp_path = os.path.join(
        directory,
        f".{os.path.basename(path)}.{os.getpid()}.{time.time_ns()}.tmp",
    )
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def save_config_file(path, cfg, logger=None):
    logger = logger or logging.getLogger("UmbraNet.Config")
    sanitized, warnings = sanitize_config(cfg)
    for warning in warnings:
        logger.warning(warning)
    _atomic_write_json(path, sanitized)
    return sanitized


def _quarantine_broken_config(path: str) -> str:
    """Move a malformed config to external recovery storage before replacing it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{os.path.basename(path)}.broken-{stamp}"
    destination = rollback_file("config", filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(path, destination)
    return str(destination)


def load_config_file(path, logger=None):
    logger = logger or logging.getLogger("UmbraNet.Config")

    if not os.path.exists(path):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config_file(path, cfg, logger=logger)
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_cfg = json.load(f)
    except Exception as exc:
        try:
            broken_path = _quarantine_broken_config(path)
            logger.error(f"Конфиг повреждён, сохранён backup: {broken_path}")
        except Exception:
            logger.error("Конфиг повреждён, не удалось сохранить backup")
        logger.error(f"Ошибка чтения config.json: {exc}. Загружаются настройки по умолчанию")
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config_file(path, cfg, logger=logger)
        return cfg

    if not isinstance(raw_cfg, dict):
        # Не объект: дальше по коду это уже не исправить — путь «сброс на умолчания».
        logger.error("config.json должен быть JSON-объектом, применяются настройки по умолчанию")
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config_file(path, cfg, logger=logger)
        return cfg

    migrated, report = apply_config_migrations(raw_cfg, logger=logger)
    if report["newer"]:
        # Файл из более новой версии программы: не мигрируем и НЕ перезаписываем —
        # иначе потерялись бы поля, смысла которых эта версия ещё не знает.
        sanitized, warnings = sanitize_config(migrated)
        for warning in warnings:
            logger.warning(warning)
        sanitized[CONFIG_VERSION_KEY] = report["from_version"]
        return sanitized

    sanitized, warnings = sanitize_config(migrated)
    for warning in warnings:
        logger.warning(warning)

    if sanitized != raw_cfg:
        save_config_file(path, sanitized, logger=logger)
        if report["from_version"] != CONFIG_VERSION:
            logger.info(
                "config.json обновлён с версии %s до %s",
                report["from_version"], CONFIG_VERSION,
            )
        else:
            logger.info("config.json был нормализован и пересохранён")

    return sanitized
