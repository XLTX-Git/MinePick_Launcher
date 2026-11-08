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

"""修改 and 导出: the instance page's two former placeholders, from the profile pointer to the zip.

The switch is deliberately one field - ``minepick.json``'s ``base_version`` - so the tests here are
about the four promises around it: only installed profiles are offered, every launch resolves the new
profile, cancelling changes nothing, and a failure leaves the instance exactly where it was. The
export side is about honest work: a zip that opens, byte progress that ends at its total, and a row on
the download page that finishes.
"""

from __future__ import annotations

import inspect
import json
import time
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool

from gui import i18n, inwindow_dialog
from gui.pages import instance_detail
from gui.pages.instance_detail import TAB_MODIFY
from gui.tasks import DONE
from launcher import config as config_mod
from launcher import paths
from launcher.instances import (
    INSTANCE_META_FILENAME,
    Instance,
    InstancesError,
    export_instance,
    instance_dir,
    profile_identity,
    resolve_instance,
    switch_instance_version,
)
from launcher.meta.version import load_version_json

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
    """No network and no Java probe outliving the window (the GUI suite's own stubs)."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    # The 修改 chooser asks for the official catalog and for downloadable loaders in the
    # background; without these stubs every dialog in this file would wait on a real request.
    monkeypatch.setattr(instance_detail, "fetch_catalog", _fail)
    monkeypatch.setattr(instance_detail, "loader_install_options", lambda *a, **kw: ([], []))
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


# ---------- fixtures: a game folder with four profiles and one pack ----------


def _write_version(game: Path, version_id: str, *, inherits: str | None = None) -> Path:
    """One installed profile. Its mainClass names the version, so a resolved chain is checkable."""
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    raw = {
        "id": version_id,
        "mainClass": "net.minecraft.client." + version_id + ".Main",
        "assetIndex": {"id": "5", "url": "https://example.invalid/5.json"},
        "assets": "5",
        "libraries": [],
        "downloads": {
            "client": {
                "url": "https://example.invalid/c.jar",
                "sha1": "0" * 40,
                "size": 1,
                "path": version_id + ".jar",
            }
        },
    }
    if inherits:
        raw["inheritsFrom"] = inherits
    (folder / (version_id + ".json")).write_text(json.dumps(raw), encoding="utf-8")
    (folder / (version_id + ".jar")).write_bytes(b"jar")
    return folder


def _make_instance(
    game: Path, version_id: str, *, inherits: str | None = None, note: str = ""
) -> Path:
    folder = _write_version(game, version_id, inherits=inherits)
    meta = Instance(id=version_id, display_name=version_id, note=note, created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    (folder / "mods" / "sodium.jar").write_bytes(b"PK\x03\x04sodium")
    saves = folder / "saves" / "world"
    saves.mkdir(parents=True, exist_ok=True)
    (saves / "level.dat").write_bytes(b"level")
    return folder


def _game(ws_tmp: Path) -> Path:
    """The fixture game folder: 1.21.11 + Fabric, a 1.20.x pair, and the pack under test."""
    game = ws_tmp / "mc"
    _make_instance(game, "1.21.11")
    _make_instance(game, "fabric-loader-0.19.3-1.21.11", inherits="1.21.11")
    _make_instance(game, "1.20.1")
    _make_instance(game, "1.20.2")
    _make_instance(game, "红石包", inherits="fabric-loader-0.19.3-1.21.11", note="红石")
    return game


def _cfg(game: Path, **overrides):
    values = {"game_dir": game, "memory_gb": 6.0, "jvm_args": ""}
    values.update(overrides)
    return config_mod.LauncherConfig(**values)


def _settle(app, window) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _pump(app, predicate=None, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate is None:
            return True
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate and predicate())


@pytest.fixture()
def window(app, monkeypatch, ws_tmp):
    """A real window on the instances page, with the fixture game folder selected."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = _game(ws_tmp)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.selected_instance = ""
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    from gui.main_window import MainWindow

    built = MainWindow()
    built.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    built.resize(1180, 760)
    built.show()
    _settle(app, built)
    yield built, game
    i18n.set_language("zh_cn")
    built.close()
    built.deleteLater()


