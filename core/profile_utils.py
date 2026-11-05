import ipaddress
import time
from urllib.parse import urlsplit

DNS_AI_PROFILE_ID = "builtin-dns-ai"
XBOX_PROFILE_ID = "builtin-xbox-dns"
# BUILTIN_PROFILE_ID — совместимое имя для основного встроенного профиля.
BUILTIN_PROFILE_ID = DNS_AI_PROFILE_ID
MAX_USER_DNS_PROFILES = 10
MAX_PROFILE_NAME_LEN = 20  # имя профиля: от 1 до 20 символов
DOT_DEFAULT_PORT = 853
DOQ_DEFAULT_PORT = 853

# ── Основной встроенный провайдер: DNS-AI ───────────────────────────────────
# DNS-AI не обслуживает обычный DNS на порту 53: используются только DoH, DoT
# и DoQ. Имя обязательно для SNI и проверки TLS-сертификата при подключении по IP.
DNS_AI_DNS_IPV4_PRIMARY = "194.87.196.13"
DNS_AI_DNS_IPV4_SECONDARY = "186.246.49.127"
DNS_AI_DNS_IPV6_PRIMARY = "2a00:b700:5::3:62"
DNS_AI_DNS_IPV6_SECONDARY = "2a0a:2b41:0:500d::53"
DNS_AI_DOH_URL = "https://dns.dns-ai.ru/dns-query"
DNS_AI_HOST = "dns.dns-ai.ru"

DNS_AI_DNS_PROFILE = {
    "id": DNS_AI_PROFILE_ID,
    "name": "DNS-AI",
    "ipv4_primary": DNS_AI_DNS_IPV4_PRIMARY,
    "ipv4_secondary": DNS_AI_DNS_IPV4_SECONDARY,
    "ipv6_primary": DNS_AI_DNS_IPV6_PRIMARY,
    "ipv6_secondary": DNS_AI_DNS_IPV6_SECONDARY,
    "doh_url": DNS_AI_DOH_URL,
    "dot_host": DNS_AI_HOST,
    "dot_ip": DNS_AI_DNS_IPV4_PRIMARY,
    "dot_port": DOT_DEFAULT_PORT,
    "doq_host": DNS_AI_HOST,
    "doq_ip": DNS_AI_DNS_IPV4_PRIMARY,
    "doq_port": DOQ_DEFAULT_PORT,
    "builtin": True,
}

# В коде резолвера это имя означает «основной встроенный профиль».
BUILTIN_DNS_PROFILE = DNS_AI_DNS_PROFILE

# ── Встроенный провайдер: xbox-dns.ru ───────────────────────────────────────
# IP адреса обновляются здесь, а предпочтительный транспорт использует DoH URL
# по доменному имени, поэтому не зависит от смены IP сервиса.
XBOX_DNS_IPV4_PRIMARY = "111.88.96.55"
XBOX_DNS_IPV4_SECONDARY = "176.99.11.77"
XBOX_DNS_IPV4_TERTIARY = "80.78.247.254"
XBOX_DNS_IPV6_PRIMARY = "2a00:ab00:1233:26::50"
XBOX_DNS_IPV6_SECONDARY = "2a00:ab00:1233:26::51"
XBOX_DOH_URL = "https://xbox-dns.ru/dns-query"
XBOX_DOT_HOST = "xbox-dns.ru"
XBOX_DOQ_HOST = "xbox-dns.ru"

XBOX_DNS_PROFILE = {
    "id": XBOX_PROFILE_ID,
    "name": "xbox-dns.ru",
    "ipv4_primary": XBOX_DNS_IPV4_PRIMARY,
    "ipv4_secondary": XBOX_DNS_IPV4_SECONDARY,
    "ipv6_primary": XBOX_DNS_IPV6_PRIMARY,
    "ipv6_secondary": XBOX_DNS_IPV6_SECONDARY,
    "doh_url": XBOX_DOH_URL,
    "dot_host": XBOX_DOT_HOST,
    "dot_ip": XBOX_DNS_IPV4_PRIMARY,
    "dot_port": DOT_DEFAULT_PORT,
    "doq_host": XBOX_DOQ_HOST,
    "doq_ip": XBOX_DNS_IPV4_PRIMARY,
    "doq_port": DOQ_DEFAULT_PORT,
    "builtin": True,
}

