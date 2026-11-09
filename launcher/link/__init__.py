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

"""Multiplayer integration surface (Terracotta)."""

from launcher.link.bus import (
    ACTIVE_STATES,
    LinkRoomSummary,
    LinkSessionBus,
    default_link_bus,
    running_terracotta_port,
)
from launcher.link.easytier import EasytierPackage, probe_nat_types
from launcher.link.selfcheck import (
    ComponentStatus,
    NatProbe,
    NatProbeError,
    NatType,
    ProcessStatus,
    SelfCheckReport,
)
from launcher.link.session import (
    FakeLinkSession,
    LinkDifficulty,
    LinkDownloadProgress,
    LinkMember,
    LinkSession,
    LinkState,
    default_link_session,
)
from launcher.link.stun import StunVerdict
from launcher.link.terracotta import TerracottaLinkSession

__all__ = [
    "ACTIVE_STATES",
    "ComponentStatus",
    "EasytierPackage",
    "FakeLinkSession",
    "LinkDifficulty",
    "LinkDownloadProgress",
    "LinkMember",
    "LinkRoomSummary",
    "LinkSession",
    "LinkSessionBus",
    "LinkState",
    "NatProbe",
    "NatProbeError",
    "NatType",
    "ProcessStatus",
    "SelfCheckReport",
    "StunVerdict",
    "TerracottaLinkSession",
    "default_link_bus",
    "default_link_session",
    "probe_nat_types",
    "running_terracotta_port",
]
