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

"""``servers.dat``: the multiplayer server list Minecraft keeps next to its options.

The file is *uncompressed NBT*: a root compound holding a ``servers`` list of compounds, each with a
``name``, an ``ip`` and - when the user pinned a picture - a base64 ``icon`` data URI.

Reading reuses the project's NBT walker
---------------------------------------
``launcher.schematic.nbt`` already decodes NBT with the standard library (it is what the Litematica
reader uses), so this module reads through :func:`read_nbt` instead of growing a second reader. That
walker is read-only on purpose, and it also drops what a *round-trip* needs (a byte becomes an int, a
float a Python float), so the write half below encodes the four known fields from typed Python values:
``name``/``ip``/``icon`` as strings and ``acceptTextures`` as a byte. Unknown keys are carried through
best-effort (see :func:`_encode_extra`) so a future Minecraft field survives a launcher edit instead of
being deleted by it.

Writing is atomic and backed up
-------------------------------
A launcher that truncates ``servers.dat`` loses the user's whole server list, so the write never
touches the original in place: the bytes go to ``servers.dat.tmp`` in the same folder, are flushed to
the disk, the previous file is copied to ``servers.dat.bak``, and only then does :func:`os.replace`
swap the temporary file in. A crash at any point leaves either the old file or the new one, never half
of each.

String encoding follows Java's ``writeUTF`` for everything in the Basic Multilingual Plane (which is
what Minecraft's reader expects); the schematic reader decodes with replacement characters for
non-BMP text, so a name made of emoji comes back altered - the same limitation the existing reader has.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from launcher.schematic.nbt import (
    TAG_BYTE,
    TAG_COMPOUND,
    TAG_DOUBLE,
    TAG_END,
    TAG_INT,
    TAG_LIST,
    TAG_LONG,
    TAG_STRING,
    NBTFormatError,
    read_nbt,
)

LOGGER = logging.getLogger(__name__)

FILENAME = "servers.dat"
BACKUP_SUFFIX = ".bak"
TEMP_SUFFIX = ".tmp"
#: The whole list is normally a few hundred bytes. A file this large is not a server list, and reading
#: it would only produce a decoded blob nobody asked for.
MAX_BYTES = 8 * 1024 * 1024
#: NBT strings are Java's ``writeUTF``: an unsigned 16-bit byte count.
MAX_STRING_BYTES = 0xFFFF
#: The fields Minecraft itself reads; everything else on an entry is preserved as an extra.
KNOWN_FIELDS = ("name", "ip", "icon", "acceptTextures")


class ServersDatError(Exception):
    """The file could not be read or written (the message is user-facing)."""


@dataclass(frozen=True)
class ServerEntry:
    """One multiplayer entry: what the game shows in its server list."""

    name: str
    ip: str
    icon: str = ""
    #: ``acceptTextures`` as Minecraft stores it (a byte, 0 or 1); ``None`` = the file had no such tag.
    accept_textures: int | None = None
    #: Keys this launcher does not know about, kept so a round-trip does not delete them.
    extra: dict[str, Any] = field(default_factory=dict)

    def renamed(self, name: str, ip: str) -> ServerEntry:
        """The same entry with a new name and address (icon and unknown fields are kept)."""
        return ServerEntry(
            name=name,
            ip=ip,
            icon=self.icon,
            accept_textures=self.accept_textures,
            extra=dict(self.extra),
        )


@dataclass
class ServersFile:
    """The decoded file: the entries plus whatever else the root compound carried."""

    entries: list[ServerEntry] = field(default_factory=list)
    hidden: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def servers_path(game_dir: Path | str) -> Path:
    """The ``servers.dat`` of one game folder."""
    return Path(game_dir) / FILENAME


def backup_path(path: Path | str) -> Path:
    """The backup a write leaves next to the file: ``servers.dat.bak``."""
    path = Path(path)
    return path.with_name(path.name + BACKUP_SUFFIX)


# ---------------------------------------------------------------- reading


def read_servers(path: Path | str) -> ServersFile:
    """Decode ``servers.dat``.

    A missing file is an empty list, not an error: a fresh game folder has no server list yet. A file
    that exists but cannot be decoded raises :class:`ServersDatError`; the caller decides whether to
    show that or to fall back to an empty list - it must never crash the page.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ServersFile()
    except OSError as exc:
        raise ServersDatError(str(exc)) from exc
    if len(raw) > MAX_BYTES:
        raise ServersDatError(f"the file is larger than {MAX_BYTES} bytes")
    if not raw:
        raise ServersDatError("the file is empty")
    try:
        document = read_nbt(io.BytesIO(raw), max_bytes=len(raw))
    except NBTFormatError as exc:
        raise ServersDatError(str(exc)) from exc
    if document.truncated:
        raise ServersDatError("the tag stream did not fit inside the read budget")
    root = document.root
    listed = root.get("servers", [])
    if listed is None:
        listed = []
    if not isinstance(listed, list):
        raise ServersDatError("the root compound has no 'servers' list")
    entries: list[ServerEntry] = []
    for item in listed:
        if not isinstance(item, dict):  # a corrupt entry must not hide the rest of the list
            LOGGER.warning("servers.dat: skipping an entry that is not a compound")
            continue
        accept = item.get("acceptTextures")
        entries.append(
            ServerEntry(
                name=_text(item.get("name")),
                ip=_text(item.get("ip")),
                icon=_text(item.get("icon")),
                accept_textures=accept
                if isinstance(accept, int) and not isinstance(accept, bool)
                else None,
                extra={key: value for key, value in item.items() if key not in KNOWN_FIELDS},
            )
        )
    hidden = root.get("hidden")
    return ServersFile(
        entries=entries,
        hidden=hidden if isinstance(hidden, int) and not isinstance(hidden, bool) else None,
        extras={key: value for key, value in root.items() if key not in ("servers", "hidden")},
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------- writing


def encode_servers(data: ServersFile) -> bytes:
    """Encode a :class:`ServersFile` as the uncompressed NBT Minecraft reads."""
    body = bytearray()
    body += _named(TAG_LIST, "servers", _encode_entries(data.entries))
    for key, value in data.extras.items():
        body += _encode_extra(key, value)
    if data.hidden is not None:
        body += _named(TAG_BYTE, "hidden", struct.pack(">b", 1 if data.hidden else 0))
    return bytes([TAG_COMPOUND]) + _utf("") + bytes(body) + b"\x00"


def _encode_entries(entries: list[ServerEntry]) -> bytes:
    out = bytearray(struct.pack(">bi", TAG_COMPOUND, len(entries)))
    for entry in entries:
        out += _named(TAG_STRING, "name", _utf(entry.name))
        out += _named(TAG_STRING, "ip", _utf(entry.ip))
        if entry.icon:
            out += _named(TAG_STRING, "icon", _utf(entry.icon))
        if entry.accept_textures is not None:
            out += _named(
                TAG_BYTE, "acceptTextures", struct.pack(">b", 1 if entry.accept_textures else 0)
            )
        for key, value in entry.extra.items():
            out += _encode_extra(key, value)
        out += b"\x00"  # end of this entry's compound
    return bytes(out)


def _named(tag: int, name: str, payload: bytes) -> bytes:
    return bytes([tag]) + _utf(name) + payload


def _utf(text: str) -> bytes:
    """A Java ``writeUTF`` string: an unsigned 16-bit length followed by UTF-8 bytes."""
    raw = str(text).encode("utf-8")
    if len(raw) > MAX_STRING_BYTES:
        raise ServersDatError(f"a string of {len(raw)} bytes does not fit in NBT")
    return struct.pack(">H", len(raw)) + raw


def _encode_extra(key: str, value: object) -> bytes:
    """One unknown key, written best-effort; a value this writer cannot type is dropped, not fatal."""
    try:
        encoded = _encode_value(value)
        if encoded is None:
            return b""
        tag, payload = encoded
        return _named(tag, key, payload)
    except (ServersDatError, struct.error, TypeError, ValueError) as exc:
        LOGGER.warning("servers.dat: dropping unsupported field %r: %s", key, exc)
        return b""


def _encode_value(value: object) -> tuple[int, bytes] | None:
    """The tag and payload of one Python value, or None for a value NBT cannot carry."""
    if isinstance(value, bool):
        return TAG_BYTE, struct.pack(">b", 1 if value else 0)
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return TAG_INT, struct.pack(">i", value)
        if -(2**63) <= value < 2**63:
            return TAG_LONG, struct.pack(">q", value)
        return None
    if isinstance(value, float):
        return TAG_DOUBLE, struct.pack(">d", value)
    if isinstance(value, str):
        return TAG_STRING, _utf(value)
    if isinstance(value, list):
        return _encode_list(value)
    if isinstance(value, dict):
        payload = bytearray()
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            encoded = _encode_value(item)
            if encoded is None:
                continue
            tag, inner = encoded
            payload += _named(tag, key, inner)
        payload += b"\x00"
        return TAG_COMPOUND, bytes(payload)
    return None


def _encode_list(values: list[Any]) -> tuple[int, bytes] | None:
    """An NBT list is homogeneous: encode it only when every element shares one tag type."""
    if not values:
        return TAG_LIST, struct.pack(">bi", TAG_END, 0)
    encoded_items = []
    element_tag: int | None = None
    for value in values:
        encoded = _encode_value(value)
        if encoded is None:
            return None
        tag, payload = encoded
        if element_tag is None:
            element_tag = tag
        elif tag != element_tag:
            return None
        encoded_items.append(payload)
    return (
        TAG_LIST,
        struct.pack(">bi", int(element_tag), len(encoded_items)) + b"".join(encoded_items),
    )


def write_servers(path: Path | str, data: ServersFile, *, backup: bool = True) -> Path | None:
    """Atomically write ``servers.dat``; returns the backup path when one was made.

    The temporary file lives in the target's folder so :func:`os.replace` is a same-filesystem rename
    (atomic on Windows and POSIX). The backup is written *before* the replace: if copying fails the
    original stays untouched and the caller gets a :class:`ServersDatError`.
    """
    path = Path(path)
    payload = encode_servers(data)
    temporary = path.with_name(path.name + TEMP_SUFFIX)
    saved_backup: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if backup and path.exists():
            saved_backup = backup_path(path)
            shutil.copy2(path, saved_backup)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # the original error is the one worth reporting
            LOGGER.debug("servers.dat: could not remove %s", temporary, exc_info=True)
        raise ServersDatError(str(exc)) from exc
    return saved_backup


def move_entry(entries: list[ServerEntry], index: int, delta: int) -> list[ServerEntry]:
    """A new list with the entry at ``index`` moved by ``delta``; out-of-range moves change nothing."""
    items = list(entries)
    target = index + delta
    if 0 <= index < len(items) and 0 <= target < len(items):
        items[index], items[target] = items[target], items[index]
    return items
