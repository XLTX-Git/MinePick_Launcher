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

"""修改 can download a core: the version list, the install, and the pointer that follows it.

The old page could only point a working instance at a profile that already existed. These tests are
about the second half: choosing a version that is *not* in ``versions/`` downloads through the
versions page's install path (stubbed here, because a test must not touch the network or Java), and
only after a usable profile exists does ``base_version`` move. The four promises the feature makes
are pinned per test: installed profiles still switch without downloading, cancelling changes nothing,
a failed install leaves the old pointer, and the backup checkbox is honoured before anything starts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication

from gui import i18n, inwindow_dialog
from gui.pages import instance_detail
from gui.pages.instance_detail import TAB_MODIFY
from gui.pages.versions_install_flow import (
    InstallFailed,
    InstallOutcome,
    install_selection,
)
from gui.tasks import DONE
from launcher import config as config_mod
from launcher import paths
from launcher.instances import (
    INSTANCE_META_FILENAME,
    Instance,
    InstancesError,
    core_profile_id,
    instance_dir,
    profile_identity,
)
from launcher.mods.loaders import ModsError
from launcher.mods.models import LoaderVersion
from launcher.net.downloader import DownloadCancelled

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No real manifest / loader request may outlive a dialog in this file."""

    def _fail(*args, **kwargs):
        raise RuntimeError("offline test")

    monkeypatch.setattr(instance_detail, "fetch_catalog", _fail)
    monkeypatch.setattr(instance_detail, "loader_install_options", lambda *a, **kw: ([], []))
    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])
    # A warm-cache catalog is session state; a test that wrote its own must not leak into the next.
    monkeypatch.setattr(instance_detail, "_CATALOG_CACHE", None)
    yield


# ---------- a game folder with three installed profiles and one pack ----------


def _write_profile(game: Path, version_id: str, *, inherits: str | None = None) -> Path:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    raw = {
        "id": version_id,
        "type": "release",
        "mainClass": "net.minecraft.client." + version_id + ".Main",
        "assetIndex": {"id": "5", "url": "https://example.invalid/5.json"},
        "assets": "5",
        "libraries": [],
    }
    if inherits:
        raw["inheritsFrom"] = inherits
    (folder / (version_id + ".json")).write_text(json.dumps(raw), encoding="utf-8")
    (folder / (version_id + ".jar")).write_bytes(b"jar")
    return folder


def _make_instance(game: Path, instance_id: str, *, inherits: str) -> Path:
    folder = _write_profile(game, instance_id, inherits=inherits)
    meta = Instance(id=instance_id, display_name=instance_id, created_at=1.0)
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
    game = ws_tmp / "mc"
    _write_profile(game, "1.21.11")
    _write_profile(game, "fabric-loader-0.19.3-1.21.11", inherits="1.21.11")
    _write_profile(game, "1.20.1")
    _make_instance(game, "红石包", inherits="fabric-loader-0.19.3-1.21.11")
    return game


def _write_manifest(data_dir: Path, versions: list[tuple[str, str, str]]) -> None:
    """The official manifest cache the chooser reads synchronously (never the network)."""
    cache = data_dir / "cache" / "version_manifest.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "manifest": {
            "latest": {"release": "1.21.11", "snapshot": "26w02a"},
            "versions": [
                {
                    "id": version_id,
                    "type": version_type,
                    "url": "https://example.invalid/version.json",
                    "time": f"{date}T12:00:00+00:00",
                    "releaseTime": f"{date}T12:00:00+00:00",
                }
                for version_id, version_type, date in versions
            ],
        },
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def window(app, monkeypatch, ws_tmp):
    """A real window on the instances page; the 红石包 instance is open on 修改."""
    data = ws_tmp / "data"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data))
    game = _game(ws_tmp)
    _write_manifest(
        data,
        [
            ("1.21.11", "release", "2025-12-09"),
            ("1.20.2", "release", "2024-02-29"),
            ("1.20.1", "release", "2023-06-12"),
            ("26w02a", "snapshot", "2026-01-13"),
        ],
    )
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
    detail = _open_detail(app, built, "红石包")
    detail.tabs[TAB_MODIFY].click()
    app.processEvents()
    yield built, game, detail
    i18n.set_language("zh_cn")
    built.close()
    built.deleteLater()


