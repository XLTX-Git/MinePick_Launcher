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

"""Width audit: every screen at 1000 / 1050 / 1400 / 1800 px, offscreen, one line per (width, screen).

The minimum-width gate (tools/measure_min_width.py) only measures the *floor*: the narrowest window
that still fits. What a window does when it is *wide* was untested, and that is where the reported
defects lived - the instance grid cut its right column in half, a provider chip lost its last
letter, a heading read as body text. This tool is the other half: it renders every screen at three
widths and reports what happens when there is plenty of room.

Three checks, in the order the report prints them:

1. **clipping** - a widget that leaves the visible area it lives in (its scroll pane on an axis that
   cannot scroll, or the window itself). The pages' horizontal scrollbars are off, so a clipped
   right edge is simply unreachable. This is a failure: the exit status is 1 when anything is
   clipped.
2. **overflow** - the same test against an ordinary parent widget: a child whose rectangle leaves its
   parent's rectangle. Also a failure (it is the usual cause of 1).
3. **elision** - text that does not fit: an ``ElidedLabel`` whose stored value differs from what it
   paints, a plain label whose string is wider than the label, a table cell the view elides, or a
   self-painted row that exposes ``elided_texts()``. Elision is *allowed* - a long path is supposed
   to elide - so it is reported, not failed: one line per screen, with the text, so a person can
   judge. ``DEFAULT_WHITELIST`` (plus ``--whitelist FILE``) marks the deliberate ones; they still
   appear in the report, tagged ``(whitelist)``, and the run counts them separately.

Output is exactly one line per (width, screen)::

    1050  launch        OK
    1400  java          省略: QLabel#someLabel "Java 21 …"
    1800  instances      OK

Exit status: 0 when nothing is clipped, 1 when anything is (a vacuous pass - a screen that produced
no widget at all - is a failure too).

Usage:
    python tools/audit_window_widths.py
    python tools/audit_window_widths.py --widths 1050,1800 --theme dark
    python tools/audit_window_widths.py --out D:/dsh-workspace/tmp/width_shots --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
#: The four widths the audit renders. 1000 is the window's product floor (MainWindow.setMinimumWidth),
#: 1050 is just under the old 1180 budget's comfort zone, and 1800 is wider than the user's own 1644
#: window, so a layout that only works at one size shows up.
DEFAULT_WIDTHS = (1000, 1050, 1400, 1800)
WINDOW_HEIGHT = 915
DEFAULT_LANGUAGE = "zh_cn"
DEFAULT_THEME = "light"
#: The nine top-level pages, then the instance detail view (its own pane, its own tables).
SCREENS = (
    "launch",
    "instances",
    "versions",
    "java",
    "account",
    "mods",
    "tools",
    "settings",
    "about",
    "instances/detail",
)
#: Slack for layout rounding. A widget may end exactly on its parent's last pixel.
EDGE_TOLERANCE = 1
#: A coordinate outside this box is "unbounded" for an axis a scroll area can scroll.
UNBOUNDED = 1_000_000
#: The QSS gives table cells 8px of padding on each side; the elision test has to subtract it or
#: every long cell would look cut.
CELL_PADDING_X = 8

#: Deliberate elisions. An entry matches a finding when every key it states matches ("*" means any
#: screen). The list is a *filter for the report*, not a way to hide text: a matched elision is still
#: printed, tagged ``(whitelist)``, so the reason stays visible next to the value.
DEFAULT_WHITELIST: tuple[dict[str, str], ...] = (
    # A filesystem path is longer than any row by nature, and ElidedLabel is exactly the widget that
    # elides one on purpose (it keeps the whole value in the tooltip and in text()).
    {"widget": "ElidedLabel", "text_contains": "\\"},
    {"widget": "ElidedLabel", "text_contains": "/"},
    # The same value can reach a plain label (a wrapped path in a settings row, an install step).
    {"screen": "*", "text_contains": "\\AppData\\"},
)


@dataclass
class Clip:
    """One widget that cannot be reached: it leaves the part of the window that clips it."""

    widget: str
    detail: str


@dataclass
class Elision:
    """One piece of text that does not fit where it is painted."""

    widget: str
    text: str
    accepted: bool = False


@dataclass
class ScreenReport:
    """What one (width, screen) pair produced."""

    screen: str
    clipped: list[Clip] = field(default_factory=list)
    elided: list[Elision] = field(default_factory=list)
    widgets: int = 0

    @property
    def ok(self) -> bool:
        return not self.clipped

    def line(self) -> str:
        """The one-line verdict this tool prints, e.g. ``OK`` or ``裁切: QLabel#x``."""
        parts = []
        for clip in self.clipped:
            parts.append(f"裁切: {clip.widget} ({clip.detail})")
        for elision in self.elided:
            tag = "(whitelist) " if elision.accepted else ""
            text = elision.text if len(elision.text) <= 60 else elision.text[:57] + "..."
            parts.append(f'省略: {tag}{elision.widget} "{text}"')
        return "; ".join(parts) if parts else "OK"


