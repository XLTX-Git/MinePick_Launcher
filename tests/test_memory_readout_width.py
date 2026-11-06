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

"""The settings memory read-out: the digits change, the row's width does not."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def test_memory_value_label_width_does_not_follow_the_number(app, monkeypatch, ws_tmp):
    """A text read-out used to widen the form as it grew from "4.0" to "12.0"."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "settings"))
    monkeypatch.setattr("launcher.memory.system_memory_gb", lambda: (16.0, 12.0))
    monkeypatch.setattr("launcher.java.locate.selected_java_bits", lambda *a, **k: 64)
    from gui.pages.settings_page import SettingsPage

    page = SettingsPage()
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    page.resize(1000, 700)
    page.show()
    try:
        for _ in range(5):
            app.processEvents()
        label = page.memory_value_label
        assert label.alignment() & Qt.AlignmentFlag.AlignRight
        reserved = label.minimumWidth()
        assert reserved > 0

        widths = []
        minimums = []
        for value in (1.0, 4.0, 9.5, 12.0, 32.0):
            page._memory_value = float(value)
            page._update_memory_labels()
            page.adjustSize()
            for _ in range(3):
                app.processEvents()
            widths.append(label.width())
            minimums.append(label.minimumWidth())
        assert len(set(widths)) == 1, widths
        assert len(set(minimums)) == 1, minimums
        assert widths[0] == reserved
        assert label.width() == label.minimumWidth()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
