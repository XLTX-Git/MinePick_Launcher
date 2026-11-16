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

"""The resource page's two control rows share one grid.

The reported screenshot showed four different widths for four similar fields: the source dropdown
(131px), the sort dropdown (95px), the loader dropdown (122px) and a game-version field that
stretched to 1262px. They were built as two independent QHBoxLayout rows, so nothing forced the
first field of row 1 to line up with the first field of row 2, and the game-version field was the
only stretchy item in its row.

These tests pin the grid: the same label/field pair occupies the same columns in both rows, the
stretchy text field owns the only expanding column, and hiding the slug field cannot move the
columns before it.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from launcher.mods.models import SearchHits


@pytest.fixture()
def app():
    application = QApplication.instance() or QApplication([])
    from gui.theme import apply_theme

    apply_theme("dark", None, None, "default")
    return application


def _page(app, monkeypatch, *, width: int = 1000):
    from gui.pages import mods_page

    monkeypatch.setattr(mods_page, "_curseforge_available", lambda: True)
    monkeypatch.setattr(
        mods_page, "search_projects", lambda *args, **kwargs: SearchHits([], total=0)
    )
    monkeypatch.setattr(mods_page, "run_in_background", lambda *args, **kwargs: None)
    page = mods_page.ResourcesPage()
    window = QWidget()
    window.resize(width, 650)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(page)
    window.show()
    for _ in range(6):
        app.processEvents()
    return window, page


def _global_x(widget, window) -> int:
    return widget.mapTo(window, QPoint(0, 0)).x()


def _global_geometry(widget, window) -> tuple[int, int, int, int]:
    point = widget.mapTo(window, QPoint(0, 0))
    return point.x(), point.y(), widget.width(), widget.height()


def test_source_and_game_version_share_width_and_left_edge(app, monkeypatch) -> None:
    """The two fields of the first column: the source dropdown and the game version."""
    window, page = _page(app, monkeypatch)
    try:
        tab = page.mods_tab
        assert page.source_combo.width() == tab.game_version_edit.width(), (
            f"source {page.source_combo.width()}px vs version {tab.game_version_edit.width()}px"
        )
        assert _global_x(page.source_combo, window) == _global_x(tab.game_version_edit, window)
    finally:
        window.close()
        page.deleteLater()
        app.processEvents()


def test_sort_and_loader_share_width_and_left_edge(app, monkeypatch) -> None:
    """The two fields of the second column: the sort dropdown and the loader."""
    window, page = _page(app, monkeypatch)
    try:
        tab = page.mods_tab
        assert tab.sort_combo.width() == tab.loader_combo.width(), (
            f"sort {tab.sort_combo.width()}px vs loader {tab.loader_combo.width()}px"
        )
        assert _global_x(tab.sort_combo, window) == _global_x(tab.loader_combo, window)
        # The old row let the game-version field eat the rest of the row (1262px at 1500px wide).
        assert tab.game_version_edit.width() < window.width() // 3
    finally:
        window.close()
        page.deleteLater()
        app.processEvents()


def test_search_and_query_buttons_share_width_and_right_edge(app, monkeypatch) -> None:
    window, page = _page(app, monkeypatch)
    try:
        tab = page.mods_tab
        combo = page.source_combo
        combo.setCurrentIndex(combo.findData("modrinth"))  # the slug query is offered here
        app.processEvents()
        assert not tab.slug_edit.isHidden() and not tab.query_button.isHidden()
        assert tab.search_button.width() == tab.query_button.width(), (
            f"search {tab.search_button.width()}px vs query {tab.query_button.width()}px"
        )
        search_right = _global_x(tab.search_button, window) + tab.search_button.width()
        query_right = _global_x(tab.query_button, window) + tab.query_button.width()
        assert search_right == query_right
        assert search_right == window.width() - 14, "the action column is not flush with the page"
    finally:
        window.close()
        page.deleteLater()
        app.processEvents()


def test_hiding_the_slug_field_does_not_move_the_columns(app, monkeypatch) -> None:
    """CurseForge hides the slug field; the four fields before it must not jump."""
    window, page = _page(app, monkeypatch)
    try:
        tab = page.mods_tab
        combo = page.source_combo
        combo.setCurrentIndex(combo.findData("modrinth"))
        app.processEvents()
        assert not tab.slug_edit.isHidden()
        before = {
            "source": _global_geometry(page.source_combo, window),
            "loader": _global_geometry(tab.loader_combo, window),
            "sort": _global_geometry(tab.sort_combo, window),
            "game": _global_geometry(tab.game_version_edit, window),
        }

        combo.setCurrentIndex(combo.findData("curseforge"))
        app.processEvents()
        assert tab.slug_edit.isHidden() and tab.query_button.isHidden()
        after = {
            "source": _global_geometry(page.source_combo, window),
            "loader": _global_geometry(tab.loader_combo, window),
            "sort": _global_geometry(tab.sort_combo, window),
            "game": _global_geometry(tab.game_version_edit, window),
        }
        assert after == before, f"the grid moved when the slug field hid: {before} -> {after}"

        combo.setCurrentIndex(combo.findData("all"))
        app.processEvents()
        assert tab.slug_edit.isHidden()
        assert {
            "source": _global_geometry(page.source_combo, window),
            "loader": _global_geometry(tab.loader_combo, window),
            "sort": _global_geometry(tab.sort_combo, window),
            "game": _global_geometry(tab.game_version_edit, window),
        } == before
    finally:
        window.close()
        page.deleteLater()
        app.processEvents()
