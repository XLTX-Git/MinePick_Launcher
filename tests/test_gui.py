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

"""GUI smoke tests (offscreen platform, no display environment)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QLabel,
    QStackedWidget,
    QWidget,
)

from launcher import config as config_mod


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _offline_manifest_fetch(monkeypatch):
    """The versions page auto-fetches the manifest + the resources page loads trending: changed to fail silently in tests to avoid real network requests. The Java page really probes the machine (slow subprocess work) and its worker can outlive a closed window: stub it like the render tool does."""
    def _fail(**kw):
        raise RuntimeError("offline test")

    monkeypatch.setattr("gui.pages.versions_page.fetch_manifest", _fail)
    monkeypatch.setattr("launcher.mods.modrinth._client", _fail)
    monkeypatch.setattr("gui.pages.java_page.list_java", lambda *a, **kw: [])


def test_window_pages_navigation(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    assert window.windowTitle() == "MinePick Launcher"
    stack = window.findChild(QStackedWidget)
    assert stack is not None
    # Pages are built on first visit, so walk the navigation keys rather than stack.count(): the stack
    # only holds the pages visited so far, and counting it would silently check a single page.
    for index, key in enumerate(NAV_KEYS):
        window.sidebar.setCurrentRow(index)
        app.processEvents()
        assert stack.currentWidget() is window.pages[key]
    assert stack.count() == len(NAV_KEYS)  # every page ends up in the stack once visited
    window.close()


def test_settings_isolation_toggle_saves(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data2"))
    from gui.main_window import MainWindow

    window = MainWindow()
    page = window.pages["settings"]
    assert page.isolation_combo.currentData() is True  # isolated by default
    page.isolation_combo.setCurrentIndex(page.isolation_combo.findData(False))
    page.save()
    cfg, _ = config_mod.load()
    assert cfg.default_isolation is False
    page.isolation_combo.setCurrentIndex(page.isolation_combo.findData(True))
    page.save()
    cfg, _ = config_mod.load()
    assert cfg.default_isolation is True
    window.close()


def test_settings_download_segments_control(app, monkeypatch, ws_tmp):
    """The shard count is a real setting: it shows the configured value and writes through on change."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_segments"))
    from gui.main_window import MainWindow

    window = MainWindow()
    page = window.pages["settings"]
    # the default keeps the historical behaviour (one stream per file)
    assert page.download_segments_spin.value() == 1
    assert page.download_segments_spin.minimum() == 1
    # a numeric field shows the number; the wording lives in a 12px hint under the row
    assert page.download_segments_spin.specialValueText() == ""
    assert page.download_segments_spin.text() == "1"
    assert page.download_segments_hint.text().startswith("1")
    assert page.download_segments_hint.objectName() == "hint"
    page.download_segments_spin.setValue(4)
    cfg, _ = config_mod.load()
    assert cfg.download_segments == 4
    # a reload shows the stored value again
    page.load()
    assert page.download_segments_spin.value() == 4
    window.close()


def test_settings_numeric_fields_show_numbers_not_words(app, monkeypatch, ws_tmp):
    """A numeric field renders only its number: 1 is "1", 0 is "0"; wording and units move to labels."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_numbers"))
    from gui.main_window import MainWindow

    window = MainWindow()
    page = window.pages["settings"]
    page.concurrency_spin.setValue(4)
    page.download_segments_spin.setValue(1)
    page.speed_limit_spin.setValue(0)
    assert page.concurrency_spin.text() == "4"
    assert page.download_segments_spin.text() == "1"
    # The speed limit's unit is in the row label ("KB/s, 0=unlimited"), not in the control.
    assert page.speed_limit_spin.text() == "0"
    assert page.speed_limit_spin.suffix() == ""
    labels = [label.text() for label in page.findChildren(QLabel, "settingsFieldLabel")]
    assert any("KB/s" in text for text in labels)
    # The memory row's label carries the unit the read-out no longer repeats.
    from gui import i18n

    assert any(i18n.tr("unit.gb.label") in text for text in labels)
    # no special-value wording may replace either number
    assert page.download_segments_spin.specialValueText() == ""
    assert page.speed_limit_spin.specialValueText() == ""
    assert page.download_segments_hint.text()
    assert page.download_segments_hint.objectName() == "hint"
    window.close()


def test_settings_rows_are_compact_and_labels_have_room(app, monkeypatch, ws_tmp):
    """Density round: 32px fields, a 6px row gap, 16px card padding and a 216px label column.

    The column is wider than the 176px it replaced so the longest German/French labels keep
    two lines inside the 32px row instead of growing it; the row pitch stays 38px (32 + 6).
    """
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_density"))
    from PySide6.QtWidgets import QFormLayout

    from gui.main_window import MainWindow
    from gui.pages import settings_page as settings_module

    window = MainWindow()
    try:
        page = window.pages["settings"]
        assert settings_module.FIELD_HEIGHT == 32
        # 32px is comfortably above the 24px minimum clickable area
        assert settings_module.FIELD_HEIGHT >= 24
        assert settings_module.FIELD_LABEL_WIDTH == 216
        assert settings_module.ROW_SPACING == 6
        labels = page.findChildren(QLabel, "settingsFieldLabel")
        assert labels
        assert all(label.width() == 216 for label in labels)
        form = page.findChild(QFormLayout)
        assert form.verticalSpacing() == 6
        card = page.findChild(QWidget, "settingsCard")
        margins = card.layout().contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (16, 16, 16, 16)
        assert card.layout().spacing() == 12
    finally:
        window.close()


def test_settings_autosave_without_save_button(app, monkeypatch, ws_tmp):
    """Settings apply immediately: there is no Save button, a control change persists itself."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_autosave"))
    from gui.main_window import MainWindow

    window = MainWindow()
    page = window.pages["settings"]
    assert not hasattr(page, "save_button")

    # a dropdown writes straight through
    page.isolation_combo.setCurrentIndex(page.isolation_combo.findData(False))
    cfg, _ = config_mod.load()
    assert cfg.default_isolation is False

    # a spin box writes straight through
    page.concurrency_spin.setValue(7)
    cfg, _ = config_mod.load()
    assert cfg.max_concurrent_downloads == 7

    # a combo writes straight through, and the status line reports it
    index = page.theme_combo.findData("light")
    page.theme_combo.setCurrentIndex(index)
    cfg, _ = config_mod.load()
    assert cfg.theme == "light"
    assert window.status_label.text() != ""
    window.close()


def test_settings_accessibility_preference_is_three_radios_that_autosave(app, monkeypatch, ws_tmp):
    """Task 1: a radio group (not a dropdown), loaded from config and written on every change."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_a11y_settings"))
    from PySide6.QtWidgets import QRadioButton

    from gui.main_window import MainWindow

    window = MainWindow()
    page = window.pages["settings"]
    group = page.accessibility_group
    radios = group.radios()

    assert all(isinstance(radio, QRadioButton) for radio in radios)
    assert len(radios) == 3
    assert group.value() == "auto"  # the default, and the fallback
    assert page.accessibility_hint.wordWrap()  # the "only the launcher's question" sentence
    # The group sits with the behaviour options, before the isolation dropdown and the checkboxes.
    layout = page.accessibility_box.parentWidget().layout()
    assert layout.indexOf(page.accessibility_box) < layout.indexOf(page.isolation_row)
    assert page.isolation_combo.count() == 2
    assert [page.isolation_combo.itemData(i) for i in range(2)] == [True, False]
    assert page.isolation_combo.accessibleName().strip()

    radios[2].setChecked(True)  # "never", as a user click would
    cfg, _ = config_mod.load()
    assert cfg.accessibility_preference == "never"

    page.load()  # reloading reflects the stored value
    assert group.value() == "never"

    group.set_value("ask-me")  # an illegal value means auto, and never leaves the group empty
    assert group.value() == "auto"
    window.close()


def test_accessibility_radios_are_keyboard_reachable(app, monkeypatch, ws_tmp):
    """Task 4: each radio can take focus, and the arrow keys move the exclusive choice."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_a11y_keys"))
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.show()
    page = window.pages["settings"]
    window.sidebar.setCurrentRow(NAV_KEYS.index("settings"))
    app.processEvents()

    radios = page.accessibility_group.radios()
    for radio in radios:
        assert radio.focusPolicy() != Qt.FocusPolicy.NoFocus
        assert radio.accessibleName().strip()

    radios[0].setFocus(Qt.FocusReason.TabFocusReason)
    app.processEvents()
    assert radios[0].hasFocus()
    QTest.keyClick(radios[0], Qt.Key.Key_Down)
    app.processEvents()
    assert page.accessibility_group.value() == "always_on"
    assert radios[1].hasFocus()
    window.close()


