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

"""The task model behind the window's download page: one download, one object.

Before this module the versions page tracked a download in the page's own widgets - the bottom bar held
the percentage, the row held a copy of it, and a failure existed only as one status line that the next
message overwrote. That works for one download and says nothing about a second one, about what finished
five seconds ago, or about the one the user cancelled halfway through.

So the download reports to a task instead: ``add_task(title)`` returns an object whose whole surface is
``set_progress(value, total, speed)``, ``finish()``, ``fail(message)`` and ``cancel()``. The window's
corner icon and its download page are then two views of that list - they hold no state of their own,
which is what keeps "the icon says 42%" and "the row says 42%" from ever disagreeing.

Deliberately not a widget and deliberately not a QObject: a task is data plus a state machine, so the
whole list - including the five-second removal and the two shots the icon's state depends on - can be
tested without a window, a timer loop or a download.

Finished is not one state. ``done`` and ``cancelled`` are the two endings the user does not have to act
on, so they leave the list by themselves after ``COMPLETED_HOLD_MS``; a ``failed`` task stays until it is
dismissed, because a failure is information nobody has read yet. A cancelled task that was in fact
finished by the time the cancel arrived becomes ``done`` - the bytes are on disk either way, and calling
that "cancelled" would be a lie the next launch contradicts.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from gui import i18n
from gui.workers import format_rate

tr = i18n.tr

#: The four states a task can be in. ``RUNNING`` is the only one that can still change.
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"

#: The state text of each, as an i18n key. The download page prints the value, tests compare against it.
STATE_KEYS = {
    RUNNING: "tasks.state.running",
    DONE: "tasks.state.done",
    CANCELLED: "tasks.state.cancelled",
    FAILED: "tasks.state.failed",
}

#: The six kinds of job the launcher runs as a task, and the title each one is listed under. The kind
#: is what picks the row's glyph on the download page; the title key is what the row says.
KIND_TITLE_KEYS: dict[str, str] = {
    "version": "tasks.title.install",
    "mod": "tasks.title.mod",
    "modpack": "tasks.title.modpack",
    "resourcepack": "tasks.title.resourcepack",
    "shaderpack": "tasks.title.shaderpack",
    # An export is not a download, but it is a job with a real byte count and a place on the same
    # page: the instance page starts it, the row shows its progress and the status line says where
    # the archive landed.
    "export": "tasks.title.export",
}

#: The reverse map: a task created by title key alone still gets its kind (the versions page's adapter
#: creates "tasks.title.install" and must not have to know the download page's vocabulary).
TITLE_KINDS: dict[str, str] = {key: kind for kind, key in KIND_TITLE_KEYS.items()}

#: How long a finished task stays in the list before removing itself.
COMPLETED_HOLD_MS = 5000
#: How long a cancelled task stays. Longer than a finished one on purpose: a cancelled row carries the
#: percentage it was stopped at, and that is the only place the user can read where it stopped.
CANCELLED_HOLD_MS = 10000


def format_size(num_bytes: float) -> str:
    """A byte count a person reads: "3.4 MB", "512 KB", "12 B" (the units the status line uses)."""
    value = max(0.0, float(num_bytes))
    if value >= 1 << 20:
        return f"{value / (1 << 20):.1f} MB"
    if value >= 1 << 10:
        return f"{value / (1 << 10):.0f} KB"
    return f"{value:.0f} B"


class Task:
    """One background job: its title, its numbers, the way it ended, and the way to stop it.

    A task is created by :meth:`TaskCenter.add_task` and owned by the center from then on - the
    producer (the install flow's adapter) keeps the object to report into, not to manage. Every mutator
    is guarded by the state machine, so a late progress callback after ``finish()`` cannot resurrect a
    settled task, and a second ``cancel()`` is a no-op instead of a second stop request.
    """

    def __init__(
        self,
        center: TaskCenter,
        title_key: str,
        title_args: tuple[object, ...] = (),
        cancel: Callable[[Task], None] | None = None,
        kind: str = "",
        retry: Callable[[], None] | None = None,
    ) -> None:
        self._center = center
        self._title_key = title_key
        self._title_args = tuple(title_args)
        self._cancel = cancel
        self._retry = retry
        self._kind = kind or TITLE_KINDS.get(title_key, "")
        self._state = RUNNING
        self._done_bytes = 0
        self._total_bytes = 0
        self._speed_bps = 0.0
        self._error = ""
        self._timer: QTimer | None = None
        self._started_at = time.monotonic()
        self._ended_at: float | None = None

    # ---------- data ----------

    @property
    def state(self) -> str:
        return self._state

    @property
    def kind(self) -> str:
        """Which job this is (``version``/``mod``/``modpack``/.../``export``), for the row's glyph."""
        return self._kind

    @property
    def error(self) -> str:
        """Why a failed task failed (empty for every other state)."""
        return self._error

    @property
    def done_bytes(self) -> int:
        return self._done_bytes

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def speed_bps(self) -> float:
        return self._speed_bps

    def is_running(self) -> bool:
        return self._state == RUNNING

    def has_total(self) -> bool:
        """Whether the producer announced a full size.

        A server that sends no ``Content-Length`` (and no task size) leaves it at 0. The row then
        says "unknown size" and the bar runs as a busy indicator instead of claiming a 0% download.
        """
        return self._total_bytes > 0

    def is_finished(self) -> bool:
        """True for every ending, including a failure that is still on screen."""
        return self._state != RUNNING

    def title(self) -> str:
        """The row's heading, translated on read so a language switch renames a task still listed."""
        return tr(self._title_key, *self._title_args)

    def state_text(self) -> str:
        return tr(STATE_KEYS[self._state])

    def percent(self) -> int:
        """Downloaded share of the announced size, 0-100 (0 while the size is still unknown)."""
        if self._total_bytes <= 0:
            return 0
        return max(0, min(100, round(self._done_bytes * 100 / self._total_bytes)))

    def percent_text(self) -> str:
        if not self.has_total():
            return "—"
        return f"{self.percent()}%"

    def size_text(self) -> str:
        """``downloaded / total`` - the pair the panel prints together.

        When the source never announced a length, the total half is the localized "unknown size"
        instead of a second 0 B, which read as a stuck download rather than a missing number.
        """
        total = format_size(self._total_bytes) if self.has_total() else tr("tasks.size.unknown")
        return f"{format_size(self._done_bytes)} / {total}"

    def speed_text(self) -> str:
        """The rate while it runs; an empty string once it stopped (a speed is not a fact then)."""
        if not self.is_running() or self._speed_bps <= 0:
            return ""
        return format_rate(self._speed_bps)

    def meta_text(self) -> str:
        """The quiet line under the bar: the two sizes, and the rate while there is one."""
        speed = self.speed_text()
        return f"{self.size_text()} · {speed}" if speed else self.size_text()

    def duration_seconds(self) -> float:
        """How long the job took, counted from the moment its task was listed.

        A running task is measured against now, a settled one against the moment it settled: the page
        prints this only for a finished job, but the number is honest in both states.
        """
        end = self._ended_at if self._ended_at is not None else time.monotonic()
        return max(0.0, end - self._started_at)

    def duration_text(self) -> str:
        """``耗时 12.3 秒`` - the one thing a finished row says that a running one cannot."""
        return tr("tasks.duration", f"{self.duration_seconds():.1f}")

    def action_text(self) -> str:
        """What the row's primary button says: stop a running job, dismiss a settled one."""
        return tr("tasks.cancel") if self.is_running() else tr("tasks.dismiss")

    def can_retry(self) -> bool:
        """Whether the row may offer 重试: a settled job whose producer registered a way to re-run it."""
        return self._retry is not None and not self.is_running()

    def retry_callback(self) -> Callable[[], None]:
        """The producer's own entry point, re-entered by :meth:`TaskCenter.retry`."""
        return self._retry  # type: ignore[return-value]  # guarded by can_retry()

    # ---------- transitions ----------

    def set_progress(self, done_bytes: int, total_bytes: int, speed_bps: float = 0.0) -> None:
        """Report how much has arrived. Ignored once the task settled."""
        if self._state != RUNNING:
            return
        self._done_bytes = max(0, int(done_bytes))
        self._total_bytes = max(0, int(total_bytes))
        self._speed_bps = max(0.0, float(speed_bps))
        self._center.notify()

    def finish(self) -> None:
        """The job completed. Also allowed from ``cancelled``: see the module docstring."""
        if self._state in (DONE, FAILED):
            return
        self._state = DONE
        self._settle()

    def fail(self, message: str = "") -> None:
        """The job failed; the message is what the row can show, and it stays until dismissed."""
        if self._state in (DONE, FAILED):
            return
        self._state = FAILED
        self._error = str(message or "")
        self._settle()

    def cancel(self) -> None:
        """Stop a running job: the state changes first, then the producer is asked to stop it."""
        if self._state != RUNNING:
            return
        self._state = CANCELLED
        self._settle()
        if self._cancel is not None:
            self._cancel(self)

    def dismiss(self) -> None:
        """Remove a settled task from the list (a running one is cancelled first, not dropped)."""
        if self.is_running():
            self.cancel()
            return
        self._center.remove(self)

    def retry(self) -> bool:
        """Run this job again: drop the settled row, then let its producer start a fresh attempt.

        Returns False when there is nothing to retry with (a running task, or a producer that did not
        register a way back). Starting the job is the producer's own entry point, so the new attempt
        becomes a new task exactly the way the first one did - no second code path into an install.
        """
        return self._center.retry(self)

    # ---------- private ----------

    def _settle(self) -> None:
        """Enter a final state: tell the views, and schedule this task's own removal when it may go."""
        if self._ended_at is None:
            self._ended_at = time.monotonic()
        self._center.notify()
        if self._state == FAILED:
            return  # a failure stays until somebody dismisses it
        hold = (
            self._center.cancelled_hold_ms
            if self._state == CANCELLED
            else self._center.completed_hold_ms
        )
        self._timer = QTimer(self._center)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._center.remove(self))
        self._timer.start(max(0, hold))

    def _stop_timer(self) -> None:
        """Stop this task's removal timer.

        The timer is not ``deleteLater()``-ed: it is a child of the center, so Qt destroys it with the
        center, and a deferred delete could outlive the center (the process then aborts in the event
        loop when it delivers a ``DeferredDelete`` to a QObject that is already gone). One stopped
        QTimer per finished task is the cheaper side of that trade.
        """
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


