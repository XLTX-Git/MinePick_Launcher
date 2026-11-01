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

"""No README may still promise a page header: top-level pages no longer draw one.

The claim was identical in all nine languages and had been copied into the release docs for months
after the header band was removed, so fixing only the English file would leave eight live copies of a
feature the user cannot find. The test pins both halves: the old claim is gone and the layout bullet
still exists (a deleted bullet would also make the old sentence disappear, but it would silently
drop the spacing/magnifier claims that are still true).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: file -> (old claim that is now false, bullet heading that must survive the edit)
CLAIMS = {
    "README.md": ("a page header with a one-line description", "**Clearer layout**"),
    "README_de.md": ("ein Seitenkopf mit einzeiliger Beschreibung", "**Klareres Layout**"),
    "README_es.md": (
        "un encabezado de página con una descripción de una línea",
        "**Diseño más claro**",
    ),
    "README_fr.md": (
        "un en-tête de page avec une description en une ligne",
        "**Mise en page plus claire**",
    ),
    "README_ja.md": ("1 行の説明付きページヘッダー", "**より分かりやすいレイアウト**"),
    "README_ko.md": ("한 줄 설명이 있는 페이지 헤더", "**더 명확해진 레이아웃**"),
    "README_ru.md": ("заголовок с описанием в одну строку", "**Более понятная вёрстка**"),
    "README_zh.md": ("每页顶部有大标题与一句说明", "**版式更清楚**"),
    "README_zh_TW.md": ("每一頁都有頁面標題與一行說明", "**版面更清楚**"),
}


def test_all_nine_readmes_are_covered():
    """A tenth README, or a renamed one, must not silently escape this check."""
    assert {path.name for path in ROOT.glob("README*.md")} == set(CLAIMS)


@pytest.mark.parametrize("name", sorted(CLAIMS))
def test_readme_no_longer_claims_a_page_header(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    stale_claim, heading = CLAIMS[name]
    assert stale_claim not in text, f"{name} still promises the removed page header"
    assert heading in text, f"{name} lost the layout bullet instead of correcting it"
