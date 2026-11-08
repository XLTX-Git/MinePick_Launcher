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
"""CurseForge modpack zip: ``manifest.json`` plus ``overrides/``.

The manifest names no URLs - only ``projectID``/``fileID`` pairs - so the files have to be resolved
through the CurseForge API at import time (``launcher.modpack.curseforge_source``). It also names no
hashes, which is why those files are verified by their declared size and file name instead.
"""

from __future__ import annotations

from pathlib import Path

from launcher.i18n import tr_core
from launcher.modpack.archive import open_zip, read_json
from launcher.modpack.errors import ModpackError
from launcher.modpack.model import FORMAT_CURSEFORGE, ModpackSpec, PackFile

MANIFEST_NAME = "manifest.json"
OVERRIDE_PREFIXES = ("overrides/",)


def _split_loader(loader_id: str) -> tuple[str, str]:
    """``forge-47.2.0`` -> ``("forge", "47.2.0")``; ``fabric-0.15.11`` -> ``("fabric", "0.15.11")``."""
    text = str(loader_id or "").strip()
    if not text:
        return "", ""
    loader, _, version = text.partition("-")
    return loader.strip().casefold(), version.strip()


def _primary_loader(mod_loaders) -> str:
    """The manifest's loader id: the entry marked ``primary`` wins, else the first one."""
    entries = [item for item in mod_loaders or [] if isinstance(item, dict)]
    for entry in entries:
        if entry.get("primary"):
            return str(entry.get("id") or "")
    return str(entries[0].get("id") or "") if entries else ""


def parse_manifest(raw: dict, source: Path) -> ModpackSpec:
    """Parse an already decoded ``manifest.json`` (the detection step reads it too)."""
    minecraft_block = raw.get("minecraft") or {}
    minecraft = str(minecraft_block.get("version") or "").strip()
    if not minecraft:
        raise ModpackError(tr_core("modpack.mc_missing"))
    loader, loader_version = _split_loader(_primary_loader(minecraft_block.get("modLoaders")))
    files: list[PackFile] = []
    for entry in raw.get("files") or []:
        if not isinstance(entry, dict):
            continue
        project_id = int(entry.get("projectID") or 0)
        file_id = int(entry.get("fileID") or 0)
        if not project_id or not file_id:
            continue
        files.append(PackFile(project_id=project_id, file_id=file_id))
    name = str(raw.get("name") or "").strip() or Path(source).stem
    version = str(raw.get("version") or "").strip()
    return ModpackSpec(
        fmt=FORMAT_CURSEFORGE,
        name=name,
        version=version,
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        files=tuple(files),
        source=Path(source),
        overrides=OVERRIDE_PREFIXES,
    )


def parse_curseforge(path: Path) -> ModpackSpec:
    """Parse a CurseForge modpack archive."""
    source = Path(path)
    with open_zip(source) as zf:
        raw = read_json(zf, MANIFEST_NAME)
    return parse_manifest(raw, source)
