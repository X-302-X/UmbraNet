"""
UmbraNet — контроллер списка транспортов DNS (логика выбора).

Телеграмизация: рисование строк — TransportCanvas (один paintEvent),
здесь только бизнес-логика: доступность транспортов, режим «Авто» с
фоновым замером самого быстрого, диалог выбора dnscrypt-резолвера.
Внешний API прежний: transportChanged(str), refresh(), stop_workers().

UmbraNet_Official / X-302-X, GPLv3.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from umbranet import engine_adapter as ea
from umbranet.widgets.transport_canvas import TransportCanvas

_AUTO = "auto"

_SHORT = {
    _AUTO: "Автоматически выбирает самый быстрый маршрут",
    "udp": "Быстро, но провайдер видит и может подменять запросы",
    "doh": "Внутри HTTPS (443). Лучшая защита от подмены",
    "dot": "Внутри TLS (853). Шифрует, но порт заметнее",
    "doq": "Поверх QUIC (853). Быстрее DoT. Нужен aioquic",
    "dnscrypt": "Свой sdns:// сервер. Нужен pynacl и штамп в профиле",
}

_LABELS = {_AUTO: "Авто", **ea.TRANSPORT_LABELS}
_ORDER = [_AUTO] + list(ea.TRANSPORTS)

_UNAVAIL_HINT = {
    "doq":      "нужен aioquic",
    "dnscrypt": "нужен pynacl + sdns://",
}

# видимая высота списка — как в старой версии (4 строки + зазоры)
_VIEW_H = 4 * 55 + 3 * 6


class _AutoPickWorker(QThread):
    """Фоновый замер транспортов -> самый быстрый (для режима «Авто»)."""
    picked = Signal(object)

    def run(self):
        self.picked.emit(ea.pick_fastest_transport())


class TransportList(QWidget):
    transportChanged = Signal(str)

    def __init__(self):
        super().__init__()

        self.setStyleSheet("background:transparent;border:none;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._canvas = TransportCanvas()
        self._canvas.setFixedHeight(_VIEW_H)
        self._canvas.rowClicked.connect(self._select)
        lay.addWidget(self._canvas)

        self._auto = ea.auto_transport_enabled()
        self._active = ea.get_transport()
        self._auto_pick_in_progress = False
        self._auto_emit_after_pick = False

        # Таймер авто-пика
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(30000)
        self._auto_timer.timeout.connect(self._run_auto_pick)

        self._initializing = True
        self._restyle()
        if self._auto:
            self._auto_timer.start()
            QTimer.singleShot(300, self._run_auto_pick)

        QTimer.singleShot(0, self._finish_init)

    def _finish_init(self):
        self._initializing = False

    # ── данные для отрисовки ─────────────────────────────────────────────

    def _rows_data(self) -> list[dict]:
        rows = []
        for key in _ORDER:
            if key == _AUTO:
                is_active = self._auto
                avail = True
                badge = ""
                if self._auto:
                    if self._auto_pick_in_progress:
                        badge = "замер..."
                    else:
                        badge = f"сейчас: {ea.TRANSPORT_LABELS.get(self._active, self._active)}"
            else:
                is_active = (not self._auto) and key == self._active
                if key == "dnscrypt":
                    avail = ea.dnscrypt_available()
                else:
                    avail, _ = ea.transport_available(key)
                badge = ""
                if not avail:
                    hint = _UNAVAIL_HINT.get(key, "не установлен")
                    badge = f"⚙ {hint}"
                elif key == "dnscrypt" and not ea.active_has_dnscrypt_stamp():
                    badge = "выбрать sdns://"

            state = "active" if is_active else ("unavail" if not avail else "normal")
            rows.append({
                "key": key,
                "label": _LABELS.get(key, key),
                "desc": _SHORT.get(key, ""),
                "badge": badge,
                "state": state,
            })
        return rows

    def _restyle(self):
        self._canvas.set_rows(self._rows_data())

    # ── выбор ────────────────────────────────────────────────────────────

    def _select(self, key: str):
        if key != _AUTO:
            if key == "dnscrypt":
                if not ea.dnscrypt_available():
                    return
            else:
                avail, _ = ea.transport_available(key)
                if not avail:
                    return

        already_active = key != _AUTO and (not self._auto) and key == self._active
        if key == _AUTO and self._auto:
            return

        if key == _AUTO:
            ea.set_auto_transport(True)
            self._auto = True
            self._auto_timer.start()
            self._auto_emit_after_pick = True
            self._run_auto_pick()
            self._restyle()
            return

        if key == "dnscrypt":
            # DNSCrypt — не только маршрут, но и настраиваемый резолвер. Поэтому
            # окно выбора открываем при каждом клике, даже если маршрут уже активен.
            from umbranet.widgets.dialogs import DnsCryptResolverDialog
            dlg = DnsCryptResolverDialog(self)
            if not dlg.exec() or not dlg.result:
                self._restyle()
                return
            ok = ea.apply_dnscrypt_resolver(dlg.result["name"], dlg.result["stamp"])
            if not ok:
                self._restyle()
                return

            if not already_active:
                ea.set_auto_transport(False)
                self._auto = False
                self._auto_timer.stop()
                ea.set_transport(key)
                self._active = key
            self._restyle()
            self.transportChanged.emit(key)
            return

        if already_active:
            return

        ea.set_auto_transport(False)
        self._auto = False
        self._auto_timer.stop()
        ea.set_transport(key)
        self._active = key
        self._restyle()
        self.transportChanged.emit(key)

    # ── авто-пик ─────────────────────────────────────────────────────────

    def _run_auto_pick(self):
        if not self._auto:
            return
        if getattr(self, "_pick_worker", None) and self._pick_worker.isRunning():
            self._auto_emit_after_pick = True
            return
        self._auto_pick_in_progress = True
        self._restyle()
        self._pick_worker = _AutoPickWorker()
        self._pick_worker.picked.connect(self._on_auto_picked)
        self._pick_worker.start()

    def _on_auto_picked(self, mode):
        if not self._auto:
            self._auto_pick_in_progress = False
            self._restyle()
            return

        changed = False
        if mode and mode != self._active:
            ea.set_transport(mode)
            self._active = mode
            changed = True

        should_emit = self._auto_emit_after_pick or changed
        self._auto_emit_after_pick = False
        self._auto_pick_in_progress = False
        self._restyle()

        if should_emit and not getattr(self, "_initializing", True):
            self.transportChanged.emit(_AUTO)

    # ── публичное ────────────────────────────────────────────────────────

    def refresh(self):
        self._auto = ea.auto_transport_enabled()
        self._active = ea.get_transport()
        self._auto_pick_in_progress = False
        self._auto_emit_after_pick = False
        self._restyle()

    def stop_workers(self):
        try:
            self._auto_timer.stop()
        except Exception:
            pass
        w = getattr(self, "_pick_worker", None)
        if w is not None and w.isRunning():
            try:
                w.wait(2000)
            except Exception:
                pass
