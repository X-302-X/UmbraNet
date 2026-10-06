"""
Тесты вкладки «Сеть и диагностика» в узком окне.
================================================================================

Жалоба пользователя: «во вкладке сеть и диагностика всё очень криво становится,
когда уменьшаешь вкладку по горизонтали».

Замеры до правки (окно 560 px) показали три поломки, и все три — из-за
фиксированных размеров:

  1. **Текст обрублен.** У трёх подписей стояла жёсткая высота: у текста
     авто-диагностики нужно 84 px, стояло 42; у блока DPI нужно 70, стояло 48.
     Лишние строки обрезались посередине, и подпись обрывалась на полуслове
     («…системный DNS и занят»).
  2. **Кнопки уезжали за край карточки.** Ряд «Сбросить DNS-кэш · Обновить защиту
     от подмен · Откатить сеть» требует 531 px, ряд в «Автодиагностике» — 475 px.
     Последняя кнопка вылезала за границу карточки.
  3. **Заголовок держал ширину.** Длинный заголовок карточки не переносился по
     словам и требовал больше места, чем было у карточки, — обрезался краем.

Заодно нашлась четвёртая: минимальная ширина вкладки была 553 px при пороге
прокрутки 620 — содержимое распирало вкладку, и в окне 560 px всё уезжало вправо
без всякой прокрутки (потому и «криво»).

Запуск: python -m pytest tests/test_network_narrow.py
"""

from __future__ import annotations

import itertools
import os
import pathlib
import sys
from contextlib import suppress

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QScrollArea,
    QWidget,
)

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# Ширины окна: от самого узкого состояния до широкого, где вкладка выглядит
# «как обычно». Шаг 20 px — тот же, что в свипах по шапке и «Логам».
WIDTHES = tuple(range(560, 1401, 20))


@pytest.fixture(scope="module")
def window():
    from umbranet.app import MainWindow
    from umbranet.views.network import NetworkView

    def fixed_health_report(view):
        # This module tests geometry, not the CI runner's network. A healthy
        # Windows host can return a short two-line message, while Linux returns
        # longer warnings. Async completion also used to change the text between
        # wide/narrow measurements. Use the real renderer with identical input.
        view._on_health_ready({
            "score": 60,
            "state": "warn",
            "title": "Проверка сетевого подключения",
            "checks": [{
                "status": "warn",
                "title": "DNS-провайдер",
                "detail": (
                    "Проверьте доступность выбранного DNS-провайдера и параметры "
                    "активного сетевого адаптера. Если соединение нестабильно, "
                    "выполните диагностику и сохраните отчёт перед изменением настроек."
                ),
            }],
        })

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(NetworkView, "_refresh_health_score", fixed_health_report)
        w = MainWindow()
        w.show()
        try:
            yield w
        finally:
            with suppress(RuntimeError):
                w.close()


def settle(times: int = 4) -> None:
    for _ in range(times):
        APP.processEvents()


@pytest.fixture(scope="module")
def net_view(window):
    window.sidebar.set_collapsed(True, animate=False)
    window._show("network")
    settle(6)
    return window._views["network"]


def body_of(view) -> QWidget:
    """Содержимое вкладки: то, что лежит внутри её собственной прокрутки."""
    scroll = view.findChild(QScrollArea)
    return scroll.widget()


def set_width(window, width: int) -> None:
    window.resize(width, 900)
    settle()


def cards_of(view) -> list[QWidget]:
    """Карточки вкладки. Тип именно RoundedPanel: это QWidget, а не QFrame, —
    на QFrame выборка выходит пустой, и проверки становятся пустышками."""
    from umbranet.widgets.rounded_panel import RoundedPanel
    body = body_of(view)
    return [c for c in body.findChildren(RoundedPanel) if c.parentWidget() is body]


# ── 1. Тексты растут, а не обрезаются ───────────────────────────────────────

