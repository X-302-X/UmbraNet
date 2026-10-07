import io
import json
import threading
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import update_checker as uc


def release(tag, **kwargs):
    return {"tag_name": tag, "draft": False, "prerelease": False, **kwargs}


@pytest.mark.parametrize("current,tag,state", [
    ("0.4.0-dev", "v0.4.0", "available"),
    ("0.4.0rc1", "v0.4.0", "available"),
    ("0.4.0", "v0.4.0", "current"),
    ("0.4.1", "v0.4.0", "current"),
    ("0.9.0", "v0.10.0", "available"),
])
def test_version_comparison(current, tag, state):
    result = uc.select_release([release(tag)], current)
    assert result.state == state


def test_stable_channel_filters_drafts_prereleases_and_bad_tags():
    releases = [release("v0.5.0rc1"), release("v0.6.0", prerelease=True),
                release("v0.7.0", draft=True), release("nonsense"), None,
                release("v0.4.0"), release("v99.0+local")]
    result = uc.select_release(releases, "0.3.0")
    assert result.version == "0.4.0"
    assert uc.select_release(releases, "0.3.0", True).version == "0.6.0"


def test_empty_repository_and_no_eligible_releases():
    assert uc.select_release([], "0.4.0-dev").state == "no_releases"
    assert uc.select_release([release("v0.4.0rc1")], "0.4.0-dev").state == "no_releases"


def test_sort_by_version_and_ignore_remote_html_url():
    result = uc.select_release([
        release("v0.10.0", html_url="file:///malicious"), release("v0.9.0")
    ], "0.4.0")
    assert result.url == uc.RELEASES_PAGE + "/tag/v0.10.0"


@pytest.mark.parametrize("code,empty", [(404, True), (403, False), (429, False), (500, False)])
def test_http_statuses(monkeypatch, code, empty):
    monkeypatch.setattr(uc.urllib.request, "urlopen", Mock(side_effect=
        urllib.error.HTTPError(uc.RELEASES_API, code, "test", {}, None)))
    if empty:
        assert uc.fetch_releases(False) == []
    else:
        with pytest.raises(urllib.error.HTTPError):
            uc.fetch_releases(False)


@pytest.mark.parametrize("channel,payload", [(False, release("v0.4.0")), (True, [release("v0.5.0rc1")])])
def test_request_and_response(monkeypatch, channel, payload):
    opened = Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(uc.urllib.request, "urlopen", opened)
    rows = uc.fetch_releases(channel)
    assert len(rows) == 1
    request = opened.call_args.args[0]
    assert request.full_url.endswith("?per_page=100" if channel else "/latest")
    assert opened.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("raw", [
    pytest.param(b"[]", id="array-not-object"),
    pytest.param(b"{}", id="missing-tag"),
    pytest.param(b"<html>error</html>", id="invalid-json"),
    pytest.param(b"x" * (uc.MAX_RESPONSE + 1), id="over-2MiB"),
])
def test_invalid_response_is_not_reported_as_up_to_date(monkeypatch, raw):
    monkeypatch.setattr(uc.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(raw))
    with pytest.raises(ValueError):
        uc.fetch_releases(False)


class InlineThread:
    """Deterministic test double; concurrency is covered with real Events below."""
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


