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

"""Block and loader glyphs for the instance cards, the dense version rows and the launch page.

The real texture comes from ``launcher.meta.block_textures``: the version's own ``assets``, then the
user's cache, then its own client jar. The jar is the last local resort and the only expensive read (a
25 MB archive for a few KB of pixels), so the first successful jar read is copied into
``<launcher data dir>/cache/block-textures/modern/``; from the next lookup on the cache answers first
and the jar is not opened again. A machine with no Minecraft at all gets the cache filled by the same
background prefetch from mcmod.cn's icon CDN - never while a page is painting. When none of them has
it, the accent block painted by :func:`gui.paint_utils.block_pixmap` is used, so the pages keep
working on a fresh install with no game files and no network.

A 16x16 texture is shown the way Minecraft shows a block in the inventory: as an isometric cube with a
diamond top, two side faces and the game's own face shading (top 100%, left 80%, right 60%). The three
faces are painted as affinely transformed copies of the texture - one ``QPainter`` transform per face -
so the artwork is the real thing, not a flat square and not a hand-drawn stand-in. Grass is the one
block with two textures: ``grass_block_top`` for the top and ``grass_block_side`` for both sides.

A source that is already a finished picture of a cube (mcmod.cn's 128px icons, or its 300px gold one)
is drawn as it is: cubing a picture of a cube would paint a little cube on every face. The threshold
is :data:`ICON_SOURCE_EDGE`, far above every 16x16 vanilla texture.

Scaling is deliberately nearest-neighbour for the 16x16 faces: the source is pixel art, and a
smoothed scale would turn it into mush. A finished icon is an ordinary picture and is scaled smoothly.
The cube is cached per (source, mtime, size, device ratio), so a list of a thousand manifest rows
decodes each texture once.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import QApplication

from gui import nav_icons, paint_utils, theme
from launcher.instances import ICON_CUSTOM_PREFIX, custom_icon_file
from launcher.meta import block_textures

#: Loader kinds that have a bundled Lucide glyph (the loader -> file mapping lives in nav_icons:
#: forge -> anvil, fabric/quilt -> spool, neoforge -> flame).
LOADER_GLYPHS: frozenset[str] = frozenset({"forge", "fabric", "neoforge", "quilt"})

#: The eight icons a user can pin, in the order the picker shows them: the four block categories the
#: automatic rule derives, then the four loader glyphs. The names are stored in ``Instance.icon``.
RULE_ICON_NAMES: tuple[str, ...] = (
    "grass",
    "command",
    "gold",
    "cobble",
    "forge",
    "fabric",
    "neoforge",
    "quilt",
)

#: Rule name -> the block texture it stands for (the same blocks ``BLOCK_BY_CATEGORY`` uses).
RULE_ICON_BLOCKS: dict[str, str] = {
    "grass": "grass_block_side",
    "command": "command_block_front",
    "gold": "gold_block",
    "cobble": "cobblestone",
}

#: Rule names that draw a loader glyph instead of a block.
RULE_ICON_LOADERS: frozenset[str] = frozenset({"forge", "fabric", "neoforge", "quilt"})

#: A custom picture is re-encoded on upload down to this edge, so a 5 MB camera JPEG cannot become
#: the launcher's icon file. The display never scales a stored picture back up past this either.
CUSTOM_ICON_MAX_EDGE = 512

#: File suffixes accepted for a custom icon (the suffix is kept when the copy is written).
CUSTOM_ICON_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")

#: Corner radius of a custom picture, as a fraction of its edge: 8px on the 40px cards.
CUSTOM_ICON_RADIUS_RATIO = 0.2

#: A custom picture is shown as the picture itself, never through the block cube pipeline.
CUSTOM_ICON_PREFIX = ICON_CUSTOM_PREFIX

#: Minecraft's own block shading: the top face is lit in full, the left face at 80% and the right at
#: 60%. Keeping the game's numbers is what makes the icon read as a Minecraft block instead of a
#: generic folded square.
CUBE_TOP_LIGHT = 1.0
CUBE_LEFT_LIGHT = 0.8
CUBE_RIGHT_LIGHT = 0.6

#: Half the cube's width as a fraction of the icon's edge. The top rhombus is then the classic 2:1
#: isometric diamond, and the side height (2 / sqrt(3) * half) makes all three visible edges the same
#: projected length: a cube, not a squashed box. The cube is 0.86 x 0.93 of the icon, which leaves a
#: little air around it at every size the pages use (30, 40 and 48 px).
CUBE_HALF_WIDTH_RATIO = 0.43

#: A source at least this many pixels on its short edge is a finished icon, not a 16x16 face: the
#: network fallback's mcmod icons are 128px (the gold one 300px) renders of a whole cube. Feeding one
#: of those through the face renderer would paint a small cube on every face, so it is scaled as-is.
ICON_SOURCE_EDGE = 64

#: The one block whose top face is a different texture: the side is dirt with a grass fringe, the top is
#: grass seen from above. Everything else (stone, gold, command block) uses one texture on all faces.
TOP_FACE_BLOCK: dict[str, str] = {"grass_block_side": "grass_block_top"}

#: Minecraft's default (plains) grass colour. Since 1.13 the game ships ``grass_block_top`` as a
#: greyscale mask and tints it per biome while rendering. The launcher has no biome, so it applies the
#: same default tint the game applies on plains; a pre-1.13 texture is already coloured and is left
#: alone (see :func:`_grass_top`).
GRASS_TINT = "#91bd59"

#: Bumped whenever a cached pixmap would have to be redrawn; the key carries everything that decides it.
_IMAGE_CACHE: dict[str, QImage] = {}
_CUBE_CACHE: dict[tuple[str, str, int, int], QPixmap] = {}
_ICON_CACHE: dict[tuple[str, int, int], QPixmap] = {}
#: Custom pictures, keyed by (path, mtime, logical edge, device ratio): uploading over ``icon.png``
#: changes the mtime, so the next paint cannot show the previous picture.
_CUSTOM_CACHE: dict[tuple[str, int, int, int], QPixmap] = {}


def clear_cache() -> None:
    """Drop decoded textures and rendered cubes (tests and theme-independent reloads)."""
    _IMAGE_CACHE.clear()
    _CUBE_CACHE.clear()
    _ICON_CACHE.clear()
    _CUSTOM_CACHE.clear()


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
    """A real block texture as an isometric cube (a finished icon as-is), the drawn block otherwise.

    ``size`` is the displayed (logical) edge in pixels; the returned pixmap carries the screen's
    device pixel ratio, so 40px stays 40px on a 125% display without a blurred upscale. The lookup
    order is block_textures': the version's assets, the user's cache (which the background prefetch
    also fills from client jars and, on a machine with no game files, from mcmod.cn), then its client
    jar. A source that is already a finished icon is scaled as-is, not cubed.
    """
    side = _load_texture(
        block_name, game_dir=game_dir, version_id=version_id, cache_root=cache_root
    )
    if side is None:
        return paint_utils.block_pixmap(size)
    if _is_finished_icon(side[1]):
        return _finished_icon(side[0], side[1], size, _screen_ratio())

    top = side
    top_block = TOP_FACE_BLOCK.get(block_name)
    if top_block is not None:
        resolved_top = _load_texture(
            top_block, game_dir=game_dir, version_id=version_id, cache_root=cache_root
        )
        if resolved_top is not None:
            top = resolved_top

    ratio = _screen_ratio()
    key = (side[0], top[0], int(size), round(ratio * 100))
    cached = _CUBE_CACHE.get(key)
    if cached is not None:
        return cached

    top_image = _grass_top(top[1]) if top_block is not None else top[1]
    pixmap = _render_cube(side[1], top_image, size, ratio)
    if pixmap is None:
        return paint_utils.block_pixmap(size)
    if len(_CUBE_CACHE) > 128:
        _CUBE_CACHE.clear()
    _CUBE_CACHE[key] = pixmap
    return pixmap


def loader_pixmap(loader: str, color: str, size: int = 22) -> QPixmap:
    """The loader's Lucide glyph in the theme accent (drawn block if the glyph is unavailable)."""
    if loader in LOADER_GLYPHS:
        icon = nav_icons.nav_icon(loader, color, size)
        if not icon.isNull():
            return icon.pixmap(size, size)
    return paint_utils.block_pixmap(size)


