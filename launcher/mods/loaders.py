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

"""Loader version sources: Fabric / Forge / NeoForge (unified LoaderVersion abstraction)."""

from __future__ import annotations

import queue
import threading
import xml.etree.ElementTree as ET

import httpx

from launcher.i18n import describe_network_error, tr_core
from launcher.meta.manifest import _new_client
from launcher.mods.models import LoaderVersion

FABRIC_META = "https://meta.fabricmc.net/v2"
FORGE_PROMOTIONS_URL = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
)
FORGE_MAVEN_BASE = "https://maven.minecraftforge.net/net/minecraftforge/forge/"
NEOFORGE_METADATA_URL = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
)
#: BMCLAPI mirrors the NeoForge maven. ``maven.neoforged.net`` answers in 0.4s or not at all: from
#: mainland China the TCP connect simply times out (measured: 42s, twice), which is what made the
#: version page report 查询失败 while Forge and Fabric were fine.
NEOFORGE_MIRROR_METADATA_URL = (
    "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml"
)
NEOFORGE_MAVEN_BASE = "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
NEOFORGE_MIRROR_MAVEN_BASE = "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/"

LOADERS = ("fabric", "forge", "neoforge")

LOADER_DISPLAY_NAMES = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}


def display_loader_name(loader: str) -> str:
    """Human-readable loader name (API identifiers stay lowercase)."""
    return LOADER_DISPLAY_NAMES.get(loader, loader)


class ModsError(Exception):
    """Mod/loader-related error (user-facing message)."""


def _get_json(url: str, client: httpx.Client | None) -> list | dict:
    own = client is None
    if own:
        client = _new_client()
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise ModsError(
            tr_core("mods.request_failed", url, describe_network_error(exc))
        ) from exc
    finally:
        if own:
            client.close()


def _get_text_from_any(sources: list[tuple[str, str]], client: httpx.Client | None) -> tuple[str, str]:
    """Ask every source at once and return the first that answers, as ``(text, base)``.

    ``sources`` are ``(metadata_url, maven_base)`` pairs: the caller learns which host answered, and
    the installer jar then comes from that same host - a list from the mirror must not point the jar
    at the host that is unreachable.

    Racing instead of trying them in turn: ``maven.neoforged.net`` does not answer from mainland
    China, so a sequential attempt costs its whole timeout before the mirror is even asked. Here the
    mirror's 0.4s answer wins the race and the official request is abandoned. Six seconds caps each
    request so a stalled host cannot hold the result.
    """
    if len(sources) == 1:
        url, base = sources[0]
        return _get_text(url, client, timeout=6.0), base

    results: queue.Queue[tuple[str, str] | Exception] = queue.Queue()
    for url, base in sources:
        def _worker(url: str = url, base: str = base) -> None:
            try:
                results.put((_get_text(url, client, timeout=6.0), base))
            except Exception as exc:  # noqa: BLE001 - handed to the caller below
                results.put(exc)

        # Daemon threads: a host that never answers must not keep the process alive at exit, and the
        # losing request is simply dropped once a winner is in.
        threading.Thread(target=_worker, name="loader-metadata", daemon=True).start()

    last: Exception | None = None
    for _ in range(len(sources)):
        item = results.get()
        if isinstance(item, Exception):
            last = item
            continue
        return item
    assert last is not None
    raise last


def _get_text(url: str, client: httpx.Client | None, *, timeout: float | None = None) -> str:
    own = client is None
    if own:
        client = _new_client()
    try:
        resp = client.get(url, timeout=timeout) if timeout is not None else client.get(url)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError as exc:
        raise ModsError(
            tr_core("mods.request_failed", url, describe_network_error(exc))
        ) from exc
    finally:
        if own:
            client.close()


def _fabric_versions(
    game_version: str, client: httpx.Client | None
) -> list[LoaderVersion]:
    # Per game version, not the global list: /versions/loader answers with the builds that actually
    # work with this MC version (255 of them for 1.21.1), newest first.
    loaders = _get_json(f"{FABRIC_META}/versions/loader/{game_version}", client)
    installers = _get_json(FABRIC_META + "/versions/installer", client)
    installer_url = installers[0]["url"] if installers else ""
    out: list[LoaderVersion] = []
    for entry in loaders:
        # The per-game payload nests the build under "loader"; the global one does not. Accept both.
        node = entry.get("loader") if isinstance(entry.get("loader"), dict) else entry
        version = node.get("version")
        if not version:
            continue
        # No `stable` filter here. On this endpoint the flag means "the current recommendation", not
        # "installable": Fabric marks the newest build true and every older one false, so filtering on it
        # left the picker with exactly one build - the whole reason the Fabric row offered no list.
        out.append(
            LoaderVersion(
                loader="fabric",
                version=version,
                game_version=game_version,
                stable=bool(node.get("stable", True)),
                installer_url=installer_url,
            )
        )
    return out


