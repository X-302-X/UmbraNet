"""
Тесты сессии контролируемой AI-генерации — `ai_strategy/session.py`
===================================================================

Модель сессии: политика (лимиты quick/deep, порог score), план без запуска
и делегирование исполнения в общий controlled runner engine_adapter.

Важный контракт: `run()` НЕ реализует генерацию сам — он вызывает
`engine_adapter.dpi_strategy_ai_run_controlled`, чтобы UI-цепочка stop/start
и сохранение лучшего Uz не дублировались в двух местах.

Запуск: python -m pytest tests/test_ai_session.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dpi"),
           str(ROOT / "core" / "dpi" / "ai_strategy")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

from ai_strategy.session import (
    ControlledGenerationSession,
    plan_generation_session,
    session_policy,
)


def test_session_policy_quick_limits():
    policy = session_policy("quick")
    assert policy["time_limit_sec"] == 180
    assert policy["max_variants"] == 18
    # базовые поля политики остаются на месте
    assert policy["min_score_to_save"] == 70
    assert policy["requires_user_confirm"] is True
    assert policy["requires_stop"] is True
    # раздельные ключи quick/deep свёрнуты в общие
    assert "quick_time_limit_sec" not in policy
    assert "deep_max_variants" not in policy


def test_session_policy_deep_limits():
    policy = session_policy("deep")
    assert policy["time_limit_sec"] == 600
    assert policy["max_variants"] == 30


def test_plan_generation_session_shape():
    plan = plan_generation_session({}, mode="quick")
    assert plan["stage"] == "controlled_generation_plan"
    assert plan["mode"] == "quick"
    assert plan["variants_count"] >= 1
    assert len(plan["variants_preview"]) <= 8
    assert plan["will_save_only_if_score_at_least"] == 70
    # план не должен ничего запускать — только данные
    preview = plan["variants_preview"][0]
    assert {"id", "seed_id", "mutation", "mask_id", "risk", "args_count"} <= set(preview)


def test_dry_run_returns_plan_without_side_effects():
    session = ControlledGenerationSession(mode="quick")
    dry = session.dry_run()
    assert dry["stage"] == "controlled_generation_plan"
    assert dry["variants_count"] == len(session.variants) or dry["variants_count"] >= 1


def test_run_delegates_to_engine_adapter(monkeypatch):
    """run() — тонкая обёртка: вызывает общий runner с mode сессии и callback'ами."""
    import umbranet.engine_adapter as ea

    calls = {}

    def fake_runner(mode="quick", on_progress=None, should_cancel=None):
        calls["mode"] = mode
        calls["on_progress"] = on_progress
        calls["should_cancel"] = should_cancel
        return {"ok": True, "stage": "ai_generation", "mode": mode, "created_id": "uz1"}

    monkeypatch.setattr(ea, "dpi_strategy_ai_run_controlled", fake_runner)

    seen = []

    def on_progress(text):
        seen.append(text)

    session = ControlledGenerationSession(mode="deep")
    result = session.run(on_progress=on_progress, should_cancel=lambda: False)

    assert result == {"ok": True, "stage": "ai_generation", "mode": "deep", "created_id": "uz1"}
    assert calls["mode"] == "deep"
    assert calls["on_progress"] is on_progress
    # callback'и действительно работают через runner
    calls["on_progress"]("прогресс")
    assert seen == ["прогресс"]
    assert calls["should_cancel"]() is False