def _cfg(game: Path):
    return config_mod.LauncherConfig(game_dir=game, memory_gb=6.0, jvm_args="")


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


def _open_detail(app, window, instance_id: str):
    from gui.main_window import NAV_KEYS

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, window)
    page = window.pages["instances"]
    assert page.open_instance(instance_id)
    _settle(app, window)
    return page.detail


# ---------- answer helpers ----------


def _choose(monkeypatch, version_id: str, seen: dict | None = None) -> None:
    """Answer the version chooser with a row, remembering what the list showed."""

    def answer(dialog) -> object:
        if seen is not None:
            seen["statuses"] = {
                row.identity.version_id: row.status_text() for row in dialog.rows
            }
            seen["labels"] = [row.label_text() for row in dialog.rows]
        row = next((row for row in dialog.rows if row.identity.version_id == version_id), None)
        assert row is not None, [row.identity.version_id for row in dialog.rows]
        dialog.chosen = row.identity
        return inwindow_dialog.Outcome.ACCEPTED

    monkeypatch.setattr(instance_detail._VersionChoiceDialog, "ask", answer)


def _choose_loader(monkeypatch, seen: dict | None = None) -> None:
    """Answer the loader chooser with the first downloadable loader, waiting for its probe."""

    def answer(dialog) -> object:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            row = next((row for row in dialog.rows if row.choice.loader_version), None)
            if row is not None:
                break
            QApplication.processEvents()
            time.sleep(0.01)
        else:  # pragma: no cover - a failed probe is a red test, not a silent skip
            raise AssertionError([row.label_text() for row in dialog.rows])
        if seen is not None:
            seen["statuses"] = {row.label_text(): row.status_text() for row in dialog.rows}
        dialog.chosen = row.identity
        return inwindow_dialog.Outcome.ACCEPTED

    monkeypatch.setattr(instance_detail._VersionChoiceDialog, "ask", answer)


def _confirm(monkeypatch, *, backup: bool | None = None, seen: dict | None = None) -> None:
    def answer(dialog) -> object:
        if backup is not None:
            dialog.backup_check.setChecked(backup)
        if seen is not None:
            seen["checked"] = dialog.wants_backup
            seen["title"] = dialog.title_label.text() if dialog.title_label else ""
            seen["default"] = dialog.default_button.text() if dialog.default_button else ""
            seen["accept"] = dialog.buttons[-1].text()
        return inwindow_dialog.Outcome.ACCEPTED

    monkeypatch.setattr(instance_detail._SwitchConfirmDialog, "ask", answer)


def _reject_confirm(monkeypatch) -> None:
    monkeypatch.setattr(
        instance_detail._SwitchConfirmDialog,
        "ask",
        lambda dialog: inwindow_dialog.Outcome.REJECTED,
    )


def _stub_install(monkeypatch, game: Path, calls: list, *, error: Exception | None = None):
    """A stand-in for the versions page's install body: writes a profile or raises."""

    def install(version_id, **kwargs):
        calls.append((version_id, kwargs.get("loader_version")))
        if error is not None:
            raise error
        loader_version = kwargs.get("loader_version")
        if loader_version is not None:
            profile_id = f"fabric-loader-{loader_version.version}-{version_id}"
            _write_profile(game, profile_id, inherits=version_id)
            return InstallOutcome(profile_id=profile_id)
        _write_profile(game, version_id)
        return InstallOutcome(profile_id=version_id)

    monkeypatch.setattr(instance_detail, "install_selection", install)


