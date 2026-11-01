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

"""Game folders: the stored list, the `game_dir` alias, and the settings page's switch.

Three layers are covered here because a folder is only useful when they agree:

* the config model - a config that predates the list still reads as one folder and still resolves the
  same directory (`game_dir` is the *current* folder, not a second truth);
* the settings page - add / set-current / remove, the confirmation, and the two refusals (the last
  folder, and a switch while a download or a launch is using the folder);
* the window - switching actually refreshes the pages that answer "what is installed here".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from launcher import config as config_mod
from launcher import paths


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _offline_pages(monkeypatch):
    """Building a page must not reach the network (the suite's rule, shared with tests/test_gui.py)."""

    def _fail(**kwargs):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)


def _write_version(game: Path, version_id: str) -> None:
    """A minimal profile list_instances() accepts: versions/<id>/<id>.json."""
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(
        json.dumps({"id": version_id, "mainClass": "net.minecraft.client.main.Main"}),
        encoding="utf-8",
    )


def _use_data_dir(monkeypatch, ws_tmp: Path) -> Path:
    """Point config.load() at a fresh launcher data dir and return the expected config path."""
    data = ws_tmp / "data"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data))
    return data / config_mod.CONFIG_FILENAME


def _folder_paths(cfg) -> list[str]:
    return [str(folder.path) for folder in cfg.game_folders]


# ---------------------------------------------------------------- 1. the legacy config


def test_legacy_config_with_only_game_dir_reads_as_one_folder(monkeypatch, ws_tmp):
    """An old config (one `game_dir`, no list) migrates to one current folder - same directory.

    This is the compatibility guarantee: every existing reader keeps resolving exactly the same path
    after the migration, and the folder list does not invent a second folder.
    """
    config_path = _use_data_dir(monkeypatch, ws_tmp)
    game = ws_tmp / "legacy-mc"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"game_dir": str(game), "memory_gb": 6}), encoding="utf-8"
    )

    cfg, loaded_path = config_mod.load(config_path)
    assert loaded_path == config_path
    assert cfg.memory_gb == 6.0  # the rest of the file is untouched
    assert cfg.game_dir == game  # the alias still names the same directory
    assert len(cfg.game_folders) == 1
    folder = cfg.game_folders[0]
    assert folder.path == game
    assert folder.name == "legacy-mc"  # the directory name is the default label
    assert folder.is_default is True
    assert config_mod.active_game_dir(cfg) == game

    # The instance registry - the reader that used to consume game_dir directly - resolves it.
    _write_version(game, "1.20.1")
    from launcher.instances import list_instances

    assert set(list_instances()) == {"1.20.1"}


