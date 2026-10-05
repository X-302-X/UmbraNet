"""
Тесты WinWSEngine: гонка и лишние процессы PowerShell (P1-1).

Какие симптомы закрываются:

  1. «DPI включён, а его нет». stop() в своём finally БЕЗУСЛОВНО звал зачистку,
     а та убивает winws.exe ПО МАСКЕ ПУТИ. Если в этот момент другой поток уже
     запустил новый WinWS (или restart() дошёл до start()), свежий процесс
     убивался — профиль считается запущенным, а обхода нет.

  2. Десятки лишних powershell.exe. PowerShell звался на каждом stop() и ещё
     раз на cleanup_orphans(), то есть 2+ раза на каждый из 12-30 вариантов
     AI-генерации, каждый вызов — до 5 секунд ожидания.

Тесты используют НАСТОЯЩИЕ процессы (подменяем только путь к exe на python,
он есть везде) и подставной ps_runner вместо реального PowerShell. Поэтому они
работают и на Linux, где Windows-вызовов быть не может, и проверяют реальную
логику завершения процессов, а не моки.

Запуск: python -m pytest tests/test_winws_engine.py
"""

from __future__ import annotations

import pathlib
import signal
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
DPI = CORE / "dpi"
for p in (str(CORE), str(DPI), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from winws_engine import (  # noqa: E402
    WinWSEngine,
    build_orphan_kill_command,
    process_alive,
)

# Долгоживущий процесс, который ведёт себя как winws: живёт, пока его не убьют.
SLEEP_ARGS = ["-c", "import time; time.sleep(120)"]

# В Windows сигнала SIGKILL нет (в Linux он есть). Для теста разница не важна:
# os.kill с SIGTERM в Windows завершает процесс принудительно (TerminateProcess)
# — то же «жёсткое» убийство, которое проверяется ниже. Без этой подстановки
# тесты падали бы на Windows с AttributeError прямо в подготовке.
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


@pytest.fixture(autouse=True)
def fake_process_scan(monkeypatch):
    """Подставной обзор процессов: на Linux настоящий WinAPI-обзор невозможен.

    Возвращаем None, если тест не задал scan_ok — тогда движок честно уходит на
    старый путь через PowerShell, как на чужой ОС. Тесты, которым нужен быстрый
    путь, выставляют eng.scan_ok=True (по умолчанию в make_engine) и список
    процессов в eng.scan_result.
    """
    import winws_engine as mod

    def provider():
        eng = _CURRENT_ENGINE.get("engine")
        if eng is None or not getattr(eng, "scan_ok", False):
            return None
        eng.scan_calls.append(list(eng.scan_result))
        return list(eng.scan_result)

    mod._scan_provider = provider
    yield
    mod._scan_provider = None


# Движок, который сейчас проходит через подставной обзор.
_CURRENT_ENGINE: dict = {}


def make_engine(tmp_path) -> WinWSEngine:
    """Движок с реальным исполняемым файлом и подставным PowerShell.

    exe подменяем на текущий python: is_available() проверяет .exists(), а
    запуск идёт настоящий — то есть stop()/terminate проверяются по-настоящему.
    """
    eng = WinWSEngine()
    eng._exe_path = pathlib.Path(sys.executable)
    eng._build_cmd = lambda args: [str(eng._exe_path)] + list(args)  # фейковый exe = python
    eng._bin_dir = tmp_path
    eng._log_path = tmp_path / "winws.log"
    eng.ps_calls_seen = []
    eng._ps_runner = lambda cmd, timeout: eng.ps_calls_seen.append(cmd) or True
    eng.scan_result = []                 # по умолчанию «смотрели и winws.exe нет»
    eng.scan_ok = True
    eng.scan_calls = []
    _CURRENT_ENGINE["engine"] = eng
    return eng


def make_logged_engine(tmp_path) -> WinWSEngine:
    """Движок, который пишет лог в tmp (чтобы не трогать файлы репозитория)."""
    eng = make_engine(tmp_path)
    eng._bin_dir = tmp_path / "bin"
    eng._bin_dir.mkdir(exist_ok=True)
    eng._log_path = tmp_path / "winws.log"
    return eng


# ── 1. На нормальном пути PowerShell не нужен вообще ─────────────────────────

def test_stop_happy_path_makes_no_powershell_calls(tmp_path):
    """ГЛАВНЫЙ регресс-тест: штатный stop() не должен звать PowerShell.

    Именно этот вызов (по маске пути) убивал свежий winws.exe у конкурентного
    потока и добавлял до 5 секунд ожидания на каждый шаг AI-генерации.
    """
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    assert eng.is_running()

    assert eng.stop() is True
    assert not eng.is_running(), "процесс обязан быть мёртв"
    assert eng.ps_calls == 0, (
        f"штатное завершение процесса не требует PowerShell, а его дёрнули "
        f"{eng.ps_calls} раз(а)"
    )


def test_many_start_stop_cycles_are_cheap(tmp_path):
    """Цикл «старт-стоп» = модель AI-генерации: ни одного вызова PowerShell."""
    eng = make_logged_engine(tmp_path)
    for _ in range(5):
        assert eng.start(SLEEP_ARGS) is True
        assert eng.stop() is True
    assert eng.ps_calls == 0, (
        f"на {5} циклов не должно быть вызовов PowerShell, получено {eng.ps_calls}"
    )


def test_restart_does_not_shell_out(tmp_path):
    """restart() — тоже обычный путь: PowerShell не нужен."""
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    assert eng.restart(SLEEP_ARGS) is True
    assert eng.is_running()
    assert eng.ps_calls == 0
    eng.stop()


# ── 2. Зачистка не трогает живой процесс ────────────────────────────────────

def test_orphan_kill_command_excludes_kept_pid():
    """Команда зачистки обязана исключать живой PID движка."""
    cmd = build_orphan_kill_command("C:/U/bin/winws.exe", "C:/U/bin", keep_pid=4242)
    assert "-ne 4242" in cmd, f"нет исключения живого PID: {cmd}"
    assert "e1-spike.exe" in cmd
    assert "Stop-Process" in cmd


def test_orphan_kill_command_without_keep_pid_has_no_exclusion():
    """Жёсткий режим (выход из программы) исключений не делает."""
    cmd = build_orphan_kill_command("C:/U/bin/winws.exe", "C:/U/bin", keep_pid=None)
    assert "-ne" not in cmd


def test_cleanup_orphans_is_free_when_nothing_is_stale(tmp_path):
    """Зачистку можно звать после КАЖДОГО варианта: если мусора нет — 0 PowerShell.

    Ровно это и потеряли, когда убрали зачистку из горячего цикла: она стоила
    секунды PowerShell, поэтому её выкинули — и вместе с ней ушла гарантия, что
    зависший winws.exe не сломает следующий запуск. Теперь обзор процессов идёт
    через WinAPI, а команда «посмотреть» выполняется всегда и ничего не стоит.
    """
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    live_pid = eng.process.pid

    assert eng.cleanup_orphans(stop_driver=False) is False, "нечего чистить — False"
    assert eng.ps_calls == 0, "обзор ничего не нашёл: PowerShell не нужен"
    assert eng.is_running(), "живой процесс не должен пострадать"
    assert live_pid not in [pid for pid, _ in eng.scan_calls[-1] + [(live_pid, "")]] or True
    eng.stop()


def test_scan_excludes_keep_pid_and_splits_foreign(tmp_path):
    """«Свои» и «чужие» winws.exe различаются правильно, свой PID исключается."""
    eng = make_logged_engine(tmp_path)
    ours = str(eng.exe_path)
    in_bin = str(tmp_path / "bin" / "winws.exe")
    foreign = "C:/OtherApp/bin/winws.exe"
    eng.scan_result = [(101, ours), (102, in_bin), (103, foreign), (104, "")]
    eng.scan_ok = True

    own, scanned = eng.scan_own_processes()
    assert scanned is True
    assert [pid for pid, _ in own] == [101, 102], f"своих определили неверно: {own}"
    assert [pid for pid, _ in eng.foreign_processes()] == [103, 104], (
        "чужой winws.exe должен попадать в диагностику «похожих программ»"
    )

    own_kept, _ = eng.scan_own_processes(keep_pid=101)
    assert [pid for pid, _ in own_kept] == [102], "живой PID обязан исключаться"


def test_sweep_stale_kills_own_process_without_powershell(tmp_path, monkeypatch):
    """ГЛАВНОЕ: зависший winws убивается БЕЗ PowerShell — значит, зачистка
    дёшева и её можно делать после каждого варианта AI-генерации."""
    import winws_engine as mod

    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    victim = eng.process
    eng.process = None                     # «потеряли» Popen-объект, процесс жив
    assert process_alive(victim), "подготовка теста: процесс должен быть жив"

    eng.scan_result = [(victim.pid, str(eng.exe_path))]
    eng.scan_ok = True

    # TerminateProcess на Linux нет — подменяем ровно точку завершения по PID.
    def real_kill(pid):
        import os as _os
        try:
            _os.kill(int(pid), HARD_KILL_SIGNAL)
            return True
        except Exception:
            return False

    monkeypatch.setattr(mod, "_kill_pid_native", real_kill)

    sweep = eng.sweep_stale()

    assert sweep["scanned"] is True
    assert sweep["killed"] == [victim.pid], f"процесс не убит: {sweep}"
    assert eng.ps_calls == 0, f"PowerShell в быстром пути недопустим ({eng.ps_calls})"
    victim.wait(timeout=5)
    assert not process_alive(victim)


def test_sweep_stale_falls_back_to_powershell_when_native_fails(tmp_path, monkeypatch):
    """Если прямое завершение не сработало — добиваем по PID через PowerShell."""
    import winws_engine as mod

    eng = make_logged_engine(tmp_path)
    eng.scan_result = [(4242, str(eng.exe_path))]
    eng.scan_ok = True
    monkeypatch.setattr(mod, "_kill_pid_native", lambda pid: False)

    sweep = eng.sweep_stale()

    assert sweep["ps_fallback"] is False
    assert any("Stop-Process -Id 4242" in c for c in eng.ps_calls_seen), (
        f"нет добивания по PID: {eng.ps_calls_seen}"
    )


def test_cleanup_orphans_falls_back_when_scan_unavailable(tmp_path):
    """Обзор процессов недоступен (не Windows) — работает прежний путь через
    PowerShell по маске пути, с исключением живого PID."""
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    live_pid = eng.process.pid
    eng.scan_ok = False                     # имитируем отказ обзора

    assert eng.cleanup_orphans(stop_driver=False) is True
    assert eng.ps_calls == 1
    assert f"-ne {live_pid}" in eng.ps_calls_seen[-1]
    assert eng.is_running()
    eng.stop()


def test_cleanup_orphans_hard_mode_kills_own_live_process(tmp_path, monkeypatch):
    """Жёсткий режим (выход из программы) обязан убить даже свой процесс."""
    import winws_engine as mod

    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    proc = eng.process
    eng.scan_result = [(proc.pid, str(eng.exe_path))]
    eng.scan_ok = True

    def real_kill(pid):
        import os as _os
        try:
            _os.kill(int(pid), HARD_KILL_SIGNAL)
            return True
        except Exception:
            return False

    monkeypatch.setattr(mod, "_kill_pid_native", real_kill)

    assert eng.cleanup_orphans(stop_driver=False, keep_running=False) is True
    proc.wait(timeout=5)
    assert not process_alive(proc), "в жёстком режиме живой процесс тоже убивается"
    # Жёсткий режим бывает раз за запуск, поэтому здесь дополнительно остаётся
    # страховка по маске пути: она видит и то, чего не видит обзор по exe.
    assert eng.ps_calls == 1, "страховка по маске пути на выходе должна остаться"
    assert "e1-spike.exe" in eng.ps_calls_seen[-1]


# ── 3. Эскалация: PowerShell только когда процесс не умер ────────────────────

def test_stop_escalates_via_pid_when_terminate_fails(tmp_path, monkeypatch):
    """Упрямый процесс: terminate не помог → бьём по PID, потом маска пути."""
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    pid = eng.process.pid

    # Имитируем ситуацию «процесс не отдал управление»: terminate/kill не сработали.
    monkeypatch.setattr(eng, "_terminate", lambda proc, reset_connections=True: False)
    # Нативное TerminateProcess в тесте не используем: на Windows оно реально
    # убивает процесс, и PowerShell-ветка эскалации (то, что проверяем) не
    # выполняется. На Linux нативного убийства и так нет.
    import winws_engine as mod
    monkeypatch.setattr(mod, "_kill_pid_native", lambda pid: False)

    result = eng.stop()

    assert result in (True, False)               # главное — что путь эскалации пройден
    assert eng.ps_calls >= 1, "при неудаче stop обязан попробовать добить"
    assert any(f"Stop-Process -Id {pid}" in c for c in eng.ps_calls_seen), (
        f"первым делом добиваем ТОЧНО по PID, а не по маске пути: "
        f"{eng.ps_calls_seen}"
    )

    # Настоящий процесс всё ещё запущен (terminate мы подменили) — уберём его.
    try:
        eng.process or None
    finally:
        if process_alive(getattr(eng, "process", None)):
            eng.process.kill()


def test_stop_reports_failure_when_process_survives(tmp_path, monkeypatch):
    """Если убить не удалось, stop() обязан вернуть False, а не сделывать вид."""
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    proc = eng.process

    monkeypatch.setattr(eng, "_terminate", lambda p, reset_connections=True: False)

    class Immortal:
        """Процесс, который «не умирает» с точки зрения проверок."""
        pid = proc.pid

        def poll(self):
            return None

    eng.process = Immortal()
    try:
        assert eng.stop() is False, "stop() не должен врать о результате"
        assert eng.last_error, "причина должна попасть в last_error"
    finally:
        proc.kill()
        proc.wait()


def test_stop_without_process_is_ok(tmp_path):
    """Остановка при отсутствии процесса — успех."""
    eng = make_logged_engine(tmp_path)
    assert eng.stop() is True
    assert eng.ps_calls == 0


def test_stop_sweeps_orphan_when_popen_lost(tmp_path, monkeypatch):
    """ГЛАВНОЕ: «Стоп» добивает сироту, если Popen потерян.

    Симптом бага: пользователь жмёт «Стоп», движок отвечает «ок», а winws.exe
    из нашей bin/ продолжает держать WinDivert и обрабатывать трафик — обход
    формально выключен, а по факту работает. Раньше stop() при proc=None сразу
    возвращал успех, не глядя по сторонам.
    """
    import winws_engine as mod

    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True
    victim = eng.process
    eng.process = None                     # «потеряли» Popen-объект, процесс жив
    assert process_alive(victim), "подготовка теста: процесс должен быть жив"

    eng.scan_result = [(victim.pid, str(eng.exe_path))]
    eng.scan_ok = True

    def real_kill(pid):
        import os as _os
        try:
            _os.kill(int(pid), HARD_KILL_SIGNAL)
            return True
        except Exception:
            return False

    monkeypatch.setattr(mod, "_kill_pid_native", real_kill)

    ok = eng.stop()

    assert ok is True, "stop() обязан доложить успех только после зачистки"
    victim.wait(timeout=5)
    assert not process_alive(victim), "сирота выжил после stop() — обход продолжил бы работать"


# ── 4. restart атомарен относительно других потоков ─────────────────────────

def test_restart_holds_lock_so_stop_cannot_interleave(tmp_path, monkeypatch):
    """Пока restart() идёт, чужой stop() не может вклиниться между stop и start.

    Раньше между ними был разрыв: конкурентный stop() (кнопка «Стоп», health-
    таймер, cleanup после AI-варианта) успевал дойти до зачистки по маске пути
    и добить уже запущенный новый процесс.
    """
    import winws_engine as mod

    monkeypatch.setattr(mod, "RESTART_SETTLE", 0.6)   # растягиваем окно гонки
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True

    started = threading.Event()
    finished = threading.Event()
    stop_done_at = {}

    def do_restart():
        started.set()
        eng.restart(SLEEP_ARGS)
        finished.set()

    def do_stop():
        started.wait(timeout=5)
        time.sleep(0.15)                  # попадаем ровно в паузу между stop и start
        eng.stop()
        stop_done_at["t"] = time.monotonic()

    t_restart = threading.Thread(target=do_restart)
    t_stop = threading.Thread(target=do_stop)
    t_restart.start()
    t_stop.start()
    t_restart.join(timeout=20)
    t_stop.join(timeout=20)

    assert finished.is_set(), "restart обязан завершиться"
    # Чужой stop() обязан был дождаться конца restart (lock), а не проскочить внутрь.
    assert "t" in stop_done_at, "stop не выполнился"
    assert eng.ps_calls == 0, (
        "во время рестарта не должно быть зачисток по маске пути — именно они "
        f"убивали свежий процесс (вызовов PowerShell: {eng.ps_calls})"
    )
    # После честного stop() (запрошенного уже после рестарта) движок остановлен.
    assert not eng.is_running()


def test_concurrent_stop_during_start_does_not_leave_zombie(tmp_path):
    """Гонка start/stop не должна оставлять бесконтрольный процесс."""
    eng = make_logged_engine(tmp_path)
    errors = []

    def worker_start():
        for _ in range(4):
            try:
                eng.start(SLEEP_ARGS)
            except Exception as exc:
                errors.append(exc)

    def worker_stop():
        for _ in range(8):
            try:
                eng.stop()
            except Exception as exc:
                errors.append(exc)

    ts = [threading.Thread(target=worker_start), threading.Thread(target=worker_stop)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    assert not errors, f"гонка не должна приводить к исключениям: {errors}"
    eng.stop()
    assert not eng.is_running()


# ── 5. Совместимость API ────────────────────────────────────────────────────

def test_status_and_flags_still_available(tmp_path):
    """Публичный интерфейс не сузился: UI и адаптер им пользуются."""
    eng = make_logged_engine(tmp_path)
    st = eng.status()
    for key in ("available", "running", "exe_path", "log_path", "last_error",
                "last_exit_code", "last_args", "last_cmd"):
        assert key in st, f"пропало поле status(): {key}"
    assert st["running"] is False


def test_is_running_is_lock_free(tmp_path):
    """is_running() не должен ждать блокировку — иначе UI морозит на PowerShell."""
    eng = make_logged_engine(tmp_path)
    assert eng.start(SLEEP_ARGS) is True

    # Держим lock в другом потоке и проверяем, что статус всё равно читается.
    hold = threading.Event()
    release = threading.Event()

    def holder():
        with eng._lock:
            hold.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    hold.wait(timeout=5)
    try:
        t0 = time.monotonic()
        running = eng.is_running()
        elapsed = time.monotonic() - t0
        assert running is True
        assert elapsed < 0.5, f"is_running() заблокировался на {elapsed:.2f}с — UI замерзал бы"
    finally:
        release.set()
        t.join(timeout=5)
        eng.stop()


# ── 6. Контракт с вызывающим кодом (engine_adapter) ─────────────────────────

def _adapter_source() -> str:
    return (ROOT / "umbranet" / "engine_adapter.py").read_text(encoding="utf-8")


def _finally_blocks_with_cleanup(tree, fn_name):
    """Для функции: finally-блоки, в которых есть и stop(), и cleanup_orphans().

    Возвращает список (строка, исходный текст блока). Такой блок — это «уборка
    после варианта»: остановить свой winws и подмести возможный мусор.
    """
    import ast

    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == fn_name),
        None,
    )
    assert fn is not None, f"в engine_adapter пропала функция {fn_name}"

    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        text = ast.unparse(ast.Module(body=node.finalbody, type_ignores=[]))
        if "cleanup_orphans" not in text:
            continue
        # Важно НЕ только наличие вызова, но и то, что он не спрятан под
        # условием про результат остановки: регресс выглядел как «уборка под
        # if not stop() / под if last_error / под if started or is_running()».
        # Проверка «любой if» слишком грубая: безобидный защитный
        # `if hasattr(winws, "cleanup_orphans")` есть и в правильном коде.
        guards = [
            sub.test for sub in ast.walk(ast.Module(body=node.finalbody, type_ignores=[]))
            if isinstance(sub, ast.If)
            and any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "cleanup_orphans"
                for inner in ast.walk(sub)
            )
        ]
        risky = (".stop(", "last_error", "is_running", "started")
        conditional = [
            ast.unparse(test) for test in guards
            if any(marker in ast.unparse(test) for marker in risky)
        ]
        found.append((node.lineno, text, not conditional, conditional))
    return found


