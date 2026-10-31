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

"""The visual system's fixed values, checked where they are cheap to check.

The rendered evidence lives in tools/audit_card_radius.py, tools/audit_ui_quality.py and the
screenshots; this file pins the numbers those tools assume: window/card 8px, control 6px, the
16px page top inset, and the dark sheet carrying the same structural blocks as the light one.
It reads the shipped sheets as text, so a rule that silently disappears is caught even when no
page happens to render that widget in the current preview data.
"""

from __future__ import annotations

from pathlib import Path

from gui import theme
from gui.widgets import PAGE_TOP_INSET

ROOT = Path(__file__).resolve().parent.parent
LIGHT = (ROOT / "gui/resources/style_light.qss").read_text(encoding="utf-8")
DARK = (ROOT / "gui/resources/style.qss").read_text(encoding="utf-8")


def test_default_radius_scale_is_card_8_and_control_6():
    extra_small, small, large = theme.radius_scale("default")
    assert (extra_small, small, large) == (4, 6, 8)
    # cards take LG, inputs/buttons take SM
    assert "QFrame#pageCard" in LIGHT and "border-radius: __RADIUS_LG__;" in LIGHT
    assert "QWidget#settingsPage QWidget#settingsCard" in LIGHT
    # The 6px control radius must come from the SM token, not a literal: the preset has to reach it.
    assert "QWidget#settingsPage QLineEdit," in LIGHT and "border-radius: __RADIUS_SM__;" in LIGHT


def test_page_top_inset_is_16px():
    assert PAGE_TOP_INSET == 16


def test_topbar_is_one_blue_in_both_themes_and_white_text_passes_aa():
    light = theme.NEUTRAL_TOKENS["BG_TOPBAR"]["light"]
    dark = theme.NEUTRAL_TOKENS["BG_TOPBAR"]["dark"]
    assert light == dark == "#1e6fd9"
    assert theme.NEUTRAL_TOKENS["FG_TOPBAR"] == {"dark": "#ffffff", "light": "#ffffff"}
    assert theme.contrast_ratio("#ffffff", "#1e6fd9") >= 4.5


def test_dark_sheet_has_the_same_structural_blocks_as_light():
    """Only structure: each selector below resolves through the dark palette, not light values."""
    structural = (
        "QWidget#settingsPage QWidget#settingsCard",
        "QWidget#settingsPage QLineEdit,",
        "QWidget#settingsPage QGroupBox",
        "QWidget#aboutPage QGroupBox",
        "QWidget#launchPage QWidget#launchPanel",
        "QWidget#navRail",
        "QStackedWidget#pageStack",
    )
    missing = [selector for selector in structural if selector not in DARK]
    assert not missing, f"dark sheet lost the light structure: {missing}"
    # 32px field surfaces: the two frame allowances are stated in both sheets.
    for sheet in (LIGHT, DARK):
        assert "min-height: 30px;" in sheet
        assert "min-height: 28px;" in sheet