# --------------------------------------------------------------------------------------------------
# geometry: what the user can actually see
# --------------------------------------------------------------------------------------------------


def _describe(widget) -> str:
    """``QLabel#hint``-style name, the same shape the other tools and reports use."""
    name = widget.objectName()
    kind = type(widget).__name__
    return f"{kind}#{name}" if name else kind


def _scroll_area_of(widget):
    """The QAbstractScrollArea whose viewport ``widget`` is, or None."""
    from PySide6.QtWidgets import QAbstractScrollArea

    parent = widget.parentWidget()
    if isinstance(parent, QAbstractScrollArea) and parent.viewport() is widget:
        return parent
    return None


def _can_scroll(area, orientation) -> bool:
    """Whether the user can bring content outside the viewport back into view on this axis.

    A scrollbar whose policy is ``AlwaysOff`` is the case that matters: the pane physically cannot
    scroll, so anything past its edge is unreachable even when Qt has a range to scroll. The instance
    grid's horizontal scrollbar is exactly that, which is how its right column got cut.
    """
    from PySide6.QtCore import Qt

    if orientation == Qt.Orientation.Horizontal:
        policy = area.horizontalScrollBarPolicy()
    else:
        policy = area.verticalScrollBarPolicy()
    return policy != Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def _scroll_host(widget):
    """The scroll area whose policy decides what ``widget`` clips.

    Two shapes: a viewport (the child every scroll area owns) and the scroll area itself. Both bound
    the same screen region, so both answer with the scroll area's policies - otherwise the scroll
    area's own rectangle would clip the taller content a second time, on an axis that scrolls.
    """
    from PySide6.QtWidgets import QAbstractScrollArea

    area = _scroll_area_of(widget)
    if area is not None:
        return area
    if isinstance(widget, QAbstractScrollArea):
        return widget
    return None


def _is_ancestor(candidate, widget) -> bool:
    """Whether ``candidate`` is ``widget`` or one of its ancestors."""
    node = widget
    while node is not None:
        if node is candidate:
            return True
        node = node.parentWidget()
    return False


def _rect_in(parent, root):
    """``parent``'s rectangle in ``root`` coordinates - whichever of the two contains the other.

    ``QWidget.mapTo`` only walks *up* the parent chain, so the two directions need different calls:
    a viewport inside the audited page maps itself into the page, while the page's own ancestors are
    reached through ``root.mapTo(ancestor)`` (and then negated). Getting this wrong is silent - the
    call answers with the widget's window position and only warns on stderr.
    """
    from PySide6.QtCore import QPoint, QRect

    if _is_ancestor(parent, root):
        point = root.mapTo(parent, QPoint(0, 0))
        return QRect(-point.x(), -point.y(), parent.width(), parent.height())
    point = parent.mapTo(root, QPoint(0, 0))
    return QRect(point.x(), point.y(), parent.width(), parent.height())


def _inside(widget, root) -> bool:
    """Whether ``root`` is one of ``widget``'s ancestors (false for a combo popup, a stray window)."""
    node = widget.parentWidget()
    while node is not None:
        if node is root:
            return True
        node = node.parentWidget()
    return False


