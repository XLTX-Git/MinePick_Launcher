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

"""In-window dialogs: the crash-diagnosis card, without an OS window of its own.

The launcher draws its own window frame and its own cards, so a system message box is the one
surface that steps outside that language: a title bar in the platform's font, the platform's button
order, the platform's colours, and - on Windows - a real window that a keyboard user can lose behind
the launcher. The crash diagnosis dialog already solved this by drawing a card over the window itself
(``gui/dialogs/crash_dialog.py`` on ``gui/overlay.py``); this module is the same idea for the ordinary
questions the rest of the app asks.

How it is built (and why it is a ``QDialog`` at all)
----------------------------------------------------
``QDialog`` is the only widget Qt gives a *blocking* ``exec()``, and every call site here is written
synchronously (``if not ok: return``). The dialog is therefore a ``QDialog`` whose window flag is
``Qt::Widget``: it is a child of the launcher window, never a top-level window, so it cannot appear on
the desktop, has no native title bar, and inherits the application stylesheet. It covers the whole
parent window with the crash overlay's blurred, dimmed backdrop and centres the same ``#overlayCard``
card in it - the object names, the QSS and the type scale are shared with the crash dialog, so the two
cannot drift apart.

Three forms are built on one base:

* the **prompt** (one 「好」 button),
* the **confirm** (「取消」/「确定」, or any two labels the caller passes),
* the **input** (a field plus the same two buttons).

Return values
-------------
Closing is *not* cancelling. ``Outcome`` separates the three ways a dialog ends:

* ``ACCEPTED``  - the accepting button was activated (Enter does that by default),
* ``REJECTED``  - the cancel button was activated,
* ``DISMISSED`` - Esc or a click on the backdrop: nobody answered the question.

``InWindowDialog.ask()`` returns the outcome; the module-level helpers turn it into what their callers
want (``bool`` for a confirmation, ``(text, ok)`` for an input). A caller that must not treat a
dismissal as a "no" can ask for the outcome itself.

Keyboard
--------
Esc dismisses, Enter activates the dialog's *default* button, Tab and Shift+Tab cycle inside the card
and never leave it (the trap is ``focusNextPrevChild``: it always consumes the key, so the page behind
the backdrop is unreachable). The default button carries the ``defaultChoice`` property the stylesheet
already paints a ring from, and the keyboard focus ring comes from ``KeyboardFocusTracker`` like every
other control in the app.

Why ``ask()`` and not ``exec()``
--------------------------------
The blocking loop has exactly one name in this app. A test (or a tool that renders the UI) replaces
``InWindowDialog.ask`` to answer without a loop; overriding ``QDialog.exec`` instead would mean every
``QDialog`` in the process, and the suite's own safety net would be far too broad.
"""

from __future__ import annotations

import enum
import logging

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, theme
from gui.overlay import BLUR_DOWNSCALE, CARD_MARGIN, CARD_WIDTH, SCRIM

tr = i18n.tr

LOGGER = logging.getLogger(__name__)

