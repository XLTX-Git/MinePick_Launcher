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

"""Phase 3/4: trigger sources, the one pre-launch gate, and the consent dialog.

Every window here is laid out off screen: the suite's conftest forces the offscreen platform, and the
dialog tests also mark the parent ``WA_DontShowOnScreen`` so the new top-level window is provably kept
off any desktop (the marker, not the platform, is the suite's screen rule).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import ClassVar

import pytest
import shiboken6
from PySide6 import QtGui
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QAccessible, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QWidget

import gui.accessibility_launch as flow
from gui.dialogs import accessibility_dialog as dialog_mod
from gui.dialogs.accessibility_dialog import (
    CHOICE_DISMISSED,
    CHOICE_NO,
    CHOICE_YES,
    AccessibilityDialog,
)
from gui.i18n import TRANSLATIONS, UI_LANGUAGES
from gui.pages.instance_detail import InstanceDetail
from gui.pages.instances_page import InstancesPage
from gui.pages.launch_page import LaunchPage
from gui.widgets import KeyboardFocusTracker
from launcher.accessibility import (
    AccessibilityState,
    TriggerSource,
    VersionTier,
    get_state,
)

HAS_ANNOUNCEMENT = hasattr(QtGui, "QAccessibleAnnouncementEvent")
DIALOG_KEYS = (
    "accessibility.dialog.onboard.title",
    "accessibility.dialog.onboard.body",
    "accessibility.dialog.narrator.title",
    "accessibility.dialog.narrator.body",
    "accessibility.dialog.yes",
    "accessibility.dialog.no",
)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture()
def env(monkeypatch, ws_tmp):
    """A game directory, a separate accessibility-state file, and a mutable fake config."""
    game = ws_tmp / "game"
    game.mkdir()
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    cfg = SimpleNamespace(
        accessibility_preference="auto",
        game_dir=game,
        selected_account="account-1",
        memory_auto=False,
        memory_gb=4.0,
    )
    config_path = ws_tmp / "config.json"
    monkeypatch.setattr(flow.config, "load", lambda: (cfg, config_path))
    monkeypatch.setattr(dialog_mod, "tr", lambda key, *args: key.format(*args) if args else key)
    return SimpleNamespace(game=game, state=ws_tmp / "accessibility.json", cfg=cfg)


def _run_gate(env, *, trigger=TriggerSource.KEYBOARD, instance_id="1.20.1", parent=None, button=None):
    calls: list[str] = []
    flow.prepare_launch_accessibility(
        parent=parent,
        instance_id=instance_id,
        game_dir=env.game,
        trigger_source=trigger,
        state_path=env.state,
        launch_button=button,
        on_continue=lambda: calls.append("launched"),
    )
    return calls


def _state(env, instance_id="1.20.1"):
    return get_state(instance_id, env.state)


class _FakeDialog(QDialog):
    """A dialog double that records its construction and opens through the non-blocking contract."""

    answer = CHOICE_YES
    instances: ClassVar[list[_FakeDialog]] = []

    def __init__(self, parent, tier) -> None:
        super().__init__(parent)
        self.tier = tier
        self.choice: str | None = None
        self._parent_closing = False
        self.opened = False
        _FakeDialog.instances.append(self)

    @property
    def parent_closing(self) -> bool:
        return self._parent_closing

    def open(self) -> None:
        self.opened = True
        if self.answer == CHOICE_YES:
            self.choice = CHOICE_YES
            self.accept()
        elif self.answer == CHOICE_NO:
            self.choice = CHOICE_NO
            self.reject()
        else:
            self.reject()

    def exec(self) -> int:  # pragma: no cover - running it is the failure
        raise AssertionError("the launch gate must use open(), never exec()")


@pytest.fixture()
def fake_dialog(monkeypatch):
    _FakeDialog.instances = []
    _FakeDialog.answer = CHOICE_YES
    monkeypatch.setattr(flow, "AccessibilityDialog", _FakeDialog)
    return _FakeDialog


# ---------------------------------------------------------------- trigger source


def test_public_tracker_accessor_tracks_keyboard_but_does_not_change_the_rule(app):
    tracker = KeyboardFocusTracker()
    widget = QWidget()
    assert tracker.last_input_was_keyboard() is False
    assert tracker.last_key() is None

    tracker.eventFilter(
        widget,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier),
    )
    assert tracker.last_input_was_keyboard() is True
    assert tracker.last_key() == Qt.Key.Key_Space

    tracker.eventFilter(widget, QEvent(QEvent.Type.MouseButtonPress))
    assert tracker.last_input_was_keyboard() is False


def test_enter_counts_as_keyboard_only_for_the_default_button(app, monkeypatch):
    class _Tracker:
        keyboard = True
        key = Qt.Key.Key_Return

        def last_input_was_keyboard(self):
            return self.keyboard

        def last_key(self):
            return self.key

    tracker = _Tracker()
    monkeypatch.setattr(app, "_focus_tracker", tracker, raising=False)

    from PySide6.QtWidgets import QPushButton

    button = QPushButton()
    assert flow.trigger_source_for_click(button) is TriggerSource.UNKNOWN

    button.setDefault(True)
    assert flow.trigger_source_for_click(button) is TriggerSource.KEYBOARD

    button.setDefault(False)
    tracker.key = Qt.Key.Key_Space
    assert flow.trigger_source_for_click(button) is TriggerSource.KEYBOARD

    tracker.keyboard = False
    assert flow.trigger_source_for_click(button) is TriggerSource.MOUSE


def test_mouse_state_reaches_the_gate_as_mouse(app, monkeypatch, env):
    """The entry, not the gate, is what must not turn a mouse launch into a keyboard one."""
    import gui.pages.launch_page as launch_page_mod

    seen: dict = {}

    def fake_gate(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(launch_page_mod, "prepare_launch_accessibility", fake_gate)

    class _Tracker:
        def last_input_was_keyboard(self):
            return False

        def last_key(self):
            return None

    monkeypatch.setattr(app, "_focus_tracker", _Tracker(), raising=False)
    page = LaunchPage()
    try:
        page.version_combo.setEditText("1.20.1")
        page._on_launch_clicked()
        assert seen["trigger_source"] is TriggerSource.MOUSE
    finally:
        page.deleteLater()


def test_space_reaches_the_gate_as_keyboard(app, monkeypatch, env):
    import gui.pages.launch_page as launch_page_mod

    seen: dict = {}

    def fake_gate(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(launch_page_mod, "prepare_launch_accessibility", fake_gate)

    class _Tracker:
        def last_input_was_keyboard(self):
            return True

        def last_key(self):
            return Qt.Key.Key_Space

    monkeypatch.setattr(app, "_focus_tracker", _Tracker(), raising=False)
    page = LaunchPage()
    try:
        page.version_combo.setEditText("1.20.1")
        page._on_launch_clicked()
        assert seen["trigger_source"] is TriggerSource.KEYBOARD
    finally:
        page.deleteLater()


def test_all_three_entries_accept_a_trigger_source():
    for cls, method in (
        (LaunchPage, "launch"),
        (InstanceDetail, "launch"),
        (InstancesPage, "_launch"),
    ):
        parameters = inspect.signature(getattr(cls, method)).parameters
        assert "trigger_source" in parameters, f"{cls.__name__}.{method} cannot receive the source"
        source = inspect.getsource(getattr(cls, method))
        assert "trigger_source" in source


# ---------------------------------------------------------------- decision -> action


def test_write_onboard_calls_ensure_option_and_records_applied(env, fake_dialog):
    env.cfg.accessibility_preference = "always_on"
    calls = _run_gate(env, trigger=TriggerSource.MOUSE)

    assert calls == ["launched"]
    assert (env.game / "options.txt").read_text(encoding="utf-8") == "onboardAccessibility:true\n"
    assert _state(env) is AccessibilityState.APPLIED
    assert fake_dialog.instances == []


def test_write_narrator_calls_ensure_option_and_records_applied(env, fake_dialog):
    env.cfg.accessibility_preference = "always_on"
    calls = _run_gate(env, instance_id="1.12", trigger=TriggerSource.MOUSE)

    assert calls == ["launched"]
    assert (env.game / "options.txt").read_text(encoding="utf-8") == "narrator:1\n"
    assert _state(env, "1.12") is AccessibilityState.APPLIED
    assert fake_dialog.instances == []


def test_write_failure_still_launches_and_records_write_failed(env, monkeypatch, fake_dialog):
    env.cfg.accessibility_preference = "always_on"
    from launcher.launch.options import OptionWriteResult

    monkeypatch.setattr(flow, "ensure_option", lambda *a, **k: OptionWriteResult.FAILED)
    calls = _run_gate(env, trigger=TriggerSource.MOUSE)

    assert calls == ["launched"]
    assert _state(env) is AccessibilityState.WRITE_FAILED
    assert fake_dialog.instances == []


def test_ask_only_appears_for_keyboard_auto_untreated(env, fake_dialog):
    env.cfg.accessibility_preference = "auto"
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD)
    assert fake_dialog.instances, "the keyboard/auto/untreated cell did not ask"
    assert fake_dialog.instances[0].opened is True
    assert calls == ["launched"]
    assert _state(env) is AccessibilityState.ANSWERED_ON
    assert (env.game / "options.txt").read_text(encoding="utf-8") == "onboardAccessibility:true\n"


@pytest.mark.parametrize("trigger", [TriggerSource.MOUSE, TriggerSource.UNKNOWN])
def test_ask_never_appears_for_mouse_or_unknown(env, fake_dialog, trigger):
    env.cfg.accessibility_preference = "auto"
    calls = _run_gate(env, trigger=trigger)

    assert fake_dialog.instances == []
    assert calls == ["launched"]
    assert not (env.game / "options.txt").exists()
    assert _state(env) is None


@pytest.mark.parametrize(
    ("preference", "state"),
    [("never", None), ("auto", AccessibilityState.ANSWERED_OFF)],
)
def test_quiet_decision_never_builds_the_dialog(env, monkeypatch, preference, state):
    env.cfg.accessibility_preference = preference
    if state is not None:
        from launcher.accessibility import save_state

        save_state("1.20.1", state, env.state)

    def explode(*_args, **_kwargs):  # pragma: no cover - construction is the failure
        raise AssertionError("a quiet decision built the consent dialog")

    monkeypatch.setattr(flow, "AccessibilityDialog", explode)
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD)
    assert calls == ["launched"]
    assert not (env.game / "options.txt").exists()


def test_preference_is_reread_on_every_launch(env, fake_dialog):
    """The first decision must not be cached: changing the preference takes effect next launch."""
    env.cfg.accessibility_preference = "auto"
    _run_gate(env, trigger=TriggerSource.MOUSE)
    assert not (env.game / "options.txt").exists()

    env.cfg.accessibility_preference = "always_on"
    calls = _run_gate(env, trigger=TriggerSource.MOUSE)
    assert calls == ["launched"]
    assert (env.game / "options.txt").read_text(encoding="utf-8") == "onboardAccessibility:true\n"
    assert _state(env) is AccessibilityState.APPLIED


def test_a_handled_instance_skips_the_mod_scan(env, monkeypatch):
    env.cfg.accessibility_preference = "auto"
    from launcher.accessibility import save_state

    save_state("1.20.1", AccessibilityState.APPLIED, env.state)

    def no_scan(_mods_dir):  # pragma: no cover - scanning a handled launch is the failure
        raise AssertionError("a handled instance must not pay for a mod scan")

    monkeypatch.setattr(flow, "classify_installed_mods", no_scan)
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD)
    assert calls == ["launched"]


def test_the_mod_scan_happens_when_the_decision_can_still_change(env, fake_dialog, monkeypatch):
    env.cfg.accessibility_preference = "auto"
    seen: list[object] = []

    def scan(mods_dir):
        seen.append(mods_dir)
        return {"disable": [], "override": []}

    monkeypatch.setattr(flow, "classify_installed_mods", scan)
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD)
    assert seen == [env.game / "mods"]
    assert calls == ["launched"]
    assert fake_dialog.instances


def test_a_probe_failure_still_launches(env, monkeypatch):
    env.cfg.accessibility_preference = "auto"

    def broken_scan(_mods_dir):
        raise OSError("the mods directory vanished")

    monkeypatch.setattr(flow, "classify_installed_mods", broken_scan)
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD)

    assert calls == ["launched"]
    assert not (env.game / "options.txt").exists()


def test_a_write_exception_still_launches(env, monkeypatch):
    env.cfg.accessibility_preference = "always_on"

    def broken_write(*_args, **_kwargs):
        raise OSError("options.txt is locked")

    monkeypatch.setattr(flow, "ensure_option", broken_write)
    calls = _run_gate(env, trigger=TriggerSource.MOUSE)
    assert calls == ["launched"]


# ---------------------------------------------------------------- dialog behaviour


def _offscreen_parent(app) -> QWidget:
    parent = QWidget()
    parent.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    parent.show()
    for _ in range(3):
        app.processEvents()
    return parent


def _open_dialog(app, parent, tier=VersionTier.ONBOARD_SCREEN) -> AccessibilityDialog:
    dialog = AccessibilityDialog(parent, tier)
    dialog.open()
    for _ in range(5):
        app.processEvents()
    return dialog


def _dispose(app, *widgets) -> None:
    """Destroy the test's top-levels now, not in the suite's teardown.

    ``finished`` schedules ``deleteLater``; closing a parent can also destroy its dialog child. Doing
    both and letting the suite's teardown walk a list that still names the deleted child is what
    produced an access violation while these tests were being built. Processing the deletion here
    keeps every test's window graph complete before the next test starts.
    """
    for widget in widgets:
        try:
            widget.close()
        except RuntimeError:
            continue
        try:
            widget.deleteLater()
        except RuntimeError:
            continue
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_dialog_inherits_offscreen_marker_before_it_is_shown(app):
    parent = _offscreen_parent(app)
    dialog = _open_dialog(app, parent)
    try:
        assert dialog.isVisible() is True, "the dialog did not open, so this proves nothing"
        assert dialog.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen) is True, (
            "the new dialog dropped the offscreen firewall marker"
        )
    finally:
        _dispose(app, dialog, parent)


def test_dialog_has_default_focus_on_yes(app):
    parent = _offscreen_parent(app)
    dialog = _open_dialog(app, parent)
    try:
        assert dialog.yes_button.isDefault() is True
        assert dialog.focusWidget() is dialog.yes_button
    finally:
        _dispose(app, dialog, parent)


@pytest.mark.parametrize("key", [Qt.Key.Key_Space, Qt.Key.Key_Return])
def test_space_and_enter_both_confirm(app, key):
    parent = _offscreen_parent(app)
    dialog = _open_dialog(app, parent)
    try:
        if key == Qt.Key.Key_Space:
            QTest.keyClick(dialog.yes_button, key)
        else:
            QTest.keyClick(dialog, key)
        assert dialog.choice == CHOICE_YES
    finally:
        _dispose(app, dialog, parent)


def test_x_close_records_dismissed_without_yes(app):
    parent = _offscreen_parent(app)
    dialog = _open_dialog(app, parent)
    try:
        dialog.close()
        assert dialog.choice == CHOICE_DISMISSED
    finally:
        _dispose(app, parent)


def test_dialog_is_deleted_after_it_closes(app):
    parent = _offscreen_parent(app)
    dialog = _open_dialog(app, parent)
    dialog.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert shiboken6.isValid(dialog) is False, "the closed dialog was not deleteLater()'d"
    _dispose(app, parent)


def test_parent_close_takes_the_dialog_with_it_and_does_not_launch(app, env):
    """The dialog is a child window; a closing parent must not be followed by a launch worker."""
    env.cfg.accessibility_preference = "auto"
    parent = _offscreen_parent(app)
    calls: list[str] = []
    flow.prepare_launch_accessibility(
        parent=parent,
        instance_id="1.20.1",
        game_dir=env.game,
        trigger_source=TriggerSource.KEYBOARD,
        state_path=env.state,
        on_continue=lambda: calls.append("launched"),
    )
    for _ in range(4):
        app.processEvents()
    dialogs = [
        widget for widget in QApplication.topLevelWidgets() if isinstance(widget, AccessibilityDialog)
    ]
    assert dialogs, "the test did not reach the dialog"

    parent.close()
    for _ in range(6):
        app.processEvents()

    assert calls == [], "the parent window closed, but a launch worker was still started"
    assert not any(widget is dialogs[0] for widget in QApplication.topLevelWidgets()), (
        "the dialog outlived the parent window"
    )
    _dispose(app, parent)


def test_dialog_announces_title_and_body_but_never_an_alert(app, monkeypatch):
    monkeypatch.delenv("MCLAUNCHER_NO_A11Y_ALERT", raising=False)
    monkeypatch.setattr(QAccessible, "isActive", staticmethod(lambda: True))
    seen: list[tuple[object, object, str]] = []

    def record(event):
        message = ""
        try:
            message = event.message()
        except (AttributeError, RuntimeError):
            pass
        seen.append((event.object(), event.type(), message))

    monkeypatch.setattr(QAccessible, "updateAccessibility", staticmethod(record))
    parent = _offscreen_parent(app)
    dialog = _open_dialog(app, parent)
    try:
        kinds = [kind for _obj, kind, _message in seen]
        assert QAccessible.Event.Alert not in kinds, f"an alert would make Windows chime: {kinds}"
        if HAS_ANNOUNCEMENT:
            messages = [
                message
                for _obj, kind, message in seen
                if kind == QAccessible.Event.Announcement
            ]
            assert dialog._title_text in messages
            assert dialog._body_text in messages
        assert dialog.title_label.accessibleName() == dialog._title_text
        assert dialog.body_label.accessibleName() == dialog._body_text
    finally:
        _dispose(app, dialog, parent)


def test_focus_ring_is_keyboard_only(app):
    tracker = KeyboardFocusTracker(app)
    app.installEventFilter(tracker)
    try:
        tracker._keyboard = False
        parent = _offscreen_parent(app)
        mouse_dialog = _open_dialog(app, parent)
        assert mouse_dialog.focusWidget() is mouse_dialog.yes_button
        assert not bool(mouse_dialog.yes_button.property("keyboardFocus")), (
            "a non-keyboard dialog painted a focus ring"
        )
        _dispose(app, mouse_dialog)

        tracker._keyboard = True
        dialog = _open_dialog(app, parent)
        assert dialog.focusWidget() is dialog.yes_button
        assert bool(dialog.yes_button.property("keyboardFocus")), (
            "the keyboard dialog did not carry the focus ring"
        )
        _dispose(app, dialog, parent)
    finally:
        app.removeEventFilter(tracker)


# ---------------------------------------------------------------- the gate's dialog mapping


def test_x_dismissal_writes_no_option_and_still_launches(app, env, monkeypatch):
    """Negative self-check target: changing X to "yes" must turn this red."""
    env.cfg.accessibility_preference = "auto"
    parent = _offscreen_parent(app)
    calls: list[str] = []
    flow.prepare_launch_accessibility(
        parent=parent,
        instance_id="1.20.1",
        game_dir=env.game,
        trigger_source=TriggerSource.KEYBOARD,
        state_path=env.state,
        on_continue=lambda: calls.append("launched"),
    )
    dialogs = [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, AccessibilityDialog) and widget.isVisible()
    ]
    assert len(dialogs) == 1, f"the gate did not open exactly one dialog: {dialogs!r}"
    dialogs[0].close()
    for _ in range(5):
        app.processEvents()

    assert calls == ["launched"]
    assert not (env.game / "options.txt").exists(), "closing the dialog must not write an option"
    assert _state(env) is AccessibilityState.DISMISSED
    _dispose(app, parent)


def test_the_launch_button_is_disabled_while_the_question_is_open(env, monkeypatch):
    env.cfg.accessibility_preference = "auto"
    button = QPushButton("Launch")
    button.setEnabled(True)
    assert button.isEnabled() is True

    class _RecordingDialog(_FakeDialog):
        def open(self) -> None:
            assert button.isEnabled() is False, "the launch button was live while the dialog was open"
            super().open()

    monkeypatch.setattr(flow, "AccessibilityDialog", _RecordingDialog)
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD, button=button)
    assert calls == ["launched"]
    assert button.isEnabled() is False  # the worker's on_finished is what re-enables it
    button.deleteLater()


def test_dialog_no_records_answered_off(env, fake_dialog):
    env.cfg.accessibility_preference = "auto"
    fake_dialog.answer = CHOICE_NO
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD)
    assert calls == ["launched"]
    assert _state(env) is AccessibilityState.ANSWERED_OFF
    assert not (env.game / "options.txt").exists()


def test_dialog_yes_write_failure_records_write_failed(env, fake_dialog, monkeypatch):
    env.cfg.accessibility_preference = "auto"
    from launcher.launch.options import OptionWriteResult

    monkeypatch.setattr(flow, "ensure_option", lambda *a, **k: OptionWriteResult.FAILED)
    calls = _run_gate(env, trigger=TriggerSource.KEYBOARD)
    assert calls == ["launched"]
    assert _state(env) is AccessibilityState.WRITE_FAILED


# ---------------------------------------------------------------- copy


def test_dialog_copy_exists_in_all_nine_languages():
    for language, _name in UI_LANGUAGES:
        table = TRANSLATIONS[language]
        for key in DIALOG_KEYS:
            assert key in table, f"{language} is missing {key}"
            assert table[key].strip(), f"{language} has an empty {key}"
        assert table["accessibility.dialog.onboard.body"] != table[
            "accessibility.dialog.narrator.body"
        ], f"{language} does not say which feature is being enabled"


def test_dialog_copy_names_the_item_not_just_the_setting():
    from gui import i18n

    original = i18n.current_language()
    try:
        i18n.set_language("zh_cn")
        onboard = dialog_mod.AccessibilityDialog(None, VersionTier.ONBOARD_SCREEN)
        narrator = dialog_mod.AccessibilityDialog(None, VersionTier.NARRATOR_ONLY)
        try:
            assert "引导屏幕" in onboard._title_text and "引导屏幕" in onboard._body_text
            assert "旁白" in narrator._title_text and "旁白" in narrator._body_text
        finally:
            onboard.deleteLater()
            narrator.deleteLater()
    finally:
        i18n.set_language(original)
