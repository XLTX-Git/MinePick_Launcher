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

"""Every rail entry must have a glyph that actually renders.

``nav_icon()`` answers a null icon for a name it does not know, and a null icon draws as nothing -
no exception, no failed test, just an empty square the reviewer notices in a screenshot. The 投影原理图
entry shipped exactly that way: the section was added with an icon key nothing mapped.

These tests are the cheap guard for that class: the mapping exists, the file exists, and the render
comes back non-null. They are deliberately about the mapping table rather than about one screenshot,
so a new section cannot be added without its icon.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


def _sections() -> tuple[tuple[str, int, str, str], ...]:
    from gui.pages.instance_detail import _SECTIONS

    return _SECTIONS


def test_every_instance_sidebar_section_names_a_mapped_icon() -> None:
    """The icon key of each rail entry is in the mapping table."""
    from gui.nav_icons import ICON_FILES

    missing = [key for key, _tab, icon, _label in _sections() if icon not in ICON_FILES]
    assert missing == [], f"sidebar sections without an icon mapping: {missing}"


def test_every_mapped_icon_file_exists() -> None:
    """A mapping pointing at a file that is not shipped would also render as nothing."""
    from gui.nav_icons import ICON_FILES
    from launcher import paths

    for key, _tab, icon, _label in _sections():
        filename = ICON_FILES[icon]
        path = paths.resource_path(f"gui/resources/icons/nav/{filename}.svg")
        assert path.is_file(), f"{key} maps to {filename}.svg, which is missing"


def test_every_sidebar_icon_renders(app) -> None:
    """The render is non-null: a mapped file that Qt cannot parse is the same blank square."""
    from gui import nav_icons

    for key, _tab, icon, _label in _sections():
        rendered = nav_icons.nav_icon(icon, "#2f9e6f", 18)
        assert not rendered.isNull(), f"{key} renders a null icon from {icon!r}"
