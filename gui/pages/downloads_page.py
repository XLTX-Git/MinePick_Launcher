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

"""The download page: every task in the window's list, on a page of its own.

The corner icon used to open a panel that slid in over the page the user was reading. That panel was
the right idea at the wrong size: four sections, sizes, rates, durations, a retry and a cancel-all do
not fit in 360px, and a download that takes minutes is not a notification. So the icon now opens this
page - the same list, given the whole content area - and the page is the list, not a copy of it: it
reads ``gui.tasks.TaskCenter`` on every ``changed`` signal and holds no state of its own.

Three consequences shape the code:

* **There is no empty state.** The page is only reachable through the corner icon, and that icon only
  exists while something is listed; so a page with nothing to show is not a state the user can reach.
  The one exception is the list emptying *while* the page is open - the five seconds after the last
  download finished - and that is not emptiness, it is the end: a card that says 全部完成 and the way
  back. That card is also what keeps the page from ever being a blank white rectangle.
* **The page is not a navigation destination.** It is the ninth widget in the window's stack and the
  eighth entry of the rail is still 关于: no sidebar item points here. It remembers where it was opened
  from, and 返回 goes back there - the page is a detour, not a place.
* **One row per task, painted like the panel's rows were.** A download reports several times a second,
  so rows refresh in place (they are only rebuilt when a task changes state, which is rare). The card
  behind a row is painted from the theme's tokens rather than declared in the stylesheet: this page
  owns no colours of its own and the two sheets stay the only place a colour is chosen.

Cancel is only ever reported the way it actually happened. A task's cancel callback is the producer's
own stop mechanism; when there is none, the row says 已取消 because the list stopped waiting for it -
never because a stop was confirmed. See ``gui/pages/mods_page.py`` for the four adapters and their
honest limits.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, inwindow_dialog, nav_icons, paint_utils, theme
from gui.tasks import CANCELLED, DONE, FAILED, RUNNING, Task, TaskCenter
from gui.widgets import ElidedLabel, add_page_top_inset, build_download_progress, style_page_layout

tr = i18n.tr

#: The four sections, in the order the page prints them.
SECTION_ORDER: tuple[str, ...] = (RUNNING, DONE, FAILED, CANCELLED)
#: The heading of each section, as an i18n key carrying the count.
SECTION_KEYS = {
    RUNNING: "tasks.section.running",
    DONE: "tasks.section.done",
    FAILED: "tasks.section.failed",
    CANCELLED: "tasks.section.cancelled",
}
#: The summary fragment of each state, as an i18n key carrying the count.
SUMMARY_KEYS = {
    RUNNING: "tasks.page.running",
    DONE: "tasks.page.done",
    FAILED: "tasks.page.failed",
    CANCELLED: "tasks.page.cancelled",
}
#: The Lucide glyph of each kind of job. Existing navigation icons on purpose - a new asset would have
#: to be added to the bundled set and to the third-party notices for one row's worth of pixels.
KIND_ICONS = {
    "version": "versions",
    "mod": "mods",
    "modpack": "export",
    "resourcepack": "resourcepacks",
    "shaderpack": "shaderpacks",
    "export": "export",
}
FALLBACK_ICON = "versions"

ICON_PX = 18
#: The card behind one row: the section surface the old panel's rows used, at the same 6px radius.
ROW_RADIUS = 6
ROW_PADDING = 12
ROW_GAP = 8
ROW_TITLE_PX = 13
ROW_SMALL_PX = 12
ACTION_MIN_WIDTH = 64


def _tokens() -> dict[str, str]:
    """The colour table of the theme in force (the light one before the first apply)."""
    return paint_utils._tokens()


class DownloadRow(QWidget):
    """One task as a card: its type, its title, its numbers, and the one or two things to do about it.

    ``action`` is what the job's own state offers - 取消 while it runs, 移除 once it settled - and
    ``retry`` appears next to it only for a settled job whose producer registered a way back. The bar
    is only built for a running task: a finished download has no "how far" left to show, which is why
    the page can print 耗时 there instead.
    """

    def __init__(self, task: Task, on_action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        self._on_action = on_action
        self.setObjectName("downloadRow")

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(ICON_PX, ICON_PX)
        self.icon_label.setAccessibleName("")
        #: The (glyph, colour) the label was last given, so a progress tick does not repaint the icon.
        self._icon_key: tuple[str, str] | None = None

        self.title_label = ElidedLabel(task.title(), elide_mode=Qt.TextElideMode.ElideRight)
        title_font = QFont(self.font())
        title_font.setPixelSize(ROW_TITLE_PX)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.title_label.setFont(title_font)

        # The percentage of a settled task: while it runs the number lives in the bar's own label.
        self.percent_label = QLabel("")
        percent_font = QFont(self.font())
        percent_font.setPixelSize(ROW_SMALL_PX)
        percent_font.setWeight(QFont.Weight.DemiBold)
        self.percent_label.setFont(percent_font)

        self.bar_row, self.bar, self.bar_text = build_download_progress("downloadProgress")
        self.bar.setRange(0, 1000)

        self.error_label = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideRight)
        self.meta_label = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideRight)
        for label in (self.error_label, self.meta_label):
            small_font = QFont(self.font())
            small_font.setPixelSize(ROW_SMALL_PX)
            label.setFont(small_font)

        self.action = QPushButton(task.action_text(), self)
        self.action.setObjectName("secondaryButton")
        self.action.setMinimumWidth(ACTION_MIN_WIDTH)
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(lambda _checked=False: self._on_action(self.task, "action"))
        self.retry = QPushButton(tr("tasks.retry"), self)
        self.retry.setObjectName("secondaryButton")
        self.retry.setMinimumWidth(ACTION_MIN_WIDTH)
        self.retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry.clicked.connect(lambda _checked=False: self._on_action(self.task, "retry"))

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        top = QWidget(right)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)
        top_layout.addWidget(self.title_label, 1)
        top_layout.addWidget(self.percent_label)
        top_layout.addWidget(self.retry)
        top_layout.addWidget(self.action)
        right_layout.addWidget(top)
        right_layout.addWidget(self.bar_row)
        right_layout.addWidget(self.error_label)
        right_layout.addWidget(self.meta_label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(ROW_PADDING, ROW_PADDING - 2, ROW_PADDING, ROW_PADDING - 2)
        layout.setSpacing(10)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(right, 1)

        theme.register_self_styling(self)
        self.refresh()

    # ---------- data ----------

    def icon_name(self) -> str:
        """Which bundled glyph says what kind of job this is."""
        return KIND_ICONS.get(self.task.kind, FALLBACK_ICON)

    def meta_text(self) -> str:
        """The quiet line: sizes and rate while it runs; sizes and duration once it is over."""
        task = self.task
        if task.state == DONE:
            return f"{task.size_text()} · {task.duration_text()}"
        if task.is_running():
            return task.meta_text()
        return task.size_text()

    # ---------- refresh ----------

    def refresh(self) -> None:
        """Re-read the task. Everything on this row follows from it, so one pass updates all of it."""
        task = self.task
        state = task.state
        self.title_label.setText(task.title())
        self.percent_label.setText(task.percent_text())
        self.percent_label.setVisible(not task.is_running())
        if task.is_running() and not task.has_total():
            # No announced length: a 0/0 bar would look like no progress ever. Qt's 0..0 range is
            # the busy animation, and the label is blank because there is no percentage to print.
            self.bar.setRange(0, 0)
            self.bar_text.setText("")
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(task.percent() * 10)  # same 0-1000 scale every progress bar uses
        self.bar_row.setVisible(task.is_running())
        self.error_label.setVisible(state == FAILED and bool(task.error))
        self.error_label.setText(task.error)
        self.meta_label.setText(self.meta_text())
        self.action.setText(task.action_text())
        self.retry.setVisible(task.can_retry())
        self._refresh_icon()
        self._refresh_names()
        self.updateGeometry()
        self.update()

    def _refresh_icon(self) -> None:
        """Re-render the type glyph only when it would actually change (a tick is not a colour swap)."""
        tokens = _tokens()
        colour = tokens.get("ACCENT", "#2f9e6f") if self.task.is_running() else tokens.get(
            "FG_HINT", "#4f6070"
        )
        key = (self.icon_name(), colour)
        if self._icon_key == key:
            return
        self._icon_key = key
        pixmap = nav_icons.nav_icon(key[0], colour, ICON_PX).pixmap(QSize(ICON_PX, ICON_PX))
        self.icon_label.setPixmap(pixmap)

    def _refresh_names(self) -> None:
        """Accessible names: a screen reader gets the row's whole state and each button's target."""
        task = self.task
        row_name = f"{task.title()} · {task.state_text()} · {task.percent_text()}"
        if self.accessibleName() != row_name:
            self.setAccessibleName(row_name)
        action_name = f"{task.action_text()} · {task.title()}"
        if self.action.accessibleName() != action_name:
            self.action.setAccessibleName(action_name)
        retry_name = f"{tr('tasks.retry')} · {task.title()}"
        if self.retry.accessibleName() != retry_name:
            self.retry.setAccessibleName(retry_name)
        tooltip = task.error or task.title()
        if self.toolTip() != tooltip:
            self.setToolTip(tooltip)

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(tokens.get("BORDER_TABLE", "#dfe4ea")), 1.0))
        painter.setBrush(QColor(tokens.get("BG_SECTION", "#f1f3f6")))
        painter.drawRoundedRect(card, ROW_RADIUS, ROW_RADIUS)
        painter.end()

    def on_theme_applied(self) -> None:
        self._icon_key = None  # a swapped theme may paint the glyph in another colour
        self._refresh_icon()
        self.update()


class _AllDoneCard(QFrame):
    """What the page says when the list emptied while it was open: 全部完成, and the way back."""

    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageCard")
        self.title_label = QLabel(tr("tasks.page.all_done.title"))
        self.title_label.setObjectName("sectionTitle")
        self.body_label = QLabel(tr("tasks.page.all_done.body"))
        self.body_label.setObjectName("hint")
        self.body_label.setWordWrap(True)
        self.back = QPushButton(tr("tasks.page.all_done.back"))
        self.back.setObjectName("secondaryButton")
        self.back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back.clicked.connect(self.back_requested.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(ROW_GAP)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        layout.addWidget(self.back, 0, Qt.AlignmentFlag.AlignLeft)

    def retranslate(self) -> None:
        self.title_label.setText(tr("tasks.page.all_done.title"))
        self.body_label.setText(tr("tasks.page.all_done.body"))
        self.back.setText(tr("tasks.page.all_done.back"))


class DownloadsPage(QWidget):
    """Every task in the window's list, as a page: a summary line, four sections, and the way back.

    The page owns no state: :meth:`sync` is the whole interface, and it is called on every
    ``TaskCenter.changed``. Rows are rebuilt only when a task changes *state* (which section it is in);
    a progress tick just refreshes the numbers in place, because a download reports several times a
    second and rebuilding a widget tree at that rate would be the expensive way to say the same thing.
    """

    back_requested = Signal()

    def __init__(self, center: TaskCenter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._center = center
        self._pairs: list[tuple[Task, DownloadRow]] = []
        self._signature: tuple[tuple[int, str], ...] | None = None
        self._headers: dict[str, QLabel] = {}
        self.setObjectName("downloadsPage")
        self.setAccessibleName(tr("tasks.page.a11y"))

        self.back_button = QPushButton(tr("tasks.page.back"))
        self.back_button.setObjectName("secondaryButton")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(self.back_requested.emit)

        self.title_label = QLabel(tr("tasks.page.title"))
        self.title_label.setObjectName("sectionTitle")

        self.cancel_all_button = QPushButton(tr("tasks.page.cancel_all"))
        self.cancel_all_button.setObjectName("dangerButton")
        self.cancel_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_all_button.clicked.connect(self._on_cancel_all)
        self.cancel_all_button.setVisible(False)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(ROW_GAP)
        header_layout.addWidget(self.back_button)
        header_layout.addWidget(self.title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.cancel_all_button)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("hint")
        self.summary_label.setWordWrap(True)

        self.rows_host = QWidget()
        self.rows_host.setObjectName("downloadRows")
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(ROW_GAP)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("downloadScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.rows_host)
        self.scroll.setAccessibleName(tr("tasks.page.a11y"))

        self.all_done_card = _AllDoneCard(self)
        self.all_done_card.back_requested.connect(self.back_requested.emit)
        # The card keeps its own height and sits at the top of the content area; the holder is what
        # takes the page's stretch, so the card never grows into a half-empty white rectangle.
        self.all_done_holder = QWidget(self)
        holder_layout = QVBoxLayout(self.all_done_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(0)
        holder_layout.addWidget(self.all_done_card)
        holder_layout.addStretch(1)
        self.all_done_holder.setVisible(False)

        layout = QVBoxLayout(self)
        style_page_layout(layout)
        add_page_top_inset(layout)  # same 16px first block as every other page
        layout.addWidget(header)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.all_done_holder, 1)

        theme.register_self_styling(self)
        # The page is the list's only view now, so it subscribes to it directly: one connection, one
        # signal, and no owner in between that could forget to pass the change on.
        self._center.changed.connect(self.sync)
        self.sync()

    # ---------- what the tests and the window ask ----------

    def rows(self) -> list[DownloadRow]:
        """The row widgets on screen, in the task list's own order (the tests drive these)."""
        return [row for _task, row in self._pairs]

    def row_for(self, task: Task) -> DownloadRow | None:
        """The row of one task, or None while it is not listed."""
        return next((row for listed, row in self._pairs if listed is task), None)

    def section_header(self, state: str) -> QLabel | None:
        """The heading of one section, or None while that section is empty (it is then hidden)."""
        return self._headers.get(state)

    def summary_text(self) -> str:
        """``2 个进行中 · 1 个已完成``: the counts that are not zero, in the page's own order."""
        counts = self._center.counts()
        parts = [tr(SUMMARY_KEYS[state], counts[state]) for state in SECTION_ORDER if counts[state]]
        return " · ".join(parts)

    def cancel_all_visible(self) -> bool:
        """Whether 全部取消 is on screen (only while something is still running)."""
        return self.cancel_all_button.isVisible()

    def is_all_done(self) -> bool:
        """Whether the list emptied while this page was open (the 全部完成 card is showing)."""
        return not self._center.tasks()

    def focus_back(self) -> None:
        """Put keyboard focus on the way out (the page opens with a sensible Tab origin)."""
        self.back_button.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---------- content ----------

    def sync(self) -> None:
        """Follow the center: rebuild the sections when a state changed, else refresh the numbers."""
        tasks = self._center.tasks()
        signature = tuple((id(task), task.state) for task in tasks)
        if signature != self._signature:
            self._signature = signature
            self._rebuild(tasks)
        else:
            for _task, row in self._pairs:
                row.refresh()
        self._refresh_chrome(tasks)

    def retranslate(self) -> None:
        """A language switch rebuilt the pages, not this page: re-read every string it owns."""
        self.setAccessibleName(tr("tasks.page.a11y"))
        self.back_button.setText(tr("tasks.page.back"))
        self.title_label.setText(tr("tasks.page.title"))
        self.cancel_all_button.setText(tr("tasks.page.cancel_all"))
        self.all_done_card.retranslate()
        self._signature = None  # the rows and the section headings have to be rebuilt in the new words
        self.sync()

    def _rebuild(self, tasks: tuple[Task, ...]) -> None:
        """Throw the sections away and lay them out again, one heading per non-empty state."""
        for _task, row in self._pairs:
            self.rows_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._pairs = []
        self._headers: dict[str, QLabel] = {}
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for state in SECTION_ORDER:
            group = [task for task in tasks if task.state == state]
            if not group:
                continue
            header = QLabel(tr(SECTION_KEYS[state], len(group)))
            header.setObjectName("sectionTitle")
            self.rows_layout.addWidget(header)
            self._headers[state] = header
            for task in group:
                row = DownloadRow(task, self._on_row_action)
                self.rows_layout.addWidget(row)
                self._pairs.append((task, row))
        self.rows_layout.addStretch(1)

    def _refresh_chrome(self, tasks: tuple[Task, ...]) -> None:
        """The parts outside the list: the summary, the sections' visibility and the 全部完成 card."""
        has_tasks = bool(tasks)
        counts = self._center.counts()
        self.summary_label.setText(self.summary_text())
        self.summary_label.setVisible(has_tasks)
        self.cancel_all_button.setVisible(counts[RUNNING] > 0)
        self.scroll.setVisible(has_tasks)
        self.all_done_holder.setVisible(not has_tasks)

    def _on_row_action(self, task: Task, action: str) -> None:
        """A row's button: re-run a settled job, stop a running one, or dismiss a settled one."""
        if action == "retry":
            task.retry()
            return
        if task.is_running():
            task.cancel()
            return
        task.dismiss()

    def _on_cancel_all(self) -> None:
        """全部取消, behind a question: stopping every transfer is not an undoable click."""
        running = self._center.running_count()
        if running <= 0:
            return
        outcome = inwindow_dialog.confirm(
            self,
            tr("tasks.page.cancel_all.title"),
            tr("tasks.page.cancel_all.message", running),
            accept_text=tr("tasks.page.cancel_all"),
            reject_text=tr("dialog.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="question",
        )
        if outcome is inwindow_dialog.Outcome.ACCEPTED:
            self._center.cancel_running()

    def on_theme_applied(self) -> None:
        for _task, row in self._pairs:
            row.on_theme_applied()
        self.update()
