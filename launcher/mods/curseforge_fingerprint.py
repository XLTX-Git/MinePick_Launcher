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

"""The fingerprint CurseForge uses to recognise a file without trusting its name.

CurseForge answers ``POST /v1/fingerprints`` with the file a jar came from, which is the only identity a
user-renamed file keeps - the same reason the Modrinth path hashes. The algorithm is MurmurHash2 over
the file with whitespace bytes removed, seed 1, and one extra mixing step for the tail; it was read out
of Prism Launcher's ``libraries/murmur2`` and is validated against CurseForge's own ``fileFingerprint``
in the tests, because a fingerprint that is *almost* right never matches and never explains why.

Kept in its own module: it is a fixed algorithm with a fixed test vector, not something the downloader
or the metadata client should own.
"""

from __future__ import annotations

from pathlib import Path

_M = 0x5BD1E995
_R = 24
_MASK = 0xFFFFFFFF
#: The bytes CurseForge's hasher drops before mixing.
_WHITESPACE = frozenset({9, 10, 13, 32})
_CHUNK = 4 * 1024 * 1024


def murmur2_filtered(path: Path) -> int:
    """The CurseForge fingerprint of one file: MurmurHash2 over the bytes minus whitespace.

    Seed is fixed at 1 and depends on the filtered length, so the length has to be known before the
    mixing starts - CurseForge reads the file twice for the same reason. The tail is folded in with the
    original algorithm's final-mix branch, then avalanched.
    """
    kept = bytearray()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            kept.extend(byte for byte in chunk if byte not in _WHITESPACE)

    size = len(kept)
    h = (1 ^ size) & _MASK
    offset = 0
    remaining = size
    while remaining >= 4:
        k = int.from_bytes(kept[offset : offset + 4], "little")
        k = (k * _M) & _MASK
        k ^= k >> _R
        k = (k * _M) & _MASK
        h = (h * _M) & _MASK
        h ^= k
        offset += 4
        remaining -= 4

    for index in range(remaining):
        h ^= kept[offset + index] << (8 * index)
    if remaining:
        h = (h * _M) & _MASK

    h ^= h >> 13
    h = (h * _M) & _MASK
    h ^= h >> 15
    return h & _MASK
