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
"""The modpack scanner/exporter with a content selection and 打包资源文件.

The export page's nine checkboxes are this module's ``categories``: an unchecked category is filtered
out of the plan, so it can never reach ``overrides/``. These tests pin the mapping (category -> archive
tree), the name/version fields of both formats, and the embed switch (a real export writes the jar into
``overrides/`` and leaves ``files[]`` empty).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from launcher.instances import INSTANCE_META_FILENAME, Instance
from launcher.modpack import (
    CATEGORIES,
    DEFAULT_CATEGORIES,
    FORMAT_CURSEFORGE,
    FORMAT_MODRINTH,
    CurseforgeOutcome,
    MatchOutcome,
    ModpackError,
    export_modpack,
    parse_modpack,
    plan_export,
)
from launcher.modpack import export as pack_export


def _write_profile(game: Path, version_id: str, *, inherits: str | None = None) -> Path:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    raw = {"id": version_id, "type": "release", "mainClass": "x.Main", "libraries": []}
    if inherits:
        raw["inheritsFrom"] = inherits
    (folder / f"{version_id}.json").write_text(json.dumps(raw), encoding="utf-8")
    (folder / f"{version_id}.jar").write_bytes(b"jar")
    return folder


def _instance(tmp: Path) -> tuple[Path, Path]:
    """A game folder with one Fabric instance holding a little of every category."""
    game = tmp / "mc"
    _write_profile(game, "1.21.11")
    _write_profile(game, "fabric-loader-0.19.3-1.21.11", inherits="1.21.11")
    folder = _write_profile(game, "fabric-loader-0.19.3-1.21.11")
    meta = Instance(
        id="fabric-loader-0.19.3-1.21.11",
        display_name="测试包",
        created_at=1.0,
    )
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    (folder / "mods" / "sodium.jar").write_bytes(b"PK\x03\x04sodium")
    (folder / "mods" / "lithium.jar.disabled").write_bytes(b"PK\x03\x04lithium")
    (folder / "config").mkdir()
    (folder / "config" / "sodium.json").write_text('{"q":true}', encoding="utf-8")
    (folder / "options.txt").write_text("fov:70\n", encoding="utf-8")
    (folder / "resourcepacks").mkdir()
    (folder / "resourcepacks" / "pack.zip").write_bytes(b"pack")
    (folder / "shaderpacks").mkdir()
    (folder / "shaderpacks" / "shader.zip").write_bytes(b"shader")
    (folder / "saves" / "world").mkdir(parents=True)
    (folder / "saves" / "world" / "level.dat").write_bytes(b"world")
    (folder / "screenshots").mkdir()
    (folder / "screenshots" / "shot.png").write_bytes(b"png")
    (folder / "schematics").mkdir()
    (folder / "schematics" / "base.litematic").write_bytes(b"schematic")
    (folder / "servers.dat").write_bytes(b"servers")
    (folder / "datapacks").mkdir()
    (folder / "datapacks" / "tweak.zip").write_bytes(b"tweak")
    (folder / "logs").mkdir()
    (folder / "logs" / "latest.log").write_text("log", encoding="utf-8")
    return game, folder


def test_the_plan_is_categorised_and_filtered_by_the_selection(ws_tmp):
    game, _folder = _instance(ws_tmp)
    plan = plan_export(
        "fabric-loader-0.19.3-1.21.11", game, categories=CATEGORIES
    )
    stats = plan.category_stats()
    assert stats["mods"].files == 2  # the disabled jar is carried too
    assert stats["config"].files == 2  # sodium.json + options.txt
    assert stats["saves"].files == 1
    assert stats["other"].files == 1  # datapacks/; logs/ is excluded on purpose
    assert stats["servers"].files == 1

    selected = plan_export(
        "fabric-loader-0.19.3-1.21.11", game, categories=DEFAULT_CATEGORIES
    )
    relatives = {entry.relative for entry in selected.entries}
    assert "mods/sodium.jar" in relatives
    assert "config/sodium.json" in relatives
    assert "saves/world/level.dat" not in relatives
    assert "screenshots/shot.png" not in relatives
    assert "servers.dat" not in relatives
    assert selected.total_bytes < plan.total_bytes


def test_an_unchecked_category_never_enters_the_archive(ws_tmp):
    game, _folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "mods-only.mrpack"
    export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        categories={"mods"},
        matcher=lambda *_a: MatchOutcome(failed=True),
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
    assert "overrides/mods/sodium.jar" in names
    assert not any(name.startswith("overrides/config/") for name in names)
    assert not any(name.startswith("overrides/saves/") for name in names)
    assert not any(name.endswith("servers.dat") for name in names)
    assert "client-overrides/options.txt" not in names


def test_name_and_version_reach_both_formats(ws_tmp):
    game, _folder = _instance(ws_tmp)
    pack_name = "红石整合"
    pack_version = "2.3.0"

    mrpack = ws_tmp / "out" / "named.mrpack"
    export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        mrpack,
        game,
        name=pack_name,
        version=pack_version,
        matcher=lambda *_a: MatchOutcome(failed=True),
    )
    with zipfile.ZipFile(mrpack) as archive:
        index = json.loads(archive.read("modrinth.index.json"))
        assert index["name"] == pack_name
        assert index["versionId"] == pack_version
        parsed = parse_modpack(mrpack)
        assert parsed.name == pack_name

    curseforge = ws_tmp / "out" / "named.zip"
    export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        curseforge,
        game,
        fmt=FORMAT_CURSEFORGE,
        name=pack_name,
        version=pack_version,
        curseforge_matcher=lambda *_a: CurseforgeOutcome(),
    )
    with zipfile.ZipFile(curseforge) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["name"] == pack_name
        assert manifest["version"] == pack_version


def test_embedding_puts_the_jars_into_overrides_and_empties_files(ws_tmp):
    game, _folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "embedded.mrpack"

    def must_not_look(*_args):
        raise AssertionError("打包资源文件 must not run the download lookup")

    result = export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        embed_files=True,
        matcher=must_not_look,
    )
    assert result.embedded is True
    with zipfile.ZipFile(dest) as archive:
        index = json.loads(archive.read("modrinth.index.json"))
        names = archive.namelist()
    assert index["files"] == []
    assert "overrides/mods/sodium.jar" in names


def test_without_embedding_a_matched_mod_is_a_download_not_a_file(ws_tmp):
    from launcher.modpack import ModrinthFile

    game, folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "linked.mrpack"
    jar = folder / "mods" / "sodium.jar"

    def matcher(candidates, _minecraft, _loader):
        matches = {
            candidate.relative: ModrinthFile(url="https://example.invalid/sodium.jar")
            for candidate in candidates
            if candidate.relative.endswith("sodium.jar")
        }
        return MatchOutcome(matches=matches)

    export_modpack("fabric-loader-0.19.3-1.21.11", dest, game, matcher=matcher)
    with zipfile.ZipFile(dest) as archive:
        index = json.loads(archive.read("modrinth.index.json"))
        names = archive.namelist()
    assert [entry["path"] for entry in index["files"]] == ["mods/sodium.jar"]
    assert "overrides/mods/sodium.jar" not in names
    # The disabled jar has no download and is carried instead.
    assert "overrides/mods/lithium.jar.disabled" in names
    assert jar.exists()


def test_curseforge_ignores_the_embed_flag(ws_tmp):
    game, _folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "cf-embed.zip"
    result = export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        fmt=FORMAT_CURSEFORGE,
        embed_files=True,
        curseforge_matcher=lambda *_a: CurseforgeOutcome(),
    )
    assert result.embedded is False
    with zipfile.ZipFile(dest) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        names = archive.namelist()
    assert manifest["files"] == []
    assert "overrides/mods/sodium.jar" in names


def test_a_selected_plan_round_trips_through_the_importer(ws_tmp):
    """The importer still reads what the new selection writes (same parse, fewer files)."""
    game, _folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "roundtrip.mrpack"
    export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        categories={"mods", "config", "saves"},
        matcher=lambda *_a: MatchOutcome(failed=True),
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
    assert "client-overrides/saves/world/level.dat" in names
    spec = parse_modpack(dest)
    # The formal install round trip lives in tests/test_modpack_import.py; here the pack must at
    # least parse to the same format, game version and loader the export started from.
    assert spec.fmt == FORMAT_MODRINTH
    assert spec.minecraft == "1.21.11"
    assert spec.loader == "fabric"
    assert spec.loader_version == "0.19.3"


def test_a_file_level_selection_is_the_only_thing_written(ws_tmp):
    """The page's per-file boxes: an unticked file is absent even though its category is ticked."""
    game, _folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "picked.mrpack"
    result = export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        categories=CATEGORIES,
        files={"mods/sodium.jar", "config/sodium.json", "saves/world/level.dat"},
        matcher=lambda *_a: MatchOutcome(failed=True),
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
    assert "overrides/mods/sodium.jar" in names
    assert "overrides/config/sodium.json" in names
    assert "client-overrides/saves/world/level.dat" in names
    # The other file of the same categories stays out: the selection is the file, not the folder.
    assert "overrides/mods/lithium.jar.disabled" not in names
    assert "client-overrides/options.txt" not in names
    assert result.overrides == 3
    assert result.missing == 0


