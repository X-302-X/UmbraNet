"""
UmbraNet - модальные диалоги (PySide6 / Qt Widgets).

Портированы из старой версии (netdocker/ui/dialogs.py) в новый
vibrant-gradient дизайн:
  • TestDnsDialog       — пинг DNS-серверов + резолв домена (система/DoH) + диагноз
  • ProcessPickerDialog — выбор запущенного процесса из списка (поиск)
  • RestoreBackupDialog — выбор резервной копии конфига для восстановления

Все тяжёлые сетевые операции уходят в QThread, результаты возвращаются
через сигналы (thread-safe для Qt).
"""

from __future__ import annotations

import time

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from umbranet import engine_adapter as ea
from umbranet import theme


# ════════════════════════════════════════════════════════════════════════════
#  Общие хелперы стиля
# ════════════════════════════════════════════════════════════════════════════
def _style_dialog(dlg: QDialog):
    dlg.setStyleSheet(f"QDialog{{background:{theme.BG};}}")


def _accent_btn(text: str, color: str, fg: str = theme.WHITE) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setMinimumHeight(38)
    b.setStyleSheet(
        f"QPushButton{{background:{color};color:{fg};border:none;"
        "border-radius:10px;font-weight:700;padding:0 18px;}"
        f"QPushButton:hover{{background:{color};}}"
        f"QPushButton:disabled{{background:{theme.BORDER};color:{theme.MUTED};}}")
    return b


def _ghost_btn(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setMinimumHeight(38)
    b.setStyleSheet(
        f"QPushButton{{background:transparent;color:{theme.SUBTEXT};"
        f"border:1px solid {theme.BORDER};border-radius:10px;font-weight:600;padding:0 18px;}}"
        f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.TEXT};}}")
    return b


def _list_widget() -> QListWidget:
    lw = QListWidget()
    lw.setStyleSheet(
        f"QListWidget{{background:{theme.INPUT_BG};color:{theme.TEXT};"
        f"border:1px solid {theme.BORDER};border-radius:10px;padding:4px;"
        "font-family:Consolas;font-size:12px;outline:none;}"
        f"QListWidget::item{{padding:6px 8px;border-radius:6px;}}"
        f"QListWidget::item:selected{{background:{theme.ACCENT};color:{theme.WHITE};}}"
        f"QListWidget::item:hover{{background:{theme.CARD};}}"
        + theme.scrollbar_qss())
    return lw


def _title_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{theme.TEXT};font-size:16px;font-weight:700;background:transparent;")
    return lbl




# ════════════════════════════════════════════════════════════════════════════
#  Мастер диагностики домена
# ════════════════════════════════════════════════════════════════════════════
class _DomainDiagnosticsWorker(QThread):
    done = Signal(dict)

    def __init__(self, domain: str):
        super().__init__()
        self.domain = domain

    def run(self):
        try:
            result = ea.diagnose_domain(self.domain)
        except Exception as exc:  # noqa: BLE001
            result = {
                "domain": self.domain,
                "summary": f"Ошибка диагностики: {exc}",
                "severity": "error",
                "steps": [{"status": "error", "title": "Диагностика упала", "detail": str(exc)}],
                "actions": ["Откройте журнал UmbraNet и пришлите traceback разработчику."],
            }
        self.done.emit(result)


