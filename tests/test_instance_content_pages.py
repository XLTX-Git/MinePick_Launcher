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

"""The instance detail's mod, resource-pack, shader-pack and save lists read the instance folder.

Every row in these tests comes from a real file written into a temporary game directory; the
"enabled" mark comes from a real ``options.txt``; a save's name and creation time come from a real
(gzipped NBT) ``level.dat``. The pages are therefore checked against the disk, not against fixtures
of the view model, and the failure tests prove an unreadable folder is reported instead of shown as
empty. The online half of a row is off for the entire file (the suite-wide switch), so a row here is
exactly what the disk says.
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
from PySide6.QtCore import QBuffer, Qt, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QPushButton

from gui import i18n, inwindow_dialog, paint_utils, theme
from gui.pages import instance_detail as detail_module
from gui.pages.instance_detail import (
    CONTENT_ICON_SIZE,
    CONTENT_ROW_HEIGHT,
    TAB_MODS,
    TAB_RESOURCEPACKS,
    TAB_SAVES,
    TAB_SHADERPACKS,
    InstanceDetail,
    human_size,
)
from gui.pages.instance_files import format_day
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance

INSTANCE_ID = "fabric-loader-0.19.3-1.21.11"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


# ---------------------------------------------------------------- fixtures


def _level_dat(level_name: str, created_ms: int | None = None) -> bytes:
    """A minimal gzip-compressed NBT file with ``Data.LevelName`` (and ``Data.Created``)."""
    text = level_name.encode("utf-8")
    string_tag = b"\x08\x00\x09LevelName" + struct.pack(">H", len(text)) + text
    long_tag = (
        b"\x04\x00\x07Created" + struct.pack(">q", created_ms) if created_ms is not None else b""
    )
    data_compound = b"\x0a\x00\x04Data" + string_tag + long_tag + b"\x00"
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


def _rows(detail: InstanceDetail, subdir: str):
    return detail.content_rows[subdir]


def _png_bytes(color: str, size: int = 8) -> bytes:
    """A real, decodable PNG of one solid colour (a pack.png, not just a signature)."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _write_pack(path: Path, icon: bytes | None = None, *, extra: str | None = None) -> None:
    """A real resource-pack zip: pack.mcmeta plus the optional root pack.png."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 34}}))
        if icon is not None:
            archive.writestr("pack.png", icon)
        if extra is not None:
            archive.writestr(extra, b"not an icon")


def _write_mod(
    path: Path, *, mod_id: str, name: str, version: str, icon: bytes | None = None
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "fabric.mod.json",
            json.dumps({"id": mod_id, "name": name, "version": version}),
        )
        if icon is not None:
            archive.writestr("icon.png", icon)


def _painted_image(row) -> QImage:
    return row.painted_icon_pixmap().toImage()


def _painted_pixels(image: QImage) -> list[tuple[int, QColor]]:
    return [
        (image.pixelColor(x, y).alpha(), image.pixelColor(x, y))
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    ]


def _hint_color() -> QColor:
    tokens = theme.current_tokens() or theme.theme_palette("light")
    return QColor(tokens["FG_HINT"])


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
        rows = _rows(detail, "resourcepacks")
        assert [row.title for row in rows] == ["Faithful 32x.zip", "Fresh Animations.zip"]
        assert [row.meta for row in rows] == [human_size(5004), human_size(900_004)]
        assert [row.status for row in rows] == [
            i18n.tr("instances.detail.res.status.enabled"),
            i18n.tr("instances.detail.res.status.disabled"),
        ]
        # Every row owns one 32px icon slot: its pack.png when present, the neutral cube otherwise.
        for row in rows:
            image = _painted_image(row)
            assert image.width() == CONTENT_ICON_SIZE
            assert _painted_pixels(image), "the neutral cube must actually draw something"
        # The tabs sit above the list and always name both counts.
        tabs = detail.content_tabs["resourcepacks"]
        assert tabs.all_button.text() == i18n.tr("instances.detail.tab.all", 2)
        assert tabs.update_button.text() == i18n.tr("instances.detail.tab.updatable", 0)
        assert not detail.content_lists["resourcepacks"].isHidden()
        assert detail.content_empty["resourcepacks"].isHidden()
    finally:
        _close(app, detail)


def test_shaderpack_rows_show_the_real_files_without_a_fake_status_mark(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    shaders = folder / "shaderpacks"
    shaders.mkdir()
    (shaders / "ComplementaryReimagined.zip").write_bytes(b"PK\x03\x04" + b"c" * 2048)

    detail = _detail(app, tab=TAB_SHADERPACKS)
    try:
        rows = _rows(detail, "shaderpacks")
        assert [(row.title, row.meta) for row in rows] == [
            ("ComplementaryReimagined.zip", human_size(2052))
        ]
        # options.txt has no shader-pack switch, so the row carries no status at all.
        assert rows[0].status == ""
        # No shader pack ships a pack.png (measured: none of BSL / Complementary / Photon / SEUS),
        # so the row carries the neutral cube rather than a letter tile or an empty slot.
        assert rows[0].icon_pixmap() is None
        image = _painted_image(rows[0])
        assert image.width() == CONTENT_ICON_SIZE
        assert _painted_pixels(image)
    finally:
        _close(app, detail)


# ---------------------------------------------------------------- pack icons


def test_a_pack_png_is_the_row_icon_and_a_missing_one_is_the_neutral_cube(
    app, monkeypatch, ws_tmp
):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    _write_pack(packs / "Painted.zip", _png_bytes("#ff00ff"))
    _write_pack(packs / "No Art.zip")
    _write_pack(packs / "Corrupt.zip", b"not a real png")

    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        rows = {row.title: row for row in _rows(detail, "resourcepacks")}
        real = rows["Painted.zip"].icon_pixmap()
        assert real is not None, "a real pack.png must be decoded into the icon slot"
        real_image = real.toImage()
        assert real_image.width() == CONTENT_ICON_SIZE and real_image.height() == CONTENT_ICON_SIZE
        # The 8x8 solid pack.png is scaled into the 32px slot, so its centre is exactly its colour.
        assert real_image.pixelColor(CONTENT_ICON_SIZE // 2, CONTENT_ICON_SIZE // 2) == QColor(
            "#ff00ff"
        )

        for label in ("No Art.zip", "Corrupt.zip"):
            assert rows[label].icon_pixmap() is None, "no artwork must mean the neutral placeholder"
            neutral = _painted_image(rows[label])
            assert neutral.size() == real_image.size(), "the placeholder must use the same slot"
            # No background block and no filled faces: the corners of the 32px slot stay transparent.
            assert neutral.pixelColor(2, 2).alpha() == 0
            assert neutral.pixelColor(CONTENT_ICON_SIZE - 3, CONTENT_ICON_SIZE - 3).alpha() == 0
            body = [
                color
                for alpha, color in _painted_pixels(neutral)
                if 140 <= alpha <= 190
            ]
            assert body, "no stroke at the expected FG_HINT opacity"
            hint = _hint_color()
            assert any(
                abs(color.red() - hint.red()) <= 3
                and abs(color.green() - hint.green()) <= 3
                and abs(color.blue() - hint.blue()) <= 3
                for color in body
            ), "the placeholder is not drawn in FG_HINT"
    finally:
        _close(app, detail)


def test_content_rows_never_draw_a_letter_tile(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    for name in ("resourcepacks", "shaderpacks"):
        (folder / name).mkdir()
    (folder / "resourcepacks" / "Plain.zip").write_bytes(b"PK\x03\x04")
    (folder / "shaderpacks" / "Shaders.zip").write_bytes(b"PK\x03\x04")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a letter tile was drawn for a content row")

    monkeypatch.setattr(paint_utils, "letter_tile_pixmap", forbidden)
    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        for subdir in ("resourcepacks", "shaderpacks"):
            rows = _rows(detail, subdir)
            assert len(rows) == 1
            image = _painted_image(rows[0])
            assert image.width() == CONTENT_ICON_SIZE
            assert _painted_pixels(image)
    finally:
        _close(app, detail)


def test_a_zip_read_error_falls_back_to_the_cube_without_crashing(app, monkeypatch, ws_tmp):
    """An encrypted/unreadable pack.png is just "no icon"; the row still shows its file name."""
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    _write_pack(packs / "BrokenRead.zip", _png_bytes("#ff00ff"))

    def explode(self, name, pwd=None):
        raise RuntimeError("encrypted entry")

    monkeypatch.setattr(zipfile.ZipFile, "read", explode)
    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        rows = _rows(detail, "resourcepacks")
        assert len(rows) == 1
        assert rows[0].title == "BrokenRead.zip"
        image = _painted_image(rows[0])
        assert image.width() == CONTENT_ICON_SIZE
        assert _painted_pixels(image), "a failed read must still leave a visible placeholder"
    finally:
        _close(app, detail)


def test_pack_icons_are_read_locally_without_the_network(app, monkeypatch, ws_tmp):
    from launcher.net import http_cache

    calls: list[str] = []

    def fail(name):
        def _fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not be reached while filling a local pack list")

        return _fail

    monkeypatch.setattr(http_cache, "fetch_icon_bytes", fail("fetch_icon_bytes"))
    monkeypatch.setattr(http_cache, "cached_icon_bytes", fail("cached_icon_bytes"))
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    _write_pack(packs / "Local.zip", _png_bytes("#3366cc"))

    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        assert len(_rows(detail, "resourcepacks")) == 1
        assert calls == [], "a pack icon must never trigger a network fetch"
    finally:
        _close(app, detail)


# ---------------------------------------------------------------- display names


def test_coded_names_are_stripped_for_display_but_kept_on_disk_and_in_the_tooltip(
    app, monkeypatch, ws_tmp
):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    coded = "\u00a7cCozyUI\u00a7b+ \u00a77v1.10\u00a7c no-fonts \u00a70.zip"
    hexed = "\u00a7x\u00a7f\u00a7f\u00a7a\u00a7a\u00a70\u00a70VanillaTweaks\u00a7r \u00a77v1.2.zip"
    _write_pack(packs / coded)
    _write_pack(packs / hexed)
    (packs / "\u00a7c").write_bytes(b"PK\x03\x04")  # stripping leaves nothing: keep the raw name

    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        rows = _rows(detail, "resourcepacks")
        shown = [row.title for row in rows]
        assert "CozyUI+ v1.10 no-fonts .zip" in shown
        assert "VanillaTweaks v1.2.zip" in shown
        assert "\u00a7c" in shown, "a name with nothing left after stripping keeps its raw form"
        assert all("\u00a7" not in text for text in shown if text != "\u00a7c")

        row = rows[shown.index("CozyUI+ v1.10 no-fonts .zip")]
        assert coded in row.toolTip(), "the tooltip carries the name as it is on disk"
        # The disk is untouched: only the row's display text differs.
        assert {path.name for path in packs.iterdir()} == {coded, hexed, "\u00a7c"}
    finally:
        _close(app, detail)


def test_deleting_a_coded_pack_uses_the_real_file_name(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    packs = folder / "resourcepacks"
    packs.mkdir()
    victim = packs / "\u00a7cUnused\u00a7r.zip"
    victim.write_bytes(b"PK\x03\x04")
    keep = packs / "Keep.zip"
    keep.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(
        detail_module.inwindow_dialog, "confirm", lambda *_a, **_k: inwindow_dialog.Outcome.ACCEPTED
    )

    detail = _detail(app, tab=TAB_RESOURCEPACKS)
    try:
        rows = _rows(detail, "resourcepacks")
        index = next(i for i, row in enumerate(rows) if row.title == "Unused.zip")
        detail.content_lists["resourcepacks"].setCurrentRow(index)
        detail.delete_selected_content("resourcepacks")
        _settle(app)
        assert not victim.exists(), "the delete used the stripped display name"
        assert keep.exists()
    finally:
        _close(app, detail)


def test_the_mods_row_strips_codes_and_delete_uses_the_real_name(
    app, monkeypatch, ws_tmp
):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    jar = folder / "mods" / "\u00a7a[sodium]\u00a7r sodium-fabric-0.6.5.jar"
    _write_mod(jar, mod_id="sodium", name="Sodium", version="0.6.5")
    monkeypatch.setattr(detail_module.inwindow_dialog, "question", lambda *_a, **_k: True)

    detail = _detail(app, tab=TAB_MODS)
    try:
        rows = detail.mods_rows
        index = next(i for i, row in enumerate(rows) if "sodium" in row.meta)
        row = rows[index]
        # The first line carries the display name and the local identity, not the file name.
        assert row.meta == "sodium · 0.6.5"
        assert jar.name in row.toolTip(), "the tooltip shows the name as it is on disk"
        assert jar.exists(), "display stripping must not rename the file"

        detail.mods_list.setCurrentRow(index)
        detail.delete_selected_mods()
        _settle(app)
        assert not jar.exists(), "the delete used the stripped display name"
    finally:
        _close(app, detail)


def test_saves_read_level_name_created_time_modified_time_and_size(app, monkeypatch, ws_tmp):
    _game, folder = _prepare(monkeypatch, ws_tmp)
    saves = folder / "saves"
    saves.mkdir()
    created = int(time.time() * 1000) - 86400 * 30 * 1000
    creative = saves / "world-folder"
    creative.mkdir()
    level_bytes = _level_dat("Creative World", created)
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
        rows = detail.saves_rows
        # Newest first: the folder without a LevelName is the newer one.
        assert [row.title for row in rows] == ["survival_2025", "Creative World"]
        assert rows[0].meta == ""
        plain_size = (plain / "level.dat").stat().st_size
        assert human_size(plain_size) in rows[0].detail
        assert rows[1].meta == ""
        assert human_size(4096 + len(level_bytes)) in rows[1].detail
        # The second line is the world's own dates, not the folder's ("created 30 days ago").
        assert format_day(created / 1000.0) in rows[1].detail
        assert format_day(now - 60) in rows[1].detail
        assert "world-folder" in rows[1].toolTip()
        assert not detail.saves_list.isHidden()
        assert detail.saves_empty.isHidden()
        assert detail.nav_badges["saves"].text() == "2"
        assert detail.saves_rows[0].height() >= CONTENT_ROW_HEIGHT
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
        assert detail.content_lists["resourcepacks"].count() == 0
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
        assert detail.saves_list.count() == 0
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
        detail.content_lists["resourcepacks"].setCurrentRow(0)
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
        rows = _rows(detail, "resourcepacks")
        index = next(i for i, row in enumerate(rows) if row.title == "unused.zip")
        detail.content_lists["resourcepacks"].setCurrentRow(index)
        detail.delete_selected_content("resourcepacks")
        _settle(app)
        assert not victim.exists()
        assert keep.exists()
        assert [row.title for row in _rows(detail, "resourcepacks")] == ["keep.zip"]
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
        detail.saves_list.setCurrentRow(0)
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
        assert [row.title for row in detail.saves_rows] == ["Keep", "Doomed"]
        detail.saves_list.setCurrentRow(1)
        detail.delete_selected_saves()
        _settle(app)
        assert not doomed.exists()
        assert keep.is_dir()
        assert len(detail.saves_rows) == 1
        assert detail.saves_rows[0].title == "Keep"
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
        detail.saves_list.setCurrentRow(0)
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
        assert detail.content_lists["resourcepacks"].count() == 0
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
        assert detail.saves_list.count() == 0
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
        # The list is the single keyboard stop of a card view (its rows are not Tab stops).
        assert detail.saves_list.focusPolicy() & Qt.FocusPolicy.TabFocus
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
        assert detail.saves_list in chain
        for button in page.findChildren(QPushButton):
            assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
    finally:
        _close(app, detail)
