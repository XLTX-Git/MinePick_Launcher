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

"""The saved Terracotta download source: config round-trip and what reaches the downloader.

The 工具 › 联机 rows persist one mirror key; the session has to pass that key to the installer as
``preferred`` at download time (an empty key means the historical latency race). These tests use a
fake downloader for the argument assertion and the real mirror race for the automatic path.
"""

from __future__ import annotations

import threading
import time

from launcher import config
from launcher.link import LinkState, terracotta
from launcher.link import mirrors as mirror_module
from launcher.link.selfcheck import ComponentStatus
from launcher.link.terracotta import TerracottaLinkSession
from tests.link_fakes import FakeCore, FakeMirror, component_missing, wait_until


def _data_dir(monkeypatch, ws_tmp):
    target = ws_tmp / "launcher"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(target))
    return target


# ------------------------------------------------------------------ config


def test_the_default_is_the_automatic_race(ws_tmp) -> None:
    cfg = config.LauncherConfig()
    assert cfg.terracotta_mirror == ""
    assert config.TERRACOTTA_MIRROR_DEFAULT == ""


def test_the_choice_round_trips_through_the_config_file(ws_tmp) -> None:
    path = ws_tmp / "config.json"
    cfg = config.LauncherConfig()
    cfg.terracotta_mirror = "cnb"
    config.save(cfg, path)

    loaded, _ = config.load(path)
    assert loaded.terracotta_mirror == "cnb"
    assert config.load(path)[0].terracotta_mirror == "cnb"  # cached read agrees


def test_an_unknown_or_written_out_auto_value_falls_back_to_the_race(ws_tmp) -> None:
    cfg = config.LauncherConfig(terracotta_mirror="AUTO")
    assert cfg.terracotta_mirror == ""
    cfg.terracotta_mirror = "GITEE"
    assert cfg.terracotta_mirror == "gitee"
    cfg.terracotta_mirror = "not-a-mirror"
    assert cfg.terracotta_mirror == ""


def test_configured_key_reads_the_saved_choice(monkeypatch, ws_tmp) -> None:
    _data_dir(monkeypatch, ws_tmp)
    assert mirror_module.configured_key() == ""
    cfg, path = config.load()
    cfg.terracotta_mirror = "gitee"
    config.save(cfg, path)
    assert mirror_module.configured_key() == "gitee"
    assert mirror_module.normalize_key("GITHUB") == "github"
    assert mirror_module.normalize_key("auto") == ""


def test_the_config_choices_match_the_mirror_table() -> None:
    """The keys the config accepts and the keys the downloader knows may not drift apart."""
    assert set(config.TERRACOTTA_MIRROR_CHOICES) == {""} | {
        mirror.key for mirror in mirror_module.MIRRORS
    }


# ------------------------------------------------------------------ downloader argument


def _fake_download(monkeypatch, recorded: dict) -> None:
    def fake_install(progress, *, preferred: str = "", **_kwargs) -> ComponentStatus:
        recorded["preferred"] = preferred
        progress(1, 1)
        return ComponentStatus(downloaded=True, version="0.4.2", supported=True)

    monkeypatch.setattr(terracotta, "install", fake_install)


def test_the_saved_mirror_is_passed_to_the_downloader_at_download_time(
    monkeypatch, ws_tmp
) -> None:
    """The session is built before the user picks a mirror; the write has to win at download time."""
    _data_dir(monkeypatch, ws_tmp)
    recorded: dict = {}
    _fake_download(monkeypatch, recorded)
    session = TerracottaLinkSession(
        process=FakeCore(None),
        component_status_fn=component_missing,
        player_name=lambda: "",
    )
    try:
        cfg, path = config.load()
        cfg.terracotta_mirror = "gitee"
        config.save(cfg, path)

        session.download_core(lambda _progress: None)

        assert wait_until(lambda: recorded.get("preferred") == "gitee")
        assert session.state() is LinkState.IDLE
    finally:
        session.dispose(wait=True)


def test_the_automatic_choice_passes_an_empty_preference(monkeypatch, ws_tmp) -> None:
    _data_dir(monkeypatch, ws_tmp)
    recorded: dict = {}
    _fake_download(monkeypatch, recorded)
    session = TerracottaLinkSession(
        process=FakeCore(None),
        component_status_fn=component_missing,
        player_name=lambda: "",
    )
    try:
        session.download_core(lambda _progress: None)
        assert wait_until(lambda: recorded.get("preferred") == "")
    finally:
        session.dispose(wait=True)


