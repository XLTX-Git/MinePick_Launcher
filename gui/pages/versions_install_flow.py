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
breadcrumb card carrying the editable instance name, one row per mod loader this launcher can
actually install, and a single pill button that reads "download vanilla" until a loader is chosen.
Nothing here opens a window: progress continues in the version list itself (one row's install button
turns into a compact progress block) and a failure is reported where every other install failure is.

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
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, paint_utils
from launcher.install import install_version
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
    """``name-2``, ``name-3`` ...: the first alternative the caller may offer for a taken name."""
    index = 2
    while f"{name}-{index}" in taken:
        index += 1
    return f"{name}-{index}"


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


class LoaderRow(QAbstractButton):
    """One loader choice: a 56px card with the loader name (15/600), its state and a chevron.

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
        self._hover = False
        self.set_status(CHECKING)

    # ---------- data ----------

    def state(self) -> str:
        return self._state

    def detail(self) -> str:
        """The error text behind a FETCH_FAILED row (empty for every other state)."""
        return self._detail

    def status_text(self) -> str:
        return tr(STATUS_KEYS[self._state])

    def set_status(self, state: str, detail: str = "") -> None:
        """Set the state (one of the four module constants) and make the row behave like it."""
        self._state = state
        self._detail = detail
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
        # box in the middle of the row.
        pen = QPen(QColor(tokens.get("FG_HINT", "#4f6070")))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        middle = self.HEIGHT / 2.0
        inset = self.CHEVRON * 0.28
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(chevron_left + inset, middle - inset * 1.6),
                    QPointF(chevron_left + self.CHEVRON - inset, middle),
                    QPointF(chevron_left + inset, middle + inset * 1.6),
                ]
            )
        )
        painter.end()


class _NameField(QLineEdit):
    """The instance-name field: reports when the user leaves it (an empty name is refilled then)."""

    focus_lost = Signal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_lost.emit()


class InstallStep(QWidget):
    """Second step of the versions page: breadcrumb card, one row per loader, one pill button.

    The step owns no install logic at all. It reports ``start_requested`` (the pill), ``back_requested``
    (the breadcrumb arrow) and ``retry_requested(loader)`` (a row whose state is "could not fetch"),
    and the page decides what those mean. That keeps the install flow in exactly one place while this
    module stays a piece of interface that a test can drive without a window.
    """

    back_requested = Signal()
    start_requested = Signal()
    retry_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("versionsInstallStep")
        self.version_id = ""
        self._default_name = ""
        self._taken: frozenset[str] = frozenset()
        self._editable = True

        self.breadcrumb = QFrame()
        self.breadcrumb.setObjectName("installBreadcrumb")
        self.back_button = QPushButton("← " + tr("versions.install.back"))
        # The site's one back-button shape (#backButton: 4px 8px padding, transparent, left aligned),
        # not the larger outlined secondary button. The breadcrumb card already supplies the frame.
        self.back_button.setObjectName("backButton")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setAccessibleName(tr("versions.install.back"))
        self.back_button.clicked.connect(self.back_requested.emit)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(32, 32)
        self.icon_label.setObjectName("installBreadcrumbIcon")
        self.name_label = QLabel(tr("versions.install.name.label"))
        self.name_label.setObjectName("installFieldLabel")
        self.name_edit = _NameField()
        self.name_edit.setAccessibleName(tr("versions.install.name.label"))
        self.name_edit.textChanged.connect(lambda _text: self._refresh_name_state())
        self.name_edit.focus_lost.connect(self._refill_name)
        self.name_fixed = QLabel()
        self.name_fixed.setObjectName("installFixedName")
        self.name_fixed.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.name_hint = QLabel()
        self.name_hint.setObjectName("installNameHint")
        self.name_hint.setWordWrap(True)
        self.name_hint.setVisible(False)

        name_column = QVBoxLayout()
        name_column.setSpacing(4)
        name_column.addWidget(self.name_label)
        name_column.addWidget(self.name_edit)
        name_column.addWidget(self.name_fixed)
        name_column.addWidget(self.name_hint)
        breadcrumb_layout = QHBoxLayout(self.breadcrumb)
        breadcrumb_layout.setContentsMargins(12, 10, 12, 10)
        breadcrumb_layout.setSpacing(12)
        breadcrumb_layout.addWidget(self.back_button)
        breadcrumb_layout.addWidget(self.icon_label)
        breadcrumb_layout.addLayout(name_column, 1)

        self.hint = QLabel(tr("versions.install.hint"))
        self.hint.setObjectName("installStepHint")
        self.hint.setWordWrap(True)

        self.rows: dict[str, LoaderRow] = {}
        rows_layout = QVBoxLayout()
        rows_layout.setSpacing(8)
        for loader in LOADER_ROWS:
            row = LoaderRow(loader)
            row.clicked.connect(lambda _checked=False, item=row: self._on_row_clicked(item))
            self.rows[loader] = row
            rows_layout.addWidget(row)

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
        layout.addWidget(self.hint)
        layout.addLayout(rows_layout)
        layout.addStretch(1)
        layout.addLayout(button_row)

    # ---------- data ----------

    def prepare(
        self,
        version_id: str,
        icon: QPixmap | None,
        *,
        editable: bool,
        name: str,
        taken: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        """Fill the step for ``version_id``: name, icon and a fresh round of loader checks."""
        self.version_id = version_id
        self._default_name = (name or version_id).strip() or version_id
        self._taken = frozenset(taken)
        self._editable = editable
        if icon is not None and not icon.isNull():
            self.icon_label.setPixmap(
                icon.scaled(
                    32,
                    32,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.icon_label.clear()
        self.name_edit.setVisible(editable)
        self.name_fixed.setVisible(not editable)
        self.name_fixed.setText(self._default_name)
        if editable:
            self.name_edit.setText(self._default_name)
        else:
            self.name_edit.setText("")
        for row in self.rows.values():
            row.setChecked(False)
            row.set_status(CHECKING)
        self._refresh_name_state()
        self._update_start_button()

    def name_editable(self) -> bool:
        """True while the field may be edited - the first install of a version, never a reinstall."""
        return self._editable

    def instance_name(self) -> str:
        """The name the instance will carry (the default whenever the field is empty)."""
        if not self._editable:
            return self._default_name
        return self.name_edit.text().strip() or self._default_name

    def name_error(self) -> str:
        """What the line under the field says, if anything."""
        if not self._editable:
            return tr("versions.install.name.fixed")
        text = self.name_edit.text().strip()
        if invalid_name_chars(text):
            return tr("versions.install.name.invalid")
        if text and text in self._taken:
            return tr("versions.install.name.taken", suggest_name(text, self._taken))
        return ""

    def name_valid(self) -> bool:
        return not invalid_name_chars(self.name_edit.text().strip())

    def set_loader_status(self, loader: str, state: str, detail: str = "") -> None:
        row = self.rows.get(loader)
        if row is None:
            return
        row.set_status(state, detail)
        self._update_start_button()

    def loader_status(self, loader: str) -> str:
        row = self.rows.get(loader)
        return row.state() if row is not None else ""

    def loader_status_text(self, loader: str) -> str:
        row = self.rows.get(loader)
        return row.status_text() if row is not None else ""

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

    def _on_row_clicked(self, row: LoaderRow) -> None:
        if row.state() == FETCH_FAILED:
            self.retry_requested.emit(row.loader)
            return
        if row.isChecked():
            for other in self.rows.values():
                if other is not row and other.isChecked():
                    other.setChecked(False)
        self._update_start_button()

    def _update_start_button(self) -> None:
        loader = self.selected_loader()
        key = "versions.install.start" if loader else "versions.install.vanilla"
        text = tr(key)
        if self.start_button.text() != text:
            self.start_button.setText(text)
        self.start_button.setEnabled(self.name_valid())

    def _refill_name(self) -> None:
        """Leaving the field empty hands the default name back (an instance always has a name)."""
        if not self._editable:
            return
        if not self.name_edit.text().strip():
            self.name_edit.setText(self._default_name)
        self._refresh_name_state()

    def _refresh_name_state(self) -> None:
        invalid = bool(invalid_name_chars(self.name_edit.text()))
        if bool(self.name_edit.property("invalid")) != invalid:
            self.name_edit.setProperty("invalid", invalid)
            style = self.name_edit.style()
            if style is not None:
                style.unpolish(self.name_edit)
                style.polish(self.name_edit)
        message = self.name_error()
        self.name_hint.setText(message)
        self.name_hint.setVisible(bool(message))
        # Only the refused characters are an error: the duplicate-name line is advice (the sidecar
        # allows two instances to share a display name, the folder is what must stay unique).
        if bool(self.name_hint.property("error")) != invalid:
            self.name_hint.setProperty("error", invalid)
            style = self.name_hint.style()
            if style is not None:
                style.unpolish(self.name_hint)
                style.polish(self.name_hint)
        self.start_button.setEnabled(self.name_valid())
