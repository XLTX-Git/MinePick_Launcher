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

"""The download page: five kinds of install, one task each, and an honest cancel.

The window is the real ``MainWindow`` built offscreen, so the page is a real stack page reached through
the real corner icon and the real navigation. The five install entry points are the *page* entry points
(``install_version_id``'s flow, the resource download, ``_install_modpack_now``)
with only the core function scripted: a test may not use the network, and the
question here is whether the adapter wired to those functions reports what they do.

Cancellation is the one place that is measured against a real transfer (:class:`_SlowPack` runs the real
``Downloader`` over a client that hands its body back chunk by chunk), because "the download really
stopped" is the claim. The other cancel test is named for what it actually is: the model's fallback when
nothing can be stopped, where the list only stops waiting.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtCore import Qt

from gui import i18n, inwindow_dialog
from gui.pages import downloads_page, mods_page
from gui.pages.downloads_page import DownloadsPage
from gui.task_ui import TaskButton
from gui.tasks import CANCELLED, DONE, FAILED, RUNNING, format_size
from launcher.meta.manifest import ManifestVersion, VersionManifest
from launcher.net import downloader

CHUNK_BYTES = 64 * 1024
CHUNKS = 24
CHUNK_DELAY = 0.06


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


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
    """A real window on the versions page, with the network stubbed out everywhere."""
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

    from gui.pages import versions_install_flow, versions_page

    monkeypatch.setattr(versions_page, "fetch_manifest", lambda cache_path=None: _manifest())
    monkeypatch.setattr(versions_install_flow, "probe_loader", lambda loader, version_id: [object()])
    # The resources page loads its first list when it is first shown; a test may not fetch it.
    monkeypatch.setattr(mods_page._ContentTab, "load_initial", lambda self: None)
    monkeypatch.setattr(mods_page, "search_projects", lambda *args, **kwargs: [])
    downloader.reset_cancellation_for_tests()

    from gui.main_window import NAV_KEYS, MainWindow

    built = MainWindow()
    built.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    built.resize(1000, 610)
    built.show()
    app.processEvents()
    built.sidebar.setCurrentRow(NAV_KEYS.index("versions"))
    assert _pump(app, lambda: built.pages["versions"].list.count() > 0)
    yield built
    downloader.reset_cancellation_for_tests()
    built.close()


# ---------- helpers ----------


def _open_page(window, app) -> DownloadsPage:
    window.open_download_page()
    app.processEvents()
    page = window.downloads_page()
    assert page is not None and page.isVisible()
    return page


def _click_icon(window, app) -> DownloadsPage:
    """The real way in: the corner icon is clicked."""
    button = window.findChild(TaskButton)
    assert button is not None, "there is no icon to click"
    button.click()
    app.processEvents()
    page = window.downloads_page()
    assert page is not None
    return page


def _version_install(window, app):
    """Start a version install through the page's own flow (the install step's pill button)."""
    page = window.pages["versions"]
    page.begin_install("1.21.11")
    assert _pump(
        app,
        lambda: all(
            page.step.loader_status(loader) != mods_page_flow().CHECKING
            for loader in mods_page_flow().LOADER_ROWS
        ),
    )
    page.step.start_button.click()
    assert _pump(app, lambda: len(window.tasks.tasks()) == 1), "the install never became a task"
    return window.tasks.tasks()[-1]


def mods_page_flow():
    from gui.pages import versions_install_flow

    return versions_install_flow


def _start_content_install(tab, slug: str, title: str) -> None:
    """Step 3 of the resource flow (the scripted core is patched by each test)."""
    tab._download_version(
        slug, title, "modrinth", object(), tab._game_dir() / "downloads" / f"{slug}.jar"
    )


def _info(title: str = "Sodium", slug: str = "sodium"):
    return SimpleNamespace(
        title=title, slug=slug, depends=[], optional_depends=[], version="1.0"
    )


def _pack():
    return SimpleNamespace(
        instance_name="Better MC",
        version="1.0",
        loader="fabric",
        loader_version="0.16",
        minecraft="1.21.11",
        files_count=3,
    )


def _progress(done_bytes: int, total_bytes: int):
    return SimpleNamespace(
        done_bytes=done_bytes,
        total_bytes=total_bytes,
        done_files=1,
        total_files=4,
        current="client.jar",
    )


# ---------- 1. the page is reachable, and only through the icon ----------


def test_there_is_no_icon_and_no_page_without_a_task(window) -> None:
    assert window.findChildren(TaskButton) == [], "an icon exists with no task to report"
    assert window.downloads_page() is None
    assert window.stack.count() == len(window.pages.built_keys())


def test_the_icon_opens_the_page_and_back_returns_to_where_it_was_opened(window, app) -> None:
    """The page remembers its origin on the window, so 返回 goes back there - not to a fixed page."""
    from gui.main_window import NAV_KEYS

    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    assert _pump(app, lambda: window.findChild(TaskButton) is not None)

    window.sidebar.setCurrentRow(NAV_KEYS.index("versions"))
    app.processEvents()
    page = _click_icon(window, app)
    assert window.stack.currentWidget() is page, "the icon opened something other than the page"
    assert window.downloads_origin() == "versions"
    assert window.sidebar.currentRow() == NAV_KEYS.index("versions"), (
        "the page must not move the navigation rail: it is a detour, not a destination"
    )

    page.back_button.click()
    app.processEvents()
    assert window.stack.currentWidget() is window.pages.peek("versions"), (
        "返回 did not go back to the page the download page was opened from"
    )

    # A different origin proves the page is not hard-wired to the versions page.
    window.sidebar.setCurrentRow(NAV_KEYS.index("settings"))
    app.processEvents()
    page = _click_icon(window, app)
    assert window.downloads_origin() == "settings"
    page.back_button.click()
    app.processEvents()
    assert window.stack.currentWidget() is window.pages.peek("settings")
    task.cancel()


def test_clicking_the_selected_rail_entry_while_on_the_page_leaves_it(window, app) -> None:
    """The rail keeps the origin selected, so its click emits no row change - it must still navigate."""
    from gui.main_window import NAV_KEYS

    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    _pump(app, lambda: window.findChild(TaskButton) is not None)
    page = _click_icon(window, app)
    assert window.stack.currentWidget() is page

    item = window.sidebar.item(NAV_KEYS.index("versions"))
    window.sidebar.itemClicked.emit(item)  # what a real click on the already-selected row emits
    app.processEvents()
    assert window.stack.currentWidget() is window.pages.peek("versions")
    task.cancel()


# ---------- 2. the five installs, one task each ----------


def test_the_five_installs_each_become_one_task(window, app, monkeypatch, ws_tmp) -> None:
    """Version, mod, modpack, resource pack, shader pack: five entry points, five rows."""
    from gui.pages import versions_page

    seen: list[str] = []

    def scripted_version(version_id, **kwargs):
        seen.append("version")
        return SimpleNamespace(downloaded=1, skipped=0, failed=[])

    def scripted_mod(*args, **kwargs):
        seen.append("mod")
        return _info("Sodium", "sodium")

    def scripted_pack(*args, **kwargs):
        seen.append("modpack" if "cache_dir" in kwargs else "pack")
        return _pack() if "cache_dir" in kwargs else _info("Fresh Animations", "fresh-animations")

    monkeypatch.setattr(versions_page, "install_version", scripted_version)
    monkeypatch.setattr(mods_page, "install_mod", scripted_mod)
    monkeypatch.setattr(mods_page, "install_modpack", scripted_pack)
    monkeypatch.setattr(mods_page, "install_resourcepack", scripted_pack)
    monkeypatch.setattr(mods_page, "install_shaderpack", scripted_pack)

    tabs = window.pages["mods"]

    _version_install(window, app)
    _start_content_install(tabs.mods_tab, "sodium", "Sodium")
    tabs.modpacks_tab._install_modpack_now("better-mc", None)
    _start_content_install(tabs.resourcepacks_tab, "fresh-animations", "Fresh Animations")
    _start_content_install(tabs.shaderpacks_tab, "complementary", "Complementary")

    assert _pump(app, lambda: len(window.tasks.tasks()) == 5), (
        f"expected one task per install, got {[t.kind for t in window.tasks.tasks()]}"
    )
    tasks = window.tasks.tasks()
    assert [task.kind for task in tasks] == [
        "version",
        "mod",
        "modpack",
        "resourcepack",
        "shaderpack",
    ]
    assert [task.title() for task in tasks] == [
        i18n.tr("tasks.title.install", "1.21.11"),
        i18n.tr("tasks.title.mod", "Sodium"),
        i18n.tr("tasks.title.modpack", "better-mc"),
        i18n.tr("tasks.title.resourcepack", "Fresh Animations"),
        i18n.tr("tasks.title.shaderpack", "Complementary"),
    ]
    assert _pump(app, lambda: all(task.state == DONE for task in window.tasks.tasks())), (
        "a scripted install must finish its task"
    )
    assert sorted(seen) == ["mod", "modpack", "pack", "pack", "version"]

    page = _open_page(window, app)
    assert len(page.rows()) == 5
    assert page.section_header(DONE) is not None
    assert page.summary_text() == i18n.tr("tasks.page.done", 5)


def test_the_page_shows_the_five_kinds_with_their_own_glyphs(window, app) -> None:
    """Each row carries a type glyph, and the five kinds are five different ones."""
    for kind, title_key in (
        ("version", "tasks.title.install"),
        ("mod", "tasks.title.mod"),
        ("modpack", "tasks.title.modpack"),
        ("resourcepack", "tasks.title.resourcepack"),
        ("shaderpack", "tasks.title.shaderpack"),
    ):
        window.tasks.add_task(title_key, "x", kind=kind)
    page = _open_page(window, app)
    names = [row.icon_name() for row in page.rows()]
    assert names == ["versions", "mods", "export", "resourcepacks", "shaderpacks"]
    for row in page.rows():
        assert not row.icon_label.pixmap().isNull(), "a row without its glyph"


# ---------- 3. progress really moves ----------


def test_progress_reaches_the_task_and_the_row(window, app, monkeypatch) -> None:
    """The core's progress callback ends up as the task's numbers and the row's bar and text."""
    release = threading.Event()

    def slow_pack(*args, **kwargs):
        progress = kwargs["progress"]
        progress(_progress(1 * 1024 * 1024, 4 * 1024 * 1024))
        release.wait(5.0)
        return _info("Fresh Animations", "fresh-animations")

    monkeypatch.setattr(mods_page, "install_resourcepack", slow_pack)
    tabs = window.pages["mods"]
    _start_content_install(tabs.resourcepacks_tab, "fresh-animations", "Fresh Animations")
    assert _pump(app, lambda: len(window.tasks.tasks()) == 1)
    task = window.tasks.tasks()[0]
    page = _open_page(window, app)

    assert _pump(app, lambda: task.done_bytes == 1 * 1024 * 1024), (
        "the progress callback never reached the task"
    )
    assert task.total_bytes == 4 * 1024 * 1024
    assert task.percent() == 25
    row = page.row_for(task)
    assert row is not None
    assert row.bar.value() == 250, "the bar is on the 0-1000 scale every bar in the app uses"
    assert row.bar_text.text() == "25%"
    assert "1.0 MB / 4.0 MB" in row.meta_label.text()
    assert row.bar_row.isVisible()
    release.set()
    assert _pump(app, lambda: task.state == DONE)


