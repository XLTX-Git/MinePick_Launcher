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

from PySide6.QtCore import (
    QDate,
    QDateTime,
    QEvent,
    QLocale,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
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

#: The four folds of the version list, in the order PCL's page shows them. They partition the whole
#: manifest: a version is in exactly one of them, and the counts therefore add up to the total.
_GROUP_ORDER = ("release", "snapshot", "legacy", "april")
_GROUP_TITLE_KEYS = {
    "release": "versions.type.release",
    "snapshot": "versions.type.snapshot",
    "legacy": "versions.filter.legacy",
    "april": "versions.group.april",
}
#: Rows one fold builds at a time. The snapshot fold alone holds ~800 manifest entries; building them
#: all for a fold the user has not asked for is the cost this page is shaped to avoid.
_GROUP_BATCH = 50


def _group_of(version) -> str:
    """Which fold a manifest entry belongs to.

    The three type-driven folds read the manifest's own ``type`` field. April Fools versions are typed
    ``snapshot`` (or, for the old ``2.0``, ``release``) by Mojang, so the April check comes first and
    reuses the launcher's existing category rule - ``block_textures.icon_category`` - instead of a
    second list of ids living here.
    """
    if block_textures.icon_category(version.id, version.type) == "april_fools":
        return "april"
    if version.type in ("old_beta", "old_alpha"):
        return "legacy"
    if version.type == "snapshot":
        return "snapshot"
    return "release"


def human_datetime(release_time: str) -> str:
    """The stamp the latest-version card shows: ``2025/12/9 20:00`` in the interface's local time.

    The manifest writes UTC ("2025-12-09T12:00:00+00:00"). The page shows the user's own clock, the
    same way every other download date on the page does, with the numeric month/day the brief asks for
    (the long form belongs to the row's date line, not to this compact meta line).
    """
    text = (release_time or "").strip()
    if len(text) < 16:
        return ""
    stamp = QDateTime.fromString(text, Qt.DateFormat.ISODate)
    if not stamp.isValid():
        return ""
    stamp = stamp.toLocalTime()
    date = stamp.date()
    return (
        f"{date.year()}/{date.month()}/{date.day()} "
        f"{stamp.time().hour():02d}:{stamp.time().minute():02d}"
    )


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
        highlight: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.version_id = version_id
        self.type_label = type_label
        self.date_text = date_text
        self.status_text = status_text
        self.installed = installed
        #: The search term this row was built for; the painted name highlights every occurrence.
        self.highlight = (highlight or "").strip()
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

    def _paint_match_highlights(
        self,
        painter: QPainter,
        tokens: dict[str, str],
        name: str,
        metrics: QFontMetrics,
        text_left: int,
    ) -> None:
        """Tint the part of the visible name the search term matched.

        The row is the same card it always was; the only addition is a flat accent wash behind the
        matched characters, built from the same ``ACCENT`` token the selection border uses. It is
        painted before the text, and only over an occurrence that survived elision, so the highlight
        can never point at a character the card does not show.
        """
        needle = self.highlight.lower()
        if not needle:
            return
        haystack = name.lower()
        spans: list[tuple[int, int]] = []
        start = haystack.find(needle)
        while start >= 0:
            spans.append((start, start + len(needle)))
            start = haystack.find(needle, start + len(needle))
        if not spans:
            return
        tint = QColor(tokens.get("ACCENT", "#2f9e6f"))
        tint.setAlpha(56)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tint)
        for start, end in spans:
            left = text_left + metrics.horizontalAdvance(name[:start])
            matched = metrics.horizontalAdvance(name[start:end])
            painter.drawRoundedRect(QRectF(left - 1, 16, matched + 2, 20), 3, 3)

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
        name = name_metrics.elidedText(self.version_id, Qt.TextElideMode.ElideRight, max(0, width))
        name_width = name_metrics.horizontalAdvance(name)
        self._paint_match_highlights(painter, tokens, name, name_metrics, text_left)
        painter.setFont(name_font)
        painter.setPen(text_color)
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


