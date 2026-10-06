"""
UmbraNet - раздел «Сеть и диагностика».

Новая упрощённая версия без хаоса:
  • одно понятное состояние UmbraNet Health;
  • одна кнопка «Проверить и вылечить» вместо ручного выбора починки;
  • отдельный компактный блок DPI / WinWS;
  • ручная проверка конкретного домена оставлена как инструмент разработчика;
  • кривую массовую «проверку сервисов» убрали.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme

log = logging.getLogger("UmbraNet.NetworkView")
from umbranet.engine_adapter import (
    add_query_log_event,
    bogus_force_update,
    bogus_last_updated,
    flush_dns_cache,
    full_diagnostics_report,
    get_engine,
    health_score,
    network_repair_soft,
    network_restore_latest,
    network_snapshot_info,
)
from umbranet.widgets.flow_layout import FlowLayout
from umbranet.widgets.rounded_panel import RoundedPanel

#: Прежнее имя раскладки-потока. Раскладка переехала в общий модуль (её используют
#: уже четыре вкладки), но имя оставлено: на него ссылаются внутренний код и тесты.
_FlowLayout = FlowLayout

# ════════════════════════════════════════════════════════════════════════════
# Workers: всё тяжёлое — только в фоне, чтобы вкладка не фризила UI.
# ════════════════════════════════════════════════════════════════════════════

class _ClickableLabel(QLabel):
    """Подпись, на которую можно нажать (нужно предупреждению о PowerShell, H4).

    Кэш проверки доступности живёт 5 минут, но человеку, который только что
    разблокировал powershell.exe, ждать их незачем: нажатие проверяет сразу.
    """

    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.clicked.emit()
        super().mouseReleaseEvent(event)


# ════════════════════════════════════════════════════════════════════════════
# Style helpers
# ════════════════════════════════════════════════════════════════════════════

def _mode_label(mode: str) -> str:
    return {
        "dns_only": "DNS Only",
        "combo": "Combo",
        "dpi_only": "DPI Only",
        "unknown": "неясно",
    }.get(mode, mode)

def _verdict_color(verdict: str, severity: str = "") -> str:
    if verdict == "ok" or severity == "ok":
        return theme.GREEN
    if verdict in ("dns-blocked", "dns-poisoned", "quic-blocked") or severity == "warning":
        return theme.YELLOW if verdict != "quic-blocked" else theme.ORANGE
    if verdict in ("tcp-blocked", "tls-blocked") or severity in ("problem", "error"):
        return theme.RED
    return theme.MUTED

def _button_row(*buttons, spacing: int = 10) -> _FlowLayout:
    """Ряд кнопок, который переносится на вторую строку, когда места не хватает."""
    flow = _FlowLayout(spacing=spacing)
    for b in buttons:
        flow.addWidget(b)
    return flow


def _wrapped(label: QLabel, min_height: int) -> QLabel:
    """Подпись, которая переносится по строкам и РАСТЁТ под свой текст.

    Раньше у таких подписей стояла жёсткая высота («setFixedHeight(42)»), а текста
    в них больше: у авто-диагностики нужно 84 px, у блока DPI — 70. Лишние
    строки обрезались прямо посередине — именно это и выглядело «очень криво».
    Теперь фиксируется только минимум: подпись занимает столько строк, сколько
    нужно, а карточка (и прокрутка вкладки) растёт вслед за ней.
    """
    label.setWordWrap(True)
    label.setMinimumHeight(min_height)
    label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    return label


def _card(title: str = "") -> tuple[QWidget, QVBoxLayout]:
    # RoundedPanel — paintEvent без QSS-градиента, в 3× быстрее при ресайзе
    f = RoundedPanel(theme.CARD, theme.BORDER, radius=14)
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    if title:
        t = QLabel(title)
        # Заголовок тоже переносится: длинное название карточки в узком окне
        # требовало больше места, чем у карточки, и обрезалось бы вместе с краем.
        t.setWordWrap(True)
        t.setStyleSheet(
            f"color:{theme.WHITE};font-size:15px;font-weight:700;"
            "background:transparent;border:none;"
        )
        lay.addWidget(t)
    return f, lay

class _HealthWorker(QThread):
    done = Signal(dict)

    def run(self):
        try:
            self.done.emit(health_score())
        except Exception as exc:  # noqa: BLE001
            self.done.emit({
                "score": 0,
                "state": "error",
                "title": "Health недоступен",
                "checks": [],
                "actions": [],
                "error": str(exc),
            })




class _AutoDoctorWorker(QThread):
    done = Signal(dict)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    @staticmethod
    def _needs_repair(hs: dict) -> tuple[bool, str]:
        checks = hs.get("checks") or []
        need_dns = False
        need_browser = False
        for c in checks:
            title = str(c.get("title") or "")
            status = str(c.get("status") or "")
            if status not in ("warn", "error"):
                continue
            if title in ("Системный DNS", "DNS/DPI утечки"):
                need_dns = True
            if title == "Браузерный DoH":
                need_browser = True
        if need_browser:
            return True, "browser"
        if need_dns:
            return True, "soft"
        return False, "none"

    def run(self):
        result = {
            "ok": False,
            "message": "",
            "before": {},
            "after": {},
            "repair_report": {},
            "actions": [],
        }
        try:
            before = health_score()
            result["before"] = before
            if not bool(getattr(self.engine, "running", False)):
                result["message"] = "UmbraNet остановлен. Нажмите «Старт», потом автолечение сработает корректно."
                result["after"] = before
                return self.done.emit(result)

            need, level = self._needs_repair(before)
            if need:
                report = network_repair_soft(level)
                result["repair_report"] = report
                result["actions"].append(f"Запущена автопочинка уровня: {level}")
                add_query_log_event(
                    "[Автодоктор]",
                    source="fixed" if report.get("ok") else "error",
                    rcode="OK" if report.get("ok") else "WARN",
                    note=(report.get("after") or {}).get("title") or "; ".join(report.get("errors") or []) or "готово",
                )
            else:
                result["actions"].append("Лечение не потребовалось")

            after = health_score()
            result["after"] = after
            result["ok"] = int(after.get("score", 0)) >= 85
            result["message"] = after.get("title", "Проверка завершена")
        except Exception as exc:  # noqa: BLE001
            result["message"] = f"Ошибка автодоктора: {exc}"
        self.done.emit(result)

class _FullReportWorker(QThread):
    done = Signal(str)

    def run(self):
        try:
            self.done.emit(full_diagnostics_report())
        except Exception as exc:  # noqa: BLE001
            self.done.emit(f"UmbraNet Full Diagnostic Report\nОшибка: {exc}")

class _BogusUpdateWorker(QThread):
    done = Signal(bool)

    def run(self):
        try:
            self.done.emit(bool(bogus_force_update()))
        except Exception:
            self.done.emit(False)


class _RestoreNetworkWorker(QThread):
    """Откат сети: PowerShell, поэтому только в фоне (иначе фриз UI на секунды)."""

    done = Signal(bool, str)

    def run(self):
        try:
            ok, msg = network_restore_latest()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)
        self.done.emit(bool(ok), str(msg))



class NetworkView(QWidget):
    # Открыть вкладку «Логи» с выбранной категорией («fixed» = Починки/лечение).
    openLogRequested = Signal(str)

    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self._health_worker = None
        self._doctor_worker = None
        self._report_worker = None
        self._bogus_worker = None
        self._blocking_worker = None
        self._restore_worker = None
        self._last_health = None
        self._last_blocking_result = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("Сеть и диагностика")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:22px;font-weight:700;")
        head.addWidget(title)
        head.addStretch()
        outer.addLayout(head)

        # H4: если PowerShell недоступен, человек должен это видеть — иначе он
        # видит только «часть кнопок не работает». Предупреждение появляется
        # само (проверка кэшируется в win_shell) и исчезает, когда PowerShell
        # разблокируют: перезапускать программу для этого не нужно.
        self._ps_warning = _ClickableLabel("")
        self._ps_warning.setWordWrap(True)
        self._ps_warning.setVisible(False)
        self._ps_warning.setCursor(Qt.PointingHandCursor)
        self._ps_warning.clicked.connect(self._recheck_powershell)
        self._ps_warning.setStyleSheet(
            f"color:{theme.YELLOW};font-size:12px;background:{theme.INPUT_BG};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:10px 12px;"
        )
        outer.addWidget(self._ps_warning)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())

        body = QWidget()
        try:
            body.setAttribute(Qt.WA_StaticContents, True)
        except Exception:
            pass
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        lay.addWidget(self._build_answering())
        lay.addWidget(self._build_health_doctor())
        lay.addWidget(self._build_dpi_tools())
        lay.addWidget(self._build_tools())
        lay.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.refresh()

    # ── H3: индикатор того, кто отвечает на DNS-запросы ──────────────────
    # До этого пользователь не видел, что запрос ушёл через запасной транспорт
    # (например, открытым UDP вместо DoH) или через запасного провайдера —
    # это было видно только в текстовом логе.
    def _build_answering(self):
        card, lay = _card("🛰  Сейчас отвечает")

        desc = QLabel(
            "Кто фактически отвечает на DNS-запросы и не ушёл ли запрос запасным путём "
            "(запасной транспорт, запасной провайдер, системный DNS)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(desc)

        self._answer_state = QLabel("Данных пока нет")
        # Состояние — не ряд с распоркой, а обычная подпись с переносом: длинный
        # текст («🟡 Отвечает запасной провайдер: … · …») в узком окне держал
        # минимальную ширину карточки и уезжал за её край.
        self._answer_state.setWordWrap(True)
        self._answer_state.setStyleSheet(
            f"color:{theme.TEXT};font-size:14px;font-weight:700;"
            "background:transparent;border:none;"
        )
        lay.addWidget(self._answer_state)

        self._answer_details = QLabel("")
        self._answer_details.setWordWrap(True)
        self._answer_details.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;"
        )
        lay.addWidget(self._answer_details)

        self._answer_counters = QLabel("")
        self._answer_counters.setWordWrap(True)
        self._answer_counters.setStyleSheet(
            f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;"
        )
        lay.addWidget(self._answer_counters)
        return card

    # Человеческие названия транспортов: в ядре они короткие (doh, udp...).
    _TRANSPORT_NAMES = {
        "doh": "DoH (HTTPS)",
        "dot": "DoT (TLS)",
        "doq": "DoQ (QUIC)",
        "dnscrypt": "DNSCrypt",
        "udp": "UDP (открытый)",
        "system": "системный DNS",
    }

    def _transport_name(self, code: str) -> str:
        code = (code or "").strip().lower()
        if not code:
            return "неизвестно"
        return self._TRANSPORT_NAMES.get(code, code.upper())

    def _refresh_answering(self):
        """Обновляет карточку «Сейчас отвечает» (H3)."""
        if not hasattr(self, "_answer_state"):
            return
        try:
            from umbranet import engine_adapter as ea
            data = ea.fallback_status() or {}
        except Exception:
            data = {}

        if not data:
            # Ядро — заглушка или ещё не отвечало: говорим как есть, без выдумок.
            self._answer_state.setText("⚪ Данных пока нет — запросов ещё не было")
            self._answer_state.setStyleSheet(
                f"color:{theme.MUTED};font-size:14px;font-weight:700;"
                "background:transparent;border:none;"
            )
            self._answer_details.setText(
                "Как только пойдут DNS-запросы, здесь появится провайдер, транспорт и "
                "доля запасных ответов."
            )
            self._answer_counters.setText("")
            return

        state = str(data.get("state") or "idle")
        provider = str(data.get("provider_name") or "—")
        transport = self._transport_name(str(data.get("transport") or ""))
        preferred = self._transport_name(str(data.get("preferred_transport") or ""))
        yellow = getattr(theme, "YELLOW", theme.ACCENT2)

        if state == "idle":
            self._answer_state.setText("⚪ Запросов ещё не было")
            color = theme.MUTED
        elif state == "system_only":
            self._answer_state.setText("⚪ Профили UmbraNet ещё не отвечали")
            color = theme.MUTED
        elif state == "fallback_provider":
            self._answer_state.setText(f"🟡 Отвечает запасной провайдер: {provider} · {transport}")
            color = yellow
        elif state == "fallback":
            self._answer_state.setText(f"🟡 Транспорт заменён: отвечает {transport}, а не {preferred}")
            color = yellow
        else:
            self._answer_state.setText(f"🟢 Отвечает {provider} · {transport}")
            color = theme.GREEN
        self._answer_state.setStyleSheet(
            f"color:{color};font-size:14px;font-weight:700;background:transparent;border:none;"
        )

        details = []
        if state == "system_only":
            details.append(
                "Маршрутизируемых доменов пока не было — обычные запросы идут "
                "через системный DNS, как и без UmbraNet."
            )
        if data.get("fallback_provider"):
            details.append(
                "Основной провайдер не ответил — запрос обслужил запасной. "
                "Обход продолжает работать, но это признак сбоя у основного."
            )
        if data.get("fallback_transport"):
            details.append(
                f"Выбран был {preferred}, но ответ пришёл через {transport}. "
                "Открытый UDP провайдер видит и может подменить — стоит проверить, "
                "не блокирует ли провайдер DoH."
            )
        if not details:
            details.append("Ответы идут штатным путём: выбранный транспорт и основной провайдер работают.")
        last_domain = str(data.get("last_answer_domain") or "")
        if last_domain:
            details.append(f"Последний маршрутизируемый запрос: {last_domain}")
        last_system = str(data.get("last_system_domain") or "")
        if last_system:
            details.append(f"Последний ответ системного DNS: {last_system}")
        self._answer_details.setText(" ".join(details))

        counters = data.get("counters") or {}
        answers = int(counters.get("answers") or 0)
        system_answers = int(data.get("system_answers") or counters.get("system_answers") or 0)
        total = answers + system_answers
        share = int(round(float(data.get("system_share") or 0.0) * 100))
        if total:
            self._answer_counters.setText(
                f"За сессию: {answers} ответ(ов) от профилей UmbraNet "
                f"(запасной транспорт — {int(counters.get('transport_fallback') or 0)}, "
                f"запасной провайдер — {int(counters.get('provider_fallback') or 0)}); "
                f"через системный DNS — {system_answers} из {total} ({share}%). "
                "Системный DNS — обычный путь для доменов вне списка маршрутизации: "
                "для них обход и не запрашивался."
            )
        else:
            self._answer_counters.setText("Записей пока нет.")

    def _build_health_doctor(self):
        """Компактная карточка лечения (2026-10-06, виджет «полностью переделан»).

        Пользователь просил: «статус одной строкой + ряд кнопок, минимум места,
        текст не съезжает». Контракты узкого окна сохранены: подписи по-прежнему
        переносятся и растут (tests/test_network_narrow.py), а ряд кнопок
        переносится на вторую строку (_FlowLayout). Рядом с «Лог» — кнопка-
        переключатель «Уведомления» с видимой галочкой (вкл/выкл).
        """
        card, lay = _card("🩺  Автодиагностика и лечение")
        row = QHBoxLayout()
        row.setSpacing(12)

        # Пилюля-оценка: компактная, без жёстких 92px/30px (съедали место).
        self._health_score_label = QLabel("—")
        self._health_score_label.setFixedSize(64, 48)
        self._health_score_label.setAlignment(Qt.AlignCenter)
        self._health_score_label.setStyleSheet(
            f"color:{theme.MUTED};font-size:22px;font-weight:900;background:{theme.INPUT_BG};"
            f"border:1px solid {theme.BORDER};border-radius:12px;"
        )
        row.addWidget(self._health_score_label, 0, Qt.AlignTop)

        texts = QVBoxLayout()
        texts.setSpacing(3)
        self._health_title = QLabel("Проверка ещё не запускалась")
        # Health reports can have long titles. Like their details, they must
        # wrap instead of imposing a minimum width on the whole network page.
        self._health_title.setWordWrap(True)
        self._health_title.setStyleSheet(f"color:{theme.TEXT};font-size:14px;font-weight:800;background:transparent;border:none;")
        self._health_text = QLabel("Нажмите одну кнопку — UmbraNet проверит состояние и сам применит безопасную починку, если она нужна.")
        _wrapped(self._health_text, 42)
        self._health_text.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        texts.addWidget(self._health_title)
        texts.addWidget(self._health_text)
        row.addLayout(texts, 1)
        lay.addLayout(row)

        self._btn_doctor = self._grad_btn("🛠 Проверить и вылечить", theme.ACCENT, theme.ACCENT2, self._run_auto_doctor)
        self._btn_open_log = self._flat_btn("📋 Лог", self._open_doctor_log)
        self._btn_notify = self._flat_btn("✓ Уведомления: вкл", self._toggle_notify)
        self._btn_notify.setCheckable(True)
        self._btn_copy_full_report = self._flat_btn("📋 Скопировать отчёт", self._copy_full_report)
        lay.addLayout(_button_row(self._btn_doctor, self._btn_open_log,
                                  self._btn_notify, self._btn_copy_full_report))
        self._sync_notify_btn()
        return card

    # ── Лог лечения и переключатель уведомлений (2026-10-06) ────────────────
    def _open_doctor_log(self):
        """Открывает вкладку «Логи» сразу с категорией «🔧 Починки» (лечение)."""
        self.openLogRequested.emit("fixed")

    def _toggle_notify(self):
        from umbranet.engine_adapter import set_doctor_notify
        set_doctor_notify(bool(self._btn_notify.isChecked()))
        self._sync_notify_btn()

    def _sync_notify_btn(self):
        from umbranet.engine_adapter import get_doctor_notify
        on = bool(get_doctor_notify())
        self._btn_notify.setChecked(on)
        self._btn_notify.setText("✓ Уведомления: вкл" if on else "🔕 Уведомления: выкл")
        border = theme.ACCENT if on else theme.BORDER
        self._btn_notify.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{theme.TEXT};"
            f"border:1px solid {border};border-radius:10px;padding:0 13px;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};}}"
            f"QPushButton:disabled{{color:{theme.MUTED};border-color:{theme.BORDER};}}"
        )

    def _build_dpi_tools(self):
        card, lay = _card("🛡  DPI-движок")
        self._dpi_title = QLabel("Движок: —")
        self._dpi_title.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:700;background:transparent;border:none;")
        self._dpi_text = QLabel("Статус DPI-движка, стратегия и лог запуска.")
        _wrapped(self._dpi_text, 48)
        self._dpi_text.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(self._dpi_title)
        lay.addWidget(self._dpi_text)

        self._btn_open_winws_log = self._flat_btn("Открыть лог движка", self._open_winws_log)
        self._btn_copy_winws_diag = self._flat_btn("📋 Скопировать DPI-диагностику", self._copy_winws_diagnostics)
        lay.addLayout(_button_row(self._btn_open_winws_log, self._btn_copy_winws_diag))
        return card

    def _build_tools(self):
        card, lay = _card("🧰  Быстрые инструменты")
        self._btn_flush = self._flat_btn("Сбросить DNS-кэш", self._flush_dns)
        self._btn_bogus_update = self._flat_btn("🛡 Обновить защиту от подмен", self._update_bogus_list)
        # P0-2: ручной откат сети. Показывает, к какому состоянию вернёт —
        # снапшот от такого-то времени либо «Авто (DHCP)», если снапшота нет.
        self._btn_restore_net = self._flat_btn("↩ Откатить сеть", self._restore_network)
        lay.addLayout(_button_row(self._btn_flush, self._btn_bogus_update,
                                  self._btn_restore_net))
        self._update_restore_hint()

        self._bogus_status = QLabel(self._bogus_status_text())
        _wrapped(self._bogus_status, 28)
        self._bogus_status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(self._bogus_status)
        return card

    # ── small widgets ────────────────────────────────────────────────────────
    def _pill(self, text: str, color: str):
        p = QLabel(text)
        p.setStyleSheet(
            f"color:{color};font-size:11px;font-weight:700;background:{theme.INPUT_BG};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:5px 9px;"
        )
        return p

    def _grad_btn(self, text, c1, c2, slot):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        b.setStyleSheet(
            f"QPushButton{{background:{theme.grad(c1, c2)};color:{theme.WHITE};"
            "border:none;border-radius:10px;font-weight:600;padding:0 13px;}"
            f"QPushButton:disabled{{background:{theme.CARD};color:{theme.MUTED};border:1px solid {theme.BORDER};}}"
        )
        b.clicked.connect(slot)
        return b

    def _flat_btn(self, text, slot):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        b.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 13px;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};}}"
            f"QPushButton:disabled{{color:{theme.MUTED};border-color:{theme.BORDER};}}"
        )
        b.clicked.connect(slot)
        return b

    def _combo_qss(self):
        return (
            f"QComboBox{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 10px;min-width:150px;}}"
            f"QComboBox:hover{{border-color:{theme.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{theme.CARD};color:{theme.TEXT};selection-background-color:{theme.ACCENT};}}"
        )

    # ── Health / Doctor ─────────────────────────────────────────────────────
    def _refresh_health_score(self):
        if self._health_worker and self._health_worker.isRunning():
            return
        self._health_title.setText("⏳ Проверяю состояние...")
        self._health_worker = _HealthWorker()
        self._health_worker.done.connect(self._on_health_ready)
        self._health_worker.start()

    def _on_health_ready(self, hs: dict):
        self._last_health = hs or {}
        if hs.get("error"):
            self._health_score_label.setText("!")
            self._health_title.setText("Health недоступен")
            self._health_text.setText(hs.get("error", "Неизвестная ошибка"))
            return

        score = int(hs.get("score", 0))
        state = hs.get("state", "warn")
        color = theme.GREEN if state == "ok" else (theme.YELLOW if state == "warn" else theme.RED)
        self._health_score_label.setText(str(score))
        self._health_score_label.setStyleSheet(
            f"color:{color};font-size:30px;font-weight:900;background:{theme.INPUT_BG};"
            f"border:1px solid {color};border-radius:14px;padding:10px;"
        )
        self._health_title.setText(hs.get("title", "Health"))
        checks = hs.get("checks") or []
        bad = [c for c in checks if c.get("status") in ("warn", "error")]
        if bad:
            text = "\n".join(f"• {c.get('title')}: {c.get('detail')}" for c in bad[:3])
        else:
            text = "Критических проблем не найдено. UmbraNet выглядит исправно."
        actions = hs.get("actions") or []
        if actions:
            text += "\nРекомендация: " + actions[0]
        self._health_text.setText(text)

    def _run_auto_doctor(self):
        if self._doctor_worker and self._doctor_worker.isRunning():
            return
        self._btn_doctor.setEnabled(False)
        self._btn_doctor.setText("Лечу...")
        self._health_title.setText("⏳ Автодоктор работает...")
        self._health_text.setText("Проверяю состояние, применяю безопасную починку при необходимости и повторно проверяю результат.")
        self._doctor_worker = _AutoDoctorWorker(self.engine)
        self._doctor_worker.done.connect(self._on_doctor_done)
        self._doctor_worker.start()

    def _on_doctor_done(self, res: dict):
        self._btn_doctor.setEnabled(True)
        self._btn_doctor.setText("🛠 Проверить и вылечить")
        after = res.get("after") or {}
        if after:
            self._on_health_ready(after)
        msg = res.get("message") or "Готово"
        actions = res.get("actions") or []
        if actions:
            msg += "\n" + "\n".join(f"• {a}" for a in actions)
        self._health_text.setText(msg)
        self._refresh_dpi_status()

    def _copy_full_report(self):
        if self._report_worker and self._report_worker.isRunning():
            return
        self._btn_copy_full_report.setEnabled(False)
        self._btn_copy_full_report.setText("Готовлю...")
        self._report_worker = _FullReportWorker()
        self._report_worker.done.connect(self._on_full_report_ready)
        self._report_worker.start()

    def _on_full_report_ready(self, text: str):
        QGuiApplication.clipboard().setText(text or "")
        self._btn_copy_full_report.setEnabled(True)
        self._btn_copy_full_report.setText("✓ Скопировано")
        QTimer.singleShot(1500, lambda: self._btn_copy_full_report.setText("📋 Скопировать отчёт"))

    # ── DPI / WinWS diagnostics ─────────────────────────────────────────────
    def _get_winws_status(self) -> dict:
        try:
            winws = getattr(self.engine, "winws", None)
            if winws is None:
                from winws_engine import get_winws_engine  # type: ignore
                winws = get_winws_engine()
            if hasattr(winws, "status"):
                return winws.status()
            return {
                "available": bool(winws and winws.is_available()),
                "running": bool(winws and winws.is_running()),
                "exe_path": str(getattr(winws, "exe_path", "")),
                "log_path": "",
                "last_error": "",
                "last_exit_code": None,
                "last_args": [],
                "last_cmd": [],
            }
        except Exception as exc:
            return {"available": False, "running": False, "last_error": str(exc), "last_args": [], "last_cmd": []}

    def _winws_log_tail(self, max_chars: int = 5000) -> str:
        try:
            path = Path(self._get_winws_status().get("log_path") or "e1-spike.log")
            if not path.exists():
                return ""
            return path.read_text(encoding="utf-8", errors="replace")[-max_chars:].strip()
        except Exception as exc:
            return f"<не удалось прочитать лог движка: {exc}>"

    def _dpi_targets_info(self) -> dict:
        cfg = getattr(self.engine, "config", {}) or {}
        raw = list(cfg.get("routed_domains", []) or [])
        raw += list(cfg.get("subscribed_domains_set", set()) or [])
        out = []
        seen = set()
        for x in raw:
            d = str(x or "").strip().lower().strip(".")
            if not d or "." not in d:
                continue
            if d not in seen:
                seen.add(d)
                out.append(d)
        try:
            from strategy_manager import get_strategy_manager  # type: ignore
            manager = get_strategy_manager()
            path = str(getattr(manager, "active_hostlist_path", ""))
        except Exception:
            path = ""
        return {"count": len(out), "domains": out, "path": path}

    def _refresh_dpi_status(self):
        if not hasattr(self, "_dpi_title"):
            return
        cfg = getattr(self.engine, "config", {}) or {}
        targets = self._dpi_targets_info()
        st = self._get_winws_status()
        if st.get("running"):
            color = theme.GREEN
            title = "Движок: запущен"
        elif st.get("available"):
            color = theme.YELLOW if cfg.get("dpi_mode", "off") != "off" else theme.MUTED
            title = "Движок: остановлен"
        else:
            color = theme.RED
            title = "Движок: не найден"
        self._dpi_title.setText(title)
        self._dpi_title.setStyleSheet(f"color:{color};font-size:15px;font-weight:700;background:transparent;border:none;")
        err = st.get("last_error") or ""
        raw_mode = cfg.get("dpi_mode", "off")
        ui_mode = {"off": "dns_only", "combo": "combo", "dpi_only": "dpi_only"}.get(raw_mode, "unknown")
        target_count = int(targets.get("count", 0) or 0)
        text = (
            f"Режим: {_mode_label(ui_mode)} • "
            f"Стратегия: {cfg.get('dpi_strategy', 'uz1')} • "
            f"Целей DPI: {target_count} • "
            f"Аргументов: {len(st.get('last_args') or [])}"
        )
        if cfg.get("dpi_mode", "off") != "off" and target_count == 0:
            text += "\n⚠ Цели DPI не выбраны. Включите сервисы/домены в главном меню — тогда WinWS будет работать только по ним."
        elif target_count:
            preview = ", ".join((targets.get("domains") or [])[:5])
            if target_count > 5:
                preview += f", +{target_count - 5} ещё"
            text += f"\nHostlist: {targets.get('path') or 'active_routed_hostlist.txt'}"
            text += f"\nЦели: {preview}"
        if err:
            text += f"\nПоследняя ошибка: {err[:260]}"
        else:
            text += f"\nЛог: {st.get('log_path', 'winws.log')}"
        self._dpi_text.setText(text)

    def _open_winws_log(self):
        try:
            import os
            import webbrowser
            status = self._get_winws_status()
            path = Path(status.get("log_path") or "e1-spike.log")
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Лог движка пока пуст: движок ещё не запускался.\n", encoding="utf-8")
            if hasattr(os, "startfile"):
                os.startfile(str(path))
            else:
                webbrowser.open(path.resolve().as_uri())
            self._btn_open_winws_log.setText("✓ Открыто")
        except Exception as exc:
            self._btn_open_winws_log.setText("Ошибка")
            self._dpi_text.setText(f"Не удалось открыть winws.log: {exc}")
        QTimer.singleShot(1500, lambda: self._btn_open_winws_log.setText("Открыть лог движка"))

    def _copy_winws_diagnostics(self):
        cfg = getattr(self.engine, "config", {}) or {}
        status = self._get_winws_status()
        try:
            from strategy_manager import get_strategy_manager  # type: ignore
            manager = get_strategy_manager()
            strategy_id = cfg.get("dpi_strategy", "uz1")
            routed_targets = list(cfg.get("routed_domains", []) or [])
            routed_targets += list(cfg.get("subscribed_domains_set", set()) or [])
            args = manager.get_args(
                strategy_id,
                routed_domains=routed_targets,
                require_hostlist=(cfg.get("dpi_mode", "off") != "off"),
            )
            strategy_error = getattr(manager, "last_error", "")
        except Exception as exc:
            args = []
            strategy_error = str(exc)

        lines = [
            "UmbraNet DPI / WinWS diagnostics",
            "=" * 44,
            f"dpi_mode: {cfg.get('dpi_mode', 'off')}",
            f"dpi_strategy: {cfg.get('dpi_strategy', 'uz1')}",
            "dpi_engine: WinWS (обход всегда через WinWS)",
            f"engine_running: {bool(getattr(self.engine, 'running', False))}",
            "",
            f"winws_available: {status.get('available')}",
            f"winws_running: {status.get('running')}",
            f"winws_exe: {status.get('exe_path', '')}",
            f"winws_log: {status.get('log_path', '')}",
            f"last_exit_code: {status.get('last_exit_code')}",
            f"last_error: {status.get('last_error', '')}",
            "",
            f"strategy_args_count: {len(args)}",
            f"strategy_error: {strategy_error}",
            f"active_hostlist_count: {getattr(manager, 'last_hostlist_count', 0) if 'manager' in locals() else 0}",
            f"active_hostlist_path: {getattr(manager, 'active_hostlist_path', '') if 'manager' in locals() else ''}",
            f"configured_dpi_targets: {self._dpi_targets_info().get('count', 0)}",
            "strategy_args:",
        ]
        lines.extend(f"  {a}" for a in args)
        last_cmd = status.get("last_cmd") or []
        if last_cmd:
            lines += ["", "last_cmd:", "  " + " ".join(last_cmd)]
        tail = self._winws_log_tail()
        if tail:
            lines += ["", "winws.log tail:", tail]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self._btn_copy_winws_diag.setText("✓ Скопировано")
        QTimer.singleShot(1500, lambda: self._btn_copy_winws_diag.setText("📋 Скопировать DPI-диагностику"))

    # ── Domain check ────────────────────────────────────────────────────────
    # ── Quick tools ────────────────────────────────────────────────────────
    def _bogus_status_text(self) -> str:
        ts = bogus_last_updated()
        if not ts:
            return "Защита от подмен: список ещё не обновлялся в этой сессии. Автообновление выполняется в фоне."
        dt = datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
        return f"Защита от подмен: последнее обновление {dt}."

    def _update_bogus_list(self):
        if self._bogus_worker and self._bogus_worker.isRunning():
            return
        self._btn_bogus_update.setEnabled(False)
        self._btn_bogus_update.setText("Обновляю...")
        self._bogus_status.setText("Защита от подмен: запрос обновления...")
        self._bogus_worker = _BogusUpdateWorker()
        self._bogus_worker.done.connect(self._on_bogus_done)
        self._bogus_worker.start()

    def _on_bogus_done(self, ok: bool):
        self._btn_bogus_update.setEnabled(True)
        self._btn_bogus_update.setText("🛡 Обновить защиту от подмен")
        if ok:
            self._bogus_status.setText(self._bogus_status_text())
            self._bogus_status.setStyleSheet(f"color:{theme.GREEN};font-size:12px;background:transparent;border:none;")
            add_query_log_event("[Защита от подмен]", source="fixed", rcode="OK", note="bogus-IP список обновлён")
        else:
            self._bogus_status.setText("Защита от подмен: не удалось обновить список (нет сети или сервер недоступен).")
            self._bogus_status.setStyleSheet(f"color:{theme.YELLOW};font-size:12px;background:transparent;border:none;")
            add_query_log_event("[Защита от подмен]", source="error", rcode="FAIL", note="не удалось обновить bogus-IP список")

    # ── misc ────────────────────────────────────────────────────────────────
    def _flush_dns(self):
        ok = flush_dns_cache()
        self._btn_flush.setText("✓ Сброшен" if ok else "Ошибка")
        QTimer.singleShot(1400, lambda: self._btn_flush.setText("Сбросить DNS-кэш"))

    # ── Откат сети (P0-2) ───────────────────────────────────────────────────
    def _update_restore_hint(self):
        """Обновляет тултип кнопки: ЧТО именно вернёт откат."""
        if not hasattr(self, "_btn_restore_net"):
            return
        info = network_snapshot_info() or {}
        if info.get("exists"):
            when = info.get("time_local") or "неизвестное время"
            count = info.get("adapters") or 0
            tip = (f"Вернуть DNS, как было до UmbraNet\nСнапшот: {when}\n"
                   f"Адаптеров в снапшоте: {count}")
        else:
            tip = ("Снапшот не найден. Будет выполнен сброс системного DNS на "
                   "«Авто (DHCP)». Снапшот создаётся автоматически при первом "
                   "запуске UmbraNet.")
        if bool(getattr(self.engine, "running", False)):
            tip += "\n\nUmbraNet запущен: после отката обход выключится, " \
                   "понадобится снова нажать «Старт»."
        try:
            self._btn_restore_net.setToolTip(tip)
        except Exception:
            pass

    def _restore_confirm_text(self) -> str:
        """Текст подтверждения отката. Отдельным методом — чтобы покрыть тестом.

        Пользователь должен понимать ДВА последствия, иначе кнопка выглядит
        как «сделать хорошо», а на деле выключает обход:
          1) DNS вернутся к снапшоту (или на «Авто», если снапшота нет);
          2) пока UmbraNet работает, после отката его DNS-сервер и маршрутизация
             перестанут использоваться — нужно снова нажать «Старт».
        """
        info = network_snapshot_info() or {}
        if info.get("exists"):
            target = (f"Системный DNS вернётся к снапшоту "
                      f"{info.get('time_local') or ''} "
                      f"({info.get('adapters', 0)} адапт.).")
        else:
            target = "Снапшот не найден — системный DNS будет сброшен на «Авто (DHCP)»."

        parts = [
            f"<b>{target}</b>",
            "Это вернёт DNS-настройки Windows в состояние до запуска UmbraNet. "
            "Полезно, если после выхода остался прописан 127.0.0.1 и интернет "
            "не работает.",
        ]
        if bool(getattr(self.engine, "running", False)):
            parts.append(
                "<b>UmbraNet сейчас запущен.</b> После отката домены пойдут мимо "
                "нашего DNS-сервера: журнал запросов перестанет пополняться, "
                "а маршрутизация через UmbraNet и обход DPI — работать. "
                "<b>Чтобы вернуть обход, нажмите «Старт» заново.</b>"
            )
        return "<br><br>".join(parts)

    def _restore_network(self):
        worker = getattr(self, "_restore_worker", None)
        if worker and worker.isRunning():
            return
        answer = QMessageBox.question(
            self, "Откатить настройки сети",
            self._restore_confirm_text(),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self._btn_restore_net.setEnabled(False)
        self._btn_restore_net.setText("Откатываю...")
        self._restore_worker = _RestoreNetworkWorker()
        self._restore_worker.done.connect(self._on_restore_done)
        self._restore_worker.start()

    def _on_restore_done(self, ok: bool, msg: str):
        self._btn_restore_net.setEnabled(True)
        self._btn_restore_net.setText("✓ Сеть откачена" if ok else "↩ Откатить сеть")
        if not ok:
            self._restore_worker = None
            QMessageBox.warning(
                self, "Откат сети не удался",
                f"{msg}\n\nВозможные причины: нужны права администратора, "
                "или снапшот повреждён.",
            )
            return
        self._update_restore_hint()
        QTimer.singleShot(2500, lambda: self._btn_restore_net.setText("↩ Откатить сеть"))
        try:
            self._refresh_dpi_status()
        except Exception:
            pass
        QMessageBox.information(self, "Сеть откачена", msg or "Готово")
        self._restore_worker = None

    def refresh(self):
        self.engine = get_engine()
        self._refresh_powershell_warning()

        self._refresh_dpi_status()
        if hasattr(self, "_bogus_status"):
            self._bogus_status.setText(self._bogus_status_text())
            self._bogus_status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        self._update_restore_hint()
        self._refresh_answering()
        self._refresh_health_score()

    def _refresh_powershell_warning(self):
        """Показывает/прячет предупреждение о недоступном PowerShell (H4)."""
        try:
            from umbranet.engine_adapter import powershell_warning
            text = powershell_warning()
        except (ImportError, OSError, ValueError) as exc:
            log.debug("Предупреждение о PowerShell не получено: %s", exc)
            text = ""
        self._ps_warning.setText(text)
        self._ps_warning.setVisible(bool(text))
        if text:
            self._ps_warning.setToolTip(
                "Нажми, чтобы проверить PowerShell заново — "
                "перезапускать программу для этого не нужно"
            )

    def _recheck_powershell(self):
        """Проверяет PowerShell заново, не ожидая, пока истечёт кэш проверки (H4)."""
        try:
            from umbranet.engine_adapter import powershell_recheck
            available = powershell_recheck()
        except (ImportError, OSError, ValueError) as exc:
            log.debug("Перепроверка PowerShell не удалась: %s", exc)
            return
        if available:
            log.info("PowerShell снова доступен — предупреждение снимается")
        self._refresh_powershell_warning()
        self._refresh_dpi_status()

    def _open_test(self):
        from umbranet.widgets.dialogs import TestDnsDialog
        TestDnsDialog(self).exec()

    def _open_domain_diag(self):
        from umbranet.widgets.dialogs import DomainDiagnosticsDialog
        DomainDiagnosticsDialog(self).exec()
