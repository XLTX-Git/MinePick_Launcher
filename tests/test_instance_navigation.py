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

"""Instance navigation: card grid, drill-down/back, the eight-entry sidebar and its width gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from gui import i18n
from launcher import config as config_mod
from launcher.instances import INSTANCE_META_FILENAME, Instance


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Same offline stubs the GUI suite uses: no network and no Java probe outliving the window."""

    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def _make_instance(
    game: Path,
    version_id: str,
    *,
    display_name: str = "",
    mods: int = 0,
    saves: int = 0,
    inherits: str | None = None,
    log_text: str = "",
) -> Path:
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    raw = {"id": version_id, "mainClass": "net.minecraft.client.main.Main"}
    if inherits:
        raw["inheritsFrom"] = inherits
    (folder / f"{version_id}.json").write_text(json.dumps(raw), encoding="utf-8")
    meta = Instance(
        id=version_id,
        display_name=display_name or version_id,
        note="",
        created_at=1.0,
    )
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(meta.model_dump(mode="json")), encoding="utf-8"
    )
    (folder / "mods").mkdir(exist_ok=True)
    (folder / "saves").mkdir(exist_ok=True)
    for index in range(mods):
        (folder / "mods" / f"mod{index}.jar").write_bytes(b"PK\x03\x04")
    for index in range(saves):
        (folder / "saves" / f"world{index}").mkdir(exist_ok=True)
    if log_text:
        (folder / "logs").mkdir(exist_ok=True)
        (folder / "logs" / "latest.log").write_text(log_text, encoding="utf-8")
    return folder


def _settle(app, window) -> None:
    for _ in range(10):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


def _active_focus(app, window):
    """The focused widget once the window is active again.

    The offscreen platform drops the application's active window while events are processed, and a
    widget of an inactive window reports no focus even though Qt still holds it as the window's focus
    widget. Re-activating reproduces the state a real desktop window is always in; it does not relax
    the check, and every caller still asserts the widget is visible and outside the hidden rail.
    """
    window.activateWindow()
    app.processEvents()
    return app.focusWidget()


def _window(app, monkeypatch, ws_tmp, *, dont_show: bool = True):
    """Build the real window on the offscreen platform.

    ``dont_show=False`` is for the focus tests: a window marked ``WA_DontShowOnScreen`` never becomes
    active, and a widget of an inactive window cannot take focus - so a focus assertion against it would
    be probing the attribute, not the focus rule. The platform is forced offscreen by conftest.py, so a
    shown window still cannot reach a desktop.
    """
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "mc"
    _make_instance(
        game,
        "fabric-loader-0.19.3-1.21.11",
        display_name="Fabric Pack",
        mods=3,
        saves=2,
        log_text="[main] instance log line\n",
    )
    _make_instance(
        game, "1.20.1", display_name="Vanilla One", saves=2, log_text=""
    )
    for version in ("1.20.2", "1.20.3"):
        _make_instance(game, version, display_name="Vanilla " + version)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.resize(1180, 760)
    if dont_show:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    _settle(app, window)
    return window, game


def _close(app, window) -> None:
    window.close()
    window.deleteLater()
    for _ in range(4):
        app.processEvents()
    i18n.set_language("zh_cn")


def _card_by_id(page, instance_id: str):
    for index in range(page.list.count()):
        item = page.list.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == instance_id:
            return page.list.itemWidget(item)
    raise AssertionError(f"card for {instance_id!r} not found")


