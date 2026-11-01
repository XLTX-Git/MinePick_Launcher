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

"""Instance detail page: a 200px, eight-entry sidebar next to the instance content.

The panel owns everything that acts on one instance (launching, mods, resource
packs, shaders, saves, settings, diagnosis). It never touches widgets from a worker
thread: every background job reports through @gui.workers.run_in_background with
bound-method callbacks, so a result that arrives after the page was torn down is
dropped by the owner guard instead of dereferencing a freed C++ object.
"""

from __future__ import annotations

import logging
import time
from functools import partial
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui import block_icons, i18n, inwindow_dialog, nav_icons, theme
from gui.accessibility_launch import (
    prepare_launch_accessibility,
    trigger_source_for_click,
)
from gui.errors import show_fatal
from gui.view_state import remember_column_widths
from gui.widgets import (
    PAGE_HEADER_GAP,
    ElidedLabel,
    EmptyState,
    NoWheelDoubleSpinBox,
    NumericTableItem,
    add_search_icon,
    apply_no_focus_outline,
    disable_keeping_focus,
    name_table_for_screen_readers,
    set_app_status,
    style_form,
    style_page_layout,
)
from gui.workers import ProgressBridge, run_in_background
from launcher import config, paths
from launcher.accessibility import TriggerSource
from launcher.auth import AccountStore
from launcher.instances import (
    ISOLATION_FOLLOW,
    ISOLATION_OFF,
    ISOLATION_ON,
    InstancesError,
    delete_instance,
    display_version_name,
    export_instance,
    get_instance,
    instance_dir,
    legacy_instances_dir,
    rename_instance,
    reset_instance_settings,
    resolve_instance,
    set_instance_icon,
    set_instance_settings,
    update_instance,
    update_instance_note,
)
from launcher.launch import (
    find_new_crash_reports,
    prepare_launch,
    resolve_launch_account,
    run_process,
)
from launcher.meta import block_textures
from launcher.mods.local import install_mod_file, scan_mods, set_mod_enabled
from launcher.mods.modrinth import (
    delete_installed_content,
    list_installed_content,
    resolve_content_dir,
)

tr = i18n.tr

# Stacked-page order. The sidebar owns the visual order; the stack is a sequence of pages only.
TAB_OVERVIEW = 0
TAB_MODS = 1
TAB_RESOURCEPACKS = 2
TAB_SHADERPACKS = 3
TAB_SAVES = 4
TAB_MODIFY = 5
TAB_EXPORT = 6
TAB_DIAGNOSIS = 7
# Compatibility aliases for callers that predate the split of resources and the merge of settings
# into the overview. New code should use the explicit constants above.
TAB_RESOURCES = TAB_RESOURCEPACKS
TAB_SETTINGS = TAB_OVERVIEW

_RESOURCE_TABS = (TAB_RESOURCEPACKS, TAB_SHADERPACKS)
_CONTENT_SUBDIRS = ("resourcepacks", "shaderpacks")

#: Sidebar metrics are product constants, not layout accidents: the width gate in
#: tests/test_instance_navigation.py measures every translated label against them.
SIDEBAR_WIDTH = 200
SIDEBAR_ITEM_HEIGHT = 36
SIDEBAR_GROUP_HEIGHT = 20
SIDEBAR_PADDING_X = 12
SIDEBAR_ICON_SIZE = 18
SIDEBAR_ICON_GAP = 8
SIDEBAR_ACCENT_BAR = 3

# One entry per stacked page. (key, stack index, icon name, translation key)
_SECTIONS = (
    ("overview", TAB_OVERVIEW, "overview", "instances.detail.nav.overview"),
    ("mods", TAB_MODS, "mods", "instances.detail.nav.mods"),
    ("resourcepacks", TAB_RESOURCEPACKS, "resourcepacks", "instances.detail.nav.resourcepacks"),
    ("shaderpacks", TAB_SHADERPACKS, "shaderpacks", "instances.detail.nav.shaderpacks"),
    ("saves", TAB_SAVES, "saves", "instances.detail.nav.saves"),
    ("modify", TAB_MODIFY, "modify", "instances.detail.nav.modify"),
    ("export", TAB_EXPORT, "export", "instances.detail.nav.export"),
    ("diagnosis", TAB_DIAGNOSIS, "diagnosis", "instances.detail.nav.diagnosis"),
)
#: Group headers in the sidebar: (translation key, item key that starts the group).
_GROUPS = (
    ("instances.detail.nav.group.resources", "mods"),
    ("instances.detail.nav.group.actions", "modify"),
)
_SEPARATOR_BEFORE = "diagnosis"
_BADGED_SECTIONS = ("mods", "saves")
_LOG_TAIL_CHARS = 64 * 1024

#: The overview's icon control: a 40px tile inside a 50px icon button (the stylesheet's 4px padding
#: plus its 1px border leave exactly the 40px the cards use, so the two surfaces show the same size).
DETAIL_ICON_SIZE = 40
DETAIL_ICON_BUTTON_SIZE = 50

#: The picker's rule tiles: 48px icons in 64px buttons, four per row (two rows of four in the card).
ICON_PICKER_ICON = 48
ICON_PICKER_TILE = 64
ICON_PICKER_COLUMNS = 4


def human_size(size: int) -> str:
    """Format a byte count with the launcher's unit strings."""
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} " + tr("unit.gb")
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} " + tr("unit.mb")
    if size >= 1 << 10:
        return f"{size / (1 << 10):.0f} " + tr("unit.kb")
    return f"{size} " + tr("unit.bytes")


def directory_size(path: Path) -> int:
    """Total size of the regular files below a directory (0 when it is unreadable)."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return total
    return total


def save_custom_icon(source: Path, folder: Path) -> str:
    """Copy a picture into the instance folder as ``icon.<ext>``, scaled to at most 512px.

    The launcher owns this copy: the picture is re-encoded instead of being referenced in place, so
    moving or deleting the original does not break the icon. A picture larger than
    :data:`gui.block_icons.CUSTOM_ICON_MAX_EDGE` is scaled down proportionally first, so a camera
    JPEG cannot put megabytes into the instance folder. The write goes through a ``.tmp`` file and
    ``Path.replace``, so an interrupted save cannot leave a truncated picture behind.

    Returns the stored value (``custom:icon.png``). Raises ``ValueError`` with a user-facing reason
    when the file is not a readable PNG/JPEG/WebP; the caller shows it in the launcher's own dialog.
    """
    source = Path(source)
    suffix = source.suffix.lower()
    if suffix not in block_icons.CUSTOM_ICON_SUFFIXES:
        raise ValueError(tr("instances.icon.error.format"))
    image = QImage(str(source))
    if image.isNull():
        raise ValueError(tr("instances.icon.error.corrupt"))
    if max(image.width(), image.height()) > block_icons.CUSTOM_ICON_MAX_EDGE:
        image = image.scaled(
            block_icons.CUSTOM_ICON_MAX_EDGE,
            block_icons.CUSTOM_ICON_MAX_EDGE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    target_suffix = ".jpg" if suffix in (".jpg", ".jpeg") else suffix
    fmt = target_suffix[1:].upper()  # png -> PNG, jpg -> JPG, webp -> WEBP
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("icon" + target_suffix)
    tmp = folder / (target.name + ".tmp")
    try:
        saved = image.save(str(tmp), fmt, 90 if fmt == "JPG" else -1)
        if not saved and fmt == "WEBP":
            # A Qt build without the WebP writer: store PNG pixels instead of failing the whole
            # upload (the value follows the file name, so the choice keeps working).
            fmt, target = "PNG", folder / "icon.png"
            tmp = folder / (target.name + ".tmp")
            saved = image.save(str(tmp), fmt)
        if not saved:
            raise ValueError(tr("instances.icon.error.write"))
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return block_icons.CUSTOM_ICON_PREFIX + target.name


def _delete_content_file(resolved, subdir: str, name: str) -> int:
    """Delete one resource file; a locked file must not stop the rest of the batch."""
    try:
        delete_installed_content(
            resolved.game_dir, subdir, name, version_id=resolved.id, isolated=resolved.isolated
        )
    except Exception:  # a locked file must not stop the batch; reported as a smaller count
        logging.getLogger(__name__).warning("Cannot delete %s/%s", subdir, name, exc_info=True)
        return 0
    return 1


class _IconChooserDialog(inwindow_dialog.InWindowDialog):
    """The instance-icon picker: eight rule tiles, then 恢复自动 and 从文件选择….

    A tile applies its rule on the click (the callback writes the metadata), so the preview in the
    card and the card behind the backdrop change immediately; Esc closes and keeps what was applied.
    "Restore automatic" applies the empty value and closes. "Choose from file…" closes the card and
    returns ``wants_file``: the page then opens the native ``QFileDialog``, so a native window is
    never stacked inside this blocking in-window card.

    Keyboard: every tile and both bottom buttons are ordinary tab stops, and Tab stays trapped
    inside the card by :class:`InWindowDialog`. Focus starts on the tile currently in force.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        current: str,
        previews: dict[str, QPixmap],
        auto_pixmap: QPixmap,
        current_pixmap: QPixmap,
        on_pick,
    ) -> None:
        super().__init__(parent, title=tr("instances.icon.title"), severity="question")
        self.current = str(current or "")
        self.wants_file = False
        self._previews = dict(previews)
        self._auto_pixmap = auto_pixmap
        self._current_pixmap = current_pixmap
        self._on_pick = on_pick
        self.add_body(tr("instances.icon.hint"))

        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(12)
        self.preview = QLabel()
        self.preview.setFixedSize(ICON_PICKER_ICON, ICON_PICKER_ICON)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setAccessibleName(tr("instances.icon.title"))
        self.preview_caption = QLabel("")
        self.preview_caption.setObjectName("overlayBody")
        self.preview_caption.setWordWrap(True)
        preview_row.addWidget(self.preview)
        preview_row.addWidget(self.preview_caption, 1)
        self.card_layout.addLayout(preview_row)

        self.rule_buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        for index, rule in enumerate(block_icons.RULE_ICON_NAMES):
            button = QPushButton()
            button.setObjectName("iconButton")
            button.setCheckable(True)
            # Inside a QDialog every QPushButton is autoDefault by default, which would make Enter
            # activate an arbitrary tile with an invisible default ring. Enter closes the card; Space
            # activates the focused tile.
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setFixedSize(ICON_PICKER_TILE, ICON_PICKER_TILE)
            button.setIconSize(QSize(ICON_PICKER_ICON, ICON_PICKER_ICON))
            button.setIcon(QIcon(previews[rule]))
            label = tr("instances.icon.rule." + rule)
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.clicked.connect(partial(self._pick_rule, rule))
            self._group.addButton(button)
            self.rule_buttons[rule] = button
            grid.addWidget(
                button,
                index // ICON_PICKER_COLUMNS,
                index % ICON_PICKER_COLUMNS,
                Qt.AlignmentFlag.AlignCenter,
            )
        self.card_layout.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.auto_button = QPushButton(tr("instances.icon.auto"))
        self.auto_button.setObjectName("secondaryButton")
        self.auto_button.setAutoDefault(False)
        self.auto_button.setDefault(False)
        self.auto_button.clicked.connect(self._choose_auto)
        self.file_button = QPushButton(tr("instances.icon.file"))
        self.file_button.setObjectName("primaryButton")
        self.file_button.setAutoDefault(False)
        self.file_button.setDefault(False)
        self.file_button.clicked.connect(self._choose_file)
        row.addWidget(self.auto_button)
        row.addWidget(self.file_button)
        self.card_layout.addLayout(row)
        self._sync_preview()

    # ---------- behaviour ----------

    def _pick_rule(self, rule: str) -> None:
        """Apply a rule now; the preview only follows when the write really happened."""
        if self._on_pick(rule):
            self.current = rule
            self._sync_preview()

    def _choose_auto(self) -> None:
        if self._on_pick(""):
            self.current = ""
            self._sync_preview()
            self.accept()

    def _choose_file(self) -> None:
        self.wants_file = True
        self.accept()

    def _sync_preview(self) -> None:
        """Mark the tile in force and show what it looks like, plus its name."""
        self._group.setExclusive(False)
        for rule, button in self.rule_buttons.items():
            button.setChecked(rule == self.current)
        self._group.setExclusive(True)
        if not self.current:
            caption = tr("instances.icon.auto.name")
            pixmap = self._auto_pixmap
        elif self.current in self._previews:
            caption = tr("instances.icon.rule." + self.current)
            pixmap = self._previews[self.current]
        elif self.current.startswith(block_icons.CUSTOM_ICON_PREFIX):
            caption = tr("instances.icon.custom")
            pixmap = self._current_pixmap
        else:
            # A rule name this build does not know: the reader shows the automatic rule, so the
            # preview says "automatic" rather than silently pretending it is a custom picture.
            caption = tr("instances.icon.auto.name")
            pixmap = self._auto_pixmap
        self.preview.setPixmap(pixmap)
        text = tr("instances.icon.current", caption)
        self.preview_caption.setText(text)
        self.preview.setAccessibleName(text)

    def _focus_initial(self) -> None:
        """Start on the rule currently in force, not on a button the user has to hunt for."""
        target = self.rule_buttons.get(self.current)
        if target is None:
            target = next(iter(self.rule_buttons.values()), None)
        if target is not None:
            target.setFocus(Qt.FocusReason.TabFocusReason)


