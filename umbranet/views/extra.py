"""
UmbraNet — дополнительная медиа-вкладка.
"""

from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QTimer, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.widgets.rounded_panel import RoundedPanel

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
except Exception:
    QAudioOutput = None  # type: ignore[misc, assignment]
    QMediaPlayer = None  # type: ignore[misc, assignment]
    QVideoWidget = None  # type: ignore[misc, assignment]

# Ролик 1278×942 — держим открыткой, а не на весь экран.
_VIDEO_W = 460
_VIDEO_H = 340


def _unfold(data: bytes) -> bytes:
    key = bytes((0xA5 ^ ((i * 17 + 31) & 0xFF)) for i in range(32))
    klen = len(key)
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key[i % klen] ^ ((i * 13) & 0xFF)
    return bytes(out)


def _payload_bytes() -> bytes | None:
    path = Path(__file__).resolve().parent.parent / "widgets" / "_gw.bin"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < 32:
        return None
    return _unfold(raw)


class _Stage(QVideoWidget if QVideoWidget is not None else QWidget):
    tapped = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.tapped.emit()
        super().mousePressEvent(event)


# Котики вокруг открытки.
# glyph, px, якорь, сторона, t вдоль края (0..1), отступ наружу, джиттер x/y.
# Низ видео и верх текста не трогаем — там слишком узко, наезжали бы.
_CATS = (
    ("😺", 30, "video",  "top",    0.04, 28,  -8,  6),
    ("😸", 22, "video",  "top",    0.20, 20,  10, -4),
    ("😹", 34, "video",  "top",    0.36, 36,  -6,  8),
    ("😻", 24, "video",  "top",    0.52, 18,  12, -2),
    ("😼", 28, "video",  "top",    0.68, 32,  -4,  5),
    ("😽", 20, "video",  "top",    0.84, 22,   8, -6),
    ("🙀", 26, "video",  "top",    0.97, 30, -10,  3),
    ("😺", 32, "video",  "left",   0.06, 26,   4, -8),
    ("😸", 24, "video",  "left",   0.24, 34,  -6,  6),
    ("😹", 36, "video",  "left",   0.44, 22,   8, -4),
    ("😻", 21, "video",  "left",   0.62, 30,  -4, 10),
    ("😼", 29, "video",  "left",   0.80, 18,   6, -6),
    ("😽", 25, "video",  "left",   0.94, 28,  -8,  4),
    ("🙀", 23, "video",  "right",  0.08, 24,   6,  8),
    ("😺", 34, "video",  "right",  0.26, 32,  -4, -6),
    ("😸", 20, "video",  "right",  0.44, 18,  10,  4),
    ("😹", 28, "video",  "right",  0.62, 36,  -8, -2),
    ("😻", 31, "video",  "right",  0.80, 22,   5,  7),
    ("😼", 22, "video",  "right",  0.95, 28,  -6, -8),
    ("😽", 27, "thanks", "left",   0.12, 30,  -5,  6),
    ("🙀", 33, "thanks", "left",   0.38, 20,   8, -8),
    ("😺", 21, "thanks", "left",   0.64, 34,  -6,  4),
    ("😸", 29, "thanks", "left",   0.88, 24,   4, -5),
    ("😹", 24, "thanks", "right",  0.10, 28,   7, -4),
    ("😻", 32, "thanks", "right",  0.36, 20,  -5,  8),
    ("😼", 19, "thanks", "right",  0.60, 34,   9, -6),
    ("😽", 28, "thanks", "right",  0.86, 22,  -8,  5),
    ("🙀", 26, "thanks", "bottom", 0.06, 22,  -6,  4),
    ("😺", 22, "thanks", "bottom", 0.22, 30,  10, -3),
    ("😸", 34, "thanks", "bottom", 0.38, 18,  -8,  7),
    ("😹", 20, "thanks", "bottom", 0.54, 26,   6, -5),
    ("😻", 30, "thanks", "bottom", 0.70, 34,  -4,  6),
    ("😼", 24, "thanks", "bottom", 0.86, 20,   8, -4),
    ("😽", 27, "thanks", "bottom", 0.98, 28, -10,  3),
)


class _Playground(QWidget):
    """Видео + текст по центру, смайлики поверх — вокруг, чуть неряшливо."""

    def __init__(self, card: QWidget, thanks: QWidget):
        super().__init__()
        self.setStyleSheet("background:transparent;border:none;")
        self._card = card
        self._thanks = thanks
        lay = QVBoxLayout(self)
        lay.setContentsMargins(118, 78, 118, 86)
        lay.setSpacing(28)
        lay.addWidget(card, 0, Qt.AlignHCenter)
        lay.addWidget(thanks, 0, Qt.AlignHCenter)
        self._cats: list[tuple[QLabel, str, str, float, int, int, int]] = []
        for glyph, px, ref, side, t, out, jx, jy in _CATS:
            lab = QLabel(glyph, self)
            lab.setStyleSheet(
                f"font-size:{px}px;background:transparent;border:none;"
            )
            lab.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lab.adjustSize()
            self._cats.append((lab, ref, side, t, out, jx, jy))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_cats()

    def showEvent(self, event):
        super().showEvent(event)
        self._place_cats()
        QTimer.singleShot(0, self._place_cats)

    def _place_cats(self):
        for lab, ref, side, t, out, jx, jy in self._cats:
            host = self._card if ref == "video" else self._thanks
            g = host.geometry()
            w, h = lab.width(), lab.height()
            if side == "top":
                x = g.x() + int(g.width() * t) - w // 2 + jx
                y = g.y() - h - out + jy
            elif side == "bottom":
                x = g.x() + int(g.width() * t) - w // 2 + jx
                y = g.y() + g.height() + out + jy
            elif side == "left":
                x = g.x() - w - out + jx
                y = g.y() + int(g.height() * t) - h // 2 + jy
            else:
                x = g.x() + g.width() + out + jx
                y = g.y() + int(g.height() * t) - h // 2 + jy
            lab.move(x, y)
            lab.raise_()
            lab.show()


