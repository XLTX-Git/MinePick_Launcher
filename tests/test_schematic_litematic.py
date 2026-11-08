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

"""The schematic header reader, driven by ``.litematic`` files this test writes itself.

Every fixture is real gzip-compressed NBT built by the small writer below, so the tests describe the
format instead of mocking the parser: metadata and region boxes are asserted against bytes the test
chose, the block arrays are real long arrays, and the "huge file" case is a genuinely large payload
whose tail the reader must never ask for.
"""

from __future__ import annotations

import gzip
import os
import struct
import threading
import time
from pathlib import Path

import pytest

from launcher.schematic import (
    HEADER_BYTES,
    SUFFIX,
    SchematicError,
    read_nbt,
    read_schematic,
    scan_schematics,
    schematic_dirs,
    schematic_folder,
)
from launcher.schematic.nbt import (
    TAG_BYTE_ARRAY,
    TAG_COMPOUND,
    TAG_INT_ARRAY,
    TAG_LIST,
    TAG_LONG_ARRAY,
    TAG_STRING,
    ArrayValue,
    NBTFormatError,
)

# --------------------------------------------------------------------------------------------------
# A writer for exactly the NBT subset a .litematic uses (standard library only, like the reader).
# --------------------------------------------------------------------------------------------------


def _utf(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def tag(tag_id: int, name: str, payload: bytes) -> bytes:
    return bytes([tag_id]) + _utf(name) + payload


def compound(*entries: bytes) -> bytes:
    """One compound *payload*: its entries then the end tag that closes them."""
    return b"".join(entries) + b"\x00"


def document(*entries: bytes) -> bytes:
    """A complete NBT file: the root compound tag, an empty root name, then its payload."""
    return bytes([TAG_COMPOUND]) + _utf("") + compound(*entries)


def byte(value: int) -> bytes:
    return struct.pack(">b", value)


def integer(value: int) -> bytes:
    return struct.pack(">i", value)


def long(value: int) -> bytes:
    return struct.pack(">q", value)


def string(value: str) -> bytes:
    return _utf(value)


def vector(x: int, y: int, z: int) -> bytes:
    return compound(tag(3, "x", integer(x)), tag(3, "y", integer(y)), tag(3, "z", integer(z)))


def long_array(*values: int) -> bytes:
    return struct.pack(">i", len(values)) + b"".join(struct.pack(">q", value) for value in values)


def int_array(*values: int) -> bytes:
    return struct.pack(">i", len(values)) + b"".join(integer(value) for value in values)


def list_of(tag_id: int, *payloads: bytes) -> bytes:
    return bytes([tag_id]) + struct.pack(">i", len(payloads)) + b"".join(payloads)


def palette(states: list[tuple[str, dict[str, str]]]) -> bytes:
    entries = []
    for name, properties in states:
        parts = [tag(TAG_STRING, "Name", string(name))]
        if properties:
            parts.append(
                tag(
                    10,
                    "Properties",
                    compound(
                        *(tag(TAG_STRING, key, string(value)) for key, value in properties.items())
                    ),
                )
            )
        entries.append(compound(*parts))
    return list_of(TAG_COMPOUND, *entries)


def region(
    name: str,
    size: tuple[int, int, int],
    position: tuple[int, int, int] = (0, 0, 0),
    *,
    states: list[tuple[str, dict[str, str]]] | None = None,
    block_states: int = 8,
) -> bytes:
    states = states if states is not None else [("minecraft:stone", {})]
    return tag(
        TAG_COMPOUND,
        name,
        compound(
            tag(TAG_COMPOUND, "Position", vector(*position)),
            tag(TAG_COMPOUND, "Size", vector(*size)),
            tag(TAG_LIST, "BlockStatePalette", palette(states)),
            tag(TAG_LONG_ARRAY, "BlockStates", long_array(*([0] * block_states))),
            tag(TAG_INT_ARRAY, "TileEntities", int_array()),
        ),
    )


def litematic(
    *,
    name: str = "小屋",
    author: str = "Steve",
    description: str = "A test schematic",
    created: int = 1_700_000_000_000,
    modified: int = 1_700_000_100_000,
    total_blocks: int = 4096,
    enclosing: tuple[int, int, int] | None = (16, 16, 16),
    data_version: int = 3955,
    regions: list[bytes] | None = None,
) -> bytes:
    """The whole file payload: root compound, metadata, version, regions."""
    metadata = compound(
        tag(TAG_STRING, "Name", string(name)),
        tag(TAG_STRING, "Author", string(author)),
        tag(TAG_STRING, "Description", string(description)),
        tag(4, "TimeCreated", long(created)),
        tag(4, "TimeModified", long(modified)),
        tag(4, "TotalBlocks", long(total_blocks)),
        tag(4, "TotalVolume", long(4096)),
        *([tag(10, "EnclosingSize", vector(*enclosing))] if enclosing is not None else []),
        tag(3, "RegionCount", integer(1)),
    )
    regions = regions if regions is not None else [region("main", (16, 16, 16))]
    return document(
        tag(TAG_COMPOUND, "Metadata", metadata),
        tag(3, "MinecraftDataVersion", integer(data_version)),
        tag(3, "Version", integer(6)),
        tag(TAG_COMPOUND, "Regions", compound(*regions)),
    )


def write_litematic(path: Path, payload: bytes, *, compress: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(payload, 6) if compress else payload
    path.write_bytes(data)


class _CountingStream:
    """A byte source that records how much of it the reader asked for.

    The parser's own ``bytes_read`` is a claim about itself; this wrapper is the independent witness
    for the "a huge file is only read at the front" test.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0
        self.read_bytes = 0

    def read(self, size: int) -> bytes:
        chunk = self.data[self.position : self.position + size]
        self.position += len(chunk)
        self.read_bytes += len(chunk)
        return chunk


# --------------------------------------------------------------------------------------------------
# The NBT layer
# --------------------------------------------------------------------------------------------------


def test_the_reader_decodes_the_primitives_and_the_node_types() -> None:
    payload = document(
        tag(TAG_STRING, "hello", string("世界")),
        tag(3, "answer", integer(42)),
        tag(4, "big", long(2**40)),
        tag(TAG_BYTE_ARRAY, "bytes", struct.pack(">i", 3) + b"\x01\x02\x03"),
        tag(TAG_INT_ARRAY, "ints", int_array(1, 2, 3)),
        tag(TAG_LONG_ARRAY, "longs", long_array(1 << 40, -(1 << 40))),
        tag(TAG_LIST, "names", list_of(TAG_STRING, string("a"), string("b"))),
        tag(TAG_COMPOUND, "nested", compound(tag(1, "flag", byte(1)))),
    )
    document_value = read_nbt(_CountingStream(payload))
    assert document_value.truncated is False
    root = document_value.root
    assert root["hello"] == "世界"
    assert root["answer"] == 42
    assert root["big"] == 2**40
    assert root["bytes"] == ArrayValue(TAG_BYTE_ARRAY, 3)
    assert root["ints"] == ArrayValue(TAG_INT_ARRAY, 3)
    assert root["longs"] == ArrayValue(TAG_LONG_ARRAY, 2)
    assert root["names"] == ["a", "b"]
    assert root["nested"] == {"flag": 1}


def test_an_array_is_counted_but_never_materialised() -> None:
    payload = document(tag(TAG_LONG_ARRAY, "BlockStates", long_array(*([7] * 1000))))
    document_value = read_nbt(_CountingStream(payload))
    value = document_value.root["BlockStates"]
    assert isinstance(value, ArrayValue)
    assert value.length == 1000
    assert document_value.bytes_read == len(payload)


def test_a_truncated_tag_stream_is_an_error_not_a_crash() -> None:
    payload = document(tag(3, "answer", integer(42)))
    with pytest.raises(NBTFormatError):
        read_nbt(_CountingStream(payload[:-2]))


def test_a_root_that_is_not_a_compound_is_rejected() -> None:
    with pytest.raises(NBTFormatError, match="root tag"):
        read_nbt(_CountingStream(bytes([3]) + _utf("") + integer(1)))


def test_an_unknown_tag_type_is_rejected() -> None:
    with pytest.raises(NBTFormatError, match="unknown tag type"):
        read_nbt(_CountingStream(document(tag(99, "x", b""))))


def test_a_plausibility_guard_stops_an_absurd_list_length() -> None:
    payload = document(tag(TAG_LIST, "x", bytes([3]) + struct.pack(">i", 999_999_999)))
    with pytest.raises(NBTFormatError, match="plausible"):
        read_nbt(_CountingStream(payload))


# --------------------------------------------------------------------------------------------------
# The schematic header
# --------------------------------------------------------------------------------------------------


def test_metadata_is_read_from_a_compressed_file(ws_tmp) -> None:
    path = ws_tmp / "cottage.litematic"
    write_litematic(path, litematic())
    info = read_schematic(path)
    assert info.name == "小屋"
    assert info.author == "Steve"
    assert info.description == "A test schematic"
    assert info.created_ms == 1_700_000_000_000
    assert info.modified_ms == 1_700_000_100_000
    assert info.data_version == 3955
    assert info.total_blocks == 4096
    assert info.truncated is False


def test_an_uncompressed_payload_is_accepted_too(ws_tmp) -> None:
    path = ws_tmp / "plain.litematic"
    write_litematic(path, litematic(name="plain"), compress=False)
    assert read_schematic(path).name == "plain"


def test_the_dimensions_come_from_the_mods_own_enclosing_size(ws_tmp) -> None:
    path = ws_tmp / "house.litematic"
    write_litematic(
        path,
        litematic(
            enclosing=(128, 64, 96),
            regions=[region("main", (64, 64, 96)), region("porch", (32, 16, 16), (96, 0, 0))],
        ),
    )
    info = read_schematic(path)
    assert info.dimensions == (128, 64, 96)
    # Volume is the sum of the region boxes: 64*64*96 + 32*16*16, not 128*64*96.
    assert info.volume == 64 * 64 * 96 + 32 * 16 * 16
    assert info.region_count == 2


def test_without_an_enclosing_size_the_dimensions_are_the_union_of_the_regions(ws_tmp) -> None:
    path = ws_tmp / "two.litematic"
    write_litematic(
        path,
        litematic(
            enclosing=None,
            regions=[
                region("a", (16, 16, 16), (0, 0, 0)),
                region("b", (8, 4, 2), (24, 10, 30)),
            ],
        ),
    )
    info = read_schematic(path)
    assert info.dimensions == (32, 16, 32)
    assert info.regions[0].position == (0, 0, 0)
    assert info.volume == 16 * 16 * 16 + 8 * 4 * 2


def test_the_palette_count_unions_distinct_block_states_across_regions(ws_tmp) -> None:
    path = ws_tmp / "states.litematic"
    write_litematic(
        path,
        litematic(
            regions=[
                region(
                    "a",
                    (16, 16, 16),
                    states=[
                        ("minecraft:stone", {}),
                        ("minecraft:oak_stairs", {"facing": "north"}),
                        ("minecraft:oak_stairs", {"facing": "south"}),
                    ],
                ),
                region(
                    "b",
                    (4, 4, 4),
                    states=[
                        # stone again: one state, not two
                        ("minecraft:stone", {}),
                        ("minecraft:oak_stairs", {"facing": "north"}),
                        ("minecraft:glass", {}),
                    ],
                ),
            ],
        ),
    )
    info = read_schematic(path)
    assert [item.palette_size for item in info.regions] == [3, 3]
    assert info.block_kinds == 4  # stone, two stair facings, glass


def test_a_file_without_a_region_is_not_a_schematic(ws_tmp) -> None:
    path = ws_tmp / "empty.litematic"
    write_litematic(path, litematic(regions=[]))
    with pytest.raises(SchematicError, match="no readable region"):
        read_schematic(path)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not nbt at all",
        b"\x1f\x8b" + b"\x00" * 20,  # gzip magic, then garbage
        b"\x0a\x00\x00",  # an empty compound: no Metadata, no Regions
    ],
    ids=["empty", "text", "broken-gzip", "empty-compound"],
)
def test_a_damaged_file_raises_schematic_error(ws_tmp, data: bytes) -> None:
    path = ws_tmp / "broken.litematic"
    path.write_bytes(data)
    with pytest.raises(SchematicError):
        read_schematic(path)


def test_a_file_without_metadata_uses_its_stem_as_the_title(ws_tmp) -> None:
    path = ws_tmp / "schematics" / "nameless.litematic"
    payload = document(tag(TAG_COMPOUND, "Regions", compound(region("main", (2, 3, 4)))))
    write_litematic(path, payload)
    files = scan_schematics(ws_tmp)
    assert files[0].readable
    assert files[0].info.name == ""
    assert files[0].title == "nameless"


# --------------------------------------------------------------------------------------------------
# A huge file is read at the front only
# --------------------------------------------------------------------------------------------------


def test_a_huge_block_array_is_never_read_past_the_budget() -> None:
    """The block array is 12 MB; a 512 KiB budget must stop inside it and keep what came before."""
    count = 1_500_000
    payload = (
        bytes([TAG_COMPOUND])
        + _utf("")
        + tag(TAG_COMPOUND, "Metadata", compound(tag(TAG_STRING, "Name", string("巨构"))))
        + tag(
            TAG_COMPOUND,
            "Regions",
            tag(
                TAG_COMPOUND,
                "main",
                tag(TAG_COMPOUND, "Size", vector(256, 256, 256))
                + tag(TAG_LIST, "BlockStatePalette", palette([("minecraft:stone", {})]))
                + bytes([TAG_LONG_ARRAY])
                + _utf("BlockStates")
                + struct.pack(">i", count)
                + b"\x00" * (8 * count)
                + b"\x00",  # closes the region
            )
            + b"\x00",  # closes Regions
        )
        + b"\x00"  # closes the root
    )
    raw = _CountingStream(payload)
    budget = 512 * 1024
    document_value = read_nbt(raw, max_bytes=budget)
    assert document_value.truncated is True
    assert document_value.bytes_read <= budget
    # The witness: the reader asked the stream for at most the budget, not for the 12 MB payload.
    assert raw.read_bytes <= budget
    assert len(payload) > 10 * budget
    # What came before the cut is still there: the metadata name and the region box.
    assert document_value.root["Metadata"]["Name"] == "巨构"
    region_node = document_value.root["Regions"]["main"]
    assert region_node["Size"] == {"x": 256, "y": 256, "z": 256}


def test_a_file_larger_than_the_budget_reads_its_header_and_marks_the_cut(ws_tmp) -> None:
    """The same shape on disk: a schema-valid file whose block array the budget cannot pass."""
    count = 1_200_000
    # Random bytes, not zeros: the point is a file that really is large on disk, and zeros would
    # compress to a few kilobytes and prove nothing.
    block_bytes = os.urandom(8 * count)
    payload = (
        bytes([TAG_COMPOUND])
        + _utf("")
        + tag(TAG_COMPOUND, "Metadata", compound(tag(TAG_STRING, "Name", string("巨大投影"))))
        + tag(
            TAG_COMPOUND,
            "Regions",
            tag(
                TAG_COMPOUND,
                "main",
                tag(TAG_COMPOUND, "Size", vector(256, 256, 256))
                + tag(TAG_LIST, "BlockStatePalette", palette([("minecraft:stone", {})]))
                + bytes([TAG_LONG_ARRAY])
                + _utf("BlockStates")
                + struct.pack(">i", count)
                + block_bytes
                + b"\x00",
            )
            + b"\x00",
        )
        + b"\x00"
    )
    path = ws_tmp / "giant.litematic"
    write_litematic(path, payload)
    assert path.stat().st_size > 8 * 1024 * 1024
    info = read_schematic(path, max_bytes=512 * 1024)
    assert info.truncated is True
    assert info.name == "巨大投影"
    assert info.dimensions == (256, 256, 256)
    assert info.region_count == 1


# --------------------------------------------------------------------------------------------------
# The folder scan
# --------------------------------------------------------------------------------------------------


def test_all_litematica_folders_are_scanned_and_sorted_newest_first(ws_tmp) -> None:
    old = ws_tmp / "schematics" / "old.litematic"
    new = ws_tmp / "config" / "litematica" / "new.litematic"
    newest = ws_tmp / "config" / "litematica" / "schematics" / "newest.litematic"
    for index, path in enumerate((old, new, newest)):
        write_litematic(path, litematic(name=f"build-{index}"))
        stamp = time.time() - 3600 + index * 60
        os.utime(path, (stamp, stamp))
    dirs = schematic_dirs(ws_tmp)
    assert [folder.name for folder in dirs] == ["schematics", "litematica", "schematics"]
    files = scan_schematics(ws_tmp)
    assert [item.file_name for item in files] == [
        "newest.litematic",
        "new.litematic",
        "old.litematic",
    ]
    assert all(item.readable for item in files)
    assert files[0].size_bytes == newest.stat().st_size


def test_other_extensions_and_hidden_files_are_ignored(ws_tmp) -> None:
    folder = ws_tmp / "schematics"
    folder.mkdir()
    write_litematic(folder / "yes.LITEMATIC", litematic(name="upper"))
    (folder / "no.schem").write_bytes(b"nope")
    (folder / ".hidden.litematic").write_bytes(b"nope")
    (folder / "notes.txt").write_text("hello", encoding="utf-8")
    files = scan_schematics(ws_tmp)
    assert [item.file_name for item in files] == ["yes.LITEMATIC"]
    assert SUFFIX in files[0].file_name.lower()


def test_a_broken_file_becomes_a_row_with_an_error_instead_of_raising(ws_tmp) -> None:
    folder = ws_tmp / "schematics"
    folder.mkdir()
    (folder / "good.litematic").write_bytes(gzip.compress(litematic(name="good")))
    (folder / "bad.litematic").write_bytes(b"\x1f\x8b\x08\x00broken")
    files = scan_schematics(ws_tmp)
    by_name = {item.file_name: item for item in files}
    assert by_name["good.litematic"].readable
    assert by_name["bad.litematic"].readable is False
    assert by_name["bad.litematic"].error
    assert by_name["bad.litematic"].title == "bad"


def test_no_schematic_folder_at_all_yields_an_empty_scan(ws_tmp) -> None:
    assert schematic_dirs(ws_tmp) == []
    assert scan_schematics(ws_tmp) == []
    # The button still opens (and creates) the default folder, so it has a target without files.
    assert schematic_folder(ws_tmp) == ws_tmp / "schematics"


def test_the_folder_helper_prefers_a_folder_that_exists(ws_tmp) -> None:
    config = ws_tmp / "config" / "litematica"
    config.mkdir(parents=True)
    assert schematic_folder(ws_tmp) == config


def test_the_scan_stops_when_the_caller_asks_it_to(ws_tmp) -> None:
    folder = ws_tmp / "schematics"
    folder.mkdir()
    for index in range(3):
        (folder / f"{index}.litematic").write_bytes(gzip.compress(litematic(name=str(index))))
    stop = threading.Event()
    stop.set()
    assert scan_schematics(ws_tmp, stop=stop) == []


def test_the_header_budget_is_a_real_limit() -> None:
    assert HEADER_BYTES >= 1024 * 1024
    # A zero budget stops before the first byte: an empty, truncated document, not an exception.
    document_value = read_nbt(_CountingStream(document(tag(3, "x", integer(1)))), max_bytes=0)
    assert document_value.truncated is True
    assert document_value.bytes_read == 0
    assert document_value.root == {}
