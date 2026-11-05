"""
Раскладка-поток: элементы идут в строку и переносятся на следующую, когда не помещаются.
================================================================================

**Зачем.** Несколько вкладок UmbraNet чинили одну и ту же болезнь: ряд элементов
(кнопки, пары полей) держал минимальную ширину вкладки, и в узком окне последний
элемент уезжал за границу карточки, а вкладка либо распирала окно, либо обрезалась
краем. Первым это вылечили в «Сети и диагностике» (жалоба «всё очень криво
становится, когда уменьшаешь вкладку по горизонтали»), потом в «О программе» и
«Настройках».

**Почему отдельным модулем.** Раскладка нужна нескольким вкладкам. Две копии
одной раскладки — это две копии её ошибок: в первой версии, например, `minimumSize`
считался по всей строке, и вкладка всё равно распиралась.

Использование:

    row = FlowLayout(spacing=10)
    row.addWidget(button1)
    row.addWidget(button2)

У раскладки свои `hasHeightForWidth`/`heightForWidth`: без них Qt нарисовал бы ряд
в одну строку и вторая строка оказалась бы под карточкой, а не внутри неё.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem


class FlowLayout(QLayout):
    """Раскладка-поток: элементы переносятся на новую строку, когда не поместились.

    `margin` — отступ по всем сторонам (обычно 0: отступы задаёт карточка),
    `spacing` — зазор между элементами по горизонтали и вертикали.
    """

    def __init__(self, parent=None, margin: int = 0, spacing: int = 10):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    # ── обязательный интерфейс QLayout ──
    def addItem(self, item):
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._lay_out(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._lay_out(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        """Минимум — по самому широкому элементу, а не по сумме всей строки.

        Это важно: если считать по сумме, то ряд кнопок снова задаст вкладке
        минимальную ширину, и вся затея с переносом не сработает.
        """
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    # ── собственно раскладка ──
    def _lay_out(self, rect: QRect, test_only: bool) -> int:
        """Расставляет элементы; при `test_only` только считает нужную высоту."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        gap = self.spacing()
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width()
            if next_x > area.right() + 1 and line_height > 0:   # не помещается — на новую строку
                x = area.x()
                y += line_height + gap
                next_x = x + hint.width()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x + gap
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


def flow_row(*widgets, spacing: int = 10) -> FlowLayout:
    """Готовый ряд из виджетов, который переносится, когда места не хватает."""
    flow = FlowLayout(spacing=spacing)
    for w in widgets:
        flow.addWidget(w)
    return flow
