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

"""Disk readers behind the instance detail's mod, resource-pack, shader-pack and save lists.

The pages list what is on disk: one row per file in ``mods/``, ``resourcepacks/`` or ``shaderpacks/``,
one row per folder in ``saves/``. Nothing here draws - the Qt page owns the pixels - but the small
helpers below turn a disk name into display text, pull a project's own artwork out of a jar or a zip,
and read the two fields a world folder holds about itself (its name and when it was created). Keeping
those decisions here means they can be measured against a temporary directory, which is exactly how
the tests exercise them. A directory that exists but cannot be read raises instead of returning an
empty list, so an unreadable folder is reported as a failure and never rendered as "this instance has
nothing installed".

The one write in this module is :func:`backup_save`: it zips a world folder and leaves the original
untouched. No function here downloads anything.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
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
#: A mod's logo is a project icon, so it follows the same 2 MB cap the online icon store uses.
MOD_ICON_MAX_BYTES = 2 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: Image signatures Qt can decode. A jar entry that merely *looks* like an icon is not one.
_IMAGE_MAGIC = (
    _PNG_SIGNATURE,
    b"\xff\xd8\xff",  # JPEG
    b"GIF87a",
    b"GIF89a",
)
#: Bounded so a big modpack folder cannot keep the bytes of a thousand jars alive forever.
_MOD_ICON_CACHE_SIZE = 512
#: Bounded for the same reason; a world icon is a 64x64 PNG.
_SAVE_ICON_CACHE_SIZE = 512
#: Bounded for the same reason; a pack icon is read once per refill.
_PACK_ICON_CACHE_SIZE = 512
#: Where a mod jar keeps its logo when its metadata does not name one. The depth limit stops a jar
#: with a thousand ``icon.png`` files from being scanned for a picture nobody asked for.
_MOD_ICON_FALLBACK_NAMES = frozenset({"icon.png", "logo.png"})
_MOD_ICON_FALLBACK_DEPTH = 3
#: Forge/NeoForge declare the logo as ``logoFile = "..."`` in the mods table.
_LOGO_FILE = re.compile(r"""^\s*logoFile\s*=\s*["']([^"']+)["']""", re.MULTILINE)

#: NBT tag header of the fields read from ``level.dat``. TAG_String, name length 9, "LevelName";
#: TAG_Long, name length 7, "Created" (milliseconds since the epoch).
_LEVEL_NAME_MARKER = b"\x08\x00\x09LevelName"
_LEVEL_CREATED_MARKER = b"\x04\x00\x07Created"
#: A world name longer than this is not a name; refusing the length also keeps a corrupt file from
#: making the page read an arbitrary slice as text.
_LEVEL_NAME_MAX_BYTES = 512
#: A creation stamp outside this window is not a date a person wrote a world on (Minecraft 1.0 is
#: 2011; the upper bound is generous for a clock-skewed machine).
_LEVEL_CREATED_MIN = 1_230_768_000.0  # 2009-01-01
_LEVEL_CREATED_MAX = 4_102_444_800.0  # 2100-01-01
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
    """One world folder in ``saves/``.

    ``world_name`` comes from ``level.dat`` when it has one; ``created`` is the game's own
    ``Data.Created`` stamp (milliseconds since the epoch) when it is readable and the folder's
    creation time otherwise, so a world is never listed without a date. ``icon`` is the world's
    ``icon.png`` - Minecraft screenshots the game the first time the world is saved - and ``None``
    when the world has never been saved in game.
    """

    folder: str
    world_name: str
    path: Path
    modified: float
    size: int
    created: float = 0.0
    icon: bytes | None = None


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


def format_day(timestamp: float) -> str:
    """Format a timestamp as a bare calendar day (``2026/08/09``), for a row's two date fields."""
    try:
        return time.strftime("%Y/%m/%d", time.localtime(timestamp))
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


