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

"""Dark-theme sweep: a state or an edge whose dark value erases the difference it exists for.

The page-by-page dark sweep (tmp/dark_sweep/REPORT.md) found two defects of one class. Both were
technically styled and visually absent, and neither is a contrast failure the pair audit can see:

* **A state that repaints in the colour it already had.** ``QLineEdit:hover`` sets
  ``border-color: BORDER_HOVER``, but on the launch, versions, settings and resources pages the
  resting outline is ``BORDER_LAUNCH_FIELD`` - and in dark those were the same value ``#33404f``.
  Hovering the settings page's Java path changed literally no pixel, while the light sheet marked the
  hover (#c5d5e8 -> #a7b4c2, measured 1.41:1). The same collapse hid the pressed state of the outlined
  buttons (``BG_BUTTON_SECONDARY_2`` equals ``BG_LIST`` in dark) and the hovered slider handle
  (``BG_HOVER`` equals ``BG_LIST_2``); those two need a token that does not exist yet, so they are
  reported rather than pinned, and only the border case has a value here.

* **An edge drawn against a backdrop that is not in the palette.** The in-window dialog's card sits
  on a theme-blind 35% black scrim (gui/overlay.py SCRIM). That tint leaves a white card 2.59:1 clear
  of the backdrop in the light sheet and ``BG_LIST`` 1.15:1 clear of it in dark, so the card's outline
  is the only thing that carries its silhouette there.

These tests pin the measurements; they do not restate the QSS. Every value is read back out of the
sheets and the palette, because the defect was exactly that the two agreed when they should not have.
"""

from __future__ import annotations

import re
from pathlib import Path

from gui import theme

RESOURCES = Path(__file__).resolve().parent.parent / "gui" / "resources"
DARK_SHEET = RESOURCES / "style.qss"
LIGHT_SHEET = RESOURCES / "style_light.qss"

# The states a sheet can paint. `:focus` is not here: the sheets gate the keyboard ring on the
# `keyboardFocus` property instead (see gui.widgets.KeyboardFocusTracker).
STATES = (":hover", ":pressed", ":checked", ":selected", ":disabled")
BORDER_PROPS = (
    "border",
    "border-color",
    "border-left",
    "border-right",
    "border-top",
    "border-bottom",
)
TOKEN = re.compile(r"__([A-Z0-9_]+)__")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# The scrim the in-window dialogs paint over the window (gui/overlay.py SCRIM["normal"]): 35% black.
SCRIM_ALPHA = 90


def _rules(path: Path) -> list[tuple[str, dict[str, str]]]:
    """Every (selector, declarations) pair in a sheet, comments and at-rules aside."""
    text = COMMENT.sub("", path.read_text(encoding="utf-8"))
    found: list[tuple[str, dict[str, str]]] = []
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        declarations: dict[str, str] = {}
        for line in block.group(2).split(";"):
            if ":" not in line:
                continue
            prop, _, value = line.partition(":")
            declarations[prop.strip()] = value.strip()
        for selector in block.group(1).split(","):
            selector = selector.strip()
            if selector:
                found.append((selector, declarations))
    return found


def _base_selector(selector: str) -> list[str]:
    """The same widget without its state, most specific spelling first."""
    stripped = selector
    for state in STATES:
        stripped = stripped.replace(state, "")
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    return [
        candidate.strip()
        for candidate in (stripped, re.sub(r"(#|\.)[A-Za-z_][\w-]*", "", stripped))
        if candidate.strip()
    ]


def _rest_declaration(
    rules: list[tuple[str, dict[str, str]]], selector: str, prop: str
) -> str | None:
    aliases = {prop, "border" if prop.startswith("border-") else prop}
    for candidate in _base_selector(selector):
        for other, declarations in rules:
            if other != candidate:
                continue
            for alias in aliases:
                if alias in declarations:
                    return declarations[alias]
    return None


def _token(value: str | None) -> str | None:
    if not value:
        return None
    tokens = TOKEN.findall(value)
    return tokens[0] if len(tokens) == 1 else None


def _colour(mode: str, token: str) -> str:
    return theme.theme_palette(mode)[token]


def test_the_hover_outline_moves_in_dark_as_far_as_it_moves_in_light():
    """BORDER_HOVER must be a real step from the outlines it replaces on hover.

    The sweep measured the flat case: dark's rest outline (BORDER_LAUNCH_FIELD) and its hover outline
    (BORDER_HOVER) were both #33404f, so `:hover` on an input repainted nothing. The light sheet's
    steps are 1.41:1 against the page-scoped inputs and 1.16:1 against the settings page's outlined
    buttons; the dark one is now at least as large as both, and each theme's floor is asserted so
    neither can drift back to "different in the file, identical on screen".
    """
    # (theme, the resting outline a hover replaces, the smallest step that reads on a 1px line)
    cases = (
        ("dark", "BORDER_LAUNCH_FIELD", 1.30),
        ("dark", "BORDER_BUTTON_SECONDARY", 1.30),
        ("light", "BORDER_LAUNCH_FIELD", 1.30),
        ("light", "BORDER_BUTTON_SECONDARY", 1.10),
    )
    for theme_name, resting, floor in cases:
        palette = theme.theme_palette(theme_name)
        assert palette["BORDER_HOVER"].lower() != palette[resting].lower(), (
            f"{theme_name}: hovering a control whose outline is {resting} repaints the border "
            f"in the colour it already has ({palette[resting]})"
        )
        step = theme.contrast_ratio(palette["BORDER_HOVER"], palette[resting])
        assert step >= floor, (
            f"{theme_name}: BORDER_HOVER is only {step:.2f}:1 from {resting}; below {floor:.2f}:1 a "
            f"1px line does not visibly change"
        )


