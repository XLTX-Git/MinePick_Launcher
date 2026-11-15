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

"""A cold icon that failed is retried while the page lives, with a backoff and a bounded count."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject

from gui import image_retry
from gui.image_retry import IconRetrier


@pytest.fixture()
def retrier(monkeypatch):
    scheduled: list[tuple[int, object]] = []
    monkeypatch.setattr(
        image_retry.QTimer,
        "singleShot",
        staticmethod(lambda delay, receiver, callback: scheduled.append((delay, callback))),
    )
    asked: list[str] = []
    owner = QObject()
    return IconRetrier(owner, asked.append), scheduled, asked


def test_a_failure_schedules_the_first_backoff(retrier) -> None:
    subject, scheduled, asked = retrier
    subject.failed("https://cdn.example/a.png")
    assert [delay for delay, _callback in scheduled] == [image_retry.BACKOFF_MS[0]]
    scheduled[0][1]()
    assert asked == ["https://cdn.example/a.png"]


def test_the_backoff_grows_and_then_repeats(retrier) -> None:
    subject, scheduled, _asked = retrier
    for _ in range(len(image_retry.BACKOFF_MS) + 1):
        subject.failed("https://cdn.example/a.png")
    delays = [delay for delay, _callback in scheduled]
    assert delays[: len(image_retry.BACKOFF_MS)] == list(image_retry.BACKOFF_MS)
    assert delays[-1] == image_retry.BACKOFF_MS[-1]


def test_a_success_stops_the_retries(retrier) -> None:
    subject, scheduled, asked = retrier
    subject.failed("https://cdn.example/a.png")
    subject.succeeded("https://cdn.example/a.png")
    assert subject.pending() == 0
    # The already-scheduled callback is stale: it must not fire after the URL moved on.
    scheduled[0][1]()
    assert asked == []


def test_the_attempt_budget_ends_the_sequence(retrier) -> None:
    subject, scheduled, _asked = retrier
    for _ in range(image_retry.MAX_ATTEMPTS + 3):
        subject.failed("https://cdn.example/a.png")
    assert len(scheduled) == image_retry.MAX_ATTEMPTS
