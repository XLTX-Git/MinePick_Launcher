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

"""The launch page avatar: the letter first, then the account's real skin face in the background.

Three behaviours are pinned here, each with its failure mode:

* a skin account's face replaces the letter once the texture arrives - and the download never runs on
  the UI thread (the fake dispatcher stands in for the worker pool);
* a failed request or undecodable bytes leave the letter untouched, so the avatar is never blank;
* one skin URL is downloaded once. The cache is process-wide in :mod:`gui.paint_utils`, shared with
  the account page, and an in-flight request is not started a second time.

Switching accounts and logging out are pinned too: a face that arrives late must not paint itself
over the next account, and logout returns the slot to "no account".
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QBuffer, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter

SKIN_URL = "https://skins.example.invalid/launch-page-steve.png"
OTHER_SKIN_URL = "https://skins.example.invalid/launch-page-alex.png"


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _clean_skin_cache():
    """The cache is process-wide by design; every case starts from an empty one."""
    from gui import paint_utils

    paint_utils.clear_skin_cache()
    yield
    paint_utils.clear_skin_cache()


class _Download:
    """One recorded ``run_in_background`` call, resolvable by the test instead of a worker thread."""

    def __init__(self, function, options: dict) -> None:
        self.function = function
        self._result = options.get("on_result")
        self._error = options.get("on_error")

    def resolve(self, data: bytes | None) -> None:
        if self._result is not None:
            self._result(data)

    def fail(self, message: str = "offline") -> None:
        if self._error is not None:
            self._error(message)


class _Runner:
    """Stand-in for ``run_in_background``: records the request and never touches the network."""

    def __init__(self) -> None:
        self.calls: list[_Download] = []

    def __call__(self, function, *args, **kwargs):
        self.calls.append(_Download(function, kwargs))


def _skin_png(face: str = "#ff0000", hat: str = "#00ff00") -> bytes:
    """A 64x64 skin with a painted head and a fully opaque hat overlay."""
    image = QImage(64, 64, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(QRect(8, 8, 8, 8), QColor(face))
    painter.fillRect(QRect(40, 8, 8, 8), QColor(hat))
    painter.end()
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _account(name: str, skin_url: str):
    from launcher.auth.models import Account

    account_id = f"{name.lower()}-{abs(hash(skin_url))}"
    return Account(
        id=account_id,
        type="microsoft" if skin_url else "offline",
        username=name,
        uuid=account_id,
        created_at=time.time(),
        skin_url=skin_url,
    )


def _select(account) -> None:
    """Make ``account`` the selected one; None means "no account" (what logout leaves behind)."""
    from launcher import config
    from launcher.auth import AccountStore

    store = AccountStore()
    if account is None:
        store.save({})
        account_id = None
    else:
        store.save({account.id: account})
        account_id = account.id
    cfg, cfg_path = config.load()
    cfg.selected_account = account_id
    config.save(cfg, cfg_path)


def _centre(pixmap) -> QColor:
    image = pixmap.toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


@pytest.fixture()
def launch_page(app, monkeypatch, ws_tmp, downloads):
    """Build a launch page on its own data/game dir, after one account has been selected."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    game = ws_tmp / "game"
    game.mkdir()
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))
    from gui import i18n
    from gui.pages.launch_page import LaunchPage
    from gui.theme import apply_theme

    apply_theme("light")
    i18n.set_language("zh_cn")
    built = []

    def build(account):
        _select(account)
        page = LaunchPage()
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.resize(1000, 610)
        built.append(page)
        return page

    try:
        yield build
    finally:
        for page in built:
            page.close()
            page.deleteLater()
        i18n.set_language("zh_cn")
        apply_theme("dark", None, None, "default")


@pytest.fixture()
def downloads(monkeypatch):
    """Patch the launch page dispatcher before any page is built (the constructor loads the avatar)."""
    import gui.pages.launch_page as launch_page_mod

    runner = _Runner()
    monkeypatch.setattr(launch_page_mod, "run_in_background", runner)
    return runner


def test_the_skin_face_replaces_the_letter_when_the_download_lands(launch_page, downloads):
    """The visible upgrade: letter immediately, then the head cropped out of the downloaded skin."""
    page = launch_page(_account("Steve", ""))
    assert not page.avatar_label.pixmap().isNull(), "the letter avatar was not painted"
    assert downloads.calls == [], "an offline account must not request a skin"

    _select(_account("Steve", SKIN_URL))
    page.refresh_account()
    letter = page.avatar_label.pixmap()
    assert not letter.isNull(), "the letter avatar disappeared while the skin was loading"
    assert len(downloads.calls) == 1
    assert downloads.calls[0].function is not None  # the blocking download, not the UI thread

    downloads.calls[0].resolve(_skin_png())
    face = page.avatar_label.pixmap()
    assert face.cacheKey() != letter.cacheKey(), "the skin download did not replace the letter"
    assert _centre(face).name() == "#00ff00", "the avatar is not the cropped skin face"


