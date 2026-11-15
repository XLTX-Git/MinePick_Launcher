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
import re
import shutil
import threading
import time
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
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
from gui.image_retry import IconRetrier
from gui.pages.export_selection import ExportCategoryCheck, ExportFilePanel
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
from gui.pages.versions_install_flow import install_selection
from gui.pages.versions_page import (
    _GROUP_ORDER as VERSION_GROUP_ORDER,
)
from gui.pages.versions_page import (
    _GROUP_TITLE_KEYS as VERSION_GROUP_TITLE_KEYS,
)
from gui.pages.versions_page import (
    _group_of as version_group_of,
)
from gui.pages.versions_page import (
    _GroupHeader as VersionGroupHeader,
)
from gui.pages.versions_page import (
    _LatestCard as VersionLatestCard,
)
from gui.pages.versions_page import (
    human_datetime as version_human_datetime,
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
from launcher.instances import (
    ISOLATION_FOLLOW,
    ISOLATION_OFF,
    ISOLATION_ON,
    SWITCH_LOADER,
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
from launcher.modpack import (
    CATEGORIES as EXPORT_CATEGORIES,
)
from launcher.modpack import (
    DEFAULT_CATEGORIES as DEFAULT_EXPORT_CATEGORIES,
)
from launcher.modpack import (
    FORMAT_CURSEFORGE,
    FORMAT_MODRINTH,
    export_modpack,
)
from launcher.mods import identity
from launcher.mods.loaders import display_loader_name, list_loader_versions
from launcher.mods.local import install_mod_file, scan_mods, set_mod_enabled
from launcher.mods.modrinth import (
    delete_installed_content,
    resolve_content_dir,
)
from launcher.mods.modrinth import (
    fetch_versions as fetch_modrinth_versions,
)
from launcher.mods.modrinth import (
    install_mod as install_modrinth_project,
)
from launcher.mods.modrinth import (
    pick_file as pick_modrinth_file,
)
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


#: A Fabric API jar's name, as Modrinth and CurseForge write it: ``fabric-api-0.92.6+1.20.1.jar``.
#: The task's own rule also accepts the underscore spelling older builds/files used.
FABRIC_API_PATTERN = re.compile(r"fabric[-_]api[-_](?P<version>.+)\.jar$", re.IGNORECASE)


def fabric_api_jar(mods_dir: Path) -> Path | None:
    """The Fabric API jar in ``mods_dir``, or None - the one fact the 修改 row is built from.

    Case-insensitive because a modpack may carry ``Fabric-API-....jar``; both spellings are accepted.
    The first match in name order is returned: two copies cannot both be loaded, and picking one
    deterministically keeps the row stable across reloads.
    """
    try:
        if not mods_dir.is_dir():
            return None
        candidates = [
            entry
            for entry in mods_dir.iterdir()
            if entry.is_file()
            and entry.name.casefold().endswith(".jar")
            and (
                entry.name.casefold().startswith("fabric-api")
                or entry.name.casefold().startswith("fabric_api")
            )
        ]
    except OSError:
        return None
    return min(candidates, key=lambda entry: entry.name.casefold()) if candidates else None


def fabric_api_version(filename: str) -> str:
    """``fabric-api-0.92.6+1.20.1.jar`` -> ``0.92.6+1.20.1``; the filename when it does not parse."""
    name = Path(filename).name
    match = FABRIC_API_PATTERN.search(name)
    if match is not None:
        version = match.group("version").strip()
        if version:
            return version
    return name


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
#: The block icon the picker paints at the left of a row (the versions page uses the same size).
CHOICE_ROW_ICON = 24
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
    #: The API identifier behind ``loader`` (fabric / forge / neoforge / quilt). Empty for vanilla.
    #: A staged plan needs it: "who owns this row" is a different question from "what does its label
    #: say", and matching a translated sentence would be wrong the day a label changes.
    loader_id: str = ""
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


# ---------- 修改: the component rows ----------

#: The four components 修改 edits, in PCL's order. Only these four are supported: OptiFine,
#: LiteLoader, Cleanroom and the rest of PCL's list are deliberately absent.
MODIFY_MC = "minecraft"
MODIFY_LOADERS: tuple[str, ...] = ("fabric", "forge", "neoforge")
#: Fabric API is not a loader component in this launcher: it is an ordinary jar in ``mods/``, so it
#: gets a row of its own after Fabric instead of being folded into the loader choice. The key is
#: deliberately outside ``MODIFY_LOADERS``: a plan stages loader switches, and this jar's actions are
#: immediate (see the 修改 methods below).
MODIFY_FABRIC_API = "fabric_api"
MODIFY_ROW_ORDER: tuple[str, ...] = (MODIFY_MC, "fabric", MODIFY_FABRIC_API, "forge", "neoforge")
#: Loader id -> the proper noun the row shows. Loader names are names, not translated words.
LOADER_TITLES: dict[str, str] = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}
#: The overview's row geometry: a 44px header line, and the expanded body starts indented under the
#: value column so a build row reads as belonging to its loader.
MODIFY_ROW_HEIGHT = 44
MODIFY_ROW_NAME_WIDTH = 112
MODIFY_ICON_SIZE = 18
#: The summary's loader mark: the planned loader's logo, or the grass block for a loader-free core.
MODIFY_SUMMARY_ICON = 24
#: One loader build row inside an expanded row: 34px, the versions page's compact-row rhythm.
LOADER_BUILD_ROW_HEIGHT = 34
#: Groups expanded in the Minecraft picker build their rows a batch at a time (snapshots alone are
#: ~800 entries); the manifest is never turned into widgets all at once.
PICKER_BATCH = 50


def _pencil_pixmap(color: str, size: int = 14) -> QPixmap:
    """A small pencil glyph for 修改, drawn rather than taken from the font (no tofu risk)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.2, size / 11.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    # The body is one slanted stroke; the nib is a short cross stroke at its lower-left end.
    painter.drawLine(
        QPointF(size * 0.28, size * 0.72), QPointF(size * 0.76, size * 0.24)
    )
    painter.drawLine(
        QPointF(size * 0.24, size * 0.76), QPointF(size * 0.36, size * 0.64)
    )
    painter.drawLine(
        QPointF(size * 0.28, size * 0.72), QPointF(size * 0.24, size * 0.76)
    )
    painter.end()
    return pixmap


def _reset_pixmap(color: str, size: int = 16) -> QPixmap:
    """A small circular-arrow glyph for 开始重置 (PCL's rotate-ccw), drawn, not from the font."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.3, size / 10.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    box = QRectF(size * 0.20, size * 0.20, size * 0.60, size * 0.60)
    # Three quarters of a circle, then the arrow head at the open end (top left).
    painter.drawArc(box, 40 * 16, 280 * 16)
    head = size * 0.14
    tip = QPointF(size * 0.20, size * 0.30)
    painter.drawLine(tip, QPointF(tip.x() + head, tip.y() - head * 0.2))
    painter.drawLine(tip, QPointF(tip.x() + head * 0.2, tip.y() + head))
    painter.end()
    return pixmap


class _RowGlyphButton(QAbstractButton):
    """A 26px icon button that paints its own chevron or cross.

    Painted for the reason the versions page paints its chevrons: the UI font has no guaranteed
    U+203A/U+2715, and a missing glyph would be a tofu box in the middle of the row. It is still a
    real button - one Tab stop, Space/Enter activate it, and assistive technology reads the name the
    caller gives it.
    """

    SIZE = 26

    def __init__(self, shape: str, *, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shape = shape  # "chevron" | "up" | "cross"
        self._hover = False
        self.setObjectName("modifyRowGlyph")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(name)
        self.setToolTip(name)

    def shape(self) -> str:
        return self._shape

    def set_shape(self, shape: str) -> None:
        if self._shape != shape:
            self._shape = shape
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.SIZE, self.SIZE)

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

    def paintEvent(self, event) -> None:
        tokens = theme.current_tokens() or theme.theme_palette("light")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._hover and self.isEnabled():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("BG_HOVER", "#e6eaf0")))
            painter.drawRoundedRect(
                QRectF(0, 0, self.SIZE, self.SIZE), 6.0, 6.0
            )
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(tokens.get("FOCUS_RING", "#3b82f6")), 1.0))
            painter.drawRoundedRect(
                QRectF(0.5, 0.5, self.SIZE - 1.0, self.SIZE - 1.0), 6.0, 6.0
            )
        color = QColor(
            tokens.get("FG_HINT", "#4f6070")
            if self.isEnabled()
            else tokens.get("FG_DISABLED", "#9aa7b4")
        )
        pen = QPen(color)
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        middle = self.SIZE / 2.0
        if self._shape == "cross":
            arm = self.SIZE * 0.20
            painter.drawLine(
                QPointF(middle - arm, middle - arm), QPointF(middle + arm, middle + arm)
            )
            painter.drawLine(
                QPointF(middle - arm, middle + arm), QPointF(middle + arm, middle - arm)
            )
        elif self._shape == "up":
            arm = self.SIZE * 0.18
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(middle - arm, middle + arm * 0.7),
                        QPointF(middle, middle - arm * 0.7),
                        QPointF(middle + arm, middle + arm * 0.7),
                    ]
                )
            )
        else:
            arm = self.SIZE * 0.18
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(middle - arm * 0.7, middle - arm),
                        QPointF(middle + arm * 0.7, middle),
                        QPointF(middle - arm * 0.7, middle + arm),
                    ]
                )
            )
        painter.end()


