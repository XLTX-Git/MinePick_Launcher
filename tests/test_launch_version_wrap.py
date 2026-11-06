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

"""The launch page's version hero line: the Japanese sentence wraps instead of being clipped.

The 360px panel leaves the 20px hero label 284px beside the block icon. The Japanese
"no version selected" sentence is 300px wide, so before the fix the label drew its first 284px and
dropped the rest. The fix is ``setWordWrap(True)`` on that one label, and the tests pin the three
things the change must not trade away:

* the full sentence is drawn (the label is exactly as tall as the wrapped text needs);
* the other eight languages keep their one-line height and every widget keeps its exact geometry;
* the wrapped second line does not push any control out of the panel.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui import i18n

FONT_FILES = (    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/malgun.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
)
#: Every language whose "no version selected" sentence fits the 284px single-line budget.
SINGLE_LINE_LANGUAGES = tuple(code for code, _name in i18n.UI_LANGUAGES if code != "ja_jp")
#: The widgets below the version label, plus the panel that has to keep all of them.
BELOW_LABEL = (
    "version_meta",
    "account_label",
    "account_button",
    "launch_button",
    "select_instance_button",
    "instance_settings_button",
    "last_launch_label",
)


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def launch_page(app, monkeypatch, ws_tmp):
    """Build a launch page for one language with nothing installed, laid out at the real size."""
    from PySide6.QtGui import QFontDatabase

    from gui.pages.launch_page import LaunchPage
    from gui.theme import apply_theme

    for font_name in FONT_FILES:
        font_file = Path(font_name)
        if font_file.exists():
            QFontDatabase.addApplicationFont(str(font_file))
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "game"
    game.mkdir()
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))
    apply_theme("light", None, None, "default")
    built = []

    def build(code: str, *, word_wrap: bool | None = None):
        """A laid-out launch page in the "nothing installed" state ``word_wrap`` selects."""
        i18n.set_language(code)
        page = LaunchPage()
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.resize(1000, 610)
        page.show()
        for _ in range(6):
            app.processEvents()
        page.set_version_id("")  # the state the audit reported: no version to show
        if word_wrap is not None:
            page.version_label.setWordWrap(word_wrap)
        page.layout().invalidate()
        page.layout().activate()
        for _ in range(4):
            app.processEvents()
        built.append(page)
        return page

    try:
        yield build
    finally:
        for page in built:
            page.close()
            page.deleteLater()
        i18n.set_language("zh_cn")
        apply_theme("dark", None, None, "default")


def _needed_height(label) -> int:
    """The height a word-wrapped label needs for its whole text at its real width."""
    metrics = label.fontMetrics()
    return metrics.boundingRect(
        0, 0, label.width(), 10_000, Qt.TextFlag.TextWordWrap, label.text()
    ).height()


def _geometry(widget, page) -> tuple[int, int, int, int]:
    point = widget.mapTo(page, widget.rect().topLeft())
    return point.x(), point.y(), widget.width(), widget.height()


def test_the_japanese_sentence_used_to_be_clipped_and_now_is_fully_drawn(launch_page):
    clipped = launch_page("ja_jp", word_wrap=False)
    label = clipped.version_label
    assert label.text() == i18n.tr("launch.version.none")
    assert label.fontMetrics().horizontalAdvance(label.text()) > label.width()
    assert label.height() < _needed_height(label)  # the reported bug: the tail is dropped

    fixed = launch_page("ja_jp")
    fixed_label = fixed.version_label
    assert fixed_label.wordWrap() is True
    assert fixed_label.height() == _needed_height(fixed_label)
    assert fixed_label.heightForWidth(fixed_label.width()) == fixed_label.height()
    assert fixed_label.height() > fixed_label.fontMetrics().height()  # it really used two lines


def test_the_other_eight_languages_keep_their_one_line_layout(launch_page):
    """Wrap must be invisible to every language whose sentence fits: same height, same geometry."""
    for code in SINGLE_LINE_LANGUAGES:
        wrapped = launch_page(code)
        plain = launch_page(code, word_wrap=False)
        label = wrapped.version_label
        assert label.text() == i18n.tr("launch.version.none")
        assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width(), (
            f"{code}: the sentence no longer fits one line, so this test is out of date"
        )
        assert label.height() == label.fontMetrics().height()
        assert _needed_height(label) == label.height()
        assert _geometry(label, wrapped) == _geometry(plain.version_label, plain)
        for name in BELOW_LABEL:
            assert _geometry(getattr(wrapped, name), wrapped) == _geometry(
                getattr(plain, name), plain
            ), f"{code}: {name} moved because of the wrap"
        assert _geometry(wrapped.panel, wrapped) == _geometry(plain.panel, plain)


def test_wrapped_japanese_keeps_every_control_inside_the_panel(launch_page):
    page = launch_page("ja_jp")
    panel = page.panel
    bounds = panel.rect()
    for name in ("version_label", *BELOW_LABEL):
        widget = getattr(page, name)
        top_left = widget.mapTo(panel, widget.rect().topLeft())
        bottom_right = widget.mapTo(panel, widget.rect().bottomRight())
        assert bounds.contains(top_left), f"{name} starts outside the panel: {top_left}"
        assert bounds.contains(bottom_right), f"{name} ends outside the panel: {bottom_right}"
    # the second line must not be painted over the meta line below it
    label = page.version_label
    assert label.mapTo(page, label.rect().bottomLeft()).y() < page.version_meta.mapTo(
        page, page.version_meta.rect().topLeft()
    ).y()
