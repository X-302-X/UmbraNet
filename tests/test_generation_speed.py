"""
Тесты скорости и «понятности» AI-генерации стратегий.

Закрывают две жалобы по факту прогона на Windows:

  1. «Генерация стала ОЧЕНЬ медленной, делает по 5 минут».
     Проверки сайтов шли ПОСЛЕДОВАТЕЛЬНО: 11 проверок × до 5 секунд ожидания =
     до минуты на один вариант, а вариантов 18. Теперь проверки идут
     параллельно (и внутри сервиса, и YouTube с Discord одновременно), а порядок
     результатов сохраняется — значит, и score, и отчёт не меняются.

  2. «Почти всегда выдаёт ошибку, мол не удалось сделать».
     Когда системный DNS не отвечает (например он остался на 127.0.0.1, а
     локальный DNS-сервер на время генерации остановлен), ВСЕ проверки умирают
     на первом шаге. Стратегия тут ни при чём, а пользователь видел «стратегия
     не найдена». Теперь перед прогоном идёт предполётная проверка, а если имена
     перестали разрешаться во время прогона — генерация останавливается сразу с
     объяснением, что делать.

Запуск: python -m pytest tests/test_generation_speed.py
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dpi"),
          str(ROOT / "core" / "dpi" / "ai_strategy"), str(ROOT / "umbranet")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ai_strategy.probes as probes  # noqa: E402


def fake_check(host: str, ok: bool = True, stage: str = "http", delay: float = 0.0):
    """Правдоподобный ответ проверки — по форме как у https_probe."""
    if delay:
        time.sleep(delay)
    return {
        "name": "https", "ok": ok, "host": host, "path": "/", "method": "HEAD",
        "status": 200 if ok else 0, "stage": stage, "ms": round(delay * 1000, 1),
    }


# ── 1. Проверки идут параллельно ───────────────────────────────────────────

def test_youtube_checks_run_in_parallel(monkeypatch):
    """6 проверок YouTube × 0.4 с: последовательно 2.4 с, параллельно ~0.4 с.

    Список вырос 2026-10-06: добавлены обязательные музыка и googlevideo."""
    calls: list[str] = []

    def slow_probe(host, *args, **kwargs):
        calls.append(host)
        return fake_check(host, delay=0.4)

    monkeypatch.setattr(probes, "https_probe", slow_probe)
    started = time.monotonic()
    result = probes.probe_youtube_basic(timeout=1.0)
    elapsed = time.monotonic() - started

    assert len(calls) == 6, f"список проверок изменился: {calls}"
    assert elapsed < 1.0, (
        f"проверки YouTube шли последовательно: {elapsed:.2f}с вместо ~0.4с — "
        f"это и растягивало генерацию на минуты"
    )
    assert result["ok"] is True and result["score"] == 100
    assert result.get("parallel") is True


def test_discord_checks_run_in_parallel_and_keep_order(monkeypatch):
    """6 проверок Discord: параллельно, и обязательные — на своих местах.

    gateway WS и voice/regions определяют вердикт по звонкам, они берутся по
    индексу. Если порядок поедет, стратегия будет оценена неправильно.
    """
    hosts: list[str] = []

    def slow_https(host, *args, **kwargs):
        hosts.append(host)
        return fake_check(host, delay=0.4)

    def slow_ws(host, *args, **kwargs):
        hosts.append(host)
        return fake_check(host, delay=0.4)

    monkeypatch.setattr(probes, "https_probe", slow_https)
    monkeypatch.setattr(probes, "websocket_hello_probe", slow_ws)
    started = time.monotonic()
    result = probes.probe_discord_basic(timeout=1.0)
    elapsed = time.monotonic() - started

    # Порог 2.0 с, а не 1.0: последовательный прогон — 6 × 0.4 = 2.4 с, а
    # параллельный на медленном CI-раннере (общая ВМ, холодные потоки)
    # занимает до ~1.1 с. 2.0 отделяет «параллельно» от «последовательно»
    # с запасом по обе стороны.
    assert elapsed < 2.0, f"проверки Discord шли последовательно: {elapsed:.2f}с"
    assert result["checks"][1]["host"] == "discord.com", "voice/regions не на своём месте"
    assert result["checks"][5]["host"] == "gateway.discord.gg", "gateway WS не на своём месте"
    assert result["required"] == {
        "gateway_ws": True, "voice_regions": True, "cdn_avatar": True,
    }
    assert result["ok"] is True


def test_discord_fails_without_cdn_avatar(monkeypatch):
    """Стратегия без живых аватарок Discord не должна считаться успешной."""

    def https(host, *args, **kwargs):
        return fake_check(host, ok=(host != "cdn.discordapp.com"))

    def ws(host, *args, **kwargs):
        return fake_check(host, ok=True)

    monkeypatch.setattr(probes, "https_probe", https)
    monkeypatch.setattr(probes, "websocket_hello_probe", ws)
    result = probes.probe_discord_basic(timeout=1.0)
    assert result["required"]["cdn_avatar"] is False
    assert result["required"]["gateway_ws"] is True
    assert result["ok"] is False


def test_services_run_in_parallel(monkeypatch):
    """YouTube и Discord проверяются одновременно: 2 × 0.5 с → меньше 0.9 с."""
    def slow_youtube(timeout=6.0):
        time.sleep(0.5)
        return {"service": "youtube", "ok": True, "score": 100, "checks": []}

    def slow_discord(timeout=6.0):
        time.sleep(0.5)
        return {"service": "discord", "ok": True, "score": 100, "checks": []}

    monkeypatch.setattr(probes, "probe_youtube_basic", slow_youtube)
    monkeypatch.setattr(probes, "probe_discord_basic", slow_discord)
    started = time.monotonic()
    result = probes.run_basic_probes(timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.9, f"сервисы проверялись последовательно: {elapsed:.2f}с"
    assert result["ok"] is True and result["score"] == 100
    assert [s["service"] for s in result["services"]] == ["youtube", "discord"]


# ── 2. Результат и оценка не изменились ────────────────────────────────────

def test_probe_result_shape_unchanged(monkeypatch):
    """Параллельность не должна менять структуру ответа, из которой считается score."""
    answers = {
        "www.youtube.com": True,
        "music.youtube.com": True,
        "youtubei.googleapis.com": True,
        "i.ytimg.com": True,
        "redirector.googlevideo.com": False,
    }
    monkeypatch.setattr(probes, "https_probe",
                        lambda host, *a, **kw: fake_check(host, ok=answers.get(host, False)))
    youtube = probes.probe_youtube_basic(timeout=1.0)

    for key in ("service", "level", "ok", "score", "checks", "required"):
        assert key in youtube, f"пропало поле {key}"
    # Контракт обновлён 2026-10-06: googlevideo (треки) — обязательная проба,
    # сломанные треки больше не проходят «в зачёт».
    assert youtube["score"] == 67, "оценка YouTube считается иначе, чем раньше"
    assert youtube["ok"] is False, "сломанный googlevideo (треки) — не зачёт"
    assert youtube["required"]["music"] is True
    assert youtube["required"]["media"] is False


def test_scoring_ignores_new_probe_fields(monkeypatch):
    """Добавленные поля (parallel/ms) не влияют на итоговый score варианта."""
    from ai_strategy.scoring import score_variant

    def variant():
        return {"id": "v1", "seed_id": "s", "mutation": "m", "mask_id": "k",
                "risk": "medium", "save_priority": 50}

    def probe_result(with_new_fields: bool):
        yt = {"service": "youtube", "ok": True, "score": 80, "checks": []}
        dc = {"service": "discord", "ok": True, "score": 60, "checks": []}
        if with_new_fields:
            yt.update({"parallel": True, "ms": 512.0})
            dc.update({"parallel": True, "ms": 700.0})
        return {"stage": "basic_probes", "services": [yt, dc]}

    old = score_variant(variant(), probe_result(False))
    new = score_variant(variant(), probe_result(True))
    assert old["score"] == new["score"], "score поехал из-за новых полей"
    assert old["service_scores"] == new["service_scores"]


# ── 3. «Не разрешаются имена» → понятная остановка, а не пустой прогон ─────

def test_all_dns_failed_detection():
    """Признак «всё умерло на DNS» распознаётся, а частичные провалы — нет."""
    import engine_adapter as ea

    def probe(stages):
        return {"services": [
            {"checks": [{"ok": False, "stage": s} for s in stages]},
        ]}

    assert ea._dpi_probe_all_dns_failed(probe(["dns", "dns", "dns"])) is True
    assert ea._dpi_probe_all_dns_failed(probe(["dns", "http_status"])) is False
    assert ea._dpi_probe_all_dns_failed(probe(["http_status", "http"])) is False
    assert ea._dpi_probe_all_dns_failed({}) is False
    assert ea._dpi_probe_all_dns_failed({"services": []}) is False
    # Хотя бы одна удачная проверка — сеть жива, прогон продолжаем.
    assert ea._dpi_probe_all_dns_failed(
        {"services": [{"checks": [{"ok": False, "stage": "dns"}, {"ok": True, "stage": "http"}]}]}
    ) is False


def test_preflight_aborts_when_dns_is_dead(monkeypatch):
    """Главное: не тратить минуты, если имена не разрешаются вообще."""
    import engine_adapter as ea
    import ai_strategy.probes as p

    monkeypatch.setattr(p, "resolve_probe",
                        lambda host, timeout=None: {"ok": False, "error": "нет ответа"})
    monkeypatch.setattr(ea, "network_restore_latest", lambda: (False, "не вышло"))
    monkeypatch.setattr(ea, "_detect_dns_conflict_processes", lambda: [])

    winws = _FakeWinWS()
    notes: list[str] = []
    result = ea._dpi_generation_preflight(winws, notes.append)

    assert result["abort"] is True, "генерация должна быть отменена, а не идти впустую"
    reason = result["reason"]
    assert "резолвит" in reason or "разрешения имён" in reason
    assert "Откатить сеть" in reason, "в сообщении должно быть, что делать пользователю"
    # Отмена тоже уходит в интерфейс: пользователь видит причину, а не тишину.
    assert any("DNS" in n for n in notes)


def test_preflight_recovers_dns_from_snapshot(monkeypatch):
    """Если DNS удалось вернуть из снапшота — прогон продолжается с пометкой."""
    import engine_adapter as ea
    import ai_strategy.probes as p

    state = {"answered": False}

    def resolve(host, timeout=None):
        return {"ok": state["answered"], "error": "нет ответа"}

    def restore():
        state["answered"] = True
        return True, "восстановлен снапшот"

    monkeypatch.setattr(p, "resolve_probe", resolve)
    monkeypatch.setattr(ea, "network_restore_latest", restore)
    monkeypatch.setattr(ea, "_detect_dns_conflict_processes", lambda: [])

    notes: list[str] = []
    result = ea._dpi_generation_preflight(_FakeWinWS(), notes.append)

    assert result["abort"] is False
    assert any("снапшот" in n for n in notes)


def test_preflight_warns_about_other_winws(monkeypatch):
    """Похожая программа рядом — предупреждаем словами про WinDivert."""
    import engine_adapter as ea
    import ai_strategy.probes as p

    monkeypatch.setattr(p, "resolve_probe", lambda host, timeout=None: {"ok": True})
    monkeypatch.setattr(ea, "_detect_dns_conflict_processes", lambda: [])
    monkeypatch.setattr(ea, "network_restore_latest", lambda: (True, "ок"))

    notes: list[str] = []
    result = ea._dpi_generation_preflight(
        _FakeWinWS(foreign=[(777, "C:/OtherApp/bin/winws.exe")]), notes.append
    )

    assert result["abort"] is False
    assert any("WinDivert" in n and "777" in n for n in notes), (
        f"нет понятного предупреждения о конфликте: {notes}"
    )


def test_preflight_warns_about_busy_port_53(monkeypatch):
    import engine_adapter as ea
    import ai_strategy.probes as p

    monkeypatch.setattr(p, "resolve_probe", lambda host, timeout=None: {"ok": True})
    monkeypatch.setattr(ea, "_detect_dns_conflict_processes", lambda: ["dnsproxy.exe"])
    monkeypatch.setattr(ea, "network_restore_latest", lambda: (True, "ок"))

    notes: list[str] = []
    ea._dpi_generation_preflight(_FakeWinWS(), notes.append)
    assert any("порт 53" in n for n in notes)


class _FakeWinWS:
    """Минимальная заглушка движка для предполётных проверок."""

    def __init__(self, foreign=None):
        self._foreign = foreign or []

    def foreign_processes(self):
        return list(self._foreign)
