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

"""Cross-reference checks for the development-process document (it lives outside this repo).

The document accumulated four self-contradictions that no test could see: an undefined category label
in §8, two different 2026-09-15 pitfall ranges in §11, §3 pointing at §2 for a rule that lives in §7,
and §6 saying four i18n fallbacks while §12 said three. These checks are deliberately mechanical and
skipped when the document is not present, so the repository's test suite stays self-contained.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

SOP = Path(
    os.environ.get(
        "MINEPICK_PROCESS_DOC",
        r"D:\dsh-workspace\MinePick_Docs\MinePick_开发标准流程.md",
    )
)

pytestmark = pytest.mark.skipif(not SOP.exists(), reason="process document is not on this machine")


def _fail(message: str) -> None:
    pytest.fail(f"{SOP.name}: {message}")


def test_section_8_declares_the_seventh_category():
    text = SOP.read_text(encoding="utf-8")
    section8 = text[text.index("## 8.") : text.index("## 9.")]
    assert "### 流程维护" in section8, "§8 has no heading for its seventh category"
    assert "§8 的分类共七类" in section8, "§8 does not state the seven categories"
    labels = re.findall(r"\(流程维护\)", section8)
    assert len(labels) >= 15, f"only {len(labels)} entries keep the (流程维护) label"
    assert "七类之一" in text and "流程维护" in text[text.index("## 10.") : text.index("## 11.")]


def test_section_11_same_day_ranges_agree():
    text = SOP.read_text(encoding="utf-8")
    section11 = text[text.index("## 11.") : text.index("## 12.")]
    # Historical rows wrap each tag in its own backtick pair; compare the ranges themselves.
    normalized = section11.replace("`", "")
    assert "[P1]–[P21]" not in normalized, "the 2026-09-15 import cannot include P21 (added later)"
    assert "[P1]…[P12]" not in normalized, "the 2026-09-15 numbering range disagrees with the import"
    assert "[P1]–[P14]" in normalized
    assert "[P1]…[P14]" in normalized


def test_section_3_points_at_section_7_for_the_external_source_rule():
    text = SOP.read_text(encoding="utf-8")
    section3 = text[text.index("## 3.") : text.index("## 4.")]
    lines = [line for line in section3.splitlines() if "参考外部源码" in line]
    assert lines, "§3 no longer references the external-source rule"
    assert any("§7" in line for line in lines), "§3 should point at §7"
    assert not any("§2" in line for line in lines), "§3 still points at §2"


def test_i18n_fallback_count_is_four_in_both_sections():
    text = SOP.read_text(encoding="utf-8")
    section6 = text[text.index("## 6.") : text.index("## 7.")]
    section12 = text[text.index("## 12.") :]
    assert "四处兜底" in section6
    assert "三处兜底" not in text, "an old 'three places' claim survived"
    line = next(line for line in section12.splitlines() if "语言回退链为英文" in line)
    assert "四处" in line
    assert "set_language()" in line and "set_core_language()" in line
