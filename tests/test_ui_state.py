"""
Тесты единого состояния UI — umbranet_ui.json (P1-2).

Что было сломано. Файл писали два модуля независимо:
  • `umbranet/theme.py` — выбранная тема;
  • `umbranet/engine_adapter.py` — «Авто»-транспорт, порядок вкладок, избранное.
Каждый делал «прочитал → поменял свой ключ → записал файл целиком» через
`open(path, "w")`, без блокировки и без атомарности. Последствия:

  1. Гонка (lost update): выбор темы затирал чужие ключи и наоборот. Пользователь
     видел, что настройки «сами сбрасываются», хотя ничего не сбрасывалось —
     их перезаписывал второй писатель.
  2. Краш на записи: `open(..., "w")` сначала обнуляет файл. Падение процесса
     между обнулением и записью оставляло пустой JSON. Читатель ловил исключение
     и молча возвращал `{}`, а причина нигде не оставалась.

Что проверяем: один путь к файлу на все модули, сохранение чужих ключей,
устойчивость к гонке из нескольких потоков, отсутствие обрезанного файла при
сбое записи и откладывание битого файла в `.broken-*` вместо тихой потери.

С появлением версии схемы (M2) файл несёт служебный ключ `state_version`;
сравнения ключей настроек идут через `without_version()`, а сама версия
проверяется тестами в `tests/test_config_migrations.py`.

Запуск: python -m pytest tests/test_ui_state.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ui_state  # noqa: E402


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    """Своё состояние UI на каждый тест — рабочий файл проекта не трогаем."""
    path = tmp_path / "umbranet_ui.json"
    monkeypatch.setenv("UMBRANET_UI_STATE", str(path))
    monkeypatch.setenv("UMBRANET_ROLLBACK_ROOT", str(tmp_path / "recovery"))
    ui_state.set_state_path(None)          # сбрасываем возможное переопределение
    yield path
    ui_state.set_state_path(None)


def read_raw(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def without_version(data: dict) -> dict:
    """Состояние без служебного ключа версии схемы (M2).

    Файл, записанный программой, всегда несёт `state_version`; ключи настроек
    проверяем отдельно от него, чтобы сравнения оставались строгими.
    """
    return {k: v for k, v in data.items() if k != ui_state.STATE_VERSION_KEY}


def leftover_temps(path: pathlib.Path) -> list[str]:
    return [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def broken_files(path: pathlib.Path) -> list[str]:
    directory = path.parent / "recovery" / "config"
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if ".broken-" in p.name)


# ── 1. Один путь на все модули ──────────────────────────────────────────────

def test_doctor_notifications_are_disabled_for_new_ui_state(state_file):
    """Новый пользователь не получает фоновые уведомления без явного согласия."""
    from umbranet.engine_adapter import get_doctor_notify

    assert get_doctor_notify() is False
    ui_state.update_state(doctor_notify=True)
    assert get_doctor_notify() is True, "сохранённый ручной выбор должен остаться включённым"


def test_all_modules_use_same_state_file(state_file, monkeypatch):
    """Путь к состоянию UI считается в одном месте.

    Раньше `theme.py` и `engine_adapter.py` вычисляли его независимо — стоит
    разойтись, и один модуль перестанет видеть состояние другого.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")

    import engine_adapter
    from umbranet import theme

    assert theme._ui_state_file() == ui_state.state_path()
    assert engine_adapter._load_ui_state() == {}          # читает тот же файл
    engine_adapter._patch_ui_state("probe", 1)
    assert read_raw(state_file)["probe"] == 1
    theme.save_theme_preference("neon")
    assert read_raw(state_file)["probe"] == 1, "тема затёрла чужой ключ"


# ── 2. Сохранение чужих ключей ──────────────────────────────────────────────

def test_update_state_keeps_other_keys(state_file):
    ui_state.save_state({"theme": "neon", "nav_order": ["home"]})
    assert ui_state.update_state(auto_transport=True) is True

    data = read_raw(state_file)
    assert data[ui_state.STATE_VERSION_KEY] == ui_state.STATE_VERSION
    assert without_version(data) == {
        "theme": "neon",
        "nav_order": ["home"],
        "auto_transport": True,
    }


