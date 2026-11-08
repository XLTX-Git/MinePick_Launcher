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

"""A minimal, read-only NBT reader built on the standard library only.

A Litematica ``.litematic`` file is a gzip-compressed NBT compound. The part the launcher shows -
the metadata, the Minecraft data version and, per region, the box and the block-state palette - is a
few kilobytes at the front of the file; the block data that follows (``BlockStates``, one packed long
per group of cells) can be hundreds of megabytes. This reader walks the tag stream and never
materialises a primitive array: an array becomes an :class:`ArrayValue` that carries its tag and its
length, and the payload is skipped in fixed-size chunks, so memory stays proportional to the tag
structure instead of to the file.

Two independent limits bound the walk:

* ``max_bytes`` counts *decompressed* bytes actually consumed. A file whose header does not fit inside
  the budget stops at the next tag boundary and returns the tags decoded so far (``truncated``), which
  is why a 300 MB schematic still answers in milliseconds.
* ``stop`` is a :class:`threading.Event` the caller sets to abort; the same tag-boundary rule applies
  (``cancelled``). This is the interrupt the UI uses when the page is torn down.

Only reading is implemented: nothing in this project writes NBT.

The public surface is :func:`read_nbt`, :func:`open_nbt_source` and the exceptions below. The tag
constants are exported because the schema reader (``launcher.schematic.litematic``) names them.
"""

from __future__ import annotations

import gzip
import struct
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

TAG_NAMES: dict[int, str] = {
    TAG_END: "end",
    TAG_BYTE: "byte",
    TAG_SHORT: "short",
    TAG_INT: "int",
    TAG_LONG: "long",
    TAG_FLOAT: "float",
    TAG_DOUBLE: "double",
    TAG_BYTE_ARRAY: "byte array",
    TAG_STRING: "string",
    TAG_LIST: "list",
    TAG_COMPOUND: "compound",
    TAG_INT_ARRAY: "int array",
    TAG_LONG_ARRAY: "long array",
}

#: Default decompressed-byte budget. Real headers are far below this: the metadata is a few hundred
#: bytes and each region adds a palette of usually a few thousand entries. The cap exists for files
#: that are genuinely huge, where reading every region's block array would cost hundreds of megabytes
#: of decompression for no extra information on screen.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
#: How much is read (and thrown away) per step when skipping an array payload. Small enough that
#: interrupting a 100 MB array stays responsive, large enough that the syscall count stays low.
SKIP_CHUNK = 64 * 1024
#: NBT is a recursive format; a corrupt file must not be able to exhaust the interpreter stack.
MAX_DEPTH = 64
#: A tag name or string longer than this is a corruption signal, not a name.
MAX_STRING_BYTES = 1 << 20
#: Upper bound on a list payload. Palettes are lists of compounds; a million-entry palette is not a
#: schematic, and materialising it would defeat the point of the bounded read.
MAX_LIST_ITEMS = 200_000


class NBTError(Exception):
    """Base class: the bytes are not readable NBT."""


class NBTFormatError(NBTError):
    """The tag stream is malformed (wrong tag, truncated body, implausible length)."""


