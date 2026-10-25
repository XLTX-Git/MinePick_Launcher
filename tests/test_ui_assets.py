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

"""The checkbox tick must ship its @2x/@3x redraws, and the retired arrows must stay gone.

gui/theme.py picks a bitmap by the screen's scaling, so a missing or wrongly sized @2x would silently
fall back to a stretched 1x tick - the soft, bitten edge this pair of files exists to prevent. The
combo and spin arrows were replaced by SVG chevrons / hidden sub-controls, so their PNGs are dead
weight: the second test keeps a later commit from quietly reinstating them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "gui" / "resources"
#: Glyph stems that no sheet and no code references any more (removed with their generator entries).
RETIRED_GLYPHS = ("down_arrow", "down_arrow_light", "spin_up", "spin_up_light", "spin_down", "spin_down_light")


def test_generated_glyphs_are_present_at_exact_multiples():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_ui_assets.py"), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all present at the exact size" in proc.stdout, proc.stdout


def test_retired_glyphs_are_not_shipped():
    leftovers = sorted(
        path.name
        for stem in RETIRED_GLYPHS
        for path in (RESOURCES / f"{stem}.png", RESOURCES / f"{stem}@2x.png", RESOURCES / f"{stem}@3x.png")
        if path.exists()
    )
    assert not leftovers, f"retired arrow glyphs came back: {leftovers}"
