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
from PySide6.QtCore import QBuffer, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QLabel, QSizePolicy

from gui import i18n, inwindow_dialog
from gui.pages.tools_page import SCREEN_STATES, ToolsPage
from launcher.link import (
    ComponentStatus,
    FakeLinkSession,
    LinkDifficulty,
    LinkDownloadProgress,
    LinkMember,
    LinkState,
    NatProbe,
    NatProbeError,
    NatType,
    ProcessStatus,
    SelfCheckReport,
    StunVerdict,
)
from launcher.link import components as components_module
from launcher.link.components import ComponentError

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


def _line(udp: str, tcp: str) -> str:
    return i18n.tr("tools.net.nat.line", udp, tcp)


def _nat_type(code: NatType) -> str:
    return i18n.tr("tools.net.nat.types").splitlines()[int(code)]


def _show(app, session, *, game_running: bool = False) -> ToolsPage:
    page = ToolsPage(
        session=session,
        game_running_provider=lambda: game_running,
    )
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

    def download_core(self, progress_cb, on_latency=None) -> None:
        self._failure_key = "tools.fail.verify"
        self._state = LinkState.NOT_DOWNLOADED
        self._notify()


class _SlowDownloadSession(FakeLinkSession):
    """Reports one byte-based progress step and never finishes, to inspect the in-row progress."""

    def download_core(self, progress_cb, on_latency=None) -> None:
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


# ------------------------------------------------------------------ host without an instance picker


def test_the_host_card_has_no_instance_picker(app) -> None:
    """A P2P room does not depend on the instance, so the control is gone - not merely hidden."""
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        assert not hasattr(page, "instance_combo")
        assert not hasattr(page, "refresh_button")
        assert page.host_card.findChildren(QComboBox) == []
        assert page.create_button.isVisibleTo(page)
        assert page.create_button.text() == i18n.tr("tools.host.submit")
        assert page.create_button.isEnabled()
        # The card says why there is nothing to pick; its absence must read as a decision.
        assert page.host_auto_hint.isVisibleTo(page)
        assert page.host_auto_hint.text() == i18n.tr("tools.host.auto")
    finally:
        _close(app, page)


def test_the_host_card_lists_two_steps_without_a_picker_step(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        steps = [
            label.text()
            for label in page.host_card.findChildren(QLabel)
            if label.objectName() == "toolsStepText"
        ]
        assert steps == [
            line.strip() for line in i18n.tr("tools.host.body").splitlines() if line.strip()
        ]
        assert len(steps) == 2, "the deleted 'choose an instance' step must not leave a third line"
    finally:
        _close(app, page)


def test_creating_a_room_needs_no_instance_and_waits_immediately(app) -> None:
    fake = FakeLinkSession(state=LinkState.IDLE)
    page = _show(app, fake)
    try:
        page.create_button.click()
        app.processEvents()
        assert fake.state() is LinkState.HOST_WAITING
        assert page.current_state() is LinkState.HOST_WAITING
        # The session interface still has the parameter; the page passes the empty string because a
        # P2P room does not care which instance holds the world.
        assert fake.instance_id() == ""
    finally:
        _close(app, page)


def test_the_waiting_hint_names_the_missing_game_process(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.HOST_WAITING), game_running=False)
    try:
        assert page.wait_hint.text() == i18n.tr("tools.wait.no_game")
        assert "游戏进程" in page.wait_hint.text()
        # The title keeps saying what the launcher is waiting for; only the hint becomes concrete.
        assert page.wait_title.text() == i18n.tr("tools.wait.title")
    finally:
        _close(app, page)


def test_the_waiting_hint_stays_generic_when_a_game_runs(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.HOST_WAITING), game_running=True)
    try:
        assert page.wait_hint.text() == i18n.tr("tools.wait.hint")
    finally:
        _close(app, page)


def _raising_detector() -> bool:
    raise OSError("no process table")


def test_an_unreadable_game_detector_keeps_the_generic_hint(app) -> None:
    """A broken detector must not turn a normal wait into a false "no game" claim."""
    page = _show(app, FakeLinkSession(state=LinkState.HOST_WAITING), game_running=False)
    page._game_running_provider = _raising_detector
    try:
        page._apply_waiting()
        assert page.wait_hint.text() == i18n.tr("tools.wait.hint")
    finally:
        _close(app, page)


