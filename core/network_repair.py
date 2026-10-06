"""
UmbraNet — Network Repair Engine v1
===================================

Безопасная автопочинка DNS/IPv6:
  1) создаёт snapshot текущих DNS-настроек активных адаптеров;
  2) применяет мягкий ремонт: DNS -> 127.0.0.1 / ::1 + fallback;
  3) очищает DNS-кэш;
  4) повторно проверяет утечки;
  5) возвращает подробный report, который можно показать в UI/скопировать.

Важно: это НЕ отключает IPv6 и НЕ делает winsock reset. Только мягкий уровень.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from config_utils import _to_bool

log = logging.getLogger("UmbraNet.NetworkRepair")

IS_WINDOWS = sys.platform == "win32"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = PROJECT_ROOT / "backups" / "network"
LAST_REPORT_PATH = BACKUP_DIR / "last_repair_report.json"


def _now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _win_shell():
    """Ленивый импорт обёртки системных вызовов (пункт H4)."""
    import win_shell
    return win_shell


def _default_runner():
    from process_monitor import _run_ps
    return _run_ps


def _default_dns_getter():
    from process_monitor import get_current_dns
    return get_current_dns


# Адреса, которые UmbraNet прописывает сам. Если они уже стоят в системе —
# значит мы (или предыдущая копия) уже вмешались в DNS, и снимать «снапшот
# как было» поздно: мы записали бы в него собственный 127.0.0.1 и потом
# «восстановили» бы интернет в никуда.
LOOPBACK_DNS = ("127.0.0.1", "::1")


def dns_already_localhost(config: dict | None = None, dns_getter=None) -> bool:
    """True, если системный DNS уже указывает на наш локальный сервер.

    Нужен как предохранитель перед съёмом снапшота: снапшот снимается только
    с ЧИСТОГО состояния (до нашего вмешательства).
    """
    if not IS_WINDOWS:
        return False
    try:
        get_dns = dns_getter or _default_dns_getter()
        adapters = get_dns() or {}
    except Exception as exc:
        log.debug("dns_already_localhost: не удалось получить DNS: %s", exc)
        # Не смогли прочитать — считаем, что УЖЕ вмешались. Так безопаснее:
        # лучше не снять снапшот, чем записать в него свой же 127.0.0.1.
        return True
    for _name, data in adapters.items():
        if not isinstance(data, dict):
            continue
        servers = list(data.get("ipv4") or []) + list(data.get("ipv6") or [])
        if any(str(s).strip() in LOOPBACK_DNS for s in servers):
            return True
    return False


def ensure_dns_snapshot(config: dict | None = None,
                        dns_getter=None,
                        ps_runner=None) -> dict | None:
    """Снимает снапшот DNS перед первым вмешательством UmbraNet.

    Возвращает снапшот либо None, если снимать нельзя/не нужно (DNS уже
    наш, либо мы не на Windows). Ошибки не поднимаем — снапшот это
    страховка, а не обязательный шаг запуска.
    """
    if not IS_WINDOWS:
        return None
    try:
        if dns_already_localhost(config, dns_getter=dns_getter):
            log.debug("ensure_dns_snapshot: DNS уже наш — снапшот не снимаем")
            return None
        snap = snapshot_network(config, dns_getter=dns_getter, ps_runner=ps_runner)
        if snap.get("ok"):
            log.info("Снапшот DNS перед запуском сохранён: %s", snap.get("path"))
            return snap
        log.warning("Снапшот DNS не сохранён: %s", snap.get("error"))
    except Exception as exc:
        log.warning("ensure_dns_snapshot: %s", exc)
    return None


def _ensure_backup_dir() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, data: dict) -> None:
    _ensure_backup_dir()
    # Stage 2: атомарная запись через tmp + replace (защита от битого JSON при краше)
    tmp = path.with_suffix(path.suffix + f".{int(time.time()*1000)}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def list_snapshots() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("network_snapshot_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def prune_old_snapshots(keep: int = 10) -> int:
    """Удаляет старые снимки, оставляя последние `keep`. Возвращает число удалённых."""
    snaps = list_snapshots()
    if len(snaps) <= keep:
        return 0
    removed = 0
    for p in snaps[keep:]:
        try:
            p.unlink()
            removed += 1
        except Exception as exc:
            log.debug("prune snapshot %s failed: %s", p, exc)
    if removed:
        log.info("prune_old_snapshots: удалено %d старых снимков, осталось %d", removed, keep)
    return removed


def latest_snapshot() -> Path | None:
    snaps = list_snapshots()
    return snaps[0] if snaps else None


def snapshot_network(config: dict | None = None, dns_getter=None, ps_runner=None) -> dict:
    """Снимает snapshot DNS-настроек активных адаптеров и сохраняет в файл."""
    if not IS_WINDOWS:
        snap = {
            "ok": False,
            "error": "Только Windows",
            "timestamp": time.time(),
            "path": "",
            "adapters": {},
            "config": config or {},
        }
        return snap

    get_dns = dns_getter or _default_dns_getter()
    run = ps_runner or _default_runner()
    adapters = get_dns()

    # Дополнительно сохраняем статус IPv6 binding, чтобы потом понимать контекст.
    bindings: dict[str, bool] = {}
    try:
        cmd = (
            "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | ForEach-Object { "
            "  $b = Get-NetAdapterBinding -Name $_.Name -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue; "
            "  $_.Name + '|' + [string]($b.Enabled) "
            "}"
        )
        stdout, _stderr, code = run(cmd, timeout=15)
        if code == 0 and stdout:
            for line in stdout.splitlines():
                if "|" in line:
                    name, enabled = line.split("|", 1)
                    bindings[name.strip()] = enabled.strip().lower() == "true"
    except Exception as exc:
        log.debug("snapshot ipv6 binding failed: %s", exc)

    snap = {
        "ok": True,
        "timestamp": time.time(),
        "time_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        "adapters": adapters,
        "ipv6_binding": bindings,
        "config": {
            "dpi_mode": (config or {}).get("dpi_mode", "off"),
            "dpi_strategy": (config or {}).get("dpi_strategy", "uz1"),
            "enable_ipv6": (config or {}).get("enable_ipv6", True),
            "fallback_dns": (config or {}).get("fallback_dns", ""),
            "fallback_dns6": (config or {}).get("fallback_dns6", ""),
            "xbox_dns_mode": (config or {}).get("xbox_dns_mode", ""),
        },
    }
    path = BACKUP_DIR / f"network_snapshot_{_now_stamp()}.json"
    # Сначала добавляем путь, затем пишем snapshot один раз. Раньше файл
    # сериализовался дважды без необходимости, что было заметно на больших
    # списках адаптеров и оставляло короткое промежуточное состояние.
    snap["path"] = str(path)
    _write_json(path, snap)
    # Stage 2: ротация — не даём папке backups/network разрастаться бесконечно
    try:
        prune_old_snapshots(keep=10)
    except Exception:
        pass
    return snap


def _dns_check(config: dict, server_running: bool, dpi_running: bool = False, dns_getter=None, ps_runner=None) -> dict:
    try:
        from dns_leak import check_dns_leak
        return check_dns_leak(
            config or {},
            server_running=server_running,
            dpi_running=dpi_running,
            ps_runner=ps_runner,
            dns_getter=dns_getter,
        )
    except Exception as exc:
        return {
            "status": "unknown",
            "title": f"Ошибка проверки: {exc}",
            "details": [],
            "can_fix": False,
            "dns_leak": False,
            "dpi_issue": False,
        }


def repair_soft(config: dict, server_running: bool = True, dpi_running: bool = False,
                dns_getter=None, ps_runner=None) -> dict:
    """Мягкая автопочинка DNS/IPv6 с snapshot и верификацией."""
    report: dict[str, Any] = {
        "ok": False,
        "timestamp": time.time(),
        "time_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": "soft",
        "steps": [],
        "snapshot": {},
        "before": {},
        "after": {},
        "actions": [],
        "errors": [],
    }

    if not IS_WINDOWS:
        report["errors"].append("Автопочинка доступна только в Windows")
        _write_json(LAST_REPORT_PATH, report)
        return report

    try:
        from process_monitor import flush_dns_cache, is_admin, set_dns_to_localhost
        if not is_admin():
            report["errors"].append("Требуются права администратора")
            _write_json(LAST_REPORT_PATH, report)
            return report

        # Снимаем снапшот только с ЧИСТОГО состояния (P0-2). Если DNS уже
        # указывает на нас (ремонт запущен во время работы UmbraNet), обычный
        # snapshot_network запечатлел бы НАШ 127.0.0.1 как «DNS пользователя» —
        # и restore_user_dns при остановке вернул бы его же: обход формально
        # выключен, а настройки DNS навсегда захвачены (работает как клин).
        if dns_already_localhost(config, dns_getter=dns_getter):
            snap = {"ok": True, "path": "", "skipped": "DNS уже указывает на UmbraNet"}
            report["steps"].append("Snapshot пропущен: DNS уже наш — не портим снапшот пользователя")
        else:
            snap = snapshot_network(config, dns_getter=dns_getter, ps_runner=ps_runner)
            report["steps"].append(f"Snapshot создан: {snap.get('path') or 'нет файла'}")
        report["snapshot"] = snap

        before = _dns_check(config, server_running=server_running, dpi_running=dpi_running,
                            dns_getter=dns_getter, ps_runner=ps_runner)
        report["before"] = before
        report["steps"].append(f"До ремонта: {before.get('status')} — {before.get('title')}")

        if not server_running:
            report["errors"].append("UmbraNet DNS-сервер не запущен. Сначала нажмите «Старт», затем запускайте автопочинку.")
            report["steps"].append("STOP: ремонт отменён, чтобы не направить DNS на неработающий 127.0.0.1")
            _write_json(LAST_REPORT_PATH, report)
            return report

        ok, msg, adapters = set_dns_to_localhost(
            fallback_ipv4=(config or {}).get("fallback_dns", "1.1.1.1"),
            fallback_ipv6=(config or {}).get("fallback_dns6", ""),
            enable_ipv6=_to_bool((config or {}).get("enable_ipv6", True), True),
        )
        report["actions"].append({"action": "set_dns_to_localhost", "ok": bool(ok), "message": msg, "adapters": adapters})
        report["steps"].append(("OK" if ok else "FAIL") + f": DNS -> localhost: {msg}")
        if not ok:
            report["errors"].append(msg)

        cache_ok = flush_dns_cache()
        report["actions"].append({"action": "flush_dns_cache", "ok": bool(cache_ok)})
        report["steps"].append("OK: DNS-кэш очищен" if cache_ok else "WARN: DNS-кэш не очищен")

        # Дадим Windows применить DNS перед повторной проверкой.
        time.sleep(0.8)
        after = _dns_check(config, server_running=server_running, dpi_running=dpi_running,
                           dns_getter=dns_getter, ps_runner=ps_runner)
        report["after"] = after
        report["steps"].append(f"После ремонта: {after.get('status')} — {after.get('title')}")

        report["ok"] = bool(ok) and after.get("status") == "ok"
        if not report["ok"] and after.get("dns_leak"):
            report["errors"].append("DNS/IPv6-утечка осталась после мягкой починки")
        _write_json(LAST_REPORT_PATH, report)
        return report
    except Exception as exc:
        report["errors"].append(str(exc))
        log.exception("repair_soft failed")
        _write_json(LAST_REPORT_PATH, report)
        return report


def restore_snapshot(path: str | None = None, ps_runner=None) -> tuple[bool, str]:
    """Восстанавливает DNS из snapshot. Если path не задан — последний snapshot."""
    if not IS_WINDOWS:
        return False, "Только Windows"
    try:
        from process_monitor import is_admin
        if not is_admin():
            return False, "Требуются права администратора"
        snap_path = Path(path) if path else latest_snapshot()
        if not snap_path or not snap_path.exists():
            return False, "Snapshot не найден"

        run = ps_runner or _default_runner()
        # PowerShell читает JSON сам — так мы не мучаемся с escaping имён адаптеров.
        ps_path = str(snap_path).replace("'", "''")
        cmd = rf'''
$snap = Get-Content -Raw -LiteralPath '{ps_path}' | ConvertFrom-Json;
$ok=@(); $err=@();
foreach ($p in $snap.adapters.PSObject.Properties) {{
  $name = [string]$p.Name;
  $dns = $p.Value;
  try {{
    $adapter = Get-NetAdapter -Name $name -ErrorAction Stop;
    $idx = $adapter.InterfaceIndex;
    $v4 = @($dns.ipv4);
    $v6 = @($dns.ipv6);
    if ($v4.Count -gt 0) {{
      Set-DnsClientServerAddress -InterfaceIndex $idx -ServerAddresses $v4 -ErrorAction Stop;
    }} else {{
      netsh interface ipv4 set dnsservers name="$name" source=dhcp | Out-Null;
    }}
    if ($v6.Count -gt 0) {{
      Set-DnsClientServerAddress -InterfaceIndex $idx -ServerAddresses $v6 -ErrorAction Stop;
    }} else {{
      netsh interface ipv6 set dnsservers name="$name" source=dhcp | Out-Null;
    }}
    $ok += $name;
  }} catch {{
    $err += ($name + ': ' + $_.Exception.Message);
  }}
}}
try {{
  if ($snap.ipv6_binding) {{
    foreach ($p in $snap.ipv6_binding.PSObject.Properties) {{
      $name = [string]$p.Name;
      $enabled = [System.Convert]::ToBoolean($p.Value);
      try {{
        if ($enabled) {{
          Enable-NetAdapterBinding -Name $name -ComponentID ms_tcpip6 -ErrorAction Stop;
        }} else {{
          Disable-NetAdapterBinding -Name $name -ComponentID ms_tcpip6 -ErrorAction Stop;
        }}
      }} catch {{ $err += ($name + ' IPv6 binding: ' + $_.Exception.Message) }}
    }}
  }}
}} catch {{ $err += ('IPv6 binding restore: ' + $_.Exception.Message) }}
Clear-DnsClientCache;
Write-Output ('OK:' + ($ok -join ','));
if ($err) {{ Write-Output ('ERR:' + ($err -join ';')) }}
'''
        stdout, stderr, _code = run(cmd, timeout=30)
        ok_names, err_names = [], []
        for line in stdout.splitlines():
            if line.startswith("OK:"):
                ok_names = [x for x in line[3:].split(",") if x]
            elif line.startswith("ERR:"):
                err_names = [x for x in line[4:].split(";") if x]
        if ok_names:
            msg = f"Восстановлено: {', '.join(ok_names)}"
            if err_names:
                msg += f"; ошибки: {'; '.join(err_names)}"
            return True, msg

        # PowerShell не сработал — возвращаем настройки через netsh (пункт H4).
        # «Откатить сеть» — это страховка человека: она обязана работать и тогда,
        # когда PowerShell заблокирован, иначе страховки просто нет.
        if ps_runner is None:
            native_ok, native_err = _restore_native(snap_path)
            if native_ok:
                msg = f"Восстановлено (через netsh): {', '.join(native_ok)}"
                if native_err:
                    msg += f"; ошибки: {'; '.join(native_err)}"
                log.info("restore_snapshot выполнен через netsh: %s", native_ok)
                return True, msg
            if native_err:
                return False, ("Не удалось восстановить: "
                               f"PowerShell — {'; '.join(err_names) or stderr or 'не ответил'}; "
                               f"netsh — {'; '.join(native_err)}")
        return False, f"Не удалось восстановить: {'; '.join(err_names) or stderr or stdout or 'нет данных'}"
    except Exception as exc:
        return False, f"Ошибка восстановления: {exc}"


def _restore_native(snap_path) -> tuple[list, list]:
    """Возвращает DNS и IPv6 из снапшота через системные утилиты (пункт H4).

    JSON читает Python (PowerShell для чтения файла вообще не нужен), а настройки
    ставятся через netsh. Возвращает (восстановленные адаптеры, ошибки).
    """
    shell = _win_shell()
    try:
        data = json.loads(Path(snap_path).read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        return [], [f"снапшот не читается: {exc}"]

    adapters = data.get("adapters") or {}
    ok_names, err_names = [], []
    for name, dns in adapters.items():
        ipv4 = list((dns or {}).get("ipv4") or [])
        ipv6 = list((dns or {}).get("ipv6") or [])
        if not ipv4 and not ipv6:
            res = shell.reset_dns_servers(name)
        else:
            res = shell.set_dns_servers(name, ipv4=ipv4 or None, ipv6=ipv6 or None,
                                        reset_ipv4=not ipv4, reset_ipv6=not ipv6)
        if res.ok:
            ok_names.append(name)
        else:
            err_names.append(f"{name}: {res.human_error}")

    for name, enabled in (data.get("ipv6_binding") or {}).items():
        res = shell.set_ipv6_enabled(name, bool(enabled))
        if not res.ok:
            err_names.append(f"{name} (IPv6): {res.human_error}")

    if ok_names:
        shell.flush_dns_cache()
    return ok_names, err_names


def snapshot_info(path: str | None = None) -> dict:
    """Информация о снапшоте для UI: существует ли, когда снят, сколько адаптеров.

    Нужна, чтобы показать пользователю, ЧТО именно вернёт кнопка «Откатить сеть»
    («DNS от 15.09.2026 14:02, 2 адаптера»), а не молчаливую кнопку.
    """
    info = {"exists": False, "path": "", "time_local": "", "adapters": 0}
    try:
        snap_path = Path(path) if path else latest_snapshot()
        if not snap_path or not snap_path.exists():
            return info
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        adapters = data.get("adapters") or {}
        info.update({
            "exists": True,
            "path": str(snap_path),
            "time_local": str(data.get("time_local") or ""),
            "adapters": len(adapters) if isinstance(adapters, dict) else 0,
        })
    except Exception as exc:
        log.debug("snapshot_info: %s", exc)
    return info


def _snapshot_is_poisoned(adapters: dict) -> bool:
    """True, если в снапшоте лежит НАШ DNS (127.0.0.1 / ::1), а не пользователя.

    Такой снапшот появляется, если он был снят во время работы UmbraNet (сбой
    прошлых версий или внешний инструмент). «Восстановление» из него вернуло бы
    в систему захваченный DNS: мёртвый 127.0.0.1 первым сервером и fallback
    вторым — интернет вроде бы работает, но настройки навсегда заклинены.
    """
    if not isinstance(adapters, dict):
        return False
    for data in adapters.values():
        if not isinstance(data, dict):
            continue
        servers = [str(s).strip() for s in (data.get("ipv4") or [])]
        servers += [str(s).strip() for s in (data.get("ipv6") or [])]
        if any(s in LOOPBACK_DNS for s in servers):
            return True
    return False


def restore_user_dns(path: str | None = None, ps_runner=None) -> tuple[bool, str]:
    """Возвращает DNS в состояние ДО UmbraNet: снапшот → иначе «Авто» (DHCP).

    ЭТО ГЛАВНАЯ функция отката. Она используется и кнопкой «Откатить сеть»,
    и при остановке/выходе, и watchdog'ом при краше.

    Почему так, а не просто reset_dns_to_auto():
      У пользователя мог быть прописан СВОЙ DNS (например 1.1.1.1 или
      корпоративный). Старый код всегда сбрасывал на DHCP и безвозвратно
      терял эти настройки. Теперь сначала пробуем вернуть снапшот.

    Почему при неудаче всё равно сбрасываем на DHCP:
      Оставленный 127.0.0.1 при выключенном UmbraNet = полное отсутствие
      интернета. Это худшее из возможных состояний, поэтому «Авто» лучше,
      чем мёртвый локальный адрес.
    """
    if not IS_WINDOWS:
        return False, "Только Windows"

    # 1) Снапшот: то, что было до нашего вмешательства.
    try:
        snap_path = Path(path) if path else latest_snapshot()
        if snap_path and snap_path.exists():
            adapters = {}
            try:
                adapters = (json.loads(snap_path.read_text(encoding="utf-8"))
                            .get("adapters") or {})
            except Exception:
                adapters = {}
            if adapters:
                if _snapshot_is_poisoned(adapters):
                    log.warning(
                        "Снапшот %s содержит наш собственный DNS — восстанавливать его нельзя, идём на DHCP",
                        snap_path.name,
                    )
                else:
                    ok, msg = restore_snapshot(str(snap_path), ps_runner=ps_runner)
                    if ok:
                        # Самопроверка (поле 2026-10-06): netsh может отчитаться
                        # об успехе, а DNS остаться нашим. Только СВЕЖЕЕ чтение
                        # решает; если яд остался — не врём, идём на DHCP.
                        try:
                            from process_monitor import get_current_dns, invalidate_dns_cache
                            invalidate_dns_cache()
                            fresh = get_current_dns(use_cache=False) or {}
                            poisoned = any(
                                any(str(s).strip() in LOOPBACK_DNS
                                    for s in ((d or {}).get("ipv4") or []) + ((d or {}).get("ipv6") or []))
                                for d in fresh.values() if isinstance(d, dict)
                            )
                        except Exception:
                            poisoned = False
                        if poisoned:
                            log.warning("Снапшот применён, но DNS всё ещё наш (127.0.0.1/::1) — идём на DHCP")
                        else:
                            log.info("DNS восстановлен из снапшота: %s", msg)
                            return True, msg
                    else:
                        log.warning("Восстановление из снапшота не удалось (%s) — падаем на DHCP", msg)
            else:
                # Снапшот пустой: у пользователя и был DHCP, восстанавливать нечего.
                log.info("В снапшоте нет статических DNS — возвращаем DHCP")
        else:
            log.info("Снапшота нет — возвращаем DHCP")
    except Exception as exc:
        log.warning("restore_user_dns: снапшот не сработал: %s", exc)

    # 2) Фолбэк: DHCP. Гарантированно убирает наш 127.0.0.1.
    try:
        from process_monitor import reset_dns_to_auto, invalidate_dns_cache
        ok, msg = reset_dns_to_auto()
        invalidate_dns_cache()
        if ok:
            log.info("DNS возвращён на DHCP: %s", msg)
        else:
            log.warning("Не удалось вернуть DNS на DHCP: %s", msg)
        return ok, msg
    except Exception as exc:
        return False, f"Не удалось вернуть DNS: {exc}"


def last_report() -> dict:
    try:
        if LAST_REPORT_PATH.exists():
            return json.loads(LAST_REPORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def format_report(report: dict) -> str:
    if not report:
        return "UmbraNet Network Repair Report\nОтчёта пока нет."
    lines = [
        "UmbraNet Network Repair Report",
        "=" * 44,
        f"time: {report.get('time_local', '')}",
        f"level: {report.get('level', '')}",
        f"ok: {report.get('ok')}",
        f"snapshot: {(report.get('snapshot') or {}).get('path', '')}",
        "",
        "before:",
        f"  status: {(report.get('before') or {}).get('status', '')}",
        f"  title: {(report.get('before') or {}).get('title', '')}",
        "",
        "after:",
        f"  status: {(report.get('after') or {}).get('status', '')}",
        f"  title: {(report.get('after') or {}).get('title', '')}",
        "",
        "steps:",
    ]
    lines.extend(f"  - {x}" for x in report.get("steps", []) or [])
    errors = report.get("errors", []) or []
    if errors:
        lines += ["", "errors:"]
        lines.extend(f"  - {x}" for x in errors)
    return "\n".join(lines)


REPAIR_LEVELS = {
    "soft": {
        "title": "Уровень 1 — мягкая DNS/IPv6 починка",
        "danger": "low",
        "steps": [
            "Создать snapshot текущих DNS-настроек",
            "Проверить DNS/IPv6-утечки до ремонта",
            "Направить IPv4 DNS активных адаптеров на 127.0.0.1 + fallback",
            "Направить IPv6 DNS активных адаптеров на ::1 + fallback, если IPv6 включён",
            "Очистить DNS-кэш Windows",
            "Повторно проверить результат",
        ],
    },
    "browser": {
        "title": "Уровень 2 — DNS + браузерный DoH",
        "danger": "medium",
        "steps": [
            "Выполнить уровень 1",
            "Удалить политики Secure DNS / DoH для Chrome и Edge из HKCU/HKLM, если они заданы",
            "Очистить DNS-кэш Windows",
            "Попросить перезапустить браузер, если политики были изменены",
            "Повторно проверить результат",
        ],
    },
    "aggressive_ipv6": {
        "title": "Уровень 3 — агрессивная IPv6 починка",
        "danger": "high",
        "steps": [
            "Выполнить уровень 1",
            "Если DNS/IPv6-утечка осталась — отключить IPv6 binding на активных адаптерах",
            "Очистить DNS-кэш Windows",
            "Повторно проверить результат",
            "Откат доступен через последний snapshot, включая IPv6 binding",
        ],
    },
}


def repair_plan(level: str = "soft") -> dict:
    level = level if level in REPAIR_LEVELS else "soft"
    data = dict(REPAIR_LEVELS[level])
    data["level"] = level
    return data


def _reset_browser_doh_policies(ps_runner=None) -> tuple[bool, str]:
    """Удаляет политики DoH Chrome/Edge. Не трогает настройки внутри профиля браузера."""
    if not IS_WINDOWS:
        return False, "Только Windows"
    run = ps_runner or _default_runner()
    cmd = r'''
$paths = @(
  'HKCU:\SOFTWARE\Policies\Google\Chrome',
  'HKLM:\SOFTWARE\Policies\Google\Chrome',
  'HKCU:\SOFTWARE\Policies\Microsoft\Edge',
  'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
);
$ok=@(); $err=@();
foreach ($p in $paths) {
  try {
    if (Test-Path $p) {
      Remove-ItemProperty -Path $p -Name 'DnsOverHttpsMode' -ErrorAction SilentlyContinue;
      Remove-ItemProperty -Path $p -Name 'DnsOverHttpsTemplates' -ErrorAction SilentlyContinue;
      $ok += $p;
    }
  } catch { $err += ($p + ': ' + $_.Exception.Message) }
}
Write-Output ('OK:' + ($ok -join ','));
if ($err) { Write-Output ('ERR:' + ($err -join ';')) }
'''
    stdout, stderr, _code = run(cmd, timeout=15)
    ok_items, err_items = [], []
    for line in stdout.splitlines():
        if line.startswith("OK:"):
            ok_items = [x for x in line[3:].split(",") if x]
        elif line.startswith("ERR:"):
            err_items = [x for x in line[4:].split(";") if x]
    if ok_items:
        msg = "DoH-политики Chrome/Edge удалены: " + ", ".join(ok_items)
        if err_items:
            msg += "; ошибки: " + "; ".join(err_items)
        return True, msg
    if err_items:
        return False, "; ".join(err_items)

    # PowerShell не сработал — снимаем те же ключи через reg (пункт H4).
    if ps_runner is None:
        native_ok, native_err = _reset_doh_reg()
        if native_ok:
            msg = "DoH-политики Chrome/Edge удалены (через reg): " + ", ".join(native_ok)
            if native_err:
                msg += "; ошибки: " + "; ".join(native_err)
            return True, msg
        if native_err:
            return False, "; ".join(native_err)
    return True, "DoH-политики Chrome/Edge не были заданы"


def _reset_doh_reg() -> tuple[list, list]:
    """Снимает DoH-политики Chrome/Edge через `reg delete` (путь без PowerShell)."""
    shell = _win_shell()
    paths = [
        r"HKCU\SOFTWARE\Policies\Google\Chrome",
        r"HKLM\SOFTWARE\Policies\Google\Chrome",
        r"HKCU\SOFTWARE\Policies\Microsoft\Edge",
        r"HKLM\SOFTWARE\Policies\Microsoft\Edge",
    ]
    ok_items, err_items = [], []
    for path in paths:
        for name in ("DnsOverHttpsMode", "DnsOverHttpsTemplates"):
            res = shell.reg_delete(path, name)
            if not res.ok:
                # HKLM без прав администратора — обычное дело, это не «поломка»:
                # политику снимает ветка текущего пользователя.
                if "HKLM" in path and ("отказано" in res.human_error.lower()
                                       or "access is denied" in res.human_error.lower()):
                    log.debug("Пропуск %s: %s", path, res.human_error)
                    continue
                err_items.append(f"{path} {name}: {res.human_error}")
                break
        else:
            ok_items.append(path)
    return ok_items, err_items


def repair_with_level(level: str, config: dict, server_running: bool = True, dpi_running: bool = False,
                      dns_getter=None, ps_runner=None) -> dict:
    """Запускает ремонт выбранного уровня."""
    level = level if level in REPAIR_LEVELS else "soft"
    report = repair_soft(config, server_running=server_running, dpi_running=dpi_running,
                         dns_getter=dns_getter, ps_runner=ps_runner)
    report["level"] = level
    report["plan"] = repair_plan(level)

    # Если базовый ремонт даже не стартовал (например сервер остановлен) — не продолжаем.
    if report.get("errors") and not server_running:
        _write_json(LAST_REPORT_PATH, report)
        return report

    if level == "browser":
        ok, msg = _reset_browser_doh_policies(ps_runner=ps_runner)
        report.setdefault("actions", []).append({"action": "reset_browser_doh_policies", "ok": bool(ok), "message": msg})
        report.setdefault("steps", []).append(("OK" if ok else "WARN") + f": браузерный DoH: {msg}")
        try:
            from process_monitor import flush_dns_cache
            flush_dns_cache()
        except Exception:
            pass
        time.sleep(0.5)
        after = _dns_check(config, server_running=server_running, dpi_running=dpi_running,
                           dns_getter=dns_getter, ps_runner=ps_runner)
        report["after"] = after
        report["ok"] = after.get("status") == "ok"
        _write_json(LAST_REPORT_PATH, report)
        return report

    if level == "aggressive_ipv6":
        after = report.get("after") or {}
        if after.get("dns_leak") or after.get("status") in ("risk", "leak"):
            try:
                from dns_leak import fix_ipv6_leak
                ok, msg = fix_ipv6_leak(disable_ipv6=True, ps_runner=ps_runner)
                report.setdefault("actions", []).append({"action": "disable_ipv6_binding", "ok": bool(ok), "message": msg})
                report.setdefault("steps", []).append(("OK" if ok else "FAIL") + f": отключение IPv6: {msg}")
                time.sleep(0.8)
                after2 = _dns_check(config, server_running=server_running, dpi_running=dpi_running,
                                    dns_getter=dns_getter, ps_runner=ps_runner)
                report["after"] = after2
                report["ok"] = bool(ok) and after2.get("status") == "ok"
                if not report["ok"]:
                    report.setdefault("errors", []).append("Агрессивная IPv6 починка не убрала проблему полностью")
            except Exception as exc:
                report.setdefault("errors", []).append(f"Ошибка агрессивной IPv6 починки: {exc}")
                report["ok"] = False
        _write_json(LAST_REPORT_PATH, report)
        return report

    _write_json(LAST_REPORT_PATH, report)
    return report