def instance_icon_pixmap(
    instance,
    folder: Path,
    size: int,
    *,
    game_dir: Path | None = None,
    cache_root: Path | None = None,
) -> QPixmap:
    """The icon of one instance, in the order the field is read.

    ``icon`` empty -> the automatic rule (grass / command block / gold / cobblestone / loader glyph);
    a rule name -> that rule; ``custom:<file>`` -> the picture inside the instance folder, drawn as
    the picture itself (rounded, never cubed). A custom value whose file is missing, unreadable or
    undecodable falls back to the automatic rule and is **not** cleared from the metadata: a volume
    that is briefly unavailable must not silently rewrite the user's choice.
    """
    value = str(getattr(instance, "icon", "") or "").strip()
    if value.startswith(CUSTOM_ICON_PREFIX):
        picture = custom_icon_pixmap(folder, value, size)
        if picture is not None:
            return picture
    elif value in RULE_ICON_NAMES:
        return rule_icon_pixmap(value, size, game_dir=game_dir, cache_root=cache_root)
    return automatic_icon_pixmap(
        str(getattr(instance, "id", "") or ""),
        size,
        game_dir=game_dir,
        cache_root=cache_root,
    )


def automatic_icon_pixmap(
    version_id: str,
    size: int,
    *,
    game_dir: Path | None = None,
    cache_root: Path | None = None,
) -> QPixmap:
    """The rule-derived icon for a version/profile id: loader glyph, else the category block.

    The loader is read from the profile chain (so a renamed Forge instance still resolves to Forge),
    and the block category from the version type: release -> grass, snapshot -> command block,
    April Fools -> gold, ancient -> cobblestone. A missing texture falls back to the drawn block.
    """
    kind = block_textures.loader_kind(version_id, game_dir=game_dir)
    if kind is not None:
        return loader_pixmap(kind, _accent(), 22)
    version_type = block_textures.version_type_for(version_id, game_dir=game_dir)
    category = block_textures.icon_category(version_id, version_type)
    block = block_textures.block_name_for_category(category)
    return block_pixmap(
        block,
        size,
        game_dir=game_dir,
        version_id=version_id,
        cache_root=cache_root,
    )