def test_no_wrapped_label_is_cut(window, net_view):
    """Ни одна переносящаяся подпись не обрезана ни на одной ширине окна.

    Проверяем по heightForWidth: сколько высоты тексту нужно при текущей ширине
    против того, сколько у виджета есть. До правки разница доходила до 42 px
    (целая строка текста), и подпись обрывалась на полуслове.
    """
    problems = []
    for width in WIDTHES:
        set_width(window, width)
        for card in cards_of(net_view):
            for label in card.findChildren(QLabel):
                if not label.wordWrap() or not label.text() or label.width() <= 0:
                    continue
                need = label.heightForWidth(label.width())
                if need > label.height() + 1:
                    problems.append(
                        f"окно={width}: «{label.text()[:32]}…» нужно {need} px, есть {label.height()}"
                    )
    assert not problems, "подписи обрезаны: " + "; ".join(problems[:6])


def test_health_text_grows_with_content(window, net_view):
    """Текст авто-диагностики занимает столько строк, сколько нужно, — и растёт.

    Раньше высота была жёстко 42 px: всё, что длиннее двух строк, исчезало. Теперь
    это минимум, а не предел, поэтому в узком окне подпись выше, чем в широком.
    """
    text = net_view._health_text.text()
    set_width(window, 1280)
    wide = net_view._health_text.height()
    set_width(window, 560)
    narrow = net_view._health_text.height()
    needed = net_view._health_text.heightForWidth(net_view._health_text.width())

    assert net_view._health_text.text() == text, "содержимое изменилось между измерениями"
    assert needed > 42, "тест должен проверять текст длиннее прежних двух строк"
    assert narrow >= needed - 1, "текст не помещается в рассчитанную высоту"
    assert narrow > 42, f"в узком окне текст остался в две строки ({narrow} px) — значит, режется"
    assert narrow > wide, f"высота не растёт с сужением: {wide} → {narrow}"



def test_health_title_wraps_without_widening_page(window, net_view):
    title = net_view._health_title
    original = title.text()
    try:
        title.setText("Проверка доступности DNS-провайдеров и параметров сетевого подключения")
        set_width(window, 560)
        assert title.wordWrap(), "длинный заголовок диагностики должен переноситься"
        needed = title.heightForWidth(title.width())
        assert needed > title.fontMetrics().height(), "заголовок должен занять несколько строк"
        assert title.height() >= needed - 1, "заголовок обрезан по высоте"
        body = body_of(net_view)
        assert body.minimumSizeHint().width() <= 400, "заголовок расширил вкладку"
    finally:
        title.setText(original)
        settle()

def test_long_status_grows_card_instead_of_being_cut(window, net_view):
    """Длинный текст в подписи растягивает карточку, а не режется её границей.

    Сценарий из жизни: при смене транспорта описание DPI становится длиннее.
    """
    card = net_view._dpi_text.parentWidget()
    original = net_view._dpi_text.text()
    set_width(window, 900)                     # обе мерки — на одной ширине окна
    try:
        net_view._dpi_text.setText("Режим: Combo")      # короткий текст — узкая карточка
        settle(6)
        before = card.height()
        net_view._dpi_text.setText("Режим: Combo • Стратегия: uz1 • "
                                   + "очень длинное пояснение, " * 12)
        settle(6)
        need = net_view._dpi_text.heightForWidth(net_view._dpi_text.width())
        assert net_view._dpi_text.height() >= need - 1, "длинный текст обрезан"
        assert card.height() > before, (
            f"карточка не выросла под текст: {before} → {card.height()}"
        )
    finally:
        net_view._dpi_text.setText(original)
        set_width(window, 900)


def test_all_long_labels_wrap(window, net_view):
    """Все длинные подписи вкладки переносятся по строкам.

    Для непереносящейся подписи Qt не считает heightForWidth — текст просто
    обрезается по ширине карточки, и увидеть это можно только глазами. Поэтому
    проверяем сам признак переноса: он и есть контракт этих подписей.
    """
    for label in (net_view._answer_state, net_view._answer_details,
                  net_view._answer_counters):
        assert label.wordWrap(), f"подпись «{label.text()[:30]}…» не переносится"

    long_labels = 0
    for card in cards_of(net_view):
        for label in card.findChildren(QLabel):
            if len(label.text()) > 60:
                assert label.wordWrap(), (
                    f"длинная подпись не переносится: «{label.text()[:40]}…»"
                )
                long_labels += 1
    assert long_labels >= 4, f"нашли всего {long_labels} длинных подписей — выборка пустая?"


