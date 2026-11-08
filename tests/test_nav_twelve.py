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

"""Twelve navigation entries: the order, the rail, the page each row reaches, and the width floor.

Every place that used to say "nine pages" and index the rail by a literal is exercised here through
``NAV_KEYS`` itself. The three pages added after 资源 must not move 工具/设置/关于, and the window must
still fit its 1000px product floor once they are built.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from gui.main_window import NAV_KEYS


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Same three stubs the lazy-page tests use: no network, no Java probing subprocess."""
    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


@pytest.fixture
def window(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game"))
    (ws_tmp / "game").mkdir(parents=True, exist_ok=True)
    from gui.main_window import MainWindow

    built = MainWindow()
    yield built
    built.close()
    built.deleteLater()
    app.processEvents()


def test_the_three_new_entries_sit_between_resources_and_tools() -> None:
    assert len(NAV_KEYS) == 12
    assert NAV_KEYS == [
        "launch",
        "instances",
        "versions",
        "java",
        "account",
        "mods",
        "screenshots",
        "servers",
        "favorites",
        "tools",
        "settings",
        "about",
    ]
    assert NAV_KEYS.index("screenshots") == NAV_KEYS.index("mods") + 1
    assert NAV_KEYS.index("servers") == NAV_KEYS.index("mods") + 2
    assert NAV_KEYS.index("favorites") == NAV_KEYS.index("mods") + 3
    assert NAV_KEYS.index("tools") == NAV_KEYS.index("mods") + 4
    # 工具 / 设置 / 关于 keep their relative order after the insertion.
    assert NAV_KEYS[-3:] == ["tools", "settings", "about"]


def test_every_nav_key_has_an_icon() -> None:
    from gui import nav_icons

    missing = [key for key in NAV_KEYS if key not in nav_icons.ICON_FILES]
    assert not missing, missing
    assert nav_icons.ICON_FILES["screenshots"] == "layout-grid"
    assert nav_icons.ICON_FILES["servers"] == "archive"
    assert nav_icons.ICON_FILES["favorites"] == "sparkles"
    # The three new glyphs must not be the ones the rail already uses for another destination.
    rail_icons = [nav_icons.ICON_FILES[key] for key in NAV_KEYS]
    assert len(set(rail_icons)) == len(rail_icons)


def test_the_rail_shows_twelve_translated_entries(window, app) -> None:
    from gui import i18n

    assert window.sidebar.count() == 12
    assert window.sidebar.count() == len(NAV_KEYS)
    for index, key in enumerate(NAV_KEYS):
        assert window.sidebar.item(index).text() == i18n.tr("nav." + key)
    assert window.sidebar.item(NAV_KEYS.index("screenshots")).text() == i18n.tr("nav.screenshots")
    assert window.sidebar.item(NAV_KEYS.index("servers")).text() == i18n.tr("nav.servers")
    assert window.sidebar.item(NAV_KEYS.index("favorites")).text() == i18n.tr("nav.favorites")


def test_each_row_reaches_its_own_page(window, app) -> None:
    for index, key in enumerate(NAV_KEYS):
        window.sidebar.setCurrentRow(index)
        app.processEvents()
        assert window.stack.currentWidget() is window.pages[key], key
    assert window.stack.count() == len(NAV_KEYS)


def test_the_new_pages_are_the_expected_classes(window) -> None:
    from gui.pages.favorites_page import FavoritesPage
    from gui.pages.screenshots_page import ScreenshotsPage
    from gui.pages.servers_page import ServersPage

    assert isinstance(window.pages["screenshots"], ScreenshotsPage)
    assert isinstance(window.pages["servers"], ServersPage)
    assert isinstance(window.pages["favorites"], FavoritesPage)


def test_the_window_still_fits_the_1000px_floor(window, app) -> None:
    for index in range(len(NAV_KEYS)):
        window.sidebar.setCurrentRow(index)
        app.processEvents()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1000, 610)
    window.show()
    for _ in range(8):
        app.processEvents()
    assert window.minimumSizeHint().width() <= 1000, (
        f"the layout needs {window.minimumSizeHint().width()}px for a 1000px window"
    )
    assert window.minimumWidth() <= 1000


def test_the_download_page_returns_to_whichever_entry_opened_it(window, app) -> None:
    window.sidebar.setCurrentRow(NAV_KEYS.index("favorites"))
    app.processEvents()
    window.open_download_page()
    app.processEvents()
    assert window.stack.currentWidget() is window.downloads_page()
    window.close_download_page()
    app.processEvents()
    assert window.stack.currentWidget() is window.pages["favorites"]