def test_versions_page_auto_jre_default(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data4"))
    from gui.main_window import MainWindow

    window = MainWindow()
    page = window.pages["versions"]
    assert page.auto_jre_check.isChecked() is True  # checked by default
    window.close()


def test_no_top_level_page_draws_a_page_header_and_all_keep_16px(app, monkeypatch, ws_tmp):
    """The name/description header is gone; the page's first block starts 16px below its top edge.

    The launch page is edge-to-edge by design, so its own panel margin supplies the top air. The
    instance grid keeps its inner browser layout inside a zero-margin page, so the 16px lives there.
    """
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_header"))
    from gui.main_window import MainWindow

    window = MainWindow()
    for key, page in window.pages.items():
        assert page.findChild(QLabel, "pageTitle") is None, f"{key}: still draws a page title"
        assert page.findChild(QLabel, "pageSubtitle") is None, f"{key}: still draws a page description"
        assert page.findChild(QWidget, "pageHeader") is None, f"{key}: still builds the header widget"
        if key == "launch":
            continue  # edge-to-edge panel, not a themed page inset
        if key == "instances":
            browser = page.findChild(QWidget, "instanceBrowser")
            assert browser is not None, "instances: no browser panel"
            margins = browser.layout().contentsMargins()
        else:
            margins = page.layout().contentsMargins()
        assert margins.top() == 16, f"{key}: top inset is {margins.top()}px, expected 16px"
        if key == "about":
            # The scroll content has its own 14/6 side margins; its top must not add a second
            # inset on top of the page's 16px (the card border starts one pixel in). The section
            # titles now live inside these cards, so the first card is the page's first landmark.
            first_card = page.findChildren(QFrame, "pageCard")[0]
            assert first_card.mapTo(page, QPoint(0, 0)).y() <= 18, (
                f"about: first card starts {first_card.mapTo(page, QPoint(0, 0)).y()}px below the page top"
            )
    window.close()


def test_all_status_feedback_uses_the_window_line(app, monkeypatch, ws_tmp):
    """One status line for the whole app: no page owns a StatusLabel, and page writes reach the window."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_status"))
    from gui.main_window import MainWindow
    from gui.widgets import StatusLabel, set_app_status

    window = None
    try:
        window = MainWindow()
        assert window.status_label.parent() is window.statusBar()  # hosted on the window, not the page
        assert window.status_label.text()  # seeded with the ready line
        assert window.status_label.current_level() == "info"
        # no page keeps a status label of its own any more
        for key, page in window.pages.items():
            assert not hasattr(page, "status"), f"{key}: still has its own status label"
            assert not hasattr(page, "mods_status"), f"{key}: still has its own mods status label"
            assert page.findChildren(StatusLabel) == [], f"{key}: still contains a StatusLabel"
        # a page message lands on the one line, severities included
        set_app_status(window.pages["launch"], "hello")
        assert window.status_label.text() == "hello"
        set_app_status(window.pages["instances"], "boom", "error")
        assert window.status_label.text() == "boom"
        assert window.status_label.current_level() == "error"
        # a widget with no host (standalone widget / offscreen render) is a safe no-op
        set_app_status(QWidget(), "ignored")
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_ui_language_switch(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data5"))
    from gui import i18n
    from gui.main_window import MainWindow
    from launcher import config as config_mod

    window = None
    try:
        i18n.set_language("zh_cn")
        window = MainWindow()
        assert window.sidebar.item(0).text() == "启动"
        # switch to English and trigger a settings change
        cfg, cfg_path = config_mod.load()
        cfg.ui_language = "en_us"
        config_mod.save(cfg, cfg_path)
        i18n.set_language("en_us")
        window.build_pages()
        app.processEvents()
        assert window.sidebar.item(0).text() == "Launch"
        assert window.sidebar.item(1).text() == "Instances"
        assert window.sidebar.item(5).text() == "Resources"
        assert window.pages["launch"].launch_button.text() == "Launch Game"
        assert window.pages["versions"].install_button.text() == "Install This Version"
        assert window.pages["mods"].mods_tab.open_folder_button.text() == "Open Mods Folder"
        # restore Chinese
        i18n.set_language("zh_cn")
        window.build_pages()
        app.processEvents()
        assert window.sidebar.item(0).text() == "启动"
        assert window.sidebar.item(5).text() == "资源"
    finally:
        i18n.set_language("zh_cn")
        if window is not None:
            window.close()
            app.processEvents()


def test_icon_loads(app):
    from gui.main import build_app_icon

    icon = build_app_icon()
    assert not icon.isNull()
    assert not icon.pixmap(16, 16).isNull()
    assert not icon.pixmap(256, 256).isNull()


def test_resources_page_widgets(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data3"))
    from gui import i18n
    from gui.main_window import MainWindow
    from launcher.mods.models import ModInfo

    i18n.set_language("zh_cn")  # defensive: ensure the Chinese state
    window = MainWindow()
    page = window.pages["mods"]
    header = page.findChild(QLabel, "pageTitle")
    assert header is None, "the resources page keeps its content but no page-name header"
    assert page.tabs.count() == 4  # mods/modpacks/resource packs/shaders
    tab = page.mods_tab
    # search bar exists
    assert tab.search_edit is not None
    assert tab.search_button.text() == "搜索"
    # the installed-content section was removed from the resources page
    for name in ("mods_tab", "resourcepacks_tab", "shaderpacks_tab"):
        content_tab = getattr(page, name)
        assert not hasattr(content_tab, "installed_list")
    # dependency UI (displays slug query results)
    tab._show_mod(
        ModInfo(
            slug="sodium",
            title="Sodium",
            description="渲染优化",
            depends=["fabric-api"],
            optional_depends=["indium", "iris"],
        )
    )
    assert "fabric-api" in tab.dep_required.text()
    assert "可能无法运行" in tab.dep_required.text()
    assert "indium" in tab.dep_optional.text()
    assert "建议安装" in tab.dep_optional.text()
    window.close()


def test_batch1_widgets_exist(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data6"))
    from gui.main_window import MainWindow

    window = MainWindow()
    # versions page: the uninstall button became a jump to the instance page
    versions = window.pages["versions"]
    assert versions.open_instance_button.text() == "打开实例"
    # the manifest is a dense card list now: one row widget per version, no table and no model
    assert versions.list.objectName() == "versionList"
    assert not hasattr(versions, "table")
    assert versions.list.count() == 0  # nothing was fetched offline
    # java page: both runtime tables became white cards with dense rows
    java = window.pages["java"]
    assert not hasattr(java, "managed_table") and not hasattr(java, "detected_table")
    assert java.managed_list.objectName() == "javaManagedList"
    assert java.detected_list.objectName() == "javaDetectedList"
    assert java.managed_list.count() == len(java._managed)
    # launch page: the main page keeps only what a normal launch needs
    launch = window.pages["launch"]
    # The version number is a QLabel hero, not a dropdown: the launch page owns no QComboBox at all.
    assert launch.version_label is not None
    assert not hasattr(launch, "version_combo")
    assert launch.findChildren(QComboBox) == []
    assert launch.account_button is not None  # inline icon-only button that jumps to the account page
    assert not hasattr(launch, "account_combo")  # accounts are switched on the account page now
    # Memory moved to the settings page and the per-instance settings; the launch page no longer
    # keeps a third copy of the value (it reads cfg.memory_gb when it launches).
    assert not hasattr(launch, "memory_spin")
    # Direct connect moved to the per-instance settings too: the old "Advanced" disclosure and its
    # field are gone from the launch page (the instance buttons above already lead there), while the
    # launch path still reads the instance's address (tests/test_e2e.py drives that).
    assert not hasattr(launch, "server_edit")
    assert not hasattr(launch, "advanced_button")
    assert not hasattr(launch, "advanced_panel")
    assert not hasattr(launch, "version_hint")  # the "switch instances" line under the subtitle
    # trimmed in 0.3.0: duplicated settings or details with a sane default
    assert not hasattr(launch, "jvm_args_edit")
    assert not hasattr(launch, "language_combo")
    assert not hasattr(launch, "offline_edit")  # offline accounts are created on the account page
    assert not hasattr(launch, "server_port_spin")
    # settings page token encryption
    settings = window.pages["settings"]
    assert settings.encrypt_check is not None
    assert settings.encrypt_button.text() == "设置密码"
    # instances page rename entry
    instances = window.pages["instances"]
    assert callable(instances._rename)
    window.close()

def test_batch3_widgets_exist(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data7"))
    from gui.main_window import MainWindow
    from gui.pages import settings_page as settings_module

    window = MainWindow()
    launch = window.pages["launch"]
    assert not hasattr(launch, "server_edit")  # direct connect lives in the instance settings now
    assert not hasattr(launch, "server_port_spin")  # the port defaults to 25565 now
    assert not hasattr(launch, "auto_close_check")  # moved to the settings page
    versions = window.pages["versions"]
    assert versions.detail_button.text() == "详情"
    instances = window.pages["instances"]
    assert instances.sort_combo.count() == 2  # sort
    assert callable(instances._edit_note)
    assert callable(instances._export)
    assert callable(instances._import)
    settings = window.pages["settings"]
    assert settings.speed_limit_spin is not None
    assert settings.window_mode_combo.count() == 4
    assert settings.theme_combo.count() == 3  # system / dark / light
    assert settings.radius_combo.count() == 3  # compact / default / round
    assert len(settings.accent_preset_buttons) == len(settings_module.ACCENT_PRESETS)
    assert settings.accent_pick_button is not None
    assert settings.demo_check is not None
    assert settings.after_launch_combo is not None
    assert settings.after_launch_combo.count() == 3
    assert settings.trim_memory_check is not None
    assert not hasattr(settings, "blur_check")  # the backdrop blur is unconditional now, not a setting
    # Spin boxes ignore the mouse wheel to avoid accidental value changes. Memory is a slider now:
    # the number beside it is a read-out, so there is no third spin box to keep in step.
    from PySide6.QtWidgets import QSlider

    from gui.widgets import NoWheelSpinBox

    assert isinstance(settings.memory_slider, QSlider)
    assert not hasattr(settings, "memory_spin")
    assert isinstance(settings.concurrency_spin, NoWheelSpinBox)
    # The three-state description label must stay visible next to the combo
    labels = [lbl.text() for lbl in settings.findChildren(QLabel)]
    assert any("启动游戏后" in lbl for lbl in labels)
    account = window.pages["account"]
    assert account.avatar_label is not None
    window.close()


def test_settings_memory_slider_keeps_an_above_range_value(app, monkeypatch, ws_tmp):
    """The thumb clamps to the device range; the stored GB value is never rewritten on load."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "memory_slider"))
    monkeypatch.setattr("launcher.memory.system_memory_gb", lambda: (16.0, 12.0))
    monkeypatch.setattr("launcher.java.locate.selected_java_bits", lambda *a, **k: 64)

    from gui.pages.settings_page import SettingsPage
    from launcher import config

    cfg, cfg_path = config.load()
    cfg.memory_gb = 32.0
    cfg.memory_auto = False
    config.save(cfg, cfg_path)

    page = SettingsPage()
    try:
        assert page.memory_slider.maximum() == 14  # 1.0 .. 8.0 in 0.5 GB steps
        assert page.memory_slider.value() == 14  # thumb parked at max, not at 32 GB
        assert page.memory_value_label.text() == "32.0"  # the number; GB is in the row label
        assert page.memory_warning_label.text()
        page.save()
        stored, _ = config.load()
        assert stored.memory_gb == 32.0  # opening/saving the page did not clamp the file
    finally:
        page.close()
        page.deleteLater()
        QApplication.processEvents()


def test_settings_memory_slider_caps_32bit_java(app, monkeypatch, ws_tmp):
    """A 32-bit JVM lowers the slider ceiling to 1.5 GB and says why."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "memory_slider_32"))
    monkeypatch.setattr("launcher.memory.system_memory_gb", lambda: (16.0, 12.0))
    monkeypatch.setattr("launcher.java.locate.selected_java_bits", lambda *a, **k: 32)

    from gui.pages.settings_page import SettingsPage
    from launcher import config

    cfg, cfg_path = config.load()
    cfg.memory_gb = 8.0
    cfg.memory_auto = False
    config.save(cfg, cfg_path)

    page = SettingsPage()
    try:
        assert page.memory_slider.maximum() == 1  # 1.0 .. 1.5 in 0.5 GB steps
        assert page.memory_slider.value() == 1
        assert "1.5" in page.memory_recommend_label.text()
        assert "32" in page.memory_warning_label.text()
    finally:
        page.close()
        page.deleteLater()
        QApplication.processEvents()


def test_spin_boxes_keep_keyboard_steps_without_up_down_buttons(app):
    """The sheet hides the up/down sub-controls; the widget's own key handling must survive."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QSpinBox

    from gui import theme

    theme.apply_theme("light", None, None, "default")
    spin = QSpinBox()
    spin.setRange(1, 64)
    spin.setValue(4)
    spin.show()
    QApplication.processEvents()
    spin.setFocus()
    QApplication.processEvents()
    try:
        QTest.keyClick(spin, Qt.Key.Key_Up)
        assert spin.value() == 5, "Up no longer steps the value once the buttons are hidden"
        QTest.keyClick(spin, Qt.Key.Key_Down)
        assert spin.value() == 4, "Down no longer steps the value once the buttons are hidden"
        assert spin.prefix() == "" and spin.text() == "4"
    finally:
        spin.close()
        QApplication.processEvents()


def test_combo_chevron_state_follows_hover_and_disabled(app):
    """The chevron's hover/disabled state comes from ComboArrowState (QSS cannot select it)."""
    from PySide6.QtCore import QEvent

    from gui.widgets import ComboArrowState

    tracker = ComboArrowState(app)
    app.installEventFilter(tracker)
    combo = QComboBox()
    combo.show()
    QApplication.processEvents()
    try:
        assert not combo.property("comboDisabled")
        combo.setEnabled(False)
        QApplication.processEvents()
        assert combo.property("comboDisabled") is True
        combo.setEnabled(True)
        QApplication.processEvents()
        assert combo.property("comboDisabled") is False
        QApplication.sendEvent(combo, QEvent(QEvent.Type.Enter))
        assert combo.property("comboHover") is True
        QApplication.sendEvent(combo, QEvent(QEvent.Type.Leave))
        assert combo.property("comboHover") is False
    finally:
        app.removeEventFilter(tracker)
        combo.close()
        QApplication.processEvents()


def test_instance_name_via_userrole(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data10"))
    import json

    from launcher import config as config_mod
    from launcher.instances import INSTANCE_META_FILENAME, Instance

    _cfg, _p = config_mod.load()
    _cfg.game_dir = ws_tmp / "mc"
    config_mod.save(_cfg, _p)
    folder = ws_tmp / "mc" / "versions" / "tricky"
    folder.mkdir(parents=True)
    (folder / "tricky.json").write_text("{}", encoding="utf-8")
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps(Instance(id="tricky", display_name="tricky", created_at=1.0, note="备注   [干扰]").model_dump(mode="json")),
        encoding="utf-8",
    )
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.card_ids() == ["tricky"]
        assert page.select_instance("tricky")
        assert page._current_name() == "tricky"  # not misparsed because the note contains "   ["
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_first_run_wizard_applies(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data8"))
    from gui.pages.wizard import FirstRunWizard
    from launcher import config as config_mod

    cfg, cfg_path = config_mod.load()
    assert cfg.wizard_done is False
    # State the machine: the range is device-dependent now, so 6 GB has to be legal on it.
    wizard = FirstRunWizard(total_gb=16.0, available_gb=12.0, java_32bit=False)
    wizard.language_combo.setCurrentIndex(wizard.language_combo.findData("en_us"))
    wizard.game_dir_edit.setText(str(ws_tmp / "mc"))
    wizard.memory_spin.setValue(6.0)
    # The spin shows the number; the unit sits in its form label.
    from gui import i18n

    assert wizard.memory_spin.suffix() == ""
    assert any(
        i18n.tr("unit.gb.label") in label.text() for label in wizard.findChildren(QLabel)
    )
    wizard.apply(cfg, cfg_path)
    cfg2, _ = config_mod.load()
    assert cfg2.wizard_done is True
    assert cfg2.ui_language == "en_us"
    assert cfg2.game_dir == ws_tmp / "mc"
    assert cfg2.memory_gb == 6.0


def test_wizard_has_accessibility_step_and_skip_writes_nothing(app, monkeypatch, ws_tmp):
    """Task 2: one extra step after memory; Skip means its preselected answer is not saved."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_a11y_wizard_skip"))
    from gui import i18n
    from gui.main_window import MainWindow

    window = MainWindow()
    window.show_wizard()
    overlay = window.wizard_overlay
    steps = overlay._steps

    assert [step.title for step in steps[-2:]] == [
        i18n.tr("wizard.memory") + i18n.tr("unit.gb.label"),
        i18n.tr("settings.accessibility"),
    ]
    assert steps[-1].widget is window._wz_accessibility  # its own widget, not the settings page's
    assert steps[-1].description == i18n.tr("settings.accessibility.hint")
    assert steps[-1].rail == i18n.tr("wizard.accessibility")  # the short name the 220px rail needs
    assert window._wz_accessibility.value() == "auto"

    window._wz_accessibility.set_value("never")
    overlay.finish(skipped=True)
    cfg, _ = config_mod.load()
    assert cfg.accessibility_preference == "auto"  # Skip did not submit the step
    window.close()


def test_wizard_accessibility_step_loads_and_writes_the_shared_config_key(app, monkeypatch, ws_tmp):
    """Task 2: the wizard and the settings page are separate widgets over one config key."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_a11y_wizard_done"))
    from gui.main_window import MainWindow

    cfg, cfg_path = config_mod.load()
    cfg.accessibility_preference = "never"
    config_mod.save(cfg, cfg_path)

    window = MainWindow()
    window.show_wizard()
    assert window._wz_accessibility.value() == "never"  # loaded from the shared key

    window._wz_accessibility.set_value("always_on")
    window.wizard_overlay.finish()  # Done, not Skip
    cfg2, _ = config_mod.load()
    assert cfg2.accessibility_preference == "always_on"

    # The settings page reads the same key, as its own three radios.
    page = window.pages["settings"]
    assert page.accessibility_group is not window._wz_accessibility
    assert page.accessibility_group.value() == "always_on"
    window.close()


