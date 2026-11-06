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

"""Memory sizing helpers: suggest a JVM heap size from system memory and mod count.

The suggestion follows the same tiered idea as popular community launchers:
targets grow with the mod count, but each tier only takes a fraction of the
available system memory, so the OS is never starved.
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

MAX_MEMORY_GB = 16.0
MIN_MEMORY_GB = 0.5

#: The settings page never offers less than 1 GB: below that even vanilla Minecraft struggles.
MIN_HEAP_GB = 1.0
#: A 32-bit JVM cannot address much more than this, whatever the machine has.
JAVA_32BIT_HEAP_LIMIT_GB = 1.5
#: Slider granularity, and the ceiling the config schema itself accepts.
HEAP_STEP_GB = 0.5
CONFIG_MAX_HEAP_GB = 64.0


def count_mods(mods_dir: Path) -> int:
    """Count mod files in a directory (enabled *.jar and disabled *.jar.disabled)."""
    if not mods_dir.is_dir():
        return 0
    try:
        return sum(
            1
            for p in mods_dir.iterdir()
            if p.name.lower().endswith((".jar", ".jar.disabled"))
        )
    except OSError:
        return 0


def system_memory_gb() -> tuple[float, float]:
    """Return (total physical GB, available physical GB) of the system."""
    if os.name == "nt":
        try:
            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                gib = 1024 ** 3
                return status.ullTotalPhys / gib, status.ullAvailPhys / gib
        except Exception:  # fall through to the fallback below
            logging.getLogger(__name__).debug("GlobalMemoryStatusEx failed", exc_info=True)
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page / 1024 ** 3
        avail = os.sysconf("SC_AVPHYS_PAGES") * page / 1024 ** 3
        return total, avail
    except (AttributeError, ValueError, OSError):
        return 8.0, 4.0


def suggest_memory_gb(
    mod_count: int = 0,
    *,
    total_gb: float | None = None,
    available_gb: float | None = None,
) -> float:
    """Suggest a JVM heap size in GB, rounded to 0.5 GB steps.

    Tier targets grow with the mod count; each tier only claims a fraction
    (100% / 70% / 40% / 15%) of the remaining available memory, and the
    result is clamped to [minimum, 75% of total, MAX_MEMORY_GB].
    """
    if total_gb is None or available_gb is None:
        total_gb, available_gb = system_memory_gb()
    if mod_count > 0:
        minimum = MIN_MEMORY_GB + mod_count / 150
        targets = (1.5 + mod_count / 90, 2.7 + mod_count / 50, 4.5 + mod_count / 25)
    else:
        minimum = MIN_MEMORY_GB
        targets = (1.5, 2.5, 4.0)
    boundaries = (targets[0], targets[1], targets[2], targets[2] * 2)
    fractions = (1.0, 0.7, 0.4, 0.15)
    avail = max(available_gb, 0.0)
    ram_give = 0.0
    prev = 0.0
    for boundary, fraction in zip(boundaries, fractions):
        delta = boundary - prev
        ram_give += min(avail * fraction, delta)
        avail -= delta / fraction
        if avail < 0.1:
            break
        prev = boundary
    ram_give = max(ram_give, minimum)
    ram_give = min(ram_give, total_gb * 0.75, MAX_MEMORY_GB)
    return round(ram_give * 2) / 2


def maximum_heap_gb(
    total_gb: float | None = None,
    *,
    java_32bit: bool = False,
    minimum_gb: float = MIN_HEAP_GB,
) -> float:
    """Largest heap the settings page should offer on this machine, in 0.5 GB steps.

    The rule is PCL's: give the JVM at most half of the physical RAM, but leave the
    OS at least 2 GB and never go below ``minimum_gb``. On a 16 GB machine that is
    8 GB, on 8 GB it is 4 GB and on 4 GB it is 2 GB.

    ``total_gb`` is optional so a caller (and a test) can state the machine instead
    of probing it. ``minimum_gb`` exists for an instance that pins more than the
    default 1 GB floor; a 32-bit JVM still caps the result at 1.5 GB unless that
    instance minimum is itself higher.

    The raw half-RAM value is rounded to the nearest 0.5 GB rather than truncated:
    Windows reports a "16 GB" machine as ~15.7 GB, and truncating would turn the
    documented 8 GB recommendation into 7.5 GB on every such machine.
    """
    if total_gb is None or total_gb <= 0:
        total_gb, _available = system_memory_gb()
    minimum = max(MIN_HEAP_GB, float(minimum_gb))
    # Leave the OS 2 GB, but never let that ceiling fall below the requested floor.
    os_reserve_limit = max(float(total_gb) - 2.0, minimum)
    limit = min(float(total_gb) * 0.5, os_reserve_limit)
    limit = max(limit, minimum)
    if java_32bit:
        limit = min(limit, max(JAVA_32BIT_HEAP_LIMIT_GB, minimum))
    limit = min(limit, CONFIG_MAX_HEAP_GB)
    limit = round(limit / HEAP_STEP_GB) * HEAP_STEP_GB
    return max(limit, minimum)

