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

"""Instance detail page: a 200px, ten-entry sidebar next to the instance content.

The panel owns everything that acts on one instance (launching, mods, resource
packs, shaders, saves, screenshots, servers, settings, diagnosis). It never touches widgets from a
worker thread: every background job reports through @gui.workers.run_in_background with
bound-method callbacks, so a result that arrives after the page was torn down is
dropped by the owner guard instead of dereferencing a freed C++ object.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
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
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import block_icons, i18n, inwindow_dialog, nav_icons, paint_utils, theme
from gui.accessibility_launch import (
    prepare_launch_accessibility,
    trigger_source_for_click,
)
from gui.errors import show_fatal
from gui.pages.instance_files import (
    backup_save,
    display_file_name,
    format_day,
    mod_icon_bytes,
    pack_icon_bytes,
    read_enabled_resource_packs,
    scan_content_files,
    scan_saves,
)
from gui.pages.screenshots_page import ScreenshotsPage
from gui.pages.servers_page import ServersPage
from gui.pages.versions_install_flow import (
    install_selection,
    loader_install_options,
)
from gui.tasks import CANCELLED, task_center_of
from gui.view_state import STATE_FILENAME
from gui.widgets import (
    PAGE_HEADER_GAP,
    ElidedLabel,
    EmptyState,
    NoWheelDoubleSpinBox,
    add_search_icon,
    apply_no_focus_outline,
    build_download_progress,
    disable_keeping_focus,
    set_app_status,
    style_form,
    style_page_layout,
    widget_alive,
)
from gui.workers import ProgressBridge, RateTracker, run_in_background
from launcher import config, paths
from launcher.accessibility import TriggerSource
from launcher.auth import AccountStore
from launcher.install import list_installed_versions
from launcher.instances import (
    ISOLATION_FOLLOW,
    ISOLATION_OFF,
    ISOLATION_ON,
    SWITCH_LOADER,
    SWITCH_VERSION,
    InstancesError,
    SwitchOutcome,
    core_profile_id,
    delete_instance,
    display_version_name,
    export_instance,
    get_instance,
    instance_dir,
    legacy_instances_dir,
    profile_identity,
    rename_instance,
    reset_instance_settings,
    resolve_instance,
    set_instance_icon,
    set_instance_settings,
    switch_candidates,
    switch_instance_version,
    update_instance,
    update_instance_note,
)
from launcher.launch import (
    find_new_crash_reports,
    prepare_launch,
    resolve_launch_account,
    run_process,
)
from launcher.meta import block_textures, fetch_manifest, version_category
from launcher.meta.version import version_json_path
from launcher.mods import identity
from launcher.mods.local import install_mod_file, scan_mods, set_mod_enabled
from launcher.mods.modrinth import delete_installed_content, resolve_content_dir
from launcher.mods.zh_names import zh_name
from launcher.net import downloader, http_cache
from launcher.net.downloader import DownloadCancelled
from launcher.schematic import SchematicFile, scan_schematics, schematic_folder

tr = i18n.tr
log = logging.getLogger(__name__)

# Stacked-page order. The sidebar owns the visual order; the stack is a sequence of pages only.
TAB_OVERVIEW = 0
TAB_MODS = 1
TAB_RESOURCEPACKS = 2
TAB_SHADERPACKS = 3
TAB_SCHEMATICS = 4
TAB_SAVES = 5
TAB_SCREENSHOTS = 6
TAB_SERVERS = 7
TAB_MODIFY = 8
TAB_EXPORT = 9
TAB_DIAGNOSIS = 10
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
    ("schematics", TAB_SCHEMATICS, "schematic", "instances.detail.card.schematic"),
    ("saves", TAB_SAVES, "saves", "instances.detail.nav.saves"),
    # 截图 / 服务器 are instance data (the game writes both into launch_dir), so they sit with the
    # other game resources rather than in a third group: two groups keep the rail short enough for
    # the 610px floor. Their labels are the old top-level nav keys, still translated everywhere.
    ("screenshots", TAB_SCREENSHOTS, "screenshots", "nav.screenshots"),
    ("servers", TAB_SERVERS, "servers", "nav.servers"),
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

#: One icon slot per resource/shader row: the pack's own pack.png when it has one, otherwise the
#: bundled Lucide "package" cube. Both are drawn into the same 32px square, so a row without artwork
#: never shifts its name left.
CONTENT_ICON_SIZE = 32

#: The four content lists are two-line cards, not tables: a 64px card holds one 32px icon, a title
#: line and a detail line, with an 8px gap between two cards (the list slot is therefore 72px). The
#: numbers are the design's, not derived from a font: a translated title may elide, the row height
#: stays put, which is what keeps the list from re-flowing between languages.
CONTENT_ROW_HEIGHT = 64
CONTENT_ROW_SLOT = CONTENT_ROW_HEIGHT + 8
CONTENT_ROW_RADIUS = 8
CONTENT_ROW_PADDING = 12
CONTENT_ROW_GAP = 12
CONTENT_BADGE_HEIGHT = 20
CONTENT_TAG_HEIGHT = 18
#: The mods list's enable switch: 20px, vertically centred, at the right edge of the card.
CONTENT_CHECK_SIZE = 20
#: The list a two-line card lives in never scrolls sideways; the text elides instead.
CARD_TEXT_FONT = 14
CARD_DETAIL_FONT = 12
CARD_TAG_FONT = 11
CARD_BADGE_FONT = 11


def online_identity_enabled() -> bool:
    """The user's online-recognition switch, default on, stored beside the column widths.

    It is read here rather than from :mod:`launcher.config` because the setting belongs to the
    interface (it only decides whether a page may talk to Modrinth), and because a QSettings file is
    the one store the settings page can write without changing the on-disk config schema. Any failure
    to read it means "on": a missing preference file must not silently disable a feature.
    """
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings(
            str(paths.launcher_dir() / STATE_FILENAME), QSettings.Format.IniFormat
        )
        value = settings.value(identity.ONLINE_SETTING_KEY, True)
    except Exception:  # noqa: BLE001 - a broken settings file must not break the page
        return True
    if isinstance(value, str):
        return value.strip().casefold() not in ("false", "0", "off", "no", "")
    return bool(value)


def human_size(size: int) -> str:
    """Format a byte count with the launcher's unit strings."""
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} " + tr("unit.gb")
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} " + tr("unit.mb")
    if size >= 1 << 10:
        return f"{size / (1 << 10):.0f} " + tr("unit.kb")
    return f"{size} " + tr("unit.bytes")


def _safe_export_filename(name: str) -> str:
    """An instance name as a Windows-legal file name for the save dialog's suggestion.

    The dialog's suggestion is the only place this matters: the user still picks the real path, and
    a name with ``:`` or ``/`` in it would otherwise make the prefilled dialog unusable.
    """
    cleaned = "".join(
        "-" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in str(name or "")
    )
    return cleaned.strip(" .") or "modpack"


def _scaled_icon_pixmap(image: QImage) -> QPixmap:
    """Centre one pack.png in an exact :data:`CONTENT_ICON_SIZE` square, DPR-aware."""
    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    ratio = max(1.0, float(screen.devicePixelRatio()) if screen is not None else 1.0)
    box = max(1, round(CONTENT_ICON_SIZE * ratio))
    scaled = image.scaled(
        box,
        box,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    # The scaled raster is in device pixels; tagging it with the ratio makes drawImage place it at its
    # logical size (a 32px slot), which keeps the icon sharp instead of upscaling it again.
    scaled.setDevicePixelRatio(ratio)
    pixmap = QPixmap(box, box)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.drawImage(
        QPointF(
            (box - scaled.width()) / (2.0 * ratio),
            (box - scaled.height()) / (2.0 * ratio),
        ),
        scaled,
    )
    painter.end()
    return pixmap


def content_icon(path: Path) -> QIcon:
    """The row icon for one resource or shader pack: its own pack.png, else a neutral cube.

    Reading is local (:func:`gui.pages.instance_files.pack_icon_bytes`): one zip entry or the folder's
    sibling ``pack.png``, never the network. A missing, corrupt or non-image pack.png falls back to
    :func:`gui.paint_utils.wire_cube_pixmap` - a thin FG_HINT outline, deliberately not a letter tile
    and not the accent colour, because it is a "no artwork" placeholder, not a status.
    """
    data = pack_icon_bytes(path)
    if data:
        image = QImage.fromData(data)
        if not image.isNull():
            return QIcon(_scaled_icon_pixmap(image))
    return QIcon(paint_utils.wire_cube_pixmap(CONTENT_ICON_SIZE))


def pixmap_from_image_bytes(data: bytes | None) -> QPixmap | None:
    """Decode stored/online image bytes into the 32px row slot, or None when they are not an image.

    This is also the corruption guard of the icon cache: bytes that Qt refuses to decode are treated
    exactly like "no icon" by the caller, which then asks for a fresh download instead of painting a
    blank square.
    """
    if not data:
        return None
    image = QImage.fromData(data)
    if image.isNull():
        return None
    return _scaled_icon_pixmap(image)


def _elided(metrics: QFontMetrics, text: str, width: float) -> str:
    """Elide on the right; a non-positive width yields an empty string instead of a Qt warning."""
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, int(width)))


class _ContentRow(QWidget):
    """One two-line card in a content list: 32px icon, title line, detail line, right-aligned marks.

    Painted rather than composed of labels for the reason the resource rows on the mods page are: a
    list of a hundred files must elide its text on one line (a QLabel either wraps or reports a width
    wider than it paints, which the overflow audit flags), and one widget per row keeps a theme swap
    cheap. The row itself takes no focus - the list is the single keyboard stop and the arrow keys
    move its current row - so a page with sixty mods does not add sixty Tab stops.

    The first line is the display name plus the local identity (mod id, installed version); the
    second is the online identity (tag pills and the summary) or the state that replaced it ("not
    recognized", "online recognition is off"). The right edge carries the "updatable" marker, and the
    resource list's enabled state; the mods list additionally owns a checkbox that renames the jar,
    exactly like the checkable column it replaces.
    """

    def __init__(
        self,
        title: str,
        *,
        key: str = "",
        meta: str = "",
        detail: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.title = title
        self.meta = meta
        self.detail = detail
        self.tags: tuple[str, ...] = ()
        self.description = ""
        self.badge = ""
        self.status = ""
        self.kind = ""
        self.installed_version = ""
        self.icon_url = ""
        #: The untouched on-disk name when the title had Minecraft formatting codes stripped; the
        #: tooltip carries it so the file a delete acts on stays readable.
        self.raw_name = ""
        #: True when the picture came from the file on disk (a jar's own logo, a pack.png, a world's
        #: screenshot): that artwork then wins over anything the online lookup could offer.
        self.artwork_is_local = False
        self.check: QCheckBox | None = None
        self._icon: QPixmap | None = None
        self._selected = False
        self._hover = False
        self._select_handler = None
        self._check_handler = None
        #: The list item this row is installed on (set by _ContentList.add_row), so a late online
        #: answer can also refresh the text a screen reader reads from the item.
        self.item: QListWidgetItem | None = None
        self.setFixedHeight(CONTENT_ROW_SLOT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self._sync_accessible_text()

    # ---------- data ----------

    def _sync_accessible_text(self) -> None:
        parts = [self.title]
        if self.meta:
            parts.append(self.meta)
        if self.detail:
            parts.append(self.detail)
        text = " · ".join(part for part in parts if part)
        self.setAccessibleName(text)
        tooltip = text
        if self.raw_name and self.raw_name != self.title:
            tooltip += "\n" + self.raw_name
        self.setToolTip(tooltip)
        if self.item is not None:
            try:
                # The item paints nothing (the widget paints the card), so its visible text stays
                # empty; the accessible text is what a screen reader reads from the item.
                self.item.setData(Qt.ItemDataRole.AccessibleTextRole, self.accessible_text())
            except RuntimeError:  # the item is already gone with its list
                self.item = None

    def accessible_text(self) -> str:
        """What a list item (and a screen reader reading the item) says about this row."""
        lines = [self.title]
        if self.meta:
            lines.append(self.meta)
        if self.tags:
            lines.append(" ".join(self.tags))
        if self.description:
            lines.append(self.description)
        elif self.detail:
            lines.append(self.detail)
        return "\n".join(line for line in lines if line)

    def apply(
        self,
        *,
        title: str | None = None,
        meta: str | None = None,
        detail: str | None = None,
        tags: tuple[str, ...] | None = None,
        description: str | None = None,
        badge: str | None = None,
        status: str | None = None,
    ) -> None:
        """Replace only the fields the caller names, then repaint once."""
        changed = False
        for name, value in (
            ("title", title),
            ("meta", meta),
            ("detail", detail),
            ("description", description),
            ("badge", badge),
            ("status", status),
        ):
            if value is not None and getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if tags is not None and self.tags != tags:
            self.tags = tags
            changed = True
        if changed:
            self._sync_accessible_text()
            self.update()

    def set_icon_pixmap(self, pixmap: QPixmap | None, *, local: bool = False) -> None:
        self._icon = pixmap
        if local and pixmap is not None:
            self.artwork_is_local = True
        self.update()

    def icon_pixmap(self) -> QPixmap | None:
        """The artwork currently drawn (the online/local picture), or None for the neutral cube."""
        return self._icon

    def painted_icon_pixmap(self) -> QPixmap:
        """What the row actually draws in its 32px slot: the artwork, else the neutral wire cube."""
        return self._icon or paint_utils.wire_cube_pixmap(CONTENT_ICON_SIZE)

    def set_select_handler(self, handler) -> None:
        self._select_handler = handler

    def add_checkbox(self, checked: bool, handler) -> QCheckBox:
        """Give the row its own enable switch (the mods list); the list stays the focus stop."""
        self.check = QCheckBox(self)
        self.check.setObjectName("contentRowCheck")
        self.check.setFixedSize(CONTENT_CHECK_SIZE, CONTENT_CHECK_SIZE)
        self.check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.check.setChecked(bool(checked))
        self.check.setToolTip(self.title)
        self._check_handler = handler
        self.check.toggled.connect(self._on_check_toggled)
        self._place_checkbox()
        return self.check

    def _on_check_toggled(self, checked: bool) -> None:
        if self._check_handler is not None:
            self._check_handler(checked)

    def _place_checkbox(self) -> None:
        if self.check is None:
            return
        self.check.move(
            max(0, self.width() - CONTENT_ROW_PADDING - CONTENT_CHECK_SIZE),
            (CONTENT_ROW_HEIGHT - CONTENT_CHECK_SIZE) // 2,
        )

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    # ---------- interaction ----------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._select_handler is not None:
            self._select_handler(self)
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_checkbox()

    # ---------- painting ----------

    def _tokens(self) -> dict[str, str]:
        return theme.current_tokens() or theme.theme_palette("light")

    @staticmethod
    def _pill_width(metrics: QFontMetrics, text: str) -> int:
        return metrics.horizontalAdvance(text) + 16

    def _paint_pill(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        background: QColor,
        foreground: QColor,
        font: QFont,
    ) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, rect.height() / 2.0, rect.height() / 2.0)
        painter.setFont(font)
        painter.setPen(foreground)
        metrics = QFontMetrics(font)
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignCenter),
            _elided(metrics, text, rect.width() - 10),
        )

    def paintEvent(self, event) -> None:
        tokens = self._tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, max(1.0, self.width() - 1.0), CONTENT_ROW_HEIGHT - 1.0)
        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        border = QColor(tokens.get("BORDER_LIST", "#c9d2dc"))
        if self._selected:
            border = QColor(tokens.get("ACCENT", "#35a06a"))
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
        elif self._hover:
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, CONTENT_ROW_RADIUS, CONTENT_ROW_RADIUS)

        icon_rect = QRectF(
            CONTENT_ROW_PADDING,
            (CONTENT_ROW_HEIGHT - CONTENT_ICON_SIZE) / 2.0,
            CONTENT_ICON_SIZE,
            CONTENT_ICON_SIZE,
        )
        clip = QPainterPath()
        clip.addRoundedRect(icon_rect, 6, 6)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(
            icon_rect.toRect(),
            self._icon or paint_utils.wire_cube_pixmap(CONTENT_ICON_SIZE),
        )
        painter.restore()

        text_left = CONTENT_ROW_PADDING + CONTENT_ICON_SIZE + CONTENT_ROW_GAP
        right = float(self.width() - CONTENT_ROW_PADDING)
        if self.check is not None:
            right = self.check.x() - CONTENT_ROW_GAP

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        section = QColor(tokens.get("BG_SECTION", "#f1f3f6"))

        badge_metrics = QFontMetrics(self._font(CARD_BADGE_FONT))
        badge_font = self._font(CARD_BADGE_FONT)
        update_background = QColor(tokens.get("ACCENT_BUTTON", "#35a06a"))
        update_foreground = QColor(tokens.get("FG_SELECTED", "#ffffff"))
        for text, background, foreground in (
            (self.badge, update_background, update_foreground),
            (self.status, section, hint_color),
        ):
            if not text:
                continue
            width = self._pill_width(badge_metrics, text)
            if right - width < text_left:
                continue
            rect = QRectF(right - width, (CONTENT_ROW_HEIGHT - CONTENT_BADGE_HEIGHT) / 2.0, width,
                          CONTENT_BADGE_HEIGHT)
            self._paint_pill(painter, rect, text, background, foreground, badge_font)
            right -= width + 6

        width = right - text_left
        if width <= 4:
            painter.end()
            return

        title_font = self._font(CARD_TEXT_FONT, bold=True)
        title_metrics = QFontMetrics(title_font)
        meta_font = self._font(CARD_DETAIL_FONT)
        meta_metrics = QFontMetrics(meta_font)
        meta = self.meta
        meta_width = min(
            float(meta_metrics.horizontalAdvance(meta)) if meta else 0.0,
            max(0.0, width * 0.55),
        )
        title_room = max(0.0, width - (meta_width + 8 if meta else 0.0))
        painter.setFont(title_font)
        painter.setPen(text_color)
        title = _elided(title_metrics, self.title, title_room)
        painter.drawText(
            QRectF(text_left, 8, title_room, 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title,
        )
        if meta and title_room < width:
            title_width = title_metrics.horizontalAdvance(title)
            meta_room = max(0.0, width - title_width - 8)
            painter.setFont(meta_font)
            painter.setPen(hint_color)
            painter.drawText(
                QRectF(text_left + title_width + 8, 10, meta_room, 18),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                _elided(meta_metrics, meta, meta_room),
            )

        cursor = float(text_left)
        tag_font = self._font(CARD_TAG_FONT)
        tag_metrics = QFontMetrics(tag_font)
        for tag in self.tags[:2]:
            tag_width = self._pill_width(tag_metrics, tag)
            if cursor + tag_width > right:
                break
            rect = QRectF(cursor, 36, tag_width, CONTENT_TAG_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(section)
            painter.drawRoundedRect(rect, 4, 4)
            painter.setFont(tag_font)
            painter.setPen(hint_color)
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignCenter),
                _elided(tag_metrics, tag, tag_width - 10),
            )
            cursor += tag_width + 6

        detail = self.description or self.detail
        if self.tags and detail:
            cursor += 2
        if detail and cursor < right:
            detail_metrics = QFontMetrics(meta_font)
            painter.setFont(meta_font)
            painter.setPen(hint_color)
            painter.drawText(
                QRectF(cursor, 36, right - cursor, 18),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                _elided(detail_metrics, detail, right - cursor),
            )
        painter.end()

    @staticmethod
    def _font(pixel_size: int, *, bold: bool = False) -> QFont:
        font = QFont()
        font.setPixelSize(pixel_size)
        if bold:
            font.setWeight(QFont.Weight.DemiBold)
        return font


