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

"""Instance icons: the optional ``icon`` field, the picker, uploads and the fallback rule.

What this pins, in the order the feature reads:

* ``icon`` is optional metadata: old sidecars load with an empty value, a rule name round-trips,
  and a path-like ``custom:`` value is degraded to "automatic" instead of becoming a file read;
* the picker owns eight rule tiles (48px icons, Tab-reachable) plus 恢复自动 and 从文件选择…;
* an upload is copied into the instance folder, scaled to 512px, and a non-image is reported
  without crashing and without touching the stored choice;
* a custom value whose picture is missing or corrupt falls back to the rule and does **not** clear
  the metadata, while 恢复自动 does return to the rule (and removes the managed copy);
* a copy carries the icon along, and export/import keeps it inside the zip.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from gui import block_icons, i18n
from gui.pages import instance_detail as detail_module
from launcher import config as config_mod
from launcher.instances import (
    INSTANCE_META_FILENAME,
    create_instance,
    delete_instance,
    export_instance,
    get_instance,
    import_instance,
    instance_dir,
    set_instance_icon,
    update_instance,
)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _clean_icon_cache():
    i18n.set_language("zh_cn")
    block_icons.clear_cache()
    yield
    block_icons.clear_cache()


def _write_version(game: Path, version_id: str = "1.20.1") -> Path:
    """One installed profile: <id>/<id>.json plus a jar, as create_instance expects."""
    folder = game / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (version_id + ".json")).write_text(
        json.dumps(
            {
                "id": version_id,
                "type": "release",
                "mainClass": "net.minecraft.client.main.Main",
                "assetIndex": {"id": "5", "url": "https://example.invalid/5.json"},
                "assets": "5",
                "libraries": [],
                "downloads": {
                    "client": {
                        "url": "https://example.invalid/c.jar",
                        "sha1": "0" * 40,
                        "size": 1,
                        "path": "c.jar",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (folder / (version_id + ".jar")).write_bytes(b"jar")
    return folder


def _png_bytes(pixmap) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert pixmap.toImage().save(buffer, "PNG")
    return bytes(buffer.data())


def _picture(path: Path, width: int, height: int, colour: str = "#cc3344") -> Path:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    assert image.save(str(path))
    return path


def _settle(app: QApplication) -> None:
    for _ in range(8):
        app.processEvents()
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(6):
        app.processEvents()


# ---------------------------------------------------------------------------
# 1. the field
# ---------------------------------------------------------------------------


def test_icon_field_is_optional_and_round_trips(ws_tmp):
    """An old sidecar without ``icon`` loads, and a rule name survives a read/write cycle."""
    game = ws_tmp / "mc"
    folder = _write_version(game)
    # A sidecar written before this feature existed: no "icon" key at all.
    (folder / INSTANCE_META_FILENAME).write_text(
        json.dumps({"id": "1.20.1", "display_name": "旧实例", "note": "保留"}), encoding="utf-8"
    )
    old = get_instance("1.20.1", game)
    assert old is not None and old.icon == "" and old.display_name == "旧实例"

    update_instance("1.20.1", game_dir=game, icon="gold")
    stored = json.loads((folder / INSTANCE_META_FILENAME).read_text(encoding="utf-8"))
    assert stored["icon"] == "gold"
    assert get_instance("1.20.1", game).icon == "gold"

    set_instance_icon("1.20.1", "custom:icon.png", game)
    assert get_instance("1.20.1", game).icon == "custom:icon.png"


def test_path_like_custom_values_degrade_to_automatic(ws_tmp):
    """A hand-edited value must never become a path read outside the instance folder."""
    game = ws_tmp / "mc"
    _write_version(game)
    for bad in ("custom:../evil.png", "custom:", "custom:..", "custom:sub/icon.png"):
        set_instance_icon("1.20.1", bad, game)
        assert get_instance("1.20.1", game).icon == ""
    set_instance_icon("1.20.1", "  custom:icon.png  ", game)
    assert get_instance("1.20.1", game).icon == "custom:icon.png"


# ---------------------------------------------------------------------------
# 2. choosing a rule changes the card immediately
# ---------------------------------------------------------------------------


def _window(app, monkeypatch, ws_tmp):
    """A real MainWindow showing one instance in a temporary game directory."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "mc"))
    game = ws_tmp / "mc"
    _write_version(game)
    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    cfg.selected_instance = "1.20.1"
    config_mod.save(cfg, cfg_path)
    i18n.set_language("zh_cn")
    from gui.main_window import MainWindow

    window = MainWindow()
    window.resize(1180, 760)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    _settle(app)
    return window, game


