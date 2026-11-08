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

"""The 投影 card of the instance detail page.

The files are real ``.litematic`` documents built by the writer in
``tests/test_schematic_litematic.py`` (gzip + NBT), so the card is tested against the same bytes the
parser tests describe. What is asserted here is the widget contract: row titles are the file names, a
row's second line carries the size, the block kinds and the modified time, a damaged file becomes a
"cannot read" row instead of an exception, selecting a row fills the metadata panel, and an instance
with no schematic folder shows the empty state that says where the files belong.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.pages.instance_detail import InstanceDetail
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance
from tests.test_schematic_litematic import litematic, region, write_litematic

INSTANCE_ID = "fabric-loader-0.19.3-1.21.11"
CREATED_MS = 1_700_000_000_000


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _settle(app: QApplication) -> None:
    for _ in range(6):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(2000)
    for _ in range(10):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(2000)


def _prepare(monkeypatch, ws_tmp, *, files: bool = True):
    """One isolated instance whose game directory the page resolves through the real code path."""
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
    if files:
        _write_house(folder / "schematics" / "cottage.litematic", newest=True)
        _write_tower(folder / "schematics" / "tower.litematic", newest=False)
    return folder


def _write_house(path, *, newest: bool) -> None:
    write_litematic(
        path,
        litematic(
            name="海滨小屋",
            author="Alex",
            description="A small seaside cottage with a red roof.",
            created=CREATED_MS,
            modified=CREATED_MS + 1000,
            enclosing=(128, 64, 96),
            regions=[
                region(
                    "main",
                    (128, 64, 96),
                    states=[
                        ("minecraft:stone", {}),
                        ("minecraft:red_wool", {}),
                        ("minecraft:oak_stairs", {"facing": "north"}),
                    ],
                )
            ],
        ),
    )
    _stamp(path, newest=newest, offset=0)


def _write_tower(path, *, newest: bool) -> None:
    write_litematic(
        path,
        litematic(
            name="Tower",
            author="Steve",
            description="",
            enclosing=None,
            regions=[region("base", (16, 48, 16), states=[("minecraft:stone", {})])],
        ),
    )
    _stamp(path, newest=newest, offset=60 if newest else -60)


def _stamp(path, *, newest: bool, offset: int) -> None:
    stamp = time.time() - 600 + (60 if newest else -60) + offset
    os.utime(path, (stamp, stamp))


def _detail(app: QApplication) -> InstanceDetail:
    detail = InstanceDetail()
    detail.set_instance(INSTANCE_ID)
    _settle(app)
    return detail


def _close(app: QApplication, detail: InstanceDetail) -> None:
    detail.deleteLater()
    app.processEvents()
    i18n.set_language("zh_cn")


def test_the_card_lists_each_file_with_its_size_kinds_and_time(app, monkeypatch, ws_tmp):
    folder = _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        assert detail.schematic_list.count() == 2
        assert [row.title for row in detail.schematic_rows] == ["cottage.litematic",
                                                                "tower.litematic"]
        first = detail.schematic_rows[0]
        assert first.detail.startswith("128×64×96 · 3 种方块 · ")
        second = detail.schematic_rows[1]
        # A file without EnclosingSize still gets a box: the union of its regions.
        assert second.detail.startswith("16×48×16 · 1 种方块 · ")
        assert detail.schematic_list.isHidden() is False
        assert detail.schematic_empty.isHidden() is True
        assert detail.schematic_meta.isHidden() is True
        assert detail.schematic_hint.isHidden() is False
        # The rows are the real files, keyed by path so a later action can resolve them.
        assert first.key == str(folder / "schematics" / "cottage.litematic")
    finally:
        _close(app, detail)


def test_selecting_a_row_shows_its_metadata(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        assert detail.schematic_meta.isHidden() is True
        detail.schematic_list.setCurrentRow(0)
        app.processEvents()
        assert detail.schematic_meta.isHidden() is False
        assert detail.schematic_hint.isHidden() is True
        values = detail.schematic_values
        assert values["name"].text() == "海滨小屋"
        assert values["author"].text() == "Alex"
        assert values["description"].text() == "A small seaside cottage with a red roof."
        expected = time.strftime("%Y/%m/%d %H:%M", time.localtime(CREATED_MS / 1000))
        assert values["created"].text() == expected
        # The row's file name stays visible; the metadata name is the mod's own Name field.
        assert detail.schematic_rows[0].title == "cottage.litematic"
    finally:
        _close(app, detail)


def test_a_damaged_file_becomes_a_cannot_read_row(app, monkeypatch, ws_tmp):
    folder = _prepare(monkeypatch, ws_tmp)
    (folder / "schematics" / "broken.litematic").write_bytes(b"\x1f\x8b\x08\x00not really")
    detail = _detail(app)
    try:
        rows = {row.title: row for row in detail.schematic_rows}
        assert set(rows) == {"cottage.litematic", "tower.litematic", "broken.litematic"}
        broken = rows["broken.litematic"]
        assert broken.detail == i18n.tr("instances.detail.schematic.unreadable")
        # Selecting it must not fill the panel with the previous file's text.
        index = [row.title for row in detail.schematic_rows].index("broken.litematic")
        detail.schematic_list.setCurrentRow(index)
        app.processEvents()
        assert detail.schematic_meta.isHidden() is True
        assert detail.schematic_hint.isHidden() is False
        # The good rows still parse: one broken download does not blank the card.
        assert rows["cottage.litematic"].detail.startswith("128×64×96")
    finally:
        _close(app, detail)


def test_an_instance_without_schematics_shows_the_empty_state(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp, files=False)
    detail = _detail(app)
    try:
        assert detail.schematic_list.count() == 0
        assert detail.schematic_list.isHidden() is True
        assert detail.schematic_empty.isHidden() is False
        assert detail.schematic_empty_title.text() == i18n.tr("instances.detail.schematic.empty")
        hint = detail.schematic_empty_hint.text()
        assert "schematics/" in hint
        assert "Litematica" in hint
        assert detail.schematic_meta.isHidden() is True
    finally:
        _close(app, detail)


def test_switching_the_instance_away_clears_the_rows(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        assert detail.schematic_list.count() == 2
        detail.set_instance(None)
        _settle(app)
        assert detail.schematic_list.count() == 0
        assert detail.schematic_list.isHidden() is True
        assert detail.schematic_empty.isHidden() is False
    finally:
        _close(app, detail)


def test_refresh_picks_up_a_file_written_after_the_first_scan(app, monkeypatch, ws_tmp):
    folder = _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        assert detail.schematic_list.count() == 2
        _write_tower(folder / "schematics" / "tower2.litematic", newest=True)
        detail.reload_schematics()
        _settle(app)
        assert [row.title for row in detail.schematic_rows] == [
            "tower2.litematic",
            "cottage.litematic",
            "tower.litematic",
        ]
    finally:
        _close(app, detail)


def test_a_superseded_scan_result_is_ignored(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        expected = [row.title for row in detail.schematic_rows]
        detail._fill_schematics((detail._schematic_generation - 1, []))
        assert [row.title for row in detail.schematic_rows] == expected
    finally:
        _close(app, detail)


def test_the_folder_button_opens_the_selected_files_folder(app, monkeypatch, ws_tmp):
    folder = _prepare(monkeypatch, ws_tmp)
    opened: list = []
    monkeypatch.setattr(InstanceDetail, "_open_path", staticmethod(lambda target: opened.append(target)))
    detail = _detail(app)
    try:
        detail.schematic_folder_button.click()
        assert opened == [folder / "schematics"]
        detail.schematic_list.setCurrentRow(0)
        detail.schematic_folder_button.click()
        assert opened[-1] == folder / "schematics"
        # Without a schematic folder, the button still has the default place to open (and create).
        detail._clear_schematics()
        detail.schematic_folder_button.click()
        assert opened[-1] == folder / "schematics"
    finally:
        _close(app, detail)


def test_the_list_is_a_keyboard_stop_and_drives_the_metadata(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app)
    try:
        # The list is one focus stop (not one per row), so sixty schematics add sixty rows but a
        # single Tab stop; the arrow keys are what move the selection, as on every other card list.
        assert detail.schematic_list.focusPolicy() != Qt.FocusPolicy.NoFocus
        detail.schematic_list.setCurrentRow(0)
        detail.schematic_list.setCurrentRow(1)
        app.processEvents()
        assert detail.schematic_values["name"].text() == "Tower"
        assert detail.schematic_meta.isHidden() is False
    finally:
        _close(app, detail)