class _ModsTable(QTableWidget):
    """Mods table: supports drag-and-drop of .jar files to install."""

    def __init__(self, detail: InstanceDetail) -> None:
        super().__init__(0, 6)
        self._detail = detail
        self.setAcceptDrops(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.ExtendedSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.horizontalHeader().setStretchLastSection(True)
        # The header is text only. Qt draws a sort indicator - a small chevron in a section's corner
        # that reads as a dropdown - as soon as sorting is on, and it lands in 启用 because that is
        # the first column. There is nothing to sort by here: scan_mods() already returns the rows in
        # display-name order, and sorting would re-order the list on every refill by the checkbox
        # column (every disabled mod first). The enabled/disabled *filter* is the toolbar combo
        # (全部 / 已启用 / 已禁用), which is where a filter belongs.
        self.setSortingEnabled(False)
        self.horizontalHeader().setSortIndicatorShown(False)

    def _jar_urls(self, event) -> bool:
        data = event.mimeData()
        if not data.hasUrls():
            return False
        return any(
            u.isLocalFile() and u.toLocalFile().lower().endswith(".jar")
            for u in data.urls()
        )

    def dragEnterEvent(self, event) -> None:
        if self._jar_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._jar_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        jars = [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if u.isLocalFile() and u.toLocalFile().lower().endswith(".jar")
        ]
        if jars:
            self._detail.install_dropped(jars)
            event.acceptProposedAction()
        else:
            event.ignore()


class InstanceDetail(QWidget):
    """Right-hand column of the instances page: everything about one instance."""

    changed = Signal()  # metadata shown in the list changed (name / note / isolation / icon)
    deleted = Signal(str)  # the instance folder is gone

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Scopes the light sheet's paper-painted scroll viewport to this panel. The panel itself stays
        # transparent on purpose: it is a tabbed inspector, not a card, so its tables keep being the
        # white surfaces floating on the page paper.
        self.setObjectName("instanceDetail")
        self.instance_id: str | None = None
        self.instance = None  # launcher.instances.Instance
        self.resolved = None  # launcher.instances.ResolvedInstance
        self.game_dir: Path = paths.default_game_dir()

        # Working state
        self._mods: list = []
        self._filling = False  # a widget fill is running: ignore the change signals it fires
        self._filling_mods = False
        self._isolation_warned = False
        self._loaded_tabs: set[int] = set()
        self._content_items: dict[str, list] = {sub: [] for sub in _CONTENT_SUBDIRS}
        self._saves: list[tuple[str, float, int]] = []
        self._badge_counts: dict[str, int] = {key: 0 for key in _BADGED_SECTIONS}

        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tabs: list[QPushButton] = []
        self.nav_buttons: dict[str, QPushButton] = {}
        self.nav_labels: dict[str, QLabel] = {}
        self.nav_icon_labels: dict[str, QLabel] = {}
        self.nav_badges: dict[str, QLabel] = {}
        self.nav_group_labels: list[QLabel] = []

        main = QHBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(16)
        self.sidebar = self._build_sidebar()
        main.addWidget(self.sidebar)

        content = QWidget()
        content.setObjectName("instanceContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        # Exactly PAGE_HEADER_GAP below the header, like every top-level page: this layout holds only
        # the header and the section stack, so its spacing is that one distance.
        content_layout.setSpacing(PAGE_HEADER_GAP)
        content_layout.addWidget(self._build_header())

        self.stack = QStackedWidget()
        self.stack.setObjectName("instanceContentStack")
        for builder in (
            self._build_overview,
            self._build_mods,
            self._build_resourcepacks_page,
            self._build_shaderpacks_page,
            self._build_saves,
            self._build_modify,
            self._build_export,
            self._build_diagnosis,
        ):
            self.stack.addWidget(builder())

        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("instanceContentScroll")
        # WCAG 4.1.2: a QScrollArea is a keyboard stop of its own and must say what it scrolls.
        self.content_scroll.setAccessibleName(tr("a11y.instance_content"))
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_scroll.setWidget(self.stack)
        content_layout.addWidget(self.content_scroll, 1)
        main.addWidget(content, 1)

        self.tab_group.idClicked.connect(self._on_tab_clicked)
        self.launch_button.clicked.connect(self._on_launch_clicked)
        self.crash_button.clicked.connect(self.open_crash_viewer)
        self.tabs[TAB_OVERVIEW].setChecked(True)
        self._sync_sidebar()

    # ---------- construction ----------

    def _build_sidebar(self) -> QScrollArea:
        """The eight-entry sidebar: 200px wide, 36px items, 20px group headings, scrollable.

        Items are real checkable buttons (a screen reader announces the label and the badge, and the
        keyboard can reach every section). Labels are QLabels rather than button text so the 12px
        horizontal padding, 18px icon, 8px gap and badge can be measured against a fixed width; the
        corresponding gate lives in tests/test_instance_navigation.py.
        """
        scroll = QScrollArea()
        scroll.setObjectName("instanceNavScroll")
        scroll.setAccessibleName(tr("a11y.instance_sidebar"))
        scroll.setFixedWidth(SIDEBAR_WIDTH)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        body = QWidget()
        body.setObjectName("instanceNavBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        group_starts = {item_key: title_key for title_key, item_key in _GROUPS}
        for section in _SECTIONS:
            key, index, _icon, label_key = section
            if key in group_starts:
                group = QLabel(tr(group_starts[key]))
                group.setObjectName("instanceNavGroup")
                group.setFixedHeight(SIDEBAR_GROUP_HEIGHT)
                group.setWordWrap(False)
                body_layout.addSpacing(8)
                body_layout.addWidget(group)
                self.nav_group_labels.append(group)
            if key == _SEPARATOR_BEFORE:
                body_layout.addSpacing(8)
                separator = QFrame()
                separator.setObjectName("instanceNavSeparator")
                separator.setFixedHeight(1)
                body_layout.addWidget(separator)
                body_layout.addSpacing(8)
            body_layout.addWidget(self._build_nav_item(key, index, label_key))
        body_layout.addStretch(1)
        scroll.setWidget(body)
        self.sidebar_body = body
        return scroll

    def _build_nav_item(self, key: str, index: int, label_key: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("instanceNavItem")
        button.setCheckable(True)
        button.setFixedHeight(SIDEBAR_ITEM_HEIGHT)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setAccessibleName(tr(label_key))
        button.setToolTip(tr(label_key))

        row = QHBoxLayout(button)
        row.setContentsMargins(SIDEBAR_PADDING_X, 0, SIDEBAR_PADDING_X, 0)
        row.setSpacing(SIDEBAR_ICON_GAP)

        icon = QLabel()
        icon.setObjectName("instanceNavIcon")
        icon.setFixedSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE)
        # The whole line belongs to the button: child labels must not swallow its click.
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        label = QLabel(tr(label_key))
        label.setObjectName("instanceNavLabel")
        label.setWordWrap(False)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        badge = QLabel("")
        badge.setObjectName("instanceNavBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedHeight(18)
        badge.setVisible(False)
        badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(icon)
        row.addWidget(label, 1)
        row.addWidget(badge)

        self.tab_group.addButton(button, index)
        self.tabs.append(button)
        self.nav_buttons[key] = button
        self.nav_labels[key] = label
        self.nav_icon_labels[key] = icon
        if key in _BADGED_SECTIONS:
            self.nav_badges[key] = badge
        else:
            badge.deleteLater()
        return button

    def _nav_colors(self) -> tuple[str, str]:
        tokens = theme.current_tokens() or theme.theme_palette("light")
        return tokens["FG_HINT"], tokens["ACCENT"]

    def _sync_sidebar(self) -> None:
        """Update icon colours, selected-label properties and the keyboard focus cue."""
        resting, selected = self._nav_colors()
        for key, _index, icon_name, _label_key in _SECTIONS:
            button = self.nav_buttons[key]
            active = button.isChecked()
            color = selected if active else resting
            self.nav_icon_labels[key].setPixmap(
                nav_icons.nav_icon(icon_name, color, SIDEBAR_ICON_SIZE).pixmap(
                    SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE
                )
            )
            label = self.nav_labels[key]
            if bool(label.property("active")) != active:
                label.setProperty("active", active)
                label.style().unpolish(label)
                label.style().polish(label)
            badge = self.nav_badges.get(key)
            if badge is not None and bool(badge.property("active")) != active:
                badge.setProperty("active", active)
                badge.style().unpolish(badge)
                badge.style().polish(badge)

    def _on_tab_clicked(self, index: int) -> None:
        self._show_section(index)

    def _show_section(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self._sync_sidebar()
        self._ensure_tab_data(index)

    def set_tab(self, index: int) -> None:
        """Activate a section programmatically (used by tests and cross-page jumps)."""
        index = max(0, min(index, len(self.tabs) - 1))
        self.tabs[index].setChecked(True)
        self._show_section(index)

    def _build_header(self) -> QWidget:
        """This page's header: the instance name over its id and directory.

        The second level keeps the heading type: the name and the two hint lines are styled by the
        same sheet rule as the launch version block (through their own object names). The header
        paints no coloured block and no card. The back control lives above it in
        :class:`InstanceDetailPage`; top-level pages no longer draw a header at all.
        """
        box = QWidget()
        self.name_label = QLabel(tr("instances.detail.value.none"))
        self.name_label.setObjectName("instanceDetailTitle")
        self.id_label = QLabel("")
        self.id_label.setObjectName("instanceDetailHint")
        # Paths read better elided on the left: the tail (folder name) is the informative part
        self.dir_label = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideLeft)
        self.dir_label.setObjectName("instanceDetailHint")
        self.launch_button = QPushButton(tr("instances.launch"))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.name_label)
        row.addWidget(self.id_label)
        row.addStretch(1)
        row.addWidget(self.launch_button)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(row)
        layout.addWidget(self.dir_label)
        return box

    def _make_card(self) -> tuple[QWidget, QVBoxLayout]:
        """One white detail card, with the shared 16px inset and 12px inner rhythm."""
        card = QWidget()
        card.setObjectName("instanceCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        return card, layout

    def _card_heading(self, key: str) -> QLabel:
        label = QLabel(tr(key))
        label.setObjectName("instanceCardTitle")
        label.setWordWrap(False)
        return label

    def _build_overview(self) -> QWidget:
        """Overview and instance settings, merged into one page with two cards."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, card_layout = self._make_card()
        self.overview_card = card
        # The icon is the card's one picture and its change control: a 40px tile in a real button
        # (keyboard reachable, named for screen readers), with the card title beside it.
        self.icon_button = QPushButton()
        self.icon_button.setObjectName("iconButton")
        self.icon_button.setFixedSize(DETAIL_ICON_BUTTON_SIZE, DETAIL_ICON_BUTTON_SIZE)
        self.icon_button.setIconSize(QSize(DETAIL_ICON_SIZE, DETAIL_ICON_SIZE))
        self.icon_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon_button.setAccessibleName(tr("instances.icon.change"))
        self.icon_button.setToolTip(tr("instances.icon.change"))
        self.icon_button.setEnabled(False)
        self.icon_button.clicked.connect(self.choose_icon)
        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.setSpacing(12)
        heading_row.addWidget(self.icon_button, 0, Qt.AlignmentFlag.AlignVCenter)
        heading_row.addWidget(
            self._card_heading("instances.detail.card.overview"), 1, Qt.AlignmentFlag.AlignVCenter
        )
        card_layout.addLayout(heading_row)
        form = QFormLayout()
        style_form(form)
        self.overview_values: dict[str, QLabel] = {}
        for key, label_key in (
            ("version", "instances.detail.row.version"),
            ("profile", "instances.detail.row.profile"),
            ("game_dir", "instances.detail.row.game_dir"),
            ("isolation", "instances.detail.row.isolation"),
            ("java", "instances.detail.row.java"),
            ("memory", "instances.detail.row.memory"),
            ("last_played", "instances.detail.row.last_played"),
            ("note", "instances.detail.row.note"),
        ):
            title = QLabel(tr(label_key))
            title.setObjectName("hint")
            value = ElidedLabel(
                tr("instances.detail.value.none"),
                elide_mode=(
                    Qt.TextElideMode.ElideLeft if key == "game_dir" else Qt.TextElideMode.ElideMiddle
                ),
            )
            self.overview_values[key] = value
            form.addRow(title, value)
        card_layout.addLayout(form)

        self.legacy_label = QLabel("")
        self.legacy_label.setObjectName("hint")
        self.legacy_label.setWordWrap(True)
        self.legacy_label.setVisible(False)
        card_layout.addWidget(self.legacy_label)

        self.open_folder_button = QPushButton(tr("instances.open_folder"))
        self.open_folder_button.setObjectName("secondaryButton")
        self.open_logs_button = QPushButton(tr("instances.detail.open_logs"))
        self.open_logs_button.setObjectName("secondaryButton")
        self.crash_button = QPushButton(tr("crash.viewer"))
        self.crash_button.setObjectName("secondaryButton")
        self.rename_button = QPushButton(tr("instances.rename"))
        self.rename_button.setObjectName("secondaryButton")
        self.delete_button = QPushButton(tr("instances.delete"))
        self.delete_button.setObjectName("dangerButton")
        files_row = QHBoxLayout()
        files_row.setContentsMargins(0, 0, 0, 0)
        files_row.setSpacing(6)
        for button in (self.open_folder_button, self.open_logs_button, self.crash_button):
            files_row.addWidget(button)
        files_row.addStretch(1)
        manage_row = QHBoxLayout()
        manage_row.setContentsMargins(0, 0, 0, 0)
        manage_row.setSpacing(6)
        for button in (self.rename_button, self.delete_button):
            manage_row.addWidget(button)
        manage_row.addStretch(1)
        card_layout.addLayout(files_row)
        card_layout.addLayout(manage_row)
        layout.addWidget(card)

        # The former 设置 tab is now the second card of 概览: one destination, no duplicate navigation.
        layout.addWidget(self._build_settings())
        layout.addStretch(1)

        self.open_folder_button.clicked.connect(self.open_instance_folder)
        self.open_logs_button.clicked.connect(self.open_logs_folder)
        self.rename_button.clicked.connect(self.rename)
        self.delete_button.clicked.connect(self.delete)
        return page

    def _build_mods(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        banner, self.mods_banner_label = self._build_share_banner()
        self.mods_banner = banner
        layout.addWidget(banner)

        self.mods_table = _ModsTable(self)
        apply_no_focus_outline(self.mods_table)
        name_table_for_screen_readers(self.mods_table, tr("a11y.instance_mods"))
        self.mods_table.setHorizontalHeaderLabels(
            [
                tr("instances.mods.col.enabled"),
                tr("instances.mods.col.name"),
                tr("instances.mods.col.id"),
                tr("instances.mods.col.version"),
                tr("instances.mods.col.loader"),
                tr("instances.mods.col.file"),
            ]
        )
        self.mods_table.setColumnWidth(0, 56)
        self.mods_table.setColumnWidth(1, 150)
        self.mods_table.setColumnWidth(2, 110)
        self.mods_table.setColumnWidth(3, 72)
        self.mods_table.setColumnWidth(4, 80)
        self.mods_search = QLineEdit()
        add_search_icon(self.mods_search)
        self.mods_search.setPlaceholderText(tr("instances.mods.search.placeholder"))
        self.mods_search.setAccessibleName(tr("instances.mods.search.placeholder"))  # WCAG 4.1.2
        self.mods_search.setClearButtonEnabled(True)
        self.mods_filter = QComboBox()
        self.mods_filter.addItem(tr("instances.mods.filter.all"), "all")
        self.mods_filter.addItem(tr("instances.mods.filter.enabled"), "enabled")
        self.mods_filter.addItem(tr("instances.mods.filter.disabled"), "disabled")
        # WCAG 4.1.2: it appears nowhere near a QFormLayout row, so nothing supplied a name for it.
        self.mods_filter.setAccessibleName(tr("a11y.mods_filter"))
        self.mods_refresh_button = QPushButton(tr("instances.mods.refresh"))
        self.mods_refresh_button.setObjectName("secondaryButton")
        self.mods_folder_button = QPushButton(tr("instances.mods.open_folder"))
        self.mods_folder_button.setObjectName("secondaryButton")
        self.mods_delete_button = QPushButton(tr("instances.mods.delete"))
        self.mods_delete_button.setObjectName("dangerButton")
        # Dragging jars onto the table is a pointer gesture with no keyboard equivalent, which fails
        # WCAG 2.5.1 and 2.5.7. This button is that equivalent: it opens a file picker and feeds the
        # very same install path the drop handler uses, so both routes behave identically.
        self.mods_install_button = QPushButton(tr("instances.mods.install"))
        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        tools.addWidget(self.mods_search, 1)
        tools.addWidget(self.mods_filter)
        tools.addWidget(self.mods_refresh_button)
        tools.addWidget(self.mods_folder_button)
        tools.addWidget(self.mods_delete_button)
        layout.addLayout(tools)
        # Its own row on purpose: inside the tools row this button raised the window's own minimum
        # width by up to 173px depending on the language (measured), because every fixed widget in a
        # row adds to that row's minimum. Alone in a row it costs height, which the layout can spare.
        install_row = QHBoxLayout()
        install_row.setContentsMargins(0, 0, 0, 0)
        install_row.addWidget(self.mods_install_button)
        install_row.addStretch(1)
        layout.addLayout(install_row)
        layout.addWidget(self.mods_table, 1)
        remember_column_widths(self.mods_table, "instance_mods")

        self.mods_table.itemChanged.connect(self._on_mod_toggled)
        # Debounce: refill only after 200ms of typing inactivity
        self._mods_search_timer = QTimer(self)
        self._mods_search_timer.setSingleShot(True)
        self._mods_search_timer.timeout.connect(self._refill_mods)
        self.mods_search.textChanged.connect(lambda _t: self._mods_search_timer.start(200))
        self.mods_filter.currentIndexChanged.connect(lambda _i: self._refill_mods())
        self.mods_refresh_button.clicked.connect(self._reload_mods)
        self.mods_folder_button.clicked.connect(self.open_mods_folder)
        self.mods_delete_button.clicked.connect(self.delete_selected_mods)
        self.mods_install_button.clicked.connect(self.choose_mod_files)
        return page

    def _build_resourcepacks_page(self) -> QWidget:
        self.content_tables: dict[str, QTableWidget] = {}
        self.content_empty: dict[str, EmptyState] = {}
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        banner, self.res_banner_label = self._build_share_banner()
        self.res_banner = banner
        layout.addWidget(banner)
        box, table, empty = self._build_content_section(
            "resourcepacks", tr("mods.tab.resourcepacks")
        )
        self.content_tables["resourcepacks"] = table
        self.content_empty["resourcepacks"] = empty
        layout.addWidget(box, 1)
        return page

    def _build_shaderpacks_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        banner, self.shader_banner_label = self._build_share_banner()
        self.shader_banner = banner
        layout.addWidget(banner)
        box, table, empty = self._build_content_section(
            "shaderpacks", tr("mods.tab.shaderpacks")
        )
        self.content_tables["shaderpacks"] = table
        self.content_empty["shaderpacks"] = empty
        layout.addWidget(box, 1)
        return page

    def _build_content_section(self, subdir: str, title: str):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("title")
        table = QTableWidget(0, 2)
        name_table_for_screen_readers(table, title)
        table.setHorizontalHeaderLabels(
            [tr("instances.detail.col.name"), tr("instances.detail.col.size")]
        )
        apply_no_focus_outline(table)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.ExtendedSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 280)
        remember_column_widths(table, "instance_" + subdir)
        empty = EmptyState(tr("mods.installed.empty"))
        open_button = QPushButton(tr("instances.detail.open_folder"))
        open_button.setObjectName("secondaryButton")
        delete_button = QPushButton(tr("instances.mods.delete"))
        delete_button.setObjectName("dangerButton")
        open_button.clicked.connect(partial(self.open_content_folder, subdir))
        delete_button.clicked.connect(partial(self.delete_selected_content, subdir))
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(open_button)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        layout.addWidget(title_label)
        layout.addWidget(table, 1)
        layout.addWidget(empty)
        layout.addLayout(buttons)
        return box, table, empty

    def _build_saves(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.saves_table = QTableWidget(0, 3)
        name_table_for_screen_readers(self.saves_table, tr("a11y.instance_saves"))
        self.saves_table.setHorizontalHeaderLabels(
            [
                tr("instances.detail.col.name"),
                tr("instances.detail.col.modified"),
                tr("instances.detail.col.size"),
            ]
        )
        apply_no_focus_outline(self.saves_table)
        self.saves_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.saves_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.saves_table.horizontalHeader().setStretchLastSection(True)
        self.saves_table.setColumnWidth(0, 260)
        remember_column_widths(self.saves_table, "instance_saves")
        self.saves_empty = EmptyState(tr("instances.detail.saves.empty"))
        self.saves_refresh_button = QPushButton(tr("instances.mods.refresh"))
        self.saves_refresh_button.setObjectName("secondaryButton")
        self.saves_folder_button = QPushButton(tr("instances.detail.saves.open_folder"))
        self.saves_folder_button.setObjectName("secondaryButton")
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.saves_refresh_button)
        buttons.addWidget(self.saves_folder_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(self.saves_table, 1)
        layout.addWidget(self.saves_empty)
        self.saves_refresh_button.clicked.connect(self.reload_saves)
        self.saves_folder_button.clicked.connect(self.open_saves_folder)
        return page

    def _build_diagnosis(self) -> QWidget:
        """Diagnosis and the live latest.log, merged into one page (two cards)."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, card_layout = self._make_card()
        card_layout.addWidget(self._card_heading("instances.detail.tab.diagnosis"))
        self.diagnosis_empty = EmptyState(tr("instances.detail.diagnosis.none"))
        card_layout.addWidget(self.diagnosis_empty)

        self.diagnosis_code = QLabel("")
        self.diagnosis_code.setObjectName("title")
        self.diagnosis_code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diagnosis_time = QLabel("")
        self.diagnosis_time.setObjectName("hint")
        self.diagnosis_body = QLabel("")
        self.diagnosis_body.setObjectName("hint")
        self.diagnosis_body.setWordWrap(True)
        self.diagnosis_body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.diagnosis_body.setAlignment(Qt.AlignmentFlag.AlignTop)
        for widget in (self.diagnosis_code, self.diagnosis_time, self.diagnosis_body):
            widget.setVisible(False)
            card_layout.addWidget(widget)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.diagnosis_copy_button = QPushButton(tr("instances.detail.diagnosis.copy"))
        self.diagnosis_copy_button.setObjectName("secondaryButton")
        self.diagnosis_copy_button.clicked.connect(self.copy_diagnosis)
        self.diagnosis_reports_label = QLabel("")
        self.diagnosis_reports_label.setObjectName("hint")
        row.addWidget(self.diagnosis_copy_button)
        row.addWidget(self.diagnosis_reports_label)
        row.addStretch(1)
        card_layout.addLayout(row)
        layout.addWidget(card)

        log_card, log_layout = self._make_card()
        log_layout.addWidget(self._card_heading("instances.detail.log.title"))
        self.log_path_label = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideLeft)
        self.log_path_label.setObjectName("hint")
        self.log_empty = QLabel(tr("instances.detail.log.empty"))
        self.log_empty.setObjectName("emptyState")
        self.log_empty.setWordWrap(True)
        self.log_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("instanceLogView")
        self.log_view.setReadOnly(True)
        self.log_view.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.log_view.setAccessibleName(tr("instances.detail.log.title"))
        self.log_view.setMinimumHeight(240)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setVisible(False)
        log_layout.addWidget(self.log_path_label)
        log_layout.addWidget(self.log_empty)
        log_layout.addWidget(self.log_view, 1)
        layout.addWidget(log_card)
        layout.addStretch(1)

        self._diagnosis_lines: list[str] = []
        self.diagnosis_copy_button.setEnabled(False)
        return page

    def _build_modify(self) -> QWidget:
        return self._build_not_implemented("modify")

    def _build_export(self) -> QWidget:
        return self._build_not_implemented("export")

    def _build_not_implemented(self, key: str) -> QWidget:
        """An honest placeholder page: the capability is absent, so no control pretends to offer it."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card, card_layout = self._make_card()
        heading = self._card_heading("instances.detail.not_implemented.title")
        body = QLabel(tr("instances.detail.not_implemented." + key))
        body.setObjectName("hint")
        body.setWordWrap(True)
        card_layout.addWidget(heading)
        card_layout.addWidget(body)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _refresh_log(self) -> None:
        """Show the tail of this instance's latest.log directly in the diagnosis page."""
        if self.resolved is None:
            self.log_path_label.setText("")
            self.log_view.clear()
            self.log_view.setVisible(False)
            self.log_empty.setVisible(True)
            return
        log_path = self.resolved.launch_dir / "logs" / "latest.log"
        self.log_path_label.setText(str(log_path))
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if len(text) > _LOG_TAIL_CHARS:
            text = text[-_LOG_TAIL_CHARS:]
        self.log_view.setPlainText(text)
        self.log_view.setVisible(bool(text))
        self.log_empty.setVisible(not text)

    def refresh_diagnosis(self) -> None:
        """Show the diagnosis recorded for this instance, plus its crash report count."""
        from gui.crash_viewer import collect_crash_reports
        from gui.dialogs.crash_dialog import load_recorded_diagnosis

        if self.instance_id is None:
            return
        record = load_recorded_diagnosis(instance_dir(self.game_dir, self.instance_id))
        has_record = bool(record and record.get("code"))
        self.diagnosis_empty.setVisible(not has_record)
        for widget in (self.diagnosis_code, self.diagnosis_time, self.diagnosis_body):
            widget.setVisible(has_record)
        self.diagnosis_copy_button.setEnabled(has_record)
        self._diagnosis_lines = []
        if has_record:
            self.diagnosis_code.setText(str(record.get("code", "")))
            recorded_at = record.get("recorded_at")
            stamp = ""
            if recorded_at:
                try:
                    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(recorded_at)))
                except (TypeError, ValueError, OSError):
                    stamp = ""
            self.diagnosis_time.setText(tr("instances.detail.diagnosis.recorded", stamp))
            self._diagnosis_lines = [str(line) for line in record.get("lines", []) if str(line)]
            self.diagnosis_body.setText("\n".join(self._diagnosis_lines))
        else:
            self.diagnosis_code.clear()
            self.diagnosis_time.clear()
            self.diagnosis_body.clear()
        try:
            count = len(collect_crash_reports(self.effective_dir()))
        except OSError:
            count = 0
        self.diagnosis_reports_label.setText(tr("instances.detail.diagnosis.reports", count))
        self._refresh_log()

    def effective_dir(self) -> Path:
        """The directory the game actually uses for this instance (its own folder when isolated)."""
        if self.resolved is not None:
            return self.resolved.launch_dir
        return self.game_dir

    def copy_diagnosis(self) -> None:
        if not self._diagnosis_lines:
            return
        summary = self.diagnosis_code.text() + "\n" + "\n".join(self._diagnosis_lines)
        QGuiApplication.clipboard().setText(summary)
        set_app_status(self, tr("instances.detail.diagnosis.copied"))

    def _build_settings(self) -> QWidget:
        card, layout = self._make_card()
        self.settings_card = card
        layout.addWidget(self._card_heading("instances.detail.card.settings"))
        form = QFormLayout()
        style_form(form)

        self.isolation_combo = QComboBox()
        # Long option texts must not dictate the page width (German/French labels otherwise push the
        # whole window's minimum width past a 1024px screen): show a bounded number of characters
        # and let the popup list size itself.
        self.isolation_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.isolation_combo.setMinimumContentsLength(14)
        self.isolation_combo.addItem(tr("instances.detail.settings.follow_global"), ISOLATION_FOLLOW)
        self.isolation_combo.addItem(tr("instances.detail.settings.isolation.on"), ISOLATION_ON)
        self.isolation_combo.addItem(tr("instances.detail.settings.isolation.off"), ISOLATION_OFF)

        # A checkbox label doubles as its click target, so it must stay readable rather than elide: the
        # visible text is a short noun phrase and the full sentence moves to the tooltip. Spanish made
        # this checkbox 354px wide, which was the floor under the whole window's minimum width.
        self.java_follow_check = QCheckBox(tr("instances.detail.settings.java.follow.short"))
        self.java_follow_check.setToolTip(tr("instances.detail.settings.java.follow"))
        self.java_edit = QLineEdit()
        self.java_edit.setPlaceholderText(tr("instances.detail.java.follow_auto"))
        # WCAG 4.1.2: the four fields below sit inside a box or an ElidedLabel row instead of in a
        # QFormLayout row of their own, so the form never handed the label over and each was announced
        # as an unnamed edit box / spinner. They are named after the form row they appear in, which is
        # the label a sighted user reads next to them.
        self.java_edit.setAccessibleName(tr("instances.detail.row.java"))
        self.java_browse_button = QPushButton(tr("settings.browse"))
        self.java_browse_button.setObjectName("secondaryButton")
        java_box = QWidget()
        java_layout = QVBoxLayout(java_box)
        java_layout.setContentsMargins(0, 0, 0, 0)
        java_layout.setSpacing(4)
        java_row = QHBoxLayout()
        java_row.setContentsMargins(0, 0, 0, 0)
        java_row.setSpacing(6)
        java_row.addWidget(self.java_edit, 1)
        java_row.addWidget(self.java_browse_button)
        java_layout.addWidget(self.java_follow_check)
        java_layout.addLayout(java_row)

        self.memory_follow_check = QCheckBox(tr("instances.detail.settings.follow_global.short"))
        self.memory_follow_check.setToolTip(tr("instances.detail.settings.follow_global"))
        self.memory_spin = NoWheelDoubleSpinBox()
        self.memory_spin.setRange(1.0, 64.0)
        self.memory_spin.setSingleStep(0.5)
        # The number is the value; the row label carries the unit, so no suffix here.
        self.memory_spin.setAccessibleName(
            tr("instances.detail.row.memory") + tr("unit.gb.label")
        )
        memory_box = QWidget()
        memory_layout = QVBoxLayout(memory_box)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        memory_layout.setSpacing(4)
        memory_layout.addWidget(self.memory_follow_check)
        memory_layout.addWidget(self.memory_spin)

        self.jvm_args_edit = QLineEdit()
        self.jvm_args_edit.setPlaceholderText(tr("instances.detail.settings.jvm_args"))
        self.jvm_args_edit.setAccessibleName(tr("instances.detail.settings.jvm_args"))
        self.game_args_edit = QLineEdit()
        self.game_args_edit.setPlaceholderText(tr("instances.detail.settings.game_args"))
        self.game_args_edit.setAccessibleName(tr("instances.detail.settings.game_args"))
        # Direct connect is the only way to hand the game a --server, and it is per instance: the launch
        # page no longer shows a field (it read as an advanced duplicate of a normal launch), so the value
        # lives here with the other per-instance settings and the launch page reads it when it launches.
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText(tr("launch.server.placeholder"))
        self.server_edit.setAccessibleName(tr("launch.server"))

        form.addRow(tr("instances.detail.row.isolation"), self.isolation_combo)
        form.addRow(tr("instances.detail.row.java"), java_box)
        form.addRow(
            tr("instances.detail.row.memory") + tr("unit.gb.label"), memory_box
        )
        # Direct connect: the row label is a plain label, not an ElidedLabel. The form measures its label
        # column before the stylesheet's 13px font reaches the widget (the fallback font measures this
        # string ~40% narrower), so an eliding label either collapsed to nothing or, with a minimum width,
        # overlapped its own field. _fill_settings re-measures this one in the painted font and lets the
        # form re-run its column sizing, which keeps the label beside the field instead of under it.
        self.server_label = QLabel(tr("launch.server"))
        form.addRow(self.server_label, self.server_edit)
        self.settings_form = form
        # These two used to be ElidedLabels. Their horizontal size policy is Ignored so a value can
        # shrink, but QFormLayout then measured the label column before the stylesheet font arrived and
        # reserved zero width for both, which made "JVM 参数" and "游戏参数" disappear entirely. A plain
        # wrapping label is the correct control here: the full sentence stays readable even in the long
        # languages, and it never asks the form for a minimum width that would compete with the fields.
        self.jvm_args_label = QLabel(tr("instances.detail.settings.jvm_args"))
        self.jvm_args_label.setWordWrap(True)
        self.jvm_args_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.game_args_label = QLabel(tr("instances.detail.settings.game_args"))
        self.game_args_label.setWordWrap(True)
        self.game_args_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        form.addRow(self.jvm_args_label, self.jvm_args_edit)
        form.addRow(self.game_args_label, self.game_args_edit)
        layout.addLayout(form)

        self.settings_save_button = QPushButton(tr("instances.detail.settings.save"))
        self.settings_reset_button = QPushButton(tr("instances.detail.settings.reset"))
        self.settings_reset_button.setObjectName("dangerButton")
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.settings_save_button)
        buttons.addWidget(self.settings_reset_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.java_follow_check.toggled.connect(self._on_java_follow_toggled)
        self.memory_follow_check.toggled.connect(self._on_memory_follow_toggled)
        self.isolation_combo.currentIndexChanged.connect(self._on_isolation_changed)
        self.java_browse_button.clicked.connect(self._browse_java)
        self.settings_save_button.clicked.connect(self._save_settings)
        self.settings_reset_button.clicked.connect(self._reset_settings)
        self._on_java_follow_toggled(True)
        self._on_memory_follow_toggled(True)
        return card

    def _build_share_banner(self) -> tuple[QWidget, QLabel]:
        """Warning strip shown on the mods/resources tabs of a non-isolated instance."""
        box = QWidget()
        label = QLabel("")
        label.setObjectName("hint")
        label.setWordWrap(True)
        button = QPushButton(tr("instances.detail.share.enable"))
        button.setObjectName("secondaryButton")
        button.clicked.connect(self.enable_isolation)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(label, 1)
        layout.addWidget(button)
        box.setVisible(False)
        return box, label

    # ---------- selection ----------

    def set_instance(self, instance_id: str | None) -> None:
        """Show the given instance (None clears the panel)."""
        if instance_id != self.instance_id:
            self._isolation_warned = False
        self.instance_id = instance_id
        if instance_id is None:
            self.instance = None
            self.resolved = None
            self._mods = []
            self._filling_mods = True
            try:
                self.mods_table.setRowCount(0)
            finally:
                self._filling_mods = False
            for subdir in _CONTENT_SUBDIRS:
                self._content_items[subdir] = []
            self._saves = []
            for key in _BADGED_SECTIONS:
                self._set_badge_count(key, 0)
            self._clear_rows()
            return
        self.reload()

    def _set_badge_count(self, key: str, count: int) -> None:
        """Store a sidebar badge count; zero hides the badge (and its width)."""
        self._badge_counts[key] = max(0, int(count))
        badge = self.nav_badges[key]
        badge.setText(str(self._badge_counts[key]))
        badge.setVisible(self._badge_counts[key] > 0)
        label_key = next(
            label_key for section_key, _i, _icon, label_key in _SECTIONS if section_key == key
        )
        name = tr(label_key)
        if self._badge_counts[key]:
            name += f" ({self._badge_counts[key]})"
        self.nav_buttons[key].setAccessibleName(name)

    def _refresh_badge_counts(self) -> None:
        """Cheap directory counts before the async table scans publish their exact values."""
        if self.resolved is None:
            for key in _BADGED_SECTIONS:
                self._set_badge_count(key, 0)
            return
        mods = 0
        try:
            if self.resolved.mods_dir.is_dir():
                mods = sum(
                    1
                    for entry in self.resolved.mods_dir.iterdir()
                    if entry.is_file()
                    and entry.name.lower().endswith((".jar", ".jar.disabled"))
                )
        except OSError:
            mods = 0
        saves = 0
        saves_dir = self.resolved.launch_dir / "saves"
        try:
            if saves_dir.is_dir():
                saves = sum(1 for entry in saves_dir.iterdir() if entry.is_dir())
        except OSError:
            saves = 0
        self._set_badge_count("mods", mods)
        self._set_badge_count("saves", saves)
        self._sync_sidebar()

    def reload(self) -> None:
        """Re-read the instance, resolve it and refresh every part of the panel."""
        if self.instance_id is None:
            return
        cfg, _ = config.load()
        self.game_dir = cfg.game_dir or paths.default_game_dir()
        self.instance = get_instance(self.instance_id, self.game_dir)
        if self.instance is None:
            self.resolved = None
            self._clear_rows()
            return
        try:
            self.resolved = resolve_instance(self.instance_id, cfg, self.game_dir)
        except InstancesError:
            self.resolved = None
            self._clear_rows()
            return
        self._fill_header()
        self._fill_overview()
        self._fill_settings()
        self._fill_banners()
        self._refresh_badge_counts()
        self._loaded_tabs = set()
        self._reload_mods()
        if self.stack.currentIndex() in (*_RESOURCE_TABS, TAB_SAVES, TAB_DIAGNOSIS):
            self._ensure_tab_data(self.stack.currentIndex(), force=True)

    def _clear_rows(self) -> None:
        self.name_label.setText(tr("instances.detail.value.none"))
        self.id_label.setText("")
        self.dir_label.setText("")
        self.icon_button.setIcon(QIcon())
        self.icon_button.setEnabled(False)
        for value in self.overview_values.values():
            value.setText(tr("instances.detail.value.none"))
        self.legacy_label.setVisible(False)
        self.mods_banner.setVisible(False)
        self.res_banner.setVisible(False)
        self.shader_banner.setVisible(False)
        self.log_path_label.setText("")
        self.log_view.clear()
        self.log_view.setVisible(False)
        self.log_empty.setVisible(True)
        for key in _BADGED_SECTIONS:
            self._set_badge_count(key, 0)

    def _fill_header(self) -> None:
        inst = self.instance
        self.name_label.setText(inst.name)
        self.id_label.setText(tr("instances.detail.header.id", inst.id))
        self.dir_label.setText(tr("instances.detail.header.dir", str(self.resolved.launch_dir)))

    def _fill_overview(self) -> None:
        inst = self.instance
        cfg, _ = config.load()
        values = self.overview_values
        values["version"].setText(inst.id)
        values["profile"].setText(display_version_name(inst.id))
        values["game_dir"].setText(str(self.resolved.launch_dir))
        state = tr("common.on") if self.resolved.isolated else tr("common.off")
        values["isolation"].setText(state + " — " + self._isolation_reason())
        values["java"].setText(self._java_text(cfg))
        values["memory"].setText(
            tr("instances.detail.memory.value", f"{self.resolved.memory_gb:g}")
            + " — "
            + (
                tr("instances.detail.memory.source.instance")
                if self.resolved.memory_from_instance
                else tr("instances.detail.settings.follow_global")
            )
        )
        if inst.last_played:
            values["last_played"].setText(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(inst.last_played))
            )
        else:
            values["last_played"].setText(tr("instances.detail.value.never"))
        values["note"].setText(inst.note or tr("instances.detail.value.no_note"))
        self._fill_icon()
        legacy = legacy_instances_dir(self.game_dir)
        if legacy is not None:
            self.legacy_label.setText(tr("instances.detail.legacy.notice", str(legacy)))
            self.legacy_label.setVisible(True)
        else:
            self.legacy_label.setVisible(False)

    def _isolation_reason(self) -> str:
        """Why the instance ended up isolated or not (the documented three-step rule)."""
        inst = self.instance
        if inst.isolated in (ISOLATION_ON, ISOLATION_OFF):
            return tr("instances.detail.isolation.reason.forced")
        folder = instance_dir(self.game_dir, inst.id)
        for sub in ("mods", "saves"):
            directory = folder / sub
            try:
                if directory.is_dir() and any(directory.iterdir()):
                    return tr("instances.detail.isolation.reason.auto")
            except OSError:
                continue
        return tr("instances.detail.isolation.reason.follow")

    def _java_text(self, cfg) -> str:
        pinned = self.instance.settings.java
        if pinned:
            return pinned
        global_java = getattr(cfg, "java_path", None)
        if global_java:
            return tr("instances.detail.java.follow_global", str(global_java))
        return tr("instances.detail.java.follow_auto")

    def _fill_banners(self) -> None:
        isolated = bool(self.resolved and self.resolved.isolated)
        shared = str(self.resolved.game_dir) if self.resolved else ""
        for banner, label in (
            (self.mods_banner, self.mods_banner_label),
            (self.res_banner, self.res_banner_label),
            (self.shader_banner, self.shader_banner_label),
        ):
            if isolated:
                banner.setVisible(False)
                continue
            label.setText(tr("instances.detail.share.banner", shared))
            banner.setVisible(True)

    def _fill_settings(self) -> None:
        inst = self.instance
        settings = inst.settings
        self._filling = True
        try:
            index = self.isolation_combo.findData(inst.isolated)
            self.isolation_combo.setCurrentIndex(max(index, 0))
            self.java_follow_check.setChecked(not settings.java)
            self.java_edit.setText(settings.java or "")
            self.memory_follow_check.setChecked(settings.memory_gb is None)
            memory = settings.memory_gb
            if memory is None and self.resolved is not None:
                memory = self.resolved.memory_gb
            self.memory_spin.setValue(min(max(float(memory or 1.0), 1.0), 64.0))
            self.jvm_args_edit.setText(settings.jvm_args)
            self.game_args_edit.setText(settings.game_args)
            self.server_edit.setText(settings.server)
        finally:
            self._filling = False
        self._on_java_follow_toggled(self.java_follow_check.isChecked())
        self._on_memory_follow_toggled(self.memory_follow_check.isChecked())
        self._sync_server_label_width()

    def _sync_server_label_width(self) -> None:
        """Size the direct-connect label in the font that is actually painted.

        ``_build_settings`` explains why: QFormLayout measured this label before the stylesheet's 13px UI
        font reached the widget, and the fallback font measures the string about 40% narrower, so the
        column it reserved clipped the text. ``ensurePolished`` puts that font in force before measuring
        (it is also what makes this work when the page is filled before it is first shown), and
        invalidating the form once makes the reserved column and the painted text agree. Idempotent: the
        width is only written when it changes, so a reload does not churn the layout.
        """
        self.server_label.ensurePolished()
        width = self.server_label.fontMetrics().horizontalAdvance(self.server_label.text()) + 2
        if self.server_label.minimumWidth() != width:
            self.server_label.setMinimumWidth(width)
            self.settings_form.invalidate()

    def _on_java_follow_toggled(self, follow: bool) -> None:
        self.java_edit.setEnabled(not follow)
        self.java_browse_button.setEnabled(not follow)

    def _on_memory_follow_toggled(self, follow: bool) -> None:
        self.memory_spin.setEnabled(not follow)

    def _ensure_tab_data(self, index: int, force: bool = False) -> None:
        if index not in (*_RESOURCE_TABS, TAB_SAVES, TAB_DIAGNOSIS):
            return
        if not force and index in self._loaded_tabs:
            return
        self._loaded_tabs.add(index)
        if index in _RESOURCE_TABS:
            self.reload_resources()
        elif index == TAB_SAVES:
            self.reload_saves()
        else:
            self.refresh_diagnosis()

    # ---------- overview actions ----------

    def _require_instance(self) -> str | None:
        if self.instance_id is None or self.resolved is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return None
        return self.instance_id

    def _fill_icon(self) -> None:
        """Show the effective icon: the chosen rule / picture, else the automatic rule."""
        if self.instance is None:
            self.icon_button.setIcon(QIcon())
            self.icon_button.setEnabled(False)
            return
        pixmap = block_icons.instance_icon_pixmap(
            self.instance,
            instance_dir(self.game_dir, self.instance.id),
            DETAIL_ICON_SIZE,
            game_dir=self.game_dir,
            cache_root=block_textures.texture_cache_root(paths.launcher_dir()),
        )
        self.icon_button.setIcon(QIcon(pixmap))
        self.icon_button.setEnabled(True)

    def choose_icon(self) -> None:
        """Open the in-window icon picker: eight rules, 恢复自动 and 从文件选择…."""
        if self._require_instance() is None or self.instance is None:
            return
        cache_root = block_textures.texture_cache_root(paths.launcher_dir())
        folder = instance_dir(self.game_dir, self.instance.id)
        dialog = _IconChooserDialog(
            self,
            current=self.instance.icon,
            # The eight tiles are rendered once for the whole card instead of on every repaint.
            previews={
                rule: block_icons.rule_icon_pixmap(
                    rule, ICON_PICKER_ICON, game_dir=self.game_dir, cache_root=cache_root
                )
                for rule in block_icons.RULE_ICON_NAMES
            },
            auto_pixmap=block_icons.automatic_icon_pixmap(
                self.instance.id, ICON_PICKER_ICON, game_dir=self.game_dir, cache_root=cache_root
            ),
            current_pixmap=block_icons.instance_icon_pixmap(
                self.instance,
                folder,
                ICON_PICKER_ICON,
                game_dir=self.game_dir,
                cache_root=cache_root,
            ),
            on_pick=self._apply_icon,
        )
        dialog.ask()
        wants_file = dialog.wants_file
        dialog.deleteLater()
        if wants_file:
            # The native picker opens after the in-window card has closed: two modal surfaces must
            # never be stacked, and the card's job here is only to ask what comes next.
            self._choose_icon_file()

    def _apply_icon(self, value: str) -> bool:
        """Store one icon choice and refresh the page; False when it could not be saved."""
        if self.instance_id is None:
            return False
        try:
            inst = set_instance_icon(self.instance_id, value, self.game_dir)
        except InstancesError as exc:
            set_app_status(self, tr("instances.icon.msg.fail", str(exc)), "error")
            return False
        # Only the icon changed: repaint the one control and let the list rebuild itself. A full
        # reload here would re-scan the mods folder on every tile click, and the list's own refresh
        # (triggered by the signal below) already re-reads the instance.
        self.instance = inst
        self._fill_icon()
        self.changed.emit()  # the card list shows the new icon immediately
        if not value:
            set_app_status(self, tr("instances.icon.msg.auto", inst.name))
        elif value.startswith(block_icons.CUSTOM_ICON_PREFIX):
            set_app_status(self, tr("instances.icon.msg.saved", tr("instances.icon.custom")))
        else:
            set_app_status(
                self, tr("instances.icon.msg.saved", tr("instances.icon.rule." + value))
            )
        return True

    def _choose_icon_file(self) -> None:
        """Pick a picture with the native dialog, then store a copy inside the instance folder."""
        if self.instance_id is None:
            return
        chosen, _selected_filter = QFileDialog.getOpenFileName(
            self,
            tr("instances.icon.file"),
            "",
            tr("instances.icon.file_filter"),
        )
        if not chosen:
            return
        folder = instance_dir(self.game_dir, self.instance_id)
        try:
            value = save_custom_icon(Path(chosen), folder)
        except (ValueError, OSError) as exc:
            inwindow_dialog.warning(
                self, tr("instances.icon.title"), tr("instances.icon.error.invalid", str(exc))
            )
            return
        self._apply_icon(value)

    @staticmethod
    def _open_path(target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def open_instance_folder(self) -> None:
        """Open the instance folder (third-party mods can be dropped in directly)."""
        if self._require_instance() is None:
            return
        target = instance_dir(self.game_dir, self.instance_id)
        self._open_path(target)
        set_app_status(self, tr("instances.msg.folder_opened", str(target)))

    def open_logs_folder(self) -> None:
        if self._require_instance() is None:
            return
        target = self.resolved.launch_dir / "logs"
        self._open_path(target)
        set_app_status(self, tr("instances.detail.msg.logs_opened", str(target)))

    def open_mods_folder(self) -> None:
        if self._require_instance() is None:
            return
        self._open_path(self.resolved.mods_dir)
        set_app_status(self, tr("mods.msg.folder_opened", str(self.resolved.mods_dir)))

    def open_content_folder(self, subdir: str) -> None:
        if self._require_instance() is None:
            return
        target = resolve_content_dir(
            self.resolved.game_dir,
            subdir,
            version_id=self.resolved.id,
            isolated=self.resolved.isolated,
        )
        self._open_path(target)
        set_app_status(self, tr("instances.msg.folder_opened", str(target)))

    def open_saves_folder(self) -> None:
        if self._require_instance() is None:
            return
        target = self.resolved.launch_dir / "saves"
        self._open_path(target)
        set_app_status(self, tr("instances.msg.folder_opened", str(target)))

    def open_crash_viewer(self) -> None:
        """Open the crash report viewer for this instance (or the global game dir)."""
        from gui.crash_viewer import CrashViewerDialog

        base = self.resolved.launch_dir if self.resolved is not None else self.game_dir
        CrashViewerDialog(base, self).exec()

    def rename(self) -> None:
        if self._require_instance() is None:
            return
        old_id = self.instance_id
        new_id, ok = inwindow_dialog.get_text(
            self,
            tr("instances.rename.dialog"),
            tr("instances.rename.prompt"),
            value=old_id,
        )
        new_id = new_id.strip()
        if not ok or not new_id or new_id == old_id:
            return
        try:
            rename_instance(old_id, new_id, self.game_dir)
        except InstancesError as exc:
            set_app_status(self, tr("instances.msg.rename_fail", str(exc)), "error")
            return
        self.instance_id = new_id
        self.reload()
        self.changed.emit()
        set_app_status(self, tr("instances.msg.renamed", old_id, new_id))

    def edit_note(self) -> None:
        if self._require_instance() is None:
            return
        # multiline: a note is free text. The old getMultiLineText() returned "" for an emptied box
        # with ok=True, which clears the note - get_text() keeps that.
        text, ok = inwindow_dialog.get_text(
            self,
            tr("instances.note"),
            tr("instances.note.prompt"),
            value=self.instance.note if self.instance else "",
            multiline=True,
        )
        if not ok:
            return
        try:
            update_instance_note(self.instance_id, text, self.game_dir)
        except InstancesError as exc:
            set_app_status(self, tr("instances.msg.note_fail", str(exc)), "error")
            return
        self.reload()
        self.changed.emit()
        set_app_status(self, tr("instances.msg.note_saved", self.instance_id))

    def export(self) -> None:
        if self._require_instance() is None:
            return
        instance_id = self.instance_id
        dest, _filter = QFileDialog.getSaveFileName(
            self, tr("instances.export"), instance_id + ".zip", "Zip (*.zip)"
        )
        if not dest:
            return
        game_dir = self.game_dir
        run_in_background(
            partial(export_instance, instance_id, Path(dest), game_dir),
            on_result=self._on_exported,
            on_error=self._on_export_failed,
        )

    def _on_exported(self, path) -> None:
        set_app_status(self, tr("instances.msg.exported", str(path)))

    def _on_export_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.msg.export_fail", message), "error")

    def delete(self) -> None:
        if self._require_instance() is None:
            return
        instance_id = self.instance_id
        message = tr("instances.delete.msg", instance_id)
        # A version other profiles inherit from cannot be removed without breaking them:
        # say so before the confirmation, not after the loss.
        from launcher.install import find_version_dependents

        try:
            dependents = find_version_dependents(self.game_dir, instance_id)
        except OSError:
            dependents = []
        if dependents:
            message += "\n\n" + tr("instances.detail.delete.dependents", ", ".join(dependents))
        # No default_yes: delete_instance() cannot be undone, so the safe answer stays the default
        # one, exactly as the old box's "No" default button did.
        if not inwindow_dialog.question(
            self,
            tr("instances.delete.dialog"),
            message,
        ):
            return
        try:
            delete_instance(instance_id, self.game_dir)
        except InstancesError as exc:
            set_app_status(self, tr("instances.msg.delete_fail", str(exc)), "error")
            return
        self.instance_id = None
        self.instance = None
        self.resolved = None
        self.deleted.emit(instance_id)

    # ---------- isolation ----------

    def _on_isolation_changed(self, _index: int) -> None:
        if self._filling or self.instance is None or self._isolation_warned:
            return
        if self.isolation_combo.currentData() == self.instance.isolated:
            return
        if not inwindow_dialog.question(
            self,
            tr("instances.detail.settings.isolation.title"),
            tr("instances.detail.settings.isolation.msg"),
        ):
            self._isolation_warned = True
            return
        self._filling = True
        try:
            index = self.isolation_combo.findData(self.instance.isolated)
            self.isolation_combo.setCurrentIndex(max(index, 0))
        finally:
            self._filling = False

    def enable_isolation(self) -> None:
        """One-click isolation for a shared instance: never moves any file."""
        if self._require_instance() is None:
            return
        instance_id = self.instance_id
        try:
            update_instance(instance_id, game_dir=self.game_dir, isolated=ISOLATION_ON)
        except InstancesError as exc:
            set_app_status(self, tr("instances.detail.share.fail", str(exc)), "error")
            return
        self.reload()
        self.changed.emit()
        set_app_status(
            self,
            tr("instances.detail.share.enabled", str(instance_dir(self.game_dir, instance_id))),
        )

    # ---------- settings ----------

    def _browse_java(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, tr("instances.detail.settings.browse_java"), self.java_edit.text()
        )
        if chosen:
            self.java_edit.setText(chosen)

    def _save_settings(self) -> None:
        if self._require_instance() is None:
            return
        instance_id = self.instance_id
        isolated = self.isolation_combo.currentData()
        try:
            set_instance_settings(
                instance_id,
                game_dir=self.game_dir,
                java="" if self.java_follow_check.isChecked() else self.java_edit.text().strip(),
                memory_gb=(
                    None if self.memory_follow_check.isChecked() else float(self.memory_spin.value())
                ),
                jvm_args=self.jvm_args_edit.text().strip(),
                game_args=self.game_args_edit.text().strip(),
                server=self.server_edit.text().strip(),
            )
            if self.instance is not None and isolated != self.instance.isolated:
                update_instance(instance_id, game_dir=self.game_dir, isolated=isolated)
        except InstancesError as exc:
            set_app_status(self, tr("instances.detail.settings.fail", str(exc)), "error")
            return
        self.reload()
        self.changed.emit()
        set_app_status(self, tr("instances.detail.settings.saved", instance_id))

    def _reset_settings(self) -> None:
        if self._require_instance() is None:
            return
        instance_id = self.instance_id
        if not inwindow_dialog.question(
            self,
            tr("instances.detail.settings.reset.title"),
            tr("instances.detail.settings.reset.msg", instance_id),
        ):
            return
        try:
            reset_instance_settings(instance_id, self.game_dir)
        except InstancesError as exc:
            set_app_status(self, tr("instances.detail.settings.fail", str(exc)), "error")
            return
        self._isolation_warned = False
        self.reload()
        self.changed.emit()
        set_app_status(self, tr("instances.detail.settings.reset.done", instance_id))

    def _selected_values(self, table: QTableWidget) -> list[str]:
        """Names of the selected rows of a content table (column 0 holds the name)."""
        rows = sorted({index.row() for index in table.selectedItems()})
        out = []
        for row in rows:
            item = table.item(row, 0)
            if item is not None and item.text():
                out.append(item.text())
        return out

    # ---------- mods ----------

    def _reload_mods(self) -> None:
        if self.resolved is None:
            return
        set_app_status(self, tr("instances.mods.loading"))
        run_in_background(
            partial(scan_mods, self.resolved.mods_dir),
            on_result=self._fill_mods_table,
            on_error=self._on_mods_failed,
        )

    def _on_mods_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.mods.msg.load_fail", message), "error")

    def _fill_mods_table(self, mods: list) -> None:
        self._mods = mods
        self._set_badge_count("mods", len(mods))
        self._sync_sidebar()
        self._refill_mods()

    def _refill_mods(self) -> None:
        want = self.mods_filter.currentData()
        needle = self.mods_search.text().strip().lower()
        shown = []
        for m in self._mods:
            if want == "enabled" and not m.enabled:
                continue
            if want == "disabled" and m.enabled:
                continue
            if needle and needle not in (m.name + " " + m.mod_id + " " + m.file).lower():
                continue
            shown.append(m)
        self._filling_mods = True
        try:
            self.mods_table.setRowCount(0)
            self.mods_table.setRowCount(len(shown))
            for row, m in enumerate(shown):
                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                check.setCheckState(
                    Qt.CheckState.Checked if m.enabled else Qt.CheckState.Unchecked
                )
                check.setData(Qt.UserRole, m.file)
                self.mods_table.setItem(row, 0, check)
                for col, text in enumerate(
                    (m.name, m.mod_id, m.version, m.loader, m.file), start=1
                ):
                    self.mods_table.setItem(row, col, QTableWidgetItem(text))
        finally:
            self._filling_mods = False
        if not shown and self._mods:
            set_app_status(self, tr("instances.mods.search.none"))
        else:
            set_app_status(self, tr("instances.mods.count", len(self._mods)))

    def _on_mod_toggled(self, item: QTableWidgetItem) -> None:
        if self._filling_mods or item.column() != 0:
            return
        mod = next((m for m in self._mods if m.file == item.data(Qt.UserRole)), None)
        if mod is None:
            return
        want = item.checkState() == Qt.CheckState.Checked
        if mod.enabled == want:
            return
        try:
            set_mod_enabled(mod, want)
        except OSError as exc:
            set_app_status(self, tr("instances.mods.msg.toggle_fail", exc), "error")
            self._filling_mods = True
            try:
                item.setCheckState(
                    Qt.CheckState.Checked if mod.enabled else Qt.CheckState.Unchecked
                )
            finally:
                self._filling_mods = False

    def delete_selected_mods(self) -> None:
        rows = sorted({i.row() for i in self.mods_table.selectedItems()}, reverse=True)
        if not rows:
            set_app_status(self, tr("instances.mods.msg.need_select"), "warning")
            return
        files = []
        for row in rows:
            item = self.mods_table.item(row, 5)
            if item:
                files.append(item.text())
        if not inwindow_dialog.question(
            self,
            tr("instances.mods.delete"),
            tr("instances.mods.delete.confirm", len(files)),
        ):
            return
        deleted = 0
        for name in files:
            mod = next((m for m in self._mods if m.file == name), None)
            if mod is None:
                continue
            try:
                mod.path.unlink()
                deleted += 1
            except OSError:
                continue
        set_app_status(self, tr("instances.mods.msg.deleted", deleted))
        self._reload_mods()

    def choose_mod_files(self) -> None:
        """Install mods picked in a file dialog - the keyboard route to what dropping a jar does.

        WCAG 2.5.1 / 2.5.7: dragging must never be the only way to do something. This hands the
        chosen files to :meth:`install_dropped`, so both routes share one implementation, one set of
        status messages and one failure behaviour.
        """
        if self.resolved is None:
            set_app_status(self, tr("instances.mods.hint"))
            return
        chosen, _filter = QFileDialog.getOpenFileNames(
            self, tr("instances.mods.install"), "", "Jar (*.jar)"
        )
        if chosen:
            self.install_dropped([Path(path) for path in chosen])

    def install_dropped(self, jars: list[Path]) -> None:
        if self.resolved is None:
            set_app_status(self, tr("instances.mods.hint"))
            return
        errors = []
        for src in jars:
            try:
                install_mod_file(src, self.resolved.mods_dir)
            except OSError as exc:
                errors.append(str(exc))
        if errors:
            set_app_status(self, tr("instances.mods.msg.import_fail", "; ".join(errors)), "error")
        else:
            set_app_status(
                self, tr("instances.mods.msg.imported", ", ".join(j.name for j in jars))
            )
        self._reload_mods()

    # ---------- resources ----------

    def reload_resources(self) -> None:
        if self.resolved is None:
            return
        resolved = self.resolved
        set_app_status(self, tr("instances.detail.res.loading"))

        def work() -> dict:
            return {
                sub: list_installed_content(
                    resolved.game_dir,
                    sub,
                    version_id=resolved.id,
                    isolated=resolved.isolated,
                )
                for sub in _CONTENT_SUBDIRS
            }

        run_in_background(work, on_result=self._fill_resources, on_error=self._on_content_failed)

    def _on_content_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.detail.res.msg.load_fail", message), "error")

    def _fill_resources(self, payload: dict) -> None:
        total = 0
        for subdir, items in payload.items():
            self._content_items[subdir] = items
            total += len(items)
            table = self.content_tables[subdir]
            table.setRowCount(0)
            table.setRowCount(len(items))
            for row, item in enumerate(items):
                table.setItem(row, 0, QTableWidgetItem(item.name))
                table.setItem(
                    row, 1, NumericTableItem(human_size(item.size), float(item.size))
                )
            self.content_empty[subdir].update_for(len(items))
        set_app_status(self, tr("instances.detail.res.count", total))

    def delete_selected_content(self, subdir: str) -> None:
        if self._require_instance() is None:
            return
        names = self._selected_values(self.content_tables[subdir])
        if not names:
            set_app_status(self, tr("instances.detail.res.msg.need_select"), "warning")
            return
        if not inwindow_dialog.question(
            self,
            tr("instances.mods.delete"),
            tr("instances.detail.res.delete.confirm", len(names)),
        ):
            return
        resolved = self.resolved

        def work() -> int:
            return sum(_delete_content_file(resolved, subdir, name) for name in names)

        run_in_background(
            work,
            on_result=self._on_content_deleted,
            on_error=self._on_content_delete_failed,
        )

    def _on_content_deleted(self, deleted: int) -> None:
        set_app_status(self, tr("instances.detail.res.msg.deleted", deleted))
        self.reload_resources()

    def _on_content_delete_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.detail.res.msg.delete_fail", message), "error")

    # ---------- saves ----------

    def reload_saves(self) -> None:
        if self.resolved is None:
            return
        target = self.resolved.launch_dir / "saves"
        set_app_status(self, tr("instances.detail.saves.loading"))

        def work() -> list:
            found = []
            if target.is_dir():
                for entry in sorted(target.iterdir()):
                    try:
                        if not entry.is_dir():
                            continue
                        found.append(
                            (entry.name, entry.stat().st_mtime, directory_size(entry))
                        )
                    except OSError:
                        continue
            return found

        run_in_background(work, on_result=self._fill_saves, on_error=self._on_saves_failed)

    def _on_saves_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.detail.saves.msg.load_fail", message), "error")

    def _fill_saves(self, saves: list) -> None:
        self._saves = saves
        self._set_badge_count("saves", len(saves))
        self._sync_sidebar()
        table = self.saves_table
        table.setRowCount(0)
        table.setRowCount(len(saves))
        for row, (name, modified, size) in enumerate(saves):
            table.setItem(row, 0, QTableWidgetItem(name))
            table.setItem(
                row, 1, QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)))
            )
            table.setItem(row, 2, NumericTableItem(human_size(size), float(size)))
        self.saves_empty.update_for(len(saves))
        set_app_status(self, tr("instances.detail.saves.count", len(saves)))

    # ---------- launch ----------

    def _on_launch_clicked(self, _checked: bool = False) -> None:
        """A real button click: classify it, then take the same path as every other entry."""
        self.launch(trigger_source_for_click(self.launch_button))

    def launch(self, trigger_source: TriggerSource | str = TriggerSource.UNKNOWN) -> None:
        if self._require_instance() is None:
            return
        instance_id = self.instance_id
        cfg, _ = config.load()
        if not cfg.selected_account:
            from launcher.config import offline_mode_allowed

            if not offline_mode_allowed():
                set_app_status(self, tr("launch.msg.offline_locked"), "warning")
                return
        resolved = self.resolved
        if resolved is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return
        disable_keeping_focus(self.launch_button)
        set_app_status(self, tr("instances.msg.preparing", instance_id))

        prepare_launch_accessibility(
            parent=self.window(),
            instance_id=instance_id,
            game_dir=resolved.game_dir,
            launch_dir=resolved.launch_dir,
            trigger_source=trigger_source,
            launch_button=self.launch_button,
            on_continue=lambda: self._launch_now(instance_id, cfg, resolved),
        )

    def _launch_now(self, instance_id: str, cfg, resolved) -> None:
        """The actual launch worker, reached only through the single pre-launch accessibility gate."""

        def do_prepare() -> object:
            try:
                account = resolve_launch_account(AccountStore(), cfg.selected_account, None)
                prepared = prepare_launch(
                    instance_id,
                    game_dir=resolved.game_dir,
                    cache_dir=paths.launcher_dir() / "cache",
                    account=account,
                    memory_gb=resolved.memory_gb,
                    language=cfg.game_language or None,
                    launch_dir=resolved.launch_dir,
                    java_path=resolved.java_path,
                    jvm_args=resolved.jvm_args or None,
                    game_args=resolved.game_args,
                )
                return ("ok", prepared, resolved)
            except Exception as exc:  # noqa: BLE001 - reported as a launch failure
                return ("error", str(exc))

        run_in_background(
            do_prepare,
            on_result=self._on_prepared,
            on_finished=self._on_launch_finished,
        )

    def _on_launch_finished(self) -> None:
        self.launch_button.setEnabled(True)

    def _on_prepared(self, result) -> None:
        kind = result[0]
        if kind == "error":
            text = tr("instances.msg.launch_fail", str(result[1]))
            set_app_status(self, text, "error")
            show_fatal(self, text)  # fatal error dialog
            return
        _kind, prepared, resolved = result
        self._last_launch = (prepared, resolved)  # the exit callback needs it for the diagnosis
        command = prepared.command
        set_app_status(self, tr("instances.msg.running", prepared.version.id, str(command.cwd)))

        # After-launch behavior (keep / hide / exit) via a signal bridge.
        cfg2, _ = config.load()
        start_bridge = ProgressBridge()
        if cfg2.after_launch_behavior != "keep":
            start_bridge.progress.connect(self._on_game_started)

        def do_run() -> object:
            started = time.time()
            run = run_process(
                command.argv,
                command.cwd,
                on_started=start_bridge if cfg2.after_launch_behavior != "keep" else None,
                capture_tail=True,  # keeps the last lines when the game dies before logging
            )
            crashes = find_new_crash_reports(command.cwd, started)
            return run, crashes, started

        run_in_background(
            do_run,
            on_result=self._on_exit,
            on_error=self._on_run_failed,
        )

    def _on_run_failed(self, message: str) -> None:
        text = tr("instances.msg.run_error", message)
        set_app_status(self, text, "error")
        show_fatal(self, text)

    def _on_exit(self, result) -> None:
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
        dialog = diagnose_after_exit(
            self.window(),
            context,
            log_path=resolved.launch_dir / "logs" / "latest.log",
        )
        if dialog is not None:
            # Remember it inside the instance so the diagnostics tab can show it later
            from gui.dialogs.crash_dialog import record_diagnosis

            record_diagnosis(instance_dir(resolved.game_dir, resolved.id), dialog.diagnosis)

    def _on_game_started(self, _value=None) -> None:
        from PySide6.QtCore import QTimer
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


