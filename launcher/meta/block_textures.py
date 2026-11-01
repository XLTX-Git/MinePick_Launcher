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

"""Minecraft block textures for version icons: category mapping, the lookup, prefetch.

The launcher never redistributes Mojang's artwork. The pages ask this module for a PNG path and paint
their own block when it returns ``None``; when a real texture is wanted, the lookup order is fixed:

1. the version's own files - ``assets`` indexes plus ``objects/<hash>``, the loose pre-index layout,
   and (because Mojang's asset index does not list block textures at all) the version's own
   ``client.jar``, which is where the exact texture for that version lives. Old versions therefore
   show their own old texture;
2. the user's cache at ``<launcher data dir>/cache/block-textures/<version or "modern">/*.png``. The
   GUI copies a jar texture here on its first read, so the expensive archive is opened once;
3. the network - mcmod.cn's icon CDN. ``start_prefetch`` asks it only for a block that levels 1 and 2
   could not supply (a plain texture, not a game file: a fresh install with no Minecraft at all).
   The gold icon is a WebP and is converted to PNG before it is stored, so the cache stays PNG and
   the same ``modern/<block>.png`` path is used. Nothing is downloaded for a texture that a local
   level already has;
4. nothing (the caller draws the accent block, so the UI never breaks).

The cache is filled after the window is already on screen by ``start_prefetch``: a daemon thread that
first copies the five missing textures out of any installed client jar (newest release first), then
fetches what is still missing from mcmod.cn, and only then falls back to the documented Mojang chain
- manifest -> a modern version's asset index -> ``resources.download.minecraft.net``. It never
downloads ``client.jar`` and swallows every failure: the next start simply tries again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import zipfile
from pathlib import Path

import httpx

from launcher.meta.assets import fetch_asset_index
from launcher.meta.manifest import (
    APRIL_FOOLS_IDS,
    MetaError,
    VersionManifest,
    _new_client,
    fetch_manifest,
    version_category,
)
from launcher.meta.version import load_version_json

LOGGER = logging.getLogger(__name__)

#: Category -> the block that stands for it on the instances and versions pages.
BLOCK_BY_CATEGORY: dict[str, str] = {
    "release": "grass_block_side",
    "snapshot": "command_block_front",
    "april_fools": "gold_block",
    "ancient": "cobblestone",
}

#: The only textures the startup prefetch fetches (order is also the fetch order). Grass needs two:
#: its side (dirt with a grass fringe) and its top (the green surface seen from above).
TEXTURE_NAMES: tuple[str, ...] = (
    "grass_block_side",
    "grass_block_top",
    "command_block_front",
    "gold_block",
    "cobblestone",
)

#: File stems per block: the modern name first, the pre-1.13 name as a fallback alias.
_TEXTURE_STEMS: dict[str, tuple[str, ...]] = {
    "grass_block_side": ("grass_block_side", "grass_side"),
    "grass_block_top": ("grass_block_top", "grass_top"),
    "command_block_front": ("command_block_front", "command_block"),
    "gold_block": ("gold_block",),
    "cobblestone": ("cobblestone",),
}

#: Pre-1.13 assets use ``textures/blocks`` (plural); 1.13+ uses ``textures/block``.
_TEXTURE_DIRS: tuple[str, ...] = ("minecraft/textures/block", "minecraft/textures/blocks")

CACHE_DIRNAME = "block-textures"
MODERN_BUCKET = "modern"
ASSET_BASE = "https://resources.download.minecraft.net/"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_CHAIN = 8

#: The four category blocks mcmod.cn's icon CDN serves, for a machine with no block texture at all.
#: The icons are modern (1.13+) art, not the pre-1.13 images, and the CDN answers a plain browser
#: request instead of the wiki's WAF challenge. No file here is shipped: the bytes go to the user's
#: cache at runtime only, and the gold one is a WebP that is converted before it is stored.
NETWORK_SOURCES: dict[str, str] = {
    "grass_block_side": "https://i.mcmod.cn/item/icon/128x128/0/10.png?v=19",
    "cobblestone": "https://i.mcmod.cn/item/icon/128x128/0/11.png?v=14",
    "command_block_front": "https://i.mcmod.cn/item/icon/128x128/1/11059.png?v=6",
    "gold_block": "https://i.mcmod.cn/editor/upload/20250101/1735694683_899182_dnZO.webp",
}

#: A browser User-Agent is mandatory: the CDN refuses a bare client, and a normal browser string is
#: enough. One icon may not hold the prefetch thread longer than :data:`NETWORK_TIMEOUT`.
NETWORK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
NETWORK_TIMEOUT = 10.0
WEBP_SIGNATURE = b"WEBP"
_PNG_IEND = b"IEND\xaeB`\x82"

_SNAPSHOT_RE = re.compile(r"^\d{2}w\d{2}", re.IGNORECASE)
_OLD_ALPHA_RE = re.compile(r"^(?:a\d|c\d+\.|inf-|rd-)", re.IGNORECASE)
_OLD_BETA_RE = re.compile(r"^b\d", re.IGNORECASE)
_ANCIENT_RELEASE_RE = re.compile(r"^1\.(\d+)(?:\.|$)")
_ANCIENT_ZERO_RE = re.compile(r"^0\.")

# Parsed asset indexes / manifest type tables, keyed by (path, mtime_ns, size). The versions page
# asks for an icon per row; without this the same half-megabyte index would be parsed once per row.
_JSON_CACHE: dict[tuple[str, int, int], dict] = {}


# ---------------------------------------------------------------------------
# Category mapping (reuses launcher.meta.manifest.version_category)
# ---------------------------------------------------------------------------


def is_ancient_release(version_id: str) -> bool:
    """1.0 - 1.6 (and the pre-release 0.x line) count as the "ancient" category.

    The official manifest still types those ids as ``release``, so the category function cannot
    separate them; they are the versions whose assets are the oldest layout the launcher may meet.
    """
    match = _ANCIENT_RELEASE_RE.match(version_id)
    if match is not None:
        return int(match.group(1)) <= 6
    return _ANCIENT_ZERO_RE.match(version_id) is not None


def icon_category(version_id: str, version_type: str) -> str:
    """Icon category: ``release`` / ``snapshot`` / ``april_fools`` / ``ancient``.

    ``version_category`` already knows the April Fools ids and the alpha/beta types; this adds only
    the early-release case (1.0 - 1.6), which the manifest still types as ``release``.
    """
    category = version_category(version_id, version_type or guess_version_type(version_id))
    if category in ("old_beta", "old_alpha"):
        return "ancient"
    if category == "release" and is_ancient_release(version_id):
        return "ancient"
    return category


def block_name_for_category(category: str) -> str:
    """The texture stem for an icon category (unknown categories read as a normal release)."""
    return BLOCK_BY_CATEGORY.get(category, BLOCK_BY_CATEGORY["release"])


def guess_version_type(version_id: str) -> str:
    """Best-effort manifest type from the id shape, for profiles with no stored type."""
    if version_id in APRIL_FOOLS_IDS:
        return "snapshot"
    if _OLD_ALPHA_RE.match(version_id):
        return "old_alpha"
    if _OLD_BETA_RE.match(version_id):
        return "old_beta"
    if _SNAPSHOT_RE.match(version_id):
        return "snapshot"
    return "release"


def loader_kind(version_id: str, *, game_dir: Path | None = None) -> str | None:
    """Loader behind a profile id (``fabric`` / ``forge`` / ``neoforge`` / ``quilt``), or ``None``.

    With ``game_dir`` the ``inheritsFrom`` chain is followed, so a profile that was renamed to a
    display name still resolves to the loader it inherits from.
    """
    candidates = [version_id]
    if game_dir is not None:
        for raw, folder in _profile_chain(game_dir, version_id):
            candidates.append(str(raw.get("id") or folder))
            parent = raw.get("inheritsFrom")
            if isinstance(parent, str) and parent.strip():
                candidates.append(parent.strip())
    for candidate in candidates:
        lowered = candidate.strip().lower()
        if lowered.startswith("fabric-loader-"):
            return "fabric"
        if lowered.startswith("quilt-loader-"):
            return "quilt"
        if lowered.startswith(("neoforge-", "neo-forge-")):
            return "neoforge"
        if "-forge-" in lowered or lowered.startswith("forge-"):
            return "forge"
    return None


def loader_kind_for_label(label: str) -> str | None:
    """Loader kind from the ``版本 · 加载器`` card label (``"Fabric 0.19.3"`` -> ``"fabric"``).

    ``instances_page.card_version_loader`` already resolves the inheritance chain and returns the
    proper noun first; this keeps the label-to-glyph decision in one place instead of a second walk.
    """
    first = (label or "").strip().split(" ", 1)[0].lower()
    return first if first in ("fabric", "forge", "neoforge", "quilt") else None


def version_type_for(
    version_id: str,
    game_dir: Path | None = None,
    *,
    cache_dir: Path | None = None,
) -> str:
    """Manifest type of a version, offline: profile JSON, then the cached manifest, then the id."""
    if game_dir is not None:
        for raw, _folder in _profile_chain(game_dir, version_id):
            value = raw.get("type")
            if isinstance(value, str) and value.strip():
                return value.strip()
    cached = _type_from_manifest_cache(cache_dir, version_id)
    if cached is not None:
        return cached
    return guess_version_type(version_id)


# ---------------------------------------------------------------------------
# Lookup: the version's assets, the user cache, the client jar
# ---------------------------------------------------------------------------


def texture_cache_root(launcher_dir: Path) -> Path:
    """``<launcher data dir>/cache/block-textures`` (the only place fetched textures are written)."""
    return Path(launcher_dir) / "cache" / CACHE_DIRNAME


def missing_textures(launcher_dir: Path) -> list[str]:
    """The texture names that are not in the user's cache yet (empty when everything is cached)."""
    root = texture_cache_root(launcher_dir) / MODERN_BUCKET
    return [name for name in TEXTURE_NAMES if not _is_texture(root / (name + ".png"))]