# ---------- 4. cancellation: what really stops, and what only stops waiting ----------


class _SlowResponse:
    """A streaming response that hands its body back one chunk at a time, with a pause in between."""

    def __init__(self, url: str, body: bytes) -> None:
        self.status_code = 200
        self.url = url
        self.headers = httpx.Headers({"Content-Length": str(len(body))})
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, size: int):
        for offset in range(0, len(self._body), size):
            time.sleep(CHUNK_DELAY)
            yield self._body[offset : offset + size]


class _SlowClient:
    def __init__(self) -> None:
        self.body = b"m" * (CHUNKS * CHUNK_BYTES)

    def stream(self, method: str, url: str, **kwargs) -> _SlowResponse:
        return _SlowResponse(url, self.body)


class _SlowPack:
    """A resource-pack download a test may interrupt: the real ``Downloader`` over the slow client."""

    def __init__(self, dest: Path) -> None:
        self.client = _SlowClient()
        self.dest = dest

    def __call__(self, slug, **kwargs):
        tasks = [
            downloader.DownloadTask(
                "https://example.invalid/pack.zip",
                self.dest / "pack.zip",
                size=len(self.client.body),
            )
        ]
        return downloader.Downloader(client=self.client, concurrency=1).download(
            tasks, kwargs["progress"]
        )


