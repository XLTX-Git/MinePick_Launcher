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

"""The two task views on the real window: no icon without tasks, three faces, the panel, and its rows.

The window is the real ``MainWindow`` built offscreen, so the button and the panel live exactly where the
product puts them (a child of the content area, positioned by hand) and the assertions can be about the
widget tree rather than about a mock: "there is no icon" is ``findChild`` returning nothing, and "the
icon did not change the window's minimum width" is the window's own ``minimumSizeHint``.

The ring is judged on pixels, not on a number the test asks the widget for: three points on the circle are
sampled, and the arc has to cover exactly the ones the percentage says it covers. A bug that painted a
full ring for every value would pass a ``percent() == 25`` assertion and fail this one.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt

from gui import i18n, theme
from gui.pages.settings_page import ACCENT_PRESETS
from gui.task_ui import (
    ACTION_HEIGHT,
    EDGE,
    ICON_SIZE,
    PANEL_WIDTH,
    ROW_HEIGHT,
    TaskButton,
    TaskPanel,
)
from launcher.meta.manifest import ManifestVersion, VersionManifest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(scope="module")
def app():
    """The suite's QApplication, made the way the launcher makes it (theme + focus tracker)."""
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


def _pump(app, predicate=None, timeout: float = 5.0) -> bool:
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


def _settle_deletes(app) -> None:
    """Deliver the ``deleteLater()`` of a widget the views dropped (that is what "gone" means)."""
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _manifest() -> VersionManifest:
    return VersionManifest(
        latest={"release": "1.21.11", "snapshot": "26w02a"},
        versions=[
            ManifestVersion(
                id=vid,
                type=vtype,  # type: ignore[arg-type]
                url="https://example.invalid/version.json",
                time=f"{date}T12:00:00+00:00",
                release_time=f"{date}T12:00:00+00:00",
            )
            for vid, vtype, date in (
                ("1.21.11", "release", "2025-12-09"),
                ("1.20.1", "release", "2024-06-13"),
                ("26w02a", "snapshot", "2026-01-13"),
            )
        ],
    )


@pytest.fixture()
def window(app, monkeypatch, ws_tmp):
    """A real window on the versions page, with the manifest stubbed and no network anywhere."""
    game = ws_tmp / "game"
    game.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))
    from launcher import config as config_mod

    cfg, cfg_path = config_mod.load()
    cfg.ui_language = "zh_cn"
    cfg.game_language = "zh_cn"
    cfg.language_initialized = True
    cfg.wizard_done = True
    cfg.game_dir = str(game)
    cfg.theme = "light"
    cfg.accent_color = ""
    cfg.ui_font = ""
    cfg.ui_radius = "default"
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    from gui.pages import versions_page

    monkeypatch.setattr(versions_page, "fetch_manifest", lambda cache_path=None: _manifest())

    from gui.main_window import NAV_KEYS, MainWindow

    built = MainWindow()
    built.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    built.resize(1000, 610)
    built.show()
    app.processEvents()
    built.sidebar.setCurrentRow(NAV_KEYS.index("versions"))
    page = built.pages["versions"]
    _pump(app, lambda: page.list.count() > 0)
    yield built
    built.close()


def _circle_offset(window, button: TaskButton) -> tuple[int, int]:
    """Where the visible circle sits in the content area (``EDGE`` is measured against these)."""
    circle = button.circle_rect()
    top_left = button.mapTo(window.content, circle.topLeft().toPoint())
    return top_left.x(), top_left.y()


# ---------- 1. no tasks: no icon at all ----------


def test_without_tasks_the_icon_does_not_exist(window, app) -> None:
    """Not hidden, not transparent: absent. No widget, no focus stop, nothing to walk or to grab."""
    assert window.tasks.tasks() == ()
    assert window.findChildren(TaskButton) == [], "an icon exists with no task to report"
    assert window.findChild(TaskPanel) is None, "the panel is built on demand, not up front"

    # The walkthrough's own rule: a focus stop is a widget inside a page's focus chain. There is no
    # task button in any page's chain, because there is no task button at all.
    import keyboard_walkthrough as walkthrough

    for page in window.pages.values():
        stops = walkthrough._page_focus_stops(page)
        assert not any(isinstance(stop, TaskButton) for stop in stops)


