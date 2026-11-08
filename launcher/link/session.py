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

"""The multiplayer session contract, plus an in-memory implementation for the UI layer.

The page in :mod:`gui.pages.tools_page` never talks to Terracotta directly. It talks to a
:class:`LinkSession`, whose real implementation will own the subprocess, the room protocol and the
state machine. Keeping that boundary here has two consequences the UI work depends on:

* the whole page can be built and driven without launching anything;
* a test can script an exact sequence of states through :class:`FakeLinkSession`, which is also what
  the screenshot tool uses to reach the six reviewed layouts.

The module is deliberately Qt-free: it belongs to the core library, and only the page turns state
changes into widgets. Listeners are plain callbacks rather than Qt signals for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class LinkState(str, Enum):
    """Every state the 联机 panel can show.

    ``FAILED`` is not one of the six layouts in the design review; it is the state that carries a
    translation key for whichever operation failed, so the UI has a truthful fallback instead of
    freezing on the previous screen.
    """

    NOT_DOWNLOADED = "not_downloaded"
    IDLE = "idle"
    HOST_WAITING = "host_waiting"
    HOST_READY = "host_ready"
    CONNECTING = "connecting"
    JOINED = "joined"
    FAILED = "failed"


class LinkDifficulty(str, Enum):
    """NAT traversal difficulty reported by Terracotta while a guest is connecting."""

    EASY = "easy"
    MEDIUM = "medium"
    TOUGH = "tough"


@dataclass(frozen=True, slots=True)
class LinkMember:
    """One person in the room.

    ``avatar`` is an optional display initial. The page falls back to the first character of
    ``name``; a backend that knows a different short form (the local player is "你" but its avatar
    reads "我") can supply one here.
    """

    name: str
    is_host: bool = False
    online: bool = True
    avatar: str = ""


#: Download progress is reported on the same 0..1000 scale as every other download in the launcher
#: (``QProgressBar.setRange(0, 1000)`` and a tenth of a percent per step).
ProgressCallback = Callable[[int], None]
#: Called after any observable property changes. May be invoked from a worker thread by a future real
#: implementation, so a listener that touches widgets must marshal the change to the GUI thread.
Listener = Callable[[], None]


class LinkSession(Protocol):
    """What the page is allowed to ask a multiplayer backend to do."""

    def state(self) -> LinkState:
        """The current state."""

    def room_code(self) -> str:
        """The room code, once one exists; empty otherwise."""

    def members(self) -> tuple[LinkMember, ...]:
        """Everyone the backend currently knows about, host included."""

    def difficulty(self) -> LinkDifficulty | None:
        """Traversal difficulty while connecting; ``None`` when the backend has not reported one."""

    def address(self) -> str:
        """The direct-connect address shown after joining, e.g. ``127.0.0.1:25566``."""

    def failure_key(self) -> str:
        """Translation key describing the last failure; empty when the state is not ``FAILED``."""

    def join(self, code: str) -> None:
        """Start joining the room with this code."""

    def host(self, instance_id: str) -> None:
        """Start hosting the room for one instance id."""

    def cancel(self) -> None:
        """Cancel connecting or hosting and return to ``IDLE``."""

    def close_room(self) -> None:
        """Close the room and return to ``IDLE``."""

    def download_core(self, progress_cb: ProgressCallback) -> None:
        """Download the Terracotta core, reporting 0..1000 through ``progress_cb``."""

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Subscribe to state changes; the return value unsubscribes."""


