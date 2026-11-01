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

"""Chinese display names and Chinese search for the resource page.

Data file: ``launcher/mods/data/zh_mod_names.json`` - ``slug -> [alias, ...]``, where the first
alias is the name a row shows as its main title and the remaining ones are the other spellings a
user might type (the English title, a short form).

Where the table comes from (public pages, fetched 2026-11-01):

* ``https://www.mcmod.cn/modlist.html?page=1..120`` - MC百科's mod list, which prints both the
  Chinese name and the English name of every project it lists.
* ``https://search.mcmod.cn/s?key=<english title>`` - the same pairing for the classic projects
  that sit too deep in that list, read from the result title "中文名 (English name)".
* ``https://api.modrinth.com/v2/projects?ids=[...]`` - decides which of those projects exist on
  Modrinth and under which slug; only a pair whose project title equals the published English
  name is kept.

Only that "slug <-> 中文名" pairing is bundled: no database file, description or other content is
copied, and a name MC百科 does not publish is simply left out rather than translated here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from launcher import paths

#: The three matching layers, best first. ``EXACT`` is a whole-field hit, ``SUBSTRING`` is a
#: partial one in either direction, ``TOKEN`` means every word of a compound query was found.
EXACT = 0
SUBSTRING = 1
TOKEN = 2

#: A query segment is looked up in the table's own vocabulary up to this length before the
#: tokenizer falls back to plain two-character chunks (a Chinese compound has no spaces, so the
#: table is the only dictionary available - and adding a real one would mean a new dependency).
_MAX_TERM = 4

_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

#: Scores only order entries inside one layer; the layer itself always wins.
_EXACT_BASE = 100
_QUERY_INSIDE_BASE = 60
_FIELD_INSIDE_BASE = 40


@dataclass(frozen=True)
class Match:
    """One entry the local table matched: its slug, display name, layer and score."""

    slug: str
    name: str
    tier: int
    score: int


@lru_cache(maxsize=1)
def zh_aliases() -> dict[str, tuple[str, ...]]:
    """The slug -> aliases table (bundled data); empty when the file is missing."""
    data_path = paths.resource_path("launcher/mods/data/zh_mod_names.json")
    try:
        raw = json.loads(data_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - treat missing data as an empty table
        return {}
    table: dict[str, tuple[str, ...]] = {}
    for slug, value in raw.items():
        # A plain string is the old one-name-per-slug shape; still accepted so a stale cache of
        # the file never breaks the page.
        items = [value] if isinstance(value, str) else list(value or ())
        aliases = tuple(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))
        if slug and aliases:
            table[str(slug)] = aliases
    return table


def clear_caches() -> None:
    """Drop the loaded table (tests patch the data file; the process itself never needs this)."""
    zh_aliases.cache_clear()
    _vocabulary.cache_clear()
    tokenize.cache_clear()


@lru_cache(maxsize=1)
def zh_names() -> dict[str, str]:
    """slug -> the name shown as the row's main title (the first alias)."""
    return {slug: aliases[0] for slug, aliases in zh_aliases().items()}


def zh_name(slug: str) -> str:
    """The Chinese name for a slug; an empty string when the table has none."""
    return zh_names().get(slug, "")


def aliases_for(slug: str) -> tuple[str, ...]:
    """Every spelling recorded for a slug (Chinese name, English title, short forms)."""
    return zh_aliases().get(slug, ())


