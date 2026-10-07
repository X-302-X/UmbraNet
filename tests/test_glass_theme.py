"""Контракт палитры темы «Ледяное стекло».

Проверяем итоговый контраст после композиции полупрозрачных поверхностей,
а не только пары исходных hex-цветов.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THEME_PATH = ROOT / "themes" / "glass.json"


def _rgba(value: str) -> tuple[float, float, float, float]:
    if value.startswith("#"):
        hex_value = value[1:]
        if len(hex_value) == 3:
            hex_value = "".join(ch * 2 for ch in hex_value)
        return tuple(int(hex_value[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (1.0,)
    match = re.fullmatch(
        r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)",
        value,
    )
    if not match:
        raise ValueError(f"Неподдерживаемый цвет темы: {value!r}")
    return tuple(int(match[i]) / 255 for i in (1, 2, 3)) + (float(match[4]),)


def _over(foreground, background):
    r, g, b, alpha = foreground
    return (
        r * alpha + background[0] * (1 - alpha),
        g * alpha + background[1] * (1 - alpha),
        b * alpha + background[2] * (1 - alpha),
        1.0,
    )


def _luminance(color) -> float:
    channels = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in color[:3]
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(first, second) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _theme() -> dict:
    return json.loads(THEME_PATH.read_text(encoding="utf-8"))


def test_glass_theme_has_complete_palette():
    palette = _theme()
    required = {
        "label", "BG", "SIDEBAR", "CARD", "CARD_TOP", "CARD_DARK", "ROW_BG",
        "BORDER", "INPUT_BG", "ACCENT", "ACCENT2", "ACCENT3", "GREEN", "RED",
        "YELLOW", "ORANGE", "PINK", "TEXT", "SUBTEXT", "MUTED", "WHITE",
    }
    assert required <= palette.keys()
    assert palette["label"] == "Ледяное стекло"


def test_glass_text_and_status_colors_meet_aa_on_surfaces():
    palette = _theme()
    base = _rgba(palette["BG"])
    surfaces = ("BG", "CARD", "CARD_DARK", "INPUT_BG", "SIDEBAR")
    foregrounds = (
        "TEXT", "SUBTEXT", "MUTED", "ACCENT", "ACCENT2", "ACCENT3",
        "GREEN", "RED", "YELLOW", "ORANGE", "PINK",
    )

    for surface_name in surfaces:
        surface = _over(_rgba(palette[surface_name]), base)
        for foreground_name in foregrounds:
            ratio = _contrast(_rgba(palette[foreground_name]), surface)
            assert ratio >= 4.5, (
                f"{foreground_name} на {surface_name}: {ratio:.2f}:1 "
                "(нужно не менее 4.5:1 для обычного текста)"
            )


def test_glass_accent_buttons_support_white_labels():
    palette = _theme()
    white = _rgba(palette["WHITE"])
    for token in (
        "ACCENT", "ACCENT2", "ACCENT3", "GREEN", "RED", "YELLOW", "ORANGE", "PINK",
    ):
        ratio = _contrast(white, _rgba(palette[token]))
        assert ratio >= 4.5, f"Белая подпись на {token}: {ratio:.2f}:1"
