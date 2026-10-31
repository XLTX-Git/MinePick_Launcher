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

"""Theme application: dark / light QSS with a custom accent color and UI font (applied immediately)."""

from __future__ import annotations

import re
import weakref

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from launcher import paths

# Default accent per theme; the tint steps below are derived from whichever base is in use
DEFAULT_ACCENTS = {"dark": "#35a06a", "light": "#2f9e6f"}

# The one page paper each theme paints behind its cards. It lives here rather than inline in
# NEUTRAL_TOKENS because accent_palette() derives ACCENT_TEXT against the light value: accent-coloured
# *text* directly on the paper is a different question from white text on an accent *fill*, and the fill
# shade it used before measured 4.3-4.4:1 as text once the paper was blue-tinted.
PAGE_PAPER = {"dark": "#0f141b", "light": "#f6f9fc"}

# Corner radius presets: (extra small, small, large) in px
RADIUS_SCALES = {
    "compact": (3, 4, 6),
    "default": (4, 6, 8),
    "round": (6, 9, 12),
}
DEFAULT_RADIUS = "default"
DEFAULT_FONT = "Microsoft YaHei UI"
# Latin first, on purpose: Microsoft YaHei UI's own Latin glyphs are markedly wider spaced than
# Segoe UI's, so a stack that starts with YaHei sets every English word and digit in the wider face.
# Measured on the same 15px line, the YaHei-first stack runs about one character wider than the
# Segoe-first one. Segoe UI first gives the Latin text its own spacing while the CJK glyphs fall
# back to YaHei, which is what the UI font is for here. The entries after it cover the scripts
# Segoe UI does not: Hangul needs Malgun Gothic and Japanese needs Yu Gothic UI - YaHei has
# Simplified Chinese only, and the trailing sans-serif maps back to YaHei on Chinese Windows, so
# without those two the Korean and Japanese UI would be a wall of tofu.
LATIN_FONT = "Segoe UI"
FONT_FALLBACKS = '"Microsoft YaHei UI", "Malgun Gothic", "Yu Gothic UI", "Microsoft YaHei", sans-serif'

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def normalize_hex(value: str | None) -> str | None:
    """Return "#rrggbb" for a user supplied color, or None when it is not a valid hex color."""
    if not value:
        return None
    match = _HEX_RE.match(str(value).strip())
    if match is None:
        return None
    return "#" + match.group(1).lower()


def _mix(color: str, target: str, ratio: float) -> str:
    """Blend color toward target by ratio (0..1)."""
    base = normalize_hex(color) or "#000000"
    other = normalize_hex(target) or "#000000"
    out = []
    for index in (1, 3, 5):
        left = int(base[index : index + 2], 16)
        right = int(other[index : index + 2], 16)
        out.append(round(left + (right - left) * ratio))
    return "#" + "".join(f"{value:02x}" for value in out)


def _lighten(color: str, ratio: float) -> str:
    return _mix(color, "#ffffff", ratio)


def _darken(color: str, ratio: float) -> str:
    return _mix(color, "#000000", ratio)


