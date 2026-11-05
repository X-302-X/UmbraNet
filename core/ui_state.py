"""
Единое состояние UI — файл `umbranet_ui.json`.

Зачем модуль (P1-2). Файл писали ДВА независимых модуля:
  • `umbranet/engine_adapter.py` — auto_transport, nav_order, favorite_services;
  • `umbranet/theme.py` — выбранная тема.
Оба делали «прочитал → поменял свой ключ → записал целиком» без блокировки и
без атомарности. Отсюда два наблюдаемых эффекта:

  1. Гонка. Пока один модуль менял свой ключ, второй писал свой и затирал
     чужое изменение (классический lost update). На практике: выбрал тему —
     пропала «Авто»-транспорт и порядок вкладок, и наоборот.
  2. Краш на записи. `open(path, "w")` сначала ОБНУЛЯЕТ файл, и только потом
     в него пишутся данные. Падение/закрытие процесса между этими двумя
     действиями оставляло пустой (или обрезанный) JSON. Читатель ловил
     исключение и молча возвращал `{}` — все настройки UI выглядели
     сброшенными, а причина нигде не писалась.

Как исправлено:
  • один писатель — этот модуль, все остальные зовут `load_state()` /
    `update_state()`;
  • `threading.RLock` на цикл «прочитал → изменил → записал»: два модуля в
    одном процессе больше не теряют изменения друг друга;
  • атомарная запись: данные пишутся в временный файл рядом и подменяются
    через `os.replace()` — читатель видит либо старую, либо новую версию,
    но никогда обрезанную;
  • битый или пустой файл не «съедается» молча: он откладывается в
    во внешнее recovery-хранилище в разделе `config/`, а причина пишется в лог.

Про межпроцессный случай: приложение однопроцессное (`single_instance`), поэтому
блокировка внутрипроцессная. Атомарная замена всё равно гарантирует ЦЕЛОСТНОСТЬ
файла при записи из двух процессов одновременно — теряется только порядок
(«кто последний, того и правда»), но не содержимое.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
import time

import schema_version
from rollback_paths import rollback_file

log = logging.getLogger("UmbraNet.ui_state")

# ── Версия схемы состояния UI (M2) ──────────────────────────────────────────
#
# Тот же механизм, что у config.json (M1): файл без `state_version` считается
# версией 0, и при чтении он поднимается до текущей. Пока ни одной миграции не
# потребовалось — переименований в состоянии интерфейса не было, — но теперь
# любая будущая правка схемы описывается одной функцией, а не ручной правкой
# файла у каждого пользователя.
STATE_VERSION = 1
STATE_VERSION_KEY = "state_version"


def _state_migrations() -> dict:
    return {}


def state_version_of(raw) -> int:
    """Номер версии состояния (для диагностики и тестов)."""
    return schema_version.read_version(raw, STATE_VERSION_KEY)

# Блокировка на всё «прочитал → изменил → записал» в рамках процесса.
_LOCK = threading.RLock()

# Переопределение пути (нужно тестам; в бою путь считается от корня проекта).
_path_override: str | None = None

_ENV_PATH = "UMBRANET_UI_STATE"


def state_path() -> str:
    """Абсолютный путь к файлу состояния UI.

    Раньше путь вычислялся в двух местах независимо; теперь он один и тот же
    для адаптера, темы и тестов.
    """
    if _path_override:
        return _path_override
    env = os.environ.get(_ENV_PATH)
    if env:
        return os.path.abspath(env)
    # core/ui_state.py → core/ → корень UmbraNet
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "umbranet_ui.json")


def set_state_path(path: str | None) -> str:
    """Подменить файл состояния (None — вернуть обычное поведение).

    Штатный сценарий — тесты: пишем в tmp, а не в рабочий файл проекта.
    """
    global _path_override
    _path_override = os.path.abspath(path) if path else None
    return state_path()


def _quarantine(path: str) -> str | None:
    """Переносит битое состояние во внешний раздел recovery-конфига."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{os.path.basename(path)}.broken-{stamp}"
    try:
        destination = rollback_file("config", filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(path, destination)
        return str(destination)
    except Exception as exc:
        log.error("Состояние UI повреждено, не удалось сохранить копию: %s", exc)
        return None


def load_state() -> dict:
    """Читает состояние. Никогда не бросает: на любой ошибке — пустой словарь.

    Битый файл не удаляем молча, а откладываем в `.broken-*`: важно понимать,
    что настройки действительно терялись, а не «сбросились сами».
    """
    path = state_path()
    with _LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("Состояние UI недоступно (%s): %s", path, exc)
            return {}

        if not raw.strip():
            # Пустой файл — это ровно тот след, что остаётся от записи «в лоб»
            # при краше: сообщаем и откладываем, а не делаем вид, что так и было.
            broken = _quarantine(path)
            log.error("Состояние UI пустое (возможно, обрыв записи); отложено: %s", broken)
            return {}

        try:
            data = json.loads(raw)
        except ValueError as exc:
            broken = _quarantine(path)
            log.error("Состояние UI повреждено (%s); отложено: %s", exc, broken)
            return {}

        if not isinstance(data, dict):
            broken = _quarantine(path)
            log.error("Состояние UI не объект (тип %s); отложено: %s", type(data).__name__, broken)
            return {}

        # Версия схемы: старый файл поднимаем до текущей, чужой (более новой
        # версии программы) не трогаем — см. core/schema_version.py.
        data, report = schema_version.migrate(
            data,
            version_key=STATE_VERSION_KEY,
            target_version=STATE_VERSION,
            migrations=_state_migrations(),
            logger=log,
        )
        if report["changed"] and not report["newer"]:
            # Одноразово закрепляем результат миграции в файле. Через
            # save_state, а не напрямую: запись атомарная, под тем же локом.
            save_state(data)
        return data


def save_state(state: dict) -> bool:
    """Атомарно записывает состояние. True — запись состоялась."""
    if not isinstance(state, dict):
        log.error("Состояние UI должно быть словарём, получено %s", type(state).__name__)
        return False
    path = state_path()
    directory = os.path.dirname(path) or "."
    # Файл, записанный программой, всегда несёт номер версии схемы. Если в
    # словаре версия из более новой версии программы — не понижаем её.
    state = dict(state)
    if schema_version.read_version(state, STATE_VERSION_KEY) <= STATE_VERSION:
        state[STATE_VERSION_KEY] = STATE_VERSION
    with _LOCK:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            log.error("Не удалось создать папку состояния UI %s: %s", directory, exc)
            return False

        # Временный файл в ТОЙ ЖЕ папке — иначе os.replace не будет атомарным.
        temp_path = os.path.join(
            directory,
            f".{os.path.basename(path)}.{os.getpid()}.{time.time_ns()}.tmp",
        )
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
            return True
        except Exception as exc:
            log.error("Не удалось сохранить состояние UI: %s", exc)
            return False
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass


def update_state(**fields) -> bool:
    """Меняет только указанные ключи, остальные сохраняет.

    Это и есть замена «прочитал → поменял → записал целиком»: чтение и запись
    держатся под одним локом, поэтому параллельный вызов из другого модуля не
    потеряет своё изменение.
    """
    path = state_path()
    with _LOCK:
        existed = os.path.exists(path)
        state = load_state()
        state.update(fields)
        ok = save_state(state)
        if ok:
            log.debug(
                "Состояние UI обновлено (%s): ключи %s",
                "перезапись" if existed else "создание",
                ", ".join(sorted(fields)),
            )
        return ok


def mutate_state(updater) -> bool:
    """То же, но изменение задаётся функцией `updater(state) -> dict | None`."""
    with _LOCK:
        state = load_state()
        result = updater(state)
        if result is not None:
            state = result
        return save_state(state)


def get_value(key: str, default=None):
    """Значение одного ключа (для читателей вроде выбора темы)."""
    return load_state().get(key, default)


def snapshot() -> dict:
    """Глубокая копия состояния — на случай отладки/диагностики."""
    return copy.deepcopy(load_state())
