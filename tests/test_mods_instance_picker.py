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

"""Installing a mod targets a **local instance**; no dialog builds one from the network.

The resources page used to open a native "select version" dialog that listed the project's
Modrinth versions (loader x game version). That is how a new instance gets built from an online
loader list, which is exactly what the editor asked to remove. The install step is now an
in-window panel that lists the instances already in the current game folder, and the download
resolves the chosen instance's own ``mods/`` directory.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from gui import i18n, inwindow_dialog
from gui.pages import mods_page
from launcher.mods.models import ModFile, ModSearchHit, ModVersion

FABRIC = "fabric-loader-0.16.9-1.21.11"
FORGE = "1.20.1-forge-47.4.22"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


# ---------------------------------------------------------------- fixtures / helpers


def _write_instance(
    game_dir: Path, instance_id: str, *, name: str = "", inherits: str = ""
) -> Path:
    """One version folder, in the shape the launcher writes (folder name == version id)."""
    folder = game_dir / "versions" / instance_id
    folder.mkdir(parents=True, exist_ok=True)
    raw: dict[str, object] = {"id": instance_id}
    if inherits:
        raw["inheritsFrom"] = inherits
    (folder / (instance_id + ".json")).write_text(json.dumps(raw), encoding="utf-8")
    if name:
        (folder / "minepick.json").write_text(
            json.dumps({"id": instance_id, "display_name": name}), encoding="utf-8"
        )
    return folder


def _two_instances(game_dir: Path) -> None:
    _write_instance(game_dir, FABRIC, name="Fabric 生存")
    _write_instance(game_dir, FORGE, name="Forge 建造")


def _cfg(game_dir: Path):
    """Only the fields the install path reads (isolation defaults to on, so mods are per-instance)."""
    return SimpleNamespace(game_dir=game_dir, default_isolation=True)


def _sync_runner(fn, *args, on_result=None, on_error=None, on_finished=None, **_kwargs):
    """Run a page request inline, like tests/test_mods_search.py does."""
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


def _offline_modrinth(monkeypatch, *, loaders: list[str] | None = None) -> ModVersion:
    """Replace the network with one matching version and a downloader that writes the file."""
    from launcher.mods import modrinth

    version = ModVersion(
        version_id="v1",
        version_number="1.0.0",
        loaders=list(loaders or ["fabric"]),
        game_versions=["1.21.11"],
        files=[
            ModFile(filename="sodium.jar", url="https://example.invalid/sodium.jar", size=3)
        ],
    )
    monkeypatch.setattr(modrinth, "fetch_versions", lambda slug, **kw: [version])
    monkeypatch.setattr(
        modrinth, "fetch_project", lambda slug, **kw: {"slug": slug, "title": "Sodium"}
    )
    monkeypatch.setattr(modrinth, "resolve_slugs", lambda project_ids: {})

    class FakeDownloader:
        def download(self, tasks, progress=None):
            for task in tasks:
                task.dest.parent.mkdir(parents=True, exist_ok=True)
                task.dest.write_bytes(b"jar")
            return SimpleNamespace(failed=[])

    monkeypatch.setattr(modrinth, "Downloader", FakeDownloader)
    return version


def _mod_hit(loaders: list[str] | None = None) -> ModSearchHit:
    return ModSearchHit(
        slug="sodium",
        title="Sodium",
        description="Modern rendering engine",
        loaders=list(loaders or ["fabric"]),
        source="modrinth",
    )


def _choose(dialog, instance_id: str):
    """The test's answer to the in-window panel: pick ``instance_id`` and accept."""
    for row in dialog.rows:
        if row.choice.instance_id == instance_id:
            row.click()
            break
    dialog._finish(inwindow_dialog.Outcome.ACCEPTED)
    return inwindow_dialog.Outcome.ACCEPTED


def _close(dialog, app) -> None:
    dialog.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------- 1. the dialog is gone