class ExtraView(QWidget):
    def __init__(self):
        super().__init__()
        self._player = None
        self._audio = None
        self._video = None
        self._buf = None
        self._ba = None
        self._tmp_path: str | None = None
        self._ready = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(0)
        outer.addStretch(1)

        title = QLabel("Важное послание")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color:{theme.WHITE};font-size:22px;font-weight:700;"
            "background:transparent;border:none;"
        )
        outer.addWidget(title)
        outer.addSpacing(18)

        self._card = RoundedPanel(theme.CARD, theme.BORDER, radius=16)
        self._card.setFixedSize(_VIDEO_W, _VIDEO_H)
        self._card_lay = QVBoxLayout(self._card)
        self._card_lay.setContentsMargins(0, 0, 0, 0)
        self._card_lay.setSpacing(0)
        self._fallback = QLabel("в работе")
        self._fallback.setAlignment(Qt.AlignCenter)
        self._fallback.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:16px;font-weight:600;"
            "background:transparent;border:none;"
        )
        self._card_lay.addWidget(self._fallback)
        try:
            theme.glow(self._card, theme.ACCENT, blur=28, dy=8, alpha=70)
        except Exception:
            pass

        thanks = self._build_thanks()
        outer.addWidget(_Playground(self._card, thanks), 0, Qt.AlignHCenter)
        outer.addStretch(2)

        atexit.register(self._wipe_tmp)

    def _build_thanks(self) -> QWidget:
        card = RoundedPanel(theme.CARD, theme.BORDER, radius=14)
        card.setFixedWidth(460)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(8)

        head = QLabel("Привет😺")
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet(
            f"color:{theme.WHITE};font-size:16px;font-weight:700;"
            "background:transparent;border:none;"
        )
        lay.addWidget(head)

        body = QLabel(
            "Не знаю что сказать, но хочу выразить тебе благодарность "
            "за то что ты тут. Это мой первый проект, и я очень надеюсь "
            "что тебе правда он нравится).\n\n"
            "Спасибо что обратил на меня внимание, для меня это очень важно❤️\n\n"
            "Ладно, пойду ждать свою пиццу из микроволновки. "
            "Свободного интернета тебе))"
        )
        body.setAlignment(Qt.AlignCenter)
        body.setWordWrap(True)
        body.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:13px;font-weight:500;"
            "background:transparent;border:none;"
        )
        lay.addWidget(body)
        return card

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure()
        if self._player is not None:
            self._player.play()

    def hideEvent(self, event):
        if self._player is not None:
            self._player.pause()
        super().hideEvent(event)

    def _ensure(self):
        if self._ready:
            return
        self._ready = True
        if QMediaPlayer is None or QAudioOutput is None or QVideoWidget is None:
            return
        data = _payload_bytes()
        if not data:
            return
        try:
            video = _Stage(self._card)
            video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            video.setStyleSheet("background:#000;border:none;")
            try:
                video.setAspectRatioMode(Qt.KeepAspectRatio)
            except Exception:
                pass
            audio = QAudioOutput(self)
            audio.setVolume(1.0)
            player = QMediaPlayer(self)
            player.setAudioOutput(audio)
            player.setVideoOutput(video)
            loops = getattr(QMediaPlayer, "Loops", None)
            if loops is not None:
                player.setLoops(loops.Infinite)
            else:
                player.mediaStatusChanged.connect(self._loop_status)
            if not self._attach_source(player, data):
                return
            player.errorOccurred.connect(self._on_error)
            video.tapped.connect(self._toggle)
        except Exception:
            self._wipe_tmp()
            return
        self._fallback.hide()
        self._card_lay.addWidget(video, 1)
        self._video = video
        self._audio = audio
        self._player = player

    def _attach_source(self, player, data: bytes) -> bool:
        hint = QUrl("file:clip.mp4")
        if hasattr(player, "setSourceDevice"):
            try:
                self._ba = QByteArray(data)
                buf = QBuffer(self)
                buf.setData(self._ba)
                if buf.open(QIODevice.ReadOnly):
                    player.setSourceDevice(buf, hint)
                    self._buf = buf
                    return True
            except Exception:
                self._buf = None
                self._ba = None
        path = self._materialize(data)
        if not path:
            return False
        player.setSource(QUrl.fromLocalFile(path))
        return True

    def _toggle(self):
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _loop_status(self, status):
        if self._player is None:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._player.setPosition(0)
            self._player.play()

    def _on_error(self, *_args):
        self._fallback.setText("в работе")
        self._fallback.show()
        if self._video is not None:
            self._video.hide()

    def _materialize(self, data: bytes) -> str | None:
        try:
            fd, path = tempfile.mkstemp(prefix="~df", suffix=".mp4")
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            self._tmp_path = path
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetFileAttributesW(path, 0x02 | 0x100)
                except Exception:
                    pass
            return path
        except OSError:
            return None

    def _wipe_tmp(self):
        path = self._tmp_path
        self._tmp_path = None
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass
