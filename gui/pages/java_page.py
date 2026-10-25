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

"""Java page: managed runtime management (delete), detected list, install a new JRE.

Both runtime lists are white cards holding dense rows (48px major-version tile, "Java 21" on the main
line, path on a 12px hint line, the row's action on the right) instead of two tables: the page asks
three questions - what the launcher downloaded, what the system already has, and how to add one - and a
table made each of them look like a data dump.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, paint_utils
from gui.widgets import (
    EmptyState,
    add_page_header,
    apply_no_focus_outline,
    disable_keeping_focus,
    set_app_status,
    style_page_layout,
    widget_alive,
)
from gui.workers import ProgressBridge, run_in_background
from launcher import paths
from launcher.java import install_java, list_java
from launcher.java.install import delete_managed_runtime, list_managed_runtimes

tr = i18n.tr

#: How a detected runtime's provider token reads in the UI. The tokens come from launcher.java.locate
#: (managed / config / java_home / path / system); the two environment-variable names stay as they are.
_PROVIDER_KEYS = {
    "managed": "java.provider.managed",
    "config": "java.provider.config",
    "java_home": "java.provider.java_home",
    "path": "java.provider.path",
    "system": "java.provider.system",
}


def provider_label(token: str) -> str:
    """The display name of a runtime's source; an unknown token is shown as the probe reported it."""
    key = _PROVIDER_KEYS.get((token or "").strip().lower())
    return tr(key) if key else (token or "")


def _enable_if_alive(button: QPushButton | None) -> None:
    """Re-enable a row button that may already have been rebuilt by refresh()."""
    if button is not None and widget_alive(button):
        button.setEnabled(True)


def _dir_size(path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total


def _format_size(size: int) -> str:
    if size >= 1 << 30:
        return f"{size / (1 << 30):.2f} {tr('unit.gb')}"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} {tr('unit.mb')}"
    if size >= 1 << 10:
        return f"{size / (1 << 10):.1f} {tr('unit.kb')}"
    return f"{size} {tr('unit.bytes')}"


_TILE_CACHE: dict[tuple[int, int, str, str], QPixmap] = {}


def major_tile(major: int, size: int = 48) -> QPixmap:
    """A rounded tile carrying the Java major version, drawn from the theme's accent tint.

    The launcher ships no runtime artwork, and every runtime would otherwise share one identical
    letter tile: the number is the one thing that tells two of them apart at a glance.
    """
    tokens = paint_utils._tokens()
    background = tokens.get("ACCENT_TINT", "#e6f4ec")
    foreground = tokens.get("ACCENT_BUTTON", tokens.get("ACCENT", "#2f9e6f"))
    key = (major, size, background, foreground)
    cached = _TILE_CACHE.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(background))
    painter.drawRoundedRect(QRectF(0, 0, size, size), 8, 8)
    font = QFont()
    font.setPixelSize(max(12, round(size * 0.34)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(foreground))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, str(major))
    painter.end()
    _TILE_CACHE[key] = pixmap
    return pixmap


