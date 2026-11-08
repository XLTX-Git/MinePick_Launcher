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

"""The set of logs one crash is described by (``CrashLogSet``).

A crash is rarely one file: the game writes ``logs/latest.log`` while it runs, a
crash report appears in ``crash-reports/`` with the full stack and the mod list,
and a JVM-level failure leaves ``hs_err_pid*.log``. This module names that set and
applies the two rules that matter for analysis:

* **kinds decide who may read a line** 鈥?a rule declares ``sources`` and the engine
  only hands it logs of exactly those kinds (``game`` / ``crash_report`` /
  ``hs_err`` / ``debug`` / ``launcher`` / the virtual ``stack`` and ``mods``);
* **only the newest crash report (and the newest hs_err) is matched** 鈥?older
  reports are kept for the export but never explain *this* launch, so a stale
  finding cannot win over the fresh one.

Every log carries its path, kind, mtime and lines (plus the original line number
of every kept line, see :class:`launcher.diagnostics.prepare.PreparedLog`).
Collection itself is reused from :mod:`launcher.diagnostics.collector`; this
module adds no second scanning mechanism.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from launcher.diagnostics.collector import collect_candidates
from launcher.diagnostics.context import DiagnosisContext
from launcher.diagnostics.prepare import (
    KIND_CRASH_REPORT,
    KIND_DEBUG,
    KIND_GAME,
    KIND_HS_ERR,
    KIND_LAUNCHER,
    KIND_MODS,
    KIND_STACK,
    MATCHABLE_KINDS,
    PreparedLog,
    prepare_candidates,
)

_LOGGER = logging.getLogger(__name__)

#: Kinds where only the newest file can explain this launch.
NEWEST_ONLY = frozenset({KIND_CRASH_REPORT, KIND_HS_ERR})
#: Real kinds in the order they are shown in the report.
ORDER = (KIND_CRASH_REPORT, KIND_HS_ERR, KIND_GAME, KIND_DEBUG, KIND_LAUNCHER)


@dataclass(frozen=True)
class CrashLogSet:
    """All logs of one crash, with virtual ``mods`` / ``stack`` views attached."""

    logs: tuple[PreparedLog, ...] = ()
    virtual_logs: tuple[PreparedLog, ...] = ()

    # ---- construction ----
    @classmethod
    def from_prepared(
        cls,
        logs: list[PreparedLog] | tuple[PreparedLog, ...],
        *,
        virtual_logs: list[PreparedLog] | tuple[PreparedLog, ...] = (),
    ) -> CrashLogSet:
        ordered = sorted(
            logs,
            key=lambda log: (
                ORDER.index(log.kind) if log.kind in ORDER else len(ORDER),
                -log.mtime,
            ),
        )
        return cls(logs=tuple(ordered), virtual_logs=tuple(virtual_logs))

    @classmethod
    def from_context(
        cls,
        context: DiagnosisContext,
        *,
        virtual_logs: list[PreparedLog] | tuple[PreparedLog, ...] = (),
    ) -> CrashLogSet:
        """Collect + prepare the logs of ``context`` (freshness gate included)."""
        prepared = prepare_candidates(collect_candidates(context))
        log_set = cls.from_prepared(prepared, virtual_logs=virtual_logs)
        _LOGGER.debug(
            "diagnostics: CrashLogSet %s",
            ", ".join(f"{log.name}[{log.kind}]={len(log.lines)}" for log in log_set.logs)
            or "empty",
        )
        return log_set

    # ---- inspection ----
    def of_kind(self, *kinds: str) -> tuple[PreparedLog, ...]:
        wanted = set(kinds)
        return tuple(log for log in self.logs if log.kind in wanted)

    def newest(self, kind: str) -> PreparedLog | None:
        candidates = [log for log in self.logs if log.kind == kind]
        if not candidates:
            return None
        return max(candidates, key=lambda log: log.mtime)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(log.kind for log in self.logs))

    @property
    def analysable(self) -> bool:
        return any(log.is_matchable for log in self.real_matching_logs())

    @property
    def crash_report(self) -> PreparedLog | None:
        return self.newest(KIND_CRASH_REPORT)

    @property
    def latest_log(self) -> PreparedLog | None:
        return self.newest(KIND_GAME)

    @property
    def hs_err(self) -> PreparedLog | None:
        return self.newest(KIND_HS_ERR)

    # ---- matching ----
    def real_matching_logs(self) -> tuple[PreparedLog, ...]:
        """Real logs a rule may read (newest crash report/hs_err only)."""
        out: list[PreparedLog] = []
        newest = {kind: self.newest(kind) for kind in NEWEST_ONLY}
        for log in self.logs:
            if not log.is_matchable:
                continue
            if log.kind in NEWEST_ONLY and log is not newest[log.kind]:
                continue
            out.append(log)
        return tuple(out)

    def matching_logs(self) -> tuple[PreparedLog, ...]:
        """Everything that takes part in matching (real matching + virtual)."""
        return (*self.real_matching_logs(), *self.virtual_matching_logs())

    def virtual_matching_logs(self) -> tuple[PreparedLog, ...]:
        return tuple(log for log in self.virtual_logs if log.is_matchable)

    def with_virtual(
        self, virtual_logs: list[PreparedLog] | tuple[PreparedLog, ...]
    ) -> CrashLogSet:
        return CrashLogSet(logs=self.logs, virtual_logs=tuple(virtual_logs))

    @property
    def virtual_kinds(self) -> tuple[str, ...]:
        return tuple(log.kind for log in self.virtual_logs)


def virtual_log(
    *,
    name: str,
    kind: str,
    lines: list[str] | tuple[str, ...],
    numbers: list[int] | tuple[int, ...] = (),
    path: str = "",
) -> PreparedLog:
    """Build one generated log (``mods`` / ``stack``) for the matcher.

    Generated lines are evidence of a *fact* (an invalid jar, a mod-owned stack
    frame), not a copy of the source text; ``numbers`` points back at the real
    line in the source file when there is one.
    """
    text = tuple(lines)
    if not numbers:
        numbers = tuple(range(1, len(text) + 1))
    return PreparedLog(
        path=Path(path or name),
        name=name,
        kind=kind,
        lines=text,
        origin="virtual",
        encoding="generated",
        total_lines=len(text),
        truncated=False,
        mtime=0.0,
        numbers=tuple(numbers),
        virtual=True,
    )


def environment_logs(context: DiagnosisContext) -> tuple[PreparedLog, ...]:
    """Virtual launcher-side lines about the version folders (multiple Forge profiles).

    A single game directory can hold several Forge profiles for the same
    Minecraft version; an instance that keeps its own ``versions`` folder while
    the shared one also has one is a classic "wrong profile" crash. The check is
    a directory listing only 鈥?no launcher code is involved.
    """
    game_dir = context.resolved_game_dir()
    versions = game_dir / "versions"
    try:
        profile_ids = sorted(entry.name for entry in versions.iterdir() if entry.is_dir())
    except OSError:
        return ()
    groups: dict[str, list[str]] = {}
    for profile in profile_ids:
        if "-forge-" not in profile:
            continue
        mc, _, _forge_version = profile.partition("-forge-")
        groups.setdefault(mc, []).append(profile)
    lines: list[str] = []
    for mc, profiles in sorted(groups.items()):
        if len(profiles) > 1:
            lines.append(
                f"multiple-forge-profiles: {len(profiles)} profiles for {mc}: "
                + ", ".join(profiles)
            )
    if not lines:
        return ()
    return (
        virtual_log(name="versions/", kind=KIND_LAUNCHER, lines=lines, numbers=(0,)),
    )


def build_virtual_logs(mod_index, stack) -> tuple[PreparedLog, ...]:
    """The virtual logs of one analysis (mod index first: rules can prefer it)."""
    out: list[PreparedLog] = []
    if mod_index is not None:
        out.extend(mod_index.as_logs())
    if stack is not None:
        out.extend(stack.as_logs())
    return tuple(out)


__all__ = [
    "KIND_MODS",
    "KIND_STACK",
    "MATCHABLE_KINDS",
    "NEWEST_ONLY",
    "ORDER",
    "CrashLogSet",
    "build_virtual_logs",
    "environment_logs",
    "virtual_log",
]

