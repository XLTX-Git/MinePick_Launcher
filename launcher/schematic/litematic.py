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

"""Read the header of a Litematica ``.litematic`` schematic.

The format is a gzip-compressed NBT compound shaped like this (only the fields used here)::

    {
      Metadata: { Name, Author, Description, TimeCreated, TimeModified,
                  TotalBlocks, TotalVolume, EnclosingSize {x,y,z}, RegionCount },
      MinecraftDataVersion: int,
      Version: int,
      Regions: {
        "<region>": {
          Position: {x,y,z}, Size: {x,y,z},
          BlockStatePalette: [{Name, Properties}, ...],
          BlockStates: [long, ...]          # skipped, see launcher.schematic.nbt
        }, ...
      }
    }

Nothing here touches ``BlockStates``. The bounding box comes from ``Metadata.EnclosingSize`` when the
file has one and is otherwise computed from the region boxes; the volume is the sum of the region
volumes, which is what "how big is this schematic" means for a multi-region file (the enclosing box
also contains the empty space between regions).

Only the header is read, so a partially decoded document is a normal outcome: ``truncated`` says the
byte budget or a cancel stopped the walk, and every field that was complete is still filled in.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from launcher.schematic.nbt import (
    DEFAULT_MAX_BYTES,
    NBTDocument,
    NBTError,
    integer,
    open_nbt_source,
    read_nbt,
)

#: The extension Litematica writes. Compared case-insensitively so ``.LITEMATIC`` is accepted too.
SUFFIX = ".litematic"
#: Decompressed bytes one header read may consume. Sixteen million blocks of block data are skipped
#: by the reader, but a file with many large regions can still pass this; 4 MiB of header is already
#: far more than any real palette needs, and stopping there keeps the background scan short.
HEADER_BYTES = DEFAULT_MAX_BYTES


class SchematicError(Exception):
    """The file is not a readable Litematica schematic (or it is damaged beyond use)."""


@dataclass(frozen=True)
class Region:
    """One named region of a schematic: its box, where it sits, and how many states it uses."""

    name: str
    size: tuple[int, int, int]
    position: tuple[int, int, int]
    palette_size: int | None

    @property
    def volume(self) -> int:
        return self.size[0] * self.size[1] * self.size[2]


@dataclass(frozen=True)
class SchematicInfo:
    """What the 投影 card shows about one file. Every field is optional in the format."""

    name: str = ""
    author: str = ""
    description: str = ""
    #: Epoch milliseconds, as Litematica stores them.
    created_ms: int | None = None
    modified_ms: int | None = None
    data_version: int | None = None
    #: ``Metadata.TotalBlocks``: Litematica's own count, which excludes air. None when absent.
    total_blocks: int | None = None
    #: ``Metadata.EnclosingSize``, the box the mod itself reports.
    enclosing: tuple[int, int, int] | None = None
    regions: tuple[Region, ...] = ()
    #: Distinct block states over all regions; None when a palette was cut off by the budget.
    block_kinds: int | None = 0
    #: True when the byte budget or a cancel stopped the read before the end of the document.
    truncated: bool = False

    @property
    def dimensions(self) -> tuple[int, int, int]:
        """The box to show: the mod's own, else the union of the region boxes."""
        if self.enclosing is not None:
            return self.enclosing
        return _union_size(self.regions)

    @property
    def volume(self) -> int:
        """Total volume: the sum of the region volumes, not the volume of the enclosing box."""
        return sum(region.volume for region in self.regions)

    @property
    def region_count(self) -> int:
        return len(self.regions)


