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

"""Instance switching: 使用, the current marker, scroll-to-current and config.selected_instance.

The launch page is the one place that decides which profile the launcher will run, so the chained
route is: a card's 使用 (or a double-click) writes ``config.selected_instance``, the launch page
shows that profile, and the instances page marks it with the detail sidebar's selection language.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from gui import i18n
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Same offline stubs the GUI suite uses: no network and no Java probe outliving the window."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _make_instance(game: Path, version_id: str, display_name: str = "") -> Path:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(
        json.dumps({"id": version_id, "mainClass": "net.minecraft.client.main.Main"}),
        encoding="utf-8",
    )
    meta = Instance(id=version_id, display_name=display_name or version_id, created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    return folder


def _settle(app) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _window(
    app,
    monkeypatch,
    ws_tmp,
    instance_ids: tuple[str, ...],
    *,
    selected: str = "",
    width: int = 1180,
    height: int = 760,
):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "mc"
    for version_id in instance_ids:
        _make_instance(game, version_id)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.selected_instance = selected
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    from gui.main_window import MainWindow

    window = MainWindow()
    window.resize(width, height)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    _settle(app)
    return window, game


def _close(app, window) -> None:
    window.close()
    window.deleteLater()
    for _ in range(4):
        app.processEvents()
    i18n.set_language("zh_cn")


def _cards(page) -> list:
    return page.cards()


def _card_by_id(page, instance_id: str):
    card = page.card_for(instance_id)
    if card is None:
        raise AssertionError(f"card for {instance_id!r} not found")
    return card


def _show_instances(window) -> None:
    from gui.main_window import NAV_KEYS

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))


# ---------------------------------------------------------------------------
# 1. 使用 writes the current instance and goes back to the launch page
# ---------------------------------------------------------------------------


def test_use_stores_the_current_instance_and_returns_to_the_launch_page(
    app, monkeypatch, ws_tmp
):
    window = None
    try:
        window, _game = _window(
            app, monkeypatch, ws_tmp, ("1.20.1", "1.20.2"), selected="1.20.1"
        )
        launch = window.pages["launch"]
        _show_instances(window)
        _settle(app)
        page = window.pages["instances"]

        _card_by_id(page, "1.20.2").use_button.click()
        _settle(app)

        stored, _ = config_mod.load()
        assert stored.selected_instance == "1.20.2"  # the folder id, not the display name
        assert window.stack.currentWidget() is launch  # back on the launch page
        assert launch._version_id == "1.20.2"
        assert launch.version_label.text() == "1.20.2"
    finally:
        if window is not None:
            _close(app, window)


def test_double_clicking_a_card_uses_it(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(
            app, monkeypatch, ws_tmp, ("1.20.1", "1.20.2"), selected="1.20.1"
        )
        launch = window.pages["launch"]
        _show_instances(window)
        _settle(app)
        page = window.pages["instances"]

        QTest.mouseDClick(_card_by_id(page, "1.20.2"), Qt.MouseButton.LeftButton)
        _settle(app)

        stored, _ = config_mod.load()
        assert stored.selected_instance == "1.20.2"
        assert window.stack.currentWidget() is launch
        assert launch._version_id == "1.20.2"
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 2. The current marker: exactly one card, in the sidebar's selection language
# ---------------------------------------------------------------------------


def test_the_current_marker_is_drawn_on_exactly_one_card(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(
            app,
            monkeypatch,
            ws_tmp,
            ("1.20.1", "1.20.2", "1.20.3"),
            selected="1.20.2",
        )
        _show_instances(window)
        _settle(app)
        page = window.pages["instances"]

        marked = [card for card in _cards(page) if card.property("current")]
        assert len(marked) == 1, [card.instance_id for card in marked]
        assert marked[0].instance_id == "1.20.2"
        # The card announces the state too, not just the accent bar.
        assert i18n.tr("instances.card.current") in marked[0].accessibleDescription()

        # The marker is the detail sidebar's: a 3px accent bar on the left, plus the wash.
        for sheet in ("style_light.qss", "style.qss"):
            text = (ROOT / "gui" / "resources" / sheet).read_text(encoding="utf-8")
            assert 'QFrame#instanceCard[current="true"]' in text
            assert "border-left: 3px solid __ACCENT__;" in text
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 3. 实例选择 scrolls the current card into view
# ---------------------------------------------------------------------------


def test_select_instance_scrolls_the_current_card_into_view(app, monkeypatch, ws_tmp):
    window = None
    try:
        ids = tuple(f"1.20.{index}" for index in range(1, 13))
        # Lexicographic last of these ids: it sits on the grid's last row, so the view must scroll.
        window, _game = _window(app, monkeypatch, ws_tmp, ids, selected="1.20.9")
        launch = window.pages["launch"]
        assert launch._version_id == "1.20.9"

        launch.select_instance_button.click()
        _settle(app)
        page = window.pages["instances"]

        assert window.stack.currentWidget() is page
        assert page.current_view() is page.view_stack.widget(0)  # the card grid
        assert page._current_name() == "1.20.9"
        card = page.card_for("1.20.9")
        assert card is not None
        scroll = page.content_scroll.verticalScrollBar()
        assert scroll.value() > 0, "the grid did not scroll at all"
        viewport = page.content_scroll.viewport()
        top = card.mapTo(viewport, QPoint(0, 0)).y()
        assert top >= 0, (top, viewport.rect())
        assert top + card.height() <= viewport.height(), (top, card.height(), viewport.rect())
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 4. A stored id that no longer exists falls back and is cleared
# ---------------------------------------------------------------------------


def test_a_missing_selected_instance_falls_back_and_is_cleared(app, monkeypatch, ws_tmp):
    window = None
    try:
        window, _game = _window(
            app, monkeypatch, ws_tmp, ("1.20.1", "1.20.2"), selected="deleted-instance"
        )
        launch = window.pages["launch"]

        assert launch._version_id == "1.20.1"  # the old first-installed rule
        stored, _ = config_mod.load()
        assert stored.selected_instance == ""  # the dangling id was forgotten

        _show_instances(window)
        _settle(app)
        assert not any(card.property("current") for card in _cards(window.pages["instances"]))
    finally:
        if window is not None:
            _close(app, window)


# ---------------------------------------------------------------------------
# 5. The config field: default, round trip, and an older file without the key
# ---------------------------------------------------------------------------


def test_selected_instance_defaults_to_empty_and_old_configs_load(ws_tmp):
    from launcher.config import LauncherConfig

    assert LauncherConfig().selected_instance == ""

    old_path = ws_tmp / "old" / "config.json"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(json.dumps({"memory_gb": 4.0}), encoding="utf-8")
    cfg, _ = config_mod.load(old_path)
    assert cfg.selected_instance == ""  # absent key keeps the previous behaviour

    cfg.selected_instance = "1.20.1"
    config_mod.save(cfg, old_path)
    again, _ = config_mod.load(old_path)
    assert again.selected_instance == "1.20.1"
