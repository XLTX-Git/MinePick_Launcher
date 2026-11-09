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
"""Rendering the ``manifest.json`` of a CurseForge modpack zip.

CurseForge's format is smaller than Modrinth's, and stricter in the one place that matters for an
export: every ``files[]`` entry is a ``projectID``/``fileID`` pair, and there is no download URL and
no hash. A local jar whose CurseForge file id is unknown therefore cannot be named in ``files[]`` at
all; it travels in ``overrides/`` instead, which both Prism and HMCL install verbatim. That trade is
deliberate and is reported to the user rather than hidden.

Like :mod:`launcher.modpack.mrpack_export`, this module only renders JSON: the caller resolves the
ids and decides which files are copied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from launcher.i18n import tr_core
from launcher.modpack.errors import ModpackError

if TYPE_CHECKING:  # pragma: no cover - typing only, importing at runtime would be a cycle
    from launcher.modpack.export import ExportPlan

#: The manifest member name every CurseForge pack reader looks for.
MANIFEST_NAME = "manifest.json"
#: The only manifest layout CurseForge has shipped; ``manifestVersion`` is not the pack's version.
MANIFEST_VERSION = 1
MANIFEST_TYPE = "minecraftModpack"
#: CurseForge has one override tree (no ``client-overrides/``), named ``overrides`` by convention.
OVERRIDES_DIR = "overrides"


def build_manifest(
    plan: ExportPlan,
    files: list[dict],
    *,
    name: str,
    version: str,
    author: str = "",
    description: str = "",
) -> dict:
    """The complete ``manifest.json`` document for ``plan`` and its resolved ``files[]`` entries."""
    if not plan.minecraft:
        raise ModpackError(tr_core("modpack.mc_missing"))
    loader_id = (
        f"{plan.loader}-{plan.loader_version}" if plan.loader and plan.loader_version else ""
    )
    if plan.loader and not plan.loader_version:
        raise ModpackError(
            tr_core("modpack.loader_unsupported", f"{plan.loader} (no version)")
        )
    manifest: dict = {
        "minecraft": {
            "version": plan.minecraft,
            "modLoaders": ([{"id": loader_id, "primary": True}] if loader_id else []),
        },
        "manifestType": MANIFEST_TYPE,
        "manifestVersion": MANIFEST_VERSION,
        "name": str(name or plan.name).strip() or plan.name,
        "version": str(version or "1.0.0").strip() or "1.0.0",
        "files": list(files),
        "overrides": OVERRIDES_DIR,
    }
    if author:
        manifest["author"] = author
    if description:
        manifest["description"] = description
    return manifest


def file_entry(project_id: int, file_id: int) -> dict:
    """One ``files[]`` entry: a pair of ids and whether the pack requires it."""
    return {
        "projectID": int(project_id),
        "fileID": int(file_id),
        "required": True,
    }