def _relative_luminance(color: str) -> float:
    base = normalize_hex(color) or "#000000"
    channels = []
    for index in (1, 3, 5):
        value = int(base[index : index + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two hex colours."""
    light, dark = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (light + 0.05) / (dark + 0.05)


def readable_fill(accent: str, target: float = 4.5) -> str:
    """Darken an accent until white text on it reaches the WCAG AA contrast target.

    Filled elements that carry white text (primary buttons, chips) use this shade;
    the pure accent stays for borders, pills and other non-text accents.
    """
    shade = accent
    for step in range(61):
        if contrast_ratio("#ffffff", shade) >= target:
            return shade
        shade = _darken(accent, step / 100)
    return shade


def readable_text_on(accent: str, *backgrounds: str, target: float = 4.5) -> str:
    """Darken an accent until it reaches the WCAG AA contrast target as TEXT on every background.

    ``readable_fill`` answers "white text on this fill"; this answers the other direction. The two are
    not interchangeable: a fill chosen for white-on-it lands around 4.3-4.4:1 when the same shade is
    used as text on a pale surface, which is exactly where the resource page's unselected chips sit.
    """
    shade = accent
    for step in range(61):
        if all(contrast_ratio(shade, background) >= target for background in backgrounds):
            return shade
        shade = _darken(accent, (step + 1) / 100)
    return shade


def accent_palette(theme: str, accent: str | None = None) -> dict[str, str]:
    """All accent-derived colors used by the style sheets.

    Keys map onto the __ACCENT*__ placeholders inside the QSS files, so a user
    supplied hex color re-tints buttons, selections, chips and cards at once.
    """
    base = normalize_hex(accent) or DEFAULT_ACCENTS.get(theme, DEFAULT_ACCENTS["dark"])
    if theme == "light":
        button = readable_fill(base)
        tint = _lighten(base, 0.90)
        return {
            "ACCENT": base,
            "ACCENT_BUTTON": button,
            "ACCENT_BUTTON_HOVER": _lighten(button, 0.10),
            "ACCENT_BUTTON_PRESS": _darken(button, 0.12),
            "ACCENT_HOVER": _darken(button, 0.10),
            "ACCENT_SEL": _lighten(base, 0.68),  # a real step below ACCENT_SOFT (0.82), not a 2-point one
            "ACCENT_SEL_TEXT": _darken(base, 0.72),
            "ACCENT_SOFT": _lighten(base, 0.82),
            "ACCENT_TINT": tint,
            # Text drawn on the page paper itself (the resource chips): the page paper and the chips'
            # tinted hover fill are both paler than white but not white, so this shade is derived
            # against the two surfaces the label can actually land on.
            "ACCENT_TEXT": readable_text_on(base, PAGE_PAPER["light"], tint),
            "FOCUS_RING": button,
        }
    button = readable_fill(base)
    return {
        "ACCENT": base,
        "ACCENT_BUTTON": button,
        "ACCENT_BUTTON_HOVER": _lighten(button, 0.10),
        "ACCENT_BUTTON_PRESS": _darken(button, 0.12),
        "ACCENT_HOVER": _darken(button, 0.10),
        "ACCENT_SEL": _darken(base, 0.20),
        "ACCENT_SEL_TEXT": "#ffffff",
        "ACCENT_SOFT": _darken(base, 0.68),
        "ACCENT_TINT": _darken(base, 0.80),
        # The dark sheet labels its chips with the pure accent, which is what this shade is there.
        "ACCENT_TEXT": base,
        "FOCUS_RING": base,
    }



# Neutral / semantic palette: one entry per role, one value per theme. Together with
# accent_palette() these are the only colours the QSS files may reference, through __TOKEN__
# placeholders; both sheets are checked to hold no colour literals by tests/test_qss_tokens.py.
NEUTRAL_TOKENS: dict[str, dict[str, str]] = {
    "BG_BUTTON_ICON": {"dark": "#1c2531", "light": "#eef0f4"},
    "BG_BUTTON_ICON_2": {"dark": "#161d27", "light": "#e2e6ec"},
    "BG_BUTTON_SECONDARY": {"dark": "#1c2531", "light": "#f1f3f6"},
    "BG_BUTTON_SECONDARY_2": {"dark": "#161d27", "light": "#e2e6ec"},
    "BG_DISABLED": {"dark": "#131a23", "light": "#f4f6f9"},
    "BG_DISABLED_2": {"dark": "#232c39", "light": "#d8dee6"},
    # dark rows hover with this; the light sheet uses ACCENT_TINT instead, so the light value
    # was a copy of the dark one that no rule could reach (FG_TEXT on it read 1.06:1).
    "BG_HOVER": {"dark": "#1c2531", "light": "#e6eaf0"},
    # Launch page fields and the settings page's disabled inputs keep their own recessed tone.
    "BG_LAUNCH_FIELD_DISABLED": {"dark": "#232c39", "light": "#e9edf2"},
    "BG_LIST": {"dark": "#161d27", "light": "#ffffff"},
    "BG_LIST_2": {"dark": "#1c2531", "light": "#ffffff"},
    # The sidebar rail: one step darker than the page paper and blue-tinted, so it recedes behind the
    # cards instead of competing with them. Dark keeps the window colour it already painted.
    "BG_NAV": {"dark": "#0f141b", "light": "#f3f6fd"},
    "BG_OPTION": {"dark": "#161d27", "light": "#f1f3f6"},
    "BG_OVERLAY_CODE": {"dark": "#2a1a1e", "light": "#fdecea"},
    # The one page paper: every page container, the content stack and each page-level scroll viewport
    # paints this, so the three near-identical blues the pages used to invent are gone.
    "BG_PAGE": PAGE_PAPER,
    "BG_RAIL_DOT": {"dark": "#33404f", "light": "#b6c1cd"},
    "BG_SCROLLBAR": {"dark": "#6d7f95", "light": "#8a96a3"},
    "BG_SECTION": {"dark": "#1c2531", "light": "#f1f3f6"},
    "BG_SEPARATOR": {"dark": "#27303d", "light": "#dfe4ea"},
    "BG_SIDEBAR": {"dark": "#1c2531", "light": "#e7ebf0"},
    # The one blue band at the top of the window. Both themes use the same shade so the chrome
    # reads as the launcher's frame rather than a theme surface; #1e6fd9 gives white text 4.85:1
    # (the PCL blue #2078da only reaches 4.41:1 and would fail AA). The hover token is the same
    # blue one step deeper, used behind the minimize glyph; close keeps its red.
    "BG_TOPBAR": {"dark": "#1a4e8c", "light": "#1e6fd9"},
    "BG_TITLEBAR_CLOSE": {"dark": "#cf3f3f", "light": "#cf3f3f"},
    "BG_TITLEBAR_MINIMIZE": {"dark": "#1a63c4", "light": "#1a63c4"},
    # Window chrome used below the title bar: the status bar and native dialogs. One step lighter
    # than the page paper and one below the cards: near the cards, but never a second white surface.
    "BG_WINDOW": {"dark": "#0f141b", "light": "#f9fafc"},
    "BORDER_BUTTON_ICON": {"dark": "#27303d", "light": "#c9d2dc"},
    "BORDER_BUTTON_SECONDARY": {"dark": "#33404f", "light": "#b6c1cd"},
    "BORDER_BUTTON_SECONDARY_2": {"dark": "#232c39", "light": "#d8dee6"},
    "BORDER_HOVER": {"dark": "#33404f", "light": "#a7b4c2"},
    "BORDER_LAUNCH_FIELD": {"dark": "#33404f", "light": "#c5d5e8"},
    "BORDER_LIST": {"dark": "#27303d", "light": "#c9d2dc"},
    # The hairline between the sidebar rail and the page: decoration, not a control, so it is one quiet
    # step below the rail rather than a visible frame.
    "BORDER_NAV": {"dark": "#27303d", "light": "#dfe7f0"},
    "BORDER_OVERLAY_CODE": {"dark": "#e5787f", "light": "#e5787f"},
    "BORDER_SWATCH": {"dark": "#3a4757", "light": "#b6c1cd"},
    "BORDER_TABLE": {"dark": "#212a36", "light": "#dfe4ea"},
    "DANGER_BG": {"dark": "#2a1a1e", "light": "#fbeceb"},
    "DANGER_BG_2": {"dark": "#221317", "light": "#f5ddda"},
    "DANGER_BORDER": {"dark": "#6d3a40", "light": "#e0b3ad"},
    "DANGER_BORDER_2": {"dark": "#e5787f", "light": "#b03a2e"},
    "DANGER_BORDER_3": {"dark": "#3a2a2d", "light": "#eddad7"},
    "DANGER_FG": {"dark": "#e5787f", "light": "#b03a2e"},
    "DANGER_FG_2": {"dark": "#7a5a5e", "light": "#c39a95"},
    "FG_BUTTON_SECONDARY": {"dark": "#b9c6d6", "light": "#33475c"},
    "FG_CARD_LATEST": {"dark": "#ffffff", "light": "#1f2a36"},
    "FG_DISABLED": {"dark": "#6b7c92", "light": "#9aa5b1"},
    "FG_DISABLED_2": {"dark": "#6b7c92", "light": "#78838f"},
    # #677585 measured 4.46:1 on the new page paper - the empty-state labels sit directly on it, so the
    # value moves one step darker (white cards are unaffected: 5.03:1 there, up from 4.71:1).
    "FG_EMPTY_STATE": {"dark": "#7b8ca3", "light": "#647080"},
    "FG_HINT": {"dark": "#8fa2b8", "light": "#4f6070"},
    # Launch page hero and quiet lines (verified cool version: #1f2328 body, #6b7280 hints).
    "FG_LAUNCH": {"dark": "#e6ecf5", "light": "#1f2328"},
    "FG_LAUNCH_HINT": {"dark": "#8fa2b8", "light": "#6b7280"},
    "FG_OPTION": {"dark": "#d8dee9", "light": "#26313d"},
    "FG_OPTION_2": {"dark": "#ffffff", "light": "#14503a"},
    "FG_OVERLAY_BODY": {"dark": "#d7e0ec", "light": "#1f2a36"},
    "FG_OVERLAY_CODE": {"dark": "#ffffff", "light": "#8c1d18"},
    "FG_PAGE_TITLE": {"dark": "#eef3fa", "light": "#16202b"},
    "FG_RAIL_ITEM": {"dark": "#7b8ca3", "light": "#5b6a7b"},
    "FG_SELECTED": {"dark": "#ffffff", "light": "#ffffff"},
    "FG_STATUS_WARNING": {"dark": "#e5c07b", "light": "#87691b"},
    "FG_TEXT": {"dark": "#e6ecf5", "light": "#1f2a36"},
    # White text on the one blue title bar; see BG_TOPBAR for the 4.85:1 measurement.
    "FG_TOPBAR": {"dark": "#ffffff", "light": "#ffffff"},
}


def theme_palette(theme: str, accent: str | None = None) -> dict[str, str]:
    """Every __TOKEN__ value for one theme: the neutral table plus the accent family."""
    palette = {name: values.get(theme, values["dark"]) for name, values in NEUTRAL_TOKENS.items()}
    palette.update(accent_palette(theme, accent))
    return palette


# QPalette roles the stylesheet cannot reach.
#
# The QSS paints the widgets we wrote, but Qt still draws some surfaces itself: the frame and base of a
# native popup, menu shadows, the viewport inside a platform-drawn container, disabled text, the 3D
# shades. Those come from the application palette, and the palette the platform hands us follows the
# *system* colour scheme - on a dark-mode Windows that is a dark palette. A light QSS over a dark palette
# therefore painted dark surfaces wherever the stylesheet stopped, which is exactly what the user saw:
# "the light theme is inexplicably ugly", wrong page and dropdown backgrounds, a see-through wizard.
# The mapping below derives every role from the same tokens the QSS uses, so there is still exactly one
# source of truth for colour.
PALETTE_ROLES = {
    "Window": "BG_WINDOW",
    "WindowText": "FG_TEXT",
    "Base": "BG_LIST",
    "AlternateBase": "BG_LIST_2",
    "Text": "FG_TEXT",
    "Button": "BG_BUTTON_SECONDARY",
    "ButtonText": "FG_TEXT",
    "BrightText": "FG_PAGE_TITLE",
    "Highlight": "ACCENT_SEL",
    "HighlightedText": "ACCENT_SEL_TEXT",
    "ToolTipBase": "BG_LIST_2",
    "ToolTipText": "FG_TEXT",
    "PlaceholderText": "FG_HINT",
    "Link": "ACCENT",
    # The 3D-shade roles (Light/Midlight/Mid/Dark/Shadow) are deliberately not mapped: they paint frame
    # edges and container backgrounds with hardcoded light/dark offsets relative to Button, which showed
    # up as wrong panel backgrounds on the mods page. Qt falls back to its own derivation there, which is
    # what the light theme looked like before. The roles above are the ones the bug actually needed.
}

PALETTE_DISABLED = {
    "Window": "BG_DISABLED",
    "Base": "BG_DISABLED",
    "Button": "BG_DISABLED",
    "WindowText": "FG_DISABLED",
    "Text": "FG_DISABLED",
    "ButtonText": "FG_DISABLED",
    "Highlight": "BG_DISABLED_2",
    "HighlightedText": "FG_DISABLED_2",
}


def theme_qpalette(theme: str, accent: str | None = None) -> QPalette:
    """The application palette for one theme, built from the same tokens as the QSS.

    ``setColor(role, colour)`` fills every colour group, so an unfocused window keeps the same colours as
    a focused one - the platform default dims text in inactive windows, which in a themed window reads as
    "the background changed when I clicked away". The disabled group is then overridden explicitly.
    """
    tokens = theme_palette(theme, accent)
    palette = QPalette()
    for role_name, token in PALETTE_ROLES.items():
        role = getattr(QPalette.ColorRole, role_name, None)
        if role is not None:
            palette.setColor(role, QColor(tokens[token]))
    for role_name, token in PALETTE_DISABLED.items():
        role = getattr(QPalette.ColorRole, role_name, None)
        if role is not None:
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(tokens[token]))
    # Qt 6.6 added an Accent role; older builds simply do not have it.
    accent_role = getattr(QPalette.ColorRole, "Accent", None)
    if accent_role is not None:
        palette.setColor(accent_role, QColor(tokens["ACCENT"]))
    return palette

def font_stack(family: str | None = None) -> str:
    """Font-family value for the QSS: Segoe UI first for Latin, then the UI font and its fallbacks.

    The default UI font (Microsoft YaHei UI) is the CJK face here, not the Latin one: its Latin
    glyphs are wider than Segoe UI's, so Segoe UI takes the first slot and YaHei catches the
    Chinese. A family the user picked explicitly keeps the first slot - they asked for it by name,
    and Segoe UI follows as that choice's Latin fallback rather than replacing it.
    """
    name = (family or "").strip()
    if name and name.casefold() != DEFAULT_FONT.casefold():
        return f'"{name}", "{LATIN_FONT}", {FONT_FALLBACKS}'
    return f'"{LATIN_FONT}", {FONT_FALLBACKS}'


def system_theme() -> str:
    """The OS colour preference: Windows light/dark setting, dark elsewhere."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except Exception:  # noqa: BLE001 - any failure means "no preference available"
        return "dark"


def resolve_theme(theme: str) -> str:
    """Turn the configured theme into a concrete one ("system" follows the OS)."""
    return system_theme() if theme == "system" else theme


def radius_scale(radius: str | None) -> tuple[int, int, int]:
    return RADIUS_SCALES.get(radius or DEFAULT_RADIUS, RADIUS_SCALES[DEFAULT_RADIUS])


# The (application, theme, accent, font, radius, device scale) last handed to setStyleSheet.
_last_applied: tuple[int, str, str, str, str, int] | None = None

# The token table (role -> colour) behind the stylesheet currently in force. Widgets that must
# style themselves - because the application stylesheet is not adopted for them on every
# platform - read their colours from here, so colour still has exactly one source of truth.
_last_tokens: dict[str, str] = {}


def current_tokens() -> dict[str, str]:
    """The token values last applied; empty before the first apply_theme call."""
    return dict(_last_tokens)


def accent_focus_ring() -> str:
    """The focus ring colour of the theme currently in force.

    Widgets that must style themselves (a widget-level stylesheet wins over the application sheet for
    every property it declares, so a rule in the sheet can never reach them) need the ring as a value,
    not as a placeholder. Reading it from the applied token table keeps colour at one source of truth:
    ``FOCUS_RING`` is the same token every other focus ring in the sheet uses, so this cannot drift.
    """
    tokens = current_tokens()
    if "FOCUS_RING" in tokens:
        return tokens["FOCUS_RING"]
    return DEFAULT_ACCENTS["dark"]  # before the first apply_theme call: any valid colour will do


def device_scale(app: QApplication) -> int:
    """The screen's device pixel ratio, clamped to the asset multiples we ship (1x/2x/3x)."""
    screen = app.primaryScreen()
    ratio = float(screen.devicePixelRatio()) if screen is not None else 1.0
    return max(1, min(3, round(ratio)))


_applying = False
_self_styling: weakref.WeakSet[object] = weakref.WeakSet()


def is_applying() -> bool:
    """True while apply_theme() is swapping the application stylesheet."""
    return _applying


def register_self_styling(obj: object) -> None:
    """Register an object with an on_theme_applied() method to be called after each swap."""
    _self_styling.add(obj)


def _notify_self_styling() -> None:
    for obj in list(_self_styling):
        callback = getattr(obj, "on_theme_applied", None)
        if callback is None:
            continue
        try:
            callback()
        except RuntimeError:
            # The widget died between the swap starting and finishing; nothing to restyle.
            pass


def apply_theme(
    theme: str,
    accent: str | None = None,
    font: str | None = None,
    radius: str | None = None,
) -> None:
    """Apply the theme; "system" follows the OS light/dark preference.

    Re-applying a stylesheet that is already in force is skipped deliberately. ``setStyleSheet`` makes
    Qt re-polish every widget in the application, and that cost *accumulates*: measured in a clean
    process with a single window, eight consecutive calls took 0.06s, 0.25s, 0.42s, 0.42s, 0.44s,
    1.72s, 2.16s. The settings page saves on every control change and re-applies the theme each time,
    so without this guard a session that touches a few settings pays seconds per change - and the test
    suite, which drives those saves, spends minutes in it.
    """
    global _last_applied, _last_tokens
    app = QApplication.instance()
    if app is None:
        return
    theme = resolve_theme(theme)
    scale = device_scale(app)
    key = (id(app), theme, accent or "", font or "", radius or "", scale)
    if key == _last_applied:
        return
    filename = "style_light.qss" if theme == "light" else "style.qss"
    path = paths.resource_path("gui/resources/" + filename)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    # url() in QSS cannot reliably resolve relative paths: replace the icon placeholders with the
    # absolute path of the file that matches this theme. The combo chevron ships as one coloured SVG
    # per state (normal / hover + keyboard focus / disabled) because a QSS image cannot be tinted; the
    # active icon uses the default accent, since an SVG cannot follow the user's accent colour.
    suffix = "-light" if theme == "light" else ""
    icons = "gui/resources/icons/ui"
    text = text.replace(
        "__CHEVRON_DOWN_ACTIVE__",
        paths.resource_path(f"{icons}/chevron-down{suffix}-active.svg").as_posix(),
    )
    text = text.replace(
        "__CHEVRON_DOWN_DISABLED__",
        paths.resource_path(f"{icons}/chevron-down{suffix}-disabled.svg").as_posix(),
    )
    text = text.replace(
        "__CHEVRON_DOWN__",
        paths.resource_path(f"{icons}/chevron-down{suffix}.svg").as_posix(),
    )
    text = text.replace("__CHECK__", paths.resource_path("gui/resources/check.png").as_posix())
    text = text.replace("__FONT__", font_stack(font))
    palette = theme_palette(theme, accent)
    # longest placeholders first so __ACCENT__ never eats the prefixed variants
    for token in sorted(palette, key=len, reverse=True):
        text = text.replace(f"__{token}__", palette[token])
    extra_small, small, large = radius_scale(radius)
    text = text.replace("__RADIUS_XS__", f"{extra_small}px")
    text = text.replace("__RADIUS_SM__", f"{small}px")
    text = text.replace("__RADIUS_LG__", f"{large}px")
    # The palette covers the surfaces Qt draws itself (native popup frames, menu shadows, platform-drawn
    # viewports, disabled text); the stylesheet covers the widgets we wrote. Both come from the same tokens,
    # and the palette must be in force *before* the sheet is touched - see the note on clearing below.
    app.setPalette(theme_qpalette(theme, accent))
    # Publish the tokens before the sheet: a widget that reacts to StyleChange reads them there.
    _last_tokens = dict(palette)
    # Clear the application sheet before installing the new one. Qt's cost for *replacing* it accumulates
    # within a process: in a clean run with one window, eight consecutive replacements took 0.06s, 0.25s,
    # 0.42s, 0.42s, 0.44s, 1.72s, 2.16s, and a real 1455-widget window paid 4.5-6.5s per dark/light switch
    # once it had. Dropping the sheet first puts Qt back on a cheap path - 0.86-1.04s per switch, flat
    # instead of growing: ten switches cost 8.6s against 54.3s. What is left is Qt re-polishing every
    # widget, and that is per-widget rather than per-rule: a 47-character sheet still costs 0.53s of it,
    # so shrinking the stylesheet would not remove it. Going below this needs the colours to move from the
    # stylesheet into the palette (setPalette alone measured 0ms), which is a restructuring, not a tweak.
    #
    # This used to say the opposite. Clearing was blamed when the 0.4.0 build shipped unstyled widgets
    # (dropdown arrows, page backgrounds, a see-through wizard) and was reverted. It was not the cause: the
    # application palette was never themed, so the surfaces Qt draws itself followed the OS while the
    # stylesheet was momentarily absent. Re-measured with the palette themed (commit 3166c91), the fast
    # path renders identically - 8 pages plus the wizard, real platform, before and after a dark->light
    # switch, match exactly at 3.3M samples each against a measured noise floor of zero. The one pixel
    # difference a wider comparison turned up was a spin box's value (a mod-count-derived memory
    # suggestion), not a colour. tests/test_theme_apply_cache.py pins the clear-then-install sequence and
    # the palette-before-sheet ordering it depends on.
    # While this swap is in flight the application stylesheet is momentarily absent and Qt is walking
    # every widget to tell it so. A widget that styles itself (see title_bar._WindowButton) must not
    # install its own sheet during that walk: doing so mutates style data under Qt's feet and corrupted
    # the heap (0xc0000374) when a theme change came from inside a widget's signal handler - which is how
    # the settings page saves. Self-styling widgets register with register_self_styling() instead and are
    # told once the swap is complete.
    global _applying
    _applying = True
    try:
        if app.styleSheet():
            app.setStyleSheet("")
        app.setStyleSheet(text)
    finally:
        _applying = False
    # Queued, not called here. This runs immediately after an application-wide stylesheet swap, and when
    # the swap was triggered from inside a widget's own signal handler - saving the settings page, for
    # instance - installing a widget-level sheet before Qt has finished dispatching that signal corrupts the
    # heap. Measured: 0xc0000374, with the frame pointing at _WindowButton._restyle. One turn of the event
    # loop later it is safe, and the application sheet is already in place, so nothing looks unstyled in the
    # meantime. Deferring only this keeps the swap itself synchronous, which the existing tests rely on.
    QTimer.singleShot(0, _notify_self_styling)
    _last_applied = key
