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

"""The combo popup: one opaque outlined panel, flush under the field, with a softly marked row.

A QComboBox popup is a top-level ``QComboBoxPrivateContainer`` (a QFrame carrying ``Qt::Popup``),
not a child of the combo. Fusion asks Qt for the *menu* flavour of that popup (``SH_ComboBox_Popup``
is 1): the panel is positioned so the selected row lines up with the field - opening upwards when a
lower row is selected - its rows are painted through QMenu's rules (a saturated ACCENT_SEL block on
the field), and the list's own QSS surface is not used at all. This module does two things, once,
for every combo when it first appears:

* :class:`_ComboPopupStyle` answers ``SH_ComboBox_Popup`` with 0, so Qt uses the ordinary listbox
  popup: flush below the field, rows from ``QComboBox QAbstractItemView``, no menu semantics;
* the window is made frameless and **opaque**. The container itself paints the BG_LIST fill and the
  1px BORDER_LIST outline (square corners, sharing the field's bottom border as its top edge), and
  the list paints its own BG_LIST surface inside it. The current entry is selected so exactly one
  row carries the soft highlight, and the list is named ``comboPopupList`` for the explicit
  stylesheet rule.

The container used to be a transparent window so a rounded list could be the whole panel. On the
native Windows platform that left the panel's fill entirely to the list: wherever the platform's
list painting did not cover the window, the page behind showed through, so the rows read as text
floating with no surface under it. An opaque container is the conservative shape: even if a
platform's list painting differs, the container behind it is already solid BG_LIST.

``Qt::Popup`` stays on the window, because that flag is what closes the dropdown when the user
clicks somewhere else or presses Escape. This is a global, automatic hook: ``install()`` is called
once from ``gui.main.create_app``, and the filter shapes every combo shown afterwards, plus every
combo that already exists when it is installed. No page has to touch its own combo boxes.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QProxyStyle,
    QStyle,
    QStyleFactory,
    QWidget,
)

#: Object name of the popup window. The stylesheet rule below uses it to paint the container's own
#: opaque fill and outline, without touching the list (also a QFrame subclass) inside it.
POPUP_OBJECT_NAME = "comboPopup"

#: Object name of the popup's list. The sheet targets it explicitly as well as through the combo
#: descendant selector, so the panel cannot lose its fill when Qt wraps the view differently.
POPUP_LIST_OBJECT_NAME = "comboPopupList"

#: Pixels of overlap Qt's listbox placement leaves between the popup's top border and the field's
#: bottom border. The panel may never cover the field - the user must be able to read the value they
#: just opened - so the popup is nudged down by exactly this amount when Qt places it below. The
#: sheet then leaves the panel's top border off, and the field's own bottom border is the seam.
POPUP_BORDER_OVERLAP = 1

#: Marks a popup that this module already shaped, so repeated Show events cannot reset its flags.
_SHAPED_PROPERTY = "comboPopupShaped"

#: The token values the container was last painted with. A theme swap changes them, so the sheet is
#: refreshed on the next show instead of keeping the colours of the theme it was first shaped under.
_SURFACE_PROPERTY = "comboPopupSurface"


def _apply_popup_surface(popup: QWidget) -> None:
    """Paint the container itself: opaque BG_LIST, a 1px outline on all four sides, square corners.

    This is the part that makes the dropdown readable on the native platform. The old shape cleared
    the container's fill and made the window translucent, trusting the list inside to be the whole
    panel; when that trust was wrong the page showed through the dropdown. Now the container owns an
    opaque fill of its own.

    The outline uses BORDER_HOVER rather than BORDER_LIST: against BG_LIST the plain border sits at
    roughly 1.3:1, which on a real screen reads as "no border at all" and made the panel look like a
    continuation of the field. The top edge is drawn too - sharing the field's bottom border sounded
    tidy but left the first row's highlight touching the field with no seam between them.
    """
    from gui.theme import current_tokens

    tokens = current_tokens()
    background = tokens.get("BG_LIST", "#ffffff")
    border = tokens.get("BORDER_HOVER") or tokens.get("BORDER_LIST", "#c9d2dc")
    if popup.property(_SURFACE_PROPERTY) == (background, border):
        return
    popup.setStyleSheet(
        f"QFrame#{POPUP_OBJECT_NAME} {{"
        f" background: {background};"
        f" border: 1px solid {border};"
        f" border-radius: 0;"
        f" }}"
    )
    # The window itself is not translucent, but a platform can still paint its own window colour in
    # the frame's margin before the sheet runs. Filling from the palette too means those pixels are
    # BG_LIST rather than the desktop's default window colour.
    popup.setAutoFillBackground(True)
    palette = popup.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(background))
    popup.setPalette(palette)
    popup.setProperty(_SURFACE_PROPERTY, (background, border))


def shape_combo_popup(combo: QComboBox) -> QWidget | None:
    """Give ``combo``'s popup an opaque, frameless panel; return the popup (or None).

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
        # A theme swap may have changed BG_LIST/BORDER_LIST since the popup was first shaped.
        _apply_popup_surface(popup)
        return popup

    # The container paints its own opaque panel; the list inside paints its BG_LIST surface. Never
    # ask for a translucent window here: on the native Windows platform it let the page behind a
    # dropdown show through the panel (the text floated with no surface under it).
    popup.setObjectName(POPUP_OBJECT_NAME)
    if isinstance(popup, QFrame):
        popup.setFrameShape(QFrame.Shape.NoFrame)
    popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    popup.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
    # Keep Qt::Popup: it is the flag that closes the dropdown on an outside click or Escape. Only
    # the decorations are added. setWindowFlags() is called before the popup is first shown.
    popup.setWindowFlags(
        popup.windowFlags()
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    _apply_popup_surface(popup)
    # Name the list explicitly for the sheet, and repolish so a selector on the new name applies
    # immediately (the combo descendant selector keeps working as the fallback in the same rule).
    view = combo.view()
    if view.objectName() != POPUP_LIST_OBJECT_NAME:
        view.setObjectName(POPUP_LIST_OBJECT_NAME)
        view.style().unpolish(view)
        view.style().polish(view)
    popup.setProperty(_SHAPED_PROPERTY, True)
    return popup


def _fit_popup_to_rows(combo: QComboBox, popup: QWidget) -> None:
    """Make the panel exactly as tall as the rows it shows.

    The listbox popup reserves a little space below the last row, which on a three-item list reads as
    an empty strip and makes the panel look taller than its contents. Pinning the view first matters:
    the container's layout sizes the window from the view's hints, so resizing the window alone is
    undone on the next layout pass.
    """
    try:
        view = combo.view()
    except RuntimeError:  # the combo's C++ side is already gone
        return
    if view is None or view.window() is not popup:
        return
    model = view.model()
    rows = model.rowCount() if model is not None else 0
    row_height = view.sizeHintForRow(0) if rows else 0
    if not rows or row_height <= 0 or rows > combo.maxVisibleItems():
        return
    wanted = rows * row_height
    if view.minimumHeight() != wanted or view.maximumHeight() != wanted:
        view.setFixedHeight(wanted)
    if popup.height() > wanted + 2:  # the container's own 1px top and bottom border
        popup.resize(popup.width(), wanted + 2)


def place_combo_popup(popup: QWidget) -> None:
    """Prepare a shaped popup once Qt has shown it: repaint for the current theme, select the row.

    The panel deliberately keeps Qt's placement - flush under the field, its top border sharing
    the field's bottom border - so no geometry is changed here. What the listbox popup does not do
    is mark the entry the field currently holds (the menu-style popup did, which is why the old
    screenshot had a filled row sitting on top of the input). Selecting it here makes that one row
    carry the soft highlight, and no other row is filled.
    """
    combo = popup.parentWidget()
    if not isinstance(combo, QComboBox):
        return
    try:
        view = combo.view()
    except RuntimeError:  # the combo's C++ side is already gone
        return
    if view.window() is not popup:
        return
    # The theme may have changed since the combo was first shown; refresh the container's colours
    # before the panel becomes visible.
    _apply_popup_surface(popup)
    # A listbox popup opens without a selected row (only the menu-style one marked the current
    # entry). Selecting it makes the open panel say which item the field already holds, and only
    # that row carries the highlight - never the whole panel.
    model = view.model()
    if model is not None:
        index = model.index(combo.currentIndex(), combo.modelColumn())
        selection = view.selectionModel()
        if index.isValid() and selection is not None:
            selection.setCurrentIndex(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
    # Qt's listbox placement puts the popup's top border one pixel on top of the field's bottom
    # border. That is invisible when both are the same colour, but it also means the panel's window
    # starts inside the field. Nudge it down so the field's value is never covered; the sheet's
    # borderless top edge then shares the field's bottom border as the seam.
    #
    # ``popup.pos()`` is the top-level window's screen position; ``mapToGlobal`` on a Qt::Popup can
    # disagree with it by the platform frame margin while the window is being shown, which made the
    # nudge depend on which row was selected. The combo has no frame, so its mapToGlobal is stable.
    combo_bottom = combo.mapToGlobal(QPoint(0, combo.height())).y()
    popup_top = popup.pos().y()
    if 0 < combo_bottom - popup_top <= POPUP_BORDER_OVERLAP:
        popup.move(popup.pos().x(), combo_bottom)

    # Trim the panel to the rows it actually shows. The listbox popup reserves a few extra pixels
    # below the last row, which on a three-item menu reads as an empty strip under the last entry and
    # makes the panel taller than its contents. Only when every row fits on screen: a capped list
    # keeps its height and scrolls instead.
    # Fit the panel to its rows, once now and once on the next event-loop turn: this function runs on
    # the popup's Show event, which is *before* Qt assigns the popup its final geometry, so an
    # immediate resize is undone by the platform's own sizing. The deferred call sees the real size.
    _fit_popup_to_rows(combo, popup)
    QTimer.singleShot(0, popup, lambda: _fit_popup_to_rows(combo, popup))


class _ComboPopupStyle(QProxyStyle):
    """Fusion, except that a combo popup is a list panel instead of a menu.

    QFusionStyle answers ``SH_ComboBox_Popup`` with 1, which tells QComboBox to treat the popup as a
    menu: Qt then aligns the *currently selected row* with the field (opening above it when a lower
    row is selected), draws the rows through ``QComboMenuDelegate`` (so ``QMenu::item:selected`` and
    its saturated ACCENT_SEL fill paint a field-sized block on top of the input), and paints the
    container with ``PE_PanelMenu`` instead of letting ``QComboBox QAbstractItemView`` draw its own
    surface. The result was a green row sitting where the input was, no panel border, and a dropdown
    that opened upwards over the category chips. Answering 0 selects the ordinary listbox popup: a
    panel flush under the field, one softly highlighted row, and the list's own QSS surface.
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


def install_combo_popup_style(app: QApplication | None = None) -> bool:
    """Install :class:`_ComboPopupStyle` as the application style.

    Must run after the base style is chosen (``setStyle("Fusion")``) and before the theme's
    stylesheet is installed, so QStyleSheetStyle wraps this style. Idempotent.
    """
    application = app if app is not None else QApplication.instance()
    if application is None:
        return False
    if getattr(application, "_combo_popup_style", None) is not None:
        return False
    base = QStyleFactory.create("Fusion") or application.style()
    style = _ComboPopupStyle(base)
    application.setStyle(style)
    application._combo_popup_style = style
    # A stylesheet installed before this call has to be re-applied so it wraps the new base style.
    if application.styleSheet():
        application.setStyleSheet(application.styleSheet())
    return True


class _ComboPopupShaper(QObject):
    """Application event filter: shape a combo's popup when the combo itself is first shown."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show:
            try:
                if isinstance(obj, QComboBox):
                    shape_combo_popup(obj)
                elif isinstance(obj, QWidget) and obj.property(_SHAPED_PROPERTY):
                    place_combo_popup(obj)
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
