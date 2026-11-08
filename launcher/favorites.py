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

"""The launcher's own favorites list, stored as one JSON file in the launcher data folder.

A favorite is deliberately *not* a cached project: it is a pointer the user made - the kind, the
project id the source API uses, the name to show when the source is unreachable, the icon URL, and
when it was added. Nothing else is copied, so a favorite cannot go stale and there is no second copy
of a project's description to keep in sync.

The file lives under ``paths.launcher_dir()`` rather than the game folder: it is the launcher's own
list, not something Minecraft reads, and it must survive switching the current game folder.

Writes are atomic (temporary file in the same folder, then :func:`os.replace`) and a damaged file is
treated as an empty list: a favorites list is nice to have and must never stop the launcher from
starting. The next successful write repairs the file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from launcher import paths

LOGGER = logging.getLogger(__name__)

FILENAME = "favorites.json"
#: The four resource categories the resources page owns. A favorite of anything else is not shown.
KINDS = ("mod", "resourcepack", "shaderpack", "modpack")
FORMAT_VERSION = 1


class FavoritesError(Exception):
    """The list could not be saved (the message is user-facing)."""


@dataclass(frozen=True)
class Favorite:
    """One starred project."""

    kind: str
    project_id: str
    name: str
    icon_url: str = ""
    added_at: float = 0.0

    @property
    def key(self) -> tuple[str, str]:
        """What makes two favorites the same project: the category and the source's id."""
        return self.kind, self.project_id

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.project_id,
            "name": self.name,
            "icon_url": self.icon_url,
            "added_at": self.added_at,
        }

    @classmethod
    def from_json(cls, raw: object) -> Favorite | None:
        """One decoded entry, or None for anything that is not a complete favorite."""
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "").strip()
        project_id = str(raw.get("id") or "").strip()
        if kind not in KINDS or not project_id:
            return None
        added = raw.get("added_at")
        try:
            added_at = float(added) if added is not None else 0.0
        except (TypeError, ValueError):
            added_at = 0.0
        return cls(
            kind=kind,
            project_id=project_id,
            name=str(raw.get("name") or project_id),
            icon_url=str(raw.get("icon_url") or ""),
            added_at=added_at,
        )


def favorites_path() -> Path:
    """The default file: ``<launcher data dir>/favorites.json``."""
    return paths.launcher_dir() / FILENAME


class FavoriteStore:
    """Reads and writes one favorites file; the in-memory list is the source of truth after ``load``.

    ``load()`` is lazy and cached: constructing the store does not touch the disk, which keeps a page
    that is never visited free. ``force=True`` re-reads (the page uses it when it is shown again, so
    a second window of the same launcher would see the other one's changes).
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else favorites_path()
        self._items: list[Favorite] | None = None

    # ---------- reading ----------

    def load(self, *, force: bool = False) -> list[Favorite]:
        if self._items is not None and not force:
            return list(self._items)
        self._items = self._read()
        return list(self._items)

    def items(self) -> list[Favorite]:
        """Favorites, most recently added first (the order the page shows)."""
        return sorted(self.load(), key=lambda item: item.added_at, reverse=True)

    def contains(self, kind: str, project_id: str) -> bool:
        key = (kind, project_id)
        return any(item.key == key for item in self.load())

    def _read(self) -> list[Favorite]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            LOGGER.warning("favorites: cannot read %s: %s", self.path, exc)
            return []
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning("favorites: %s is not valid JSON, treating it as empty: %s", self.path, exc)
            return []
        raw_items = data.get("favorites") if isinstance(data, dict) else None
        if not isinstance(raw_items, list):
            return []
        decoded = [Favorite.from_json(item) for item in raw_items]
        return [item for item in decoded if item is not None]

    # ---------- writing ----------

    def add(
        self,
        kind: str,
        project_id: str,
        name: str,
        icon_url: str = "",
        *,
        added_at: float | None = None,
    ) -> bool:
        """Add one favorite; returns False when it was already there (nothing is written then)."""
        if kind not in KINDS:
            raise ValueError(f"unknown favorite kind {kind!r}")
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ValueError("a favorite needs a project id")
        items = self.load()
        if any(item.key == (kind, project_id) for item in items):
            return False
        if added_at is None:
            # time.time() has a coarse enough tick that two stars added back-to-back can share it.
            # The list is ordered by this stamp, so a new favorite must sort above the previous one.
            added_at = time.time()
            if items:
                added_at = max(added_at, max(item.added_at for item in items) + 0.000001)
        items.append(
            Favorite(
                kind=kind,
                project_id=project_id,
                name=str(name or project_id),
                icon_url=str(icon_url or ""),
                added_at=float(added_at),
            )
        )
        self._write(items)
        return True

    def remove(self, kind: str, project_id: str) -> bool:
        """Drop one favorite; returns False when it was not in the list."""
        items = self.load()
        kept = [item for item in items if item.key != (kind, project_id)]
        if len(kept) == len(items):
            return False
        self._write(kept)
        return True

    def toggle(
        self,
        kind: str,
        project_id: str,
        name: str,
        icon_url: str = "",
    ) -> bool:
        """Star or unstar one project; returns True when it is favorited afterwards."""
        if self.contains(kind, project_id):
            self.remove(kind, project_id)
            return False
        self.add(kind, project_id, name, icon_url)
        return True

    def _write(self, items: list[Favorite]) -> None:
        payload = {
            "version": FORMAT_VERSION,
            "favorites": [item.as_json() for item in items],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("favorites: could not remove %s", temporary, exc_info=True)
            raise FavoritesError(str(exc)) from exc
        self._items = list(items)


#: One store per file for the whole process. The pages are rebuilt on a language switch, but the list
#: is not page state: rebuilding a page must not re-read the file from under an unsaved change.
_STORE: FavoriteStore | None = None


def store() -> FavoriteStore:
    """The process-wide store for the current launcher data folder."""
    global _STORE
    path = favorites_path()
    if _STORE is None or _STORE.path != path:
        _STORE = FavoriteStore(path)
    return _STORE


def reset_cache() -> None:
    """Forget the cached store (tests switch ``MCLAUNCHER_DATA_DIR``; nothing else needs this)."""
    global _STORE
    _STORE = None
