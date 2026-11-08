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

import threading
import time

import pytest
from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from gui import i18n, paint_utils
from gui.pages.tools_page import SCREEN_STATES, ToolsPage, _MemberAvatar
from launcher.link import (
    ComponentStatus,
    FakeLinkSession,
    LinkDifficulty,
    LinkDownloadProgress,
    LinkMember,
    LinkState,
    ProcessStatus,
    SelfCheckReport,
    StunVerdict,
    skins,
)
from launcher.link.terracotta import TerracottaClient, TerracottaLinkSession
from tests.link_fakes import FakeCore, FakeTerracotta, wait_until

INSTANCES = [("demo-instance", "1.21.11 · 原版")]

ROOM_OPTIONS = {
    LinkState.HOST_READY: {
        "room_code": "U/8F3K-2QX9-7WMD-4TRA",
        "members": (LinkMember("Steve", is_host=True), LinkMember("Alex")),
        "address": "127.0.0.1:25566",
    },
    LinkState.CONNECTING: {
        "room_code": "U/8F3K-2QX9-7WMD-4TRA",
        "difficulty": LinkDifficulty.TOUGH,
    },
    LinkState.JOINED: {
        "room_code": "U/8F3K-2QX9-7WMD-4TRA",
        "members": (LinkMember("Alex"), LinkMember("Steve", is_host=True)),
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


def _show(app, session) -> ToolsPage:
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


class _DiagnosticSession(FakeLinkSession):
    """A session that can answer the self-check card without any network."""

    def __init__(self, report: SelfCheckReport, **kwargs) -> None:
        super().__init__(**kwargs)
        self.report = report
        self.checks = 0

    def component_status(self) -> ComponentStatus:
        return self.report.component

    def process_status(self) -> ProcessStatus:
        return self.report.process

    def self_check(self) -> SelfCheckReport:
        self.checks += 1
        return self.report


class _DownloadFailSession(FakeLinkSession):
    """Keeps the page on the onboarding layout and reports a failed install."""

    def download_core(self, progress_cb) -> None:
        self._failure_key = "tools.fail.verify"
        self._state = LinkState.NOT_DOWNLOADED
        self._notify()


class _SlowDownloadSession(FakeLinkSession):
    """Reports one byte-based progress step and never finishes, to inspect the in-row progress."""

    def download_core(self, progress_cb) -> None:
        progress_cb(LinkDownloadProgress.from_bytes(3_200_000, 8_135_914))


class _PhaseSession(FakeLinkSession):
    """``host-starting`` is a HOST_WAITING layout with its own title."""

    def phase(self) -> str:
        return "host-starting"


def _skin_png(face: str = "#ff0000", hat: str = "#00ff00") -> bytes:
    """A 64x64 skin with a painted head and an opaque hat overlay."""
    image = QImage(64, 64, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(QRect(8, 8, 8, 8), QColor(face))
    painter.fillRect(QRect(40, 8, 8, 8), QColor(hat))
    painter.end()
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _pump(app, predicate, timeout: float = 3.0) -> bool:
    """Process Qt events while waiting for a cross-thread result."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


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
        assert page.current_state() is LinkState.IDLE
        assert page.download_progress_bar.maximum() == 1000
        # The onboarding row returns to its button form once the core is installed.
        assert not page.download_button.isHidden()
        assert page.download_button.text() == ""
        assert not page.download_button.icon().isNull()
        assert page.download_progress_row.isHidden()
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
            members=(LinkMember("Alex"), LinkMember("Steve", is_host=True)),
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
            members=(LinkMember("Steve", is_host=True), LinkMember("Alex")),
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
        labels = [page.channel_buttons[key].text() for key in ("cnb", "gitee", "github")]
        assert labels == ["CNB", "Gitee", "GitHub"]
        assert page.channel_speed_labels["cnb"].text() == i18n.tr(
            "tools.onboard.channels.unmeasured"
        )
        page.channel_buttons["cnb"].click()
        assert page._selected_channel == "cnb"
        assert page.channel_buttons["cnb"].isChecked()
    finally:
        _close(app, page)


def test_join_and_host_cards_are_stacked_full_width(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        assert page.join_card.width() == page.host_card.width()
        assert page.host_card.y() > page.join_card.y()
        # The state pages fill the column left of the network self-check card.
        assert page.join_card.width() == page.state_stack.width()
        assert page.join_card.width() > 400, "both cards must use the full state column width"
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
    seen: list[LinkDownloadProgress] = []
    fake.download_core(seen.append)
    assert [value.permille for value in seen] == [0, 500, 1000]
    assert fake.state() is LinkState.IDLE


# ------------------------------------------------------------------ download row


def test_download_progress_is_shown_in_the_same_row(app) -> None:
    page = _show(app, _SlowDownloadSession(state=LinkState.NOT_DOWNLOADED))
    try:
        page.download_button.click()
        app.processEvents()
        assert not page.download_progress_row.isHidden()
        assert page.download_button.isHidden()
        assert page.download_progress_bar.value() == LinkDownloadProgress.from_bytes(
            3_200_000, 8_135_914
        ).permille
        text = page.download_status.text()
        assert "3.2" in text and "8.1" in text
    finally:
        _close(app, page)


def test_download_failure_stays_in_the_row_with_a_retry(app) -> None:
    page = _show(app, _DownloadFailSession(state=LinkState.NOT_DOWNLOADED))
    try:
        page.download_button.click()
        app.processEvents()
        assert page.current_state() is LinkState.NOT_DOWNLOADED
        assert page.download_button.text() == i18n.tr("tasks.retry")
        assert page.download_button.isVisibleTo(page)
        assert page.download_progress_row.isHidden()
        assert page.download_status.text() == i18n.tr("tools.fail.verify")
        assert page.download_status.objectName() == "statusWarning"
    finally:
        _close(app, page)


# ------------------------------------------------------------------ network check line


def test_the_page_has_no_self_check_column(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        assert not hasattr(page, "self_check_card")
        assert page.self_check_button.text() == i18n.tr("tools.net.title")
        assert page.self_check_button.toolTip() == i18n.tr("tools.net.note")
        assert page.self_check_button.isVisibleTo(page)
        assert page.self_check_line.isHidden()
    finally:
        _close(app, page)


def test_network_check_line_shows_the_cone_verdict(app) -> None:
    report = SelfCheckReport(
        component=ComponentStatus(downloaded=True, version="0.4.2", supported=True),
        process=ProcessStatus(running=True, port=25566),
        stun=StunVerdict.CONE,
    )
    session = _DiagnosticSession(report, state=LinkState.IDLE)
    page = _show(app, session)
    labels = i18n.tr("tools.net.verdicts").splitlines()
    try:
        page.self_check_button.click()
        assert _pump(app, lambda: session.checks == 1 and not page._checking)
        assert page.self_check_line.isVisibleTo(page)
        assert page.self_check_line.text() == i18n.tr("tools.net.udp.line", labels[1])
        assert page.self_check_button.text() == i18n.tr("tools.net.refresh")
        assert page.self_check_button.isEnabled()
    finally:
        _close(app, page)


def test_network_check_verdicts_cover_every_probe_result(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    labels = i18n.tr("tools.net.verdicts").splitlines()
    try:
        for verdict, expected in zip(
            (StunVerdict.UNKNOWN, StunVerdict.CONE, StunVerdict.SYMMETRIC,
             StunVerdict.BLOCKED, StunVerdict.TIMEOUT, StunVerdict.ERROR),
            labels,
            strict=True,
        ):
            page._on_self_check_ready(SelfCheckReport(stun=verdict), page.session())
            assert page.self_check_line.text() == i18n.tr("tools.net.udp.line", expected)
    finally:
        _close(app, page)


def test_network_check_without_a_backend_reports_a_failed_check(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    labels = i18n.tr("tools.net.verdicts").splitlines()
    try:
        page.self_check_button.click()
        app.processEvents()
        assert page.self_check_line.text() == i18n.tr("tools.net.udp.line", labels[-1])
        assert page.self_check_button.isEnabled()
    finally:
        _close(app, page)


def test_network_check_disables_the_button_while_the_probe_runs(app) -> None:
    gate = threading.Event()

    class _BlockingSession(_DiagnosticSession):
        def self_check(self) -> SelfCheckReport:
            gate.wait(2)
            return self.report

    session = _BlockingSession(
        SelfCheckReport(stun=StunVerdict.CONE), state=LinkState.IDLE
    )
    page = _show(app, session)
    labels = i18n.tr("tools.net.verdicts").splitlines()
    try:
        page.self_check_button.click()
        app.processEvents()
        assert not page.self_check_button.isEnabled()
        assert page.self_check_button.text() == i18n.tr("tools.net.refresh")
        assert page.self_check_line.text() == i18n.tr(
            "tools.net.udp.line", i18n.tr("tools.net.checking")
        )
        gate.set()
        assert _pump(app, lambda: page.self_check_button.isEnabled())
        assert page.self_check_line.text() == i18n.tr("tools.net.udp.line", labels[1])
    finally:
        gate.set()
        _close(app, page)


# ------------------------------------------------------------------ members and avatars


@pytest.mark.parametrize(
    ("name", "avatar", "expected"),
    [
        ("Steve", "", "S"),
        ("alex", "", "A"),
        ("7_days", "", "7"),
        ("Craft_Fox", "", "C"),
        ("", "x", "X"),
        ("玩家", "", "?"),
        ("", "", "?"),
    ],
)
def test_letter_avatar_takes_the_first_ascii_letter_or_digit(name, avatar, expected) -> None:
    from gui.pages.tools_page import _avatar_initial

    assert _avatar_initial(LinkMember(name, avatar=avatar)) == expected


def test_premium_member_gets_the_cached_skin_face(app) -> None:
    paint_utils.clear_skin_cache()
    skins.clear_cache()
    url = "https://skins.example.invalid/steve.png"
    skins.remember_skin_url(FakeLinkSession.DEMO_STEVE_UUID, url)
    paint_utils.finish_skin(url, _skin_png())
    avatar = _MemberAvatar(
        LinkMember("Steve", premium=True, uuid=FakeLinkSession.DEMO_STEVE_UUID)
    )
    try:
        avatar.load_skin()
        app.processEvents()
        assert avatar._face is not None
        assert avatar._face.width() == 24
    finally:
        paint_utils.clear_skin_cache()
        skins.clear_cache()


def test_offline_member_keeps_the_letter_avatar(app) -> None:
    skin = _MemberAvatar(LinkMember("Alex"))
    skin.load_skin()
    app.processEvents()
    assert skin._face is None


# ------------------------------------------------------------------ host-starting and lifecycle


def test_host_starting_uses_its_own_title(app) -> None:
    page = _show(app, _PhaseSession(state=LinkState.HOST_WAITING))
    try:
        assert page.wait_title.text() == i18n.tr("tools.wait.starting")
    finally:
        _close(app, page)


def test_destroying_the_page_stops_the_real_poller(app) -> None:
    fake = FakeTerracotta()
    session = TerracottaLinkSession(
        process=FakeCore(fake.port),
        client_factory=lambda port: TerracottaClient(port, timeout=0.5),
        poll_interval=0.02,
        attach_interval=0.02,
        component_status_fn=lambda: ComponentStatus(
            downloaded=True, version="0.4.2", supported=True
        ),
        stun_probe=lambda: StunVerdict.CONE,
        player_name=lambda: "Steve",
    )
    page = _show(app, session)
    try:
        assert wait_until(lambda: session.state() is LinkState.IDLE)
        assert session.is_polling()
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert wait_until(lambda: not session.is_polling())
    finally:
        session.dispose(wait=True)
        fake.stop()


# ------------------------------------------------------------------ navigation


def test_open_multiplayer_is_wired_to_the_window(app, monkeypatch, ws_tmp) -> None:
    """The window's handler copies the direct address and reports it on the status line."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_mp"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game_mp"))
    (ws_tmp / "game_mp").mkdir()
    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()
    try:
        window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
        app.processEvents()
        page = window.pages["tools"]
        page.set_session(
            FakeLinkSession(state=LinkState.JOINED, address="127.0.0.1:25566")
        )
        app.processEvents()
        page.open_multiplayer_button.click()
        app.processEvents()
        assert QApplication.clipboard().text() == "127.0.0.1:25566"
        assert window.status_label.text() == i18n.tr(
            "tools.joined.copied", "127.0.0.1:25566"
        )
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()



def test_tools_is_the_nav_entry_after_the_three_collection_pages(app, monkeypatch, ws_tmp) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game"))
    (ws_tmp / "game").mkdir()
    from gui import nav_icons
    from gui.main_window import NAV_KEYS, MainWindow

    assert NAV_KEYS.index("mods") == 5
    assert NAV_KEYS[NAV_KEYS.index("mods") + 1 : NAV_KEYS.index("mods") + 4] == [
        "screenshots",
        "servers",
        "favorites",
    ]
    assert NAV_KEYS.index("tools") == NAV_KEYS.index("mods") + 4
    assert NAV_KEYS.index("settings") == NAV_KEYS.index("tools") + 1
    assert nav_icons.ICON_FILES["tools"] == "wrench"

    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1000, 610)
    window.show()
    for _ in range(4):
        app.processEvents()
    try:
        assert window.sidebar.count() == len(NAV_KEYS)
        assert window.sidebar.item(NAV_KEYS.index("tools")).text() == i18n.tr("nav.tools")
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
        assert window.sidebar.count() == len(NAV_KEYS)
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
