# SPDX-FileCopyrightText: 2026 WDNDXLTX
# SPDX-License-Identifier: GPL-3.0-only
#
# This file is part of MinePick Launcher.
#
# MinePick Launcher is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# MinePick Launcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with MinePick Launcher. If not, see <https://www.gnu.org/licenses/>.

"""The About page's update states: available, downloading, ready, up to date, failed, cancelled.

Each test drives one state through the page's own transitions (not by poking labels), so the button
visibility rules, the status line and the settings switches are covered together. The install itself
is exercised in tests/test_update_install.py; here only the refusals and the flow into it.
"""

from __future__ import annotations

import threading

import pytest
from PySide6.QtWidgets import QApplication

from gui import i18n
from launcher import config, updates
from launcher.update import install


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn():
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


@pytest.fixture()
def page(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    from gui.pages.about_page import AboutPage
    from gui.theme import apply_theme

    apply_theme("dark")
    widget = AboutPage()
    widget.resize(1000, 610)
    widget.show()
    for _ in range(6):
        app.processEvents()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def _release(**overrides) -> updates.ReleaseInfo:
    values = {
        "version": "0.6.0",
        "tag": "v0.6.0",
        "page_url": "https://example.invalid/releases/v0.6.0",
        "notes": "## 更新内容\n- 修复了自动更新的重启问题\n- 新增后台下载与取消",
        "exe_url": "https://example.invalid/MinePick_Launcher.exe",
        "exe_name": "MinePick_Launcher.exe",
        "exe_size": 48_200_000,
        "exe_digest": "sha256:" + "a" * 64,
        "published_at": "2026-11-01T09:30:00Z",
    }
    values.update(overrides)
    return updates.ReleaseInfo(**values)


def test_available_state_shows_version_size_date_and_foldable_notes(page, app) -> None:
    page.auto_download.setChecked(False)  # "notify": stay in the available state for inspection
    page._show_available(_release())
    app.processEvents()

    assert "0.6.0" in page.state_label.text()
    assert "0.6.0" in page.latest_label.text()
    assert i18n.tr("about.update.size").split("{}")[0] in page.size_label.text()
    assert "46.0 MB" in page.size_label.text() or "48" in page.size_label.text()
    assert "2026-11-01" in page.published_label.text()
    assert not page.release_box.isHidden()
    assert not page.download_button.isHidden()
    assert not page.check_button.isHidden()

    assert page.notes_label.isHidden(), "release notes must start folded"
    assert not page.notes_button.isHidden()
    page.notes_button.click()
    assert not page.notes_label.isHidden()
    assert "重启问题" in page.notes_label.text()
    page.notes_button.click()
    assert page.notes_label.isHidden()


def test_up_to_date_state_names_the_check_time(page, app) -> None:
    page._show_up_to_date("2026-11-08 20:20")
    app.processEvents()

    assert page.state_label.text() == i18n.tr("about.update.up_to_date_at", "2026-11-08 20:20")
    assert "已经是最新版本" in page.state_label.text()
    assert "2026-11-08 20:20" in page.state_label.text()
    assert page.release_box.isHidden()
    assert page.download_button.isHidden()
    assert not page.check_button.isHidden()


def test_downloading_state_shows_progress_and_a_cancel_button(page, app) -> None:
    page._release = _release()
    page._state = "downloading"
    page._cancel = threading.Event()
    page._sync_buttons()
    page._on_download_progress((500, 1000))
    app.processEvents()

    assert not page.progress_row.isHidden()
    assert page.progress_bar.value() == 500
    assert not page.cancel_button.isHidden()
    assert page.download_button.isHidden()

    page.cancel_download()
    assert page._cancel.is_set()
    assert not page.cancel_button.isEnabled()  # a second click is not a second cancel


def test_a_cancelled_download_never_reaches_the_install_state(page, app) -> None:
    page._release = _release()
    page._state = "downloading"
    page._sync_buttons()
    page._on_download_result(("cancelled", ""))
    app.processEvents()

    assert page._state == "available"
    assert page.state_label.text() == i18n.tr("about.update.cancelled")
    assert page.progress_row.isHidden()
    assert page.restart_button.isHidden()
    assert not page.download_button.isHidden()  # the user may try the download again


def test_ready_state_offers_exactly_the_restart_action(page, app, monkeypatch) -> None:
    monkeypatch.setattr(updates, "can_self_update", lambda: True)
    page._release = _release()
    page._show_ready("0.6.0")
    app.processEvents()

    assert page.state_label.text() == i18n.tr("about.update.install.ready", "0.6.0")
    assert not page.restart_button.isHidden()
    assert page.check_button.isHidden()  # one action at a time; "check" would race the staged file
    assert page.download_button.isHidden()


def test_a_source_checkout_refuses_install_and_says_why(page, app, ws_tmp) -> None:
    staged = ws_tmp / "staged.exe"
    staged.write_bytes(b"MZ")
    page._pending = staged
    page._release = _release()
    page._show_ready("0.6.0")
    app.processEvents()

    assert page.state_label.text() == i18n.tr("about.update.install.source")
    page.restart_button.click()
    app.processEvents()
    assert page.state_label.text() == i18n.tr("about.update.install.source")
    assert page._state == "failed"
    assert not page.retry_button.isHidden()
    assert staged.read_bytes() == b"MZ"  # the source checkout never touched the staged file


def test_a_failed_check_shows_the_reason_and_a_retry(page, app) -> None:
    page._show_failure("signature")
    app.processEvents()

    assert page.state_label.text() == i18n.tr("about.update.failed.signature")
    assert not page.retry_button.isHidden()
    assert not page.check_button.isHidden()
    assert page.download_button.isHidden()


def test_retry_repeats_the_action_that_failed(page, app, monkeypatch) -> None:
    checks: list[bool] = []
    monkeypatch.setattr(page, "check_updates", lambda: checks.append(True))
    page._last_action = "check"
    page._show_failure("network")
    page.retry_button.click()
    assert checks == [True]

    downloads: list[bool] = []
    monkeypatch.setattr(page, "_start_download", lambda: downloads.append(True))
    page._release = _release()
    page._last_action = "download"
    page._show_failure("network")
    page.retry()
    assert downloads == [True]


def test_an_unwritable_install_directory_is_refused_with_its_reason(page, app, monkeypatch, ws_tmp) -> None:
    staged = ws_tmp / "staged.exe"
    staged.write_bytes(b"MZ")
    page._pending = staged
    page._state = "ready"
    monkeypatch.setattr(
        install, "check_feasibility", lambda *_a, **_k: install.Feasibility(ok=False, reason="writable")
    )
    monkeypatch.setattr(updates, "can_self_update", lambda: True)
    page.restart_and_install()
    app.processEvents()

    assert page.state_label.text() == i18n.tr("about.update.install.writable")
    assert page._state == "failed"
    assert not page.retry_button.isHidden()
    assert staged.read_bytes() == b"MZ"  # nothing was touched


def test_the_auto_check_switch_uses_the_existing_config_key(page, app) -> None:
    page.auto_check.setChecked(False)
    app.processEvents()
    cfg, _ = config.load()
    assert cfg.auto_update_mode == "off"

    page.auto_check.setChecked(True)
    app.processEvents()
    assert config.load()[0].auto_update_mode == "download_notify"
    assert page.auto_download.isEnabled()

    page.auto_download.setChecked(False)
    app.processEvents()
    assert config.load()[0].auto_update_mode == "notify"
    assert page.current_mode() == "notify"