def _sidecar(game: Path, instance_id: str) -> dict:
    return json.loads(
        (instance_dir(game, instance_id) / INSTANCE_META_FILENAME).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 1. The list offers installed and uninstalled versions, each with its state
# ---------------------------------------------------------------------------


def test_the_version_list_shows_installed_and_uninstalled_versions(app, window, monkeypatch):
    _built, _game_dir, detail = window
    seen: dict = {}
    _choose(monkeypatch, "1.20.2", seen)
    detail.change_game_version()

    assert seen["statuses"]["1.20.1"] == i18n.tr("instances.detail.modify.status.installed")
    assert seen["statuses"]["1.20.2"] == i18n.tr("instances.detail.modify.status.download")
    assert seen["statuses"]["26w02a"] == i18n.tr("instances.detail.modify.status.download")
    # The current game version is the other button's business; it is not in this list.
    assert "1.21.11" not in seen["statuses"]
    assert "红石包" not in seen["statuses"]


def test_the_loader_list_offers_installed_and_downloadable_loaders(app, window, monkeypatch):
    _built, _game, detail = window
    loader = LoaderVersion(
        loader="fabric", version="0.20.0", game_version="1.21.11", installer_url=""
    )
    monkeypatch.setattr(
        instance_detail,
        "loader_install_options",
        lambda *_a, **_k: ([("fabric", loader)], []),
    )
    seen: dict = {}
    _choose_loader(monkeypatch, seen)
    detail.change_loader()

    labels = seen["statuses"]
    assert any("0.20.0" in label for label in labels)
    assert seen["statuses"][next(label for label in labels if "0.20.0" in label)] == (
        i18n.tr("instances.detail.modify.status.download")
    )
    # The installed vanilla/loader profiles of the same game version are still there.
    assert any("原版" in label for label in labels)
    assert any("0.19.3" in label for label in labels)


# ---------------------------------------------------------------------------
# 2. Choosing an uninstalled version installs it, then moves the pointer
# ---------------------------------------------------------------------------


def test_choosing_an_uninstalled_version_installs_then_points_at_it(app, window, monkeypatch):
    _built, game, detail = window
    calls: list = []
    _stub_install(monkeypatch, game, calls)
    _choose(monkeypatch, "1.20.2")
    confirm: dict = {}
    _confirm(monkeypatch, backup=False, seen=confirm)

    detail.change_game_version()
    assert confirm["accept"] == i18n.tr("instances.detail.modify.confirm.accept_install")
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.20.2",
    ), "the switch never reached the page"

    assert calls and calls[0][0] == "1.20.2"
    assert calls[0][1] is None
    assert _sidecar(game, "红石包")["base_version"] == "1.20.2"
    # The instance folder itself is untouched: a switch is a pointer, not a copy.
    folder = instance_dir(game, "红石包")
    assert (folder / "mods" / "sodium.jar").exists()
    assert (folder / "saves" / "world" / "level.dat").exists()
    # The page and the overview now name the downloaded core and the folder it came from.
    assert detail.modify_values["game_version"].text() == "1.20.2"
    assert detail.modify_values["loader"].text() == i18n.tr(
        "instances.detail.modify.vanilla"
    )
    assert detail.modify_values["core_dir"].text() == str(instance_dir(game, "1.20.2"))
    assert detail.overview_values["version"].text() == "1.20.2"
    assert detail.overview_values["loader"].text() == i18n.tr(
        "instances.detail.modify.vanilla"
    )
    assert detail.overview_values["core_dir"].text() == str(instance_dir(game, "1.20.2"))
    # The window's task list saw the install start and end, instead of a row that spins forever.
    tasks = window[0].tasks.tasks()
    assert [task.state for task in tasks if task.kind == "version"] == [DONE]


def test_choosing_an_installed_version_moves_only_the_pointer(app, window, monkeypatch):
    _built, _game_dir, detail = window

    def must_not_install(*args, **kwargs):
        raise AssertionError("an installed profile must not be downloaded again")

    monkeypatch.setattr(instance_detail, "install_selection", must_not_install)
    _choose(monkeypatch, "1.20.1")
    _confirm(monkeypatch, backup=False)

    detail.change_game_version()
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.20.1",
    )
    assert detail.modify_values["core_dir"].text().endswith(str(Path("1.20.1")))


# ---------------------------------------------------------------------------
# 3. Cancelling changes nothing
# ---------------------------------------------------------------------------


def test_cancelling_the_list_installs_nothing(app, window, monkeypatch):
    _built, game, detail = window
    calls: list = []
    _stub_install(monkeypatch, game, calls)
    monkeypatch.setattr(
        instance_detail._VersionChoiceDialog,
        "ask",
        lambda dialog: inwindow_dialog.Outcome.DISMISSED,
    )
    before = _sidecar(game, "红石包")

    detail.change_game_version()
    _settle(app, window[0])

    assert calls == []
    assert _sidecar(game, "红石包") == before