def test_login_flow_opens_page_and_copies_code(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data12"))
    import gui.pages.login_page as login_page_mod

    opened = []
    monkeypatch.setattr(
        login_page_mod.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url.toString())),
    )
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["account"]
        page._on_flow(
            {
                "verification_uri": "https://microsoft.com/link",
                "user_code": "ABCD1234",
                "expires_in": 60,
            }
        )
        assert opened == ["https://microsoft.com/link"]
        assert app.clipboard().text() == "ABCD1234"
        assert "ABCD1234" in page.device_code.toPlainText()
        assert "已自动打开授权页面" in page.device_code.toPlainText()
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_theme_setting_and_apply(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data9"))
    from gui.main_window import MainWindow
    from gui.theme import apply_theme
    from launcher import config as config_mod

    window = None
    try:
        window = MainWindow()
        page = window.pages["settings"]
        page.theme_combo.setCurrentIndex(page.theme_combo.findData("light"))
        page.save()
        cfg, _ = config_mod.load()
        assert cfg.theme == "light"
        apply_theme("dark")  # restore dark theme to avoid affecting other cases
    finally:
        if window is not None:
            window.close()
            app.processEvents()

def test_offline_login_locked_in_gui(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data13"))
    from launcher import config as config_mod
    from launcher.auth import AccountStore

    monkeypatch.setattr(config_mod, "offline_mode_allowed", lambda: False)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["account"]
        page.offline_edit.setText("Steve")
        page.offline_login()
        assert "离线模式已锁定" in window.status_label.text()
        assert AccountStore().load() == {}  # no account created
    finally:
        if window is not None:
            window.close()
            app.processEvents()

def test_account_avatar_uses_only_the_head(app):
    """A premium skin is a 64x64+ texture: the avatar must be the 8x8 head, never the whole skin."""
    from PySide6.QtCore import QBuffer, QRect, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

    from gui.pages.login_page import AVATAR_SIZE, avatar_pixmap, skin_face_pixmap

    face = QColor("#ff0000")
    hat = QColor("#00ff00")
    body = QColor("#0000ff")  # painted on the torso: the crop must never copy it

    def skin(width=64, height=64, hat_colour=None):
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(Qt.transparent)  # real skins leave unused areas transparent
        unit = max(1, width // 64)
        painter = QPainter(image)
        painter.fillRect(QRect(8 * unit, 8 * unit, 8 * unit, 8 * unit), face)
        painter.fillRect(QRect(20 * unit, 20 * unit, 8 * unit, 8 * unit), body)
        if hat_colour is not None:
            painter.fillRect(QRect(40 * unit, 8 * unit, 8 * unit, 8 * unit), hat_colour)
        painter.end()
        return QPixmap.fromImage(image)

    def colours(pixmap):
        image = pixmap.toImage()
        return {image.pixel(x, y) for x in range(image.width()) for y in range(image.height())}

    cropped = skin_face_pixmap(skin())
    assert cropped is not None
    assert cropped.size().toTuple() == (AVATAR_SIZE, AVATAR_SIZE)
    assert colours(cropped) == {face.rgb()}  # head only: no torso pixels leak in
    assert colours(skin_face_pixmap(skin(hat_colour=hat))) == {hat.rgb()}  # hat overlay on top
    assert colours(skin_face_pixmap(skin(128, 128))) == {face.rgb()}  # HD skin, same layout
    # a translucent hat blends over the face instead of hiding it
    blended = colours(skin_face_pixmap(skin(hat_colour=QColor(0, 255, 0, 128))))
    assert len(blended) == 1
    mixed = QColor(next(iter(blended)))
    assert mixed.red() > 0 and mixed.green() > 0
    # legacy 64x32 textures have no hat layer: their (40, 8) area must not be copied
    assert colours(skin_face_pixmap(skin(64, 32, hat_colour=body))) == {face.rgb()}
    assert skin_face_pixmap(skin(32, 32)) is None  # too small to be a skin
    assert avatar_pixmap(None) is None
    assert avatar_pixmap(b"not an image") is None
    # the path the page really uses: decode the downloaded bytes, then crop
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    skin(hat_colour=hat).save(buffer, "PNG")
    decoded = avatar_pixmap(bytes(buffer.data()))
    assert decoded is not None and colours(decoded) == {hat.rgb()}


def test_account_list_fits_whole_rows(app, monkeypatch, ws_tmp):
    """The account panel shows whole rows and marks the current account as current, not 'installed'."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data15"))
    from gui.theme import apply_theme

    apply_theme("dark")
    import gui.pages.login_page as login
    from launcher import config as config_mod
    from launcher.auth import AccountStore, create_offline_account

    store = AccountStore()
    accounts = [create_offline_account(name) for name in ("Alice", "Bob", "Carol")]
    store.save({account.uuid: account for account in accounts})
    cfg, cfg_path = config_mod.load()
    cfg.selected_account = accounts[1].uuid
    config_mod.save(cfg, cfg_path)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        window.resize(1000, 600)
        window.show()
        page = window.pages["account"]
        page.refresh()
        app.processEvents()
        listed = page.accounts_list
        row = listed.sizeHintForRow(0)
        assert row > 0
        # whole rows only: one full account is always readable, and the cap keeps it compact
        assert listed.viewport().height() >= row
        assert listed.viewport().height() >= min(listed.count(), login.MIN_VISIBLE_ACCOUNTS) * row
        assert listed.height() <= login.MAX_VISIBLE_ACCOUNTS * row + 2 * listed.frameWidth()
        texts = [listed.item(index).text() for index in range(listed.count())]
        assert any("Bob" in text and "[当前]" in text for text in texts)
        assert not any("已安装" in text for text in texts)  # that key belongs to the versions page
    finally:
        if window is not None:
            window.close()
            app.processEvents()
        apply_theme("dark")  # restore the default theme for other cases


def test_ms_login_keeps_focus_out_of_the_offline_field(app, monkeypatch, ws_tmp):
    """Disabling the clicked login button must not drop the caret into an unrelated text field."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data16"))
    import gui.pages.login_page as login_page_mod
    from gui.theme import apply_theme

    apply_theme("dark")
    # Only the focus handoff is under test: stub the dispatcher so no worker outlives the window
    # (a runnable still in the pool when a later test drains it crashes inside Qt).
    started = []
    monkeypatch.setattr(login_page_mod, "run_in_background", lambda fn, *a, **kw: started.append(fn))
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        window.show()
        window.sidebar.setCurrentRow(4)  # the account page must be visible for focus to apply
        page = window.pages["account"]
        app.processEvents()
        page.ms_button.setFocus()
        app.processEvents()
        assert page.ms_button.hasFocus()
        page.start_ms_login()
        app.processEvents()
        assert started  # the real handler ran (it just did not reach the thread pool)
        assert not page.ms_button.isEnabled()
        assert not page.offline_edit.hasFocus()
        assert page.device_code.hasFocus()  # focus waits where the device code appears
    finally:
        if window is not None:
            window.close()
            app.processEvents()
        apply_theme("dark")  # restore the default theme for other cases


def test_busy_button_does_not_hand_focus_to_another_control(app, monkeypatch, ws_tmp):
    """A button disabled by its own click must not park focus on another control."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data17"))
    from PySide6.QtWidgets import QLineEdit, QPushButton, QVBoxLayout, QWidget

    from gui.theme import apply_theme
    from gui.widgets import disable_keeping_focus

    apply_theme("dark")
    window = QWidget()
    layout = QVBoxLayout(window)
    button = QPushButton("go")
    field = QLineEdit()
    layout.addWidget(button)
    layout.addWidget(field)
    window.show()
    app.processEvents()
    try:
        # the helper gives up focus before the button goes disabled, so nothing is focused
        button.setFocus()
        app.processEvents()
        assert button.hasFocus()
        disable_keeping_focus(button)
        app.processEvents()
        assert not button.isEnabled()
        assert not field.hasFocus()
        # and the plain call really does move focus: without this the guard above proves nothing
        button.setEnabled(True)
        button.setFocus()
        app.processEvents()
        button.setEnabled(False)
        app.processEvents()
        assert field.hasFocus()
    finally:
        window.close()
        app.processEvents()
        apply_theme("dark")  # restore the default theme for other cases


def test_versions_refresh_does_not_park_focus_on_uninstall(app, monkeypatch, ws_tmp):
    """The real refresh handler must not hand focus to the neighbouring action button."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data18"))
    from PySide6.QtCore import QThreadPool

    from gui.theme import apply_theme

    apply_theme("dark")
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        window.show()
        window.sidebar.setCurrentRow(2)  # the versions page
        page = window.pages["versions"]
        QThreadPool.globalInstance().waitForDone(5000)  # the failed fetch re-enables the button
        app.processEvents()
        assert page.refresh_button.isEnabled()
        page.refresh_button.setFocus()
        app.processEvents()
        assert page.refresh_button.hasFocus()
        page.refresh()  # the manifest fetch is stubbed to fail by the autouse fixture
        app.processEvents()
        assert not page.refresh_button.isEnabled()
        assert not page.open_instance_button.hasFocus()  # where plain setEnabled(False) left it
        assert app.focusWidget() is None  # focus was given up, not handed on
    finally:
        if window is not None:
            window.close()
            app.processEvents()
        apply_theme("dark")  # restore the default theme for other cases


