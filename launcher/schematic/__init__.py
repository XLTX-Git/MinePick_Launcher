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

"""Litematica schematic browsing: a bounded NBT header reader plus the folder scan.

``launcher.schematic.nbt`` is the format layer (tags, gzip, byte budget), ``litematic`` turns a
document into the facts the 投影 card shows, and ``scan`` finds the files of one instance. Everything
is standard library only and free of Qt, so it runs on a worker thread.
"""

from launcher.schematic.litematic import (
    HEADER_BYTES,
    SUFFIX,
    Region,
    SchematicError,
    SchematicInfo,
    parse_schematic,
    read_schematic,
)
from launcher.schematic.nbt import ArrayValue, NBTDocument, NBTError, NBTFormatError, read_nbt
from launcher.schematic.scan import (
    PRIMARY_SUBDIR,
    SCHEMATIC_SUBDIRS,
    SchematicFile,
    scan_schematics,
    schematic_dirs,
    schematic_folder,
)

__all__ = [
    "HEADER_BYTES",
    "PRIMARY_SUBDIR",
    "SCHEMATIC_SUBDIRS",
    "SUFFIX",
    "ArrayValue",
    "NBTDocument",
    "NBTError",
    "NBTFormatError",
    "Region",
    "SchematicError",
    "SchematicFile",
    "SchematicInfo",
    "parse_schematic",
    "read_nbt",
    "read_schematic",
    "scan_schematics",
    "schematic_dirs",
    "schematic_folder",
]
