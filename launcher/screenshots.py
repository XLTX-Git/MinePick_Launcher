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

"""Where Minecraft put the screenshots: the shared game folder and every instance folder.

A screenshot is written to the game directory the game process was started with. That is the shared
game folder for an ordinary launch, and the instance folder when the instance is isolated - so the
same launcher has to look in both places, and the list has to say which one a picture came from
("Screenshot 2026-11-08_12.34.56.png" can exist in both folders).

This module is deliberately Qt-free: it only stats and lists files, so the scan can run without a
QApplication and a test can call it directly. Decoding a thumbnail needs Qt and lives in the page
(``gui.pages.screenshots_page``), where the background loader can use ``QImageReader``'s scaled read.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: ``instance_id`` of a screenshot that lives in the shared game folder rather than in an instance.
SHARED_INSTANCE_ID = ""
#: Minecraft writes PNG; the rest are accepted because a user may drop a converted picture in the
#: folder and hiding it would look like the launcher lost it.
SUPPORTED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"})


@dataclass(frozen=True)
class ScreenshotEntry:
    """One picture on disk, with the folder it belongs to and the time it was written."""

    path: Path
    filename: str
    taken_at: float
    instance_id: str = SHARED_INSTANCE_ID
    instance_name: str = ""
    size_bytes: int = 0

    @property
    def is_shared(self) -> bool:
        """True for the shared game folder, i.e. a screenshot that belongs to no instance."""
        return not self.instance_id


def screenshots_dir(game_dir: Path | str) -> Path:
    """The shared ``<game_dir>/screenshots`` folder (it may not exist yet)."""
    return Path(game_dir) / "screenshots"


def instance_screenshots_dir(game_dir: Path | str, instance_id: str) -> Path:
    """The ``versions/<id>/screenshots`` folder of one instance (it may not exist)."""
    return Path(game_dir) / "versions" / instance_id / "screenshots"


def _entries_in(directory: Path, instance_id: str, instance_name: str) -> list[ScreenshotEntry]:
    """Every supported picture directly inside ``directory``; an unreadable folder reads as empty."""
    try:
        children = list(directory.iterdir())
    except OSError:
        return []
    found: list[ScreenshotEntry] = []
    for child in children:
        try:
            if child.suffix.lower() not in SUPPORTED_SUFFIXES or not child.is_file():
                continue
            stat = child.stat()
        except OSError:
            continue
        found.append(
            ScreenshotEntry(
                path=child,
                filename=child.name,
                taken_at=stat.st_mtime,
                instance_id=instance_id,
                instance_name=instance_name,
                size_bytes=stat.st_size,
            )
        )
    return found


def scan_screenshots(
    game_dir: Path | str,
    instances: Mapping[str, str] | None = None,
) -> list[ScreenshotEntry]:
    """Every screenshot of ``game_dir``, newest first.

    ``instances`` maps an instance id to the name shown in the list; ids that are not in the mapping
    fall back to the folder name (an instance folder without a sidecar is still a folder the game can
    write into). Only folders that exist are read, so a folder with no ``screenshots/`` costs one
    ``is_dir()`` and nothing else.

    The order is by modification time, newest first: that is the order the folder was written in and
    the only order a user can reason about when they remember "the one I took a minute ago". Ties fall
    back to the filename and then the full path, so the order is stable across scans.
    """
    root = Path(game_dir)
    names = dict(instances or {})
    found = _entries_in(screenshots_dir(root), SHARED_INSTANCE_ID, "")
    try:
        version_folders = sorted(child for child in (root / "versions").iterdir() if child.is_dir())
    except OSError:
        version_folders = []
    for folder in version_folders:
        shots = folder / "screenshots"
        if not shots.is_dir():
            continue
        found.extend(_entries_in(shots, folder.name, names.get(folder.name, folder.name)))
    # One physical file can only be listed once even if two scans reach it (a game folder that is
    # also an instance folder); the first entry wins because the shared folder is read first.
    unique: dict[str, ScreenshotEntry] = {}
    for entry in found:
        unique.setdefault(str(entry.path), entry)
    return sorted(
        unique.values(),
        key=lambda entry: (-entry.taken_at, entry.filename.casefold(), str(entry.path)),
    )


def scan_instance_screenshots(
    game_dir: Path | str,
    instance_id: str,
    instance_name: str,
    instance_dir: Path | str | None = None,
) -> list[ScreenshotEntry]:
    """The screenshots of one instance plus the shared folder's, newest first.

    The instance half is read from the folder the game actually runs in (``instance_dir``, i.e.
    ``ResolvedInstance.launch_dir``), not from ``versions/<id>``: an isolated instance runs inside its
    own folder, while a shared-mode instance runs in the game root and therefore writes to the shared
    ``screenshots/`` only. Reading the *effective* folder is what makes both modes show the truth;
    passing ``launch_dir`` is the caller's one job. A shared-mode launch directory is the game root,
    so the second scan is skipped and every row is marked shared.

    The rows keep the same "shared folder" / instance-name distinction the unscoped scan uses, so the
    page's source column needs no special case.
    """
    root = Path(game_dir)
    shared = screenshots_dir(root)
    own = (Path(instance_dir) if instance_dir is not None else root / "versions" / instance_id)
    own_shots = own / "screenshots"
    found = _entries_in(shared, SHARED_INSTANCE_ID, "")
    if own != root and own_shots != shared:
        found.extend(_entries_in(own_shots, instance_id, instance_name))
    unique: dict[str, ScreenshotEntry] = {}
    for entry in found:
        unique.setdefault(str(entry.path), entry)
    return sorted(
        unique.values(),
        key=lambda entry: (-entry.taken_at, entry.filename.casefold(), str(entry.path)),
    )