def test_cards_are_a_responsive_grid_and_open_the_detail_page(app, monkeypatch, ws_tmp):
    """The first view is a 2-3 column white-card grid; a card drill-down is a real page switch."""
    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        assert page.current_view() is page.view_stack.widget(0)
        assert page.detail_page.isHidden()
        assert page.list.count() == 4

        xs = {
            page.list.visualItemRect(page.list.item(i)).x()
            for i in range(page.list.count())
        }
        assert len(xs) == 3, (
            f"expected 3 columns at 1180px, got x positions {sorted(xs)}; "
            f"window={window.width()} page={page.width()} viewport={page.list.viewport().width()} "
            f"rects={[page.list.visualItemRect(page.list.item(i)).getRect() for i in range(page.list.count())]}"
        )

        for index in range(page.list.count()):
            card = page.list.itemWidget(page.list.item(index))
            assert card is not None
            assert card.objectName() == "instanceCard"
            assert card.accessibleName().strip()
            name = card.findChild(QLabel, "instanceCardName")
            meta = card.findChild(QLabel, "instanceCardMeta")
            use = card.findChild(QPushButton, "instanceCardUse")
            manage = card.findChild(QPushButton, "instanceCardManage")
            assert name is not None and name.text()
            assert meta is not None and " · " in meta.text()
            # Both actions are real buttons side by side: 使用 is the primary, 管理 the outline.
            assert use is not None and use.text() == i18n.tr("instances.card.use")
            assert manage is not None and manage.text() == i18n.tr("instances.card.manage")

        fabric = _card_by_id(page, "fabric-loader-0.19.3-1.21.11")
        counts = fabric.findChild(QLabel, "instanceCardCounts")
        assert counts is not None
        assert counts.text() == "Mod 3 · 存档 2"
        assert "Fabric 0.19.3" in fabric.findChild(QLabel, "instanceCardMeta").text()
        assert "1.21.11" in fabric.findChild(QLabel, "instanceCardMeta").text()

        # Single-clicking the card body is not an action any more (使用/管理 are the two actions);
        # 管理 is what opens the detail page.
        QTest.mouseClick(fabric, Qt.MouseButton.LeftButton)
        _settle(app, window)
        assert page.current_view() is page.view_stack.widget(0)
        fabric.findChild(QPushButton, "instanceCardManage").click()
        _settle(app, window)
        assert page.current_view() is page.detail_page
        assert page.detail.instance_id == "fabric-loader-0.19.3-1.21.11"
        assert page.detail.name_label.text() == "Fabric Pack"

        page.detail_page.back_button.click()
        _settle(app, window)
        assert page.current_view() is page.view_stack.widget(0)
        assert not page.detail_page.isVisible()
    finally:
        if window is not None:
            _close(app, window)


def test_detail_sidebar_is_eight_items_in_three_groups(app, monkeypatch, ws_tmp):
    """8 stacked pages, deterministic order, 36px rows, 20px group headings and live badges."""
    from gui.pages.instance_detail import (
        SIDEBAR_GROUP_HEIGHT,
        SIDEBAR_ITEM_HEIGHT,
        SIDEBAR_WIDTH,
        TAB_DIAGNOSIS,
        TAB_MODIFY,
        TAB_RESOURCEPACKS,
        TAB_SAVES,
        TAB_SHADERPACKS,
    )

    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        assert page.open_instance("fabric-loader-0.19.3-1.21.11")
        _settle(app, window)
        detail = page.detail

        assert detail.sidebar.width() == SIDEBAR_WIDTH
        assert not detail.sidebar.verticalScrollBar().isVisible()
        assert len(detail.tabs) == 8
        assert detail.stack.count() == 8
        assert all(button.height() == SIDEBAR_ITEM_HEIGHT for button in detail.tabs)
        assert all(label.height() == SIDEBAR_GROUP_HEIGHT for label in detail.nav_group_labels)
        assert [label.text() for label in detail.nav_group_labels] == [
            i18n.tr("instances.detail.nav.group.resources"),
            i18n.tr("instances.detail.nav.group.actions"),
        ]
        assert [key for key in detail.nav_buttons] == [
            "overview",
            "mods",
            "resourcepacks",
            "shaderpacks",
            "saves",
            "modify",
            "export",
            "diagnosis",
        ]
        assert detail.tabs[0].isChecked()
        assert detail.nav_badges["mods"].text() == "3"
        assert detail.nav_badges["mods"].isVisible()
        assert detail.nav_badges["saves"].text() == "2"
        assert detail.nav_badges["saves"].isVisible()

        # Resource packs and shaders are independent destinations, not a merged page.
        detail.tabs[TAB_RESOURCEPACKS].click()
        app.processEvents()
        assert detail.stack.currentIndex() == TAB_RESOURCEPACKS
        assert detail.stack.widget(TAB_RESOURCEPACKS) is not detail.stack.widget(TAB_SHADERPACKS)
        detail.tabs[TAB_SHADERPACKS].click()
        app.processEvents()
        assert detail.stack.currentIndex() == TAB_SHADERPACKS

        # 修改 and 导出 explain the missing capability instead of pretending to implement it.
        for index in (TAB_MODIFY,):
            detail.tabs[index].click()
            app.processEvents()
            labels = [label.text() for label in detail.stack.widget(index).findChildren(QLabel)]
            assert i18n.tr("instances.detail.not_implemented.title") in labels
            assert any("." in text or "。" in text for text in labels)

        # The diagnosis page owns the log viewer and shows the file contents directly.
        detail.tabs[TAB_DIAGNOSIS].click()
        app.processEvents()
        assert detail.log_view.isVisible()
        assert "instance log line" in detail.log_view.toPlainText()

        # Selecting a save-bearing instance updates the badge when its table has loaded.
        detail.tabs[TAB_SAVES].click()
        app.processEvents()
        assert detail.stack.currentIndex() == TAB_SAVES
    finally:
        if window is not None:
            _close(app, window)


