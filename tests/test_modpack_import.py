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
"""Modpack import: three synthetic formats, hash verification, rollback and the CurseForge gate.

Every fixture here is built by this file (`zipfile` plus a local HTTP server on 127.0.0.1): no test
downloads a real modpack. The installer calls are the one stubbed seam - a pack import must not run
Fabric's or Forge's installer jar in a test suite - and the version/loader *profiles* those calls
would create are written to disk instead, so the importer's "already installed" branch is what these
tests exercise; the stub fails the test if an installer is reached anyway.
"""

from __future__ import annotations

import hashlib
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from launcher.modpack import (
    FORMAT_CURSEFORGE,
    FORMAT_MODRINTH,
    FORMAT_MULTIMC,
    ModpackError,
    curseforge_source,
    detect_format,
    import_modpack,
    importer,
    loader_source,
    parse_modpack,
    strip_version_suffix,
)
from launcher.modpack.errors import CurseforgeKeyMissing
from launcher.modpack.naming import sanitize_instance_name, unique_instance_name
from launcher.mods import curseforge
from launcher.mods.models import LoaderVersion
from launcher.net import downloader
from launcher.net.downloader import DownloadCancelled

# ---------------------------------------------------------------------------
# A local file server: the pack's own bytes, served from this process only
# ---------------------------------------------------------------------------


