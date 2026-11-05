"""Windows PAC snapshot, apply, verification, and crash-safe restoration.

Only values in the current user's Internet Settings key are touched.  The
manager records the exact previous value and registry type before PAC is set;
therefore a manual proxy, a pre-existing PAC URL, a bypass list, and auto-detect
return exactly as they were after UmbraNet stops.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

try:
    from rollback_paths import rollback_subdir
except ImportError:  # Package import outside UmbraNet's historical flat core path.
    from core.rollback_paths import rollback_subdir

log = logging.getLogger("UmbraNet.SystemProxy")

IS_WINDOWS = sys.platform == "win32"
INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
PROXY_VALUE_NAMES = ("AutoConfigURL", "AutoDetect", "ProxyEnable", "ProxyServer", "ProxyOverride")
SNAPSHOT_VERSION = 1
SNAPSHOT_ENV = "UMBRANET_PROXY_SNAPSHOT"


@dataclass(frozen=True, slots=True)
class RegistryValue:
    exists: bool
    value: Any = None
    value_type: int = 1

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: object) -> "RegistryValue":
        if not isinstance(raw, dict):
            raise ValueError("некорректная запись снимка proxy")
        exists = bool(raw.get("exists", False))
        value_type = raw.get("value_type", 1)
        if isinstance(value_type, bool):
            raise ValueError("некорректный тип значения proxy")
        try:
            value_type = int(value_type)
        except (TypeError, ValueError):
            raise ValueError("некорректный тип значения proxy") from None
        return cls(exists=exists, value=raw.get("value"), value_type=value_type)


class RegistryStore(Protocol):
    def read(self, name: str) -> RegistryValue: ...
    def write(self, name: str, entry: RegistryValue) -> None: ...
    def delete(self, name: str) -> None: ...


class WindowsRegistryStore:
    """Minimal typed HKCU registry access, isolated for deterministic tests."""

    def __init__(self):
        if not IS_WINDOWS:
            raise RuntimeError("PAC доступен только в Windows")
        try:
            import winreg
        except ImportError as exc:  # pragma: no cover - Windows-only import
            raise RuntimeError(f"winreg недоступен: {exc}") from exc
        self._winreg = winreg

    def _open(self, access: int):
        return self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, access)

    def read(self, name: str) -> RegistryValue:
        try:
            with self._open(self._winreg.KEY_READ) as key:
                value, value_type = self._winreg.QueryValueEx(key, name)
                return RegistryValue(True, value, int(value_type))
        except FileNotFoundError:
            return RegistryValue(False)

    def write(self, name: str, entry: RegistryValue) -> None:
        if not entry.exists:
            self.delete(name)
            return
        with self._open(self._winreg.KEY_SET_VALUE) as key:
            self._winreg.SetValueEx(key, name, 0, int(entry.value_type), entry.value)

    def delete(self, name: str) -> None:
        try:
            with self._open(self._winreg.KEY_SET_VALUE) as key:
                self._winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return


def _default_snapshot_path() -> Path:
    override = str(os.environ.get(SNAPSHOT_ENV, "") or "").strip()
    if override:
        return Path(override).expanduser()
    return rollback_subdir("proxy") / "proxy_snapshot.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _notify_wininet() -> bool:
    """Tell WinINET clients to reread Internet Settings after registry changes."""
    if not IS_WINDOWS:
        return False
    try:  # pragma: no cover - Windows API
        wininet = ctypes.WinDLL("wininet", use_last_error=True)
        changed = bool(wininet.InternetSetOptionW(0, 39, None, 0))
        refreshed = bool(wininet.InternetSetOptionW(0, 37, None, 0))
        if not (changed and refreshed):
            log.warning("WinINET did not confirm a proxy settings refresh")
        return changed and refreshed
    except Exception as exc:  # pragma: no cover - Windows API
        log.warning("Не удалось обновить WinINET proxy-настройки: %s", exc)
        return False


class SystemProxyManager:
    """Transactional owner of the UmbraNet PAC setting for one runtime session."""

    def __init__(self, *, snapshot_path: str | Path | None = None,
                 registry: RegistryStore | None = None,
                 notifier=None,
                 windows: bool | None = None):
        self._windows = IS_WINDOWS if windows is None else bool(windows)
        self._snapshot_path = Path(snapshot_path) if snapshot_path is not None else _default_snapshot_path()
        self._registry = registry
        self._notifier = notifier or _notify_wininet

    @property
    def snapshot_path(self) -> Path:
        return self._snapshot_path

    def _store(self) -> RegistryStore:
        if not self._windows:
            raise RuntimeError("PAC доступен только в Windows")
        if self._registry is None:
            self._registry = WindowsRegistryStore()
        return self._registry

    def _read_snapshot(self) -> dict[str, Any] | None:
        path = self._snapshot_path
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"снимок proxy повреждён: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != SNAPSHOT_VERSION:
            raise RuntimeError("снимок proxy имеет неизвестный формат")
        values = raw.get("values")
        if not isinstance(values, dict):
            raise RuntimeError("снимок proxy не содержит значения реестра")
        for name in PROXY_VALUE_NAMES:
            RegistryValue.from_mapping(values.get(name))
        return raw

    def _snapshot(self, pac_url: str) -> dict[str, Any]:
        store = self._store()
        values = {name: store.read(name).to_mapping() for name in PROXY_VALUE_NAMES}
        data = {
            "version": SNAPSHOT_VERSION,
            "owner": "umbranet-transport",
            "created_at": time.time(),
            "pac_url": pac_url,
            "values": values,
        }
        _atomic_write_json(self._snapshot_path, data)
        return data

    def _snapshot_for_apply(self, pac_url: str) -> tuple[bool, str]:
        existing = self._read_snapshot()
        if existing is None:
            self._snapshot(pac_url)
            return True, "исходные proxy-настройки сохранены"
        existing_url = str(existing.get("pac_url") or "")
        current = self.current_pac_url()
        if current == existing_url:
            return True, "используется существующий снимок активного PAC"
        # A stale snapshot cannot safely describe today's settings.  It is safe
        # to discard only when the old UmbraNet PAC is not currently installed.
        try:
            self._snapshot_path.unlink()
        except OSError as exc:
            return False, f"не удалось заменить устаревший снимок proxy: {exc}"
        self._snapshot(pac_url)
        return True, "устаревший снимок заменён текущими proxy-настройками"

    def current_pac_url(self) -> str:
        try:
            entry = self._store().read("AutoConfigURL")
        except Exception:
            return ""
        return str(entry.value or "").strip() if entry.exists else ""

    @staticmethod
    def _valid_loopback_pac_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1"}
            and port is not None
            and 1 <= port <= 65535
            and not parsed.username
            and not parsed.password
        )

    def apply_pac(self, pac_url: str) -> tuple[bool, str]:
        """Atomically preserve current settings, then select exactly one PAC URL."""
        url = str(pac_url or "").strip()
        if not self._valid_loopback_pac_url(url):
            return False, "PAC UmbraNet должен указывать на loopback HTTP URL"
        try:
            store = self._store()
            saved, saved_message = self._snapshot_for_apply(url)
            if not saved:
                return False, saved_message
            # REG_SZ = 1, REG_DWORD = 4.  Keep manual proxy values intact but
            # disabled; restoration returns their exact original state.
            store.write("ProxyEnable", RegistryValue(True, 0, 4))
            store.write("AutoDetect", RegistryValue(True, 0, 4))
            store.write("AutoConfigURL", RegistryValue(True, url, 1))
            if not self._notifier():
                raise RuntimeError("Windows не подтвердил обновление WinINET proxy-настроек")
            if self.current_pac_url() != url:
                raise RuntimeError("Windows не подтвердил установку PAC UmbraNet")
            return True, saved_message
        except Exception as exc:
            # A partial registry write is worse than a failed Start: return the
            # saved user state before the caller closes the local gateway.
            rollback_ok, rollback_message = self.restore()
            suffix = "" if rollback_ok else f"; откат proxy не удался: {rollback_message}"
            return False, str(exc) + suffix

    def restore(self) -> tuple[bool, str]:
        """Restore the snapshot and remove it only after a successful registry write."""
        if not self._windows:
            return False, "PAC доступен только в Windows"
        try:
            snapshot = self._read_snapshot()
            if snapshot is None:
                return True, "снимок proxy отсутствует: восстанавливать нечего"
            store = self._store()
            values = snapshot["values"]
            for name in PROXY_VALUE_NAMES:
                entry = RegistryValue.from_mapping(values[name])
                if entry.exists:
                    store.write(name, entry)
                else:
                    store.delete(name)
            if not self._notifier():
                return False, "Windows не подтвердил обновление WinINET proxy-настроек"
            mismatched = [
                name for name in PROXY_VALUE_NAMES
                if store.read(name) != RegistryValue.from_mapping(values[name])
            ]
            if mismatched:
                return False, "Windows не подтвердил восстановление proxy: " + ", ".join(mismatched)
            self._snapshot_path.unlink(missing_ok=True)
            return True, "исходные proxy/PAC-настройки восстановлены"
        except Exception as exc:
            return False, str(exc)

    def status(self) -> dict[str, object]:
        snapshot = None
        error = ""
        try:
            snapshot = self._read_snapshot()
        except Exception as exc:
            error = str(exc)
        return {
            "windows": self._windows,
            "snapshot_path": str(self._snapshot_path),
            "snapshot_exists": snapshot is not None,
            "snapshot_pac_url": str((snapshot or {}).get("pac_url") or ""),
            "current_pac_url": self.current_pac_url() if self._windows else "",
            "error": error,
        }


def restore_proxy_snapshot() -> tuple[bool, str]:
    """Crash-recovery entry point used by the watchdog process."""
    return SystemProxyManager().restore()
