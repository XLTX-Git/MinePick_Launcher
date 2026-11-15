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

"""The versions page's four folds, its 50-row batches and its flat search.

The page used to be one flat list behind category chips. It is now PCL's shape: two latest-version
shortcuts, then 正式版 / 快照版 / 远古版 / 愚人节版 as fold cards. These tests pin the two properties
that shape exists for - a closed fold builds no rows, and an open one builds 50 at a time - plus the
search behaviour that turns the folds into one flat result list.
"""

from __future__ import annotations

import re

import pytest

from launcher.meta.manifest import ManifestVersion


def _no_network(**kwargs):
    raise RuntimeError("offline test")


def _versions() -> list[ManifestVersion]:
    versions = [
        ManifestVersion(
            id=f"1.21.{index}",
            type="release",
            url="https://example.invalid/v.json",
            time="",
            release_time=f"2025-12-{9 - index:02d}T12:00:00+00:00",
        )
        for index in range(3)
    ]
    versions += [
        ManifestVersion(
            id=f"snap-{index:02d}",
            type="snapshot",
            url="https://example.invalid/v.json",
            time="",
            release_time=f"2025-01-{index:02d}T12:00:00+00:00",
        )
        for index in range(1, 56)
    ]
    versions += [
        ManifestVersion(
            id="b1.7.3",
            type="old_beta",
            url="https://example.invalid/v.json",
            time="",
            release_time="2011-07-08T12:00:00+00:00",
        ),
        ManifestVersion(
            id="a1.0.4",
            type="old_alpha",
            url="https://example.invalid/v.json",
            time="",
            release_time="2010-07-08T12:00:00+00:00",
        ),
        ManifestVersion(
            id="24w14potato",
            type="snapshot",
            url="https://example.invalid/v.json",
            time="",
            release_time="2024-04-01T12:00:00+00:00",
        ),
        ManifestVersion(
            id="25w14craftmine",
            type="snapshot",
            url="https://example.invalid/v.json",
            time="",
            release_time="2025-04-01T12:00:00+00:00",
        ),
    ]
    return versions


@pytest.fixture(scope="module")
def app():
    """The suite's QApplication, built the way the launcher builds it (theme + focus tracker)."""
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


@pytest.fixture()
def page(app, monkeypatch, ws_tmp):
    from types import SimpleNamespace

    from launcher import config as config_mod

    game = ws_tmp / "game"
    game.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.ui_language = "zh_cn"
    config_mod.save(cfg, cfg_path)

    from gui import i18n
    from gui.pages import versions_install_flow, versions_page

    i18n.set_language("zh_cn")
    monkeypatch.setattr(versions_page, "fetch_manifest", _no_network)
    monkeypatch.setattr(
        versions_install_flow, "probe_loader", lambda loader, version_id: [object()]
    )
    built = versions_page.VersionsPage()
    built.resize(960, 720)
    built.show()
    app.processEvents()
    built._on_manifest(
        SimpleNamespace(
            versions=_versions(),
            latest={"release": "1.21.0", "snapshot": "snap-55"},
        )
    )
    app.processEvents()
    yield built
    built.close()


# ---------- 1. the four groups hold exactly what the manifest types say ----------


def test_the_four_groups_partition_the_manifest(page) -> None:
    counts = page.group_counts()
    assert counts == {"release": 3, "snapshot": 55, "legacy": 2, "april": 2}
    assert sum(counts.values()) == len(page.manifest.versions)
    assert page.group_of("1.21.0") == "release"
    assert page.group_of("snap-01") == "snapshot"
    assert page.group_of("b1.7.3") == "legacy"
    assert page.group_of("a1.0.4") == "legacy"
    # April Fools versions are typed "snapshot" in the manifest; the existing icon/category rule is
    # what moves them into the fourth fold, and they are not counted in the snapshot fold too.
    assert page.group_of("24w14potato") == "april"
    assert page.group_of("25w14craftmine") == "april"
    assert page.group_counts()["snapshot"] == 55


# ---------- 2. every fold starts closed and builds no rows ----------


def test_every_fold_starts_closed_and_no_row_is_built(page) -> None:
    from gui.pages import versions_page

    assert page.expanded_groups() == []
    assert page.list.count() == 4 + page.shortcut_count(), "a closed page is four header items and nothing else"
    assert page.rows == []
    assert page.findChildren(versions_page._VersionRow) == []
    headers = page.group_headers()
    assert list(headers) == ["release", "snapshot", "legacy", "april"]
    assert [headers[key].count for key in headers] == [3, 55, 2, 2]
    assert headers["release"].title == "正式版"
    assert headers["april"].title == "愚人节版"
    assert "已折叠" in headers["release"].accessibleName()
    assert page.version_ids() == []
    # The category chips and the old pager are gone; the folds and their "load more" replace both.
    for gone in ("tabs", "tab_group", "page_label", "prev_page_button", "next_page_button"):
        assert not hasattr(page, gone), f"the versions page still has a {gone} control"


