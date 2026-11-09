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

"""A text-bearing control may never be laid out smaller than its own text needs.

The defect this pins: ``QPushButton#secondaryButton`` carries ``padding: 8px 16px`` and a 1px border
in the stylesheet, so its ``minimumSizeHint().height()`` is 35px at the 13px UI font. The 联机
banner's 重新检测 button was ``setFixedHeight(26)`` and every 32px "field metric" button was
``setFixedHeight(32)``: a fixed height below the control's own minimum does not shrink the text, it
paints the glyphs into a smaller box and cuts the top and bottom off. Measured: 26px vs 35px for
重新检测, 32px vs 35px for every outlined button, 32px vs 33px for a filled one.

The criterion is the one the controls themselves use, not a hint guessed by the test:

* QPushButton / QComboBox / QLineEdit / QCheckBox / QRadioButton / QGroupBox :
  ``height() >= minimumSizeHint().height()`` - the minimum already contains the stylesheet padding,
  border and font metrics, which is exactly the number the fixed heights ignored;
* QLabel: ``fontMetrics().height() <= height()``. A label has no inner padding of its own; for a
  non-wrapping label the text advance must also fit the width. A word-wrapped label is sized by its
  width, so its natural (unwrapped) width says nothing about clipping and is not compared. Elided
  labels are excluded on the width axis: an ellipsis is their contract, not a defect.

The window is forced smaller than the 1000x610 product floor (``setMinimumSize(0, 0)`` plus the
floor-raising hook disabled) to reproduce the reported 1483-physical-pixel / 150% laptop, whose
988-logical-pixel screen cannot give the window its minimum. The product floor itself is not touched.

A second scenario grows the base font through the stylesheet (``QWidget { font-size: 17px; }``) in a
window tall enough that height is not the constraint. ``app.setFont()`` cannot drive this: the
stylesheet sets an explicit 13px size on every QWidget and wins over the application font (measured).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt, QThreadPool
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
)

from gui import i18n, theme
from gui.main_window import NAV_KEYS, MainWindow
from gui.widgets import ElidedLabel
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance

#: The reported laptop: 1483 physical pixels at 150% scaling = 988 logical pixels. The product floor
#: stays 1000x610; the screen is smaller than the floor, which is why the window is handed 988.
SMALL_SCREENS = ((1000, 610), (988, 599))
#: A font with 20% taller metrics than the 13px default, rendered in a window tall enough that the
#: only thing under test is "does the control grow with its text".
LARGE_FONT_PX = 17
LARGE_FONT_SIZE = (1000, 760)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No network, no Java probe and no startup update check while the window exists."""
    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])
    monkeypatch.setattr("gui.pages.about_page.AboutPage.auto_check_on_start", lambda self: None)
    monkeypatch.setattr(
        "gui.pages.about_page.start_startup_update_check", lambda parent: None
    )


def _make_instance(game, version_id: str, *, mods: int = 0, saves: int = 0) -> None:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version_id}.json").write_text(
        json.dumps({"id": version_id, "mainClass": "net.minecraft.client.main.Main"}),
        encoding="utf-8",
    )
    meta = Instance(id=version_id, display_name=version_id, note="", created_at=1.0)
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )
    for index in range(mods):
        (folder / "mods").mkdir(exist_ok=True)
        (folder / "mods" / f"mod{index}.jar").write_bytes(b"PK\x03\x04")
    for index in range(saves):
        (folder / "saves").mkdir(exist_ok=True)
        (folder / "saves" / f"world{index}").mkdir(exist_ok=True)


def _settle(app, window=None) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(8):
        app.processEvents()


def _make_window(app, monkeypatch, ws_tmp) -> MainWindow:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "mc"
    _make_instance(game, "fabric-loader-0.19.3-1.21.11", mods=3, saves=2)
    _make_instance(game, "1.20.1", saves=2)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")
    window = MainWindow()
    # The real app applies the QSS to the application in create_app(); this suite's QApplication
    # already exists (conftest builds it for the screen firewall). Style **this window** instead:
    # setStyleSheet on the application re-polishes every widget in the process, which reaches widgets
    # earlier test files already deleted and crashed the run inside Qt. The sheet is the same one.
    window.setStyleSheet(theme.stylesheet_text("light"))
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    _settle(app)
    for index in range(len(NAV_KEYS)):
        window.sidebar.setCurrentRow(index)
        _settle(app, window)
    return window


