"""Notification-only GitHub release checker. No downloads or code execution."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

from packaging.version import InvalidVersion

from core.app_version import parse_app_version
from core.diagnostics import log_recoverable

log = logging.getLogger("UmbraNet.UpdateChecker")
RELEASES_API = "https://api.github.com/repos/X-302-X/UmbraNet/releases"
RELEASES_PAGE = "https://github.com/X-302-X/UmbraNet/releases"
CHECK_INTERVAL = 86_400
RETRY_INTERVAL = 3_600
MAX_RESPONSE = 2 * 1024 * 1024


@dataclass(frozen=True)
class UpdateResult:
    state: str = "idle"
    version: str = ""
    url: str = ""
    message: str = "Проверка обновлений ещё не выполнялась."


def select_release(releases: list, current_version: str,
                   include_prereleases: bool = False) -> UpdateResult:
    """Pick by version, not GitHub order; reject drafts and invalid tags."""
    current = parse_app_version(current_version).value
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft", False):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or len(tag) > 100:
            continue
        try:
            parsed = parse_app_version(tag)
            version = parsed.value
        except InvalidVersion:
            continue
        if not include_prereleases and (
            release.get("prerelease", False) or version.is_prerelease or version.is_devrelease
        ):
            continue
        # Local builds are not a public release channel.
        if version.local is not None:
            continue
        candidates.append((version, tag, parsed.display))
    if not candidates:
        return UpdateResult("no_releases", message="В выбранном канале пока нет релизов.")
    version, tag, display = max(candidates)
    if version <= current:
        return UpdateResult("current", message="Новых версий в выбранном канале нет.")
    # Do not trust html_url from remote JSON. Open only this repository on HTTPS.
    url = RELEASES_PAGE + "/tag/" + quote(tag, safe="")
    return UpdateResult("available", display, url,
                        f"Доступна версия {display}. Установка — вручную со страницы релиза.")


def fetch_releases(include_prereleases: bool) -> list:
    # /latest avoids missing stable releases behind a long series of prereleases.
    url = RELEASES_API + ("?per_page=100" if include_prereleases else "/latest")
    request = urllib.request.Request(url, headers={
        "User-Agent": "UmbraNet-UpdateChecker",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []  # normal before the first published release
        raise
    if len(raw) > MAX_RESPONSE:
        raise ValueError("Release response exceeds size limit")
    payload = json.loads(raw)
    if include_prereleases:
        if not isinstance(payload, list):
            raise ValueError("Expected release array")
        return payload
    if not isinstance(payload, dict) or not isinstance(payload.get("tag_name"), str):
        raise ValueError("Expected release object")
    return [payload]


class UpdateChecker:
    """Thread-safe state polled by Qt; worker never touches widgets.

    One bounded daemon worker, monotonic 24h interval, 1h retry on error. Channel
    changes discard an in-flight result rather than showing a stale prerelease.
    """
    def __init__(self, current_version: str, *, include_prereleases: bool = False):
        parse_app_version(current_version)  # validate both short and legacy labels
        self.current_version = current_version
        self._include_prereleases = bool(include_prereleases)
        self._lock = threading.Lock()
        self._result = UpdateResult()
        self._busy = False
        self._generation = 0
        self._next_due = 0.0

    @property
    def include_prereleases(self) -> bool:
        with self._lock:
            return self._include_prereleases

    @property
    def result(self) -> UpdateResult:
        with self._lock:
            return self._result

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def set_channel(self, include_prereleases: bool) -> None:
        with self._lock:
            if self._include_prereleases == bool(include_prereleases):
                return
            self._include_prereleases = bool(include_prereleases)
            self._generation += 1
            self._next_due = 0.0
            self._result = UpdateResult()

    def check_async(self, *, force: bool = False) -> bool:
        with self._lock:
            if self._busy or (not force and time.monotonic() < self._next_due):
                return False
            self._busy = True
            generation = self._generation
            channel = self._include_prereleases
            self._result = UpdateResult("checking", message="Проверяем GitHub Releases…")
        try:
            threading.Thread(target=self._run, args=(generation, channel),
                             name="UmbraNet-UpdateChecker", daemon=True).start()
        except Exception as exc:
            log_recoverable(log, "Не удалось запустить проверку релизов", exc)
            with self._lock:
                self._busy = False
                self._next_due = time.monotonic() + RETRY_INTERVAL
                self._result = UpdateResult("error", message="Не удалось запустить проверку обновлений.")
            return False
        return True

    def _run(self, generation: int, channel: bool) -> None:
        try:
            result = select_release(fetch_releases(channel), self.current_version, channel)
        except Exception as exc:
            log_recoverable(log, "Проверка GitHub Releases недоступна", exc)
            result = UpdateResult("error", message=(
                "Не удалось проверить обновления (сеть или лимит GitHub). "
                "Работа UmbraNet не затронута; повторим позже."
            ))
        with self._lock:
            self._busy = False
            if generation != self._generation:
                return
            self._result = result
            self._next_due = time.monotonic() + (
                RETRY_INTERVAL if result.state == "error" else CHECK_INTERVAL
            )
