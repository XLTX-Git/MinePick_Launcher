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

"""Nine navigation entries: the order, the rail, the page each row reaches, and the width floor.

The three pages that briefly lived on the rail (截图 / 服务器 / 收藏夹) are gone from it: screenshots
and servers are sections of the instance detail page, favorites was deleted. Every place that indexes
the rail goes through ``NAV_KEYS`` itself, and this file pins the list back to the nine destinations
and the 1000px product floor.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from gui.main_window import NAV_KEYS

EXPECTED_NAV = [
    "launch",
    "instances",
    "versions",
    "java",
    "account",
    "mods",
    "tools",
    "settings",
    "about",
]


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


def test_the_navigation_is_back_to_nine_entries() -> None:
    assert len(NAV_KEYS) == 9
    assert NAV_KEYS == EXPECTED_NAV
    # The three removed destinations must not sneak back between 资源 and 工具.
    assert NAV_KEYS.index("mods") + 1 == NAV_KEYS.index("tools")
    assert NAV_KEYS[-3:] == ["tools", "settings", "about"]


def test_the_removed_pages_are_not_rail_destinations(window) -> None:
    from gui.pages.instance_detail import InstanceDetail

    assert window.pages.get("screenshots") is None
    assert window.pages.get("servers") is None
    assert window.pages.get("favorites") is None
    # Both live inside the instance detail page's stack instead.
    detail = window.pages["instances"].detail
    assert isinstance(detail, InstanceDetail)
    assert "screenshots" in detail.nav_buttons
    assert "servers" in detail.nav_buttons


def test_every_nav_key_has_a_unique_icon() -> None:
    from gui import nav_icons

    missing = [key for key in NAV_KEYS if key not in nav_icons.ICON_FILES]
    assert not missing, missing
    rail_icons = [nav_icons.ICON_FILES[key] for key in NAV_KEYS]
    assert len(set(rail_icons)) == len(rail_icons)
    # The section glyphs must differ from the icons the same detail sidebar already uses.
    assert nav_icons.ICON_FILES["screenshots"] == "image"
    assert nav_icons.ICON_FILES["servers"] == "server"
    assert nav_icons.ICON_FILES["overview"] != nav_icons.ICON_FILES["screenshots"]
    assert nav_icons.ICON_FILES["saves"] != nav_icons.ICON_FILES["servers"]
    assert "favorites" not in nav_icons.ICON_FILES


def test_the_rail_shows_nine_translated_entries(window, app) -> None:
    from gui import i18n

    assert window.sidebar.count() == len(NAV_KEYS)
    for index, key in enumerate(NAV_KEYS):
        assert window.sidebar.item(index).text() == i18n.tr("nav." + key)


def test_each_row_reaches_its_own_page(window, app) -> None:
    for index, key in enumerate(NAV_KEYS):
        window.sidebar.setCurrentRow(index)
        app.processEvents()
        assert window.stack.currentWidget() is window.pages[key], key
    assert window.stack.count() == len(NAV_KEYS)


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
    window.sidebar.setCurrentRow(NAV_KEYS.index("mods"))
    app.processEvents()
    window.open_download_page()
    app.processEvents()
    assert window.stack.currentWidget() is window.downloads_page()
    window.close_download_page()
    app.processEvents()
    assert window.stack.currentWidget() is window.pages["mods"]
