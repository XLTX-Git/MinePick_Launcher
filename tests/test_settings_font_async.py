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

"""The UI-font dropdown is usable on the first frame and complete once the worker returns.

``QFontDatabase.families()`` is the slow call this page used to make synchronously in its
constructor: hundreds of milliseconds to seconds on a machine with many fonts, paid before the
settings page could paint. The page now offers the default plus the configured family immediately,
and a worker replaces that placeholder with the full list.
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from gui import i18n


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn():
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


def _wait_for_workers(app, timeout_ms: int = 5000) -> None:
    QThreadPool.globalInstance().waitForDone(timeout_ms)
    for _ in range(6):
        app.processEvents()


def _texts(combo) -> list[str]:
    return [combo.itemText(index) for index in range(combo.count())]


def test_font_list_fills_in_the_background_and_the_dropdown_is_never_empty(
    app, monkeypatch, ws_tmp
):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    from launcher import config

    cfg, cfg_path = config.load()
    cfg.ui_font = "Consolas"
    config.save(cfg, cfg_path)

    from gui.pages import settings_page

    release = threading.Event()
    slow_families = [
        "Consolas",
        "Arial",
        "A Font Family That Is Definitely Not Installed",
    ]

    def families():
        release.wait(5.0)
        return slow_families

    monkeypatch.setattr(settings_page, "_installed_font_families", families)

    started = time.monotonic()
    page = settings_page.SettingsPage()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, (
        f"SettingsPage() waited {elapsed:.2f}s for the font scan; the constructor must not call "
        "QFontDatabase.families() itself"
    )

    # The placeholder is real and selectable: default + the configured family. Opening the dropdown
    # on this frame shows a non-empty list instead of an empty popup.
    placeholder = _texts(page.font_combo)
    assert page.font_combo.count() >= 2
    assert i18n.tr("settings.font.default") in placeholder
    assert "Consolas" in placeholder
    assert slow_families[-1] not in placeholder, "the worker's answer arrived before it was released"
    assert page.font_combo.findData("Consolas") >= 0

    # ... and the popup's own model is that non-empty list, not an empty view.
    page.font_combo.showPopup()
    app.processEvents()
    assert page.font_combo.view().model().rowCount() >= 2
    page.font_combo.hidePopup()
    app.processEvents()

    release.set()
    _wait_for_workers(app)

    texts = _texts(page.font_combo)
    assert slow_families[-1] in texts, "the worker's font list never reached the combo"
    assert i18n.tr("settings.font.common") in texts
    header_index = texts.index(i18n.tr("settings.font.common"))
    header_item = page.font_combo.model().item(header_index)
    assert header_item is not None and not header_item.isEnabled()
    assert page.font_combo.findData("Consolas") >= 0
    assert page.font_combo.currentData() == "Consolas", "the selection was lost when the list filled"
    completer = page.font_combo.completer()
    assert completer is not None and slow_families[-1] in completer.model().stringList()


def test_the_scan_result_does_not_write_the_config(app, monkeypatch, ws_tmp):
    """Filling the list is not a user change: the autosave must not fire while the combo is rebuilt."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data2"))
    from launcher import config

    cfg, cfg_path = config.load()
    cfg.ui_font = "Arial"
    config.save(cfg, cfg_path)
    writes: list[int] = []
    original_save = config.save

    def counting_save(*args, **kwargs):
        writes.append(1)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(config, "save", counting_save)

    from gui.pages import settings_page

    monkeypatch.setattr(
        settings_page, "_installed_font_families", lambda: ["Arial", "Consolas", "Georgia"]
    )
    page = settings_page.SettingsPage()
    _wait_for_workers(app)
    assert _texts(page.font_combo).count("Georgia") == 1
    assert writes == [], (
        "rebuilding the font combo wrote the config; filling the list is not a settings change"
    )
    assert page.font_combo.currentData() == "Arial"