# ---------- 2. the icon: where it is and what it says ----------


def test_the_icon_floats_sixteen_pixels_from_the_corner_with_its_own_geometry(window, app) -> None:
    minimum_before = window.minimumSizeHint().width()

    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    app.processEvents()
    button = window.findChild(TaskButton)
    assert button is not None and button.isVisible()

    x, y = _circle_offset(window, button)
    assert window.content.width() - (x + ICON_SIZE) == EDGE
    assert window.content.height() - (y + ICON_SIZE) == EDGE
    assert ICON_SIZE == 48

    # Floating means "in no layout": the button adds nothing to the window's minimum width.
    assert window.minimumSizeHint().width() == minimum_before
    task.cancel()
    _settle_deletes(app)


def test_the_icon_has_a_percentage_ring_while_a_task_runs(window, app) -> None:
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    task.set_progress(3 * 1024 * 1024, 12 * 1024 * 1024, 1.2 * 1024 * 1024)
    app.processEvents()
    button = window.findChild(TaskButton)
    assert button is not None
    assert button.state() == "running"
    assert button.percent() == 25
    assert button.accessibleName() == i18n.tr("tasks.icon.progress", 25)


def _ring_pixels(button: TaskButton) -> tuple[QPoint, QPoint, QPoint]:
    """Three points on the ring: 45 deg, 135 deg and 225 deg clockwise from twelve o'clock."""
    centre = button.circle_rect().center()
    radius = (ICON_SIZE - 9.0) / 2.0  # the ring is drawn 4.5px inside the circle, 3px wide
    points = []
    for degrees in (45.0, 135.0, 225.0):
        radians = math.radians(degrees)
        points.append(
            QPoint(
                round(centre.x() + radius * math.sin(radians)),
                round(centre.y() - radius * math.cos(radians)),
            )
        )
    return tuple(points)  # type: ignore[return-value]


def _is_filled(button: TaskButton, point: QPoint) -> bool:
    """Is the ring painted at ``point``? The filled stroke is the foreground, the track is not."""
    image = button.grab().toImage()
    ratio = image.devicePixelRatio()
    colour = image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))
    return min(colour.red(), colour.green(), colour.blue()) > 230


def test_the_ring_arc_covers_the_percentage_and_nothing_more(window, app) -> None:
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    app.processEvents()
    button = window.findChild(TaskButton)
    assert button is not None
    right, left, bottom = _ring_pixels(button)

    task.set_progress(25, 100, 0)
    app.processEvents()
    assert _is_filled(button, right), "a quarter of the ring starts at twelve o'clock and ends at three"
    assert not _is_filled(button, left), "the arc must stop at the quarter mark"
    assert not _is_filled(button, bottom)

    task.set_progress(75, 100, 0)
    app.processEvents()
    assert _is_filled(button, right)
    assert _is_filled(button, left), "three quarters of the ring reach past nine o'clock"
    assert _is_filled(button, bottom)

    task.set_progress(100, 100, 0)
    app.processEvents()
    assert all(_is_filled(button, point) for point in (right, left, bottom))
    task.cancel()
    _settle_deletes(app)


def test_the_icon_says_done_after_a_success_and_leaves_after_its_hold(window, app) -> None:
    window.tasks.completed_hold_ms = 120
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    app.processEvents()
    task.set_progress(100, 100, 0)
    task.finish()
    app.processEvents()
    button = window.findChild(TaskButton)
    assert button is not None and button.state() == "done"
    assert button.accessibleName() == i18n.tr("tasks.icon.done")

    assert _pump(app, lambda: window.task_overlay.button() is None, timeout=3.0), (
        "the icon outlived the finished task it was reporting"
    )
    _settle_deletes(app)
    assert window.findChild(TaskButton) is None