class DomainDiagnosticsDialog(QDialog):
    """Мастер «Почему сайт не открывается?».

    Даёт обычному пользователю последовательный ответ: запущен ли UmbraNet,
    смотрит ли Windows на 127.0.0.1, не обходит ли браузер системный DNS,
    маршрутизируется ли домен, отвечают ли local DNS/DoH, есть ли bogus-IP,
    доступен ли TCP 443 и нет ли DNS/IPv6-утечки.
    """

    QUICK = ["chatgpt.com", "claude.ai", "github.com", "discord.com", "youtube.com", "google.com"]

    _STATUS = {
        "ok": ("✓", theme.GREEN, "OK"),
        "warn": ("⚠", theme.YELLOW, "WARN"),
        "warning": ("⚠", theme.YELLOW, "WARN"),
        "error": ("✕", theme.RED, "FAIL"),
        "err": ("✕", theme.RED, "FAIL"),
        "info": ("ℹ", theme.ACCENT3, "INFO"),
    }

    def __init__(self, parent=None, domain: str = "chatgpt.com"):
        super().__init__(parent)
        self.setWindowTitle("Мастер диагностики сайта")
        self.resize(820, 680)
        self.setMinimumSize(700, 560)
        _style_dialog(self)
        self._worker: _DomainDiagnosticsWorker | None = None
        self._last_result: dict | None = None
        self._build(domain)

    def _build(self, domain: str):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(_title_label("🧭  Почему сайт не открывается?"))

        intro = QLabel(
            "Введите домен — UmbraNet проверит всю цепочку: запуск, системный DNS, "
            "DoH в браузере, маршрутизацию, local DNS, upstream, bogus-IP, TCP 443 и DNS-утечки."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;")
        root.addWidget(intro)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._domain = QLineEdit()
        self._domain.setText(domain)
        self._domain.setPlaceholderText("например, chatgpt.com")
        self._domain.setMinimumHeight(38)
        self._domain.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:6px 10px;font-family:Consolas;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        self._domain.returnPressed.connect(self._run)
        top.addWidget(self._domain, 1)
        self._btn_run = _accent_btn("▶  Диагностика", theme.ACCENT)
        self._btn_run.clicked.connect(self._run)
        top.addWidget(self._btn_run)
        root.addLayout(top)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        ql = QLabel("Быстро:")
        ql.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        quick.addWidget(ql)
        for d in self.QUICK:
            chip = QPushButton(d)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setStyleSheet(
                f"QPushButton{{background:{theme.CARD};color:{theme.SUBTEXT};"
                f"border:1px solid {theme.BORDER};border-radius:9px;padding:5px 9px;font-size:11px;}}"
                f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.TEXT};}}")
            chip.clicked.connect(lambda _=False, x=d: (self._domain.setText(x), self._run()))
            quick.addWidget(chip)
        quick.addStretch()
        root.addLayout(quick)

        self._summary = QLabel("Нажмите «Диагностика» для запуска проверки.")
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(
            f"QLabel{{background:{theme.CARD};color:{theme.TEXT};border:1px solid {theme.BORDER};"
            "border-radius:12px;padding:10px;font-size:13px;font-weight:700;}")
        root.addWidget(self._summary)

        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(
            f"QPlainTextEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:10px;"
            "font-family:Consolas;font-size:12px;}" + theme.scrollbar_qss())
        root.addWidget(self._out, 1)

        bottom = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;")
        bottom.addWidget(self._status, 1)
        self._btn_copy = _ghost_btn("📋 Скопировать отчёт")
        self._btn_copy.clicked.connect(self._copy)
        bottom.addWidget(self._btn_copy)
        close = _ghost_btn("Закрыть")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        root.addLayout(bottom)

    def _run(self):
        if self._worker and self._worker.isRunning():
            return
        domain = ea.normalize_domain(self._domain.text())
        self._domain.setText(domain)
        self._out.clear()
        self._summary.setText("⏳ Выполняется диагностика...")
        self._summary.setStyleSheet(
            f"QLabel{{background:rgba(251,191,36,0.12);color:{theme.TEXT};border:1px solid {theme.YELLOW};"
            "border-radius:12px;padding:10px;font-size:13px;font-weight:700;}")
        self._btn_run.setEnabled(False)
        self._status.setText("Проверка может занять несколько секунд...")
        self._worker = _DomainDiagnosticsWorker(domain)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, result: dict):
        self._last_result = result
        self._btn_run.setEnabled(True)
        sev = result.get("severity", "info")
        icon, color, label = self._STATUS.get(sev, self._STATUS["info"])
        self._summary.setText(f"{icon} {result.get('domain', '')}: {result.get('summary', '')}")
        self._summary.setStyleSheet(
            f"QLabel{{background:rgba(255,255,255,0.04);color:{theme.TEXT};border:1px solid {color};"
            "border-radius:12px;padding:10px;font-size:13px;font-weight:700;}")
        self._out.setPlainText(self._format_report(result))
        self._status.setText("✓ Готово")
        self._status.setStyleSheet(f"color:{theme.GREEN};font-size:12px;")

    def _format_report(self, r: dict) -> str:
        lines = []
        lines.append(f"UmbraNet — мастер диагностики домена: {r.get('domain', '')}")
        lines.append(f"Итог: {r.get('summary', '')}")
        lines.append("=" * 72)
        lines.append("")
        lines.append("Проверки:")
        for i, step in enumerate(r.get("steps") or [], 1):
            st = step.get("status", "info")
            icon, _color, label = self._STATUS.get(st, self._STATUS["info"])
            lines.append(f"{i:02d}. {icon} [{label}] {step.get('title', '')}")
            if step.get("detail"):
                lines.append(f"    {step.get('detail')}")
        lines.append("")
        lines.append("Рекомендации:")
        actions = r.get("actions") or []
        if actions:
            for i, act in enumerate(actions, 1):
                lines.append(f"{i}. {act}")
        else:
            lines.append("— Рекомендаций нет.")
        return "\n".join(lines)

    def _copy(self):
        text = self._out.toPlainText()
        if not text and self._last_result:
            text = self._format_report(self._last_result)
        if text:
            QGuiApplication.clipboard().setText(text)
            self._status.setText("Скопировано в буфер обмена")
            self._status.setStyleSheet(f"color:{theme.ACCENT3};font-size:12px;")

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.wait(2500)
        super().closeEvent(event)


