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
"""Modpack import errors.

A failure is one sentence the user reads: it is already translated where it is raised
(``tr_core``), because only that call site knows which file, which loader or which archive member
is the subject.
"""

from __future__ import annotations


class ModpackError(Exception):
    """A modpack cannot be imported; the message is user-facing."""


class CurseforgeKeyMissing(ModpackError):
    """CurseForge is unavailable because no API key is configured (or the key was rejected).

    Its own type on purpose: a pack that needs CurseForge must stop at the first file instead of
    reporting a hundred "download failed" lines, and the caller can tell this case from an ordinary
    network failure without comparing translated strings.
    """
