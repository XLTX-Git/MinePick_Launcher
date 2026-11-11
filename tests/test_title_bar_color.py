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

"""Custom title-bar colour: derived text/hover/capsule tokens, config round-trip, settings save.

The colour is picked for looks, so every dependent role is measured rather than assumed: the user's
white, black, mid-grey, pale yellow and deep purple are exercised as the extremes the derivation has
to survive. The settings page owns the other half: a valid hex is stored and applied at once, an
invalid one is reported and leaves the stored value alone.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QWidget

from gui import i18n, theme
from launcher import config

EXTREMES = ("#ffffff", "#000000", "#808080", "#ffff00", "#4b0082")


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn():
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


@pytest.fixture(autouse=True)
def _restore_default_theme():
    """Keep a custom title-bar test from leaking its applied colour into the next test."""
    yield
    theme.apply_theme("dark", "", None, "default", "")


@pytest.mark.parametrize("mode", ("dark", "light"))
@pytest.mark.parametrize("color", EXTREMES)
def test_custom_color_overrides_the_bar_and_keeps_text_at_aa(color: str, mode: str) -> None:
    palette = theme.theme_palette(mode, None, color)
    assert palette["BG_TOPBAR"] == color
    assert theme.contrast_ratio(palette["FG_TOPBAR"], palette["BG_TOPBAR"]) >= 4.5


@pytest.mark.parametrize("color", EXTREMES)
def test_window_button_hovers_clear_the_non_text_minimum(color: str) -> None:
    palette = theme.theme_palette("dark", None, color)
    for token in ("BG_TITLEBAR_CLOSE", "BG_TITLEBAR_MINIMIZE"):
        assert palette[token] != color, token
        assert theme.contrast_ratio(palette["FG_TOPBAR"], palette[token]) >= 3.0, token


@pytest.mark.parametrize("color", EXTREMES)
def test_link_capsule_stays_readable_on_the_custom_bar(color: str) -> None:
    palette = theme.theme_palette("dark", None, color)
    assert palette["BG_TOPBAR_CAPSULE"] != color
    assert theme.contrast_ratio(palette["FG_TOPBAR"], palette["BG_TOPBAR_CAPSULE"]) >= 4.5


def test_empty_value_follows_the_theme_default() -> None:
    assert theme.theme_palette("light", None, "")["BG_TOPBAR"] == "#1e6fd9"
    assert theme.theme_palette("dark", None, "")["BG_TOPBAR"] == "#1a4e8c"
    for mode in ("dark", "light"):
        palette = theme.theme_palette(mode, None, "")
        assert palette["BG_TOPBAR"] == theme.NEUTRAL_TOKENS["BG_TOPBAR"][mode]
        assert palette["FG_TOPBAR"] == "#ffffff"
        assert "BG_TOPBAR_CAPSULE" not in palette
    assert theme.theme_palette("dark", None, "not-a-color") == theme.theme_palette("dark")


def test_the_same_custom_color_can_derive_different_text_per_theme() -> None:
    """A mid-grey where both white and black clear AA: each theme prefers its own side."""
    color = "#767676"
    dark = theme.theme_palette("dark", None, color)
    light = theme.theme_palette("light", None, color)
    assert dark["FG_TOPBAR"] == "#ffffff"
    assert light["FG_TOPBAR"] == "#000000"
    assert theme.contrast_ratio(dark["FG_TOPBAR"], color) >= 4.5
    assert theme.contrast_ratio(light["FG_TOPBAR"], color) >= 4.5


def test_config_round_trip(ws_tmp) -> None:
    path = ws_tmp / "config.json"
    assert config.LauncherConfig().title_bar_color == ""
    cfg = config.LauncherConfig(title_bar_color="#e08a3c")
    config.save(cfg, path)
    loaded, _ = config.load(path)
    assert loaded.title_bar_color == "#e08a3c"


def test_window_buttons_and_capsule_read_the_derived_tokens(app, monkeypatch) -> None:
    monkeypatch.setattr(theme, "_last_applied", None)
    theme.apply_theme("dark", "", None, "default", "#808080")
    tokens = theme.current_tokens()

    host = QWidget()
    from gui.main_window import _LinkCapsule
    from gui.title_bar import TitleBar

    bar = TitleBar(host, "MinePick Launcher")
    capsule = _LinkCapsule(host)
    try:
        assert tokens["FG_TOPBAR"] in bar.close_button.styleSheet()
        assert tokens["BG_TITLEBAR_CLOSE"] in bar.close_button.styleSheet()
        assert tokens["FG_TOPBAR"] in capsule.styleSheet()
        assert tokens["BG_TOPBAR_CAPSULE"] in capsule.styleSheet()
    finally:
        bar.deleteLater()
        capsule.deleteLater()
        host.deleteLater()
        app.processEvents()


def test_settings_page_applies_and_resets_a_valid_color(app, monkeypatch, ws_tmp) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_valid"))
    from gui.pages import settings_page

    page = settings_page.SettingsPage()
    app.processEvents()
    try:
        theme_name = page.theme_combo.currentData() or "dark"
        expected_default = theme.theme_palette(theme_name)["BG_TOPBAR"]
        assert page.title_bar_edit.text() == ""
        assert page.title_bar_edit.placeholderText() == expected_default
        page.title_bar_edit.setText("#E08A3C")
        page.save()
        stored, _ = config.load()
        assert stored.title_bar_color == "#e08a3c"
        assert theme.current_tokens()["BG_TOPBAR"] == "#e08a3c"

        page.title_bar_default_button.click()
        app.processEvents()
        stored, _ = config.load()
        assert stored.title_bar_color == ""
        assert page.title_bar_edit.text() == ""
        assert page.title_bar_edit.placeholderText() == expected_default
        assert theme.current_tokens()["BG_TOPBAR"] == expected_default
        assert theme.current_tokens()["BG_TOPBAR"] != "#e08a3c"
    finally:
        page.deleteLater()
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(5000)


def test_placeholder_and_swatch_track_the_current_theme(app, monkeypatch, ws_tmp) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_theme_tracking"))
    from gui.pages import settings_page

    page = settings_page.SettingsPage()
    app.processEvents()
    try:
        page.theme_combo.setCurrentIndex(page.theme_combo.findData("light"))
        app.processEvents()
        assert page.title_bar_edit.placeholderText() == "#1e6fd9"
        assert "#1e6fd9" in page.title_bar_swatch.styleSheet()

        page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
        app.processEvents()
        assert page.title_bar_edit.placeholderText() == "#1a4e8c"
        assert "#1a4e8c" in page.title_bar_swatch.styleSheet()
    finally:
        page.deleteLater()
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(5000)


def test_invalid_color_is_reported_and_does_not_write_config(app, monkeypatch, ws_tmp) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_invalid"))
    from gui.pages import settings_page

    cfg, path = config.load()
    cfg.title_bar_color = "#123456"
    config.save(cfg, path)

    page = settings_page.SettingsPage()
    app.processEvents()
    messages: list[str] = []
    monkeypatch.setattr(
        settings_page, "set_app_status", lambda _w, text, level=None: messages.append(text)
    )
    try:
        page.title_bar_edit.setText("zzzzzz")
        page.save()
        stored, _ = config.load()
        assert stored.title_bar_color == "#123456", "an invalid value must not reach the config"
        assert page.title_bar_edit.text() == "zzzzzz", "the field keeps the text so it can be fixed"
        assert messages[-1] == i18n.tr("settings.title_bar.invalid")
    finally:
        page.deleteLater()
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(5000)


def test_apply_theme_reapplies_when_only_the_title_bar_color_changes(app, monkeypatch) -> None:
    monkeypatch.setattr(theme, "_last_applied", None)
    app.setStyleSheet("")
    calls: list[str] = []
    original = QApplication.setStyleSheet

    def recording(self, sheet):  # type: ignore[no-untyped-def]
        calls.append(sheet)
        return original(self, sheet)

    monkeypatch.setattr(QApplication, "setStyleSheet", recording)

    theme.apply_theme("dark", "", None, "default", "#112233")
    assert len(calls) == 1, calls
    assert "#112233" in calls[0]
    assert "#112233" in theme.current_tokens()["BG_TOPBAR"]

    calls.clear()
    theme.apply_theme("dark", "", None, "default", "#112233")
    assert calls == [], "an unchanged title-bar colour re-polished the whole application"

    theme.apply_theme("dark", "", None, "default", "#445566")
    assert len(calls) == 2 and calls[0] == "", calls
    assert "#445566" in calls[1]
    assert "#112233" not in calls[1]


def test_switching_theme_with_an_empty_value_reapplies_the_theme_bar(app, monkeypatch) -> None:
    """The default is not frozen: the bar follows the theme, including through a real re-apply."""
    monkeypatch.setattr(theme, "_last_applied", None)
    app.setStyleSheet("")

    theme.apply_theme("dark", "", None, "default", "")
    assert theme.current_tokens()["BG_TOPBAR"] == "#1a4e8c"
    assert "#1a4e8c" in app.styleSheet()

    theme.apply_theme("light", "", None, "default", "")
    assert theme.current_tokens()["BG_TOPBAR"] == "#1e6fd9"
    assert "#1e6fd9" in app.styleSheet()


def test_switching_theme_keeps_a_custom_bar_and_rederives_its_text(app, monkeypatch) -> None:
    monkeypatch.setattr(theme, "_last_applied", None)
    app.setStyleSheet("")
    color = "#8b5cf6"

    theme.apply_theme("dark", "", None, "default", color)
    dark = theme.current_tokens()
    assert dark["BG_TOPBAR"] == color
    assert theme.contrast_ratio(dark["FG_TOPBAR"], color) >= 4.5

    theme.apply_theme("light", "", None, "default", color)
    light = theme.current_tokens()
    assert light["BG_TOPBAR"] == color, "the user's colour does not change with the theme"
    assert theme.contrast_ratio(light["FG_TOPBAR"], color) >= 4.5
    assert light["FG_TOPBAR"] == theme.theme_palette("light", None, color)["FG_TOPBAR"]
