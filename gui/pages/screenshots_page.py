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

"""截图 page: every screenshot Minecraft wrote, newest first, with the actions a gallery needs.

Two entry points share one implementation. Standalone (``embedded=False``) the page scans every
instance, which is what its own tests still drive. Embedded in the instance detail page it is scoped
by :meth:`ScreenshotsPage.set_instance`: it lists *that* instance's screenshots plus the shared game
folder's, and every row still says which of the two it came from. A shared-mode instance runs in the
game root, so its list is just the shared folder - the scan follows ``launch_dir``, not a guessed
folder name.

The scan of two kinds of folder (see ``launcher.screenshots``) only stats files, so it can run on the
GUI thread; *decoding* the pictures cannot - a 4K screenshot is tens of megabytes of pixels, and a
folder of them would freeze the window for seconds. Every thumbnail therefore goes through
``QImageReader`` on a worker thread, at a size set *before* the decode, so the full image is never
materialised. The rows appear immediately with an empty thumbnail slot and fill in as the batches land.

The four actions are the ones a screenshot folder is actually used for: open it, show its folder,
copy the picture, delete it. Delete asks first and the safe answer is the default one - Enter keeps the
picture (``default=Outcome.REJECTED``), which is what a destructive question must do.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, inwindow_dialog, paint_utils, theme
from gui.widgets import (
    ElidedLabel,
    EmptyState,
    add_page_top_inset,
    set_app_status,
    style_page_layout,
    widget_alive,
)
from gui.workers import run_in_background
from launcher import config, screenshots

tr = i18n.tr
LOGGER = logging.getLogger(__name__)

THUMB_WIDTH = 120
THUMB_HEIGHT = 68
ROW_PADDING = 12
ROW_GAP = 12
ROW_RADIUS = 6
ROW_BUTTON_HEIGHT = 30
#: How many pictures one worker decodes before reporting back. Small enough that the first row fills
#: in quickly and that a huge folder cannot queue hundreds of images at once.
THUMB_BATCH = 12
#: Upper bound on the thumbnail cache; a long session in a big folder cannot grow it forever.
THUMB_CACHE_LIMIT = 256


def _tokens() -> dict[str, str]:
    """The colour table of the theme in force (the light one before the first apply)."""
    return paint_utils._tokens()


def format_taken_at(timestamp: float) -> str:
    """The screenshot's modification time as the list shows it (minute precision is enough)."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OSError, OverflowError, ValueError):
        return ""


def load_thumbnail(path: str, width: int = THUMB_WIDTH, height: int = THUMB_HEIGHT) -> QImage | None:
    """Decode one screenshot at thumbnail size, never at its original size.

    ``QImageReader.setScaledSize`` is what keeps a 3840×2160 PNG from being decompressed into 32 MB of
    QImage only to be thrown away by a ``scaled()`` call: the decoder is asked for the target size in
    the first place. The second ``scaled`` call is the fallback for formats whose handler ignores the
    request - the result must still fit the slot, and the call is cheap on an already-small image.

    Returns None for a file this Qt build cannot read (a corrupt or truncated picture): the row keeps
    its empty slot instead of showing a broken-image icon nobody can act on.
    """
    reader = QImageReader(path)
    reader.setAutoTransform(True)  # phone screenshots carry an EXIF orientation
    target = QSize(max(1, width), max(1, height))
    try:
        source = reader.size()
    except (OSError, ValueError):
        source = QSize()
    if source.isValid() and source.width() > 0 and source.height() > 0:
        scaled = source.scaled(target, Qt.AspectRatioMode.KeepAspectRatio)
        if scaled.width() < source.width() or scaled.height() < source.height():
            reader.setScaledSize(scaled)
    try:
        image = reader.read()
    except (OSError, ValueError):  # a decoder that throws instead of returning a null image
        return None
    if image.isNull():
        return None
    if image.width() > width or image.height() > height:
        image = image.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


def load_thumbnail_batch(items: list[tuple[str, float]]) -> list[tuple[str, float, QImage | None]]:
    """Decode a batch of thumbnails on a worker thread; QImage is safe to hand back (QPixmap is not)."""
    results: list[tuple[str, float, QImage | None]] = []
    for path, stamp in items:
        try:
            results.append((path, stamp, load_thumbnail(path)))
        except Exception:  # one unreadable picture must not drop the whole batch
            LOGGER.warning("screenshot thumbnail failed: %s", path, exc_info=True)
            results.append((path, stamp, None))
    return results


