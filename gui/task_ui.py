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

"""The window's corner task icon, and the page it opens.

The icon answers "is anything running, and how far": a ring with the percentage in the middle while a
download runs, a check while everything just finished, an alert while a failure is waiting to be read.
It does not own state - it reads :class:`gui.tasks.TaskCenter` on every ``changed`` signal, so the arc it
draws and the bar on the page cannot disagree.

Clicking it opens the download page (``gui/pages/downloads_page.py``): a whole page, reached from the
window, that lists every task with its numbers, its cancel, its retry and its duration. This module no
longer owns a view of the list at all - the page is the list. What is left here is the icon's lifetime,
which is the list's:

The icon does not exist when there is nothing to show, and that is a structural rule, not ``hide()``: it
is created when the first task arrives and destroyed when the last one leaves, so there is no invisible
widget left in the window, no focus stop nobody can reach, and nothing for the walkthrough or a
screenshot to trip over. That rule is also why the page can never be reached empty: the only way in is a
widget that only exists while something is listed.

The icon paints itself. That is the same choice the version rows make: three faces on one accent fill,
the glyphs drawn rather than typed (the UI font has no guaranteed U+2713/U+26A0, and a missing glyph
would be a tofu box where the state should be), and the colours read from the theme's token table at
paint time - the one source of truth for both sheets.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractButton, QWidget

from gui import i18n, paint_utils, theme
from gui.tasks import DONE, FAILED, RUNNING, TaskCenter

tr = i18n.tr

#: Distance from the window content's right/bottom edges to the visible circle.
EDGE = 16
#: The circle the user sees. The widget is larger by the focus band on every side, so the halo can be
#: painted without shaving the circle.
ICON_SIZE = 48
FOCUS_BAND = 4
ICON_WIDGET = ICON_SIZE + FOCUS_BAND * 2


def _tokens() -> dict[str, str]:
    """The colour table of the theme in force (the light one before the first apply)."""
    return paint_utils._tokens()


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
        # The name says which face this is; the tooltip also says what pressing it does.
        tooltip = f"{name} · {tr('tasks.icon.open')}"
        if self.toolTip() != tooltip:
            self.setToolTip(tooltip)
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


class TaskOverlay(QObject):
    """Keeps the window's task icon alive and positioned, and turns a click into the download page.

    The button's lifetime is the list's: it is created with the first task and destroyed with the last,
    so "there is no icon" is a property of the widget tree rather than of a visibility flag. The page
    itself belongs to the window (it is a stack page with a remembered origin), so all this object does
    with a click is ask the window to open it - the icon never owns the list view.
    """

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._center: TaskCenter = window.tasks
        self._button: TaskButton | None = None
        self._center.changed.connect(self._sync)
        self._sync()

    # ---------- accessors ----------

    def button(self) -> TaskButton | None:
        return self._button

    def _host(self) -> QWidget:
        content = getattr(self._window, "content", None)
        return content if isinstance(content, QWidget) else self._window.centralWidget()

    # ---------- lifetime ----------

    def _sync(self) -> None:
        """Follow the center: grow or drop the icon, and keep it in the corner."""
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
            self._button.clicked.connect(self.open_downloads)
            self._button.show()
            self._button.raise_()
            self._place_button()
        else:
            self._place_button()

    def retranslate(self) -> None:
        """The pages were rebuilt in another language; the icon is the window's own."""
        if self._button is not None:
            self._button.refresh()

    # ---------- geometry ----------

    def _button_target(self) -> QPoint:
        host = self._host()
        return QPoint(
            max(0, host.width() - EDGE - ICON_SIZE - FOCUS_BAND),
            max(0, host.height() - EDGE - ICON_SIZE - FOCUS_BAND),
        )

    def _place_button(self) -> None:
        if self._button is None:
            return
        target = self._button_target()
        if self._button.pos() != target:
            self._button.move(target)

    def reposition(self) -> None:
        """The window changed size: move the icon back into its corner."""
        self._place_button()

    # ---------- the way in ----------

    def open_downloads(self) -> None:
        """The icon was clicked: open the download page (the window records where from)."""
        opener = getattr(self._window, "open_download_page", None)
        if callable(opener):
            opener()