def _open_detail(app, window, instance_id: str):
    from gui.main_window import NAV_KEYS

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, window)
    page = window.pages["instances"]
    assert page.open_instance(instance_id)
    _settle(app, window)
    return page.detail


def _answer(dialog) -> object:
    """Record what a dialog showed, then dismiss it (the suite's default answer)."""
    return inwindow_dialog.Outcome.DISMISSED


# ---------------------------------------------------------------------------
# 1. 修改 lists the installed versions - and only the installed ones
# ---------------------------------------------------------------------------


def test_the_modify_page_lists_the_installed_versions(app, monkeypatch, ws_tmp, window):
    built, _game = window
    detail = _open_detail(app, built, "红石包")
    detail.tabs[TAB_MODIFY].click()
    app.processEvents()

    # The top of the page is the state the launch path will really use: 1.21.11 through Fabric,
    # reached by following the pack's inheritsFrom chain.
    assert detail.modify_values["game_version"].text() == "1.21.11"
    assert detail.modify_values["loader"].text() == "Fabric 0.19.3"
    assert detail.modify_values["point_to"].text() == "红石包"

    seen: dict[str, list[str]] = {}

    def record(dialog) -> object:
        seen["ids"] = [row.identity.version_id for row in dialog.rows]
        seen["labels"] = [row.label_text() for row in dialog.rows]
        return inwindow_dialog.Outcome.DISMISSED

    monkeypatch.setattr(instance_detail._VersionChoiceDialog, "ask", record)

    detail.change_game_version()
    assert seen["ids"] == ["1.20.1", "1.20.2"]  # only the *other* Minecraft versions, sorted
    assert seen["labels"] == ["1.20.1 · 原版", "1.20.2 · 原版"]

    detail.change_loader()
    # Same Minecraft version only: the vanilla profile (which drops the loader) and the other loader.
    assert seen["ids"] == ["1.21.11", "fabric-loader-0.19.3-1.21.11"]
    assert seen["labels"] == ["1.21.11 · 原版", "1.21.11 · Fabric 0.19.3"]


# ---------------------------------------------------------------------------
# 2. A switch re-points the profile every launch resolves
# ---------------------------------------------------------------------------


def test_a_switch_changes_the_profile_launch_resolves(app, monkeypatch, ws_tmp, window):
    built, game = window
    detail = _open_detail(app, built, "红石包")
    target = profile_identity(game, "1.20.2")

    monkeypatch.setattr(
        instance_detail._SwitchConfirmDialog,
        "ask",
        lambda dialog: inwindow_dialog.Outcome.ACCEPTED,
    )
    detail._confirm_switch(target)
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.20.2",
    ), "the switch never reached the page"

    stored = json.loads(
        (instance_dir(game, "红石包") / INSTANCE_META_FILENAME).read_text(encoding="utf-8")
    )
    assert stored["base_version"] == "1.20.2"  # a pointer, not a copy
    assert (instance_dir(game, "红石包") / "mods" / "sodium.jar").exists()
    assert (instance_dir(game, "红石包") / "saves" / "world" / "level.dat").exists()

    resolved = resolve_instance("红石包", _cfg(game), game)
    assert resolved.version_id == "1.20.2"
    assert detail.effective_version_id() == "1.20.2"
    # The chain the launcher builds from really is the target's profile, not the pack's own file.
    version = load_version_json(resolved.version_id, versions_dir=game / "versions")
    assert version.main_class == "net.minecraft.client.1.20.2.Main"

    # ... and the one call site that hands the id to prepare_launch reads that field.
    source = inspect.getsource(instance_detail.InstanceDetail._launch_now)
    assert "version_id = resolved.version_id" in source

    calls: list[str] = []
    monkeypatch.setattr(instance_detail, "resolve_launch_account", lambda *a, **kw: object())
    monkeypatch.setattr(
        instance_detail,
        "prepare_launch",
        lambda version_id, **kwargs: calls.append(version_id) or (_ for _ in ()).throw(
            RuntimeError("stop here")
        ),
    )
    monkeypatch.setattr(instance_detail, "show_fatal", lambda *a, **kw: None)
    detail._launch_now("红石包", _cfg(game), resolved)
    assert _pump(app, lambda: calls), "prepare_launch was never called"
    assert calls == ["1.20.2"]