def _force_geometry(window, app, width: int, height: int) -> None:
    """Hand the window a size below the product floor, the way a too-small screen does.

    ``MainWindow._enforce_layout_floor`` is the guard that raises the minimum back to the layout's
    own requirement. On a screen narrower than that requirement Windows still gives the window the
    screen's size, so the test reproduces that state: the floor hook is disabled for this window and
    the minimum is cleared, then the real geometry is applied.
    """
    window._enforce_layout_floor = lambda: None  # type: ignore[method-assign]
    window.setMinimumSize(0, 0)
    window.resize(width, height)
    _settle(app)
    assert (window.width(), window.height()) == (width, height), (
        f"the window did not take the forced {width}x{height} geometry "
        f"(got {window.width()}x{window.height()})"
    )


def _text_of(widget) -> str:
    if isinstance(widget, QLabel):
        return widget.text()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, QAbstractButton):
        return widget.text()
    if isinstance(widget, QGroupBox):
        return widget.title()
    return ""


def _label_is_rich(widget: QLabel) -> bool:
    text = widget.text()
    if widget.textFormat() == Qt.TextFormat.RichText:
        return True
    return "<" in text and ">" in text


def _clip_reason(widget) -> str | None:
    """The acceptance criterion for one widget, or ``None`` when its text fits."""
    text = _text_of(widget).strip()
    if not text or not widget.isVisible():
        return None
    name = f"{type(widget).__name__}({widget.objectName() or '-'})「{text[:18]}」"
    if isinstance(widget, QLabel):
        line = widget.fontMetrics().height()
        if line > widget.height():
            return f"{name} 高 {widget.height()} < 行高 {line}"
        if widget.wordWrap() or isinstance(widget, ElidedLabel) or _label_is_rich(widget):
            return None
        need = widget.fontMetrics().horizontalAdvance(text)
        if need > widget.width():
            return f"{name} 宽 {widget.width()} < 文字 {need}"
        return None
    if isinstance(widget, (QAbstractButton, QComboBox, QLineEdit, QGroupBox)):
        # QSpinBox/QDoubleSpinBox own an internal QLineEdit; it is not a field the page sizes.
        if isinstance(widget, QLineEdit) and isinstance(widget.parentWidget(), QAbstractSpinBox):
            return None
        need = widget.minimumSizeHint().height()
        if widget.height() < need:
            return f"{name} 高 {widget.height()} < minimumSizeHint {need}"
    return None


def _text_widgets(page):
    for cls in (QLabel, QAbstractButton, QComboBox, QLineEdit, QGroupBox):
        yield from page.findChildren(cls)


def _outside_viewport(widget) -> bool:
    """True when a scrolling ancestor doesn't currently show this widget.

    A QListWidget lays its item widgets out lazily: rows below the viewport keep the geometry of
    their last (possibly zero-sized) layout pass until they are scrolled into view. Reading them as
    "clipped" would report the scroll boundary, not a defect. A widget that is fully inside the
    viewport is checked; one that is not is left for the pass that actually shows it.
    """
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            viewport = parent.viewport()
            local = widget.rect().translated(widget.mapTo(viewport, QPoint(0, 0)))
            return not viewport.rect().contains(local)
        parent = parent.parentWidget()
    return False


def _clipped(page) -> list[str]:
    issues = []
    for widget in _text_widgets(page):
        if _outside_viewport(widget):
            continue
        reason = _clip_reason(widget)
        if reason:
            issues.append(reason)
        # A text widget that escapes a clipping ancestor loses its bottom edge even when its own
        # height is correct - the 创建大厅 third step is exactly this case. A scroll area is skipped:
        # content below its viewport is reachable, not lost.
        if widget.isVisible() and isinstance(widget, (QLabel, QAbstractButton, QComboBox, QLineEdit)):
            probe = widget.parentWidget()
            while probe is not None and not isinstance(probe, QAbstractScrollArea):
                probe = probe.parentWidget()
            if probe is None:
                parent = widget.parentWidget()
                while parent is not None and parent is not page:
                    local = widget.rect().translated(widget.mapTo(parent, QPoint(0, 0)))
                    if not parent.rect().contains(local):
                        text = _text_of(widget).strip()
                        issues.append(
                            f"{type(widget).__name__}({widget.objectName() or '-'})"
                            f"「{text[:18]}」 在 {type(parent).__name__}"
                            f"({parent.objectName() or '-'}) 内超出 "
                            f"{parent.width()}x{parent.height()}"
                        )
                        break
                    parent = parent.parentWidget()
    return issues


def _detail_tabs():
    from gui.pages.instance_detail import (
        TAB_DIAGNOSIS,
        TAB_EXPORT,
        TAB_MODIFY,
        TAB_MODS,
        TAB_OVERVIEW,
        TAB_RESOURCEPACKS,
        TAB_SAVES,
        TAB_SCHEMATICS,
        TAB_SCREENSHOTS,
        TAB_SERVERS,
        TAB_SHADERPACKS,
    )

    return (
        ("overview", TAB_OVERVIEW),
        ("mods", TAB_MODS),
        ("resourcepacks", TAB_RESOURCEPACKS),
        ("shaderpacks", TAB_SHADERPACKS),
        ("schematics", TAB_SCHEMATICS),
        ("saves", TAB_SAVES),
        ("screenshots", TAB_SCREENSHOTS),
        ("servers", TAB_SERVERS),
        ("modify", TAB_MODIFY),
        ("export", TAB_EXPORT),
        ("diagnosis", TAB_DIAGNOSIS),
    )


