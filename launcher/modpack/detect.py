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
"""Format detection for a pack the user picked.

Detection reads only the few marker members of the archive, and a CurseForge ``manifest.json`` is
only accepted when it really looks like one (a ``minecraft`` object): many unrelated zips carry a
file with that name, and a wrong guess would produce a confusing parse error instead of "this is not
a modpack".
"""

from __future__ import annotations

from pathlib import Path

from launcher.i18n import tr_core
from launcher.modpack import curseforge_pack, mrpack, multimc
from launcher.modpack.archive import common_root, open_zip, read_json
from launcher.modpack.errors import ModpackError
from launcher.modpack.model import FORMAT_CURSEFORGE, FORMAT_MODRINTH, FORMAT_MULTIMC, ModpackSpec


def _curseforge_manifest(raw: dict) -> bool:
    """Whether a decoded ``manifest.json`` is a CurseForge modpack manifest."""
    return isinstance(raw.get("minecraft"), dict) and isinstance(raw.get("files"), list)


def detect_format(path: Path) -> str:
    """One of the ``FORMAT_*`` constants, or :class:`ModpackError` when the file is not a pack."""
    source = Path(path)
    if source.is_dir():
        if (source / multimc.PACK_NAME).is_file():
            return FORMAT_MULTIMC
        raise ModpackError(tr_core("modpack.format_unknown", source.name))
    with open_zip(source) as zf:
        names = zf.namelist()
        if mrpack.INDEX_NAME in names:
            return FORMAT_MODRINTH
        if curseforge_pack.MANIFEST_NAME in names:
            try:
                if _curseforge_manifest(read_json(zf, curseforge_pack.MANIFEST_NAME)):
                    return FORMAT_CURSEFORGE
            except ModpackError:
                pass  # a broken manifest.json is not a CurseForge pack; the other markers still count
        root = common_root(names)
        if root + multimc.PACK_NAME in names:
            return FORMAT_MULTIMC
    raise ModpackError(tr_core("modpack.format_unknown", source.name))


def parse_modpack(path: Path) -> ModpackSpec:
    """Detect the format and parse it into one :class:`ModpackSpec`."""
    source = Path(path)
    fmt = detect_format(source)
    if fmt == FORMAT_MODRINTH:
        return mrpack.parse_mrpack(source)
    if fmt == FORMAT_CURSEFORGE:
        return curseforge_pack.parse_curseforge(source)
    return multimc.parse_multimc(source)