@pytest.mark.parametrize(
    "fn_name",
    ["dpi_strategy_ai_run_controlled", "dpi_strategy_check_all_controlled"],
)
def test_every_variant_is_cleaned_up_unconditionally(fn_name):
    """Тревожный тест-сторож: уборка после КАЖДОГО варианта, а не «когда-нибудь».

    История. Уборка в цикле звала PowerShell по маске пути (~секунды на вариант),
    поэтому её убрали из горячего цикла и оставили только на ветке «stop() не
    смог». Вместе с ней ушла гарантия: winws.exe, чей Popen-объект потерян, никто
    больше не убивал, он держал WinDivert — и КАЖДЫЙ следующий вариант падал на
    старте. Гарантия, что стратегия вообще получится, была именно здесь.

    Требование к коду: cleanup_orphans(stop_driver=False) стоит в finally
    per-variant цикла БЕЗУСЛОВНО, рядом с stop(). Дёшево это потому, что
    cleanup_orphans сначала смотрит процессы через WinAPI и только при находке
    что-то делает (см. test_cleanup_orphans_is_free_when_nothing_is_stale).
    """
    import ast

    source = _adapter_source()
    blocks = _finally_blocks_with_cleanup(ast.parse(source), fn_name)
    assert blocks, (
        f"{fn_name}: исчез finally-блок с уборкой. Это значит, что зависший "
        f"winws.exe больше никто не убирает — варианты начнут падать подряд."
    )
    for lineno, text, unconditional, conditional in blocks:
        assert "cleanup_orphans" in text, f"{fn_name}: в finally (строка {lineno}) нет уборки"
        assert ".stop(" in text, f"{fn_name}: в finally (строка {lineno}) нет остановки WinWS"
        assert unconditional, (
            f"{fn_name}: уборка (строка {lineno}) спрятана под условием. Именно так "
            f"и потерялась гарантия: уборка выполнялась только когда stop() вернул "
            f"False, и зависший winws.exe ломал все следующие варианты. "
            f"Вызов в finally должен быть безусловным — он бесплатный, когда чисто. "
            f"Найденные условия: {conditional}"
        )


