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

"""Retry a project icon that failed to load, while the page keeps running.

A list that did not get its pictures on the first try used to keep the letter tile until the next
rebuild: the failure callback only cleared the "in flight" mark, and nothing asked again. Someone who
stayed on the page - or on several pages in a row - saw the same blank tiles even after the network came
back, which is what "首次启动没下载到图片的" describes.

Each URL gets its own backoff (5 s, 15 s, 60 s, then every 5 min) so a page with forty cold rows does not
turn a dead CDN into a request storm, and a success clears the count. Nothing is written to disk: a
failed icon leaves no negative cache entry, so the next launch tries again by itself.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

#: Waits before the 1st, 2nd, 3rd ... retry; the last value repeats.
BACKOFF_MS: tuple[int, ...] = (5_000, 15_000, 60_000, 300_000)
#: Retries attempted per URL while the page lives. The repetition of the last backoff makes this the
#: only thing that ends the sequence, so the number is deliberate rather than implied by the tuple.
MAX_ATTEMPTS = 8


class IconRetrier(QObject):
    """Retries failed icon fetches for one page, with a backoff and a bounded attempt count."""

    def __init__(self, owner: QObject, retry: Callable[[str], None], parent: QObject | None = None):
        super().__init__(parent if parent is not None else owner)
        self._retry = retry
        #: url -> attempts made so far (a success deletes the entry)
        self._attempts: dict[str, int] = {}

    def failed(self, url: str) -> None:
        """Record one failure and schedule the next attempt, if the budget allows."""
        if not url:
            return
        attempts = self._attempts.get(url, 0)
        if attempts >= MAX_ATTEMPTS:
            return
        self._attempts[url] = attempts + 1
        delay = BACKOFF_MS[min(attempts, len(BACKOFF_MS) - 1)]

        def again() -> None:
            # The page may be gone by now; QTimer.singleShot with a bound method on a deleted QObject
            # is safe in PySide, but the retry callback itself can still touch a torn-down list.
            if self._attempts.get(url) != attempts + 1:
                return  # a later failure rescheduled it already
            self._retry(url)

        QTimer.singleShot(delay, self, again)

    def succeeded(self, url: str) -> None:
        self._attempts.pop(url, None)

    def pending(self) -> int:
        return len(self._attempts)
