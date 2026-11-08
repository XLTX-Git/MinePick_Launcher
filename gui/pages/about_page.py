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

"""About page: version and source link, legal notices, credits, and the update check with its settings.

All feedback goes to the single status bar (like every other page). The check and the download run on
worker threads and only report back through queued callbacks; the swap itself is delegated to a helper
script that runs after this process exits, because a running single-file EXE cannot replace itself.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, inwindow_dialog
from gui.widgets import (
    add_page_top_inset,
    build_download_progress,
    disable_keeping_focus,
    set_app_status,
    style_page_layout,
)
from gui.workers import ProgressBridge, run_in_background
from launcher import __version__, config, notices, updates

tr = i18n.tr

SOURCE_URL = f"https://github.com/{updates.REPO}"
LICENSE_URL = f"https://github.com/{updates.REPO}/blob/main/LICENSE"
NOTICES_URL = f"https://github.com/{updates.REPO}/blob/main/THIRD_PARTY_NOTICES.md"

MODE_KEYS: tuple[tuple[str, str], ...] = (
    ("download_install", "about.update.mode.download_install"),
    ("download_notify", "about.update.mode.download_notify"),
    ("notify", "about.update.mode.notify"),
    ("off", "about.update.mode.off"),
)
MODES = {value for value, _key in MODE_KEYS}
DEFAULT_MODE = "download_notify"
FAILURE_CODES = ("network", "http", "parse", "no_asset", "size", "signature")
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
        self._checking = False
        self._downloading = False

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

        outer = QVBoxLayout(self)
        style_page_layout(outer)
        add_page_top_inset(outer)
        outer.addWidget(scroll)

        self.load()
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

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

        self.check_button = QPushButton(tr("about.update.check"))
        self.check_button.clicked.connect(self.check_updates)
        self.open_page_button = QPushButton(tr("about.update.open_page"))
        self.open_page_button.setObjectName("secondaryButton")
        self.open_page_button.clicked.connect(self.open_release_page)
        self.open_page_button.setVisible(False)
        button_row = QHBoxLayout()
        button_row.addWidget(self.check_button)
        button_row.addWidget(self.open_page_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.progress_row, self.progress_bar, self.progress_text = build_download_progress("updateProgress")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_row.setVisible(False)
        layout.addWidget(self.progress_row)

        # The mode label is a form label, not a second section heading: it names the field under it.
        mode_title = QLabel(tr("about.update.mode.title"))
        mode_title.setObjectName("formLabel")
        layout.addWidget(mode_title)
        self.mode_combo = QComboBox()
        # The QGroupBox this card used to be gave the combo its accessible name (the group title).
        # A plain card has no such title to inherit, so name the field explicitly (WCAG 4.1.2).
        self.mode_combo.setAccessibleName(tr("about.update.mode.title"))
        for value, key in MODE_KEYS:
            self.mode_combo.addItem(tr(key), value)
        layout.addWidget(self.mode_combo)
        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("hint")
        self.mode_hint.setWordWrap(True)
        layout.addWidget(self.mode_hint)
        return box

    def _build_credits(self) -> QFrame:
        box, layout = self._card("about.credits.title")

        for group_key, entries in notices.groups():
            heading = QLabel(tr(group_key))
            heading.setObjectName("sectionTitle")
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
        self.mode_combo.blockSignals(True)
        index = self.mode_combo.findData(self._valid_mode(cfg.auto_update_mode))
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
        self._update_mode_hint()

    @staticmethod
    def _valid_mode(value: str) -> str:
        return value if value in MODES else DEFAULT_MODE

    def current_mode(self) -> str:
        return self._valid_mode(str(self.mode_combo.currentData() or DEFAULT_MODE))

    def _on_mode_changed(self) -> None:
        cfg, cfg_path = config.load()
        cfg.auto_update_mode = self.current_mode()
        config.save(cfg, cfg_path)
        self._update_mode_hint()

    def _update_mode_hint(self) -> None:
        self.mode_hint.setText(tr(f"{dict(MODE_KEYS)[self.current_mode()]}.desc"))

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
        if self._checking or self._downloading:
            return
        self._checking = True
        disable_keeping_focus(self.check_button)
        set_app_status(self, tr("about.update.checking"))
        run_in_background(
            updates.check_for_update,
            __version__,
            on_result=self._on_check_result,
            on_finished=self._on_check_finished,
        )

    def _on_check_finished(self) -> None:
        self._checking = False
        self.check_button.setEnabled(True)

    def _on_check_result(self, result: updates.UpdateCheck) -> None:
        if result.status == "failed":
            set_app_status(self, tr(self._failure_key(result.error)), "error")
            return
        self.latest_label.setText(tr("about.update.latest", result.latest))
        if result.status == "up_to_date":
            self._release = None
            self.open_page_button.setVisible(False)
            set_app_status(self, tr("about.update.up_to_date"))
            return
        self._release = result.release
        self.open_page_button.setVisible(True)
        set_app_status(self, tr("about.update.available", result.latest))
        self._handle_available(result.release)

    @staticmethod
    def _failure_key(code: str) -> str:
        return f"about.update.failed.{code if code in FAILURE_CODES else 'network'}"

    def _handle_available(self, release: updates.ReleaseInfo | None) -> None:
        """Act on a new release according to the configured mode."""
        if release is None:
            return
        if self.current_mode() in ("download_install", "download_notify"):
            self._start_download(release)
        else:
            self._prompt_available(release)

    # ---------- download ----------

    def _start_download(self, release: updates.ReleaseInfo) -> None:
        if self._downloading:
            return
        self._downloading = True
        self.progress_bar.setValue(0)
        self.progress_row.setVisible(True)
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
                path = updates.download_release_exe(release, updates.updates_dir(), on_progress=on_progress)
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
            on_error=self._on_download_failed,
            on_finished=self._on_download_finished,
        )

    def _on_download_finished(self) -> None:
        self._downloading = False
        self.check_button.setEnabled(True)
        self.progress_row.setVisible(False)

    def _on_download_progress(self, payload) -> None:
        done, total = payload
        if total:
            self.progress_bar.setValue(int(done * 1000 / total))
            set_app_status(self, tr("about.update.downloading", str(done * 100 // total)))
        else:
            set_app_status(self, tr("about.update.downloading", "?"))

    def _on_download_result(self, payload) -> None:
        status, value = payload
        version = self._pending_version
        if status == "failed":
            self._on_download_failed(str(value))
            return
        self._pending = Path(value)
        updates.save_pending(self._pending, version)
        if self.current_mode() == "download_install":
            if not updates.can_self_update():
                # never promise an install-on-exit that cannot happen (source checkout / read-only)
                set_app_status(self, tr("about.update.install_dev"), "warning")
                self.open_page_button.setVisible(True)
                return
            set_app_status(self, tr("about.update.install_on_exit"))
            return
        set_app_status(self, tr("about.update.downloaded", version))
        self._prompt_install(version)

    def _on_download_failed(self, code: str) -> None:
        set_app_status(self, tr(self._failure_key(str(code))), "error")
        if self._release is not None:
            self.open_page_button.setVisible(True)

    # ---------- prompts ----------

    def _prompt_available(self, release: updates.ReleaseInfo) -> None:
        outcome = inwindow_dialog.confirm(
            self,
            tr("about.update.prompt.title"),
            tr("about.update.prompt.message", release.version),
            accept_text=tr("about.update.open_page"),
            reject_text=tr("about.update.prompt.later"),
        )
        if outcome is inwindow_dialog.Outcome.ACCEPTED:
            self._open(release.page_url)

    def _prompt_install(self, version: str) -> None:
        if not updates.can_self_update():
            set_app_status(self, tr("about.update.install_dev"), "warning")
            self.open_page_button.setVisible(True)
            return
        outcome = inwindow_dialog.confirm(
            self,
            tr("about.update.prompt.title"),
            tr("about.update.downloaded", version),
            accept_text=tr("about.update.install_now"),
            reject_text=tr("about.update.install_later"),
        )
        if outcome is inwindow_dialog.Outcome.ACCEPTED:
            self.install_now()

    # ---------- install ----------

    def _staged_update(self) -> Path | None:
        if self._pending is not None and self._pending.is_file():
            return self._pending
        staged = updates.load_pending()
        return staged[0] if staged is not None else None

    def install_now(self) -> None:
        """Swap the running EXE through a detached helper, then quit; failure keeps everything as is."""
        pending = self._staged_update()
        if pending is None:
            return
        if not install_staged_update(self, pending):
            self.open_page_button.setVisible(True)

    def has_pending_install(self) -> bool:
        """Whether an update is staged (the window uses this when deciding about installing on exit)."""
        return bool(self._staged_update())

    def install_pending_on_exit(self) -> None:
        """Install a staged update when the app closes (the "download and install" mode)."""
        preferred = self._pending if self._pending is not None and self._pending.is_file() else None
        install_pending_on_exit(preferred)

    def auto_check_on_start(self) -> None:
        """Startup check; the "off" mode really means no automatic check.

        An update that was downloaded in an earlier session is offered again instead of being
        downloaded twice (the user may have answered "later" to the prompt).
        """
        updates.cleanup_backups(Path(sys.executable))
        staged = updates.load_pending()
        if staged is not None and self.current_mode() != "off":
            self._pending, self._pending_version = staged
            if self.current_mode() != "download_install":  # that mode installs it on exit
                self._prompt_install(self._pending_version)
            return
        if self.current_mode() == "off":
            return
        self.check_updates()

    def refresh(self) -> None:
        """Reload the update settings (the window calls this whenever the page is shown)."""
        self.load()


# --------------------------------------------------------------------------------------
# The paths that must work without the page
# --------------------------------------------------------------------------------------
#
# Startup used to ask the window's lazy registry for the About page just to run its update check, and
# the close path did the same to install a staged update. Asking for a page with ``pages.get`` builds
# it, so every session constructed the whole About page - credits included - about 2.5 seconds after
# starting, and a close that never opened it built one only to destroy it immediately. The check and
# the exit install need no page widgets, only a parent for their prompts and the window's status
# line; the two entry points below are that page-free path. ``AboutPage.auto_check_on_start`` stays
# for the case where the page does exist, so its labels and buttons keep reflecting the check.


def install_staged_update(parent: QWidget, pending: Path) -> bool:
    """Start the detached updater for ``pending``, then quit; shared by the page and the exit path.

    Returns False when nothing was installed (the caller may want to offer the release page). This
    is the page's old ``install_now`` body, moved out so "install on exit" never needs the page.
    """
    if not pending.is_file():
        return False
    if not updates.can_self_update():
        set_app_status(parent, tr("about.update.install_manual"), "warning")
        return False
    try:
        script = updates.write_install_script(pending, Path(sys.executable), restart=True)
    except OSError:
        set_app_status(parent, tr("about.update.install_manual"), "error")
        return False
    if not updates.launch_install_script(script):
        set_app_status(parent, tr("about.update.install_manual"), "error")
        return False
    updates.clear_pending()
    set_app_status(parent, tr("about.update.install_on_exit"))
    application = QApplication.instance()
    if application is not None:
        QTimer.singleShot(200, application.quit)
    return True


def install_pending_on_exit(preferred: Path | None = None) -> bool:
    """Install a staged update when the app closes ("download and install" mode), page or not.

    Reads the mode from the config (the page's combo writes it there on every change), so it is the
    same decision the page makes. ``preferred`` is the page's in-memory download when it still has
    one; the on-disk staged update is the fallback, and the normal source when no page was built.
    """
    cfg, _ = config.load()
    if AboutPage._valid_mode(cfg.auto_update_mode) != "download_install":
        return False
    if not updates.can_self_update():
        return False
    if preferred is not None and preferred.is_file():
        pending = preferred
    else:
        staged = updates.load_pending()
        if staged is None:
            return False
        pending = staged[0]
    try:
        script = updates.write_install_script(pending, Path(sys.executable), restart=False)
    except OSError:
        return False  # closing must never raise; the staged update stays for the next start
    if not updates.launch_install_script(script):
        # The staged update stays pending, and the log records why: an update that silently does
        # nothing is worse than one that fails loudly.
        logging.getLogger(__name__).warning(
            "could not start the update helper %s; the staged update stays pending", script
        )
        return False
    return True


class StartupUpdateCheck(QObject):
    """The startup update check without the About page.

    Follows the same policy as :meth:`AboutPage.auto_check_on_start`: an update staged in an earlier
    session is offered again (except in "install on exit" mode, which installs it at close), and the
    configured mode decides whether a new release is downloaded, only announced, or ignored. What it
    has no widgets for is the page's progress bar and labels: progress and results go to the window's
    one status line, and the prompts are the same in-window dialogs every page uses.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self._mode = AboutPage._valid_mode(config.load()[0].auto_update_mode)
        self._downloading = False
        self._pending_version = ""

    def start(self) -> None:
        """Run the session's startup policy: an offer for a staged update, else one check."""
        updates.cleanup_backups(Path(sys.executable))
        staged = updates.load_pending()
        if staged is not None and self._mode != "off":
            path, version = staged
            if self._mode != "download_install":  # that mode installs it on exit
                self._offer_install(path, version)
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
            set_app_status(self._parent, tr("about.update.up_to_date"))
            return
        set_app_status(self._parent, tr("about.update.available", result.latest))
        if self._mode in ("download_install", "download_notify"):
            self._download(result.release)
        else:
            self._offer_page(result.release)

    def _offer_page(self, release: updates.ReleaseInfo | None) -> None:
        page = release.page_url if release is not None else updates.RELEASES_PAGE
        outcome = inwindow_dialog.confirm(
            self._parent,
            tr("about.update.prompt.title"),
            tr("about.update.prompt.message", release.version if release is not None else ""),
            accept_text=tr("about.update.open_page"),
            reject_text=tr("about.update.prompt.later"),
        )
        if outcome is inwindow_dialog.Outcome.ACCEPTED:
            QDesktopServices.openUrl(QUrl(page))

    # ---------- download ----------

    def _download(self, release: updates.ReleaseInfo | None) -> None:
        if release is None or self._downloading:
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
                path = updates.download_release_exe(
                    release, updates.updates_dir(), on_progress=on_progress
                )
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
        if status == "failed":
            set_app_status(self._parent, tr(AboutPage._failure_key(str(value))), "error")
            return
        path = Path(value)
        updates.save_pending(path, self._pending_version)
        if self._mode == "download_install":
            if not updates.can_self_update():
                set_app_status(self._parent, tr("about.update.install_dev"), "warning")
            else:
                set_app_status(self._parent, tr("about.update.install_on_exit"))
            return
        set_app_status(self._parent, tr("about.update.downloaded", self._pending_version))
        self._offer_install(path, self._pending_version)

    # ---------- install ----------

    def _offer_install(self, path: Path, version: str) -> None:
        if not updates.can_self_update():
            set_app_status(self._parent, tr("about.update.install_dev"), "warning")
            return
        outcome = inwindow_dialog.confirm(
            self._parent,
            tr("about.update.prompt.title"),
            tr("about.update.downloaded", version),
            accept_text=tr("about.update.install_now"),
            reject_text=tr("about.update.install_later"),
        )
        if outcome is inwindow_dialog.Outcome.ACCEPTED:
            install_staged_update(self._parent, path)


def start_startup_update_check(parent: QWidget) -> StartupUpdateCheck:
    """Create and start the page-free startup check; the caller keeps the returned object alive."""
    check = StartupUpdateCheck(parent)
    check.start()
    return check