def test_embedding_is_also_limited_to_the_ticked_files(ws_tmp):
    """打包资源文件 changes where the bytes travel, not which files are selected."""
    game, _folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "picked-embedded.mrpack"

    def must_not_look(*_args):
        raise AssertionError("打包资源文件 must not run the download lookup")

    result = export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        categories={"mods"},
        files={"mods/sodium.jar"},
        embed_files=True,
        matcher=must_not_look,
    )
    assert result.embedded is True
    with zipfile.ZipFile(dest) as archive:
        index = json.loads(archive.read("modrinth.index.json"))
        names = archive.namelist()
    assert index["files"] == []
    assert "overrides/mods/sodium.jar" in names
    assert "overrides/mods/lithium.jar.disabled" not in names


def test_a_ticked_file_deleted_before_the_export_is_skipped_and_counted(ws_tmp):
    """Deleting a ticked file after the page counted it must not fail the export."""
    game, folder = _instance(ws_tmp)
    (folder / "mods" / "sodium.jar").unlink()
    dest = ws_tmp / "out" / "vanished.mrpack"
    result = export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        categories=CATEGORIES,
        files={"mods/sodium.jar", "config/sodium.json"},
        matcher=lambda *_a: MatchOutcome(failed=True),
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
    assert result.missing == 1
    assert "overrides/config/sodium.json" in names
    assert "overrides/mods/sodium.jar" not in names