class _ContentList(QListWidget):
    """The scrollable card list behind all four content pages.

    ``objectName("resourceList")`` reuses the stylesheet the mods page already defines for a card
    list: a transparent, borderless viewport whose cards paint themselves, plus a focus ring on the
    view. ``setItemWidget`` gives every row its own card; the list remains the single focus stop, so
    Up/Down move the selection and Space (in the mods list) toggles the current mod.

    A list that is laid out before its final width hands older item widgets a stale width; the
    viewport width is therefore re-applied on every resize. The empty-state label is an overlay over
    the viewport, so "this list is empty" is centred where the rows would be.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resourceList")
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        apply_no_focus_outline(self)
        self._rows: list[_ContentRow] = []
        self._empty_overlay: QLabel | None = None
        self._space_handler = None
        self.jar_drop_handler = None
        self.setAcceptDrops(True)
        self.currentRowChanged.connect(self._sync_selection)

    # ---------- rows ----------

    def rows(self) -> list[_ContentRow]:
        return list(self._rows)

    def add_row(self, row: _ContentRow) -> None:
        index = len(self._rows)
        self._rows.append(row)
        item = QListWidgetItem("")
        item.setData(Qt.ItemDataRole.AccessibleTextRole, row.accessible_text())
        item.setData(Qt.ItemDataRole.UserRole, row.key)
        item.setSizeHint(QSize(0, CONTENT_ROW_SLOT))
        self.addItem(item)
        self.setItemWidget(item, row)
        row.item = item
        row.set_select_handler(lambda _row, index=index: self.setCurrentRow(index))

    def clear_rows(self) -> None:
        # Drop the Python references before clear(): clear() destroys the item widgets, and a
        # currentRowChanged(-1) handler must not walk a list of already-deleted widgets.
        self._rows = []
        self.blockSignals(True)
        try:
            self.clear()
        finally:
            self.blockSignals(False)

    def set_space_handler(self, handler) -> None:
        self._space_handler = handler

    def set_jars_dropped_handler(self, handler) -> None:
        self.jar_drop_handler = handler

    def _sync_selection(self, current: int) -> None:
        for index, row in enumerate(self._rows):
            row.set_selected(index == current)

    # ---------- keyboard / drag and drop ----------

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space,) and self._space_handler is not None and self.count():
            self._space_handler()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _jar_urls(event) -> bool:
        data = event.mimeData()
        if not data.hasUrls():
            return False
        return any(
            url.isLocalFile() and url.toLocalFile().lower().endswith(".jar")
            for url in data.urls()
        )

    def dragEnterEvent(self, event) -> None:
        if self.jar_drop_handler is not None and self._jar_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self.jar_drop_handler is not None and self._jar_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        jars = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".jar")
        ]
        if jars and self.jar_drop_handler is not None:
            self.jar_drop_handler(jars)
            event.acceptProposedAction()
        else:
            event.ignore()

    # ---------- empty state / widths ----------

    def attach_empty_overlay(self, label: QLabel) -> None:
        self._empty_overlay = label
        label.setParent(self.viewport())
        label.setGeometry(self.viewport().rect())
        label.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.sync_item_widths()
        if self._empty_overlay is not None:
            self._empty_overlay.setGeometry(self.viewport().rect())

    def sync_item_widths(self) -> None:
        width = self.viewport().width()
        for index in range(self.count()):
            widget = self.itemWidget(self.item(index))
            if widget is not None and widget.width() != width:
                widget.resize(width, widget.height())


class _ContentTabs(QWidget):
    """The two tabs above a content list: ``全部 (N)`` and ``可更新 (M)``.

    Two checkable buttons in an exclusive group, the same ``categoryTab`` pill the versions page
    uses, so no stylesheet rule had to be added for them. ``M`` is always shown, including zero: the
    number is the answer to "is anything outdated?", and hiding the tab would hide the answer.
    """

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.all_button = QPushButton()
        self.update_button = QPushButton()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for button in (self.all_button, self.update_button):
            button.setObjectName("categoryTab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(button)
        self.all_button.setChecked(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.all_button)
        row.addWidget(self.update_button)
        row.addStretch(1)
        self.all_button.toggled.connect(self._on_toggled)
        self.update_button.toggled.connect(self._on_toggled)
        self.set_counts(0, 0)

    def _on_toggled(self, checked: bool) -> None:
        if checked:
            self.changed.emit()

    def mode(self) -> str:
        return "updates" if self.update_button.isChecked() else "all"

    def set_mode(self, mode: str) -> None:
        button = self.update_button if mode == "updates" else self.all_button
        if not button.isChecked():
            button.setChecked(True)

    def set_counts(self, total: int, updates: int) -> None:
        self.all_button.setText(tr("instances.detail.tab.all", total))
        self.update_button.setText(tr("instances.detail.tab.updatable", updates))
        self.all_button.setAccessibleName(tr("instances.detail.tab.all", total))
        self.update_button.setAccessibleName(tr("instances.detail.tab.updatable", updates))


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


#: The switch pickers: one 56px card per installed version, the same shape the versions page gives a
#: loader choice (a painted button), because it is the same kind of decision - pick one row, go.
CHOICE_ROW_HEIGHT = 56
CHOICE_ROW_PADDING = 16
CHOICE_ROW_RADIUS = 8
CHOICE_ROW_CHEVRON = 12
#: The picker's list scrolls instead of growing the card past the window.
CHOICE_LIST_MAX_HEIGHT = 320
#: Catalog rows built at a time. The official manifest holds ~900 versions; building one widget per
#: entry in a dialog would repeat the stall the versions page fixes with its batches. Search reads the
#: whole catalog, and the "load more" row appends the next batch, so no version is unreachable.
CHOICE_PAGE = 60

#: Version category -> the label the chooser prints under the id.
_VERSION_TYPE_LABELS = {
    "release": "versions.type.release",
    "snapshot": "versions.type.snapshot",
    "old_beta": "versions.type.old_beta",
    "old_alpha": "versions.type.old_alpha",
    "april_fools": "versions.group.april",
}

#: What the chooser rows stand for. ``installed`` means "point at it"; ``loader_version`` means
#: "download this loader through its own installer, then point at the profile it creates".
@dataclass(frozen=True)
class _SwitchChoice:
    version_id: str
    minecraft: str
    loader: str = ""
    installed: bool = False
    meta: str = ""
    loader_version: object | None = None

    @property
    def label(self) -> str:
        """``1.21.11 · Fabric 0.19.3`` - what the row's first line says."""
        loader = self.loader or tr("instances.detail.modify.vanilla")
        return f"{self.minecraft} · {loader}"

    @property
    def needs_install(self) -> bool:
        """True for anything this game folder cannot point at yet."""
        return not self.installed

    @property
    def is_loader_install(self) -> bool:
        return self.loader_version is not None

    def installer_identity(self) -> str:
        """The id or loader name the progress line can name before the profile exists."""
        if self.loader_version is not None:
            return self.loader
        return self.version_id


def _catalog_cache_path() -> Path:
    return paths.launcher_dir() / "cache" / "version_manifest.json"


#: The official manifest of this session, once a page or this dialog has read it.
_CATALOG_CACHE: list | None = None


def _catalog_from_cache() -> list | None:
    """The manifest entries when they are already on disk; never waits for the network.

    The choices dialog opens synchronously, so a cold cache must not block it on a 278KB request.
    The caller fills the installed profiles at once and asks a worker for the catalog; a warm cache
    (the normal case after the versions page has ever been opened) answers here instead, which is
    what makes the list complete on the first paint and deterministic in tests.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    cache = _catalog_cache_path()
    if not cache.is_file():
        return None
    try:
        manifest = fetch_manifest(cache_path=cache)
    except Exception:  # noqa: BLE001 - any unreadable cache falls back to the background fetch
        return None
    _CATALOG_CACHE = list(manifest.versions)
    return _CATALOG_CACHE


def fetch_catalog() -> list:
    """Fetch (or read) the official manifest for the chooser; the worker half of the same seam."""
    global _CATALOG_CACHE
    manifest = fetch_manifest(cache_path=_catalog_cache_path())
    _CATALOG_CACHE = list(manifest.versions)
    return _CATALOG_CACHE


def _catalog_choices(
    versions: list, *, installed_ids: set[str], current_version_id: str, current_minecraft: str
) -> list[_SwitchChoice]:
    """The official versions 修改 does not already offer as an installed profile.

    A version that is installed is already in the first part of the list with its real loader, so the
    catalog entry would only be a second row for the same folder. The current Minecraft version is
    excluded for the same reason the installed candidates exclude it: changing the loader is the other
    button, and this list is about a different game version.
    """
    out: list[_SwitchChoice] = []
    for version in versions:
        version_id = getattr(version, "id", "")
        if not version_id or version_id == current_version_id or version_id == current_minecraft:
            continue
        if version_id in installed_ids:
            continue
        category = version_category(version_id, getattr(version, "type", "release"))
        type_label = tr(_VERSION_TYPE_LABELS.get(category, "versions.type.release"))
        released = str(getattr(version, "release_time", "") or "")[:10]
        out.append(
            _SwitchChoice(
                version_id=version_id,
                minecraft=version_id,
                installed=False,
                meta=" · ".join(part for part in (type_label, released) if part),
            )
        )
    return out


def _loader_choices(
    options: list[tuple[str, object]], installed: list[_SwitchChoice], minecraft: str
) -> list[_SwitchChoice]:
    """The newest downloadable loader of each kind, minus the version already installed.

    ``installed`` carries the display labels of the profiles this game version already has ("Fabric
    0.19.3"); a downloadable row whose label matches is the same loader build, so offering it would
    be a download that changes nothing. A newer build than the installed one still gets a row.
    """
    from launcher.mods.loaders import display_loader_name

    installed_labels = {choice.loader for choice in installed if choice.loader}
    out: list[_SwitchChoice] = []
    for loader_key, loader_version in options:
        label = f"{display_loader_name(loader_key)} {loader_version.version}"
        if label in installed_labels:
            continue
        out.append(
            _SwitchChoice(
                version_id="",
                minecraft=minecraft,
                loader=label,
                installed=False,
                meta=tr("instances.detail.modify.status.download_loader"),
                loader_version=loader_version,
            )
        )
    return out


class _VersionChoiceRow(QAbstractButton):
    """One version the instance can be pointed at: its identity, its state and its profile id.

    A real ``QAbstractButton``: it is one Tab stop, Space/Enter activate it and assistive technology
    reads both lines as the button's name. Painted rather than composed for the reason the versions
    page paints its loader rows: the text has to elide on one line instead of reporting an unwrapped
    width, which would widen the dialog for the longest id.

    ``installed`` frames the row as "point here"; a catalog entry says "未安装 · 将下载" and the page
    treats it as a request to install first. The state is painted, not a second widget, so a list of
    hundreds of manifest entries stays one widget each.
    """

    def __init__(self, choice: _SwitchChoice, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.identity = choice
        self.choice = choice
        self.setObjectName("versionChoiceRow")
        self.setFixedHeight(CHOICE_ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hover = False
        self.setAccessibleName(f"{self.label_text()} · {self.status_text()}")
        self.setAccessibleDescription(self.meta_text())
        self.setToolTip(self.meta_text())

    def label_text(self) -> str:
        """``1.21.11 · Fabric 0.19.3`` - what the instance would run, in one line."""
        return self.choice.label

    def status_text(self) -> str:
        if not self.choice.installed:
            return tr("instances.detail.modify.status.download")
        return tr("instances.detail.modify.status.installed")

    def meta_text(self) -> str:
        """The second line: the profile id, or the catalog's type/date for a version to download."""
        return self.choice.meta or self.choice.version_id or self.choice.loader

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

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = theme.current_tokens() or theme.theme_palette("light")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, CHOICE_ROW_HEIGHT - 1.0)
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        if self._hover:
            border = QColor(tokens.get("BORDER_HOVER", border.name()))
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            border = QColor(tokens.get("FOCUS_RING", border.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, CHOICE_ROW_RADIUS, CHOICE_ROW_RADIUS)

        chevron_left = self.width() - CHOICE_ROW_PADDING - CHOICE_ROW_CHEVRON
        text_left = CHOICE_ROW_PADDING
        title_font = QFont(self.font())
        title_font.setPixelSize(15)
        title_font.setWeight(QFont.Weight.DemiBold)
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        status_font = QFont(self.font())
        status_font.setPixelSize(12)
        status_metrics = QFontMetrics(status_font)
        status_text = self.status_text()
        status_width = min(
            status_metrics.horizontalAdvance(status_text) + 2,
            max(0, chevron_left - text_left - 60),
        )
        status_left = chevron_left - 12 - status_width
        text_width = max(0, status_left - 12 - text_left)

        painter.setFont(title_font)
        painter.setPen(QColor(tokens.get("FG_TEXT", "#1f2a36")))
        painter.drawText(
            QRectF(text_left, 8, text_width, 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            QFontMetrics(title_font).elidedText(
                self.label_text(), Qt.TextElideMode.ElideRight, int(text_width)
            ),
        )
        painter.setFont(meta_font)
        painter.setPen(QColor(tokens.get("FG_HINT", "#4f6070")))
        painter.drawText(
            QRectF(text_left, 28, text_width, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            QFontMetrics(meta_font).elidedText(
                self.meta_text(), Qt.TextElideMode.ElideMiddle, int(text_width)
            ),
        )

        status_colour = (
            tokens.get("ACCENT_TEXT", tokens.get("FG_HINT", "#4f6070"))
            if self.choice.needs_install
            else tokens.get("FG_HINT", "#4f6070")
        )
        painter.setFont(status_font)
        painter.setPen(QColor(status_colour))
        painter.drawText(
            QRectF(status_left, 0, status_width, CHOICE_ROW_HEIGHT),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            status_metrics.elidedText(status_text, Qt.TextElideMode.ElideRight, int(status_width)),
        )

        # A drawn chevron: the UI font has no guaranteed U+203A, and a missing glyph would be a tofu
        # box in the middle of the row.
        pen = QPen(QColor(tokens.get("FG_HINT", "#4f6070")))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        middle = CHOICE_ROW_HEIGHT / 2.0
        inset = CHOICE_ROW_CHEVRON * 0.28
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(chevron_left + inset, middle - inset * 1.6),
                    QPointF(chevron_left + CHOICE_ROW_CHEVRON - inset, middle),
                    QPointF(chevron_left + inset, middle + inset * 1.6),
                ]
            )
        )
        painter.end()


