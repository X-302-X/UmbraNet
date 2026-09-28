"""
Тесты нормализации конфига — `core/config_utils.sanitize_config` (пункт 10 плана).
=================================================================================

Это «входной фильтр» программы: что бы ни лежало в `config.json` (правка руками,
остатки старой версии, битые типы), после `sanitize_config` настройки должны быть
безопасными и рабочими — иначе программа либо не поднимется, либо будет вести себя
странно. Именно поэтому проверяем не «приятные» значения, а мусор: строки вместо
чисел, чужие адреса, перевёрнутые флаги, ссылки не тех схем.

Отдельно проверяем историческую ловушку: `bool("false") == True`, из-за которой
старые конфиги с JSON-строками неожиданно ВКЛЮЧАЛИ проверку сертификатов. Флаг
`tls_verify` разбирается отдельной функцией `_to_bool`, и это закреплено тестом.

Запуск: python -m pytest tests/test_config_sanitize.py
"""

from __future__ import annotations

import copy
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dns"), str(ROOT / "core" / "dpi")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import config_utils
from config_utils import DEFAULT_CONFIG, sanitize_config


def _sanitize(**raw):
    """Прогон сырых настроек через нормализацию: (итог, предупреждения)."""
    return sanitize_config(dict(raw))


def _has_warning(warnings, fragment: str) -> bool:
    return any(fragment.lower() in w.lower() for w in warnings)


# ── Основа ──────────────────────────────────────────────────────────────────

def test_broken_config_falls_back_to_defaults():
    """Не объект вместо конфига (список, строка, None) — берём настройки по умолчанию."""
    for broken in ([], "не json", 42, None):
        cfg, warnings = sanitize_config(broken)
        assert cfg == DEFAULT_CONFIG, f"на входе {broken!r} получен не дефолтный конфиг"
        assert warnings, "о подмене конфига никто не предупредил"


def test_unknown_keys_are_dropped():
    """Лишние ключи не переносятся: конфиг — только известные настройки."""
    cfg, _ = _sanitize(какой_то_мусор="значение", ещё_один=123)
    assert "какой_то_мусор" not in cfg and "ещё_один" not in cfg
    assert set(cfg) == set(DEFAULT_CONFIG), "набор ключей разошёлся с DEFAULT_CONFIG"


def test_default_config_is_not_modified():
    """Нормализация не трогает сам DEFAULT_CONFIG (общие списки — классическая ловушка)."""
    before = copy.deepcopy(DEFAULT_CONFIG)

    cfg, _ = _sanitize(routed_domains=["example.com"], blocked_domains=["bad.example"])
    cfg["routed_domains"].append("ещё.example")
    cfg["blocked_domains"].clear()

    assert DEFAULT_CONFIG == before, "правка результата изменила DEFAULT_CONFIG"
    assert DEFAULT_CONFIG["routed_domains"] is not cfg["routed_domains"], (
        "список в результате — та же ссылка, что в DEFAULT_CONFIG"
    )


def test_result_is_idempotent():
    """Повторная нормализация уже нормального конфига ничего не меняет и не ругается."""
    raw = {
        "xbox_dns_mode": "doq", "dpi_mode": "combo", "listen_port": 5353,
        "listen_host": "127.0.0.1", "listen_host6": "::1",
        "routed_domains": ["Example.com", "www.example.com", "example.com"],
        "routed_processes": ["Chrome.exe", "chrome.exe"],
        "routed_subscriptions": ["https://example.com/list.txt"],
    }
    first, first_warnings = sanitize_config(raw)
    second, second_warnings = sanitize_config(first)

    assert second == first, "вторая нормализация изменила конфиг"
    assert not second_warnings, f"вторая нормализация ругается: {second_warnings}"
    assert first_warnings == [], f"нормальный конфиг вызвал предупреждения: {first_warnings}"


# ── Режимы и флаги ──────────────────────────────────────────────────────────

def test_dns_mode_valid_values_and_case():
    for value in ("udp", "doh", "dot", "doq", "dnscrypt", " DoH ", "DOT"):
        cfg, warnings = _sanitize(xbox_dns_mode=value)
        assert cfg["xbox_dns_mode"] == value.strip().lower(), f"{value!r} не принят"
        assert not _has_warning(warnings, "xbox_dns_mode"), f"{value!r} вызвал предупреждение"


