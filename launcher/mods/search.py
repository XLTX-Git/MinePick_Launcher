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

"""Cross-source search building blocks.

The resource page can search Modrinth, CurseForge, or both at once. Paging, the "All" merge and the
per-source failure isolation live here instead of inside the Qt widget so they are unit-testable
without a QApplication and so both source clients agree on what "page 2" means.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from launcher.mods.models import ModSearchHit, SearchHits

#: One page of a single-source search. The "All" source uses ``ALL_PAGE_SIZE`` per source, so its
#: merged page still holds about one page of rows.
PAGE_SIZE = 30
ALL_PAGE_SIZE = 15

#: The semantic sort keys the dropdown offers. Each client maps them to its own API vocabulary.
SORT_CHOICES: tuple[str, ...] = ("relevance", "downloads", "updated", "newest")

_SOURCE_RANK = {"modrinth": 0, "curseforge": 1}


def page_offset(page: int, page_size: int = PAGE_SIZE) -> int:
    """Zero-based offset for a 1-based page number (page 1 -> 0, page 2 -> page_size)."""
    return max(0, int(page) - 1) * max(1, int(page_size))


def merge_same_titles(hits: Iterable[ModSearchHit]) -> list[ModSearchHit]:
    """Merge both sources without de-duplicating, keeping equal titles next to each other.

    The first time a title appears fixes the group's position, so the merge preserves the sources'
    own ordering instead of alphabetising the page. Inside a group Modrinth comes first; the two
    entries stay separate even when the slug is identical, because their metadata may differ.
    """
    groups: dict[str, list[ModSearchHit]] = {}
    order: list[str] = []
    for hit in hits:
        title = hit.title.strip().casefold()
        # An empty title would otherwise put every untitled hit into one group; the slug keeps those
        # apart while still grouping exact duplicates within one source.
        key = title or "\0" + hit.slug
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(hit)

    merged: list[ModSearchHit] = []
    for key in order:
        group = groups[key]
        group.sort(key=lambda hit: _SOURCE_RANK.get(hit.source, len(_SOURCE_RANK)))
        merged.extend(group)
    return merged


def source_has_next(page: Sequence[ModSearchHit], offset: int, page_size: int) -> bool:
    """Whether another page exists for one source.

    ``SearchHits.total`` is authoritative when the API reports it. The preview tools stub
    ``search_projects`` with a plain list (no ``total``), so a full page is then treated as "there
    may be more" - the next button costs one request and disables itself when the page is short.
    """
    if not page:
        return False
    total = int(getattr(page, "total", 0) or 0)
    if total > 0:
        return offset + len(page) < total
    return len(page) >= page_size


def collect_all_sources(
    sources: Sequence[tuple[str, Callable[[], SearchHits]]],
) -> tuple[list[SearchHits], list[tuple[str, str]]]:
    """Run every source, keeping a failing one from taking the others down.

    Returns the pages that succeeded and ``(source label, message)`` pairs for those that did not.
    The resource page shows the successful rows plus a status line naming the failed source.
    """
    pages: list[SearchHits] = []
    errors: list[tuple[str, str]] = []
    for label, fetch in sources:
        try:
            pages.append(fetch())
        except Exception as exc:  # noqa: BLE001 - one source must never cancel the other
            errors.append((label, str(exc) or exc.__class__.__name__))
    return pages, errors


@dataclass
class PageOutcome:
    """What one page request produced, already merged and with its status information."""

    hits: list[ModSearchHit] = field(default_factory=list)
    has_next: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)