def find_block_texture(
    block_name: str,
    *,
    game_dir: Path | None = None,
    version_id: str | None = None,
    cache_root: Path | None = None,
) -> Path | None:
    """Path from own assets, then the user cache, else ``None``.

    This covers the two on-disk levels. A texture that only exists inside the version's own
    ``client.jar`` has no path; callers that render pixels use :func:`jar_texture_bytes` between the
    two levels (the GUI does). Every lookup here is read-only and never raises for a missing file.
    """
    found = find_asset_texture(block_name, game_dir=game_dir, version_id=version_id)
    if found is not None:
        return found
    return find_cached_texture(block_name, cache_root=cache_root, version_id=version_id)


def find_asset_texture(
    block_name: str,
    *,
    game_dir: Path | None = None,
    version_id: str | None = None,
) -> Path | None:
    """Level 1: the version's own ``assets`` (index + objects, or the loose pre-index layout)."""
    if game_dir is None:
        return None
    return _texture_from_assets(Path(game_dir), block_name, version_id)


def find_cached_texture(
    block_name: str,
    *,
    cache_root: Path | None = None,
    version_id: str | None = None,
) -> Path | None:
    """Level 2: ``<version or modern>/<block>.png`` under the user's texture cache."""
    if cache_root is None:
        return None
    for bucket in _cache_buckets(version_id):
        candidate = Path(cache_root) / bucket / (block_name + ".png")
        if _is_texture(candidate):
            return candidate
    return None


