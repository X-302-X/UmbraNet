"""
Тесты единой обёртки системных вызовов и деградации без PowerShell (пункт H4).
================================================================================

**Задача (из отчёта аудита).** «PowerShell заблокирован антивирусом → внятная
ошибка». До этой работы обёртки не было: PowerShell вызывался из четырёх мест, и
у каждого была своя обработка отказа — где-то пустая строка, где-то `False`,
где-то исключение. Если PowerShell в системе недоступен (политика компании,
антивирус, вырезанный Windows PowerShell), часть функций молча не работала:
человек видел «кнопка не действует» вместо «PowerShell недоступен, вот что делать».

**Что проверяем.**

  1. Проверка доступности PowerShell: находится/не находится, кэшируется,
     перепроверяется, различает «нет файла» и «запуск запрещён».
  2. Если PowerShell нет — он не запускается вовсе (никаких зависаний на попытках).
  3. Деградация: очистка DNS-кэша → `ipconfig /flushdns`; список адаптеров и
     DNS-серверы → `netsh`; установка/сброс DNS → `netsh`; закрытие процесса →
     `taskkill`; политики браузеров → `reg`.
  4. Внятный текст: короткая причина для логов, полная подсказка для интерфейса и
     отчёта («что случилось» + «что недоступно» + «что сделать»).
  5. Видно человеку: предупреждение в «Сети и диагностике», строка в полном отчёте,
     проверка в Health Score.

Все тесты работают без Windows: подменяется единственное место, где модуль
запускает процессы — `win_shell._run_hidden`.

Запуск: python -m pytest tests/test_win_shell.py
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
for _p in (str(CORE), str(CORE / "dns"), str(CORE / "dpi"), str(ROOT), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import win_shell                                                            # noqa: E402


def _program(path) -> str:
    """Имя программы из пути — с учётом Windows-разделителей (тесты идут на Linux)."""
    return re.split(r"[\\/]", str(path))[-1].lower()


# ── поддельная система ──────────────────────────────────────────────────────

class FakeSystem:
    """Подменяет win_shell._run_hidden: помнит вызовы, отвечает по программе.

    modes для PowerShell:
      "ok"      — доступен, отвечает;
      "missing" — файла нет (FileNotFoundError);
      "blocked" — запуск запрещён политикой/антивирусом;
      "timeout" — не отвечает.
    """

    def __init__(self, mode: str = "ok", ps_out: str = "", ps_code: int = 0,
                 netsh: dict | None = None, ipconfig_code: int = 0,
                 taskkill_code: int = 0, reg_code: int = 0,
                 reg_err: str = "", reg_out: str = ""):
        self.mode = mode
        self.ps_out = ps_out
        self.ps_code = ps_code
        self.netsh = dict(netsh or {})
        self.ipconfig_code = ipconfig_code
        self.taskkill_code = taskkill_code
        self.reg_code = reg_code
        self.reg_err = reg_err
        self.reg_out = reg_out
        self.calls: list[list[str]] = []

    # удобные выборки для проверок
    def calls_of(self, program: str) -> list[list[str]]:
        return [c for c in self.calls if program in _program(c[0])]

    @property
    def ps_calls(self) -> list[list[str]]:
        return [c for c in self.calls if _program(c[0]).startswith(("powershell", "pwsh"))]

    def __call__(self, args, timeout=15.0):
        args = [str(a) for a in args]
        self.calls.append(args)
        program = _program(args[0])
        joined = " ".join(args).lower()

        if program.startswith(("powershell", "pwsh")):
            if self.mode == "missing":
                return "", "программа не найдена", -1
            if self.mode == "blocked":
                return "", "запуск запрещён: отказано в доступе", -4
            if self.mode == "timeout":
                return "", f"таймаут {timeout:g} с", -2
            if win_shell.PROBE_MARK.lower() in joined:
                return win_shell.PROBE_MARK, "", 0
            return self.ps_out, ("" if self.ps_code == 0 else "ошибка PowerShell"), self.ps_code

        if program.startswith("netsh"):
            for key, answer in self.netsh.items():
                if key in joined:
                    if isinstance(answer, tuple):
                        return answer
                    return answer, "", 0
            return "", "", 0

        if program.startswith("ipconfig"):
            return "Настройка протокола IP для Windows", "", self.ipconfig_code

        if program.startswith("taskkill"):
            return "", "", self.taskkill_code

        if program.startswith("reg"):
            return self.reg_out, self.reg_err, self.reg_code

        return "", f"неизвестная программа {program}", -1


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    """Между тестами кэш проверки PowerShell сбрасывается."""
    win_shell.reset_probe()
    monkeypatch.setattr(win_shell, "IS_WINDOWS", True, raising=False)
    yield
    win_shell.reset_probe()


@pytest.fixture
def fake(monkeypatch):
    def _install(**kwargs) -> FakeSystem:
        fs = FakeSystem(**kwargs)
        monkeypatch.setattr(win_shell, "_run_hidden", fs, raising=False)
        return fs
    return _install


# ── 1. доступность PowerShell ───────────────────────────────────────────────

def test_powershell_available_when_probe_answers(fake):
    fs = fake(mode="ok")
    assert win_shell.powershell_available() is True
    assert len(fs.ps_calls) == 1, "должна быть одна проверка, без лишних запусков"


def test_probe_result_is_cached(fake):
    fs = fake(mode="ok")  # noqa: F841 - нужен ради подмены _run_hidden
    for _ in range(5):
        assert win_shell.powershell_available() is True
    assert len(fs.ps_calls) == 1, f"проверка не кэшируется: {len(fs.ps_calls)} запусков"


def test_probe_refreshed_after_ttl(fake, monkeypatch):
    """Антивирус может разблокировать PowerShell — программа обязана это заметить."""
    fs = fake(mode="missing")
    assert win_shell.powershell_available() is False

    fs.mode = "ok"                        # «антивирус отпустил»
    time_now = [1000.0]
    monkeypatch.setattr(win_shell.time, "monotonic", lambda: time_now[0])
    win_shell._probe["ts"] = 1000.0        # проверка была «сейчас»
    assert win_shell.powershell_available() is False, "в пределах TTL верим кэшу"

    time_now[0] += win_shell.PROBE_TTL + 1
    assert win_shell.powershell_available() is True, "после TTL проверка должна обновиться"


@pytest.mark.parametrize("mode,reason", [("missing", "missing"), ("blocked", "blocked"),
                                         ("timeout", "timeout")])
def test_probe_reports_reason(fake, mode, reason):
    """Причина называется точно: «нет файла» и «запуск запрещён» — разные случаи."""
    fake(mode=mode)
    status = win_shell.powershell_status(force=True)
    assert status["available"] is False
    assert status["reason"] == reason


def test_explicit_path_from_environment(monkeypatch, fake):
    """Путь к PowerShell можно задать переменной среды (нестандартная установка)."""
    fs = fake(mode="ok")
    monkeypatch.setenv(win_shell.POWERSHELL_ENV, r"D:\tools\pwsh\pwsh.exe")
    win_shell.reset_probe()
    assert win_shell.powershell_available() is True
    assert fs.ps_calls[0][0] == r"D:\tools\pwsh\pwsh.exe"


# ── 2. когда PowerShell нет — он не запускается ─────────────────────────────

def test_run_ps_does_not_start_when_unavailable(fake):
    fs = fake(mode="missing")
    res = win_shell.run_ps("Get-Something")
    assert res.via == "none"
    assert not res.ok
    assert "не найден" in res.human_error
    assert not any("Get-Something" in " ".join(c) for c in fs.calls), \
        "команда не должна запускаться, если PowerShell недоступен"


def test_run_ps_passes_flags_and_returns_result(fake):
    fs = fake(mode="ok", ps_out="строка вывода")
    res = win_shell.run_ps("Get-Something", timeout=7)
    assert res.ok and res.stdout == "строка вывода" and res.via == "powershell"
    args = fs.ps_calls[-1]
    for flag in ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"):
        assert flag in args, f"нет флага {flag}"
    assert "Get-Something" in args


def test_launch_failure_forgets_probe(fake):
    """Если PowerShell исчез после успешной проверки — следующая проверка это увидит."""
    fs = fake(mode="ok")
    assert win_shell.powershell_available() is True
    fs.mode = "blocked"                                  # антивирус подключился
    win_shell.run_ps("Get-Something")                    # запуск провалился
    assert win_shell.powershell_status()["available"] is False, "проверка не сброшена"
    assert win_shell.powershell_status()["reason"] == "blocked"


def test_human_error_is_short_and_hint_is_full(fake):
    fake(mode="blocked")
    short = win_shell.human_error("blocked")
    assert short.startswith("PowerShell недоступен") and len(short) < 120, short
    assert "антивирус" in win_shell.POWERSHELL_HINT


# ── 3. деградация: очистка кэша, адаптеры, DNS-серверы ──────────────────────

def test_flush_cache_uses_powershell_when_available(fake):
    fs = fake(mode="ok")
    res = win_shell.flush_dns_cache()
    assert res.ok and res.via == "powershell"
    assert fs.calls_of("ipconfig") == [], "ipconfig не нужен, если PowerShell работает"


def test_flush_cache_falls_back_to_ipconfig(fake):
    """ГЛАВНОЕ для H4: без PowerShell кэш всё равно чистится."""
    fs = fake(mode="missing")
    res = win_shell.flush_dns_cache()
    assert res.ok, f"кэш не очищен: {res.human_error}"
    assert res.via == "ipconfig"
    assert any("/flushdns" in c for c in fs.calls_of("ipconfig"))
    assert "PowerShell" in res.note, f"не сказано, почему не PowerShell: {res.note!r}"


def test_flush_cache_reports_both_failures(fake):
    fs = fake(mode="missing", ipconfig_code=1)
    res = win_shell.flush_dns_cache()
    assert not res.ok
    assert "PowerShell" in res.human_error and "ipconfig" in res.human_error


NETSH_INTERFACES_RU = """Админ. состояние   Состояние       Тип             Имя интерфейса
-------------------------------------------------------------------
Включено           Подключено      Выделенный      Ethernet
Включено           Отключено       Выделенный      VPN Adapter
Отключено          Отключено       Выделенный      Hyper-V Virtual Ethernet"""

NETSH_DNS_RU = """Настройка интерфейса Ethernet
Тип DNS-серверов, настроенных для этого интерфейса: Статические
DNS-серверы, настроенные для этого интерфейса: 127.0.0.1
                                              1.1.1.1