def test_every_ticked_file_gone_is_an_explicit_refusal(ws_tmp):
    """Nothing left to write is still an error, but it names how many ticked files disappeared."""
    game, folder = _instance(ws_tmp)
    (folder / "mods" / "sodium.jar").unlink()
    with pytest.raises(ModpackError) as raised:
        export_modpack(
            "fabric-loader-0.19.3-1.21.11",
            ws_tmp / "out" / "empty.mrpack",
            game,
            categories=CATEGORIES,
            files={"mods/sodium.jar"},
            matcher=lambda *_a: MatchOutcome(failed=True),
        )
    assert "1" in str(raised.value)
    assert not (ws_tmp / "out" / "empty.mrpack").exists()


def test_a_file_deleted_between_the_scan_and_its_turn_is_skipped(ws_tmp, monkeypatch):
    """The scan saw the jar; it disappears while being hashed - the archive still gets the rest."""
    game, _folder = _instance(ws_tmp)
    dest = ws_tmp / "out" / "mid-run.mrpack"
    real_hash = pack_export._hash_file

    def vanish(path, **kwargs):
        Path(path).unlink()
        return real_hash(path, **kwargs)

    monkeypatch.setattr(pack_export, "_hash_file", vanish)
    result = export_modpack(
        "fabric-loader-0.19.3-1.21.11",
        dest,
        game,
        categories={"mods"},
        matcher=lambda *_a: MatchOutcome(failed=True),
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
    assert result.missing == 1
    assert "overrides/mods/sodium.jar" not in names
    # The disabled jar is not hashed, so it still travels in overrides/.
    assert "overrides/mods/lithium.jar.disabled" in names