class PackServer:
    """One HTTP server for a test's pack files.

    ``payloads[name]`` is a list of bodies: each GET consumes the first entry and the last entry
    repeats, which is how "the first download was wrong, the re-download was right" is expressed
    without a second server.
    """

    def __init__(self) -> None:
        self.payloads: dict[str, list[bytes]] = {}
        self.hits: dict[str, int] = {}
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _name(self) -> str:
                return self.path.lstrip("/").split("?", 1)[0]

            def do_GET(self) -> None:
                name = self._name()
                outer.hits[name] = outer.hits.get(name, 0) + 1
                bodies = outer.payloads.get(name)
                if not bodies:
                    self.send_error(404)
                    return
                data = bodies.pop(0) if len(bodies) > 1 else bodies[0]
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_HEAD(self) -> None:
                bodies = outer.payloads.get(self._name()) or [b""]
                self.send_response(200)
                self.send_header("Content-Length", str(len(bodies[0])))
                self.end_headers()

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
    """No test may read the developer's config, and none may inherit a cancellation latch."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MCLAUNCHER_CF_KEY", "")
    downloader.reset_cancellation_for_tests()
    yield
    downloader.reset_cancellation_for_tests()


@pytest.fixture()
def offline_loader(monkeypatch):
    """Resolve a pack's loader build without touching Fabric's meta API."""

    def fake_loader(loader: str, version: str, minecraft: str) -> LoaderVersion:
        return LoaderVersion(
            loader=loader,
            version=version,
            game_version=minecraft,
            installer_url="https://example.invalid/installer.jar",
        )

    monkeypatch.setattr(loader_source, "loader_installer", fake_loader)


@pytest.fixture()
def no_installers(monkeypatch):
    """Fail loudly if an import really tries to run an installer in a test."""

    def forbidden(*args, **kwargs):
        raise AssertionError("an installer was invoked; the test profile must already exist")

    monkeypatch.setattr(importer, "install_version", forbidden)
    monkeypatch.setattr(importer, "install_loader", forbidden)


# ---------------------------------------------------------------------------
# Profiles and packs: the smallest things the launcher accepts as an install
# ---------------------------------------------------------------------------


def _write_profile(game: Path, version_id: str, *, parent: str = "") -> None:
    """A profile the launcher can load: the base carries assetIndex, a loader inherits it."""
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    payload: dict = {"id": version_id, "mainClass": "net.minecraft.client.main.Main"}
    if parent:
        payload["inheritsFrom"] = parent
    else:
        payload["assetIndex"] = {
            "id": "5",
            "sha1": "",
            "size": 0,
            "totalSize": 0,
            "url": "https://example.invalid/5.json",
        }
        payload["downloads"] = {"client": {"path": version_id + "/" + version_id + ".jar"}}
    (folder / f"{version_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    (folder / f"{version_id}.jar").write_bytes(b"client-jar")


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _build_mrpack(
    path: Path,
    *,
    name: str,
    version: str,
    minecraft: str,
    loader: str,
    loader_version: str,
    files: list[dict],
    overrides: dict[str, bytes] | None = None,
) -> Path:
    dependencies = {"minecraft": minecraft}
    if loader == "fabric":
        dependencies["fabric-loader"] = loader_version
    elif loader:
        dependencies[loader] = loader_version
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": version,
        "name": name,
        "files": files,
        "dependencies": dependencies,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        for member, data in (overrides or {}).items():
            zf.writestr("overrides/" + member, data)
    return path


def _build_curseforge(
    path: Path,
    *,
    name: str,
    version: str,
    minecraft: str,
    loader_id: str,
    files: list[dict],
    overrides: dict[str, bytes] | None = None,
) -> Path:
    manifest = {
        "minecraft": {
            "version": minecraft,
            "modLoaders": [{"id": loader_id, "primary": True}],
        },
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": name,
        "version": version,
        "files": files,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for member, data in (overrides or {}).items():
            zf.writestr("overrides/" + member, data)
    return path


def _build_multimc(
    folder: Path, *, name: str, minecraft: str, loader: str, loader_version: str
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    components = [{"uid": "net.minecraft", "version": minecraft, "cached": True}]
    if loader == "fabric":
        components.append(
            {"uid": "net.fabricmc.fabric-loader", "version": loader_version, "cached": True}
        )
    elif loader == "forge":
        components.append({"uid": "net.minecraftforge", "version": loader_version, "cached": True})
    (folder / "mmc-pack.json").write_text(
        json.dumps({"formatVersion": 1, "components": components}), encoding="utf-8"
    )
    (folder / "instance.cfg").write_text(
        "InstanceType=OneSix\nname=" + name + "\n", encoding="utf-8"
    )
    mods = folder / ".minecraft" / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    (mods / "jei.jar").write_bytes(b"jei-bytes")
    (folder / ".minecraft" / "options.txt").write_bytes(b"fov:70")
    return folder


def _zip_directory(folder: Path, target: Path, *, wrapper: str = "") -> Path:
    """Zip an instance directory, optionally inside one wrapper folder (the MultiMC export shape)."""
    with zipfile.ZipFile(target, "w") as zf:
        for entry in sorted(folder.rglob("*")):
            if entry.is_file():
                relative = entry.relative_to(folder).as_posix()
                zf.writestr(((wrapper + "/") if wrapper else "") + relative, entry.read_bytes())
    return target


# ---------------------------------------------------------------------------
# 1. Parsing: version, loader and file list for each of the three formats
# ---------------------------------------------------------------------------


def test_mrpack_parses_dependencies_and_files(ws_tmp, server):
    pack = _build_mrpack(
        ws_tmp / "Fabulously Optimized 6.4.1.mrpack",
        name="Fabulously Optimized 6.4.1",
        version="6.4.1",
        minecraft="1.20.1",
        loader="fabric",
        loader_version="0.15.11",
        files=[
            {
                "path": "mods/sodium.jar",
                "downloads": [server.url("sodium.jar")],
                "hashes": {"sha1": "a" * 40},
                "fileSize": 4096,
            },
            {
                "path": "mods/server-only.jar",
                "downloads": [server.url("server-only.jar")],
                "hashes": {"sha1": "b" * 40},
                "fileSize": 12,
                "env": {"client": "unsupported"},
            },
        ],
        overrides={"options.txt": b"fov:70"},
    )
    assert detect_format(pack) == FORMAT_MODRINTH
    spec = parse_modpack(pack)
    assert spec.fmt == FORMAT_MODRINTH
    assert (spec.minecraft, spec.loader, spec.loader_version) == ("1.20.1", "fabric", "0.15.11")
    assert [item.path for item in spec.files] == ["mods/sodium.jar"]
    assert spec.files[0].sha1 == "a" * 40 and spec.files[0].size == 4096
    assert spec.overrides == ("overrides/", "client-overrides/")
    assert spec.name == "Fabulously Optimized 6.4.1"
    assert spec.version == "6.4.1"


def test_curseforge_manifest_parses_loader_and_project_files(ws_tmp):
    pack = _build_curseforge(
        ws_tmp / "Better MC 1.20.1.zip",
        name="Better MC 1.20.1",
        version="23",
        minecraft="1.20.1",
        loader_id="forge-47.2.0",
        files=[
            {"projectID": 1234, "fileID": 5678, "required": True},
            {"projectID": 9012, "fileID": 3456, "required": False},
        ],
        overrides={"config/better.json": b"{}"},
    )
    assert detect_format(pack) == FORMAT_CURSEFORGE
    spec = parse_modpack(pack)
    assert spec.fmt == FORMAT_CURSEFORGE
    assert (spec.minecraft, spec.loader, spec.loader_version) == ("1.20.1", "forge", "47.2.0")
    assert [(item.project_id, item.file_id) for item in spec.files] == [(1234, 5678), (9012, 3456)]
    assert all(item.is_curseforge for item in spec.files)
    assert spec.overrides == ("overrides/",)
    assert spec.version == "23"


def test_multimc_directory_and_zip_parse_components(ws_tmp):
    folder = _build_multimc(
        ws_tmp / "All The Mods",
        name="All The Mods",
        minecraft="1.20.1",
        loader="fabric",
        loader_version="0.15.11",
    )
    assert detect_format(folder) == FORMAT_MULTIMC
    spec = parse_modpack(folder)
    assert spec.fmt == FORMAT_MULTIMC
    assert (spec.minecraft, spec.loader, spec.loader_version) == ("1.20.1", "fabric", "0.15.11")
    assert spec.name == "All The Mods"
    assert spec.source_is_dir is True
    assert spec.overrides == (".minecraft",)
    assert spec.files == ()

    archive = _zip_directory(folder, ws_tmp / "All The Mods.zip", wrapper="All The Mods")
    assert detect_format(archive) == FORMAT_MULTIMC
    zipped = parse_modpack(archive)
    assert (zipped.minecraft, zipped.loader, zipped.loader_version) == (
        "1.20.1",
        "fabric",
        "0.15.11",
    )
    assert zipped.source_is_dir is False
    assert zipped.overrides == ("All The Mods/.minecraft/",)


def test_a_plain_zip_is_not_a_modpack(ws_tmp):
    plain = ws_tmp / "holiday-photos.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("readme.txt", b"nothing here")
    with pytest.raises(ModpackError) as error:
        detect_format(plain)
    assert "holiday-photos.zip" in str(error.value)


def test_a_corrupt_archive_names_itself(ws_tmp):
    broken = ws_tmp / "broken.mrpack"
    broken.write_bytes(b"not a zip at all")
    with pytest.raises(ModpackError) as error:
        detect_format(broken)
    assert "broken.mrpack" in str(error.value)


# ---------------------------------------------------------------------------
# 2. The name: the pack name without its version suffix
# ---------------------------------------------------------------------------


def test_instance_name_drops_the_pack_version_suffix():
    assert strip_version_suffix("Fabulously Optimized 6.4.1") == "Fabulously Optimized"
    assert strip_version_suffix("Better MC [FORGE] 1.20.1") == "Better MC"
    # A name that is only a version keeps itself: an instance called 1.20.1 is better than none.
    assert strip_version_suffix("1.20.1") == "1.20.1"
    # The folder id is sanitised to what validate_name accepts.
    assert sanitize_instance_name("A/B: pack?") == "A B pack"
    assert len(sanitize_instance_name("x" * 60)) == 32


def test_instance_name_avoids_an_existing_folder(ws_tmp):
    game = ws_tmp / "game"
    _write_profile(game, "Fabulously Optimized")
    assert unique_instance_name("Fabulously Optimized", game) == "Fabulously Optimized-2"


# ---------------------------------------------------------------------------
# 3. Import: hashes, overrides and the sidecar
# ---------------------------------------------------------------------------


def test_mrpack_import_verifies_hashes_and_copies_overrides(
    ws_tmp, server, offline_loader, no_installers
):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    _write_profile(game, "fabric-loader-0.15.11-1.20.1", parent="1.20.1")
    mod_bytes = b"sodium-jar" * 200
    config_bytes = b'{"key": true}'
    server.payloads["sodium.jar"] = [mod_bytes]
    server.payloads["config.json"] = [config_bytes]
    pack = _build_mrpack(
        ws_tmp / "pack.mrpack",
        name="Fabulously Optimized 6.4.1",
        version="6.4.1",
        minecraft="1.20.1",
        loader="fabric",
        loader_version="0.15.11",
        files=[
            {
                "path": "mods/sodium.jar",
                "downloads": [server.url("sodium.jar")],
                "hashes": {"sha1": _sha1(mod_bytes)},
                "fileSize": len(mod_bytes),
            },
            {
                "path": "config/sodium.json",
                "downloads": [server.url("config.json")],
                "hashes": {"sha1": _sha1(config_bytes)},
                "fileSize": len(config_bytes),
            },
        ],
        overrides={"options.txt": b"fov:70"},
    )
    events: list = []
    result = import_modpack(
        pack, game_dir=game, cache_dir=ws_tmp / "cache", report=events.append
    )
    assert result.instance.id == "Fabulously Optimized"
    assert result.instance.display_name == "Fabulously Optimized"
    assert result.profile_id == "fabric-loader-0.15.11-1.20.1"
    assert result.files == 2
    target = game / "versions" / "Fabulously Optimized"
    assert (target / "mods" / "sodium.jar").read_bytes() == mod_bytes
    assert (target / "config" / "sodium.json").read_bytes() == config_bytes
    assert (target / "options.txt").read_bytes() == b"fov:70"  # overrides strip their prefix
    sidecar = json.loads((target / "minepick.json").read_text(encoding="utf-8"))
    assert sidecar["display_name"] == "Fabulously Optimized"
    phases = [event.phase for event in events]
    assert phases[0] == "parse" and phases[-1] == "done"
    assert "files" in phases and "overrides" in phases
    # Nothing was written outside the instance folder.
    assert not (game / "options.txt").exists()


def test_curseforge_import_downloads_into_mods(
    ws_tmp, server, offline_loader, no_installers, monkeypatch
):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    _write_profile(game, "1.20.1-forge-47.2.0", parent="1.20.1")
    mod_bytes = b"forge-mod" * 50
    server.payloads["bettercombat.jar"] = [mod_bytes]
    pack = _build_curseforge(
        ws_tmp / "Better MC.zip",
        name="Better MC 1.20.1",
        version="23",
        minecraft="1.20.1",
        loader_id="forge-47.2.0",
        files=[{"projectID": 1234, "fileID": 5678, "required": True}],
        overrides={"config/better.json": b"{}"},
    )
    resolved = curseforge.CfFile(
        id=5678, name="bettercombat.jar", url=server.url("bettercombat.jar"), size=len(mod_bytes)
    )
    monkeypatch.setattr(curseforge_source, "resolve_file", lambda pack_file, **kw: resolved)
    monkeypatch.setattr(curseforge, "effective_api_key", lambda: "test-key")
    result = import_modpack(pack, game_dir=game, cache_dir=ws_tmp / "cache")
    target = game / "versions" / result.instance.id
    assert (target / "mods" / "bettercombat.jar").read_bytes() == mod_bytes
    assert (target / "config" / "better.json").read_bytes() == b"{}"


def test_multimc_import_copies_the_game_directory(ws_tmp, offline_loader, no_installers):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    _write_profile(game, "fabric-loader-0.15.11-1.20.1", parent="1.20.1")
    instance = _build_multimc(
        ws_tmp / "All The Mods",
        name="All The Mods",
        minecraft="1.20.1",
        loader="fabric",
        loader_version="0.15.11",
    )
    result = import_modpack(instance, game_dir=game, cache_dir=ws_tmp / "cache")
    target = game / "versions" / result.instance.id
    assert (target / "mods" / "jei.jar").read_bytes() == b"jei-bytes"
    assert (target / "options.txt").read_bytes() == b"fov:70"
    assert result.overrides == 2


# ---------------------------------------------------------------------------
# 4. Verification: a wrong hash is re-downloaded once, then reported
# ---------------------------------------------------------------------------


def test_hash_mismatch_is_downloaded_again(ws_tmp, server, offline_loader, no_installers):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    good = b"the-real-mod-bytes"
    server.payloads["mod.jar"] = [b"wrong-bytes", good]
    pack = _build_mrpack(
        ws_tmp / "pack.mrpack",
        name="Retry Pack 1.0.0",
        version="1.0.0",
        minecraft="1.20.1",
        loader="",
        loader_version="",
        files=[
            {
                "path": "mods/mod.jar",
                "downloads": [server.url("mod.jar")],
                "hashes": {"sha1": _sha1(good)},
                "fileSize": len(good),
            }
        ],
    )
    result = import_modpack(pack, game_dir=game, cache_dir=ws_tmp / "cache")
    target = game / "versions" / result.instance.id
    assert (target / "mods" / "mod.jar").read_bytes() == good
    assert server.hits["mod.jar"] == 2  # first batch + the forced retry


def test_hash_mismatch_reports_the_file_and_rolls_the_instance_back(
    ws_tmp, server, offline_loader, no_installers
):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    server.payloads["broken.jar"] = [b"never-the-declared-bytes"]
    pack = _build_mrpack(
        ws_tmp / "pack.mrpack",
        name="Broken Pack 1.0.0",
        version="1.0.0",
        minecraft="1.20.1",
        loader="",
        loader_version="",
        files=[
            {
                "path": "mods/broken.jar",
                "downloads": [server.url("broken.jar")],
                "hashes": {"sha1": _sha1(b"expected-bytes")},
                "fileSize": len(b"expected-bytes"),
            }
        ],
    )
    with pytest.raises(ModpackError) as error:
        import_modpack(pack, game_dir=game, cache_dir=ws_tmp / "cache")
    assert "mods/broken.jar" in str(error.value)
    assert server.hits["broken.jar"] == 2
    # The half-imported instance is gone; only the pack's own (complete) profile install stays.
    assert not (game / "versions" / "Broken Pack").exists()
    assert sorted(entry.name for entry in (game / "versions").iterdir()) == ["1.20.1"]


def test_curseforge_size_mismatch_is_retried_then_reported(
    ws_tmp, server, offline_loader, no_installers, monkeypatch
):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    _write_profile(game, "1.20.1-forge-47.2.0", parent="1.20.1")
    server.payloads["mod.jar"] = [b"short"]
    pack = _build_curseforge(
        ws_tmp / "pack.zip",
        name="Size Pack",
        version="1",
        minecraft="1.20.1",
        loader_id="forge-47.2.0",
        files=[{"projectID": 1, "fileID": 2, "required": True}],
    )
    resolved = curseforge.CfFile(
        id=2, name="mod.jar", url=server.url("mod.jar"), size=500  # the declared size is not met
    )
    monkeypatch.setattr(curseforge_source, "resolve_file", lambda pack_file, **kw: resolved)
    monkeypatch.setattr(curseforge, "effective_api_key", lambda: "test-key")
    with pytest.raises(ModpackError) as error:
        import_modpack(pack, game_dir=game, cache_dir=ws_tmp / "cache")
    assert "mods/mod.jar" in str(error.value)
    assert server.hits["mod.jar"] == 2
    assert not (game / "versions" / "Size Pack").exists()


# ---------------------------------------------------------------------------
# 5. Path traversal: refused, before anything is installed
# ---------------------------------------------------------------------------


def test_a_file_path_that_escapes_the_instance_is_refused(ws_tmp):
    pack = _build_mrpack(
        ws_tmp / "evil.mrpack",
        name="Evil Pack",
        version="1",
        minecraft="1.20.1",
        loader="",
        loader_version="",
        files=[
            {
                "path": "../outside.jar",
                "downloads": ["https://example.invalid/outside.jar"],
                "hashes": {"sha1": "a" * 40},
                "fileSize": 1,
            }
        ],
    )
    with pytest.raises(ModpackError) as error:
        parse_modpack(pack)
    assert "outside.jar" in str(error.value)


def test_an_override_member_that_escapes_is_refused(ws_tmp, offline_loader, no_installers):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    pack = ws_tmp / "escape.mrpack"
    index = {
        "formatVersion": 1,
        "name": "Escape Pack",
        "files": [],
        "dependencies": {"minecraft": "1.20.1"},
    }
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        zf.writestr("overrides/../../escaped.txt", b"oops")
    with pytest.raises(ModpackError) as error:
        import_modpack(pack, game_dir=game, cache_dir=ws_tmp / "cache")
    assert "escaped.txt" in str(error.value)
    assert not (ws_tmp / "escaped.txt").exists()
    assert not (game / "versions" / "Escape Pack").exists()


# ---------------------------------------------------------------------------
# 6. Cancel: the instance folder does not survive it
# ---------------------------------------------------------------------------


def test_cancel_after_the_instance_exists_leaves_no_half_import(
    ws_tmp, server, offline_loader, no_installers
):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    good = b"mod-bytes"
    server.payloads["mod.jar"] = [good]
    pack = _build_mrpack(
        ws_tmp / "pack.mrpack",
        name="Cancelled Pack 2.0.0",
        version="2.0.0",
        minecraft="1.20.1",
        loader="",
        loader_version="",
        files=[
            {
                "path": "mods/mod.jar",
                "downloads": [server.url("mod.jar")],
                "hashes": {"sha1": _sha1(good)},
                "fileSize": len(good),
            }
        ],
        overrides={"options.txt": b"fov:70"},
    )
    stop = threading.Event()

    def report(progress) -> None:
        # The instance already exists when the file phase starts; cancelling here is the case that
        # must roll it back.
        if getattr(progress, "phase", "") == "files":
            stop.set()

    with pytest.raises(DownloadCancelled):
        import_modpack(
            pack,
            game_dir=game,
            cache_dir=ws_tmp / "cache",
            report=report,
            should_stop=stop.is_set,
        )
    assert not (game / "versions" / "Cancelled Pack").exists()
    assert sorted(entry.name for entry in (game / "versions").iterdir()) == ["1.20.1"]


# ---------------------------------------------------------------------------
# 7. CurseForge without a key: a clear refusal, never a silent skip
# ---------------------------------------------------------------------------


def test_curseforge_without_a_key_says_so_and_skips_nothing(
    ws_tmp, offline_loader, no_installers, monkeypatch
):
    game = ws_tmp / "game"
    _write_profile(game, "1.20.1")
    _write_profile(game, "1.20.1-forge-47.2.0", parent="1.20.1")
    pack = _build_curseforge(
        ws_tmp / "keyless.zip",
        name="Keyless Pack",
        version="1",
        minecraft="1.20.1",
        loader_id="forge-47.2.0",
        files=[{"projectID": 11, "fileID": 22, "required": True}],
    )
    monkeypatch.setattr(curseforge, "effective_api_key", lambda: "")
    monkeypatch.setattr(curseforge, "_key_rejected", False)
    with pytest.raises(CurseforgeKeyMissing) as error:
        import_modpack(pack, game_dir=game, cache_dir=ws_tmp / "cache")
    message = str(error.value)
    assert "API Key" in message and "CurseForge" in message
    assert not (game / "versions" / "Keyless Pack").exists()


# ---------------------------------------------------------------------------
# 8. The instance page's entry point
# ---------------------------------------------------------------------------


def test_instances_page_offers_the_import_entry(app, monkeypatch, ws_tmp):
    """The page has one 导入 menu with both entries, and it starts exactly one background job.

    The two entries share one button on purpose: a third full-width button pushed the page's
    minimum width past the product's 1000px window floor in Japanese, and both entries are the same
    kind of action (bring content from outside into this page).
    """
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QWidget

    from gui import i18n
    from gui.pages import instances_page
    from gui.tasks import TaskCenter
    from launcher import config as config_module

    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "mc"
    game.mkdir()
    cfg, cfg_path = config_module.load()
    cfg.game_dir = str(game)
    config_module.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    class _Host(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.tasks = TaskCenter(self)
            self.messages: list[tuple[str, str | None]] = []

        def set_status(self, text: str, level: str | None = None) -> None:
            self.messages.append((text, level))

    host = _Host()
    page = instances_page.InstancesPage()
    page.setParent(host)
    assert page.import_button.text() == i18n.tr("instances.import_menu")
    assert page.import_button.menu() is page.import_menu
    labels = [action.text() for action in page.import_menu.actions()]
    assert labels == [i18n.tr("instances.import"), i18n.tr("instances.import_pack")]
    assert page.import_pack_action.toolTip() == i18n.tr("instances.import_pack.hint")
    assert page.import_button.isEnabled()

    # Each menu entry is wired to its own handler (neither opens a dialog here: both are replaced).
    calls: list[str] = []
    monkeypatch.setattr(page, "_import", lambda *args: calls.append("instance"))
    monkeypatch.setattr(page, "_import_modpack", lambda *args: calls.append("pack"))
    page.import_instance_action.trigger()
    page.import_pack_action.trigger()
    assert calls == ["instance", "pack"]

    started_paths: list = []

    def fake_import(path, **kwargs):
        kwargs["report"](
            SimpleNamespace(
                phase="files",
                done_files=1,
                total_files=2,
                done_bytes=5,
                total_bytes=10,
                current="mods/x.jar",
            )
        )
        started_paths.append(path)
        return SimpleNamespace(
            instance=SimpleNamespace(id="Imported", name="Imported"), files=2, overrides=0
        )

    monkeypatch.setattr("launcher.modpack.import_modpack", fake_import)
    assert page.start_modpack_import(ws_tmp / "pack.mrpack") is True
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(8):
        app.processEvents()
    assert started_paths and started_paths[0].name == "pack.mrpack"
    done_text = i18n.tr("instances.import_pack.done", "Imported", 2)
    assert any(text == done_text for text, _level in host.messages)
    assert page.import_button.isEnabled()
    assert [task.state for task in host.tasks.tasks()] == ["done"]
    page.deleteLater()
    host.deleteLater()


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])