def test_host_starting_never_claims_there_is_no_game(app) -> None:
    """``host-starting`` means the LAN world was found: a game is running by definition."""
    page = _show(app, _PhaseSession(state=LinkState.HOST_WAITING), game_running=False)
    try:
        assert page.wait_title.text() == i18n.tr("tools.wait.starting")
        assert page.wait_hint.text() == i18n.tr("tools.wait.hint")
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


def test_channels_disclosure_starts_collapsed_and_expands_sources(
    app, monkeypatch, ws_tmp
) -> None:
    # The rows persist the choice, so this test must not touch the developer's own config.
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "channels_disclosure"))
    page = _show(app, FakeLinkSession(state=LinkState.NOT_DOWNLOADED))
    try:
        assert not page.channels_detail.isVisibleTo(page)
        page.channels_button.click()
        app.processEvents()
        assert page.channels_detail.isVisibleTo(page)
        labels = [page.channel_buttons[key].text() for key in ("", "cnb", "gitee", "github")]
        assert labels[0] == i18n.tr("tools.onboard.channels.auto")
        assert labels[1:] == ["CNB", "Gitee", "GitHub"]
        assert page.channel_buttons[""].isChecked(), "自动 must be the default"
        assert page.channel_speed_labels["cnb"].text() == i18n.tr(
            "tools.onboard.channels.unmeasured"
        )
        page.channel_buttons["cnb"].click()
        assert page._selected_channel == "cnb"
        assert page.channel_buttons["cnb"].isChecked()
    finally:
        _close(app, page)


