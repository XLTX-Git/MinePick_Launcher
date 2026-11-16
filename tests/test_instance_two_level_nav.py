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

"""The instances page's two-level navigation: the game-folder rail, then grouped cards.

What this pins, in the order the page reads:

* the left 200px column lists the game folders (default first, then in add order) with their
  instance counts and a Tab-reachable list whose rows carry "name + count" as their accessible text;
* the right pane groups the current folder's cards as 正式版 / 快照版 / 带加载器, renders no empty
  group, and flattens to one result grid with an "N 个结果" line while searching;
* switching a folder rewrites the pane (search kept), updates the counts, lands focus on the first
  card, and tells the rest of the window that the game directory moved;
* a folder with no instance at all shows the "还没有实例" state with the 去下载版本 way out.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from gui import i18n
from gui.pages.instance_folders import FOLDER_RAIL_WIDTH
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No network and no Java probe outliving the window (the GUI suite's three stubs)."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _make_instance(
    game, version_id: str, *, display_name: str = "", version_type: str = "release"
) -> None:
    """One instance = one profile folder with its sidecar metadata (and a declared type)."""
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(
        json.dumps({"id": version_id, "type": version_type, "mainClass": "x"}),
        encoding="utf-8",
    )
    meta = Instance(
        id=version_id, display_name=display_name or version_id, note="", created_at=1.0
    )
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )


def _settle(app, window=None) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(8):
        app.processEvents()


def _active_focus(app, window):
    """The focused widget once the window is active again (the offscreen platform drops it)."""
    window.activateWindow()
    app.processEvents()
    return app.focusWidget()


def _window(app, monkeypatch, ws_tmp, *, dont_show: bool = True, three_folders: bool = True):
    """A real window over three game folders: 主资料夹 (5 instances), 副资料夹 (1), 空资料夹 (0)."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    main = ws_tmp / "主资料夹"
    other = ws_tmp / "副资料夹"
    empty = ws_tmp / "空资料夹"
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(main))

    _make_instance(main, "1.20.1", display_name="Vanilla 1.20.1")
    _make_instance(main, "1.21.11", display_name="Vanilla 1.21.11")
    _make_instance(main, "26w02a", display_name="Snapshot 26w02a", version_type="snapshot")
    _make_instance(
        main, "fabric-loader-0.19.3-1.21.11", display_name="Fabric Pack"
    )
    _make_instance(main, "1.20.1-forge-47.4.22", display_name="Forge Pack")
    _make_instance(other, "1.21.9", display_name="Other Folder Release")
    empty.mkdir(parents=True, exist_ok=True)

    cfg, cfg_path = config_mod.load()
    from launcher.config import GameFolder

    folders = [
        GameFolder(path=main, name="主资料夹"),
        GameFolder(path=other, name="副资料夹"),
    ]
    if three_folders:
        folders.append(GameFolder(path=empty, name="空资料夹"))
    cfg.game_folders = folders
    cfg.game_dir = main
    cfg.ui_language = "zh_cn"
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.resize(1180, 760)
    if dont_show:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, window)
    return window, main, other, empty


def _close(app, window) -> None:
    window.close()
    window.deleteLater()
    for _ in range(4):
        app.processEvents()
    i18n.set_language("zh_cn")


# ---------------------------------------------------------------------------
# 1. The right pane groups the cards as 正式版 / 快照版 / 带加载器
# ---------------------------------------------------------------------------


def test_cards_are_grouped_by_type(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _main, _other, _empty = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]

        titles = [section.title_label.text() for section in page.groups_view.sections()]
        assert titles == [
            i18n.tr("instances.group.release"),
            i18n.tr("instances.group.snapshot"),
            i18n.tr("instances.group.loader"),
        ]
        assert [section.title_label.isVisibleTo(page) for section in page.groups_view.sections()] == [
            True,
            True,
            True,
        ]

        section_ids = [
            [card.instance_id for card in section.cards]
            for section in page.groups_view.sections()
        ]
        assert section_ids == [
            ["1.20.1", "1.21.11"],
            ["26w02a"],
            ["fabric-loader-0.19.3-1.21.11", "1.20.1-forge-47.4.22"],
        ]
        # The rows themselves keep their content: a 44px icon, 使用 and 管理, the current marker.
        fabric = page.card_for("fabric-loader-0.19.3-1.21.11")
        assert fabric.meta_label.text().startswith("1.21.11 · Fabric 0.19.3")
        assert fabric.use_button.text() == i18n.tr("instances.card.use")
        assert fabric.manage_button.text() == i18n.tr("instances.card.manage")
        assert fabric.findChild(QLabel, "instanceCardIcon") is not None
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 2. An empty group is not rendered
# ---------------------------------------------------------------------------


