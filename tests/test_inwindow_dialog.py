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

"""The in-window dialogs: three forms, three outcomes, one focus trap.

What this file pins, and why each piece needs a test rather than a screenshot:

* the **outcomes**: a dismissal is not a cancel, and neither is an accept. Every dialog that replaced a
  ``QMessageBox``/``QInputDialog`` now answers with :class:`Outcome`, so the distinction has to be real;
* the **keyboard**: Esc dismisses, Enter activates the default button, Tab cycles inside the card and
  *cannot* reach the page behind the backdrop. The trap is asserted twice - once by walking the cycle,
  once with a focusable page widget behind the dialog that must never receive focus;
* the **window-ness**: the dialog is a child widget of the launcher window, not a window of its own, so
  it cannot appear on the desktop and cannot be focused behind the launcher;
* the **call-site budget**: no ``QMessageBox`` is called anywhere in ``gui/`` any more, and exactly one
  ``QInputDialog`` is left (the pre-window password prompt in ``gui/main.py``, which has no window to
  draw in). Counted from the AST, so a docstring that mentions the class name cannot hide a call;
* the **behaviour of the converted sites**: the empty-string, cancel and default-button rules each old
  call site relied on.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from gui import inwindow_dialog
from gui.inwindow_dialog import (
    InWindowDialog,
    InWindowInput,
    InWindowMessage,
    Outcome,
)

ROOT = Path(__file__).resolve().parent.parent
GUI = ROOT / "gui"

#: The real blocking entry point, captured before the suite's autouse fixture replaces it (see
#: tests/conftest.py: no test may enter a modal loop by accident).
_REAL_ASK = InWindowDialog.ask


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture()
def host(app):
    """A plain shown window to draw in; one per test, so "the visible dialog" is unambiguous."""
    window = QWidget()
    window.resize(900, 620)
    layout = QVBoxLayout(window)
    layout.addWidget(QLineEdit("page field"))
    window.show()
    app.processEvents()
    try:
        yield window
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def _show(app, dialog: InWindowDialog) -> InWindowDialog:
    dialog.show()
    for _ in range(3):
        app.processEvents()
    return dialog


def _dialog_of(host: QWidget) -> InWindowDialog:
    """The one visible in-window dialog under ``host``."""
    found = [child for child in host.findChildren(InWindowDialog) if child.isVisible()]
    assert len(found) == 1, f"expected exactly one open dialog, found {len(found)}"
    return found[0]


def _focus_inside(dialog: InWindowDialog) -> bool:
    focused = QApplication.focusWidget()
    return focused is not None and dialog.card.isAncestorOf(focused)


# ---------------------------------------------------------------- the three forms


def test_prompt_form_has_one_button_and_returns_accepted(app, host):
    """Form 1: title + body + one 「好」; activating it accepts."""
    dialog = _show(app, InWindowMessage(host, "提示", "配置已保存。"))
    assert dialog.title_label.text() == "提示"
    assert dialog.body_label.text() == "配置已保存。"
    assert [button.text() for button in dialog.buttons] == [inwindow_dialog.tr("dialog.ok")]
    assert dialog.default_button is dialog.buttons[0]

    QTest.mouseClick(dialog.buttons[0], Qt.MouseButton.LeftButton)
    app.processEvents()
    assert dialog.outcome is Outcome.ACCEPTED
    assert not dialog.isVisible()


def test_confirm_form_separates_cancel_from_dismissal(app, host):
    """Form 2: 取消 is REJECTED, Esc and a backdrop click are DISMISSED - three outcomes, not two."""
    cancel_dialog = _show(
        app,
        InWindowMessage(
            host,
            "删除实例？",
            "无法撤销。",
            buttons=[
                (inwindow_dialog.tr("dialog.cancel"), Outcome.REJECTED),
                (inwindow_dialog.tr("dialog.confirm"), Outcome.ACCEPTED),
            ],
        ),
    )
    assert [button.text() for button in cancel_dialog.buttons] == [
        inwindow_dialog.tr("dialog.cancel"),
        inwindow_dialog.tr("dialog.confirm"),
    ]
    QTest.mouseClick(cancel_dialog.buttons[0], Qt.MouseButton.LeftButton)
    app.processEvents()
    assert cancel_dialog.outcome is Outcome.REJECTED

    escape_dialog = _show(
        app,
        InWindowMessage(
            host,
            "删除实例？",
            "无法撤销。",
            buttons=[
                (inwindow_dialog.tr("dialog.cancel"), Outcome.REJECTED),
                (inwindow_dialog.tr("dialog.confirm"), Outcome.ACCEPTED),
            ],
        ),
    )
    QTest.keyClick(escape_dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert escape_dialog.outcome is Outcome.DISMISSED

    backdrop_dialog = _show(
        app,
        InWindowMessage(
            host,
            "删除实例？",
            "无法撤销。",
            buttons=[
                (inwindow_dialog.tr("dialog.cancel"), Outcome.REJECTED),
                (inwindow_dialog.tr("dialog.confirm"), Outcome.ACCEPTED),
            ],
        ),
    )
    # A click outside the card is a dismissal; a click *on* the card must not be one.
    card = backdrop_dialog.card.geometry()
    QTest.mouseClick(backdrop_dialog, Qt.MouseButton.LeftButton, pos=card.center())
    app.processEvents()
    assert backdrop_dialog.isVisible(), "a click on the card dismissed the dialog"
    QTest.mouseClick(backdrop_dialog, Qt.MouseButton.LeftButton, pos=_corner(backdrop_dialog))
    app.processEvents()
    assert backdrop_dialog.outcome is Outcome.DISMISSED


def _corner(dialog: InWindowDialog):
    """A point inside the dialog but outside the card (the card is centred)."""
    from PySide6.QtCore import QPoint

    card = dialog.card.geometry()
    x = 4 if card.left() > 8 else dialog.width() - 4
    return QPoint(x, max(4, card.top() // 2))


def test_input_form_returns_what_was_typed(app, host):
    """Form 3: the field, its prefilled value, and (text, ok) through the wrapper."""
    dialog = _show(app, InWindowInput(host, "重命名", "新名称", value="旧名字"))
    assert isinstance(dialog.input_widget, QLineEdit)
    assert dialog.text == "旧名字"
    assert [button.text() for button in dialog.buttons] == [
        inwindow_dialog.tr("dialog.cancel"),
        inwindow_dialog.tr("dialog.confirm"),
    ]
    dialog.input_widget.setText("新名字")
    QTest.keyClick(dialog.input_widget, Qt.Key.Key_Return)
    app.processEvents()
    assert dialog.outcome is Outcome.ACCEPTED
    assert dialog.text == "新名字"


def test_input_form_supports_a_multiline_note(app, host):
    """The note prompt is free text: Enter inserts a newline there, it does not accept."""
    from PySide6.QtWidgets import QPlainTextEdit

    dialog = _show(app, InWindowInput(host, "备注", "写下备注", multiline=True))
    assert isinstance(dialog.input_widget, QPlainTextEdit)
    dialog.set_text("第一行")
    before = dialog.text
    QTest.keyClick(dialog.input_widget, Qt.Key.Key_Return)
    app.processEvents()
    assert dialog.isVisible(), "Enter closed a multiline editor instead of adding a line"
    assert dialog.text.count("\n") == before.count("\n") + 1, "Enter did not insert a newline"
    QTest.keyClick(dialog.buttons[1], Qt.Key.Key_Space)  # the confirm button
    app.processEvents()
    assert dialog.outcome is Outcome.ACCEPTED


# ---------------------------------------------------------------- keyboard


def test_escape_dismisses_and_enter_accepts(app, host):
    """The two keys the spec names, on controls that do not handle them themselves."""
    for key, expected in ((Qt.Key.Key_Escape, Outcome.DISMISSED), (Qt.Key.Key_Return, Outcome.ACCEPTED)):
        dialog = _show(
            app,
            InWindowMessage(
                host,
                "确认",
                "继续吗？",
                buttons=[
                    (inwindow_dialog.tr("dialog.cancel"), Outcome.REJECTED),
                    (inwindow_dialog.tr("dialog.confirm"), Outcome.ACCEPTED),
                ],
            ),
        )
        QTest.keyClick(dialog, key)
        app.processEvents()
        assert dialog.outcome is expected, f"{key} gave {dialog.outcome!r}"


def test_enter_activates_the_default_button(app, host):
    """With cancel as the default (the safe choice), Enter must cancel - not confirm."""
    dialog = _show(
        app,
        InWindowMessage(
            host,
            "退出？",
            "仍有下载任务。",
            buttons=[
                (inwindow_dialog.tr("dialog.cancel"), Outcome.REJECTED),
                (inwindow_dialog.tr("dialog.confirm"), Outcome.ACCEPTED),
            ],
            default=Outcome.REJECTED,
        ),
    )
    assert dialog.default_button is dialog.buttons[0]
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    app.processEvents()
    assert dialog.outcome is Outcome.REJECTED


def test_tab_and_shift_tab_wrap_inside_the_card(app, host):
    """Tab from the last control lands on the first, Shift+Tab from the first on the last."""
    dialog = _show(
        app,
        InWindowInput(host, "重命名", "新名称", value="x"),
    )
    field, cancel, confirm = dialog.input_widget, dialog.buttons[0], dialog.buttons[1]
    assert dialog._focus_chain() == [field, cancel, confirm]

    QTest.keyClick(field, Qt.Key.Key_Tab)
    app.processEvents()
    assert QApplication.focusWidget() is cancel
    QTest.keyClick(cancel, Qt.Key.Key_Tab)
    app.processEvents()
    assert QApplication.focusWidget() is confirm
    QTest.keyClick(confirm, Qt.Key.Key_Tab)
    app.processEvents()
    assert QApplication.focusWidget() is field, "Tab did not wrap to the first control"
    QTest.keyClick(field, Qt.Key.Key_Backtab)  # Shift+Tab
    app.processEvents()
    assert QApplication.focusWidget() is confirm, "Shift+Tab did not wrap to the last control"


def test_tab_never_reaches_the_page_behind_the_backdrop(app, host):
    """The trap: the page field behind the backdrop stays unreachable however many Tabs are pressed."""
    behind = host.findChild(QLineEdit)
    behind.setFocus()
    app.processEvents()
    assert QApplication.focusWidget() is behind

    dialog = _show(
        app,
        InWindowMessage(
            host,
            "确认",
            "继续吗？",
            buttons=[
                (inwindow_dialog.tr("dialog.cancel"), Outcome.REJECTED),
                (inwindow_dialog.tr("dialog.confirm"), Outcome.ACCEPTED),
            ],
        ),
    )
    assert _focus_inside(dialog)

    seen = set()
    for _ in range(12):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        app.processEvents()
        focused = QApplication.focusWidget()
        assert focused is not behind, "Tab escaped the dialog and reached the page behind it"
        assert _focus_inside(dialog), f"focus left the card: {focused!r}"
        seen.add(focused)
    assert len(seen) == 2, f"the cycle should visit both buttons, saw {seen!r}"


def test_the_focus_ring_is_drawn_for_keyboard_focus(app, host):
    """A Tab-focused button carries the property the stylesheet draws the ring from."""
    from gui.widgets import KeyboardFocusTracker

    tracker = KeyboardFocusTracker(QApplication.instance())
    QApplication.instance().installEventFilter(tracker)
    try:
        dialog = _show(
            app,
            InWindowMessage(
                host,
                "确认",
                "继续吗？",
                buttons=[
                    (inwindow_dialog.tr("dialog.cancel"), Outcome.REJECTED),
                    (inwindow_dialog.tr("dialog.confirm"), Outcome.ACCEPTED),
                ],
            ),
        )
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        app.processEvents()
        focused = QApplication.focusWidget()
        assert focused in dialog.buttons
        assert focused.property("keyboardFocus") is True, "the focus ring property was not set"
    finally:
        QApplication.instance().removeEventFilter(tracker)


# ---------------------------------------------------------------- window-ness / a11y


def test_the_dialog_is_a_child_of_the_window_and_not_a_window(app, host):
    """The proof that this is drawn in the window: no window of its own, covering the parent."""
    before = set(QApplication.topLevelWidgets())
    dialog = _show(app, InWindowMessage(host, "提示", "正文"))
    assert dialog.isWindow() is False
    assert dialog.parentWidget() is host
    assert dialog.geometry() == host.rect()
    assert set(QApplication.topLevelWidgets()) == before, "the dialog created a top-level window"
    # And it is painted into the window: a grab of the host contains the card, a grab of the host
    # without the dialog does not.
    with_dialog = host.grab().toImage()
    dialog.hide()
    app.processEvents()
    without = host.grab().toImage()
    assert with_dialog != without, "the dialog is not painted into the window at all"


def test_every_control_has_an_accessible_name(app, host):
    """WCAG 4.1.2 for the card itself: every control a keyboard reaches is named."""
    dialog = _show(app, InWindowInput(host, "重命名", "新名称", value="x"))
    assert dialog.accessibleName() == "重命名"
    assert dialog.card.accessibleName() == "重命名"
    controls = [
        widget
        for widget in dialog.findChildren(QWidget)
        if widget.focusPolicy() & Qt.FocusPolicy.TabFocus
    ]
    assert len(controls) == 3, [type(widget).__name__ for widget in controls]
    for control in controls:
        assert control.accessibleName().strip(), f"{type(control).__name__} has no accessible name"


def test_severity_changes_the_badge_and_the_primary_button(app, host):
    """information / warning / critical stay distinguishable: different badge, different primary."""
    names = {}
    pixmaps = {}
    for severity in ("information", "warning", "critical"):
        dialog = _show(app, InWindowMessage(host, "标题", "正文", severity=severity))
        primary = dialog.buttons[0]
        names[severity] = primary.objectName()
        pixmaps[severity] = inwindow_dialog.severity_pixmap(severity).toImage()
        assert primary.property("defaultChoice") is True
        dialog._finish(Outcome.DISMISSED)
        app.processEvents()
    assert names == {
        "information": "primaryButton",
        "warning": "warningButton",
        "critical": "dangerButton",
    }
    assert len({bytes(image.constBits()) for image in pixmaps.values()}) == 3, (
        "the severity badges are not visually distinct"
    )


def test_a_long_message_scrolls_instead_of_growing_the_card(app, host):
    """A long error stays inside the window: the body is capped and scrolls, the buttons stay up."""
    long_text = "很长的错误信息。" * 400
    dialog = _show(app, InWindowMessage(host, "导出失败", long_text, severity="warning"))
    assert dialog.card.height() <= host.height() - 2 * inwindow_dialog.CARD_MARGIN
    assert dialog.card.geometry().top() >= inwindow_dialog.CARD_MARGIN
    assert dialog.card.geometry().bottom() <= host.height() - inwindow_dialog.CARD_MARGIN
    for button in dialog.buttons:
        assert button.geometry().bottom() <= dialog.card.height(), "a button was pushed out of the card"


def test_the_dialog_does_not_change_the_windows_minimum_width(app, monkeypatch, ws_tmp):
    """A dialog drawn inside the window must not widen the window's own floor."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        for _ in range(6):
            app.processEvents()
        before = window.minimumSizeHint().width()
        dialog = InWindowMessage(
            window, "删除这个实例？", "很长的确认文本。" * 40, severity="critical"
        )
        dialog.show()
        for _ in range(6):
            app.processEvents()
        after = window.minimumSizeHint().width()
        assert after == before, f"the in-window dialog changed the window floor: {before} -> {after}"
        dialog._finish(Outcome.DISMISSED)
        app.processEvents()
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
            app.processEvents()


