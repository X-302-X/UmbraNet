"""
Обёртка DPI-движка UmbraNet: исполняет bin/e1-spike.exe (собственное ядро).

Модуль сохраняет историческое имя `winws_engine` ради совместимости импортов
(dns_server / engine_adapter / network / тесты), но выполняет он НЕ winws, а
наш движок e1-spike.exe. Принцип «отвёртки»: движок получает список целей и
Uz (аргументы StrategyManager.get_args) и ТОЛЬКО исполняет их.

Контракт запуска:  e1-spike.exe protect --json --strategy=<args.json>
Штатный стоп:      перевод строки в stdin (как «⏹ Стоп» в Engine Lab).
Тест на регресс:   tests/test_winws_engine.py
"""
import logging
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def _win_shell():
    """Ленивый импорт обёртки системных вызовов (пункт H4).

    Путь до `core/` добавляем явно: движок может импортироваться и как
    `dpi.winws_engine`, и как `winws_engine` — от этого зависит sys.path.
    """
    core_dir = str(Path(__file__).resolve().parents[1])
    if core_dir not in sys.path:
        sys.path.insert(0, core_dir)
    import win_shell
    return win_shell

log = logging.getLogger("UmbraNet.WinWSEngine")

# Сколько ждём мягкого завершения (terminate) и добивания (kill)
STOP_TIMEOUT = 3.0
KILL_TIMEOUT = 2.0
# Пауза между stop и start внутри restart: WinDivert нужен тик, чтобы
# освободить драйвер до новой попытки его захватить.
RESTART_SETTLE = 0.2
# Таймаут PowerShell-вызовов
PS_TIMEOUT = 5.0


def build_orphan_kill_command(exe_path, bin_dir, keep_pid=None) -> str:
    """Готовит PowerShell-команду зачистки winws.exe текущей установки.

    Убиваем только процессы, связанные с НАШЕЙ папкой bin, чтобы не трогать
    посторонние копии winws. keep_pid — PID, который трогать нельзя
    (живой WinWS движка): без этого ограничения зачистка убивает свежий запуск.
    """
    exe_ps = str(exe_path).replace("'", "''")
    bin_ps = str(bin_dir).rstrip("\\/").replace("'", "''")
    keep_clause = ""
    if keep_pid:
        keep_clause = f" -and ($_.ProcessId -ne {int(keep_pid)})"
    return (
        f"$exe='{exe_ps}'; $bin='{bin_ps}'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -ieq 'e1-spike.exe' -and ("
        " ($_.ExecutablePath -and $_.ExecutablePath -ieq $exe) -or "
        " ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($bin, [System.StringComparison]::OrdinalIgnoreCase)) -or "
        " ($_.CommandLine -and $_.CommandLine.Contains($bin))"
        f"){keep_clause} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
    )


def process_alive(proc) -> bool:
    """Жив ли процесс Popen. Никогда не бросает — это диагностический запрос."""
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False


# ── Быстрый обзор процессов winws.exe без PowerShell ────────────────────────
# Зачем: PowerShell (Get-CimInstance Win32_Process) идёт 0.5-3 секунды, а в
# AI-генерации зачистка нужна ПОСЛЕ КАЖДОГО варианта. Именно из-за цены вызова
# зачистку и убрали из горячего цикла — и зря: вместе с ней ушла гарантия, что
# зависший winws.exe (тот, чей Popen потерян) не держит WinDivert и не ломает
# следующий запуск. Правильное решение — оставить зачистку, но сделать обзор
# быстрым: перечислить процессы через WinAPI напрямую (десятки миллисекунд).

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = -1

# Инъекция обзора процессов для тестов: callable() -> list[tuple[int, str]] | None
_scan_provider = None


