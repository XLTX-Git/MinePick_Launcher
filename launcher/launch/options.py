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

"""Safe, single-option edits of ``options.txt``.

The game owns this file, so the launcher only touches one line at a time: an absent key is
appended, an existing key gets a new value while its separator style (``key: 1`` vs ``key:1``)
is kept, and every other line is left alone. Writes are atomic, the first write keeps one
``.bak`` copy next to the file, and no failure ever escapes to the launch path.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from enum import Enum
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


class OptionWriteResult(str, Enum):
    """What :func:`ensure_option` did (or why it did nothing)."""

    WRITTEN = "written"
    UNCHANGED = "unchanged"
    FAILED = "failed"


def _line_key_and_prefix(line: str) -> tuple[str, str] | None:
    """Return ``(key, prefix-through-the-separator)`` for an option line, or None."""
    colon = line.find(":")
    if colon < 0:
        return None
    key = line[:colon].strip()
    if not key:
        return None
    rest = line[colon + 1 :]
    # Keep the game's own spacing after the colon: "key: 1" stays "key: <new>".
    padding = rest[: len(rest) - len(rest.lstrip())]
    return key, line[: colon + 1] + padding


def _read_text(path: Path) -> tuple[bool, str]:
    """Return ``(existed, text)``; a missing file is a new file, not an error."""
    try:
        return True, path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return False, ""


def _backup_once(path: Path) -> None:
    """Keep the pre-launcher file once; an existing .bak is never overwritten."""
    backup = path.with_name(path.name + ".bak")
    if backup.exists():
        return
    shutil.copy2(path, backup)


def _atomic_write(path: Path, text: str) -> None:
    """Write UTF-8/LF without a BOM, then replace the target in one step."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def option_present(path: Path, key: str) -> bool:
    """Whether ``path`` already contains ``key`` (read-only; failures mean "not present").

    The launcher asks this before deciding what to do, so it must never be the thing that stops a
    launch: a missing file is an ordinary "no", and an unreadable or corrupt one is logged and
    treated the same way. Matching is exact and uses the same line parser as :func:`ensure_option`,
    so a key such as ``key`` cannot be confused with ``key_extra``.
    """
    try:
        existed, text = _read_text(Path(path))
    except (OSError, UnicodeError) as exc:
        _LOGGER.warning("could not read options file %s: %s", path, exc)
        return False
    if not existed:
        return False
    for line in text.splitlines():
        parsed = _line_key_and_prefix(line)
        if parsed is not None and parsed[0] == key:
            return True
    return False


def ensure_option(path: Path, key: str, value: str) -> OptionWriteResult:
    """Ensure exactly one ``key:value`` line, without disturbing the rest of the file.

    Semantics:
    * missing key -> append one line;
    * existing key -> replace only those lines, preserving ``key:`` and its spacing;
    * same value already there -> :attr:`OptionWriteResult.UNCHANGED` (no backup, no write);
    * first real write -> ``options.txt.bak`` once (never overwritten);
    * encoding -> read ``utf-8-sig``, write ``utf-8`` (no BOM) with LF line endings;
    * atomic replacement via a temp file + :func:`os.replace`;
    * every failure -> :attr:`OptionWriteResult.FAILED`, logged, never raised.

    An existing file's trailing-newline state is preserved (a file without a final newline
    stays that way); a newly created file ends with one LF. New keys use ``key:value``.
    """
    path = Path(path)
    try:
        if not key or any(char in key for char in ":\r\n"):
            raise ValueError(f"invalid options key: {key!r}")
        if any(char in value for char in "\r\n"):
            raise ValueError("option values may not contain newlines")

        existed, original = _read_text(path)
        lines = original.splitlines()
        rendered = f"{key}:{value}"
        out: list[str] = []
        found = False
        changed = False
        for line in lines:
            parsed = _line_key_and_prefix(line)
            if parsed is None or parsed[0] != key:
                out.append(line)
                continue
            found = True
            new_line = parsed[1] + str(value)
            out.append(new_line)
            changed = changed or new_line != line
        if not found:
            out.append(rendered)
            changed = True

        if not changed:
            return OptionWriteResult.UNCHANGED

        # A read-only options.txt is a user decision (or a lock): report it instead of
        # spending a backup and failing halfway through the write.
        if existed:
            mode = path.stat().st_mode
            if not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                _LOGGER.warning("options.txt is read-only; leaving it untouched: %s", path)
                return OptionWriteResult.FAILED

        path.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            _backup_once(path)
        # Keep the source's trailing-newline style; brand-new files end with LF.
        newline_at_end = original.endswith(("\n", "\r")) if existed and original else True
        new_text = "\n".join(out) + ("\n" if newline_at_end else "")
        _atomic_write(path, new_text)
    except Exception as exc:  # noqa: BLE001 - an options edit must never break a launch
        _LOGGER.warning("could not update %s (%s): %s", path, key, exc)
        return OptionWriteResult.FAILED
    return OptionWriteResult.WRITTEN
