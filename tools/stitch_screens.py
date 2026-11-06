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

"""Stitch light-theme and dark-theme page screenshots into one side-by-side PNG.

``tools/shot_pages.py`` renders one theme at a time and names every page
``<label>_<index>_<page>.png``. This tool pairs the two directories by that page part (so
``light_6_settings.png`` pairs with ``dark_6_settings.png``), scales both down to fit
``--max-width``, and joins them with a 1px divider:

    python tools/stitch_screens.py --light shots/light --dark shots/dark --out shots/both

A page that exists on only one side is reported on stderr and skipped, and the run still exits 0:
one missing render must not cost the other pages in the set.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_MAX_WIDTH = 1600
DEFAULT_DIVIDER_COLOR = "#dfe7f0"
DIVIDER_WIDTH = 1
LABEL_MARGIN = 8
LABEL_FONT_PX = 14

#: ``<label>_<index>_<page>.png``. The greedy prefix makes this pick the *last* numbered segment,
#: so a label that itself contains an index-like token cannot swallow the page part.
_PAGE_SUFFIX = re.compile(r"^(?:.*_)?(\d+_[A-Za-z][A-Za-z0-9_-]*)$")
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_DIGIT_RUN = re.compile(r"(\d+)")

_QT_APP = None


def page_key(stem: str) -> str:
    """Return the page part of a screenshot name, dropping the per-theme label prefix.

    The label differs per theme (``light_6_settings.png`` vs ``dark_6_settings.png``), but the
    numbered page suffix is the same. A name without a numbered suffix (``light_home.png``) falls
    back to everything after its first underscore.
    """
    match = _PAGE_SUFFIX.match(stem)
    if match:
        return match.group(1)
    _prefix, sep, rest = stem.partition("_")
    return rest if sep and rest else stem


def _natural_key(stem: str) -> tuple[tuple[int, int | str], ...]:
    """Sort ``2_versions`` before ``10_about`` instead of lexicographically."""
    parts = _DIGIT_RUN.split(stem)
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def _screenshots(directory: Path) -> dict[str, Path]:
    """Map every page key in ``directory`` to its PNG, skipping (with a warning) duplicate keys."""
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*.png"), key=lambda item: _natural_key(item.stem)):
        key = page_key(path.stem)
        if key in found:
            print(
                f"warning: {path.name} and {found[key].name} both map to {key}.png; "
                "keeping the first",
                file=sys.stderr,
            )
            continue
        found[key] = path
    return found


def _ensure_app() -> None:
    """Create the windowless Qt application that QPainter needs for the optional label font."""
    global _QT_APP
    if _QT_APP is not None:
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        _QT_APP = QGuiApplication([])


def _load(path: Path):
    from PySide6.QtGui import QImage

    image = QImage(str(path))
    if image.isNull():
        raise OSError(f"cannot read PNG: {path}")
    return image.convertToFormat(QImage.Format.Format_RGB32)


def _fit(image, width: int):
    """Smooth-scale (never distorting) a screenshot to exactly ``width`` pixels."""
    from PySide6.QtCore import Qt

    if image.width() == width:
        return image
    return image.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)


def _draw_label(painter, text: str, font, x: int) -> None:
    """Draw a small translucent badge at the top-left of the panel that starts at ``x``."""
    from PySide6.QtGui import QColor, QFontMetrics

    metrics = QFontMetrics(font)
    width = metrics.horizontalAdvance(text) + 12
    height = metrics.height() + 6
    painter.setPen(QColor(255, 255, 255, 220))
    painter.setBrush(QColor(0, 0, 0, 150))
    painter.drawRoundedRect(x + LABEL_MARGIN, LABEL_MARGIN, width, height, 4, 4)
    painter.setFont(font)
    painter.setPen(QColor(255, 255, 255))
    painter.drawText(x + LABEL_MARGIN + 6, LABEL_MARGIN + 3 + metrics.ascent(), text)


def stitch_pair(
    light_path: Path,
    dark_path: Path,
    target: Path,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    divider_color: str = DEFAULT_DIVIDER_COLOR,
    labels: bool = False,
) -> tuple[int, int]:
    """Compose one pair, save it to ``target`` and return the written ``(width, height)``.

    Both panels get the same width - half of the space left after the divider, capped at the wider
    source so a pair of small shots is never blown up. Heights follow each screenshot's aspect
    ratio; the canvas is as tall as the taller panel and the shorter side is padded with the
    divider colour.
    """
    _ensure_app()
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    left, right = _load(light_path), _load(dark_path)
    budget = max_width - DIVIDER_WIDTH
    panel_width = max(1, min(budget // 2, max(left.width(), right.width())))
    left = _fit(left, panel_width)
    right = _fit(right, panel_width)

    canvas = QImage(
        left.width() + DIVIDER_WIDTH + right.width(),
        max(left.height(), right.height()),
        QImage.Format.Format_RGB32,
    )
    canvas.fill(QColor(divider_color))

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(0, 0, left)
    painter.drawImage(left.width() + DIVIDER_WIDTH, 0, right)
    if labels:
        font = QFont("Segoe UI")
        font.setPixelSize(LABEL_FONT_PX)
        font.setBold(True)
        _draw_label(painter, "Light", font, 0)
        _draw_label(painter, "Dark", font, left.width() + DIVIDER_WIDTH)
    painter.end()

    target.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(target), "PNG"):
        raise OSError(f"cannot write PNG: {target}")
    return canvas.width(), canvas.height()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stitch light and dark page screenshots side by side.",
        epilog=(
            "Pairing: files are matched by page name, the part from the page index on "
            "(light_6_settings.png + dark_6_settings.png -> 6_settings.png). A page present on "
            "only one side is reported on stderr and skipped; the rest are still written."
        ),
    )
    parser.add_argument("--light", required=True, help="directory with the light-theme PNGs")
    parser.add_argument("--dark", required=True, help="directory with the dark-theme PNGs")
    parser.add_argument("--out", required=True, help="output directory for the paired PNGs")
    parser.add_argument(
        "--max-width",
        type=int,
        default=DEFAULT_MAX_WIDTH,
        help=(
            "maximum total width of one stitched image, divider included "
            f"(default: {DEFAULT_MAX_WIDTH}); each panel gets half of it"
        ),
    )
    parser.add_argument(
        "--divider-color",
        default=DEFAULT_DIVIDER_COLOR,
        help=f"1px separator colour (default: {DEFAULT_DIVIDER_COLOR})",
    )
    parser.add_argument(
        "--labels",
        action="store_true",
        help=(
            "draw a small 'Light'/'Dark' badge in each panel's top-left corner; off by default "
            "because it covers the window title bar"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.max_width < DIVIDER_WIDTH + 2:
        parser.error(f"--max-width must be at least {DIVIDER_WIDTH + 2} (two panels plus divider)")
    if not _HEX_COLOR.match(args.divider_color):
        parser.error("--divider-color must be a hex colour such as #dfe7f0")

    light_dir, dark_dir = Path(args.light), Path(args.dark)
    for option, directory in (("--light", light_dir), ("--dark", dark_dir)):
        if not directory.is_dir():
            parser.error(f"{option} is not a directory: {directory}")
    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error(f"cannot create --out directory {out_dir}: {exc}")

    light, dark = _screenshots(light_dir), _screenshots(dark_dir)
    keys = sorted(set(light) | set(dark), key=_natural_key)
    if not keys:
        print(f"no .png files found in {light_dir} or {dark_dir}", file=sys.stderr)
        return 1

    written = skipped = failed = 0
    for key in keys:
        if key not in light or key not in dark:
            present = light.get(key) or dark.get(key)
            missing = "--dark" if key not in dark else "--light"
            print(
                f"skipped {present.name}: no counterpart for page {key!r} in {missing}",
                file=sys.stderr,
            )
            skipped += 1
            continue
        target = out_dir / f"{key}.png"
        try:
            width, height = stitch_pair(
                light[key],
                dark[key],
                target,
                max_width=args.max_width,
                divider_color=args.divider_color,
                labels=args.labels,
            )
        except OSError as exc:  # one unreadable/unwritable image must not stop the set
            print(f"failed {key}.png: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"stitched {target.name} ({width}x{height})")
        written += 1

    print(f"{written} stitched, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
