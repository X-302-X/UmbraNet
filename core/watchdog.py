"""
UmbraNet — Watchdog Process
===========================

Микро-скрипт, который защищает интернет пользователя при краше основной программы.
Запускается отдельным процессом, живёт ровно столько, сколько жив родитель.

Как он узнаёт о краше (переписано 15.09.2026)
---------------------------------------------
Раньше здесь был цикл с `tasklist /FI "PID eq <pid>"` раз в 3 секунды. Это
ломается на переиспользовании PID: Windows отдаёт освободившийся PID другому
процессу, watchdog видит «родитель жив» и НЕ срабатывает никогда — у
пользователя остаётся мёртвый 127.0.0.1 в настройках DNS и полное отсутствие
интернета. Плюс `tasklist` на русской Windows печатает в cp866, и сравнение
строки «не найдены» могло не совпасть.

Теперь используется pipe-детект смерти родителя — стандартный приём, который
работает мгновенно и не зависит от PID:

    родитель (app.py) держит наш stdin открытым (stdin=subprocess.PIPE);
    мы читаем из pipe — чтение БЛОКИРУЕТСЯ;
    как только родитель умирает, ОС закрывает его конец pipe,
    наше чтение получает EOF и возвращается.

Никакого поллинга, ни одного дочернего процесса, ноль ложных срабатываний.

Протокол (родитель → watchdog, по строке в stdin)
-------------------------------------------------
    HELLO\\n    — рукопожатие сразу после запуска: «я жив, pipe рабочий»
    CLEAN\\n    — штатный выход: родитель сам всё вернул, ничего не делаем
    RESTORE\\n  — «верни DNS, как было до UmbraNet»; делаем это сами, чтобы
                 не морозить UI родителя на время работы PowerShell
    <EOF>      — родитель УПАЛ, никто ничего не вернул: спасаем интернет

Почему HELLO обязателен
-----------------------
При запуске через pythonw.exe у процесса НЕТ sys.stdin/sys.stdout. Если читать
через sys.stdin, то readline() не вызовется вовсе, мы сразу увидим «пустоту» и
решим, что родитель мёртв — и сбросим DNS НЕМЕДЛЕННО ПОСЛЕ СТАРТА, сломав
маршрутизацию живого UmbraNet. Поэтому:
  • читаем сырой fd 0 (это pipe, переданный родителем) — работает и под pythonw;
  • ждём HELLO как доказательство, что родитель жив и канал рабочий.

Правило безопасности (асимметрия решений)
-----------------------------------------
  чистый EOF  (b"")        → родитель умер   → ВОЗВРАЩАЕМ DNS
  ошибка чтения (exception) → мы не мониторим → НИЧЕГО НЕ ТРОГАЕМ и выходим

Второе важнее первого: если мы не можем доказать смерть родителя, трогать DNS
живой программы нельзя — это отключило бы пользователю интернет через UmbraNet.

Возврат DNS делается через network_repair.restore_user_dns(): сначала
снапшот (то, что реально было у пользователя), и только потом DHCP.

Вывод идёт через _say(), потому что под pythonw.exe print() уронил бы watchdog.

Лицензия: GPLv3 · UmbraNet_Official / X-302-X
"""

import os
import subprocess
import sys
import time

# Команды протокола (родитель → watchdog)
SIGNAL_HELLO = "HELLO"
SIGNAL_CLEAN = "CLEAN"
SIGNAL_RESTORE = "RESTORE"

# Сколько раз и как долго ждать рабочий pipe, прежде чем сдаться.
# Если родитель жив, но канал почему-то не читается — лучше ничего не делать,
# чем сбросить DNS работающей программы.
READ_RETRY_LIMIT = 5
READ_RETRY_SLEEP = 1.0

# Последний рубеж, если снапшот недоступен
FALLBACK_PS = (
    "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
    "foreach ($a in $adapters) { "
    "  Set-DnsClientServerAddress -InterfaceAlias $a.Name -ResetServerAddresses -ErrorAction SilentlyContinue; "
    "}"
)