class CardRow(QWidget):
    """One row of a page list: a rounded section card that tints while the pointer is over it.

    Shared by the three new pages (截图 / 服务器 / 收藏夹). They are the same visual object - a thin
    list of cards on the page paper - and a copy per file would be three places to keep in step.
    """

    RADIUS = ROW_RADIUS
    PADDING = ROW_PADDING

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("cardRow")
        self._hover = False

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(tokens.get("BORDER_TABLE", "#dfe4ea")), 1.0))
        painter.setBrush(
            QColor(
                tokens.get(
                    "BG_HOVER" if self._hover else "BG_SECTION",
                    "#f1f3f6",
                )
            )
        )
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)
        painter.end()


class _Thumbnail(QLabel):
    """A fixed-size slot that paints its picture with rounded corners, centred, aspect kept."""

    def __init__(self, width: int = THUMB_WIDTH, height: int = THUMB_HEIGHT) -> None:
        super().__init__()
        self.setFixedSize(width, height)
        self._pixmap: QPixmap | None = None

    def set_thumbnail(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_tokens().get("BG_OPTION", "#f1f3f6")))
        slot = QRectF(self.rect())
        painter.drawRoundedRect(slot, 4, 4)
        if self._pixmap is None or self._pixmap.isNull():
            painter.end()
            return
        clip = QPainterPath()
        clip.addRoundedRect(slot, 4, 4)
        painter.setClipPath(clip)
        pixmap = self._pixmap
        x = (self.width() - pixmap.width() / max(1.0, pixmap.devicePixelRatio())) / 2.0
        y = (self.height() - pixmap.height() / max(1.0, pixmap.devicePixelRatio())) / 2.0
        painter.drawPixmap(int(x), int(y), pixmap)
        painter.end()


