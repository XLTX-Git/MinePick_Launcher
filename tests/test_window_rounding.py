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

"""The DWM rounding call is best-effort: it must never break startup, and must run once.

The actual compositor effect cannot be proven offscreen (see tools/verify_window_rounding.py,
which uses a real native window and reads the preference back). These tests pin the safety
contract around it: no dwmapi, a raising dwmapi, and a readback failure all degrade to a
square window instead of an exception - and MainWindow asks for the preference on first show.
"""

from __future__ import annotations

import types

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui import window_effects


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def test_missing_dwm_is_a_silent_no(monkeypatch):
    monkeypatch.setattr(window_effects, "_dwm", lambda: None)
    window = types.SimpleNamespace(winId=lambda: 1)
    assert window_effects.apply_rounded_corners(window) is False
    assert window_effects.corner_preference(window) is None


def test_a_raising_dwm_is_silent(monkeypatch):
    class _Broken:
        def DwmSetWindowAttribute(self, *_args):
            raise OSError("dwmapi call failed")

        def DwmGetWindowAttribute(self, *_args):
            raise OSError("dwmapi call failed")

    monkeypatch.setattr(window_effects, "_dwm", lambda: _Broken())
    window = types.SimpleNamespace(winId=lambda: 1)
    assert window_effects.apply_rounded_corners(window) is False
    assert window_effects.corner_preference(window) is None


def test_a_missing_native_handle_is_silent(monkeypatch):
    monkeypatch.setattr(window_effects, "_dwm", lambda: types.SimpleNamespace())
    window = types.SimpleNamespace(winId=lambda: 0)
    assert window_effects.apply_rounded_corners(window) is False
    assert window_effects.corner_preference(window) is None


def test_main_window_asks_for_rounding_on_first_show(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_rounding"))
    from gui import main_window

    calls = []

    def _record(window) -> bool:
        calls.append(window)
        return True

    monkeypatch.setattr(main_window.window_effects, "apply_rounded_corners", _record)
    window = main_window.MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        window.show()
        app.processEvents()
        assert calls == [window], "the first show must ask DWM for the rounded corners once"
        window.hide()
        window.show()
        app.processEvents()
        assert calls == [window], "a second show must not repeat the one-shot attribute"
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