class _Abort(Exception):
    """Internal control flow for the two bounded stops, never escapes this module."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ArrayValue:
    """A primitive array whose payload was skipped: only its tag and element count are kept."""

    tag: int
    length: int


@dataclass(frozen=True)
class NBTDocument:
    """One decoded NBT document, possibly incomplete when a limit stopped the walk."""

    name: str
    root: dict[str, object]
    bytes_read: int
    truncated: bool = False
    cancelled: bool = False


def integer(value: object) -> int | None:
    """An NBT integer, or None for anything else (``bool`` is not an NBT integer here)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class _Reader:
    """The tag walker. One instance reads one document; it is not reusable."""

    def __init__(
        self,
        raw: BinaryIO,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        stop: threading.Event | None = None,
    ) -> None:
        self.raw = raw
        self.max_bytes = max(0, int(max_bytes))
        self.stop = stop
        self.used = 0

    # ---------- bounded byte input ----------

    def _before_read(self, size: int) -> None:
        if self.stop is not None and self.stop.is_set():
            raise _Abort("cancel")
        if self.used + size > self.max_bytes:
            raise _Abort("budget")

    def read(self, size: int) -> bytes:
        """Read exactly ``size`` bytes, or fail: NBT has no optional fields."""
        if size <= 0:
            return b""
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            step = min(remaining, SKIP_CHUNK)
            self._before_read(step)
            chunk = self.raw.read(step)
            if not chunk:
                raise NBTFormatError(f"the tag stream ended {remaining} byte(s) early")
            self.used += len(chunk)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def skip(self, size: int) -> None:
        """Discard ``size`` bytes without ever holding more than one chunk of them."""
        while size > 0:
            step = min(size, SKIP_CHUNK)
            self._before_read(step)
            chunk = self.raw.read(step)
            if not chunk:
                raise NBTFormatError(f"the tag stream ended {size} byte(s) early")
            self.used += len(chunk)
            size -= len(chunk)

    # ---------- primitives ----------

    def u8(self) -> int:
        return self.read(1)[0]

    def i8(self) -> int:
        return struct.unpack(">b", self.read(1))[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack(">f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack(">d", self.read(8))[0]

    def name(self) -> str:
        """A tag name or string: an unsigned 16-bit byte count, then modified UTF-8."""
        size = self.u16()
        if size > MAX_STRING_BYTES:
            raise NBTFormatError(f"a string of {size} bytes is not a plausible NBT string")
        return self.read(size).decode("utf-8", "replace")

    # ---------- tags ----------

    def document(self) -> NBTDocument:
        """Decode the root compound, returning whatever survived a bounded stop."""
        root: dict[str, object] = {}
        name = ""
        truncated = False
        cancelled = False
        try:
            tag = self.u8()
            if tag != TAG_COMPOUND:
                raise NBTFormatError(
                    f"the root tag is {TAG_NAMES.get(tag, tag)!r}, not a compound"
                )
            name = self.name()
            self._compound_body(root, 0)
        except _Abort as abort:
            truncated = abort.reason == "budget"
            cancelled = abort.reason == "cancel"
        return NBTDocument(
            name=name,
            root=root,
            bytes_read=self.used,
            truncated=truncated,
            cancelled=cancelled,
        )

    def _compound_body(self, node: dict[str, object], depth: int) -> None:
        """Fill ``node`` in place so an interrupted walk keeps every finished sibling.

        Compounds are attached to their parent *before* their body is read (see the caller), which is
        what makes a partial document useful: the metadata in front of a huge region survives even
        when the region's block array is where the budget runs out.
        """
        while True:
            tag = self.u8()
            if tag == TAG_END:
                return
            if tag not in TAG_NAMES:
                raise NBTFormatError(f"unknown tag type {tag}")
            key = self.name()
            if tag == TAG_COMPOUND:
                child: dict[str, object] = {}
                node[key] = child
                self._compound_body(child, depth + 1)
            elif tag == TAG_LIST:
                # A list is only attached once complete: a half-read palette must not look like a
                # shorter palette, which would undercount the block types the file contains.
                node[key] = self._list(depth + 1)
            else:
                node[key] = self._payload(tag, depth + 1)

    def _list(self, depth: int) -> list[object]:
        element = self.u8()
        length = max(self.i32(), 0)
        if length > MAX_LIST_ITEMS:
            raise NBTFormatError(f"a list of {length} entries is not a plausible NBT list")
        if element == TAG_END:
            if length:
                raise NBTFormatError(f"a list claims {length} entries of type 'end'")
            return []
        if element not in TAG_NAMES:
            raise NBTFormatError(f"unknown list element type {element}")
        items: list[object] = []
        for _ in range(length):
            items.append(self._payload(element, depth + 1))
        return items

    def _payload(self, tag: int, depth: int) -> object:
        if depth > MAX_DEPTH:
            raise NBTFormatError(f"NBT nesting deeper than {MAX_DEPTH} levels")
        if tag == TAG_BYTE:
            return self.i8()
        if tag == TAG_SHORT:
            return struct.unpack(">h", self.read(2))[0]
        if tag == TAG_INT:
            return self.i32()
        if tag == TAG_LONG:
            return self.i64()
        if tag == TAG_FLOAT:
            return self.f32()
        if tag == TAG_DOUBLE:
            return self.f64()
        if tag == TAG_STRING:
            return self.name()
        if tag == TAG_BYTE_ARRAY:
            return self._array(tag, 1)
        if tag == TAG_INT_ARRAY:
            return self._array(tag, 4)
        if tag == TAG_LONG_ARRAY:
            return self._array(tag, 8)
        if tag == TAG_COMPOUND:
            node: dict[str, object] = {}
            self._compound_body(node, depth + 1)
            return node
        if tag == TAG_LIST:
            return self._list(depth + 1)
        raise NBTFormatError(f"unknown tag type {tag}")

    def _array(self, tag: int, width: int) -> ArrayValue:
        length = max(self.i32(), 0)
        self.skip(length * width)
        return ArrayValue(tag=tag, length=length)


def read_nbt(
    raw: BinaryIO,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    stop: threading.Event | None = None,
) -> NBTDocument:
    """Decode one NBT document from ``raw``, bounded by ``max_bytes`` and ``stop``.

    A malformed stream raises :class:`NBTFormatError`; a stream that is merely larger than the budget
    returns the tags decoded so far with ``truncated=True``. Compressed-data failures (a broken gzip
    member) are reported as :class:`NBTFormatError` too, because to the caller they mean the same
    thing: this file cannot be read.
    """
    reader = _Reader(raw, max_bytes=max_bytes, stop=stop)
    try:
        return reader.document()
    except (EOFError, zlib.error) as exc:
        raise NBTFormatError(f"the compressed tag stream is broken: {exc}") from exc


@contextmanager
def open_nbt_source(path: Path | str) -> Iterator[BinaryIO]:
    """Open ``path`` as an NBT byte stream, transparently through gzip when it is compressed.

    Litematica always writes gzip. A few third-party exporters write the same payload uncompressed,
    and the two-byte magic is the whole difference, so both are accepted: sniffing two bytes is much
    cheaper than a failed first read, and it keeps one reader for the format instead of two.
    """
    with open(path, "rb") as handle:
        magic = handle.read(2)
        handle.seek(0)
        if magic == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=handle, mode="rb") as compressed:
                yield compressed
        else:
            yield handle
