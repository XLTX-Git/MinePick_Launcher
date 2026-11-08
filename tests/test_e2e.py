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

"""E2E tests: pytest + offscreen GUI, simulating UI interactions to verify core flows.

Covers: offline login integration, multi-account switching, mod search (respx-mocked Modrinth),
the version delete flow (instances page, mocked confirmation dialog), launch-page JVM arg persistence,
the launch flow (prepare -> run -> exit code), crash report viewer.
"""

import faulthandler
import json
import os
import sys
import time
from types import SimpleNamespace
from typing import Self

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from launcher import config as config_mod
from launcher.auth import AccountStore, create_offline_account


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline_manifest_fetch(monkeypatch):
    """The versions page auto-fetches the manifest + the resources page loads trending: changed to fail silently in tests to avoid real network requests."""
    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)


def _wait_until(app, cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class _HangGuard:
    """Turn an unanswered modal dialog into a test failure instead of a stuck runner.

    A blocking ``QMessageBox.exec()`` (or any window-modal dialog) runs a nested Qt event loop,
    and a ``QTimer`` still fires inside that loop. On expiry the guard records which dialog is
    open, dumps every thread's stack, and closes it so control returns to the test; ``__exit__``
    then fails with the dialog's name. Without this guard the launch tests wait forever in
    ``show_fatal()`` or in the accessibility consent dialog, and CI hangs instead of failing.

    The guard covers the class of hang the launch flow can produce (a dialog nobody answers).
    A hang with no event loop running (for example a non-Qt deadlock) is outside its reach.
    """

    def __init__(self, seconds: float = 20.0) -> None:
        self._seconds = seconds
        self._modal: str | None = None
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self._timer.start(int(seconds * 1000))

    def _expire(self) -> None:
        widget = QApplication.activeModalWidget()
        if widget is None:
            self._modal = "no active modal widget"
        else:
            self._modal = f"{type(widget).__name__}: {widget.windowTitle()!r}"
            widget.close()
        faulthandler.dump_traceback()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._timer.stop()
        if self._modal is not None:
            pytest.fail(
                f"launch test exceeded {self._seconds:g}s with a modal dialog open "
                f"({self._modal}); the dialog was closed so the runner fails instead of hanging"
            )
        return False


def _install_launch_instance(ws_tmp, cfg, cfg_path, version: str = "1.20.1"):
    """Create the profile the launch flow resolves before its (faked) worker starts.

    ``launch_page.launch()`` calls ``resolve_instance()`` on the main thread before
    ``prepare_launch``; without ``versions/<id>/<id>.json`` that raises and
    ``_on_launch_error`` opens a blocking ``QMessageBox.critical``.
    """
    game = ws_tmp / "mc"
    folder = game / "versions" / version
    folder.mkdir(parents=True)
    (folder / f"{version}.json").write_text(json.dumps({"id": version}), encoding="utf-8")
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    return game


SEARCH_RAW = {
    "hits": [
        {
            "project_id": "AANobbMI",
            "slug": "sodium",
            "title": "Sodium",
            "description": "渲染优化",
            "downloads": 123456,
        }
    ]
}


def test_e2e_offline_login_updates_launch_page(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    # offline mode gate: mark as already verified first
    _cfg, _p = config_mod.load()
    _cfg.offline_unlocked = True
    config_mod.save(_cfg, _p)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["account"]
        page.offline_edit.setText("Steve")
        page.offline_login()
        app.processEvents()
        cfg, _ = config_mod.load()
        accounts = AccountStore().load()
        assert cfg.selected_account in accounts
        # integration: the launch page shows which account is active (switching lives on this page)
        assert "Steve" in window.pages["launch"].account_label.text()
        # the account list shows this account
        assert page.accounts_list.count() == 1
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_e2e_account_switch(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data2"))
    store = AccountStore()
    first = create_offline_account("Alice")
    second = create_offline_account("Bob")
    store.save({first.id: first, second.id: second})
    cfg, cfg_path = config_mod.load()
    cfg.selected_account = first.id
    config_mod.save(cfg, cfg_path)

    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["account"]
        assert page.accounts_list.count() == 2
        # select the second account and switch
        row = next(
            i for i in range(page.accounts_list.count())
            if page.accounts_list.item(i).data(Qt.UserRole) == second.id
        )
        page.accounts_list.setCurrentRow(row)
        page.switch_account()
        app.processEvents()
        cfg, _ = config_mod.load()
        assert cfg.selected_account == second.id
        assert "Bob" in window.pages["launch"].account_label.text()
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_e2e_mods_search_flow(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data3"))
    # worker threads are unaffected by the respx contextvar: inject a thread-safe MockTransport
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SEARCH_RAW)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "launcher.mods.modrinth._client",
        lambda: httpx.Client(transport=transport, headers={"User-Agent": "e2e"}),
    )
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        tab = window.pages["mods"].mods_tab
        tab.search_edit.setText("sodium")
        QTest.mouseClick(tab.search_button, Qt.LeftButton)  # simulate clicking search
        assert _wait_until(app, lambda: tab.list.count() >= 1)
        assert "Sodium" in tab.list.item(0).text()
        assert "123,456" in tab.list.item(0).text()  # download count shown
        assert "渲染优化" in tab.desc_label.text()
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_e2e_version_delete_via_instances_page(app, monkeypatch, ws_tmp):
    """打开实例 hands the profile over to the instances page, where 删除 removes it."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data4"))
    game = ws_tmp / "mc"
    vdir = game / "versions" / "1.20.1"
    vdir.mkdir(parents=True)
    (vdir / "1.20.1.json").write_text(json.dumps({"id": "1.20.1"}), encoding="utf-8")
    (vdir / "1.20.1.jar").write_bytes(b"jar")
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    # simulate clicking "Yes" in the confirmation dialog, which gui/inwindow_dialog.py draws
    monkeypatch.setattr("gui.inwindow_dialog.question", lambda *a, **k: True)

    from gui.main_window import NAV_KEYS, MainWindow
    from launcher.instances import list_instances
    from launcher.meta import ManifestVersion

    window = None
    try:
        window = MainWindow()
        page = window.pages["versions"]
        page._on_manifest(
            SimpleNamespace(
                versions=[ManifestVersion(id="1.20.1", type="release", url="https://x/1.20.1.json", time="", release_time="2023-06-07")],
                latest={},
            )
        )
        # The page opens with every fold closed; this flow drives a row, so open its fold.
        page.expand_group("release")
        assert page.list.count() == 4 + 1  # four fold headers + the one release row
        assert page.status_of("1.20.1") == "已安装"
        page.list.setCurrentRow(page._row_index("1.20.1"))
        # 打开实例: the versions page only jumps, the instances page owns the entry
        page.open_instance_selected()
        assert window.sidebar.currentRow() == NAV_KEYS.index("instances")
        instances = window.pages["instances"]
        assert instances._current_name() == "1.20.1"

        instances.detail.delete()
        assert _wait_until(app, lambda: not vdir.exists())
        assert list_instances(game) == {}
        # the card's status refreshes once the versions page re-reads the installed set
        page.refresh_installed()
        assert page.status_of("1.20.1") == "—"
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_e2e_launch_uses_configured_jvm_args(app, monkeypatch, ws_tmp):
    """Custom JVM arguments are a launcher setting now: the launch page no longer has a field,
    it must pick the configured value up and hand it to the launcher."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data5"))
    _cfg, _p = config_mod.load()
    _cfg.offline_unlocked = True
    _cfg.jvm_args = "-XX:+UseG1GC"
    _install_launch_instance(ws_tmp, _cfg, _p)

    # The accessibility consent gate reads its per-instance state through the real state API.
    # Recording this instance as handled keeps the gate silent even for a keyboard launch,
    # which is the trigger that would otherwise open the (unanswered, hence blocking) dialog.
    from launcher.accessibility import (
        AccessibilityState,
        TriggerSource,
        get_state,
        save_state,
    )

    assert save_state("1.20.1", AccessibilityState.ANSWERED_OFF)
    assert get_state("1.20.1") is AccessibilityState.ANSWERED_OFF

    import gui.pages.launch_page as launch_page_mod
    from launcher.launch import JavaMissingError

    seen: dict = {}

    def fake_prepare(*args, **kwargs):
        seen.update(kwargs)
        raise JavaMissingError(8)

    monkeypatch.setattr(launch_page_mod, "prepare_launch", fake_prepare)
    # the Java-download offer answers "No"; the card is drawn by gui/inwindow_dialog.py
    monkeypatch.setattr("gui.inwindow_dialog.question", lambda *a, **k: False)

    from gui.main_window import MainWindow

    window = None
    with _HangGuard():
        try:
            window = MainWindow()
            page = window.pages["launch"]
            page.set_version_id("1.20.1")
            page.launch(TriggerSource.KEYBOARD)
            app.processEvents()
            assert _wait_until(app, lambda: "已取消" in window.status_label.text())
            assert "-XX:+UseG1GC" in (seen.get("jvm_args") or "")
        finally:
            if window is not None:
                window.close()
                app.processEvents()


