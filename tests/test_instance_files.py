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

"""The disk readers behind the instance detail's resource-pack, shader-pack and save pages.

These tests use real files in a temporary directory: the options file the game writes, a gzipped
NBT ``level.dat``, a folder that is really a file. The point is the failure direction - an
unreadable directory must raise so the page can report it, never look like an empty list.
"""

from __future__ import annotations

import base64
import gzip
import os
import struct
import time
import zipfile

import pytest

from gui.pages.instance_files import (
    backup_save,
    directory_size,
    display_file_name,
    format_modified,
    pack_icon_bytes,
    read_enabled_resource_packs,
    read_level_name,
    scan_content_files,
    scan_saves,
    strip_formatting_codes,
)

#: A real 1x1 PNG. The icon reader only accepts real PNG bytes, so the tests must not fake one.
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _level_dat(level_name: str, *, compress: bool = True) -> bytes:
    """The game's minimal shape: TAG_Compound -> TAG_Compound("Data") -> TAG_String("LevelName")."""
    text = level_name.encode("utf-8")
    string_tag = b"\x08\x00\x09LevelName" + struct.pack(">H", len(text)) + text
    data_compound = b"\x0a\x00\x04Data" + string_tag + b"\x00"
    raw = b"\x0a\x00\x00" + data_compound + b"\x00"
    return gzip.compress(raw) if compress else raw


# ---------------------------------------------------------------- options.txt


def test_enabled_packs_come_from_options_txt(ws_tmp):
    options = ws_tmp / "options.txt"
    options.write_text(
        'resourcePacks:["vanilla","file/Faithful 32x.zip"]\nlang:zh_cn\n', encoding="utf-8"
    )
    assert read_enabled_resource_packs(options) == {"vanilla", "file/faithful 32x.zip"}
    # Older versions wrote the bare file name; that spelling counts too (it is matched by name).
    options.write_text('resourcePacks:["Faithful 32x.zip"]\n', encoding="utf-8")
    assert "faithful 32x.zip" in read_enabled_resource_packs(options)


def test_missing_options_txt_means_nothing_beyond_vanilla_is_enabled(ws_tmp):
    assert read_enabled_resource_packs(ws_tmp / "options.txt") == set()


def test_unparseable_options_reports_unknown_instead_of_disabled(ws_tmp):
    options = ws_tmp / "options.txt"
    options.write_text("resourcePacks:not-json\n", encoding="utf-8")
    assert read_enabled_resource_packs(options) is None
    options.write_text("resourcePacks:{}\n", encoding="utf-8")  # a JSON value of the wrong shape
    assert read_enabled_resource_packs(options) is None


# ---------------------------------------------------------------- content folders


def test_content_scan_lists_only_files_and_states_which_are_enabled(ws_tmp):
    packs = ws_tmp / "resourcepacks"
    packs.mkdir()
    (packs / "b.zip").write_bytes(b"x" * 120)
    (packs / "A.zip").write_bytes(b"x" * 20)
    (packs / "unpacked").mkdir()  # a folder is not a pack file
    enabled = {"file/a.zip"}
    items = scan_content_files(packs, enabled=enabled)
    assert [item.name for item in items] == ["A.zip", "b.zip"]
    assert [item.size for item in items] == [20, 120]
    assert [item.enabled for item in items] == [True, False]
    # Without the enabled list (shader packs) the state stays unknown rather than "off".
    assert [item.enabled for item in scan_content_files(packs)] == [None, None]


def test_missing_content_folder_is_empty(ws_tmp):
    assert scan_content_files(ws_tmp / "resourcepacks") == []


def test_a_file_where_the_content_folder_should_be_raises(ws_tmp):
    packs = ws_tmp / "resourcepacks"
    packs.write_bytes(b"not a directory")
    with pytest.raises(OSError):
        scan_content_files(packs)


# ---------------------------------------------------------------- level.dat


def test_level_name_is_read_from_compressed_and_plain_nbt(ws_tmp):
    compressed = ws_tmp / "compressed.dat"
    compressed.write_bytes(_level_dat("Creative World"))
    assert read_level_name(compressed) == "Creative World"
    plain = ws_tmp / "plain.dat"
    plain.write_bytes(_level_dat("Plain World", compress=False))
    assert read_level_name(plain) == "Plain World"


