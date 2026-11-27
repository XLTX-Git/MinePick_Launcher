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

"""Version icons: the isometric cube (shading, nearest-neighbour pixels), loader glyphs, fallback."""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from gui import block_icons, nav_icons
from launcher.meta import block_textures as bt


@pytest.fixture()
def texture_cache(ws_tmp):
    """A cache with one 16x16 checkerboard texture (two exact colours, no gradients)."""
    root = bt.texture_cache_root(ws_tmp / "data") / bt.MODERN_BUCKET
    root.mkdir(parents=True)
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(QColor("#cc2222"))
    for y in range(16):
        for x in range(16):
            if (x + y) % 2:
                image.setPixelColor(x, y, QColor("#2244cc"))
    assert image.save(str(root / "grass_block_side.png"))
    block_icons.clear_cache()
    try:
        yield bt.texture_cache_root(ws_tmp / "data")
    finally:
        block_icons.clear_cache()


def _painted(pixmap) -> int:
    image = pixmap.toImage()
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )


def _colours(pixmap) -> set[str]:
    """Every colour with a painted (non-transparent) pixel; empty corners would read as #000000."""
    image = pixmap.toImage()
    return {
        image.pixelColor(x, y).name()
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    }


def _shaded(colour: str, factor: float) -> str:
    """The exact colour the cube renderer produces for one face (it rounds per channel)."""
    source = QColor(colour)
    return QColor(
        round(source.red() * factor),
        round(source.green() * factor),
        round(source.blue() * factor),
    ).name()


def _face_centre(size: int, face: str) -> tuple[int, int]:
    """A pixel safely inside the top / left / right face of the cube layout."""
    half = size * block_icons.CUBE_HALF_WIDTH_RATIO
    diamond = half / 2.0
    depth = 2.0 / math.sqrt(3.0) * half
    center_x = size / 2.0
    top_y = (size - (half + depth)) / 2.0
    left = (center_x - half, top_y + diamond)
    peak = (center_x, top_y)
    right = (center_x + half, top_y + diamond)
    bottom = (center_x, top_y + 2.0 * diamond)
    if face == "top":
        points = (left, peak, right, bottom)
    elif face == "left":
        points = (left, bottom, (bottom[0], bottom[1] + depth), (left[0], left[1] + depth))
    else:
        points = (bottom, right, (right[0], right[1] + depth), (bottom[0], bottom[1] + depth))
    return round(sum(point[0] for point in points) / 4), round(
        sum(point[1] for point in points) / 4
    )


def _pixel(pixmap, point: tuple[int, int]) -> QColor:
    return pixmap.toImage().pixelColor(*point)


