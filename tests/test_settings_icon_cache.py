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

"""The settings page's two online-recognition controls: the master switch and the image cache.

The switch is a UI preference stored in ``ui_state.ini`` (not a launcher-config field), and it has
to take effect in the same process, because the instance detail checks the module flag before every
lookup. The clear button has to report what it freed - the store itself only counts files.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.pages import settings_page
from launcher.mods import identity
from launcher.net import http_cache


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    yield
    identity.set_enabled(True)
    i18n.set_language("zh_cn")


def _page(app: QApplication) -> settings_page.SettingsPage:
    page = settings_page.SettingsPage()
    app.processEvents()
    return page


def test_online_recognition_is_on_by_default_and_persists_its_switch(app):
    page = _page(app)
    try:
        assert page.online_identity_check.isChecked() is True
        assert identity.is_enabled() is True
        page.online_identity_check.setChecked(False)
        app.processEvents()
        assert identity.is_enabled() is False, "the switch must apply without a restart"
        stored = QSettings(
            str(settings_page.paths.launcher_dir() / settings_page.STATE_FILENAME),
            QSettings.Format.IniFormat,
        ).value(identity.ONLINE_SETTING_KEY)
        assert str(stored).lower() in ("false", "0")
    finally:
        page.deleteLater()
        app.processEvents()

    reopened = _page(app)
    try:
        assert reopened.online_identity_check.isChecked() is False
        assert identity.is_enabled() is False
    finally:
        reopened.deleteLater()
        app.processEvents()


def test_clearing_the_icon_cache_reports_the_space_it_freed(app, monkeypatch, ws_tmp):
    page = _page(app)
    cache = ws_tmp / "icon-cache"
    cache.mkdir()
    (cache / "a.png").write_bytes(b"x" * 1024)
    (cache / "b.png").write_bytes(b"y" * 2048)
    monkeypatch.setattr(http_cache, "icon_cache_dir", lambda: cache)
    messages: list[str] = []
    monkeypatch.setattr(
        settings_page, "set_app_status", lambda _w, text, level=None: messages.append(text)
    )
    try:
        page.clear_icon_cache_button.click()
        app.processEvents()
        assert list(cache.iterdir()) == []
        assert messages, "clearing the cache must report what it did"
        assert "2" in messages[-1]
        assert "KB" in messages[-1] or "MB" in messages[-1]
    finally:
        page.deleteLater()
        app.processEvents()


def test_clearing_an_empty_icon_cache_says_so(app, monkeypatch, ws_tmp):
    page = _page(app)
    cache = ws_tmp / "empty-icon-cache"
    cache.mkdir()
    monkeypatch.setattr(http_cache, "icon_cache_dir", lambda: cache)
    messages: list[str] = []
    monkeypatch.setattr(
        settings_page, "set_app_status", lambda _w, text, level=None: messages.append(text)
    )
    try:
        page.clear_icon_cache_button.click()
        app.processEvents()
        assert messages == [i18n.tr("settings.icon_cache.empty")]
    finally:
        page.deleteLater()
        app.processEvents()