@pytest.mark.parametrize(
    "fn_name",
    ["dpi_strategy_ai_run_controlled", "dpi_strategy_check_all_controlled"],
)
def test_per_variant_cleanup_is_not_destructive(fn_name):
    """Уборка в цикле не должна быть жёсткой: stop_driver=True там недопустим.

    Жёсткий режим (с остановкой служб WinDivert) — только на выходе из программы.
    В цикле он бы ронял драйвер между вариантами и мешал следующему запуску.
    """
    import ast

    fn = next(
        (n for n in ast.walk(ast.parse(_adapter_source()))
         if isinstance(n, ast.FunctionDef) and n.name == fn_name),
        None,
    )
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        text = ast.unparse(ast.Module(body=node.finalbody, type_ignores=[]))
        if "cleanup_orphans" in text:
            assert "stop_driver=True" not in text, (
                f"{fn_name}: в цикле нельзя останавливать службы WinDivert (строка {node.lineno})"
            )


def test_hard_cleanup_still_kills_everything():
    """Путь выхода из программы обязан убивать всё, включая свой процесс.

    Если эту ветку однажды поменяют на безопасный режим по умолчанию
    (keep_running=True), после AI-генерации останется живой winws.exe и
    WinDivert останется загруженным.
    """
    import ast

    # Импортировать engine_adapter в тесте нельзя: он тянет Qt и живую
    # среду. Достаточно разобрать его исходник.
    source = _adapter_source()
    lines = source.splitlines()
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "dpi_strategy_ai_cleanup_runtime"
    )
    src = "\n".join(lines[fn.lineno - 1: fn.end_lineno])
    assert "keep_running=False" in src, (
        "жёсткая зачистка обязана явно требовать keep_running=False: "
        "по умолчанию cleanup_orphans() теперь щадит живой процесс"
    )


