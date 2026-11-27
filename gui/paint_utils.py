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

"""Painted pixmaps and the shared avatar helpers: version block, letter fallback, skin face, chevron.

Everything here is drawn with QPainter. The launch page must not ship an external image (no PNG/SVG
asset, no network), so the version icon is an isometric block, the avatar falls back to the same
hue-from-name disc the account page uses, and the disclosure chevron is a drawn glyph. Colours come
from ``theme.current_tokens()``, the table the stylesheet is built from, so the pixmaps follow the
user's accent without a second palette.

This module also owns the one downloaded-skin cache the two account avatars share (the launch page's
36px face and the account page's 42px one): one request per URL per process, and a failed texture
falls back to the letter without leaving an empty slot.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
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
_CUBE_CACHE: dict[tuple[int, str], QPixmap] = {}
#: Decoded skin textures by URL. The download itself lives with the pages; this only keeps the result,
#: so the same skin is fetched once for the whole process and every avatar crops its own face from it.
_SKIN_CACHE: dict[str, QPixmap] = {}
#: Callbacks waiting for an in-flight URL. The first waiter is the one that has to start the download.
_SKIN_WAITERS: dict[str, list[Callable[[QPixmap | None], None]]] = {}

#: The "no artwork" cube: 66% of its slot, thin edges, and FG_HINT at 65% so it sits lightly on the
#: row instead of reading as a missing-image block. Both numbers are visual constants from the PCL
#: comparison, not measurements of a specific font.
WIRE_CUBE_EXTENT = 0.66
WIRE_CUBE_ALPHA = 0.65


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


def block_pixmap(size: int = 30, *, color: str | None = None) -> QPixmap:
    """The version icon: a small isometric block built from the accent colour.

    The mock-up asks for "a 24-32 px block stand-in" when the repository has no real version icon;
    drawing it keeps the launch page free of shipped artwork and lets it follow the accent.

    ``color`` overrides the accent. It exists for the version categories: a machine with no block
    texture at all used to paint one accent-coloured cube for every row, so a release, a snapshot and
    a 0.x version looked identical and the page stopped saying what each row is. The caller that knows
    the category passes that block's own colour instead.
    """
    accent = color or _tokens().get("ACCENT", "#2f9e6f")
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


def skin_face_pixmap(skin: QPixmap, size: int = 42) -> QPixmap | None:
    """Crop just the head out of a Minecraft skin texture (hat overlay included).

    A modern skin is 64x64 (HD skins are 128x128, 256x256...): the head's front face sits at
    (8, 8) and its hat overlay at (40, 8), both 8x8 units. Legacy 64x32 skins have no overlay.
    Nearest-neighbour scaling keeps the pixel-art look. Returns None for textures too small to
    be a skin (the caller then keeps the letter avatar).

    ``size`` defaults to the account page's 42px slot; the launch page passes its own 36. The
    function is shared by both pages so a skin is cropped by one rule, not two copies that drift.
    """
    if skin.isNull() or skin.width() < 64 or skin.height() < 32:
        return None
    unit = max(1, skin.width() // 64)  # HD skins store the same layout at a larger scale
    face = skin.copy(QRect(8 * unit, 8 * unit, 8 * unit, 8 * unit))
    if face.isNull():
        return None
    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    target = QRect(0, 0, size, size)
    painter.drawPixmap(target, face)
    if skin.height() >= 64 * unit:  # legacy 64x32 textures carry no hat layer
        painter.drawPixmap(target, skin.copy(QRect(40 * unit, 8 * unit, 8 * unit, 8 * unit)))
    painter.end()
    return canvas


def cached_skin(skin_url: str) -> QPixmap | None:
    """The decoded skin texture remembered for ``skin_url``, or None before the download lands."""
    if not skin_url:
        return None
    return _SKIN_CACHE.get(skin_url)


def skin_face_for(skin_url: str, size: int) -> QPixmap | None:
    """The cached skin's face at ``size``; None when it is not downloaded yet or is not a skin."""
    skin = cached_skin(skin_url)
    return skin_face_pixmap(skin, size) if skin is not None else None