def test_the_old_version_picker_dialog_does_not_exist_any_more() -> None:
    """No class, no table, and no "select version" title: the whole surface is gone."""
    source = Path(mods_page.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert "_VersionPickerDialog" not in classes
    assert "_LoaderPromptDialog" not in classes
    # The dialog was a native QDialog holding a QTableWidget of network versions; neither import
    # survives, so neither can come back through a "spare path".
    assert "QTableWidget" not in source
    assert "QDialogButtonBox" not in source
    assert "mods.picker.title" not in source
    assert "mods.picker.col." not in source
    assert "mods.picker.ok" not in source
    assert "_pick_version" not in source


# ---------------------------------------------------------------- 2. the panel lists local instances


def test_the_panel_lists_the_instances_of_the_game_folder(app, ws_tmp) -> None:
    game_dir = ws_tmp / "game"
    _two_instances(game_dir)
    choices = mods_page.instance_choices(game_dir, ["fabric"], cfg=_cfg(game_dir))
    assert [choice.instance_id for choice in choices] == [FABRIC, FORGE]
    assert (choices[0].loader_id, choices[0].minecraft) == ("fabric", "1.21.11")
    assert (choices[1].loader_id, choices[1].minecraft) == ("forge", "1.20.1")
    assert choices[0].name == "Fabric 生存"
    assert choices[0].mods_dir == game_dir / "versions" / FABRIC / "mods"

    host = QWidget()
    dialog = mods_page._InstancePickerDialog(host, choices, mod_title="Sodium")
    try:
        # It is a child widget of the window, not a native window of its own.
        assert dialog.parentWidget() is host
        assert dialog.isWindow() is False
        assert (dialog.windowFlags() & Qt.WindowType.WindowType_Mask) == Qt.WindowType.Widget
        assert len(dialog.rows) == 2
        first = dialog.rows[0]
        assert first.choice is choices[0]
        name = first.accessibleName()
        assert "Fabric 生存" in name
        assert FABRIC in name  # the version id
        assert "Fabric" in name  # the loader
        assert first.toolTip() == ""  # nothing to explain on a matching row
        assert dialog.install_button.text() == i18n.tr("mods.instance.install")
        assert dialog.install_button.isEnabled() is False  # nothing chosen yet
    finally:
        _close(dialog, app)


def test_the_install_button_names_the_instance_decision(app) -> None:
    page = mods_page.ResourcesPage()
    try:
        tab = page.mods_tab
        tab._set_entries([_mod_hit()])
        assert tab.install_button.text() == i18n.tr("mods.install_to_instance")
    finally:
        page.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------- 3. the click installs there


def test_the_click_writes_into_the_chosen_instances_mods(app, ws_tmp, monkeypatch) -> None:
    game_dir = ws_tmp / "game"
    _two_instances(game_dir)
    _offline_modrinth(monkeypatch)
    monkeypatch.setattr(mods_page.config, "load", lambda: (_cfg(game_dir), None))
    monkeypatch.setattr(mods_page, "run_in_background", _sync_runner)
    monkeypatch.setattr(
        mods_page._InstancePickerDialog,
        "ask",
        lambda dialog: _choose(dialog, FABRIC),
    )
    statuses: list[str] = []
    monkeypatch.setattr(
        mods_page, "set_app_status", lambda _widget, text, level=None: statuses.append(text)
    )

    page = mods_page.ResourcesPage()
    try:
        tab = page.mods_tab
        before = sorted(path.name for path in (game_dir / "versions").iterdir())
        tab._set_entries([_mod_hit()])
        tab._on_install_clicked()

        installed = game_dir / "versions" / FABRIC / "mods" / "sodium.jar"
        assert installed.read_bytes() == b"jar"
        assert not (game_dir / "versions" / FORGE / "mods").exists()
        # "No new instance": the folder set is byte-for-byte the same afterwards.
        after = sorted(path.name for path in (game_dir / "versions").iterdir())
        assert after == before == sorted([FABRIC, FORGE])
        # The feedback stays on this page and names the instance that just gained the mod.
        assert statuses[-1] == i18n.tr("mods.msg.installed_into", "Fabric 生存", "Sodium", "sodium")
        assert tab.isVisible() is False  # never navigated away; the page object is untouched
    finally:
        page.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------- 4. loader mismatch


def test_a_loader_mismatch_greys_the_row_and_explains_it(app, ws_tmp) -> None:
    game_dir = ws_tmp / "game"
    _two_instances(game_dir)
    choices = mods_page.instance_choices(game_dir, ["fabric"], cfg=_cfg(game_dir))
    forge = next(choice for choice in choices if choice.instance_id == FORGE)
    assert forge.usable is False
    assert forge.note == i18n.tr("mods.instance.mismatch", "Fabric", "Forge")
    assert "Fabric" in forge.note and "Forge" in forge.note

    dialog = mods_page._InstancePickerDialog(None, choices, mod_title="Sodium")
    try:
        row = next(item for item in dialog.rows if item.choice.instance_id == FORGE)
        assert row.isEnabled() is False
        assert row.isCheckable() is False
        assert forge.note in row.accessibleName()
        assert row.toolTip() == forge.note
        row.click()  # a disabled choice cannot be taken
        assert row.isChecked() is False
        assert dialog.selected_choice() is None
        assert dialog.install_button.isEnabled() is False
    finally:
        _close(dialog, app)


def test_a_vanilla_instance_explains_by_itself(app, ws_tmp) -> None:
    game_dir = ws_tmp / "game"
    _write_instance(game_dir, "1.21.11", name="原版")
    choices = mods_page.instance_choices(game_dir, ["fabric"], cfg=_cfg(game_dir))
    assert choices[0].loader_id == "vanilla"
    assert choices[0].note == i18n.tr("mods.instance.mismatch.vanilla", "Fabric")
    assert choices[0].usable is False


def test_a_mod_without_loader_metadata_is_not_greyed(app, ws_tmp) -> None:
    """Unknown loaders are "the source did not say", never "incompatible"."""
    game_dir = ws_tmp / "game"
    _two_instances(game_dir)
    choices = mods_page.instance_choices(game_dir, [], cfg=_cfg(game_dir))
    assert all(choice.usable for choice in choices)


# ---------------------------------------------------------------- 5. empty folder


def test_an_empty_folder_says_so_and_points_at_the_versions_page(app, ws_tmp, monkeypatch) -> None:
    game_dir = ws_tmp / "game"
    (game_dir / "versions").mkdir(parents=True)
    choices = mods_page.instance_choices(game_dir, ["fabric"], cfg=_cfg(game_dir))
    assert choices == []

    dialog = mods_page._InstancePickerDialog(None, choices, mod_title="Sodium")
    try:
        assert dialog.rows == []
        assert dialog.list_area.isHidden() is True
        assert dialog.empty_state.isHidden() is False
        assert dialog.empty_state.text() == i18n.tr("mods.instance.empty")
        assert dialog.versions_button.isHidden() is False
        assert dialog.versions_button.text() == i18n.tr("mods.instance.go_versions")
        assert dialog.install_button.isEnabled() is False
        dialog.versions_button.click()
        assert dialog.wants_versions is True
    finally:
        _close(dialog, app)


def test_the_empty_state_opens_the_versions_page(app, ws_tmp, monkeypatch) -> None:
    game_dir = ws_tmp / "game"
    (game_dir / "versions").mkdir(parents=True)
    monkeypatch.setattr(mods_page.config, "load", lambda: (_cfg(game_dir), None))
    visited: list[bool] = []

    def answer(dialog):
        dialog.versions_button.click()
        return inwindow_dialog.Outcome.REJECTED

    monkeypatch.setattr(mods_page._InstancePickerDialog, "ask", answer)
    page = mods_page.ResourcesPage()
    try:
        tab = page.mods_tab
        monkeypatch.setattr(tab, "_go_to_versions", lambda: visited.append(True))
        tab._install_hit(_mod_hit())
        assert visited == [True]
    finally:
        page.deleteLater()
        app.processEvents()
