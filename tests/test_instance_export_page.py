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
"""导出: the modpack form (name/version/categories/advanced/destination) and what it writes.

The form is the page's own state: the checkboxes decide the archive, 打包资源文件 decides where the
jars travel, the destination is remembered per instance, and a second export of the same instance is
refused instead of started. These tests drive the page (not only the exporter), with the download
lookup stubbed out so nothing touches the network.
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool

from gui import i18n
from gui.pages import instance_detail
from gui.pages.instance_detail import TAB_EXPORT
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance
from launcher.modpack import CurseforgeOutcome, MatchOutcome

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(instance_detail, "fetch_catalog", list)
    monkeypatch.setattr(instance_detail, "list_loader_versions", lambda *a, **kw: [])
    monkeypatch.setattr(instance_detail, "_CATALOG_CACHE", None)

    real = instance_detail.export_modpack

    def offline(instance_id, dest, game_dir, **kwargs):
        kwargs.setdefault("matcher", lambda *_a: MatchOutcome(failed=True))
        kwargs.setdefault("curseforge_matcher", lambda *_a: CurseforgeOutcome())
        return real(instance_id, dest, game_dir, **kwargs)

    monkeypatch.setattr(instance_detail, "export_modpack", offline)
    yield


def _write_profile(game: Path, version_id: str, *, inherits: str | None = None) -> Path:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    raw = {"id": version_id, "type": "release", "mainClass": "x.Main", "libraries": []}
    if inherits:
        raw["inheritsFrom"] = inherits
    (folder / f"{version_id}.json").write_text(json.dumps(raw), encoding="utf-8")
    (folder / f"{version_id}.jar").write_bytes(b"jar")
    return folder


def _game(ws_tmp: Path) -> Path:
    game = ws_tmp / "mc"
    _write_profile(game, "1.21.11")
    _write_profile(game, "fabric-loader-0.19.3-1.21.11", inherits="1.21.11")
    folder = _write_profile(game, "fabric-loader-0.19.3-1.21.11")
    meta = Instance(id="fabric-loader-0.19.3-1.21.11", display_name="测试包", created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    (folder / "mods" / "sodium.jar").write_bytes(b"PK\x03\x04sodium")
    (folder / "config").mkdir()
    (folder / "config" / "sodium.json").write_text('{"q":true}', encoding="utf-8")
    (folder / "saves" / "world").mkdir(parents=True)
    (folder / "saves" / "world" / "level.dat").write_bytes(b"world")
    return game


def _settle(app, window) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _pump(app, predicate=None, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate is None or predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate and predicate())


@pytest.fixture()
def window(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = _game(ws_tmp)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.selected_instance = ""
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    from gui.main_window import NAV_KEYS, MainWindow

    built = MainWindow()
    built.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    built.resize(1180, 760)
    built.show()
    _settle(app, built)
    built.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, built)
    page = built.pages["instances"]
    for _ in range(300):
        app.processEvents()
        if page.open_instance("fabric-loader-0.19.3-1.21.11"):
            break
        time.sleep(0.01)
    else:  # pragma: no cover - a missing fixture is a red test, not a silent skip
        raise AssertionError("the instance never appeared in the list")
    _settle(app, built)
    detail = page.detail
    detail.set_tab(TAB_EXPORT)
    _settle(app, built)
    yield built, game, detail
    i18n.set_language("zh_cn")
    built.close()
    built.deleteLater()


def test_the_category_rows_show_counts_and_the_summary_follows_the_boxes(window):
    _built, _game, detail = window
    assert detail._export_ready
    mods = detail.export_category_rows["mods"][1].text()
    assert mods == i18n.tr("instances.detail.export.category.stat", 1, "10 B")
    assert detail.export_category_rows["saves"][1].text() == i18n.tr(
        "instances.detail.export.category.stat", 1, "5 B"
    )
    assert detail.export_pack_summary.text() == i18n.tr(
        "instances.detail.export.pack.summary.selected", 2, "20 B"
    )

    # Unticking the mods leaves only the config file, and the summary follows immediately.
    detail.export_category_rows["mods"][0].setChecked(False)
    assert detail.export_pack_summary.text() == i18n.tr(
        "instances.detail.export.pack.summary.selected", 1, "10 B"
    )
    detail.export_category_rows["config"][0].setChecked(False)
    assert detail.export_pack_summary.text() == i18n.tr(
        "instances.detail.export.pack.summary.none"
    )
    assert not detail.export_pack_button.isEnabled()


def test_the_name_and_version_are_written_into_the_archive(app, window):
    _built, _game, detail = window
    detail.export_name_edit.setText("红石整合")
    detail.export_version_edit.setText("2.3.0")
    dest = Path(detail.game_dir).parent / "named.mrpack"
    detail.export_pack_to(dest)
    assert _pump(app, lambda: detail.export_pack_result.isVisibleTo(detail))
    with zipfile.ZipFile(dest) as archive:
        index = json.loads(archive.read("modrinth.index.json"))
    assert index["name"] == "红石整合"
    assert index["versionId"] == "2.3.0"


def test_the_form_round_trips_through_the_saved_config(app, window, monkeypatch):
    _built, _game, detail = window
    detail.export_name_edit.setText("存档包")
    detail.export_version_edit.setText("9.9.9")
    detail.export_category_rows["mods"][0].setChecked(False)
    detail.export_category_rows["saves"][0].setChecked(True)
    detail.export_embed_check.setChecked(True)
    detail.save_export_config()
    preset = config_mod.load()[0].export_presets["fabric-loader-0.19.3-1.21.11"]
    assert preset.name == "存档包"
    assert preset.version == "9.9.9"
    assert "mods" not in preset.categories and "saves" in preset.categories
    assert preset.embed_files is True

    # A fresh page starts from the defaults; 读取配置 brings the saved form back.
    detail.export_name_edit.setText("别的")
    detail.export_version_edit.setText("1.0.0")
    detail.export_category_rows["mods"][0].setChecked(True)
    detail.export_category_rows["saves"][0].setChecked(False)
    detail.export_embed_check.setChecked(False)
    detail.load_export_config()
    assert detail.export_name_edit.text() == "存档包"
    assert detail.export_version_edit.text() == "9.9.9"
    assert not detail.export_category_rows["mods"][0].isChecked()
    assert detail.export_category_rows["saves"][0].isChecked()
    assert detail.export_embed_check.isChecked()


def test_a_remembered_destination_is_reused_and_an_invalid_one_is_reported(
    app, window, monkeypatch, ws_tmp
):
    _built, _game, detail = window
    folder = ws_tmp / "exports"
    folder.mkdir()
    detail._remember_export_folder(folder)
    assert detail.export_destination.text() == i18n.tr(
        "instances.detail.export.destination", str(folder)
    )

    def no_dialog(*_args, **_kwargs):
        raise AssertionError("a remembered folder must not open the save dialog")

    monkeypatch.setattr(instance_detail.QFileDialog, "getSaveFileName", no_dialog)
    detail.export_pack()
    dest = folder / "测试包 1.0.0.mrpack"
    assert _pump(app, lambda: dest.is_file())
    assert _pump(app, lambda: detail.export_pack_result.isVisibleTo(detail))

    # The remembered folder disappears: the page says so and asks for a new one.
    import shutil

    shutil.rmtree(folder)
    detail._fill_export()
    assert detail.export_destination.text() == i18n.tr(
        "instances.detail.export.destination.invalid", str(folder)
    )
    chosen = ws_tmp / "again.mrpack"
    monkeypatch.setattr(
        instance_detail.QFileDialog,
        "getSaveFileName",
        lambda *a, **k: (str(chosen), ""),
    )
    detail.export_pack()
    assert _pump(app, lambda: chosen.is_file())


def test_a_second_export_of_the_same_instance_is_refused(app, window, monkeypatch):
    _built, _game, detail = window
    seen: list[str] = []

    class _Running:
        def is_running(self):
            return True

        def title(self):
            return i18n.tr("tasks.title.export", detail.instance_id)

    detail._pack_task = _Running()
    monkeypatch.setattr(
        instance_detail.inwindow_dialog,
        "information",
        lambda _parent, title, text: seen.append(text),
    )
    detail.export_pack()
    assert seen == [i18n.tr("instances.detail.export.busy")]
    detail._pack_task = None


def test_embedding_is_disabled_for_curseforge_with_the_reason(window):
    _built, _game, detail = window
    detail.export_pack_format.setCurrentIndex(1)  # CurseForge
    assert not detail.export_embed_check.isEnabled()
    assert detail.export_embed_hint.text() == i18n.tr(
        "instances.detail.export.embed.curseforge"
    )
    detail.export_pack_format.setCurrentIndex(0)  # back to Modrinth
    assert detail.export_embed_check.isEnabled()
    assert detail.export_embed_hint.text() == i18n.tr(
        "instances.detail.export.embed.hint"
    )