def test_e2e_launch_uses_the_instances_direct_connect_server(app, monkeypatch, ws_tmp):
    """Direct connect is instance data now: the launch page has no field, but must still pass it on.

    The field used to live behind an "advanced" disclosure on the launch page. It moved to the
    per-instance settings (instances page) because the server belongs to the instance, and the launch
    page has to keep honouring it - losing it silently would be the one regression that matters (the
    feature would still be configured, just never used). ``prepare_launch`` is the seam: whatever lands
    in its ``server`` argument is what becomes ``--server`` on the command line.
    """
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data5b"))
    _cfg, _p = config_mod.load()
    _cfg.offline_unlocked = True
    game = _install_launch_instance(ws_tmp, _cfg, _p)

    from launcher.instances import resolve_instance, set_instance_settings

    set_instance_settings("1.20.1", game_dir=game, server="mc.example.com")
    assert resolve_instance("1.20.1", _cfg, game).server == "mc.example.com"

    from launcher.accessibility import AccessibilityState, TriggerSource, save_state

    assert save_state("1.20.1", AccessibilityState.ANSWERED_OFF)

    import gui.pages.launch_page as launch_page_mod
    from launcher.launch import JavaMissingError

    seen: dict = {}

    def fake_prepare(*args, **kwargs):
        seen.clear()
        seen.update(kwargs)
        raise JavaMissingError(8)

    monkeypatch.setattr(launch_page_mod, "prepare_launch", fake_prepare)
    # the Java-download offer answers "No"; the card is drawn by gui/inwindow_dialog.py
    monkeypatch.setattr("gui.inwindow_dialog.question", lambda *a, **k: False)

    from gui.main_window import MainWindow

    window = None
    with _HangGuard():
        try:
            window = MainWindow()
            page = window.pages["launch"]
            page.set_version_id("1.20.1")
            page.launch(TriggerSource.KEYBOARD)
            app.processEvents()
            assert _wait_until(app, lambda: "已取消" in window.status_label.text())
            assert seen.get("server") == "mc.example.com"

            # An empty value means "join no server", and that is how the instance clears it.
            set_instance_settings("1.20.1", game_dir=game, server="")
            page.launch(TriggerSource.KEYBOARD)
            app.processEvents()
            assert _wait_until(app, lambda: seen.get("server") is None)
        finally:
            if window is not None:
                window.close()
                app.processEvents()


