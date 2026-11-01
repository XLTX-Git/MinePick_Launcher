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

"""The icon checks behind ``shot_pages.py --warm-cache --expect-icons``.

Two behaviours matter and are easy to get subtly wrong:

* ``wait_until`` has to pump the Qt event loop (a worker's result arrives through a queued
  connection) and still stop at its deadline instead of waiting forever;
* ``--expect-icons`` must fail when a row's ``_icon`` is still None - the cache file existing is not
  proof, because a failed decode leaves the row painting its letter tile.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import shot_pages


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


class _Row:
    """The two attributes the icon helpers read from a real ``_ResourceRow``."""

    def __init__(self, title: str, icon: object | None = None) -> None:
        self.title = title
        self.icon_url = "https://cdn.example.test/icon.png"
        self._icon = icon


def test_wait_until_returns_true_as_soon_as_the_predicate_holds(app):
    state = {"done": False}
    QTimer.singleShot(50, lambda: state.__setitem__("done", True))
    assert shot_pages.wait_until(lambda: state["done"], timeout_ms=2000) is True


def test_wait_until_stops_at_its_deadline(app):
    """A false predicate costs the timeout, not the default eight seconds."""
    started = time.monotonic()
    assert shot_pages.wait_until(lambda: False, timeout_ms=100) is False
    assert time.monotonic() - started < 1.5


def test_icons_ready_is_false_while_a_row_paints_its_letter_tile(monkeypatch):
    rows = [_Row("Sodium", icon=None), _Row("Iris", icon=object())]
    monkeypatch.setattr(shot_pages, "resource_rows", lambda page: rows)
    assert shot_pages.icons_ready(object()) is False
    assert [row.title for row in shot_pages.missing_icons(object())] == ["Sodium"]


def test_expect_icons_refuses_to_save_when_an_icon_did_not_load(monkeypatch):
    rows = [_Row("Sodium", icon=None), _Row("Iris", icon=object())]
    monkeypatch.setattr(shot_pages, "resource_rows", lambda page: rows)
    with pytest.raises(SystemExit, match="did not load"):
        shot_pages.assert_icons_loaded(object(), "page 'mods'")


def test_expect_icons_accepts_a_page_whose_rows_all_have_icons(monkeypatch):
    rows = [_Row("Sodium", icon=object()), _Row("Iris", icon=object())]
    monkeypatch.setattr(shot_pages, "resource_rows", lambda page: rows)
    shot_pages.assert_icons_loaded(object(), "page 'mods'")  # must not raise
