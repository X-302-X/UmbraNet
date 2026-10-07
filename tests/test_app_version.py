"""Public a/b/r labels must not inherit PEP 440's meaning of 'r' (post)."""
from types import SimpleNamespace

from packaging.version import InvalidVersion, Version
import pytest

from core.app_version import parse_app_version
from umbranet import __version__


@pytest.mark.parametrize("text,label,value", [
    ("26.0.1a", "26.0.1a", "26.0.1a0"),
    ("v26.0.1b", "26.0.1b", "26.0.1b0"),
    ("26.0.1r", "26.0.1r", "26.0.1"),
    (" V26.0.1B ", "26.0.1b", "26.0.1b0"),
    ("v026.00.01R", "26.0.1r", "26.0.1"),
    ("0.4.0-dev", "0.4.0.dev0", "0.4.0.dev0"),
    ("v0.4.0-rc.1", "0.4.0rc1", "0.4.0rc1"),
    ("v0.4.0", "0.4.0", "0.4.0"),
])
def test_display_and_comparison_are_separate(text, label, value):
    parsed = parse_app_version(text)
    assert parsed.display == label
    assert parsed.value == Version(value)


def test_stage_order_numeric_order_and_legacy_transition():
    ordered = ["0.4.0-dev", "0.4.0rc1", "0.4.0", "26.0.1a", "26.0.1b",
               "26.0.1rc1", "26.0.1r", "26.0.2a", "26.0.10b", "26.1.0a", "27.0.1a"]
    values = [parse_app_version(text).value for text in ordered]
    assert all(before < after for before, after in zip(values, values[1:]))


def test_r_is_plain_stable_not_post_release():
    value = parse_app_version("v26.0.1r").value
    assert value == Version("26.0.1")
    assert not value.is_prerelease
    assert not value.is_postrelease
    assert not value.is_devrelease


@pytest.mark.parametrize("text", ["26.0.1a", "26.0.1b"])
def test_alpha_and_beta_are_prereleases(text):
    assert parse_app_version(text).value.is_prerelease


@pytest.mark.parametrize("text", [None, 26, "", " ", "26.0.1z", "26.0.1r/../../x", "v26.0.1r<script>"])
def test_invalid_labels_are_rejected(text):
    with pytest.raises(InvalidVersion):
        parse_app_version(text)


def test_overlong_version_is_rejected():
    with pytest.raises(InvalidVersion):
        parse_app_version("1" * 101)


def test_current_version_keeps_public_spelling():
    parsed = parse_app_version(__version__)
    assert __version__ == "26.0.7b"
    assert parsed.display == __version__
    assert parsed.value.is_prerelease


def test_network_report_uses_the_same_public_version(monkeypatch):
    from umbranet import engine_adapter as ea

    results = {
        "get_engine": SimpleNamespace(config={}, running=False),
        "health_score": {"score": 100, "title": "test", "checks": []},
        "_winws_status_for_report": {},
        "is_real_engine": False,
        "is_admin": False,
        "powershell_status_line": "PowerShell: test",
        "get_current_mode": "dns_only",
        "get_active_dns_profile": {},
        "get_current_dns_settings": {},
        "get_browser_doh_policies": {},
        "network_repair_report_text": "test",
    }
    for name, value in results.items():
        monkeypatch.setattr(ea, name, lambda *a, _value=value, **k: _value)
    report = ea.full_diagnostics_report()
    assert f"version: {__version__}" in report.splitlines()
    assert "26.0.7b0" not in report