class ScreenshotRow(CardRow):
    """One screenshot: its thumbnail, filename, time and folder, then the four actions."""

    HEIGHT = THUMB_HEIGHT + 2 * ROW_PADDING

    def __init__(
        self,
        entry: screenshots.ScreenshotEntry,
        *,
        on_open,
        on_reveal,
        on_copy,
        on_delete,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry = entry
        self.thumbnail = _Thumbnail()
        #: True once a decode has been asked for. A file Qt cannot read must not be requested again on
        #: every batch, or a folder with one broken picture would keep a worker busy forever.
        self.thumbnail_requested = False
        self.name_label = ElidedLabel(entry.filename, elide_mode=Qt.TextElideMode.ElideMiddle)
        name_font = QFont(self.font())
        name_font.setPixelSize(13)
        name_font.setWeight(QFont.Weight.DemiBold)
        self.name_label.setFont(name_font)
        owner = entry.instance_name or tr("screenshots.shared")
        meta = " · ".join(part for part in (format_taken_at(entry.taken_at), owner) if part)
        self.meta_label = ElidedLabel(meta, elide_mode=Qt.TextElideMode.ElideRight)
        self.meta_label.setObjectName("hint")
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        self.meta_label.setFont(meta_font)
        self.setToolTip(str(entry.path) + "\n" + meta)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        text.addWidget(self.name_label)
        text.addWidget(self.meta_label)

        self.open_button = self._action(tr("screenshots.open"), entry.filename, on_open)
        self.reveal_button = self._action(tr("screenshots.reveal"), entry.filename, on_reveal)
        self.copy_button = self._action(tr("screenshots.copy"), entry.filename, on_copy)
        self.delete_button = self._action(
            tr("instances.delete"), entry.filename, on_delete, object_name="rowDanger"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(ROW_PADDING, ROW_PADDING, ROW_PADDING, ROW_PADDING)
        layout.setSpacing(ROW_GAP)
        layout.addWidget(self.thumbnail)
        layout.addLayout(text, 1)
        for button in (self.open_button, self.reveal_button, self.copy_button, self.delete_button):
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(self.HEIGHT)

    @staticmethod
    def _action(text: str, subject: str, callback, *, object_name: str = "rowInstall") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setFixedHeight(ROW_BUTTON_HEIGHT)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Four buttons reading the same word per row would be useless with a screen reader; the file
        # name is what tells them apart.
        name = f"{text} · {subject}"
        button.setAccessibleName(name)
        button.setToolTip(name)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self.thumbnail.set_thumbnail(pixmap)

    @property
    def has_thumbnail(self) -> bool:
        return self.thumbnail._pixmap is not None


class ScreenshotsPage(QWidget):
    """The gallery: a scan of the shared folder (plus one instance, when scoped), newest first."""

    def __init__(self, *, embedded: bool = False) -> None:
        super().__init__()
        self.setObjectName("screenshotsPage")
        self.setAccessibleName(tr("nav.screenshots"))
        self._entries: list[screenshots.ScreenshotEntry] = []
        self._rows: list[ScreenshotRow] = []
        #: path -> (mtime, pixmap); the mtime is part of the key so an overwritten file is re-read.
        self._thumb_cache: dict[str, tuple[float, QPixmap]] = {}
        #: Bumped by every rebuild; a batch that lands for an older generation is dropped.
        self._generation = 0
        self._shots_dir = Path()
        #: True inside the instance detail page. There the page starts with no scope and must not
        #: scan the shared folder until the detail tells it which instance it is showing.
        self._embedded = embedded
        #: (instance id, display name, launch dir, game dir) once set_instance has run.
        self._scope: tuple[str, str, Path, Path] | None = None

        self.count_label = QLabel("")
        self.count_label.setObjectName("hint")
        self.scope_label = QLabel(tr("instances.detail.screenshots.scope"))
        self.scope_label.setObjectName("hint")
        self.scope_label.setVisible(False)
        self.refresh_button = QPushButton(tr("java.refresh"))
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setFixedHeight(32)
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_folder_button = QPushButton(tr("crash.open_folder"))
        self.open_folder_button.setObjectName("secondaryButton")
        self.open_folder_button.setFixedHeight(32)
        self.open_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        header_layout.addWidget(self.count_label)
        header_layout.addWidget(self.scope_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.refresh_button)
        header_layout.addWidget(self.open_folder_button)

        self.empty = EmptyState("")
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("screenshotsScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.rows_host)
        self.scroll.setAccessibleName(tr("a11y.screenshots"))

        self.stack = QStackedWidget()
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(self.empty)

        layout = QVBoxLayout(self)
        style_page_layout(layout)
        add_page_top_inset(layout)
        if embedded:
            # Inside the instance detail the section owns the content area edge to edge, like the
            # mods/saves pages beside it; a page-level inset here would shift the rows 14px right.
            layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(header)
        layout.addWidget(self.stack, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.open_folder_button.clicked.connect(self.open_folder)
        theme.register_self_styling(self)
        if not embedded:
            self.refresh()

    # ---------- instance scope ----------

    def set_instance(
        self,
        instance_id: str,
        instance_name: str,
        launch_dir: Path | str,
        game_dir: Path | str,
    ) -> None:
        """Scope the gallery to one instance plus the shared folder.

        ``launch_dir`` is the folder the game actually runs in (``ResolvedInstance.launch_dir``):
        the instance folder when isolated, the game root when shared. Reading the effective folder
        rather than ``versions/<id>`` means the list follows the mode instead of assuming one.
        """
        self._scope = (instance_id, instance_name, Path(launch_dir), Path(game_dir))
        self.scope_label.setVisible(True)
        # Refresh even while the tab is hidden: the detail page names its instance before the gallery
        # is ever shown, and a scan gated on visibility left ``entries()`` empty until some later show.
        # The scan is a handful of is_dir() calls, so doing it once more on show costs nothing.
        self.refresh()

    def clear_instance(self) -> None:
        """Drop the scope and the rows (the detail page is being cleared)."""
        self._scope = None
        self._entries = []
        self._rebuild()

    def has_scope(self) -> bool:
        return self._scope is not None

    def on_theme_applied(self) -> None:
        for row in self._rows:
            row.update()

    # ---------- data ----------

    def entries(self) -> list[screenshots.ScreenshotEntry]:
        """The scanned pictures, in the order the page shows them."""
        return list(self._entries)

    def rows(self) -> list[ScreenshotRow]:
        """The row widgets, in the same order (the tests drive these)."""
        return list(self._rows)

    def refresh(self) -> None:
        """Rescan the configured locations and rebuild the list; thumbnails come from the cache."""
        if self._embedded and self._scope is None:
            return  # the detail has not named an instance yet; never scan the whole game folder here
        game_dir, instance = self._scan_root()
        self._shots_dir = (
            screenshots.screenshots_dir(instance[2]) if instance is not None
            else screenshots.screenshots_dir(game_dir)
        )
        if instance is not None:
            self._entries = screenshots.scan_instance_screenshots(
                game_dir, instance[0], instance[1], instance[2]
            )
        else:
            self._entries = screenshots.scan_screenshots(game_dir, self._instance_names(game_dir))
        self._rebuild()
        self._apply_thumbnails()

    def _scan_root(self) -> tuple[Path, tuple[str, str, Path, Path] | None]:
        """The game folder to scan and - when scoped - the instance tuple that narrows it."""
        if self._scope is not None:
            return self._scope[3], self._scope
        return self._game_dir(), None

    @staticmethod
    def _game_dir() -> Path:
        cfg, _ = config.load()
        return config.active_game_dir(cfg)

    @staticmethod
    def _instance_names(game_dir: Path) -> dict[str, str]:
        """id -> display name, for the "which instance" column. A missing registry is not an error."""
        from launcher import instances

        try:
            return {inst.id: inst.name for inst in instances.list_instances(game_dir).values()}
        except OSError:
            return {}

    def _rebuild(self) -> None:
        self._generation += 1
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
        for index, entry in enumerate(self._entries):
            row = ScreenshotRow(
                entry,
                on_open=lambda row_index=index: self._act(row_index, self.open_entry),
                on_reveal=lambda row_index=index: self._act(row_index, self.reveal_entry),
                on_copy=lambda row_index=index: self._act(row_index, self.copy_entry),
                on_delete=lambda row_index=index: self._act(row_index, self.delete_entry),
            )
            cached = self._thumb_cache.get(str(entry.path))
            if cached is not None and cached[0] == entry.taken_at:
                row.set_thumbnail(cached[1])
            self.rows_layout.addWidget(row)
            self._rows.append(row)
        self.rows_layout.addStretch(1)
        self.count_label.setText(tr("screenshots.count", len(self._entries)))
        self.empty.setText(
            tr("screenshots.empty") + "\n" + tr("screenshots.empty_hint", str(self._shots_dir))
        )
        self.stack.setCurrentIndex(0 if self._entries else 1)

    # ---------- thumbnails (background) ----------

    def _apply_thumbnails(self) -> None:
        """Hand the first batch of missing thumbnails to a worker; repeat until none is missing."""
        pending = [
            row
            for row in self._rows
            if not row.has_thumbnail and not row.thumbnail_requested
        ]
        if not pending:
            return
        generation = self._generation
        batch = pending[:THUMB_BATCH]
        for row in batch:
            row.thumbnail_requested = True
        items = [(str(row.entry.path), row.entry.taken_at) for row in batch]

        def apply(results, token: int = generation) -> None:
            if not widget_alive(self) or token != self._generation:
                return
            for path, stamp, image in results:
                if image is None:
                    continue
                pixmap = QPixmap.fromImage(image)
                if len(self._thumb_cache) >= THUMB_CACHE_LIMIT:
                    self._thumb_cache.clear()
                self._thumb_cache[path] = (stamp, pixmap)
                for row in self._rows:
                    if str(row.entry.path) == path and row.entry.taken_at == stamp:
                        row.set_thumbnail(pixmap)
            self._apply_thumbnails()

        def failed(_message: str, token: int = generation) -> None:
            if widget_alive(self) and token == self._generation:
                LOGGER.warning("screenshot thumbnails: %s", _message)

        run_in_background(load_thumbnail_batch, items, on_result=apply, on_error=failed)

    # ---------- actions ----------

    def _act(self, index: int, action) -> bool:
        """Run ``action`` for the entry at ``index``; a list rebuilt since the click has no such row."""
        if not 0 <= index < len(self._entries):
            return False
        return bool(action(self._entries[index]))

    def open_entry(self, entry: screenshots.ScreenshotEntry) -> bool:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry.path))):
            set_app_status(self, tr("screenshots.open_failed", entry.filename), "error")
            return False
        return True

    def reveal_entry(self, entry: screenshots.ScreenshotEntry) -> bool:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry.path.parent))):
            set_app_status(self, tr("screenshots.open_failed", str(entry.path.parent)), "error")
            return False
        return True

    def open_folder(self) -> bool:
        """Open the shared screenshots folder (it is created on demand, like the game does)."""
        try:
            self._shots_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            set_app_status(self, tr("screenshots.open_failed", str(exc)), "error")
            return False
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._shots_dir))):
            set_app_status(self, tr("screenshots.open_failed", str(self._shots_dir)), "error")
            return False
        return True

    def copy_entry(self, entry: screenshots.ScreenshotEntry) -> bool:
        """Put the picture itself on the clipboard (what "copy" means for an image)."""
        image = QImage(str(entry.path))
        if image.isNull():
            set_app_status(self, tr("screenshots.copy_failed", entry.filename), "error")
            return False
        QApplication.clipboard().setImage(image)
        set_app_status(self, tr("screenshots.copied", entry.filename))
        return True

    def delete_entry(self, entry: screenshots.ScreenshotEntry) -> bool:
        """Ask, then delete the file. The question's default answer is "keep" (Enter cancels)."""
        outcome = inwindow_dialog.confirm(
            self,
            tr("screenshots.delete.title"),
            tr("screenshots.delete.message", str(entry.path)),
            accept_text=tr("instances.delete"),
            reject_text=tr("dialog.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="question",
        )
        if outcome is not inwindow_dialog.Outcome.ACCEPTED:
            return False
        try:
            entry.path.unlink()
        except OSError as exc:
            set_app_status(self, tr("screenshots.delete_failed", str(exc)), "error")
            return False
        set_app_status(self, tr("screenshots.deleted", entry.filename))
        self.refresh()
        return True

    # ---------- lifecycle ----------

    def showEvent(self, event) -> None:
        """Every visit re-reads the folder: the game may have written a screenshot meanwhile."""
        super().showEvent(event)
        if not self._embedded or self._scope is not None:
            self.refresh()
