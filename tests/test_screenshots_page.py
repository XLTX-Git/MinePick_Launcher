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

"""The screenshot page: the two folders it scans, its thumbnails and its delete question."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage

from gui import i18n
from launcher import screenshots


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def game(ws_tmp, monkeypatch):
    """A game folder with the shared screenshots, an instance's own, and no config surprises."""
    root = ws_tmp / "game"
    (root / "screenshots").mkdir(parents=True)
    (root / "versions" / "fabric-demo" / "screenshots").mkdir(parents=True)
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(root))
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    return root


def _touch(path: Path, when: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (when, when))
    return path


def test_scan_reads_the_shared_folder_and_every_instance_folder(game) -> None:
    _touch(game / "screenshots" / "2026-11-08_20.00.00.png", 300)
    _touch(game / "versions" / "fabric-demo" / "screenshots" / "2026-11-08_19.00.00.png", 200)
    _touch(game / "versions" / "fabric-demo" / "screenshots" / "notes.txt", 400)

    found = screenshots.scan_screenshots(game, {"fabric-demo": "整合包演示"})

    assert [entry.filename for entry in found] == [
        "2026-11-08_20.00.00.png",
        "2026-11-08_19.00.00.png",
    ]
    assert found[0].is_shared
    assert found[1].instance_id == "fabric-demo"
    assert found[1].instance_name == "整合包演示"
    # newest first, even though the instance file would win a plain name sort
    assert found[0].taken_at > found[1].taken_at


def test_scan_instance_reads_only_that_instance_and_the_shared_folder(game) -> None:
    """The scoped scan used by the instance detail: one instance plus the shared folder, no others."""
    _touch(game / "screenshots" / "shared.png", 300)
    _touch(game / "versions" / "fabric-demo" / "screenshots" / "mine.png", 200)
    _touch(game / "versions" / "other" / "screenshots" / "not-mine.png", 400)

    found = screenshots.scan_instance_screenshots(
        game, "fabric-demo", "整合包演示", game / "versions" / "fabric-demo"
    )

    assert [entry.filename for entry in found] == ["shared.png", "mine.png"]
    assert found[0].is_shared
    assert found[1].instance_id == "fabric-demo"
    assert found[1].instance_name == "整合包演示"
    assert all(entry.filename != "not-mine.png" for entry in found)


def test_scan_instance_in_shared_mode_scans_the_shared_folder_once(game) -> None:
    """A shared-mode instance runs in the game root: the launch_dir is the shared folder."""
    _touch(game / "screenshots" / "shared.png", 300)

    found = screenshots.scan_instance_screenshots(game, "shared-mode", "共享实例", game)

    assert [entry.filename for entry in found] == ["shared.png"]
    assert found[0].is_shared


def test_scan_without_any_screenshot_folder_is_empty(game) -> None:
    (game / "screenshots").rmdir()
    (game / "versions" / "fabric-demo" / "screenshots").rmdir()
    assert screenshots.scan_screenshots(game, {}) == []


def test_scan_ignores_other_suffixes_and_directories(game) -> None:
    _touch(game / "screenshots" / "a.png", 100)
    _touch(game / "screenshots" / "b.PNG", 100)
    (game / "screenshots" / "not-a-picture.jpg").mkdir()
    (game / "screenshots" / "readme.md").write_text("x", encoding="utf-8")
    assert [entry.filename for entry in screenshots.scan_screenshots(game, {})] == [
        "a.png",
        "b.PNG",
    ]


def test_a_big_picture_is_decoded_at_thumbnail_size(app, game, ws_tmp) -> None:
    from gui.pages.screenshots_page import THUMB_HEIGHT, THUMB_WIDTH, load_thumbnail

    path = ws_tmp / "big.png"
    image = QImage(1600, 900, QImage.Format.Format_RGB32)
    image.fill(0xFF3366)
    assert image.save(str(path))

    thumb = load_thumbnail(str(path))

    assert thumb is not None
    assert thumb.width() <= THUMB_WIDTH
    assert thumb.height() <= THUMB_HEIGHT
    # 1600x900 keeps its 16:9 shape: 120x68 is the slot, so the width is the limit
    assert thumb.width() == THUMB_WIDTH


def test_an_unreadable_picture_has_no_thumbnail(app, ws_tmp) -> None:
    from gui.pages.screenshots_page import load_thumbnail

    broken = ws_tmp / "broken.png"
    broken.write_bytes(b"not a png at all")
    assert load_thumbnail(str(broken)) is None
    assert load_thumbnail(str(ws_tmp / "missing.png")) is None


