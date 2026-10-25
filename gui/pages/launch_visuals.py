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

"""Painted visuals for the launch page: version block, avatar, chevron, right-hand artwork.

Everything here is drawn with QPainter. The launch page mock-up must not ship an external image
(no PNG/SVG asset, no network), so the version icon is an isometric block, the avatar falls back to
the same hue-from-name disc the account page uses, and the right column is generated from the theme's
own colours. Colours come from ``theme.current_tokens()``, the table the stylesheet is built from, so
the artwork follows the user's accent without a second palette.

The artwork is deliberately light-theme only. The dark sheet is not part of this round, so the
widget hides itself whenever the applied palette is dark (``LaunchArtwork._sync_theme``); it never
paints its pale gradient over a dark page.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

from gui import theme

#: Bumped whenever a cached pixmap would have to be redrawn; the cache key uses the colour anyway.
_BLOCK_CACHE: dict[tuple[int, str], QPixmap] = {}
_CHEVRON_CACHE: dict[tuple[bool, str], QPixmap] = {}


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


#: Block silhouettes in the artwork, as (centre-x, centre-y, edge length, depth, alpha) fractions
#: of the panel. Hand-placed so the composition works at every size: two large blocks in the middle
#: ground, smaller ones in front, one half-off the right edge.
_BLOCKS: tuple[tuple[float, float, float, float, float], ...] = (
    (0.78, 0.20, 0.34, 0.55, 0.26),
    (0.40, 0.40, 0.46, 0.60, 0.18),
    (0.82, 0.58, 0.40, 0.52, 0.22),
    (0.30, 0.72, 0.30, 0.58, 0.16),
    (0.60, 0.86, 0.52, 0.34, 0.12),
)


class LaunchArtwork(QWidget):
    """The decorative right column: pixel grid, block silhouettes and a pale accent gradient.

    Contract:
    * no text, no controls, no image file, no network - ``paintEvent`` is the whole widget;
    * it scales with the panel: every coordinate is a fraction of the current size;
    * it paints only while the applied palette is light, and hides itself otherwise. The dark theme
      is out of scope this round, and a pale green panel dropped onto a dark page would not be a
      dark-theme design;
    * it is mouse-transparent and takes no focus, so it never adds a keyboard stop or a tooltip.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("launchArtwork")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._radius = 8
        self._active = True
        self._sync_theme()
        theme.register_self_styling(self)

    # ---------- sizing ----------

    def sizeHint(self) -> QSize:
        return QSize(520, 420)

    def minimumSizeHint(self) -> QSize:
        # 0 on purpose: the artwork gives way first when a long translation needs the width, and the
        # launch page's own minimum width must not be set by a decorative panel.
        return QSize(0, 0)

    # ---------- theme ----------

    def on_theme_applied(self) -> None:
        """Re-read the applied palette after every theme swap (registered with gui.theme)."""
        self._sync_theme()

    def _sync_theme(self) -> None:
        from launcher import config

        cfg, _ = config.load()
        self._radius = theme.radius_scale(cfg.ui_radius)[2]
        self._active = _palette_is_light()
        self.setVisible(self._active)
        self.update()

    # ---------- painting ----------

    def paintEvent(self, _event) -> None:
        if not self._active or self.width() <= 0 or self.height() <= 0:
            return
        tokens = _tokens()
        accent = tokens.get("ACCENT", "#2f9e6f")
        white = tokens.get("BG_LIST", "#ffffff")
        pale_blue = tokens.get("BG_SETTINGS_BOTTOM", "#e3edf9")

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        area = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(area, self._radius, self._radius)
        painter.setClipPath(clip)

        gradient = QLinearGradient(0.0, area.top(), 0.0, area.bottom())
        gradient.setColorAt(0.0, _blend(accent, white, 0.80))
        gradient.setColorAt(0.42, _blend(accent, white, 0.94))
        gradient.setColorAt(0.72, white)
        gradient.setColorAt(1.0, _blend(pale_blue, white, 0.15))
        painter.fillRect(area, gradient)

        self._paint_grid(painter, area, accent)
        for index, (cx, cy, length, depth, alpha) in enumerate(_BLOCKS):
            self._paint_block(painter, area, index, cx, cy, length, depth, alpha, accent)
        painter.end()

    @staticmethod
    def _paint_grid(painter: QPainter, area: QRectF, accent: str) -> None:
        """A very faint pixel grid: the 'graph paper' under the blocks."""
        step = max(14.0, area.width() / 20.0)
        grid = QColor(accent)
        grid.setAlpha(14)
        painter.setPen(QPen(grid, 1.0))
        x = area.left() + step
        while x < area.right():
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
            x += step
        y = area.top() + step
        while y < area.bottom():
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            y += step

    @staticmethod
    def _paint_block(
        painter: QPainter,
        area: QRectF,
        index: int,
        cx: float,
        cy: float,
        length: float,
        depth: float,
        alpha: float,
        accent: str,
    ) -> None:
        """One semi-transparent isometric block, sized as a fraction of the panel's short edge."""
        edge = min(area.width(), area.height()) * length
        if edge <= 2.0:
            return
        half = edge / 2.0
        rise = edge * 0.30
        drop = edge * depth
        center_x = area.left() + area.width() * cx
        center_y = area.top() + area.height() * cy
        # Every second block sits a little higher, so the silhouette does not read as one row.
        center_y -= edge * 0.08 if index % 2 else 0.0

        top_face = QPolygonF(
            [
                QPointF(center_x, center_y - rise - drop),
                QPointF(center_x + half, center_y - rise * 0.55 - drop),
                QPointF(center_x, center_y - rise * 0.10 - drop),
                QPointF(center_x - half, center_y - rise * 0.55 - drop),
            ]
        )
        left_face = QPolygonF(
            [
                QPointF(center_x - half, center_y - rise * 0.55 - drop),
                QPointF(center_x, center_y - rise * 0.10 - drop),
                QPointF(center_x, center_y - rise * 0.10),
                QPointF(center_x - half, center_y - rise * 0.55),
            ]
        )
        right_face = QPolygonF(
            [
                QPointF(center_x + half, center_y - rise * 0.55 - drop),
                QPointF(center_x, center_y - rise * 0.10 - drop),
                QPointF(center_x, center_y - rise * 0.10),
                QPointF(center_x + half, center_y - rise * 0.55),
            ]
        )
        top = QColor(accent)
        top.setAlphaF(min(1.0, alpha * 1.35))
        left = QColor(accent)
        left.setAlphaF(alpha)
        right = QColor(accent)
        right.setAlphaF(alpha * 0.65)
        outline = QColor(accent)
        outline.setAlphaF(min(1.0, alpha * 1.6))
        pen = QPen(outline, 1.0)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(top)
        painter.drawPolygon(top_face)
        painter.setBrush(left)
        painter.drawPolygon(left_face)
        painter.setBrush(right)
        painter.drawPolygon(right_face)


def _palette_is_light() -> bool:
    """True when the *applied* palette is the light one.

    Reading the palette (not the config) matters: the render tools and tests call ``apply_theme``
    directly, and a widget must follow the sheet actually in force. Before the first ``apply_theme``
    call there is no themed palette, so the light default is used - the widget is then corrected by
    ``on_theme_applied``.
    """
    app = QApplication.instance()
    if app is None:
        return True
    window = app.palette().color(QPalette.ColorRole.Window)
    return window.lightnessF() > 0.5