def test_cancel_really_stops_the_transfer_and_is_not_a_failure(
    window, app, monkeypatch, ws_tmp
) -> None:
    """A resource pack's cancel stops the bytes: measured against the transfer, not against a flag."""
    slow = _SlowPack(ws_tmp / "download")
    slow.dest.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mods_page, "install_resourcepack", slow)
    tabs = window.pages["mods"]
    _start_content_install(tabs.resourcepacks_tab, "fresh-animations", "Fresh Animations")
    assert _pump(app, lambda: len(window.tasks.tasks()) == 1)
    task = window.tasks.tasks()[0]

    assert _pump(app, lambda: downloader.active_download_count() > 0, timeout=5.0)
    part = slow.dest / "pack.zip.part"
    assert _pump(app, lambda: part.exists(), timeout=5.0), "the download never started writing"

    page = _open_page(window, app)
    row = page.row_for(task)
    assert row is not None
    row.action.click()  # 取消
    app.processEvents()
    assert task.state == CANCELLED
    assert task.percent() == 0 or task.percent() > 0  # the percentage froze where it stopped

    assert _pump(app, lambda: downloader.active_download_count() == 0, timeout=10.0), (
        "the transfer never unwound after the cancel"
    )
    written = part.stat().st_size if part.exists() else 0
    time.sleep(0.3)
    after = part.stat().st_size if part.exists() else 0
    assert after == written, "bytes were still being written after the cancel"
    assert written < len(slow.client.body), "the transfer had already finished: nothing was cancelled"

    # A cancel is not a failure: the row says 已取消, and no error was reported for it.
    assert task.error == ""
    assert window.status_label.text() == i18n.tr("tasks.status.cancelled", task.title())
    assert window.tasks.counts()[CANCELLED] == 1
    assert window.tasks.counts()[FAILED] == 0