class _ComponentRow(QWidget):
    """One component of the core: a name, its version (or why it cannot be chosen), and its actions.

    The row is a summary line, not a control: the whole row is not clickable (a mis-click would
    change a loader). The actions are the two small buttons on the right (修改 for Minecraft, ✕/›
    for a loader), each a real focusable button with its own accessible name.

    A loader row owns an expandable body. Expanding it lists that loader's compatible builds in
    place (``›`` becomes ``⌃``); the list is built by the page, because only the page knows which
    builds are installed and how to reach the loader source.
    """

    def __init__(self, key: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setObjectName("modifyComponentRow")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setObjectName("modifyComponentHeader")
        header.setMinimumHeight(MODIFY_ROW_HEIGHT)
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(8)
        self.name_label = QLabel(title)
        self.name_label.setObjectName("modifyRowName")
        self.name_label.setFixedWidth(MODIFY_ROW_NAME_WIDTH)
        self.icon_label = QLabel()
        self.icon_label.setObjectName("modifyRowIcon")
        self.icon_label.setFixedSize(MODIFY_ICON_SIZE, MODIFY_ICON_SIZE)
        self.icon_label.setVisible(False)
        self.value_label = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideRight)
        self.value_label.setObjectName("modifyRowValue")
        self.edit_button = QPushButton(tr("instances.detail.modify.edit"))
        self.edit_button.setObjectName("modifyRowAction")
        self.edit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_button.setIcon(QIcon(_pencil_pixmap(theme.current_tokens().get("FG_HINT", "#4f6070"))))
        self.edit_button.setIconSize(QSize(14, 14))
        self.edit_button.setVisible(False)
        self.change_button = _RowGlyphButton(
            "chevron", name=tr("instances.detail.modify.row.change")
        )
        self.change_button.setVisible(False)
        self.remove_button = _RowGlyphButton(
            "cross", name=tr("instances.detail.modify.row.remove")
        )
        self.remove_button.setVisible(False)
        row.addWidget(self.name_label)
        row.addWidget(self.icon_label)
        row.addWidget(self.value_label, 1)
        row.addWidget(self.edit_button)
        row.addWidget(self.remove_button)
        row.addWidget(self.change_button)
        outer.addWidget(header)
        self._header = header

        self.body = QWidget()
        self.body.setObjectName("modifyComponentBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(MODIFY_ROW_NAME_WIDTH, 0, 0, 8)
        self.body_layout.setSpacing(4)
        self.body.setVisible(False)
        outer.addWidget(self.body)
        self._expanded = False

    # ---------- data ----------

    def value_text(self) -> str:
        return self.value_label.text()

    def value_muted(self) -> bool:
        return bool(self.value_label.property("muted"))

    def set_value(
        self,
        text: str,
        *,
        muted: bool = False,
        icon: QPixmap | None = None,
        tooltip: str = "",
    ) -> None:
        """One line: the version with its loader mark, or the reason this component cannot be used."""
        self.value_label.setText(text)
        self.value_label.setToolTip(tooltip or text)
        if bool(self.value_label.property("muted")) != muted:
            self.value_label.setProperty("muted", muted)
            style = self.value_label.style()
            if style is not None:
                style.unpolish(self.value_label)
                style.polish(self.value_label)
        if icon is None or icon.isNull():
            self.icon_label.clear()
            self.icon_label.setVisible(False)
        else:
            self.icon_label.setPixmap(
                icon.scaled(
                    MODIFY_ICON_SIZE,
                    MODIFY_ICON_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.icon_label.setVisible(True)

    def set_actions(self, *, edit: bool = False, change: bool = False, remove: bool = False) -> None:
        self.edit_button.setVisible(edit)
        self.change_button.setVisible(change)
        self.remove_button.setVisible(remove)

    def set_busy(self, busy: bool) -> None:
        for button in (self.edit_button, self.change_button, self.remove_button):
            button.setEnabled(not busy)

    def set_remove_name(self, name: str) -> None:
        self.remove_button.setAccessibleName(name)
        self.remove_button.setToolTip(name)

    # ---------- expansion ----------

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, *, shape: str | None = None) -> None:
        self._expanded = expanded
        self.body.setVisible(expanded)
        self.change_button.set_shape(shape or ("up" if expanded else "chevron"))
        self.setProperty("expanded", expanded)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)

    def clear_body(self) -> None:
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()


class _LoaderBuildRow(QAbstractButton):
    """One loader build inside an expanded row: version, stability and what choosing it does.

    Compact (34px, one line) on purpose: this list sits *inside* the card, under the row that owns
    it, so it must read as a sub-list rather than a second page. ``当前`` marks the build the
    instance runs now; every other row applies the normal switch flow when clicked.
    """

    def __init__(
        self,
        choice: _SwitchChoice,
        *,
        title: str,
        stability: str,
        current: bool,
        status: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.choice = choice
        self.title = title
        self.stability = stability
        self.current = current
        self.status = status
        self._hover = False
        self.setObjectName("loaderBuildRow")
        self.setFixedHeight(LOADER_BUILD_ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setEnabled(not current)
        parts = [title, stability, status]
        self.setAccessibleName(" · ".join(part for part in parts if part))
        self.setToolTip(self.accessibleName())

    def title_text(self) -> str:
        return self.title

    def stability_text(self) -> str:
        return self.stability

    def status_text(self) -> str:
        return self.status

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

    def paintEvent(self, event) -> None:
        tokens = theme.current_tokens() or theme.theme_palette("light")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if (self._hover and self.isEnabled()) or self.current:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(
                QColor(
                    tokens.get(
                        "ACCENT_TINT" if self.current else "BG_HOVER", "#eef2f7"
                    )
                )
            )
            painter.drawRoundedRect(
                QRectF(0, 0, self.width(), self.height()), 6.0, 6.0
            )
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(tokens.get("FOCUS_RING", "#3b82f6")), 1.0))
            painter.drawRoundedRect(
                QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0), 6.0, 6.0
            )
        title_font = QFont(self.font())
        title_font.setPixelSize(13)
        title_font.setWeight(QFont.Weight.DemiBold)
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        title_metrics = QFontMetrics(title_font)
        meta_metrics = QFontMetrics(meta_font)
        right = self.width() - 10 - meta_metrics.horizontalAdvance(self.status) - 12
        title_width = max(0, right - 10)
        text_color = QColor(
            tokens.get("FG_TEXT", "#1f2a36")
            if self.isEnabled()
            else tokens.get("FG_HINT", "#4f6070")
        )
        painter.setFont(title_font)
        painter.setPen(text_color)
        title = self.title_text()
        stability_width = (
            meta_metrics.horizontalAdvance(self.stability) + 10 if self.stability else 0
        )
        painter.drawText(
            QRectF(10, 0, max(0, title_width - stability_width), self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title_metrics.elidedText(
                title, Qt.TextElideMode.ElideRight, max(0, title_width - stability_width)
            ),
        )
        if self.stability:
            painter.setFont(meta_font)
            painter.setPen(QColor(tokens.get("FG_HINT", "#4f6070")))
            painter.drawText(
                QRectF(
                    10 + max(0, title_width - stability_width) + 8,
                    0,
                    stability_width,
                    self.height(),
                ),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.stability,
            )
        painter.setFont(meta_font)
        painter.setPen(
            QColor(
                tokens.get("ACCENT_TEXT", "#2f7d5b")
                if self.status
                else tokens.get("FG_HINT", "#4f6070")
            )
        )
        painter.drawText(
            QRectF(right, 0, self.width() - right - 10, self.height()),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            meta_metrics.elidedText(
                self.status, Qt.TextElideMode.ElideRight, max(0, self.width() - right - 10)
            ),
        )
        painter.end()


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


def _version_block_pixmap(
    version_id: str, version_type: str, *, installed: bool, game_dir: Path
) -> QPixmap:
    """The block the versions page would draw for this version, or an empty pixmap.

    Same function the versions page uses, so a row here and the same row there cannot disagree about
    what 26.3 looks like. Only an installed version can read its own texture from the game folder;
    everything else falls back to the drawn block.
    """
    from gui import block_icons
    from launcher.meta import block_textures

    try:
        category = block_textures.icon_category(version_id, version_type)
        block = block_textures.block_name_for_category(category)
        return block_icons.block_pixmap(
            block,
            CHOICE_ROW_ICON,
            game_dir=game_dir if installed else None,
            version_id=version_id,
            cache_root=block_textures.texture_cache_root(paths.launcher_dir()),
        )
    except Exception:  # noqa: BLE001 - an icon is decoration; a failure must not empty the row
        return QPixmap()


class _FitStack(QStackedWidget):
    """A stack whose height is the page on screen, not the tallest page it holds.

    ``QStackedWidget.sizeHint()`` is the maximum over every page, and the detail page puts this stack
    straight inside a ``QScrollArea``: a short tab therefore inherited the tall tab's height and could
    be scrolled far past its own content, into empty space. Reporting the current page keeps the
    scrollbar's range exactly as long as what is on screen.
    """

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        if page is None:
            return super().sizeHint()
        hint = page.sizeHint()
        return QSize(hint.width(), hint.height())

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        if page is None:
            return super().minimumSizeHint()
        return page.minimumSizeHint()


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
        #: The block standing for this version's type, when one could be drawn. Painted at the row's
        #: left edge; the text starts to its right.
        self._icon: QPixmap | None = None
        self.setAccessibleName(f"{self.label_text()} · {self.status_text()}")
        self.setAccessibleDescription(self.meta_text())
        self.setToolTip(self.meta_text())

    def set_block_icon(self, pixmap: QPixmap | None) -> None:
        """Give the row the same block the versions page would show for this version."""
        self._icon = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.update()

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
        if self._icon is not None:
            icon_size = CHOICE_ROW_ICON
            painter.drawPixmap(
                CHOICE_ROW_PADDING,
                (CHOICE_ROW_HEIGHT - icon_size) // 2,
                icon_size,
                icon_size,
                self._icon,
            )
            text_left = CHOICE_ROW_PADDING + icon_size + 10
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


@dataclass(frozen=True)
class _ResetRun:
    """What one 开始重置 left on disk (the profile it verified, and whether the loader was skipped)."""

    profile_id: str
    #: True when the current loader build is no longer offered by its source: only the vanilla core
    #: was re-installed, and the existing loader files were deliberately left untouched.
    loader_kept: bool = False


def _perform_reset(
    minecraft: str,
    loader_id: str,
    loader_version: str,
    game_dir: Path,
    *,
    stop: threading.Event,
    report: ProgressBridge,
    cache_dir: Path | None,
    runtime_dir: Path | None,
    concurrency: int,
) -> _ResetRun:
    """Re-install the core this instance already runs, through the versions page's install path.

    The vanilla core is installed first (``install_base=True``: that is what re-downloads and
    verifies the jar, the libraries and the assets). The loader is re-installed only when its source
    still lists the exact build currently in use - a reset must not silently move the instance to a
    different loader build, so a build the source no longer offers means "keep the files" and the
    caller says so. ``base_version`` is never touched: there is no pointer to get wrong.
    """
    loader = None
    loader_kept = False
    if loader_id and loader_version:
        try:
            builds = list_loader_versions(loader_id, minecraft)
        except Exception:  # noqa: BLE001 - an unreachable source means "keep the loader files"
            builds = []
        loader = next(
            (build for build in builds if str(build.version) == str(loader_version)),
            None,
        )
        loader_kept = loader is None
    if stop.is_set():
        raise DownloadCancelled("cancelled")
    outcome = install_selection(
        minecraft,
        game_dir=game_dir,
        loader_version=loader,
        install_base=True,
        cache_dir=cache_dir,
        runtime_dir=runtime_dir,
        concurrency=concurrency,
        auto_install_java=True,
        progress=lambda progress: report(("install", progress)),
        should_stop=stop.is_set,
    )
    if stop.is_set():
        raise DownloadCancelled("cancelled")
    return _ResetRun(profile_id=outcome.profile_id, loader_kept=loader_kept)


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


@dataclass(frozen=True)
class _DiagnosisPayload:
    """Everything the 诊断 tab renders, computed on a worker thread.

    The tab used to compute this inline: reading every log, scanning the mods folder and running the
    knowledge base froze the window for seconds, and the user saw an empty page while it happened.
    The payload is plain data, so the worker never touches a widget.
    """

    record: object | None
    live: object | None
    log_path: str
    log_text: str
    reports: int


def _analyze_instance_data(instance_id, game_dir, instance, resolved):
    """Run the local knowledge base over one instance's logs (never raises)."""
    if resolved is None or instance_id is None:
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
            version_id=instance_id,
            game_dir=resolved.game_dir,
            launch_dir=resolved.launch_dir,
            instance=instance,
            mods_dir=resolved.mods_dir,
            memory_gb=resolved.memory_gb,
            java_path=resolved.java_path,
            # Only this instance's own logs: the launcher's shared log would otherwise explain a
            # crash that happened in another instance.
            launcher_logs_dir=instance_dir(game_dir, instance_id) / ".no-launcher-logs",
        )
        log_set = CrashLogSet.from_context(context)
        index = CrashModIndex.scan(resolved.mods_dir)
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