def test_the_icon_says_failed_and_does_not_go_away_by_itself(window, app) -> None:
    window.tasks.completed_hold_ms = 60
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    app.processEvents()
    task.fail("boom")
    app.processEvents()
    button = window.findChild(TaskButton)
    assert button is not None and button.state() == "failed"
    assert button.accessibleName() == i18n.tr("tasks.icon.failed")

    assert _pump(app, lambda: False, timeout=0.5) is False  # let any (wrong) timer fire
    assert window.findChild(TaskButton) is not None, "a failure must stay on screen"
    assert window.tasks.tasks() == (task,)

    task.dismiss()
    assert _pump(app, lambda: window.task_overlay.button() is None, timeout=2.0)
    _settle_deletes(app)
    assert window.findChild(TaskButton) is None


def test_the_three_faces_are_three_different_pictures(window, app) -> None:
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    app.processEvents()

    def face() -> bytes:
        button = window.findChild(TaskButton)
        assert button is not None
        return bytes(button.grab().toImage().constBits())

    task.set_progress(50, 100, 0)
    app.processEvents()
    running = face()
    task.fail("boom")
    app.processEvents()
    failed = face()
    assert failed != running, "the alert face painted the same pixels as the ring"

    task.dismiss()
    app.processEvents()
    done_task = window.tasks.add_task("tasks.title.install", "1.20.1")
    app.processEvents()
    done_task.finish()
    app.processEvents()
    done = face()
    assert done not in (running, failed), "the check face painted the same pixels as another state"
    window.tasks.remove(done_task)
    _settle_deletes(app)


# ---------- 3. the panel ----------


def test_the_panel_lists_the_tasks_and_their_numbers(window, app) -> None:
    running = window.tasks.add_task("tasks.title.install", "1.21.11")
    running.set_progress(3 * 1024 * 1024, 12 * 1024 * 1024, 1.2 * 1024 * 1024)
    failed = window.tasks.add_task("tasks.title.install", "1.20.1")
    failed.set_progress(1, 2, 0)
    failed.fail(i18n.tr("tasks.error.partial", 2))
    app.processEvents()

    window.task_overlay.open_panel()
    app.processEvents()
    panel = window.task_overlay.panel()
    assert panel is not None and panel.isVisible() and panel.is_open()
    assert panel.width() == PANEL_WIDTH
    rows = panel.rows()
    assert [row.task for row in rows] == [running, failed]
    assert rows[0].accessibleName() == (
        f"{i18n.tr('tasks.title.install', '1.21.11')} · {i18n.tr('tasks.state.running')} · 25%"
    )
    assert running.percent_text() == "25%"
    assert running.size_text() == "3.0 MB / 12.0 MB"
    assert running.speed_text() == "1.2 MB/s"
    assert rows[0].action.text() == i18n.tr("tasks.cancel")
    assert rows[1].action.text() == i18n.tr("tasks.dismiss")

    # The panel sits above the button and inside the content area: it can never be a window.
    assert panel.parent() is window.content
    assert panel.geometry().right() < window.content.width()
    assert panel.geometry().bottom() < window.content.height()


def test_the_panel_says_there_are_no_tasks_when_the_last_one_goes(window, app) -> None:
    window.tasks.completed_hold_ms = 60
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    window.task_overlay.open_panel()
    app.processEvents()
    panel = window.task_overlay.panel()
    assert panel is not None
    assert not panel.empty_label.isVisible()

    task.finish()
    assert _pump(app, lambda: not window.tasks.tasks(), timeout=3.0)
    _settle_deletes(app)
    assert panel.empty_label.isVisible(), "an empty list has to say so"
    assert panel.empty_label.text() == i18n.tr("tasks.empty")
    assert panel.rows() == [] and panel.is_empty()
    assert window.findChildren(TaskButton) == [], "the icon left with the last task"
    window.task_overlay.close_panel()
    _pump(app, lambda: not panel.isVisible(), timeout=2.0)
    assert not panel.is_open()


