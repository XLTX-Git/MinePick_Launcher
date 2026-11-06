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

"""Resource search: paging offsets, the "All" merge and the page-reset rules (no network)."""

from __future__ import annotations

import pytest

from launcher.mods.loaders import ModsError
from launcher.mods.models import ModSearchHit, SearchHits
from launcher.mods.search import (
    ALL_PAGE_SIZE,
    PAGE_SIZE,
    collect_all_sources,
    merge_same_titles,
    page_offset,
    source_has_next,
)


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


def _hit(slug: str, title: str, source: str) -> ModSearchHit:
    return ModSearchHit(slug=slug, title=title, source=source)


def test_page_offset_for_both_page_sizes() -> None:
    assert PAGE_SIZE == 30
    assert ALL_PAGE_SIZE == 15
    assert page_offset(1) == 0
    assert page_offset(2) == 30
    assert page_offset(3) == 60
    # "All" asks each source for 15; both use the same page number.
    assert page_offset(2, ALL_PAGE_SIZE) == 15
    assert page_offset(0) == 0  # defensive: page numbers start at 1


def test_merge_keeps_same_title_rows_adjacent_modrinth_first() -> None:
    merged = merge_same_titles(
        [
            _hit("cf-sodium", "Sodium", "curseforge"),
            _hit("m-iris", "Iris", "modrinth"),
            _hit("m-sodium", "Sodium", "modrinth"),
        ]
    )
    # The first Sodium sets the group's place; Modrinth comes first inside it.
    assert [(hit.title, hit.source) for hit in merged] == [
        ("Sodium", "modrinth"),
        ("Sodium", "curseforge"),
        ("Iris", "modrinth"),
    ]


def test_merge_does_not_deduplicate_identical_slugs() -> None:
    merged = merge_same_titles(
        [
            _hit("sodium", "Sodium", "modrinth"),
            _hit("sodium", "Sodium", "curseforge"),
        ]
    )
    assert len(merged) == 2
    assert [hit.source for hit in merged] == ["modrinth", "curseforge"]


def test_collect_all_sources_keeps_the_survivor() -> None:
    def boom() -> SearchHits:
        raise ModsError("CurseForge 429: enter your own key in Settings")

    pages, errors = collect_all_sources(
        [
            ("Modrinth", lambda: SearchHits([_hit("sodium", "Sodium", "modrinth")], total=1)),
            ("CurseForge", boom),
        ]
    )
    assert len(pages) == 1
    assert [hit.slug for hit in pages[0]] == ["sodium"]
    assert len(errors) == 1
    assert errors[0][0] == "CurseForge"
    assert "Settings" in errors[0][1]


def test_source_has_next_uses_total_then_page_fullness() -> None:
    assert source_has_next(SearchHits([_hit("a", "A", "modrinth")], total=31), 0, 30)
    assert not source_has_next(SearchHits([_hit("a", "A", "modrinth")], total=31), 30, 30)
    # Preview tools stub search_projects with a plain list (no total): a full page may have more.
    assert source_has_next([_hit("a", "A", "modrinth")] * 30, 0, 30)
    assert not source_has_next([_hit("a", "A", "modrinth")] * 29, 0, 30)
    assert not source_has_next([], 0, 30)


def _sync_runner(fn, *args, on_result=None, on_error=None, on_finished=None, **_kwargs):
    """Run a page request inline so the test never depends on the thread pool."""
    try:
        result = fn(*args)
    except Exception as exc:  # noqa: BLE001 - mirrors Worker.run's error channel
        if on_error is not None:
            on_error(str(exc))
    else:
        if on_result is not None:
            on_result(result)
    if on_finished is not None:
        on_finished()


def test_rank_hits_puts_the_name_layers_above_downloads() -> None:
    """Exact name, then a partial one, then a word-by-word one, and only then the rest."""
    from launcher.mods.search import rank_hits

    hits = [
        _hit("popular-but-unrelated", "Popular But Unrelated", "modrinth"),
        _hit("entityculling", "Entity Culling", "modrinth"),
        _hit("exact-name", "实体渲染优化", "modrinth"),
    ]
    hits[0].downloads = 900_000_000
    hits[1].downloads = 20_000_000
    hits[2].downloads = 10
    assert [hit.slug for hit in rank_hits(hits, "实体渲染优化")] == [
        "exact-name",
        "entityculling",
        "popular-but-unrelated",
    ]


def test_rank_hits_breaks_ties_on_downloads_and_keeps_the_rest_stable() -> None:
    from launcher.mods.search import rank_hits

    first = _hit("a", "Alpha", "modrinth")
    second = _hit("b", "Beta", "modrinth")
    first.downloads = 5
    second.downloads = 50
    # Neither name answers the query: the download count orders them.
    assert [hit.slug for hit in rank_hits([first, second], "zzz")] == ["b", "a"]
    # Without a query the page keeps the source's own order.
    assert [hit.slug for hit in rank_hits([first, second], "")] == ["a", "b"]


