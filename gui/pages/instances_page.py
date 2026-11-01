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

"""Instances page: a two-level browser - a game-folder rail, then grouped cards.

An instance *is* a version folder (`versions/<id>/`), but the launcher can point at several game
folders, so the page has two levels of its own:

* the 200px rail on the left (``gui.pages.instance_folders.FolderRail``) lists the known folders with
  their instance counts and carries the 「添加文件夹…」 entry - it is this page's column, never a
  second main rail;
* the pane on the right holds the search box, the sort control and the cards, grouped as 正式版 /
  快照版 / 带加载器 (empty groups do not render). A search flattens that grouping into one result
  grid with an "N 个结果" line, the same language the resources page uses.

Everything that acts on one instance lives in @gui.pages.instance_detail.InstanceDetail; the cards
never embed it, and the detail page is shown only after a deliberate drill-down.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import block_icons, i18n, theme
from gui.pages.instance_detail import InstanceDetailPage
from gui.pages.instance_folders import (
    FOLDER_RAIL_GAP,
    FolderRail,
    add_game_folder_entry,
    folder_instance_count,
    ordered_folders,
)
from gui.widgets import (
    ElidedLabel,
    EmptyState,
    add_page_top_inset,
    add_search_icon,
    attach_hover_scrollbar,
    disable_keeping_focus,
    set_app_status,
    style_page_layout,
)
from gui.workers import run_in_background
from launcher import config, paths
from launcher.accessibility import TriggerSource
from launcher.install import list_installed_versions
from launcher.instances import (
    InstancesError,
    create_instance,
    default_instance_name,
    display_version_name,
    import_instance,
    instance_dir,
    list_instances,
    resolve_instance,
)
from launcher.meta import block_textures

tr = i18n.tr

CARD_HEIGHT = 156
CARD_GAP = 12
CARD_MIN_WIDTH = 260
CARD_MAX_COLUMNS = 3
# The two-level layout spends 212px on the folder rail, so the old "three 260px cards" floor (860)
# no longer fits the 1180px window budget in tools/measure_min_width.py. The floor is now two cards at
# the width the grid actually draws them (320px), and a third column is added once the pane can hold
# three of those. tests/test_window_clipping.py keeps the window above the old 1000px pin from this.
CARD_FLOOR_WIDTH = 320
CARD_GRID_MIN_WIDTH = 2 * CARD_FLOOR_WIDTH + 3 * CARD_GAP
CARD_COLUMNS_MIN_WIDTH = CARD_MAX_COLUMNS * CARD_FLOOR_WIDTH + 4 * CARD_GAP

# The three card groups, in the order they are shown. A group with no card is not rendered at all.
GROUP_RELEASE = "release"
GROUP_SNAPSHOT = "snapshot"
GROUP_LOADER = "loader"
GROUP_ORDER = (GROUP_RELEASE, GROUP_SNAPSHOT, GROUP_LOADER)
GROUP_TITLE_KEYS = {
    GROUP_RELEASE: "instances.group.release",
    GROUP_SNAPSHOT: "instances.group.snapshot",
    GROUP_LOADER: "instances.group.loader",
}
#: Version categories that read as a snapshot. April Fools versions are snapshots with a joke id.
SNAPSHOT_CATEGORIES = frozenset({"snapshot", "april_fools"})


def instance_group(version: str, loader: str, game_dir: Path) -> str:
    """Which of the three groups a card belongs to.

    A profile with a loader (Fabric / Quilt / Forge / NeoForge) is 带加载器 whatever it inherits: the
    point of the group is "this instance runs mods", not which Minecraft build sits underneath it.
    Everything else is 快照版 for a snapshot (April Fools included) and 正式版 for everything else -
    the alpha/beta types fall there too, so the three groups cover every instance without a fourth
    "other" bucket that would only ever hold one card.
    """
    if loader != tr("instances.card.vanilla"):
        return GROUP_LOADER
    version_type = block_textures.version_type_for(
        version, game_dir, cache_dir=paths.launcher_dir() / "cache"
    )
    category = block_textures.icon_category(version, version_type)
    return GROUP_SNAPSHOT if category in SNAPSHOT_CATEGORIES else GROUP_RELEASE


def card_columns(width: int) -> int:
    """How many cards fit across ``width``: three only at the three-card floor, else two or one."""
    if width >= CARD_COLUMNS_MIN_WIDTH:
        return CARD_MAX_COLUMNS
    if width >= CARD_GRID_MIN_WIDTH:
        return 2
    return 1


def _profile_json(game_dir: Path, version_id: str) -> dict:
    path = instance_dir(game_dir, version_id) / (version_id + ".json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _known_profile_identity(version_id: str) -> tuple[str, str] | None:
    """(minecraft version, loader) for the profile-id shapes the launcher installs."""
    if version_id.startswith("fabric-loader-"):
        parts = version_id.split("-")
        if len(parts) >= 4:
            return parts[3], "Fabric " + parts[2]
    if version_id.startswith("quilt-loader-"):
        parts = version_id.split("-")
        if len(parts) >= 4:
            return parts[3], "Quilt " + parts[2]
    if "-forge-" in version_id:
        minecraft, loader_version = version_id.split("-forge-", 1)
        return minecraft, "Forge " + loader_version
    if version_id.startswith("neoforge-"):
        loader_version = version_id[len("neoforge-") :]
        numbers = loader_version.split(".")
        # NeoForge encodes the Minecraft version in its first two components (21.1.5 -> 1.21.1).
        minecraft = (
            f"1.{numbers[0]}.{numbers[1]}"
            if len(numbers) >= 2 and numbers[0].isdigit() and numbers[1].isdigit()
            else loader_version
        )
        return minecraft, "NeoForge " + loader_version
    return None


def card_version_loader(instance, game_dir: Path) -> tuple[str, str]:
    """Resolve the ``版本 · 加载器`` line, following an inherited profile when needed."""
    seen: set[str] = set()
    current = instance.id
    while current and current not in seen:
        seen.add(current)
        known = _known_profile_identity(current)
        if known is not None:
            return known
        parent = _profile_json(game_dir, current).get("inheritsFrom")
        if not isinstance(parent, str) or not parent.strip():
            return current, tr("instances.card.vanilla")
        current = parent.strip()
    return instance.id, tr("instances.card.vanilla")


def instance_counts(instance, cfg, game_dir: Path) -> tuple[int, int]:
    """(mods, saves) for the card footer; unreadable directories count as zero."""
    try:
        resolved = resolve_instance(instance.id, cfg, game_dir)
    except (InstancesError, OSError):
        return 0, 0
    mods = 0
    try:
        if resolved.mods_dir.is_dir():
            mods = sum(
                1
                for entry in resolved.mods_dir.iterdir()
                if entry.is_file()
                and entry.name.lower().endswith((".jar", ".jar.disabled"))
            )
    except OSError:
        mods = 0
    saves = 0
    saves_dir = resolved.launch_dir / "saves"
    try:
        if saves_dir.is_dir():
            saves = sum(1 for entry in saves_dir.iterdir() if entry.is_dir())
    except OSError:
        saves = 0
    return mods, saves


class _InstanceCard(QFrame):
    """One white card: 使用 is its primary action, 管理 opens the detail page.

    The card used to be one big button whose only affordance was 管理, so there was no way to say
    "run this one". Now the two actions are real buttons, the card body answers a double-click with
    使用, and the instance the launcher is set to run carries the same 3px accent bar the detail
    sidebar uses for its selected row (one selection language across the app).
    """

    use_requested = Signal(str)
    manage_requested = Signal(str)

    def __init__(
        self,
        instance,
        meta: str,
        counts: str,
        icon_pixmap: QPixmap,
        *,
        current: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.instance_id = instance.id
        self.setObjectName("instanceCard")
        # A QFrame only paints a QSS background when it is told to; the card's white fill depends on it.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # The card is a surface, not a control: the two real buttons are the keyboard stops.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # The "current" marker is the detail sidebar's 3px accent bar plus its pale accent wash,
        # painted by the stylesheet through this property; keeping it a property means the card can
        # be re-marked without rebuilding the widget.
        self.setProperty("current", bool(current))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        description = meta + (" · " + counts if counts else "")
        self.setAccessibleName(tr("instances.card.open", instance.name))
        accessible = description
        if current:
            accessible += " · " + tr("instances.card.current")
        self.setAccessibleDescription(accessible)
        self.setToolTip(f"{instance.name}\n{description}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        icon = QLabel()
        icon.setObjectName("instanceCardIcon")
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(icon_pixmap)
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # The card's text is painted by child labels, not controls. ElidedLabel enables text
        # selection, which also gives it ClickFocus - so each name/meta line entered the focus chain
        # as a focus stop with no :focus rule (the keyboard walkthrough flagged it). Mouse events
        # already pass through to the card, so selecting this text was unreachable anyway. Turn focus
        # off on exactly these two card labels; keep the class default for the detail page's
        # path/ID labels, which stay selectable.
        name = ElidedLabel(instance.name)
        name.setObjectName("instanceCardName")
        name.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Named attributes: the two text lines are what the card says, and the tests (plus any later
        # card variant) read them instead of walking the layout.
        self.name_label = name
        top.addWidget(icon)
        top.addWidget(name, 1)
        layout.addLayout(top)

        meta_label = ElidedLabel(meta)
        meta_label.setObjectName("instanceCardMeta")
        meta_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.meta_label = meta_label
        layout.addWidget(meta_label)
        layout.addStretch(1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        counts_label = QLabel(counts, self)
        counts_label.setObjectName("instanceCardCounts")
        counts_label.setWordWrap(False)
        # Parented above: a parentless label that is told to be visible is a top-level window for an
        # instant, and showing it takes the window activation away from the page the user is on.
        counts_label.setVisible(bool(counts))
        self.counts_label = counts_label
        counts_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # The primary action. It keeps the button base rule (accent fill, white text), so it reads as
        # "the" action on the card without a second colour vocabulary.
        self.use_button = QPushButton(tr("instances.card.use"))
        self.use_button.setObjectName("instanceCardUse")
        self.use_button.setAccessibleName(
            tr("instances.card.use") + " " + instance.name
        )
        self.use_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.use_button.setFixedHeight(28)
        self.use_button.clicked.connect(lambda: self.use_requested.emit(self.instance_id))
        # The secondary sibling: the same quiet outline the card used to draw for 管理.
        self.manage_button = QPushButton(tr("instances.card.manage"))
        self.manage_button.setObjectName("instanceCardManage")
        self.manage_button.setAccessibleName(tr("instances.card.open", instance.name))
        self.manage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage_button.setFixedHeight(28)
        self.manage_button.clicked.connect(lambda: self.manage_requested.emit(self.instance_id))
        bottom.addWidget(counts_label)
        bottom.addStretch(1)
        bottom.addWidget(self.use_button)
        bottom.addWidget(self.manage_button)
        layout.addLayout(bottom)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-clicking the card body uses the instance (the buttons keep their own clicks)."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.use_requested.emit(self.instance_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _GroupSection(QWidget):
    """One group: a 15/600 heading plus the cards that belong to it, laid out in a responsive grid.

    The heading is the same ``sectionTitle`` label the rest of the app uses for a 15px/600 block
    title, so the group headers do not invent a second type scale. A flat (search) section passes an
    empty title and the heading stays hidden.
    """

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("instanceGroupSection")
        self.cards: list[_InstanceCard] = []
        self._columns = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        # Parented at creation: a parentless label shown before it is added to the layout is a
        # top-level window for an instant and steals the window activation.
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("sectionTitle")
        self.title_label.setVisible(bool(title))
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(CARD_GAP)
        self.grid.setVerticalSpacing(CARD_GAP)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.title_label)
        layout.addLayout(self.grid)

    def add_card(self, card: _InstanceCard) -> None:
        self.cards.append(card)

    def reflow(self, width: int) -> None:
        """Give every card the cell's exact width and place it in the current column count."""
        columns = card_columns(width)
        card_width = max(CARD_MIN_WIDTH, (width - (columns - 1) * CARD_GAP) // columns)
        for card in self.cards:
            if card.width() != card_width or card.height() != CARD_HEIGHT:
                card.setFixedSize(card_width, CARD_HEIGHT)
        if columns == self._columns and self.grid.count() == len(self.cards):
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
        self._columns = columns


class _CardGroupView(QWidget):
    """The scrollable content: group sections stacked top to bottom, each reflowing with the width."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("instanceGroups")
        self._sections: list[_GroupSection] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        # 16px between two groups: one step above the 8px heading-to-cards gap, so a group reads as
        # one block (title + its cards) rather than as a wall of equally spaced items.
        self._layout.setSpacing(16)
        self._layout.addStretch(1)

    def clear(self) -> None:
        for section in self._sections:
            self._layout.removeWidget(section)
            # Reparent before the deferred delete: a widget only removed from the layout keeps its old
            # geometry (and stays painted) until the event loop runs DeferredDelete.
            section.setParent(None)
            section.deleteLater()
        self._sections = []

    def sections(self) -> list[_GroupSection]:
        return list(self._sections)

    def add_section(self, section: _GroupSection) -> _GroupSection:
        self._sections.append(section)
        self._layout.insertWidget(self._layout.count() - 1, section)
        section.reflow(self.width())
        return section

    def cards(self) -> list[_InstanceCard]:
        return [card for section in self._sections for card in section.cards]

    def reflow(self, width: int | None = None) -> None:
        target = self.width() if width is None else width
        for section in self._sections:
            section.reflow(target)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.reflow()


class _FolderEmptyState(QWidget):
    """Shown when the current folder holds no instance at all: the hint plus the way out."""

    download_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("instanceFolderEmpty")
        self.label = EmptyState(tr("instances.folder.empty"), self)
        self.label.update_for(0)  # EmptyState starts hidden; this panel is the only thing on screen
        self.download_button = QPushButton(tr("instances.folder.download"))
        self.download_button.setObjectName("secondaryButton")
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.clicked.connect(self.download_requested)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addStretch(1)
        # The hint owns the full pane width: an aligned widget would only get its size hint, and a
        # wrapping QLabel measured before its width is known clips the sentence ("这个文件夹里还没有").
        layout.addWidget(self.label)
        layout.addWidget(self.download_button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)


class _CreateDialog(QDialog):
    """Create another instance by copying an installed profile (instances are version folders)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("instances.dialog.title"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("instances.name.placeholder"))
        self.version_combo = QComboBox()
        self.version_combo.setEditable(True)
        self.version_combo.addItem(tr("instances.loading"))
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("instances.create.ok"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            tr("instances.create.cancel")
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.version_combo.currentIndexChanged.connect(self._auto_name)

        form = QFormLayout(self)
        form.addRow(tr("instances.name"), self.name_edit)
        form.addRow(tr("instances.version"), self.version_combo)
        form.addRow(self.buttons)

        self._fill_versions()

    def _fill_versions(self) -> None:
        """List installed profiles only - a copy needs the profile files to duplicate."""
        self.version_combo.clear()
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()
        try:
            installed = sorted(list_installed_versions(game_dir))
        except OSError:
            installed = []
        if not installed:
            self.version_combo.addItem(tr("instances.none_installed"))
            return
        for version_id in installed:
            self.version_combo.addItem(display_version_name(version_id), version_id)

    def _auto_name(self) -> None:
        version_id = (self.version_combo.currentData() or self.version_combo.currentText()).strip()
        if not version_id or version_id.startswith(tr("instances.loading").split("（")[0]):
            return
        if not self.name_edit.text().strip():
            self.name_edit.setText(default_instance_name(version_id))

    def values(self) -> tuple[str, str]:
        version_id = self.version_combo.currentData() or self.version_combo.currentText().strip()
        return self.name_edit.text().strip(), version_id


class InstancesPage(QWidget):
    """Two-level instance browser, plus the drill-down detail page it reveals on request."""

    #: A card (or the 管理 affordance drawn on it) asks the window to open the detail page.
    instance_open_requested = Signal(str)
    #: A card's 使用 button (or a double-click on the card) asks the window to make it current.
    instance_use_requested = Signal(str)
    #: The detail page's back control asks to return to the grid.
    back_requested = Signal()
    #: The detail view is on screen. The window uses this to hide the main navigation rail, because
    #: the detail page has an eight-entry sidebar of its own and two sidebars side by side do not fit.
    detail_shown = Signal()
    #: The card grid is on screen again: the main navigation rail has to come back.
    list_shown = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("instancesPage")
        #: The instance whose card was picked (the detail pane follows it). Kept by id, not by widget,
        #: because every refresh rebuilds the cards.
        self._selected_id: str | None = None
        #: The folder the card pane is showing, and the instances found in it by the last refresh.
        self._game_dir: Path | None = None
        self._instances: dict = {}
        #: Set while a reveal still has to be scrolled into view once the layout has run.
        self._pending_reveal: str | None = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal_pending_card)

        self.search_edit = QLineEdit()
        add_search_icon(self.search_edit)
        self.search_edit.setPlaceholderText(tr("instances.search.placeholder"))
        self.search_edit.setAccessibleName(tr("instances.search.placeholder"))  # WCAG 4.1.2
        self.search_edit.setClearButtonEnabled(True)

        # The left column: folders, their counts and the shared 「添加文件夹…」 entry.
        self.folder_rail = FolderRail()
        self.folder_list = self.folder_rail.list

        # The right pane: one scroll area of group sections. Searching collapses it to a single flat
        # section, which is why the same container serves both states.
        self.groups_view = _CardGroupView()
        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("instanceGroupScroll")
        self.content_scroll.setAccessibleName(tr("nav.instances"))
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_scroll.setWidget(self.groups_view)
        # The card buttons are the keyboard stops; a second stop around the whole grid would announce
        # the same content twice (the old IconMode grid had the same rule).
        self.content_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The pane's floor: two cards at the width the grid draws them. Declared here, not on show, so
        # the page's own minimum width is part of the window's floor from the moment it is built
        # (tools/measure_min_width.py reads the window's requirement, and a floor that only appears
        # while the page is visible would let the window shrink under it again).
        self.content_scroll.setMinimumWidth(CARD_GRID_MIN_WIDTH)
        attach_hover_scrollbar(self.content_scroll)

        # "N 个结果", the resources page's search-summary wording. Hidden unless a search is running.
        self.result_count = QLabel("")
        self.result_count.setObjectName("instanceResultCount")
        self.result_count.setVisible(False)

        self.empty_folder = _FolderEmptyState()
        self.empty_search = EmptyState(tr("instances.search.none"))
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.content_scroll)
        self.list_stack.addWidget(self.empty_folder)
        self.list_stack.addWidget(self.empty_search)

        self.sort_combo = QComboBox()
        self.sort_combo.addItem(tr("instances.sort.name"), "name")
        self.sort_combo.addItem(tr("instances.sort.time"), "time")
        # WCAG 4.1.2: the visible label sits in the same row layout, not in a QFormLayout row, so it
        # never became this combo's name and the control was announced as an unlabelled list.
        self.sort_combo.setAccessibleName(tr("instances.sort.label"))

        self.create_button = QPushButton(tr("instances.create_copy"))
        self.import_button = QPushButton(tr("instances.import"))
        self.import_button.setObjectName("secondaryButton")

        self.detail_page = InstanceDetailPage()
        self.detail = self.detail_page.detail
        self.detail_stack = self.detail_page.detail_stack
        self.no_selection = self.detail_page.no_selection

        # Legacy attribute names kept for callers (and tests) that predate the detail page
        self.open_folder_button = self.detail.open_folder_button
        self.launch_button = self.detail.launch_button
        self.crash_button = self.detail.crash_button
        self.mods_table = self.detail.mods_table

        browser = QWidget()
        browser.setObjectName("instanceBrowser")
        browser_layout = QVBoxLayout(browser)
        style_page_layout(browser_layout)
        browser_layout.setSpacing(12)
        add_page_top_inset(browser_layout)

        # Two levels, side by side: the folder rail spans the full height of the pane, so its list and
        # its 「添加文件夹…」 entry line up with the search row at the top and the last card row below.
        main_row = QHBoxLayout()
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(FOLDER_RAIL_GAP)
        pane = QVBoxLayout()
        pane.setContentsMargins(0, 0, 0, 0)
        pane.setSpacing(8)

        first_row = QHBoxLayout()
        first_row.setContentsMargins(0, 0, 0, 0)
        first_row.setSpacing(8)
        sort_label = QLabel(tr("instances.sort.label"))
        sort_label.setBuddy(self.sort_combo)
        first_row.addWidget(self.search_edit, 1)
        first_row.addWidget(sort_label)
        first_row.addWidget(self.sort_combo)
        pane.addLayout(first_row)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addStretch(1)
        actions.addWidget(self.create_button)
        actions.addWidget(self.import_button)
        pane.addLayout(actions)
        pane.addWidget(self.result_count)
        pane.addWidget(self.list_stack, 1)
        main_row.addWidget(self.folder_rail)
        main_row.addLayout(pane, 1)
        browser_layout.addLayout(main_row)

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(browser)
        self.view_stack.addWidget(self.detail_page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view_stack)

        self.create_button.clicked.connect(self._create)
        self.import_button.clicked.connect(self._import)
        self.sort_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        # Debounce: refilter only after 200ms of typing inactivity
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._on_search_changed)
        self.search_edit.textChanged.connect(lambda _t: self._search_timer.start(200))
        # Arrow keys move the rail's current row and the pane follows; a click or Enter on a row is a
        # commit, so focus then lands on the first card (the rows it may have been standing on are
        # about to be rebuilt).
        self.folder_rail.folder_changed.connect(self._on_folder_row_changed)
        self.folder_rail.folder_activated.connect(self._on_folder_activated)
        self.folder_rail.add_requested.connect(self._on_add_folder)
        self.empty_folder.download_requested.connect(self._go_to_versions)
        self.detail.changed.connect(self._on_detail_changed)
        self.detail.deleted.connect(self._on_detail_deleted)
        self.detail_page.back_requested.connect(self._on_back_clicked)
        self.refresh()

    # ---------- page navigation ----------

    def open_instance(self, instance_id: str) -> bool:
        """Show one instance's detail page (the window calls this for every drill-down route)."""
        found = self.select_instance(instance_id)
        if not found:
            return False
        self.detail_page.detail_stack.setCurrentWidget(self.detail)
        self.view_stack.setCurrentWidget(self.detail_page)
        self.detail_page.focus_back_button()
        # The main rail steps aside only after the detail page is up and focus has moved there: an
        # emitted signal first would hide the rail while its own item still held the keyboard focus.
        self.detail_shown.emit()
        return True

    def _ensure_card_visible(self, card: _InstanceCard) -> None:
        """Scroll ``card`` into view, and again once the layout has a size.

        Two reasons the second pass exists. A card created during this refresh has no position until
        Qt lays the new sections out (``ensureWidgetVisible`` reads the child's current geometry), and
        a reveal that runs in the same call as the page switch sees a scroll area whose viewport is
        still 0px tall. The immediate attempt covers the already-laid-out case; the queued one covers
        the page that has just been shown, one event-loop turn later.
        """
        self._pending_reveal = card.instance_id
        self._scroll_card_into_view(card)
        self._reveal_timer.start(0)

    def _scroll_card_into_view(self, card: _InstanceCard | None) -> None:
        if card is None:
            return
        layout = self.groups_view.layout()
        if layout is not None:
            layout.activate()
        # A page switch can resize the content widget down to the viewport (the scroll area applies a
        # size hint that is still stale) - the range becomes 0 and ensureWidgetVisible has nowhere to
        # scroll. Give the content its real height first; the scroll area keeps it.
        hint = self.groups_view.sizeHint()
        if hint.height() > self.groups_view.height():
            self.groups_view.resize(self.groups_view.width(), hint.height())
        self.content_scroll.ensureWidgetVisible(card)

    def _reveal_pending_card(self) -> None:
        """Finish a reveal after the layout has run: scroll to the card the pane was asked for."""
        instance_id, self._pending_reveal = self._pending_reveal, None
        self._scroll_card_into_view(self.card_for(instance_id))

    def show_list(self) -> None:
        """Return to the card grid, putting keyboard focus back on the selected card's 使用 button.

        Focus lands on a control that stays visible: the selected card's primary action when there is
        one, the first card otherwise, and the search field when the folder is empty. It used to stay
        on the back button whenever the grid was empty, which left the window with focus on a widget
        inside the now-hidden detail page.
        """
        self.view_stack.setCurrentWidget(self.view_stack.widget(0))
        if self.isVisible():
            card = self.card_for(self._selected_id)
            if card is None:
                cards = self.groups_view.cards()
                card = cards[0] if cards else None
            target = card.use_button if card is not None else self.search_edit
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            if card is not None:
                self._ensure_card_visible(card)
        self.list_shown.emit()

    def reveal_instance(self, instance_id: str) -> bool:
        """Show the grid scrolled to one instance, make it the selected card, then focus it.

        This is what arriving from the launch page's 实例选择 does: the stored instance is selected
        and scrolled into the viewport instead of the page opening at the top of a long grid. It also
        switches the game folder when the instance lives in another one - the grid only ever shows
        the current folder, so otherwise the row would simply not exist.
        """
        wanted = str(instance_id or "").strip()
        found = self.select_instance(wanted) if wanted else False
        card = self.card_for(wanted)
        if card is not None:
            self._ensure_card_visible(card)
        self.show_list()
        return found and card is not None

    def reveal_current_instance(self) -> bool:
        """Scroll to the instance the launcher is set to start on (no-op when none is stored)."""
        cfg, _ = config.load()
        return self.reveal_instance(str(cfg.selected_instance or "").strip())

    def current_view(self) -> QWidget:
        return self.view_stack.currentWidget()

    def _on_back_clicked(self) -> None:
        self.show_list()
        self.back_requested.emit()

    def _open_requested(self, instance_id: str) -> None:
        self.instance_open_requested.emit(instance_id)

    # ---------- the folder rail ----------

    def _on_folder_row_changed(self, path: str) -> None:
        """The rail's current row moved (arrow keys included): show that folder's instances.

        Keyboard focus stays in the rail here on purpose: with the arrow keys a user walks the folder
        list, and pulling focus into the pane after the first press would end that walk. The commit
        paths (click / Enter) move focus instead - see :meth:`_on_folder_activated`.
        """
        if not path or self._is_active_folder(path):
            return
        self.switch_folder(path, move_focus=False)

    def _on_folder_activated(self, path: str) -> None:
        """A row was committed (click or Enter): switch, then hand focus to the first card."""
        if not path:
            return
        if self._is_active_folder(path):
            self.focus_first_card()
            return
        self.switch_folder(path, move_focus=True)

    def _is_active_folder(self, path) -> bool:
        cfg, _ = config.load()
        active = cfg.active_folder()
        return active is not None and config.same_game_folder(active.path, path)

    def active_folder_path(self) -> str:
        cfg, _ = config.load()
        folder = cfg.active_folder()
        return str(folder.path) if folder is not None else ""

    def switch_folder(self, path, *, move_focus: bool = True) -> bool:
        """Make ``path`` the current game folder and show its instances.

        The search text is deliberately *not* cleared: typing a filter and then walking the folders
        is how a user finds "the 1.20.1 instance in the other folder", so the new folder is filtered
        with the same needle. The selection is dropped (it belonged to the old folder) and every page
        that resolves the game directory is told to re-read it.
        """
        cfg, cfg_path = config.load()
        if not cfg.set_active_game_folder(path):
            set_app_status(self, tr("settings.folder.missing"), "warning")
            self.refresh()
            return False
        config.save(cfg, cfg_path)
        self._selected_id = None
        self.refresh()
        self._notify_folder_changed()
        if move_focus:
            self.focus_first_card()
        return True

    def focus_first_card(self) -> None:
        """Put keyboard focus on the first card of the pane (or the closest visible control).

        Called after a folder switch: the cards the focus may have been standing on are rebuilt by
        the switch, so the focus has to be placed again deliberately or it would be lost with them.
        """
        cards = self.groups_view.cards()
        if cards:
            self._ensure_card_visible(cards[0])
            cards[0].use_button.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        if self.list_stack.currentWidget() is self.empty_folder:
            self.empty_folder.download_button.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.search_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _notify_folder_changed(self) -> None:
        """Tell the window the current folder moved, so every page reads the new directory.

        The settings page announces the same event with its ``settings_changed`` signal, which is
        wired to ``MainWindow._on_settings_changed`` - the window's one "the game folder moved"
        handler (status-bar folder label, launch page, versions/resources pages). The rail calls that
        slot directly because this page has no window-level wiring of its own; it is fetched through
        ``getattr`` so a page built in a bare widget host (the render tools) simply skips it.
        """
        handler = getattr(self.window(), "_on_settings_changed", None)
        if callable(handler):
            handler()

    def _on_add_folder(self) -> None:
        """The rail's 「添加文件夹…」: the same entry the settings page runs, then redraw the rail."""
        if add_game_folder_entry(self) is None:
            return
        self.refresh()

    def _go_to_versions(self) -> None:
        """The empty folder's 去下载版本: open the versions page in the main rail."""
        from gui.main_window import NAV_KEYS

        sidebar = getattr(self.window(), "sidebar", None)
        if sidebar is None:
            return
        sidebar.setCurrentRow(NAV_KEYS.index("versions"))

    # ---------- the card pane ----------

    def refresh(self, select: str | None = None) -> None:
        """Rebuild the folder rail and the card pane; `select` forces a selection."""
        wanted = select if select is not None else self._selected_id
        cfg, _ = config.load()
        folders = ordered_folders(cfg.effective_game_folders())
        active = cfg.active_folder()
        if active is None and folders:
            active = folders[0]
        game_dir = Path(active.path) if active is not None else config.active_game_dir(cfg)
        self._game_dir = game_dir
        instances = list_instances(game_dir)
        self._instances = instances
        counts = {
            str(folder.path): (
                len(instances)
                if active is not None and config.same_game_folder(folder.path, active.path)
                else folder_instance_count(folder.path)
            )
            for folder in folders
        }
        self.folder_rail.set_folders(
            folders, active.path if active is not None else "", counts
        )
        current_id = str(cfg.selected_instance or "").strip()
        ordered = sorted(instances.values(), key=self._sort_key)
        needle = self.search_edit.text().strip().lower()
        shown = self._build_cards(ordered, cfg, game_dir, current_id, needle)
        self._update_list_state(len(instances), shown, bool(needle))
        # After the pane is the current stack page, not before: showing a card while its scroll area
        # is hidden does not give it a position, and the reveal then cannot scroll to it.
        self._settle_layout()
        self._select(wanted)
        if not instances:
            self.show_list()

    def _build_cards(self, ordered, cfg, game_dir: Path, current_id: str, needle: str) -> int:
        """Draw the cards - grouped by type, or flat while searching - and return how many showed."""
        self.groups_view.clear()
        self.result_count.setVisible(bool(needle))
        grouped: dict[str, list[_InstanceCard]] = {key: [] for key in GROUP_ORDER}
        flat: list[_InstanceCard] = []
        for inst in ordered:
            if needle and needle not in self._haystack(inst):
                continue
            version, loader = card_version_loader(inst, game_dir)
            mods, saves = instance_counts(inst, cfg, game_dir)
            counts = " · ".join(
                part
                for part in (
                    tr("instances.card.mods", mods) if mods else "",
                    tr("instances.card.saves", saves) if saves else "",
                )
                if part
            )
            card = _InstanceCard(
                inst,
                f"{version} · {loader}",
                counts,
                self._card_icon(version, loader, game_dir),
                current=inst.id == current_id,
                parent=self.groups_view,
            )
            card.use_requested.connect(self.instance_use_requested)
            card.manage_requested.connect(self._open_requested)
            # The card body's tooltip keeps the two facts the card itself elides: the profile label
            # beside the display name (when they differ) and the folder the instance lives in.
            card.setToolTip(self._row_text(inst) + "\n" + str(instance_dir(game_dir, inst.id)))
            if needle:
                flat.append(card)
            else:
                grouped[instance_group(version, loader, game_dir)].append(card)
        shown = 0
        if needle:
            # Searching does not group: one flat grid, introduced by the "N 个结果" line.
            section = self.groups_view.add_section(_GroupSection())
            for card in flat:
                section.add_card(card)
            shown = len(flat)
            self.result_count.setText(tr("instances.search.results", shown))
        else:
            for key in GROUP_ORDER:
                cards = grouped[key]
                if not cards:  # an empty group is not rendered at all
                    continue
                section = self.groups_view.add_section(
                    _GroupSection(tr(GROUP_TITLE_KEYS[key]))
                )
                for card in cards:
                    section.add_card(card)
                shown += len(cards)
            self.result_count.setText("")
        return shown

    def _settle_layout(self) -> None:
        """Position the freshly built sections, and show them when the page is on screen.

        Position first, unconditionally: the cards must enter the section grids even when this page
        is hidden (it is rebuilt from other pages' signals), or they would sit outside every layout
        until the next resize.

        Showing is guarded by visibility: a widget created while its page is on screen stays hidden
        until the event loop runs its show/layout requests, and a hidden card has no position (it
        reports 0,0), which is what made ``ensureWidgetVisible`` scroll nowhere. But doing that from
        a page that is *not* on screen posts window-level layout work, and the window then re-runs its
        minimum-width floor while the user is typing on the page they are actually looking at.
        """
        self._relayout_cards()
        if not self.isVisible():
            return
        for section in self.groups_view.sections():
            section.show()
            for card in section.cards:
                card.show()
            section_layout = section.layout()
            if section_layout is not None:
                section_layout.activate()
        layout = self.groups_view.layout()
        if layout is not None:
            layout.activate()

    def cards(self) -> list[_InstanceCard]:
        """Every card on screen, in visual order (across the group sections)."""
        return self.groups_view.cards()

    def card_ids(self) -> list[str]:
        return [card.instance_id for card in self.cards()]

    def card_for(self, instance_id: str | None) -> _InstanceCard | None:
        if not instance_id:
            return None
        for card in self.groups_view.cards():
            if card.instance_id == instance_id:
                return card
        return None

    def _card_icon(self, version: str, loader: str, game_dir: Path) -> QPixmap:
        """Loader glyph for a modded profile, the version's own block otherwise.

        ``card_version_loader`` already resolved the inherited Minecraft version, so the block
        category (release / snapshot / April Fools / ancient) is read from that version rather than
        from the display-name folder. The icon is rendered at 40px, the slot's own size, so the
        pixmap is shown 1:1 (the version rows keep the same rule at 48px); a missing texture falls
        back to the drawn block.
        """
        kind = block_textures.loader_kind_for_label(loader)
        tokens = theme.current_tokens() or theme.theme_palette("light")
        if kind is not None:
            return block_icons.loader_pixmap(kind, tokens.get("ACCENT", "#2f9e6f"), 22)
        launcher_dir = paths.launcher_dir()
        version_type = block_textures.version_type_for(
            version, game_dir, cache_dir=launcher_dir / "cache"
        )
        category = block_textures.icon_category(version, version_type)
        block = block_textures.block_name_for_category(category)
        return block_icons.block_pixmap(
            block,
            40,
            game_dir=game_dir,
            version_id=version,
            cache_root=block_textures.texture_cache_root(launcher_dir),
        )

    def _relayout_cards(self) -> None:
        """Reflow the sections against the pane width the user can actually see."""
        width = self.content_scroll.viewport().width()
        if width <= 0:
            width = self.width()
        self.groups_view.reflow(width)

    def _sort_key(self, inst):
        # Starred instances come first, then the chosen order
        primary = 0 if inst.star else 1
        if self.sort_combo.currentData() == "time":
            return (primary, -float(inst.created_at or 0.0), inst.name.lower())
        return (primary, inst.name.lower())

    def _row_text(self, inst) -> str:
        text = inst.name  # display name (falls back to the folder name)
        label = display_version_name(inst.id)
        if label != inst.name:
            text += "   [" + label + "]"
        if inst.note:
            text += "  — " + inst.note
        return text

    def _haystack(self, inst) -> str:
        """Search matches the display name, the instance id, the profile label and the note."""
        return " ".join(
            (inst.name, inst.id, display_version_name(inst.id), inst.note)
        ).lower()

    def _update_list_state(self, total: int, shown: int, searching: bool) -> None:
        """Pick the pane's state: cards, the empty folder, or the no-match hint.

        ``total`` is what the current folder holds and ``shown`` what survived the filter. An empty
        folder gets the two-line empty state with the 去下载版本 button (the folder is new - it has
        nothing to install from); a folder with instances but no matches gets the search hint.
        """
        if total == 0:
            self.list_stack.setCurrentWidget(self.empty_folder)
            return
        if shown == 0:
            self.list_stack.setCurrentWidget(self.empty_search)
            self.empty_search.update_for(0)
            return
        self.list_stack.setCurrentWidget(self.content_scroll)
        if searching:
            # The cards were laid out while the pane was hidden; give them the real width now.
            self._relayout_cards()

    def _select(self, instance_id: str | None) -> None:
        """Select a card by instance id (no selection when it is gone)."""
        found = instance_id if instance_id in self._instances else None
        self._selected_id = found
        self._on_selection_changed()

    def select_instance(self, instance_id: str) -> bool:
        """Show one instance in the selection model (used by the versions/launch pages).

        The grid only ever shows the current game folder, so an instance that lives in another
        folder switches the folder first: the detail pane resolves its paths against the current one.
        """
        wanted = str(instance_id or "").strip()
        if not wanted:
            self.refresh(select=None)
            return False
        if wanted not in self._instances:
            folder = self._folder_containing(wanted)
            if folder is not None and not self._is_active_folder(folder.path):
                self.switch_folder(folder.path, move_focus=False)
        self.refresh(select=wanted)
        found = self._selected_id == wanted
        if not found:
            set_app_status(self, tr("instances.msg.not_found", wanted), "warning")
        return found

    def _folder_containing(self, instance_id: str):
        """The registered folder that holds ``instance_id``, or None when no folder does."""
        cfg, _ = config.load()
        for folder in ordered_folders(cfg.effective_game_folders()):
            if self._is_active_folder(folder.path):
                continue
            try:
                if instance_id in list_instances(Path(folder.path)):
                    return folder
            except OSError:  # a folder that was moved or deleted: not a candidate
                continue
        return None

    def _current_name(self) -> str | None:
        return self._selected_id

    def _on_search_changed(self) -> None:
        self.refresh()

    def _on_selection_changed(self) -> None:
        instance_id = self._selected_id
        if instance_id is None:
            self.detail_page.set_instance(None)
            return
        self.detail_page.set_instance(instance_id)

    def _on_detail_changed(self) -> None:
        """Metadata changed in the detail: rebuild the grid, keeping the shown instance."""
        self.refresh(select=self.detail.instance_id or self._selected_id)

    def _on_detail_deleted(self, _instance_id: str) -> None:
        self.refresh()
        self.show_list()

    def _selected_instance(self):
        instance_id = self._current_name()
        if not instance_id:
            return None
        return self._instances.get(instance_id) if hasattr(self, "_instances") else None

    # ---------- page-level actions (delegating to the detail) ----------

    def _open_folder(self) -> None:
        """Open the selected instance folder."""
        if self._current_name() is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return
        self.detail.open_instance_folder()

    def _rename(self) -> None:
        if self._current_name() is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return
        self.detail.rename()

    def _edit_note(self) -> None:
        if self._current_name() is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return
        self.detail.edit_note()

    def _export(self) -> None:
        if self._current_name() is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return
        self.detail.export()

    def _delete(self) -> None:
        if self._current_name() is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return
        self.detail.delete()

    def _launch(self, trigger_source: TriggerSource | str = TriggerSource.UNKNOWN) -> None:
        """Legacy delegate: the detail page owns the button, but this entry passes the source on."""
        if self._current_name() is None:
            set_app_status(self, tr("instances.msg.need_select"), "warning")
            return
        self.detail.launch(trigger_source)

    def _open_crash_viewer(self) -> None:
        self.detail.open_crash_viewer()

    def _install_dropped(self, jars: list[Path]) -> None:
        self.detail.install_dropped(jars)

    def _create(self) -> None:
        dialog = _CreateDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, version = dialog.values()
        if not name or not version:
            set_app_status(self, tr("instances.msg.need_fields"), "warning")
            return
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()

        def do_create() -> object:
            return create_instance(
                name, version, game_dir, cache_dir=paths.launcher_dir() / "cache"
            )

        disable_keeping_focus(self.create_button)
        run_in_background(
            do_create,
            on_result=self._on_created,
            on_error=self._on_create_failed,
            on_finished=self._on_create_finished,
        )

    def _on_created(self, instance) -> None:
        self.refresh(select=instance.id)
        set_app_status(self, tr("instances.msg.created", instance.name))

    def _on_create_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.msg.create_fail", message), "error")

    def _on_create_finished(self) -> None:
        self.create_button.setEnabled(True)

    def _import(self) -> None:
        zip_path, _filter = QFileDialog.getOpenFileName(
            self, tr("instances.import"), "", "Zip (*.zip)"
        )
        if not zip_path:
            return
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()

        disable_keeping_focus(self.import_button)
        run_in_background(
            import_instance,
            Path(zip_path),
            game_dir,
            on_result=self._on_imported,
            on_error=self._on_import_failed,
            on_finished=self._on_import_finished,
        )

    def _on_imported(self, instance) -> None:
        self.refresh(select=instance.id)
        set_app_status(self, tr("instances.msg.imported", instance.name))

    def _on_import_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.msg.import_fail", message), "error")

    def _on_import_finished(self) -> None:
        self.import_button.setEnabled(True)