class InstanceDetailPage(QWidget):
    """Full-page host for :class:`InstanceDetail`, with the exit back to the instance grid.

    The instances page owns both this page and the card grid in one internal QStackedWidget. Keeping the
    back control here means the detail page can be reached from anywhere (the card grid, the launch page
    or the versions page) without making the main sidebar grow a ninth entry.
    """

    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("instanceDetailPage")
        layout = QVBoxLayout(self)
        style_page_layout(layout)
        layout.setSpacing(8)

        self.back_button = QPushButton("← " + tr("instances.detail.back"))
        self.back_button.setObjectName("backButton")
        self.back_button.setAccessibleName(tr("instances.detail.back"))
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(lambda: self.back_requested.emit())

        self.detail = InstanceDetail()
        self.no_selection = QLabel(tr("instances.select.hint"))
        self.no_selection.setObjectName("emptyState")
        self.no_selection.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_selection.setWordWrap(True)
        self.detail_stack = QStackedWidget()
        self.detail_stack.addWidget(self.detail)
        self.detail_stack.addWidget(self.no_selection)

        layout.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.detail_stack, 1)

    def set_instance(self, instance_id: str | None) -> None:
        """Load one instance, or show the empty hint when there is none."""
        if instance_id is None:
            self.detail_stack.setCurrentWidget(self.no_selection)
            self.detail.set_instance(None)
            return
        self.detail_stack.setCurrentWidget(self.detail)
        self.detail.set_instance(instance_id)

    def focus_back_button(self) -> None:
        """Keyboard users land on the way back, not on a card that is now hidden."""
        self.back_button.setFocus(Qt.FocusReason.OtherFocusReason)