# ════════════════════════════════════════════════════════════════════════════
#  Тест DNS — фоновый воркер
# ════════════════════════════════════════════════════════════════════════════
class _DnsTestWorker(QThread):
    """Прогоняет пинг серверов и (опц.) резолв домена. Шлёт сигналы в UI."""
    ping_result = Signal(str, object)        # (имя сервера, ms|None)
    log_line = Signal(str, str)              # (текст, тег цвета)
    finished_all = Signal()

    # сервера для пинга: (имя, ip|None, тип)
    SERVERS = [
        ("Системный DNS", None, "sys"),
        ("Cloudflare", "1.1.1.1", "tcp"),
        ("Google DNS", "8.8.8.8", "tcp"),
        ("Quad9", "9.9.9.9", "tcp"),
        ("xbox-dns.ru DoH", None, "doh"),
    ]

    def __init__(self, domain: str = ""):
        super().__init__()
        self.domain = domain

    def run(self):
        # ── пинг серверов ──
        for name, ip, kind in self.SERVERS:
            if kind == "doh":
                ok, ms = ea.probe_doh(ea.XBOX_DOH_URL)
            elif kind == "sys":
                ips, ms = ea.resolve_system("google.com")
                ok = ips is not None
                if not ok:
                    ms = None
            else:
                ok, ms = ea.probe_host(ip)
            self.ping_result.emit(name, ms if ok else None)

        # ── резолв домена ──
        if self.domain:
            self._domain_test(self.domain)
        self.finished_all.emit()

    def _domain_test(self, domain: str):
        cfg = ea.get_engine().config
        w = self.log_line.emit

        w(f"━━━  {domain}  ━━━\n", "head")

        w("\n🖥  Системный DNS\n", "head")
        ips, ms = ea.resolve_system(domain)
        if ips:
            w(f"  ✓  {ms} мс  →  {', '.join(ips)}\n", "ok")
        else:
            w(f"  ✗  Ошибка: {ms}\n", "err")

        w("\n☁  Cloudflare DoH\n", "head")
        ips_cf, ms_cf = ea.resolve_doh(domain, "https://cloudflare-dns.com/dns-query")
        if ips_cf:
            w(f"  ✓  {ms_cf} мс  →  {', '.join(ips_cf)}\n", "ok")
        else:
            w(f"  ✗  Ошибка: {ms_cf}\n", "err")

        w("\n🔵  Google DoH\n", "head")
        ips_g, ms_g = ea.resolve_doh(domain, "https://dns.google/dns-query")
        if ips_g:
            w(f"  ✓  {ms_g} мс  →  {', '.join(ips_g)}\n", "ok")
        else:
            w(f"  ✗  Ошибка: {ms_g}\n", "err")

        w("\n🎮  xbox-dns.ru DoH\n", "head")
        ips_x, ms_x = ea.resolve_doh(domain, ea.XBOX_DOH_URL)
        if ips_x:
            w(f"  ✓  {ms_x} мс  →  {', '.join(ips_x)}\n", "ok")
        else:
            w(f"  ✗  Ошибка: {ms_x}\n", "err")

        # маршрутизация
        try:
            routed = ea.is_domain_routed(domain, cfg)
        except Exception:
            routed = False
        w("\n", "dim")
        if routed:
            w("  🔒 Домен маршрутизируется через DoH\n", "ok")
        else:
            w("  ℹ  Домен НЕ в списке маршрутизации (обычный DNS)\n", "warn")

        # проверка порта 443
        w("\n🔌  Проверка TCP-соединения (порт 443)\n", "head")
        test_ips = ips if ips else (ips_cf if ips_cf else [])
        v4 = [ip for ip in test_ips if ":" not in ip][:2]
        port_ok = None
        if v4:
            for ip in v4:
                r = ea.check_port(ip, 443)
                if port_ok is None:
                    port_ok = r
                if r is True:
                    w(f"  ✓  {ip}:443 — порт открыт, IP не заблокирован\n", "ok")
                elif r is False:
                    w(f"  ✗  {ip}:443 — порт ЗАКРЫТ! Провайдер блокирует по IP\n", "err")
                else:
                    w(f"  ⚠  {ip}:443 — таймаут\n", "warn")
        else:
            w("  ℹ  Нет IPv4-адресов для проверки\n", "warn")

        # диагноз
        w("\n💡  Диагноз\n", "head")
        sys_ok = bool(ips)
        doh_ok = bool(ips_cf) or bool(ips_g)
        if not sys_ok and doh_ok:
            w("  → DNS заблокирован провайдером, но DoH работает.\n", "warn")
            w("  → Убедись, что UmbraNet DNS-сервер ЗАПУЩЕН и\n", "warn")
            w("    в Windows DNS установлен на 127.0.0.1\n", "warn")
        elif sys_ok and port_ok is False:
            w("  → DNS работает, но провайдер блокирует IP-адреса!\n", "err")
            w("  → DoH и UmbraNet НЕ помогут — нужен VPN или прокси.\n", "err")
        elif sys_ok and port_ok is True:
            w("  → DNS и IP доступны. Возможные причины блокировки:\n", "warn")
            w("  → 1. Браузер использует свой DoH (игнорирует системный DNS)\n", "warn")
            w("  → 2. Старый DNS-кэш — очисти: chrome://net-internals/#dns\n", "warn")
            w("  → 3. Попробуй режим инкогнито (Ctrl+Shift+N)\n", "warn")
        elif not sys_ok and not doh_ok:
            w("  → Ни системный DNS, ни DoH не работают.\n", "err")
            w("  → Проверь подключение к интернету.\n", "err")
        else:
            w("  → Всё выглядит нормально. Проверь настройки браузера.\n", "ok")
        w("─" * 38 + "\n", "dim")