def test_cancelling_the_confirmation_installs_nothing(app, window, monkeypatch):
    _built, game, detail = window
    calls: list = []
    _stub_install(monkeypatch, game, calls)
    _choose(monkeypatch, "1.20.2")
    _reject_confirm(monkeypatch)
    before = _sidecar(game, "红石包")

    detail.change_game_version()
    _settle(app, window[0])

    assert calls == []
    assert _sidecar(game, "红石包") == before
    assert detail.instance.base_version == ""


def test_cancelling_a_running_install_leaves_the_pointer(app, window, monkeypatch):
    _built, game, detail = window
    started = {"value": False}

    def slow_install(*args, **kwargs):
        started["value"] = True
        time.sleep(0.3)
        raise DownloadCancelled("cancelled")

    monkeypatch.setattr(instance_detail, "install_selection", slow_install)
    _choose(monkeypatch, "1.20.2")
    _confirm(monkeypatch, backup=False)

    detail.change_game_version()
    assert _pump(app, lambda: started["value"])
    assert detail.switch_progress_container.isVisibleTo(detail)
    assert detail.switch_cancel_button.isEnabled()
    assert detail._switch_task is not None
    detail.cancel_switch()
    assert _pump(app, lambda: not detail._switch_busy, timeout=8.0)

    assert _sidecar(game, "红石包")["base_version"] == ""
    assert detail.instance.base_version == ""
    assert detail.modify_error.isVisible()
    assert detail.modify_error.text() == i18n.tr("instances.detail.modify.cancelled")


# ---------------------------------------------------------------------------
# 4. A failed install leaves the pointer exactly where it was
# ---------------------------------------------------------------------------


def test_a_failed_install_keeps_the_previous_pointer_and_says_why(app, window, monkeypatch):
    _built, game, detail = window
    calls: list = []
    _stub_install(monkeypatch, game, calls, error=ModsError("网络连接失败"))
    _choose(monkeypatch, "1.20.2")
    _confirm(monkeypatch, backup=False)

    detail.change_game_version()
    assert _pump(app, lambda: not detail._switch_busy and detail.modify_error.isVisible())

    assert _sidecar(game, "红石包")["base_version"] == ""
    assert detail.instance.base_version == ""
    assert "网络连接失败" in detail.modify_error.text()
    assert detail.instance is not None
    assert detail.effective_version_id() == "红石包"


def test_a_failed_pointer_write_keeps_the_previous_pointer_and_says_why(
    app, window, monkeypatch
):
    """The install succeeded but the pointer could not be written: the pointer must not move."""
    _built, game, detail = window
    calls: list = []
    _stub_install(monkeypatch, game, calls)
    monkeypatch.setattr(
        instance_detail,
        "switch_instance_version",
        lambda *a, **kw: (_ for _ in ()).throw(
            InstancesError("切换失败，已恢复原来的指向: verify")
        ),
    )
    _choose(monkeypatch, "1.20.2")
    _confirm(monkeypatch, backup=False)

    detail.change_game_version()
    assert _pump(app, lambda: not detail._switch_busy and detail.modify_error.isVisible())

    assert _sidecar(game, "红石包")["base_version"] == ""
    assert detail.instance.base_version == ""
    assert "切换失败" in detail.modify_error.text()


# ---------------------------------------------------------------------------
# 5. The backup checkbox is honoured, and defaults to checked
# ---------------------------------------------------------------------------


def test_the_backup_defaults_to_checked_and_produces_a_zip(app, window, monkeypatch):
    _built, _game, detail = window
    seen: dict = {}
    _choose(monkeypatch, "1.20.1")
    _confirm(monkeypatch, seen=seen)

    detail.change_game_version()
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.20.1",
    )

    assert seen["checked"] is True
    # Enter is the safe answer: the default button is 取消, and the accepting one names the action.
    assert seen["default"] == i18n.tr("dialog.cancel")
    assert seen["accept"] == i18n.tr("instances.detail.modify.confirm.accept")
    backed = list((paths.launcher_dir() / "backups").glob("*.zip"))
    assert len(backed) == 1


