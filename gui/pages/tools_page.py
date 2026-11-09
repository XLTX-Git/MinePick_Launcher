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
import re
from collections.abc import Callable, Iterable

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, paint_utils, theme
from gui.widgets import (
    add_page_top_inset,
    build_download_progress,
    style_page_layout,
    widget_alive,
)
from gui.workers import run_in_background
from launcher.link import (
    LinkDifficulty,
    LinkDownloadProgress,
    LinkMember,
    LinkSession,
    LinkState,
    NatProbe,
    NatType,
    SelfCheckReport,
    StunVerdict,
    default_link_session,
    skins,
)
from launcher.link.mirrors import MIRRORS, read_report

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
#: Member avatars use the height of the ready row (40 px) minus its 8 px vertical padding.
MEMBER_AVATAR_SIZE = 24
#: A Minecraft name is ASCII by rule; the letter avatar takes its first letter or digit.
_AVATAR_LETTER = re.compile(r"[A-Za-z0-9]")
_STUN_VERDICT_INDEX = {
    StunVerdict.UNKNOWN: 0,
    StunVerdict.CONE: 1,
    StunVerdict.SYMMETRIC: 2,
    StunVerdict.BLOCKED: 3,
    StunVerdict.TIMEOUT: 4,
    StunVerdict.ERROR: 5,
}
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


def _configured_mirror() -> str:
    """The mirror saved on 工具 › 联机 (``""`` = automatic), tolerant of a missing config."""
    try:
        from launcher import config

        return str(config.load()[0].terracotta_mirror or "").strip().lower()
    except Exception:  # an unreadable config simply means "automatic"
        log.debug("无法读取下载源设置", exc_info=True)
        return ""


def _save_mirror_choice(key: str) -> None:
    """Persist the chosen mirror; an empty key is the automatic race.

    The downloader reads it when it installs the core, so this is the entire wiring of the radio
    rows: nothing is downloaded here, and a choice made after an install cannot trigger a reinstall.
    """
    try:
        from launcher import config

        cfg, path = config.load()
        if cfg.terracotta_mirror == key:
            return
        cfg.terracotta_mirror = key
        config.save(cfg, path)
    except Exception:  # a failed settings write must not break the page
        log.debug("无法保存下载源设置", exc_info=True)


def _difficulty_labels() -> list[str]:
    """The three localized difficulty names, in ``LinkDifficulty`` order."""
    labels = _lines("tools.difficulty")
    return labels if len(labels) >= 3 else ["", "", ""]


def _avatar_initial(member: LinkMember) -> str:
    """The letter avatar's character: first ASCII letter or digit, upper-cased.

    A Minecraft name is ``[A-Za-z0-9_]{3,16}``, so this only ever falls back for a value that is
    not a player name (a server name, an author) or is malformed: a CJK character or an emoji would
    otherwise become a tofu box. ``member.avatar`` is an explicit display initial; it goes through
    the same rule so a backend cannot smuggle a glyph into the circle.
    """
    for candidate in (member.avatar, member.name):
        match = _AVATAR_LETTER.search(str(candidate or ""))
        if match:
            return match.group(0).upper()
    return "?"


def _format_mb(byte_count: int) -> str:
    """One decimal place, in the decimal MB the row's example uses ("3.2 / 8.1 MB")."""
    return f"{max(0, int(byte_count)) / 1_000_000:.1f}"


def _verdict_text(verdict: StunVerdict) -> str:
    """The localized UDP verdict; index order matches ``tools.net.verdicts``."""
    lines = _lines("tools.net.verdicts")
    index = _STUN_VERDICT_INDEX.get(verdict, len(lines) - 1)
    return lines[index] if index < len(lines) else ""


def _nat_type_text(nat_type: NatType | None) -> str:
    """The EasyTier type name, indexed by its 0-8 wire code; data loss reads as 未检测."""
    if nat_type is None:
        return tr("tools.net.nat.unknown")
    labels = _lines("tools.net.nat.types")
    index = int(nat_type)
    return labels[index] if 0 <= index < len(labels) else tr("tools.net.nat.unknown")


def _nat_reason(nat: NatProbe | None) -> str:
    """The translated reason for a probe that did not return both types."""
    error = nat.error if isinstance(nat, NatProbe) else None
    key = f"tools.net.reason.{error.value}" if error is not None else "tools.net.reason.not_tested"
    return tr(key)


