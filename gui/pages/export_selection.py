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
"""Per-file selection for the 导出 page: the expandable list under one category.

PCL's page lets a person keep most of a folder and leave one file behind; the category checkboxes
alone cannot express that. This module supplies the two widgets that make it possible without
turning the page into a thousand-control form:

* :class:`ExportCategoryCheck` - a tri-state checkbox whose click still means 全选 / 全不选. The
  partial state is a *summary* of its files, never a state the user can choose by clicking: Qt's
  default tristate cycle (unchecked -> partial -> checked) would make a category click ambiguous.
* :class:`ExportFilePanel` - the expanded body. It builds one row per file **only for the current
  page** (``EXPORT_FILES_PER_PAGE`` rows), so a screenshots folder with a thousand entries costs
  twenty-five checkboxes, not a thousand. Page changes rebuild the rows; the selection itself lives
  in the page (``InstanceDetail``), which is also what a rescan or a loaded preset rewrites.

The panel never decides what is selected. It renders the relative paths it is handed, calls back on
one file or on 全选/全不选 for the whole category, and leaves the source of truth to its owner.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui import i18n
from gui.widgets import ElidedLabel

#: Rows built per page. The export card itself sits in the page's scroll area, so this number is a
#: trade-off between "one long page" and "too many clicks": 25 rows are about one screen, and a
#: thousand screenshots are still only 40 pages.
EXPORT_FILES_PER_PAGE = 25


class ExportCategoryCheck(QCheckBox):
    """A tri-state category checkbox that toggles between 全选 and 全不选 when clicked.

    The middle state is set programmatically from the files below it. Letting Qt cycle into it on a
    click would mean "click a category once" leaves it partially selected without the user touching
    any file, which is not a selection a category row can express.
    """

    def nextCheckState(self) -> None:
        self.setCheckState(
            Qt.CheckState.Unchecked
            if self.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )


class ExportFilePanel(QWidget):
    """The expanded file list under one category: 全选/全不选, a pager and one page of rows."""

    def __init__(
        self,
        category: str,
        size_text: Callable[[int], str],
        on_file_toggled: Callable[[str, bool], None],
        on_bulk_select: Callable[[str, bool], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.category = category
        self._size_text = size_text
        self._on_file_toggled = on_file_toggled
        self._on_bulk_select = on_bulk_select
        self._entries: list[tuple[str, int]] = []
        self._selected: frozenset[str] = frozenset()
        self._page = 1
        #: What the currently built rows were built from. ``refresh`` rebuilds only when it changes,
        #: so ticking one box does not destroy and recreate the page (and its keyboard focus).
        self._rendered_signature: tuple[tuple[str, int], ...] = ()
        self._rows: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        # Indent past the category row's arrow slot (24px) and the category checkbox's own indicator,
        # so a file box sits under the category's label instead of under its arrow.
        layout.setContentsMargins(48, 0, 0, 4)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.all_button = QPushButton("")
        self.all_button.setObjectName("rowAction")
        self.all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_button.clicked.connect(self._toggle_all)
        self.count_label = QLabel("")
        self.count_label.setObjectName("hint")
        self.count_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        header.addWidget(self.all_button)
        header.addWidget(self.count_label)
        header.addStretch(1)

        self.prev_button = QPushButton("‹")
        self.prev_button.setObjectName("rowAction")
        self.prev_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_button.clicked.connect(lambda: self._go(-1))
        self.page_label = QLabel("")
        self.page_label.setObjectName("hint")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_button = QPushButton("›")
        self.next_button.setObjectName("rowAction")
        self.next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_button.clicked.connect(lambda: self._go(1))
        self.prev_button.setAccessibleName(i18n.tr("instances.detail.export.files.prev"))
        self.next_button.setAccessibleName(i18n.tr("instances.detail.export.files.next"))
        self.prev_button.setToolTip(i18n.tr("instances.detail.export.files.prev"))
        self.next_button.setToolTip(i18n.tr("instances.detail.export.files.next"))
        header.addWidget(self.prev_button)
        header.addWidget(self.page_label)
        header.addWidget(self.next_button)
        layout.addLayout(header)

        body = QWidget()
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(2)
        layout.addWidget(body)

        self._update_header()

    # ---------- data ----------

    def set_entries(self, entries: Iterable[tuple[str, int]]) -> None:
        """Replace the category's file list (relative path, bytes); keeps the page when possible."""
        self._entries = [(str(relative), max(0, int(size))) for relative, size in entries]
        self._page = max(1, min(self._page, self._page_count()))

    def refresh(self, selected: Iterable[str]) -> None:
        """Render ``selected``: rebuild the rows if the files changed, else just repaint their state."""
        self._selected = frozenset(selected)
        signature = self._signature()
        if signature != self._rendered_signature:
            self._page = max(1, min(self._page, self._page_count()))
            self._render()
            return
        for relative, check in self._rows.items():
            wanted = relative in self._selected
            if check.isChecked() != wanted:
                check.blockSignals(True)
                check.setChecked(wanted)
                check.blockSignals(False)
        self._update_header()

    def clear(self) -> None:
        """Drop the list and its widgets (an instance switch invalidates both)."""
        self._entries = []
        self._selected = frozenset()
        self._page = 1
        self._rendered_signature = ()
        self._clear_rows()
        self._update_header()

    def checkbox_for(self, relative: str) -> QCheckBox | None:
        """The row checkbox currently built for ``relative`` (None when it is on another page)."""
        return self._rows.get(relative)

    @property
    def page(self) -> int:
        return self._page

    # ---------- rows ----------

    def _signature(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._entries)

    def _page_count(self) -> int:
        total = len(self._entries)
        if total <= EXPORT_FILES_PER_PAGE:
            return 1
        return (total + EXPORT_FILES_PER_PAGE - 1) // EXPORT_FILES_PER_PAGE

    def _clear_rows(self) -> None:
        self._rows.clear()
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _render(self) -> None:
        self._clear_rows()
        start = (self._page - 1) * EXPORT_FILES_PER_PAGE
        page_entries = self._entries[start : start + EXPORT_FILES_PER_PAGE]
        for relative, size in page_entries:
            row = QWidget()
            row.setObjectName("exportFileRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            check = QCheckBox()
            check.setCursor(Qt.CursorShape.PointingHandCursor)
            size_text = self._size_text(size)
            # The visible name is elided; a screen reader gets the whole path and the size, which is
            # exactly the information the two labels paint.
            check.setAccessibleName(f"{relative} · {size_text}")
            check.setToolTip(relative)
            check.setChecked(relative in self._selected)
            check.toggled.connect(partial(self._on_row_toggled, relative))

            name = ElidedLabel(relative, elide_mode=Qt.TextElideMode.ElideMiddle)
            name.setObjectName("exportFileName")
            name.setToolTip(relative)
            value = QLabel(size_text)
            value.setObjectName("hint")
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            value.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

            row_layout.addWidget(check)
            row_layout.addWidget(name, 1)
            row_layout.addWidget(value)
            self._body_layout.addWidget(row)
            self._rows[relative] = check
        self._rendered_signature = self._signature()
        self._update_header()

    def _on_row_toggled(self, relative: str, checked: bool) -> None:
        self._on_file_toggled(relative, bool(checked))

    def _toggle_all(self, _checked: bool = False) -> None:
        total = len(self._entries)
        chosen = sum(1 for relative, _size in self._entries if relative in self._selected)
        self._on_bulk_select(self.category, chosen < total)

    def _go(self, delta: int) -> None:
        page = max(1, min(self._page + delta, self._page_count()))
        if page == self._page:
            return
        self._page = page
        self._render()

    def _update_header(self) -> None:
        total = len(self._entries)
        chosen = sum(1 for relative, _size in self._entries if relative in self._selected)
        self.all_button.setText(
            i18n.tr(
                "instances.detail.export.files.select_none"
                if total and chosen == total
                else "instances.detail.export.files.select_all"
            )
        )
        self.all_button.setAccessibleName(self.all_button.text())
        self.all_button.setEnabled(total > 0)
        self.count_label.setText(
            i18n.tr("instances.detail.export.files.selected", chosen, total)
        )
        pages = self._page_count()
        self.page_label.setText(
            i18n.tr("instances.detail.export.files.page", self._page, pages)
        )
        self.prev_button.setEnabled(self._page > 1)
        self.next_button.setEnabled(self._page < pages)
        # A single page needs no pager, but 全选/全不选 stays: it is the category-wide shortcut.
        for widget in (self.prev_button, self.page_label, self.next_button):
            widget.setVisible(pages > 1)
