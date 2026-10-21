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

"""The one accessibility question asked before a launch.

Window-modal and non-blocking: the caller uses ``open()``, never ``exec()``, so the event loop keeps
turning while the player decides. There is no timeout; a question nobody answers must not launch the
game behind the player's back, and the caller keeps the launch button disabled meanwhile.

Closing the window is the same answer as "No" for the game (nothing is written) but is recorded
separately as :attr:`AccessibilityState.DISMISSED`. The caller still launches: this dialog is the
launcher asking a favour, not a gate on playing the game.

Accessibility:
* the title and body are announced through the launcher's existing status announcement channel
  (``gui.widgets.announce_to_assistive_tech``), which never sends ``QAccessible::Alert`` - the one
  event Qt's Windows bridge answers with a system sound;
* [Yes] is the default button and receives focus; [No] is a secondary button;
* Space activates the focused button, Enter activates the default [Yes], so both confirm;
* the focus ring remains gated on ``keyboardFocus`` from ``KeyboardFocusTracker``, exactly as on every
  other page;
* if an ancestor is kept off screen, the dialog carries ``WA_DontShowOnScreen`` before it is shown,
  which is what lets the render tools and the test suite exercise the real dialog without a desktop
  window.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui import i18n
from gui.widgets import announce_to_assistive_tech
from launcher.accessibility import VersionTier

tr = i18n.tr

CHOICE_YES = "yes"
CHOICE_NO = "no"
CHOICE_DISMISSED = "dismissed"

_COPY_KEYS = {
    VersionTier.ONBOARD_SCREEN: (
        "accessibility.dialog.onboard.title",
        "accessibility.dialog.onboard.body",
    ),
    VersionTier.NARRATOR_ONLY: (
        "accessibility.dialog.narrator.title",
        "accessibility.dialog.narrator.body",
    ),
}


def _ancestor_is_kept_off_screen(widget: QWidget | None) -> bool:
    """Whether any window above ``widget`` is marked ``WA_DontShowOnScreen``.

    The marker is not inherited by a top-level QDialog (the quit-confirmation incident measured that
    with a QMessageBox), so the dialog has to look for it and carry it before being shown.
    """
    try:
        node = widget
        while node is not None:
            if node.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
                return True
            node = node.parentWidget()
    except RuntimeError:  # a half-destroyed parent during shutdown
        return False
    return False


class AccessibilityDialog(QDialog):
    """Ask whether the game's onboarding screen or narrator may be enabled.

    The dialog only asks and reports the answer. Nothing is written from here; that remains the
    pre-launch gate's job so the state mapping lives in one place.
    """

    def __init__(self, parent: QWidget | None, tier: VersionTier | str) -> None:
        super().__init__(parent)
        self._tier = VersionTier(tier)
        if self._tier not in _COPY_KEYS:
            raise ValueError(f"no accessibility question for tier {self._tier.value!r}")
        self._parent = parent
        self._choice: str | None = None
        self._parent_closing = False
        self._content_announced = False

        title_key, body_key = _COPY_KEYS[self._tier]
        self._title_text = tr(title_key)
        self._body_text = tr(body_key)

        self.setWindowTitle(self._title_text)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(480)
        self.setSizeGripEnabled(False)
        if _ancestor_is_kept_off_screen(parent):
            self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

        self.title_label = QLabel(self._title_text)
        self.title_label.setObjectName("title")
        self.title_label.setWordWrap(True)
        self.title_label.setAccessibleName(self._title_text)

        self.body_label = QLabel(self._body_text)
        self.body_label.setObjectName("overlayBody")
        self.body_label.setWordWrap(True)
        self.body_label.setAccessibleName(self._body_text)

        self.yes_button = QPushButton(tr("accessibility.dialog.yes"))
        self.yes_button.setAccessibleName(tr("accessibility.dialog.yes"))
        # Both the Qt default role and the stylesheet's marker: this build does not paint the
        # standard :default state reliably once a stylesheet sets a button fill.
        self.yes_button.setDefault(True)
        self.yes_button.setAutoDefault(True)
        self.yes_button.setProperty("defaultChoice", True)
        self.yes_button.clicked.connect(self._answer_yes)

        self.no_button = QPushButton(tr("accessibility.dialog.no"))
        self.no_button.setObjectName("secondaryButton")
        self.no_button.setAccessibleName(tr("accessibility.dialog.no"))
        self.no_button.setAutoDefault(False)
        self.no_button.clicked.connect(self._answer_no)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        buttons.addStretch(1)
        buttons.addWidget(self.no_button)
        buttons.addWidget(self.yes_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        layout.addLayout(buttons)

        if parent is not None:
            parent.installEventFilter(self)
        # Delete through the event loop once the window is gone, not while its finished handler is
        # still reading the choice. Scheduling it here means every caller gets the lifecycle.
        self.finished.connect(self.deleteLater)

    @property
    def choice(self) -> str | None:
        """``yes`` / ``no`` / ``dismissed``, or None while the question is still open."""
        return self._choice

    @property
    def parent_closing(self) -> bool:
        """True when the parent window closed the dialog: the caller must not start a launch for that."""
        return self._parent_closing

    def _answer_yes(self) -> None:
        if self._choice is None:
            self._choice = CHOICE_YES
        self.accept()

    def _answer_no(self) -> None:
        if self._choice is None:
            self._choice = CHOICE_NO
        self.reject()

    def reject(self) -> None:
        """The window close button and Esc: record the dismissal, then close normally."""
        if self._choice is None:
            self._choice = CHOICE_DISMISSED
        super().reject()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.yes_button.setFocus()
        if not self._content_announced:
            self._content_announced = True
            # Announced after the window exists and has its focus, through the same gated carriers
            # every status message uses. The labels themselves stay ordinary QLabels so their
            # titles keep the theme's styling; only the announcement path is shared.
            announce_to_assistive_tech(self.title_label, self._title_text)
            announce_to_assistive_tech(self.body_label, self._body_text)

    def eventFilter(self, obj, event) -> bool:
        """Close with the parent window and never launch after the parent has gone away."""
        if obj is self._parent and event.type() == QEvent.Type.Close:
            self._parent_closing = True
            if self._choice is None:
                self._choice = CHOICE_DISMISSED
            self.reject()
        return super().eventFilter(obj, event)
