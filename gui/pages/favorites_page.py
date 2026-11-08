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

"""收藏夹 page: the projects the user starred, with the two actions the list can offer locally.

A favorite stores a pointer (kind + project id + name + icon URL + when it was added), never a copy of
the project. That is why this page can show the list without a network request: if the icon is
already in the launcher's image cache the row paints it, otherwise it paints the project's letter
tile, and the name and category still tell the user what they starred.

Opening the project's detail on the resources page is deliberately *not* done here. The resources page
exposes no "open this project" entry, and this change was allowed to add the star to that page and
nothing else; a jump built out of its private widgets would break the first time its search flow is
touched. Copying the name is the honest fallback: the user can paste it into the search field.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPixmap
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

from gui import i18n, paint_utils, theme
from gui.pages.screenshots_page import CardRow
from gui.widgets import (
    ElidedLabel,
    EmptyState,
    add_page_top_inset,
    set_app_status,
    style_page_layout,
)
from launcher import favorites

tr = i18n.tr
LOGGER = logging.getLogger(__name__)

ICON_SIZE = 48
ROW_PADDING = 12
ROW_GAP = 12
ROW_HEIGHT = 68
ROW_BUTTON_HEIGHT = 30
#: The category label reuses the resources page's own tab names: one word per category, translated.
KIND_KEYS = {
    "mod": "mods.tab.mods",
    "modpack": "mods.tab.modpacks",
    "resourcepack": "mods.tab.resourcepacks",
    "shaderpack": "mods.tab.shaderpacks",
}


def kind_label(kind: str) -> str:
    """The translated category of a favorite; an unknown kind is shown as it was stored."""
    key = KIND_KEYS.get(kind)
    return tr(key) if key else kind


def format_added_at(timestamp: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OSError, OverflowError, ValueError):
        return ""


def favorite_icon(name: str, icon_url: str) -> QPixmap:
    """The project icon if it is already cached, else a letter tile - never a network request."""
    if icon_url:
        try:
            from launcher.net.http_cache import cached_icon_bytes

            stored = cached_icon_bytes(icon_url)
        except (OSError, ValueError):
            stored = None
        if stored:
            pixmap = QPixmap()
            if pixmap.loadFromData(stored):
                return pixmap.scaled(
                    ICON_SIZE,
                    ICON_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
    return paint_utils.letter_tile_pixmap((name or "?").strip()[:1] or "?", ICON_SIZE, 8)


class _ProjectIcon(QWidget):
    """A rounded 48px project tile: the cached icon when there is one, else the letter tile."""

    def __init__(self, name: str, icon_url: str, size: int = ICON_SIZE) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self._size = size
        self._pixmap = favorite_icon(name, icon_url)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 8, 8)
        painter.setClipPath(clip)
        ratio = max(1.0, self._pixmap.devicePixelRatio())
        width = self._pixmap.width() / ratio
        height = self._pixmap.height() / ratio
        painter.drawPixmap(
            int((self._size - width) / 2.0), int((self._size - height) / 2.0), self._pixmap
        )
        painter.end()


class FavoriteRow(CardRow):
    """One favorite: icon, name, category + date, and the two local actions."""

    HEIGHT = ROW_HEIGHT

    def __init__(
        self,
        favorite: favorites.Favorite,
        *,
        on_copy,
        on_remove,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.favorite = favorite
        self.icon = _ProjectIcon(favorite.name, favorite.icon_url)
        self.name_label = ElidedLabel(favorite.name, elide_mode=Qt.TextElideMode.ElideRight)
        name_font = QFont(self.font())
        name_font.setPixelSize(13)
        name_font.setWeight(QFont.Weight.DemiBold)
        self.name_label.setFont(name_font)
        meta = " · ".join(
            part for part in (kind_label(favorite.kind), format_added_at(favorite.added_at)) if part
        )
        self.meta_label = ElidedLabel(meta, elide_mode=Qt.TextElideMode.ElideRight)
        self.meta_label.setObjectName("hint")
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        self.meta_label.setFont(meta_font)
        self.setToolTip(f"{favorite.name}\n{meta}\n{favorite.project_id}")

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        text.addWidget(self.name_label)
        text.addWidget(self.meta_label)

        self.copy_button = self._action(tr("favorites.copy_name"), favorite.name, on_copy)
        self.remove_button = self._action(
            tr("favorites.remove"), favorite.name, on_remove, object_name="rowDanger"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(ROW_PADDING, ROW_PADDING, ROW_PADDING, ROW_PADDING)
        layout.setSpacing(ROW_GAP)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text, 1)
        layout.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignVCenter)
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


class FavoritesPage(QWidget):
    """The starred projects, most recently added first."""

    def __init__(self, store: favorites.FavoriteStore | None = None) -> None:
        super().__init__()
        self.setObjectName("favoritesPage")
        self.setAccessibleName(tr("nav.favorites"))
        self._store = store if store is not None else favorites.store()
        self._items: list[favorites.Favorite] = []
        self._rows: list[FavoriteRow] = []

        self.count_label = QLabel("")
        self.count_label.setObjectName("hint")
        self.refresh_button = QPushButton(tr("java.refresh"))
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setFixedHeight(32)
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        header_layout.addWidget(self.count_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.refresh_button)

        self.empty = EmptyState(tr("favorites.empty") + "\n" + tr("favorites.empty_hint"))
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("favoritesScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.rows_host)
        self.scroll.setAccessibleName(tr("a11y.favorites"))
        self.stack = QStackedWidget()
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(self.empty)

        layout = QVBoxLayout(self)
        style_page_layout(layout)
        add_page_top_inset(layout)
        layout.addWidget(header)
        layout.addWidget(self.stack, 1)

        # The empty state has no rows, and a page with no focus stop at all cannot be reached with
        # the keyboard: 刷新 is the one control that always exists here.
        self.refresh_button.clicked.connect(self._on_refresh_clicked)
        theme.register_self_styling(self)
        self.refresh()

    def _on_refresh_clicked(self) -> None:
        self.refresh(force=True)

    def on_theme_applied(self) -> None:
        self.refresh()

    def store(self) -> favorites.FavoriteStore:
        """The store this page reads (the tests use it to add a favorite behind the page's back)."""
        return self._store

    def items(self) -> list[favorites.Favorite]:
        return list(self._items)

    def rows(self) -> list[FavoriteRow]:
        return list(self._rows)

    def refresh(self, *, force: bool = True) -> None:
        """Re-read the store and rebuild the list (the file is tiny; every visit re-reads it)."""
        self._items = sorted(self._store.load(force=force), key=lambda item: item.added_at, reverse=True)
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
        for index, favorite in enumerate(self._items):
            row = FavoriteRow(
                favorite,
                on_copy=lambda row_index=index: self.copy_name(row_index),
                on_remove=lambda row_index=index: self.remove(row_index),
            )
            self.rows_layout.addWidget(row)
            self._rows.append(row)
        self.rows_layout.addStretch(1)
        self.count_label.setText(tr("favorites.count", len(self._items)))
        self.stack.setCurrentIndex(0 if self._items else 1)

    def copy_name(self, index: int) -> bool:
        if not 0 <= index < len(self._items):
            return False
        name = self._items[index].name
        QApplication.clipboard().setText(name)
        set_app_status(self, tr("favorites.name_copied", name))
        return True

    def remove(self, index: int) -> bool:
        """Unstar one project; the store writes the file, the list follows."""
        if not 0 <= index < len(self._items):
            return False
        favorite = self._items[index]
        try:
            removed = self._store.remove(favorite.kind, favorite.project_id)
        except favorites.FavoritesError as exc:
            set_app_status(self, tr("favorites.save_failed", str(exc)), "error")
            return False
        if not removed:
            return False
        set_app_status(self, tr("favorites.removed", favorite.name))
        self.refresh()
        return True

    def showEvent(self, event) -> None:
        """A star can be added on the resources page while this page is hidden."""
        super().showEvent(event)
        self.refresh(force=True)
