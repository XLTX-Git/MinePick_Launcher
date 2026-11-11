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

"""Render the About page's five update states (zh_cn, light and dark) offscreen.

This is the screenshot half of tests/test_about_update_ui.py: the states are the same ones the tests
drive, but captured through tools/shot_pages.py's harness (offscreen platform, system CJK fonts
registered before the theme is applied, real sleeps while the event loop settles) instead of asserted.
It writes nothing into the repository - the preview tree and the PNGs live under the workspace tmp
folder - and it is deliberately not named ``test_*.py``: pytest must not run a renderer.

Usage:
    python tests/render_about_update_states.py [--theme light|dark] [--out DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
WORK = ROOT.parent / "tmp" / "update_states"
OUT = WORK / "shots"
PREVIEW = WORK / "preview"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/deng.ttf",
    "C:/Windows/Fonts/consola.ttf",
)

RELEASE_NOTES = (
    "• 自动更新改为在后台下载，下载完成后由你决定何时重启安装\n"
    "• 更新文件在安装前校验 SHA-256 摘要，校验不通过绝不安装\n"
    "• 启动时自动清理上一次更新留下的旧文件\n"
    "• 修复了重启后提示“找不到指定模块”的问题"
)


def _release(updates):
    return updates.ReleaseInfo(
        version="0.6.0",
        tag="v0.6.0",
        page_url="https://github.com/XLTX-Git/MinePick_Launcher/releases/tag/v0.6.0",
        notes=RELEASE_NOTES,
        exe_url="https://example.invalid/MinePick_Launcher.exe",
        exe_name="MinePick_Launcher.exe",
        exe_size=48_200_000,
        exe_digest="sha256:" + "a" * 64,
        published_at="2026-11-01T09:30:00Z",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", default="light", choices=("light", "dark"))
    parser.add_argument("--out", default="", help="directory for the PNGs (defaults to the tmp folder)")
    args = parser.parse_args()

    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

    import shot_pages

    data_dir, game_dir = shot_pages.ensure_preview(PREVIEW)
    os.environ["MCLAUNCHER_DATA_DIR"] = str(data_dir)
    os.environ["MINECRAFT_GAME_DIR"] = str(game_dir)
    shot_pages.prepare_render_config(
        data_dir,
        language="zh_cn",
        theme=args.theme,
        accent="",
        font="",
        radius="default",
        title_bar="",
        game_dir=game_dir,
    )

    from PySide6.QtCore import QPoint, QThreadPool
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QFrame

    from gui import i18n
    from gui.main import create_app
    from gui.pages import java_page, mods_page, versions_page
    from gui.theme import apply_theme
    from launcher import config, updates
    from launcher.meta.manifest import ManifestVersion, VersionManifest

    manifest = VersionManifest(
        latest={"release": "1.21.11", "snapshot": "26w02a"},
        versions=[
            ManifestVersion(
                id=vid,
                type=vtype,
                url="https://example.invalid/version.json",
                time=f"{date}T12:00:00+00:00",
                release_time=f"{date}T12:00:00+00:00",
            )
            for vid, vtype, date in shot_pages.FAKE_VERSIONS
        ],
    )
    versions_page.fetch_manifest = lambda cache_path=None: manifest
    mods_page.search_projects = lambda *a, **k: []
    java_page.list_java = lambda *a, **k: []

    app = create_app([])
    if i18n.current_language() != "zh_cn":
        raise SystemExit(f"asked for zh_cn, the application started in {i18n.current_language()!r}")
    for font_file in FONTS:
        if Path(font_file).exists():
            QFontDatabase.addApplicationFont(font_file)
    cfg, _ = config.load()
    apply_theme(cfg.theme, cfg.accent_color, cfg.ui_font, cfg.ui_radius, cfg.title_bar_color)

    # The window schedules its startup update check 2.5s after construction. A renderer must not let
    # a real network check land in the middle of the state being captured (measured: the "ready"
    # picture was overwritten by "checking" once), so both startup entry points are switched off.
    import gui.pages.about_page as about_page_module

    about_page_module.start_startup_update_check = lambda parent: None
    about_page_module.AboutPage.auto_check_on_start = lambda self: None

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.resize(1000, 610)
    window.apply_window_mode()
    window.show()
    shot_pages.wait_for_workers(app, QThreadPool.globalInstance(), 5.0)
    window.sidebar.setCurrentRow(NAV_KEYS.index("about"))
    for _ in range(8):
        app.processEvents()
        time.sleep(0.08)

    page = window.pages["about"]
    page.auto_download.setChecked(False)  # "notify": a check leaves the page in the available state
    release = _release(updates)
    updates.can_self_update = lambda: True  # the packaged UI state; this process is a source checkout
    card = page.findChildren(QFrame, "pageCard")[1]

    def apply_state(name: str) -> None:
        page.release_box.setVisible(False)
        page._release = None
        page._notes_expanded = False
        page.notes_label.setVisible(False)
        page.notes_button.setText(i18n.tr("about.update.notes.show"))
        page.latest_label.setText(i18n.tr("about.update.latest_unknown"))
        if name == "1_available":
            page._show_available(release)
            page._toggle_notes()  # show the foldable release notes
        elif name == "2_downloading":
            page._show_available(release)
            page._state = "downloading"
            page._cancel = threading.Event()
            page._sync_buttons()
            page._on_download_progress((462, 1000))
        elif name == "3_ready":
            page._show_available(release)
            page._show_ready("0.6.0")
        elif name == "4_up_to_date":
            page.latest_label.setText(i18n.tr("about.update.latest", "0.6.0"))
            page._show_up_to_date("2026-11-08 20:20")
        elif name == "5_failed":
            page._show_available(release)
            page._show_failure("signature")

    out = Path(args.out) if args.out else OUT
    out.mkdir(parents=True, exist_ok=True)
    for name in ("1_available", "2_downloading", "3_ready", "4_up_to_date", "5_failed"):
        apply_state(name)
        inner = page.scroll.widget()
        bar = page.scroll.verticalScrollBar()
        bar.setValue(max(bar.minimum(), card.mapTo(inner, QPoint(0, 0)).y() - 8))
        for _ in range(10):
            app.processEvents()
            time.sleep(0.06)
        shot_pages.defocus_for_shot(window, page)
        shot_pages.assert_no_focus_mark(window, page)
        image = shot_pages.render_page_image(window)
        target = out / f"zh_cn_{args.theme}_{name}.png"
        image.save(str(target))
        print(f"saved {target} ({image.width()}x{image.height()})")

    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
