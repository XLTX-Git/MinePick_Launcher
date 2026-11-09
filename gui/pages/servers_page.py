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

"""服务器 page: the game's own ``servers.dat``, editable without starting the game.

The file is the game's, not the launcher's - it is where the multiplayer screen keeps its list - so the
page does not invent a second store. It reads ``servers.dat`` every time it is shown, writes the whole
list back after every change, and says so in its empty state.

The page is also a section of the instance detail view. :meth:`ServersPage.set_instance` scopes it to
the instance's effective launch folder (``ResolvedInstance.launch_dir``): an isolated instance has its
own ``servers.dat`` beside its mods and saves, a shared-mode instance uses the game root's file. The
header names the exact file in force, because "which list am I editing" is the one question this page
must never leave ambiguous.

The order is the list's order: the game shows the entries in file order, and 上移/下移 write the new
order straight back. A write is atomic and leaves a ``servers.dat.bak`` copy of the previous file
(``launcher.servers`` explains both); a file that cannot be decoded is reported in a line above the
list and treated as empty, so a corrupt file cannot stop the page from opening.

The favicon is stored *inside* the entry as a base64 data URI, so a row can paint it without a network
request and without a second cache. There is no latency column: a real measurement needs a Server List
Ping implementation (hand-written, UTF-16BE strings, a timeout) and showing a number the launcher did
not measure would be worse than showing none.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, inwindow_dialog, paint_utils, theme
from gui.pages.screenshots_page import CardRow
from gui.widgets import (
    ElidedLabel,
    EmptyState,
    add_page_top_inset,
    set_app_status,
    style_form,
    style_page_layout,
)
from launcher import config, servers

tr = i18n.tr
LOGGER = logging.getLogger(__name__)

ICON_SIZE = 40
ROW_PADDING = 12
ROW_GAP = 12
ROW_HEIGHT = 68
ROW_BUTTON_HEIGHT = 30
FIELD_HEIGHT = 36
#: A favicon is a 64×64 PNG in practice; anything near this is not one and is not worth decoding.
MAX_FAVICON_BYTES = 256 * 1024
_DATA_URI = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,", re.IGNORECASE)


def favicon_pixmap(icon: str, size: int = ICON_SIZE) -> QPixmap | None:
    """Decode the base64 favicon an entry may carry, or None when it has none worth painting."""
    match = _DATA_URI.match(icon or "")
    if match is None:
        return None
    payload = icon[match.end() :]
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > MAX_FAVICON_BYTES:
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(raw):
        return None
    if pixmap.width() > size or pixmap.height() > size:
        pixmap = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


class _Favicon(QWidget):
    """The 40px square left of an entry: the game's favicon, or a letter tile when there is none."""

    def __init__(self, name: str, icon: str, size: int = ICON_SIZE) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self._size = size
        self._pixmap = favicon_pixmap(icon, size)
        if self._pixmap is None:
            self._pixmap = paint_utils.letter_tile_pixmap((name or "?").strip()[:1] or "?", size, 8)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 8, 8)
        painter.setClipPath(clip)
        if self._pixmap is not None:
            ratio = max(1.0, self._pixmap.devicePixelRatio())
            width = self._pixmap.width() / ratio
            height = self._pixmap.height() / ratio
            painter.drawPixmap(
                int((self._size - width) / 2.0),
                int((self._size - height) / 2.0),
                self._pixmap,
            )
        painter.end()