def test_chinese_search_falls_back_to_the_english_name_the_api_knows(
    app, monkeypatch
) -> None:
    """搜 "投影": the Chinese query finds nothing, so the same page asks again with "litematica"."""
    from gui.pages import mods_page
    from gui.pages.mods_page import ResourcesPage

    queries: list[str] = []

    def fake_search(query, *, limit, offset=0, sort="relevance", project_type="mod"):
        queries.append(query)
        if query == "litematica":
            return SearchHits([_hit("litematica", "Litematica", "modrinth")], total=1)
        return SearchHits([], total=0)

    def fake_project(slug):
        return {"slug": slug, "title": "Litematica", "downloads": 1000}

    monkeypatch.setattr(mods_page, "search_projects", fake_search)
    monkeypatch.setattr(mods_page, "fetch_project", fake_project)
    monkeypatch.setattr(mods_page, "run_in_background", _sync_runner)

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        tab._initial_loaded = True
        tab.search_edit.setText("投影")
        tab._search()
        assert queries == ["投影", "litematica"]
        slugs = [entry.slug for entry in tab._entries]
        assert slugs[0] == "litematica"
        # The local table's own matches and the API hit are the same project, so it shows once.
        assert slugs.count("litematica") == 1
    finally:
        page.deleteLater()
        app.processEvents()


def test_chinese_search_with_hits_does_not_spend_a_second_request(app, monkeypatch) -> None:
    from gui.pages import mods_page
    from gui.pages.mods_page import ResourcesPage

    queries: list[str] = []

    def fake_search(query, *, limit, offset=0, sort="relevance", project_type="mod"):
        queries.append(query)
        return SearchHits([_hit("sodium", "钠", "modrinth")], total=1)

    monkeypatch.setattr(mods_page, "search_projects", fake_search)
    monkeypatch.setattr(mods_page, "fetch_project", lambda slug: {"slug": slug, "title": slug})
    monkeypatch.setattr(mods_page, "run_in_background", _sync_runner)

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        tab._initial_loaded = True
        tab.search_edit.setText("钠")
        tab._search()
        assert queries == ["钠"]
    finally:
        page.deleteLater()
        app.processEvents()


def test_row_shows_the_chinese_name_on_top_and_the_english_one_below(app) -> None:
    from gui.pages.mods_page import ResourcesPage

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        unknown = _hit("definitely-not-in-the-table", "Definitely Not In The Table", "modrinth")
        unknown.description = "an English summary"
        tab._set_entries([_hit("sodium", "Sodium", "modrinth"), unknown])
        app.processEvents()
        # A known project: Chinese headline, English project title underneath.
        assert tab._rows[0].title == "钠"
        assert tab._rows[0].subtitle == "Sodium"
        assert tab.list.item(0).text().startswith("钠 · Sodium")
        assert tab.title_label.text() == "钠 · Sodium"
        # An unknown one keeps the old shape: English title and its description.
        assert tab._rows[1].title == "Definitely Not In The Table"
        assert tab._rows[1].subtitle == "an English summary"
        assert tab.list.item(1).text() == "Definitely Not In The Table"
    finally:
        page.deleteLater()
        app.processEvents()


def test_all_source_page_two_asks_each_source_for_its_own_offset(app, monkeypatch) -> None:
    from gui.pages import mods_page
    from gui.pages.mods_page import ResourcesPage
    from launcher.mods import curseforge as cf

    calls: list[tuple[str, int, int, str]] = []

    def fake_modrinth(query, *, limit, offset=0, sort="relevance", project_type="mod"):
        calls.append(("modrinth", offset, limit, sort))
        return SearchHits([_hit("m", "Modrinth only", "modrinth")], total=100)

    def fake_curseforge(
        query, *, limit, offset=0, sort="relevance", kind="mod", game_version="", loader=""
    ):
        calls.append(("curseforge", offset, limit, sort))
        return SearchHits([_hit("c", "CurseForge only", "curseforge")], total=100)

    monkeypatch.setattr(mods_page, "search_projects", fake_modrinth)
    monkeypatch.setattr(cf, "search_projects", fake_curseforge)
    monkeypatch.setattr(mods_page, "run_in_background", _sync_runner)

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        combo = page.source_combos[0]
        combo.setCurrentIndex(combo.findData("all"))  # reloads page 1 synchronously
        calls.clear()
        tab._go_to_page(2)
        assert calls == [
            ("modrinth", ALL_PAGE_SIZE, ALL_PAGE_SIZE, "relevance"),
            ("curseforge", ALL_PAGE_SIZE, ALL_PAGE_SIZE, "relevance"),
        ]
        from gui import i18n

        assert tab.list.count() == 2
        assert tab.page_label.text() == i18n.tr("mods.page.label", 2)
        assert tab.prev_page_button.isEnabled()
        assert tab.next_page_button.isEnabled()
    finally:
        page.deleteLater()
        app.processEvents()


