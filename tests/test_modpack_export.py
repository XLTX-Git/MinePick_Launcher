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
"""Modpack export: the archive is read back by this project's own importer.

Every fixture is synthetic (`zipfile` plus a loopback HTTP server); no test downloads a real pack and
no test needs Modrinth or CurseForge. The lookup is the one seam a test replaces: the production
matchers are exercised by their own unit tests, and what matters here is the failure contract - a
lookup that fails must put *every* mod into ``overrides/`` and say so, never half a file list.

The strongest test in this file is :func:`test_mrpack_round_trips_through_the_importer`: the pack
this exporter writes is parsed by ``launcher.modpack.mrpack`` and then *installed* by
``launcher.modpack.importer`` into a second game directory, with the hashes verifying the bytes that
arrived. If that passes, the package is not merely "a zip with JSON" - it is a pack this launcher
itself accepts.
"""

from __future__ import annotations

import hashlib
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from launcher.modpack import (
    FORMAT_CURSEFORGE,
    FORMAT_MODRINTH,
    ModpackError,
    parse_modpack,
)
from launcher.modpack import archive as pack_archive
from launcher.modpack import export as pack_export
from launcher.modpack.curseforge_export import MANIFEST_NAME
from launcher.modpack.export import (
    CurseforgeIds,
    CurseforgeOutcome,
    MatchOutcome,
    ModCandidate,
    ModrinthFile,
    export_modpack,
    loader_identity,
    match_modrinth,
    plan_export,
)
from launcher.modpack.mrpack_export import INDEX_NAME
from launcher.mods.curseforge import CfFile
from launcher.mods.loaders import ModsError
from launcher.mods.models import LoaderVersion, ModFile, ModSearchHit, ModVersion, SearchHits
from launcher.net.downloader import DownloadCancelled

# ---------------------------------------------------------------------------
# A loopback file server: the pack's own bytes, served from this process only
# ---------------------------------------------------------------------------


class PackServer:
    """One HTTP server for the files a pack points at (GET only, body from memory)."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:
                name = self.path.lstrip("/").split("?", 1)[0]
                data = outer.payloads.get(name)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:  # silence the test output
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/{name}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def server():
    instance = PackServer()
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, ws_tmp):
    """No test reads the developer's config, cache or CurseForge key."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MCLAUNCHER_CF_KEY", "")
    from launcher.mods import curseforge

    curseforge.reset_session_state()
    yield
    curseforge.reset_session_state()


# ---------------------------------------------------------------------------
# A synthetic instance: two real mod jars, one metadata-less jar, and the noise
# ---------------------------------------------------------------------------


