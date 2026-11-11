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

"""The category chips are pills: the checked fill is rounded, not a square green block.

The user reported the selected 模组 chip as a square green rectangle. The cause was geometry, not a
missing pseudo-state: the chip had no min-height, so it was 19px tall while the rule asked for a
16px radius. Qt silently paints a square fill when the radius is larger than half the widget, which
is exactly the reported shape. These tests pin both halves of the fix: a 32px-tall chip and a
radius declared in the state rules too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parents[1]
SHEETS = (ROOT / "gui/resources/style.qss", ROOT / "gui/resources/style_light.qss")


@pytest.fixture()
def app():
    application = QApplication.instance() or QApplication([])
    from gui.theme import apply_theme

    apply_theme("dark", None, None, "default")
    return application


def _chip_row() -> tuple[QWidget, QPushButton]:
    window = QWidget()
    window.setObjectName("resourcesPage")
    layout = QVBoxLayout(window)
    row = QHBoxLayout()
    chips = []
    for text in ("模组", "整合包", "资源包", "光影"):
        chip = QPushButton(text)
        chip.setObjectName("resourceChip")
        chip.setCheckable(True)
        row.addWidget(chip)
        chips.append(chip)
    row.addStretch(1)
    layout.addLayout(row)
    layout.addStretch(1)
    window.show()
    chips[0].setChecked(True)
    return window, chips[0]


def test_checked_resource_chip_is_a_rounded_pill(app) -> None:
    from gui.theme import current_tokens

    window, chip = _chip_row()
    try:
        for _ in range(6):
            app.processEvents()
        assert chip.height() == 32, (
            f"chip is {chip.height()}px tall; the 16px radius needs a 32px box or Qt squares it"
        )
        image = QImage(chip.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        chip.render(image)
        for point in ((0, 0), (1, 1), (2, 2)):
            assert image.pixelColor(*point).alpha() == 0, (
                f"corner {point} is filled: the checked chip was painted as a square"
            )
        assert image.pixelColor(chip.width() // 2, chip.height() // 2).alpha() == 255
        accent = current_tokens()["ACCENT_BUTTON"].lstrip("#")
        expected = (int(accent[0:2], 16), int(accent[2:4], 16), int(accent[4:6], 16))
        centre = image.pixelColor(chip.width() // 2, chip.height() // 2)
        assert (centre.red(), centre.green(), centre.blue()) == expected
    finally:
        window.close()
        app.processEvents()


def _block(text: str, selector: str) -> str:
    start = text.index(selector)
    return text[start : text.index("}", start)]


def test_both_sheets_declare_the_chip_geometry_and_state_radii() -> None:
    for sheet in SHEETS:
        text = sheet.read_text(encoding="utf-8")
        base = _block(text, "QPushButton#resourceChip {")
        assert "min-height: 30px;" in base, sheet.name
        assert "border-radius: 16px;" in base, sheet.name
        checked = _block(text, "QPushButton#resourceChip:checked {")
        assert "border-radius: 16px;" in checked, sheet.name
        hover = _block(text, "QPushButton#resourceChip:hover")
        assert "border-radius: 16px;" in hover, sheet.name

        tab_base = _block(text, "QPushButton#categoryTab {")
        assert "border-radius: 15px;" in tab_base, sheet.name
        tab_checked = _block(text, "QPushButton#categoryTab:checked {")
        assert "border-radius: 15px;" in tab_checked, sheet.name
        tab_hover = _block(text, "QPushButton#categoryTab:hover")
        assert "border-radius: 15px;" in tab_hover, sheet.name