def watch_skin(skin_url: str, callback: Callable[[QPixmap | None], None]) -> bool:
    """Show a skin as soon as it is available, downloading each URL at most once.

    ``callback`` runs on the GUI thread: immediately with the decoded texture when the URL is already
    cached, or when the single in-flight download finishes (with None if it failed). The return value
    says whether the caller must start that download - never start a second one for the same URL.

    The callbacks belong to widgets, so a page that is rebuilt can have its labels destroyed before
    the download finishes; a ``RuntimeError`` from one waiter (Qt's deleted-object error) must not
    keep the others from getting the texture.
    """
    if not skin_url:
        callback(None)
        return False
    skin = _SKIN_CACHE.get(skin_url)
    if skin is not None:
        callback(skin)
        return False
    waiters = _SKIN_WAITERS.setdefault(skin_url, [])
    waiters.append(callback)
    return len(waiters) == 1


def finish_skin(skin_url: str, data: bytes | None) -> QPixmap | None:
    """Decode a finished download, remember it and hand it to every waiting avatar.

    ``data`` is None when the request failed: the waiters are released with None and the letter
    stays, so a failed skin never leaves an empty slot behind. An image too small to be a skin is
    treated exactly like a failed request.
    """
    skin: QPixmap | None = None
    if data:
        candidate = QPixmap()
        if candidate.loadFromData(data):
            skin = candidate
    if skin is not None and skin_url:
        _SKIN_CACHE[skin_url] = skin
    for callback in _SKIN_WAITERS.pop(skin_url, []):
        try:
            callback(skin)
        except RuntimeError:
            pass  # the page that asked was rebuilt/closed; the other waiters still get the texture
    return skin


def clear_skin_cache() -> None:
    """Forget every remembered skin and waiter (tests: the cache is process-wide on purpose)."""
    _SKIN_CACHE.clear()
    _SKIN_WAITERS.clear()


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


def wire_cube_pixmap(size: int = 32) -> QPixmap:
    """The neutral "no artwork" mark: a thin isometric cube outline, centred in its slot.

    This is the stand-in for a resource pack without a ``pack.png``. It is deliberately *only* an
    outline: no filled faces, no rounded background, no border box, because on a list row those read
    as "something is missing here". The cube covers :data:`WIRE_CUBE_EXTENT` of the slot and is drawn
    in FG_HINT at :data:`WIRE_CUBE_ALPHA`, so it is present but never louder than a real pack icon.
    Geometry: the eight visible corners of a cube, projected isometrically.
    """
    hint = QColor(_tokens().get("FG_HINT", "#5b6a7b"))
    hint.setAlphaF(WIRE_CUBE_ALPHA)
    key = (size, hint.name(QColor.NameFormat.HexArgb))
    cached = _CUBE_CACHE.get(key)
    if cached is not None:
        return cached
    pixmap, _ratio = _pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(hint)
    pen.setWidthF(max(1.0, size / 26.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    extent = size * WIRE_CUBE_EXTENT
    center = size / 2.0
    top = (size - extent) / 2.0
    half = extent / 2.0
    rise = extent * 0.25
    drop = extent * 0.50
    top_vertex = QPointF(center, top)
    right_vertex = QPointF(center + half, top + rise)
    middle_vertex = QPointF(center, top + rise * 2)
    left_vertex = QPointF(center - half, top + rise)
    lower_left = QPointF(center - half, top + rise + drop)
    lower_middle = QPointF(center, top + rise * 2 + drop)
    lower_right = QPointF(center + half, top + rise + drop)
    # The top rhombus, the three verticals and the two lower edges: exactly the visible edges of a
    # wireframe cube, with no face fills.
    for start, end in (
        (top_vertex, right_vertex),
        (right_vertex, middle_vertex),
        (middle_vertex, left_vertex),
        (left_vertex, top_vertex),
        (left_vertex, lower_left),
        (middle_vertex, lower_middle),
        (right_vertex, lower_right),
        (lower_left, lower_middle),
        (lower_middle, lower_right),
    ):
        painter.drawLine(start, end)
    painter.end()
    _CUBE_CACHE[key] = pixmap
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
