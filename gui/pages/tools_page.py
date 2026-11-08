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

"""The 工具 page: its 联机 tab and the six reviewed layouts.

The page owns no room logic. It renders a :class:`launcher.link.LinkSession` and forwards user
actions to it; the session's listener fires :attr:`ToolsPage.session_changed`, which is connected to
:meth:`_sync` as a queued Qt signal, so a future backend can notify from a worker thread.

Every colour, radius and spacing in the page-level stylesheet comes from the existing theme tokens
(``gui/resources/style.qss`` and ``style_light.qss``). The small painted widgets read the same tokens
at paint time instead of carrying their own colours.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, theme
from gui.widgets import add_page_top_inset, build_download_progress, style_page_layout
from launcher.link import (
    LinkDifficulty,
    LinkMember,
    LinkSession,
    LinkState,
    default_link_session,
)

tr = i18n.tr
log = logging.getLogger(__name__)

#: Same scale as every other download in the launcher: 0..1000, a tenth of a percent a step.
PROGRESS_MAX = 1000
#: The six reviewed layouts, in the order of the design mock-up. ``FAILED`` borrows the last page.
SCREEN_STATES: tuple[LinkState, ...] = (
    LinkState.NOT_DOWNLOADED,
    LinkState.IDLE,
    LinkState.HOST_WAITING,
    LinkState.HOST_READY,
    LinkState.CONNECTING,
    LinkState.JOINED,
)
_DIFFICULTY_INDEX = {
    LinkDifficulty.EASY: 0,
    LinkDifficulty.MEDIUM: 1,
    LinkDifficulty.TOUGH: 2,
}


def _tokens() -> dict[str, str]:
    """The applied palette, with the light one as the pre-theme fallback (tests build pages early)."""
    return theme.current_tokens() or theme.theme_palette("light")


def _lines(key: str) -> list[str]:
    """Split a multi-line translation into its non-empty lines (numbered steps, status + hint)."""
    return [line.strip() for line in tr(key).splitlines() if line.strip()]


def _difficulty_labels() -> list[str]:
    """The three localized difficulty names, in ``LinkDifficulty`` order."""
    labels = _lines("tools.difficulty")
    return labels if len(labels) >= 3 else ["", "", ""]


def _default_instances() -> list[tuple[str, str]]:
    """Instances of the current game folder, as ``(id, label)`` pairs for the host combo."""
    from launcher import config
    from launcher.instances import display_version_name, list_instances

    cfg, _ = config.load()
    folder = cfg.active_folder()
    game_dir = folder.path if folder is not None else None
    items: list[tuple[str, str]] = []
    for instance in list_instances(game_dir).values():
        version = display_version_name(instance.version_id)
        label = instance.name if version == instance.name else f"{instance.name} · {version}"
        items.append((instance.id, label))
    return items


class _RoundBadge(QWidget):
    """A painted disc used for the info mark, step numbers, avatars and the joined check.

    Painted rather than styled with a label because a 16-24px circle needs a radius of half its
    size, which is geometry, not a theme radius token. The fill and ink are still token names.
    """

    def __init__(
        self,
        text: str,
        *,
        diameter: int = 18,
        fill: str = "ACCENT_SOFT",
        ink: str = "ACCENT_TEXT",
        pixel_size: int = 10,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._fill = fill
        self._ink = ink
        self._pixel_size = pixel_size
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens[self._fill]))
        painter.drawEllipse(self.rect())
        font = QFont(self.font())
        font.setPixelSize(self._pixel_size)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(tokens[self._ink]))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()


class _Spinner(QWidget):
    """A small arc spinner. The timer only runs while the widget is visible."""

    def __init__(self, diameter: int = 22, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0
        self.setFixedSize(diameter, diameter)
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        side = min(self.width(), self.height())
        rect = QRectF(1.0, 1.0, side - 2.0, side - 2.0)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QPen(QColor(tokens["BORDER_LIST"]), 2.0)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(rect, 0, 360 * 16)
        arc = QPen(QColor(tokens["ACCENT"]), 2.0)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc)
        painter.drawArc(rect, self._angle * 16, 270 * 16)
        painter.end()


class ToolsPage(QWidget):
    """工具 › 联机: the page depends only on the :class:`LinkSession` interface."""

    #: Emitted from the session listener. A queued connection is what makes a worker-thread backend
    #: safe later; with the fake everything happens on the GUI thread and is delivered immediately.
    session_changed = Signal()
    #: The progress callback is itself a signal so a real downloader may report from a worker.
    download_progress = Signal(int)
    #: Reserved for the next change: opening multiplayer needs a real launch path.
    open_multiplayer_requested = Signal(str)

    def __init__(
        self,
        session: LinkSession | None = None,
        *,
        session_factory: Callable[[], LinkSession] = default_link_session,
        instances_provider: Callable[[], list[tuple[str, str]]] | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("toolsPage")
        self._session = session if session is not None else session_factory()
        self._instances_provider = instances_provider or _default_instances
        self._unsubscribe: Callable[[], None] | None = None
        self._last_state: LinkState | None = None
        self._pages: dict[LinkState, QWidget] = {}

        self._build_ui()
        self.session_changed.connect(self._sync)
        self.download_progress.connect(self._on_download_progress)
        self.destroyed.connect(self._on_destroyed)
        self._attach_session(self._session)
        self._refresh_instances()
        self._sync()
        theme.register_self_styling(self)

    # ------------------------------------------------------------------ construction
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        style_page_layout(outer)
        add_page_top_inset(outer)

        title = QLabel(tr("nav.tools"))
        title.setObjectName("toolsPageTitle")
        outer.addWidget(title)

        card = QFrame()
        card.setObjectName("pageCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 8, 16, 8)
        card_layout.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("toolsTabs")
        self.tabs.setAccessibleName(tr("tools.tab.link"))
        self.tabs.tabBar().setExpanding(False)
        self.tabs.setDocumentMode(False)

        tab_page = QWidget()
        tab_layout = QVBoxLayout(tab_page)
        tab_layout.setContentsMargins(0, 4, 0, 0)
        tab_layout.setSpacing(8)

        tab_layout.addWidget(self._build_banner())
        self.state_stack = QStackedWidget()
        tab_layout.addWidget(self.state_stack, 1)

        self._pages[LinkState.NOT_DOWNLOADED] = self._build_onboarding_page()
        self._pages[LinkState.IDLE] = self._build_idle_page()
        self._pages[LinkState.HOST_WAITING] = self._build_host_waiting_page()
        self._pages[LinkState.HOST_READY] = self._build_host_ready_page()
        self._pages[LinkState.CONNECTING] = self._build_connecting_page()
        self._pages[LinkState.JOINED] = self._build_joined_page()
        self._failed_page = self._build_failed_page()
        for page in (*self._pages.values(), self._failed_page):
            self.state_stack.addWidget(page)

        self.tabs.addTab(tab_page, tr("tools.tab.link"))
        card_layout.addWidget(self.tabs, 1)
        card_layout.addWidget(self._build_footer())
        outer.addWidget(card, 1)

    def _build_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("toolsBanner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)
        layout.addWidget(_RoundBadge("i", diameter=16, fill="ACCENT", ink="FG_SELECTED", pixel_size=10))
        text = QLabel(tr("tools.banner"))
        text.setObjectName("toolsBannerText")
        text.setWordWrap(True)
        layout.addWidget(text, 1)
        return banner

    def _build_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("toolsFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        provided = QLabel(tr("tools.footer.provided"))
        provided.setObjectName("toolsFooterText")
        disable = QLabel(tr("tools.footer.disable"))
        disable.setObjectName("toolsFooterLink")
        layout.addWidget(provided)
        layout.addStretch(1)
        layout.addWidget(disable)
        return footer

    @staticmethod
    def _state_page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        return page, layout

    def _steps(self, key: str, *, numbered: bool = True) -> QWidget:
        """A vertical list of steps, with a painted number badge when ``numbered``."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for index, line in enumerate(_lines(key), start=1):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            if numbered:
                row.addWidget(_RoundBadge(str(index)), 0, Qt.AlignmentFlag.AlignTop)
            label = QLabel(line)
            label.setObjectName("toolsStepText")
            label.setWordWrap(True)
            row.addWidget(label, 1)
            layout.addLayout(row)
        return box

    def _section(self, title_key: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("toolsInnerCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        title = QLabel(tr(title_key))
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        return card, layout

    def _build_onboarding_page(self) -> QWidget:
        page, layout = self._state_page()
        title = QLabel(tr("tools.onboard.title"))
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        body = QLabel(tr("tools.onboard.body"))
        body.setObjectName("hint")
        body.setWordWrap(True)
        layout.addWidget(body)

        row = QFrame()
        row.setObjectName("toolsRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 12, 12, 12)
        row_layout.setSpacing(12)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(4)
        row_title = QLabel(tr("tools.onboard.download"))
        row_title.setObjectName("toolsRowTitle")
        row_title.setWordWrap(True)
        row_hint = QLabel(tr("tools.onboard.download_hint"))
        row_hint.setObjectName("hint")
        row_hint.setWordWrap(True)
        text_box.addWidget(row_title)
        text_box.addWidget(row_hint)
        self.download_progress_row, self.download_progress_bar, _ = build_download_progress(
            "linkCoreProgress"
        )
        self.download_progress_bar.setRange(0, PROGRESS_MAX)
        self.download_progress_bar.setValue(0)
        self.download_progress_bar.setAccessibleName(tr("tools.onboard.download"))
        text_box.addWidget(self.download_progress_row)
        row_layout.addLayout(text_box, 1)

        self.download_button = QPushButton("\u2193")
        self.download_button.setObjectName("toolsIconButton")
        self.download_button.setFixedSize(40, 40)
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.setAccessibleName(tr("tools.onboard.download"))
        self.download_button.clicked.connect(self._download_core)
        row_layout.addWidget(self.download_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(row)
        layout.addStretch(1)

        self.channels_button = QPushButton()
        self.channels_button.setObjectName("toolsDisclosure")
        self.channels_button.setCheckable(True)
        self.channels_button.setAccessibleName(tr("tools.onboard.channels"))
        self.channels_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        channels_layout = QHBoxLayout(self.channels_button)
        channels_layout.setContentsMargins(0, 0, 0, 0)
        channels_layout.setSpacing(8)
        channels_label = QLabel(tr("tools.onboard.channels"))
        channels_label.setObjectName("toolsDisclosureText")
        self.channels_arrow = QLabel("\u25be")
        self.channels_arrow.setObjectName("toolsDisclosureArrow")
        self.channels_arrow.setFixedWidth(16)
        self.channels_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        channels_layout.addWidget(channels_label)
        channels_layout.addStretch(1)
        channels_layout.addWidget(self.channels_arrow)
        self.channels_button.toggled.connect(self._on_channels_toggled)
        layout.addWidget(self.channels_button)

        self.channels_detail = QLabel(tr("tools.onboard.channels_detail"))
        self.channels_detail.setObjectName("hint")
        self.channels_detail.setWordWrap(True)
        self.channels_detail.setVisible(False)
        layout.addWidget(self.channels_detail)
        return page

    def _build_idle_page(self) -> QWidget:
        page, layout = self._state_page()

        join_card, join_layout = self._section("tools.join.title")
        self.join_card = join_card
        join_layout.addWidget(self._steps("tools.join.body"))
        join_layout.addStretch(1)
        join_row = QHBoxLayout()
        join_row.setContentsMargins(0, 0, 0, 0)
        join_row.setSpacing(8)
        self.join_edit = QLineEdit()
        self.join_edit.setObjectName("toolsJoinEdit")
        self.join_edit.setAccessibleName(tr("tools.join.placeholder"))
        self.join_edit.setPlaceholderText(tr("tools.join.placeholder"))
        self.join_edit.setFixedHeight(32)
        self.clear_button = QPushButton(tr("tools.join.clear"))
        self.clear_button.setObjectName("secondaryButton")
        self.paste_button = QPushButton(tr("tools.join.paste"))
        self.paste_button.setObjectName("secondaryButton")
        self.join_button = QPushButton(tr("tools.join.submit"))
        for button in (self.clear_button, self.paste_button, self.join_button):
            button.setFixedHeight(32)
        join_row.addWidget(self.join_edit, 1)
        join_row.addWidget(self.clear_button)
        join_row.addWidget(self.paste_button)
        join_row.addWidget(self.join_button)
        join_layout.addLayout(join_row)
        self.clear_button.clicked.connect(self.join_edit.clear)
        self.paste_button.clicked.connect(self._paste_code)
        self.join_button.clicked.connect(self._join)
        self.join_edit.textChanged.connect(self._update_join_enabled)
        self.join_edit.returnPressed.connect(self._join)
        layout.addWidget(join_card)

        host_card, host_layout = self._section("tools.host.title")
        self.host_card = host_card
        host_layout.addWidget(self._steps("tools.host.body"))
        host_layout.addStretch(1)
        host_label = QLabel(tr("tools.host.instance"))
        host_label.setObjectName("formLabel")
        self.instance_combo = QComboBox()
        self.instance_combo.setAccessibleName(tr("tools.host.instance"))
        self.instance_combo.setFixedHeight(32)
        self.refresh_button = QPushButton(tr("mods.installed.refresh"))
        self.refresh_button.setObjectName("secondaryButton")
        self.create_button = QPushButton(tr("tools.host.submit"))
        for button in (self.refresh_button, self.create_button):
            button.setFixedHeight(32)
        host_row = QHBoxLayout()
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.setSpacing(8)
        host_row.addWidget(self.instance_combo, 1)
        host_row.addWidget(self.refresh_button)
        host_row.addWidget(self.create_button)
        host_layout.addWidget(host_label)
        host_layout.addLayout(host_row)
        self.refresh_button.clicked.connect(self._refresh_instances)
        self.create_button.clicked.connect(self._host)
        layout.addWidget(host_card)
        layout.addStretch(1)
        return page

    def _build_host_waiting_page(self) -> QWidget:
        page, layout = self._state_page()
        layout.addStretch(1)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(8)
        heading.addStretch(1)
        heading.addWidget(_Spinner())
        title = QLabel(tr("tools.wait.title"))
        title.setObjectName("toolsCenterTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        layout.addLayout(heading)

        hint = QLabel(tr("tools.wait.hint"))
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        card = QFrame()
        card.setObjectName("toolsStepCard")
        card.setMinimumWidth(520)
        card.setMaximumWidth(560)
        self.wait_step_card = card
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(8)
        next_label = QLabel(tr("wizard.next"))
        next_label.setObjectName("hint")
        card_layout.addWidget(next_label)
        card_layout.addWidget(self._steps("tools.wait.steps"))
        cancel_row = QHBoxLayout()
        cancel_row.setContentsMargins(0, 0, 0, 0)
        cancel_row.setSpacing(8)
        cancel_row.addStretch(1)
        self.wait_cancel_button = QPushButton(tr("dialog.cancel"))
        self.wait_cancel_button.setObjectName("secondaryButton")
        self.wait_cancel_button.setFixedHeight(32)
        self.wait_cancel_button.clicked.connect(self._cancel)
        cancel_row.addWidget(self.wait_cancel_button)
        cancel_row.addStretch(1)
        card_layout.addLayout(cancel_row)

        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.setSpacing(0)
        card_row.addStretch(1)
        card_row.addWidget(card)
        card_row.addStretch(1)
        layout.addLayout(card_row)
        layout.addStretch(1)
        return page

    def _build_host_ready_page(self) -> QWidget:
        page, layout = self._state_page()

        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(8)
        status.addWidget(_RoundBadge("\u2022", diameter=10, fill="ACCENT", ink="FG_SELECTED", pixel_size=8))
        title = QLabel(tr("tools.ready.status"))
        title.setObjectName("sectionTitle")
        status.addWidget(title)
        status.addStretch(1)
        layout.addLayout(status)

        room = QFrame()
        room.setObjectName("toolsRoomCode")
        room_layout = QVBoxLayout(room)
        room_layout.setContentsMargins(16, 12, 16, 12)
        room_layout.setSpacing(4)
        code_hint = QLabel(tr("tools.ready.code_hint"))
        code_hint.setObjectName("hint")
        code_hint.setWordWrap(True)
        room_layout.addWidget(code_hint)
        code_row = QHBoxLayout()
        code_row.setContentsMargins(0, 0, 0, 0)
        code_row.setSpacing(8)
        self.room_code_label = QLabel("")
        self.room_code_label.setObjectName("toolsRoomCodeText")
        self.room_code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code_row.addWidget(self.room_code_label, 1)
        self.copy_button = QPushButton(tr("tools.ready.copy"))
        self.copy_button.setObjectName("secondaryButton")
        self.copy_button.setFixedHeight(32)
        self.copy_button.clicked.connect(self._copy_code)
        code_row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignVCenter)
        room_layout.addLayout(code_row)
        layout.addWidget(room)

        self.members_count_label = QLabel("")
        self.members_count_label.setObjectName("sectionTitle")
        layout.addWidget(self.members_count_label)

        members_panel = QFrame()
        members_panel.setObjectName("toolsMemberPanel")
        self.members_layout = QVBoxLayout(members_panel)
        self.members_layout.setContentsMargins(12, 4, 12, 4)
        self.members_layout.setSpacing(0)
        layout.addWidget(members_panel)

        close_row = QHBoxLayout()
        close_row.setContentsMargins(0, 0, 0, 0)
        close_row.setSpacing(8)
        self.close_room_button = QPushButton(tr("tools.ready.close"))
        self.close_room_button.setObjectName("secondaryButton")
        self.close_room_button.setFixedHeight(32)
        self.close_room_button.clicked.connect(self._close_room)
        close_row.addWidget(self.close_room_button)
        close_row.addStretch(1)
        layout.addLayout(close_row)
        layout.addStretch(1)
        return page

    def _build_connecting_page(self) -> QWidget:
        page, layout = self._state_page()
        layout.addStretch(1)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(8)
        heading.addStretch(1)
        heading.addWidget(_Spinner())
        title = QLabel(tr("tools.connecting.title"))
        title.setObjectName("toolsCenterTitle")
        heading.addWidget(title)
        separator = QLabel("\u00b7")
        separator.setObjectName("hint")
        heading.addWidget(separator)
        self.difficulty_prefix = QLabel("")
        self.difficulty_prefix.setObjectName("toolsDifficultyText")
        heading.addWidget(self.difficulty_prefix)
        self.difficulty_pill = QLabel("")
        self.difficulty_pill.setObjectName("toolsDifficultyPill")
        heading.addWidget(self.difficulty_pill)
        self.difficulty_suffix = QLabel("")
        self.difficulty_suffix.setObjectName("toolsDifficultyText")
        heading.addWidget(self.difficulty_suffix)
        heading.addStretch(1)
        layout.addLayout(heading)

        hint = QLabel(tr("tools.connecting.hint"))
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.connecting_warning = QLabel(tr("tools.connecting.warning"))
        self.connecting_warning.setObjectName("statusWarning")
        self.connecting_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connecting_warning.setWordWrap(True)
        layout.addWidget(self.connecting_warning)

        cancel_row = QHBoxLayout()
        cancel_row.setContentsMargins(0, 0, 0, 0)
        cancel_row.setSpacing(8)
        cancel_row.addStretch(1)
        self.connecting_cancel_button = QPushButton(tr("dialog.cancel"))
        self.connecting_cancel_button.setObjectName("secondaryButton")
        self.connecting_cancel_button.setFixedHeight(32)
        self.connecting_cancel_button.clicked.connect(self._cancel)
        cancel_row.addWidget(self.connecting_cancel_button)
        cancel_row.addStretch(1)
        layout.addLayout(cancel_row)
        layout.addStretch(1)
        return page

    def _build_joined_page(self) -> QWidget:
        page, layout = self._state_page()
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(8)
        status.addWidget(
            _RoundBadge("\u2713", diameter=28, fill="ACCENT_SOFT", ink="ACCENT_TEXT", pixel_size=13)
        )
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(0)
        self.joined_title = QLabel("")
        self.joined_title.setObjectName("toolsCenterTitle")
        self.joined_hint = QLabel("")
        self.joined_hint.setObjectName("hint")
        text_box.addWidget(self.joined_title)
        text_box.addWidget(self.joined_hint)
        status.addLayout(text_box)
        status.addStretch(1)
        self.open_multiplayer_button = QPushButton(tr("tools.joined.open"))
        self.open_multiplayer_button.setFixedHeight(32)
        self.open_multiplayer_button.clicked.connect(self._open_multiplayer)
        status.addWidget(self.open_multiplayer_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(status)

        address_panel = QFrame()
        address_panel.setObjectName("toolsInnerCard")
        address_layout = QVBoxLayout(address_panel)
        address_layout.setContentsMargins(16, 12, 16, 12)
        address_layout.setSpacing(4)
        address_label = QLabel(tr("tools.joined.address_label"))
        address_label.setObjectName("hint")
        address_layout.addWidget(address_label)
        self.address_label = QLabel("")
        self.address_label.setObjectName("toolsAddressText")
        self.address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        address_layout.addWidget(self.address_label)
        layout.addWidget(address_panel)

        note = QFrame()
        note.setObjectName("toolsInfoPanel")
        note_layout = QHBoxLayout(note)
        note_layout.setContentsMargins(12, 8, 12, 8)
        note_layout.setSpacing(8)
        note_layout.addWidget(
            _RoundBadge("i", diameter=16, fill="ACCENT", ink="FG_SELECTED", pixel_size=10)
        )
        note_text = QLabel(tr("tools.joined.note"))
        note_text.setObjectName("toolsBannerText")
        note_text.setWordWrap(True)
        note_layout.addWidget(note_text, 1)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _build_failed_page(self) -> QWidget:
        page, layout = self._state_page()
        layout.addStretch(1)
        title = QLabel(tr("tasks.state.failed"))
        title.setObjectName("toolsCenterTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        self.failure_reason = QLabel("")
        self.failure_reason.setObjectName("hint")
        self.failure_reason.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.failure_reason.setWordWrap(True)
        layout.addWidget(self.failure_reason)
        retry_row = QHBoxLayout()
        retry_row.setContentsMargins(0, 0, 0, 0)
        retry_row.setSpacing(8)
        retry_row.addStretch(1)
        self.retry_button = QPushButton(tr("tasks.retry"))
        self.retry_button.setObjectName("secondaryButton")
        self.retry_button.setFixedHeight(32)
        self.retry_button.clicked.connect(self._retry)
        retry_row.addWidget(self.retry_button)
        retry_row.addStretch(1)
        layout.addLayout(retry_row)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ session wiring
    def _attach_session(self, session: LinkSession) -> None:
        self._detach_session()
        self._session = session
        self._unsubscribe = session.add_listener(self.session_changed.emit)

    def _detach_session(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_destroyed(self, *_args) -> None:
        self._detach_session()

    def set_session(self, session: LinkSession) -> None:
        """Replace the backend, for tests and the screenshot tool."""
        self._attach_session(session)
        self._refresh_instances()
        self._sync()

    def session(self) -> LinkSession:
        """The active backend, read-only for callers that want to script it."""
        return self._session

    def on_theme_applied(self) -> None:
        """The painted discs/spinner read tokens at paint time; one repaint is all they need."""
        self.update()

    # ------------------------------------------------------------------ state rendering
    def _sync(self) -> None:
        state = self._session.state()
        page = self._pages.get(state, self._failed_page)
        self.state_stack.setCurrentWidget(page)
        if state is LinkState.HOST_READY:
            self._apply_ready()
        elif state is LinkState.CONNECTING:
            self._apply_connecting()
        elif state is LinkState.JOINED:
            self._apply_joined()
        elif state is LinkState.FAILED:
            self._apply_failed()
        else:
            # The ready page is hidden; do not keep stale rows alive behind it. The next HOST_READY
            # rebuilds them from the session, which is the only source of truth.
            self._rebuild_members(())
            if (
                state is LinkState.IDLE
                and self._last_state is not LinkState.IDLE
                and self.instance_combo.count() == 0
            ):
                self._refresh_instances()
        self._update_join_enabled()
        self.create_button.setEnabled(
            state is LinkState.IDLE and bool(self.instance_combo.currentData())
        )
        self._last_state = state

    def _apply_ready(self) -> None:
        code = self._session.room_code()
        self.room_code_label.setText(code or "\u2014")
        self.copy_button.setEnabled(bool(code))
        members = self._session.members()
        online = sum(1 for member in members if member.online)
        self.members_count_label.setText(tr("tools.ready.members_count", online))
        self._rebuild_members(members)

    def _apply_connecting(self) -> None:
        difficulty = self._session.difficulty()
        if difficulty is None:
            self.difficulty_prefix.setVisible(False)
            self.difficulty_pill.setVisible(False)
            self.difficulty_suffix.setVisible(False)
            self.connecting_warning.setVisible(False)
            return
        labels = _difficulty_labels()
        index = _DIFFICULTY_INDEX.get(difficulty, 2)
        text = labels[index] if index < len(labels) else ""
        prefix, _marker, suffix = tr("tools.connecting.difficulty").partition("{}")
        self.difficulty_prefix.setText(prefix)
        self.difficulty_suffix.setText(suffix)
        self.difficulty_pill.setText(text)
        self.difficulty_pill.setProperty("level", difficulty.value)
        self.difficulty_pill.style().unpolish(self.difficulty_pill)
        self.difficulty_pill.style().polish(self.difficulty_pill)
        for widget in (self.difficulty_prefix, self.difficulty_pill, self.difficulty_suffix):
            widget.setVisible(True)
        self.connecting_warning.setVisible(difficulty is LinkDifficulty.TOUGH)

    def _apply_joined(self) -> None:
        lines = _lines("tools.joined.status")
        self.joined_title.setText(lines[0] if lines else "")
        self.joined_hint.setText(lines[1] if len(lines) > 1 else "")
        self.address_label.setText(self._session.address() or "\u2014")

    def _apply_failed(self) -> None:
        reason = self._session.failure_key()
        self.failure_reason.setText(tr(reason) if reason else "")
        self.failure_reason.setVisible(bool(reason))

    def _rebuild_members(self, members: Iterable[LinkMember]) -> None:
        while self.members_layout.count():
            item = self.members_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for member in members:
            self.members_layout.addWidget(self._member_row(member))

    @staticmethod
    def _member_row(member: LinkMember) -> QFrame:
        row = QFrame()
        row.setObjectName("toolsMemberRow")
        row.setFixedHeight(40)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        name = member.name.strip()
        layout.addWidget(
            _RoundBadge(
                member.avatar or name[:1] or "?",
                diameter=24,
                fill="ACCENT_TINT",
                ink="ACCENT_TEXT",
                pixel_size=11,
            )
        )
        name_label = QLabel(name or "?")
        name_label.setObjectName("toolsMemberName")
        layout.addWidget(name_label)
        if member.is_host:
            badge = QLabel(tr("tools.ready.host_badge"))
            badge.setObjectName("toolsHostBadge")
            layout.addWidget(badge)
        layout.addStretch(1)
        if member.online:
            dot = QLabel("\u25cf")
            dot.setObjectName("toolsOnlineDot")
            layout.addWidget(dot)
            state_label = QLabel(tr("tools.ready.online"))
            state_label.setObjectName("toolsOnlineText")
            layout.addWidget(state_label)
        return row

    # ------------------------------------------------------------------ actions
    def _download_core(self) -> None:
        if self._session.state() is not LinkState.NOT_DOWNLOADED:
            return
        self.download_button.setEnabled(False)
        self.download_progress_row.setVisible(True)
        self.download_progress_bar.setValue(0)
        try:
            self._session.download_core(self.download_progress.emit)
        except Exception:
            log.exception("link core download failed")
        finally:
            self.download_button.setEnabled(True)

    def _on_download_progress(self, value: int) -> None:
        self.download_progress_row.setVisible(True)
        self.download_progress_bar.setValue(max(0, min(PROGRESS_MAX, int(value))))

    def _join(self) -> None:
        code = self.join_edit.text().strip()
        if code and self._session.state() is LinkState.IDLE:
            self._session.join(code)

    def _paste_code(self) -> None:
        text = QApplication.clipboard().text().strip()
        if text:
            self.join_edit.setText(text)

    def _update_join_enabled(self) -> None:
        self.join_button.setEnabled(
            self._session.state() is LinkState.IDLE and bool(self.join_edit.text().strip())
        )

    def _host(self) -> None:
        instance_id = str(self.instance_combo.currentData() or "")
        if instance_id and self._session.state() is LinkState.IDLE:
            self._session.host(instance_id)

    def _refresh_instances(self) -> None:
        previous = str(self.instance_combo.currentData() or "")
        try:
            items = list(self._instances_provider())
        except Exception:
            log.exception("could not list instances for the host picker")
            items = []
        self.instance_combo.blockSignals(True)
        try:
            self.instance_combo.clear()
            if items:
                for instance_id, label in items:
                    self.instance_combo.addItem(label, instance_id)
                index = self.instance_combo.findData(previous)
                self.instance_combo.setCurrentIndex(max(index, 0))
            else:
                self.instance_combo.addItem("\u2014", "")
                self.instance_combo.setCurrentIndex(0)
        finally:
            self.instance_combo.blockSignals(False)
        self.create_button.setEnabled(
            self._session.state() is LinkState.IDLE and bool(self.instance_combo.currentData())
        )

    def _cancel(self) -> None:
        self._session.cancel()

    def _retry(self) -> None:
        self._session.cancel()

    def _close_room(self) -> None:
        self._session.close_room()

    def _copy_code(self) -> None:
        code = self._session.room_code()
        if code:
            QApplication.clipboard().setText(code)

    def _open_multiplayer(self) -> None:
        self.open_multiplayer_requested.emit(self._session.address())

    def _on_channels_toggled(self, checked: bool) -> None:
        self.channels_detail.setVisible(checked)
        self.channels_arrow.setText("\u25b4" if checked else "\u25be")

    # ------------------------------------------------------------------ test helpers / introspection
    def current_state(self) -> LinkState:
        """The state the page is currently showing (used by the screenshot driver and tests)."""
        for state, page in self._pages.items():
            if page is self.state_stack.currentWidget():
                return state
        return LinkState.FAILED

    def member_row_count(self) -> int:
        """How many member rows the ready page currently shows."""
        return self.members_layout.count()
