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

"""The multiplayer session bus: ownership, page rebuilds, and attaching a room at startup.

The point of the bus is a lifecycle the page cannot provide: the session (and its polling thread)
outlives every page, and the window is the only owner that disposes it. The tests drive the real
``TerracottaLinkSession`` against a local fake HTTP service, so "still polling" is a measured fact
rather than a flag.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from gui.pages.tools_page import ToolsPage
from launcher.link import LinkState, TerracottaLinkSession
from launcher.link.bus import ACTIVE_STATES, LinkRoomSummary, LinkSessionBus
from launcher.link.session import FakeLinkSession
from launcher.link.terracotta import TerracottaClient, TerracottaProcess
from tests.link_fakes import FakeCore, FakeTerracotta, component_ready, wait_until

ROOM = "U/8F3K-2QX9-7WMD-4TRA"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _pump(app, predicate, timeout: float = 3.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def _session(fake: FakeTerracotta, **kwargs) -> TerracottaLinkSession:
    client_factory = kwargs.pop(
        "client_factory", lambda port: TerracottaClient(port, timeout=0.5)
    )
    return TerracottaLinkSession(
        process=kwargs.pop("process", FakeCore(fake.port)),
        client_factory=client_factory,
        poll_interval=0.02,
        attach_interval=0.02,
        component_status_fn=component_ready,
        player_name=lambda: "Steve",
        **kwargs,
    )


# ------------------------------------------------------------------ summary


@pytest.mark.parametrize(
    "state", [LinkState.HOST_READY, LinkState.JOINED, LinkState.CONNECTING]
)
def test_every_room_state_is_active(state: LinkState) -> None:
    bus = LinkSessionBus()
    try:
        bus.replace_session(FakeLinkSession(state=state, room_code=ROOM))
        summary = bus.summary()
        assert summary.active
        assert summary.room_code == ROOM
        assert summary.short_code() == "U/8F3K"
    finally:
        bus.shutdown()


@pytest.mark.parametrize(
    "state",
    [
        LinkState.NOT_DOWNLOADED,
        LinkState.IDLE,
        LinkState.HOST_WAITING,
        LinkState.FAILED,
    ],
)
def test_no_other_state_shows_a_room(state: LinkState) -> None:
    bus = LinkSessionBus()
    try:
        bus.replace_session(FakeLinkSession(state=state, room_code=ROOM))
        summary = bus.summary()
        assert not summary.active
        assert summary.room_code == "", "without a room there is no code to show"
    finally:
        bus.shutdown()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("", ""),
        ("U/8F3K", "U/8F3K"),
        ("U/8F3K-2QX9-7WMD-4TRA", "U/8F3K"),
        ("ABCDEFGHIJ", "ABCDEF"),
        ("-leading", ""),
    ],
)
def test_the_capsule_code_is_the_first_short_segment(code: str, expected: str) -> None:
    assert LinkRoomSummary(room_code=code).short_code() == expected


def test_the_three_room_states_are_the_documented_set() -> None:
    assert ACTIVE_STATES == {
        LinkState.HOST_READY,
        LinkState.JOINED,
        LinkState.CONNECTING,
    }


# ------------------------------------------------------------------ listeners


def test_listeners_hear_every_room_change_and_the_shutdown() -> None:
    bus = LinkSessionBus()
    states: list[LinkState] = []
    unsubscribe = bus.add_listener(lambda: states.append(bus.summary().state))
    fake = FakeLinkSession(state=LinkState.IDLE)
    try:
        bus.replace_session(fake)
        assert states == [LinkState.IDLE]

        fake.set_state(LinkState.HOST_READY, room_code=ROOM)
        assert states[-1] is LinkState.HOST_READY

        fake.close_room()
        assert states[-1] is LinkState.IDLE

        unsubscribe()
        fake.set_state(LinkState.JOINED)
        assert states[-1] is LinkState.IDLE, "an unsubscribed listener must not run"
    finally:
        bus.shutdown()


# ------------------------------------------------------------------ page lifecycle


def test_destroying_the_page_keeps_the_bus_session_polling(app) -> None:
    """The regression this exists for: a rebuilt page used to dispose the session and stop polling."""
    fake = FakeTerracotta()
    session = _session(fake)
    bus = LinkSessionBus(session_factory=lambda: session)
    page = ToolsPage(bus=bus)
    page.resize(820, 600)
    page.show()
    try:
        assert wait_until(lambda: session.state() is LinkState.IDLE)
        assert session.is_polling()
        assert bus.peek_session() is session

        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

        assert bus.peek_session() is session
        assert session.is_polling(), "destroying the page must not stop the bus's poller"
        # The poller still follows the backend after the page is gone.
        fake.set_state(
            "host-ok",
            room=ROOM,
            profiles=[{"name": "Steve", "kind": "HOST"}],
        )
        assert wait_until(lambda: bus.summary().active)
        assert bus.summary().state is LinkState.HOST_READY
        assert bus.summary().room_code == ROOM
    finally:
        bus.shutdown()
        assert wait_until(lambda: not session.is_polling())
        fake.stop()


def test_a_language_switch_rebuild_keeps_the_session_and_poller(app, monkeypatch, ws_tmp) -> None:
    """The window rebuilds every page on a language switch; the bus's session must survive it."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_bus"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game_bus"))
    (ws_tmp / "game_bus").mkdir()
    from gui import i18n
    from gui.main_window import NAV_KEYS, MainWindow

    fake = FakeTerracotta()
    session = _session(fake)
    i18n.set_language("zh_cn")
    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()
    try:
        window.link_bus.replace_session(session)
        window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
        assert _pump(app, lambda: session.state() is LinkState.IDLE)
        first = window.pages["tools"]
        assert first.session() is session
        assert session.is_polling()

        i18n.set_language("en_us")
        window.build_pages()
        app.processEvents()

        assert window.link_bus.peek_session() is session, "the rebuild must not replace the session"
        assert session.is_polling(), "the rebuild must not stop the polling thread"

        window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
        app.processEvents()
        second = window.pages["tools"]
        assert second is not first
        assert second.session() is session
        assert second.current_state() is LinkState.IDLE
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")
        assert wait_until(lambda: not session.is_polling()), (
            "closing the window is what stops the bus's poller"
        )
        fake.stop()


