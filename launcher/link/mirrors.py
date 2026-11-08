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

"""Pick the fastest of Terracotta's three official download mirrors.

Measured in this project's own network (2026-11): CNB about 3.1 MB/s, Gitee about 2.0 MB/s,
GitHub about 0.09 MB/s. Every mirror serves the same bytes, so the launcher races a HEAD (or a
one-byte ranged GET when HEAD is refused) against all three at once, keeps the first answer and
remembers the winner for :data:`MIRROR_TTL` seconds in ``<launcher dir>/cache/mirror-pick.json``.
The next download therefore starts immediately instead of measuring again.

A user-chosen mirror always wins over both the cache and the race. The setting arrives either as
the ``preferred`` argument or as ``MINEPICK_TERRACOTTA_MIRROR`` (``cnb`` / ``gitee`` / ``github``);
this round the onboarding page only shows the choice, it does not wire it to the downloader, which
is stated in the delivery report.

The race uses daemon threads so a slow mirror can neither block the download nor keep the
interpreter alive when the launcher quits. The result is written atomically (temporary file then
``Path.replace``), so a killed process never leaves half a JSON file behind.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from launcher import paths

logger = logging.getLogger(__name__)

#: How long a measured winner is trusted.
MIRROR_TTL = 7 * 24 * 3600.0

#: ``host:port``-free override of the automatic choice.
ENV_MIRROR = "MINEPICK_TERRACOTTA_MIRROR"

CACHE_DIRNAME = "cache"
CACHE_FILENAME = "mirror-pick.json"


@dataclass(frozen=True, slots=True)
class Mirror:
    """One release mirror: a stable key, a display name and the URL prefix of its downloads."""

    key: str
    label: str
    base_url: str

    def url(self, version: str, asset: str) -> str:
        tag = version if version.startswith("v") else "v" + version
        return f"{self.base_url.rstrip('/')}/{tag}/{asset}"


#: Speed order from the project's measurements; also the fallback order when every probe fails.
MIRRORS: tuple[Mirror, ...] = (
    Mirror(
        "cnb",
        "CNB",
        "https://cnb.cool/HMCL-Terracotta/Terracotta/-/releases/download",
    ),
    Mirror(
        "gitee",
        "Gitee",
        "https://gitee.com/burningtnt/Terracotta/releases/download",
    ),
    Mirror(
        "github",
        "GitHub",
        "https://github.com/burningtnt/Terracotta/releases/download",
    ),
)


@dataclass(frozen=True, slots=True)
class MirrorReport:
    """What the last race measured, for the 其他下载渠道 rows."""

    chosen: str = ""
    latency_ms: dict[str, int] = field(default_factory=dict)
    measured_at: float = 0.0

    def latency(self, key: str) -> int | None:
        return self.latency_ms.get(key)


def cache_path() -> Path:
    """``<launcher dir>/cache/mirror-pick.json`` (resolved at call time, not import time)."""
    return paths.launcher_dir() / CACHE_DIRNAME / CACHE_FILENAME


def mirror_by_key(key: str) -> Mirror | None:
    for mirror in MIRRORS:
        if mirror.key == key:
            return mirror
    return None


def label_for(key: str) -> str:
    mirror = mirror_by_key(key)
    return mirror.label if mirror is not None else key


def preferred_key() -> str:
    """The user's manual choice from the environment (``""`` when unset or unknown)."""
    return str(os.environ.get(ENV_MIRROR, "")).strip().lower()


def read_report(path: Path | None = None) -> MirrorReport:
    """The cached race result; an unreadable or malformed file reads as an empty report."""
    target = path if path is not None else cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MirrorReport()
    if not isinstance(raw, dict):
        return MirrorReport()
    chosen = str(raw.get("chosen") or "")
    latencies: dict[str, int] = {}
    raw_latencies = raw.get("latency_ms")
    if isinstance(raw_latencies, dict):
        for key, value in raw_latencies.items():
            if isinstance(value, (int, float)):
                latencies[str(key)] = int(value)
    measured_at = raw.get("measured_at")
    return MirrorReport(
        chosen=chosen,
        latency_ms=latencies,
        measured_at=float(measured_at) if isinstance(measured_at, (int, float)) else 0.0,
    )


