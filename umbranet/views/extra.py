"""
UmbraNet — скрытая дополнительная вкладка с открыткой.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from umbranet import theme
from umbranet.widgets.rounded_panel import RoundedPanel

# Рамка совпадает с фото 16:9: без пустых полос над изображением и под ним.
_CARD_W = 460
_CARD_H = round(_CARD_W * 9 / 16)
_PLAYGROUND_MIN_W = _CARD_W + 2 * 88  # место для котиков по бокам
_PLAYGROUND_TOP = 78
_PLAYGROUND_BOTTOM = 86
_CARD_GAP = 28


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


# Котики вокруг открытки.
# glyph, px, якорь, сторона, t вдоль края (0..1), отступ наружу, джиттер x/y.
# Низ изображения и верх текста не трогаем — там слишком узко, наезжали бы.
_CATS = (
    ("😺", 30, "image",  "top",    0.04, 28,  -8,  6),
    ("😸", 22, "image",  "top",    0.20, 20,  10, -4),
    ("😹", 34, "image",  "top",    0.36, 36,  -6,  8),
    ("😻", 24, "image",  "top",    0.52, 18,  12, -2),
    ("😼", 28, "image",  "top",    0.68, 32,  -4,  5),
    ("😽", 20, "image",  "top",    0.84, 22,   8, -6),
    ("🙀", 26, "image",  "top",    0.97, 30, -10,  3),
    ("😺", 32, "image",  "left",   0.06, 26,   4, -8),
    ("😸", 24, "image",  "left",   0.24, 34,  -6,  6),
    ("😹", 36, "image",  "left",   0.44, 22,   8, -4),
    ("😻", 21, "image",  "left",   0.62, 30,  -4, 10),
    ("😼", 29, "image",  "left",   0.80, 18,   6, -6),
    ("😽", 25, "image",  "left",   0.94, 28,  -8,  4),
    ("🙀", 23, "image",  "right",  0.08, 24,   6,  8),
    ("😺", 34, "image",  "right",  0.26, 32,  -4, -6),
    ("😸", 20, "image",  "right",  0.44, 18,  10,  4),
    ("😹", 28, "image",  "right",  0.62, 36,  -8, -2),
    ("😻", 31, "image",  "right",  0.80, 22,   5,  7),
    ("😼", 22, "image",  "right",  0.95, 28,  -6, -8),
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
    """Картинка + текст по центру, смайлики поверх — вокруг, чуть неряшливо."""

    def __init__(self, card: QWidget, thanks: QWidget):
        super().__init__()
        self.setStyleSheet("background:transparent;border:none;")
        self._card = card
        self._thanks = thanks
        # Размеры и вертикальные координаты карточек фиксированы. Группа
        # может менять ширину, но обе карточки всегда центрируются по одной оси.
        # Обычная раскладка при нехватке высоты могла сжать зазор до наложения.
        self.setMinimumWidth(_PLAYGROUND_MIN_W)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card.setParent(self)
        thanks.setParent(self)
        thanks_y = _PLAYGROUND_TOP + _CARD_H + _CARD_GAP
        self._thanks_y = thanks_y
        card.move(0, _PLAYGROUND_TOP)
        thanks.move(0, thanks_y)
        self.setFixedHeight(thanks_y + thanks.height() + _PLAYGROUND_BOTTOM)
        self._center_cards()
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
        self._center_cards()
        self._place_cats()

    def showEvent(self, event):
        super().showEvent(event)
        self._center_cards()
        self._place_cats()
        QTimer.singleShot(0, self._layout_children)

    def _layout_children(self):
        self._center_cards()
        self._place_cats()

    def _center_cards(self):
        x = max(0, (self.width() - _CARD_W) // 2)
        self._card.move(x, _PLAYGROUND_TOP)
        self._thanks.move(x, self._thanks_y)

    def _place_cats(self):
        for lab, ref, side, t, out, jx, jy in self._cats:
            host = self._card if ref == "image" else self._thanks
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
        self._ready = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(0)
        outer.addStretch(1)

        title = QLabel("Важное послание")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color:{theme.TEXT};font-size:22px;font-weight:700;"
            "background:transparent;border:none;"
        )
        outer.addWidget(title)
        outer.addSpacing(18)

        self._card = RoundedPanel(theme.CARD, theme.BORDER, radius=16)
        self._card.setFixedSize(_CARD_W, _CARD_H)
        self._card_lay = QVBoxLayout(self._card)
        self._card_lay.setContentsMargins(0, 0, 0, 0)
        self._card_lay.setSpacing(0)
        self._fallback = QLabel("в работе")
        self._fallback.setFixedSize(_CARD_W, _CARD_H)
        self._fallback.setAlignment(Qt.AlignCenter)
        self._fallback.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:16px;font-weight:600;"
            "background:transparent;border:none;"
        )
        self._card_lay.addWidget(self._fallback)
        try:
            # Смещение тени по вертикали убрано: ореол одинаковый со всех сторон.
            theme.glow(self._card, theme.ACCENT, blur=28, dy=0, alpha=70)
        except Exception:
            pass

        thanks = self._build_thanks()
        outer.addWidget(_Playground(self._card, thanks))
        outer.addStretch(2)
        # При нехватке места прокручивается страница, а не сжимаются карточки.
        # Минимальной ширины хватает и для карточек, и для котиков по краям;
        # на обычной ширине горизонтальная прокрутка не нужна.
        page_hint = outer.sizeHint()
        self.setMinimumWidth(max(page_hint.width(), _PLAYGROUND_MIN_W + 48))
        self.setMinimumHeight(page_hint.height())

    def _build_thanks(self) -> QWidget:
        card = RoundedPanel(theme.CARD, theme.BORDER, radius=14)
        card.setFixedWidth(460)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(8)

        head = QLabel("Привет😺")
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet(
            f"color:{theme.TEXT};font-size:16px;font-weight:700;"
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
        lay.activate()
        panel_height = (
            lay.heightForWidth(card.width())
            if lay.hasHeightForWidth()
            else lay.sizeHint().height()
        )
        if panel_height <= 0:
            panel_height = lay.sizeHint().height()
        card.setFixedHeight(panel_height)
        return card

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure()

    def _ensure(self):
        """Загружает открытку только при открытии скрытой вкладки."""
        if self._ready:
            return
        self._ready = True
        data = _payload_bytes()
        if not data:
            return
        try:
            image = QPixmap()
            if not image.loadFromData(data):
                return
            self._fallback.setText("")
            self._fallback.setPixmap(
                image.scaled(
                    _CARD_W,
                    _CARD_H,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        except Exception:
            # Скрытый ресурс необязателен: при ошибке декодирования остаётся
            # нейтральная заглушка, а остальное приложение работает как прежде.
            return
