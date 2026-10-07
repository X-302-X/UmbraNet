"""
UmbraNet - AI-стратегии.

Современная вкладка для управления DPI-стратегиями:
  • показывает JSON-файлы из UmbraNet/strategies аккуратным списком;
  • ЛКМ — выбор, двойной ЛКМ — сделать активной;
  • ПКМ — контекстное меню;
  • создаёт, удаляет, переименовывает и описывает Uz-стратегии.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.widgets.rounded_panel import RoundedPanel
from umbranet.engine_adapter import (
    DPI_STRATEGY_LIMIT,
    dpi_strategy_delete,
    dpi_strategy_duplicate,
    dpi_strategy_items,
    dpi_strategy_set_active,
    dpi_strategy_update_meta,
    get_engine,
    get_strategies_dir,
)

MAX_STRATEGIES_VISIBLE = DPI_STRATEGY_LIMIT


def _rgba(color: str, alpha: int) -> str:
    """Возвращает rgba() для hex-цветов темы; безопасно падает в transparent."""
    c = QColor(color)
    if not c.isValid():
        return "transparent"
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"

def _rgba_qc(color: str, alpha: int) -> str:
    """rgba c alpha 0-1 для theme.qc / RoundedPanel (иначе qc клампит 18→1.0→непрозрачно)."""
    c = QColor(color)
    if not c.isValid():
        return "transparent"
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha/255:.3f})"


def _card(title: str = "") -> tuple[QWidget, QVBoxLayout]:
    f = RoundedPanel(theme.CARD, theme.BORDER, radius=16)
    # QLabel прозрачность — через отдельный стиль
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(12)
    if title:
        t = QLabel(title)
        t.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:800;background:transparent;border:none;")
        lay.addWidget(t)
    return f, lay


# Технические строки прогресса → короткий статус простым языком.
# Полные диагностические сообщения остаются в журнале внизу окна.
_STEP_RULES: tuple[tuple[str, str], ...] = (
    ("probes youtube/discord", "Параллельно: YouTube + голосовые функции Discord"),
    ("проверка youtube", "Проверяем YouTube"),
    ("voice_readiness", "Проверяем голосовые функции Discord"),
    ("запуск winws", "Запускаем тестовый обход"),
    ("winws запущен", "Проверяем работу обхода"),
    ("winws.exe сообщает", "Диагностируем запуск DPI-движка"),
    ("зачищены зависшие", "Очищаем временный запуск"),
    ("cleanup", "Очищаем временный запуск"),
    ("не остановился", "Завершаем предыдущий запуск"),
    ("остановлена до запуска", "Проверяем условия запуска"),
    ("отменена пользователем", "Завершаем отмену"),
    ("внимание", "Проверяем сетевое окружение"),
    ("список истины", "Готовим список сайтов для проверки"),
    ("подготовлено вариантов", "Подготовили варианты для теста"),
    ("controlled session подготовлена", "Начинаем проверку вариантов"),
    ("рабочий вариант не найден", "Сравниваем результаты проверки"),
    ("пропущена", "Пропускаем этот вариант"),
    ("создана стратегия", "Сохраняем новую стратегию"),
    ("подготовлена", "Подготовка завершена"),
    ("остановлен", "Готовим следующий вариант"),
)

# Большой статус — короткий, чтобы не прыгала высота над шкалой прогресса.
_STEP_MAX_CHARS = 72


def human_step(text: str) -> str:
    """Переводит техническую строку в короткое описание текущего действия."""
    import re

    raw = str(text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    pair = re.search(r"(\d+)\s*/\s*(\d+)", raw)
    score = re.search(r"score\s+(\d+)", raw, re.IGNORECASE)

    if pair and low.startswith("проверка uz"):
        index, total = pair.groups()
        if score:
            return f"Результат проверки: score {score.group(1)}"
        return f"Проверяем стратегию {index} из {total}"
    if pair and "вариант" in low:
        index, total = pair.groups()
        if score:
            return f"Результат варианта: score {score.group(1)}"
        return f"Проверяем вариант {index} из {total}"

    for marker, label in _STEP_RULES:
        if marker in low:
            return label
    if score:
        return f"Оцениваем результат: score {score.group(1)}"
    return "Выполняем проверку"


class AiGenerationProgressDialog(QDialog):
    """Небольшое окно прогресса controlled AI-генерации."""
    cancelRequested = Signal()

    def __init__(self, parent=None, total_variants: int = 0, time_limit: str | int = "—",
                 window_title: str = "AI-генерация Uz",
                 title_text: str = "AI-генерация запущена",
                 subtitle_text: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.setModal(False)
        self.resize(680, 560)
        self.setMinimumSize(600, 500)
        self._total = max(1, int(total_variants or 1))
        self._best_score = None
        # Состояние оценки времени: считаем сами по факту, а не берём из плана.
        self._clock = time.monotonic          # отдельным атрибутом — удобно в тестах
        self._started_at: float | None = None          # старт первого варианта
        self._item_started_at: float | None = None     # старт текущего варианта
        self._items_done = 0                            # сколько уже завершено
        self._current_index = 0
        self._finished = False
        self._step_full_text = ""
        self.setStyleSheet(f"QDialog{{background:{theme.BG};}}")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        card = RoundedPanel(theme.CARD, theme.BORDER, radius=18)
        card.setObjectName("confirmCard")
        card.setStyleSheet("QLabel{background:transparent;border:none;}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon = QLabel("🧪")
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            f"background:{theme.grad(theme.ACCENT, theme.ACCENT2)};color:{theme.WHITE};"
            "border-radius:15px;font-size:22px;border:none;"
        )
        top.addWidget(icon)
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        self._title = QLabel(title_text)
        self._title.setStyleSheet(f"color:{theme.TEXT};font-size:18px;font-weight:900;background:transparent;border:none;")
        # ВАЖНО: про «лимит сессии» здесь не пишем. В окне подтверждения уже
        # сказано «от 1 до 10 минут», и второй, другой лимит (180 сек) только
        # путал: пользователь видел два разных обещания по времени. Вместо этого
        # ниже живёт оценка «сколько примерно осталось» — она считается по факту.
        # Параметр time_limit оставлен для совместимости вызовов.
        # Подзаголовок нейтральный: конкретные сервисы показываем только в
        # статусе фактического этапа проверки, а не как обещание о назначении
        # обхода. Полные подробности всегда остаются в журнале ниже.
        self._subtitle = QLabel(
            subtitle_text or "Пожалуйста, потерпите: идёт создание Uz"
        )
        self._subtitle.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;font-weight:600;background:transparent;border:none;")
        # Длинную строку не обрезаем «в никуда»: сокращаем многоточием под ширину
        # окна (см. _apply_step_text). Иначе на узком окне текст просто пропадал.
        self._subtitle_full = self._subtitle.text()
        title_box.addWidget(self._title)
        title_box.addWidget(self._subtitle)
        top.addLayout(title_box, 1)
        lay.addLayout(top)

        # Главный статус текущего действия — крупный и цветовой:
        # жёлтый в процессе, зелёный при успехе, красный при ошибке.
        self._activity_state = "working"
        self._step = QLabel()
        self._step.setObjectName("activityStatus")
        self._step.setAlignment(Qt.AlignCenter)
        self._step.setWordWrap(False)
        self._step.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._step.setFixedHeight(66)
        lay.addWidget(self._step)
        self._set_activity_status("Подготавливаем проверку", "working")

        self._progress = QProgressBar()
        self._progress.setRange(0, self._total)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat(f"0 / {self._total}")
        self._progress.setFixedHeight(18)
        self._progress.setStyleSheet(
            f"QProgressBar{{background:{theme.INPUT_BG};border:1px solid {theme.BORDER};"
            "border-radius:9px;text-align:center;color:" + theme.TEXT + ";font-size:10px;font-weight:800;}"
            f"QProgressBar::chunk{{background:{theme.grad(theme.ACCENT, theme.ACCENT2)};border-radius:8px;}}"
        )
        lay.addWidget(self._progress)

        # Оценка «сколько осталось»: считается по факту прошедшего времени, а не
        # берётся из плана (план врёт: время одного варианта зависит от сети).
        self._eta = QLabel(self._eta_idle_text())
        self._eta.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;font-weight:700;background:transparent;border:none;")
        lay.addWidget(self._eta)

        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._update_eta)

        self._best = QLabel("Лучший результат: —")
        self._best.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(self._best)

        # Журнал намеренно в нижней части окна: сначала видны действие,
        # прогресс и ETA; технические подробности можно раскрыть/скопировать ниже.
        log_header = QHBoxLayout()
        log_header.setSpacing(8)
        self._log_title = QLabel("Подробный журнал")
        self._log_title.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:11px;font-weight:800;background:transparent;border:none;"
        )
        log_header.addWidget(self._log_title)
        log_header.addStretch(1)
        self._btn_copy_logs = QPushButton("Копировать логи")
        self._btn_copy_logs.setCursor(Qt.PointingHandCursor)
        self._btn_copy_logs.setFixedHeight(28)
        self._btn_copy_logs.setEnabled(False)
        self._btn_copy_logs.setStyleSheet(
            f"QPushButton{{background:{_rgba(theme.ACCENT, 12)};color:{theme.TEXT};"
            f"border:1px solid {_rgba(theme.ACCENT, 70)};border-radius:9px;padding:0 11px;font-size:11px;font-weight:800;}}"
            f"QPushButton:hover{{background:{_rgba(theme.ACCENT, 24)};border-color:{theme.ACCENT};}}"
            f"QPushButton:disabled{{color:{theme.MUTED};background:{_rgba(theme.WHITE, 4)};border-color:{theme.BORDER};}}"
        )
        self._btn_copy_logs.clicked.connect(self._copy_logs)
        log_header.addWidget(self._btn_copy_logs)
        lay.addLayout(log_header)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(96)
        self._log.setStyleSheet(
            f"QPlainTextEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:12px;padding:10px;"
            "font-family:Consolas;font-size:11px;}" + theme.scrollbar_qss()
        )
        lay.addWidget(self._log, 1)

        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1400)
        self._copy_feedback_timer.timeout.connect(self._restore_copy_logs_button)
        root.addWidget(card, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._btn_cancel = QPushButton("Отменить")
        self._btn_cancel.setCursor(Qt.PointingHandCursor)
        self._btn_cancel.setMinimumHeight(36)
        self._btn_cancel.setStyleSheet(
            f"QPushButton{{background:{_rgba(theme.RED, 18)};color:{theme.RED};"
            f"border:1px solid {_rgba(theme.RED, 90)};border-radius:11px;padding:0 18px;font-weight:800;}}"
            f"QPushButton:hover{{background:{_rgba(theme.RED, 32)};color:{theme.TEXT};}}"
            "QPushButton:disabled{color:#777;border-color:#444;background:rgba(255,255,255,0.05);}"
        )
        self._btn_cancel.clicked.connect(self._request_cancel)
        buttons.addWidget(self._btn_cancel)
        self._btn_close = QPushButton("Скрыть")
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.setMinimumHeight(36)
        self._btn_close.setStyleSheet(
            f"QPushButton{{background:{_rgba(theme.WHITE, 8)};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:11px;padding:0 18px;font-weight:700;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.TEXT};}}"
        )
        self._btn_close.clicked.connect(self.hide)
        buttons.addWidget(self._btn_close)
        root.addLayout(buttons)

    def _set_activity_status(self, text: str, state: str = "working") -> None:
        """Обновляет крупный статус и его цвет (running / success / error)."""
        palette = {
            "working": theme.YELLOW,
            "success": theme.GREEN,
            "error": theme.RED,
            "cancelled": theme.YELLOW,
        }
        color = palette.get(state, theme.YELLOW)
        self._activity_state = state
        self._step_full_text = str(text or "")
        self._step.setStyleSheet(
            f"QLabel#activityStatus{{color:{color};background:{_rgba(color, 15)};"
            f"border:1px solid {_rgba(color, 88)};border-radius:14px;"
            "padding:9px 14px;font-size:18px;font-weight:900;}"
        )
        self._apply_step_text()

    def _copy_logs(self) -> None:
        text = self._log.toPlainText()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self._btn_copy_logs.setText("Скопировано")
        self._copy_feedback_timer.start()

    def _restore_copy_logs_button(self) -> None:
        self._btn_copy_logs.setText("Копировать логи")

    def _request_cancel(self):
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setText("Отмена...")
        self.append("AI-генерация: пользователь запросил отмену")
        self._set_activity_status("Отменяем и останавливаем текущую проверку", "cancelled")
        self.cancelRequested.emit()

    # ── Оценка времени до конца ──────────────────────────────────────────────
    def _eta_idle_text(self) -> str:
        return "Осталось примерно: считаем после первого варианта"

    @staticmethod
    def _format_left(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        if total < 60:
            return f"~{total} с"
        minutes, sec = divmod(total, 60)
        if minutes >= 60:
            return "больше часа"
        if minutes >= 10 or not sec:
            return f"~{minutes} мин"
        return f"~{minutes} мин {sec} с"

    def _mark_item_started(self, index: int, total: int) -> None:
        now = self._clock()
        if self._started_at is None:
            self._started_at = now
            self._eta_timer.start()
        # Новый номер в прогрессе означает, что предыдущий элемент закончился:
        # именно так считаем среднее время одного варианта по факту.
        self._items_done = max(self._items_done, index - 1)
        if index != self._current_index:
            self._current_index = index
            self._item_started_at = now

    def _mark_item_done(self, index: int) -> None:
        self._items_done = max(self._items_done, index)
        self._item_started_at = self._clock()

    def _estimate_left(self) -> float | None:
        """Сколько ещё примерно ждать. None — данных пока мало."""
        if self._finished or self._started_at is None or self._items_done <= 0:
            return None
        now = self._clock()
        # Среднее берём по ЗАВЕРШЁННЫМ вариантам (время до старта текущего),
        # а не вместе с текущим: иначе затянувшийся вариант раздувал среднее и
        # «осталось» вдруг росло вместо того, чтобы уменьшаться.
        anchor = self._item_started_at if self._item_started_at is not None else now
        per_item = max(0.0, (anchor - self._started_at) / self._items_done)
        left = per_item * (self._total - max(0, self._items_done))
        # Учитываем, что часть текущего варианта уже прошла: без этого оценка
        # «застывала» бы на среднем значении и не отражала прогресс.
        if self._items_done < self._total and self._item_started_at is not None:
            left -= max(0.0, now - self._item_started_at)
        return max(0.0, left)

    def _update_eta(self) -> None:
        if self._finished:
            return
        left = self._estimate_left()
        if left is None:
            self._eta.setText(self._eta_idle_text())
            return
        # «примерно» уже сказано словами, поэтому тильду из формата убираем —
        # иначе в строке два знака приблизительности подряд.
        # Счётчик «готово X из Y» живёт ТОЛЬКО в основной шкале (2026-10-06,
        # пожелание пользователя): в строке с временем он отставал на 1 и путал.
        human = self._format_left(left).lstrip("~")
        self._eta.setText(f"Осталось примерно: {human}")

    # ── Подпись шага ─────────────────────────────────────────────────────────
    @staticmethod
    def _elide(label: QLabel, text: str, padding: int, fallback_chars: int) -> str:
        """Сокращает текст многоточием под ширину виджета (или по символам)."""
        width = label.width() - padding
        if width > 120:
            text = QFontMetrics(label.font()).elidedText(text, Qt.ElideRight, width)
        elif len(text) > fallback_chars:
            # Окно ещё не показано и настоящей ширины нет — режем по символам.
            text = text[:fallback_chars - 1].rstrip() + "…"
        return text

    def _apply_step_text(self) -> None:
        """Показывает короткую подпись шага в одну строку (без «прыжков» шкалы)."""
        text = self._step_full_text
        self._step.setText(
            self._elide(self._step, text, 26, _STEP_MAX_CHARS) if text else ""
        )
        if self._subtitle_full:
            self._subtitle.setText(self._elide(self._subtitle, self._subtitle_full, 4, 90))

    def resizeEvent(self, event):                       # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._apply_step_text()

    def append(self, text: str):
        import re
        text = str(text or "")
        if not text:
            return
        # Большой статус — простой и жёлтый во время работы; полный текст ниже в логе.
        self._set_activity_status(human_step(text), "working")
        m = re.search(r"(?:вариант(?:а)?|стратегия)\s+(\d+)\s*/\s*(\d+)", text, re.IGNORECASE)
        if m:
            cur = int(m.group(1))
            total = int(m.group(2))
            if total != self._total:
                self._total = max(1, total)
                self._progress.setRange(0, self._total)
            self._progress.setValue(max(0, min(cur, self._total)))
            self._progress.setFormat(f"{min(cur, self._total)} / {self._total}")
            self._mark_item_started(cur, total)
        sm = re.search(r"score\s+(\d+)", text, re.IGNORECASE)
        if sm:
            score = int(sm.group(1))
            if self._best_score is None or score > self._best_score:
                self._best_score = score
                self._best.setText(f"Лучший результат: score {score}")
            if m:
                self._mark_item_done(int(m.group(1)))
        self._update_eta()
        self._log.appendPlainText(text)
        self._btn_copy_logs.setEnabled(True)
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())

    def finish(self, result: dict):
        ok = bool(result.get("ok"))
        best = result.get("best") if isinstance(result.get("best"), dict) else {}
        best_score = best.get("score", self._best_score if self._best_score is not None else "—")
        report_lines = [str(x) for x in (result.get("report_lines") or []) if str(x).strip()]
        final_status = ""
        final_state = "working"
        final_eta = "Время выполнения: завершено"

        if result.get("stage") == "strategy_check":
            if ok:
                self._title.setText("Проверка стратегий завершена")
                final_status = "Готово — проверка завершена"
                final_state = "success"
            elif result.get("cancelled"):
                self._title.setText("Проверка отменена")
                final_status = "Проверка отменена пользователем"
                final_state = "cancelled"
                final_eta = "Время выполнения: остановлено"
            else:
                self._title.setText("Не получилось проверить стратегии")
                final_status = "Не получилось — проверка завершилась с ошибкой"
                final_state = "error"
                final_eta = "Время выполнения: без результата"
            self._subtitle.setText("Результаты проверки всех Uz-стратегий")
            self._subtitle_full = self._subtitle.text()
            if ok:
                self._progress.setValue(self._total)
                self._progress.setFormat(f"{self._total} / {self._total}")
            if best:
                self._best.setText(f"Лучший результат: {best.get('strategy_id', '—')} • score {best.get('score', 0)}")
            if result.get("error"):
                self.append(f"Проверка завершена: {result.get('error')}")
            if report_lines:
                self.append("— Итоговый отчёт —")
                for line in report_lines:
                    self.append(line)
        elif ok:
            sid = str(result.get("created_id", ""))
            self._title.setText("AI-генерация завершена")
            self._subtitle.setText(f"Создана стратегия {sid or 'Uz'}")
            self._subtitle_full = self._subtitle.text()
            self._progress.setValue(self._total)
            self._progress.setFormat(f"{self._total} / {self._total}")
            self._best.setText(f"Лучший результат: score {best_score}")
            self.append(str(result.get("message") or f"Создана стратегия {sid}"))
            if report_lines:
                self.append("— Итоговый отчёт —")
                for line in report_lines:
                    self.append(line)
            final_status = f"Готово — стратегия {sid or 'Uz'} создана"
            final_state = "success"
        else:
            reason = str(result.get("reason_text") or result.get("error") or result.get("reason") or "рабочая стратегия не найдена")
            preflight = result.get("preflight") if isinstance(result.get("preflight"), dict) else {}
            if result.get("cancelled"):
                self._title.setText("AI-генерация отменена")
                final_status = "Отменено пользователем — Uz не создана"
                final_state = "cancelled"
                final_eta = "Время выполнения: остановлено"
            elif preflight.get("abort"):
                self._title.setText("Генерация остановлена до запуска")
                lower_reason = reason.lower()
                if "порта 53" in lower_reason:
                    final_status = "Не получилось — занят локальный порт 53"
                elif "windivert" in lower_reason:
                    final_status = "Не получилось — конфликт WinDivert"
                else:
                    final_status = "Не получилось начать генерацию"
                final_state = "error"
                final_eta = "Время выполнения: генерация не запущена"
            elif result.get("reason") in ("below_threshold", "required_probes_failed", "no_variants"):
                self._title.setText("Подходящая стратегия не найдена")
                final_status = "Не получилось — варианты не прошли проверки"
                final_state = "error"
                final_eta = "Время выполнения: стратегия не создана"
            else:
                self._title.setText("AI-генерация завершилась ошибкой")
                final_status = "Не получилось — Uz не создана"
                final_state = "error"
                final_eta = "Время выполнения: стратегия не создана"
            self._subtitle.setText("Uz не создана")
            self._subtitle_full = self._subtitle.text()
            self._best.setText(f"Лучший результат: score {best_score}")
            self.append(f"Uz не создана: {reason} • лучший score: {best_score}")
            if report_lines:
                self.append("— Итоговый отчёт —")
                for line in report_lines:
                    self.append(line)

        self._finished = True
        self._set_activity_status(final_status, final_state)
        try:
            self._eta_timer.stop()
        except Exception:
            pass
        self._eta.setText(final_eta)
        self._btn_close.setText("Закрыть")
        if hasattr(self, "_btn_cancel"):
            self._btn_cancel.setEnabled(False)
            self._btn_cancel.setText("Отмена")
        try:
            self._btn_close.clicked.disconnect()
        except Exception:
            pass
        self._btn_close.clicked.connect(self.accept)
        self.show()
        self.raise_()
        self.activateWindow()
        # Отчёт остаётся открытым; при необходимости его можно быстро скопировать.


class StrategyLabView(QWidget):
    generationRequested = Signal()
    generationCancelRequested = Signal()
    strategyCheckRequested = Signal()
    strategyCheckCancelRequested = Signal()

    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self._selected_id: str = ""
        self._flash_id: str = ""  # временная подсветка только что созданной стратегии
        self._generation_dialog: AiGenerationProgressDialog | None = None
        self._rows: dict[str, QFrame] = {}
        self._items: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        # ── Hero / быстрые действия ───────────────────────────────────────────
        head_card, head_lay = _card("")
        head_row = QHBoxLayout()
        head_row.setSpacing(14)

        icon = QLabel("🧪")
        icon.setFixedSize(50, 50)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            f"background:{theme.grad(theme.ACCENT, theme.ACCENT2)};"
            f"color:{theme.WHITE};border-radius:16px;font-size:24px;border:none;"
        )
        # glow убран — QGraphicsDropShadowEffect тормозил живой ресайз (-3 мс/кадр)
        head_row.addWidget(icon)

        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("AI-стратегии")
        title.setStyleSheet(f"color:{theme.TEXT};font-size:24px;font-weight:900;background:transparent;border:none;")
        subtitle = QLabel("Библиотека Uz-профилей для DPI/Combo: выбери, настрой и активируй")
        # Перенос и разрешение сжиматься: без этого длинный текст задавал
        # минимальную ширину вкладки (~400 px только на одну эту строку), а
        # вместе с ней и минимальный размер всего окна.
        subtitle.setWordWrap(True)
        subtitle.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        subtitle.setMinimumWidth(120)
        subtitle.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;font-weight:600;background:transparent;border:none;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        head_row.addLayout(title_box, 1)

        stat_box = QHBoxLayout()
        stat_box.setSpacing(8)
        self._stat_total = self._metric("Всего", "—", theme.ACCENT)
        self._stat_ready = self._metric("Заполнено", "—", theme.ACCENT2)
        self._stat_active = self._metric("Активная", "—", theme.GREEN)
        stat_box.addWidget(self._stat_total)
        stat_box.addWidget(self._stat_ready)
        stat_box.addWidget(self._stat_active)
        head_row.addLayout(stat_box)
        head_lay.addLayout(head_row)
        outer.addWidget(head_card)

        # ── Панель генератора ────────────────────────────────────────────────
        intro, il = _card("⚡ Генератор и управление")
        desc = QLabel(
            "Стратегии хранятся как JSON в папке UmbraNet/strategies. "
            "Список ниже оставлен привычным: ЛКМ выбирает строку, двойной ЛКМ активирует, "
            "ПКМ открывает меню. Внешний вид теперь не похож на скучную папку — это библиотека профилей."
        )
        desc.setWordWrap(True)
        desc.setFixedHeight(36)
        try:
            from PySide6.QtWidgets import QSizePolicy as _SP
            desc.setSizePolicy(_SP.Preferred, _SP.Fixed)
        except Exception:
            pass
        desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;line-height:145%;background:transparent;border:none;")
        il.addWidget(desc)

        btns = QHBoxLayout()
        btns.setSpacing(9)
        self._btn_create = self._grad_btn("Сгенерировать Uz", theme.ACCENT, theme.ACCENT2, self._create_strategy)
        self._btn_check_all = self._flat_btn("🔍 Проверить все Uz", self._check_all_strategies)
        self._btn_delete = self._flat_btn("🗑 Удалить выбранную", self._delete_selected)
        self._btn_delete.setEnabled(False)
        self._btn_copy_path = self._flat_btn("📋 Скопировать путь", self._copy_selected_path)
        self._btn_copy_path.setEnabled(False)
        btns.addWidget(self._btn_create)
        btns.addWidget(self._btn_check_all)
        btns.addWidget(self._btn_delete)
        btns.addWidget(self._btn_copy_path)
        btns.addStretch()
        il.addLayout(btns)

        self._status = QLabel("—")
        self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        il.addWidget(self._status)
        outer.addWidget(intro)

        # ── Современная библиотека стратегий ─────────────────────────────────
        list_card, ll = _card("")
        # Библиотека уже в RoundedPanel (через _card), градиент QSS убран для живого ресайза
        list_card.setObjectName("libraryCard")
        list_card.setStyleSheet("QLabel{background:transparent;border:none;}")

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)
        accent = QWidget()
        accent.setFixedSize(4, 28)
        accent.setStyleSheet(f"background:{theme.grad(theme.ACCENT, theme.ACCENT2, horizontal=False)};border-radius:2px;")
        list_title = QLabel("Библиотека Uz")
        list_title.setStyleSheet(f"color:{theme.TEXT};font-size:16px;font-weight:900;background:transparent;border:none;")
        self._list_summary = QLabel("—")
        self._list_summary.setStyleSheet(f"color:{theme.MUTED};font-size:12px;background:transparent;border:none;")
        self._selected_label = QLabel("Выбрано: —")
        self._selected_label.setStyleSheet(
            f"color:{theme.ACCENT2};font-size:12px;font-weight:800;"
            f"background:{_rgba(theme.ACCENT2, 20)};border:1px solid {_rgba(theme.ACCENT2, 70)};"
            "border-radius:10px;padding:5px 10px;"
        )
        hint = QLabel("ЛКМ — выбрать  •  двойной ЛКМ — активировать  •  ПКМ — меню")
        hint.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;")
        # Подсказка по управлению занимает ~330 px и в узком окне задавала
        # минимальную ширину всей вкладки. Прячем её, когда места мало (см.
        # _apply_layout_mode) — управление всё равно видно из подсказок строк.
        self._hint_label = hint
        top.addWidget(accent)
        top.addWidget(list_title)
        top.addWidget(self._list_summary, 1)
        top.addWidget(hint)
        top.addWidget(self._selected_label)
        ll.addLayout(top)

        self._list_scroll = QScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setFrameShape(QFrame.NoFrame)
        self._list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # Минимум списка маленький: иначе он диктует минимальную ВЫСОТУ всей
        # вкладки (было 360 + две карточки сверху = страница не могла стать ниже
        # 750 px, и это мешало окну ужиматься под половину экрана).
        self._list_scroll.setMinimumHeight(200)
        self._list_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}"
            + theme.scrollbar_qss()
        )
        self._list_holder = QWidget()
        try:
            self._list_holder.setAttribute(Qt.WA_StaticContents, True)
        except Exception:
            pass
        self._list_holder.setStyleSheet("background:transparent;border:none;")
        self._list = QVBoxLayout(self._list_holder)
        self._list.setContentsMargins(0, 8, 2, 0)
        self._list.setSpacing(9)
        self._list_scroll.setWidget(self._list_holder)
        ll.addWidget(self._list_scroll, 1)
        outer.addWidget(list_card, 1)

        self.refresh()

    # ── Виджеты / стили ──────────────────────────────────────────────────────
    def _metric(self, label: str, value: str, color: str) -> QWidget:
        # RoundedPanel вместо QSS — без маски скругления на каждый кадр
        # _rgba даёт int 0-255 для QSS, а qc нужен 0-1 → используем _rgba_qc
        box = RoundedPanel(_rgba_qc(color, 18), _rgba_qc(color, 62), radius=14)
        box.setObjectName("metric")
        box.setMinimumWidth(76)
        box.setStyleSheet("QLabel{background:transparent;border:none;}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(1)
        v = QLabel(value)
        v.setObjectName("metricValue")
        v.setAlignment(Qt.AlignCenter)
        v.setStyleSheet(f"color:{color};font-size:16px;font-weight:900;background:transparent;border:none;")
        l = QLabel(label)
        l.setAlignment(Qt.AlignCenter)
        l.setStyleSheet(f"color:{theme.MUTED};font-size:10px;font-weight:700;background:transparent;border:none;")
        lay.addWidget(v)
        lay.addWidget(l)
        return box

    def _set_metric(self, box: QFrame, value: str) -> None:
        lbl = box.findChild(QLabel, "metricValue")
        if lbl:
            lbl.setText(value)

    def _grad_btn(self, text, c1, c2, slot):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(36)
        b.setStyleSheet(
            f"QPushButton{{background:{theme.grad(c1, c2)};color:{theme.WHITE};"
            "border:none;border-radius:12px;font-weight:800;padding:0 14px;text-align:center;}"
            f"QPushButton:hover{{background:{theme.grad(c2, c1)};}}"
            f"QPushButton:disabled{{background:{theme.CARD};color:{theme.MUTED};border:1px solid {theme.BORDER};}}"
        )
        # glow убран для живого ресайза
        b.clicked.connect(slot)
        return b

    def _flat_btn(self, text, slot):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(36)
        b.setStyleSheet(
            f"QPushButton{{background:{_rgba(theme.WHITE, 10)};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:12px;padding:0 14px;font-weight:700;}}"
            f"QPushButton:hover{{background:{_rgba(theme.ACCENT, 22)};border-color:{_rgba(theme.ACCENT, 120)};color:{theme.TEXT};}}"
            f"QPushButton:disabled{{color:{theme.MUTED};background:{_rgba(theme.WHITE, 5)};border-color:{theme.BORDER};}}"
        )
        b.clicked.connect(slot)
        return b

    def _clear_list(self):
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w:
                # ВАЖНО: не отцепляем видимый QWidget как top-level окно.
                # На Windows/PySide setParent(None) у видимого виджета может на
                # долю секунды показать маленькое отдельное окно при refresh()
                # вкладки/смене режима. Сначала скрываем, потом удаляем.
                w.hide()
                w.deleteLater()
        self._rows.clear()

    def _short_path(self, path: str) -> str:
        try:
            p = Path(path)
            return f".../{p.parent.name}/{p.name}"
        except Exception:
            return path

    def _file_name(self, path: str, sid: str) -> str:
        try:
            return Path(path).name
        except Exception:
            return f"{sid}.json"

    def _state_text_color(self, active: bool, args_count: int) -> tuple[str, str]:
        if active:
            return "АКТИВНА", theme.GREEN
        if args_count > 0:
            return "ГОТОВА", theme.ACCENT2
        return "ПУСТАЯ", theme.YELLOW

    def _score_chip_texts(self, item: dict) -> list[tuple[str, str]]:
        """Компактные chips для AI-сгенерированных стратегий."""
        if not item.get("ai_generated"):
            return []
        chips: list[tuple[str, str]] = []
        score = item.get("score", "")
        if score != "" and score is not None:
            chips.append((f"AI score {score}", theme.ACCENT))
        svc = item.get("service_scores") if isinstance(item.get("service_scores"), dict) else {}
        if "discord" in svc:
            chips.append((f"Discord {svc.get('discord')}", theme.ACCENT3))
        if "youtube" in svc:
            chips.append((f"YouTube {svc.get('youtube')}", theme.RED))
        req = item.get("required") if isinstance(item.get("required"), dict) else {}
        discord_req = req.get("discord") if isinstance(req.get("discord"), dict) else {}
        if discord_req:
            ok = all(bool(v) for v in discord_req.values())
            chips.append((("Voice OK" if ok else "Voice FAIL"), theme.GREEN if ok else theme.YELLOW))
        return chips

    def _make_row(self, item: dict) -> QFrame:
        sid = item.get("id", "")
        active = bool(item.get("active"))
        args_count = int(item.get("args_count", 0) or 0)
        path = str(item.get("path", ""))

        row = QFrame()
        row.setObjectName("strategyRow")
        row.setCursor(Qt.PointingHandCursor)
        row.setProperty("sid", sid)
        # Фиксированная компактная высота: при малом числе стратегий строки
        # не должны растягиваться и превращаться в «толстые карточки».
        row.setFixedHeight(92 if item.get("ai_generated") else 74)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 10, 0)
        lay.setSpacing(0)

        rail = QWidget()
        rail.setObjectName("rail")
        rail.setFixedWidth(5)
        lay.addWidget(rail)

        body = QHBoxLayout()
        body.setContentsMargins(10, 8, 0, 8)
        body.setSpacing(10)
        lay.addLayout(body, 1)

        badge = QLabel(item.get("name", sid)[:3].upper())
        badge.setObjectName("badge")
        badge.setFixedSize(44, 44)
        badge.setAlignment(Qt.AlignCenter)
        body.addWidget(badge)

        texts = QVBoxLayout()
        texts.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        name = QLabel(item.get("name", sid))
        name.setObjectName("name")
        title_row.addWidget(name)
        id_chip = QLabel(sid)
        id_chip.setObjectName("idChip")
        id_chip.setAlignment(Qt.AlignCenter)
        title_row.addWidget(id_chip)
        title_row.addStretch()
        texts.addLayout(title_row)

        if item.get("ai_generated"):
            seed = str(item.get("seed_id") or "seed")
            mutation = str(item.get("mutation") or "base")
            mask = str(item.get("mask_id") or "mask")
            desc_text = f"AI-сгенерирована • {seed} / {mutation} / {mask}"
        else:
            desc_text = item.get("description", "") or "Без описания"
        desc = QLabel(desc_text)
        desc.setObjectName("desc")
        desc.setWordWrap(False)
        desc.setFixedHeight(16)
        texts.addWidget(desc)

        meta = QHBoxLayout()
        meta.setSpacing(5)
        count_chip = QLabel(f"{args_count} арг.")
        count_chip.setObjectName("countChip")
        count_chip.setAlignment(Qt.AlignCenter)
        file_chip = QLabel(self._file_name(path, sid))
        file_chip.setObjectName("fileChip")
        file_chip.setAlignment(Qt.AlignCenter)
        path_chip = QLabel(self._short_path(path))
        path_chip.setObjectName("pathChip")
        path_chip.setAlignment(Qt.AlignCenter)
        meta.addWidget(count_chip)
        file_chip.setVisible(not bool(item.get("ai_generated")))
        path_chip.setVisible(not bool(item.get("ai_generated")))
        meta.addWidget(file_chip)
        meta.addWidget(path_chip)
        for idx, (text, color) in enumerate(self._score_chip_texts(item)):
            chip = QLabel(text)
            chip.setObjectName(f"scoreChip{idx}")
            chip.setProperty("scoreColor", color)
            chip.setAlignment(Qt.AlignCenter)
            meta.addWidget(chip)
        meta.addStretch()
        texts.addLayout(meta)

        body.addLayout(texts, 1)

        side = QVBoxLayout()
        side.setSpacing(0)
        state_txt, _state_color = self._state_text_color(active, args_count)
        state = QLabel(state_txt)
        state.setObjectName("state")
        state.setAlignment(Qt.AlignCenter)
        state.setFixedWidth(76)
        side.addWidget(state)
        side.addStretch()
        body.addLayout(side)

        # Дочерние QLabel/QWidget не должны «съедать» клики:
        # кликабельной остаётся вся строка целиком.
        for child in row.findChildren(QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        row.setContextMenuPolicy(Qt.CustomContextMenu)
        row.customContextMenuRequested.connect(lambda pos, k=sid, r=row: self._show_context_menu(k, r.mapToGlobal(pos)))
        row.mousePressEvent = lambda _e, k=sid: self._select(k)
        row.mouseDoubleClickEvent = lambda _e, k=sid: self._activate_strategy(k)
        self._rows[sid] = row
        return row

    def _render_rows(self):
        for sid, row in self._rows.items():
            selected = sid == self._selected_id
            flashed = sid == self._flash_id
            item = next((x for x in self._items if x.get("id") == sid), {})
            active = bool(item.get("active"))
            args_count = int(item.get("args_count", 0) or 0)
            state_text, state_color = self._state_text_color(active, args_count)

            if flashed:
                border = _rgba(theme.ACCENT3, 225)
                bg = _rgba(theme.ACCENT3, 28)
            elif selected:
                border = _rgba(theme.ACCENT, 150)
                bg = _rgba(theme.ACCENT, 22)
            else:
                border = _rgba(theme.GREEN, 105) if active else theme.BORDER
                bg = _rgba(theme.WHITE, 8)
            hover_bg = _rgba(theme.ACCENT, 18)
            row.setStyleSheet(
                f"QFrame#strategyRow{{background:{bg};border:1px solid {border};border-radius:12px;}}"
                f"QFrame#strategyRow:hover{{background:{hover_bg};border-color:{_rgba(theme.ACCENT, 155)};}}"
                "QLabel{background:transparent;border:none;}"
            )

            rail = row.findChild(QWidget, "rail")
            badge = row.findChild(QLabel, "badge")
            name = row.findChild(QLabel, "name")
            id_chip = row.findChild(QLabel, "idChip")
            state = row.findChild(QLabel, "state")
            desc = row.findChild(QLabel, "desc")
            count_chip = row.findChild(QLabel, "countChip")
            file_chip = row.findChild(QLabel, "fileChip")
            path_chip = row.findChild(QLabel, "pathChip")
            score_chips = [c for c in row.findChildren(QLabel) if str(c.objectName()).startswith("scoreChip")]

            if rail:
                rail_color = theme.ACCENT3 if flashed else (theme.ACCENT if selected else (theme.GREEN if active else "transparent"))
                rail.setStyleSheet(f"background:{rail_color};border-radius:2px;")
            if badge:
                badge_bg = theme.GREEN if active else (
                    theme.ACCENT if args_count else theme.YELLOW
                )
                badge.setStyleSheet(
                    f"background:{badge_bg};color:{theme.text_on_color(badge_bg)};border-radius:14px;"
                    "font-size:13px;font-weight:900;letter-spacing:0.4px;"
                )
            if name:
                name.setStyleSheet(f"color:{theme.TEXT};font-size:14px;font-weight:800;background:transparent;border:none;")
            if id_chip:
                id_chip.setStyleSheet(
                    f"color:{theme.MUTED};background:{_rgba(theme.WHITE, 9)};border:1px solid {theme.BORDER};"
                    "border-radius:8px;padding:2px 7px;font-size:10px;font-weight:800;"
                )
            if state:
                state.setText(state_text)
                state.setStyleSheet(
                    f"color:{state_color};background:{_rgba(state_color, 18)};border:1px solid {_rgba(state_color, 92)};"
                    "border-radius:9px;padding:3px 6px;font-size:10px;font-weight:900;"
                )
            if desc:
                desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;background:transparent;border:none;")
            chip_style = (
                f"color:{theme.MUTED};background:{_rgba(theme.WHITE, 8)};border:1px solid {theme.BORDER};"
                "border-radius:8px;padding:2px 7px;font-size:10px;font-weight:700;"
            )
            if count_chip:
                count_color = theme.YELLOW if args_count == 0 else theme.ACCENT3
                count_chip.setStyleSheet(
                    f"color:{count_color};background:{_rgba(count_color, 15)};border:1px solid {_rgba(count_color, 65)};"
                    "border-radius:8px;padding:2px 7px;font-size:10px;font-weight:800;"
                )
            if file_chip:
                file_chip.setStyleSheet(chip_style)
            if path_chip:
                path_chip.setStyleSheet(chip_style)
            for chip in score_chips:
                color = chip.property("scoreColor") or theme.ACCENT
                chip.setStyleSheet(
                    f"color:{color};background:{_rgba(color, 16)};border:1px solid {_rgba(color, 78)};"
                    "border-radius:8px;padding:2px 7px;font-size:10px;font-weight:900;"
                )

    # ── Действия ─────────────────────────────────────────────────────────────
    def _select(self, sid: str):
        self._selected_id = sid
        self._btn_delete.setEnabled(bool(sid))
        self._btn_copy_path.setEnabled(bool(sid))
        self._selected_label.setText(f"Выбрано: {sid}" if sid else "Выбрано: —")
        it = self._selected_item() if sid else None
        if it and it.get("ai_generated"):
            svc = it.get("service_scores") if isinstance(it.get("service_scores"), dict) else {}
            parts = []
            if it.get("score") not in ("", None):
                parts.append(f"score {it.get('score')}")
            if "discord" in svc:
                parts.append(f"Discord {svc.get('discord')}")
            if "youtube" in svc:
                parts.append(f"YouTube {svc.get('youtube')}")
            self._status.setText(f"Выбрано: {sid} • " + " • ".join(parts))
        else:
            self._status.setText(f"Выбрано: {sid}")
        self._render_rows()

    def _selected_item(self) -> dict | None:
        for it in self._items:
            if it.get("id") == self._selected_id:
                return it
        return None

    def _activate_strategy(self, sid: str | None = None):
        sid = sid or self._selected_id
        if not sid:
            return
        ok, msg = dpi_strategy_set_active(sid)
        self._status.setText(msg)
        if ok:
            self.refresh()
        else:
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")

    def _rename_selected(self):
        it = self._selected_item()
        if not it:
            return
        text, ok = QInputDialog.getText(self, "Переименовать стратегию", "Новое имя:", text=str(it.get("name", it.get("id", ""))))
        if not ok:
            return
        ok2, msg = dpi_strategy_update_meta(it.get("id", ""), name=text)
        self._status.setText(msg)
        if ok2:
            self.refresh()
        else:
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")

    def _edit_description_selected(self):
        it = self._selected_item()
        if not it:
            return
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Описание стратегии",
            "Описание:",
            str(it.get("description", "")),
        )
        if not ok:
            return
        ok2, msg = dpi_strategy_update_meta(it.get("id", ""), description=text)
        self._status.setText(msg)
        if ok2:
            self.refresh()
        else:
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")

    def _show_context_menu(self, sid: str, global_pos):
        self._select(sid)
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: {theme.CARD}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 10px; padding: 5px; }}"
            f"QMenu::item {{ padding: 8px 24px 8px 12px; border-radius: 7px; font-size: 12px; }}"
            f"QMenu::item:selected {{ background-color: {_rgba(theme.ACCENT, 75)}; color: {theme.WHITE}; }}"
            f"QMenu::item:disabled {{ color: {theme.MUTED}; }}"
            f"QMenu::separator {{ height:1px; background:{theme.BORDER}; margin:5px 6px; }}"
        )
        it = self._selected_item() or {}
        is_active = bool(it.get("active"))
        act_active = QAction("✓ Уже активна" if is_active else "✓ Сделать активной", self)
        act_active.setEnabled(not is_active)
        act_duplicate = QAction("⧉ Создать копию", self)
        act_duplicate.setEnabled(len(self._items) < MAX_STRATEGIES_VISIBLE)
        act_rename = QAction("✏ Изменить имя", self)
        act_desc = QAction("📝 Изменить описание", self)
        act_show_file = QAction("📁 Показать в папке", self)
        act_copy = QAction("📋 Скопировать путь", self)
        act_delete = QAction("🗑 Удалить", self)
        act_active.triggered.connect(lambda: self._activate_strategy(sid))
        act_duplicate.triggered.connect(self._duplicate_selected)
        act_rename.triggered.connect(self._rename_selected)
        act_desc.triggered.connect(self._edit_description_selected)
        act_show_file.triggered.connect(self._show_selected_in_folder)
        act_copy.triggered.connect(self._copy_selected_path)
        act_delete.triggered.connect(self._delete_selected)
        menu.addAction(act_active)
        menu.addSeparator()
        menu.addAction(act_duplicate)
        menu.addAction(act_rename)
        menu.addAction(act_desc)
        menu.addSeparator()
        menu.addAction(act_show_file)
        menu.addAction(act_copy)
        menu.addSeparator()
        menu.addAction(act_delete)
        menu.exec(global_pos)

    def _scroll_to_strategy(self, sid: str):
        """Показывает стратегию в видимой области списка после refresh/layout."""
        row = self._rows.get(sid)
        if row:
            self._list_scroll.ensureWidgetVisible(row, 0, 22)

    def _clear_flash(self, sid: str):
        """Снимает временную подсветку, не трогая более новую подсветку."""
        if self._flash_id == sid:
            self._flash_id = ""
            self._render_rows()

    # Пороги «узкого окна» для библиотеки AI-стратегий. Выведены из половины
    # экрана: окно 960 px минус сайдбар и отступы ≈ 750 px доступной ширины.
    # Порог считаем по ДОСТУПНОЙ ширине страницы (окно минус меню): страница с
    # видимыми подсказками требует 965 px, поэтому при 1180-пиксельном окне
    # (≈970 доступно) подсказки на месте, а чуть уже — прячем их, и страница
    # ужимается до 740. Без этого между 740 и 965 px всё равно появлялась
    # горизонтальная прокрутка.
    NARROW_WIDTH = 960        # прячем подсказку по управлению и «Скопировать путь»
    VERY_NARROW_WIDTH = 700   # прячем ещё и сводку по стратегиям

    def _apply_layout_mode(self):
        """Прячет второстепенные надписи в узком окне, чтобы вкладка помещалась.

        Без этого шапка библиотеки требовала ~965 px, и при снапе окна на
        половину экрана появлялась горизонтальная прокрутка. Функциональность не
        страдает: «Скопировать путь» есть в меню строки (ПКМ), а подсказка по
        управлению продублирована в тултипах.
        """
        # ВАЖНО: ширину берём не у себя, а у «хозяина». В узком окне вкладка
        # лежит внутри прокрутки и её собственная ширина равна минимальной
        # (965 px) — по ней «узко/не узко» не определить. Родитель-вьюпорт знает
        # настоящую доступную ширину.
        width = self.width()
        host = self.parentWidget()
        if host is not None and host.width() > 0:
            width = min(width, host.width())
        try:
            width = int(width)
        except Exception:
            return
        hide_hint = width < self.NARROW_WIDTH
        hide_extra = width < self.VERY_NARROW_WIDTH
        for widget, hidden in (
            (getattr(self, "_hint_label", None), hide_hint),
            # Кнопку «Скопировать путь» прячем уже при «половине экрана»: она
            # даёт ~160 px и мешала вкладке влезть в 960-пиксельное окно. Само
            # действие остаётся в меню строки (ПКМ → Скопировать путь).
            (getattr(self, "_btn_copy_path", None), hide_hint),
            (getattr(self, "_list_summary", None), hide_extra),
        ):
            if widget is None:
                continue
            try:
                if widget.isVisible() == hidden:
                    widget.setVisible(not hidden)
            except Exception:
                pass

    def resizeEvent(self, event):                       # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._apply_layout_mode()

    def _set_status_info(self, text: str):
        self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        self._status.setText(text)

    def _set_status_warn(self, text: str):
        self._status.setStyleSheet(f"color:{theme.YELLOW};font-size:12px;background:transparent;border:none;")
        self._status.setText(text)

    def _generation_stop_started(self, was_running: bool):
        if was_running:
            self._set_status_warn("AI-генерация: останавливаем UmbraNet для безопасного подбора...")
        else:
            self._set_status_info("AI-генерация: UmbraNet уже остановлен, можно готовить controlled session.")

    def _generation_busy(self):
        self._set_status_warn("Дождитесь завершения текущей операции UmbraNet, затем запустите генерацию снова.")

    def _generation_plan_ready(self, plan: dict):
        variants = ((plan.get("variants") or {}).get("count") if isinstance(plan.get("variants"), dict) else None) or 0
        session = plan.get("session") if isinstance(plan.get("session"), dict) else {}
        policy = session.get("policy") if isinstance(session.get("policy"), dict) else {}
        max_variants = policy.get("max_variants", variants)
        # Про «лимит сессии» не пишем: в окне подтверждения уже сказано
        # «от 1 до 10 минут», а второй лимит (180 сек) только путал. Здесь —
        # только то, что реально известно: сколько вариантов будет проверено.
        # Сам параметр лимита оставлен и передаётся дальше: он часть подписи
        # окна (совместимость), но в интерфейсе больше не показывается.
        time_limit = policy.get("time_limit_sec", "—")
        self._set_status_info(
            f"AI-генерация подготовлена: UmbraNet остановлен • вариантов: {variants} из {max_variants}"
        )
        if self._generation_dialog:
            try:
                self._generation_dialog.close()
                self._generation_dialog.deleteLater()
            except Exception:
                pass
        self._generation_dialog = AiGenerationProgressDialog(self, total_variants=int(variants or max_variants or 1), time_limit=time_limit)
        self._generation_dialog.cancelRequested.connect(self._generation_cancel_requested)
        self._generation_dialog.append("AI-генерация: controlled session подготовлена")
        self._generation_dialog.show()
        self._generation_dialog.raise_()
        self._generation_dialog.activateWindow()

    def _generation_plan_error(self, message: str):
        self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")
        self._status.setText(f"AI-генерация: {message}")
        if self._generation_dialog:
            self._generation_dialog.finish({"ok": False, "error": message})

    def _generation_progress(self, text: str):
        self._set_status_warn(str(text))
        if self._generation_dialog:
            self._generation_dialog.append(str(text))

    def _generation_cancel_requested(self):
        self._set_status_warn("AI-генерация: отмена запрошена, останавливаем DPI-runtime...")
        self.generationCancelRequested.emit()

    def _generation_finished(self, result: dict):
        if self._generation_dialog:
            self._generation_dialog.finish(result)
        if result.get("ok"):
            sid = str(result.get("created_id", ""))
            msg = str(result.get("message") or f"AI-генерация завершена: {sid}")
            best = result.get("best") if isinstance(result.get("best"), dict) else {}
            score = best.get("score") if best else None
            if score is not None:
                msg = f"{msg} • score {score}"
            self._set_status_info(msg)
            if sid:
                self._focus_created_strategy(sid)
            else:
                self.refresh()
            return
        best = result.get("best") if isinstance(result.get("best"), dict) else {}
        best_score = best.get("score", "—") if best else "—"
        reason = str(result.get("reason_text") or result.get("error") or result.get("reason") or "рабочая стратегия не найдена")
        if result.get("cancelled"):
            self._set_status_warn("AI-генерация отменена. DPI-runtime очищен, Uz не создана.")
        else:
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")
            self._status.setText(f"AI-генерация завершена без создания Uz: {reason} • лучший score: {best_score}")
        self.refresh()

    def _focus_created_strategy(self, sid: str):
        """Выбирает, подсвечивает и прокручивает список к новой/скопированной стратегии."""
        self._selected_id = sid
        self._flash_id = sid
        self.refresh()
        # После перестройки списка Qt должен успеть пересчитать layout,
        # поэтому прокручиваем к новой строке на следующем тике event-loop.
        QTimer.singleShot(0, lambda s=sid: self._scroll_to_strategy(s))
        QTimer.singleShot(120, lambda s=sid: self._scroll_to_strategy(s))
        QTimer.singleShot(1800, lambda s=sid: self._clear_flash(s))

    def _confirm_generation_dialog(self) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("AI-генерация Uz")
        dlg.setModal(True)
        dlg.setMinimumWidth(520)
        dlg.setStyleSheet(f"QDialog{{background:{theme.BG};}}")

        root = QVBoxLayout(dlg)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        card = RoundedPanel(theme.CARD, theme.BORDER, radius=18)
        card.setObjectName("confirmCard")
        card.setStyleSheet("QLabel{background:transparent;border:none;}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon = QLabel("🧪")
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            f"background:{theme.grad(theme.ACCENT, theme.ACCENT2)};color:{theme.WHITE};"
            "border-radius:15px;font-size:22px;border:none;"
        )
        top.addWidget(icon)
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("Запустить AI-генерацию Uz?")
        title.setStyleSheet(f"color:{theme.TEXT};font-size:18px;font-weight:900;background:transparent;border:none;")
        subtitle = QLabel("UmbraNet подготовит controlled session для подбора стратегии.")
        subtitle.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;font-weight:600;background:transparent;border:none;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top.addLayout(title_box, 1)
        lay.addLayout(top)

        text = QLabel(
            "Генерация может занять некоторое время — ориентировочно от 1 до 10 минут.\n\n"
            "Перед началом UmbraNet остановит текущие сетевые процессы, чтобы подбор проходил "
            "в чистом и стабильном режиме."
        )
        text.setWordWrap(True)
        text.setStyleSheet(f"color:{theme.TEXT};font-size:13px;line-height:145%;background:transparent;border:none;")
        lay.addWidget(text)

        warn = QLabel("Если нажать «Нет», окно просто закроется и текущая работа UmbraNet не будет остановлена.")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            f"color:{theme.YELLOW};background:{_rgba(theme.YELLOW, 14)};"
            f"border:1px solid {_rgba(theme.YELLOW, 70)};border-radius:12px;"
            "padding:9px 11px;font-size:12px;font-weight:700;"
        )
        lay.addWidget(warn)
        root.addWidget(card)

        buttons = QHBoxLayout()
        buttons.addStretch()
        no = QPushButton("Нет")
        no.setCursor(Qt.PointingHandCursor)
        no.setMinimumHeight(36)
        no.setStyleSheet(
            f"QPushButton{{background:{_rgba(theme.WHITE, 8)};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:11px;padding:0 18px;font-weight:700;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};color:{theme.TEXT};}}"
        )
        yes = QPushButton("Да, начать")
        yes.setCursor(Qt.PointingHandCursor)
        yes.setMinimumHeight(36)
        yes.setStyleSheet(
            f"QPushButton{{background:{theme.grad(theme.ACCENT, theme.ACCENT2)};color:{theme.WHITE};"
            "border:none;border-radius:11px;padding:0 18px;font-weight:800;}"
            f"QPushButton:hover{{background:{theme.grad(theme.ACCENT2, theme.ACCENT)};}}"
        )
        no.clicked.connect(dlg.reject)
        yes.clicked.connect(dlg.accept)
        buttons.addWidget(no)
        buttons.addWidget(yes)
        root.addLayout(buttons)
        return dlg.exec() == QDialog.Accepted


    def _check_stop_started(self, was_running: bool):
        if was_running:
            self._set_status_warn("Проверка Uz: останавливаем UmbraNet для безопасной проверки...")
        else:
            self._set_status_info("Проверка Uz: UmbraNet уже остановлен, готовим проверку.")

    def _check_plan_ready(self):
        if self._generation_dialog:
            try:
                self._generation_dialog.close()
                self._generation_dialog.deleteLater()
            except Exception:
                pass
        total = max(1, len(dpi_strategy_items()))
        self._generation_dialog = AiGenerationProgressDialog(
            self, total_variants=total, time_limit="—",
            window_title="Проверка Uz",
            title_text="Проверка стратегий запущена",
            subtitle_text="Пожалуйста, потерпите: идёт проверка Uz-стратегий",
        )
        self._generation_dialog.cancelRequested.connect(self._check_cancel_requested)
        self._generation_dialog.append("Проверка Uz: controlled session подготовлена")
        self._generation_dialog.show()
        self._generation_dialog.raise_()
        self._generation_dialog.activateWindow()

    def _check_progress(self, text: str):
        self._set_status_warn(str(text))
        if self._generation_dialog:
            self._generation_dialog.append(str(text))

    def _check_cancel_requested(self):
        self._set_status_warn("Проверка Uz: отмена запрошена, останавливаем DPI-runtime...")
        self.strategyCheckCancelRequested.emit()

    def _check_finished(self, result: dict):
        if self._generation_dialog:
            self._generation_dialog.finish(result)
        if result.get("ok"):
            best = result.get("best") if isinstance(result.get("best"), dict) else {}
            if best:
                self._set_status_info(f"Проверка завершена: лучший {best.get('strategy_id')} • score {best.get('score', 0)}")
            else:
                self._set_status_info("Проверка завершена")
        else:
            self._set_status_warn(f"Проверка Uz завершена: {result.get('error', 'нет результата')}")
        self.refresh()

    def _check_all_strategies(self):
        self._set_status_warn("Проверка всех Uz запрошена...")
        self.strategyCheckRequested.emit()

    def _create_strategy(self):
        if not self._confirm_generation_dialog():
            self._set_status_info("AI-генерация отменена")
            return
        self._set_status_warn("AI-генерация запрошена: подготавливаем остановку UmbraNet...")
        self.generationRequested.emit()

    def _duplicate_selected(self):
        sid = self._selected_id
        if not sid:
            return
        if len(self._items) >= MAX_STRATEGIES_VISIBLE:
            self._status.setText(f"Достигнут лимит: максимум {MAX_STRATEGIES_VISIBLE} стратегий")
            self._status.setStyleSheet(f"color:{theme.YELLOW};font-size:12px;background:transparent;border:none;")
            return
        ok, msg, new_id = dpi_strategy_duplicate(sid)
        self._status.setText(msg)
        if ok:
            self._focus_created_strategy(new_id)
        else:
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")

    def _selected_strategy_file(self) -> Path | None:
        """Абсолютный путь к JSON выбранной стратегии в папке текущего UmbraNet.

        Не доверяем текущей рабочей папке Windows: при запуске через ярлык cwd
        может быть Documents. Основной источник правды — get_strategies_dir().
        """
        it = self._selected_item()
        if not it:
            return None
        sid = str(it.get("id", self._selected_id) or self._selected_id).strip().lower()
        strategies_dir = Path(get_strategies_dir()).resolve()
        candidates: list[Path] = []
        if sid:
            candidates.append(strategies_dir / f"{sid}.json")
        raw = str(it.get("path", "") or "").strip()
        if raw:
            raw_path = Path(raw).expanduser()
            # Если path из старого состояния был относительным, привязываем его к папке приложения,
            # а не к Documents/текущей рабочей папке процесса.
            candidates.append(raw_path if raw_path.is_absolute() else strategies_dir / raw_path.name)
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                if resolved.exists() and resolved.is_file():
                    return resolved
            except Exception:
                continue
        return None

    def _show_selected_in_folder(self):
        it = self._selected_item()
        if not it:
            return
        path = self._selected_strategy_file()
        if not path:
            self._status.setText(f"Файл стратегии не найден: {it.get('id', '')}")
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")
            return
        try:
            import os
            import subprocess
            import sys
            if sys.platform.startswith("win"):
                # Explorer надёжнее принимает /select через одну командную строку.
                # Так он открывает UmbraNet\strategies и выделяет конкретный uzN.json,
                # а не падает в Documents при относительном/неверно распарсенном пути.
                win_path = os.path.normpath(str(path))
                subprocess.Popen(f'explorer.exe /select,"{win_path}"')
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
            self._status.setText(f"Показано в папке: {path.name}")
            self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        except Exception as exc:
            self._status.setText(f"Не удалось открыть папку: {exc}")
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")

    def _delete_selected(self):
        sid = self._selected_id
        if not sid:
            return
        it = self._selected_item() or {}
        if bool(it.get("active")):
            message = (
                f"Стратегия {sid} сейчас активна.\n\n"
                "Если удалить её, UmbraNet автоматически выберет другую доступную стратегию.\n\n"
                "Продолжить?"
            )
        else:
            message = f"Удалить стратегию {sid} из папки strategies?\n\nФайл будет удалён с диска."
        ans = QMessageBox.warning(
            self,
            "Удалить стратегию",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        ok, msg = dpi_strategy_delete(sid)
        self._status.setText(msg)
        if ok:
            self._selected_id = ""
            self.refresh()
        else:
            self._status.setStyleSheet(f"color:{theme.RED};font-size:12px;background:transparent;border:none;")

    def _copy_selected_path(self):
        it = self._selected_item()
        if not it:
            return
        path = self._selected_strategy_file()
        QGuiApplication.clipboard().setText(str(path) if path else str(it.get("path", "")))
        self._btn_copy_path.setText("✓ Скопировано")
        QTimer.singleShot(1300, lambda: self._btn_copy_path.setText("📋 Скопировать путь"))

    def _make_empty_state(self) -> QFrame:
        empty = QFrame()
        empty.setObjectName("emptyState")
        empty.setMinimumHeight(180)
        empty.setStyleSheet(
            f"QFrame#emptyState{{background:{_rgba(theme.WHITE, 7)};border:1px dashed {_rgba(theme.ACCENT, 90)};border-radius:16px;}}"
            "QLabel{background:transparent;border:none;}"
        )
        lay = QVBoxLayout(empty)
        lay.setContentsMargins(18, 26, 18, 26)
        lay.setSpacing(8)
        icon = QLabel("🧪")
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(f"color:{theme.ACCENT2};font-size:34px;background:transparent;border:none;")
        title = QLabel("Стратегий пока нет")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color:{theme.TEXT};font-size:16px;font-weight:900;background:transparent;border:none;")
        text = QLabel("Нажмите «Сгенерировать Uz», чтобы подготовить AI-подбор стратегии.")
        text.setAlignment(Qt.AlignCenter)
        text.setWordWrap(True)
        text.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(icon)
        lay.addWidget(title)
        lay.addWidget(text)
        return empty

    def _items_structure_key(self, items: list[dict]) -> tuple:
        """Ключ структуры списка без active-флага.

        active может меняться при переключении режима/стратегии, но для этого
        не надо удалять и заново создавать строки. Достаточно перерисовать
        существующие row, иначе Qt на Windows может мигать маленьким окном.
        """
        key = []
        for x in items or []:
            svc = x.get("service_scores") if isinstance(x.get("service_scores"), dict) else {}
            req = x.get("required") if isinstance(x.get("required"), dict) else {}
            key.append((
                str(x.get("id", "")),
                str(x.get("name", "")),
                str(x.get("description", "")),
                int(x.get("args_count", 0) or 0),
                str(x.get("path", "")),
                bool(x.get("ai_generated")),
                str(x.get("score", "")),
                tuple(sorted((str(k), str(v)) for k, v in svc.items())),
                tuple(sorted((str(k), str(v)) for k, v in req.items())),
                str(x.get("seed_id", "")),
                str(x.get("mutation", "")),
                str(x.get("mask_id", "")),
            ))
        return tuple(key)

    def _update_summary(self):
        self._btn_delete.setEnabled(bool(self._selected_id))
        self._btn_copy_path.setEnabled(bool(self._selected_id))
        self._selected_label.setText(f"Выбрано: {self._selected_id}" if self._selected_id else "Выбрано: —")
        ready = sum(1 for x in self._items if int(x.get("args_count", 0) or 0) > 0)
        active_item = next((x for x in self._items if x.get("active")), None)
        active_id = str(active_item.get("id", "—")) if active_item else "—"
        self._list_summary.setText(f"Показано: {len(self._items)} / {MAX_STRATEGIES_VISIBLE} • заполнено: {ready}")
        self._set_metric(self._stat_total, str(len(self._items)))
        self._set_metric(self._stat_ready, str(ready))
        self._set_metric(self._stat_active, active_id)

    def refresh(self):
        self.engine = get_engine()
        new_items = dpi_strategy_items()
        new_key = self._items_structure_key(new_items)
        old_key = getattr(self, "_items_structure_cache", None)

        if old_key == new_key and self._rows:
            # Ничего структурно не изменилось — не пересоздаём QWidget-строки.
            # Это убирает микро-окна/мигание при входе во вкладку и смене режима.
            self._items = new_items
            if self._selected_id and not any(x.get("id") == self._selected_id for x in self._items):
                self._selected_id = ""
            self._update_summary()
            self._render_rows()
            self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
            self._status.setText(f"Стратегий: {len(self._items)}" + (f" • выбрано: {self._selected_id}" if self._selected_id else ""))
            return

        self._items = new_items
        self._items_structure_cache = new_key
        self._clear_list()

        if not self._items:
            self._selected_id = ""
            self._list.addWidget(self._make_empty_state())
            self._list.addStretch()
        else:
            if self._selected_id and not any(x.get("id") == self._selected_id for x in self._items):
                self._selected_id = ""
            for item in self._items:
                self._list.addWidget(self._make_row(item))
            self._list.addStretch()

        self._update_summary()
        self._render_rows()
        self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        self._status.setText(f"Стратегий: {len(self._items)}" + (f" • выбрано: {self._selected_id}" if self._selected_id else ""))