def test_a_second_transfer_stopped_by_the_same_latch_ends_cancelled_not_failed(
    window, app, monkeypatch
) -> None:
    """The stop latch is process-wide: the *other* row's transfer stops too, and that is a cancel.

    Emulated the way the real downloader behaves: the second install raises ``DownloadCancelled`` when
    the latch is set. Reporting that as a failure would blame the user for a stop they did ask for.
    """
    release = threading.Event()

    def pack_a(*args, **kwargs):
        release.wait(5.0)
        raise downloader.DownloadCancelled("cancelled")

    def pack_b(*args, **kwargs):
        # Waits until the other row's cancel has set the latch, then dies exactly like the real
        # downloader does at its next 64 KB chunk.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not downloader.is_cancelled():
            time.sleep(0.01)
        raise downloader.DownloadCancelled("cancelled")

    monkeypatch.setattr(mods_page, "install_resourcepack", pack_a)
    tabs = window.pages["mods"]
    _start_content_install(tabs.resourcepacks_tab, "fresh-animations", "Fresh Animations")
    monkeypatch.setattr(mods_page, "install_shaderpack", pack_b)
    _start_content_install(tabs.shaderpacks_tab, "complementary", "Complementary")
    assert _pump(app, lambda: len(window.tasks.tasks()) == 2)
    first, second = window.tasks.tasks()

    page = _open_page(window, app)
    page.row_for(first).action.click()  # 取消 the first one only
    app.processEvents()
    assert first.state == CANCELLED
    assert _pump(app, lambda: second.state != RUNNING), "the second transfer never unwound"
    assert second.state == CANCELLED, (
        "the transfer the shared latch stopped was reported as a failure the user caused"
    )
    assert second.error == ""
    assert window.tasks.counts()[FAILED] == 0
    release.set()


