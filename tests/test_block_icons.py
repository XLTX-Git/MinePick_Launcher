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

"""Version icons: nearest-neighbour pixel scaling, loader glyphs, and the drawn fallback."""

from __future__ import annotations

import json
import zipfile

import pytest
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
    image = pixmap.toImage()
    return {
        image.pixelColor(x, y).name()
        for x in range(image.width())
        for y in range(image.height())
    }


def _solid_png(path, colour: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    assert image.save(str(path))
    return path


def _solid_png_bytes(path, colour: str) -> bytes:
    return _solid_png(path, colour).read_bytes()


def test_texture_is_scaled_to_48px_without_smoothing(texture_cache):
    pixmap = block_icons.block_pixmap("grass_block_side", 48, cache_root=texture_cache)
    assert not pixmap.isNull()
    assert round(pixmap.width() / pixmap.devicePixelRatio()) == 48
    image = pixmap.toImage()
    colours = {
        image.pixelColor(x, y).name()
        for x in range(image.width())
        for y in range(image.height())
    }
    # FastTransformation keeps the two source colours exactly; any smoothing would add blends.
    assert colours == {"#cc2222", "#2244cc"}


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


def test_lookup_order_is_assets_then_jar_then_cache(ws_tmp):
    """The version's own files win over the user cache, so an old version keeps its old texture."""
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

    def icon():
        block_icons.clear_cache()
        return block_icons.block_pixmap(
            "grass_block_side", 48, game_dir=game, version_id="1.20.1", cache_root=cache
        )

    assert _colours(icon()) == {"#cc2222"}  # own assets
    object_path.unlink()
    assert _colours(icon()) == {"#2244cc"}  # own client jar
    assert _colours(
        block_icons.block_pixmap(
            "grass_block_side",
            48,
            game_dir=ws_tmp / "no-game",
            version_id="1.20.1",
            cache_root=cache,
        )
    ) == {"#33aa33"}  # user cache