class TestDnsDialog(QDialog):
    """Окно «Тест DNS и доменов»."""

    QUICK = ["chatgpt.com", "claude.ai", "gemini.google.com",
             "github.com", "google.com", "ya.ru"]

    _TAG_COLOR = {
        "head": theme.ACCENT3, "ok": theme.GREEN, "err": theme.RED,
        "warn": theme.YELLOW, "dim": theme.MUTED,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Тест DNS и доменов")
        self.resize(760, 640)
        self.setMinimumSize(680, 560)
        _style_dialog(self)
        self._worker: _DnsTestWorker | None = None
        self._ping_rows: dict[str, QLabel] = {}
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        root.addWidget(_title_label("🧪  Тест DNS и доменов"))

        # ── строка домена + кнопки ──
        top = QHBoxLayout()
        top.setSpacing(8)
        self._domain = QLineEdit()
        self._domain.setPlaceholderText("например, chatgpt.com")
        self._domain.setMinimumHeight(38)
        self._domain.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:6px 10px;font-family:Consolas;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        self._domain.returnPressed.connect(self._run)
        top.addWidget(self._domain, 1)

        self._btn_run = _accent_btn("▶  Проверить", theme.ACCENT)
        self._btn_run.clicked.connect(self._run)
        top.addWidget(self._btn_run)
        root.addLayout(top)

        # ── быстрые домены ──
        quick = QHBoxLayout()
        quick.setSpacing(6)
        ql = QLabel("Быстро:")
        ql.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        quick.addWidget(ql)
        for d in self.QUICK:
            chip = QPushButton(d)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setStyleSheet(
                f"QPushButton{{background:{theme.CARD};color:{theme.SUBTEXT};"
                f"border:1px solid {theme.BORDER};border-radius:9px;padding:4px 10px;font-size:11px;}}"
                f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.TEXT};}}")
            chip.clicked.connect(lambda _=False, x=d: (self._domain.setText(x), self._run()))
            quick.addWidget(chip)
        quick.addStretch()
        root.addLayout(quick)

        # ── карточка пинга серверов ──
        ping_card = QFrame()
        ping_card.setStyleSheet(f"QFrame{{{theme.card_qss()}}}")
        pl = QVBoxLayout(ping_card)
        pl.setContentsMargins(14, 12, 14, 12)
        pl.setSpacing(4)
        pt = QLabel("📡  Пинг DNS-серверов")
        pt.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;")
        pl.addWidget(pt)
        for name, ip, _ in _DnsTestWorker.SERVERS:
            row = QHBoxLayout()
            n = QLabel(name)
            n.setFixedWidth(190)
            n.setStyleSheet(f"color:{theme.TEXT};font-size:12px;background:transparent;")
            row.addWidget(n)
            a = QLabel(ip if ip else "авто")
            a.setStyleSheet(f"color:{theme.MUTED};font-size:11px;font-family:Consolas;background:transparent;")
            row.addWidget(a, 1)
            res = QLabel("—")
            res.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            res.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;font-weight:700;font-family:Consolas;background:transparent;")
            self._ping_rows[name] = res
            row.addWidget(res)
            pl.addLayout(row)
        root.addWidget(ping_card)

        # ── результат резолва (текст) ──
        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(
            f"QPlainTextEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:8px;"
            "font-family:Consolas;font-size:12px;}" + theme.scrollbar_qss())
        root.addWidget(self._out, 1)

        # ── статус + закрыть ──
        bottom = QHBoxLayout()
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;")
        bottom.addWidget(self._status, 1)
        close = _ghost_btn("Закрыть")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        root.addLayout(bottom)

    # ── запуск ──
    def _run(self):
        if self._worker and self._worker.isRunning():
            return
        domain = self._domain.text().strip().lower()
        for pref in ("https://", "http://", "www."):
            domain = domain.removeprefix(pref)
        domain = domain.split("/")[0]
        self._domain.setText(domain)

        self._out.clear()
        for lbl in self._ping_rows.values():
            lbl.setText("...")
            lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;font-weight:700;font-family:Consolas;background:transparent;")
        self._btn_run.setEnabled(False)
        self._set_status("⏳ Тестирование...", theme.YELLOW)

        self._worker = _DnsTestWorker(domain)
        self._worker.ping_result.connect(self._on_ping)
        self._worker.log_line.connect(self._on_log)
        self._worker.finished_all.connect(self._on_done)
        self._worker.start()

    def _on_ping(self, name: str, ms):
        lbl = self._ping_rows.get(name)
        if not lbl:
            return
        if ms is None:
            lbl.setText("Недоступен")
            lbl.setStyleSheet(f"color:{theme.RED};font-size:12px;font-weight:700;font-family:Consolas;background:transparent;")
        else:
            lbl.setText(f"{ms} мс")
            lbl.setStyleSheet(f"color:{theme.GREEN};font-size:12px;font-weight:700;font-family:Consolas;background:transparent;")

    def _on_log(self, text: str, tag: str):
        color = self._TAG_COLOR.get(tag, theme.TEXT)
        self._out.appendHtml(
            f'<span style="color:{color};white-space:pre">{self._escape(text)}</span>')

    @staticmethod
    def _escape(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    def _on_done(self):
        self._btn_run.setEnabled(True)
        self._set_status("✓ Готово", theme.GREEN)

    def _set_status(self, text: str, color: str):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color};font-size:12px;")

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.wait(2000)
        super().closeEvent(event)