def test_the_automatic_choice_races_the_mirrors(monkeypatch, ws_tmp) -> None:
    """An empty preference must probe several mirrors, not return one without measuring."""
    _data_dir(monkeypatch, ws_tmp)
    fast = FakeMirror(b"x")
    slow = FakeMirror(b"x", delay=0.2)
    servers = (fast, slow)
    try:
        chosen = mirror_module.choose_mirror(
            version="0.4.2",
            asset="terracotta-0.4.2-windows-x86_64-pkg.tar.gz",
            mirrors=(
                mirror_module.Mirror("cnb", "CNB", fast.base_url),
                mirror_module.Mirror("gitee", "Gitee", slow.base_url),
            ),
            preferred=mirror_module.configured_key(),
            path=ws_tmp / "mirror-pick.json",
            timeout=2.0,
        )
        assert chosen.key == "cnb"
        assert wait_until(lambda: all(server.requests("HEAD") >= 1 for server in servers))
        report = mirror_module.read_report(ws_tmp / "mirror-pick.json")
        assert report.chosen == "cnb"
        assert report.latency("cnb") is not None
    finally:
        for server in servers:
            server.stop()


def test_a_saved_choice_skips_the_race(monkeypatch, ws_tmp) -> None:
    _data_dir(monkeypatch, ws_tmp)
    cfg, path = config.load()
    cfg.terracotta_mirror = "github"
    config.save(cfg, path)
    server = FakeMirror(b"x")
    try:
        chosen = mirror_module.choose_mirror(
            version="0.4.2",
            asset="asset.tar.gz",
            mirrors=(mirror_module.Mirror("github", "GitHub", server.base_url),),
            preferred=mirror_module.configured_key(),
            path=ws_tmp / "mirror-pick.json",
            timeout=1.0,
        )
        assert chosen.key == "github"
        assert server.requests() == 0, "a manual choice must not probe the race"
    finally:
        server.stop()


def test_all_three_channels_are_measured_after_the_winner_is_returned(
    monkeypatch, ws_tmp
) -> None:
    """The download starts on the first answer; the two slow probes still report afterwards."""
    _data_dir(monkeypatch, ws_tmp)
    fast = FakeMirror(b"x")
    mid = FakeMirror(b"x", delay=0.25)
    slow = FakeMirror(b"x", delay=0.45)
    mirrors = (
        mirror_module.Mirror("cnb", "CNB", fast.base_url),
        mirror_module.Mirror("gitee", "Gitee", mid.base_url),
        mirror_module.Mirror("github", "GitHub", slow.base_url),
    )
    seen: list[tuple[str, int | None]] = []
    after_return: list[str] = []
    returned = threading.Event()
    lock = threading.Lock()

    def on_latency(key: str, latency: int | None) -> None:
        with lock:
            seen.append((key, latency))
            if returned.is_set():
                after_return.append(key)

    report_path = ws_tmp / "mirror-pick.json"
    try:
        started = time.monotonic()
        chosen = mirror_module.choose_mirror(
            version="0.4.2",
            asset="asset.tar.gz",
            mirrors=mirrors,
            path=report_path,
            timeout=2.0,
            on_latency=on_latency,
        )
        elapsed = time.monotonic() - started
        returned.set()
        # The winner is returned without waiting for the two delayed probes.
        assert chosen.key == "cnb"
        assert elapsed < 0.2, f"the race blocked for {elapsed:.2f}s on the slow mirrors"
        assert wait_until(lambda: len(seen) == 3, timeout=5.0), seen
        assert {key for key, _latency in seen} == {"cnb", "gitee", "github"}
        assert all(latency is not None for _key, latency in seen)
        assert set(after_return) == {"gitee", "github"}, after_return
        assert wait_until(
            lambda: set(mirror_module.read_report(report_path).latency_ms)
            == {"cnb", "gitee", "github"},
            timeout=5.0,
        ), mirror_module.read_report(report_path).latency_ms
        report = mirror_module.read_report(report_path)
        assert report.chosen == "cnb"
    finally:
        for server in (fast, mid, slow):
            server.stop()


def test_a_probe_that_never_answers_is_timed_out_not_unmeasured(monkeypatch, ws_tmp) -> None:
    """``None`` is delivered for a failed probe, and the cache records it as failed (超时)."""
    _data_dir(monkeypatch, ws_tmp)
    fast = FakeMirror(b"x")
    dead = FakeMirror(b"x", delay=0.6)
    mirrors = (
        mirror_module.Mirror("cnb", "CNB", fast.base_url),
        mirror_module.Mirror("github", "GitHub", dead.base_url),
    )
    seen: dict[str, int | None] = {}
    lock = threading.Lock()

    def on_latency(key: str, latency: int | None) -> None:
        with lock:
            seen[key] = latency

    report_path = ws_tmp / "mirror-pick.json"
    try:
        mirror_module.choose_mirror(
            version="0.4.2",
            asset="asset.tar.gz",
            mirrors=mirrors,
            path=report_path,
            timeout=0.15,
            on_latency=on_latency,
        )
        assert wait_until(lambda: set(seen) == {"cnb", "github"}, timeout=5.0), seen
        assert seen["cnb"] is not None
        assert seen["github"] is None, "a probe that timed out must report None, not stay silent"
        assert wait_until(
            lambda: mirror_module.read_report(report_path).timed_out("github"), timeout=5.0
        )
        report = mirror_module.read_report(report_path)
        assert report.latency("cnb") is not None
        assert not report.timed_out("cnb")
    finally:
        fast.stop()
        dead.stop()


