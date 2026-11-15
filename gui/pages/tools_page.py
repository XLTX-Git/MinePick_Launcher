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

The page owns no room logic and, since the room indicator work, it does not own the session either.
It renders the :class:`launcher.link.session.LinkSession` held by a
:class:`launcher.link.bus.LinkSessionBus` and forwards user actions to it; the bus (owned by the
window, or the process-wide default for a lone page) survives page rebuilds, so a language switch
cannot stop the poller. The session's listener reaches the page through
:attr:`ToolsPage.session_changed`, a queued Qt signal, so a worker-thread backend is safe.

Every colour, radius and spacing in the page-level stylesheet comes from the existing theme tokens
(``gui/resources/style.qss`` and ``style_light.qss``). The small painted widgets read the same tokens
at paint time instead of carrying their own colours.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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

from gui import i18n, inwindow_dialog, paint_utils, theme
from gui.widgets import (
    ElidedLabel,
    add_page_top_inset,
    build_download_progress,
    set_app_status,
    style_page_layout,
    widget_alive,
)
from gui.workers import has_running_game, run_in_background
from launcher.link import (
    LinkDifficulty,
    LinkDownloadProgress,
    LinkMember,
    LinkSession,
    LinkSessionBus,
    LinkState,
    NatProbe,
    NatType,
    SelfCheckReport,
    StunVerdict,
    default_link_bus,
    default_link_session,
)
from launcher.link.components import ComponentError
from launcher.link.mirrors import MIRRORS, read_report

tr = i18n.tr
log = logging.getLogger(__name__)

