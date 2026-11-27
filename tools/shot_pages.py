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

# Page renderer: render every page offscreen to PNG for the README screenshots and for
# eye-checking layout work. Reads a project tree read-only; all preview config/data live
# inside the preview folder.

"""Offscreen page renderer for the launcher UI.

``--warm-cache`` fills the project-icon disk cache before the window is built, so the resource rows
paint real icons instead of letter tiles; ``--expect-icons`` refuses to save any page whose rows are
still showing a tile. They can be combined in one run: warm first, then render and verify.

Pictures are rendered with :func:`render_page_image`, not ``QWidget.grab()``: the grab pixmap's
unpainted pixels belong to the platform style, not to the page. Qt fills them with the palette window
background (measured #efefef with no stylesheet, BG_WINDOW under the theme), and a translucent widget
leaves them black - none of those is the ``BG_PAGE`` paper the stylesheet defines. Rendering into an
opaque QImage that starts as the current theme's ``BG_PAGE`` decides every such pixel here, on every
platform, and keeps the PNG opaque.

The white corners that prompted this were a separate bug: the demo iris icon was an opaque 400x400
white PNG (see ``FAKE_MOD_ICONS``). The prefill is the belt for the next unpainted pixel, not the
repair for that one.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # never drop __pycache__ into the tree being rendered


FAKE_MODS = [
    ("sodium", "Sodium", "Modern rendering engine and client-side optimization mod", 231_450_000),
    ("iris", "Iris Shaders", "A modern shaders mod compatible with OptiFine shaderpacks", 48_120_000),
    ("lithium", "Lithium", "No-compromises game logic and performance optimization mod", 92_300_000),
    ("fabric-api", "Fabric API", "Lightweight and modular API providing common hooks and intercompatibility", 187_900_000),
    ("jei", "Just Enough Items", "View items and recipes for anything in the game", 210_770_000),
    ("modmenu", "Mod Menu", "Adds a mod menu to view the list of mods you have installed", 120_540_000),
    ("cloth-config", "Cloth Config API", "Configuration screen API used by many fabric mods", 165_880_000),
    ("roughly-enough-items", "Roughly Enough Items", "Recipe viewer with a modern look and feel", 61_230_000),
    ("create", "Create", "Aesthetic technology mod with rotational power and contraptions", 74_610_000),
    ("xaeros-minimap", "Xaero's Minimap", "Minimap with waypoints, entity radar and cave mode", 88_940_000),
    ("appleskin", "AppleSkin", "Food-related HUD information: hunger, saturation and exhaustion", 57_330_000),
    ("journeymap", "JourneyMap", "Real-time mapping in game or in a web browser", 69_150_000),
]

#: Real project icons for the demo rows, keyed by slug. The resource page is the only rendered page
#: whose rows carry an image, and a letter tile is not a usable README screenshot. Kept out of
#: ``FAKE_MODS`` so the four-tuple stays the shared contract for the other render/measure tools.
FAKE_MOD_ICONS = {
    "sodium": "https://cdn.modrinth.com/data/AANobbMI/icon.png",
    # Not ``icon.png``: that file is an opaque 400x400 PNG with a white background (colour type 2,
    # no alpha), so its square stays white on the card. The URL the search API actually hands the
    # launcher is this transparent _96.webp; it is what a real screenshot shows, and its transparent
    # corners are what make the card colour visible.
    "iris": (
        "https://cdn.modrinth.com/data/YL57xq9U/"
        "18d0e7f076d3d6ed5bedd472b853909aac5da202_96.webp"
    ),
    "lithium": "https://cdn.modrinth.com/data/gvQqBUqZ/icon.png",
    "fabric-api": "https://cdn.modrinth.com/data/P7dR8mSH/icon.png",
    "jei": (
        "https://cdn.modrinth.com/data/u6dRKJwZ/"
        "4a3f18ac0d096c9f8e9176984c44be4e58f94c89_96.webp"
    ),
    "modmenu": (
        "https://cdn.modrinth.com/data/mOgUt4GM/"
        "5a20ed1450a0e1e79a1fe04e61bb4e5878bf1d20.png"
    ),
    "cloth-config": "https://cdn.modrinth.com/data/9s6osm5g/icon.png",
    "roughly-enough-items": "https://cdn.modrinth.com/data/nfn13YXA/icon.png",
    "create": (
        "https://cdn.modrinth.com/data/LNytGWDc/"
        "61d716699bcf1ec42ed4926a9e1c7311be6087e2_96.webp"
    ),
    "xaeros-minimap": (
        "https://cdn.modrinth.com/data/1bokaNcj/"
        "354080f65407e49f486fcf9c4580e82c45ae63b8_96.webp"
    ),
    "appleskin": "https://cdn.modrinth.com/data/EsAfCjCV/icon.png",
    "journeymap": (
        "https://cdn.modrinth.com/data/lfHFW1mp/"
        "a1c571a21a88f6fa59eab67829f216f65ab393ee_96.webp"
    ),
}

#: The icon prefetch is network-bound; eight at a time keeps a cold run short without opening a
#: connection per row at once. Each URL is retried because this project's CDN is occasionally slow.
ICON_WARM_CONCURRENCY = 8
ICON_WARM_ATTEMPTS = 3


FAKE_VERSIONS = [
    ("1.21.11", "release", "2025-12-09"),
    ("1.21.10", "release", "2025-11-25"),
    ("1.21.9", "release", "2025-11-11"),
    ("1.21.8", "release", "2025-09-30"),
    ("1.21.7", "release", "2025-08-19"),
    ("1.21.6", "release", "2025-07-15"),
    ("1.21.5", "release", "2025-06-03"),
    ("1.21.4", "release", "2025-03-25"),
    ("1.21.3", "release", "2025-02-11"),
    ("1.21.1", "release", "2024-08-08"),
    ("1.21", "release", "2024-06-13"),
    ("1.20.6", "release", "2024-04-29"),
    ("1.20.4", "release", "2023-12-07"),
    ("1.20.2", "release", "2023-09-21"),
    ("1.20.1", "release", "2023-06-12"),
    ("1.19.4", "release", "2023-03-14"),
    ("1.18.2", "release", "2022-02-28"),
    ("1.16.5", "release", "2021-01-15"),
    ("1.12.2", "release", "2017-09-18"),
    ("1.8.9", "release", "2015-12-09"),
    ("26w02a", "snapshot", "2026-01-13"),
    ("25w45a", "snapshot", "2025-11-04"),
    ("24w14potato", "snapshot", "2024-04-01"),
    ("23w13a_or_b", "snapshot", "2023-04-01"),
    ("22w13oneblockatatime", "snapshot", "2022-04-01"),
    ("20w14infinite", "snapshot", "2020-04-01"),
    ("1.RV-Pre1", "snapshot", "2016-03-31"),
    ("b1.7.3", "old_beta", "2011-07-08"),
    ("a1.2.6", "old_alpha", "2010-12-02"),
]


def ensure_preview(root: Path, game_dir: Path | None = None) -> tuple[Path, Path]:
    """Create the preview data/game directories used by the renderer.

    ``game_dir`` overrides where the demo game directory lives. The settings page shows that path,
    and the pictures end up in the READMEs and the source package, so a render for the release
    passes a neutral directory to keep the build machine's paths out of the screenshots.
    """
    preview = root / "_preview"
    data = preview / "data"
    game = Path(game_dir) if game_dir is not None else preview / "game"
    # Managed Java runtimes are detected by directory only (runtime/java-<major>), so the
    # preview never plants a fake java.exe: the launcher would probe it and Windows would
    # pop "unsupported 16-bit application" dialogs for the bogus executable.
    (data / "runtime" / "java-8" / "bin").mkdir(parents=True, exist_ok=True)
    (data / "runtime" / "java-21" / "bin").mkdir(parents=True, exist_ok=True)
    (data / "runtime" / "java-8" / "release").write_text(
        "IMPLEMENTOR=\"Preview\"\nJAVA_VERSION=\"1.8.0_999\"\n", encoding="utf-8"
    )
    (data / "runtime" / "java-21" / "release").write_text(
        "IMPLEMENTOR=\"Preview\"\nJAVA_VERSION=\"21.0.9\"\n", encoding="utf-8"
    )
    (data / "runtime" / "java-8" / "bin" / "preview-placeholder.txt").write_text(
        "Preview stub: no executable here on purpose (see tools/shot_pages.py).\n",
        encoding="utf-8",
    )
    (data / "runtime" / "java-21" / "bin" / "preview-placeholder.txt").write_text(
        "Preview stub: no executable here on purpose (see tools/shot_pages.py).\n",
        encoding="utf-8",
    )

    versions = [
        "1.20.1",
        "1.21.11",
        "fabric-loader-0.19.3-1.21.11",
        "1.20.1-forge-47.4.22",
        "neoforge-21.1.5",
    ]
    for vid in versions:
        vdir = game / "versions" / vid
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{vid}.json").write_text(
            json.dumps({"id": vid, "mainClass": "net.minecraft.client.main.Main"}),
            encoding="utf-8",
        )

    # Instances *are* version folders now (0.3.0): two of them carry a display name, a note and
    # their own mods/saves, which is exactly what makes them isolate themselves (see
    # launcher.instances.isolation_enabled), so the screenshots show real per-instance data.
    demo_instances = [
        ("整合包演示", "fabric-loader-0.19.3-1.21.11", "Fabulously Optimized 整合包，含性能模组", True),
        ("生存存档", "1.20.1", "长期存档，装了 JEI 和 Create", False),
    ]
    for name, parent_id, note, star in demo_instances:
        idir = game / "versions" / name
        (idir / "mods").mkdir(parents=True, exist_ok=True)
        (idir / "saves").mkdir(parents=True, exist_ok=True)
        (idir / f"{name}.json").write_text(
            json.dumps(
                {"id": name, "inheritsFrom": parent_id, "mainClass": "net.minecraft.client.main.Main"}
            ),
            encoding="utf-8",
        )
        (idir / "minepick.json").write_text(
            json.dumps(
                {
                    "id": name,
                    "display_name": name,
                    "note": note,
                    "star": star,
                    "created_at": time.time() - 86400 * 9,
                    "isolated": "follow",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    for jar in ("sodium-fabric-0.6.5.jar", "lithium-fabric-0.15.3.jar", "iris-fabric-1.8.1.jar"):
        (game / "versions" / "整合包演示" / "mods" / jar).write_bytes(b"PK\x03\x04trial")
    world = game / "versions" / "生存存档" / "saves" / "新世界"
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"\x0a\x00\x00")
    # The pre-0.3.0 layout must not linger in the preview: the overview would otherwise show
    # the "old instances/ folder detected" notice in every published screenshot.
    legacy = game / "instances"
    if legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)
    return data, game


def _write_preview_screenshot(path: Path, *, horizon: int, sun: int) -> None:
    """Paint one small placeholder picture (no fonts: this runs before/without a UI font pass)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter

    image = QImage(320, 180, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    sky = QLinearGradient(QPointF(0, 0), QPointF(0, 180))
    sky.setColorAt(0.0, QColor("#5f9bd0"))
    sky.setColorAt(1.0, QColor("#c9e6f5"))
    painter.fillRect(image.rect(), sky)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#f4e2a1"))
    painter.drawRect(sun, 24, 30, 30)
    painter.setBrush(QColor("#5c8f3f"))
    painter.drawRect(0, horizon, 320, 180 - horizon)
    painter.setBrush(QColor("#7a5a37"))
    painter.drawRect(0, 162, 320, 18)
    painter.setBrush(QColor("#3f6ea8"))
    painter.drawRect(36, horizon - 34, 56, 34)
    painter.setBrush(QColor("#d8dde3"))
    painter.drawRect(44, horizon - 26, 16, 14)
    painter.setBrush(QColor("#8a4b2f"))
    painter.drawRect(232, horizon - 46, 10, 46)
    painter.setBrush(QColor("#3f7d35"))
    painter.drawRect(210, horizon - 70, 54, 28)
    painter.end()
    image.save(str(path))


def seed_preview_media(game_dir: Path) -> None:
    """Give the two demo instances screenshots and servers.dat so the section shots have content.

    Runs after ``create_app()`` (QPainter needs the GUI application for its raster engine) and before
    the window exists. Everything is rewritten on every render, so a stale picture cannot leak into a
    new one. The mtimes are pinned: the shared folder gets the newest picture, which is also what the
    row order and the "source" column are supposed to demonstrate.
    """
    from launcher import servers

    shared = game_dir / "screenshots"
    shared.mkdir(parents=True, exist_ok=True)
    for index, (name, when, horizon, sun) in enumerate(
        (
            ("2026-11-08_20.31.12.png", 1_762_600_000, 132, 244),
            ("2026-11-08_17.05.44.png", 1_762_588_000, 126, 36),
        )
    ):
        target = shared / name
        _write_preview_screenshot(target, horizon=horizon, sun=sun)
        os.utime(target, (when, when))

    for name, base, horizon, sun in (
        ("整合包演示", 1_762_580_000, 120, 190),
        ("生存存档", 1_762_570_000, 140, 70),
    ):
        own = game_dir / "versions" / name / "screenshots"
        own.mkdir(parents=True, exist_ok=True)
        target = own / f"2026-11-08_{'19' if name == '整合包演示' else '16'}.42.09.png"
        _write_preview_screenshot(target, horizon=horizon, sun=sun)
        os.utime(target, (base, base))
        servers.write_servers(
            servers.servers_path(own.parent),
            servers.ServersFile(
                entries=[
                    servers.ServerEntry(name="Hypixel", ip="mc.hypixel.net"),
                    servers.ServerEntry(name="本地测试服", ip="127.0.0.1:25565"),
                    servers.ServerEntry(name="朋友的世界", ip="play.example.net:25565"),
                ]
            ),
        )

    servers.write_servers(
        servers.servers_path(game_dir),
        servers.ServersFile(
            entries=[
                servers.ServerEntry(name="2b2t", ip="connect.2b2t.org"),
                servers.ServerEntry(name="Hypixel", ip="mc.hypixel.net"),
            ]
        ),
    )


#: The launcher's default interface language, and the language a render uses when ``--language`` is
#: not given. A fixed default is the point: the preview data dir is shared by every render, so "keep
#: whatever the last run left on disk" is exactly how a German render used to turn the next one German.
DEFAULT_LANGUAGE = "zh_cn"


def prepare_render_config(
    data_dir: Path,
    *,
    language: str,
    theme: str,
    accent: str,
    font: str,
    radius: str,
    title_bar: str = "",
    game_dir: Path,
) -> None:
    """Write every render-deciding config key **before** the application is created.

    ``create_app()`` calls ``config.load()`` itself, and it reads both ``ui_language`` and
    ``wizard_done`` at that moment - too early for a write that happens later in this script (the old
    code wrote theme/font/radius after ``create_app`` and never wrote the language at all). The preview
    data dir is per ``--preview`` tree but shared by consecutive renders, so each render has to state
    its own values instead of inheriting the previous run's.

    ``wizard_done`` belongs in this list because while it is false ``create_app()`` overwrites
    ``ui_language`` with the system language ([P21]): a render that writes the language but not this
    flag would still come out in the machine's language. ``game_language`` and
    ``language_initialized`` are here for the same kind of reason: the settings page shows the former,
    and the latter is what stops the one-time auto-initializer from writing a different game language
    after this function has already set it.
    """
    from launcher import config

    cfg, cfg_path = config.load(data_dir / config.CONFIG_FILENAME)
    cfg.ui_language = language
    # The settings page also displays game_language, and create_app() initializes it once from
    # ui_language on first launch - i.e. the first language ever rendered would otherwise be pinned
    # into every later language's screenshot. Write both, and tell the one-time initializer that it
    # is done so it cannot write a different language after this function returns.
    cfg.game_language = language
    cfg.language_initialized = True
    cfg.wizard_done = True
    cfg.game_dir = str(game_dir)
    cfg.theme = theme
    cfg.accent_color = accent
    cfg.title_bar_color = title_bar
    cfg.ui_font = font
    cfg.ui_radius = radius
    config.save(cfg, cfg_path)


def wait_for_workers(app, pool, seconds: float = 20.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        if pool.waitForDone(120):
            break
    for _ in range(6):
        app.processEvents()
        time.sleep(0.05)


def wait_until(pred, timeout_ms: int = 8000) -> bool:
    """Pump the Qt event loop until ``pred()`` is true, or return False on the deadline.

    Rendering cannot use ``QThreadPool.waitForDone`` alone for this: a worker's result reaches the
    GUI through a queued signal, so the last hop only happens while the event loop is pumped. This
    helper is the small, testable loop around that - it returns the predicate's final value instead
    of raising, so a caller can decide whether a timeout is fatal (``--expect-icons``) or not.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return bool(pred())
        if app is not None:
            app.processEvents()
        time.sleep(0.02)


def resource_rows(page) -> list:
    """Every ``_ResourceRow`` the page currently holds (only the resource pages have any)."""
    from gui.pages.mods_page import _ResourceRow

    return list(page.findChildren(_ResourceRow))


def missing_icons(page) -> list:
    """Rows whose project icon never arrived; these are exactly the rows still painting a letter tile."""
    return [row for row in resource_rows(page) if row._icon is None]


def icons_ready(page) -> bool:
    """True when every resource row on the page has a real project icon."""
    return not missing_icons(page)


def assert_icons_loaded(page, where: str) -> None:
    """Refuse to save a picture whose rows still show letter tiles."""
    rows = resource_rows(page)
    missing = [row for row in rows if row._icon is None]
    if not missing:
        return
    titles = ", ".join(str(getattr(row, "title", "?")) for row in missing[:8])
    raise SystemExit(
        f"refusing to save {where}: {len(missing)} of {len(rows)} resource icon(s) did not load "
        f"({titles}); use --warm-cache or fix the icon URL"
    )


def warm_icon_cache(urls, *, workers: int = ICON_WARM_CONCURRENCY) -> None:
    """Download every URL into the project-icon disk cache before the window is built.

    ``launcher.net.http_cache.fetch_icon_bytes`` is the same store the resource rows read, so once
    this returns the rows paint real icons synchronously (``_ContentTab._load_icon`` peeks at the
    cache first). Work is bounded: at most ``workers`` requests run at a time, and a URL that fails
    is retried a few times. A URL that still fails is fatal - rendering it would produce the letter
    tile this option exists to avoid.
    """
    from concurrent.futures import ThreadPoolExecutor

    from launcher.net.http_cache import fetch_icon_bytes

    unique = list(dict.fromkeys(str(url) for url in urls if url))
    if not unique:
        return

    def fetch(url: str):
        last: Exception | None = None
        for attempt in range(ICON_WARM_ATTEMPTS):
            try:
                fetch_icon_bytes(url, timeout=8.0)
                return url, None
            except Exception as exc:  # noqa: BLE001 - report what the network said, then retry
                last = exc
                time.sleep(0.5 * (attempt + 1))
        return url, last

    failures: list[tuple[str, Exception | None]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(unique)))) as pool:
        for url, error in pool.map(fetch, unique):
            if error is None:
                print(f"warm-cache: {url}")
            else:
                failures.append((url, error))
                print(f"warm-cache: FAILED {url}: {error}")
    if failures:
        raise SystemExit(
            f"warm-cache: {len(failures)} of {len(unique)} icon(s) could not be cached; "
            "refusing to render a page that would show letter tiles"
        )
    print(f"warm-cache: {len(unique)} icon(s) ready")


def _scroll_page_to(page, fraction: float) -> None:
    """Scroll a page's QScrollArea to a fraction of its range (0 = top, 1 = bottom)."""
    from PySide6.QtWidgets import QScrollArea

    value = max(0.0, min(1.0, float(fraction)))
    for area in page.findChildren(QScrollArea):
        bar = area.verticalScrollBar()
        bar.setValue(round(bar.maximum() * value))


def _scroll_page_to_bottom(page) -> None:
    """Scroll a page's QScrollArea to the end (settings page) so the lower rows are visible."""
    _scroll_page_to(page, 1.0)


#: Property the focus ring is gated on, set by ``gui.widgets.KeyboardFocusTracker`` (see the
#: ``[keyboardFocus="true"]`` block at the end of the stylesheets). A published picture must never
#: carry it: the ring means "the keyboard put focus here", and a renderer has no user.
FOCUS_MARK = "keyboardFocus"


def _shot_widgets(root) -> list:
    """``root`` plus its descendant widgets, skipping anything that is not a widget at all."""
    from PySide6.QtWidgets import QWidget

    if not isinstance(root, QWidget):
        return []
    return [root, *root.findChildren(QWidget)]


def marked_widgets(root):
    """Every widget at or below ``root`` still carrying the keyboard-focus mark.

    Read-only, and deliberately independent of the tracker: it looks at the property itself, so a mark
    set by hand - or by a tracker this process never installed - is found exactly the same way.
    """
    from PySide6.QtWidgets import QApplication

    widgets = _shot_widgets(root)
    found = []
    for widget in widgets:
        try:
            if bool(widget.property(FOCUS_MARK)):
                found.append(widget)
        except RuntimeError:  # a wrapper whose C++ side is already gone
            continue
    focused = QApplication.focusWidget() if QApplication.instance() is not None else None
    if focused is not None and all(w is not focused for w in widgets):
        widgets.append(focused)
        if bool(focused.property(FOCUS_MARK)):
            found.append(focused)
    return found


def describe_widget(widget) -> str:
    """``QListWidget#sidebar``-style label for a message a human has to act on."""
    if widget is None:
        return "None"
    name = widget.objectName()
    kind = type(widget).__name__
    return f"{kind}#{name}" if name else kind


def defocus_for_shot(*roots) -> None:
    """Take every trace of focus out of the widgets before a grab.

    Two separate things draw a focus ring, and a screenshot must have neither:

    * the *focus itself* - a window that comes up puts focus on the first control in the tab order
      (the navigation rail, which is where a plain ``:focus`` rule would light up), and a dialog puts it
      on its default button. The focused widget is handed back with ``clearFocus()``, which leaves the
      window with no focused control at all rather than moving the focus to another one that would then
      draw its own ring;
    * the ``keyboardFocus`` *mark* the focus ring is really gated on, which is a property and therefore
      outlives the focus that caused it. Every widget is unmarked with the same
      ``setProperty`` + ``unpolish``/``polish`` pair ``gui.widgets.KeyboardFocusTracker._mark`` uses, so
      the repaint is exactly the one the tracker itself would produce.

    Also clears the renderer's own leftovers: a widget marked by an earlier grab would otherwise keep
    its ring in the next picture.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        focused = app.focusWidget()
        if focused is not None:
            focused.clearFocus()
    seen = []
    for root in roots:
        seen.extend(_shot_widgets(root))
    focused = app.focusWidget() if app is not None else None
    if focused is not None:
        seen.append(focused)
    for widget in seen:
        try:
            if not bool(widget.property(FOCUS_MARK)):
                continue
            widget.setProperty(FOCUS_MARK, False)
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)
        except RuntimeError:  # dead wrapper: nothing left to unmark
            continue
    # give the unpolish/polish repaints their turn before the pixels are read
    for _ in range(4):
        app.processEvents()


def assert_no_focus_mark(*roots) -> None:
    """Fail loudly if any widget in the tree still carries the keyboard-focus mark.

    This is the guard, not a review step: the pictures are referenced by nine READMEs, so a focus ring
    that reached one of them used to be found by a human eye, after publication. From here on a ring
    that a *renderer* can produce at all stops the render with the offending widget named, which is a
    failure the person who introduced it sees immediately.
    """
    marked = marked_widgets(roots[0] if roots else None)
    for extra in roots[1:]:
        for widget in marked_widgets(extra):
            if all(w is not widget for w in marked):
                marked.append(widget)
    if not marked:
        return
    names = ", ".join(sorted({describe_widget(w) for w in marked}))
    raise SystemExit(
        f"refusing to save: {len(marked)} widget(s) still carry {FOCUS_MARK}=true "
        f"({names}); the picture would carry a keyboard focus ring. "
        "Call defocus_for_shot() before rendering, and find who set the mark."
    )


def page_paper_color():
    """The page background of the theme currently in force (the ``BG_PAGE`` token).

    Never a hard-coded white: the token is the same value the stylesheet paints every page with, so a
    light render gets the light paper and a dark render the dark one.
    """
    from PySide6.QtGui import QColor

    from gui import theme

    return QColor(theme.current_tokens().get("BG_PAGE", "#0a1622"))


def show_preview_capsule(window) -> None:
    """Put the real 联机中 capsule in the title bar for a render.

    The capsule is hidden until a room exists, which a screenshot machine never has. Showing the
    product's own widget (rather than painting a look-alike) is what makes a title-bar render prove
    that the capsule's derived fill and text are readable on a custom bar.
    """
    from gui import i18n

    text = i18n.tr("link.badge.active", "U/8F3K")
    window.link_capsule.setText(text)
    window.link_capsule.setAccessibleName(text)
    window.link_capsule.setToolTip(i18n.tr("link.badge.tooltip"))
    window.link_capsule.show()


def render_page_image(widget):
    """Render ``widget`` into an opaque QImage pre-filled with the page paper colour.

    ``QWidget.grab()`` hands back a pixmap that Qt first fills with the palette window background, so
    a pixel the widget tree never paints keeps a platform/style colour instead of a colour this
    screenshot's own stylesheet defines - black, for a widget that sets ``WA_TranslucentBackground``.
    Rendering into an image that starts as the current ``BG_PAGE`` makes those pixels the paper:
    every widget that does paint (the card fill under an icon included) still overwrites it, and the
    image is opaque, so the saved PNG cannot be re-composited against white by a later viewer.
    """
    from PySide6.QtGui import QImage

    ratio = float(widget.devicePixelRatioF()) or 1.0
    width = max(1, round(widget.width() * ratio))
    height = max(1, round(widget.height() * ratio))
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.setDevicePixelRatio(ratio)
    image.fill(page_paper_color())
    widget.render(image)
    return image


def main() -> int:
    sys.stdout.reconfigure(errors="replace")  # never crash on console encoding
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="project tree to import from")
    parser.add_argument("--out", required=True, help="output directory for PNGs")
    parser.add_argument(
        "--language",
        "--lang",
        default=DEFAULT_LANGUAGE,
        help=(
            "interface language to render; written into the preview config before the app is created "
            f"(default: {DEFAULT_LANGUAGE})"
        ),
    )
    parser.add_argument("--theme", default="dark", choices=["dark", "light"])
    parser.add_argument("--label", default="", help="filename prefix")
    parser.add_argument("--accent", default="", help="custom accent color, e.g. #3b82f6")
    parser.add_argument(
        "--title-bar",
        default="",
        help="custom title bar color, e.g. #e08a3c; empty follows the theme",
    )
    parser.add_argument("--font", default="", help="custom UI font family")
    parser.add_argument("--radius", default="", help="corner radius preset: compact / default / round")
    parser.add_argument(
        "--link-capsule",
        action="store_true",
        help="show the 联机中 capsule in the title bar for the render (preview only)",
    )
    parser.add_argument(
        "--scroll-bottom", action="store_true", help="scroll the page to the bottom before rendering"
    )
    parser.add_argument(
        "--scroll",
        type=float,
        default=None,
        help="scroll a page to this fraction of its range before rendering (0 = top, 1 = bottom)",
    )
    parser.add_argument("--pages", default="", help="comma separated page subset, e.g. launch,settings")
    parser.add_argument(
        "--game-dir",
        default="",
        help="show this game directory instead of the preview folder (keeps build paths out of published screenshots)",
    )
    parser.add_argument(
        "--dialog-shot",
        action="store_true",
        help="also render the crash diagnosis dialog over the instances page",
    )
    parser.add_argument(
        "--detail-shot",
        action="store_true",
        help="also render the instance detail page (next index after the nav pages) for the selected demo instance",
    )
    parser.add_argument(
        "--detail-sections",
        action="store_true",
        help="also render the instance detail with 截图 and 服务器 selected (implies --detail-shot)",
    )
    parser.add_argument(
        "--tools-states",
        action="store_true",
        help="also render the six reviewed 联机 layouts of the tools page",
    )
    parser.add_argument(
        "--warm-cache",
        action="store_true",
        help="download every result icon into the disk cache before building the window",
    )
    parser.add_argument(
        "--expect-icons",
        action="store_true",
        help="refuse to save a page while any of its result icons is still a letter tile",
    )
    parser.add_argument(
        "--preview",
        default="",
        help="where to keep _preview data (defaults to --root; point at the work folder when rendering the released tree)",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(root))
    os.chdir(root)

    data_dir, game_dir = ensure_preview(
        Path(args.preview).resolve() if args.preview else root,
        # Deliberately not resolved: the value is what the screenshots *display*, and a relative
        # name keeps the build machine's path out of published pictures without needing write
        # access outside the checkout (`--game-dir Minecraft` shows "Minecraft" in the UI).
        Path(args.game_dir) if args.game_dir else None,
    )
    os.environ["MCLAUNCHER_DATA_DIR"] = str(data_dir)
    os.environ["MINECRAFT_GAME_DIR"] = str(game_dir)

    # The app fills this cache on startup (launcher.meta.block_textures.prefetch_missing_textures), but
    # the renderer builds its own data directory, so without this the version rows fall back to the
    # painted stand-in and the pictures stop showing what a normal machine shows. Non-fatal by design:
    # a render with no network keeps the stand-ins, which are now coloured per category.
    from launcher.meta import block_textures

    try:
        written = block_textures.prefetch_missing_textures(data_dir, game_dir=game_dir)
        print(f"block textures: {written} fetched")
    except Exception as exc:  # noqa: BLE001 - a render must not die for a missing icon
        print(f"block textures: prefetch skipped ({type(exc).__name__}: {exc})")

    from gui import i18n

    known_languages = {code for code, _name in i18n.UI_LANGUAGES}
    if args.language not in known_languages:
        parser.error(
            f"unknown language {args.language!r}; choose one of {', '.join(sorted(known_languages))}"
        )
    # Before anything imports/creates the QApplication: create_app() loads this file and decides the UI
    # language + theme from it, so a later write would only affect the *next* render (the leak this
    # fixes). radius is always written too: an omitted --radius used to inherit the previous value.
    prepare_render_config(
        data_dir,
        language=args.language,
        theme=args.theme,
        accent=args.accent,
        font=args.font,
        radius=args.radius or "default",
        title_bar=args.title_bar,
        game_dir=game_dir,
    )

    from PySide6.QtCore import QThreadPool

    from gui.pages import java_page, mods_page, versions_page
    from launcher.meta.manifest import ManifestVersion, VersionManifest
    from launcher.mods.models import ModSearchHit

    manifest = VersionManifest(
        latest={"release": "1.21.11", "snapshot": "26w02a"},
        versions=[
            ManifestVersion(
                id=vid,
                type=vtype,  # type: ignore[arg-type]
                url="https://example.invalid/version.json",
                time=f"{date}T12:00:00+00:00",
                release_time=f"{date}T12:00:00+00:00",
            )
            for vid, vtype, date in FAKE_VERSIONS
        ],
    )
    hits = [
        ModSearchHit(
            slug=slug,
            title=title,
            description=desc,
            downloads=dl,
            icon_url=FAKE_MOD_ICONS.get(slug, ""),
        )
        for slug, title, desc, dl in FAKE_MODS
    ]

    # The rows read this store synchronously when it is warm (see _ContentTab._load_icon), so warming
    # before the window exists is what keeps the images from being letter tiles at grab time.
    if args.warm_cache:
        warm_icon_cache(hit.icon_url for hit in hits)

    # offline stubs so rendering never waits on the network
    versions_page.fetch_manifest = lambda cache_path=None: manifest  # type: ignore[assignment]
    mods_page.search_projects = lambda *a, **k: list(hits)  # type: ignore[assignment]
    java_page.list_java = lambda *a, **k: []  # type: ignore[assignment]

    from gui.main import create_app
    from gui.pages.wizard import FirstRunWizard  # noqa: F401  (import check only)
    from gui.theme import apply_theme

    app = create_app([])
    # The app now proves it read the language this render asked for. Without this guard a regression that
    # moves the write after create_app() would silently produce pictures in the previous language again;
    # failing here names the two languages instead.
    started_language = i18n.current_language()
    if started_language != args.language:
        raise SystemExit(
            f"refusing to render: asked for {args.language!r}, but the application started in "
            f"{started_language!r}; prepare_render_config() must run before create_app()"
        )
    print(f"language {started_language}")

    # the offscreen platform ships almost no font families: register the system CJK fonts
    # explicitly, otherwise every Chinese label renders as tofu boxes
    from PySide6.QtGui import QFontDatabase

    for font_file in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/deng.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/malgun.ttf",
        "C:/Windows/Fonts/malgunbd.ttf",
        "C:/Windows/Fonts/YuGothM.ttc",
        "C:/Windows/Fonts/meiryo.ttc",
    ):
        if Path(font_file).exists():
            QFontDatabase.addApplicationFont(font_file)

    from launcher import config
    from launcher.auth import AccountStore, create_offline_account

    # The config was written before create_app(); re-apply the theme here because the CJK fonts were
    # registered just above (they are not available when create_app() applies the theme).
    cfg, _ = config.load()
    apply_theme(cfg.theme, cfg.accent_color, cfg.ui_font, cfg.ui_radius, cfg.title_bar_color)
    # The instance detail's 截图 / 服务器 sections need something to show: small painted pictures and
    # a real servers.dat for each demo instance. QPainter needs the application that now exists.
    seed_preview_media(Path(game_dir))
    store = AccountStore()
    accounts = store.load()
    if not accounts:
        account = create_offline_account("Steve")
        store.save({account.uuid: account})

    from gui.main_window import MainWindow

    window = MainWindow()
    window.resize(1000, 600)
    window.apply_window_mode()
    window.show()
    wait_for_workers(app, QThreadPool.globalInstance(), 3.0)

    # load page data through the real code paths (background workers, stubbed sources)
    window.pages["versions"].refresh()
    wait_for_workers(app, QThreadPool.globalInstance(), 15.0)
    window.pages["java"].refresh()
    wait_for_workers(app, QThreadPool.globalInstance(), 15.0)

    from gui.main_window import NAV_KEYS  # single source of truth for the page order

    names = list(NAV_KEYS)
    if args.pages:
        wanted = [item.strip() for item in args.pages.split(",") if item.strip()]
        names = [name for name in names if name in wanted]
    prefix = f"{args.label}_" if args.label else ""
    for name in names:
        index = NAV_KEYS.index(name)
        window.sidebar.setCurrentRow(index)
        for _ in range(8):
            app.processEvents()
            time.sleep(0.05)
        wait_for_workers(app, QThreadPool.globalInstance(), 8.0)
        if name == "versions":
            # The fold view is the default, but a picture of six collapsed groups shows no version row
            # and therefore no block icon - which is exactly what the published screenshot must show.
            page = window.pages["versions"]
            groups = page.expanded_groups()
            if not groups:
                page.expand_group("release")
                for _ in range(6):
                    app.processEvents()
                    time.sleep(0.05)
                wait_for_workers(app, QThreadPool.globalInstance(), 5.0)
        if name == "instances":
            # An instance page without a selection only shows the hint: select a demo instance
            # (prefer the second row, which is the modded one) so the detail panel is visible.
            page = window.pages["instances"]
            if len(page.card_ids()):
                page.select_instance(page.card_ids()[1 if len(page.card_ids()) > 1 else 0])
            for _ in range(6):
                app.processEvents()
                time.sleep(0.05)
        if args.scroll is not None:
            _scroll_page_to(window.pages[name], args.scroll)
            for _ in range(4):
                app.processEvents()
                time.sleep(0.05)
        elif args.scroll_bottom:
            _scroll_page_to_bottom(window.pages[name])
            for _ in range(4):
                app.processEvents()
                time.sleep(0.05)
        target = out_dir / f"{prefix}{index}_{name}.png"
        if args.expect_icons:
            page = window.pages[name]
            if name == "mods" and not resource_rows(page):
                target.unlink(missing_ok=True)  # never leave a stale picture behind a failed check
                raise SystemExit(
                    "refusing to save page 'mods': it rendered no resource rows, "
                    "so --expect-icons cannot prove a single icon"
                )
            if not wait_until(lambda page=page: icons_ready(page), timeout_ms=8000):
                target.unlink(missing_ok=True)
                assert_icons_loaded(page, f"page '{name}'")
        # No focus ring can reach a published picture: defocus first, then refuse to save if any
        # keyboardFocus mark survived (see defocus_for_shot/assert_no_focus_mark).
        if args.link_capsule:
            show_preview_capsule(window)
            app.processEvents()
        defocus_for_shot(window, window.pages[name])
        assert_no_focus_mark(window, window.pages[name])
        image = render_page_image(window)
        image.save(str(target))
        print(f"saved {target} ({image.width()}x{image.height()})")

    if args.dialog_shot:
        _render_dialog_shot(app, window, Path(game_dir), out_dir, prefix)
    if args.detail_shot or args.detail_sections:
        _render_detail_shot(app, window, out_dir, prefix, sections=args.detail_sections)
    if args.tools_states:
        _render_tools_states(app, window, out_dir, prefix)

    window.close()
    return 0


#: (filename suffix, state, FakeLinkSession keyword arguments) for the six reviewed layouts.
#: Player names are real Minecraft-shaped IDs (ASCII, 3-16 characters) - the game cannot have a
#: CJK user name. The fake has no UUIDs, so these renders keep the letter avatars; the real-backend
#: render (which resolves demo UUIDs through Mojang) is what shows Steve's and Alex's skin faces.
TOOLS_STATE_SHOTS = (
    ("1_onboarding", "NOT_DOWNLOADED", {}),
    ("2_idle", "IDLE", {}),
    ("3_host_wait", "HOST_WAITING", {}),
    (
        "4_host_ready",
        "HOST_READY",
        {
            "room_code": "U/8F3K-2QX9-7WMD-4TRA",
            "members": (("Steve", True, ""), ("Alex", False, "")),
            "address": "127.0.0.1:25566",
        },
    ),
    (
        "5_connecting",
        "CONNECTING",
        {
            "room_code": "U/8F3K-2QX9-7WMD-4TRA",
            "difficulty": "TOUGH",
        },
    ),
    (
        "6_joined",
        "JOINED",
        {
            "room_code": "U/8F3K-2QX9-7WMD-4TRA",
            "members": (("Alex", False, ""), ("Steve", True, "")),
            "address": "127.0.0.1:25566",
        },
    ),
)


def _render_tools_states(app, window, out_dir: Path, prefix: str) -> None:
    """Render the six 联机 layouts by scripting a :class:`FakeLinkSession` per state.

    Uses the same real window and the same screenshot pipeline as every other page: the tab bar,
    banner, footer and navigation rail are the product's, not a reconstruction. The fake is also
    what the page's own tests drive, so a picture that works here is a state the tests can reach.
    """
    from PySide6.QtCore import QThreadPool

    from gui.main_window import NAV_KEYS
    from launcher.link import FakeLinkSession, LinkDifficulty, LinkMember, LinkState

    window.resize(1000, 610)
    window.sidebar.setCurrentRow(NAV_KEYS.index("tools"))
    for _ in range(8):
        app.processEvents()
        time.sleep(0.05)
    wait_for_workers(app, QThreadPool.globalInstance(), 5.0)
    page = window.pages["tools"]
    # The waiting layout is the "now open the LAN world" instruction, and that assumes a game to open
    # it in. A render machine has no game running, so the page's own detector (the launcher's process
    # registry) would substitute the "no game process detected" diagnostic for the reviewed copy.
    # The picture is of the reviewed layout, so the detector answers "a game is running" here.
    page._game_running_provider = lambda: True
    out_dir.mkdir(parents=True, exist_ok=True)
    for suffix, state_name, options in TOOLS_STATE_SHOTS:
        kwargs = dict(options)
        if "members" in kwargs:
            kwargs["members"] = tuple(
                LinkMember(name, is_host=is_host, avatar=avatar)
                for name, is_host, avatar in kwargs["members"]
            )
        if "difficulty" in kwargs:
            kwargs["difficulty"] = LinkDifficulty[kwargs["difficulty"]]
        session = FakeLinkSession(state=LinkState[state_name], **kwargs)
        page.set_session(session)
        for _ in range(10):
            app.processEvents()
            time.sleep(0.05)
        defocus_for_shot(window, page)
        assert_no_focus_mark(window, page)
        image = render_page_image(window)
        target = out_dir / f"{prefix}tools_{suffix}.png"
        image.save(str(target))
        print(f"saved {target} ({image.width()}x{image.height()})")


def _render_detail_shot(
    app, window, out_dir: Path, prefix: str, *, sections: bool = False
) -> None:
    """Render the instance detail page, the card grid's drill-down target.

    The published set is nine nav pages plus this one: the detail view has no nav-rail entry of its
    own, so the page loop above can never reach it. Opening it also drops the main rail (the detail
    page has a ten-entry sidebar of its own), which is part of the picture and the reason this pass
    renders the whole window rather than the page widget.

    With ``sections`` it also renders the two 游戏资源 sections that used to be top-level pages
    (截图 / 服务器): they live only inside this detail view now, so this is the only place a picture of
    them can be taken.

    Runs last on purpose: the rail is hidden while the detail page is up, so a later page pass would
    have to restore the grid first.
    """
    from PySide6.QtCore import QThreadPool

    from gui.main_window import NAV_KEYS

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    for _ in range(8):
        app.processEvents()
        time.sleep(0.05)
    page = window.pages["instances"]
    ids = page.card_ids()
    if not ids:
        raise SystemExit(
            "refusing to save page 'instance_detail': the preview tree has no demo instance"
        )
    # The second demo instance is the modded one (ensure_preview creates it that way), so it has the
    # fullest detail page. One instance is still enough for the picture.
    if not page.open_instance(ids[1] if len(ids) > 1 else ids[0]):
        raise SystemExit("refusing to save page 'instance_detail': the instance could not be opened")
    wait_for_workers(app, QThreadPool.globalInstance(), 8.0)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.05)
    target = out_dir / f"{prefix}{len(NAV_KEYS)}_instance_detail.png"
    _save_detail_picture(window, page, target)
    if not sections:
        return
    from gui.pages.instance_detail import TAB_SCREENSHOTS, TAB_SERVERS

    detail = page.detail_page.detail
    for tab, suffix in ((TAB_SCREENSHOTS, "screenshots"), (TAB_SERVERS, "servers")):
        detail.set_tab(tab)
        wait_for_workers(app, QThreadPool.globalInstance(), 8.0)
        for _ in range(12):
            app.processEvents()
            time.sleep(0.05)
        target = out_dir / f"{prefix}{len(NAV_KEYS)}_instance_detail_{suffix}.png"
        _save_detail_picture(window, page, target)


def _save_detail_picture(window, page, target: Path) -> None:
    """Defocus, refuse a focus-marked picture, then save the window at the page's own size."""
    defocus_for_shot(window, page)
    assert_no_focus_mark(window, page)
    image = render_page_image(window)
    image.save(str(target))
    print(f"saved {target} ({image.width()}x{image.height()})")


def _render_dialog_shot(app, window, game_dir: Path, out_dir: Path, prefix: str) -> None:
    """Render the crash diagnosis dialog over the instances page.

    Uses one of the shipped sample logs so the picture shows a real diagnosis
    (error code, findings, advice) instead of a placeholder.
    """
    from gui.dialogs.crash_dialog import CrashDiagnosisDialog, context_for_launch
    from launcher.diagnostics import diagnose

    logs = game_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path("tests/data/crash_samples/oom.latest.log"), logs / "latest.log")

    from gui.main_window import NAV_KEYS

    window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
    page = window.pages["instances"]
    if len(page.card_ids()):
        page.select_instance(page.card_ids()[1 if len(page.card_ids()) > 1 else 0])
    for _ in range(8):
        app.processEvents()
        time.sleep(0.05)

    diagnosis = diagnose(
        context_for_launch(
            version_id="1.20.1", game_dir=game_dir, exit_code=1, started=time.time() - 60
        )
    )
    dialog = CrashDiagnosisDialog(window, diagnosis, log_path=logs / "latest.log")
    dialog.show()
    for _ in range(12):
        app.processEvents()
        time.sleep(0.05)
    # The dialog is part of the picture too: its own default button is marked from the FocusIn a
    # dialog sends when it opens, so the same rule applies before this render.
    defocus_for_shot(window, dialog)
    assert_no_focus_mark(window, dialog)
    image = render_page_image(window)
    target = out_dir / f"{prefix}{len(NAV_KEYS) + 1}_dialog.png"
    image.save(str(target))
    print(f"saved {target} ({image.width()}x{image.height()})")
    dialog.close()


if __name__ == "__main__":
    raise SystemExit(main())