def rule_icon_pixmap(
    rule: str,
    size: int,
    *,
    game_dir: Path | None = None,
    cache_root: Path | None = None,
) -> QPixmap:
    """One of the eight pinnable rule icons at ``size`` logical pixels.

    A block rule uses the real texture (the same renderer as the automatic rule); a loader rule
    draws its glyph centred in a transparent tile at 60% of the edge, so the four line glyphs read
    at the same weight as the four blocks beside them.
    """
    block = RULE_ICON_BLOCKS.get(rule)
    if block is not None:
        return block_pixmap(block, size, game_dir=game_dir, cache_root=cache_root)
    return _loader_tile(rule, size)


def custom_icon_pixmap(folder: Path, icon: str, size: int) -> QPixmap | None:
    """The user's picture as a rounded ``size`` tile, or ``None`` when it cannot be read.

    The picture is scaled to cover the square (smoothly), centre-cropped and clipped to a rounded
    rectangle. It is deliberately not passed through the isometric cube: a user-supplied picture is
    a finished icon, not a 16x16 block face. The result is cached per (file, mtime, size, ratio), so
    a list of cards decodes each upload once.
    """
    path = custom_icon_file(Path(folder), icon)
    if path is None or not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    ratio = _screen_ratio()
    key = (str(path), mtime, int(size), round(ratio * 100))
    cached = _CUSTOM_CACHE.get(key)
    if cached is not None:
        return cached
    image = QImage(str(path))
    if image.isNull():
        return None
    edge = max(1, round(int(size) * ratio))
    scaled = image.scaled(
        edge,
        edge,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    if scaled.isNull():
        return None
    # Centre-crop the covered square, then clip it; painting in device pixels (the canvas carries the
    # ratio only afterwards) keeps the radius and the image dimensions in the same space.
    x = max(0, (scaled.width() - edge) // 2)
    y = max(0, (scaled.height() - edge) // 2)
    canvas = QImage(edge, edge, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rounded = QPainterPath()
    radius = edge * CUSTOM_ICON_RADIUS_RATIO
    rounded.addRoundedRect(QRectF(0.0, 0.0, float(edge), float(edge)), radius, radius)
    painter.setClipPath(rounded)
    painter.drawImage(-x, -y, scaled)
    painter.end()
    pixmap = QPixmap.fromImage(canvas)
    pixmap.setDevicePixelRatio(ratio)
    if len(_CUSTOM_CACHE) > 64:
        _CUSTOM_CACHE.clear()
    _CUSTOM_CACHE[key] = pixmap
    return pixmap


def _loader_tile(rule: str, size: int) -> QPixmap:
    """A loader glyph centred in a transparent ``size`` tile (the picker's 48px slot)."""
    glyph_size = max(16, round(int(size) * 0.6))
    glyph = loader_pixmap(rule, _accent(), glyph_size)
    ratio = _screen_ratio()
    edge = max(1, round(int(size) * ratio))
    canvas = QPixmap(edge, edge)
    canvas.fill(Qt.GlobalColor.transparent)
    canvas.setDevicePixelRatio(ratio)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    glyph_ratio = glyph.devicePixelRatio() or 1.0
    glyph_width = glyph.width() / glyph_ratio
    glyph_height = glyph.height() / glyph_ratio
    painter.drawPixmap(
        QPointF((size - glyph_width) / 2.0, (size - glyph_height) / 2.0), glyph
    )
    painter.end()
    return canvas


def _accent() -> str:
    """The theme's accent colour (the light palette before any theme was applied)."""
    tokens = theme.current_tokens() or theme.theme_palette("light")
    return tokens.get("ACCENT", "#2f9e6f")


def isometric_pixmap(side: QImage, top: QImage | None = None, size: int = 48) -> QPixmap:
    """Paint a 16x16 texture as an isometric cube of ``size`` logical pixels.

    ``side`` is the texture on both side faces; ``top`` (the same texture when omitted) is the diamond
    on top. The faces are shaded with :data:`CUBE_TOP_LIGHT`, :data:`CUBE_LEFT_LIGHT` and
    :data:`CUBE_RIGHT_LIGHT`, and drawn without smoothing or antialiasing so every texel stays a hard
    pixel. The result carries the screen's device pixel ratio.
    """
    return _render_cube(
        side, top if top is not None else side, size, _screen_ratio()
    ) or QPixmap()


def _is_finished_icon(image: QImage) -> bool:
    """Is this a pre-rendered icon rather than a 16x16 face texture?

    The network fallback stores mcmod.cn's 128px cube renders (300px for gold) in the same cache as
    the jar's 16x16 faces; the short edge is what separates them, because a vanilla texture is 16 or
    an animated 16xN strip (cropped to its first frame before this runs).
    """
    return min(image.width(), image.height()) >= ICON_SOURCE_EDGE


def _finished_icon(source: str, image: QImage, size: int, ratio: float) -> QPixmap:
    """Scale a finished icon into the tile: smooth (it is a picture), ratio-aware, cached."""
    key = (source, int(size), round(ratio * 100))
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    edge = max(1, round(int(size) * ratio))
    scaled = image.scaled(
        edge,
        edge,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pixmap = QPixmap.fromImage(scaled)
    pixmap.setDevicePixelRatio(ratio)
    if len(_ICON_CACHE) > 64:
        _ICON_CACHE.clear()
    _ICON_CACHE[key] = pixmap
    return pixmap


def _render_cube(
    side: QImage, top: QImage, size: int, ratio: float
) -> QPixmap | None:
    """Render one cube; ``None`` when either texture is empty."""
    side = _first_frame(side)
    top = _first_frame(top)
    if side.isNull() or top.isNull():
        return None
    edge = max(1, round(int(size) * ratio))
    pixmap = QPixmap(edge, edge)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(ratio)

    painter = QPainter(pixmap)
    # Pixel art: a smoothed or antialiased edge is a blurred edge.
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    _paint_faces(painter, side, top, float(size))
    painter.end()
    return pixmap


def _paint_faces(painter: QPainter, side: QImage, top: QImage, extent: float) -> None:
    """Lay the three faces out as the three visible sides of a cube standing on its corner.

    The top face is the rhombus ``left -> peak -> bottom -> right``; the left face drops from the
    ``left``/``bottom`` edge and the right face from ``bottom``/``right``. Each face is an affine image
    of the texture square: one transform maps texture (0,0) to the face's top-left corner and its two
    edges onto the two projected cube edges.
    """
    half = extent * CUBE_HALF_WIDTH_RATIO
    diamond = half / 2.0
    depth = 2.0 / math.sqrt(3.0) * half
    center_x = extent / 2.0
    top_y = (extent - (half + depth)) / 2.0

    left = QPointF(center_x - half, top_y + diamond)
    peak = QPointF(center_x, top_y)
    right = QPointF(center_x + half, top_y + diamond)
    bottom = QPointF(center_x, top_y + 2.0 * diamond)
    drop = QPointF(0.0, depth)

    _paint_face(
        painter, _shaded(top, CUBE_TOP_LIGHT), left, peak - left, bottom - left
    )
    _paint_face(
        painter, _shaded(side, CUBE_LEFT_LIGHT), left, bottom - left, drop
    )
    _paint_face(
        painter, _shaded(side, CUBE_RIGHT_LIGHT), bottom, right - bottom, drop
    )


def _paint_face(
    painter: QPainter, image: QImage, origin: QPointF, across: QPointF, down: QPointF
) -> None:
    """Draw one textured face through the affine transform its parallelogram describes."""
    width = float(max(1, image.width()))
    height = float(max(1, image.height()))
    transform = QTransform(
        across.x() / width,
        across.y() / width,
        down.x() / height,
        down.y() / height,
        origin.x(),
        origin.y(),
    )
    painter.save()
    painter.setTransform(transform)
    painter.drawImage(0, 0, image)
    painter.restore()


def _shaded(image: QImage, factor: float) -> QImage:
    """The texture darkened by ``factor`` (0.8 = 80% light), leaving transparency untouched."""
    out = image.convertToFormat(QImage.Format.Format_ARGB32)
    if factor >= 0.999:
        return out
    for y in range(out.height()):
        for x in range(out.width()):
            color = out.pixelColor(x, y)
            out.setPixelColor(
                x,
                y,
                QColor(
                    round(color.red() * factor),
                    round(color.green() * factor),
                    round(color.blue() * factor),
                    color.alpha(),
                ),
            )
    return out


def _grass_top(image: QImage) -> QImage:
    """Colour a modern greyscale grass top the way the game does; return colours untouched.

    Pre-1.13 games ship a pre-tinted ``grass_top``; 1.13+ ships a greyscale mask. Tinting the latter
    is what keeps the top face from reading as stone, and skipping it for the former avoids
    double-tinting an old texture.
    """
    if not _is_greyscale(image):
        return image
    tint = QColor(GRASS_TINT)
    out = image.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(out.height()):
        for x in range(out.width()):
            color = out.pixelColor(x, y)
            out.setPixelColor(
                x,
                y,
                QColor(
                    round(color.red() * tint.red() / 255),
                    round(color.green() * tint.green() / 255),
                    round(color.blue() * tint.blue() / 255),
                    color.alpha(),
                ),
            )
    return out


def _is_greyscale(image: QImage) -> bool:
    """Is the image a greyscale mask? True when nearly every opaque texel has equal channels."""
    opaque = 0
    greyscale = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() < 8:
                continue
            opaque += 1
            channels = (color.red(), color.green(), color.blue())
            if max(channels) - min(channels) <= 6:
                greyscale += 1
    return opaque > 0 and greyscale * 100 >= opaque * 95


def _first_frame(image: QImage) -> QImage:
    """Crop an animated texture strip to its first frame (``command_block_front`` is 16x64)."""
    if image.isNull() or image.width() == image.height():
        return image
    edge = min(image.width(), image.height())
    return image.copy(0, 0, edge, edge)


# ---------------------------------------------------------------------------
# Texture lookup: the three block_textures levels, memoised by source
# ---------------------------------------------------------------------------


def _load_texture(
    block_name: str,
    *,
    game_dir: Path | None,
    version_id: str | None,
    cache_root: Path | None,
) -> tuple[str, QImage] | None:
    """Resolve one block texture: ``(cache key, image)`` or ``None`` when it is nowhere.

    Order: the version's own assets, then the user cache, then the version's client jar. The cache
    sits before the jar on purpose: a jar read is a 25 MB archive scan, and once this method has
    copied a jar texture into the cache (see :meth:`block_textures.store_texture_bytes`) the next
    lookup must stop at the cheap copy instead of opening the jar again. The cache is also where the
    background prefetch puts mcmod.cn's icons on a machine with no game files; painting never waits
    for that request and never makes one itself.

    The returned key identifies the exact bytes (a path's mtime is part of the file key, a client
    jar's location and profile id are part of the jar key), so the caller's cube cache cannot serve
    pixels from a texture that has since changed.
    """
    path = block_textures.find_asset_texture(
        block_name, game_dir=game_dir, version_id=version_id
    )
    if path is not None:
        loaded = _load_path(path)
        if loaded is not None:
            return loaded

    path = block_textures.find_cached_texture(
        block_name, cache_root=cache_root, version_id=version_id
    )
    if path is not None:
        loaded = _load_path(path)
        if loaded is not None:
            return loaded

    if game_dir is not None and version_id:
        source = f"jar:{game_dir}:{version_id}:{block_name}"
        cached = _IMAGE_CACHE.get(source)
        if cached is not None:
            return source, cached
        data = block_textures.jar_texture_bytes(
            block_name, game_dir=game_dir, version_id=version_id
        )
        if data is not None:
            image = QImage.fromData(data, "PNG")
            if not image.isNull():
                image = _first_frame(image)
                _remember(source, image)
                # First read from the jar: leave the pixels in the cache so the next launch (and the
                # next card of the same version) does not open the archive again. A failure is
                # silent - the icon is already decoded either way.
                block_textures.store_texture_bytes(block_name, data, cache_root)
                return source, image
    return None


def _load_path(path: Path) -> tuple[str, QImage] | None:
    """Decode a texture file once per (path, mtime)."""
    try:
        key = f"file:{path}:{path.stat().st_mtime_ns}"
    except OSError:
        return None
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        return key, cached
    image = QImage(str(path))
    if image.isNull():
        return None
    image = _first_frame(image)
    _remember(key, image)
    return key, image


def _remember(key: str, image: QImage) -> None:
    """Memoise one decoded texture, keeping the cache from growing with every version ever seen."""
    if len(_IMAGE_CACHE) > 64:
        _IMAGE_CACHE.clear()
    _IMAGE_CACHE[key] = image
