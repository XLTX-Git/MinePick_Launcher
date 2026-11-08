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

"""Tools › 联机: the six layouts render, the fake session drives them, and the nav index is right."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.pages.tools_page import SCREEN_STATES, ToolsPage
from launcher.link import FakeLinkSession, LinkDifficulty, LinkMember, LinkState

INSTANCES = [("demo-instance", "1.21.11 · 原版")]

ROOM_OPTIONS = {
    LinkState.HOST_READY: {
        "room_code": "U/8F3K-2QX9-7WMD-4TRA",
        "members": (LinkMember("你", is_host=True, avatar="我"), LinkMember("小王")),
        "address": "127.0.0.1:25566",
    },
    LinkState.CONNECTING: {
        "room_code": "U/8F3K-2QX9-7WMD-4TRA",
        "difficulty": LinkDifficulty.TOUGH,
    },
    LinkState.JOINED: {
        "room_code": "U/8F3K-2QX9-7WMD-4TRA",
        "members": (LinkMember("你", avatar="我"), LinkMember("小王", is_host=True)),
        "address": "127.0.0.1:25566",
    },
}


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn_language():
    """These assertions read Chinese labels; restore the process-wide language afterwards."""
    previous = i18n.current_language()
    i18n.set_language("zh_cn")
    yield
    i18n.set_language(previous)


def _show(app, session: FakeLinkSession) -> ToolsPage:
    page = ToolsPage(session=session, instances_provider=lambda: list(INSTANCES))
    page.resize(820, 600)
    page.show()
    for _ in range(4):
        app.processEvents()
    return page


def _close(app, page: ToolsPage) -> None:
    page.close()
    page.deleteLater()
    app.processEvents()


# ------------------------------------------------------------------ six layouts


@pytest.mark.parametrize("state", SCREEN_STATES)
def test_each_reviewed_state_renders(app, state: LinkState) -> None:
    """Every state in the design review builds, paints and selects its own page."""
    options = ROOM_OPTIONS.get(state, {})
    page = _show(app, FakeLinkSession(state=state, **options))
    try:
        assert page.current_state() is state
        assert page.state_stack.currentWidget() is page._pages[state]
        pixmap = page.grab()
        assert not pixmap.isNull(), f"{state} painted nothing"
    finally:
        _close(app, page)


def test_failed_state_renders_with_its_reason(app) -> None:
    """FAILED is outside the six reviewed pictures but is a real state and must render."""
    page = _show(app, FakeLinkSession(state=LinkState.FAILED, failure_key="tasks.retry"))
    try:
        assert page.current_state() is LinkState.FAILED
        assert page.failure_reason.isVisibleTo(page)
        assert page.failure_reason.text() == i18n.tr("tasks.retry")
        assert not page.grab().isNull()
    finally:
        _close(app, page)


# ------------------------------------------------------------------ transitions


def test_fake_session_drives_the_six_state_machine(app) -> None:
    fake = FakeLinkSession()
    page = _show(app, fake)
    try:
        assert page.current_state() is LinkState.NOT_DOWNLOADED
        page.download_button.click()
        app.processEvents()
        assert page.download_progress_bar.value() == 1000
        assert page.download_progress_bar.maximum() == 1000
        assert page.current_state() is LinkState.IDLE
        assert page.join_edit.isVisibleTo(page)
        assert not page.join_button.isEnabled(), "an empty room code must not be submittable"

        page.join_edit.setText("U/AAAA-BBBB-CCCC-DDDD")
        assert page.join_button.isEnabled()
        page.join_button.click()
        app.processEvents()
        assert page.current_state() is LinkState.CONNECTING
        assert page.difficulty_pill.text() == "困难"
        assert page.connecting_warning.isVisibleTo(page)
        assert page.connecting_cancel_button.isVisibleTo(page)

        fake.complete_join(
            members=(LinkMember("你", avatar="我"), LinkMember("小王")),
            address="127.0.0.1:25566",
        )
        app.processEvents()
        assert page.current_state() is LinkState.JOINED
        assert page.address_label.text() == "127.0.0.1:25566"
        assert not page.connecting_cancel_button.isVisibleTo(page)

        fake.close_room()
        app.processEvents()
        assert page.current_state() is LinkState.IDLE
    finally:
        _close(app, page)


def test_difficulty_label_and_warning_follow_the_session(app) -> None:
    fake = FakeLinkSession(state=LinkState.CONNECTING, difficulty=LinkDifficulty.EASY)
    page = _show(app, fake)
    try:
        assert page.difficulty_pill.text() == "简单"
        assert not page.connecting_warning.isVisibleTo(page)
        fake.set_state(LinkState.CONNECTING, difficulty=LinkDifficulty.MEDIUM)
        app.processEvents()
        assert page.difficulty_pill.text() == "中等"
        assert not page.connecting_warning.isVisibleTo(page)
        fake.set_state(LinkState.CONNECTING, difficulty=LinkDifficulty.TOUGH)
        app.processEvents()
        assert page.difficulty_pill.text() == "困难"
        assert page.connecting_warning.isVisibleTo(page)
    finally:
        _close(app, page)


def test_host_waiting_and_ready_drive_the_step_card_room_code_and_members(app) -> None:
    fake = FakeLinkSession(state=LinkState.IDLE)
    page = _show(app, fake)
    try:
        assert page.instance_combo.count() == 1
        assert page.create_button.isEnabled()
        page.create_button.click()
        app.processEvents()
        assert fake.state() is LinkState.HOST_WAITING
        assert page.current_state() is LinkState.HOST_WAITING
        assert page.wait_cancel_button.isVisibleTo(page)

        fake.publish(
            members=(LinkMember("你", is_host=True, avatar="我"), LinkMember("小王")),
        )
        app.processEvents()
        assert page.current_state() is LinkState.HOST_READY
        assert page.room_code_label.text() == fake.room_code()
        assert page.member_row_count() == 2
        assert "2" in page.members_count_label.text()
        assert not page.wait_cancel_button.isVisibleTo(page)

        page.close_room_button.click()
        app.processEvents()
        assert page.current_state() is LinkState.IDLE
        assert page.member_row_count() == 0
    finally:
        _close(app, page)


def test_empty_or_failed_operations_land_on_the_failed_page(app) -> None:
    fake = FakeLinkSession(state=LinkState.IDLE)
    page = _show(app, fake)
    try:
        fake.fail("")
        app.processEvents()
        assert page.current_state() is LinkState.FAILED
        assert not page.failure_reason.isVisibleTo(page)
        page.retry_button.click()
        app.processEvents()
        assert page.current_state() is LinkState.IDLE
    finally:
        _close(app, page)


def test_join_clear_and_paste_are_wired(app) -> None:
    fake = FakeLinkSession(state=LinkState.IDLE)
    page = _show(app, fake)
    try:
        page.join_edit.setText("U/TEST")
        page.clear_button.click()
        assert page.join_edit.text() == ""
        QApplication.clipboard().setText("U/PASTE-1234")
        page.paste_button.click()
        assert page.join_edit.text() == "U/PASTE-1234"
    finally:
        _close(app, page)


def test_channels_disclosure_starts_collapsed_and_expands_sources(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.NOT_DOWNLOADED))
    try:
        assert not page.channels_detail.isVisibleTo(page)
        page.channels_button.click()
        app.processEvents()
        assert page.channels_detail.isVisibleTo(page)
        assert "GitHub" in page.channels_detail.text()
    finally:
        _close(app, page)


def test_join_and_host_cards_are_stacked_full_width(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        assert page.join_card.width() == page.host_card.width()
        assert page.host_card.y() > page.join_card.y()
        assert page.join_card.width() > 600, "both cards must use the full content width"
    finally:
        _close(app, page)


def test_wait_cancel_lives_inside_the_step_card(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.HOST_WAITING))
    try:
        assert page.wait_cancel_button.isVisibleTo(page)
        assert page.wait_step_card.isAncestorOf(page.wait_cancel_button)
    finally:
        _close(app, page)


def test_joined_open_button_emits_the_direct_address(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.JOINED, address="127.0.0.1:25566"))
    seen: list[str] = []
    page.open_multiplayer_requested.connect(seen.append)
    try:
        page.open_multiplayer_button.click()
        assert seen == ["127.0.0.1:25566"]
    finally:
        _close(app, page)


def test_failed_state_shows_the_session_reason_key(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.FAILED, failure_key="tools.connecting.warning"))
    try:
        assert page.failure_reason.isVisibleTo(page)
        assert page.failure_reason.text() == i18n.tr("tools.connecting.warning")
    finally:
        _close(app, page)


def test_fake_session_listener_can_be_removed() -> None:
    fake = FakeLinkSession()
    states: list[LinkState] = []
    unsubscribe = fake.add_listener(lambda: states.append(fake.state()))
    fake.download_core(lambda value: None)
    assert states == [LinkState.IDLE]
    unsubscribe()
    fake.join("U/NEXT")
    assert states == [LinkState.IDLE], "an unsubscribed listener must not run again"


def test_fake_download_reports_the_project_progress_scale() -> None:
    fake = FakeLinkSession()
    seen: list[int] = []
    fake.download_core(seen.append)
    assert seen == [0, 500, 1000]
    assert fake.state() is LinkState.IDLE


# ------------------------------------------------------------------ navigation


def test_tools_is_the_ninth_nav_entry_between_mods_and_settings(app, monkeypatch, ws_tmp) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game"))
    (ws_tmp / "game").mkdir()
    from gui import nav_icons
    from gui.main_window import NAV_KEYS, MainWindow

    assert len(NAV_KEYS) == 9
    assert NAV_KEYS[5] == "mods"
    assert NAV_KEYS[6] == "tools"
    assert NAV_KEYS[7] == "settings"
    assert nav_icons.ICON_FILES["tools"] == "wrench"

    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1000, 610)
    window.show()
    for _ in range(4):
        app.processEvents()
    try:
        assert window.sidebar.count() == 9
        assert window.sidebar.item(6).text() == i18n.tr("nav.tools")
        window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
        app.processEvents()
        assert window.stack.currentWidget() is window.pages["tools"]
        assert isinstance(window.pages["tools"], ToolsPage)
        assert window.minimumWidth() >= 1000
        assert window.minimumSizeHint().width() <= 1180
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_language_switch_rebuilds_the_tools_entry_and_page(app, monkeypatch, ws_tmp) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_lang"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game_lang"))
    (ws_tmp / "game_lang").mkdir()
    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()
    try:
        i18n.set_language("en_us")
        window.build_pages()
        app.processEvents()
        assert window.sidebar.count() == 9
        assert window.sidebar.item(NAV_KEYS.index("tools")).text() == "Tools"
        window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
        app.processEvents()
        page = window.pages["tools"]
        assert isinstance(page, ToolsPage)
        assert page.current_state() is LinkState.NOT_DOWNLOADED
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