def test_card_title_wraps_instead_of_widening_card(window, net_view):
    """Заголовок карточки переносится по словам, а не держит ширину карточки.

    Раньше длинный заголовок карточки требовал 383 px — больше, чем карточка в
    узком окне, — и обрезался краем. Проверяем на заголовке «Быстрые инструменты»,
    подставляя ему такое же длинное название.
    """
    set_width(window, 560)
    title = None
    for card in cards_of(net_view):
        for label in card.findChildren(QLabel):
            if "Быстрые инструменты" in label.text():
                title = label
    assert title is not None, "заголовок карточки не найден"
    assert title.wordWrap(), "заголовок не переносится"
    assert title.width() <= body_of(net_view).width(), "заголовок шире содержимого вкладки"

    original = title.text()
    try:
        title.setText("🧰 Быстрые инструменты с очень длинным названием, "
                      "которое в узком окне обязано переноситься по словам")
        settle(6)
        needed = title.heightForWidth(title.width())
        assert needed > title.fontMetrics().height(), "длинный заголовок не перенёсся"
        assert title.height() >= needed - 1, "длинный заголовок обрезан по высоте"
        assert title.width() <= body_of(net_view).width(), "длинный заголовок шире содержимого вкладки"
    finally:
        title.setText(original)
        settle()


# ── 2. Кнопки переносятся, ничего не уезжает за карточку ────────────────────

def test_nothing_goes_outside_its_card(window, net_view):
    """Правый край ни одного элемента не выходит за границу его карточки.

    До правки последняя кнопка ряда уезжала за край на 20–40 px, а весь ряд
    распирал вкладку: правый край доходил до 537 px при доступных 522.
    """
    problems = []
    for width in WIDTHES:
        set_width(window, width)
        for card in cards_of(net_view):
            limit = card.width() - 16                     # внутренние отступы карточки
            for child in card.findChildren(QLabel) + card.findChildren(QPushButton):
                pos = child.mapTo(card, child.rect().topLeft())
                right = pos.x() + child.width()
                if right > limit + 1:
                    problems.append(
                        f"окно={width}: «{child.text()[:26]}» вылез ({right} > {limit})"
                    )
    assert not problems, "элементы вылезают за карточку: " + "; ".join(problems[:6])


def test_no_button_text_is_cut(window, net_view):
    """Подписи кнопок не обрезаны: кнопка не у́же своего естественного размера.

    Кнопки этой вкладки ужимать нечем (в отличие от шапки и «Логов»), поэтому
    ширина ниже sizeHint означает ровно одно: ряд не поместился и Qt обрезал текст.
    """
    problems = []
    for width in WIDTHES:
        set_width(window, width)
        for card in cards_of(net_view):
            for btn in card.findChildren(QPushButton):
                if btn.width() + 1 < btn.sizeHint().width():
                    problems.append(
                        f"окно={width}: «{btn.text()[:26]}» есть {btn.width()}, "
                        f"нужно {btn.sizeHint().width()}"
                    )
    assert not problems, "подписи кнопок обрезаны: " + "; ".join(problems[:6])


def test_tools_buttons_wrap_to_second_row(window, net_view):
    """В узком окне ряд кнопок встаёт в две строки, а не обрезается.

    Проверяем по положению: «Откатить сеть» оказывается ниже «Сбросить DNS-кэш».
    """
    set_width(window, 560)
    card = net_view._btn_flush.parentWidget()
    first = net_view._btn_flush.mapTo(card, net_view._btn_flush.rect().topLeft())
    last = net_view._btn_restore_net.mapTo(card, net_view._btn_restore_net.rect().topLeft())
    assert last.y() > first.y(), "кнопки остались в одну строку и вылезли за карточку"
    assert last.x() < first.x() + 10, "перенесённая кнопка должна начинать новую строку"


def test_tools_buttons_stay_in_one_row_when_wide(window, net_view):
    """В широком окне ряд кнопок остаётся одной строкой — привычный вид."""
    set_width(window, 1280)
    card = net_view._btn_flush.parentWidget()
    ys = {b.mapTo(card, b.rect().topLeft()).y()
          for b in (net_view._btn_flush, net_view._btn_bogus_update, net_view._btn_restore_net)}
    assert len(ys) == 1, f"в широком окне кнопки разъехались по строкам: {ys}"


