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

"""The instance list is compact rows, not large cards.

The page used to draw one 156px card per instance: a name row, a metadata row, a counts row and a
button row, mostly empty space. It showed two instances on a normal window. These tests pin the
replacement - an 88px row with the icon and two text lines on the left and 使用/管理 centred on the
right - and the things that must survive the change: group membership, both buttons, search/sort, a
long name that must elide with its full value in the tooltip, and a row a keyboard user can land on
with Tab and see the focus ring on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt, QThreadPool
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.pages import instances_page
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance

LONG_NAME = "超长实例名称用于确认省略号规则与完整提示是否仍然有效" * 3
LONG_VERSION = "1.21.11-Fabric_0.20.0"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    # MainWindow is built without ``gui.main.create_app`` here, so install the tracker the app would
    # install: without it no widget ever gets the ``keyboardFocus`` property the focus ring is gated on.
    if getattr(application, "_focus_tracker", None) is None:
        from gui.widgets import KeyboardFocusTracker

        application._focus_tracker = KeyboardFocusTracker(application)
        application.installEventFilter(application._focus_tracker)
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No network and no Java probe outliving the window (the same stubs the GUI suite uses)."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _make_instance(
    game: Path,
    version_id: str,
    *,
    display_name: str = "",
    mods: int = 0,
    saves: int = 0,
    created_at: float = 1.0,
    version_type: str = "release",
) -> Path:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(
        json.dumps(
            {
                "id": version_id,
                "type": version_type,
                "mainClass": "net.minecraft.client.main.Main",
            }
        ),
        encoding="utf-8",
    )
    meta = Instance(
        id=version_id,
        display_name=display_name or version_id,
        note="",
        created_at=created_at,
    )
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    (folder / "saves").mkdir(exist_ok=True)
    for index in range(mods):
        (folder / "mods" / f"mod{index}.jar").write_bytes(b"PK\x03\x04")
    for index in range(saves):
        (folder / "saves" / f"world{index}").mkdir(exist_ok=True)
    return folder


def _settle(app, window) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _window(app, monkeypatch, ws_tmp, *, dont_show: bool = True):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "mc"
    # Group releases: 1.21.11, the long name, the long version id. Snapshot: 26w02a. Loaders: one of
    # each kind. Seven rows keep both the multi-group and the "still scrolls" cases real.
    _make_instance(game, "1.21.11", created_at=50.0)
    _make_instance(
        game,
        "1.20.1",
        display_name=LONG_NAME,
        mods=3,
        saves=2,
        created_at=40.0,
    )
    _make_instance(game, LONG_VERSION, display_name="长版本号实例", created_at=30.0)
    _make_instance(game, "26w02a", display_name="快照", version_type="snapshot", created_at=20.0)
    _make_instance(
        game,
        "fabric-loader-0.19.3-1.21.11",
        display_name="Fabric Pack",
        mods=2,
        created_at=10.0,
    )
    _make_instance(
        game, "1.20.1-forge-47.4.22", display_name="Forge Pack", created_at=5.0
    )
    _make_instance(game, "neoforge-21.1.5", display_name="NeoForge Pack", created_at=1.0)

    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.selected_instance = "1.20.1"
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
    return window, game


def _close(app, window) -> None:
    window.close()
    window.deleteLater()
    for _ in range(4):
        app.processEvents()
    i18n.set_language("zh_cn")


# ---------------------------------------------------------------------------
# 1. The row itself: height, one column, two text lines, two centred buttons
# ---------------------------------------------------------------------------


def test_rows_are_compact_single_column_and_keep_both_buttons(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        cards = page.cards()
        assert len(cards) == 7
        assert 88 <= instances_page.CARD_HEIGHT <= 96

        for card in cards:
            # The whole point of the change: a row is 88-96px, not the old 156px card.
            assert card.height() == instances_page.CARD_HEIGHT
            assert 40 <= card.icon_label.width() <= 48
            # Two lines, stacked, both inside the row.
            name_top = card.name_label.mapTo(card, QPoint(0, 0)).y()
            meta_top = card.meta_label.mapTo(card, QPoint(0, 0)).y()
            assert meta_top > name_top
            assert meta_top + card.meta_label.height() <= card.height()
            # 使用 and 管理 are vertically centred on the row and fully inside it.
            for button in (card.use_button, card.manage_button):
                top = button.mapTo(card, QPoint(0, 0)).y()
                assert abs((top + button.height() // 2) - card.height() // 2) <= 2
                assert button.mapTo(card, QPoint(0, 0)).x() + button.width() <= card.width()
            # The text column ends before the first button: a long name may elide, it may never push
            # the buttons (the buttons are the actions, the labels are the flexible part).
            text_right = card.text_column.mapTo(card, QPoint(0, 0)).x() + card.text_column.width()
            use_left = card.use_button.mapTo(card, QPoint(0, 0)).x()
            assert text_right < use_left

        # One column at every width: every row starts at the same x and spans the pane.
        xs = {card.mapTo(page, QPoint(0, 0)).x() for card in cards}
        assert len(xs) == 1
        assert {card.width() for card in cards} == {
            page.content_scroll.viewport().width()
        }
    finally:
        if window is not None:
            _close(app, window)


def test_the_two_buttons_still_act(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        card = page.card_for("fabric-loader-0.19.3-1.21.11")
        assert card is not None

        use_spy = QSignalSpy(page.instance_use_requested)
        card.use_button.click()
        _settle(app, window)
        assert use_spy.count() == 1
        assert use_spy.at(0)[0] == "fabric-loader-0.19.3-1.21.11"

        # 管理 is still the drill-down into the detail page, and back returns to the rows.
        card.manage_button.click()
        _settle(app, window)
        assert page.current_view() is page.detail_page
        assert page.detail.instance_id == "fabric-loader-0.19.3-1.21.11"
        page.detail_page.back_button.click()
        _settle(app, window)
        assert page.current_view() is page.view_stack.widget(0)

        # Double-clicking the row body still uses it.
        card = page.card_for("1.21.11")
        QTest.mouseDClick(card, Qt.MouseButton.LeftButton)
        _settle(app, window)
        assert [use_spy.at(index)[0] for index in range(use_spy.count())] == [
            "fabric-loader-0.19.3-1.21.11",
            "1.21.11",
        ]
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 2. Grouping, counts and the controls above the list are unchanged
# ---------------------------------------------------------------------------


def test_groups_keep_their_heading_and_card_count(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        sections = page.groups_view.sections()
        assert [section.title_label.text() for section in sections] == [
            i18n.tr("instances.group.release"),
            i18n.tr("instances.group.snapshot"),
            i18n.tr("instances.group.loader"),
        ]
        assert all(section.title_label.isVisibleTo(page) for section in sections)
        ids = [[card.instance_id for card in section.cards] for section in sections]
        assert {key: len(value) for key, value in zip(
            ("release", "snapshot", "loader"), ids, strict=True
        )} == {"release": 3, "snapshot": 1, "loader": 3}
        assert set(ids[0]) == {"1.21.11", "1.20.1", LONG_VERSION}
        assert ids[1] == ["26w02a"]
        assert set(ids[2]) == {
            "fabric-loader-0.19.3-1.21.11",
            "1.20.1-forge-47.4.22",
            "neoforge-21.1.5",
        }
        assert sum(len(section.cards) for section in sections) == len(page.cards()) == 7
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 3. Long text: elide, never squeeze the buttons, keep the full value in the tooltip
# ---------------------------------------------------------------------------


def test_long_name_elides_with_the_full_name_in_the_tooltip(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        # 1000px is the product's own lower bound for the window, so this is the worst width the row
        # can really be asked to fit (the layout floor is lower, but the user cannot drag there).
        window.resize(1000, 760)
        _settle(app, window)
        page = window.pages["instances"]
        card = page.card_for("1.20.1")
        assert card is not None
        assert card.name_label.text() == LONG_NAME
        assert card.name_label.toolTip() == LONG_NAME
        # The fixture has to be wider than its slot, or this test would not exercise the elide rule.
        assert (
            card.name_label.fontMetrics().horizontalAdvance(LONG_NAME)
            > card.name_label.width()
        )
        # Elided, but still one painted line inside the row.
        assert card.name_label.height() <= card.name_label.fontMetrics().height() + 4
        assert card.height() == instances_page.CARD_HEIGHT

        # The separately-reported long version must not be clipped into the buttons either.
        long_version = page.card_for(LONG_VERSION)
        assert long_version is not None
        assert LONG_VERSION in long_version.meta_label.text()
        assert long_version.meta_label.toolTip() == long_version.meta_label.text()
        assert long_version.meta_label.height() <= (
            long_version.meta_label.fontMetrics().height() + 4
        )
        assert (
            long_version.use_button.mapTo(long_version, QPoint(0, 0)).x()
            >= long_version.text_column.mapTo(long_version, QPoint(0, 0)).x()
            + long_version.text_column.width()
        )
    finally:
        if window is not None:
            _close(app, window)


def test_no_language_squeezes_the_buttons(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        window.resize(1000, 760)
        _settle(app, window)
        page = window.pages["instances"]
        for code, _language in i18n.UI_LANGUAGES:
            i18n.set_language(code)
            page.refresh()
            _settle(app, window)
            for card in page.cards():
                assert card.height() == instances_page.CARD_HEIGHT, code
                for button in (card.use_button, card.manage_button):
                    assert button.width() >= button.minimumSizeHint().width(), (
                        code,
                        card.instance_id,
                        button.text(),
                    )
                    assert (
                        button.mapTo(card, QPoint(0, 0)).x() + button.width()
                        <= card.width()
                    ), (code, card.instance_id, button.objectName())
                text_right = (
                    card.text_column.mapTo(card, QPoint(0, 0)).x()
                    + card.text_column.width()
                )
                use_left = card.use_button.mapTo(card, QPoint(0, 0)).x()
                assert text_right < use_left, (code, card.instance_id)
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 4. Search and sort still drive the same rows
# ---------------------------------------------------------------------------


def test_search_flattens_and_sort_reorders_the_rows(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]

        page.search_edit.setText("Fabric Pack")
        page._on_search_changed()
        _settle(app, window)
        assert page.list_stack.currentWidget() is page.content_scroll
        sections = page.groups_view.sections()
        assert len(sections) == 1
        assert not sections[0].title_label.isVisibleTo(page)
        assert [card.instance_id for card in page.cards()] == [
            "fabric-loader-0.19.3-1.21.11"
        ]
        assert page.result_count.text() == i18n.tr("instances.search.results", 1)

        page.search_edit.setText("")
        page._on_search_changed()
        _settle(app, window)
        assert len(page.groups_view.sections()) == 3
        assert len(page.cards()) == 7

        # Newest first: the created_at values are distinct, and the sort is stable inside a group.
        page.sort_combo.setCurrentIndex(1)
        _settle(app, window)
        release = page.groups_view.sections()[0].cards
        assert [card.instance_id for card in release] == [
            "1.21.11",
            "1.20.1",
            LONG_VERSION,
        ]
        page.sort_combo.setCurrentIndex(0)
        _settle(app, window)
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 5. Keyboard: the row is a stop, Tab reaches both buttons, the ring is gated on the keyboard
# ---------------------------------------------------------------------------


def test_the_row_is_a_focus_stop_and_tab_reaches_both_buttons(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp, dont_show=False)
        page = window.pages["instances"]
        card = page.cards()[0]
        assert card.focusPolicy() == Qt.FocusPolicy.StrongFocus

        window.activateWindow()
        card.setFocus(Qt.FocusReason.OtherFocusReason)
        _settle(app, window)
        assert card.hasFocus()

        QTest.keyClick(card, Qt.Key.Key_Tab)
        _settle(app, window)
        assert page.cards()[0].use_button.hasFocus()
        QTest.keyClick(page.cards()[0].use_button, Qt.Key.Key_Tab)
        _settle(app, window)
        assert page.cards()[0].manage_button.hasFocus()
        QTest.keyClick(page.cards()[0].manage_button, Qt.Key.Key_Tab)
        _settle(app, window)
        assert page.cards()[1].hasFocus()

        # Shift+Tab back to the row: focus that arrives with a key press marks the row, which is what
        # the stylesheet's [keyboardFocus="true"] focus ring is gated on.
        row = page.cards()[0]
        row.use_button.setFocus(Qt.FocusReason.OtherFocusReason)
        _settle(app, window)
        QTest.keyClick(row.use_button, Qt.Key.Key_Backtab)
        _settle(app, window)
        assert row.hasFocus(), QApplication.focusWidget()
        assert row.property("keyboardFocus") is True

        # Enter on the focused row opens its detail page, like 管理.
        QTest.keyClick(row, Qt.Key.Key_Return)
        _settle(app, window)
        assert page.current_view() is page.detail_page
        assert page.detail.instance_id == row.instance_id
    finally:
        if window is not None:
            _close(app, window)
