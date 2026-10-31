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

"""Tests for tools/stitch_screens.py: pairing, layout, divider, scaling and missing sides."""

from __future__ import annotations

from pathlib import Path

from tools import stitch_screens


def _write_png(path: Path, width: int, height: int, color: str) -> None:
    """Write a solid-colour PNG, so any pixel that is not that colour comes from the stitcher."""
    from PySide6.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "PNG")


def _load(path: Path):
    from PySide6.QtGui import QImage

    image = QImage(str(path))
    assert not image.isNull(), f"output image is missing or unreadable: {path}"
    return image.convertToFormat(QImage.Format.Format_RGB32)


def _run(light: Path, dark: Path, out: Path, *extra: str) -> int:
    return stitch_screens.main(
        ["--light", str(light), "--dark", str(dark), "--out", str(out), *extra]
    )


def test_page_key_drops_the_theme_prefix():
    assert stitch_screens.page_key("light_6_settings") == "6_settings"
    assert stitch_screens.page_key("dark_6_settings") == "6_settings"
    assert stitch_screens.page_key("final_0_launch") == "0_launch"
    assert stitch_screens.page_key("settings") == "settings"


def test_stitches_side_by_side_with_divider(ws_tmp):
    light, dark, out = ws_tmp / "light", ws_tmp / "dark", ws_tmp / "out"
    light.mkdir()
    dark.mkdir()
    _write_png(light / "light_2_beta.png", 120, 80, "#ff0000")
    _write_png(dark / "dark_2_beta.png", 120, 80, "#0000ff")

    code = _run(light, dark, out, "--max-width", "81", "--divider-color", "#00ff00")

    assert code == 0
    result = _load(out / "2_beta.png")
    # 81 = 40 (light) + 1 (divider) + 40 (dark); both panels keep the 3:2 source ratio.
    assert (result.width(), result.height()) == (81, 27)
    assert result.pixelColor(0, 0).name() == "#ff0000"
    assert result.pixelColor(result.width() - 1, 0).name() == "#0000ff"
    assert all(result.pixelColor(40, y).name() == "#00ff00" for y in range(result.height()))


def test_small_pair_is_not_blown_up(ws_tmp):
    light, dark, out = ws_tmp / "light", ws_tmp / "dark", ws_tmp / "out"
    light.mkdir()
    dark.mkdir()
    _write_png(light / "light_0_launch.png", 30, 20, "#ff0000")
    _write_png(dark / "dark_0_launch.png", 30, 20, "#0000ff")

    assert _run(light, dark, out) == 0

    result = _load(out / "0_launch.png")
    assert (result.width(), result.height()) == (61, 20)


def test_missing_side_is_skipped_without_failing_the_run(ws_tmp, capsys):
    light, dark, out = ws_tmp / "light", ws_tmp / "dark", ws_tmp / "out"
    light.mkdir()
    dark.mkdir()
    _write_png(light / "light_1_first.png", 40, 20, "#ff0000")
    _write_png(light / "light_2_second.png", 40, 20, "#ff0000")
    _write_png(dark / "dark_1_first.png", 40, 20, "#0000ff")

    code = _run(light, dark, out)

    assert code == 0
    assert (out / "1_first.png").is_file()
    assert not (out / "2_second.png").exists()
    stderr = capsys.readouterr().err
    assert "2_second" in stderr
    assert "--dark" in stderr


def test_labels_are_optional(ws_tmp):
    light, dark = ws_tmp / "light", ws_tmp / "dark"
    light.mkdir()
    dark.mkdir()
    _write_png(light / "light_0_launch.png", 200, 100, "#ff0000")
    _write_png(dark / "dark_0_launch.png", 200, 100, "#0000ff")

    plain, labeled = ws_tmp / "plain", ws_tmp / "labeled"
    assert _run(light, dark, plain, "--max-width", "401") == 0
    assert _run(light, dark, labeled, "--max-width", "401", "--labels") == 0

    before = _load(plain / "0_launch.png")
    after = _load(labeled / "0_launch.png")
    assert (before.width(), before.height()) == (after.width(), after.height()) == (401, 100)
    assert before.pixelColor(15, 15).name() == "#ff0000"
    assert after.pixelColor(15, 15).name() != "#ff0000"  # the Light badge covers it
