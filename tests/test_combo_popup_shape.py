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

"""The combo popup is a real rounded card: window attributes, pixels, keyboard and outside click.

The user reported this repeatedly: ``QComboBox QAbstractItemView`` had ``border-radius: 0`` because
the popup is its own top-level window and QSS cannot round one. gui/popup_shape.py now makes the
window translucent and frameless (keeping ``Qt::Popup``) so the list's QSS radius is visible. These
tests pin the parts that a stylesheet grep cannot see:

* the window really carries the attributes, and they are applied automatically when the combo is
  shown (the install hook, not a per-page call);
* the four corners of the rendered window are transparent, so it is not a square window with a
  rounded card inside it (the "black edge" the user warned about);
* clicking outside the popup still closes it, and the arrow keys still move the highlighted row and
  commit on Enter;
* while the popup is open, keyboard focus stays on a visible control inside the popup (it never
  wanders to a widget the user cannot see).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parents[1]
SHEETS = (ROOT / "gui/resources/style.qss", ROOT / "gui/resources/style_light.qss")


@pytest.fixture()
def app():
    """The suite's QApplication, with the dark sheet in force so the QSS radius is the shipped one."""
    application = QApplication.instance() or QApplication([])
    from gui.theme import apply_theme

    apply_theme("dark", None, None, "default")
    return application


@pytest.fixture()
def shaper(app):
    """Install the automatic hook for one test and remove it again (no cross-test side effects)."""
    from gui import popup_shape

    installed = popup_shape.install(app)
    try:
        yield popup_shape
    finally:
        if installed:
            popup_shape.uninstall(app)


def _combo_window(app, *, items: int = 6) -> tuple[QWidget, QComboBox]:
    """A shown window with one combo at the top and empty space below it for an outside click."""
    window = QWidget()
    window.resize(440, 340)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(12, 12, 12, 12)
    combo = QComboBox()
    combo.addItems([f"option {index}" for index in range(items)])
    combo.setCurrentIndex(0)
    layout.addWidget(combo)
    layout.addStretch(1)
    window.show()
    app.processEvents()
    return window, combo


def _open(combo: QComboBox, app) -> QWidget:
    combo.showPopup()
    for _ in range(8):
        app.processEvents()
    return combo.view().window()


def test_combo_popup_is_shaped_automatically_when_the_combo_is_shown(app, shaper):
    """The hook, not each page, shapes the popup; it happens on the combo's Show event."""
    window = QWidget()
    window.resize(420, 320)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(12, 12, 12, 12)
    combo = QComboBox()
    combo.addItems(["one", "two", "three"])
    layout.addWidget(combo)
    # Ask for the popup before the combo is shown: creating the container must not shape it; the
    # Show event from window.show() is what the installed hook reacts to.
    popup = combo.view().window()
    assert not popup.property("comboPopupShaped"), "the popup was shaped before the combo was shown"

    window.show()
    app.processEvents()

    assert popup is not combo
    assert popup.property("comboPopupShaped"), "the install hook did not shape the shown combo"

    flags = popup.windowFlags()
    assert flags & Qt.WindowType.Popup, "Qt::Popup is what closes the dropdown on an outside click"
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.NoDropShadowWindowHint
    assert popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert popup.frameShape() == QFrame.Shape.NoFrame
    assert popup.objectName() == shaper.POPUP_OBJECT_NAME

    popup = _open(combo, app)
    assert popup.isVisible()
    assert combo.view().isVisible()
    assert popup.width() >= combo.width()
    combo.hidePopup()
    window.close()