def _looks_like_image(data: bytes) -> bool:
    """True for the raster formats Qt decodes without an extra plugin (PNG/JPEG/GIF/WebP)."""
    if data.startswith(_IMAGE_MAGIC):
        return True
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _icon_path(value: object) -> str:
    """A metadata ``icon`` value -> one archive path.

    Fabric and Quilt accept both shapes: a plain string, or a dictionary keyed by the icon size
    (``{"64": "icon_64.png", "128": "icon_128.png"}``). The largest key wins because that is the
    sharpest picture the author shipped; a dictionary with non-numeric keys falls back to its
    longest value so the choice stays deterministic instead of following dict order.
    """
    if isinstance(value, str):
        return value.strip().lstrip("/")
    if not isinstance(value, dict):
        return ""
    candidates: list[tuple[tuple[int, int], str]] = []
    for key, item in value.items():
        if not isinstance(item, str) or not item.strip():
            continue
        text = str(key).strip()
        rank = int(text) if text.isdigit() else -1
        candidates.append(((rank, len(item)), item.strip().lstrip("/")))
    if not candidates:
        return ""
    candidates.sort(key=lambda entry: entry[0], reverse=True)
    return candidates[0][1]


def _metadata_icon_path(archive: zipfile.ZipFile, names: set[str]) -> str:
    """The icon a mod's own metadata names, in the order the loaders declare it."""
    candidates: list[str] = []
    if "fabric.mod.json" in names:
        try:
            data = json.loads(archive.read("fabric.mod.json").decode("utf-8", "replace"))
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
            data = {}
        if isinstance(data, dict):
            candidates.append(_icon_path(data.get("icon")))
    if "quilt.mod.json" in names:
        try:
            data = json.loads(archive.read("quilt.mod.json").decode("utf-8", "replace"))
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
            data = {}
        if isinstance(data, dict):
            loader = data.get("quilt_loader") if isinstance(data.get("quilt_loader"), dict) else {}
            metadata = loader.get("metadata") if isinstance(loader.get("metadata"), dict) else {}
            candidates.append(_icon_path(metadata.get("icon")))
            candidates.append(_icon_path(data.get("icon")))
    for toml_name in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
        if toml_name not in names:
            continue
        try:
            text = archive.read(toml_name).decode("utf-8", "replace")
        except (OSError, RuntimeError, zipfile.BadZipFile):
            continue
        match = _LOGO_FILE.search(text)
        if match:
            candidates.append(match.group(1).strip().lstrip("/"))
    return next((path for path in candidates if path), "")


def _fallback_icon_name(names: list[str]) -> str:
    """The shallowest ``icon.png`` / ``logo.png`` (depth <= 3), or ``""``.

    Root ``icon.png`` wins over ``assets/<mod>/icon.png``, and a short path wins over a long one,
    so two jars that both ship artwork resolve to the same entry run to run.
    """
    best = ""
    best_rank: tuple[int, int, str] | None = None
    for name in names:
        if not name or name.endswith("/"):
            continue
        base = name.rsplit("/", 1)[-1].casefold()
        if base not in _MOD_ICON_FALLBACK_NAMES:
            continue
        depth = name.count("/")
        if depth > _MOD_ICON_FALLBACK_DEPTH:
            continue
        rank = (depth, len(name), name.casefold())
        if best_rank is None or rank < best_rank:
            best_rank, best = rank, name
    return best


