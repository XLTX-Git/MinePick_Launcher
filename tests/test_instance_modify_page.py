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
"""修改: the staged plan, the three loader-row states, the in-panel picker and 开始重置.

The page is a plan editor now. Clicking ✕ or choosing a build changes *what 开始修改 would do* and
nothing on disk; only the primary button runs the existing switch flow (confirm, optional backup,
install if needed, pointer move). These tests pin that boundary first - a staged plan leaves the
instance sidecar byte-identical - and then each state of the four component rows.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool

from gui import i18n, inwindow_dialog
from gui.pages import instance_detail
from gui.pages.instance_detail import (
    MODIFY_LOADERS,
    TAB_MODIFY,
    _SwitchChoice,
)
from gui.pages.versions_install_flow import InstallOutcome
from gui.tasks import DONE
from launcher import config as config_mod
from launcher.instances import (
    INSTANCE_META_FILENAME,
    Instance,
    instance_dir,
)
from launcher.mods.models import LoaderVersion

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
    """No request may outlive a test: the catalog, the loader sources and the lookups are stubbed."""

    def _fail(*args, **kwargs):
        raise RuntimeError("offline test")

    monkeypatch.setattr(instance_detail, "fetch_catalog", _fail)
    monkeypatch.setattr(instance_detail, "list_loader_versions", lambda *a, **kw: [])
    monkeypatch.setattr(instance_detail, "_CATALOG_CACHE", None)
    yield


def _write_profile(game: Path, version_id: str, *, inherits: str | None = None) -> Path:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    raw = {
        "id": version_id,
        "type": "release",
        "mainClass": "net.minecraft.client." + version_id + ".Main",
        "libraries": [],
    }
    if inherits:
        raw["inheritsFrom"] = inherits
    (folder / (version_id + ".json")).write_text(json.dumps(raw), encoding="utf-8")
    (folder / (version_id + ".jar")).write_bytes(b"jar")
    return folder


def _make_instance(game: Path, instance_id: str, *, inherits: str | None = None) -> Path:
    folder = _write_profile(game, instance_id, inherits=inherits)
    meta = Instance(id=instance_id, display_name=instance_id, created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    (folder / "mods" / "sodium.jar").write_bytes(b"PK\x03\x04sodium")
    return folder


def _game(ws_tmp: Path) -> Path:
    game = ws_tmp / "mc"
    _make_instance(game, "1.21.11")
    _make_instance(game, "fabric-loader-0.19.3-1.21.11", inherits="1.21.11")
    _make_instance(game, "红石包", inherits="fabric-loader-0.19.3-1.21.11")
    return game


def _write_manifest(data: Path, versions: list[tuple[str, str, str]]) -> None:
    cache = data / "cache" / "version_manifest.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "manifest": {
            "latest": {"release": versions[0][0], "snapshot": versions[-1][0]},
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
        if predicate is None or predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate and predicate())


@pytest.fixture()
def window(app, monkeypatch, ws_tmp):
    """A real window with the fixture game folder, opened on the Fabric instance."""
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
    from gui.main_window import NAV_KEYS

    built.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, built)
    page = built.pages["instances"]
    # The card list is scanned in the background: retry until the instance is there.
    for _ in range(300):
        app.processEvents()
        if page.open_instance("红石包"):
            break
        time.sleep(0.01)
    else:  # pragma: no cover - a missing fixture is a red test, not a silent skip
        raise AssertionError("the instance never appeared in the list")
    _settle(app, built)
    detail = page.detail
    detail.tabs[TAB_MODIFY].click()
    _settle(app, built)
    yield built, game, detail
    i18n.set_language("zh_cn")
    built.close()
    built.deleteLater()


def _sidecar(game: Path, instance_id: str) -> str:
    return (instance_dir(game, instance_id) / INSTANCE_META_FILENAME).read_text(
        encoding="utf-8"
    )


def _accept_confirm(monkeypatch, *, backup: bool = False) -> None:
    def answer(dialog) -> object:
        dialog.backup_check.setChecked(backup)
        return inwindow_dialog.Outcome.ACCEPTED

    monkeypatch.setattr(instance_detail._SwitchConfirmDialog, "ask", answer)


def _row(detail, key: str):
    return detail.modify_rows[key]


# ---------------------------------------------------------------------------
# 1. The four rows and the compatibility matrix
# ---------------------------------------------------------------------------


def test_the_rows_show_the_current_core_and_the_incompatible_loaders(window):
    _built, _game, detail = window
    assert [key for key in detail.modify_rows] == ["minecraft", *MODIFY_LOADERS]
    assert _row(detail, "minecraft").value_text() == "1.21.11"
    assert _row(detail, "fabric").value_text() == "0.19.3"
    assert detail.modify_summary_subtitle.text() == "1.21.11 · Fabric 0.19.3"
    # The loaded loader offers both actions; the two others explain why they cannot be used.
    assert _row(detail, "fabric").change_button.isVisibleTo(detail)
    assert _row(detail, "fabric").remove_button.isVisibleTo(detail)
    for other in ("forge", "neoforge"):
        assert _row(detail, other).value_text() == i18n.tr(
            "instances.detail.modify.incompatible", "Fabric"
        )
        assert _row(detail, other).value_muted()
        assert not _row(detail, other).change_button.isVisibleTo(detail)
    assert detail.reset_button.text() == i18n.tr("instances.detail.modify.reset.start")


def test_a_vanilla_instance_offers_all_three_loaders(app, window, monkeypatch):
    built, _game, _detail = window
    page = built.pages["instances"]
    assert page.open_instance("1.21.11")
    _settle(app, built)
    detail = page.detail
    detail.tabs[TAB_MODIFY].click()
    _settle(app, built)
    for key in MODIFY_LOADERS:
        assert _row(detail, key).value_text() == i18n.tr(
            "instances.detail.modify.not_added"
        )
        assert _row(detail, key).change_button.isVisibleTo(detail)
        assert not _row(detail, key).remove_button.isVisibleTo(detail)


# ---------------------------------------------------------------------------
# 2. Staging changes nothing on disk
# ---------------------------------------------------------------------------


def test_removing_the_loader_stages_a_plan_and_touches_nothing(app, window, monkeypatch):
    _built, game, detail = window
    before = _sidecar(game, "红石包")
    detail.remove_loader("fabric")
    _settle(app, detail)

    assert _sidecar(game, "红石包") == before  # the plan is not written
    assert detail.planned_target() is not None
    assert detail.modify_summary_subtitle.text() == "1.21.11 · Fabric 0.19.3 → 1.21.11 · 原版"
    assert detail.reset_button.text() == i18n.tr("instances.detail.modify.start.modify")
    for key in MODIFY_LOADERS:
        assert _row(detail, key).value_text() == i18n.tr(
            "instances.detail.modify.not_added"
        )
        assert _row(detail, key).change_button.isVisibleTo(detail)


def test_applying_the_plan_runs_the_switch_and_clears_it(app, window, monkeypatch):
    _built, game, detail = window
    _accept_confirm(monkeypatch)
    detail.remove_loader("fabric")
    detail.apply_plan()

    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.21.11",
    ), detail.modify_error.text()
    assert detail.planned_target() is None
    assert detail.reset_button.text() == i18n.tr("instances.detail.modify.reset.start")
    assert detail.modify_summary_subtitle.text() == "1.21.11 · 原版"
    assert _row(detail, "fabric").value_text() == i18n.tr(
        "instances.detail.modify.not_added"
    )
    assert _row(detail, "fabric").change_button.isVisibleTo(detail)
    stored = json.loads(_sidecar(game, "红石包"))
    assert stored["base_version"] == "1.21.11"


def test_a_failed_apply_keeps_the_plan_and_shows_the_error(app, window, monkeypatch):
    _built, game, detail = window
    _accept_confirm(monkeypatch)
    monkeypatch.setattr(
        instance_detail,
        "switch_instance_version",
        lambda *a, **kw: (_ for _ in ()).throw(
            instance_detail.InstancesError("切换失败，已恢复原来的指向")
        ),
    )
    detail.remove_loader("fabric")
    detail.apply_plan()

    assert _pump(app, lambda: not detail._switch_busy and detail.modify_error.isVisible())
    assert detail.planned_target() is not None  # the plan survives a failure
    assert "切换失败" in detail.modify_error.text()
    assert json.loads(_sidecar(game, "红石包"))["base_version"] == ""


def test_cancelling_the_confirmation_changes_nothing(app, window, monkeypatch):
    _built, game, detail = window
    monkeypatch.setattr(
        instance_detail._SwitchConfirmDialog,
        "ask",
        lambda dialog: inwindow_dialog.Outcome.REJECTED,
    )
    before = _sidecar(game, "红石包")
    detail.remove_loader("fabric")
    detail.apply_plan()
    _settle(app, detail)
    assert _sidecar(game, "红石包") == before
    assert detail.instance.base_version == ""


# ---------------------------------------------------------------------------
# 3. The Minecraft picker is a sub-view of 修改, never a dialog
# ---------------------------------------------------------------------------


def test_the_version_picker_replaces_the_panel_and_keeps_the_sidebar(app, window):
    _built, _game, detail = window
    checked = detail.tab_group.checkedId()
    detail.change_game_version()
    _settle(app, detail)

    assert detail.modify_stack.currentIndex() == 1
    assert detail.tab_group.checkedId() == checked  # the sidebar selection did not move
    assert detail.picker_title.text() == i18n.tr("instances.detail.modify.pick.version")
    detail.picker_back_button.click()
    _settle(app, detail)
    assert detail.modify_stack.currentIndex() == 0
    assert detail.planned_target() is None
    assert detail.tab_group.checkedId() == checked


def test_the_picker_lists_installed_and_downloadable_versions(app, window):
    _built, _game, detail = window
    detail.change_game_version()
    _settle(app, detail)
    # Four folds, all collapsed: the counts are there before any row is built.
    headers = detail.picker_body.findChildren(instance_detail.VersionGroupHeader)
    assert len(headers) == 4
    assert sum(header.count for header in headers) == 4
    # Expanding "release" builds its rows: 1.21.11 is installed, 1.20.x is not.
    detail._toggle_picker_group("release")
    _settle(app, detail)
    rows = detail.picker_body.findChildren(instance_detail._VersionChoiceRow)
    assert rows
    statuses = {row.choice.version_id: row.status_text() for row in rows}
    assert statuses["1.21.11"] == i18n.tr("instances.detail.modify.status.installed")
    assert statuses["1.20.2"] == i18n.tr("instances.detail.modify.status.download")


def test_picking_a_version_stages_it_instead_of_switching(app, window):
    _built, _game, detail = window
    detail.change_game_version()
    _settle(app, detail)
    detail._toggle_picker_group("release")
    _settle(app, detail)
    row = next(
        row
        for row in detail.picker_body.findChildren(instance_detail._VersionChoiceRow)
        if row.choice.version_id == "1.20.2"
    )
    row.click()
    _settle(app, detail)

    assert detail.modify_stack.currentIndex() == 0
    assert detail.planned_target() is not None
    assert detail.planned_target().version_id == "1.20.2"
    assert "→ 1.20.2" in detail.modify_summary_subtitle.text()
    assert _row(detail, "minecraft").value_text() == "1.20.2"
    # 1.20.2 is a different game version: the staged loader is dropped, and each loader row is
    # resolved against the *new* version (the stub source has nothing for it, so: 无可用版本).
    for key in MODIFY_LOADERS:
        assert _row(detail, key).value_text() == i18n.tr(
            "instances.detail.modify.no_versions"
        )
        assert not _row(detail, key).change_button.isVisibleTo(detail)


# ---------------------------------------------------------------------------
# 4. Loader rows expand in place
# ---------------------------------------------------------------------------


def _stub_builds(monkeypatch, builds):
    monkeypatch.setattr(
        instance_detail, "list_loader_versions", lambda loader, minecraft: list(builds)
    )


def test_a_loader_row_expands_with_stability_and_only_one_at_a_time(app, window, monkeypatch):
    _built, _game, detail = window
    builds = [
        LoaderVersion(loader="fabric", version="0.20.1", game_version="1.21.11", stable=True),
        LoaderVersion(loader="fabric", version="0.20.0", game_version="1.21.11", stable=False),
    ]
    _stub_builds(monkeypatch, builds)
    detail.change_loader("fabric")
    assert _pump(app, lambda: detail.modify_rows["fabric"].body_layout.count() > 0)
    _settle(app, detail)
    items = detail.modify_rows["fabric"].body.findChildren(instance_detail._LoaderBuildRow)
    # The installed profile it runs now comes first (marked 当前), then the two downloadable builds.
    assert len(items) == 3
    assert items[0].current
    assert {item.stability_text() for item in items if item.stability_text()} == {
        i18n.tr("instances.detail.modify.build.stable"),
        i18n.tr("instances.detail.modify.build.beta"),
    }
    assert detail.modify_rows["fabric"].is_expanded()

    # Another row replaces the first one instead of adding a second open row.
    detail.change_loader("forge")
    assert _pump(app, lambda: detail.modify_rows["forge"].is_expanded())
    assert not detail.modify_rows["fabric"].is_expanded()


def test_choosing_a_build_stages_it_and_collapses_the_row(app, window, monkeypatch):
    _built, _game, detail = window
    build = LoaderVersion(
        loader="fabric", version="0.20.1", game_version="1.21.11", stable=True
    )
    _stub_builds(monkeypatch, [build])
    detail.change_loader("fabric")
    assert _pump(
        app,
        lambda: detail.modify_rows["fabric"].body.findChildren(
            instance_detail._LoaderBuildRow
        ),
    )
    _settle(app, detail)
    item = next(
        row
        for row in detail.modify_rows["fabric"].body.findChildren(
            instance_detail._LoaderBuildRow
        )
        if row.choice.loader_version is not None
    )
    item.click()
    _settle(app, detail)

    plan = detail.planned_target()
    assert plan is not None and plan.loader_version is build
    assert not detail.modify_rows["fabric"].is_expanded()
    assert "→ 1.21.11 · Fabric 0.20.1" in detail.modify_summary_subtitle.text()
    # Staging a loader rewrites the other loaders' reason, including the installed one.
    assert _row(detail, "forge").value_text() == i18n.tr(
        "instances.detail.modify.incompatible", "Fabric"
    )


def test_a_loader_that_has_no_version_says_so_and_offers_no_action(app, window, monkeypatch):
    _built, _game, detail = window
    # Stage a different game version so the page has to ask the loader sources about it.
    detail._pick_choice(
        _SwitchChoice(version_id="26w02a", minecraft="26w02a", installed=False)
    )
    _stub_builds(monkeypatch, [])
    assert _pump(
        app,
        lambda: _row(detail, "fabric").value_text()
        == i18n.tr("instances.detail.modify.no_versions"),
    )
    row = _row(detail, "fabric")
    assert row.value_muted()
    assert not row.change_button.isVisibleTo(detail)


# ---------------------------------------------------------------------------
# 5. 开始重置 re-installs the current core
# ---------------------------------------------------------------------------


def test_reset_reinstalls_the_current_core_through_the_install_path(app, window, monkeypatch):
    _built, _game, detail = window
    calls: list = []

    def fake_install(version_id, **kwargs):
        calls.append((version_id, kwargs.get("loader_version")))
        return InstallOutcome(profile_id="fabric-loader-0.19.3-1.21.11")

    build = LoaderVersion(
        loader="fabric", version="0.19.3", game_version="1.21.11", stable=True
    )
    monkeypatch.setattr(instance_detail, "install_selection", fake_install)
    monkeypatch.setattr(instance_detail, "list_loader_versions", lambda *a, **kw: [build])
    monkeypatch.setattr(
        instance_detail.inwindow_dialog,
        "confirm",
        lambda *a, **kw: inwindow_dialog.Outcome.ACCEPTED,
    )

    detail.reset_core()
    assert _pump(app, lambda: bool(calls) and not detail._switch_busy)
    assert calls == [("1.21.11", build)]
    assert detail.instance.base_version == ""  # a reset never moves the pointer
    assert detail.planned_target() is None
    assert detail.modify_result.isVisibleTo(detail)


def test_reset_failure_keeps_the_pointer_and_shows_the_error(app, window, monkeypatch):
    _built, game, detail = window

    def broken_install(version_id, **kwargs):
        raise instance_detail.InstancesError("下载失败")

    monkeypatch.setattr(instance_detail, "install_selection", broken_install)
    monkeypatch.setattr(instance_detail, "list_loader_versions", lambda *a, **kw: [])
    monkeypatch.setattr(
        instance_detail.inwindow_dialog,
        "confirm",
        lambda *a, **kw: inwindow_dialog.Outcome.ACCEPTED,
    )

    detail.reset_core()
    assert _pump(app, lambda: not detail._switch_busy and detail.modify_error.isVisible())
    assert "下载失败" in detail.modify_error.text()
    assert json.loads(_sidecar(game, "红石包"))["base_version"] == ""
    assert _row(detail, "fabric").value_text() == "0.19.3"


def test_reset_asks_first_and_does_nothing_when_rejected(app, window, monkeypatch):
    _built, _game, detail = window
    calls: list = []
    monkeypatch.setattr(
        instance_detail, "install_selection", lambda *a, **kw: calls.append(1)
    )
    monkeypatch.setattr(
        instance_detail.inwindow_dialog,
        "confirm",
        lambda *a, **kw: inwindow_dialog.Outcome.REJECTED,
    )
    detail.reset_core()
    _settle(app, detail)
    assert calls == []


# ---------------------------------------------------------------------------
# 6. The task row and the lazy placeholder
# ---------------------------------------------------------------------------


def test_the_switch_shows_up_in_the_window_task_list(app, window, monkeypatch):
    built, _game, detail = window
    _accept_confirm(monkeypatch)
    detail.remove_loader("fabric")
    detail.apply_plan()
    assert _pump(
        app,
        lambda: detail.instance is not None and detail.instance.base_version == "1.21.11",
    )
    tasks = built.tasks.tasks()
    assert [task.state for task in tasks if task.kind == "version"] == [DONE]

def test_the_verify_button_completes_the_instance_without_rebuilding_it(
    app, window, monkeypatch
):
    """校验并补全 is the manual half of the pre-launch repair: same planner, no core rebuild."""
    _built, game, detail = window
    called: list[tuple[str, Path]] = []

    class _Plan:
        tasks = [object(), object()]
        total_bytes = 5 * 1024 * 1024

    def fake_repair(version_id, *, game_dir, **kwargs):
        called.append((version_id, game_dir))
        return _Plan()

    monkeypatch.setattr("launcher.repair.repair", fake_repair)
    detail.verify_button.click()
    for _ in range(200):
        app.processEvents()
        if called:
            break
        time.sleep(0.01)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.01)

    assert called and called[0][1] == game
    assert "2" in detail.modify_result.text(), detail.modify_result.text()
    assert detail.verify_button.isEnabled()


def test_the_verify_button_says_so_when_nothing_is_missing(app, window, monkeypatch):
    _built, _game, detail = window

    class _Plan:
        tasks: list[object] = []
        total_bytes = 0

    monkeypatch.setattr("launcher.repair.repair", lambda version_id, **kw: _Plan())
    detail.verify_button.click()
    for _ in range(200):
        app.processEvents()
        if detail.modify_result.text() not in ("", i18n.tr("instances.detail.modify.verify.checking")):
            break
        time.sleep(0.01)
    assert detail.modify_result.text() == i18n.tr("instances.detail.modify.verify.ok")
