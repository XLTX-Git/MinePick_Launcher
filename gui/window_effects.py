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

"""Native window effects the style sheet cannot express.

The window is frameless (see gui/title_bar.py), and Windows stops drawing its own rounded
frame for a frameless top-level window. Windows 11 exposes the corner preference through
DWM, so the same 8px the cards use can be asked for here. The call is best-effort by design:
Windows 10 has no ``DWMWA_WINDOW_CORNER_PREFERENCE`` attribute, and a launcher must not fail
to start because a cosmetic DWM attribute is unavailable.
"""

from __future__ import annotations

import sys

#: ``DWMWA_WINDOW_CORNER_PREFERENCE`` from dwmapi.h (Windows 11 22000+).
DWMWA_WINDOW_CORNER_PREFERENCE = 33
#: ``DWMWCP_ROUND`` - the 8px radius Windows 11 uses for its own windows at 100% scale.
DWMWCP_ROUND = 2


def _dwm() -> object | None:
    """The dwmapi module, or ``None`` where it does not exist (older Windows, non-Windows)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return ctypes.WinDLL("dwmapi")
    except Exception:  # noqa: BLE001 - a missing dwmapi is a supported configuration
        return None


def apply_rounded_corners(window) -> bool:
    """Ask DWM to round ``window``'s native corners; return ``True`` only when it accepted.

    ``window`` is a QWidget/QMainWindow whose native handle exists (it has been shown, or
    ``winId()`` was called). Every failure path is silent: the window simply stays square,
    which is what Windows 10 does anyway. The return value exists for the verification tool
    and test, not for the application to branch on.
    """
    dwm = _dwm()
    if dwm is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(window.winId())
        if not hwnd:
            return False
        setter = dwm.DwmSetWindowAttribute
        setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        setter.restype = ctypes.c_long
        value = ctypes.c_int(DWMWCP_ROUND)
        result = setter(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(value), ctypes.sizeof(value))
        return result == 0
    except Exception:  # noqa: BLE001 - cosmetic only; never let it reach startup
        return False


def corner_preference(window) -> int | None:
    """Read back the applied corner preference, or ``None`` when DWM cannot answer.

    Used by the real-window verification so "the call returned 0" can be checked against
    what the window actually carries, instead of trusting the setter's return value alone.
    """
    dwm = _dwm()
    if dwm is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(window.winId())
        if not hwnd:
            return None
        getter = dwm.DwmGetWindowAttribute
        getter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        getter.restype = ctypes.c_long
        value = ctypes.c_int(-1)
        result = getter(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(value), ctypes.sizeof(value))
        return int(value.value) if result == 0 else None
    except Exception:  # noqa: BLE001 - readback is evidence, never a startup dependency
        return None