class _JavaRow(QWidget):
    """One dense runtime row inside a white card: major-version tile, name + tag, path, action.

    The row paints itself (like the version cards), so the path can elide on one line and a list of
    runtimes costs one widget per row instead of a label tree. It sits inside a shared card, so it
    paints no card of its own: only a hairline between two rows, a hover tint and a selection tint.
    """

    HEIGHT = 68
    ICON = 48
    GAP = 12
    TAG_HEIGHT = 18

    def __init__(
        self,
        major: int,
        title: str,
        tag: str,
        meta: str,
        *,
        action: QPushButton | None = None,
        on_select=None,
        separator: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.major = major
        self.title = title
        self.tag = tag
        self.meta = meta
        self._on_select = on_select
        self._separator = separator
        self._selected = False
        self._hover = False
        self._icon = major_tile(major, self.ICON)
        self.action = action
        self.setFixedHeight(self.HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addStretch(1)
        if action is not None:
            layout.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip(f"{title}\n{meta}")

    def text_width(self) -> int:
        """Width available to the title and the path line."""
        text_left = self.ICON + self.GAP
        right = self.action.x() if self.action is not None else self.width()
        return max(0, right - self.GAP - text_left)

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()
        if self.action is not None:
            policy = Qt.FocusPolicy.StrongFocus if selected else Qt.FocusPolicy.ClickFocus
            if self.action.focusPolicy() != policy:
                self.action.setFocusPolicy(policy)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._on_select is not None:
                self._on_select()
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        tokens = paint_utils._tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # The list paints its item text behind the item widget; an opaque fill is what keeps that text
        # (the accessible representation) from showing through the transparent row.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.get("BG_LIST", "#ffffff")))
        painter.drawRect(self.rect())
        if self._selected or self._hover:
            background = QColor(
                tokens.get("ACCENT_TINT" if self._selected else "BG_SIDEBAR", "#eef2f6")
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRect(self.rect())
        if self._separator:
            pen = QPen(QColor(tokens.get("BG_SEPARATOR", "#e6ebf1")))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

        icon_rect = QRectF(0, (self.HEIGHT - self.ICON) / 2.0, self.ICON, self.ICON)
        painter.drawPixmap(icon_rect.toRect(), self._icon)

        text_left = self.ICON + self.GAP
        text_right = self.action.x() - self.GAP if self.action is not None else self.width() - 8
        width = max(0, int(text_right - text_left))
        if width <= 0:
            painter.end()
            return

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        title_font = QFont(self.font())
        title_font.setPixelSize(15)
        title_font.setWeight(QFont.Weight.DemiBold)
        title_metrics = QFontMetrics(title_font)
        tag_font = QFont(self.font())
        tag_font.setPixelSize(11)
        tag_metrics = QFontMetrics(tag_font)
        tag_width = min(width, tag_metrics.horizontalAdvance(self.tag) + 12)

        painter.setFont(title_font)
        painter.setPen(text_color)
        title = title_metrics.elidedText(self.title, Qt.TextElideMode.ElideRight, width)
        title_width = title_metrics.horizontalAdvance(title)
        painter.drawText(
            QRectF(text_left, 15, width, 22),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title,
        )
        if self.tag and text_left + title_width + 8 + tag_width <= text_right:
            tag_rect = QRectF(text_left + title_width + 8, 17, tag_width, self.TAG_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("BG_SECTION", "#f1f3f6")))
            painter.drawRoundedRect(tag_rect, 4, 4)
            painter.setFont(tag_font)
            painter.setPen(hint_color)
            painter.drawText(
                tag_rect,
                int(Qt.AlignmentFlag.AlignCenter),
                tag_metrics.elidedText(self.tag, Qt.TextElideMode.ElideRight, int(tag_width) - 12),
            )

        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        meta_metrics = QFontMetrics(meta_font)
        painter.setFont(meta_font)
        painter.setPen(hint_color)
        painter.drawText(
            QRectF(text_left, 37, width, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            meta_metrics.elidedText(self.meta, Qt.TextElideMode.ElideMiddle, width),
        )
        painter.end()


class JavaPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        # Scopes the light sheet's card + dense-row rules to this page.
        self.setObjectName("javaPage")
        self._managed: dict[int, object] = {}
        self.rows: list[_JavaRow] = []
        self.detected_rows: list[_JavaRow] = []

        self.managed_list = QListWidget()
        self.managed_list.setObjectName("javaManagedList")
        # WCAG 4.1.2: the two lists are focus stops with no text of their own, and they list different
        # things, so each carries its own section name instead of the old shared "Java runtimes" name.
        self.managed_list.setAccessibleName(tr("java.managed.label"))
        apply_no_focus_outline(self.managed_list)
        self.managed_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.managed_list.setMinimumHeight(2 * _JavaRow.HEIGHT)
        self.managed_list.currentRowChanged.connect(self._sync_managed_selection)

        self.detected_list = QListWidget()
        self.detected_list.setObjectName("javaDetectedList")
        self.detected_list.setAccessibleName(tr("java.detected.label"))
        apply_no_focus_outline(self.detected_list)
        self.detected_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detected_list.setMinimumHeight(2 * _JavaRow.HEIGHT)
        self.detected_list.currentRowChanged.connect(self._sync_detected_selection)

        self.refresh_button = QPushButton(tr("java.refresh"))
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setFixedHeight(32)
        self.major_combo = QComboBox()
        for major in (8, 11, 17, 21, 25):
            self.major_combo.addItem(str(major), major)
        self.major_combo.setFixedHeight(32)  # the design system's field metric
        # WCAG 4.1.2: its "major version" label is a plain QLabel in the same row layout, so the combo
        # never received it as a name (only a QFormLayout row passes the label on automatically).
        self.major_combo.setAccessibleName(tr("java.install.major"))
        self.install_button = QPushButton(tr("java.install.button"))
        self.install_button.setFixedHeight(32)

        self.empty_managed = EmptyState(tr("empty.java"))
        self.empty_detected = EmptyState(tr("empty.java"))

        self.managed_card, self.managed_stack = self._card(
            tr("java.managed.label"),
            self.managed_list,
            self.empty_managed,
        )
        self.detected_card, self.detected_stack = self._card(
            tr("java.detected.label"),
            self.detected_list,
            self.empty_detected,
            header_action=self.refresh_button,
        )

        install_card = QFrame()
        install_card.setObjectName("pageCard")
        install_layout = QHBoxLayout(install_card)
        install_layout.setContentsMargins(12, 8, 12, 8)
        install_layout.setSpacing(8)
        install_title = QLabel(tr("java.install.label"))
        install_title.setObjectName("formLabel")
        install_layout.addWidget(install_title)
        install_layout.addSpacing(8)
        install_layout.addWidget(self.major_combo)
        install_layout.addWidget(self.install_button)
        install_layout.addStretch(1)

        layout = QVBoxLayout(self)
        style_page_layout(layout)
        add_page_header(layout, tr("nav.java"), tr("page.java.desc"))
        layout.addWidget(self.managed_card, 1)
        layout.addWidget(self.detected_card, 1)
        layout.addWidget(install_card)

        self.refresh_button.clicked.connect(self.refresh)
        self.install_button.clicked.connect(self._install)
        self.refresh()

    # ---------- construction ----------

    def _card(
        self,
        title: str,
        body: QWidget,
        empty: QLabel,
        *,
        header_action: QWidget | None = None,
    ) -> tuple[QFrame, QStackedWidget]:
        """One white card: a 15px section title (plus an optional header action) above its list.

        The empty state is a *page* of a stack, not a line under the list: an empty runtime list then
        reads as "this card has no rows", centred in the card, instead of leaving a blank list box with
        a message pinned to its bottom edge.
        """
        card = QFrame()
        card.setObjectName("pageCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        header.addWidget(heading)
        header.addStretch(1)
        if header_action is not None:
            header.addWidget(header_action)
        layout.addLayout(header)
        stack = QStackedWidget()
        stack.addWidget(body)
        stack.addWidget(empty)
        layout.addWidget(stack, 1)
        return card, stack

    @staticmethod
    def _show_empty(stack: QStackedWidget, empty: QLabel, count: int) -> None:
        """Switch the card between its list and its "nothing here" message."""
        empty.update_for(count)
        stack.setCurrentIndex(1 if count == 0 else 0)

    # ---------- data ----------

    def refresh(self) -> None:
        self._refresh_managed()
        # Probing java -version is slow, so only the *probe* runs on a worker thread. Filling
        # the list happens in the result callback, which Qt delivers on the GUI thread —
        # touching widgets from the worker corrupted Qt's state and crashed the process.
        run_in_background(
            list_java,
            probe_dir=paths.launcher_dir() / "cache",
            on_result=self._apply_detected,
            on_error=lambda _m: None,
        )

    def _refresh_managed(self) -> None:
        items = list_managed_runtimes()
        self._managed = {major: d for major, d in items}
        self._fill_managed(items)
        self._show_empty(self.managed_stack, self.empty_managed, len(items))

    def _fill_managed(self, items: list[tuple[int, object]]) -> None:
        self.rows = []
        self.managed_list.blockSignals(True)
        self.managed_list.clear()
        for index, (major, directory) in enumerate(items):
            size = _dir_size(directory)
            button = QPushButton(tr("java.delete"))
            button.setObjectName("rowDanger")
            button.setFixedHeight(32)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A column of buttons that all read "Delete" is useless with a screen reader.
            button.setAccessibleName(tr("java.delete") + " · Java " + str(major))
            button.clicked.connect(
                lambda _checked=False, row=index: self._delete_row(row)
            )
            row = _JavaRow(
                major,
                "Java " + str(major),
                "",
                f"{directory} · {_format_size(size)}",
                action=button,
                on_select=lambda row=index: self.managed_list.setCurrentRow(row),
                separator=index < len(items) - 1,
            )
            item = QListWidgetItem("Java " + str(major))
            item.setData(Qt.UserRole, major)
            item.setSizeHint(QSize(0, _JavaRow.HEIGHT))
            self.managed_list.addItem(item)
            self.managed_list.setItemWidget(item, row)
            self.rows.append(row)
        self.managed_list.blockSignals(False)
        self._sync_managed_selection(self.managed_list.currentRow())

    def _refresh_detected(self) -> list:
        """The background half: a pure probe with no widget access at all."""
        return list_java(probe_dir=paths.launcher_dir() / "cache")

    def _apply_detected(self, runtimes: list) -> None:
        """The GUI half: fills the list once the probe comes back.

        Runs on the main thread, and only while this page still exists: closing the window
        while the probe is in flight used to end in an access violation.
        """
        if not widget_alive(self) or not widget_alive(self.detected_list):
            return
        self.detected_rows = []
        self.detected_list.blockSignals(True)
        self.detected_list.clear()
        for index, runtime in enumerate(runtimes):
            row = _JavaRow(
                runtime.major,
                "Java " + str(runtime.major),
                provider_label(getattr(runtime, "provider", "")),
                str(runtime.path),
                on_select=lambda row=index: self.detected_list.setCurrentRow(row),
                separator=index < len(runtimes) - 1,
            )
            item = QListWidgetItem("Java " + str(runtime.major))
            item.setData(Qt.UserRole, runtime.major)
            item.setSizeHint(QSize(0, _JavaRow.HEIGHT))
            self.detected_list.addItem(item)
            self.detected_list.setItemWidget(item, row)
            self.detected_rows.append(row)
        self.detected_list.blockSignals(False)
        self._sync_detected_selection(self.detected_list.currentRow())
        self._show_empty(self.detected_stack, self.empty_detected, len(runtimes))

    # ---------- selection ----------

    def _sync_managed_selection(self, index: int = -1) -> None:
        for row_index, row in enumerate(self.rows):
            row.set_selected(row_index == index)

    def _sync_detected_selection(self, index: int = -1) -> None:
        for row_index, row in enumerate(self.detected_rows):
            row.set_selected(row_index == index)

    def _selected_major(self) -> int | None:
        item = self.managed_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _delete_row(self, index: int) -> None:
        """Row delete: select that row, then run the page's delete flow with the row's own button."""
        self.managed_list.setCurrentRow(index)
        item = self.managed_list.item(index)
        row = self.managed_list.itemWidget(item) if item is not None else None
        button = row.action if isinstance(row, _JavaRow) else None
        self._delete_selected(button=button)

    # ---------- actions ----------

    def _delete_selected(self, button: QPushButton | None = None) -> None:
        major = self._selected_major()
        if major is None:
            set_app_status(self, tr("account.msg.need_select"), "warning")
            return
        answer = QMessageBox.question(
            self,
            tr("java.delete.dialog"),
            tr("java.delete.msg", major, str(self._managed.get(major, ""))),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def do_delete() -> object:
            return delete_managed_runtime(major)

        if button is not None:
            disable_keeping_focus(button)
        # The row is rebuilt by refresh(), so the button may be a deleted C++ object by the time the
        # worker finishes: re-enabling it is guarded instead of dereferencing it.
        run_in_background(
            do_delete,
            on_result=lambda _d: (
                self.refresh(),
                set_app_status(self, tr("java.msg.deleted", "Java " + str(major))),
            ),
            on_error=lambda m: set_app_status(self, tr("java.msg.delete_fail", m), "error"),
            on_finished=lambda: _enable_if_alive(button),
        )

    def _install(self) -> None:
        major = self.major_combo.currentData()
        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("java.msg.installing", major))
        bridge = ProgressBridge()
        bridge.progress.connect(
            lambda p: set_app_status(
                self,
                "Java " + str(major) + ": " + str(p.done_files) + "/" + str(p.total_files)
                + " " + p.current,
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
            on_result=lambda _r: (
                self.refresh(),
                set_app_status(self, tr("java.msg.installed", major)),
            ),
            on_error=lambda m: set_app_status(self, tr("java.msg.fail", m), "error"),
            on_finished=lambda: self.install_button.setEnabled(True),
        )
