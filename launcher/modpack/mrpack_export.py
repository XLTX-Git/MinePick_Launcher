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
"""Rendering the ``modrinth.index.json`` this project's own importer reads back.

The importer (:mod:`launcher.modpack.mrpack`) is the specification this writer is written against:
``formatVersion`` 1, ``game`` "minecraft", the loader named in ``dependencies``, and one ``files[]``
entry per file that has a real download URL. Everything else travels in ``overrides/`` (and
``client-overrides/`` for the client-only tree) and is copied verbatim on import, which is the part
of the format that keeps a pack importable even when no project id could be resolved.

The module only renders JSON. It does not read jars, hash files or open zips: the caller
(:mod:`launcher.modpack.export`) owns the scan and chooses which files become ``files[]`` entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from launcher.i18n import tr_core
from launcher.modpack.errors import ModpackError

if TYPE_CHECKING:  # pragma: no cover - typing only, importing at runtime would be a cycle
    from launcher.modpack.export import ExportPlan

#: The index member name both the Modrinth App and this launcher's importer look for.
INDEX_NAME = "modrinth.index.json"
#: The only format version Modrinth has ever published; a reader that does not know a newer one must
#: refuse it rather than guess, which is why the number is explicit instead of implied.
FORMAT_VERSION = 1
#: ``game`` is the Minecraft id, not the display name.
GAME = "minecraft"

#: Loader name (the vocabulary :mod:`launcher.modpack.model` and the importer use) -> the
#: ``dependencies`` key the format defines. Quilt is here because the importer parses it and fails
#: with a sentence naming the loader; writing it keeps that round trip honest.
LOADER_DEPENDENCIES = {
    "fabric": "fabric-loader",
    "quilt": "quilt-loader",
    "forge": "forge",
    "neoforge": "neoforge",
}

#: A client instance is everything this launcher can describe. ``server`` is "optional" rather than
#: "required" or "unsupported": many mods do run on a server, and the format treats an omitted answer
#: as required, which would make a server refuse a file that would have worked.
CLIENT_ENV = {"client": "required", "server": "optional"}


def build_index(
    plan: ExportPlan,
    files: list[dict],
    *,
    name: str,
    version: str,
    summary: str = "",
) -> dict:
    """The complete index document for ``plan`` and its resolved ``files[]`` entries."""
    dependencies: dict[str, str] = {"minecraft": plan.minecraft}
    if plan.loader:
        key = LOADER_DEPENDENCIES.get(plan.loader)
        if key is None:
            raise ModpackError(tr_core("modpack.loader_unsupported", plan.loader))
        if not plan.loader_version:
            raise ModpackError(
                tr_core("modpack.loader_unsupported", f"{plan.loader} (no version)")
            )
        dependencies[key] = plan.loader_version
    index: dict = {
        "formatVersion": FORMAT_VERSION,
        "game": GAME,
        "versionId": str(version or "1.0.0").strip() or "1.0.0",
        "name": str(name or plan.name).strip() or plan.name,
        "dependencies": dependencies,
        "files": list(files),
    }
    text = str(summary or "").strip()
    if text:
        index["summary"] = text
    return index


def file_entry(*, path: str, url: str, sha1: str, sha512: str, size: int) -> dict:
    """One ``files[]`` entry: where it goes, what it must hash to, and where to get it."""
    return {
        "path": path,
        "hashes": {"sha1": sha1, "sha512": sha512},
        "env": dict(CLIENT_ENV),
        "downloads": [url],
        "fileSize": max(0, int(size)),
    }
