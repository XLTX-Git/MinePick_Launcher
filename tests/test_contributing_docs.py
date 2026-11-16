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

"""The two contribution guides exist, point at each other, and name the real commands.

A contribution guide rots the same way a README does: someone renames a script or drops a check, the
document keeps promising it, and the first person to follow it is the one who finds out. These
assertions are cheap and only cover what can be checked mechanically - the commands must be the ones
this repository actually runs, and the two language versions must stay reachable from one another.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Commands a contributor has to be able to copy from the document verbatim.
REQUIRED_COMMANDS = (
    "pytest -q",
    "ruff check launcher gui tests",
    "python tools/ui_regression.py --quick",
    "pip install -r requirements-dev.txt",
)

#: The interface ships in these languages; a new key needs all of them.
LANGUAGE_CODES = (
    "zh_cn",
    "zh_tw",
    "en_us",
    "ja_jp",
    "ko_kr",
    "ru_ru",
    "fr_fr",
    "es_es",
    "de_de",
)


def _text(name: str) -> str:
    path = ROOT / name
    assert path.is_file(), f"{name} is missing"
    return path.read_text(encoding="utf-8")


def test_both_language_versions_exist_and_link_each_other() -> None:
    english = _text("CONTRIBUTING.md")
    chinese = _text("CONTRIBUTING_zh.md")
    assert "CONTRIBUTING_zh.md" in english
    assert "CONTRIBUTING.md" in chinese


def test_the_guides_name_the_commands_this_repository_runs() -> None:
    for name in ("CONTRIBUTING.md", "CONTRIBUTING_zh.md"):
        text = _text(name)
        for command in REQUIRED_COMMANDS:
            assert command in text, f"{name} does not mention {command!r}"


def test_the_guides_state_the_language_policy() -> None:
    """A new string needs all nine tables - that is the rule this project actually enforces."""
    chinese = _text("CONTRIBUTING_zh.md")
    for code in LANGUAGE_CODES:
        assert code in chinese, f"CONTRIBUTING_zh.md does not list {code}"


def test_the_source_archive_ships_the_guides() -> None:
    """The archive is what a reader downloads to build from source; without these it lacks the rules."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_source_zip", ROOT / "tools" / "build_source_zip.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    included = set(module.INCLUDE_FILES)
    assert {"CONTRIBUTING.md", "CONTRIBUTING_zh.md"} <= included


def test_the_english_and_chinese_guides_cover_the_same_sections() -> None:
    """Both files are translated, not summarised: the same top-level sections must appear in both."""
    english = [line for line in _text("CONTRIBUTING.md").splitlines() if line.startswith("## ")]
    chinese = [line for line in _text("CONTRIBUTING_zh.md").splitlines() if line.startswith("## ")]
    assert len(english) == len(chinese), (
        f"{len(english)} sections in English vs {len(chinese)} in Chinese: one version was edited alone"
    )