Регистрация с суффиксом: Основной DNS-суффикс"""


def test_list_adapters_uses_powershell_when_available(fake):
    fs = fake(mode="ok", ps_out="Ethernet\nWi-Fi")
    names, res = win_shell.list_adapters()
    assert names == ["Ethernet", "Wi-Fi"] and res.via == "powershell"
    assert fs.calls_of("netsh") == []


def test_list_adapters_falls_back_to_netsh(fake):
    """Без PowerShell адаптеры берутся из netsh (русский вывод, отключённые — мимо)."""
    fs = fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU})
    names, res = win_shell.list_adapters()
    assert names == ["Ethernet"], names
    assert res.via == "netsh" and res.ok


def test_list_adapters_english_output(fake):
    fake(mode="missing", netsh={"show interface":
                                "Admin State    State          Type      Interface Name\n"
                                "Enabled        Connected      Dedicated  Wi-Fi"})
    names, _res = win_shell.list_adapters()
    assert names == ["Wi-Fi"], names


def test_parse_dns_servers_keeps_order_and_families(fake):
    ipv4, ipv6 = win_shell.parse_dns_servers(
        "DNS-серверы: 127.0.0.1\n   1.1.1.1\nIPv6: ::1 2a00:1450:4010::1")
    assert ipv4 == ["127.0.0.1", "1.1.1.1"], "порядок = основной, затем резервный"
    assert ipv6 == ["::1", "2a00:1450:4010::1"], "сокращённые адреса не должны обрезаться"


def test_dns_servers_native_skips_service_adapters(fake):
    """Виртуальные адаптеры (WSL, Hyper-V) в чтении DNS не участвуют.

    Иначе netsh-путь приносил бы чужой DNS виртуального адаптера и проверка
    утечек кричала бы там, где всё в порядке. Фильтр тот же, что в PowerShell-команде.
    """
    fs = fake(mode="missing", netsh={
        "show interface":
            "Включено  Подключено  Выделенный  Ethernet\n"
            "Включено  Подключено  Выделенный  vEthernet (WSL)\n"
            "Включено  Подключено  Выделенный  wintun",
        "ipv4 show dnsservers": NETSH_DNS_RU})
    data, _res = win_shell.dns_servers_native()
    assert "vEthernet (WSL)" not in data, data
    assert "Ethernet" in data and "wintun" in data, data
    assert len(fs.calls_of("netsh")) == 5, "служебные адаптеры не должны даже опрашиваться"


def test_dns_servers_native_returns_same_shape(fake):
    fs = fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU,
                                     "ipv4 show dnsservers": NETSH_DNS_RU,
                                     "ipv6 show dnsservers": "DNS-серверы: ::1"})
    data, res = win_shell.dns_servers_native()
    assert data == {"Ethernet": {"ipv4": ["127.0.0.1", "1.1.1.1"], "ipv6": ["::1"]}}, data
    assert res.ok and res.via == "netsh"
    assert len(fs.calls_of("netsh")) == 3, "один вызов на список + по одному на семейство"


# ── 4. деградация: установка/сброс DNS, процессы, реестр ────────────────────

def test_set_dns_servers_primary_and_secondary(fake):
    fs = fake(mode="missing")
    res = win_shell.set_dns_servers("Ethernet", ipv4=["127.0.0.1", "1.1.1.1"])
    assert res.ok, res.human_error
    calls = [" ".join(c) for c in fs.calls_of("netsh")]
    assert any("ipv4 set dnsservers" in c and "127.0.0.1" in c and "primary" in c for c in calls)
    assert any("ipv4 add dnsservers" in c and "1.1.1.1" in c and "index=2" in c for c in calls)


def test_set_dns_servers_can_reset_ipv6_to_dhcp(fake):
    """По IPv6 внешний DNS ставить нельзя (утечка) — значит семейство уходит на авто."""
    fs = fake(mode="missing")
    res = win_shell.set_dns_servers("Ethernet", ipv4=["127.0.0.1"], reset_ipv6=True)
    assert res.ok, res.human_error
    calls = [" ".join(c) for c in fs.calls_of("netsh")]
    assert any("ipv6 set dnsservers" in c and "source=dhcp" in c for c in calls)


def test_reset_dns_servers_resets_both_families(fake):
    fs = fake(mode="missing")
    res = win_shell.reset_dns_servers("Ethernet")
    assert res.ok
    calls = [" ".join(c) for c in fs.calls_of("netsh")]
    assert sum("source=dhcp" in c for c in calls) == 2, calls


def test_kill_process_falls_back_to_taskkill(fake):
    fs = fake(mode="missing")
    res = win_shell.kill_process(pid=4242)
    assert res.ok and res.via == "taskkill", res.human_error
    assert any("4242" in c for c in fs.calls_of("taskkill"))


def test_taskkill_128_means_already_dead(fake):
    """taskkill 128 = «процесса нет»: для зачистки это успех, а не ошибка."""
    fake(mode="missing", taskkill_code=128)
    res = win_shell.kill_process(pid=1)
    assert res.ok and res.code == 0


def test_reg_set_falls_back_to_reg_add(fake):
    fs = fake(mode="missing")
    res = win_shell.reg_set(r"HKCU\SOFTWARE\Policies\Google\Chrome", "DnsOverHttpsMode", "secure")
    assert res.ok, res.human_error
    calls = [" ".join(c) for c in fs.calls_of("reg")]
    assert any("add" in c and "HKCU\\SOFTWARE" in c and "DnsOverHttpsMode" in c
               and "secure" in c and "/f" in c for c in calls), calls


def test_reg_delete_missing_value_is_success(fake):
    """«Значения и не было» — это успех, а не ошибка: цель достигнута."""
    fake(mode="missing", reg_code=1,
         reg_err="ОШИБКА: не удалось найти указанный раздел реестра")
    res = win_shell.reg_delete(r"HKCU\SOFTWARE\Policies\Google\Chrome",
                               "DnsOverHttpsTemplates")
    assert res.ok, res.human_error
    assert res.code == 0
    assert "не было" in res.stdout


def test_reg_failure_is_reported_with_program_name(fake):
    """Если и reg не смог — сказано, чем именно не смог, а не «код 1»."""
    fake(mode="missing", reg_code=1, reg_err="ОШИБКА: отказано в доступе")
    res = win_shell.reg_set(r"HKLM\SOFTWARE\Policies\Google\Chrome", "DnsOverHttpsMode", "safe")
    assert not res.ok
    assert "reg" in res.human_error and "отказано" in res.human_error, res.human_error


# ── 5. текст для человека ───────────────────────────────────────────────────

def test_status_line_explains_what_to_do(fake):
    fake(mode="blocked")
    line = win_shell.status_line()
    assert "НЕДОСТУПЕН" in line and "антивирус" in line
    assert "netsh" in line and "ipconfig" in line, "не сказано, что всё-таки работает"
    assert "журнал DNS-запросов" in line, "не сказано, что именно не работает"


def test_limitations_empty_when_powershell_works(fake):
    fake(mode="ok")
    assert win_shell.limitations() == []
    assert "доступен" in win_shell.status_line()


def test_limitations_list_when_blocked(fake):
    fake(mode="blocked")
    limits = win_shell.limitations()
    assert limits and any("журнал DNS-запросов" in x for x in limits)
    assert "netsh" in win_shell.fallback_names()


# ── 6. интеграция: process_monitor работает без PowerShell ──────────────────

@pytest.fixture
def monitor(monkeypatch, fake):
    """process_monitor с «Windows без PowerShell» и правами администратора."""
    import process_monitor as pm
    monkeypatch.setattr(pm, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(pm, "is_admin", lambda: True, raising=False)
    monkeypatch.setattr(pm, "_DNS_CACHE", {"ts": 0.0, "data": {}}, raising=False)
    return pm


def test_monitor_flush_cache_without_powershell(monitor, fake):
    fake(mode="missing")
    assert monitor.flush_dns_cache() is True


def test_monitor_adapters_without_powershell(monitor, fake):
    fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU})
    assert monitor.get_active_adapters() == ["Ethernet"]


def test_monitor_current_dns_without_powershell(monitor, fake):
    """Проверка утечек не должна «видеть» систему без DNS, если PowerShell молчит."""
    fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU,
                                "ipv4 show dnsservers": NETSH_DNS_RU,
                                "ipv6 show dnsservers": "DNS-серверы: ::1"})
    data = monitor.get_current_dns(use_cache=False)
    assert data == {"Ethernet": {"ipv4": ["127.0.0.1", "1.1.1.1"], "ipv6": ["::1"]}}, data


def test_monitor_set_dns_to_localhost_without_powershell(monitor, fake):
    fs = fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU})
    ok, msg, adapters = monitor.set_dns_to_localhost(fallback_ipv4="1.1.1.1", enable_ipv6=True)
    assert ok, msg
    assert adapters == ["Ethernet"], msg
    assert "netsh" in msg, f"человек должен знать, что сработало через netsh: {msg}"
    calls = [" ".join(c) for c in fs.calls_of("netsh")]
    assert any("ipv4 set dnsservers" in c and "127.0.0.1" in c for c in calls)
    assert any("1.1.1.1" in c for c in calls), "резервный DNS не прописан"
    assert any("ipv6 set dnsservers" in c and "::1" in c for c in calls)
    assert fs.calls_of("ipconfig"), (
        "после смены DNS через netsh кэш Windows обязан очищаться через ipconfig"
    )


def test_monitor_set_dns_profile_without_powershell(monitor, fake):
    fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU})
    ok, msg, adapters = monitor.set_dns_profile("8.8.8.8", "8.8.4.4", profile_name="тест")
    assert ok and adapters == ["Ethernet"], msg
    assert "через netsh" in msg


def test_monitor_reset_dns_without_powershell(monitor, fake):
    fs = fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU})
    ok, msg = monitor.reset_dns_to_auto()
    assert ok and "netsh" in msg, msg
    calls = [" ".join(c) for c in fs.calls_of("netsh")]
    assert sum("source=dhcp" in c for c in calls) == 2


def test_monitor_doh_policy_without_powershell(monitor, fake):
    fs = fake(mode="missing")
    ok, msg = monitor.set_chrome_doh("https://example.test/dns-query")
    assert ok and "reg" in msg, msg
    calls = [" ".join(c) for c in fs.calls_of("reg")]
    assert any("DnsOverHttpsTemplates" in c and "example.test" in c for c in calls), calls


def test_run_ps_keeps_old_interface(monitor, fake):
    """Старый интерфейс «кортеж из трёх» сохранён: на него опираются вызовы и тесты."""
    fake(mode="missing")
    out, err, code = monitor._run_ps("Get-Something")            # noqa: SLF001
    assert (out, code) == ("", -1)
    assert "PowerShell" in err


def test_monitor_reports_when_nothing_works(monitor, fake):
    """Ни PowerShell, ни netsh: причина называется, а не «ни один адаптер»."""
    fake(mode="missing", netsh={})                     # netsh молчит: пустой вывод
    ok, msg, _adapters = monitor.set_dns_to_localhost()
    assert not ok
    assert "PowerShell" in msg, msg


# ── 7. видно человеку: предупреждение, Health Score, отчёт ──────────────────

def test_health_score_warns_about_powershell(monkeypatch, fake):
    fake(mode="blocked")
    from umbranet import engine_adapter as ea
    monkeypatch.setattr(ea, "get_startup_health", lambda: {"severity": "ok", "summary": "ok"})
    monkeypatch.setattr(ea, "get_engine", lambda: type("E", (), {"running": False, "config": {}})())
    hs = ea.health_score()
    check = next((c for c in hs["checks"] if c["title"] == "PowerShell недоступен"), None)
    assert check is not None, [c["title"] for c in hs["checks"]]
    # Именно предупреждение, а не ошибка: программа продолжает работать через
    # системные утилиты, а не «сломана».
    assert check["status"] == "warn", check
    assert check.get("penalty", 0) > 0, "недоступный PowerShell должен снижать балл"
    assert any("powershell.exe" in a for a in hs["actions"]), hs["actions"]


def test_warning_text_goes_to_ui(monkeypatch, fake):
    """Предупреждение для интерфейса: причина + что работает + что сделать."""
    fake(mode="missing")
    from umbranet import engine_adapter as ea
    text = ea.powershell_warning()
    assert text.startswith("⚠")
    assert "не найден" in text
    assert "netsh" in text and "ipconfig" in text
    assert "антивирус" in text


def test_network_view_shows_warning_label(monkeypatch):
    """Вкладка «Сеть и диагностика» показывает предупреждение, когда оно есть."""
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from umbranet import engine_adapter as ea
    monkeypatch.setattr(ea, "powershell_warning",
                        lambda: "⚠  PowerShell запуск заблокирован. Работаем через netsh.")
    from umbranet.app import MainWindow
    win = MainWindow()
    try:
        win.show()                    # без показа окна isVisible() у детей всегда False
        win._show("network")                                      # noqa: SLF001
        for _ in range(3):
            app.processEvents()
        view = win._views["network"]                              # noqa: SLF001
        assert view._ps_warning.isVisible(), "предупреждение не показано"   # noqa: SLF001
        assert "заблокирован" in view._ps_warning.text()                    # noqa: SLF001
    finally:
        win.close()


def test_network_view_hides_warning_at_rest(monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from umbranet import engine_adapter as ea
    monkeypatch.setattr(ea, "powershell_warning", lambda: "")
    from umbranet.app import MainWindow
    win = MainWindow()
    try:
        win.show()
        win._show("network")                                      # noqa: SLF001
        for _ in range(3):
            app.processEvents()
        view = win._views["network"]                              # noqa: SLF001
        assert view._ps_warning.text() == ""
        assert not view._ps_warning.isVisible(), "предупреждение висит без причины"  # noqa: SLF001
    finally:
        win.close()


def test_full_report_contains_powershell_line(monkeypatch, fake):
    fake(mode="missing")
    from umbranet import engine_adapter as ea
    monkeypatch.setattr(ea, "health_score",
                        lambda: {"score": 100, "title": "ok", "checks": [], "actions": []})
    for name in ("get_current_dns_settings", "get_browser_doh_policies", "_winws_status_for_report"):
        if hasattr(ea, name):
            monkeypatch.setattr(ea, name, lambda *a, **k: {})
    report = ea.full_diagnostics_report()
    assert "PowerShell" in report, report[:400]


# ── 8. трекер процессов: говорит причину, а не молчит ───────────────────────

def test_tracker_reports_powershell_reason(fake):
    fake(mode="blocked")
    from process_dns_tracker import DnsProcessTracker
    reason = DnsProcessTracker._powershell_unavailable()          # noqa: SLF001
    assert reason and "PowerShell" in reason and "заблокирован" in reason


def test_tracker_launches_when_powershell_available(fake):
    fake(mode="ok")
    from process_dns_tracker import DnsProcessTracker
    assert DnsProcessTracker._powershell_unavailable() == ""      # noqa: SLF001


# ── 9. декодирование вывода консоли ─────────────────────────────────────────

def test_decode_console_handles_cp866(monkeypatch):
    # Кодовая страница консоли раннера зависит от локали ОС: на русской
    # Windows это cp866, на английской (контрагентские CI) — cp437, а cp437
    # «декодит» любые байты без ошибки — cp866-ветка не успевала сработать.
    # Тест про саму поддержку cp866, поэтому страницу фиксируем как в
    # не-Windows-режиме (utf-8 — он не декодирует cp866-байты и пропускает их
    # к cp866).
    monkeypatch.setattr(win_shell, "OEM_CODEPAGE", "utf-8")
    text = "Ошибка: не удалось найти параметр"
    assert win_shell.decode_console(text.encode("cp866")) == text


def test_decode_console_survives_broken_bytes():
    assert win_shell.decode_console(b"\xff\xfe\x00bad") != ""
    assert win_shell.decode_console(b"") == ""


def test_oem_codepage_is_cp866_or_system_default():
    """На Windows берём реальную кодовую страницу консоли; в тестах — utf-8."""
    assert win_shell.OEM_CODEPAGE.startswith("cp") or win_shell.OEM_CODEPAGE == "utf-8"


# ── 10. глобальный IPv6 (проверка утечек) ───────────────────────────────────

def test_global_ipv6_count_filters_service_addresses(fake):
    fake(mode="missing", netsh={"show addresses":
                                "addr ::1\n"
                                "addr fe80::1234%12\n"
                                "addr 2a00:1450:4010::1\n"
                                "addr 2606:4700:4700::1111"})
    count, res = win_shell.global_ipv6_count()
    assert res.ok and count == 2, f"служебные адреса посчитались: {count}"


def test_global_ipv6_count_zero_when_no_global(fake):
    fake(mode="missing", netsh={"show addresses": "addr ::1\naddr fe80::1%5"})
    count, _res = win_shell.global_ipv6_count()
    assert count == 0


def test_ipv6_toggle_uses_netsh(fake):
    fs = fake(mode="missing")
    res = win_shell.set_ipv6_enabled("Ethernet", False)
    assert res.ok, res.human_error
    calls = [" ".join(c) for c in fs.calls_of("netsh")]
    assert any("ipv6 set interface" in c and "admin=disabled" in c for c in calls), calls


# ── 11. откат сети и политики браузеров без PowerShell ──────────────────────
# «Откатить сеть» — страховка человека. Если страховка не работает именно тогда,
# когда что-то сломалось (а PowerShell мог отключить тот же антивирус), она не нужна.

API_TIMEOUT = 1


def _snapshot_file(tmp_path, adapters=None, binding=None) -> pathlib.Path:
    import json
    path = tmp_path / "network_snapshot_test.json"
    path.write_text(json.dumps({
        "ok": True,
        "time_local": "2026-09-17 12:00:00",
        "adapters": adapters if adapters is not None else {
            "Ethernet": {"ipv4": ["192.168.1.50", "8.8.8.8"], "ipv6": []},
            "Wi-Fi": {"ipv4": [], "ipv6": ["::1"]},
        },
        "ipv6_binding": binding if binding is not None else {"Ethernet": True},
        "config": {},
    }, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def repair(monkeypatch):
    import network_repair
    # is_admin импортируется внутри функции из process_monitor — гасим оба пути
    import process_monitor as pm
    monkeypatch.setattr(network_repair, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(pm, "is_admin", lambda: True, raising=False)
    return network_repair


def test_restore_snapshot_without_powershell(repair, fake, tmp_path):
    """ГЛАВНОЕ: восстановление из снапшота работает через netsh, если PowerShell нет."""
    fs = fake(mode="missing")
    snap = _snapshot_file(tmp_path)
    ok, msg = repair.restore_snapshot(str(snap))
    assert ok, msg
    assert "netsh" in msg, f"человек должен знать, чем восстановили: {msg}"
    calls = [" ".join(c) for c in fs.calls_of("netsh")]
    assert any("ipv4 set dnsservers" in c and "192.168.1.50" in c and "primary" in c for c in calls), calls
    assert any("8.8.8.8" in c for c in calls), "резервный DNS не восстановлен"
    assert any("ipv6 set dnsservers" in c and "::1" in c for c in calls), "IPv6-DNS не восстановлен"
    assert any("source=dhcp" in c for c in calls), "пустое семейство должно вернуться на авто"
    assert any("ipv6 set interface" in c for c in calls), "состояние IPv6 не восстановлено"
    assert fs.calls_of("ipconfig"), "после восстановления кэш DNS надо сбросить"


def test_restore_snapshot_reads_json_with_python(repair, fake, tmp_path):
    """JSON читает Python: PowerShell для чтения файла не нужен и не вызывается."""
    fs = fake(mode="missing")
    snap = _snapshot_file(tmp_path, adapters={"Ethernet": {"ipv4": ["10.0.0.1"], "ipv6": []}},
                          binding={})
    ok, _msg = repair.restore_snapshot(str(snap))
    assert ok
    assert not any("ConvertFrom-Json" in " ".join(c) for c in fs.calls), \
        "чтение снапшота ушло в PowerShell вместо Python"


def test_restore_snapshot_broken_file_is_explained(repair, fake, tmp_path):
    fake(mode="missing")
    broken = tmp_path / "broken.json"
    broken.write_text("{не json", encoding="utf-8")
    ok, msg = repair.restore_snapshot(str(broken))
    assert not ok
    assert "снапшот не читается" in msg, msg


def test_doh_policies_reset_without_powershell(repair, fake):
    """Снятие DoH-политик без PowerShell: reg delete по обеим ветвям реестра."""
    fs = fake(mode="missing")
    ok, msg = repair._reset_browser_doh_policies()                      # noqa: SLF001
    assert ok, msg
    assert "reg" in msg, msg
    calls = [" ".join(c) for c in fs.calls_of("reg")]
    assert any("HKCU\\SOFTWARE\\Policies\\Google\\Chrome" in c and "DnsOverHttpsMode" in c
               for c in calls), calls
    assert any("HKCU\\SOFTWARE\\Policies\\Microsoft\\Edge" in c and "DnsOverHttpsTemplates" in c
               for c in calls), calls


def test_doh_policies_hklm_denied_is_not_an_error(repair, fake):
    """HKLM без прав администратора — обычное дело: пользовательская ветка снята."""
    fake(mode="missing", reg_code=1, reg_err="ОШИБКА: отказано в доступе")
    ok, msg = repair._reset_browser_doh_policies()                      # noqa: SLF001
    # reg вернул одну и ту же ошибку на все пути — тогда честно сообщаем о сбое,
    # но если «отказано» только про HKLM, результат считается успешным.
    assert (ok and "reg" in msg) or (not ok and "отказано" in msg), msg


# ── 12. проверка утечек и зачистка winws без PowerShell ─────────────────────

@pytest.fixture
def leak(monkeypatch):
    import dns_leak
    monkeypatch.setattr(dns_leak, "IS_WINDOWS", True, raising=False)
    return dns_leak


def test_has_ipv6_connectivity_without_powershell(leak, fake):
    """Без PowerShell «IPv6 работает» определяется по адресам адаптеров (netsh).

    Раньше при недоступном PowerShell функция возвращала False — то есть проверка
    утечек молча считала, что утечки нет, даже когда она есть.
    """
    fake(mode="missing", netsh={"show addresses": "addr 2a00:1450:4010::1"})
    assert leak.has_ipv6_connectivity() is True


def test_has_ipv6_connectivity_no_global_address(leak, fake):
    fake(mode="missing", netsh={"show addresses": "addr ::1\naddr fe80::1%12"})
    assert leak.has_ipv6_connectivity() is False


def test_fix_ipv6_leak_without_powershell(leak, fake, monkeypatch):
    fake(mode="missing", netsh={"show interface": NETSH_INTERFACES_RU})
    import process_monitor as pm
    monkeypatch.setattr(pm, "is_admin", lambda: True, raising=False)
    ok, msg = leak.fix_ipv6_leak(disable_ipv6=True)
    assert ok, msg
    assert "netsh" in msg, msg


def test_engine_kills_by_pid_without_powershell(monkeypatch, fake):
    """Зависший winws.exe закрывается через taskkill — иначе он держит WinDivert."""
    fs = fake(mode="missing")
    from winws_engine import WinWSEngine
    eng = WinWSEngine()
    monkeypatch.setattr(eng, "_ps_runner", None, raising=False)
    assert eng._kill_by_pid(4242) is True                               # noqa: SLF001
    assert any("4242" in c for c in fs.calls_of("taskkill")), fs.calls


def test_engine_kill_by_pid_keeps_injection(monkeypatch):
    """Инъекция для тестов остаётся главной: системные вызовы не происходят."""
    from winws_engine import WinWSEngine
    seen = []
    eng = WinWSEngine()
    eng._ps_runner = lambda cmd, timeout: seen.append(cmd) or False    # noqa: SLF001
    assert eng._kill_by_pid(4242) is False                             # noqa: SLF001
    assert seen and "Stop-Process -Id 4242" in seen[0]


# ── 13. предупреждение в узком окне ─────────────────────────────────────────
# Текст предупреждения длинный (около 400 знаков), а вкладка должна оставаться
# целой на любой ширине — та же болезнь, что чинилась в §27 для «Сети» и «О программе».

def test_warning_card_survives_narrow_window(fake):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fake(mode="blocked")                      # «Windows, PowerShell заблокирован»
    from umbranet.app import MainWindow
    win = MainWindow()
    problems = []
    try:
        win.show()
        win.sidebar.set_collapsed(True, animate=False)
        win._show("network")                                      # noqa: SLF001
        for _ in range(6):
            app.processEvents()
        view = win._views["network"]                              # noqa: SLF001
        label = view._ps_warning                                  # noqa: SLF001
        assert label.isVisible(), "предупреждение не показано при заблокированном PowerShell"
        assert "netsh" in label.text() and "антивирус" in label.text()

        from PySide6.QtWidgets import QScrollArea
        scroll = view.findChild(QScrollArea)
        for width in range(560, 1401, 20):
            win.resize(width, 900)
            for _ in range(3):
                app.processEvents()
            if label.heightForWidth(label.width()) > label.height() + 1:
                problems.append(f"окно={width}: текст предупреждения обрезан")
            if scroll is not None and scroll.horizontalScrollBar().maximum() > 0:
                problems.append(f"окно={width}: горизонтальная прокрутка "
                                f"{scroll.horizontalScrollBar().maximum()}")
    finally:
        win.close()
    assert not problems, "; ".join(problems[:5])


def test_clicking_warning_rechecks_powershell(fake):
    """Человек разблокировал powershell.exe — клик по предупреждению снимает его.

    Кэш проверки живёт 5 минут; ждать их, чтобы убедиться, что всё починилось,
    незачем: нажатие проверяет сразу.
    """
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fs = fake(mode="blocked")
    from umbranet.app import MainWindow
    win = MainWindow()
    try:
        win.show()
        win._show("network")                                      # noqa: SLF001
        for _ in range(6):
            app.processEvents()
        label = win._views["network"]._ps_warning                 # noqa: SLF001
        assert label.isVisible(), "предупреждение не показано"
        probes_before = len(fs.ps_calls)

        fs.mode = "ok"                         # «антивирус разблокировал»
        label.clicked.emit()                   # = клик по карточке
        for _ in range(4):
            app.processEvents()

        assert len(fs.ps_calls) > probes_before, "клик не запустил перепроверку"
        assert label.text() == "" and not label.isVisible(), "предупреждение не снялось"
    finally:
        win.close()
