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

"""Custom accent color and UI font: parsing, palette derivation and QSS placeholders."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from gui import theme
from launcher import paths


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def test_normalize_hex_accepts_and_rejects() -> None:
    assert theme.normalize_hex("35a06a") == "#35a06a"
    assert theme.normalize_hex("#35A06A") == "#35a06a"
    assert theme.normalize_hex("  #ff8800  ") == "#ff8800"
    assert theme.normalize_hex("") is None
    assert theme.normalize_hex(None) is None
    assert theme.normalize_hex("#12345") is None
    assert theme.normalize_hex("zzzzzz") is None


def test_accent_palette_uses_the_custom_base() -> None:
    palette = theme.accent_palette("dark", "#3b82f6")
    assert palette["ACCENT"] == "#3b82f6"
    assert palette["ACCENT_SEL_TEXT"] == "#ffffff"
    # derived steps must differ from the base and stay valid hex colors.
    # ACCENT_PRESS used to be listed here; 4-B1 deleted it because no rule in either sheet referenced
    # it - the pressed fill is ACCENT_BUTTON_PRESS - so it was a colour nothing could draw.
    for key in ("ACCENT_HOVER", "ACCENT_SEL", "ACCENT_SOFT"):
        value = palette[key]
        assert value != palette["ACCENT"]
        assert theme.normalize_hex(value) == value


def test_focus_ring_clears_non_text_contrast() -> None:
    """WCAG 1.4.11 wants 3:1 for the focus indicator.

    A bright accent against a light surface reads 1.96:1, so the light theme draws its ring with the
    same darkened fill its buttons use; the dark theme keeps the raw accent, which already passes.
    """
    light = theme.accent_palette("light", "#35a06a")
    dark = theme.accent_palette("dark", "#35a06a")
    assert light["FOCUS_RING"] == light["ACCENT_BUTTON"]
    assert light["FOCUS_RING"] != light["ACCENT"]
    assert dark["FOCUS_RING"] == dark["ACCENT"]


def test_accent_palette_falls_back_to_theme_default() -> None:
    assert theme.accent_palette("dark", None)["ACCENT"] == theme.DEFAULT_ACCENTS["dark"]
    assert theme.accent_palette("light", "not-a-color")["ACCENT"] == theme.DEFAULT_ACCENTS["light"]


def test_light_palette_keeps_text_readable() -> None:
    palette = theme.accent_palette("light", "#2f9e6f")
    # light theme selection uses a pale tint with a dark foreground
    assert palette["ACCENT_SEL"] > palette["ACCENT"]
    assert palette["ACCENT_SEL_TEXT"] < palette["ACCENT"]


def test_font_stack_default_and_custom() -> None:
    stack = theme.font_stack(None)
    assert theme.DEFAULT_FONT in stack
    # Latin takes Segoe UI's narrower glyphs; YaHei stays behind it for the Chinese.
    assert stack.startswith('"Segoe UI"')
    assert stack.index("Segoe UI") < stack.index(theme.DEFAULT_FONT)
    assert theme.DEFAULT_FONT in theme.font_stack("")
    assert theme.font_stack("Consolas").startswith('"Consolas"')
    # A user-picked family stays primary; Segoe UI follows it as the Latin fallback.
    custom = theme.font_stack("Consolas")
    assert custom.index("Consolas") < custom.index("Segoe UI")
    # fallbacks stay in place so unknown families still resolve
    assert "Segoe UI" in theme.font_stack("No Such Font 12345")


def test_font_stack_keeps_the_non_chinese_fallbacks() -> None:
    """Japanese and Korean have no glyphs in YaHei or Segoe UI: their faces must stay in the chain."""
    stack = theme.font_stack(None)
    for family in ("Malgun Gothic", "Yu Gothic UI", "Microsoft YaHei", "sans-serif"):
        assert family in stack, family


def test_stylesheets_use_placeholders_not_hardcoded_accent() -> None:
    """Regression guard: the QSS files must stay re-tintable."""
    for name, accent in (("style.qss", "#35a06a"), ("style_light.qss", "#2f9e6f")):
        text = paths.resource_path("gui/resources/" + name).read_text(encoding="utf-8")
        assert "__ACCENT__" in text, name
        assert "__FONT__" in text, name
        assert accent not in text, f"{name} still hardcodes {accent}"


def test_radius_presets_and_placeholders(monkeypatch, ws_tmp) -> None:
    """Every radius preset must produce a full set of sizes, and the QSS must stay placeholder-based."""
    from gui import theme
    from launcher import paths

    assert theme.radius_scale(None) == theme.RADIUS_SCALES["default"]
    assert theme.radius_scale("nonsense") == theme.RADIUS_SCALES["default"]
    small, medium, large = (theme.radius_scale(name) for name in ("compact", "default", "round"))
    assert small[0] < medium[0] < large[0]
    assert small[2] < medium[2] < large[2]

    for name in ("style.qss", "style_light.qss"):
        text = paths.resource_path("gui/resources/" + name).read_text(encoding="utf-8")
        assert "__RADIUS_SM__" in text and "__RADIUS_LG__" in text, name
        assert "border-radius: 6px" not in text, name  # still hard-coded


def test_system_theme_resolves_to_a_concrete_theme() -> None:
    from gui import theme

    assert theme.resolve_theme("dark") == "dark"
    assert theme.resolve_theme("light") == "light"
    assert theme.resolve_theme("system") in ("dark", "light")


def test_the_preset_swatch_grid_is_gone(app, monkeypatch, ws_tmp) -> None:
    """The colour dialog already has preset grids; the page must not draw a second one."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_presets"))
    from PySide6.QtCore import QPoint

    from gui.main_window import MainWindow

    window = MainWindow()
    page = window.pages["settings"]
    assert not hasattr(page, "accent_preset_buttons")
    assert not hasattr(page, "title_bar_preset_buttons")
    # Both colour rows keep the same shape and order: field, preview swatch, reset, picker.
    accent = [
        page.accent_edit,
        page.accent_swatch,
        page.accent_default_button,
        page.accent_pick_button,
    ]
    title = [
        page.title_bar_edit,
        page.title_bar_swatch,
        page.title_bar_default_button,
        page.title_bar_pick_button,
    ]
    for good, bad in zip(accent, title):
        assert type(good) is type(bad)
    assert page.accent_default_button.text() == page.title_bar_default_button.text()
    assert page.accent_pick_button.text() == page.title_bar_pick_button.text()

    # Same grid: every corresponding control has the same x and width once the page is laid out.
    window.resize(1000, 610)
    window.show()
    for _ in range(6):
        app.processEvents()
    for left, right in zip(accent, title):
        left_x = left.mapTo(page, QPoint(0, 0)).x()
        right_x = right.mapTo(page, QPoint(0, 0)).x()
        assert left_x == right_x, f"{type(left).__name__} is not on the shared grid"
        assert left.width() == right.width()
    window.close()