# ---------------------------------------------------------------- the call-site budget


def _calls_in_gui(class_name: str) -> list[tuple[str, int]]:
    """Every ``ClassName(...)``/``ClassName.method(...)`` call in gui/, from the syntax tree."""
    found: list[tuple[str, int]] = []
    for path in sorted(GUI.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = None
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                name = target.value.id
            if name == class_name:
                found.append((path.relative_to(ROOT).as_posix(), node.lineno))
    return found


def test_no_qmessagebox_call_is_left_in_the_ui():
    """13 QMessageBox call sites became 0 (measured: 14 - 11 static + 3 constructed)."""
    calls = _calls_in_gui("QMessageBox")
    assert calls == [], f"QMessageBox is still called: {calls}"


def test_only_the_pre_window_password_prompt_still_uses_qinputdialog():
    """6 QInputDialog call sites became 1: the one in gui/main.py that runs before the window exists."""
    calls = _calls_in_gui("QInputDialog")
    assert len(calls) == 1, f"expected exactly one QInputDialog call, found {calls}"
    path, _line = calls[0]
    assert path == "gui/main.py", f"the remaining QInputDialog is not the pre-window one: {path}"
    source = (ROOT / path).read_text(encoding="utf-8")
    # ... and it carries the reason, so nobody "finishes the job" by converting it too.
    assert "Deliberately the only QInputDialog left in the app" in source
    assert "no window to draw in yet" in source


def test_no_widget_file_imports_qmessagebox_any_more():
    """The import is as telling as the call: a leftover would invite the next one."""
    offenders = []
    for path in sorted(GUI.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:from PySide6\.QtWidgets import[^)\n]*\bQMessageBox\b|QMessageBox,)\s*$", text, re.MULTILINE):
            offenders.append(f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, match.start()) + 1}")
    assert offenders == [], f"QMessageBox is still imported: {offenders}"


