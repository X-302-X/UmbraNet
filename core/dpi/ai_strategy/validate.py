"""Структурная валидация DPI-стратегии: «полностью рабочая и не поломанная».

Цель генерации (2026-10-06, постановка пользователя): выдавать ТОЛЬКО
полностью рабочие и не поломанные стратегии. Валидатор — контракт, который
проверяется ДВАЖДЫ:
  • на входе КАЖДОГО варианта — поломанный не запускается вовсе;
  • перед сохранением лучшего в strategies/ — поломанное не пишется.

Контракт взят с эталонной рабочей strategies/uz1.json:
  • секции разделяются только --new; пустых секций нет;
  • каждая секция с --filter-* имеет цель ({hostlist} или --filter-l7)
    и --dpi-desync=;
  • голосовая секция ПОЛНАЯ: --filter-l7=discord,stun + fake-discord +
    fake-stun, а в --wf-udp есть голосовые диапазоны;
  • плейсхолдеры только известные ({hostlist}, {bin}, {lists});
  • при известном bin_dir каждый {bin}\\файл существует.
"""
from __future__ import annotations

import os
import re
from typing import Any

KNOWN_PLACEHOLDERS = ("hostlist", "bin", "lists")
VOICE_FILTER = "--filter-l7=discord,stun"
VOICE_FAKE_ARGS = ("--dpi-desync-fake-discord=", "--dpi-desync-fake-stun=")
VOICE_UDP_RANGES = ("19294-19344", "50000-65535")

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


def validate_strategy_args(
    args: list[str] | None,
    bin_dir: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Проверяет структуру стратегии. Возвращает {ok, problems}."""
    problems: list[str] = []
    if not args:
        return {"ok": False, "problems": ["args пусты"]}
    if not all(isinstance(a, str) for a in args):
        return {"ok": False, "problems": ["args должен быть списком строк"]}

    # --new: не в начале/конце и не два подряд (пустых секций быть не должно).
    if args[0] == "--new":
        problems.append("--new в начале args")
    if args[-1] == "--new":
        problems.append("--new в конце args")
    for i in range(len(args) - 1):
        if args[i] == "--new" and args[i + 1] == "--new":
            problems.append("два --new подряд (пустая секция)")
            break

    sections: list[list[str]] = [[]]
    for a in args:
        if a == "--new":
            sections.append([])
        else:
            sections[-1].append(a)

    for i, sec in enumerate(sections, start=1):
        if not sec:
            problems.append(f"секция {i}: пустая")
            continue
        if not any(a.startswith("--filter-") for a in sec):
            problems.append(f"секция {i}: нет --filter-*")
            continue
        if not any(a.startswith("--dpi-desync=") for a in sec):
            problems.append(f"секция {i}: нет --dpi-desync=")
        # Цель секции — ИЛИ хосты из главного меню ({hostlist}), ИЛИ протокол
        # (--filter-l7): голосовая секция таргетится протоколом discord,stun.
        has_target = any("{hostlist}" in a for a in sec) or any(
            a.startswith("--filter-l7=") for a in sec
        )
        if not has_target:
            problems.append(f"секция {i}: нет цели ({{hostlist}} или --filter-l7)")

    # Голос: только ПОЛНАЯ секция считается. Сломанный голос = сломанная стратегия.
    voice_secs = [s for s in sections if VOICE_FILTER in s]
    if not voice_secs:
        problems.append("нет голосовой секции --filter-l7=discord,stun")
    else:
        v = voice_secs[0]
        for need in VOICE_FAKE_ARGS:
            if not any(a.startswith(need) for a in v):
                problems.append(f"голосовая секция: нет {need}…")
        wf_udp = [a for a in args if a.startswith("--wf-udp=")]
        if not any(all(r in a for r in VOICE_UDP_RANGES) for a in wf_udp):
            problems.append("в --wf-udp нет голосовых диапазонов 19294-19344,50000-65535")

    if not any(a.startswith("--wf-tcp=") for a in args):
        problems.append("нет --wf-tcp (перехват TCP)")

    # Плейсхолдеры: только известные, иначе молча не расширятся.
    for a in args:
        for m in _PLACEHOLDER_RE.finditer(a):
            if m.group(1) not in KNOWN_PLACEHOLDERS:
                problems.append(f"неизвестный плейсхолдер {{{m.group(1)}}} в «{a}»")

    # Файлы {bin}\\... должны существовать (когда известен bin_dir).
    if bin_dir:
        for a in args:
            if "{bin}" not in a or "=" not in a:
                continue
            rel = a.split("=", 1)[1].replace("{bin}", "")
            rel = rel.replace("\\\\", "\\").lstrip("\\/")
            if not rel or not os.path.isfile(os.path.join(str(bin_dir), rel)):
                problems.append(f"файл {{bin}} не найден: {rel or a}")

    return {"ok": not problems, "problems": problems}