def test_e2e_crash_viewer_lists_reports(app, ws_tmp):
    game = ws_tmp / "mc2"
    reports = game / "crash-reports"
    reports.mkdir(parents=True)
    (reports / "crash-2024-01-01_00.00.00-client.txt").write_text("boom", encoding="utf-8")

    from gui.crash_viewer import CrashViewerDialog

    dialog = CrashViewerDialog(game)
    assert dialog.list.count() == 1
    assert "boom" in dialog.viewer.toPlainText()
    dialog.close()

def test_e2e_launch_flow_runs_and_reports_exit(app, monkeypatch, ws_tmp):
    """Simulates the full launch flow (prepare -> run -> exit code) with no log panel in the page."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data6"))
    _cfg, _p = config_mod.load()
    _cfg.offline_unlocked = True
    _cfg.after_launch_behavior = "keep"  # hide/exit paths are covered by test_gui
    cwd = _install_launch_instance(ws_tmp, _cfg, _p)

    import gui.pages.launch_page as launch_page_mod

    argv = ["java", "-version"]
    run_calls = []  # (argv, cwd, kwargs)
    real_thread = launch_page_mod.GameProcessThread

    def make_thread(thread_argv, thread_cwd, **kwargs):
        run_calls.append((thread_argv, thread_cwd, kwargs))
        # The game itself is a real, short-lived process: the thread's exit result is what this test
        # verifies, not a fake return value. Exit 7 stands in for the game's exit code.
        return real_thread(
            [sys.executable, "-u", "-c", "import sys; sys.exit(7)"], thread_cwd, **kwargs
        )

    def fake_prepare(*args, **kwargs):
        return SimpleNamespace(
            command=SimpleNamespace(argv=argv, cwd=cwd),
            account=SimpleNamespace(username="Steve"),
            version=SimpleNamespace(id="1.20.1"),
            java=SimpleNamespace(major=17),
            isolated=False,
        )

    monkeypatch.setattr(launch_page_mod, "prepare_launch", fake_prepare)
    monkeypatch.setattr(launch_page_mod, "GameProcessThread", make_thread)

    from gui.main_window import MainWindow

    window = None
    with _HangGuard():
        try:
            window = MainWindow()
            page = window.pages["launch"]
            page.set_version_id("1.20.1")
            page.launch()
            # the prepared command really reaches the dedicated game thread
            assert _wait_until(app, lambda: bool(run_calls))
            assert run_calls[0][0] == argv
            assert run_calls[0][1] == cwd
            assert run_calls[0][2].get("capture_tail") is True
            # the exit code lands on the window's single status line and the button becomes usable again
            assert _wait_until(app, lambda: "退出码: 7" in window.status_label.text())
            assert _wait_until(app, lambda: page.launch_button.isEnabled())
            # the game-log panel is gone: neither the widget nor its tailing timer exists
            assert not hasattr(page, "log_view")
            assert not hasattr(page, "_log_timer")
            # the page keeps no status line of its own either: launch feedback goes to the window
            assert not hasattr(page, "status")
        finally:
            if window is not None:
                window.close()
                app.processEvents()

