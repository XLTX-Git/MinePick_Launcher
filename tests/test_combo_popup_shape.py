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

"""The combo popup is an opaque outlined panel below the field, with one softly marked row.

The user reported this repeatedly: the dropdown opened upwards over the category chips, its
selected row was a saturated ACCENT_SEL block sitting on top of the input, and there was no visible
panel. The cause was Fusion's ``SH_ComboBox_Popup=1``: Qt treated the popup as a menu. These tests
pin the parts a stylesheet grep cannot see:

* the style override answers 0, so Qt uses the ordinary listbox popup;
* the window really carries the attributes, and they are applied automatically when the combo is
  shown (the install hook, not a per-page call);
* the window is opaque: every pixel of the grabbed panel has alpha 255, and the container itself
  paints BG_LIST plus a 1px BORDER_LIST outline (the old transparent window let the page behind a
  native dropdown show through);
* the panel opens below the field (sharing one border line), paints the BG_LIST fill and never
  paints the whole surface in ACCENT_SEL;
* the current row is the only highlighted row (BG_HOVER), and arrow keys move it;
* clicking outside still closes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QPushButton,
    QStyle,
    QStyleOptionComboBox,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parents[1]
SHEETS = (ROOT / "gui/resources/style.qss", ROOT / "gui/resources/style_light.qss")


@pytest.fixture()
def app():
    """The suite's QApplication, with the popup style override and the dark sheet in force."""
    application = QApplication.instance() or QApplication([])
    from gui import popup_shape

    popup_shape.install_combo_popup_style(application)
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
    assert not popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground), (
        "the dropdown window must stay opaque: a translucent window let the page show through the "
        "panel on the native platform"
    )
    assert popup.autoFillBackground()
    assert popup.frameShape() == QFrame.Shape.NoFrame
    assert popup.objectName() == shaper.POPUP_OBJECT_NAME
    from gui.theme import current_tokens

    tokens = current_tokens()
    sheet = popup.styleSheet()
    assert f"background: {tokens['BG_LIST']}" in sheet
    assert f"border: 1px solid {tokens['BORDER_LIST']}" in sheet
    assert "border-radius: 0" in sheet
    assert "transparent" not in sheet

    popup = _open(combo, app)
    assert popup.isVisible()
    assert combo.view().isVisible()
    assert popup.width() >= combo.width()
    combo.hidePopup()
    window.close()


def test_combo_popup_window_is_the_outlined_panel(app, shaper):
    """The window is the panel: its corners are the BORDER_LIST outline, not a square window fill.

    The panel is square and flush under the field by design now. What must not come back is the old
    translucent-window artefact: a window larger than the list, filled with the platform's window
    colour around a smaller surface (the "black edge"), or a window with holes the page showed
    through.
    """
    from gui.theme import current_tokens

    tokens = current_tokens()
    window, combo = _combo_window(app, items=3)
    popup = _open(combo, app)
    assert popup.isVisible()
    assert not popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    image = _render(popup)
    border_hex = tokens["BORDER_LIST"].lstrip("#")
    border = (int(border_hex[0:2], 16), int(border_hex[2:4], 16), int(border_hex[4:6], 16), 255)
    for point in ((0, 0), (image.width() - 1, 0), (0, image.height() - 1), (image.width() - 1, image.height() - 1)):
        colour = image.pixelColor(*point)
        assert (colour.red(), colour.green(), colour.blue(), colour.alpha()) == border, (
            f"popup corner {point} is not the panel outline: the window paints outside the list"
        )
    # The panel really covers the window: no unpainted margin around the list.
    assert all(
        image.pixelColor(x, 0).alpha() == 255 for x in (0, image.width() // 2, image.width() - 1)
    )
    assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() == 255
    combo.hidePopup()
    window.close()


def test_combo_popup_has_no_translucent_pixels(app, shaper):
    """Regression: the native dropdown showed the page through it.

    The window used to carry WA_TranslucentBackground and the container painted ``transparent``, so
    on the real platform the list's text floated with no panel under it. Offscreen cannot reproduce
    the compositing failure, but it can prove the two things that caused it are gone: the window is
    not translucent, and every pixel - border, container margin and list - is fully opaque.
    """
    from gui.theme import current_tokens

    tokens = current_tokens()
    window, combo = _combo_window(app, items=4)
    popup = _open(combo, app)
    image = _render(popup)
    assert not popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    min_alpha = 255
    translucent = []
    for y in range(image.height()):
        for x in range(image.width()):
            alpha = image.pixelColor(x, y).alpha()
            min_alpha = min(min_alpha, alpha)
            if alpha < 255:
                translucent.append((x, y, alpha))
    assert min_alpha == 255, (
        f"the popup has {len(translucent)} translucent pixels (min alpha {min_alpha}); the first "
        f"ones are {translucent[:8]}"
    )
    # The container is the fill, not just the list: the 1px inset behind the list is BG_LIST too.
    # Use the bottom row (the fixture selects row 0, whose highlight covers the top row).
    bg = tokens["BG_LIST"].lstrip("#")
    expected = (int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16), 255)
    inset = image.pixelColor(1, image.height() - 2)
    assert (inset.red(), inset.green(), inset.blue(), inset.alpha()) == expected
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


