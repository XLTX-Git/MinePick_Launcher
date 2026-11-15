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

"""The versions page's install step: pick a loader, name the instance, then download.

PCL's download page (``PageDownloadInstall``) is a flow inside one page rather than a chain of
dialogs, and this is that flow's second step: the version list is *replaced* (not covered) by a
one-row header carrying the editable instance name, one row per mod loader this launcher can
actually install - with the loader's builds expanding inside that row - and a single pill button
that reads "download vanilla" until a loader is chosen. Nothing here opens a window: progress
continues in the version list itself (one row's install button turns into a compact progress block)
and a failure is reported where every other install failure is.

Why the loader list is derived instead of written down: a row may only offer to add a loader when the
install path can really run that loader's installer, so the rows come from
``launcher.mods.loaders.LOADERS`` - the tuple ``list_loader_versions`` and ``installer_args`` accept.
Quilt has a display name and a mod-filter id in this launcher but no loader source and no installer
arguments (``list_loader_versions("quilt", ...)`` raises), and OptiFine has neither; both therefore
get no row at all instead of a row that could never do anything. Adding Quilt to ``LOADERS`` is what
makes its row appear here, with no change in this file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import block_icons, i18n, paint_utils
from launcher.install import install_version
from launcher.instances import MAX_INSTANCE_NAME_LENGTH, unique_instance_name
from launcher.mods.installer import install_loader
from launcher.mods.loaders import LOADERS, display_loader_name, list_loader_versions
from launcher.mods.models import LoaderVersion
from launcher.net.downloader import DownloadCancelled, DownloadProgress, DownloadResult

tr = i18n.tr

#: The loaders that get a row, in the order the page shows them. Derived from the tuple the install
#: path accepts (see the module docstring), never from the display-name table: a name is not a
#: capability.
LOADER_ROWS: tuple[str, ...] = tuple(
    loader for loader in ("forge", "neoforge", "fabric", "quilt") if loader in LOADERS
)

# The three states a loader row can end in. ``CHECKING`` is the state before the answer arrives.
CAN_ADD = "can_add"
NO_VERSION = "no_version"
FETCH_FAILED = "fetch_failed"
CHECKING = "checking"

#: The state text, by state. "No version available" is only ever shown when the source answered
#: with an empty list; an answer that never arrived is FETCH_FAILED and says so.
STATUS_KEYS = {
    CAN_ADD: "versions.install.loader.can_add",
    NO_VERSION: "versions.install.loader.no_version",
    FETCH_FAILED: "versions.install.loader.failed",
    CHECKING: "versions.install.loader.checking",
}

#: Characters an instance name may not carry. The name is never a folder (the folder stays
#: ``versions/<version id>``), but a name that Windows cannot put on disk is a trap: a later export or
#: a folder-shaped feature would fail on it, so the field refuses it now.
INVALID_NAME_CHARS = '/\\:*?"<>|'


def invalid_name_chars(name: str) -> list[str]:
    """The refused characters in ``name``, in the order they appear and without repeats."""
    found: list[str] = []
    for char in name:
        if char in INVALID_NAME_CHARS and char not in found:
            found.append(char)
    return found


def suggest_name(name: str, taken: set[str] | frozenset[str]) -> str:
    """``name-2``, ``name-3`` ...: the first free alternative for a name that is already taken."""
    return unique_instance_name(name, taken, fallback=name)


def _tokens() -> dict[str, str]:
    """The colour table of the theme in force (the same one the QSS placeholders resolve from)."""
    return paint_utils._tokens()


def probe_loader(loader: str, version_id: str) -> list:
    """Which versions ``loader`` has for ``version_id`` - the step's one network seam.

    The page runs this in the background once per row. An empty answer is a real answer ("this loader
    has nothing for that game version") and is shown as such; an exception is the other state
    ("could not fetch"), which is the only one with a retry. Folding the two together would make the
    page claim a loader has no version when all it knows is that the request failed.
    """
    return list_loader_versions(loader, version_id)


class InstallFailed(Exception):
    """An install that reached the downloader but did not put a usable profile on disk."""


@dataclass(frozen=True)
class InstallOutcome:
    """What an install left behind: the profile id every launch path resolves, and the download summary."""

    profile_id: str
    result: DownloadResult | None = None


def install_selection(
    version_id: str,
    *,
    game_dir: Path,
    loader_version: LoaderVersion | None = None,
    install_base: bool = True,
    cache_dir: Path | None = None,
    runtime_dir: Path | None = None,
    concurrency: int = 4,
    auto_install_java: bool = True,
    progress: Callable[[DownloadProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> InstallOutcome:
    """Run the versions page's install body: core first, then (optionally) the loader.

    The page's ``install_version_id`` grew this sequence inside a frozen method, and the instance page
    needs exactly the same sequence when 修改 downloads a version the folder does not have yet. Rather
    than writing a second downloader, this function performs the same two library calls in the same
    order: ``install_version`` writes ``versions/<id>/`` (JSON, jar, libraries, assets) and
    ``install_loader`` runs the loader's own installer, which creates the inherited profile. The
    returned ``profile_id`` is what the caller points ``base_version`` at - the vanilla id, or the
    loader profile id the installer reported.

    ``install_base=False`` is for a loader switch whose game version is already installed: the loader
    installer inherits from it, and re-downloading its files would only add network work. A download
    with failed files is not a usable core, so it raises :class:`InstallFailed`; a cancellation
    (``should_stop``, or the downloader's own cooperative flag) raises ``DownloadCancelled`` and leaves
    the pointer to the caller, which must not write it.
    """
    result: DownloadResult | None = None
    if install_base:
        result = install_version(
            version_id,
            game_dir=game_dir,
            cache_dir=cache_dir,
            concurrency=concurrency,
            progress=progress,
            auto_install_java=auto_install_java,
            runtime_dir=runtime_dir,
        )
        failed = list(getattr(result, "failed", []) or [])
        if failed:
            # "Installed, but N files are missing" is not a core this launcher can promise to start.
            raise InstallFailed(tr("versions.msg.partial", len(failed)))
        if should_stop is not None and should_stop():
            raise DownloadCancelled("cancelled")
    profile_id = version_id
    if loader_version is not None:
        profile_id = install_loader(
            loader_version, game_dir, cache_dir=cache_dir, progress=progress
        )
        if should_stop is not None and should_stop():
            # The installer is a synchronous subprocess that cannot be interrupted; leaving the new
            # profile on disk is harmless, but the caller must not point the instance at a switch the
            # user cancelled halfway through.
            raise DownloadCancelled("cancelled")
    return InstallOutcome(profile_id=profile_id, result=result)


def loader_install_options(game_version: str) -> tuple[list[tuple[str, LoaderVersion]], list[str]]:
    """The newest installable version of each loader, plus the loaders whose source failed.

    One query per loader (the same ``list_loader_versions`` the versions page's install step probes)
    and only the newest answer: the page's own install body has always installed ``versions[0]``, so a
    row that offered another version would promise a choice the installer does not implement. A
    failure is returned instead of raised so one unreachable loader source cannot hide the others.
    """
    options: list[tuple[str, LoaderVersion]] = []
    failures: list[str] = []
    for loader in LOADER_ROWS:
        try:
            versions = list_loader_versions(loader, game_version)
        except Exception as exc:  # noqa: BLE001 - reported per loader, never fatal to the list
            failures.append(str(exc) or type(exc).__name__)
            continue
        if versions:
            options.append((loader, versions[0]))
    return options, failures


class LoaderVersionItem(QAbstractButton):
    """One build inside an expanded loader row: a marker, the version, and its publication line.

    The row is a real button (Tab stop, Space/Enter) because choosing a build is the whole point of
    the expansion. It is painted instead of composed for the same reason the loader rows are: the
    list can hold dozens of builds and must not build a widget tree per entry.

    Two shapes share the class. The newest build is the block at the top of the expansion: two lines,
    "66.1.8" over "最新版本 | 发布于 2026/11/12 21:29". Every other build is one 32px line with the
    date pushed to the right edge. A source without dates simply omits the date half; this class
    never invents one.
    """

    HEIGHT = 32
    LATEST_HEIGHT = 46
    RADIUS = 6
    MARKER = 13
    PADDING = 12

    def __init__(
        self,
        version: LoaderVersion,
        *,
        latest: bool = False,
        meta: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.version = version
        self.latest = latest
        self.meta = meta
        self._selected = False
        self._hover = False
        self.setObjectName("loaderVersionItem")
        self.setFixedHeight(self.LATEST_HEIGHT if latest else self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        parts = [version.version, meta]
        self.setAccessibleName(" · ".join(part for part in parts if part))
        self.setToolTip(self.accessibleName())

    # ---------- state ----------

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self.setAccessibleDescription(
            tr("versions.install.loader.selected") if selected else ""
        )
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(320, self.height())

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
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        if self._selected:
            painter.setPen(QPen(QColor(tokens.get("ACCENT", "#2f9e6f")), 1.0))
            painter.setBrush(QColor(tokens.get("ACCENT_TINT", "#eef7f2")))
            painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)
        elif self._hover and self.isEnabled():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("BG_HOVER", "#eef2f7")))
            painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(tokens.get("FOCUS_RING", "#3b82f6")), 1.0))
            painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        # The marker is the selection itself: an empty outlined square that fills with the accent and
        # a check once this build is the one the row carries.
        marker_x = self.PADDING
        marker_y = (self.height() - self.MARKER) / 2.0
        marker = QRectF(marker_x, marker_y, self.MARKER, self.MARKER)
        painter.setBrush(
            QColor(tokens.get("ACCENT", "#2f9e6f"))
            if self._selected
            else QColor(tokens.get("BG_LIST", "#ffffff"))
        )
        painter.setPen(
            QPen(
                QColor(
                    tokens.get("ACCENT", "#2f9e6f")
                    if self._selected
                    else tokens.get("FG_HINT", "#4f6070")
                ),
                1.2,
            )
        )
        painter.drawRoundedRect(marker, 3.0, 3.0)
        if self._selected:
            pen = QPen(QColor(tokens.get("FG_SELECTED", "#ffffff")))
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(marker_x + 3.0, marker_y + 6.6),
                        QPointF(marker_x + 5.4, marker_y + 9.0),
                        QPointF(marker_x + 10.0, marker_y + 4.0),
                    ]
                )
            )

        text_left = marker_x + self.MARKER + 11
        text_width = max(0.0, self.width() - text_left - self.PADDING)
        title_font = QFont(self.font())
        title_font.setPixelSize(14 if self.latest else 13)
        title_font.setWeight(
            QFont.Weight.DemiBold if self.latest else QFont.Weight.Normal
        )
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        title_metrics = QFontMetrics(title_font)
        meta_metrics = QFontMetrics(meta_font)
        title_color = QColor(
            tokens.get("FG_TEXT", "#1f2a36")
            if self.isEnabled()
            else tokens.get("FG_DISABLED", "#8f949a")
        )
        meta_color = QColor(tokens.get("FG_HINT", "#4f6070"))

        if self.latest:
            painter.setFont(title_font)
            painter.setPen(title_color)
            painter.drawText(
                QRectF(text_left, 5.0, text_width, 20.0),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                title_metrics.elidedText(
                    self.version.version, Qt.TextElideMode.ElideRight, int(text_width)
                ),
            )
            painter.setFont(meta_font)
            painter.setPen(meta_color)
            painter.drawText(
                QRectF(text_left, 23.0, text_width, 18.0),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                meta_metrics.elidedText(
                    self.meta, Qt.TextElideMode.ElideRight, int(text_width)
                ),
            )
        else:
            meta_width = min(
                meta_metrics.horizontalAdvance(self.meta),
                max(0.0, text_width * 0.55),
            ) if self.meta else 0.0
            version_width = max(0.0, text_width - meta_width - (8.0 if meta_width else 0.0))
            painter.setFont(title_font)
            painter.setPen(title_color)
            painter.drawText(
                QRectF(text_left, 0.0, version_width, self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                title_metrics.elidedText(
                    self.version.version, Qt.TextElideMode.ElideRight, int(version_width)
                ),
            )
            if meta_width:
                painter.setFont(meta_font)
                painter.setPen(meta_color)
                painter.drawText(
                    QRectF(
                        text_left + version_width + 8.0,
                        0.0,
                        meta_width,
                        self.height(),
                    ),
                    int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                    meta_metrics.elidedText(
                        self.meta, Qt.TextElideMode.ElideRight, int(meta_width)
                    ),
                )
        painter.end()


class LoaderRow(QAbstractButton):
    """One loader choice's header: a 56px card with the loader name (15/600), its value and a chevron.

    The header only opens and closes the build list that :class:`LoaderChoice` renders under it. The
    value is the chosen build's version once there is one, else the row's state ("可以添加", "查询中…");
    the chevron points up while the list is open.

    Painted rather than composed, for the reason the version rows are: a list of choices must not pay
    for three child widgets each, and the state text has to elide on one line (a QLabel either wraps
    or reports a width wider than it paints, which is what pushes a page's minimum width up).

    The row is a real ``QAbstractButton``, so it is a Tab stop, Space/Enter activate it, and assistive
    technology reports it as a button with the loader's name and state. It is only checkable while the
    loader can actually be added: a row whose state is "no version available" cannot be selected, and
    a row that says "could not fetch" activates a retry instead.
    """

    HEIGHT = 56
    PADDING = 16
    RADIUS = 8
    CHEVRON = 12

    def __init__(self, loader: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.loader = loader
        self.setObjectName("loaderChoiceRow")
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._state = CHECKING
        self._detail = ""
        self._versions: list[LoaderVersion] = []
        self._selected_version = ""
        self._expanded = False
        self._hover = False
        self.set_status(CHECKING)

    # ---------- data ----------

    def state(self) -> str:
        return self._state

    def detail(self) -> str:
        """The error text behind a FETCH_FAILED row (empty for every other state)."""
        return self._detail

    def versions(self) -> list[LoaderVersion]:
        """Every build of this loader the source listed for the pending game version."""
        return list(self._versions)

    def status_text(self) -> str:
        if self._selected_version:
            return self._selected_version
        return tr(STATUS_KEYS[self._state])

    def set_selected_version(self, version: str) -> None:
        """Show the picked build on the closed row instead of the "available" state text."""
        self._selected_version = str(version or "")
        name = display_loader_name(self.loader)
        text = self.status_text()
        self.setAccessibleName(f"{name} · {text}")
        self.update()

    def selected_version(self) -> str:
        return self._selected_version

    def set_expanded(self, expanded: bool) -> None:
        """The chevron turns up while the builds are open under the row."""
        self._expanded = expanded
        self.update()

    def is_expanded(self) -> bool:
        return self._expanded

    def set_status(
        self,
        state: str,
        detail: str = "",
        *,
        versions: list[LoaderVersion] | None = None,
    ) -> None:
        """Set the state (one of the four module constants) and make the row behave like it."""
        self._state = state
        self._detail = detail
        if versions is not None:
            self._versions = list(versions)
        if state != CAN_ADD:
            self._versions = []
            self._selected_version = ""
        self.setCheckable(state == CAN_ADD)
        self.setEnabled(state in (CAN_ADD, FETCH_FAILED))
        if state != CAN_ADD and self.isChecked():
            self.setChecked(False)
        name = display_loader_name(self.loader)
        self.setAccessibleName(f"{name} · {self.status_text()}")
        self.setToolTip(detail if state == FETCH_FAILED and detail else "")
        self.update()

    # ---------- geometry ----------

    def sizeHint(self) -> QSize:
        return QSize(360, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        """A narrow floor: the status text elides instead of widening the page's minimum width."""
        return QSize(240, self.HEIGHT)

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
        # The focus ring is painted from the keyboardFocus property (QSS cannot paint inside a custom
        # paintEvent); a repaint is what makes it appear, and it must happen for both focus paths.
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
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.HEIGHT - 1.0)

        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        if self.isChecked():
            border = QColor(tokens.get("ACCENT", "#2f9e6f"))
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
        elif self._hover and self.isEnabled():
            border = QColor(tokens.get("BORDER_HOVER", border.name()))
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            border = QColor(tokens.get("FOCUS_RING", border.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        text_color = tokens.get("FG_TEXT", "#1f2a36")
        if not self.isEnabled():
            text_color = tokens.get("FG_DISABLED", text_color)
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_metrics = QFontMetrics(name_font)
        status_font = QFont(self.font())
        status_font.setPixelSize(12)
        status_metrics = QFontMetrics(status_font)

        status_color = {
            CAN_ADD: tokens.get("ACCENT_TEXT", tokens.get("FG_HINT", "#4f6070")),
            FETCH_FAILED: tokens.get("FG_STATUS_WARNING", tokens.get("FG_HINT", "#4f6070")),
        }.get(self._state, tokens.get("FG_HINT", "#4f6070"))

        chevron_left = self.width() - self.PADDING - self.CHEVRON
        status_width = min(
            status_metrics.horizontalAdvance(self.status_text()) + 2,
            max(0, chevron_left - self.PADDING - 120),
        )
        status_left = chevron_left - 12 - status_width
        name_left = self.PADDING
        name_width = max(0, status_left - 12 - name_left)

        painter.setFont(name_font)
        painter.setPen(QColor(text_color))
        painter.drawText(
            QRectF(name_left, 0, name_width, self.HEIGHT),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name_metrics.elidedText(
                display_loader_name(self.loader), Qt.TextElideMode.ElideRight, name_width
            ),
        )
        painter.setFont(status_font)
        painter.setPen(QColor(status_color))
        painter.drawText(
            QRectF(status_left, 0, status_width, self.HEIGHT),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            status_metrics.elidedText(
                self.status_text(), Qt.TextElideMode.ElideRight, int(status_width)
            ),
        )

        # A drawn chevron: the UI font has no guaranteed U+203A, and a missing glyph would be a tofu
        # box in the middle of the row. It points up while this row's builds are open below it.
        pen = QPen(QColor(tokens.get("FG_HINT", "#4f6070")))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        middle = self.HEIGHT / 2.0
        inset = self.CHEVRON * 0.28
        # Symmetric about the row's centre line. The up chevron used to reach 1.6*inset down but only
        # 1*inset up, so its ink sat below centre and the whole glyph read as riding high in the row.
        if self._expanded:
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(chevron_left + inset, middle + inset),
                        QPointF(chevron_left + self.CHEVRON / 2.0, middle - inset),
                        QPointF(chevron_left + self.CHEVRON - inset, middle + inset),
                    ]
                )
            )
        else:
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(chevron_left + inset, middle - inset),
                        QPointF(chevron_left + self.CHEVRON - inset, middle),
                        QPointF(chevron_left + inset, middle + inset),
                    ]
                )
            )
        painter.end()


