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

"""Pixel gate for the four corners of every ``#pageCard``.

A QSS ``border-radius`` only rounds the card's own paint. If a list, scroll area or table
inside the card is flush with the edge, its square viewport is painted over the card's rounded
corner: the pixel at the corner is then the child's fill (or the card's white) instead of the
page paper. Offscreen rendering is enough to see this, and a per-corner pixel read is the only
thing that can prove it - a style-sheet grep cannot.

What is sampled: the 3px L at each corner (three pixels along each outer edge, five pixels per
corner; the inner diagonal of the 3x3 block is the card's antialiased arc and is deliberately
not judged). The page paper is read from the theme token ``BG_PAGE``, because the card sits on
the page paper on every page that uses it. A pixel passes when every RGB channel is within
``PAPER_TOLERANCE`` of that paper. The tolerance is 4/255 (about 1.6%), not 10% of the card
fill: the light card is #ffffff and the paper #f6f9fc, only 9/6/3 apart, so a 10% allowance
would silently accept a square white child. Antialiasing at the outer 3px of an 8px arc stays
on the paper side of the arc, so it does not need that allowance.

Exit status: 0 when every checked corner is page paper, 1 when any corner is covered (or when
no card was found at all - a vacuous pass is still a failure).

Usage:
    python tools/audit_card_radius.py                 # light + dark, zh_cn
    python tools/audit_card_radius.py --theme light --out D:/dsh-workspace/tmp/card_radius
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LANGUAGE = "zh_cn"
#: Per-channel tolerance against BG_PAGE. Deliberately smaller than the light card/paper gap.
PAPER_TOLERANCE = 4


def _prepare_environment(root: Path, language: str, theme: str) -> Path:
    """A throw-away data dir plus the render config, before Qt/theme imports."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.path.insert(0, str(root))
    # Work under the workspace temp tree: the suite's sandbox denies random mkdtemp-style
    # directories, and the repository must stay free of generated preview data.
    work_root = Path(os.environ.get("MCLAUNCHER_TMP", r"D:\dsh-workspace\tmp")) / "card_radius_work"
    work = work_root / f"{theme}_{language}"
    if work.exists():
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    data = work / "data"
    game = work / "game"
    data.mkdir(parents=True)
    game.mkdir(parents=True)
    os.environ["MCLAUNCHER_DATA_DIR"] = str(data)
    os.environ["MINECRAFT_GAME_DIR"] = str(game)
    from launcher import config

    cfg, cfg_path = config.load(data / config.CONFIG_FILENAME)
    cfg.ui_language = language
    cfg.game_language = language
    cfg.language_initialized = True
    cfg.wizard_done = True
    cfg.game_dir = str(game)
    cfg.theme = theme
    cfg.accent_color = ""
    cfg.ui_font = ""
    cfg.ui_radius = "default"
    config.save(cfg, cfg_path)
    return work


def _register_fonts() -> None:
    from PySide6.QtGui import QFontDatabase

    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "malgun.ttf", "YuGothM.ttc", "consola.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def _diff(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return max(abs(a - b) for a, b in zip(left, right))


def _corner_points(x0: int, y0: int, x1: int, y1: int) -> dict[str, list[tuple[int, int]]]:
    """The 3px outer L at each corner: (0,0)..(2,0) and (0,0)..(0,2)."""
    return {
        "TL": [(x0 + dx, y0) for dx in range(3)] + [(x0, y0 + dy) for dy in range(1, 3)],
        "TR": [(x1 - dx, y0) for dx in range(3)] + [(x1, y0 + dy) for dy in range(1, 3)],
        "BL": [(x0 + dx, y1) for dx in range(3)] + [(x0, y1 - dy) for dy in range(1, 3)],
        "BR": [(x1 - dx, y1) for dx in range(3)] + [(x1, y1 - dy) for dy in range(1, 3)],
    }


def _check_card(card, window, image, paper: tuple[int, int, int], page_key: str) -> dict:
    from PySide6.QtCore import QPoint

    top_left = card.mapTo(window, QPoint(0, 0))
    x0, y0 = top_left.x(), top_left.y()
    x1, y1 = x0 + card.width() - 1, y0 + card.height() - 1
    entry = {
        "page": page_key,
        "geometry": [x0, y0, card.width(), card.height()],
        "corners": {},
        "ok": True,
        "skipped": False,
    }
    # A card inside a hidden page of the stack still reports a geometry, but the pixels at that
    # geometry belong to whatever page *is* on screen - measuring them invents a failure. Skip any
    # card the current page does not actually show.
    if not card.isVisible():
        entry["skipped"] = True
        return entry
    if x0 < 3 or y0 < 3 or x1 >= image.width() - 3 or y1 >= image.height() - 3:
        entry["skipped"] = True
        return entry
    for name, points in _corner_points(x0, y0, x1, y1).items():
        samples = []
        failures = []
        for x, y in points:
            colour = image.pixelColor(x, y).getRgb()[:3]
            samples.append({"pixel": [x, y], "rgb": list(colour), "delta": _diff(colour, paper)})
            if _diff(colour, paper) > PAPER_TOLERANCE:
                failures.append(samples[-1])
        entry["corners"][name] = {"samples": samples, "failures": failures}
        if failures:
            entry["ok"] = False
    return entry


