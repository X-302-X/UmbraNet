# Технические заметки UmbraNet

Карта репозитория, устройство подсистем и рабочие соглашения — для тех, кто
развивает программу. Процесс выпуска версий — отдельно, в [RELEASING.md](./RELEASING.md).

## Карта репозитория

| Путь | Что внутри |
|---|---|
| `start.pyw` | точка запуска без консоли (pythonw); ловит ошибки в MessageBox |
| `start.bat`, `install.bat`, `cleanup_umbranet.bat` | запуск с UAC, установка `.venv`, аварийная очистка |
| `umbranet/` | UI на PySide6: главное окно, вкладки (`views/`), виджеты (`widgets/`), тема, трей |
| `umbranet/engine_adapter.py` | фасад «UI → ядро»: единственная точка, через которую UI трогает движок |
| `core/` | ядро без Qt: DNS, DPI, диагностика, восстановление сети |
| `bin/` | winws.exe + WinDivert + cygwin1.dll + шаблоны полезной нагрузки ([NOTICE.txt](../bin/NOTICE.txt)) |
| `strategies/` | Uz-стратегии: JSON с аргументами winws.exe |
| `themes/` | темы оформления |
| `grik/` | standalone-виджет графиков пинга (`python -m grik`) |
| `tests/` | pytest; интерфейсные тесты поднимают Qt в offscreen-режиме |
| `docs/` | документация (этот файл, RELEASING.md) |

## Слои и импорты

```
start.pyw → umbranet/main.py → umbranet/app.py (окно, вкладки)
                                   │
                                   ▼ только через фасад
                       umbranet/engine_adapter.py
                                   │
              ┌────────────────────┴───────────────────┐
              ▼ плоские импорты (sys.path)              ▼
        core/dns/*  core/dpi/*  core/*.py         _StubEngine
```

Три важных следствия:

1. **UI не импортирует ядро напрямую** — только `engine_adapter`. Если ядра нет
   (например, отладка UI не на Windows), поднимается заглушка `_StubEngine` с тем
   же контрактом, и интерфейс остаётся работоспособным.
2. **Ядро живёт на «плоских» импортах** (`import config_utils`, `from winws_engine
   import get_winws_engine`): папки `core/`, `core/dns/`, `core/dpi/` добавляются в
   `sys.path`. Так ядро переносимо копированием. `pytest.ini` повторяет тот же
   набор путей (`pythonpath = . core core/dns core/dpi`).
3. **Двойные имена модулей — норма**: один и тот же файл импортируется и как
   `dpi.winws_engine`, и как `winws_engine`. Это осознанно; не «чините» это,
   не проверив оба пути.

Импорт `umbranet.engine_adapter` не тянет Qt — только logging/os/sys и
`core.diagnostics`. Qt появляется лишь в `umbranet/` (views/widgets).

## Ядро: DNS (`core/dns/`)

Локальный DNS-сервер на `127.0.0.1:53` / `::1:53` (UDP + TCP):

- `dns_server.py` — оркестратор: выборочная маршрутизация (routed-домены через
  xbox-dns/DoH, остальное — как у провайдера), bogus-детект, подписки;