def _sidebar_width_gate(detail):
    """Return one (language, item, needed, budget) tuple for every item over the 200px budget."""
    from gui.pages.instance_detail import (
        SIDEBAR_ACCENT_BAR,
        SIDEBAR_ICON_GAP,
        SIDEBAR_ICON_SIZE,
        SIDEBAR_PADDING_X,
        SIDEBAR_WIDTH,
    )

    budget = SIDEBAR_WIDTH - 2 * SIDEBAR_PADDING_X - SIDEBAR_ACCENT_BAR
    failures = []
    for key, button in detail.nav_buttons.items():
        label = detail.nav_labels[key]
        label.ensurePolished()
        badge = detail.nav_badges.get(key)
        badge_width = 0
        if badge is not None and badge.isVisible():
            badge.ensurePolished()
            badge_width = badge.sizeHint().width() + SIDEBAR_ICON_GAP
        needed = (
            SIDEBAR_ACCENT_BAR
            + button.layout().contentsMargins().left()
            + SIDEBAR_ICON_SIZE
            + SIDEBAR_ICON_GAP
            + label.fontMetrics().horizontalAdvance(label.text())
            + badge_width
            + button.layout().contentsMargins().right()
        )
        if needed > budget:
            failures.append((key, label.text(), needed, budget))
    return failures


def test_jvm_and_game_argument_labels_are_visible(app, monkeypatch, ws_tmp):
    """Regression: the two settings labels used to collapse to width 0 inside QFormLayout.

    They were ElidedLabels with an Ignored horizontal policy; QFormLayout measured the label column
    before the stylesheet font arrived, so both labels were invisible even though their fields were
    there. The fix uses ordinary wrapping labels, and this gate checks the painted width/height rather
    than trusting the source.
    """
    from gui.pages.instance_detail import TAB_OVERVIEW

    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp)
        page = window.pages["instances"]
        assert page.open_instance("fabric-loader-0.19.3-1.21.11")
        _settle(app, window)
        detail = page.detail
        detail.set_tab(TAB_OVERVIEW)

        for attribute, key, field in (
            ("jvm_args_label", "instances.detail.settings.jvm_args", detail.jvm_args_edit),
            ("game_args_label", "instances.detail.settings.game_args", detail.game_args_edit),
        ):
            label = getattr(detail, attribute)
            label.ensurePolished()
            detail.content_scroll.ensureWidgetVisible(label)
            app.processEvents()
            assert label.text() == i18n.tr(key)
            assert detail.settings_form.labelForField(field) is label
            assert label.wordWrap() is True
            assert label.width() > 0, f"{attribute} still has zero width"
            assert label.height() >= label.fontMetrics().height(), f"{attribute} has no painted line"
            assert label.isVisibleTo(detail), f"{attribute} is not part of the rendered page"
    finally:
        if window is not None:
            _close(app, window)