# ---------------------------------------------------------------- the converted sites


def test_question_returns_false_for_both_no_and_dismissal(app, host, monkeypatch):
    """The old code asked ``!= Yes``; a dismissal must therefore behave like No."""

    def _dismiss(self):
        self._outcome = Outcome.DISMISSED
        self.reject()
        return Outcome.DISMISSED

    monkeypatch.setattr(InWindowDialog, "ask", _dismiss)
    assert inwindow_dialog.question(host, "删除", "确定？") is False

    def _accept(self):
        self._outcome = Outcome.ACCEPTED
        self.accept()
        return Outcome.ACCEPTED

    monkeypatch.setattr(InWindowDialog, "ask", _accept)
    assert inwindow_dialog.question(host, "删除", "确定？") is True


def test_question_keeps_the_callers_default_button(app, host, monkeypatch):
    """``question(default_yes=False)`` puts the safe answer on Enter, which is what the old boxes did."""
    seen = {}

    def _enter(self):
        self.show()
        QApplication.processEvents()
        seen[self.body_label.text()] = self.default_button.text()
        QTest.keyClick(self, Qt.Key.Key_Return)
        return self.outcome

    monkeypatch.setattr(InWindowDialog, "ask", _enter)
    assert inwindow_dialog.question(host, "删除", "破坏性操作") is False
    assert inwindow_dialog.question(host, "装 Java", "友好的操作", default_yes=True) is True
    assert seen["破坏性操作"] == inwindow_dialog.tr("dialog.no")
    assert seen["友好的操作"] == inwindow_dialog.tr("dialog.yes")


