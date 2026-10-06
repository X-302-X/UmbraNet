"""Обязательные пробы YouTube: музыка и медиа (2026-10-06, поле).

Пользователь: «YT Music грузится не полностью и треки не грузятся», хотя
www.youtube.com работал. Стратегия, ломающая музыку/треки, НЕ должна
сохраняться — это проверяют пробы.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dpi"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))


def _fake_https(monkeypatch, broken_hosts: set[str]):
    import ai_strategy.probes as probes

    def fake(host, path="/", method="GET", timeout=6.0, **kw):
        return {"name": "https", "ok": host not in broken_hosts, "host": host, "path": path}

    monkeypatch.setattr(probes, "https_probe", fake)


def test_music_failure_rejects_strategy(monkeypatch):
    import ai_strategy.probes as probes

    _fake_https(monkeypatch, {"music.youtube.com"})
    r = probes.probe_youtube_basic()
    assert r["required"]["music"] is False
    assert r["required"]["media"] is True
    assert r["ok"] is False, "стратегия без музыки не должна проходить"


def test_media_failure_rejects_strategy(monkeypatch):
    import ai_strategy.probes as probes

    _fake_https(monkeypatch, {"redirector.googlevideo.com"})
    r = probes.probe_youtube_basic()
    assert r["required"]["media"] is False
    assert r["ok"] is False, "стратегия без треков (googlevideo) не должна проходить"


def test_all_green_when_music_and_media_work(monkeypatch):
    import ai_strategy.probes as probes

    _fake_https(monkeypatch, set())
    r = probes.probe_youtube_basic()
    assert r["required"] == {"music": True, "media": True}
    assert r["ok"] is True
    assert r["score"] == 100