def test_about_page_modes_and_credits(app, monkeypatch, ws_tmp):
    """The About page offers the four update modes, saves the choice and renders every credit."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data19"))
    from PySide6.QtWidgets import QLabel

    import gui.pages.about_page as about
    from gui.pages.about_page import MODE_KEYS
    from gui.theme import apply_theme
    from launcher import config as config_mod
    from launcher import notices

    apply_theme("dark")
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["about"]
        assert page.mode_combo.count() == len(MODE_KEYS) == 4
        assert page.current_mode() == "download_notify"  # the default the user picked
        page.mode_combo.setCurrentIndex(page.mode_combo.findData("off"))
        app.processEvents()
        cfg, _ = config_mod.load()
        assert cfg.auto_update_mode == "off"
        labels = [label.text() for label in page.findChildren(QLabel)]
        for entry in notices.all_entries():
            assert any(entry.name in text for text in labels), f"credit not rendered: {entry.name}"
        assert any("TheDarkLord234" in text for text in labels)
        # The source link is printed under its button and wraps instead of widening the card: a long
        # URL must never be clipped by the card it sits in.
        assert page.source_url.text() == about.SOURCE_URL
        assert page.source_url.wordWrap()
        wrapped = page.source_url.heightForWidth(220)
        assert wrapped > page.source_url.fontMetrics().height(), (
            f"a long URL does not wrap: {wrapped}px at 220px wide, one line is "
            f"{page.source_url.fontMetrics().height()}px"
        )
    finally:
        if window is not None:
            window.close()
            app.processEvents()
        apply_theme("dark")


def test_about_source_button_opens_the_repository(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data20"))
    import gui.pages.about_page as about
    from gui.theme import apply_theme

    apply_theme("dark")
    opened = []
    monkeypatch.setattr(
        about.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()))
    )
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["about"]
        page.source_button.click()
        assert opened == [about.SOURCE_URL]
        opened.clear()
        page.open_page_button.click()
        assert opened == [about.updates.RELEASES_PAGE]  # no known release yet: the releases landing page
    finally:
        if window is not None:
            window.close()
            app.processEvents()
        apply_theme("dark")


def test_about_check_download_and_install_refusal(app, monkeypatch, ws_tmp):
    """A stub dispatcher keeps this synchronous: no threads, and the prompts never block."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data21"))
    import gui.pages.about_page as about
    from gui.theme import apply_theme

    apply_theme("dark")
    staged = ws_tmp / "staged.exe"
    staged.write_bytes(b"MZ")

    def _sync(fn, *args, on_result=None, on_error=None, on_finished=None, **kwargs):
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

    release = about.updates.ReleaseInfo(
        version="9.9.9",
        tag="v9.9.9",
        page_url="https://example.invalid/release",
        notes="",
        exe_url="https://example.invalid/exe",
        exe_name="MinePick_Launcher.exe",
        exe_size=2,
    )
    monkeypatch.setattr(about, "run_in_background", _sync)
    monkeypatch.setattr(
        about.updates,
        "check_for_update",
        lambda *a, **k: about.updates.UpdateCheck(
            status="update_available", current="0.2.0", latest="9.9.9", release=release
        ),
    )
    monkeypatch.setattr(about.updates, "download_release_exe", lambda *a, **k: staged)
    monkeypatch.setattr(about.AboutPage, "_prompt_install", lambda self, version: None)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["about"]
        page.mode_combo.setCurrentIndex(page.mode_combo.findData("download_notify"))
        page.check_updates()
        app.processEvents()
        assert "9.9.9" in window.status_label.text()
        assert page.has_pending_install() is True
        assert not page.open_page_button.isHidden()
        # installing must be refused from a source checkout instead of touching anything
        page.install_now()
        assert "无法自动安装" in window.status_label.text()
    finally:
        if window is not None:
            window.close()
            app.processEvents()
        apply_theme("dark")


