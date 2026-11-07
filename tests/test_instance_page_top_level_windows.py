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

"""The instances page never puts a widget of its own on screen as a separate window.

The report: clicking 实例 in the rail flashed two empty windows with a native title bar in the
top-left corner of the screen, then they vanished. Both came from this page turning one of its own
widgets into a top-level window for one event-loop turn:

* a widget built with no parent *is* a top-level window - ``QWidget()``, ``QFrame()``,
  ``QLabel("text")`` (the first argument is the text, not a parent) - and the moment it is shown
  before it has been adopted by a layout it becomes a real window with a native frame;
* ``QWidget.setParent(None)`` on a widget that is currently visible is the same thing in reverse:
  the section the card grid had just dropped became a window at the old geometry until the deferred
  delete collected it.

These tests watch the page at the level the user sees it: what is a window while the page is built,
switched to and refreshed. They are deliberately about *no* new window rather than about a
particular widget class, so a future widget gets the protection without being listed here.
"""

from __future__ import annotations

import gc
import json

import pytest
from PySide6.QtCore import QEvent, QObject, QThreadPool
from PySide6.QtWidgets import QApplication, QWidget

from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No network and no Java probe outliving the window (the GUI suite's three stubs)."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _make_instance(game, version_id: str, *, display_name: str, version_type: str = "release") -> None:
    """One instance = one profile folder with its sidecar metadata (and a declared type)."""
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(
        json.dumps({"id": version_id, "type": version_type, "mainClass": "x"}),
        encoding="utf-8",
    )
    meta = Instance(id=version_id, display_name=display_name, note="", created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )


def _settle(app, window=None) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(8):
        app.processEvents()


def _data_dir(monkeypatch, ws_tmp, name: str):
    """Two game folders, and two card groups (正式版 + 带加载器) in the first one.

    Two groups on purpose: the report was two flashing windows, and one flash per group is what the
    page produced when it handed its sections to the window system before adopting them.
    """
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / name))
    main = ws_tmp / f"{name}-主资料夹"
    other = ws_tmp / f"{name}-副资料夹"
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(main))
    _make_instance(main, "1.21.11", display_name="Vanilla 1.21.11")
    _make_instance(main, "1.20.1-forge-47.4.22", display_name="Forge Pack")
    other.mkdir(parents=True, exist_ok=True)

    cfg, cfg_path = config_mod.load()
    from launcher.config import GameFolder

    cfg.game_folders = [
        GameFolder(path=main, name="主资料夹"),
        GameFolder(path=other, name="副资料夹"),
    ]
    cfg.game_dir = main
    cfg.ui_language = "zh_cn"
    config_mod.save(cfg, cfg_path)

    from gui import i18n

    i18n.set_language("zh_cn")
    return main


@pytest.fixture
def window(app, monkeypatch, ws_tmp):
    _data_dir(monkeypatch, ws_tmp, "instances_no_windows")
    from gui.main_window import MainWindow

    built = MainWindow()
    built.resize(1180, 760)
    built.show()
    _settle(app, built)
    yield built
    built.close()
    built.deleteLater()
    app.processEvents()
    gc.collect()


# ---------------------------------------------------------------------------
# what the user sees: no window appears while the page is built and shown
# ---------------------------------------------------------------------------


def _top_level_widgets() -> list:
    """Top-level widgets in the Qt sense, without the popups that legitimately have a parent.

    A ``QComboBox``'s popup container is a window with the combo as its parent: it is Qt's own, it is
    not a stray window of this page, and it is never visible until the user opens the dropdown.
    """
    return [widget for widget in QApplication.topLevelWidgets() if widget.parentWidget() is None]


def _describe(widget) -> str:
    try:
        return (
            f"{type(widget).__name__}(objectName={widget.objectName()!r}, "
            f"visible={widget.isVisible()}, geometry={widget.geometry().getRect()})"
        )
    except RuntimeError:  # already deleted between the snapshot and the message
        return "<deleted>"


def _new_top_level_widgets(before: set[int]) -> list:
    return [widget for widget in _top_level_widgets() if id(widget) not in before]


def test_constructing_the_instances_page_adds_no_top_level_window(app, window):
    """The page is a plain child widget: building it may not create a window of its own."""
    before = {id(widget) for widget in _top_level_widgets()}
    page = window.pages["instances"]  # first visit: this is where the page is constructed
    _settle(app, window)
    assert page.parentWidget() is not None
    assert _new_top_level_widgets(before) == [], [
        _describe(widget) for widget in _new_top_level_widgets(before)
    ]