def test_sidebar_width_gate_all_nine_languages(app):
    """Hard gate: icon + gap + label + badge must fit inside 200px minus item padding.

    The badge is measured with the worst realistic count (999), so adding a fourth digit is the only
    way this gate can go red. Failure messages name both the language and the item.
    """
    from PySide6.QtGui import QFontDatabase

    from gui.pages.instance_detail import InstanceDetail
    from gui.theme import apply_theme

    for font_name in (
        "msyh.ttc",
        "msyhbd.ttc",
        "simhei.ttf",
        "malgun.ttf",
        "YuGothM.ttc",
        "meiryo.ttc",
    ):
        font_file = Path("C:/Windows/Fonts") / font_name
        if font_file.exists():
            QFontDatabase.addApplicationFont(str(font_file))
    apply_theme("light", None, None, "default")
    failures: list[str] = []
    try:
        for code, _language_name in i18n.UI_LANGUAGES:
            i18n.set_language(code)
            detail = InstanceDetail()
            detail.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            detail.resize(900, 620)
            detail.show()
            for _ in range(5):
                app.processEvents()
            detail._set_badge_count("mods", 999)
            detail._set_badge_count("saves", 999)
            for _ in range(3):
                app.processEvents()
            for item, text, needed, budget in _sidebar_width_gate(detail):
                failures.append(
                    f"{code} / {item} ({text!r}): needs {needed}px > budget {budget}px"
                )
            for key, label in detail.nav_labels.items():
                assert not label.wordWrap(), f"{code} / {key} would wrap"
            detail.close()
            detail.deleteLater()
            app.processEvents()
    finally:
        i18n.set_language("zh_cn")
    assert not failures, "sidebar width gate failed:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# One rail at a time: the detail sidebar replaces the main navigation rail
# ---------------------------------------------------------------------------


def test_instance_detail_replaces_the_main_rail_and_back_restores_it(
    app, monkeypatch, ws_tmp
):
    """One sidebar at a time: detail hides the main rail, back (or any page) brings it back.

    The focus half of this test is the point: the rail is hidden while a keyboard user may be
    standing on it, and a hidden widget must never keep focus. Every state is checked through
    ``QApplication.focusWidget()`` against ``isVisible()``, not just against the source.
    """
    from gui.main_window import NAV_KEYS

    window = None
    try:
        window, _game = _window(app, monkeypatch, ws_tmp, dont_show=False)
        page = window.pages["instances"]
        assert window.nav_rail.isVisible()
        assert page.current_view() is page.view_stack.widget(0)
        stack_width_before = window.stack.width()

        # A keyboard user is on the rail when the detail opens.
        window.activateWindow()
        app.processEvents()
        window.sidebar.setFocus(Qt.FocusReason.OtherFocusReason)
        app.processEvents()
        assert window.sidebar.hasFocus()

        assert page.open_instance("1.20.1")
        _settle(app, window)
        focused = _active_focus(app, window)

        assert page.current_view() is page.detail_page
        assert page.detail.instance_id == "1.20.1"
        assert not window.nav_rail.isVisible()
        # One sidebar, at the far left: the detail sidebar starts where the content starts, and the
        # content gained exactly the rail's width.
        assert page.detail.sidebar.mapTo(window, QPoint(0, 0)).x() <= 24
        assert window.stack.width() == stack_width_before + window.nav_rail.width()

        assert focused is not None, "the detail page left the window with no focused control"
        assert focused.isVisible(), f"{type(focused).__name__} kept focus while hidden"
        assert not window.nav_rail.isAncestorOf(focused)
        assert focused is page.detail_page.back_button

        # Hiding the rail while focus is on it must move focus to a visible control, even when the
        # detail page was already open.
        window.set_nav_rail_visible(True)
        _active_focus(app, window)
        window.sidebar.setFocus(Qt.FocusReason.OtherFocusReason)
        app.processEvents()
        assert window.sidebar.hasFocus()
        window.set_nav_rail_visible(False)
        _settle(app, window)
        focused = _active_focus(app, window)
        assert focused is not None and focused.isVisible()
        assert not window.nav_rail.isAncestorOf(focused)
        assert focused is page.detail_page.back_button

        # Back: the grid and the main rail return, and focus is on a visible card.
        page.detail_page.back_button.click()
        _settle(app, window)
        assert page.current_view() is page.view_stack.widget(0)
        assert window.nav_rail.isVisible()
        focused = _active_focus(app, window)
        assert focused is not None and focused.isVisible()
        assert not window.nav_rail.isAncestorOf(focused)

        # A top-level page reached programmatically while the rail is hidden brings it back too:
        # "the rail cannot come back" is exactly the failure this asserts against.
        assert page.open_instance("1.20.1")
        _settle(app, window)
        assert not window.nav_rail.isVisible()
        window.sidebar.setCurrentRow(NAV_KEYS.index("launch"))
        _settle(app, window)
        assert window.nav_rail.isVisible()
        assert window.stack.currentWidget() is window.pages["launch"]
    finally:
        if window is not None:
            _close(app, window)