def _scan_processes_winapi() -> list[tuple[int, str]] | None:
    """[(pid, путь к exe)] для процессов с именем winws.exe. None — не смогли.

    None означает «посмотреть не получилось» (не Windows, нет прав, сбой
    ctypes) — вызывающий код обязан откатиться на старый путь через PowerShell.
    Пустой список означает «смотрели и winws.exe нет»: PowerShell уже не нужен.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE or not snapshot:
            return None
        found: list[tuple[int, str]] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if str(entry.szExeFile).lower() == "e1-spike.exe":
                    pid = int(entry.th32ProcessID)
                    path = ""
                    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if handle:
                        try:
                            buf = ctypes.create_unicode_buffer(32768)
                            size = wintypes.DWORD(len(buf))
                            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                                path = buf.value or ""
                        finally:
                            kernel32.CloseHandle(handle)
                    found.append((pid, path))
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return found
    except Exception as exc:                      # pragma: no cover - только Windows
        log.debug("Быстрый обзор процессов не удался: %s", exc)
        return None


def _scan_processes() -> list[tuple[int, str]] | None:
    """Единая точка обзора: инъекция для тестов, иначе быстрый WinAPI-обзор."""
    if _scan_provider is not None:
        try:
            return _scan_provider()
        except Exception as exc:                  # pragma: no cover - тесты
            log.debug("scan_provider упал: %s", exc)
            return None
    return _scan_processes_winapi()


def _same_path(a: str, b: str) -> bool:
    """Сравнение путей Windows без учёта регистра и лишних слэшей."""
    def norm(value: str) -> str:
        return (value or "").replace("/", "\\").rstrip("\\").lower()
    return bool(a) and norm(a) == norm(b)


def _is_within(path: str, folder: str) -> bool:
    """Лежит ли путь внутри папки (без учёта регистра)."""
    a = (path or "").replace("/", "\\").lower()
    b = (folder or "").replace("/", "\\").rstrip("\\").lower()
    return bool(a) and bool(b) and (a == b or a.startswith(b + "\\"))


def _kill_pid_native(pid: int) -> bool:
    """TerminateProcess напрямую, без PowerShell. False — не получилось."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:                      # pragma: no cover - только Windows
        log.debug("TerminateProcess(%s) не удался: %s", pid, exc)
        return False