class _GroupHeader(QPushButton):
    """One fold: a full-row card with the category name, its count and a chevron.

    The card is the control (PCL's "带计数的折叠分组": the whole row toggles), not a label with a small
    button beside it. It is a ``QPushButton`` subclass so Space/Enter, the focus chain and the
    ``keyboardFocus`` mark come for free; the painting is this class's own, the way the version rows
    paint themselves, because the count has to sit next to the elided title.
    """

    HEIGHT = 44
    #: The list slot is the card plus the 8px gap below it (same convention as ``_VersionRow``).
    SLOT_HEIGHT = HEIGHT + 8
    RADIUS = 8
    PADDING = 14
    CHEVRON = 9

    def __init__(
        self,
        group_key: str,
        title: str,
        count: int,
        expanded: bool,
        *,
        on_toggle,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.group_key = group_key
        self.title = title
        self.count = count
        self._expanded = expanded
        self._on_toggle = on_toggle
        self._hover = False
        self.setObjectName("versionGroupHeader")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(self.SLOT_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("")
        self.clicked.connect(lambda: self._on_toggle(self.group_key))
        self._sync_accessibility()

    # ---------- data ----------

    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self._sync_accessibility()
        self.update()

    def set_count(self, count: int) -> None:
        self.count = count
        self._sync_accessibility()
        self.update()

    def _sync_accessibility(self) -> None:
        state = tr("versions.group.expanded" if self._expanded else "versions.group.collapsed")
        self.setAccessibleName(f"{self.title} ({self.count}) · {state}")
        self.setToolTip(f"{self.title} ({self.count})")

    # ---------- geometry ----------

    def sizeHint(self) -> QSize:
        return QSize(600, self.SLOT_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(220, self.SLOT_HEIGHT)

    # ---------- interaction ----------

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        self.update()

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = paint_utils._tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.HEIGHT - 1.0)
        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        if self._hover:
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
            border = QColor(tokens.get("BORDER_HOVER", border.name()))
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            border = QColor(tokens.get("FOCUS_RING", border.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        title_font = QFont(self.font())
        title_font.setPixelSize(15)
        title_font.setWeight(QFont.Weight.DemiBold)
        title_metrics = QFontMetrics(title_font)
        count_font = QFont(self.font())
        count_font.setPixelSize(12)
        count_metrics = QFontMetrics(count_font)
        count_text = f"({self.count})"
        chevron_left = self.width() - self.PADDING - self.CHEVRON
        count_width = count_metrics.horizontalAdvance(count_text)
        title_width = max(
            0,
            chevron_left - 12 - count_width - 8 - self.PADDING,
        )
        title = title_metrics.elidedText(
            self.title, Qt.TextElideMode.ElideRight, int(title_width)
        )
        painter.setFont(title_font)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(self.PADDING, 0, title_width, self.HEIGHT),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title,
        )
        title_used = title_metrics.horizontalAdvance(title)
        painter.setFont(count_font)
        painter.setPen(hint_color)
        painter.drawText(
            QRectF(
                self.PADDING + title_used + 8,
                1,
                max(0, count_width),
                self.HEIGHT,
            ),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            count_text,
        )
        self._paint_chevron(painter, hint_color, chevron_left)

    def _paint_chevron(self, painter: QPainter, color: QColor, left: float) -> None:
        """A 9px chevron: right when folded, down when open - the one visual that says "fold"."""
        middle = self.HEIGHT / 2.0
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        if self._expanded:
            x, y = left + self.CHEVRON / 2.0, middle - 2.0
            path.moveTo(x - 4.0, y)
            path.lineTo(x, y + 4.0)
            path.lineTo(x + 4.0, y)
        else:
            x, y = left, middle
            path.moveTo(x, y - 4.0)
            path.lineTo(x + 4.0, y)
            path.lineTo(x, y + 4.0)
        painter.drawPath(path)


class _LoadMoreRow(QPushButton):
    """The one way another batch of a fold is built: a quiet full-row button under its rows."""

    HEIGHT = 40
    SLOT_HEIGHT = HEIGHT + 8
    RADIUS = 8
    PADDING = 14

    def __init__(self, text: str, *, on_load, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._hover = False
        self.setObjectName("versionLoadMore")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(self.SLOT_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.clicked.connect(on_load)

    def sizeHint(self) -> QSize:
        return QSize(600, self.SLOT_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(160, self.SLOT_HEIGHT)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        self.update()

    def paintEvent(self, event) -> None:
        tokens = paint_utils._tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.HEIGHT - 1.0)
        background = QColor(tokens.get("BG_SECTION", "#f1f3f6"))
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        if self._hover:
            border = QColor(tokens.get("BORDER_HOVER", border.name()))
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            border = QColor(tokens.get("FOCUS_RING", border.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        font = QFont(self.font())
        font.setPixelSize(12)
        metrics = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(QColor(tokens.get("FG_HINT", "#4f6070")))
        text = metrics.elidedText(
            self._text, Qt.TextElideMode.ElideRight, max(0, self.width() - self.PADDING * 2)
        )
        painter.drawText(
            QRectF(0, 0, self.width(), self.HEIGHT),
            int(Qt.AlignmentFlag.AlignCenter),
            text,
        )


class _LatestCard(QPushButton):
    """The latest release / latest snapshot shortcut: block icon, version and its publication stamp.

    The whole card is the control, like the fold headers: clicking it opens the install step for that
    version, which is exactly what the same version's row button does. It is a shortcut, not a second
    install path - it calls ``_install_latest``, the page's one entry into ``begin_install``.
    """

    HEIGHT = 64
    RADIUS = 8
    PADDING = 12
    ICON = 40
    GAP = 12
    CHEVRON = 8

    def __init__(
        self,
        kind: str,
        kind_label: str,
        *,
        on_click,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.kind_label = kind_label
        self.version_id = ""
        self.version_type = kind
        self.datetime_text = ""
        self._icon = QPixmap()
        self._hover = False
        self.setObjectName("versionLatestCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.clicked.connect(on_click)
        self.set_version("", kind, "", QPixmap())

    # ---------- data ----------

    def icon(self) -> QPixmap:
        return self._icon

    def set_version(
        self,
        version_id: str,
        version_type: str,
        datetime_text: str,
        icon: QPixmap,
    ) -> None:
        self.version_id = (version_id or "").strip()
        self.version_type = version_type or self.kind
        self.datetime_text = datetime_text or ""
        self._icon = icon if icon is not None else QPixmap()
        self.setEnabled(bool(self.version_id))
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if self.version_id
            else Qt.CursorShape.ArrowCursor
        )
        if self.version_id:
            meta = self._meta_text()
            self.setAccessibleName(
                " · ".join(part for part in (self.kind_label, self.version_id, meta) if part)
            )
            self.setToolTip(self.accessibleName())
        else:
            self.setAccessibleName(self.kind_label)
            self.setToolTip(self.kind_label)
        self.update()

    def _meta_text(self) -> str:
        if not self.version_id:
            return tr("versions.latest.empty")
        if self.datetime_text:
            return f"{self.kind_label} · {tr('versions.latest.published', self.datetime_text)}"
        return self.kind_label

    # ---------- geometry ----------

    def sizeHint(self) -> QSize:
        return QSize(600, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(260, self.HEIGHT)

    # ---------- interaction ----------

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        self.update()

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = paint_utils._tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.HEIGHT - 1.0)
        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        if self._hover and self.isEnabled():
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
            border = QColor(tokens.get("BORDER_HOVER", border.name()))
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            border = QColor(tokens.get("FOCUS_RING", border.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        icon_rect = QRectF(
            self.PADDING,
            (self.HEIGHT - self.ICON) / 2.0,
            self.ICON,
            self.ICON,
        )
        if not self._icon.isNull():
            painter.drawPixmap(icon_rect.toRect(), self._icon)

        text_left = self.PADDING + self.ICON + self.GAP
        text_right = self.width() - self.PADDING - self.CHEVRON - 12
        width = max(0, text_right - text_left)
        if width <= 0:
            painter.end()
            return

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        if not self.isEnabled():
            text_color = QColor(tokens.get("FG_DISABLED", text_color.name()))
            hint_color = QColor(tokens.get("FG_DISABLED", hint_color.name()))
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_metrics = QFontMetrics(name_font)
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        meta_metrics = QFontMetrics(meta_font)

        name = name_metrics.elidedText(
            self.version_id or tr("versions.latest.none"),
            Qt.TextElideMode.ElideRight,
            width,
        )
        painter.setFont(name_font)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(text_left, 15, width, 22),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name,
        )
        meta = meta_metrics.elidedText(self._meta_text(), Qt.TextElideMode.ElideRight, width)
        painter.setFont(meta_font)
        painter.setPen(hint_color)
        painter.drawText(
            QRectF(text_left, 37, width, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            meta,
        )
        if self.isEnabled():
            self._paint_chevron(painter, hint_color)

    def _paint_chevron(self, painter: QPainter, color: QColor) -> None:
        """A right-pointing chevron: this row opens a step, it does not fold."""
        x = self.width() - self.PADDING - self.CHEVRON
        middle = self.HEIGHT / 2.0
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(x, middle - 4.0)
        path.lineTo(x + 4.0, middle)
        path.lineTo(x, middle + 4.0)
        painter.drawPath(path)


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
        # The four folds. Every manifest version lands in exactly one bucket; the buckets are sorted
        # newest first on every rebuild, and only a bucket the user opened builds row widgets.
        self._buckets: dict[str, list[tuple[object, str]]] = {
            key: [] for key in _GROUP_ORDER
        }
        self._expanded: set[str] = set()
        self._shown: dict[str, int] = {key: 0 for key in _GROUP_ORDER}
        #: The header widgets of the current build (recreated on every rebuild), keyed by fold.
        self._headers: dict[str, _GroupHeader] = {}
        #: The header items, so a batch can be inserted directly under its fold without re-laying the
        #: whole list (the header is the anchor the index is computed from).
        self._header_items: dict[str, QListWidgetItem] = {}
        #: The "load more" rows of the current build, so focus can stay on the control that asked.
        self._load_more_rows: dict[str | None, _LoadMoreRow] = {}
        self._load_more_items: dict[str | None, QListWidgetItem] = {}
        #: Search mode builds a flat list, also in batches; ``_search_total`` is the number of matches
        #: in the whole manifest (what the "N 个结果" line counts), not the number of rows built.
        self._search_total = 0
        self._search_shown = 0
        self._search_needle = ""
        #: Folds a search had to open because it found a version inside them (shown in the result
        #: line). A closing search closes only these - a fold the user opened by hand stays open.
        self._auto_groups: list[str] = []
        self._auto_opened: set[str] = set()
        #: Icons of the two latest versions, so the install step opened from a card still has one.
        self._latest_icons: dict[str, QPixmap] = {}
        # The step replaces this container, so the list and every filter widget live inside it.
        self._list_step = QWidget()
        self._list_step.setObjectName("versionsListStep")

        self.search_edit = QLineEdit()
        add_search_icon(self.search_edit)
        self.search_edit.setPlaceholderText(tr("versions.search.placeholder"))
        self.search_edit.setAccessibleName(tr("versions.search.placeholder"))  # WCAG 4.1.2
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedHeight(32)  # the design system's field metric
        self.refresh_button = QPushButton(tr("versions.refresh"))
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setFixedHeight(32)
        # Latest version shortcuts: the latest release and the latest snapshot, one full-row card each.
        self.latest_cards: dict[str, _LatestCard] = {}
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
        # Fold headers are 52px, version rows 76px: item sizes are no longer uniform. Batched layout
        # is what keeps a list of hundreds of item widgets fast anyway: measured on the live manifest,
        # one more 50-row batch stays at ~60ms from 100 to 742 rows, while the default layout grew to
        # seconds once ~450 rows existed (Qt re-lays out every item on each insertion).
        self.list.setUniformItemSizes(False)
        self.list.setLayoutMode(QListView.LayoutMode.Batched)
        self.list.setBatchSize(_GROUP_BATCH)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        apply_no_focus_outline(self.list)
        # The search summary, right above the list: "N 个结果 · 已展开：快照版". Hidden when the box is
        # empty (the page then says "共 N 个版本" on the hint line below the list, as it always did).
        self.result_count = QLabel("")
        self.result_count.setObjectName("hint")
        self.result_count.setVisible(False)
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
        # The four actions above only ever act on the selected row: with no row selected they are
        # hidden (not left clickable-but-dead), and this one line says how to bring them back. It keeps
        # the row's height so showing/hiding the buttons never moves the list.
        self.action_hint = QLabel(tr("versions.select.hint"))
        self.action_hint.setObjectName("hint")
        self.action_hint.setFixedHeight(32)
        self.action_hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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

        # Two stacked full-row shortcuts (the brief's "one line each"): a block, the version number and
        # the kind plus its publication stamp. Clicking one opens the same install step a row's button
        # opens; there is no second install path.
        latest_column = QVBoxLayout()
        latest_column.setSpacing(8)
        for kind, key in (
            ("release", "versions.latest.release"),
            ("snapshot", "versions.latest.snapshot"),
        ):
            card = _LatestCard(
                kind,
                tr(key),
                on_click=lambda _checked=False, k=kind: self._install_latest(k),
                parent=self._list_step,
            )
            self.latest_cards[kind] = card
            latest_column.addWidget(card)

        layout = QVBoxLayout(self._list_step)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addLayout(latest_column)
        layout.addWidget(self.result_count)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.info_label)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.open_instance_button)
        buttons.addWidget(self.detail_button)
        buttons.addWidget(self.launch_button)
        buttons.addWidget(self.action_hint)
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
        self._sync_actions(None)  # an empty list has nothing to act on: hide the four buttons

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

    def _manifest_version(self, version_id: str):
        """One manifest entry by id, answering for both ``VersionManifest`` and the test doubles."""
        if not version_id or self.manifest is None:
            return None
        find = getattr(self.manifest, "find", None)
        if callable(find):
            return find(version_id)
        return next(
            (v for v in getattr(self.manifest, "versions", []) if v.id == version_id),
            None,
        )

    def _update_latest_cards(self) -> None:
        """Fill the two shortcuts from ``manifest.latest`` (and the version it names, for the stamp)."""
        latest = getattr(self.manifest, "latest", None) or {}
        self._latest_icons = {}
        game_dir = self._game_dir()
        cache_root = block_textures.texture_cache_root(paths.launcher_dir())
        for kind in ("release", "snapshot"):
            version_id = (latest.get(kind) or "").strip()
            version = self._manifest_version(version_id)
            if version is None:
                # The id can be absent from the version array (or the array can be stale): the
                # shortcut still names what the manifest says is latest, just without a stamp/icon.
                icon = (
                    self._row_icon(version_id, kind, False, game_dir, cache_root)
                    if version_id
                    else QPixmap()
                )
                self._latest_icons[version_id] = icon
                self.latest_cards[kind].set_version(version_id, kind, "", icon)
                continue
            icon = self._row_icon(
                version.id, version.type, version.id in self._installed, game_dir, cache_root
            )
            self._latest_icons[version.id] = icon
            self.latest_cards[kind].set_version(
                version.id,
                version.type,
                human_datetime(version.release_time),
                icon,
            )

    def _install_latest(self, kind: str) -> None:
        latest = getattr(self.manifest, "latest", None) or {}
        version_id = (latest.get(kind) or "").strip()
        if version_id:
            self.begin_install(version_id)

    # ---------- the four folds: state and public reads ----------

    def group_of(self, version_id: str) -> str:
        """Which fold a version id belongs to (empty when the manifest does not hold it)."""
        version = self._manifest_version(version_id)
        return _group_of(version) if version is not None else ""

    def group_counts(self) -> dict[str, int]:
        """The count each fold header shows: every manifest version, regardless of the search box."""
        return {key: len(self._buckets.get(key, [])) for key in _GROUP_ORDER}

    def group_total(self, key: str) -> int:
        return len(self._buckets.get(key, []))

    def expanded_groups(self) -> list[str]:
        return [key for key in _GROUP_ORDER if key in self._expanded]

    def is_group_expanded(self, key: str) -> bool:
        return key in self._expanded

    def group_headers(self) -> dict[str, _GroupHeader]:
        return dict(self._headers)

    def toggle_group(self, key: str) -> None:
        """Open or close one fold (the header's click; also the keyboard's Space/Enter)."""
        if key not in self._buckets:
            return
        if key in self._expanded:
            self._expanded.discard(key)
            self._shown[key] = 0
        else:
            self._expanded.add(key)
            self._shown[key] = _GROUP_BATCH
        self._refilter()
        # A rebuild replaces the header widget the keyboard was standing on; put focus back on the
        # same fold, where the next Tab/arrow step continues from.
        header = self._headers.get(key)
        if header is not None:
            header.setFocus(Qt.FocusReason.OtherFocusReason)

    def expand_group(self, key: str) -> None:
        """Open a fold with its first batch of rows (idempotent; used by tests and begin_install)."""
        if key not in self._buckets:
            return
        if key not in self._expanded:
            self._expanded.add(key)
            self._shown[key] = _GROUP_BATCH
            self._refilter()
        elif self._shown.get(key, 0) < _GROUP_BATCH:
            self._shown[key] = min(_GROUP_BATCH, len(self._buckets[key]))
            self._refilter()

    def collapse_group(self, key: str) -> None:
        if key in self._expanded:
            self._expanded.discard(key)
            self._shown[key] = 0
            self._refilter()

    def load_more(self, key: str | None = None) -> None:
        """Build another 50 rows of one fold (``None`` = the flat search list).

        The batch is inserted where it belongs instead of rebuilding the list, which leaves every
        existing card, its selection and the scroll offset untouched. With the list's batched layout
        mode a 50-row batch measured ~60ms from 100 to 742 rows; rebuilding the list at 742 rows
        measured ~850ms, and per-item insertion without the batched layout reached seconds.
        """
        if key is None:
            if not self._search_needle or self._search_total <= self._search_shown:
                return
            old = self._search_shown
            new = min(self._search_total, old + _GROUP_BATCH)
            self._search_shown = new
            self._append_search_batch(old, new)
            focus = self._load_more_rows.get(None)
        else:
            if self._search_needle or key not in self._buckets:
                return
            total = len(self._buckets[key])
            old = self._shown.get(key, 0)
            if old >= total:
                return
            new = min(total, old + _GROUP_BATCH)
            self._shown[key] = new
            self._append_group_batch(key, old, new)
            focus = self._load_more_rows.get(key)
        if focus is not None:
            focus.setFocus(Qt.FocusReason.OtherFocusReason)
        elif self.rows:
            # No batch left: the control disappears, so continue on the last row it revealed.
            self.rows[-1].install.setFocus(Qt.FocusReason.OtherFocusReason)

    def _drop_load_more(self, key: str | None) -> None:
        """Remove one fold's "load more" item before its replacement is inserted."""
        item = self._load_more_items.pop(key, None)
        self._load_more_rows.pop(key, None)
        if item is None:
            return
        row = self.list.row(item)
        if row >= 0:
            self.list.takeItem(row)

    def _append_group_batch(self, key: str, old: int, new: int) -> None:
        """Insert rows ``old:new`` of a fold right after its header (before its load-more row)."""
        header = self._header_items.get(key)
        if header is None:
            return
        index = self.list.row(header) + 1 + old
        game_dir = self._game_dir()
        cache_root = block_textures.texture_cache_root(paths.launcher_dir())
        created: list[tuple[QListWidgetItem, _VersionRow]] = []
        for version, category in self._buckets.get(key, [])[old:new]:
            created.append(
                self._make_row_item(version, category, "", game_dir, cache_root)
            )
        # One batch, one layout pass: inserting and installing 50 items one call at a time re-laid the
        # list after every step (measured 111ms for the second batch, 236ms for the fifth). Blocking
        # updates and signals for the duration, plus the list's batched layout mode, keeps a batch at
        # ~60ms whatever is already built.
        self.list.blockSignals(True)
        self.list.setUpdatesEnabled(False)
        try:
            self._drop_load_more(key)
            insert_at = index
            for item, _row in created:
                self.list.insertItem(insert_at, item)
                insert_at += 1
            for item, row in created:
                self.list.setItemWidget(item, row)
                row.setVisible(True)
                self.rows.append(row)
            if new < len(self._buckets.get(key, [])):
                item, row = self._new_load_more_item(key, add=False)
                self.list.insertItem(index + len(created), item)
                self.list.setItemWidget(item, row)
                row.setVisible(True)
        finally:
            self.list.setUpdatesEnabled(True)
            self.list.blockSignals(False)
        self._sync_row_selection(self._selected_id())
        QTimer.singleShot(0, self.list, self.list.sync_item_widths)

    def _append_search_batch(self, old: int, new: int) -> None:
        """Append the next ``old:new`` matches to the flat search list."""
        matches = self._search_matches(self._search_needle)
        game_dir = self._game_dir()
        cache_root = block_textures.texture_cache_root(paths.launcher_dir())
        created: list[tuple[QListWidgetItem, _VersionRow]] = []
        for version, category in matches[old:new]:
            created.append(
                self._make_row_item(
                    version, category, self._search_needle, game_dir, cache_root
                )
            )
        self.list.blockSignals(True)
        self.list.setUpdatesEnabled(False)
        try:
            self._drop_load_more(None)
            for item, _row in created:
                self.list.addItem(item)
            for item, row in created:
                self.list.setItemWidget(item, row)
                row.setVisible(True)
                self.rows.append(row)
            if new < self._search_total:
                self._new_load_more_item(None)
        finally:
            self.list.setUpdatesEnabled(True)
            self.list.blockSignals(False)
        self._sync_row_selection(self._selected_id())
        QTimer.singleShot(0, self.list, self.list.sync_item_widths)

    def _ensure_version_visible(self, version_id: str) -> None:
        """Open the fold (and build enough rows) that holds ``version_id``, so its row exists.

        The latest cards name versions that may sit in a fold the user has never opened. Entering the
        install step from one of them must behave like entering it from a row - including the row's
        inline progress once the download starts - so the fold is opened here, with only the batches
        needed to reach that row.
        """
        needle = self.search_edit.text().strip().lower()
        if needle:
            matches = self._search_matches(needle)
            index = next(
                (i for i, (version, _c) in enumerate(matches) if version.id == version_id),
                -1,
            )
            if index < 0:
                return
            if index >= self._search_shown:
                self._search_shown = (index // _GROUP_BATCH + 1) * _GROUP_BATCH
                self._rebuild(needle)
            return
        group = self.group_of(version_id)
        if not group:
            return
        items = self._buckets[group]
        index = next(
            (i for i, (version, _c) in enumerate(items) if version.id == version_id), -1
        )
        if index < 0:
            return
        changed = False
        if group not in self._expanded:
            self._expanded.add(group)
            self._shown[group] = _GROUP_BATCH
            changed = True
        elif index >= self._shown.get(group, 0):
            self._shown[group] = (index // _GROUP_BATCH + 1) * _GROUP_BATCH
            changed = True
        if changed:
            self._rebuild("")

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
        # A shortcut card can name a version inside a closed fold; open exactly enough of it that the
        # row exists, so the download's inline progress has a row to land on when the step returns.
        self._ensure_version_visible(version_id)
        self._list_scroll = self.list.verticalScrollBar().value()
        self._list_row = self.list.currentRow()
        index = self._row_index(version_id)
        if index >= 0:
            self.list.setCurrentRow(index)
        row = self._row_for(version_id)
        self.step.prepare(
            version_id,
            row.icon() if row is not None else self._latest_icons.get(version_id),
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

    # ---------- the list: four folds, or one flat search result ----------

    def _build_buckets(self) -> None:
        """Sort the whole manifest into the four folds, newest first inside each one."""
        buckets: dict[str, list[tuple[object, str]]] = {key: [] for key in _GROUP_ORDER}
        for version in self.manifest.versions:
            buckets[_group_of(version)].append(
                (version, version_category(version.id, version.type))
            )
        for key in _GROUP_ORDER:
            buckets[key].sort(
                key=lambda pair: pair[0].release_time or "", reverse=True
            )
        self._buckets = buckets

    def _refilter(self) -> None:
        """Rebuild the list for the current search box: folds when empty, flat results when not."""
        if self.manifest is None:
            return
        self._build_buckets()
        self._rebuild(self.search_edit.text().strip().lower())
        self._update_result_line()

    def _search_matches(self, needle: str) -> list[tuple[object, str]]:
        """Every version whose id contains ``needle``, newest first across all four folds."""
        matches = [
            (version, category)
            for key in _GROUP_ORDER
            for version, category in self._buckets.get(key, [])
            if needle in version.id.lower()
        ]
        matches.sort(key=lambda pair: pair[0].release_time or "", reverse=True)
        return matches

    def _rebuild(self, needle: str) -> None:
        """Build the items the current mode needs, and nothing else.

        In fold mode a closed fold contributes one 52px header item and no rows at all - that is what
        keeps ~800 snapshots free until somebody opens the fold. An open fold contributes at most its
        built batch (50 rows plus, while more remain, one "load more" item). Search mode builds a flat
        batch of matches instead. Every count the header shows is read from the full bucket, never from
        the rows that were built.

        Items are all added before any item widget is installed, for the reason the old ``_populate``
        documented: interleaving the two makes ``addItem`` re-lay-out every widget already in the list,
        which measured quadratic on this page. Signals are blocked while the list is rebuilt so a
        half-filled list never reports a selection.
        """
        current = self._selected_id()
        scroll = self.list.verticalScrollBar().value()
        if needle != self._search_needle:
            # A new query (or leaving search) is a new list: start it at the top. A rebuild of the
            # same mode - expand, collapse, load more, a refresh - keeps the user's position.
            scroll = 0
        self._search_needle = needle
        self._auto_groups = []
        if needle:
            # The first search builds its first batch; a later keystroke keeps however many batches
            # the user has already asked for. Clearing the box resets the batch for the next search.
            if self._search_shown <= 0:
                self._search_shown = _GROUP_BATCH
            # A hit inside a fold opens that fold (the flat result list below still carries every
            # hit): the fold's state is what makes the auto-expansion real, and the result line names
            # the folds it had to open. Clearing the box closes exactly those again.
            for key in _GROUP_ORDER:
                if any(needle in version.id.lower() for version, _c in self._buckets.get(key, [])):
                    self._auto_groups.append(key)
                    if key not in self._expanded:
                        self._expanded.add(key)
                        self._auto_opened.add(key)
                    if self._shown.get(key, 0) < _GROUP_BATCH:
                        self._shown[key] = _GROUP_BATCH
        else:
            # Clearing the box returns to the fold view: close only the folds this search opened.
            for key in self._auto_opened:
                self._expanded.discard(key)
                self._shown[key] = 0
            self._auto_opened.clear()
            self._search_shown = 0

        # Drop the Python references before clear(): clear() would otherwise let the row list point at
        # C++ widgets that are being destroyed, and the selection handler walks it.
        self.rows = []
        self._meta = {}
        self._headers = {}
        self._header_items = {}
        self._load_more_rows = {}
        self._load_more_items = {}
        game_dir = self._game_dir()
        cache_root = block_textures.texture_cache_root(paths.launcher_dir())
        pending: list[tuple[QListWidgetItem, QWidget]] = []
        self.list.blockSignals(True)
        self.list.setUpdatesEnabled(False)
        try:
            self.list.clear()
            if needle:
                matches = self._search_matches(needle)
                self._search_total = len(matches)
                for version, category in matches[: self._search_shown]:
                    pending.append(
                        self._new_row_item(version, category, needle, game_dir, cache_root)
                    )
                if len(matches) > self._search_shown:
                    pending.append(self._new_load_more_item(None))
            else:
                self._search_total = 0
                for key in _GROUP_ORDER:
                    pending.append(self._new_header_item(key))
                    if key not in self._expanded:
                        continue
                    items = self._buckets.get(key, [])
                    limit = min(self._shown.get(key, 0), len(items))
                    for version, category in items[:limit]:
                        pending.append(
                            self._new_row_item(version, category, "", game_dir, cache_root)
                        )
                    if limit < len(items):
                        pending.append(self._new_load_more_item(key))
            for item, widget in pending:
                self.list.setItemWidget(item, widget)
                # The page can be rebuilt while it is hidden (a signal from another page). Qt then
                # keeps the item widget hidden until another show/layout pass, which leaves the focus
                # chain without the fold headers the keyboard walk needs. Showing the widget here is
                # what makes it a real, focusable control as soon as its page is shown.
                widget.setVisible(True)
                if isinstance(widget, _VersionRow):
                    self.rows.append(widget)
        finally:
            self.list.setUpdatesEnabled(True)
            self.list.blockSignals(False)

        ids = self.version_ids()
        # Never select a row on the user's behalf: an automatic selection would make the bottom action
        # bar look usable the moment a fold opens (or a search returns hits), which is exactly the
        # "selected-looking but nobody selected it" state the bar now waits for a real selection to
        # avoid. A rebuild only keeps a selection that is still visible; otherwise the bar hides.
        selected = current if current in ids else None
        if selected is not None:
            self.list.setCurrentRow(self._row_index(selected))
        else:
            self.list.setCurrentRow(-1)
        self._sync_row_selection(selected)
        # A rebuild during a download (a search keystroke) hands the new row the running progress.
        self._sync_install_row()
        self._restore_scroll(scroll)
        # The page may still be inside its first layout pass here; re-apply the viewport width once
        # the widget hierarchy has settled (see _VersionList).
        QTimer.singleShot(0, self.list, self.list.sync_item_widths)

    def _new_row_item(
        self,
        version,
        category: str,
        highlight: str,
        game_dir: Path,
        cache_root: Path,
    ) -> tuple[QListWidgetItem, _VersionRow]:
        """One dense version card as a list item (the card is still ``_VersionRow``, unchanged)."""
        item, row = self._make_row_item(version, category, highlight, game_dir, cache_root)
        self.list.addItem(item)
        return item, row

    def _make_row_item(
        self,
        version,
        category: str,
        highlight: str,
        game_dir: Path,
        cache_root: Path,
    ) -> tuple[QListWidgetItem, _VersionRow]:
        """Build (but do not insert) the item and the card of one version."""
        type_label = tr(_TYPE_LABELS.get(category, "versions.type.release"))
        date_text = human_date(version.release_time)
        installed = version.id in self._installed
        status_text = tr(
            "versions.status.installed" if installed else "versions.status.not_installed"
        )
        self._meta[version.id] = (type_label, date_text, status_text)
        # The item's own text is the accessible/testable representation; the card paints the
        # human-readable form on top of it.
        item = QListWidgetItem(
            " · ".join(part for part in (version.id, type_label, date_text) if part)
        )
        item.setData(Qt.UserRole, version.id)
        item.setSizeHint(QSize(0, _VersionRow.SLOT_HEIGHT))
        row = _VersionRow(
            version.id,
            type_label,
            date_text,
            status_text,
            installed,
            self._row_icon(version.id, version.type, installed, game_dir, cache_root),
            on_select=lambda version_id=version.id: self._select_version(version_id),
            on_install=lambda version_id=version.id: self._on_row_install(version_id),
            highlight=highlight,
        )
        return item, row

    def _new_header_item(self, key: str) -> tuple[QListWidgetItem, _GroupHeader]:
        title = tr(_GROUP_TITLE_KEYS[key])
        count = len(self._buckets.get(key, []))
        header = _GroupHeader(
            key,
            title,
            count,
            key in self._expanded,
            on_toggle=self.toggle_group,
        )
        self._headers[key] = header
        item = QListWidgetItem(f"{title} ({count})")
        # A fold header is a control of its own (its widget is the focus stop and the click target);
        # the list item must not also become a selectable version row.
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setSizeHint(QSize(0, _GroupHeader.SLOT_HEIGHT))
        self.list.addItem(item)
        self._header_items[key] = item
        return item, header

    def _new_load_more_item(
        self, key: str | None, *, add: bool = True
    ) -> tuple[QListWidgetItem, _LoadMoreRow]:
        if key is None:
            remaining = max(0, self._search_total - self._search_shown)
            on_load = lambda: self.load_more(None)
        else:
            remaining = max(0, len(self._buckets.get(key, [])) - self._shown.get(key, 0))
            on_load = lambda group=key: self.load_more(group)
        text = tr("versions.group.more", remaining)
        row = _LoadMoreRow(text, on_load=on_load)
        self._load_more_rows[key] = row
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setSizeHint(QSize(0, _LoadMoreRow.SLOT_HEIGHT))
        if add:
            self.list.addItem(item)
        self._load_more_items[key] = item
        return item, row

    def _update_result_line(self) -> None:
        """The line above the list: "N 个结果" while searching, the manifest total otherwise."""
        total = len(self.manifest.versions) if self.manifest is not None else 0
        if self._search_needle:
            text = tr("versions.search.results", self._search_total)
            if self._auto_groups:
                names = " / ".join(tr(_GROUP_TITLE_KEYS[key]) for key in self._auto_groups)
                text += " · " + tr("versions.search.expanded", names)
            self.result_count.setText(text)
            self.result_count.setVisible(True)
            version_id = self._selected_id()
            # Without a selection the line below the list is not a second "select a version" hint:
            # the action bar's own hint says that. It shows the manifest total, as it does in the
            # fold view, so the two lines do not repeat the same instruction.
            self.info_label.setText(
                tr("versions.info.selected", version_id)
                if version_id
                else tr("versions.msg.shown", total)
            )
            return
        self.result_count.clear()
        self.result_count.setVisible(False)
        self.info_label.setText(tr("versions.msg.shown", total))

    def _restore_scroll(self, value: int) -> None:
        """Put the scroll offset back after a rebuild, once the new rows have a layout."""
        bar = self.list.verticalScrollBar()

        def apply() -> None:
            bar.setValue(min(value, bar.maximum()))

        apply()
        # The bar is the context object: if the page is torn down first (a language switch rebuilds
        # every page), Qt drops the callback instead of calling into a deleted scroll bar.
        QTimer.singleShot(0, bar, apply)

    def _select_version(self, version_id: str) -> None:
        index = self._row_index(version_id)
        if index >= 0:
            self.list.setCurrentRow(index)

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

    def _sync_row_selection(self, version_id: str | None) -> None:
        for row in self.rows:
            row.set_selected(row.version_id == version_id)
        self._sync_actions(version_id)

    def _sync_actions(self, version_id: str | None) -> None:
        """Show the four action buttons only while a row is selected, and close the same hole.

        The buttons act on one version; with no row selected there is nothing for them to act on, and
        a clickable button that can only answer "please select a version" is the dead control this
        page had. Hiding them also takes them out of the Tab chain for free (Qt skips hidden
        widgets), and the hint in their place says how to make them appear.

        ``install_button``'s enabled state is deliberately not touched: the frozen install flow uses
        it as its busy signal (``_start_from_step`` checks it, ``on_finished`` hands it back), so
        showing the button is this page's decision while enabling it stays the flow's.
        """
        selected = version_id is not None
        for button in (
            self.install_button,
            self.open_instance_button,
            self.detail_button,
            self.launch_button,
        ):
            button.setVisible(selected)
        self.action_hint.setVisible(not selected)
        self.open_instance_button.setEnabled(selected)
        self.detail_button.setEnabled(selected)
        self.launch_button.setEnabled(selected)

    def _on_select(self, row: int = -1) -> None:
        version_id = self._selected_id()
        self._sync_row_selection(version_id)
        if version_id is None:
            # The line below the list used to keep the last "已选择: x" after the row was collapsed
            # away; put back whatever this mode shows without a selection (the result hint in search,
            # the manifest total in the fold view).
            self._update_result_line()
            return
        self.info_label.setText(tr("versions.info.selected", version_id))

    # ---------- row metadata (what the cards paint, addressable for tests) ----------

    def version_ids(self) -> list[str]:
        """The visible version ids, top to bottom - the list's own order, headers left out."""
        ids: list[str] = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            version_id = item.data(Qt.UserRole)
            if version_id is not None:
                ids.append(version_id)
        return ids

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