def _audit_theme(root: Path, theme: str, language: str, out_dir: Path | None) -> tuple[list[dict], dict]:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFrame

    work = _prepare_environment(root, language, theme)
    from gui.main import create_app
    from gui.pages import java_page, mods_page, versions_page
    from gui.theme import apply_theme, theme_palette
    from launcher.meta.manifest import VersionManifest

    # No network and no Java probing: the cards exist even with empty data, and a gate must be
    # deterministic and fast.
    versions_page.fetch_manifest = lambda cache_path=None: VersionManifest(latest={}, versions=[])
    mods_page.search_projects = lambda *a, **k: []
    java_page.list_java = lambda *a, **k: []
    if hasattr(java_page, "list_managed_runtimes"):
        java_page.list_managed_runtimes = list

    app = QApplication.instance() or create_app([])
    _register_fonts()
    apply_theme(theme, None, None, "default")

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1000, 610)
    window.show()
    for _ in range(12):
        app.processEvents()
        time.sleep(0.03)

    paper_hex = theme_palette(theme)["BG_PAGE"]
    paper = tuple(int(paper_hex[i : i + 2], 16) for i in (1, 3, 5))
    entries: list[dict] = []
    pages_checked: list[str] = []
    for index, key in enumerate(NAV_KEYS):
        window.sidebar.setCurrentRow(index)
        for _ in range(8):
            app.processEvents()
            time.sleep(0.02)
        image = window.grab().toImage()
        cards = window.pages[key].findChildren(QFrame, "pageCard")
        if cards:
            pages_checked.append(key)
        for card in cards:
            entry = _check_card(card, window, image, paper, key)
            entries.append(entry)
            if out_dir is not None and not entry["ok"] and not entry["skipped"]:
                out_dir.mkdir(parents=True, exist_ok=True)
                x0, y0 = entry["geometry"][:2]
                crop = image.copy(max(0, x0 - 4), max(0, y0 - 4), 24, 24)
                crop.save(str(out_dir / f"{theme}_{key}_{x0}_{y0}_TL.png"))
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            image.save(str(out_dir / f"{theme}_{index}_{key}.png"))

    window.close()
    window.deleteLater()
    app.processEvents()
    return entries, {"theme": theme, "language": language, "paper": paper_hex, "pages": pages_checked, "work": str(work)}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Pixel-audit every #pageCard's four corners.")
    parser.add_argument("--theme", default="both", choices=("light", "dark", "both"))
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--out", default="", help="optional directory for page PNGs of failing corners")
    args = parser.parse_args(argv)

    themes = ("light", "dark") if args.theme == "both" else (args.theme,)
    out_dir = Path(args.out) if args.out else None
    failures = 0
    checked = 0
    skipped = 0
    reports = []
    for theme in themes:
        entries, meta = _audit_theme(ROOT, theme, args.language, out_dir)
        theme_failures = [entry for entry in entries if not entry["ok"] and not entry["skipped"]]
        theme_checked = [entry for entry in entries if not entry["skipped"]]
        checked += len(theme_checked)
        skipped += len(entries) - len(theme_checked)
        failures += len(theme_failures)
        reports.append({**meta, "cards": len(entries), "checked": len(theme_checked), "failures": len(theme_failures)})
        print(f"=== {theme} / {args.language}: {len(theme_checked)} corner-checked card(s), "
              f"{len(entries) - len(theme_checked)} skipped (off-window) ===")
        for entry in entries:
            if entry["skipped"]:
                continue
            x0, y0, w, h = entry["geometry"]
            if entry["ok"]:
                print(f"  OK   {entry['page']:10} {w:4}x{h:<4} at {x0:4},{y0:4}")
                continue
            print(f"  FAIL {entry['page']:10} {w:4}x{h:<4} at {x0:4},{y0:4}")
            for name, corner in entry["corners"].items():
                for failure in corner["failures"]:
                    print(
                        f"       {name} pixel {tuple(failure['pixel'])} rgb={tuple(failure['rgb'])} "
                        f"delta={failure['delta']} (page {tuple(int(meta['paper'][i:i+2], 16) for i in (1, 3, 5))})"
                    )
        if not entries:
            print(f"FAIL: {theme} has no #pageCard widget at all - the gate would pass vacuously")
            failures += 1

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "card_radius.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")

    print()
    print(f"checked {checked} card(s) across {len(reports)} theme(s); skipped {skipped} off-window card(s); "
          f"violations {failures}")
    if checked == 0:
        print("FAIL: no card corner was checked")
        return 1
    print("OK: every checked card corner is page paper" if failures == 0 else "FAIL: covered card corner(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