class ServerRow(CardRow):
    """One server: favicon, name, address, and the five things the list can do with it."""

    HEIGHT = ROW_HEIGHT

    def __init__(
        self,
        entry: servers.ServerEntry,
        index: int,
        count: int,
        *,
        on_up,
        on_down,
        on_edit,
        on_copy,
        on_delete,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry = entry
        self.index = index
        self.favicon = _Favicon(entry.name, entry.icon)
        self.name_label = ElidedLabel(entry.name or entry.ip, elide_mode=Qt.TextElideMode.ElideRight)
        name_font = QFont(self.font())
        name_font.setPixelSize(13)
        name_font.setWeight(QFont.Weight.DemiBold)
        self.name_label.setFont(name_font)
        self.address_label = ElidedLabel(entry.ip, elide_mode=Qt.TextElideMode.ElideMiddle)
        self.address_label.setObjectName("hint")
        address_font = QFont(self.font())
        address_font.setPixelSize(12)
        self.address_label.setFont(address_font)
        self.setToolTip((entry.name + "\n" if entry.name else "") + entry.ip)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        text.addWidget(self.name_label)
        text.addWidget(self.address_label)

        subject = entry.name or entry.ip
        self.up_button = self._action(tr("servers.up"), subject, on_up)
        self.down_button = self._action(tr("servers.down"), subject, on_down)
        self.edit_button = self._action(tr("servers.edit"), subject, on_edit)
        self.copy_button = self._action(tr("servers.copy"), subject, on_copy)
        self.delete_button = self._action(
            tr("instances.delete"), subject, on_delete, object_name="rowDanger"
        )
        # A move that cannot happen is not offered as an action that does nothing.
        self.up_button.setEnabled(index > 0)
        self.down_button.setEnabled(index < count - 1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(ROW_PADDING, ROW_PADDING, ROW_PADDING, ROW_PADDING)
        layout.setSpacing(ROW_GAP)
        layout.addWidget(self.favicon, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text, 1)
        for button in (
            self.up_button,
            self.down_button,
            self.edit_button,
            self.copy_button,
            self.delete_button,
        ):
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(self.HEIGHT)

    @staticmethod
    def _action(text: str, subject: str, callback, *, object_name: str = "rowInstall") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setFixedHeight(ROW_BUTTON_HEIGHT)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        name = f"{text} · {subject}"
        button.setAccessibleName(name)
        button.setToolTip(name)
        button.clicked.connect(lambda _checked=False: callback())
        return button


class ServersPage(QWidget):
    """The game's server list, with add/edit/delete/reorder that write ``servers.dat`` back."""

    def __init__(self, path: Path | str | None = None, *, embedded: bool = False) -> None:
        super().__init__()
        self.setObjectName("serversPage")
        self.setAccessibleName(tr("nav.servers"))
        self._override_path = Path(path) if path is not None else None
        self._file = servers.ServersFile()
        self._rows: list[ServerRow] = []
        self._editing: int | None = None
        #: True inside the instance detail page: the file comes from the shown instance, not from the
        #: global game folder, and nothing is read until the detail names that instance.
        self._embedded = embedded
        #: (instance id, name, launch dir, isolated) once set_instance has run.
        self._scope: tuple[str, str, Path, bool] | None = None

        self.count_label = QLabel("")
        self.count_label.setObjectName("hint")
        self.file_label = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideMiddle)
        self.file_label.setObjectName("hint")
        self.scope_label = QLabel("")
        self.scope_label.setObjectName("hint")
        self.scope_label.setWordWrap(True)
        self.file_row = QWidget()
        file_row = QHBoxLayout(self.file_row)
        file_row.setContentsMargins(0, 0, 0, 0)
        file_row.setSpacing(8)
        file_row.addWidget(self.file_label, 1)
        self.file_row.setVisible(False)
        self.refresh_button = QPushButton(tr("java.refresh"))
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setFixedHeight(32)
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button = QPushButton(tr("servers.add"))
        self.add_button.setFixedHeight(32)
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        header_layout.addWidget(self.count_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.refresh_button)
        header_layout.addWidget(self.add_button)

        self.read_error = QLabel("")
        self.read_error.setObjectName("warning")
        self.read_error.setWordWrap(True)
        self.read_error.setVisible(False)

        self.form = QFrame()
        self.form.setObjectName("pageCard")
        form_layout = QVBoxLayout(self.form)
        form_layout.setContentsMargins(16, 12, 16, 12)
        form_layout.setSpacing(8)
        fields = QFormLayout()
        style_form(fields)
        self.name_edit = QLineEdit()
        self.name_edit.setFixedHeight(FIELD_HEIGHT)
        self.name_edit.setPlaceholderText(tr("servers.name_placeholder"))
        self.address_edit = QLineEdit()
        self.address_edit.setFixedHeight(FIELD_HEIGHT)
        self.address_edit.setPlaceholderText(tr("servers.address_placeholder"))
        # QFormLayout passes the label on to the field as its accessible name (WCAG 4.1.2).
        fields.addRow(QLabel(tr("servers.name")), self.name_edit)
        fields.addRow(QLabel(tr("servers.address")), self.address_edit)
        form_layout.addLayout(fields)
        form_buttons = QHBoxLayout()
        form_buttons.setSpacing(8)
        form_buttons.addStretch(1)
        self.cancel_button = QPushButton(tr("dialog.cancel"))
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.setFixedHeight(32)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button = QPushButton(tr("servers.save"))
        self.save_button.setFixedHeight(32)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        form_buttons.addWidget(self.cancel_button)
        form_buttons.addWidget(self.save_button)
        form_layout.addLayout(form_buttons)
        self.form.setVisible(False)

        self.empty = EmptyState(tr("servers.empty") + "\n" + tr("servers.empty_hint"))
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("serversScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.rows_host)
        self.scroll.setAccessibleName(tr("a11y.servers"))
        self.stack = QStackedWidget()
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(self.empty)

        layout = QVBoxLayout(self)
        style_page_layout(layout)
        add_page_top_inset(layout)
        if embedded:
            # The instance detail's sections own the content edge to edge (see the mods page there).
            layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(header)
        layout.addWidget(self.file_row)
        layout.addWidget(self.scope_label)
        layout.addWidget(self.read_error)
        layout.addWidget(self.form)
        layout.addWidget(self.stack, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.add_button.clicked.connect(self.begin_add)
        self.cancel_button.clicked.connect(self.cancel_form)
        self.save_button.clicked.connect(self.save_form)
        self.name_edit.returnPressed.connect(self.save_form)
        self.address_edit.returnPressed.connect(self.save_form)
        theme.register_self_styling(self)
        if not embedded:
            self.refresh()

    # ---------- instance scope ----------

    def set_instance(
        self,
        instance_id: str,
        instance_name: str,
        launch_dir: Path | str,
        isolated: bool,
    ) -> None:
        """Scope the page to the ``servers.dat`` of one instance's effective launch folder."""
        self._scope = (instance_id, instance_name, Path(launch_dir), bool(isolated))
        self.file_row.setVisible(True)
        self.scope_label.setText(
            tr("instances.detail.servers.scope.isolated")
            if isolated
            else tr("instances.detail.servers.scope.shared")
        )
        self._sync_file_label()
        if self.isVisible():
            self.refresh()

    def clear_instance(self) -> None:
        """Drop the scope and the rows (the detail page is being cleared)."""
        self._scope = None
        self._file = servers.ServersFile()
        self._editing = None
        self.form.setVisible(False)
        self.file_row.setVisible(False)
        self.scope_label.setText("")
        self.read_error.setVisible(False)
        self._rebuild()

    def has_scope(self) -> bool:
        return self._scope is not None

    def _sync_file_label(self) -> None:
        path = self.file_path()
        self.file_label.setText(tr("instances.detail.servers.file", str(path)))
        self.file_label.setToolTip(str(path))

    def on_theme_applied(self) -> None:
        self.refresh()

    # ---------- data ----------

    def file_path(self) -> Path:
        """The ``servers.dat`` this page edits.

        A test's explicit path wins; then the shown instance's launch folder (isolated: its own
        folder, shared: the game root); then the current game folder's file.
        """
        if self._override_path is not None:
            return self._override_path
        if self._scope is not None:
            return servers.servers_path(self._scope[2])
        return servers.servers_path(config.active_game_dir())

    def entries(self) -> list[servers.ServerEntry]:
        return list(self._file.entries)

    def rows(self) -> list[ServerRow]:
        return list(self._rows)

    def editing_index(self) -> int | None:
        return self._editing

    def refresh(self) -> None:
        """Read the file again and rebuild the list; a broken file is a message, never an exception."""
        if self._embedded and self._scope is None:
            return  # the detail has not named an instance yet; never edit the global file here
        path = self.file_path()
        self._sync_file_label()
        try:
            self._file = servers.read_servers(path)
        except servers.ServersDatError as exc:
            LOGGER.warning("servers.dat: %s", exc)
            self._file = servers.ServersFile()
            self.read_error.setText(tr("servers.read_failed", str(exc)))
            self.read_error.setVisible(True)
        else:
            self.read_error.setVisible(False)
        self._rebuild()

    def _rebuild(self) -> None:
        for row in self._rows:
            self.rows_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        count = len(self._file.entries)
        for index, entry in enumerate(self._file.entries):
            row = ServerRow(
                entry,
                index,
                count,
                on_up=lambda row_index=index: self.move(row_index, -1),
                on_down=lambda row_index=index: self.move(row_index, 1),
                on_edit=lambda row_index=index: self.begin_edit(row_index),
                on_copy=lambda row_index=index: self.copy_address(row_index),
                on_delete=lambda row_index=index: self.delete_entry(row_index),
            )
            self.rows_layout.addWidget(row)
            self._rows.append(row)
        self.rows_layout.addStretch(1)
        self.count_label.setText(tr("servers.count", count))
        self.stack.setCurrentIndex(0 if count else 1)

    # ---------- writing ----------

    def _commit(self, entries: list[servers.ServerEntry]) -> bool:
        """Write the list, then re-read it; returns False and reports the error when it did not stick."""
        data = servers.ServersFile(
            entries=list(entries),
            hidden=self._file.hidden,
            extras=dict(self._file.extras),
        )
        try:
            servers.write_servers(self.file_path(), data)
        except servers.ServersDatError as exc:
            set_app_status(self, tr("servers.write_failed", str(exc)), "error")
            self.refresh()  # the disk is the truth after a failed write
            return False
        self._file = data
        self.refresh()
        return True

    # ---------- actions ----------

    def begin_add(self) -> None:
        """Open the form empty, for a new entry."""
        self._editing = None
        self.name_edit.clear()
        self.address_edit.clear()
        self.form.setVisible(True)
        self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def begin_edit(self, index: int) -> None:
        """Open the form filled with the entry at ``index``; its icon is kept when saving."""
        if not 0 <= index < len(self._file.entries):
            return
        self._editing = index
        entry = self._file.entries[index]
        self.name_edit.setText(entry.name)
        self.address_edit.setText(entry.ip)
        self.form.setVisible(True)
        self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_edit.selectAll()

    def cancel_form(self) -> None:
        self._editing = None
        self.form.setVisible(False)
        self.add_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def save_form(self) -> bool:
        """Validate, then add or update one entry and write the file."""
        name = self.name_edit.text().strip()
        address = self.address_edit.text().strip()
        if not name or not address:
            set_app_status(self, tr("servers.invalid"), "warning")
            (self.name_edit if not name else self.address_edit).setFocus(
                Qt.FocusReason.OtherFocusReason
            )
            return False
        entries = list(self._file.entries)
        if self._editing is None:
            entries.append(servers.ServerEntry(name=name, ip=address))
        elif 0 <= self._editing < len(entries):
            entries[self._editing] = entries[self._editing].renamed(name, address)
        else:  # the list changed under the form (the file was reloaded): add instead of losing it
            self._editing = None
            entries.append(servers.ServerEntry(name=name, ip=address))
        if not self._commit(entries):
            return False
        self._editing = None
        self.form.setVisible(False)
        set_app_status(self, tr("servers.saved", name))
        return True

    def move(self, index: int, delta: int) -> bool:
        """Move one entry up or down and write the new order back."""
        moved = servers.move_entry(self._file.entries, index, delta)
        if moved == self._file.entries:
            return False
        return self._commit(moved)

    def copy_address(self, index: int) -> bool:
        if not 0 <= index < len(self._file.entries):
            return False
        address = self._file.entries[index].ip
        QApplication.clipboard().setText(address)
        set_app_status(self, tr("servers.copied", address))
        return True

    def delete_entry(self, index: int) -> bool:
        """Ask, then remove one entry and write the file. The default answer is "keep"."""
        if not 0 <= index < len(self._file.entries):
            return False
        entry = self._file.entries[index]
        outcome = inwindow_dialog.confirm(
            self,
            tr("servers.delete.title"),
            tr("servers.delete.message", entry.name or entry.ip),
            accept_text=tr("instances.delete"),
            reject_text=tr("dialog.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="question",
        )
        if outcome is not inwindow_dialog.Outcome.ACCEPTED:
            return False
        remaining = [item for position, item in enumerate(self._file.entries) if position != index]
        if not self._commit(remaining):
            return False
        set_app_status(self, tr("servers.deleted", entry.name or entry.ip))
        return True

    # ---------- lifecycle ----------

    def showEvent(self, event) -> None:
        """The game can edit servers.dat while the launcher is open: re-read on every visit.

        An open form is left alone: its text is unsaved user input, and reloading the list under it
        would leave the form's index pointing at a different entry.
        """
        super().showEvent(event)
        if (not self._embedded or self._scope is not None) and self._editing is None:
            self.refresh()
