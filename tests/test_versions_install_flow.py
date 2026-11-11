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

"""The versions page's install step: an in-page flow, and the install body it must not touch.

Two things are pinned here, and they pull in opposite directions on purpose:

* the **step** - choosing a loader and naming the instance on the page itself, the breadcrumb coming
  back to the list the user left, the two loader states that must never be folded together, and the
  vanilla button that is a real option - is behaviour, so it is driven through the page's widgets;
* the **download reporting** - ``_on_progress`` / ``_on_installed`` / ``_on_install_error`` - is *not*
  behaviour to re-test but a contract: the tests at the bottom parse this render's own methods and
  the sources frozen from HEAD (``FROZEN_INSTALL_FLOW``) and require the same AST. The install entry
  and the instance creation deliberately changed (loader build selection, one new instance per
  install), so those two methods are covered by the behaviour tests above instead.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import time
from types import SimpleNamespace

import pytest

from launcher.instances import list_instances, update_instance
from launcher.meta.manifest import ManifestVersion
from launcher.mods import loaders as loader_sources
from launcher.mods.models import LoaderVersion


@pytest.fixture(scope="module")
def app():
    """The suite's QApplication, built the way the launcher builds it (theme + focus tracker).

    ``create_app`` is what installs the keyboard-only focus tracker and the stylesheet; a bare
    ``QApplication`` would leave every focus ring unpainted, and the focus test below would then be
    measuring the harness instead of the page.
    """
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


def _pump(app, predicate=None, timeout: float = 8.0) -> bool:
    """Run the event loop until ``predicate`` holds (the page's work happens in the worker pool)."""
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


def _no_network(**kwargs):
    raise RuntimeError("offline test")


def _versions() -> list[ManifestVersion]:
    """A small manifest: enough releases to scroll, plus one of each other category."""
    versions = [
        ManifestVersion(
            id=f"rel-{index}",
            type="release",
            url="https://example.invalid/v.json",
            time="",
            release_time=f"2025-01-{index:02d}T12:00:00+00:00",
        )
        for index in range(1, 26)
    ]
    versions += [
        ManifestVersion(
            id="26.4-snapshot-6",
            type="snapshot",
            url="https://example.invalid/v.json",
            time="",
            release_time="2026-03-24T12:00:00+00:00",
        ),
        ManifestVersion(
            id="24w14potato",
            type="snapshot",
            url="https://example.invalid/v.json",
            time="",
            release_time="2024-04-01T12:00:00+00:00",
        ),
        ManifestVersion(
            id="b1.7.3",
            type="old_beta",
            url="https://example.invalid/v.json",
            time="",
            release_time="2011-07-08T12:00:00+00:00",
        ),
    ]
    return versions


def _make_instance(game_dir, instance_id: str, display_name: str) -> None:
    """An installed profile whose sidecar carries ``display_name`` (what the instances page reads)."""
    folder = game_dir / "versions" / instance_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{instance_id}.json").write_text("{}", encoding="utf-8")
    update_instance(instance_id, game_dir=game_dir, display_name=display_name)


@pytest.fixture()
def page(app, monkeypatch, ws_tmp):
    """A real VersionsPage, offscreen, with a fixed manifest and an empty game directory."""
    game = ws_tmp / "game"
    game.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))

    from launcher import config as config_mod

    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)

    from gui.pages import versions_install_flow, versions_page

    monkeypatch.setattr(versions_page, "fetch_manifest", _no_network)
    # Every loader can add every version unless a test says otherwise: one probe seam, no network.
    monkeypatch.setattr(
        versions_install_flow,
        "probe_loader",
        lambda loader, version_id: [
            LoaderVersion(
                loader=loader, version="1.0.0", game_version=version_id, recommended=True
            )
        ],
    )

    built = versions_page.VersionsPage()
    built.resize(920, 560)
    built.show()
    app.processEvents()
    built._on_manifest(
        SimpleNamespace(
            versions=_versions(), latest={"release": "rel-1", "snapshot": "26.4-snapshot-6"}
        )
    )
    # The page opens with all four folds closed (that is its contract); these tests drive rows, so
    # the release fold is opened through the same public call the header uses.
    built.expand_group("release")
    app.processEvents()
    yield built
    built.close()


