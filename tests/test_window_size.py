"""
Тесты размера окна: ужимаемость до половины экрана и запоминание размера.

Две жалобы по факту работы на Windows:

  1. «Окно слишком большое, нельзя уменьшить в два раза; раскладка Windows
     ("половина экрана", "четверть") не работает — окно съедает всё место».
     Причина: setMinimumSize(1100x680). На экране 1920x1080 половина — это 960 px,
     а окно отказывалось ужиматься ниже 1100. Плюс содержимое тяжёлых вкладок
     (библиотека AI-стратегий — 965 px, маршрутизация — 749 px) физически не
     сжималось, и Qt держал окно по содержимому.
     Решение: маленький минимум + прокрутка для вкладок, чьё содержимое не
     сжимается (и по ширине, и по высоте) +
     адаптивная шапка (в узком окне второстепенные надписи прячутся, меню
     сворачивается в иконки, верхняя панель встаёт в два ряда).

  2. «Программа не запоминает последний размер окна».
     Решение: размер/положение/«развёрнуто» хранятся в общем состоянии UI
     (core/ui_state.py), сохраняются при изменении (с паузой), при сворачивании
     в трей и при закрытии, восстанавливаются при старте — с проверкой, что окно
     не окажется за пределами экрана после смены мониторов.

Запуск: python -m pytest tests/test_window_size.py
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# Экран в песочнице маленький (800x800), поэтому «широкое окно» эмулируем
# уменьшением порога: логика та же, а размер влезает в доступную область.
NARROW_WINDOW_WIDTH = 1000


# Окно создаём ОДИН раз на весь модуль и переиспользуем.
# Почему не как в остальных тестах: MainWindow поднимает тренарный движок и
# таймеры (singleShot на health-проверку). Несколько окон подряд в одном
# процессе PySide6 приводят к падению при сборке мусора — проверено: сегфолт на
# шестом окне. Переиспользование одного окна убирает проблему и не мешает
# проверкам: логика «следующий запуск» воспроизводится вызовом
# _restore_window_geometry() на том же объекте (тот же путь кода).
@pytest.fixture(scope="module")
def window():
    import app as app_mod
    win = app_mod.MainWindow()
    win.resize(800, 640)
    win.show()
    for _ in range(5):
        APP.processEvents()
    yield win
    try:
        win.hide()
        win.deleteLater()
        for _ in range(3):
            APP.processEvents()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_window(window):
    """Каждый тест начинает с одинакового размера окна и с ТЕМ ЖЕ состоянием панели.

    Панель — состояние, которое тесты меняют, а окно у нас одно на весь модуль
    (см. фикстуру window). Поэтому состояние, каким тест его получил, возвращаем
    на место после теста: иначе результат зависел бы от порядка выполнения.
    Именно «вернуть как было», а не «свернуть»: иначе стартовая проверка
    test_sidebar_starts_collapsed измеряла бы не то, с чем программа открывается,
    а то, что выставила фикстура.
    """
    was_expanded = window.sidebar._expanded
    window.resize(800, 640)
    for _ in range(3):
        APP.processEvents()
    yield
    window.sidebar.set_collapsed(not was_expanded, animate=False)
    window.resize(800, 640)
    for _ in range(3):
        APP.processEvents()


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    """Своё состояние UI: рабочий umbranet_ui.json проекта не трогаем."""
    path = tmp_path / "umbranet_ui.json"
    monkeypatch.setenv("UMBRANET_UI_STATE", str(path))
    import ui_state
    ui_state.set_state_path(None)
    yield path
    ui_state.set_state_path(None)


def saved_geometry(path: pathlib.Path) -> dict:
    import json
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("window_geometry") or {}


def page_holder(window, key: str) -> object:
    """То, что лежит в стеке для вкладки: сама страница или её прокрутка."""
    return window.stack.widget(window._pages[key])


# ── 1. Минимум окна позволяет половину и четверть экрана ────────────────────

def test_window_minimum_allows_half_screen(window):
    """ГЛАВНОЕ: минимум окна должен помещаться в половину обычного экрана.

    Половина 1920x1080 — 960 px. Раньше минимум был 1100 px, и раскладка
    Windows не могла ужать окно: оно оставалось шире половины экрана.
    """
    from umbranet import theme

    assert theme.WIN_MIN_W <= 960, (
        f"минимум по ширине {theme.WIN_MIN_W} не даёт встать на половину экрана (960)"
    )
    assert theme.WIN_MIN_H <= 600, (
        f"минимум по высоте {theme.WIN_MIN_H} не даёт встать на половину экрана (540-600)"
    )
    assert window.minimumWidth() == theme.WIN_MIN_W
    assert window.minimumHeight() == theme.WIN_MIN_H


@pytest.mark.parametrize("width, height", [(760, 560), (640, 480), (600, 440)])
def test_window_actually_shrinks(width, height, window):
    """Окно реально ужимается до запрошенного размера, а не «упирается»."""
    window.resize(width, height)
    for _ in range(4):
        APP.processEvents()
    assert window.width() <= width, f"окно не ужалось по ширине: {window.width()} > {width}"
    assert window.height() <= height, f"окно не ужалось по высоте: {window.height()} > {height}"


def test_heavy_pages_get_scroll_instead_of_blocking_resize(window):
    """Тяжёлым вкладкам тесно в узком окне — они получают прокрутку.

    Без этого их минимальная ширина (965 px у библиотеки AI-стратегий) упиралась
    в окно, и ужать его было нельзя.
    """
    window.resize(700, 560)
    for _ in range(4):
        APP.processEvents()

    # Pages are instantiated lazily so startup does not synchronously build
    # every heavy widget tree.  Visit the relevant pages before asserting each
    # page's permanent scroll-wrapper policy.
    for key in ("strategy_lab", "network", "settings"):
        window._show(key)
    holders = {key: page_holder(window, key) for key in window._pages}
    assert isinstance(holders["strategy_lab"], QScrollArea), (
        "библиотека AI-стратегий без прокрутки: узкое окно обрежет её справа"
    )
    # А лёгкие вкладки оборачивать не нужно: они сжимаются сами.
    assert not isinstance(holders["network"], QScrollArea)
    assert not isinstance(holders["settings"], QScrollArea)
    # «Маршрутизация» — отдельный случай, см. тесты ниже: у неё прокрутка внутри.


def _find_scroll_area(widget):
    """Ближайшая прокрутка над виджетом (или None)."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def _spin_wheel(widget, dy: int):
    """Прокрутить колесом прямо по виджету (как это делает человек мышью)."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    event = QWheelEvent(
        QPointF(20, 20), widget.mapToGlobal(QPoint(20, 20)),
        QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier,
        Qt.NoScrollPhase, False,
    )
    APP.sendEvent(widget, event)
    for _ in range(2):
        APP.processEvents()


def test_routing_page_is_never_wrapped_in_page_scroll(window):
    """ГЛАВНОЕ (жалоба пользователя): «Маршрутизация» НЕ прокручивается целиком.

    Ни вкладкой, ни левой колонкой. Полоса поперёк колонки выглядела как
    разделитель посередине окна («ползунок, который разделяет главное меню на две
    части») — пользователь просил убрать её полностью. Прокручиваться должны
    только сами списки и правая карточка с выбором маршрута.
    """
    holder = page_holder(window, "routing")
    assert not isinstance(holder, QScrollArea), (
        "вкладка «Маршрутизация» снова завёрнута в прокрутку страницы — так быть не должно"
    )

    window.resize(960, 540)
    for _ in range(6):
        APP.processEvents()
    window._show("routing")
    for _ in range(6):
        APP.processEvents()

    view = window._views["routing"]
    left_column = view._canvas.parentWidget()
    assert _find_scroll_area(left_column) is None, (
        "над левой колонкой снова есть прокрутка — это тот самый ползунок поперёк окна"
    )

    # И само правило проверяем напрямую: для ключа «routing» обёртка не создаётся
    # даже у заведомо «тяжёлой» страницы. Так гарантия не зависит от того, какие
    # сейчас минимальные размеры у вкладки.
    from PySide6.QtWidgets import QWidget

    heavy = QWidget()
    heavy.setMinimumSize(900, 700)
    assert window._scrollable_page(heavy, key="routing") is heavy, (
        "«Маршрутизация» снова попадает под общее правило обёртки в прокрутку"
    )
    assert isinstance(window._scrollable_page(heavy, key="other"), QScrollArea), (
        "правило обёртки сломалось: другая тяжёлая вкладка обязана получить прокрутку"
    )


def test_routing_right_part_scrolls_inside_itself(window):
    """Правая часть («Маршрут DNS») прокручивается внутри себя.

    Пользователь перечислил, чему прокрутка разрешена: списки сервисов, записи
    «Диспетчера задач» и правая часть вкладки с выбором маршрута. Правая часть
    получила собственную прокрутку: при низком окне полоса появляется у правого
    края, а не поперёк вкладки, при достаточной высоте полос нет вовсе.
    """
    window.resize(1180, 760)
    for _ in range(6):
        APP.processEvents()
    window._show("routing")
    for _ in range(6):
        APP.processEvents()

    view = window._views["routing"]
    scroll = _find_scroll_area(view._right_panel)
    assert scroll is not None, "у правой части нет своей прокрутки — низкое окно обрежет выбор маршрута"
    assert not scroll.verticalScrollBar().isVisible(), (
        "на обычном окне 1180x760 правая часть влезает — полосы быть не должно"
    )

    window.resize(960, 540)
    for _ in range(6):
        APP.processEvents()
    assert scroll.verticalScrollBar().isVisible(), (
        "на низком окне правая часть должна прокручиваться внутри себя"
    )


def test_lists_scroll_instead_of_the_column(window):
    """Списки ужимаются и прокручиваются сами — вместо прокрутки всей колонки.

    Проверяем на 1180x700: колонка обязана влезть целиком (иначе низ уедет за
    край вкладки, а прокрутки у неё нет), а её списки — прокручиваться колесом
    внутри своей рамки.
    """
    view = window._views["routing"]
    left_column = view._canvas.parentWidget()
    for size in ((1180, 700), (960, 540)):
        window.resize(*size)
        for _ in range(6):
            APP.processEvents()
        window._show("routing")
        for _ in range(6):
            APP.processEvents()
        assert _find_scroll_area(left_column) is None, (
            f"при окне {size[0]}x{size[1]} над колонкой снова прокрутка — это тот самый ползунок"
        )
        assert left_column.height() >= left_column.minimumSizeHint().height(), (
            f"при окне {size[0]}x{size[1]} колонке нужно {left_column.minimumSizeHint().height()} px, "
            f"а дали {left_column.height()} — низ уедет за край вкладки, прокрутки у колонки нет"
        )

    # Список сервисов прокручивается своим колесом (содержимого больше, чем места).
    services = view._canvas
    services._offset = 0
    if services._max_offset() > 0:
        _spin_wheel(services, -120)
        assert services._offset > 0, "список сервисов не прокручивается колесом"

    # Записи «Диспетчера задач» — тоже: раздуваем содержимое и крутим.
    records = view._manual_canvas
    records._content_h = records.height() + 400
    records._offset = 0
    _spin_wheel(records, -120)
    assert records._offset > 0, "записи «Диспетчера задач» не прокручиваются колесом"


def test_task_list_bottom_is_reachable(window):
    """Низ «Диспетчера задач» достижим: у списка своя прокрутка до конца.

    Раньше длинный список упирался в низ вкладки и последние строки были
    недостижимы. Теперь до низа списка домотать можно — внутри самой рамки
    списка, без полосы поперёк колонки.
    """
    window.resize(1180, 700)
    for _ in range(6):
        APP.processEvents()
    window._show("routing")
    for _ in range(6):
        APP.processEvents()

    records = window._views["routing"]._manual_canvas
    records._content_h = records.height() + 600      # как будто записей много
    records._offset = 0
    _spin_wheel(records, -100000)                    # крутим «до упора» вниз
    assert records._offset == records._max_offset(), (
        f"список не докручивается до конца: {records._offset} из {records._max_offset()}"
    )
    _spin_wheel(records, 100000)
    assert records._offset == 0, "список не докручивается обратно наверх"


def test_side_panel_hides_before_mode_buttons_shrink(window):
    """ГЛАВНОЕ (просьба пользователя): правая часть вкладки прячется ЗАРАНЕЕ.

    Когда окно сужается, панель «Маршрут DNS» / «Стратегия DPI» исчезает до того,
    как кнопки режимов DNS / Combo / DPI начнут ужиматься. Так момент исчезновения
    панели не совпадает с моментом, когда меняются кнопки (иначе два изменения на
    одном шаге читались бы как рывок), а списки получают всю ширину раньше.
    """
    view = window._views["routing"]
    for width in (1200, 1000, 900, 860):
        window.resize(width, 700)
        for _ in range(4):
            APP.processEvents()
        assert not view.is_narrow(), (
            f"при ширине {width} окно ещё широкое — панель обязана быть на месте"
        )
        assert view._right_scroll.isVisible(), f"при ширине {width} панель обязана быть видна"
        assert view._right_spacer.width() > 300, "место под панель обязано быть зарезервировано"

    narrow_at = None
    for width in range(859, 560, -1):
        window.resize(width, 700)
        for _ in range(2):
            APP.processEvents()
        if view.is_narrow():
            narrow_at = width
            break
    assert narrow_at is not None, "панель так и не спряталась при сужении окна"
    assert window.mode_switch.compression() == 0.0, (
        f"при ширине {narrow_at} кнопки режимов уже начали ужиматься — панель обязана "
        "была исчезнуть раньше"
    )
    assert not view._right_scroll.isVisible(), "панель спряталась, а виджет остался виден"
    # Распорка не просто нулевая — она убрана из раскладки совсем: нулевая оставила
    # бы между списками и краем свой зазор в 16 px.
    assert view._body_layout.indexOf(view._right_spacer) < 0, (
        "место под панель не освободилось: распорка осталась в раскладке"
    )
    left_column = view._canvas.parentWidget()
    left_right_edge = left_column.mapTo(view, left_column.rect().topLeft()).x() + left_column.width()
    assert left_right_edge >= view.width() - 30, (
        f"списки не заняли освободившуюся ширину: колонка кончается на {left_right_edge} "
        f"при вкладке {view.width()}"
    )
    assert view._narrow_toggle.isVisible(), "в узком окне обязана появиться кнопка вызова панели"


def test_side_panel_hides_not_too_early(window):
    """Панель прячется заранее, но БЕЗ большого запаса (замечание пользователя).

    Сначала запас был 50 px, и панель исчезала «слишком рано». Теперь 25 px: она
    всё ещё уходит до начала ужима кнопок режимов (чтобы два изменения не
    приходились на один шаг), но вплотную к этому моменту.
    """
    view = window._views["routing"]

    def state(width: int):
        window.resize(width, 700)
        for _ in range(4):
            APP.processEvents()
        return view.is_narrow(), window.mode_switch.compression()

    hidden_at = shrunk_at = None
    for width in range(1000, 600, -2):
        narrow, t = state(width)
        if narrow and hidden_at is None:
            hidden_at = width
        if t > 0 and shrunk_at is None:
            shrunk_at = width
        if hidden_at is not None and shrunk_at is not None:
            break

    assert hidden_at is not None, "панель так и не спряталась при сужении окна"
    assert shrunk_at is not None, "кнопки режимов так и не начали ужиматься"
    lead = hidden_at - shrunk_at
    assert lead >= 6, (
        f"панель спряталась на {hidden_at}, а кнопки начали ужиматься на {shrunk_at}: "
        f"запас всего {lead} px — панель обязана уходить ДО начала ужима кнопок, "
        "а не в тот же шаг"
    )
    assert lead <= 35, (
        f"запас до начала ужима кнопок {lead} px — это слишком рано (пользователь просил "
        "вдвое меньше прежних 50 px)"
    )


def test_content_does_not_jump_when_panel_hides(window):
    """ГЛАВНОЕ (замечание пользователя): при исчезновении панели ничего не съезжает.

    Кнопка вызова панели выше текста заголовка «Сервисы», и без фиксированной
    высоты строки она поднимала строку — из-за этого весь список уезжал вниз:
    «внутренности сервисов чуток уходят вниз». Теперь строка заголовка одной и той
    же высоты в обоих состояниях, и вертикаль содержимого не меняется.
    """
    from PySide6.QtWidgets import QLabel

    view = window._views["routing"]
    window._show("routing")
    for _ in range(4):
        APP.processEvents()

    def positions():
        left = view._canvas.parentWidget()
        title = next(x for x in left.findChildren(QLabel) if "Сервисы" in x.text())
        rows = {
            "заголовок": title.mapTo(view, title.rect().center()).y(),
            "поиск": view._service_search_input.mapTo(view, view._service_search_input.rect().topLeft()).y(),
            "список сервисов": view._canvas.mapTo(view, view._canvas.rect().topLeft()).y(),
            "диспетчер задач": view._manual_canvas.mapTo(view, view._manual_canvas.rect().topLeft()).y(),
            "кнопка вызова панели": view._narrow_toggle.mapTo(
                view, view._narrow_toggle.rect().center()).y(),
        }
        return rows

    # Сравниваем при ОДНОЙ И ТОЙ ЖЕ высоте окна: «Диспетчер задач» прижат к низу
    # вкладки, и при изменении высоты он честно едет — это не тот сдвиг, который
    # заметил пользователь. Речь о сдвиге от прятанья панели.
    window.resize(1180, 620)
    for _ in range(6):
        APP.processEvents()
    wide = positions()

    window.resize(720, 620)
    for _ in range(6):
        APP.processEvents()
    narrow = positions()
    assert view.is_narrow(), "при 720 окно узкое — панель обязана быть спрятана"

    # «Диспетчер задач» прижат к НИЗУ вкладки, а вкладка в узком окне короче
    # (шапка в узком окне выше — это отдельное, давнее поведение): поэтому его
    # положение считаем от низа вкладки, а не в абсолютных координатах. Так тест
    # ловит именно «съехало/отклеилось», а не честный сдвиг прижатого блока.
    for name, y_wide in wide.items():
        if name == "диспетчер задач":
            continue
        assert narrow[name] == y_wide, (
            f"при исчезновении панели «{name}» съехал по вертикали: {y_wide} → {narrow[name]}"
        )

    # «Диспетчер задач» прижат к НИЗУ вкладки, а сама вкладка в узком окне короче
    # (шапка в узком окне выше — давнее поведение, к панели отношения не имеет):
    # поэтому у блока проверяем не координату, а прижатость к низу колонки.
    section, column = view._manual_section, view._left_column
    block_bottom = section.mapTo(view, section.rect().topLeft()).y() + section.height()
    column_bottom = column.mapTo(view, column.rect().topLeft()).y() + column.height()
    assert abs(column_bottom - block_bottom) <= 8, (
        f"«Диспетчер задач» отклеился от низа вкладки: низ блока {block_bottom}, "
        f"низ колонки {column_bottom}"
    )
    # Кнопка вызова панели стоит на одной линии с заголовком, а не ниже него.
    assert narrow["кнопка вызова панели"] == narrow["заголовок"], (
        f"кнопка вызова панели ниже заголовка: {narrow['кнопка вызова панели']} против "
        f"{narrow['заголовок']}"
    )

    window.resize(1180, 620)
    for _ in range(6):
        APP.processEvents()
    for name, y_wide in wide.items():
        assert positions()[name] == y_wide, f"после возврата панели «{name}» съехал"


def test_buttons_do_not_stick_to_the_edge_in_narrow_window(window):
    """В узком окне кнопки не липнут к правому краю (замечание пользователя).

    В широком окне справа стоит панель, поэтому правый отступ вкладки нулевой.
    Когда панель прячется, отступ обязан вернуться — иначе «+ Добавить» и поле
    ввода упираются в самый край окна.
    """
    view = window._views["routing"]
    window._show("routing")
    for _ in range(4):
        APP.processEvents()

    def gap_to_edge() -> int:
        left = view._canvas.parentWidget()
        button = next(b for b in left.findChildren(type(view._narrow_toggle))
                      if "Добавить" in b.text())
        right = button.mapTo(view, button.rect().topLeft()).x() + button.width()
        return view.width() - right

    window.resize(1180, 760)
    for _ in range(6):
        APP.processEvents()
    assert gap_to_edge() > 300, "в широком окне кнопка стоит перед панелью — зазор большой"

    window.resize(720, 620)
    for _ in range(6):
        APP.processEvents()
    gap = gap_to_edge()
    assert gap >= 16, (
        f"в узком окне «+ Добавить» прижата к краю: зазор всего {gap} px — "
        "кнопка выглядит приклеенной к границе окна"
    )


def test_side_panel_layout_is_untouched_in_wide_window(window):
    """В широком окне раскладка прежняя: панель на своём месте, пиксель в пиксель.

    Панель больше не лежит в раскладке (её место держит распорка), поэтому это
    надо проверять отдельно — иначе легко получить сдвинутую панель и не заметить.
    """
    view = window._views["routing"]

    def geometry_at(width: int):
        window.resize(width, 760)
        for _ in range(5):
            APP.processEvents()
        window._show("routing")
        for _ in range(4):
            APP.processEvents()
        panel = view._right_panel
        top_left = panel.mapTo(view, panel.rect().topLeft())
        return top_left.x(), top_left.y(), panel.width(), panel.height()

    window.resize(1180, 760)
    for _ in range(5):
        APP.processEvents()
    window._show("routing")
    for _ in range(4):
        APP.processEvents()
    before = geometry_at(1180)

    # Панель обязана прилегать к правому краю вкладки (как и раньше) и по высоте
    # занимать вкладку целиком.
    x, _y, w, h = before
    assert x + w >= view.width() - 1, (
        f"панель отъехала от правого края: правый край {x + w}, ширина вкладки {view.width()}"
    )
    assert h >= view.height() * 0.8, f"панель потеряла высоту: {h} при вкладке {view.height()}"

    # Место под панель занято: списки не заезжают под неё. Если распорка пропала,
    # список растянулся бы во всю ширину и оказался под панелью — это и проверяем.
    assert view._body_layout.indexOf(view._right_spacer) >= 0, (
        "в широком окне распорки нет в раскладке: место под панель не зарезервировано"
    )
    left_column = view._canvas.parentWidget()
    left_right_edge = left_column.mapTo(view, left_column.rect().topLeft()).x() + left_column.width()
    assert left_right_edge <= x + 2, (
        f"списки заехали под панель: колонка кончается на {left_right_edge}, "
        f"а панель начинается на {x}"
    )

    # Уход в узкий режим и обратно не должен сдвигать её ни на пиксель.
    window.resize(700, 620)
    for _ in range(5):
        APP.processEvents()
    window.resize(1180, 760)
    for _ in range(6):
        APP.processEvents()
    assert geometry_at(1180) == before, (
        f"после ухода в узкое окно и возврата панель встала иначе: {geometry_at(1180)} против {before}"
    )


def test_right_panel_opens_over_the_list_in_narrow_window(window):
    """В узком окне правая часть вызывается кнопкой и выезжает ПОВЕРХ списка."""
    view = window._views["routing"]
    window.resize(700, 620)
    for _ in range(5):
        APP.processEvents()
    window._show("routing")
    for _ in range(5):
        APP.processEvents()

    assert view.is_narrow(), "при ширине 700 окно узкое — панель обязана быть спрятана"
    assert view._narrow_toggle.isVisible(), "кнопка вызова панели обязана быть видна"
    assert not view._right_scroll.isVisible()

    view.toggle_right_panel()
    for _ in range(4):
        APP.processEvents()
    assert view._right_scroll.isVisible(), "по кнопке панель обязана появиться"
    assert view._backdrop.isVisible(), "под панелью обязано быть затемнение (клик мимо закрывает)"

    # Панель внутри вкладки и не накрывает строку с кнопкой: она должна нажиматься.
    geo = view._right_scroll.geometry()
    assert geo.right() <= view.width(), "панель вылезла за вкладку"
    assert geo.top() >= view._content_top() - 1, "панель накрыла строку заголовка с кнопкой"
    button_center = view._narrow_toggle.mapTo(view, view._narrow_toggle.rect().center())
    assert not geo.contains(button_center), "кнопка вызова панели оказалась под панелью"

    from PySide6.QtCore import Qt as _Qt

    QTest.mouseClick(view._narrow_toggle, _Qt.LeftButton)
    for _ in range(4):
        APP.processEvents()
    assert not view._right_scroll.isVisible(), "повторное нажатие кнопки обязано закрыть панель"
    assert not view._backdrop.isVisible(), "затемнение обязано исчезнуть вместе с панелью"


def test_right_panel_closes_on_click_outside_and_escape(window):
    """Панель поверх списка закрывается кликом мимо и клавишей Esc."""
    from PySide6.QtCore import QPoint
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QMouseEvent

    view = window._views["routing"]
    window.resize(700, 620)
    for _ in range(5):
        APP.processEvents()
    window._show("routing")
    for _ in range(5):
        APP.processEvents()

    view.open_right_panel()
    for _ in range(3):
        APP.processEvents()
    assert view._right_scroll.isVisible()

    # клик в пустое место слева от панели — это «клик мимо»
    point = QPoint(30, view.height() - 30)
    APP.sendEvent(
        view._backdrop,
        QMouseEvent(QMouseEvent.MouseButtonPress, point, view._backdrop.mapToGlobal(point),
                    _Qt.LeftButton, _Qt.LeftButton, _Qt.NoModifier),
    )
    for _ in range(4):
        APP.processEvents()
    assert not view._right_scroll.isVisible(), "клик мимо не закрыл панель"

    view.open_right_panel()
    for _ in range(3):
        APP.processEvents()
    QTest.keyClick(view, _Qt.Key_Escape)
    for _ in range(4):
        APP.processEvents()
    assert not view._right_scroll.isVisible(), "Esc не закрыл панель"


def test_side_panel_returns_with_a_margin_no_flicker(window):
    """На границе панель не мигает: между «спряталась» и «вернулась» есть полоса.

    Порог возврата выше порога исчезновения (гистерезис 24 px). Без него панель
    дёргалась бы туда-обратно при дрожании окна мышью ровно на границе. Полосу
    измеряем по факту: идём вниз мелкими шагами до исчезновения, потом вверх до
    возврата, и смотрим, сколько пикселей между этими двумя ширинами.
    """
    view = window._views["routing"]

    def narrow_at(width: int) -> bool:
        window.resize(width, 700)
        for _ in range(4):
            APP.processEvents()
        return view.is_narrow()

    hidden_at = None
    for width in range(1000, 600, -4):
        if narrow_at(width):
            hidden_at = width
            break
    assert hidden_at is not None, "панель не спряталась при сужении — граница не найдена"

    shown_at = None
    for width in range(hidden_at, 1100, 4):
        if not narrow_at(width):
            shown_at = width
            break
    assert shown_at is not None, "панель не вернулась при расширении окна обратно"

    band = shown_at - hidden_at
    assert band >= 16, (
        f"полоса возврата всего {band} px (спряталась на {hidden_at}, вернулась на {shown_at}): "
        "при дрожании окна на границе панель будет мигать"
    )


def test_services_header_is_compact_and_high(window):
    """ГЛАВНОЕ (просьба пользователя): шапка вкладки и поиск подняты, зазоры плотнее.

    Надпись «Сервисы» вместе с кнопкой маршрута и строкой поиска должны стоять
    выше — за счёт этого интерфейс плотнее, а сэкономленные пиксели получает
    список сервисов (ради него всё и делалось).
    """
    from PySide6.QtWidgets import QLabel

    view = window._views["routing"]
    # Ждём: при ресайзе окно «замораживает» содержимое до устаканивания размера,
    # поэтому одних processEvents мало — раскладка пересчитывается по таймеру.
    window.resize(1180, 760)
    QTest.qWait(350)
    window._show("routing")
    QTest.qWait(250)
    # Стартовая плашка («не готов к запуску») — временное сообщение о правах
    # администратора: она занимает полсотни пикселей сверху и к плотности шапки
    # отношения не имеет. Убираем, чтобы замер не зависел от того, успела ли она
    # появиться.
    window.health_banner.setVisible(False)
    QTest.qWait(150)
    assert not window.health_banner.isVisible()

    left = view._canvas.parentWidget()
    title = next(x for x in left.findChildren(QLabel) if "Сервисы" in x.text())
    title_top = title.mapTo(view, title.rect().topLeft()).y()
    title_bottom = title_top + title.height()
    search_top = view._service_search_input.mapTo(
        view, view._service_search_input.rect().topLeft()).y()
    search_bottom = search_top + view._service_search_input.height()
    list_top = view._canvas.mapTo(view, view._canvas.rect().topLeft()).y()

    assert title_top <= 12, (
        f"шапка вкладки стоит слишком низко: «Сервисы» начинается на y={title_top} "
        "(было 19 до уплотнения)"
    )
    assert search_top - title_bottom <= 14, (
        f"между надписью и поиском {search_top - title_bottom} px — слишком просторно"
    )
    assert list_top - search_bottom <= 16, (
        f"между поиском и списком сервисов {list_top - search_bottom} px — слишком просторно"
    )
    assert left.layout().spacing() <= 8, (
        f"зазоры в колонке по-прежнему {left.layout().spacing()} px — плотность не поднялась"
    )
    # Главное следствие: список сервисов стал выше (до уплотнения было ~230 px).
    assert view._canvas.height() >= 245, (
        f"список сервисов не выиграл в высоте: {view._canvas.height()} px при окне 1180x760"
    )


# ── «Диспетчер задач»: высота тянется за фиолетовую линию ──────────────────

def _drag_dispatcher(view, dy: int, steps: int = 4):
    """Тянет заголовок «Диспетчера задач» на dy пикселей вниз (минус — вверх)."""
    from PySide6.QtCore import QPointF
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QMouseEvent

    header = view._manual_section.header
    local = QPointF(header.rect().center())
    start = QPointF(header.mapToGlobal(local))
    APP.sendEvent(header, QMouseEvent(QMouseEvent.MouseButtonPress, local, start,
                                      _Qt.LeftButton, _Qt.LeftButton, _Qt.NoModifier))
    for step in range(1, steps + 1):
        point = QPointF(start.x(), start.y() + dy * step / steps)
        APP.sendEvent(header, QMouseEvent(QMouseEvent.MouseMove, local, point,
                                          _Qt.NoButton, _Qt.LeftButton, _Qt.NoModifier))
    APP.sendEvent(header, QMouseEvent(QMouseEvent.MouseButtonRelease, local,
                                      QPointF(start.x(), start.y() + dy),
                                      _Qt.LeftButton, _Qt.NoButton, _Qt.NoModifier))
    QTest.qWait(80)


def _dispatcher_fits(window) -> tuple[bool, str]:
    """Помещается ли колонка с «Диспетчером задач» в видимую часть вкладки.

    Проверять «низ блока против высоты колонки» бесполезно: когда блок вылезает,
    вместе с ним растёт и сама колонка (раскладка отдаёт ей минимальную высоту).
    Поэтому сравниваем с тем, что реально видно, — с высотой вкладки.
    """
    view = window._views["routing"]
    column = view._left_column
    section = view._manual_section
    bottom = section.mapTo(view, section.rect().topLeft()).y() + section.height()
    # Список не должен быть выше своего блока: лишняя высота не раздувает колонку,
    # а обрезается внутри блока — то есть нижние карточки просто исчезают.
    canvas_h = view._manual_canvas.height()
    clipped = canvas_h + view._dispatcher_chrome_h() > section.height() + 2
    detail = (f"колонка {column.height()} при вкладке {view.height()}, низ блока {bottom}, "
              f"список {canvas_h} + шапка {view._dispatcher_chrome_h()} "
              f"{'НЕ влезает' if clipped else 'влезает'} в блок {section.height()}")
    fits = (not clipped) and column.height() <= view.height() - 4 and bottom <= view.height()
    return fits, detail


def _dispatcher_ready(window):
    """Окно нужного размера, вкладка показана, стартовая плашка убрана."""
    window.resize(1180, 760)
    QTest.qWait(300)
    window._show("routing")
    QTest.qWait(200)
    window.health_banner.setVisible(False)
    QTest.qWait(120)
    return window._views["routing"]


def test_dispatcher_line_is_draggable_with_vertical_cursor(window):
    """ГЛАВНОЕ (просьба пользователя): за фиолетовую линию можно тянуть.

    У заголовка «ДИСПЕТЧЕР ЗАДАЧ» должен быть курсор изменения размера — иначе
    человек не догадается, что линия тянется. Тяга вверх увеличивает блок, тяга
    вниз уменьшает, а список сервисов забирает освободившееся место (сумма высот
    сохраняется).
    """
    view = _dispatcher_ready(window)
    header = view._manual_section.header

    assert header.cursor().shape() == Qt.SizeVerCursor, (
        f"курсор над линией не «вертикальный размер», а {header.cursor().shape()} — "
        "человек не поймёт, что линию можно тянуть"
    )

    services_before = view._canvas.height()
    records_before = view._manual_canvas.height()

    _drag_dispatcher(view, -60)                 # вверх — блок больше
    grew = view._manual_canvas.height()
    assert grew == records_before + 60, (
        f"тяга вверх на 60 не увеличила блок: {records_before} → {grew}"
    )
    assert view._canvas.height() == services_before - 60, (
        "место не перешло от сервисов: список сервисов обязан ужаться ровно на столько же"
    )

    _drag_dispatcher(view, 30)                  # вниз — блок меньше
    shrunk = view._manual_canvas.height()
    assert shrunk == grew - 30, f"тяга вниз на 30 не уменьшила блок: {grew} → {shrunk}"
    assert view._canvas.height() == services_before - 60 + 30


def test_dispatcher_line_has_hover_feedback(window):
    """Линия подсвечивается, когда курсор над заголовком (подсказка «тяни сюда»)."""
    from PySide6.QtCore import QEvent

    view = _dispatcher_ready(window)
    header = view._manual_section.header
    idle = header._line.styleSheet()

    APP.sendEvent(header, QEvent(QEvent.Enter))
    hovered = header._line.styleSheet()
    APP.sendEvent(header, QEvent(QEvent.Leave))
    restored = header._line.styleSheet()

    assert idle != hovered, "при наведении на заголовок линия никак не отзывается"
    assert restored == idle, "после ухода курсора линия не вернулась к обычному виду"
    assert header._line.height() == 1, (
        "толщина линии изменилась — значит при наведении что-то сдвигается по вертикали"
    )


def test_dispatcher_drag_stops_at_sane_limits(window):
    """Тяга упирается в пределы: список не исчезает и не выдавливает сервисы."""
    view = _dispatcher_ready(window)
    floor = view._dispatcher_floor()

    _drag_dispatcher(view, 2000)                # тянем вниз «до упора»
    assert view._manual_canvas.height() == floor, (
        f"блок не остановился на минимуме: {view._manual_canvas.height()} вместо {floor}"
    )
    assert view._canvas.height() >= view._canvas.minimumHeight(), (
        "сервисам не осталось их минимума"
    )

    _drag_dispatcher(view, -2000)               # тянем вверх «до упора»
    assert view._canvas.height() == view._canvas.minimumHeight(), (
        f"сервисы должны были сжаться до минимума, а не до {view._canvas.height()}"
    )
    fits, detail = _dispatcher_fits(window)
    assert fits, f"блок вылез за низ вкладки: {detail}"
    assert view._manual_canvas.height() > floor, "блок не вырос при тяге вверх"
    # Верхний предел: блок не выше того, что оставляет сервисам их минимум. Без
    # него тяга «до упора вверх» уводит блок за край (сравнение с высотой вкладки
    # это ловит не всегда: вкладка растёт вместе с блоком).
    limit = view._dispatcher_max_block_h()
    assert view._manual_section.height() <= limit + 2, (
        f"блок выше предела: {view._manual_section.height()} против {limit}"
    )


def test_dispatcher_height_fits_smaller_window_and_comes_back(window):
    """Выбранная высота не ломает низкое окно и возвращается на прежнее место.

    Человек выбрал большую высоту — при уменьшении окна блок ужимается, чтобы
    ничего не вылезло за край; при возврате размера выбранная высота возвращается
    (она запоминается отдельно от применённой).
    """
    view = _dispatcher_ready(window)
    _drag_dispatcher(view, -300)
    chosen = view._manual_canvas.height()
    assert chosen > 300, "не удалось задать большую высоту для проверки"

    for height in (620, 520, 430):
        window.resize(1180, height)
        QTest.qWait(300)
        fits, detail = _dispatcher_fits(window)
        assert fits, f"при окне высотой {height} блок вылез за низ вкладки: {detail}"
        assert view._canvas.height() >= view._canvas.minimumHeight(), (
            f"при окне высотой {height} сервисы сжались ниже минимума"
        )
        # Прямая проверка предела: высота блока не больше того, что остаётся
        # сервисам после их минимума. Без неё «сравнение с высотой вкладки» ловит
        # нарушение не всегда — сама вкладка растёт вместе с блоком.
        limit = view._dispatcher_max_block_h()
        assert view._manual_section.height() <= limit + 2, (
            f"при окне высотой {height} блок выше предела: "
            f"{view._manual_section.height()} против {limit}"
        )

    window.resize(1180, 760)
    QTest.qWait(300)
    assert view._manual_canvas.height() == chosen, (
        f"после возврата окна высота не вернулась: {view._manual_canvas.height()} вместо {chosen}"
    )


def test_dispatcher_height_is_remembered(window):
    """Выбранная высота сохраняется в состоянии UI и читается обратно."""
    import ui_state

    view = _dispatcher_ready(window)
    _drag_dispatcher(view, -120)
    # Сохраняется высота БЛОКА целиком (линия, название, кнопки, фильтр и список):
    # именно её человек двигает мышью за линию.
    saved = int(view._dispatcher_pref)
    assert saved == view._manual_section.height(), (
        f"высота блока {view._manual_section.height()} не совпадает с выбранной {saved}"
    )

    stored = ui_state.get_value(view.DISPATCHER_HEIGHT_KEY, None)
    assert stored == saved, f"в состоянии UI лежит {stored}, а выбрано было {saved}"
    assert view._load_dispatcher_height() == saved, (
        "сохранённая высота не читается обратно — после перезапуска размер потеряется"
    )


def test_add_and_process_buttons_are_same_size(window):
    """«Добавить» и «Процесс» — одного размера (просьба пользователя).

    Кнопки стоят рядом в строке добавления «Диспетчера задач», и разная ширина
    выглядела неаккуратно. Высота у обеих 36 px, ширина — по более широкой.
    """
    view = _dispatcher_ready(window)
    add_btn, pick_btn = view._add_btn, view._pick_btn

    assert add_btn.isVisible() and pick_btn.isVisible(), "кнопки строки добавления пропали"
    assert (add_btn.width(), add_btn.height()) == (pick_btn.width(), pick_btn.height()), (
        f"кнопки разного размера: «Добавить» {add_btn.width()}x{add_btn.height()}, "
        f"«Процесс» {pick_btn.width()}x{pick_btn.height()}"
    )
    assert add_btn.height() == 36, f"высота кнопки {add_btn.height()} вместо 36"
    assert add_btn.width() >= add_btn.sizeHint().width(), (
        "кнопка уже, чем нужно её подписи — текст обрежется"
    )
    assert pick_btn.width() >= pick_btn.sizeHint().width(), (
        "кнопка уже, чем нужно её подписи — текст обрежется"
    )


def test_dispatcher_line_is_the_top_edge_of_the_block(window):
    """Граница блока — сама фиолетовая линия, а не чёрная полоса над ней.

    Раньше над линией оставалось 10 px поля шапки плюс 8 px зазора до списка
    сервисов: блок начинался чёрным фоном, и граница проходила не по линии
    (замечание пользователя). Теперь зазора перед блоком нет, а линия — самая
    верхняя грань блока: первая же строка пикселей блока и есть линия.
    """
    view = _dispatcher_ready(window)
    section, column, services = view._manual_section, view._left_column, view._canvas

    gap = (section.mapTo(column, section.rect().topLeft()).y()
           - (services.mapTo(column, services.rect().topLeft()).y() + services.height()))
    assert gap == 0, f"между списком сервисов и блоком осталось {gap} px тёмного фона"
    assert section.header.layout().contentsMargins().top() == 0, (
        "над фиолетовой линией снова есть поле — блок начинается не с линии"
    )

    # Линия внутри шапки стоит на самом её верху, то есть на верху блока.
    from PySide6.QtCore import QPoint
    line_y = section.header._line.mapTo(section, QPoint(0, 0)).y()
    assert line_y == 0, f"фиолетовая линия внутри блока на y={line_y}, а не на самой верхней грани"

    # И первая же строка пикселей блока — она сама (градиент слева направо, поэтому
    # проверяем начало линии, где она яркая, а не выцветший правый край).
    image = section.grab().toImage()
    edge = max(5, image.width() // 3)
    row = [image.pixelColor(x, 0) for x in range(4, edge, 10)]
    lit = [c for c in row if c.alpha() > 40 and (c.red() + c.green() + c.blue()) > 120]
    assert len(lit) >= len(row) - 1, (
        "верхняя строка блока — не фиолетовая линия: "
        f"{[c.name() for c in row[:4]]} при ширине {image.width()}"
    )


def test_right_panel_is_clickable_in_wide_window(window):
    """В широком окне правую панель можно нажимать сразу, без сужения окна.

    Что было (нашёл пользователь): при открытии программы в обычном, широком
    окне во вкладке «Маршрутизация» НЕ нажималось НИЧЕГО в правой части — ни
    маршрут, ни стратегия. Стоило сузить окно и открыть ту же панель кнопкой —
    и всё начинало работать.

    Причина не в самой панели, а в распорке раскладки: панель плавающая (в
    раскладке её нет), место под неё держит пустой QWidget-распорка — ровно с
    той же геометрией и ВЫШЕ панели по стеку (распорка создаётся позже). Она и
    забирала на себя все клики и колесо мыши. В узком окне распорку убирают из
    раскладки, а панель поднимают (raise_) — поэтому там всё работало.

    Проверяем то, что важно человеку: клик в точке правой панели должен попадать
    В ПАНЕЛЬ (а не в невидимого соседа), и строка маршрута обязана на него
    реагировать.
    """
    from PySide6.QtCore import QPoint, Qt

    view = window._views["routing"]
    window.resize(1180, 760)
    QTest.qWait(400)
    window._show("routing")
    QTest.qWait(200)
    window.health_banner.setVisible(False)
    QTest.qWait(150)

    view.set_narrow_mode(False, force=True)
    QTest.qWait(250)
    panel = view._right_scroll
    assert panel.isVisible(), "правая панель в широком окне не показана"

    # 1) Распорка-заполнитель обязана пропускать мышь сквозь себя.
    assert view._right_spacer.testAttribute(Qt.WA_TransparentForMouseEvents), (
        "распорка снова ловит мышь: она лежит поверх панели и съест все клики"
    )

    # 2) Под точками внутри панели должен оказаться её собственный контент.
    canvas = view._vpn_exit_list._canvas
    assert canvas.isVisible(), "список VPN-выходов не показан в широком окне"

    clicks = []
    canvas.rowClicked.connect(clicks.append)
    for row_y in (20, 100, 180):
        pos = canvas.mapTo(window, QPoint(canvas.width() // 2, row_y))
        under = window.childAt(pos)
        assert under is not None, f"в точке {pos} нет ни одного виджета"
        assert panel.isAncestorOf(under), (
            f"клик в точке {pos} попадает в {type(under).__name__}, а не в правую панель — "
            "человек жмёт по маршруту, а нажатие уходит «в пустоту»"
        )

    # 3) И настоящее нажатие доходит до строки маршрута.
    pos = canvas.mapTo(window, QPoint(canvas.width() // 2, 20))
    under = window.childAt(pos)
    QTest.mouseClick(under, Qt.LeftButton, pos=under.mapFrom(window, pos))
    QTest.qWait(200)
    assert clicks, "нажатие по строке маршрута в широком окне не сработало"


def test_right_panel_is_opaque(window):
    """Правая панель маршрутов плотная: сквозь неё не видно кнопок и карточек.

    Панель выезжает ПОВЕРХ списка сервисов (узкое окно), и полупрозрачное «стекло»
    показывало через себя содержимое списка (замечание пользователя). Проверяем
    строго: то, что нарисовала сама панель, совпадает с тем, что видно в окне на
    её месте — значит, сквозь неё ничего не просвечивает.
    """
    from PySide6.QtCore import QPoint

    view = window._views["routing"]
    window.resize(760, 700)
    QTest.qWait(400)
    window._show("routing")
    QTest.qWait(200)
    window.health_banner.setVisible(False)
    QTest.qWait(150)

    view.set_narrow_mode(True, force=True)
    QTest.qWait(150)
    view.open_right_panel()
    QTest.qWait(250)
    panel = view._right_scroll
    assert panel.isVisible(), "панель маршрутов не открылась"

    origin = panel.mapTo(window, QPoint(0, 0))
    window_image = window.grab().toImage()
    panel_image = panel.grab().toImage()

    worst, worst_at = 0, None
    for x in range(70, panel_image.width() - 70, 7):
        for y in range(50, panel_image.height() - 50, 7):
            seen = window_image.pixelColor(origin.x() + x, origin.y() + y)
            own = panel_image.pixelColor(x, y)
            diff = max(abs(seen.red() - own.red()), abs(seen.green() - own.green()),
                       abs(seen.blue() - own.blue()))
            if diff > worst:
                worst, worst_at = diff, (x, y)
    assert worst <= 2, (
        f"сквозь правую панель просвечивает содержимое списка: расхождение {worst} "
        f"в точке {worst_at} — фон панели обязан быть плотным"
    )
    # Заодно: фон панели именно плотный цвет, а не полупрозрачный.
    import theme
    assert theme.qc(theme.opaque(theme.CARD_DARK)).alpha() == 255, (
        "фон панели снова полупрозрачный"
    )
    view.close_right_panel()
    QTest.qWait(150)


def test_dispatcher_collapses_down_to_the_buttons(window):
    """Блок уходит ниже и встаёт «в упор до кнопок» (просьба пользователя).

    В свёрнутом виде в блоке остаются только фиолетовая линия, название
    «Диспетчер задач» и строка с кнопками «Процесс» / «Добавить»: список,
    надпись «Активные записи» и фильтр прячутся, а низ блока совпадает с низом
    строки кнопок и с низом колонки.
    """
    view = _dispatcher_ready(window)
    _drag_dispatcher(view, 2000)                 # тянем линию вниз до упора

    assert view._dispatcher_collapsed, "блок не свернулся до кнопок"
    assert view._list_head_host.isVisible() is False, "надпись «Активные записи» не спряталась"
    assert view._search_input.isVisible() is False, "строка фильтра не спряталась"
    assert view._manual_canvas.isVisible() is False, "список карточек не спрятался"
    assert view._add_btn.isVisible() and view._pick_btn.isVisible(), "кнопки пропали вместе с начинкой"

    section, column = view._manual_section, view._left_column
    stub = view._dispatcher_stub_h()
    assert section.height() == stub, (
        f"высота свёрнутого блока {section.height()} вместо {stub} (шапка + кнопки)"
    )
    buttons_bottom = (view._add_btn.mapTo(view, view._add_btn.rect().topLeft()).y()
                      + view._add_btn.height())
    section_bottom = section.mapTo(view, section.rect().topLeft()).y() + section.height()
    assert abs(section_bottom - buttons_bottom) <= 2, (
        f"под кнопками осталось {section_bottom - buttons_bottom} px — блок должен "
        "упираться в строку с кнопками"
    )
    column_bottom = column.mapTo(view, column.rect().topLeft()).y() + column.height()
    assert abs(column_bottom - section_bottom) <= 8, (
        f"блок отклеился от низа колонки: {column_bottom} против {section_bottom}"
    )
    fits, detail = _dispatcher_fits(window)
    assert fits, f"свёрнутый блок вылез за вкладку: {detail}"


def test_dispatcher_opens_again_after_collapse(window):
    """Из свёрнутого состояния блок возвращается: тянем линию вверх — список снова виден."""
    view = _dispatcher_ready(window)
    _drag_dispatcher(view, 2000)
    assert view._dispatcher_collapsed, "блок не свернулся"
    collapsed_h = view._manual_section.height()

    _drag_dispatcher(view, -400)                 # тянем вверх
    assert not view._dispatcher_collapsed, "блок не раскрылся обратно"
    for name, widget in (("надпись «Активные записи»", view._list_head_host),
                         ("строка фильтра", view._search_input),
                         ("список карточек", view._manual_canvas)):
        assert widget.isVisible(), f"после раскрытия не вернулась {name}"
    assert view._manual_section.height() > collapsed_h + 100, (
        f"блок почти не вырос: {collapsed_h} → {view._manual_section.height()}"
    )
    assert view._manual_canvas.height() >= 40, (
        f"список вернулся высотой {view._manual_canvas.height()} px — меньше одной карточки"
    )
    fits, detail = _dispatcher_fits(window)
    assert fits, f"раскрытый блок вылез за вкладку: {detail}"


def test_wrap_rule_checks_width_and_height(window):
    """Самодельные виджеты: тесно по ширине ИЛИ по высоте → вкладка получает прокрутку.

    Проверяем правило напрямую, а не только на текущих вкладках. Важно именно
    «или»: список «Диспетчера задач» упирается в ВЫСОТУ окна, а не в ширину, и
    правило, которое смотрит только ширину, оставило бы его обрезанным снизу.
    """
    from PySide6.QtWidgets import QWidget

    wide = QWidget()
    wide.setMinimumSize(800, 100)          # широкая, но низкая
    tall = QWidget()
    tall.setMinimumSize(300, 600)          # узкая, но высокая — как длинный список
    small = QWidget()
    small.setMinimumSize(300, 100)

    assert isinstance(window._scrollable_page(wide), QScrollArea), (
        "широкая вкладка должна получить прокрутку"
    )
    assert isinstance(window._scrollable_page(tall), QScrollArea), (
        "высокая вкладка должна получить прокрутку: иначе низ будет обрезан"
    )
    assert window._scrollable_page(small) is small, "небольшую вкладку оборачивать не нужно"


def test_background_has_no_glow_in_bottom_left_corner(window):
    """ГЛАВНОЕ (жалоба пользователя): в левом нижнем углу фона нет свечения.

    Фон окна рисовал две «туманности» — розовую у правого верхнего угла и
    бирюзово-зелёную у левого нижнего. Второе пятно читалось как случайный
    зелёный круг и было видно на каждой вкладке (фон общий для всего
    приложения). Проверяем пиксели: в левом нижнем углу цвет обязан совпадать
    с цветом фона темы, а не быть светлее.

    Тест пиксельный, а не «есть ли функция»: если пятно вернут любой другой
    отрисовкой (градиент, картинка, эффект), проверка всё равно поймает.
    """
    from PySide6.QtGui import QColor

    from umbranet import theme

    window.resize(1180, 760)
    for _ in range(6):
        APP.processEvents()
    window._show("routing")
    for _ in range(6):
        APP.processEvents()

    image = window.grab().toImage()
    base = QColor(theme.BG)

    def delta(color: QColor) -> int:
        return (abs(color.red() - base.red())
                + abs(color.green() - base.green())
                + abs(color.blue() - base.blue()))

    # Смотрим по самой нижней полосе области содержимого: там нет ни карточек, ни
    # панелей — только фон. Прежнее пятно (радиус 450 с центром в 100 px от низа)
    # как раз накрывало эту полосу, поэтому проверка его поймала бы.
    y = image.height() - 3
    x_start = window.sidebar.width() + 4
    for x in range(x_start, min(x_start + 260, image.width() - 1), 20):
        color = QColor(image.pixel(x, y))
        assert delta(color) <= 6, (
            f"в левом нижнем углу свечение: точка ({x},{y}) = {color.name()}, "
            f"а фон темы {base.name()} — пятно надо убрать"
        )


def test_only_one_background_nebula_remains(window):
    """Второе свечение фона убрано из отрисовки, а не только «спрятано»."""
    import inspect

    source = inspect.getsource(type(window).paintEvent)
    assert "nebula_bl" not in source, (
        "paintEvent всё ещё рисует свечение левого нижнего угла"
    )
    rebuild = inspect.getsource(type(window)._rebuild_sprites)
    assert "nebula_bl = sprite" not in rebuild, (
        "_rebuild_sprites всё ещё собирает спрайт свечения левого нижнего угла"
    )


def test_small_pages_are_not_wrapped(window):
    """Вкладкам с собственной прокруткой лишняя обёртка не нужна."""
    for key in ("network", "settings", "about"):
        window._show(key)
    holders = {key: page_holder(window, key) for key in window._pages}
    for key in ("network", "settings", "about"):
        assert not isinstance(holders[key], QScrollArea), (
            f"«{key}» обёрнута зря: у вкладки своя прокрутка, обёртка только меняет вид"
        )


def test_no_scrollbars_when_window_is_roomy(window):
    """Когда места хватает, прокрутка не появляется — вид вкладки не меняется."""
    window.resize(790, 640)
    for _ in range(5):
        APP.processEvents()
    window._show("strategy_lab")
    for _ in range(5):
        APP.processEvents()

    holder = page_holder(window, "strategy_lab")
    assert isinstance(holder, QScrollArea)
    assert not holder.horizontalScrollBar().isVisible(), (
        "горизонтальная прокрутка появилась там, где места хватает"
    )


# ── 2. Подстройка каркаса окна ─────────────────────────────────────────────

def test_sidebar_starts_collapsed(window):
    """ГЛАВНОЕ (решение пользователя): программа открывается со СВЁРНУТОЙ панелью.

    Так вкладкам достаётся больше места, а развернуть панель человек может сам
    кнопкой внизу. Раньше панель открывалась развёрнутой (210 px).
    """
    import inspect

    from umbranet import theme
    from umbranet.widgets.sidebar import Sidebar

    assert window.sidebar._expanded is False, "панель обязана открываться свёрнутой"
    assert window.sidebar.width() == theme.SIDEBAR_W_COLLAPSED
    assert window.sidebar.width() < theme.SIDEBAR_W_EXPANDED
    # Кнопка разворота должна быть на месте и понятной по направлению стрелки.
    assert window.sidebar._toggle_btn.text() == "»", "свёрнутая панель показывает стрелку вправо"
    # И сам класс по умолчанию свёрнут: любой новый вызов Sidebar() получит значки.
    default = inspect.signature(Sidebar.__init__).parameters["expanded"].default
    assert default is False, f"по умолчанию панель должна быть свёрнута, а не expanded={default}"


def test_sidebar_is_not_touched_by_window_resize(window):
    """Автосворачивания по ширине окна больше НЕТ — панель меняет только человек.

    Раньше окно само решало «узко/широко» и переключало панель: при сужении она
    сворачивалась, при расширении разворачивалась. Пользователь попросил это
    убрать — панель не должна меняться сама.
    """
    from PySide6.QtTest import QTest

    from umbranet import theme

    window.sidebar.set_collapsed(True, animate=False)       # свёрнуто — как при старте
    for width in (560, 720, 960, 1180, 1400):
        window.resize(width, 640)
        for _ in range(4):
            APP.processEvents()
        assert window.sidebar._expanded is False, (
            f"при ширине {window.width()} панель развернулась сама — так быть не должно"
        )
        assert window.sidebar.width() == theme.SIDEBAR_W_COLLAPSED

    # И наоборот: развёрнутую панель окно тоже не должно сворачивать.
    window.sidebar.toggle()                                  # человек развернул
    QTest.qWait(400)                                         # анимация ширины 180 мс
    assert window.sidebar._expanded is True
    assert window.sidebar.width() == theme.SIDEBAR_W_EXPANDED
    for width in (1400, 1180, 960, 720, 560):
        window.resize(width, 640)
        for _ in range(4):
            APP.processEvents()
        assert window.sidebar._expanded is True, (
            f"при ширине {window.width()} панель свернулась сама — так быть не должно"
        )
        assert window.sidebar.width() == theme.SIDEBAR_W_EXPANDED


def test_sidebar_toggle_expands_and_collapses(window):
    """Кнопка внизу панели разворачивает и сворачивает её (это делает человек)."""
    from PySide6.QtTest import QTest

    from umbranet import theme

    window.sidebar.set_collapsed(True, animate=False)
    assert window.sidebar.width() == theme.SIDEBAR_W_COLLAPSED

    window.sidebar._toggle_btn.click()
    QTest.qWait(400)
    assert window.sidebar._expanded is True
    assert window.sidebar.width() == theme.SIDEBAR_W_EXPANDED
    assert window.sidebar._toggle_btn.text() == "«", "развёрнутая панель показывает стрелку влево"

    window.sidebar._toggle_btn.click()
    QTest.qWait(400)
    assert window.sidebar._expanded is False
    assert window.sidebar.width() == theme.SIDEBAR_W_COLLAPSED


def test_topbar_is_a_single_horizontal_row(window):
    """Структурный сторож: верхняя панель — одна строка, режимы и кнопки в ней.

    Если кто-то вернёт «в столбик» (как было сделано при первой правке окна),
    этот тест упадёт первым — ещё до того, как пользователь увидит прыгающие
    кнопки.
    """
    from PySide6.QtWidgets import QBoxLayout

    row = window._topbar_row
    assert row.direction() == QBoxLayout.LeftToRight, (
        "верхняя панель обязана оставаться одной строкой: кнопки не должны переезжать вверх"
    )
    assert row.indexOf(window.mode_switch) != -1, "переключатель режимов обязан быть в этой строке"
    assert row.indexOf(window.control) != -1, "кнопки Старт/Перезапуск обязаны быть в этой же строке"


def _row_y(window, widget) -> int:
    """Вертикаль виджета относительно окна."""
    return widget.mapTo(window, widget.rect().topLeft()).y()


def _right_edge(window, widget) -> int:
    """Правый край виджета относительно окна."""
    return widget.mapTo(window, widget.rect().topLeft()).x() + widget.width()


def test_topbar_never_moves_buttons_between_rows(window):
    """ГЛАВНОЕ (жалоба пользователя): при сужении окна ничего не уезжает вверх.

    Раньше верхняя панель при нехватке места перестраивалась «в столбик», и
    кнопки DNS / Combo / DPI перескакивали в другой ряд — на каждом изменении
    размера картинка прыгала. Теперь панель всегда одна строка, а подписи
    уступают место значкам: проверяем и вертикаль, и то, что ничего не обрезано
    и не налезает друг на друга — во всех состояниях кнопок.
    """
    states = [
        ("остановлено", lambda: window.control.set_running(False, mode="dns_only")),
        ("запущено", lambda: window.control.set_running(True, mode="combo")),
        ("нет прав", lambda: window.control.set_running(True, mode="dns_only", admin_warn=True)),
        ("ошибка", lambda: window.control.set_error(
            "Предстартовая проверка: не удалось запустить DNS-сервер на порту 53")),
        ("генерация", lambda: window.control.set_ai_busy()),
    ]
    for label, set_state in states:
        set_state()
        for width in range(1180, 540, -40):
            window.resize(width, 640)
            for _ in range(3):
                APP.processEvents()
            widgets = {
                "режимы": window.mode_switch,
                "точка": window.control._dot,
                "перезапуск": window.control.btn_restart,
                "старт": window.control.btn_power,
            }
            rows = {name: _row_y(window, w) for name, w in widgets.items()}
            assert len(set(rows.values())) == 1, (
                f"[{label}] при ширине {window.width()} элементы шапки оказались "
                f"в разных рядах: {rows}"
            )
            for name, widget in widgets.items():
                assert _right_edge(window, widget) <= window.width(), (
                    f"[{label}] «{name}» выходит за окно: правый край "
                    f"{_right_edge(window, widget)}, ширина окна {window.width()}"
                )
            restart_left = _right_edge(window, window.control.btn_restart) - window.control.btn_restart.width()
            assert _right_edge(window, window.mode_switch) <= restart_left + 1, (
                f"[{label}] кнопки режимов налезли на «Перезапуск» при ширине {window.width()}"
            )


def test_mode_buttons_shrink_smoothly_with_the_window(window):
    """ГЛАВНОЕ (жалоба пользователя): DNS / Combo / DPI ужимаются ПЛАВНО.

    Раньше кнопки переключались скачком: в определённый момент ширины окна они
    разом становились вдвое мельче — «дергается и не особо красиво». Теперь ширина
    едет вслед за окном: на каждые 25 px окна — считаные пиксели, а подпись по
    дороге укорачивается (DNS Only → DNS… → значок).

    Проверяем именно отсутствие скачков: идём по ширине окна мелким шагом и следим,
    что кнопка не расширяется при сужении и ни на одном шаге не прыгает больше
    20 px (прежнее переключение давало скачок ~80 px за один шаг).
    """
    from umbranet import theme

    blue = theme.MODES["blue"]
    btn = window.mode_switch._buttons["blue"]

    def settle(width: int) -> int:
        window.resize(width, 640)
        for _ in range(4):
            APP.processEvents()
        return btn.width()

    wide = settle(1180)
    assert window.mode_switch.compression() == 0.0, "в широком окне ужима быть не должно"
    assert btn.text() == f"{blue['emoji']}  {blue['name']}", (
        f"в широком окне подпись обязана быть целиком, а не {btn.text()!r}"
    )

    # Вход в ужим не должен дёргать ширину: кнопка стартует ровно с той ширины,
    # которую занимала с подписью (её считает Qt с учётом QSS-отступов, а не
    # шрифтовые метрики — иначе на первом же шаге был бы рывок ~12 px).
    entry = None
    for width in range(1150, 550, -25):
        current = settle(width)
        if window.mode_switch.compression() > 0.0:
            entry = current
            break
    assert entry is not None, "ужим так и не начался: окно сужалось, а кнопки стояли"
    assert abs(entry - wide) <= 6, (
        f"на входе в ужим кнопка прыгнула с {wide} на {entry} px — начало ужима обязано быть незаметным"
    )

    prev = wide
    for width in range(1150, 550, -25):
        current = settle(width)
        assert current <= prev, (
            f"при сужении окна до {width} кнопка вдруг расширилась: {prev} → {current}"
        )
        assert prev - current <= 20, (
            f"при сужении окна до {width} кнопка прыгнула на {prev - current} px "
            f"({prev} → {current}) — ужим обязан быть плавным, без рывков"
        )
        prev = current

    settle(560)
    assert window.mode_switch.compression() == 1.0, (
        "в узком окне ужим обязан дойти до конца (только значки)"
    )
    assert btn.text() == blue["emoji"], f"в узком окне должна остаться только значок, а не {btn.text()!r}"
    assert btn.toolTip() == blue["name"], "название режима обязано переехать в подсказку"
    assert btn.width() * 2 < wide, (
        f"в узком окне кнопка должна быть заметно уже полной: {btn.width()} против {wide}"
    )
    assert window.mode_switch._compact, "флаг ужима обязан быть поднят"

    # Промежуточная ширина: кнопка между полной и значковой — это и есть плавность.
    settle(700)
    t = window.mode_switch.compression()
    assert 0.0 < t < 1.0, f"на промежуточной ширине ужим обязан быть частичным, а не {t}"
    assert btn.width() < wide, "на промежуточной ширине кнопка обязана быть уже полной"
    # Подпись по дороге укорачивается, а не обрезается рамкой: иначе текст просто
    # упирался бы в край кнопки.
    assert btn.text() != f"{blue['emoji']}  {blue['name']}", (
        "на промежуточном ужиме подпись обязана укоротиться, а не остаться целиком"
    )
    assert len(btn.text()) < len(f"{blue['emoji']}  {blue['name']}")
    assert btn.text().startswith(blue["emoji"]), "значок режима обязан оставаться на месте"
    assert btn.toolTip() == blue["name"], "пока подпись укорочена, название живёт в подсказке"

    # Возврат широкого окна — как было.
    assert settle(1180) == wide, "при возврате широкого окна кнопка обязана вернуть прежнюю ширину"
    assert btn.text() == f"{blue['emoji']}  {blue['name']}"
    assert not window.mode_switch._compact, "флаг ужима обязан сняться"


def test_status_text_gives_way_but_dot_stays(window):
    """Текст статуса в узком окне скрывается, а точка-индикатор остаётся с подсказкой."""
    window.control.set_running(True, mode="combo")
    window.resize(1180, 640)
    for _ in range(4):
        APP.processEvents()
    assert window.control._status.isVisible(), "в широком окне текст статуса должен быть виден"
    assert window.control._dot.toolTip() == window.control._status.text(), (
        "подсказка точки обязана повторять текст статуса"
    )

    window.resize(700, 640)
    for _ in range(4):
        APP.processEvents()
    assert not window.control._status.isVisible(), "в узком окне текст статуса прячется"
    assert window.control._dot.isVisible(), "точка-индикатор обязана остаться: по ней видно цвет"
    assert "DNS" in window.control._dot.toolTip(), (
        "состояние должно оставаться доступным в подсказке точки"
    )


def test_controls_stay_visible_in_half_screen_window(window):
    """Кнопка «Старт» не уезжает за край окна в узком режиме."""
    window.resize(700, 560)
    for _ in range(5):
        APP.processEvents()
    button = window.control.btn_power
    right_edge = button.mapTo(window, button.rect().topLeft()).x() + button.width()
    assert right_edge <= window.width(), (
        f"кнопка «Старт» выходит за окно: правый край {right_edge} при ширине {window.width()}"
    )


# ── 3. Запоминание размера окна ────────────────────────────────────────────

def test_geometry_saved_on_resize(state_file, window):
    """Изменил размер — он сохранён (после паузы, а не на каждый пиксель)."""
    window.resize(700, 560)
    for _ in range(3):
        APP.processEvents()

    assert state_file.exists() is False or not saved_geometry(state_file), (
        "файл должен писаться после паузы, а не мгновенно на каждое движение"
    )

    assert window._save_window_geometry() is True
    data = saved_geometry(state_file)
    assert data.get("w") == window.width()
    assert data.get("h") == window.height()


def test_geometry_restored_on_next_launch(state_file, window):
    """ГЛАВНОЕ: следующий запуск открывается в прежнем размере и месте."""
    window.resize(720, 580)
    window.move(40, 60)
    for _ in range(3):
        APP.processEvents()
    assert window._save_window_geometry() is True
    expected_size = (window.width(), window.height())

    # Имитируем следующий запуск: меняем размер, затем восстанавливаем
    # сохранённый — ровно тот путь кода, что выполняется при старте приложения.
    window.resize(800, 640)
    for _ in range(3):
        APP.processEvents()
    assert window._restore_window_geometry() is True, "геометрия не восстановилась"
    assert (window.width(), window.height()) == expected_size, (
        f"размер не тот: {window.width()}x{window.height()} вместо {expected_size}"
    )


def test_geometry_restored_from_numbers_if_blob_missing(state_file, window):
    """Если blob потерялся (старый файл/правка руками), берём размер и позицию числами."""
    import json

    state_file.write_text(json.dumps({
        "window_geometry": {"w": 700, "h": 560, "x": 30, "y": 40, "maximized": False, "blob": ""}
    }), encoding="utf-8")
    assert window._restore_window_geometry() is True
    assert (window.width(), window.height()) == (700, 560)


def test_maximized_flag_is_remembered(state_file, window):
    """«Развёрнуто на весь экран» тоже запоминаем (иначе окно вернётся маленьким)."""
    window.resize(700, 560)
    window.isMaximized = lambda: True          # offscreen не умеет разворачивать по-настоящему
    assert window._save_window_geometry() is True
    assert saved_geometry(state_file).get("maximized") is True


def test_geometry_saved_when_window_hidden_to_tray(state_file, window):
    """Сворачивание в трей = окно скрылось: размер должен быть уже сохранён."""
    window.resize(720, 600)
    for _ in range(3):
        APP.processEvents()
    window.hide()
    for _ in range(3):
        APP.processEvents()

    data = saved_geometry(state_file)
    assert data.get("w"), "при сворачивании в трей размер не сохранился"
    assert data["w"] <= 800 and data["h"] <= 800


def test_first_launch_size_fits_screen(state_file, window):
    """Без сохранённой настройки окно открывается в размер, вписанный в экран."""
    from PySide6.QtGui import QGuiApplication

    avail = QGuiApplication.primaryScreen().availableGeometry()
    width, height = window._default_window_size()

    assert width <= avail.width(), "окно шире экрана"
    assert height <= avail.height(), "окно выше экрана"
    assert width <= int(avail.width() * 0.9) + 1, (
        f"окно занимает почти весь экран: {width} из {avail.width()}"
    )


def test_window_is_pulled_back_onto_screen(state_file, window):
    """Мониторов стало меньше / разрешение изменилось — окно не теряется за краем."""
    import json

    state_file.write_text(json.dumps({
        "window_geometry": {"w": 700, "h": 560, "x": -9000, "y": -9000, "maximized": False, "blob": ""}
    }), encoding="utf-8")
    assert window._restore_window_geometry() is True

    from PySide6.QtGui import QGuiApplication

    frame = window.frameGeometry()
    screens = QGuiApplication.screens()
    assert any(s.availableGeometry().intersects(frame) for s in screens), (
        f"окно осталось за пределами экрана: {frame}"
    )


def test_corrupted_geometry_does_not_break_startup(state_file, window):
    """Испорченная настройка геометрии не должна мешать запуску."""
    import json

    state_file.write_text(json.dumps({"window_geometry": {"w": "не число", "blob": "не base64!"}}),
                          encoding="utf-8")
    assert window._restore_window_geometry() is False
    width, height = window._default_window_size()
    assert width > 0 and height > 0


def test_real_state_file_is_not_touched():
    """Прогон тестов не должен переписывать рабочий umbranet_ui.json проекта."""
    import ui_state

    assert str(ROOT / "umbranet_ui.json") != ui_state.state_path(), (
        "тесты пишут в рабочий файл настроек — это ломает проверку на чистой копии"
    )
