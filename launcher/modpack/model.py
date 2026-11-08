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
"""The data a parsed modpack is passed around as: no filesystem handle, no Qt, no installer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The three accepted formats, as stored in :attr:`ModpackSpec.fmt`.
FORMAT_MODRINTH = "modrinth"
FORMAT_CURSEFORGE = "curseforge"
FORMAT_MULTIMC = "multimc"

#: The phases an import reports. The GUI maps each one to its own status line; the importer only
#: emits them in order.
PHASE_PARSE = "parse"
PHASE_INSTALL = "install"
PHASE_FILES = "files"
PHASE_OVERRIDES = "overrides"
PHASE_DONE = "done"


@dataclass(frozen=True)
class PackFile:
    """One file a pack carries: a direct download, or a CurseForge project/file pair.

    ``path`` is the destination inside the instance folder for Modrinth files and empty for
    CurseForge entries (their real name - and therefore their destination - is only known after the
    API answers, so the importer computes it then). ``sha1`` is what the pack declares; CurseForge
    has no hash in its manifest, so those files are verified by declared size and file name instead.
    """

    path: str = ""
    url: str = ""
    sha1: str = ""
    size: int = 0
    project_id: int = 0
    file_id: int = 0

    @property
    def is_curseforge(self) -> bool:
        return bool(self.project_id and self.file_id)


@dataclass(frozen=True)
class ModpackSpec:
    """A parsed pack: what to install, and where its own files live.

    ``source`` is the archive (or the MultiMC instance directory). ``overrides`` are archive member
    prefixes to copy into the instance folder (mrpack: ``overrides/`` and ``client-overrides/``;
    CurseForge: ``overrides/``; MultiMC zip: ``<root>/.minecraft/``); a MultiMC *directory* instead
    names folder names relative to ``source`` (``.minecraft``).
    """

    fmt: str
    name: str
    version: str
    minecraft: str
    loader: str
    loader_version: str
    files: tuple[PackFile, ...]
    source: Path
    source_is_dir: bool = False
    overrides: tuple[str, ...] = ()
    instance_name: str = ""


@dataclass(frozen=True)
class ModpackProgress:
    """One progress tick: which phase runs, how far it is, and what it is doing right now."""

    phase: str
    done_files: int = 0
    total_files: int = 0
    done_bytes: int = 0
    total_bytes: int = 0
    current: str = ""