def test_channel_choice_is_saved_and_restored_without_a_download(
    app, monkeypatch, ws_tmp
) -> None:
    """A radio click is a setting, not a download: it must survive a page rebuild and do nothing else."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "channels_persist"))
    from launcher import config

    fake = FakeLinkSession(state=LinkState.NOT_DOWNLOADED)
    downloads: list = []
    fake.download_core = downloads.append  # type: ignore[method-assign]
    page = _show(app, fake)
    try:
        page.channels_button.click()
        app.processEvents()
        assert page.channel_buttons[""].isChecked()
        page.channel_buttons["gitee"].click()
        app.processEvents()
        assert config.load()[0].terracotta_mirror == "gitee"
        assert downloads == [], "picking a source must not re-download the component"
        assert fake.state() is LinkState.NOT_DOWNLOADED
    finally:
        _close(app, page)

    # A rebuilt page (the language switch builds a new one) shows the saved source again.
    restored = _show(app, FakeLinkSession(state=LinkState.NOT_DOWNLOADED))
    try:
        assert restored.channel_buttons["gitee"].isChecked()
        assert not restored.channel_buttons[""].isChecked()
        restored.channel_buttons[""].click()
        app.processEvents()
        assert config.load()[0].terracotta_mirror == ""
        assert restored.channel_buttons[""].isChecked()
    finally:
        _close(app, restored)


def test_channel_rows_show_the_last_measurement_or_unmeasured(app, monkeypatch, ws_tmp) -> None:
    data_dir = ws_tmp / "channels_speeds"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data_dir))
    from launcher.link import mirrors as mirror_module

    mirror_module._write_report(
        mirror_module.MirrorReport(
            chosen="cnb",
            latency_ms={"cnb": 37, "gitee": 120},
            measured_at=time.time(),
            failed=("github",),
        ),
        data_dir / "cache" / "mirror-pick.json",
    )
    page = _show(app, FakeLinkSession(state=LinkState.NOT_DOWNLOADED))
    try:
        assert page.channel_speed_labels["cnb"].text() == i18n.tr(
            "tools.onboard.channels.latency", 37
        )
        assert page.channel_speed_labels["gitee"].text() == i18n.tr(
            "tools.onboard.channels.latency", 120
        )
        # A probe that ran and failed reads 超时; 未测 is only for a channel never probed.
        assert page.channel_speed_labels["github"].text() == i18n.tr(
            "tools.onboard.channels.timeout"
        )
    finally:
        _close(app, page)


def test_live_latency_updates_fill_all_three_channel_rows(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.NOT_DOWNLOADED))
    try:
        page._on_channel_latency("cnb", 203)
        page._on_channel_latency("gitee", 480)
        page._on_channel_latency("github", None)
        assert page.channel_speed_labels["cnb"].text() == i18n.tr(
            "tools.onboard.channels.latency", 203
        )
        assert page.channel_speed_labels["gitee"].text() == i18n.tr(
            "tools.onboard.channels.latency", 480
        )
        assert page.channel_speed_labels["github"].text() == i18n.tr(
            "tools.onboard.channels.timeout"
        )
    finally:
        _close(app, page)


def test_the_download_hands_the_mirror_callback_to_the_session(app) -> None:
    """The page must pass its own listener into the downloader, or the other rows stay 未测."""
    seen: dict[str, object] = {}

    class _RecordingSession(FakeLinkSession):
        def download_core(self, progress_cb, on_latency=None) -> None:
            seen["on_latency"] = on_latency
            super().download_core(progress_cb)

    session = _RecordingSession(state=LinkState.NOT_DOWNLOADED)
    page = _show(app, session)
    try:
        page._download_core()
        callback = seen.get("on_latency")
        assert callable(callback), "the page did not hand a latency listener to the downloader"
        callback("github", None)
        app.processEvents()
        assert page.channel_speed_labels["github"].text() == i18n.tr(
            "tools.onboard.channels.timeout"
        )
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
        assert page.self_check_button.toolTip() == i18n.tr("tools.net.nat.note")
        assert page.self_check_button.isVisibleTo(page)
        assert page.self_check_result.text() == ""
        assert page.self_check_result.parentWidget().objectName() == "toolsBanner"
    finally:
        _close(app, page)


def test_banner_height_is_the_button_plus_the_8px_padding(app) -> None:
    """The green bar is one line plus 8px above and below, not a thick empty block.

    The floor is the self-check button: 35px + 8 + 8. A shorter bar would squash the button, which
    is exactly what the old fixed 26px height did. 56px is the ceiling from the user's report.
    """
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        banner = page.findChild(QFrame, "toolsBanner")
        text = page.findChild(QLabel, "toolsBannerText")
        assert banner is not None and text is not None
        assert banner.height() >= page.self_check_button.height() + 16, (
            f"banner {banner.height()}px < self-check button "
            f"{page.self_check_button.height()}px + 16px"
        )
        assert banner.height() <= 56, f"banner is still {banner.height()}px tall"
        assert page.self_check_button.height() >= page.self_check_button.minimumSizeHint().height()
        assert text.height() >= text.fontMetrics().height()
        # Width is untouched: the bar still spans the card's content column.
        assert banner.width() == page.state_stack.width()
        assert text.width() > text.sizeHint().width()
    finally:
        _close(app, page)


@pytest.mark.parametrize("width", (988, 1000))
def test_banner_fits_without_clipping_at_the_window_floor(app, width) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        page.resize(width, 610)
        app.processEvents()
        banner = page.findChild(QFrame, "toolsBanner")
        text = page.findChild(QLabel, "toolsBannerText")
        assert banner is not None and text is not None
        button = page.self_check_button
        assert button.isVisibleTo(page)
        assert button.geometry().right() <= banner.contentsRect().right()
        assert text.geometry().right() <= banner.contentsRect().right()
        assert banner.height() <= 56
        # No horizontal overflow of the content column.
        assert banner.width() == page.state_stack.width()
    finally:
        _close(app, page)


def test_the_udp_tcp_result_sits_inside_the_banner_at_the_window_floor(app) -> None:
    """The result occupies the banner's blank space; the hint gives way before it does.

    Structurally, the hint is the only Ignored-width label in the row while the result keeps its
    Fixed size hint, so a narrow banner elides the sentence and never the measurement.
    """
    nat = NatProbe(udp=NatType.SYMMETRIC_EASY, tcp=NatType.RESTRICTED_CONE)
    session = _DiagnosticSession(SelfCheckReport(nat=nat), state=LinkState.IDLE)
    page = _show(app, session)
    try:
        page.resize(1000, 610)
        app.processEvents()
        page.self_check_button.click()
        assert _pump(app, lambda: not page._checking and session.checks == 1)
        page.resize(1000, 610)
        app.processEvents()

        result = page.self_check_result
        banner = page.findChild(QFrame, "toolsBanner")
        hint = page.findChild(QLabel, "toolsBannerText")
        assert result.parentWidget() is banner
        assert result.text() == _line(
            _nat_type(NatType.SYMMETRIC_EASY), _nat_type(NatType.RESTRICTED_CONE)
        )
        # The measurement is fully painted: its label has at least its size hint, no elision.
        assert result.width() >= result.sizeHint().width()
        assert result.fontMetrics().horizontalAdvance(result.text()) <= result.width()
        # The sentence is the widget allowed to give way, and the banner stays one row tall.
        assert hint.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
        assert result.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed
        assert result.geometry().right() <= banner.contentsRect().right()
        assert hint.geometry().right() <= result.geometry().left()
        assert banner.height() <= 56
        # No second row survives under the banner: the block is exactly the banner.
        assert not hasattr(page, "self_check_line")
        assert banner.parentWidget().height() == banner.height()
    finally:
        _close(app, page)


def test_network_check_line_shows_the_cone_fallback(app) -> None:
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
        assert page.self_check_result.isVisibleTo(page)
        assert page.self_check_result.text() == _line(labels[1], i18n.tr("tools.net.nat.unknown"))
        assert page.self_check_button.text() == i18n.tr("tools.net.refresh")
        assert page.self_check_button.isEnabled()
    finally:
        _close(app, page)


def test_network_check_line_shows_both_easytier_types(app) -> None:
    nat = NatProbe(udp=NatType.SYMMETRIC_EASY, tcp=NatType.RESTRICTED_CONE)
    session = _DiagnosticSession(SelfCheckReport(nat=nat), state=LinkState.IDLE)
    page = _show(app, session)
    try:
        page.self_check_button.click()
        assert _pump(app, lambda: not page._checking and session.checks == 1)
        assert page.self_check_result.text() == _line(
            _nat_type(NatType.SYMMETRIC_EASY), _nat_type(NatType.RESTRICTED_CONE)
        )
        assert page.self_check_result.toolTip() == i18n.tr("tools.net.nat.easytier_tip")
    finally:
        _close(app, page)


def test_tcp_failure_keeps_the_udp_fallback_and_gives_the_reason(app) -> None:
    nat = NatProbe(error=NatProbeError.TIMEOUT)
    session = _DiagnosticSession(
        SelfCheckReport(stun=StunVerdict.CONE, nat=nat), state=LinkState.IDLE
    )
    page = _show(app, session)
    labels = i18n.tr("tools.net.verdicts").splitlines()
    try:
        page.self_check_button.click()
        assert _pump(app, lambda: not page._checking and session.checks == 1)
        assert page.self_check_result.text() == _line(
            labels[1], i18n.tr("tools.net.nat.unknown")
        )
        assert i18n.tr("tools.net.reason.timeout") in page.self_check_result.toolTip()
    finally:
        _close(app, page)


def test_network_check_verdicts_cover_every_probe_result(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    labels = i18n.tr("tools.net.verdicts").splitlines()
    unknown = i18n.tr("tools.net.nat.unknown")
    try:
        for verdict, expected in zip(
            (StunVerdict.UNKNOWN, StunVerdict.CONE, StunVerdict.SYMMETRIC,
             StunVerdict.BLOCKED, StunVerdict.TIMEOUT, StunVerdict.ERROR),
            labels,
            strict=True,
        ):
            page._on_self_check_ready(SelfCheckReport(stun=verdict), page.session())
            udp = expected if verdict not in (StunVerdict.UNKNOWN, StunVerdict.ERROR) else unknown
            assert page.self_check_result.text() == _line(udp, unknown)
    finally:
        _close(app, page)


def test_network_check_without_a_backend_reports_a_failed_check(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    unknown = i18n.tr("tools.net.nat.unknown")
    try:
        page.self_check_button.click()
        app.processEvents()
        assert page.self_check_result.text() == _line(unknown, unknown)
        assert page.self_check_result.toolTip() == i18n.tr("tools.net.nat.failed_tip")
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
        assert page.self_check_button.text() == i18n.tr("tools.net.checking")
        assert page.self_check_result.text() == _line(
            i18n.tr("tools.net.checking"), i18n.tr("tools.net.checking")
        )
        assert page.self_check_result.toolTip() == i18n.tr("tools.net.nat.checking_tip")
        gate.set()
        assert _pump(app, lambda: page.self_check_button.isEnabled())
        assert page.self_check_result.text() == _line(
            labels[1], i18n.tr("tools.net.nat.unknown")
        )
    finally:
        gate.set()
        _close(app, page)


# ------------------------------------------------------------------ members

# The room reports a name and nothing else - no UUID, no premium flag, no launcher origin - so the
# member row shows the name and the host badge, and nothing that would look like a verified identity.
# These tests pin that: no avatar widget, and a hostile/empty name still renders as text.


def test_a_member_row_is_a_name_badge_and_state(app) -> None:
    from PySide6.QtWidgets import QLabel

    row = ToolsPage._member_row(LinkMember("Steve", is_host=True))
    labels = [child.text() for child in row.findChildren(QLabel)]
    assert "Steve" in labels
    assert i18n.tr("tools.ready.host_badge") in labels
    assert not [child for child in row.findChildren(QLabel) if child.objectName() == "toolsMemberAvatar"]


def test_a_member_with_no_name_still_renders(app) -> None:
    from PySide6.QtWidgets import QLabel

    row = ToolsPage._member_row(LinkMember("   "))

    texts = [child.text() for child in row.findChildren(QLabel)]
    assert texts[0] == "?", texts



# ------------------------------------------------------------------ host-starting and lifecycle


def test_host_starting_uses_its_own_title(app) -> None:
    page = _show(app, _PhaseSession(state=LinkState.HOST_WAITING))
    try:
        assert page.wait_title.text() == i18n.tr("tools.wait.starting")
    finally:
        _close(app, page)


# The real-poller lifecycle (page destroyed / language switch) lives in tests/test_link_bus.py:
# the session is owned by the bus now, so a destroyed page must *not* stop the poller.


# ------------------------------------------------------------------ 停用联机功能


def _accept(monkeypatch) -> None:
    """Answer the next in-window question with its destructive button."""
    monkeypatch.setattr(
        inwindow_dialog.InWindowDialog, "ask", lambda self: inwindow_dialog.Outcome.ACCEPTED
    )


def _refuse(monkeypatch) -> None:
    """Answer the next in-window question with its cancel button."""
    monkeypatch.setattr(
        inwindow_dialog.InWindowDialog, "ask", lambda self: inwindow_dialog.Outcome.REJECTED
    )


def _answers(
    monkeypatch, outcomes: list[inwindow_dialog.Outcome]
) -> list[inwindow_dialog.InWindowDialog]:
    """Answer successive in-window questions from a list, keeping the dialogs for inspection."""
    dialogs: list[inwindow_dialog.InWindowDialog] = []
    queue = iter(outcomes)

    def ask(dialog: inwindow_dialog.InWindowDialog) -> inwindow_dialog.Outcome:
        dialogs.append(dialog)
        return next(queue)

    monkeypatch.setattr(inwindow_dialog.InWindowDialog, "ask", ask)
    return dialogs


def _component_tree(data_dir, *, payload: bytes = b"x" * 2048):
    """A fake launcher dir with the Terracotta payload and the EasyTier self-check component."""
    terracotta = data_dir / "components" / "terracotta"
    terracotta.mkdir(parents=True)
    (terracotta / "terracotta-0.4.2-windows-x86_64.exe").write_bytes(payload)
    easytier = data_dir / "components" / "easytier"
    easytier.mkdir(parents=True)
    (easytier / "easytier-core.exe").write_bytes(b"y" * 32)
    return terracotta, easytier


def _statuses(monkeypatch) -> list[tuple[str, str | None]]:
    """Capture what the page reports on the window's single status line."""
    import gui.pages.tools_page as tools_page_module

    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        tools_page_module,
        "set_app_status",
        lambda _widget, text, level=None: seen.append((text, level)),
    )
    return seen