#: The three ways a dialog can end (see the module docstring).
class Outcome(enum.Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISMISSED = "dismissed"


#: Severities a message can carry. ``question`` is the neutral "do you want to ...?" one: same colour
#: family as information, with a question mark instead of an "i" glyph.
SEVERITIES = ("information", "question", "warning", "critical")

#: The glyph drawn per severity, as a single character in the icon badge.
GLYPHS = {"information": "i", "question": "?", "warning": "!", "critical": "!"}

#: Which theme token colours the badge and the primary button per severity.
SEVERITY_TOKENS = {
    "information": "ACCENT",
    "question": "ACCENT",
    "warning": "FG_STATUS_WARNING",
    "critical": "DANGER_FG",
}

#: The button variant that carries the severity. ``primaryButton`` is the filled accent button from the
#: base QSS; the other two are the outlined variants the sheets already define (danger) or add for the
#: warning case, so a warning's primary action is never mistaken for an ordinary information one.
SEVERITY_BUTTON = {
    "information": "primaryButton",
    "question": "primaryButton",
    "warning": "warningButton",
    "critical": "dangerButton",
}

#: The scrim tint per severity. Only a critical message darkens the backdrop red: that is the crash
#: dialog's "the launcher itself failed" signal, and a warning does not deserve it.
SEVERITY_SCRIM = {
    "information": "normal",
    "question": "normal",
    "warning": "normal",
    "critical": "error",
}

ICON_PX = 24  # logical size of the severity badge
CARD_MIN_WIDTH = 320  # the card never shrinks below this, whatever the message says
CARD_PADDING = 24  # the card's inner margin, on the spacing ladder
CARD_SPACING = 16
BODY_MAX_HEIGHT = 320  # a long error scrolls inside the card instead of growing past the window
PIXMAP_SCALE = 2  # the badge is drawn at 2x so it stays crisp on a scaled display


class _WrappedLabel(QLabel):
    """A word-wrapped label that reports the height its text needs at the width it was given.

    Qt lays a ``QLabel`` out by its ``sizeHint``, which for wrapped text is the *unwrapped* width - so
    inside a scroll area the label is resized to the viewport and the overflow is simply clipped, with
    no scrollbar. Pinning the minimum height to ``heightForWidth`` is what turns a long error message
    into scrollable text instead of a truncated one.
    """

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setMinimumHeight(self.heightForWidth(max(1, self.width())))



# ---------------------------------------------------------------- helpers


def host_window(parent: QWidget | None) -> QWidget | None:
    """The window a dialog draws itself in: the parent's top level, else the active window.

    Returning ``None`` means there is nothing to draw in (no window exists yet, which is the case for
    the pre-window password prompt in ``gui/main.py``). Callers must not fall back to an OS window: an
    in-window dialog with no window is a contradiction, and the module-level helpers refuse to show.
    """
    if parent is not None:
        window = parent if parent.isWindow() else parent.window()
        if window is not None:
            return window
    return QApplication.activeWindow()


def token_color(name: str) -> QColor:
    """A live theme colour, with the dark palette as the fallback when no theme has been applied."""
    tokens = theme.current_tokens() or {}
    value = tokens.get(name)
    if not value:
        try:
            value = theme.theme_palette("dark")[name]
        except (KeyError, RuntimeError):  # a token that does not exist is a bug, not a crash
            LOGGER.warning("in-window dialog: unknown theme token %r", name)
            return QColor(0, 0, 0, 0)
    return QColor(value)


def severity_pixmap(severity: str, size: int = ICON_PX) -> QPixmap:
    """A filled circle in the severity's colour with its glyph knocked out in the card colour.

    Painted at runtime like ``gui/icons.py`` does, for the same reasons: no asset per theme, and the
    badge follows the user's accent choice. The glyph is drawn in the card fill (``BG_LIST``) so it
    reads as a hole in the badge in both themes.

    The pixmap is twice the logical size and carries a device pixel ratio, so it stays crisp on a
    scaled display. Everything inside is painted in *logical* pixels: QPainter applies the ratio on
    its own, and drawing in device pixels there left one quarter of the circle in the canvas
    (measured: the badge showed a single arc and no glyph at all).
    """
    glyph = GLYPHS.get(severity, "i")
    colour = token_color(SEVERITY_TOKENS.get(severity, "ACCENT"))
    background = token_color("BG_LIST")
    pixmap = QPixmap(size * PIXMAP_SCALE, size * PIXMAP_SCALE)
    pixmap.setDevicePixelRatio(PIXMAP_SCALE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawEllipse(0, 0, size, size)
    font = painter.font()
    font.setPixelSize(int(size * 0.62))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(background)
    painter.drawText(
        QRect(0, 0, size, size),
        int(Qt.AlignmentFlag.AlignCenter),
        glyph,
    )
    painter.end()
    return pixmap


# ---------------------------------------------------------------- base


class InWindowDialog(QDialog):
    """The crash-diagnosis card as a blocking, in-window dialog.

    Subclasses fill :attr:`card_layout` and call :meth:`ask`. Everything a subclass must not have to
    repeat - the backdrop, the card, the geometry, the focus trap and the outcome bookkeeping - lives
    here.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        severity: str = "information",
        card_width: int = CARD_WIDTH,
    ) -> None:
        host = host_window(parent)
        super().__init__(host)
        # A child widget, not a window: no native frame, no taskbar entry, no desktop window. This is
        # the line that makes the whole feature "in-window" rather than "a dialog that looks like one".
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setObjectName("inWindowDialog")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self._severity = severity if severity in SEVERITIES else "information"
        self._card_width = card_width
        self._outcome: Outcome | None = None
        self._snapshot: QPixmap | None = None
        self._title_text = title
        self._title_label: QLabel | None = None
        self._icon_label: QLabel | None = None
        self._buttons: list[QPushButton] = []
        self._default_button: QPushButton | None = None
        self._default_outcome: Outcome | None = None

        self._card = QWidget(self)
        self._card.setObjectName("overlayCard")  # the crash dialog's card, from the shared QSS
        self._card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card.setAccessibleName(title)
        self._card_layout = QVBoxLayout(self._card)
        self._card_layout.setContentsMargins(CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING)
        self._card_layout.setSpacing(CARD_SPACING)
        self._build_head()
        self.hide()

    # ---------- construction ----------

    def _build_head(self) -> None:
        """The badge and the title, on one row: the crash dialog's title line plus a severity badge."""
        head = QHBoxLayout()
        head.setSpacing(12)
        self._icon_label = QLabel()
        self._icon_label.setPixmap(severity_pixmap(self._severity))
        self._icon_label.setAccessibleName(self._title_text)
        head.addWidget(self._icon_label, 0, Qt.AlignmentFlag.AlignTop)
        self._title_label = QLabel(self._title_text)
        self._title_label.setObjectName("title")
        self._title_label.setWordWrap(True)
        self._title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(self._title_label, 1)
        self._card_layout.addLayout(head)

    def add_body(self, text: str) -> QLabel:
        """Add the message body under the title, scrolling inside the card when it is long.

        The same recipe as the crash dialog (``QLabel#overlayBody`` in a frameless, transparent
        ``QScrollArea#overlayScroll``), with the height cap that keeps a long error from pushing the
        buttons out of the window.
        """
        label = _WrappedLabel(text)
        label.setObjectName("overlayBody")  # the crash dialog's body colour
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setAccessibleName(self._title_text)
        area = QScrollArea()
        area.setObjectName("overlayScroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Cap the width so a long message wraps at the card's width instead of widening the card with
        # its unwrapped size hint (the width the audit languages make that visible).
        area.setMaximumWidth(self._card_width - 2 * CARD_PADDING)
        area.setMaximumHeight(BODY_MAX_HEIGHT)
        area.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the arrows keys must not become a tab stop here
        area.setWidget(label)
        self._card_layout.addWidget(area, 1)
        self._body_label = label
        return label

    def add_buttons(self, entries: list[tuple[str, Outcome]], default: Outcome | None) -> None:
        """Add the button row. Entries are ``(label, outcome)``; the accepting one is styled primary."""
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        for label, outcome in entries:
            button = QPushButton(label)
            button.setObjectName(
                SEVERITY_BUTTON[self._severity]
                if outcome is Outcome.ACCEPTED
                else "secondaryButton"
            )
            button.setAccessibleName(label)
            # Enter is handled by this dialog (see keyPressEvent) so that it always activates the
            # *default* button: an autoDefault button would take Enter first and the two rules would
            # disagree about which button Enter means.
            button.setAutoDefault(False)
            button.setDefault(False)
            button.clicked.connect(lambda _checked=False, result=outcome: self._finish(result))
            self._buttons.append(button)
            row.addWidget(button)
            if default is not None and outcome is default:
                self._mark_default(button, outcome)
        self._card_layout.addLayout(row)

    def _mark_default(self, button: QPushButton, outcome: Outcome) -> None:
        """Mark the default button with the property the stylesheet draws its ring from.

        The standard ``:default`` pseudo-state identifies the wrong button in this Qt build (measured
        for the quit dialog, T-0004 E8 step 3), which is why the marker is an explicit property and
        why both buttons rendered pixel-identical without it.
        """
        self._default_button = button
        self._default_outcome = outcome
        button.setProperty("defaultChoice", True)
        button.setDefault(True)

    # ---------- public API ----------

    @property
    def outcome(self) -> Outcome | None:
        """How the dialog last ended (``None`` until it closes)."""
        return self._outcome

    @property
    def title_label(self) -> QLabel | None:
        return self._title_label

    @property
    def body_label(self) -> QLabel | None:
        return getattr(self, "_body_label", None)

    @property
    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)

    @property
    def default_button(self) -> QPushButton | None:
        return self._default_button

    @property
    def snapshot(self) -> QPixmap | None:
        """The blurred backdrop (None when grabbing it failed: the scrim is then a flat tint)."""
        return self._snapshot

    @property
    def card(self) -> QWidget:
        return self._card

    @property
    def card_layout(self) -> QVBoxLayout:
        """The card's layout, for subclasses that add their own rows."""
        return self._card_layout

    def ask(self) -> Outcome:
        """Show over the parent window, block until it closes, return how it ended.

        Subclasses call this once, from ``__init__``'s caller. It is deliberately the *only* blocking
        entry point: tests and render tools replace this method (never ``QDialog.exec``) to answer
        without a nested event loop.
        """
        self._outcome = None
        self._prepare()
        super().exec()
        return self._outcome if self._outcome is not None else Outcome.DISMISSED

    def _prepare(self) -> None:
        """Cover the host window, take the backdrop snapshot and place the card."""
        host = self.parentWidget()
        if host is not None:
            self.setGeometry(host.rect())
            host.installEventFilter(self)
            self._snapshot = self._grab_backdrop(host)
        self._place_card()

    def _grab_backdrop(self, host: QWidget) -> QPixmap | None:
        """Grab and downscale the window; degrade to the flat scrim when that is impossible."""
        if not host.isVisible():
            return None
        pixmap = host.grab()
        if pixmap.isNull() or pixmap.width() < BLUR_DOWNSCALE or pixmap.height() < BLUR_DOWNSCALE:
            return None
        return pixmap.scaled(
            max(1, pixmap.width() // BLUR_DOWNSCALE),
            max(1, pixmap.height() // BLUR_DOWNSCALE),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    # ---------- geometry ----------

    def _card_rect(self) -> QRect:
        available_w = max(CARD_MIN_WIDTH, self.width() - 2 * CARD_MARGIN)
        wanted = max(self._card_width, self._card.sizeHint().width(), CARD_MIN_WIDTH)
        width = min(wanted, available_w)
        available_h = max(120, self.height() - 2 * CARD_MARGIN)
        height = min(self._card.sizeHint().height(), available_h)
        x = (self.width() - width) // 2
        y = max(CARD_MARGIN, (self.height() - height) // 2)
        return QRect(x, y, width, height)

    def _place_card(self) -> None:
        self._card.setGeometry(self._card_rect())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_card()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        host = self.parentWidget()
        if host is not None:
            self.setGeometry(host.rect())
        self._place_card()
        self._focus_initial()

    def show(self) -> None:
        """Grab the backdrop while still hidden, then show.

        The snapshot has to be taken before this widget paints, otherwise it captures itself: a child
        covers the area it grabbed, and ``QWidget.grab`` renders every visible child. ``ask()`` grabs in
        :meth:`_prepare` for the same reason; this override covers a caller that shows the dialog itself
        (a test, a render tool). ``exec()`` calls the C++ ``show()``, so it does not come through here
        twice.
        """
        if self._snapshot is None:
            host = self.parentWidget()
            if host is not None:
                self._snapshot = self._grab_backdrop(host)
        super().show()

    def eventFilter(self, obj, event) -> bool:
        """Keep the overlay glued to the window it covers."""
        host = self.parentWidget()
        if obj is host and event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
            self.setGeometry(host.rect())
            self._place_card()
        return False

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if self._snapshot is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(self.rect(), self._snapshot)
        painter.fillRect(self.rect(), SCRIM[SEVERITY_SCRIM[self._severity]])

    # ---------- focus ----------

    def _focus_chain(self) -> list[QWidget]:
        """The card's focusable controls, in tab order.

        The TabFocus bit is the filter, not "any focus policy": a selectable label is ClickFocus, and
        Qt's own tab chain skips those too - so a body label that can be selected with the mouse must
        not become a stop that shows no focus ring.
        """
        chain: list[QWidget] = []
        for widget in self._card.findChildren(QWidget):
            if not (widget.focusPolicy() & Qt.FocusPolicy.TabFocus):
                continue
            if not widget.isVisibleTo(self._card) or not widget.isEnabled():
                continue
            chain.append(widget)
        return chain

    def _focus_initial(self) -> None:
        """Start on the default button, or in the field when there is one (that is what was asked)."""
        target = getattr(self, "input_widget", None) or self._default_button
        if target is not None:
            target.setFocus(Qt.FocusReason.TabFocusReason)

    def focusNextPrevChild(self, next: bool) -> bool:
        """Keep Tab inside the card.

        Returning True unconditionally is the trap: the key is consumed here, so the host window's
        focus chain (the page behind the backdrop, its title bar buttons, the sidebar) is never
        reached. The cycle wraps in both directions.
        """
        chain = self._focus_chain()
        if not chain:
            return True
        current = QApplication.focusWidget()
        index = chain.index(current) if current in chain else -1
        if index < 0:
            target = chain[0] if next else chain[-1]
        else:
            target = chain[(index + (1 if next else -1)) % len(chain)]
        target.setFocus(Qt.FocusReason.TabFocusReason)
        return True

    # ---------- closing ----------

    def _finish(self, outcome: Outcome) -> None:
        """Set the outcome, then let QDialog end its loop the matching way."""
        if self._outcome is None:
            self._outcome = outcome
        if outcome is Outcome.ACCEPTED:
            self.accept()
        else:
            self.reject()

    def accept(self) -> None:
        if self._outcome is None:
            self._outcome = Outcome.ACCEPTED
        super().accept()

    def reject(self) -> None:
        """Reaching QDialog's own reject (Esc, a parent close) is a dismissal, not a cancel."""
        if self._outcome is None:
            self._outcome = Outcome.DISMISSED
        super().reject()

    def closeEvent(self, event) -> None:
        if self._outcome is None:
            self._outcome = Outcome.DISMISSED
        host = self.parentWidget()
        if host is not None:
            host.removeEventFilter(self)
        super().closeEvent(event)

    # ---------- input ----------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Esc dismisses, Enter activates the default button, everything else is Qt's."""
        key = event.key()
        if key == Qt.Key.Key_Escape:
            # Esc is always "nobody answered", whether or not a cancel button exists next to it.
            self._outcome = Outcome.DISMISSED
            super().reject()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._default_outcome is not None:
                self._finish(self._default_outcome)
            else:
                self.accept()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        """A click on the backdrop dismisses; a click on the card must not (it falls through to here
        from the card, which is a plain QWidget and ignores mouse presses)."""
        if self._card.geometry().contains(event.position().toPoint()):
            event.accept()
            return
        self._outcome = Outcome.DISMISSED
        self.reject()
        event.accept()


# ---------------------------------------------------------------- the three forms


class InWindowMessage(InWindowDialog):
    """A prompt or a confirmation: a title, a body and one or two buttons."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        body: str = "",
        *,
        severity: str = "information",
        buttons: list[tuple[str, Outcome]] | None = None,
        default: Outcome | None = Outcome.ACCEPTED,
    ) -> None:
        super().__init__(parent, title=title, severity=severity)
        self.add_body(body)
        entries = buttons if buttons is not None else [(tr("dialog.ok"), Outcome.ACCEPTED)]
        self.add_buttons(entries, default)
        if default is None and self._buttons:
            # No explicit default: Enter still has to do something, and the accepting button is the
            # one a dialog's Enter means. The marker stays off (nothing is highlighted).
            self._default_outcome = Outcome.ACCEPTED
            self._default_button = next(
                (
                    button
                    for button, (_label, outcome) in zip(self._buttons, entries, strict=True)
                    if outcome is Outcome.ACCEPTED
                ),
                None,
            )


class InWindowInput(InWindowDialog):
    """An input: a title, a body, one field and the cancel/confirm pair."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        body: str = "",
        *,
        value: str = "",
        password: bool = False,
        multiline: bool = False,
        severity: str = "question",
    ) -> None:
        super().__init__(parent, title=title, severity=severity)
        self.add_body(body)
        self.multiline = multiline
        if multiline:
            field: QWidget = QPlainTextEdit()
            field.setPlainText(value)
            field.setMinimumHeight(96)  # three lines: enough to see a note, not a fixed box
        else:
            field = QLineEdit(value)
            field.setEchoMode(
                QLineEdit.EchoMode.Password if password else QLineEdit.EchoMode.Normal
            )
            field.selectAll()
        field.setObjectName("inWindowInput")
        field.setAccessibleName(body or title)
        self.input_widget = field
        self._card_layout.addWidget(field)
        self.add_buttons(
            [(tr("dialog.cancel"), Outcome.REJECTED), (tr("dialog.confirm"), Outcome.ACCEPTED)],
            Outcome.ACCEPTED,
        )

    # ---------- public API ----------

    @property
    def text(self) -> str:
        """The field's text as it stands (also after a cancel, like ``QInputDialog.getText``)."""
        field = self.input_widget
        if isinstance(field, QPlainTextEdit):
            return field.toPlainText()
        return field.text()

    def set_text(self, value: str) -> None:
        field = self.input_widget
        if isinstance(field, QPlainTextEdit):
            field.setPlainText(value)
        else:
            field.setText(value)


# ---------------------------------------------------------------- call-site helpers


def _ask(dialog: InWindowDialog) -> Outcome:
    """Run a dialog and return its outcome; never draws when there is no window to draw in."""
    if dialog.parentWidget() is None:
        LOGGER.warning(
            "in-window dialog %r had no window to draw in: the question was not asked",
            dialog.title_label.text() if dialog.title_label is not None else "",
        )
        return Outcome.DISMISSED
    return dialog.ask()


def information(parent: QWidget | None, title: str, text: str) -> Outcome:
    """``QMessageBox.information``: one 「好」 button, the accent-coloured badge and primary button."""
    return _ask(InWindowMessage(parent, title, text, severity="information"))


def warning(parent: QWidget | None, title: str, text: str) -> Outcome:
    """``QMessageBox.warning``: the warning badge and an outlined warning-coloured primary button."""
    return _ask(InWindowMessage(parent, title, text, severity="warning"))


def critical(parent: QWidget | None, title: str, text: str) -> Outcome:
    """``QMessageBox.critical``: the danger badge, the red scrim and the danger-coloured primary."""
    return _ask(InWindowMessage(parent, title, text, severity="critical"))


def confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    accept_text: str | None = None,
    reject_text: str | None = None,
    default: Outcome = Outcome.ACCEPTED,
    severity: str = "question",
) -> Outcome:
    """A two-button question with caller-supplied labels; returns the ``Outcome``.

    ``default`` names the button Enter activates. A destructive question passes ``Outcome.REJECTED``
    so the safe answer stays the default one, which is what ``QMessageBox.question(..., No)`` did.
    """
    entries = [
        (reject_text or tr("dialog.cancel"), Outcome.REJECTED),
        (accept_text or tr("dialog.confirm"), Outcome.ACCEPTED),
    ]
    return _ask(InWindowMessage(parent, title, text, severity=severity, buttons=entries, default=default))


def question(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    yes_text: str | None = None,
    no_text: str | None = None,
    default_yes: bool = False,
) -> bool:
    """``QMessageBox.question``: True only when the accepting button was activated.

    ``default_yes`` mirrors the old box's default button: ``QMessageBox.question`` with a ``No``
    default (the destructive questions) keeps ``No`` on Enter.
    """
    outcome = confirm(
        parent,
        title,
        text,
        accept_text=yes_text or tr("dialog.yes"),
        reject_text=no_text or tr("dialog.no"),
        default=Outcome.ACCEPTED if default_yes else Outcome.REJECTED,
        severity="question",
    )
    return outcome is Outcome.ACCEPTED


def get_text(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    value: str = "",
    password: bool = False,
    multiline: bool = False,
) -> tuple[str, bool]:
    """``QInputDialog.getText`` / ``getMultiLineText``: ``(text, ok)``.

    ``ok`` is False for both a cancel click and a dismissal, which is the distinction the old API did
    not make either; the field's text is still returned so a caller can preserve what was typed.
    """
    dialog = InWindowInput(
        parent, title, text, value=value, password=password, multiline=multiline
    )
    outcome = _ask(dialog)
    return dialog.text, outcome is Outcome.ACCEPTED


__all__ = [
    "GLYPHS",
    "SEVERITIES",
    "InWindowDialog",
    "InWindowInput",
    "InWindowMessage",
    "Outcome",
    "confirm",
    "critical",
    "get_text",
    "host_window",
    "information",
    "question",
    "severity_pixmap",
    "token_color",
    "warning",
]
