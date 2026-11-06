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

"""The card lists' online half: local artwork wins, the switch off means no request, and a late
project icon repaints only the rows that asked for it.

The recognition answers come from a fake ``identity.lookup`` and the icon store is stubbed, so these
tests can count requests and observe the first paint (the neutral cube) and the second one (the real
picture) without touching Modrinth or the developer's icon cache.
"""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.pages import instance_detail as detail_module
from gui.pages.instance_detail import (
    TAB_MODS,
    TAB_RESOURCEPACKS,
    TAB_SAVES,
    TAB_SHADERPACKS,
    InstanceDetail,
)
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance
from launcher.mods import identity
from launcher.net import http_cache

INSTANCE_ID = "fabric-loader-0.19.3-1.21.11"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _png_bytes(color: str, size: int = 8) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


BLUE = _png_bytes("#0000ff")
MAGENTA = _png_bytes("#ff00ff")


def _settle(app: QApplication, detail: InstanceDetail | None = None) -> None:
    for _ in range(6):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(2000)
    if detail is not None:
        assert detail._identity_queue.wait_idle(5.0)
    for _ in range(12):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(2000)


def _prepare(monkeypatch, ws_tmp, *, packs: int = 1, shaders: int = 2) -> tuple[Path, Path]:
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
    with zipfile.ZipFile(mods / "sodium.jar", "w") as archive:
        archive.writestr(
            "fabric.mod.json",
            json.dumps({"id": "sodium", "name": "Sodium", "version": "0.6.5"}),
        )

    resourcepacks = folder / "resourcepacks"
    resourcepacks.mkdir()
    with zipfile.ZipFile(resourcepacks / "Faithful 32x.zip", "w") as archive:
        archive.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 34}}))
        archive.writestr("pack.png", MAGENTA)

    shaderpacks = folder / "shaderpacks"
    shaderpacks.mkdir()
    for index in range(shaders):
        (shaderpacks / f"BSL_v8.4.{index}.zip").write_bytes(b"PK\x03\x04" + bytes([index]))

    saves = folder / "saves"
    world = saves / "my-world"
    world.mkdir(parents=True)
    (world / "icon.png").write_bytes(MAGENTA)
    return game, folder


def _identity(icon_url: str = "https://cdn.example/x.png", *, latest: str = "", title: str = "Project"):
    return identity.Identity(
        kind="shader",
        slug="project",
        title=title,
        description="A short summary",
        categories=("tag-one", "tag-two"),
        icon_url=icon_url,
        latest_version=latest,
    )


def _detail(app: QApplication, tab: int) -> InstanceDetail:
    detail = InstanceDetail()
    detail.set_instance(INSTANCE_ID)
    detail.set_tab(tab)
    _settle(app, detail)
    return detail


def _centre(pixmap) -> QColor:
    image = pixmap.toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


def _enable_online(monkeypatch, value) -> None:
    monkeypatch.setattr(detail_module, "online_identity_enabled", lambda: True)
    identity.set_enabled(True)
    monkeypatch.setattr(identity, "lookup", lambda *args, **kwargs: value)


