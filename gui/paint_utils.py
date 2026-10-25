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

"""Painted pixmaps for the launch page: version block, avatar fallback and disclosure chevron.

Everything here is drawn with QPainter. The launch page must not ship an external image (no PNG/SVG
asset, no network), so the version icon is an isometric block, the avatar falls back to the same
hue-from-name disc the account page uses, and the disclosure chevron is a drawn glyph. Colours come
from ``theme.current_tokens()``, the table the stylesheet is built from, so the pixmaps follow the
user's accent without a second palette.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication

from gui import theme

#: Bumped whenever a cached pixmap would have to be redrawn; the cache key uses the colour anyway.
_BLOCK_CACHE: dict[tuple[int, str], QPixmap] = {}
_CHEVRON_CACHE: dict[tuple[bool, str], QPixmap] = {}
_LETTER_CACHE: dict[tuple[str, int, str, str], QPixmap] = {}


def _tokens() -> dict[str, str]:
    """The colour table of the theme in force (the light one before the first apply)."""
    tokens = theme.current_tokens()
    return tokens or theme.theme_palette("light")


def _blend(first: str, second: str, ratio: float) -> QColor:
    """Blend two tokens, ratio 0 = first, 1 = second."""
    left = QColor(first)
    right = QColor(second)
    return QColor(
        round(left.red() + (right.red() - left.red()) * ratio),
        round(left.green() + (right.green() - left.green()) * ratio),
        round(left.blue() + (right.blue() - left.blue()) * ratio),
    )


def _pixmap(size: int) -> tuple[QPixmap, float]:
    """A transparent pixmap of ``size`` logical pixels, device-pixel-ratio aware."""
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    ratio = max(1.0, float(screen.devicePixelRatio()) if screen is not None else 1.0)
    pixmap = QPixmap(max(1, round(size * ratio)), max(1, round(size * ratio)))
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap, ratio


def block_pixmap(size: int = 30) -> QPixmap:
    """The version icon: a small isometric block built from the accent colour.

    The mock-up asks for "a 24-32 px block stand-in" when the repository has no real version icon;
    drawing it keeps the launch page free of shipped artwork and lets it follow the accent.
    """
    accent = _tokens().get("ACCENT", "#2f9e6f")
    key = (size, accent)
    cached = _BLOCK_CACHE.get(key)
    if cached is not None:
        return cached
    pixmap, _ratio = _pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    edge = _blend(accent, "#ffffff", 0.55)
    pen = QPen(edge)
    pen.setWidthF(max(1.0, size / 22.0))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    top = size * 0.09
    half = size * 0.40
    rise = size * 0.20
    depth = size * 0.34
    center = size * 0.50
    upper = QPolygonF(
        [
            QPointF(center, top),
            QPointF(center + half, top + rise),
            QPointF(center, top + rise * 2),
            QPointF(center - half, top + rise),
        ]
    )
    left = QPolygonF(
        [
            QPointF(center - half, top + rise),
            QPointF(center, top + rise * 2),
            QPointF(center, top + rise * 2 + depth),
            QPointF(center - half, top + rise + depth),
        ]
    )
    right = QPolygonF(
        [
            QPointF(center + half, top + rise),
            QPointF(center, top + rise * 2),
            QPointF(center, top + rise * 2 + depth),
            QPointF(center + half, top + rise + depth),
        ]
    )
    painter.setBrush(_blend(accent, "#ffffff", 0.30))
    painter.drawPolygon(upper)
    painter.setBrush(_blend(accent, "#ffffff", 0.02))
    painter.drawPolygon(left)
    painter.setBrush(_blend(accent, "#000000", 0.14))
    painter.drawPolygon(right)
    painter.end()
    _BLOCK_CACHE[key] = pixmap
    return pixmap


def avatar_pixmap(username: str, size: int = 36) -> QPixmap | None:
    """The account avatar fallback: the first letter in the account's own disc.

    The account page already owns this design (``gui/pages/login_page.py``): the hue is derived from
    the name and ``AVATAR_VALUE`` is the number that keeps the white letter above 4.5:1 for all 360
    hues. Re-using its constants keeps the two avatars the same color for the same account. A real
    skin is not fetched here - the launch page must stay offline, and the account page still upgrades
    its own avatar when the network is available.
    """
    letter = (username.strip()[:1] or "").upper()
    if not letter:
        return None
    from gui.pages.login_page import AVATAR_HUE_STEP, AVATAR_SATURATION, AVATAR_VALUE

    pixmap, _ratio = _pixmap(max(32, size))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    hue = (sum(ord(char) for char in username) * AVATAR_HUE_STEP) % 360
    painter.setBrush(QColor.fromHsv(hue, AVATAR_SATURATION, AVATAR_VALUE))
    painter.setPen(Qt.PenStyle.NoPen)
    extent = pixmap.width() / pixmap.devicePixelRatio()
    painter.drawEllipse(QRectF(0, 0, extent, extent))
    painter.setPen(QColor(_tokens().get("FG_SELECTED", "#ffffff")))
    font = QFont()
    font.setPixelSize(max(12, round(extent * 0.46)))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, extent, extent), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return pixmap


def letter_tile_pixmap(text: str, size: int = 48, radius: int = 8) -> QPixmap:
    """A rounded-square placeholder icon carrying the first character of ``text``.

    The resource list shows a project's real icon when the API returned an ``icon_url`` and falls back
    to this otherwise. Like the version block and the avatar above, it is *drawn*: the launcher ships
    no per-project artwork, and painting it means the tile follows the accent colour of the theme.
    ``radius`` is a corner radius in logical pixels (the list asks for 8 on a 48px tile).
    """
    letter = (text.strip()[:1] or "?").upper()
    tokens = _tokens()
    background = tokens.get("ACCENT_TINT", "#e6f4ec")
    foreground = tokens.get("ACCENT_BUTTON", tokens.get("ACCENT", "#2f9e6f"))
    key = (letter, size, radius, background + foreground)
    cached = _LETTER_CACHE.get(key)
    if cached is not None:
        return cached
    pixmap, _ratio = _pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(background))
    extent = pixmap.width() / pixmap.devicePixelRatio()
    painter.drawRoundedRect(QRectF(0, 0, extent, extent), radius, radius)
    font = QFont()
    font.setPixelSize(max(12, round(extent * 0.42)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(foreground))
    painter.drawText(QRectF(0, 0, extent, extent), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    _LETTER_CACHE[key] = pixmap
    return pixmap


def chevron_pixmap(expanded: bool, size: int = 10) -> QPixmap:
    """A small chevron for the advanced-section toggle (down = collapsed, up = expanded).

    A drawn glyph is used instead of a triangle character: the UI font has no guaranteed U+25BE,
    and a missing glyph would show a tofu box in the middle of the button text.
    """
    color = _tokens().get("FG_HINT", "#5b6a7b")
    key = (expanded, color)
    cached = _CHEVRON_CACHE.get(key)
    if cached is not None:
        return cached
    pixmap, _ratio = _pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.4, size / 7.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    inset = size * 0.18
    if expanded:
        points = [
            QPointF(inset, size - inset * 1.6),
            QPointF(size / 2, inset * 1.6),
            QPointF(size - inset, size - inset * 1.6),
        ]
    else:
        points = [
            QPointF(inset, inset * 1.6),
            QPointF(size / 2, size - inset * 1.6),
            QPointF(size - inset, inset * 1.6),
        ]
    painter.drawPolyline(QPolygonF(points))
    painter.end()
    _CHEVRON_CACHE[key] = pixmap
    return pixmap
