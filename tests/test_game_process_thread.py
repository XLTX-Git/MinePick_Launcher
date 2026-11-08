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

"""A game process runs on a thread of its own, never on the shared QThreadPool.

The pool has no thread-count floor for the GUI: downloads, installs, the font scan and the
instance scan all share it. A game lasts hours, so a pool slot parked in ``process.wait()`` for
that long is a slot every later task loses - the F3 defect. The dedicated watcher also has to
survive the launcher: closing the window stops watching it, but must not kill the game, and no
QThread may be left running (a QThread destroyed while running aborts the process).
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThread, QThreadPool
from PySide6.QtWidgets import QApplication

from gui import i18n, workers


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn():
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


def _wait_until(app, predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.01)
    return bool(predicate())


def _python(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def _terminate(thread: workers.GameProcessThread) -> None:
    process = thread.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - a test teardown must never hang
            process.kill()


def test_game_process_uses_its_own_thread_not_the_shared_pool(app, ws_tmp):
    pool = QThreadPool.globalInstance()
    baseline = pool.activeThreadCount()
    thread = workers.GameProcessThread(_python("print('hello'); import time; time.sleep(0.5)"), ws_tmp)
    results: list[workers.GameRunResult] = []
    delivered_on: list[QThread] = []

    def on_exit(result):
        results.append(result)
        delivered_on.append(QThread.currentThread())

    thread.exited.connect(on_exit)
    workers.start_game_process(thread)
    try:
        assert _wait_until(app, lambda: bool(results)), "the game result never reached the GUI thread"
        assert pool.activeThreadCount() <= baseline, (
            "the game occupied a shared-pool thread: "
            f"{pool.activeThreadCount()} active vs {baseline} before"
        )
        assert results[0].exit_code == 0
        assert results[0].tail == ["hello"]
        assert results[0].cwd == ws_tmp
        assert delivered_on[0] is app.thread(), "the result was not delivered on the GUI thread"
        assert _wait_until(app, lambda: not thread.isRunning())
    finally:
        _terminate(thread)


def test_shutdown_stops_watching_without_killing_the_game(app, ws_tmp):
    thread = workers.GameProcessThread(_python("import time; time.sleep(30)"), ws_tmp)
    workers.start_game_process(thread)
    try:
        assert _wait_until(app, lambda: thread.process is not None and thread.isRunning())
        process = thread.process

        workers.shutdown_game_processes()

        assert not thread.isRunning(), "the watcher thread survived shutdown (a zombie thread)"
        assert process.poll() is None, "shutdown killed the game; the game is an independent process"
    finally:
        _terminate(thread)


def test_the_launch_page_uses_the_dedicated_thread_and_a_close_leaves_the_game_running(
    app, monkeypatch, ws_tmp
):
    from launcher import config

    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    cfg, cfg_path = config.load()
    cfg.after_launch_behavior = "keep"  # no auto-hide/auto-quit while the test watches the thread
    cfg.auto_crash_analysis = False
    config.save(cfg, cfg_path)

    from gui.main_window import MainWindow

    window = None
    thread = None
    try:
        window = MainWindow()
        page = window.pages["launch"]
        pool = QThreadPool.globalInstance()
        baseline = pool.activeThreadCount()
        prepared = SimpleNamespace(
            command=SimpleNamespace(argv=_python("import time; time.sleep(30)"), cwd=ws_tmp),
            version=SimpleNamespace(id="1.20.1"),
            java=SimpleNamespace(major=17),
            account=SimpleNamespace(username="Steve"),
            isolated=False,
        )
        resolved = SimpleNamespace(game_dir=ws_tmp, launch_dir=ws_tmp, instance=None)

        page._on_prepared(("ok", prepared, resolved))

        thread = page._game_thread
        assert isinstance(thread, workers.GameProcessThread), (
            f"the game went to {type(thread).__name__}, not the dedicated thread"
        )
        assert _wait_until(app, lambda: thread.process is not None and thread.isRunning())
        assert pool.activeThreadCount() <= baseline

        window.close()
        app.processEvents()

        assert not thread.isRunning(), "closing the window left the game watcher thread running"
        assert page._game_thread is None
        assert thread.process is not None and thread.process.poll() is None, (
            "closing the launcher terminated the game"
        )
    finally:
        if thread is not None:
            _terminate(thread)
        if window is not None:
            window.deleteLater()
        app.processEvents()
