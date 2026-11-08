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
"""Instance names for imported packs: the pack's name without its version suffix.

A pack is called "Fabulously Optimized 6.4.1" and the instance should be called
"Fabulously Optimized": the version is the pack build, not the instance's name, and appending it to
the folder name would make every update produce a second instance. The result still has to satisfy
:func:`launcher.instances.validate_name` (1-32 characters, no path separators), so the candidate is
sanitised and truncated, and a folder that already exists gets ``-2``, ``-3`` ...
"""

from __future__ import annotations

import re
from pathlib import Path

from launcher.instances import instance_dir

#: A trailing version token: "6.4.1", "v1.20.1", "1.20.1-forge", "1.19.2+build.3".
_VERSION_TAIL = re.compile(
    r"[\s\-_·]*[vV]?\d+(?:[.\-_]\d+)*(?:[-+~][0-9A-Za-z][0-9A-Za-z._+\-]*)?\s*$"
)
#: A trailing bracket group such as "[FORGE]" or "(1.20.1)".
_BRACKET_TAIL = re.compile(r"[\(\[\{][^\)\]\}]{0,48}[\)\]\}]\s*$")
#: Everything :func:`launcher.instances.validate_name` would refuse.
_INVALID_CHARS = re.compile(r"[^\w\u4e00-\u9fff ._-]+")
_WHITESPACE = re.compile(r"\s+")

MAX_LENGTH = 32


def strip_version_suffix(name: str) -> str:
    """The pack name with a trailing version (and a trailing bracket group) removed.

    Repeats, because both may be present ("Better MC [FORGE] 1.20.1"). A name that is *only* a
    version keeps itself: an instance called "1.20.1" is better than one called "modpack".
    """
    original = str(name or "").strip()
    text = original
    for _ in range(3):
        before = text
        text = _BRACKET_TAIL.sub("", text).strip(" -_·")
        text = _VERSION_TAIL.sub("", text).strip(" -_·")
        if text == before:
            break
    return text or original


def sanitize_instance_name(name: str) -> str:
    """Make ``name`` acceptable as an instance folder id ('' when nothing is left)."""
    text = _INVALID_CHARS.sub(" ", str(name or ""))
    text = _WHITESPACE.sub(" ", text).strip(" ._-")
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH].rstrip(" ._-")
    return text


def _with_suffix(base: str, index: int) -> str:
    suffix = f"-{index}"
    return base[: MAX_LENGTH - len(suffix)].rstrip(" ._-") + suffix


def unique_instance_name(proposed: str, game_dir: Path, *, fallback: str = "") -> str:
    """A folder id that no instance in ``game_dir`` uses yet."""
    base = sanitize_instance_name(proposed) or sanitize_instance_name(fallback) or "modpack"
    name = base
    index = 2
    while instance_dir(Path(game_dir), name).exists():
        name = _with_suffix(base, index)
        index += 1
    return name
