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

"""Thread bridge: run core synchronous functions on QThreadPool, reporting results/progress/errors via signals.

Also includes download rate / remaining time estimation, and the dedicated thread one game process
runs on (see :class:`GameProcessThread`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal, Slot

# The bounded output tail is the runner's own implementation (its decoding policy is shared with the
# crash diagnosis). Reused here rather than copied: a second decoder could drift from the first.
from launcher.launch.runner import TAIL_LINES, _capture_stream, _decode_tail


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


# Set once the application starts shutting down. Worker threads can outlive the interface - a download
# or an update can be minutes long - and a progress callback that reaches a destroyed Qt object aborts
# the whole process ("Fatal Python error: Aborted", with the thread parked in as_completed). Everything
# a worker thread can call into Qt goes through the flag below, so after shutdown begins those calls are
# no-ops by construction rather than by timing.
_shutting_down = False


def begin_shutdown() -> None:
    """Mark the application as shutting down; worker callbacks become inert from here on."""
    global _shutting_down
    _shutting_down = True


def is_shutting_down() -> bool:
    return _shutting_down


def reset_shutdown_for_tests() -> None:
    """Only for tests: the flag is process-global, so a test must be able to clear it."""
    global _shutting_down
    _shutting_down = False


class ProgressBridge(QObject):
    """Callable object: emits a queued signal back to the main thread when called from a worker thread."""

    progress = Signal(object)

    def __call__(self, value: Any) -> None:
        if _shutting_down:
            return
        self.progress.emit(value)


class RateTracker:
    """Estimate download rate and remaining time by sampling done_bytes."""

    def __init__(self) -> None:
        self.last_bytes = 0
        self.last_time: float | None = None
        self.rate_bps = 0.0

    def update(self, done_bytes: int) -> tuple[float, float | None]:
        """Return (rate_bps, eta_seconds); eta is None when there is not enough data."""
        now = time.monotonic()
        if self.last_time is not None and now > self.last_time:
            elapsed = now - self.last_time
            self.rate_bps = (done_bytes - self.last_bytes) / elapsed
        self.last_bytes = done_bytes
        self.last_time = now
        return self.rate_bps, self.eta(done_bytes)

    def eta(self, done_bytes: int) -> float | None:
        if self.rate_bps <= 0:
            return None
        return max(0.0, (self.total_bytes - done_bytes) / self.rate_bps)

    def set_total(self, total_bytes: int) -> None:
        self.total_bytes = total_bytes


def format_rate(rate_bps: float) -> str:
    if rate_bps >= 1 << 20:
        return f"{rate_bps / (1 << 20):.1f} MB/s"
    if rate_bps >= 1 << 10:
        return f"{rate_bps / (1 << 10):.0f} KB/s"
    return f"{rate_bps:.0f} B/s"


def format_eta(eta_seconds: float | None) -> str:
    if eta_seconds is None:
        return "--:--"
    minutes, seconds = divmod(int(eta_seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


class Worker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
            self._safe_emit(self.signals.result, result)
        except Exception as exc:  # noqa: BLE001 - forward uniformly for UI display
            self._safe_emit(self.signals.error, str(exc))
        finally:
            self._safe_emit(self.signals.finished)

    @staticmethod
    def _safe_emit(signal, *args) -> None:
        """Deliver a worker result, unless the interface is going away.

        Every emit a worker makes goes through here, so the shutdown flag is the one place that has to
        be right: once ``begin_shutdown()`` has run, a worker that finishes late cannot reach Qt at all
        - no signal, no queued event, no dereference of a widget being destroyed. The check costs one
        boolean read in normal use. ``RuntimeError`` is still swallowed for the case where a receiver
        was destroyed without a shutdown (a page closed on its own).
        """
        if is_shutting_down():
            return
        try:
            signal.emit(*args)
        except RuntimeError:
            pass


# Strong-reference registry: QRunnable is autoDelete-managed by the thread pool; if the
# Python-side Worker/signals QObject gets GC'd, queued result/error/finished events are dropped.
_alive: set[Worker] = set()


def _guard_owner(callback: Callable[..., Any] | None) -> Callable[..., Any] | None:
    """Skip a callback whose bound owner widget no longer exists.

    A worker result is delivered through a queued connection, so it can arrive after the page
    that asked for it was closed or torn down. Calling the bound method then dereferences a
    freed C++ object: Windows reports it as an access violation inside Qt6Widgets.dll. A plain
    function (no ``__self__``) is passed through untouched.
    """
    if callback is None:
        return None
    owner = getattr(callback, "__self__", None)
    if owner is None or isinstance(owner, type):
        return callback

    from gui.widgets import widget_alive

    def guarded(*args: Any) -> None:
        if not widget_alive(owner):
            return
        callback(*args)

    return guarded


def run_in_background(
    fn: Callable[..., Any],
    *args: Any,
    on_result: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_finished: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Worker:
    """Run ``fn`` on the shared Qt pool; the callbacks arrive on the GUI thread."""
    worker = Worker(fn, *args, **kwargs)
    on_result = _guard_owner(on_result)
    on_error = _guard_owner(on_error)
    on_finished = _guard_owner(on_finished)
    if on_result is not None:
        worker.signals.result.connect(on_result)
    if on_error is not None:
        worker.signals.error.connect(on_error)
    if on_finished is not None:
        worker.signals.finished.connect(on_finished)

    _alive.add(worker)

    def _release() -> None:
        # The finished signal fires on the main thread after all callbacks, so it is safe to release the reference here
        _alive.discard(worker)

    worker.signals.finished.connect(_release)
    QThreadPool.globalInstance().start(worker)
    return worker


#: How often the game thread checks its process and its release flag. Short enough that closing the
#: launcher waits at most this long, long enough that a multi-hour game costs two wakeups a second.
GAME_POLL_INTERVAL_MS = 120
#: How long the game thread waits for its output reader to drain after the process exits.
TAIL_READER_JOIN_SECONDS = 2.0


@dataclass(frozen=True)
class GameRunResult:
    """A finished game process: its exit code, bounded output tail, start time and working directory.

    ``cwd`` is what the crash-report lookup needs, ``started`` what it compares the report mtimes
    against; both are captured here (in the thread) so the GUI callback needs no other state.
    """

    exit_code: int
    tail: list[str]
    started: float
    cwd: Path


class GameProcessThread(QThread):
    """One game process on a thread of its own; it is never started on the shared ``QThreadPool``.

    The pool exists for downloads, installs and other tasks measured in seconds. A game is measured in
    hours, and a ``QRunnable`` parked in ``process.wait()`` for that long was one pool slot gone for
    the rest of the session - after which a download or an update could be starved. This thread is
    created per launch, so the pool's ``activeThreadCount`` never includes the game.

    The alternative was ``QProcess``. It is event-driven and needs no thread, but its destructor kills
    the child process: closing the launcher while a game is running would then terminate the game,
    and "退出启动器" must leave it running (that is what the after-launch "exit" mode promises). This
    thread starts an independent OS process with ``subprocess.Popen`` and only *watches* it; releasing
    the thread stops the watch and leaves the game untouched.
    """

    #: The child is up (no arguments: the page's after-launch slots take none).
    started = Signal()
    #: The child exited normally; carries one :class:`GameRunResult`. Delivered on the GUI thread.
    exited = Signal(object)
    #: The child could not be started at all (missing Java, bad working directory, ...).
    failed = Signal(str)

    def __init__(
        self,
        argv: list[str],
        cwd: Path,
        *,
        capture_tail: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._argv = list(argv)
        self._cwd = Path(cwd)
        self._capture_tail = capture_tail
        #: Set by release(): stop watching, do not touch the process.
        self._release = threading.Event()
        #: The live Popen, kept as an attribute so the process is reachable for the whole run.
        self.process: subprocess.Popen | None = None

    def run(self) -> None:
        started = time.time()
        try:
            process = self._spawn()
        except (OSError, ValueError) as exc:  # missing/unrunnable executable, or an empty argv
            self.failed.emit(str(exc))
            return
        self.process = process
        buffer: deque[bytes] = deque(maxlen=TAIL_LINES)
        reader: threading.Thread | None = None
        if self._capture_tail and process.stdout is not None:
            reader = threading.Thread(
                target=_capture_stream, args=(process.stdout, buffer), daemon=True
            )
            reader.start()
        self.started.emit()
        while True:
            code = process.poll()
            if code is not None:
                if reader is not None:
                    reader.join(timeout=TAIL_READER_JOIN_SECONDS)
                self.exited.emit(GameRunResult(code, _decode_tail(buffer), started, self._cwd))
                return
            if self._release.is_set():
                # The launcher is going away; the game is an independent OS process and keeps
                # running. Returning here is what makes the QThread finish (a QThread destroyed
                # while it is still running aborts the process).
                return
            self.msleep(GAME_POLL_INTERVAL_MS)

    def _spawn(self) -> subprocess.Popen:
        """Start the child with the runner's own console policy (no console flash under a GUI)."""
        extra: dict[str, Any] = {}
        if os.name == "nt" and sys.stdout is None and sys.stdin is None:
            extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if self._capture_tail:
            return subprocess.Popen(
                self._argv,
                cwd=str(self._cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **extra,
            )
        return subprocess.Popen(self._argv, cwd=str(self._cwd), **extra)

    def release(self) -> None:
        """Stop watching the process; it is neither terminated nor waited for."""
        self._release.set()


#: Every game thread that has been started and not yet reported back. Module-level so the thread
#: survives a language switch rebuilding the page, and so the quit path can find it even when the
#: window that started it is gone.
_game_processes: set[GameProcessThread] = set()
_hooked_app: object | None = None


def _forget_game_process(thread: GameProcessThread) -> None:
    _game_processes.discard(thread)


def start_game_process(thread: GameProcessThread) -> GameProcessThread:
    """Start ``thread`` and keep it reachable until it finishes or the app quits.

    One game is normally the only one; the registry exists so a quit can release every watcher even
    after the page that owned it was rebuilt (a language switch) and its Python reference dropped.
    """
    global _hooked_app
    _game_processes.add(thread)
    thread.finished.connect(lambda: _forget_game_process(thread))
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None and _hooked_app is not app:
        # Connected once per application: release and join the watcher before the event loop ends.
        app.aboutToQuit.connect(shutdown_game_processes)
        _hooked_app = app
    thread.start()
    return thread


def shutdown_game_processes(timeout_ms: int = 2000) -> None:
    """Stop watching every running game and join its thread, without killing the game.

    Called on close (the window may close while a game runs) and from ``aboutToQuit`` (the
    after-launch "exit" mode calls ``QApplication.quit()``, not ``window.close()``). Waiting is
    bounded by the poll interval plus scheduling: the threads return after one tick. A QThread
    destroyed while still running aborts the process, which is exactly the shutdown crash this
    avoids; the game itself is an independent process and is left alone.
    """
    threads = list(_game_processes)
    for thread in threads:
        thread.release()  # a thread that has not reached run() yet still sees this flag
    for thread in threads:
        if thread.isRunning():
            thread.wait(timeout_ms)
    for thread in list(_game_processes):
        if not thread.isRunning():
            _game_processes.discard(thread)
