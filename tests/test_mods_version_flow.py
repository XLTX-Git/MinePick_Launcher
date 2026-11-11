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

"""The resources page's install flow: choose a file version, then a save location.

PCL's flow is reproduced end to end here without the network or a native window:

* the version list is a real in-window card and shows every field the user needs;
* the default save folder follows PCL's four-step priority (last folder per kind, selected
  compatible instance, most recently used compatible instance, the game folder);
* the system save dialog is replaced at its one call site (``ask_save_file``), so a test can answer
  "save here" or "cancel" without opening a window;
* a cancelled dialog downloads nothing, and an accepted one writes exactly the chosen path.

The memory of the last folder lives in ``launcher.config.LauncherConfig.resource_dirs`` and is keyed
by the content folder name (``mods`` / ``resourcepacks`` / ``shaderpacks``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from gui import i18n, inwindow_dialog
from gui.pages import mods_page
from launcher import config as config_mod
from launcher.mods.models import ModFile, ModSearchHit, ModVersion

FABRIC = "fabric-loader-0.16.9-1.21.11"
FORGE = "1.20.1-forge-47.4.22"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture()
def game(ws_tmp, monkeypatch):
    """A game folder wired into the real config (so ``remember_content_dir`` can be measured)."""
    root = ws_tmp / "game"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(root))
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = root
    cfg.selected_instance = ""
    cfg.default_isolation = True
    config_mod.save(cfg, cfg_path)
    return root


def _instance(
    game_dir: Path,
    instance_id: str,
    *,
    display: str = "",
    extra: dict | None = None,
) -> Path:
    folder = game_dir / "versions" / instance_id
    folder.mkdir(parents=True, exist_ok=True)
    raw: dict[str, object] = {"id": instance_id}
    raw.update(extra or {})
    (folder / (instance_id + ".json")).write_text(json.dumps(raw), encoding="utf-8")
    if display:
        (folder / "minepick.json").write_text(
            json.dumps({"id": instance_id, "display_name": display}),
            encoding="utf-8",
        )
    return folder


def _version(
    *,
    number: str = "1.0.0",
    loaders: list[str] | None = None,
    game_versions: list[str] | None = None,
    filename: str = "sodium-1.0.jar",
    size: int = 1024,
    published: str = "2024-06-13T12:00:00+00:00",
) -> ModVersion:
    return ModVersion(
        version_id="v-" + number,
        version_number=number,
        loaders=list(loaders or ["fabric"]),
        game_versions=list(game_versions or ["1.21.11"]),
        files=[ModFile(filename=filename, url="https://example.invalid/x.jar", size=size)],
        date_published=published,
    )


def _hit() -> ModSearchHit:
    return ModSearchHit(
        slug="sodium",
        title="Sodium",
        description="Modern rendering engine",
        loaders=["fabric"],
        source="modrinth",
    )


def _sync_runner(fn, *args, on_result=None, on_error=None, on_finished=None, **_kwargs):
    try:
        result = fn(*args)
    except Exception as exc:  # noqa: BLE001 - mirrors Worker.run's error channel
        if on_error is not None:
            on_error(str(exc))
    else:
        if on_result is not None:
            on_result(result)
    if on_finished is not None:
        on_finished()


def _statuses(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        mods_page, "set_app_status", lambda _widget, text, level=None: seen.append(text)
    )
    return seen


def _close(widget, app) -> None:
    try:
        widget.deleteLater()
    except RuntimeError:
        pass  # its parent already took it down
    app.processEvents()


# ---------------------------------------------------------------- 1. the version card


def test_the_version_card_shows_every_field_and_selects_one(app) -> None:
    first = _version(number="1.0.0", size=1572864)
    second = _version(
        number="0.9.0",
        loaders=["fabric", "quilt"],
        game_versions=["1.20.1", "1.20.2"],
        filename="sodium-0.9.jar",
    )
    host = QWidget()
    dialog = mods_page._VersionPickerDialog(host, [first, second], project_title="Sodium")
    try:
        assert dialog.title_label.text() == i18n.tr("mods.versions.title", "Sodium")
        assert len(dialog.rows) == 2
        assert dialog.selected_version() is first, "newest first is pre-selected"
        assert dialog.next_button.text() == i18n.tr("mods.versions.next")

        row = dialog.rows[0]
        name = row.accessibleName()
        assert "1.0.0" in name
        assert i18n.tr("mods.versions.mc") in name and "1.21.11" in name
        assert "Fabric" in name
        assert "2024-06-13" in name
        assert "MB" in name

        assert "Quilt" in dialog.rows[1].accessibleName()
        dialog.rows[1].click()
        assert dialog.selected_version() is second
        assert dialog.rows[0].isChecked() is False
    finally:
        _close(dialog, app)


def test_a_curseforge_file_prefills_the_real_file_name(app) -> None:
    from launcher.mods.curseforge import CfFile

    cf_file = CfFile(
        id=7,
        name="Sodium 1.0",
        url="https://example.invalid/sodium.jar",
        size=2048,
        filename="sodium-fabric-1.0.jar",
        game_versions=["1.21.11"],
        loaders=["fabric"],
        published="2024-06-13T00:00:00Z",
    )
    assert mods_page.version_filename_of(cf_file) == "sodium-fabric-1.0.jar"
    assert mods_page.version_loaders_of(cf_file) == ["fabric"]
    assert mods_page.version_game_versions_of(cf_file) == ["1.21.11"]
    host = QWidget()
    dialog = mods_page._VersionPickerDialog(host, [cf_file], project_title="Sodium")
    try:
        assert dialog.selected_version() is cf_file
        assert "sodium-fabric-1.0.jar" in dialog.rows[0].accessibleName()
    finally:
        _close(dialog, app)


# ---------------------------------------------------------------- 2. the default folder


def _compatible_instance(game_dir: Path, loader: str = "fabric") -> Path:
    if loader == "fabric":
        return _instance(
            game_dir,
            FABRIC,
            extra={"inheritsFrom": "1.21.11", "type": "release"},
        )
    return _instance(game_dir, FORGE, extra={"inheritsFrom": "1.21.11"})


def _touch_folder(path: Path, modified: float, files: int = 1) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (path / f"file-{index}.jar").write_bytes(b"x")
    os.utime(path, (modified, modified))


def test_priority_1_the_last_folder_wins(game) -> None:
    _compatible_instance(game)
    remembered = game / "custom-mods"
    cfg = config_mod.LauncherConfig(
        game_dir=game,
        selected_instance=FABRIC,
        resource_dirs={"mods": str(remembered)},
    )
    assert (
        mods_page.default_content_dir(
            "mod", ["fabric"], ["1.21.11"], cfg=cfg, game_dir=game
        )
        == remembered
    )


def test_priority_2_the_selected_compatible_instance(game) -> None:
    folder = _compatible_instance(game)
    cfg = config_mod.LauncherConfig(game_dir=game, selected_instance=FABRIC)
    assert mods_page.default_content_dir(
        "mod", ["fabric"], ["1.21.11"], cfg=cfg, game_dir=game
    ) == folder / "mods"


def test_priority_3_the_most_recently_used_compatible_instance(game) -> None:
    older = _compatible_instance(game)
    _touch_folder(older / "mods", 1000.0, files=9)
    newer = _instance(
        game,
        "fabric-loader-0.15.11-1.21.11",
        extra={"inheritsFrom": "1.21.11"},
    )
    _touch_folder(newer / "mods", 2000.0, files=1)
    cfg = config_mod.LauncherConfig(game_dir=game)
    assert mods_page.default_content_dir(
        "mod", ["fabric"], ["1.21.11"], cfg=cfg, game_dir=game
    ) == newer / "mods"


def test_priority_3_breaks_a_mtime_tie_by_file_count(game) -> None:
    few = _compatible_instance(game)
    _touch_folder(few / "mods", 3000.0, files=1)
    many = _instance(
        game,
        "fabric-loader-0.14.0-1.21.11",
        extra={"inheritsFrom": "1.21.11"},
    )
    _touch_folder(many / "mods", 3000.0, files=3)
    cfg = config_mod.LauncherConfig(game_dir=game)
    assert mods_page.default_content_dir(
        "mod", ["fabric"], ["1.21.11"], cfg=cfg, game_dir=game
    ) == many / "mods"


def test_priority_4_no_compatible_instance_means_the_game_folder(game) -> None:
    _compatible_instance(game, "forge")
    cfg = config_mod.LauncherConfig(game_dir=game, selected_instance=FORGE)
    # The selected instance is a Forge instance and the file is Fabric-only: it may not win.
    assert (
        mods_page.default_content_dir(
            "mod", ["fabric"], ["1.21.11"], cfg=cfg, game_dir=game
        )
        == game
    )


def test_a_game_version_mismatch_also_refuses_an_instance(game) -> None:
    _compatible_instance(game)
    cfg = config_mod.LauncherConfig(game_dir=game, selected_instance=FABRIC)
    assert (
        mods_page.default_content_dir(
            "mod", ["fabric"], ["1.20.1"], cfg=cfg, game_dir=game
        )
        == game
    )
    choices = mods_page.instance_choices(
        game, ["fabric"], cfg=cfg, kind="mod", game_versions=["1.20.1"]
    )
    assert choices[0].usable is False
    assert choices[0].note == i18n.tr(
        "mods.instance.mismatch.game", "1.20.1", "1.21.11"
    )


def test_the_memory_is_written_to_the_config(game) -> None:
    chosen = game / "somewhere" / "mods"
    mods_page.remember_content_dir("mod", chosen)
    cfg, _path = config_mod.load()
    assert cfg.resource_dirs["mods"] == str(chosen)
    assert (
        mods_page.default_content_dir(
            "mod", ["fabric"], ["1.21.11"], cfg=cfg, game_dir=game
        )
        == chosen
    )


def test_each_content_kind_uses_its_own_folder_and_memory_key(game) -> None:
    instance = _compatible_instance(game)
    cfg = config_mod.LauncherConfig(game_dir=game, selected_instance=FABRIC)
    assert (
        mods_page.default_content_dir(
            "resourcepack", [], [], cfg=cfg, game_dir=game
        )
        == instance / "resourcepacks"
    )
    assert (
        mods_page.default_content_dir("shaderpack", [], [], cfg=cfg, game_dir=game)
        == instance / "shaderpacks"
    )
    shaders = game / "shared-shaders"
    mods_page.remember_content_dir("shaderpack", shaders)
    cfg, _path = config_mod.load()
    assert cfg.resource_dirs == {"shaderpacks": str(shaders)}


# ---------------------------------------------------------------- 3. the click: save-as


def _prepare_tab(game: Path, monkeypatch, versions: list[ModVersion]):
    monkeypatch.setattr(mods_page, "run_in_background", _sync_runner)
    monkeypatch.setattr(mods_page, "fetch_versions", lambda slug: versions)
    page = mods_page.ResourcesPage()
    tab = page.mods_tab
    tab._set_entries([_hit()])
    return page, tab


def test_an_accepted_save_dialog_downloads_exactly_that_path(game, app, monkeypatch) -> None:
    _compatible_instance(game)
    cfg, cfg_path = config_mod.load()
    cfg.selected_instance = FABRIC
    config_mod.save(cfg, cfg_path)

    page, tab = _prepare_tab(game, monkeypatch, [_version()])
    chosen = game / "user" / "pick" / "sodium.jar"
    calls: list[tuple] = []

    def choose_version(dialog):
        dialog.rows[0].click()
        return inwindow_dialog.Outcome.ACCEPTED

    def save(parent, title, suggested, filter_text):
        calls.append((title, suggested, filter_text))
        return chosen

    downloads: list[Path] = []

    def fake_install(slug, **kwargs):
        downloads.append(kwargs["dest"])
        kwargs["dest"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["dest"].write_bytes(b"jar")
        return SimpleNamespace(title="Sodium", slug=slug, depends=[], optional_depends=[])

    monkeypatch.setattr(mods_page._VersionPickerDialog, "ask", choose_version)
    monkeypatch.setattr(mods_page, "ask_save_file", save)
    monkeypatch.setattr(mods_page, "install_mod", fake_install)
    statuses = _statuses(monkeypatch)

    before = sorted(path.name for path in (game / "versions").iterdir())
    try:
        tab._on_install_clicked()
    finally:
        page.deleteLater()
        app.processEvents()

    assert downloads == [chosen]
    assert chosen.read_bytes() == b"jar"
    # No instance and no content folder structure was created by the install.
    assert sorted(path.name for path in (game / "versions").iterdir()) == before
    assert len(calls) == 1
    _title, suggested, filter_text = calls[0]
    assert suggested == game / "versions" / FABRIC / "mods" / "sodium-1.0.jar"
    assert filter_text == "*.jar"
    assert statuses[-1] == i18n.tr("mods.msg.saved_to", str(chosen))

    cfg, _path = config_mod.load()
    assert cfg.resource_dirs["mods"] == str(chosen.parent)


def test_a_cancelled_save_dialog_downloads_nothing(game, app, monkeypatch) -> None:
    page, tab = _prepare_tab(game, monkeypatch, [_version()])
    called: list[str] = []

    def forbidden(*_args, **_kwargs):
        called.append("download")
        raise AssertionError("a cancelled save dialog must not download")

    def choose_version(dialog):
        return inwindow_dialog.Outcome.ACCEPTED

    monkeypatch.setattr(mods_page._VersionPickerDialog, "ask", choose_version)
    monkeypatch.setattr(mods_page, "ask_save_file", lambda *a, **k: None)
    monkeypatch.setattr(mods_page, "install_mod", forbidden)
    statuses = _statuses(monkeypatch)
    try:
        tab._on_install_clicked()
    finally:
        page.deleteLater()
        app.processEvents()

    assert called == []
    assert statuses[-1] == i18n.tr("mods.msg.save_cancelled")
    assert not any(path.suffix == ".jar" for path in game.rglob("*.jar"))


def test_the_version_dialog_can_be_cancelled_before_the_save_dialog(
    game, app, monkeypatch
) -> None:
    page, tab = _prepare_tab(game, monkeypatch, [_version()])
    saved: list[bool] = []
    monkeypatch.setattr(
        mods_page._VersionPickerDialog,
        "ask",
        lambda dialog: inwindow_dialog.Outcome.REJECTED,
    )
    monkeypatch.setattr(mods_page, "ask_save_file", lambda *a, **k: saved.append(True))
    statuses = _statuses(monkeypatch)
    try:
        tab._on_install_clicked()
    finally:
        page.deleteLater()
        app.processEvents()
    assert saved == []
    assert statuses[-1] == i18n.tr("mods.status.default"), "the loading line must not stay behind"
    assert tab.install_button.isEnabled(), "the install button must come back"


def test_an_empty_version_list_says_so_and_opens_no_dialog(game, app, monkeypatch) -> None:
    page, tab = _prepare_tab(game, monkeypatch, [])
    opened: list[bool] = []
    monkeypatch.setattr(
        mods_page._VersionPickerDialog, "ask", lambda dialog: opened.append(True)
    )
    statuses = _statuses(monkeypatch)
    try:
        tab._on_install_clicked()
    finally:
        page.deleteLater()
        app.processEvents()
    assert opened == []
    assert statuses[-1] == i18n.tr("mods.versions.none")