def visible_rect(widget, root):
    """(own rect, visible rect) of ``widget`` in ``root`` coordinates.

    The visible rect is the own rect intersected with every ancestor's clip band. Once an axis is
    reachable by scrolling, it stays unbounded for the rest of the walk: the scroll viewport sits
    inside every ancestor above it, so content past its edge is one scroll away, and a page or a
    frame higher up must not take that back (that is how a scrollable card would otherwise look
    "clipped" to a page that is smaller than the card).
    """
    from PySide6.QtCore import QPoint, QRect, Qt

    origin = widget.mapTo(root, QPoint(0, 0))
    rect = QRect(origin.x(), origin.y(), widget.width(), widget.height())
    visible = QRect(rect)
    free_x = False
    free_y = False
    parent = widget.parentWidget()
    while parent is not None:
        band = _rect_in(parent, root)
        area = _scroll_host(parent)
        if area is not None:
            free_x = free_x or _can_scroll(area, Qt.Orientation.Horizontal)
            free_y = free_y or _can_scroll(area, Qt.Orientation.Vertical)
        if free_x:
            band.setLeft(-UNBOUNDED)
            band.setRight(UNBOUNDED)
        if free_y:
            band.setTop(-UNBOUNDED)
            band.setBottom(UNBOUNDED)
        visible = visible.intersected(band)
        parent = parent.parentWidget()
    return rect, visible


def inspect_clipping(root) -> tuple[list[Clip], int]:
    """Every visible descendant of ``root`` that cannot be seen in full, plus the count walked."""
    from PySide6.QtWidgets import QWidget

    clipped: list[Clip] = []
    count = 0
    for widget in root.findChildren(QWidget):
        if not widget.isVisible() or widget.width() <= 0 or widget.height() <= 0:
            continue
        # A native child window is not clipped by its parent's paint (a combo popup, a menu), and a
        # widget parented outside this root (a popup's internal list) has no rectangle here at all.
        if widget.isWindow() or not _inside(widget, root):
            continue
        count += 1
        rect, visible = visible_rect(widget, root)
        lost_x = rect.width() - visible.width()
        lost_y = rect.height() - visible.height()
        if lost_x <= EDGE_TOLERANCE and lost_y <= EDGE_TOLERANCE:
            continue
        where = (
            f"rect {rect.width()}x{rect.height()}, "
            f"visible {max(0, visible.width())}x{max(0, visible.height())}"
        )
        clipped.append(Clip(_describe(widget), where))
    return clipped, count


# --------------------------------------------------------------------------------------------------
# text: what does not fit
# --------------------------------------------------------------------------------------------------


def _label_elided(label) -> str | None:
    """The full text when a QLabel is cutting it, else None."""
    from PySide6.QtCore import Qt

    from gui.widgets import ElidedLabel

    if isinstance(label, ElidedLabel):
        full = label.text()
        if not full:
            return None
        metrics = label.fontMetrics()
        shown = metrics.elidedText(full, label._elide_mode, max(0, label.width() - 2))
        return full if shown != full else None
    if label.wordWrap() or not label.text() or "\n" in label.text():
        return None
    # Rich text is measured by the label's own document, not by fontMetrics on the markup.
    if label.textFormat() == Qt.TextFormat.RichText or label.text().startswith("<"):
        return None
    available = label.contentsRect().width()
    if label.fontMetrics().horizontalAdvance(label.text()) > available + EDGE_TOLERANCE:
        return label.text()
    return None


def _table_elisions(table) -> list[tuple[str, str]]:
    """(cell name, full text) for every visible cell the view would elide."""
    from PySide6.QtCore import Qt

    found: list[tuple[str, str]] = []
    metrics = table.fontMetrics()
    mode = table.textElideMode() if hasattr(table, "textElideMode") else Qt.TextElideMode.ElideRight
    viewport = table.viewport().rect()
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is None or not item.text().strip():
                continue
            cell = table.visualItemRect(item)
            if not cell.isValid() or not cell.intersects(viewport):
                continue
            available = table.columnWidth(column) - 2 * CELL_PADDING_X
            if available <= 0:
                continue
            if metrics.elidedText(item.text(), mode, available) != item.text():
                found.append((f"cell {row},{column}", item.text()))
    return found


def inspect_elision(root, screen: str, whitelist: tuple[dict[str, str], ...]) -> list[Elision]:
    """Every visible piece of text that does not fit, with the whitelist applied."""
    from PySide6.QtWidgets import QLabel, QTableWidget, QWidget

    findings: list[Elision] = []
    seen: set[int] = set()

    def add(widget, text: str) -> None:
        if id(widget) in seen:
            return
        seen.add(id(widget))
        findings.append(Elision(_describe(widget), text, _accepted(screen, _describe(widget), text)))

    for widget in root.findChildren(QWidget):
        if not widget.isVisible() or widget.isWindow() or not _inside(widget, root):
            continue
        if isinstance(widget, QLabel):
            text = _label_elided(widget)
            if text is not None:
                add(widget, text)
            continue
        if isinstance(widget, QTableWidget):
            for name, text in _table_elisions(widget):
                findings.append(
                    Elision(
                        f"{_describe(widget)} {name}",
                        text,
                        _accepted(screen, _describe(widget), text),
                    )
                )
            continue
        hook = getattr(widget, "elided_texts", None)
        if callable(hook):
            try:
                pairs = hook()
            except RuntimeError:  # the C++ side is already gone
                pairs = []
            for _shown, full in pairs:
                add(widget, full)
    return findings


