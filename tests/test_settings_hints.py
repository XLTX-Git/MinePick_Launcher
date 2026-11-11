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

"""Settings labels name the field; the explanation is a hint row under it.

The page used to carry both shapes: some fields had a hint line, others stuffed the explanation
into the label's brackets (and the speed limit label even wrapped because of it). The colour rows
had a second preset swatch grid that overflowed into the next form row. These tests pin the
uniform shape without touching any language table: the split happens at runtime from the same
translated string, so all nine languages get the same treatment.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QFormLayout, QLabel, QWidget


@pytest.fixture()
def app():
    application = QApplication.instance() or QApplication([])
    from gui.theme import apply_theme

    apply_theme("dark", None, None, "default")
    return application


@pytest.mark.parametrize(
    ("text", "label", "hint"),
    [
        ("Java 路径（留空自动探测）", "Java 路径", "留空自动探测"),
        ("下载速度限制\n（KB/s，0=不限）", "下载速度限制", "KB/s，0=不限"),
        ("Java Path (auto-detect if empty)", "Java Path", "auto-detect if empty"),
        ("No brackets here", "No brackets here", ""),
    ],
)
def test_split_label_hint(text, label, hint) -> None:
    from gui.pages.settings_page import split_label_hint

    assert split_label_hint(text) == (label, hint)


def _settings_page(app, ws_tmp, monkeypatch):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_hints"))
    from gui.pages.settings_page import SettingsPage

    window = QWidget()
    window.resize(1000, 700)
    from PySide6.QtWidgets import QVBoxLayout

    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    page = SettingsPage()
    layout.addWidget(page)
    window.show()
    for _ in range(6):
        app.processEvents()
    return window, page


def test_no_label_carries_a_bracketed_explanation(app, ws_tmp, monkeypatch) -> None:
    window, page = _settings_page(app, ws_tmp, monkeypatch)
    try:
        form = page.findChild(QFormLayout)
        labels = [
            item.widget().text()
            for row in range(form.rowCount())
            if (item := form.itemAt(row, QFormLayout.ItemRole.LabelRole)) is not None
            and item.widget() is not None
        ]
        bad = [
            text
            for text in labels
            if ("（" in text and "GB" not in text) or ("(" in text and "GB" not in text)
        ]
        assert not bad, f"labels still carry explanations: {bad}"
        # The Java / speed / proxy / memory / encryption hints exist as hint rows under the fields.
        hints = [
            label.text()
            for label in page.findChildren(QLabel)
            if label.objectName() == "hint" and label.text()
        ]
        assert any("留空自动探测" in text for text in hints)
        assert any("0=不限" in text for text in hints)
        assert any("可选" in text for text in hints)
        assert any("按 mod 数量与可用内存" in text for text in hints)
        assert any("accounts.json 中的敏感令牌" in text for text in hints)
    finally:
        window.close()


def test_colour_rows_are_one_grid_and_have_no_preset_row(app, ws_tmp, monkeypatch) -> None:
    from gui.pages.settings_page import ROW_SPACING

    window, page = _settings_page(app, ws_tmp, monkeypatch)
    try:
        assert not hasattr(page, "accent_preset_buttons")
        assert not hasattr(page, "title_bar_preset_buttons")
        accent = [
            page.accent_edit,
            page.accent_swatch,
            page.accent_default_button,
            page.accent_pick_button,
        ]
        title = [
            page.title_bar_edit,
            page.title_bar_swatch,
            page.title_bar_default_button,
            page.title_bar_pick_button,
        ]
        for left, right in zip(accent, title):
            lx = left.mapTo(page, QPoint(0, 0))
            rx = right.mapTo(page, QPoint(0, 0))
            assert lx.x() == rx.x(), "the two colour rows are not on the same grid"
            assert left.width() == right.width()
        assert page.accent_default_button.text() == page.title_bar_default_button.text()

        # The rows are adjacent form rows: the gap is the page's own ROW_SPACING, and nothing
        # (there is no preset grid any more) can overlap the title-bar row.
        accent_bottom = max(
            widget.mapTo(page, QPoint(0, widget.height())).y() for widget in accent
        )
        title_top = min(widget.mapTo(page, QPoint(0, 0)).y() for widget in title)
        assert title_top - accent_bottom == ROW_SPACING
        for widget in accent:
            rect = QRect(widget.mapTo(page, QPoint(0, 0)), widget.size())
            for other in title:
                other_rect = QRect(other.mapTo(page, QPoint(0, 0)), other.size())
                assert not rect.intersects(other_rect)
    finally:
        window.close()
