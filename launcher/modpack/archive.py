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
"""Archive access for modpack import, behind the launcher's one path guard.

A pack is a zip someone else built: every member name is attacker-controlled data. The guard is
:func:`launcher.launch.natives._member_target` - the same function the natives extractor uses, not a
second copy of it - and the policy at this call site is stricter than the natives one: an unsafe
entry is refused (the whole import fails with the entry named), because a pack that tries to write
outside the instance folder is not a pack this launcher should half-install.

The guard's destination must be absolute: it compares the resolved member against the resolved
destination, and a relative destination would make every member look like an escape.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

from launcher.i18n import tr_core
from launcher.launch.natives import _member_target
from launcher.modpack.errors import ModpackError

#: A directory that does not need to exist: the guard only resolves paths, and this name is joined
#: with a member name to ask "would this stay inside a destination?" without writing anything.
_PROBE_DIRNAME = "_modpack_member_probe_"


def validate_member(name: str) -> str:
    """Return ``name`` when it is a safe archive-relative path, or refuse it.

    Used while *parsing*: a pack whose manifest names ``../outside.jar`` is rejected before a single
    byte is downloaded, instead of after the loader install has run.
    """
    probe = Path(os.getcwd()).resolve() / _PROBE_DIRNAME
    if not name or _member_target(probe, name) is None:
        raise ModpackError(tr_core("modpack.unsafe_path", name or "?"))
    return name


def member_target(dest: Path, name: str) -> Path:
    """``dest/name`` resolved, or :class:`ModpackError` when the member would leave ``dest``."""
    if not name:
        raise ModpackError(tr_core("modpack.unsafe_path", "?"))
    resolved_dest = Path(dest).resolve()
    target = _member_target(resolved_dest, name)
    if target is None:
        raise ModpackError(tr_core("modpack.unsafe_path", name))
    return target


def open_zip(path: Path) -> zipfile.ZipFile:
    """Open a pack archive, reporting a corrupt download as a message instead of a traceback."""
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ModpackError(tr_core("modpack.zip_invalid", Path(path).name)) from exc


def read_json(zf: zipfile.ZipFile, name: str) -> dict:
    """One JSON member as a mapping; a missing or malformed member names itself in the error."""
    try:
        raw = json.loads(zf.read(name))
    except KeyError as exc:
        raise ModpackError(tr_core("modpack.member_missing", name)) from exc
    except (ValueError, OSError) as exc:
        raise ModpackError(tr_core("modpack.parse_failed", name)) from exc
    if not isinstance(raw, dict):
        raise ModpackError(tr_core("modpack.parse_failed", name))
    return raw


def common_root(names: list[str]) -> str:
    """The single top-level folder every entry lives under, as a ``dir/`` prefix ("" when none).

    MultiMC and Prism zip an instance as a wrapper folder (``MyPack/mmc-pack.json``), while Modrinth
    and CurseForge put their index at the archive root. Finding the prefix instead of guessing it is
    what makes both shapes importable without a second parser.
    """
    entries = [name for name in names if name and not name.endswith("/")]
    if not entries:
        return ""
    if any("/" not in name for name in entries):
        return ""
    roots = {name.split("/", 1)[0] for name in entries}
    if len(roots) != 1:
        return ""
    return roots.pop() + "/"


def extract_prefixes(zf: zipfile.ZipFile, prefixes: tuple[str, ...], dest: Path) -> int:
    """Copy every file member under one of ``prefixes`` into ``dest``; return how many were copied."""
    if not prefixes:
        return 0
    count = 0
    for member in zf.infolist():
        name = member.filename
        prefix = next((item for item in prefixes if name.startswith(item)), None)
        if prefix is None or member.is_dir():
            continue
        relative = name[len(prefix) :]
        if not relative:
            continue
        target = member_target(dest, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as source, target.open("wb") as sink:
            shutil.copyfileobj(source, sink)
        count += 1
    return count


def copy_tree(source: Path, dest: Path) -> int:
    """Copy a directory tree into ``dest`` (files only, never a symlink); return the file count."""
    source = Path(source).resolve()
    count = 0
    for entry in sorted(source.rglob("*")):
        if entry.is_symlink() or not entry.is_file():
            continue
        relative = entry.relative_to(source).as_posix()
        target = member_target(dest, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry, target)
        count += 1
    return count
