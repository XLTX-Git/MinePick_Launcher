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

"""The card-corner pixel gate must fail on a square cover, not just pass on today's cards.

The tool's first run on this tree reports zero violations (the cards inset their lists), so
the gate needs a constructive negative: paint the three outermost pixels of one corner in the
card's fill and prove the checker reports that corner. The other corners must keep passing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import audit_card_radius as audit


def _paper() -> tuple[int, int, int]:
    return (246, 249, 252)


def _image(colour: tuple[int, int, int]) -> QImage:
    image = QImage(60, 60, QImage.Format.Format_RGB32)
    image.fill(QColor(*colour))
    return image


class _Card:
    def __init__(self, left: int, top: int, width: int = 20, height: int = 20) -> None:
        self._left = left
        self._top = top
        self._width = width
        self._height = height

    def mapTo(self, _window, _point):
        return QPoint(self._left, self._top)

    def isVisible(self) -> bool:
        # The audit skips cards the current page does not show; this stub models a shown one.
        return True

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


def test_a_rounded_card_corner_passes():
    entry = audit._check_card(_Card(10, 10), None, _image(_paper()), _paper(), "java")
    assert entry["ok"] is True
    assert entry["skipped"] is False


def test_a_square_cover_on_one_corner_fails_that_corner_only():
    image = _image(_paper())
    card = _Card(10, 10)
    # A flush child painting the card's own fill over the outermost 3x3 of the top-left corner.
    for y in range(10, 13):
        for x in range(10, 13):
            image.setPixelColor(x, y, QColor(255, 255, 255))
    entry = audit._check_card(card, None, image, _paper(), "java")
    assert entry["ok"] is False
    assert entry["corners"]["TL"]["failures"]
    assert entry["corners"]["TR"]["failures"] == []
    assert entry["corners"]["BL"]["failures"] == []
    assert entry["corners"]["BR"]["failures"] == []
    assert entry["corners"]["TL"]["failures"][0]["rgb"] == [255, 255, 255]


def test_the_light_card_fill_is_outside_the_paper_tolerance():
    """The light card is only 9/6/3 from the paper; the gate must not excuse that as antialiasing."""
    assert audit._diff((255, 255, 255), _paper()) > audit.PAPER_TOLERANCE


def test_each_corner_samples_three_pixels_along_both_outer_edges():
    points = audit._corner_points(10, 10, 29, 29)
    assert set(points) == {"TL", "TR", "BL", "BR"}
    for corner in points.values():
        assert len(corner) == 5
    # The inner diagonal of the 3x3 block is the antialiased arc, not a corner pixel.
    assert (12, 12) not in points["TL"]

def test_an_invisible_card_is_skipped() -> None:
    """A card on a hidden page must not be measured against the page that is on screen."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "audit_card_radius", Path(__file__).resolve().parent.parent / "tools/audit_card_radius.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Hidden(_Card):
        def isVisible(self) -> bool:
            return False

    from PySide6.QtGui import QImage

    image = QImage(40, 40, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    entry = module._check_card(Hidden(5, 5), None, image, (255, 255, 255), "page")
    assert entry["skipped"] is True