def _solid_png(path, colour: str, height: int = 16):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(16, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    assert image.save(str(path))
    return path


def _solid_png_bytes(path, colour: str) -> bytes:
    return _solid_png(path, colour).read_bytes()


def _encoded_png(colour: str, edge: int = 16) -> bytes:
    """A complete PNG in memory (the network tests must serve real bytes, not a bare signature)."""
    image = QImage(edge, edge, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _write_jar(game: Path, block: str, payload: bytes) -> Path:
    jar = game / "versions" / "1.20.1" / "1.20.1.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr(f"assets/minecraft/textures/block/{block}.png", payload)
    return jar


def test_texture_is_rendered_as_a_48px_cube_without_smoothing(texture_cache):
    """FastTransformation + no antialiasing: exactly the six shaded source colours, no blends."""
    pixmap = block_icons.block_pixmap("grass_block_side", 48, cache_root=texture_cache)
    assert not pixmap.isNull()
    assert round(pixmap.width() / pixmap.devicePixelRatio()) == 48
    assert pixmap.height() / pixmap.devicePixelRatio() == 48
    # Transparent corners are part of the icon shape, not painted texels.
    assert pixmap.toImage().pixelColor(0, 0).alpha() == 0
    assert _painted(pixmap) > 48 * 48 // 2
    expected = {
        colour
        for source in ("#cc2222", "#2244cc")
        for colour in (
            source,
            _shaded(source, block_icons.CUBE_LEFT_LIGHT),
            _shaded(source, block_icons.CUBE_RIGHT_LIGHT),
        )
    }
    assert _colours(pixmap) == expected


def test_cube_faces_use_minecrafts_100_80_60_shading(texture_cache):
    """Top is full brightness, left 80%, right 60%; a pixel sample proves the order and the values."""
    root = texture_cache
    _solid_png(root / bt.MODERN_BUCKET / "gold_block.png", "#cc2222")
    block_icons.clear_cache()
    pixmap = block_icons.block_pixmap("gold_block", 48, cache_root=texture_cache)

    top = _pixel(pixmap, _face_centre(48, "top")).name()
    left = _pixel(pixmap, _face_centre(48, "left")).name()
    right = _pixel(pixmap, _face_centre(48, "right")).name()
    assert top == "#cc2222"
    assert left == _shaded("#cc2222", block_icons.CUBE_LEFT_LIGHT)
    assert right == _shaded("#cc2222", block_icons.CUBE_RIGHT_LIGHT)

    def luminance(name: str) -> float:
        colour = QColor(name)
        return 0.2126 * colour.red() + 0.7152 * colour.green() + 0.0722 * colour.blue()

    assert luminance(top) > luminance(left) > luminance(right)
    assert block_icons.CUBE_TOP_LIGHT == 1.0
    assert block_icons.CUBE_LEFT_LIGHT == 0.8
    assert block_icons.CUBE_RIGHT_LIGHT == 0.6


def test_isometric_pixmap_is_a_public_renderer_with_a_transparent_background():
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(QColor("#2244cc"))
    pixmap = block_icons.isometric_pixmap(image, size=64)
    assert round(pixmap.width() / pixmap.devicePixelRatio()) == 64
    assert _colours(pixmap) == {
        "#2244cc",
        _shaded("#2244cc", block_icons.CUBE_LEFT_LIGHT),
        _shaded("#2244cc", block_icons.CUBE_RIGHT_LIGHT),
    }
    assert pixmap.toImage().pixelColor(0, 63).alpha() == 0


def test_grass_block_uses_grass_block_top_for_the_top_face(texture_cache):
    """Grass is the two-texture block: top from grass_block_top, both sides from grass_block_side."""
    root = texture_cache / bt.MODERN_BUCKET
    _solid_png(root / "grass_block_side.png", "#cc2222")
    _solid_png(root / "grass_block_top.png", "#2244cc")
    block_icons.clear_cache()
    assert block_icons.TOP_FACE_BLOCK["grass_block_side"] == "grass_block_top"

    pixmap = block_icons.block_pixmap("grass_block_side", 48, cache_root=texture_cache)
    assert _pixel(pixmap, _face_centre(48, "top")).name() == "#2244cc"
    assert _pixel(pixmap, _face_centre(48, "left")).name() == _shaded(
        "#cc2222", block_icons.CUBE_LEFT_LIGHT
    )
    assert _pixel(pixmap, _face_centre(48, "right")).name() == _shaded(
        "#cc2222", block_icons.CUBE_RIGHT_LIGHT
    )


def test_animated_texture_uses_its_first_frame(texture_cache):
    """command_block_front ships as a 16x64 strip: the cube takes frame one, not a squashed strip."""
    root = texture_cache / bt.MODERN_BUCKET
    image = QImage(16, 64, QImage.Format.Format_ARGB32)
    image.fill(QColor("#22cc44"))
    for y in range(16):
        for x in range(16):
            image.setPixelColor(x, y, QColor("#cc2222"))
    assert image.save(str(root / "command_block_front.png"))
    block_icons.clear_cache()

    pixmap = block_icons.block_pixmap("command_block_front", 48, cache_root=texture_cache)
    assert _colours(pixmap) == {
        "#cc2222",
        _shaded("#cc2222", block_icons.CUBE_LEFT_LIGHT),
        _shaded("#cc2222", block_icons.CUBE_RIGHT_LIGHT),
    }


def test_greyscale_grass_top_is_tinted_but_a_coloured_one_is_left_alone(texture_cache):
    """1.13+ ships the grass top as a greyscale mask; Minecraft tints it, so the launcher does too."""
    root = texture_cache / bt.MODERN_BUCKET
    _solid_png(root / "grass_block_side.png", "#66aa44")
    _solid_png(root / "grass_block_top.png", "#8f8f8f")
    block_icons.clear_cache()

    pixmap = block_icons.block_pixmap("grass_block_side", 48, cache_root=texture_cache)
    top = _pixel(pixmap, _face_centre(48, "top"))
    tint = QColor(block_icons.GRASS_TINT)
    assert top.name() == QColor(
        round(0x8F * tint.red() / 255),
        round(0x8F * tint.green() / 255),
        round(0x8F * tint.blue() / 255),
    ).name()

    # A pre-1.13 grass_top is already green: tinting it a second time would make it almost black.
    _solid_png(root / "grass_block_top.png", "#66cc33")
    block_icons.clear_cache()
    coloured = block_icons.block_pixmap("grass_block_side", 48, cache_root=texture_cache)
    assert _pixel(coloured, _face_centre(48, "top")).name() == "#66cc33"


def test_missing_texture_falls_back_to_the_drawn_block(ws_tmp):
    empty = bt.texture_cache_root(ws_tmp / "data")
    pixmap = block_icons.block_pixmap("cobblestone", 48, cache_root=empty)
    assert not pixmap.isNull()
    assert _painted(pixmap) > 0


def test_every_loader_kind_renders_its_bundled_glyph():
    assert nav_icons.ICON_FILES["forge"] == "anvil"
    assert nav_icons.ICON_FILES["fabric"] == "spool"
    assert nav_icons.ICON_FILES["neoforge"] == "flame"
    for kind in ("forge", "fabric", "neoforge", "quilt"):
        pixmap = block_icons.loader_pixmap(kind, "#2f9e6f", 22)
        assert not pixmap.isNull(), f"{kind}: no glyph"
        assert _painted(pixmap) > 12, f"{kind}: glyph painted almost nothing"


def test_unknown_loader_still_returns_an_icon():
    pixmap = block_icons.loader_pixmap("liteloader", "#2f9e6f", 22)
    assert not pixmap.isNull()
    assert _painted(pixmap) > 0


def test_lookup_order_is_assets_then_cache_then_jar(ws_tmp):
    """The version's own files win, then the cache, then the jar.

    The cache sits before the jar because the jar is the expensive source: once a version's texture
    has been copied there (see the next test), opening the 25 MB archive again would be pure waste.
    """
    game = ws_tmp / "game"
    cache = bt.texture_cache_root(ws_tmp / "data")
    _solid_png(cache / bt.MODERN_BUCKET / "grass_block_side.png", "#33aa33")

    jar = game / "versions" / "1.20.1" / "1.20.1.jar"
    jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr(
            "assets/minecraft/textures/block/grass_block_side.png",
            _solid_png_bytes(ws_tmp / "jar-source.png", "#2244cc"),
        )

    digest = "ab" + "0" * 38
    asset = _solid_png(ws_tmp / "asset.png", "#cc2222")
    index = {
        "objects": {
            "minecraft/textures/block/grass_block_side.png": {
                "hash": digest,
                "size": asset.stat().st_size,
            }
        }
    }
    (game / "assets" / "indexes").mkdir(parents=True)
    (game / "assets" / "indexes" / "1.20.1.json").write_text(json.dumps(index), encoding="utf-8")
    object_path = game / "assets" / "objects" / digest[:2] / digest
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(asset.read_bytes())

    def cube_colours(colour: str) -> set[str]:
        return {
            colour,
            _shaded(colour, block_icons.CUBE_LEFT_LIGHT),
            _shaded(colour, block_icons.CUBE_RIGHT_LIGHT),
        }

    def icon():
        block_icons.clear_cache()
        return block_icons.block_pixmap(
            "grass_block_side", 48, game_dir=game, version_id="1.20.1", cache_root=cache
        )

    assert _colours(icon()) == cube_colours("#cc2222")  # own assets
    object_path.unlink()
    assert _colours(icon()) == cube_colours("#33aa33")  # the user cache beats the jar


def test_first_jar_read_is_cached_and_the_second_read_never_opens_the_jar(ws_tmp, monkeypatch):
    """Requirement: the first read from client.jar lands in cache/block-textures/modern/."""
    game = ws_tmp / "game"
    jar_bytes = _solid_png_bytes(ws_tmp / "jar-source.png", "#2244cc")
    jar = game / "versions" / "1.20.1" / "1.20.1.jar"
    jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("assets/minecraft/textures/block/grass_block_side.png", jar_bytes)
        archive.writestr("assets/minecraft/textures/block/grass_block_top.png", jar_bytes)

    cache = bt.texture_cache_root(ws_tmp / "data")
    calls: list[str] = []
    real = bt.jar_texture_bytes

    def counting(block_name, **kwargs):
        calls.append(block_name)
        return real(block_name, **kwargs)

    monkeypatch.setattr(bt, "jar_texture_bytes", counting)

    def icon():
        block_icons.clear_cache()
        return block_icons.block_pixmap(
            "grass_block_side", 48, game_dir=game, version_id="1.20.1", cache_root=cache
        )

    cube_colours = {
        "#2244cc",
        _shaded("#2244cc", block_icons.CUBE_LEFT_LIGHT),
        _shaded("#2244cc", block_icons.CUBE_RIGHT_LIGHT),
    }
    assert _colours(icon()) == cube_colours
    # Grass is the two-texture block: the side and the top both come out of the jar on this read.
    assert calls == ["grass_block_side", "grass_block_top"]

    root = cache / bt.MODERN_BUCKET
    for name in ("grass_block_side", "grass_block_top"):
        stored = root / (name + ".png")
        assert stored.is_file()
        assert stored.read_bytes() == jar_bytes  # the exact jar pixels, written atomically
    assert not list(root.glob("*.tmp"))

    # Second read: the cache answers both faces, the jar is not opened again.
    assert _colours(icon()) == cube_colours
    assert calls == ["grass_block_side", "grass_block_top"]


def test_lookup_order_is_assets_cache_jar_network_then_the_drawn_block(ws_tmp):
    """The four sources win in this order, and only an empty machine reaches the network.

    The machine is built four times over: the version's assets (red), the user cache (green), the
    client jar (blue) and, once all of those are gone, a fake CDN response (a yellow 128px finished
    icon - the shape mcmod.cn actually serves). Removing one level at a time moves the icon to the
    next, so the order is asserted rather than described. The last step serves a 404 and expects the
    drawn block, with no exception.
    """
    game = ws_tmp / "game"
    cache = bt.texture_cache_root(ws_tmp / "data")
    root = cache / bt.MODERN_BUCKET
    block = "cobblestone"

    # level 1: the version's own assets (index + hashed object)
    digest = "ab" + "0" * 38
    (game / "assets" / "indexes").mkdir(parents=True)
    (game / "assets" / "indexes" / "1.20.1.json").write_text(
        json.dumps(
            {"objects": {f"minecraft/textures/block/{block}.png": {"hash": digest, "size": 1}}}
        ),
        encoding="utf-8",
    )
    (game / "versions" / "1.20.1").mkdir(parents=True)
    (game / "versions" / "1.20.1" / "1.20.1.json").write_text(
        json.dumps(
            {"id": "1.20.1", "assetIndex": {"id": "1.20.1", "url": "https://example.invalid/"}}
        ),
        encoding="utf-8",
    )
    asset = game / "assets" / "objects" / digest[:2] / digest
    asset.parent.mkdir(parents=True)
    asset.write_bytes(_encoded_png("#cc2222"))

    # level 2: the user cache, level 3: the client jar
    _solid_png(root / f"{block}.png", "#33aa33")
    jar = _write_jar(game, block, _encoded_png("#2244cc"))

    # level 4: the network, answered by a MockTransport - no socket is opened here.
    yellow = _encoded_png("#ffcc00", 128)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=yellow, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def cube(colour: str) -> set[str]:
        return {
            colour,
            _shaded(colour, block_icons.CUBE_LEFT_LIGHT),
            _shaded(colour, block_icons.CUBE_RIGHT_LIGHT),
        }

    def icon():
        block_icons.clear_cache()
        return block_icons.block_pixmap(
            block, 48, game_dir=game, version_id="1.20.1", cache_root=cache
        )

    try:
        assert _colours(icon()) == cube("#cc2222")  # level 1 wins
        assert requested == []
        asset.unlink()
        assert _colours(icon()) == cube("#33aa33")  # level 2 beats the jar
        assert requested == []
        (root / f"{block}.png").unlink()
        assert _colours(icon()) == cube("#2244cc")  # level 3 beats the network
        assert requested == []

        # All local levels gone. The jar read above re-cached its pixels, so that copy goes too.
        jar.unlink()
        (root / f"{block}.png").unlink()
        # Keep the prefetch focused: every other name is already in the cache.
        for name in bt.TEXTURE_NAMES:
            if name != block:
                _solid_png(root / f"{name}.png", "#888888")
        assert bt.prefetch_missing_textures(ws_tmp / "data", game_dir=game, client=client) == 1
        assert requested == [bt.NETWORK_SOURCES[block]]  # level 4: exactly one icon request
        pixmap = icon()
        assert _pixel(pixmap, (0, 0)).alpha() == 255  # a finished square icon, not a cube
        assert _colours(pixmap) == {"#ffcc00"}

        # level 5: the network fails as well -> the drawn accent block, and no exception.
        (root / f"{block}.png").unlink()

        def not_found(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, request=request)

        failing = httpx.Client(transport=httpx.MockTransport(not_found))
        try:
            assert (
                bt.prefetch_missing_textures(ws_tmp / "data", game_dir=game, client=failing) == 0
            )
        finally:
            failing.close()
        drawn = icon()
        assert not drawn.isNull()
        assert _pixel(drawn, (0, 0)).alpha() == 0
        assert _painted(drawn) > 0
    finally:
        client.close()


def test_a_cached_texture_never_reaches_the_network(ws_tmp):
    """With all five textures in the cache, the prefetch asks for nothing and paints locally."""
    cache = bt.texture_cache_root(ws_tmp / "data")
    root = cache / bt.MODERN_BUCKET
    for name in bt.TEXTURE_NAMES:
        _solid_png(root / f"{name}.png", "#33aa33")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert bt.prefetch_missing_textures(ws_tmp / "data", client=client) == 0
        block_icons.clear_cache()
        pixmap = block_icons.block_pixmap("cobblestone", 48, cache_root=cache)
        assert _colours(pixmap) == {
            "#33aa33",
            _shaded("#33aa33", block_icons.CUBE_LEFT_LIGHT),
            _shaded("#33aa33", block_icons.CUBE_RIGHT_LIGHT),
        }
        assert requested == []
    finally:
        client.close()

def test_the_painted_fallback_differs_per_block(ws_tmp):
    """With no texture at all the stand-in must still say which block it stands for.

    A fresh install with no game files and no network hit the same accent-coloured cube for every
    version row, so a release, a snapshot and a 0.x version looked identical.
    """
    from PySide6.QtGui import QColor

    empty = bt.texture_cache_root(ws_tmp / "data")
    colours = {}
    for block in ("grass_block_side", "command_block_front", "gold_block", "cobblestone"):
        pixmap = block_icons.block_pixmap(block, 40, cache_root=empty)
        image = pixmap.toImage()
        colours[block] = QColor(image.pixel(image.width() // 2, image.height() // 4)).name()
    assert len(set(colours.values())) == len(colours), colours