def test_get_text_returns_the_text_and_a_flag_for_every_end(app, host, monkeypatch):
    """``get_text`` is ``QInputDialog.getText``: (text, ok), cancel and dismissal both not ok."""
    results = []

    def _type_then_close(close):
        def _ask(self):
            self.show()
            QApplication.processEvents()
            self.set_text("打好的字")
            close(self)
            QApplication.processEvents()
            return self.outcome

        return _ask

    monkeypatch.setattr(
        InWindowDialog, "ask", _type_then_close(lambda dialog: QTest.mouseClick(dialog.buttons[1], Qt.MouseButton.LeftButton))
    )
    results.append(inwindow_dialog.get_text(host, "改名", "新名称"))
    monkeypatch.setattr(
        InWindowDialog, "ask", _type_then_close(lambda dialog: QTest.mouseClick(dialog.buttons[0], Qt.MouseButton.LeftButton))
    )
    results.append(inwindow_dialog.get_text(host, "改名", "新名称"))
    monkeypatch.setattr(
        InWindowDialog, "ask", _type_then_close(lambda dialog: QTest.keyClick(dialog, Qt.Key.Key_Escape))
    )
    results.append(inwindow_dialog.get_text(host, "改名", "新名称"))
    assert results == [("打好的字", True), ("打好的字", False), ("打好的字", False)]


