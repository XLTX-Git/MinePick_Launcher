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

"""Device-aware heap bounds shared by both first-run wizards.

The first-run wizard used to offer a fixed 0.5 - 64 GB range no matter what the machine had. The
numbers here come from :mod:`launcher.memory` - the same module the settings page already uses - so
the wizard cannot drift away from the rule the settings page states: at most half of physical RAM,
the OS keeps at least 2 GB, a 1 GB floor, 0.5 GB steps, and the 32-bit JVM ceiling of 1.5 GB.

Everything is injectable: ``total_gb`` / ``available_gb`` / ``java_32bit`` let a test state the
machine instead of probing it, which is also what keeps the wizard testable on a build server.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryBounds:
    """What this machine can offer the JVM, and what the wizard should preselect."""

    total_gb: float
    available_gb: float
    minimum_gb: float
    maximum_gb: float
    recommended_gb: float
    java_32bit: bool


def memory_bounds(
    *,
    total_gb: float | None = None,
    available_gb: float | None = None,
    java_32bit: bool | None = None,
    java_path: str | None = None,
    minimum_gb: float | None = None,
) -> MemoryBounds:
    """Resolve the heap range and the recommended default for this machine.

    ``java_path`` is the configured Java (``None`` = auto-detect); its bitness lowers the ceiling to
    1.5 GB exactly as the settings page does. ``minimum_gb`` exists for an instance floor, but both
    wizards use the 1 GB launcher floor.
    """
    from launcher.java.locate import selected_java_bits
    from launcher.memory import (
        HEAP_STEP_GB,
        MIN_HEAP_GB,
        maximum_heap_gb,
        suggest_memory_gb,
        system_memory_gb,
    )

    if total_gb is None or available_gb is None:
        probed_total, probed_available = system_memory_gb()
        total_gb = probed_total if total_gb is None else total_gb
        available_gb = probed_available if available_gb is None else available_gb
    total = max(float(total_gb), 0.0)
    available = max(float(available_gb), 0.0)
    if java_32bit is None:
        java_32bit = selected_java_bits(java_path) == 32
    floor = MIN_HEAP_GB if minimum_gb is None else max(MIN_HEAP_GB, float(minimum_gb))
    ceiling = maximum_heap_gb(total, java_32bit=bool(java_32bit), minimum_gb=floor)
    recommended = suggest_memory_gb(0, total_gb=total, available_gb=available)
    recommended = round(recommended / HEAP_STEP_GB) * HEAP_STEP_GB
    recommended = min(max(recommended, floor), ceiling)
    return MemoryBounds(
        total_gb=total,
        available_gb=available,
        minimum_gb=floor,
        maximum_gb=ceiling,
        recommended_gb=recommended,
        java_32bit=bool(java_32bit),
    )