# ---------------------------------------------------------------------------
# 3. Cancelling changes nothing at all
# ---------------------------------------------------------------------------


def _tree(folder: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            stat = path.stat()
            out[path.relative_to(folder).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return out


def test_cancelling_the_confirmation_changes_nothing(app, monkeypatch, ws_tmp, window):
    built, game = window
    detail = _open_detail(app, built, "红石包")
    folder = instance_dir(game, "红石包")
    before = _tree(folder)
    sidecar = (folder / INSTANCE_META_FILENAME).read_text(encoding="utf-8")

    monkeypatch.setattr(
        instance_detail._SwitchConfirmDialog,
        "ask",
        lambda dialog: inwindow_dialog.Outcome.REJECTED,
    )
    detail._confirm_switch(profile_identity(game, "1.20.1"))
    _settle(app, built)

    assert detail.instance.base_version == ""
    assert detail.effective_version_id() == "红石包"
    assert (folder / INSTANCE_META_FILENAME).read_text(encoding="utf-8") == sidecar
    assert _tree(folder) == before
    backups = paths.launcher_dir() / "backups"
    assert not backups.exists() or not list(backups.glob("*.zip"))

    # Cancelling the *list* (Esc, 取消) is the other half of the same promise.
    monkeypatch.setattr(
        instance_detail._VersionChoiceDialog, "ask", lambda dialog: inwindow_dialog.Outcome.DISMISSED
    )
    detail.change_game_version()
    detail.change_loader()
    _settle(app, built)
    assert detail.instance.base_version == ""
    assert _tree(folder) == before


# ---------------------------------------------------------------------------
# 4. The backup is written before the profile changes
# ---------------------------------------------------------------------------


def test_the_backup_is_written_before_the_profile_changes(ws_tmp):
    game = _game(ws_tmp)
    backups = ws_tmp / "backups"
    outcome = switch_instance_version("红石包", "1.20.2", game, backup_dir=backups)

    assert outcome.backup_path is not None and outcome.backup_path.exists()
    assert outcome.backup_path.parent == backups
    with zipfile.ZipFile(outcome.backup_path) as archive:
        archived = json.loads(archive.read(INSTANCE_META_FILENAME))
        assert "mods/sodium.jar" in archive.namelist()
    # The archive holds the sidecar *before* the switch, so it was written first - not after.
    assert archived["base_version"] == ""
    assert archived["note"] == "红石"
    current = json.loads(
        (instance_dir(game, "红石包") / INSTANCE_META_FILENAME).read_text(encoding="utf-8")
    )
    assert current["base_version"] == "1.20.2"


def test_the_confirmation_checks_the_backup_by_default(app, monkeypatch, ws_tmp, window):
    built, game = window
    detail = _open_detail(app, built, "红石包")
    seen: dict[str, bool] = {}

    def answer(dialog) -> object:
        seen["checked"] = dialog.wants_backup
        return inwindow_dialog.Outcome.ACCEPTED

    monkeypatch.setattr(instance_detail._SwitchConfirmDialog, "ask", answer)
    detail._confirm_switch(profile_identity(game, "1.20.1"))
    assert seen["checked"] is True  # 默认勾上
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.20.1",
    )
    backed = list((paths.launcher_dir() / "backups").glob("*.zip"))
    assert len(backed) == 1, "a checked backup must really produce a zip"
    with zipfile.ZipFile(backed[0]) as archive:
        assert "mods/sodium.jar" in archive.namelist()


# ---------------------------------------------------------------------------
# 5. A failed switch rolls back
# ---------------------------------------------------------------------------


def test_a_failed_switch_restores_the_previous_pointer(ws_tmp, monkeypatch):
    game = _game(ws_tmp)
    folder = instance_dir(game, "红石包")
    before = (folder / INSTANCE_META_FILENAME).read_text(encoding="utf-8")

    def broken_write(target, inst) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("launcher.instances._write_sidecar", broken_write)
    with pytest.raises(InstancesError) as caught:
        switch_instance_version("红石包", "1.20.2", game)

    message = str(caught.value)
    assert "恢复" in message  # 切换失败，已恢复原来的指向
    assert (folder / INSTANCE_META_FILENAME).read_text(encoding="utf-8") == before
    assert (folder / "mods" / "sodium.jar").exists()


def test_a_failed_switch_restores_from_the_backup_when_the_pointer_cannot_be_restored(
    ws_tmp, monkeypatch
):
    game = _game(ws_tmp)
    folder = instance_dir(game, "红石包")
    backups = ws_tmp / "backups"
    monkeypatch.setattr(
        "launcher.instances._verify_switch",
        lambda *a, **kw: (_ for _ in ()).throw(InstancesError("verify failed")),
    )
    monkeypatch.setattr(
        "launcher.instances._restore_sidecar_bytes",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("cannot write")),
    )
    with pytest.raises(InstancesError) as caught:
        switch_instance_version("红石包", "1.20.2", game, backup_dir=backups)

    assert "恢复" in str(caught.value)
    # The archive put the whole folder back: same files, same pre-switch pointer.
    restored = json.loads(
        (folder / INSTANCE_META_FILENAME).read_text(encoding="utf-8")
    )
    assert restored["base_version"] == ""
    assert restored["note"] == "红石"
    assert (folder / "mods" / "sodium.jar").exists()
    assert (folder / "saves" / "world" / "level.dat").exists()


