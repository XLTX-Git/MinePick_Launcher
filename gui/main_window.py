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

"""Main window: sidebar navigation + page stack (pages built on first visit, rebuilt on language switch)."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, nav_icons, theme, window_effects

tr = i18n.tr
from gui.pages.about_page import AboutPage
from gui.pages.instances_page import InstancesPage
from gui.pages.java_page import JavaPage
from gui.pages.launch_page import LaunchPage
from gui.pages.login_page import LoginPage
from gui.pages.mods_page import ResourcesPage
from gui.pages.settings_page import SettingsPage
from gui.pages.versions_tasks import TaskAwareVersionsPage
from gui.task_ui import TaskOverlay
from gui.tasks import TaskCenter
from gui.wizard_overlay import BulletList, OptionGrid, WizardOverlay, WizardStep

NAV_KEYS = ["launch", "instances", "versions", "java", "account", "mods", "settings", "about"]
PAGE_FADE_MS = 150  # interactive, so it stays under the 200ms ceiling (guideline §2.3)
# The floor every page fits at vertically. The layout cannot state it: the stack ignores its height
# policy on purpose (see below), so only the widest *width* is a real layout requirement and the
# height stays a deliberate, language-independent minimum.
MIN_WINDOW_HEIGHT = 610


class _LazyPages:
    """The window's page registry, building each page the first time it is asked for.

    Startup used to construct all eight pages - 802 widgets offscreen, 1457 in the real window - before
    the window was even up, and every one of them is polished by the application stylesheet at that
    cost. Only the first page is visible at that moment, so the other seven are now built on first use.

    Two access patterns are deliberately different:

    * ``pages[key]`` / ``pages.get(key)`` build that one page - this is what navigation and the
      cross-page slots use, so opening one page never builds another.
    * iterating (``for key in pages``, ``pages.items()``, ``len(pages)``) means "I want all of them" and
      materialises the lot. The quality audits and the GUI tests walk every page on purpose, and this
      keeps them working without change - and without silently auditing a single page.
    """

    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._built: dict[str, QWidget] = {}

    # -- building ---------------------------------------------------------
    def _build(self, key: str) -> QWidget:
        page = self._window._make_page(key)
        self._built[key] = page
        self._window._adopt_page(key, page)
        # A page that has just arrived can be wider than the window, so the floor is recomputed here as
        # well as on resize: with lazy construction this is the moment the layout learns its real width,
        # and waiting for the platform to notice would leave the page painted into a narrower window.
        self._window._enforce_layout_floor()
        return page

    def _materialise_all(self) -> None:
        for key in NAV_KEYS:
            if key not in self._built:
                self._build(key)

    def built_keys(self) -> tuple[str, ...]:
        """Keys built so far, without building anything - for tests and diagnostics."""
        return tuple(key for key in NAV_KEYS if key in self._built)

    def peek(self, key: str) -> QWidget | None:
        """The page if it happens to be built, else ``None`` - never builds."""
        return self._built.get(key)

    def clear(self) -> None:
        """Drop every page (language switch): the next access builds a fresh one."""
        for page in self._built.values():
            page.setParent(None)
            page.deleteLater()
        self._built.clear()

    # -- mapping behaviour ------------------------------------------------
    def __getitem__(self, key: str) -> QWidget:
        page = self._built.get(key)
        return page if page is not None else self._build(key)

    def get(self, key: str, default: QWidget | None = None) -> QWidget | None:
        if key not in NAV_KEYS:
            return default
        return self[key]

    def __contains__(self, key: object) -> bool:
        return key in NAV_KEYS

    def __iter__(self):
        self._materialise_all()
        return iter(NAV_KEYS)

    def __len__(self) -> int:
        return len(NAV_KEYS)

    def keys(self) -> list[str]:
        return list(NAV_KEYS)

    def values(self) -> list[QWidget]:
        self._materialise_all()
        return [self._built[key] for key in NAV_KEYS]

    def items(self) -> list[tuple[str, QWidget]]:
        self._materialise_all()
        return [(key, self._built[key]) for key in NAV_KEYS]
ENTRY_START_OPACITY = 0.45  # rolled back to the pre-0.4.0 value at the user's request: the page
# switch fades in from a clearly translucent state again, which is what it used to look like


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MinePick Launcher")
        self.resize(1000, 600)
        # The floor is the layout's own requirement, and it is enforced in resizeEvent - never pinned to a
        # constant. It used to be `setMinimumSize(1000, 610)`, which is below what the layout needs in six
        # of the nine languages (fr_fr reaches 1164px, ru_ru 1152, es_es 1155, de_de 1110, ja_jp 1085,
        # en_us 1048) and therefore *allowed* a 1000px window - Qt then painted the pages into a strip
        # narrower than their minimum, which is what the user saw as clipped text on the instances page.
        # The widest language has to fit without the user resizing anything: 1180px is that budget, and
        # tools/measure_min_width.py is what holds every language to it. See _enforce_layout_floor for why
        # this cannot simply be "let Qt derive the minimum" under lazy page construction.
        self._floor_pass = False
        # The task views float over the content area and are positioned by hand. Declared before the
        # first resize (below), because resizeEvent already asks them where they belong.
        self.task_overlay: TaskOverlay | None = None
        # The DWM corner preference is a one-shot window attribute, applied in showEvent once the
        # native handle exists. False means "not attempted yet", not "failed": on Windows 10 it will
        # simply stay False and the window keeps the square frame it always had.
        self._corners_rounded = False
        self.setMinimumHeight(MIN_WINDOW_HEIGHT)

        from gui.widgets import StatusLabel, apply_no_focus_outline

        self.sidebar = QListWidget()
        self.sidebar.setAccessibleName(tr("a11y.nav"))  # WCAG 4.1.2
        self.sidebar.setObjectName("sidebar")
        # 24px glyph + 10px transparent right padding; the item height stays the pre-icon 40px.
        self.sidebar.setIconSize(nav_icons.icon_size())
        apply_no_focus_outline(self.sidebar)  # remove focus outline from the selected item's text

        nav = QWidget()
        # The content margin is zero now so the launch page's white panel can reach the window edge.
        # The nav keeps its own 12px inset (and its 150px rail), so every other page looks unchanged.
        # The object name is what the light sheet paints the rail with; WA_StyledBackground is required
        # for a plain QWidget to honour a QSS background at all (the settings page needs the same
        # attribute for its paper). Neither changes the layout: the rail keeps its 162px and the 1px
        # separator on its right edge is drawn inside that width.
        nav.setObjectName("navRail")
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nav.setFixedWidth(162)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(12, 12, 0, 12)
        nav_layout.addWidget(self.sidebar, 1)
        # Named because the instance detail page swaps this rail for its own sidebar: the detail host
        # hides it, and every top-level page shows it again (see set_nav_rail_visible).
        self.nav_rail = nav

        self.stack = QStackedWidget()
        # The page paper is painted on the stack itself (see QStackedWidget#pageStack in the light
        # sheet): pages are transparent, and the stack's own margins would otherwise show the chrome.
        self.stack.setObjectName("pageStack")
        # The stack carries the old 12/6/12/12 frame for every page except the launch page (which
        # goes edge-to-edge). QStackedLayout lays its child out in the *widget's* contents rect, so
        # the margin is set on the stack itself; this keeps the other seven pages pixel-identical.
        self.stack.setContentsMargins(6, 12, 12, 12)
        # The window's height must not depend on which pages happen to be built. A stacked widget
        # reports the largest minimum of its children, so with lazy construction a tall page would
        # change the floor the moment it was first visited. Ignoring the height stops that; the width
        # is a real requirement and is tracked per language in _enforce_layout_floor instead.
        self.stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)

        content = QWidget()
        content.setObjectName("appContent")
        # Named for the task views: they float over this area (below the title bar, above the status
        # bar) and are positioned against its edges, so no layout ever owns them.
        self.content = content
        layout = QHBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(nav)
        layout.addWidget(self.stack, 1)
        # Frameless window: the frame is ours, so it follows the theme instead of the OS
        from gui.title_bar import TitleBar

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        central = QWidget()
        central.setObjectName("appRoot")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self, self.windowTitle())
        outer.addWidget(self.title_bar)
        outer.addWidget(content, 1)
        self.setCentralWidget(central)
        # The whole app reports on this one line: pages forward their progress and
        # errors here through gui.widgets.set_app_status (colours follow the severity).
        self.status_label = StatusLabel("")
        self.status_label.setObjectName("hint")
        self.statusBar().addWidget(self.status_label)

        # The window's task list: one model, two views. The center is created before any page, because
        # a page reaches it through its parent chain (gui.tasks.task_center_of) rather than being handed
        # one - which is what keeps an offscreen page without a window free of tasks.
        self.tasks = TaskCenter(self)
        self.task_overlay = TaskOverlay(self)

        # One handler for both jobs: it builds the page if this is its first visit, shows it, refreshes
        # it. Connecting the stack directly as well would fight it over which widget is current.
        self.sidebar.currentRowChanged.connect(self._on_nav_changed)
        self.build_pages()
        self.sidebar.setCurrentRow(0)
        self.set_status(tr("status.ready"))
        # Icons are rasterised from the SVGs after every theme/accent switch; the colour is part of
        # nav_icons' cache key, so a switched theme can never keep the old-coloured pixmap.
        theme.register_self_styling(self)
        # Startup update check: delayed so the window is up first, once per session
        self._update_checked = False
        QTimer.singleShot(2500, self._auto_check_updates)

    def on_theme_applied(self) -> None:
        """Called by apply_theme() after the stylesheet swap: repaint the themed nav icons."""
        nav_icons.clear_cache()
        self._refresh_nav_icons()

    def _auto_check_updates(self) -> None:
        """Let the About page run its startup check (it honours the configured update mode)."""
        if self._update_checked:
            return
        self._update_checked = True
        page = self.pages.get("about")
        if page is not None:
            page.auto_check_on_start()

    def set_status(self, text: str, level: str | None = None) -> None:
        """Single status line of the app: ``level`` is "info" (default), "warning" or "error".

        An empty message falls back to the neutral ready line, so the bar never ends up
        blank when a page clears what used to be its own page-local status label.
        """
        if not text:
            self.status_label.setText(tr("status.ready"))
            return
        if level == "error":
            self.status_label.set_error(text)
        elif level == "warning":
            self.status_label.set_warning(text)
        else:
            self.status_label.setText(text)

    def _make_page(self, key: str) -> QWidget:
        """Construct one page. Called by the registry the first time that page is needed."""
        factories = {
            "launch": LaunchPage,
            "instances": InstancesPage,
            # The versions page's install flow is frozen; this subclass is the adapter that connects it
            # to the window's task list without editing a line of the flow (gui/pages/versions_tasks.py).
            "versions": TaskAwareVersionsPage,
            "java": JavaPage,
            "account": LoginPage,
            "mods": ResourcesPage,
            "settings": SettingsPage,
            "about": AboutPage,
        }
        return factories[key]()

    def _wiring_for(self, key: str) -> list[tuple[str, object]]:
        """Outgoing signal → window-level slot for one page.

        Connected when the page is built. The slots resolve the peer page through the registry at
        signal time, because connecting straight to another page's bound method would build that peer
        immediately and undo the laziness this exists for.
        """
        return {
            "account": [("account_changed", self._on_account_changed)],
            "launch": [
                ("account_changed", self._on_launch_account_changed),
                # 实例选择 opens the card grid (where an instance can actually be chosen), not the
                # detail page of the profile that happens to be current; 实例设置 still drills in.
                ("instance_requested", self._goto_instances_list),
                ("instance_settings_requested", self._goto_instance_settings),
                ("account_requested", self._goto_account),
            ],
            "instances": [
                ("instance_open_requested", self._goto_instance),
                # 使用 on a card: make it the launch target and go back to the launch page.
                ("instance_use_requested", self._on_instance_use_requested),
                ("back_requested", self._on_instances_back),
                # The instance detail page replaces the main rail with its own sidebar: the page says
                # which of its two views is on screen, and the window shows exactly one rail.
                ("detail_shown", self._on_instance_detail_shown),
                ("list_shown", self._on_instance_list_shown),
            ],
            "settings": [("settings_changed", self._on_settings_changed)],
            "versions": [
                ("versions_changed", self._on_versions_changed),
                ("launch_requested", self._goto_launch),
                ("instance_requested", self._goto_instance),
            ],
        }.get(key, [])

    def _adopt_page(self, key: str, page: QWidget) -> None:
        """Put a freshly built page into the stack and connect its outgoing signals."""
        self.stack.addWidget(page)
        for signal_name, slot in self._wiring_for(key):
            getattr(page, signal_name).connect(slot)

    def build_pages(self) -> None:
        """(Re)create the navigation. Pages themselves are constructed on first use."""
        # A page may own background work (the versions page owns the download behind its task). It is
        # asked to stop that work before it is dropped: its worker's callbacks go through
        # run_in_background, which drops a callback whose page is gone, so a task left running would
        # spin in the corner for the rest of the session. A language switch is what runs this.
        # (There is no registry on the very first call - the window is being built.)
        previous = getattr(self, "pages", None)
        if previous is not None:
            for key in previous.built_keys():
                page = previous.peek(key)
                stop = getattr(page, "cancel_running_task", None)
                if callable(stop):
                    stop()
        # A fresh registry: after a language switch every page must be built again in the new language
        self.pages = _LazyPages(self)
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        for key in NAV_KEYS:
            self.sidebar.addItem(i18n.tr("nav." + key))
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()
        self.sidebar.setCurrentRow(0)
        self.sidebar.blockSignals(False)
        self._on_nav_changed(0)  # blocks were held, so the first page is shown explicitly
        # The task views are the window's, not a page's, so they survive this rebuild - but their
        # strings (panel title, empty state, the rows' state labels) were built in the old language.
        if getattr(self, "task_overlay", None) is not None:
            self.task_overlay.retranslate()

    def _apply_page_inset(self, key: str) -> None:
        """Give the launch page the full content area; keep the old frame on every other page.

        The launch page asks for a zero stack inset because its own layout draws the 16px paper
        margin around the card (a second inset here would double it). Every other page used to sit
        inside 12px of margin, plus that 6px gap: reproducing the same 6/12/12/12 insets on the
        stack keeps their geometry - and therefore every screenshot and width measurement -
        unchanged. QStackedLayout honours the widget's contents margins, not a layout-level margin,
        so the value is set on the stack itself.
        """
        if key == "launch":
            self.stack.setContentsMargins(0, 0, 0, 0)
        else:
            self.stack.setContentsMargins(6, 12, 12, 12)

    # ---------- one rail at a time ----------

    def set_nav_rail_visible(self, visible: bool) -> None:
        """Show or hide the main rail.

        The instance detail page has an eight-entry sidebar of its own. Two sidebars side by side left
        the content 162px narrower and read as two competing navigations, so while a detail page is
        open the main rail steps aside and the detail sidebar owns the left edge; every other page
        shows the main rail again (``_on_nav_changed`` is the safety net, and the detail page's back
        control restores it through ``list_shown``).

        A hidden widget must not keep keyboard focus: if the rail (or one of its list items) was the
        focused control when the detail page opened, focus moves to the back control before the rail
        disappears, so the next Tab starts from a visible, meaningful control.
        """
        if self.nav_rail.isHidden() == (not visible):
            return
        if not visible:
            self._move_focus_out_of_nav_rail()
        self.nav_rail.setVisible(visible)

    def _move_focus_out_of_nav_rail(self) -> None:
        """Move keyboard focus to the detail page's back control before the rail is hidden."""
        focused = QApplication.focusWidget()
        if focused is None:
            return
        if focused is not self.nav_rail and not self.nav_rail.isAncestorOf(focused):
            return
        # peek() never builds a page: hiding the rail must not construct the instances page just to
        # give focus away. The detail view is the only legitimate reason to hide the rail, so its back
        # control is the target; a stray programmatic hide falls back to the grid's search field.
        page = self.pages.peek("instances")
        if page is None:
            return
        if page.current_view() is page.detail_page:
            page.detail_page.focus_back_button()
        elif page.isVisible():
            page.search_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_instance_detail_shown(self) -> None:
        """An instance detail page is on screen: its sidebar replaces the main navigation rail."""
        self.set_nav_rail_visible(False)

    def _on_instance_list_shown(self) -> None:
        """The instance card grid is back on screen: the main navigation rail returns."""
        self.set_nav_rail_visible(True)

    def _on_nav_changed(self, row: int) -> None:
        """Show the page for ``row`` - building it on first visit - then refresh and fade it in."""
        if not 0 <= row < len(NAV_KEYS):
            return
        # Every top-level page is reached through the main rail, so entering one always restores it.
        # This is the safety net for "the rail was hidden by an instance detail page and the user
        # somehow got back to a top-level page": the rail cannot stay hidden outside the detail view.
        self.set_nav_rail_visible(True)
        key = NAV_KEYS[row]
        page = self.pages[key]  # first visit to a page is what constructs it
        self.stack.setCurrentWidget(page)
        self._apply_page_inset(key)
        if key == "launch":
            # auto-syncs the version dropdown after a loader/modpack install
            page.refresh_versions()
        elif key == "instances":
            page.refresh()
            page.show_list()
        elif key == "versions":
            # a version can be deleted on the instances page: re-mark the installed column
            page.refresh_installed()
        elif key == "about":
            page.refresh()  # the update settings may have changed elsewhere
        self._refresh_nav_icons()
        self._fade_in_current_page()

    def _refresh_nav_icons(self) -> None:
        """Paint every rail item: hint colour at rest, accent for the selected row.

        Called on first build, on every selection change and after a theme/accent swap, which is what
        keeps the icons from freezing in the colours of whichever theme was active at startup.
        """
        tokens = theme.current_tokens() or theme.theme_palette("light")
        resting = tokens["FG_HINT"]
        selected = tokens["ACCENT"]
        current = self.sidebar.currentRow()
        for index, key in enumerate(NAV_KEYS):
            item = self.sidebar.item(index)
            if item is None:
                continue
            item.setIcon(nav_icons.nav_icon_with_gap(key, selected if index == current else resting))

    # -- cross-page slots (resolve the peer lazily, so peers are not built by wiring alone) --
    def _on_account_changed(self) -> None:
        self.pages["launch"].refresh_account()

    def _on_launch_account_changed(self) -> None:
        self.pages["account"].refresh()

    def _on_versions_changed(self) -> None:
        self.pages["launch"].refresh_versions()

    def _fade_in_current_page(self) -> None:
        """Short fade when switching pages: cheap, and it hides the layout jump.

        Reduced motion keeps this fade on purpose (guideline §5.2: transparency helps read a state
        change; it is movement and scaling that cause discomfort), so only the timing and the entry
        opacity differ from the overlay's fancier entrance.
        """
        page = self.stack.currentWidget()
        if page is None:
            return
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", page)
        animation.setDuration(PAGE_FADE_MS)
        # R-QT-6: a page fades in from near-opaque, not from nothing.
        animation.setStartValue(ENTRY_START_OPACITY)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        # drop the effect once done, otherwise every child repaint goes through it
        animation.finished.connect(lambda: page.setGraphicsEffect(None))
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade_animation = animation  # keep a reference so it is not collected

    def _on_settings_changed(self) -> None:
        from launcher import config

        cfg, _ = config.load()
        if i18n.current_language() != cfg.ui_language:
            i18n.set_language(cfg.ui_language)
            self.build_pages()
            self.set_status(tr("status.ready"))
        self.pages["launch"].refresh_config()
        # The instance registry lives in the game directory: refresh the list immediately
        self.pages["instances"].refresh()

    def _goto_launch(self, version_id: str) -> None:
        self.pages["launch"].set_version_id(version_id)
        self.sidebar.setCurrentRow(0)

    def _goto_instances_list(self, version_id: str = "") -> None:
        """Open the card grid scrolled to one instance (the launch page's 实例选择)."""
        from launcher import config

        self.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        cfg, _ = config.load()
        wanted = str(version_id or "").strip() or str(cfg.selected_instance or "").strip()
        self.pages["instances"].reveal_instance(wanted)

    def _on_instance_use_requested(self, instance_id: str) -> None:
        """A card's 使用: store it as the launch target and return to the launch page."""
        self.pages["launch"].set_version_id(instance_id)  # writes config.selected_instance
        self.sidebar.setCurrentRow(NAV_KEYS.index("launch"))

    def _goto_instance(self, version_id: str) -> None:
        """Open one instance's detail page, whichever page asked for it."""
        self.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        self.pages["instances"].open_instance(version_id)

    def _on_instances_back(self) -> None:
        """The detail page's back control: return to the card grid on the instances page."""
        self.sidebar.setCurrentRow(NAV_KEYS.index("instances"))
        self.set_nav_rail_visible(True)
        self.pages["instances"].show_list()

    def _goto_instance_settings(self, version_id: str) -> None:
        """Open the per-instance settings card of the profile selected on the launch page."""
        from gui.pages.instance_detail import TAB_OVERVIEW

        self._goto_instance(version_id)
        self.pages["instances"].detail.set_tab(TAB_OVERVIEW)

    def _goto_account(self) -> None:
        """The launch page's account button: switching happens on the account page."""
        self.sidebar.setCurrentRow(NAV_KEYS.index("account"))

    def apply_window_mode(self) -> None:
        """Apply the window startup state from config: default/maximized/minimized/remember last size."""
        from launcher import config

        cfg, _ = config.load()
        mode = cfg.window_start_mode
        if mode == "maximized":
            self.showMaximized()
        elif mode == "minimized":
            self.showMinimized()
        elif mode == "remember" and cfg.window_geometry:
            try:
                from PySide6.QtCore import QByteArray

                self.restoreGeometry(
                    QByteArray.fromHex(bytes(cfg.window_geometry, "ascii"))
                )
            except (TypeError, ValueError):
                pass


    # ---------- first-run wizard (in-window overlay) ----------

    def show_wizard(self) -> None:
        """First run: cover the content area with the wizard instead of opening a dialog."""

        overlay = self._build_wizard_overlay()
        overlay.setParent(self)
        overlay.setGeometry(self.rect())
        overlay.finished.connect(self._on_wizard_finished)
        # the status bar and its size grip are managed by QMainWindow's layout and would be
        # painted above an unmanaged child, so they step aside while the overlay is up
        self.statusBar().hide()
        overlay.show_overlay()
        self.wizard_overlay = overlay

    def _build_wizard_overlay(self):
        from PySide6.QtWidgets import QAbstractSpinBox, QFileDialog, QLineEdit, QPushButton, QSlider

        from gui import i18n
        from gui.widgets import AccessibilityPreferenceGroup, NoWheelDoubleSpinBox
        from launcher import config, paths

        cfg, _ = config.load()

        # 1 / start: what is about to happen, as a short list instead of one lonely line
        welcome = BulletList(tr("wizard.points").split("\n"))

        # 2 + 3 / languages: every option is visible, nothing hidden inside a dropdown
        self._wz_ui_language = OptionGrid(list(i18n.UI_LANGUAGES), columns=3)
        self._wz_ui_language.set_value(cfg.ui_language)
        # settings apply immediately everywhere — including the interface language chosen here
        self._wz_ui_language.selected.connect(self._on_wizard_language_selected)
        game_languages = [
            (code, label if code else tr("settings.game_language.follow"))
            for code, label in config.GAME_LANGUAGES
        ]
        self._wz_game_language = OptionGrid(game_languages, columns=3)
        self._wz_game_language.set_value(cfg.game_language)

        # 4 / game directory: field + browse + a live status line
        self._wz_game_dir = QLineEdit(str(cfg.game_dir) if cfg.game_dir else "")
        self._wz_game_dir.setPlaceholderText(str(paths.default_game_dir()))
        browse = QPushButton(tr("settings.browse"))
        browse.setObjectName("wizardBrowse")

        def pick_directory() -> None:
            chosen = QFileDialog.getExistingDirectory(
                self, tr("wizard.game_dir"), str(paths.default_game_dir())
            )
            if chosen:
                self._wz_game_dir.setText(chosen)

        browse.clicked.connect(pick_directory)
        self._wz_dir_status = QLabel("")
        self._wz_dir_status.setObjectName("wizardStatus")

        def refresh_status() -> None:
            raw = self._wz_game_dir.text().strip()
            target = Path(raw) if raw else paths.default_game_dir()
            if target.is_dir():
                self._wz_dir_status.setText("\u2713  " + str(target))
            else:
                self._wz_dir_status.setText("\u2022  " + str(target))

        self._wz_game_dir.textChanged.connect(refresh_status)
        dir_line = QWidget()
        dir_line_layout = QHBoxLayout(dir_line)
        dir_line_layout.setContentsMargins(0, 0, 0, 0)
        dir_line_layout.setSpacing(8)
        dir_line_layout.addWidget(self._wz_game_dir, 1)
        dir_line_layout.addWidget(browse)
        dir_box = QWidget()
        dir_box_layout = QVBoxLayout(dir_box)
        dir_box_layout.setContentsMargins(0, 0, 0, 0)
        dir_box_layout.setSpacing(8)
        dir_box_layout.addWidget(dir_line)
        dir_box_layout.addWidget(self._wz_dir_status)
        dir_box_layout.addStretch(1)
        refresh_status()

        # 5 / memory: this machine's real range, the recommended value preselected, and a device
        # line that says what the range came from. Same rule as the settings page
        # (launcher.memory via gui.memory_bounds), not a fixed 0.5-64 anymore.
        from gui.memory_bounds import memory_bounds
        from launcher.memory import HEAP_STEP_GB

        self._wz_memory_bounds = memory_bounds(java_path=cfg.java_path)
        bounds = self._wz_memory_bounds
        self._wz_memory = NoWheelDoubleSpinBox()
        self._wz_memory.setObjectName("wizardMemory")
        # the slider is the control here: the spin box is a read-out, not a stepper
        self._wz_memory.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._wz_memory.setDecimals(1)  # one decimal, like the settings page's read-out
        self._wz_memory.setRange(bounds.minimum_gb, bounds.maximum_gb)
        self._wz_memory.setSingleStep(HEAP_STEP_GB)
        self._wz_memory.setValue(bounds.recommended_gb)
        # A number, not "4.0 GB": the step heading below carries the unit.
        slider = QSlider(Qt.Orientation.Horizontal)
        ticks = max(1, round((bounds.maximum_gb - bounds.minimum_gb) / HEAP_STEP_GB))
        slider.setRange(0, ticks)
        slider.setValue(round((bounds.recommended_gb - bounds.minimum_gb) / HEAP_STEP_GB))
        self._wz_memory_slider = slider
        self._wz_memory_updating = False

        def on_slider(tick: int) -> None:
            if self._wz_memory_updating:
                return
            self._wz_memory_updating = True
            try:
                self._wz_memory.setValue(round(bounds.minimum_gb + tick * HEAP_STEP_GB, 2))
            finally:
                self._wz_memory_updating = False

        def on_spin(value: float) -> None:
            if self._wz_memory_updating:
                return
            self._wz_memory_updating = True
            try:
                slider.setValue(round((float(value) - bounds.minimum_gb) / HEAP_STEP_GB))
            finally:
                self._wz_memory_updating = False

        slider.valueChanged.connect(on_slider)
        self._wz_memory.valueChanged.connect(on_spin)
        self._wz_memory_device = QLabel(
            tr("wizard.memory.device", f"{bounds.total_gb:.1f}")
        )
        self._wz_memory_device.setObjectName("wizardStatus")
        self._wz_memory_cap = QLabel("")
        self._wz_memory_cap.setObjectName("wizardStatus")
        self._wz_memory_cap.setWordWrap(True)
        self._wz_memory_cap.setVisible(bounds.java_32bit)
        if bounds.java_32bit:
            self._wz_memory_cap.setText(
                tr("settings.memory_java32", f"{bounds.maximum_gb:g}")
            )
        memory_box = QWidget()
        memory_layout = QVBoxLayout(memory_box)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        memory_layout.setSpacing(12)
        memory_layout.addWidget(self._wz_memory)
        memory_layout.addWidget(slider)
        memory_layout.addWidget(self._wz_memory_device)
        memory_layout.addWidget(self._wz_memory_cap)
        memory_layout.addStretch(1)

        # 6 / accessibility prompt policy: the same three answers as the settings page, but its own
        # widget (never a shared one) and its own read/write of the one config key. Skipping the
        # wizard must leave this untouched, so nothing is written until _on_wizard_finished.
        self._wz_accessibility = AccessibilityPreferenceGroup()
        self._wz_accessibility.set_value(cfg.accessibility_preference)

        steps = [
            WizardStep(tr("wizard.start"), tr("wizard.welcome.desc"), welcome, tr("wizard.start")),
            WizardStep(
                tr("settings.ui_language"), tr("wizard.hint"), self._wz_ui_language,
                tr("settings.ui_language"),
            ),
            WizardStep(
                tr("settings.game_language"), tr("wizard.hint"), self._wz_game_language,
                tr("settings.game_language"),
            ),
            WizardStep(tr("wizard.game_dir"), tr("wizard.default_dir.hint"), dir_box, tr("wizard.game_dir")),
            WizardStep(
                tr("wizard.memory") + tr("unit.gb.label"),
                tr("wizard.hint"),
                memory_box,
                tr("wizard.memory"),
            ),
            WizardStep(
                tr("settings.accessibility"),
                tr("settings.accessibility.hint"),
                self._wz_accessibility,
                tr("wizard.accessibility"),  # the rail is 220px wide: use the short name
            ),
        ]
        labels = {
            "back": tr("wizard.back"),
            "next": tr("wizard.next"),
            "done": tr("wizard.done"),
            "skip": tr("wizard.skip"),
        }
        return WizardOverlay(self, steps, labels)

    def _on_wizard_language_selected(self, code: str) -> None:
        """Apply the chosen interface language at once: re-translate and rebuild the wizard."""
        from gui import i18n
        from gui.theme import apply_theme
        from launcher import config

        cfg, cfg_path = config.load()
        cfg.ui_language = code
        config.save(cfg, cfg_path)
        i18n.set_language(code)
        apply_theme(cfg.theme, cfg.accent_color, cfg.ui_font, cfg.ui_radius)
        self._rebuild_wizard()

    def _rebuild_wizard(self) -> None:
        """Rebuild the overlay in the new language, staying on the same step."""
        overlay = getattr(self, "wizard_overlay", None)
        step = getattr(overlay, "_index", 0)
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()
        fresh = self._build_wizard_overlay()
        fresh.setParent(self)
        fresh.setGeometry(self.rect())
        fresh.finished.connect(self._on_wizard_finished)
        fresh._index = step
        fresh._sync()
        fresh.show_overlay()
        self.wizard_overlay = fresh

    def _on_wizard_finished(self, skipped: bool = False) -> None:
        """Persist the wizard choices (never clobbering what the user already had) and switch theme.

        ``skipped`` is True when the Skip button ended the wizard: then no step was submitted, and the
        accessibility preference - whose default is a policy, not a value the user picked - keeps
        whatever it had (``auto`` on a first run). The other steps predate this rule and still write
        their preselected values, matching how the wizard has always behaved.
        """
        from gui import i18n
        from gui.theme import apply_theme
        from launcher import config

        cfg, cfg_path = config.load()
        cfg.ui_language = self._wz_ui_language.value() or cfg.ui_language
        cfg.game_language = self._wz_game_language.value() or cfg.game_language
        chosen_dir = self._wz_game_dir.text().strip()
        if chosen_dir:
            cfg.game_dir = chosen_dir
        cfg.memory_gb = self._wz_memory.value()
        if not skipped:
            cfg.accessibility_preference = self._wz_accessibility.value()
        cfg.wizard_done = True
        config.save(cfg, cfg_path)
        config.initialize_language(cfg, cfg_path)
        i18n.set_language(cfg.ui_language)
        apply_theme(cfg.theme, cfg.accent_color, cfg.ui_font, cfg.ui_radius)
        self.build_pages()
        self.statusBar().show()
        self.set_status(tr("status.ready"))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The custom title bar means Windows never draws its own rounded frame, so the corners stay
        # square until DWM is asked for the Windows 11 preference. Do it once, on the first show, when
        # the native handle is guaranteed; a failure is cosmetic and silent (see window_effects).
        if not self._corners_rounded:
            self._corners_rounded = window_effects.apply_rounded_corners(self)

    def event(self, event) -> bool:
        # Qt refreshes a top-level's minimum size hint on its next layout pass, not at the moment a page
        # is added - measured with en_us: the hint still read 547px while the instances page was already
        # built, and became 1048px only after that pass. LayoutRequest is the pass, so the floor is
        # recomputed there; without it the first grab after a wide page was built photographed the old
        # width.
        if event.type() == QEvent.Type.LayoutRequest:
            self._enforce_layout_floor()
        return super().event(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._enforce_layout_floor()
        # The task views are positioned by hand, never by a layout: that is what keeps the floating
        # button out of the window's minimum width (tools/measure_min_width.py measures the same
        # number before and after this change).
        if self.task_overlay is not None:
            self.task_overlay.reposition()

    def _enforce_layout_floor(self) -> None:
        """Raise the window's minimum width to what the layout needs, and never lower it.

        Why the minimum is enforced instead of left to Qt: with lazy page construction the layout's
        requirement is not known at startup - it is 547px for en_us while only the launch page exists and
        1048px once all eight do (fr_fr: 657px then 1164px). Qt derives a widget's floor from the pages
        that happen to be built, so on its own the floor would depend on which pages the session has
        visited. Growing it as pages arrive is what this does, and it only ever grows: a floor that
        followed the layout back down would shrink the window under the page still in the stack.

        The width is therefore exactly the layout's own requirement - per language, the widest page
        decides (tools/measure_min_width.py measures the same number) - and the height keeps its
        deliberate 610px floor from the constructor. Windows is still free to be *larger*: this is a
        minimum, not a size. That is also what makes tools/shot_pages.py's ``resize(1000, 600)`` land on
        the natural width again instead of on a clipped 1000px.
        """
        if self._floor_pass:  # setMinimumSize below re-enters through resizeEvent; stop here
            return
        self._floor_pass = True
        try:
            hinted = self.minimumSizeHint()
            width = max(hinted.width(), self.minimumWidth())
            height = max(MIN_WINDOW_HEIGHT, self.minimumHeight())
            if (width, height) != (self.minimumWidth(), self.minimumHeight()):
                self.setMinimumSize(width, height)
                # Raising the minimum is a request, not a move, and on this Qt/Windows build it does not
                # even resize a window that is already visible and smaller: measured with en_us, the floor
                # went to 1048px while the shown window stayed 1000px wide, so a grab taken then is a grab
                # of the clipped width. Widening explicitly makes the window take the floor now - the
                # user's height and any larger width they chose are left alone.
                if self.width() < width:
                    self.resize(width, self.height())
        finally:
            self._floor_pass = False

    def keyPressEvent(self, event) -> None:
        """Accept keys that nothing in the window claimed.

        Windows plays its default alert sound when a key event reaches the window unused and unaccepted
        - pressing Enter or Space with the focus on a list item, a label or a disabled button was the
        audible "ding". This handler only runs after every child widget declined the key, so buttons,
        dialogs and line edits keep their own behaviour; it just stops the event escaping to the desktop.
        """
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
            Qt.Key.Key_Backspace,
        ):
            event.accept()
            return
        super().keyPressEvent(event)

    def _ancestor_is_kept_off_screen(self) -> bool:
        """Is this window (or any window above it) marked ``WA_DontShowOnScreen``?

        That attribute is how the render tools and the test suite lay out and paint a real window without
        putting it on the desktop (``tools/snapshot_ui_render.py``, ``tools/shot_pages_real.py`` and
        ``tests/conftest.py`` all rely on it). It stays on the window that was marked, so the check walks
        parents: the marker is set on the top-level window before ``show()``, and a child added later has
        to find it rather than look at itself.
        """
        widget = self
        while widget is not None:
            if widget.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
                return True
            widget = widget.parentWidget()
        return False

    def _confirm_quit_while_downloading(self) -> bool:
        """Ask before quitting when downloads are still running. Returns True when it is fine to close.

        The downloads themselves have no cancellation (an interrupted one keeps its .part and resumes
        next time), so the only real choices are "wait" and "quit now" - the dialog exists so the second
        one is deliberate. Nothing runs it when no download is active: the count is zero and the whole
        thing is one integer comparison.

        When the window is kept off screen there is nobody to ask, and the question is not asked at all:
        see the call site for why carrying the attribute onto the dialog does not work.
        """
        from launcher.net import downloader

        active = downloader.active_download_count()
        if active <= 0:
            return True
        from gui import inwindow_dialog
        from gui.i18n import tr

        if self._ancestor_is_kept_off_screen():
            # Never build the dialog at all. The question is drawn inside this window now, so the
            # attribute would in principle carry over - but off screen there is nobody to ask, and a
            # dialog that waits for a click nobody can make is exactly the hang this branch exists to
            # rule out. Refusing the quit is the same conservative answer the dialog gives when
            # nothing is clicked.
            logging.getLogger(__name__).info(
                "quit confirmation skipped: an ancestor is WA_DontShowOnScreen, so this interaction has "
                "no one to ask (%d download(s) active) - refusing the quit",
                active,
            )
            return False
        # The safe choice is the default one: Enter still answers "keep downloading" (the old box set
        # its default button to cancel for the same reason).
        outcome = inwindow_dialog.confirm(
            self,
            tr("quit.busy.confirm"),
            tr("quit.busy.message").format(active),
            accept_text=tr("quit.busy.confirm"),
            reject_text=tr("quit.busy.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="question",
        )
        return outcome is inwindow_dialog.Outcome.ACCEPTED

    def closeEvent(self, event) -> None:
        if not getattr(self, "_quit_confirmed", False):
            if not self._confirm_quit_while_downloading():
                event.ignore()
                return
            self._quit_confirmed = True
        from launcher import config

        page = self.pages.get("about")
        if page is not None:
            page.install_pending_on_exit()  # "download and install" mode swaps the EXE on exit
        cfg, cfg_path = config.load()
        if cfg.window_start_mode == "remember":
            geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
            cfg.window_geometry = bytes(geometry.saveGeometry().toHex()).decode("ascii")
            config.save(cfg, cfg_path)
        super().closeEvent(event)


