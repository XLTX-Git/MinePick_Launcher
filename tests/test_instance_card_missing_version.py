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

"""The instance card when the folder's version profile cannot be read.

A hand-made pack names its profile ``version.json``; a damaged install may have lost the profile and
kept only the jar. The first card must still show 版本 · 加载器, the second must say 版本信息缺失,
and neither may take the page (or the rest of the list) down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication

from gui import i18n
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance

PROFILE = {
    "id": "我的整合包",
    "type": "release",
    "mainClass": "net.minecraft.client.main.Main",
    "assetIndex": {"id": "5", "url": "https://example.invalid/5.json"},
    "assets": "5",
    "libraries": [],
}


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """The GUI suite's offline stubs: no network, no Java probe outliving the window."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _settle(app) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _sidecar(folder: Path, instance_id: str) -> None:
    meta = Instance(id=instance_id, created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )


def _window(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "mc"
    versions = game / "versions"

    hand = versions / "我的整合包"
    hand.mkdir(parents=True)
    (hand / "version.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    _sidecar(hand, "我的整合包")

    broken = versions / "26.3-snapshot-8"
    broken.mkdir(parents=True)
    (broken / "26.3-snapshot-8.jar").write_bytes(b"jar")
    _sidecar(broken, "26.3-snapshot-8")

    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.selected_instance = ""
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    from gui.main_window import MainWindow

    window = MainWindow()
    window.resize(1180, 760)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    _settle(app)
    return window, game


def _close(app, window) -> None:
    window.close()
    window.deleteLater()
    for _ in range(4):
        app.processEvents()
    i18n.set_language("zh_cn")


def test_cards_show_the_renamed_profile_and_survive_a_missing_one(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        from gui.main_window import NAV_KEYS

        window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        _settle(app)
        page = window.pages["instances"]

        hand = page.card_for("我的整合包")
        broken = page.card_for("26.3-snapshot-8")

        # Both folders are instances: one has a version.json, the other still holds its jar.
        assert hand is not None, [card.instance_id for card in page.cards()]
        assert broken is not None, [card.instance_id for card in page.cards()]

        # The hand-made pack is labelled from version.json ...
        assert hand.meta_label.text() == "我的整合包 · " + i18n.tr("instances.card.vanilla")
        assert i18n.tr("instances.card.version_missing") not in hand.meta_label.text()

        # ... while the damaged one says exactly what is missing, and keeps its action.
        assert broken.meta_label.text() == i18n.tr("instances.card.version_missing")
        assert broken.use_button.isEnabled()
        assert broken in page.cards()
    finally:
        if window is not None:
            _close(app, window)
