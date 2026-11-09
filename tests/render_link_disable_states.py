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

"""Render the three 宸ュ叿 鈥?鑱旀満 states this change touches (zh_cn, light and dark).

The pictures prove the two reviews: the 鍒涘缓澶у巺 card no longer has a host-instance dropdown, and
鍋滅敤鑱旀満鍔熻兘 is a real confirmation plus the onboarding page with a completion line. Rendering goes
through tools/shot_pages.py's harness (offscreen platform, system CJK fonts registered before the
theme is applied, real sleeps while the event loop settles), the same pipeline every published
screenshot uses.

This is the screenshot half of tests/test_tools_page.py, deliberately not named ``test_*.py``: pytest
must not run a renderer. It writes nothing into the repository - the preview tree and the PNGs live
under the workspace tmp folder.

Usage:
    python tests/render_link_disable_states.py [--theme light|dark] [--out DIR]
"""

import os

# Before anything imports Qt: this renderer must never put a window on the user's desktop.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
WORK = ROOT.parent / "tmp" / "link_disable_states"
PREVIEW = WORK / "preview"
OUT = WORK / "shots"

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


def _settle(app, seconds: float = 0.5) -> None:
    for _ in range(max(1, int(seconds / 0.05))):
        app.processEvents()
        time.sleep(0.05)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", default="light", choices=("light", "dark"))
    parser.add_argument("--out", default="", help="directory for the PNGs (defaults to the tmp folder)")
    args = parser.parse_args()

    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

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
        game_dir=game_dir,
    )

    from PySide6.QtCore import QThreadPool
    from PySide6.QtGui import QFontDatabase

    from gui import i18n, inwindow_dialog
    from gui.main import create_app
    from gui.pages import java_page
    from gui.theme import apply_theme
    from launcher import config
    from launcher.link import FakeLinkSession, LinkState

    java_page.list_java = lambda *a, **k: []  # no Java probing subprocess during a render
    import gui.pages.about_page as about_page_module

    about_page_module.start_startup_update_check = lambda parent: None
    about_page_module.AboutPage.auto_check_on_start = lambda self: None

    app = create_app([])
    if i18n.current_language() != "zh_cn":
        raise SystemExit(f"asked for zh_cn, the application started in {i18n.current_language()!r}")
    for font_file in FONTS:
        if Path(font_file).exists():
            QFontDatabase.addApplicationFont(font_file)
    cfg, _ = config.load()
    apply_theme(cfg.theme, cfg.accent_color, cfg.ui_font, cfg.ui_radius)

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    window.resize(1000, 610)
    window.apply_window_mode()
    window.show()
    shot_pages.wait_for_workers(app, QThreadPool.globalInstance(), 5.0)
    window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
    _settle(app)

    page = window.pages["tools"]
    page.set_session(FakeLinkSession(state=LinkState.IDLE))
    _settle(app)

    out = Path(args.out) if args.out else OUT
    out.mkdir(parents=True, exist_ok=True)

    def save(name: str) -> None:
        shot_pages.defocus_for_shot(window, page)
        shot_pages.assert_no_focus_mark(window, page)
        image = shot_pages.render_page_image(window)
        target = out / f"zh_cn_{args.theme}_{name}.png"
        image.save(str(target))
        print(f"saved {target} ({image.width()}x{image.height()})")

    # 1. The 鍒涘缓澶у巺 card as it is now: steps, the automatic-detection note, and 鍒涘缓 - no picker.
    save("1_create_card")

    # 2. 鍋滅敤鑱旀満鍔熻兘's confirmation, built by the page itself (the same card the button shows).
    dialog = page._disable_link_dialog()
    dialog.show()
    _settle(app)
    shot_pages.defocus_for_shot(window, dialog)
    shot_pages.assert_no_focus_mark(window, dialog)
    image = shot_pages.render_page_image(window)
    target = out / f"zh_cn_{args.theme}_2_disable_confirm.png"
    image.save(str(target))
    print(f"saved {target} ({image.width()}x{image.height()})")
    dialog.close()
    _settle(app, 0.2)

    # 3. What the window shows after the deletion: the onboarding page plus the completion line on
    #    the status bar. The confirmation is answered by replacing its one blocking entry point, the
    #    same seam tests/conftest.py uses, so the product flow itself runs.
    original_ask = inwindow_dialog.InWindowMessage.ask
    inwindow_dialog.InWindowMessage.ask = lambda self: inwindow_dialog.Outcome.ACCEPTED
    try:
        page.disable_link.click()
    finally:
        inwindow_dialog.InWindowMessage.ask = original_ask
    _settle(app)
    if page.current_state() is not LinkState.NOT_DOWNLOADED:
        raise SystemExit(
            f"refusing to save: the page is still on {page.current_state()} after the deletion"
        )
    save("3_disabled")

    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