def store_texture_bytes(block_name: str, data: bytes, cache_root: Path | None) -> Path | None:
    """Write a texture that was just read from a local source into the user cache.

    The GUI reads a version's PNG out of its ``client.jar`` (Mojang's asset index does not list
    block textures), which opens and scans a 25 MB archive for a few KB of pixels. Once those
    bytes are copied here, the next lookup finds ``<cache>/block-textures/modern/<block>.png`` and
    never touches the jar again. The write is a temporary file plus ``replace``, so a crash cannot
    leave a half-written PNG behind, and an already-cached file is left alone (it may be newer).

    Returns the cache path when the texture is in the cache afterwards, ``None`` when the input
    was not a PNG or the write failed - a cache that cannot be written must never break the icon.
    """
    if cache_root is None or not block_name:
        return None
    if not data or not data.startswith(PNG_SIGNATURE):
        return None
    if any(part in block_name for part in ("/", "\\", "..")):
        return None
    destination = Path(cache_root) / MODERN_BUCKET / (block_name + ".png")
    if _is_texture(destination):
        return destination
    try:
        _store_texture(destination, data)
    except (OSError, ValueError):
        return None
    return destination if _is_texture(destination) else None


def jar_texture_bytes(
    block_name: str,
    *,
    game_dir: Path | None = None,
    version_id: str | None = None,
) -> bytes | None:
    """Level 1b: the block PNG inside the version's own ``client.jar``.

    Mojang's asset index does not list block textures - they ship inside the client jar - so this is
    the only place a version's exact texture exists on disk. It reads a few KB from a file the user
    already owns; nothing is downloaded. Loader profiles are followed to their parent folder.
    """
    if game_dir is None or not version_id:
        return None
    game = Path(game_dir)
    folders = [version_id]
    for _raw, folder in _profile_chain(game, version_id):
        if folder not in folders:
            folders.append(folder)
    for folder in folders:
        jar = game / "versions" / folder / (folder + ".jar")
        if not jar.is_file():
            continue
        data = _texture_from_jar(jar, block_name)
        if data is not None:
            return data
    return None