def test_a_cancelled_task_is_what_the_panel_shows(window, app) -> None:
    stopped: list[bool] = []
    task = window.tasks.add_task(
        "tasks.title.install", "1.21.11", cancel=lambda _task: stopped.append(True)
    )
    task.set_progress(1, 4, 0)
    window.task_overlay.open_panel()
    app.processEvents()
    panel = window.task_overlay.panel()
    row = panel.rows()[0]

    row.action.click()  # the row's 取消
    app.processEvents()
    assert stopped == [True], "the cancel button did not ask the producer to stop"
    assert task.state == "cancelled"
    assert row.action.text() == i18n.tr("tasks.dismiss")
    assert task.percent() == 25
    _settle_deletes(app)


def test_a_panel_row_is_a_focus_stop_with_a_name(window, app) -> None:
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    window.task_overlay.open_panel()
    app.processEvents()
    panel = window.task_overlay.panel()
    row = panel.rows()[0]
    assert row.action.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert row.action.accessibleName() == f"{i18n.tr('tasks.cancel')} · {task.title()}"
    task.cancel()
    _settle_deletes(app)


# ---------- 4. measurements: sizes, fonts, contrast ----------


def test_the_sizes_are_the_ones_the_spec_names() -> None:
    assert ICON_SIZE == 48
    assert EDGE == 16
    assert PANEL_WIDTH == 360
    assert ROW_HEIGHT == 96
    assert ACTION_HEIGHT == 32


def test_the_row_paints_its_text_at_a_readable_size_and_weight(window, app) -> None:
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    window.task_overlay.open_panel()
    app.processEvents()
    row = window.task_overlay.panel().rows()[0]

    title = row.title_font()
    small = row.small_font()
    assert title.pixelSize() == 14 and title.weight() == 600
    assert small.pixelSize() == 12 and small.weight() == 400
    assert row.action.height() == ACTION_HEIGHT, "the touch target stays the design metric"
    task.cancel()
    _settle_deletes(app)


def _contrast(foreground: str, background: str) -> float:
    return theme.contrast_ratio(theme.normalize_hex(foreground), theme.normalize_hex(background))


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_every_text_in_the_task_views_clears_the_contrast_floor(theme_name) -> None:
    """4.5:1 for text, 3:1 for the ring's filled arc against its own track (WCAG 1.4.3 / 1.4.11)."""
    palette = theme.theme_palette(theme_name)
    for foreground, background in (
        (palette["FG_SELECTED"], palette["ACCENT_BUTTON"]),  # the ring, the number, the glyph
        (palette["FG_TEXT"], palette["BG_SECTION"]),  # the row title
        (palette["FG_HINT"], palette["BG_SECTION"]),  # the state, the sizes, the rate
        (palette["DANGER_FG"], palette["BG_SECTION"]),  # a failed task's state and reason
        (palette["ACCENT_TEXT"], palette["BG_SECTION"]),  # a finished task's state
        (palette["FG_EMPTY_STATE"], palette["BG_LIST"]),  # 暂无任务 inside the panel
    ):
        assert _contrast(foreground, background) >= 4.5, (foreground, background, theme_name)
    # Non-text: the bar's fill against the track it moves over.
    assert _contrast(palette["ACCENT"], palette["BG_LIST"]) >= 3.0, theme_name


def test_every_accent_preset_keeps_the_icon_readable() -> None:
    for accent in ACCENT_PRESETS:
        for theme_name in ("light", "dark"):
            palette = theme.theme_palette(theme_name, accent)
            assert _contrast(palette["FG_SELECTED"], palette["ACCENT_BUTTON"]) >= 4.5, (
                accent,
                theme_name,
            )
