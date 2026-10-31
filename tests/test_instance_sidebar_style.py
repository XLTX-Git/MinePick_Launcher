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

"""The instance detail sidebar's selected state: a left accent bar, not a rounded pill.

These are source-level gates on both stylesheets. The visual result is measured separately in the
screenshots; what this file pins down is the *recipe* - a future edit that brings the full
``__ACCENT_SOFT__`` pill back, or drops the 3px bar, or goes back to the near-black selected-text
token, has to fail here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SHEETS = ("style.qss", "style_light.qss")
RESOURCES = Path(__file__).resolve().parent.parent / "gui/resources"


def _rule_body(sheet: str, selector: str) -> str:
    """The declaration block of one exact selector, comments removed."""
    text = (RESOURCES / sheet).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", text, re.DOTALL)
    assert match is not None, f"{sheet}: no rule for {selector!r}"
    return match.group(1)


@pytest.mark.parametrize("sheet", SHEETS)
def test_detail_selection_is_a_left_bar_not_a_pill(sheet: str):
    """3px accent bar + accent text + a very pale accent wash, on a square row."""
    base = _rule_body(sheet, "QPushButton#instanceNavItem")
    checked = _rule_body(sheet, "QPushButton#instanceNavItem:checked")
    active_label = _rule_body(sheet, 'QLabel#instanceNavLabel[active="true"]')
    active_badge = _rule_body(sheet, 'QLabel#instanceNavBadge[active="true"]')

    # Square row, with the 3px accent bar as the selection cue.
    assert "border-radius: 0" in base
    assert "border-left: 3px solid transparent" in base
    assert "border-left: 3px solid __ACCENT__" in checked
    assert "border-radius" not in checked
    # A very pale accent wash - the token one step paler than the old ACCENT_SOFT pill.
    assert "background: __ACCENT_TINT__" in checked
    assert "__ACCENT_SOFT__" not in checked
    # Primary-coloured text, not the near-black "selected text" token.
    assert "color: __ACCENT_TEXT__" in active_label
    assert "color: __ACCENT_SOFT__" not in active_label
    # The badge keeps a visible chip on the now-pale row.
    assert "background: __ACCENT_SOFT__" in active_badge
    assert "color: __ACCENT_TEXT__" in active_badge