def _forge_versions(
    game_version: str, client: httpx.Client | None
) -> list[LoaderVersion]:
    data = _get_json(FORGE_PROMOTIONS_URL, client)
    promos = data.get("promos", {})
    out: list[LoaderVersion] = []
    for suffix, recommended in (("-recommended", True), ("-latest", False)):
        key = game_version + suffix
        version = promos.get(key)
        if not version:
            continue
        out.append(
            LoaderVersion(
                loader="forge",
                version=version,
                game_version=game_version,
                stable=True,
                recommended=recommended,
                installer_url=(
                    FORGE_MAVEN_BASE + game_version + "-" + version + "/forge-"
                    + game_version + "-" + version + "-installer.jar"
                ),
            )
        )
    return out


def _version_key(value: str) -> tuple:
    """Sort key that prefers numeric segments and falls back to non-numeric ones (e.g. 20.2.12-beta)."""
    parts: list[tuple[int, object]] = []
    for part in value.split("."):
        try:
            parts.append((0, int(part)))
        except ValueError:
            parts.append((1, part))
    return tuple(parts)


def _neoforge_base(game_version: str) -> str:
    """MC version -> NeoForge version prefix (1.20.2 -> 20.2; 26.1.2 -> 26.1.2)."""
    if game_version.startswith("1."):
        return game_version[2:]
    return game_version


def _neoforge_versions(
    game_version: str, client: httpx.Client | None
) -> list[LoaderVersion]:
    xml_text, maven_base = _get_text_from_any(
        [
            # Mirror first, for the reason spelled out in ``list_game_versions`` below.
            (NEOFORGE_MIRROR_METADATA_URL, NEOFORGE_MIRROR_MAVEN_BASE),
            (NEOFORGE_METADATA_URL, NEOFORGE_MAVEN_BASE),
        ],
        client,
    )
    root = ET.fromstring(xml_text)
    base = _neoforge_base(game_version)
    versions = [
        node.text
        for node in root.findall(".//version")
        if node.text
        and node.text.startswith(base + ".")
        and "-" not in node.text  # skip pre-release builds like beta/rc
    ]
    versions.sort(key=_version_key)
    out: list[LoaderVersion] = []
    for version in reversed(versions[-20:]):  # newest first
        out.append(
            LoaderVersion(
                loader="neoforge",
                version=version,
                game_version=game_version,
                stable=True,
                installer_url=(
                    maven_base + version + "/neoforge-" + version + "-installer.jar"
                ),
            )
        )
    return out


def list_loader_versions(
    loader: str, game_version: str, *, client: httpx.Client | None = None
) -> list[LoaderVersion]:
    """List the available versions of a loader for a given MC version (newest to oldest)."""
    if loader == "fabric":
        return _fabric_versions(game_version, client)  # API already returns newest-first
    if loader == "forge":
        return list(reversed(_forge_versions(game_version, client)))  # latest first
    if loader == "neoforge":
        return _neoforge_versions(game_version, client)  # already newest-first
    raise ModsError(tr_core("mods.unknown_loader", loader))


def list_game_versions(loader: str, *, client: httpx.Client | None = None) -> list[str]:
    """List the MC versions supported by a loader."""
    if loader == "fabric":
        data = _get_json(FABRIC_META + "/versions/game", client)
        return [entry["version"] for entry in data if entry.get("stable", True)]
    if loader == "forge":
        data = _get_json(FORGE_PROMOTIONS_URL, client)
        promos = data.get("promos", {})
        versions = sorted({key.rsplit("-", 1)[0] for key in promos})
        return list(reversed(versions))
    if loader == "neoforge":
        xml_text, _maven_base = _get_text_from_any(
            [
                # Mirror first: maven.neoforged.net is unreachable from mainland China (TCP connect
                # times out), and paying that timeout before every version list is 6s the user spends
                # staring at 查询中. The official host stays as the fallback for everywhere else.
                (NEOFORGE_MIRROR_METADATA_URL, NEOFORGE_MIRROR_MAVEN_BASE),
                (NEOFORGE_METADATA_URL, NEOFORGE_MAVEN_BASE),
            ],
            client,
        )
        root = ET.fromstring(xml_text)
        bases: set[str] = set()
        for node in root.findall(".//version"):
            if not node.text:
                continue
            parts = node.text.split(".")
            if len(parts) == 3:
                # 20.2.88 -> MC 1.20.2
                bases.add("1." + ".".join(parts[:2]))
            elif len(parts) >= 4:
                # 26.1.2.95 -> MC 26.1.2
                bases.add(".".join(parts[:3]))
        return sorted(bases, reverse=True)
    raise ModsError(tr_core("mods.unknown_loader", loader))
