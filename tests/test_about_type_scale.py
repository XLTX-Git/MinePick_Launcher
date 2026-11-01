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

"""The About page's type scale: a section title must read as a title.

The report said the four section titles looked the same size as the body under them. They are
15px/600 against body text at 12-13px, and this file pins that difference so a later stylesheet edit
cannot flatten it back - plus the 8px step each title keeps to the content it introduces (the card's
own title through the layout's spacing, a legal block through its spacer).
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from gui import i18n


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    from gui.theme import apply_theme

    apply_theme("dark", None, None, "default")
    yield application


@pytest.fixture(autouse=True)
def _zh(app):
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


def _next_widget(layout, start: int):
    for index in range(start + 1, layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is not None:
            return widget
    return None


def test_about_section_titles_stay_bigger_than_their_body(app, monkeypatch, ws_tmp):
    from gui.pages.about_page import TITLE_BODY_GAP, AboutPage

    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    page = AboutPage()
    page.resize(1000, 610)
    page.show()
    try:
        for _ in range(8):
            app.processEvents()
        titles = [label for label in page.findChildren(QLabel) if label.objectName() == "sectionTitle"]
        bodies = [
            label
            for label in page.findChildren(QLabel)
            if label.objectName() == "hint" and label.text() and label.isVisible()
        ]
        assert len(titles) >= 4, "the four section titles are gone"
        assert bodies, "no body text to compare against"
        for title in titles:
            assert title.font().pixelSize() == 15, f"{title.text()!r} is {title.font().pixelSize()}px"
            assert title.font().weight() >= 600, f"{title.text()!r} is {title.font().weight()}"
        for body in bodies:
            size = body.font().pixelSize()
            assert size <= 13, f"{body.text()[:20]!r} is {size}px, expected 13 or less"
            assert titles[0].font().pixelSize() - size >= 2, "the title is not distinguishable"

        # The four titles the report named, each 8px above its content.
        by_text = {title.text(): title for title in titles}
        checked = 0
        for key in (
            "about.info.title",
            "about.legal.title",
            "about.upstream.title",
            "about.licenses.title",
        ):
            text = i18n.tr(key)
            assert text in by_text, f"{text!r} is missing from the About page"
            title = by_text[text]
            layout = title.parentWidget().layout()
            body = _next_widget(layout, layout.indexOf(title))
            assert body is not None, f"{text!r} has nothing under it"
            gap = body.geometry().top() - (title.geometry().top() + title.geometry().height())
            assert gap == TITLE_BODY_GAP, f"{text!r} sits {gap}px above its body"
            checked += 1
        assert checked == 4
    finally:
        page.close()
        page.deleteLater()