# ------------------------------------------------------------------ startup attach


def test_startup_attaches_a_room_left_running_by_a_previous_launcher(ws_tmp) -> None:
    """Lock file + reachable port = a room to show; the real HTTP client reads its state back."""
    fake = FakeTerracotta()
    fake.set_state(
        "host-ok",
        index=2,
        room=ROOM,
        profiles=[
            {"machine_id": "a", "name": "Steve", "vendor": "", "kind": "HOST"},
            {"machine_id": "b", "name": "Alex", "vendor": "", "kind": "GUEST"},
        ],
    )
    lock = ws_tmp / "terracotta.lock"
    lock.write_bytes(fake.port.to_bytes(2, "big"))

    def no_launch(*_args, **_kwargs):
        raise AssertionError("an already running instance must be attached, not launched")

    process = TerracottaProcess(
        lock_path=lock,
        executable=ws_tmp / "terracotta.exe",
        popen=no_launch,
    )
    bus = LinkSessionBus(
        session_factory=lambda: _session(fake, process=process)
    )
    try:
        assert bus.start_when_room_running(process=process) is True
        assert wait_until(lambda: bus.summary().active)
        summary = bus.summary()
        assert summary.state is LinkState.HOST_READY
        assert summary.room_code == ROOM
        assert summary.short_code() == "U/8F3K"
    finally:
        bus.shutdown()
        fake.stop()


def test_a_normal_start_creates_no_session_and_no_poller(ws_tmp) -> None:
    """No lock file / dead port means "stay lazy": the page creates the session on first visit."""
    process = TerracottaProcess(
        lock_path=ws_tmp / "absent.lock",
        executable=ws_tmp / "terracotta.exe",
        popen=lambda *_args, **_kwargs: pytest.fail("nothing may be launched at startup"),
    )
    bus = LinkSessionBus()
    try:
        assert bus.start_when_room_running(process=process) is False
        assert bus.peek_session() is None
        assert not bus.summary().active
    finally:
        bus.shutdown()
