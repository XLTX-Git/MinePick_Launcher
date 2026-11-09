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

"""Account page: multi-account list and switching, Microsoft device-code login (live countdown), offline login, logout."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, paint_utils
from gui.widgets import (
    add_page_top_inset,
    apply_no_focus_outline,
    set_app_status,
    style_page_layout,
)
from gui.workers import ProgressBridge, run_in_background
from launcher import config
from launcher.auth import AccountStore, MicrosoftSession, create_offline_account

tr = i18n.tr

AVATAR_SIZE = 42  # px; the account avatar is square, so one size covers both uses
# The letter avatar's fill is a disc whose hue comes from the username, so each account gets its own
# colour. Saturation and value are the only design choices left in it, and the white letter on top is
# what constrains them: WCAG AA wants 4.5:1, and the worst of the 360 hues is always 60 (yellow, the
# brightest point of the wheel), so the value has to come down until that single hue clears the bar.
# Measured over the whole circle, white letter on QColor.fromHsv(hue, AVATAR_SATURATION, value):
#     value 150 -> 3.13:1  (the original fill: fine as large text, below AA for a normal-size letter)
#     value 120 -> 4.64:1  (clears it, but by 3% - a rounding change would break it silently)
#     value 110 -> 5.35:1  <- used, 19% of headroom, and the hue stays the one thing that varies
# Saturation barely moves the number (100..255 all land within 0.1 of each other at value 110), so it
# stays where it was and keeps neighbouring hues apart. The cost is a darker disc: its own contrast
# against the dark page background drops from 1.91:1 to 1.44:1 at the worst hue, which is acceptable
# for a decorative avatar - the account name beside it carries the information.
# tests/test_account_avatar.py walks all 360 hues and fails if either number stops clearing 4.5:1.
AVATAR_HUE_STEP = 37  # hue = sum(ord(c) for c in username) * step % 360
AVATAR_SATURATION = 160
AVATAR_VALUE = 110
MIN_VISIBLE_ACCOUNTS = 2  # rows kept readable even in a short window (a half-cut row is worse)
MAX_VISIBLE_ACCOUNTS = 4  # rows shown once the page has room for them


def avatar_pixmap(data: bytes | None, size: int = AVATAR_SIZE) -> QPixmap | None:
    """Decode downloaded skin bytes into a face-only avatar; None keeps the letter avatar.

    The crop itself lives in :func:`gui.paint_utils.skin_face_pixmap`, shared with the launch page.
    """
    skin = QPixmap()
    if not data or not skin.loadFromData(data):
        return None
    return paint_utils.skin_face_pixmap(skin, size)


class _AccountRow(QWidget):
    """One account inside the white list card: avatar, name, type/status, switch and remove.

    Painted by hand for the same reasons the other dense rows are: the meta line stays one elided
    line, and a list of accounts costs one widget per row (plus its two buttons). The card owns the
    surface, so the row only paints a hairline, a hover tint and the selection tint.
    """

    HEIGHT = 68
    AVATAR = 40
    GAP = 12
    TAG_HEIGHT = 18

    def set_avatar(self, pixmap: QPixmap | None) -> None:
        """Swap the generated letter avatar for the account's own skin face."""
        if pixmap is None or pixmap.isNull():
            return
        self._avatar = pixmap
        self.update()

    def __init__(
        self,
        username: str,
        kind_label: str,
        is_current: bool,
        *,
        on_switch,
        on_remove,
        separator: bool = True,
        avatar: QPixmap | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.username = username
        self.kind_label = kind_label
        self.is_current = is_current
        self._on_switch = on_switch
        self._on_remove = on_remove
        self._separator = separator
        self._selected = False
        self._hover = False
        self._avatar = (
            avatar
            if avatar is not None and not avatar.isNull()
            else paint_utils.avatar_pixmap(username, self.AVATAR)
        )
        self._fallback = paint_utils.letter_tile_pixmap(
            username or "?", self.AVATAR, self.AVATAR // 2
        )
        self.setFixedHeight(self.HEIGHT)

        self.switch = QPushButton(tr("account.action.switch"))
        self.switch.setObjectName("rowAction")
        self.switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switch.setAccessibleName(tr("account.action.switch") + " · " + username)
        self.switch.setEnabled(not is_current)  # already current: nothing to switch to
        self.switch.clicked.connect(on_switch)

        self.remove = QPushButton(tr("account.action.remove"))
        self.remove.setObjectName("rowDanger")
        self.remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove.setAccessibleName(tr("account.action.remove") + " · " + username)
        self.remove.clicked.connect(on_remove)
        # ClickFocus, not StrongFocus: selecting the row (arrow keys or a click) is what puts its two
        # buttons into the Tab chain — see set_selected.
        for button in (self.switch, self.remove):
            button.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addStretch(1)
        layout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.remove, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip(f"{username}\n{kind_label}")

    # ---------- geometry ----------

    def text_width(self) -> int:
        """Width the name and the meta line may use."""
        return max(0, self.switch.x() - self.GAP - (self.AVATAR + self.GAP))

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()
        for button in (self.switch, self.remove):
            policy = Qt.FocusPolicy.StrongFocus if selected else Qt.FocusPolicy.ClickFocus
            if button.focusPolicy() != policy:
                button.setFocusPolicy(policy)

    # ---------- painting ----------

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        tokens = paint_utils._tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # The list paints its item text behind the item widget; an opaque fill is what keeps that text
        # (the accessible representation) from showing through the transparent row.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.get("BG_LIST", "#ffffff")))
        painter.drawRect(self.rect())
        if self._selected or self._hover:
            background = QColor(
                tokens.get("ACCENT_TINT" if self._selected else "BG_SIDEBAR", "#eef2f6")
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRect(self.rect())
        if self._separator:
            pen = QPen(QColor(tokens.get("BG_SEPARATOR", "#e6ebf1")))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

        avatar_rect = QRectF(0, (self.HEIGHT - self.AVATAR) / 2.0, self.AVATAR, self.AVATAR)
        painter.drawPixmap(avatar_rect.toRect(), self._avatar or self._fallback)

        text_left = self.AVATAR + self.GAP
        width = self.text_width()
        if width <= 0:
            painter.end()
            return

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_metrics = QFontMetrics(name_font)
        painter.setFont(name_font)
        painter.setPen(text_color)
        name = name_metrics.elidedText(self.username, Qt.TextElideMode.ElideRight, width)
        name_width = name_metrics.horizontalAdvance(name)
        painter.drawText(
            QRectF(text_left, 15, width, 22),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name,
        )

        # The "current" badge sits right after the name; it disappears only when the name itself
        # would have to be truncated to make room for it.
        tag_font = QFont(self.font())
        tag_font.setPixelSize(11)
        tag_metrics = QFontMetrics(tag_font)
        tag = tr("account.status.current") if self.is_current else ""
        tag_width = min(width, tag_metrics.horizontalAdvance(tag) + 12) if tag else 0
        if tag and text_left + name_width + 8 + tag_width <= text_left + width:
            tag_rect = QRectF(text_left + name_width + 8, 17, tag_width, self.TAG_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("ACCENT_TINT", "#e6f4ec")))
            painter.drawRoundedRect(tag_rect, 4, 4)
            painter.setFont(tag_font)
            painter.setPen(QColor(tokens.get("ACCENT_TEXT", "#1f6b46")))
            painter.drawText(
                tag_rect,
                int(Qt.AlignmentFlag.AlignCenter),
                tag_metrics.elidedText(tag, Qt.TextElideMode.ElideRight, int(tag_width) - 12),
            )

        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        meta_metrics = QFontMetrics(meta_font)
        painter.setFont(meta_font)
        painter.setPen(hint_color)
        meta = self.kind_label
        if self.is_current:
            meta = f"{meta} · {tr('account.status.current')}"
        painter.drawText(
            QRectF(text_left, 37, width, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            meta_metrics.elidedText(meta, Qt.TextElideMode.ElideRight, width),
        )
        painter.end()


class LoginPage(QWidget):
    account_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        # Scopes the light sheet's card + dense-row rules to this page.
        self.setObjectName("accountPage")
        self.rows: list[_AccountRow] = []
        # The skin URL the avatar slot currently belongs to: a download that finishes after the user
        # switched accounts must not paint itself over the new one.
        self._avatar_skin_url = ""
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        self.account_label = QLabel()
        self.accounts_list = QListWidget()
        self.accounts_list.setObjectName("accountList")
        self.accounts_list.setAccessibleName(tr("account.list.label"))  # WCAG 4.1.2
        apply_no_focus_outline(self.accounts_list)
        self.accounts_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.accounts_list.currentRowChanged.connect(self._on_row_changed)
        self.add_button = QPushButton(tr("account.add"))
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.device_code = QTextEdit()
        self.device_code.setAccessibleName(tr("a11y.device_code"))  # WCAG 4.1.2
        self.device_code.setMinimumHeight(40)
        self.device_code.setReadOnly(True)
        self.device_code.setFixedHeight(88)
        self.ms_button = QPushButton(tr("login.ms.button"))
        # The page's one primary action is 添加账户 in the account card; this is the same flow's own
        # button inside the device-code card, so it wears the secondary style instead of a second green.
        self.ms_button.setObjectName("secondaryButton")
        self.offline_edit = QLineEdit()
        self.offline_edit.setAccessibleName(tr("a11y.offline_name"))  # WCAG 4.1.2
        self.offline_edit.setPlaceholderText(tr("login.offline.placeholder"))
        self.offline_button = QPushButton(tr("login.offline.button"))
        self.offline_button.setObjectName("secondaryButton")

        account_row = QHBoxLayout()
        account_row.setSpacing(8)
        account_row.addWidget(self.avatar_label)
        account_row.addWidget(self.account_label, 1)
        account_row.addStretch(1)

        accounts_card = QFrame()
        accounts_card.setObjectName("pageCard")
        accounts_layout = QVBoxLayout(accounts_card)
        accounts_layout.setContentsMargins(12, 8, 12, 8)
        accounts_layout.setSpacing(6)
        accounts_header = QHBoxLayout()
        accounts_header.setSpacing(8)
        accounts_title = QLabel(tr("account.list.label"))
        accounts_title.setObjectName("sectionTitle")
        accounts_header.addWidget(accounts_title)
        accounts_header.addStretch(1)
        # The page's one primary action: adding an account runs the device-code flow below.
        accounts_header.addWidget(self.add_button)
        accounts_layout.addLayout(accounts_header)
        accounts_layout.addWidget(self.accounts_list, 1)

        login_card = QFrame()
        login_card.setObjectName("pageCard")
        login_layout = QVBoxLayout(login_card)
        login_layout.setContentsMargins(12, 8, 12, 8)
        login_layout.setSpacing(6)
        ms_hint = QLabel(tr("login.ms.hint"))
        ms_hint.setObjectName("hint")
        ms_hint.setWordWrap(True)
        login_layout.addWidget(ms_hint)
        login_layout.addWidget(self.device_code)
        login_layout.addWidget(self.ms_button)

        offline_card = QFrame()
        offline_card.setObjectName("pageCard")
        offline_layout = QVBoxLayout(offline_card)
        offline_layout.setContentsMargins(12, 8, 12, 8)
        offline_layout.setSpacing(6)
        offline_hint = QLabel(tr("login.offline.hint"))
        offline_hint.setObjectName("hint")
        offline_hint.setWordWrap(True)
        offline_layout.addWidget(offline_hint)
        offline_row = QHBoxLayout()
        offline_row.setSpacing(8)
        offline_row.addWidget(self.offline_edit, 1)
        offline_row.addWidget(self.offline_button)
        offline_layout.addLayout(offline_row)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(8)
        inner_layout.addWidget(accounts_card)
        inner_layout.addWidget(login_card)
        inner_layout.addWidget(offline_card)
        inner_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setAccessibleName(tr("nav.account"))  # WCAG 4.1.2
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)

        layout = QVBoxLayout(self)
        style_page_layout(layout)
        add_page_top_inset(layout)
        layout.addLayout(account_row)
        layout.addWidget(scroll, 1)

        self.add_button.clicked.connect(lambda: self.ms_button.click())
        self.ms_button.clicked.connect(self.start_ms_login)
        self.offline_button.clicked.connect(self.offline_login)
        self.offline_edit.returnPressed.connect(self.offline_login)
        # Device-code countdown
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._on_countdown_tick)
        self._countdown_seconds = 0
        self.refresh()

    def refresh(self) -> None:
        cfg, _ = config.load()
        accounts = AccountStore().load()
        account = accounts.get(cfg.selected_account) if cfg.selected_account else None
        self._avatar_skin_url = str(getattr(account, "skin_url", "") or "") if account else ""
        if account is None:
            self.account_label.setText(tr("account.none"))
            self._clear_avatar()  # no account: the slot keeps its size and paints nothing
        else:
            kind = tr("kind.ms") if account.type == "microsoft" else tr("kind.offline")
            self.account_label.setText(tr("account.current", account.username, kind))
            self._update_avatar(account)
        self._refresh_accounts_list()

    # ---------- Avatar ----------

    def _clear_avatar(self) -> None:
        """Leave the avatar slot empty: transparent, and still exactly AVATAR_SIZE wide.

        Both "no account yet" and "a name with no letter in it" end up here. The label is *cleared* and
        not handed a transparent pixmap, because an empty QLabel paints nothing at all, so the page's own
        background shows through the slot. The fixed size is what keeps the rest of the account row where
        it was: the layout allocates the same 42px whether or not anything is drawn inside it.
        """
        self.avatar_label.clear()

    def _set_letter_avatar(self, username: str) -> None:
        """Draw the fallback avatar: the account's first letter, white, inside its own colour disc.

        The disc's hue is taken from the username, so two accounts differ in colour as well as in letter;
        AVATAR_VALUE is the number that keeps the white letter readable on every one of those hues (see
        the note on the constants). A name that has no letter to show - empty, or nothing but whitespace -
        leaves the avatar transparent rather than falling back to a placeholder glyph.
        """
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

        letter = (username.strip()[:1] or "").upper()
        if not letter:
            self._clear_avatar()
            return
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        hue = (sum(ord(c) for c in username) * AVATAR_HUE_STEP) % 360
        painter.setBrush(QColor.fromHsv(hue, AVATAR_SATURATION, AVATAR_VALUE))
        painter.setPen(_Qt.NoPen)
        painter.drawEllipse(0, 0, 64, 64)
        painter.setPen(QColor("#ffffff"))
        font = QFont()
        font.setPixelSize(30)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), _Qt.AlignCenter, letter)
        painter.end()
        self.avatar_label.setPixmap(
            pixmap.scaled(AVATAR_SIZE, AVATAR_SIZE, _Qt.KeepAspectRatio, _Qt.SmoothTransformation)
        )

    def _update_avatar(self, account) -> None:
        """Paint the letter first, then upgrade to the account's skin face when it arrives.

        The download is shared through :mod:`gui.paint_utils`: one request per skin URL for the whole
        process, so the launch page and this page never fetch the same texture twice. A failed or
        unusable texture leaves the letter in place rather than an empty slot.
        """
        self._set_letter_avatar(account.username)
        skin_url = str(getattr(account, "skin_url", "") or "")
        if not skin_url:
            return

        def apply_face(source) -> None:
            if source is None or self._avatar_skin_url != skin_url:
                return  # failed, or another account owns the slot by now
            face = paint_utils.skin_face_pixmap(source, AVATAR_SIZE)
            if face is None:
                return
            try:
                self.avatar_label.setPixmap(face)
                for row in self.rows:
                    if row.username == account.username:
                        row.set_avatar(face)
                        break
            except RuntimeError:
                pass  # page already destroyed (language switch rebuild)

        if not paint_utils.watch_skin(skin_url, apply_face):
            return  # cached (the face is already painted) or another download will call back

        def do_fetch() -> object:
            from launcher.meta.manifest import _new_client

            client = _new_client()
            try:
                resp = client.get(skin_url)
                resp.raise_for_status()
                return resp.content
            finally:
                client.close()

        run_in_background(
            do_fetch,
            on_result=lambda data: paint_utils.finish_skin(skin_url, data),
            on_error=lambda _message: paint_utils.finish_skin(skin_url, None),
        )

    # ---------- Account list ----------

    def _refresh_accounts_list(self) -> None:
        cfg, _ = config.load()
        accounts = AccountStore().load()
        ordered = sorted(accounts.items(), key=lambda kv: kv[1].username)
        # Drop the Python references before clear(): the C++ row widgets are destroyed there.
        self.rows = []
        self.accounts_list.blockSignals(True)
        self.accounts_list.clear()
        for index, (account_id, account) in enumerate(ordered):
            kind = tr("kind.ms") if account.type == "microsoft" else tr("kind.offline")
            # The item text stays the accessible/testable representation it always was: the name, its
            # type and - for the selected one - the [current] marker the account tests assert on.
            item = QListWidgetItem(account.username + "（" + kind + "）")
            item.setData(Qt.UserRole, account_id)
            if account_id == cfg.selected_account:
                item.setText(item.text() + "  [" + tr("account.status.current") + "]")
            item.setSizeHint(QSize(0, _AccountRow.HEIGHT))
            self.accounts_list.addItem(item)
            is_current = account_id == cfg.selected_account
            row = _AccountRow(
                account.username,
                kind,
                is_current,
                on_switch=lambda row_index=index: self._switch_row(row_index),
                on_remove=lambda account_key=account_id: self._remove_row(account_key),
                separator=index < len(ordered) - 1,
                avatar=paint_utils.skin_face_for(
                    str(getattr(account, "skin_url", "") or ""), _AccountRow.AVATAR
                ),
            )
            self.accounts_list.setItemWidget(item, row)
            self.rows.append(row)
        self.accounts_list.blockSignals(False)
        self._on_row_changed(self.accounts_list.currentRow())
        self._fit_accounts_list()

    def _fit_accounts_list(self) -> None:
        """Size the account list by whole rows, so no account is ever cut in half.

        Row height is measured from the live list (it follows the QSS padding, the UI font and
        the active language) and the frame from the widget, so nothing here is hardcoded.
        """
        measure = None
        if self.accounts_list.count() == 0:
            # A throw-away row: an empty list has nothing to measure. It carries the same size hint a
            # real row does, otherwise the empty and the one-account page would size the list
            # differently and the avatar slot's "the page does not move" test would fail.
            measure = QListWidgetItem(" ")
            measure.setSizeHint(QSize(0, _AccountRow.HEIGHT))
            self.accounts_list.addItem(measure)
        row_height = self.accounts_list.sizeHintForRow(0)
        if measure is not None:
            self.accounts_list.takeItem(0)
        if row_height <= 0:  # not polished yet: showEvent() measures again
            return
        frame = 2 * self.accounts_list.frameWidth()
        rows = max(1, self.accounts_list.count())
        low = min(rows, MIN_VISIBLE_ACCOUNTS)
        high = max(low, min(rows, MAX_VISIBLE_ACCOUNTS))
        self.accounts_list.setMinimumHeight(low * row_height + frame)
        self.accounts_list.setMaximumHeight(high * row_height + frame)

    def showEvent(self, event) -> None:  # Qt override
        super().showEvent(event)
        self._fit_accounts_list()  # first chance to measure the styled row height

    def _selected_account_id(self) -> str | None:
        item = self.accounts_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _on_row_changed(self, index: int = -1) -> None:
        for row_index, row in enumerate(self.rows):
            row.set_selected(row_index == index)

    def _switch_row(self, index: int) -> None:
        """Row switch: select the row, then run the page's existing switch flow."""
        self.accounts_list.setCurrentRow(index)
        self.switch_account()

    def _remove_row(self, account_id: str) -> None:
        """Row remove: select that account, then run the page's existing logout flow."""
        for index in range(self.accounts_list.count()):
            item = self.accounts_list.item(index)
            if item is not None and item.data(Qt.UserRole) == account_id:
                self.accounts_list.setCurrentRow(index)
                break
        self.logout()

    def switch_account(self) -> None:
        account_id = self._selected_account_id()
        if account_id is None:
            set_app_status(self, tr("account.msg.need_select"), "warning")
            return
        cfg, cfg_path = config.load()
        cfg.selected_account = account_id
        config.save(cfg, cfg_path)
        self.refresh()
        account = AccountStore().load().get(account_id)
        set_app_status(self, tr("account.msg.switched", account.username if account else account_id))
        self.account_changed.emit()

    def _save_account(self, account) -> None:
        store = AccountStore()
        accounts = store.load()
        accounts[account.id] = account
        store.save(accounts)
        cfg, cfg_path = config.load()
        cfg.selected_account = account.id
        config.save(cfg, cfg_path)
        if account.type == "microsoft":
            from launcher.config import unlock_offline_mode

            unlock_offline_mode()  # unlock offline mode after a licensed login
        self.refresh()
        self.account_changed.emit()

    def start_ms_login(self) -> None:
        # Disabling a button that holds focus hands focus to the next widget in the chain - here the
        # offline username box, which the user never clicked. Park it on the read-only code view
        # instead: that is where the device code shows up, and it has no focus outline in QSS.
        self.device_code.setFocus()
        self.ms_button.setEnabled(False)
        self.device_code.clear()
        set_app_status(self, tr("login.msg.waiting"))
        bridge = ProgressBridge()
        bridge.progress.connect(self._on_progress)
        flow_bridge = ProgressBridge()
        flow_bridge.progress.connect(self._on_flow)  # worker thread -> main thread
        cfg, _ = config.load()

        def do_login(progress) -> object:
            session = MicrosoftSession(client_id=cfg.msa_client_id)
            return session.login_interactive(progress=progress, on_flow=flow_bridge)

        run_in_background(
            do_login,
            bridge,
            on_result=self._on_ms_ok,
            on_error=self._on_error,
            on_finished=self._on_login_finished,
        )

    def _on_flow(self, flow: dict) -> None:
        """Device code generated: auto-open the auth page + copy the code + start the countdown."""
        uri = str(flow.get("verification_uri") or "")
        if uri:
            QDesktopServices.openUrl(QUrl(uri))
            self.device_code.append(tr("login.msg.page_opened"))
        code = str(flow.get("user_code") or "")
        if code:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.clipboard().setText(code)
                self.device_code.append(tr("login.msg.code_copied", code))
        try:
            self._countdown_seconds = int(flow.get("expires_in") or 0)
        except (TypeError, ValueError):
            self._countdown_seconds = 0
        self._on_countdown_tick()
        if self._countdown_seconds > 0:
            self._countdown.start()

    def _on_countdown_tick(self) -> None:
        if self._countdown_seconds > 0:
            minutes, seconds = divmod(self._countdown_seconds, 60)
            set_app_status(self, tr("login.msg.countdown", f"{minutes:02d}:{seconds:02d}"))
            self._countdown_seconds -= 1

    def _stop_countdown(self) -> None:
        self._countdown.stop()
        self._countdown_seconds = 0

    def _on_login_finished(self) -> None:
        self._stop_countdown()
        self.ms_button.setEnabled(True)

    def _on_progress(self, message: str) -> None:
        self.device_code.append(message)

    def _on_ms_ok(self, account) -> None:
        self._stop_countdown()
        set_app_status(self, tr("login.msg.ok"))
        self._save_account(account)

    def _on_error(self, message: str) -> None:
        self._stop_countdown()
        set_app_status(self, tr("login.msg.fail", message), "error")

    def offline_login(self) -> None:
        from launcher.config import offline_mode_allowed

        if not offline_mode_allowed():
            set_app_status(self, tr("launch.msg.offline_locked"), "warning")
            return
        try:
            account = create_offline_account(self.offline_edit.text())
        except ValueError as exc:
            set_app_status(self, tr("login.msg.fail", exc), "error")
            return
        self._save_account(account)
        set_app_status(self, tr("login.msg.offline_ok"))

    def logout(self) -> None:
        cfg, cfg_path = config.load()
        store = AccountStore()
        accounts = store.load()
        # Prefer logging out the account selected in the list, otherwise the current account
        target = self._selected_account_id() or cfg.selected_account
        if not target:
            set_app_status(self, tr("login.msg.no_account"))
            return
        accounts.pop(target, None)
        if cfg.selected_account == target:
            cfg.selected_account = None
        store.save(accounts)
        config.save(cfg, cfg_path)
        self.refresh()
        self.account_changed.emit()
        set_app_status(self, tr("login.msg.logged_out"))
