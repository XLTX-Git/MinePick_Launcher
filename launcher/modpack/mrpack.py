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
"""Modrinth ``.mrpack``: ``modrinth.index.json`` plus ``overrides/``.

The index is the whole pack: ``dependencies`` names the Minecraft version and the loader build,
``files[]`` carries the direct ``downloads`` URL and ``hashes.sha1`` of every file, and the
``overrides/`` tree is copied over the instance afterwards. ``env.client`` (or the older boolean
``env.client``) marks a file the client does not need, and such a file is skipped.
"""

from __future__ import annotations

from pathlib import Path

from launcher.i18n import tr_core
from launcher.modpack.archive import open_zip, read_json, validate_member
from launcher.modpack.errors import ModpackError
from launcher.modpack.model import FORMAT_MODRINTH, ModpackSpec, PackFile

INDEX_NAME = "modrinth.index.json"
#: ``overrides/`` is the cross-loader tree; ``client-overrides/`` is the client-only one. Both are
#: copied: this launcher only runs clients.
OVERRIDE_PREFIXES = ("overrides/", "client-overrides/")
#: Dependency key -> installer name. The importer accepts fabric / forge / neoforge; quilt is parsed
#: so that a Quilt pack fails with a sentence naming the loader instead of "no Minecraft version".
LOADER_KEYS = {
    "fabric-loader": "fabric",
    "forge": "forge",
    "neoforge": "neoforge",
    "quilt-loader": "quilt",
}


def _client_excluded(env) -> bool:
    """Whether the pack's ``env`` block says the client does not need this file."""
    if not isinstance(env, dict):
        return False
    value = env.get("client")
    return value is False or str(value).strip().casefold() == "unsupported"


def _loader(dependencies) -> tuple[str, str]:
    """(loader, loader version) from the index's ``dependencies`` (empty strings for a vanilla pack)."""
    if not isinstance(dependencies, dict):
        return "", ""
    for key, value in dependencies.items():
        loader = LOADER_KEYS.get(str(key))
        if loader:
            return loader, str(value or "").strip()
    return "", ""


def parse_mrpack(path: Path) -> ModpackSpec:
    """Parse a ``.mrpack`` archive into a :class:`ModpackSpec`."""
    source = Path(path)
    with open_zip(source) as zf:
        raw = read_json(zf, INDEX_NAME)
    minecraft = str((raw.get("dependencies") or {}).get("minecraft") or "").strip()
    if not minecraft:
        raise ModpackError(tr_core("modpack.mc_missing"))
    loader, loader_version = _loader(raw.get("dependencies"))
    files: list[PackFile] = []
    for entry in raw.get("files") or []:
        if not isinstance(entry, dict):
            continue
        if _client_excluded(entry.get("env")):
            continue
        relative = str(entry.get("path") or "").strip().replace("\\", "/")
        if not relative:
            continue
        validate_member(relative)
        downloads = [str(url) for url in entry.get("downloads") or [] if str(url or "").strip()]
        hashes = entry.get("hashes") or {}
        files.append(
            PackFile(
                path=relative,
                url=downloads[0] if downloads else "",
                sha1=str(hashes.get("sha1") or "").strip().lower(),
                size=int(entry.get("fileSize") or 0),
            )
        )
    name = str(raw.get("name") or "").strip() or source.stem
    version = str(raw.get("versionId") or "").strip()
    return ModpackSpec(
        fmt=FORMAT_MODRINTH,
        name=name,
        version=version,
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        files=tuple(files),
        source=source,
        overrides=OVERRIDE_PREFIXES,
    )
