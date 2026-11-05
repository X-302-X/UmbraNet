import io
import json
import threading
import urllib.request
from unittest.mock import Mock

import pytest

import dns_server as dns
from core.dpi import domain_updater as du


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # No process trackers, GUI, or background networking from __init__.
    eng = dns.UmbraNetDNS.__new__(dns.UmbraNetDNS)
    eng.config = {"routed_subscriptions": ["https://example.org/list?token=SECRET"],
                  "subscribed_domains_set": {"old.example"}}
    eng._subscriptions_lock = threading.Lock()
    eng._subscription_state_lock = threading.RLock()
    eng._subscriptions_generation = 0
    eng._subscription_cache_valid = True
    eng._cfg_ref = [eng.config]
    eng.cache = Mock()
    monkeypatch.setattr(dns, "save_config", lambda cfg: None)
    monkeypatch.setattr(dns, "_CORE_DIR", str(tmp_path))
    monkeypatch.setenv("UMBRANET_ROLLBACK_ROOT", str(tmp_path / "recovery"))
    (tmp_path / "subscribed_domains_cache.json").write_text('["old.example"]')
    return eng


def response(raw):
    result = io.BytesIO(raw)
    result.headers = {}
    return result


def test_real_subscription_refresh_updates_config_and_atomic_cache(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k:
                        response(b"# comment\n0.0.0.0 new.example\nother.example\n"))
    assert engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"new.example", "other.example"}
    path = tmp_path / "subscribed_domains_cache.json"
    assert set(json.loads(path.read_text())) == {"new.example", "other.example"}
    backup_path = tmp_path / "recovery" / "network" / "subscribed_domains_cache.json.bak"
    assert json.loads(backup_path.read_text()) == ["old.example"]
    assert not path.with_suffix(".json.bak").exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("body", [b"", b"<html>error.example</html>", b"# temporarily unavailable"])
def test_empty_or_invalid_subscription_preserves_cache(engine, tmp_path, monkeypatch, body):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(body))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert json.loads((tmp_path / "subscribed_domains_cache.json").read_text()) == ["old.example"]


def test_subscription_failure_preserves_cache_without_logging_tokens(engine, monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=OSError("url?token=SECRET")))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert "SECRET" not in caplog.text


def test_manual_and_scheduled_subscription_refresh_do_not_overlap(engine, monkeypatch):
    entered, release, done, duplicate = (threading.Event() for _ in range(4))
    calls = []
    def fetch():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return True
    monkeypatch.setattr(engine, "_fetch_subscriptions", fetch)
    results = []
    engine.update_subscriptions_async(lambda *r: (results.append(r), done.set()))
    assert entered.wait(2)
    assert not engine._update_subscriptions()
    engine.update_subscriptions_async(lambda *r: (results.append(r), duplicate.set()))
    assert duplicate.wait(2)
    assert results == [(False, 1)]
    release.set()
    assert done.wait(2)
    assert results[-1] == (True, 1)
    assert len(calls) == 1
    assert engine._subscriptions_lock.acquire(False)
    engine._subscriptions_lock.release()


def test_changed_subscriptions_discard_old_download(engine, tmp_path, monkeypatch):
    def fetch(*args, **kwargs):
        engine.config["routed_subscriptions"] = ["https://example.org/new-list"]
        return response(b"stale.example")
    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert json.loads((tmp_path / "subscribed_domains_cache.json").read_text()) == ["old.example"]


def test_all_subscription_sources_must_succeed(engine, tmp_path, monkeypatch):
    engine.config["routed_subscriptions"].append("https://example.org/other")
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=[response(b"new.example"), TimeoutError]))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}


def test_explicit_subscription_removal_clears_cache(engine, tmp_path):
    engine.config["routed_subscriptions"] = []
    assert engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == set()
    assert not (tmp_path / "subscribed_domains_cache.json").exists()


def strategy(tmp_path, **extra):
    data = {"id": "test", "remote_url": "https://example.org/domains?SECRET", "args": ["--original"]}
    data.update(extra)
    path = tmp_path / "test.json"
    path.write_text(json.dumps(data))
    return path


class StreamingResponse:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        pass
    def iter_content(self, chunk_size):
        yield self.data


