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

"""Adapter: the versions page's frozen install flow, seen as a task in the window's task list.

The install flow is a contract (``tests/test_versions_install_flow.py`` pins the AST of the five methods
that own it), and it has to stay that way: a download that changes how it reports itself is a download
whose failure paths change too. So the page's install body keeps calling ``_on_progress(progress)``,
``_on_installed(payload)`` and ``_on_install_error(message)`` exactly as before - and this subclass sits
*behind* those three names. Each override does two things in a fixed order: the page's own behaviour
first (``super()``, unchanged), then the task. Nothing is re-implemented, and no callback is replaced by
a stand-in that pretends to be it - the flow's effect on the page is byte for byte the one it had.

Two of the three need more than "call through":

* **``_on_install_error`` and a cancelled download.** The user pressed cancel, so the flow's error path
  is reporting an ending the user asked for - a modal "install failed" dialog and a red status line
  would both be wrong. The adapter checks the task first: a cancelled task takes the quiet ending (the
  row gets its install button back, the status line says 已取消), and every other error takes the
  page's own path unchanged.
* **The end of a download.** The page's thin bottom bar used to signal it, by being hidden. That bar is
  gone (the page refuses it, see ``versions_page.py``), so the adapter clears the row's inline progress
  itself, at exactly the point where the flow has finished with the download.

Cancelling is real: the task's cancel button calls the downloader's cooperative cancellation
(``launcher.net.downloader.cancel_current_downloads``), which stops the transfer at its next 64 KB chunk
and keeps the ``.part`` files for the next attempt. What the page cannot do is kill the worker thread -
nothing can - so the row and the task are updated immediately and the flow is allowed to unwind at its
own pace; when its error callback arrives, the adapter recognises the cancellation and does not turn it
into a failure.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer

from gui import i18n
from gui.pages.versions_page import VersionsPage
from gui.tasks import CANCELLED, Task, task_center_of
from gui.widgets import set_app_status
from launcher.net import downloader

tr = i18n.tr

#: How long a successful install waits before the install step gives way to the version list. Long
#: enough to see the row turn into "installed", short enough not to feel stuck.
AUTO_RETURN_MS = 2500


class TaskAwareVersionsPage(VersionsPage):
    """``VersionsPage`` plus the task list - the only difference is the adapter below."""

    def __init__(self) -> None:
        super().__init__()
        #: The task the running install reports into (None between installs).
        self._task: Task | None = None
        #: True once this page is on its way out, so the cancel path must not touch its widgets.
        self._closing = False
        self._auto_return = QTimer(self)
        self._auto_return.setSingleShot(True)
        self._auto_return.timeout.connect(self._return_from_install_step)

    # ---------- the install entry ----------

    def install_version_id(self, version_id: str) -> None:
        """Attach a task to this install, then run the frozen flow unchanged.

        The task exists before the first byte is asked for, so a download that fails immediately still
        gets a row (and the failure still gets a place to be read). The flow itself is entered and left
        exactly as it was: ``super()`` is the whole install body.
        """
        previous = self._task
        self._task = None
        if previous is not None and previous.is_running():
            # Only reachable through install_selected(): the page's own buttons are disabled while a
            # download runs. A task left "running" would spin the icon for the rest of the session.
            previous.fail(tr("tasks.error.replaced"))
        center = task_center_of(self)
        if center is not None:
            self._task = center.add_task(
                "tasks.title.install",
                version_id,
                cancel=self._cancel_download,
            )
        self._auto_return.stop()
        # A cancel belongs to the install that was stopped, never to the next one.
        downloader.clear_cancellation()
        super().install_version_id(version_id)

    # ---------- the three frozen callbacks, called through ----------

    def _on_progress(self, p) -> None:
        """The page's own progress first (bar, rate, status line), then the task's numbers."""
        if self._closing:
            return
        super()._on_progress(p)
        task = self._task
        if task is not None and task.is_running():
            # The rate comes from the tracker the unchanged callback just updated, so the panel's
            # "MB/s" is the same number the status line prints.
            task.set_progress(p.done_bytes, p.total_bytes, self._rate.rate_bps)

    def _on_installed(self, payload) -> None:
        """A finished install: the page's own ending first, then the task's."""
        if self._closing:
            return  # the page is on its way out (a language switch): no widgets to update
        super()._on_installed(payload)
        downloader.clear_cancellation()
        # The bar's hide used to end the row's inline progress; the bar is refused now, so the adapter
        # is what hands the row its install button back (after ``_refilter`` rebuilt the rows).
        self._finish_inline_progress()
        task, self._task = self._task, None
        if task is not None:
            result = payload[0] if payload else None
            failed = list(getattr(result, "failed", []) or [])
            if failed:
                # The version is installed, but part of it is not: that is a failure the user has to
                # see, not a green "done".
                task.fail(tr("tasks.error.partial", len(failed)))
            else:
                task.finish()
        self._schedule_auto_return()

    def _on_install_error(self, message: str) -> None:
        """A failed - or cancelled - install."""
        task, self._task = self._task, None
        if self._closing:
            # The page is being dropped (a language switch): its status line is gone and a modal
            # dialog would be opened on a dying window. The task itself was already cancelled by
            # ``cancel_running_task``, so there is nothing left to report.
            return
        if task is not None and task.state == CANCELLED:
            # The user stopped this download. The flow's error path would call it a failure (a red
            # status line and a modal dialog); the adapter ends it the way it actually ended.
            downloader.clear_cancellation()
            self._finish_inline_progress()
            set_app_status(self, tr("tasks.status.cancelled", task.title()))
            return
        super()._on_install_error(message)
        if task is not None:
            task.fail(message)

    # ---------- cancel ----------

    def _cancel_download(self, task: Task) -> None:
        """The task's cancel button: ask the transfer to stop, and take the row back at once.

        The stop is the downloader's cooperative one - a worker notices at its next chunk boundary, so
        this call never blocks. The display does not wait for it: the row's inline progress returns to
        the install button immediately, because leaving a moving bar under a task that says 已取消 would
        be the interface disagreeing with itself.
        """
        downloader.cancel_current_downloads()
        if self._closing:
            return  # the page is being destroyed: its widgets are not ours to touch any more
        self._finish_inline_progress()
        set_app_status(self, tr("tasks.status.cancelled", task.title()))

    def cancel_running_task(self) -> None:
        """The window is dropping this page (a language switch rebuilds every page): stop its install.

        The worker's callbacks are delivered through ``run_in_background``, which drops a callback whose
        page is gone - so the task would never hear about its download again and the icon would spin for
        the rest of the session. Cancelling it is the honest ending: nothing failed, the page went away,
        and the transfer stops where it is (the ``.part`` files stay for the next attempt).
        """
        self._closing = True
        task, self._task = self._task, None
        if task is not None and task.is_running():
            task.cancel()

    # ---------- the way back to the list ----------

    def _schedule_auto_return(self) -> None:
        """After a successful install, offer the way back to the version list."""
        self._auto_return.start(AUTO_RETURN_MS)

    def _return_from_install_step(self) -> None:
        """Leave the install step - but only if that step is what the user is looking at right now.

        "The user moved to another page" is checked twice, and both checks only *read*: the page must be
        the visible one, and its own stack must still be showing the step. Nothing here touches the
        window's navigation, so a download that finishes while the user reads the settings page cannot
        pull them back.
        """
        if not self.isVisible():
            return
        if self.stack.currentWidget() is not self.step:
            return
        self._leave_step()
