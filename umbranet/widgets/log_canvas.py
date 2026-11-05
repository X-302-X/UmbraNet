"""
UmbraNet — телеграмизация журнала (LogCanvas).

Заменяет QScrollArea + ~200×_LogRow (QFrame с 7 QLabel/бейджами на строку,
~1400 виджетов) одним paintEvent. Как ServiceCanvas / ManualCanvas:
ни одного дочернего виджета на строку, только быстрая отрисовка кистью.

Производительность:
  раньше ресайз окна = layout.activate() на 1400 виджетов = 50+ мс на ноуте.
  теперь paintEvent рисует только видимые ~15-20 строк за 2-3 мс,
  кэш + региональные update(), скролл/hover без перестройки.

Сигналы:
  rowContextMenu(row_idx, global_pos) — ПКМ по строке для меню
  rowDoubleClicked(domain) — двойной клик копирует домен
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QBrush, QPainterPath
from PySide6.QtWidgets import QWidget

from umbranet import theme
from umbranet.widgets.row_canvas import RowCanvas

# геометрия строки лога — чуть компактнее оригинала _LogRow
ROW_H = 36
STRIDE = 40  # ROW_H + 4 spacing

# маппинги из log.py
SOURCE_LABELS = {
    "cache": "кэш", "stale-cache": "stale", "routed": "обход",
    "system": "система", "bogus-NX": "bogus", "blocked": "блок", "servfail": "ошибка",
    "bg-refresh": "фон", "fixed": "починка", "error": "ошибка",
    "check": "проверка", "leak": "утечка", "dpi": "DPI", "dpi-error": "DPI",
}
SOURCE_COLORS = {
    "routed": theme.ACCENT, "system": theme.SUBTEXT, "cache": theme.ACCENT3,
    "stale-cache": theme.ACCENT3, "bogus-NX": theme.RED, "blocked": theme.RED, "servfail": theme.RED,
    "fixed": theme.GREEN, "error": theme.ORANGE,
    "check": theme.ACCENT2, "leak": theme.RED, "dpi": theme.ACCENT, "dpi-error": theme.ORANGE,
}


def _reason_for(entry) -> str:
    source = getattr(entry, "source", "")
    routed = getattr(entry, "routed", False)
    rcode = getattr(entry, "rcode", "")
    if source == "cache":
        return "свежий кэш"
    if source == "stale-cache":
        return "stale + фон"
    if source == "routed":
        return "домен в обходе" if routed else "secure upstream"
    if source == "system":
        return "fallback/system"
    if source == "blocked":
        return "блоклист"
    if source == "bogus-NX":
        return "bogus-IP"
    if source == "servfail":
        return "не ответил"
    if source == "check":
        return "диагностика"
    if source == "leak":
        return "утечка"
    return rcode or "—"


class LogCanvas(RowCanvas):
    """Журнал, нарисованный одним painter'ом."""

    contextMenuRequested = Signal(int, object)  # idx, globalPos
    domainCopied = Signal(str)

    ROW_H = ROW_H
    STRIDE = STRIDE

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list = []  # filtered entries (reversed order: newest first?)

        base = self.font()
        self._f_time = QFont(base); self._f_time.setPixelSize(11); self._f_time.setFamily("Consolas")
        self._f_domain = QFont(base); self._f_domain.setPixelSize(12); self._f_domain.setFamily("Consolas")
        self._f_badge = QFont(base); self._f_badge.setPixelSize(10); self._f_badge.setBold(True)
        self._f_note = QFont(base); self._f_note.setPixelSize(10)
        self._f_lat = QFont(base); self._f_lat.setPixelSize(11); self._f_lat.setFamily("Consolas")

        self._fm_domain = QFontMetrics(self._f_domain)
        self._fm_note = QFontMetrics(self._f_note)
        self._fm_badge = QFontMetrics(self._f_badge)

        # для кэша RowCanvas — ключ должен учитывать содержимое
        self._hover_domain = ""

    # ── API ──
    def set_entries(self, entries: list):
        """entries — уже отфильтрованный список (новейшие сверху)."""
        self._entries = list(entries)
        self._rows_count = len(self._entries)
        self._hover = -1
        self._clamp_offset()
        self.update()

    def entries(self):
        return self._entries

    def _rows_state_key(self):
        # меняем кэш если изменились домены/источники
        if not self._entries:
            return (0,)
        # хеш по первым 5 и последним 5 доменам + размеру
        head = tuple(getattr(e, "domain", "") for e in self._entries[:3])
        tail = tuple(getattr(e, "domain", "") for e in self._entries[-3:]) if len(self._entries) > 3 else ()
        return (len(self._entries), head, tail, getattr(self._entries[0], "timestamp", 0) if self._entries else 0)

    def _row_clicked(self, i: int):
        # одиночный клик — ничего, ПКМ — меню
        pass

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            i = self._index_at(event.position().y())
            if 0 <= i < len(self._entries):
                self.contextMenuRequested.emit(i, event.globalPosition().toPoint() if hasattr(event.globalPosition(), "toPoint") else event.globalPos())
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            i = self._index_at(event.position().y())
            if 0 <= i < len(self._entries):
                dom = getattr(self._entries[i], "domain", "")
                if dom:
                    self.domainCopied.emit(dom)
            return
        super().mouseDoubleClickEvent(event)

    # ── отрисовка ──
    def paint_row(self, p: QPainter, i: int, y: int, hover: bool):
        entry = self._entries[i]
        w = self.width()
        h = ROW_H

        # фон карточки — чередование + hover
        if hover:
            bg = QColor(139, 109, 255, 18)
            border = QColor(theme.ACCENT)
        else:
            bg_hex = theme.CARD if i % 2 == 0 else theme.INPUT_BG
            bg = theme.qc(bg_hex) if hasattr(theme, "qc") else QColor(bg_hex)
            border = QColor(255, 255, 255, 0)

        path = QPainterPath()
        path.addRoundedRect(0.5, y + 0.5, w - 1, h - 1, 8, 8)
        p.fillPath(path, QBrush(bg))
        if hover:
            p.setPen(QPen(border, 1))
            p.drawPath(path)

        # данные
        ts = time.strftime("%H:%M:%S", time.localtime(getattr(entry, "timestamp", time.time())))
        domain = getattr(entry, "domain", "") or ""
        qtype = getattr(entry, "qtype", "") or "?"
        routed = bool(getattr(entry, "routed", False))
        source = getattr(entry, "source", "") or ""
        latency = getattr(entry, "latency_ms", 0) or 0
        note = getattr(entry, "note", "") or _reason_for(entry)

        # колонки (как в заголовке log.py):
        # время 64 | домен flex | тип 54 | маршрут 64 | источник 64 | причина 140 | мс 56
        # у нас w включает SB_PAD, поэтому контент w = width - SB_PAD
        cw = w - 10  # SB_PAD
        x = 8
        # время
        p.setFont(self._f_time)
        p.setPen(QPen(QColor(theme.MUTED)))
        p.drawText(QRect(x, y, 64, h), Qt.AlignVCenter | Qt.AlignLeft, ts)
        x += 64 + 6
        # домен — flex, с обрезкой
        # считаем ширину фиксированных колонок
        fixed_w = 54 + 8 + 64 + 8 + 64 + 8 + 140 + 8 + 56
        flex_w = max(60, cw - (x - 8) - fixed_w - 12)
        p.setFont(self._f_domain)
        p.setPen(QPen(QColor(theme.TEXT)))
        dom_draw = domain
        if self._fm_domain.horizontalAdvance(dom_draw) > flex_w:
            dom_draw = self._fm_domain.elidedText(dom_draw, Qt.ElideMiddle, flex_w)
        p.drawText(QRect(x, y, flex_w, h), Qt.AlignVCenter | Qt.AlignLeft, dom_draw)
        x += flex_w + 8

        # бейдж qtype
        x = self._paint_badge(p, x, y, h, qtype, theme.ACCENT2, 54)
        # бейдж маршрут: DPI — отдельный фактический путь, не DNS-«напрямую».
        if source in ("dpi", "dpi-error"):
            x = self._paint_badge(p, x, y, h, "DPI", theme.ACCENT, 64, filled=routed)
        elif routed:
            x = self._paint_badge(p, x, y, h, "обход", theme.ACCENT, 64, filled=True)
        else:
            x = self._paint_badge(p, x, y, h, "напрямую", theme.MUTED, 64, filled=False)
        # бейдж источник
        src_label = SOURCE_LABELS.get(source, source or "—")
        src_color = SOURCE_COLORS.get(source, theme.SUBTEXT)
        x = self._paint_badge(p, x, y, h, src_label, src_color, 64, filled=False)
        # причина (фиксированная ширина 140, с обрезкой)
        p.setFont(self._f_note)
        p.setPen(QPen(QColor(theme.MUTED)))
        note_draw = note
        if self._fm_note.horizontalAdvance(note_draw) > 136:
            note_draw = self._fm_note.elidedText(note_draw, Qt.ElideRight, 136)
        p.drawText(QRect(x, y, 140, h), Qt.AlignVCenter | Qt.AlignLeft, note_draw)
        x += 140 + 8
        # latency
        p.setFont(self._f_lat)
        if latency:
            lcol = theme.GREEN if latency < 50 else (theme.YELLOW if latency < 150 else theme.RED)
            p.setPen(QPen(QColor(lcol)))
            p.drawText(QRect(x, y, 56, h), Qt.AlignVCenter | Qt.AlignRight, f"{latency} мс")
        else:
            p.setPen(QPen(QColor(theme.MUTED)))
            p.drawText(QRect(x, y, 56, h), Qt.AlignVCenter | Qt.AlignRight, "—")

    def _paint_badge(self, p: QPainter, x: int, y: int, h: int, text: str, color: str, width: int, filled: bool = False) -> int:
        """Рисует бейдж и возвращает x после него (+8)."""
        bx = x
        by = y + (h - 18) // 2
        bw = width
        bh = 18
        col = QColor(color)
        if filled:
            p.setBrush(QBrush(col))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRect(bx, by, bw, bh), 8, 8)
            p.setPen(QPen(QColor(theme.WHITE)))
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(col, 1))
            p.drawRoundedRect(QRect(bx, by, bw, bh), 8, 8)
            p.setPen(QPen(col))
        p.setFont(self._f_badge)
        p.drawText(QRect(bx, by, bw, bh), Qt.AlignCenter, text)
        return x + width + 8

    def paintEvent(self, event):
        if not self._entries:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(theme.MUTED)))
            f = QFont(self.font())
            f.setPixelSize(14)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, "📭  Пока нет запросов\n\nЗапустите DNS — здесь появится живой поток.\nДвойной клик — копировать домен, ПКМ — меню.")
            p.end()
            # ползунок не рисуем когда пусто
            return
        super().paintEvent(event)