def test_all_source_keeps_modrinth_rows_when_curseforge_fails(app, monkeypatch) -> None:
    from gui.pages import mods_page
    from gui.pages.mods_page import ResourcesPage
    from launcher.mods import curseforge as cf

    def fake_modrinth(query, *, limit, offset=0, sort="relevance", project_type="mod"):
        return SearchHits([_hit("m1", "Alpha", "modrinth"), _hit("m2", "Beta", "modrinth")], total=2)

    def failing_curseforge(*_args, **_kwargs):
        raise ModsError("CurseForge 429: enter your own API key in Settings")

    monkeypatch.setattr(mods_page, "search_projects", fake_modrinth)
    monkeypatch.setattr(cf, "search_projects", failing_curseforge)
    monkeypatch.setattr(mods_page, "run_in_background", _sync_runner)

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        combo = page.source_combos[0]
        combo.setCurrentIndex(combo.findData("all"))
        assert [hit.slug for hit in tab._entries] == ["m1", "m2"]
        assert [row._meta[-1][1] for row in tab._rows] == ["Modrinth", "Modrinth"]
    finally:
        page.deleteLater()
        app.processEvents()


def test_source_combo_offers_all_and_is_not_height_pinned(app) -> None:
    from gui import i18n
    from gui.pages.mods_page import ResourcesPage

    page = ResourcesPage()
    try:
        combo = page.source_combos[0]
        labels = [combo.itemText(index) for index in range(combo.count())]
        assert labels == [
            i18n.tr("mods.source.modrinth"),
            i18n.tr("mods.source.curseforge"),
            i18n.tr("mods.source.all"),
        ]
        assert page.current_source() == "modrinth"  # default stays Modrinth
        # The popup-scrollbar fix: a minimum lets the popup size itself; a fixed height did not.
        assert combo.minimumHeight() == 36
        assert combo.maximumHeight() > combo.minimumHeight()
        assert combo.minimumWidth() > 0
        assert combo.maximumWidth() > combo.minimumWidth()
        # The actual regression: three items fit, so the popup must not grow a scrollbar.
        combo.showPopup()
        app.processEvents()
        try:
            view = combo.view()
            assert view.model().rowCount() == 3
            assert view.verticalScrollBar().maximum() == 0
        finally:
            combo.hidePopup()
            app.processEvents()
    finally:
        page.deleteLater()
        app.processEvents()


def test_sort_change_resets_to_page_one(app) -> None:
    from gui.pages.mods_page import ResourcesPage

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        tab._initial_loaded = True
        tab._page = 3
        loaded: list[int] = []
        tab._load_page = lambda: loaded.append(tab._page)
        tab.sort_combo.setCurrentIndex(tab.sort_combo.findData("downloads"))
        assert tab._page == 1
        assert loaded == [1]
    finally:
        page.deleteLater()
        app.processEvents()


def test_new_search_resets_to_page_one(app) -> None:
    from gui.pages.mods_page import ResourcesPage

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        tab._initial_loaded = True
        tab._page = 5
        loaded: list[int] = []
        tab._load_page = lambda: loaded.append(tab._page)
        tab.search_edit.setText("sodium")
        tab._search()
        assert tab._page == 1
        assert tab._query == "sodium"
        assert loaded == [1]
    finally:
        page.deleteLater()
        app.processEvents()


def test_source_change_resets_paging_and_query(app) -> None:
    from gui.pages.mods_page import ResourcesPage

    page = ResourcesPage()
    try:
        tab = page.mods_tab
        tab._initial_loaded = True
        tab._page = 4
        tab._has_next = True
        tab._query = "sodium"
        tab._set_entries([_hit("sodium", "Sodium", "modrinth")])
        # The source switch triggers the lazy first-page load; keep it off the network.
        for other in (
            page.mods_tab,
            page.modpacks_tab,
            page.resourcepacks_tab,
            page.shaderpacks_tab,
        ):
            other.load_initial = lambda: None
        combo = page.source_combos[0]
        combo.setCurrentIndex(combo.findData("all"))
        app.processEvents()
        assert page.current_source() == "all"
        assert tab._page == 1
        assert tab._has_next is False
        assert tab._query == ""
        assert tab.list.count() == 0
        assert not tab.next_page_button.isEnabled()
        assert not tab.prev_page_button.isEnabled()
    finally:
        page.deleteLater()
        app.processEvents()