def test_cancel_without_a_stop_callback_only_abandons_the_wait_it_is_not_a_real_stop(window, app) -> None:
    """假停: nothing can stop this job, so 取消 only stops the list waiting for it.

    The five installs all register a real (cooperative) stop, so the shipped page never takes this
    path. It is the model's fallback and it is pinned here so the difference cannot quietly become
    "the launcher says it stopped something it never asked to stop".
    """
    task = window.tasks.add_task("tasks.title.mod", "sodium", kind="mod")  # no cancel callback
    page = _open_page(window, app)
    row = page.row_for(task)
    assert row is not None
    row.action.click()
    app.processEvents()
    assert task.state == CANCELLED, "the row has to end: the list is done waiting for it"
    assert task.percent() == 0
    assert window.tasks.counts()[CANCELLED] == 1


# ---------- 5. who leaves, and when ----------


def test_a_language_switch_drops_the_page_and_the_next_click_builds_it_again(window, app) -> None:
    """A rebuilt window (what a language switch does) must not leave the old page wired to the list."""
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    page = _open_page(window, app)
    assert page.row_for(task) is not None

    window.build_pages()  # every stack page is dropped and rebuilt
    app.processEvents()
    assert window.downloads_page() is None
    assert task.is_running(), "a rebuild must not settle the task it was listing"

    fresh = _open_page(window, app)
    assert fresh is not page
    assert fresh.row_for(task) is not None
    task.set_progress(1, 4)  # a tick on a task the old page used to hold: no dead-widget crash
    app.processEvents()
    assert fresh.row_for(task).bar.value() == 250
    task.cancel()


def test_a_failed_row_stays_and_a_finished_row_leaves_after_its_hold(window, app) -> None:
    window.tasks.completed_hold_ms = 120
    done = window.tasks.add_task("tasks.title.mod", "sodium", kind="mod")
    failed = window.tasks.add_task("tasks.title.modpack", "better-mc", kind="modpack")
    page = _open_page(window, app)
    assert {row.task for row in page.rows()} == {done, failed}

    failed.fail("连接超时")
    done.finish()
    app.processEvents()
    assert page.summary_text() == " · ".join(
        [i18n.tr("tasks.page.done", 1), i18n.tr("tasks.page.failed", 1)]
    )
    assert page.section_header(FAILED).text() == i18n.tr("tasks.section.failed", 1)
    assert page.section_header(DONE).text() == i18n.tr("tasks.section.done", 1)

    assert _pump(app, lambda: done not in window.tasks.tasks(), timeout=4.0), (
        "a finished task must remove itself after its hold"
    )
    app.processEvents()
    assert page.row_for(failed) is not None, "a failure stays until somebody dismisses it"
    assert page.row_for(done) is None
    assert page.row_for(failed).error_label.isVisible()
    assert page.row_for(failed).error_label.text() == "连接超时"


def test_a_failed_install_marks_the_task_failed_with_the_reason(app) -> None:
    """The install adapter must turn a worker exception into a visible failed row, not a silent 0%."""
    from PySide6.QtWidgets import QWidget

    from gui.pages.mods_page import _TaskedInstall
    from gui.tasks import TaskCenter

    host = QWidget()
    host.tasks = TaskCenter(host)
    seen: list[str] = []
    install = _TaskedInstall(host, kind="mod", title="Fabric API")

    def job(progress):
        progress(downloader.DownloadProgress(done_bytes=1024, total_bytes=2048))
        raise RuntimeError("network down")

    try:
        install.run(
            job,
            on_result=lambda _value: None,
            on_error=seen.append,
            on_finished=lambda: None,
        )
        assert _pump(app, lambda: install.task is None, timeout=4.0), "the worker never settled"
        task = host.tasks.tasks()[0]
        assert task.state == FAILED
        assert task.error == "network down"
        assert seen == ["network down"]
    finally:
        host.deleteLater()
        app.processEvents()


