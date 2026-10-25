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

"""Navigation icons rendered from the bundled Lucide SVGs.

Eight 24px Lucide glyphs ship under ``gui/resources/icons/nav`` (ISC, see THIRD_PARTY_NOTICES.md).
The SVGs use ``currentColor`` for their stroke, so the one thing this module does is substitute the
colour the caller asks for, render the vector at the screen's device pixel ratio and cache the result.
No icon theme, no network, no second font: the raster is generated from the same file at every scale.

Caching is keyed by (name, colour, size, DPR); a theme or accent switch therefore asks for a different
colour and gets a different entry, never a stale one.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from launcher import paths

#: Sidebar keys (the same names NAV_KEYS uses) mapped onto their bundled Lucide file.
ICON_FILES: dict[str, str] = {
    "launch": "play",
    "instances": "layers",
    "versions": "download",
    "java": "coffee",
    "account": "user",
    "mods": "puzzle",
    "settings": "settings",
    "about": "info",
}

_CACHE: dict[tuple[str, str, int, float], QIcon] = {}
_GAP_CACHE: dict[tuple[str, str, int, float, int], QIcon] = {}


def clear_cache() -> None:
    """Drop rendered icons (theme or accent changed; colour is already part of the key)."""
    _CACHE.clear()
    _GAP_CACHE.clear()


def _screen_ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    try:
        return max(1.0, float(screen.devicePixelRatio()) if screen is not None else 1.0)
    except (AttributeError, RuntimeError):
        return 1.0


def _render(name: str, color: str, size: int, ratio: float) -> QPixmap | None:
    """Render one SVG with its ``currentColor`` replaced by ``color`` at ``size`` logical pixels."""
    filename = ICON_FILES.get(name)
    if filename is None:
        return None
    path = paths.resource_path(f"gui/resources/icons/nav/{filename}.svg")
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return None
    svg = source.replace("currentColor", color).encode("utf-8")
    renderer = QSvgRenderer(QByteArray(svg))
    if not renderer.isValid():
        return None

    pixmap = QPixmap(max(1, round(size * ratio)), max(1, round(size * ratio)))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0.0, 0.0, float(pixmap.width()), float(pixmap.height())))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def nav_icon(name: str, color: str, size: int = 24) -> QIcon:
    """A themed QIcon for one sidebar entry; unknown names return an empty QIcon.

    The pixmap is rendered at ``size * devicePixelRatio`` and tagged with that ratio, so 125% / 150%
    scaling gets a sharp vector raster instead of an upscaled 24px bitmap.
    """
    ratio = _screen_ratio()
    key = (name, color, int(size), ratio)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    pixmap = _render(name, color, size, ratio)
    if pixmap is None:
        return QIcon()
    icon = QIcon(pixmap)
    _CACHE[key] = icon
    return icon


def nav_icon_with_gap(name: str, color: str, size: int = 24, gap: int = 10) -> QIcon:
    """``nav_icon`` padded on the right so the rail keeps a 10px gap between icon and label.

    QListWidget derives the icon/text gap from the style and exposes no setter for it. A wider
    pixmap is the supported way to state that distance without a custom item delegate.
    """
    ratio = _screen_ratio()
    key = (name, color, int(size), ratio, int(gap))
    cached = _GAP_CACHE.get(key)
    if cached is not None:
        return cached
    base = _render(name, color, size, ratio)
    if base is None:
        return QIcon()
    canvas = QPixmap(max(1, round((size + gap) * ratio)), max(1, round(size * ratio)))
    canvas.fill(Qt.GlobalColor.transparent)
    canvas.setDevicePixelRatio(ratio)
    painter = QPainter(canvas)
    painter.drawPixmap(QPointF(0.0, 0.0), base)
    painter.end()
    icon = QIcon(canvas)
    _GAP_CACHE[key] = icon
    return icon


def icon_size(size: int = 24, gap: int = 10) -> QSize:
    """The logical size to hand to ``QListWidget.setIconSize`` for ``nav_icon_with_gap``."""
    return QSize(size + gap, size)