def test_disable_link_is_a_focusable_named_button(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        link = page.disable_link
        assert link.isVisibleTo(page)
        assert link.text() == i18n.tr("tools.footer.disable")
        assert link.objectName() == "toolsFooterLink"
        assert link.accessibleName() == i18n.tr("tools.footer.disable")
        assert link.cursor().shape() == Qt.CursorShape.PointingHandCursor
        assert link.focusPolicy() & Qt.FocusPolicy.TabFocus, "the link has to be keyboard reachable"
    finally:
        _close(app, page)


def test_disable_confirmation_says_what_it_deletes_and_defaults_to_cancel(app) -> None:
    page = _show(app, FakeLinkSession(state=LinkState.IDLE))
    try:
        dialog = page._disable_link_dialog()
        assert dialog.title_label.text() == i18n.tr("tools.disable.title")
        assert dialog.body_label.text() == i18n.tr("tools.disable.message")
        assert [button.text() for button in dialog.buttons] == [
            i18n.tr("dialog.cancel"),
            i18n.tr("tools.disable.confirm"),
        ]
        # Destructive: Enter must keep the component, not delete it.
        assert dialog.default_button is not None
        assert dialog.default_button.text() == i18n.tr("dialog.cancel")
        body = dialog.body_label.text()
        assert "8 MB" in body, "the size of the download is what the user is giving up"
        assert "陶瓦" in body, "the deleted component has to be named"
        assert "关闭" in body, "an open room being closed first has to be stated"
        dialog.close()
    finally:
        _close(app, page)


def test_cancelling_the_confirmation_deletes_nothing(app, monkeypatch, ws_tmp) -> None:
    data_dir = ws_tmp / "disable_cancel"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data_dir))
    terracotta, easytier = _component_tree(data_dir)

    class _Session(FakeLinkSession):
        def __init__(self) -> None:
            super().__init__(state=LinkState.IDLE)
            self.calls: list[str] = []

        def close_room(self) -> None:
            self.calls.append("close_room")
            super().close_room()

        def remove_core(self) -> int:
            self.calls.append("remove_core")
            return 1

    session = _Session()
    seen = _statuses(monkeypatch)
    page = _show(app, session)
    try:
        _refuse(monkeypatch)
        page.disable_link.click()
        app.processEvents()
        assert session.calls == [], "a cancelled confirmation must not close or delete anything"
        assert terracotta.exists() and easytier.exists()
        assert page.current_state() is LinkState.IDLE
        assert seen == []
    finally:
        _close(app, page)


