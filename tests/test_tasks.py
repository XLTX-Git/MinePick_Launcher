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

"""The task model: the state machine, the numbers the views draw, and who leaves when.

Everything here is model behaviour, so most of it needs no window: a task is data plus a state machine,
and the two timers (the five-second removal of a finished task, and the fact that a failure has none)
are driven through the suite's QApplication so they are the real timers, not a test double.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from gui import i18n
from gui.tasks import (
    CANCELLED,
    COMPLETED_HOLD_MS,
    DONE,
    FAILED,
    RUNNING,
    TaskCenter,
    format_size,
    task_center_of,
)


@pytest.fixture(scope="module")
def app():
    """The suite's QApplication (the timers need one; the model itself does not)."""
    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    yield QApplication([])


def _pump(app, predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


@pytest.fixture()
def center(app):
    center = TaskCenter()
    yield center
    center.clear()


# ---------- the state machine ----------


def test_a_new_task_is_running_and_has_no_numbers_yet(center) -> None:
    task = center.add_task("tasks.title.install", "1.21.11")
    assert center.tasks() == (task,)
    assert task.state == RUNNING
    assert task.is_running() and not task.is_finished()
    assert task.percent() == 0
    assert task.size_text() == "0 B / 0 B"
    assert task.speed_text() == ""
    assert task.title() == i18n.tr("tasks.title.install", "1.21.11")
    assert task.state_text() == i18n.tr("tasks.state.running")
    assert task.action_text() == i18n.tr("tasks.cancel")


def test_progress_is_the_downloaded_share_of_the_total(center) -> None:
    task = center.add_task("tasks.title.install", "1.21.11")
    task.set_progress(3 * 1024 * 1024, 12 * 1024 * 1024, 1.2 * 1024 * 1024)
    assert task.percent() == 25
    assert task.percent_text() == "25%"
    assert task.size_text() == "3.0 MB / 12.0 MB"
    assert task.speed_text() == "1.2 MB/s"
    assert task.meta_text() == "3.0 MB / 12.0 MB · 1.2 MB/s"

    task.set_progress(12 * 1024 * 1024, 12 * 1024 * 1024, 0)
    assert task.percent() == 100
    assert task.speed_text() == "", "a speed is not a fact once the transfer stopped"


@pytest.mark.parametrize(
    ("done", "total", "percent"),
    [(0, 100, 0), (1, 3, 33), (2, 3, 67), (999, 1000, 100), (0, 0, 0), (50, 10, 100)],
)
def test_the_percentage_survives_odd_numbers(center, done, total, percent) -> None:
    task = center.add_task("tasks.title.install", "x")
    task.set_progress(done, total, 0)
    assert task.percent() == percent


def test_progress_after_the_end_is_ignored(center) -> None:
    task = center.add_task("tasks.title.install", "1.21.11")
    task.set_progress(1, 10)
    task.finish()
    task.set_progress(9, 10)
    assert task.percent() == 10, "a late progress callback must not move a settled task"


def test_the_endings_are_final(center) -> None:
    done = center.add_task("tasks.title.install", "a")
    done.finish()
    done.fail("late")
    done.cancel()
    assert done.state == DONE and done.error == ""

    failed = center.add_task("tasks.title.install", "b")
    failed.fail("boom")
    failed.finish()
    assert failed.state == FAILED and failed.error == "boom"

    cancelled = center.add_task("tasks.title.install", "c")
    cancelled.cancel()
    cancelled.cancel()  # a second click is not a second stop request
    assert cancelled.state == CANCELLED


def test_a_cancelled_task_that_really_finished_says_done(center) -> None:
    """The cancel asked for a stop; the bytes landed anyway. Reporting "cancelled" would be a lie."""
    task = center.add_task("tasks.title.install", "1.21.11")
    task.cancel()
    task.finish()
    assert task.state == DONE


def test_cancel_asks_the_producer_once(center) -> None:
    stopped: list[str] = []
    task = center.add_task("tasks.title.install", "1.21.11", cancel=lambda t: stopped.append(t.title()))
    task.cancel()
    task.cancel()
    assert stopped == [i18n.tr("tasks.title.install", "1.21.11")]


# ---------- what the icon asks ----------


def test_the_icon_has_no_face_without_tasks(center) -> None:
    assert center.icon_state() is None
    assert center.running_task() is None


def test_the_icon_follows_the_list(center) -> None:
    first = center.add_task("tasks.title.install", "a")
    assert center.icon_state() == RUNNING
    assert center.running_task() is first

    second = center.add_task("tasks.title.install", "b")
    assert center.icon_state() == RUNNING

    second.fail("boom")
    assert center.icon_state() == RUNNING, "a running download keeps the ring: the alert waits"

    first.finish()
    assert center.icon_state() == FAILED, "with nothing running, the failure is what the icon says"

    center.remove(second)
    assert center.icon_state() == DONE, "the finished task is still listed, so the check is showing"

    center.remove(first)
    assert center.icon_state() is None


def test_the_icon_says_done_only_when_every_task_is_done(center) -> None:
    task = center.add_task("tasks.title.install", "a")
    task.finish()
    assert center.icon_state() == DONE
    center.remove(task)
    assert center.icon_state() is None

    # A cancelled task is not "all done": the corner has nothing to report, even though the row stays
    # in the panel for its five seconds.
    cancelled = center.add_task("tasks.title.install", "b")
    cancelled.cancel()
    assert center.icon_state() is None


# ---------- who leaves, and when ----------


def test_a_finished_task_removes_itself_after_five_seconds(app) -> None:
    """The product's own hold, not a shortened one: 5s is what the user is promised."""
    assert COMPLETED_HOLD_MS == 5000
    center = TaskCenter()
    try:
        task = center.add_task("tasks.title.install", "a")
        task.finish()
        assert _pump(app, lambda: task not in center.tasks(), timeout=2.0) is False, (
            "the task left the list before its five seconds were up"
        )
        assert _pump(app, lambda: not center.tasks(), timeout=6.0), (
            "a finished task never removed itself"
        )
    finally:
        center.clear()


def test_a_failed_task_stays_until_it_is_dismissed(app) -> None:
    center = TaskCenter()
    center.completed_hold_ms = 50  # not taken: a failure is not on a timer at all
    try:
        task = center.add_task("tasks.title.install", "a")
        task.fail("boom")
        assert _pump(app, lambda: bool(center.tasks()), timeout=0.4) is True
        assert center.tasks() == (task,), "a failure must not disappear on its own"
        task.dismiss()
        assert center.tasks() == ()
    finally:
        center.clear()


def test_a_cancelled_task_leaves_like_a_finished_one(app) -> None:
    center = TaskCenter()
    center.completed_hold_ms = 30
    try:
        task = center.add_task("tasks.title.install", "a")
        task.cancel()
        assert _pump(app, lambda: not center.tasks(), timeout=3.0)
    finally:
        center.clear()


def test_dismissing_a_running_task_cancels_it_instead(center) -> None:
    stopped: list[bool] = []
    task = center.add_task("tasks.title.install", "a", cancel=lambda _t: stopped.append(True))
    task.dismiss()  # the row's button says 取消 while it runs; dismiss must not drop it silently
    assert task.state == CANCELLED and stopped == [True]
    assert center.tasks() == (task,)


# ---------- text ----------


def test_the_title_is_translated_when_it_is_read(center) -> None:
    task = center.add_task("tasks.title.install", "1.21.11")
    assert task.title() == "安装 1.21.11"
    i18n.set_language("en_us")
    try:
        assert task.title() == "Install 1.21.11", "a language switch renames a task still listed"
    finally:
        i18n.set_language("zh_cn")


def test_sizes_are_human_readable() -> None:
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(2048) == "2 KB"
    assert format_size(3.4 * 1024 * 1024) == "3.4 MB"


# ---------- who owns the list ----------


def test_a_page_finds_the_center_through_its_host() -> None:
    host = QWidget()
    host.tasks = TaskCenter(host)  # the window's contract: the center hangs off the window
    page = QWidget(host)
    assert task_center_of(page) is host.tasks
    assert task_center_of(QWidget()) is None, "a widget without a host has no tasks"