def test_button_row_height_follows_rows(window, net_view):
    """Высота ряда кнопок соответствует числу строк, а не одной строке.

    Это проверка того, что раскладка-поток (FlowLayout) отдаёт настоящую высоту:
    без hasHeightForWidth карточка нарисовала бы ряд в одну строку и вторая строка
    кнопок накрыла бы нижнюю часть карточки.
    """
    from umbranet.views.network import _FlowLayout

    card = net_view._btn_doctor.parentWidget()
    flows = [lay for lay in card.findChildren(_FlowLayout)]
    assert flows, "ряд кнопок собран не раскладкой-потоком"

    set_width(window, 1280)
    wide_flow = flows[0].heightForWidth(card.width() - 32)
    set_width(window, 560)
    narrow_flow = flows[0].heightForWidth(card.width() - 32)

    one_button = net_view._btn_doctor.height()
    assert wide_flow == one_button, f"в широком окне ряд должен быть в одну строку ({wide_flow})"
    assert narrow_flow >= 2 * one_button, (
        f"в узком окне ряд должен занять две строки, а занял {narrow_flow} px"
    )
    assert flows[0].geometry().height() >= narrow_flow, (
        "карточка не выделила места под вторую строку кнопок"
    )


def test_buttons_stay_inside_their_row(window, net_view):
    """Кнопки не выходят за границы своего ряда — даже когда ряд в две строки.

    Ряд обязан честно занимать две строки по высоте. Если раскладка-поток считает
    высоту неправильно (или не сообщает, что умеет переносить), вторая строка
    оказывается ниже выделенного места: карточка накрывает кнопки, а кнопки —
    следующую карточку.
    """
    from umbranet.views.network import _FlowLayout

    problems = []
    for width in (560, 640, 720, 900, 1280):
        set_width(window, width)
        for card in cards_of(net_view):
            for flow in card.findChildren(_FlowLayout):
                box = flow.geometry()
                for i in range(flow.count()):
                    item = flow.itemAt(i)
                    btn = item.widget() if item is not None else None
                    if btn is None:
                        continue
                    pos = btn.mapTo(card, btn.rect().topLeft())
                    if (pos.y() < box.y() - 1 or pos.x() < box.x() - 1
                            or pos.y() + btn.height() > box.y() + box.height() + 1
                            or pos.x() + btn.width() > box.x() + box.width() + 1):
                        problems.append(
                            f"окно={width}: «{btn.text()[:24]}» вне своего ряда "
                            f"({pos.x()},{pos.y()} {btn.width()}x{btn.height()} "
                            f"при ряде {box.x()},{box.y()} {box.width()}x{box.height()})"
                        )
    assert not problems, "кнопки вылезают за ряд: " + "; ".join(problems[:5])


def test_long_state_text_does_not_widen_card(window, net_view):
    """Длинный статус («отвечает запасной провайдер …») не распирает карточку.

    Текст состояния — единственная подпись вкладки, которую формирует ядро, и она
    бывает длинной. Переноситься она обязана: иначе её ширина становится
    минимальной шириной карточки и всего содержимого вкладки.
    """
    card = net_view._answer_state.parentWidget()
    original = net_view._answer_state.text()
    set_width(window, 560)
    try:
        net_view._answer_state.setText(
            "🟡 Отвечает запасной провайдер: some-provider.example.com · DoH (HTTPS) "
            "вместо DoT (TLS)"
        )
        settle(6)
        need = card.minimumSizeHint().width()
        assert need <= 400, f"карточку распирает длинный статус: {need} px"
        assert net_view._answer_state.height() > 30, (
            f"статус остался в одну строку ({net_view._answer_state.height()} px)"
        )
    finally:
        net_view._answer_state.setText(original)
        set_width(window, 560)


# ── 3. Вкладка ужимается, содержимое не распирает её ────────────────────────

def test_page_min_width_fits_scroll_threshold(window, net_view):
    """Минимальная ширина вкладки укладывается в порог прокрутки.

    Было 553 px (при пороге 620 вкладка ещё не оборачивалась, но содержимое в
    окне 560 px уезжало вправо без прокрутки): ширина вкладки упиралась в кнопки.
    """
    set_width(window, 560)
    need = body_of(net_view).minimumSizeHint().width()
    assert need <= window.SCROLL_MIN_PAGE_W, (
        f"вкладке нужно {need} px — это больше порога прокрутки {window.SCROLL_MIN_PAGE_W}"
    )
    assert need <= 400, f"вкладка всё ещё распирается содержимым: {need} px"


