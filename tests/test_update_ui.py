"""Notification-only UI: opening a release is always an explicit user action."""
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
from PySide6.QtWidgets import QApplication

from core.update_checker import UpdateChecker, UpdateResult, select_release
from umbranet import __version__
from umbranet.views import about

APP = QApplication.instance() or QApplication([])


@pytest.fixture
def view(monkeypatch):
    checker = UpdateChecker(__version__)
    monkeypatch.setattr(about.ea, "get_update_checker", lambda: checker)
    monkeypatch.setattr(about.ea, "set_update_channel", checker.set_channel)
    monkeypatch.setattr(about.ea, "get_startup_health", lambda: {"summary": "OK", "severity": "ok"})
    monkeypatch.setattr(checker, "check_async", Mock(return_value=True))
    widget = about.AboutView()
    yield widget, checker
    widget._update_poll.stop()
    widget.close()
    widget.deleteLater()


def test_release_is_not_opened_automatically(view, monkeypatch):
    widget, checker = view
    opened = Mock(return_value=True)
    monkeypatch.setattr(about.QDesktopServices, "openUrl", opened)
    checker._result = UpdateResult("available", "0.4.0",
        "https://github.com/X-302-X/UmbraNet/releases/tag/v0.4.0", "Доступна версия 0.4.0")
    widget._refresh_update_status()
    assert not widget._open_release.isHidden()
    assert "0.4.0" in widget._release_status.text()
    opened.assert_not_called()
    widget._open_release.click()
    assert opened.call_args.args[0].toString() == checker.result.url


def test_error_and_no_release_have_no_download_button(view):
    widget, checker = view
    for state in ("error", "no_releases", "current"):
        checker._result = UpdateResult(state, message=state)
        widget._refresh_update_status()
        assert widget._open_release.isHidden()
        assert widget._check_release.isEnabled()


def test_manual_check_and_prerelease_opt_in(view):
    widget, checker = view
    assert not widget._prereleases.isChecked()
    widget._check_release.click()
    checker.check_async.assert_called_once_with(force=True)
    widget._prereleases.setChecked(True)
    assert checker.include_prereleases
    assert checker.check_async.call_count == 2


def test_release_button_gets_layout_geometry_after_hidden_resize(view):
    """A hidden button may have stale geometry; showing it must relayout it."""
    from PySide6.QtWidgets import QScrollArea

    widget, checker = view
    widget.show()
    for width in (900, 434, 574, 434):
        checker._result = UpdateResult("current", message="Новых версий нет")
        widget._refresh_update_status()
        widget.resize(width, 600)
        APP.processEvents()
        assert widget._open_release.isHidden()
        checker._result = UpdateResult("available", "0.4.0",
            "https://github.com/X-302-X/UmbraNet/releases/tag/v0.4.0", "Доступна версия 0.4.0")
        widget._refresh_update_status()
        for _ in range(4):
            APP.processEvents()
        body = widget.findChild(QScrollArea).widget()
        button = widget._open_release
        assert button.isVisibleTo(body)
        left = button.mapTo(body, button.rect().topLeft()).x()
        assert left >= 0
        assert left + button.width() <= body.width()


@pytest.mark.parametrize("tag,display", [("v26.0.1r", "26.0.1r"), ("v26.0.2b", "26.0.2b")])
def test_update_notice_keeps_short_version_label(view, tag, display):
    widget, checker = view
    checker._result = select_release([{"tag_name": tag}], __version__, include_prereleases=True)
    widget._refresh_update_status()
    assert display in widget._release_status.text()
    assert display + "0" not in widget._release_status.text()
    assert ".post" not in widget._release_status.text()
    assert not widget._open_release.isHidden()


def test_about_and_copied_report_show_current_beta(view):
    from PySide6.QtWidgets import QLabel
    widget, checker = view
    labels = [label.text() for label in widget.findChildren(QLabel)]
    assert f"v{__version__}" in labels
    assert __version__ in labels
    assert widget._report_text().startswith(f"UmbraNet v{__version__}\n")
    assert checker.current_version == __version__