def _texture_from_jar(jar: Path, block_name: str) -> bytes | None:
    try:
        with zipfile.ZipFile(jar) as archive:
            return _texture_from_zip(archive, block_name)
    except (OSError, zipfile.BadZipFile):
        return None


def _texture_from_zip(archive: zipfile.ZipFile, block_name: str) -> bytes | None:
    for candidate in _texture_keys(block_name):
        try:
            info = archive.getinfo("assets/" + candidate)
        except KeyError:
            continue
        if info.file_size > 1_000_000:  # a 16x16 PNG is a few KB; skip a bizarre entry
            continue
        try:
            data = archive.read(info)
        except (OSError, zipfile.BadZipFile, RuntimeError):
            continue
        if data.startswith(PNG_SIGNATURE):
            return data
    return None


def _cache_buckets(version_id: str | None) -> tuple[str, ...]:
    """``(<version>, "modern")`` - the version bucket is optional and must stay a plain folder name."""
    if version_id and not any(part in version_id for part in ("/", "\\", "..")):
        return (version_id, MODERN_BUCKET)
    return (MODERN_BUCKET,)


def _texture_from_assets(game_dir: Path, block_name: str, version_id: str | None) -> Path | None:
    assets = game_dir / "assets"
    # Loose pre-index layout (and the copy users sometimes keep next to a modern install).
    for candidate in _texture_keys(block_name):
        loose = assets / candidate
        if _is_texture(loose):
            return loose
    for index in _asset_indexes(assets, version_id):
        objects = index.get("objects")
        if not isinstance(objects, dict):
            continue
        for candidate in _texture_keys(block_name):
            entry = objects.get(candidate)
            if not isinstance(entry, dict):
                continue
            digest = entry.get("hash")
            if not isinstance(digest, str) or len(digest) < 2:
                continue
            path = assets / "objects" / digest[:2] / digest
            if _is_texture(path):
                return path
    return None


def _texture_keys(block_name: str) -> tuple[str, ...]:
    stems = _TEXTURE_STEMS.get(block_name, (block_name,))
    return tuple(f"{directory}/{stem}.png" for stem in stems for directory in _TEXTURE_DIRS)


def _asset_indexes(assets: Path, version_id: str | None) -> list[dict]:
    """Parsed indexes for this version, most specific first (the declared index, then its id)."""
    ids: list[str] = []
    for raw, _folder in _profile_chain_from_assets(assets, version_id):
        info = raw.get("assetIndex")
        if isinstance(info, dict):
            value = info.get("id")
            if isinstance(value, str) and value.strip():
                ids.append(value.strip())
                break
    if version_id and version_id not in ids:
        ids.append(version_id)
    indexes: list[dict] = []
    for index_id in ids:
        if any(part in index_id for part in ("/", "\\", "..")):
            continue
        raw = _read_json(assets / "indexes" / (index_id + ".json"))
        if raw:
            indexes.append(raw)
    return indexes