def test_a_failed_or_undecodable_skin_keeps_the_letter(launch_page, downloads):
    """No error dialog and no blank slot: the fallback simply stays."""
    page = launch_page(_account("Steve", ""))
    _select(_account("Steve", SKIN_URL))
    page.refresh_account()
    letter = page.avatar_label.pixmap()

    downloads.calls[0].fail("connection reset")
    assert page.avatar_label.pixmap().cacheKey() == letter.cacheKey()

    page.refresh_account()  # failure released the URL: this starts a fresh request
    assert len(downloads.calls) == 2
    letter = page.avatar_label.pixmap()  # refresh repaints the letter, so measure this one
    downloads.calls[1].resolve(b"this is not a PNG")
    assert page.avatar_label.pixmap().cacheKey() == letter.cacheKey()
    assert page.avatar_label.pixmap().width() == 36


def test_one_skin_url_is_downloaded_once(launch_page, downloads):
    """Refreshes share the in-flight request, the cache, and a second page on the same account."""
    page = launch_page(_account("Steve", ""))
    _select(_account("Steve", SKIN_URL))
    page.refresh_account()
    assert len(downloads.calls) == 1

    page.refresh_account()  # while the first request is still in flight
    page.refresh_account()
    assert len(downloads.calls) == 1, "an in-flight skin was requested a second time"

    downloads.calls[0].resolve(_skin_png())
    page.refresh_account()  # now a cache hit
    assert len(downloads.calls) == 1, "a cached skin was downloaded again"
    assert _centre(page.avatar_label.pixmap()).name() == "#00ff00"

    second = launch_page(_account("Steve", SKIN_URL))  # the account is still selected
    assert len(downloads.calls) == 1, "the process-wide cache was not shared with a second page"
    assert _centre(second.avatar_label.pixmap()).name() == "#00ff00"


def test_a_late_skin_does_not_overwrite_the_account_that_replaced_it(launch_page, downloads):
    """Switch accounts while the first download is running: its result must be ignored."""
    page = launch_page(_account("Steve", ""))
    _select(_account("Steve", SKIN_URL))
    page.refresh_account()
    _select(_account("Alex", OTHER_SKIN_URL))
    page.refresh_account()
    assert len(downloads.calls) == 2

    downloads.calls[0].resolve(_skin_png(hat="#00ff00"))  # Steve's late answer
    assert _centre(page.avatar_label.pixmap()).name() != "#00ff00", "a stale face won the slot"

    downloads.calls[1].resolve(_skin_png(hat="#0000ff"))
    assert _centre(page.avatar_label.pixmap()).name() == "#0000ff"

    _select(_account("Steve", ""))  # back to an offline account: the letter returns
    page.refresh_account()
    assert _centre(page.avatar_label.pixmap()).name() != "#0000ff"


def test_logout_returns_the_avatar_slot_to_empty(launch_page, downloads):
    """The launch page follows the account change to "no account", not just "no skin"."""
    page = launch_page(_account("Steve", ""))
    _select(_account("Steve", SKIN_URL))
    page.refresh_account()
    downloads.calls[0].resolve(_skin_png())
    assert not page.avatar_label.pixmap().isNull()

    _select(None)
    page.refresh_account()
    assert page.avatar_label.pixmap().isNull(), "the avatar survived logout"
    assert page.avatar_label.text() == ""


def test_the_account_page_reuses_the_launch_pages_cached_skin(app, monkeypatch, ws_tmp):
    """The cache lives between the pages, so opening the account page never downloads again."""
    from gui import paint_utils

    paint_utils.finish_skin(SKIN_URL, _skin_png())  # what a launch-page download would leave behind
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(ws_tmp / "game"))
    _select(_account("Steve", SKIN_URL))

    import gui.pages.login_page as login_page_mod
    from gui.pages.login_page import LoginPage
    from gui.theme import apply_theme

    calls = []
    monkeypatch.setattr(
        login_page_mod, "run_in_background", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    apply_theme("light")
    try:
        page = LoginPage()
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.resize(1000, 610)
        page.refresh()
        assert calls == [], "the account page downloaded a skin the launch page already had"
        assert _centre(page.avatar_label.pixmap()).name() == "#00ff00"
    finally:
        apply_theme("dark", None, None, "default")