def test_a_dismissed_dialog_is_not_a_cancel(app, host):
    """The three-outcome contract: ``Outcome`` is what makes the distinction available to callers."""
    dialog = _show(app, InWindowMessage(host, "确认", "继续吗？"))
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert dialog.outcome is Outcome.DISMISSED
    assert dialog.outcome is not Outcome.REJECTED


def test_errors_module_opens_a_critical_card(app, host, monkeypatch):
    """``gui/errors.py`` is the entry point for every error: it must build the critical card."""
    from gui import errors, i18n

    seen = []

    def _ask(self):
        seen.append(self)
        return Outcome.DISMISSED

    monkeypatch.setattr(InWindowDialog, "ask", _ask)
    errors.show_fatal(host, "启动失败：找不到 Java")
    assert len(seen) == 1
    dialog = seen[0]
    assert dialog.title_label.text() == i18n.tr("error.title")
    assert dialog.body_label.text() == "启动失败：找不到 Java"
    assert dialog.buttons[0].objectName() == "dangerButton"

    errors.show_warning(host, "这只是提醒")
    assert len(seen) == 2
    assert seen[1].title_label.text() == i18n.tr("error.warning.title")
    assert seen[1].buttons[0].objectName() == "warningButton"


def test_show_fatal_without_a_window_does_not_crash(monkeypatch):
    """A fatal error before any window exists must not raise (and must not open an OS window)."""
    from gui import errors

    dialogs = []
    monkeypatch.setattr(InWindowDialog, "ask", lambda self: dialogs.append(self) or Outcome.DISMISSED)
    errors.show_fatal(None, "no window yet")
    assert dialogs == [], "a dialog was asked to block with no window behind it"


