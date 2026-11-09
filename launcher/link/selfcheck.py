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
from enum import Enum, IntEnum

from launcher.link.stun import StunVerdict


class NatType(IntEnum):
    """EasyTier's NAT classification, one value per wire code (0-8)."""

    OPEN_INTERNET = 0
    NO_NAT = 1
    FULL_CONE = 2
    RESTRICTED_CONE = 3
    PORT_RESTRICTED_CONE = 4
    SYMMETRIC_EASY = 5
    SYMMETRIC = 6
    SYMMETRIC_FIREWALL = 7
    BLOCKED = 8

    @classmethod
    def from_code(cls, value: object) -> NatType | None:
        """The code as a type, or ``None`` for a bool, a string or an out-of-range number."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        try:
            return cls(value)
        except ValueError:
            return None


class NatProbeError(str, Enum):
    """Why a probe could not provide both results; the UI translates the value."""

    MISSING = "missing"
    INSTALL = "install"
    TIMEOUT = "timeout"
    BAD_OUTPUT = "bad_output"
    EXIT = "exit"
    OS = "os"


@dataclass(frozen=True, slots=True)
class NatProbe:
    """UDP/TCP NAT types from EasyTier, or ``None`` for a result that was not obtained.

    ``error`` is the reason both results may be absent. A valid probe leaves it empty; a failed
    probe keeps ``detail`` for the log while the interface only shows the translated category.
    """

    udp: NatType | None = None
    tcp: NatType | None = None
    error: NatProbeError | None = None
    detail: str = ""

    @property
    def tested(self) -> bool:
        """Whether EasyTier returned at least the UDP type."""
        return self.udp is not None


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
    """Everything the network row needs, produced off the GUI thread.

    ``nat`` is the EasyTier result (UDP + TCP). When it is unavailable, ``stun`` still carries the
    built-in UDP verdict so the row can show the fallback instead of going blank.
    """

    component: ComponentStatus = field(default_factory=ComponentStatus)
    process: ProcessStatus = field(default_factory=ProcessStatus)
    stun: StunVerdict = StunVerdict.UNKNOWN
    nat: NatProbe = field(default_factory=NatProbe)
