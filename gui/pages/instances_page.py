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

"""Instances page: a pure, responsive card grid. Opening a card drills into the detail page.

An instance *is* a version folder (`versions/<id>/`), so this page owns the
grid, the search box and the cross-instance actions. Everything that acts on
one instance lives in @gui.pages.instance_detail.InstanceDetail; the card grid
never embeds it, and the detail page is shown only after a deliberate drill-down.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, nav_icons, theme
from gui.pages.instance_detail import InstanceDetailPage
from gui.widgets import (
    ElidedLabel,
    EmptyState,
    add_page_top_inset,
    add_search_icon,
    apply_no_focus_outline,
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

tr = i18n.tr

CARD_HEIGHT = 156
CARD_GAP = 12
CARD_MIN_WIDTH = 260
CARD_MAX_COLUMNS = 3
# Three cards plus their four 12px gutters. Stated as a floor so the window never shrinks into a
# one-card strip that would contradict the 2-3 column layout; tests/test_window_clipping.py also
# uses the resulting floor to prove it still exceeds the old 1000px fixed pin.
CARD_GRID_MIN_WIDTH = 860
CARD_COLUMNS_MIN_WIDTH = CARD_GRID_MIN_WIDTH


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


class _InstanceCard(QPushButton):
    """One white card. The whole surface is the button; 管理 is its visual affordance, not a fake action."""

    def __init__(
        self,
        instance,
        meta: str,
        counts: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.instance_id = instance.id
        self.setObjectName("instanceCard")
        self.setAccessibleName(tr("instances.card.open", instance.name))
        description = meta + (" · " + counts if counts else "")
        self.setAccessibleDescription(description)
        self.setToolTip(f"{instance.name}\n{description}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

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
        tokens = theme.current_tokens() or theme.theme_palette("light")
        icon.setPixmap(
            nav_icons.nav_icon("instances", tokens["ACCENT"], 22).pixmap(22, 22)
        )
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # The card button is the one focus stop; its text is painted by child labels, not controls.
        # ElidedLabel enables text selection, which also gives it ClickFocus - so each name/meta line
        # entered the focus chain as a second stop with no :focus rule (the keyboard walkthrough
        # flagged it). Mouse events already pass through to the card (WA_TransparentForMouseEvents),
        # so selecting this text was unreachable anyway. Turn focus off on exactly these two card
        # labels; keep the class default for the detail page's path/ID labels, which stay selectable.
        name = ElidedLabel(instance.name)
        name.setObjectName("instanceCardName")
        name.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        top.addWidget(icon)
        top.addWidget(name, 1)
        layout.addLayout(top)

        meta_label = ElidedLabel(meta)
        meta_label.setObjectName("instanceCardMeta")
        meta_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(meta_label)
        layout.addStretch(1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        counts_label = QLabel(counts)
        counts_label.setObjectName("instanceCardCounts")
        counts_label.setWordWrap(False)
        counts_label.setVisible(bool(counts))
        counts_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        manage = QLabel(tr("instances.card.manage"))
        manage.setObjectName("instanceCardManage")
        manage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        manage.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        bottom.addWidget(counts_label)
        bottom.addStretch(1)
        bottom.addWidget(manage)
        layout.addLayout(bottom)

        self.manage_label = manage


class _CardListWidget(QListWidget):
    """Icon-mode grid that reports viewport width changes so the column count can follow the window."""

    resized = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setUniformItemSizes(True)
        # The card gap lives in the grid cell (item width = cell - gap), not in QListView's spacing:
        # spacing is applied asymmetrically at column boundaries, which made 3-column rows wrap a few
        # pixels early. With an explicit grid cell the column count is exactly viewport // cell.
        self.setSpacing(0)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        # The card buttons are the keyboard stops. A second focus stop around the whole grid would
        # announce the same content twice.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()


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
    """Card grid, plus the drill-down detail page it reveals on request."""

    #: A card (or the 管理 affordance drawn on it) asks the window to open the detail page.
    instance_open_requested = Signal(str)
    #: The detail page's back control asks to return to the grid.
    back_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("instancesPage")

        self.search_edit = QLineEdit()
        add_search_icon(self.search_edit)
        self.search_edit.setPlaceholderText(tr("instances.search.placeholder"))
        self.search_edit.setAccessibleName(tr("instances.search.placeholder"))  # WCAG 4.1.2
        self.search_edit.setClearButtonEnabled(True)

        self.list = _CardListWidget()
        self.list.setObjectName("instanceCardList")
        self.list.setAccessibleName(tr("nav.instances"))  # WCAG 4.1.2
        self.list.setMinimumWidth(CARD_GRID_MIN_WIDTH)
        apply_no_focus_outline(self.list)
        self.empty_instances = EmptyState(tr("empty.instances"))
        self.empty_search = EmptyState(tr("instances.search.none"))
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.list)
        self.list_stack.addWidget(self.empty_instances)
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

        first_row = QHBoxLayout()
        first_row.setContentsMargins(0, 0, 0, 0)
        first_row.setSpacing(8)
        sort_label = QLabel(tr("instances.sort.label"))
        sort_label.setBuddy(self.sort_combo)
        first_row.addWidget(self.search_edit, 1)
        first_row.addWidget(sort_label)
        first_row.addWidget(self.sort_combo)
        browser_layout.addLayout(first_row)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addStretch(1)
        actions.addWidget(self.create_button)
        actions.addWidget(self.import_button)
        browser_layout.addLayout(actions)
        browser_layout.addWidget(self.list_stack, 1)

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
        self.list.currentRowChanged.connect(self._on_selection_changed)
        self.list.resized.connect(self._relayout_cards)
        self.detail.changed.connect(self._on_detail_changed)
        self.detail.deleted.connect(self._on_detail_deleted)
        self.detail_page.back_requested.connect(self._on_back_clicked)
        self._card_size = QSize(0, 0)
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
        return True

    def show_list(self) -> None:
        """Return to the card grid, putting keyboard focus back on the current card."""
        self.view_stack.setCurrentWidget(self.view_stack.widget(0))
        item = self.list.currentItem()
        card = self.list.itemWidget(item) if item is not None else None
        if card is not None:
            card.setFocus(Qt.FocusReason.OtherFocusReason)

    def current_view(self) -> QWidget:
        return self.view_stack.currentWidget()

    def _on_back_clicked(self) -> None:
        self.show_list()
        self.back_requested.emit()

    def _open_requested(self, instance_id: str) -> None:
        self.instance_open_requested.emit(instance_id)

    # ---------- list ----------

    def refresh(self, select: str | None = None) -> None:
        """Rebuild the card grid; `select` forces a selection (None keeps the current one)."""
        wanted = select if select is not None else self._current_name()
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()
        instances = list_instances(game_dir)
        self._instances = instances
        ordered = sorted(instances.values(), key=self._sort_key)
        needle = self.search_edit.text().strip().lower()
        self.list.blockSignals(True)
        try:
            self.list.clear()
            shown = 0
            for inst in ordered:
                if needle and needle not in self._haystack(inst):
                    continue
                item = QListWidgetItem(self._row_text(inst))
                item.setData(Qt.UserRole, inst.id)  # instance id in a data role, never parsed
                item.setToolTip(str(instance_dir(game_dir, inst.id)))
                item.setSizeHint(QSize(self._card_size.width() or CARD_MIN_WIDTH, CARD_HEIGHT))
                self.list.addItem(item)
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
                    inst, f"{version} · {loader}", counts, parent=self.list
                )
                card.clicked.connect(partial(self._open_requested, inst.id))
                self.list.setItemWidget(item, card)
                shown += 1
        finally:
            self.list.blockSignals(False)
        self._update_list_state(len(instances), self.list.count())
        self._relayout_cards()
        self._select(wanted)
        if not instances:
            self.show_list()

    def _relayout_cards(self) -> None:
        width = self.list.viewport().width()
        if width <= 0 or self.list.count() == 0:
            return
        columns = CARD_MAX_COLUMNS if width >= CARD_COLUMNS_MIN_WIDTH else 2
        # Two 12px page gutters plus one 12px gap between each pair of columns. Dividing the *inner*
        # width (not the viewport) is what makes a 3-column row fit instead of wrapping the last card.
        cell_width = max(
            CARD_MIN_WIDTH + CARD_GAP, (width - 2 * CARD_GAP) // columns
        )
        card_width = cell_width - CARD_GAP
        self.list.setGridSize(QSize(cell_width, CARD_HEIGHT + CARD_GAP))
        size = QSize(card_width, CARD_HEIGHT)
        if size == self._card_size:
            return
        self._card_size = size
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item is not None:
                item.setSizeHint(size)
        self.list.viewport().update()

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

    def _update_list_state(self, total: int, shown: int) -> None:
        if total == 0:
            self.list_stack.setCurrentWidget(self.empty_instances)
            self.empty_instances.update_for(0)
            return
        if shown == 0:
            self.list_stack.setCurrentWidget(self.empty_search)
            self.empty_search.update_for(0)
            return
        self.list_stack.setCurrentWidget(self.list)

    def _select(self, instance_id: str | None) -> None:
        """Select a card by instance id (no selection when it is gone)."""
        row = -1
        if instance_id:
            for index in range(self.list.count()):
                if self.list.item(index).data(Qt.UserRole) == instance_id:
                    row = index
                    break
        self.list.setCurrentRow(row)  # -1 when the instance disappeared: shows the hint panel
        self._on_selection_changed(row)

    def select_instance(self, instance_id: str) -> bool:
        """Show one instance in the selection model (used by the versions/launch pages)."""
        self.refresh(select=instance_id)
        found = self._current_name() == instance_id
        if not found:
            set_app_status(self, tr("instances.msg.not_found", instance_id), "warning")
        return found

    def _current_name(self) -> str | None:
        item = self.list.currentItem()
        if item is None:
            return None
        instance_id = item.data(Qt.UserRole)
        return instance_id if instance_id else None

    def _on_search_changed(self) -> None:
        self.refresh()

    def _on_selection_changed(self, _row: int) -> None:
        instance_id = self._current_name()
        if instance_id is None:
            self.detail_page.set_instance(None)
            return
        self.detail_page.set_instance(instance_id)

    def _on_detail_changed(self) -> None:
        """Metadata changed in the detail: rebuild the grid, keeping the shown instance."""
        self.refresh(select=self.detail.instance_id or self._current_name())

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