class _VersionChoiceDialog(inwindow_dialog.InWindowDialog):
    """The version list: installed profiles first, then the official catalog, each with its state.

    Choosing a row closes the card and returns the choice; the page then asks the second question
    (the confirmation with the backup option). Nothing is written here - this dialog is a list, and
    the only thing it decides is which profile the user wants, or which version to download first.

    The catalog arrives after the dialog is already open (the page reads a warm disk cache
    synchronously and fetches a cold one in the background), so ``add_choices`` is part of the
    interface: the installed rows are usable immediately, and the official rows appear when they are
    known. The list never builds the whole manifest at once: it shows ``CHOICE_PAGE`` rows and a
    "load more" row, and the search field filters the whole catalog without rendering it.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        hint: str,
        choices: list[_SwitchChoice],
        empty_text: str,
        searchable: bool = False,
        loading_text: str = "",
    ) -> None:
        super().__init__(parent, title=title, severity="question")
        self.chosen: _SwitchChoice | None = None
        self.rows: list[_VersionChoiceRow] = []
        self.list_area: QScrollArea | None = None
        self.search_edit: QLineEdit | None = None
        self.more_button: QPushButton | None = None
        self._choices = list(choices)
        self._empty_text = empty_text
        self._limit = CHOICE_PAGE
        self._rebuilding = False

        # A plain label, not add_body(): the body helper builds a *scroll area* with stretch 1, which
        # took the whole card and left the list a clipped strip at the bottom (measured on the
        # 更换游戏版本 shot). The hint is one sentence; it needs no scroller.
        hint_label = QLabel(hint)
        hint_label.setObjectName("overlayBody")
        hint_label.setWordWrap(True)
        hint_label.setAccessibleName(title)
        self.card_layout.addWidget(hint_label)

        if searchable:
            self.search_edit = QLineEdit()
            self.search_edit.setObjectName("inWindowInput")
            self.search_edit.setPlaceholderText(tr("instances.detail.modify.search"))
            self.search_edit.setAccessibleName(tr("instances.detail.modify.search"))
            self.search_edit.setClearButtonEnabled(True)
            self.search_edit.textChanged.connect(self._on_search)
            self.card_layout.addWidget(self.search_edit)

        area = QScrollArea()
        area.setObjectName("overlayScroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        area.viewport().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.card_layout.addWidget(area)
        self.list_area = area

        self.empty_label = QLabel(empty_text)
        self.empty_label.setObjectName("overlayBody")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAccessibleName(empty_text)
        self.card_layout.addWidget(self.empty_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("overlayBody")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(bool(loading_text))
        self.status_label.setText(loading_text)
        self.card_layout.addWidget(self.status_label)

        self.add_buttons(
            [(tr("dialog.cancel"), inwindow_dialog.Outcome.REJECTED)], default=None
        )
        self._rebuild()

    # ---------- data ----------

    def add_choices(self, choices: list[_SwitchChoice]) -> None:
        """Append rows that arrived after the dialog opened (a catalog fetch / loader probe)."""
        if not choices:
            self._rebuild()
            return
        self._choices.extend(choices)
        self._rebuild()

    def set_status_text(self, text: str) -> None:
        """One line under the list: what is loading, failed, or could not be found."""
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def _on_search(self, _text: str) -> None:
        self._limit = CHOICE_PAGE
        self._rebuild()

    def _visible_choices(self) -> list[_SwitchChoice]:
        search_edit = self.search_edit
        needle = search_edit.text().strip().casefold() if search_edit is not None else ""
        if not needle:
            return list(self._choices)
        return [
            choice
            for choice in self._choices
            if needle in choice.version_id.casefold()
            or needle in choice.minecraft.casefold()
            or needle in choice.loader.casefold()
        ]

    # ---------- construction ----------

    def _rebuild(self) -> None:
        """Rebuild the visible page of rows (a catalog answer, a search keystroke, "load more")."""
        if self._rebuilding or self.list_area is None:
            return
        self._rebuilding = True
        try:
            focused = QApplication.focusWidget()
            focus_index = next(
                (index for index, row in enumerate(self.rows) if row is focused), None
            )
            visible = self._visible_choices()
            shown = visible[: self._limit]
            body = QWidget()
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            self.rows = []
            for choice in shown:
                row = _VersionChoiceRow(choice)
                row.clicked.connect(partial(self._choose, choice))
                layout.addWidget(row)
                self.rows.append(row)
            self.more_button = None
            if len(visible) > len(shown):
                self.more_button = QPushButton(
                    tr("instances.detail.modify.load_more", len(visible) - len(shown))
                )
                self.more_button.setObjectName("secondaryButton")
                self.more_button.setAccessibleName(self.more_button.text())
                self.more_button.clicked.connect(self._load_more)
                layout.addWidget(self.more_button)
            layout.addStretch(1)
            self.list_area.setWidget(body)
            extra = 48 if self.more_button is not None else 0
            height = len(shown) * CHOICE_ROW_HEIGHT + max(0, len(shown) - 1) * 8 + extra
            self.list_area.setFixedHeight(min(max(height, CHOICE_ROW_HEIGHT), CHOICE_LIST_MAX_HEIGHT))
            self.list_area.setVisible(bool(visible))
            empty_key = (
                self._empty_text
                if not self._choices
                else tr("instances.detail.modify.empty.search")
            )
            self.empty_label.setText(empty_key)
            self.empty_label.setVisible(not visible)
            if focus_index is not None and focus_index < len(self.rows):
                self.rows[focus_index].setFocus(Qt.FocusReason.TabFocusReason)
            if self.isVisible():
                self._place_card()
        finally:
            self._rebuilding = False

    def _load_more(self) -> None:
        self._limit += CHOICE_PAGE
        self._rebuild()

    def _choose(self, choice: _SwitchChoice) -> None:
        """One click is the whole choice: remember the row and close."""
        self.chosen = choice
        self._finish(inwindow_dialog.Outcome.ACCEPTED)

    def _focus_initial(self) -> None:
        if self.search_edit is not None:
            self.search_edit.setFocus(Qt.FocusReason.TabFocusReason)
        elif self.rows:
            self.rows[0].setFocus(Qt.FocusReason.TabFocusReason)
        else:
            super()._focus_initial()

    def focusNextPrevChild(self, next: bool) -> bool:
        """Keep the tab trap, and scroll the newly focused row into view (the list can overflow)."""
        handled = super().focusNextPrevChild(next)
        target = QApplication.focusWidget()
        if self.list_area is not None and target is not None:
            self.list_area.ensureWidgetVisible(target)
        return handled


class _SwitchConfirmDialog(inwindow_dialog.InWindowDialog):
    """The second confirmation: what changes, what stays, and the backup option.

    The backup is checked by default and lives inside the question rather than in a settings page:
    the user asked for one switch, and the copy that protects it belongs to that one answer. Enter
    activates 取消 on purpose - re-pointing a working instance is not something a stray Return should
    do, so the safe answer is the default one.

    A target that is not installed yet gets the longer body: the core is downloaded and written to
    ``versions/`` before the pointer moves, so "what happens" is more than one field this time.
    """

    def __init__(
        self, parent: QWidget | None, *, current: str, target: str, needs_install: bool = False
    ) -> None:
        super().__init__(
            parent,
            title=tr("instances.detail.modify.confirm.title"),
            severity="question",
        )
        body_key = (
            "instances.detail.modify.confirm.body_install"
            if needs_install
            else "instances.detail.modify.confirm.body"
        )
        self.add_body(tr(body_key, target, current))
        self.backup_check = QCheckBox(tr("instances.detail.modify.confirm.backup"))
        self.backup_check.setChecked(True)
        self.backup_check.setAccessibleName(tr("instances.detail.modify.confirm.backup"))
        self.backup_check.setToolTip(tr("instances.detail.modify.confirm.backup"))
        self.card_layout.addWidget(self.backup_check)
        accept_key = (
            "instances.detail.modify.confirm.accept_install"
            if needs_install
            else "instances.detail.modify.confirm.accept"
        )
        self.add_buttons(
            [
                (tr("dialog.cancel"), inwindow_dialog.Outcome.REJECTED),
                (tr(accept_key), inwindow_dialog.Outcome.ACCEPTED),
            ],
            default=inwindow_dialog.Outcome.REJECTED,
        )

    @property
    def wants_backup(self) -> bool:
        return self.backup_check.isChecked()


@dataclass(frozen=True)
class _SwitchRun:
    """The result of one install-then-point job (what the page announces when it is done)."""

    outcome: SwitchOutcome
    backup_path: Path | None
    installed_profile: str


def _perform_switch(
    instance_id: str,
    choice: _SwitchChoice,
    game_dir: Path,
    *,
    backup_dir: Path | None,
    stop: threading.Event,
    report: ProgressBridge,
    cache_dir: Path | None,
    runtime_dir: Path | None,
    concurrency: int,
) -> _SwitchRun:
    """Back up (optionally), install what is missing, then move the pointer - in that order.

    The order is the failure contract: the zip exists before a byte is written, the profile exists
    before ``base_version`` names it, and anything that fails leaves the pointer where it was. A
    cancelled transfer never reaches the last step, so 取消 cannot half-switch an instance.

    This runs on a worker thread. ``report`` is a ``ProgressBridge`` (its call is queued to the main
    thread), and every path that touches Qt-owned state stays out of here.
    """
    backup_path: Path | None = None
    if backup_dir is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = Path(backup_dir) / f"{instance_id}-{stamp}.zip"
        try:
            export_instance(
                instance_id,
                backup_path,
                game_dir,
                on_progress=lambda done, total: report(("backup", done, total)),
                should_stop=stop.is_set,
            )
        except Exception as exc:  # any backup failure must stop before the install
            if stop.is_set():
                raise DownloadCancelled("cancelled") from exc
            raise InstancesError(
                tr("instances.detail.modify.backup_failed", str(exc))
            ) from exc
    if stop.is_set():
        raise DownloadCancelled("cancelled")

    if choice.installed:
        # The pointer move is the whole operation: no download, no copy. This is the behaviour the
        # page had before the chooser learned to offer the official catalog.
        profile_id = choice.version_id
    else:
        if choice.is_loader_install:
            base_id = choice.minecraft
            # A loader installer inherits from the game version; installing its files again would only
            # download the same version JSON and skip every verified file.
            install_base = version_json_path(instance_dir(game_dir, base_id)) is None
        else:
            base_id = choice.version_id
            install_base = True
        installed = install_selection(
            base_id,
            loader_version=choice.loader_version,
            install_base=install_base,
            game_dir=game_dir,
            cache_dir=cache_dir,
            runtime_dir=runtime_dir,
            concurrency=concurrency,
            auto_install_java=True,
            progress=lambda progress: report(("install", progress)),
            should_stop=stop.is_set,
        )
        if stop.is_set():
            raise DownloadCancelled("cancelled")
        profile_id = installed.profile_id
    outcome = switch_instance_version(instance_id, profile_id, game_dir)
    return _SwitchRun(
        outcome=outcome,
        backup_path=backup_path,
        installed_profile=profile_id,
    )


class InstanceDetail(QWidget):
    """Right-hand column of the instances page: everything about one instance."""

    changed = Signal()  # metadata shown in the list changed (name / note / isolation / icon)
    deleted = Signal(str)  # the instance folder is gone
    #: One lookup arrived: (row key, launcher.mods.identity.Identity | None). A signal, not a direct
    #: callback, because the worker runs on its own thread and a widget may only be painted here.
    identity_ready = Signal(str, object)

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
        self._isolation_warned = False
        self._loaded_tabs: set[int] = set()
        self._content_items: dict[str, list] = {sub: [] for sub in _CONTENT_SUBDIRS}
        #: The normal empty-state text per content page, restored after a failed read replaced it
        #: with the error message (an unreadable folder must not keep looking like a load failure).
        self._content_empty_text: dict[str, str] = {}
        self._saves: list = []
        #: 投影: the last scan's files, its request generation and the event that interrupts the
        #: running scan (set when the page is reloaded or cleared).
        self._schematic_stop: threading.Event | None = None
        self._schematic_generation = 0
        self._badge_counts: dict[str, int] = {key: 0 for key in _BADGED_SECTIONS}
        #: 修改: one switch runs at a time, and its two buttons stay disabled while it does.
        self._switch_busy = False
        #: The running switch's task (the window's download list), its progress bridge, its stop
        #: event and its rate tracker. All None between switches.
        self._switch_task = None
        self._switch_bridge: ProgressBridge | None = None
        self._switch_stop: threading.Event | None = None
        self._switch_rate: RateTracker | None = None
        self._switch_cancelled = False
        #: 导出: the running export's task, its progress bridge, its stop flag and its destination.
        self._export_task = None
        self._export_bridge: ProgressBridge | None = None
        self._export_stop: threading.Event | None = None
        self._export_rate: RateTracker | None = None
        self._export_dest: Path | None = None
        #: 导出为整合包: same shape as the zip export above, kept apart so one cancel can never
        #: stop the other. ``_pack_last`` is the finished archive the "open folder" button reveals.
        self._pack_task = None
        self._pack_bridge: ProgressBridge | None = None
        self._pack_stop: threading.Event | None = None
        self._pack_rate: RateTracker | None = None
        self._pack_dest: Path | None = None
        self._pack_last: Path | None = None
        self._pack_fmt: str | None = None

        # The four card lists, keyed like the pages that own them.
        self.content_lists: dict[str, _ContentList] = {}
        self.content_rows: dict[str, list[_ContentRow]] = {}
        self.content_empty: dict[str, EmptyState] = {}
        self.content_tabs: dict[str, _ContentTabs] = {}
        #: One lookup per row key: the answered ones (a cached None included) so a refill paints what
        #: is known without touching the network again, and the ones still queued in the worker.
        self._identity_cache: dict[str, identity.Identity | None] = {}
        self._identity_pending: set[str] = set()
        self._identity_queue = identity.LookupQueue()
        #: Project icons by URL, for this page only: the disk cache is the persistent store, this map
        #: only saves decoding the same image again when a list is refilled.
        self._icon_pixmaps: dict[str, QPixmap] = {}
        self._icon_pending: set[str] = set()
        self.identity_ready.connect(self._on_identity_ready)

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
            self._build_schematic_page,
            self._build_saves,
            self._build_screenshots,
            self._build_servers,
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
        """The ten-entry sidebar: 200px wide, 36px items, 20px group headings, scrollable.

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
            ("loader", "instances.detail.row.loader"),
            ("core_dir", "instances.detail.row.core_dir"),
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
                    Qt.TextElideMode.ElideLeft
                    if key in ("game_dir", "core_dir")
                    else Qt.TextElideMode.ElideMiddle
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

        self.mods_tabs = _ContentTabs()
        self.mods_list = _ContentList()
        # WCAG 4.1.2: the list is a focus stop of its own and has no text; the name says what it is.
        self.mods_list.setAccessibleName(tr("a11y.instance_mods"))
        self.mods_list.set_jars_dropped_handler(self.install_dropped)
        self.mods_list.set_space_handler(self._toggle_current_mod)
        self.mods_empty = EmptyState(tr("instances.mods.search.none"))
        self.mods_list.attach_empty_overlay(self.mods_empty)
        # A compatibility alias: gui/pages/instances_page.py re-exports the old table under this name
        # for callers that predate the card list. New code reads mods_list.
        self.mods_table = self.mods_list
        self.mods_rows: list[_ContentRow] = []

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
        # Dragging jars onto the list is a pointer gesture with no keyboard equivalent, which fails
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
        layout.addWidget(self.mods_tabs)
        layout.addWidget(self.mods_list, 1)

        # Debounce: refill only after 200ms of typing inactivity
        self._mods_search_timer = QTimer(self)
        self._mods_search_timer.setSingleShot(True)
        self._mods_search_timer.timeout.connect(self._refill_mods)
        self.mods_search.textChanged.connect(lambda _t: self._mods_search_timer.start(200))
        self.mods_filter.currentIndexChanged.connect(lambda _i: self._refill_mods())
        self.mods_tabs.changed.connect(self._refill_mods)
        self.mods_refresh_button.clicked.connect(self._reload_mods)
        self.mods_folder_button.clicked.connect(self.open_mods_folder)
        self.mods_delete_button.clicked.connect(self.delete_selected_mods)
        self.mods_install_button.clicked.connect(self.choose_mod_files)
        return page

    def _build_resourcepacks_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        banner, self.res_banner_label = self._build_share_banner()
        self.res_banner = banner
        layout.addWidget(banner)
        box = self._build_content_section(
            "resourcepacks",
            tr("instances.detail.nav.resourcepacks"),
            tr("instances.detail.resourcepacks.empty"),
            with_status=True,
        )
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
        box = self._build_content_section(
            "shaderpacks",
            tr("instances.detail.nav.shaderpacks"),
            tr("instances.detail.shaderpacks.empty"),
        )
        layout.addWidget(box, 1)
        return page

    def _build_content_section(
        self, subdir: str, title: str, empty_text: str, *, with_status: bool = False
    ) -> QWidget:
        """One file list: title, 全部/可更新 tabs, open/delete, and the two-line card list.

        The icon is the pack's own ``pack.png`` when the file has one, the project's cached online
        icon when Modrinth recognized it, and the same 32px neutral cube otherwise. Resource packs
        carry the enabled state the game recorded, shown as a status mark on the row; shader packs do
        not have one in options.txt, so their rows carry no status instead of a fake "off".
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("title")
        tabs = _ContentTabs()
        list_widget = _ContentList()
        list_widget.setAccessibleName(title)
        empty = EmptyState(empty_text)
        list_widget.attach_empty_overlay(empty)
        self.content_lists[subdir] = list_widget
        self.content_rows[subdir] = []
        self.content_empty[subdir] = empty
        self.content_tabs[subdir] = tabs
        self._content_empty_text[subdir] = empty_text
        open_button = QPushButton(tr("instances.detail.open_folder"))
        open_button.setObjectName("secondaryButton")
        delete_button = QPushButton(tr("instances.mods.delete"))
        delete_button.setObjectName("dangerButton")
        open_button.clicked.connect(partial(self.open_content_folder, subdir))
        delete_button.clicked.connect(partial(self.delete_selected_content, subdir))
        tabs.changed.connect(partial(self._refill_content, subdir))
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(open_button)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        layout.addWidget(title_label)
        layout.addWidget(tabs)
        layout.addLayout(buttons)
        layout.addWidget(list_widget, 1)
        return box

    def _build_saves(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.saves_empty = EmptyState(tr("instances.detail.saves.empty"))
        self.saves_empty_text = tr("instances.detail.saves.empty")
        self.saves_list = _ContentList()
        self.saves_list.setAccessibleName(tr("a11y.instance_saves"))
        self.saves_list.attach_empty_overlay(self.saves_empty)
        # Compatibility alias for the same reason as mods_table (instances_page re-exports it).
        self.saves_table = self.saves_list
        self.saves_rows: list[_ContentRow] = []
        self.saves_refresh_button = QPushButton(tr("instances.mods.refresh"))
        self.saves_refresh_button.setObjectName("secondaryButton")
        self.saves_folder_button = QPushButton(tr("instances.detail.saves.open_folder"))
        self.saves_folder_button.setObjectName("secondaryButton")
        self.saves_backup_button = QPushButton(tr("instances.detail.saves.backup"))
        self.saves_backup_button.setObjectName("secondaryButton")
        self.saves_delete_button = QPushButton(tr("instances.mods.delete"))
        self.saves_delete_button.setObjectName("dangerButton")
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        for button in (
            self.saves_refresh_button,
            self.saves_folder_button,
            self.saves_backup_button,
            self.saves_delete_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(self.saves_list, 1)
        self.saves_refresh_button.clicked.connect(self.reload_saves)
        self.saves_folder_button.clicked.connect(self.open_saves_folder)
        self.saves_backup_button.clicked.connect(self.backup_selected_save)
        self.saves_delete_button.clicked.connect(self.delete_selected_saves)
        return page

    # ---------- 截图 / 服务器 (instance media sections) ----------
    #
    # Both used to be top-level pages and are embedded here unchanged (embedded=True): they own the
    # content edge to edge, like the mods and saves sections beside them. The scope is pushed by
    # _sync_instance_pages() on every reload; the pages read the instance's *effective* launch_dir,
    # so an isolated instance shows its own screenshots/servers.dat and a shared one shows the
    # global pair. Each page refreshes on show and, while it is the visible tab, when the instance
    # changes (set_instance checks isVisible()), so no extra lazy-load bookkeeping is needed here.

    def _build_screenshots(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.screenshots_page = ScreenshotsPage(embedded=True)
        layout.addWidget(self.screenshots_page, 1)
        return page

    def _build_servers(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.servers_page = ServersPage(embedded=True)
        layout.addWidget(self.servers_page, 1)
        return page

    def _sync_instance_pages(self) -> None:
        """Hand the shown instance (or none) to the two media sections.

        Nothing is scanned here unless the section is on screen: ``set_instance`` only refreshes a
        visible page, and the page's own ``showEvent`` covers the first visit. That is what keeps a
        detail page on 概览 from decoding a folder of screenshots or reading servers.dat.
        """
        if self.resolved is None:
            self.screenshots_page.clear_instance()
            self.servers_page.clear_instance()
            return
        name = self.instance.name if self.instance is not None else self.resolved.id
        self.screenshots_page.set_instance(
            self.resolved.id, name, self.resolved.launch_dir, self.resolved.game_dir
        )
        self.servers_page.set_instance(
            self.resolved.id, name, self.resolved.launch_dir, self.resolved.isolated
        )

    # ---------- 投影 / Litematica schematics ----------
    #
    # One self-contained block: the card, its background read, the row builder, the selection and the
    # folder button. It is a card of the 概览 page, so nothing else had to be re-ordered; the page's
    # life cycle reaches it through reload_schematics() and _clear_schematics() only.

    def _build_schematic_page(self) -> QWidget:
        """The 投影原理图 tab: the same card, on its own page instead of the bottom of 概览.

        PCL puts it in the sidebar beside 存档 / 截图 / 服务器, and it belongs there for the same
        reason those do: the files are instance data the user comes back to on purpose.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_schematic_card())
        layout.addStretch(1)
        return page

    def _build_schematic_card(self) -> QWidget:
        """The 投影 card: one row per ``.litematic`` file, the selected file's metadata, one button.

        Read-only on purpose: the files belong to the game and to Litematica, and this launcher does
        not render the build (no 3D view). The row's size line plus the metadata panel are the whole
        feature. The list is a ``_ContentList`` so a schematic row looks like every other file row on
        this page; it shows no block thumbnail, because the launcher does not read block data and a
        made-up picture would be worse than none.
        """
        card, card_layout = self._make_card()
        self.schematic_card = card
        card_layout.addWidget(self._card_heading("instances.detail.card.schematic"))

        # The empty state is a block rather than the list's centred overlay: it carries a second line
        # that says where the files belong, and an overlay would clip it against the card edge.
        self.schematic_empty = QWidget()
        empty_layout = QVBoxLayout(self.schematic_empty)
        empty_layout.setContentsMargins(0, 4, 0, 4)
        empty_layout.setSpacing(4)
        self.schematic_empty_title = EmptyState(
            tr("instances.detail.schematic.empty"), self.schematic_empty
        )
        # Shown here, unlike the list overlays elsewhere: this label is not an overlay, and a
        # parentless widget must never be made visible (it would be a top-level window for a frame).
        self.schematic_empty_title.setVisible(True)
        self.schematic_empty_hint = QLabel(tr("instances.detail.schematic.empty_hint"))
        self.schematic_empty_hint.setObjectName("hint")
        self.schematic_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.schematic_empty_hint.setWordWrap(True)
        empty_layout.addWidget(self.schematic_empty_title)
        empty_layout.addWidget(self.schematic_empty_hint)
        card_layout.addWidget(self.schematic_empty)

        self.schematic_list = _ContentList()
        # WCAG 4.1.2: the list is a focus stop with no text of its own; the name says what it holds.
        self.schematic_list.setAccessibleName(tr("a11y.instance_schematics"))
        # The card sits in the overview's scroll area, so the list never scrolls by itself: a nested
        # scrollbar inside a scrolling page is one wheel event away from trapping the pointer.
        self.schematic_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.schematic_list.currentRowChanged.connect(self._on_schematic_selected)
        self.schematic_list.setVisible(False)
        self.schematic_rows: list[_ContentRow] = []
        self._schematics: list[SchematicFile] = []
        card_layout.addWidget(self.schematic_list)

        self.schematic_hint = QLabel(tr("instances.detail.schematic.select"))
        self.schematic_hint.setObjectName("hint")
        self.schematic_hint.setWordWrap(True)
        self.schematic_hint.setVisible(False)
        card_layout.addWidget(self.schematic_hint)

        self.schematic_meta = QWidget()
        meta_form = QFormLayout(self.schematic_meta)
        style_form(meta_form)
        self.schematic_values: dict[str, QLabel] = {}
        self.schematic_titles: dict[str, QLabel] = {}
        for key, label_key in (
            ("name", "instances.detail.schematic.meta.name"),
            ("author", "instances.detail.schematic.meta.author"),
            ("created", "instances.detail.schematic.meta.created"),
            ("description", "instances.detail.schematic.meta.description"),
        ):
            title = QLabel(tr(label_key))
            title.setObjectName("hint")
            if key == "description":
                # Prose wraps instead of eliding: it is the one field the user actually reads.
                value = QLabel("")
                value.setWordWrap(True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            else:
                value = ElidedLabel("")
            self.schematic_values[key] = value
            self.schematic_titles[key] = title
            meta_form.addRow(title, value)
        self.schematic_meta.setVisible(False)
        card_layout.addWidget(self.schematic_meta)

        self.schematic_refresh_button = QPushButton(tr("instances.mods.refresh"))
        self.schematic_refresh_button.setObjectName("secondaryButton")
        self.schematic_folder_button = QPushButton(tr("instances.detail.schematic.open_folder"))
        self.schematic_folder_button.setObjectName("secondaryButton")
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.schematic_refresh_button)
        buttons.addWidget(self.schematic_folder_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)
        self.schematic_refresh_button.clicked.connect(self.reload_schematics)
        self.schematic_folder_button.clicked.connect(self.open_schematic_folder)
        return card

    def reload_schematics(self) -> None:
        """Read every schematic header off the GUI thread; the old rows stay until it answers."""
        if self.resolved is None:
            return
        if self._schematic_stop is not None:
            self._schematic_stop.set()
        stop = threading.Event()
        self._schematic_stop = stop
        self._schematic_generation += 1
        set_app_status(self, tr("instances.detail.schematic.loading"))
        run_in_background(
            self._read_schematics,
            self.resolved.launch_dir,
            self._schematic_generation,
            stop,
            on_result=self._fill_schematics,
            on_error=self._on_schematics_failed,
        )

    def _read_schematics(
        self, root: Path, generation: int, stop: threading.Event
    ) -> tuple[int, list[SchematicFile]]:
        """Worker-thread side: the pure scan, tagged with the request it belongs to.

        The generation tag is what keeps two overlapping requests in order: a page that was reloaded
        (or an instance the user left) must not paint the answer of the scan it superseded.
        """
        return generation, scan_schematics(root, stop=stop)

    def _fill_schematics(self, payload: tuple[int, list[SchematicFile]]) -> None:
        generation, files = payload
        if generation != self._schematic_generation:
            return
        self._schematics = files
        self.schematic_list.clear_rows()
        self.schematic_rows = []
        for item in files:
            row = self._make_schematic_row(item)
            self.schematic_rows.append(row)
            self.schematic_list.add_row(row)
        count = len(files)
        # Height follows the row count: the list itself cannot scroll (see _build_schematic_card),
        # so every row has to be inside it. The overview's own scroll area takes care of a long list.
        self.schematic_list.setFixedHeight(max(1, count) * CONTENT_ROW_SLOT + 2)
        self.schematic_list.setVisible(count > 0)
        self.schematic_empty.setVisible(count == 0)
        # With rows but no selection, the panel below would be an empty gap: say what to do instead.
        self.schematic_hint.setVisible(count > 0)
        self.schematic_meta.setVisible(False)
        for value in self.schematic_values.values():
            value.setText("")
        set_app_status(self, tr("instances.detail.schematic.count", count))

    def _on_schematics_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.detail.schematic.load_fail", message), "error")
        self._clear_schematics()

    def _make_schematic_row(self, item: SchematicFile) -> _ContentRow:
        """One card for one file: its name, then size, block kinds and modified time (or 无法读取)."""
        if item.info is None:
            row = _ContentRow(item.file_name, key=str(item.path))
            row.apply(detail=tr("instances.detail.schematic.unreadable"))
            if item.error:
                row.setToolTip(row.toolTip() + "\n" + item.error)
            return row
        row = _ContentRow(item.file_name, key=str(item.path))
        row.apply(detail=self._schematic_detail(item))
        return row

    @staticmethod
    def _schematic_detail(item: SchematicFile) -> str:
        """The second line: ``128×64×96 · 42 种方块 · 2026/08/09 12:30``."""
        info = item.info
        parts = ["×".join(str(value) for value in info.dimensions)]
        if info.block_kinds is not None:
            parts.append(tr("instances.detail.schematic.kinds", info.block_kinds))
        parts.append(InstanceDetail._schematic_stamp(item.modified))
        return " · ".join(part for part in parts if part)

    @staticmethod
    def _schematic_stamp(timestamp: float) -> str:
        """A calendar day and a time of day; empty when the timestamp is out of range."""
        try:
            return time.strftime("%Y/%m/%d %H:%M", time.localtime(timestamp))
        except (OverflowError, OSError, ValueError):
            return ""

    @staticmethod
    def _schematic_created(milliseconds: int | None) -> str:
        if not milliseconds:
            return tr("instances.detail.value.none")
        return InstanceDetail._schematic_stamp(milliseconds / 1000.0) or tr(
            "instances.detail.value.none"
        )

    def _on_schematic_selected(self, index: int) -> None:
        """Show the selected file's metadata; hide the panel again when the selection is gone."""
        item = self._schematics[index] if 0 <= index < len(self._schematics) else None
        info = item.info if item is not None else None
        self.schematic_meta.setVisible(info is not None)
        self.schematic_hint.setVisible(info is None and bool(self._schematics))
        values = self.schematic_values
        if info is None:
            for value in values.values():
                value.setText("")
            return
        none = tr("instances.detail.value.none")
        values["name"].setText(info.name or item.path.stem)
        values["author"].setText(info.author or none)
        values["created"].setText(self._schematic_created(info.created_ms))
        values["description"].setText(info.description or none)
        # A .litematic saved without metadata is common (the file may carry nothing but Regions), so a
        # panel of 名称 / （无） / （无） says less than one that shows only what the file actually has.
        form = self.schematic_meta.layout()
        for value in values.values():
            empty = not value.text().strip() or value.text() == none
            if isinstance(form, QFormLayout):
                form.setRowVisible(value, not empty)

    def _selected_schematic(self) -> SchematicFile | None:
        index = self.schematic_list.currentRow()
        if 0 <= index < len(self._schematics):
            return self._schematics[index]
        return None

    def open_schematic_folder(self) -> None:
        """Open the selected file's folder, or the instance's schematic folder when none is selected."""
        if self._require_instance() is None:
            return
        item = self._selected_schematic()
        target = item.path.parent if item is not None else schematic_folder(self.resolved.launch_dir)
        self._open_path(target)
        set_app_status(self, tr("instances.msg.folder_opened", str(target)))

    def _clear_schematics(self) -> None:
        """Drop the rows and cancel the running scan (instance switched, or gone)."""
        if self._schematic_stop is not None:
            self._schematic_stop.set()
            self._schematic_stop = None
        self._schematic_generation += 1
        self._schematics = []
        self.schematic_rows = []
        self.schematic_list.clear_rows()
        self.schematic_list.setVisible(False)
        self.schematic_empty.setVisible(True)
        self.schematic_hint.setVisible(False)
        self.schematic_meta.setVisible(False)
        for value in self.schematic_values.values():
            value.setText("")

    def _build_diagnosis(self) -> QWidget:
        """Diagnosis and the live latest.log, merged into one page (two cards).

        The result card shows the cause, the three-step certainty, at most three
        evidence lines (file + line number) and the rule's action buttons. The raw
        log card starts collapsed while a result is shown — the evidence is the
        entry point, the full text is one click away.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, card_layout = self._make_card()
        card_layout.addWidget(self._card_heading("instances.detail.tab.diagnosis"))
        self.diagnosis_empty = EmptyState(tr("instances.detail.diagnosis.none"))
        card_layout.addWidget(self.diagnosis_empty)

        self.diagnosis_code = QLabel("")
        self.diagnosis_code.setObjectName("hint")
        self.diagnosis_code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diagnosis_time = QLabel("")
        self.diagnosis_time.setObjectName("hint")
        self.diagnosis_cause = QLabel("")
        self.diagnosis_cause.setObjectName("title")
        self.diagnosis_cause.setWordWrap(True)
        self.diagnosis_cause.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.diagnosis_confidence = QLabel("")
        self.diagnosis_confidence.setObjectName("hint")
        self.diagnosis_suspect = QLabel("")
        self.diagnosis_suspect.setObjectName("hint")
        self.diagnosis_suspect.setWordWrap(True)
        self.diagnosis_suspect.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.diagnosis_evidence = QLabel("")
        self.diagnosis_evidence.setObjectName("hint")
        self.diagnosis_evidence.setWordWrap(True)
        self.diagnosis_evidence.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.diagnosis_evidence.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.diagnosis_evidence_title = QLabel(tr("diagnosis.section.evidence"))
        self.diagnosis_evidence_title.setObjectName("hint")
        self.diagnosis_evidence_title.setVisible(False)
        self.diagnosis_body = QLabel("")
        self.diagnosis_body.setObjectName("hint")
        self.diagnosis_body.setWordWrap(True)
        self.diagnosis_body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.diagnosis_body.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.diagnosis_actions = QWidget()
        self.diagnosis_actions_layout = QHBoxLayout(self.diagnosis_actions)
        self.diagnosis_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.diagnosis_actions_layout.setSpacing(6)
        self.diagnosis_actions_layout.addStretch(1)
        self.diagnosis_action_buttons: list[QPushButton] = []

        self._diagnosis_result_widgets = (
            self.diagnosis_code,
            self.diagnosis_time,
            self.diagnosis_cause,
            self.diagnosis_confidence,
            self.diagnosis_suspect,
            self.diagnosis_evidence_title,
            self.diagnosis_evidence,
            self.diagnosis_body,
            self.diagnosis_actions,
        )
        for widget in self._diagnosis_result_widgets:
            widget.setVisible(False)
            card_layout.addWidget(widget)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.diagnosis_copy_button = QPushButton(tr("instances.detail.diagnosis.copy"))
        self.diagnosis_copy_button.setObjectName("secondaryButton")
        self.diagnosis_copy_button.clicked.connect(self.copy_diagnosis)
        self.raw_toggle = QPushButton(tr("instances.detail.diagnosis.raw_show"))
        self.raw_toggle.setObjectName("secondaryButton")
        self.raw_toggle.setAccessibleName(tr("instances.detail.diagnosis.raw_show"))
        self.raw_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.raw_toggle.clicked.connect(self.toggle_raw_log)
        self.raw_toggle.setVisible(False)
        self.diagnosis_reports_label = QLabel("")
        self.diagnosis_reports_label.setObjectName("hint")
        row.addWidget(self.diagnosis_copy_button)
        row.addWidget(self.raw_toggle)
        row.addWidget(self.diagnosis_reports_label)
        row.addStretch(1)
        card_layout.addLayout(row)
        layout.addWidget(card)

        log_card, log_layout = self._make_card()
        self.log_card = log_card
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
        self._live_diagnosis = None
        self.diagnosis_copy_button.setEnabled(False)
        card.setAccessibleName(tr("instances.detail.tab.diagnosis"))
        return page

    def toggle_raw_log(self) -> None:
        """Reveal or hide the raw log card (collapsed by default while a result shows)."""
        showing = self.log_card.isVisible()
        self.log_card.setVisible(not showing)
        key = (
            "instances.detail.diagnosis.raw_show"
            if showing
            else "instances.detail.diagnosis.raw_hide"
        )
        self.raw_toggle.setText(tr(key))
        self.raw_toggle.setAccessibleName(tr(key))

    def _clear_diagnosis_actions(self) -> None:
        for button in self.diagnosis_action_buttons:
            self.diagnosis_actions_layout.removeWidget(button)
            button.hide()
            button.setParent(None)
            button.deleteLater()
        self.diagnosis_action_buttons.clear()

    def _analyze_instance(self):
        """Run the local knowledge base over this instance's logs (never raises)."""
        if self.resolved is None or self.instance_id is None:
            return None
        from launcher.diagnostics import (
            CrashLogSet,
            CrashModIndex,
            CrashStackAnalyzer,
            DiagnosisContext,
            diagnose,
        )

        try:
            context = DiagnosisContext(
                version_id=self.instance_id,
                game_dir=self.resolved.game_dir,
                launch_dir=self.resolved.launch_dir,
                instance=self.instance,
                mods_dir=self.resolved.mods_dir,
                memory_gb=self.resolved.memory_gb,
                java_path=self.resolved.java_path,
                # Only this instance's own logs: the launcher's shared log would
                # otherwise explain a crash that happened in another instance.
                launcher_logs_dir=instance_dir(self.game_dir, self.instance_id)
                / ".no-launcher-logs",
            )
            log_set = CrashLogSet.from_context(context)
            index = CrashModIndex.scan(self.resolved.mods_dir)
            stack = CrashStackAnalyzer(index).analyze(log_set.logs)
            return diagnose(
                context,
                logs=list(log_set.logs),
                mod_index=index,
                stack=stack,
            )
        except Exception:
            log.exception("diagnostics: the instance analysis failed")
            return None

    def _render_live_diagnosis(self, diagnosis) -> None:
        """Fill the result card from a diagnosis (cause / certainty / evidence / actions)."""
        primary = diagnosis.primary
        self._live_diagnosis = diagnosis
        self.diagnosis_actions.setVisible(False)
        self._clear_diagnosis_actions()
        self.diagnosis_code.setText(diagnosis.code)
        self.diagnosis_cause.setText(tr(primary.cause))
        self.diagnosis_confidence.setText(
            tr("diagnosis.confidence", tr(f"diagnosis.certainty.{primary.certainty}"))
        )
        suspect = diagnosis.facts.suspect_text
        if suspect and suspect != "-":
            file = diagnosis.facts.suspect_file
            name = f"{suspect} · {file}" if file and file != "-" else suspect
            self.diagnosis_suspect.setText(tr("instances.detail.diagnosis.suspect", name))
            self.diagnosis_suspect.setVisible(True)
        else:
            self.diagnosis_suspect.clear()
            self.diagnosis_suspect.setVisible(False)

        evidence_lines = []
        ordered = sorted(primary.evidence, key=lambda item: item.line_number or 0)
        for item in ordered[:3]:
            text = item.line.strip()
            if len(text) > 220:
                text = text[:217] + "…"
            evidence_lines.append(f"{item.location}  {text}")
        self.diagnosis_evidence.setText("\n".join(evidence_lines))
        self.diagnosis_evidence.setVisible(bool(evidence_lines))
        self.diagnosis_evidence_title.setVisible(bool(evidence_lines))

        for action in primary.actions:
            button = self._action_button(action, primary)
            if button is not None:
                self.diagnosis_actions_layout.insertWidget(
                    self.diagnosis_actions_layout.count() - 1, button
                )
                self.diagnosis_action_buttons.append(button)
        self.diagnosis_actions.setVisible(bool(self.diagnosis_action_buttons))

        self._diagnosis_lines = []
        from launcher.diagnostics import render_lines

        try:
            self._diagnosis_lines = list(render_lines(diagnosis, tr))
        except Exception:
            log.exception("diagnostics: could not render the diagnosis summary")
        self.diagnosis_body.clear()
        self.diagnosis_body.setVisible(False)
        self.diagnosis_time.clear()
        self.diagnosis_time.setVisible(False)
        self.diagnosis_empty.setVisible(False)
        self.diagnosis_copy_button.setEnabled(bool(self._diagnosis_lines))

    def _action_button(self, action, primary) -> QPushButton | None:
        """One clickable action; the knowledge base only describes it, this runs it."""
        label = tr(action.i18n_key, *action.args)
        if action.kind == "disable_mod":
            target = self._mod_action_target(action.param("mod"))
            if target is None:
                return None
            button = QPushButton(label)
            button.setObjectName("secondaryButton")
            button.setAccessibleName(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(partial(self._disable_mod, target))
            return button
        if action.kind == "open_mods":
            button = QPushButton(label)
            button.setObjectName("secondaryButton")
            button.setAccessibleName(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(self._open_mods_folder)
            return button
        if action.kind == "set_memory":
            button = QPushButton(label)
            button.setObjectName("secondaryButton")
            button.setAccessibleName(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                partial(self._apply_suggested_memory, action.param("memory"))
            )
            return button
        if action.kind == "open_help":
            button = QPushButton(label)
            button.setObjectName("secondaryButton")
            button.setAccessibleName(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(partial(self._show_help, primary))
            return button
        return None

    def _mod_action_target(self, name: str) -> Path | None:
        if not name or name == "-" or self.resolved is None:
            return None
        target = self.resolved.mods_dir / name
        try:
            return target if target.is_file() else None
        except OSError:
            return None

    def _open_mods_folder(self) -> None:
        if self.resolved is None:
            return
        target = self.resolved.mods_dir
        if not target.is_dir():
            target = self.resolved.launch_dir
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            set_app_status(
                self, tr("instances.detail.diagnosis.open_failed", str(target)), "error"
            )

    def _disable_mod(self, target: Path) -> None:
        if not inwindow_dialog.question(
            self,
            tr("instances.detail.diagnosis.disable.title"),
            tr("instances.detail.diagnosis.disable.msg", target.name),
        ):
            return
        disabled = target.with_name(target.name + ".disabled")
        try:
            target.replace(disabled)
        except OSError as exc:
            set_app_status(
                self, tr("instances.detail.diagnosis.disable_failed", str(exc)), "error"
            )
            return
        set_app_status(self, tr("instances.detail.diagnosis.disabled", target.name))
        self.reload()

    def _apply_suggested_memory(self, value: str) -> None:
        if self._require_instance() is None:
            return
        try:
            memory = float(value)
        except ValueError:
            return
        try:
            set_instance_settings(
                self.instance_id, game_dir=self.game_dir, memory_gb=memory
            )
        except (InstancesError, OSError) as exc:
            set_app_status(
                self, tr("instances.detail.diagnosis.memory_failed", str(exc)), "error"
            )
            return
        set_app_status(self, tr("instances.detail.diagnosis.memory_set", f"{memory:g}"))
        self.reload()

    def _show_help(self, primary) -> None:
        """Local help: the rule's own cause + advice, never a web request."""
        advice = "\n".join(tr(key, *args) for key, args in primary.advice)
        text = tr(primary.cause) + (("\n\n" + advice) if advice else "")
        inwindow_dialog.information(
            self, tr("instances.detail.diagnosis.help.title"), text
        )

    def _build_modify(self) -> QWidget:
        """修改: what the instance runs now, and the two ways to change the core it runs.

        The read-only rows name the core the launch path really resolves (version, loader, folder).
        The two buttons open a chooser that offers both installed profiles and versions that are not
        installed yet; a version from the catalog is downloaded through the versions page's install
        path, then the instance's ``base_version`` points at the profile that install produced. An
        installed profile needs neither step - it is only the pointer that moves.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, card_layout = self._make_card()
        card_layout.addWidget(self._card_heading("instances.detail.nav.modify"))
        form = QFormLayout()
        style_form(form)
        self.modify_values: dict[str, ElidedLabel] = {}
        for key, label_key in (
            ("game_version", "instances.detail.modify.game_version"),
            ("loader", "instances.detail.modify.loader"),
            ("point_to", "instances.detail.modify.point_to"),
            ("core_dir", "instances.detail.row.core_dir"),
        ):
            title = QLabel(tr(label_key))
            title.setObjectName("hint")
            value = ElidedLabel(
                tr("instances.detail.value.none"),
                elide_mode=(
                    Qt.TextElideMode.ElideLeft
                    if key == "core_dir"
                    else Qt.TextElideMode.ElideMiddle
                ),
            )
            self.modify_values[key] = value
            form.addRow(title, value)
        card_layout.addLayout(form)

        self.modify_hint = QLabel(tr("instances.detail.modify.hint"))
        self.modify_hint.setObjectName("hint")
        self.modify_hint.setWordWrap(True)
        card_layout.addWidget(self.modify_hint)

        #: The last failure, in place: a status line is overwritten by the next message, and "the
        #: instance still runs the old core because the download failed" is information to keep.
        self.modify_error = QLabel("")
        self.modify_error.setObjectName("hint")
        self.modify_error.setWordWrap(True)
        self.modify_error.setVisible(False)
        card_layout.addWidget(self.modify_error)

        self.change_version_button = QPushButton(tr("instances.detail.modify.change_version"))
        self.change_version_button.setObjectName("secondaryButton")
        self.change_version_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_loader_button = QPushButton(tr("instances.detail.modify.change_loader"))
        self.change_loader_button.setObjectName("secondaryButton")
        self.change_loader_button.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.change_version_button)
        buttons.addWidget(self.change_loader_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)

        # The install half of a switch reports in two places: this row (what the next second is
        # doing, with a cancel button) and the window's task list (the same numbers, surviving a
        # page change). Both read the one ProgressBridge, so they cannot disagree.
        self.switch_state_label = QLabel("")
        self.switch_state_label.setObjectName("hint")
        self.switch_state_label.setWordWrap(True)
        self.switch_state_label.setVisible(False)
        card_layout.addWidget(self.switch_state_label)
        self.switch_progress_row, self.switch_progress_bar, self.switch_progress_text = (
            build_download_progress("switchProgress")
        )
        self.switch_cancel_button = QPushButton(tr("instances.detail.modify.cancel"))
        self.switch_cancel_button.setObjectName("secondaryButton")
        self.switch_cancel_button.setAccessibleName(tr("instances.detail.modify.cancel"))
        self.switch_cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switch_cancel_button.clicked.connect(self.cancel_switch)
        progress_row = QWidget()
        progress_layout = QHBoxLayout(progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(8)
        progress_layout.addWidget(self.switch_progress_row, 1)
        progress_layout.addWidget(self.switch_cancel_button)
        progress_row.setVisible(False)
        self.switch_progress_container = progress_row
        card_layout.addWidget(progress_row)

        layout.addWidget(card)
        layout.addStretch(1)
        # Nothing selected yet: a switch needs the instance the panel shows, and _fill_modify is what
        # enables these two once one is loaded.
        self.change_version_button.setEnabled(False)
        self.change_loader_button.setEnabled(False)
        self.change_version_button.clicked.connect(self.change_game_version)
        self.change_loader_button.clicked.connect(self.change_loader)
        return page

    def _build_export(self) -> QWidget:
        """导出: the whole instance as a zip, or a standard modpack other launchers can import.

        Two products on purpose. The zip is a backup of this folder and only this launcher can read
        it back as an instance; the 整合包 is the portable artifact (``.mrpack`` / CurseForge zip),
        which is the thing to hand to somebody else. Both run as tasks on the download page, and the
        pack's numbers are also shown in place so 取消 is reachable without leaving the page.
        """
        from launcher.modpack import FORMAT_CURSEFORGE, FORMAT_MODRINTH

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, card_layout = self._make_card()
        card_layout.addWidget(self._card_heading("instances.detail.nav.export"))
        self.export_hint = QLabel(tr("instances.detail.export.hint"))
        self.export_hint.setObjectName("hint")
        self.export_hint.setWordWrap(True)
        card_layout.addWidget(self.export_hint)

        self.export_button = QPushButton(tr("instances.detail.export.button"))
        self.export_button.setObjectName("secondaryButton")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.export_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)

        # A sentence, not a link: the zip has no document to point at. It says what the archive is
        # *for*, because the difference from the modpack below is the whole point of this page.
        self.export_zip_note = QLabel(tr("instances.detail.export.zip_note"))
        self.export_zip_note.setObjectName("hint")
        self.export_zip_note.setWordWrap(True)
        card_layout.addWidget(self.export_zip_note)

        self.export_pack_hint = QLabel(tr("instances.detail.export.pack.hint"))
        self.export_pack_hint.setObjectName("hint")
        self.export_pack_hint.setWordWrap(True)
        card_layout.addWidget(self.export_pack_hint)

        self.export_pack_format = QComboBox()
        self.export_pack_format.addItem(
            tr("instances.detail.export.pack.format.modrinth"), FORMAT_MODRINTH
        )
        self.export_pack_format.addItem(
            tr("instances.detail.export.pack.format.curseforge"), FORMAT_CURSEFORGE
        )
        # The combo shows the format's own name; its field label is not announced with it, so the
        # accessible name carries the question the control answers.
        self.export_pack_format.setAccessibleName(
            tr("instances.detail.export.pack.format")
        )
        self.export_pack_format.currentIndexChanged.connect(self._on_export_format_changed)
        format_row = QFormLayout()
        style_form(format_row)
        format_label = QLabel(tr("instances.detail.export.pack.format"))
        format_label.setObjectName("hint")
        format_row.addRow(format_label, self.export_pack_format)
        card_layout.addLayout(format_row)

        self.export_pack_summary = QLabel(tr("instances.detail.export.pack.summary.none"))
        self.export_pack_summary.setObjectName("hint")
        self.export_pack_summary.setWordWrap(True)
        card_layout.addWidget(self.export_pack_summary)

        self.export_pack_button = QPushButton(tr("instances.detail.export.pack.button"))
        self.export_pack_button.setObjectName("primaryButton")
        self.export_pack_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_pack_open_button = QPushButton(
            tr("instances.detail.export.pack.open_folder")
        )
        self.export_pack_open_button.setObjectName("secondaryButton")
        self.export_pack_open_button.setAccessibleName(
            tr("instances.detail.export.pack.open_folder")
        )
        self.export_pack_open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_pack_open_button.setVisible(False)
        pack_buttons = QHBoxLayout()
        pack_buttons.setContentsMargins(0, 0, 0, 0)
        pack_buttons.setSpacing(6)
        pack_buttons.addWidget(self.export_pack_button)
        pack_buttons.addWidget(self.export_pack_open_button)
        pack_buttons.addStretch(1)
        card_layout.addLayout(pack_buttons)

        # The same two-place progress the 修改 switch uses: a sentence plus a bar and 取消 in this
        # card, and the identical numbers in the window's task list (one ProgressBridge feeds both).
        # It sits above the explanatory note so the window's floating task button (bottom-right
        # corner) can never cover 取消, which is the control the user needs while it runs.
        self.export_pack_state = QLabel("")
        self.export_pack_state.setObjectName("hint")
        self.export_pack_state.setWordWrap(True)
        self.export_pack_state.setVisible(False)
        card_layout.addWidget(self.export_pack_state)
        (
            self.export_pack_progress_row,
            self.export_pack_progress_bar,
            self.export_pack_progress_text,
        ) = build_download_progress("packExportProgress")
        self.export_pack_cancel_button = QPushButton(
            tr("instances.detail.export.pack.cancel")
        )
        self.export_pack_cancel_button.setObjectName("secondaryButton")
        self.export_pack_cancel_button.setAccessibleName(
            tr("instances.detail.export.pack.cancel")
        )
        self.export_pack_cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_pack_cancel_button.clicked.connect(self.cancel_export_pack)
        progress_row = QWidget()
        progress_layout = QHBoxLayout(progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(8)
        progress_layout.addWidget(self.export_pack_progress_row, 1)
        progress_layout.addWidget(self.export_pack_cancel_button)
        progress_row.setVisible(False)
        self.export_pack_progress_container = progress_row
        card_layout.addWidget(progress_row)

        self.export_pack_note = QLabel(tr("instances.detail.export.pack.note"))
        self.export_pack_note.setObjectName("hint")
        self.export_pack_note.setWordWrap(True)
        card_layout.addWidget(self.export_pack_note)

        # The finished archive, in place: the path stays on screen (a status line is overwritten by
        # the next message) and 打开所在文件夹 is the next thing a person wants to do.
        self.export_pack_result = QLabel("")
        self.export_pack_result.setObjectName("hint")
        self.export_pack_result.setWordWrap(True)
        self.export_pack_result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.export_pack_result.setVisible(False)
        card_layout.addWidget(self.export_pack_result)

        layout.addWidget(card)
        layout.addStretch(1)
        self.export_button.clicked.connect(self.export)
        self.export_pack_button.clicked.connect(self.export_pack)
        self.export_pack_open_button.clicked.connect(self.open_exported_pack_folder)
        # Nothing selected yet: _fill_export is what enables these once an instance is loaded.
        self.export_pack_button.setEnabled(False)
        self.export_pack_format.setEnabled(False)
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
        if not log_path.is_file():
            # A crash report is the other raw log the diagnosis points into; when
            # latest.log is already rotated away, show the newest report instead.
            reports = sorted(
                (self.resolved.launch_dir / "crash-reports").glob("crash-*.txt"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if reports:
                log_path = reports[0]
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
        """Analyze this instance's own logs, falling back to the recorded summary.

        The live pass is what makes the card real: it runs the current log set
        through the knowledge base and shows cause, certainty, evidence with
        line numbers and the rule's actions. The recorded ``diagnosis.json``
        remains the fallback for an instance whose logs were already cleaned up.
        """
        from gui.crash_viewer import collect_crash_reports
        from gui.dialogs.crash_dialog import load_recorded_diagnosis

        if self.instance_id is None:
            return
        record = load_recorded_diagnosis(instance_dir(self.game_dir, self.instance_id))
        has_record = bool(record and record.get("code"))
        live = self._analyze_instance()
        has_live = bool(
            live is not None and live.analysable and not live.primary.fallback
        )

        self._refresh_log()
        if has_live:
            self._render_live_diagnosis(live)
            self.diagnosis_empty.setVisible(False)
            for widget in (
                self.diagnosis_code,
                self.diagnosis_cause,
                self.diagnosis_confidence,
                self.diagnosis_evidence,
                self.diagnosis_actions,
            ):
                widget.setVisible(True)
            self.diagnosis_code.setVisible(True)
            self.diagnosis_confidence.setVisible(True)
            has_log = bool(self.log_view.toPlainText())
            # A crash report / hs_err is the "raw log" a result points into, so it starts
            # collapsed. A diagnosis made from latest.log alone keeps the log card open:
            # there is no crash report to expand, and the log is the only source shown.
            from launcher.diagnostics import KIND_CRASH_REPORT, KIND_HS_ERR

            collapse = any(
                log.kind in (KIND_CRASH_REPORT, KIND_HS_ERR) for log in live.logs
            )
            self.log_card.setVisible(not (has_log and collapse))
            self.raw_toggle.setVisible(has_log and collapse)
            self.raw_toggle.setText(tr("instances.detail.diagnosis.raw_show"))
            self.raw_toggle.setAccessibleName(
                tr("instances.detail.diagnosis.raw_show")
            )
        else:
            self._live_diagnosis = None
            self._clear_diagnosis_actions()
            for widget in self._diagnosis_result_widgets:
                widget.setVisible(False)
            self.diagnosis_empty.setVisible(not has_record)
            self.log_card.setVisible(True)
            self.raw_toggle.setVisible(False)
            self.diagnosis_copy_button.setEnabled(has_record)
            self._diagnosis_lines = []
            if has_record:
                self.diagnosis_code.setText(str(record.get("code", "")))
                recorded_at = record.get("recorded_at")
                stamp = ""
                if recorded_at:
                    try:
                        stamp = time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(float(recorded_at))
                        )
                    except (TypeError, ValueError, OSError):
                        stamp = ""
                self.diagnosis_time.setText(tr("instances.detail.diagnosis.recorded", stamp))
                self._diagnosis_lines = [
                    str(line) for line in record.get("lines", []) if str(line)
                ]
                self.diagnosis_body.setText("\n".join(self._diagnosis_lines))
                for widget in (self.diagnosis_code, self.diagnosis_time, self.diagnosis_body):
                    widget.setVisible(True)
            else:
                self.diagnosis_code.clear()
                self.diagnosis_time.clear()
                self.diagnosis_body.clear()
        try:
            count = len(collect_crash_reports(self.effective_dir()))
        except OSError:
            count = 0
        self.diagnosis_reports_label.setText(tr("instances.detail.diagnosis.reports", count))

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
            # The finished archive's path belongs to the instance that was shown a moment ago.
            self._forget_exported_pack()
        self.instance_id = instance_id
        if instance_id is None:
            self.instance = None
            self.resolved = None
            self._mods = []
            self.mods_list.clear_rows()
            self.mods_rows = []
            for subdir in _CONTENT_SUBDIRS:
                self._content_items[subdir] = []
                list_widget = self.content_lists.get(subdir)
                if list_widget is not None:
                    list_widget.clear_rows()
                self.content_rows[subdir] = []
            self._saves = []
            self.saves_list.clear_rows()
            self.saves_rows = []
            for key in _BADGED_SECTIONS:
                self._set_badge_count(key, 0)
            self._cancel_identity_work(clear_cache=True)
            self._clear_rows()
            return
        self.reload()

    def _cancel_identity_work(self, *, clear_cache: bool = False) -> None:
        """Stop the queued lookups and drop the in-memory answers (an instance switch invalidates them).

        The disk cache is what makes a revisit instant; this only cancels the queue so a result for
        the instance the user just left cannot paint a row of the instance now shown.
        """
        self._identity_queue.cancel_all()
        self._identity_pending.clear()
        if clear_cache:
            self._identity_cache.clear()
            self._icon_pixmaps.clear()
            self._icon_pending.clear()

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
        # The master switch is read on every reload, so a user who turned it off sees only local
        # information on the next visit even if the settings page was never rebuilt.
        identity.set_enabled(online_identity_enabled())
        # A switch that was turned off must not keep showing answers this page already holds: the
        # memory caches are dropped together with the queued work, so the next fill is local-only.
        self._cancel_identity_work(clear_cache=not identity.is_enabled())
        self._fill_header()
        self._fill_overview()
        self._fill_settings()
        self._fill_banners()
        self._fill_modify()
        self._fill_export()
        self._refresh_badge_counts()
        self._sync_instance_pages()
        self._loaded_tabs = set()
        self._reload_mods()
        # 投影 is a card of this page, not a lazily loaded tab, so it refreshes with the instance.
        self.reload_schematics()
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
        self.mods_list.clear_rows()
        self.mods_rows = []
        self.mods_list.setVisible(False)
        self.mods_empty.update_for(0)
        for subdir, list_widget in self.content_lists.items():
            list_widget.clear_rows()
            list_widget.setVisible(False)
            self.content_rows[subdir] = []
            empty = self.content_empty.get(subdir)
            if empty is not None:
                empty.setText(self._content_empty_text.get(subdir, ""))
                empty.setVisible(True)
        self.saves_list.clear_rows()
        self.saves_rows = []
        self.saves_list.setVisible(False)
        self.saves_empty.setText(self.saves_empty_text)
        self.saves_empty.setVisible(True)
        self._clear_schematics()
        for value in self.modify_values.values():
            value.setText(tr("instances.detail.value.none"))
            value.setToolTip("")
        self.modify_error.setText("")
        self.modify_error.setVisible(False)
        self.switch_progress_row.setVisible(False)
        self.switch_progress_container.setVisible(False)
        self.switch_state_label.setVisible(False)
        self.change_version_button.setEnabled(False)
        self.change_loader_button.setEnabled(False)
        for key in _BADGED_SECTIONS:
            self._set_badge_count(key, 0)
        # The two media sections are hidden as well; their rows and scope labels must not outlive
        # the instance they belong to. (Guarded: this can run while the page is being torn down.)
        screenshots_page = getattr(self, "screenshots_page", None)
        if screenshots_page is not None:
            screenshots_page.clear_instance()
        servers_page = getattr(self, "servers_page", None)
        if servers_page is not None:
            servers_page.clear_instance()

    def _fill_header(self) -> None:
        inst = self.instance
        self.name_label.setText(inst.name)
        self.id_label.setText(tr("instances.detail.header.id", inst.id))
        self.dir_label.setText(tr("instances.detail.header.dir", str(self.resolved.launch_dir)))

    def _fill_overview(self) -> None:
        inst = self.instance
        cfg, _ = config.load()
        values = self.overview_values
        version_id = self.resolved.version_id
        values["version"].setText(version_id)
        values["profile"].setText(display_version_name(version_id))
        values["loader"].setText(self._loader_text(version_id))
        core_folder = instance_dir(self.game_dir, core_profile_id(self.game_dir, version_id))
        values["core_dir"].setText(str(core_folder))
        values["core_dir"].setToolTip(str(core_folder))
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

    def _loader_text(self, version_id: str) -> str:
        """The loader a profile runs, or the honest "cannot read it" for a missing/broken profile."""
        if version_json_path(instance_dir(self.game_dir, version_id)) is None:
            return tr("instances.detail.modify.unknown")
        identity = profile_identity(self.game_dir, version_id)
        return identity.loader or tr("instances.detail.modify.vanilla")

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

    def _fill_modify(self) -> None:
        """Show what the instance would launch now: game version, loader, profile and core folder.

        Every value comes from the profile the launch path will read (``resolved.version_id``,
        followed through ``inheritsFrom``), so the page cannot claim a version the launcher would not
        start. A folder whose profile is unreadable says so instead of guessing from its name.
        """
        values = self.modify_values
        if self.instance is None or self.resolved is None:
            for value in values.values():
                value.setText(tr("instances.detail.value.none"))
                value.setToolTip("")
            self.change_version_button.setEnabled(False)
            self.change_loader_button.setEnabled(False)
            return
        version_id = self.resolved.version_id
        values["point_to"].setText(version_id)
        values["point_to"].setToolTip(version_id)
        core_folder = instance_dir(self.game_dir, core_profile_id(self.game_dir, version_id))
        values["core_dir"].setText(str(core_folder))
        values["core_dir"].setToolTip(str(core_folder))
        if version_json_path(core_folder) is None:
            unknown = tr("instances.detail.modify.unknown")
            values["game_version"].setText(unknown)
            values["loader"].setText(unknown)
        else:
            identity = profile_identity(self.game_dir, version_id)
            values["game_version"].setText(identity.minecraft or tr("instances.detail.modify.unknown"))
            values["loader"].setText(identity.loader or tr("instances.detail.modify.vanilla"))
        enabled = not self._switch_busy
        self.change_version_button.setEnabled(enabled)
        self.change_loader_button.setEnabled(enabled)

    def change_game_version(self) -> None:
        """更换游戏版本: installed profiles of other versions, plus the official catalog."""
        self._choose_switch_target(SWITCH_VERSION)

    def change_loader(self) -> None:
        """更换加载器: this game version's profiles, plus the loaders that can be downloaded."""
        self._choose_switch_target(SWITCH_LOADER)

    def _identity_text(self, version_id: str) -> str:
        """``1.21.11 · Fabric 0.19.3`` for one profile id (the sentence the dialogs read)."""
        identity = profile_identity(self.game_dir, version_id)
        loader = identity.loader or tr("instances.detail.modify.vanilla")
        return f"{identity.minecraft} · {loader}"

    @staticmethod
    def _choice_for_identity(identity) -> _SwitchChoice:
        """An installed profile as a chooser row: the pointer move is the whole operation."""
        return _SwitchChoice(
            version_id=identity.version_id,
            minecraft=identity.minecraft,
            loader=identity.loader,
            installed=True,
            meta=identity.version_id,
        )

    def _choose_switch_target(self, kind: str) -> None:
        """Open the candidate list for one kind of switch, then ask for confirmation.

        The list is complete before it is useful: installed profiles are local and shown at once, and
        the downloadable half (the official catalog, or each loader's newest build) is requested in
        the background while the dialog is already open. Nothing here downloads anything - a click
        only records the choice.
        """
        if self._require_instance() is None or self.resolved is None or self._switch_busy:
            return
        is_loader = kind == SWITCH_LOADER
        current = profile_identity(self.game_dir, self.resolved.version_id)
        installed = [
            self._choice_for_identity(identity)
            for identity in switch_candidates(self.game_dir, self.resolved.version_id, kind)
        ]
        dialog = _VersionChoiceDialog(
            self,
            title=tr(
                "instances.detail.modify.change_loader"
                if is_loader
                else "instances.detail.modify.change_version"
            ),
            hint=tr(
                "instances.detail.modify.hint.loader"
                if is_loader
                else "instances.detail.modify.hint.version"
            ),
            choices=installed,
            empty_text=tr(
                "instances.detail.modify.empty.loader"
                if is_loader
                else "instances.detail.modify.empty.version"
            ),
            searchable=not is_loader,
            loading_text=tr(
                "instances.detail.modify.loading_loaders"
                if is_loader
                else "instances.detail.modify.loading_versions"
            ),
        )
        if is_loader:
            self._probe_loader_choices(dialog, current, installed)
        else:
            self._load_catalog_choices(dialog, current)
        dialog.ask()
        chosen = dialog.chosen
        dialog.deleteLater()
        if chosen is None:
            return
        self._confirm_switch(chosen)

    def _load_catalog_choices(self, dialog: _VersionChoiceDialog, current) -> None:
        """Fill the official half of the version chooser (cache now, network only when cold)."""
        installed_ids = set(list_installed_versions(self.game_dir))
        current_version_id = self.resolved.version_id if self.resolved is not None else ""

        def add(versions: list) -> None:
            if not widget_alive(dialog):
                return
            dialog.add_choices(
                _catalog_choices(
                    versions,
                    installed_ids=installed_ids,
                    current_version_id=current_version_id,
                    current_minecraft=current.minecraft,
                )
            )
            dialog.set_status_text("")

        cached = _catalog_from_cache()
        if cached is not None:
            add(cached)
            return

        def on_error(message: str) -> None:
            if widget_alive(dialog):
                dialog.set_status_text(tr("instances.detail.modify.loading_failed", message))

        run_in_background(fetch_catalog, on_result=add, on_error=on_error)

    def _probe_loader_choices(
        self, dialog: _VersionChoiceDialog, current, installed: list[_SwitchChoice]
    ) -> None:
        """Fill the downloadable half of the loader chooser (one background probe)."""

        def on_result(payload) -> None:
            if not widget_alive(dialog):
                return
            try:
                options, failures = payload
            except (TypeError, ValueError):
                return
            choices = _loader_choices(options, installed, current.minecraft)
            dialog.add_choices(choices)
            if failures and not options:
                dialog.set_status_text(
                    tr("instances.detail.modify.loaders_failed", failures[0])
                )
            elif choices:
                dialog.set_status_text("")
            elif not failures:
                dialog.set_status_text(tr("instances.detail.modify.loaders_none"))

        def on_error(message: str) -> None:
            if widget_alive(dialog):
                dialog.set_status_text(tr("instances.detail.modify.loaders_failed", message))

        run_in_background(
            partial(loader_install_options, current.minecraft),
            on_result=on_result,
            on_error=on_error,
        )

    def _confirm_switch(self, target) -> None:
        """The second question: what the switch does, and whether to back the instance up first."""
        if self._require_instance() is None or self.resolved is None:
            return
        if not isinstance(target, _SwitchChoice):
            # ``VersionIdentity`` is the core's installed-profile record; tests and older callers
            # hand one straight to this method, and it means the same installed row.
            target = _SwitchChoice(
                version_id=target.version_id,
                minecraft=target.minecraft,
                loader=target.loader,
                installed=True,
                meta=target.version_id,
            )
        dialog = _SwitchConfirmDialog(
            self,
            current=self._identity_text(self.resolved.version_id),
            target=target.label,
            needs_install=target.needs_install,
        )
        outcome = dialog.ask()
        wants_backup = dialog.wants_backup
        dialog.deleteLater()
        if outcome is not inwindow_dialog.Outcome.ACCEPTED:
            return
        self._start_switch(target, backup=wants_backup)

    def _start_switch(self, target, *, backup: bool) -> None:
        """Run the install/switch (and the optional backup) in the background, never on the UI thread."""
        instance_id = self.instance_id
        if instance_id is None or self._switch_busy or self.resolved is None:
            return
        if isinstance(target, str):
            target = _SwitchChoice(
                version_id=target,
                minecraft=profile_identity(self.game_dir, target).minecraft,
                installed=True,
                meta=target,
            )
        if not isinstance(target, _SwitchChoice):
            raise TypeError(f"unsupported switch target: {target!r}")
        self._switch_busy = True
        self._switch_cancelled = False
        self.change_version_button.setEnabled(False)
        self.change_loader_button.setEnabled(False)
        self.modify_error.setVisible(False)
        self._switch_stop = threading.Event()
        self._switch_rate = RateTracker()
        self._switch_bridge = ProgressBridge()
        self._switch_bridge.progress.connect(self._on_switch_progress)
        self.switch_state_label.setText(
            tr(
                "instances.detail.modify.installing"
                if target.needs_install
                else "instances.detail.modify.switching",
                target.label,
            )
        )
        self.switch_state_label.setVisible(True)
        self.switch_progress_bar.setRange(0, 0)
        self.switch_progress_text.setVisible(False)
        self.switch_progress_row.setVisible(True)
        self.switch_progress_container.setVisible(True)
        self.switch_cancel_button.setEnabled(True)
        backup_dir = (paths.launcher_dir() / "backups") if backup else None
        cfg, _ = config.load()
        center = task_center_of(self)
        self._switch_task = (
            center.add_task(
                "tasks.title.install",
                target.installer_identity(),
                cancel=self._cancel_switch_job,
            )
            if center is not None
            else None
        )
        set_app_status(
            self,
            tr(
                "instances.detail.modify.installing"
                if target.needs_install
                else "instances.detail.modify.switching",
                target.label,
            ),
        )
        run_in_background(
            partial(
                _perform_switch,
                instance_id,
                target,
                self.game_dir,
                backup_dir=backup_dir,
                stop=self._switch_stop,
                report=self._switch_bridge,
                cache_dir=paths.launcher_dir() / "cache",
                runtime_dir=paths.launcher_dir() / "runtime",
                concurrency=cfg.max_concurrent_downloads,
            ),
            on_result=self._on_switched,
            on_error=self._on_switch_failed,
            on_finished=self._on_switch_finished,
        )

    def _on_switch_progress(self, payload) -> None:
        """A progress tick from the worker: the inline row and the task list see the same numbers."""
        task = self._switch_task
        phase = payload[0] if isinstance(payload, (tuple, list)) and payload else ""
        if phase == "backup":
            try:
                _done, done, total = payload
            except (TypeError, ValueError):
                return
            self.switch_progress_bar.setRange(0, 0)
            self.switch_progress_text.setVisible(False)
            self.switch_state_label.setText(tr("instances.detail.modify.backing_up"))
            if task is not None:
                task.set_progress(int(done), int(total))
            return
        if phase != "install" or len(payload) < 2:
            return
        progress = payload[1]
        total_files = int(getattr(progress, "total_files", 0) or 0)
        done_files = int(getattr(progress, "done_files", 0) or 0)
        if total_files:
            self.switch_progress_bar.setRange(0, 1000)
            self.switch_progress_bar.setValue(int(done_files * 1000 / total_files))
            self.switch_progress_text.setVisible(True)
            self.switch_state_label.setText(
                tr(
                    "instances.detail.modify.downloading",
                    done_files,
                    total_files,
                    getattr(progress, "current", ""),
                )
            )
        else:
            self.switch_progress_bar.setRange(0, 0)
        if task is not None:
            rate = 0.0
            rate_tracker = self._switch_rate
            if rate_tracker is not None:
                rate_tracker.set_total(int(getattr(progress, "total_bytes", 0) or 0))
                rate, _eta = rate_tracker.update(int(getattr(progress, "done_bytes", 0) or 0))
            task.set_progress(
                int(getattr(progress, "done_bytes", 0) or 0),
                int(getattr(progress, "total_bytes", 0) or 0),
                rate,
            )

    def cancel_switch(self) -> None:
        """The inline 取消: stop the transfer, or wait out an installer that cannot be interrupted."""
        task = self._switch_task
        if task is not None and task.is_running():
            task.cancel()
            return
        if self._switch_stop is not None:
            self._cancel_switch_job(task)

    def _cancel_switch_job(self, task) -> None:
        """Stop whatever the switch is doing; the pointer is only written after the last check."""
        self._switch_cancelled = True
        if self._switch_stop is not None:
            self._switch_stop.set()
        downloader.cancel_current_downloads()
        self.switch_cancel_button.setEnabled(False)
        self.switch_state_label.setText(tr("instances.detail.modify.cancelling"))
        if task is not None:
            set_app_status(self, tr("tasks.status.cancelled", task.title()))

    def _on_switched(self, result) -> None:
        """The switch is on disk: rebuild this page and let the instance cards follow."""
        if isinstance(result, _SwitchRun):
            outcome = result.outcome
            backup_path = result.backup_path
        else:  # a bare SwitchOutcome (kept for callers that only re-point an installed profile)
            outcome = result
            backup_path = outcome.backup_path
        target = outcome.target
        loader = target.loader or tr("instances.detail.modify.vanilla")
        task = self._switch_task
        if task is not None and task.is_running():
            task.finish()
        self.modify_error.setVisible(False)
        self.reload()
        self.changed.emit()  # the card list shows the new profile immediately, no restart
        name = f"{target.minecraft} · {loader}"
        if backup_path is not None:
            set_app_status(
                self,
                tr("instances.detail.modify.switched_backup", name, str(backup_path)),
            )
        else:
            set_app_status(self, tr("instances.detail.modify.switched", name))

    def _on_switch_failed(self, message: str) -> None:
        """The switch failed; the launcher's message already says whether the pointer was restored."""
        task = self._switch_task
        cancelled = bool(
            (self._switch_stop is not None and self._switch_stop.is_set())
            or downloader.is_user_cancelled()
            or (task is not None and task.state == CANCELLED)
        )
        self.reload()
        if cancelled:
            text = tr("instances.detail.modify.cancelled")
            self.modify_error.setText(text)
            self.modify_error.setVisible(True)
            if task is not None and task.state == CANCELLED:
                set_app_status(self, tr("tasks.status.cancelled", task.title()))
            else:
                set_app_status(self, text)
            return
        if task is not None:
            task.fail(message)
        current = self._identity_text(self.resolved.version_id) if self.resolved else ""
        text = tr("instances.detail.modify.failed", current, message)
        self.modify_error.setText(text)
        self.modify_error.setVisible(True)
        set_app_status(self, text, "error")

    def _on_switch_finished(self) -> None:
        self._switch_busy = False
        self._switch_bridge = None
        self._switch_stop = None
        self._switch_rate = None
        self._switch_task = None
        if self._switch_cancelled:
            # Only a cancellation this switch asked for is ours to clear; another page's running
            # download shares the downloader's process-wide flag and must keep its own stop request.
            downloader.clear_cancellation()
        self._switch_cancelled = False
        self.switch_progress_row.setVisible(False)
        self.switch_progress_container.setVisible(False)
        self.switch_state_label.setVisible(False)
        self.switch_cancel_button.setEnabled(True)
        has_instance = self.instance is not None and self.resolved is not None
        self.change_version_button.setEnabled(has_instance)
        self.change_loader_button.setEnabled(has_instance)

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

    def effective_version_id(self) -> str:
        """The profile the next launch will resolve (the pointer's target when 修改 set one).

        One accessor for the fact the launch path depends on, so a test can assert what 启动 would
        use without starting a process.
        """
        return self.resolved.version_id if self.resolved is not None else ""

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
        """导出为 zip: ask where with the native dialog, then hand the work to a task."""
        if self._require_instance() is None:
            return
        instance_id = self.instance_id
        dest, _filter = QFileDialog.getSaveFileName(
            self,
            tr("instances.detail.nav.export"),
            instance_id + ".zip",
            tr("instances.detail.export.filter"),
        )
        if not dest:
            return
        path = Path(dest)
        if path.suffix.lower() != ".zip":
            path = path.with_name(path.name + ".zip")
        self.export_to(path)

    def export_to(self, dest: Path) -> None:
        """Start one export into ``dest``: a task on the download page, with its real byte count."""
        instance_id = self.instance_id
        if instance_id is None:
            return
        if self._export_task is not None and self._export_task.is_running():
            return  # one export at a time; the running row owns 取消
        center = task_center_of(self)
        self._export_dest = Path(dest)
        self._export_stop = threading.Event()
        self._export_rate = RateTracker()
        self._export_bridge = ProgressBridge()
        self._export_bridge.progress.connect(self._on_export_progress)
        self._export_task = (
            center.add_task(
                "tasks.title.export",
                instance_id,
                kind="export",
                cancel=self._cancel_export,
            )
            if center is not None
            else None
        )
        set_app_status(self, tr("instances.detail.export.running", str(self._export_dest)))
        bridge = self._export_bridge

        def report(done: int, total: int) -> None:
            """Worker-thread hook: one queued signal per call, never a widget touch from here."""
            bridge((done, total))

        run_in_background(
            partial(
                export_instance,
                instance_id,
                self._export_dest,
                self.game_dir,
                on_progress=report,
                should_stop=self._export_stop.is_set,
            ),
            on_result=self._on_exported,
            on_error=self._on_export_failed,
            on_finished=self._on_export_finished,
        )

    def _on_export_progress(self, payload) -> None:
        """The worker's ``(done, total)`` pair, with the rate the tracker measured between them."""
        task = self._export_task
        rate_tracker = self._export_rate
        if task is None or rate_tracker is None:
            return
        try:
            done, total = payload
            done_bytes, total_bytes = int(done), int(total)
        except (TypeError, ValueError):
            return
        rate_tracker.set_total(total_bytes)
        rate, _eta = rate_tracker.update(done_bytes)
        task.set_progress(done_bytes, total_bytes, rate)

    def _cancel_export(self, task) -> None:
        """The row's 取消: ask the exporter to stop at its next file, and say so at once."""
        if self._export_stop is not None:
            self._export_stop.set()
        set_app_status(self, tr("tasks.status.cancelled", task.title()))

    def _on_exported(self, path) -> None:
        task, self._export_task = self._export_task, None
        if task is not None:
            task.finish()
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        set_app_status(
            self, tr("instances.detail.export.done", str(path), human_size(size))
        )

    def _on_export_failed(self, message: str) -> None:
        task, self._export_task = self._export_task, None
        if task is not None and task.state == CANCELLED:
            # The user stopped it: the export raising on purpose is not a failure to report.
            set_app_status(self, tr("tasks.status.cancelled", task.title()))
            return
        if task is not None:
            task.fail(message)
        set_app_status(self, tr("instances.msg.export_fail", message), "error")

    def _on_export_finished(self) -> None:
        """The worker is gone: drop the bridge so a late queued tick cannot touch it."""
        self._export_bridge = None
        self._export_stop = None

    # ---------- 导出为整合包: a standard pack Modrinth App / Prism / HMCL can import ----------

    def _pack_format(self) -> str:
        """The format the combo selects (the Modrinth one is the default and the fallback)."""
        from launcher.modpack import FORMAT_MODRINTH

        return str(self.export_pack_format.currentData() or FORMAT_MODRINTH)

    def _on_export_format_changed(self, _index: int = 0) -> None:
        """A different format is a different pack: recount before the user commits."""
        self._fill_export()

    def _fill_export(self) -> None:
        """Show what the selected format would carry: mods, overrides, file count and total size.

        The scan is local (counts and sizes only), so this runs on the GUI thread without a network
        round trip or a pause - the same reason the page can afford to refresh it on every format
        change. While an export runs the labels belong to the worker and are left alone.
        """
        from launcher.modpack import FORMAT_CURSEFORGE, plan_export

        if self.instance_id is None or self.resolved is None:
            self.export_pack_summary.setText(tr("instances.detail.value.none"))
            self.export_pack_button.setEnabled(False)
            self.export_pack_format.setEnabled(False)
            return
        if self._pack_task is not None and self._pack_task.is_running():
            return
        self.export_pack_format.setEnabled(True)
        fmt = self._pack_format()
        try:
            plan = plan_export(self.instance_id, self.game_dir, fmt=fmt)
        except Exception as exc:  # noqa: BLE001 - a broken instance must not break the page
            self.export_pack_summary.setText(
                tr("instances.detail.export.pack.summary.failed", str(exc))
            )
            self.export_pack_button.setEnabled(False)
            return
        if not plan.entries:
            self.export_pack_summary.setText(tr("instances.detail.export.pack.summary.none"))
            self.export_pack_button.setEnabled(False)
            return
        self.export_pack_summary.setText(
            tr(
                "instances.detail.export.pack.summary",
                plan.mod_count,
                plan.mod_count,
                plan.other_count,
                human_size(plan.total_bytes),
            )
        )
        self.export_pack_button.setEnabled(True)
        self.export_pack_note.setText(
            tr(
                "instances.detail.export.pack.note.curseforge"
                if fmt == FORMAT_CURSEFORGE
                else "instances.detail.export.pack.note"
            )
        )

    def export_pack(self) -> None:
        """导出为整合包: ask where with the native dialog, then hand the work to a task."""
        from launcher.modpack import FORMAT_MODRINTH

        if self._require_instance() is None:
            return
        fmt = self._pack_format()
        suffix = ".mrpack" if fmt == FORMAT_MODRINTH else ".zip"
        filter_key = (
            "instances.detail.export.pack.filter.modrinth"
            if fmt == FORMAT_MODRINTH
            else "instances.detail.export.pack.filter.curseforge"
        )
        base = self.instance.name if self.instance is not None else (self.instance_id or "")
        suggested = _safe_export_filename(base) + suffix
        dest, _selected = QFileDialog.getSaveFileName(
            self,
            tr("instances.detail.export.pack.title"),
            suggested,
            tr(filter_key),
        )
        if not dest:
            return
        path = Path(dest)
        if path.suffix.lower() != suffix:
            path = path.with_name(path.name + suffix)
        self.export_pack_to(path, fmt=fmt)

    def export_pack_to(self, dest: Path, *, fmt: str | None = None) -> None:
        """Start one pack export into ``dest``: worker thread, task row and inline progress."""
        from launcher.modpack import FORMAT_CURSEFORGE, export_modpack

        instance_id = self.instance_id
        if instance_id is None or self.resolved is None:
            return
        if self._pack_task is not None and self._pack_task.is_running():
            return  # one pack export at a time; the running row owns 取消
        chosen = fmt or self._pack_format()
        center = task_center_of(self)
        self._pack_fmt = chosen
        self._pack_dest = Path(dest)
        self._pack_stop = threading.Event()
        self._pack_rate = RateTracker()
        self._pack_bridge = ProgressBridge()
        self._pack_bridge.progress.connect(self._on_pack_progress)
        self._pack_task = (
            center.add_task(
                "tasks.title.export",
                instance_id,
                kind="export",
                cancel=self._cancel_pack_export,
            )
            if center is not None
            else None
        )
        self.export_pack_button.setEnabled(False)
        self.export_pack_format.setEnabled(False)
        self.export_pack_open_button.setVisible(False)
        self.export_pack_result.setVisible(False)
        self.export_pack_state.setText(
            tr(
                "instances.detail.export.pack.state.match.curseforge"
                if chosen == FORMAT_CURSEFORGE
                else "instances.detail.export.pack.state.hash"
            )
        )
        self.export_pack_state.setVisible(True)
        self.export_pack_progress_bar.setRange(0, 0)
        self.export_pack_progress_text.setVisible(False)
        self.export_pack_progress_row.setVisible(True)
        self.export_pack_progress_container.setVisible(True)
        self.export_pack_cancel_button.setEnabled(True)
        set_app_status(self, tr("instances.detail.export.running", str(self._pack_dest)))
        bridge = self._pack_bridge
        run_in_background(
            partial(
                export_modpack,
                instance_id,
                self._pack_dest,
                self.game_dir,
                fmt=chosen,
                on_progress=bridge,
                should_stop=self._pack_stop.is_set,
            ),
            on_result=self._on_pack_exported,
            on_error=self._on_pack_export_failed,
            on_finished=self._on_pack_export_finished,
        )

    def _on_pack_progress(self, progress) -> None:
        """One export tick: the inline row and the task list are fed from the same numbers."""
        from launcher.modpack import FORMAT_CURSEFORGE, PHASE_MATCH, PHASE_WRITE

        task = self._pack_task
        rate_tracker = self._pack_rate
        if task is None or rate_tracker is None:
            return
        phase = str(getattr(progress, "phase", "") or "")
        done_bytes = int(getattr(progress, "done_bytes", 0) or 0)
        total_bytes = int(getattr(progress, "total_bytes", 0) or 0)
        done_files = int(getattr(progress, "done_files", 0) or 0)
        total_files = int(getattr(progress, "total_files", 0) or 0)
        if phase == PHASE_WRITE:
            if total_bytes:
                self.export_pack_progress_bar.setRange(0, 1000)
                self.export_pack_progress_bar.setValue(
                    max(0, min(1000, round(done_bytes * 1000 / total_bytes)))
                )
                self.export_pack_progress_text.setVisible(True)
            self.export_pack_state.setText(
                tr(
                    "instances.detail.export.pack.state.write",
                    done_files,
                    total_files,
                )
            )
        elif phase == PHASE_MATCH:
            self.export_pack_progress_bar.setRange(0, 0)
            self.export_pack_progress_text.setVisible(False)
            self.export_pack_state.setText(
                tr(
                    "instances.detail.export.pack.state.match.curseforge"
                    if self._pack_fmt == FORMAT_CURSEFORGE
                    else "instances.detail.export.pack.state.match.modrinth"
                )
            )
        else:
            # Hash phase, or the first tick before a phase is named: an indeterminate bar is the
            # honest shape while the total is not yet meaningful.
            self.export_pack_progress_bar.setRange(0, 0)
            self.export_pack_progress_text.setVisible(False)
            self.export_pack_state.setText(tr("instances.detail.export.pack.state.hash"))
        rate_tracker.set_total(total_bytes)
        rate, _eta = rate_tracker.update(done_bytes)
        task.set_progress(done_bytes, total_bytes, rate)

    def cancel_export_pack(self) -> None:
        """The inline 取消: cancel the task, which is what flips its state and asks us to stop.

        The state has to change through :meth:`gui.tasks.Task.cancel` (not by setting the stop flag
        directly): the failure handler distinguishes "the user stopped it" from "it broke" by that
        state, and a directly-set flag would leave the row running until it failed.
        """
        task = self._pack_task
        if task is not None:
            task.cancel()
        elif self._pack_stop is not None:
            self._pack_stop.set()

    def _cancel_pack_export(self, task) -> None:
        if self._pack_stop is not None:
            self._pack_stop.set()
        self.export_pack_cancel_button.setEnabled(False)
        if task is not None:
            set_app_status(self, tr("tasks.status.cancelled", task.title()))
            if self.export_pack_state.isVisible():
                self.export_pack_state.setText(tr("instances.detail.modify.cancelling"))

    def _hide_pack_progress(self) -> None:
        self.export_pack_state.setVisible(False)
        self.export_pack_progress_container.setVisible(False)

    def _on_pack_exported(self, result) -> None:
        task, self._pack_task = self._pack_task, None
        self._pack_fmt = None
        if task is not None:
            task.finish()
        self._hide_pack_progress()
        self.export_pack_button.setEnabled(True)
        self.export_pack_format.setEnabled(True)
        path = Path(result.path)
        self._pack_last = path
        self.export_pack_open_button.setVisible(True)
        lines = [
            str(path),
            tr(
                "instances.detail.export.pack.report",
                result.files,
                result.overrides,
            ),
        ]
        if result.degraded:
            lines.append(tr("instances.detail.export.pack.offline"))
        self.export_pack_result.setText("\n".join(lines))
        self.export_pack_result.setVisible(True)
        set_app_status(
            self, tr("instances.detail.export.done", str(path), human_size(result.size))
        )

    def _on_pack_export_failed(self, message: str) -> None:
        task, self._pack_task = self._pack_task, None
        self._pack_fmt = None
        self._hide_pack_progress()
        self.export_pack_button.setEnabled(True)
        self.export_pack_format.setEnabled(True)
        if task is not None and task.state == CANCELLED:
            # The user stopped it: the exporter raising on purpose is not a failure to report.
            self.export_pack_state.setText(tr("instances.detail.export.pack.cancelled"))
            self.export_pack_state.setVisible(True)
            set_app_status(self, tr("tasks.status.cancelled", task.title()))
            return
        if task is not None:
            task.fail(message)
        text = tr("instances.msg.export_fail", message)
        self.export_pack_state.setText(text)
        self.export_pack_state.setVisible(True)
        set_app_status(self, text, "error")

    def _on_pack_export_finished(self) -> None:
        """The worker is gone: drop the bridge so a late queued tick cannot touch it."""
        self._pack_bridge = None
        self._pack_stop = None

    def _forget_exported_pack(self) -> None:
        """A different instance's archive must not stay on screen (the path would be a lie)."""
        self._pack_last = None
        self.export_pack_result.setVisible(False)
        self.export_pack_result.setText("")
        self.export_pack_open_button.setVisible(False)

    def open_exported_pack_folder(self) -> None:
        """打开所在文件夹: reveal the archive this page just wrote."""
        target = self._pack_last
        if target is None:
            return
        folder = target.parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            set_app_status(
                self, tr("instances.detail.diagnosis.open_failed", str(folder)), "error"
            )

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

    def _selected_row(self, list_widget: _ContentList, rows: list[_ContentRow]) -> _ContentRow | None:
        """The card the keyboard or the mouse selected, or None when nothing is selected.

        The list's current row is the selection: a card cannot hold the selection itself without
        becoming a focus stop of its own (sixty mods would then be sixty Tab stops). The row's ``key``
        is the untouched on-disk name, so a delete never resolves a name through display stripping.
        """
        index = list_widget.currentRow()
        if 0 <= index < len(rows):
            return rows[index]
        return None

    # ---------- online identity ----------

    def _game_version(self) -> str:
        """The Minecraft version the instance runs, or "" when the profile cannot be read."""
        if self.resolved is None:
            return ""
        try:
            return profile_identity(self.resolved.game_dir, self.effective_version_id()).minecraft
        except Exception:  # noqa: BLE001 - recognition is optional; a broken profile just means ""
            return ""

    def _all_rows(self):
        yield from self.mods_rows
        for rows in self.content_rows.values():
            yield from rows
        yield from self.saves_rows

    def _request_identity(
        self, key: str, *, name: str, modid: str, kind: str, loader: str = ""
    ) -> None:
        """Queue one lookup unless it is already known or already queued (and the switch is on)."""
        if not online_identity_enabled() or not identity.is_enabled() or self.resolved is None:
            return
        if key in self._identity_cache or key in self._identity_pending:
            return
        request = identity.LookupRequest(
            key=key,
            name=name,
            modid=modid,
            kind=kind,
            game_version=self._game_version(),
            loader=loader,
        )
        self._identity_pending.add(key)
        if not self._identity_queue.submit(request, self._identity_arrived):
            self._identity_pending.discard(key)

    def _identity_arrived(self, request: identity.LookupRequest, result: object) -> None:
        """Worker-thread callback: hop to the widget thread with a signal, never touch a widget here."""
        self.identity_ready.emit(request.key, result)

    def _on_identity_ready(self, key: str, result: object) -> None:
        self._identity_pending.discard(key)
        if self.resolved is None:
            return
        value = result if isinstance(result, identity.Identity) else None
        self._identity_cache[key] = value
        for row in self._all_rows():
            if row.key == key:
                self._apply_identity(row, value)
        # The tab counts are a summary of these answers, so they have to move when one arrives -
        # without rebuilding the list the user is reading.
        self._refresh_tab_counts(key)

    def _refresh_tab_counts(self, key: str) -> None:
        """Update the 全部/可更新 numbers after a late identity; refill only when that is the tab shown."""
        if key.startswith("mod:"):
            if self._mods:
                updates = sum(1 for mod in self._mods if self._mod_updatable(mod))
                self.mods_tabs.set_counts(len(self._mods), updates)
                if self.mods_tabs.mode() == "updates":
                    self._refill_mods()
            return
        for subdir in _CONTENT_SUBDIRS:
            kind = "resourcepack" if subdir == "resourcepacks" else "shader"
            if not key.startswith(kind + ":"):
                continue
            items = self._content_items.get(subdir, [])
            if items:
                updates = sum(1 for item in items if self._content_updatable(subdir, item))
                tabs = self.content_tabs[subdir]
                tabs.set_counts(len(items), updates)
                if tabs.mode() == "updates":
                    self._refill_content(subdir)
            return

    def _apply_identity(self, row: _ContentRow, value: identity.Identity | None) -> None:
        """Paint one row's online half: title/tags/summary, the update marker and the project icon."""
        row.icon_url = value.icon_url if value is not None else ""
        if value is None:
            row.apply(tags=(), description="", badge="", detail=tr("instances.detail.identity.unknown"))
            return
        title = row.title
        chinese = zh_name(value.slug)
        if chinese:
            title = chinese
        elif value.title:
            title = value.title
        updatable = bool(row.installed_version) and value.is_updatable(row.installed_version)
        row.apply(
            title=title,
            tags=value.categories[:2],
            description=value.description,
            detail="",
            badge=tr("instances.detail.badge.update") if updatable else "",
        )
        if value.icon_url:
            self._apply_remote_icon(row, value.icon_url)

    def _apply_remote_icon(self, row: _ContentRow, url: str) -> None:
        """Show the project icon: page cache, then the disk cache (read only), then a download.

        The disk cache is the store :mod:`launcher.net.http_cache` already owns for project icons, so
        a recognized row usually paints its real picture in the same pass that builds it. Only a miss
        starts a worker; the row keeps the neutral cube until the bytes arrive, and that arrival
        repaints this row alone (see :meth:`_on_icon_ready`) so the scroll position and the selection
        survive it.
        """
        if not url or row.artwork_is_local:
            return
        pixmap = self._icon_pixmaps.get(url)
        if pixmap is None:
            data = http_cache.cached_icon_bytes(url)
            if data:
                pixmap = pixmap_from_image_bytes(data)
                if pixmap is None:
                    # Not an image: drop the broken entry so the download can replace it.
                    identity.forget_cached_icon(url)
                else:
                    self._icon_pixmaps[url] = pixmap
        if pixmap is not None:
            row.set_icon_pixmap(pixmap)
            return
        self._request_remote_icon(url)

    def _request_remote_icon(self, url: str) -> None:
        """Download one project icon in the background; identical URLs share one request."""
        if url in self._icon_pending or not online_identity_enabled() or not identity.is_enabled():
            return
        self._icon_pending.add(url)
        run_in_background(
            lambda: http_cache.fetch_icon_bytes(url),
            on_result=lambda data, url=url: self._on_icon_ready(url, data),
            on_error=lambda _message, url=url: self._icon_pending.discard(url),
            on_finished=lambda url=url: self._icon_pending.discard(url),
        )

    def _on_icon_ready(self, url: str, data: object) -> None:
        """One download arrived: repaint the rows that asked for that URL, and only those rows."""
        try:
            self._icon_pending.discard(url)
        except RuntimeError:  # the page was torn down while the worker ran
            return
        if not isinstance(data, (bytes, bytearray)):
            return
        pixmap = pixmap_from_image_bytes(bytes(data))
        if pixmap is None:
            identity.forget_cached_icon(url)
            return
        self._icon_pixmaps[url] = pixmap
        for row in self._all_rows():
            if row.icon_url != url or row.artwork_is_local:
                continue
            try:
                row.set_icon_pixmap(pixmap)
            except RuntimeError:  # the row went away with its list
                continue

    # ---------- mods ----------

    def _reload_mods(self) -> None:
        if self.resolved is None:
            return
        set_app_status(self, tr("instances.mods.loading"))
        run_in_background(
            partial(scan_mods, self.resolved.mods_dir),
            on_result=self._fill_mods,
            on_error=self._on_mods_failed,
        )

    def _on_mods_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.mods.msg.load_fail", message), "error")

    def _fill_mods(self, mods: list) -> None:
        self._mods = mods
        self._set_badge_count("mods", len(mods))
        self._sync_sidebar()
        self._refill_mods()

    @staticmethod
    def _mod_key(mod) -> str:
        """Stable row key for a mod: the file name (with ``.disabled``) is what delete acts on."""
        return "mod:" + mod.file

    def _mod_updatable(self, mod) -> bool:
        if not identity.is_enabled():
            return False
        value = self._identity_cache.get(self._mod_key(mod))
        return bool(value is not None and value.is_updatable(mod.version))

    def _refill_mods(self) -> None:
        want = self.mods_filter.currentData()
        needle = self.mods_search.text().strip().lower()
        show_updates = self.mods_tabs.mode() == "updates"
        shown = []
        for mod in self._mods:
            if want == "enabled" and not mod.enabled:
                continue
            if want == "disabled" and mod.enabled:
                continue
            if needle and needle not in (
                mod.name + " " + mod.mod_id + " " + mod.file
            ).lower():
                continue
            if show_updates and not self._mod_updatable(mod):
                continue
            shown.append(mod)
        self.mods_list.clear_rows()
        self.mods_rows = []
        for mod in shown:
            row = self._make_mod_row(mod)
            self.mods_rows.append(row)
            self.mods_list.add_row(row)
        updates = sum(1 for mod in self._mods if self._mod_updatable(mod))
        self.mods_tabs.set_counts(len(self._mods), updates)
        self.mods_list.setVisible(True)
        if not shown:
            if show_updates and self._mods:
                self.mods_empty.setText(tr("instances.detail.tab.updatable.empty"))
            elif needle or want != "all":
                self.mods_empty.setText(tr("instances.mods.search.none"))
            else:
                self.mods_empty.setText(tr("instances.detail.mods.empty"))
            self.mods_empty.update_for(0)
        else:
            self.mods_empty.setVisible(False)
        if not shown and self._mods:
            set_app_status(self, tr("instances.mods.search.none"))
        else:
            set_app_status(self, tr("instances.mods.count", len(self._mods)))

    def _make_mod_row(self, mod) -> _ContentRow:
        title = zh_name(mod.mod_id) or mod.name
        meta = " · ".join(part for part in (mod.mod_id, mod.version) if part)
        row = _ContentRow(title, key=self._mod_key(mod), meta=meta)
        row.kind = "mod"
        row.installed_version = mod.version
        local_icon = pixmap_from_image_bytes(mod_icon_bytes(mod.path))
        if local_icon is not None:
            row.set_icon_pixmap(local_icon, local=True)
        else:
            row.set_icon_pixmap(None)
        row.add_checkbox(
            mod.enabled,
            lambda checked, mod=mod, row=row: self._toggle_mod(mod, checked, row),
        )
        display = display_file_name(mod.file)
        if display != mod.file:
            row.raw_name = mod.file
        if identity.is_enabled() and row.key in self._identity_cache:
            self._apply_identity(row, self._identity_cache[row.key])
        elif not identity.is_enabled():
            row.apply(detail=tr("instances.detail.identity.off"))
        else:
            self._request_identity(
                row.key,
                name=display_file_name(mod.file),
                modid=mod.mod_id,
                kind="mod",
                loader=mod.loader,
            )
        return row

    def _toggle_mod(self, mod, want: bool, row: _ContentRow) -> None:
        if mod.enabled == want:
            return
        try:
            set_mod_enabled(mod, want)
        except OSError as exc:
            set_app_status(self, tr("instances.mods.msg.toggle_fail", exc), "error")
            if row.check is not None:
                row.check.blockSignals(True)
                row.check.setChecked(mod.enabled)
                row.check.blockSignals(False)

    def _toggle_current_mod(self) -> None:
        """Space on the mods list: flip the current row, the keyboard route to the checkbox."""
        row = self._selected_row(self.mods_list, self.mods_rows)
        if row is None:
            return
        mod = next((item for item in self._mods if self._mod_key(item) == row.key), None)
        if mod is None or row.check is None:
            return
        row.check.setChecked(not mod.enabled)

    def delete_selected_mods(self) -> None:
        row = self._selected_row(self.mods_list, self.mods_rows)
        if row is None:
            set_app_status(self, tr("instances.mods.msg.need_select"), "warning")
            return
        mod = next((item for item in self._mods if self._mod_key(item) == row.key), None)
        if mod is None:
            return
        files = [mod.file]
        if not inwindow_dialog.question(
            self,
            tr("instances.mods.delete"),
            tr("instances.mods.delete.confirm", len(files)),
        ):
            return
        deleted = 0
        try:
            mod.path.unlink()
            deleted = 1
        except OSError:
            deleted = 0
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
            # The game records the enabled packs in the options file of the directory it runs in;
            # for an isolated instance that is the instance folder, otherwise the shared game root.
            enabled = read_enabled_resource_packs(resolved.launch_dir / "options.txt")
            return {
                subdir: scan_content_files(
                    resolve_content_dir(
                        resolved.game_dir,
                        subdir,
                        version_id=resolved.id,
                        isolated=resolved.isolated,
                    ),
                    enabled=enabled if subdir == "resourcepacks" else None,
                )
                for subdir in _CONTENT_SUBDIRS
            }

        run_in_background(work, on_result=self._fill_resources, on_error=self._on_content_failed)

    def _on_content_failed(self, message: str) -> None:
        """Report an unreadable folder in the status line *and* where the list would be.

        A failed read must not look like an empty instance: the list is hidden and the empty-state
        overlay carries the reason until the next successful read restores its normal text.
        """
        text = tr("instances.detail.res.msg.load_fail", message)
        set_app_status(self, text, "error")
        for subdir in _CONTENT_SUBDIRS:
            list_widget = self.content_lists.get(subdir)
            if list_widget is not None:
                list_widget.clear_rows()
            self.content_rows[subdir] = []
            empty = self.content_empty.get(subdir)
            if empty is not None:
                empty.setText(text)
                empty.setVisible(True)

    def _fill_resources(self, payload: dict) -> None:
        for subdir, items in payload.items():
            self._content_items[subdir] = items
            self._refill_content(subdir)
        # Both lists load in one pass (they live on two sibling pages), but the status line must not
        # mix them into one meaningless total: say each count, so either page reads correctly.
        set_app_status(
            self,
            tr(
                "instances.detail.res.counts",
                len(payload.get("resourcepacks", [])),
                len(payload.get("shaderpacks", [])),
            ),
        )

    def _content_updatable(self, subdir: str, item) -> bool:
        if not identity.is_enabled():
            return False
        kind = "resourcepack" if subdir == "resourcepacks" else "shader"
        value = self._identity_cache.get(f"{kind}:{item.name}")
        installed = identity.version_from_filename(item.name)
        return bool(value is not None and value.is_updatable(installed))

    def _refill_content(self, subdir: str) -> None:
        items = self._content_items.get(subdir, [])
        tabs = self.content_tabs[subdir]
        show_updates = tabs.mode() == "updates"
        shown = [
            item for item in items if not show_updates or self._content_updatable(subdir, item)
        ]
        list_widget = self.content_lists[subdir]
        list_widget.clear_rows()
        rows: list[_ContentRow] = []
        for item in shown:
            row = self._make_content_row(subdir, item)
            rows.append(row)
            list_widget.add_row(row)
        self.content_rows[subdir] = rows
        updates = sum(1 for item in items if self._content_updatable(subdir, item))
        tabs.set_counts(len(items), updates)
        list_widget.setVisible(True)
        empty = self.content_empty[subdir]
        empty.setText(self._content_empty_text.get(subdir, empty.text()))
        empty.update_for(len(shown))
        if not shown and show_updates and items:
            empty.setText(tr("instances.detail.tab.updatable.empty"))

    def _make_content_row(self, subdir: str, item) -> _ContentRow:
        kind = "resourcepack" if subdir == "resourcepacks" else "shader"
        display = display_file_name(item.name)
        row = _ContentRow(display, key=f"{kind}:{item.name}", meta=human_size(item.size))
        row.kind = kind
        row.installed_version = identity.version_from_filename(item.name)
        if subdir == "resourcepacks" and item.enabled is not None:
            state = "enabled" if item.enabled else "disabled"
            row.apply(status=tr("instances.detail.res.status." + state))
        local_icon = pixmap_from_image_bytes(pack_icon_bytes(item.path))
        row.set_icon_pixmap(local_icon, local=local_icon is not None)
        if display != item.name:
            row.raw_name = item.name
        if identity.is_enabled() and row.key in self._identity_cache:
            self._apply_identity(row, self._identity_cache[row.key])
        elif not identity.is_enabled():
            row.apply(detail=tr("instances.detail.identity.off"))
        else:
            query = identity.pack_search_name(item.name)
            self._request_identity(row.key, name=query, modid="", kind=kind)
        return row

    def delete_selected_content(self, subdir: str) -> None:
        if self._require_instance() is None:
            return
        row = self._selected_row(self.content_lists[subdir], self.content_rows[subdir])
        if row is None:
            set_app_status(self, tr("instances.detail.res.msg.need_select"), "warning")
            return
        names = [row.key.split(":", 1)[1]]
        # A destructive question: Enter must keep the safe answer, so the default is the cancel
        # button and only an explicit click on 删除所选 removes anything.
        outcome = inwindow_dialog.confirm(
            self,
            tr("instances.mods.delete"),
            tr("instances.detail.res.delete.confirm", len(names)),
            accept_text=tr("instances.mods.delete"),
            reject_text=tr("dialog.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="critical",
        )
        if outcome is not inwindow_dialog.Outcome.ACCEPTED:
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
        self.reload_resources()
        set_app_status(self, tr("instances.detail.res.msg.deleted", deleted))

    def _on_content_delete_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.detail.res.msg.delete_fail", message), "error")

    # ---------- saves ----------

    def reload_saves(self) -> None:
        if self.resolved is None:
            return
        target = self.resolved.launch_dir / "saves"
        set_app_status(self, tr("instances.detail.saves.loading"))

        run_in_background(
            partial(scan_saves, target),
            on_result=self._fill_saves,
            on_error=self._on_saves_failed,
        )

    def _on_saves_failed(self, message: str) -> None:
        text = tr("instances.detail.saves.msg.load_fail", message)
        set_app_status(self, text, "error")
        self.saves_list.clear_rows()
        self.saves_rows = []
        self.saves_empty.setText(text)
        self.saves_empty.setVisible(True)

    def _fill_saves(self, saves: list) -> None:
        self._saves = saves
        self._set_badge_count("saves", len(saves))
        self._sync_sidebar()
        self.saves_list.clear_rows()
        self.saves_rows = []
        for save in saves:
            row = self._make_save_row(save)
            self.saves_rows.append(row)
            self.saves_list.add_row(row)
        self.saves_list.setVisible(True)
        self.saves_empty.setText(self.saves_empty_text)
        self.saves_empty.update_for(len(saves))
        set_app_status(self, tr("instances.detail.saves.count", len(saves)))

    def _make_save_row(self, save) -> _ContentRow:
        row = _ContentRow(save.world_name, key=save.folder, detail=self._save_detail(save))
        row.kind = "save"
        row.set_icon_pixmap(pixmap_from_image_bytes(save.icon), local=save.icon is not None)
        if save.world_name != save.folder:
            # The folder is what delete/backup act on, so keep it discoverable when level.dat
            # renamed the world away from it.
            row.setToolTip(row.toolTip() + "\n" + save.folder)
        return row

    @staticmethod
    def _save_detail(save) -> str:
        """The second line: when the world was created, when it was last written, and its size."""
        created = format_day(save.created) if save.created else ""
        modified = format_day(save.modified)
        if created:
            times = tr("instances.detail.saves.times", created, modified)
        else:
            times = tr("instances.detail.saves.modified", modified)
        return times + " · " + human_size(save.size)

    def _selected_save_folders(self) -> list[str]:
        row = self._selected_row(self.saves_list, self.saves_rows)
        return [row.key] if row is not None else []

    def delete_selected_saves(self) -> None:
        if self._require_instance() is None:
            return
        folders = self._selected_save_folders()
        if not folders:
            set_app_status(self, tr("instances.detail.saves.msg.need_select"), "warning")
            return
        outcome = inwindow_dialog.confirm(
            self,
            tr("instances.detail.saves.delete.title"),
            tr("instances.detail.saves.delete.confirm", len(folders)),
            accept_text=tr("instances.mods.delete"),
            reject_text=tr("dialog.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="critical",
        )
        if outcome is not inwindow_dialog.Outcome.ACCEPTED:
            return
        base = self.resolved.launch_dir / "saves"

        def work() -> tuple[int, str]:
            deleted = 0
            errors: list[str] = []
            for folder in folders:
                try:
                    shutil.rmtree(base / folder)
                    deleted += 1
                except OSError as exc:
                    errors.append(f"{folder}: {exc}")
            return deleted, "; ".join(errors)

        run_in_background(
            work,
            on_result=self._on_saves_deleted,
            on_error=self._on_saves_delete_failed,
        )

    def _on_saves_deleted(self, result: tuple[int, str]) -> None:
        deleted, errors = result
        self.reload_saves()
        if errors:
            set_app_status(self, tr("instances.detail.saves.msg.delete_fail", errors), "error")
        else:
            set_app_status(self, tr("instances.detail.saves.msg.deleted", deleted))

    def _on_saves_delete_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.detail.saves.msg.delete_fail", message), "error")

    def backup_selected_save(self) -> None:
        """Zip one selected world, after asking where the archive should go."""
        if self._require_instance() is None:
            return
        folders = self._selected_save_folders()
        if len(folders) != 1:
            set_app_status(self, tr("instances.detail.saves.backup.need_one"), "warning")
            return
        folder = folders[0]
        saved_dir = self.resolved.launch_dir / "saves"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination, _filter = QFileDialog.getSaveFileName(
            self,
            tr("instances.detail.saves.backup.title"),
            str(saved_dir / f"{folder}-{stamp}.zip"),
            "Zip (*.zip)",
        )
        if not destination:
            return
        set_app_status(self, tr("instances.detail.saves.msg.backing_up", folder))
        run_in_background(
            partial(backup_save, saved_dir / folder, Path(destination)),
            on_result=self._on_save_backed_up,
            on_error=self._on_save_backup_failed,
        )

    def _on_save_backed_up(self, path) -> None:
        set_app_status(self, tr("instances.detail.saves.msg.backed_up", str(path)))

    def _on_save_backup_failed(self, message: str) -> None:
        set_app_status(self, tr("instances.detail.saves.msg.backup_fail", message), "error")

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
        # The profile the instance points at, not its folder name: 修改 may have re-pointed it, and
        # every launch path resolves through the same field (launcher.instances.Instance.version_id).
        version_id = resolved.version_id

        def do_prepare() -> object:
            try:
                account = resolve_launch_account(AccountStore(), cfg.selected_account, None)
                prepared = prepare_launch(
                    version_id,
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

        self.back_button = QPushButton("← " + tr("instances.detail.back"), self)
        self.back_button.setObjectName("backButton")
        self.back_button.setAccessibleName(tr("instances.detail.back"))
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(lambda: self.back_requested.emit())

        self.detail = InstanceDetail(self)
        self.no_selection = QLabel(tr("instances.select.hint"), self)
        self.no_selection.setObjectName("emptyState")
        self.no_selection.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_selection.setWordWrap(True)
        self.detail_stack = QStackedWidget(self)
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

    def set_back_origin(self, origin: str) -> None:
        """Name the page the back control leads to: the card grid, or the launch page."""
        key = "instances.detail.back.launch" if origin == "launch" else "instances.detail.back"
        self.back_button.setText("← " + tr(key))
        self.back_button.setAccessibleName(tr(key))

    def focus_back_button(self) -> None:
        """Keyboard users land on the way back, not on a card that is now hidden."""
        self.back_button.setFocus(Qt.FocusReason.OtherFocusReason)