def test_dns_mode_invalid_falls_back_to_doh():
    """Неизвестный транспорт → DoH (безопасный выбор) и предупреждение."""
    cfg, warnings = _sanitize(xbox_dns_mode="через-спутник")
    assert cfg["xbox_dns_mode"] == "doh"
    assert _has_warning(warnings, "xbox_dns_mode")


def test_dpi_mode_invalid_falls_back_to_off():
    """Неизвестный режим DPI → выключено: не запускаем движок наугад."""
    cfg, warnings = _sanitize(dpi_mode="максимум-на-всё")
    assert cfg["dpi_mode"] == "off"
    assert _has_warning(warnings, "dpi_mode")


def test_dpi_strategy_is_not_validated_but_normalized():
    """Стратегия — свободное имя файла: только регистр и пробелы, без подмены."""
    cfg, _ = _sanitize(dpi_strategy="  UZ1_Auto  ")
    assert cfg["dpi_strategy"] == "uz1_auto"

    cfg, _ = _sanitize(dpi_strategy="")
    assert cfg["dpi_strategy"] == DEFAULT_CONFIG["dpi_strategy"], "пустая стратегия не заменена"


def test_tls_verify_string_false_means_false():
    """«false» строкой — это ВЫКЛЮЧЕНО (историческая ловушка bool('false') == True)."""
    for raw in ("false", "False", "0", "no", "off"):
        cfg, _ = _sanitize(tls_verify=raw)
        assert cfg["tls_verify"] is False, f"{raw!r} разобран как включено"

    for raw in ("true", "1", "yes", "on", True):
        cfg, _ = _sanitize(tls_verify=raw)
        assert cfg["tls_verify"] is True, f"{raw!r} разобран как выключено"


def test_unknown_words_in_flags_mean_default():
    """Незнакомое слово («нет», «наверное») — не догадки: берём значение по умолчанию.

    Для tls_verify по умолчанию проверка ВКЛЮЧЕНА, поэтому даже нераспознанное
    значение не ослабляет безопасность.
    """
    for raw in ("нет", "наверное", "2", "да!"):
        cfg, _ = _sanitize(tls_verify=raw)
        assert cfg["tls_verify"] is True, f"незнакомое {raw!r} выключило проверку сертификатов"

    cfg, _ = _sanitize(route_all="ага")
    assert cfg["route_all"] == DEFAULT_CONFIG["route_all"]


def test_boolean_flags_accept_garbage_safely():
    """Мусор во флагах не роняет нормализацию — берём значение по умолчанию."""
    cfg, _ = _sanitize(enable_ipv6="может быть", route_all={"да": True},
                       optimistic_cache_enabled=None, provider_failover=[])
    assert cfg["enable_ipv6"] == DEFAULT_CONFIG["enable_ipv6"]
    assert cfg["route_all"] == DEFAULT_CONFIG["route_all"]
    assert cfg["optimistic_cache_enabled"] == DEFAULT_CONFIG["optimistic_cache_enabled"]
    assert cfg["provider_failover"] == DEFAULT_CONFIG["provider_failover"]


# ── Адреса и порт ───────────────────────────────────────────────────────────

def test_listen_port_range():
    cfg, _ = _sanitize(listen_port=5353)
    assert cfg["listen_port"] == 5353

    for bad, fragment in ((0, "диапазона"), (70000, "диапазона"), ("порт", "числом")):
        cfg, warnings = _sanitize(listen_port=bad)
        assert cfg["listen_port"] == 53, f"порт {bad!r} не заменён на 53"
        assert _has_warning(warnings, "listen_port")


def test_listen_host_must_be_ipv4():
    """Адрес прослушивания — только IPv4-адрес: имя хоста или IPv6 не подходят.

    Любой КОРРЕКТНЫЙ IPv4 принимается, включая адрес локальной сети: пользователь
    может осознанно поднять DNS для домашней сети, а не только на 127.0.0.1.
    """
    cfg, _ = _sanitize(listen_host="192.168.1.5")
    assert cfg["listen_host"] == "192.168.1.5", "корректный IPv4 из локальной сети отвергнут"

    for bad in ("localhost", "::1", "", "не-адрес", 127):
        cfg, warnings = _sanitize(listen_host=bad)
        assert cfg["listen_host"] == "127.0.0.1", f"listen_host={bad!r} не заменён"
        assert _has_warning(warnings, "listen_host")


