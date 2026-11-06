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

"""The adapter: a real install becomes a task, and a cancel really stops the transfer.

The frozen install flow (``versions_page.install_version_id`` and its three callbacks) is entered from the
real page - the install step's pill, the real worker pool, the real ``download_version`` closure - and
only the *download* is scripted, because a test may not use the network. The cancellation test is the
exception on purpose: it runs the real ``Downloader`` over a client that hands its body back chunk by
chunk, so "the download really stops" is measured against the transfer itself (a batch that is still
writing bytes after the cancel fails the test), not against a flag this suite set.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtCore import Qt

from gui import i18n
from gui.pages import versions_install_flow, versions_tasks
from launcher.meta.manifest import ManifestVersion, VersionManifest
from launcher.net import downloader

# A body that arrives slowly enough for the cancel to land in the middle of it.
SLOW_CHUNKS = 24
SLOW_CHUNK_BYTES = 64 * 1024
SLOW_CHUNK_DELAY = 0.06


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
    # No loader source may be asked: the rows answer instantly and the vanilla pill is what the tests use.
    monkeypatch.setattr(versions_install_flow, "probe_loader", lambda loader, version_id: [object()])
    downloader.reset_cancellation_for_tests()

    from gui.main_window import NAV_KEYS, MainWindow

    built = MainWindow()
    built.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    built.resize(1000, 610)
    built.show()
    app.processEvents()
    built.sidebar.setCurrentRow(NAV_KEYS.index("versions"))
    page = built.pages["versions"]
    assert _pump(app, lambda: page.list.count() > 0), "the version list never filled"
    yield built, page
    downloader.reset_cancellation_for_tests()
    built.close()


def _start_install(window, page, app, version_id: str = "1.21.11"):
    """Open the install step, wait for the loader rows, press the pill - the real path to the download."""
    page.begin_install(version_id)
    assert _pump(
        app,
        lambda: all(
            page.step.loader_status(loader) != versions_install_flow.CHECKING
            for loader in versions_install_flow.LOADER_ROWS
        ),
    )
    page.step.start_button.click()
    # The task is created synchronously by the adaptor, but a scripted install can also finish inside
    # the same event-loop turn, so this waits for the task to *exist*, not for it to still be running.
    assert _pump(app, lambda: len(window.tasks.tasks()) == 1), (
        "the install never became a task: "
        f"start={page.step.start_button.isEnabled()} "
        f"install={page.install_button.isEnabled()} "
        f"name={page.step.name_edit.text()!r} valid={page.step.name_valid()}"
    )
    return window.tasks.tasks()[-1]


def _progress(done_files: int, total_files: int, done_bytes: int, total_bytes: int):
    return SimpleNamespace(
        done_files=done_files,
        total_files=total_files,
        done_bytes=done_bytes,
        total_bytes=total_bytes,
        current="client.jar",
    )


# ---------- a successful install ----------


def test_an_install_becomes_a_task_and_finishes_as_one(window, app, monkeypatch) -> None:
    from gui.pages import versions_page

    def scripted_install(version_id, **kwargs):
        progress = kwargs["progress"]
        progress(_progress(1, 4, 1 * 1024 * 1024, 4 * 1024 * 1024))
        progress(_progress(4, 4, 4 * 1024 * 1024, 4 * 1024 * 1024))
        return SimpleNamespace(downloaded=4, skipped=0, failed=[])

    monkeypatch.setattr(versions_page, "install_version", scripted_install)

    built, page = window
    task = _start_install(built, page, app)

    assert task.title() == i18n.tr("tasks.title.install", "1.21.11")
    assert _pump(app, lambda: task.state == "done"), "a finished install left its task running"
    assert task.done_bytes == 4 * 1024 * 1024
    assert task.total_bytes == 4 * 1024 * 1024
    assert task.percent() == 100
    assert built.tasks.icon_state() == "done"

    # The frozen flow's ending ran (the status line says 完成), and the row's progress went back to
    # its install button rather than being left mid-download.
    assert i18n.tr("versions.msg.done", 4, 0) in built.status_label.text()
    row = page._row_for("1.21.11")
    assert row is not None and not row.install_running()
    assert row.install.isVisible()

    built.open_download_page()
    app.processEvents()
    assert built.downloads_page().rows()[0].task is task


# ---------- cancelling: the transfer stops ----------


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
            time.sleep(SLOW_CHUNK_DELAY)
            yield self._body[offset : offset + size]


class _SlowClient:
    """Just enough of ``httpx.Client`` for the single-stream download path."""

    def __init__(self) -> None:
        self.body = b"m" * (SLOW_CHUNKS * SLOW_CHUNK_BYTES)

    def stream(self, method: str, url: str, **kwargs) -> _SlowResponse:
        return _SlowResponse(url, self.body)


class _SlowInstall:
    """The download a test may interrupt: a real ``Downloader`` batch over the slow client."""

    def __init__(self, dest) -> None:
        self.client = _SlowClient()
        self.dest = dest

    def __call__(self, version_id, **kwargs):
        tasks = [
            downloader.DownloadTask(
                "https://example.invalid/client.jar",
                self.dest / "client.jar",
                size=len(self.client.body),
            )
        ]
        return downloader.Downloader(client=self.client, concurrency=1).download(
            tasks, kwargs["progress"]
        )


def test_cancel_stops_the_real_download_and_is_not_reported_as_a_failure(
    window, app, monkeypatch, ws_tmp
) -> None:
    from gui.pages import versions_page

    slow = _SlowInstall(ws_tmp / "download")
    slow.dest.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(versions_page, "install_version", slow)

    dialogs: list[str] = []
    monkeypatch.setattr(versions_page, "show_fatal", lambda parent, message: dialogs.append(message))

    built, page = window
    task = _start_install(built, page, app)

    # The transfer is really running: the downloader counts it, and a thread is writing bytes.
    assert _pump(app, lambda: downloader.active_download_count() > 0, timeout=5.0)
    assert task.is_running()
    part = slow.dest / "client.jar.part"
    assert _pump(app, lambda: part.exists(), timeout=5.0), "the download never started writing"

    built.open_download_page()
    app.processEvents()
    assert built.downloads_page().rows()[0].task is task

    built.downloads_page().rows()[0].action.click()  # 取消
    app.processEvents()
    assert task.state == "cancelled"
    # Back to the versions page: the assertions below are about what its row shows, and a widget on a
    # page that is not in the stack is not visible (the download page has the stack right now).
    built.close_download_page()
    app.processEvents()
    assert built.stack.currentWidget() is page
    assert not page.progress_row.isVisible(), "the removed bar must not come back"
    row = page._row_for("1.21.11")
    assert row is not None and not row.install_running(), (
        "the row's inline progress has to go back at once, not when the socket notices"
    )

    # The worker ends by itself: the download is stopped, not abandoned.
    assert _pump(app, lambda: page.install_button.isEnabled(), timeout=10.0), (
        "the install never unwound after the cancel"
    )
    assert _pump(app, lambda: downloader.active_download_count() == 0, timeout=5.0)
    written = part.stat().st_size if part.exists() else 0
    time.sleep(0.3)
    after = part.stat().st_size if part.exists() else 0
    assert after == written, "bytes were still being written after the cancel"
    assert written < len(slow.client.body), "the transfer had already finished: nothing was cancelled"

    # A cancel is not a failure: no dialog, no red status line, and the task says 已取消.
    assert dialogs == [], "cancelling popped the install-failed dialog"
    assert app.activeModalWidget() is None
    assert task.state == "cancelled"
    assert built.status_label.text() == i18n.tr("tasks.status.cancelled", task.title())

    # The row is back to 安装 and the version is not marked installed.
    assert row.install.isVisible()
    assert row.install.text() == i18n.tr("versions.install.short")
    assert "1.21.11" not in page._installed

    # The cancellation latches were released: the launcher can download again afterwards.
    assert downloader.is_cancelled() is False
    assert downloader.is_user_cancelled() is False
    # Only the cancelled task is left, so the corner reports nothing (no ring, no check, no alert).
    assert built.tasks.icon_state() is None


def test_a_cancel_after_the_last_batch_still_stops_the_next_one(window, app) -> None:
    """The latch is what makes a cancel land between two batches instead of being forgotten."""
    downloader.clear_cancellation()
    assert downloader.cancel_current_downloads() == 0
    assert downloader.is_user_cancelled() is True
    with pytest.raises(downloader.DownloadCancelled):
        downloader.Downloader(client=_SlowClient()).download([])
    downloader.clear_cancellation()
    assert downloader.is_user_cancelled() is False
    assert downloader.Downloader(client=_SlowClient()).download([]).downloaded == 0, (
        "a cleared cancellation must not refuse the next batch"
    )


def test_a_page_rebuilt_mid_download_does_not_leave_a_spinning_task(
    window, app, monkeypatch, ws_tmp
) -> None:
    """A language switch deletes every page; the download it left behind must not spin forever."""

    from gui.pages import versions_page

    slow = _SlowInstall(ws_tmp / "rebuild")
    slow.dest.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(versions_page, "install_version", slow)

    built, page = window
    task = _start_install(built, page, app)
    assert _pump(app, lambda: downloader.active_download_count() > 0, timeout=5.0)

    built.build_pages()  # what a language switch does: every page is dropped and rebuilt
    assert task.state == "cancelled", "the task kept the icon spinning after its page was gone"
    assert _pump(app, lambda: downloader.active_download_count() == 0, timeout=10.0), (
        "the transfer kept running after its page was gone"
    )


# ---------- a real failure is still a failure ----------

def test_a_failed_install_still_reports_itself(window, app, monkeypatch) -> None:
    from gui.pages import versions_page

    def broken_install(version_id, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(versions_page, "install_version", broken_install)
    dialogs: list[str] = []
    monkeypatch.setattr(versions_page, "show_fatal", lambda parent, message: dialogs.append(message))

    built, page = window
    task = _start_install(built, page, app)
    assert _pump(app, lambda: task.state == "failed"), "a failed install did not fail its task"
    assert "boom" in task.error
    assert len(dialogs) == 1 and "boom" in dialogs[0], "the flow's own error dialog was swallowed"
    assert task in built.tasks.tasks(), "a failure stays listed until it is dismissed"

    row = page._row_for("1.21.11")
    assert row is not None and not row.install_running()
    assert row.install.text() == i18n.tr("versions.install.short")
    assert "1.21.11" not in page._installed


# ---------- the way back to the list ----------


def test_the_install_step_returns_to_the_list_after_a_success(window, app, monkeypatch) -> None:
    _built, page = window
    assert 2000 <= versions_tasks.AUTO_RETURN_MS <= 3000, "the promised delay is 2-3 seconds"
    monkeypatch.setattr(versions_tasks, "AUTO_RETURN_MS", 40)

    page.stack.setCurrentWidget(page.step)
    page._on_installed((SimpleNamespace(downloaded=1, skipped=0, failed=[]), None))
    assert _pump(app, lambda: page.stack.currentWidget() is page._list_step, timeout=2.0), (
        "the step never gave way to the version list"
    )


def test_the_return_does_not_steal_the_page_the_user_moved_to(window, app, monkeypatch) -> None:
    built, page = window
    monkeypatch.setattr(versions_tasks, "AUTO_RETURN_MS", 40)

    page.stack.setCurrentWidget(page.step)
    built.sidebar.setCurrentRow(0)  # the user went to the launch page while the install ran
    app.processEvents()
    page._on_installed((SimpleNamespace(downloaded=1, skipped=0, failed=[]), None))
    assert _pump(app, lambda: False, timeout=0.3) is False  # give the timer every chance to fire
    assert page.stack.currentWidget() is page.step, "a finished install pulled the user back"
    assert built.sidebar.currentRow() == 0, "the navigation rail was moved by a download"
