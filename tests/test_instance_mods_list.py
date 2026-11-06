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

"""The mods list is a two-line card list with 全部/可更新 tabs, not a sortable table.

The table it replaced carried a header whose sort chevron read as a dropdown, so the regression this
file pins is structural: no header view, no sorting, two tabs that always state their counts, and a
single keyboard stop for the whole list. The "updatable" half is exercised by putting a known
:class:`launcher.mods.identity.Identity` into the page's answer cache - the live recognition path has
its own tests, and this file must not touch the network.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication, QHeaderView

from gui import i18n
from gui.pages.instance_detail import InstanceDetail
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance
from launcher.mods import identity

INSTANCE_ID = "fabric-loader-0.19.3-1.21.11"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _settle(app: QApplication) -> None:
    for _ in range(3):
        for _ in range(6):
            app.processEvents()
        QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _prepare(monkeypatch, ws_tmp) -> tuple[Path, Path]:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "mc"))
    game = ws_tmp / "mc"
    folder = game / "versions" / INSTANCE_ID
    folder.mkdir(parents=True)
    (folder / f"{INSTANCE_ID}.json").write_text(
        json.dumps({"id": INSTANCE_ID, "mainClass": "net.minecraft.client.main.Main"}),
        encoding="utf-8",
    )
    meta = Instance(id=INSTANCE_ID, display_name="Fabric Pack", created_at=1.0, isolated="on")
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")
    mods = folder / "mods"
    mods.mkdir()
    for name, mod_id, version in (
        ("sodium.jar", "sodium", "0.6.5"),
        ("lithium.jar", "lithium", "0.15.3"),
    ):
        with zipfile.ZipFile(mods / name, "w") as archive:
            archive.writestr(
                "fabric.mod.json",
                json.dumps({"id": mod_id, "name": mod_id.title(), "version": version}),
            )
    return game, folder


def _detail(app: QApplication) -> InstanceDetail:
    detail = InstanceDetail()
    detail.set_instance(INSTANCE_ID)
    detail.set_tab(1)  # TAB_MODS
    _settle(app)
    return detail


def test_the_mods_page_has_no_table_header_and_one_keyboard_stop(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        page = detail.stack.widget(1)
        assert page.findChildren(QHeaderView) == [], "a table header came back"
        assert detail.mods_list.count() == 2
        assert detail.mods_list.objectName() == "resourceList"
        # The list, not each row, is the page's keyboard stop for the content area.
        assert detail.mods_list.focusPolicy() & Qt.FocusPolicy.TabFocus
        for row in detail.mods_rows:
            assert row.focusPolicy() == Qt.FocusPolicy.NoFocus
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_zero_updatable_tab_is_still_shown_and_filters_to_an_empty_state(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        tabs = detail.mods_tabs
        assert tabs.all_button.text() == i18n.tr("instances.detail.tab.all", 2)
        assert tabs.update_button.text() == i18n.tr("instances.detail.tab.updatable", 0)
        assert tabs.update_button.isEnabled(), "the tab must say 0 instead of disappearing"

        tabs.set_mode("updates")
        _settle(app)
        assert detail.mods_list.count() == 0
        assert not detail.mods_empty.isHidden()
        assert detail.mods_empty.text() == i18n.tr("instances.detail.tab.updatable.empty")

        tabs.set_mode("all")
        _settle(app)
        assert detail.mods_list.count() == 2
        assert detail.mods_empty.isHidden()
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_a_known_newer_version_marks_the_row_and_fills_the_updatable_tab(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        key = "mod:sodium.jar"
        detail._identity_cache[key] = identity.Identity(
            kind="mod",
            slug="sodium",
            title="Sodium",
            description="Modern rendering engine",
            categories=("optimization", "fabric"),
            icon_url="",
            latest_version="0.7.0",
        )
        identity.set_enabled(True)
        detail._refill_mods()
        _settle(app)
        row = next(row for row in detail.mods_rows if row.key == key)
        assert row.badge == i18n.tr("instances.detail.badge.update")
        assert row.tags == ("optimization", "fabric")
        assert row.description == "Modern rendering engine"
        assert detail.mods_tabs.update_button.text() == i18n.tr(
            "instances.detail.tab.updatable", 1
        )

        detail.mods_tabs.set_mode("updates")
        _settle(app)
        assert [item.key for item in detail.mods_rows] == [key]
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")
