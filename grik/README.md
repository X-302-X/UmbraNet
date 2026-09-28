# grik — график пинга UmbraNet

Standalone-виджет графиков пинга (DNS и DPI), вынесенный из главного меню
(«Маршрутизация»).

## Зачем вынесен

Диагностика лагов показала: тики таймера обновления графиков во время
живого resize окна перерисовывали графики в каждом кадре растягивания.
Чем выше «Частота обновления» в настройках графика — тем сильнее
«слайд-шоу» вместо плавного увеличения окна. График убран из главного
меню целиком, код сохранён здесь.

## Что внутри

| Файл             | Что содержит                                                        |
|------------------|---------------------------------------------------------------------|
| `ping_graph.py`  | Виджет графика (бывший `umbranet/widgets/sparkline.py`)             |
| `ping_worker.py` | Фоновый замер пинга DNS/DPI (бывший `_PingWorker` из routing)       |
| `graph_config.py`| Окно настроек: частота обновления, вид, сетка, высота               |
| `config_store.py`| Хранение настроек — `grik/grik_config.json`                         |
| `panel.py`       | Готовая секция «Пинг сети»: 2 графика + таймер + кнопки             |
| `__main__.py`    | Запуск отдельным окном: `python -m grik`                            |

## Посмотреть / настроить

```
python -m grik
```

Откроется окно с графиками и их настройками (в standalone-режиме замер
пингует публичный DNS 1.1.1.1).

## Вернуть в главное меню (когда захотим)

```python
from grik import PingGraphPanel

panel = PingGraphPanel(context_provider=lambda: (
    profile, dns_mode, routed_domains, measure_dns, measure_dpi,
))
lay.addWidget(panel)          # в правую панель «Маршрутизации»
panel.set_mode_visible("combo")   # dns_only / dpi_only / combo
```

UmbraNet_Official / X-302-X, GPLv3.