def _open_step(page, app, version_id: str):
    """Open the install step and wait until no loader row is still checking."""
    from gui.pages import versions_install_flow

    page.begin_install(version_id)
    _pump(
        app,
        lambda: all(
            page.step.loader_status(loader) != versions_install_flow.CHECKING
            for loader in versions_install_flow.LOADER_ROWS
        ),
    )
    return page.step


# ---------- the breadcrumb: back returns to the same list ----------


def test_back_returns_to_the_same_filter_and_scroll_position(page, app) -> None:
    list_widget = page.list
    # Four fold headers plus the 25 release rows the fixture opened.
    assert list_widget.count() == 29
    page.list.setCurrentRow(7)
    app.processEvents()
    scroll_range = list_widget.verticalScrollBar().maximum()
    assert scroll_range > 0, "the manifest must be long enough for this test to scroll"
    page.list.verticalScrollBar().setValue(scroll_range - 40)
    kept_row = page.list.currentRow()
    kept_scroll = page.list.verticalScrollBar().value()
    assert kept_scroll > 0

    step = _open_step(page, app, "rel-12")
    assert page.stack.currentWidget() is step
    assert app.activeModalWidget() is None, "the step must not open a window"

    step.back_button.click()
    app.processEvents()

    assert page.stack.currentWidget() is page._list_step
    assert page.list is list_widget, "the list itself must survive the step"
    assert page.list.count() == 29
    assert page.list.verticalScrollBar().value() == kept_scroll
    assert page.list.currentRow() == kept_row


def test_back_button_uses_the_site_back_shape(page, app) -> None:
    """The breadcrumb back button is the shared #backButton, not a full outlined secondary button."""
    step = _open_step(page, app, "rel-12")
    button = step.back_button
    assert button.objectName() == "backButton"
    assert button.text().startswith("← ")
    assert button.height() <= 30, f"the back button is {button.height()}px tall"


# ---------- the instance name ----------


def test_an_illegal_name_is_flagged_while_typing(page, app) -> None:
    from gui import i18n

    step = _open_step(page, app, "rel-2")
    assert step.name_edit.text() == "rel-2"

    step.name_edit.setText('my/name:c*?"')
    app.processEvents()

    assert step.name_edit.property("invalid") is True
    assert step.name_hint.isVisible()
    hint = step.name_hint.text()
    assert hint == i18n.tr("versions.install.name.invalid")
    for char in '/\\:*?"<>|':
        assert char in hint
    assert not step.name_valid()
    assert not step.start_button.isEnabled()

    step.name_edit.setText("a clean name")
    app.processEvents()
    assert step.name_edit.property("invalid") is False
    assert step.name_valid()
    assert step.start_button.isEnabled()


def test_an_emptied_name_falls_back_to_the_default_on_focus_out(page, app) -> None:
    step = _open_step(page, app, "rel-3")
    step.name_edit.setFocus()
    app.processEvents()
    step.name_edit.setText("   ")
    app.processEvents()
    step.start_button.setFocus()  # leaving the field
    app.processEvents()

    assert step.name_edit.text() == "rel-3"
    assert step.instance_name() == "rel-3"
    assert step.start_button.isEnabled()


def test_a_taken_name_is_a_hard_error_with_the_next_free_alternative(page, app) -> None:
    game = page._game_dir()
    _make_instance(game, "other", "rel-4")
    _make_instance(game, "other-2", "rel-4-2")

    step = _open_step(page, app, "rel-4")
    # The default is already the first free name (the step must not open on a name it refuses).
    assert step.instance_name() == "rel-4-3"
    assert step.name_valid()

    step.name_edit.setText("rel-4")
    app.processEvents()

    assert "rel-4-3" in step.name_hint.text()
    # A duplicate folder would overwrite an instance, so this is a refusal now, not advice.
    assert step.start_button.isEnabled() is False
    assert step.name_valid() is False

    step.name_edit.setText("rel-4-3")
    app.processEvents()
    assert step.start_button.isEnabled()
    assert step.name_valid()


