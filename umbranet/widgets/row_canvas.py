"""
UmbraNet — базовый канвас строчных списков (радио-выбор).

Общая механика для TransportCanvas и DpiStrategyCanvas:
один paintEvent на весь список, затухающий ползунок прокрутки,
hover-подсветка строк, клики маппятся по координатам. Ни одной
карточки-QFrame на строку (как в ServiceCanvas / ManualCanvas).

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from umbranet import theme
from umbranet.widgets.wheel_scroll import wheel_delta_px

SB_PAD = 10      # отступ ползунка от правого края
SB_W = 6         # толщина ползунка
SB_FADE_MS = 900 # через сколько ползунок затухает


class RowCanvas(QWidget):
    """Список одинаковых по высоте строк: скролл/hover/клики здесь,
    отрисовка строки — в наследнике (paint_row)."""

    rowClicked = None   # наследник объявляет Signal(int) / (str) сам

    # ── геометрия, которую задаёт наследник ──
    ROW_H = 40
    STRIDE = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover = -1
        self._offset = 0
        self._rows_count = 0

        self._sb_op = 0.0
        self._sb_drag = False
        self._sb_grab_dy = 0
        self._sb_fade: QVariantAnimation | None = None
        self._sb_hide = QTimer(self)
        self._sb_hide.setSingleShot(True)
        self._sb_hide.setInterval(SB_FADE_MS)
        self._sb_hide.timeout.connect(lambda: self._fade_sb(0.0))

        self.setMouseTracking(True)

        # Кэш видимой области: при живом resize (кадр летит каждый шаг)
        # paintEvent — один drawPixmap; AA-рендер строк происходит только
        # при скролле/hover/изменении данных (фикс «resize дёргается»).
        self._cache_pm: QPixmap | None = None
        self._cache_key = None

    def _rows_state_key(self):
        """Хук наследника: что меняет вид строк (состояния/бейджи)."""
        return None

    def _render_cache(self) -> QPixmap:
        # DPR: на Windows-ноутах масштаб 125-150% — кэш обязан рендериться
        # в физических пикселях, иначе текст «мылится» при растяжении
        dpr = self.devicePixelRatioF()
        pm = QPixmap(max(1, int(self.width() * dpr + 0.5)),
                     max(1, int(self.height() * dpr + 0.5)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        first = self._offset // self.STRIDE
        y = -(self._offset - first * self.STRIDE)
        i = first
        while y < self.height() and i < self._rows_count:
            self.paint_row(p, i, y, self._hover == i)
            y += self.STRIDE
            i += 1
        p.end()
        return pm

    # ═════════════════ модель / геометрия ═══════════════════════════════

    def _content_h(self) -> int:
        return self._rows_count * self.STRIDE - (self.STRIDE - self.ROW_H) \
            if self._rows_count else 0

    def _max_offset(self) -> int:
        return max(0, self._content_h() - self.height())

    def _clamp_offset(self):
        self._offset = max(0, min(self._offset, self._max_offset()))

    def _index_at(self, y: float) -> int:
        yy = int(y + self._offset)
        if yy < 0:
            return -1
        i = yy // self.STRIDE
        if i >= self._rows_count:
            return -1
        if yy - i * self.STRIDE > self.ROW_H:
            return -1      # зазор между карточками
        return i

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_offset()

    # ═════════════════ прокрутка ═══════════════════════════════════════

    def _scroll(self, dy: int):
        off = max(0, min(self._offset + dy, self._max_offset()))
        if off != self._offset:
            self._offset = off
            self.update()

    def _show_sb(self):
        self._sb_hide.stop()
        if self._sb_fade:
            try:
                self._sb_fade.stop()
            except RuntimeError:
                pass
        self._sb_op = 1.0
        self.update()
        self._sb_hide.start()

    def _fade_sb(self, target: float):
        start = self._sb_op
        if start == target:
            return
        anim = QVariantAnimation(self, duration=160,
                                 startValue=start, endValue=target)
        def tick(v):
            self._sb_op = float(v)
            self.update()
        anim.valueChanged.connect(tick)
        try:
            anim.finished.connect(lambda: self._sb_fade and None)
        except RuntimeError:
            pass
        self._sb_fade = anim
        anim.start()

    def _thumb_rect(self) -> QRect:
        track = self.height() - 8
        h = max(24, int(track * self.height() / max(1, self._content_h())))
        max_off = self._max_offset()
        y = 4
        if max_off > 0:
            y = 4 + int((self._offset / max_off) * (track - h))
        return QRect(self.width() - SB_PAD - SB_W, y, SB_W, h)

    # ═════════════════ события мыши ════════════════════════════════════

    def wheelEvent(self, event):
        # Одно деление колеса — 2 строки (шаг строки у каждого наследника свой:
        # self.STRIDE). Было 180 px за щелчок — в логах и транспортах список
        # проматывало мимо нужной строки.
        self._scroll(wheel_delta_px(event.angleDelta().y(), self.STRIDE))

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
                page = max(self.STRIDE * 3, self.height() - self.STRIDE * 3)
                self._scroll(page if pos.y() < tr.top() else -page)
            return
        i = self._index_at(pos.y())
        if i >= 0:
            self._row_clicked(i)

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
        if i != self._hover:
            self._hover = i
            self.update()
        if i < 0:
            self.setCursor(Qt.ArrowCursor)
        else:
            self.setCursor(self._cursor_for_row(i))

    def mouseReleaseEvent(self, event):
        self._sb_drag = False

    def leaveEvent(self, event):
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    # ═════════════════ общая отрисовка ═════════════════════════════════

    def paintEvent(self, event):
        key = (self.width(), self.height(), self._offset, self._hover,
               self._rows_state_key(), self.devicePixelRatioF())
        if self._cache_pm is None or self._cache_key != key:
            self._cache_pm = self._render_cache()
            self._cache_key = key
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache_pm)
        # затухающий ползунок
        if self._sb_op > 0.01 and self._max_offset() > 0:
            c = QColor(theme.ACCENT3)
            c.setAlpha(int(140 * self._sb_op))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(self._thumb_rect(), 3, 3)

    # ═════════════════ переопределяется наследником ════════════════════

    def paint_row(self, p: QPainter, i: int, y: int, hover: bool):
        raise NotImplementedError

    def _row_clicked(self, i: int):
        pass

    def _cursor_for_row(self, i: int):
        return Qt.PointingHandCursor

    # ── утилиты для наследников ──

    @staticmethod
    def _qc(hexstr: str) -> QColor:
        return QColor(hexstr)

    @staticmethod
    def _pen(color, w=1.0) -> QPen:
        pen = QPen(RowCanvas._qc(color) if isinstance(color, str) else color)
        pen.setWidthF(w)
        return pen

    @staticmethod
    def _wrap2(text: str, fm: QFontMetrics, max_w: int) -> list[str]:
        """Перенос по словам максимум в 2 строки, вторая — с ellipsis."""
        if not text:
            return [""]
        if fm.horizontalAdvance(text) <= max_w:
            return [text]
        words = text.split()
        line1 = ""
        for j, w in enumerate(words):
            cand = (line1 + " " + w).strip()
            if fm.horizontalAdvance(cand) > max_w:
                break
            line1 = cand
        rest = " ".join(words[j:]) if j < len(words) else ""
        line2 = fm.elidedText(rest, Qt.ElideRight, max_w)
        return [line1, line2]
