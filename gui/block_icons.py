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

"""Block and loader glyphs for the instance cards and the dense version rows.

The real texture comes from ``launcher.meta.block_textures``: the version's own ``assets``, then its
own client jar, then the user's cache. When none of them has it, the accent block painted by
:func:`gui.paint_utils.block_pixmap` is used, so the pages keep working on a fresh install with no
game files and no network.

Scaling is deliberately nearest-neighbour: the source is a 16x16 pixel-art texture, and a smoothed
48px scale would turn it into mush. The pixmap is cached per (file, mtime, size, device ratio), so a
list of a thousand manifest rows loads each texture once.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from gui import nav_icons, paint_utils
from launcher.meta import block_textures

#: Loader kinds that have a bundled Lucide glyph (the loader -> file mapping lives in nav_icons:
#: forge -> anvil, fabric/quilt -> spool, neoforge -> flame).
LOADER_GLYPHS: frozenset[str] = frozenset({"forge", "fabric", "neoforge", "quilt"})

_TEXTURE_CACHE: dict[tuple[str, int, int, int], QPixmap] = {}
_JAR_CACHE: dict[tuple[str, int, int], QPixmap] = {}


def clear_cache() -> None:
    """Drop scaled textures (tests and theme-independent reloads)."""
    _TEXTURE_CACHE.clear()
    _JAR_CACHE.clear()


def _screen_ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    try:
        return max(1.0, float(screen.devicePixelRatio()) if screen is not None else 1.0)
    except (AttributeError, RuntimeError):
        return 1.0


def block_pixmap(
    block_name: str,
    size: int,
    *,
    game_dir: Path | None = None,
    version_id: str | None = None,
    cache_root: Path | None = None,
) -> QPixmap:
    """A real block texture when one resolves, the drawn accent block otherwise.

    ``size`` is the displayed (logical) edge in pixels; the returned pixmap carries the screen's
    device pixel ratio, so 48px stays 48px on a 125% display without a blurred upscale.
    """
    path = block_textures.find_asset_texture(
        block_name, game_dir=game_dir, version_id=version_id
    )
    if path is not None:
        pixmap = _scaled_texture(path, size)
        if pixmap is not None:
            return pixmap
    if game_dir is not None and version_id:
        source = f"jar:{game_dir}:{version_id}:{block_name}"
        cached = _cached_jar(source, size)
        if cached is not None:
            return cached
        data = block_textures.jar_texture_bytes(
            block_name, game_dir=game_dir, version_id=version_id
        )
        if data is not None:
            pixmap = _scaled_bytes(data, source, size)
            if pixmap is not None:
                return pixmap
    path = block_textures.find_cached_texture(
        block_name, cache_root=cache_root, version_id=version_id
    )
    if path is not None:
        pixmap = _scaled_texture(path, size)
        if pixmap is not None:
            return pixmap
    return paint_utils.block_pixmap(size)


def loader_pixmap(loader: str, color: str, size: int = 22) -> QPixmap:
    """The loader's Lucide glyph in the theme accent (drawn block if the glyph is unavailable)."""
    if loader in LOADER_GLYPHS:
        icon = nav_icons.nav_icon(loader, color, size)
        if not icon.isNull():
            return icon.pixmap(size, size)
    return paint_utils.block_pixmap(size)


def _cached_jar(source: str, size: int) -> QPixmap | None:
    """The scaled jar texture if it is already decoded - avoids re-opening the zip on every row."""
    return _JAR_CACHE.get((source, int(size), round(_screen_ratio() * 100)))


def _scaled_texture(path: Path, size: int) -> QPixmap | None:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    ratio = _screen_ratio()
    key = (str(path), mtime, int(size), round(ratio * 100))
    cached = _TEXTURE_CACHE.get(key)
    if cached is not None:
        return cached
    image = QImage(str(path))
    pixmap = _scale(image, size, ratio)
    if pixmap is not None:
        _TEXTURE_CACHE[key] = pixmap
    return pixmap


def _scaled_bytes(data: bytes, source: str, size: int) -> QPixmap | None:
    ratio = _screen_ratio()
    key = (source, int(size), round(ratio * 100))
    cached = _JAR_CACHE.get(key)
    if cached is not None:
        return cached
    image = QImage.fromData(data, "PNG")
    pixmap = _scale(image, size, ratio)
    if pixmap is not None:
        _JAR_CACHE[key] = pixmap
    return pixmap


def _scale(image: QImage, size: int, ratio: float) -> QPixmap | None:
    if image.isNull():
        return None
    edge = max(1, round(size * ratio))
    scaled = image.scaled(
        edge,
        edge,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    pixmap = QPixmap.fromImage(scaled)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap
