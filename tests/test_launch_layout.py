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

"""The launch page's action group sits below the middle, and stays a stretch, not a fixed gap.

The launch sheet asks for the primary action and its two secondary siblings to sit about a third
below the panel's centre. Before this the two ``addStretch(1)`` around them centred the group exactly;
now the upper spring is twice the lower one (2:1), which puts the group two thirds of the free space
down. These tests pin both facts that matter:

* the spring factors are 2 and 1, so a future edit cannot silently return to dead-centre;
* growing the window moves the group by the extra free space's sixth (the difference between 2/3 and
  1/2 of it) instead of pinning it to a fixed offset, and a shorter window still keeps every control
  inside the card.

No account, no version and no network: this is a geometry test, rendered offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

SHORT_HEIGHT = 610
TALL_HEIGHT = 900
SQUASHED_HEIGHT = 460


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def page(app, monkeypatch, ws_tmp):
    """A launched-and-laid-out page with nothing installed, so the card has its shortest content."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "game"
    game.mkdir()
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))
    from PySide6.QtCore import Qt

    from gui import i18n
    from gui.pages.launch_page import LaunchPage
    from gui.theme import apply_theme

    apply_theme("light", None, None, "default")
    i18n.set_language("zh_cn")

    page = LaunchPage()
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    page.resize(1000, SHORT_HEIGHT)
    page.show()
    _settle(app)
    try:
        yield page
    finally:
        page.close()
        page.deleteLater()
        i18n.set_language("zh_cn")
        apply_theme("dark", None, None, "default")


def _settle(app) -> None:
    for _ in range(8):
        app.processEvents()


def _top(widget, page) -> int:
    return widget.mapTo(page.panel, QPoint(0, 0)).y()


def _bottom(widget, page) -> int:
    return _top(widget, page) + widget.height()


def _spring_factors(page) -> list[int]:
    layout = page.panel.layout()
    return [layout.stretch(index) for index in range(layout.count()) if layout.stretch(index) > 0]


def test_the_actions_hang_from_a_two_to_one_spring_pair(page):
    """The design decision itself: twice as much air above the buttons as below them."""
    assert _spring_factors(page) == [2, 1], (
        "the action group is not positioned by a 2:1 stretch pair; the page would centre it again "
        "or pin it with a fixed spacing"
    )


def test_the_group_sits_a_third_below_the_middle(page, app):
    """Measured, not just declared: the space above the group is twice the space below it."""
    _settle(app)
    above = _top(page.launch_button, page) - _bottom(page.avatar_label, page)
    # The 4px fixed gap between the lower spring and the last-launch label is not part of the spring.
    below = _top(page.last_launch_label, page) - _bottom(page.select_instance_button, page) - 4
    assert above > below, "the action group still sits above the middle"
    assert above == pytest.approx(2 * below, abs=6), (
        f"the springs did not settle at 2:1 (above={above}px, below={below}px)"
    )


def test_growing_the_window_moves_the_group_with_the_upper_spring(page, app):
    """A stretch, not a fixed offset: a taller window must not pin the buttons to the top."""
    short_top = _top(page.launch_button, page)
    page.resize(1000, TALL_HEIGHT)
    _settle(app)
    tall_top = _top(page.launch_button, page)

    extra = TALL_HEIGHT - SHORT_HEIGHT
    assert tall_top > short_top, "the action group did not follow the taller window"
    # The new free space splits 2:1, so the group itself takes two thirds of it.
    assert tall_top - short_top == pytest.approx(extra * 2 / 3, abs=4)


def test_a_squashed_window_still_keeps_the_group_inside_the_card(page, app):
    """The lower third must not trade away the short-window layout: everything stays in the panel."""
    page.resize(1000, SQUASHED_HEIGHT)
    _settle(app)
    bounds = page.panel.rect()
    for name in (
        "version_label",
        "version_meta",
        "account_label",
        "launch_button",
        "select_instance_button",
        "instance_settings_button",
        "last_launch_label",
    ):
        widget = getattr(page, name)
        top_left = widget.mapTo(page.panel, QPoint(0, 0))
        bottom_right = widget.mapTo(page.panel, QPoint(widget.width() - 1, widget.height() - 1))
        assert bounds.contains(top_left), f"{name} starts outside the card: {top_left}"
        assert bounds.contains(bottom_right), f"{name} ends outside the card: {bottom_right}"
        assert widget.height() > 0, f"{name} was squeezed to nothing"