def test_combo_popup_window_has_transparent_rounded_corners(app, shaper):
    """Rendered alpha: the four corners are page-behind, not the square window fill."""
    window, combo = _combo_window(app)
    popup = _open(combo, app)
    assert popup.isVisible()

    image = QImage(popup.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    popup.render(image)

    corners = ((0, 0), (image.width() - 1, 0), (0, image.height() - 1), (image.width() - 1, image.height() - 1))
    for point in corners:
        assert image.pixelColor(*point).alpha() == 0, (
            f"popup corner {point} is opaque: the square window fill is still painted outside the "
            "rounded list, which is the black edge the user reported"
        )
    # Qt reserves 4px above and below the list inside the popup window; with the translucent window
    # those two bands are transparent as well, not a dark strip around the card.
    for x in (0, image.width() // 2, image.width() - 1):
        assert image.pixelColor(x, 0).alpha() == 0
    assert image.pixelColor(image.width() // 2, 5).alpha() > 0, "the list itself never painted"
    assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() == 255

    combo.hidePopup()
    window.close()


def test_combo_popup_closes_on_an_outside_click(app, shaper):
    """Qt::Popup must stay: a mouse press outside the dropdown closes it."""
    window, combo = _combo_window(app)
    popup = _open(combo, app)
    assert popup.isVisible()
    assert app.activePopupWidget() is popup

    # The top-right corner of the window is outside the (below-the-combo) popup and over inert
    # window space. The click goes through the real QWindow entry point, not widget.sendEvent.
    outside = QPoint(window.width() - 4, 4)
    global_outside = window.mapToGlobal(outside)
    popup_top_left = popup.mapToGlobal(QPoint(0, 0))
    assert not popup.rect().translated(popup_top_left).contains(global_outside)

    QTest.mouseClick(
        window.windowHandle(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        outside,
    )
    for _ in range(8):
        app.processEvents()
    assert not popup.isVisible(), "the dropdown stayed open after a click outside it"
    assert app.activePopupWidget() is None
    window.close()


def test_combo_popup_keeps_keyboard_navigation_and_focus_visible(app, shaper):
    """Arrow keys move the highlight, Enter commits, and focus never lands on an invisible widget."""
    window, combo = _combo_window(app)
    combo.setCurrentIndex(0)
    popup = _open(combo, app)
    view = combo.view()

    focus = app.focusWidget()
    assert focus is not None
    assert focus.isVisible()
    assert focus is view or popup.isAncestorOf(focus)

    assert view.currentIndex().row() == 0
    QTest.keyClick(view, Qt.Key.Key_Down)
    app.processEvents()
    assert view.currentIndex().row() == 1
    assert popup.isVisible()

    focus = app.focusWidget()
    assert focus is not None and focus.isVisible()
    assert focus is view or popup.isAncestorOf(focus)

    QTest.keyClick(view, Qt.Key.Key_Up)
    app.processEvents()
    assert view.currentIndex().row() == 0
    QTest.keyClick(view, Qt.Key.Key_Down)
    app.processEvents()
    assert view.currentIndex().row() == 1

    QTest.keyClick(view, Qt.Key.Key_Return)
    app.processEvents()
    assert not popup.isVisible(), "Enter did not commit and close the dropdown"
    assert combo.currentIndex() == 1

    # Re-open and walk Tab a few times. The popup may keep focus on its list or close; either is
    # fine, but focus must never be on a widget the user cannot see.
    popup = _open(combo, app)
    for _ in range(4):
        focus = app.focusWidget()
        if focus is not None:
            assert focus.isVisible(), f"focus moved to an invisible {type(focus).__name__}"
            if popup.isVisible():
                assert focus is view or popup.isAncestorOf(focus), (
                    "focus left the open popup for another widget"
                )
        QTest.keyClick(focus or view, Qt.Key.Key_Tab)
        app.processEvents()

    if popup.isVisible():
        QTest.keyClick(app.focusWidget() or view, Qt.Key.Key_Escape)
        app.processEvents()
        assert not popup.isVisible(), "Escape did not close the dropdown"
    combo.hidePopup()
    window.close()


def test_combo_popup_stylesheet_rounds_the_list_and_keeps_one_border():
    for sheet in SHEETS:
        text = sheet.read_text(encoding="utf-8")
        start = text.index("QComboBox QAbstractItemView {")
        block = text[start : text.index("}", start)]
        assert "border-radius: __RADIUS_LG__;" in block, sheet.name
        assert "border-radius: 0" not in block, sheet.name
        assert "border: 1px solid __BORDER_LIST__;" in block, sheet.name
        assert "padding: 4px;" in block, sheet.name


def test_create_app_installs_the_popup_shape_hook():
    """The automatic hook must live in the application bootstrap, not in one page's combo."""
    source = (ROOT / "gui/main.py").read_text(encoding="utf-8")
    assert "from gui import popup_shape" in source
    assert "popup_shape.install(app)" in source