def test_the_list_is_built_without_waiting_for_a_thumbnail(app, game, monkeypatch) -> None:
    """Rows appear first; the decodes are dispatched to the worker pool."""
    from gui.pages import screenshots_page

    for index in range(8):
        _touch(game / "screenshots" / f"{index:02d}.png", 100 + index)
    calls: list[tuple[object, list]] = []

    def record(fn, *args, **kwargs):
        calls.append((fn, list(args)))

    monkeypatch.setattr(screenshots_page, "run_in_background", record)
    started = time.monotonic()
    page = screenshots_page.ScreenshotsPage()
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 1.0, "building the list waited for the pictures"
        assert len(page.rows()) == 8
        assert all(not row.has_thumbnail for row in page.rows())
        assert calls, "no background decode was dispatched"
        assert calls[0][0] is screenshots_page.load_thumbnail_batch
        assert calls[0][1][0], "the batch was empty"
    finally:
        page.deleteLater()


def test_a_broken_picture_is_not_requested_again_and_again(app, game, monkeypatch) -> None:
    """A file Qt cannot decode must not keep scheduling a batch: the row is asked for once."""
    from gui.pages import screenshots_page

    for index in range(5):
        _touch(game / "screenshots" / f"{index:02d}.png", 100 + index)
    calls: list[list] = []

    def immediately(fn, items, *, on_result=None, on_error=None):
        calls.append(list(items))
        assert on_result is not None
        on_result([(path, stamp, None) for path, stamp in items])

    monkeypatch.setattr(screenshots_page, "run_in_background", immediately)
    page = screenshots_page.ScreenshotsPage()
    try:
        assert len(calls) == 1, f"the failed batch was requeued {len(calls) - 1} time(s)"
        assert len(page.rows()) == 5
        assert all(not row.has_thumbnail for row in page.rows())
    finally:
        page.deleteLater()


def test_delete_asks_first_and_defaults_to_cancel(app, game, monkeypatch) -> None:
    from gui import inwindow_dialog
    from gui.pages import screenshots_page

    path = _touch(game / "screenshots" / "keep.png", 100)
    page = screenshots_page.ScreenshotsPage()
    seen: dict[str, object] = {}

    def refuse(parent, title, text, **kwargs):
        seen.update(kwargs)
        seen["title"] = title
        seen["text"] = text
        return inwindow_dialog.Outcome.DISMISSED

    monkeypatch.setattr(screenshots_page.inwindow_dialog, "confirm", refuse)
    try:
        assert page.delete_entry(page.entries()[0]) is False
        assert path.exists(), "the dismissed question deleted the file"
        assert seen["default"] is inwindow_dialog.Outcome.REJECTED
        assert "keep.png" in str(seen["text"])
    finally:
        page.deleteLater()


def test_delete_removes_the_file_once_accepted(app, game, monkeypatch) -> None:
    from gui import inwindow_dialog
    from gui.pages import screenshots_page

    path = _touch(game / "screenshots" / "throw-away.png", 100)
    page = screenshots_page.ScreenshotsPage()
    monkeypatch.setattr(
        screenshots_page.inwindow_dialog,
        "confirm",
        lambda *args, **kwargs: inwindow_dialog.Outcome.ACCEPTED,
    )
    try:
        assert page.delete_entry(page.entries()[0]) is True
        assert not path.exists()
        assert page.entries() == []
    finally:
        page.deleteLater()


def test_the_empty_state_names_the_folder_it_scanned(app, game) -> None:
    from gui.pages.screenshots_page import ScreenshotsPage

    page = ScreenshotsPage()
    try:
        assert page.entries() == []
        assert page.rows() == []
        assert page.stack.currentIndex() == 1  # the empty page, not the scroll area
        text = page.empty.text()
        assert "还没有截图" in text or "No screenshots yet" in text
        assert str(game / "screenshots") in text
    finally:
        page.deleteLater()


def test_the_embedded_page_waits_for_a_scope(app, game) -> None:
    """Inside the detail page the gallery must not scan the whole game folder before it is scoped."""
    from gui.pages.screenshots_page import ScreenshotsPage

    _touch(game / "screenshots" / "shared.png", 100)
    page = ScreenshotsPage(embedded=True)
    try:
        assert page.entries() == []
        assert not page.has_scope()
        page.set_instance(
            "fabric-demo", "整合包演示", game / "versions" / "fabric-demo", game
        )
        page.refresh()
        assert page.has_scope()
        assert page.scope_label.isVisibleTo(page)
        assert page.scope_label.text() == i18n.tr("instances.detail.screenshots.scope")
        assert [entry.filename for entry in page.entries()] == ["shared.png"]
        page.clear_instance()
        assert not page.has_scope()
        assert page.entries() == []
    finally:
        page.deleteLater()