# ---------- the loaders ----------


def test_only_loaders_the_install_path_supports_get_a_row(page, app) -> None:
    from gui.pages import versions_install_flow

    step = _open_step(page, app, "rel-5")
    assert step.loader_names() == ["Forge", "NeoForge", "Fabric"]
    assert "Quilt" not in step.loader_names()
    assert "OptiFine" not in step.loader_names()
    # The rows come from the tuple the install path accepts, not from a display-name table.
    assert versions_install_flow.LOADER_ROWS == tuple(
        loader
        for loader in ("forge", "neoforge", "fabric", "quilt")
        if loader in loader_sources.LOADERS
    )


def test_no_version_and_a_failed_fetch_are_two_different_states(page, app, monkeypatch) -> None:
    from gui import i18n
    from gui.pages import versions_install_flow

    calls: list[tuple[str, str]] = []

    def scripted(loader: str, version_id: str):
        calls.append((loader, version_id))
        if loader == "forge":
            return []  # a real answer: this loader has nothing for that game version
        if loader == "neoforge":
            raise RuntimeError("the loader source did not answer")
        return [object()]

    monkeypatch.setattr(versions_install_flow, "probe_loader", scripted)
    step = _open_step(page, app, "rel-6")

    assert step.loader_status("forge") == versions_install_flow.NO_VERSION
    assert step.loader_status("neoforge") == versions_install_flow.FETCH_FAILED
    assert step.loader_status("fabric") == versions_install_flow.CAN_ADD
    no_version = step.loader_status_text("forge")
    failed = step.loader_status_text("neoforge")
    assert no_version != failed
    assert no_version == i18n.tr("versions.install.loader.no_version")
    assert failed == i18n.tr("versions.install.loader.failed")
    # Only the failed one offers a retry, and it carries why it failed.
    assert step.rows["neoforge"].isEnabled()
    assert "did not answer" in step.rows["neoforge"].toolTip()
    assert not step.rows["forge"].isEnabled()
    assert not step.rows["forge"].isCheckable()

    before = len(calls)
    step.rows["neoforge"].click()  # a failed row retries instead of being selected
    _pump(app, lambda: step.loader_status("neoforge") != versions_install_flow.CHECKING)
    assert len(calls) > before
    assert step.selected_loader() is None


def test_the_pill_downloads_vanilla_until_a_loader_is_chosen(page, app) -> None:
    from gui import i18n

    step = _open_step(page, app, "rel-7")
    assert step.selected_loader() is None
    assert step.start_button.text() == i18n.tr("versions.install.vanilla")
    assert step.start_button.isEnabled(), "the vanilla download is a real option, not a fallback"
    assert step.start_button.height() == 48

    step.rows["forge"].click()
    app.processEvents()
    assert step.selected_loader() == "forge"
    assert step.start_button.text() == i18n.tr("versions.install.start")

    step.rows["fabric"].click()
    app.processEvents()
    assert step.selected_loader() == "fabric", "one loader at a time"

    step.rows["fabric"].click()  # the same row again: back to vanilla
    app.processEvents()
    assert step.selected_loader() is None
    assert step.start_button.text() == i18n.tr("versions.install.vanilla")


def test_an_installed_version_can_be_installed_again_with_its_own_name(page, app) -> None:
    """Installing a version twice is a normal setup (two instances), never a disabled state."""
    from gui import i18n

    game = page._game_dir()
    _make_instance(game, "rel-8", "already here")
    page.refresh_installed()

    step = _open_step(page, app, "rel-8")
    assert step.name_editable() is True
    assert step.name_edit.isVisible() is True
    # The existing folder is taken, so the step opens on the first free name, not on "rel-8".
    assert step.name_edit.text() == "rel-8-2"
    assert step.name_valid()
    assert step.start_button.isEnabled()

    row = page._row_for("rel-8")
    assert row is not None and row.installed
    assert row.install.text() == i18n.tr("versions.install.short"), (
        "an installed row must still offer the same install action"
    )


