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

"""Crash-analysis tests: real fixtures, synthetic shapes, the mod index and the stack parser.

The three ``*_real.txt`` files were captured from a real player's crash reports
and sanitized (paths -> ``C:/Users/example``, UUIDs zeroed, separators unified,
truncated under 8 KiB while keeping the Description, the full ``Caused by``
chain and the Fabric mod list). Everything under ``synthetic/`` is marked with
``# synthetic sample: shaped after a real log, not captured from one`` and is
kept only for the causes the real machine did not produce (OOM, 32-bit memory,
driver violations, missing dependencies, resource packs, no exception).
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from launcher.diagnostics import (
    NOTHING_TO_ANALYSE_CODE,
    UNKNOWN_CODE,
    CrashLogSet,
    CrashModIndex,
    DiagnosisContext,
    diagnose,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "crash"
SYNTHETIC = FIXTURES / "synthetic"
SAMPLES = Path(__file__).resolve().parent / "data" / "crash_samples"
SYNTHETIC_MARKER = "# synthetic sample: shaped after a real log, not captured from one"
VERSION_ID = "fabric-loader-0.19.3-1.21.11"


def _game(ws_tmp: Path) -> Path:
    game = ws_tmp / "game"
    game.mkdir(parents=True, exist_ok=True)
    return game


def _context(
    ws_tmp: Path, *, game: Path | None = None, mods: Path | None = None
) -> DiagnosisContext:
    game = game or _game(ws_tmp)
    return DiagnosisContext(
        version_id=VERSION_ID,
        game_dir=game,
        launch_dir=game,
        mods_dir=mods,
        memory_gb=4.0,
        java_major=17,
        launcher_logs_dir=ws_tmp / "launcher-logs",
    )


def _place(ws_tmp: Path, fixture: str, relative: str) -> Path:
    source = FIXTURES / fixture
    target = _game(ws_tmp) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def _write_jar(
    path: Path,
    *,
    mod_id: str,
    name: str,
    classes: tuple[str, ...] = (),
    depends: dict[str, str] | None = None,
    forge: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if forge:
            archive.writestr(
                "META-INF/mods.toml",
                'modLoader="javafml"\nloaderVersion="[47,)"\n'
                f'[[mods]]\nmodId="{mod_id}"\ndisplayName="{name}"\nversion="2.0"\n',
            )
        else:
            archive.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "id": mod_id,
                        "name": name,
                        "version": "1.0",
                        "depends": depends or {},
                    }
                ),
            )
        for entry in classes:
            archive.writestr(entry, b"\xca\xfe\xba\xbe")
    return path


def _evidence_text(diagnosis) -> str:
    return "\n".join(item.line for item in diagnosis.primary.evidence)


# --------------------------------------------------------------------------- #
# Real logs (sanitized, not rewritten)
# --------------------------------------------------------------------------- #
def test_real_fabric_entrypoint_failure(ws_tmp) -> None:
    _place(
        ws_tmp,
        "fabric_entrypoint_real.txt",
        "crash-reports/crash-2026-08-13_11.57.28-client.txt",
    )
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0125"
    assert diagnosis.confidence == "high"
    assert diagnosis.primary.certainty == "certain"
    assert diagnosis.facts.suspect_id == "notebot"
    assert "Could not execute entrypoint stage" in _evidence_text(diagnosis)
    evidence = diagnosis.primary.evidence
    assert any(item.line_number == 5 for item in evidence)
    assert all(item.log_name.startswith("crash-") for item in evidence)
    assert any(action.kind == "open_mods" for action in diagnosis.primary.actions)


def test_real_mixin_injection_failure(ws_tmp) -> None:
    _place(
        ws_tmp,
        "mixin_injection_real.txt",
        "crash-reports/crash-2026-06-22_21.24.01-client.txt",
    )
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0106"
    assert diagnosis.confidence == "high"
    assert diagnosis.facts.suspect_id == "carpet-tis-addition"
    assert "MixinApplyError" in _evidence_text(diagnosis)
    assert any(item.line_number == 31 for item in diagnosis.primary.evidence)
    # The mixin failure names its own mod; the frame that loaded the class does not win.
    assert diagnosis.stack is not None
    assert diagnosis.stack.has_mixin_failure
    assert diagnosis.stack.mixins
    assert any("carpet-tis-addition" in mixin.mods for mixin in diagnosis.stack.mixins)


def test_real_null_pointer_report(ws_tmp) -> None:
    _place(
        ws_tmp,
        "null_pointer_real.txt",
        "crash-reports/crash-2026-06-28_12.29.19-client.txt",
    )
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0132"
    assert diagnosis.primary.certainty == "info"
    assert diagnosis.facts.suspect_id == ""
    assert "NullPointerException" in _evidence_text(diagnosis)
    assert all(item.line_number for item in diagnosis.primary.evidence)
    assert any(action.kind == "open_help" for action in diagnosis.primary.actions)


# --------------------------------------------------------------------------- #
# Synthetic shapes (marked at line 1)
# --------------------------------------------------------------------------- #
def test_synthetic_out_of_memory(ws_tmp) -> None:
    _place(ws_tmp, "synthetic/oom.latest.log", "logs/latest.log")
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0101"
    assert diagnosis.confidence == "high"
    assert diagnosis.primary.certainty == "certain"
    memory_action = next(a for a in diagnosis.primary.actions if a.kind == "set_memory")
    assert memory_action.param("memory") == "6"  # 4 GB configured -> 6 GB suggested
    assert any(item.line_number == 7 for item in diagnosis.primary.evidence)


def test_synthetic_32bit_memory(ws_tmp) -> None:
    _place(ws_tmp, "synthetic/low_memory_32bit.hs_err.log", "hs_err_pid3312.log")
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0102"
    assert diagnosis.confidence == "high"
    assert "insufficient memory" in _evidence_text(diagnosis)
    assert any(item.log_kind == "hs_err" for item in diagnosis.primary.evidence)
    assert any(item.line_number == 2 for item in diagnosis.primary.evidence)


def test_synthetic_gpu_driver_violation(ws_tmp) -> None:
    _place(ws_tmp, "synthetic/gpu_driver.hs_err.log", "hs_err_pid4820.log")
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0103"
    assert diagnosis.confidence == "high"
    assert "EXCEPTION_ACCESS_VIOLATION" in _evidence_text(diagnosis)
    assert any(action.kind == "open_help" for action in diagnosis.primary.actions)


def test_synthetic_missing_dependency(ws_tmp) -> None:
    _place(ws_tmp, "synthetic/missing_dependency.latest.log", "logs/latest.log")
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0104"
    assert "Missing or unsupported mandatory dependencies" in _evidence_text(diagnosis)
    assert any(item.line_number for item in diagnosis.primary.evidence)
    assert any(action.kind == "open_mods" for action in diagnosis.primary.actions)


def test_synthetic_resource_pack(ws_tmp) -> None:
    _place(ws_tmp, "synthetic/resource_pack.latest.log", "logs/latest.log")
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0133"
    assert diagnosis.confidence == "medium"
    assert diagnosis.primary.certainty == "suspected"
    assert "resource pack" in _evidence_text(diagnosis).lower()


def test_synthetic_forge_install_incomplete(ws_tmp) -> None:
    launcher_logs = ws_tmp / "launcher-logs"
    launcher_logs.mkdir(parents=True)
    shutil.copyfile(SYNTHETIC / "forge_incomplete.launcher.log", launcher_logs / "launcher.log")
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.code == "MPL-Crash-0129"
    assert "Forge installation is incomplete" in _evidence_text(diagnosis)


def test_multiple_forge_profiles_are_reported(ws_tmp) -> None:
    game = _game(ws_tmp)
    for profile in ("1.20.1-forge-47.1.0", "1.20.1-forge-47.4.22"):
        (game / "versions" / profile).mkdir(parents=True)
    (game / "logs").mkdir()
    (game / "logs" / "latest.log").write_text(
        "[12:00:00] [main/INFO]: okay\n", encoding="utf-8"
    )

    diagnosis = diagnose(_context(ws_tmp, game=game))

    assert diagnosis.code == "MPL-Crash-0130"
    assert "multiple-forge-profiles:" in _evidence_text(diagnosis)
    assert diagnosis.primary.evidence[0].log_name == "versions/"


def test_clean_log_is_not_a_crash(ws_tmp) -> None:
    _place(ws_tmp, "synthetic/no_exception.latest.log", "logs/latest.log")
    diagnosis = diagnose(_context(ws_tmp))

    assert diagnosis.analysable
    assert diagnosis.code == UNKNOWN_CODE
    assert diagnosis.primary.fallback
    assert not diagnosis.primary.actions


def test_no_logs_do_not_crash(ws_tmp) -> None:
    diagnosis = diagnose(_context(ws_tmp))
    assert diagnosis.code == NOTHING_TO_ANALYSE_CODE
    assert not diagnosis.analysable
    assert diagnosis.primary.fallback
    assert diagnosis.findings

    empty = _game(ws_tmp) / "logs"
    empty.mkdir(parents=True)
    (empty / "latest.log").write_text("", encoding="utf-8")
    again = diagnose(_context(ws_tmp))
    assert again.code == NOTHING_TO_ANALYSE_CODE


def test_a_tiny_log_does_not_crash(ws_tmp) -> None:
    logs = _game(ws_tmp) / "logs"
    logs.mkdir(parents=True)
    (logs / "latest.log").write_text("x\n", encoding="utf-8")

    diagnosis = diagnose(_context(ws_tmp))
    assert diagnosis.analysable
    assert diagnosis.code == UNKNOWN_CODE
    assert diagnosis.primary.fallback


# --------------------------------------------------------------------------- #
# Stack parsing
# --------------------------------------------------------------------------- #
def test_multi_layer_caused_by_and_suppressed(ws_tmp) -> None:
    report = """---- Minecraft Crash Report ----