def test_missing_or_damaged_level_dat_yields_no_name(ws_tmp):
    assert read_level_name(ws_tmp / "absent.dat") is None
    truncated = ws_tmp / "truncated.dat"
    truncated.write_bytes(b"\x1f\x8b\x08\x00garbage")
    assert read_level_name(truncated) is None
    marker_without_length = ws_tmp / "short.dat"
    marker_without_length.write_bytes(b"\x08\x00\x09LevelName")
    assert read_level_name(marker_without_length) is None


# ---------------------------------------------------------------- saves


def test_saves_are_named_from_level_dat_or_the_folder_and_sorted_newest_first(ws_tmp):
    saves = ws_tmp / "saves"
    saves.mkdir()
    older = saves / "folder-name"
    older.mkdir()
    (older / "level.dat").write_bytes(_level_dat("Creative World"))
    (older / "region").mkdir()
    (older / "region" / "r.0.0.mca").write_bytes(b"\0" * 4096)
    newer = saves / "survival_2025"
    newer.mkdir()
    (newer / "level.dat").write_bytes(b"\x0a\x00\x00")  # no LevelName: fall back to folder name
    now = time.time()
    os.utime(older, (now - 60, now - 60))
    os.utime(newer, (now, now))

    found = scan_saves(saves)
    assert [save.world_name for save in found] == ["survival_2025", "Creative World"]
    assert [save.folder for save in found] == ["survival_2025", "folder-name"]
    assert found[1].size == directory_size(older)
    assert found[1].modified == older.stat().st_mtime


def test_missing_saves_folder_is_empty_but_a_file_raises(ws_tmp):
    assert scan_saves(ws_tmp / "saves") == []
    (ws_tmp / "saves").write_bytes(b"not a directory")
    with pytest.raises(OSError):
        scan_saves(ws_tmp / "saves")


# ---------------------------------------------------------------- backup


def test_backup_zips_the_world_folder_and_skips_the_locked_session_file(ws_tmp):
    world = ws_tmp / "saves" / "my-world"
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(_level_dat("My World"))
    (world / "region").mkdir()
    (world / "region" / "r.0.0.mca").write_bytes(b"\0" * 128)
    (world / "session.lock").write_bytes(b"locked")
    destination = ws_tmp / "backups" / "my-world.zip"

    backup_save(world, destination)

    assert destination.is_file()
    assert not destination.with_name(destination.name + ".part").exists()
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
    assert "my-world/level.dat" in names
    assert "my-world/region/r.0.0.mca" in names
    # The game holds session.lock open while the world is loaded; including it would make a backup
    # of a running world fail for a file that has no world data in it.
    assert not any(name.endswith("session.lock") for name in names)
    assert world.is_dir(), "a backup never moves or deletes the original"


# ---------------------------------------------------------------- formatting


def test_formatting_codes_are_removed_from_display_text():
    assert (
        strip_formatting_codes("\u00a7cCozyUI\u00a7b+ \u00a77v1.10\u00a7c no-fonts \u00a70.zip")
        == "CozyUI+ v1.10 no-fonts .zip"
    )
    # The modern hex form is just more § pairs: stripping repeats until none is left.
    assert (
        strip_formatting_codes(
            "\u00a7x\u00a7f\u00a7f\u00a7a\u00a7a\u00a70\u00a70VanillaTweaks\u00a7r \u00a77v1.2.zip"
        )
        == "VanillaTweaks v1.2.zip"
    )
    assert strip_formatting_codes("plain.zip") == "plain.zip"


def test_a_name_that_is_only_codes_keeps_its_raw_form():
    assert strip_formatting_codes("\u00a7c") == ""
    assert display_file_name("\u00a7c") == "\u00a7c"
    assert display_file_name("\u00a7x\u00a71\u00a72") == "\u00a7x\u00a71\u00a72"
    assert display_file_name("\u00a7cCozy.zip") == "Cozy.zip"


# ---------------------------------------------------------------- pack icons


