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

"""The nine READMEs must describe the version folds, not the removed category tabs.

The versions page replaced its category chips with four collapsible groups. The category-tabs
sentence was copied into every translation, so correcting only one file would leave eight live
descriptions of a control the user cannot find. Each language is pinned twice: the stale phrase is
gone *and* the fold wording is present, because deleting the bullet would also make the old phrase
disappear while silently dropping the rest of the versions feature line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: file -> (phrase that is now false, phrase that describes the four folds)
CLAIMS = {
    "README.md": ("category tabs", "collapsible version groups"),
    "README_de.md": ("Kategorie-Tabs", "aufklappbaren Versionsgruppen"),
    "README_es.md": ("pestañas por categoría", "grupos de versiones plegables"),
    "README_fr.md": ("onglets par catégorie", "groupes de versions repliables"),
    "README_ja.md": ("カテゴリタブ", "折りたたみ式のバージョングループ"),
    "README_ko.md": ("분류 탭", "접을 수 있는 버전 그룹"),
    "README_ru.md": ("вкладками категорий", "раскрывающимися группами версий"),
    "README_zh.md": ("分类页签", "按类型分组的可折叠版本列表"),
    "README_zh_TW.md": ("分類頁籤", "按類型分組的可摺疊版本清單"),
}


def test_all_nine_readmes_are_covered():
    """A tenth README, or a renamed one, must not silently escape this check."""
    assert {path.name for path in ROOT.glob("README*.md")} == set(CLAIMS)


@pytest.mark.parametrize("name", sorted(CLAIMS))
def test_readme_describes_the_fold_groups(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    stale_claim, fold_claim = CLAIMS[name]
    assert stale_claim not in text, f"{name} still promises the removed category tabs"
    assert fold_claim in text, f"{name} lost the version bullet instead of correcting it"