def test_the_step_is_walkable_with_the_keyboard_and_paints_its_focus(page, app) -> None:
    """The step is a hidden stack page, so the walkthrough tool never scores it: this is the check.

    A keyboard user must be able to Tab from the name field through the rows to the pill, every stop
    must be marked (the ``keyboardFocus`` property the focus rules are gated on), and the painted row
    must actually change pixels - a rule in the stylesheet is not the same thing as a visible cue.
    """
    from pathlib import Path

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from gui.widgets import KeyboardFocusTracker

    tracker = KeyboardFocusTracker(app)
    app.installEventFilter(tracker)
    try:
        step = _open_step(page, app, "rel-14")
        step.name_edit.setFocus()
        app.processEvents()

        row = step.rows["forge"]

        def pixels(widget) -> bytes:
            image = widget.grab().toImage()
            return bytes(image.constBits())

        before = pixels(row)
        focused = None
        for target in (row, step.rows["neoforge"], step.rows["fabric"], step.start_button):
            QTest.keyClick(app.focusWidget(), Qt.Key.Key_Tab)
            app.processEvents()
            assert app.focusWidget() is target, f"Tab did not reach {type(target).__name__}"
            assert target.property("keyboardFocus") is True
            if target is row:
                # Grabbed while this row holds focus: the cue is only painted then.
                focused = pixels(row)
        assert focused is not None and focused != before, "the focused loader row painted no cue"
        # A row with no version for this game version is disabled, so Tab skips it instead of parking
        # the keyboard on a control that cannot do anything (the states test drives that case).
        assert all(step.rows[loader].isEnabled() for loader in step.rows)

        # The pill paints its ring from the stylesheet, which the walkthrough cannot see on a hidden
        # page; this is the same selector scan it does for the stops it walks.
        resources = Path(__file__).resolve().parent.parent / "gui" / "resources"
        for sheet in ("style.qss", "style_light.qss"):
            text = (resources / sheet).read_text(encoding="utf-8")
            assert 'QPushButton#installPrimary[keyboardFocus="true"]' in text, sheet
            assert 'QAbstractButton#loaderChoiceRow[keyboardFocus="true"]' in text, sheet
    finally:
        app.removeEventFilter(tracker)


# ---------- the row progress ----------


def test_the_row_shows_the_download_and_then_says_installed(page, app) -> None:
    from gui import i18n

    row = page._row_for("rel-9")
    assert row is not None and not row.install_running()
    assert row.install.text() == i18n.tr("versions.install.short")

    page._attach_inline_progress("rel-9")
    app.processEvents()
    assert row.install_running()
    assert not row.install.isVisible(), "the install button is the progress block while it runs"
    assert row.install.text() == i18n.tr("versions.install.short")

    page.progress_bar.setRange(0, 1000)
    page.progress_bar.setValue(420)
    app.processEvents()
    assert row.progress_value() == 420

    # The end of a download is the page's install button being handed back, which is what the flow's
    # on_finished does (the bottom bar that used to signal this is gone - see the next test).
    page.install_button.setEnabled(False)  # what the flow does when an install starts
    app.processEvents()
    page.install_button.setEnabled(True)
    app.processEvents()
    assert not row.install_running()
    assert row.install.isVisible()

    # A completed install rebuilds the row as installed; its action stays "Install" so it can be
    # installed a second time.
    _make_instance(page._game_dir(), "rel-9", "")
    page.refresh_installed()
    app.processEvents()
    done = page._row_for("rel-9")
    assert done.installed
    assert done.install.text() == i18n.tr("versions.install.short")
    assert done.install.isEnabled()


