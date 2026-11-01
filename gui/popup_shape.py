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

"""Rounded corners for every combo box dropdown.

A QComboBox popup is a top-level ``QComboBoxPrivateContainer`` (a QFrame carrying ``Qt::Popup``),
not a child of the combo. A stylesheet can only paint *inside* that window: a ``border-radius`` on
``QComboBox QAbstractItemView`` rounds the list, while the window itself stays a square filled with
the palette's window colour - the square patch the user saw outside a rounded edge. No style sheet
can make a window translucent; only a window attribute can. This module sets that attribute, once,
for every combo when it first appears:

* ``Qt::Popup`` stays on the window, because that flag is what closes the dropdown when the user
  clicks somewhere else or presses Escape;
* ``FramelessWindowHint`` and ``NoDropShadowWindowHint`` remove the platform frame and its shadow,
  so nothing square is painted around the list;
* ``WA_TranslucentBackground`` makes the pixels outside the list's rounded border the page behind,
  instead of the window fill.

The list keeps drawing its own background, border and radius from the stylesheet; the window is
only made able to show them. This is a global, automatic hook: ``install()`` is called once from
``gui.main.create_app``, and the filter shapes every combo shown afterwards, plus every combo that
already exists when it is installed. No page has to touch its own combo boxes.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QWidget

#: Object name of the popup window. The stylesheet rule below uses it to clear the container's own
#: frame fill without touching the list (also a QFrame subclass) inside it.
POPUP_OBJECT_NAME = "comboPopup"

#: Marks a popup that this module already shaped, so repeated Show events cannot reset its flags.
_SHAPED_PROPERTY = "comboPopupShaped"


def shape_combo_popup(combo: QComboBox) -> QWidget | None:
    """Give ``combo``'s popup a transparent, frameless window; return the popup (or None).

    Safe to call more than once and safe before the combo has ever been shown. The popup is created
    by ``view()`` on first use, so asking for it here does not show anything.
    """
    try:
        popup = combo.view().window()
    except RuntimeError:  # the combo's C++ side is already gone
        return None
    if popup is None or popup is combo:
        return None
    # Only the combo's own popup container, never a view that some custom combo parented into the
    # main window: turning the main window translucent would be a catastrophic mistake here.
    if popup.parentWidget() is not combo or not (popup.windowFlags() & Qt.WindowType.Popup):
        return None
    if popup.property(_SHAPED_PROPERTY):
        return popup

    # The window's own fill is the square patch around the rounded list. Clearing the frame and
    # asking for per-pixel alpha removes it; the QSS rule for the name is the belt to that braces
    # (a QFrame subclass could otherwise paint a panel edge inside the window).
    popup.setObjectName(POPUP_OBJECT_NAME)
    if isinstance(popup, QFrame):
        popup.setFrameShape(QFrame.Shape.NoFrame)
    popup.setAutoFillBackground(False)
    popup.setStyleSheet(
        f"QFrame#{POPUP_OBJECT_NAME} {{ background: transparent; border: none; }}"
    )
    # Keep Qt::Popup: it is the flag that closes the dropdown on an outside click or Escape. Only
    # the decorations are added. setWindowFlags() is called before the popup is first shown.
    popup.setWindowFlags(
        popup.windowFlags()
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    popup.setProperty(_SHAPED_PROPERTY, True)
    return popup


class _ComboPopupShaper(QObject):
    """Application event filter: shape a combo's popup when the combo itself is first shown."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, QComboBox):
            try:
                shape_combo_popup(obj)
            except Exception:  # noqa: BLE001 - a filter may never raise into Qt's dispatch
                return False
        return False


def install(app: QApplication | None = None) -> bool:
    """Install the automatic hook; return whether this call installed it.

    Idempotent: the shaper is kept on the application object (a Python attribute is enough to keep
    the QObject alive) and a second call is a no-op. Existing combos are shaped immediately, which
    also covers a late install in a test or a tool.
    """
    application = app if app is not None else QApplication.instance()
    if application is None:
        return False
    if getattr(application, "_combo_popup_shaper", None) is not None:
        return False
    shaper = _ComboPopupShaper(application)
    application.installEventFilter(shaper)
    application._combo_popup_shaper = shaper
    for widget in QApplication.allWidgets():
        if isinstance(widget, QComboBox):
            shape_combo_popup(widget)
    return True


def uninstall(app: QApplication | None = None) -> bool:
    """Remove the hook (used by tests; the application itself keeps it for its whole life)."""
    application = app if app is not None else QApplication.instance()
    if application is None:
        return False
    shaper = getattr(application, "_combo_popup_shaper", None)
    if shaper is None:
        return False
    application.removeEventFilter(shaper)
    del application._combo_popup_shaper
    shaper.deleteLater()
    return True
