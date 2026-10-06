"""
Тесты аудита после остановки — `engine_adapter.verify_teardown`
================================================================

Баг, который закрывают эти тесты: после «Стоп» обход мог продолжать работать
(живой winws.exe или захваченный системный DNS), а диагностика молчала — ей
было нечего проверять. verify_teardown отвечает на вопросы «не осталось ли
хвостов» и докладывает проблемы, ничего не чиня сам.

Запуск: python -m pytest tests/test_teardown_audit.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dpi"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))


class _FakeWinWS:
    def __init__(self, own=None, foreign=None):
        self._own = own or []
        self._foreign = foreign

    def scan_own_processes(self, keep_pid=None):
        return list(self._own), True

    def foreign_processes(self):
        return None if self._foreign is None else list(self._foreign)


def _run_audit(monkeypatch, *, winws, dns_settings):
    import winws_engine
    import umbranet.engine_adapter as ea

    monkeypatch.setattr(winws_engine, "get_winws_engine", lambda: winws)
    monkeypatch.setattr(ea, "get_current_dns_settings", lambda use_cache=True: dns_settings)
    return ea.verify_teardown()


def test_audit_ok_when_everything_torn_down(monkeypatch):
    """Чистая остановка: процессов нет, DNS вернулся — проблем нет."""
    report = _run_audit(
        monkeypatch,
        winws=_FakeWinWS(own=[]),
        dns_settings={"Ethernet": {"ipv4": ["1.1.1.1"], "ipv6": []}},
    )
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["winws_own"] == []
    assert report["dns_localhost_adapters"] == []


def test_audit_flags_own_winws_leftover(monkeypatch):
    """ГЛАВНОЕ: живой winws.exe после остановки — это и есть «обход не выключился»."""
    report = _run_audit(
        monkeypatch,
        winws=_FakeWinWS(own=[(777, "C:/UmbraNet/bin/winws.exe")]),
        dns_settings={"Ethernet": {"ipv4": ["1.1.1.1"], "ipv6": []}},
    )
    assert report["ok"] is False
    assert any("PID 777" in p for p in report["problems"]), report["problems"]
    assert report["winws_own"][0]["pid"] == 777


def test_audit_flags_dns_still_localhost(monkeypatch):
    """Системный DNS всё ещё наш — резолв уходит на fallback, сайты «живут»."""
    report = _run_audit(
        monkeypatch,
        winws=_FakeWinWS(own=[]),
        dns_settings={"Ethernet": {"ipv4": ["127.0.0.1", "8.8.8.8"], "ipv6": []}},
    )
    assert report["ok"] is False
    assert report["dns_localhost_adapters"] == ["Ethernet"]
    assert any("системный DNS" in p for p in report["problems"]), report["problems"]


def test_audit_notes_foreign_winws_without_flagging(monkeypatch):
    """Чужие winws.exe — только заметка: это не наши хвосты, мы их не трогаем."""
    report = _run_audit(
        monkeypatch,
        winws=_FakeWinWS(own=[], foreign=[(4242, "C:/OtherApp/bin/winws.exe")]),
        dns_settings={"Ethernet": {"ipv4": ["1.1.1.1"], "ipv6": []}},
    )
    assert report["ok"] is True
    assert report["problems"] == []
    assert any("чужие" in n for n in report["notes"]), report["notes"]
    assert report["winws_foreign"][0]["pid"] == 4242


def test_audit_survives_probe_failures(monkeypatch):
    """Сбой проверок не должен ронять аудит — он сам часть диагностики."""
    class _BrokenWinWS:
        def scan_own_processes(self, keep_pid=None):
            raise RuntimeError("нет прав")

        def foreign_processes(self):
            raise RuntimeError("нет прав")

    import umbranet.engine_adapter as ea
    import winws_engine

    monkeypatch.setattr(winws_engine, "get_winws_engine", lambda: _BrokenWinWS())
    monkeypatch.setattr(ea, "get_current_dns_settings", lambda use_cache=True: (_ for _ in ()).throw(OSError("ps down")))

    report = ea.verify_teardown()
    assert "ok" in report and "problems" in report, "аудит обязан вернуть структуру даже при сбоях"