class TaskCenter(QObject):
    """The window's task list: one instance, one signal, and the two questions the icon asks.

    ``changed`` is emitted for every mutation - a task added, a number moved, a state settled, a task
    removed. One signal is enough: both views (the icon and the panel) are cheap to redraw from the
    list, and a second signal would only be a second thing to forget to emit.
    """

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tasks: list[Task] = []
        #: How long a finished task stays listed. A test shortens it; the product uses the default.
        self.completed_hold_ms = COMPLETED_HOLD_MS
        #: How long a cancelled task stays listed (longer: it carries where the transfer stopped).
        self.cancelled_hold_ms = CANCELLED_HOLD_MS

    # ---------- list ----------

    def tasks(self) -> tuple[Task, ...]:
        """The tasks in the order they were added (oldest first, which is how the page shows them)."""
        return tuple(self._tasks)

    def add_task(
        self,
        title_key: str,
        *title_args: object,
        cancel: Callable[[Task], None] | None = None,
        kind: str = "",
        retry: Callable[[], None] | None = None,
    ) -> Task:
        """Create and list a running task. ``cancel`` is what actually stops the underlying work.

        ``retry`` is the way back: the producer's own entry point, called when the user presses 重试 on
        a settled row. ``kind`` names which job it is (see :data:`KIND_TITLE_KEYS`); when
        it is left empty it is derived from the title key, so a caller that only knows the title still
        gets the right glyph.
        """
        task = Task(self, title_key, title_args, cancel, kind, retry)
        self._tasks.append(task)
        self.notify()
        return task

    def retry(self, task: Task) -> bool:
        """Drop a settled task and ask its producer to run the job again (see :meth:`Task.retry`)."""
        if task.is_running() or not task.can_retry():
            return False
        callback = task.retry_callback()
        self.remove(task)
        callback()
        return True

    def remove(self, task: Task) -> None:
        """Drop one task (its own five-second timer, or the row's dismiss button)."""
        if task not in self._tasks:
            return
        task._stop_timer()
        self._tasks.remove(task)
        self.notify()

    def clear(self) -> None:
        for task in list(self._tasks):
            task._stop_timer()
        self._tasks.clear()
        self.notify()

    def notify(self) -> None:
        """Announce a change to the views."""
        self.changed.emit()

    # ---------- what the icon asks ----------

    def running_count(self) -> int:
        return sum(1 for task in self._tasks if task.is_running())

    def failed_count(self) -> int:
        return sum(1 for task in self._tasks if task.state == FAILED)

    def tasks_in(self, state: str) -> tuple[Task, ...]:
        """Every task in one state, oldest first (the download page's four sections)."""
        return tuple(task for task in self._tasks if task.state == state)

    def counts(self) -> dict[str, int]:
        """How many tasks each state holds, including the zeroes (the page's summary line)."""
        counts = {RUNNING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
        for task in self._tasks:
            counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    def cancel_running(self) -> int:
        """Ask every running task to stop; return how many were asked (the page's 全部取消)."""
        running = [task for task in self._tasks if task.is_running()]
        for task in running:
            task.cancel()
        return len(running)

    def icon_state(self) -> str | None:
        """Which of the icon's three faces to show, or None when there is nothing to show at all.

        Running wins over failed on purpose: the icon is first of all a live progress indicator, and a
        failure that arrives while another download runs is still there when it finishes - at which
        point the alert appears and stays.

        ``done`` is the only face with a deadline, and it means *all* of them: a list holding a
        cancelled task is not "all done", so it shows no icon at all (the row is still on the download
        page for its ten seconds, but there is nothing interesting to report in the corner of the window).
        """
        if not self._tasks:
            return None
        if self.running_count():
            return RUNNING
        if self.failed_count():
            return FAILED
        if all(task.state == DONE for task in self._tasks):
            return DONE
        return None

    def running_task(self) -> Task | None:
        """The task the icon's ring reports: the oldest one still running."""
        return next((task for task in self._tasks if task.is_running()), None)


def task_center_of(widget) -> TaskCenter | None:
    """The task center of the window ``widget`` belongs to, or None when it has no host.

    The same walk ``gui.widgets.set_app_status`` does for the status line: a page asks the window it is
    embedded in, so a page never has to be handed a center (and a page rendered offscreen without a
    window simply has no tasks). Reads are guarded because a queued worker callback can arrive after
    the widget tree was destroyed.
    """
    node = widget
    for _ in range(64):  # a widget tree is shallow; the bound only keeps a cycle from hanging
        if node is None:
            return None
        try:
            center = getattr(node, "tasks", None)
            if isinstance(center, TaskCenter):
                return center
            node = node.parent() if hasattr(node, "parent") else None
        except RuntimeError:  # the C++ object is gone (a result that outlived its page)
            return None
    return None
