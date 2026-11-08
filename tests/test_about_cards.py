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

"""The About page's section titles belong to their cards, not to a QGroupBox frame.

Qt paints ``QGroupBox::title`` on the frame, so "软件信息" / "软件更新" used to float just above the
card border and read as page headings. The page now builds plain ``QFrame#pageCard`` cards - the same
card every other page uses - with a 15/600 ``QLabel#sectionTitle`` inside, 8px above the body and 16px
from every card edge. These tests pin the structure and the numbers, and prove the page still scrolls
now that its cards are taller.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QFrame, QGroupBox, QLabel, QScrollArea

ROOT = Path(__file__).resolve().parents[1]
SHEETS = (ROOT / "gui/resources/style.qss", ROOT / "gui/resources/style_light.qss")


@pytest.fixture()
def app():
    application = QApplication.instance() or QApplication([])
    from gui.theme import apply_theme

    apply_theme("dark", None, None, "default")
    return application


@pytest.fixture()
def page(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    from gui.pages.about_page import AboutPage

    widget = AboutPage()
    widget.resize(1000, 610)
    widget.show()
    for _ in range(6):
        app.processEvents()
    try:
        yield widget
    finally:
        widget.close()


def _cards(page) -> list[QFrame]:
    cards = page.findChildren(QFrame, "pageCard")
    assert len(cards) == 3, f"expected the three about cards, found {len(cards)}"
    return cards


def test_about_page_has_no_group_boxes_and_three_titled_cards(page):
    from gui import i18n

    assert page.findChildren(QGroupBox) == [], (
        "the About page still builds a QGroupBox: its title would be painted on the frame again"
    )
    # The group box used to supply the old dropdown's accessible name; the card does not, so the
    # page has to name each field itself (WCAG 4.1.2).
    assert page.auto_check.accessibleName() == i18n.tr("about.update.auto_check")
    assert page.auto_download.accessibleName() == i18n.tr("about.update.auto_download")
    cards = _cards(page)
    expected = [
        i18n.tr("about.info.title"),
        i18n.tr("about.update.title"),
        i18n.tr("about.credits.title"),
    ]
    titles = []
    for card in cards:
        title = card.findChild(QLabel, "sectionTitle")
        assert title is not None, "a card has no in-card title label"
        assert title.parentWidget() is card, "the title is not a child of the card"
        assert card.layout().indexOf(title) == 0, "the title is not the card's first item"
        assert title.font().pixelSize() == 15, f"title is {title.font().pixelSize()}px, expected 15px"
        assert title.font().weight() == 600, f"title weight is {title.font().weight()}, expected 600"
        titles.append(title.text())
    assert titles == expected


def test_about_cards_have_16px_inner_padding_and_an_8px_title_gap(page):
    for card in _cards(page):
        margins = card.layout().contentsMargins()
        assert margins.left() >= 16, f"left padding is {margins.left()}px"
        assert margins.right() >= 16, f"right padding is {margins.right()}px"
        assert margins.top() >= 16, f"top padding is {margins.top()}px"
        assert margins.bottom() >= 16, f"bottom padding is {margins.bottom()}px"
        assert card.layout().spacing() == 8, "the title-to-body step must be 8px"

        title = card.layout().itemAt(0).widget()
        body = card.layout().itemAt(1).widget()
        assert isinstance(title, QLabel) and title.objectName() == "sectionTitle"
        assert body is not None
        # Both geometries are relative to the card, so this is the exact visible gap, not a guess.
        assert body.geometry().top() - (title.geometry().top() + title.geometry().height()) == 8
        assert body.geometry().left() >= 16
        assert card.width() - (body.geometry().left() + body.geometry().width()) >= 16


def test_about_page_scrolls_after_the_cards_grow(page, app):
    scroll = page.findChild(QScrollArea)
    assert scroll is not None
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0, "the taller About page does not scroll"
    bar.setValue(bar.maximum())
    app.processEvents()
    assert bar.value() == bar.maximum()
    assert scroll.widget().height() > scroll.viewport().height()


def test_about_stylesheets_no_longer_target_a_group_box_title():
    for sheet in SHEETS:
        text = sheet.read_text(encoding="utf-8")
        assert "QWidget#aboutPage QGroupBox" not in text, sheet.name
        assert "QWidget#aboutPage QLabel#hint" in text, sheet.name
        assert "QWidget#aboutPage QLabel#aboutLink" in text, sheet.name
        assert "QLabel#sectionTitle { font-size: 15px; font-weight: 600;" in text, sheet.name