def test_switching_to_the_instances_page_adds_no_top_level_window(app, window):
    """The reported action, measured: click 实例, and the window list must not grow.

    The first visit is the interesting one - it constructs the page, shows it and then rebuilds the
    card pane on top of it, which is the whole sequence the report described.
    """
    from gui.main_window import NAV_KEYS

    before = {id(widget) for widget in _top_level_widgets()}
    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, window)
    page = window.pages["instances"]
    assert page.cards(), "the fixture's instances did not reach the grid"
    new = _new_top_level_widgets(before)
    assert new == [], [
        f"{_describe(widget)} appeared when 实例 was opened" for widget in new
    ]


def test_refreshing_the_page_while_it_is_on_screen_adds_no_top_level_window(app, window):
    """Every refresh rebuilds the cards: the old sections must not become windows on the way out."""
    from gui.main_window import NAV_KEYS

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, window)
    page = window.pages["instances"]
    assert page.isVisible()
    before = {id(widget) for widget in _top_level_widgets()}
    page.refresh()
    _settle(app, window)
    new = _new_top_level_widgets(before)
    assert new == [], [
        f"{_describe(widget)} appeared when the card pane was rebuilt" for widget in new
    ]


# ---------------------------------------------------------------------------
# the mechanism: a widget may not be shown, or detached, while it has no parent
# ---------------------------------------------------------------------------


class _WindowCreationWatch(QObject):
    """Records the two moments a QWidget becomes a top-level window on screen.

    * ``QWidget.show()`` / ``setVisible(True)`` called on a widget with no parent: it is shown *as*
      a window, which is the flash;
    * ``QWidget.setParent(None)`` called on a widget that is currently visible: Qt turns it into a
      window, which is the same flash one step later.

    Python-level calls are what this page makes, so overriding those three methods is enough; the
    event filter is the backstop for a show that comes from Qt's own layout code.
    """

    def __init__(self) -> None:
        super().__init__()
        self.shown_without_parent: list[str] = []
        self.detached_while_visible: list[str] = []
        self._original_show = QWidget.show
        self._original_set_visible = QWidget.setVisible
        self._original_set_parent = QWidget.setParent
        watch = self

        def show(widget):
            watch._note_show("show()", widget)
            return watch._original_show(widget)

        def set_visible(widget, visible):
            if visible:
                watch._note_show("setVisible(True)", widget)
            return watch._original_set_visible(widget, visible)

        def set_parent(widget, *args):
            target = args[0] if args else None
            try:
                visible = widget.isVisible()
            except RuntimeError:
                visible = False
            if target is None and visible:
                watch.detached_while_visible.append(_describe(widget))
            return watch._original_set_parent(widget, *args)

        QWidget.show = show
        QWidget.setVisible = set_visible
        QWidget.setParent = set_parent

    def _note_show(self, how: str, widget) -> None:
        try:
            if widget.parentWidget() is None:
                self.shown_without_parent.append(f"{how}: {_describe(widget)}")
        except RuntimeError:  # the C++ object is gone; nothing was shown
            pass

    def eventFilter(self, obj, event) -> bool:
        try:
            orphan_window_shown = (
                isinstance(obj, QWidget)
                and event.type() is QEvent.Type.Show
                and obj.parentWidget() is None
                and obj.isWindow()
            )
        except Exception:  # noqa: BLE001 - a filter may never disturb Qt's dispatch
            return False
        if orphan_window_shown:
            self.shown_without_parent.append(f"QEvent.Show: {_describe(obj)}")
        return False

    def restore(self) -> None:
        QWidget.show = self._original_show
        QWidget.setVisible = self._original_set_visible
        QWidget.setParent = self._original_set_parent


def test_no_widget_of_the_page_is_ever_shown_or_detached_without_a_parent(app, window):
    """Build the page, switch to it, refresh it, and watch every way a widget can become a window."""
    from gui.main_window import NAV_KEYS

    watch = _WindowCreationWatch()
    app.installEventFilter(watch)
    try:
        page = window.pages["instances"]  # constructs the page
        _settle(app, window)
        window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        _settle(app, window)
        page.refresh()
        _settle(app, window)
    finally:
        app.removeEventFilter(watch)
        watch.restore()

    assert watch.shown_without_parent == [], watch.shown_without_parent
    assert watch.detached_while_visible == [], watch.detached_while_visible
