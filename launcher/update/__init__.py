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

"""Self-update mechanics that must work while the launcher itself is running.

:mod:`launcher.update.install` owns the local half of an update - feasibility, the in-place swap of the
executable, the restart, and the cleanup of what a previous attempt left behind. The network half
(release lookup, download, verification) stays in :mod:`launcher.updates`.
"""

from launcher.update.install import (
    LEGACY_BACKUP_SUFFIX,
    NEW_SUFFIX,
    OLD_SUFFIX,
    Feasibility,
    InstallError,
    apply_and_restart,
    check_feasibility,
    cleanup_leftovers,
    is_packaged,
    relaunch,
    restart_arguments,
    running_executable,
    stage_download,
    swap_in_place,
)

__all__ = [
    "LEGACY_BACKUP_SUFFIX",
    "NEW_SUFFIX",
    "OLD_SUFFIX",
    "Feasibility",
    "InstallError",
    "apply_and_restart",
    "check_feasibility",
    "cleanup_leftovers",
    "is_packaged",
    "relaunch",
    "restart_arguments",
    "running_executable",
    "stage_download",
    "swap_in_place",
]