def _write_jar(path: Path, mod_id: str, display_name: str) -> None:
    """A minimal Fabric mod jar: the two files ``read_mod_metadata`` looks for."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "fabric.mod.json",
            json.dumps({"id": mod_id, "name": display_name, "version": "1.0.0"}),
        )
        zf.writestr(f"assets/{mod_id}/marker.txt", "payload-" + mod_id)


def _write_profile(game: Path, version_id: str, *, parent: str = "") -> None:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    payload: dict = {"id": version_id, "mainClass": "net.minecraft.client.main.Main"}
    if parent:
        payload["inheritsFrom"] = parent
    else:
        payload["assetIndex"] = {"id": "5", "sha1": "", "size": 0, "totalSize": 0, "url": ""}
        payload["downloads"] = {"client": {"path": version_id + "/" + version_id + ".jar"}}
    (folder / f"{version_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    (folder / f"{version_id}.jar").write_bytes(b"client-jar")


@pytest.fixture()
def instance(ws_tmp):
    """(game dir, instance dir, loader profile id) for the fixture every export test uses."""
    game = ws_tmp / "game"
    profile_id = "fabric-loader-0.15.11-1.20.1"
    _write_profile(game, profile_id)
    folder = game / "versions" / "测试包"
    (folder / "mods").mkdir(parents=True)
    (folder / "config" / "nested").mkdir(parents=True)
    (folder / "resourcepacks").mkdir()
    (folder / "shaderpacks").mkdir()
    (folder / "saves" / "world").mkdir(parents=True)
    (folder / "logs").mkdir()

    _write_jar(folder / "mods" / "sodium-0.6.5.jar", "sodium", "Sodium")
    _write_jar(folder / "mods" / "lithium-0.15.3.jar", "lithium", "Lithium")
    (folder / "mods" / "local-only.jar").write_bytes(b"a mod with no source")
    (folder / "mods" / "disabled.jar.disabled").write_bytes(b"disabled mod")
    (folder / "mods" / "readme.txt").write_text("not a mod", encoding="utf-8")
    (folder / "config" / "sodium.json").write_text('{"quality": true}', encoding="utf-8")
    (folder / "config" / "nested" / "values.toml").write_text("x = 1", encoding="utf-8")
    (folder / "resourcepacks" / "pack.zip").write_bytes(b"resource pack bytes")
    (folder / "shaderpacks" / "shader.zip").write_bytes(b"shader bytes")
    (folder / "options.txt").write_bytes(b"fov:70\n")
    (folder / "servers.dat").write_bytes(b"private address book")
    (folder / "saves" / "world" / "level.dat").write_bytes(b"world bytes")
    (folder / "logs" / "latest.log").write_text("a log line", encoding="utf-8")
    (folder / "minepick.json").write_text(
        json.dumps(
            {
                "id": "测试包",
                "display_name": "测试包",
                "note": "测试整合包 1.0\n第二行不该进 summary",
                "isolated": "on",
                "base_version": profile_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return game, folder, profile_id


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _sha512(path: Path) -> str:
    return hashlib.sha512(path.read_bytes()).hexdigest()


def _pack_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def _matched(matcher_map: dict[str, str]):
    """A matcher returning exactly the given ``relative -> url`` map."""

    def matcher(candidates, minecraft, loader):
        assert minecraft and loader
        known = {candidate.relative: candidate for candidate in candidates}
        matches = {}
        for relative, url in matcher_map.items():
            assert relative in known, f"the test matcher named a file the scan did not find: {relative}"
            matches[relative] = ModrinthFile(url=url, project=relative)
        return MatchOutcome(matches=matches)

    return matcher


# ---------------------------------------------------------------------------
# 1. The local plan: what is pack material and what is not
# ---------------------------------------------------------------------------


def test_plan_counts_mods_overrides_and_excludes_private_folders(instance) -> None:
    game, _folder, _profile = instance
    plan = plan_export("测试包", game, fmt=FORMAT_MODRINTH)

    assert plan.minecraft == "1.20.1"
    assert plan.loader == "fabric"
    assert plan.loader_version == "0.15.11"
    assert plan.name == "测试包"
    assert plan.summary == "测试整合包 1.0"
    # sodium, lithium and the metadata-less jar are candidates; the disabled jar is not.
    assert plan.mod_count == 3
    assert plan.disabled_mods == 1
    relatives = {entry.relative for entry in plan.entries}
    assert "mods/sodium-0.6.5.jar" in relatives
    assert "mods/local-only.jar" in relatives
    assert "mods/disabled.jar.disabled" in relatives
    assert "config/sodium.json" in relatives
    assert "config/nested/values.toml" in relatives
    assert "options.txt" in relatives
    assert "resourcepacks/pack.zip" in relatives
    # Private or irrelevant material must never travel in a pack.
    for absent in ("saves/world/level.dat", "logs/latest.log", "servers.dat", "minepick.json"):
        assert absent not in relatives
    # The non-jar in mods/ is counted as skipped, not as content.
    assert plan.skipped == 1
    assert plan.total_bytes == sum(entry.size for entry in plan.entries)
    assert plan.other_count == len(plan.entries) - plan.mod_count


def test_plan_never_touches_the_network(instance, monkeypatch) -> None:
    game, _folder, _profile = instance

    def forbidden(*args, **kwargs):
        raise AssertionError("plan_export must not perform a lookup")

    monkeypatch.setattr(pack_export, "match_modrinth", forbidden)
    monkeypatch.setattr(pack_export, "match_curseforge", forbidden)
    plan = plan_export("测试包", game)
    assert plan.mod_count == 3


def test_curseforge_plan_uses_one_override_tree(instance) -> None:
    game, _folder, _profile = instance
    plan = plan_export("测试包", game, fmt=FORMAT_CURSEFORGE)
    assert {entry.override for entry in plan.entries} == {"overrides"}


def test_mrpack_plan_separates_client_only_files(instance) -> None:
    game, _folder, _profile = instance
    plan = plan_export("测试包", game, fmt=FORMAT_MODRINTH)
    by_relative = {entry.relative: entry.override for entry in plan.entries}
    assert by_relative["options.txt"] == "client-overrides"
    assert by_relative["resourcepacks/pack.zip"] == "client-overrides"
    assert by_relative["shaderpacks/shader.zip"] == "client-overrides"
    assert by_relative["config/sodium.json"] == "overrides"
    assert by_relative["mods/sodium-0.6.5.jar"] == "overrides"


@pytest.mark.parametrize(
    ("version_id", "expected"),
    [
        ("fabric-loader-0.15.11-1.20.1", ("1.20.1", "fabric", "0.15.11")),
        ("1.20.1-forge-47.4.22", ("1.20.1", "forge", "47.4.22")),
        ("neoforge-21.1.5", ("1.21.1", "neoforge", "21.1.5")),
        ("1.21.11", ("1.21.11", "", "")),
    ],
)
def test_loader_identity_reads_every_profile_shape(ws_tmp, version_id, expected) -> None:
    game = ws_tmp / "shapes"
    _write_profile(game, version_id)
    assert loader_identity(game, version_id) == expected


# ---------------------------------------------------------------------------
# 2. Modrinth: hashes, index, overrides, and the importer's own parse
# ---------------------------------------------------------------------------


def test_mrpack_export_writes_a_verified_index(instance, server) -> None:
    game, folder, _profile = instance
    server.payloads["sodium.jar"] = (folder / "mods" / "sodium-0.6.5.jar").read_bytes()
    server.payloads["lithium.jar"] = (folder / "mods" / "lithium-0.15.3.jar").read_bytes()
    dest = ws_tmp_pack(game, "pack.mrpack")

    result = export_modpack(
        "测试包",
        dest,
        game,
        fmt=FORMAT_MODRINTH,
        matcher=_matched(
            {
                "mods/sodium-0.6.5.jar": server.url("sodium.jar"),
                "mods/lithium-0.15.3.jar": server.url("lithium.jar"),
            }
        ),
    )

    assert result.files == 2
    assert result.matched == 2
    assert result.mods == 3
    assert result.degraded is False
    assert result.path == dest and dest.is_file()

    with zipfile.ZipFile(dest) as zf:
        index = json.loads(zf.read(INDEX_NAME))
        assert index["formatVersion"] == 1
        assert index["game"] == "minecraft"
        assert index["versionId"] == "1.0.0"
        assert index["name"] == "测试包"
        assert index["summary"] == "测试整合包 1.0"
        assert index["dependencies"] == {
            "minecraft": "1.20.1",
            "fabric-loader": "0.15.11",
        }
        files = {entry["path"]: entry for entry in index["files"]}
        assert set(files) == {"mods/sodium-0.6.5.jar", "mods/lithium-0.15.3.jar"}
        expected_urls = {
            "mods/sodium-0.6.5.jar": server.url("sodium.jar"),
            "mods/lithium-0.15.3.jar": server.url("lithium.jar"),
        }
        for relative, entry in files.items():
            source = folder / relative
            assert entry["hashes"]["sha1"] == _sha1(source)
            assert entry["hashes"]["sha512"] == _sha512(source)
            assert entry["fileSize"] == source.stat().st_size
            assert entry["env"] == {"client": "required", "server": "optional"}
            assert entry["downloads"] == [expected_urls[relative]]
        # A resolved file is a URL, not a copy: its bytes must not be in the archive.
        names = zf.namelist()
        assert "mods/sodium-0.6.5.jar" not in names
        assert "mods/lithium-0.15.3.jar" not in names
        # Everything else is carried verbatim, in the tree the format defines.
        assert "overrides/mods/local-only.jar" in names
        assert "overrides/mods/disabled.jar.disabled" in names
        assert "overrides/config/sodium.json" in names
        assert "overrides/config/nested/values.toml" in names
        assert "client-overrides/options.txt" in names
        assert "client-overrides/resourcepacks/pack.zip" in names
        assert "client-overrides/shaderpacks/shader.zip" in names
        assert not any(name.endswith("servers.dat") for name in names)
        assert not any(name.startswith("overrides/saves/") for name in names)
        assert not any(name.startswith("overrides/logs/") for name in names)
        assert zf.read("overrides/config/sodium.json") == b'{"quality": true}'
        assert zf.read("overrides/mods/local-only.jar") == b"a mod with no source"
        assert zf.read("client-overrides/options.txt") == b"fov:70\n"
        for name in names:
            assert not Path(name).is_absolute()
            assert ":" not in name
            assert ".." not in Path(name).parts
            pack_archive.validate_member(name)


def test_mrpack_round_trips_through_the_importer(instance, server, monkeypatch, ws_tmp) -> None:
    """Export, then parse *and install* the archive with this project's own importer."""
    game, folder, profile_id = instance
    server.payloads["sodium.jar"] = (folder / "mods" / "sodium-0.6.5.jar").read_bytes()
    server.payloads["lithium.jar"] = (folder / "mods" / "lithium-0.15.3.jar").read_bytes()
    dest = ws_tmp_pack(game, "roundtrip.mrpack")
    export_modpack(
        "测试包",
        dest,
        game,
        matcher=_matched(
            {
                "mods/sodium-0.6.5.jar": server.url("sodium.jar"),
                "mods/lithium-0.15.3.jar": server.url("lithium.jar"),
            }
        ),
    )

    # --- the parser: version, loader and the file list the index declares -------------------
    spec = parse_modpack(dest)
    assert spec.fmt == FORMAT_MODRINTH
    assert spec.name == "测试包"
    assert spec.version == "1.0.0"
    assert spec.minecraft == "1.20.1"
    assert spec.loader == "fabric"
    assert spec.loader_version == "0.15.11"
    assert {item.path for item in spec.files} == {
        "mods/sodium-0.6.5.jar",
        "mods/lithium-0.15.3.jar",
    }
    for item in spec.files:
        assert item.sha1 == _sha1(folder / item.path)
        assert item.url.startswith("http://127.0.0.1:")

    # --- the installer: a second game directory receives the same instance ------------------
    from launcher.modpack import importer, loader_source

    target = ws_tmp / "imported"
    _write_profile(target, "1.20.1")
    _write_profile(target, profile_id)
    monkeypatch.setattr(
        loader_source,
        "loader_installer",
        lambda loader, version, minecraft: LoaderVersion(
            loader=loader,
            version=version,
            game_version=minecraft,
            installer_url="https://example.invalid/installer.jar",
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("the profile must already exist in the import target")

    monkeypatch.setattr(importer, "install_version", forbidden)
    monkeypatch.setattr(importer, "install_loader", forbidden)
    imported = importer.import_modpack(
        dest,
        game_dir=target,
        cache_dir=ws_tmp / "cache",
        concurrency=2,
    )

    installed = target / "versions" / imported.name
    assert imported.name == "测试包"
    assert imported.files == 2
    assert imported.spec.minecraft == "1.20.1"
    assert imported.spec.loader_version == "0.15.11"
    assert (installed / "mods" / "sodium-0.6.5.jar").read_bytes() == (
        folder / "mods" / "sodium-0.6.5.jar"
    ).read_bytes()
    assert (installed / "mods" / "lithium-0.15.3.jar").read_bytes() == (
        folder / "mods" / "lithium-0.15.3.jar"
    ).read_bytes()
    # Overrides: every carried file arrived, byte for byte, and the excluded ones did not.
    assert (installed / "mods" / "local-only.jar").read_bytes() == b"a mod with no source"
    assert (installed / "mods" / "disabled.jar.disabled").read_bytes() == b"disabled mod"
    assert (installed / "config" / "sodium.json").read_bytes() == b'{"quality": true}'
    assert (installed / "config" / "nested" / "values.toml").read_text(encoding="utf-8") == "x = 1"
    assert (installed / "options.txt").read_text(encoding="utf-8") == "fov:70\n"
    assert (installed / "resourcepacks" / "pack.zip").read_bytes() == b"resource pack bytes"
    assert (installed / "shaderpacks" / "shader.zip").read_bytes() == b"shader bytes"
    assert not (installed / "saves").exists()
    assert not (installed / "logs").exists()
    assert not (installed / "servers.dat").exists()


def test_a_failed_lookup_packs_every_mod_into_overrides(instance) -> None:
    game, folder, _profile = instance
    dest = ws_tmp_pack(game, "offline.mrpack")

    def offline(candidates, minecraft, loader):
        raise RuntimeError("Modrinth is unreachable")

    result = export_modpack("测试包", dest, game, matcher=offline)
    assert result.degraded is True
    assert result.files == 0
    assert result.matched == 0
    assert result.overrides == len(plan_export("测试包", game).entries)
    with zipfile.ZipFile(dest) as zf:
        index = json.loads(zf.read(INDEX_NAME))
        assert index["files"] == []
        assert "overrides/mods/sodium-0.6.5.jar" in zf.namelist()
        assert "overrides/mods/lithium-0.15.3.jar" in zf.namelist()
        assert "overrides/mods/local-only.jar" in zf.namelist()
        assert zf.read("overrides/mods/sodium-0.6.5.jar") == (
            folder / "mods" / "sodium-0.6.5.jar"
        ).read_bytes()
    spec = parse_modpack(dest)
    assert spec.files == ()
    assert spec.minecraft == "1.20.1" and spec.loader == "fabric"


def test_a_matcher_that_reports_failure_also_packs_everything(instance) -> None:
    game, _folder, _profile = instance
    dest = ws_tmp_pack(game, "reported.mrpack")
    result = export_modpack(
        "测试包",
        dest,
        game,
        matcher=lambda candidates, minecraft, loader: MatchOutcome(failed=True),
    )
    assert result.degraded is True and result.files == 0
    with zipfile.ZipFile(dest) as zf:
        assert json.loads(zf.read(INDEX_NAME))["files"] == []


# ---------------------------------------------------------------------------
# 3. CurseForge: project/file ids where known, overrides for the rest
# ---------------------------------------------------------------------------


def test_curseforge_export_writes_the_manifest_and_overrides(instance) -> None:
    game, _folder, _profile = instance
    dest = ws_tmp_pack(game, "pack.zip")

    def cf_matcher(candidates, minecraft, loader):
        known = {candidate.relative for candidate in candidates}
        assert "mods/sodium-0.6.5.jar" in known
        return CurseforgeOutcome(
            matches={"mods/sodium-0.6.5.jar": CurseforgeIds(project_id=394468, file_id=987654)}
        )

    result = export_modpack(
        "测试包", dest, game, fmt=FORMAT_CURSEFORGE, curseforge_matcher=cf_matcher
    )
    assert result.files == 1
    assert result.mods == 3

    with zipfile.ZipFile(dest) as zf:
        manifest = json.loads(zf.read(MANIFEST_NAME))
        assert manifest["manifestType"] == "minecraftModpack"
        assert manifest["manifestVersion"] == 1
        assert manifest["minecraft"] == {
            "version": "1.20.1",
            "modLoaders": [{"id": "fabric-0.15.11", "primary": True}],
        }
        assert manifest["files"] == [
            {"projectID": 394468, "fileID": 987654, "required": True}
        ]
        assert manifest["overrides"] == "overrides"
        names = zf.namelist()
        # CurseForge has exactly one override tree; client-only files use it too.
        assert "overrides/options.txt" in names
        assert "overrides/resourcepacks/pack.zip" in names
        assert "overrides/mods/lithium-0.15.3.jar" in names
        assert "overrides/mods/local-only.jar" in names
        assert "mods/sodium-0.6.5.jar" not in names
        assert not any(name.startswith("client-overrides/") for name in names)
        assert zf.read("overrides/config/sodium.json") == b'{"quality": true}'
        for name in names:
            assert not Path(name).is_absolute()
            assert ":" not in name
            assert ".." not in Path(name).parts
            pack_archive.validate_member(name)

    spec = parse_modpack(dest)
    assert spec.fmt == FORMAT_CURSEFORGE
    assert spec.minecraft == "1.20.1"
    assert spec.loader == "fabric"
    assert spec.loader_version == "0.15.11"
    assert [(item.project_id, item.file_id) for item in spec.files] == [(394468, 987654)]


def test_curseforge_without_ids_puts_every_mod_in_overrides(instance) -> None:
    game, _folder, _profile = instance
    dest = ws_tmp_pack(game, "cf-fallback.zip")
    result = export_modpack(
        "测试包",
        dest,
        game,
        fmt=FORMAT_CURSEFORGE,
        curseforge_matcher=lambda candidates, minecraft, loader: CurseforgeOutcome(),
    )
    assert result.files == 0
    assert result.matched == 0
    with zipfile.ZipFile(dest) as zf:
        manifest = json.loads(zf.read(MANIFEST_NAME))
        assert manifest["files"] == []
        assert "overrides/mods/sodium-0.6.5.jar" in zf.namelist()
    spec = parse_modpack(dest)
    assert spec.files == ()
    assert spec.minecraft == "1.20.1"


# ---------------------------------------------------------------------------
# 4. The failure contract: cancel, empty instance, path guard
# ---------------------------------------------------------------------------


def test_cancelling_mid_write_leaves_no_file(instance, monkeypatch) -> None:
    game, _folder, _profile = instance
    dest = ws_tmp_pack(game, "cancelled.mrpack")
    written: list[int] = []
    # The reporter coalesces ticks for the interface; a test needs every tick to stop between two
    # files. The cancellation path itself is unchanged.
    monkeypatch.setattr(pack_export, "REPORT_INTERVAL", 0.0)

    def report(progress) -> None:
        if progress.phase == "write" and progress.done_files >= 1:
            written.append(progress.done_files)

    def should_stop() -> bool:
        return bool(written)

    with pytest.raises(DownloadCancelled):
        export_modpack(
            "测试包",
            dest,
            game,
            matcher=lambda candidates, minecraft, loader: MatchOutcome(),
            on_progress=report,
            should_stop=should_stop,
        )
    assert written, "the export never reached its write phase"
    assert not dest.exists(), "a cancelled export left the archive behind"
    assert not list(dest.parent.glob("*.tmp")), "a cancelled export left its .tmp behind"
    assert not list(dest.parent.iterdir()), "a cancelled export left something in the folder"


def test_an_empty_instance_is_refused_before_a_file_is_written(ws_tmp) -> None:
    game = ws_tmp / "empty"
    profile_id = "fabric-loader-0.15.11-1.20.1"
    _write_profile(game, profile_id)
    folder = game / "versions" / "空包"
    (folder / "mods").mkdir(parents=True)
    (folder / "minepick.json").write_text(
        json.dumps(
            {
                "id": "空包",
                "display_name": "空包",
                "isolated": "on",
                "base_version": profile_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dest = ws_tmp / "empty.mrpack"
    with pytest.raises(ModpackError):
        export_modpack("空包", dest, game)
    assert not dest.exists()
    assert not list(dest.parent.glob("*.tmp"))


def test_export_member_guard_refuses_absolute_and_escaping_names() -> None:
    with pytest.raises(ModpackError):
        pack_export._write_member("../outside.jar")
    with pytest.raises(ModpackError):
        pack_export._write_member("C:/Windows/system32/evil.jar")
    with pytest.raises(ModpackError):
        pack_export._write_member("/etc/passwd")
    # A normal member is accepted (the guard is not refusing everything).
    pack_export._write_member("overrides/mods/ok.jar")


# ---------------------------------------------------------------------------
# 5. The normalising helpers (small, but they decide what a pack says)
# ---------------------------------------------------------------------------


def test_match_key_ignores_case_and_separators() -> None:
    assert pack_export._match_key("Complementary Reimagined") == pack_export._match_key(
        "complementary-reimagined"
    )
    assert pack_export._match_key("sodium") != pack_export._match_key("sodium-extra")


def ws_tmp_pack(game: Path, name: str) -> Path:
    """A destination next to the fixture game dir (created on demand by the exporter)."""
    return game.parent / "out" / name


def test_plan_summary_keeps_the_first_line_only(instance) -> None:
    game, _folder, _profile = instance
    plan = plan_export("测试包", game)
    assert "\n" not in plan.summary
    assert plan.summary == "测试整合包 1.0"


# ---------------------------------------------------------------------------
# 6. The production matchers, against fakes (no network at all)
# ---------------------------------------------------------------------------


class _FakeModrinth:
    """The two ``launcher.mods.modrinth`` calls the exporter makes, with fixed answers."""

    def __init__(self, hits, versions) -> None:
        self.hits = list(hits)
        self.versions = list(versions)
        self.queries: list[str] = []

    def search_projects(self, query, *, limit=5):
        self.queries.append(query)
        return SearchHits(self.hits)

    def fetch_versions(self, slug):
        return list(self.versions)


class _FakeCurseforge:
    """The two ``launcher.mods.curseforge`` calls the exporter makes, with fixed answers."""

    def __init__(self, hits, files) -> None:
        self.hits = list(hits)
        self.files = list(files)

    def search_projects(self, query, *, limit=10, game_version="", loader=""):
        return SearchHits(self.hits)

    def list_files(self, mod_id, *, game_version="", loader=""):
        return list(self.files)


def _candidate(*, sha1: str = "a" * 40, size: int = 100, name: str = "sodium.jar") -> ModCandidate:
    return ModCandidate(
        relative="mods/" + name,
        source=Path(name),
        mod_id="sodium",
        display_name="Sodium",
        sha1=sha1,
        size=size,
    )


def test_modrinth_match_is_decided_by_the_local_hash() -> None:
    fake = _FakeModrinth(
        [ModSearchHit(slug="sodium", title="Sodium")],
        [
            ModVersion(
                version_id="v1",
                files=[
                    ModFile(
                        filename="sodium.jar",
                        url="https://cdn.example.invalid/sodium.jar",
                        sha1="A" * 40,
                    )
                ],
            )
        ],
    )
    found = pack_export._modrinth_file(fake, _candidate(sha1="a" * 40))
    assert found is not None
    assert found.url == "https://cdn.example.invalid/sodium.jar"
    assert found.version_id == "v1"
    # The same project, a different build: no download link may be guessed.
    assert pack_export._modrinth_file(fake, _candidate(sha1="b" * 40)) is None


def test_modrinth_match_requires_an_exact_project_name() -> None:
    fake = _FakeModrinth([ModSearchHit(slug="sodium-extra", title="Sodium Extra")], [])
    assert pack_export._modrinth_file(fake, _candidate()) is None
    # It tried the mod id first, then the display name (the second is what a Forge jar needs).
    assert fake.queries == ["sodium", "Sodium"]


def test_match_modrinth_degrades_instead_of_raising(monkeypatch) -> None:
    from launcher.mods import modrinth

    def offline(*args, **kwargs):
        raise ModsError("offline")

    monkeypatch.setattr(modrinth, "search_projects", offline)
    outcome = match_modrinth([_candidate()], "1.20.1", "fabric")
    assert outcome.failed is True
    assert outcome.matches == {}


def test_curseforge_match_requires_the_same_name_and_size() -> None:
    fake = _FakeCurseforge(
        [ModSearchHit(slug="394468", title="Sodium", source="curseforge")],
        [
            CfFile(
                id=987654,
                name="sodium-0.6.5.jar",
                url="https://edge.example.invalid/sodium.jar",
                size=320,
            )
        ],
    )
    candidate = _candidate(size=320, name="sodium-0.6.5.jar")
    found = pack_export._curseforge_ids(fake, candidate, "1.20.1", "fabric")
    assert found == CurseforgeIds(project_id=394468, file_id=987654)
    # A size mismatch is not a match: the wrong fileID would install a different mod.
    other = _candidate(size=321, name="sodium-0.6.5.jar")
    assert pack_export._curseforge_ids(fake, other, "1.20.1", "fabric") is None


def test_match_curseforge_without_a_key_makes_no_request(monkeypatch) -> None:
    from launcher.mods import curseforge

    called: list[object] = []
    monkeypatch.setattr(curseforge, "has_key", lambda: False)
    monkeypatch.setattr(
        curseforge, "search_projects", lambda *args, **kwargs: called.append(args)
    )
    outcome = pack_export.match_curseforge([_candidate()], "1.20.1", "fabric")
    assert outcome == CurseforgeOutcome()
    assert called == []