# ════════════════════════════════════════════════════════════════════════════
#  Выбор процесса
# ════════════════════════════════════════════════════════════════════════════
class _ProcLoadWorker(QThread):
    loaded = Signal(list)          # имена — сразу, чтобы список не ждал иконки
    paths_ready = Signal(dict)     # name -> путь к .exe (QPixmap собирает GUI-поток)

    def run(self):
        names = sorted({p.get("name") for p in ea.get_running_processes() if p.get("name")},
                       key=str.lower)
        self.loaded.emit(names)
        if self.isInterruptionRequested():
            return
        paths = ea.resolve_process_exes(names, interrupt=self.isInterruptionRequested)
        if not self.isInterruptionRequested():
            self.paths_ready.emit(paths)


class ProcessPickerDialog(QDialog):
    """Выбор имени запущенного процесса. После accept() — self.result."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Выбор процесса")
        self.resize(420, 540)
        self.setMinimumSize(360, 420)
        _style_dialog(self)
        self.result: str | None = None
        self._all: list[str] = []
        self._paths: dict = {}
        self._build()
        self._load()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        root.addWidget(_title_label("Выберите запущенный процесс"))

        self._search = QLineEdit()
        self._search.setPlaceholderText("поиск...")
        self._search.setMinimumHeight(36)
        self._search.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:6px 10px;font-family:Consolas;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        self._search.textChanged.connect(self._filter)
        root.addWidget(self._search)

        self._list = _list_widget()
        self._list.setIconSize(QSize(16, 16))
        self._list.itemDoubleClicked.connect(lambda _i: self._pick())
        root.addWidget(self._list, 1)

        self._hint = QLabel("Загрузка списка процессов...")
        self._hint.setStyleSheet(f"color:{theme.MUTED};font-size:11px;")
        root.addWidget(self._hint)

        btns = QHBoxLayout()
        cancel = _ghost_btn("Отмена")
        cancel.clicked.connect(self.reject)
        add = _accent_btn("✓  Добавить выбранный", theme.GREEN)
        add.clicked.connect(self._pick)
        btns.addWidget(cancel)
        btns.addStretch()
        btns.addWidget(add)
        root.addLayout(btns)

    def _load(self):
        self._worker = _ProcLoadWorker()
        self._worker.loaded.connect(self._on_loaded)
        self._worker.paths_ready.connect(self._on_paths)
        self._worker.start()

    def _on_loaded(self, names: list):
        self._all = names
        if names:
            self._hint.setText(f"Найдено процессов: {len(names)}")
        else:
            self._hint.setText("Список пуст (доступно только на Windows / с правами).")
        self._filter(self._search.text() if hasattr(self, "_search") else "")

    def _on_paths(self, paths: dict):
        self._paths = dict(paths or {})
        # Перерисовать видимые строки с настоящими иконками, не сбрасывая фильтр.
        q = self._search.text() if hasattr(self, "_search") else ""
        self._filter(q)

    def _item_icon(self, name: str) -> QIcon:
        from umbranet.process_icons import gamepad_icon, pixmap_for_exe
        path = self._paths.get(name)
        pm = pixmap_for_exe(path) if path else None
        if pm is not None:
            return QIcon(pm)
        return gamepad_icon()

    def _show(self, items):
        current = self._list.currentItem().text() if self._list.currentItem() else None
        self._list.clear()
        for it in items:
            item = QListWidgetItem(it)
            item.setIcon(self._item_icon(it))
            self._list.addItem(item)
        if current:
            matches = self._list.findItems(current, Qt.MatchExactly)
            if matches:
                self._list.setCurrentItem(matches[0])

    def _filter(self, text: str):
        q = text.lower().strip()
        self._show([x for x in self._all if q in x.lower()] if q else self._all)

    def _pick(self):
        it = self._list.currentItem()
        if not it:
            return
        self.result = it.text()
        self.accept()

    def closeEvent(self, event):
        w = getattr(self, "_worker", None)
        if w is not None:
            try:
                w.loaded.disconnect(self._on_loaded)
            except (TypeError, RuntimeError):
                pass
            try:
                w.paths_ready.disconnect(self._on_paths)
            except (TypeError, RuntimeError):
                pass
            if w.isRunning():
                w.requestInterruption()
                w.wait(1500)
        super().closeEvent(event)


# ════════════════════════════════════════════════════════════════════════════
#  Восстановление резервной копии
# ════════════════════════════════════════════════════════════════════════════
class RestoreBackupDialog(QDialog):
    """Выбор бэкапа конфига. После accept() — self.result = путь к файлу."""

    def __init__(self, backups: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Восстановить резервную копию")
        self.resize(560, 400)
        self.setMinimumSize(460, 320)
        _style_dialog(self)
        self.result: str | None = None
        self._backups = backups or []
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        root.addWidget(_title_label("Выберите резервную копию настроек"))

        self._list = _list_widget()
        self._list.itemDoubleClicked.connect(lambda _i: self._choose())
        root.addWidget(self._list, 1)

        if not self._backups:
            empty = QLabel("Резервных копий пока нет.")
            empty.setStyleSheet(f"color:{theme.MUTED};font-size:12px;")
            root.addWidget(empty)
        else:
            for item in self._backups:
                dt = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(item.get("mtime", 0)))
                size_kb = max(1, int(item.get("size", 0)) // 1024)
                li = QListWidgetItem(f"{dt}    {item.get('name', '?')}    {size_kb} KB")
                li.setData(Qt.UserRole, item.get("path"))
                self._list.addItem(li)
            self._list.setCurrentRow(0)

        btns = QHBoxLayout()
        cancel = _ghost_btn("Отмена")
        cancel.clicked.connect(self.reject)
        restore = _accent_btn("↺  Восстановить", theme.GREEN)
        restore.clicked.connect(self._choose)
        restore.setEnabled(bool(self._backups))
        btns.addWidget(cancel)
        btns.addStretch()
        btns.addWidget(restore)
        root.addLayout(btns)

    def _choose(self):
        it = self._list.currentItem()
        if not it:
            return
        self.result = it.data(Qt.UserRole)
        self.accept()


# ════════════════════════════════════════════════════════════════════════════
#  Подсказка по транспортам DNS (попап «?»)
# ════════════════════════════════════════════════════════════════════════════
class TransportHelpDialog(QDialog):
    """Объясняет, что делает каждый режим транспорта (UDP/DoH/DoT/DoQ/DNSCrypt)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Режимы транспорта DNS")
        self.resize(560, 560)
        self.setMinimumSize(480, 460)
        _style_dialog(self)
        self._build()

    def _build(self):
        # импортируем тут, чтобы не плодить циклические импорты на уровне модуля
        from umbranet.widgets.header import _TRANSPORT_HELP

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(_title_label("📖  Режимы транспорта DNS"))

        # карточки по каждому транспорту
        wrap = QVBoxLayout()
        wrap.setSpacing(10)
        for key in ea.TRANSPORTS:
            title, desc = _TRANSPORT_HELP.get(key, (ea.TRANSPORT_LABELS.get(key, key), ""))
            avail, reason = ea.transport_available(key)
            chain = " → ".join(ea.TRANSPORT_LABELS.get(t, t) for t in ea.TRANSPORT_FALLBACK.get(key, []))

            card = QFrame()
            card.setObjectName("TransportHelpCard")
            # Важно: QLabel наследуется от QFrame, поэтому селектор "QFrame{...}"
            # случайно рисовал серую рамку вокруг названия/статуса/описания.
            # Ограничиваем стиль только самой карточкой по objectName.
            card.setStyleSheet(f"QFrame#TransportHelpCard{{{theme.card_qss()}}}")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.setSpacing(4)

            head = QHBoxLayout()
            t = QLabel(title)
            t.setStyleSheet(f"color:{theme.TEXT};font-size:14px;font-weight:700;background:transparent;border:none;")
            head.addWidget(t)
            head.addStretch()
            badge = QLabel("● доступен" if avail else "● недоступен")
            badge.setStyleSheet(
                f"color:{theme.GREEN if avail else theme.YELLOW};font-size:11px;"
                "font-weight:700;background:transparent;border:none;")
            head.addWidget(badge)
            cl.addLayout(head)

            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
            cl.addWidget(d)

            ch = QLabel(f"Цепочка: {chain}")
            ch.setWordWrap(True)
            ch.setStyleSheet(f"color:{theme.MUTED};font-size:11px;font-family:Consolas;background:transparent;border:none;")
            cl.addWidget(ch)

            if not avail and reason:
                r = QLabel(f"⚠ {reason}")
                r.setWordWrap(True)
                r.setStyleSheet(f"color:{theme.YELLOW};font-size:11px;background:transparent;border:none;")
                cl.addWidget(r)

            wrap.addWidget(card)

        # обёртка со скроллом
        from PySide6.QtWidgets import QScrollArea
        inner = QWidget()
        inner.setLayout(wrap)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        close = _ghost_btn("Закрыть")
        close.clicked.connect(self.accept)
        brow = QHBoxLayout()
        brow.addStretch()
        brow.addWidget(close)
        root.addLayout(brow)


