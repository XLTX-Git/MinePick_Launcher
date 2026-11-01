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

"""Versions page: manifest list as dense version cards, details, install (with progress) and a jump to
the instance page."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QLocale, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import block_icons, i18n, paint_utils
from gui.errors import show_fatal
from gui.pages import versions_install_flow
from gui.pages.versions_install_flow import (
    CAN_ADD,
    CHECKING,
    FETCH_FAILED,
    LOADER_ROWS,
    NO_VERSION,
    InstallStep,
)
from gui.widgets import (
    add_page_top_inset,
    add_search_icon,
    apply_no_focus_outline,
    build_download_progress,
    disable_keeping_focus,
    set_app_status,
    style_page_layout,
)
from gui.workers import (
    ProgressBridge,
    RateTracker,
    format_eta,
    format_rate,
    run_in_background,
)
from launcher import config, paths
from launcher.install import install_version, list_installed_versions
from launcher.instances import (
    InstancesError,
    get_instance,
    list_instances,
    update_instance,
)
from launcher.meta import block_textures, fetch_manifest, version_category

tr = i18n.tr

_log = logging.getLogger(__name__)

# Display name per type column
_TYPE_LABELS = {
    "release": "versions.type.release",
    "snapshot": "versions.type.snapshot",
    "april_fools": "versions.type.april",
    "old_beta": "versions.type.old_beta",
    "old_alpha": "versions.type.old_alpha",
}


def _ui_locale() -> QLocale:
    """The QLocale of the interface language, so dates read the way the rest of the page does."""
    code = i18n.current_language() or "zh_cn"
    parts = code.split("_")
    name = parts[0] + ("_" + parts[1].upper() if len(parts) > 1 else "")
    locale = QLocale(name)
    return locale if locale.language() != QLocale.Language.C else QLocale(QLocale.Language.English)


def human_date(release_time: str) -> str:
    """A release date a person reads: "2025年12月9日" / "9. Dezember 2025" (never raw ISO).

    The manifest gives "2025-12-09T12:00:00+00:00". The date part is parsed and printed with the
    interface language's own long pattern, minus the weekday: Qt's long form carries it ("Dienstag,
    9. Dezember 2025"), which is too long for a 12px meta line and is not what a release date says.
    A value that does not parse is returned unchanged rather than hidden.
    """
    text = (release_time or "").strip()
    if len(text) < 10:
        return text
    date = QDate.fromString(text[:10], "yyyy-MM-dd")
    if not date.isValid():
        return text
    locale = _ui_locale()
    pattern = locale.dateFormat(QLocale.FormatType.LongFormat)
    for token in ("dddd", "ddd"):
        pattern = pattern.replace(token, "")
    pattern = pattern.strip(" ,，、")
    if not pattern:
        return text[:10]
    return locale.toString(date, pattern)


class _LoaderChoice:
    """The loader decision one install runs with - the object the install body asks for it.

    It used to be a modal dialog. The choice is a step of this page now (the version list is replaced
    by ``InstallStep``, not covered by a window), so what the install body receives is the answer that
    step already recorded: ``exec()`` reports "accepted" - the step's own back button is the only way
    to say no, and taking it never calls the install body at all - and ``selected_loader()`` returns
    the loader the user picked, ``None`` meaning the vanilla version.

    Why the install body still asks an object rather than reading the field: ``install_version_id`` is
    the install flow this change must not touch (``_on_progress`` / ``_on_installed`` /
    ``_on_install_error`` and the download closure around them are byte for byte the same), and that
    body opens with these three lines. The decision moved; the body did not.
    """

    def __init__(self, page: VersionsPage) -> None:
        self._page = page

    def exec(self) -> QDialog.DialogCode:
        """Never opens a window: the choice was made before the install body was entered."""
        return QDialog.DialogCode.Accepted

    def selected_loader(self) -> str | None:
        return self._page._pending_loader


#: The name the unchanged install body constructs. It is a decision carrier now, not a window; the
#: alias is what keeps ``install_version_id`` byte-identical to the commit before this one.
_LoaderPromptDialog = _LoaderChoice


class _VersionRow(QWidget):
    """One dense version card: 48px block icon, name + type tag, date/status meta line, install.

    Everything except the install button is painted by hand, for the same two reasons the resource
    rows paint themselves: the meta line must elide on one line (a QLabel either wraps or reports text
    wider than it paints), and a list that can hold a thousand manifest entries must not pay for a
    widget tree per row - one widget plus one button is all Qt has to re-polish on a theme swap.
    """

    HEIGHT = 68
    #: The list slot is the card plus the 8px gap below it. Qt offsets an item widget by the list's
    #: spacing without shrinking it, so the gap lives inside the slot instead (same as the mods list).
    SLOT_HEIGHT = HEIGHT + 8
    ICON = 48
    PADDING = 12
    GAP = 12
    RADIUS = 8
    TAG_HEIGHT = 18
    #: The width of the progress block that stands in for the install button while a download runs.
    PROGRESS_WIDTH = 112
    PROGRESS_HEIGHT = 32

    def __init__(
        self,
        version_id: str,
        type_label: str,
        date_text: str,
        status_text: str,
        installed: bool,
        icon: QPixmap,
        *,
        on_select,
        on_install,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.version_id = version_id
        self.type_label = type_label
        self.date_text = date_text
        self.status_text = status_text
        self.installed = installed
        self._on_select = on_select
        self._on_install = on_install
        self._selected = False
        self._hover = False
        self._icon = icon
        self._install_running = False
        self._progress_value = 0

        # An installed row keeps its button - it is the reinstall path, and the step it opens hides the
        # name field - but it says so instead of offering "Install" again.
        label = tr("versions.status.installed") if installed else tr("versions.install.short")
        self.install = QPushButton(label)
        self.install.setObjectName("rowInstall")
        self.install.setFixedHeight(32)
        self.install.setCursor(Qt.CursorShape.PointingHandCursor)
        # A wall of buttons that all read "Install" is useless with a screen reader; the version name
        # is what tells them apart.
        self.install.setAccessibleName(label + " · " + version_id)
        self.install.clicked.connect(self._activate)
        self.setFixedHeight(self.SLOT_HEIGHT)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, self.PADDING, self.SLOT_HEIGHT - self.HEIGHT)
        row.setSpacing(0)
        row.addStretch(1)
        row.addWidget(self.install, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip(self._tooltip_text())

    # ---------- data ----------

    def icon(self) -> QPixmap:
        """The 48px block this row drew (the install step shows it at 32px)."""
        return self._icon

    def install_running(self) -> bool:
        return self._install_running

    def progress_value(self) -> int:
        """The value on the row's progress block (0-1000, the scale the page's bar uses)."""
        return self._progress_value

    def set_install_progress(self, value: int) -> None:
        """Show this row's download as ``[■■■□□ 42%]`` in place of its install button."""
        self._progress_value = max(0, min(1000, int(value)))
        if not self._install_running:
            self._install_running = True
            self.install.setVisible(False)
        self.update()

    def clear_install_progress(self) -> None:
        """Back to the ordinary row: the install button returns (a failed download offers it again)."""
        self._progress_value = 0
        if self._install_running:
            self._install_running = False
            self.install.setVisible(True)
        self.update()

    def _meta_text(self) -> str:
        if self.installed and self.status_text:
            return f"{self.date_text} · {self.status_text}" if self.date_text else self.status_text
        return self.date_text

    def _tooltip_text(self) -> str:
        parts = [self.version_id]
        if self.type_label:
            parts.append(self.type_label)
        meta = self._meta_text()
        if meta:
            parts.append(meta)
        return "\n".join(parts)

    def _trailing_left(self) -> int:
        """Where the name/meta text has to stop: the install button, or the progress block in its place.

        The button is hidden while a download runs, so the layout stops reserving its slot; the block
        that replaced it is drawn at the same right edge instead.
        """
        if self._install_running:
            return max(0, self.width() - self.PADDING - self.PROGRESS_WIDTH)
        return self.install.x()

    def text_width(self) -> int:
        """Width available to the name and the meta line (what the 20-character names must fit)."""
        text_left = self.PADDING + self.ICON + self.GAP
        return max(0, self._trailing_left() - self.GAP - text_left)

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()
        # Tab reaches the install button of the selected row only: a thousand identical stops would
        # make Tab unusable inside the list, and the arrow keys already move the selection to every row.
        policy = Qt.FocusPolicy.StrongFocus if selected else Qt.FocusPolicy.ClickFocus
        if self.install.focusPolicy() != policy:
            self.install.setFocusPolicy(policy)

    # ---------- interaction ----------

    def _activate(self) -> None:
        """Install this version: select the row first so the page's actions follow the click."""
        self._on_select()
        self._on_install()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
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

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = paint_utils._tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0, 0, self.width(), self.HEIGHT).adjusted(0.5, 0.5, -0.5, -0.5)
        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        if self._selected:
            border = QColor(tokens.get("ACCENT", "#2f9e6f"))
        elif self._hover:
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        icon_rect = QRectF(self.PADDING, (self.HEIGHT - self.ICON) / 2.0, self.ICON, self.ICON)
        painter.drawPixmap(icon_rect.toRect(), self._icon)

        text_left = self.PADDING + self.ICON + self.GAP
        text_right = max(text_left, self._trailing_left() - self.GAP)
        width = text_right - text_left
        if width <= 0:
            painter.end()
            return

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_metrics = QFontMetrics(name_font)
        tag_font = QFont(self.font())
        tag_font.setPixelSize(11)
        tag_metrics = QFontMetrics(tag_font)
        tag_width = min(width, tag_metrics.horizontalAdvance(self.type_label) + 12)

        name_rect = QRectF(text_left, 15, max(0, width), 22)
        painter.setFont(name_font)
        painter.setPen(text_color)
        name = name_metrics.elidedText(self.version_id, Qt.TextElideMode.ElideRight, max(0, width))
        name_width = name_metrics.horizontalAdvance(name)
        painter.drawText(
            name_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name,
        )
        # The tag sits right after the name (not at the far edge); it disappears only when the name
        # would have to be truncated to make room for it.
        if self.type_label and text_left + name_width + 8 + tag_width <= text_right:
            tag_rect = QRectF(text_left + name_width + 8, 17, tag_width, self.TAG_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("BG_SECTION", "#f1f3f6")))
            painter.drawRoundedRect(tag_rect, 4, 4)
            painter.setFont(tag_font)
            painter.setPen(hint_color)
            painter.drawText(
                tag_rect,
                int(Qt.AlignmentFlag.AlignCenter),
                tag_metrics.elidedText(self.type_label, Qt.TextElideMode.ElideRight, int(tag_width) - 12),
            )

        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        meta_metrics = QFontMetrics(meta_font)
        painter.setFont(meta_font)
        painter.setPen(hint_color)
        meta = meta_metrics.elidedText(self._meta_text(), Qt.TextElideMode.ElideRight, int(width))
        painter.drawText(
            QRectF(text_left, 37, width, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            meta,
        )
        if self._install_running:
            self._paint_progress(painter, tokens, hint_color)
        painter.end()

    def _paint_progress(self, painter: QPainter, tokens: dict[str, str], text_color: QColor) -> None:
        """The install button's stand-in while this row downloads: ``[■■■□□ 42%]``.

        Segments rather than a bar: at 32px tall a bar's fill edge says little, while five steps say
        "one file in five" at a glance and still read as the page's progress language beside the
        percentage. The percentage sits on the block's own flat surface, never on the moving fill, for
        the reason ``build_download_progress`` moves the page's label out of its bar: a token that
        contrasts with the unfilled half cannot contrast with the accent fill.
        """
        block = QRectF(
            self.width() - self.PADDING - self.PROGRESS_WIDTH,
            (self.HEIGHT - self.PROGRESS_HEIGHT) / 2.0,
            self.PROGRESS_WIDTH,
            self.PROGRESS_HEIGHT,
        )
        painter.setPen(QPen(QColor(tokens.get("BORDER_LIST", "#c9d2dc")), 1.0))
        painter.setBrush(QColor(tokens.get("BG_SECTION", "#f1f3f6")))
        painter.drawRoundedRect(block, 6, 6)

        segments, filled = 5, self._progress_value / 1000.0 * 5
        width, height, gap = 9.0, 8.0, 3.0
        left = block.left() + 8.0
        middle = block.center().y()
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(segments):
            rect = QRectF(left + index * (width + gap), middle - height / 2.0, width, height)
            done = index + 1 <= int(filled)
            painter.setBrush(
                QColor(
                    tokens.get("ACCENT", "#2f9e6f")
                    if done
                    else tokens.get("BG_RAIL_DOT", "#b6c1cd")
                )
            )
            painter.drawRoundedRect(rect, 2, 2)

        percent_font = QFont(self.font())
        percent_font.setPixelSize(11)
        painter.setFont(percent_font)
        painter.setPen(text_color)
        text_left = left + segments * (width + gap) + 2.0
        painter.drawText(
            QRectF(text_left, block.top(), block.right() - 8.0 - text_left, block.height()),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            f"{round(self._progress_value / 10)}%",
        )


class _VersionList(QListWidget):
    """QListWidget whose item widgets always span the viewport.

    Qt hands an item widget the item's visual rect when it is installed, but a list laid out before
    its final width (or one whose scrollbar appears later) leaves older widgets wider than the
    viewport - the card then hangs over the right edge and its install button is clipped. Re-applying
    the viewport width on every resize keeps every card and button inside the list.
    """

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.sync_item_widths()

    def sync_item_widths(self) -> None:
        width = self.viewport().width()
        for index in range(self.count()):
            widget = self.itemWidget(self.item(index))
            if widget is not None and widget.width() != width:
                widget.resize(width, widget.height())


class VersionsPage(QWidget):
    launch_requested = Signal(str)
    instance_requested = Signal(str)
    versions_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        # Scopes the light sheet's dense-list rules to this page.
        self.setObjectName("versionsPage")
        self.manifest = None
        self._installed: set[str] = set()
        #: visible metadata per version id: (type label, human date, status text)
        self._meta: dict[str, tuple[str, str, str]] = {}
        self.rows: list[_VersionRow] = []
        # The install step's state: the version being set up, the loader the user picked (None =
        # vanilla), the name the step collected, and what the row progress is attached to.
        self._pending_version_id = ""
        self._pending_loader: str | None = None
        self._pending_display_name = ""
        self._install_before_ids: set[str] = set()
        self._installing_version_id = ""
        self._installing_row: _VersionRow | None = None
        self._probe_generation = 0
        self._list_scroll = 0
        self._list_row = -1
        # The step replaces this container, so the list and every filter widget live inside it.
        self._list_step = QWidget()
        self._list_step.setObjectName("versionsListStep")

        # Category chips: all / release / snapshot / april fools / legacy
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tabs: list[QPushButton] = []
        for label, data in (
            ("versions.all_types", None),
            ("versions.type.release", "release"),
            ("versions.type.snapshot", "snapshot"),
            ("versions.type.april", "april_fools"),
            ("versions.filter.legacy", "legacy"),
        ):
            btn = QPushButton(tr(label))
            btn.setCheckable(True)
            btn.setObjectName("resourceChip")
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setAccessibleName(tr(label))
            btn.setProperty("filter", data)
            self.tab_group.addButton(btn)
            self.tabs.append(btn)
        self.tabs[0].setChecked(True)
        self.search_edit = QLineEdit()
        add_search_icon(self.search_edit)
        self.search_edit.setPlaceholderText(tr("versions.search.placeholder"))
        self.search_edit.setAccessibleName(tr("versions.search.placeholder"))  # WCAG 4.1.2
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedHeight(32)  # the design system's field metric
        self.refresh_button = QPushButton(tr("versions.refresh"))
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setFixedHeight(32)
        # Latest version cards: latest release / latest snapshot
        self.latest_labels: dict[str, QLabel] = {}
        self.latest_buttons: dict[str, QPushButton] = {}
        self.list = _VersionList()
        self.list.setObjectName("versionList")
        # WCAG 4.1.2: the list is a keyboard stop with no text of its own. Built from the column
        # names this page still has in every language, so the name matches what the rows show.
        self.list.setAccessibleName(
            " · ".join(
                (
                    tr("versions.col.id"),
                    tr("versions.col.type"),
                    tr("versions.col.time"),
                    tr("versions.status.col"),
                )
            )
        )
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        apply_no_focus_outline(self.list)
        self.info_label = QLabel(tr("versions.info.default"))
        self.info_label.setObjectName("hint")
        self.install_button = QPushButton(tr("versions.install"))
        # Deleting an installed version happens on the instances page (its 删除 action),
        # so this button only jumps there: one page owns "remove a version".
        self.open_instance_button = QPushButton(tr("versions.open_instance"))
        self.open_instance_button.setObjectName("secondaryButton")
        self.detail_button = QPushButton(tr("versions.detail"))
        self.detail_button.setObjectName("secondaryButton")
        self.launch_button = QPushButton(tr("versions.goto"))
        self.launch_button.setObjectName("secondaryButton")
        self.launch_button.setEnabled(False)
        self.auto_jre_check = QCheckBox(tr("versions.auto_jre"))
        self.auto_jre_check.setChecked(True)  # checked by default
        self.auto_jre_check.setObjectName("hint")
        self.progress_row, self.progress_bar, self.progress_text = build_download_progress("downloadProgress")
        self.progress_row.setVisible(False)
        # The thin bar at the bottom of this page is gone. The download's display is the row's own
        # progress block (and the window's task views); this bar was only ever a second copy of the
        # same numbers, and it is the one the user asked to have removed. The install flow still shows
        # it - ``install_version_id`` and its three callbacks are frozen, see FROZEN_INSTALL_FLOW - so
        # the refusal lives here: the widget is in no layout, is zero-sized, and the page refuses every
        # show request it gets, which means it never becomes visible and never takes a pixel.
        self.progress_row.setParent(self._list_step)
        self.progress_row.setFixedSize(0, 0)
        self.progress_row.installEventFilter(self)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.refresh_button)

        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(8)
        for btn in self.tabs:
            tabs_row.addWidget(btn)
        tabs_row.addStretch(1)

        latest_row = QHBoxLayout()
        latest_row.setSpacing(8)
        for kind, key in (
            ("release", "versions.latest.release"),
            ("snapshot", "versions.latest.snapshot"),
        ):
            card = QFrame()
            card.setObjectName("latestCard")
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(12, 8, 12, 8)
            card_layout.setSpacing(16)
            text_column = QVBoxLayout()
            text_column.setSpacing(0)
            kind_label = QLabel(tr(key))
            kind_label.setObjectName("latestKind")
            version_label = QLabel(tr("versions.latest.none"))
            version_label.setObjectName("latestVersion")
            text_column.addWidget(kind_label)
            text_column.addWidget(version_label)
            install_btn = QPushButton(tr("versions.install.short"))
            # A card shortcut installs like a row does, so it wears the row's secondary button: the
            # page keeps exactly one primary action (the install of the selected version below).
            install_btn.setObjectName("rowInstall")
            install_btn.setFixedHeight(32)
            install_btn.setEnabled(False)
            install_btn.setAccessibleName(tr("versions.install.short") + " · " + tr(key))
            install_btn.clicked.connect(
                lambda _checked=False, k=kind: self._install_latest(k)
            )
            card_layout.addLayout(text_column)
            card_layout.addWidget(install_btn)
            self.latest_labels[kind] = version_label
            self.latest_buttons[kind] = install_btn
            latest_row.addWidget(card)
        latest_row.addStretch(1)

        layout = QVBoxLayout(self._list_step)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addLayout(tabs_row)
        layout.addLayout(latest_row)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.info_label)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.open_instance_button)
        buttons.addWidget(self.detail_button)
        buttons.addWidget(self.launch_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(self.auto_jre_check)

        # The install step replaces the list instead of covering it: the list widget itself keeps its
        # filter, its selection and its scroll position, which is what the breadcrumb's back arrow has
        # to come home to.
        self.step = InstallStep()
        self.stack = QStackedWidget()
        self.stack.setObjectName("versionsStack")
        self.stack.addWidget(self._list_step)
        self.stack.addWidget(self.step)
        outer = QVBoxLayout(self)
        # The page keeps the shared outer margin and the 16px top air; the steps inside it supply their
        # own rhythm, because either one may be the visible page.
        style_page_layout(outer)
        add_page_top_inset(outer)
        outer.setSpacing(0)
        outer.addWidget(self.stack)

        self.refresh_button.clicked.connect(self.refresh)
        self.tab_group.buttonClicked.connect(lambda *_a: self._refilter())
        # Debounce: re-filter only after 200ms of typing inactivity
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._refilter)
        self.search_edit.textChanged.connect(lambda _t: self._search_timer.start(200))
        self.list.currentRowChanged.connect(self._on_select)
        # Every entry into an install opens the step; the step's own button starts the download.
        self.install_button.clicked.connect(self._begin_install_selected)
        self.open_instance_button.clicked.connect(self.open_instance_selected)
        self.detail_button.clicked.connect(self.show_details)
        self.launch_button.clicked.connect(
            lambda: self.launch_requested.emit(self._selected_id() or "")
        )
        self.step.back_requested.connect(self._leave_step)
        self.step.start_requested.connect(self._start_from_step)
        self.step.retry_requested.connect(self._probe_loader)
        # The inline progress rides on the page's own bar: ``_on_progress`` keeps writing the bar (the
        # install flow is unchanged), and the row follows the same numbers.
        self.progress_bar.valueChanged.connect(self._on_progress_value)
        # The end of a download is the page's own install button being handed back (the flow re-enables
        # it in ``on_finished``, after either callback ran). That is the moment the row's inline progress
        # goes away again - success rebuilds the rows anyway, a failure offers the install button again.
        self.install_button.installEventFilter(self)
        # The display name lands on the instance folder the install just created (the sidecar), before
        # the other pages are told to re-read; ``versions_changed`` is emitted at the end of
        # ``_on_installed``, where the new folders are already on disk.
        self.versions_changed.connect(self._apply_pending_display_name)
        self._rate = RateTracker()
        self._fetch_started = False
        self._refresh_installed()

    def showEvent(self, event) -> None:
        """Fetch the manifest on first show instead of at construction (lazy page data)."""
        super().showEvent(event)
        if not self._fetch_started:
            self._fetch_started = True
            self.refresh()

    def _game_dir(self) -> Path:
        cfg, _ = config.load()
        env_value = os.environ.get(paths.ENV_GAME_DIR)
        return (
            cfg.game_dir
            or (Path(env_value).expanduser() if env_value else None)
            or paths.default_game_dir()
        )

    def _refresh_installed(self) -> None:
        self._installed = set(list_installed_versions(self._game_dir()))

    def _selected_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def refresh(self) -> None:
        disable_keeping_focus(self.refresh_button)
        set_app_status(self, tr("versions.msg.fetching"))
        cache = paths.launcher_dir() / "cache" / "version_manifest.json"

        def do_fetch() -> object:
            return fetch_manifest(cache_path=cache)

        run_in_background(
            do_fetch,
            on_result=self._on_manifest,
            on_error=lambda m: set_app_status(self, tr("versions.msg.fetch_fail", m), "error"),
            on_finished=lambda: self.refresh_button.setEnabled(True),
        )

    def _on_manifest(self, manifest) -> None:
        self.manifest = manifest
        self._refresh_installed()
        self._update_latest_cards()
        set_app_status(self, tr("versions.msg.fetched", len(manifest.versions)))
        self._refilter()

    def _update_latest_cards(self) -> None:
        latest = getattr(self.manifest, "latest", None) or {}
        for kind in ("release", "snapshot"):
            version_id = (latest.get(kind) or "").strip()
            self.latest_labels[kind].setText(version_id or tr("versions.latest.none"))
            self.latest_buttons[kind].setEnabled(bool(version_id))

    def _install_latest(self, kind: str) -> None:
        latest = getattr(self.manifest, "latest", None) or {}
        version_id = (latest.get(kind) or "").strip()
        if version_id:
            self.begin_install(version_id)

    def _on_row_install(self, version_id: str) -> None:
        """Row install: open the install step for this version.

        The row used to start the download straight away behind a modal loader question. That question
        is the page's second step now, so the row's button is an entry into the step, and the download
        itself still starts from exactly one place (``install_version_id``, called by the step's pill).
        The button that owns the install lifecycle stays the busy signal that keeps a second download
        from starting while one runs.
        """
        if not self.install_button.isEnabled():
            return
        self.begin_install(version_id)

    # ---------- install step: choose a loader, name the instance, then download ----------

    def begin_install(self, version_id: str) -> None:
        """Show the install step for ``version_id`` (the row, the latest card and the page button).

        The list is not destroyed, only replaced: its filter, its selection and its scroll offset are
        remembered here so the breadcrumb's back arrow returns to exactly the list the user left.
        """
        if not self.install_button.isEnabled():
            return
        self._pending_version_id = version_id
        self._pending_loader = None
        self._pending_display_name = ""
        self._list_scroll = self.list.verticalScrollBar().value()
        self._list_row = self.list.currentRow()
        index = self._row_index(version_id)
        if index >= 0:
            self.list.setCurrentRow(index)
        row = self._row_for(version_id)
        self.step.prepare(
            version_id,
            row.icon() if row is not None else None,
            editable=version_id not in self._installed,
            name=version_id,
            taken=self._display_names(),
        )
        self.stack.setCurrentWidget(self.step)
        self.step.name_edit.setFocus()
        self._probe_loaders(version_id)

    def _begin_install_selected(self) -> None:
        """The page's own install button: the selected version, through the same step."""
        version_id = self._selected_id()
        if version_id is None:
            set_app_status(self, tr("versions.msg.need_select"), "warning")
            return
        self.begin_install(version_id)

    def _leave_step(self) -> None:
        """Back to the list, exactly where the user left it."""
        self._probe_generation += 1  # answers for the step that was just left are not shown
        self.stack.setCurrentWidget(self._list_step)
        if 0 <= self._list_row < self.list.count():
            self.list.setCurrentRow(self._list_row)
        self.list.verticalScrollBar().setValue(self._list_scroll)

    def _start_from_step(self) -> None:
        """The step's pill button: record the answer, go back to the list, start the download.

        The next step is in the list itself: the row's install button becomes a progress block, so the
        user watches the download where they asked for it. There is no progress page and no task bar.
        """
        if not self.install_button.isEnabled() or not self.step.name_valid():
            return
        self._pending_loader = self.step.selected_loader()
        self._pending_display_name = (
            self.step.instance_name() if self.step.name_editable() else ""
        )
        self._probe_generation += 1
        version_id = self._pending_version_id
        self._install_before_ids = set(self._installed)
        self.stack.setCurrentWidget(self._list_step)
        self._attach_inline_progress(version_id)
        # The one install entry the page has always used (the loader decision is read from the field
        # the step filled in), so the download, its callbacks and its status text are untouched.
        self.install_version_id(version_id)

    def _probe_loaders(self, version_id: str) -> None:
        """Ask every loader row whether it can add this version (one background query per loader)."""
        self._probe_generation += 1
        for loader in LOADER_ROWS:
            self._probe_loader(loader)

    def _probe_loader(self, loader: str) -> None:
        """Query one loader's versions for the pending version; a row can also ask for a retry."""
        version_id = self._pending_version_id
        if not version_id or loader not in LOADER_ROWS:
            return
        generation = self._probe_generation
        self.step.set_loader_status(loader, CHECKING)

        def do_probe() -> object:
            return versions_install_flow.probe_loader(loader, version_id)

        def on_result(versions) -> None:
            if generation != self._probe_generation:
                return  # the step moved on: an answer for an abandoned version is not shown
            # Empty is a real answer ("none for this version"), not a failure: the two states must
            # never be folded together, because only one of them is worth retrying.
            state = CAN_ADD if versions else NO_VERSION
            self.step.set_loader_status(loader, state)

        def on_error(message: str) -> None:
            if generation != self._probe_generation:
                return
            self.step.set_loader_status(loader, FETCH_FAILED, message)

        run_in_background(do_probe, on_result=on_result, on_error=on_error)

    # ---------- row progress and the instance name ----------

    def _row_index(self, version_id: str) -> int:
        for index in range(self.list.count()):
            if self.list.item(index).data(Qt.UserRole) == version_id:
                return index
        return -1

    def _row_for(self, version_id: str) -> _VersionRow | None:
        return next((row for row in self.rows if row.version_id == version_id), None)

    def _attach_inline_progress(self, version_id: str) -> None:
        self._installing_version_id = version_id
        self._installing_row = self._row_for(version_id)
        if self._installing_row is not None:
            self._installing_row.set_install_progress(0)

    def _sync_install_row(self) -> None:
        """Re-attach the running progress to the row a rebuild just created.

        A rebuild can happen during a download (typing in the search box re-filters the list); the
        progress bar still holds the value, so the new row picks the download up where it is.
        """
        if not self._installing_version_id:
            return
        self._installing_row = self._row_for(self._installing_version_id)
        if self._installing_row is not None:
            self._installing_row.set_install_progress(self.progress_bar.value())

    def _on_progress_value(self, value: int) -> None:
        if self._installing_row is not None:
            self._installing_row.set_install_progress(value)

    def _finish_inline_progress(self) -> None:
        """The download ended (success or failure): give the row its install button back.

        The loader decision is dropped here too, so a later install that did not come through the step
        cannot silently reuse the previous answer.
        """
        row = self._installing_row
        self._installing_row = None
        self._installing_version_id = ""
        self._pending_loader = None
        if row is not None:
            row.clear_install_progress()

    def eventFilter(self, obj, event) -> bool:
        """The two page-layer decisions that keep the frozen install flow's own widgets in order.

        *The bottom bar*: the flow shows it at the start of every install and hides it at the end. This
        page refuses the show - the widget is in no layout and stays zero-sized - so it never becomes a
        second progress display. It is only the percentage source the row's own block reads, which is
        why ``_on_progress`` writing it is still exactly what makes the row move.

        *The install button*: the flow hands it back once the worker finished, whichever callback ran.
        That is the page's one end-of-install signal, and it is what returns a row's inline progress to
        its install button - the bar's own hide used to do that, and it cannot any more, because the bar
        is never shown.
        """
        if obj is self.progress_row:
            if event.type() == QEvent.Type.Show:
                self.progress_row.hide()
            return False
        if (
            obj is self.install_button
            and event.type() == QEvent.Type.EnabledChange
            and self.install_button.isEnabled()
            and self._installing_version_id
        ):
            self._finish_inline_progress()
        return super().eventFilter(obj, event)

    def _display_names(self) -> set[str]:
        """Every display name an instance already carries (what a new name must not repeat)."""
        try:
            return {instance.name for instance in list_instances(self._game_dir()).values()}
        except OSError:
            return set()

    def _apply_pending_display_name(self) -> None:
        """Write the name the step collected onto the instance the install just created.

        The folder stays ``versions/<version id>``: the name is the *display* name, stored in the
        instance's own ``minepick.json`` sidecar (``launcher.instances``), which is what the instances
        page renders. A name the user already set is never overwritten, and a reinstall collects no
        name at all, so it cannot rename anything either.
        """
        name = self._pending_display_name.strip()
        version_id = self._pending_version_id
        self._pending_display_name = ""
        if not name or not version_id:
            return
        created = self._installed - self._install_before_ids
        # A loader install creates its own profile next to the vanilla folder; that profile is the
        # instance the user will start, so it takes the name.
        target = next((vid for vid in created if vid != version_id), version_id)
        if target not in self._installed:
            return
        game_dir = self._game_dir()
        try:
            existing = get_instance(target, game_dir=game_dir)
            if existing is not None and existing.display_name.strip():
                return  # a name the user already gave this instance is never overwritten
            update_instance(target, game_dir=game_dir, display_name=name)
        except (InstancesError, OSError):
            _log.warning("versions: could not store the instance name for %s", target, exc_info=True)

    def _refilter(self) -> None:
        if self.manifest is None:
            return
        checked = self.tab_group.checkedButton()
        want = checked.property("filter") if checked is not None else None
        needle = self.search_edit.text().strip().lower()
        shown = []
        for v in self.manifest.versions:
            category = version_category(v.id, v.type)
            if want is None:
                ok = True
            elif want == "legacy":
                ok = category in ("old_beta", "old_alpha")
            else:
                ok = want == category
            if ok and (not needle or needle in v.id.lower()):
                shown.append((v, category))
        # 所有分类(含「全部类型」)一律按发布时间倒序:最新在最前;release_time 是 ISO 字符串,
        # 直接按字符串降序即可,缺时间的排在最后
        shown.sort(key=lambda pair: pair[0].release_time or "", reverse=True)
        self._populate(shown)
        self.info_label.setText(tr("versions.msg.shown", len(shown)))

    def _populate(self, shown: list[tuple[object, str]]) -> None:
        """Fill the dense list with one card per version, in the order ``_refilter`` decided.

        Items are all added before any item widget is installed. Interleaving the two makes
        ``addItem`` re-lay-out every widget already in the list, which measured quadratic: 400 rows
        took 5.9s interleaved against 0.1s batched on this page. The manifest holds ~1000 versions, so
        the batched shape is what keeps a refresh (and every search keystroke) responsive. Signals are
        blocked while the list is rebuilt so a half-filled list never reports a selection.
        """
        current = self._selected_id()
        # Drop the Python references before clear(): clear() would otherwise let the row list point at
        # C++ widgets that are being destroyed, and the selection handler walks it.
        self.rows = []
        self._meta = {}
        self.list.blockSignals(True)
        self.list.setUpdatesEnabled(False)
        try:
            self.list.clear()
            for v, category in shown:
                type_label = tr(_TYPE_LABELS.get(category, "versions.type.release"))
                date_text = human_date(v.release_time)
                status_text = (
                    tr("versions.status.installed")
                    if v.id in self._installed
                    else tr("versions.status.not_installed")
                )
                self._meta[v.id] = (type_label, date_text, status_text)
                # The item's own text is the accessible/testable representation; the card paints the
                # human-readable form on top of it.
                item = QListWidgetItem(
                    " · ".join(part for part in (v.id, type_label, date_text) if part)
                )
                item.setData(Qt.UserRole, v.id)
                item.setSizeHint(QSize(0, _VersionRow.SLOT_HEIGHT))
                self.list.addItem(item)
            game_dir = self._game_dir()
            cache_root = block_textures.texture_cache_root(paths.launcher_dir())
            for index, (v, category) in enumerate(shown):
                type_label, date_text, status_text = self._meta[v.id]
                installed = v.id in self._installed
                # The same install path the module always used: the row's button calls it by id.
                row = _VersionRow(
                    v.id,
                    type_label,
                    date_text,
                    status_text,
                    installed,
                    self._row_icon(v.id, v.type, installed, game_dir, cache_root),
                    on_select=lambda row_index=index: self.list.setCurrentRow(row_index),
                    on_install=lambda version_id=v.id: self._on_row_install(version_id),
                )
                self.list.setItemWidget(self.list.item(index), row)
                self.rows.append(row)
        finally:
            self.list.setUpdatesEnabled(True)
            self.list.blockSignals(False)
        if shown:
            index = next(
                (i for i, (v, _c) in enumerate(shown) if v.id == current),
                0,
            )
            self.list.setCurrentRow(index)
        self._sync_row_selection(self.list.currentRow())
        # A rebuild during a download (a search keystroke) hands the new row the running progress.
        self._sync_install_row()
        # The page may still be inside its first layout pass here; re-apply the viewport width once
        # the widget hierarchy has settled (see _VersionList).
        QTimer.singleShot(0, self.list.sync_item_widths)

    def _row_icon(
        self,
        version_id: str,
        version_type: str,
        installed: bool,
        game_dir: Path,
        cache_root: Path,
    ) -> QPixmap:
        """The block standing for this row's version type.

        release -> grass, snapshot -> command block, April Fools -> gold, ancient -> cobblestone.
        Only an installed version can supply its own texture from ``assets`` or its client jar;
        every other row starts at the user's cache and ends at the drawn block, exactly like the
        instance cards.
        """
        category = block_textures.icon_category(version_id, version_type)
        block = block_textures.block_name_for_category(category)
        return block_icons.block_pixmap(
            block,
            _VersionRow.ICON,
            game_dir=game_dir if installed else None,
            version_id=version_id,
            cache_root=cache_root,
        )

    def _sync_row_selection(self, index: int) -> None:
        for row_index, row in enumerate(self.rows):
            row.set_selected(row_index == index)

    def _on_select(self, row: int = -1) -> None:
        self._sync_row_selection(row)
        version_id = self._selected_id()
        if version_id is None:
            return
        self.info_label.setText(tr("versions.info.selected", version_id))

    # ---------- row metadata (what the cards paint, addressable for tests) ----------

    def version_ids(self) -> list[str]:
        """The visible version ids, top to bottom - the list's own order."""
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def type_of(self, version_id: str) -> str:
        return self._meta.get(version_id, ("", "", ""))[0]

    def date_of(self, version_id: str) -> str:
        return self._meta.get(version_id, ("", "", ""))[1]

    def status_of(self, version_id: str) -> str:
        return self._meta.get(version_id, ("", "", ""))[2]

    def install_selected(self) -> None:
        version_id = self._selected_id()
        if version_id is None:
            set_app_status(self, tr("versions.msg.need_select"), "warning")
            return
        self.install_version_id(version_id)

    def install_version_id(self, version_id: str) -> None:
        # Ask about a mod loader before installing
        prompt = _LoaderPromptDialog(self)
        if prompt.exec() != QDialog.DialogCode.Accepted:
            return
        loader = prompt.selected_loader()
        cfg, _ = config.load()
        game_dir = self._game_dir()
        disable_keeping_focus(self.install_button)
        self.progress_row.setVisible(True)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        set_app_status(self, tr("versions.msg.installing", version_id))
        self._rate = RateTracker()
        bridge = ProgressBridge()
        bridge.progress.connect(self._on_progress)

        def do_install(progress) -> object:
            result = install_version(
                version_id,
                game_dir=game_dir,
                cache_dir=paths.launcher_dir() / "cache",
                concurrency=cfg.max_concurrent_downloads,
                progress=progress,
                auto_install_java=self.auto_jre_check.isChecked(),
                runtime_dir=paths.launcher_dir() / "runtime",
            )
            profile_id = None
            if loader is not None:
                from launcher.mods import ModsError, install_loader, list_loader_versions

                versions = list_loader_versions(loader, version_id)
                if not versions:
                    raise ModsError(tr("versions.msg.loader_unsupported", loader, version_id))
                profile_id = install_loader(
                    versions[0], game_dir, cache_dir=paths.launcher_dir() / "cache"
                )
            return result, profile_id

        run_in_background(
            do_install,
            bridge,
            on_result=self._on_installed,
            on_error=self._on_install_error,
            on_finished=lambda: self.install_button.setEnabled(True),
        )

    def open_instance_selected(self) -> None:
        """Hand the selected profile over to the instances page (where it is managed/deleted)."""
        version_id = self._selected_id()
        if version_id is None:
            set_app_status(self, tr("versions.msg.need_select"), "warning")
            return
        if version_id not in self._installed:
            set_app_status(self, tr("versions.msg.open_instance_fail", version_id), "error")
            return
        self.instance_requested.emit(version_id)

    def show_details(self) -> None:
        version_id = self._selected_id()
        if version_id is None:
            set_app_status(self, tr("versions.msg.need_select"), "warning")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("versions.detail.title"))
        dialog.resize(560, 420)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(tr("versions.detail.loading"))
        layout = QVBoxLayout(dialog)
        layout.addWidget(view)
        cache = paths.launcher_dir() / "cache"

        def do_load() -> object:
            from launcher.meta import detect_platform, load_version_json, resolve_libraries

            version = load_version_json(version_id, cache_dir=cache)
            platform = detect_platform()
            resolved = resolve_libraries(version.libraries, platform)
            natives = [r for r in resolved if r.classifier is not None]
            return version, resolved, natives

        def _safe_text(text: str) -> None:
            try:
                view.setPlainText(text)
            except RuntimeError:
                pass  # dialog was closed while loading

        run_in_background(
            do_load,
            on_result=lambda result: self._fill_details(view, result),
            on_error=lambda m: _safe_text(tr("versions.detail.fail", m)),
        )
        dialog.exec()

    @staticmethod
    def _fill_details(view, result) -> None:
        """Fill the version detail text; silently ignore when the dialog was closed (RuntimeError)."""
        try:
            version, resolved, natives = result
        except RuntimeError:
            return
        try:
            if version.java_version is not None:
                java_text = tr(
                    "versions.detail.java_version",
                    version.java_version.major_version,
                    version.java_version.component,
                )
            else:
                java_text = tr("versions.detail.java_default")
            client_art = version.downloads.get("client")
            none_text = tr("versions.detail.none")
            client_url = (
                client_art.url
                if client_art is not None and client_art.url
                else tr("versions.detail.no_url")
            )
            if client_art is None:
                client_url, sha1_text, size_text = none_text, none_text, none_text
            else:
                sha1_text = client_art.sha1 or none_text
                size_text = str(client_art.size) if client_art.size is not None else none_text
            fmt = (
                tr("versions.detail.legacy")
                if version.is_legacy
                else tr("versions.detail.modern")
            )
            view.setPlainText(
                tr(
                    "versions.detail.line",
                    version.id,
                    version.type,
                    version.release_time or tr("versions.detail.unknown"),
                    version.main_class,
                    java_text,
                    version.asset_index.id,
                    version.assets,
                    version.client_jar_name,
                    client_url,
                    sha1_text,
                    size_text,
                    len(version.libraries),
                    len(resolved),
                    len(natives),
                    fmt,
                    len(version.effective_game_arguments()),
                    len(version.effective_jvm_arguments()),
                )
            )
        except RuntimeError:
            pass  # dialog was closed while loading

    def refresh_installed(self) -> None:
        """Re-read the installed profiles: another page may have removed a version."""
        self._refresh_installed()
        self._refilter()

    def _on_progress(self, p) -> None:
        if p.total_files:
            self.progress_bar.setValue(int(p.done_files * 1000 / p.total_files))
            self._rate.set_total(p.total_bytes)
            rate, eta = self._rate.update(p.done_bytes)
            if rate > 0:
                set_app_status(
                    self,
                    tr(
                        "versions.msg.downloading_rate",
                        p.done_files,
                        p.total_files,
                        p.current,
                        format_rate(rate),
                        format_eta(eta),
                    ),
                )
            else:
                set_app_status(
                    self, tr("versions.msg.downloading", p.done_files, p.total_files, p.current)
                )

    def _on_installed(self, payload) -> None:
        result, profile_id = payload
        self.progress_row.setVisible(False)
        self.launch_button.setEnabled(True)
        self._refresh_installed()
        self._refilter()
        if result.failed:
            message = tr("versions.msg.partial", len(result.failed))
        else:
            message = tr("versions.msg.done", result.downloaded, result.skipped)
        if profile_id:
            message += " | " + tr("versions.msg.loader_done", profile_id)
        set_app_status(self, message)
        self.versions_changed.emit()

    def _on_install_error(self, message: str) -> None:
        self.progress_row.setVisible(False)
        text = tr("versions.msg.install_fail", message)
        set_app_status(self, text, "error")
        show_fatal(self, text)  # fatal error dialog