def test_periodic_check_throttle_and_retry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(uc, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(uc, "threading", SimpleNamespace(Lock=threading.Lock, Thread=InlineThread))
    fetch = Mock(side_effect=[TimeoutError, [release("v0.4.0")]])
    monkeypatch.setattr(uc, "fetch_releases", fetch)
    checker = uc.UpdateChecker("0.4.0-dev")
    assert checker.check_async()
    assert checker.result.state == "error"
    assert not checker.check_async()
    now[0] += uc.RETRY_INTERVAL
    assert checker.check_async()
    assert checker.result.state == "available"
    now[0] += uc.CHECK_INTERVAL - 1
    assert not checker.check_async()


def test_inflight_channel_change_discards_result_and_duplicate_check(monkeypatch):
    entered, release_worker = threading.Event(), threading.Event()
    def fetch(channel):
        entered.set()
        assert release_worker.wait(3)
        return [release("v0.5.0rc1")]
    monkeypatch.setattr(uc, "fetch_releases", fetch)
    real_thread = threading.Thread
    threads = []
    def make_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(uc, "threading", SimpleNamespace(Lock=threading.Lock, Thread=make_thread))
    checker = uc.UpdateChecker("0.4.0", include_prereleases=True)
    assert checker.check_async()
    assert entered.wait(2)
    assert not checker.check_async(force=True)
    checker.set_channel(False)
    release_worker.set()
    threads[0].join(2)
    assert checker.result.state == "idle"
    assert not checker.busy
    assert checker.check_async()
    threads[1].join(2)
    assert checker.result.state == "no_releases"


def test_thread_start_failure_is_recoverable(monkeypatch):
    thread = Mock()
    thread.start.side_effect = RuntimeError("no threads")
    monkeypatch.setattr(uc, "threading", SimpleNamespace(Lock=threading.Lock, Thread=lambda **k: thread))
    checker = uc.UpdateChecker("0.4.0")
    assert not checker.check_async()
    assert not checker.busy
    assert checker.result.state == "error"


@pytest.mark.parametrize("current,tag,state", [
    ("26.0.1a", "v26.0.1b", "available"),
    ("26.0.1b", "v26.0.1r", "available"),
    ("26.0.1r", "v26.0.1b", "current"),
    ("26.0.1b", "v26.0.1a", "current"),
    ("26.0.1b", "v26.0.1b", "current"),
    ("26.0.1r", "v26.0.1r", "current"),
    ("26.0.1", "v26.0.1r", "current"),
    ("26.0.1r", "v26.0.1", "current"),
    ("26.0.1b0", "v26.0.1b", "current"),
    ("26.0.1r", "v26.0.2a", "available"),
    ("26.0.9r", "v26.0.10b", "available"),
    ("0.4.0-dev", "v26.0.1b", "available"),
    ("26.0.1b", "v0.4.0", "current"),
])
def test_short_version_ordering(current, tag, state):
    result = uc.select_release([release(tag)], current, include_prereleases=True)
    assert result.state == state


@pytest.mark.parametrize("tag,prerelease", [
    ("v26.0.2a", False), ("v26.0.2b", False),
    ("v26.0.2a", True), ("v26.0.2b", True), ("v26.0.2r", True),
])
def test_stable_channel_honors_suffix_and_github_flag(tag, prerelease):
    assert uc.select_release([release(tag, prerelease=prerelease)], "26.0.1b").state == "no_releases"


def test_beta_user_can_receive_stable_release_without_opt_in():
    result = uc.select_release([release("v26.0.1r")], "26.0.1b")
    assert result.state == "available"
    assert result.version == "26.0.1r"
    assert ".post" not in result.message


def test_short_release_selection_and_notification_keep_public_label():
    releases = [release("v26.0.1r"), release("v26.0.2a", prerelease=True),
                release("v26.0.2b", prerelease=True, html_url="file:///not-a-release"),
                release("v99.0.1r", draft=True), release("not-a-version")]
    result = uc.select_release(releases, "26.0.1b", True)
    assert result.version == "26.0.2b"
    assert "26.0.2b0" not in result.message
    assert "26.0.2b." in result.message
    assert result.url == uc.RELEASES_PAGE + "/tag/v26.0.2b"
    assert uc.select_release(releases, "26.0.1b").version == "26.0.1r"


@pytest.mark.parametrize("version", ["26.0.1a", "26.0.1b", "26.0.1r", "0.4.0-dev"])
def test_checker_constructor_accepts_public_and_legacy_versions(version):
    checker = uc.UpdateChecker(version)
    assert checker.current_version == version


def test_async_checker_handles_stable_r_tag(monkeypatch):
    from umbranet import __version__
    monkeypatch.setattr(uc, "threading", SimpleNamespace(Lock=threading.Lock, Thread=InlineThread))
    monkeypatch.setattr(uc, "fetch_releases", lambda channel: [release("v26.0.8r")])
    checker = uc.UpdateChecker(__version__)
    assert checker.check_async()
    assert not checker.busy
    assert checker.result.state == "available"
    assert checker.result.version == "26.0.8r"