- `dns_transports.py`, `dnscrypt.py`, `quic_probe.py` — транспорты **DoH / DoQ /
  DNSCrypt** (sdns:// штампы для пользовательских профилей);
- `dns_cache.py` — кэш с «оптимистичным» режимом (stale-ответы при обрывах связи);
- `upstream_strategy.py` — как опрашивать несколько upstream (fastest и др.);
- `provider_health.py`, `fallback_state.py` — здоровье провайдеров и резервный DNS;
- `bogus_ips.py` + `core/bogus_updater.py` — детект «заглушек» провайдеров:
  builtin-список + автообновляемый `bogus_ips_remote.json` (raw-файл этого же
  репозитория) + диск-кэш `bogus_ips_cache.json`;
- `dns_leak.py` — проверка утечек DNS (site-local заглушки Microsoft `fec0::ffff:*`
  утечками не считаются);
- `query_log.py` — журнал запросов для вкладки «Логи».

## Ядро: DPI (`core/dpi/`)

- `winws_engine.py` — **production-путь**: запуск `bin/winws.exe` с аргументами
  стратегии. Потокобезопасен (RLock на все переходы состояния, см. шапку файла);
  лог winws пишется в `winws.log`; есть safety-net против «сирот» winws.exe.
- `dpi_engine.py` (корень `core/dpi/`) и `legacy/dpi_engine.py` — **DEPRECATED**
  реализации на pydivert, сохранены для истории/отладки. В новом коде не
  использовать.
- `strategy_manager.py` — модель стратегий: `routed_domains`/подписки/процессы =
  *цели* обработки; JSON Uz1/Uz2/… = только *метод* (args winws.exe).
  Список пишется в `strategies/active_routed_hostlist.txt` (читает winws.exe).
- `domain_updater.py` — обновление легаси remote-кэшей доменов (по явному
  `remote_url`; новые цели сами не добавляет).
- `ai_strategy/` — «AI-лаборатория» (см. ниже).

## AI-лаборатория (`core/dpi/ai_strategy/`)

Генерация и проверка стратегий на живых сервисах (YouTube, Discord):

| Модуль | Роль |
|---|---|
| `seeds.py` | базовые шаблоны аргументов |
| `masks.py`, `mutations.py`, `candidates.py` | маски сервисов, мутации, сборка вариантов |
| `targets.py` + `data/generation_targets.json` | «список истины» — домены для проверки |
| `probes.py` | сетевые пробы (resolve/HTTPS/WebSocket) |
| `scoring.py` | оценка вариантов (Discord весит больше; `ok` обязателен) |
| `session.py` | политика сессии (лимиты времени/вариантов, порог score) и `plan_generation_session` |
| `autotuner.py` | dry-run план будущей генерации |

Исполнение — `engine_adapter.dpi_strategy_ai_run_controlled`: preflight сети,
временный запуск winws.exe на каждом варианте, пробы, score, сохранение лучшего
Uz при score ≥ порога. UI-цепочка (`umbranet/app.py`) перед запуском останавливает
основной движок и убирает runtime-мусор (`dpi_strategy_ai_cleanup_runtime`);
`ControlledGenerationSession.run()` — тонкая обёртка над этим же runner.

## Сеть: восстановление и watchdog

Менять системный DNS — опасно, поэтому возврат сделан многослойным
(подробности в README, раздел «Что происходит при Стоп, выходе и краше»):

- `core/network_repair.py` — снапшот DNS-настроек в `backups/network/` и мягкий
  ремонт; условие отката требует признака `_dns_was_set_by_app` (правило **P0-1**:
  не трогать DNS, который мы не меняли);
- `core/watchdog.py` — отдельный процесс: живёт по каналу (pipe) с родителем,
  при EOF (родитель умер — включая «Снять задачу») возвращает DNS (правило **P0-3**:
  не опрос PID, который переиспользуется);
- `core/backup_utils.py` — резервные копии `config.json`.

## Состояние, конфигурация, сервисы

- `config.json` — основные настройки; `umbranet_ui.json` — состояние интерфейса
  (тема, порядок вкладок, «Авто»-транспорт). Оба файла версионируются
  (`core/schema_version.py`): `config_version` / `state_version`, миграции
  применяются по одной на шаг, «файл из будущего» не перезаписывается.
- `core/config_utils.py`, `core/profile_utils.py`, `core/service_profiles.py`,
  `core/routed_presets.py` — санитайзинг конфига, DNS-профили, каталог сервисов,
  пресеты режимов.
- `core/autostart.py` — автозапуск через Планировщик задач (XML-задача);
  `core/single_instance.py` — защита от второго запуска (PID-файл не удаляется —
  правило **P2-2**);
- `core/watchdog.py`, `core/blocking_detector.py`, `core/blocking_services.py`,
  `core/diagnostics.py` — диагностика типа блокировки и «понятные» ошибки
  (`log_recoverable` — стандартная точка логируемых исключений: лог + продолжение).
- `core/update_checker.py` — проверка релизов GitHub (только уведомление, ничего
  не скачивает); URL репозитория — константы `RELEASES_API`/`RELEASES_PAGE`.

## UI (`umbranet/`)

- `app.py` — главное окно: боковая панель (порядок вкладок перетаскиванием),
  фоновые потоки (автодоктор, AI-генерация, обновления).
- `views/` — вкладки: `routing` (главная), `network` (сеть и диагностика),
  `strategy_lab` (AI), `profiles` (DNS-профили), `log`, `settings`, `about`.
- `widgets/` — кастомные виджеты (canvas-списки, toggle, слайдеры, glow-обёртки,
  `live_resize.py` — борьба с «слайд-шоу» при ресайзе, `tray.py` — трей).
- `theme.py` + `themes/*.json` — темы оформления.
- `process_icons.py` — иконки процессов (с заглушкой при отсутствии иконки).

## Тесты и CI

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                  # UI-тесты требуют offscreen: QT_QPA_PLATFORM=offscreen
ruff check --select E9,F63,F7,F82,F811 .
```

- CI (`.github/workflows/ci.yml`): Windows + Linux × Python 3.10–3.12–3.13–3.14;
  на Windows дополнительно прогоняется `install.bat` (повторный запуск по уже
  созданному venv — регресс-кейс).
- Линтер намеренно узкий: полный набор ruff в проекте не проходит (см. «Долги»).
- UI-тесты живут в offscreen-режиме; без системных библиотек Qt (Linux без
  `libxkbcommon` и т.п.) они аккуратно скипаются через `pytest.importorskip`.
- Регресс-тесты важнейших правок помечены номерами задач в шапке файла
  (`P0-1`, `P1-2`, `P2-1`, …) — это следы прошлых аудитов, не текущий backlog.

## Версии

Публичная метка версии — в `umbranet/__init__.py` (`__version__`), сравнение —
через `core/app_version.py` (`26.0.1a < 26.0.1b < 26.0.1r`; `r` — не PEP 440
`.post`). Никогда не перезатирайте опубликованный тег. Правила выпуска —
в [RELEASING.md](./RELEASING.md).

## Известные долги (осознанные)

- **Глухие `except: Exception` и `try/except/pass`** — разбросаны по коду как
  точка надёжности UI (не ронять программу из-за мелочи). Полный ruff-набор из-за
  этого не включён; разбор — отдельная задача.
- **`core/dpi/legacy/`** и **`core/dpi/dpi_engine.py`** — мёртвый код pydivert-эры,
  сохранён для истории. Не использовать и не «улучшать».
- **`grik/`** вынесен из главного меню из-за производительности при live-resize;
  возврат в UI описан в [grik/README.md](../grik/README.md).
- **`strategies/`**: remote_hostlists больше не используются, но легаси-код
  `domain_updater.py` остался (только явные `remote_url`, автоматически цели
  не расширяет).

## Стиль и соглашения

- Комментарии и docstring — по-русски, с объяснением *почему* (а не только *что*).
- Ошибки, которые не должны ронять программу: `log_recoverable(log, msg, exc)`.
- Файлы состояния пишутся атомарно (tmp + replace), см. `core/config_utils.py`.
- Правки «на будущее» принято помечать номером задачи в комментарии
  (формат `P<аудит>-<номер>`), чтобы регресс находился тестом.