class WinWSEngine:
    def __init__(self):
        self.process = None
        self._log_handle = None
        self._bin_dir = self._find_bin_dir()
        self._exe_path = self._bin_dir / "e1-spike.exe"
        # Все логи — в одной папке logs\ рядом с bin\ (пожелание 2026-10-05:
        # «сделать так, чтобы все логи были там»).
        logs_dir = self._bin_dir.parent / "logs"
        self._log_path = logs_dir / "e1-spike.log"
        self._args_path = logs_dir / "e1spike_args.json"
        self.last_error = ""
        self.last_args = []
        self.last_cmd = []
        self.last_exit_code = None
        # Сериализует переходы состояния процесса (RLock — start зовёт stop).
        self._lock = threading.RLock()
        # Сколько раз дёрнули PowerShell. На нормальной работе должно быть 0;
        # используется в тестах и в диагностике.
        self.ps_calls = 0
        # Инъекция запуска PowerShell для тестов (None = настоящий запуск).
        self._ps_runner = None

    def _find_bin_dir(self):
        current = Path(__file__).resolve().parent
        for _ in range(6):
            candidate = current / "bin"
            if candidate.exists():
                return candidate
            current = current.parent
        return Path.cwd() / "bin"

    @property
    def exe_path(self):
        return self._exe_path

    @property
    def log_path(self):
        return self._log_path

    def is_available(self):
        return self._exe_path.exists()

    def is_running(self) -> bool:
        """Запущен ли WinWS.

        Читаем БЕЗ блокировки: это частый запрос статуса из UI-потока, и он не
        должен ждать, пока другой поток досчитает PowerShell. Устаревший ответ
        на время операции безвреден, а блокировка UI — нет.
        """
        return process_alive(self.process)

    def _tail_log(self, max_chars: int = 2000) -> str:
        try:
            if not self._log_path.exists():
                return ""
            data = self._log_path.read_text(encoding="utf-8", errors="replace")
            return data[-max_chars:].strip()
        except Exception:
            return ""

    # Что в логе winws.exe означает «не смог подключиться к драйверу».
    # Нужно, чтобы вместо «стратегия не найдена» пользователь видел настоящую
    # причину: например, что WinDivert занят другой DPI-программой.
    ERROR_MARKERS = (
        "failed to open", "cannot open", "can't open", "access is denied",
        "windivert", "error", "ошибка", "отказано",
    )

    def error_hint(self, max_chars: int = 4000) -> str:
        """Строка из лога winws.exe, похожая на ошибку. Пусто — ничего не нашли.

        Лог winws мы и так пишем сами, поэтому это бесплатная диагностика: она
        объясняет пустой результат прогона (драйвер занят, нет прав, неверный
        аргумент) без догадок.
        """
        try:
            data = self._tail_log(max_chars=max_chars)
            if not data:
                return ""
            lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
            for line in reversed(lines):
                low = line.lower()
                if any(marker in low for marker in self.ERROR_MARKERS):
                    # Отсекаем наши же технические строки («WinWS start», «CMD: ...»).
                    if low.startswith("cmd:") or "winws start" in low:
                        continue
                    return line[:300]
        except Exception:
            return ""
        return ""

    def _close_log_handle(self):
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def _build_cmd(self, args):
        """Команда запуска движка (выделена для тестов с фейковым exe).

        Контракт «отвёртки»: аргументы Uz (список целей + приёмы обхода) из
        StrategyManager.get_args пишутся в JSON-стратегию, и движок получает
        e1-spike.exe protect --json --strategy=<файл> — ничего своего он не
        подмешивает.
        """
        payload = {"id": "umbra", "name": "UmbraNet", "args": [str(a) for a in args]}
        self._args_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return [str(self._exe_path), "protect", "--json", f"--strategy={self._args_path}"]

    def start(self, args):
        with self._lock:
            return self._start_locked(args)

    def _start_locked(self, args):
        self.last_error = ""
        self.last_exit_code = None
        self.last_args = list(args or [])
        self.last_cmd = []

        if not self.is_available():
            self.last_error = f"e1-spike.exe не найден: {self._exe_path}"
            log.error(self.last_error)
            return False
        if not args:
            self.last_error = "Пустые аргументы движка: стратегия не найдена или повреждена"
            log.error(self.last_error)
            return False
        if self.is_running():
            self.stop(reset_connections=False)

        cmd = self._build_cmd(args)
        self.last_cmd = cmd
        try:
            log.info("Запуск движка: %s", " ".join(self.last_cmd))
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._close_log_handle()
            self._log_handle = open(self._log_path, "a", encoding="utf-8", errors="replace")
            self._log_handle.write("\n" + "=" * 80 + "\n")
            self._log_handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " e1-spike start\n")
            self._log_handle.write("CMD: " + " ".join(cmd) + "\n")
            self._log_handle.flush()

            env = os.environ.copy()
            # Чтобы WinWS точно видел WinDivert.dll/cygwin1.dll рядом с собой.
            env["PATH"] = str(self._bin_dir) + os.pathsep + env.get("PATH", "")

            self.process = subprocess.Popen(
                cmd,
                cwd=str(self._bin_dir),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            time.sleep(0.8)
            if self.process.poll() is not None:
                self.last_exit_code = self.process.returncode
                tail = self._tail_log()
                self.last_error = f"Движок упал при старте (код {self.last_exit_code})"
                if tail:
                    self.last_error += f": {tail[-500:]}"
                log.error(self.last_error)
                self.process = None
                self._close_log_handle()
                return False
            log.info("Движок запущен (PID %s), лог: %s", self.process.pid, self._log_path)
            return True
        except Exception as exc:
            self.last_error = f"Ошибка запуска движка: {exc}"
            log.error(self.last_error)
            self.process = None
            self._close_log_handle()
            return False

    # ── Запуск PowerShell: единственная точка в модуле ────────────────────────

    def _run_ps(self, ps_cmd: str, timeout: float = PS_TIMEOUT, quiet: bool = False) -> bool:
        """Выполняет PowerShell и считает вызовы в self.ps_calls.

        Счётчик нужен, чтобы регресс «PowerShell на каждом stop()» ловился
        тестом, а не превращался в минуты ожидания у пользователя.
        """
        self.ps_calls += 1
        runner = self._ps_runner
        if runner is not None:                      # инъекция для тестов
            try:
                return bool(runner(ps_cmd, timeout))
            except Exception as exc:
                log.debug("ps_runner упал: %s", exc)
                return False
        if os.name != "nt":
            return False
        # Через win_shell (пункт H4): там проверка доступности PowerShell и запуск
        # без мелькания окна. Недоступный PowerShell — не «ошибка на каждой
        # зачистке», а один раз записанная причина и работа через taskkill.
        shell = _win_shell()
        res = shell.run_ps(ps_cmd, timeout=timeout)
        if not res.ok:
            # quiet: добровольные/страховочные шаги (например, остановка
            # службы WinDivert) не должны засорять last_error — её отсутствие
            # или отказ штатной остановки не ошибка (поле 2026-10-04:
            # «last_error: Stop-Service…» в отчёте выглядел пугающе).
            if not quiet:
                self.last_error = res.human_error or "PowerShell не сработал"
            log.debug("%s (команда: %s)", res.human_error or "PowerShell не сработал", ps_cmd[:60])
        return res.ok

    def _kill_orphan_processes(self, keep_pid=None) -> bool:
        """Добивает winws.exe из нашей папки bin, если Python потерял Popen-объект.

        Это главный DPI-safety-net: при краше winws.exe может остаться жить и
        продолжать держать WinDivert и файлы UmbraNet/bin.

        keep_pid — процесс, который убивать НЕЛЬЗЯ (наш живой WinWS). Передавайте
        его всегда, когда зачистка идёт рядом с работающим движком: иначе будет
        убит свежий запуск (симптом «DPI включён, а его нет»).
        """
        cmd = build_orphan_kill_command(self._exe_path, self._bin_dir, keep_pid)
        return self._run_ps(cmd)

    def _kill_by_pid(self, pid: int) -> bool:
        """Точечное добивание по PID — без маски пути, ничего лишнего не заденет.

        Если PowerShell не сработал (или его нет), тот же процесс закрывается
        через `taskkill /F /PID` (пункт H4): раньше в этой ситуации процесс
        оставался жить и держал WinDivert, то есть у человека «DPI выключен, а
        интернет всё ещё не работает».
        """
        if self._run_ps(
            f"Stop-Process -Id {int(pid)} -Force -ErrorAction SilentlyContinue"
        ):
            return True
        if self._ps_runner is not None:
            return False                       # инъекция для тестов: без системных вызовов
        res = _win_shell().kill_process(pid=int(pid))
        if res.ok:
            log.info("winws.exe (PID %s) закрыт через %s: PowerShell не сработал", pid, res.via)
        else:
            self.last_error = res.human_error or "не удалось закрыть процесс"
        return res.ok

    def scan_own_processes(self, keep_pid=None) -> tuple[list[tuple[int, str]], bool]:
        """Свои winws.exe: список (pid, путь) и признак «посмотреть удалось».

        «Свои» — запущенные из нашего exe или из нашей папки bin. Чужие
        копии winws не трогаем. keep_pid — наоборот, исключаем: это
        живой WinWS движка, убивать его нельзя.
        """
        scanned = _scan_processes()
        if scanned is None:
            return [], False
        own: list[tuple[int, str]] = []
        for pid, path in scanned or []:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                continue
            if keep_pid and pid_int == int(keep_pid):
                continue
            if _same_path(path, str(self._exe_path)) or _is_within(path, str(self._bin_dir)):
                own.append((pid_int, str(path)))
        return own, True

    def foreign_processes(self) -> list[tuple[int, str]] | None:
        """winws.exe, НЕ относящиеся к нашей установке — «похожие программы».

        Нужны для понятной диагностики: если WinDivert занят чужой программой
        (сторонняя DPI-программа или другая копия), наш winws.exe не сможет захватить
        драйвер, и ни один вариант не покажет результат. Об этом лучше сказать
        прямо, чем показывать «стратегия не найдена».
        """
        scanned = _scan_processes()
        if scanned is None:
            return None
        foreign: list[tuple[int, str]] = []
        for pid, path in scanned or []:
            if _same_path(path, str(self._exe_path)) or _is_within(path, str(self._bin_dir)):
                continue
            try:
                foreign.append((int(pid), str(path)))
            except (TypeError, ValueError):
                continue
        return foreign

    def sweep_stale(self, keep_pid=None) -> dict:
        """Убивает зависшие winws.exe нашей установки, НЕ запуская PowerShell.

        Зачем отдельный метод: обзор через WinAPI + TerminateProcess занимает
        десятки миллисекунд, поэтому его можно делать после КАЖДОГО варианта
        AI-генерации. Изначально зачистку из горячего цикла убрали именно
        из-за цены PowerShell — и потеряли гарантию, что переживший сбой
        winws.exe не останется держать WinDivert и не сломает следующий запуск.

        Результат: {"scanned", "killed", "left", "ps_fallback"}.
        ps_fallback=True означает, что посмотреть процессы не удалось (не
        Windows, нет прав, сбой) и вызывающему стоит пойти старым путём через
        PowerShell.
        """
        own, scanned = self.scan_own_processes(keep_pid=keep_pid)
        if not scanned:
            return {"scanned": False, "killed": [], "left": [], "ps_fallback": True}
        killed: list[int] = []
        left: list[int] = []
        for pid, path in own:
            ok = _kill_pid_native(pid)
            if ok:
                killed.append(pid)
                log.warning("Убит зависший движок (PID %s): %s", pid, path)
            else:
                left.append(pid)
        if left:
            # Прямое завершение не сработало — добиваем по PID через PowerShell.
            for pid in list(left):
                self._kill_by_pid(pid)
            time.sleep(0.1)
            still, scanned_again = self.scan_own_processes(keep_pid=keep_pid)
            if scanned_again:
                left = [pid for pid, _path in still]
        return {"scanned": True, "killed": killed, "left": left, "ps_fallback": False}

    def cleanup_orphans(self, stop_driver: bool = False, keep_running: bool = True) -> bool:
        """Зачистка DPI-хвостов текущей установки UmbraNet.

        keep_running=True (по умолчанию) — не трогать живой WinWS движка. Это
        безопасный вариант для вызова из фона: раньше зачистка убивала по маске
        пути и могла снести только что запущенный процесс.
        keep_running=False — жёсткий режим для выхода из программы, где убить
        нужно всё.

        Возвращает True, если что-то реально было сделано (кто-то убит или
        выполнена PowerShell-зачистка). Если процессов не было вовсе — False и
        НОЛЬ запусков PowerShell: именно поэтому зачистку можно звать в цикле.
        """
        with self._lock:
            keep = None
            if keep_running:
                proc = self.process
                if process_alive(proc):
                    keep = getattr(proc, "pid", None)

            sweep = self.sweep_stale(keep_pid=keep)
            if sweep["scanned"]:
                killed = bool(sweep["killed"]) or bool(sweep["left"])
                if sweep["left"]:
                    log.error("Не удалось убить winws.exe: %s", sweep["left"])
            else:
                # Посмотреть процессы не смогли — прежний путь (по маске пути).
                killed = self._kill_orphan_processes(keep_pid=keep)

            if not keep_running:
                # Жёсткий режим — это выход из программы, он бывает один раз за
                # запуск: здесь дополнительно оставляем страховку по маске пути
                # (она видит и процессы с нашим bin в командной строке, которых
                # быстрый обзор по пути к exe может не заметить).
                killed = self._kill_orphan_processes(keep_pid=None) or killed

            if stop_driver:
                self._stop_windivert_services()
            return killed

    def _stop_windivert_services(self) -> None:
        if os.name != "nt" and self._ps_runner is None:
            return
        ps = (
            "foreach ($s in 'WinDivert','WinDivert14','WinDivert64') { "
            "  Stop-Service -Name $s -Force -ErrorAction SilentlyContinue "
            "}"
        )
        self._run_ps(ps, quiet=True)

    def stop(self, reset_connections: bool = True) -> bool:
        """Останавливает WinWS. True = процесс гарантированно мёртв.

        reset_connections=True (кнопка «Стоп»): движку шлётся "rst" — он
        сбрасывает «тёплые» соединения целей RST (иначе браузер продолжает
        грузить по уже открытым каналам). False (перезапуск при смене
        галочек) — тихий стоп без сброса.

        На нормальном пути PowerShell НЕ вызывается: terminate/kill достаточно.
        Это и убирает лишние powershell.exe, и не даёт зачистке по маске пути
        задеть чужой свежий запуск.
        """
        with self._lock:
            proc = self.process
            self.process = None
            if proc is None:
                self._close_log_handle()
                # Popen мог потеряться (restart из другого потока, сбой), а сирота
                # из нашей bin/ всё ещё держит WinDivert и ПРОДОЛЖАЕТ обход —
                # как раз случай «Стоп нажат, а трафик всё ещё обрабатывается».
                # Обычный путь остановки такой процесс не видит, поэтому здесь
                # быстрая зачистка только СВОИХ процессов, без PowerShell.
                sweep = self.sweep_stale(keep_pid=None)
                if sweep.get("killed") or sweep.get("left"):
                    log.warning(
                        "stop(): привязка к процессу потеряна, но нашлись движки нашей установки — "
                        "убито %s, осталось %s",
                        sweep.get("killed"), sweep.get("left"),
                    )
                return not bool(sweep.get("left"))

            pid = getattr(proc, "pid", None)
            dead = self._terminate(proc, reset_connections)
            self._close_log_handle()

            if dead:
                log.info("Движок остановлен (код %s)", self.last_exit_code)
                return True

            # Эскалация: процесс не отдал управление.
            # Бьём ТОЧНО по PID — маска пути могла бы задеть свежий запуск.
            log.warning("Движок (PID %s) не завершился штатно — добиваю", pid)
            if pid:
                # Сначала напрямую через WinAPI (десятки миллисекунд), и только
                # если не вышло — PowerShell. На Windows это убирает ещё один
                # внешний процесс из горячего пути.
                if not _kill_pid_native(pid):
                    self._kill_by_pid(pid)
                dead = not process_alive(proc)
            if not dead:
                # Последний рубеж: путь-маска. Здесь keep_pid=None осознанно —
                # мы уже пытались остановить именно этот процесс.
                self._kill_orphan_processes(keep_pid=None)
                dead = not process_alive(proc)
            if not dead:
                self.last_error = f"Движок (PID {pid}) не удалось остановить"
                log.error(self.last_error)
            return dead

    def _terminate(self, proc, reset_connections: bool = True) -> bool:
        """terminate → wait → kill → wait. True, если процесс умер."""
        try:
            if proc.poll() is not None:
                self.last_exit_code = proc.returncode
                return True
            # Штатный стоп: движок закрывается по строке в stdin (как
            # «⏹ Стоп» в Engine Lab) — без kill и без обрыва статистики.
            # "rst" — стоп И сброс соединений RST (кнопка Стоп); пустая
            # строка — тихий стоп (перезапуск при смене галочек).
            try:
                if proc.stdin:
                    proc.stdin.write(b"rst\n" if reset_connections else b"\n")
                    proc.stdin.flush()
                    # Живой движок выходит за ~100 мс; ждём до секунды,
                    # дальше — штатная эскалация terminate/kill.
                    deadline = time.time() + 1.0
                    while time.time() < deadline:
                        if proc.poll() is not None:
                            self.last_exit_code = proc.returncode
                            return True
                        time.sleep(0.05)
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=KILL_TIMEOUT)
            self.last_exit_code = proc.returncode
            return True
        except Exception as exc:
            self.last_error = f"Ошибка остановки WinWS: {exc}"
            log.error(self.last_error)
            return not process_alive(proc)

    def restart(self, args):
        """Перезапускает winws.exe с новыми аргументами — АТОМАРНО.

        stop + пауза + start держатся под одним lock. Раньше здесь был разрыв:
        между stop() и start() успевал вклиниться stop() из другого потока
        (кнопка «Стоп», health-таймер, cleanup после AI-варианта), и его
        зачистка по маске пути добивала свежий winws.exe.
        """
        with self._lock:
            log.info("Перезапуск WinWS...")
            self.stop(reset_connections=False)
            time.sleep(RESTART_SETTLE)
            return self._start_locked(args)

    def status(self) -> dict:
        return {
            "available": self.is_available(),
            "running": self.is_running(),
            "exe_path": str(self._exe_path),
            "log_path": str(self._log_path),
            "last_error": self.last_error,
            "last_exit_code": self.last_exit_code,
            "last_args": list(self.last_args),
            "last_cmd": list(self.last_cmd),
        }


_engine = None


def get_winws_engine():
    global _engine
    if _engine is None:
        _engine = WinWSEngine()
    return _engine
