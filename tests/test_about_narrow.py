"""
Тесты вкладки «О программе» в узком окне.
================================================================================

Та же болезнь, что нашлась в «Сети и диагностике» (раздел 26 журнала): подписи с
жёсткой высотой обрезают текст, а одна длинная строка может держать ширину всей
вкладки.

Замеры до правки:

  • «💡 Где что находится» — шесть строк текста при жёсткой высоте 110 px, а нужно
    214: две последние строки не показывались никогда.
  • Описание программы — нужно 56 px при 40.
  • Значение «Предстартовая проверка» — «Нет прав администратора: UmbraNet не
    сможет прописать системный DNS и занять порт 53. Запустите через start.bat…» —
    не переносилось по словам и **держало минимальную ширину вкладки 1257 px**. В
    окне уже 900 px появлялась горизонтальная прокрутка, а текст уезжал за край
    (на блоках заметно по обрезанным «за…» и «почем…»).

Запуск: python -m pytest tests/test_about_narrow.py
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
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QScrollArea,
    QWidget,
)

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

WIDTHES = tuple(range(560, 1401, 20))


@pytest.fixture(scope="module")
def window():
    from umbranet.app import MainWindow
    w = MainWindow()
    w.show()
    yield w
    with suppress(RuntimeError):
        w.close()


def settle(times: int = 4) -> None:
    for _ in range(times):
        APP.processEvents()


@pytest.fixture(scope="module")
def about_view(window):
    window.sidebar.set_collapsed(True, animate=False)
    window._show("about")
    settle(6)
    return window._views["about"]


def page_parts(view) -> tuple[QScrollArea, QWidget]:
    """Прокрутка вкладки (если есть) и её содержимое."""
    scroll = view.findChild(QScrollArea)
    if scroll is None:
        return None, view
    return scroll, scroll.widget()


def set_width(window, width: int) -> None:
    window.resize(width, 880)
    settle()


def label_with(view: QWidget, needle: str) -> QLabel:
    for lbl in view.findChildren(QLabel):
        if needle in lbl.text():
            return lbl
    raise AssertionError(f"подпись «{needle}» не найдена")


# ── 1. Тексты не обрезаются ─────────────────────────────────────────────────

def test_no_label_is_cut(window, about_view):
    """Ни одна переносящаяся подпись не обрезана ни на одной ширине окна."""
    problems = []
    for width in WIDTHES:
        set_width(window, width)
        _, body = page_parts(about_view)
        for lbl in body.findChildren(QLabel):
            if not lbl.wordWrap() or not lbl.text() or lbl.width() <= 0:
                continue
            need = lbl.heightForWidth(lbl.width())
            if need > lbl.height() + 1:
                problems.append(
                    f"окно={width}: «{lbl.text()[:34]}…» нужно {need} px, есть {lbl.height()}"
                )
    assert not problems, "подписи обрезаны: " + "; ".join(problems[:6])


def help_label(view) -> QLabel:
    """Подпись со списком «Где что находится» (одна на всю карточку)."""
    for lbl in view.findChildren(QLabel):
        if "Маршрутизация" in lbl.text() and "Настройки" in lbl.text():
            return lbl
    raise AssertionError("текст-список не найден")


def test_help_list_is_fully_visible(window, about_view):
    """Список «Где что находится» виден целиком — все шесть пунктов.

    До правки под отведённые 110 px попадало три с половиной пункта: остальные
    обрезались, и до них нельзя было долистать (это не прокрутка содержимого, а
    именно отсечение по высоте).
    """
    set_width(window, 700)
    lab = help_label(about_view)
    assert lab.wordWrap(), "список не переносится"
    for entry in ("Маршрутизация", "Сеть и диагностика", "AI-стратегии", "DNS-профили", "Логи", "Настройки"):
        assert entry in lab.text(), f"в тексте пропал пункт «{entry}»"
    need = lab.heightForWidth(lab.width())
    assert lab.height() >= need - 1, (
        f"список обрезан: нужно {need} px, есть {lab.height()}"
    )


def test_help_rows_use_emoji_and_blue_names(about_view):
    """Вкладки в списке подсвечены голубым и со смайликами (мини-редизайн).

    Имена вкладок набраны голубым (about._HELP_TAB_COLOR) и начинаются со
    смайлика из бокового меню — список перестал быть «серой простынёй» и пункты
    узнаются по тем же иконкам, что и вкладки.
    """
    from umbranet.views import about as about_mod

    html = help_label(about_view).text()
    assert about_mod._HELP_TAB_COLOR in html, "голубой цвет названий вкладок пропал"
    for emoji, name in (
        ("🔀", "Маршрутизация"),
        ("🤖", "Сеть и диагностика"),
        ("🧪", "AI-стратегии"),
        ("🧩", "DNS-профили"),
        ("📑", "Логи"),
        ("⚙", "Настройки"),
    ):
        assert f"{emoji} {name}" in html, f"в списке нет смайлика у «{name}»"
        assert (
            f"<span style='color:{about_mod._HELP_TAB_COLOR};'>{emoji} {name}</span>"
        ) in html, f"«{name}» не подсвечено голубым"


def test_update_button_is_static_not_stretched(window, about_view):
    """«Проверить обновления» — статичная кнопка по тексту, а не во всю карточку.

    С политикой размера по умолчанию (Minimum) кнопка занимала всю строку и
    «продлевалась бесконечно» при растягивании окна. Теперь ширина — sizeHint и
    не меняется от ширины окна.
    """
    btn = about_view._check_release
    widths = []
    for width in (1280, 900, 700):
        set_width(window, width)
        widths.append(btn.width())
        assert btn.width() <= btn.sizeHint().width() + 1, (
            f"окно={width}: кнопка растянулась до {btn.width()} px "
            f"при sizeHint {btn.sizeHint().width()} px"
        )
    assert widths[0] == widths[-1], f"кнопка «дышит» при ресайзе: {widths}"


def test_hero_description_grows(window, about_view):
    """Описание программы и строка про локальные данные растут под текст."""
    desc = label_with(about_view, "Локальный DNS-инструмент")
    privacy = label_with(about_view, "хранятся локально")

    set_width(window, 1280)
    wide = (desc.height(), privacy.height())
    set_width(window, 560)
    narrow = (desc.height(), privacy.height())

    assert narrow[0] > 40, f"описание осталось в две строки ({narrow[0]} px) — значит, режется"
    assert narrow[0] >= wide[0], f"высота описания не растёт: {wide[0]} → {narrow[0]}"
    assert narrow[1] >= privacy.heightForWidth(privacy.width()) - 1, "строка про локальные данные обрезана"


# ── 2. Вкладку не распирает одна длинная строка ─────────────────────────────

def test_status_value_does_not_widen_page(window, about_view):
    """Длинное значение «Предстартовая проверка» не держит ширину вкладки.

    Именно оно делало вкладке минимум 1257 px: одна строка, не переносящаяся по
    словам, тянула за собой карточку, карточка — вкладку, вкладка — прокрутку.
    """
    set_width(window, 700)
    value = None
    for lbl in about_view.findChildren(QLabel):
        if lbl.text().startswith("Нет прав администратора"):
            value = lbl
    if value is None:
        pytest.skip("в этой среде предстартовая проверка без предупреждения")

    assert value.wordWrap(), "значение не переносится по словам"
    assert value.height() >= value.heightForWidth(value.width()) - 1, (
        f"значение обрезано: нужно {value.heightForWidth(value.width())}, есть {value.height()}"
    )


def test_page_min_width_is_small(window, about_view):
    """Минимальная ширина вкладки — сотни пикселей, а не 1257, как было."""
    set_width(window, 560)
    _, body = page_parts(about_view)
    need = body.minimumSizeHint().width()
    assert need <= 400, f"вкладку распирает содержимым: {need} px"
    assert need <= window.SCROLL_MIN_PAGE_W, (
        f"{need} px больше порога прокрутки {window.SCROLL_MIN_PAGE_W}"
    )


def test_no_horizontal_scroll(window, about_view):
    """Горизонтальной прокрутки нет ни на одной ширине окна."""
    problems = []
    for width in WIDTHES:
        set_width(window, width)
        scroll, body = page_parts(about_view)
        if scroll is None:
            if body.minimumSizeHint().width() > body.width():
                problems.append(f"окно={width}: содержимое шире вкладки")
            continue
        if scroll.horizontalScrollBar().isVisible():
            problems.append(f"окно={width}: появилась горизонтальная прокрутка")
        if body.minimumSizeHint().width() > scroll.viewport().width():
            problems.append(f"окно={width}: содержимое шире окна")
    assert not problems, "; ".join(problems[:5])


@pytest.mark.parametrize("state", ["idle", "checking", "available", "current", "no_releases", "error"])
def test_content_fits_page_width(window, about_view, monkeypatch, state):
    """Visible content fits, including the release button when an update exists.

    Qt excludes hidden widgets from layout; a never-shown button can still have
    its default 640px geometry. That is not visible overflow. Test every update
    state so ignoring hidden widgets cannot hide an oversized *visible* button.
    """
    from unittest.mock import Mock
    from core.update_checker import UpdateResult
    from umbranet.views import about

    checker = Mock()
    checker.busy = state == "checking"
    checker.result = UpdateResult(
        state, "0.4.0" if state == "available" else "",
        "https://github.com/X-302-X/UmbraNet/releases/tag/v0.4.0" if state == "available" else "",
        "Доступна версия 0.4.0. Установка вручную." if state == "available" else "Проверка обновлений: " + state,
    )
    monkeypatch.setattr(about.ea, "get_update_checker", lambda: checker)
    about_view._refresh_update_status()
    try:
        problems = []
        for width in (560, 700, 900, 1280):
            set_width(window, width)
            _, body = page_parts(about_view)
            assert about_view._open_release.isVisibleTo(body) == (state == "available")
            limit = body.width()
            for child in body.findChildren(QLabel) + body.findChildren(QPushButton):
                if not child.isVisibleTo(body):
                    continue
                pos = child.mapTo(body, child.rect().topLeft())
                if pos.x() < 0 or pos.x() + child.width() > limit + 1:
                    problems.append(
                        f"окно={width}, состояние={state}: «{child.text()[:26]}» вылез "
                        f"({pos.x() + child.width()} > {limit})"
                    )
        assert not problems, "элементы вылезают за содержимое: " + "; ".join(problems[:5])
    finally:
        # The page is shared by this module's tests; leave it in its initial state.
        checker.result = UpdateResult()
        checker.busy = False
        about_view._refresh_update_status()


def test_cards_do_not_ride_over_each_other(window, about_view):
    """Карточки не наезжают друг на друга: подписи растут, а раскладка за ними."""
    for width in (560, 700, 900, 1280):
        set_width(window, width)
        _, body = page_parts(about_view)
        lay = body.layout()
        tops = []
        for i in range(lay.count()):
            card = lay.itemAt(i).widget()
            if card is None:
                continue
            tops.append(card.mapTo(body, card.rect().topLeft()).y())
        for prev_y, next_y in itertools.pairwise(sorted(tops)):
            assert next_y - prev_y >= 60, (
                f"окно={width}: карточки наехали друг на друга ({prev_y} → {next_y})"
            )
