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

"""The font-weight gate must fail on a 13px bold rule, not merely pass on today's sheets.

The gate exists because the two shipped stylesheets report 50 violations on the fix's parent
commit, and zero after it. A checker that only ever saw the clean sheets could be an empty loop;
these tests build the failing case from a synthetic sheet (13px + 600), prove the CLI exits 1 on
it, and separately prove the shipped sheets still parse and pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import audit_font_weight as audit


def _violations(body: str) -> list[dict]:
    return audit.audit_text(body)


def test_a_13px_semibold_rule_is_a_violation():
    found = _violations(
        "QWidget { font-size: 13px; }\n"
        "QLabel#fieldLabel { font-size: 13px; font-weight: 600; }\n"
    )
    assert len(found) == 1
    assert found[0]["selector"] == "QLabel#fieldLabel"
    assert found[0]["font_size"] == 13
    assert found[0]["font_weight"] == 600
    assert found[0]["line"] == 2
    assert found[0]["font_size_source"] == "own font-size"


def test_13px_regular_and_15px_semibold_both_pass():
    assert _violations(
        "QWidget { font-size: 13px; }\n"
        "QLabel#fieldLabel { font-size: 13px; font-weight: 400; }\n"
        "QLabel#sectionTitle { font-size: 15px; font-weight: 600; }\n"
    ) == []


@pytest.mark.parametrize("size,weight", [(11, 700), (12, 600), (13, 500), (13, 700)])
def test_every_small_bold_combination_fails(size: int, weight: int):
    found = _violations(
        f"QWidget {{ font-size: 13px; }}\n"
        f"QLabel#x {{ font-size: {size}px; font-weight: {weight}; }}\n"
    )
    assert len(found) == 1
    assert found[0]["font_weight"] == weight


def test_a_rule_that_inherits_the_base_13px_is_judged_at_13px():
    found = _violations("QWidget { font-size: 13px; }\nQPushButton { font-weight: 600; }\n")
    assert len(found) == 1
    assert found[0]["font_size"] == 13
    assert found[0]["font_size_source"] == "base QWidget size"


def test_an_ancestor_selector_that_raises_the_size_is_honoured():
    assert _violations(
        "QWidget { font-size: 13px; }\n"
        "QWidget#launchPage { font-size: 16px; }\n"
        "QWidget#launchPage QLabel#accountName { font-weight: 600; }\n"
    ) == []


def test_the_selection_variant_inherits_its_base_selector_size():
    found = _violations(
        "QWidget { font-size: 13px; }\n"
        "QLabel#rail { font-size: 13px; }\n"
        'QLabel#rail[state="current"] { font-weight: 700; }\n'
    )
    assert len(found) == 1
    assert found[0]["font_size_source"] == "same selector"


def test_the_named_bold_weight_is_judged_like_700():
    found = _violations(
        "QWidget { font-size: 13px; }\n"
        "QLabel#x { font-size: 13px; font-weight: bold; }\n"
    )
    assert len(found) == 1
    assert found[0]["font_weight"] == 700


@pytest.mark.parametrize("sheet", audit.SHEETS)
def test_the_shipped_sheets_hold_no_small_bold_rule(sheet: str):
    counted, found = audit.audit_sheet(ROOT / sheet)
    assert counted >= 20, f"{sheet} parsed only {counted} weight rules - the gate would be weak"
    assert found == [], f"{sheet}: " + "; ".join(
        f"line {item['weight_line']} {item['selector']} {item['font_weight']} at "
        f"{item['font_size']:g}px"
        for item in found
    )


def test_cli_exits_1_on_a_synthetic_failing_sheet(ws_tmp):
    sheet = ws_tmp / "bad.qss"
    sheet.write_text(
        "QWidget { font-size: 13px; }\n"
        "QLabel#fieldLabel { font-size: 13px; font-weight: 600; }\n",
        encoding="utf-8",
    )
    assert audit.main(["--sheet", str(sheet)]) == 1


def test_cli_exits_0_on_a_synthetic_clean_sheet(ws_tmp):
    sheet = ws_tmp / "good.qss"
    sheet.write_text(
        "QWidget { font-size: 13px; }\n"
        "QLabel#fieldLabel { font-size: 13px; font-weight: 400; }\n",
        encoding="utf-8",
    )
    assert audit.main(["--sheet", str(sheet)]) == 0


def test_an_empty_sheet_fails_instead_of_passing_vacuously(ws_tmp):
    sheet = ws_tmp / "empty.qss"
    sheet.write_text("QWidget { color: #ffffff; }\n", encoding="utf-8")
    assert audit.main(["--sheet", str(sheet)]) == 1