// Ticking entity

Time: 2026-01-01 12:00:00
Description: Ticking entity

java.lang.RuntimeException: outer
\tat net.minecraft.server.MinecraftServer.tick(MinecraftServer.java:100)
Caused by: java.lang.IllegalStateException: middle
\tat com.example.mod.TickHandler.tick(TickHandler.java:88)
Suppressed: java.io.IOException: suppressed cleanup
\tat com.example.mod.Io.fail(Io.java:12)
Caused by: java.lang.NullPointerException: root
\tat com.example.mod.TickHandler.inner(TickHandler.java:42)
"""
    game = _game(ws_tmp)
    crash = game / "crash-reports" / "crash-2026-01-01_12.00.00-client.txt"
    crash.parent.mkdir(parents=True)
    crash.write_text(report, encoding="utf-8")
    mods = game / "mods"
    _write_jar(
        mods / "examplemod-1.0.jar",
        mod_id="examplemod",
        name="Example Mod",
        classes=("com/example/mod/TickHandler.class", "com/example/mod/Io.class"),
    )

    diagnosis = diagnose(_context(ws_tmp, game=game, mods=mods))
    stack = diagnosis.stack
    assert stack is not None
    kinds = [item.kind for item in stack.exceptions]
    assert kinds == [
        "java.lang.RuntimeException",
        "java.lang.IllegalStateException",
        "java.io.IOException",
        "java.lang.NullPointerException",
    ]
    assert stack.exceptions[2].suppressed is True
    assert stack.root_exception is not None
    assert stack.root_exception.kind.endswith("NullPointerException")
    assert stack.frames_for(2)
    assert stack.suspect_id == "examplemod"
    assert stack.suspect_frame is not None
    assert stack.suspect_frame.log_line == 14
    assert stack.suspect_frame.location.endswith("TickHandler.inner")


def test_mod_index_package_and_name_maps(ws_tmp) -> None:
    mods = ws_tmp / "mods"
    _write_jar(
        mods / "alpha-1.0.jar",
        mod_id="alpha",
        name="Alpha Mod",
        classes=("com/alpha/mod/AlphaThing.class",),
    )
    _write_jar(
        mods / "beta-2.0.jar",
        mod_id="beta",
        name="Beta Mod",
        classes=("com/beta/deep/BetaThing.class",),
    )
    _write_jar(
        mods / "forgeexample-2.0.jar",
        mod_id="forgeexample",
        name="Forge Example",
        classes=("org/example/forge/ForgeThing.class",),
        forge=True,
    )
    # Two mods under the same two-component root (fi.dy.masa) must stay apart.
    _write_jar(
        mods / "malilib-1.0.jar",
        mod_id="malilib",
        name="MaLiLib",
        classes=("fi/dy/masa/malilib/MaLiLib.class",),
    )
    _write_jar(
        mods / "litematica-1.0.jar",
        mod_id="litematica",
        name="Litematica",
        classes=("fi/dy/masa/litematica/Litematica.class",),
    )
    index = CrashModIndex.scan(mods)

    assert index.find_by_class("com.alpha.mod.AlphaThing").mod_id == "alpha"
    assert index.find_by_class("com.beta.deep.BetaThing").mod_id == "beta"
    assert index.find_by_class("org.example.forge.ForgeThing").mod_id == "forgeexample"
    assert index.find_by_class("fi.dy.masa.malilib.MaLiLib").mod_id == "malilib"
    assert index.find_by_class("fi.dy.masa.litematica.Litematica").mod_id == "litematica"
    assert index.find_by_class("com.alpha.mod.AlphaThing$Inner") == index.by_id("alpha")
    # Case-insensitive display name and file name mappings.
    assert index.find_by_name("ALPHA mod").mod_id == "alpha"
    assert index.find_by_name("alpha-1.0.jar").mod_id == "alpha"
    # A mod id used as a package segment also resolves.
    assert index.find_by_class("somewhere.alpha.Thing").mod_id == "alpha"
    assert index.find_by_class("net.minecraft.client.Main") is None


def test_mod_index_anomalies(ws_tmp) -> None:
    mods = ws_tmp / "mods"
    _write_jar(
        mods / "needs.jar",
        mod_id="needs",
        name="Needs",
        depends={"missinglib": "*"},
    )
    _write_jar(mods / "dupe-a.jar", mod_id="dupe", name="Dupe")
    _write_jar(mods / "dupe-b.jar", mod_id="dupe", name="Dupe")
    _write_jar(mods / "conflict-a.jar", mod_id="conflict", name="Alpha")
    _write_jar(mods / "conflict-b.jar", mod_id="conflict", name="Beta")
    (mods / "broken.jar").write_bytes(b"not a zip at all")
    (mods / "assets").mkdir()
    (mods / "loose.class").write_bytes(b"\xca\xfe\xba\xbe")

    index = CrashModIndex.scan(mods)
    kinds = {anomaly.kind for anomaly in index.anomalies}

    assert "invalid-jar" in kinds
    assert "leftover" in kinds
    assert "duplicate-id" in kinds
    assert "id-conflict" in kinds
    assert "missing-dependency" in kinds
    assert any("requires missinglib" in anomaly.line for anomaly in index.anomalies)
    assert any("unpacked directory" in anomaly.line for anomaly in index.anomalies)
    assert index.duplicate_ids
    assert index.invalid_jars


def test_bad_jar_is_a_certain_finding(ws_tmp) -> None:
    game = _game(ws_tmp)
    logs = game / "logs"
    logs.mkdir()
    (logs / "latest.log").write_text("x\n", encoding="utf-8")
    mods = game / "mods"
    mods.mkdir()
    (mods / "broken.jar").write_bytes(b"not a zip")

    diagnosis = diagnose(_context(ws_tmp, game=game, mods=mods))

    assert diagnosis.code == "MPL-Crash-0121"
    assert diagnosis.confidence == "high"
    evidence = diagnosis.primary.evidence
    assert evidence[0].log_name == "broken.jar"
    assert evidence[0].line.startswith("invalid-jar:")
    assert evidence[0].location == "broken.jar"  # no line number for a scanning fact
    assert {action.kind for action in diagnosis.primary.actions} >= {
        "disable_mod",
        "open_mods",
    }


def test_illegal_mod_file_name_is_a_suspected_finding(ws_tmp) -> None:
    from launcher.diagnostics.logset import virtual_log
    from launcher.diagnostics.modindex import _illegal_name

    # Windows cannot create these names, so the detector is exercised directly and
    # the rule through the same virtual "mods" log the index would produce.
    assert _illegal_name("CON.jar") == "reserved device name"
    assert _illegal_name("bad:name.jar") == "illegal character"
    assert _illegal_name("trailing.jar ") == "trailing space or dot"

    log = virtual_log(
        name="CON.jar",
        kind="mods",
        lines=("illegal-name: CON.jar (reserved device name)",),
        numbers=(0,),
    )
    diagnosis = diagnose(_context(ws_tmp), logs=[log], mod_index=CrashModIndex.empty())

    assert diagnosis.code == "MPL-Crash-0122"
    assert diagnosis.primary.certainty == "suspected"


# --------------------------------------------------------------------------- #
# Log set
# --------------------------------------------------------------------------- #
def test_log_set_keeps_only_the_newest_crash_report(ws_tmp) -> None:
    game = _game(ws_tmp)
    reports = game / "crash-reports"
    reports.mkdir(parents=True)
    old = reports / "crash-old.txt"
    new = reports / "crash-new.txt"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    oldest = 1_700_000_000
    old.touch()
    new.touch()
    os.utime(old, (oldest, oldest))
    os.utime(new, (oldest + 60, oldest + 60))

    log_set = CrashLogSet.from_context(_context(ws_tmp, game=game))
    matching = log_set.real_matching_logs()

    assert log_set.crash_report is not None
    assert log_set.crash_report.name == "crash-new.txt"
    assert [log.name for log in matching] == ["crash-new.txt"]
    assert log_set.crash_report.mtime > 0
    assert len(log_set.crash_report.lines) == len(log_set.crash_report.numbers)


@pytest.mark.parametrize(
    "name",
    [
        "oom.latest.log",
        "low_memory_32bit.hs_err.log",
        "gpu_driver.hs_err.log",
        "missing_dependency.latest.log",
        "resource_pack.latest.log",
        "no_exception.latest.log",
        "forge_incomplete.launcher.log",
    ],
)
def test_synthetic_fixtures_are_marked(name: str) -> None:
    first_line = (SYNTHETIC / name).read_text(encoding="utf-8").splitlines()[0]
    assert first_line == SYNTHETIC_MARKER


def test_original_samples_are_marked_too() -> None:
    marked = 0
    for path in sorted(SAMPLES.iterdir()):
        if not path.is_file() or path.stat().st_size == 0:
            continue  # empty_report.txt must stay empty: a marker would make it analysable
        try:
            first = path.read_text(encoding="utf-8").splitlines()[0]
        except UnicodeDecodeError:
            first = path.read_text(encoding="gb18030").splitlines()[0]
        assert first == SYNTHETIC_MARKER, path.name
        marked += 1
    assert marked >= 9
