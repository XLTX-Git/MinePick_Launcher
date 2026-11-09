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

"""The process-wide owner of the multiplayer :class:`~launcher.link.session.LinkSession`.

The 工具 › 联机 page used to construct and dispose the session itself. That is wrong for a detached
backend: switching the interface language rebuilds every page, so the poller stopped while the
Terracotta process kept the room alive, and nothing in the rest of the window knew the room existed.
Ownership therefore sits here instead:

* the window creates a bus and asks it for the session (``session()``) - lazily, and only once;
* pages observe the bus (``add_listener``) and never dispose the session: rebuilding or destroying a
  page only unsubscribes its listener, so the poller and the room survive the rebuild;
* the window calls ``shutdown()`` when it closes. That stops polling; the detached Terracotta process
  and its room keep running on purpose.

``start_when_room_running`` is the restart path: Terracotta advertises its port through a lock file
(:func:`launcher.link.terracotta.TerracottaProcess.existing_port` verifies it still answers ``/meta``),
so after a launcher restart the bus creates the session, the session's poller attaches to the running
service and reads the current ``/state`` back. That is the same mechanism that makes the room outlive
the launcher in the first place.

The module is Qt-free; listeners are plain callbacks and may be invoked from the session's poll thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from launcher.link.session import LinkSession, LinkState, default_link_session

logger = logging.getLogger(__name__)

#: The states in which a room exists and the window must show its 联机中 capsule. ``HOST_WAITING`` is
#: not one of them: the room has not been created yet and there is no code to show. ``FAILED`` is not
#: either: the backend can still be running, but the launcher is not in a room.
ACTIVE_STATES: frozenset[LinkState] = frozenset(
    {LinkState.HOST_READY, LinkState.JOINED, LinkState.CONNECTING}
)

#: How much of the room code fits in the title-bar capsule (``U/8F3K`` out of ``U/8F3K-2QX9-...``).
SHORT_CODE_LENGTH = 6


@dataclass(frozen=True, slots=True)
class LinkRoomSummary:
    """What the window needs to know about the room without touching the session's whole API."""

    state: LinkState = LinkState.IDLE
    room_code: str = ""

    @property
    def active(self) -> bool:
        """Whether the room exists now (the capsule is visible exactly then)."""
        return self.state in ACTIVE_STATES

    def short_code(self, limit: int = SHORT_CODE_LENGTH) -> str:
        """The first segment of the room code, cut to ``limit`` characters (empty when no code)."""
        code = str(self.room_code or "").strip()
        if not code:
            return ""
        return code.split("-", 1)[0][:limit]


def running_terracotta_port(process: object | None = None) -> int | None:
    """The port of a Terracotta service that is still running, or ``None``.

    A separate probe from the session so the window can decide *whether* to create one at startup:
    the lock file alone is not enough (a killed process leaves it behind), which is why
    :meth:`TerracottaProcess.existing_port` also verifies ``/meta``. ``process`` exists for tests and
    accepts anything with the same ``existing_port`` method.
    """
    try:
        probe = process
        if probe is None:
            from launcher.link.terracotta import TerracottaProcess

            probe = TerracottaProcess()
        existing_port = getattr(probe, "existing_port", None)
        if not callable(existing_port):
            return None
        port = existing_port()
        return int(port) if port else None
    except Exception:  # a failed probe means "no running room", never a broken startup
        logger.debug("探测已运行的陶瓦实例失败", exc_info=True)
        return None


class LinkSessionBus:
    """Owns one :class:`LinkSession` for the whole process; pages only observe it.

    Ownership model: the bus creates the session on first use (or when a running room is detected at
    startup), hands it to every observer, and is the only object that disposes it - in
    :meth:`shutdown`, called from the window's close. A page's ``close``/``deleteLater`` must not call
    ``dispose``; it only removes its listener so the poller keeps running.

    Notifications are forwarded from the session to the bus listeners, so observers registered before
    the session exists still receive the first state change. ``replace_session`` is for tests and the
    screenshot tool: it swaps the observed backend and notifies, without disposing anything the caller
    did not hand over.
    """

    def __init__(
        self,
        session_factory: Callable[[], LinkSession] = default_link_session,
    ) -> None:
        self._factory = session_factory
        self._lock = threading.RLock()
        self._session: LinkSession | None = None
        self._unsubscribe_session: Callable[[], None] | None = None
        self._listeners: list[Callable[[], None]] = []

    # -- ownership --------------------------------------------------------
    def session(self) -> LinkSession:
        """The one session of this bus, created on first use."""
        with self._lock:
            if self._session is not None:
                return self._session
            session = self._factory()
            self._adopt_locked(session)
            return session

    def peek_session(self) -> LinkSession | None:
        """The session if it has been created; never creates one (for introspection and tests)."""
        with self._lock:
            return self._session

    def start_when_room_running(self, process: object | None = None) -> bool:
        """Create the session when a Terracotta service is already running; return whether it did.

        This is the restart path described in the module docstring. It is deliberately a probe first:
        with no room running a launcher start must not create a poller at all, which is what keeps the
        normal startup free of a background thread.
        """
        with self._lock:
            if self._session is not None:
                return True
        if running_terracotta_port(process) is None:
            return False
        self.session()
        return True

    def replace_session(self, session: LinkSession) -> LinkSession | None:
        """Observe ``session`` instead; returns the previous one. The caller keeps ownership."""
        with self._lock:
            previous = self._session
            self._adopt_locked(session)
        self._notify()
        return previous

    def shutdown(self) -> None:
        """Stop observing and dispose the session (window close). The room process keeps running."""
        with self._lock:
            session = self._session
            unsubscribe = self._unsubscribe_session
            self._session = None
            self._unsubscribe_session = None
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:
                logger.debug("解除联机总线对会话的订阅失败", exc_info=True)
        if session is not None:
            dispose = getattr(session, "dispose", None)
            if callable(dispose):
                try:
                    dispose()
                except Exception:
                    logger.debug("停止联机会话失败", exc_info=True)
        self._notify()

    def _adopt_locked(self, session: LinkSession) -> None:
        """Install ``session`` and forward its notifications (the bus lock must be held)."""
        previous_unsubscribe = self._unsubscribe_session
        self._session = session
        self._unsubscribe_session = session.add_listener(self._notify)
        if previous_unsubscribe is not None:
            try:
                previous_unsubscribe()
            except Exception:
                logger.debug("解除旧联机会话订阅失败", exc_info=True)

    # -- observation ------------------------------------------------------
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to any session change; the return value unsubscribes."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def _notify(self) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.debug("联机总线监听器异常", exc_info=True)

    # -- state ------------------------------------------------------------
    def summary(self) -> LinkRoomSummary:
        """The room as of now; a missing or broken session reads as "no room"."""
        with self._lock:
            session = self._session
        if session is None:
            return LinkRoomSummary()
        try:
            state = session.state()
            code = session.room_code() if state in ACTIVE_STATES else ""
        except Exception:
            logger.debug("读取联机会话状态失败", exc_info=True)
            return LinkRoomSummary()
        return LinkRoomSummary(state=state, room_code=code)


#: The process's default bus, used by pages built without an explicit owner (tests, tools, and any
#: future page that is not the main window's). The main window creates its own bus so a test can
#: drive one window's session without touching this one.
_DEFAULT_BUS = LinkSessionBus()


def default_link_bus() -> LinkSessionBus:
    """The process-wide bus the main window and lone pages fall back to."""
    return _DEFAULT_BUS