def test_listen_host6_accepts_only_ipv6():
    cfg, _ = _sanitize(listen_host6="2001:db8::1")
    assert cfg["listen_host6"] == "2001:db8::1"

    cfg, warnings = _sanitize(listen_host6="127.0.0.1")
    assert cfg["listen_host6"] == "::1"
    assert _has_warning(warnings, "listen_host6")


def test_fallback_dns_must_be_public_ipv4():
    """Запасной DNS не может быть IPv6, пустышкой или «самим собой» (localhost)."""
    cfg, _ = _sanitize(fallback_dns="1.1.1.1")
    assert cfg["fallback_dns"] == "1.1.1.1"

    for bad in ("2001:4860:4860::8888", "не-адрес", ""):
        cfg, warnings = _sanitize(fallback_dns=bad)
        assert cfg["fallback_dns"] == "8.8.8.8", f"fallback_dns={bad!r} не заменён"
        assert _has_warning(warnings, "fallback_dns")


def test_fallback_dns_loopback_is_rejected():
    """Запасной DNS на localhost = петля на самого себя: подменяем и предупреждаем."""
    cfg, warnings = _sanitize(fallback_dns="127.0.0.1")
    assert cfg["fallback_dns"] == "8.8.8.8"
    assert _has_warning(warnings, "localhost")

    # То же, но адрес совпадает с listen_host — тоже петля.
    cfg, warnings = _sanitize(listen_host="10.0.0.5", fallback_dns="10.0.0.5")
    assert cfg["fallback_dns"] == "8.8.8.8"
    assert _has_warning(warnings, "localhost")


def test_fallback_dns6_can_be_disabled_by_empty_value():
    """Пустая строка / None в fallback_dns6 — это «не использовать IPv6», а не ошибка."""
    for empty in ("", "   ", None):
        cfg, warnings = _sanitize(fallback_dns6=empty)
        assert cfg["fallback_dns6"] == "", f"{empty!r} не выключил IPv6-fallback"
        assert not _has_warning(warnings, "fallback_dns6")


def test_fallback_dns6_loopback_is_rejected():
    cfg, warnings = _sanitize(fallback_dns6="::1")
    assert cfg["fallback_dns6"] == DEFAULT_CONFIG["fallback_dns6"]
    assert _has_warning(warnings, "fallback_dns6")


def test_cache_ttl_ranges():
    """TTL кэшей ограничены сверху: невероятные значения заменяются на дефолтные."""
    cfg, _ = _sanitize(routed_cache_ttl=30, routed_reply_ttl=2, stale_cache_ttl=600)
    assert (cfg["routed_cache_ttl"], cfg["routed_reply_ttl"], cfg["stale_cache_ttl"]) == (30, 2, 600)

    for field, limit in (("routed_cache_ttl", 3600), ("routed_reply_ttl", 3600),
                         ("stale_cache_ttl", 86400)):
        cfg, warnings = _sanitize(**{field: limit * 10})
        assert cfg[field] == DEFAULT_CONFIG[field], f"{field} не ограничен сверху"
        assert _has_warning(warnings, field)

        cfg, warnings = _sanitize(**{field: -5})
        assert cfg[field] == DEFAULT_CONFIG[field], f"{field} принял отрицательное значение"
        assert _has_warning(warnings, field)

        cfg, warnings = _sanitize(**{field: "много"})
        assert cfg[field] == DEFAULT_CONFIG[field]
        assert _has_warning(warnings, field)


def test_upstream_mode_values():
    for value in ("sequential", "parallel", "fastest"):
        cfg, _ = _sanitize(upstream_mode=value)
        assert cfg["upstream_mode"] == value

    cfg, warnings = _sanitize(upstream_mode="как-нибудь")
    assert cfg["upstream_mode"] == DEFAULT_CONFIG["upstream_mode"]
    assert _has_warning(warnings, "upstream_mode")


# ── Списки доменов и процессов ──────────────────────────────────────────────

def test_domain_lists_are_normalized_and_deduplicated():
    """Домены приводятся к нижнему регистру, без www, схем и путей; дубли убираются."""
    cfg, warnings = _sanitize(
        routed_domains=["Example.COM", "https://www.example.com/path?x=1", "example.com", ""],
        allowlist_domains=["  site.org.  "],
        blocked_domains=["Bad.Example", "BAD.example"],
    )

    assert cfg["routed_domains"] == ["example.com"], f"получилось {cfg['routed_domains']}"
    assert cfg["allowlist_domains"] == ["site.org"]
    assert cfg["blocked_domains"] == ["bad.example"]
    assert warnings == [], f"нормальные записи вызвали предупреждения: {warnings}"


