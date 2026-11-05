"""Safe local custom-server import contracts for the Servers screen."""

from __future__ import annotations

import json

from vpn.importers import import_custom_server_document, import_custom_server_file


def _profile(profile_id: str = "home") -> dict:
    return {
        "id": profile_id,
        "name": "Домашний сервер",
        "protocol": "socks5",
        "host": "proxy.example.net",
        "port": 1080,
        "enabled": True,
        "priority": 100,
    }


def test_import_accepts_a_transport_profiles_document_without_network_activity():
    result = import_custom_server_document({"transport_profiles": [_profile()]})

    assert result.message == "Найдено серверов: 1"
    assert result.profiles == [{
        "id": "home",
        "name": "Домашний сервер",
        "protocol": "socks5",
        "host": "proxy.example.net",
        "port": 1080,
        "enabled": True,
        "priority": 100,
        "country_hint": "",
        "username": "",
        "password": "",
    }]


def test_import_accepts_a_single_profile_or_plain_profile_list():
    single = import_custom_server_document(_profile())
    listed = import_custom_server_document([_profile("second")])

    assert single.profiles[0]["id"] == "home"
    assert listed.profiles[0]["id"] == "second"


def test_import_rejects_unknown_modern_document_instead_of_guessing_a_proxy_route():
    result = import_custom_server_document({"outbounds": [{"protocol": "vless"}]})

    assert result.profiles == []
    assert "не найден" in result.message.lower()


def test_file_import_is_bounded_and_requires_json(tmp_path):
    valid = tmp_path / "server.json"
    valid.write_text(json.dumps({"profiles": [_profile()]}), encoding="utf-8")
    invalid = tmp_path / "server.txt"
    invalid.write_text("not json", encoding="utf-8")

    assert import_custom_server_file(valid).profiles[0]["id"] == "home"
    assert import_custom_server_file(invalid).profiles == []