def _archive_entry(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo | None:
    """One entry by path, accepting a differently-cased spelling when the exact name is absent."""
    try:
        return archive.getinfo(name)
    except KeyError:
        pass
    wanted = name.casefold()
    for entry in archive.infolist():
        if entry.filename.casefold() == wanted:
            return entry
    return None


def _read_archive_icon(archive: zipfile.ZipFile, names: set[str]) -> bytes | None:
    """Read the icon a jar names, then the conventional fallbacks; ``None`` when it has none."""
    candidates = [_metadata_icon_path(archive, names)]
    candidates.append(_fallback_icon_name(archive.namelist()))
    for name in candidates:
        if not name:
            continue
        info = _archive_entry(archive, name)
        if info is None or info.is_dir() or info.file_size > MOD_ICON_MAX_BYTES:
            continue
        try:
            data = archive.read(info)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            continue
        if data and len(data) <= MOD_ICON_MAX_BYTES and _looks_like_image(data):
            return data
    return None


@lru_cache(maxsize=_MOD_ICON_CACHE_SIZE)
def _read_mod_icon(source: str, mtime_ns: int, size: int) -> bytes | None:
    """Body of :func:`mod_icon_bytes`; the arguments are the jar's identity, not its content."""
    try:
        with zipfile.ZipFile(Path(source)) as archive:
            return _read_archive_icon(archive, set(archive.namelist()))
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        return None


def mod_icon_bytes(path: Path) -> bytes | None:
    """The PNG/JPEG/WebP bytes of a mod jar's own icon, read locally, or None when it has none.

    The order is the one the loaders themselves use, because the first answer is what the game
    shows: Fabric/Quilt's ``icon`` metadata (a string, or a size-keyed dictionary), Forge/NeoForge's
    ``logoFile``, and only then the conventional ``icon.png`` / ``logo.png`` somewhere in the first
    three levels of the archive. The archive is never extracted, and a missing, unreadable, corrupt
    or oversized entry is simply "no artwork" - the page draws its neutral cube. The result is
    cached by (path, mtime, size), so refilling a list does not reopen every jar, while a replaced
    jar gets a new entry.
    """
    source = Path(path)
    try:
        info = source.stat()
    except OSError:
        return None
    return _read_mod_icon(str(source), info.st_mtime_ns, info.st_size)


def save_icon_bytes(folder: Path) -> bytes | None:
    """The world's own ``icon.png`` (the screenshot Minecraft saves), or None when it has none.

    A world that has never been saved in game has no icon; the caller draws the neutral cube. Only
    that one file is read - the world folder is never scanned for pictures.
    """
    icon = Path(folder) / "icon.png"
    try:
        info = icon.stat()
    except OSError:
        return None
    return _read_save_icon(str(icon), info.st_mtime_ns, info.st_size)


@lru_cache(maxsize=_SAVE_ICON_CACHE_SIZE)
def _read_save_icon(source: str, mtime_ns: int, size: int) -> bytes | None:
    try:
        data = Path(source).read_bytes()
    except OSError:
        return None
    if not data or len(data) > PACK_ICON_MAX_BYTES or not data.startswith(_PNG_SIGNATURE):
        return None
    return data


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


def _level_bytes(level_dat: Path) -> bytes | None:
    """The uncompressed NBT body of a ``level.dat``, or None when it cannot be read."""
    try:
        raw = level_dat.read_bytes()
    except OSError:
        return None
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            return None
    return raw


def read_level_name(level_dat: Path) -> str | None:
    """The ``LevelName`` string in a save's ``level.dat``, or None when it cannot be recovered.

    Minecraft writes a gzip-compressed NBT compound (``Data.LevelName``). Only that one field is
    read - the whole file is not modelled - so a world whose format this launcher does not
    understand still lists under its folder name instead of failing the page.
    """
    raw = _level_bytes(level_dat)
    if raw is None:
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


def read_level_created(level_dat: Path) -> float | None:
    """``Data.Created`` - the game's own world creation time - as seconds since the epoch.

    The game stores milliseconds in a TAG_Long; the conversion is decided by magnitude (a value past
    :data:`_LEVEL_CREATED_MAX` in seconds can only be milliseconds, and the other way round), so both
    the millisecond field the game writes and a seconds field some editor may write come out as a
    date. A missing, corrupt or implausible value returns None and the caller falls back to the
    folder's own creation time rather than showing 1970.
    """
    raw = _level_bytes(level_dat)
    if raw is None:
        return None
    position = raw.find(_LEVEL_CREATED_MARKER)
    if position < 0:
        return None
    start = position + len(_LEVEL_CREATED_MARKER)
    if start + 8 > len(raw):
        return None
    value = struct.unpack_from(">q", raw, start)[0]
    if value <= 0:
        return None
    seconds = value / 1000.0 if value > int(_LEVEL_CREATED_MAX) else float(value)
    if not _LEVEL_CREATED_MIN <= seconds <= _LEVEL_CREATED_MAX:
        return None
    return seconds


def scan_saves(directory: Path) -> list[SaveFolder]:
    """The world folders in ``saves/``, newest first.

    Like :func:`scan_content_files`: a missing ``saves/`` is empty, one that exists but cannot be
    listed raises. ``level.dat`` supplies the display name and the in-game creation time (the folder
    name / the folder's own creation time are the fallbacks), and the world's ``icon.png`` is read
    here - not on the GUI thread - so a list of worlds paints its thumbnails in the first pass.
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
            created=read_level_created(entry / "level.dat") or entry.stat().st_ctime,
            icon=save_icon_bytes(entry),
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
