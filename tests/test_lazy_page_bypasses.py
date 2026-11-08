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

"""The startup update check and the exit install never build the About page.

``pages.get("about")`` builds on first access, and the window used it at ~2.5s after startup and
again on close - so the page was always constructed, even for a session that never opened it, and
the close path's ``if page is not None`` could never be false. The registry's ``peek`` is the
non-building read; these tests pin that both paths use it and still do their job.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from gui import i18n
from gui.main_window import MainWindow
from launcher import config
from launcher import updates as updates_mod


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh_cn():
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


@pytest.fixture
def window(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    built = MainWindow()
    yield built
    built.close()
    built.deleteLater()
    app.processEvents()


def test_the_startup_check_does_not_build_the_about_page(window, monkeypatch):
    import gui.pages.about_page as about

    started: list[object] = []
    monkeypatch.setattr(
        about, "start_startup_update_check", lambda parent: started.append(parent)
    )

    window._auto_check_updates()

    assert started == [window], "the page-free startup check was not used"
    assert window.pages.peek("about") is None
    assert window.pages.built_keys() == ("launch",)


def test_the_page_free_check_reports_on_the_status_line(window, monkeypatch, app):
    """A real headless check: no page is built and the result still reaches the one status line."""
    import gui.pages.about_page as about

    cfg, cfg_path = config.load()
    cfg.auto_update_mode = "download_notify"
    config.save(cfg, cfg_path)

    def sync_run(fn, *args, on_result=None, on_error=None, on_finished=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - mirror the worker contract
            if on_error is not None:
                on_error(str(exc))
        else:
            if on_result is not None:
                on_result(result)
        finally:
            if on_finished is not None:
                on_finished()

    monkeypatch.setattr(about, "run_in_background", sync_run)
    monkeypatch.setattr(about.updates, "cleanup_backups", lambda *a, **k: None)
    monkeypatch.setattr(about.updates, "load_pending", lambda: None)
    monkeypatch.setattr(
        about.updates,
        "check_for_update",
        lambda *a, **k: updates_mod.UpdateCheck(
            status="up_to_date", current="0.5.0", latest="0.5.0", release=None
        ),
    )

    session = about.StartupUpdateCheck(window)
    session.start()
    app.processEvents()

    assert window.status_label.text() == i18n.tr("about.update.up_to_date")
    assert window.pages.peek("about") is None


def test_close_installs_a_staged_update_without_building_the_about_page(
    window, monkeypatch, ws_tmp, app
):
    cfg, cfg_path = config.load()
    cfg.auto_update_mode = "download_install"
    cfg.window_start_mode = "default"
    config.save(cfg, cfg_path)
    staged = ws_tmp / "staged-update.exe"
    staged.write_bytes(b"MZ")
    script = ws_tmp / "update-helper.cmd"
    script.write_text("", encoding="utf-8")
    launched: list[object] = []

    import gui.pages.about_page as about

    monkeypatch.setattr(about.updates, "load_pending", lambda: (staged, "9.9.9"))
    monkeypatch.setattr(about.updates, "can_self_update", lambda: True)
    monkeypatch.setattr(about.updates, "write_install_script", lambda *a, **k: script)
    monkeypatch.setattr(
        about.updates, "launch_install_script", lambda value: launched.append(value) or True
    )

    assert window.pages.peek("about") is None
    window.close()
    app.processEvents()

    assert launched == [script], "the staged update was not handed to the install helper on exit"
    assert window.pages.peek("about") is None, "closing built the About page"


def test_the_page_still_runs_the_check_when_it_already_exists(window, monkeypatch):
    """The no-build shortcut must not skip the page's own check when the user did open About."""
    import gui.pages.about_page as about

    page = window.pages["about"]  # the user visited it
    called: list[bool] = []
    monkeypatch.setattr(page, "auto_check_on_start", lambda: called.append(True))
    monkeypatch.setattr(about, "start_startup_update_check", lambda parent: called.append(False))

    about_page = window.pages.peek("about")
    assert about_page is page
    window._auto_check_updates()

    assert called == [True]
