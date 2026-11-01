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

"""The window's product floor is a minimum, not a fixed size.

The layout used to decide the floor alone; after the instance grid became one column it dropped to
746px (zh_cn) / 870px (es_es) and the window could be dragged uncomfortably narrow. 1000px is now a
deliberate lower bound on top of that layout requirement. The distinction matters: ``setFixedWidth``
would also stop the user from making the window wider, which this test rules out.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.main_window import MainWindow


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def window(app, ws_tmp, monkeypatch):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "floor"))
    i18n.set_language("zh_cn")
    built = MainWindow()
    built.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    yield built
    built.close()
    built.deleteLater()
    app.processEvents()


def test_minimum_width_floor_is_1000(window, app):
    """The floor is at least 1000 even before a layout pass."""
    assert window.minimumWidth() >= 1000


def test_the_floor_is_not_a_fixed_width(window, app):
    """A fixed width would set minimum == maximum; the user must still be able to widen the window."""
    assert window.maximumWidth() > window.minimumWidth()


def test_a_shown_window_cannot_be_dragged_below_the_floor(window, app):
    """Resizing to 700 is clamped to the floor, so the layout is never painted into a narrower strip."""
    window.resize(700, 600)
    window.show()
    for _ in range(8):
        app.processEvents()
    assert window.width() >= 1000