def test_choosing_a_rule_updates_the_card_without_a_restart(app, monkeypatch, ws_tmp):
    """The picker writes the field; the list rebuilds from it, so the card changes at once."""
    window = None
    try:
        window, game = _window(app, monkeypatch, ws_tmp)
        from gui.main_window import NAV_KEYS

        window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        page = window.pages["instances"]
        page.select_instance("1.20.1")
        _settle(app)
        before = _png_bytes(page.card_for("1.20.1").icon_label.pixmap())

        def answer_with_fabric(dialog):
            dialog._pick_rule("fabric")
            return detail_module.inwindow_dialog.Outcome.DISMISSED

        monkeypatch.setattr(detail_module._IconChooserDialog, "ask", answer_with_fabric)
        page.detail.choose_icon()
        _settle(app)

        assert get_instance("1.20.1", game).icon == "fabric"
        after = _png_bytes(page.card_for("1.20.1").icon_label.pixmap())
        assert after != before, "the card still shows the automatic icon"
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
            app.processEvents()
        i18n.set_language("zh_cn")


# ---------------------------------------------------------------------------
# 3. the picker itself
# ---------------------------------------------------------------------------


def _chooser(app, *, current: str = ""):
    """The picker with real tile pixmaps, without entering its blocking loop."""
    parent = detail_module.InstanceDetail()
    previews = {
        rule: block_icons.rule_icon_pixmap(rule, detail_module.ICON_PICKER_ICON)
        for rule in block_icons.RULE_ICON_NAMES
    }
    picks: list[str] = []
    dialog = detail_module._IconChooserDialog(
        parent,
        current=current,
        previews=previews,
        auto_pixmap=block_icons.automatic_icon_pixmap("1.20.1", 48),
        current_pixmap=previews["grass"],
        on_pick=lambda value: picks.append(value) or True,
    )
    return parent, dialog, previews, picks


def test_picker_has_eight_48px_rule_tiles_and_two_actions(app):
    parent, dialog, previews, picks = _chooser(app)
    try:
        assert list(dialog.rule_buttons) == list(block_icons.RULE_ICON_NAMES)
        for rule, button in dialog.rule_buttons.items():
            assert button.iconSize() == QSize(48, 48), rule
            assert button.accessibleName() == i18n.tr("instances.icon.rule." + rule)
            assert button.toolTip() == button.accessibleName()
            # Keyboard walkthrough requirement: every tile is a Tab stop.
            assert button.focusPolicy() & Qt.FocusPolicy.TabFocus, rule
        assert dialog.auto_button.text() == i18n.tr("instances.icon.auto")
        assert dialog.file_button.text() == i18n.tr("instances.icon.file")
        for button in (dialog.auto_button, dialog.file_button):
            assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
        assert dialog.auto_button.objectName() == "secondaryButton"

        # Clicking a tile applies it through the callback and previews exactly that tile.
        dialog.rule_buttons["gold"].click()
        assert picks == ["gold"]
        assert dialog.current == "gold"
        assert _png_bytes(dialog.preview.pixmap()) == _png_bytes(previews["gold"])
    finally:
        dialog.deleteLater()
        parent.deleteLater()
        app.processEvents()


def test_picker_starts_on_the_rule_in_force(app):
    parent, dialog, _previews, _picks = _chooser(app, current="cobble")
    try:
        assert dialog.rule_buttons["cobble"].isChecked()
        assert not dialog.rule_buttons["grass"].isChecked()
        assert "cobble" in dialog.preview_caption.text() or "圆石" in dialog.preview_caption.text()
    finally:
        dialog.deleteLater()
        parent.deleteLater()
        app.processEvents()