def _diagnosis_payload(instance_id, game_dir, instance, resolved) -> _DiagnosisPayload:
    """Read the logs, run the analysis and count crash reports - all off the GUI thread."""
    from gui.crash_viewer import collect_crash_reports
    from gui.dialogs.crash_dialog import load_recorded_diagnosis

    record = load_recorded_diagnosis(instance_dir(game_dir, instance_id))
    live = _analyze_instance_data(instance_id, game_dir, instance, resolved)
    folder = resolved.launch_dir if resolved is not None else game_dir
    log_path = ""
    text = ""
    if resolved is not None:
        path = resolved.launch_dir / "logs" / "latest.log"
        if not path.is_file():
            # A crash report is the other raw log the diagnosis points into; when latest.log is
            # already rotated away, show the newest report instead.
            reports = sorted(
                (resolved.launch_dir / "crash-reports").glob("crash-*.txt"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if reports:
                path = reports[0]
        log_path = str(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if len(text) > _LOG_TAIL_CHARS:
            text = text[-_LOG_TAIL_CHARS:]
    try:
        count = len(collect_crash_reports(folder))
    except OSError:
        count = 0
    return _DiagnosisPayload(
        record=record, live=live, log_path=log_path, log_text=text, reports=count
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
        #: The last core job's success sentence, kept on screen after the progress row is hidden.
        self._switch_result_text = ""
        #: 修改's staged plan: the _SwitchChoice a 开始修改 click would apply. None means 开始重置.
        self._plan: _SwitchChoice | None = None
        self._identity = None
        self._identity_minecraft = ""
        self._core_known = False
        self._remove_target = None
        self._modify_ready = False
        self._expanded_loader: str | None = None
        #: Fabric API (a jar in mods/, not a loader): the file the row currently shows, the fetched
        #: Modrinth versions, and the one background job's state. These actions are immediate and
        #: never staged into ``_plan``.
        self._fabric_api_jar: Path | None = None
        self._fabric_api_versions: list = []
        self._fabric_api_failure = ""
        self._fabric_api_loading = False
        self._fabric_api_busy = False
        self._fabric_api_generation = 0
        #: Loader builds by (loader, minecraft), and - under the same key - whether that loader has
        #: any build for that game version at all (True / False / None while a probe is running).
        self._loader_builds: dict[tuple[str, str], tuple[list, str]] = {}
        self._loader_availability: dict[tuple[str, str], object] = {}
        self._picker_kind = ""
        self._picker_versions: list = []
        self._picker_choices: dict[str, _SwitchChoice] = {}
        self._picker_group_state: dict[str, bool] = {}
        self._picker_limits: dict[str, int] = {}
        self._picker_generation = 0
        #: 导出 / 诊断 own a lazily loaded body; these flags are what the tab's placeholder reads.
        self._export_ready = False
        self._diagnosis_ready = False
        self._diagnosis_pending = False
        self._diagnosis_generation = 0
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
        self._pack_embed = False
        #: 导出's last scan (all categories), the form's instance and the destination it remembers.
        self._export_plan = None
        self._export_instance: str | None = None
        self._export_dest: Path | None = None
        self._export_dest_invalid = ""
        #: The per-file selection (instance-relative paths) and the categories whose file list is
        #: expanded. None means "not initialized for this instance yet"; the first scan fills it with
        #: the default categories' files.
        self._export_selected: set[str] | None = None
        self._export_expanded: set[str] = set()
        self.export_category_arrows: dict[str, QPushButton] = {}
        self.export_file_panels: dict[str, ExportFilePanel] = {}

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
        #: Same retry contract as the mods page: a cold icon that failed comes back on its own.
        self._icon_retry = IconRetrier(self, self._request_remote_icon)
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

        self.stack = _FitStack()
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
        # Not widgetResizable: that mode sizes the stack once and then only ever grows it, so a short tab
        # kept the previous tab's height and could be scrolled into empty space, while a tall tab got
        # clipped. The geometry is driven from the current page instead (_sync_content_geometry).
        self.content_scroll.setWidgetResizable(False)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_scroll.setWidget(self.stack)
        self.content_scroll.viewport().installEventFilter(self)
        self._sync_content_geometry()
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
                group.setMinimumHeight(SIDEBAR_GROUP_HEIGHT)
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
        badge.setMinimumHeight(18)
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

    def _sync_content_geometry(self) -> None:
        """Size the stack to the page on screen: viewport width, page height, never more.

        The scroll area is not widgetResizable, so this is the one place that decides the scrollable
        size. Height is the current page's own hint (``_FitStack`` reports exactly that), which keeps the
        scrollbar's range equal to the content: no empty space below a short tab, no clipped tall one.
        """
        viewport = self.content_scroll.viewport()
        if viewport is None or self.stack is None:
            return
        page = self.stack.currentWidget()
        wanted = self.stack.sizeHint().height()
        if page is not None:
            wanted = self._content_bottom(page)
        height = max(viewport.height(), wanted)
        size = QSize(viewport.width(), height)
        if self.stack.size() != size:
            self.stack.resize(size)

    @staticmethod
    def _content_bottom(page: QWidget) -> int:
        """Where the page's painted content actually ends, measured from visible leaf widgets.

        ``sizeHint()`` is not that number. Every tab ends in a stretch, and a container that fills the
        tab reports the whole tab as its own height, so the hint kept claiming ~300px more than the card
        drew - a short tab could be scrolled into blank space long after its content ended. Leaves (a
        label, a button - widgets with no visible children) cannot be stretched that way, so the lowest
        one is where the content really stops.
        """
        bottom = 0
        for child in page.findChildren(QWidget):
            if not child.isVisible() or child.isWindow():
                continue
            if any(grandchild.isVisible() for grandchild in child.children() if isinstance(grandchild, QWidget)):
                continue
            corner = child.mapTo(page, QPoint(0, child.height()))
            bottom = max(bottom, corner.y())
        return bottom + 24 if bottom else page.sizeHint().height()

    def eventFilter(self, watched, event) -> bool:
        viewport = getattr(self.content_scroll, "viewport", lambda: None)()
        if watched is viewport and event.type() == QEvent.Type.Resize:
            self._sync_content_geometry()
        return super().eventFilter(watched, event)

    def _show_section(self, index: int) -> None:
        if index != TAB_MODIFY:
            # The version picker is a sub-view of 修改, not a tab: leaving the tab returns to the
            # overview, and the sidebar's checked row is untouched either way.
            self._show_modify_overview()
        self.stack.setCurrentIndex(index)
        # The stack's size hint follows the current page (see _FitStack); the scrollable size follows it.
        self.stack.updateGeometry()
        self._sync_content_geometry()
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
        # 40px icon in a 50px square: #iconButton's 4px padding and 1px border already frame it.
        # The base QPushButton rule's text padding (4px 12px) otherwise claims a 54px minimum for a
        # control that has no label, so zero it out; the fixed square is the design.
        self.icon_button.setStyleSheet("padding: 0;")
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
        # Painted before the (seconds-long) analysis starts, so switching to the tab shows a
        # sentence instead of a blank content area.
        self.diagnosis_loading = EmptyState(tr("instances.detail.loading"))
        card_layout.addWidget(self.diagnosis_loading)

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
        """修改: the core as one summary line plus one row per component.

        Two views live in this panel and the sidebar never notices which one is showing:

        * the overview (this method's card) - instance summary, four component rows and 开始重置;
        * the version picker (``_build_modify_picker``) - what 修改 on the Minecraft row opens, with
          a 返回 button that changes nothing.

        A loader row does not switch views at all: its ``›`` expands the compatible builds *inside*
        the row (``›`` becomes ``⌃``), so comparing two loaders keeps the summary in sight.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # A Fabric instance without Fabric API is the state in which "the mods do not work" is the
        # next thing the user will report. The strip is above the card, in the page's own column, and
        # is shown only for that exact state (see _refresh_fabric_api).
        self.modify_warning = QLabel(tr("instances.detail.fabric_api.warning"))
        self.modify_warning.setObjectName("modifyWarning")
        self.modify_warning.setWordWrap(True)
        self.modify_warning.setVisible(False)
        layout.addWidget(self.modify_warning)

        self.modify_stack = QStackedWidget()
        self.modify_stack.setObjectName("instanceModifyStack")
        # 0 = overview, 1 = version picker. The order is also the default the page opens on.
        self.modify_stack.addWidget(self._build_modify_overview())
        self.modify_stack.addWidget(self._build_modify_picker())
        layout.addWidget(self.modify_stack)
        return page

    @staticmethod
    def _modify_separator() -> QFrame:
        """The 1px rule between component rows (QSS paints it from BORDER_LIST)."""
        line = QFrame()
        line.setObjectName("modifySeparator")
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(1)
        return line

    def _build_modify_overview(self) -> QWidget:
        """The overview card: what runs now, what can be changed, and 开始重置."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, card_layout = self._make_card()
        summary = QWidget()
        summary.setObjectName("modifySummary")
        summary_row = QHBoxLayout(summary)
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.setSpacing(12)
        self.modify_summary_icon = QLabel()
        self.modify_summary_icon.setObjectName("modifySummaryIcon")
        self.modify_summary_icon.setFixedSize(MODIFY_SUMMARY_ICON, MODIFY_SUMMARY_ICON)
        self.modify_summary_icon.setVisible(False)
        self.modify_summary_title = QLabel(tr("instances.detail.value.none"))
        self.modify_summary_title.setObjectName("modifySummaryTitle")
        self.modify_summary_subtitle = QLabel("")
        self.modify_summary_subtitle.setObjectName("hint")
        summary_row.addWidget(self.modify_summary_icon)
        summary_row.addWidget(self.modify_summary_title)
        summary_row.addWidget(self.modify_summary_subtitle, 1)
        card_layout.addWidget(summary)
        card_layout.addWidget(self._modify_separator())

        # The page used to paint nothing at all while the instance was being read: an EmptyState is
        # the honest placeholder, and it is what the tab shows until _fill_modify has real data.
        self.modify_loading = EmptyState(tr("instances.detail.loading"))
        card_layout.addWidget(self.modify_loading)

        self.modify_rows_container = QWidget()
        self.modify_rows_container.setObjectName("modifyRows")
        rows_layout = QVBoxLayout(self.modify_rows_container)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(0)
        self.modify_rows: dict[str, _ComponentRow] = {}
        self._fabric_api_separator_before: QFrame | None = None
        self._fabric_api_separator_after: QFrame | None = None
        for index, key in enumerate(MODIFY_ROW_ORDER):
            title = "Minecraft" if key == MODIFY_MC else (
                "Fabric API" if key == MODIFY_FABRIC_API else LOADER_TITLES[key]
            )
            row = _ComponentRow(key, title)
            if index:
                separator = self._modify_separator()
                rows_layout.addWidget(separator)
                if key == MODIFY_FABRIC_API:
                    self._fabric_api_separator_before = separator
                elif MODIFY_ROW_ORDER[index - 1] == MODIFY_FABRIC_API:
                    self._fabric_api_separator_after = separator
            rows_layout.addWidget(row)
            self.modify_rows[key] = row
        card_layout.addWidget(self.modify_rows_container)

        self.modify_rows[MODIFY_MC].edit_button.clicked.connect(self.change_game_version)
        for loader_key in MODIFY_LOADERS:
            row = self.modify_rows[loader_key]
            row.change_button.clicked.connect(
                partial(self.toggle_loader_row, loader_key)
            )
            row.remove_button.clicked.connect(partial(self.remove_loader, loader_key))
        fabric_api_row = self.modify_rows[MODIFY_FABRIC_API]
        fabric_api_row.change_button.clicked.connect(self.toggle_fabric_api_row)
        fabric_api_row.remove_button.clicked.connect(self.remove_fabric_api)

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
        self.modify_result = QLabel("")
        self.modify_result.setObjectName("hint")
        self.modify_result.setWordWrap(True)
        self.modify_result.setVisible(False)
        card_layout.addWidget(self.modify_result)

        card_layout.addWidget(self._modify_separator())
        self.reset_button = QPushButton(tr("instances.detail.modify.reset.start"))
        self.reset_button.setObjectName("modifyResetButton")
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.setAccessibleName(tr("instances.detail.modify.reset.start"))
        self.reset_button.setEnabled(False)
        self.reset_button.clicked.connect(self._on_modify_button)
        #: Completes a folder that other launchers left half-installed; sits next to 重置 because both
        #: answer "this instance does not run, make it run" - one rebuilds the core, one fetches the
        #: files that are simply absent. It never rewrites the profile.
        self.verify_button = QPushButton(tr("instances.detail.modify.verify"))
        self.verify_button.setObjectName("secondaryButton")
        self.verify_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.verify_button.setAccessibleName(tr("instances.detail.modify.verify"))
        self.verify_button.setEnabled(False)
        self.verify_button.clicked.connect(self._verify_files)
        reset_row = QHBoxLayout()
        reset_row.setContentsMargins(0, 4, 0, 0)
        reset_row.setSpacing(8)
        reset_row.addStretch(1)
        reset_row.addWidget(self.verify_button)
        reset_row.addWidget(self.reset_button)
        reset_row.addStretch(1)
        card_layout.addLayout(reset_row)

        # The core job reports in two places: this row (what the next second is doing, with a cancel
        # button) and the window's task list (the same numbers, surviving a page change). Both read
        # the one ProgressBridge, so they cannot disagree.
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
        return page

    def _build_modify_picker(self) -> QWidget:
        """The Minecraft version picker, shown in place of the overview (never a dialog).

        It reuses the versions page's own grouped-list widgets - the fold header and the latest-
        version card - so the two screens cannot drift apart. Rows are built per expanded fold, a
        batch at a time: the official manifest is ~900 entries and the picker is inside the panel,
        not a page of its own.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card, card_layout = self._make_card()
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.picker_back_button = QPushButton("← " + tr("instances.detail.modify.back"))
        self.picker_back_button.setObjectName("backButton")
        self.picker_back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.picker_back_button.setAccessibleName(tr("instances.detail.modify.back"))
        self.picker_back_button.clicked.connect(self._show_modify_overview)
        self.picker_title = QLabel("")
        self.picker_title.setObjectName("instanceCardTitle")
        header.addWidget(self.picker_back_button)
        header.addWidget(self.picker_title, 1)
        card_layout.addLayout(header)

        self.picker_subtitle = QLabel("")
        self.picker_subtitle.setObjectName("hint")
        self.picker_subtitle.setWordWrap(True)
        card_layout.addWidget(self.picker_subtitle)
        self.picker_status = QLabel("")
        self.picker_status.setObjectName("hint")
        self.picker_status.setWordWrap(True)
        self.picker_status.setVisible(False)
        card_layout.addWidget(self.picker_status)

        self.picker_body = QWidget()
        self.picker_body.setObjectName("modifyPickerBody")
        self.picker_body_layout = QVBoxLayout(self.picker_body)
        self.picker_body_layout.setContentsMargins(0, 0, 0, 0)
        self.picker_body_layout.setSpacing(8)
        card_layout.addWidget(self.picker_body)

        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_export(self) -> QWidget:
        """导出: the whole instance as a zip, or a standard modpack other launchers can import.

        Two products on purpose, in one card divided by a rule. The zip is a backup of this folder
        and only this launcher can read it back as an instance; the 整合包 is the portable artifact
        (``.mrpack`` / CurseForge zip). The pack half is the same shape PCL's page has: a name and a
        version, one checkbox per content category with its file count and size, an arrow that opens
        the category's per-file list, one advanced option (打包资源文件), the destination it will use,
        and one primary button. Nothing here opens a wizard: the page decides, then the export runs
        as a task with progress and 取消 in place.
        """
        from launcher.modpack import CATEGORIES

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

        # Lazily filled: the placeholder is what the tab shows before _fill_export has data, instead
        # of the blank page a slow first scan used to leave behind.
        self.export_loading = EmptyState(tr("instances.detail.loading"))
        card_layout.addWidget(self.export_loading)

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
        card_layout.addWidget(self._modify_separator())

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
        name_form = QFormLayout()
        style_form(name_form)
        format_label = QLabel(tr("instances.detail.export.pack.format"))
        format_label.setObjectName("hint")
        name_form.addRow(format_label, self.export_pack_format)

        self.export_name_edit = QLineEdit()
        self.export_name_edit.setMaxLength(120)
        self.export_name_edit.setAccessibleName(tr("instances.detail.export.name"))
        self.export_name_edit.textChanged.connect(self._on_export_name_changed)
        name_label = QLabel(tr("instances.detail.export.name"))
        name_label.setObjectName("hint")
        name_form.addRow(name_label, self.export_name_edit)

        self.export_version_edit = QLineEdit()
        self.export_version_edit.setMaxLength(40)
        self.export_version_edit.setAccessibleName(tr("instances.detail.export.version"))
        self.export_version_edit.textChanged.connect(self._on_export_name_changed)
        version_label = QLabel(tr("instances.detail.export.version"))
        version_label.setObjectName("hint")
        name_form.addRow(version_label, self.export_version_edit)
        card_layout.addLayout(name_form)

        content_label = QLabel(tr("instances.detail.export.content"))
        content_label.setObjectName("instanceCardTitle")
        card_layout.addWidget(content_label)
        self.export_category_rows: dict[str, tuple[QCheckBox, ElidedLabel]] = {}
        for category in CATEGORIES:
            line = QWidget()
            line.setObjectName("exportCategoryRow")
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(8)
            # The arrow has its own fixed-width slot: an empty category hides the arrow (there is
            # nothing to expand) while every checkbox in the column still starts at the same x.
            arrow_slot = QWidget()
            arrow_slot.setFixedWidth(24)
            arrow_layout = QHBoxLayout(arrow_slot)
            arrow_layout.setContentsMargins(0, 0, 0, 0)
            arrow_layout.setSpacing(0)
            arrow = QPushButton("▸")
            arrow.setObjectName("iconButton")
            arrow.setFixedWidth(24)
            arrow.setCursor(Qt.CursorShape.PointingHandCursor)
            arrow.clicked.connect(partial(self._toggle_export_category, category))
            arrow_layout.addWidget(arrow)
            check = ExportCategoryCheck(tr(f"instances.detail.export.category.{category}"))
            check.setTristate(True)
            check.setCursor(Qt.CursorShape.PointingHandCursor)
            check.setChecked(category in DEFAULT_EXPORT_CATEGORIES)
            check.stateChanged.connect(
                partial(self._on_export_category_changed, category)
            )
            stats = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideRight)
            stats.setObjectName("hint")
            stats.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            # The label takes the rest of the row and paints itself right-aligned: a fixed width put
            # it past the row's right edge (measured: geometry x == row width), so nothing showed.
            stats.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            row.addWidget(arrow_slot)
            row.addWidget(check)
            row.addWidget(stats, 1)
            self.export_category_rows[category] = (check, stats)
            self.export_category_arrows[category] = arrow
            card_layout.addWidget(line)

            # Built now, filled when the category is expanded: constructing one container per
            # category is cheap, while its 25 file rows only exist for the page actually shown.
            panel = ExportFilePanel(
                category,
                human_size,
                self._on_export_file_toggled,
                self._on_export_category_bulk,
            )
            panel.setVisible(False)
            self.export_file_panels[category] = panel
            card_layout.addWidget(panel)

        advanced_label = QLabel(tr("instances.detail.export.advanced"))
        advanced_label.setObjectName("instanceCardTitle")
        card_layout.addWidget(advanced_label)
        self.export_embed_check = QCheckBox(tr("instances.detail.export.embed"))
        self.export_embed_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_embed_check.toggled.connect(self._on_export_embed_changed)
        card_layout.addWidget(self.export_embed_check)
        self.export_embed_hint = QLabel("")
        self.export_embed_hint.setObjectName("hint")
        self.export_embed_hint.setWordWrap(True)
        card_layout.addWidget(self.export_embed_hint)

        destination_row = QHBoxLayout()
        destination_row.setContentsMargins(0, 0, 0, 0)
        destination_row.setSpacing(8)
        self.export_destination = ElidedLabel("", elide_mode=Qt.TextElideMode.ElideMiddle)
        self.export_destination.setObjectName("hint")
        self.export_dest_button = QPushButton(tr("instances.detail.export.change_dest"))
        self.export_dest_button.setObjectName("secondaryButton")
        self.export_dest_button.setAccessibleName(tr("instances.detail.export.change_dest"))
        self.export_dest_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_dest_button.clicked.connect(self.choose_export_folder)
        destination_row.addWidget(self.export_destination, 1)
        destination_row.addWidget(self.export_dest_button)
        card_layout.addLayout(destination_row)

        config_row = QHBoxLayout()
        config_row.setContentsMargins(0, 0, 0, 0)
        config_row.setSpacing(6)
        self.export_save_button = QPushButton(tr("instances.detail.export.config.save"))
        self.export_save_button.setObjectName("secondaryButton")
        self.export_save_button.setAccessibleName(
            tr("instances.detail.export.config.save")
        )
        self.export_save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_save_button.clicked.connect(self.save_export_config)
        self.export_load_button = QPushButton(tr("instances.detail.export.config.load"))
        self.export_load_button.setObjectName("secondaryButton")
        self.export_load_button.setAccessibleName(
            tr("instances.detail.export.config.load")
        )
        self.export_load_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_load_button.clicked.connect(self.load_export_config)
        config_row.addWidget(self.export_save_button)
        config_row.addWidget(self.export_load_button)
        config_row.addStretch(1)
        card_layout.addLayout(config_row)

        self.export_pack_summary = QLabel(tr("instances.detail.export.pack.summary.none"))
        self.export_pack_summary.setObjectName("hint")
        self.export_pack_summary.setWordWrap(True)
        card_layout.addWidget(self.export_pack_summary)

        self.export_pack_button = QPushButton(tr("instances.detail.export.start"))
        self.export_pack_button.setObjectName("modifyResetButton")
        self.export_pack_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_pack_button.setAccessibleName(tr("instances.detail.export.start"))
        self.export_pack_button.setEnabled(False)
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
        self.export_pack_format.setEnabled(False)
        self.export_name_edit.setEnabled(False)
        self.export_version_edit.setEnabled(False)
        self.export_save_button.setEnabled(False)
        self.export_load_button.setEnabled(False)
        self.export_dest_button.setEnabled(False)
        return page

    def _apply_log(self, payload: _DiagnosisPayload) -> None:
        self.log_path_label.setText(payload.log_path)
        self.log_view.setPlainText(payload.log_text)
        self.log_view.setVisible(bool(payload.log_text))
        self.log_empty.setVisible(not payload.log_text)

    def refresh_diagnosis(self) -> None:
        """Analyse this instance's logs off the GUI thread, showing a placeholder meanwhile.

        The analysis reads every log, scans the mods folder and runs the knowledge base: on a big
        instance that is seconds of work, and doing it on the GUI thread is what left the tab blank
        while the window stopped painting. The placeholder is painted first, the worker computes a
        plain-data payload, and the result is rendered back on this thread.
        """
        if self.instance_id is None:
            self.diagnosis_loading.setVisible(False)
            return
        self._diagnosis_generation += 1
        generation = self._diagnosis_generation
        self._diagnosis_ready = False
        self._diagnosis_pending = True
        self.diagnosis_loading.setText(tr("instances.detail.loading"))
        self.diagnosis_loading.setVisible(True)
        self.diagnosis_empty.setVisible(False)
        for widget in self._diagnosis_result_widgets:
            widget.setVisible(False)
        self.log_view.setVisible(False)
        self.log_empty.setVisible(False)
        run_in_background(
            partial(
                _diagnosis_payload,
                self.instance_id,
                self.game_dir,
                self.instance,
                self.resolved,
            ),
            on_result=partial(self._on_diagnosis_payload, generation),
            on_error=partial(self._on_diagnosis_failed, generation),
        )

    def _on_diagnosis_payload(self, generation: int, payload) -> None:
        if not widget_alive(self) or generation != self._diagnosis_generation:
            return
        self._diagnosis_pending = False
        self._diagnosis_ready = True
        self.diagnosis_loading.setVisible(False)
        self._render_diagnosis(payload)

    def _on_diagnosis_failed(self, generation: int, message: str) -> None:
        if not widget_alive(self) or generation != self._diagnosis_generation:
            return
        self._diagnosis_pending = False
        self._diagnosis_ready = False
        self.diagnosis_loading.setText(
            tr("instances.detail.loading_failed", str(message))
        )
        self.diagnosis_loading.setVisible(True)

    def _render_diagnosis(self, payload: _DiagnosisPayload) -> None:
        """Render one payload (the rendering half of the old synchronous refresh)."""
        record = payload.record
        has_record = bool(record and record.get("code"))
        live = payload.live
        has_live = bool(
            live is not None and live.analysable and not live.primary.fallback
        )

        self._apply_log(payload)
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
            has_log = bool(payload.log_text)
            # A crash report / hs_err is the "raw log" a result points into, so it starts
            # collapsed. A diagnosis made from latest.log alone keeps the log card open:
            # there is no crash report to expand, and the log is the only source shown.
            from launcher.diagnostics import KIND_CRASH_REPORT, KIND_HS_ERR

            collapse = any(
                entry.kind in (KIND_CRASH_REPORT, KIND_HS_ERR) for entry in live.logs
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
        self.diagnosis_reports_label.setText(
            tr("instances.detail.diagnosis.reports", payload.reports)
        )

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
            # A plan belongs to the instance it was staged on, and the picker to the core it listed.
            self._plan = None
            self._loader_builds.clear()
            self._loader_availability.clear()
            self._reset_fabric_api_state()
            self._show_modify_overview()
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
        # The lazy tabs describe the instance that just changed: their old answers must not stay.
        self._diagnosis_ready = False
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
        self._plan = None
        self.modify_error.setText("")
        self.modify_error.setVisible(False)
        self.modify_result.setText("")
        self.modify_result.setVisible(False)
        self.switch_progress_row.setVisible(False)
        self.switch_progress_container.setVisible(False)
        self.switch_state_label.setVisible(False)
        self._fill_modify()
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

    # ---------- 修改: reading and rendering ----------

    def _fill_modify(self) -> None:
        """Read the core the launch path really resolves and render the four component rows.

        The rows show a *plan* when one is staged: choosing a build, or removing a loader, only
        records what 开始修改 would do. ``_plan`` is the one piece of staged state; it is cleared when
        the plan is applied, when the instance changes, and when it would change nothing.
        """
        ready = self.instance is not None and self.resolved is not None
        self._modify_ready = ready
        self.modify_loading.setText(
            tr("instances.detail.loading")
            if self.instance_id
            else tr("instances.msg.need_select")
        )
        self.modify_loading.setVisible(not ready)
        self.modify_rows_container.setVisible(ready)
        self.modify_hint.setVisible(ready)
        self._collapse_loader_row()
        self._collapse_fabric_api_row()
        for row in self.modify_rows.values():
            row.clear_body()
            row.set_expanded(False)
        self._identity = None
        if not ready:
            self.modify_summary_title.setText(tr("instances.detail.value.none"))
            self.modify_summary_subtitle.setText("")
            self._remove_target = None
            self._reset_fabric_api_state()
            self.modify_warning.setVisible(False)
            for row in self.modify_rows.values():
                row.set_value(tr("instances.detail.value.none"), muted=True)
                row.set_actions()
            self._sync_modify_summary()
            self._sync_modify_button()
            return
        version_id = self.resolved.version_id
        identity = profile_identity(self.game_dir, version_id)
        core_folder = instance_dir(self.game_dir, core_profile_id(self.game_dir, version_id))
        self._core_known = version_json_path(core_folder) is not None
        self._identity = identity if self._core_known else None
        self._identity_minecraft = identity.minecraft
        # Safe removal: a loader that is unreadable, or a game version with no loader-free profile,
        # has no safe target - the cross is then simply not shown rather than pretending.
        self._remove_target = (
            self._safe_removal_target(version_id) if identity.loader_id else None
        )
        self.modify_summary_title.setText(self.instance.name)
        self._render_modify_rows()
        self._sync_modify_button()

    def _render_modify_rows(self) -> None:
        """Paint the rows from the staged plan when there is one, else from the current core.

        Compatibility is a property of the *planned* core: with NeoForge staged on a Fabric
        instance, Fabric's own row says "与 NeoForge 不兼容" - the row describes what applying the
        plan would mean, not what is on disk this second.
        """
        unknown = tr("instances.detail.modify.unknown")
        plan = self._plan
        identity = self._identity
        planned_loader = plan.loader_id if plan is not None else (
            identity.loader_id if identity is not None else ""
        )
        planned_loader_label = (
            LOADER_TITLES.get(planned_loader, planned_loader) if planned_loader else ""
        )
        planned_minecraft = (
            plan.minecraft
            if plan is not None and plan.minecraft
            else (identity.minecraft if identity is not None else "")
        )
        mc_row = self.modify_rows[MODIFY_MC]
        if identity is None:
            mc_row.set_value(unknown, muted=True)
            mc_row.set_actions(edit=True)
        else:
            mc_row.set_value(planned_minecraft or unknown, tooltip=planned_minecraft)
            mc_row.set_actions(edit=True)
        accent = (theme.current_tokens() or theme.theme_palette("light")).get(
            "ACCENT", "#35a06a"
        )
        for key in MODIFY_LOADERS:
            row = self.modify_rows[key]
            if identity is None:
                row.set_value(unknown, muted=True)
                row.set_actions()
                continue
            if key == planned_loader:
                if plan is not None and plan.loader_id == key:
                    build = self._loader_build(plan.loader) or plan.loader_version or key
                else:
                    build = self._loader_build(identity.loader) or identity.loader
                row.set_value(build, icon=block_icons.loader_pixmap(key, accent, MODIFY_ICON_SIZE))
                row.set_actions(change=True, remove=self._remove_target is not None)
                row.set_remove_name(
                    tr(
                        "instances.detail.modify.row.remove.named",
                        LOADER_TITLES.get(key, key),
                    )
                )
            elif planned_loader:
                # Another loader owns the plan: this row names it instead of offering a replacement
                # that would silently discard the staged one.
                row.set_value(
                    tr("instances.detail.modify.incompatible", planned_loader_label),
                    muted=True,
                )
                row.set_actions()
            elif self._loader_availability.get((key, planned_minecraft)) is False:
                # The fourth state: this loader has no build for the planned game version at all.
                # Distinct from 不兼容 - no other loader is in the way, the game version is unsupported.
                row.set_value(tr("instances.detail.modify.no_versions"), muted=True)
                row.set_actions()
            else:
                row.set_value(tr("instances.detail.modify.can_add"), muted=True)
                row.set_actions(change=True)
        self._sync_modify_summary()
        self._set_modify_busy(self._switch_busy)
        self._render_fabric_api_row()
        self._refresh_loader_availability(planned_minecraft)

    def _sync_modify_summary(self) -> None:
        """The first line: the current core, and - when a plan is staged - what it would become."""
        identity = self._identity
        if identity is None:
            self.modify_summary_icon.clear()
            self.modify_summary_icon.setVisible(False)
            if self.instance is not None:
                self.modify_summary_subtitle.setText(
                    tr("instances.detail.loading")
                )
            return
        vanilla = tr("instances.detail.modify.vanilla")
        current = f"{identity.minecraft or tr('instances.detail.modify.unknown')} · {identity.loader or vanilla}"
        plan = self._plan
        if plan is None:
            self.modify_summary_subtitle.setText(current)
            target_loader = identity.loader_id
            tooltip = current
        else:
            target = f"{plan.minecraft or identity.minecraft} · {plan.loader or vanilla}"
            self.modify_summary_subtitle.setText(f"{current} → {target}")
            target_loader = plan.loader_id
            tooltip = self.modify_summary_subtitle.text()
        self.modify_summary_subtitle.setToolTip(tooltip)
        pixmap = self._summary_icon(target_loader)
        if pixmap is None:
            self.modify_summary_icon.clear()
            self.modify_summary_icon.setVisible(False)
        else:
            self.modify_summary_icon.setPixmap(pixmap)
            self.modify_summary_icon.setVisible(True)

    def _summary_icon(self, loader_id: str):
        """The planned loader's own mark, or the grass block for a loader-free core."""
        try:
            if loader_id:
                accent = (theme.current_tokens() or theme.theme_palette("light")).get(
                    "ACCENT", "#35a06a"
                )
                pixmap = block_icons.loader_pixmap(loader_id, accent, MODIFY_SUMMARY_ICON)
            else:
                pixmap = block_icons.rule_icon_pixmap(
                    "grass",
                    MODIFY_SUMMARY_ICON,
                    game_dir=self.game_dir,
                    cache_root=block_textures.texture_cache_root(paths.launcher_dir()),
                )
        except Exception:  # noqa: BLE001 - a missing texture must not break the summary
            return None
        if pixmap is None or pixmap.isNull():
            return None
        return pixmap

    def _sync_modify_button(self) -> None:
        """One primary button: 开始重置 with no plan, 开始修改 with one."""
        has_plan = self._plan is not None
        text = tr(
            "instances.detail.modify.start.modify"
            if has_plan
            else "instances.detail.modify.reset.start"
        )
        self.reset_button.setText(text)
        self.reset_button.setAccessibleName(text)
        colour = (theme.current_tokens() or theme.theme_palette("light")).get(
            "FG_SELECTED", "#ffffff"
        )
        pixmap = _pencil_pixmap(colour, 16) if has_plan else _reset_pixmap(colour, 16)
        self.reset_button.setIcon(QIcon(pixmap))
        self.reset_button.setIconSize(QSize(16, 16))
        self.reset_button.setEnabled(self._modify_ready and not self._switch_busy)
        # Completing files needs the same "an instance is loaded and no core job is running" gate as
        # the primary button: both download into the same folder.
        self.verify_button.setEnabled(self._modify_ready and not self._switch_busy)

    def _set_modify_busy(self, busy: bool) -> None:
        """One core job at a time: the primary button and every row action follow the same flag."""
        self.reset_button.setEnabled(not busy and self._modify_ready)
        self.verify_button.setEnabled(not busy and self._modify_ready)
        for row in self.modify_rows.values():
            row.set_busy(busy)

    # ---------- 修改: the staged plan ----------

    def _plan_is_current(self, choice: _SwitchChoice) -> bool:
        """True when the target is the profile (or the core profile) the instance runs now.

        A thin instance folder runs a profile it only inherits, so "is this the current core?" has
        two right answers: the instance's own id and the profile the chain reads the core from.
        """
        if not choice.installed or self.resolved is None:
            return False
        current = self.resolved.version_id
        return choice.version_id in {
            current,
            core_profile_id(self.game_dir, current),
        }

    def _set_plan(self, choice: _SwitchChoice | None) -> None:
        """Stage (or clear) the plan a 开始修改 click would apply. Never touches the disk."""
        if choice is not None and self._plan_is_current(choice):
            choice = None
        self._plan = choice
        self._collapse_loader_row()
        self._show_modify_overview()
        self._render_modify_rows()
        self._sync_modify_button()

    def planned_target(self) -> _SwitchChoice | None:
        """The staged target, for tests and for the apply step."""
        return self._plan

    @staticmethod
    def _loader_build(label: str) -> str:
        """``Fabric 0.19.3`` -> ``0.19.3`` (the row already names the loader)."""
        text = str(label or "").strip()
        if not text:
            return ""
        _name, _sep, build = text.partition(" ")
        return build.strip() or text

    def _current_minecraft(self) -> str:
        if self.resolved is None:
            return ""
        return profile_identity(self.game_dir, self.resolved.version_id).minecraft

    def _planned_minecraft(self) -> str:
        """The game version the rows describe: the staged one when there is a plan, else current."""
        if self._plan is not None and self._plan.minecraft:
            return self._plan.minecraft
        return self._current_minecraft()

    def _refresh_loader_availability(self, minecraft: str) -> None:
        """Ask each loader source whether it has *any* build for the planned game version.

        Only a *changed* game version is probed. The current one needs no request: its loader is
        installed, and with no loader at all the three rows are addable by definition (the original
        rule for a vanilla instance), so the common path costs nothing and cannot stall on a network
        timeout. A loader that answers with an empty list is `无可用版本`; a failed request is not an
        empty answer, and the row stays addable (its expansion retries and reports the error).
        """
        if not minecraft or minecraft == self._current_minecraft():
            return
        for key in MODIFY_LOADERS:
            if (key, minecraft) in self._loader_availability:
                continue
            self._loader_availability[(key, minecraft)] = None
            run_in_background(
                partial(list_loader_versions, key, minecraft),
                on_result=partial(self._on_loader_availability, key, minecraft),
                on_error=partial(self._on_loader_availability_failed, key, minecraft),
            )

    def _on_loader_availability(self, key: str, minecraft: str, builds) -> None:
        if not widget_alive(self):
            return
        builds = list(builds or [])
        self._loader_availability[(key, minecraft)] = bool(builds)
        self._loader_builds[(key, minecraft)] = (builds, "")
        if minecraft == self._planned_minecraft():
            self._render_modify_rows()
            if self._expanded_loader == key:
                self._fill_loader_body(key)

    def _on_loader_availability_failed(self, key: str, minecraft: str, message: str) -> None:
        if not widget_alive(self):
            return
        self._loader_availability[(key, minecraft)] = "error"
        if minecraft == self._planned_minecraft():
            self._render_modify_rows()

    def _safe_removal_target(self, version_id: str):
        """The loader-free profile of this game version, or None when there is no safe way out.

        Removing a loader is a pointer move to an *installed* profile that carries no loader (the
        vanilla ``1.21.11`` next to a Fabric instance). When no such profile exists, the cross is not
        shown: this page does not offer deletions it cannot perform.
        """
        for candidate in switch_candidates(self.game_dir, version_id, SWITCH_LOADER):
            if not candidate.loader:
                return candidate
        return None

    def change_game_version(self) -> None:
        """The Minecraft row's 修改: open the grouped version picker *inside* this panel."""
        if self._switch_busy or not self._modify_ready or self.resolved is None:
            return
        self._collapse_loader_row()
        self._picker_generation += 1
        generation = self._picker_generation
        self._picker_kind = MODIFY_MC
        self._picker_versions = []
        self._picker_choices = {}
        self._picker_group_state = {}
        self._picker_limits = {}
        self.picker_title.setText(tr("instances.detail.modify.pick.version"))
        self.picker_subtitle.setText(
            tr("instances.detail.modify.pick.version.hint", self._current_minecraft())
        )
        self._clear_picker_body()
        self.modify_stack.setCurrentIndex(1)
        versions = _catalog_from_cache()
        if versions is None:
            self._set_picker_status(tr("instances.detail.modify.loading_versions"))
            run_in_background(
                fetch_catalog,
                on_result=partial(self._on_catalog_ready, generation),
                on_error=partial(self._on_catalog_failed, generation),
            )
            return
        self._fill_version_picker(versions)

    def change_loader(self, key: str | None = None) -> None:
        """Expand the build list of ``key`` (default: the loader in use, else Fabric).

        The list is in the row, not in a dialog and not on another page: it is the same row the
        user just looked at, and the summary above it keeps showing the current core.
        """
        if self._switch_busy or not self._modify_ready or self.resolved is None:
            return
        identity = profile_identity(self.game_dir, self.resolved.version_id)
        target = key or identity.loader_id or MODIFY_LOADERS[0]
        if target not in self.modify_rows:
            return
        self._show_modify_overview()
        self.expand_loader_row(target)

    # ---------- 修改: loader row expansion ----------

    def toggle_loader_row(self, key: str) -> None:
        """``›`` expands the compatible builds in place; ``⌃`` collapses the row again."""
        if key not in self.modify_rows or self._switch_busy or not self._modify_ready:
            return
        row = self.modify_rows[key]
        if row.is_expanded():
            self._collapse_loader_row(key)
            return
        self._collapse_loader_row()
        row.set_expanded(True)
        self._expanded_loader = key
        self._fill_loader_body(key)

    def expand_loader_row(self, key: str) -> None:
        """Open a row without toggling it shut (the programmatic form of ``›``)."""
        row = self.modify_rows.get(key)
        if row is None:
            return
        if row.is_expanded():
            self._fill_loader_body(key)
            return
        self.toggle_loader_row(key)

    def _collapse_loader_row(self, key: str | None = None) -> None:
        target = key or self._expanded_loader
        if target is None:
            return
        row = self.modify_rows.get(target)
        if row is not None and row.is_expanded():
            row.set_expanded(False)
            row.clear_body()
        if self._expanded_loader == target:
            self._expanded_loader = None

    def _fill_loader_body(self, key: str) -> None:
        """Paint the builds of one loader inside its row (cached answer, or a background probe)."""
        row = self.modify_rows.get(key)
        if row is None:
            return
        row.clear_body()
        minecraft = self._planned_minecraft()
        cached = self._loader_builds.get((key, minecraft))
        if cached is None:
            label = QLabel(tr("instances.detail.modify.loading_builds"))
            label.setObjectName("hint")
            label.setWordWrap(True)
            row.body_layout.addWidget(label)
            self._probe_loader_builds(key, minecraft)
            return
        builds, failure = cached
        items = self._loader_build_items(key, minecraft, builds)
        if not items:
            # Installed profiles are listed even when the source could not answer; only a row with
            # neither an installed profile nor a downloadable build has nothing to show.
            label = QLabel(
                tr("instances.detail.modify.builds_failed", failure)
                if failure
                else tr("instances.detail.modify.builds_none")
            )
            label.setObjectName("hint")
            label.setWordWrap(True)
            row.body_layout.addWidget(label)
            return
        for choice, title, stability, current, status in items:
            button = _LoaderBuildRow(
                choice, title=title, stability=stability, current=current, status=status
            )
            button.clicked.connect(partial(self._pick_loader_build, choice))
            row.body_layout.addWidget(button)

    def _probe_loader_builds(self, key: str, minecraft: str) -> None:
        """Ask the loader's own source for its builds of this game version (one request)."""
        run_in_background(
            partial(list_loader_versions, key, minecraft),
            on_result=partial(self._on_loader_builds, key, minecraft),
            on_error=partial(self._on_loader_builds_failed, key, minecraft),
        )

    def _on_loader_builds(self, key: str, minecraft: str, builds) -> None:
        if not widget_alive(self):
            return
        self._loader_builds[(key, minecraft)] = (list(builds or []), "")
        if self._expanded_loader == key and self._current_minecraft() == minecraft:
            self._fill_loader_body(key)

    def _on_loader_builds_failed(self, key: str, minecraft: str, message: str) -> None:
        if not widget_alive(self):
            return
        self._loader_builds[(key, minecraft)] = ([], str(message))
        if self._expanded_loader == key and self._current_minecraft() == minecraft:
            self._fill_loader_body(key)

    def _loader_build_items(self, key: str, minecraft: str, builds) -> list[tuple]:
        """(choice, title, stability, is_current, status) for one loader's expanded list.

        Installed profiles of this loader come first (choosing one only moves the pointer); the
        downloadable builds follow, and a build that is already installed is not offered twice.
        """
        version_id = self.resolved.version_id if self.resolved is not None else ""
        installed = [
            candidate
            for candidate in switch_candidates(self.game_dir, version_id, SWITCH_LOADER)
            if candidate.loader_id == key and candidate.minecraft == minecraft
        ]
        items: list[tuple] = []
        for candidate in installed:
            choice = replace(self._choice_for_identity(candidate), loader_id=key)
            is_current = self._plan_is_current(choice)
            items.append(
                (
                    choice,
                    self._loader_build(candidate.loader) or candidate.version_id,
                    "",
                    is_current,
                    tr(
                        "instances.detail.modify.build.current"
                        if is_current
                        else "instances.detail.modify.status.installed"
                    ),
                )
            )
        labels = {candidate.loader for candidate in installed}
        for build in builds:
            label = f"{display_loader_name(key)} {build.version}"
            if label in labels:
                continue
            choice = _SwitchChoice(
                version_id="",
                minecraft=minecraft,
                loader=label,
                loader_id=key,
                installed=False,
                meta=str(build.version),
                loader_version=build,
            )
            items.append(
                (
                    choice,
                    str(build.version),
                    tr(
                        "instances.detail.modify.build.stable"
                        if getattr(build, "stable", True)
                        else "instances.detail.modify.build.beta"
                    ),
                    False,
                    tr("instances.detail.modify.build.download"),
                )
            )
        return items

    def _pick_loader_build(self, choice: _SwitchChoice, _checked: bool = False) -> None:
        if self._switch_busy:
            return
        planned = self._planned_minecraft()
        if not choice.installed and planned:
            # A loader build is bound to its game version: staging it on the planned core (which may
            # be a different Minecraft version) is what makes one plan out of the two choices.
            choice = replace(choice, minecraft=planned)
        self._set_plan(choice)

    def remove_loader(self, key: str) -> None:
        """``✕``: stage a pointer move to the loader-free profile of the same game version."""
        if self._switch_busy or not self._modify_ready or self.resolved is None:
            return
        identity = profile_identity(self.game_dir, self.resolved.version_id)
        target = self._remove_target
        if target is None or identity.loader_id != key:
            return
        self._set_plan(replace(self._choice_for_identity(target), loader_id=""))

    # ---------- 修改: Fabric API (a jar in mods/, not a loader) ----------

    def _reset_fabric_api_state(self) -> None:
        """Forget the jar and the fetched list (a new instance, or no instance at all)."""
        self._fabric_api_generation += 1
        self._fabric_api_jar = None
        self._fabric_api_versions = []
        self._fabric_api_failure = ""
        self._fabric_api_loading = False
        self._fabric_api_busy = False
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is not None:
            row.setVisible(False)
        for separator in (
            self._fabric_api_separator_before,
            self._fabric_api_separator_after,
        ):
            if separator is not None:
                separator.setVisible(False)
        if hasattr(self, "modify_warning"):
            self.modify_warning.setVisible(False)

    def _fabric_api_relevant(self) -> bool:
        """Only a Fabric instance gets the row: Fabric API means nothing to Forge or vanilla."""
        return self._identity is not None and self._identity.loader_id == "fabric"

    def _fabric_api_jar_path(self) -> Path | None:
        if self.resolved is None:
            return None
        return fabric_api_jar(self.resolved.mods_dir)

    def _render_fabric_api_row(self) -> None:
        """Show the Fabric API row (and its warning) for the current, not the staged, loader.

        The row is about a jar that exists on disk right now. A staged plan that would switch this
        instance to Fabric has not created anything yet, so it must not make the row appear; after
        开始修改 the page re-reads the instance and the row follows. Nothing here calls
        ``_sync_modify_summary`` or ``_sync_modify_button``: this row is outside the plan.
        """
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is None:
            return
        relevant = self._fabric_api_relevant() and self.instance is not None
        row.setVisible(relevant)
        for separator in (
            self._fabric_api_separator_before,
            self._fabric_api_separator_after,
        ):
            if separator is not None:
                separator.setVisible(relevant)
        if not relevant:
            self._collapse_fabric_api_row()
            self._fabric_api_jar = None
            if hasattr(self, "modify_warning"):
                self.modify_warning.setVisible(False)
            return
        jar = self._fabric_api_jar_path()
        self._fabric_api_jar = jar
        if jar is not None:
            row.set_value(
                fabric_api_version(jar.name),
                tooltip=jar.name,
            )
            row.set_actions(remove=True)
            row.set_remove_name(
                tr("instances.detail.fabric_api.remove")
            )
        else:
            row.set_value(tr("instances.detail.modify.can_add"), muted=True)
            row.set_actions(change=True)
        self.modify_warning.setVisible(jar is None)
        if self._fabric_api_busy:
            row.set_busy(True)

    def toggle_fabric_api_row(self) -> None:
        """``›`` opens the Modrinth builds under the row, exactly like a loader's build list."""
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is None or not self._modify_ready or self._switch_busy or self._fabric_api_busy:
            return
        if not self._fabric_api_relevant():
            return
        if row.is_expanded():
            self._collapse_fabric_api_row()
            return
        self._collapse_loader_row()
        self._collapse_fabric_api_row()
        row.set_expanded(True)
        self._probe_fabric_api_versions()

    def _collapse_fabric_api_row(self) -> None:
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is not None and row.is_expanded():
            row.set_expanded(False)
            row.clear_body()

    def _probe_fabric_api_versions(self) -> None:
        """Ask Modrinth for the ``fabric-api`` builds of this instance's game version."""
        if self._fabric_api_loading or self._fabric_api_busy:
            return
        game_version = self._current_minecraft()
        if not game_version:
            return
        self._fabric_api_generation += 1
        generation = self._fabric_api_generation
        self._fabric_api_loading = True
        self._fabric_api_failure = ""
        self._fill_fabric_api_body()
        run_in_background(
            partial(
                fetch_modrinth_versions,
                "fabric-api",
                game_version=game_version,
            ),
            on_result=partial(self._on_fabric_api_versions, generation),
            on_error=partial(self._on_fabric_api_failed, generation),
        )

    def _on_fabric_api_versions(self, generation: int, versions) -> None:
        if not widget_alive(self) or generation != self._fabric_api_generation:
            return
        self._fabric_api_loading = False
        self._fabric_api_versions = list(versions or [])
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is not None and row.is_expanded():
            self._fill_fabric_api_body()

    def _on_fabric_api_failed(self, generation: int, message: str) -> None:
        if not widget_alive(self) or generation != self._fabric_api_generation:
            return
        self._fabric_api_loading = False
        self._fabric_api_failure = str(message)
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is not None and row.is_expanded():
            self._fill_fabric_api_body()

    def _fill_fabric_api_body(self) -> None:
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is None:
            return
        row.clear_body()
        if self._fabric_api_busy:
            label = QLabel(tr("instances.detail.fabric_api.downloading"))
            label.setObjectName("hint")
            label.setWordWrap(True)
            row.body_layout.addWidget(label)
            return
        if self._fabric_api_loading:
            label = QLabel(tr("instances.detail.fabric_api.loading"))
            label.setObjectName("hint")
            label.setWordWrap(True)
            row.body_layout.addWidget(label)
            return
        if self._fabric_api_failure:
            label = QLabel(
                tr("instances.detail.fabric_api.failed", self._fabric_api_failure)
            )
            label.setObjectName("hint")
            label.setWordWrap(True)
            row.body_layout.addWidget(label)
            retry = QPushButton(tr("instances.detail.fabric_api.retry"))
            retry.setObjectName("secondaryButton")
            retry.setAccessibleName(retry.text())
            retry.clicked.connect(self._probe_fabric_api_versions)
            row.body_layout.addWidget(retry)
            return
        if not self._fabric_api_versions:
            label = QLabel(
                tr(
                    "instances.detail.fabric_api.none",
                    self._current_minecraft() or "?",
                )
            )
            label.setObjectName("hint")
            label.setWordWrap(True)
            row.body_layout.addWidget(label)
            return
        caption = QLabel(tr("instances.detail.fabric_api.versions"))
        caption.setObjectName("hint")
        row.body_layout.addWidget(caption)
        for version in self._fabric_api_versions:
            date = str(getattr(version, "date_published", "") or "")[:10]
            choice = _SwitchChoice(
                version_id="",
                minecraft=self._current_minecraft(),
                loader="Fabric API",
                loader_id="fabric",
                installed=False,
                meta=str(getattr(version, "version_number", "") or ""),
            )
            item = _LoaderBuildRow(
                choice,
                title=str(getattr(version, "version_number", "") or "?"),
                stability="",
                current=False,
                status=date,
            )
            item.clicked.connect(
                partial(self._install_fabric_api_version, version)
            )
            row.body_layout.addWidget(item)

    def _install_fabric_api_version(self, version, _checked: bool = False) -> None:
        """Download one Fabric API build straight into the instance's mods folder.

        This is deliberately **not** part of the staged 修改 plan: a jar in ``mods/`` is ordinary
        content, and PCL's plan exists for loader/core switches. The download starts now, the row is
        the only thing refreshed at the end, and 开始重置 / 开始修改 keep describing the core.
        """
        if (
            self._fabric_api_busy
            or self._switch_busy
            or not self._modify_ready
            or self.resolved is None
        ):
            return
        file = pick_modrinth_file(version)
        if file is None or not file.url:
            set_app_status(self, tr("instances.detail.fabric_api.failed", ""), "error")
            return
        destination = self.resolved.mods_dir / file.filename
        self._fabric_api_busy = True
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is not None:
            row.set_busy(True)
        self._fill_fabric_api_body()
        run_in_background(
            partial(
                install_modrinth_project,
                "fabric-api",
                game_dir=self.game_dir,
                game_version=self._current_minecraft(),
                mod_version=version,
                dest=destination,
            ),
            on_result=partial(self._on_fabric_api_installed, version),
            on_error=self._on_fabric_api_install_failed,
        )

    def _on_fabric_api_installed(self, version, _info) -> None:
        if not widget_alive(self):
            return
        self._fabric_api_busy = False
        self._collapse_fabric_api_row()
        self._render_fabric_api_row()
        set_app_status(
            self,
            tr(
                "instances.detail.fabric_api.downloaded",
                str(getattr(version, "version_number", "") or ""),
            ),
        )

    def _on_fabric_api_install_failed(self, message: str) -> None:
        if not widget_alive(self):
            return
        self._fabric_api_busy = False
        self._fabric_api_failure = str(message)
        row = self.modify_rows.get(MODIFY_FABRIC_API)
        if row is not None:
            row.set_busy(False)
        self._fill_fabric_api_body()
        set_app_status(
            self, tr("instances.detail.fabric_api.failed", str(message)), "error"
        )

    def remove_fabric_api(self) -> None:
        """``✕``: delete the jar immediately. Destructive, so the safe button is the default."""
        if self._fabric_api_busy or not self._modify_ready or self.resolved is None:
            return
        jar = self._fabric_api_jar or self._fabric_api_jar_path()
        if jar is None:
            self._render_fabric_api_row()
            return
        outcome = inwindow_dialog.confirm(
            self,
            tr("instances.detail.fabric_api.remove.title"),
            tr("instances.detail.fabric_api.remove.body", jar.name),
            accept_text=tr("instances.detail.fabric_api.remove"),
            reject_text=tr("dialog.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="critical",
        )
        if outcome is not inwindow_dialog.Outcome.ACCEPTED:
            return
        try:
            jar.unlink()
        except OSError as exc:
            set_app_status(
                self,
                tr("instances.detail.fabric_api.delete_failed", str(exc)),
                "error",
            )
            self._render_fabric_api_row()
            return
        self._collapse_fabric_api_row()
        self._render_fabric_api_row()
        set_app_status(self, tr("instances.detail.fabric_api.deleted"))

    # ---------- 修改: the Minecraft version picker ----------

    def _show_modify_overview(self) -> None:
        self._collapse_loader_row()
        self.modify_stack.setCurrentIndex(0)

    def _clear_picker_body(self) -> None:
        while self.picker_body_layout.count():
            item = self.picker_body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _set_picker_status(self, text: str) -> None:
        self.picker_status.setText(text)
        self.picker_status.setVisible(bool(text))

    def _on_catalog_ready(self, generation: int, versions) -> None:
        if not widget_alive(self) or generation != self._picker_generation:
            return
        if self._picker_kind != MODIFY_MC or self.modify_stack.currentIndex() != 1:
            return
        self._set_picker_status("")
        self._fill_version_picker(list(versions or []))

    def _on_catalog_failed(self, generation: int, message: str) -> None:
        if not widget_alive(self) or generation != self._picker_generation:
            return
        self._set_picker_status(
            tr("instances.detail.modify.loading_failed", str(message))
        )

    def _fill_version_picker(self, versions: list) -> None:
        """Latest cards plus the versions page's four folds, with the installed state per row."""
        self._picker_versions = list(versions)
        self._picker_choices = {}
        self._clear_picker_body()
        layout = self.picker_body_layout
        latest: dict = {}
        for version in versions:
            kind = str(getattr(version, "type", "") or "")
            if kind in ("release", "snapshot") and kind not in latest:
                latest[kind] = version
        if latest:
            caption = QLabel(tr("instances.detail.modify.latest"))
            caption.setObjectName("hint")
            layout.addWidget(caption)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setSpacing(8)
            for kind in ("release", "snapshot"):
                version = latest.get(kind)
                if version is None:
                    continue
                card = VersionLatestCard(
                    kind,
                    tr(f"versions.latest.{kind}"),
                    on_click=partial(
                        self._pick_catalog_version, str(getattr(version, "id", ""))
                    ),
                )
                card.set_version(
                    str(getattr(version, "id", "")),
                    kind,
                    version_human_datetime(
                        str(getattr(version, "release_time", "") or "")
                    ),
                    _version_block_pixmap(
                        str(getattr(version, "id", "")),
                        str(getattr(version, "type", "") or kind),
                        installed=version_json_path(
                            instance_dir(self.game_dir, str(getattr(version, "id", "")))
                        )
                        is not None,
                        game_dir=self.game_dir,
                    ),
                )
                holder_layout.addWidget(card, 1)
            layout.addWidget(holder)
        buckets: dict[str, list] = {key: [] for key in VERSION_GROUP_ORDER}
        for version in versions:
            buckets[version_group_of(version)].append(version)
        for key in VERSION_GROUP_ORDER:
            entries = buckets[key]
            expanded = bool(self._picker_group_state.get(key, False))
            header = VersionGroupHeader(
                key,
                tr(VERSION_GROUP_TITLE_KEYS[key]),
                len(entries),
                expanded,
                on_toggle=self._toggle_picker_group,
            )
            layout.addWidget(header)
            if not expanded or not entries:
                continue
            limit = self._picker_limits.get(key, PICKER_BATCH)
            for version in entries[:limit]:
                choice = self._choice_for_catalog(version)
                self._picker_choices[choice.version_id] = choice
                row = _VersionChoiceRow(choice)
                row.set_block_icon(
                    _version_block_pixmap(
                        choice.version_id,
                        str(getattr(version, "type", "") or "release"),
                        installed=choice.installed,
                        game_dir=self.game_dir,
                    )
                )
                row.clicked.connect(partial(self._pick_choice, choice))
                layout.addWidget(row)
            if len(entries) > limit:
                more = QPushButton(tr("versions.group.more", len(entries) - limit))
                more.setObjectName("secondaryButton")
                more.setAccessibleName(more.text())
                more.clicked.connect(partial(self._load_more_picker, key))
                layout.addWidget(more)
        layout.addStretch(1)

    def _toggle_picker_group(self, key: str) -> None:
        self._picker_group_state[key] = not self._picker_group_state.get(key, False)
        self._picker_limits.pop(key, None)
        self._fill_version_picker(self._picker_versions)

    def _load_more_picker(self, key: str) -> None:
        self._picker_limits[key] = self._picker_limits.get(key, PICKER_BATCH) + PICKER_BATCH
        self._fill_version_picker(self._picker_versions)

    def _choice_for_catalog(self, version) -> _SwitchChoice:
        """One manifest entry as a choice: installed profiles point, everything else downloads."""
        version_id = str(getattr(version, "id", "") or "")
        if version_json_path(instance_dir(self.game_dir, version_id)) is not None:
            identity = profile_identity(self.game_dir, version_id)
            return _SwitchChoice(
                version_id=version_id,
                minecraft=identity.minecraft or version_id,
                loader=identity.loader,
                loader_id=identity.loader_id,
                installed=True,
                meta=version_id,
            )
        category = version_category(
            version_id, str(getattr(version, "type", "release") or "release")
        )
        type_label = tr(_VERSION_TYPE_LABELS.get(category, "versions.type.release"))
        released = str(getattr(version, "release_time", "") or "")[:10]
        return _SwitchChoice(
            version_id=version_id,
            minecraft=version_id,
            installed=False,
            meta=" · ".join(part for part in (type_label, released) if part),
        )

    def _pick_catalog_version(self, version_id: str, _checked: bool = False) -> None:
        version = next(
            (
                item
                for item in self._picker_versions
                if str(getattr(item, "id", "")) == version_id
            ),
            None,
        )
        if version is None:
            return
        self._pick_choice(self._choice_for_catalog(version))

    def _pick_choice(self, choice: _SwitchChoice, _checked: bool = False) -> None:
        if self._switch_busy:
            return
        self._set_plan(choice)

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
            loader_id=identity.loader_id,
            installed=True,
            meta=identity.version_id,
        )

    # ---------- 修改: the one primary button ----------

    def _verify_files(self) -> None:
        """Complete an instance that is missing files, without rebuilding its core.

        A folder another launcher left half-installed has a readable profile and no client jar; the game
        then dies in the class loader naming nothing. This is the manual half of the pre-launch repair
        the launch page runs - same planner, so it fetches exactly the files that are absent and leaves
        the profile, the loader and everything that checks out alone.
        """
        if not (self._modify_ready and self.instance_id and self.resolved):
            return
        from gui.workers import run_in_background

        version_id = self.resolved.version_id
        game_dir = self.game_dir
        self.verify_button.setEnabled(False)
        self.verify_button.setText(tr("instances.detail.modify.verify.checking"))
        self.modify_result.setVisible(True)
        self.modify_result.setText(tr("instances.detail.modify.verify.checking"))

        def work() -> str:
            from launcher.repair import repair

            plan = repair(version_id, game_dir=game_dir)
            if not plan.tasks:
                return tr("instances.detail.modify.verify.ok")
            megabytes = plan.total_bytes / (1024 * 1024) if plan.total_bytes else 0.0
            return tr(
                "instances.detail.modify.verify.done", len(plan.tasks), f"{megabytes:.0f}"
            )

        def done(message) -> None:
            self.modify_result.setText(str(message))
            self.verify_button.setText(tr("instances.detail.modify.verify"))
            self.verify_button.setEnabled(True)
            self._fill_modify()

        def failed(message) -> None:
            self.modify_result.setText(
                tr("instances.detail.modify.verify.failed", str(message))
            )
            self.verify_button.setText(tr("instances.detail.modify.verify"))
            self.verify_button.setEnabled(True)

        run_in_background(work, on_result=done, on_error=failed)

    def _on_modify_button(self) -> None:
        """开始重置 with no plan, 开始修改 with one."""
        if self._plan is None:
            self.reset_core()
        else:
            self.apply_plan()

    def apply_plan(self) -> None:
        """Apply the staged plan through the existing switch flow (backup, install, pointer)."""
        plan = self._plan
        if plan is None or self._switch_busy or self.resolved is None:
            return
        self._confirm_switch(plan)

    def reset_core(self) -> None:
        """开始重置: re-install the core this instance already runs (PCL's Reset).

        A reset never moves ``base_version``. It re-downloads and verifies the files the launch path
        resolves: the vanilla core of the game version, and the loader build only when the loader's
        source still offers exactly that build (otherwise the existing loader files are left alone
        and the result says so). A failure therefore cannot point the instance somewhere else - the
        page reloads, the pointer is unchanged, and the error stays on screen.
        """
        if self._switch_busy or not self._modify_ready or self.resolved is None:
            return
        identity = profile_identity(self.game_dir, self.resolved.version_id)
        if not identity.minecraft:
            set_app_status(self, tr("instances.detail.modify.reset.no_core"), "warning")
            return
        loader = identity.loader or tr("instances.detail.modify.vanilla")
        name = f"{identity.minecraft} · {loader}"
        accepted = inwindow_dialog.confirm(
            self,
            tr("instances.detail.modify.reset.confirm.title"),
            tr("instances.detail.modify.reset.confirm.body", name),
            accept_text=tr("instances.detail.modify.reset.start"),
        )
        if accepted is not inwindow_dialog.Outcome.ACCEPTED:
            return
        self._start_reset(identity)

    def _start_reset(self, identity) -> None:
        build = self._loader_build(identity.loader)

        def make_worker(stop, bridge, cfg):
            return partial(
                _perform_reset,
                identity.minecraft,
                identity.loader_id,
                build,
                self.game_dir,
                stop=stop,
                report=bridge,
                cache_dir=paths.launcher_dir() / "cache",
                runtime_dir=paths.launcher_dir() / "runtime",
                concurrency=cfg.max_concurrent_downloads,
            )

        self._begin_core_job(
            tr("instances.detail.modify.reset.running", identity.minecraft),
            make_worker,
            identity=identity.minecraft,
            on_result=self._on_reset_done,
            on_error=self._on_reset_failed,
        )

    def _on_reset_done(self, result) -> None:
        task, self._switch_task = self._switch_task, None
        if task is not None and task.is_running():
            task.finish()
        self.modify_error.setVisible(False)
        self.reload()
        self.changed.emit()
        name = self._identity_text(self.resolved.version_id) if self.resolved else ""
        key = (
            "instances.detail.modify.reset.done_loader_kept"
            if getattr(result, "loader_kept", False)
            else "instances.detail.modify.reset.done"
        )
        self.modify_result.setText(tr(key, name))
        self.modify_result.setVisible(True)
        set_app_status(self, self.modify_result.text())

    def _on_reset_failed(self, message: str) -> None:
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
        text = tr("instances.detail.modify.reset.failed", message)
        self.modify_error.setText(text)
        self.modify_error.setVisible(True)
        set_app_status(self, text, "error")

    def _confirm_switch(self, target) -> bool:
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
            return False
        self._start_switch(target, backup=wants_backup)
        return True

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
        backup_dir = (paths.launcher_dir() / "backups") if backup else None

        def make_worker(stop, bridge, cfg):
            return partial(
                _perform_switch,
                instance_id,
                target,
                self.game_dir,
                backup_dir=backup_dir,
                stop=stop,
                report=bridge,
                cache_dir=paths.launcher_dir() / "cache",
                runtime_dir=paths.launcher_dir() / "runtime",
                concurrency=cfg.max_concurrent_downloads,
            )

        self._begin_core_job(
            tr(
                "instances.detail.modify.installing"
                if target.needs_install
                else "instances.detail.modify.switching",
                target.label,
            ),
            make_worker,
            identity=target.installer_identity(),
            on_result=self._on_switched,
            on_error=self._on_switch_failed,
        )

    def _begin_core_job(self, state_text, make_worker, *, identity, on_result, on_error) -> bool:
        """Start one core job (a switch or a reset): busy flag, inline progress, task-center row.

        Both jobs share this because they must be indistinguishable to the user: one at a time, the
        same progress row with 取消, the same row in the window's task list, and the same rollback
        contract (the pointer is only written by the switch, after its own last check).
        """
        if self._switch_busy or self.resolved is None:
            return False
        self._switch_busy = True
        self._switch_cancelled = False
        self._switch_result_text = ""
        self._set_modify_busy(True)
        self.modify_error.setVisible(False)
        self.modify_result.setVisible(False)
        self._switch_stop = threading.Event()
        self._switch_rate = RateTracker()
        self._switch_bridge = ProgressBridge()
        self._switch_bridge.progress.connect(self._on_switch_progress)
        self.switch_state_label.setText(state_text)
        self.switch_state_label.setVisible(True)
        self.switch_progress_bar.setRange(0, 0)
        self.switch_progress_text.setVisible(False)
        self.switch_progress_row.setVisible(True)
        self.switch_progress_container.setVisible(True)
        self.switch_cancel_button.setEnabled(True)
        cfg, _ = config.load()
        center = task_center_of(self)
        self._switch_task = (
            center.add_task(
                "tasks.title.install", identity, cancel=self._cancel_switch_job
            )
            if center is not None
            else None
        )
        set_app_status(self, state_text)
        run_in_background(
            make_worker(self._switch_stop, self._switch_bridge, cfg),
            on_result=on_result,
            on_error=on_error,
            on_finished=self._on_switch_finished,
        )
        return True

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
        self._plan = None
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
        self.switch_cancel_button.setEnabled(True)
        self._fill_modify()
        if self._switch_result_text:
            self.switch_state_label.setText(self._switch_result_text)
            self.switch_state_label.setVisible(True)
        else:
            self.switch_state_label.setVisible(False)

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
        """Load what the tab shows, or leave its placeholder up until the data arrives.

        Every tab here is filled from data that is read, scanned or downloaded; a tab that is shown
        before any of it exists used to paint an empty content area (the reported several-second
        blank on 修改). The three lazy tabs therefore paint their placeholder first, and only the
        diagnosis tab really blocks on work - which is why its work runs on a worker thread.
        """
        if index == TAB_MODIFY:
            if force or not self._modify_ready:
                self._fill_modify()
            return
        if index == TAB_EXPORT:
            if force or not self._export_ready:
                self._fill_export()
            return
        if index == TAB_DIAGNOSIS:
            if force or not self._diagnosis_ready:
                self.refresh_diagnosis()
            return
        if index not in (*_RESOURCE_TABS, TAB_SAVES):
            return
        if not force and index in self._loaded_tabs:
            return
        self._loaded_tabs.add(index)
        if index in _RESOURCE_TABS:
            self.reload_resources()
        else:
            self.reload_saves()

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

    # ---------- 导出: state ----------

    def _pack_format(self) -> str:
        """The format the combo selects (the Modrinth one is the default and the fallback)."""
        return str(self.export_pack_format.currentData() or FORMAT_MODRINTH)

    def _on_export_format_changed(self, _index: int = 0) -> None:
        """A different format is a different pack: recount before the user commits."""
        self._fill_export()

    def export_pack_name(self) -> str:
        """The name the page will write into the pack (the field is authoritative)."""
        return self.export_name_edit.text().strip()

    def export_pack_version(self) -> str:
        return self.export_version_edit.text().strip()

    def _export_selected_files(self) -> frozenset[str]:
        """The ticked files, as instance-relative paths - exactly what the archive may carry."""
        return frozenset(self._export_selected or ())

    def _export_selection(self) -> frozenset[str]:
        """The categories that still have at least one ticked file (derived from the file set)."""
        plan = self._export_plan
        selected = self._export_selected_files()
        if plan is None:
            return frozenset()
        return frozenset(
            entry.category for entry in plan.all_entries if entry.relative in selected
        )

    def _entries_for_category(self, category: str) -> list:
        """The scanned files of one category, in scan order (the rows keep a stable order)."""
        plan = self._export_plan
        if plan is None:
            return []
        return [entry for entry in plan.all_entries if entry.category == category]

    def _selected_count(self, entries) -> int:
        selected = self._export_selected_files()
        return sum(1 for entry in entries if entry.relative in selected)

    # ---------- 导出: per-file selection ----------

    def _set_export_category_selected(self, category: str, select: bool) -> None:
        """全选 / 全不选 one category, then repaint the tri-state and the summary."""
        entries = self._entries_for_category(category)
        if not entries:
            return
        chosen = set(self._export_selected or ())
        for entry in entries:
            if select:
                chosen.add(entry.relative)
            else:
                chosen.discard(entry.relative)
        self._export_selected = chosen
        self._refresh_export_selection()

    def _on_export_file_toggled(self, relative: str, checked: bool) -> None:
        """One file box: the panel only reports it, this method owns the selection."""
        chosen = set(self._export_selected or ())
        if checked:
            chosen.add(relative)
        else:
            chosen.discard(relative)
        self._export_selected = chosen
        self._refresh_export_selection()

    def _on_export_category_bulk(self, category: str, select: bool) -> None:
        """The panel's 全选/全不选 button."""
        self._set_export_category_selected(category, select)

    def _on_export_category_changed(self, category: str, _state: int = 0) -> None:
        """A category box was clicked (or setChecked called): it means 全选 / 全不选.

        The partial state is only ever set programmatically, with signals blocked, so reaching it
        here means a caller set it without a selection to match; leaving the files alone is the
        honest answer instead of silently clearing them.
        """
        row = self.export_category_rows.get(category)
        if row is None:
            return
        state = row[0].checkState()
        if state == Qt.CheckState.PartiallyChecked:
            return
        self._set_export_category_selected(category, state == Qt.CheckState.Checked)

    def _toggle_export_category(self, category: str, _checked: bool = False) -> None:
        """The arrow: expand or collapse one category's paged file list."""
        if not self._entries_for_category(category):
            return
        if category in self._export_expanded:
            self._export_expanded.discard(category)
        else:
            self._export_expanded.add(category)
        self._refresh_export_selection()

    def _sync_export_arrow(self, category: str) -> None:
        """The arrow's direction, name and visibility (an empty category keeps no arrow)."""
        arrow = self.export_category_arrows.get(category)
        if arrow is None:
            return
        if not self._entries_for_category(category):
            arrow.setVisible(False)
            return
        expanded = category in self._export_expanded
        arrow.setVisible(True)
        arrow.setText("▾" if expanded else "▸")
        key = (
            "instances.detail.export.files.collapse"
            if expanded
            else "instances.detail.export.files.expand"
        )
        arrow.setAccessibleName(tr(key))
        arrow.setToolTip(tr(key))

    def _refresh_export_selection(self) -> None:
        """Repaint everything that depends on the file set: tri-states, arrows, panels, summary.

        One place owns the derivation (``selected files -> category state``), which is what keeps the
        summary, the nine category boxes and the expanded lists from ever disagreeing.
        """
        plan = self._export_plan
        if plan is None:
            return
        selected = self._export_selected_files()
        for category, (check, _stats) in self.export_category_rows.items():
            entries = self._entries_for_category(category)
            total = len(entries)
            chosen = self._selected_count(entries)
            if not total or not chosen:
                state = Qt.CheckState.Unchecked
            elif chosen == total:
                state = Qt.CheckState.Checked
            else:
                state = Qt.CheckState.PartiallyChecked
            check.blockSignals(True)
            check.setCheckState(state)
            check.setEnabled(total > 0)
            check.blockSignals(False)
            arrow = self.export_category_arrows.get(category)
            if arrow is not None:
                arrow.setVisible(total > 0)
            panel = self.export_file_panels.get(category)
            if panel is not None:
                panel.set_entries(
                    [(entry.relative, entry.size) for entry in entries]
                )
                expanded = total > 0 and category in self._export_expanded
                if not expanded and not total:
                    self._export_expanded.discard(category)
                panel.setVisible(expanded)
                if expanded:
                    panel.refresh(selected)
                self._sync_export_arrow(category)
        self._sync_export_summary()

    def _embed_files(self, fmt: str) -> bool:
        """打包资源文件 only means something for Modrinth (CurseForge has no URL list)."""
        return fmt == FORMAT_MODRINTH and self.export_embed_check.isChecked()

    def _export_preset(self):
        """The saved 导出 settings for this instance, or None."""
        if self.instance_id is None:
            return None
        cfg, _ = config.load()
        return cfg.export_presets.get(self.instance_id)

    def _store_export_preset(self, preset) -> None:
        if self.instance_id is None:
            return
        cfg, _ = config.load()
        cfg.export_presets[self.instance_id] = preset
        config.save(cfg)

    def _remember_export_folder(self, folder: Path) -> None:
        """Remember where this instance's pack was written (PCL's ``configPackPath``)."""
        from launcher.config import ExportPreset

        self._export_dest = Path(folder)
        self._export_dest_invalid = ""
        preset = self._export_preset() or ExportPreset()
        self._store_export_preset(preset.model_copy(update={"folder": str(folder)}))
        self._sync_export_destination()

    def _ensure_export_defaults(self) -> None:
        """First fill for this instance: the name, version and categories start from the defaults.

        Only the *destination folder* is restored automatically; the form itself waits for 读取配置.
        That is what makes the two config buttons mean something, and it keeps a saved preset from
        silently deciding what a later export contains.
        """
        if self._export_instance == self.instance_id:
            return
        self._export_instance = self.instance_id
        self._export_dest = None
        self._export_dest_invalid = ""
        self._export_plan = None
        # A new instance answers the file-level question from scratch: the first scan of this
        # instance selects the default categories' files. The panels are emptied here so no row of
        # the previous instance can be ticked against this one's plan.
        self._export_selected = None
        self._export_expanded.clear()
        for panel in self.export_file_panels.values():
            panel.clear()
        for arrow in self.export_category_arrows.values():
            arrow.setText("▸")
            arrow.setVisible(False)
        display = ""
        if self.instance is not None:
            display = self.instance.name or self.instance.id
        elif self.instance_id:
            display = self.instance_id
        self.export_name_edit.blockSignals(True)
        self.export_version_edit.blockSignals(True)
        try:
            self.export_name_edit.setText(_safe_export_filename(display) or "pack")
            self.export_version_edit.setText("1.0.0")
        finally:
            self.export_name_edit.blockSignals(False)
            self.export_version_edit.blockSignals(False)
        self.export_embed_check.blockSignals(True)
        self.export_embed_check.setChecked(False)
        self.export_embed_check.blockSignals(False)
        preset = self._export_preset()
        if preset is not None and preset.folder:
            folder = Path(preset.folder)
            if folder.is_dir():
                self._export_dest = folder
            else:
                self._export_dest_invalid = preset.folder

    def _reconcile_export_selection(self, previous, plan) -> None:
        """Carry the ticked files from one scan to the next; the first scan starts from the defaults.

        A file that disappeared is dropped (the disk is authoritative), and one that appeared in a
        category the user had fully ticked is added - otherwise a reload would silently leave new
        content out of a pack the user already described as "all of it". A category that was only
        partly ticked keeps exactly the remaining ticks.
        """
        present = {entry.relative for entry in plan.all_entries}
        if self._export_selected is None:
            self._export_selected = {
                entry.relative
                for entry in plan.all_entries
                if entry.category in DEFAULT_EXPORT_CATEGORIES
            }
            return
        old_selected = self._export_selected
        complete: set[str] = set()
        if previous is not None:
            old_by_category: dict[str, list[str]] = {}
            for entry in previous.all_entries:
                old_by_category.setdefault(entry.category, []).append(entry.relative)
            complete = {
                category
                for category, relatives in old_by_category.items()
                if relatives and all(path in old_selected for path in relatives)
            }
        kept = {path for path in old_selected if path in present}
        for entry in plan.all_entries:
            if entry.category in complete:
                kept.add(entry.relative)
        self._export_selected = kept

    def _sync_export_categories(self, plan) -> None:
        """One line per category: how many files and how much space the category holds."""
        stats = plan.category_stats()
        for key, (check, stats_label) in self.export_category_rows.items():
            stat = stats.get(key)
            files = stat.files if stat is not None else 0
            text = (
                tr(
                    "instances.detail.export.category.stat",
                    files,
                    human_size(stat.bytes if stat is not None else 0),
                )
                if files
                else tr("instances.detail.export.category.empty")
            )
            stats_label.setText(text)
            check.setAccessibleName(f"{check.text()} · {text}")
        self._refresh_export_selection()

    def _sync_export_advanced(self, fmt: str) -> None:
        embeddable = fmt == FORMAT_MODRINTH
        self.export_embed_check.setEnabled(embeddable)
        self.export_embed_hint.setText(
            tr(
                "instances.detail.export.embed.hint"
                if embeddable
                else "instances.detail.export.embed.curseforge"
            )
        )

    def _sync_export_note(self) -> None:
        fmt = self._pack_format()
        if self._embed_files(fmt):
            key = "instances.detail.export.pack.note.embed"
        elif fmt == FORMAT_CURSEFORGE:
            key = "instances.detail.export.pack.note.curseforge"
        else:
            key = "instances.detail.export.pack.note"
        self.export_pack_note.setText(tr(key))

    def _sync_export_summary(self) -> None:
        plan = self._export_plan
        if plan is None:
            self.export_pack_button.setEnabled(False)
            return
        selected = self._export_selected_files()
        chosen = [entry for entry in plan.all_entries if entry.relative in selected]
        if not chosen:
            self.export_pack_summary.setText(
                tr("instances.detail.export.pack.summary.none")
            )
            self.export_pack_button.setEnabled(False)
            self._sync_export_note()
            return
        self.export_pack_summary.setText(
            tr(
                "instances.detail.export.pack.summary.selected",
                len(chosen),
                human_size(sum(entry.size for entry in chosen)),
            )
        )
        self.export_pack_button.setEnabled(True)
        self._sync_export_note()

    def _sync_export_destination(self) -> None:
        dest = self._export_dest
        if dest is not None and dest.is_dir():
            self.export_destination.setText(
                tr("instances.detail.export.destination", str(dest))
            )
            self.export_destination.setToolTip(str(dest))
        else:
            invalid = self._export_dest_invalid or (
                str(self._export_dest) if self._export_dest is not None else ""
            )
            if invalid:
                self.export_destination.setText(
                    tr("instances.detail.export.destination.invalid", invalid)
                )
                self.export_destination.setToolTip(invalid)
            else:
                self.export_destination.setText(
                    tr("instances.detail.export.destination.none")
                )
                self.export_destination.setToolTip("")

    def _on_export_name_changed(self, _text: str = "") -> None:
        self._sync_export_summary()

    def _on_export_embed_changed(self, _checked: bool = False) -> None:
        self._sync_export_note()

    def choose_export_folder(self) -> None:
        """更改位置…: pick the destination folder this instance's pack is written into."""
        start = str(self._export_dest) if self._export_dest is not None else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, tr("instances.detail.export.change_dest"), start
        )
        if not chosen:
            return
        self._remember_export_folder(Path(chosen))

    def _suggested_pack_name(self, fmt: str) -> str:
        """``名称 版本.mrpack`` (PCL's file name rule: a space, and no version when it is empty)."""
        suffix = ".mrpack" if fmt == FORMAT_MODRINTH else ".zip"
        fallback = ""
        if self.instance is not None:
            fallback = self.instance.name or self.instance.id
        name = self.export_pack_name() or fallback or (self.instance_id or "pack")
        version = self.export_pack_version()
        base = " ".join(
            part for part in (_safe_export_filename(name), version) if part
        )
        return base + suffix

    def _export_running(self) -> bool:
        """True while *this instance* already has an export task (the page's own or another one)."""
        task = self._pack_task
        if task is not None and task.is_running():
            return True
        center = task_center_of(self)
        if center is None or self.instance_id is None:
            return False
        expected = tr("tasks.title.export", self.instance_id)
        for other in center.tasks():
            if other is task or other.kind != "export" or not other.is_running():
                continue
            if other.title() == expected:
                return True
        return False

    def _warn_export_running(self) -> None:
        """PCL refuses a second export of the same instance instead of starting a duplicate."""
        text = tr("instances.detail.export.busy")
        set_app_status(self, text, "warning")
        inwindow_dialog.information(
            self, tr("instances.detail.export.busy.title"), text
        )

    # ---------- 导出: config ----------

    def save_export_config(self) -> None:
        """保存配置: store this form for this instance (name, version, files, embed, folder).

        The category list stays what the old preset had (the categories that carry at least one
        ticked file); the file-level part is written as the *exceptions* inside those categories.
        A preset therefore stays small when only one screenshot was left out, and a preset written
        before this field existed (``excluded_files == []``) still means "every file of the ticked
        categories" - the exact old behaviour.
        """
        from launcher.config import ExportPreset

        if self.instance_id is None:
            return
        plan = self._export_plan
        entries = list(plan.all_entries) if plan is not None else []
        selected = self._export_selected_files()
        categories = sorted(
            {entry.category for entry in entries if entry.relative in selected}
        )
        active = set(categories)
        excluded = sorted(
            entry.relative
            for entry in entries
            if entry.category in active and entry.relative not in selected
        )
        self._store_export_preset(
            ExportPreset(
                name=self.export_pack_name(),
                version=self.export_pack_version(),
                categories=categories,
                excluded_files=excluded,
                embed_files=self.export_embed_check.isChecked(),
                folder=str(self._export_dest) if self._export_dest else "",
            )
        )
        set_app_status(self, tr("instances.detail.export.config.saved"))

    def load_export_config(self) -> None:
        """读取配置: apply the stored form to this page (nothing is exported)."""
        preset = self._export_preset()
        if preset is None:
            set_app_status(
                self, tr("instances.detail.export.config.none"), "warning"
            )
            return
        self.export_name_edit.setText(preset.name)
        self.export_version_edit.setText(preset.version)
        if self._export_plan is None:
            # The tab was not filled yet (the buttons are enabled from the instance, not from the
            # plan): scan once so the saved file selection has something to land on.
            self._fill_export()
        plan = self._export_plan
        if plan is not None:
            categories = set(preset.categories)
            excluded = set(preset.excluded_files)
            self._export_selected = {
                entry.relative
                for entry in plan.all_entries
                if entry.category in categories and entry.relative not in excluded
            }
            self._refresh_export_selection()
        self.export_embed_check.blockSignals(True)
        self.export_embed_check.setChecked(preset.embed_files)
        self.export_embed_check.blockSignals(False)
        if preset.folder:
            folder = Path(preset.folder)
            self._export_dest = folder if folder.is_dir() else None
            self._export_dest_invalid = "" if folder.is_dir() else preset.folder
        self._sync_export_summary()
        self._sync_export_destination()
        set_app_status(self, tr("instances.detail.export.config.loaded"))

    # ---------- 导出: filling ----------

    def _fill_export(self) -> None:
        """Show the pack the current selection would write: category counts and the total.

        The scan is local (counts and sizes only), so this runs on the GUI thread without a network
        round trip or a pause - the same reason the page can afford to refresh it on every format
        change. It scans *all* categories at once so every checkbox can show its size even while it
        is unticked; the summary only counts the ticked files. The previous scan is carried into the
        new one so a format change (which only changes override trees) does not lose the file ticks;
        the first scan starts from the default categories.
        """
        from launcher.modpack import plan_export

        if self.instance_id is None or self.resolved is None:
            self._export_ready = False
            self.export_loading.setText(tr("instances.msg.need_select"))
            self.export_loading.setVisible(True)
            self.export_pack_summary.setText(tr("instances.detail.value.none"))
            self.export_pack_button.setEnabled(False)
            self.export_pack_format.setEnabled(False)
            for widget in (
                self.export_name_edit,
                self.export_version_edit,
                self.export_save_button,
                self.export_load_button,
                self.export_dest_button,
            ):
                widget.setEnabled(False)
            return
        self._export_ready = True
        self.export_loading.setVisible(False)
        previous = self._export_plan
        self._ensure_export_defaults()
        fmt = self._pack_format()
        busy = self._export_running()
        self.export_pack_format.setEnabled(not busy)
        for widget in (
            self.export_name_edit,
            self.export_version_edit,
            self.export_save_button,
            self.export_dest_button,
        ):
            widget.setEnabled(not busy)
        self.export_load_button.setEnabled(not busy and self._export_preset() is not None)
        if self._pack_task is not None and self._pack_task.is_running():
            return
        try:
            plan = plan_export(
                self.instance_id,
                self.game_dir,
                fmt=fmt,
                categories=EXPORT_CATEGORIES,
            )
        except Exception as exc:  # noqa: BLE001 - a broken instance must not break the page
            self._export_plan = None
            self.export_pack_summary.setText(
                tr("instances.detail.export.pack.summary.failed", str(exc))
            )
            self.export_pack_button.setEnabled(False)
            return
        self._export_plan = plan
        self._reconcile_export_selection(previous, plan)
        self._sync_export_categories(plan)
        self._sync_export_advanced(fmt)
        self._sync_export_destination()

    # ---------- 导出: running ----------

    def export_pack(self) -> None:
        """开始导出: write the pack where the remembered folder says, else ask once."""
        if self._require_instance() is None:
            return
        if self._export_running():
            self._warn_export_running()
            return
        if not self._export_selected_files():
            self._sync_export_summary()
            set_app_status(self, tr("instances.detail.export.pack.summary.none"), "warning")
            return
        fmt = self._pack_format()
        dest: Path | None = None
        if self._export_dest is not None and self._export_dest.is_dir():
            dest = self._export_dest / self._suggested_pack_name(fmt)
            if dest.exists():
                accepted = inwindow_dialog.confirm(
                    self,
                    tr("instances.detail.export.overwrite.title"),
                    tr("instances.detail.export.overwrite.body", str(dest)),
                    accept_text=tr("instances.detail.export.overwrite.accept"),
                )
                if accepted is not inwindow_dialog.Outcome.ACCEPTED:
                    return
        else:
            suffix = ".mrpack" if fmt == FORMAT_MODRINTH else ".zip"
            filter_key = (
                "instances.detail.export.pack.filter.modrinth"
                if fmt == FORMAT_MODRINTH
                else "instances.detail.export.pack.filter.curseforge"
            )
            chosen, _selected = QFileDialog.getSaveFileName(
                self,
                tr("instances.detail.export.pack.title"),
                self._suggested_pack_name(fmt),
                tr(filter_key),
            )
            if not chosen:
                return
            dest = Path(chosen)
            if dest.suffix.lower() != suffix:
                dest = dest.with_name(dest.name + suffix)
        self._remember_export_folder(dest.parent)
        self.export_pack_to(dest, fmt=fmt)

    def export_pack_to(
        self,
        dest: Path,
        *,
        fmt: str | None = None,
        name: str | None = None,
        version: str | None = None,
        categories: frozenset[str] | None = None,
        embed_files: bool | None = None,
        files: frozenset[str] | None = None,
    ) -> None:
        """Start one pack export into ``dest``: worker thread, task row and inline progress.

        ``categories``/``files`` exist for callers that want a different selection than the form's;
        the default is the page's own file-level selection. Passing categories only keeps the old
        category-level meaning (every file of those categories), which is what an existing caller of
        this method expects.
        """
        instance_id = self.instance_id
        if instance_id is None or self.resolved is None:
            return
        if self._export_running():
            self._warn_export_running()
            return  # one pack export at a time; the running row owns 取消
        chosen = fmt or self._pack_format()
        plan_entries = (
            self._export_plan.all_entries if self._export_plan is not None else ()
        )
        selected_files = (
            self._export_selected_files() if files is None else frozenset(files)
        )
        if categories is None:
            if files is None:
                selected = self._export_selection()
            else:
                selected = frozenset(
                    entry.category
                    for entry in plan_entries
                    if entry.relative in selected_files
                )
        else:
            selected = frozenset(categories)
            if files is None:
                selected_files = frozenset(
                    entry.relative
                    for entry in plan_entries
                    if entry.category in selected
                )
        embed = self._embed_files(chosen) if embed_files is None else bool(embed_files)
        pack_name = self.export_pack_name() if name is None else name
        pack_version = self.export_pack_version() if version is None else version
        center = task_center_of(self)
        self._pack_fmt = chosen
        self._pack_embed = embed
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
        self.export_open_folder_visible = False
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
                name=pack_name,
                version=pack_version,
                categories=selected,
                files=selected_files,
                embed_files=embed,
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
        self._remember_export_folder(path.parent)
        self.export_pack_open_button.setVisible(True)
        lines = [
            str(path),
            tr(
                "instances.detail.export.pack.report",
                result.files,
                result.overrides,
            ),
        ]
        if getattr(result, "embedded", False):
            lines.append(tr("instances.detail.export.pack.report.embedded"))
        if getattr(result, "missing", 0):
            # Ticked, but gone from disk by the time the export ran: the pack is still valid, and
            # saying how many were skipped is the difference between "it worked" and "where is it?".
            lines.append(
                tr("instances.detail.export.pack.report.skip", int(result.missing))
            )
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
        self,
        key: str,
        *,
        name: str,
        modid: str,
        kind: str,
        loader: str = "",
        path: Path | str | None = None,
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
            # The file itself, when the row knows it: a hash match names the project exactly, where the
            # search below can match the wrong project or none at all after a rename.
            path=str(path) if path else "",
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
            on_error=lambda _message, url=url: self._on_icon_failed(url),
            on_finished=lambda url=url: self._icon_pending.discard(url),
        )

    def _on_icon_failed(self, url: str) -> None:
        """Clear the in-flight mark and try again later; a failed icon must not stay a cube forever."""
        self._icon_pending.discard(url)
        self._icon_retry.failed(url)

    def _on_icon_ready(self, url: str, data: object) -> None:
        """One download arrived: repaint the rows that asked for that URL, and only those rows."""
        try:
            self._icon_pending.discard(url)
            self._icon_retry.succeeded(url)
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
                path=getattr(mod, "path", None),
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
            self._request_identity(
                row.key, name=query, modid="", kind=kind, path=item.path
            )
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
