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

"""Verify the Windows 11 DWM corner preference on a real native window.

The offscreen test suite (and every render tool) cannot prove a compositor effect: the
rounded corner is drawn by DWM outside the window surface, which is why a Qt grab or
``PrintWindow`` shows a square client even when the window is round on the desktop.

This tool therefore creates a **real** native frameless window on the Windows platform,
asks for the same preference the application asks for, and reports two independent facts:

* the ``DwmSetWindowAttribute`` return code (0 = accepted);
* ``DwmGetWindowAttribute`` reading the preference back (2 = ``DWMWCP_ROUND``).

It never calls ``show()``: the native handle is realised with ``winId()`` while the window
stays hidden, which is enough for DWM to accept the attribute and for the readback to answer.
A pixel crop is deliberately not attempted - the compositor corner is outside the window
surface, and capturing a real window would mean putting one on the user's screen.

Usage:
    python tools/verify_window_rounding.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path(os.environ.get("MCLAUNCHER_TMP", r"D:\dsh-workspace\tmp")) / "ui_refs" / "final" / "window_rounding"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify DWM rounded corners without showing a window.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    os.environ.pop("QT_QPA_PLATFORM", None)
    sys.path.insert(0, str(ROOT))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    from gui import window_effects

    app = QApplication([])
    platform = app.platformName()
    result: dict[str, object] = {"platform": platform}
    if platform != "windows":
        result["verified"] = False
        result["reason"] = "not the Windows platform; DWM is unavailable"
        (out / "window_rounding.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"UNVERIFIED: platform={platform!r}; run this tool on Windows")
        return 2

    window = QWidget()
    window.setWindowTitle("MinePick rounding check")
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    window.setStyleSheet("background: #1E6FD9; border-radius: 8px;")
    window.resize(360, 220)
    # No show(), no move onto a screen: winId() realises the native handle while the window
    # remains hidden, and DWM accepts/readbacks the corner preference on that handle.
    hwnd = int(window.winId())

    applied = window_effects.apply_rounded_corners(window)
    readback = window_effects.corner_preference(window)
    result.update(
        {
            "hwnd": hwnd,
            "device_pixel_ratio": float(window.devicePixelRatioF()),
            "window_shown": False,
            "set_returned_true": bool(applied),
            "readback_corner_preference": readback,
            "expected_round_value": window_effects.DWMWCP_ROUND,
            "verified": bool(applied and readback == window_effects.DWMWCP_ROUND),
        }
    )

    window.close()
    window.deleteLater()
    app.processEvents()

    (out / "window_rounding.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"platform       : {platform}")
    print(f"hwnd           : {result['hwnd']} (devicePixelRatio {result['device_pixel_ratio']}, shown=False)")
    print(f"DwmSetWindowAttribute -> {applied} (expected True, HRESULT 0)")
    print(f"DwmGetWindowAttribute -> {readback} (expected {window_effects.DWMWCP_ROUND} = DWMWCP_ROUND)")
    print(f"JSON           : {out / 'window_rounding.json'}")
    print("VERIFIED" if result["verified"] else "UNVERIFIED")
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