def test_picker_tab_chain_reaches_every_control_and_never_leaves_the_card(app):
    """WCAG 2.1.1: the eight tiles and both actions are in the dialog's Tab cycle."""
    parent, dialog, _previews, _picks = _chooser(app)
    try:
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        app.processEvents()
        chain = dialog._focus_chain()
        expected = set(dialog.rule_buttons.values()) | {dialog.auto_button, dialog.file_button}
        assert set(chain) == expected
        assert len(chain) == 10
        for _ in range(12):  # more than one full cycle: Tab is consumed, never escapes the card
            assert dialog.focusNextPrevChild(True) is True
        assert dialog._focus_chain() == chain
    finally:
        dialog.close()
        dialog.deleteLater()
        parent.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# 4. uploads: copied in, scaled to 512, invalid files reported
# ---------------------------------------------------------------------------


def test_upload_is_copied_into_the_instance_folder_and_scaled_to_512(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    source = _picture(ws_tmp / "big.png", 1024, 600)

    value = detail_module.save_custom_icon(source, folder)

    assert value == "custom:icon.png"
    stored = folder / "icon.png"
    assert stored.is_file()
    image = QImage(str(stored))
    assert (image.width(), image.height()) == (512, 300)  # proportional: 1024x600 -> 512x300
    set_instance_icon("1.20.1", value, game)
    pixmap = block_icons.instance_icon_pixmap(get_instance("1.20.1", game), folder, 40)
    assert not pixmap.isNull()
    assert round(pixmap.width() / pixmap.devicePixelRatio()) == 40


def test_upload_keeps_the_file_format_and_a_small_picture_is_not_upscaled(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    source = _picture(ws_tmp / "small.jpg", 120, 80)
    value = detail_module.save_custom_icon(source, folder)
    assert value == "custom:icon.jpg"
    image = QImage(str(folder / "icon.jpg"))
    assert (image.width(), image.height()) == (120, 80)


def test_a_non_image_is_reported_without_touching_the_choice(app, monkeypatch, ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    bad = ws_tmp / "broken.png"
    bad.write_bytes(b"this is not a picture")
    with pytest.raises(ValueError):
        detail_module.save_custom_icon(bad, folder)

    # The page path: the native picker returns the broken file; the existing warning carries the error.
    detail = detail_module.InstanceDetail()
    warnings: list[tuple[str, str]] = []
    try:
        detail.instance_id = "1.20.1"
        detail.game_dir = game
        monkeypatch.setattr(
            detail_module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(bad), "")
        )
        monkeypatch.setattr(
            detail_module.inwindow_dialog,
            "warning",
            lambda parent, title, text: warnings.append((title, text)),
        )
        detail._choose_icon_file()  # must not raise
        assert warnings and warnings[0][0] == i18n.tr("instances.icon.title")
        assert get_instance("1.20.1", game).icon == ""
        assert not (folder / "icon.png").exists()
    finally:
        detail.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# 5. missing / corrupt pictures fall back without clearing the field
# ---------------------------------------------------------------------------


def test_missing_picture_falls_back_to_the_rule_and_keeps_the_config(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    set_instance_icon("1.20.1", "custom:missing.png", game)

    inst = get_instance("1.20.1", game)
    shown = block_icons.instance_icon_pixmap(inst, folder, 40)
    automatic = block_icons.automatic_icon_pixmap("1.20.1", 40)
    assert _png_bytes(shown) == _png_bytes(automatic)
    assert get_instance("1.20.1", game).icon == "custom:missing.png"  # not cleared


def test_corrupt_picture_falls_back_to_the_rule_and_keeps_the_config(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    (folder / "icon.png").write_bytes(b"definitely not a PNG")
    set_instance_icon("1.20.1", "custom:icon.png", game)

    shown = block_icons.instance_icon_pixmap(get_instance("1.20.1", game), folder, 40)
    automatic = block_icons.automatic_icon_pixmap("1.20.1", 40)
    assert _png_bytes(shown) == _png_bytes(automatic)
    assert get_instance("1.20.1", game).icon == "custom:icon.png"


# ---------------------------------------------------------------------------
# 6. restore automatic returns to the rule
# ---------------------------------------------------------------------------


def test_restore_automatic_returns_to_the_rule_and_removes_the_managed_copy(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    value = detail_module.save_custom_icon(_picture(ws_tmp / "pic.png", 300, 300), folder)
    set_instance_icon("1.20.1", value, game)
    assert (folder / "icon.png").is_file()

    set_instance_icon("1.20.1", "", game)
    assert get_instance("1.20.1", game).icon == ""
    assert not (folder / "icon.png").exists()  # the launcher-managed copy leaves with the choice
    assert _png_bytes(
        block_icons.instance_icon_pixmap(get_instance("1.20.1", game), folder, 40)
    ) == _png_bytes(block_icons.automatic_icon_pixmap("1.20.1", 40))


# ---------------------------------------------------------------------------
# 7. copy / export / import / delete
# ---------------------------------------------------------------------------


def test_a_copied_instance_carries_the_custom_icon(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    value = detail_module.save_custom_icon(_picture(ws_tmp / "pic.png", 200, 200), folder)
    set_instance_icon("1.20.1", value, game)

    copied = create_instance("副本", "1.20.1", game)

    assert copied.icon == "custom:icon.png"
    assert get_instance("副本", game).icon == "custom:icon.png"
    target = instance_dir(game, "副本") / "icon.png"
    assert target.is_file()
    assert target.read_bytes() == (folder / "icon.png").read_bytes()


def test_a_copied_instance_carries_a_rule_icon(ws_tmp):
    game = ws_tmp / "mc"
    _write_version(game)
    set_instance_icon("1.20.1", "fabric", game)
    copied = create_instance("副本", "1.20.1", game)
    assert copied.icon == "fabric"


def test_export_import_keeps_the_custom_picture(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    value = detail_module.save_custom_icon(_picture(ws_tmp / "pic.png", 260, 180), folder)
    set_instance_icon("1.20.1", value, game)
    dest = export_instance("1.20.1", ws_tmp / "out.zip", game)
    with zipfile.ZipFile(dest) as archive:
        assert "icon.png" in archive.namelist()

    delete_instance("1.20.1", game)
    imported = import_instance(dest, game)
    assert imported.icon == "custom:icon.png"
    assert (instance_dir(game, "1.20.1") / "icon.png").is_file()


def test_deleting_the_instance_removes_its_picture(ws_tmp):
    game = ws_tmp / "mc"
    folder = _write_version(game)
    value = detail_module.save_custom_icon(_picture(ws_tmp / "pic.png", 100, 100), folder)
    set_instance_icon("1.20.1", value, game)
    delete_instance("1.20.1", game)
    assert not folder.exists()


# ---------------------------------------------------------------------------
# 8. the detail page's own control
# ---------------------------------------------------------------------------


def test_detail_icon_button_follows_the_choice_and_restores_the_rule(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "mc"))
    game = ws_tmp / "mc"
    _write_version(game)
    detail = detail_module.InstanceDetail()
    changes: list[bool] = []
    try:
        detail.set_instance("1.20.1")
        assert detail.icon_button.isEnabled()
        automatic = _png_bytes(detail.icon_button.icon().pixmap(40, 40))

        assert detail._apply_icon("fabric") is True
        assert get_instance("1.20.1", game).icon == "fabric"
        pinned = _png_bytes(detail.icon_button.icon().pixmap(40, 40))
        assert pinned != automatic

        detail.changed.connect(lambda: changes.append(True))
        assert detail._apply_icon("") is True
        assert get_instance("1.20.1", game).icon == ""
        assert _png_bytes(detail.icon_button.icon().pixmap(40, 40)) == automatic
        assert changes, "the card list was never told the icon changed"
    finally:
        detail.deleteLater()
        app.processEvents()