def test_a_group_without_cards_is_not_shown(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _main, other, _empty = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        assert page.switch_folder(other)
        _settle(app, window)

        sections = page.groups_view.sections()
        assert len(sections) == 1, [s.title_label.text() for s in sections]
        assert sections[0].title_label.text() == i18n.tr("instances.group.release")
        assert [card.instance_id for card in sections[0].cards] == ["1.21.9"]
        # No hidden leftovers of the other two headings either.
        labels = [label.text() for label in page.groups_view.findChildren(QLabel)]
        assert i18n.tr("instances.group.snapshot") not in labels
        assert i18n.tr("instances.group.loader") not in labels
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 3. Searching flattens the groups and reports "N 个结果"
# ---------------------------------------------------------------------------


def test_searching_flattens_the_result_grid(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _main, _other, _empty = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]

        page.search_edit.setText("1.20")
        page._on_search_changed()
        _settle(app, window)

        sections = page.groups_view.sections()
        assert len(sections) == 1
        assert not sections[0].title_label.isVisibleTo(page), "a flat result grid must not group"
        assert [card.instance_id for card in sections[0].cards] == [
            # Sorted by display name inside the flat result grid: "Forge Pack" before "Vanilla 1.20.1".
            "1.20.1-forge-47.4.22",
            "1.20.1",
        ]
        assert page.result_count.isVisibleTo(page)
        assert page.result_count.text() == i18n.tr("instances.search.results", 2)

        # Clearing the box brings the three headings back and hides the count line.
        page.search_edit.clear()
        page._on_search_changed()
        _settle(app, window)
        assert len(page.groups_view.sections()) == 3
        assert not page.result_count.isVisibleTo(page)
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 4. Switching a folder replaces the pane; the search text is kept
# ---------------------------------------------------------------------------


def test_switching_folder_rebuilds_the_pane_and_keeps_the_search(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, main, other, _empty = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        assert page.card_ids() == [
            "1.20.1",
            "1.21.11",
            "26w02a",
            "fabric-loader-0.19.3-1.21.11",
            "1.20.1-forge-47.4.22",
        ]
        assert page.folder_rail.current_path() == str(main)

        # The rail's own signal is what a click / Enter emits: switch, then the pane follows.
        page.folder_rail.folder_activated.emit(str(other))
        _settle(app, window)

        assert page.card_ids() == ["1.21.9"]
        stored, _path = config_mod.load()
        assert config_mod.same_game_folder(stored.game_dir, other)
        assert page.folder_rail.current_path() == str(other)

        # A search term survives the switch and filters the new folder.
        page.search_edit.setText("other")
        page._on_search_changed()
        _settle(app, window)
        page.folder_rail.folder_activated.emit(str(main))
        _settle(app, window)
        assert page.search_edit.text() == "other"
        assert page.cards() == []  # 主资料夹 has no "other" instance
        assert page.list_stack.currentWidget() is page.empty_search
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 5. A folder with no instance shows the empty state with the way out
# ---------------------------------------------------------------------------


def test_empty_folder_shows_the_download_way_out(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _main, _other, empty = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        assert page.switch_folder(empty)
        _settle(app, window)

        assert page.list_stack.currentWidget() is page.empty_folder
        assert page.empty_folder.label.text() == i18n.tr("instances.folder.empty")
        assert page.empty_folder.download_button.text() == i18n.tr("instances.folder.download")
        assert page.empty_folder.download_button.isVisibleTo(page)

        from gui.main_window import NAV_KEYS

        page.empty_folder.download_button.click()
        _settle(app, window)
        assert window.sidebar.currentRow() == NAV_KEYS.index("versions")
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 6. After a switch, focus is on the first card (never lost with the old widgets)
# ---------------------------------------------------------------------------


def test_focus_lands_on_the_first_card_after_a_switch(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _main, other, _empty = _window(
            app, monkeypatch, ws_tmp, dont_show=False
        )
        page = window.pages["instances"]
        window.activateWindow()
        app.processEvents()

        # A keyboard user is standing on a card of the folder that is about to be replaced.
        page.card_for("1.20.1").use_button.setFocus(Qt.FocusReason.OtherFocusReason)
        app.processEvents()
        assert page.card_for("1.20.1").use_button.hasFocus()

        assert page.switch_folder(other)
        _settle(app, window)
        focused = _active_focus(app, window)

        assert focused is not None, "the switch left the window with no focused control"
        assert focused.isVisible()
        assert focused is page.card_for("1.21.9").use_button
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 7. The rail's rows are reachable and named "folder + count"
# ---------------------------------------------------------------------------


def test_folder_rows_are_tab_reachable_and_named(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _main, other, _empty = _window(app, monkeypatch, ws_tmp, dont_show=False)
        page = window.pages["instances"]
        rail = page.folder_rail

        assert rail.list.focusPolicy() != Qt.FocusPolicy.NoFocus
        assert rail.list.accessibleName() == i18n.tr("instances.folder.list")
        assert rail.width() == FOLDER_RAIL_WIDTH

        expected = [
            i18n.tr("instances.folder.a11y", "主资料夹", 5),
            i18n.tr("instances.folder.a11y", "副资料夹", 1),
            i18n.tr("instances.folder.a11y", "空资料夹", 0),
        ]
        assert rail.item_texts() == expected
        for row, text in zip(rail.rows(), expected, strict=False):
            assert row.accessibleName() == text

        # The rail is this page's column: the main rail does not own it, and it disappears with the
        # list view when the detail page takes over.
        assert not window.nav_rail.isAncestorOf(rail)
        assert page.open_instance("1.20.1")
        _settle(app, window)
        assert not rail.isVisible()
        page.show_list()
        _settle(app, window)
        assert rail.isVisible()

        # Up/Down walk the folders while focus stays in the rail (that is what makes the walk usable).
        window.activateWindow()
        app.processEvents()
        rail.list.setFocus(Qt.FocusReason.OtherFocusReason)
        app.processEvents()
        assert _active_focus(app, window) is rail.list
        QTest.keyClick(rail.list, Qt.Key.Key_Down)
        _settle(app, window)
        assert rail.current_path() == str(other)
        assert _active_focus(app, window) is rail.list, (
            "an arrow key must not pull focus out of the folder list"
        )
    finally:
        if window is not None:
            _close(app, window)

def test_the_rail_can_remove_a_folder_after_confirming(app, monkeypatch, ws_tmp):
    """「添加文件夹」 had no counterpart: dropping a folder needed the settings page."""
    from gui import inwindow_dialog

    window, _main, other, _empty = _window(app, monkeypatch, ws_tmp)
    try:
        page = window.pages["instances"]
        assert page.folder_rail.count() == 3
        monkeypatch.setattr(inwindow_dialog, "question", lambda *a, **k: True)
        page.folder_rail.remove_requested.emit(str(other))
        _settle(app, window)
        assert page.folder_rail.count() == 2
        cfg, _path = config_mod.load()
        assert all(str(folder.path) != str(other) for folder in cfg.effective_game_folders())
    finally:
        _close(app, window)


def test_removing_the_last_folder_is_refused(app, monkeypatch, ws_tmp):
    """One folder always stays: the rail says so instead of emptying itself."""
    window, _main, _other, _empty = _window(app, monkeypatch, ws_tmp, three_folders=False)
    try:
        page = window.pages["instances"]
        monkeypatch.setattr(
            "gui.pages.instances_page.inwindow_dialog.question", lambda *a, **k: True
        )
        # Remove every folder but the active one; the last removal must be refused.
        page.folder_rail.remove_requested.emit(page.folder_rail.path_at(1))
        _settle(app, window)
        last = page.folder_rail.path_at(0)
        page.folder_rail.remove_requested.emit(last)
        _settle(app, window)
        assert page.folder_rail.count() == 1
    finally:
        _close(app, window)
