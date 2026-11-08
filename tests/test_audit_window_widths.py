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

"""The width gate needs a constructive negative: a page that *is* clipped must be reported.

Its first run on this tree reports clipping 0 across 27 screens, so a gate that only ever passes
would be indistinguishable from a gate that never looks. If a wide child is put on a page, the audit
has to say 裁切; a page that fits has to say OK; and elision has to be listed *without* failing, with
the whitelist annotating rather than hiding it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import audit_window_widths as audit


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def _fake_page(app, *, wide_child: bool, long_text: bool = False) -> QWidget:
    page = QWidget()
    page.resize(400, 300)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.addLayout(QHBoxLayout())
    if wide_child:
        # 380px into a 400px page: the right edge is 100px past the page, i.e. unreachable.
        child = QLabel("sticking out", page)
        child.setGeometry(380, 10, 100, 20)
    if long_text:
        label = QLabel("a sentence that cannot fit the box it was given", page)
        label.setGeometry(10, 60, 80, 20)
    page.show()
    _pump(app)
    return page


def test_gate_reports_a_deliberately_clipped_page(app):
    """The negative case: a page that overflows its own rectangle is reported as 裁切."""
    page = _fake_page(app, wide_child=True)
    try:
        report = audit.inspect_screen(page, "fake")
        assert not report.ok, report.line()
        clipped = [clip.widget for clip in report.clipped]
        assert any("QLabel" in name for name in clipped), clipped
        assert "裁切" in report.line()
    finally:
        page.close()
        page.deleteLater()


def test_gate_passes_a_page_that_fits(app):
    """The positive case: nothing clipped means OK, so the report above is not always-red."""
    page = _fake_page(app, wide_child=False)
    try:
        report = audit.inspect_screen(page, "fake")
        assert report.ok, report.line()
        assert report.line() == "OK"
    finally:
        page.close()
        page.deleteLater()


def test_gate_lists_elision_without_failing(app):
    """Elision is reported with its text but is not a failure; the whitelist only annotates."""
    page = _fake_page(app, wide_child=False, long_text=True)
    try:
        report = audit.inspect_screen(page, "fake")
        assert report.ok, "elision must not fail the run"
        texts = [item.text for item in report.elided]
        assert any("cannot fit" in text for text in texts), texts
        assert "省略" in report.line()
        audit.set_whitelist(({"screen": "fake", "text_contains": "cannot fit"},))
        try:
            accepted = audit.inspect_screen(page, "fake")
            assert accepted.elided and accepted.elided[0].accepted
            assert "(whitelist)" in accepted.line()
        finally:
            audit.set_whitelist()
    finally:
        page.close()
        page.deleteLater()


def test_gate_ships_the_four_widths_and_ten_screens():
    """The gate's own scope: four widths, the nine pages and the instance detail view."""
    assert audit.DEFAULT_WIDTHS == (1000, 1050, 1400, 1800)
    assert len(audit.SCREENS) == 10
    assert "instances/detail" in audit.SCREENS
    assert set(audit.SCREENS) >= {
        "launch",
        "instances",
        "versions",
        "java",
        "account",
        "mods",
        "tools",
        "settings",
        "about",
    }
