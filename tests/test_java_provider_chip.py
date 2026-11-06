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

"""The Java runtime row's provider chip never cuts its own label.

The chip was sized from its own label plus 12px of padding and then elided against
``int(chip_width) - 12`` - the padding subtracted twice, with ``int()`` throwing away the fraction.
A label 25.6px wide got a 25px window and came out as "PA..." for PATH. The chip is now measured once
from the longest provider name, and the padding is only ever added.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication

from gui import i18n


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh(app):
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


def test_provider_chip_width_fits_every_label(app):
    """Every provider label, not just PATH, fits inside the chip paintEvent draws."""
    from gui.pages.java_page import (
        _PROVIDER_KEYS,
        TAG_PADDING_X,
        _JavaRow,
        provider_chip_width,
        provider_label,
    )

    row = _JavaRow(21, "Java 21", provider_label("path"), r"C:\Program Files\Java\bin\javaw.exe")
    try:
        _title_font, tag_font, _meta_font = row._row_fonts()
        metrics = QFontMetrics(tag_font)
        chip = provider_chip_width(tag_font)
        for token in _PROVIDER_KEYS:
            label = provider_label(token)
            assert chip >= metrics.horizontalAdvance(label) + TAG_PADDING_X, (
                f"{token!r} ({label!r}) needs {metrics.horizontalAdvance(label)}px, chip is {chip}px"
            )
            assert metrics.elidedText(label, Qt.TextElideMode.ElideRight, chip) == label
    finally:
        row.deleteLater()


def test_provider_row_keeps_path_readable(app):
    """At a normal row width nothing is cut; narrowed far enough, the row reports what it cut."""
    from gui.pages.java_page import _JavaRow, provider_label

    row = _JavaRow(
        21,
        "Java 21",
        provider_label("path"),
        r"C:\Program Files\Java\jdk-21.0.9\bin\javaw.exe",
    )
    try:
        row.resize(600, _JavaRow.HEIGHT)
        assert row.elided_texts() == [], "a 600px row cut something it has room for"
        # The hook itself is not a no-op: a row narrower than its chip has to report the chip.
        row.resize(90, _JavaRow.HEIGHT)
        assert row.elided_texts(), "a row too narrow for its chip reported nothing"
    finally:
        row.deleteLater()
