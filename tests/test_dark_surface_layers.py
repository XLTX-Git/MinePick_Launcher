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

"""Dark theme: three cool-blue surface levels, each a measurable step from the one below it.

Dark used to paint the window, the page paper and the rail in one value (#0f141b), with the card
almost on top of them (#161d27, 1.09:1): one black, no depth. The surface palette is a ladder now:

    level 1 (deepest)  BG_WINDOW = BG_NAV   #03080f   H 215  S 67%  L  3.5%
    level 2            BG_PAGE              #0a1622   H 210  S 55%  L  8.6%
    level 3 (lightest) BG_LIST              #122030   H 212  S 46%  L 12.9%

Every adjacent pair sits between 1.10:1 and 1.20:1, so the step is measurable rather than a hunch,
and nothing goes above 1.20:1 (past that the levels read as patches instead of one surface
receding).

The ladder is cool blue on purpose - hue 210-215, blue 12/24/30 levels above red - because at these
luminances an unsaturated slate reads as grey, which is what the old #0f141b did. HSL saturation
alone is a poor witness at the bottom of the range (it rises as lightness falls, so a near-black grey
can quote a high S); the tests therefore check hue and the blue-minus-red chroma together with it.

The card keeps its old order of magnitude: #122030 is 15% above #161d27 in relative luminance, and
the fills drawn on top of it (hover, section header, popup, outlined button) were left where the
previous change calibrated them rather than being re-derived. The in-window overlay card is part of
the same cool-blue family now, and still has to clear the 1.8:1 scrim floor that the overlay change
established.

Light is not part of the change and is pinned twice: the exact token values and the byte digest of
gui/resources/style_light.qss.
"""

from __future__ import annotations

import colorsys
import hashlib
import itertools
from pathlib import Path

import pytest

from gui import overlay, theme

RESOURCES = Path(__file__).resolve().parent.parent / "gui" / "resources"
LIGHT_SHEET = RESOURCES / "style_light.qss"

#: Same pin as tests/test_dark_state_contrast.py holds. Both changes are dark-only; if a later edit
#: really does touch the light sheet, both digests have to move in that commit.
LIGHT_SHEET_SHA256 = "7d1df4693408603f586b0cf5959f725e6e73a853878b25ab80ec6729f2a81549"

#: The three levels, deepest first. BG_NAV is not a fourth level: the rail shares level 1 with the
#: window chrome, which is what makes the rail recede behind the page instead of competing with it.
LEVELS = ("BG_WINDOW", "BG_PAGE", "BG_LIST")

#: The floor from the overlay change (tests/test_dark_state_contrast.py): the card has to separate
#: itself from every scrimmed backdrop it can be painted over.
CARD_FLOOR = 1.8


def _hsl(colour: str) -> tuple[float, float, float]:
    value = theme.normalize_hex(colour)
    assert value is not None, colour
    red, green, blue = (int(value[index : index + 2], 16) / 255 for index in (1, 3, 5))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return hue * 360, saturation * 100, lightness * 100


def _chroma(colour: str) -> int:
    value = theme.normalize_hex(colour)
    assert value is not None, colour
    return int(value[5:7], 16) - int(value[1:3], 16)


def _scrimmed(colour: str, severity: str) -> str:
    """`colour` under the dark scrim for one severity, composited the way paintEvent does it."""
    scrim = overlay.scrim_colour(severity, "dark")
    base = theme.normalize_hex(colour)
    assert base is not None
    alpha = scrim.alpha() / 255
    tint = (scrim.red(), scrim.green(), scrim.blue())
    return "#" + "".join(
        f"{round(int(base[index : index + 2], 16) * (1 - alpha) + tint[channel] * alpha):02x}"
        for channel, index in enumerate((1, 3, 5))
    )


def test_dark_paints_three_surface_levels():
    """The four surface tokens are three values now, and both steps are inside 1.10-1.20:1."""
    dark = theme.theme_palette("dark")
    assert dark["BG_NAV"] == dark["BG_WINDOW"], (
        f"the rail ({dark['BG_NAV']}) and the window chrome ({dark['BG_WINDOW']}) are level 1 and "
        f"have to be the same value"
    )
    values = {token: dark[token] for token in LEVELS}
    assert len(set(values.values())) == 3, (
        f"the dark surfaces still collapse into fewer than three levels: {values}"
    )
    for lower, upper in itertools.pairwise(LEVELS):
        step = theme.contrast_ratio(dark[upper], dark[lower])
        assert 1.10 <= step <= 1.20, (
            f"dark: {upper} is {step:.3f}:1 from {lower}; the ladder asks for 1.10-1.20:1, and past "
            f"1.20:1 the levels read as separate patches instead of one surface receding"
        )


