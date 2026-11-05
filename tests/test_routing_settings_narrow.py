"""
Проверки «Маршрутизации» и «Настроек» в узком окне.
===================================================

Проверяют оставшиеся пользовательские вкладки после вывода DNS-профилей из
навигации: на узком окне элементы не обрезаются, не выходят за карточки и не
создают горизонтальную прокрутку.

«Настройки» показывают только темы, поведение приложения и надёжность VPN.
Устаревшие DNS-значения остаются совместимыми с конфигурацией, но не имеют
элементов управления в этой вкладке. Тесты дополнительно страхуют это UI-правило.

Запуск: python -m pytest tests/test_routing_settings_narrow.py
"""

from __future__ import annotations

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
from PySide6.QtWidgets import (                                             # noqa: E402
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

#: Те же ширины, что в свипах по шапке, «Логам», «Сети» и «О программе».
WIDTHS = tuple(range(560, 1401, 20))


@pytest.fixture(scope="module")
def window():
    from umbranet.app import MainWindow
    w = MainWindow()
    w.show()
    yield w
    with suppress(RuntimeError):
        w.close()


@pytest.fixture(scope="module")
def views(window):
    """Открывает пользовательские вкладки, которым нужна проверка узкого окна."""
    window.sidebar.set_collapsed(True, animate=False)
    out = {}
    for key in ("routing", "settings"):
        window._show(key)                                  # noqa: SLF001
        settle(6)
        out[key] = window._views[key]                      # noqa: SLF001
    return out


def settle(times: int = 4) -> None:
    for _ in range(times):
        APP.processEvents()


def set_width(window, width: int) -> None:
    window.resize(width, 900)
    settle()


def inner_scroll(view) -> QScrollArea | None:
    return view.findChild(QScrollArea)


def cards_of(view) -> list[QWidget]:
    """Карточки вкладки. Тип именно RoundedPanel (QFrame даёт пустой список)."""
    from umbranet.widgets.rounded_panel import RoundedPanel
    root = inner_scroll(view)
    body = root.widget() if root is not None else view
    return [c for c in body.findChildren(RoundedPanel) if c.parentWidget() is body]


def all_labels(view) -> list[QLabel]:
    root = inner_scroll(view)
    body = root.widget() if root is not None else view
    return [lb for lb in body.findChildren(QLabel) if lb.isVisible() and lb.width() > 0]


# ── 1. Ничего не обрезано ни на одной ширине ────────────────────────────────

def test_dns_profiles_are_not_registered(window):
    """DNS-профили больше не создаются как скрытая страница приложения."""
    assert "profiles" not in window._views  # noqa: SLF001
    assert "profiles" not in window._pages  # noqa: SLF001


def test_settings_do_not_expose_legacy_dns_controls(window, views):
    """Обычные настройки не показывают старые DNS-поля, кэш или списки фильтрации."""
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    visible_text = "\n".join(label.text() for label in all_labels(view))
    forbidden = (
        "Локальный порт", "Upstream", "IPv6 DNS", "Приоритет IPv6",
        "Стратегия upstream", "Кэш DNS", "TTL", "blocklist", "allowlist",
    )
    assert not [item for item in forbidden if item in visible_text], visible_text


def test_no_text_is_cut_in_routing_and_settings(window, views):
    """Ни одна переносящаяся подпись не обрезана на 560–1400."""
    problems = []
    for key, view in views.items():
        for width in WIDTHS:
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            for lb in all_labels(view):
                if not lb.wordWrap() or not lb.text() or lb.text().startswith("<"):
                    continue
                need = lb.heightForWidth(lb.width())
                if need > lb.height() + 1:
                    problems.append(
                        f"{key} {width}: «{lb.text()[:34]}…» нужно {need}, есть {lb.height()}"
                    )
    assert not problems, "подписи обрезаны: " + "; ".join(problems[:6])


def test_toggle_labels_grow_in_narrow_window(window, views):
    """Подписи переключателей растут по тексту: в узком окне они выше, чем в широком.

    До правки высота была жёстко 22 px, и вторая строка текста пропадала целиком.
    """
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    set_width(window, 1280)
    label = view._toggle_labels["developer_mode"]          # noqa: SLF001
    wide = label.height()

    set_width(window, 560)
    narrow = label.height()

    assert narrow > 22, f"подпись осталась в одну строку ({narrow} px) — значит, режется"
    assert narrow >= wide, f"высота не растёт с сужением: {wide} → {narrow}"
    assert label.heightForWidth(label.width()) <= narrow + 1


def test_section_descriptions_grow(window, views):
    """Описание темы занимает столько строк, сколько нужно."""
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    set_width(window, 560)
    label = view._theme_desc                                # noqa: SLF001
    need = label.heightForWidth(label.width())
    assert need <= label.height() + 1, f"нужно {need}, есть {label.height()}"
    assert label.minimumHeight() != label.maximumHeight(), (
        f"высота жёсткая ({label.height()} px) — длинный текст будет обрезан"
    )
    assert label.sizePolicy().verticalPolicy() != QSizePolicy.Fixed, (
        "политика высоты Fixed — виджет не сможет вырасти"
    )


# ── 2. Ничего не вылезает и не появляется горизонтальной прокрутки ──────────

# Отдельно проверяется запас по ширине (test_settings_keeps_width_margin): вкладка
# должна не «еле влезать», а иметь 20 px свободы, иначе любая правка на волосок
# снова включает прокрутку.

def test_no_horizontal_scroll_in_routing_and_settings(window, views):
    """Горизонтальной прокрутки нет ни на одной проверяемой ширине."""
    problems = []
    for key, view in views.items():
        for width in WIDTHS:
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            scroll = inner_scroll(view)
            if scroll is None:
                continue
            maximum = scroll.horizontalScrollBar().maximum()
            if maximum > 0:
                problems.append(f"{key} {width}: прокрутка {maximum} px")
    assert not problems, "; ".join(problems[:6])


def test_nothing_sticks_out_of_cards(window, views):
    """Элементы не выходят за границу своей карточки."""
    problems = []
    for key, view in views.items():
        for width in (560, 700, 900, 1280):
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            for card in cards_of(view):
                for child in card.findChildren(QWidget):
                    if not child.isVisible() or child.width() <= 0:
                        continue
                    if child.parentWidget() is None or child.parentWidget() is card:
                        continue
                    right = child.mapTo(card, child.rect().topRight()).x()
                    if right > card.width() + 1:
                        text = child.text()[:24] if hasattr(child, "text") else ""
                        problems.append(f"{key} {width}: {type(child).__name__} "
                                        f"«{text}» выходит за карточку ({right} > {card.width()})")
    assert not problems, "; ".join(problems[:6])


def test_buttons_are_not_squeezed(window, views):
    """Кнопки не сжимаются до обрезков: их ширина не меньше нужной."""
    problems = []
    for key, view in views.items():
        for width in WIDTHS:
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            for btn in view.findChildren(QPushButton):
                if not btn.isVisible() or btn.width() <= 0 or not btn.text():
                    continue
                if btn.width() + 1 < btn.sizeHint().width():
                    problems.append(f"{key} {width}: «{btn.text()[:22]}» "
                                    f"{btn.width()} < {btn.sizeHint().width()}")
    assert not problems, "; ".join(problems[:6])


# ── 3. Заголовок «Настроек»: кнопки переезжают, а не сжимаются ──────────────

def test_settings_header_buttons_move_to_second_row(window, views):
    """В узком окне кнопки уходят под заголовок, в широком стоят в одну строку."""
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    head = view._head_bar                                  # noqa: SLF001
    title = head._title                                    # noqa: SLF001
    buttons = head._buttons                                # noqa: SLF001

    set_width(window, 1280)
    same_line = [b.mapTo(view, b.rect().center()).y() for b in buttons]
    assert max(same_line) - min(same_line) <= 2, "в широком окне кнопки должны быть в один ряд"
    assert buttons[0].width() + 1 >= buttons[0].sizeHint().width()

    set_width(window, 560)
    title_bottom = title.mapTo(view, title.rect().bottomLeft()).y()
    for btn in buttons:
        btn_top = btn.mapTo(view, btn.rect().topLeft()).y()
        assert btn_top >= title_bottom - 2, "в узком окне кнопки должны быть под заголовком"
        assert btn.width() + 1 >= btn.sizeHint().width(), (
            f"кнопка «{btn.text()}» сжата до обрезка: {btn.width()} < {btn.sizeHint().width()}"
        )
    assert title.width() + 1 >= title.sizeHint().width(), "заголовок обрезан"


def test_settings_header_does_not_overlap_first_card(window, views):
    """Второй ряд кнопок не наезжает на первую карточку (высота считается по раскладке)."""
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    head = view._head_bar                                  # noqa: SLF001
    # Порядок важен: сначала широко, потом узко — проверяется именно переход
    # «один ряд → второй ряд». Если окно уже узкое, перехода нет и поломка не видна.
    for width in (1280, 560, 900, 620, 700, 560):
        set_width(window, width)
        cards = cards_of(view)
        if not cards:
            continue
        top_card = min(cards, key=lambda c: c.mapTo(view, c.rect().topLeft()).y())
        head_bottom = head.mapTo(view, head.rect().bottomLeft()).y()
        card_top = top_card.mapTo(view, top_card.rect().topLeft()).y()
        assert head_bottom <= card_top + 1, (
            f"окно {width}: кнопки заголовка перекрывают карточку "
            f"({head_bottom} > {card_top})"
        )


# ── 4. Поля ввода: ужимаются, но не исчезают ────────────────────────────────

def test_fields_shrink_but_stay_usable(window, views):
    """Поле HTTPS-проверки остаётся пригодным в узком окне."""
    from umbranet.views.settings import _FIELD_MIN_W
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    field = view._transport_probe_url                       # noqa: SLF001

    set_width(window, 1280)
    wide = field.width()
    set_width(window, 560)
    least = min(field.maximumWidth(), _FIELD_MIN_W)
    assert field.width() >= least, f"поле ужалось до {field.width()} px (< {least})"
    assert field.width() <= field.maximumWidth(), "поле шире своего предела"
    assert field.width() <= wide, "в узком окне поле не должно быть шире, чем в широком"
    for visible_field in view.findChildren(QLineEdit):
        assert visible_field.width() <= visible_field.maximumWidth(), (
            f"поле шире своего предела: {visible_field.width()} > {visible_field.maximumWidth()}"
        )


def test_vpn_reliability_help_badges_are_compact_and_descriptive(window, views):
    """Подсказки не раздувают форму и объясняют проверку/запрет DIRECT."""
    from umbranet.views.settings import HINT_HOVER_DELAY_MS
    from umbranet.widgets.hover_hint import SHOW_DELAY_MS

    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    badges = [label for label in view.findChildren(QLabel) if label.text() == "?"]

    assert HINT_HOVER_DELAY_MS == SHOW_DELAY_MS == 180
    assert len(badges) == 2
    assert all(badge.width() == 18 and badge.height() == 18 for badge in badges)
    assert all(badge._show_delay.interval() == HINT_HOVER_DELAY_MS for badge in badges)  # noqa: SLF001
    tooltips = {badge.accessibleDescription() for badge in badges}
    assert any("проверяет TLS" in text for text in tooltips)
    assert any("не добавляет DIRECT" in text for text in tooltips)


def test_form_labels_keep_flexible_width_bounds(window, views):
    """Подписи VPN-полей ограничены, но не зафиксированы жёстко."""
    from umbranet.views.settings import _LABEL_MAX_W, _LABEL_MIN_W
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    texts = ("Тема приложения", "HTTPS-проверка сервера", "Режим автозамены", "Реакция на сбой")
    pick = lambda: [lb for lb in view.findChildren(QLabel) if lb.text() in texts]  # noqa: E731

    set_width(window, 1280)
    wide = {lb.text(): lb.width() for lb in pick()}
    assert set(wide) == set(texts), wide
    for label in pick():
        assert _LABEL_MIN_W <= label.width() <= _LABEL_MAX_W, label.text()
        assert label.minimumWidth() != label.maximumWidth(), (
            f"«{label.text()}»: ширина жёсткая ({label.width()} px) — ужиматься не сможет"
        )

    set_width(window, 560)
    for label in pick():
        assert _LABEL_MIN_W <= label.width() <= _LABEL_MAX_W, f"{label.text()}: {label.width()} px"


# ── 5. «Маршрутизация»: регрессия ───────────────────────────────────────────

def test_routing_tab_keeps_its_own_scroll(window, views):
    """«Маршрутизация» держится своего правила: своя прокрутка, горизонтальной нет.

    Эта вкладка исключена из автоматической обёртки приложением
    (NEVER_WRAP_IN_PAGE_SCROLL): у неё собственная прокрутка и перетаскиваемая
    правая панель (§16.1). Проверяем, что после правок соседних вкладок правило
    не сломалось и содержимое не распирает окно вбок.
    """
    from umbranet.app import MainWindow
    assert "routing" in MainWindow.NEVER_WRAP_IN_PAGE_SCROLL

    view = views["routing"]
    window._show("routing")                                # noqa: SLF001
    for width in WIDTHS:
        set_width(window, width)
        scroll = inner_scroll(view)
        assert scroll is not None, "у вкладки должна быть своя прокрутка"
        assert scroll.horizontalScrollBar().maximum() == 0, \
            f"окно {width}: появилась горизонтальная прокрутка"
        assert scroll.verticalScrollBar().maximum() >= 0


def test_flow_layout_is_shared(window, views):
    """Раскладка-поток живёт в общем модуле и используется нужными вкладками."""
    from umbranet.widgets.flow_layout import FlowLayout
    from umbranet.views.network import _FlowLayout as net_flow
    assert net_flow is FlowLayout, "в «Сети» должна использоваться общая раскладка"

    found = views["settings"].findChildren(FlowLayout)
    assert found, "вкладка settings не использует переносящуюся раскладку"


def test_flow_layout_wraps_and_reports_height():
    """Прямой тест раскладки: элементы переносятся, и высота это учитывает."""
    from umbranet.widgets.flow_layout import FlowLayout
    holder = QWidget()
    flow = FlowLayout(spacing=10)
    holder.setLayout(flow)
    for _ in range(4):
        btn = QPushButton("кнопка")
        btn.setFixedSize(120, 36)
        flow.addWidget(btn)
    holder.resize(500, 200)
    settle(3)

    assert flow.hasHeightForWidth(), (
        "раскладка не сообщает, что высота зависит от ширины: чужой родитель "
        "выделит ей одну строку, и вторая строка окажется под карточкой"
    )
    one_row = flow.heightForWidth(600)
    two_rows = flow.heightForWidth(300)
    assert one_row < two_rows, f"перенос не учитывается в высоте: {one_row} vs {two_rows}"
    xs = [it.geometry().x() for it in (flow.itemAt(i) for i in range(flow.count()))]
    assert max(xs) + 120 <= 500 + 1, "элемент вышел за пределы раскладки"