def test_the_thin_bottom_bar_can_no_longer_be_shown(page, app) -> None:
    """The bar the user asked to have removed: refused at the page layer, in no layout, no pixels.

    The install flow still calls ``progress_row.setVisible(True)`` at the start of every install and
    ``setVisible(False)`` at the end - that code is frozen - so the page has to be the thing that says
    no. This drives the same calls the flow makes and checks the widget never becomes visible, is not
    in the page's layout, and takes no space, while the numbers it carries still reach the row.
    """
    from PySide6.QtCore import QSize

    assert page.progress_row.parent() is page._list_step
    assert page._list_step.layout().indexOf(page.progress_row) == -1, "the bar is not in the layout"
    assert page.progress_row.size() == page.progress_row.minimumSize() == QSize(0, 0)
    assert not page.progress_row.isVisible()

    page._attach_inline_progress("rel-15")
    page.progress_row.setVisible(True)  # what install_version_id does
    app.processEvents()
    assert not page.progress_row.isVisible(), "the page refuses the show"
    assert page.progress_row.isHidden()

    page.progress_bar.setRange(0, 1000)
    page.progress_bar.setValue(250)
    app.processEvents()
    row = page._row_for("rel-15")
    assert row is not None and row.progress_value() == 250, "the row still follows the same numbers"
    assert not page.progress_row.isVisible()

    page.progress_row.setVisible(False)  # what _on_installed/_on_install_error do
    app.processEvents()
    assert not page.progress_row.isVisible()


def test_a_rebuild_during_a_download_reattaches_the_progress(page, app) -> None:
    page._attach_inline_progress("rel-10")
    page.progress_bar.setRange(0, 1000)
    page.progress_bar.setValue(300)
    page.search_edit.setText("rel-")
    page._search_timer.stop()
    page._refilter()
    app.processEvents()

    row = page._row_for("rel-10")
    assert row is not None
    assert row.install_running()
    assert row.progress_value() == 300


# ---------- the name landing on the new instance ----------


def test_every_install_creates_its_own_instance_with_the_typed_name(
    page, app, monkeypatch
) -> None:
    from gui.pages import versions_page

    game = page._game_dir()

    def fake_install(version_id, **kwargs):
        target = kwargs.get("target_id") or version_id
        folder = game / "versions" / target
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{target}.json").write_text(
            json.dumps({"id": target}), encoding="utf-8"
        )
        return SimpleNamespace(downloaded=1, skipped=0, failed=[])

    monkeypatch.setattr(versions_page, "install_version", fake_install)
    step = _open_step(page, app, "rel-11")
    step.name_edit.setText("我的整合包")
    app.processEvents()
    step.start_button.click()
    _pump(app, lambda: (game / "versions" / "我的整合包" / "minepick.json").is_file())

    assert page.stack.currentWidget() is page._list_step
    instances = list_instances(game)
    assert instances["我的整合包"].display_name == "我的整合包"
    # The profile JSON's id follows the folder, so the new instance launches as itself.
    profile = json.loads(
        (game / "versions" / "我的整合包" / "我的整合包.json").read_text(encoding="utf-8")
    )
    assert profile["id"] == "我的整合包"
    # The version id itself was not used as the folder: an install is a new instance, not an
    # overwrite of the canonical profile.
    assert not (game / "versions" / "rel-11").exists()

    # A second install opens on the version id (the first instance took the typed name), and it
    # creates a second, separately visible instance.
    _pump(app, lambda: page.install_button.isEnabled())
    again = _open_step(page, app, "rel-11")
    assert again.name_editable() is True
    assert again.instance_name() == "rel-11"
    again.start_button.click()
    _pump(app, lambda: (game / "versions" / "rel-11" / "minepick.json").is_file())

    instances = list_instances(game)
    assert instances["我的整合包"].display_name == "我的整合包"
    assert instances["rel-11"].display_name == "rel-11"


