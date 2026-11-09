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

"""The persistent 联机中 capsule in the title bar.

It must appear exactly while a room exists (``HOST_READY`` / ``JOINED`` / ``CONNECTING``), bring the
user back to the live 工具 › 联机 page without restarting anything, disappear the moment the room
closes, and never change the window's width or height. The startup case uses a real lock file and a
real (fake) HTTP service, so it exercises the same attach path as a launcher restart.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from gui import i18n
from launcher.link import FakeLinkSession, LinkMember, LinkSessionBus, LinkState
from tests.link_fakes import FakeTerracotta, component_ready

ROOM = "U/8F3K-2QX9-7WMD-4TRA"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn_language():
    previous = i18n.current_language()
    i18n.set_language("zh_cn")
    yield
    i18n.set_language(previous)


def _pump(app, predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def _settle(app, times: int = 6) -> None:
    for _ in range(times):
        app.processEvents()
        time.sleep(0.02)


def _window(app, monkeypatch, ws_tmp, suffix: str):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / f"data_badge_{suffix}"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / f"game_badge_{suffix}"))
    (ws_tmp / f"game_badge_{suffix}").mkdir()
    from gui.main_window import MainWindow

    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1000, 610)
    window.show()
    _settle(app)
    return window


def _close(app, window) -> None:
    window.close()
    window.deleteLater()
    app.processEvents()


def _title_bar_clipping(window) -> list[str]:
    """Direct children of the title bar that leave its rectangle (a clipped capsule is a failure)."""
    bar = window.title_bar
    problems: list[str] = []
    for child in bar.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
        if child.isHidden():
            continue
        geometry = child.geometry()
        if (
            geometry.left() < -1
            or geometry.top() < -1
            or geometry.right() > bar.width() + 1
            or geometry.bottom() > bar.height() + 1
        ):
            problems.append(f"{child.objectName() or type(child).__name__}: {geometry}")
    return problems


# ------------------------------------------------------------------ visibility


@pytest.mark.parametrize(
    "state", [LinkState.HOST_READY, LinkState.JOINED, LinkState.CONNECTING]
)
def test_the_capsule_appears_for_every_room_state(
    app, monkeypatch, ws_tmp, state: LinkState
) -> None:
    window = _window(app, monkeypatch, ws_tmp, state.value)
    try:
        assert not window.link_capsule.isVisibleTo(window)
        window.link_bus.replace_session(
            FakeLinkSession(state=state, room_code=ROOM)
        )
        assert _pump(app, lambda: window.link_capsule.isVisibleTo(window))
        assert window.link_capsule.text() == i18n.tr("link.badge.active", "U/8F3K")
        assert window.link_capsule.accessibleName() == window.link_capsule.text()
        assert window.link_capsule.toolTip() == i18n.tr("link.badge.tooltip")
    finally:
        _close(app, window)


@pytest.mark.parametrize(
    "state",
    [
        LinkState.NOT_DOWNLOADED,
        LinkState.IDLE,
        LinkState.HOST_WAITING,
        LinkState.FAILED,
    ],
)
def test_the_capsule_stays_hidden_without_a_room(
    app, monkeypatch, ws_tmp, state: LinkState
) -> None:
    window = _window(app, monkeypatch, ws_tmp, f"hidden_{state.value}")
    try:
        window.link_bus.replace_session(
            FakeLinkSession(state=state, room_code=ROOM)
        )
        _settle(app)
        assert not window.link_capsule.isVisibleTo(window)
    finally:
        _close(app, window)


def test_the_capsule_disappears_as_soon_as_the_room_closes(
    app, monkeypatch, ws_tmp
) -> None:
    window = _window(app, monkeypatch, ws_tmp, "close")
    try:
        session = FakeLinkSession(state=LinkState.HOST_READY, room_code=ROOM)
        window.link_bus.replace_session(session)
        assert _pump(app, lambda: window.link_capsule.isVisibleTo(window))

        session.close_room()
        assert _pump(app, lambda: not window.link_capsule.isVisibleTo(window))
    finally:
        _close(app, window)


# ------------------------------------------------------------------ navigation


def test_clicking_the_capsule_opens_the_live_room_page(
    app, monkeypatch, ws_tmp
) -> None:
    """The page shows the room that exists now; opening it must not start a new attempt."""
    from gui.main_window import NAV_KEYS

    window = _window(app, monkeypatch, ws_tmp, "click")
    try:
        session = FakeLinkSession(
            state=LinkState.HOST_READY,
            room_code=ROOM,
            members=(LinkMember("Steve", is_host=True), LinkMember("Alex")),
            address="127.0.0.1:25566",
        )
        window.link_bus.replace_session(session)
        window.sidebar.setCurrentRow(NAV_KEYS.index("settings"))
        _settle(app)
        assert _pump(app, lambda: window.link_capsule.isVisibleTo(window))

        window.link_capsule.click()
        app.processEvents()

        page = window.pages["tools"]
        assert window.stack.currentWidget() is page
        assert window.sidebar.currentRow() == NAV_KEYS.index("tools")
        assert page.tabs.currentIndex() == 0
        assert page.current_state() is LinkState.HOST_READY
        assert page.room_code_label.text() == ROOM
        assert page.member_row_count() == 2
        assert session.state() is LinkState.HOST_READY, "the click must not restart the room"
    finally:
        _close(app, window)


# ------------------------------------------------------------------ geometry


@pytest.mark.parametrize("language", ["zh_cn", "ja_jp"])
def test_the_capsule_does_not_grow_the_window(
    app, monkeypatch, ws_tmp, language: str
) -> None:
    """At the 1000px floor the capsule must fit inside the title bar and add no width or height."""
    i18n.set_language(language)
    window = _window(app, monkeypatch, ws_tmp, f"geometry_{language}")
    try:
        width_before = window.minimumSizeHint().width()
        height_before = window.minimumHeight()
        bar_height = window.title_bar.height()
        assert not window.link_capsule.isVisibleTo(window)

        window.link_bus.replace_session(
            FakeLinkSession(state=LinkState.HOST_READY, room_code=ROOM)
        )
        assert _pump(app, lambda: window.link_capsule.isVisibleTo(window))
        _settle(app)

        assert window.width() == 1000
        assert window.height() == 610
        assert window.minimumHeight() == height_before
        assert window.minimumSizeHint().width() <= width_before, (
            "the capsule must not raise the window's minimum width"
        )
        assert window.title_bar.height() == bar_height == 38
        assert window.link_capsule.height() == 22
        assert window.link_capsule.width() > 0
        assert _title_bar_clipping(window) == []
    finally:
        i18n.set_language("zh_cn")
        _close(app, window)


# ------------------------------------------------------------------ startup attach


def test_a_window_started_next_to_a_running_room_shows_the_capsule(
    app, monkeypatch, ws_tmp
) -> None:
    """Restart path: lock file + reachable port at startup => session attached and capsule shown."""
    from launcher.link.terracotta import TerracottaClient, TerracottaLinkSession

    class _RunningProcess:
        """The part of TerracottaProcess the session uses once the room is already running."""

        def __init__(self, port: int) -> None:
            self.port = port

        def existing_port(self) -> int:
            return self.port

        def ensure_running(self) -> int:
            return self.port

    fake = FakeTerracotta()
    fake.set_state(
        "host-ok",
        index=2,
        room=ROOM,
        profiles=[{"machine_id": "a", "name": "Steve", "vendor": "", "kind": "HOST"}],
    )
    lock = ws_tmp / "terracotta.lock"
    lock.write_bytes(fake.port.to_bytes(2, "big"))
    monkeypatch.setattr(
        "launcher.link.terracotta.terracotta_lock_path", lambda: lock
    )
    session = TerracottaLinkSession(
        process=_RunningProcess(fake.port),
        client_factory=lambda port: TerracottaClient(port, timeout=0.5),
        poll_interval=0.02,
        attach_interval=0.02,
        component_status_fn=component_ready,
        player_name=lambda: "Steve",
    )
    monkeypatch.setattr(
        "gui.main_window.LinkSessionBus",
        lambda: LinkSessionBus(session_factory=lambda: session),
    )
    window = _window(app, monkeypatch, ws_tmp, "attach")
    try:
        assert window.link_bus.peek_session() is session
        assert _pump(app, lambda: window.link_capsule.isVisibleTo(window))
        assert window.link_capsule.text() == i18n.tr("link.badge.active", "U/8F3K")
        assert window.link_bus.summary().state is LinkState.HOST_READY
        assert window.link_bus.summary().room_code == ROOM
    finally:
        _close(app, window)
        fake.stop()