def _scan(app, window) -> list[str]:
    """Visit each page (and each detail tab) while it is the visible one, then measure it."""
    failures = []
    from launcher.link.session import FakeLinkSession, LinkState

    for index, key in enumerate(NAV_KEYS):
        window.sidebar.setCurrentRow(index)
        page = window.pages[key]
        if key == "tools":
            page.set_session(FakeLinkSession(state=LinkState.IDLE))
        _settle(app, window)
        failures.extend(f"{key}: {issue}" for issue in _clipped(page))

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    page = window.pages["instances"]
    assert page.open_instance("fabric-loader-0.19.3-1.21.11")
    detail = page.detail
    for name, index in _detail_tabs():
        detail.set_tab(index)
        _settle(app, window)
        # The whole detail, not only the tab page: the shared sidebar and header are part of every
        # tab and must satisfy the same rule.
        failures.extend(f"instance/{name}: {issue}" for issue in _clipped(detail))
    return failures


def _close(app, window) -> None:
    window.close()
    window.deleteLater()
    _settle(app)
    i18n.set_language("zh_cn")


@pytest.mark.parametrize(("width", "height"), SMALL_SCREENS)
def test_no_text_control_is_smaller_than_its_own_minimum(app, monkeypatch, ws_tmp, width, height):
    """Every page and every detail tab, at a window a 1483px/150% screen can actually give it."""
    window = None
    try:
        window = _make_window(app, monkeypatch, ws_tmp)
        _force_geometry(window, app, width, height)
        failures = _scan(app, window)
        assert not failures, (
            f"[{width}x{height}] {len(failures)} text control(s) are laid out below their own "
            "minimum:\n  " + "\n  ".join(failures)
        )
    finally:
        if window is not None:
            _close(app, window)


def test_text_controls_grow_with_a_larger_base_font(app, monkeypatch, ws_tmp):
    """The same controls must follow the font metrics, not a hard-coded field height.

    ``app.setFont()`` does not reach these widgets: the stylesheet sets an explicit 13px size on
    every QWidget and wins over the application font. Bumping that one declaration through the
    stylesheet is therefore what reproduces a machine whose font is taller than the field metric.
    """
    window = None
    original_sheet = None
    try:
        window = _make_window(app, monkeypatch, ws_tmp)
        _force_geometry(window, app, *LARGE_FONT_SIZE)
        # Style this window, never the application: setStyleSheet on the QApplication re-polishes
        # every widget in the process and reaches the ones earlier test files already destroyed.
        original_sheet = window.styleSheet()
        window.setStyleSheet(original_sheet + f"\nQWidget {{ font-size: {LARGE_FONT_PX}px; }}\n")
        _settle(app)
        failures = _scan(app, window)
        assert not failures, (
            f"[font {LARGE_FONT_PX}px] {len(failures)} text control(s) are laid out below their "
            "own minimum:\n  " + "\n  ".join(failures)
        )
    finally:
        if original_sheet is not None and window is not None:
            window.setStyleSheet(original_sheet)
        if window is not None:
            _close(app, window)


def test_tools_page_does_not_need_a_scrollbar_at_988(app, monkeypatch, ws_tmp):
    """The reported page must fit without a scrollbar at 988x610, not merely clip less.

    The 加入大厅 and 创建大厅 cards, the self-check line and the footer all have to be visible at
    once. A scroll area would hide the same content behind a scrollbar, which is not what the report
    asked for; and the page has no scroll area today, so this also pins that nobody adds one to make
    the clipping test pass.
    """
    window = None
    try:
        window = _make_window(app, monkeypatch, ws_tmp)
        _force_geometry(window, app, 988, 610)
        window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
        page = window.pages["tools"]
        from launcher.link.session import FakeLinkSession, LinkState

        page.set_session(FakeLinkSession(state=LinkState.IDLE))
        _settle(app)
        assert not page.findChildren(QScrollArea) or all(
            area.verticalScrollBar().maximum() == 0
            for area in page.findChildren(QScrollArea)
            if area.isVisible()
        ), "the tools page grew a vertical scrollbar at 988x610"
        need = page.minimumSizeHint().height()
        assert page.height() >= need, (
            f"the tools page is {page.height()}px tall but its layout needs {need}px: the "
            "创建大厅 / 加入大厅 cards or the footer would be clipped"
        )
    finally:
        if window is not None:
            _close(app, window)