def test_two_installs_of_the_same_version_keep_their_own_names(page, app, monkeypatch) -> None:
    from gui.pages import versions_page

    game = page._game_dir()
    _make_instance(game, "other", "own name")
    page.refresh_installed()

    def fake_install(version_id, **kwargs):
        target = kwargs.get("target_id") or version_id
        folder = game / "versions" / target
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{target}.json").write_text(
            json.dumps({"id": target}), encoding="utf-8"
        )
        return SimpleNamespace(downloaded=1, skipped=0, failed=[])

    monkeypatch.setattr(versions_page, "install_version", fake_install)
    step = _open_step(page, app, "rel-13")
    # "own name" is taken; the version id is free and is the suggested name.
    assert step.instance_name() == "rel-13"
    step.start_button.click()
    _pump(app, lambda: (game / "versions" / "rel-13" / "minepick.json").is_file())

    instances = list_instances(game)
    assert instances["rel-13"].display_name == "rel-13"
    assert instances["other"].display_name == "own name"  # the older instance is untouched
    assert (game / "versions" / "rel-13").is_dir()


def test_a_chosen_loader_reaches_the_installer_and_names_its_new_instance(
    page, app, monkeypatch
) -> None:
    """The pick has to arrive in the installer, and the loader profile becomes the new instance."""
    from gui.pages import versions_page
    from launcher import mods as mods_package
    from launcher.mods.models import LoaderVersion

    game = page._game_dir()
    seen: dict[str, object] = {}

    def fake_install(version_id, **kwargs):
        folder = game / "versions" / version_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{version_id}.json").write_text(
            json.dumps({"id": version_id}), encoding="utf-8"
        )
        return SimpleNamespace(downloaded=1, skipped=0, failed=[])

    def fake_install_loader(
        loader_version, game_dir, *args, cache_dir=None, instance_id=None, **kwargs
    ):
        seen["loader_version"] = loader_version
        seen["instance_id"] = instance_id
        folder = game_dir / "versions" / instance_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{instance_id}.json").write_text(
            json.dumps({"id": instance_id, "inheritsFrom": "rel-5"}), encoding="utf-8"
        )
        return instance_id

    monkeypatch.setattr(versions_page, "install_version", fake_install)
    monkeypatch.setattr(
        mods_package,
        "list_loader_versions",
        lambda loader, version_id: (_ for _ in ()).throw(
            AssertionError("the step's selected build must be used, not a fresh list")
        ),
    )
    monkeypatch.setattr(mods_package, "install_loader", fake_install_loader)

    step = _open_step(page, app, "rel-5")
    step.rows["fabric"].click()
    step.name_edit.setText("my pack")
    app.processEvents()
    step.start_button.click()
    _pump(app, lambda: (game / "versions" / "my pack" / "minepick.json").is_file())

    selected = seen["loader_version"]
    assert isinstance(selected, LoaderVersion)
    assert selected.version == "1.0.0"
    assert seen["instance_id"] == "my pack"
    instances = list_instances(game)
    assert instances["my pack"].display_name == "my pack"
    # The vanilla folder the download created is the loader's inherited base, not the new instance.
    assert instances["rel-5"].display_name == ""


