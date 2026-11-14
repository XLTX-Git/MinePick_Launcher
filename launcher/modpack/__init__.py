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
"""Importing modpacks that were downloaded somewhere else - and exporting one that can be.

One import flow, three formats, and no second installer:

* **Modrinth ``.mrpack``** - a zip with ``modrinth.index.json`` (``files[]`` with direct
  ``downloads`` and ``hashes.sha1``, plus ``overrides/``) and the pack's ``dependencies``.
* **CurseForge modpack zip** - a zip with ``manifest.json`` (``minecraft.version``,
  ``minecraft.modLoaders[]`` such as ``forge-47.2.0``, ``files[]`` of project/file ids) and
  ``overrides/``.
* **MultiMC / Prism instance** - a directory or zip holding ``mmc-pack.json`` (the ``net.minecraft``
  and loader components), ``instance.cfg`` and the ``.minecraft/`` game directory.

The target Minecraft version and loader are installed through the launcher's existing chain
(:func:`launcher.install.install_version` and :func:`launcher.mods.installer.install_loader`), the
instance is created by :func:`launcher.instances.create_instance`, and every archive member is
written through the launcher's one path guard (``launcher.launch.natives._member_target``).
Nothing here talks to a server the launcher does not already use: Modrinth files come from the URLs
the pack itself names, and CurseForge files come from :mod:`launcher.mods.curseforge`.

:func:`launcher.modpack.export.export_modpack` is the other direction, writing a Modrinth or
CurseForge pack from an instance; it is re-exported below so callers do not reach into the module
path.
"""

from __future__ import annotations

from launcher.modpack.detect import detect_format, parse_modpack
from launcher.modpack.errors import ModpackError
from launcher.modpack.export import (
    CATEGORIES,
    CATEGORY_CONFIG,
    CATEGORY_MODS,
    CATEGORY_OTHER,
    CATEGORY_RESOURCEPACKS,
    CATEGORY_SAVES,
    CATEGORY_SCHEMATICS,
    CATEGORY_SCREENSHOTS,
    CATEGORY_SERVERS,
    CATEGORY_SHADERPACKS,
    DEFAULT_CATEGORIES,
    PHASE_HASH,
    PHASE_MATCH,
    PHASE_WRITE,
    CurseforgeIds,
    CurseforgeOutcome,
    ExportEntry,
    ExportPlan,
    ExportResult,
    MatchOutcome,
    ModCandidate,
    ModrinthFile,
    export_modpack,
    loader_identity,
    match_modrinth,
    plan_export,
)
from launcher.modpack.importer import ImportResult, import_modpack
from launcher.modpack.model import (
    FORMAT_CURSEFORGE,
    FORMAT_MODRINTH,
    FORMAT_MULTIMC,
    PHASE_DONE,
    PHASE_FILES,
    PHASE_INSTALL,
    PHASE_OVERRIDES,
    PHASE_PARSE,
    ModpackProgress,
    ModpackSpec,
    PackFile,
)
from launcher.modpack.naming import strip_version_suffix

__all__ = [
    "CATEGORIES",
    "CATEGORY_CONFIG",
    "CATEGORY_MODS",
    "CATEGORY_OTHER",
    "CATEGORY_RESOURCEPACKS",
    "CATEGORY_SAVES",
    "CATEGORY_SCHEMATICS",
    "CATEGORY_SCREENSHOTS",
    "CATEGORY_SERVERS",
    "CATEGORY_SHADERPACKS",
    "DEFAULT_CATEGORIES",
    "FORMAT_CURSEFORGE",
    "FORMAT_MODRINTH",
    "FORMAT_MULTIMC",
    "PHASE_DONE",
    "PHASE_FILES",
    "PHASE_HASH",
    "PHASE_INSTALL",
    "PHASE_MATCH",
    "PHASE_OVERRIDES",
    "PHASE_PARSE",
    "PHASE_WRITE",
    "CurseforgeIds",
    "CurseforgeOutcome",
    "ExportEntry",
    "ExportPlan",
    "ExportResult",
    "ImportResult",
    "MatchOutcome",
    "ModCandidate",
    "ModpackError",
    "ModpackProgress",
    "ModpackSpec",
    "ModrinthFile",
    "PackFile",
    "detect_format",
    "export_modpack",
    "import_modpack",
    "loader_identity",
    "match_modrinth",
    "parse_modpack",
    "plan_export",
    "strip_version_suffix",
]
