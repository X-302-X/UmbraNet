"""Паритет генерации стратегий (uz) и рабочей Uz1 (2026-10-06).

Жалоба пользователя: «мы изменили Uz1, но не меняли саму генерацию стратегий».
Так и было: seed balanced отставал от strategies/uz1.json (без голосовой секции,
без fake-http, wf-udp без голосовых портов) — сгенерированная стратегия ломала
голос Дискорда. Эти тесты не дают форматам снова разъехаться.

Правила:
  1. seed balanced_fake_multisplit — ТОЧНАЯ копия args из strategies/uz1.json;
  2. каждый полный seed несёт голосовую секцию (--filter-l7=discord,stun);
  3. ни одна мутация, включая tcp_only, не убирает голос.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dpi"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

VOICE_MARKERS = ("--filter-l7=discord,stun", "--dpi-desync-fake-discord")


def _uz1_args() -> list[str]:
    data = json.loads((ROOT / "strategies" / "uz1.json").read_text(encoding="utf-8"))
    return list(data.get("args") or [])


def test_balanced_seed_matches_uz1_json_exactly():
    """Seed balanced — зеркало uz1.json. Любое изменение Uz1 = осознанное
    обновление seed (или тест покажет, что генерация отстала)."""
    from ai_strategy.seeds import get_seed

    assert get_seed("balanced_fake_multisplit")["args"] == _uz1_args()


def test_every_seed_keeps_voice():
    """Все seed'ы несут голосовую секцию и голосовые порты в --wf-udp."""
    from ai_strategy.seeds import SEEDS

    for seed in SEEDS:
        args = seed.get("args") or []
        for marker in VOICE_MARKERS:
            assert any(marker in a for a in args), f"{seed.get('id')}: нет {marker}"
        assert any(
            a.startswith("--wf-udp=") and "50000-65535" in a for a in args
        ), f"{seed.get('id')}: в --wf-udp нет голосовых портов"


def test_all_generated_variants_keep_voice():
    """Во всех вариантах генерации (quick и deep) голос остаётся целым."""
    from ai_strategy.mutations import generate_variants

    for mode in ("quick", "deep"):
        variants = generate_variants(mode=mode, max_variants=50)
        assert variants, f"{mode}: варианты не сгенерировались"
        for v in variants:
            args = v.get("args") or []
            for marker in VOICE_MARKERS:
                assert any(marker in a for a in args), (
                    f"{mode}/{v.get('id')}: мутация убила голос (нет {marker})"
                )


def test_tcp_only_keeps_voice_and_wf():
    """Мутация tcp_only вырезает UDP/QUIC-443, но НЕ голос."""
    from ai_strategy.mutations import _remove_udp_blocks

    out = _remove_udp_blocks(_uz1_args())
    for marker in VOICE_MARKERS:
        assert any(marker in a for a in out), f"tcp_only убила голос (нет {marker})"
    assert any(
        a.startswith("--wf-udp=") and "50000-65535" in a for a in out
    ), "tcp_only убрала голосовые порты из --wf-udp"
    assert not any(a.startswith("--filter-udp=443") for a in out), (
        "udp/QUIC-443 блок должен быть вырезан"
    )
    assert any(a.startswith("--filter-tcp=443") for a in out), "TCP-блок должен остаться"