def test_unchecking_the_backup_writes_no_zip(app, window, monkeypatch):
    _built, _game, detail = window
    seen: dict = {}
    _choose(monkeypatch, "1.20.1")
    _confirm(monkeypatch, backup=False, seen=seen)

    detail.change_game_version()
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.20.1",
    )

    assert seen["checked"] is False
    assert not (paths.launcher_dir() / "backups").exists() or not list(
        (paths.launcher_dir() / "backups").glob("*.zip")
    )


# ---------------------------------------------------------------------------
# 6. A loader switch installs the loader, then points at the profile it created
# ---------------------------------------------------------------------------


def test_choosing_a_downloadable_loader_installs_it_then_points_at_its_profile(
    app, window, monkeypatch
):
    _built, game, detail = window
    loader = LoaderVersion(
        loader="fabric", version="0.20.0", game_version="1.21.11", installer_url=""
    )
    monkeypatch.setattr(
        instance_detail,
        "loader_install_options",
        lambda *_a, **_k: ([("fabric", loader)], []),
    )
    calls: list = []
    _stub_install(monkeypatch, game, calls)
    _choose_loader(monkeypatch)
    _confirm(monkeypatch, backup=False)

    detail.change_loader()
    target = "fabric-loader-0.20.0-1.21.11"
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == target,
    ), (detail.instance.base_version, detail.modify_error.text())

    assert calls and calls[0][0] == "1.21.11"
    assert calls[0][1] is loader
    stored = _sidecar(game, "红石包")
    assert stored["base_version"] == target
    assert profile_identity(game, target).minecraft == "1.21.11"
    assert detail.modify_values["loader"].text() == "Fabric 0.20.0"


# ---------------------------------------------------------------------------
# 7. The extracted install body: core then loader, and cancellation after either
# ---------------------------------------------------------------------------


def _install_result(*, failed: list | None = None):
    class _Result:
        pass

    result = _Result()
    result.failed = list(failed or [])
    return result


def test_install_selection_runs_the_core_then_the_loader(monkeypatch, ws_tmp):
    calls: list = []
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_version",
        lambda version_id, **kwargs: calls.append(("core", version_id)) or _install_result(),
    )
    loader = LoaderVersion(
        loader="fabric", version="0.20.0", game_version="1.21.11", installer_url=""
    )
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_loader",
        lambda loader_version, game_dir, **kwargs: calls.append(
            ("loader", loader_version.version)
        )
        or "fabric-loader-0.20.0-1.21.11",
    )

    outcome = install_selection(
        "1.21.11", loader_version=loader, game_dir=ws_tmp / "mc"
    )

    assert calls == [("core", "1.21.11"), ("loader", "0.20.0")]
    assert outcome.profile_id == "fabric-loader-0.20.0-1.21.11"


def test_install_selection_refuses_a_partial_core(monkeypatch, ws_tmp):
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_version",
        lambda version_id, **kwargs: _install_result(failed=[("lib.jar", "timeout")]),
    )
    with pytest.raises(InstallFailed) as caught:
        install_selection("1.21.11", game_dir=ws_tmp / "mc")
    assert i18n.tr("versions.msg.partial", 1) in str(caught.value)


def test_install_selection_can_skip_the_core_and_still_honours_cancel(monkeypatch, ws_tmp):
    calls: list = []
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_version",
        lambda version_id, **kwargs: calls.append("core") or _install_result(),
    )
    loader = LoaderVersion(
        loader="fabric", version="0.20.0", game_version="1.21.11", installer_url=""
    )
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_loader",
        lambda loader_version, game_dir, **kwargs: calls.append("loader")
        or "fabric-loader-0.20.0-1.21.11",
    )
    with pytest.raises(DownloadCancelled):
        install_selection(
            "1.21.11",
            loader_version=loader,
            install_base=False,
            game_dir=ws_tmp / "mc",
            should_stop=lambda: True,
        )
    assert calls == ["loader"]


def test_the_core_folder_is_the_profile_the_chain_really_reads(ws_tmp):
    """``红石包`` inherits ``fabric-loader-…``; that is the folder the core comes from."""
    game = _game(ws_tmp)
    assert core_profile_id(game, "红石包") == "fabric-loader-0.19.3-1.21.11"
    assert core_profile_id(game, "1.20.1") == "1.20.1"
    assert core_profile_id(game, "missing-profile") == "missing-profile"