def test_confirming_closes_the_room_then_deletes_only_the_terracotta_component(
    app, monkeypatch, ws_tmp
) -> None:
    data_dir = ws_tmp / "disable_accept"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data_dir))
    terracotta, easytier = _component_tree(data_dir)

    class _DiskSession(FakeLinkSession):
        """Removes the real (temporary) component tree, the way the real backend does."""

        def __init__(self) -> None:
            super().__init__(state=LinkState.IDLE)
            self.calls: list[str] = []

        def close_room(self) -> None:
            self.calls.append("close_room")
            super().close_room()

        def remove_core(self) -> int:
            self.calls.append("remove_core")
            removed = components_module.remove()
            self._reset(LinkState.NOT_DOWNLOADED)
            return removed

    session = _DiskSession()
    seen = _statuses(monkeypatch)
    page = _show(app, session)
    try:
        _accept(monkeypatch)
        page.disable_link.click()
        app.processEvents()
        assert session.calls == ["close_room", "remove_core"], "the room goes before the files"
        assert not terracotta.exists(), "the downloaded multiplayer component has to be gone"
        assert easytier.exists(), "the network self-check component must survive"
        assert (easytier / "easytier-core.exe").is_file()
        assert page.current_state() is LinkState.NOT_DOWNLOADED
        assert page._pages[LinkState.NOT_DOWNLOADED] is page.state_stack.currentWidget()
        assert len(seen) == 1
        text, level = seen[0]
        assert level is None, "a completed deletion is not an error"
        assert text == i18n.tr("tools.disable.done", "0.0"), f"unexpected completion message: {text}"
        assert "MB" in text
    finally:
        _close(app, page)