# ---------------------------------------------------------------------------
# 6. The export produces a zip that opens
# ---------------------------------------------------------------------------


def test_the_export_produces_a_readable_zip(ws_tmp):
    game = _game(ws_tmp)
    dest = ws_tmp / "out" / "红石包.zip"
    reported: list[tuple[int, int]] = []
    path = export_instance(
        "红石包", dest, game, on_progress=lambda done, total: reported.append((done, total))
    )

    assert path == dest and dest.exists()
    with zipfile.ZipFile(dest) as archive:
        assert archive.testzip() is None  # a real archive, every CRC checked
        names = archive.namelist()
        assert INSTANCE_META_FILENAME in names
        assert "mods/sodium.jar" in names
        assert "saves/world/level.dat" in names
        archived = json.loads(archive.read(INSTANCE_META_FILENAME))
    assert archived["note"] == "红石"

    assert reported, "the export reported no progress at all"
    done, total = reported[-1]
    assert total > 0 and done == total, (done, total)
    assert [value for value, _total in reported] == sorted(value for value, _total in reported)
    assert not list(dest.parent.glob("*.tmp")), "the .tmp file outlived a successful export"


# ---------------------------------------------------------------------------
# 7. The export is a task, and it finishes on the download page
# ---------------------------------------------------------------------------


def test_the_export_shows_up_and_finishes_on_the_download_page(app, monkeypatch, ws_tmp, window):
    built, _game = window
    detail = _open_detail(app, built, "红石包")
    dest = ws_tmp / "page-export.zip"

    detail.export_to(dest)
    tasks = built.tasks.tasks()
    assert len(tasks) == 1, "the export never became a task"
    assert tasks[0].kind == "export"
    assert tasks[0].title() == i18n.tr("tasks.title.export", "红石包")
    assert tasks[0].state in ("running", DONE)

    assert _pump(app, lambda: built.tasks.tasks() and built.tasks.tasks()[0].state == DONE), (
        built.tasks.tasks()[0].state,
        built.tasks.tasks()[0].error,
        built.tasks.tasks()[0].done_bytes,
        built.tasks.tasks()[0].total_bytes,
    )
    task = built.tasks.tasks()[0]
    assert task.percent() == 100
    assert task.done_bytes == task.total_bytes > 0
    assert dest.exists() and dest.stat().st_size > 0

    page = built.open_download_page()
    app.processEvents()
    if page is None:
        page = built.downloads_page()
    assert page is not None
    rows = page.rows()
    assert [row.task.kind for row in rows] == ["export"]
    assert rows[0].task.state == DONE
    assert rows[0].icon_name() == "export"
