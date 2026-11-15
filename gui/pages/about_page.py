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

"""About page: version and source link, legal notices, credits, and the update flow.

All feedback also goes to the single status bar (like every other page); the update card keeps its own
one-line state text so a screenshot of the page alone says what happened. The check and the download
run on worker threads and only report back through queued callbacks.

The install is the one part that must not run on the GUI thread: it renames the running executable and
copies the verified download next to it. That is :func:`launcher.update.install.apply_and_restart`'s
job, and it starts the new build itself; this page only quits afterwards. A swap is never started from
an automatic path - the user has to click "restart and install", because the running launcher is the
thing being replaced.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import i18n
from gui.tasks import format_size
from gui.widgets import (
    add_page_top_inset,
    build_download_progress,
    set_app_status,
    style_page_layout,
)
from gui.workers import ProgressBridge, run_in_background
from launcher import __version__, config, notices, updates
from launcher.net.downloader import DownloadCancelled
from launcher.update import install

tr = i18n.tr

SOURCE_URL = f"https://github.com/{updates.REPO}"
LICENSE_URL = f"https://github.com/{updates.REPO}/blob/main/LICENSE"
NOTICES_URL = f"https://github.com/{updates.REPO}/blob/main/THIRD_PARTY_NOTICES.md"

#: The update modes the config may hold. ``download_install`` is the pre-0.5 value and no longer
#: installs by itself (a swap needs a click); it is read as ``download_notify`` so an old config keeps
#: auto-downloading instead of silently changing what the user chose.
MODES = ("download_install", "download_notify", "notify", "off")
DEFAULT_MODE = "download_notify"
#: Update-check failure codes, mapped to ``about.update.failed.<code>``.
FAILURE_CODES = ("network", "http", "parse", "no_asset", "size", "signature", "no_release")
#: Install refusal/failure codes, mapped to ``about.update.install.<code>`` where one exists.
INSTALL_CODES = ("source", "missing", "writable", "space", "stage", "swap", "rollback", "restart")
PROGRESS_STEP = 1 << 20  # report download progress about once per MB

#: Space between a section title and the content it introduces: one blank line's worth. Every title
#: on this page uses it - the three cards' own titles and the legal blocks inside the first card -
#: so a heading always reads as belonging to the text under it instead of running into it.
TITLE_BODY_GAP = 8
#: Space between two rows of ordinary body content (the form rhythm every other page uses).
BODY_ROW_GAP = 6


class AboutPage(QWidget):
    """About / update page (see the module docstring)."""

    def __init__(self) -> None:
        super().__init__()
        # Scopes the light sheet's "each section is a white card on the paper" rule to this page.
        self.setObjectName("aboutPage")
        self._release: updates.ReleaseInfo | None = None
        self._pending: Path | None = None
        self._pending_version = ""
        self._state = "idle"
        self._last_action = "check"
        self._cancel: threading.Event | None = None
        self._notes_expanded = False

        inner = QWidget()
        layout = QVBoxLayout(inner)
        style_page_layout(layout)
        # The outer page layout already owns the 16px top inset; a 12px inner margin here would
        # put the first card 28px below the page top, visibly emptier than every other page.
        layout.setContentsMargins(14, 0, 14, 6)
        layout.addWidget(self._build_information())
        layout.addWidget(self._build_updates())
        layout.addWidget(self._build_credits())
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setAccessibleName(tr("nav.about"))  # WCAG 4.1.2
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        self.scroll = scroll

        outer = QVBoxLayout(self)
        style_page_layout(outer)
        add_page_top_inset(outer)
        outer.addWidget(scroll)

        self.load()
        self._sync_buttons()
        self._set_status("")

    # ---------- construction ----------

    def _card(self, title_key: str) -> tuple[QFrame, QVBoxLayout]:
        """One white card with its title *inside* it: a 15/600 heading, 8px above the body.

        The page used to build ``QGroupBox`` sections. Qt draws a group box's title on the frame
        itself, so "软件信息" / "软件更新" sat above the card border and read as if they belonged to
        the page rather than to the card. A plain ``QFrame#pageCard`` (the same card every other page
        uses) plus this label keeps the title inside the card's 16px padding. The title is a plain
        label, not a focusable control, so the page's keyboard order does not change.
        """
        card = QFrame()
        card.setObjectName("pageCard")
        layout = QVBoxLayout(card)
        # 16px on every side is the card's own inset: the old QGroupBox painted 12px of padding and
        # let the layout add none, which left long legal lines 12px from the border. The body keeps
        # the form rhythm of 6px between its rows; only the title-to-body step is 8px.
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(TITLE_BODY_GAP)
        title = QLabel(tr(title_key))
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(BODY_ROW_GAP)
        layout.addWidget(body)
        return card, body_layout

    def _build_information(self) -> QFrame:
        box, layout = self._card("about.info.title")

        layout.addWidget(QLabel(tr("about.info.version", __version__)))

        self.source_button = QPushButton(tr("about.info.source"))
        self.source_button.clicked.connect(lambda: self._open(SOURCE_URL))
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_button)
        source_row.addStretch(1)
        layout.addLayout(source_row)

        # The link itself, under the button that opens it: a long URL wraps instead of pushing the
        # card wider. Plain text on purpose - it is not a control, so it must not become a focus stop
        # that would need a focus ring of its own; the button above is the way to open it.
        self.source_url = QLabel(SOURCE_URL)
        self.source_url.setObjectName("aboutLink")
        self.source_url.setWordWrap(True)
        self.source_url.setToolTip(SOURCE_URL)
        layout.addWidget(self.source_url)

        layout.addWidget(
            self._legal_block("about.legal.title", "about.legal.body", LICENSE_URL, "about.legal.view_license")
        )
        layout.addWidget(self._legal_block("about.upstream.title", "about.upstream.body", None, None))
        layout.addWidget(
            self._legal_block(
                "about.licenses.title", "about.licenses.body", NOTICES_URL, "about.licenses.notices"
            )
        )
        return box

    def _legal_block(self, title_key: str, body_key: str, url: str | None, link_key: str | None) -> QWidget:
        """A titled paragraph of legal text with an optional link button."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        # 6px above every block: the card's own 6px spacing plus this makes two blocks sit 12px apart,
        # while the rows inside one block stay at the form rhythm of 6px.
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(BODY_ROW_GAP)
        title = QLabel(tr(title_key))
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        # The title gets the same 8px step to its body as the card titles get: 6px of layout spacing
        # plus these 2px. Without it the heading sat as tight against its paragraph as two body rows
        # sit against each other, so it read as part of the text instead of as a heading over it.
        layout.addSpacing(TITLE_BODY_GAP - BODY_ROW_GAP)
        body = QLabel(tr(body_key))
        body.setObjectName("hint")
        body.setWordWrap(True)
        layout.addWidget(body)
        if url and link_key:
            button = QPushButton(tr(link_key))
            button.setObjectName("secondaryButton")
            button.clicked.connect(lambda _checked=False, target=url: self._open(target))
            row = QHBoxLayout()
            row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)
        return holder

    def _build_updates(self) -> QFrame:
        box, layout = self._card("about.update.title")

        versions = QHBoxLayout()
        self.current_label = QLabel(tr("about.update.current", __version__))
        self.latest_label = QLabel(tr("about.update.latest_unknown"))
        self.latest_label.setObjectName("hint")
        versions.addWidget(self.current_label)
        versions.addSpacing(18)
        versions.addWidget(self.latest_label)
        versions.addStretch(1)
        layout.addLayout(versions)

        # Release details: hidden until a check finds something newer. A plain container (not a
        # nested card) so the card spacing stays the page's 6px rhythm when it is shown.
        self.release_box = QWidget()
        release_layout = QVBoxLayout(self.release_box)
        release_layout.setContentsMargins(0, 2, 0, 0)
        release_layout.setSpacing(BODY_ROW_GAP)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(18)
        self.size_label = QLabel()
        self.size_label.setObjectName("hint")
        self.published_label = QLabel()
        self.published_label.setObjectName("hint")
        meta_row.addWidget(self.size_label)
        meta_row.addWidget(self.published_label)
        meta_row.addStretch(1)
        release_layout.addLayout(meta_row)
        self.notes_button = QPushButton(tr("about.update.notes.show"))
        self.notes_button.setObjectName("secondaryButton")
        self.notes_button.clicked.connect(self._toggle_notes)
        notes_row = QHBoxLayout()
        notes_row.addWidget(self.notes_button)
        notes_row.addStretch(1)
        release_layout.addLayout(notes_row)
        self.notes_label = QLabel()
        self.notes_label.setObjectName("hint")
        self.notes_label.setWordWrap(True)
        # The notes are release text, not this page's prose: selectable so a user can copy a line
        # into a bug report, and never a focus ring of its own (it is not a control).
        self.notes_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes_label.setVisible(False)
        release_layout.addWidget(self.notes_label)
        self.release_box.setVisible(False)
        layout.addWidget(self.release_box)

        # Download progress with the one control that belongs to it: cancel.
        progress_row = QHBoxLayout()
        self.progress_row, self.progress_bar, _progress_text = build_download_progress("updateProgress")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.cancel_button = QPushButton(tr("about.update.cancel"))
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.clicked.connect(self.cancel_download)
        self.cancel_button.setVisible(False)
        progress_row.addWidget(self.progress_row, 1)
        progress_row.addWidget(self.cancel_button)
        layout.addLayout(progress_row)

        # The page's own status line: the window status bar also reports every step, but a screenshot
        # of the card must say on its own whether it is checking, up to date, failed, or ready.
        self.state_label = QLabel()
        self.state_label.setObjectName("hint")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.check_button = QPushButton(tr("about.update.check"))
        self.check_button.clicked.connect(self.check_updates)
        self.download_button = QPushButton(tr("about.update.download_install"))
        self.download_button.clicked.connect(self._start_download)
        self.download_button.setVisible(False)
        self.restart_button = QPushButton(tr("about.update.restart_install"))
        self.restart_button.clicked.connect(self.restart_and_install)
        self.restart_button.setVisible(False)
        self.retry_button = QPushButton(tr("about.update.retry"))
        self.retry_button.setObjectName("secondaryButton")
        self.retry_button.clicked.connect(self.retry)
        self.retry_button.setVisible(False)
        self.open_page_button = QPushButton(tr("about.update.open_page"))
        self.open_page_button.setObjectName("secondaryButton")
        self.open_page_button.clicked.connect(self.open_release_page)
        self.open_page_button.setVisible(False)
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        for button in (
            self.check_button,
            self.download_button,
            self.restart_button,
            self.retry_button,
            self.open_page_button,
        ):
            button_row.addWidget(button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        # Settings: two switches over the one existing config key (auto_update_mode), so the startup
        # check keeps reading the value it always read. The mode title is the form label for them.
        mode_title = QLabel(tr("about.update.mode.title"))
        mode_title.setObjectName("formLabel")
        layout.addWidget(mode_title)
        self.auto_check = QCheckBox(tr("about.update.auto_check"))
        self.auto_check.setAccessibleName(tr("about.update.auto_check"))
        self.auto_check.toggled.connect(self._on_settings_changed)
        layout.addWidget(self.auto_check)
        self.auto_download = QCheckBox(tr("about.update.auto_download"))
        self.auto_download.setAccessibleName(tr("about.update.auto_download"))
        self.auto_download.toggled.connect(self._on_settings_changed)
        layout.addWidget(self.auto_download)
        self.settings_hint = QLabel(tr("about.update.settings.hint"))
        self.settings_hint.setObjectName("hint")
        self.settings_hint.setWordWrap(True)
        layout.addWidget(self.settings_hint)
        return box

    def _build_credits(self) -> QFrame:
        box, layout = self._card("about.credits.title")

        for group_key, entries in notices.groups():
            heading = QLabel(tr(group_key))
            # One step below the card's own title: 依赖项 / 其他 are sub-headings inside 特别鸣谢, and at
            # the same 15/600 they read as sibling cards rather than as groups within one.
            heading.setObjectName("creditsGroupTitle")
            layout.addWidget(heading)
            for entry in entries:
                layout.addWidget(self._credit_row(entry))
        return box

    def _credit_row(self, entry: notices.Credit) -> QWidget:
        """One acknowledgment: name (never translated), translated description, optional link."""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        text = QLabel(f"<b>{entry.name}</b> — {tr(entry.detail_key)}")
        text.setObjectName("hint")
        text.setWordWrap(True)
        row.addWidget(text, 1)
        if entry.url:
            button = QPushButton(tr("about.credits.link"))
            button.setObjectName("secondaryButton")
            button.clicked.connect(lambda _checked=False, target=entry.url: self._open(target))
            row.addWidget(button)
        return holder

    # ---------- config ----------

    def load(self) -> None:
        """Fill the widgets from the config (signals blocked: this is not a user change)."""
        cfg, _ = config.load()
        mode = self._valid_mode(cfg.auto_update_mode)
        self.auto_check.blockSignals(True)
        self.auto_download.blockSignals(True)
        self.auto_check.setChecked(mode != "off")
        self.auto_download.setChecked(mode == "download_notify")
        self.auto_check.blockSignals(False)
        self.auto_download.blockSignals(False)
        self.auto_download.setEnabled(mode != "off")

    @staticmethod
    def _valid_mode(value: str) -> str:
        # "download_install" is the old automatic-install mode. Installing without a click is gone,
        # so it reads as "download and tell me"; every other unknown value falls back to the default.
        if value == "download_install":
            return "download_notify"
        return value if value in MODES else DEFAULT_MODE

    def current_mode(self) -> str:
        """The mode the two switches currently express (same vocabulary as the config)."""
        if not self.auto_check.isChecked():
            return "off"
        return "download_notify" if self.auto_download.isChecked() else "notify"

    def _on_settings_changed(self) -> None:
        self.auto_download.setEnabled(self.auto_check.isChecked())
        cfg, cfg_path = config.load()
        cfg.auto_update_mode = self.current_mode()
        config.save(cfg, cfg_path)

    # ---------- links ----------

    @staticmethod
    def _open(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def open_release_page(self) -> None:
        page = self._release.page_url if self._release is not None else updates.RELEASES_PAGE
        self._open(page)

    # ---------- update check ----------

    def check_updates(self) -> None:
        """Manual check; the automatic one goes through :meth:`auto_check_on_start`."""
        if self._state in ("checking", "downloading", "installing"):
            return
        self._last_action = "check"
        self._state = "checking"
        self._sync_buttons()
        self._set_status(tr("about.update.checking"))
        set_app_status(self, tr("about.update.checking"))
        run_in_background(
            updates.check_for_update,
            __version__,
            on_result=self._on_check_result,
            on_error=self._on_check_error,
            on_finished=self._on_check_finished,
        )

    def _on_check_finished(self) -> None:
        if self._state == "checking":  # a result already moved the page on (download, ready, ...)
            self._state = "idle"
        self._sync_buttons()

    def _on_check_error(self, _message: str) -> None:
        self._show_failure("network")

    def _on_check_result(self, result: updates.UpdateCheck) -> None:
        if result.latest:
            self.latest_label.setText(tr("about.update.latest", result.latest))
        if result.status == "failed":
            self._show_failure(result.error)
            return
        if result.status == "up_to_date":
            self._show_up_to_date(result.checked_at)
            return
        self._show_available(result.release)

    def _show_up_to_date(self, checked_at: str = "") -> None:
        self._release = None
        self._state = "up_to_date"
        self.release_box.setVisible(False)
        if not checked_at:
            checked_at = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")
        text = tr("about.update.up_to_date_at", checked_at)
        self._set_status(text)
        set_app_status(self, text)
        self._sync_buttons()

    def _show_available(self, release: updates.ReleaseInfo | None) -> None:
        if release is None:
            return
        self._release = release
        self._state = "available"
        self.latest_label.setText(tr("about.update.latest", release.version))
        if release.exe_size:
            self.size_label.setText(tr("about.update.size", format_size(release.exe_size)))
            self.size_label.setVisible(True)
        else:
            self.size_label.setVisible(False)
        if release.published_date:
            self.published_label.setText(tr("about.update.published", release.published_date))
            self.published_label.setVisible(True)
        else:
            self.published_label.setVisible(False)
        notes = (release.notes or "").strip()
        self.notes_button.setVisible(bool(notes))
        self.notes_label.setText(notes)
        self._notes_expanded = False
        self.notes_label.setVisible(False)
        self.notes_button.setText(tr("about.update.notes.show"))
        self.release_box.setVisible(True)
        text = tr("about.update.available", release.version)
        self._set_status(text)
        set_app_status(self, text)
        self._sync_buttons()
        if self.current_mode() == "download_notify":
            self._start_download()

    def _toggle_notes(self) -> None:
        self._notes_expanded = not self._notes_expanded
        self.notes_label.setVisible(self._notes_expanded)
        self.notes_button.setText(
            tr("about.update.notes.hide" if self._notes_expanded else "about.update.notes.show")
        )

    @staticmethod
    def _failure_key(code: str) -> str:
        return f"about.update.failed.{code if code in FAILURE_CODES else 'network'}"

    @staticmethod
    def _install_failure_text(code: str) -> str:
        if code in ("source", "missing", "writable", "space"):
            return tr(f"about.update.install.{code}")
        return tr("about.update.install.failed")

    def _show_failure(self, code: str, detail: str = "") -> None:
        """Show what went wrong and offer a retry; never leave a spinner or a hidden reason."""
        self._state = "failed"
        if code in INSTALL_CODES:
            text = self._install_failure_text(code)
        else:
            text = tr(self._failure_key(str(code)))
        self._set_status(text, detail=detail)
        set_app_status(self, text, "error")
        self._sync_buttons()

    # ---------- download ----------

    def _start_download(self) -> None:
        release = self._release
        if release is None or self._state in ("downloading", "installing"):
            return
        self._last_action = "download"
        self._state = "downloading"
        self._cancel = threading.Event()
        cancel_event = self._cancel
        self.progress_bar.setValue(0)
        self._sync_buttons()
        self._set_status(tr("about.update.downloading", "0"))
        set_app_status(self, tr("about.update.downloading", "0"))
        bridge = ProgressBridge()
        bridge.progress.connect(self._on_download_progress)

        def do_download(progress) -> tuple[str, object]:
            last = 0

            def on_progress(done: int, total: int) -> None:
                nonlocal last
                if done - last >= PROGRESS_STEP or (total and done == total):
                    last = done
                    progress((done, total))

            try:
                path = updates.download_release_exe(
                    release,
                    updates.updates_dir(),
                    on_progress=on_progress,
                    cancel=cancel_event,
                )
            except DownloadCancelled:
                return "cancelled", ""
            except updates.UpdateError as exc:
                return "failed", exc.code
            return "ok", path

        self._pending_version = release.version
        run_in_background(
            do_download,
            bridge,
            # bound methods on purpose: the dispatcher's owner guard only sees callbacks that
            # expose __self__ (a lambda would still run against a page rebuilt by a language switch)
            on_result=self._on_download_result,
            on_error=self._on_download_error,
            on_finished=self._on_download_finished,
        )

    def cancel_download(self) -> None:
        """Stop the running download; the partial file is deleted and nothing is installed."""
        if self._cancel is not None and self._state == "downloading":
            self._cancel.set()
            self.cancel_button.setEnabled(False)

    def _on_download_finished(self) -> None:
        if self._state == "downloading":  # no result arrived: treat as a failed attempt, not a spinner
            self._show_failure("network")
        self._cancel = None
        self._sync_buttons()

    def _on_download_error(self, _message: str) -> None:
        self._show_failure("network")

    def _on_download_progress(self, payload) -> None:
        done, total = payload
        if total:
            percent = str(done * 100 // total)
            self.progress_bar.setValue(int(done * 1000 / total))
            self._set_status(tr("about.update.downloading", percent))
            set_app_status(self, tr("about.update.downloading", percent))
        else:
            self._set_status(tr("about.update.downloading", "?"))
            set_app_status(self, tr("about.update.downloading", "?"))

    def _on_download_result(self, payload) -> None:
        status, value = payload
        version = self._pending_version
        if status == "cancelled":
            self._cancel = None
            self._state = "available"
            self._set_status(tr("about.update.cancelled"))
            set_app_status(self, tr("about.update.cancelled"))
            self._sync_buttons()
            return
        if status == "failed":
            self._cancel = None
            self._show_failure(str(value))
            return
        self._cancel = None
        self._pending = Path(value)
        updates.save_pending(self._pending, version)
        self._show_ready(version)

    # ---------- install ----------

    def _staged_update(self) -> Path | None:
        if self._pending is not None and self._pending.is_file():
            return self._pending
        staged = updates.load_pending()
        return staged[0] if staged is not None else None

    def has_pending_install(self) -> bool:
        """Whether the download is on disk and waiting for the user's click."""
        return bool(self._staged_update())

    def _show_ready(self, version: str) -> None:
        """A verified download is on disk: offer the one action that replaces the executable."""
        self._state = "ready"
        self.release_box.setVisible(self._release is not None)
        if version:
            self.latest_label.setText(tr("about.update.latest", version))
        if updates.can_self_update():
            text = tr("about.update.install.ready", version)
            self._set_status(text)
            set_app_status(self, text)
        else:
            # Say it here, not only after a click: a source checkout can download, but never install.
            text = tr("about.update.install.source")
            self._set_status(text)
            set_app_status(self, text, "warning")
        self._sync_buttons()

    def restart_and_install(self) -> None:
        """Swap the running executable and restart it; the caller stays alive until the swap worked."""
        if self._state == "installing":
            return
        pending = self._staged_update()
        if pending is None:
            self._show_failure("missing")
            return
        self._last_action = "install"
        feasibility = install.check_feasibility(None, pending.stat().st_size)
        if not feasibility.ok:
            self._show_failure(feasibility.reason, detail=feasibility.detail)
            return
        self._state = "installing"
        self._set_status(tr("about.update.installing"))
        set_app_status(self, tr("about.update.installing"))
        self._sync_buttons()

        def do_install() -> tuple[str, str]:
            try:
                install.apply_and_restart(pending, argv=sys.argv)
            except install.InstallError as exc:
                return "failed", exc.code
            except Exception:
                logging.getLogger(__name__).exception("the update install raised unexpectedly")
                return "failed", "swap"
            return "ok", ""

        run_in_background(do_install, on_result=self._on_install_result)

    def install_now(self) -> None:
        """Old name kept for the page's callers: installing is the restart-and-install action now."""
        self.restart_and_install()

    def install_pending_on_exit(self) -> None:
        """Retired: closing the window must not replace the executable (see the module function).

        ``gui.main_window`` still calls this on close; doing nothing keeps a staged download for the
        next start instead of installing it without the user's click.
        """
        return

    def _on_install_result(self, payload) -> None:
        status, code = payload
        if status == "failed":
            self._show_failure(str(code))
            return
        updates.clear_pending()
        self._state = "done"
        text = tr("about.update.install.started")
        self._set_status(text)
        set_app_status(self, text)
        self._sync_buttons()
        application = QApplication.instance()
        if application is not None:
            QTimer.singleShot(200, application.quit)

    def retry(self) -> None:
        """Repeat the step that failed: the check, the download, or the install."""
        if self._last_action == "download" and self._release is not None:
            self._state = "available"
            self._start_download()
        elif self._last_action == "install":
            self._state = "ready"
            self.restart_and_install()
        else:
            self.check_updates()

    # ---------- state ----------

    def _set_status(self, text: str, detail: str = "") -> None:
        self.state_label.setText(text)
        self.state_label.setToolTip(detail or text)

    def _sync_buttons(self) -> None:
        """One place that decides which control is visible/enabled for the current state."""
        state = self._state
        self.check_button.setVisible(state not in ("downloading", "ready", "installing", "done"))
        self.check_button.setEnabled(state not in ("checking", "downloading", "installing"))
        self.download_button.setVisible(state == "available")
        self.download_button.setEnabled(state == "available")
        self.restart_button.setVisible(state == "ready")
        self.restart_button.setEnabled(state == "ready")
        self.retry_button.setVisible(state == "failed")
        self.retry_button.setEnabled(state == "failed")
        self.open_page_button.setVisible(self._release is not None and state in ("available", "failed", "ready"))
        self.cancel_button.setVisible(state == "downloading")
        self.cancel_button.setEnabled(state == "downloading")
        self.progress_row.setVisible(state == "downloading")

    def refresh(self) -> None:
        """Reload the update settings (the window calls this whenever the page is shown)."""
        self.load()

    # ---------- startup ----------

    def auto_check_on_start(self) -> None:
        """Startup policy: collect leftovers, offer a staged update, else check when enabled.

        A staged update is only *offered*: the previous behaviour installed it on exit, and that
        replaced the running executable without the user confirming the one action that matters.
        """
        updates.cleanup_leftovers()
        staged = updates.load_pending()
        if self.current_mode() != "off" and staged is not None:
            self._pending, self._pending_version = staged
            self._show_ready(self._pending_version)
            return
        if self.current_mode() == "off":
            return
        self.check_updates()


# --------------------------------------------------------------------------------------
# The paths that must work without the page
# --------------------------------------------------------------------------------------
#
# Startup used to ask the window's lazy registry for the About page just to run its update check, and
# the close path did the same to install a staged update. Asking for a page with ``pages.get`` builds
# it, so every session constructed the whole About page - credits included - about 2.5 seconds after
# starting, and a close that never opened it built one only to destroy it immediately. The check needs
# no page widgets, only a parent for its status line; the entry point below is that page-free path.
# ``AboutPage.auto_check_on_start`` stays for the case where the page does exist, so its labels and
# buttons keep reflecting the check.
#
# There is deliberately no "install on exit" helper any more. The swap renames the running executable,
# and doing that as a side effect of closing the window was an install the user never confirmed.


def install_pending_on_exit(preferred: Path | None = None) -> bool:
    """Retired: automatic installation on exit is gone (a swap always needs the user's click).

    ``gui.main_window`` still asks this on close; returning False leaves a staged download exactly
    where it is, to be offered on the next start. Kept as a named function (instead of deleting the
    call site) so the window - which this change does not touch - keeps working.
    """
    _ = preferred
    return False


class StartupUpdateCheck(QObject):
    """The startup update check without the About page.

    Follows the same policy as :meth:`AboutPage.auto_check_on_start`: an update staged in an earlier
    session is offered again, and the configured mode decides whether a new release is downloaded or
    only announced. What it has no widgets for is the page's progress bar and labels: every step goes
    to the window's one status line. It never installs anything; the About page owns that action.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self._mode = AboutPage._valid_mode(config.load()[0].auto_update_mode)
        self._downloading = False
        self._pending_version = ""

    def start(self) -> None:
        """Run the session's startup policy: an offer for a staged update, else one check."""
        updates.cleanup_leftovers()
        staged = updates.load_pending()
        if self._mode != "off" and staged is not None:
            _path, version = staged
            set_app_status(self._parent, tr("about.update.install.ready", version))
            return
        if self._mode == "off":
            return
        self._check()

    # ---------- check ----------

    def _check(self) -> None:
        set_app_status(self._parent, tr("about.update.checking"))
        run_in_background(
            updates.check_for_update,
            __version__,
            on_result=self._on_check_result,
        )

    def _on_check_result(self, result: updates.UpdateCheck) -> None:
        if result.status == "failed":
            set_app_status(self._parent, tr(AboutPage._failure_key(result.error)), "error")
            return
        if result.status == "up_to_date":
            checked_at = result.checked_at or datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")
            set_app_status(self._parent, tr("about.update.up_to_date_at", checked_at))
            return
        set_app_status(self._parent, tr("about.update.available", result.latest))
        if result.release is not None and self._mode == "download_notify":
            self._download(result.release)

    # ---------- download ----------

    def _download(self, release: updates.ReleaseInfo) -> None:
        if self._downloading:
            return
        self._downloading = True
        self._pending_version = release.version
        set_app_status(self._parent, tr("about.update.downloading", "0"))
        bridge = ProgressBridge()
        bridge.progress.connect(self._on_download_progress)

        def do_download(progress) -> tuple[str, object]:
            last = 0

            def on_progress(done: int, total: int) -> None:
                nonlocal last
                if done - last >= PROGRESS_STEP or (total and done == total):
                    last = done
                    progress((done, total))

            try:
                path = updates.download_release_exe(release, updates.updates_dir(), on_progress=on_progress)
            except DownloadCancelled:
                return "cancelled", ""
            except updates.UpdateError as exc:
                return "failed", exc.code
            return "ok", path

        run_in_background(
            do_download,
            bridge,
            on_result=self._on_download_result,
            on_finished=self._on_download_finished,
        )

    def _on_download_finished(self) -> None:
        self._downloading = False

    def _on_download_progress(self, payload) -> None:
        done, total = payload
        percent = str(done * 100 // total) if total else "?"
        set_app_status(self._parent, tr("about.update.downloading", percent))

    def _on_download_result(self, payload) -> None:
        status, value = payload
        if status == "cancelled":
            set_app_status(self._parent, tr("about.update.cancelled"))
            return
        if status == "failed":
            set_app_status(self._parent, tr(AboutPage._failure_key(str(value))), "error")
            return
        updates.save_pending(Path(value), self._pending_version)
        set_app_status(self._parent, tr("about.update.install.ready", self._pending_version))


def start_startup_update_check(parent: QWidget) -> StartupUpdateCheck:
    """Create and start the page-free startup check; the caller keeps the returned object alive."""
    check = StartupUpdateCheck(parent)
    check.start()
    return check
