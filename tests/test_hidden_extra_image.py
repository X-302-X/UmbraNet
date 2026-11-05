"""Скрытый ресурс пасхалки — статичная открытка вместо проигрываемого видео."""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PAYLOAD = ROOT / "umbranet" / "widgets" / "_gw.bin"
_EXPECTED_SHA256 = "a82b70137c783e459ab2a0cdabae487858cc73a6385f8cba920bf698d2cd0555"


def _unfold(data: bytes) -> bytes:
    key = bytes((0xA5 ^ ((i * 17 + 31) & 0xFF)) for i in range(32))
    return bytes(b ^ key[i % len(key)] ^ ((i * 13) & 0xFF) for i, b in enumerate(data))


def test_hidden_payload_is_the_attached_jpeg_and_not_plaintext():
    stored = _PAYLOAD.read_bytes()
    assert not stored.startswith(b"\xff\xd8\xff"), "JPEG не должен лежать в ресурсах открытым файлом"

    image = _unfold(stored)
    assert image.startswith(b"\xff\xd8\xff"), "после раскрытия должен получиться JPEG"
    assert hashlib.sha256(image).hexdigest() == _EXPECTED_SHA256


def test_secret_view_no_longer_imports_or_materializes_video():
    for name in ("extra.py", "kus.py"):
        source = (ROOT / "umbranet" / "views" / name).read_text(encoding="utf-8")
        for obsolete in ("QMediaPlayer", "QVideoWidget", "clip.mp4", "tempfile.mkstemp"):
            assert obsolete not in source, f"{name} всё ещё содержит видеокод: {obsolete}"
