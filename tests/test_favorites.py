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

"""收藏夹: the JSON store, the page, and the star on a resource row."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from launcher import favorites


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _store(directory) -> favorites.FavoriteStore:
    return favorites.FavoriteStore(directory / "favorites.json")


# ---------------------------------------------------------------- the store


def test_add_persists_kind_id_name_icon_and_time(ws_tmp) -> None:
    store = _store(ws_tmp)
    assert store.add("mod", "sodium", "Sodium", "https://cdn.example/sodium.png") is True

    reloaded = _store(ws_tmp)
    items = reloaded.items()
    assert len(items) == 1
    item = items[0]
    assert (item.kind, item.project_id, item.name) == ("mod", "sodium", "Sodium")
    assert item.icon_url == "https://cdn.example/sodium.png"
    assert item.added_at > 0
    payload = json.loads((ws_tmp / "favorites.json").read_text(encoding="utf-8"))
    assert payload["version"] == favorites.FORMAT_VERSION
    assert payload["favorites"][0]["id"] == "sodium"


def test_adding_twice_is_a_no_op(ws_tmp) -> None:
    store = _store(ws_tmp)
    assert store.add("mod", "sodium", "Sodium") is True
    assert store.add("mod", "sodium", "Sodium again") is False
    assert len(store.items()) == 1


def test_toggle_stars_and_unstars(ws_tmp) -> None:
    store = _store(ws_tmp)
    assert store.toggle("shaderpack", "iris", "Iris") is True
    assert store.contains("shaderpack", "iris")
    assert store.toggle("shaderpack", "iris", "Iris") is False
    assert not store.contains("shaderpack", "iris")


def test_remove_reports_whether_it_removed_anything(ws_tmp) -> None:
    store = _store(ws_tmp)
    store.add("modpack", "fabulously-optimized", "Fabulously Optimized")
    assert store.remove("modpack", "fabulously-optimized") is True
    assert store.remove("modpack", "fabulously-optimized") is False


def test_items_are_newest_first_and_can_sort_by_time(ws_tmp) -> None:
    store = _store(ws_tmp)
    store.add("mod", "older", "Older", added_at=100.0)
    store.add("mod", "newer", "Newer", added_at=200.0)
    assert [item.project_id for item in store.items()] == ["newer", "older"]


def test_a_damaged_file_reads_as_empty_and_is_repaired_by_the_next_write(ws_tmp) -> None:
    path = ws_tmp / "favorites.json"
    path.write_text("{not json", encoding="utf-8")
    store = _store(ws_tmp)
    assert store.items() == []
    store.add("mod", "sodium", "Sodium")
    assert [item.project_id for item in _store(ws_tmp).items()] == ["sodium"]
    assert json.loads(path.read_text(encoding="utf-8"))["favorites"][0]["id"] == "sodium"


def test_an_unknown_kind_is_rejected(ws_tmp) -> None:
    store = _store(ws_tmp)
    with pytest.raises(ValueError):
        store.add("texture", "x", "X")


def test_a_blank_id_is_rejected(ws_tmp) -> None:
    store = _store(ws_tmp)
    with pytest.raises(ValueError):
        store.add("mod", "   ", "X")


def test_writing_is_atomic(ws_tmp) -> None:
    path = ws_tmp / "favorites.json"
    store = favorites.FavoriteStore(path)
    store.add("mod", "sodium", "Sodium")
    assert not (ws_tmp / "favorites.json.tmp").exists()
    assert path.exists()


# ---------------------------------------------------------------- the page


def test_the_page_lists_the_stars(app, ws_tmp) -> None:
    from gui.pages.favorites_page import FavoritesPage

    store = _store(ws_tmp)
    store.add("mod", "sodium", "Sodium", added_at=100.0)
    store.add("shaderpack", "iris", "Iris Shaders", added_at=200.0)
    page = FavoritesPage(store=store)
    try:
        assert [row.favorite.name for row in page.rows()] == ["Iris Shaders", "Sodium"]
        assert page.count_label.text()
        assert page.stack.currentIndex() == 0
    finally:
        page.deleteLater()


def test_removing_a_star_updates_the_file_and_the_list(app, ws_tmp) -> None:
    from gui.pages.favorites_page import FavoritesPage

    store = _store(ws_tmp)
    store.add("mod", "sodium", "Sodium")
    page = FavoritesPage(store=store)
    try:
        assert page.remove(0) is True
        assert page.rows() == []
        assert page.stack.currentIndex() == 1
        assert _store(ws_tmp).items() == []
    finally:
        page.deleteLater()


def test_copying_a_name_puts_it_on_the_clipboard(app, ws_tmp) -> None:
    from PySide6.QtWidgets import QApplication

    from gui.pages.favorites_page import FavoritesPage

    store = _store(ws_tmp)
    store.add("mod", "sodium", "Sodium")
    page = FavoritesPage(store=store)
    try:
        assert page.copy_name(0) is True
        assert QApplication.clipboard().text() == "Sodium"
    finally:
        page.deleteLater()


def test_empty_page_says_where_the_stars_come_from(app, ws_tmp) -> None:
    from gui.pages.favorites_page import FavoritesPage

    page = FavoritesPage(store=_store(ws_tmp))
    try:
        assert page.rows() == []
        assert page.stack.currentIndex() == 1
        # The empty state has no rows, so 刷新 is the page's only keyboard stop; without it the page
        # could not be reached with Tab at all (tools/keyboard_walkthrough.py fails such a page).
        assert page.refresh_button.isEnabled()
        assert page.refresh_button.accessibleName() or page.refresh_button.text()
        text = page.empty.text()
        assert "还没有收藏" in text or "No favorites yet" in text
    finally:
        page.deleteLater()


# ---------------------------------------------------------------- the entry point


def test_a_resource_row_carries_the_favorite_button(app) -> None:
    from gui import i18n
    from gui.pages.mods_page import _ResourceRow
    from launcher.mods.models import ModSearchHit

    calls: list[int] = []
    row = _ResourceRow(
        ModSearchHit(slug="sodium", title="Sodium", description="Fast"),
        "模组",
        "Modrinth",
        on_select=lambda: None,
        on_install=lambda: None,
        on_favorite=lambda: calls.append(1),
    )
    try:
        assert row.favorite is not None
        assert row.favorite.text() == i18n.tr("favorites.add")
        assert row.title in row.favorite.accessibleName()
        row.favorite.click()
        assert calls == [1]
        row.set_favorite_state(True)
        assert row.favorite.text() == i18n.tr("favorites.favorited")
    finally:
        row.deleteLater()


def test_the_resources_page_writes_the_shared_store(app, ws_tmp, monkeypatch) -> None:
    from gui import i18n
    from gui.pages import mods_page
    from launcher.mods.models import ModSearchHit

    store = favorites.FavoriteStore(ws_tmp / "favorites.json")
    monkeypatch.setattr(favorites, "store", lambda: store)
    page = mods_page.ResourcesPage()
    try:
        page.mods_tab._set_entries(
            [ModSearchHit(slug="sodium", title="Sodium", description="Fast")]
        )
        # The list re-applies its viewport width on a zero-delay timer; let it run while the page is
        # still alive (the timer would otherwise fire after deleteLater and touch a dead widget).
        app.processEvents()
        row = page.mods_tab._rows[0]
        assert row.favorite.text() == i18n.tr("favorites.add")
        row.favorite.click()
        assert store.contains("mod", "sodium")
        assert row.favorite.text() == i18n.tr("favorites.favorited")
        row.favorite.click()
        assert not store.contains("mod", "sodium")
        assert row.favorite.text() == i18n.tr("favorites.add")
        app.processEvents()
    finally:
        page.deleteLater()
        app.processEvents()