def test_a_failed_deletion_reports_the_concrete_reason(app, monkeypatch) -> None:
    class _RefusingSession(FakeLinkSession):
        def remove_core(self) -> int:
            raise ComponentError(
                "tools.disable.fail",
                "terracotta-0.4.2-windows-x86_64.exe: 另一个程序正在使用此文件",
            )

    page = _show(app, _RefusingSession(state=LinkState.IDLE))
    seen = _statuses(monkeypatch)
    try:
        _accept(monkeypatch)
        page.disable_link.click()
        app.processEvents()
        assert len(seen) == 1
        text, level = seen[0]
        assert level == "error"
        assert "另一个程序正在使用此文件" in text, "the OS reason has to reach the user"
        assert text == i18n.tr(
            "tools.disable.fail", "terracotta-0.4.2-windows-x86_64.exe: 另一个程序正在使用此文件"
        )
        # The deletion failed, so the page must not pretend the component is gone.
        assert page.current_state() is LinkState.IDLE
    finally:
        _close(app, page)


def test_a_locked_component_asks_before_ending_the_core(app, monkeypatch) -> None:
    """The sharing violation is the one failure the user can solve here: ask, then retry."""
    locked_detail = (
        "terracotta-0.4.2-windows-x86_64.exe: 另一个程序正在使用此文件，进程无法访问。"
    )

    class _LockedSession(FakeLinkSession):
        def __init__(self) -> None:
            super().__init__(state=LinkState.IDLE)
            self.calls: list[str] = []

        def remove_core(self) -> int:
            self.calls.append("remove_core")
            raise ComponentError("tools.disable.locked", locked_detail)

        def force_remove_core(self) -> int:
            self.calls.append("force_remove_core")
            self._reset(LinkState.NOT_DOWNLOADED)
            return 2048

    session = _LockedSession()
    dialogs = _answers(
        monkeypatch,
        [inwindow_dialog.Outcome.ACCEPTED, inwindow_dialog.Outcome.ACCEPTED],
    )
    seen = _statuses(monkeypatch)
    page = _show(app, session)
    try:
        page.disable_link.click()
        app.processEvents()
        assert len(dialogs) == 2, "the lock has to produce a second, explicit question"
        assert dialogs[0].title_label.text() == i18n.tr("tools.disable.title")
        locked = dialogs[1]
        assert locked.title_label.text() == i18n.tr("tools.disable.locked.title")
        assert locked.body_label.text() == i18n.tr("tools.disable.locked.message")
        assert [button.text() for button in locked.buttons] == [
            i18n.tr("dialog.cancel"),
            i18n.tr("tools.disable.locked.confirm"),
        ]
        assert locked.default_button is not None
        assert locked.default_button.text() == i18n.tr("dialog.cancel")
        body = locked.body_label.text()
        assert "结束" in body, "the action the button will take has to be stated"
        assert "HMCL" in body, "another program's room disconnecting has to be stated"
        assert "重新下载" in body, "the need to download again has to be stated"
        assert session.calls == ["remove_core", "force_remove_core"], "the retry is a separate call"
        assert page.current_state() is LinkState.NOT_DOWNLOADED
        assert seen == [(i18n.tr("tools.disable.done", "0.0"), None)]
    finally:
        _close(app, page)


