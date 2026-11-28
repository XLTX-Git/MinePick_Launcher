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

"""The UI glossary must match the language tables, row for row.

``docs/glossary.md`` is what makes a Chinese label inside an English comment readable to someone who
does not read Chinese. The table is only useful while it still names the current labels, so each row is
checked against ``gui/i18n.py`` instead of being trusted: the key must exist, the English column must
equal the English string, and the Chinese column must be that label or a shorthand of it (``投影`` for
``投影原理图``).
"""

from __future__ import annotations

import re
from pathlib import Path

from gui import i18n

GLOSSARY = Path(__file__).resolve().parent.parent / "docs" / "glossary.md"
ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*`([A-Za-z0-9_.]+)`\s*\|$")


def _rows() -> list[tuple[str, str, str]]:
    rows = []
    for line in GLOSSARY.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line.strip())
        if match:
            rows.append((match.group(1), match.group(2), match.group(3)))
    return rows


def test_the_glossary_exists_and_covers_the_four_areas() -> None:
    text = GLOSSARY.read_text(encoding="utf-8")
    assert GLOSSARY.is_file(), "docs/glossary.md is missing"
    for heading in ("Main navigation", "Instance detail sidebar", "Actions", "States and labels"):
        assert f"## {heading}" in text, f"missing section: {heading}"
    assert len(_rows()) >= 45, f"the glossary shrank to {len(_rows())} rows"


def test_every_row_matches_the_language_tables() -> None:
    zh = i18n.TRANSLATIONS["zh_cn"]
    en = i18n.TRANSLATIONS["en_us"]
    problems = []
    for chinese, english, key in _rows():
        if key not in zh or key not in en:
            problems.append(f"{key}: no such key")
            continue
        if chinese not in zh[key]:
            problems.append(f"{key}: glossary says {chinese!r}, table says {zh[key]!r}")
        if english != en[key]:
            problems.append(f"{key}: glossary says {english!r}, table says {en[key]!r}")
    assert not problems, "\n".join(problems)


def test_the_glossary_does_not_promise_template_labels() -> None:
    """A label with ``{}`` in it is the caller's number, not a fixed string a comment can quote."""
    zh = i18n.TRANSLATIONS["zh_cn"]
    for _chinese, _english, key in _rows():
        assert "{}" not in zh.get(key, ""), f"{key} is a template and belongs in no row"
