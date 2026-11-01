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

"""The quit confirmation must not be built at all when its window is kept off screen.

Why this file exists. ``gui/main_window.py`` asked "quit while a download is running?" with a
``QMessageBox``. That box was a top-level window of its own, so it did not inherit its parent's
``WA_DontShowOnScreen``: with the parent marked, the box was a real window (the report measured
``box.isVisible() = True``, ``winId = 4262432``, ``testAttribute(WA_DontShowOnScreen) = False``), and it
only appeared while a download was in flight, which is why it was intermittent.

The product now draws that question with the in-window dialog (``gui/inwindow_dialog.py``), which is a
child of the launcher window and cannot become a window of its own. The off-screen branch is kept
anyway, and this file still pins it: off screen there is nobody to ask, and a dialog that waits for a
click nobody can make is a hang rather than a question. Refusing the quit is the same conservative
answer the dialog gives when nothing is clicked.

These tests pin both halves: the off-screen window must skip the question (and must not block), and a
normal window must still ask it, with its text and its safe default, so only the one branch changed.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def asking_quit(monkeypatch, ws_tmp):
    """A real MainWindow with a download in flight, plus a non-blocking stand-in for the dialog.

    ``InWindowDialog.ask`` is the dialog's single blocking entry point (see gui/inwindow_dialog.py), so
    replacing it keeps the product's whole construction path - title, body, buttons, default marker -
    intact and only removes the loop that waits for a click. Every dialog built is recorded, so "was
    the question asked?" is readable after the call.
    """
    from gui import inwindow_dialog
    from gui.main_window import MainWindow
    from launcher.net import downloader

    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setattr(downloader, "active_download_count", lambda: 2)
    assert downloader.active_download_count() == 2, "the download state was not set up"

    made: list[object] = []

    def _ask(dialog):
        made.append(dialog)
        return inwindow_dialog.Outcome.DISMISSED  # "no button clicked": the caller must refuse

    monkeypatch.setattr(inwindow_dialog.InWindowDialog, "ask", _ask)

    window = MainWindow()
    try:
        yield window, made
    finally:
        window.close()
        window.deleteLater()


def test_an_offscreen_window_does_not_build_the_quit_dialog(asking_quit, app, caplog):
    """The marked branch: no dialog, no block, no window - and the skip is logged, never silent."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    window, made = asking_quit
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    for _ in range(4):
        app.processEvents()
    assert window.isVisible() is True, "the window was not shown, so this proves nothing"
    assert window._ancestor_is_kept_off_screen() is True, "the marker was not found on the window itself"

    toplevels_before = len(QApplication.topLevelWidgets())
    with caplog.at_level(logging.INFO, logger="gui.main_window"):
        allowed = window._confirm_quit_while_downloading()

    assert allowed is False, (
        "an off-screen window was allowed to quit: with nobody to answer the question the conservative "
        "answer is to refuse, which is also what the dialog returns when nothing is clicked"
    )
    assert made == [], (
        f"the quit dialog was built for an off-screen window: {made!r}. Off screen there is nobody to "
        f"answer it, so building it can only hang the caller"
    )
    assert len(QApplication.topLevelWidgets()) == toplevels_before, (
        "a new top-level widget appeared during the close path"
    )
    messages = [record.getMessage() for record in caplog.records if record.name == "gui.main_window"]
    assert any("skipped" in message and "WA_DontShowOnScreen" in message for message in messages), (
        f"the skip was not logged, so it is silent: {messages}"
    )
    assert any("2 download" in message for message in messages), (
        f"the log line does not say why: {messages}"
    )


def test_a_marked_parent_above_the_window_is_found(asking_quit):
    """The marker lives on the window that was marked; a lookup that only read the window would miss it."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    window, _made = asking_quit
    assert window._ancestor_is_kept_off_screen() is False, "an unmarked window read as marked"

    parent = QWidget()
    parent.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.setParent(parent, Qt.WindowType.Window)
    try:
        assert window.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen) is False, (
            "the window itself is marked, so this case is not testing the walk up the tree"
        )
        assert window._ancestor_is_kept_off_screen() is True, (
            "a marker on a parent window was not found - the dialog would still be built there"
        )
    finally:
        window.setParent(None)
        parent.deleteLater()


def test_a_normal_window_still_asks_the_question(asking_quit, app):
    """The negative control: an ordinary window keeps the dialog, its text and its safe default."""
    from gui.i18n import tr

    window, made = asking_quit
    window.show()  # not marked: this is what every real user has
    for _ in range(4):
        app.processEvents()
    assert window._ancestor_is_kept_off_screen() is False

    allowed = window._confirm_quit_while_downloading()

    assert len(made) == 1, f"the quit dialog was not built for a normal window: {made!r}"
    dialog = made[0]
    assert dialog.title_label.text() == tr("quit.busy.confirm"), dialog.title_label.text()
    assert dialog.body_label.text() == tr("quit.busy.message").format(2), dialog.body_label.text()
    assert allowed is False, (
        "the stand-in answers 'dismissed', which must refuse the quit - if this is True the product "
        "would quit on an unanswered question"
    )
    labels = [button.text() for button in dialog.buttons]
    assert labels == [tr("quit.busy.cancel"), tr("quit.busy.confirm")], labels
    assert dialog.default_button is not None
    assert dialog.default_button.text() == tr("quit.busy.cancel"), (
        "the safe choice is no longer the default one"
    )


def test_an_offscreen_close_does_not_block(asking_quit, app):
    """Driving the real close path off screen must return, not sit in a modal loop with nobody to answer.

    The stand-in above is deliberately *not* used here: ``ask`` is left alone, so if the off-screen
    branch ever stopped being taken this test would hang rather than pass. It is bounded by the suite's
    own timeout behaviour, which is the point - a hang is the failure being ruled out.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    from gui import inwindow_dialog

    window, _made = asking_quit
    original_exec = QMessageBox.exec
    original_ask = inwindow_dialog.InWindowDialog.ask

    calls: list[str] = []

    def _must_not_run(self) -> int:  # pragma: no cover - running it is the failure
        calls.append("exec")
        return original_exec(self)

    def _must_not_ask(self):  # pragma: no cover - running it is the failure
        calls.append("ask")
        return original_ask(self)

    QMessageBox.exec = _must_not_run
    inwindow_dialog.InWindowDialog.ask = _must_not_ask
    try:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        for _ in range(4):
            app.processEvents()
        window.close()  # the product's own close path, with a download in flight
        for _ in range(4):
            app.processEvents()
    finally:
        QMessageBox.exec = original_exec
        inwindow_dialog.InWindowDialog.ask = original_ask

    assert calls == [], (
        "the off-screen close path entered the dialog: with nobody to answer, that is the modal loop "
        "a person saw as a window on the desktop, and it does not return on its own"
    )
    assert window.isVisible() is True, "the refused close did not leave the window up"
