"""
UmbraNet - «телеграмизация» списка сервисов (вариант Б, PySide6).

Техника Telegram Desktop: ВЕСЬ список сервисов (избранное + категории +
строки со звёздами и тумблерами + ползунок прокрутки) рисует ОДИН
paintEvent одного QWidget. Ни одного дочернего виджета на строку:
фон hover, звёзды, эмодзи, имена, чип DNS/DPI, тумблеры и scrollbar рисуются кистью.
В DNS-only нельзя включить DPI-сервисы (и наоборот); Combo — всё можно.
На заблокированном тумблере курсор — 🚫 (без белого фона), плюс подсказка сменить режим.

Зачем: раньше каждая строка была QFrame'ом с QPushButton + 2 QLabel +
Toggle (~150 виджетов в области прокрутки) — при ресайзе окна Qt
перерисовывал их всех, и на слабом ноутбуке это стоило 15-20 мс на кадр.
Теперь весь список красится за единицы миллисекунд, перерисовываются
только затронутые области (региональные update()).

Взаимодействие с RoutingView — через три сигнала:
  serviceToggled(svc, on) / favoriteToggled(svc) / categoryToggled(cat, on)
Состояния прилетают обратно через set_service_states() / set_favorites()
(refresh() движка) — тумблеры анимируются только при реальном изменении.

Автор: X-302-X / UmbraNet_Official
Лицензия: GPLv3
"""

from __future__ import annotations

import bisect