def test_rename_keeps_the_empty_name_rule(app, monkeypatch, ws_tmp):
    """The old rename ignored an empty name or a cancel; a real rename still happens otherwise."""
    import json

    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "mc"
    folder = game / "versions" / "1.20.1"
    folder.mkdir(parents=True)
    (folder / "1.20.1.json").write_text(json.dumps({"id": "1.20.1"}), encoding="utf-8")

    from launcher import config as config_mod

    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)

    from gui.main_window import MainWindow
    from launcher.instances import list_instances

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        page.refresh()
        page.select_instance("1.20.1")
        app.processEvents()
        detail = page.detail
        assert detail.instance_id == "1.20.1"

        for answer, ok in (("", True), ("   ", True), ("1.20.1", True), ("新名字", False)):
            monkeypatch.setattr(
                "gui.inwindow_dialog.get_text", lambda *a, _t=answer, _ok=ok, **k: (_t, _ok)
            )
            detail.rename()
            app.processEvents()
            assert sorted(list_instances(game)) == ["1.20.1"], (
                f"{answer!r} with ok={ok} renamed the instance"
            )

        monkeypatch.setattr("gui.inwindow_dialog.get_text", lambda *a, **k: ("新名字", True))
        detail.rename()
        app.processEvents()
        assert sorted(list_instances(game)) == ["新名字"]
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
            app.processEvents()


def test_password_prompts_keep_their_rules(app, monkeypatch, ws_tmp):
    """Empty, mismatched and cancelled passwords must all come back as None - as before."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["settings"]
        answers = []
        monkeypatch.setattr(
            "gui.inwindow_dialog.get_text", lambda *a, **k: answers.pop(0)
        )
        answers[:] = [("", True)]
        assert page._prompt_new_password() is None, "an empty password was accepted"
        answers[:] = [("abc", True), ("abd", True)]
        assert page._prompt_new_password() is None, "a password mismatch was accepted"
        answers[:] = [("abc", False)]
        assert page._prompt_new_password() is None, "a cancelled password was accepted"
        answers[:] = [("abc", True), ("abc", True)]
        assert page._prompt_new_password() == "abc"
        answers[:] = [("", True)]
        assert page._prompt_current_password() is None
        answers[:] = [("secret", False)]
        assert page._prompt_current_password() is None
        answers[:] = [("secret", True)]
        assert page._prompt_current_password() == "secret"
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
            app.processEvents()
