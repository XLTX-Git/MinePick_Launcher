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

"""Gate: a small-text rule must not ask for a semibold or bold face.

Microsoft YaHei UI ships exactly three weights - Regular, Light, Bold - so every QSS
``font-weight`` from 500 upward resolves to Bold on the Chinese glyphs a label falls back to.
At 13px that is what the settings page's field labels and row titles showed: bold Chinese on a
one-pixel stem, i.e. text that reads smeared rather than emphasised. The rule this gate enforces
is therefore about the size the weight lands on, not about the weight itself:

  * effective ``font-size`` of 13px or less  ->  ``font-weight`` must stay below 500;
  * 600 / 700 stay reserved for titles and buttons at 15px and up.

"Effective" because most rules declare only ``font-weight`` and inherit the size: the sheet's
base ``QWidget`` rule sets 13px, and a page/container selector can raise it. For each weight rule
the size is resolved in this order - its own block, another rule with the same selector (the
selection pseudo-state variant of a styled label), the nearest ancestor selector that sets a
size, then the base ``QWidget`` size. A violation is reported with the selector, both line
numbers and the two values.

Exit status: 0 when no small rule is bold, 1 when one is (or when no weight rule was parsed at
all - an empty sheet must not pass vacuously).

Usage:
    python tools/audit_font_weight.py
    python tools/audit_font_weight.py --sheet gui/resources/style_light.qss
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHEETS = ("gui/resources/style_light.qss", "gui/resources/style.qss")
#: A rule at or below this size must not be bold: see the module docstring.
SMALL_TEXT_PX = 13
#: The first weight that resolves to bold on Microsoft YaHei UI.
BOLD_WEIGHT = 500
#: The sheet's base text size, used when a rule inherits and nothing else declares one.
BASE_SIZE_PX = 13

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_SIZE = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*px", re.IGNORECASE)
_WEIGHT = re.compile(r"font-weight\s*:\s*([A-Za-z0-9]+)", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"\[[^\]]*\]")
# A pseudo-class is a single colon; `::item` is a subcontrol and must survive.
_PSEUDO_CLASS = re.compile(r"(?<!:):[A-Za-z][A-Za-z0-9_-]*")
_WEIGHT_NAMES = {"normal": 400, "lighter": 300, "bold": 700, "bolder": 700}


@dataclass(frozen=True)
class Rule:
    """One ``selector { body }`` block, with the values this gate needs."""

    selector: str
    body: str
    line: int
    weight_line: int | None
    font_size: float | None
    font_weight: int | None


@dataclass(frozen=True)
class Side:
    """Where an effective font size came from, for the report."""

    size: float
    source: str


def _number(text: str) -> int | None:
    name = text.strip().lower()
    if name in _WEIGHT_NAMES:
        return _WEIGHT_NAMES[name]
    try:
        return int(name)
    except ValueError:
        return None


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def parse_rules(text: str) -> list[Rule]:
    """Parse every rule, keeping the line numbers of the selector and of ``font-weight``."""
    stripped = _COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    rules: list[Rule] = []
    for match in _RULE.finditer(stripped):
        selector_text = match.group(1)
        selector = " ".join(selector_text.split())
        rule_line = _line_of(stripped, match.start(1) + len(selector_text) - len(selector_text.lstrip()))
        body = match.group(2)
        weight_line = None
        weight_match = _WEIGHT.search(body)
        if weight_match is not None:
            weight_line = _line_of(stripped, match.start(2) + weight_match.start())
        size_match = _SIZE.search(body)
        rules.append(
            Rule(
                selector=selector,
                body=body,
                line=rule_line,
                weight_line=weight_line,
                font_size=float(size_match.group(1)) if size_match else None,
                font_weight=_number(weight_match.group(1)) if weight_match else None,
            )
        )
    return rules


def _selectors(selector: str) -> list[str]:
    return [part.strip() for part in selector.split(",") if part.strip()]


def _normalise(selector: str) -> str:
    """Selector with attribute filters and pseudo-classes removed, whitespace collapsed."""
    selector = _ATTRIBUTE.sub("", selector)
    selector = _PSEUDO_CLASS.sub("", selector)
    return " ".join(selector.split()).casefold()


def _base_size(rules: list[Rule]) -> float:
    for rule in rules:
        if rule.font_size is not None and _normalise(rule.selector) == "qwidget":
            return rule.font_size
    return float(BASE_SIZE_PX)


def effective_size(rule: Rule, rules: list[Rule], base: float) -> Side:
    """The size a ``font-weight`` in this rule lands on, and where that size came from."""
    if rule.font_size is not None:
        return Side(rule.font_size, "own font-size")
    own = {_normalise(selector) for selector in _selectors(rule.selector)}
    own.discard("")
    for other in rules:
        if other is rule or other.font_size is None:
            continue
        if own & {_normalise(selector) for selector in _selectors(other.selector)}:
            return Side(other.font_size, "same selector")
    # The nearest ancestor selector that declares a size: `QWidget#launchPage QLabel#name`
    # inherits from `QWidget#launchPage` when that rule sets one.
    best: tuple[int, float] | None = None
    for selector in _selectors(rule.selector):
        parts = selector.split()
        for cut in range(len(parts) - 1, 0, -1):
            prefix = _normalise(" ".join(parts[:cut]))
            for other in rules:
                if other.font_size is None:
                    continue
                if prefix in {_normalise(item) for item in _selectors(other.selector)} and (
                    best is None or len(prefix) > best[0]
                ):
                    best = (len(prefix), other.font_size)
    if best is not None:
        return Side(best[1], "inherited from an ancestor selector")
    return Side(base, "base QWidget size")


def audit_rules(rules: list[Rule]) -> list[dict]:
    """Every rule whose effective size is small while its weight resolves to bold."""
    base = _base_size(rules)
    violations = []
    for rule in rules:
        if rule.font_weight is None or rule.font_weight < BOLD_WEIGHT:
            continue
        side = effective_size(rule, rules, base)
        if side.size > SMALL_TEXT_PX:
            continue
        violations.append(
            {
                "selector": rule.selector,
                "line": rule.line,
                "weight_line": rule.weight_line,
                "font_size": side.size,
                "font_size_source": side.source,
                "font_weight": rule.font_weight,
            }
        )
    return violations


def audit_text(text: str) -> list[dict]:
    """Convenience wrapper for tests: parse one synthetic sheet and return its violations."""
    return audit_rules(parse_rules(text))


def audit_sheet(path: Path) -> tuple[int, list[dict]]:
    """(number of rules that set a weight, violations) for one stylesheet."""
    rules = parse_rules(path.read_text(encoding="utf-8"))
    counted = sum(1 for rule in rules if rule.font_weight is not None)
    return counted, audit_rules(rules)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Reject bold faces on small text rules.")
    parser.add_argument(
        "--sheet",
        action="append",
        default=None,
        help="stylesheet to audit (repeatable; defaults to both shipped sheets)",
    )
    args = parser.parse_args(argv)
    sheets = args.sheet or list(SHEETS)

    total = 0
    violations = 0
    for sheet in sheets:
        path = Path(sheet)
        if not path.is_absolute():
            path = ROOT / path
        counted, found = audit_sheet(path)
        total += counted
        violations += len(found)
        print(f"=== {sheet}: {counted} rule(s) set font-weight, {len(found)} violation(s) ===")
        for item in found:
            print(
                f"  line {item['weight_line']:>4} (rule {item['line']:>4})  "
                f"{item['font_weight']:>3} at {item['font_size']:g}px "
                f"({item['font_size_source']})  {item['selector']}"
            )

    print()
    print(f"{total} weight rule(s) inspected across {len(sheets)} sheet(s); violations {violations}")
    if total == 0:
        print("FAIL: no font-weight rule was parsed - the gate would pass vacuously")
        return 1
    print(
        "FAIL: a rule at 13px or less asks for a bold face - use 400"
        if violations
        else "OK: no bold face is requested at 13px or less"
    )
    return 0 if violations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