class FakeLinkSession:
    """In-memory :class:`LinkSession` for tests, screenshots and the pre-backend UI.

    The normal methods model what a user action does; ``set_state`` (and the convenience helpers
    ``publish`` / ``complete_join`` / ``fail``) model what an asynchronous backend would report a
    moment later. Nothing here sleeps, touches the network or starts a process.
    """

    DEFAULT_ROOM_CODE = "U/8F3K-2QX9-7WMD-4TRA"
    DEFAULT_ADDRESS = "127.0.0.1:25566"

    def __init__(
        self,
        *,
        state: LinkState = LinkState.NOT_DOWNLOADED,
        room_code: str = "",
        members: Iterable[LinkMember] = (),
        difficulty: LinkDifficulty | None = None,
        address: str = "",
        failure_key: str = "",
    ) -> None:
        self._state = state
        self._room_code = room_code
        self._members = tuple(members)
        self._difficulty = difficulty
        self._address = address
        self._failure_key = failure_key
        self._listeners: list[Listener] = []
        self._instance_id = ""

    # -- observable state -------------------------------------------------
    def state(self) -> LinkState:
        return self._state

    def room_code(self) -> str:
        return self._room_code

    def members(self) -> tuple[LinkMember, ...]:
        return self._members

    def difficulty(self) -> LinkDifficulty | None:
        return self._difficulty

    def address(self) -> str:
        return self._address

    def failure_key(self) -> str:
        return self._failure_key

    def instance_id(self) -> str:
        """The instance passed to the last ``host`` call (useful in tests)."""
        return self._instance_id

    # -- notification -----------------------------------------------------
    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    # -- user actions -----------------------------------------------------
    def join(self, code: str) -> None:
        code = str(code or "").strip()
        if not code:
            self.fail()
            return
        self._room_code = code
        self._members = ()
        self._difficulty = LinkDifficulty.TOUGH
        self._address = self._address or self.DEFAULT_ADDRESS
        self._failure_key = ""
        self._state = LinkState.CONNECTING
        self._notify()

    def host(self, instance_id: str) -> None:
        instance_id = str(instance_id or "").strip()
        if not instance_id:
            self.fail()
            return
        self._instance_id = instance_id
        self._room_code = ""
        self._members = ()
        self._difficulty = None
        self._address = ""
        self._failure_key = ""
        self._state = LinkState.HOST_WAITING
        self._notify()

    def cancel(self) -> None:
        if self._state in (
            LinkState.HOST_WAITING,
            LinkState.HOST_READY,
            LinkState.CONNECTING,
            LinkState.JOINED,
            LinkState.FAILED,
        ):
            self._reset(LinkState.IDLE)

    def close_room(self) -> None:
        self._reset(LinkState.IDLE)

    def download_core(self, progress_cb: ProgressCallback) -> None:
        if self._state is not LinkState.NOT_DOWNLOADED:
            return
        for value in (0, 500, 1000):
            progress_cb(value)
        self._state = LinkState.IDLE
        self._failure_key = ""
        self._notify()

    # -- scripted backend events ------------------------------------------
    def publish(
        self,
        *,
        room_code: str = "",
        members: Iterable[LinkMember] | None = None,
        difficulty: LinkDifficulty | None = None,
    ) -> None:
        """Move from ``HOST_WAITING`` to ``HOST_READY`` (the LAN world was opened)."""
        self._room_code = room_code or self.DEFAULT_ROOM_CODE
        self._members = (
            tuple(members) if members is not None else (LinkMember("你", is_host=True),)
        )
        self._difficulty = difficulty
        self._address = self.DEFAULT_ADDRESS
        self._failure_key = ""
        self._state = LinkState.HOST_READY
        self._notify()

    def complete_join(
        self,
        *,
        members: Iterable[LinkMember] | None = None,
        address: str = "",
    ) -> None:
        """Move from ``CONNECTING`` to ``JOINED`` once the P2P link is up."""
        self._members = (
            tuple(members)
            if members is not None
            else (LinkMember("你"), LinkMember("小王"))
        )
        self._address = address or self.DEFAULT_ADDRESS
        self._failure_key = ""
        self._state = LinkState.JOINED
        self._notify()

    def fail(self, reason_key: str = "") -> None:
        """Move to ``FAILED`` with an i18n key; an empty key shows the generic failure title."""
        self._failure_key = reason_key
        self._state = LinkState.FAILED
        self._notify()

    def set_state(
        self,
        state: LinkState,
        *,
        room_code: str | None = None,
        members: Iterable[LinkMember] | None = None,
        difficulty: LinkDifficulty | None = None,
        address: str | None = None,
        failure_key: str | None = None,
    ) -> None:
        """Script the next backend state directly, without going through a user action."""
        self._state = state
        if room_code is not None:
            self._room_code = room_code
        if members is not None:
            self._members = tuple(members)
        if difficulty is not None:
            self._difficulty = difficulty
        if address is not None:
            self._address = address
        if failure_key is not None:
            self._failure_key = failure_key
        if state is not LinkState.FAILED and failure_key is None:
            self._failure_key = ""
        self._notify()

    def _reset(self, state: LinkState) -> None:
        self._state = state
        self._room_code = ""
        self._members = ()
        self._difficulty = None
        self._address = ""
        self._failure_key = ""
        self._instance_id = ""
        self._notify()


def default_link_session() -> LinkSession:
    """The session a freshly built page uses until the real backend lands.

    It is the fake on purpose: this change is the UI skeleton, and a page that cannot render without
    a subprocess would fail every audit and screenshot. The next change replaces this one factory.
    """
    return FakeLinkSession()
