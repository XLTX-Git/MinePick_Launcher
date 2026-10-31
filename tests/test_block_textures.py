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

"""Version-type -> block mapping, the three-step lookup, and the background prefetch.

No test here opens a socket: every network interaction goes through a small duck-typed client, which
also proves the "a cached texture is never fetched again" rule by counting the calls it receives.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from launcher.meta import block_textures as bt

PNG = b"\x89PNG\r\n\x1a\n" + b"icon"


def _write_png(path: Path, payload: bytes = PNG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. version type -> block name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version_id", "version_type", "expected"),
    (
        ("1.21.8", "release", "grass_block_side"),
        ("26w02a", "snapshot", "command_block_front"),
        ("24w14potato", "snapshot", "gold_block"),
        ("2.0", "release", "gold_block"),
        ("25w14craftmine", "snapshot", "gold_block"),
        ("b1.7.3", "old_beta", "cobblestone"),
        ("a1.2.6", "old_alpha", "cobblestone"),
        # early releases are typed "release" by Mojang but are the ancient category
        ("1.0", "release", "cobblestone"),
        ("1.6.4", "release", "cobblestone"),
        ("1.7.10", "release", "grass_block_side"),
        ("1.21.11", "release", "grass_block_side"),
    ),
)
def test_version_type_maps_to_the_expected_block(version_id, version_type, expected):
    category = bt.icon_category(version_id, version_type)
    assert bt.block_name_for_category(category) == expected


def test_category_fallback_is_a_normal_release():
    assert bt.block_name_for_category("not-a-category") == "grass_block_side"


def test_unknown_type_is_guessed_from_the_id():
    assert bt.guess_version_type("26w02a") == "snapshot"
    assert bt.guess_version_type("b1.7.3") == "old_beta"
    assert bt.guess_version_type("a1.2.6") == "old_alpha"
    assert bt.guess_version_type("1.20.1") == "release"


def test_loader_detection_follows_the_inheritance_chain(ws_tmp):
    game = ws_tmp / "game"
    _write_json(
        game / "versions" / "整合包" / "整合包.json",
        {"id": "整合包", "inheritsFrom": "fabric-loader-0.19.3-1.21.11"},
    )
    assert bt.loader_kind("整合包", game_dir=game) == "fabric"
    assert bt.loader_kind("1.20.1-forge-47.4.22") == "forge"
    assert bt.loader_kind("neoforge-21.1.5") == "neoforge"
    assert bt.loader_kind("quilt-loader-0.24.0-1.20.1") == "quilt"
    assert bt.loader_kind("1.21.11") is None
    assert bt.loader_kind_for_label("Fabric 0.19.3") == "fabric"
    assert bt.loader_kind_for_label("NeoForge 21.1.5") == "neoforge"
    assert bt.loader_kind_for_label("原版") is None


def test_version_type_reads_the_profile_then_the_cached_manifest(ws_tmp):
    game = ws_tmp / "game"
    _write_json(
        game / "versions" / "25w45a" / "25w45a.json",
        {"id": "25w45a", "type": "snapshot"},
    )
    assert bt.version_type_for("25w45a", game) == "snapshot"

    cache = ws_tmp / "data" / "cache"
    _write_json(
        cache / "version_manifest.json",
        {
            "fetched_at": 0,
            "manifest": {
                "latest": {},
                "versions": [{"id": "1.0", "type": "release", "url": "https://x/"}],
            },
        },
    )
    # no profile on disk: falls back to the cached manifest rather than the id shape
    assert bt.version_type_for("1.0", ws_tmp / "empty", cache_dir=cache) == "release"
    # and with neither source the id shape is the last resort
    assert bt.version_type_for("26w02a", ws_tmp / "empty", cache_dir=ws_tmp / "no-cache") == "snapshot"


# ---------------------------------------------------------------------------
# 2. three-step lookup order
# ---------------------------------------------------------------------------


def test_own_assets_win_over_the_cache(ws_tmp):
    game = ws_tmp / "game"
    digest = "ab" + "c" * 38
    _write_json(
        game / "assets" / "indexes" / "1.20.1.json",
        {
            "objects": {
                "minecraft/textures/block/grass_block_side.png": {
                    "hash": digest,
                    "size": len(PNG),
                }
            }
        },
    )
    _write_json(
        game / "versions" / "1.20.1" / "1.20.1.json",
        {"id": "1.20.1", "assetIndex": {"id": "1.20.1", "url": "https://x/"}},
    )
    own = _write_png(game / "assets" / "objects" / digest[:2] / digest)
    cache_root = bt.texture_cache_root(ws_tmp / "data")
    cached = _write_png(cache_root / bt.MODERN_BUCKET / "grass_block_side.png")

    found = bt.find_block_texture(
        "grass_block_side", game_dir=game, version_id="1.20.1", cache_root=cache_root
    )
    assert found == own
    assert found != cached


def test_old_loose_layout_and_the_pre_113_name_alias(ws_tmp):
    game = ws_tmp / "game"
    old = _write_png(game / "assets" / "minecraft" / "textures" / "blocks" / "grass_side.png")
    found = bt.find_block_texture("grass_block_side", game_dir=game, version_id="1.6.4")
    assert found == old


def test_cache_is_used_when_the_version_has_no_assets(ws_tmp):
    game = ws_tmp / "game"
    (game / "assets").mkdir(parents=True)
    cache_root = bt.texture_cache_root(ws_tmp / "data")
    cached = _write_png(cache_root / bt.MODERN_BUCKET / "cobblestone.png")

    assert (
        bt.find_block_texture(
            "cobblestone", game_dir=game, version_id="1.6.4", cache_root=cache_root
        )
        == cached
    )


def test_version_cache_bucket_is_preferred_over_modern(ws_tmp):
    cache_root = bt.texture_cache_root(ws_tmp / "data")
    versioned = _write_png(cache_root / "1.20.1" / "gold_block.png")
    _write_png(cache_root / bt.MODERN_BUCKET / "gold_block.png")

    assert bt.find_block_texture("gold_block", version_id="1.20.1", cache_root=cache_root) == versioned


def test_no_assets_and_no_cache_returns_none(ws_tmp):
    assert (
        bt.find_block_texture(
            "command_block_front",
            game_dir=ws_tmp / "no-game",
            version_id="26w02a",
            cache_root=bt.texture_cache_root(ws_tmp / "data"),
        )
        is None
    )


def _write_client_jar(path: Path, block_names: list[str], payload: bytes = PNG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name in block_names:
            archive.writestr(f"assets/minecraft/textures/block/{name}.png", payload)
    return path


def test_client_jar_is_a_level_one_source(ws_tmp):
    """Mojang's index has no block textures; the installed jar is where a version's own PNG lives."""
    game = ws_tmp / "game"
    _write_json(
        game / "versions" / "1.20.1" / "1.20.1.json",
        {"id": "1.20.1", "releaseTime": "2023-06-12T12:00:00+00:00"},
    )
    jar = _write_client_jar(
        game / "versions" / "1.20.1" / "1.20.1.jar", ["grass_block_side"]
    )
    assert bt.jar_texture_bytes(
        "grass_block_side", game_dir=game, version_id="1.20.1"
    ) == PNG
    # a loader profile follows inheritsFrom to the vanilla jar
    _write_json(
        game / "versions" / "整合包" / "整合包.json",
        {"id": "整合包", "inheritsFrom": "1.20.1"},
    )
    assert bt.jar_texture_bytes(
        "grass_block_side", game_dir=game, version_id="整合包"
    ) == PNG
    # an unrelated block is not invented
    assert bt.jar_texture_bytes("gold_block", game_dir=game, version_id="1.20.1") is None
    assert jar.is_file()


def test_prefetch_prefers_local_jars_and_never_touches_the_network(ws_tmp):
    game = ws_tmp / "game"
    _write_json(
        game / "versions" / "1.21.1" / "1.21.1.json",
        {"id": "1.21.1", "releaseTime": "2024-08-08T12:00:00+00:00"},
    )
    _write_client_jar(
        game / "versions" / "1.21.1" / "1.21.1.jar", list(bt.TEXTURE_NAMES)
    )
    data = ws_tmp / "data"
    client = _ExplodingClient()
    assert bt.prefetch_missing_textures(data, game_dir=game, client=client) == 4
    assert client.urls == []  # a local jar is enough: no manifest, no index, no PNG request
    root = bt.texture_cache_root(data) / bt.MODERN_BUCKET
    for name in bt.TEXTURE_NAMES:
        assert (root / (name + ".png")).read_bytes() == PNG
    assert bt.missing_textures(data) == []


# ---------------------------------------------------------------------------
# 3./4. prefetch: failure is silent, cached files are never downloaded again
# ---------------------------------------------------------------------------


class _ExplodingClient:
    """Every request fails, like an offline machine (never touches the network)."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.closed = False

    def get(self, url: str):
        self.urls.append(url)
        raise httpx.ConnectError("offline", request=httpx.Request("GET", url))

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """Serves canned responses by URL and records every request."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.closed = False

    def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        return self.responses[url]

    def close(self) -> None:
        self.closed = True


def _response(url: str, *, json_body: dict | None = None, content: bytes | None = None):
    return httpx.Response(
        200,
        json=json_body,
        content=content,
        request=httpx.Request("GET", url),
    )


MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
VERSION_URL = "https://example.test/1.21.8.json"
INDEX_URL = "https://example.test/1.21.8-index.json"


def _prefetch_responses(*, digest: str, block_key: str, texture: bytes = PNG) -> dict:
    return {
        MANIFEST_URL: _response(
            MANIFEST_URL,
            json_body={
                "latest": {"release": "1.21.8", "snapshot": "26w02a"},
                "versions": [
                    {
                        "id": "1.21.8",
                        "type": "release",
                        "url": VERSION_URL,
                        "time": "",
                        "releaseTime": "",
                    }
                ],
            },
        ),
        VERSION_URL: _response(
            VERSION_URL,
            json_body={
                "id": "1.21.8",
                "type": "release",
                "mainClass": "net.minecraft.client.main.Main",
                "assetIndex": {"id": "1.21.8", "url": INDEX_URL},
            },
        ),
        INDEX_URL: _response(
            INDEX_URL,
            json_body={"objects": {block_key: {"hash": digest, "size": len(texture)}}},
        ),
        f"https://resources.download.minecraft.net/{digest[:2]}/{digest}": _response(
            f"https://resources.download.minecraft.net/{digest[:2]}/{digest}", content=texture
        ),
    }


def test_prefetch_failure_is_silent_and_writes_nothing(ws_tmp):
    client = _ExplodingClient()
    assert bt.prefetch_missing_textures(ws_tmp / "data", client=client) == 0
    assert client.urls  # it did try
    assert not (bt.texture_cache_root(ws_tmp / "data") / bt.MODERN_BUCKET).exists()


def test_complete_cache_is_not_downloaded_again(ws_tmp):
    root = bt.texture_cache_root(ws_tmp / "data") / bt.MODERN_BUCKET
    for name in bt.TEXTURE_NAMES:
        _write_png(root / (name + ".png"))
    client = _FakeClient({})
    assert bt.prefetch_missing_textures(ws_tmp / "data", client=client) == 0
    assert client.urls == []
    assert bt.missing_textures(ws_tmp / "data") == []
    assert bt.start_prefetch(ws_tmp / "data") is None


def test_prefetch_only_fetches_the_missing_texture(ws_tmp):
    root = bt.texture_cache_root(ws_tmp / "data") / bt.MODERN_BUCKET
    for name in bt.TEXTURE_NAMES:
        if name != "gold_block":
            _write_png(root / (name + ".png"), b"keep-me")
    missing = root / "gold_block.png"
    assert not missing.exists()

    digest = hashlib.sha1(PNG).hexdigest()
    client = _FakeClient(
        _prefetch_responses(
            digest=digest,
            block_key="minecraft/textures/block/gold_block.png",
        )
    )
    assert bt.prefetch_missing_textures(ws_tmp / "data", client=client) == 1
    assert missing.read_bytes() == PNG
    # exactly one PNG request, and it is the asset CDN object - never client.jar
    png_requests = [url for url in client.urls if url.startswith(bt.ASSET_BASE)]
    assert png_requests == [bt.ASSET_BASE + digest[:2] + "/" + digest]
    assert all("client.jar" not in url for url in client.urls)
    # the three existing files were not replaced
    for name in bt.TEXTURE_NAMES:
        if name != "gold_block":
            assert (root / (name + ".png")).read_bytes() == b"keep-me"


def test_a_non_png_response_is_rejected_without_leaving_a_file(ws_tmp):
    digest = hashlib.sha1(b"<html>not found</html>").hexdigest()
    client = _FakeClient(
        _prefetch_responses(
            digest=digest,
            block_key="minecraft/textures/block/gold_block.png",
            texture=b"<html>not found</html>",
        )
    )
    assert bt.prefetch_missing_textures(ws_tmp / "data", client=client) == 0
    root = bt.texture_cache_root(ws_tmp / "data") / bt.MODERN_BUCKET
    assert not (root / "gold_block.png").exists()
    assert not root.exists() or not list(root.glob("*.tmp"))


def test_start_prefetch_uses_a_daemon_thread(ws_tmp, monkeypatch):
    root = bt.texture_cache_root(ws_tmp / "data") / bt.MODERN_BUCKET
    for name in bt.TEXTURE_NAMES[:3]:
        _write_png(root / (name + ".png"))
    calls: list[Path] = []
    monkeypatch.setattr(
        bt, "prefetch_missing_textures", lambda launcher_dir, **_kw: calls.append(launcher_dir) or 0
    )
    thread = bt.start_prefetch(ws_tmp / "data")
    assert thread is not None and thread.daemon
    thread.join(timeout=5)
    assert calls == [ws_tmp / "data"]
