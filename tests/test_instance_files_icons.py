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

"""Artwork read off the disk: a mod jar's logo, a world's screenshot and its creation date.

Every fixture is a real zip / a real gzipped NBT file written into a temporary directory, so the
readers are measured against the formats the game and the loaders actually write. The mark that a
jar's own metadata wins over the conventional fallback is the *content* of the returned bytes, not
just "something came back".
"""

from __future__ import annotations

import gzip
import json
import struct
import time
import zipfile
from pathlib import Path

from gui.pages.instance_files import (
    format_day,
    mod_icon_bytes,
    read_level_created,
    scan_saves,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 24


def _png(tag: bytes) -> bytes:
    """A PNG-signature payload whose body identifies the entry it came from."""
    return b"\x89PNG\r\n\x1a\n" + tag


def _jar(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


# ---------------------------------------------------------------- mod icons


def test_a_fabric_icon_string_is_the_mod_icon(ws_tmp):
    jar = _jar(
        ws_tmp / "string.jar",
        {
            "fabric.mod.json": json.dumps({"id": "demo", "icon": "assets/demo/icon.png"}).encode(),
            "assets/demo/icon.png": _png(b"stated"),
        },
    )
    assert mod_icon_bytes(jar) == _png(b"stated")


def test_a_size_keyed_icon_dictionary_takes_the_largest_entry(ws_tmp):
    jar = _jar(
        ws_tmp / "dict.jar",
        {
            "fabric.mod.json": json.dumps(
                {"id": "demo", "icon": {"32": "icon_32.png", "128": "icon_128.png", "64": "icon_64.png"}}
            ).encode(),
            "icon_32.png": _png(b"32"),
            "icon_64.png": _png(b"64"),
            "icon_128.png": _png(b"128"),
        },
    )
    assert mod_icon_bytes(jar) == _png(b"128"), "the sharpest declared icon must win"


def test_a_quilt_metadata_icon_is_read(ws_tmp):
    jar = _jar(
        ws_tmp / "quilt.jar",
        {
            "quilt.mod.json": json.dumps(
                {"quilt_loader": {"metadata": {"icon": "quilt_icon.png"}}}
            ).encode(),
            "quilt_icon.png": _png(b"quilt"),
        },
    )
    assert mod_icon_bytes(jar) == _png(b"quilt")


def test_a_forge_logo_file_is_read(ws_tmp):
    jar = _jar(
        ws_tmp / "forge.jar",
        {
            "META-INF/mods.toml": (
                b'[[mods]]\nmodId="demo"\nlogoFile = "assets/demo/logo.png"\n'
            ),
            "assets/demo/logo.png": _png(b"forge"),
        },
    )
    assert mod_icon_bytes(jar) == _png(b"forge")


def test_a_neoforge_logo_file_is_read(ws_tmp):
    jar = _jar(
        ws_tmp / "neo.jar",
        {
            "META-INF/neoforge.mods.toml": (
                b'[[mods]]\nmodId="demo"\nlogoFile="neo.png"\n'
            ),
            "neo.png": _png(b"neo"),
        },
    )
    assert mod_icon_bytes(jar) == _png(b"neo")


def test_the_conventional_fallback_prefers_the_root_icon(ws_tmp):
    jar = _jar(
        ws_tmp / "fallback.jar",
        {
            "assets/demo/icon.png": _png(b"nested"),
            "icon.png": _png(b"root"),
        },
    )
    assert mod_icon_bytes(jar) == _png(b"root")


def test_a_deep_icon_fallback_is_not_searched(ws_tmp):
    jar = _jar(
        ws_tmp / "deep.jar",
        {"assets/demo/textures/gui/icon.png": _png(b"deep")},
    )
    assert mod_icon_bytes(jar) is None, "depth 4 is not a mod icon location"


def test_a_jar_without_artwork_returns_none(ws_tmp):
    jar = _jar(ws_tmp / "plain.jar", {"fabric.mod.json": b'{"id":"demo"}'})
    assert mod_icon_bytes(jar) is None


def test_metadata_that_points_at_a_non_image_is_not_an_icon(ws_tmp):
    jar = _jar(
        ws_tmp / "wrong.jar",
        {
            "fabric.mod.json": json.dumps({"id": "demo", "icon": "notes.txt"}).encode(),
            "notes.txt": b"this is not a picture",
        },
    )
    assert mod_icon_bytes(jar) is None


def test_an_oversized_entry_is_refused(ws_tmp):
    jar = _jar(
        ws_tmp / "big.jar",
        {
            "fabric.mod.json": json.dumps({"id": "demo", "icon": "huge.png"}).encode(),
            "huge.png": b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024),
        },
    )
    assert mod_icon_bytes(jar) is None


def test_an_unreadable_jar_returns_none(ws_tmp):
    broken = ws_tmp / "broken.jar"
    broken.write_bytes(b"not a zip")
    assert mod_icon_bytes(broken) is None


# ---------------------------------------------------------------- save icon / dates


def _level_dat(created: int | None, name: str = "World") -> bytes:
    text = name.encode()
    string_tag = b"\x08\x00\x09LevelName" + struct.pack(">H", len(text)) + text
    long_tag = b"\x04\x00\x07Created" + struct.pack(">q", created) if created else b""
    compound = b"\x0a\x00\x04Data" + string_tag + long_tag + b"\x00"
    return gzip.compress(b"\x0a\x00\x00" + compound + b"\x00")


def test_read_level_created_returns_seconds(ws_tmp):
    level = ws_tmp / "level.dat"
    created_ms = int(time.time() * 1000) - 86_400_000
    level.write_bytes(_level_dat(created_ms))
    value = read_level_created(level)
    assert value is not None
    assert abs(value - created_ms / 1000.0) < 1.0


def test_read_level_created_rejects_a_missing_or_impossible_stamp(ws_tmp):
    missing = ws_tmp / "missing.dat"
    missing.write_bytes(_level_dat(None))
    assert read_level_created(missing) is None
    corrupt = ws_tmp / "corrupt.dat"
    corrupt.write_bytes(b"\x04\x00\x07Created" + struct.pack(">q", -5))
    assert read_level_created(corrupt) is None


def test_a_world_carries_its_icon_and_creation_time(ws_tmp):
    saves = ws_tmp / "saves"
    world = saves / "my-world"
    world.mkdir(parents=True)
    created_ms = int(time.time() * 1000) - 30 * 86_400_000
    (world / "level.dat").write_bytes(_level_dat(created_ms, "My World"))
    (world / "icon.png").write_bytes(_png(b"world"))

    worlds = scan_saves(saves)
    assert len(worlds) == 1
    save = worlds[0]
    assert save.world_name == "My World"
    assert save.icon == _png(b"world")
    assert abs(save.created - created_ms / 1000.0) < 1.0
    assert format_day(save.created) in ("", format_day(created_ms / 1000.0))


def test_a_world_without_an_icon_falls_back_to_the_folder_time(ws_tmp):
    saves = ws_tmp / "saves"
    world = saves / "bare"
    world.mkdir(parents=True)
    worlds = scan_saves(saves)
    assert worlds[0].icon is None
    assert worlds[0].created > 0, "the folder's own creation time is the fallback, not 1970"
    assert worlds[0].world_name == "bare"