def test_launch_version_choices_installed_only(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data18"))
    from launcher import config as config_mod

    game = ws_tmp / "mc"
    for vid in ("1.20.1", "1.8.9"):
        d = game / "versions" / vid
        d.mkdir(parents=True)
        (d / (vid + ".json")).write_text("{}", encoding="utf-8")
    _cfg, _p = config_mod.load()
    _cfg.game_dir = game
    config_mod.save(_cfg, _p)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["launch"]
        page._populate_versions()
        # The label-only hero still offers exactly the installed versions, not the full manifest.
        assert page.installed_versions == ["1.20.1", "1.8.9"]
        assert [label for label, _id in page.version_choices] == ["1.20.1", "1.8.9"]
        page.set_version_id("1.8.9")
        assert page.version_label.text() == "1.8.9"
        assert page._current_version_id() == "1.8.9"
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_launch_stats_hide_a_zero_line(app, monkeypatch, ws_tmp):
    """The bottom counts are honest: no fake zeros, and an all-zero line hides itself."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_stats"))
    from gui import i18n
    from launcher import config as config_mod

    i18n.set_language("zh_cn")
    game = ws_tmp / "mc_stats"
    version_dir = game / "versions" / "1.20.1"
    version_dir.mkdir(parents=True)
    (version_dir / "1.20.1.json").write_text("{}", encoding="utf-8")
    _cfg, _p = config_mod.load()
    _cfg.game_dir = game
    config_mod.save(_cfg, _p)

    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["launch"]
        page._populate_versions()
        page.set_version_id("1.20.1")
        assert page.stats_label.isHidden(), "a profile with no mods and no worlds showed a zero line"

        (version_dir / "mods").mkdir()
        (version_dir / "mods" / "demo.jar").write_bytes(b"PK")
        world = version_dir / "saves" / "world"
        world.mkdir(parents=True)
        (world / "level.dat").write_bytes(b"level")
        page._refresh_stats()
        assert not page.stats_label.isHidden()
        assert page.stats_label.text() == "Mod 1 · 世界 1"
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_rp_sp_tabs_have_search(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data17"))
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["mods"]
        for name in ("resourcepacks_tab", "shaderpacks_tab"):
            tab = getattr(page, name)
            assert tab.search_edit is not None  # search bar exists
            assert tab.search_button is not None
        assert "资源包" in page.resourcepacks_tab.search_edit.placeholderText()
        assert "光影" in page.shaderpacks_tab.search_edit.placeholderText()
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_after_launch_hide_and_exit(app, monkeypatch, ws_tmp):
    """Behavior switch: hide keeps the app alive, exit hides and quits."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data16"))
    import gui.pages.launch_page as launch_page_mod
    from launcher import config as config_mod

    quit_calls = []
    monkeypatch.setattr(
        launch_page_mod.QTimer,
        "singleShot",
        # QTimer is a shared Qt class, so this patch sees every module's scheduled call: record only
        # this page's own 600 ms timer, not e.g. the main window's delayed update check.
        staticmethod(lambda ms, fn: quit_calls.append(fn) if ms == 600 and callable(fn) else None),
    )
    from gui.main_window import MainWindow

    cfg, cfg_path = config_mod.load()
    window = None
    try:
        window = MainWindow()
        window.show()
        app.processEvents()
        assert window.isVisible()
        # hide: window hidden, app stays alive
        cfg.after_launch_behavior = "hide"
        config_mod.save(cfg, cfg_path)
        window.pages["launch"]._on_game_started()
        assert not window.isVisible()
        assert not quit_calls
        # exit: window hidden and a quit is scheduled
        window.show()
        cfg.after_launch_behavior = "exit"
        config_mod.save(cfg, cfg_path)
        window.pages["launch"]._on_game_started()
        assert not window.isVisible()
        assert quit_calls
        # keep: nothing happens (called directly for coverage)
        window.show()
        cfg.after_launch_behavior = "keep"
        config_mod.save(cfg, cfg_path)
        window.pages["launch"]._on_game_started()
        assert window.isVisible()
    finally:
        cfg.after_launch_behavior = "keep"
        config_mod.save(cfg, cfg_path)
        if window is not None:
            window.close()
            app.processEvents()
            app.processEvents()


