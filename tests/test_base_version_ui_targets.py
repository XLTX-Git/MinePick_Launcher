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

"""The two surfaces that still read an instance's folder instead of its ``base_version`` pointer.

修改 re-points an instance at another installed profile; the detail page and the CLI follow that
field, and these tests are about the two places that did not: the launch page (which must build the
command from the pointed profile) and the instance card's ``版本 · 加载器`` line (which must label the
pointed profile). Both have an honest fallback: a pointer at a profile that is no longer installed
must not blank the card, and must not turn an instance that still has a readable profile into a
guaranteed missing-profile failure.
"""

from __future__ import annotations

import builtins
import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.pages.instances_page import card_version_meta
from launcher import config as config_mod
from launcher.instances import (
    INSTANCE_META_FILENAME,
    Instance,
    get_instance,
)

POINTED = "1.20.2"
POINTER_INSTANCE = "红石包"
BROKEN_POINTER_INSTANCE = "断链包"
MISSING_TARGET = "不存在"


@pytest.fixture(scope="module")
def app():
    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """The GUI suite's offline stubs: no network and no Java probe outliving the window."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _write_profile(game: Path, version_id: str, *, inherits: str | None = None) -> Path:
    """One installed profile; ``mainClass`` names it so a resolved chain would be checkable."""
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


def _write_instance(
    game: Path, instance_id: str, *, inherits: str, base_version: str = ""
) -> Path:
    """An instance folder with its own profile and a sidecar that may hold a ``base_version``."""
    folder = _write_profile(game, instance_id, inherits=inherits)
    meta = Instance(
        id=instance_id,
        display_name=instance_id,
        created_at=1.0,
        base_version=base_version,
    )
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    (folder / "mods" / "sodium.jar").write_bytes(b"PK\x03\x04sodium")
    return folder


def _game(ws_tmp: Path) -> Path:
    """Two plain profiles and the two instances under test (one good pointer, one dangling)."""
    game = ws_tmp / "mc"
    _write_profile(game, "1.20.1")
    _write_profile(game, POINTED)
    _write_instance(game, POINTER_INSTANCE, inherits="1.20.1", base_version=POINTED)
    _write_instance(
        game, BROKEN_POINTER_INSTANCE, inherits="1.20.1", base_version=MISSING_TARGET
    )
    return game


@pytest.fixture()
def window(app, monkeypatch, ws_tmp):
    """A real window over the fixture game folder, with offline mode unlocked."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = _game(ws_tmp)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.selected_instance = ""
    cfg.offline_unlocked = True
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


def _settle(app, window) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _pump(app, predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _show_instances(app, window):
    from gui.main_window import NAV_KEYS

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, window)
    return window.pages["instances"]


def _stub_launch(app, monkeypatch, window, instance_id: str) -> dict:
    """Point the launch page at one instance and capture what ``prepare_launch`` receives.

    The launch page reaches ``prepare_launch`` only through the accessibility gate and the worker,
    so the gate's state is pre-answered (no dialog) and the worker is stopped with a Java offer the
    test declines - the seam ``tests/test_e2e.py`` uses for the same reason.
    """
    from launcher.accessibility import AccessibilityState, TriggerSource, save_state

    assert save_state(instance_id, AccessibilityState.ANSWERED_OFF)

    import gui.pages.launch_page as launch_page_mod
    from launcher.launch import JavaMissingError

    seen: dict = {}

    def fake_prepare(version_id, *args, **kwargs):
        seen["version_id"] = version_id
        raise JavaMissingError(8)

    monkeypatch.setattr(launch_page_mod, "prepare_launch", fake_prepare)
    # The Java offer is answered "No" so the (background) launch ends quietly instead of downloading.
    monkeypatch.setattr("gui.inwindow_dialog.question", lambda *a, **k: False)

    page = window.pages["launch"]
    page.set_version_id(instance_id)
    page.launch(TriggerSource.KEYBOARD)
    assert _pump(app, lambda: "version_id" in seen), "prepare_launch was never called"
    return seen


# ---------------------------------------------------------------------------
# 1. The card and the launch page both follow base_version
# ---------------------------------------------------------------------------


def test_the_card_and_the_launch_page_follow_the_base_version(app, monkeypatch, window):
    built, _game_dir = window
    page = _show_instances(app, built)
    card = page.card_for(POINTER_INSTANCE)
    assert card is not None, [item.instance_id for item in page.cards()]

    # The card labels the *pointed* profile (1.20.2), not the folder's own 1.20.1 chain.
    assert card.meta_label.text() == POINTED + " · " + i18n.tr("instances.card.vanilla")

    # The launch page hands the pointed profile to prepare_launch, not the folder id.
    seen = _stub_launch(app, monkeypatch, built, POINTER_INSTANCE)
    assert seen["version_id"] == POINTED
    assert seen["version_id"] != POINTER_INSTANCE


# ---------------------------------------------------------------------------
# 2. A dangling pointer falls back to the folder's own profile
# ---------------------------------------------------------------------------


def test_a_missing_base_version_falls_back_to_the_folder_and_says_so(app, monkeypatch, window):
    built, game = window
    page = _show_instances(app, built)
    card = page.card_for(BROKEN_POINTER_INSTANCE)
    assert card is not None, [item.instance_id for item in page.cards()]

    # Not blank: the card says what is wrong, keeps its action and stays on screen.
    assert card.meta_label.text() == i18n.tr("instances.card.base_version_missing")
    assert card.meta_label.text().strip() == "指向的版本缺失"
    assert card.use_button.isEnabled()

    # The pair still comes from the folder's own profile (its 1.20.1 chain), not from the pointer.
    instance = get_instance(BROKEN_POINTER_INSTANCE, game)
    assert instance is not None
    version, loader, text = card_version_meta(instance, game)
    assert (version, loader) == ("1.20.1", i18n.tr("instances.card.vanilla"))
    assert text == i18n.tr("instances.card.base_version_missing")

    # The launch page falls back to the folder id, so a working instance still launches.
    seen = _stub_launch(app, monkeypatch, built, BROKEN_POINTER_INSTANCE)
    assert seen["version_id"] == BROKEN_POINTER_INSTANCE


# ---------------------------------------------------------------------------
# 3. Regression: the detail page and the CLI keep resolving the same profile
# ---------------------------------------------------------------------------


def test_the_detail_page_still_shows_the_pointed_profile(app, window):
    built, _game_dir = window
    page = _show_instances(app, built)
    assert page.open_instance(POINTER_INSTANCE)
    _settle(app, built)
    detail = page.detail

    from gui.pages.instance_detail import TAB_MODIFY

    detail.tabs[TAB_MODIFY].click()
    app.processEvents()
    assert detail.modify_rows["minecraft"].value_text() == POINTED
    assert detail.modify_summary_subtitle.text() == (
        POINTED + " · " + i18n.tr("instances.detail.modify.vanilla")
    )


def test_the_cli_still_launches_the_pointed_profile(monkeypatch, window):
    _built, _game_dir = window
    from launcher import cli
    from launcher.launch import JavaMissingError

    calls: list[str] = []

    def fake_prepare(version_id, *args, **kwargs):
        calls.append(version_id)
        raise JavaMissingError(8)

    # cmd_instance imports prepare_launch from the package at call time, so the package attribute is
    # the seam. Declining the Java download keeps the CLI from starting a process.
    monkeypatch.setattr("launcher.launch.prepare_launch", fake_prepare)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "n")

    code = cli.main(["instance", "launch", POINTER_INSTANCE])
    assert code == 1  # the Java offer was declined, so nothing ran
    assert calls == [POINTED]
