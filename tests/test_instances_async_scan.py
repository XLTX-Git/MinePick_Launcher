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

"""The instances page reads its folder on a worker, with a placeholder and a scan generation.

Building the page and entering it used to call ``list_instances`` (and per-row version/mod/save
counts) synchronously on the GUI thread. Now the scan runs on the pool, the pane shows the
loading hint until the answer arrives, and a superseded scan is dropped instead of overwriting a
newer one (the fast page-switch case).
"""

from __future__ import annotations

import json
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication

from gui import i18n
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn():
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _wait_for_workers(app, timeout_ms: int = 5000) -> None:
    QThreadPool.globalInstance().waitForDone(timeout_ms)
    for _ in range(8):
        app.processEvents()


def _wait_until(app, predicate, timeout_s: float = 3.0) -> bool:
    """Pump the GUI loop until the worker has visibly started/finished (events, not just the pool)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.01)
    return bool(predicate())


def _make_instance(game, version_id: str, *, version_type: str = "release") -> None:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(
        json.dumps({"id": version_id, "type": version_type, "mainClass": "x"}), encoding="utf-8"
    )
    meta = Instance(id=version_id, display_name=version_id, note="", created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )


def _game_with_two_instances(ws_tmp):
    game = ws_tmp / "mc"
    _make_instance(game, "1.20.1")
    _make_instance(game, "26w02a", version_type="snapshot")
    return game


def _configure(monkeypatch, ws_tmp, game):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.ui_language = "zh_cn"
    config_mod.save(cfg, cfg_path)


def test_construction_starts_a_background_scan_with_a_visible_placeholder(
    app, monkeypatch, ws_tmp
):
    game = _game_with_two_instances(ws_tmp)
    _configure(monkeypatch, ws_tmp, game)

    from gui.pages import instances_page

    release = threading.Event()
    original = instances_page.InstancesPage._scan_task
    calls: list[int] = []

    def slow_scan(self, generation, *args, **kwargs):
        calls.append(generation)
        release.wait(5.0)
        return original(self, generation, *args, **kwargs)

    monkeypatch.setattr(instances_page.InstancesPage, "_scan_task", slow_scan)

    started = time.monotonic()
    page = instances_page.InstancesPage()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, (
        f"InstancesPage() blocked for {elapsed:.2f}s; the directory scan must run on a worker"
    )
    assert _wait_until(app, lambda: bool(calls)), "the constructor never started the background scan"

    # The in-between state is a real hint, not an empty pane and not made-up rows.
    assert page.list_stack.currentWidget() is page.loading_state
    assert page.loading_state.isVisibleTo(page)
    assert page.loading_state.text() == i18n.tr("instances.loading")
    assert page.cards() == [], "a placeholder must not present fake instance rows"

    release.set()
    _wait_for_workers(app)

    assert page.list_stack.currentWidget() is page.content_scroll
    assert set(page.card_ids()) == {"1.20.1", "26w02a"}
    assert page.loading_state.text() == i18n.tr("instances.loading")


def test_a_superseded_scan_cannot_overwrite_the_newer_one(app, monkeypatch, ws_tmp):
    game = _game_with_two_instances(ws_tmp)
    _configure(monkeypatch, ws_tmp, game)

    from gui.pages import instances_page

    gate_first = threading.Event()
    original = instances_page.InstancesPage._scan_task
    generations: list[int] = []

    def controlled_scan(self, generation, *args, **kwargs):
        generations.append(generation)
        if len(generations) == 1:
            gate_first.wait(5.0)  # the first scan is still running when the user comes back
        return original(self, generation, *args, **kwargs)

    monkeypatch.setattr(instances_page.InstancesPage, "_scan_task", controlled_scan)

    page = instances_page.InstancesPage()  # generation 1, blocked
    assert _wait_until(app, lambda: generations == [1]), generations

    applied: list[int] = []
    original_apply = page._apply_scan

    def spy(scan):
        applied.append(scan.generation)
        original_apply(scan)

    monkeypatch.setattr(page, "_apply_scan", spy)

    page.refresh_in_background()  # generation 2: the page was re-entered
    gate_first.set()  # generation 1 finishes late (before or after generation 2, both are valid)
    _wait_for_workers(app)
    assert applied == [2], (
        f"the stale scan overwrote the newer one (applied generations: {applied})"
    )
    assert set(page.card_ids()) == {"1.20.1", "26w02a"}


def test_every_page_entry_starts_a_new_scan(app, monkeypatch, ws_tmp):
    game = _game_with_two_instances(ws_tmp)
    _configure(monkeypatch, ws_tmp, game)

    from gui.pages import instances_page

    original = instances_page.InstancesPage._scan_task
    generations: list[int] = []

    def counting_scan(self, generation, *args, **kwargs):
        generations.append(generation)
        return original(self, generation, *args, **kwargs)

    monkeypatch.setattr(instances_page.InstancesPage, "_scan_task", counting_scan)

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    try:
        window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        _wait_for_workers(app)
        assert len(generations) == 1
        assert set(window.pages["instances"].card_ids()) == {"1.20.1", "26w02a"}

        window.sidebar.setCurrentRow(NAV_KEYS.index("settings"))
        app.processEvents()
        window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        _wait_for_workers(app)
        assert len(generations) == 2, "returning to the page did not start a fresh scan"
        assert set(window.pages["instances"].card_ids()) == {"1.20.1", "26w02a"}
        assert window.pages["instances"].list_stack.currentWidget() is (
            window.pages["instances"].content_scroll
        )
    finally:
        window.close()
        app.processEvents()