def has_cjk(text: str) -> bool:
    """Whether the text contains CJK unified ideographs (Chinese input -> local search first)."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text))


def _normalize(text: str) -> str:
    return str(text).casefold().strip()


def _fields_text(fields) -> list[str]:
    return [text for text in (_normalize(field) for field in fields) if text]


@lru_cache(maxsize=1)
def _vocabulary() -> frozenset[str]:
    """Every alias and slug of the table, plus the 2..4 character pieces of the Chinese ones.

    The pieces are what lets "实体渲染优化" be cut into 实体渲染 / 优化 even when no table entry is
    itself spelled that way: the table is the dictionary, and its substrings are the words.
    Latin names contribute their whole spelling only - their pieces would be noise.
    """
    words: set[str] = set()
    for slug, aliases in zh_aliases().items():
        for text in (slug, *aliases):
            normalized = _normalize(text)
            if not normalized:
                continue
            words.add(normalized)
            if not has_cjk(normalized):
                continue
            for start in range(len(normalized)):
                for size in range(2, _MAX_TERM + 1):
                    piece = normalized[start : start + size]
                    if len(piece) == size:
                        words.add(piece)
    return frozenset(words)


@lru_cache(maxsize=128)
def tokenize(query: str) -> tuple[str, ...]:
    """Split a query into the words the table knows, without a segmentation dependency.

    Latin text splits on spaces and punctuation. A Chinese run has no separators, so the longest
    table term starting at each position is taken; when the table has nothing there, two
    characters are kept together (they are more useful as one probe than as two single ones).
    """
    text = _normalize(query)
    if not text:
        return ()
    if not has_cjk(text):
        return tuple(part for part in _SPLIT.split(text) if part)
    vocabulary = _vocabulary()
    tokens: list[str] = []
    for run in _SPLIT.split(text):
        if not run:
            continue
        if not has_cjk(run):
            tokens.append(run)
            continue
        tokens.extend(_segment(run, vocabulary))
    return tuple(tokens)


def _segment(run: str, vocabulary: frozenset[str]) -> list[str]:
    """Cut one run of Chinese characters into table terms, longest match first."""
    tokens: list[str] = []
    index = 0
    while index < len(run):
        chunk = ""
        for size in range(min(_MAX_TERM, len(run) - index), 1, -1):
            candidate = run[index : index + size]
            if candidate in vocabulary:
                chunk = candidate
                break
        if not chunk:
            remaining = len(run) - index
            chunk = run[index : index + 2] if remaining > 1 else run[index:]
        tokens.append(chunk)
        index += len(chunk)
    return tokens


def name_match(query: str, fields) -> tuple[int, int] | None:
    """How one entry's name fields answer a query: ``(tier, score)`` or None.

    ``fields`` is every spelling of the entry (slug, English title, Chinese aliases). The three
    layers are tried in order, so an exact name always beats a partial one and a partial one
    always beats a word-by-word hit - which is what keeps the row a user meant at the top.
    """
    text = _normalize(query)
    if not text:
        return None
    texts = _fields_text(fields)
    if not texts:
        return None
    if text in texts:
        return (EXACT, _EXACT_BASE + len(text))
    best: tuple[int, int] | None = None
    for candidate in texts:
        if text in candidate:
            # The query is part of a longer name: the closer the two lengths, the better.
            score = (
                _QUERY_INSIDE_BASE
                + len(text)
                + (5 if candidate.startswith(text) else 0)
                - min(15, len(candidate) - len(text))
            )
        elif candidate in text:
            # A recorded name is part of what the user typed ("投影" inside "投影仪").
            score = _FIELD_INSIDE_BASE + min(len(candidate), 20)
        else:
            continue
        if best is None or score > best[1]:
            best = (SUBSTRING, score)
    if best is not None:
        return best
    tokens = tokenize(text)
    if not tokens or (len(tokens) == 1 and len(tokens[0]) < 2):
        return None
    haystack = "\n".join(texts)
    covered = 0
    for token in tokens:
        if token not in haystack:
            return None
        covered += len(token)
    return (TOKEN, min(covered, 30))


def match_slugs(query: str, table: dict[str, tuple[str, ...]] | None = None) -> list[Match]:
    """Search the local table: every entry that answers the query, best match first."""
    entries = zh_aliases() if table is None else table
    text = _normalize(query)
    if not text:
        return []
    matches: list[Match] = []
    for slug, aliases in entries.items():
        found = name_match(text, (slug, *aliases))
        if found is None:
            continue
        matches.append(Match(slug, aliases[0] if aliases else slug, found[0], found[1]))
    matches.sort(key=lambda match: (match.tier, -match.score, match.slug))
    return matches


def search_local(query: str) -> list[str]:
    """The slugs the local table finds for a query, best match first."""
    return [match.slug for match in match_slugs(query)]


def english_query(query: str) -> str:
    """The English term to retry a Chinese query with (the best matching slug), "" when none.

    "投影" is not a word Modrinth's index knows; "litematica" is. The table is what turns one
    into the other, so a Chinese search that comes back empty gets a second request.
    """
    matches = match_slugs(query)
    return matches[0].slug if matches else ""