def test_update_state_creates_file_and_folder(tmp_path, monkeypatch):
    """Папки ещё нет (первый запуск) — файл всё равно должен появиться."""
    path = tmp_path / "nested" / "umbranet_ui.json"
    monkeypatch.setenv("UMBRANET_UI_STATE", str(path))
    assert ui_state.update_state(theme="neon") is True
    assert read_raw(path)["theme"] == "neon"


def test_save_state_rejects_non_dict(state_file):
    assert ui_state.save_state(["не", "словарь"]) is False
    assert not state_file.exists()


# ── 3. Гонка двух модулей (главный регресс) ─────────────────────────────────

def test_concurrent_writers_do_not_lose_each_other(state_file):
    """ГЛАВНЫЙ тест P1-2: два модуля пишут одновременно — данные не теряются.

    theme.py сохраняет тему, engine_adapter.py — свои ключи. При старом коде
    (read-modify-write без блокировки) почти каждый прогон терял часть ключей.
    """
    topic_writes, adapter_writes = 40, 40

    def theme_module():
        for i in range(topic_writes):
            ui_state.update_state(theme=f"theme-{i}")

    def adapter_module():
        for i in range(adapter_writes):
            ui_state.update_state(auto_transport=bool(i % 2), nav_order=[f"k{i}"])

    threads = [threading.Thread(target=theme_module), threading.Thread(target=adapter_module)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    data = read_raw(state_file)                 # файл валиден (не обрезан)
    assert "theme" in data, "тема потерялась — её затёр второй писатель"
    assert "nav_order" in data, "порядок вкладок потерялся"
    assert "auto_transport" in data, "«Авто»-транспорт потерялся"


def test_many_threads_keep_all_keys(state_file):
    """Восемь «модулей» пишут свои ключи — на выходе должны быть все восемь."""
    keys = [f"key-{i}" for i in range(8)]

    def writer(key: str):
        for _ in range(25):
            ui_state.update_state(**{key: True})

    threads = [threading.Thread(target=writer, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    data = read_raw(state_file)
    missing = [k for k in keys if not data.get(k)]
    assert not missing, f"потеряны ключи при параллельной записи: {missing}"


def test_mutate_state_reads_and_writes_under_one_lock(state_file):
    """Список вкладок: изменение через mutator не теряет чужую работу."""
    ui_state.save_state({"nav_order": ["home"]})

    def add_tab(state):
        state.setdefault("nav_order", []).append("map")
        state["theme"] = "neon"        # побочный ключ другого модуля

    assert ui_state.mutate_state(add_tab) is True
    assert without_version(read_raw(state_file)) == {"nav_order": ["home", "map"], "theme": "neon"}


# ── 4. Атомарность: нет обрезанного файла ───────────────────────────────────

def test_failed_replace_keeps_old_file(state_file, monkeypatch):
    """Падение на моменте подмены файла: старая версия цела, мусор убран."""
    ui_state.save_state({"theme": "neon"})

    def boom(src, dst):
        raise OSError("диск отвалился")

    monkeypatch.setattr(ui_state.os, "replace", boom)
    assert ui_state.save_state({"theme": "другая"}) is False

    assert without_version(read_raw(state_file)) == {"theme": "neon"}, "старое состояние затёрто"
    assert leftover_temps(state_file) == [], "остался временный файл"


def test_crash_while_writing_keeps_old_file(state_file, monkeypatch):
    """Обрыв записи (как при краше) не оставляет полупустой JSON.

    Раньше данные писались прямо в рабочий файл, поэтому обрыв давал пустой
    или обрезанный JSON. Теперь пишем во временный файл и подменяем целиком.
    """
    ui_state.save_state({"theme": "neon"})
    original_dump = json.dump

    def half_dump(obj, f, **kwargs):
        f.write('{"theme": "ne')            # половина JSON
        raise KeyboardInterrupt("процесс убили")

    monkeypatch.setattr(json, "dump", half_dump)
    with pytest.raises(KeyboardInterrupt):
        ui_state.save_state({"theme": "другая"})
    monkeypatch.setattr(json, "dump", original_dump)

    assert without_version(read_raw(state_file)) == {"theme": "neon"}
    assert leftover_temps(state_file) == []


def test_reader_never_sees_partial_file(state_file):
    """Во время записи читатель видит либо старую, либо новую версию."""
    ui_state.save_state({"theme": "neon", "nav_order": [str(i) for i in range(50)]})
    stop = threading.Event()
    failures = []

    def reader():
        while not stop.is_set():
            try:
                data = ui_state.load_state()
                if data and sorted(data) not in (
                    ["nav_order", "state_version", "theme"],
                    ["state_version", "theme"],
                ):
                    failures.append(data)
            except Exception as exc:      # сюда попадать не должны
                failures.append(exc)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(30):
            ui_state.save_state({"theme": f"t{i}", "nav_order": [str(i)]})
    finally:
        stop.set()
        t.join(timeout=10)

    assert not failures, f"читатель увидел битое состояние: {failures[:2]}"


# ── 5. Битый файл откладывается, а не съедается молча ───────────────────────

@pytest.mark.parametrize(
    "content, title",
    [
        ("", "пустой файл (обрыв записи)"),
        ("{это не json", "битый JSON"),
        ('["не", "объект"]', "не словарь"),
        ("null", "null вместо состояния"),
    ],
)
def test_broken_file_is_quarantined(state_file, content, title):
    state_file.write_text(content, encoding="utf-8")

    assert ui_state.load_state() == {}, f"{title}: должны вернуть пустое состояние"
    assert not state_file.exists(), "битый файл должен быть отложен, а не оставлен на месте"
    saved = broken_files(state_file)
    assert saved, f"{title}: копия не сохранена, следы потери настроек недоступны"
    quarantined = state_file.parent / "recovery" / "config" / saved[0]
    assert quarantined.read_text(encoding="utf-8") == content


def test_missing_file_is_not_an_error(state_file):
    assert ui_state.load_state() == {}
    assert state_file.exists() is False
    assert broken_files(state_file) == [], "отсутствие файла — не «повреждение»"


def test_state_survives_repeated_reads(state_file):
    """Чтение не портит файл и не плодит .broken (частая причина ложных сбросов)."""
    ui_state.save_state({"theme": "neon"})
    for _ in range(5):
        assert ui_state.load_state()["theme"] == "neon"
    assert broken_files(state_file) == []


def test_theme_and_adapter_share_state_end_to_end(state_file):
    """Интеграция: тема + ключи адаптера живут в одном файле и не мешают друг другу."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")

    import engine_adapter
    from umbranet import theme

    engine_adapter.set_auto_transport(True)
    engine_adapter.set_nav_order(["home", "dpi"], ["home", "dpi", "map"])
    engine_adapter.set_favorite_services(["youtube"], ["youtube", "discord"])
    theme_name = theme.save_theme_preference(theme.DEFAULT_THEME)

    data = read_raw(state_file)
    assert data["auto_transport"] is True
    assert data["nav_order"] == ["home", "dpi"]
    assert data["favorite_services"] == ["youtube"]
    assert data["theme"] == theme_name
    # и обратно: состояние читается теми же функциями
    assert engine_adapter.auto_transport_enabled() is True
    assert engine_adapter.get_nav_order(["map", "home"]) == ["home", "map"]
    assert engine_adapter.get_favorite_services(["youtube"]) == ["youtube"]


def test_auto_transport_on_for_new_install(state_file):
    """Скачали приложение — в «Маршрут DNS» сразу «Авто», без ключа в файле."""
    assert not state_file.exists()
    import engine_adapter
    assert engine_adapter.auto_transport_enabled() is True
    engine_adapter.set_auto_transport(False)
    assert engine_adapter.auto_transport_enabled() is False
