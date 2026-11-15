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
  hover (#c5d5e8 -> #a7b4c2, measured 1.41:1). The same collapse hides the pressed state of the
  outlined buttons: at rest they are transparent over ``BG_LIST``, and in dark the fill they painted
  while pressed was ``BG_LIST`` to the byte, so pressing one changed nothing at all. That one now has
  a token of its own (``BG_BUTTON_PRESSED``) and is pinned below.

* **An edge drawn against a backdrop that is not in the palette.** The in-window dialog's card sits on
  a scrim painted over a snapshot of the window (gui/overlay.py). That scrim used to be a single
  35% black for both themes, which leaves a white card 2.59:1 clear of the backdrop in light but
  ``BG_LIST`` only 1.15:1 clear of it in dark: the card's fill and the dimmed page read as one
  surface. The scrim now follows the theme and the card has a fill of its own, because darkening the
  scrim alone cannot pass the floor - see the ceiling test below.

These tests pin the measurements; they do not restate the QSS. Every value is read back out of the
sheets and the palette (and out of gui/overlay.py for the scrim), because the defect was exactly that
the two agreed when they should not have.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from gui import overlay, theme

RESOURCES = Path(__file__).resolve().parent.parent / "gui" / "resources"
DARK_SHEET = RESOURCES / "style.qss"
LIGHT_SHEET = RESOURCES / "style_light.qss"

#: The pinned digest of gui/resources/style_light.qss. It is a witness, not a convenience: a
#: stylesheet edit is the one way to change the light theme without touching a token value, so every
#: deliberate light-sheet change must update this digest in the same commit.
LIGHT_SHEET_SHA256 = "24b3dbfe1b5677aa96568871fa3f532af1bc7590f184b46c2676aac0fcbb8ee5"

#: A card's fill has to be at least this far from the surface behind it to read as a separate sheet.
CARD_FLOOR = 1.8

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


def _scrimmed(mode: str, base_token: str, severity: str = "normal") -> str:
    """The scrim painted source-over ``base_token``, the way both overlays composite it.

    The card is drawn *on top of* the scrim (a child widget paints after its parent), so what the card
    has to separate from is the page with the scrim over it - not the scrim's own colour.
    """
    colour = overlay.scrim_colour(severity, mode)
    red, green, blue, alpha = colour.getRgb()
    weight = alpha / 255
    value = _colour(mode, base_token).lstrip("#")
    base = [int(value[index : index + 2], 16) for index in (0, 2, 4)]
    return "#" + "".join(
        f"{round(channel * (1 - weight) + tint * weight):02x}"
        for channel, tint in zip(base, (red, green, blue), strict=True)
    )


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


def _overlay_card_declaration(sheet: Path, prop: str) -> str | None:
    for selector, declarations in _rules(sheet):
        if selector == "QWidget#overlayCard":
            return _token(declarations.get(prop))
    return None


def _pressed_background(sheet: Path, selector: str) -> str | None:
    """The token a ``:pressed`` rule paints its background with."""
    for name, declarations in _rules(sheet):
        if name == selector:
            return _token(declarations.get("background"))
    return None


def test_the_scrim_follows_the_theme(monkeypatch):
    """Light keeps the 35% black both themes used; dark dims harder, and its severities harder still.

    The scrim is a paint, not a QSS declaration, so it cannot go through the token table: it lives in
    gui/overlay.py and is resolved against the theme in force. Both consumers (the crash diagnosis
    overlay and gui/inwindow_dialog.py) index the same ``SCRIM`` table, so one lookup rule covers both.
    """
    # Light: the exact values the theme-blind table had - the light rendering must not move.
    assert overlay.scrim_colour("normal", "light").getRgb() == (0, 0, 0, 90)
    assert overlay.scrim_colour("warning", "light").getRgb() == (80, 0, 0, 140)
    assert overlay.scrim_colour("error", "light").getRgb() == (80, 0, 0, 140)

    # Dark: heavier than light in every severity (the card's fill is darker there, so the backdrop
    # has to be darker still) and never transparent.
    for severity in ("normal", "warning", "error"):
        dark = overlay.scrim_colour(severity, "dark")
        light = overlay.scrim_colour(severity, "light")
        assert dark.alpha() > light.alpha() >= 90, severity
        assert dark.alpha() < 255, f"{severity}: the backdrop must not become opaque black"

    # The table resolves at lookup time, so an overlay that is already open follows a theme switch
    # instead of keeping the palette it was built with.
    monkeypatch.setattr(theme, "_last_theme", "light")
    assert overlay.SCRIM["normal"].getRgb() == (0, 0, 0, 90)
    monkeypatch.setattr(theme, "_last_theme", "dark")
    assert overlay.SCRIM["normal"].getRgb() == (0, 0, 0, 150)
    assert set(overlay.SCRIM) == {"normal", "warning", "error"}
    assert "unknown" not in overlay.SCRIM, (
        "an unknown severity must not resolve to the normal scrim"
    )


def test_a_darker_scrim_could_not_have_carried_the_dark_card():
    """Why the card needed a fill of its own: BG_LIST cannot reach the floor against *any* scrim.

    Contrast against a backdrop dimmed towards black approaches "BG_LIST against pure black" from
    below, so no alpha value lifts the old fill over 1.8:1 - measured, the old card sat at 1.15:1 and
    the mathematical ceiling is 1.24:1. The scrim was half the fix; the fill is the other half.
    """
    dark = theme.theme_palette("dark")
    ceiling = theme.contrast_ratio(dark["BG_LIST"], "#000000")
    assert ceiling < CARD_FLOOR, (
        f"BG_LIST reaches {ceiling:.2f}:1 against a *fully black* backdrop, so darkening the scrim "
        f"could not have lifted it over {CARD_FLOOR}:1 after all"
    )


def test_the_dark_overlay_card_clears_the_scrim():
    """The dialog card's fill against the dimmed page, for every severity the crash dialog can show.

    Measured before this change: 1.15:1 with the black scrim, 1.07:1 with the red one - the card and
    the page behind it were one surface, and only the outline said where the card was.
    """
    dark = theme.theme_palette("dark")
    for surface in ("BG_PAGE", "BG_SECTION"):  # the paper, and a page card it covers
        for severity in ("normal", "warning", "error"):
            backdrop = _scrimmed("dark", surface, severity)
            ratio = theme.contrast_ratio(dark["BG_OVERLAY_CARD"], backdrop)
            assert ratio >= CARD_FLOOR, (
                f"dark: the overlay card's fill is only {ratio:.2f}:1 from the {severity} scrim over "
                f"{surface} (floor {CARD_FLOOR:.2f}:1); the card reads as part of the backdrop"
            )


def test_the_dark_overlay_card_outline_frames_that_fill():
    """The outline's job changed: it now frames the raised fill instead of carrying the silhouette.

    BORDER_SWATCH - the previous choice - would be 1.06:1 above the new fill, i.e. invisible; the
    token's value has to stay a visible hairline on both the fill and the scrim.
    """
    dark = theme.theme_palette("dark")
    assert _overlay_card_declaration(DARK_SHEET, "background") == "BG_OVERLAY_CARD"
    assert _overlay_card_declaration(DARK_SHEET, "border") == "BORDER_OVERLAY_CARD"
    on_fill = theme.contrast_ratio(dark["BORDER_OVERLAY_CARD"], dark["BG_OVERLAY_CARD"])
    on_scrim = theme.contrast_ratio(dark["BORDER_OVERLAY_CARD"], _scrimmed("dark", "BG_PAGE"))
    assert on_fill >= 1.30, f"dark: the card's outline is {on_fill:.2f}:1 above its own fill"
    assert on_scrim >= 2.0, f"dark: the card's outline is {on_scrim:.2f}:1 against the scrim"


def test_the_light_overlay_is_unchanged():
    """Light pays nothing for the dark card: same scrim, same white fill, same quiet outline.

    The light sheet still spells the card exactly as it did (``BG_LIST`` + ``BORDER_BUTTON_ICON``);
    the tokens this change added carry the light values those rules already resolved to.
    """
    light = theme.theme_palette("light")
    assert light["BG_OVERLAY_CARD"] == "#ffffff"
    assert light["BORDER_OVERLAY_CARD"] == light["BORDER_BUTTON_ICON"]
    ratio = theme.contrast_ratio(light["BG_OVERLAY_CARD"], _scrimmed("light", "BG_PAGE"))
    assert abs(ratio - 2.59) <= 0.01, f"light: the card/scrim contrast moved to {ratio:.2f}:1"
    outline = theme.contrast_ratio(light["BORDER_BUTTON_ICON"], _scrimmed("light", "BG_PAGE"))
    assert abs(outline - 1.70) <= 0.01, f"light: the card outline moved to {outline:.2f}:1"
    # The light sheet keeps its own spelling of the rule, not the dark one's tokens.
    assert "background: __BG_LIST__;" in LIGHT_SHEET.read_text(encoding="utf-8")
    assert _overlay_card_declaration(LIGHT_SHEET, "border") == "BORDER_BUTTON_ICON"


def test_the_dark_pressed_fill_is_a_step_away_from_rest():
    """A pressed outlined button has to move away from the surface it sits on.

    At rest the button is transparent, so what the user compares the press against is the page or card
    behind it - ``BG_LIST``. In dark the old pressed fill was that exact colour, which is why pressing
    changed no pixel on screen even though the stylesheet named a different token.
    """
    dark = theme.theme_palette("dark")
    rest = dark["BG_LIST"]
    assert dark["BG_BUTTON_SECONDARY_2"] == rest, (
        "the collapse this token exists for is gone from the palette; the test below now proves less"
    )
    pressed = dark["BG_BUTTON_PRESSED"]
    step = theme.contrast_ratio(pressed, rest)
    assert step >= 1.15, (
        f"dark: the pressed fill is only {step:.2f}:1 from the surface at rest; below 1.15:1 the "
        f"button looks unpressed while the user holds it"
    )
    # The press has to clear the *hover* fill too, or pressing looks like hovering.
    hover = dark["BG_BUTTON_SECONDARY"]
    hover_step = theme.contrast_ratio(pressed, hover)
    assert hover_step >= 1.10, (
        f"dark: the pressed fill is only {hover_step:.2f}:1 from the hover fill, so pressing reads "
        f"as hovering"
    )
    # Upwards, not downwards: there is no headroom below a near-black surface, and "pressed" must not
    # become a hole or pure black. Lighter than rest means more contrast against black.
    assert theme.contrast_ratio(pressed, "#000000") > theme.contrast_ratio(rest, "#000000"), (
        "dark: the pressed fill went darker than the surface; below BG_LIST the next step is black"
    )
    assert pressed.lower() != "#000000"


def test_the_pressed_token_is_dark_only():
    """Light keeps the spelling and the values it shipped with; dark is the sheet that moved."""
    light = theme.theme_palette("light")
    assert light["BG_BUTTON_PRESSED"] == light["BG_BUTTON_SECONDARY_2"] == "#e2e6ec"
    assert light["BG_BUTTON_ICON_2"] == "#e2e6ec"

    dark_sheet = DARK_SHEET.read_text(encoding="utf-8")
    light_sheet = LIGHT_SHEET.read_text(encoding="utf-8")
    assert "__BG_BUTTON_SECONDARY_2__" not in dark_sheet
    assert "__BG_BUTTON_ICON_2__" not in dark_sheet
    assert "__BG_BUTTON_PRESSED__" in dark_sheet
    assert "__BG_BUTTON_PRESSED__" not in light_sheet  # the light sheet was not touched
    assert (
        _pressed_background(DARK_SHEET, "QPushButton#secondaryButton:pressed") == "BG_BUTTON_PRESSED"
    )
    assert _pressed_background(DARK_SHEET, "QPushButton#iconButton:pressed") == "BG_BUTTON_PRESSED"
    assert (
        _pressed_background(LIGHT_SHEET, "QPushButton#secondaryButton:pressed")
        == "BG_BUTTON_SECONDARY_2"
    )
    assert _pressed_background(LIGHT_SHEET, "QPushButton#iconButton:pressed") == "BG_BUTTON_ICON_2"


def test_the_light_stylesheet_keeps_its_pinned_digest():
    """The light sheet is pinned byte for byte.

    Light's rendering is proved at three levels: the digest below (not one byte of the sheet moved
    without this comment changing too), the token values (the light column), and the scrim (exact
    RGBA above). If a future change really does edit the light theme, update this digest in the same
    commit - the point is that touching the light sheet becomes a decision instead of a drift.
    """
    digest = hashlib.sha256(LIGHT_SHEET.read_bytes()).hexdigest()
    assert digest == LIGHT_SHEET_SHA256, (
        f"gui/resources/style_light.qss changed (sha256 {digest}); update the pinned digest in the "
        f"same commit if this light-theme change is deliberate"
    )


def test_the_dark_palette_values_the_sweep_set():
    """Pin the two dark values so a later edit is a decision, not a drift."""
    assert theme.NEUTRAL_TOKENS["BORDER_HOVER"]["dark"] == "#46566a"
    assert theme.contrast_ratio("#46566a", "#33404f") >= 1.30
