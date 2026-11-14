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

"""Complete an instance whose files are missing, without reinstalling it.

A game folder made by another launcher can arrive incomplete - the version JSON is there but a library,
the client jar or half the assets are not, and Minecraft then dies at class-loading time with a message
that names none of it. :func:`plan_repair` walks the same task list the installer uses and returns only
what is actually missing or damaged; the downloader already skips files that check out, so repairing is
a download pass over an existing folder, not a reinstall: no version JSON is rewritten and nothing that
is already correct is touched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from launcher import paths
from launcher.install import plan_tasks
from launcher.meta.assets import AssetIndex
from launcher.meta.rules import Platform, detect_platform
from launcher.meta.version import VersionJson, load_version_json, version_json_path
from launcher.net.downloader import DownloadTask


@dataclass
class RepairPlan:
    """What a repair would download, and what it could not even look at."""

    version_id: str
    tasks: list[DownloadTask] = field(default_factory=list)
    #: Human-readable reasons a complete check was impossible (missing version JSON, no asset index).
    problems: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(task.size or 0 for task in self.tasks)

    @property
    def needed(self) -> bool:
        return bool(self.tasks)


def _load_asset_index(version: VersionJson, game_dir: Path, fetcher: Callable[[str], AssetIndex]) -> AssetIndex | None:
    """The version's asset index: from the local folder when it is there, else fetched.

    An incomplete instance often lacks the index file too, so the local read cannot be the only path;
    ``fetcher`` is injected so a test never needs the network.
    """
    index_id = version.asset_index.id
    if not index_id:
        return None
    local = paths.GamePaths(game_dir).assets_dir / "indexes" / f"{index_id}.json"
    if local.is_file():
        try:
            return AssetIndex.model_validate_json(local.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass  # damaged index: fall through to the fetcher rather than failing the repair
    return fetcher(index_id)


def plan_repair(
    version_id: str,
    *,
    game_dir: Path,
    platform: Platform | None = None,
    asset_index_fetcher: Callable[[str], AssetIndex] | None = None,
) -> RepairPlan:
    """List the files this instance is missing, in the order the installer would fetch them.

    Returns a plan even when nothing is missing (``tasks`` empty), so the caller can say "everything
    checks out" instead of guessing. Never raises for a missing version JSON: that is the one thing a
    repair cannot create from nothing, and it is reported through ``problems``.
    """
    plan = RepairPlan(version_id=version_id)
    folder = paths.GamePaths(game_dir).versions_dir / version_id
    json_path = version_json_path(folder)
    if json_path is None:
        plan.problems.append(f"no single version JSON in {folder}")
        return plan

    version = load_version_json(
        version_id, versions_dir=paths.GamePaths(game_dir).versions_dir
    )
    if platform is None:
        platform = detect_platform()

    index: AssetIndex | None = None
    if version.asset_index.id:
        fetcher = asset_index_fetcher
        if fetcher is None:
            from launcher.meta.assets import fetch_asset_index

            fetcher = fetch_asset_index
        try:
            index = _load_asset_index(version, game_dir, fetcher)
        except Exception as exc:  # noqa: BLE001 - a repair reports, it does not propagate
            plan.problems.append(f"asset index unavailable: {exc}")
    if index is None:
        # No index means the asset half cannot be checked; libraries and the client jar still can.
        plan.problems.append("asset index missing: assets were not checked")

    planned = plan_tasks(version, game_dir, platform, index)
    plan.tasks = [task for task in planned if not _already_holds(task)]
    return plan


def _already_holds(task: DownloadTask) -> bool:
    """Whether the destination already has the file this task would fetch.

    Size is the test, not the hash: a repair runs before a launch and hashing several hundred megabytes
    would make it feel broken. A truncated download is the case that matters, and that shows up as a
    size mismatch, which the downloader would notice anyway - this only keeps the count honest.
    """
    try:
        if not task.dest.is_file():
            return False
        if task.size is not None:
            return task.dest.stat().st_size == task.size
        return True
    except OSError:
        return False