def _profile_chain_from_assets(assets: Path, version_id: str | None):
    """The profile chain is under ``<game>/versions``; ``assets``' parent is the game directory."""
    if not version_id:
        return
    yield from _profile_chain(assets.parent, version_id)


def _profile_chain(game_dir: Path, version_id: str):
    """Yields ``(raw profile, folder id)`` from the version outwards through ``inheritsFrom``."""
    seen: set[str] = set()
    current = version_id
    while current and current not in seen and len(seen) < _MAX_CHAIN:
        seen.add(current)
        raw = _read_json(Path(game_dir) / "versions" / current / (current + ".json"))
        if not raw:
            return
        yield raw, current
        parent = raw.get("inheritsFrom")
        current = parent.strip() if isinstance(parent, str) and parent.strip() else ""


def _type_from_manifest_cache(cache_dir: Path | None, version_id: str) -> str | None:
    if cache_dir is None:
        return None
    raw = _read_json(Path(cache_dir) / "version_manifest.json")
    if not raw:
        return None
    payload = raw.get("manifest") if isinstance(raw.get("manifest"), dict) else raw
    versions = payload.get("versions")
    if not isinstance(versions, list):
        return None
    for entry in versions:
        if isinstance(entry, dict) and entry.get("id") == version_id:
            value = entry.get("type")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _read_json(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _JSON_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    if len(_JSON_CACHE) >= 16:
        _JSON_CACHE.clear()
    _JSON_CACHE[key] = raw
    return raw


def _is_texture(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Network level: mcmod.cn's icon CDN
# ---------------------------------------------------------------------------


def network_texture_bytes(
    block_name: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = NETWORK_TIMEOUT,
) -> bytes | None:
    """One block icon from mcmod.cn as PNG bytes, or ``None`` when it cannot be had.

    Only the four names in :data:`NETWORK_SOURCES` exist here. The request carries a browser
    User-Agent (the CDN refuses a bare client) and is bounded by ``timeout``; the gold icon arrives
    as WebP and is decoded and re-encoded as PNG. Every failure - DNS, timeout, 404, a truncated
    body, an HTML error page - returns ``None`` and raises nothing, so the caller keeps its drawn
    block exactly like an offline start. Nothing is written here; the caller stores the bytes.
    """
    url = NETWORK_SOURCES.get(block_name)
    if not url:
        return None
    own_client = client is None
    if own_client:
        client = _new_client()
    try:
        response = client.get(
            url,
            headers={"User-Agent": NETWORK_USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return _image_bytes_as_png(response.content)
    except (httpx.HTTPError, OSError, ValueError):
        return None
    finally:
        if own_client:
            client.close()


def _image_bytes_as_png(data: bytes) -> bytes | None:
    """PNG passed through, the WebP icon decoded through Qt, anything else (HTML) refused."""
    if data.startswith(PNG_SIGNATURE):
        # A cut-off download still starts with the signature, while a complete PNG ends with the
        # IEND chunk; the check keeps a half-received icon from poisoning the cache.
        return data if data.endswith(_PNG_IEND) else None
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == WEBP_SIGNATURE:
        return _webp_as_png(data)
    return None


def _webp_as_png(data: bytes) -> bytes | None:
    """Decode the one WebP icon with Qt's image plugin and re-encode it as PNG.

    QImage is imported here, not at module scope: PySide6 is the optional GUI extra, and the
    launcher core must stay importable without it. A CLI-only install therefore skips this one icon
    and keeps the three PNG sources; the cache never has to know which format the CDN served.
    """
    try:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage
    except ImportError:
        return None
    image = QImage.fromData(data)
    if image.isNull():
        return None
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        return None
    png = bytes(buffer.data())
    return png if png.startswith(PNG_SIGNATURE) else None


# ---------------------------------------------------------------------------
# Background prefetch at startup
# ---------------------------------------------------------------------------


def start_prefetch(launcher_dir: Path, game_dir: Path | None = None) -> threading.Thread | None:
    """Fill the texture cache in a daemon thread; returns ``None`` when nothing is missing.

    Called once after the main window is shown. The cache hit check is five ``stat`` calls, so the
    every-day start does no thread and no network at all.
    """
    if not missing_textures(launcher_dir):
        return None
    thread = threading.Thread(
        target=prefetch_missing_textures,
        args=(Path(launcher_dir),),
        kwargs={"game_dir": Path(game_dir) if game_dir is not None else None},
        name="block-texture-prefetch",
        daemon=True,
    )
    thread.start()
    return thread


def prefetch_missing_textures(
    launcher_dir: Path,
    *,
    game_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> int:
    """Fill the missing block textures; returns how many were written. Never raises.

    The user's own installed client jars are read first: Mojang's asset index does not list block
    textures (they ship inside the jar), so this is where an exact per-version texture actually
    lives, and it costs a few KB of local IO instead of a 25 MB download. Only the names no local
    jar had are then fetched from mcmod.cn's CDN (all four category icons), and only the ones still
    missing after that fall through to the documented Mojang chain: manifest -> latest release ->
    version JSON -> asset index -> object hash -> ``resources.download.minecraft.net``. Any failure
    leaves the cache as it was and is logged at debug level, so the next start retries.
    """
    try:
        return _prefetch_missing(Path(launcher_dir), Path(game_dir) if game_dir else None, client)
    except Exception:  # a background icon fetch must never disturb the launcher
        LOGGER.debug("block texture prefetch failed; keeping the drawn fallback", exc_info=True)
        return 0


def _prefetch_missing(
    launcher_dir: Path, game_dir: Path | None, client: httpx.Client | None
) -> int:
    root = texture_cache_root(launcher_dir)
    missing = [name for name in TEXTURE_NAMES if not _is_texture(root / MODERN_BUCKET / (name + ".png"))]
    if not missing:
        return 0

    written = 0
    if game_dir is not None:
        missing, from_jars = _fill_from_local_jars(root, missing, game_dir)
        written += from_jars
        if from_jars:
            LOGGER.info("cached %d block texture(s) from the installed client jar(s)", from_jars)
    if not missing:
        return written

    # Network level, before the documented Mojang chain: Mojang's asset index does not list block
    # textures at all, while mcmod.cn serves all four category icons. Only a name that is still
    # missing after the cache, every local jar and the game's own loose assets is asked for, so a
    # machine with game files never reaches this point for a texture it already has.
    missing, from_network = _fill_from_network(root, missing, game_dir, client)
    written += from_network
    if from_network:
        LOGGER.info("cached %d block texture(s) from mcmod.cn", from_network)
    if not missing:
        return written

    cache_dir = launcher_dir / "cache"
    own_client = client is None
    if own_client:
        client = _new_client()
    try:
        try:
            manifest = fetch_manifest(
                cache_path=cache_dir / "version_manifest.json", client=client
            )
        except (MetaError, httpx.HTTPError, OSError, ValueError):
            return written
        version_id = _modern_release(manifest)
        if not version_id:
            return written
        try:
            version = load_version_json(
                version_id, cache_dir=cache_dir, manifest=manifest, client=client
            )
            index = fetch_asset_index(
                version.asset_index,
                cache_path=cache_dir / "assets" / (version.asset_index.id + ".json"),
                client=client,
            )
        except (MetaError, httpx.HTTPError, OSError, ValueError):
            return written

        for name in missing:
            destination = root / MODERN_BUCKET / (name + ".png")
            if _is_texture(destination):
                continue
            digest = _hash_for(index, name)
            if not digest:
                continue
            try:
                _download_texture(client, digest, destination)
            except (httpx.HTTPError, OSError, ValueError):
                continue
            written += 1
        if written:
            LOGGER.info("cached %d block texture(s) for version icons", written)
        return written
    finally:
        if own_client:
            client.close()


def _fill_from_network(
    root: Path, missing: list[str], game_dir: Path | None, client: httpx.Client | None
) -> tuple[list[str], int]:
    """Store the mcmod.cn icons for the names no local level had; returns (still missing, written).

    Only names in :data:`NETWORK_SOURCES` are requested: ``grass_block_top`` has no icon there, so a
    fresh install keeps the drawn top face (the cube uses the side texture for it) instead of asking
    for an image that does not exist. A texture already sitting loose in the game's own ``assets``
    folder is local too: it is dropped from ``missing`` (so the Mojang chain is not tried for it
    either) and never downloaded. Each success reuses :func:`store_texture_bytes`, so the file lands
    in ``modern/<block>.png`` through the same atomic temp-file-plus-replace path.
    """
    local = [name for name in missing if _has_loose_asset(game_dir, name)]
    remaining = [name for name in missing if name not in local]
    targets = [name for name in remaining if name in NETWORK_SOURCES]
    if not targets:
        return remaining, 0
    own_client = client is None
    if own_client:
        client = _new_client()
    written = 0
    try:
        for name in targets:
            data = network_texture_bytes(name, client=client)
            if data is None:
                continue
            if store_texture_bytes(name, data, root) is None:
                continue
            remaining.remove(name)
            written += 1
        return remaining, written
    finally:
        if own_client:
            client.close()


def _has_loose_asset(game_dir: Path | None, block_name: str) -> bool:
    """Is the texture already in the game's own ``assets`` folder (the loose, pre-index layout)?"""
    if game_dir is None:
        return False
    assets = Path(game_dir) / "assets"
    return any(_is_texture(assets / key) for key in _texture_keys(block_name))


def _fill_from_local_jars(root: Path, missing: list[str], game_dir: Path) -> tuple[list[str], int]:
    """Copy the missing textures out of whichever installed client jar has them (newest first)."""
    remaining = list(missing)
    written = 0
    for jar in _local_jars(game_dir):
        if not remaining:
            break
        try:
            with zipfile.ZipFile(jar) as archive:
                for name in list(remaining):
                    data = _texture_from_zip(archive, name)
                    if data is None:
                        continue
                    try:
                        _store_texture(root / MODERN_BUCKET / (name + ".png"), data)
                    except (OSError, ValueError):
                        continue
                    remaining.remove(name)
                    written += 1
        except (OSError, zipfile.BadZipFile):
            continue
    return remaining, written


def _local_jars(game_dir: Path) -> list[Path]:
    """Installed client jars, newest release first (loader profiles usually have no jar)."""
    versions_dir = Path(game_dir) / "versions"
    try:
        folders = list(versions_dir.iterdir())
    except OSError:
        return []
    entries: list[tuple[str, float, Path]] = []
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
            jar = folder / (folder.name + ".jar")
            if not jar.is_file():
                continue
            raw = _read_json(folder / (folder.name + ".json"))
            release = raw.get("releaseTime")
            entries.append(
                (release if isinstance(release, str) else "", jar.stat().st_mtime, jar)
            )
        except OSError:
            continue
    entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [entry[2] for entry in entries]


def _modern_release(manifest: VersionManifest) -> str | None:
    latest = (manifest.latest or {}).get("release")
    if isinstance(latest, str) and latest.strip():
        return latest.strip()
    for version in manifest.versions:
        if version.type == "release":
            return version.id
    return None


def _hash_for(index, block_name: str) -> str | None:
    objects = getattr(index, "objects", None) or {}
    for candidate in _texture_keys(block_name):
        obj = objects.get(candidate)
        digest = getattr(obj, "hash", None) if obj is not None else None
        if isinstance(digest, str) and len(digest) >= 2 and not any(
            part in digest for part in ("/", "\\", "..")
        ):
            return digest
    return None


def _download_texture(client: httpx.Client, digest: str, destination: Path) -> None:
    """Fetch one object, verify it, and move it into the cache atomically."""
    response = client.get(ASSET_BASE + digest[:2] + "/" + digest)
    response.raise_for_status()
    data = response.content
    if hashlib.sha1(data).hexdigest().lower() != digest.lower():
        raise ValueError("downloaded block texture does not match its object hash")
    _store_texture(destination, data)


def _store_texture(destination: Path, data: bytes) -> None:
    """Write a verified PNG into the cache via a temporary file + replace."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("block texture is not a PNG")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
