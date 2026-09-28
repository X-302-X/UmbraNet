"""
Тесты версий схемы и миграций — M1/M2/M3 (`core/config_utils.py`, `core/ui_state.py`,
`core/schema_version.py`).
====================================================================================

(Пункт 4 плана.)

Зачем. У программы два файла состояния — `config.json` и `umbranet_ui.json`.
Раньше оба читались «как есть»: если поле убрали или поменяли у него смысл,
старый файл продолжал нести старое значение, и оно молча побеждало новое
умолчание. Человек видел «настройка сбросилась сама» или «новая функция не
работает» — а причина была в версии файла.

Что проверяем: номер версии появляется в файле, старый файл без номера
поднимается до текущей версии, устаревшие поля вычищаются (в том числе то,
которое реально ломало DPI), файл из более новой версии программы не
перезаписывается, миграции идемпотентны и мусор в номере версии не ломает чтение.

Запуск: python -m pytest tests/test_config_migrations.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import schema_version
import ui_state
from config_utils import (
    CONFIG_VERSION,
    CONFIG_VERSION_KEY,
    DEFAULT_CONFIG,
    LEGACY_CONFIG_FIELDS,
    apply_config_migrations,
    load_config_file,
    save_config_file,
)


def write_json(path: pathlib.Path, data) -> pathlib.Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── Механизм версий (core/schema_version.py) ────────────────────────────────

@pytest.mark.parametrize("raw_value, expected", [
    (1, 1),
    ("2", 2),
    ("  3 ", 3),
    (0, 0),
    (None, 0),
    ("мусор", 0),
    ("", 0),
    (-5, 0),
    (True, 0),          # bool — не версия, хотя в Python это подкласс int
    (2.5, 0),
    ([1], 0),
])
def test_read_version_handles_garbage(raw_value, expected):
    """Номер версии из файла: мусор читается как «версии нет», а не как ошибка."""
    assert schema_version.read_version({CONFIG_VERSION_KEY: raw_value}, CONFIG_VERSION_KEY) == expected


def test_read_version_of_non_dict():
    assert schema_version.read_version("не словарь", CONFIG_VERSION_KEY) == 0
    assert schema_version.read_version(None, CONFIG_VERSION_KEY) == 0


def test_migrate_applies_steps_in_order():
    """Цепочка миграций применяется по шагам и в правильном порядке."""
    order = []

    def step0(cfg):
        order.append("0→1")
        cfg["a"] = 1
        return "поставили a"

    def step1(cfg):
        order.append("1→2")
        cfg["b"] = 2
        return "поставили b"

    cfg, report = schema_version.migrate(
        {"a": 0}, version_key="v", target_version=2, migrations={0: step0, 1: step1})

    assert order == ["0→1", "1→2"], f"порядок миграций: {order}"
    assert cfg == {"a": 1, "b": 2, "v": 2}
    assert report["from_version"] == 0 and report["to_version"] == 2
    assert report["notes"] == ["поставили a", "поставили b"]
    assert report["changed"] is True and report["newer"] is False


def test_migrate_skips_missing_steps():
    """Шаг без описанной миграции — не ошибка: меняется только номер версии."""
    cfg, report = schema_version.migrate(
        {"x": 1, "v": 0}, version_key="v", target_version=3, migrations={})
    assert cfg == {"x": 1, "v": 3}, f"получилось {cfg}"
    assert report["notes"] == []


def test_migrate_does_not_touch_input():
    """Миграция работает с копией: исходный словарь не меняется."""
    original = {"v": 0, "field": "старое"}
    cfg, _ = schema_version.migrate(
        original, version_key="v", target_version=1,
        migrations={0: lambda c: c.pop("field", None) and None})
    assert original == {"v": 0, "field": "старое"}, "исходные данные изменены"
    assert cfg["v"] == 1


def test_migrate_is_idempotent():
    """Повторная миграция уже мигрированного словаря ничего не меняет."""
    first, _ = apply_config_migrations({"listen_port": 53})
    second, report = apply_config_migrations(first)

    assert second == first, "повторная миграция изменила конфиг"
    assert report["notes"] == [] and report["changed"] is False


def test_newer_file_is_not_migrated_and_not_changed():
    """Файл из более новой версии программы: не мигрируем и не трогаем."""
    raw = {"v": 99, "поле_будущего": "значение"}
    cfg, report = schema_version.migrate(
        raw, version_key="v", target_version=1,
        migrations={0: lambda c: c.update({"испорчено": True}) or None})

    assert report["newer"] is True
    assert cfg == raw, f"файл из будущего изменён: {cfg}"
    assert "испорчено" not in cfg and cfg["v"] == 99, "номер версии понижен"


def test_migrate_non_dict_returns_empty():
    cfg, report = schema_version.migrate(
        "мусор", version_key="v", target_version=1, migrations={})
    assert cfg == {} and report["changed"] is False


# ── M1: версия и миграции config.json ───────────────────────────────────────

def test_config_has_version_in_defaults():
    """Текущая версия схемы лежит в умолчаниях — значит, пишется во все новые файлы."""
    assert DEFAULT_CONFIG[CONFIG_VERSION_KEY] == CONFIG_VERSION
    assert CONFIG_VERSION >= 1


def test_new_config_file_gets_version(tmp_path):
    """Первый запуск: файла нет — создаётся сразу с номером версии."""
    path = tmp_path / "config.json"
    cfg = load_config_file(str(path))

    assert cfg[CONFIG_VERSION_KEY] == CONFIG_VERSION
    assert read_json(path)[CONFIG_VERSION_KEY] == CONFIG_VERSION, "версия не записана в файл"


def test_saved_config_carries_version(tmp_path):
    """save_config_file всегда пишет номер версии, даже если его не передали."""
    path = tmp_path / "config.json"
    save_config_file(str(path), {"listen_port": 5353})
    assert read_json(path)[CONFIG_VERSION_KEY] == CONFIG_VERSION


def test_old_file_without_version_is_migrated(tmp_path, caplog):
    """Файл прошлой версии (без номера) обновляется при первом же чтении."""
    path = write_json(tmp_path / "config.json", {"listen_port": 5300, "tls_verify": False})
    with caplog.at_level("INFO"):
        cfg = load_config_file(str(path))

    assert cfg[CONFIG_VERSION_KEY] == CONFIG_VERSION
    on_disk = read_json(path)
    assert on_disk[CONFIG_VERSION_KEY] == CONFIG_VERSION, "обновлённый файл не пересохранён"
    assert on_disk["listen_port"] == 5300, "пользовательская настройка потерялась"
    assert on_disk["tls_verify"] is False, "настройка пользователя перезаписана умолчанием"
    assert "обновлён с версии 0" in caplog.text, (
        "в логе нет следа обновления настроек — человек не поймёт, почему настройки изменились"
    )


def test_migration_result_reaches_the_file(tmp_path, monkeypatch):
    """Результат миграции доезжает до файла, а не только до памяти.

    Подменяем реестр миграций на «перевод значения известного поля»: это ровно
    тот случай, ради которого версия схемы и нужна, — санитайзер такое поле
    считает корректным и сам по себе ничего не исправит.
    """
    import config_utils

    monkeypatch.setitem(config_utils._CONFIG_MIGRATIONS, 0,
                        lambda cfg: cfg.update({"listen_port": 5353}) or "порт переведён на 5353")
    path = write_json(tmp_path / "config.json", {"listen_port": 53})

    cfg = load_config_file(str(path))

    assert cfg["listen_port"] == 5353, "миграция не доехала до памяти"
    assert read_json(path)["listen_port"] == 5353, "миграция не доехала до файла"


def test_legacy_field_is_removed_from_config(tmp_path):
    """Устаревший выключатель DPI вычищается из файла и из памяти.

    С `use_winws: false` в старом файле DPI после обновления молча не поднимался:
    в интерфейсе «включено», а трафик шёл без обхода. Ключ удаляется, чтобы
    поведение определялось только нынешним кодом.

    Режим DPI в этом же старом файле задан прежним именем значения
    (конфиг версии ≤ 2): миграция должна переименовать значение, сохранив выбор.
    """
    import config_utils

    path = write_json(tmp_path / "config.json",
                      {"use_winws": False, "dpi_mode": config_utils._LEGACY_DPI_ONLY_NAME,
                       "listen_port": 53})
    cfg = load_config_file(str(path))

    assert "use_winws" not in cfg, "устаревшее поле осталось в конфиге"
    assert "use_winws" not in read_json(path), "устаревшее поле осталось в файле"
    _, report = apply_config_migrations({"use_winws": False})
    assert report["notes"] and "use_winws" in report["notes"][0], (
        "миграция устаревшего поля не оставила описания в отчёте"
    )
    assert cfg["dpi_mode"] == "dpi_only", "миграция потеряла выбор режима"


def test_map_home_fields_are_removed_from_config(tmp_path):
    """Настройки удалённой Кибер-карты вычищаются из памяти и из файла.

    `map_home_lat` / `map_home_lon` — «домашняя» точка карты трафика. Карта
    удалена из программы целиком, ключи остались в файлах прошлой версии:
    без миграции человек открывает config.json, видит их и думает, что
    настройка живёт, хотя код её уже не понимает.
    """
    assert "map_home_lat" not in DEFAULT_CONFIG and "map_home_lon" not in DEFAULT_CONFIG

    path = write_json(tmp_path / "config.json",
                      {"config_version": 1, "map_home_lat": 55.75, "map_home_lon": 37.62,
                       "listen_port": 5300})
    cfg = load_config_file(str(path))

    assert "map_home_lat" not in cfg and "map_home_lon" not in cfg, "устаревшие поля остались в конфиге"
    on_disk = read_json(path)
    assert "map_home_lat" not in on_disk and "map_home_lon" not in on_disk, "устаревшие поля остались в файле"
    assert cfg["listen_port"] == 5300, "миграция задела нужную настройку"

    _, report = apply_config_migrations({"config_version": 1, "map_home_lat": 55.75, "map_home_lon": 37.62})
    assert report["notes"] and "map_home_lat" in report["notes"][0], (
        "миграция устаревших полей не оставила описания в отчёте"
    )


def test_legacy_fields_registry_explains_itself():
    """Реестр устаревших полей не пустой и каждое поле объяснено.

    Реестр — это инструкция для миграции. Поле без объяснения нельзя проверить
    и легко забыть, зачем оно там.
    """
    assert LEGACY_CONFIG_FIELDS, "реестр устаревших полей пуст"
    assert "use_winws" in LEGACY_CONFIG_FIELDS, "пропал известный устаревший выключатель DPI"
    for field, why in LEGACY_CONFIG_FIELDS.items():
        assert isinstance(why, str) and len(why) > 15, f"{field}: нет внятного объяснения"


def test_migration_keeps_sanitizer_behaviour(tmp_path):
    """После миграции конфиг всё равно проходит санитайзер (мусор не проскочил)."""
    path = write_json(tmp_path / "config.json",
                      {"use_winws": True, "listen_host": "8.8.8.8", "listen_port": "не число"})
    cfg = load_config_file(str(path))

    assert cfg["listen_host"] == "8.8.8.8", "корректный IPv4 должен приниматься"
    assert cfg["listen_port"] == DEFAULT_CONFIG["listen_port"], "мусор в порту не заменён умолчанием"
    assert "use_winws" not in cfg


def test_second_read_of_migrated_file_is_quiet(tmp_path):
    """Повторное чтение уже мигрированного файла ничего не переписывает."""
    path = write_json(tmp_path / "config.json", {"use_winws": False})
    load_config_file(str(path))
    stamp = path.stat().st_mtime_ns

    load_config_file(str(path))
    assert path.stat().st_mtime_ns == stamp, "мигрированный файл переписывается при каждом чтении"


def test_config_from_future_version_is_not_overwritten(tmp_path):
    """Файл более новой версии программы: настройки читаются, файл не перезаписывается."""
    future = {"config_version": 99, "listen_port": 5353, "поле_из_будущего": "важное"}
    path = write_json(tmp_path / "config.json", future)

    cfg = load_config_file(str(path))

    assert cfg["listen_port"] == 5353, "понятные настройки потеряны"
    assert cfg[CONFIG_VERSION_KEY] == 99, "номер версии понижен в памяти"
    assert read_json(path) == future, "файл из будущего перезаписан — чужие поля потеряны"


def test_apply_config_migrations_reports_what_it_did():
    """Миграция возвращает человеческое описание — оно уходит в лог."""
    cfg, report = apply_config_migrations({"use_winws": False, "listen_port": 53})

    assert report["from_version"] == 0 and report["to_version"] == CONFIG_VERSION
    assert report["notes"], "нет описания применённой миграции"
    assert "use_winws" in report["notes"][0], f"в описании нет имени поля: {report['notes']}"
    assert "use_winws" not in cfg


def test_broken_json_still_quarantined(tmp_path):
    """Битый JSON по-прежнему откладывается в `.broken-*`, а не разбирается как версия."""
    path = tmp_path / "config.json"
    path.write_text("{это не json", encoding="utf-8")

    cfg = load_config_file(str(path))
    assert cfg[CONFIG_VERSION_KEY] == CONFIG_VERSION
    assert sorted(p.name for p in tmp_path.iterdir() if ".broken-" in p.name), "нет копии битого файла"


def test_backup_from_old_version_is_migrated(tmp_path, monkeypatch, caplog):
    """Восстановление бэкапа прошлой версии тоже проходит миграцию."""
    sys.path.insert(0, str(ROOT / "core"))
    import backup_utils
    import config_utils

    monkeypatch.setitem(config_utils._CONFIG_MIGRATIONS, 0,
                        lambda cfg: cfg.update({"stale_cache_ttl": 1800}) or "срок кэша переведён")
    path = write_json(tmp_path / "config-старый.json",
                      {"use_winws": False, "listen_port": 5300, "stale_cache_ttl": 3600})

    with caplog.at_level("INFO"):
        cfg = backup_utils.load_config_backup(str(path))

    assert "use_winws" not in cfg, "из бэкапа вернулся устаревший выключатель DPI"
    assert cfg["listen_port"] == 5300
    assert cfg[CONFIG_VERSION_KEY] == CONFIG_VERSION
    assert cfg["stale_cache_ttl"] == 1800, "миграция бэкапа не применилась"
    assert "срок кэша переведён" in caplog.text, "о миграции бэкапа нигде не сказано"


# ── M2: версия состояния интерфейса ─────────────────────────────────────────

@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "umbranet_ui.json"
    monkeypatch.setenv("UMBRANET_UI_STATE", str(path))
    ui_state.set_state_path(None)
    yield path
    ui_state.set_state_path(None)


def test_state_file_gets_version_on_first_write(state_file):
    ui_state.save_state({"theme": "neon"})
    data = read_json(state_file)

    assert data[ui_state.STATE_VERSION_KEY] == ui_state.STATE_VERSION
    assert data["theme"] == "neon", "настройка пользователя не сохранилась"


def test_old_state_without_version_is_upgraded(state_file):
    """Старое состояние интерфейса без номера версии поднимается при чтении."""
    write_json(state_file, {"theme": "neon", "nav_order": ["home", "dpi"]})
    data = ui_state.load_state()

    assert data[ui_state.STATE_VERSION_KEY] == ui_state.STATE_VERSION
    assert data["theme"] == "neon" and data["nav_order"] == ["home", "dpi"]
    assert read_json(state_file)[ui_state.STATE_VERSION_KEY] == ui_state.STATE_VERSION, (
        "обновлённое состояние не закреплено в файле"
    )


def test_state_update_keeps_version(state_file):
    """Обычная запись состояния не теряет номер версии."""
    ui_state.save_state({"theme": "neon"})
    ui_state.update_state(auto_transport=True)

    data = read_json(state_file)
    assert data[ui_state.STATE_VERSION_KEY] == ui_state.STATE_VERSION
    assert data["auto_transport"] is True and data["theme"] == "neon"


def test_state_from_future_version_is_not_overwritten(state_file):
    """Состояние от более новой версии программы: читаем, но файл не переписываем."""
    future = {ui_state.STATE_VERSION_KEY: 99, "theme": "neon", "поле_будущего": True}
    write_json(state_file, future)

    data = ui_state.load_state()

    assert data["theme"] == "neon"
    assert data[ui_state.STATE_VERSION_KEY] == 99, "номер версии понижен"
    assert read_json(state_file) == future, "состояние из будущего перезаписано"


def test_empty_and_broken_state_still_quarantined(state_file):
    """Версионирование не сломало защиту от обрыва записи (общий регресс P1-2)."""
    state_file.write_text("", encoding="utf-8")
    assert ui_state.load_state() == {}
    assert not state_file.exists()

    write_json(state_file, {"theme": "neon"})
    assert ui_state.load_state()["theme"] == "neon"


def test_state_version_helper_reads_garbage_as_zero(state_file):
    assert ui_state.state_version_of({}) == 0
    assert ui_state.state_version_of({ui_state.STATE_VERSION_KEY: "3"}) == 3
    assert ui_state.state_version_of({ui_state.STATE_VERSION_KEY: "мусор"}) == 0


def test_project_config_is_not_the_working_file():
    """Сторож: во время прогона тестов рабочий config.json проекта не используется.

    Иначе первый же тест, который поднимает узел DNS, пересохранит рабочий файл
    (нормализация значений, миграции схемы) — и в рабочей копии появятся правки,
    которых пользователь не делал.
    """
    import dns_server

    assert dns_server.config_file() != dns_server.CONFIG_FILE, (
        "тесты пишут в рабочий config.json проекта"
    )
    assert pathlib.Path(dns_server.config_file()) == pathlib.Path(
        os.environ["UMBRANET_CONFIG"]
    ), "путь к конфигу не берётся из переменной среды"


def test_config_env_is_read_on_each_call(tmp_path, monkeypatch):
    """Путь к конфигу читается при каждом обращении, а не запоминается при импорте."""
    import dns_server

    first = tmp_path / "первый.json"
    second = tmp_path / "второй.json"
    monkeypatch.setenv("UMBRANET_CONFIG", str(first))
    assert dns_server.config_file() == str(first)
    monkeypatch.setenv("UMBRANET_CONFIG", str(second))
    assert dns_server.config_file() == str(second), "путь закэширован при импорте модуля"


def test_dns_controller_uses_isolated_config():
    """Узел DNS читает и пишет конфиг по подменённому пути (не в проект)."""
    import dns_server

    cfg = dns_server.load_config()
    assert pathlib.Path(dns_server.config_file()).exists(), "конфиг не создан по подменённому пути"
    assert cfg[CONFIG_VERSION_KEY] == CONFIG_VERSION



