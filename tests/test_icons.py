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

"""Icon-only buttons must actually draw something: a painter mistake fails silently."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from gui import icons


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def test_every_glyph_paints_pixels(app):
    for kind in icons.KINDS:
        qicon = icons.icon(kind, 20, color="#ffffff")
        assert not qicon.isNull(), f"{kind}: no icon"
        image = qicon.pixmap(20, 20).toImage()
        painted = sum(
            1
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha() > 0
        )
        assert painted > 12, f"{kind} painted almost nothing ({painted} pixels)"


def test_unknown_kind_returns_a_blank_icon_instead_of_raising(app):
    """Unknown kinds must not raise; they paint nothing (a blank pixmap, not a null icon)."""
    image = icons.icon("does-not-exist", 20, color="#ffffff").pixmap(20, 20).toImage()
    painted = sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )
    assert painted == 0


def test_every_navigation_icon_renders_its_svg(app):
    """All eight bundled Lucide files must resolve and actually paint; a broken path fails here."""
    from gui import nav_icons

    for name in nav_icons.ICON_FILES:
        icon = nav_icons.nav_icon(name, "#2f9e6f", 24)
        assert not icon.isNull(), f"{name}: no icon"
        image = icon.pixmap(24, 24).toImage()
        painted = sum(
            1
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha() > 0
        )
        assert painted > 12, f"{name} painted almost nothing ({painted} pixels)"


def test_navigation_icon_colour_is_part_of_the_cache_key(app):
    """A theme switch must not hand back the old-coloured pixmap."""
    from gui import nav_icons

    light = nav_icons.nav_icon("launch", "#2f9e6f", 24).pixmap(24, 24).toImage()
    dark = nav_icons.nav_icon("launch", "#e6ecf5", 24).pixmap(24, 24).toImage()
    assert light != dark, "the icon cache ignored the requested colour"


def test_navigation_svgs_keep_their_licence_comment():
    """The bundled files are third-party assets: their ISC header must ship with them."""
    from gui import nav_icons
    from launcher import paths

    for filename in nav_icons.ICON_FILES.values():
        text = paths.resource_path(f"gui/resources/icons/nav/{filename}.svg").read_text(
            encoding="utf-8"
        )
        assert "@license lucide-static" in text, f"{filename}: missing licence header"
        assert "ISC" in text, f"{filename}: missing ISC notice"
