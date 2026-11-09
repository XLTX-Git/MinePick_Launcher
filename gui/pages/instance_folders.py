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

"""The instances page's game-folder rail: the first level of its two-level navigation.

This is a page-local rail, not a second main navigation. The main rail (启动 / 实例 / 版本 / …) still
says which *page* is open; this one says which *folder* the instance list is showing, and it exists
only while the instances page is (the detail page keeps its own sidebar and hides this one with the
rest of the list).

``FolderRail`` owns the 200px column, its row widgets and the 「添加文件夹…」 entry.
:func:`add_game_folder_entry` is that entry: the same three steps the settings page's game-directory
card runs (a directory picker, ``LauncherConfig.add_game_folder``, ``config.save``), so a folder added
from either place lands in the same list and reports the same "added" / "duplicate" message.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui import i18n
from gui.widgets import ElidedLabel, apply_no_focus_outline, set_app_status
from launcher import config, paths
from launcher.instances import list_instances

tr = i18n.tr

#: The rail's fixed width, measured against the detail sidebar's 200px so the two left columns of the
#: app read as one width. It is a hard width, not a hint: the card pane's own floor is stated on top
#: of it (see ``instances_page.CARD_GRID_MIN_WIDTH``), which is what keeps the window inside 1180px.
FOLDER_RAIL_WIDTH = 200
#: Gap between the rail and the card pane. 12px is the same gutter the card grid uses.
FOLDER_RAIL_GAP = 12
#: One row is 36px, exactly the detail sidebar's item height (one selection language, one rhythm).
FOLDER_ITEM_HEIGHT = 36
FOLDER_ROW_PADDING_X = 12


def ordered_folders(folders) -> list:
    """The rows in display order: the launcher's default folder first, the rest as they were added.

    "Default" is the folder the launcher uses when the config names none (``paths.default_game_dir``
    - normally ``%APPDATA%\\.minecraft``), *not* the folder that is current right now. Pinning the
    current folder would re-sort the rail on every switch, which would move the row out from under a
    keyboard user's arrow keys; the current folder is shown by the selected row instead.
    """
    try:
        default_dir = paths.default_game_dir()
    except OSError:  # pragma: no cover - a platform without a resolvable home
        return list(folders)
    pinned: list = []
    rest: list = []
    for folder in folders:
        if str(folder.path or "").strip() and config.same_game_folder(folder.path, default_dir):
            pinned.append(folder)
        else:
            rest.append(folder)
    return pinned + rest


class _FolderRow(QWidget):
    """One rail row: the folder name (elided) and its instance count.

    Both are labels, so the row cannot steal the pointer from the list: the item underneath owns the
    click and the keyboard selection. ``active`` is the selected row's mark - the QSS paints the 3px
    accent bar and the pale accent wash on the list item, while these labels switch to the accent text
    colour, the same pair the detail sidebar uses for its selected row.
    """

    def __init__(self, name: str, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(FOLDER_ITEM_HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(FOLDER_ROW_PADDING_X, 0, FOLDER_ROW_PADDING_X, 0)
        layout.setSpacing(8)
        # Parented at creation: a parentless label that is told to be visible is a top-level window
        # for an instant. The row itself is built while the rail is on screen (a folder switch
        # rebuilds it), so nothing in here may start life parentless.
        self.name_label = ElidedLabel(name, self)
        self.name_label.setObjectName("instanceFolderName")
        # ElidedLabel is selectable by mouse, which makes it a ClickFocus stop with no focus rule of
        # its own; the row is a list item, so its text must not enter the tab chain.
        self.name_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.count_label = QLabel(str(count), self)
        self.count_label.setObjectName("instanceFolderCount")
        self.count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.count_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.count_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.name_label, 1)
        layout.addWidget(self.count_label, 0)

    def set_count(self, count: int) -> None:
        self.count_label.setText(str(count))

    def set_active(self, active: bool) -> None:
        for label in (self.name_label, self.count_label):
            if bool(label.property("active")) == active:
                continue
            label.setProperty("active", active)
            style = label.style()
            style.unpolish(label)
            style.polish(label)


class FolderRail(QWidget):
    """The 200px folder column: a keyboard-navigable list plus the shared 「添加文件夹…」 entry.

    Three signals keep the page in charge of the data:
    ``folder_changed`` fires whenever the current row moves (arrow keys included), so the card pane
    follows the selection; ``folder_activated`` fires when the user commits a row (click, or Enter on
    it) and the page may hand keyboard focus to the first card; ``add_requested`` asks the page to run
    the add-folder entry.
    """

    folder_changed = Signal(str)
    folder_activated = Signal(str)
    add_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("instanceFolderRail")
        self.setFixedWidth(FOLDER_RAIL_WIDTH)
        self._rows: list[_FolderRow] = []
        self._items: list[QListWidgetItem] = []
        self._paths: list[str] = []
        self._labels: list[tuple[str, int]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.list = QListWidget(self)
        self.list.setObjectName("instanceFolderList")
        # WCAG 4.1.2: the list is a keyboard stop (Tab reaches it, the arrow keys move inside it) and
        # would otherwise be announced as an unnamed list.
        self.list.setAccessibleName(tr("instances.folder.list"))
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setSpacing(0)
        # The list's own border is the keyboard focus ring (see the sheet): a view repaints its frame
        # on focus, while an item pseudo-state proved unreliable for list items.
        apply_no_focus_outline(self.list)

        self.add_button = QPushButton(tr("settings.folder.add"), self)
        self.add_button.setObjectName("secondaryButton")
        self.add_button.setAccessibleName(tr("settings.folder.add"))
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(self.add_requested)

        layout.addWidget(self.list, 1)
        layout.addWidget(self.add_button)

        self.list.currentRowChanged.connect(self._on_current_row_changed)
        self.list.itemClicked.connect(self._on_item_activated)
        self.list.itemActivated.connect(self._on_item_activated)

    # ---------- data ----------

    def set_folders(self, folders, active_path, counts: dict[str, int]) -> None:
        """Draw ``folders`` and select ``active_path``; cheap when the folder set did not change."""
        paths = [str(folder.path) for folder in folders]
        labels = [(folder.display_name(), int(counts.get(str(folder.path), 0))) for folder in folders]
        if paths != self._paths:
            self._rebuild(folders, labels)
        elif labels != self._labels:
            for row, (_name, count) in zip(self._rows, labels, strict=False):
                row.set_count(count)
            self._labels = labels
        self.set_active(active_path)

    def _rebuild(self, folders, labels: list[tuple[str, int]]) -> None:
        self.list.blockSignals(True)
        try:
            self.list.clear()
            self._rows = []
            self._items = []
            self._paths = []
            for folder, (name, count) in zip(folders, labels, strict=False):
                path = str(folder.path)
                text = tr("instances.folder.a11y", name, count)
                item = QListWidgetItem()
                # What a screen reader announces: the name *and* the instance count, in one string.
                # Only the accessible role is set - a display-role text would be painted by the view
                # underneath the transparent row widget, and the row's two labels would show twice.
                item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setSizeHint(
                    QSize(self.list.viewport().width() or FOLDER_RAIL_WIDTH, FOLDER_ITEM_HEIGHT)
                )
                item.setToolTip(path)
                self.list.addItem(item)
                # Parented at creation (to the view's viewport, the row's final home): a parentless
                # widget is a top-level window until the view adopts it, and this rail is rebuilt
                # while the page is already on screen.
                row = _FolderRow(name, count, self.list.viewport())
                row.setAccessibleName(text)
                self.list.setItemWidget(item, row)
                self._items.append(item)
                self._rows.append(row)
                self._paths.append(path)
            self._labels = labels
        finally:
            self.list.blockSignals(False)
        self._sync_row_widths()

    def set_active(self, active_path) -> None:
        """Select the row for ``active_path`` without re-emitting ``folder_changed``."""
        row = self.row_for_path(active_path)
        self.list.blockSignals(True)
        try:
            self.list.setCurrentRow(row)
        finally:
            self.list.blockSignals(False)
        self._apply_marks(row)

    def row_for_path(self, path) -> int:
        for index, candidate in enumerate(self._paths):
            if config.same_game_folder(candidate, path):
                return index
        return -1

    def current_path(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def rows(self) -> list[_FolderRow]:
        return list(self._rows)

    def item_texts(self) -> list[str]:
        """The accessible text of every row, in display order (name + instance count)."""
        return [str(item.data(Qt.ItemDataRole.AccessibleTextRole) or "") for item in self._items]

    def _apply_marks(self, row: int) -> None:
        for index, widget in enumerate(self._rows):
            widget.set_active(index == row)

    # ---------- events ----------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_row_widths()

    def _sync_row_widths(self) -> None:
        """Match every row's width to the list's viewport, not to the rail's width.

        The rail is 200px wide but the list draws a 1px frame, so its viewport is 198px. The item
        hint used to be the rail's 200px, which put the row 2px past the viewport's right edge - the
        row is the view's child, so the viewport's width is what it may occupy.
        tools/audit_window_widths.py caught the overflow at 1050px.
        """
        width = self.list.viewport().width()
        if width <= 0:
            return
        for item in self._items:
            if item.sizeHint().width() != width:
                item.setSizeHint(QSize(width, FOLDER_ITEM_HEIGHT))

    def _on_current_row_changed(self, row: int) -> None:
        self._apply_marks(row)
        path = self.current_path()
        if path:
            self.folder_changed.emit(path)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        path = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if path:
            self.folder_activated.emit(path)


def add_game_folder_entry(parent: QWidget) -> object | None:
    """Run the shared 「添加文件夹…」 entry; return the new folder, or None when nothing was added.

    This is the same flow the settings page's game-directory card uses: pick a directory, register it
    with ``LauncherConfig.add_game_folder`` (which refuses a duplicate and never switches the current
    folder), save, and report with the same ``settings.folder.*`` messages. The instances page calls
    this and then re-reads its folders, so a folder added here is immediately a rail row.
    """
    cfg, cfg_path = config.load()
    start = str(cfg.game_dir or paths.default_game_dir())
    chosen = QFileDialog.getExistingDirectory(parent, tr("settings.game_dir"), start)
    if not chosen:
        return None
    folder, created = cfg.add_game_folder(chosen)
    if not created:
        set_app_status(parent, tr("settings.folder.duplicate", str(folder.path)), "warning")
        return None
    config.save(cfg, cfg_path)
    set_app_status(parent, tr("settings.folder.added", folder.display_name()))
    return folder


def folder_instance_count(folder_path) -> int:
    """How many instances ``folder_path`` holds (0 when the folder is missing or unreadable)."""
    try:
        found = list_instances(Path(folder_path))
    except (OSError, ValueError):
        return 0
    return len(found)