@pytest.mark.parametrize("token", (*LEVELS, "BG_OVERLAY_CARD"))
def test_dark_surfaces_are_cool_blue_not_grey(token):
    """Hue 210-215 and a real blue-red gap; HSL S alone is not enough at these luminances."""
    dark = theme.theme_palette("dark")
    hue, saturation, _lightness = _hsl(dark[token])
    assert 208 <= hue <= 218, f"{token} sits at hue {hue:.1f}, outside the cool-blue band"
    assert saturation >= 28, (
        f"{token} has only {saturation:.1f}% HSL saturation; a dark surface at this lightness reads "
        f"as grey below ~28%"
    )
    assert _chroma(dark[token]) >= 12, (
        f"{token} has blue-minus-red {_chroma(dark[token])}; a dark blue needs at least 12 levels of "
        f"separation between its blue and red channels to stop reading as neutral grey"
    )


def test_the_dark_level_values_are_the_measured_ones():
    """Pin the three values and the steps, so a later edit is a decision and not a drift."""
    dark = theme.theme_palette("dark")
    assert dark["BG_WINDOW"] == "#03080f"
    assert dark["BG_NAV"] == "#03080f"
    assert dark["BG_PAGE"] == "#0a1622"
    assert dark["BG_LIST"] == "#122030"
    assert 1.10 <= theme.contrast_ratio(dark["BG_PAGE"], dark["BG_WINDOW"]) <= 1.20
    assert 1.10 <= theme.contrast_ratio(dark["BG_LIST"], dark["BG_PAGE"]) <= 1.20
    # The deepest level is near black on purpose: keeping the card at its old lightness with two
    # measurable steps leaves no room for a lighter window. The report keeps a zoomed comparison.
    assert theme.contrast_ratio(dark["BG_WINDOW"], "#000000") >= 1.03


def test_dark_text_keeps_its_floor_on_the_new_card():
    """Lighter cards are the risk this change carries: body text must still clear 7:1, hints 4.5:1."""
    dark = theme.theme_palette("dark")
    for token, floor in (("FG_TEXT", 7.0), ("FG_HINT", 4.5), ("FG_EMPTY_STATE", 4.5)):
        for surface in ("BG_LIST", "BG_PAGE"):
            ratio = theme.contrast_ratio(dark[token], dark[surface])
            assert ratio >= floor, (
                f"dark: {token} on {surface} is {ratio:.2f}:1, below the {floor:.1f}:1 floor"
            )


def test_the_overlay_card_is_cool_blue_and_still_clears_every_scrim():
    """The overlay card was grey-blue; it follows the family without losing its 1.8:1 floor."""
    dark = theme.theme_palette("dark")
    hue, saturation, _lightness = _hsl(dark["BG_OVERLAY_CARD"])
    assert 208 <= hue <= 218
    assert saturation >= 28
    assert _chroma(dark["BG_OVERLAY_CARD"]) >= 12
    for surface in ("BG_PAGE", "BG_SECTION"):  # the paper, and a page card it covers
        for severity in ("normal", "warning", "error"):
            backdrop = _scrimmed(dark[surface], severity)
            ratio = theme.contrast_ratio(dark["BG_OVERLAY_CARD"], backdrop)
            assert ratio >= CARD_FLOOR, (
                f"dark: the overlay card's fill is only {ratio:.2f}:1 from the {severity} scrim over "
                f"{surface} (floor {CARD_FLOOR:.2f}:1); the card reads as part of the backdrop"
            )


def test_the_light_column_did_not_move():
    """The light theme is not part of this change: every touched token keeps its light value."""
    light = theme.theme_palette("light")
    expected = {
        "BG_WINDOW": "#f9fafc",
        "BG_NAV": "#f3f6fd",
        "BG_PAGE": "#f6f9fc",
        "BG_LIST": "#ffffff",
        "BG_LIST_2": "#ffffff",
        "BG_HOVER": "#e6eaf0",
        "BG_SECTION": "#f1f3f6",
        "BG_SIDEBAR": "#e7ebf0",
        "BG_BUTTON_SECONDARY": "#f1f3f6",
        "BG_BUTTON_ICON": "#eef0f4",
        "BG_BUTTON_PRESSED": "#e2e6ec",
        "BG_BUTTON_SECONDARY_2": "#e2e6ec",
        "BG_BUTTON_ICON_2": "#e2e6ec",
        "BG_OPTION": "#f1f3f6",
        "BG_DISABLED_2": "#d8dee6",
        "BG_LAUNCH_FIELD_DISABLED": "#e9edf2",
        "BG_OVERLAY_CARD": "#ffffff",
        "BORDER_OVERLAY_CARD": "#c9d2dc",
    }
    for token, value in expected.items():
        assert light[token] == value, f"light {token} moved to {light[token]}, expected {value}"


def test_the_light_stylesheet_is_byte_identical():
    """Not one byte of gui/resources/style_light.qss may move: this is a dark-only change."""
    digest = hashlib.sha256(LIGHT_SHEET.read_bytes()).hexdigest()
    assert digest == LIGHT_SHEET_SHA256, (
        f"gui/resources/style_light.qss changed (sha256 {digest}); this change is supposed to leave "
        f"the light theme byte-identical"
    )