def test_the_dark_hover_outline_stays_a_quiet_outline():
    """The step must not overshoot into a bright edge: it has to stay below the text colour's weight.

    The dark value is also the resting outline of a checkbox/radio indicator, so it is checked against
    the fill it is drawn on the way the light sheet's own indicator outline is (2.11:1 there).
    """
    dark = theme.theme_palette("dark")
    light = theme.theme_palette("light")
    for mode, palette in (("dark", dark), ("light", light)):
        assert theme.contrast_ratio(palette["BORDER_HOVER"], palette["BG_LIST"]) <= 3.0, (
            f"{mode}: the hover outline reads as a control border, not as an accent edge"
        )
    on_card = theme.contrast_ratio(dark["BORDER_HOVER"], dark["BG_LIST"])
    light_on_card = theme.contrast_ratio(light["BORDER_HOVER"], light["BG_LIST"])
    assert abs(on_card - light_on_card) <= 0.35, (
        f"the indicator outline is {on_card:.2f}:1 on the card in dark and {light_on_card:.2f}:1 in "
        f"light; the two themes should frame a checkbox the same way"
    )


def test_every_dark_hover_rule_repaints_its_border_somewhere_new():
    """No `:hover` rule in the dark sheet may resolve to the border colour its rest state already has.

    This is the generic form of the defect: it does not name BORDER_HOVER, so a future rule that pairs
    a hover outline with a token equal to its rest outline fails here even if the tokens are new. The
    light sheet is measured too - the sweep's contract is that the light theme's states do not change,
    and that its own rules are not flat (which would make the comparison meaningless).
    """
    for sheet, mode in ((DARK_SHEET, "dark"), (LIGHT_SHEET, "light")):
        rules = _rules(sheet)
        palette = theme.theme_palette(mode)
        flat: list[str] = []
        for selector, declarations in rules:
            if ":hover" not in selector:
                continue
            for prop in BORDER_PROPS:
                state_token = _token(declarations.get(prop))
                if state_token is None:
                    continue
                rest_token = _token(_rest_declaration(rules, selector, prop))
                if rest_token is None:
                    continue
                if palette[state_token].lower() == palette[rest_token].lower():
                    flat.append(f"{selector} {{ {prop}: {state_token} }} == {rest_token}")
        assert not flat, (
            f"{sheet.name}: these hover rules repaint a border in the colour it already has, so the "
            f"hover is invisible:\n  " + "\n  ".join(sorted(set(flat)))
        )


def _overlay_card_outline(sheet: Path) -> str | None:
    for selector, declarations in _rules(sheet):
        if selector == "QWidget#overlayCard":
            return _token(declarations.get("border"))
    return None


def _scrim_over(colour: str) -> str:
    """The scrim's tint applied over `colour`, the way paintEvent composites it (source-over black)."""
    alpha = SCRIM_ALPHA / 255
    value = colour.lstrip("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return f"#{round(red * (1 - alpha)):02x}{round(green * (1 - alpha)):02x}{round(blue * (1 - alpha)):02x}"


def test_the_dark_overlay_card_edge_carries_the_silhouette():
    """The dialog card's silhouette against the scrim, in the one theme where the fill cannot carry it.

    The scrim is theme-blind, so the card's *fill* clears it by 2.59:1 in light and 1.15:1 in dark -
    which is why the dark sheet draws the stronger outline. 2.0:1 is the floor: below it the card's
    edge and the dimmed page read as one surface (the sweep measured 1.46:1 before the change).
    """
    token = _overlay_card_outline(DARK_SHEET)
    assert token is not None, "style.qss no longer outlines the overlay card"
    ratio = theme.contrast_ratio(_colour("dark", token), _scrim_over(_colour("dark", "BG_PAGE")))
    assert ratio >= 2.0, (
        f"dark: the overlay card's outline {token} lights up at {ratio:.2f}:1 against the scrim, so "
        f"the card has no visible silhouette"
    )


def test_the_light_sheet_keeps_its_own_overlay_outline():
    """The sweep's contract: the light theme is unchanged. Its card keeps the quiet outline.

    Light does not need the stronger edge, because its card *fill* is what separates it from the
    scrim (2.59:1); the measured outline is pinned here so a later change to the light sheet has to
    come with a decision attached to it.
    """
    assert _overlay_card_outline(LIGHT_SHEET) == "BORDER_BUTTON_ICON"
    ratio = theme.contrast_ratio(
        _colour("light", "BORDER_BUTTON_ICON"), _scrim_over(_colour("light", "BG_PAGE"))
    )
    assert 1.60 <= ratio <= 1.80, (
        f"light: the overlay outline moved to {ratio:.2f}:1 against the scrim"
    )
    assert theme.NEUTRAL_TOKENS["BORDER_HOVER"]["light"] == "#a7b4c2"
    assert theme.NEUTRAL_TOKENS["BORDER_SWATCH"]["light"] == "#b6c1cd"


def test_the_dark_palette_values_the_sweep_set():
    """Pin the two dark values so a later edit is a decision, not a drift."""
    assert theme.NEUTRAL_TOKENS["BORDER_HOVER"]["dark"] == "#46566a"
    assert theme.contrast_ratio("#46566a", "#33404f") >= 1.30