def _nat_tooltip(nat: NatProbe | None, *, fallback_failed: bool = False) -> str:
    """The result's source, or the concrete reason TCP/Everything is untested."""
    if not isinstance(nat, NatProbe):
        return tr("tools.net.nat.failed_tip")
    if nat.tested:
        if nat.tcp is not None:
            return tr("tools.net.nat.easytier_tip")
        return tr("tools.net.nat.tcp_tip", _nat_reason(nat))
    reason = tr("tools.net.nat.fallback_tip", _nat_reason(nat))
    if fallback_failed:
        return f"{tr('tools.net.nat.failed_tip')} {reason}"
    return reason


def _download_icon(size: int = 24) -> QIcon:
    """A white down-arrow for the primary download button.

    Painted rather than typed as U+2193: the button's 4px border and 12px padding leave a 32px
    control no room for a text glyph (measured: the arrow was clipped to nothing), and an icon
    keeps the square primary button readable at its required 32x32 size. The colour is the theme's
    ``FG_SELECTED`` token, i.e. the same ink the base ``QPushButton`` rule paints its label with.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(_tokens().get("FG_SELECTED", "#ffffff")), max(1.5, size / 10.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    center = size / 2.0
    painter.drawLine(center, size * 0.18, center, size * 0.78)
    painter.drawLine(size * 0.28, size * 0.54, center, size * 0.78)
    painter.drawLine(size * 0.72, size * 0.54, center, size * 0.78)
    painter.end()
    return QIcon(pixmap)


def _chevron_icon(expanded: bool, size: int = 10) -> QPixmap:
    """The disclosure arrow, drawn by the shared helper (no glyph fallback to a tofu box)."""
    return paint_utils.chevron_pixmap(expanded, size)


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


class _MemberAvatar(QWidget):
    """One member's 24px avatar: the real skin face for a premium UUID, a letter otherwise.

    The letter is painted first and is exactly the disc/tint/letter the ready list always showed,
    so an offline member (or a failed skin lookup) does not change size, position or colour. A
    premium member's texture is fetched through the same :mod:`gui.paint_utils` cache the account
    pages use: UUID -> session-server URL -> skin texture -> face crop, all on the worker pool. The
    widget never waits for the network; it upgrades itself when the answer arrives.
    """

    def __init__(self, member: LinkMember, diameter: int = MEMBER_AVATAR_SIZE) -> None:
        super().__init__()
        self._member = member
        self._diameter = diameter
        self._face = None
        self._skin_url = ""
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # -- paint ------------------------------------------------------------
    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._face is not None:
            path = QPainterPath()
            path.addEllipse(QRectF(self.rect()))
            painter.setClipPath(path)
            painter.drawPixmap(self.rect(), self._face)
            painter.end()
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens["ACCENT_TINT"]))
        painter.drawEllipse(self.rect())
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(tokens["ACCENT_TEXT"]))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, _avatar_initial(self._member))
        painter.end()

    # -- skin -------------------------------------------------------------
    def load_skin(self) -> None:
        """Kick off the background lookup when the member has a premium UUID."""
        member = self._member
        if not (member.premium and member.uuid):
            return  # offline, or a backend (Terracotta v0.4.2) that provides no UUID
        url = skins.cached_skin_url(member.uuid)
        if url:
            self._use_skin_url(url)
            return
        run_in_background(
            lambda: skins.resolve_skin_url(member.uuid),
            on_result=self._on_skin_url,
        )

    def _on_skin_url(self, url) -> None:
        if not widget_alive(self) or not str(url or ""):
            return
        self._use_skin_url(str(url))

    def _use_skin_url(self, url: str) -> None:
        self._skin_url = url

        def apply(source, expected: str = url) -> None:
            if source is None or not widget_alive(self) or self._skin_url != expected:
                return
            face = paint_utils.skin_face_pixmap(source, self._diameter)
            if face is None:
                return  # not a skin texture: the letter stays
            self._face = face
            self.update()

        if not paint_utils.watch_skin(url, apply):
            return  # already cached (painted above) or another download will call back
        run_in_background(
            lambda: self._download_skin(url),
            on_result=lambda data: paint_utils.finish_skin(url, data),
            on_error=lambda _message: paint_utils.finish_skin(url, None),
        )

    @staticmethod
    def _download_skin(url: str) -> bytes:
        from launcher.meta.manifest import _new_client

        client = _new_client()
        try:
            response = client.get(url)
            response.raise_for_status()
            return response.content
        finally:
            client.close()


class ToolsPage(QWidget):
    """工具 › 联机: the page depends only on the :class:`LinkSession` interface."""

    #: Emitted from the session listener. A queued connection is what makes a worker-thread backend
    #: safe later; with the fake everything happens on the GUI thread and is delivered immediately.
    session_changed = Signal()
    #: The progress callback is itself a signal so a real downloader may report from a worker.
    download_progress = Signal(object)
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
        self._disposed = False
        self._downloading = False
        self._download_failure = ""
        self._checking = False
        self._selected_channel = ""

        self._build_ui()
        self.session_changed.connect(self._sync)
        self.download_progress.connect(self._on_download_progress)
        # A bound method of the page itself is not called for its own `destroyed` signal: Qt has
        # already severed the object's connections by then (measured with PySide6 6.11). The lambda
        # is a plain callable, so it still runs and can stop the poller before the C++ object goes.
        self.destroyed.connect(lambda *_args: self._dispose_session())
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

        tab_layout.addWidget(self._build_banner_block())
        self.state_stack = QStackedWidget()

        self._pages[LinkState.NOT_DOWNLOADED] = self._build_onboarding_page()
        self._pages[LinkState.IDLE] = self._build_idle_page()
        self._pages[LinkState.HOST_WAITING] = self._build_host_waiting_page()
        self._pages[LinkState.HOST_READY] = self._build_host_ready_page()
        self._pages[LinkState.CONNECTING] = self._build_connecting_page()
        self._pages[LinkState.JOINED] = self._build_joined_page()
        self._failed_page = self._build_failed_page()
        for page in (*self._pages.values(), self._failed_page):
            self.state_stack.addWidget(page)

        # The content column is the full tab width again: the network check moved into the
        # in-window dialog opened from the banner, so no diagnostic panel competes with the cards.
        tab_layout.addWidget(self.state_stack, 1)

        self.tabs.addTab(tab_page, tr("tools.tab.link"))
        card_layout.addWidget(self.tabs, 1)
        card_layout.addWidget(self._build_footer())
        outer.addWidget(card, 1)

    def _build_banner_block(self) -> QWidget:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._build_banner())
        self.self_check_line = QLabel()
        self.self_check_line.setObjectName("hint")
        self.self_check_line.setContentsMargins(36, 0, 0, 0)
        self.self_check_line.hide()
        layout.addWidget(self.self_check_line)
        return block

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
        self.self_check_button = QPushButton(tr("tools.net.title"))
        self.self_check_button.setObjectName("secondaryButton")
        self.self_check_button.setFixedHeight(26)
        self.self_check_button.setAccessibleName(tr("tools.net.title"))
        self.self_check_button.setToolTip(tr("tools.net.nat.note"))
        self.self_check_button.clicked.connect(self._run_self_check)
        layout.addWidget(self.self_check_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return banner

    def _run_self_check(self) -> None:
        """Probe EasyTier's UDP+TCP types and put both results on one line under the banner."""
        if self._checking:
            return
        runner = getattr(self._session, "self_check", None)
        if not callable(runner):
            self._on_self_check_ready(None, self._session)
            return
        self._checking = True
        session = self._session
        self.self_check_button.setEnabled(False)
        self.self_check_button.setText(tr("tools.net.checking"))
        self._show_self_check_line(
            tr("tools.net.checking"),
            tr("tools.net.checking"),
            tr("tools.net.nat.checking_tip"),
        )
        run_in_background(
            runner,
            on_result=lambda report: self._on_self_check_ready(report, session),
            on_error=lambda _message: self._on_self_check_ready(None, session),
        )

    def _show_self_check_line(self, udp: str, tcp: str, tooltip: str) -> None:
        self.self_check_line.setText(tr("tools.net.nat.line", udp, tcp))
        self.self_check_line.setToolTip(tooltip)
        self.self_check_line.show()

    def _on_self_check_ready(self, report, session) -> None:
        if session is not self._session or not widget_alive(self):
            return
        self._checking = False
        self.self_check_button.setEnabled(True)
        self.self_check_button.setText(tr("tools.net.refresh"))
        nat = report.nat if isinstance(report, SelfCheckReport) else None
        if isinstance(nat, NatProbe) and nat.tested:
            self._show_self_check_line(
                _nat_type_text(nat.udp),
                _nat_type_text(nat.tcp),
                _nat_tooltip(nat),
            )
            return
        unknown = tr("tools.net.nat.unknown")
        verdict = report.stun if isinstance(report, SelfCheckReport) else StunVerdict.UNKNOWN
        fallback_failed = verdict in (StunVerdict.UNKNOWN, StunVerdict.ERROR)
        udp = unknown if fallback_failed else _verdict_text(verdict)
        self._show_self_check_line(
            udp,
            unknown,
            _nat_tooltip(nat, fallback_failed=fallback_failed and isinstance(nat, NatProbe)),
        )

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
        self.download_status = QLabel(tr("tools.onboard.download_hint"))
        self.download_status.setObjectName("hint")
        self.download_status.setWordWrap(True)
        text_box.addWidget(row_title)
        text_box.addWidget(self.download_status)
        self.download_progress_row, self.download_progress_bar, _ = build_download_progress(
            "linkCoreProgress"
        )
        self.download_progress_bar.setRange(0, PROGRESS_MAX)
        self.download_progress_bar.setValue(0)
        self.download_progress_bar.setAccessibleName(tr("tools.onboard.download"))
        self.download_progress_row.setMinimumWidth(220)
        self.download_progress_row.setMaximumWidth(340)
        row_layout.addLayout(text_box, 1)
        row_layout.addWidget(self.download_progress_row, 0, Qt.AlignmentFlag.AlignVCenter)

        self.download_button = QPushButton()
        # Deliberately no objectName: the base QPushButton rule is the primary (filled) style, and
        # #secondaryButton is the outlined variant. The call to action is the primary one.
        self.download_button.setFixedSize(32, 32)
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.setAccessibleName(tr("tools.onboard.download"))
        self.download_button.setIcon(_download_icon())
        self.download_button.setIconSize(QSize(16, 16))
        self.download_button.clicked.connect(self._download_core)
        row_layout.addWidget(self.download_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(row)

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
        self.channels_arrow = QLabel()
        self.channels_arrow.setObjectName("toolsDisclosureArrow")
        self.channels_arrow.setFixedSize(16, 16)
        self.channels_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.channels_arrow.setPixmap(_chevron_icon(False))
        channels_layout.addWidget(channels_label)
        channels_layout.addStretch(1)
        channels_layout.addWidget(self.channels_arrow)
        self.channels_button.toggled.connect(self._on_channels_toggled)
        layout.addWidget(self.channels_button)

        self.channels_detail = self._build_channels()
        self.channels_detail.setVisible(False)
        layout.addWidget(self.channels_detail)
        layout.addStretch(1)
        return page

    def _build_channels(self) -> QWidget:
        """The automatic choice plus the three official mirrors, with their last measurement.

        The selected row is persisted in :attr:`launcher.config.LauncherConfig.terracotta_mirror`;
        the downloader reads it at download time and uses the chosen mirror as its preferred one.
        The automatic row is exactly the historical race. Toggling a row never starts a download:
        if the core is already installed, the choice only applies to the next install.
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.channel_buttons: dict[str, QRadioButton] = {}
        self.channel_speed_labels: dict[str, QLabel] = {}
        group = QButtonGroup(box)
        group.setExclusive(True)
        auto_radio = QRadioButton(tr("tools.onboard.channels.auto"))
        auto_radio.setAccessibleName(tr("tools.onboard.channels.auto"))
        auto_radio.toggled.connect(lambda checked: self._on_channel_toggled("", checked))
        auto_hint = QLabel("")  # the automatic row has no single mirror to measure
        auto_hint.setObjectName("hint")
        group.addButton(auto_radio)
        self.channel_buttons[""] = auto_radio
        auto_row = QWidget()
        auto_layout = QHBoxLayout(auto_row)
        auto_layout.setContentsMargins(0, 0, 0, 0)
        auto_layout.setSpacing(8)
        auto_layout.addWidget(auto_radio)
        auto_layout.addStretch(1)
        auto_layout.addWidget(auto_hint)
        layout.addWidget(auto_row)
        for mirror in MIRRORS:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            radio = QRadioButton(mirror.label)
            radio.setAccessibleName(mirror.label)
            radio.toggled.connect(
                lambda checked, key=mirror.key: self._on_channel_toggled(key, checked)
            )
            speed = QLabel(tr("tools.onboard.channels.unmeasured"))
            speed.setObjectName("hint")
            speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            group.addButton(radio)
            self.channel_buttons[mirror.key] = radio
            self.channel_speed_labels[mirror.key] = speed
            row_layout.addWidget(radio)
            row_layout.addStretch(1)
            row_layout.addWidget(speed)
            layout.addWidget(row)
        footer = QLabel(tr("tools.onboard.channels.footer"))
        footer.setObjectName("hint")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        self._refresh_channel_speeds()
        self._select_saved_channel()
        return box

    def _select_saved_channel(self) -> None:
        """Check the persisted mirror, or 自动, without treating it as a new user choice."""
        saved = _configured_mirror()
        key = saved if saved in self.channel_speed_labels else ""
        for radio in self.channel_buttons.values():
            radio.blockSignals(True)
        self.channel_buttons[key].setChecked(True)
        for radio in self.channel_buttons.values():
            radio.blockSignals(False)
        self._selected_channel = key


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
        self.wait_title = QLabel(tr("tools.wait.title"))
        self.wait_title.setObjectName("toolsCenterTitle")
        heading.addWidget(self.wait_title)
        heading.addStretch(1)
        layout.addLayout(heading)

        self.wait_hint = QLabel(tr("tools.wait.hint"))
        self.wait_hint.setObjectName("hint")
        self.wait_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.wait_hint.setWordWrap(True)
        layout.addWidget(self.wait_hint)

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
        self._unsubscribe = session.add_listener(self._emit_session_changed)

    def _emit_session_changed(self) -> None:
        """A session may notify from a worker thread; the signal queues it onto the GUI thread."""
        if self._disposed:
            return
        try:
            self.session_changed.emit()
        except RuntimeError:
            pass  # the page was destroyed between the notification and this call

    def _detach_session(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def set_session(self, session: LinkSession) -> None:
        """Replace the backend, for tests and the screenshot tool."""
        previous = getattr(self, "_session", None)
        if previous is not None and previous is not session:
            dispose = getattr(previous, "dispose", None)
            if callable(dispose):
                try:
                    dispose()
                except Exception:
                    log.debug("停止旧联机会话失败", exc_info=True)
        self._disposed = False
        self._downloading = False
        self._download_failure = ""
        self._checking = False
        if hasattr(self, "self_check_line"):
            self.self_check_line.hide()
            self.self_check_line.clear()
            self.self_check_line.setToolTip("")
            self.self_check_button.setText(tr("tools.net.title"))
            self.self_check_button.setEnabled(True)
        self._attach_session(session)
        self._refresh_instances()
        self._sync()

    def session(self) -> LinkSession:
        """The active backend, read-only for callers that want to script it."""
        return self._session

    def on_theme_applied(self) -> None:
        """The painted discs/spinner read tokens at paint time; one repaint is all they need."""
        self.update()
        if self.download_button.text():
            return  # the retry label is text, not a painted icon
        self.download_button.setIcon(_download_icon())
        self.channels_arrow.setPixmap(_chevron_icon(self.channels_button.isChecked()))

    # ------------------------------------------------------------------ state rendering
    def _sync(self) -> None:
        state = self._session.state()
        page = self._pages.get(state, self._failed_page)
        self.state_stack.setCurrentWidget(page)
        if state is LinkState.HOST_READY:
            self._apply_ready()
        elif state is LinkState.HOST_WAITING:
            self._apply_waiting()
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
        self._sync_download_row(state)
        self._refresh_channel_speeds()
        self._update_join_enabled()
        self.create_button.setEnabled(
            state is LinkState.IDLE and bool(self.instance_combo.currentData())
        )
        self._last_state = state

    def _apply_waiting(self) -> None:
        """``host-starting`` is the same layout as ``host-scanning`` with a different title."""
        phase = ""
        getter = getattr(self._session, "phase", None)
        if callable(getter):
            phase = str(getter() or "")
        self.wait_title.setText(
            tr("tools.wait.starting") if phase == "host-starting" else tr("tools.wait.title")
        )

    def _sync_download_row(self, state: LinkState) -> None:
        """Keep the onboarding row truthful: progress while transferring, retry after a failure."""
        if state is not LinkState.NOT_DOWNLOADED:
            if self._downloading or self._download_failure:
                self._reset_download_row()
            return
        failure = self._session.failure_key()
        if self._downloading and failure:
            self._show_download_failure(failure)
        elif self._download_failure and not self._downloading:
            # A failure stays on screen with its retry button until the user retries.
            self.download_button.setEnabled(True)

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
        avatar = _MemberAvatar(member)
        layout.addWidget(avatar)
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
        # The letter is already painted; a premium UUID upgrades it when the texture arrives.
        avatar.load_skin()
        return row

    # ------------------------------------------------------------------ actions
    def _download_core(self) -> None:
        if self._session.state() is not LinkState.NOT_DOWNLOADED:
            return
        if self._downloading:
            return
        self._downloading = True
        self._download_failure = ""
        self._show_download_progress(LinkDownloadProgress(permille=0))
        self.download_button.setEnabled(False)
        self.download_button.setVisible(False)
        try:
            self._session.download_core(self._emit_download_progress)
        except Exception:
            log.exception("link core download failed")
            self._show_download_failure("tools.fail.download")

    def _emit_download_progress(self, progress: LinkDownloadProgress) -> None:
        """Called from the download thread; the signal carries the value to the GUI thread."""
        if self._disposed:
            return
        try:
            self.download_progress.emit(progress)
        except RuntimeError:
            pass  # the page was destroyed while the download was running

    def _show_download_progress(self, progress: LinkDownloadProgress) -> None:
        self.download_progress_row.setVisible(True)
        self.download_progress_bar.setValue(max(0, min(PROGRESS_MAX, int(progress.permille))))
        if progress.total_bytes > 0:
            self.download_status.setText(
                tr(
                    "tools.onboard.downloading",
                    _format_mb(progress.done_bytes),
                    _format_mb(progress.total_bytes),
                )
            )

    def _on_download_progress(self, value) -> None:
        if isinstance(value, LinkDownloadProgress):
            progress = value
        elif isinstance(value, (int, float)):
            progress = LinkDownloadProgress(permille=int(value))
        else:
            return
        if not self._downloading:
            return
        self._show_download_progress(progress)

    def _show_download_failure(self, key: str) -> None:
        """Show the reason and a retry control in the same row; never a dialog."""
        self._downloading = False
        self._download_failure = str(key or "tools.fail.download")
        self.download_progress_row.setVisible(False)
        self.download_button.setVisible(True)
        self.download_button.setEnabled(True)
        self.download_button.setText(tr("tasks.retry"))
        self.download_button.setIcon(QIcon())
        self.download_button.setFixedSize(64, 32)
        self.download_button.setAccessibleName(tr("tasks.retry"))
        self.download_status.setText(tr(self._download_failure))
        self._set_status_level("statusWarning")

    def _reset_download_row(self) -> None:
        self._downloading = False
        self._download_failure = ""
        self.download_progress_row.setVisible(False)
        self.download_progress_bar.setValue(0)
        self.download_button.setVisible(True)
        self.download_button.setEnabled(True)
        self.download_button.setText("")
        self.download_button.setIcon(_download_icon())
        self.download_button.setIconSize(QSize(16, 16))
        self.download_button.setFixedSize(32, 32)
        self.download_button.setAccessibleName(tr("tools.onboard.download"))
        self.download_status.setText(tr("tools.onboard.download_hint"))
        self._set_status_level("hint")

    def _set_status_level(self, object_name: str) -> None:
        self.download_status.setObjectName(object_name)
        style = self.download_status.style()
        if style is not None:
            style.unpolish(self.download_status)
            style.polish(self.download_status)

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
        self.channels_arrow.setPixmap(_chevron_icon(checked))
        if checked:
            self._refresh_channel_speeds()

    def _on_channel_toggled(self, key: str, checked: bool) -> None:
        """Persist the chosen source; the next download uses it, nothing is downloaded now."""
        if not checked:
            return
        self._selected_channel = key if key in self.channel_buttons else ""
        _save_mirror_choice(self._selected_channel)

    def _refresh_channel_speeds(self) -> None:
        """Show the last measured latency per mirror, or 未测 when no race has run yet."""
        report = read_report()
        for key, label in self.channel_speed_labels.items():
            latency = report.latency(key)
            if latency is None:
                label.setText(tr("tools.onboard.channels.unmeasured"))
            else:
                label.setText(tr("tools.onboard.channels.latency", latency))

    # ------------------------------------------------------------------ lifecycle
    def cancel_running_task(self) -> None:
        """Language switch: drop the poller, keep the room (the process is detached)."""
        self._dispose_session()

    def stop_background_work(self) -> None:
        """Window close: stop polling; the detached Terracotta process keeps its room."""
        self._dispose_session()

    def _dispose_session(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._detach_session()
        dispose = getattr(self._session, "dispose", None)
        if callable(dispose):
            try:
                dispose()
            except Exception:
                log.debug("停止联机会话失败", exc_info=True)


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