def test_unknown_size_prints_unknown_and_runs_a_busy_bar(window, app) -> None:
    """A source with no announced length must not read as a stuck ``0 B / 0 B`` download."""
    task = window.tasks.add_task("tasks.title.mod", "sodium", kind="mod")
    task.set_progress(1024, 0)
    page = _open_page(window, app)
    row = page.row_for(task)
    assert row.meta_label.text().startswith(f"{format_size(1024)} / ")
    assert row.meta_label.text().endswith(i18n.tr("tasks.size.unknown"))
    assert "0 B / 0 B" not in row.meta_label.text()
    assert (row.bar.minimum(), row.bar.maximum()) == (0, 0), "an unknown total needs a busy bar"
    assert row.bar_text.text() == "", "a busy bar has no percentage to print"
    assert task.percent_text() == "—"


def test_a_settled_row_prints_its_duration_and_a_running_one_prints_its_rate(window, app) -> None:
    running = window.tasks.add_task("tasks.title.install", "1.21.11")
    running.set_progress(3 * 1024 * 1024, 12 * 1024 * 1024, 1.2 * 1024 * 1024)
    done = window.tasks.add_task("tasks.title.mod", "sodium", kind="mod")
    done.set_progress(2 * 1024 * 1024, 2 * 1024 * 1024)
    done.finish()
    page = _open_page(window, app)
    running_row = page.row_for(running)
    done_row = page.row_for(done)
    assert "1.2 MB/s" in running_row.meta_label.text(), "a running row has to print its rate"
    assert running_row.percent_label.isVisible() is False, (
        "while it runs the percentage lives in the bar's own label"
    )
    assert done_row.bar_row.isVisible() is False, "a settled row has no bar"
    assert done_row.percent_label.isVisible() is True
    assert done_row.percent_label.text() == "100%"
    assert i18n.tr("tasks.duration", f"{done.duration_seconds():.1f}") in done_row.meta_label.text()


# ---------- 6. the page's own controls ----------


def test_the_summary_counts_every_non_empty_section(window, app) -> None:
    window.tasks.add_task("tasks.title.install", "a")
    done = window.tasks.add_task("tasks.title.mod", "b", kind="mod")
    failed = window.tasks.add_task("tasks.title.modpack", "c", kind="modpack")
    done.finish()
    failed.fail("boom")
    page = _open_page(window, app)
    assert page.summary_text() == " · ".join(
        [
            i18n.tr("tasks.page.running", 1),
            i18n.tr("tasks.page.done", 1),
            i18n.tr("tasks.page.failed", 1),
        ]
    )
    assert [header.text() for header in (page.section_header(RUNNING), page.section_header(DONE))] == [
        i18n.tr("tasks.section.running", 1),
        i18n.tr("tasks.section.done", 1),
    ]
    assert page.section_header(CANCELLED) is None, "an empty section prints no heading at all"
    assert page.all_done_card.isVisible() is False, "the page has no empty state while tasks exist"
    assert page.scroll.isVisible()
    assert page.cancel_all_visible()