def test_cancelling_the_locked_question_keeps_the_component_and_reports_nothing(
    app, monkeypatch, ws_tmp
) -> None:
    """The second cancel is a no: no process is ended, no error is shown, the files survive."""
    data_dir = ws_tmp / "disable_locked_cancel"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data_dir))
    terracotta, easytier = _component_tree(data_dir)

    class _LockedSession(FakeLinkSession):
        def __init__(self) -> None:
            super().__init__(state=LinkState.IDLE)
            self.calls: list[str] = []

        def remove_core(self) -> int:
            self.calls.append("remove_core")
            raise ComponentError(
                "tools.disable.locked",
                "terracotta-0.4.2-windows-x86_64.exe: 另一个程序正在使用此文件",
            )

        def force_remove_core(self) -> int:
            self.calls.append("force_remove_core")
            raise AssertionError("a cancelled question must not end a process")

    session = _LockedSession()
    dialogs = _answers(
        monkeypatch,
        [inwindow_dialog.Outcome.ACCEPTED, inwindow_dialog.Outcome.REJECTED],
    )
    seen = _statuses(monkeypatch)
    page = _show(app, session)
    try:
        page.disable_link.click()
        app.processEvents()
        assert len(dialogs) == 2
        assert session.calls == ["remove_core"]
        assert seen == [], "a cancelled second question is not an error"
        assert page.current_state() is LinkState.IDLE
        assert (terracotta / "terracotta-0.4.2-windows-x86_64.exe").is_file()
        assert easytier.exists()
    finally:
        _close(app, page)