def test_page_has_no_horizontal_scroll(window, net_view):
    """Горизонтальной прокрутки у вкладки нет: всё влезает по ширине."""
    problems = []
    for width in WIDTHES:
        set_width(window, width)
        scroll = net_view.findChild(QScrollArea)
        if scroll.horizontalScrollBar().isVisible():
            problems.append(f"окно={width}: появилась горизонтальная прокрутка")
        if body_of(net_view).minimumSizeHint().width() > scroll.viewport().width():
            problems.append(f"окно={width}: содержимое шире окна")
    assert not problems, "; ".join(problems[:5])


def test_cards_do_not_overlap(window, net_view):
    """Карточки не наезжают друг на друга при сужении.

    Если раскладка-поток врёт про свою высоту, следующая карточка рисуется поверх
    второй строки кнопок — глазами это выглядит как «всё поехало».
    """
    for width in (560, 700, 900, 1280):
        set_width(window, width)
        cards = cards_of(net_view)
        tops = sorted(c.mapTo(body_of(net_view), c.rect().topLeft()).y() for c in cards)
        for prev_y, next_y in itertools.pairwise(tops):
            assert next_y - prev_y >= 60, (
                f"окно={width}: карточки наехали друг на друга ({prev_y} → {next_y})"
            )


# ── 4. Раскладка-поток ──────────────────────────────────────────────────────

def test_flow_layout_wraps_and_reports_height():
    """Прямой тест раскладки-потока: переносит элементы и честно считает высоту."""
    from umbranet.views.network import _FlowLayout

    host = QWidget()
    host.resize(300, 200)
    flow = _FlowLayout(spacing=10)
    host.setLayout(flow)
    buttons = [QPushButton("кнопка " + "x" * 8) for _ in range(4)]
    for b in buttons:
        flow.addWidget(b)
    host.show()
    APP.processEvents()

    one_row = max(b.sizeHint().width() for b in buttons)
    assert flow.heightForWidth(one_row * 4 + 30) == max(b.sizeHint().height() for b in buttons), \
        "в одну строку высота должна быть равна высоте кнопки"
    assert flow.heightForWidth(one_row + 1) > 2 * buttons[0].sizeHint().height(), \
        "тесная ширина должна давать несколько строк"
    assert flow.minimumSize().width() <= one_row + 2, "минимум должен быть по самому широкому элементу"
    assert flow.count() == 4 and flow.itemAt(0) is not None and flow.takeAt(9) is None
    assert flow.sizeHint().width() <= one_row + 2, "подсказка размера не должна требовать всю строку"
    assert isinstance(flow.minimumSize(), QSize)

    with suppress(RuntimeError):
        host.close()


# ── 2. Жалоба 2026-10-06: «текст съезжает, когда уменьшаешь страницу» ────────
# Старые тесты гоняли только ширину при высоте 900. Проверяем перекрытие
# соседних подписей (настоящее «съезжание») на маленьких высотах тоже.

def test_no_labels_overlap(window, net_view):
    """Ни одна подпись не перекрывает соседнюю ни на одной геометрии окна.

    «Съезжание» глазами = прямоугольки текстов накладываются друг на друга.
    Проверяем попарно по глобальным координатам для ширины и высоты вместе.
    """
    problems = []
    for width in (560, 760, 960, 1280):
        for height in (560, 640, 720, 900):
            window.resize(width, height)
            settle(6)
            for card in cards_of(net_view):
                labels = [
                    lb for lb in card.findChildren(QLabel)
                    if lb.text() and lb.isVisible()
                ]
                for a, b in itertools.combinations(labels, 2):
                    rect_a = QRect(a.mapToGlobal(QPoint(0, 0)), a.size())
                    rect_b = QRect(b.mapToGlobal(QPoint(0, 0)), b.size())
                    inter = rect_a.adjusted(1, 1, -1, -1) & rect_b.adjusted(1, 1, -1, -1)
                    if not inter.isEmpty():
                        problems.append(
                            f"{width}x{height}: «{a.text()[:18]}…» × «{b.text()[:18]}…»"
                        )
    assert not problems, "подписи перекрываются: " + "; ".join(problems[:6])
