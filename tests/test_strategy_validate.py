"""«Генерация выдаёт только полностью рабочие и не поломанные стратегии» (2026-10-06).

Три рубежа:
  1. validate_strategy_args — структурный контракт (секции, цели, голос, bin);
  2. раннер генерации — поломанный вариант не запускается;
  3. сохранение — поломанная стратегия не пишется в strategies/.
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


def _uz1_args() -> list[str]:
    data = json.loads((ROOT / "strategies" / "uz1.json").read_text(encoding="utf-8"))
    return list(data.get("args") or [])


def _valid() -> list[str]:
    return list(_uz1_args())


# ---------- 1. Валидатор ----------

def test_uz1_passes_validation():
    from ai_strategy.validate import validate_strategy_args

    r = validate_strategy_args(_valid())
    assert r["ok"], r["problems"]


def test_uz1_passes_with_bin_dir():
    from ai_strategy.validate import validate_strategy_args

    r = validate_strategy_args(_valid(), bin_dir=ROOT / "bin")
    assert r["ok"], r["problems"]


def test_missing_voice_fails():
    from ai_strategy.validate import validate_strategy_args

    args = [a for a in _valid() if "discord" not in a and "stun" not in a.lower()]
    r = validate_strategy_args(args)
    assert not r["ok"]
    assert any("голос" in p.lower() or "filter-l7" in p for p in r["problems"])


def test_broken_voice_fails():
    from ai_strategy.validate import validate_strategy_args

    args = [a.replace("--dpi-desync-fake-discord=", "--dpi-desync-fake-x=") for a in _valid()]
    r = validate_strategy_args(args)
    assert not r["ok"]
    assert any("fake-discord" in p for p in r["problems"])


def test_unknown_placeholder_fails():
    from ai_strategy.validate import validate_strategy_args

    args = [a.replace("{hostlist}", "{hostlst}") for a in _valid()]
    r = validate_strategy_args(args)
    assert not r["ok"]
    assert any("плейсхолдер" in p for p in r["problems"])


def test_empty_section_fails():
    from ai_strategy.validate import validate_strategy_args

    args = _valid()
    i = args.index("--new")
    args.insert(i + 1, "--new")
    r = validate_strategy_args(args)
    assert not r["ok"]


def test_missing_bin_file_fails(tmp_path):
    from ai_strategy.validate import validate_strategy_args

    r = validate_strategy_args(_valid(), bin_dir=tmp_path)
    assert not r["ok"]
    assert any("не найден" in p for p in r["problems"])


def test_all_generated_variants_pass_validation():
    """Ни один вариант генерации не является поломанным."""
    from ai_strategy.mutations import generate_variants
    from ai_strategy.validate import validate_strategy_args

    n = 0
    for mode in ("quick", "deep"):
        for v in generate_variants(mode=mode, max_variants=50):
            n += 1
            r = validate_strategy_args(v.get("args"))
            assert r["ok"], f"{mode}/{v.get('id')}: {r['problems']}"
    assert n > 0


# ---------- 3. Ворота сохранения ----------

class _StubManager:
    def __init__(self, strategies_dir: pathlib.Path):
        self.strategies_dir = strategies_dir

    def list_strategies(self, enabled_only: bool = True):
        return []


def test_save_gate_refuses_broken_strategy(tmp_path, monkeypatch):
    import umbranet.engine_adapter as ea

    monkeypatch.setattr(ea, "_dpi_get_strategy_manager", lambda: _StubManager(tmp_path))
    broken = {"id": "x", "args": [a for a in _valid() if "discord" not in a], "seed_id": "s"}
    ok, msg, sid = ea._dpi_write_ai_strategy_from_variant(broken, {"score": 99}, ["d.example"])
    assert ok is False
    assert "поломана" in msg
    assert sid == ""
    assert not list(tmp_path.glob("uz*.json")), "поломанная стратегия не должна писаться"


def _stub_layout(tmp_path: pathlib.Path) -> pathlib.Path:
    """Как в продукте: strategies/ и bin/ — соседи."""
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in (
        "tls_clienthello_www_google_com.bin",
        "quic_initial_www_google_com.bin",
        "tls_clienthello_max_ru.bin",
        "stun.bin",
    ):
        (bindir / name).write_bytes(b"")
    return strategies


def test_save_gate_accepts_valid_strategy(tmp_path, monkeypatch):
    import umbranet.engine_adapter as ea

    strategies = _stub_layout(tmp_path)
    monkeypatch.setattr(ea, "_dpi_get_strategy_manager", lambda: _StubManager(strategies))
    good = {"id": "y", "args": _valid(), "seed_id": "balanced_fake_multisplit"}
    ok, msg, sid = ea._dpi_write_ai_strategy_from_variant(good, {"score": 90}, ["d.example"])
    assert ok is True, msg
    assert sid.startswith("uz")
    written = strategies / f"{sid}.json"
    assert written.is_file()
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["args"] == _valid()
