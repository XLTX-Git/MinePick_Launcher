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

"""Find one instance's schematic files and read their headers.

Litematica has moved its default folder over the years, so all the layouts it has shipped are
checked: ``schematics/`` in the instance folder (the current default), ``config/litematica/`` (the
mod's config folder, which some versions use for the files themselves) and
``config/litematica/schematics/``. Only files directly inside those folders are listed; a schematic
pack nested deeper is a layout nobody ships.

The scan is one pass over file metadata plus one bounded header read per file. Failures are data, not
exceptions: a damaged file becomes a row with ``error`` set, because one broken download must not
blank the whole card. The caller runs this on a worker thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from launcher.schematic.litematic import (
    HEADER_BYTES,
    SUFFIX,
    SchematicError,
    SchematicInfo,
    read_schematic,
)

#: The folder Litematica writes to by default, relative to the instance (game) directory.
PRIMARY_SUBDIR = "schematics"
#: Every folder Litematica has used for the files, most common first.
SCHEMATIC_SUBDIRS: tuple[str, ...] = (
    PRIMARY_SUBDIR,
    "config/litematica",
    "config/litematica/schematics",
)


@dataclass(frozen=True)
class SchematicFile:
    """One ``.litematic`` on disk: its file identity, its header, and why the header is missing."""

    path: Path
    modified: float
    size_bytes: int
    info: SchematicInfo | None = None
    error: str = ""

    @property
    def readable(self) -> bool:
        return self.info is not None

    @property
    def file_name(self) -> str:
        """The on-disk name, which is the row title (the metadata name is shown beside it)."""
        return self.path.name

    @property
    def title(self) -> str:
        """The metadata name when the file carries one, else the file's stem."""
        name = (self.info.name if self.info is not None else "").strip()
        return name or self.path.stem


def schematic_dirs(instance_root: Path) -> list[Path]:
    """The existing schematic folders of one instance, in scan order, without duplicates."""
    found: list[Path] = []
    seen: set[Path] = set()
    for subdir in SCHEMATIC_SUBDIRS:
        candidate = instance_root / subdir
        try:
            key = candidate.resolve()
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_dir():
                found.append(candidate)
        except OSError:
            continue
    return found


def schematic_folder(instance_root: Path) -> Path:
    """Where "open the folder" should land: the first folder that exists, else the default one.

    The default is returned even when it does not exist yet: the button is also the way to create the
    folder Litematica will read, and the caller creates it before opening (see InstanceDetail).
    """
    found = schematic_dirs(instance_root)
    return found[0] if found else instance_root / PRIMARY_SUBDIR


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _is_schematic(path: Path) -> bool:
    return not path.name.startswith(".") and path.name.lower().endswith(SUFFIX)


def scan_schematics(
    instance_root: Path,
    *,
    max_bytes: int = HEADER_BYTES,
    stop: threading.Event | None = None,
) -> list[SchematicFile]:
    """Every schematic of one instance, newest first, with its header read.

    ``stop`` is checked between files and inside the header read, so a page that is torn down while a
    large file is being decoded leaves no worker parked on it.
    """
    files: list[SchematicFile] = []
    seen: set[Path] = set()
    for folder in schematic_dirs(Path(instance_root)):
        if stop is not None and stop.is_set():
            break
        try:
            entries = sorted(folder.iterdir(), key=lambda entry: entry.name.lower())
        except OSError:
            continue
        for entry in entries:
            if stop is not None and stop.is_set():
                break
            if entry in seen:
                continue
            seen.add(entry)
            try:
                if not entry.is_file() or not _is_schematic(entry):
                    continue
                stat = entry.stat()
            except OSError:
                continue
            info: SchematicInfo | None = None
            error = ""
            try:
                info = read_schematic(entry, max_bytes=max_bytes, stop=stop)
            except SchematicError as exc:
                error = _error_text(exc)
            except OSError as exc:  # a stat/read race with the user's file manager
                error = _error_text(exc)
            files.append(
                SchematicFile(
                    path=entry,
                    modified=stat.st_mtime,
                    size_bytes=stat.st_size,
                    info=info,
                    error=error,
                )
            )
    files.sort(key=lambda item: (-item.modified, item.file_name.lower()))
    return files