def test_the_selected_loader_build_is_the_one_installed(page, app, monkeypatch) -> None:
    """A loader can have several builds for one game version; the dropdown choice must be used."""
    from gui.pages import versions_install_flow, versions_page
    from launcher import mods as mods_package
    from launcher.mods.models import LoaderVersion

    builds = [
        LoaderVersion(loader="fabric", version="0.16.9", game_version="rel-6"),
        LoaderVersion(loader="fabric", version="0.15.0", game_version="rel-6"),
    ]
    monkeypatch.setattr(
        versions_install_flow,
        "probe_loader",
        lambda loader, version_id: builds if loader == "fabric" else [],
    )
    game = page._game_dir()
    seen: dict[str, object] = {}

    def fake_install(version_id, **kwargs):
        folder = game / "versions" / version_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{version_id}.json").write_text(
            json.dumps({"id": version_id}), encoding="utf-8"
        )
        return SimpleNamespace(downloaded=1, skipped=0, failed=[])

    def fake_install_loader(loader_version, game_dir, *args, instance_id=None, **kwargs):
        seen["version"] = loader_version.version
        seen["instance_id"] = instance_id
        folder = game_dir / "versions" / instance_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{instance_id}.json").write_text(
            json.dumps({"id": instance_id}), encoding="utf-8"
        )
        return instance_id

    monkeypatch.setattr(versions_page, "install_version", fake_install)
    monkeypatch.setattr(mods_package, "install_loader", fake_install_loader)

    step = _open_step(page, app, "rel-6")
    step.rows["fabric"].click()
    assert step.loader_version_combo.count() == 2
    assert step.selected_loader_version().version == "0.16.9"
    step.loader_version_combo.setCurrentIndex(1)
    app.processEvents()
    assert step.selected_loader_version().version == "0.15.0"
    # The default instance name follows the chosen build, so two builds cannot collide.
    assert step.instance_name() == "rel-6_fabric_0.15.0"

    step.start_button.click()
    _pump(app, lambda: (game / "versions" / "rel-6_fabric_0.15.0" / "minepick.json").is_file())
    assert seen == {"version": "0.15.0", "instance_id": "rel-6_fabric_0.15.0"}


# ---------- the install flow itself: unchanged, proven by AST ----------


def _dump(source: str) -> str:
    return ast.dump(ast.parse(textwrap.dedent(source)))


def test_the_install_flow_methods_are_unchanged() -> None:
    from gui.pages import versions_page

    checked = []
    for name, frozen in FROZEN_INSTALL_FLOW.items():
        method = getattr(versions_page.VersionsPage, name)
        current = inspect.getsource(method)
        assert _dump(current) == _dump(frozen), (
            f"{name} differs from the commit this UI change started from - the download reporting "
            "flow must not change"
        )
        checked.append(name)
    assert checked == list(FROZEN_INSTALL_FLOW) and len(checked) == 3


def test_the_ast_check_would_notice_a_change() -> None:
    """Guard the guard: the same comparison, on a source with one token changed, must fail."""
    frozen = FROZEN_INSTALL_FLOW["_on_install_error"]
    changed = frozen.replace("show_fatal(self, text)", "show_fatal(self, text + '!')")
    assert changed != frozen
    assert _dump(changed) != _dump(frozen)


FROZEN_INSTALL_FLOW = {
    "_on_progress": """\
def _on_progress(self, p) -> None:
    if p.total_files:
        self.progress_bar.setValue(int(p.done_files * 1000 / p.total_files))
        self._rate.set_total(p.total_bytes)
        rate, eta = self._rate.update(p.done_bytes)
        if rate > 0:
            set_app_status(
                self,
                tr(
                    "versions.msg.downloading_rate",
                    p.done_files,
                    p.total_files,
                    p.current,
                    format_rate(rate),
                    format_eta(eta),
                ),
            )
        else:
            set_app_status(
                self, tr("versions.msg.downloading", p.done_files, p.total_files, p.current)
            )
""",
    "_on_installed": """\
def _on_installed(self, payload) -> None:
    result, profile_id = payload
    self.progress_row.setVisible(False)
    self.launch_button.setEnabled(True)
    self._refresh_installed()
    self._refilter()
    if result.failed:
        message = tr("versions.msg.partial", len(result.failed))
    else:
        message = tr("versions.msg.done", result.downloaded, result.skipped)
    if profile_id:
        message += " | " + tr("versions.msg.loader_done", profile_id)
    set_app_status(self, message)
    self.versions_changed.emit()
""",
    "_on_install_error": """\
def _on_install_error(self, message: str) -> None:
    self.progress_row.setVisible(False)
    text = tr("versions.msg.install_fail", message)
    set_app_status(self, text, "error")
    show_fatal(self, text)  # fatal error dialog
""",
}