def _vector(value: object) -> tuple[int, int, int] | None:
    """An NBT ``{x, y, z}`` compound as a tuple, or None when it is not one."""
    if not isinstance(value, dict):
        return None
    parts: list[int] = []
    for axis in ("x", "y", "z"):
        number = integer(value.get(axis))
        if number is None:
            return None
        parts.append(number)
    return parts[0], parts[1], parts[2]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _state_key(entry: object) -> str:
    """One palette entry as a comparable key: the block name plus its sorted properties.

    Two entries that differ only in a property (a stair's facing, a slab's half) are two different
    block states, which is what the palette counts, so the properties belong in the key.
    """
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    name = _text(entry.get("Name"))
    if not name:
        return ""
    properties = entry.get("Properties")
    if not isinstance(properties, dict):
        return name
    pairs = sorted(
        f"{key}={value}"
        for key, value in properties.items()
        if isinstance(key, str) and isinstance(value, str)
    )
    return name if not pairs else name + "[" + ",".join(pairs) + "]"


def _union_size(regions: tuple[Region, ...]) -> tuple[int, int, int]:
    if not regions:
        return 0, 0, 0
    low = [min(region.position[axis] for region in regions) for axis in range(3)]
    high = [
        max(region.position[axis] + region.size[axis] for region in regions) for axis in range(3)
    ]
    return high[0] - low[0], high[1] - low[1], high[2] - low[2]


def _regions_from(document: NBTDocument) -> tuple[tuple[Region, ...], int | None]:
    """Read every complete region; the palette sizes and the union of their block states."""
    raw = document.root.get("Regions")
    if not isinstance(raw, dict):
        return (), 0
    regions: list[Region] = []
    states: set[str] = set()
    complete = True
    for region_name, value in raw.items():
        if not isinstance(region_name, str) or not isinstance(value, dict):
            continue
        size = _vector(value.get("Size"))
        if size is None:
            # A region that was cut off before its Size is not usable; its siblings still are.
            continue
        position = _vector(value.get("Position")) or (0, 0, 0)
        palette = value.get("BlockStatePalette")
        palette_size: int | None = None
        if isinstance(palette, list):
            palette_size = len(palette)
            for entry in palette:
                key = _state_key(entry)
                if key:
                    states.add(key)
        else:
            complete = False
        regions.append(
            Region(name=region_name, size=size, position=position, palette_size=palette_size)
        )
    return tuple(regions), (len(states) if complete else None)


def parse_schematic(
    raw: BinaryIO,
    *,
    max_bytes: int = HEADER_BYTES,
    stop: threading.Event | None = None,
) -> SchematicInfo:
    """Decode a schematic header from an NBT stream (gzip decompression is the caller's job)."""
    try:
        document = read_nbt(raw, max_bytes=max_bytes, stop=stop)
    except NBTError as exc:
        raise SchematicError(str(exc)) from exc
    if not isinstance(document.root, dict):
        raise SchematicError("the file is not an NBT compound")
    metadata = document.root.get("Metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    regions, block_kinds = _regions_from(document)
    if not regions:
        # A file with nothing to place is not a schematic the card can show: a damaged download, an
        # unrelated NBT file, or a read that stopped before the first region's box was complete.
        raise SchematicError("the file has no readable region")
    info = SchematicInfo(
        name=_text(metadata.get("Name")),
        author=_text(metadata.get("Author")),
        description=_text(metadata.get("Description")),
        created_ms=integer(metadata.get("TimeCreated")),
        modified_ms=integer(metadata.get("TimeModified")),
        data_version=integer(document.root.get("MinecraftDataVersion")),
        total_blocks=integer(metadata.get("TotalBlocks")),
        enclosing=_vector(metadata.get("EnclosingSize")),
        regions=regions,
        block_kinds=block_kinds,
        truncated=document.truncated or document.cancelled,
    )
    return info


def read_schematic(
    path: Path | str,
    *,
    max_bytes: int = HEADER_BYTES,
    stop: threading.Event | None = None,
) -> SchematicInfo:
    """Read one ``.litematic`` file's header. Raises :class:`SchematicError` when unreadable."""
    try:
        with open_nbt_source(path) as raw:
            return parse_schematic(raw, max_bytes=max_bytes, stop=stop)
    except SchematicError:
        raise
    except OSError as exc:
        raise SchematicError(str(exc) or exc.__class__.__name__) from exc