def _write_report(report: MirrorReport, path: Path | None = None) -> None:
    """Persist one report atomically (the directory is created on demand)."""
    target = path if path is not None else cache_path()
    payload = {
        "version": 1,
        "chosen": report.chosen,
        "latency_ms": report.latency_ms,
        "measured_at": report.measured_at or time.time(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        logger.debug("无法写入镜像测速缓存: %s", target, exc_info=True)


def _probe(mirror: Mirror, version: str, asset: str, timeout: float) -> int | None:
    """Round-trip time of a HEAD (or one ranged byte) against one mirror, or ``None``."""
    url = mirror.url(version, asset)
    started = time.monotonic()
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(timeout)) as client:
            try:
                response = client.head(url)
                if response.status_code in (200, 204, 206):
                    return max(1, round((time.monotonic() - started) * 1000))
            except httpx.HTTPError:
                pass  # HEAD is optional; the ranged GET below is the second opinion
            with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                if response.status_code in (200, 206):
                    return max(1, round((time.monotonic() - started) * 1000))
    except (httpx.HTTPError, OSError):
        return None
    return None


def _race(
    mirrors: Sequence[Mirror], version: str, asset: str, timeout: float
) -> tuple[Mirror | None, dict[str, int]]:
    """Run every probe at once; return the first mirror that answered and the latencies seen."""
    results: queue.Queue[tuple[str, int | None]] = queue.Queue()
    by_key = {mirror.key: mirror for mirror in mirrors}

    def run(mirror: Mirror) -> None:
        results.put((mirror.key, _probe(mirror, version, asset, timeout)))

    for mirror in mirrors:
        thread = threading.Thread(
            target=run,
            args=(mirror,),
            name=f"terracotta-mirror-{mirror.key}",
            daemon=True,
        )
        thread.start()

    latencies: dict[str, int] = {}
    winner: Mirror | None = None
    deadline = time.monotonic() + timeout + 0.5
    remaining = len(mirrors)
    while remaining > 0:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            key, latency = results.get(timeout=left)
        except queue.Empty:
            break
        remaining -= 1
        if latency is not None:
            latencies[key] = latency
            if winner is None:
                winner = by_key.get(key)  # the first answer wins: "先回来的先用"
                break  # the download starts now; the other probes are daemon threads
    return winner, latencies


def choose_mirror(
    *,
    version: str,
    asset: str,
    mirrors: Sequence[Mirror] = MIRRORS,
    preferred: str = "",
    path: Path | None = None,
    timeout: float = 4.0,
    now: float | None = None,
) -> Mirror:
    """The mirror to download from: manual choice, then fresh cache, then the race."""
    chosen_key = (preferred or preferred_key()).strip().lower()
    manual = next((mirror for mirror in mirrors if mirror.key == chosen_key), None)
    timestamp = time.time() if now is None else now
    if manual is not None:
        previous = read_report(path)
        _write_report(
            MirrorReport(
                chosen=manual.key,
                latency_ms=previous.latency_ms,
                measured_at=timestamp,
            ),
            path,
        )
        return manual

    cached = read_report(path)
    if cached.chosen and timestamp - cached.measured_at < MIRROR_TTL:
        match = next((mirror for mirror in mirrors if mirror.key == cached.chosen), None)
        if match is not None:
            return match

    winner, latencies = _race(list(mirrors), version, asset, timeout)
    if winner is None:
        # Every probe failed: a HEAD can be blocked while GET still works, so try the measured
        # order rather than reporting a download failure that may not exist.
        return mirrors[0]
    _write_report(
        MirrorReport(chosen=winner.key, latency_ms=latencies, measured_at=timestamp), path
    )
    return winner
