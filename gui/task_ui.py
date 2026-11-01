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

"""The window's task views: a floating 48px button and the panel it slides open.

The two views are one idea seen twice. The button answers "is anything running, and how far" - a ring
with the percentage in the middle while a download runs, a check while everything just finished, an
alert while a failure is waiting to be read. The panel is the list itself: one card per task with its
title, state, percentage, bytes, rate and the one button that stops it (or dismisses it once it is
over). Neither view owns state: both read the same :class:`gui.tasks.TaskCenter`, so a task's percentage
in the panel and the arc on the button cannot disagree.

The button does not exist when there is nothing to show, and that is a structural rule, not
``hide()``: it is created when the first task arrives and destroyed when the last one leaves, so there
is no invisible widget left in the window, no focus stop nobody can reach, and nothing for the
walkthrough or a screenshot to trip over. Only the panel outlives that moment, and only when it is
already open: it then says 暂无任务 until it is closed (that is how the empty state is reachable at all -
the list emptied while the panel was showing it).

Everything here paints itself. That is the same choice the version rows and the loader rows make: the
title and the meta line have to elide on one line, a list must not pay for a widget tree per row, and
the colours come from the theme's token table, which is the one source of truth for both sheets.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, motion, paint_utils, theme
from gui.tasks import DONE, FAILED, RUNNING, Task, TaskCenter

tr = i18n.tr

#: Distance from the window content's right/bottom edges to the visible circle.
EDGE = 16
#: The circle the user sees. The widget is larger by the focus band on every side, so the halo can be
#: painted without shaving the circle.
ICON_SIZE = 48
FOCUS_BAND = 4
ICON_WIDGET = ICON_SIZE + FOCUS_BAND * 2
#: Air between the button and the panel above it.
GAP = 8

PANEL_WIDTH = 360
PANEL_MAX_HEIGHT = 420
PANEL_MIN_HEIGHT = 120
PANEL_RADIUS = 8
PANEL_PADDING = 12
PANEL_HEADER = 28
PANEL_ANIMATION_MS = 180

ROW_HEIGHT = 96
ROW_RADIUS = 6
ROW_PADDING = 12
ACTION_HEIGHT = 32
ACTION_MIN_WIDTH = 64


def _tokens() -> dict[str, str]:
    """The colour table of the theme in force (the light one before the first apply)."""
    return paint_utils._tokens()


def _state_color(tokens: dict[str, str], state: str) -> str:
    """The one accent a task's state text may carry: success, failure, or the quiet default."""
    if state == DONE:
        return tokens.get("ACCENT_TEXT", tokens.get("FG_HINT", "#4f6070"))
    if state == FAILED:
        return tokens.get("DANGER_FG", tokens.get("FG_STATUS_WARNING", "#b03a2e"))
    return tokens.get("FG_HINT", "#4f6070")


