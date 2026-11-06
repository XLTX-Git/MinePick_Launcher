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

"""The instance detail's resource-pack, shader-pack and save pages read the instance folder.

Every row in these tests comes from a real file written into a temporary game directory; the
"enabled" column comes from a real ``options.txt``; a save's name comes from a real (gzipped NBT)
``level.dat``. The pages are therefore checked against the disk, not against fixtures of the
view model, and the failure tests prove an unreadable folder is reported instead of shown as empty.
"""

from __future__ import annotations

import gzip
import json
import os
import struct
import time
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication, QPushButton

from gui import i18n, inwindow_dialog
from gui.pages import instance_detail as detail_module
from gui.pages.instance_detail import (
    TAB_RESOURCEPACKS,
    TAB_SAVES,
    TAB_SHADERPACKS,
    InstanceDetail,
    human_size,
)
from gui.pages.instance_files import format_modified
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance

INSTANCE_ID = "fabric-loader-0.19.3-1.21.11"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


# ---------------------------------------------------------------- fixtures


def _level_dat(level_name: str) -> bytes:
    """A minimal gzip-compressed NBT file with ``Data.LevelName`` - the game's own shape."""
    text = level_name.encode("utf-8")
    string_tag = b"\x08\x00\x09LevelName" + struct.pack(">H", len(text)) + text
    data_compound = b"\x0a\x00\x04Data" + string_tag + b"\x00"
    return gzip.compress(b"\x0a\x00\x00" + data_compound + b"\x00")


def _write_instance(game: Path) -> Path:
    """One isolated instance folder, resolved like the launcher writes it."""
    folder = game / "versions" / INSTANCE_ID
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{INSTANCE_ID}.json").write_text(
        json.dumps({"id": INSTANCE_ID, "mainClass": "net.minecraft.client.main.Main"}),
        encoding="utf-8",
    )
    meta = Instance(
        id=INSTANCE_ID,
        display_name="Fabric Pack",
        note="",
        created_at=1.0,
        isolated="on",
    )
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    return folder


def _prepare(monkeypatch, ws_tmp) -> tuple[Path, Path]:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "mc"))
    game = ws_tmp / "mc"
    folder = _write_instance(game)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")
    return game, folder


def _settle(app: QApplication) -> None:
    """Let every worker finish, including the reload a finished action starts."""
    for _ in range(3):
        for _ in range(8):
            app.processEvents()
        QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(8):
        app.processEvents()


def _detail(app: QApplication, *, tab: int | None = None) -> InstanceDetail:
    detail = InstanceDetail()
    detail.set_instance(INSTANCE_ID)
    _settle(app)
    if tab is not None:
        detail.set_tab(tab)
        _settle(app)
    return detail


def _close(app: QApplication, detail: InstanceDetail) -> None:
    detail.deleteLater()
    app.processEvents()
    i18n.set_language("zh_cn")


def _table_text(detail: InstanceDetail, subdir: str) -> list[list[str]]:
    table = detail.content_tables[subdir]
    rows = []
    for row in range(table.rowCount()):
        rows.append(
            [
                table.item(row, column).text() if table.item(row, column) else ""
                for column in range(table.columnCount())
            ]
        )
    return rows


# ---------------------------------------------------------------- resource packs