#: Same scale as every other download in the launcher: 0..1000, a tenth of a percent a step.
PROGRESS_MAX = 1000
#: The 8px step of the page's 4/8 spacing ladder. The banner's vertical padding uses it, so the bar
#: is one text line + 8px above and below (or the 35px self-check button + 16px, whichever is taller).
BANNER_VERTICAL_PADDING = 8
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
    download_progress = Signal(object)
    #: One mirror probe answered: (key, latency in ms or None = timed out). The mirror race calls its
    #: listener from a daemon thread, so this signal is the hop back to the GUI thread.
    channel_latency = Signal(str, object)
    #: Reserved for the next change: opening multiplayer needs a real launch path.
    open_multiplayer_requested = Signal(str)

    def __init__(
        self,
        session: LinkSession | None = None,
        *,
        bus: LinkSessionBus | None = None,
        session_factory: Callable[[], LinkSession] = default_link_session,
        game_running_provider: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("toolsPage")
        # Ownership: the bus owns the session, never this page. The window passes its own bus, so a
        # rebuild (language switch) or a destroyed page can only unsubscribe; a test/screenshot that
        # passes a session gets a private bus so the session still outlives the page. A page built
        # without either falls back to the process-wide bus (see launcher.link.bus).
        if bus is not None:
            self._bus = bus
        elif session is not None:
            self._bus = LinkSessionBus(session_factory=lambda: session)
        elif session_factory is not default_link_session:
            self._bus = LinkSessionBus(session_factory=session_factory)
        else:
            self._bus = default_link_bus()
        self._session = self._bus.session()
        #: Whether a game process is running, asked once per waiting-state repaint. It is the
        #: launcher's own process registry by default, not a new monitor: a game started elsewhere is
        #: simply not visible here, which is why the hint says "not detected" and keeps the generic
        #: instruction as the fallback step.
        self._game_running_provider = game_running_provider or has_running_game
        self._unsubscribe: Callable[[], None] | None = None
        self._pages: dict[LinkState, QWidget] = {}
        self._disposed = False
        self._downloading = False
        self._download_failure = ""
        self._checking = False
        self._disabling = False
        self._selected_channel = ""

        self._build_ui()
        self.session_changed.connect(self._sync)
        self.download_progress.connect(self._on_download_progress)
        self.channel_latency.connect(self._on_channel_latency)
        # A bound method of the page itself is not called for its own `destroyed` signal: Qt has
        # already severed the object's connections by then (measured with PySide6 6.11). The lambda
        # is a plain callable, so it still runs and can stop observing before the C++ object goes.
        # It must not dispose the session: that belongs to the bus.
        self.destroyed.connect(lambda *_args: self._release())
        self._attach_bus()
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
        # The buttons on this page now grow to their own QSS minimum (35px for an outlined one),
        # which is 9px more than the old fixed 26/32 boxes. The height comes back from the card's own
        # vertical padding (8 -> 4) and gap (8 -> 4): both are on the 4/8 ladder, and neither moves
        # the banner, the cards or the footer horizontally.
        card_layout.setContentsMargins(16, 4, 16, 4)
        card_layout.setSpacing(4)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("toolsTabs")
        self.tabs.setAccessibleName(tr("tools.tab.link"))
        self.tabs.tabBar().setExpanding(False)
        # One tab and no room to grow new ones: the scroll-button reservation is what raised the tab
        # bar's own minimum width to 123px and made a width audit call the 48px single tab squeezed.
        # Without it the bar's minimum is its one tab (48px), which is what it actually has.
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.setDocumentMode(False)

        tab_page = QWidget()
        tab_layout = QVBoxLayout(tab_page)
        # No 4px top inset: the tab bar's 2px underline already separates it from the banner, and the
        # card's own padding supplies the rest. This is one of the 16px reclaimed for the taller
        # buttons (see card_layout above); the page must fit 610px without a scrollbar.
        tab_layout.setContentsMargins(0, 0, 0, 0)
        # 4px, not 8: the banner and the two cards already own their inner gaps; this one only
        # separates the banner block from the state card. Together with the footer's 4px top it
        # reclaims the last 8px the taller buttons took, so 988x599 also fits without scrolling.
        tab_layout.setSpacing(4)

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
        return block

    def _build_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("toolsBanner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(12, BANNER_VERTICAL_PADDING, 12, BANNER_VERTICAL_PADDING)
        layout.setSpacing(8)
        layout.addWidget(_RoundBadge("i", diameter=16, fill="ACCENT", ink="FG_SELECTED", pixel_size=10))
        # The hint is the part that gives way on a narrow window: an elided one-line label (not a
        # word-wrapped one) keeps the banner one row tall and lets the measured result - which must
        # stay readable - keep its full width to the right.
        text = ElidedLabel(tr("tools.banner"), elide_mode=Qt.TextElideMode.ElideRight)
        text.setObjectName("toolsBannerText")
        # A read-only sentence: it must not become a keyboard stop. ElidedLabel defaults to a focus
        # policy, which put a stop in the walkthrough with no :focus rule to draw for it.
        text.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(text, 1)
        # UDP/TCP results live inside the banner now, in the blank space before the button. Empty
        # until a check has run, in which case it takes no width and changes no geometry.
        self.self_check_result = QLabel("")
        self.self_check_result.setObjectName("hint")
        self.self_check_result.setWordWrap(False)
        self.self_check_result.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.self_check_result, 0, Qt.AlignmentFlag.AlignVCenter)
        self.self_check_button = QPushButton(tr("tools.net.title"))
        self.self_check_button.setObjectName("secondaryButton")
        # No fixed height: #secondaryButton carries `padding: 8px 16px` and a 1px border, so the QSS
        # itself asks for 35px at the 13px UI font. The old 26px box cut the button's text; leaving
        # the height to minimumSizeHint also lets a taller font grow it instead of clipping again.
        self.self_check_button.setAccessibleName(tr("tools.net.title"))
        self.self_check_button.setToolTip(tr("tools.net.nat.note"))
        self.self_check_button.clicked.connect(self._run_self_check)
        layout.addWidget(self.self_check_button, 0, Qt.AlignmentFlag.AlignVCenter)
        # The floor is the button, not the text: 35px + 8px top + 8px bottom. A shorter bar would
        # squash the button, which is exactly the defect the old 26px fixed height caused.
        banner.setMinimumHeight(
            self.self_check_button.minimumSizeHint().height() + 2 * BANNER_VERTICAL_PADDING
        )
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
        self._show_self_check_result(
            tr("tools.net.checking"),
            tr("tools.net.checking"),
            tr("tools.net.nat.checking_tip"),
        )
        run_in_background(
            runner,
            on_result=lambda report: self._on_self_check_ready(report, session),
            on_error=lambda _message: self._on_self_check_ready(None, session),
        )

    def _show_self_check_result(self, udp: str, tcp: str, tooltip: str) -> None:
        """Write the result into the banner's blank space; the old line below it is gone."""
        self.self_check_result.setText(tr("tools.net.nat.line", udp, tcp))
        self.self_check_result.setToolTip(tooltip)

    def _on_self_check_ready(self, report, session) -> None:
        if session is not self._session or not widget_alive(self):
            return
        self._checking = False
        self.self_check_button.setEnabled(True)
        self.self_check_button.setText(tr("tools.net.refresh"))
        nat = report.nat if isinstance(report, SelfCheckReport) else None
        if isinstance(nat, NatProbe) and nat.tested:
            self._show_self_check_result(
                _nat_type_text(nat.udp),
                _nat_type_text(nat.tcp),
                _nat_tooltip(nat),
            )
            return
        unknown = tr("tools.net.nat.unknown")
        verdict = report.stun if isinstance(report, SelfCheckReport) else StunVerdict.UNKNOWN
        fallback_failed = verdict in (StunVerdict.UNKNOWN, StunVerdict.ERROR)
        udp = unknown if fallback_failed else _verdict_text(verdict)
        self._show_self_check_result(
            udp,
            unknown,
            _nat_tooltip(nat, fallback_failed=fallback_failed and isinstance(nat, NatProbe)),
        )

    def _build_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("toolsFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        provided = QLabel(tr("tools.footer.provided"))
        provided.setObjectName("toolsFooterText")
        # A button, not a label: 停用联机功能 deletes the downloaded payload, so it has to be a real
        # control - reachable with Tab, announced as a button, and it must not look clickable while
        # doing nothing (which is what the label it used to be did).
        self.disable_link = QPushButton(tr("tools.footer.disable"))
        self.disable_link.setObjectName("toolsFooterLink")
        self.disable_link.setAccessibleName(tr("tools.footer.disable"))
        self.disable_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.disable_link.clicked.connect(self._disable_link)
        layout.addWidget(provided)
        layout.addStretch(1)
        layout.addWidget(self.disable_link)
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
        # Icon-only square: the base rule's 12px horizontal padding exists to pad a label, and it
        # inflates the button's minimum width to 52px for a 16px glyph. The fixed 32px box is the
        # design; zeroing the padding makes the control's own minimum agree with it instead of
        # painting an icon into a box the style says is too small.
        self.download_button.setStyleSheet("padding: 0;")
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
        self.clear_button = QPushButton(tr("tools.join.clear"))
        self.clear_button.setObjectName("secondaryButton")
        self.paste_button = QPushButton(tr("tools.join.paste"))
        self.paste_button.setObjectName("secondaryButton")
        self.join_button = QPushButton(tr("tools.join.submit"))
        # No explicit heights on these: #secondaryButton's QSS padding and the base button rule decide
        # their minimum (35px / 33px at the 13px font), and the row lets the tallest control set the
        # height. A 32px floor would sit below that minimum and the layout would compress them again.
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
        # No instance picker: a P2P room does not depend on which instance started the world, and
        # Terracotta finds the open LAN world by itself. The line says so, so its absence reads as a
        # decision rather than a missing control.
        self.host_auto_hint = QLabel(tr("tools.host.auto"))
        self.host_auto_hint.setObjectName("hint")
        self.host_auto_hint.setWordWrap(True)
        host_layout.addWidget(self.host_auto_hint)
        self.create_button = QPushButton(tr("tools.host.submit"))
        # Same as the join row: let the QSS-derived minimum decide, so the font can grow the button.
        host_row = QHBoxLayout()
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.setSpacing(8)
        host_row.addStretch(1)
        host_row.addWidget(self.create_button)
        host_layout.addLayout(host_row)
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
        note_text.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self.retry_button.clicked.connect(self._retry)
        retry_row.addWidget(self.retry_button)
        retry_row.addStretch(1)
        layout.addLayout(retry_row)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ session wiring
    def _attach_bus(self) -> None:
        self._detach_bus()
        self._unsubscribe = self._bus.add_listener(self._on_bus_changed)

    def _on_bus_changed(self) -> None:
        """Follow a bus that swapped or dropped its session, then repaint from the session.

        The page keeps a reference for its own reads, but the bus owns it: a replacement is adopted
        here, and a bus without a session (shutdown) is ignored rather than crashed on.
        """
        if self._disposed:
            return
        session = self._bus.peek_session()
        if session is None:
            return
        if session is not self._session:
            self._session = session
        self._emit_session_changed()

    def _emit_session_changed(self) -> None:
        """A session may notify from a worker thread; the signal queues it onto the GUI thread."""
        if self._disposed:
            return
        try:
            self.session_changed.emit()
        except RuntimeError:
            pass  # the page was destroyed between the notification and this call

    def _detach_bus(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _release(self) -> None:
        """Stop observing. The bus keeps the session and its poller alive; the room stays open."""
        if self._disposed:
            return
        self._disposed = True
        self._detach_bus()

    def set_session(self, session: LinkSession) -> None:
        """Replace the observed backend, for tests and the screenshot tool.

        The bus is the owner, so the swap goes through it: the window's room indicator and this page
        keep reading the same session. The previous session is not disposed here.
        """
        self._disposed = False
        self._downloading = False
        self._download_failure = ""
        self._checking = False
        if hasattr(self, "self_check_result"):
            self.self_check_result.clear()
            self.self_check_result.setToolTip("")
            self.self_check_button.setText(tr("tools.net.title"))
            self.self_check_button.setEnabled(True)
        self._attach_bus()
        self._bus.replace_session(session)
        self._session = session
        self._sync()

    def session(self) -> LinkSession:
        """The active backend, read-only for callers that want to script it."""
        return self._session

    def instance_bus(self) -> LinkSessionBus:
        """The bus this page observes (the window's, or a private one for a supplied session)."""
        return self._bus

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
        self._sync_download_row(state)
        self._refresh_channel_speeds()
        self._update_join_enabled()
        self.create_button.setEnabled(state is LinkState.IDLE)

    def _apply_waiting(self) -> None:
        """``host-starting`` is the same layout as ``host-scanning`` with a different title."""
        phase = ""
        getter = getattr(self._session, "phase", None)
        if callable(getter):
            phase = str(getter() or "")
        self.wait_title.setText(
            tr("tools.wait.starting") if phase == "host-starting" else tr("tools.wait.title")
        )
        # "Waiting for you to open LAN" assumes there is a game to open it in. The detection is one
        # cheap read of the launcher's own game-process registry per state change (no timer, no
        # process-table scan); a game started outside this launcher is not visible, so the wording
        # says "not detected" and keeps the LAN instruction as the thing to do next.
        running = self._detect_game_running()
        if phase == "host-starting":
            # Terracotta already found the LAN world, so a game is running no matter what the
            # registry says (the world may have been opened from another launcher).
            running = True
        self.wait_hint.setText(tr("tools.wait.hint") if running else tr("tools.wait.no_game"))

    def _detect_game_running(self) -> bool:
        """Whether a game process is running; an unreadable answer keeps the neutral hint."""
        try:
            return bool(self._game_running_provider())
        except Exception:  # a broken detector must not turn a normal wait into an error page
            log.debug("无法检测正在运行的游戏进程", exc_info=True)
            return True

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
        # No avatar. The room reports a name and nothing else - no UUID, no premium flag - so a face
        # here would be a guess presented as a fact. The name is what the person chose to send.
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
        if self._downloading:
            return
        self._downloading = True
        self._download_failure = ""
        self._show_download_progress(LinkDownloadProgress(permille=0))
        self.download_button.setEnabled(False)
        self.download_button.setVisible(False)
        try:
            self._session.download_core(
                self._emit_download_progress,
                on_latency=self._emit_channel_latency,
            )
        except Exception:
            log.exception("link core download failed")
            self._show_download_failure("tools.fail.download")

    def _emit_channel_latency(self, key: str, latency_ms: int | None) -> None:
        """Called from the mirror race thread(s): hop to the GUI thread with a signal."""
        if self._disposed:
            return
        try:
            self.channel_latency.emit(str(key), latency_ms)
        except RuntimeError:
            pass  # the page was destroyed while the probes were still running

    def _on_channel_latency(self, key: str, latency_ms) -> None:
        """One channel's row gets its result: a measured latency, or 超时 for a probe that failed."""
        label = self.channel_speed_labels.get(str(key))
        if label is None:
            return
        if latency_ms is None:
            label.setText(tr("tools.onboard.channels.timeout"))
            return
        try:
            label.setText(tr("tools.onboard.channels.latency", int(latency_ms)))
        except (TypeError, ValueError):
            label.setText(tr("tools.onboard.channels.timeout"))

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
        """Create a room; the host instance is found by Terracotta's own LAN scan.

        The session interface still takes an instance id, but a P2P room does not depend on it: the
        two sides only have to run the same version, so the page passes the empty string rather than
        changing what ``LinkSession.host`` means.
        """
        if self._session.state() is not LinkState.IDLE:
            return
        self._session.host("")

    def _cancel(self) -> None:
        self._session.cancel()

    def _retry(self) -> None:
        self._session.cancel()

    def _close_room(self) -> None:
        """Close the room for real: the tunnel ends, guests drop, our core stops.

        Closing is what a person does when they want to play alone, so it does not leave an empty room
        behind. When somebody else is still in it the click asks first - the guests are about to be
        disconnected and that is not a thing to discover afterwards.
        """
        others = [member for member in self._session.members() if not member.is_host]
        if others and not inwindow_dialog.confirm(
            self,
            tr("tools.ready.close_confirm.title"),
            tr("tools.ready.close_confirm.body", len(others)),
            accept_text=tr("tools.ready.close_confirm.accept"),
            reject_text=tr("dialog.cancel"),
            default=inwindow_dialog.Outcome.REJECTED,
            severity="warning",
        ):
            return
        self._session.close_room()

    def _disable_link_dialog(self) -> inwindow_dialog.InWindowMessage:
        """The 停用联机功能 confirmation; the render tool shows this exact card.

        Built here rather than inline so the picture and the shipped question cannot drift apart.
        The safe answer is the default one: Enter cancels, and only the warning button deletes.
        """
        return inwindow_dialog.InWindowMessage(
            self,
            tr("tools.disable.title"),
            tr("tools.disable.message"),
            severity="warning",
            buttons=[
                (tr("dialog.cancel"), inwindow_dialog.Outcome.REJECTED),
                (tr("tools.disable.confirm"), inwindow_dialog.Outcome.ACCEPTED),
            ],
            default=inwindow_dialog.Outcome.REJECTED,
        )

    def _disable_link_locked_dialog(self) -> inwindow_dialog.InWindowMessage:
        """The second confirmation, shown only when a running core holds its executable open.

        Built here for the same reason as the first card: the render tool and the shipped question
        are the same object. The safe answer is again the default - Enter cancels - and only the
        explicit button ends a process. The body has to name both consequences: another program
        using the shared core loses its room too, and the component has to be downloaded again.
        """
        return inwindow_dialog.InWindowMessage(
            self,
            tr("tools.disable.locked.title"),
            tr("tools.disable.locked.message"),
            severity="warning",
            buttons=[
                (tr("dialog.cancel"), inwindow_dialog.Outcome.REJECTED),
                (tr("tools.disable.locked.confirm"), inwindow_dialog.Outcome.ACCEPTED),
            ],
            default=inwindow_dialog.Outcome.REJECTED,
        )

    def _confirm_force_remove(self) -> bool:
        """Ask whether ending the core is acceptable; a dismissal counts as a no."""
        dialog = self._disable_link_locked_dialog()
        if dialog.parentWidget() is None:
            log.warning("停用联机功能: 没有可以绘制二次确认框的窗口，已取消")
            return False
        return dialog.ask() is inwindow_dialog.Outcome.ACCEPTED

    def _remove_core_with_confirmation(self) -> int | None:
        """Delete the payload; on a sharing violation ask once, then end the core and retry.

        ``None`` means the user declined or the failure has already been reported. Only
        ``tools.disable.locked`` earns the second question: it is the one removal failure the user
        can solve from here. Every other error is reported immediately and never offers to end a
        process, because killing one could not fix it.
        """
        try:
            return int(self._session.remove_core() or 0)
        except ComponentError as exc:
            if exc.key != "tools.disable.locked":
                self._report_disable_failure(exc)
                return None
        except Exception as exc:  # noqa: BLE001 - any removal failure has to reach the user
            self._report_disable_failure(exc)
            return None
        if not self._confirm_force_remove():
            return None
        try:
            return int(self._session.force_remove_core() or 0)
        except Exception as exc:  # noqa: BLE001 - the retry can be refused as well
            self._report_disable_failure(exc)
            return None

    def _report_disable_failure(self, exc: BaseException) -> None:
        """Put the concrete reason on the window's status line; never swallow it."""
        detail = str(getattr(exc, "detail", "") or exc or type(exc).__name__)
        log.warning("删除联机组件失败: %s", detail)
        set_app_status(self, tr("tools.disable.fail", detail), "error")

    def _disable_link(self) -> None:
        """停用联机功能: delete the downloaded payload, then show the onboarding page again.

        The order is the documented one: the room goes first, then the files. Removing the files is
        the session's job (it is the only owner of the component state), so the page can go back to
        ``NOT_DOWNLOADED`` without guessing. A running core keeps its executable open on Windows;
        that particular failure gets a second, explicit question before the page ends the process -
        every other reason is reported immediately with the OS text.
        """
        if self._disabling:
            return
        if self._session.state() is LinkState.NOT_DOWNLOADED:
            set_app_status(self, tr("tools.disable.none"))
            return
        dialog = self._disable_link_dialog()
        if dialog.parentWidget() is None:
            log.warning("停用联机功能: 没有可以绘制确认框的窗口，已取消")
            return
        if dialog.ask() is not inwindow_dialog.Outcome.ACCEPTED:
            return
        self._disabling = True
        try:
            try:
                self._session.close_room()
            except Exception as exc:  # noqa: BLE001 - the room close is part of this operation
                self._report_disable_failure(exc)
                return
            removed = self._remove_core_with_confirmation()
            if removed is None:
                return
        finally:
            self._disabling = False
        if removed <= 0:
            set_app_status(self, tr("tools.disable.none"))
            return
        set_app_status(self, tr("tools.disable.done", _format_mb(removed)))

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
        """Show the last measured latency per mirror: 超时 for a failed probe, else 未测."""
        report = read_report()
        for key, label in self.channel_speed_labels.items():
            latency = report.latency(key)
            if latency is not None:
                label.setText(tr("tools.onboard.channels.latency", latency))
            elif report.timed_out(key):
                label.setText(tr("tools.onboard.channels.timeout"))
            else:
                label.setText(tr("tools.onboard.channels.unmeasured"))

    def focus_link_tab(self) -> None:
        """The title-bar capsule's destination: show 联机 as it is now, never restart anything."""
        self.tabs.setCurrentIndex(0)
        self.tabs.setFocus(Qt.FocusReason.OtherFocusReason)

    # ------------------------------------------------------------------ lifecycle
    def cancel_running_task(self) -> None:
        """Language switch: stop observing; the bus keeps polling and the room stays open."""
        self._release()

    def stop_background_work(self) -> None:
        """Window close: stop observing. The window shuts the bus down separately."""
        self._release()

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

