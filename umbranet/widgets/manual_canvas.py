"""
UmbraNet - «телеграмизация» ручного списка «Диспетчер задач».

Второй список вкладки «Маршрутизация» (подписки, домены, процессы) рисует
ОДИН paintEvent одного QWidget — как список сервисов (service_canvas.py)
и как Telegram Desktop. Ни одной карточки-QFrame с QLabel-ами и
QPushButton-ами на строку: иконка, чип типа, имя, бейдж сервиса и кнопка
удаления ✕ рисуются кистью, hover подсвечивает карточку, ползунок
прокрутки плавно затухает. У процессов вместо эмодзи 🎮 может лежать
настоящая иконка .exe в item["pixmap"] (QPixmap); если её нет — 🎮.

Семантика кнопки ✕ (как раньше):
  подписка           -> subscriptionRemoved(url)
  домен сервиса      -> serviceToggled(svc, False)   (выключает весь сервис)
  ручной домен/процесс -> itemRemoved(name, key)

Защищённые записи (item["protected"] = True, например chrome.exe / msedge.exe /
firefox.exe) НЕ удаляются: вместо ✕ рисуется замок 🔒, клик по правой зоне
игнорируется, курсор-«рука» не показывается, а в тултипе объясняется почему.
Раньше у них рисовался рабочий на вид ✕, но клик молча ничего не делал —
именно это и путало пользователя.

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
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from umbranet import theme

# ── геометрия ────────────────────────────────────────────────────────────────
CARD_H = 36          # высота карточки
CARD_STRIDE = 42     # шаг карточек (36 + spacing 6)
SB_PAD, SB_W = 10, 6

ICON_X, ICON_W = 10, 22       # иконка
ICON_PIXMAP = 16              # реальная иконка .exe (вместо эмодзи 🎮)
KIND_X, KIND_W = 40, 66       # чип «домен/процесс/подписка»
KIND_H = 20
NAME_X = 114                  # имя (после чипа + отступ)
RM_W = 20                     # зона кнопки ✕
RM_RIGHT = 8

# цвета чипа по типу записи
KIND_STYLE = {
    "routed_subscriptions": ("подписка", theme.ACCENT),
    "routed_domains": ("домен", theme.ACCENT2),
    "routed_processes": ("процесс", theme.ORANGE),
}
KIND_DEFAULT = ("запись", theme.MUTED)


class ManualCanvas(QWidget):
    #: Предпочтительная высота: столько занимает список, когда места хватает.
    #: Минимум задаётся отдельно и меньше (см. routing.py): при низком окне
    #: список ужимается и прокручивается сам вместо того, чтобы выезжать за
    #: нижний край вкладки — прокрутки вкладки у «Маршрутизации» нет.
    PREFERRED_H = 200

    """Ручной список активных записей, нарисованный одним painter'ом."""

    subscriptionRemoved = Signal(str)       # url подписки
    serviceToggled = Signal(str, bool)      # сервис, новое состояние
    itemRemoved = Signal(str, str)          # (name, config_key)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[dict] = []        # {name, key, icon, badge, badge_color, display}
        self._filtered: list[dict] = []
        self._search = ""
        self._hover = -1
        self._hover_rm = False
        self._offset = 0

        self._tops: list[int] = []
        self._content_h = 0

        base = self.font()
        self._f_icon = QFont(base); self._f_icon.setPixelSize(14)
        self._f_kind = QFont(base); self._f_kind.setPixelSize(10); self._f_kind.setBold(True)
        self._f_name = QFont(base); self._f_name.setPixelSize(12); self._f_name.setFamily("Consolas")
        self._fm_name = QFontMetrics(self._f_name)
        self._f_badge = QFont(base); self._f_badge.setPixelSize(10); self._f_badge.setBold(True)
        self._f_rm = QFont(base); self._f_rm.setPixelSize(12)
        self._f_hint = QFont(base); self._f_hint.setPixelSize(12)
        self._fm_badge = QFontMetrics(self._f_badge)

        # ползунок прокрутки (как в service_canvas: рисуем сами, затухает)
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
        self.setMinimumHeight(120)
        self._refilter()

    # ══════════════════ публичный API ═════════════════════════════════════

    def set_items(self, items: list[dict]):
        """items: [{name, key, icon, badge, badge_color, display, pixmap?}, ...]"""
        self._items = items
        self._refilter()

    def try_fill_process_icons(self):
        """Докинуть иконки процессам, у которых ещё только 🎮.

        Список имён мог не измениться (кэш routing), а chrome за это время
        запустился — тогда путь к .exe уже находится. Без пересборки всего
        списка подставляем pixmap и перерисовываем.
        """
        try:
            from umbranet.process_icons import pixmap_for_process
        except Exception:
            return
        changed = False
        for it in self._items:
            if it.get("key") != "routed_processes":
                continue
            pm = it.get("pixmap")
            if isinstance(pm, QPixmap) and not pm.isNull():
                continue
            fresh = pixmap_for_process(it.get("name") or "")
            if fresh is not None:
                it["pixmap"] = fresh
                changed = True
        if changed:
            self.update()

    def apply_search(self, text: str):
        q = (text or "").strip().lower()
        if q == self._search:
            return
        self._search = q
        self._refilter()

    # ══════════════════ модель ════════════════════════════════════════════

    def _refilter(self):
        q = self._search
        if not q:
            self._filtered = list(self._items)
        else:
            self._filtered = [it for it in self._items
                              if (q in (it["display"] or "").lower())
                              or (q in (it["name"] or "").lower())]
        self._tops = [i * CARD_STRIDE for i in range(len(self._filtered))]
        self._content_h = (len(self._filtered) * CARD_STRIDE - (CARD_STRIDE - CARD_H)
                           if self._filtered else 0)
        self._clamp_offset()
        self.update()

    def _max_offset(self) -> int:
        return max(0, self._content_h - self.height())

    def _clamp_offset(self):
        self._offset = max(0, min(self._offset, self._max_offset()))

    def _index_at(self, y: float) -> int:
        yy = int(y + self._offset)
        if yy < 0:
            return -1
        i = yy // CARD_STRIDE
        if i >= len(self._filtered):
            return -1
        if yy - i * CARD_STRIDE > CARD_H:
            return -1   # зазор между карточками
        return i

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
        return QSize(self.width() or 400, self.PREFERRED_H)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_offset()

    # ── мышь ──

    def _in_rm_zone(self, x: float) -> bool:
        return (self.width() - SB_PAD) - x < RM_W + RM_RIGHT + 4

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        if self._sb_op > 0.05 and pos.x() >= self.width() - SB_PAD - 2:
            tr = self._thumb_rect()
            if tr.isValid() and tr.top() - 4 <= pos.y() <= tr.bottom() + 4:
                self._sb_drag = True
                self._sb_grab_dy = int(pos.y()) - tr.top()
                self._show_sb()
            elif tr.isValid():
                page = max(CARD_STRIDE * 3, self.height() - CARD_STRIDE * 3)
                self._scroll(page if pos.y() < tr.top() else -page)
            return
        i = self._index_at(pos.y())
        if i < 0:
            return
        it = self._filtered[i]
        if not self._in_rm_zone(pos.x()):
            return   # клик по карточке (не по ✕) ничего не делает — как раньше
        if it.get("protected"):
            return   # защищено: ✕ не рисуется, клик игнорируем на всякий случай
        if it["key"] == "routed_subscriptions":
            self.subscriptionRemoved.emit(it["name"])
        elif it.get("badge"):
            self.serviceToggled.emit(it["badge"], False)
        else:
            self.itemRemoved.emit(it["name"], it["key"])

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
        # у защищённых записей кнопки удаления нет — hover-зона ✕ не считается
        if i >= 0 and self._filtered[i].get("protected"):
            rm = False
        else:
            rm = i >= 0 and self._in_rm_zone(pos.x())
        if i != self._hover or rm != self._hover_rm:
            old = self._hover
            self._hover = i
            self._hover_rm = rm
            if old >= 0:
                self.update(0, old * CARD_STRIDE - self._offset, self.width(), CARD_H)
            if i >= 0:
                self.update(0, i * CARD_STRIDE - self._offset, self.width(), CARD_H)
        hand = rm or (self._sb_op > 0.05 and pos.x() >= self.width() - SB_PAD - 2)
        self.setCursor(Qt.PointingHandCursor if hand else Qt.ArrowCursor)
        if rm and i >= 0:
            it = self._filtered[i]
            if it["key"] == "routed_subscriptions":
                self.setToolTip("Удалить подписку")
            elif it.get("badge"):
                self.setToolTip(f"Выключить сервис «{it['badge']}»")
            else:
                self.setToolTip(f"Удалить {it['display']}")
        elif i >= 0 and self._filtered[i].get("protected"):
            # объясняем замок, чтобы отсутствие ✕ не выглядело багом
            self.setToolTip(
                f"🔒 {self._filtered[i]['display']} — под защитой UmbraNet, "
                "удалить нельзя"
            )
        else:
            self.setToolTip("")

    def mouseReleaseEvent(self, event):
        if self._sb_drag:
            self._sb_drag = False
            self.update(self.width() - SB_PAD - 2, 0, SB_PAD + 2, self.height())

    def leaveEvent(self, event):
        if self._hover >= 0:
            self.update(0, self._hover * CARD_STRIDE - self._offset, self.width(), CARD_H)
        self._hover = -1
        self._hover_rm = False
        self.setCursor(Qt.ArrowCursor)

    # ══════════════════ отрисовка ═════════════════════════════════════════

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width() - SB_PAD

        if not self._filtered:
            hint = (f"Нет доменов по запросу «{self._search}»" if self._search
                    else "Нет активных доменов. Включите сервисы выше или добавьте домен вручную.")
            p.setPen(QPen(QColor(theme.MUTED)))
            p.setFont(self._f_hint)
            p.drawText(QRect(0, 0, w, self.height()),
                       Qt.AlignCenter | Qt.TextWordWrap, hint)
            self._paint_scrollbar(p)
            p.end()
            return

        first = self._offset // CARD_STRIDE
        y = first * CARD_STRIDE - self._offset
        i = first
        while i < len(self._filtered) and y < self.height():
            self._paint_card(p, self._filtered[i], QRect(0, y, w, CARD_H),
                             hovered=(i == self._hover))
            y += CARD_STRIDE
            i += 1

        self._paint_scrollbar(p)
        p.end()

    def _paint_card(self, p: QPainter, it: dict, rect: QRect, hovered: bool):
        # фон карточки: прозрачное стекло с рамкой; hover — фиолет
        if hovered:
            bg = QColor(139, 109, 255, 26)      # rgba(139,109,255,0.10)
            border = QColor(theme.ACCENT)
        else:
            bg = QColor(255, 255, 255, 9)       # rgba(255,255,255,0.035)
            border = QColor(255, 255, 255, 20)  # theme.BORDER
        p.setBrush(bg)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(QRect(rect.x(), rect.y() + 1, rect.width(), rect.height() - 2), 10, 10)

        cy = rect.y() + rect.height() // 2

        # иконка: реальный .exe, иначе эмодзи (🎮 у процессов)
        pm = it.get("pixmap")
        if isinstance(pm, QPixmap) and not pm.isNull():
            sz = min(ICON_PIXMAP, ICON_W, max(8, rect.height() - 8))
            x = rect.x() + ICON_X + (ICON_W - sz) // 2
            y = rect.y() + (rect.height() - sz) // 2
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(x, y, sz, sz, pm)
        else:
            p.setPen(QPen(QColor(theme.TEXT)))
            p.setFont(self._f_icon)
            p.drawText(QRect(ICON_X, rect.y(), ICON_W, rect.height()),
                       Qt.AlignCenter, it.get("icon") or "")

        # чип типа записи
        kind_text, kind_color = KIND_STYLE.get(it["key"], KIND_DEFAULT)
        kc = QColor(kind_color)
        p.setBrush(QColor(255, 255, 255, 9))
        p.setPen(QPen(kc, 1))
        p.drawRoundedRect(QRect(KIND_X, cy - KIND_H // 2, KIND_W, KIND_H), 8, 8)
        p.setPen(QPen(kc))
        p.setFont(self._f_kind)
        p.drawText(QRect(KIND_X, cy - KIND_H // 2, KIND_W, KIND_H),
                   Qt.AlignCenter, kind_text)

        # имя (Consolas, с обрезкой)
        rm_x = rect.width() - RM_RIGHT - RM_W
        badge_w = 0
        if it.get("badge") and it["key"] != "routed_subscriptions":
            badge_w = self._fm_badge.horizontalAdvance(it["badge"]) + 14
        avail = rm_x - 12 - NAME_X - badge_w
        name = it["display"]
        if self._fm_name.horizontalAdvance(name) > avail:
            name = self._fm_name.elidedText(name, Qt.ElideRight, avail)
        p.setPen(QPen(QColor(theme.TEXT)))
        p.setFont(self._f_name)
        p.drawText(QRect(NAME_X, rect.y(), avail, rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, name)

        # бейдж сервиса (правее имени, перед ✕)
        if badge_w:
            p.setPen(QPen(QColor(it.get("badge_color") or theme.ACCENT2)))
            p.setFont(self._f_badge)
            p.drawText(QRect(rm_x - 12 - badge_w, rect.y(), badge_w, rect.height()),
                       Qt.AlignVCenter | Qt.AlignRight, it["badge"])

        # кнопка ✕ — у защищённых записей вместо неё замок
        if it.get("protected"):
            # Замок рисуем вектором, а не эмодзи: монохромный, не зависит от
            # emoji-шрифта системы и совпадает по весу с остальной графикой.
            lx = rm_x + RM_W // 2
            ly = rect.y() + rect.height() // 2
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(theme.MUTED), 1.2))
            p.drawArc(QRect(lx - 4, ly - 8, 8, 9), 0, 180 * 16)   # дужка
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.MUTED))
            p.drawRoundedRect(QRect(lx - 5, ly - 2, 10, 8), 2, 2)  # корпус
            p.setBrush(Qt.NoBrush)
        else:
            p.setFont(self._f_rm)
            p.setPen(QPen(QColor(theme.RED) if (hovered and self._hover_rm)
                          else QColor(theme.MUTED)))
            p.drawText(QRect(rm_x, rect.y(), RM_W, rect.height()), Qt.AlignCenter, "✕")

    def _paint_scrollbar(self, p: QPainter):
        if self._sb_op <= 0.01:
            return
        tr = self._thumb_rect()
        if not tr.isValid():
            return
        c = (qc_color(theme.ACCENT) if self._sb_drag
             else qc_color(theme.SUBTEXT) if self._sb_hover
             else QColor("#4b4d75"))
        c = QColor(c)
        c.setAlpha(int(230 * self._sb_op))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(tr, 3, 3)


def qc_color(color: str) -> QColor:
    """QColor со строк темы (#hex / rgba(...)); без кэша — используется редко."""
    c = QColor(color)
    if not c.isValid():
        s = color.strip().lower()
        if s.startswith("rgba(") and s.endswith(")"):
            parts = [x.strip() for x in s[5:-1].split(",")]
            if len(parts) == 4:
                r, g, b = (int(float(x)) for x in parts[:3])
                c = QColor(r, g, b, int(max(0.0, min(1.0, float(parts[3]))) * 255))
    return c
