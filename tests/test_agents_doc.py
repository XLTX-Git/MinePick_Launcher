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

"""AGENTS.md must keep pointing at files that exist.

An agent guide is read by someone who cannot tell a stale path from a real one: they will run the
command, get "file not found", and spend the next ten minutes deciding whether the project or the
document is broken. This walks every repository path the document mentions in backticks and checks it
is there. Two of the paths in the first draft were wrong (a tool under the wrong directory, an archive
described in the folder it is copied to rather than the one it is built in), which is exactly the drift
this catches.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "AGENTS.md"

#: Paths that are deliberately illustrative rather than real (``docs/<name>.md``, ``audit_*.py``).
_PLACEHOLDER = re.compile(r"[<>*]")
#: First path segments that mean "a repository path" rather than prose.
_SEGMENTS = (
    "launcher/",
    "gui/",
    "tests/",
    "tools/",
    "scripts/",
    "docs/",
    "build/",
    "dist/",
    "Releases/",
)


def _doc() -> str:
    assert DOC.is_file(), "AGENTS.md is missing"
    return DOC.read_text(encoding="utf-8")


def test_agents_doc_exists_and_names_the_commands() -> None:
    """The commands are written with a ``$py`` prefix, so the fragments matter, not the full line."""
    text = _doc()
    for fragment in (
        "-m gui",
        "ruff check launcher gui tests",
        "ui_regression.py --quick",
        "ui_regression.py --list-checks",
        "-m pytest -q",
        "check_readme_sync.py",
        "PyInstaller build_exe.spec",
        "sign_exe.ps1",
    ):
        assert fragment in text, f"AGENTS.md does not mention {fragment!r}"


def test_every_repository_path_it_mentions_exists() -> None:
    missing: list[str] = []
    for span in re.findall(r"`([^`\n]+)`", _doc()):
        if _PLACEHOLDER.search(span) or span.startswith(("http", "Cert:", "CN=")):
            continue
        candidate = span.split(" ")[0].rstrip(".,;:")
        if not candidate.startswith(_SEGMENTS):
            continue
        # A directory reference ends with "/" and is checked as a directory.
        if not (ROOT / candidate).exists():
            missing.append(candidate)
    assert not missing, f"AGENTS.md points at paths that do not exist: {sorted(set(missing))}"


def test_it_states_the_test_environment_and_the_locales() -> None:
    text = _doc()
    for needle in ("QT_QPA_PLATFORM", "ws_tmp", "offscreen"):
        assert needle in text, f"AGENTS.md does not mention {needle!r}"
    for code in ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr", "ru_ru", "fr_fr", "es_es", "de_de"):
        assert code in text, f"AGENTS.md does not list the {code} locale"


def test_the_source_archive_ships_the_agent_notes() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_source_zip", ROOT / "tools" / "build_source_zip.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert "AGENTS.md" in set(module.INCLUDE_FILES)

def test_it_does_not_name_the_gates_checks() -> None:
    """The checks are printed by --list-checks; listing them here is drift waiting to happen."""
    text = _doc()
    for enumerated in ("corner audit (dark)", "GPL headers", "readme sync (9 translations)"):
        assert enumerated not in text, f"AGENTS.md enumerates the gate's checks: {enumerated!r}"


def test_the_i18n_fallback_rule_is_stated() -> None:
    """The one rule whose absence silently breaks a language."""
    text = _doc().lower()
    assert "fallback is `en_us`" in text
    for call in ("tr_core()", "set_core_language()", "detect_system_language()"):
        assert call.lower() in text, f"AGENTS.md does not name {call}"


def test_it_is_machine_independent() -> None:
    """The file ships in the repository: no absolute paths, no ties to one workstation."""
    text = _doc()
    offenders = re.findall(r"[A-Za-z]:\\[^\s`)]+|/(?:home|Users)/[^\s`)]+", text)
    assert not offenders, f"AGENTS.md hardcodes local paths: {offenders}"
    for private in ("MinePick_Docs", "PCL-CE-reference", ".venv", "dsh-workspace"):
        assert private not in text, f"AGENTS.md leaks a local/private location: {private!r}"

#: CJK ideographs, kana, hangul and full-width punctuation.
_CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]")


def test_the_english_guides_contain_no_cjk() -> None:
    """These two files are loaded into a reader's context verbatim.

    A Chinese sentence in a file an English-speaking agent reads is at best noise and at worst a
    moderation problem for whoever pasted it, so the English guides stay pure ASCII-plus-punctuation.
    The Chinese guide (CONTRIBUTING_zh.md) is where Chinese belongs.
    """
    for name in ("AGENTS.md", "CONTRIBUTING.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        hits = [f"{number}: {line.strip()[:60]}" for number, line in enumerate(text.splitlines(), 1)
                if _CJK.search(line)]
        assert not hits, f"{name} contains CJK text: {hits}"


def test_claude_md_points_at_the_agents_file() -> None:
    """`CLAUDE.md` is a one-line import, not a second copy of the manual.

    Claude Code expands `@AGENTS.md` at load time (and, from v2.1.277, reads `AGENTS.md` on its own
    when no `CLAUDE.md` exists). A bare path on a line is not an import - the model may or may not
    open the file - and a pasted copy would drift from the real manual on the next edit.
    """
    root = Path(__file__).resolve().parent.parent
    pointer = root / "CLAUDE.md"
    assert pointer.is_file(), "CLAUDE.md is missing"
    lines = pointer.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "@AGENTS.md", lines[:3]
    assert len([line for line in lines if line.strip()]) == 1, "CLAUDE.md carries more than the import"
    assert (root / "AGENTS.md").is_file(), "the import target is missing"


def test_the_traps_live_in_the_contributing_guide() -> None:
    """The Qt traps and the product invariants moved to CONTRIBUTING.md: one source, both audiences.

    They are what a contributor - human or agent - needs before touching the GUI, and CONTRIBUTING.md
    is the file a human opens first. AGENTS.md keeps only the pointer, so the text cannot exist twice
    and drift.
    """
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    agents = _doc()
    for heading in ("## Qt traps already paid for", "## Product invariants"):
        assert heading in contributing, f"CONTRIBUTING.md lost the {heading!r} section"
    assert "CONTRIBUTING.md" in agents and "Qt traps" in agents and "Product invariants" in agents, (
        "AGENTS.md no longer points at the moved sections"
    )
    # The two sections are lists; a section that lost its bullets is an empty promise.
    traps = contributing.split("## Qt traps already paid for", 1)[1].split("## Product invariants", 1)[0]
    invariants = contributing.split("## Product invariants", 1)[1].split("\n## ", 1)[0]
    assert traps.count("\n- ") >= 7, f"Qt traps shrank to {traps.count(chr(10) + '- ')} bullets"
    assert invariants.count("\n- ") >= 5, "the product invariants shrank"
    assert "## 已经踩过的 Qt 坑" in (ROOT / "CONTRIBUTING_zh.md").read_text(encoding="utf-8")