def test_resourcepack_rows_show_the_real_files_size_and_enabled_state(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    (packs / "Faithful 32x.zip").write_bytes(b"PK\x03\x04" + b"a" * 5000)
    (packs / "Fresh Animations.zip").write_bytes(b"PK\x03\x04" + b"b" * 900_000)
    # A folder is not a pack file: the list is files only, like the game's own folder listing.
    (packs / "unpacked-pack").mkdir()
    (folder / "options.txt").write_text(
        'resourcePacks:["vanilla","file/Faithful 32x.zip"]\n', encoding="utf-8"
    )

    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        table = detail.content_tables["resourcepacks"]
        assert table.horizontalHeaderItem(2).text() == i18n.tr("instances.detail.col.status")
        assert [row[0] for row in _table_text(detail, "resourcepacks")] == [
            "Faithful 32x.zip",
            "Fresh Animations.zip",
        ]
        assert [row[1] for row in _table_text(detail, "resourcepacks")] == [
            human_size(5004),
            human_size(900_004),
        ]
        assert [row[2] for row in _table_text(detail, "resourcepacks")] == [
            i18n.tr("instances.detail.res.status.enabled"),
            i18n.tr("instances.detail.res.status.disabled"),
        ]
        # Every row carries the letter tile, drawn from its own name.
        for row in range(table.rowCount()):
            assert not table.item(row, 0).icon().isNull()
        assert not table.isHidden()
        assert detail.content_empty["resourcepacks"].isHidden()
    finally:
        _close(app, detail)


def test_shaderpack_rows_show_the_real_files_without_a_fake_status_column(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    shaders = folder / "shaderpacks"
    shaders.mkdir()
    (shaders / "ComplementaryReimagined.zip").write_bytes(b"PK\x03\x04" + b"c" * 2048)

    detail = _detail(app, tab=TAB_SHADERPACKS)
    try:
        table = detail.content_tables["shaderpacks"]
        # options.txt has no shader-pack switch, so the page shows no "enabled" column at all.
        assert table.columnCount() == 2
        assert _table_text(detail, "shaderpacks") == [
            ["ComplementaryReimagined.zip", human_size(2052)]
        ]
        assert not table.item(0, 0).icon().isNull()
    finally:
        _close(app, detail)


# ---------------------------------------------------------------- saves


def test_saves_read_level_name_modified_time_and_size(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    saves = folder / "saves"
    saves.mkdir()
    creative = saves / "world-folder"
    creative.mkdir()
    level_bytes = _level_dat("Creative World")
    (creative / "level.dat").write_bytes(level_bytes)
    (creative / "region").mkdir()
    (creative / "region" / "r.0.0.mca").write_bytes(b"\0" * 4096)
    plain = saves / "survival_2025"
    plain.mkdir()
    (plain / "level.dat").write_bytes(b"\x0a\x00\x00")  # no LevelName: fall back to folder name
    now = time.time()
    os.utime(creative, (now - 60, now - 60))
    os.utime(plain, (now, now))

    detail = _detail(app, tab=TAB_SAVES)
    try:
        table = detail.saves_table
        # Newest first: the folder without a LevelName is the newer one.
        assert [table.item(row, 0).text() for row in range(table.rowCount())] == [
            "survival_2025",
            "Creative World",
        ]
        assert table.item(1, 0).toolTip() == "world-folder"
        assert table.item(1, 1).text() == format_modified(now - 60)
        assert table.item(1, 2).text() == human_size(4096 + len(level_bytes))
        assert not table.isHidden()
        assert detail.saves_empty.isHidden()
        assert detail.nav_badges["saves"].text() == "2"
    finally:
        _close(app, detail)


def test_empty_folders_show_their_own_empty_state_and_an_open_button(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    for name in ("resourcepacks", "shaderpacks", "saves"):
        (folder / name).mkdir()

    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        assert not detail.content_empty["resourcepacks"].isHidden()
        assert (
            detail.content_empty["resourcepacks"].text()
            == i18n.tr("instances.detail.resourcepacks.empty")
        )
        assert detail.content_tables["resourcepacks"].isHidden()
        buttons = [
            button.text()
            for button in detail.stack.widget(TAB_RESOURCEPACKS).findChildren(QPushButton)
        ]
        assert i18n.tr("instances.detail.open_folder") in buttons

        detail.set_tab(TAB_SHADERPACKS)
        _settle(app)
        assert not detail.content_empty["shaderpacks"].isHidden()
        assert (
            detail.content_empty["shaderpacks"].text()
            == i18n.tr("instances.detail.shaderpacks.empty")
        )

        detail.set_tab(TAB_SAVES)
        _settle(app)
        assert not detail.saves_empty.isHidden()
        assert detail.saves_empty.text() == i18n.tr("instances.detail.saves.empty")
        assert detail.saves_table.isHidden()
        assert detail.saves_folder_button.isVisibleTo(detail)
    finally:
        _close(app, detail)


# ---------------------------------------------------------------- deletes


def test_deleting_a_resourcepack_asks_first_with_cancel_as_the_default(
    app, monkeypatch, ws_tmp
):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    victim = packs / "unused.zip"
    victim.write_bytes(b"PK\x03\x04")
    (folder / "options.txt").write_text("resourcePacks:[]\n", encoding="utf-8")

    calls: list[dict] = []

    def refuse(*_args, **kwargs):
        calls.append(kwargs)
        return inwindow_dialog.Outcome.REJECTED

    monkeypatch.setattr(detail_module.inwindow_dialog, "confirm", refuse)
    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        detail.content_tables["resourcepacks"].selectRow(0)
        detail.delete_selected_content("resourcepacks")
        assert calls and calls[0]["default"] is inwindow_dialog.Outcome.REJECTED
        assert calls[0]["accept_text"] == i18n.tr("instances.mods.delete")
        assert victim.exists(), "a cancelled confirmation must not delete anything"
    finally:
        _close(app, detail)


def test_deleting_a_resourcepack_after_confirmation_removes_it(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    victim = packs / "unused.zip"
    victim.write_bytes(b"PK\x03\x04")
    keep = packs / "keep.zip"
    keep.write_bytes(b"PK\x03\x04")
    (folder / "options.txt").write_text("resourcePacks:[]\n", encoding="utf-8")
    monkeypatch.setattr(
        detail_module.inwindow_dialog, "confirm", lambda *_a, **_k: inwindow_dialog.Outcome.ACCEPTED
    )
    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        rows = [
            detail.content_tables["resourcepacks"].item(row, 0).text()
            for row in range(detail.content_tables["resourcepacks"].rowCount())
        ]
        detail.content_tables["resourcepacks"].selectRow(rows.index("unused.zip"))
        detail.delete_selected_content("resourcepacks")
        _settle(app)
        assert not victim.exists()
        assert keep.exists()
        assert [row[0] for row in _table_text(detail, "resourcepacks")] == ["keep.zip"]
    finally:
        _close(app, detail)


def test_deleting_a_save_asks_first_with_cancel_as_the_default(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    saves = folder / "saves"
    saves.mkdir()
    world = saves / "my-world"
    world.mkdir()
    (world / "level.dat").write_bytes(_level_dat("My World"))

    calls: list[dict] = []

    def refuse(*_args, **kwargs):
        calls.append(kwargs)
        return inwindow_dialog.Outcome.REJECTED

    monkeypatch.setattr(detail_module.inwindow_dialog, "confirm", refuse)
    detail = _detail(app, tab=TAB_SAVES)
    try:
        detail.saves_table.selectRow(0)
        detail.delete_selected_saves()
        assert calls and calls[0]["default"] is inwindow_dialog.Outcome.REJECTED
        assert world.is_dir(), "a cancelled confirmation must not delete a world"
    finally:
        _close(app, detail)


def test_deleting_a_save_after_confirmation_removes_the_world(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    saves = folder / "saves"
    saves.mkdir()
    doomed = saves / "doomed"
    doomed.mkdir()
    (doomed / "level.dat").write_bytes(_level_dat("Doomed"))
    keep = saves / "keep"
    keep.mkdir()
    (keep / "level.dat").write_bytes(_level_dat("Keep"))
    now = time.time()
    os.utime(doomed, (now - 60, now - 60))
    os.utime(keep, (now, now))
    monkeypatch.setattr(
        detail_module.inwindow_dialog, "confirm", lambda *_a, **_k: inwindow_dialog.Outcome.ACCEPTED
    )
    detail = _detail(app, tab=TAB_SAVES)
    try:
        assert [detail.saves_table.item(row, 0).text() for row in range(2)] == ["Keep", "Doomed"]
        detail.saves_table.selectRow(1)
        detail.delete_selected_saves()
        _settle(app)
        assert not doomed.exists()
        assert keep.is_dir()
        assert detail.saves_table.rowCount() == 1
        assert detail.saves_table.item(0, 0).text() == "Keep"
    finally:
        _close(app, detail)


# ---------------------------------------------------------------- backup


def test_backing_up_a_save_writes_a_zip_with_the_world_folder(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    saves = folder / "saves"
    saves.mkdir()
    world = saves / "my-world"
    world.mkdir()
    (world / "level.dat").write_bytes(_level_dat("My World"))
    (world / "region").mkdir()
    (world / "region" / "r.0.0.mca").write_bytes(b"\0" * 128)
    (world / "session.lock").write_bytes(b"locked")

    destination = ws_tmp / "backup.zip"
    monkeypatch.setattr(
        detail_module.QFileDialog,
        "getSaveFileName",
        lambda *_a, **_k: (str(destination), ""),
    )
    detail = _detail(app, tab=TAB_SAVES)
    try:
        detail.saves_table.selectRow(0)
        detail.backup_selected_save()
        _settle(app)
        assert destination.is_file()
        with zipfile.ZipFile(destination) as archive:
            names = archive.namelist()
        assert "my-world/level.dat" in names
        assert "my-world/region/r.0.0.mca" in names
        # A running game keeps session.lock open; a backup must not fail (or include) it.
        assert not any(name.endswith("session.lock") for name in names)
        assert world.is_dir(), "a backup never moves or deletes the original"
    finally:
        _close(app, detail)


# ---------------------------------------------------------------- failures


def test_an_unreadable_resourcepack_folder_is_reported_not_shown_as_empty(
    app, monkeypatch, ws_tmp
):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    # A file where the directory should be: resolving/looking inside it raises an OSError.
    (folder / "resourcepacks").write_bytes(b"not a directory")
    statuses: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        detail_module, "set_app_status", lambda _w, text, level=None: statuses.append((text, level))
    )
    detail = _detail(app)
    try:
        detail.reload_resources()
        _settle(app)
        assert statuses, "a failed read must reach the status line"
        assert "读取资源文件失败" in statuses[-1][0]
        assert statuses[-1][1] == "error"
        assert detail.content_tables["resourcepacks"].isHidden()
        assert not detail.content_empty["resourcepacks"].isHidden()
        assert "读取资源文件失败" in detail.content_empty["resourcepacks"].text()
    finally:
        _close(app, detail)


def test_an_unreadable_saves_folder_is_reported_not_shown_as_empty(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    (folder / "saves").write_bytes(b"not a directory")
    statuses: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        detail_module, "set_app_status", lambda _w, text, level=None: statuses.append((text, level))
    )
    detail = _detail(app)
    try:
        detail.reload_saves()
        _settle(app)
        assert statuses and "读取存档失败" in statuses[-1][0]
        assert statuses[-1][1] == "error"
        assert detail.saves_table.isHidden()
        assert not detail.saves_empty.isHidden()
        assert "读取存档失败" in detail.saves_empty.text()
    finally:
        _close(app, detail)


# ---------------------------------------------------------------- reachability


def test_the_new_controls_are_in_the_keyboard_focus_chain(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    for name in ("resourcepacks", "shaderpacks", "saves"):
        (folder / name).mkdir()
    detail = _detail(app, tab=TAB_SAVES)
    try:
        page = detail.stack.widget(TAB_SAVES)
        controls = [
            detail.saves_refresh_button,
            detail.saves_folder_button,
            detail.saves_backup_button,
            detail.saves_delete_button,
        ]
        for button in controls:
            assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
            assert button.isVisibleTo(detail)
        # Walk the widget's focus chain: every one of them is a tab stop in the shown page.
        chain: list[object] = []
        node = detail
        for _ in range(5000):
            chain.append(node)
            node = node.nextInFocusChain()
            if node is detail:
                break
        for button in controls:
            assert button in chain
        for button in page.findChildren(QPushButton):
            assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
    finally:
        _close(app, detail)