class LoaderChoice(QWidget):
    """One loader row and the build list it opens *inside itself*.

    This is the PCL shape: clicking the row expands its builds under the header instead of revealing
    a dropdown at the bottom of the page. One row is open at a time (the step enforces that), the
    chosen build collapses the row, and clicking the header again closes the list without touching
    the choice.

    The "newest" build is presented separately at the top with its publication line; the rest of the
    source's list follows under an "全部版本 (N)" caption. Dates are printed only when the source
    supplies one - the three loader sources this launcher reads do not, so the rows show the version
    alone rather than a made-up date.
    """

    version_chosen = Signal(object)
    version_cleared = Signal()

    #: How tall the open build list may grow before it scrolls (about six rows).
    BODY_MAX_HEIGHT = 240

    def __init__(self, loader: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.loader = loader
        self.setObjectName("loaderChoice")
        self.header = LoaderRow(loader)
        self._versions: list[LoaderVersion] = []
        self._expanded = False
        self._selected: LoaderVersion | None = None
        self._items: list[LoaderVersionItem] = []
        self.body = QWidget()
        self.body.setObjectName("loaderChoiceBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(28, 0, 0, 8)
        self.body_layout.setSpacing(2)
        self.body.setVisible(False)
        # The body scrolls. Fabric alone lists 255 builds for one game version: laid out directly, the
        # column squeezed every row until the version numbers overlapped. A bounded viewport keeps the
        # rows at their real height and lets the list scroll.
        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("loaderChoiceScroll")
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setMaximumHeight(self.BODY_MAX_HEIGHT)
        self.body_scroll.setWidget(self.body)
        self.body_scroll.setVisible(False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.header)
        outer.addWidget(self.body_scroll)

    # ---------- data ----------

    def versions(self) -> list[LoaderVersion]:
        return list(self._versions)

    def selected_version(self) -> LoaderVersion | None:
        return self._selected

    def is_expanded(self) -> bool:
        return self._expanded

    def items(self) -> list[LoaderVersionItem]:
        """The built rows (empty while the list is closed or has not been rebuilt yet)."""
        return list(self._items)

    def set_versions(self, versions: list[LoaderVersion]) -> None:
        """Replace the offered builds and rebuild the body when it is open."""
        self._versions = list(versions)
        if self._selected is not None and self._selected.version not in {
            version.version for version in self._versions
        }:
            self.clear_selection(notify=False)
        if self._expanded:
            self._rebuild()

    def clear_selection(self, *, notify: bool = True) -> None:
        if self._selected is None:
            return
        self._selected = None
        self.header.setChecked(False)
        self.header.set_selected_version("")
        for item in self._items:
            item.set_selected(False)
        if notify:
            self.version_cleared.emit()

    # ---------- expansion ----------

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.header.set_expanded(expanded)
        self.body.setVisible(expanded)
        self.body_scroll.setVisible(expanded)
        if expanded:
            self._rebuild()

    def _clear_body(self) -> None:
        self._items = []
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _rebuild(self) -> None:
        self._clear_body()
        if not self._versions:
            label = QLabel(tr("versions.install.loader.no_version"))
            label.setObjectName("installStepHint")
            label.setWordWrap(True)
            self.body_layout.addWidget(label)
            return
        newest = self._versions[0]
        latest_meta = tr("versions.install.loader.latest")
        published = _published_text(getattr(newest, "release_time", ""))
        if published:
            latest_meta += " | " + tr("versions.latest.published", published)
        latest_item = LoaderVersionItem(newest, latest=True, meta=latest_meta)
        latest_item.clicked.connect(partial(self._choose, newest))
        self._items.append(latest_item)
        self.body_layout.addWidget(latest_item)

        rest = self._versions[1:]
        if rest:
            caption = QLabel(tr("versions.install.loader.all", len(self._versions)))
            caption.setObjectName("installStepHint")
            self.body_layout.addWidget(caption)
            for version in rest:
                item = LoaderVersionItem(
                    version,
                    meta=_published_text(getattr(version, "release_time", "")),
                )
                item.clicked.connect(partial(self._choose, version))
                self._items.append(item)
                self.body_layout.addWidget(item)
        self._sync_items()
        self.body_scroll.verticalScrollBar().setValue(0)

    def _choose(self, version: LoaderVersion) -> None:
        if self._selected is not None and self._selected.version == version.version:
            # Clicking the chosen build again is the way back to "download the vanilla version"; the
            # row header only opens and closes the list, as PCL's does.
            self.clear_selection()
            return
        self._selected = version
        self.header.setChecked(True)
        self.header.set_selected_version(version.version)
        self._sync_items()
        self.version_chosen.emit(version)

    def _sync_items(self) -> None:
        for item in self._items:
            item.set_selected(
                self._selected is not None
                and item.version.version == self._selected.version
            )

    def selected_title(self) -> str:
        return self._selected.version if self._selected is not None else ""


def _published_text(release_time: str) -> str:
    """A loader build's publication stamp, or "" when the source has none.

    The sources this launcher reads publish no per-build dates, so this is deliberately allowed to
    return nothing: the row then shows the version number alone instead of a fabricated date.
    """
    text = (release_time or "").strip()
    if len(text) < 10:
        return text
    from PySide6.QtCore import QDate, QDateTime

    if len(text) < 16:
        # A date without a time means the source only published a day: printing 00:00 would invent
        # a time the source never gave.
        date = QDate.fromString(text[:10], "yyyy-MM-dd")
        return f"{date.year()}/{date.month()}/{date.day()}" if date.isValid() else text
    stamp = QDateTime.fromString(text, Qt.DateFormat.ISODate)
    if stamp.isValid():
        stamp = stamp.toLocalTime()
        date, time = stamp.date(), stamp.time()
        return (
            f"{date.year()}/{date.month()}/{date.day()} "
            f"{time.hour():02d}:{time.minute():02d}"
        )
    date = QDate.fromString(text[:10], "yyyy-MM-dd")
    if date.isValid():
        return f"{date.year()}/{date.month()}/{date.day()}"
    return text


class _NameField(QLineEdit):
    """The instance-name field: reports when the user leaves it (an empty name is refilled then)."""

    focus_lost = Signal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_lost.emit()


class InstallStep(QWidget):
    """Second step of the versions page: a one-row header, one loader row, one pill button.

    The step owns no install logic at all. It reports ``start_requested`` (the pill), ``back_requested``
    (the header's back arrow) and ``retry_requested(loader)`` (a row whose state is "could not fetch"),
    and the page decides what those mean. That keeps the install flow in exactly one place while this
    module stays a piece of interface that a test can drive without a window.
    """

    back_requested = Signal()
    start_requested = Signal()
    retry_requested = Signal(str)
    #: Emitted when the checked loader changes ("" = the vanilla download). The page recomputes the
    #: default instance name from it, because a loader instance may not share the vanilla folder.
    loader_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("versionsInstallStep")
        self.version_id = ""
        self._default_name = ""
        self._taken: frozenset[str] = frozenset()
        self._loader_versions: dict[str, list[LoaderVersion]] = {}
        self._game_dir: Path | None = None
        self._cache_root: Path | None = None
        self._fallback_icon: QPixmap | None = None

        # The top line is one row, not a card: back arrow, the loader mark, and the name field filling
        # the rest. PCL's install page has exactly this shape, and the previous label + framed card
        # only added two lines of chrome to a flow that is one decision away from downloading.
        self.breadcrumb = QWidget()
        self.breadcrumb.setObjectName("installHeader")
        self.back_button = QPushButton()
        self.back_button.setObjectName("backButton")
        self.back_button.setFlat(True)
        self.back_button.setFixedSize(32, 32)
        self.back_button.setIconSize(QSize(20, 20))
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setAccessibleName(tr("versions.install.back"))
        self.back_button.setToolTip(tr("versions.install.back"))
        self.back_button.clicked.connect(self.back_requested.emit)
        self._back_icon = QIcon()
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(32, 32)
        self.icon_label.setObjectName("installHeaderIcon")
        self.name_edit = _NameField()
        self.name_edit.setFixedHeight(36)
        self.name_edit.setMaxLength(MAX_INSTANCE_NAME_LENGTH)
        self.name_edit.setAccessibleName(tr("versions.install.name.label"))
        self.name_edit.textChanged.connect(lambda _text: self._refresh_name_state())
        self.name_edit.focus_lost.connect(self._refill_name)
        self.name_hint = QLabel()
        self.name_hint.setObjectName("installNameHint")
        self.name_hint.setWordWrap(True)
        self.name_hint.setVisible(False)

        header_layout = QHBoxLayout(self.breadcrumb)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)
        header_layout.addWidget(self.back_button)
        header_layout.addWidget(self.icon_label)
        header_layout.addWidget(self.name_edit, 1)

        self.hint = QLabel(tr("versions.install.hint"))
        self.hint.setObjectName("installStepHint")
        self.hint.setWordWrap(True)

        self.rows: dict[str, LoaderRow] = {}
        self.choices: dict[str, LoaderChoice] = {}
        rows_layout = QVBoxLayout()
        rows_layout.setSpacing(8)
        for loader in LOADER_ROWS:
            choice = LoaderChoice(loader)
            choice.header.clicked.connect(
                lambda _checked=False, item=choice: self._on_row_clicked(item)
            )
            choice.version_chosen.connect(
                lambda version, key=loader: self._on_version_chosen(key, version)
            )
            choice.version_cleared.connect(
                lambda key=loader: self._on_version_cleared(key)
            )
            self.rows[loader] = choice.header
            self.choices[loader] = choice
            rows_layout.addWidget(choice)

        self.start_button = QPushButton(tr("versions.install.vanilla"))
        self.start_button.setObjectName("installPrimary")
        self.start_button.setMinimumHeight(48)
        self.start_button.setMinimumWidth(200)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.start_requested.emit)
        button_row = QHBoxLayout()
        button_row.setSpacing(0)
        button_row.addStretch(1)
        button_row.addWidget(self.start_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        # The page owns its own outer margin and 16px top inset (it is one of the top-level pages);
        # this step only supplies its internal 8px rhythm, because it sits inside that page.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.breadcrumb)
        layout.addWidget(self.name_hint)
        layout.addWidget(self.hint)
        layout.addLayout(rows_layout)
        layout.addStretch(1)
        layout.addLayout(button_row)
        self._refresh_back_icon()
        self._refresh_header_icon()

    # ---------- data ----------

    def prepare(
        self,
        version_id: str,
        icon: QPixmap | None = None,
        *,
        name: str,
        taken: set[str] | frozenset[str] = frozenset(),
        game_dir: Path | None = None,
        cache_root: Path | None = None,
    ) -> None:
        """Fill the step for ``version_id``: name, grass mark and a fresh round of loader checks."""
        self.version_id = version_id
        self._default_name = (name or version_id).strip() or version_id
        self._taken = frozenset(taken)
        self._game_dir = Path(game_dir) if game_dir is not None else None
        self._cache_root = Path(cache_root) if cache_root is not None else None
        self._fallback_icon = icon
        self._loader_versions = {}
        self.name_edit.setText(self._default_name)
        for row in self.rows.values():
            row.setChecked(False)
            row.set_selected_version("")
            row.set_status(CHECKING)
        for choice in self.choices.values():
            choice.clear_selection(notify=False)
            choice.set_versions([])
            choice.set_expanded(False)
        self._refresh_header_icon()
        self._refresh_name_state()
        self._update_start_button()

    def name_editable(self) -> bool:
        """Every install names its own instance now, including a version that is already installed."""
        return True

    def default_name(self) -> str:
        return self._default_name

    def set_default_name(
        self, name: str, taken: set[str] | frozenset[str] | None = None
    ) -> None:
        """Change the name the field falls back to (the loader build changes the default).

        A name the user typed is never overwritten: only an empty field or the previous default
        follows the new loader.
        """
        new_default = (name or self.version_id).strip() or self.version_id
        if taken is not None:
            self._taken = frozenset(taken)
        current = self.name_edit.text().strip()
        if current in ("", self._default_name):
            self.name_edit.setText(new_default)
        self._default_name = new_default
        self._refresh_name_state()
        self._update_start_button()

    def instance_name(self) -> str:
        """The name the instance will carry (the default whenever the field is empty)."""
        return self.name_edit.text().strip() or self._default_name

    def name_error(self) -> str:
        """What the line under the field says, if anything. Any message here is a hard error."""
        text = self.name_edit.text().strip()
        if invalid_name_chars(text):
            return tr("versions.install.name.invalid")
        if len(text) > MAX_INSTANCE_NAME_LENGTH:
            return tr("versions.install.name.length")
        if text and text.casefold() in {item.casefold() for item in self._taken}:
            return tr("versions.install.name.taken_error", suggest_name(text, self._taken))
        return ""

    def name_valid(self) -> bool:
        return not self.name_error()

    def set_loader_status(
        self,
        loader: str,
        state: str,
        detail: str = "",
        *,
        versions: list[LoaderVersion] | None = None,
    ) -> None:
        row = self.rows.get(loader)
        if row is None:
            return
        row.set_status(state, detail, versions=versions)
        choice = self.choices.get(loader)
        if choice is not None:
            choice.set_versions(list(versions or []))
            if state != CAN_ADD:
                choice.set_expanded(False)
        if versions is not None:
            self._loader_versions[loader] = list(versions)
        elif state != CAN_ADD:
            self._loader_versions.pop(loader, None)
        self._refresh_header_icon()
        self._update_start_button()

    def loader_versions(self, loader: str) -> list[LoaderVersion]:
        """The builds offered for ``loader`` (empty until its probe answered with a list)."""
        return list(self._loader_versions.get(loader, []))

    def loader_status(self, loader: str) -> str:
        row = self.rows.get(loader)
        return row.state() if row is not None else ""

    def loader_status_text(self, loader: str) -> str:
        row = self.rows.get(loader)
        return row.status_text() if row is not None else ""

    def selected_loader_version(self) -> LoaderVersion | None:
        """The build chosen inside the checked loader's row (None = no loader chosen)."""
        loader = self.selected_loader()
        if loader is None:
            return None
        chosen = self.choices[loader].selected_version()
        if chosen is not None:
            return chosen
        versions = self._loader_versions.get(loader, [])
        return versions[0] if versions else None

    def selected_loader(self) -> str | None:
        """The checked loader, or None - None means "download the vanilla version"."""
        for loader, row in self.rows.items():
            if row.isChecked() and row.state() == CAN_ADD:
                return loader
        return None

    def loader_names(self) -> list[str]:
        """The loaders that got a row, top to bottom (what the tests and the report read)."""
        return [display_loader_name(loader) for loader in self.rows]

    # ---------- private ----------

    def _on_row_clicked(self, choice: LoaderChoice) -> None:
        """Open this row's builds in place; a second click closes them without changing the choice.

        Opening another row closes the previous one, so the page never grows two build lists: the
        summary and the pill keep their place while the user compares loaders.
        """
        row = choice.header
        # The header is a checkable button (space/enter activate it), but only a build row decides
        # the selection: undo the toggle the click just performed before opening/closing the list.
        row.setChecked(choice.selected_version() is not None)
        if row.state() == FETCH_FAILED:
            self.retry_requested.emit(row.loader)
            return
        if row.state() != CAN_ADD:
            return
        if choice.is_expanded():
            choice.set_expanded(False)
            return
        self._collapse_choices()
        choice.set_expanded(True)

    def _collapse_choices(self) -> None:
        for choice in self.choices.values():
            if choice.is_expanded():
                choice.set_expanded(False)

    def _on_version_chosen(self, loader: str, version: LoaderVersion) -> None:
        """A build was picked: it becomes the row's answer and every other loader lets go."""
        for key, choice in self.choices.items():
            if key == loader:
                continue
            choice.clear_selection(notify=False)
            choice.set_expanded(False)
        self.choices[loader].set_expanded(False)
        self._refresh_header_icon()
        self._update_start_button()
        self.loader_changed.emit(loader)

    def _on_version_cleared(self, loader: str) -> None:
        """The chosen build was clicked again: the flow is back to the vanilla download."""
        self._refresh_header_icon()
        self._update_start_button()
        self.loader_changed.emit("")

    def _refresh_loader_choice(self) -> None:
        """Push the probed builds into their rows (the rows build their own lists on demand)."""
        for loader, choice in self.choices.items():
            choice.set_versions(self._loader_versions.get(loader, []))
        self._refresh_header_icon()
        self._update_start_button()

    # ---------- the top line ----------

    @staticmethod
    def _back_pixmap(color: str, size: int = 20) -> QPixmap:
        """A left arrow for the icon-only back button (drawn, so no font glyph can go missing)."""
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(color))
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(size * 0.72, size * 0.5), QPointF(size * 0.28, size * 0.5))
        painter.drawLine(QPointF(size * 0.28, size * 0.5), QPointF(size * 0.46, size * 0.32))
        painter.drawLine(QPointF(size * 0.28, size * 0.5), QPointF(size * 0.46, size * 0.68))
        painter.end()
        return pixmap

    def _refresh_back_icon(self) -> None:
        if not hasattr(self, "back_button"):
            return
        tokens = _tokens()
        self._back_icon = QIcon(
            self._back_pixmap(tokens.get("FG_HINT", "#4f6070"))
        )
        self.back_button.setIcon(self._back_icon)

    def _refresh_header_icon(self) -> None:
        """Grass for a vanilla install, the chosen loader's own mark once one is selected."""
        if not hasattr(self, "icon_label") or not hasattr(self, "rows"):
            return  # a style event during construction, before the page exists
        tokens = _tokens()
        accent = tokens.get("ACCENT", "#2f9e6f")
        loader = self.selected_loader() or ""
        pixmap: QPixmap | None = None
        try:
            if loader:
                logo_dir = (
                    self._cache_root.parent / "loader-logos"
                    if self._cache_root is not None
                    else None
                )
                pixmap = block_icons.loader_pixmap(
                    loader, accent, 32, logo_cache_dir=logo_dir
                )
            else:
                pixmap = block_icons.rule_icon_pixmap(
                    "grass", 32, game_dir=self._game_dir, cache_root=self._cache_root
                )
        except Exception:  # noqa: BLE001 - a missing texture must not break the step
            pixmap = None
        fallback = self._fallback_icon
        if (pixmap is None or pixmap.isNull()) and fallback is not None and not fallback.isNull():
            pixmap = fallback
        if pixmap is None or pixmap.isNull():
            self.icon_label.clear()
            return
        self.icon_label.setPixmap(
            pixmap.scaled(
                32,
                32,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def changeEvent(self, event) -> None:
        """Repaint the drawn marks when the theme changes (their colours are tokens, not QSS)."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self._refresh_back_icon()
            self._refresh_header_icon()
            for row in getattr(self, "rows", {}).values():
                row.update()
            for choice in getattr(self, "choices", {}).values():
                for item in choice.items():
                    item.update()

    def _update_start_button(self) -> None:
        loader = self.selected_loader()
        key = "versions.install.start" if loader else "versions.install.vanilla"
        text = tr(key)
        if self.start_button.text() != text:
            self.start_button.setText(text)
        self.start_button.setEnabled(self.name_valid())

    def _refill_name(self) -> None:
        """Leaving the field empty hands the default name back (an instance always has a name)."""
        if not self.name_edit.text().strip():
            self.name_edit.setText(self._default_name)
        self._refresh_name_state()

    def _refresh_name_state(self) -> None:
        message = self.name_error()
        invalid = bool(message)
        if bool(self.name_edit.property("invalid")) != invalid:
            self.name_edit.setProperty("invalid", invalid)
            style = self.name_edit.style()
            if style is not None:
                style.unpolish(self.name_edit)
                style.polish(self.name_edit)
        self.name_hint.setText(message)
        self.name_hint.setVisible(bool(message))
        if bool(self.name_hint.property("error")) != invalid:
            self.name_hint.setProperty("error", invalid)
            style = self.name_hint.style()
            if style is not None:
                style.unpolish(self.name_hint)
                style.polish(self.name_hint)
        self.start_button.setEnabled(self.name_valid())
