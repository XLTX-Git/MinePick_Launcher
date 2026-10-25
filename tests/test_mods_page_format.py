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

"""The two pure formatters behind the resource cards: download counts and "when was it updated".

The list paints "2.88 亿" / "3 周前" instead of "288,000,000" / an ISO timestamp, so these are the
numbers a user actually reads. Both functions take the language and (for the relative time) "now" as
arguments: no QApplication, no clock, no process-wide language switch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gui.pages.mods_page import format_downloads, format_updated

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (999, "999"),
        (10_000, "1万"),
        (123_456, "12.35万"),
        (92_300_000, "9230万"),
        (231_450_000, "2.31亿"),
        (1_000_000_000, "10亿"),
    ],
)
def test_download_counts_are_human_readable_in_chinese(value: int, expected: str) -> None:
    assert format_downloads(value, "zh_cn") == expected


def test_traditional_chinese_uses_its_own_unit_characters() -> None:
    assert format_downloads(231_450_000, "zh_tw") == "2.31億"
    assert format_downloads(92_300_000, "zh_tw") == "9230萬"


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (999, "999"),
        (1_500, "1.5K"),
        (123_456, "123.46K"),
        (1_234_567, "1.23M"),
        (231_450_000, "231.45M"),
        (2_880_000_000, "2.88B"),
    ],
)
def test_download_counts_use_si_suffixes_outside_chinese(value: int, expected: str) -> None:
    for lang in ("en_us", "de_de", "fr_fr", "es_es", "ru_ru", "ja_jp", "ko_kr"):
        assert format_downloads(value, lang) == expected


def test_negative_counts_never_render_a_minus_sign() -> None:
    assert format_downloads(-5, "en_us") == "0"


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=40), "刚刚"),
        (timedelta(minutes=5), "5 分钟前"),
        (timedelta(hours=3), "3 小时前"),
        (timedelta(days=2), "2 天前"),
        (timedelta(days=21), "3 周前"),
        (timedelta(days=100), "3 个月前"),
        (timedelta(days=800), "2 年前"),
    ],
)
def test_relative_time_is_human_readable_in_chinese(delta: timedelta, expected: str) -> None:
    stamp = (NOW - delta).isoformat().replace("+00:00", "Z")
    assert format_updated(stamp, lang="zh_cn", now=NOW) == expected


def test_relative_time_follows_the_language() -> None:
    stamp = (NOW - timedelta(days=21)).isoformat().replace("+00:00", "Z")
    assert format_updated(stamp, lang="en_us", now=NOW) == "3 w ago"
    assert format_updated(stamp, lang="de_de", now=NOW) == "vor 3 Wo."
    assert format_updated(stamp, lang="fr_fr", now=NOW) == "il y a 3 sem."


def test_a_timestamp_in_the_future_reads_as_just_now() -> None:
    stamp = (NOW + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    assert format_updated(stamp, lang="zh_cn", now=NOW) == "刚刚"


@pytest.mark.parametrize("value", ["", "   ", "not-a-date", "2024-13-45T99:00:00Z"])
def test_missing_or_invalid_timestamps_render_nothing(value: str) -> None:
    """The card drops the segment instead of showing a placeholder or a raw ISO string."""
    assert format_updated(value, lang="zh_cn", now=NOW) == ""


def test_timestamp_with_an_explicit_offset_is_accepted() -> None:
    # CurseForge uses the same shape; a +02:00 offset must still resolve to 3 hours.
    assert format_updated("2026-10-25T11:00:00+02:00", lang="zh_cn", now=NOW) == "3 小时前"
