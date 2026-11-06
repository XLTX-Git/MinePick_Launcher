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

"""Disk readers behind the instance detail's resource-pack, shader-pack and save pages.

The pages list what is on disk: one row per file in ``resourcepacks/`` or ``shaderpacks/``, one row
per folder in ``saves/``. Nothing here draws or decides what a row looks like - the Qt page owns
that - which is what keeps these functions testable against a temporary directory. A directory that
exists but cannot be read raises instead of returning an empty list, so an unreadable folder is
reported as a failure and never rendered as "this instance has nothing installed".

The one write in this module is :func:`backup_save`: it zips a world folder and leaves the original
untouched. No function here downloads anything.
"""

from __future__ import annotations

import gzip
import json
import logging
import struct
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: NBT tag header of the one field read from ``level.dat``: TAG_String, name length 9, "LevelName".
_LEVEL_NAME_MARKER = b"\x08\x00\x09LevelName"
#: A world name longer than this is not a name; refusing the length also keeps a corrupt file from
#: making the page read an arbitrary slice as text.
_LEVEL_NAME_MAX_BYTES = 512
#: Files a running world keeps open and that carry no world data. Zipping one of them fails with a
#: sharing violation while the game is running, so a backup skips it instead of failing as a whole.
_BACKUP_SKIP = frozenset({"session.lock"})


@dataclass(frozen=True)
class ContentFile:
    """One file in ``resourcepacks/`` or ``shaderpacks/``.

    ``enabled`` is True/False when the caller passed the enabled pack ids, and None when that list
    could not be read (the page then says "unknown" instead of guessing).
    """

    name: str
    path: Path
    size: int
    enabled: bool | None = None


@dataclass(frozen=True)
class SaveFolder:
    """One world folder in ``saves/``; ``world_name`` comes from ``level.dat`` when it has one."""

    folder: str
    world_name: str
    path: Path
    modified: float
    size: int


def directory_size(path: Path) -> int:
    """Total size of the regular files below a directory (the part that could be read)."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return total
    return total


def format_modified(timestamp: float) -> str:
    """Format a modification time the way the overview formats a last-played time."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return ""


def read_enabled_resource_packs(options_path: Path) -> set[str] | None:
    """The pack ids ``options.txt`` records as enabled, in lower case.

    The game writes ``resourcePacks:["vanilla","file/Faithful.zip"]``; a pack inside
    ``resourcepacks/`` is switched on by the ``file/<name>`` entry (older versions wrote the bare
    name), and both spellings are accepted here. A missing options.txt means the game never wrote
    its settings, so nothing but vanilla is on - an empty set, not an error. A file that exists but
    cannot be read or parsed returns None: the page then shows "unknown" instead of claiming every
    pack is disabled.
    """
    try:
        text = options_path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return set()
    except OSError:
        LOGGER.warning("cannot read %s", options_path, exc_info=True)
        return None
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key.strip() != "resourcePacks":
            continue
        try:
            entries = json.loads(value.strip())
        except ValueError:
            LOGGER.warning("cannot parse resourcePacks in %s", options_path)
            return None
        if not isinstance(entries, list):
            return None
        return {str(entry).strip().casefold() for entry in entries if str(entry).strip()}
    return set()


def _is_enabled(name: str, enabled: set[str]) -> bool:
    key = name.casefold()
    return key in enabled or f"file/{key}" in enabled


def scan_content_files(directory: Path, *, enabled: set[str] | None = None) -> list[ContentFile]:
    """The files directly inside a content directory, sorted by name.

    A directory that does not exist yet is an empty one (the launcher creates it on first install).
    A directory that exists but cannot be listed raises ``OSError`` - the caller reports it.
    """
    if not directory.exists():
        return []
    items = [
        ContentFile(
            name=entry.name,
            path=entry,
            size=entry.stat().st_size,
            enabled=None if enabled is None else _is_enabled(entry.name, enabled),
        )
        for entry in directory.iterdir()
        if entry.is_file()
    ]
    items.sort(key=lambda item: item.name.casefold())
    return items


def read_level_name(level_dat: Path) -> str | None:
    """The ``LevelName`` string in a save's ``level.dat``, or None when it cannot be recovered.

    Minecraft writes a gzip-compressed NBT compound (``Data.LevelName``). Only that one field is
    read - the whole file is not modelled - so a world whose format this launcher does not
    understand still lists under its folder name instead of failing the page.
    """
    try:
        raw = level_dat.read_bytes()
    except OSError:
        return None
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            return None
    position = raw.find(_LEVEL_NAME_MARKER)
    if position < 0:
        return None
    start = position + len(_LEVEL_NAME_MARKER)
    if start + 2 > len(raw):
        return None
    length = struct.unpack_from(">H", raw, start)[0]
    start += 2
    if not 0 < length <= _LEVEL_NAME_MAX_BYTES or start + length > len(raw):
        return None
    try:
        name = raw[start:start + length].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return name.strip() or None


def scan_saves(directory: Path) -> list[SaveFolder]:
    """The world folders in ``saves/``, newest first.

    Like :func:`scan_content_files`: a missing ``saves/`` is empty, one that exists but cannot be
    listed raises. ``level.dat`` is only read for the display name; the folder name is the fallback.
    """
    if not directory.exists():
        return []
    saves = [
        SaveFolder(
            folder=entry.name,
            world_name=read_level_name(entry / "level.dat") or entry.name,
            path=entry,
            modified=entry.stat().st_mtime,
            size=directory_size(entry),
        )
        for entry in directory.iterdir()
        if entry.is_dir()
    ]
    saves.sort(key=lambda save: (-save.modified, save.folder.casefold()))
    return saves


def backup_save(folder: Path, destination: Path) -> Path:
    """Zip one world folder into ``destination`` and return it.

    The archive holds the folder itself (``<world>/level.dat``), so unzipping it into ``saves/``
    restores the world under its own name. The write goes to a ``.part`` file and is renamed at the
    end: a cancelled or failed backup cannot leave a half-written zip that looks like a backup.
    ``session.lock`` is skipped because the running game holds it open and it carries no world data.
    """
    folder = Path(folder)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(folder.rglob("*")):
                if not path.is_file() or path.name in _BACKUP_SKIP:
                    continue
                archive.write(path, (Path(folder.name) / path.relative_to(folder)).as_posix())
        partial.replace(destination)
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
    return destination