def _accepted(screen: str, widget: str, text: str) -> bool:
    """Whether a whitelist entry covers this finding."""
    for rule in _whitelist:
        if "screen" in rule and rule["screen"] not in ("*", screen):
            continue
        if "widget" in rule and rule["widget"] not in widget:
            continue
        if "text_contains" in rule and rule["text_contains"] not in text:
            continue
        return True
    return False


#: The active whitelist: ``DEFAULT_WHITELIST`` plus ``--whitelist FILE``. A module global because
#: the per-widget walk is called from nested helpers; ``set_whitelist`` is the one writer.
_whitelist: tuple[dict[str, str], ...] = DEFAULT_WHITELIST


def set_whitelist(extra: tuple[dict[str, str], ...] = ()) -> None:
    """Replace the active whitelist with the defaults plus ``extra`` (used by the CLI and tests)."""
    global _whitelist
    _whitelist = tuple(DEFAULT_WHITELIST) + tuple(extra)


def inspect_screen(root, screen: str) -> ScreenReport:
    """Run all checks over one screen and return its line."""
    clipped, count = inspect_clipping(root)
    report = ScreenReport(screen=screen, clipped=clipped, widgets=count)
    report.elided = inspect_elision(root, screen, _whitelist)
    return report


# --------------------------------------------------------------------------------------------------
# the window: preview data, then one fresh window per width
# --------------------------------------------------------------------------------------------------


def _work_dir() -> Path:
    return Path(os.environ.get("MCLAUNCHER_TMP", r"D:\dsh-workspace\tmp")) / "audit_window_widths"