# ---------- 3. opening a fold builds 50 rows; "load more" builds the next 50 ----------


def test_opening_a_fold_builds_fifty_rows_and_load_more_adds_fifty(page) -> None:
    # The header card is the control: clicking it is the same path as the keyboard's Space/Enter.
    page.group_headers()["snapshot"].click()
    assert page.is_group_expanded("snapshot")
    assert len(page.rows) == 50, "only the first batch is built"
    # four headers + 50 rows + one "load more" item
    assert page.list.count() == 4 + page.shortcut_count() + 50 + 1
    assert len(page.version_ids()) == 50
    assert page.rows[0].version_id == "snap-55"  # newest first
    assert page.rows[0].highlight == ""

    page.list.setCurrentRow(page._row_index("snap-30"))
    assert page._selected_id() == "snap-30"
    page.load_more("snapshot")
    assert len(page.rows) == 55
    assert page.list.count() == 4 + page.shortcut_count() + 55
    assert page.version_ids()[-1] == "snap-01"
    assert page._selected_id() == "snap-30", "a batch must not move the selection"

    # The other folds are still closed and still built no rows.
    assert page.group_headers()["release"].count == 3
    assert not page.is_group_expanded("release")
    assert all(row.version_id.startswith("snap-") for row in page.rows)

    page.collapse_group("snapshot")
    assert page.rows == []
    assert page.list.count() == 4 + page.shortcut_count()

    # Space on the focused header is the keyboard path to the same fold.
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    header = page.group_headers()["snapshot"]
    header.setFocus()
    QTest.keyClick(header, Qt.Key.Key_Space)
    assert page.is_group_expanded("snapshot")
    assert len(page.rows) == 50


# ---------- 4. search is one flat result list with an "N 个结果" line ----------


def test_search_is_flat_and_shows_the_result_count(page) -> None:
    page.search_edit.setText("1.21.")
    page._refilter()

    assert page.list.count() == 3
    assert page.group_headers() == {}, "search mode draws no fold headers"
    assert page.version_ids() == ["1.21.0", "1.21.1", "1.21.2"]
    assert page.result_count.isVisible()
    assert page.result_count.text().startswith("3 个结果")
    assert [row.highlight for row in page.rows] == ["1.21.", "1.21.", "1.21."]


def test_a_broad_search_is_batched_too(page) -> None:
    page.search_edit.setText("snap-")
    page._refilter()
    total = page._search_total
    assert total == 55
    assert len(page.rows) == 50, "a search builds one batch, not every match"
    assert page.list.count() == 50 + 1

    page.load_more(None)
    assert len(page.rows) == total
    assert page.list.count() == total


# ---------- 5. a hit in a closed fold opens that fold and highlights the match ----------


def test_a_hit_in_a_closed_fold_expands_it_and_highlights_the_match(page) -> None:
    assert page.expanded_groups() == []

    page.search_edit.setText("potato")
    page._refilter()

    assert page.version_ids() == ["24w14potato"]
    assert len(page.rows) == 1
    assert page.rows[0].highlight == "potato"
    # The fold was closed; the search had to open it, and says so in the result line.
    assert page.is_group_expanded("april")
    assert "已展开" in page.result_count.text()
    assert "愚人节版" in page.result_count.text()
    assert "1 个结果" in page.result_count.text()


# ---------- 6. clearing the box returns to the fold view ----------


def test_clearing_the_search_returns_to_the_fold_view(page) -> None:
    page.search_edit.setText("potato")
    page._refilter()
    assert page.list.count() == 1

    page.search_edit.clear()
    page._refilter()

    assert page.result_count.isVisible() is False
    # Clearing the box returns to the fold view; the folds the search opened are closed again.
    assert page.list.count() == 4 + page.shortcut_count()
    assert page.group_headers().keys() == {"release", "snapshot", "legacy", "april"}
    assert page.expanded_groups() == []
    assert page.rows == []
    assert page.version_ids() == []


# ---------- the latest-version shortcuts ----------