# ── Запасной встроенный провайдер: comss.one DNS ────────────────────────────
# Тот же класс сервиса (Smart-DNS для ИИ: ChatGPT/Gemini/Claude/Copilot),
# на базе PowerDNS, поддерживает DoH/DoT/DoQ. Используется как failover, если
# основной (xbox-dns) недоступен — чтобы пользователь не остался без доступа.
COMSS_PROFILE_ID = "builtin-comss"
COMSS_IPV4_PRIMARY = "83.220.169.155"
COMSS_IPV4_SECONDARY = "212.109.195.93"
COMSS_DOH_URL = "https://dns.comss.one/dns-query"
COMSS_HOST = "dns.comss.one"

COMSS_DNS_PROFILE = {
    "id": COMSS_PROFILE_ID,
    "name": "comss.one",
    "ipv4_primary": COMSS_IPV4_PRIMARY,
    "ipv4_secondary": COMSS_IPV4_SECONDARY,
    "ipv6_primary": "",
    "ipv6_secondary": "",
    "doh_url": COMSS_DOH_URL,
    "dot_host": COMSS_HOST,
    "dot_ip": COMSS_IPV4_PRIMARY,
    "dot_port": DOT_DEFAULT_PORT,
    "doq_host": COMSS_HOST,
    "doq_ip": COMSS_IPV4_PRIMARY,
    "doq_port": DOQ_DEFAULT_PORT,
    "builtin": True,
}



def _clean_text(value):
    return str(value).strip() if value is not None else ""



def _validate_ip(value, version):
    value = _clean_text(value)
    if not value:
        return ""
    try:
        ip = ipaddress.ip_address(value)
        return str(ip) if ip.version == version else ""
    except Exception:
        return ""



def _validate_doh_url(value):
    value = _clean_text(value)
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https") and parts.netloc:
            return value
    except Exception:
        pass
    return ""



def _validate_stamp(value):
    """sdns:// штамп DNSCrypt (или пусто). Базовая проверка формата."""
    value = _clean_text(value)
    if not value:
        return ""
    return value if value.startswith("sdns://") else ""


def _validate_host(value):
    """Имя хоста для SNI/проверки сертификата DoT/DoQ (или пусто)."""
    return _clean_text(value)


def _validate_port(value, default=853):
    try:
        port = int(value)
        if 1 <= port <= 65535:
            return port
    except Exception:
        pass
    return default


def _validate_ip_any(value):
    """IP любой версии (для dot_ip/doq_ip)."""
    value = _clean_text(value)
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except Exception:
        return ""