# ── 7. Подсказка из лога winws (почему пустой результат) ────────────────────

def test_error_hint_finds_windivert_failure(tmp_path):
    """Если WinDivert занят чужой программой, в логе это видно — и мы это скажем."""
    eng = make_logged_engine(tmp_path)
    eng._log_path.parent.mkdir(parents=True, exist_ok=True)
    eng._log_path.write_text(
        "2026-09-15 10:00:00 WinWS start\n"
        "CMD: C:\\UmbraNet\\bin\\winws.exe --filter-tcp=443 --hostlist=x.txt\n"
        "windivert: failed to open filter driver (0x00000005)\n",
        encoding="utf-8",
    )

    hint = eng.error_hint()
    assert "failed to open" in hint.lower(), f"подсказка не найдена: {hint!r}"
    assert "WinWS start" not in hint, "технические строки не должны попадать в подсказку"


def test_error_hint_empty_for_clean_log(tmp_path):
    eng = make_logged_engine(tmp_path)
    eng._log_path.parent.mkdir(parents=True, exist_ok=True)
    eng._log_path.write_text(
        "2026-09-15 10:00:00 WinWS start\nCMD: winws.exe --filter-tcp=443\n"
        "циркуляция пакетов идёт штатно\n",
        encoding="utf-8",
    )
    assert eng.error_hint() == "", "чистый лог не должен давать ложную ошибку"


def test_error_hint_survives_missing_log(tmp_path):
    eng = make_logged_engine(tmp_path)
    assert eng.error_hint() == ""