def test_a_non_lock_failure_is_reported_without_the_second_question(app, monkeypatch) -> None:
    """Only a sharing violation offers to end a process; anything else is reported as it is."""
    class _RefusingSession(FakeLinkSession):
        def remove_core(self) -> int:
            raise ComponentError("tools.disable.fail", "Access is denied")

        def force_remove_core(self) -> int:
            raise AssertionError("an ordinary denial must not offer to end a process")

    dialogs = _answers(monkeypatch, [inwindow_dialog.Outcome.ACCEPTED])
    seen = _statuses(monkeypatch)
    page = _show(app, _RefusingSession(state=LinkState.IDLE))
    try:
        page.disable_link.click()
        app.processEvents()
        assert len(dialogs) == 1
        assert seen == [(i18n.tr("tools.disable.fail", "Access is denied"), "error")]
        assert page.current_state() is LinkState.IDLE
    finally:
        _close(app, page)


def test_a_failed_forced_deletion_reports_the_reason_and_keeps_the_state(app, monkeypatch) -> None:
    """If the process is gone or cannot be ended and the files still refuse, say so."""
    class _StubbornSession(FakeLinkSession):
        def remove_core(self) -> int:
            raise ComponentError("tools.disable.locked", "terracotta.exe: file in use")

        def force_remove_core(self) -> int:
            raise ComponentError("tools.disable.fail", "terracotta.exe: still in use")

    dialogs = _answers(
        monkeypatch,
        [inwindow_dialog.Outcome.ACCEPTED, inwindow_dialog.Outcome.ACCEPTED],
    )
    seen = _statuses(monkeypatch)
    page = _show(app, _StubbornSession(state=LinkState.IDLE))
    try:
        page.disable_link.click()
        app.processEvents()
        assert len(dialogs) == 2
        assert seen == [
            (i18n.tr("tools.disable.fail", "terracotta.exe: still in use"), "error")
        ]
        assert page.current_state() is LinkState.IDLE, "nothing was deleted, so nothing may move"
    finally:
        _close(app, page)


def test_fake_force_remove_core_never_touches_the_real_component_tree(
    monkeypatch, ws_tmp
) -> None:
    """The fake changes its own state only; a page test must leave the real payload alone."""
    data_dir = ws_tmp / "fake_force_remove"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data_dir))
    terracotta, easytier = _component_tree(data_dir)
    session = FakeLinkSession(state=LinkState.IDLE)
    assert session.force_remove_core() == FakeLinkSession.DEMO_REMOVED_BYTES
    assert session.state() is LinkState.NOT_DOWNLOADED
    assert (terracotta / "terracotta-0.4.2-windows-x86_64.exe").is_file()
    assert (easytier / "easytier-core.exe").is_file()


def test_disable_link_on_the_onboarding_page_deletes_nothing(app, monkeypatch) -> None:
    """With nothing installed there is no question to ask and no file to delete."""
    asked: list[object] = []
    monkeypatch.setattr(
        inwindow_dialog.InWindowDialog,
        "ask",
        lambda self: asked.append(self) or inwindow_dialog.Outcome.REJECTED,
    )

    class _Session(FakeLinkSession):
        def remove_core(self) -> int:
            raise AssertionError("nothing is installed, so nothing may be removed")

    page = _show(app, _Session(state=LinkState.NOT_DOWNLOADED))
    seen = _statuses(monkeypatch)
    try:
        page.disable_link.click()
        app.processEvents()
        assert asked == []
        assert seen == [(i18n.tr("tools.disable.none"), None)]
        assert page.current_state() is LinkState.NOT_DOWNLOADED
    finally:
        _close(app, page)


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



def test_tools_is_the_nav_entry_after_resources(app, monkeypatch, ws_tmp) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game"))
    (ws_tmp / "game").mkdir()
    from gui import nav_icons
    from gui.main_window import NAV_KEYS, MainWindow

    assert NAV_KEYS.index("mods") == 5
    # 截图 / 服务器 left the rail for the instance detail sidebar; favorites was deleted.
    assert NAV_KEYS[NAV_KEYS.index("mods") + 1] == "tools"
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