def test_latest_cards_name_the_latest_versions_with_a_publication_stamp(page) -> None:
    release = page.latest_cards["release"]
    snapshot = page.latest_cards["snapshot"]
    assert release.version_id == "1.21.0"
    assert snapshot.version_id == "snap-55"
    assert release.isEnabled() and snapshot.isEnabled()
    assert re.search(r"发布于 \d{4}/\d{1,2}/\d{1,2} \d{2}:\d{2}", release._meta_text())
    assert "最新正式版" in release._meta_text()
    assert "最新快照版" in snapshot._meta_text()


def test_clicking_a_latest_card_opens_the_install_step_and_the_fold(page, app) -> None:
    assert page.expanded_groups() == []
    page.latest_cards["release"].click()
    app.processEvents()

    assert page.stack.currentWidget() is page.step
    assert page._pending_version_id == "1.21.0"
    assert page.is_group_expanded("release"), "the shortcut opens the fold its version lives in"
    assert page._row_for("1.21.0") is not None


def _pixels(widget) -> bytes:
    image = widget.grab().toImage()
    return bytes(image.constBits())


def test_the_new_controls_paint_a_keyboard_focus_ring(page) -> None:
    """The fold header and the latest shortcut are painted by hand, not by the stylesheet.

    A QSS rule cannot paint inside a custom ``paintEvent``, so the ring is drawn from the
    ``keyboardFocus`` property the app's tracker sets. This is the pixel proof for both controls (the
    walkthrough cannot see item widgets in a page that is not on screen).
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from gui.widgets import KeyboardFocusTracker

    app = QApplication.instance()
    tracker = KeyboardFocusTracker(app)
    app.installEventFilter(tracker)
    try:
        for control in (page.group_headers()["release"], page.latest_cards["release"]):
            control.clearFocus()
            app.processEvents()
            before = _pixels(control)
            QTest.keyClick(page.search_edit, Qt.Key.Key_F13)  # arm keyboard navigation
            control.setFocus(Qt.FocusReason.TabFocusReason)
            app.processEvents()
            assert control.property("keyboardFocus") is True
            assert _pixels(control) != before, "the focused control painted no focus ring"
            control.clearFocus()
            app.processEvents()
    finally:
        app.removeEventFilter(tracker)


# ---------- 7. the bottom actions exist only while a row is selected ----------


def _action_buttons(page):
    return (
        page.install_button,
        page.open_instance_button,
        page.detail_button,
        page.launch_button,
    )


def _assert_actions_hidden(page) -> None:
    for button in _action_buttons(page):
        assert button.isHidden(), f"{button.text()} is still offered with no selected row"
    assert not page.action_hint.isHidden(), "the empty bar must say how to make the actions appear"
    assert page.action_hint.isVisibleTo(page)


def _assert_actions_available(page) -> None:
    for button in _action_buttons(page):
        assert not button.isHidden(), f"{button.text()} did not appear for the selected row"
        assert button.isEnabled(), f"{button.text()} is shown but disabled for a selected row"
    assert page.action_hint.isHidden(), "the hint has nothing to explain once a row is selected"


def test_no_selection_hides_the_bottom_actions_and_keeps_the_hint(page) -> None:
    from gui import i18n

    assert page._selected_id() is None
    _assert_actions_hidden(page)
    assert page.action_hint.text() == i18n.tr("versions.select.hint")

    # Opening a fold must not select its first row on the user's behalf: the bar stays away.
    page.group_headers()["release"].click()
    assert page._selected_id() is None
    assert page.list.count() == 4 + page.shortcut_count() + 3
    _assert_actions_hidden(page)

    # A real selection brings the four actions back; collapsing that fold takes them away again.
    page.list.setCurrentRow(page._row_index("1.21.0"))
    _assert_actions_available(page)
    page.collapse_group("release")
    assert page._selected_id() is None
    _assert_actions_hidden(page)


def test_selecting_a_row_shows_and_enables_every_action(page) -> None:
    page.group_headers()["release"].click()
    page.list.setCurrentRow(page._row_index("1.21.1"))

    assert page._selected_id() == "1.21.1"
    _assert_actions_available(page)


def test_search_results_follow_the_same_selection_rule(page) -> None:
    page.search_edit.setText("1.21.")
    page._refilter()

    assert page._selected_id() is None
    _assert_actions_hidden(page)

    page.list.setCurrentRow(page._row_index("1.21.2"))
    assert page._selected_id() == "1.21.2"
    _assert_actions_available(page)

    # A new query that no longer holds the selected row clears the selection and the bar with it.
    page.search_edit.setText("snap-")
    page._refilter()
    assert page._selected_id() is None
    _assert_actions_hidden(page)