def test_domain_cache_refresh_never_changes_strategy(tmp_path, monkeypatch):
    path = strategy(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(du.requests, "get", lambda *a, **k: StreamingResponse(b"example.org\nExample.NET\n"))
    assert du.update_all_strategies(tmp_path)
    assert path.read_bytes() == before
    assert (tmp_path / "remote_hostlist_test.txt").read_text() == "example.net\nexample.org\n"
    assert not list(tmp_path.glob("*.tmp"))


# Explicit IDs are essential: pytest otherwise puts the entire 5 MiB body in
# the node ID and streams it as one enormous line in CI's verbose output.
@pytest.mark.parametrize("raw", [
    pytest.param(b"", id="empty"),
    pytest.param(b"<html>example.org</html>", id="html"),
    pytest.param(b"--dpi-desync=fake", id="winws-option"),
    pytest.param(b"x" * (du.MAX_BYTES + 1), id="over-5MiB"),
])
def test_bad_remote_hostlist_preserves_working_file(tmp_path, monkeypatch, raw):
    strategy(tmp_path)
    cache = tmp_path / "remote_hostlist_test.txt"
    cache.write_text("old.example\n")
    monkeypatch.setattr(du.requests, "get", lambda *a, **k: StreamingResponse(raw))
    assert not du.update_all_strategies(tmp_path)
    assert cache.read_text() == "old.example\n"


def test_strategy_id_cannot_escape_cache_directory(tmp_path, monkeypatch):
    strategy(tmp_path, id="../../escape")
    fetch = Mock()
    monkeypatch.setattr(du.requests, "get", fetch)
    assert not du.update_all_strategies(tmp_path)
    fetch.assert_not_called()


def test_current_uz_strategy_has_no_remote_fetch(tmp_path, monkeypatch):
    strategy(tmp_path, remote_url=None)
    fetch = Mock()
    monkeypatch.setattr(du.requests, "get", fetch)
    assert du.update_all_strategies(tmp_path)
    fetch.assert_not_called()


def test_hostlist_updates_guard_duplicate_requests(tmp_path):
    with du._lock:
        assert not du.update_all_strategies(tmp_path)


def test_subscription_url_lines_are_not_mistaken_for_comments(engine, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k:
                        response(b"// comment\nhttps://example.org/path // comment\n"))
    assert engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"example.org"}