def _prepare_environment(theme: str, language: str) -> Path:
    """A throw-away data + game tree and the config, written before Qt imports the theme.

    The audit needs content to be wrong about: an instance list to lay out, a mod table with rows, a
    Java list with every provider label. It writes the smallest tree that produces those, and it
    lives under the workspace temp directory so the checkout stays free of preview data.
    """
    import shutil

    work = _work_dir()
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    data = work / "data"
    game = work / "game"
    data.mkdir(parents=True)
    game.mkdir(parents=True)
    os.environ["MCLAUNCHER_DATA_DIR"] = str(data)
    os.environ["MINECRAFT_GAME_DIR"] = str(game)

    # Managed runtimes are detected by directory name (runtime/java-<major>): no executable, so the
    # probe reports the directory and the launcher never tries to run a stub.
    for major in (8, 21):
        runtime = data / "runtime" / f"java-{major}"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "release").write_text(
            f'IMPLEMENTOR="Audit"\nJAVA_VERSION="{1.8 if major == 8 else 21}.0.0"\n', encoding="utf-8"
        )

    # Three instances in different groups, each with its own mods/saves, so the card grid and the
    # detail page both have something to draw.
    instances = (
        ("audit-fabric", "fabric-loader-0.19.3-1.21.11"),
        ("audit-vanilla", "1.21.11"),
        ("audit-forge", "1.20.1-forge-47.4.22"),
    )
    for name, parent in instances:
        folder = game / "versions" / name
        (folder / "mods").mkdir(parents=True)
        (folder / "saves").mkdir(parents=True)
        (folder / f"{name}.json").write_text(
            json.dumps(
                {"id": name, "inheritsFrom": parent, "mainClass": "net.minecraft.client.main.Main"}
            ),
            encoding="utf-8",
        )
        (folder / "minepick.json").write_text(
            json.dumps(
                {
                    "id": name,
                    "display_name": name,
                    "note": "",
                    "star": name == "audit-fabric",
                    "isolated": "follow",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    mods = game / "versions" / "audit-fabric" / "mods"
    for jar in (
        "sodium-fabric-0.6.5.jar",
        "lithium-fabric-0.15.3.jar.disabled",
        "iris-fabric-1.8.1.jar",
        "fabric-api-0.141.3+1.21.11.jar",
    ):
        (mods / jar).write_bytes(b"PK\x03\x04audit")

    from launcher import config

    cfg, cfg_path = config.load(data / config.CONFIG_FILENAME)
    cfg.ui_language = language
    cfg.game_language = language
    cfg.language_initialized = True
    cfg.wizard_done = True
    cfg.game_dir = str(game)
    cfg.theme = theme
    cfg.accent_color = ""
    cfg.ui_font = ""
    cfg.ui_radius = "default"
    config.save(cfg, cfg_path)
    return work


def _register_fonts() -> None:
    from PySide6.QtGui import QFontDatabase

    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "malgun.ttf", "YuGothM.ttc", "consola.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def _fake_runtimes():
    """Detected rows covering every provider label, including the one that has to stay readable."""
    from launcher.java.locate import JavaRuntime

    return [
        JavaRuntime(
            path=Path(r"C:\Program Files\Java\jdk-21.0.9\bin\javaw.exe"),
            major=21,
            provider="path",
            version='openjdk version "21.0.9" 2025-10-21 LTS',
        ),
        JavaRuntime(
            path=Path(r"C:\Program Files\Eclipse Adoptium\jdk-17\bin\javaw.exe"),
            major=17,
            provider="java_home",
            version='openjdk version "17.0.12" 2024-07-16',
        ),
        JavaRuntime(
            path=Path(r"C:\Program Files\Common Files\Oracle\Java\javapath\javaw.exe"),
            major=8,
            provider="config",
            version='java version "1.8.0_451"',
        ),
        JavaRuntime(
            path=Path(r"C:\Windows\System32\javaw.exe"),
            major=11,
            provider="system",
            version='openjdk version "11.0.25" 2025-01-21 LTS',
        ),
    ]


def _stub_sources() -> None:
    """The pages' data sources, answered from memory: the audit must never wait on the network."""
    from gui.pages import java_page, mods_page, versions_page
    from launcher.meta.manifest import ManifestVersion, VersionManifest
    from launcher.mods.models import ModSearchHit

    manifest = VersionManifest(
        latest={"release": "1.21.11", "snapshot": "26w02a"},
        versions=[
            ManifestVersion(
                id=version,
                type="release",
                url="https://example.invalid/version.json",
                time=f"2025-01-{day:02d}T12:00:00+00:00",
                release_time=f"2025-01-{day:02d}T12:00:00+00:00",
            )
            for day, version in enumerate(
                ("1.21.11", "1.21.10", "1.21.9", "1.21.8", "1.21.7", "1.21.6"), start=1
            )
        ],
    )
    hits = [
        ModSearchHit(
            slug=f"audit-{index}",
            title=title,
            description="A representative result row for the width audit",
            downloads=1_000_000 - index,
        )
        for index, title in enumerate(
            ("Sodium", "Iris Shaders", "Lithium", "Fabric API", "Just Enough Items", "Mod Menu")
        )
    ]
    versions_page.fetch_manifest = lambda cache_path=None: manifest  # type: ignore[assignment]
    mods_page.search_projects = lambda *args, **kwargs: list(hits)  # type: ignore[assignment]
    java_page.list_java = lambda *args, **kwargs: _fake_runtimes()  # type: ignore[assignment]


def _settle(app, seconds: float) -> None:
    """Spin the event loop so layouts, paints and queued worker results all land."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _build_window(app, width: int):
    """One fresh window at ``width``: a reused window would keep the sizes of the previous width."""
    from PySide6.QtCore import Qt

    from gui.main_window import NAV_KEYS, MainWindow

    window = MainWindow()
    # WA_DontShowOnScreen keeps the window off the desktop while it still lays out and paints.
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(width, WINDOW_HEIGHT)
    window.apply_window_mode()
    window.show()
    _settle(app, 2.0)
    # Build every page once, then load the two pages whose data arrives on a worker.
    for key in NAV_KEYS:
        window.pages.get(key)
    window.pages["versions"].refresh()
    window.pages["java"].refresh()
    _settle(app, 4.0)
    return window


def _show_screen(app, window, screen: str):
    """Navigate to ``screen`` and return the widget the checks should walk."""
    from gui.main_window import NAV_KEYS

    if screen == "instances/detail":
        page = window.pages["instances"]
        window.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        _settle(app, 0.4)
        page.refresh()
        _settle(app, 0.3)
        ids = page.card_ids()
        if not ids:
            return page
        target = next((card_id for card_id in ids if "fabric" in card_id), ids[0])
        page.open_instance(target)
        _settle(app, 0.4)
        page.detail_page.detail.set_tab(1)  # 模组: the table, its toolbar and its header
        _settle(app, 0.6)
        return page.detail_page
    window.sidebar.setCurrentRow(NAV_KEYS.index(screen))
    _settle(app, 0.5)
    page = window.pages[screen]
    if screen == "instances":
        page.show_list()
        _settle(app, 0.3)
    return page


def _grab(window, out_dir: Path | None, width: int, screen: str) -> None:
    """Save the screen's picture (the shot is part of the deliverable, not just a debug aid)."""
    if out_dir is None:
        return
    from shot_pages import defocus_for_shot

    defocus_for_shot(window)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = screen.replace("/", "_")
    window.grab().save(str(out_dir / f"{width}_{name}.png"))


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------


def _parse_whitelist(path: Path) -> tuple[dict[str, str], ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise SystemExit(f"{path}: expected a JSON list of objects")
    return tuple({str(key): str(value) for key, value in item.items()} for item in data)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Render every screen at four widths and audit them.")
    parser.add_argument(
        "--widths",
        default=",".join(str(width) for width in DEFAULT_WIDTHS),
        help="comma-separated window widths (default: 1000,1050,1400,1800)",
    )
    parser.add_argument("--theme", default=DEFAULT_THEME, choices=("light", "dark"))
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--out", default="", help="directory for the per-screen PNGs (default: temp)")
    parser.add_argument("--whitelist", default="", help="JSON list of extra whitelist entries")
    parser.add_argument("--json", action="store_true", help="also print a machine-readable summary")
    args = parser.parse_args(argv)

    widths = [int(item) for item in args.widths.split(",") if item.strip()]
    if not widths:
        parser.error("no widths given")

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools"))
    os.chdir(ROOT)

    if args.whitelist:
        set_whitelist(_parse_whitelist(Path(args.whitelist)))
    else:
        set_whitelist()

    work = _prepare_environment(args.theme, args.language)
    out_dir = Path(args.out) if args.out else work / "shots"

    from gui.main import create_app

    app = create_app([])
    _register_fonts()
    from gui.theme import apply_theme
    from launcher import config

    cfg, _ = config.load()
    apply_theme(cfg.theme, cfg.accent_color, cfg.ui_font, cfg.ui_radius)
    _stub_sources()

    reports: list[ScreenReport] = []
    rows: list[tuple[int, ScreenReport]] = []
    for width in widths:
        window = _build_window(app, width)
        for screen in SCREENS:
            root = _show_screen(app, window, screen)
            _grab(window, out_dir, width, screen)
            report = inspect_screen(root, screen)
            if report.widgets == 0:
                report.clipped.append(Clip("(no widget)", "the screen rendered nothing to check"))
            rows.append((width, report))
            reports.append(report)
            print(f"{width:<5} {screen:<14} {report.line()}")
        window.close()
        window.deleteLater()
        _settle(app, 0.2)

    clipped = sum(len(report.clipped) for report in reports)
    elided = sum(len(report.elided) for report in reports)
    accepted = sum(1 for report in reports for item in report.elided if item.accepted)
    print()
    print(
        f"checked {len(widths)} widths x {len(SCREENS)} screens: "
        f"clipping {clipped}, elision {elided} ({accepted} whitelisted), "
        f"widgets {sum(report.widgets for report in reports)}"
    )
    if args.json:
        print(
            json.dumps(
                {
                    "widths": widths,
                    "screens": list(SCREENS),
                    "clipping": clipped,
                    "elision": elided,
                    "elision_whitelisted": accepted,
                    "ok": clipped == 0,
                    "rows": [
                        {
                            "width": width,
                            "screen": report.screen,
                            "ok": report.ok,
                            "clipped": [clip.widget for clip in report.clipped],
                            "elided": [item.text for item in report.elided],
                        }
                        for width, report in rows
                    ],
                }
            )
        )
    if clipped:
        print("FAIL: content is clipped - it cannot be reached at this width")
        return 1
    print("OK: nothing is clipped at any width (elisions are listed above for review)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