class TaskButton(QAbstractButton):
    """The floating task button: one circle, three faces, painted rather than iconographic.

    A ``QAbstractButton`` so it is a Tab stop, Space/Enter press it, the screen reader gets a name that
    says what the ring means, and the focus cue can be painted (the ``keyboardFocus`` property the rest
    of the window uses keeps it keyboard-only).

    Three faces, all on the same accent fill:
    ``running`` paints a ring arc plus the percentage, ``done`` a check, ``failed`` an alert. The glyphs
    are drawn, never typed - the UI font has no guaranteed U+2713/U+26A0, and a missing glyph would be a
    tofu box where the state should be.
    """

    def __init__(self, center: TaskCenter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._center = center
        self.setObjectName("taskButton")
        self.setFixedSize(ICON_WIDGET, ICON_WIDGET)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        center.changed.connect(self.refresh)
        theme.register_self_styling(self)
        self.refresh()

    # ---------- data ----------

    def state(self) -> str:
        """Which face is on screen right now (the center's answer, never a copy of it)."""
        return self._center.icon_state() or DONE

    def percent(self) -> int:
        """The number the ring reports: the running task's downloaded share."""
        task = self._center.running_task()
        return task.percent() if task is not None else 0

    def circle_rect(self) -> QRectF:
        """The visible circle in widget coordinates (what ``EDGE`` measures to)."""
        return QRectF(FOCUS_BAND, FOCUS_BAND, ICON_SIZE, ICON_SIZE)

    def refresh(self) -> None:
        state = self.state()
        if state == RUNNING:
            name = tr("tasks.icon.progress", self.percent())
        elif state == FAILED:
            name = tr("tasks.icon.failed")
        else:
            name = tr("tasks.icon.done")
        if self.accessibleName() != name:
            self.setAccessibleName(name)
        if self.toolTip() != name:
            self.setToolTip(name)
        self.update()

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        circle = self.circle_rect()
        fill = QColor(tokens.get("ACCENT_BUTTON", "#2f9e6f"))
        foreground = QColor(tokens.get("FG_SELECTED", "#ffffff"))

        if self.hasFocus() and bool(self.property("keyboardFocus")):
            halo = QPen(QColor(tokens.get("FOCUS_RING", fill.name())))
            halo.setWidthF(2.0)
            painter.setPen(halo)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(circle.adjusted(-2.5, -2.5, 2.5, 2.5))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(circle)

        state = self.state()
        if state == RUNNING:
            self._paint_ring(painter, circle, foreground)
        elif state == DONE:
            self._paint_check(painter, circle, foreground)
        else:
            self._paint_alert(painter, circle, foreground)
        painter.end()

    def _paint_ring(self, painter: QPainter, circle: QRectF, foreground: QColor) -> None:
        """A full track plus the downloaded part of it, with the percentage inside."""
        percent = self.percent()
        ring = circle.adjusted(4.5, 4.5, -4.5, -4.5)
        track = QColor(foreground)
        track.setAlpha(70)
        track_pen = QPen(track)
        track_pen.setWidthF(3.0)
        painter.setPen(track_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(ring)
        if percent > 0:
            arc = QPen(foreground)
            arc.setWidthF(3.0)
            arc.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(arc)
            # 90*16 is twelve o'clock; the sweep is negative so the arc fills clockwise.
            painter.drawArc(ring, 90 * 16, -round(percent / 100.0 * 360 * 16))
        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(foreground)
        painter.drawText(circle, int(Qt.AlignmentFlag.AlignCenter), str(percent))

    def _paint_check(self, painter: QPainter, circle: QRectF, foreground: QColor) -> None:
        pen = QPen(foreground)
        pen.setWidthF(2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center = circle.center()
        size = circle.width()
        painter.drawPolyline(
            QPolygonF(
                [
                    center + QPointF(-size * 0.20, size * 0.00),
                    center + QPointF(-size * 0.05, size * 0.16),
                    center + QPointF(size * 0.21, -size * 0.16),
                ]
            )
        )

    def _paint_alert(self, painter: QPainter, circle: QRectF, foreground: QColor) -> None:
        ring = QPen(foreground)
        ring.setWidthF(2.2)
        painter.setPen(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(circle.adjusted(11.0, 11.0, -11.0, -11.0))
        center = circle.center()
        bar = QPen(foreground)
        bar.setWidthF(2.4)
        bar.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(bar)
        painter.drawLine(center + QPoint(0, -6), center + QPoint(0, 1))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(foreground)
        painter.drawEllipse(center + QPoint(0, 6), 1.6, 1.6)

    # ---------- events ----------

    def on_theme_applied(self) -> None:
        """The colours are read at paint time; a swap only has to schedule the repaint."""
        self.refresh()


class _CloseButton(QAbstractButton):
    """The panel's close control: a drawn cross, 28px, with a real accessible name."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("taskPanelClose")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr("tasks.close"))
        self.setToolTip(tr("tasks.close"))

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("BG_HOVER", "#e6eaf0")))
            painter.drawRoundedRect(QRectF(0, 0, 28, 28), 6, 6)
        pen = QPen(QColor(tokens.get("FG_HINT", "#4f6070")))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        center = self.rect().center()
        arm = 5
        painter.drawLine(center.x() - arm, center.y() - arm, center.x() + arm, center.y() + arm)
        painter.drawLine(center.x() - arm, center.y() + arm, center.x() + arm, center.y() - arm)
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            ring = QPen(QColor(tokens.get("FOCUS_RING", "#2f9e6f")))
            ring.setWidthF(1.6)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(1, 1, 26, 26), 6, 6)
        painter.end()

    def on_theme_applied(self) -> None:
        self.update()


class TaskRow(QWidget):
    """One task, drawn as a card: title, state, percentage, bar, sizes and rate, one action.

    Painted for the reason the version rows are painted: the title and the meta line have to elide on a
    single line, and the list is redrawn on every progress tick - a widget tree per row would pay for
    that. The one real child is the action button, because it has to be a focus stop with a name
    (``取消 · 安装 1.21.11``) and the stylesheet's own secondary-button look.
    """

    TITLE_PX = 14
    SMALL_PX = 12

    def __init__(self, task: Task, on_action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        self._on_action = on_action
        self.setObjectName("taskRow")
        self.setFixedHeight(ROW_HEIGHT)
        self.setAccessibleName(task.title())
        self.action = QPushButton(task.action_text(), self)
        self.action.setObjectName("secondaryButton")
        self.action.setFixedHeight(ACTION_HEIGHT)
        self.action.setMinimumWidth(ACTION_MIN_WIDTH)
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(lambda _checked=False: self._on_action(self.task))
        theme.register_self_styling(self)
        self.refresh()

    # ---------- fonts ----------

    def title_font(self) -> QFont:
        """The font the title is painted with (the row paints its own text, so this is the truth)."""
        font = QFont(self.font())
        font.setPixelSize(self.TITLE_PX)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def small_font(self) -> QFont:
        """The font of the state, the percentage, the sizes and the rate."""
        font = QFont(self.font())
        font.setPixelSize(self.SMALL_PX)
        font.setWeight(QFont.Weight.Normal)
        return font

    def refresh(self) -> None:
        """Re-read the task: the action's label and both accessible names follow the state."""
        text = self.task.action_text()
        if self.action.text() != text:
            self.action.setText(text)
            self.action.adjustSize()  # a wider label must not be clipped by the 取消 one's width
        name = f"{text} · {self.task.title()}"
        if self.action.accessibleName() != name:
            self.action.setAccessibleName(name)
        row_name = f"{self.task.title()} · {self.task.state_text()} · {self.task.percent_text()}"
        if self.accessibleName() != row_name:
            self.setAccessibleName(row_name)
        tooltip = self.task.error or self.task.title()
        if self.toolTip() != tooltip:
            self.setToolTip(tooltip)
        self.update()

    def resizeEvent(self, event) -> None:
        size = self.action.sizeHint()
        width = max(ACTION_MIN_WIDTH, size.width())
        self.action.resize(width, ACTION_HEIGHT)
        # Vertically centred against the whole card, so every text line can run to the left of it.
        self.action.move(
            self.width() - ROW_PADDING - width, (ROW_HEIGHT - ACTION_HEIGHT) // 2
        )
        super().resizeEvent(event)

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        state = self.task.state
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, ROW_HEIGHT - 1.0)
        painter.setPen(QPen(QColor(tokens.get("BORDER_TABLE", "#dfe4ea")), 1.0))
        painter.setBrush(QColor(tokens.get("BG_SECTION", "#f1f3f6")))
        painter.drawRoundedRect(card, ROW_RADIUS, ROW_RADIUS)

        content_left = ROW_PADDING
        # The text column stops where the action button starts: the percentage sits on the same line
        # as the state, and painting it under a real widget would hide half of it.
        content_width = max(0, self.width() - ROW_PADDING * 2 - self.action.width() - 12)
        title_width = content_width

        title_font = self.title_font()
        title_metrics = QFontMetrics(title_font)
        painter.setFont(title_font)
        painter.setPen(QColor(tokens.get("FG_TEXT", "#1f2a36")))
        painter.drawText(
            QRectF(content_left, 10, title_width, 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title_metrics.elidedText(self.task.title(), Qt.TextElideMode.ElideRight, int(title_width)),
        )

        small_font = self.small_font()
        small_metrics = QFontMetrics(small_font)
        painter.setFont(small_font)
        state_width = min(
            content_width,
            small_metrics.horizontalAdvance(self.task.state_text()) + 2,
        )
        painter.setPen(QColor(_state_color(tokens, state)))
        painter.drawText(
            QRectF(content_left, 34, state_width, 16),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.task.state_text(),
        )
        percent_font = QFont(small_font)
        percent_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(percent_font)
        painter.setPen(QColor(tokens.get("FG_TEXT", "#1f2a36")))
        painter.drawText(
            QRectF(content_left, 34, content_width, 16),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            self.task.percent_text(),
        )

        self._paint_bar(painter, tokens, state, content_left, content_width)

        painter.setFont(small_font)
        painter.setPen(QColor(tokens.get("FG_HINT", "#4f6070")))
        meta = self.task.error if state == FAILED and self.task.error else self.task.meta_text()
        painter.drawText(
            QRectF(content_left, 66, content_width, 16),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            small_metrics.elidedText(meta, Qt.TextElideMode.ElideRight, int(content_width)),
        )
        painter.end()

    def _paint_bar(
        self,
        painter: QPainter,
        tokens: dict[str, str],
        state: str,
        left: int,
        width: int,
    ) -> None:
        bar = QRectF(left, 56, width, 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.get("BG_LIST", "#ffffff")))
        painter.drawRoundedRect(bar, 3, 3)
        if state == FAILED:
            fill = tokens.get("DANGER_FG", "#b03a2e")
        elif state in (DONE, RUNNING):
            fill = tokens.get("ACCENT", "#2f9e6f")
        else:  # cancelled: the part that did arrive, in a neutral tone
            fill = tokens.get("FG_DISABLED", "#9aa5b1")
        filled = bar.adjusted(0, 0, -width * (1.0 - self.task.percent() / 100.0), 0)
        if filled.width() > 0:
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(filled, 3, 3)

    def on_theme_applied(self) -> None:
        self.update()


class TaskPanel(QWidget):
    """The task list as a card that slides in from the window's right edge.

    The panel is a widget, not a window: it is a child of the window's content area, slides inside it,
    and can never end up on the desktop. Its rows refresh in place on every ``changed`` signal instead
    of being rebuilt, because a download reports progress several times a second.
    """

    close_requested = Signal()

    def __init__(self, center: TaskCenter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._center = center
        self._open = False
        self._pairs: list[tuple[Task, TaskRow]] = []
        self.setObjectName("taskPanel")
        self.setAccessibleName(tr("tasks.title"))
        self.setFixedWidth(PANEL_WIDTH)

        self.title_label = QLabel(tr("tasks.title"))
        self.title_label.setObjectName("sectionTitle")
        self.close_button = _CloseButton(self)
        self.close_button.clicked.connect(self.close_requested.emit)

        header = QWidget(self)
        header.setFixedHeight(PANEL_HEADER)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        header_layout.addWidget(self.title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.close_button)

        self.rows_host = QWidget()
        self.rows_host.setObjectName("taskRows")
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        self.rows_layout.addStretch(1)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("taskScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.rows_host)

        self.empty_label = QLabel(tr("tasks.empty"))
        self.empty_label.setObjectName("emptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(PANEL_PADDING, PANEL_PADDING, PANEL_PADDING, PANEL_PADDING)
        layout.setSpacing(8)
        layout.addWidget(header)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.empty_label, 1)

        # The overlay drives sync() - it owns both views and runs one pass per change. Connecting the
        # panel here as well would refresh every row twice for every progress tick.
        theme.register_self_styling(self)
        self.sync()

    # ---------- state ----------

    def is_open(self) -> bool:
        return self._open

    def set_open(self, is_open: bool) -> None:
        self._open = is_open

    def rows(self) -> list[TaskRow]:
        """The visible row widgets, in the task list's own order (what the tests drive)."""
        return [row for _task, row in self._pairs]

    def is_empty(self) -> bool:
        return not self._center.tasks()

    # ---------- content ----------

    def sync(self) -> None:
        """Follow the center: rows added/removed, existing rows refreshed in place."""
        tasks = self._center.tasks()
        existing = {task: row for task, row in self._pairs}
        kept: list[tuple[Task, TaskRow]] = []
        for task in tasks:
            row = existing.get(task)
            if row is None:
                row = TaskRow(task, self._run_action)
                self.rows_layout.insertWidget(len(kept), row)
            else:
                row.refresh()
            kept.append((task, row))
        for task, row in self._pairs:
            if task not in tasks:
                self.rows_layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
        self._pairs = kept
        empty = not tasks
        self.empty_label.setVisible(empty)
        self.scroll.setVisible(not empty)
        if empty:
            self.scroll.hide()
        self.update()

    def retranslate(self) -> None:
        """A language switch rebuilt the pages, not this panel: re-read every string it owns."""
        self.setAccessibleName(tr("tasks.title"))
        self.title_label.setText(tr("tasks.title"))
        self.empty_label.setText(tr("tasks.empty"))
        self.close_button.setAccessibleName(tr("tasks.close"))
        self.close_button.setToolTip(tr("tasks.close"))
        for _task, row in self._pairs:
            row.refresh()

    def _run_action(self, task: Task) -> None:
        """The row's one button: stop a running job, dismiss a settled one."""
        if task.is_running():
            task.cancel()
        else:
            task.dismiss()

    # ---------- events ----------

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.close_requested.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(tokens.get("BORDER_TABLE", "#dfe4ea")), 1.0))
        painter.setBrush(QColor(tokens.get("BG_LIST", "#ffffff")))
        painter.drawRoundedRect(card, PANEL_RADIUS, PANEL_RADIUS)
        painter.end()

    def on_theme_applied(self) -> None:
        self.update()
        for _task, row in self._pairs:
            row.update()


class TaskOverlay(QObject):
    """Keeps the window's task views alive, positioned and in step with the task center.

    The button's lifetime is the list's: it is created with the first task and destroyed with the last,
    so "there is no icon" is a property of the widget tree rather than of a visibility flag. The panel is
    created the first time it is opened and kept afterwards; it is positioned above the button, so the
    two never cover each other, and it is a child of the content area rather than a window.
    """

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._center: TaskCenter = window.tasks
        self._button: TaskButton | None = None
        self._panel: TaskPanel | None = None
        self._closing = False
        # One animation object for the life of the window, retargeted per open/close. Qt deletes an
        # animation that was started with DeleteWhenStopped, and a Python reference to it then points
        # at a dead C++ object - which is exactly the crash the second open would hit.
        self._animation = QPropertyAnimation(self)
        self._animation.setDuration(PANEL_ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._on_animation_finished)
        self._center.changed.connect(self._sync)
        self._sync()

    # ---------- accessors ----------

    def button(self) -> TaskButton | None:
        return self._button

    def panel(self) -> TaskPanel | None:
        """The panel, if it has ever been opened (it is created on demand, never pre-built)."""
        return self._panel

    def _host(self) -> QWidget:
        content = getattr(self._window, "content", None)
        return content if isinstance(content, QWidget) else self._window.centralWidget()

    # ---------- lifetime ----------

    def _sync(self) -> None:
        """Follow the center: grow or drop the icon, refresh the panel when it exists."""
        if self._center.icon_state() is None:
            if self._button is not None:
                # Hidden first and deleted on the next event-loop turn: this runs inside the button's
                # own click path (or inside a task's signal), where destroying the sender is not legal.
                # The widget is out of the tree - not merely invisible - when the turn ends.
                self._button.hide()
                self._button.deleteLater()
                self._button = None
        elif self._button is None:
            self._button = TaskButton(self._center, self._host())
            self._button.clicked.connect(self.toggle)
            self._button.show()
            self._button.raise_()
            self._place_button()
        else:
            self._place_button()
        if self._panel is not None:
            self._panel.sync()
            self._panel.raise_()
            if self._panel.is_open() and self._button is None:
                self._place_panel()

    def retranslate(self) -> None:
        """The pages were rebuilt in another language; the task views are the window's own."""
        if self._panel is not None:
            self._panel.retranslate()
        if self._button is not None:
            self._button.refresh()

    # ---------- geometry ----------

    def _button_target(self) -> QPoint:
        host = self._host()
        return QPoint(
            max(0, host.width() - EDGE - ICON_SIZE - FOCUS_BAND),
            max(0, host.height() - EDGE - ICON_SIZE - FOCUS_BAND),
        )

    def _panel_geometry(self) -> tuple[int, int, int, int]:
        host = self._host()
        width = max(160, min(PANEL_WIDTH, host.width() - EDGE * 2))
        x = host.width() - EDGE - width
        bottom = self._button_target().y() - GAP
        height = max(PANEL_MIN_HEIGHT, min(PANEL_MAX_HEIGHT, bottom - EDGE))
        y = max(EDGE, bottom - height)
        return x, y, width, height

    def _place_button(self) -> None:
        if self._button is None:
            return
        target = self._button_target()
        if self._button.pos() != target:
            self._button.move(target)

    def _place_panel(self) -> None:
        if self._panel is None:
            return
        x, y, width, height = self._panel_geometry()
        self._panel.setFixedWidth(width)
        self._panel.resize(width, height)
        self._panel.move(x, y)

    def reposition(self) -> None:
        """The window changed size: move the button, and the panel with it when it is open."""
        self._place_button()
        if self._panel is not None and self._panel.is_open():
            self._place_panel()

    # ---------- open / close ----------

    def toggle(self) -> None:
        if self._panel is not None and self._panel.is_open():
            self.close_panel()
        else:
            self.open_panel()

    def open_panel(self) -> None:
        host = self._host()
        if self._panel is None:
            self._panel = TaskPanel(self._center, host)
            self._panel.close_requested.connect(self.close_panel)
            self._panel.sync()
        panel = self._panel
        panel.set_open(True)
        x, y, width, height = self._panel_geometry()
        panel.setFixedWidth(width)
        panel.resize(width, height)
        panel.move(x, y)
        panel.show()
        panel.raise_()
        self._animate(panel, QPoint(x, y), closing=False)
        panel.close_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_panel(self) -> None:
        panel = self._panel
        if panel is None or not panel.is_open():
            return
        panel.set_open(False)
        x, _y, _width, _height = self._panel_geometry()
        host = self._host()
        self._animate(panel, QPoint(max(x, host.width()), panel.y()), closing=True)
        if self._button is not None:
            self._button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _animate(self, panel: TaskPanel, target: QPoint, *, closing: bool) -> None:
        """Slide the panel to ``target`` (or hide it at once when motion is reduced)."""
        self._animation.stop()
        self._closing = closing
        if not motion.animations_enabled():
            panel.move(target)
            if closing:
                panel.hide()
            return
        self._animation.setTargetObject(panel)
        self._animation.setStartValue(panel.pos())
        self._animation.setEndValue(target)
        self._animation.start()

    def _on_animation_finished(self) -> None:
        if self._closing and self._panel is not None:
            self._panel.hide()
