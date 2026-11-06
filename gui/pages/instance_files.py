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
per folder in ``saves/``. Nothing here draws - the Qt page owns the pixels - but the small helpers
below turn a disk name into display text and a pack's own ``pack.png`` into bytes, which keeps those
two decisions testable against a temporary directory. A directory that exists but cannot be read
raises instead of returning an empty list, so an unreadable folder is reported as a failure and never
rendered as "this instance has nothing installed".

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
from functools import lru_cache
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: Minecraft's formatting prefix. The character after it is a colour (``§c``) or a style (``§l``).
FORMATTING_CODE = "\u00a7"
#: A real ``pack.png`` is a few KB; anything this large is not a pack icon and is not read.
PACK_ICON_MAX_BYTES = 4 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: Bounded so a big modpack folder cannot keep the bytes of a thousand packs alive forever.
_PACK_ICON_CACHE_SIZE = 512

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


def strip_formatting_codes(name: str) -> str:
    """Remove Minecraft's ``§`` formatting codes from ``name`` (display text only).

    The game lets a resource-pack or shader-pack file carry colour and style codes: ``§`` plus exactly
    one character. Removing the pair repeats until no ``§`` is left, so both ``§cCozyUI§b+ §7v1.10.zip``
    and the modern hex form ``§x§1§2§a§b§c§dName.zip`` come out clean. A trailing ``§`` is dropped on
    its own because there is no following character to consume. This is a *display* helper: the caller
    keeps ``name`` for every path operation, and nothing here changes the file on disk.
    """
    out: list[str] = []
    index = 0
    while index < len(name):
        if name[index] == FORMATTING_CODE:
            index += 2  # the prefix plus the one character it formats
            continue
        out.append(name[index])
        index += 1
    return "".join(out)


def display_file_name(name: str) -> str:
    """``name`` without formatting codes, falling back to the raw name when nothing is left.

    A file literally named ``§c`` has no readable text once the code is removed; showing an empty cell
    would make the row look broken, so the original name is shown instead (the tooltip always carries
    it anyway).
    """
    cleaned = strip_formatting_codes(name)
    return cleaned or name


@lru_cache(maxsize=_PACK_ICON_CACHE_SIZE)
def _read_pack_icon(source: str, mtime_ns: int, size: int, folder: bool) -> bytes | None:
    """Body of :func:`pack_icon_bytes`; the arguments are the source file's identity, not its content."""
    path = Path(source)
    if folder:
        try:
            data = path.read_bytes()
        except OSError:
            return None
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                try:
                    info = archive.getinfo("pack.png")
                except KeyError:
                    return None
                if info.file_size > PACK_ICON_MAX_BYTES:
                    return None
                data = archive.read(info)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
            return None
    if len(data) > PACK_ICON_MAX_BYTES or not data.startswith(_PNG_SIGNATURE):
        return None
    return data


def pack_icon_bytes(path: Path) -> bytes | None:
    """The PNG bytes of a pack's own ``pack.png``, read locally, or None when it has none.

    A ``.zip`` keeps the icon at the archive root (that is where the game reads it); a folder pack
    keeps it as ``<folder>/pack.png``. Only that one entry is read - the archive is never extracted.
    The result is cached by (path, mtime, size), so refilling a list does not reopen every zip, while a
    replaced pack gets a new entry. A missing, unreadable, corrupt or non-PNG file returns None and the
    caller draws its neutral placeholder. Nothing here touches the network.
    """
    source = Path(path)
    try:
        if source.is_dir():
            icon = source / "pack.png"
            info = icon.stat()
            key = (str(icon), info.st_mtime_ns, info.st_size, True)
        else:
            info = source.stat()
            key = (str(source), info.st_mtime_ns, info.st_size, False)
    except OSError:
        return None
    return _read_pack_icon(*key)


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