def test_legacy_config_saves_back_as_the_same_directory(monkeypatch, ws_tmp):
    config_path = _use_data_dir(monkeypatch, ws_tmp)
    game = ws_tmp / "legacy-mc"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"game_dir": str(game)}), encoding="utf-8")

    cfg, _ = config_mod.load(config_path)
    config_mod.save(cfg, config_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert Path(raw["game_dir"]) == game
    assert [Path(entry["path"]) for entry in raw["game_folders"]] == [game]
    assert raw["game_folders"][0]["is_default"] is True
    again, _ = config_mod.load(config_path)
    assert again == cfg


def test_a_config_without_a_folder_keeps_the_default_rule():
    """No `game_dir` and no list: the env/platform default rule still applies, as it always did."""
    cfg = config_mod.LauncherConfig()
    assert cfg.game_dir is None
    assert cfg.game_folders == []
    assert config_mod.active_game_dir(cfg) == paths.default_game_dir()


def test_direct_game_dir_write_joins_the_folder_list_on_save(monkeypatch, ws_tmp):
    """The wizard and older code write `game_dir` alone; the list must not silently override it."""
    config_path = _use_data_dir(monkeypatch, ws_tmp)
    first = ws_tmp / "first"
    second = ws_tmp / "second"
    cfg, _ = config_mod.load(config_path)
    cfg.game_dir = first
    config_mod.save(cfg, config_path)

    cfg, _ = config_mod.load(config_path)
    cfg.game_dir = second  # what _on_wizard_finished() does
    config_mod.save(cfg, config_path)

    reloaded, _ = config_mod.load(config_path)
    assert reloaded.game_dir == second
    assert [Path(folder.path) for folder in reloaded.game_folders] == [first, second]
    assert [folder.is_default for folder in reloaded.game_folders] == [False, True]
    assert config_mod.active_game_dir(reloaded) == second


# ---------------------------------------------------------------- 2. the folder operations


def test_add_set_current_and_remove(ws_tmp):
    game_a = ws_tmp / "a"
    game_b = ws_tmp / "b"
    cfg = config_mod.LauncherConfig(game_dir=game_a)
    assert len(cfg.game_folders) == 1  # the constructor migrated the single folder

    folder, created = cfg.add_game_folder(game_b, name="Bee")
    assert created is True
    assert folder.name == "Bee"
    assert len(cfg.game_folders) == 2
    assert cfg.game_dir == game_a  # adding never switches the current folder

    assert cfg.set_active_game_folder(game_b) is True
    assert cfg.game_dir == game_b
    assert [folder.is_default for folder in cfg.game_folders] == [False, True]
    assert config_mod.active_game_dir(cfg) == game_b

    assert cfg.remove_game_folder(game_b) is True  # the current folder: the other one is promoted
    assert cfg.game_dir == game_a
    assert len(cfg.game_folders) == 1
    assert cfg.active_folder().path == game_a


def test_duplicate_and_unknown_paths_are_handled(ws_tmp):
    game = ws_tmp / "mc"
    cfg = config_mod.LauncherConfig(game_dir=game)

    # The same path spelled with a trailing separator is still the same folder.
    duplicate, created = cfg.add_game_folder(str(game) + os.sep)
    assert created is False
    assert duplicate.path == game
    assert len(cfg.game_folders) == 1

    assert cfg.set_active_game_folder(ws_tmp / "missing") is False
    assert cfg.remove_game_folder(ws_tmp / "missing") is False
    assert cfg.rename_game_folder(ws_tmp / "missing", "x") is False
    assert cfg.game_dir == game
    assert len(cfg.game_folders) == 1


def test_the_last_folder_cannot_be_removed(ws_tmp):
    game = ws_tmp / "only"
    cfg = config_mod.LauncherConfig(game_dir=game)
    assert cfg.remove_game_folder(game) is False
    assert cfg.game_dir == game
    assert len(cfg.game_folders) == 1


def test_folder_name_defaults_and_renames(ws_tmp):
    game = ws_tmp / "My Pack"
    cfg = config_mod.LauncherConfig(game_dir=game)
    folder = cfg.active_folder()
    assert folder.name == "My Pack"
    assert cfg.rename_game_folder(game, "红石整合包") is True
    assert cfg.active_folder().name == "红石整合包"
    assert cfg.rename_game_folder(game, "   ") is True  # empty falls back to the directory name
    assert cfg.active_folder().name == "My Pack"


def test_folders_are_deduplicated_and_names_filled_on_load(monkeypatch, ws_tmp):
    config_path = _use_data_dir(monkeypatch, ws_tmp)
    game = ws_tmp / "mc"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "game_folders": [
                    {"path": str(game), "name": ""},
                    {"path": str(game) + os.sep, "name": "duplicate"},
                    {"path": "", "name": "empty"},
                    {"nope": 1},
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg, _ = config_mod.load(config_path)
    assert [Path(folder.path) for folder in cfg.game_folders] == [game]
    assert cfg.game_folders[0].name == "mc"
    assert cfg.game_folders[0].is_default is True
    assert cfg.game_dir == game


def test_effective_folders_show_the_implicit_default(ws_tmp):
    """A fresh config still has one folder in effect; the settings page has to show it."""
    cfg = config_mod.LauncherConfig()
    effective = cfg.effective_game_folders()
    assert len(effective) == 1
    assert effective[0].path == paths.default_game_dir()
    assert effective[0].is_default is True
    assert cfg.game_folders == []  # reading the page does not silently write a folder


# ---------------------------------------------------------------- 3. the settings page


def _settings_page(app, monkeypatch, ws_tmp, game_a: Path, game_b: Path):
    _use_data_dir(monkeypatch, ws_tmp)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game_a
    config_mod.save(cfg, cfg_path)
    cfg, cfg_path = config_mod.load()
    cfg.add_game_folder(game_b)
    config_mod.save(cfg, cfg_path)

    from gui.pages.settings_page import SettingsPage

    page = SettingsPage()
    return page


def test_settings_page_lists_folders_and_disables_only_the_last_remove(
    app, monkeypatch, ws_tmp
):
    game_a = ws_tmp / "a"
    game_b = ws_tmp / "b"
    page = _settings_page(app, monkeypatch, ws_tmp, game_a, game_b)
    try:
        rows = page._folder_rows
        assert [str(path) for path, *_rest in rows] == [str(game_a), str(game_b)]
        current_rows = [row for row in rows if row[1]]
        assert len(current_rows) == 1 and current_rows[0][0] == game_a
        assert rows[0][2].text() != "" and not rows[0][2].isEnabled()  # 「当前」 is not a switch
        assert rows[0][3].isEnabled() is True
        assert rows[1][2].isEnabled() is True

        # One folder left: 移除 is disabled (the list always keeps one).
        cfg, cfg_path = config_mod.load()
        assert cfg.remove_game_folder(game_b) is True
        config_mod.save(cfg, cfg_path)
        page._reload_folders()
        assert len(page._folder_rows) == 1
        assert page._folder_rows[0][3].isEnabled() is False
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_remove_asks_and_a_dismissal_keeps_the_folder(app, monkeypatch, ws_tmp):
    """The confirmation is real: a dismissal (Esc / backdrop / cancel) removes nothing."""
    game_a = ws_tmp / "a"
    game_b = ws_tmp / "b"
    page = _settings_page(app, monkeypatch, ws_tmp, game_a, game_b)
    try:
        cfg, _ = config_mod.load()
        assert len(cfg.game_folders) == 2
        # conftest already makes every in-window dialog return DISMISSED.
        assert page._remove_folder(game_b) is False
        cfg, _ = config_mod.load()
        assert len(cfg.game_folders) == 2
        assert cfg.game_dir == game_a

        from gui import inwindow_dialog

        monkeypatch.setattr(
            inwindow_dialog, "confirm", lambda *a, **k: inwindow_dialog.Outcome.ACCEPTED
        )
        assert page._remove_folder(game_b) is True
        cfg, _ = config_mod.load()
        assert [Path(folder.path) for folder in cfg.game_folders] == [game_a]
        assert cfg.game_dir == game_a
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_switch_is_refused_while_a_download_runs(app, monkeypatch, ws_tmp):
    """A download writes into the current folder; switching then is refused and says why."""
    game_a = ws_tmp / "a"
    game_b = ws_tmp / "b"
    page = _settings_page(app, monkeypatch, ws_tmp, game_a, game_b)
    try:
        from launcher.net import downloader

        with downloader.download_activity():
            assert page._folder_switch_block_reason() != ""
            assert page._set_current_folder(game_b) is False
            page._update_folder_actions()
            assert page.folder_busy_label.text()
            assert not page.folder_busy_label.isHidden()
            row_b = page._folder_rows[1]
            assert row_b[2].isEnabled() is False  # 「设为当前」 disabled while busy
        # ... and the folder did not change
        cfg, _ = config_mod.load()
        assert cfg.game_dir == game_a
        assert page._folder_switch_block_reason() == ""
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_switch_is_refused_while_the_game_launches(app, monkeypatch, ws_tmp):
    """The launch button is disabled for the whole pre-launch + run worker: that is the guard."""
    _use_data_dir(monkeypatch, ws_tmp)
    game_a = ws_tmp / "a"
    game_b = ws_tmp / "b"
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game_a
    config_mod.save(cfg, cfg_path)
    cfg, cfg_path = config_mod.load()
    cfg.add_game_folder(game_b)
    config_mod.save(cfg, cfg_path)

    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        page = window.pages["settings"]
        launch_button = window.pages["launch"].launch_button
        launch_button.setEnabled(False)
        assert page._folder_switch_block_reason() != ""
        assert page._set_current_folder(game_b) is False
        launch_button.setEnabled(True)
        assert page._folder_switch_block_reason() == ""
        assert page._set_current_folder(game_b) is True
    finally:
        window.close()
        app.processEvents()


# ---------------------------------------------------------------- 4. switching refreshes the window


def test_switching_the_folder_refreshes_instances_versions_and_the_status_label(
    app, monkeypatch, ws_tmp
):
    """The pages that answer "what is installed here" read the new folder after a switch."""
    _use_data_dir(monkeypatch, ws_tmp)
    game_a = ws_tmp / "a"
    game_b = ws_tmp / "b"
    _write_version(game_a, "1.20.1")
    _write_version(game_b, "1.21.11")

    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game_a
    config_mod.save(cfg, cfg_path)

    from PySide6.QtCore import Qt

    from gui.main_window import NAV_KEYS, MainWindow

    def instance_ids(page) -> set[str]:
        return {
            page.list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(page.list.count())
        }

    window = MainWindow()
    try:
        window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        app.processEvents()
        instances = window.pages["instances"]
        assert instance_ids(instances) == {"1.20.1"}
        versions = window.pages["versions"]
        versions.refresh_installed()
        assert versions._installed == {"1.20.1"}

        # The resources page is built here but never shown: building it must be enough for the
        # switch to reach it (its refresh is what re-reads the new folder's content).
        resources = window.pages["mods"]
        resources_refreshed: list[bool] = []
        monkeypatch.setattr(resources, "refresh", lambda: resources_refreshed.append(True))

        settings = window.pages["settings"]
        assert settings._add_folder(game_b) is True
        assert resources_refreshed == []  # adding a folder is not a switch
        assert settings._set_current_folder(game_b) is True
        app.processEvents()

        assert resources_refreshed == [True]
        assert instance_ids(instances) == {"1.21.11"}
        assert versions._installed == {"1.21.11"}
        assert config_mod.load()[0].game_dir == game_b
        assert "b" in window.folder_label.text()
        assert window.folder_label.toolTip() == str(game_b)
    finally:
        window.close()
        app.processEvents()
