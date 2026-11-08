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

"""Instances page: a two-level browser - a game-folder rail, then grouped rows.

An instance *is* a version folder (`versions/<id>/`), but the launcher can point at several game
folders, so the page has two levels of its own:

* the 200px rail on the left (``gui.pages.instance_folders.FolderRail``) lists the known folders with
  their instance counts and carries the 「添加文件夹…」 entry - it is this page's column, never a
  second main rail;
* the pane on the right holds the search box, the sort control and the rows, grouped as 正式版 /
  快照版 / 带加载器 (empty groups do not render). A search flattens that grouping into one result
  list with an "N 个结果" line, the same language the resources page uses.

One instance is one compact row (icon, name, one metadata line, 使用/管理 on the right), not a large
card: a 156px card showed two instances per screen, which is what "these cards waste space" was about.
The row keeps the same group headings and the same two-level navigation; it only spends less height.

Everything that acts on one instance lives in @gui.pages.instance_detail.InstanceDetail; the rows
never embed it, and the detail page is shown only after a deliberate drill-down.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
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

from gui import block_icons, i18n
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
from launcher.meta.version import version_json_path

tr = i18n.tr

#: The compact instance row: 1px border, 14px above and below, a 44px icon and two text lines. 88px is
#: the height the "one screen shows five or six" target needs; the old card was 156px and showed two.
CARD_HEIGHT = 88
#: The row gap. Eight, not twelve: the row is already the visual block, so the gap only separates rows.
CARD_GAP = 8
#: A row's hard floor, independent of the text it happens to hold.
CARD_MIN_WIDTH = 260
#: The icon's box, and the size its pixmap is rendered at (1:1, never scaled by the label).
CARD_ICON_SIZE = 44
# One row per instance, always. The grid used to add a column at 676px (two) and 1008px (three), and
# at 1644x915 the third column was cut in half: the fixed card width became the content widget's
# minimum width, the horizontal scrollbar is off, and the pane could never shrink back under a width it
# had once been laid out at. A row that spans the pane removes the whole class of "a column that does
# not fit"; the number of columns no longer changes with the window at all.
CARD_COLUMNS = 1
# The pane's floor: the width one row is comfortably drawn at, plus the pane's two gutters. The row is
# narrower than the card it replaced (the card stacked its counts and buttons under the text), so the
# floor drops from 320 to 300 (measured by tools/measure_min_width.py: 746 -> 718 in zh_cn; en_us stays
# at 747 because its unchanged action-button row, not the list, is that language's floor).
CARD_FLOOR_WIDTH = 300
CARD_GRID_MIN_WIDTH = CARD_FLOOR_WIDTH + 2 * CARD_GAP

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


@dataclass(frozen=True)
class _ScanData:
    """One directory scan's result: everything the pane needs, with no Qt object in it.

    The scan itself runs on a worker (the versions tree can be large and every row costs a version
    JSON, a mods count and a saves count). This is the hand-off back to the GUI thread; the row
    widgets and their pixmaps are still built on the GUI thread from this data.
    """

    generation: int
    wanted: str | None
    game_dir: Path
    instances: dict = field(default_factory=dict)
    folders: list = field(default_factory=list)
    active_path: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    #: instance id -> (minecraft version, loader, the card's 版本 · 加载器 line, group key)
    meta: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    #: instance id -> (mods, saves)
    numbers: dict[str, tuple[int, int]] = field(default_factory=dict)
    current_id: str = ""
    error: str = ""


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
    """How many cards fit across ``width``: always one, so a card spans the whole pane.

    Kept as a function (rather than inlining the constant) because it is the seam the tests and the
    width audit talk to, and the answer is deliberately independent of ``width`` now.
    """
    return CARD_COLUMNS


def _profile_json(game_dir: Path, version_id: str) -> dict:
    path = version_json_path(instance_dir(game_dir, version_id))
    if path is None:
        return {}
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


def card_version_loader(
    instance, game_dir: Path, *, version_id: str | None = None
) -> tuple[str, str]:
    """Resolve the ``版本 · 加载器`` line, following an inherited profile when needed.

    ``version_id`` is the profile to describe; it defaults to the one the instance really runs
    (``instance.version_id``, i.e. ``base_version`` when the instance points at another profile).
    The caller passes the folder id explicitly when the pointer is broken, so the card still
    describes the profile that is left.
    """
    start = version_id or instance.version_id
    seen: set[str] = set()
    current = start
    while current and current not in seen:
        seen.add(current)
        known = _known_profile_identity(current)
        if known is not None:
            return known
        parent = _profile_json(game_dir, current).get("inheritsFrom")
        if not isinstance(parent, str) or not parent.strip():
            return current, tr("instances.card.vanilla")
        current = parent.strip()
    return start, tr("instances.card.vanilla")


def card_version_meta(instance, game_dir: Path) -> tuple[str, str, str]:
    """The card's ``版本 · 加载器`` line, or the sentence that says what is missing instead.

    Returns ``(version, loader, text)``: the first two still drive the card's group, the third is
    what the card shows. The pair describes the profile the launch path runs (``instance.version_id``),
    so 修改 re-pointing an instance at another installed profile changes this line too. A pointer at a
    profile that is no longer installed is not a blank line: the pair falls back to the folder's own
    profile (so the group and the icon still mean something) and the card says 指向的版本缺失. A folder
    with no profile at all (a damaged install with only its jar left) keeps its card and says
    版本信息缺失. A profile that exists but is sparse still labels the card - the loader can come from
    the folder id, and an empty official-format file is not "missing".
    """
    pointed = instance.version_id
    own = instance.id
    if pointed != own and version_json_path(instance_dir(game_dir, pointed)) is None:
        version, loader = card_version_loader(instance, game_dir, version_id=own)
        return version, loader, tr("instances.card.base_version_missing")
    version, loader = card_version_loader(instance, game_dir)
    if version_json_path(instance_dir(game_dir, pointed)) is None:
        return version, loader, tr("instances.card.version_missing")
    return version, loader, f"{version} · {loader}"


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


class _RowLabel(ElidedLabel):
    """An eliding label that asks for its text width, but gives it back when the row is narrow.

    ``ElidedLabel`` ignores its size hint so its container can hand it the real width. That is right
    for a label that fills a row on its own, but these two lines share their row: the metadata line
    is followed by a fixed-width tail (the counts), and the name must give width back before either
    of them is squeezed. So this label states the width its text really needs - the inherited hint is
    measured from the *elided* string the base QLabel was last given, often nothing - and still lets
    a narrow layout shrink it. ``ElidedLabel`` paints the ellipsis once the layout does.
    """

    def sizeHint(self) -> QSize:
        # ElidedLabel paints the *elided* string into the base QLabel, so the inherited size hint is
        # measured from whatever fit the last width - often nothing. Measure the logical text instead.
        metrics = self.fontMetrics()
        return QSize(
            metrics.horizontalAdvance(self.text()) + 2,
            max(metrics.height(), super().sizeHint().height()),
        )

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())


class _InstanceCard(QFrame):
    """One compact instance row: 使用 is its primary action, 管理 opens the detail page.

    Height is the point of this widget: the old card stacked a name row, a metadata row, a counts row
    and a button row inside 156px of mostly empty space. The row keeps the same facts in two lines -
    icon, name, metadata (version · loader · counts) - and moves the two buttons to the right edge,
    vertically centred. One row is 88px, so a normal-height window shows five or six instances instead
    of two.

    The row itself is a keyboard stop, not just its buttons: Tab lands on the row (the whole row paints
    the focus ring), then 使用, then 管理; Enter/Space on the row opens the detail page. The instance
    the launcher is set to run carries the same 3px accent bar and pale accent wash the detail sidebar
    uses (one selection language across the app), and a double-click on the row body still uses it.
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
        # A QFrame only paints a QSS background when it is told to; the row's list fill depends on it.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # The row is one keyboard stop of its own; the two buttons stay the two actions.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(CARD_HEIGHT)
        # The "current" marker is the detail sidebar's 3px accent bar plus its pale accent wash,
        # painted by the stylesheet through this property; keeping it a property means the row can
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

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(14)

        # Parented at creation: a parentless widget is a top-level window, and the row's parts are
        # built while the grid is already on screen (every refresh rebuilds them).
        icon = QLabel(self)
        icon.setObjectName("instanceCardIcon")
        icon.setFixedSize(CARD_ICON_SIZE, CARD_ICON_SIZE)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(icon_pixmap)
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.icon_label = icon
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        # The two text lines. The column is transparent to the mouse so a double-click anywhere on the
        # text still reaches the row; the labels themselves are not focus stops (ElidedLabel enables
        # text selection, which would add a ClickFocus stop with no focus rule - the keyboard
        # walkthrough used to flag exactly that). The row's own focus ring is the cue.
        text = QWidget(self)
        text.setObjectName("instanceCardText")
        text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.text_column = text
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        name = _RowLabel(instance.name, elide_mode=Qt.TextElideMode.ElideRight)
        name.setObjectName("instanceCardName")
        name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        name.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Named attributes: the two text lines are what the row says, and the tests (plus any later
        # row variant) read them instead of walking the layout.
        self.name_label = name
        text_layout.addWidget(name)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(6)
        meta_label = _RowLabel(meta, elide_mode=Qt.TextElideMode.ElideRight)
        meta_label.setObjectName("instanceCardMeta")
        meta_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        meta_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.meta_label = meta_label
        # The counts used to sit on their own footer row. They now close the metadata line, separated
        # from the version by the same middle dot, so the row loses the card's third line. The tail
        # keeps its natural width; the version label is the only part that may shrink (and elide).
        self.meta_separator = QLabel("·", text)
        self.meta_separator.setObjectName("instanceCardMetaSep")
        self.meta_separator.setVisible(bool(counts))
        meta_row.addWidget(meta_label, 0)
        meta_row.addWidget(self.meta_separator, 0)
        counts_label = QLabel(counts, text)
        counts_label.setObjectName("instanceCardCounts")
        counts_label.setWordWrap(False)
        # Parented above: a parentless label that is told to be visible is a top-level window for an
        # instant, and showing it takes the window activation away from the page the user is on.
        counts_label.setVisible(bool(counts))
        self.counts_label = counts_label
        meta_row.addWidget(counts_label, 0)
        meta_row.addStretch(1)
        text_layout.addLayout(meta_row)
        layout.addWidget(text, 1, Qt.AlignmentFlag.AlignVCenter)

        # The primary action. It keeps the button base rule (accent fill, white text), so it reads as
        # "the" action on the row without a second colour vocabulary.
        self.use_button = QPushButton(tr("instances.card.use"), self)
        self.use_button.setObjectName("instanceCardUse")
        self.use_button.setAccessibleName(tr("instances.card.use") + " " + instance.name)
        self.use_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.use_button.setFixedHeight(30)
        self.use_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.use_button.clicked.connect(lambda: self.use_requested.emit(self.instance_id))
        # The secondary sibling: the same quiet outline the card used to draw for 管理.
        self.manage_button = QPushButton(tr("instances.card.manage"), self)
        self.manage_button.setObjectName("instanceCardManage")
        self.manage_button.setAccessibleName(tr("instances.card.open", instance.name))
        self.manage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage_button.setFixedHeight(30)
        self.manage_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.manage_button.clicked.connect(lambda: self.manage_requested.emit(self.instance_id))
        layout.addWidget(self.use_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.manage_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def keyPressEvent(self, event) -> None:
        """Enter/Space on the focused row opens the detail page, like 管理 does."""
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.manage_requested.emit(self.instance_id)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-clicking the row body uses the instance (the buttons keep their own clicks)."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.use_requested.emit(self.instance_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _GroupSection(QWidget):
    """One group: a 15/600 heading plus its instance rows, each spanning the pane.

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
        # The single column takes every pixel the section has: that is what makes the card fill the
        # row. No horizontal alignment on the grid (it would shrink the layout back to its size hint).
        self.grid.setColumnStretch(0, 1)
        layout.addWidget(self.title_label)
        layout.addLayout(self.grid)
        # The grid keeps its rows at their size hint and the section keeps its content at the top;
        # without this the spare height would be spread between the cards instead of staying below.
        layout.addStretch(1)

    def add_card(self, card: _InstanceCard) -> None:
        self.cards.append(card)

    def reflow(self, width: int) -> None:
        """Place every row in the one column; the layout supplies the width, not the row.

        The width used to be written into the card with ``setFixedSize``. That fixed width became the
        content widget's minimum width, so a pane laid out once at 1386px could never shrink back to
        its 1224px viewport: the scroll area kept the wider widget and, because the horizontal
        scrollbar is off, cut the right edge off the row. An expanding row in a stretched one-column
        grid follows the pane in both directions; only the height stays fixed at :data:`CARD_HEIGHT`.
        """
        for card in self.cards:
            if card.height() != CARD_HEIGHT:
                card.setFixedHeight(CARD_HEIGHT)
            if card.minimumWidth() != CARD_MIN_WIDTH:
                card.setMinimumWidth(CARD_MIN_WIDTH)
        columns = card_columns(width)
        if columns == self._columns and self.grid.count() == len(self.cards):
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index, 0)
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

    def clear(self, retired: QWidget | None = None) -> None:
        """Drop every section; ``retired`` is where a dropped section waits for its deferred delete.

        A section is detached, not deleted on the spot: ``deleteLater`` is what keeps a card's
        ``clicked`` handler from deleting the widget that is emitting the signal. The detach has to be
        ordered as well. ``setParent(None)`` on a *visible* widget is the exact shape of the flash this
        page was reported for: Qt turns the widget into a top-level window (its own native title bar,
        at the top-left corner) for as long as it exists, which is one event-loop turn. So the section
        is hidden first - a hidden widget is not a window anybody can see - and then handed to
        ``retired``, a hidden child of the page, so it is never a top-level window either. With no
        ``retired`` host (a bare ``_CardGroupView`` in a test) the detach is the only option left.
        """
        for section in self._sections:
            self._layout.removeWidget(section)
            section.hide()
            if retired is not None:
                section.setParent(retired)
            else:
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
        self.download_button = QPushButton(tr("instances.folder.download"), self)
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
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText(tr("instances.name.placeholder"))
        self.version_combo = QComboBox(self)
        self.version_combo.setEditable(True)
        self.version_combo.addItem(tr("instances.loading"))
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
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
    #: The detail page's back control when the card was opened from the launch page: the window
    #: returns to the launch page instead of the card grid, so the way in is the way out.
    return_to_launch_requested = Signal()
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
        #: The newest applied scan, and a counter that decides which of two overlapping scans wins.
        #: A page entry while the previous scan still runs bumps the counter, so the older answer is
        #: dropped instead of overwriting the newer one.
        self._last_scan: _ScanData | None = None
        self._scan_generation = 0
        #: Set while a reveal still has to be scrolled into view once the layout has run.
        self._pending_reveal: str | None = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal_pending_card)

        self.search_edit = QLineEdit(self)
        add_search_icon(self.search_edit)
        self.search_edit.setPlaceholderText(tr("instances.search.placeholder"))
        self.search_edit.setAccessibleName(tr("instances.search.placeholder"))  # WCAG 4.1.2
        self.search_edit.setClearButtonEnabled(True)

        # The left column: folders, their counts and the shared 「添加文件夹…」 entry.
        self.folder_rail = FolderRail(self)
        self.folder_list = self.folder_rail.list

        # The right pane: one scroll area of group sections. Searching collapses it to a single flat
        # section, which is why the same container serves both states.
        self.groups_view = _CardGroupView(self)
        self.content_scroll = QScrollArea(self)
        self.content_scroll.setObjectName("instanceGroupScroll")
        self.content_scroll.setAccessibleName(tr("nav.instances"))
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_scroll.setWidget(self.groups_view)
        # The rows and their two buttons are the keyboard stops; a second stop around the whole list
        # would announce the same content twice (the old IconMode grid had the same rule).
        self.content_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The pane's floor: one row at the width the list draws it. Declared here, not on show, so
        # the page's own minimum width is part of the window's floor from the moment it is built
        # (tools/measure_min_width.py reads the window's requirement, and a floor that only appears
        # while the page is visible would let the window shrink under it again).
        self.content_scroll.setMinimumWidth(CARD_GRID_MIN_WIDTH)
        attach_hover_scrollbar(self.content_scroll)

        # "N 个结果", the resources page's search-summary wording. Hidden unless a search is running.
        self.result_count = QLabel("", self)
        self.result_count.setObjectName("instanceResultCount")
        self.result_count.setVisible(False)

        self.empty_folder = _FolderEmptyState(self)
        self.empty_search = EmptyState(tr("instances.search.none"), self)
        #: Shown while the directory scan runs in the background: a hint, not an empty pane, and
        #: never the previous folder's rows presented as the current state. It reuses the existing
        #: (加载中...) string, so the waiting state needs no new translation.
        self.loading_state = EmptyState(tr("instances.loading"), self)
        self.loading_state.update_for(0)  # EmptyState starts hidden; the stack shows it on purpose
        self.list_stack = QStackedWidget(self)
        self.list_stack.addWidget(self.content_scroll)
        self.list_stack.addWidget(self.empty_folder)
        self.list_stack.addWidget(self.empty_search)
        self.list_stack.addWidget(self.loading_state)

        self.sort_combo = QComboBox(self)
        self.sort_combo.addItem(tr("instances.sort.name"), "name")
        self.sort_combo.addItem(tr("instances.sort.time"), "time")
        # WCAG 4.1.2: the visible label sits in the same row layout, not in a QFormLayout row, so it
        # never became this combo's name and the control was announced as an unlabelled list.
        self.sort_combo.setAccessibleName(tr("instances.sort.label"))

        self.create_button = QPushButton(tr("instances.create_copy"), self)
        self.import_button = QPushButton(tr("instances.import"), self)
        self.import_button.setObjectName("secondaryButton")

        self.detail_page = InstanceDetailPage(self)
        self.detail = self.detail_page.detail
        self.detail_stack = self.detail_page.detail_stack
        self.no_selection = self.detail_page.no_selection

        # Legacy attribute names kept for callers (and tests) that predate the detail page
        self.open_folder_button = self.detail.open_folder_button
        self.launch_button = self.detail.launch_button
        self.crash_button = self.detail.crash_button
        self.mods_table = self.detail.mods_table

        # Where a section waits for its deferred delete (see :meth:`_CardGroupView.clear`): a hidden
        # child of this page, so a dropped section is neither painted nor a top-level window. It is not
        # inside groups_view, so ``groups_view.findChildren`` keeps describing only the live sections.
        self._retired_groups = QWidget(self)
        self._retired_groups.setObjectName("instanceGroupRetired")
        self._retired_groups.setVisible(False)

        browser = QWidget(self)
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
        sort_label = QLabel(tr("instances.sort.label"), browser)
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

        self.view_stack = QStackedWidget(self)
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
        # Building the page must not scan the versions tree on the GUI thread: the first scan runs
        # on a worker and fills the pane when it comes back (see refresh_in_background).
        self.refresh_in_background()

    # ---------- page navigation ----------

    def open_instance(self, instance_id: str, *, origin: str = "list") -> bool:
        """Show one instance's detail page (the window calls this for every drill-down route).

        ``origin`` names the page that asked for it, and only decides where the back control goes and
        what it says: the card grid returns to the grid, the launch page returns to the launch page.
        """
        found = self.select_instance(instance_id)
        if not found:
            return False
        self._detail_origin = origin
        self.detail_page.set_back_origin(origin)
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
        origin = getattr(self, "_detail_origin", "list")
        self.show_list()
        if origin == "launch":
            self.return_to_launch_requested.emit()
        else:
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
        """Rebuild synchronously; action paths and tests need the new state on return.

        This reads the versions tree on the calling (GUI) thread. Page entry and construction use
        :meth:`refresh_in_background` instead, so opening 实例 never waits for the disk.
        """
        self._scan_generation += 1
        generation = self._scan_generation
        wanted = select if select is not None else self._selected_id
        cfg, _ = config.load()
        folders, active, game_dir = self._scan_roots(cfg)
        self._apply_scan(self._scan_task(generation, wanted, cfg, folders, active, game_dir))

    def refresh_in_background(self, select: str | None = None) -> None:
        """Scan the folder on a worker, then fill the pane with the answer (never block the GUI).

        A page entry starts one, and so does construction. The generation counter makes only the
        newest scan allowed to paint: a scan superseded by a newer entry - or by an action path's
        synchronous refresh - drops its result instead of overwriting the newer state. Meanwhile the
        pane shows the loading placeholder, which is neither blank nor a made-up list.
        """
        self._scan_generation += 1
        generation = self._scan_generation
        wanted = select if select is not None else self._selected_id
        cfg, _ = config.load()
        folders, active, game_dir = self._scan_roots(cfg)
        self._show_scanning_state()
        run_in_background(
            self._scan_task,
            generation,
            wanted,
            cfg,
            folders,
            active,
            game_dir,
            on_result=self._on_scan_ready,
        )

    @staticmethod
    def _scan_roots(cfg) -> tuple[list, object, Path]:
        """The folder facts a scan needs, resolved once so both refresh paths agree."""
        folders = ordered_folders(cfg.effective_game_folders())
        active = cfg.active_folder()
        if active is None and folders:
            active = folders[0]
        game_dir = Path(active.path) if active is not None else config.active_game_dir(cfg)
        return folders, active, game_dir

    def _scan_task(
        self, generation: int, wanted: str | None, cfg, folders: list, active, game_dir: Path
    ) -> _ScanData:
        """Collect everything the pane draws from disk; runs on a worker and touches no widget."""
        try:
            instances = list_instances(game_dir)
            active_path = str(active.path) if active is not None else ""
            counts = {
                str(folder.path): (
                    len(instances)
                    if active is not None and config.same_game_folder(folder.path, active.path)
                    else folder_instance_count(folder.path)
                )
                for folder in folders
            }
            meta: dict[str, tuple[str, str, str, str]] = {}
            numbers: dict[str, tuple[int, int]] = {}
            for instance in instances.values():
                version, loader, text = card_version_meta(instance, game_dir)
                meta[instance.id] = (version, loader, text, instance_group(version, loader, game_dir))
                numbers[instance.id] = instance_counts(instance, cfg, game_dir)
        except Exception as exc:  # noqa: BLE001 - a broken tree is one reported state, never a crash
            return _ScanData(
                generation=generation, wanted=wanted, game_dir=game_dir, error=str(exc)
            )
        return _ScanData(
            generation=generation,
            wanted=wanted,
            game_dir=game_dir,
            instances=instances,
            folders=folders,
            active_path=active_path,
            counts=counts,
            meta=meta,
            numbers=numbers,
            current_id=str(cfg.selected_instance or "").strip(),
        )

    def _on_scan_ready(self, scan: _ScanData) -> None:
        """A worker's scan came back (GUI thread): paint it only if it is still the newest one."""
        if scan.generation != self._scan_generation:
            return  # a newer entry/refresh already owns the pane
        if scan.error:
            set_app_status(self, tr("instances.fetch_fail", scan.error), "error")
            self.loading_state.setText(tr("instances.fetch_fail", scan.error))
            self.list_stack.setCurrentWidget(self.loading_state)
            return
        self._apply_scan(scan)

    def _show_scanning_state(self) -> None:
        """The in-between state: a hint that the list is being read, not an empty or stale pane."""
        self.loading_state.setText(tr("instances.loading"))
        self.loading_state.update_for(0)
        self.list_stack.setCurrentWidget(self.loading_state)

    def _apply_scan(self, scan: _ScanData) -> None:
        """Paint one scan: the folder rail, the cards, the selection and the empty state."""
        self._game_dir = scan.game_dir
        self._instances = scan.instances
        self._last_scan = scan
        self.folder_rail.set_folders(scan.folders, scan.active_path, scan.counts)
        ordered = sorted(scan.instances.values(), key=self._sort_key)
        needle = self.search_edit.text().strip().lower()
        shown = self._build_cards(ordered, scan, needle)
        self._update_list_state(len(scan.instances), shown, bool(needle))
        # After the pane is the current stack page, not before: showing a card while its scroll area
        # is hidden does not give it a position, and the reveal then cannot scroll to it.
        self._settle_layout()
        self._select(scan.wanted)
        if not scan.instances:
            self.show_list()

    def _build_cards(self, ordered, scan: _ScanData, needle: str) -> int:
        """Draw the rows - grouped by type, or flat while searching - and return how many showed."""
        self.groups_view.clear(self._retired_groups)
        self.result_count.setVisible(bool(needle))
        grouped: dict[str, list[_InstanceCard]] = {key: [] for key in GROUP_ORDER}
        flat: list[_InstanceCard] = []
        for inst in ordered:
            if needle and needle not in self._haystack(inst):
                continue
            _version, _loader, meta, group = scan.meta[inst.id]
            mods, saves = scan.numbers[inst.id]
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
                meta,
                counts,
                self._card_icon(inst, scan.game_dir),
                current=inst.id == scan.current_id,
                parent=self.groups_view,
            )
            card.use_requested.connect(self.instance_use_requested)
            card.manage_requested.connect(self._open_requested)
            # The card body's tooltip keeps the two facts the card itself elides: the profile label
            # beside the display name (when they differ) and the folder the instance lives in.
            card.setToolTip(self._row_text(inst) + "\n" + str(instance_dir(scan.game_dir, inst.id)))
            if needle:
                flat.append(card)
            else:
                grouped[group].append(card)
        shown = 0
        if needle:
            # Searching does not group: one flat grid, introduced by the "N 个结果" line.
            # Parented at creation, unlike the labels this page used to build: _settle_layout() shows
            # these sections, and a section that the layout has not adopted yet is a window of its own
            # for one frame - the flash the card grid was reported for.
            section = self.groups_view.add_section(_GroupSection(parent=self.groups_view))
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
                    _GroupSection(tr(GROUP_TITLE_KEYS[key]), self.groups_view)
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
        self._apply_tab_order()
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

    def _card_icon(self, inst, game_dir: Path) -> QPixmap:
        """The instance's chosen icon, or the rule-derived one.

        The whole read order lives in :func:`gui.block_icons.instance_icon_pixmap`: the instance's
        ``icon`` field first (a rule name, or ``custom:<file>`` inside its folder), the automatic
        rule when it is empty or the picture cannot be read. A custom picture is shown as the
        picture (rounded at :data:`CARD_ICON_SIZE`), never through the isometric cube. The icon is
        rendered at the slot's own size, so the pixmap is shown 1:1.
        """
        return block_icons.instance_icon_pixmap(
            inst,
            instance_dir(game_dir, inst.id),
            CARD_ICON_SIZE,
            game_dir=game_dir,
            cache_root=block_textures.texture_cache_root(paths.launcher_dir()),
        )

    def _relayout_cards(self) -> None:
        """Reflow the sections against the pane width the user can actually see."""
        width = self.content_scroll.viewport().width()
        if width <= 0:
            width = self.width()
        self.groups_view.reflow(width)

    def _apply_tab_order(self) -> None:
        """Make Tab walk the rows in the order they are painted: row, 使用, 管理, next row.

        The widgets are created in *sort* order and only then grouped, so their creation order (which
        Qt's default focus chain follows) is not the visual order: from the first release row, Tab
        could jump into the loader group before the second release row. Stating the order explicitly
        after every rebuild keeps the keyboard path identical to the eye's path, including after a
        search flattens the groups into one section.
        """
        previous = None
        for card in self.groups_view.cards():
            if previous is not None:
                QWidget.setTabOrder(previous, card)
            QWidget.setTabOrder(card, card.use_button)
            QWidget.setTabOrder(card.use_button, card.manage_button)
            previous = card.manage_button

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