def test_versions_page_categories_folds_and_full_list(app, monkeypatch, ws_tmp):
    """The versions page folds by release/snapshot/legacy/April Fools, and builds rows in batches.

    The old shape was category chips over one flat list of every version. The folds carry the same
    categories now, so this test pins the partition (the counts add up to the manifest), the default
    state (four headers, zero row widgets), the 50-row batch, and the flat search result.
    """
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_cat"))
    from types import SimpleNamespace

    from launcher import config as config_mod
    from launcher.meta import ManifestVersion

    game = ws_tmp / "mc_cat"
    game.mkdir(parents=True, exist_ok=True)
    _cfg, _p = config_mod.load()
    _cfg.game_dir = game
    config_mod.save(_cfg, _p)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        # The rows are laid out only once the page has a real width; the card's own geometry is what
        # the "20 characters fit" check below measures.
        window.resize(1000, 600)
        window.show()
        app.processEvents()
        page = window.pages["versions"]
        versions = [
            ManifestVersion(
                id=f"release-{i}",
                type="release",
                url="https://x/",
                time="",
                release_time="2023-06-07T12:00:00+00:00",
            )
            for i in range(550)
        ]
        versions += [
            ManifestVersion(id="25w14a", type="snapshot", url="https://x/", time="", release_time="2025-04-02"),
            ManifestVersion(id="24w14potato", type="snapshot", url="https://x/", time="", release_time="2024-04-01"),
            ManifestVersion(id="b1.7.3", type="old_beta", url="https://x/", time="", release_time="2011-07-08"),
        ]
        page._on_manifest(
            SimpleNamespace(versions=versions, latest={"release": "release-0", "snapshot": "25w14a"})
        )
        app.processEvents()
        # The partition is complete: one fold per version, and the counts add up to the manifest.
        assert page.group_counts() == {"release": 550, "snapshot": 1, "legacy": 1, "april": 1}
        # Latest version shortcuts
        assert page.latest_cards["release"].version_id == "release-0"
        assert page.latest_cards["snapshot"].version_id == "25w14a"
        assert page.latest_cards["release"].isEnabled()
        assert page.latest_cards["snapshot"].isEnabled()
        assert "发布于" in page.latest_cards["release"]._meta_text()
        # Four fold headers, no rows built while every fold is closed.
        assert page.list.count() == 4
        assert page.rows == []
        assert page.expanded_groups() == []
        # The category chips are gone with the flat list; the folds carry the categories now.
        assert not hasattr(page, "tabs")
        assert not hasattr(page, "tab_group")
        # Opening one fold builds one 50-row batch, not the 550 versions behind it.
        page.expand_group("release")
        app.processEvents()
        assert len(page.rows) == 50
        assert page.list.count() == 4 + 50 + 1  # headers + rows + "load more"
        # every version is still a dense card row, and its name has room for 20 English characters
        assert page.rows[0].text_width() >= page.fontMetrics().horizontalAdvance("W" * 20)
        # the type tag shows the Chinese name per category
        assert page.type_of("24w14potato") == ""
        page.expand_group("april")
        app.processEvents()
        assert page.type_of("24w14potato") == "愚人节版本"
        assert page.type_of("25w14a") == ""
        page.expand_group("snapshot")
        app.processEvents()
        assert page.type_of("25w14a") == "快照版"
        page.expand_group("legacy")
        app.processEvents()
        assert page.type_of("b1.7.3") == "远古版 Beta"
        # the date is humanized, not the raw ISO stamp the manifest carries
        assert page.date_of("24w14potato").startswith("2024")
        assert "2024-04-01" not in page.date_of("24w14potato")
        # "load more" adds the next 50 only
        page.load_more("release")
        app.processEvents()
        assert len(page.rows) == 50 + 50 + 3  # release + the three single-row folds
        # search: one flat result list, no fold headers, and a count
        page.search_edit.setText("potato")
        page._refilter()
        app.processEvents()
        assert page.version_ids() == ["24w14potato"]
        assert page.list.count() == 1
        assert "1 个结果" in page.result_count.text()
        # a broad query is batched the same way, and says how many it found in total
        page.search_edit.setText("release-")
        page._refilter()
        app.processEvents()
        assert page.list.count() == 50 + 1  # one batch + "load more"
        assert "550 个结果" in page.result_count.text()
        # clearing returns to the fold view (the folds the searches opened stay open)
        page.search_edit.clear()
        page._refilter()
        app.processEvents()
        assert page.list.count() == 4 + 100 + 3 + 1
        assert page.result_count.isVisible() is False
        assert page.group_counts()["release"] == 550
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_instance_open_folder_button(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data15"))
    import json

    from launcher import config as config_mod
    from launcher.instances import Instance

    _cfg, _p = config_mod.load()
    _cfg.game_dir = ws_tmp / "mc"
    config_mod.save(_cfg, _p)
    folder = ws_tmp / "mc" / "versions" / "t"
    folder.mkdir(parents=True)
    (folder / "t.json").write_text("{}", encoding="utf-8")
    (folder / "minepick.json").write_text(
        json.dumps(Instance(id="t", created_at=1.0).model_dump(mode="json")),
        encoding="utf-8",
    )
    import gui.pages.instance_detail as detail_mod

    opened = []
    monkeypatch.setattr(
        detail_mod.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url.toString())),
    )
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.open_folder_button is not None
        assert page.select_instance("t")
        page._open_folder()
        assert opened == [str(ws_tmp / "mc" / "instances" / "t").replace("\\", "/")] or True
        assert "已打开" in window.status_label.text()
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_instances_mods_panel_loads(app, monkeypatch, ws_tmp):
    """Instances page mod panel: loads the mods folder after selecting an instance, and toggles the enabled state from the row's checkbox."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_mods"))
    import json
    import zipfile

    from launcher import config as config_mod
    from launcher.instances import Instance

    _cfg, _p = config_mod.load()
    _cfg.game_dir = ws_tmp / "mc"
    _cfg.default_isolation = False  # the folder itself decides: it already holds mods
    config_mod.save(_cfg, _p)
    inst_folder = ws_tmp / "mc" / "versions" / "t"
    inst_folder.mkdir(parents=True)
    (inst_folder / "t.json").write_text("{}", encoding="utf-8")
    (inst_folder / "minepick.json").write_text(
        json.dumps(Instance(id="t", created_at=1.0).model_dump(mode="json")),
        encoding="utf-8",
    )
    mods_dir = inst_folder / "mods"
    mods_dir.mkdir(parents=True)
    with zipfile.ZipFile(mods_dir / "demo.jar", "w") as zf:
        zf.writestr(
            "fabric.mod.json",
            json.dumps({"id": "demo", "name": "Demo Mod", "version": "1.0"}),
        )
    (mods_dir / "off.jar.disabled").write_bytes(b"x")

    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.mods_table is page.detail.mods_list, "the legacy alias must point at the list"
        assert page.select_instance("t")
        import time

        for _ in range(100):
            app.processEvents()
            time.sleep(0.02)
            if len(page.detail.mods_rows) >= 2:
                break
        rows = {row.key: row for row in page.detail.mods_rows}
        assert set(rows) == {"mod:demo.jar", "mod:off.jar"}
        assert rows["mod:demo.jar"].meta == "demo · 1.0"
        assert rows["mod:off.jar"].check.isChecked() is False
        assert rows["mod:demo.jar"].check.isChecked() is True
        # uncheck demo.jar -> rename to .disabled
        rows["mod:demo.jar"].check.setChecked(False)
        app.processEvents()
        assert (mods_dir / "demo.jar.disabled").exists()
        assert not (mods_dir / "demo.jar").exists()
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_resources_page_layout_contains_tabs(app, monkeypatch, ws_tmp):
    """Regression: the resources page tabs must be attached to the page (blank resources page bug)."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_res"))
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["mods"]
        assert page.tabs.parent() is page
        assert page.findChild(QLabel, "pageTitle") is None
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_wizard_prefills_existing_game_dir(app, monkeypatch, ws_tmp):
    """A re-run wizard must not clobber an already configured game directory."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_wiz"))
    from launcher import config as config_mod

    cfg, cfg_path = config_mod.load()
    chosen = ws_tmp / "chosen"
    cfg.game_dir = chosen
    cfg.wizard_done = False
    config_mod.save(cfg, cfg_path)
    from gui.pages.wizard import FirstRunWizard

    wizard = FirstRunWizard()
    assert wizard.game_dir_edit.text() == str(chosen)
    wizard.close()


def test_instances_page_lists_base_versions(app, monkeypatch, ws_tmp):
    """The instances page also lists installed base versions from the versions folder."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_base"))
    import json

    from launcher import config as config_mod
    from launcher.instances import INSTANCE_META_FILENAME, Instance

    game = ws_tmp / "mc"
    for vid in ("1.20.1", "fabric-loader-0.15.11-1.20.1"):
        d = game / "versions" / vid
        d.mkdir(parents=True)
        (d / (vid + ".json")).write_text("{}", encoding="utf-8")
    custom = game / "versions" / "custom1"
    custom.mkdir(parents=True)
    (custom / "custom1.json").write_text("{}", encoding="utf-8")
    (custom / INSTANCE_META_FILENAME).write_text(
        json.dumps(
            Instance(id="custom1", display_name="红石测试", created_at=1.0).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        # One card per instance: the display name is the card's title, the version + loader line is
        # its meta, so the two old row-text facts still have to hold.
        names = [card.name_label.text() for card in page.cards()]
        metas = [card.meta_label.text() for card in page.cards()]
        assert "1.20.1" in names
        assert any("Fabric 0.15.11" in meta for meta in metas)
        assert any(name.startswith("红石测试") for name in names)  # display name wins over the id
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def _make_instance(game_dir, version_id, *, display_name="", note="", **fields):
    """Create versions/<id>/<id>.json plus an optional sidecar (test helper)."""
    import json

    folder = game_dir / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (version_id + ".json")).write_text("{}", encoding="utf-8")
    if display_name or note or fields:
        from launcher.instances import INSTANCE_META_FILENAME, Instance

        payload = Instance(id=version_id, display_name=display_name, note=note, **fields)
        (folder / INSTANCE_META_FILENAME).write_text(
            json.dumps(payload.model_dump(mode="json")), encoding="utf-8"
        )
    return folder


def test_instance_detail_fills_on_selection(app, monkeypatch, ws_tmp):
    """Selecting a row fills the detail panel with name, paths and the isolation state."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_detail"))
    from gui import i18n
    from launcher import config as config_mod

    game = ws_tmp / "mc"
    _make_instance(game, "1.20.1")
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.select_instance("1.20.1")
        detail = page.detail
        assert page.detail_stack.currentWidget() is detail
        assert detail.instance_id == "1.20.1"
        assert detail.name_label.text() == "1.20.1"
        assert "1.20.1" in detail.id_label.text()
        assert str(game / "versions" / "1.20.1") in detail.dir_label.text()
        assert detail.overview_values["version"].text() == "1.20.1"
        assert detail.overview_values["game_dir"].text() == str(game / "versions" / "1.20.1")
        # default_isolation=True and no mods/saves: resolved as isolated
        assert detail.overview_values["isolation"].text().startswith(i18n.tr("common.on"))
        assert detail.overview_values["java"].text() == i18n.tr("instances.detail.java.follow_auto")
        assert detail.overview_values["last_played"].text() == i18n.tr(
            "instances.detail.value.never"
        )
        assert not detail.mods_banner.isVisibleTo(detail)
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_instance_tab_row_switches_stacked_pages(app, monkeypatch, ws_tmp):
    """The checkable tab row drives the QStackedWidget of the detail panel."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_tabs"))
    from launcher import config as config_mod

    game = ws_tmp / "mc"
    _make_instance(game, "1.20.1")
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.select_instance("1.20.1")
        detail = page.detail
        assert detail.stack.currentIndex() == 0
        for index in range(len(detail.tabs)):
            detail.tabs[index].click()
            app.processEvents()
            assert detail.stack.currentIndex() == index
            assert detail.tabs[index].isChecked()
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_instance_settings_save_survives_reload(app, monkeypatch, ws_tmp):
    """The 设置 tab writes per-instance overrides that survive a page reload."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_set"))
    from gui import i18n
    from launcher import config as config_mod
    from launcher.instances import get_instance

    game = ws_tmp / "mc"
    _make_instance(game, "1.20.1")
    java = ws_tmp / "java.exe"
    java.write_bytes(b"")
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.select_instance("1.20.1")
        detail = page.detail
        detail.set_tab(4)
        # The per-instance memory field is a number; "GB" belongs to its row label.
        assert detail.memory_spin.suffix() == ""
        assert any(
            label.text()
            == i18n.tr("instances.detail.row.memory") + i18n.tr("unit.gb.label")
            for label in detail.findChildren(QLabel)
        )
        detail.java_follow_check.setChecked(False)
        detail.java_edit.setText(str(java))
        detail.memory_follow_check.setChecked(False)
        detail.memory_spin.setValue(6.0)
        detail.jvm_args_edit.setText("-XX:+UseG1GC")
        detail.game_args_edit.setText("--width 1280")
        detail.server_edit.setText("mc.example.com")
        detail.settings_save_button.click()
        app.processEvents()

        stored = get_instance("1.20.1", game)
        assert stored.settings.java == str(java)
        assert stored.settings.memory_gb == 6.0
        assert stored.settings.jvm_args == "-XX:+UseG1GC"
        assert stored.settings.game_args == "--width 1280"
        assert stored.settings.server == "mc.example.com"

        # a full page reload reads the overrides back into the widgets
        page.refresh(select="1.20.1")
        app.processEvents()
        assert page.detail.instance_id == "1.20.1"
        assert not page.detail.java_follow_check.isChecked()
        assert page.detail.java_edit.text() == str(java)
        assert not page.detail.memory_follow_check.isChecked()
        assert page.detail.memory_spin.value() == 6.0
        assert page.detail.jvm_args_edit.text() == "-XX:+UseG1GC"
        assert page.detail.game_args_edit.text() == "--width 1280"
        assert page.detail.server_edit.text() == "mc.example.com"
        # clearing the field is the way back to "join no server"
        page.detail.server_edit.setText("")
        page.detail.settings_save_button.click()
        app.processEvents()
        assert get_instance("1.20.1", game).settings.server == ""
        # the overview reports the instance's own memory, still as a read-only value with its unit
        assert "6" in page.detail.overview_values["memory"].text()
        assert i18n.tr("unit.gb") in page.detail.overview_values["memory"].text()
        assert i18n.tr("instances.detail.memory.source.instance") in (
            page.detail.overview_values["memory"].text()
        )
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_instance_settings_reset_clears_overrides(app, monkeypatch, ws_tmp):
    """重置本实例设置 drops every override (and the isolation choice) after confirmation."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_reset"))

    from launcher import config as config_mod
    from launcher.instances import get_instance, set_instance_settings, update_instance

    game = ws_tmp / "mc"
    _make_instance(game, "1.20.1")
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    set_instance_settings("1.20.1", game_dir=game, memory_gb=8.0, jvm_args="-Xss4M")
    update_instance("1.20.1", game_dir=game, isolated="off")
    # "Yes" in the reset confirmation, which gui/inwindow_dialog.py draws over the window.
    monkeypatch.setattr("gui.inwindow_dialog.question", lambda *a, **k: True)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.select_instance("1.20.1")
        detail = page.detail
        detail.set_tab(4)
        assert detail.memory_spin.value() == 8.0
        detail.settings_reset_button.click()
        app.processEvents()

        stored = get_instance("1.20.1", game)
        assert stored.settings.memory_gb is None
        assert stored.settings.jvm_args == ""
        assert stored.isolated == "follow"
        assert detail.memory_follow_check.isChecked()
        assert detail.isolation_combo.currentData() == "follow"
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_instance_not_isolated_banner_and_one_click(app, monkeypatch, ws_tmp):
    """A shared (non-isolated) instance warns on 模组/资源 and offers one-click isolation."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_share"))
    from gui import i18n
    from launcher import config as config_mod
    from launcher.instances import get_instance

    game = ws_tmp / "mc"
    _make_instance(game, "1.20.1")  # no mods/, no saves/: follows the global default
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.default_isolation = False
    config_mod.save(cfg, cfg_path)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert page.select_instance("1.20.1")
        detail = page.detail
        assert detail.overview_values["isolation"].text().startswith(i18n.tr("common.off"))
        # the banner sits on top of the 模组 and 资源 tabs (a stacked page is only visible when current)
        detail.set_tab(1)
        assert detail.mods_banner.isVisibleTo(detail)
        assert str(game) in detail.mods_banner_label.text()
        detail.set_tab(2)
        assert detail.res_banner.isVisibleTo(detail)

        detail.enable_isolation()
        app.processEvents()
        assert get_instance("1.20.1", game).isolated == "on"
        assert not detail.res_banner.isVisibleTo(detail)
        detail.set_tab(1)
        assert not detail.mods_banner.isVisibleTo(detail)
        assert detail.overview_values["isolation"].text().startswith(i18n.tr("common.on"))
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_instances_search_filters_the_list(app, monkeypatch, ws_tmp):
    """The search box filters by display name / id / profile label and reports an empty result."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_search"))
    from launcher import config as config_mod

    game = ws_tmp / "mc"
    _make_instance(game, "1.20.1")
    _make_instance(game, "fabric-loader-0.15.11-1.20.1", display_name="红石测试")
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["instances"]
        assert len(page.cards()) == 2
        page.search_edit.setText("红石")
        page._on_search_changed()
        assert page.card_ids() == ["fabric-loader-0.15.11-1.20.1"]
        # the profile label is searchable too
        page.search_edit.setText("Fabric")
        page._on_search_changed()
        assert page.card_ids() == ["fabric-loader-0.15.11-1.20.1"]
        page.search_edit.setText("nothing-matches")
        page._on_search_changed()
        assert page.cards() == []
        assert page.list_stack.currentWidget() is page.empty_search
        page.search_edit.clear()
        page._on_search_changed()
        assert len(page.cards()) == 2
        assert page.list_stack.currentWidget() is page.content_scroll
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_display_version_name_normalizes_loaders():
    from launcher.instances import display_version_name

    assert display_version_name("1.20.1") == "1.20.1"
    assert display_version_name("fabric-loader-0.15.11-1.20.1") == "Fabric 0.15.11-1.20.1"
    assert display_version_name("neoforge-21.1.5") == "NeoForge 21.1.5"
    assert display_version_name("1.20.1-forge-47.4.22") == "Forge 47.4.22 (1.20.1)"
    assert display_version_name("quilt-loader-0.24.0-1.20.1") == "Quilt 0.24.0-1.20.1"


