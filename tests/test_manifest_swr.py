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

"""The version-manifest cache: a stale copy is shown at once, the refresh lands quietly in the list."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from launcher.meta.manifest import (
    ManifestVersion,
    VersionManifest,
    add_refresh_listener,
    fetch_manifest,
    remove_refresh_listener,
    wait_for_background_refresh,
)

OLD_RAW = {
    "latest": {"release": "1.21.8", "snapshot": "25w01a"},
    "versions": [
        {
            "id": "1.21.8",
            "type": "release",
            "url": "https://example.com/v/1.21.8.json",
            "time": "2026-01-01T00:00:00+00:00",
            "releaseTime": "2026-01-01T00:00:00+00:00",
        }
    ],
}
NEW_RAW = {
    "latest": {"release": "1.22.0", "snapshot": "25w99z"},
    "versions": [
        {
            "id": "1.22.0",
            "type": "release",
            "url": "https://example.com/v/1.22.0.json",
            "time": "2026-02-01T00:00:00+00:00",
            "releaseTime": "2026-02-01T00:00:00+00:00",
        },
        *OLD_RAW["versions"],
    ],
}
PISTON = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def _stale_cache(path) -> None:
    path.write_text(
        json.dumps({"fetched_at": 0, "manifest": OLD_RAW}),
        encoding="utf-8",
    )


def _recording_client(payload: dict):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_a_stale_manifest_is_returned_before_the_network_answers(ws_tmp) -> None:
    cache = ws_tmp / "version_manifest.json"
    _stale_cache(cache)
    client, calls = _recording_client(NEW_RAW)

    started = time.monotonic()
    manifest = fetch_manifest(cache_path=cache, client=client)
    elapsed = time.monotonic() - started

    assert manifest.latest["release"] == "1.21.8"  # the copy already on disk
    assert elapsed < 0.5, "the first paint must not wait for the refresh"
    assert calls == []
    assert wait_for_background_refresh(5.0)
    assert json.loads(cache.read_text(encoding="utf-8"))["manifest"]["latest"]["release"] == "1.22.0"
    # The refresh must ask for a fresh copy even though the response cache could still be inside its
    # own max-age window.
    assert calls[0].headers["cache-control"] == "no-cache"


def test_a_new_manifest_reaches_the_registered_listener(ws_tmp) -> None:
    cache = ws_tmp / "version_manifest.json"
    _stale_cache(cache)
    client, _calls = _recording_client(NEW_RAW)
    seen: list[VersionManifest] = []
    callback = seen.append
    add_refresh_listener(callback)
    try:
        fetch_manifest(cache_path=cache, client=client)
        assert wait_for_background_refresh(5.0)
    finally:
        remove_refresh_listener(callback)

    assert len(seen) == 1
    assert seen[0].latest["release"] == "1.22.0"


def test_an_unchanged_manifest_does_not_wake_the_listener(ws_tmp) -> None:
    cache = ws_tmp / "version_manifest.json"
    _stale_cache(cache)
    client, _calls = _recording_client(OLD_RAW)
    seen: list[VersionManifest] = []
    callback = seen.append
    add_refresh_listener(callback)
    try:
        fetch_manifest(cache_path=cache, client=client)
        assert wait_for_background_refresh(5.0)
    finally:
        remove_refresh_listener(callback)

    assert seen == []
    refreshed = json.loads(cache.read_text(encoding="utf-8"))
    assert refreshed["fetched_at"] > 0  # the fetch time moved, the list did not have to be rebuilt


# ---------- the versions page keeps its place when the refresh lands ----------


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is not None:
        yield QApplication.instance()
        return
    from gui.main import create_app

    yield create_app([])


def _versions() -> list[ManifestVersion]:
    return [
        ManifestVersion(
            id=f"rel-{index}",
            type="release",
            url="https://example.invalid/v.json",
            time="",
            release_time=f"2025-01-{index:02d}T12:00:00+00:00",
        )
        for index in range(1, 26)
    ]


@pytest.fixture()
def page(app, monkeypatch, ws_tmp):
    """A real versions page, offscreen, with a fixed manifest and an empty game directory."""
    game = ws_tmp / "game"
    game.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setenv("MINECRAFT_GAME_DIR", str(game))

    from launcher import config as config_mod

    cfg, cfg_path = config_mod.load()
    cfg.game_dir = game
    config_mod.save(cfg, cfg_path)

    from gui.pages import versions_page

    def _no_network(**_kwargs):
        raise RuntimeError("offline test")

    monkeypatch.setattr(versions_page, "fetch_manifest", _no_network)
    built = versions_page.VersionsPage()
    built.resize(920, 560)
    built.show()
    app.processEvents()
    built._on_manifest(
        SimpleNamespace(versions=_versions(), latest={"release": "rel-1", "snapshot": "rel-25"})
    )
    # The page starts with every fold closed; this test scrolls rows, so open the release fold.
    built.expand_group("release")
    app.processEvents()
    yield built
    built.close()


class _StubPages:
    def __init__(self, page) -> None:
        self._page = page

    def peek(self, key: str):
        return self._page if key == "versions" else None


class _StubWindow:
    def __init__(self, page) -> None:
        self.pages = _StubPages(page)


def test_the_versions_list_updates_without_moving_the_scroll(page, app) -> None:
    from gui.main import _ManifestRefreshBridge

    list_widget = page.list
    # Four fold headers plus the 25 release rows the fixture opened.
    assert list_widget.count() == 29 + page.shortcut_count()
    page.list.setCurrentRow(7)
    app.processEvents()
    scroll_range = list_widget.verticalScrollBar().maximum()
    assert scroll_range > 40, "the manifest must be long enough for this test to scroll"
    list_widget.verticalScrollBar().setValue(40)
    app.processEvents()
    selected = page._selected_id()

    newer = SimpleNamespace(
        versions=_versions()
        + [
            ManifestVersion(
                id="27w01a",
                type="snapshot",
                url="https://example.invalid/v.json",
                time="",
                release_time="2027-01-01T12:00:00+00:00",
            )
        ],
        latest={"release": "rel-1", "snapshot": "27w01a"},
    )
    bridge = _ManifestRefreshBridge(_StubWindow(page))
    bridge._apply(newer)
    app.processEvents()

    # The new version is a snapshot and the release fold is the only one open, so the built rows do
    # not change; the manifest total does, which is what proves the refresh reached the page.
    assert page.group_total("snapshot") == 1
    assert list_widget.count() == 29 + page.shortcut_count()
    assert page._selected_id() == selected
    assert list_widget.verticalScrollBar().value() == 40