def sanitize_dns_profile(raw_profile, builtin=False, fallback_name=None):
    raw_profile = raw_profile if isinstance(raw_profile, dict) else {}
    name = _clean_text(raw_profile.get("name")) or fallback_name or ("Встроенный профиль" if builtin else "Новый профиль")
    # Ограничиваем длину имени (1..MAX_PROFILE_NAME_LEN), лишнее обрезаем.
    name = name[:MAX_PROFILE_NAME_LEN]
    profile_id = _clean_text(raw_profile.get("id")) or (BUILTIN_PROFILE_ID if builtin else f"user-{time.time_ns()}")
    profile = {
        "id": profile_id,
        "name": name,
        "ipv4_primary": _validate_ip(raw_profile.get("ipv4_primary"), 4),
        "ipv4_secondary": _validate_ip(raw_profile.get("ipv4_secondary"), 4),
        "ipv6_primary": _validate_ip(raw_profile.get("ipv6_primary"), 6),
        "ipv6_secondary": _validate_ip(raw_profile.get("ipv6_secondary"), 6),
        "doh_url": _validate_doh_url(raw_profile.get("doh_url")),
        # DoT (DNS-over-TLS)
        "dot_host": _validate_host(raw_profile.get("dot_host")),
        "dot_ip": _validate_ip_any(raw_profile.get("dot_ip")),
        "dot_port": _validate_port(raw_profile.get("dot_port"), DOT_DEFAULT_PORT),
        # DoQ (DNS-over-QUIC)
        "doq_host": _validate_host(raw_profile.get("doq_host")),
        "doq_ip": _validate_ip_any(raw_profile.get("doq_ip")),
        "doq_port": _validate_port(raw_profile.get("doq_port"), DOQ_DEFAULT_PORT),
        # DNSCrypt — sdns:// штамп (для пользовательских серверов)
        "dnscrypt_stamp": _validate_stamp(raw_profile.get("dnscrypt_stamp")),
        "builtin": bool(builtin),
    }
    return profile



def sanitize_user_dns_profiles(raw_profiles):
    profiles = []
    seen_ids = set()
    if not isinstance(raw_profiles, list):
        return profiles
    for idx, raw in enumerate(raw_profiles[:MAX_USER_DNS_PROFILES], start=1):
        profile = sanitize_dns_profile(raw, builtin=False, fallback_name=f"Профиль {idx}")
        if profile["id"] in seen_ids:
            profile["id"] = f"user-{time.time_ns()}-{idx}"
        seen_ids.add(profile["id"])
        profiles.append(profile)
    return profiles



def get_builtin_dns_profiles():
    """Встроенные профили в порядке, в котором они показываются и фейловерятся."""
    return [
        dict(DNS_AI_DNS_PROFILE),
        dict(XBOX_DNS_PROFILE),
        dict(COMSS_DNS_PROFILE),
    ]


def get_failover_providers(config):
    """Возвращает упорядоченный список провайдеров для failover-резолва.

    Порядок:
      1) активный профиль (выбранный пользователем) — первым;
      2) затем остальные провайдеры (встроенные + пользовательские),
         кроме активного — как запасные.

    Failover отключается, если config["provider_failover"] == False —
    тогда возвращаем только активный профиль.
    """
    active = get_active_dns_profile(config)
    if not config.get("provider_failover", True):
        return [active]

    providers = [active]
    seen = {active.get("id")}
    for p in get_all_dns_profiles(config):
        pid = p.get("id")
        if pid in seen:
            continue
        # запасной провайдер должен уметь хоть как-то резолвить
        if p.get("doh_url") or p.get("ipv4_primary") or p.get("ipv6_primary"):
            providers.append(p)
            seen.add(pid)
    return providers



def get_all_dns_profiles(config):
    return get_builtin_dns_profiles() + sanitize_user_dns_profiles(config.get("user_dns_profiles", []))



def get_profile_by_id(config, profile_id):
    profile_id = _clean_text(profile_id)
    for profile in get_all_dns_profiles(config):
        if profile["id"] == profile_id:
            return profile
    return None



def get_active_dns_profile(config):
    active_id = _clean_text(config.get("active_dns_profile")) or BUILTIN_PROFILE_ID
    profile = get_profile_by_id(config, active_id)
    return profile if profile is not None else dict(BUILTIN_DNS_PROFILE)



def make_new_user_dns_profile(existing_profiles):
    existing_names = {p.get("name") for p in existing_profiles if isinstance(p, dict)}
    index = 1
    while True:
        name = f"Новый профиль {index}"
        if name not in existing_names:
            break
        index += 1
    return sanitize_dns_profile(
        {
            "name": name,
            "ipv4_primary": "",
            "ipv4_secondary": "",
            "ipv6_primary": "",
            "ipv6_secondary": "",
            "doh_url": "",
        },
        builtin=False,
        fallback_name=name,
    )