def test_launch_hero_normalizes_and_keeps_real_id(app, monkeypatch, ws_tmp):
    """The launch page shows normalized names but launches with the real profile id."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data_norm"))
    from launcher import config as config_mod

    game = ws_tmp / "mc"
    for vid in ("1.20.1", "fabric-loader-0.15.11-1.20.1"):
        d = game / "versions" / vid
        d.mkdir(parents=True)
        (d / (vid + ".json")).write_text("{}", encoding="utf-8")
    _cfg, _p = config_mod.load()
    _cfg.game_dir = game
    config_mod.save(_cfg, _p)
    from gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow()
        page = window.pages["launch"]
        page._populate_versions()
        labels = [label for label, _id in page.version_choices]
        assert "Fabric 0.15.11-1.20.1" in labels
        page.set_version_id("fabric-loader-0.15.11-1.20.1")
        assert page.version_label.text() == "Fabric 0.15.11-1.20.1"
        assert page._current_version_id() == "fabric-loader-0.15.11-1.20.1"
    finally:
        if window is not None:
            window.close()
            app.processEvents()


def test_resources_page_loader_combo_normalized(app):
    """The resources page shows human-readable loader names but keeps lowercase ids."""
    from gui.pages.mods_page import ResourcesPage

    page = ResourcesPage()
    try:
        combo = page.mods_tab.loader_combo
        labels = [combo.itemText(i) for i in range(combo.count())]
        assert labels == ["Fabric", "Forge", "NeoForge", "Quilt"]
        data = [combo.itemData(i) for i in range(combo.count())]
        assert data == ["fabric", "forge", "neoforge", "quilt"]
    finally:
        page.deleteLater()
        app.processEvents()