def _say(message: str) -> None:
    """Печать без падения: под pythonw.exe sys.stdout is None."""
    out = getattr(sys, "stdout", None)
    if out is None:
        return
    try:
        print(message, flush=True)
    except Exception:
        pass


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def reset_dns_to_auto() -> None:
    """Аварийный сброс DNS на Авто (DHCP) через PowerShell."""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", FALLBACK_PS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class _FdStream:
    """Читалка сырого fd. Нужна, потому что под pythonw.exe sys.stdin is None,
    а pipe от родителя при этом прекрасно работает и доступен как fd 0."""

    def __init__(self, fd: int = 0):
        self._fd = fd

    def read(self, size: int = 4096) -> bytes:
        # Only a real zero-byte read is EOF. Let LineReader classify OSError
        # as a broken/unmonitored pipe, not proof that the parent has died.
        return os.read(self._fd, size)


class UnmonitoredError(RuntimeError):
    """Мы не можем наблюдать за родителем → не имеем права трогать DNS."""


class LineReader:
    """Построчный читатель с сохранением остатка буфера.

    Остаток критичен: в pipe одной пачкой прилетает и «HELLO\\n», и «CLEAN\\n».
    Без сохранения остатка вторую строку мы бы потеряли и решили, что родитель
    умер (а это привело бы к ложному сбросу DNS живого приложения).
    """

    def __init__(self, stream):
        self._stream = stream
        self._buf = b""
        self._eof = False

    def read_line(self) -> tuple[str, bool]:
        """Возвращает (строка, была_ошибка_чтения).

        Ошибка чтения — это НЕ EOF: это разные ситуации, и решения по ним
        противоположные (см. правило безопасности в шапке файла).
        """
        while b"\n" not in self._buf and not self._eof:
            try:
                chunk = self._stream.read(4096)
            except Exception:
                return "", True                    # ошибка ≠ доказательство смерти
            if not chunk:
                self._eof = True
                break                              # чистый EOF
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            self._buf += chunk
            if len(self._buf) > 4096:              # защита от мусора в канале
                break
        if b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
        else:
            line, self._buf = self._buf, b""
        return line.strip().upper().decode("ascii", "ignore"), False


def _default_stream():
    """Канал связи с родителем.

    sys.stdin.buffer берём, если он есть; иначе — сырой fd 0 (случай pythonw.exe).
    """
    return getattr(sys.stdin, "buffer", None) or _FdStream(0)


def _classify(line: str) -> str:
    if line.startswith(SIGNAL_HELLO):
        return SIGNAL_HELLO
    if line.startswith(SIGNAL_CLEAN):
        return SIGNAL_CLEAN
    if line.startswith(SIGNAL_RESTORE):
        return SIGNAL_RESTORE
    return ""


def read_parent_signal(stream=None) -> str:
    """Читает ОДНУ команду. Возвращает HELLO / CLEAN / RESTORE / "".

    Пустая строка = EOF (родитель умер, ничего не сказав).
    Удобно для тестов: подставь io.BytesIO и проверь любой сценарий.
    """
    reader = LineReader(stream if stream is not None else _default_stream())
    line, _failed = reader.read_line()
    return _classify(line)