def test_domain_lists_with_broken_items_are_cleared():
    """Список из одних мусорных записей очищается и об этом предупреждают."""
    cfg, warnings = _sanitize(routed_domains=[123, None, "   ", ""])
    assert cfg["routed_domains"] == []
    assert _has_warning(warnings, "routed_domains")


def test_domain_lists_wrong_type_fall_back():
    """Список не того типа: blocked/allowlist опустошаются, routed — берём дефолт."""
    cfg, warnings = _sanitize(blocked_domains="example.com", allowlist_domains=42,
                              routed_domains={"a": 1})
    assert cfg["blocked_domains"] == []
    assert cfg["allowlist_domains"] == []
    assert cfg["routed_domains"] == DEFAULT_CONFIG["routed_domains"], (
        "сломанный routed_domains оставил программу без списка целей"
    )
    assert _has_warning(warnings, "routed_domains")


def test_process_list_is_normalized():
    cfg, _ = _sanitize(routed_processes=["Chrome.EXE", "chrome.exe", "  msedge.exe  ", "", 5])
    assert cfg["routed_processes"] == ["chrome.exe", "msedge.exe"]

    cfg, warnings = _sanitize(routed_processes="chrome.exe")
    assert cfg["routed_processes"] == []
    assert _has_warning(warnings, "routed_processes")


def test_bogus_lists_accept_only_lists():
    cfg, _ = _sanitize(bogus_ips_extra=[" 1.2.3.4 ", ""], bogus_subnets_extra=["10.0.0.0/8"])
    assert cfg["bogus_ips_extra"] == ["1.2.3.4"]
    assert cfg["bogus_subnets_extra"] == ["10.0.0.0/8"]

    cfg, warnings = _sanitize(bogus_ips_extra="1.2.3.4")
    assert cfg["bogus_ips_extra"] == []
    assert _has_warning(warnings, "bogus_ips_extra")


# ── Подписки и профили ──────────────────────────────────────────────────────

def test_subscriptions_accept_only_http_and_https():
    """Подписки скачиваются из сети: file://, data:// и прочее отбрасываются."""
    cfg, warnings = _sanitize(routed_subscriptions=[
        "https://example.com/list.txt",
        "http://example.com/other.txt",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/list.txt",
        "просто текст",
        "",
    ])

    assert cfg["routed_subscriptions"] == ["https://example.com/list.txt",
                                           "http://example.com/other.txt"]
    assert _has_warning(warnings, "небезопасный"), f"о небезопасных ссылках не предупредили: {warnings}"


def test_subscriptions_wrong_type_is_cleared():
    cfg, warnings = _sanitize(routed_subscriptions="https://example.com/list.txt")
    assert cfg["routed_subscriptions"] == []
    assert _has_warning(warnings, "routed_subscriptions")


def test_active_profile_unknown_falls_back_to_builtin():
    """Профиль, которого больше нет (например, удалили пользовательский), — на встроенный."""
    cfg, warnings = _sanitize(active_dns_profile="user-которого-нет")
    assert cfg["active_dns_profile"] == config_utils.BUILTIN_PROFILE_ID
    assert _has_warning(warnings, "active_dns_profile")


def test_active_profile_keeps_known_builtin():
    cfg, warnings = _sanitize(active_dns_profile=config_utils.BUILTIN_PROFILE_ID)
    assert cfg["active_dns_profile"] == config_utils.BUILTIN_PROFILE_ID
    assert not _has_warning(warnings, "active_dns_profile")


def test_user_profiles_wrong_type_is_cleared():
    cfg, warnings = _sanitize(user_dns_profiles={"id": "x"})
    assert cfg["user_dns_profiles"] == []
    assert _has_warning(warnings, "user_dns_profiles")


def test_user_profile_can_become_active():
    """Свой профиль с корректным id можно выбрать активным — он не подменяется встроенным."""
    cfg, warnings = _sanitize(
        user_dns_profiles=[{"id": "my-profile", "name": "Мой DNS",
                            "ipv4": ["9.9.9.9"]}],
        active_dns_profile="my-profile",
    )
    assert cfg["user_dns_profiles"], "пользовательский профиль выброшен целиком"
    assert cfg["active_dns_profile"] == "my-profile", (
        "выбранный пользовательский профиль подменён встроенным"
    )
    assert not _has_warning(warnings, "active_dns_profile")
