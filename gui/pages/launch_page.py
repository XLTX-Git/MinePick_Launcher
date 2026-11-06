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

"""Launch page: choose a version/account and launch the game (direct connect lives per instance)."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from PySide6.QtCore import QDateTime, QLocale, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui import block_icons, i18n, icons, theme
from gui.accessibility_launch import (
    prepare_launch_accessibility,
    trigger_source_for_click,
)
from gui.errors import show_fatal
from gui.paint_utils import avatar_pixmap, block_pixmap
from gui.widgets import (
    PAGE_HEADER_GAP,
    disable_keeping_focus,
    set_app_status,
)
from gui.workers import run_in_background
from launcher import config, paths
from launcher.accessibility import TriggerSource
from launcher.auth import AccountStore
from launcher.launch import (
    JavaMissingError,
    find_new_crash_reports,
    prepare_launch,
    resolve_launch_account,
    run_process,
)

tr = i18n.tr
_LOGGER = logging.getLogger(__name__)

#: Version kinds the manifest knows, mapped onto the labels the versions page already uses.
_TYPE_LABEL_KEYS = {
    "release": "versions.type.release",
    "snapshot": "versions.type.snapshot",
    "april_fools": "versions.type.april",
    "old_beta": "versions.type.old_beta",
    "old_alpha": "versions.type.old_alpha",
}
_SNAPSHOT_ID = re.compile(r"^\d{2}w\d{2}[a-z]$")


def _locale_name(code: str) -> str:
    """``zh_cn`` -> ``zh_CN``: QLocale wants the region upper-cased."""
    language, _, region = code.partition("_")
    return f"{language}_{region.upper()}" if region else language


def _base_version_id(version_id: str) -> str:
    """The Minecraft version behind a loader profile id (the id itself for a vanilla one)."""
    if version_id.startswith(("fabric-loader-", "quilt-loader-")):
        return version_id.split("-")[-1]
    if "-forge-" in version_id:
        return version_id.split("-forge-", 1)[0]
    return version_id


def _loader_label(version_id: str) -> str:
    """``fabric-loader-0.16.9-1.20.1`` -> ``Fabric 0.16.9``; empty for a vanilla version."""
    if version_id.startswith("fabric-loader-"):
        parts = version_id.split("-")
        return "Fabric " + parts[2] if len(parts) >= 4 else "Fabric"
    if version_id.startswith("quilt-loader-"):
        parts = version_id.split("-")
        return "Quilt " + parts[2] if len(parts) >= 4 else "Quilt"
    if version_id.startswith("neoforge-"):
        return "NeoForge " + version_id[len("neoforge-") :]
    if "-forge-" in version_id:
        return "Forge " + version_id.split("-forge-", 1)[1]
    return ""


def _guessed_version_type(version_id: str) -> str:
    """A local fallback when the version JSON is absent or carries no ``type``."""
    base = _base_version_id(version_id)
    if _SNAPSHOT_ID.match(base):
        return "snapshot"
    if base.startswith("b1."):
        return "old_beta"
    if base.startswith("a1."):
        return "old_alpha"
    return "release"


class _LaunchActionButton(QPushButton):
    """The primary action, painting its 16px title through a child label.

    A QPushButton paints one font size; the launch sheet wants the click target and its text to have
    separate metrics, so the title lives in a transparent label that takes no focus and no mouse.
    The button stays one keyboard stop and one click.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        self.setObjectName("launchButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 4)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)
        self._title_label = QLabel(title)
        self._title_label.setObjectName("launchActionTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The dark sheet has no rule for this label; ButtonText is the button's own white there.
        self._title_label.setForegroundRole(QPalette.ColorRole.ButtonText)
        self._layout.addWidget(self._title_label)
        self._layout.addStretch(1)
        self.setFixedHeight(40)
        self._sync_accessible_name()

    def text(self) -> str:  # QPushButton.text() is not virtual; callers and tests use this
        return self._title

    def setText(self, text: str) -> None:
        self._title = text
        self._title_label.setText(text)
        self._sync_accessible_name()

    def _sync_accessible_name(self) -> None:
        self.setAccessibleName(self._title)


def _count_worlds(saves_dir: Path) -> int:
    """World folders under ``saves`` (a folder counts when it holds a level.dat)."""
    try:
        return sum(
            1
            for entry in saves_dir.iterdir()
            if entry.is_dir() and (entry / "level.dat").is_file()
        )
    except OSError:
        return 0


class LaunchPage(QWidget):
    account_changed = Signal()
    instance_requested = Signal(str)  # show this profile on the instances page
    instance_settings_requested = Signal(str)  # ... and open its per-instance settings
    account_requested = Signal()  # the account button jumps to the account page

    def __init__(self) -> None:
        super().__init__()
        self._version_id = ""
        self._version_choices: list[tuple[str, str]] = []
        self.setObjectName("launchPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # ---- version block: the page's visual subject (and its page header) ----
        self.version_icon = QLabel()
        self.version_icon.setFixedSize(32, 32)
        self.version_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_icon.setPixmap(block_pixmap(30))
        # The big version number is this page's visual subject, not a page name: the shared page
        # header (name + description) was removed from every top-level page. It keeps the same
        # 20px heading treatment through its own object name.
        self.version_label = QLabel(tr("launch.version.none"))
        self.version_label.setObjectName("launchVersionTitle")
        self.version_label.setProperty("versionHero", True)
        # The panel is 360px wide and 16px of paper on each side plus the 32px block icon leaves this
        # line 284px. The Japanese "no version selected" sentence is 300px at the 20px hero size, so
        # the label clipped its tail instead of drawing it. Wrapping keeps the sentence and the type
        # size as they are (no shorter translation, no smaller font); the panel's stretches absorb the
        # second line, and every language whose label fits stays exactly one line tall.
        self.version_label.setWordWrap(True)
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.version_meta = QLabel("")
        self.version_meta.setObjectName("launchVersionMeta")
        # A "switch instances?" line used to sit under the subtitle. The instance buttons below already
        # say the same thing, so the key stays in the tables but the page no longer draws it.

        version_text = QVBoxLayout()
        version_text.setContentsMargins(0, 0, 0, 0)
        version_text.setSpacing(4)
        version_text.addWidget(self.version_label)
        version_text.addWidget(self.version_meta)
        version_text.addStretch(1)

        version_row = QHBoxLayout()
        version_row.setContentsMargins(0, 0, 0, 0)
        version_row.setSpacing(12)
        version_row.addWidget(self.version_icon, 0, Qt.AlignmentFlag.AlignTop)
        version_row.addLayout(version_text, 1)

        self.version_block = QWidget()
        self.version_block.setLayout(version_row)

        # ---- account block: one inline row, the switch button on its right ----
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(36, 36)
        self.avatar_label.setAccessibleName(tr("launch.account.avatar"))
        self.account_label = QLabel("")
        self.account_label.setObjectName("accountName")
        # Accounts are switched on the account page: this is a plain button, not a menu or a combo.
        self.account_button = QPushButton()
        # Keep the shared #iconButton identity (it already owns the dark sheet's quiet icon style and
        # focus ring); the launch sheet narrows it to 24px through the accountSwitch property.
        self.account_button.setObjectName("iconButton")
        self.account_button.setProperty("accountSwitch", True)
        # A noun, not the tooltip's verb phrase: a screen reader must not read a command (C4).
        self.account_button.setAccessibleName(tr("nav.account"))
        self.account_button.setIcon(icons.icon("switch", 16))
        self.account_button.setIconSize(QSize(16, 16))
        self.account_button.setToolTip(tr("launch.account.switch"))
        self.account_button.setFixedSize(24, 24)

        account_row = QHBoxLayout()
        account_row.setContentsMargins(0, 0, 0, 0)
        account_row.setSpacing(12)
        account_row.addWidget(self.avatar_label)
        account_row.addWidget(self.account_label, 1)
        account_row.addWidget(self.account_button, 0, Qt.AlignmentFlag.AlignVCenter)

        # ---- the one primary action and its two equal secondary siblings ----
        self.launch_button = _LaunchActionButton(tr("launch.button"))
        self.select_instance_button = QPushButton(tr("launch.instances.select"))
        self.instance_settings_button = QPushButton(tr("launch.instances.settings"))
        for button in (self.select_instance_button, self.instance_settings_button):
            # Equal width, same baseline, 12px apart: the two halves of one secondary row. The labels
            # are long in German and Russian, so the launch sheet gives them 12px type and 8px side
            # padding - measured against the 320px panel interior, that is what keeps both on one line.
            button.setObjectName("secondaryButton")
            button.setFixedHeight(40)
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        secondary_row = QHBoxLayout()
        secondary_row.setContentsMargins(0, 0, 0, 0)
        secondary_row.setSpacing(12)
        secondary_row.addWidget(self.select_instance_button, 1)
        secondary_row.addWidget(self.instance_settings_button, 1)

        # The old "Advanced" block held the direct-server field. It was the only way to reach --server,
        # so the field moved to the per-instance settings (instances page, settings tab) instead of being
        # dropped: the value is instance data, not launch-page state, and the launch page still reads it
        # through resolve_instance().server when it builds the command line. The keys stay in the tables.

        # ---- quiet bottom block: counts only when they exist, then the last launch ----
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("launchStats")
        self.stats_label.setVisible(False)
        self.last_launch_label = QLabel("")
        self.last_launch_label.setObjectName("launchLast")
        self.last_launch_label.setWordWrap(True)

        # ---- the white card: 16px of paper around it, 16px of its own air inside ----
        self.panel = QWidget()
        self.panel.setObjectName("launchPanel")
        self.panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.panel.setFixedWidth(360)
        self.panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        panel = QVBoxLayout(self.panel)
        panel.setContentsMargins(16, 16, 16, 16)
        panel.setSpacing(0)
        panel.addWidget(self.version_block)
        # The version block is this panel's top content; the gap to the account row below it
        # stays the same 16px the instance-detail header uses.
        panel.addSpacing(PAGE_HEADER_GAP)
        panel.addLayout(account_row)
        panel.addStretch(1)
        panel.addWidget(self.launch_button)
        panel.addSpacing(12)
        panel.addLayout(secondary_row)
        panel.addStretch(1)
        panel.addWidget(self.stats_label)
        panel.addSpacing(4)
        panel.addWidget(self.last_launch_label)

        # The rest of the page is deliberately blank: the panel is the only thing on the paper.
        layout = QHBoxLayout(self)
        # 16px of paper on every side: the panel is a card floating on the page, not a half of a
        # two-column split. The right half stays empty on purpose (no artwork, no widgets).
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(0)
        layout.addWidget(self.panel, 0)
        layout.addStretch(1)

        self.launch_button.clicked.connect(self._on_launch_clicked)
        self.account_button.clicked.connect(self.account_requested.emit)
        self.select_instance_button.clicked.connect(self._request_instance)
        self.instance_settings_button.clicked.connect(self._request_instance_settings)
        theme.register_self_styling(self)

        self.refresh_config()
        self.refresh_account()
        self._populate_versions()

    def on_theme_applied(self) -> None:
        """Re-draw the block icon and avatar after a theme/accent swap."""
        self._refresh_version_icon()
        self.refresh_account()

    # ---- version state: the launch page shows it, the paths choose it ----

    @property
    def version_choices(self) -> list[tuple[str, str]]:
        """(display label, real profile id) for every installed version, installed-only."""
        return list(self._version_choices)

    @property
    def installed_versions(self) -> list[str]:
        """The real ids behind the display labels, sorted the way the page lists them."""
        return [version_id for _label, version_id in self._version_choices]

    def _version_label_text(self) -> str:
        """The display name of the current profile, never the raw loader id when a name exists."""
        version_id = self._current_version_id()
        if not version_id:
            return tr("launch.version.none")
        for label, candidate in self._version_choices:
            if candidate == version_id:
                return label
        from launcher.instances import display_version_name

        return display_version_name(version_id)

    def refresh_config(self) -> None:
        """The settings page changed something the launch page shows: re-read the quiet lines.

        Memory is deliberately not here any more - it is a launcher setting (settings page) and a
        per-instance one (instance settings), so the launch page reads ``cfg.memory_gb`` at launch
        time instead of owning a second copy the user has to keep in sync.
        """
        self._refresh_last_launch()
        self._refresh_stats()

    def refresh_account(self) -> None:
        """Show the selected account; switching happens on the account page (one click away)."""
        cfg, _ = config.load()
        accounts = AccountStore().load()
        account = accounts.get(cfg.selected_account) if cfg.selected_account else None
        if account is None:
            self.account_label.setText(tr("launch.account.none.item"))
            self.avatar_label.clear()
            return
        kind = tr("kind.ms") if account.type == "microsoft" else tr("kind.offline")
        self.account_label.setText(tr("launch.account.label", account.username, kind))
        avatar = avatar_pixmap(account.username, 36)
        if avatar is None:
            self.avatar_label.clear()
        else:
            self.avatar_label.setPixmap(avatar)

    def _refresh_version_block(self) -> None:
        """Update the block icon, the version number, the meta line and the launch history."""
        self._refresh_version_icon()
        self.version_label.setText(self._version_label_text())
        self.version_meta.setText(self._version_meta_text())
        self._refresh_last_launch()
        self._refresh_stats()

    def _refresh_version_icon(self) -> None:
        """Show the selected version's own block as a 3D cube, the drawn block when it has no texture.

        The texture comes from the same three-step lookup the instance cards use (the version's assets,
        then its client jar, then the user cache), so the launch page shows the exact artwork of the
        version it is about to run - a 1.20.1 world shows the 1.20.1 grass, not a generic icon.
        """
        version_id = self._current_version_id()
        if not version_id:
            self.version_icon.setPixmap(block_pixmap(30))
            return
        from launcher.meta import block_textures

        game_dir = self._game_dir()
        category = block_textures.icon_category(version_id, self._version_type(version_id))
        block = block_textures.block_name_for_category(category)
        self.version_icon.setPixmap(
            block_icons.block_pixmap(
                block,
                30,
                game_dir=game_dir,
                version_id=version_id,
                cache_root=block_textures.texture_cache_root(paths.launcher_dir()),
            )
        )

    def _refresh_stats(self) -> None:
        """Show mods/worlds only when the selected profile really has some.

        Zeros are not information: the mock-up's "Mod 0 · 世界 0" line read as empty chrome, so a
        zero component is dropped, and a line with no components hides itself entirely.
        """
        parts: list[str] = []
        version_id = self._current_version_id()
        if version_id:
            try:
                from launcher.instances import resolve_instance
                from launcher.memory import count_mods

                cfg, _ = config.load()
                resolved = resolve_instance(version_id, cfg, self._game_dir())
                mods = count_mods(resolved.mods_dir)
                worlds = _count_worlds(resolved.launch_dir / "saves")
                if mods:
                    parts.append(tr("launch.stats.mods", mods))
                if worlds:
                    parts.append(tr("launch.stats.worlds", worlds))
            except Exception:  # noqa: BLE001 - a missing instance simply has nothing to count
                parts = []
        text = ""
        if len(parts) == 2:
            text = tr("launch.stats.line", parts[0], parts[1])
        elif parts:
            text = parts[0]
        self.stats_label.setText(text)
        self.stats_label.setVisible(bool(text))

    def _version_type(self, version_id: str) -> str:
        """The version JSON's own ``type``, or a local guess when the file is absent.

        Read from disk only: a version the user typed but has not installed must not start a
        manifest download just to label the hero row.
        """
        path = self._game_dir() / "versions" / version_id / f"{version_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        declared = str(data.get("type") or "")
        return declared or _guessed_version_type(version_id)

    def _version_meta_text(self) -> str:
        version_id = self._current_version_id()
        if not version_id:
            return ""
        from launcher.meta.manifest import version_category

        kind = version_category(version_id, self._version_type(version_id))
        type_label = tr(_TYPE_LABEL_KEYS.get(kind, "versions.type.release"))
        loader = _loader_label(version_id)
        return tr("launch.version.meta", type_label, loader) if loader else type_label

    def _last_launch_stamp(self, version_id: str) -> float:
        """When this version last ran: the instance's own stamp, else its latest game log.

        ``Instance.last_played`` is the clean source, but nothing writes it today; ``latest.log``
        (the file the game itself appends to) is the honest fallback, and its mtime is a real "last
        launch" rather than a made-up value. Both routes are local and best-effort: any failure
        means "never launched".
        """
        from launcher.instances import get_instance, resolve_instance

        try:
            instance = get_instance(version_id, self._game_dir())
            if instance is not None and instance.last_played:
                return float(instance.last_played)
            cfg, _ = config.load()
            resolved = resolve_instance(version_id, cfg, self._game_dir())
            log = resolved.launch_dir / "logs" / "latest.log"
            if log.is_file():
                return float(log.stat().st_mtime)
        except Exception:  # noqa: BLE001 - a missing/unreadable instance just means "none"
            return 0.0
        return 0.0

    def _refresh_last_launch(self) -> None:
        """The quiet bottom line: "last launch: <date> · <version>" or "never launched"."""
        version_id = self._current_version_id()
        stamp = self._last_launch_stamp(version_id) if version_id else 0.0
        if stamp <= 0 or not version_id:
            self.last_launch_label.setText(tr("launch.last.none"))
            return
        from launcher.instances import display_version_name

        locale = QLocale(_locale_name(i18n.current_language()))
        when = locale.toString(
            QDateTime.fromSecsSinceEpoch(int(stamp)).date(),
            QLocale.FormatType.ShortFormat,
        )
        detail = tr("launch.last.detail", when, display_version_name(version_id))
        self.last_launch_label.setText(tr("launch.last.line", detail))

    def _current_version_id(self) -> str:
        """The real profile id behind the display label on screen."""
        return self._version_id.strip()

    def _request_instance(self) -> None:
        """Jump to the instances page showing the selected profile."""
        version_id = self._current_version_id()
        if not version_id:
            set_app_status(self, tr("launch.msg.need_id"), "warning")
            return
        self.instance_requested.emit(version_id)

    def _request_instance_settings(self) -> None:
        """Jump to the selected profile's per-instance settings."""
        version_id = self._current_version_id()
        if not version_id:
            set_app_status(self, tr("launch.msg.need_id"), "warning")
            return
        self.instance_settings_requested.emit(version_id)

    def set_version_id(self, version_id: str) -> None:
        """Show/launch this profile (the versions and instances pages hand one over).

        The launch page is the one place that decides which profile the launcher will run, so the
        choice is written to ``config.selected_instance`` here instead of being kept in memory only.
        """
        self._version_id = str(version_id or "").strip()
        self._remember_selected_instance(self._version_id)
        self._refresh_version_block()

    def _remember_selected_instance(self, instance_id: str) -> None:
        """Persist the launch target (empty = keep the historical first-installed behaviour)."""
        cfg, cfg_path = config.load()
        target = str(instance_id or "").strip()
        if str(cfg.selected_instance or "").strip() == target:
            return
        cfg.selected_instance = target
        config.save(cfg, cfg_path)

    def _game_dir(self) -> Path:
        cfg, _ = config.load()
        env_value = os.environ.get(paths.ENV_GAME_DIR)
        return (
            cfg.game_dir
            or (Path(env_value).expanduser() if env_value else None)
            or paths.default_game_dir()
        )

    def _populate_versions(self) -> None:
        """List only installed versions/profiles, keeping the stored/current one when it still exists."""
        from launcher.install import list_installed_versions
        from launcher.instances import display_version_name

        cfg, _ = config.load()
        stored = str(cfg.selected_instance or "").strip()
        installed = sorted(list_installed_versions(self._game_dir()))
        self._version_choices = [(display_version_name(vid), vid) for vid in installed]
        ids = [version_id for _label, version_id in self._version_choices]
        if stored and stored in ids:
            self._version_id = stored
        elif stored:
            # The chosen instance was deleted or renamed: forget the dangling id and fall back to
            # the historical rule instead of showing a profile that no longer exists.
            self._remember_selected_instance("")
            self._version_id = ids[0] if ids else ""
        elif self._version_id and self._version_id not in ids:
            # The profile disappeared (uninstalled/renamed): fall back to the first one, or to the
            # empty state when nothing is installed.
            self._version_id = ids[0] if ids else ""
        elif not self._version_id and ids:
            self._version_id = ids[0]
        self._refresh_version_block()

    def refresh_versions(self) -> None:
        """Refresh the installed list after the versions page installs/uninstalls."""
        self._populate_versions()

    def _on_launch_clicked(self, _checked: bool = False) -> None:
        """A real button click: classify it, then take the same path as every other entry."""
        self.launch(trigger_source_for_click(self.launch_button))

    def launch(self, trigger_source: TriggerSource | str = TriggerSource.UNKNOWN) -> None:
        version_id = self._current_version_id()
        if not version_id:
            set_app_status(self, tr("launch.msg.need_id"))
            return
        cfg, _ = config.load()
        env_value = os.environ.get(paths.ENV_GAME_DIR)
        game_dir = (
            cfg.game_dir
            or (Path(env_value).expanduser() if env_value else None)
            or paths.default_game_dir()
        )
        disable_keeping_focus(self.launch_button)
        set_app_status(self, tr("launch.msg.preparing", version_id))
        # Offline-mode gate: the no-account fallback still requires the offline unlock
        if not cfg.selected_account:
            from launcher.config import offline_mode_allowed

            if not offline_mode_allowed():
                self.launch_button.setEnabled(True)
                set_app_status(self, tr("launch.msg.offline_locked"), "warning")
                return

        # options.txt and mods live in the effective working directory, which isolation decides.
        # Resolving it here is cheap; a failure must not stop the pre-launch step (the worker will
        # report the real launch error).
        launch_dir = None
        try:
            from launcher.instances import resolve_instance

            launch_dir = resolve_instance(version_id, cfg, game_dir).launch_dir
        except Exception:  # the accessibility probe degrades to the shared directory
            _LOGGER.warning(
                "could not resolve %s for the accessibility pre-launch step; using the shared directory",
                version_id,
                exc_info=True,
            )

        prepare_launch_accessibility(
            parent=self.window(),
            instance_id=version_id,
            game_dir=game_dir,
            launch_dir=launch_dir,
            trigger_source=trigger_source,
            launch_button=self.launch_button,
            on_continue=lambda: self._launch_now(version_id, cfg, game_dir),
        )

    def _launch_now(self, version_id: str, cfg, game_dir: Path) -> None:
        """The actual launch worker, reached only through the single pre-launch accessibility gate."""
        # Direct connect is per instance now (instances page, settings tab): the launch page shows no
        # field but still passes the instance's address to the game, so a configured server keeps being
        # joined at launch time. It is read from the same resolved instance as memory/java, below.
        # Memory is a setting (settings page) or a per-instance override (instance settings); this
        # page no longer keeps a third copy, so the ordinary case reads the launcher setting.
        memory_gb = cfg.memory_gb
        if cfg.memory_auto:
            from launcher.memory import count_mods, suggest_memory_gb, system_memory_gb

            mods_dir = game_dir / "mods"
            memory_gb = suggest_memory_gb(count_mods(mods_dir))
            _total, avail = system_memory_gb()
            if avail < memory_gb + 2:
                set_app_status(
                    self, tr("launch.msg.low_ram", f"{avail:.1f}", f"{memory_gb:.1f}"), "warning"
                )
            else:
                set_app_status(self, tr("launch.msg.auto_memory", f"{memory_gb:.1f}"))

        def do_prepare() -> object:
            try:
                account = resolve_launch_account(
                    AccountStore(), cfg.selected_account, None
                )
                from launcher.instances import resolve_instance

                resolved = resolve_instance(version_id, cfg, game_dir)
                prepared = prepare_launch(
                    version_id,
                    game_dir=game_dir,
                    cache_dir=paths.launcher_dir() / "cache",
                    account=account,
                    memory_gb=resolved.memory_gb if resolved.memory_from_instance else memory_gb,
                    demo=cfg.demo_mode,
                    launch_dir=resolved.launch_dir,
                    java_path=resolved.java_path,
                    language=cfg.game_language or None,
                    jvm_args=resolved.jvm_args or (cfg.jvm_args or None),
                    game_args=resolved.game_args,
                    server=resolved.server or None,
                )
                return ("ok", prepared, resolved)
            except JavaMissingError as exc:
                return ("java", exc.required_major)

        run_in_background(
            do_prepare,
            on_result=self._on_prepared,
            on_error=self._on_launch_error,
            on_finished=lambda: self.launch_button.setEnabled(True),
        )

    def _confirm_java_download(self, major: int) -> bool:
        from gui import inwindow_dialog

        # default_yes keeps the old QMessageBox default button: Enter accepts the download offer
        # instead of dismissing it.
        return inwindow_dialog.question(
            self,
            "Java",
            tr("launch.msg.java_need", major),
            default_yes=True,
        )

    def _download_java_then_retry(self, major: int) -> None:
        from gui.workers import ProgressBridge
        from launcher.java import install_java

        set_app_status(self, tr("launch.msg.java_downloading", major))
        bridge = ProgressBridge()
        bridge.progress.connect(
            lambda p: set_app_status(
                self, "Java: " + str(p.done_files) + "/" + str(p.total_files) + " " + p.current
            )
        )

        def do_install(progress) -> object:
            return install_java(
                major,
                runtime_dir=paths.launcher_dir() / "runtime",
                probe_dir=paths.launcher_dir() / "cache",
                progress=progress,
            )

        run_in_background(
            do_install,
            bridge,
            on_result=lambda _r: self.launch(),
            on_error=lambda m: set_app_status(
                self, tr("launch.msg.java_fail", m), "error"
            ),
        )

    def _on_prepared(self, result) -> None:
        kind = result[0]
        if kind == "java":
            major = result[1]
            if self._confirm_java_download(major):
                self._download_java_then_retry(major)
            else:
                set_app_status(self, tr("launch.msg.cancelled", major))
            return
        _kind, prepared, resolved = result
        command = prepared.command
        self._last_launch = (prepared, resolved)  # the exit callback needs it for the diagnosis
        set_app_status(
            self,
            tr(
                "launch.msg.running",
                prepared.version.id,
                prepared.java.major,
                prepared.account.username,
                tr("common.on") if prepared.isolated else tr("common.off"),
            )
        )

        # After-launch behavior (keep / hide / exit) is handled via a signal
        # bridge back to the main thread once the game process starts.
        from gui.workers import ProgressBridge

        cfg2, _ = config.load()
        start_bridge = ProgressBridge()
        if cfg2.after_launch_behavior != "keep":
            start_bridge.progress.connect(self._on_game_started)

        def do_run() -> object:
            started = __import__("time").time()
            run = run_process(
                command.argv,
                command.cwd,
                on_started=start_bridge if cfg2.after_launch_behavior != "keep" else None,
                capture_tail=True,  # the tail is the only trace when the game dies before logging
            )
            crashes = find_new_crash_reports(command.cwd, started)
            return run, crashes, started

        run_in_background(
            do_run,
            on_result=self._on_game_exit,
            on_error=lambda m: (
                set_app_status(self, tr("launch.msg.run_error", m), "error"),
                show_fatal(self, tr("launch.msg.run_error", m)),
            ),
        )

    def _on_game_started(self, _value=None) -> None:
        from PySide6.QtWidgets import QApplication

        cfg, _ = config.load()
        if cfg.trim_memory_on_launch:
            from launcher.launch.memory import trim_working_set

            trim_working_set()
        set_app_status(self, tr("launch.msg.auto_closing"))
        if cfg.after_launch_behavior == "keep":
            return
        if cfg.after_launch_behavior == "hide":
            # Hide the window but keep the launcher running in the background.
            self.window().hide()
            return
        # Exit: hide first so no stale window remains while the app quits.
        self.window().hide()
        QTimer.singleShot(600, QApplication.instance().quit)

    def _on_game_exit(self, result) -> None:
        run, crashes, started = result
        code = getattr(run, "exit_code", run)
        tail = list(getattr(run, "tail", ()) or ())
        message = tr("launch.msg.exit", code)
        if crashes:
            message += tr("launch.msg.crash", len(crashes))
        set_app_status(self, message)
        self._diagnose(code, tail, started)

    def _diagnose(self, code, tail, started) -> None:
        """Run the knowledge base for a finished launch and show the diagnosis overlay."""
        cfg, _ = config.load()
        if not cfg.auto_crash_analysis:
            return
        pending = getattr(self, "_last_launch", None)
        if pending is None:
            return
        prepared, resolved = pending
        from gui.dialogs.crash_dialog import context_for_launch, diagnose_after_exit

        context = context_for_launch(
            version_id=prepared.version.id,
            game_dir=resolved.game_dir,
            launch_dir=resolved.launch_dir,
            exit_code=code,
            started=started,
            tail=tail,
            instance=resolved,
        )
        diagnose_after_exit(
            self.window(),
            context,
            log_path=resolved.launch_dir / "logs" / "latest.log",
        )

    def _on_launch_error(self, message: str) -> None:
        text = tr("launch.msg.fail", message)
        set_app_status(self, text, "error")
        show_fatal(self, text)  # fatal error dialog
        cfg, _ = config.load()
        if not cfg.auto_crash_analysis:
            return
        from gui.dialogs.crash_dialog import context_for_launch, diagnose_after_exit

        game_dir = cfg.game_dir or paths.default_game_dir()
        context = context_for_launch(
            version_id="", game_dir=game_dir, error_text=text, memory_gb=cfg.memory_gb
        )
        diagnose_after_exit(self.window(), context)