@pytest.mark.parametrize("urls", [[], ["https://example.org/replacement"]], ids=["remove", "replace"])
def test_reload_during_cache_serialization_discards_download(engine, tmp_path, monkeypatch, urls):
    """Reload after fetching, during disk preparation: old pre-write guard missed it."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(b"stale.example"))
    monkeypatch.setattr(dns, "load_config", lambda: {"routed_subscriptions": list(urls)})
    dump = dns.json.dump
    def reload_during_write(*args, **kwargs):
        dump(*args, **kwargs)
        engine.reload_config()
    monkeypatch.setattr(dns.json, "dump", reload_during_write)
    assert not engine._update_subscriptions()
    assert engine.config["routed_subscriptions"] == urls
    assert engine.config["subscribed_domains_set"] == set()
    assert engine._cfg_ref[0] is engine.config
    assert not (tmp_path / "subscribed_domains_cache.json").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_commit_and_reload_are_serialized(engine, tmp_path, monkeypatch):
    """An actual second thread cannot reload between os.replace and memory publication."""
    entered, release, reload_started = (threading.Event() for _ in range(3))
    results, errors = [], []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(b"new.example"))
    monkeypatch.setattr(dns, "load_config", lambda: {"routed_subscriptions": []})
    replace = dns.os.replace
    def paused_replace(*args):
        entered.set()
        assert release.wait(5)
        replace(*args)
    monkeypatch.setattr(dns.os, "replace", paused_replace)
    def refresh():
        try:
            results.append(engine._update_subscriptions())
        except BaseException as exc:
            errors.append(exc)
    def reload():
        try:
            reload_started.set()
            engine.reload_config()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=refresh, daemon=True)
    reloader = threading.Thread(target=reload, daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        # Deterministic check, not a timing-sensitive sleep/negative event wait.
        acquired = engine._subscription_state_lock.acquire(blocking=False)
        if acquired:
            engine._subscription_state_lock.release()
        assert not acquired, "The state lock must cover both disk and memory publication"
        reloader.start()
        assert reload_started.wait(3)
    finally:
        release.set()
        worker.join(5)
        if reloader.ident is not None:
            reloader.join(5)
    assert not worker.is_alive() and not reloader.is_alive()
    assert not errors
    assert results == [True]
    assert engine.config["routed_subscriptions"] == []
    assert engine.config["subscribed_domains_set"] == set()
    assert not (tmp_path / "subscribed_domains_cache.json").exists()


def test_ui_removal_during_serialization_invalidates_download(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(b"stale.example"))
    url = engine.config["routed_subscriptions"][0]
    dump = dns.json.dump
    def remove_during_write(*args, **kwargs):
        dump(*args, **kwargs)
        assert engine.change_subscription(url, remove=True)
    monkeypatch.setattr(dns.json, "dump", remove_during_write)
    assert not engine._update_subscriptions()
    assert engine.config["routed_subscriptions"] == []
    assert engine.config["subscribed_domains_set"] == set()
    assert not (tmp_path / "subscribed_domains_cache.json").exists()


def test_remove_and_readd_same_url_invalidates_old_generation(engine, tmp_path, monkeypatch):
    url = engine.config["routed_subscriptions"][0]
    def fetch(*args, **kwargs):
        assert engine.change_subscription(url, remove=True)
        assert engine.change_subscription(url)
        return response(b"stale.example")
    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    assert not engine._update_subscriptions()
    assert engine.config["routed_subscriptions"] == [url]
    assert engine.config["subscribed_domains_set"] == set()
    assert not list(tmp_path.glob("*.tmp"))


def test_source_edit_does_not_wait_for_network(engine, monkeypatch):
    entered, release, done = (threading.Event() for _ in range(3))
    url = engine.config["routed_subscriptions"][0]
    def fetch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return response(b"stale.example")
    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    results, edits = [], []
    engine.update_subscriptions_async(lambda *r: (results.append(r), done.set()))
    def edit():
        edits.append(engine.change_subscription(url, remove=True))
    worker = threading.Thread(target=edit, daemon=True)
    try:
        assert entered.wait(3)
        worker.start()
        worker.join(2)
        assert not worker.is_alive(), "Editing sources must not wait for the download lock"
        assert edits == [True]
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(5)
        assert done.wait(5)
    assert results == [(False, 0)]


def test_disabled_subscriptions_do_not_load_leftover_cache(engine):
    engine.config["routed_subscriptions"] = []
    engine.load_subscribed_domains()
    assert engine.config["subscribed_domains_set"] == set()


def test_failed_unlink_cannot_reload_obsolete_cache_in_session(engine, monkeypatch):
    url = engine.config["routed_subscriptions"][0]
    monkeypatch.setattr(dns.os, "remove", Mock(side_effect=PermissionError("locked cache")))
    assert engine.change_subscription(url, remove=True)
    assert engine.change_subscription("https://example.org/new-list")
    engine.load_subscribed_domains()
    assert engine.config["subscribed_domains_set"] == set()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(b"fresh.example"))
    assert engine._update_subscriptions()
    assert engine._subscription_cache_valid
    engine.load_subscribed_domains()
    assert engine.config["subscribed_domains_set"] == {"fresh.example"}


def test_failed_cache_replace_preserves_old_memory_and_disk(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(b"new.example"))
    monkeypatch.setattr(dns.os, "replace", Mock(side_effect=PermissionError("locked cache")))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert json.loads((tmp_path / "subscribed_domains_cache.json").read_text()) == ["old.example"]
    assert not list(tmp_path.glob("*.tmp"))


def test_unchanged_config_reload_keeps_offline_cache(engine, monkeypatch):
    urls = list(engine.config["routed_subscriptions"])
    monkeypatch.setattr(dns, "load_config", lambda: {"routed_subscriptions": urls})
    engine.reload_config()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert engine._cfg_ref[0] is engine.config


def test_duplicate_source_edits_are_noops(engine):
    url = engine.config["routed_subscriptions"][0]
    assert not engine.change_subscription(url)
    assert not engine.change_subscription("https://example.org/missing", remove=True)
    assert engine._subscriptions_generation == 0
    assert engine.config["subscribed_domains_set"] == {"old.example"}


def test_adding_source_keeps_working_cache_if_new_source_is_offline(engine, tmp_path, monkeypatch):
    assert engine.change_subscription("https://example.org/new-list")
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=[response(b"new.example"), TimeoutError]))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert json.loads((tmp_path / "subscribed_domains_cache.json").read_text()) == ["old.example"]


def test_partial_source_removal_discards_aggregate_and_offline_cannot_restore_it(engine, tmp_path, monkeypatch):
    old_url = engine.config["routed_subscriptions"][0]
    assert engine.change_subscription("https://example.org/remaining")
    assert engine.change_subscription(old_url, remove=True)
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=TimeoutError))
    assert not engine._update_subscriptions()
    engine.load_subscribed_domains()
    assert engine.config["subscribed_domains_set"] == set()
    assert not (tmp_path / "subscribed_domains_cache.json").exists()
