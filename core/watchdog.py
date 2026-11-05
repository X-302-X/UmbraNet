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
    HELLO\\n          — рукопожатие сразу после запуска: «я жив, pipe рабочий»
    ARM_PROXY\\n      — текущая сессия владеет только PAC/proxy: при EOF вернуть
                       именно сохранённые proxy-настройки, DNS не трогать
    ARM_BOTH\\n       — legacy-сессия владеет DNS и PAC
    CLEAN\\n          — штатный выход: родитель сам всё вернул, ничего не делаем
    RESTORE\\n        — legacy: вернуть DNS, как было до UmbraNet
    RESTORE_PROXY\\n  — вернуть только сохранённые PAC/manual proxy-настройки
    RESTORE_ALL\\n    — вернуть DNS и proxy
    <EOF>            — родитель УПАЛ: вернуть только ресурсы, указанные ARM

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

from dataclasses import dataclass
import os
import subprocess
import sys
import time

# Команды протокола (родитель → watchdog)
SIGNAL_HELLO = "HELLO"
SIGNAL_CLEAN = "CLEAN"
SIGNAL_RESTORE = "RESTORE"
SIGNAL_RESTORE_PROXY = "RESTORE_PROXY"
SIGNAL_RESTORE_ALL = "RESTORE_ALL"
SIGNAL_ARM_PROXY = "ARM_PROXY"
SIGNAL_ARM_BOTH = "ARM_BOTH"


@dataclass(frozen=True)
class WatchdogSession:
    """Final pipe result and resources that must be restored on parent death."""

    signal: str
    restore_dns: bool = True
    restore_proxy: bool = False

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
    if line.startswith(SIGNAL_RESTORE_PROXY):
        return SIGNAL_RESTORE_PROXY
    if line.startswith(SIGNAL_RESTORE_ALL):
        return SIGNAL_RESTORE_ALL
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


def wait_for_parent_session(stream=None, sleep_fn=None) -> WatchdogSession:
    """Wait for HELLO and return a resource-aware final watchdog session.

    A legacy parent that sends only ``HELLO`` keeps the historic DNS-only
    recovery behaviour.  A transport-only parent sends ``ARM_PROXY`` after
    HELLO, making a later EOF restore the PAC snapshot without touching DNS.
    """
    sleep = sleep_fn or time.sleep
    reader = LineReader(stream if stream is not None else _default_stream())

    for attempt in range(READ_RETRY_LIMIT):
        line, failed = reader.read_line()
        if line.startswith(SIGNAL_HELLO):
            break
        if failed:
            _say(f"UmbraNet watchdog: stdin не читается (попытка {attempt + 1}/{READ_RETRY_LIMIT})")
            sleep(READ_RETRY_SLEEP)
            continue
        if line == "":
            _say("UmbraNet watchdog: родитель завершился сразу после старта")
            return WatchdogSession("")
        _say(f"UmbraNet watchdog: неожиданная строка от родителя: {line[:40]!r}")
        sleep(READ_RETRY_SLEEP)
    else:
        raise UnmonitoredError("родитель не прислал HELLO")

    restore_dns, restore_proxy = True, False
    for _attempt in range(READ_RETRY_LIMIT):
        line, failed = reader.read_line()
        if failed:
            sleep(READ_RETRY_SLEEP)
            continue
        if line == SIGNAL_ARM_PROXY:
            restore_dns, restore_proxy = False, True
            continue
        if line == SIGNAL_ARM_BOTH:
            restore_dns, restore_proxy = True, True
            continue
        signal = _classify(line)
        if signal == SIGNAL_RESTORE_PROXY:
            return WatchdogSession(signal, restore_dns=False, restore_proxy=True)
        if signal == SIGNAL_RESTORE_ALL:
            return WatchdogSession(signal, restore_dns=True, restore_proxy=True)
        return WatchdogSession(signal, restore_dns=restore_dns, restore_proxy=restore_proxy)
    raise UnmonitoredError("канал связи с родителем перестал читаться")


def wait_for_parent(stream=None, sleep_fn=None) -> str:
    """Compatibility wrapper returning only the final signal string."""
    return wait_for_parent_session(stream, sleep_fn=sleep_fn).signal


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


def restore_user_proxy() -> bool:
    """Return the prior PAC/manual proxy state from the transport snapshot."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from vpn.system_proxy import restore_proxy_snapshot
        ok, message = restore_proxy_snapshot()
        _say(f"UmbraNet watchdog: восстановление proxy -> {ok} ({message})")
        return bool(ok)
    except Exception as exc:
        _say(f"UmbraNet watchdog: восстановление proxy не удалось: {exc}")
        return False


def handle_session(session: WatchdogSession, *, restore_dns_fn=None,
                   restore_proxy_fn=None, admin_fn=None) -> str:
    """Restore exactly the resources armed for a crashed parent session."""
    if session.signal == SIGNAL_CLEAN:
        return "skip"
    actions: list[str] = []
    if session.restore_dns:
        can_restore_dns = (admin_fn or is_admin)()
        if can_restore_dns:
            (restore_dns_fn or restore_user_dns)()
            actions.append("dns")
        else:
            _say("UmbraNet watchdog: нет прав администратора для возврата DNS")
    if session.restore_proxy:
        (restore_proxy_fn or restore_user_proxy)()
        actions.append("proxy")
    return "restore" if actions else "skip"


def handle_signal(signal: str, restore_fn=None) -> str:
    """Compatibility handler for the established DNS-only watchdog protocol."""
    return handle_session(
        WatchdogSession(signal, restore_dns=True, restore_proxy=False),
        restore_dns_fn=restore_fn,
        admin_fn=lambda: True,
    )


def main() -> None:
    # Отладочный режим: ждём сигнал и печатаем, что бы сделали, не меняя систему.
    # Проверка протокола руками: printf 'HELLO\nCLEAN\n' | python core/watchdog.py --check
    if len(sys.argv) > 1 and sys.argv[1] in ("--check", "--signal-test"):
        try:
            session = wait_for_parent_session()
        except UnmonitoredError as exc:
            _say(f"unmonitored: {exc}")
            return
        action = "skip" if session.signal == SIGNAL_CLEAN else "restore"
        _say(f"signal={session.signal!r} action={action}")
        return

    try:
        session = wait_for_parent_session()
    except UnmonitoredError as exc:
        # Не смогли доказать смерть родителя → не трогаем ресурсы живой программы.
        _say(f"UmbraNet watchdog: {exc} — выходим без восстановления")
        return

    _say(f"UmbraNet watchdog: родитель завершился (signal={session.signal or 'EOF'})")
    try:
        handle_session(session)
    except Exception as exc:                    # последний предохранитель
        _say(f"UmbraNet watchdog: неожиданная ошибка: {exc}")


if __name__ == "__main__":
    main()
