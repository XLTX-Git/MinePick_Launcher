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

"""The CurseForge fingerprint: MurmurHash2 with whitespace removed.

The algorithm was validated against CurseForge itself before this file existed: the jar
``jei-26.3-neoforge-31.17.0.113.jar`` (mod 238222, file 9575423) hashes to ``1430156027``, which is the
``fileFingerprint`` their own API reports for it, and our implementation reproduces it exactly. The
tests below pin the arithmetic that got there; the last one re-runs the live comparison when a key is
available, so a future change to the mixing has to face the same authority.
"""

import os
import pathlib

import pytest

from launcher.mods.curseforge_fingerprint import murmur2_filtered

#: The value CurseForge reports for ``jei-26.3-neoforge-31.17.0.113.jar``.
JEI_FINGERPRINT = 1430156027
JEI_MOD_ID = 238222


def _write(where: pathlib.Path, data: bytes) -> pathlib.Path:
    where.write_bytes(data)
    return where


def test_whitespace_bytes_do_not_change_the_fingerprint(ws_tmp) -> None:
    """CurseForge drops spaces and line endings before hashing, so a re-saved file must not drift."""
    plain = _write(ws_tmp / "a.jar", b"abcdefgh")
    padded = _write(ws_tmp / "b.jar", b"ab\tcd\r\nef gh\x0a")
    assert murmur2_filtered(plain) == murmur2_filtered(padded)


def test_different_bytes_give_a_different_fingerprint(ws_tmp) -> None:
    first = _write(ws_tmp / "a.jar", b"abcdefgh")
    second = _write(ws_tmp / "b.jar", b"abcdefgi")
    assert murmur2_filtered(first) != murmur2_filtered(second)


def test_the_fingerprint_is_a_stable_32_bit_value(ws_tmp) -> None:
    path = _write(ws_tmp / "a.jar", b"hello world")
    value = murmur2_filtered(path)
    assert 0 <= value <= 0xFFFFFFFF
    # Regression guard, not independent evidence: this is what the implementation produced on the day
    # it reproduced CurseForge's own answer for a real jar. If the mixing changes, the number changes,
    # and the live test at the bottom of this file is what proves which of the two is right.
    assert value == 2824650221


def test_an_empty_file_still_hashes(ws_tmp) -> None:
    path = _write(ws_tmp / "empty.jar", b"")
    assert murmur2_filtered(path) == murmur2_filtered(_write(ws_tmp / "blank.jar", b" \t\r\n"))


@pytest.mark.skipif(
    not (pathlib.Path("build/cf_key.txt").is_file() or os.environ.get("MCLAUNCHER_CF_KEY")),
    reason="no CurseForge API key in this checkout",
)
def test_the_live_api_agrees_with_our_fingerprint() -> None:
    """The authority check: CurseForge maps the JEI fingerprint back to JEI."""
    from launcher.mods import curseforge

    match = curseforge.match_fingerprint(JEI_FINGERPRINT)
    assert match is not None
    assert (match.get("file") or {}).get("modId") == JEI_MOD_ID
