"""
Controlled AI generation session model.

Модель контролируемой генерации: политику (лимиты времени/вариантов, порог
score) и план (`plan_generation_session`) считает этот модуль; исполнение
делегирует в общий controlled runner (`umbranet/engine_adapter.py` →
`dpi_strategy_ai_run_controlled`).

Полный цикл сессии:
  1. запросить подтверждение пользователя в UI (цепочка UI);
  2. остановить UmbraNet так же, как кнопка Stop (цепочка UI);
  3. тестировать временные варианты из mutations.py в изоляции (runner);
  4. выбрать лучший через scoring.py (runner);
  5. сохранить только финальную Uz, если score достаточный (runner);
  6. восстановить предыдущее состояние (dpi_strategy_ai_cleanup_runtime).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .mutations import generate_variants


@dataclass
class SessionPolicy:
    requires_user_confirm: bool = True
    requires_stop: bool = True
    restore_previous_state: bool = True
    test_mode: str = "combo"
    isolation: str = "stop_start_each_variant"
    min_score_to_save: int = 70
    quick_time_limit_sec: int = 180
    deep_time_limit_sec: int = 600
    quick_max_variants: int = 18
    deep_max_variants: int = 30
    per_variant_timeout_sec: int = 35


def session_policy(mode: str = "quick") -> dict[str, Any]:
    policy = asdict(SessionPolicy())
    deep = mode == "deep"
    data = dict(policy)
    data["time_limit_sec"] = data["deep_time_limit_sec"] if deep else data["quick_time_limit_sec"]
    data["max_variants"] = data["deep_max_variants"] if deep else data["quick_max_variants"]
    data.pop("quick_time_limit_sec", None)
    data.pop("deep_time_limit_sec", None)
    data.pop("quick_max_variants", None)
    data.pop("deep_max_variants", None)
    return data


def plan_generation_session(config: dict[str, Any] | None = None,
                            mode: str = "quick") -> dict[str, Any]:
    """План контролируемой генерации без фактического запуска."""
    policy = session_policy(mode)
    max_variants = int(policy.get("max_variants", 12) or 12)
    variants = generate_variants(mode=mode, max_variants=max_variants)
    return {
        "stage": "controlled_generation_plan",
        "mode": mode,
        "policy": policy,
        "variants_count": len(variants),
        "variants_preview": [
            {
                "id": v.get("id"),
                "seed_id": v.get("seed_id"),
                "mutation": v.get("mutation"),
                "mask_id": v.get("mask_id"),
                "risk": v.get("risk"),
                "args_count": v.get("args_count"),
            }
            for v in variants[:8]
        ],
        "will_save_only_if_score_at_least": policy.get("min_score_to_save", 70),
    }


class ControlledGenerationSession:
    """Сессия контролируемой генерации: политика + делегирование runner.

    `dry_run()` возвращает план, ничего не запуская. `run()` исполняет генерацию
    через общий controlled runner engine_adapter: winws.exe гоняется на
    временных вариантах, пробы YouTube/Discord считаются через scoring, лучший
    Uz сохраняется при score ≥ порога. Политика (включая `min_score_to_save`)
    внутри runner берётся из `session_policy(self.mode)` — тот же расчёт,
    что показывает `dry_run()`.

    Контракт как у runner: основной UmbraNet должен быть остановлен вызывающей
    цепочкой (UI делает это до вызова), иначе временные варианты будут мешать
    боевому движку.
    """

    def __init__(self, config: dict[str, Any] | None = None, mode: str = "quick"):
        self.config = config or {}
        self.mode = mode
        self.policy = session_policy(mode)
        self.variants = generate_variants(mode=mode, max_variants=int(self.policy.get("max_variants", 12)))

    def dry_run(self) -> dict[str, Any]:
        return plan_generation_session(self.config, self.mode)

    def run(self, on_progress=None, should_cancel=None) -> dict[str, Any]:
        """Исполняет контролируемую генерацию через общий runner.

        Локальный импорт: на этапе импорта модуля core не зависит от слоя UI.
        Прогресс и отмена — те же callback'и, что у
        `engine_adapter.dpi_strategy_ai_run_controlled`.
        """
        try:
            from umbranet.engine_adapter import dpi_strategy_ai_run_controlled
        except ImportError as exc:
            raise RuntimeError(
                "ControlledGenerationSession.run требует слой UI (umbranet.engine_adapter): "
                "запускайте из состава UmbraNet или вызывайте dry_run()."
            ) from exc

        return dpi_strategy_ai_run_controlled(
            mode=self.mode,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
