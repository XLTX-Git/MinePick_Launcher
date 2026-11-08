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

"""The detail page's way back leads where the reader came from.

The launch page's 实例设置 and the card grid both drill into the same detail page. Before this the
back control always said 返回实例列表 and always landed on the grid, which loses the reader who started
on the launch page. These tests pin both directions, and pin that the label and the accessible name
follow the same origin - a control that says one thing and does another is worse than either.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

# The suite's own helpers live in a sibling test module. Tests are not a package, so the directory
# has to be on the path before the import can see it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_instance_navigation import _settle, _window


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No network and no Java probe outliving the window, same as the other GUI suites."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


VERSION_ID = "fabric-loader-0.19.3-1.21.11"


def test_the_launch_page_origin_says_so_and_goes_back_there(app, monkeypatch, ws_tmp):
    """Opened from 实例设置: the label names the launch page, and the back control lands on it."""
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp, dont_show=False)
        page = window.pages["instances"]

        window._goto_instance_settings(VERSION_ID)
        _settle(app, window)

        back = page.detail_page.back_button
        assert "启动" in back.text(), f"the back control still reads {back.text()!r}"
        assert "启动" in back.accessibleName(), "the accessible name disagrees with the visible label"

        QTest.mouseClick(back, Qt.MouseButton.LeftButton)
        _settle(app, window)

        assert window.stack.currentWidget() is window.pages["launch"], (
            "back from a launch-page drill-down did not return to the launch page"
        )
        assert window.nav_rail.isVisible(), "the main rail stayed hidden after returning"
    finally:
        if window is not None:
            window.close()


def test_the_grid_origin_still_says_instances_and_goes_back_to_the_grid(app, monkeypatch, ws_tmp):
    """Opened from a card: unchanged behaviour, so the new route did not hijack the old one."""
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp, dont_show=False)
        page = window.pages["instances"]

        window._goto_instance(VERSION_ID)
        _settle(app, window)

        back = page.detail_page.back_button
        assert "实例" in back.text(), f"the back control reads {back.text()!r}"
        assert "实例" in back.accessibleName()

        QTest.mouseClick(back, Qt.MouseButton.LeftButton)
        _settle(app, window)

        assert page.current_view() is page.view_stack.widget(0), (
            "back from a grid drill-down did not return to the card grid"
        )
    finally:
        if window is not None:
            window.close()


def test_an_unknown_instance_does_not_record_an_origin(app, monkeypatch, ws_tmp):
    """A failed drill-down must not leave a stale origin behind for the next one."""
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp, dont_show=False)
        page = window.pages["instances"]

        window._goto_instance_settings(VERSION_ID)
        _settle(app, window)
        assert page.open_instance("no-such-instance", origin="list") is False

        # The refusal changed nothing: the pending origin is still the launch page.
        assert getattr(page, "_detail_origin", "list") == "launch"
        assert "启动" in page.detail_page.back_button.text()
    finally:
        if window is not None:
            window.close()
