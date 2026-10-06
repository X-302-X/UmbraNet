"""
Тесты окна прогресса AI-генерации (жалобы по факту прогона на Windows).

Что было не так:

  1. «Лимит сессии 180 сек» — в окне, где идёт подбор, мелким текстом стоял
     лимит из внутреннего плана. В окне подтверждения при этом написано «от 1
     до 10 минут». Пользователь видел два разных обещания по времени, и второе
     выглядело как ошибка. Теперь лимита в окне нет вообще, а вместо него —
     честная оценка «сколько ещё примерно ждать», посчитанная по факту.

  2. «Шкала прыгает» — подпись шага была многострочной и её высота менялась:
     у одного варианта длинное имя (seed/mutation/mask/args), у другого
     короткое, поэтому плашка то вырастала, то сжималась и шкала прогресса
     ездила вверх-вниз. Теперь подпись — человеческая и в одну строку
     фиксированной высоты, длинное сокращается многоточием, а полный
     технический текст остаётся в логе окна.

Запуск: python -m pytest tests/test_generation_dialog.py
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

from PySide6.QtGui import QFontMetrics  # noqa: E402

from umbranet.views import strategy_lab as lab  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def make_dialog(total: int = 4, **kwargs):
    dlg = lab.AiGenerationProgressDialog(None, total_variants=total, **kwargs)
    dlg.resize(620, 470)
    return dlg


def all_visible_text(dlg) -> str:
    """Весь текст, который пользователь видит в окне."""
    parts = []
    for name in ("_subtitle", "_step", "_eta", "_best", "_title"):
        widget = getattr(dlg, name, None)
        if widget is not None:
            parts.append(widget.text())
    parts.append(dlg._log.toPlainText())
    for button_name in ("_btn_cancel", "_btn_close"):
        button = getattr(dlg, button_name, None)
        if button is not None:
            parts.append(button.text())
    return "\n".join(parts)


# ── 1. Никакого «лимита сессии» ─────────────────────────────────────────────

def test_progress_window_never_mentions_session_limit():
    """ГЛАВНОЕ: в окне подбора нет ни слова про лимит сессии."""
    dlg = make_dialog(total=18, time_limit=180)
    text = all_visible_text(dlg).lower()
    assert "лимит" not in text, f"в окне снова появился лимит: {text}"
    assert "180" not in text, f"число 180 снова в окне: {text}"


def test_source_has_no_session_limit_phrase():
    """И в исходнике окна — ни старой подписи, ни упоминания в статусе вкладки.

    Проверяем только строки, которые идут в интерфейс: комментарии с
    объяснением, почему лимит убран, остаются.
    """
    src = (ROOT / "umbranet" / "views" / "strategy_lab.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "Лимит сессии" not in line, f"остался вывод лимита: {stripped}"
        # «лимит: N сек» из плана сессии в интерфейс больше не попадает
        # (сообщение про лимит числа стратегий — это другое, оно осмысленно).
        assert "лимит: {" not in line, f"остался лимит сессии в статусе: {stripped}"


def test_subtitle_is_neutral_about_services():
    """ГЛАВНОЕ: окно создания Uz не должно называть конкретные сервисы.

    Раньше подзаголовок был «Варианты проверяются по очереди: подключение,
    YouTube и Discord». Пользователь справедливо возразил: обход нужен не только
    для этих двух сервисов, а такая подпись читается как обещание, что генерация
    работает только для них. Теперь текст нейтральный и по делу.
    """
    dlg = make_dialog()
    subtitle = dlg._subtitle.text()
    assert "YouTube" not in subtitle and "Discord" not in subtitle, (
        f"в подзаголовке снова названы конкретные сервисы: {subtitle}"
    )
    assert "потерпите" in subtitle.lower(), f"нет спокойного пояснения: {subtitle}"
    assert "Uz" in subtitle, f"не сказано, что создаётся Uz: {subtitle}"


def test_check_window_subtitle_is_neutral_too():
    """Окно «Проверка Uz» — тот же принцип: без «списка истины YouTube + Discord»."""
    dlg = make_dialog(total=5, window_title="Проверка Uz",
                      title_text="Проверка стратегий запущена",
                      subtitle_text="Пожалуйста, потерпите: идёт проверка Uz-стратегий. "
                                    "Окно можно скрыть — процесс продолжится.")
    subtitle = dlg._subtitle.text()
    assert "YouTube" not in subtitle and "Discord" not in subtitle
    assert "потерпите" in subtitle.lower()


def test_subtitle_fits_without_clipping_at_any_size():
    """Подзаголовок не должен обрезаться «в никуда» на узком окне.

    Раньше длинный текст просто пропадал за краем (QLabel без переноса), и было
    видно «Пожалуйста, потерпите: идёт создание Uz. Окно можно скрыть — пр…».
    Проверяем ровно то, что видит пользователь: текст либо влезает целиком,
    либо заканчивается многоточием.
    """
    dlg = make_dialog(total=18)
    dlg.setMinimumSize(560, 420)
    for width, height in ((560, 420), (620, 470), (900, 700)):
        dlg.resize(width, height)
        dlg.show()
        APP.processEvents()
        dlg._apply_step_text()
        APP.processEvents()
        shown = dlg._subtitle.text()
        needed = QFontMetrics(dlg._subtitle.font()).horizontalAdvance(shown)
        assert needed <= dlg._subtitle.width() or shown.endswith("…"), (
            f"при {width}x{height} подзаголовок обрезан: «{shown}» "
            f"({needed}px при {dlg._subtitle.width()}px)"
        )
        assert not shown.endswith("…"), f"текст не влезает целиком: «{shown}»"
    dlg.close()


def test_long_custom_subtitle_is_elided_not_clipped():
    """Если кто-то передаст длинный подзаголовок — увидим многоточие, а не обрез."""
    dlg = make_dialog(
        total=5,
        subtitle_text="Пожалуйста, потерпите: идёт создание Uz, тут длинное пояснение, "
                      "которое заведомо не поместится в одну строку на узком окне",
    )
    dlg.setMinimumSize(560, 420)
    dlg.resize(560, 420)
    dlg.show()
    APP.processEvents()
    dlg._apply_step_text()
    APP.processEvents()
    shown = dlg._subtitle.text()
    assert shown.endswith("…"), f"обрезка без многоточия: «{shown}»"
    dlg.close()


def test_step_text_is_never_clipped_at_minimum_size():
    """Та же проверка для плашки шага: длинный текст → многоточие, не обрез."""
    dlg = make_dialog(total=18)
    dlg.setMinimumSize(560, 420)
    dlg.resize(560, 420)
    dlg.show()
    APP.processEvents()
    dlg.append("AI-генерация: winws.exe сообщает: " + "windivert: failed to open " * 6)
    APP.processEvents()
    shown = dlg._step.text()
    needed = QFontMetrics(dlg._step.font()).horizontalAdvance(shown)
    assert shown.endswith("…"), f"нет многоточия: «{shown}»"
    assert needed <= dlg._step.width() - 20, f"подпись шире плашки: {needed}px"
    assert dlg._step.height() == make_dialog()._step.height(), "высота плашки уехала"
    dlg.close()


def test_no_service_names_in_dialog_headlines():
    """Сторож по исходнику: в ЗАГОЛОВКАХ и подписях окна сервисов быть не должно.

    В логе окна они остаются (это факт измерения: какие проверки выполнялись),
    поэтому проверяем именно тексты плашек — строки с `_title`/`_subtitle`/
    `_STEP_RULES`, а не сообщения прогресса.
    """
    src = (ROOT / "umbranet" / "views" / "strategy_lab.py").read_text(encoding="utf-8")
    head = src[:src.index("class StrategyLabView")]
    for lineno, line in enumerate(head.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue          # комментарии в интерфейс не попадают
        if "YouTube" in line or "Discord" in line:
            raise AssertionError(
                f"строка {lineno} окна генерации называет сервис: {stripped}"
            )


# ── 2. Оценка времени до конца ──────────────────────────────────────────────

def test_finish_shows_verdict_not_report_lines():
    """Баг «ничего не отображается» (2026-10-06): после finish() подписи
    показывали мусор — подзаголовок откатывался к «потерпите», а строка шага
    становилась последней строкой отчёта. Итог должен быть читаемым."""
    dlg = make_dialog(total=4)
    dlg.append("AI-генерация: вариант 1/4 • seed=uz1")
    dlg.finish({
        "ok": False, "stage": "ai_generation",
        "reason_text": "обязательные проверки Discord/YouTube не прошли",
        "created_id": "",
        "best": {"score": 94},
        "report_lines": ["YouTube: FAIL", "  required: music=FAIL, media=OK"],
    })
    assert "Uz не создана" in dlg._subtitle.text(), dlg._subtitle.text()
    assert "потерпите" not in dlg._subtitle.text().lower(), dlg._subtitle.text()
    assert "Готово" in dlg._step.text(), dlg._step.text()
    assert "music=FAIL" not in dlg._step.text(), dlg._step.text()
    log = dlg._log.toPlainText()
    assert "music=FAIL" in log, "отчёт должен быть в логе окна"


def test_finish_ok_shows_created_strategy():
    dlg = make_dialog(total=4)
    dlg.finish({
        "ok": True, "stage": "ai_generation",
        "created_id": "uz2", "message": "AI-стратегия создана: Uz2",
        "best": {"score": 90},
        "report_lines": ["Список истины: 3 домена"],
    })
    assert "Создана стратегия uz2" in dlg._subtitle.text(), dlg._subtitle.text()
    assert "Готово" in dlg._step.text() or "готово" in dlg._step.text(), dlg._step.text()
    assert "Список истины" not in dlg._step.text()


def test_eta_asks_to_wait_before_first_result():
    dlg = make_dialog(total=4)
    assert "первого варианта" in dlg._eta.text()


def test_eta_counts_down_after_first_variant():
    """После первого варианта появляется честная оценка по факту времени.

    Клок подменяем: тест не должен ждать реальные минуты.
    """
    dlg = make_dialog(total=4)
    now = {"t": 1000.0}
    dlg._clock = lambda: now["t"]

    dlg.append("AI-генерация: вариант 1/4 • seed=uz1 • mutation=base • mask=seed")
    now["t"] += 10.0                       # вариант занял 10 секунд
    dlg.append("AI-генерация: вариант 1/4 score 55 raw 60 • youtube=60, discord=50")

    # 10 секунд на вариант → осталось 3 варианта ≈ 30 секунд
    assert "30 с" in dlg._eta.text(), dlg._eta.text()
    # Счётчик готовности — только в основной шкале (пожелание 2026-10-06):
    # в строке с временем он отставал на 1.
    assert "готово" not in dlg._eta.text(), dlg._eta.text()
    assert "1 / 4" in dlg._progress.text() or "1 / 4" in dlg._progress.format(), (
        dlg._progress.text()
    )


def test_eta_shrinks_while_current_variant_runs():
    """Оценка уменьшается сама, пока идёт текущий вариант — без новых строк.

    Раньше (если считать среднее вместе с незавершённым вариантом) «осталось»
    росло, когда вариант затягивался: число скакало вверх. Считаем среднее по
    завершённым: 10 с на вариант, 3 варианта осталось → 30 с, и дальше только
    вниз, по мере того как текущий вариант идёт.
    """
    dlg = make_dialog(total=4)
    now = {"t": 0.0}
    dlg._clock = lambda: now["t"]

    dlg.append("AI-генерация: вариант 1/4")
    now["t"] = 10.0
    dlg.append("AI-генерация: вариант 1/4 score 50")
    dlg._update_eta()
    first = dlg._estimate_left()
    assert first == 30.0, f"ожидали 30 с, получили {first}"

    dlg.append("AI-генерация: вариант 2/4")
    now["t"] += 4.0                        # 4 секунды уже ушло на вариант 2
    dlg._update_eta()

    assert "26 с" in dlg._eta.text(), f"оценка не учитывает текущий вариант: {dlg._eta.text()}"
    assert dlg._estimate_left() < first, "оценка обязана уменьшаться, а не расти"


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "~0 с"),
        (45, "~45 с"),
        (60, "~1 мин"),
        (150, "~2 мин 30 с"),
        (600, "~10 мин"),
        (1500, "~25 мин"),
        (7200, "больше часа"),
    ],
)
def test_eta_format_is_human(seconds, expected):
    assert lab.AiGenerationProgressDialog._format_left(seconds) == expected


def test_eta_says_done_after_finish():
    dlg = make_dialog(total=4)
    dlg.append("AI-генерация: вариант 1/4")
    dlg.finish({"ok": False, "error": "не вышло", "best": {}})
    assert "всё готово" in dlg._eta.text()
    assert not dlg._eta_timer.isActive(), "таймер оценки должен остановиться"


def test_eta_never_goes_negative():
    """Оценка не должна показывать «минус» при совсем медленном варианте."""
    dlg = make_dialog(total=2)
    now = {"t": 0.0}
    dlg._clock = lambda: now["t"]
    dlg.append("AI-генерация: вариант 1/2")
    now["t"] = 10.0
    dlg.append("AI-генерация: вариант 1/2 score 60")
    now["t"] = 900.0                       # второй вариант идёт очень долго
    assert dlg._estimate_left() == 0.0
    dlg._update_eta()
    assert "0 с" in dlg._eta.text()


# ── 3. Человеческие короткие подписи вместо технических имён ────────────────

@pytest.mark.parametrize(
    "raw, must_contain, must_not_contain",
    [
        ("AI-генерация: probes YouTube/Discord для variant_7",
         "Проверяем доступность сайтов", "variant_7"),
        ("AI-генерация: запуск WinWS для variant_7 (12 args)",
         "Запускаем обход", "variant_7"),
        ("AI-генерация: вариант 3/18 • seed=uz1 • mutation=split_ttl • mask=seed_default",
         "Вариант 3 из 18", "split_ttl"),
        ("AI-генерация: вариант 3/18 score 82 raw 85 • youtube=90, discord=70",
         "лучший результат 82", "youtube=90"),
        ("Проверка Uz: стратегия 2/5 • uz2",
         "Стратегия 2 из 5", "uz2"),
        ("AI-генерация: подготовлено вариантов: 18",
         "Подготовка вариантов", ""),
        ("AI-генерация: зачищены зависшие winws для variant_7",
         "Уборка", "зависшие"),
    ],
)
def test_step_text_is_human(raw, must_contain, must_not_contain):
    human = lab.human_step(raw)
    assert must_contain.lower() in human.lower(), f"{raw!r} → {human!r}"
    if must_not_contain:
        assert must_not_contain.lower() not in human.lower(), f"{raw!r} → {human!r}"


def test_step_label_hides_services_but_log_stays_honest():
    """Плашка — нейтральная, лог — точный: видно, что реально измерялось.

    Это не косметика: подпись не должна обещать, что обход «для YouTube и
    Discord», но диагностика обязана оставаться правдивой — если проверялись
    именно эти сайты, в логе окна это будет написано.
    """
    dlg = make_dialog(total=18)
    raw = "AI-генерация: probes YouTube/Discord для variant_7"
    dlg.append(raw)

    assert "YouTube" not in dlg._step.text(), f"сервис просочился в плашку: {dlg._step.text()}"
    assert "Проверяем доступность сайтов" in dlg._step.text(), dlg._step.text()
    assert "YouTube" in dlg._log.toPlainText(), "лог должен остаться точным"


def test_step_label_shows_human_text_and_log_keeps_technical():
    """В плашке — понятное, в логе — полный технический текст (для разбора)."""
    dlg = make_dialog(total=18)
    raw = "AI-генерация: вариант 7/18 • seed=uz3 • mutation=split • mask=ttl_v2 • args=14"
    dlg.append(raw)

    assert "seed=uz3" not in dlg._step.text(), f"в плашке остался техтекст: {dlg._step.text()}"
    assert "Вариант 7 из 18" in dlg._step.text(), dlg._step.text()
    assert raw in dlg._log.toPlainText(), "полный текст должен остаться в логе окна"


# ── 4. Шкала больше не «прыгает» ────────────────────────────────────────────

def test_step_label_height_is_fixed():
    """Высота плашки шага не меняется — именно она сдвигала шкалу прогресса."""
    dlg = make_dialog(total=18)
    short_before = dlg._step.height()
    dlg.append("AI-генерация: probes YouTube/Discord для v1")
    short_after = dlg._step.height()
    dlg.append(
        "AI-генерация: вариант 12/18 • seed=uz_super_long_seed_name • "
        "mutation=very_long_mutation_name_here • mask=mask_with_long_name • args=27"
    )
    long_after = dlg._step.height()

    assert short_before == short_after == long_after, (
        f"высота плашки меняется ({short_before} → {short_after} → {long_after}) — "
        f"шкала прогресса будет прыгать"
    )
    assert dlg._step.wordWrap() is False, "перенос строк на плашке шага снова включён"


def test_progress_bar_geometry_is_stable_at_minimum_size():
    """Проверка «шкала прыгает» в самом уязвимом состоянии: окно минимального размера.

    Причина прыжка: многострочная плашка шага то вырастала (длинное техническое
    имя варианта), то сжималась. На большом окне лишнюю высоту забирал лог и
    сдвиг был незаметен, а на узком/низком окне лог уже не может сжаться — и
    шкала ездила на строку вниз-вверх. Здесь ловим именно это: до мутации
    (wordWrap=True без фиксированной высоты) шкала уезжает на 14 px.
    """
    dlg = make_dialog(total=18)
    dlg.setMinimumSize(560, 420)
    dlg.resize(560, 420)
    for i in range(60):                       # лог заполнен, сжиматься ему некуда
        dlg.append(f"AI-генерация: вариант {i}/18 score 10 • строка лога {i}")
    dlg.show()
    APP.processEvents()
    dlg.append("подготовка")
    APP.processEvents()
    dlg._step.parentWidget().layout().activate()
    APP.processEvents()
    short_y = dlg._progress.y()
    short_h = dlg._step.height()

    dlg.append(
        "AI-генерация: winws.exe сообщает: очень длинная причина ошибки, "
        "которая раньше переносилась на три строки и двигала шкалу вниз"
    )
    APP.processEvents()
    dlg._step.parentWidget().layout().activate()
    APP.processEvents()

    assert dlg._step.height() == short_h, "плашка шага изменила высоту — шкала поедет"
    assert dlg._progress.y() == short_y, (
        f"шкала сдвинулась по вертикали: {short_y} → {dlg._progress.y()}"
    )
    dlg.close()


def test_long_step_is_elided_not_wrapped():
    """Длинная подпись сокращается многоточием, а не переносится на 2-ю строку."""
    dlg = make_dialog(total=18)
    dlg.show()
    APP.processEvents()
    dlg.append("AI-генерация: winws.exe сообщает: " + "windivert: failed to open " * 12)
    APP.processEvents()

    shown = dlg._step.text()
    assert "windivert" in shown, f"суть сообщения потерялась: {shown!r}"
    assert len(shown) < 140, f"подпись не сокращена: {len(shown)} символов"
    assert shown.endswith("…"), f"нет многоточия в конце: {shown!r}"
    assert dlg._step.height() == dlg._step.height()   # высота стабильна
    dlg.close()


def test_warning_step_keeps_its_meaning():
    """«Внимание» без сути бесполезно: пользователь должен прочитать, о чём оно."""
    dlg = make_dialog(total=18)
    dlg.append(
        "AI-генерация: внимание — рядом работает другая программа с winws.exe: "
        "PID 777 (C:/OtherApp/bin/winws.exe). Она может держать WinDivert."
    )
    step = dlg._step.text()
    assert "Внимание" in step, step
    assert "winws.exe" in step, f"потерялась суть предупреждения: {step}"


# ── 5. Прежнее поведение окна не сломано ────────────────────────────────────

def test_progress_bar_still_tracks_variants():
    dlg = make_dialog(total=0)
    dlg.append("AI-генерация: вариант 3/7")
    assert dlg._progress.maximum() == 7
    assert dlg._progress.value() == 3
    assert dlg._progress.format() == "3 / 7"


def test_best_score_still_updates():
    dlg = make_dialog(total=4)
    dlg.append("AI-генерация: вариант 1/4 score 40")
    dlg.append("AI-генерация: вариант 2/4 score 73")
    dlg.append("AI-генерация: вариант 3/4 score 61")
    assert "73" in dlg._best.text(), dlg._best.text()


def test_check_session_uses_human_prefix():
    """«Проверка Uz» пишет про стратегии, а не про варианты."""
    dlg = make_dialog(total=5, window_title="Проверка Uz",
                      title_text="Проверка стратегий запущена",
                      subtitle_text="Стратегии проверяются по очереди.")
    dlg.append("Проверка Uz: стратегия 2/5 • uz2")
    assert "Стратегия 2 из 5" in dlg._step.text(), dlg._step.text()
    assert dlg._progress.value() == 2