def wait_for_parent(stream=None, sleep_fn=None) -> str:
    """Ждёт HELLO, затем финальную команду. Возвращает CLEAN / RESTORE / "".

    "" = родитель умер (нормальный сценарий спасения интернета).
    Бросает UnmonitoredError, если канал так и не заработал — тогда watchdog
    обязан выйти, НИЧЕГО не трогая: мы не можем доказать смерть родителя,
    а сброс DNS работающей программы отключил бы пользователю интернет.
    """
    sleep = sleep_fn or time.sleep
    reader = LineReader(stream if stream is not None else _default_stream())

    # Фаза 1: рукопожатие — доказательство, что родитель жив и канал рабочий.
    # ВАЖНО: raise живёт в else — он срабатывает только если цикл НЕ сделал break.
    # Вынести его наружу нельзя: тогда он убил бы и удачное рукопожатие,
    # и вся вторая фаза стала бы недостижимой (ловушка, на которой я уже
    # один раз тут споткнулся).
    for attempt in range(READ_RETRY_LIMIT):
        line, failed = reader.read_line()
        if line.startswith(SIGNAL_HELLO):
            break
        if failed:
            _say(f"UmbraNet watchdog: stdin не читается (попытка {attempt + 1}/{READ_RETRY_LIMIT})")
            sleep(READ_RETRY_SLEEP)
            continue
        if line == "":
            # Чистый EOF сразу после старта: родитель умер, не успев поздороваться.
            # Ровно тот случай, ради которого watchdog и существует.
            _say("UmbraNet watchdog: родитель завершился сразу после старта")
            return ""
        # Не HELLO и не EOF: непонятный протокол. Это НЕ доказательство смерти
        # родителя, поэтому ждём дальше, а потом просто выйдем ничего не тронув.
        _say(f"UmbraNet watchdog: неожиданная строка от родителя: {line[:40]!r}")
        sleep(READ_RETRY_SLEEP)
    else:
        raise UnmonitoredError("родитель не прислал HELLO")

    # Фаза 2: ждём CLEAN / RESTORE / смерти родителя.
    for _attempt in range(READ_RETRY_LIMIT):
        line, failed = reader.read_line()
        if failed:
            sleep(READ_RETRY_SLEEP)
            continue
        return _classify(line)
    raise UnmonitoredError("канал связи с родителем перестал читаться")


def restore_user_dns() -> bool:
    """Вернуть DNS пользователя: снапшот → иначе DHCP.

    network_repair импортируется лениво: если модуль недоступен (переместили
    файлы, сломан импорт), watchdog обязан всё равно сработать через DHCP,
    а не упасть молча.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from network_repair import restore_user_dns as _restore
        ok, msg = _restore()
        _say(f"UmbraNet watchdog: восстановление DNS -> {ok} ({msg})")
        if ok:
            return True
    except Exception as exc:
        _say(f"UmbraNet watchdog: снапшот недоступен ({exc}) — сбрасываю на Авто")
    try:
        reset_dns_to_auto()
        _say("UmbraNet watchdog: DNS сброшен на Авто (DHCP)")
        return True
    except Exception as exc:
        _say(f"UmbraNet watchdog: аварийный сброс DNS не удался: {exc}")
        return False


def handle_signal(signal: str, restore_fn=None) -> str:
    """Реакция на сигнал. Возвращает название действия (для логов и тестов).

    CLEAN   -> "skip"    родитель сам вернул DNS, не вмешиваемся
    RESTORE -> "restore" просьба вернуть DNS
    ""      -> "restore" родитель упал — спасаем интернет
    """
    if signal == SIGNAL_CLEAN:
        return "skip"
    (restore_fn or restore_user_dns)()
    return "restore"


def main() -> None:
    # Отладочный режим: ждём сигнал и печатаем, что бы сделали, НЕ трогая DNS.
    # Проверка протокола руками: printf 'HELLO\nCLEAN\n' | python core/watchdog.py --check
    if len(sys.argv) > 1 and sys.argv[1] in ("--check", "--signal-test"):
        try:
            sig = wait_for_parent()
        except UnmonitoredError as exc:
            _say(f"unmonitored: {exc}")
            return
        _say(f"signal={sig!r} action={'skip' if sig == SIGNAL_CLEAN else 'restore'}")
        return

    if not is_admin():
        _say("UmbraNet watchdog: нет прав администратора — выходим")
        sys.exit(1)  # Без админа DNS всё равно не поменять

    try:
        signal = wait_for_parent()
    except UnmonitoredError as exc:
        # Не смогли доказать смерть родителя → НЕ трогаем DNS живой программы.
        _say(f"UmbraNet watchdog: {exc} — выходим, DNS не трогаем")
        return

    _say(f"UmbraNet watchdog: родитель завершился (signal={signal or 'EOF'})")
    try:
        handle_signal(signal)
    except Exception as exc:                    # последний предохранитель
        _say(f"UmbraNet watchdog: неожиданная ошибка: {exc}")


if __name__ == "__main__":
    main()