def test_a_shader_pack_uses_the_online_icon_when_it_has_no_local_artwork(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    _enable_online(monkeypatch, _identity())
    monkeypatch.setattr(http_cache, "cached_icon_bytes", lambda _url: BLUE)
    monkeypatch.setattr(
        http_cache,
        "fetch_icon_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("the disk cache had the icon")),
    )
    detail = _detail(app, TAB_SHADERPACKS)
    try:
        row = detail.content_rows["shaderpacks"][0]
        assert row.icon_pixmap() is not None
        assert _centre(row.icon_pixmap()) == QColor("#0000ff")
        assert row.tags == ("tag-one", "tag-two")
        assert row.description == "A short summary"
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_a_pack_png_wins_over_the_online_icon(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    _enable_online(monkeypatch, _identity())
    monkeypatch.setattr(http_cache, "cached_icon_bytes", lambda _url: BLUE)
    monkeypatch.setattr(
        http_cache,
        "fetch_icon_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("local artwork must win")),
    )
    detail = _detail(app, TAB_RESOURCEPACKS)
    try:
        row = detail.content_rows["resourcepacks"][0]
        assert row.artwork_is_local is True
        assert _centre(row.icon_pixmap()) == QColor("#ff00ff"), "the pack's own pack.png must win"
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_a_save_uses_the_worlds_own_screenshot(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    detail = _detail(app, TAB_SAVES)
    try:
        row = detail.saves_rows[0]
        assert row.icon_pixmap() is not None
        assert _centre(row.icon_pixmap()) == QColor("#ff00ff")
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_an_unrecognized_row_says_so_and_draws_the_cube(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    _enable_online(monkeypatch, None)
    monkeypatch.setattr(http_cache, "cached_icon_bytes", lambda _url: None)
    detail = _detail(app, TAB_MODS)
    try:
        row = detail.mods_rows[0]
        assert row.detail == i18n.tr("instances.detail.identity.unknown")
        assert row.icon_pixmap() is None, "a jar without artwork shows the neutral cube"
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_the_switch_off_means_local_information_and_zero_requests(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    calls: list[object] = []
    monkeypatch.setattr(
        identity, "lookup", lambda *args, **kwargs: calls.append("lookup") or _identity()
    )
    monkeypatch.setattr(
        http_cache,
        "cached_icon_bytes",
        lambda *_a: (_ for _ in ()).throw(AssertionError("no icon may be looked up")),
    )
    monkeypatch.setattr(
        http_cache,
        "fetch_icon_bytes",
        lambda *_a: (_ for _ in ()).throw(AssertionError("no icon may be downloaded")),
    )
    identity.set_enabled(True)
    detail = InstanceDetail()
    detail.set_instance(INSTANCE_ID)
    detail.set_tab(TAB_MODS)
    _settle(app, detail)
    try:
        row = detail.mods_rows[0]
        assert calls == []
        assert row.detail == i18n.tr("instances.detail.identity.off")
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_a_cold_icon_shows_the_cube_first_then_repaints_only_its_rows(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp, shaders=12)
    _enable_online(monkeypatch, _identity("https://cdn.example/shared.png"))
    monkeypatch.setattr(http_cache, "cached_icon_bytes", lambda _url: None)
    release = threading.Event()
    started = threading.Event()
    downloaded: list[str] = []

    def fetch(url: str) -> bytes:
        downloaded.append(url)
        started.set()
        release.wait(5.0)
        return BLUE

    monkeypatch.setattr(http_cache, "fetch_icon_bytes", fetch)

    detail = InstanceDetail()
    detail.set_instance(INSTANCE_ID)
    detail.set_tab(TAB_SHADERPACKS)
    for _ in range(300):
        app.processEvents()
        if started.is_set():
            break
        time.sleep(0.02)
    try:
        assert started.is_set(), "the icon download never started"
        rows = detail.content_rows["shaderpacks"]
        assert all(row.icon_pixmap() is None for row in rows), "the first paint is the cube"
        scrollbar = detail.content_lists["shaderpacks"].verticalScrollBar()
        scrollbar.setValue(min(2, scrollbar.maximum()))
        scroll_before = scrollbar.value()

        release.set()
        _settle(app, detail)
        assert all(row.icon_pixmap() is not None for row in rows)
        assert all(_centre(row.icon_pixmap()) == QColor("#0000ff") for row in rows)
        assert downloaded.count("https://cdn.example/shared.png") == 1, (
            "one URL must be downloaded once, however many rows share it"
        )
        assert scrollbar.value() == scroll_before, "a late icon must not move the list"
    finally:
        release.set()
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")


def test_a_corrupt_cached_icon_is_dropped_and_downloaded_again(app, monkeypatch, ws_tmp):
    _prepare(monkeypatch, ws_tmp)
    _enable_online(monkeypatch, _identity())
    monkeypatch.setattr(http_cache, "cached_icon_bytes", lambda _url: b"definitely not an image")
    monkeypatch.setattr(http_cache, "fetch_icon_bytes", lambda _url: BLUE)
    forgotten: list[str] = []
    monkeypatch.setattr(identity, "forget_cached_icon", lambda url: forgotten.append(url) or True)

    detail = _detail(app, TAB_SHADERPACKS)
    try:
        row = detail.content_rows["shaderpacks"][0]
        assert forgotten, "a corrupt cache entry must be dropped"
        assert _centre(row.icon_pixmap()) == QColor("#0000ff")
        assert row.icon_pixmap() is not None
    finally:
        detail.deleteLater()
        app.processEvents()
        i18n.set_language("zh_cn")