from PySide6.QtCore import (
    QEasingCurve,
    QRect,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from umbranet import theme

# ── геометрия ────────────────────────────────────────────────────────────────
HDR_H = 48          # заголовок секции (линия + название + тумблер)
ROW_H = 28          # высота строки сервиса (22 контент + поля 3+3)
ROW_STRIDE = 32     # шаг строк (ROW_H + spacing 4)
SEC_GAP = 12        # отступ между секциями
TOGGLE_W, TOGGLE_H = 46, 26
STAR_X, STAR_W = 8, 22          # зона звезды
EMOJI_X, EMOJI_W = 37, 28       # зона эмодзи строки
NAME_X = 72                     # начало имени
TG_RIGHT = 6                    # правый отступ тумблера строки
CHIP_W, CHIP_H = 34, 16         # чип «DNS» / «DPI»
NAME_MARK_GAP = 6               # зазор имя ↔ чип (чип сразу после названия)
SB_PAD, SB_W = 10, 6            # зона и толщина ползунка прокрутки

# кэш QColor по строке темы (тема может быть любой из themes/, поэтому
# парсим строку один раз и запоминаем — на кадр ноль распарсов)
_QC_CACHE: dict[str, QColor] = {}


def qc(color: str) -> QColor:
    c = _QC_CACHE.get(color)
    if c is not None:
        return c
    c = QColor(color)
    if not c.isValid():
        # запасной парсер для rgba(r,g,b,a) на случай старого Qt
        s = color.strip().lower()
        if s.startswith("rgba(") and s.endswith(")"):
            parts = [p.strip() for p in s[5:-1].split(",")]
            if len(parts) == 4:
                r, g, b = (int(float(x)) for x in parts[:3])
                a = float(parts[3])
                c = QColor(r, g, b, int(max(0.0, min(1.0, a)) * 255))
    _QC_CACHE[color] = c
    return c


class ServiceCanvas(QWidget):
    #: Предпочтительная высота списка, когда места хватает (см. sizeHint).
    PREFERRED_H = 160

    """Список сервисов «Маршрутизации», нарисованный одним painter'ом."""

    serviceToggled = Signal(str, bool)    # сервис, новое состояние
    favoriteToggled = Signal(str)         # сервис (добавить/убрать из избранного)
    categoryToggled = Signal(str, bool)   # категория, новое состояние

    def __init__(self, catalog: list[tuple], parent=None, bypass_map: dict | None = None):
        super().__init__(parent)
        # catalog: [(cat, emoji, color1, color2, [(svc, svc_emoji), ...]), ...]
        self._catalog = catalog
        self._bypass_map = dict(bypass_map or {})
        self._app_mode = "dns_only"             # dns_only / combo / dpi_only
        self._dev_mode = False                  # настройки: снять блокировку DNS/DPI
        self._favorites: list[str] = []
        self._on: dict[str, bool] = {}          # svc -> включён
        self._tpos: dict[str, float] = {}       # ключ тумблера -> позиция 0..1 (0.5 partial)
        self._search = ""
        self._hover = -1                        # индекс строки под курсором
        self._hover_star = False
        self._offset = 0
        self._ban_cur: QCursor | None = None    # 🚫 на прозрачном фоне, только над тумблером

        # плоская модель + префикс-суммы
        self._rows: list[dict] = []
        self._tops: list[int] = []
        self._content_h = 0

        # кэш шрифтов
        base = self.font()
        self._f_star = QFont(base); self._f_star.setPixelSize(15); self._f_star.setBold(True)
        self._f_hdr_emoji = QFont(base); self._f_hdr_emoji.setPixelSize(15)
        self._f_title = QFont(base); self._f_title.setPixelSize(11)
        self._f_title.setWeight(QFont.Weight.ExtraBold)
        self._f_title.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        self._f_emoji = QFont(base); self._f_emoji.setPixelSize(16)
        self._f_name = QFont(base); self._f_name.setPixelSize(13)
        self._fm_name = QFontMetrics(self._f_name)
        self._f_chip = QFont(base); self._f_chip.setPixelSize(9); self._f_chip.setBold(True)
        self._grad_cache: dict[tuple, QLinearGradient] = {}

        # ползунок прокрутки (рисуем сами, плавное затухание)
        self._sb_op = 0.0
        self._sb_drag = False
        self._sb_hover = False
        self._sb_grab_dy = 0
        self._sb_fade: QVariantAnimation | None = None
        self._sb_hide = QTimer(self)
        self._sb_hide.setSingleShot(True)
        self._sb_hide.setInterval(900)
        self._sb_hide.timeout.connect(lambda: self._fade_sb(0.0))

        self.setMouseTracking(True)
        # Предпочтительная высота — 160 (см. sizeHint), минимум меньше: при
        # низком окне список ужимается и прокручивается сам. Полосы прокрутки
        # поперёк вкладки при этом не появляется — её у «Маршрутизации» нет.
        self.setMinimumHeight(70)
        self._rebuild()

    # ══════════════════ публичный API (для RoutingView) ══════════════════

    def set_favorites(self, favorites: list[str]):
        self._favorites = [s for s in favorites]
        self._rebuild()

    def set_service_states(self, states: dict[str, bool]):
        """Обновить состояния тумблеров; анимируются только изменившиеся."""
        for svc, on in states.items():
            self._on[svc] = bool(on)
        # тумблеры строк
        for i, r in enumerate(self._rows):
            if r["kind"] == "row":
                self._animate_toggle(r["svc"], 1.0 if self._on.get(r["svc"]) else 0.0)
            elif r["kind"] == "header" and r.get("cat"):
                self._animate_toggle("cat:" + r["cat"], self._cat_pos(r["cat"]))
        self.update()

    def apply_search(self, text: str):
        q = (text or "").strip().lower()
        if q == self._search:
            return
        self._search = q
        self._rebuild()

    def set_bypass_map(self, bypass_map: dict):
        self._bypass_map = dict(bypass_map or {})
        self.update()

    def set_app_mode(self, mode: str):
        """Текущий режим окна: dns_only / combo / dpi_only — для подсказки и акцента чипа."""
        mode = str(mode or "dns_only")
        if mode not in ("dns_only", "combo", "dpi_only"):
            mode = "dns_only"
        if mode == self._app_mode:
            return
        self._app_mode = mode
        self.update()

    def set_developer_mode(self, on: bool) -> None:
        on = bool(on)
        if on == self._dev_mode:
            return
        self._dev_mode = on
        self.update()

    def _bypass_of(self, svc: str) -> str:
        v = str(self._bypass_map.get(svc) or "dns").strip().lower()
        return v if v in ("dns", "dpi") else "dns"

    def _locked(self, svc: str) -> bool:
        """Сервис нельзя ВКЛЮЧИТЬ в текущем режиме (выключить — можно)."""
        if self._dev_mode or self._app_mode == "combo":
            return False
        b = self._bypass_of(svc)
        if self._app_mode == "dpi_only":
            return b != "dpi"
        return b != "dns"

    def _elided_name(self, svc: str, content_w: int) -> str:
        """Имя с обрезкой: справа оставляем место под чип и тумблер."""
        toggle_x = content_w - TOGGLE_W - TG_RIGHT
        cluster = NAME_MARK_GAP + CHIP_W
        avail = max(0, toggle_x - NAME_X - cluster - 8)
        name = svc
        if avail > 0 and self._fm_name.horizontalAdvance(name) > avail:
            name = self._fm_name.elidedText(name, Qt.ElideRight, avail)
        return name

    def _chip_x(self, svc: str, content_w: int) -> int:
        """x чипа — сразу после названия, не у тумблера."""
        name_w = self._fm_name.horizontalAdvance(self._elided_name(svc, content_w))
        return NAME_X + name_w + NAME_MARK_GAP

    def _locked_tip(self, svc: str | None = None) -> str:
        """Почему тумблер серый — человеку нужно сменить режим."""
        need = "DPI" if self._app_mode == "dns_only" else "DNS"
        other = "DPI или Combo" if self._app_mode == "dns_only" else "DNS или Combo"
        if svc:
            return (
                f"{svc} работает через {need}. "
                f"Переключитесь на {other}, чтобы включить."
            )
        return f"В этом режиме эти сервисы не включить. Переключитесь на {other}."

    def _ban_cursor(self) -> QCursor:
        """Настоящий 🚫 на прозрачном фоне — без белого квадрата."""
        if self._ban_cur is None:
            s = 24
            pm = QPixmap(s, s)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.TextAntialiasing, True)
            font = QFont(self.font())
            # Windows: Segoe UI Emoji даёт цветной 🚫 без подложки
            font.setFamily("Segoe UI Emoji")
            font.setPixelSize(18)
            p.setFont(font)
            p.setPen(QPen(qc(theme.RED)))
            p.drawText(QRect(0, 0, s, s), Qt.AlignCenter, "🚫")
            p.end()
            self._ban_cur = QCursor(pm, s // 2, s // 2)
        return self._ban_cur

    # ══════════════════ модель ════════════════════════════════════════════

    def _rebuild(self):
        """Пересобрать плоский список записей под текущие избранное/поиск."""
        rows: list[dict] = []
        q = self._search

        # ── избранное ──
        fav_rows = [s for s in self._favorites
                    if (not q) or (q in s.lower())]
        if fav_rows:
            rows.append({"kind": "header", "fav": True})
            for s in fav_rows:
                rows.append({"kind": "row", "svc": s, "fav": True})
            rows.append({"kind": "gap"})

        # ── категории ──
        for cat, emoji, c1, c2, svcs in self._catalog:
            cat_match = q and q in cat.lower()
            match = [s for s, _e in svcs if (not q) or cat_match or (q in s.lower())]
            if not match:
                continue
            rows.append({"kind": "header", "cat": cat, "emoji": emoji, "c1": c1, "c2": c2})
            for s, se in svcs:
                if s in match:
                    rows.append({"kind": "row", "svc": s, "emoji": se, "fav": False})
            rows.append({"kind": "gap"})

        self._rows = rows
        self._tops = []
        cum = 0
        for k, r in enumerate(rows):
            self._tops.append(cum)
            if r["kind"] == "gap":
                if k == len(rows) - 1:
                    cum += SEC_GAP // 2      # хвостовому зазору хватит половины
                else:
                    cum += SEC_GAP
            else:
                cum += HDR_H if r["kind"] == "header" else ROW_STRIDE
        if rows and rows[-1]["kind"] == "row":
            cum = cum - ROW_STRIDE + ROW_H   # последняя строка не даёт нижнего spacing
        self._content_h = cum
        self._clamp_offset()
        self.update()

    def _svc_cat(self, svc: str) -> str:
        return self._svc_cat_map().get(svc, "")

    _cat_map_cache = None

    def _svc_cat_map(self) -> dict:
        if self._cat_map_cache is None:
            self._cat_map_cache = {s: cat for cat, _e, _c1, _c2, svcs in self._catalog
                                   for s, _se in svcs}
        return self._cat_map_cache

    def _svc_emoji(self, svc: str) -> str:
        for cat, _e, _c1, _c2, svcs in self._catalog:
            for s, se in svcs:
                if s == svc:
                    return se
        return "🌐"

    def _cat_svcs(self, cat: str) -> list[str]:
        for c, _e, _c1, _c2, svcs in self._catalog:
            if c == cat:
                return [s for s, _se in svcs]
        return []

    def _cat_pos(self, cat: str) -> float:
        """Позиция тумблера категории: 1 вкл / 0.5 partial / 0 выкл."""
        states = [self._on.get(s, False) for s in self._cat_svcs(cat)]
        if states and all(states):
            return 1.0
        if any(states):
            return 0.5
        return 0.0

    # ── геометрия ──

    def _content_w(self) -> int:
        return self.width() - SB_PAD

    def _entry_h(self, i: int) -> int:
        k = self._rows[i]["kind"]
        if k == "header":
            return HDR_H
        if k == "gap":
            return SEC_GAP
        return ROW_H

    def _row_rect(self, i: int) -> QRect:
        if not (0 <= i < len(self._rows)):
            return QRect()
        return QRect(0, self._tops[i] - self._offset, self._content_w(), self._entry_h(i))

    def _index_at(self, y: float) -> int:
        yy = y + self._offset
        if yy < 0:
            return -1
        i = bisect.bisect_right(self._tops, yy) - 1
        if i < 0 or i >= len(self._rows):
            return -1
        if yy >= self._tops[i] + self._entry_h(i):
            return -1   # зазор между строками
        if self._rows[i]["kind"] == "gap":
            return -1
        return i

    def _first_visible(self) -> tuple[int, int]:
        i = max(0, bisect.bisect_right(self._tops, self._offset) - 1)
        return i, self._tops[i] - self._offset

    def _max_offset(self) -> int:
        return max(0, self._content_h - self.height())

    def _clamp_offset(self):
        self._offset = max(0, min(self._offset, self._max_offset()))

    # ── прокрутка и ползунок ──

    def _scroll(self, delta: int):
        self._offset += delta
        self._clamp_offset()
        self._show_sb()
        self.update()

    def _show_sb(self):
        if self._max_offset() <= 0:
            return
        if self._sb_op < 1.0:
            self._fade_sb(1.0)
        self._sb_hide.start()

    def _fade_sb(self, target: float):
        if self._sb_fade is not None:
            try:
                self._sb_fade.stop()
            except RuntimeError:
                pass
        anim = QVariantAnimation(self)
        anim.setDuration(180)
        anim.setStartValue(self._sb_op)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.InOutCubic)

        def tick(v):
            self._sb_op = float(v)
            self.update(self.width() - SB_PAD - 2, 0, SB_PAD + 2, self.height())

        anim.valueChanged.connect(tick)
        anim.start(QVariantAnimation.DeleteWhenStopped)
        self._sb_fade = anim

    def _thumb_rect(self) -> QRect:
        max_off = self._max_offset()
        if max_off <= 0 or self.height() <= 0:
            return QRect()
        track = self.height() - 8
        thumb_h = max(28, int(track * self.height() / max(1, self._content_h)))
        if thumb_h >= track:
            return QRect()
        t = self._offset / max_off
        y = 4 + int(t * (track - thumb_h))
        return QRect(self.width() - SB_PAD + 1, y, SB_W, thumb_h)

    def wheelEvent(self, event):
        self._scroll(-event.angleDelta().y() * 3 // 2)

    def sizeHint(self):
        """Предпочтительная высота списка (минимум задан отдельно и меньше)."""
        return QSize(self.width() or 400, self.PREFERRED_H)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._grad_cache.clear()
        self._clamp_offset()

    # ── мышь ──

    def _row_zone(self, i: int, x: float) -> str:
        """star / chip / help / toggle / body для строки; для заголовка — toggle/body."""
        r = self._rows[i]
        if r["kind"] == "row":
            if x < STAR_X + STAR_W + 6:
                return "star"
            w = self._content_w()
            toggle_x = w - TOGGLE_W - TG_RIGHT
            if x >= toggle_x:
                return "toggle"
            chip_x = self._chip_x(r["svc"], w)
            if chip_x <= x < chip_x + CHIP_W + 3:
                return "chip"
            return "body"
        # заголовок: тумблер справа
        if self._content_w() - x < TOGGLE_W + 10:
            return "toggle"
        return "body"

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        # ползунок прокрутки
        if self._sb_op > 0.05 and pos.x() >= self.width() - SB_PAD - 2:
            tr = self._thumb_rect()
            if tr.isValid() and tr.top() - 4 <= pos.y() <= tr.bottom() + 4:
                self._sb_drag = True
                self._sb_grab_dy = int(pos.y()) - tr.top()
                self._show_sb()
            elif tr.isValid():
                page = max(ROW_STRIDE * 3, self.height() - ROW_STRIDE * 3)
                self._scroll(page if pos.y() < tr.top() else -page)
            return
        i = self._index_at(pos.y())
        if i < 0:
            return
        r = self._rows[i]
        zone = self._row_zone(i, pos.x())
        if r["kind"] == "row":
            if zone == "star":
                self.favoriteToggled.emit(r["svc"])
            elif zone == "toggle":
                new = not self._on.get(r["svc"], False)
                if new and self._locked(r["svc"]):
                    return
                self._on[r["svc"]] = new
                self._animate_toggle(r["svc"], 1.0 if new else 0.0)
                self.serviceToggled.emit(r["svc"], new)
        elif r["kind"] == "header":
            if zone == "toggle" and r.get("cat"):
                pos_cat = self._cat_pos(r["cat"])
                new = pos_cat != 1.0     # partial/off -> включить всё; on -> выключить
                if new and not any(not self._locked(s) for s in self._cat_svcs(r["cat"])):
                    return
                self.categoryToggled.emit(r["cat"], new)

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._sb_drag:
            tr = self._thumb_rect()
            max_off = self._max_offset()
            track = self.height() - 8
            t = (pos.y() - self._sb_grab_dy - 4) / max(1, track - tr.height())
            self._offset = int(max(0.0, min(1.0, t)) * max_off)
            self._show_sb()
            self.update()
            return
        i = self._index_at(pos.y())
        zone = self._row_zone(i, pos.x()) if i >= 0 else ""
        star = zone == "star"
        if i != self._hover or star != self._hover_star:
            old = self._hover
            self._hover = i
            self._hover_star = star
            if old >= 0:
                self.update(self._row_rect(old))
            if i >= 0:
                self.update(self._row_rect(i))
        # 🚫 и подсказка — только над самим тумблером, не над всей строкой
        locked_toggle = False
        locked_tip = ""
        if i >= 0 and zone == "toggle":
            r = self._rows[i]
            if r["kind"] == "row" and self._locked(r["svc"]) and not self._on.get(r["svc"]):
                locked_toggle = True
                locked_tip = self._locked_tip(r["svc"])
            elif (r["kind"] == "header" and r.get("cat")
                  and self._cat_pos(r["cat"]) != 1.0
                  and not any(not self._locked(s) for s in self._cat_svcs(r["cat"]))):
                locked_toggle = True
                locked_tip = self._locked_tip()
        if star:
            self.setCursor(Qt.PointingHandCursor)
            r = self._rows[i]
            self.setToolTip("Убрать из избранного" if r["svc"] in self._favorites
                            else "Добавить в избранное")
        elif locked_toggle:
            self.setCursor(self._ban_cursor())
            self.setToolTip(locked_tip)
        else:
            hand = (
                i >= 0 and zone == "toggle"
            ) or (
                self._sb_op > 0.05 and pos.x() >= self.width() - SB_PAD - 2
            )
            self.setCursor(Qt.PointingHandCursor if hand else Qt.ArrowCursor)
            self.setToolTip("")

    def mouseReleaseEvent(self, event):
        if self._sb_drag:
            self._sb_drag = False
            self.update(self.width() - SB_PAD - 2, 0, SB_PAD + 2, self.height())

    def leaveEvent(self, event):
        if self._hover >= 0:
            self.update(self._row_rect(self._hover))
        self._hover = -1
        self._hover_star = False
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("")

    # ── анимация тумблеров ──

    def _animate_toggle(self, key: str, target: float):
        cur = self._tpos.get(key)
        if cur is None or abs(cur - target) < 0.01:
            self._tpos[key] = target
            return
        anim = QVariantAnimation(self)
        anim.setDuration(140)
        anim.setStartValue(cur)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.InOutCubic)

        def tick(v, k=key):
            self._tpos[k] = float(v)
            self.update(self._toggle_rect(k))

        anim.valueChanged.connect(tick)
        anim.start(QVariantAnimation.DeleteWhenStopped)
        self._tpos[key] = target   # логическое состояние сразу; рисуем по анимации

    def _toggle_rect(self, key: str) -> QRect:
        """Прямоугольник тумблера (для региональной перерисовки)."""
        for i, r in enumerate(self._rows):
            rr = self._row_rect(i)
            if r["kind"] == "row" and r["svc"] == key:
                x = self._content_w() - TOGGLE_W - TG_RIGHT
                return QRect(x, rr.y() + (ROW_H - TOGGLE_H) // 2, TOGGLE_W, TOGGLE_H)
            if r["kind"] == "header" and key == "cat:" + str(r.get("cat")):
                x = self._content_w() - TOGGLE_W - 10
                return QRect(x, rr.y() + (HDR_H - TOGGLE_H) // 2, TOGGLE_W, TOGGLE_H)
        return QRect(0, 0, self.width(), self.height())

    # ══════════════════ отрисовка ═════════════════════════════════════════

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self._content_w()

        i, y = self._first_visible()
        while i < len(self._rows) and y < self.height():
            r = self._rows[i]
            if r["kind"] == "gap":
                y += SEC_GAP
                i += 1
                continue
            rect = QRect(0, y, w, HDR_H if r["kind"] == "header" else ROW_H)
            if r["kind"] == "header":
                self._paint_header(p, r, rect)
            else:
                self._paint_row(p, r, rect, hovered=(i == self._hover))
            y += HDR_H if r["kind"] == "header" else ROW_STRIDE
            i += 1

        self._paint_scrollbar(p)
        p.end()

    def _paint_header(self, p: QPainter, r: dict, rect: QRect):
        c1 = theme.YELLOW if r.get("fav") else r["c1"]
        emoji = "⭐" if r.get("fav") else r["emoji"]
        title = "Избранное" if r.get("fav") else str(r["cat"])

        # тонкая неоновая линия в цвете категории
        key = (c1, rect.width())
        grad = self._grad_cache.get(key)
        if grad is None:
            grad = QLinearGradient(0, 0, rect.width(), 0)
            grad.setColorAt(0.0, qc(c1))
            grad.setColorAt(1.0, QColor(255, 255, 255, 8))
            if len(self._grad_cache) > 64:
                self._grad_cache.clear()
            self._grad_cache[key] = grad
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRect(QRect(0, rect.y() + 10, rect.width(), 1))

        # эмодзи + название
        p.setPen(QPen(qc(c1)))
        p.setFont(self._f_hdr_emoji)
        p.drawText(QRect(4, rect.y() + 16, 32, 22), Qt.AlignCenter, emoji)
        p.setFont(self._f_title)
        p.drawText(QRect(44, rect.y() + 16, rect.width() - 44 - 130, 22),
                   Qt.AlignVCenter | Qt.AlignLeft, title.upper())

        # тумблер категории (в «Избранном» его нет)
        if r.get("cat"):
            pos = self._tpos.get("cat:" + r["cat"], self._cat_pos(r["cat"]))
            x = rect.width() - TOGGLE_W - 10
            self._paint_toggle(p, x, rect.y() + (HDR_H - TOGGLE_H) // 2, pos)

    def _paint_row(self, p: QPainter, r: dict, rect: QRect, hovered: bool = False):
        svc = r["svc"]
        fav = svc in self._favorites

        if hovered:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 12))   # rgba(255,255,255,0.045)
            p.drawRoundedRect(QRect(0, rect.y(), rect.width(), ROW_H), 8, 8)

        # звезда
        p.setFont(self._f_star)
        if fav or (hovered and self._hover_star):
            p.setPen(QPen(qc(theme.YELLOW)))
        else:
            p.setPen(QPen(qc(theme.MUTED)))
        p.drawText(QRect(STAR_X, rect.y(), STAR_W, ROW_H), Qt.AlignCenter,
                   "★" if fav else "☆")

        # эмодзи
        p.setPen(QPen(qc(theme.TEXT)))
        p.setFont(self._f_emoji)
        p.drawText(QRect(EMOJI_X, rect.y(), EMOJI_W, ROW_H), Qt.AlignCenter,
                   r.get("emoji") or self._svc_emoji(svc))

        # имя + сразу за ним чип DNS/DPI (не столбиком у тумблера)
        name = self._elided_name(svc, rect.width())
        name_w = self._fm_name.horizontalAdvance(name)
        p.setFont(self._f_name)
        p.setPen(QPen(qc(theme.TEXT)))
        p.drawText(QRect(NAME_X, rect.y(), max(1, name_w), ROW_H),
                   Qt.AlignVCenter | Qt.AlignLeft, name)
        self._paint_bypass_chip(p, svc, self._chip_x(svc, rect.width()), rect.y())

        # тумблер (у заблокированных «выкл» рисуем серым)
        on = bool(self._on.get(svc))
        pos = self._tpos.get(svc, 1.0 if on else 0.0)
        x = rect.width() - TOGGLE_W - TG_RIGHT
        self._paint_toggle(p, x, rect.y() + (ROW_H - TOGGLE_H) // 2, pos,
                           disabled=self._locked(svc) and not on)

    def _paint_bypass_chip(self, p: QPainter, svc: str, x: int, row_y: int):
        bypass = self._bypass_of(svc)
        label = "DPI" if bypass == "dpi" else "DNS"
        # Цвета фиксированные: не зависят от текущего режима окна.
        color = qc(theme.ORANGE) if bypass == "dpi" else qc(theme.ACCENT2)
        y = row_y + (ROW_H - CHIP_H) // 2
        p.setBrush(QColor(color.red(), color.green(), color.blue(), 28))
        p.setPen(QPen(color, 1))
        p.drawRoundedRect(QRect(x, y, CHIP_W, CHIP_H), 6, 6)
        p.setFont(self._f_chip)
        p.setPen(QPen(color))
        p.drawText(QRect(x, y, CHIP_W, CHIP_H), Qt.AlignCenter, label)

    def _paint_toggle(self, p: QPainter, x: int, y: int, pos: float,
                      disabled: bool = False):
        """Пилюля как umbranet.widgets.Toggle: #4b4d75 -> GREEN, partial ORANGE."""
        if disabled:
            pos = 0.0
            track = QColor("#3a3b55")
        elif abs(pos - 0.5) < 0.01:
            track = qc(theme.ORANGE)
        else:
            off = QColor("#4b4d75")
            on = qc(theme.GREEN)
            t = pos
            track = QColor(
                int(off.red() + (on.red() - off.red()) * t),
                int(off.green() + (on.green() - off.green()) * t),
                int(off.blue() + (on.blue() - off.blue()) * t),
            )
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRect(x, y, TOGGLE_W, TOGGLE_H), 13, 13)
        d = TOGGLE_H - 4
        kx = x + 2 + pos * (TOGGLE_W - d - 4)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRect(int(kx), y + 2, d, d))

    def _paint_scrollbar(self, p: QPainter):
        if self._sb_op <= 0.01:
            return
        tr = self._thumb_rect()
        if not tr.isValid():
            return
        c = (qc(theme.ACCENT) if self._sb_drag
             else qc(theme.SUBTEXT) if self._sb_hover
             else QColor("#4b4d75"))
        c = QColor(c)
        c.setAlpha(int(230 * self._sb_op))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(tr, 3, 3)
