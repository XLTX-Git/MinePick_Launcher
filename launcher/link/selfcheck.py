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

"""Value objects for the 网络自检 card.

Kept in their own module so the page, the component installer and the session can share the
shape of a report without importing each other. Qt-free like the rest of ``launcher.link``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from launcher.link.stun import StunVerdict


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """Whether the Terracotta payload is installed, and which version it is."""

    downloaded: bool = False
    version: str = ""
    supported: bool = True
    path: str = ""


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    """Whether a Terracotta instance is reachable, and on which loopback port."""

    running: bool = False
    port: int | None = None


@dataclass(frozen=True, slots=True)
class SelfCheckReport:
    """Everything the four self-check rows need, produced off the GUI thread."""

    component: ComponentStatus = field(default_factory=ComponentStatus)
    process: ProcessStatus = field(default_factory=ProcessStatus)
    stun: StunVerdict = StunVerdict.UNKNOWN