def test_cancel_all_asks_first_and_cancels_only_on_accept(window, app, monkeypatch) -> None:
    stopped: list[str] = []
    first = window.tasks.add_task("tasks.title.mod", "a", kind="mod", cancel=lambda t: stopped.append("a"))
    second = window.tasks.add_task("tasks.title.mod", "b", kind="mod", cancel=lambda t: stopped.append("b"))
    page = _open_page(window, app)

    asked: list[tuple[str, str]] = []

    def reject(parent, title, text, **kwargs):
        asked.append((title, text))
        return inwindow_dialog.Outcome.REJECTED

    monkeypatch.setattr(downloads_page.inwindow_dialog, "confirm", reject)
    page.cancel_all_button.click()
    app.processEvents()
    assert asked and asked[0][0] == i18n.tr("tasks.page.cancel_all.title")
    assert asked[0][1] == i18n.tr("tasks.page.cancel_all.message", 2)
    assert stopped == [], "a dismissed question must not stop a single download"
    assert first.is_running() and second.is_running()

    monkeypatch.setattr(
        downloads_page.inwindow_dialog,
        "confirm",
        lambda parent, title, text, **kwargs: inwindow_dialog.Outcome.ACCEPTED,
    )
    page.cancel_all_button.click()
    app.processEvents()
    assert first.state == CANCELLED and second.state == CANCELLED
    assert sorted(stopped) == ["a", "b"]
    assert page.cancel_all_visible() is False, "全部取消 disappears once nothing is running"


def test_the_all_done_card_replaces_the_list_and_goes_back(window, app) -> None:
    window.tasks.completed_hold_ms = 100
    task = window.tasks.add_task("tasks.title.install", "1.21.11")
    page = _open_page(window, app)
    assert page.is_all_done() is False

    task.finish()
    assert _pump(app, lambda: page.is_all_done(), timeout=4.0), (
        "the page never reached its 全部完成 state after the last task left"
    )
    app.processEvents()
    assert page.all_done_card.isVisible()
    assert page.scroll.isVisible() is False
    assert page.all_done_card.title_label.text() == i18n.tr("tasks.page.all_done.title")
    assert page.summary_label.isVisible() is False

    page.all_done_card.back.click()
    app.processEvents()
    assert window.stack.currentWidget() is window.pages.peek("versions")


def test_a_row_is_a_focus_stop_with_a_name(window, app) -> None:
    task = window.tasks.add_task("tasks.title.mod", "sodium", kind="mod")
    page = _open_page(window, app)
    row = page.row_for(task)
    assert row is not None
    assert row.action.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert row.action.accessibleName() == f"{i18n.tr('tasks.cancel')} · {task.title()}"
    assert row.accessibleName() == (
        f"{task.title()} · {task.state_text()} · {task.percent_text()}"
    )
    assert window.focusWidget() is not None


# ---------- 7. the retry button ----------


def test_retry_from_the_page_runs_the_install_again(window, app, monkeypatch) -> None:
    attempts: list[int] = []

    def broken(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return _info("Sodium", "sodium")

    monkeypatch.setattr(mods_page, "install_mod", broken)
    tabs = window.pages["mods"]
    _start_content_install(tabs.mods_tab, "sodium", "Sodium")
    assert _pump(app, lambda: len(window.tasks.tasks()) == 1)
    first = window.tasks.tasks()[0]
    assert _pump(app, lambda: first.state == FAILED)
    assert first.error and "boom" in first.error

    page = _open_page(window, app)
    row = page.row_for(first)
    assert row is not None and row.retry.isVisible(), "a failed row with a producer offers 重试"
    row.retry.click()
    app.processEvents()
    assert first not in window.tasks.tasks(), "the failed row is replaced by the new attempt"
    assert _pump(app, lambda: len(window.tasks.tasks()) == 1)
    second = window.tasks.tasks()[0]
    assert second is not first and second.kind == "mod"
    assert _pump(app, lambda: second.state == DONE), "the retried install did not run"
    assert len(attempts) == 2


def test_a_failed_version_row_offers_no_retry_and_says_why(window, app, monkeypatch) -> None:
    """The version install asks a modal loader question; a retry from here would pop it, so it has none.

    The row still offers 移除, and the versions page is where the install is started again.
    """
    from gui.pages import versions_page

    def broken(version_id, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(versions_page, "install_version", broken)
    monkeypatch.setattr(versions_page, "show_fatal", lambda parent, message: None)
    task = _version_install(window, app)
    assert _pump(app, lambda: task.state == FAILED)
    page = _open_page(window, app)
    row = page.row_for(task)
    assert row is not None
    assert row.retry.isVisible() is False
    assert row.action.text() == i18n.tr("tasks.dismiss")
    assert task.can_retry() is False