# ════════════════════════════════════════════════════════════════════════════
#  Выбор готового DNSCrypt-резолвера
# ════════════════════════════════════════════════════════════════════════════
class DnsCryptResolverDialog(QDialog):
    """Выбор готового DNSCrypt-резолвера (sdns:// штамп).

    После accept(): self.result = {'name':..., 'stamp':...} выбранного резолвера.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Выбор DNSCrypt-резолвера")
        self.resize(560, 520)
        self.setMinimumSize(460, 420)
        _style_dialog(self)
        self.result = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        root.addWidget(_title_label("🔐  Выбор DNSCrypt-резолвера"))

        intro = QLabel(
            "DNSCrypt требует сервер со штампом sdns://. Выбери готовый из "
            "проверенных публичных резолверов — UmbraNet создаст профиль с ним "
            "и сделает его активным. Свой штамп можно вписать в разделе "
            "«DNS-профили».")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;")
        root.addWidget(intro)

        # список карточек
        wrap = QVBoxLayout()
        wrap.setSpacing(10)
        self._cards = []
        self._selected_idx = -1
        for i, res in enumerate(ea.DNSCRYPT_RESOLVERS):
            card = QFrame()
            card.setCursor(Qt.PointingHandCursor)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            cl.setSpacing(2)
            t = QLabel(res["name"])
            t.setStyleSheet(f"color:{theme.TEXT};font-size:14px;font-weight:700;background:transparent;border:none;")
            cl.addWidget(t)
            d = QLabel(res.get("desc", ""))
            d.setWordWrap(True)
            d.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
            cl.addWidget(d)
            card.mousePressEvent = lambda _e, idx=i: self._select(idx)
            self._cards.append(card)
            wrap.addWidget(card)

        inner = QWidget()
        inner.setLayout(wrap)
        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        # кнопки
        brow = QHBoxLayout()
        cancel = _ghost_btn("Отмена")
        cancel.clicked.connect(self.reject)
        self._apply = _accent_btn("✓  Применить", theme.GREEN)
        self._apply.clicked.connect(self._confirm)
        self._apply.setEnabled(False)
        brow.addWidget(cancel)
        brow.addStretch()
        brow.addWidget(self._apply)
        root.addLayout(brow)

        self._restyle()

    def _select(self, idx: int):
        self._selected_idx = idx
        self._apply.setEnabled(True)
        self._restyle()

    def _restyle(self):
        for i, card in enumerate(self._cards):
            if i == self._selected_idx:
                card.setStyleSheet(
                    "QFrame{background:" + theme.grad(theme.CARD_TOP, theme.ROW_BG, False) + ";"
                    f"border:1px solid {theme.ACCENT};border-radius:10px;}}"
                    "QLabel{background:transparent;border:none;}")
            else:
                card.setStyleSheet(
                    f"QFrame{{background:{theme.ROW_BG};"
                    f"border:1px solid {theme.BORDER};border-radius:10px;}}"
                    f"QFrame:hover{{border-color:{theme.ACCENT};}}"
                    "QLabel{background:transparent;border:none;}")

    def _confirm(self):
        if self._selected_idx < 0:
            return
        self.result = dict(ea.DNSCRYPT_RESOLVERS[self._selected_idx])
        self.accept()


# ════════════════════════════════════════════════════════════════════════════
#  Перезапуск после смены темы
# ════════════════════════════════════════════════════════════════════════════
class ThemeRestartDialog(QDialog):
    """Тема применится только после перезапуска — спрашиваем, делать ли это сейчас."""

    def __init__(self, theme_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Смена темы")
        self.setModal(True)
        self.setFixedWidth(460)
        _style_dialog(self)
        self.setStyleSheet(
            f"QDialog{{background:{theme.BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};}}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        root.addWidget(_title_label("Перезапустить UmbraNet?"))

        body = QLabel(
            f"Тема «{theme_name}» сохранится сразу, но в окне она появится "
            f"только после перезапуска приложения.\n\n"
            f"Перезагрузить UmbraNet сейчас?"
        )
        body.setWordWrap(True)
        body.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;"
        )
        root.addWidget(body)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        cancel = _ghost_btn("Отмена")
        cancel.clicked.connect(self.reject)
        restart = _accent_btn("Перезагрузить", theme.ACCENT)
        restart.setDefault(True)
        restart.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addStretch()
        btns.addWidget(restart)
        root.addLayout(btns)