def test_pack_icon_is_only_the_root_entry_of_a_zip(ws_tmp):
    pack = ws_tmp / "Pack.zip"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr("pack.mcmeta", "{}")
        archive.writestr("pack.png", _PNG_1PX)
        archive.writestr("shaders/pack.png", b"a nested file is not the pack icon")
    assert pack_icon_bytes(pack) == _PNG_1PX
    # The archive is read, never extracted: only the zip exists in this folder.
    assert [path.name for path in ws_tmp.iterdir()] == ["Pack.zip"]


def test_a_folder_pack_reads_its_own_pack_png(ws_tmp):
    folder = ws_tmp / "FolderPack"
    folder.mkdir()
    (folder / "pack.png").write_bytes(_PNG_1PX)
    assert pack_icon_bytes(folder) == _PNG_1PX


def test_an_empty_pack_png_is_not_an_icon(ws_tmp):
    empty = ws_tmp / "EmptyPng.zip"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("pack.png", b"")
    assert pack_icon_bytes(empty) is None


def test_a_pack_png_over_the_size_ceiling_is_skipped_without_reading_it(ws_tmp):
    from gui.pages import instance_files

    huge = ws_tmp / "Huge.zip"
    # 20 MB of PNG-signature-prefixed zeros: a valid signature but far over the 4 MB ceiling. The
    # reader must reject it from the zip header instead of pulling 20 MB into memory.
    padding = 20 * 1024 * 1024 - len(instance_files._PNG_SIGNATURE)
    payload = instance_files._PNG_SIGNATURE + b"\0" * padding
    with zipfile.ZipFile(huge, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("pack.png", payload)
    reads: list[str] = []
    original = zipfile.ZipFile.read

    def counting_read(self, name, pwd=None):
        reads.append(str(name))
        return original(self, name, pwd)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(zipfile.ZipFile, "read", counting_read)
        assert pack_icon_bytes(huge) is None
    assert reads == [], "an oversized pack.png must be rejected before its bytes are read"


def test_missing_corrupt_or_non_png_pack_icons_are_none(ws_tmp):
    no_icon = ws_tmp / "NoIcon.zip"
    with zipfile.ZipFile(no_icon, "w") as archive:
        archive.writestr("pack.mcmeta", "{}")
    assert pack_icon_bytes(no_icon) is None

    corrupt = ws_tmp / "Corrupt.zip"
    corrupt.write_bytes(b"this is not a zip")
    assert pack_icon_bytes(corrupt) is None

    not_an_image = ws_tmp / "NotAnImage.zip"
    with zipfile.ZipFile(not_an_image, "w") as archive:
        archive.writestr("pack.png", b"GIF89a but not a png")
    assert pack_icon_bytes(not_an_image) is None

    empty = ws_tmp / "EmptyFolder"
    empty.mkdir()
    assert pack_icon_bytes(empty) is None
    assert pack_icon_bytes(ws_tmp / "absent.zip") is None


def test_pack_icon_bytes_are_cached_by_path_mtime_and_size(ws_tmp):
    from gui.pages import instance_files

    pack = ws_tmp / "Cached.zip"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr("pack.png", _PNG_1PX)
    before = instance_files._read_pack_icon.cache_info()
    assert pack_icon_bytes(pack) == _PNG_1PX
    assert pack_icon_bytes(pack) == _PNG_1PX
    after = instance_files._read_pack_icon.cache_info()
    assert after.misses == before.misses + 1, "the second read must hit the cache"
    assert after.hits >= before.hits + 1

    # A replaced pack is a different file: new bytes, so the key must miss instead of serving stale.
    changed = _PNG_1PX + b"different payload"
    pack.write_bytes(b"")
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr("pack.png", changed)
    os.utime(pack, (time.time() + 5, time.time() + 5))
    assert pack_icon_bytes(pack) == changed


# ---------------------------------------------------------------- objects


def test_size_and_time_formatting(ws_tmp):
    stamp = time.time()
    assert format_modified(stamp) == time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    assert format_modified(float("nan")) == ""
    world = ws_tmp / "world"
    (world / "region").mkdir(parents=True)
    (world / "region" / "a.mca").write_bytes(b"x" * 10)
    (world / "level.dat").write_bytes(b"y" * 5)
    assert directory_size(world) == 15
    # A file that cannot be read is not counted, and directory_size never raises for that.
    assert directory_size(ws_tmp / "absent") == 0