def _pixel_counts(image: QImage) -> dict[tuple[int, int, int, int], int]:
    counts: dict[tuple[int, int, int, int], int] = {}
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            key = (colour.red(), colour.green(), colour.blue(), colour.alpha())
            counts[key] = counts.get(key, 0) + 1
    return counts


def _render(popup: QWidget) -> QImage:
    image = QImage(popup.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    popup.render(image)
    return image


def test_combo_popup_style_overrides_fusion_menu_behaviour(app):
    """SH_ComboBox_Popup must answer 0: that is what keeps the panel below the field and out of
    QMenu's painting path. Read through a real style option - the hint depends on ``option``."""
    from gui.theme import current_tokens

    combo = QComboBox()
    combo.addItems(["a", "b"])
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    assert (
        combo.style().styleHint(QStyle.StyleHint.SH_ComboBox_Popup, option, combo) == 0
    ), "Fusion's menu-style popup is back: it aligns a row over the input and opens upwards"
    assert current_tokens()["BG_LIST"]  # the sheet is really in force for this app fixture
    combo.deleteLater()


def test_combo_popup_opens_below_the_field_for_every_current_row(app, shaper):
    """The panel must stay below the field no matter which row the field currently holds.

    The menu-style popup aligned the selected row with the field: with the last row selected it
    opened 50px *above* the combo and covered the category chips. A listbox popup is always below
    while the screen has room; its 1px top border shares the field's bottom border.
    """
    window, combo = _combo_window(app, items=3)
    for row in (0, 1, 2):
        combo.setCurrentIndex(row)
        popup = _open(combo, app)
        combo_bottom = combo.mapToGlobal(QPoint(0, combo.height())).y()
        popup_top = popup.mapToGlobal(QPoint(0, 0)).y()
        assert popup_top >= combo_bottom, (
            f"row {row}: popup top {popup_top} is above the field bottom {combo_bottom}: the "
            "dropdown covers the field"
        )
        assert popup_top <= combo_bottom + 4, (
            f"row {row}: popup top {popup_top} is {popup_top - combo_bottom}px below the field "
            f"bottom {combo_bottom}; the panel must sit flush under it"
        )
        combo.hidePopup()
        app.processEvents()
    window.close()


def test_combo_popup_paints_the_panel_fill_and_only_a_soft_selected_row(app, shaper):
    """Pixels: BG_LIST panel, the derived ACCENT_SEL_ROW on the current row only, legible text."""
    from gui.theme import contrast_ratio, current_tokens

    tokens = current_tokens()
    window, combo = _combo_window(app, items=3)
    combo.setCurrentIndex(1)
    popup = _open(combo, app)
    image = _render(popup)
    counts = _pixel_counts(image)
    opaque = {colour: count for colour, count in counts.items() if colour[3] == 255}
    dominant = max(opaque, key=opaque.get)
    bgl = tokens["BG_LIST"].lstrip("#")
    expected = (int(bgl[0:2], 16), int(bgl[2:4], 16), int(bgl[4:6], 16), 255)
    assert dominant == expected, (
        f"popup background is #{dominant[0]:02x}{dominant[1]:02x}{dominant[2]:02x}; the panel "
        f"must be BG_LIST #{tokens['BG_LIST']}"
    )

    row_hex = tokens["ACCENT_SEL_ROW"].lstrip("#")
    row_rgb = (int(row_hex[0:2], 16), int(row_hex[2:4], 16), int(row_hex[4:6], 16), 255)
    assert row_rgb in counts, "the current row is not painted with ACCENT_SEL_ROW"
    highlight_pixels = counts[row_rgb]
    assert highlight_pixels < image.width() * image.height() * 0.5, (
        "more than half the popup is highlighted: this is the old solid-block look again"
    )

    # Dark derives the row colour from ACCENT_SEL; the raw accent must not appear anywhere.
    if tokens["ACCENT_SEL"] != tokens["ACCENT_SEL_ROW"]:
        accent = tokens["ACCENT_SEL"].lstrip("#")
        accent_rgb = (int(accent[0:2], 16), int(accent[2:4], 16), int(accent[4:6], 16))
        assert not any(colour[:3] == accent_rgb for colour in counts), (
            "the saturated ACCENT_SEL is painted in the popup: the dark row must use the blend"
        )
    # The row must be legible and visibly (but quietly) distinct from the panel.
    assert contrast_ratio(tokens["FG_TEXT"], tokens["ACCENT_SEL_ROW"]) >= 4.5
    assert contrast_ratio(tokens["ACCENT_SEL_ROW"], tokens["BG_LIST"]) > 1.2
    assert combo.view().objectName() == shaper.POPUP_LIST_OBJECT_NAME
    combo.hidePopup()
    window.close()


def test_arrow_keys_move_the_soft_highlight(app, shaper):
    """Keyboard feedback survives the switch from a solid accent row to a subtle one."""
    window, combo = _combo_window(app, items=3)
    _open(combo, app)
    view = combo.view()
    assert [index.row() for index in view.selectionModel().selectedIndexes()] == [0]
    QTest.keyClick(view, Qt.Key.Key_Down)
    app.processEvents()
    assert [index.row() for index in view.selectionModel().selectedIndexes()] == [1]
    QTest.keyClick(view, Qt.Key.Key_Up)
    app.processEvents()
    assert [index.row() for index in view.selectionModel().selectedIndexes()] == [0]
    combo.hidePopup()
    window.close()


def test_resources_page_source_popup_opens_below_the_field(app, shaper, monkeypatch):
    """The real page, not a synthetic combo: the source dropdown must not cover its own field.

    This is the user's report: after choosing CurseForge the field disappeared and the list started
    at the top of the page. The regression test measures the two global rectangles and the distance
    from the field's bottom to the popup's top on the page that showed it.
    """
    from gui import i18n
    from gui.pages import mods_page

    monkeypatch.setattr(mods_page, "_curseforge_available", lambda: True)
    monkeypatch.setattr(mods_page, "search_projects", lambda *a, **k: [])
    monkeypatch.setattr(mods_page, "run_in_background", lambda *a, **k: None)

    page = mods_page.ResourcesPage()
    window = QWidget()
    window.resize(1000, 610)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(page)
    window.show()
    app.processEvents()
    try:
        combo = page.source_combo
        assert [combo.itemText(i) for i in range(combo.count())] == [
            i18n.tr("mods.source.all"),
            i18n.tr("mods.source.curseforge"),
            i18n.tr("mods.source.modrinth"),
        ]
        assert combo.currentData() == "all"
        combo.setCurrentIndex(combo.findData("curseforge"))
        popup = _open(combo, app)
        combo_rect = QRect(combo.mapToGlobal(QPoint(0, 0)), combo.size())
        popup_rect = QRect(popup.mapToGlobal(QPoint(0, 0)), popup.size())
        assert not combo_rect.intersects(popup_rect), (
            "the popup overlaps the field: the value the user selected is covered"
        )
        assert popup_rect.top() >= combo_rect.bottom() + 1 - 2, (
            f"popup top {popup_rect.top()} vs field bottom {combo_rect.bottom()}"
        )
        assert abs(popup_rect.left() - combo_rect.left()) <= 2, "popup left edge is not aligned"
        assert abs(popup_rect.width() - combo_rect.width()) <= 2, "popup width does not match"
        window_bottom = window.mapToGlobal(QPoint(0, window.height())).y()
        assert popup_rect.height() <= window_bottom - combo_rect.bottom(), (
            "the popup is taller than the space below the field in this window"
        )
        combo.hidePopup()
    finally:
        page.deleteLater()
        window.close()
        app.processEvents()


def test_resources_page_sort_popup_is_opaque_and_leaves_the_chips_alone(
    app, shaper, monkeypatch
):
    """The reported screenshot: opening "sort" made 资源包/光影 disappear behind a see-through panel.

    Offscreen cannot reproduce the native compositing failure, but it can pin the facts the fix
    depends on: the panel is fully opaque, it opens *below* its field (so it cannot cover the chip
    row), and the four category chips keep their visibility and geometry while it is open.
    """
    from gui import i18n
    from gui.pages import mods_page

    monkeypatch.setattr(mods_page, "_curseforge_available", lambda: True)
    monkeypatch.setattr(mods_page, "search_projects", lambda *a, **k: [])
    monkeypatch.setattr(mods_page, "run_in_background", lambda *a, **k: None)

    page = mods_page.ResourcesPage()
    window = QWidget()
    window.resize(1000, 610)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(page)
    window.show()
    app.processEvents()
    try:
        assert len(page.chips) == 4
        assert [chip.text() for chip in page.chips] == [
            i18n.tr("mods.tab.mods"),
            i18n.tr("mods.tab.modpacks"),
            i18n.tr("mods.tab.resourcepacks"),
            i18n.tr("mods.tab.shaderpacks"),
        ]
        before = {chip.text(): chip.geometry() for chip in page.chips}

        combo = page.mods_tab.sort_combo
        popup = _open(combo, app)
        assert popup.isVisible()
        assert not popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # The chip row must survive the dropdown: still four buttons, all visible, all in place.
        chips = page.findChildren(QPushButton, "resourceChip")
        assert len(chips) == 4
        for chip in chips:
            assert chip.isVisible(), f"category chip {chip.text()!r} disappeared"
            assert chip.geometry() == before[chip.text()], (
                f"category chip {chip.text()!r} moved while the sort dropdown was open"
            )

        # And the panel itself is below the field, not over it or over the chips.
        combo_rect = QRect(combo.mapToGlobal(QPoint(0, 0)), combo.size())
        popup_rect = QRect(popup.mapToGlobal(QPoint(0, 0)), popup.size())
        assert not combo_rect.intersects(popup_rect)
        assert popup_rect.top() >= combo_rect.bottom() + 1 - 2
        assert popup_rect.width() >= combo_rect.width()

        image = _render(popup)
        min_alpha = min(
            image.pixelColor(x, y).alpha()
            for y in range(image.height())
            for x in range(image.width())
        )
        assert min_alpha == 255, f"the sort popup is not opaque: min alpha {min_alpha}"
        combo.hidePopup()
    finally:
        page.deleteLater()
        window.close()
        app.processEvents()


def test_combo_popup_stylesheet_is_an_outlined_panel():
    """Both sheets: the list paints BG_LIST inside the container's outline, square corners."""
    for sheet in SHEETS:
        text = sheet.read_text(encoding="utf-8")
        start = text.index("QComboBox QAbstractItemView,")
        block = text[start : text.index("}", start)]
        assert "QListView#comboPopupList" in block, sheet.name
        assert "background: __BG_LIST__;" in block, sheet.name
        # The container (gui/popup_shape.py) owns the 1px BORDER_LIST outline now. A second border
        # on the list would paint a 2px edge, so the list rule must not carry one.
        assert "border: none;" in block, sheet.name
        assert "__BORDER_LIST__" not in block, sheet.name
        assert "border-radius: 0;" in block, sheet.name
        assert "padding: 0;" in block, sheet.name
        assert "__ACCENT_SEL__" not in block, sheet.name
        # The selected pseudo-state must live in the same sheet; otherwise the delegate paints
        # every row in the panel fill and the current entry is invisible.
        selected = text[text.index("QListView#comboPopupList::item:selected") :]
        selected_block = selected[: selected.index("}", 0)]
        assert "background: __ACCENT_SEL_ROW__;" in selected_block, sheet.name


def test_create_app_installs_the_popup_hooks():
    """Both hooks must live in the application bootstrap, not in one page's combo."""
    source = (ROOT / "gui/main.py").read_text(encoding="utf-8")
    assert "from gui import popup_shape" in source
    assert "popup_shape.install(app)" in source
    assert "popup_shape.install_combo_popup_style(app)" in source
