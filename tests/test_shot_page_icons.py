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

"""The icon checks behind ``shot_pages.py --warm-cache --expect-icons``.

Two behaviours matter and are easy to get subtly wrong:

* ``wait_until`` has to pump the Qt event loop (a worker's result arrives through a queued
  connection) and still stop at its deadline instead of waiting forever;
* ``--expect-icons`` must fail when a row's ``_icon`` is still None - the cache file existing is not
  proof, because a failed decode leaves the row painting its letter tile.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import shot_pages


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


class _Row:
    """The two attributes the icon helpers read from a real ``_ResourceRow``."""

    def __init__(self, title: str, icon: object | None = None) -> None:
        self.title = title
        self.icon_url = "https://cdn.example.test/icon.png"
        self._icon = icon


def test_wait_until_returns_true_as_soon_as_the_predicate_holds(app):
    state = {"done": False}
    QTimer.singleShot(50, lambda: state.__setitem__("done", True))
    assert shot_pages.wait_until(lambda: state["done"], timeout_ms=2000) is True


def test_wait_until_stops_at_its_deadline(app):
    """A false predicate costs the timeout, not the default eight seconds."""
    started = time.monotonic()
    assert shot_pages.wait_until(lambda: False, timeout_ms=100) is False
    assert time.monotonic() - started < 1.5


def test_icons_ready_is_false_while_a_row_paints_its_letter_tile(monkeypatch):
    rows = [_Row("Sodium", icon=None), _Row("Iris", icon=object())]
    monkeypatch.setattr(shot_pages, "resource_rows", lambda page: rows)
    assert shot_pages.icons_ready(object()) is False
    assert [row.title for row in shot_pages.missing_icons(object())] == ["Sodium"]


def test_expect_icons_refuses_to_save_when_an_icon_did_not_load(monkeypatch):
    rows = [_Row("Sodium", icon=None), _Row("Iris", icon=object())]
    monkeypatch.setattr(shot_pages, "resource_rows", lambda page: rows)
    with pytest.raises(SystemExit, match="did not load"):
        shot_pages.assert_icons_loaded(object(), "page 'mods'")


def test_expect_icons_accepts_a_page_whose_rows_all_have_icons(monkeypatch):
    rows = [_Row("Sodium", icon=object()), _Row("Iris", icon=object())]
    monkeypatch.setattr(shot_pages, "resource_rows", lambda page: rows)
    shot_pages.assert_icons_loaded(object(), "page 'mods'")  # must not raise


def test_render_page_image_starts_every_pixel_at_the_page_paper(app):
    """A surface no widget paints is the page colour, not the platform's black.

    ``QWidget.render()`` paints the palette window background first, so for an ordinary widget the
    prefill is invisible either way. It is the surfaces that opt out of that fill - a translucent
    overlay or a ``WA_NoSystemBackground`` widget - where the image's initial colour is what the
    saved file shows. ``QWidget.grab()`` left those black (measured on this Qt); rendering into an
    image pre-filled with the current ``BG_PAGE`` leaves the paper.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QWidget

    from gui.theme import apply_theme, current_tokens

    apply_theme("light", None, None, "default")
    try:
        bare = QWidget()
        bare.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        bare.resize(40, 30)
        image = shot_pages.render_page_image(bare)
        assert image.size().width() == 40 and image.size().height() == 30
        assert image.format() == QImage.Format.Format_RGB32
        assert not image.hasAlphaChannel()
        paper = QColor(current_tokens()["BG_PAGE"])
        assert paper.name() != QColor(current_tokens()["BG_LIST"]).name()  # the test can tell them apart
        for point in ((0, 0), (39, 0), (0, 29), (39, 29), (20, 15)):
            assert image.pixelColor(*point) == paper
    finally:
        apply_theme("dark", None, None, "default")


def test_a_mod_icon_corner_shows_the_card_behind_it_in_the_rendered_image(app):
    """The published mods picture: a transparent project-icon corner is the card, not white.

    The real row paints its card and then draws the icon clipped to a rounded square, so a
    transparent icon pixel has to leave the card colour visible. The test samples four points inside
    that icon square but outside the artwork - the pixels the white ``icon.png`` used to fill.
    """
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication

    from gui.pages.mods_page import _ResourceRow
    from gui.theme import apply_theme, current_tokens
    from launcher.mods.models import ModSearchHit

    apply_theme("light", None, None, "default")
    row = None
    try:
        hit = ModSearchHit(
            slug="demo", title="Demo", description="demo row", downloads=1, icon_url=""
        )
        row = _ResourceRow(
            hit, "mod", "Modrinth", on_select=lambda: None, on_install=lambda: None
        )
        icon = QPixmap(48, 48)
        icon.fill(Qt.GlobalColor.transparent)
        painter = QPainter(icon)
        painter.fillRect(QRect(16, 16, 16, 16), QColor("#e05252"))
        painter.end()
        # The asset itself must have transparent corners, or the assertion below would pass for the
        # wrong reason (an opaque white icon paints white everywhere).
        assert icon.toImage().pixelColor(0, 0).alpha() == 0
        row.set_icon(icon)
        row.resize(520, row.SLOT_HEIGHT)
        row.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        row.show()
        for _ in range(4):
            QApplication.processEvents()

        image = shot_pages.render_page_image(row)
        icon_left = row.PADDING
        icon_top = (row.HEIGHT - row.ICON) // 2
        # inside the rounded clip at each corner, but outside the 16px artwork square
        for dx, dy in ((8, 2), (row.ICON - 9, 2), (8, row.ICON - 3), (row.ICON - 9, row.ICON - 3)):
            pixel = image.pixelColor(icon_left + dx, icon_top + dy)
            assert pixel.name() == current_tokens()["BG_LIST"], (
                f"icon corner at ({dx}, {dy}) is {pixel.name()}, not the card colour"
            )
    finally:
        if row is not None:
            row.close()
            row.deleteLater()
        apply_theme("dark", None, None, "default")
