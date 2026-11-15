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

"""Online content recognition: matching, the disk cache, version comparison and the worker.

Every HTTP answer comes from an ``httpx.MockTransport``, so the tests measure what the module asked
for (URL, facets, loader/game-version filters, User-Agent) and how often - not what the network
happened to return. The cache lives under a temporary launcher data directory, which is why the
"second call is free" and "offline uses the stored answer" assertions can count requests.
"""

from __future__ import annotations

import itertools
import pathlib
import threading
import time

import httpx
import pytest

from launcher.mods import identity

SEARCH_HITS = {
    "hits": [
        {
            "slug": "sodium",
            "title": "Sodium",
            "description": "A modern rendering engine",
            "categories": ["optimization", "fabric"],
            "icon_url": "https://cdn.example/sodium.png",
            "date_modified": "2026-01-01T00:00:00Z",
        },
        {
            "slug": "sodium-extra",
            "title": "Sodium Extra",
            "description": "Extra options for Sodium",
            "categories": ["optimization"],
            "icon_url": "",
            "date_modified": "2026-01-01T00:00:00Z",
        },
    ],
    "total_hits": 2,
}
PROJECT = {
    "slug": "sodium",
    "title": "Sodium",
    "description": "A modern rendering engine",
    "categories": ["optimization", "fabric"],
    "icon_url": "https://cdn.example/sodium.png",
    "updated": "2026-02-02T00:00:00Z",
}
VERSIONS = [{"version_number": "0.7.0"}, {"version_number": "0.6.5"}]


@pytest.fixture(autouse=True)
def _temporary_launcher_dir(monkeypatch, ws_tmp):
    """Keep every cache file inside this test (and make its directory observable)."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    identity.set_enabled(True)
    yield
    identity.set_enabled(True)
    identity.clear_cache()


@pytest.fixture()
def server():
    """A tiny Modrinth: records each request, answers search/project/version."""

    class Fake:
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.versions = list(VERSIONS)
            self.hits = dict(SEARCH_HITS)

        def handler(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path
            if path == "/v2/search":
                return httpx.Response(200, json=self.hits)
            if path == "/v2/project/sodium":
                return httpx.Response(200, json=PROJECT)
            if path == "/v2/project/sodium/version":
                return httpx.Response(200, json=self.versions)
            return httpx.Response(404, json={"error": "not found"})

        def client(self) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(self.handler))

    return Fake()


# ---------------------------------------------------------------- lookup


def test_lookup_matches_the_slug_and_returns_the_project_details(server):
    client = server.client()
    value = identity.lookup("Sodium", "sodium", "mod", client=client)
    client.close()
    assert isinstance(value, identity.Identity)
    assert (value.slug, value.title) == ("sodium", "Sodium")
    assert value.description == "A modern rendering engine"
    assert value.categories == ("optimization", "fabric")
    assert value.icon_url == "https://cdn.example/sodium.png"
    assert value.latest_version == "", "no game version was supplied, so no update can be judged"
    # The product User-Agent Modrinth requires is sent on every request.
    assert all("MinePick-Launcher/" in request.headers["User-Agent"] for request in server.requests)
    # The search is filtered to the project type the caller asked for.
    assert 'project_type:mod' in server.requests[0].url.params["facets"]
    assert server.requests[0].url.params["limit"] == "5"


def test_title_matching_is_case_and_separator_insensitive(server):
    server.hits = {
        "hits": [
            {
                "slug": "complementary-reimagined",
                "title": "Complementary Reimagined",
                "description": "A shader pack",
                "categories": ["shaders"],
                "icon_url": "https://cdn.example/cr.png",
                "date_modified": "2026-01-01T00:00:00Z",
            }
        ],
        "total_hits": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/search":
            return httpx.Response(200, json=server.hits)
        if request.url.path == "/v2/project/complementary-reimagined":
            return httpx.Response(
                200,
                json={
                    "slug": "complementary-reimagined",
                    "title": "Complementary Reimagined",
                    "description": "A shader pack",
                    "categories": ["shaders"],
                    "icon_url": "https://cdn.example/cr.png",
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    value = identity.lookup("ComplementaryReimagined", "", "shader", client=client)
    client.close()
    assert value is not None and value.slug == "complementary-reimagined"


def test_a_search_without_a_matching_hit_returns_none_and_is_cached(server):
    server.hits = {"hits": [SEARCH_HITS["hits"][1]], "total_hits": 1}
    client = server.client()
    assert identity.lookup("A Totally Different Mod", "different", "mod", client=client) is None
    client.close()
    requests_before = len(server.requests)
    # The "nothing matched" answer is cached too: a second call is answered from disk.
    client = server.client()
    assert identity.lookup("A Totally Different Mod", "different", "mod", client=client) is None
    client.close()
    assert len(server.requests) == requests_before


def test_the_second_lookup_is_answered_from_the_cache(server):
    client = server.client()
    first = identity.lookup("Sodium", "sodium", "mod", client=client)
    client.close()
    assert len(server.requests) == 2  # search + project
    client = server.client()
    second = identity.lookup("Sodium", "sodium", "mod", client=client)
    client.close()
    assert second == first
    assert len(server.requests) == 2, "the disk cache must answer without a request"


def test_an_expired_entry_is_refreshed_but_still_used_when_offline(server):
    client = server.client()
    first = identity.lookup("Sodium", "sodium", "mod", client=client, ttl=0)
    client.close()
    assert first is not None

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    offline_client = httpx.Client(transport=httpx.MockTransport(offline))
    second = identity.lookup("Sodium", "sodium", "mod", client=offline_client, ttl=0)
    offline_client.close()
    assert second == first, "an offline launcher must still show the cached identity"


def test_no_cache_and_no_network_returns_none_without_raising():
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(offline))
    assert identity.lookup("Sodium", "sodium", "mod", client=client) is None
    client.close()


def test_the_newest_version_is_asked_for_the_instances_game_version_and_loader(server):
    client = server.client()
    value = identity.lookup(
        "Sodium", "sodium", "mod", game_version="1.21.11", loader="fabric", client=client
    )
    client.close()
    assert value is not None and value.latest_version == "0.7.0"
    version_request = next(r for r in server.requests if r.url.path.endswith("/version"))
    assert version_request.url.params["game_versions"] == '["1.21.11"]'
    assert version_request.url.params["loaders"] == '["fabric"]'
    assert value.is_updatable("0.6.5") is True
    assert value.is_updatable("0.7.0") is False
    assert value.is_updatable("") is False, "an unknown installed version is never updatable"


def test_a_resource_pack_asks_for_versions_without_a_loader(server):
    server.hits = {
        "hits": [
            {
                "slug": "faithful",
                "title": "Faithful 32x",
                "description": "A pack",
                "categories": ["32x"],
                "icon_url": "",
                "date_modified": "2026-01-01T00:00:00Z",
            }
        ],
        "total_hits": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        server.requests.append(request)
        if request.url.path == "/v2/search":
            return httpx.Response(200, json=server.hits)
        if request.url.path == "/v2/project/faithful":
            return httpx.Response(
                200,
                json={"slug": "faithful", "title": "Faithful 32x", "description": "A pack"},
            )
        if request.url.path == "/v2/project/faithful/version":
            return httpx.Response(200, json=[{"version_number": "2.0"}])
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    value = identity.lookup(
        "Faithful 32x", "", "resourcepack", game_version="1.21.11", client=client
    )
    client.close()
    assert value is not None and value.latest_version == "2.0"
    version_request = next(r for r in server.requests if r.url.path.endswith("/version"))
    assert "loaders" not in version_request.url.params


# ---------------------------------------------------------------- switch / queue


def test_the_switch_off_means_no_request_at_all(monkeypatch):
    calls: list[object] = []

    def client() -> httpx.Client:
        calls.append("request")
        raise AssertionError("the network was reached while recognition was switched off")

    identity.set_enabled(False)
    try:
        assert identity.lookup("Sodium", "sodium", "mod") is None
    finally:
        identity.set_enabled(True)
    assert calls == []


def test_the_queue_refuses_work_while_switched_off():
    queue = identity.LookupQueue(interval=0.0, worker=lambda _request: None)
    identity.set_enabled(False)
    try:
        assert queue.submit(identity.LookupRequest("k", "n", "m", "mod"), lambda *_a: None) is False
    finally:
        identity.set_enabled(True)
    assert queue.queries == 0


def test_the_queue_is_fifo_and_spaces_the_requests():
    starts: list[float] = []
    lock = threading.Lock()

    def worker(request: identity.LookupRequest):
        with lock:
            starts.append(time.monotonic())

    queue = identity.LookupQueue(interval=0.05, worker=worker)
    results: list[str] = []
    for index in range(3):
        queue.submit(
            identity.LookupRequest(f"k{index}", f"n{index}", f"m{index}", "mod"),
            lambda request, _result: results.append(request.key),
        )
    assert queue.wait_idle(5.0)
    assert results == ["k0", "k1", "k2"], "the worker must answer in submission order"
    gaps = [second - first for first, second in itertools.pairwise(starts)]
    assert all(gap >= 0.04 for gap in gaps), f"requests were not spaced: {gaps}"


def test_a_key_already_queued_is_not_queued_twice():
    release = threading.Event()
    started = threading.Event()

    def worker(_request):
        started.set()
        release.wait(2.0)

    queue = identity.LookupQueue(interval=0.0, worker=worker)
    request = identity.LookupRequest("same-key", "n", "m", "mod")
    assert queue.submit(request, lambda *_a: None) is True
    assert started.wait(2.0)
    assert queue.submit(request, lambda *_a: None) is False
    release.set()
    assert queue.wait_idle(5.0)


def test_cancellation_stops_the_worker_and_drops_every_answer():
    release = threading.Event()
    started = threading.Event()

    def worker(_request):
        started.set()
        release.wait(2.0)
        return identity.Identity(kind="mod", slug="s", title="t", description="d")

    queue = identity.LookupQueue(interval=0.0, worker=worker)
    received: list[str] = []
    queue.submit(identity.LookupRequest("k0", "n", "m", "mod"), lambda request, _r: received.append(request.key))
    assert started.wait(2.0)
    for index in range(1, 5):
        queue.submit(
            identity.LookupRequest(f"k{index}", "n", "m", "mod"),
            lambda request, _r: received.append(request.key),
        )
    queue.cancel_all()
    release.set()
    assert queue.wait_idle(5.0)
    assert received == [], "a result that arrived after cancellation must be dropped"
    assert queue.is_running() is False, "the worker thread must exit once its queue is empty"


# ---------------------------------------------------------------- versions and names


def test_versions_are_compared_ignoring_a_leading_v():
    assert identity.normalize_version(" v1.2.3 ") == "1.2.3"
    assert identity.versions_differ("v1.2.3", "1.2.3") is False
    assert identity.versions_differ("1.2.3", "1.2.4") is True
    assert identity.versions_differ("", "1.2.4") is False
    assert identity.versions_differ("1.2.3", "") is False


def test_version_from_filename_reads_a_pack_name():
    assert identity.version_from_filename("BSL_v8.4.01.1.zip") == "8.4.01.1"
    assert identity.version_from_filename("ComplementaryReimagined_r5.3.zip") == "5.3"
    assert identity.version_from_filename("Faithful 32x.zip") == ""
    assert identity.pack_search_name("BSL_v8.4.01.1.zip") == "BSL"
    assert (
        identity.pack_search_name("ComplementaryReimagined_r5.3.zip")
        == "Complementary Reimagined"
    )
    assert identity.pack_search_name("Faithful 32x.zip") == "Faithful 32x"


# ---------------------------------------------------------------- icon store


def test_forget_cached_icon_removes_exactly_that_entry(monkeypatch, ws_tmp):
    from launcher.net import http_cache

    icons = ws_tmp / "icons"
    monkeypatch.setattr(http_cache, "icon_cache_dir", lambda: icons)
    monkeypatch.setattr(http_cache, "_enabled", True)
    url = "https://cdn.example/sodium.png"
    other = "https://cdn.example/other.png"
    handler = lambda request: httpx.Response(
        200, content=b"\x89PNG\r\n\x1a\nfake", headers={"content-type": "image/png"}
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    http_cache.fetch_icon_bytes(url, client=client)
    http_cache.fetch_icon_bytes(other, client=client)
    client.close()
    assert http_cache.cached_icon_bytes(url) is not None

    assert identity.forget_cached_icon(url) is True
    assert http_cache.cached_icon_bytes(url) is None
    assert http_cache.cached_icon_bytes(other) is not None, "another URL's icon must survive"
    assert identity.forget_cached_icon(url) is False, "forgetting a missing entry is not an error"

# ---------------------------------------------------------------------------------------------
# Hash identification: the only one that cannot be wrong
# ---------------------------------------------------------------------------------------------


def _jar(ws_tmp, payload: bytes = b"PK\x03\x04not-really-a-jar") -> pathlib.Path:
    target = ws_tmp / "some-renamed-thing.jar"
    target.write_bytes(payload)
    return target


def test_a_file_is_identified_by_its_content_not_its_name(ws_tmp, monkeypatch) -> None:
    """A renamed jar still hashes to the file the author uploaded, so the update badge is right."""
    from launcher.mods import identity

    path = _jar(ws_tmp)
    sha1 = identity.file_sha1(path)
    assert len(sha1) == 40

    seen: list[str] = []

    def fake_version(client, value):
        seen.append(value)
        return {"project_id": "PROJECT", "version_number": "1.2.3"}

    monkeypatch.setattr(identity, "_version_from_hash", fake_version)
    monkeypatch.setattr(
        identity,
        "_request_json",
        lambda client, path_, params=None: {
            "slug": "sodium",
            "title": "Sodium",
            "description": "Renderer",
            "icon_url": "https://cdn.example/icon.png",
            "categories": [],
        },
    )
    monkeypatch.setattr(identity, "_latest_version", lambda *a, **k: "1.2.4")

    found = identity.lookup_by_hash(
        path, "mod", game_version="1.21.11", loader="fabric", client=object()
    )

    assert seen == [sha1], "the hash of the bytes on disk is what gets asked about"
    assert found is not None
    assert found.slug == "sodium"
    assert found.latest_version == "1.2.4"


def test_a_file_that_is_not_from_modrinth_answers_none(ws_tmp, monkeypatch) -> None:
    from launcher.mods import identity

    monkeypatch.setattr(identity, "_version_from_hash", lambda client, sha1: None)
    assert identity.lookup_by_hash(_jar(ws_tmp), "mod", client=object()) is None


def test_the_queue_tries_the_hash_before_the_name(ws_tmp, monkeypatch) -> None:
    """When the row knows its file, a hash hit ends the lookup - no search, no name matching."""
    from launcher.mods import identity

    path = _jar(ws_tmp)
    hit = identity.Identity(kind="mod", slug="sodium", title="Sodium", description="")
    monkeypatch.setattr(identity, "lookup_by_hash", lambda *a, **k: hit)
    called: list[str] = []
    monkeypatch.setattr(identity, "lookup", lambda *a, **k: called.append("name") or None)

    request = identity.LookupRequest(
        key="mod:a.jar", name="a", modid="sodium", kind="mod", path=str(path)
    )
    assert identity.LookupQueue._default_worker(request) is hit
    assert called == [], "a hash hit must not fall through to the name search"


def test_a_hash_miss_falls_back_to_the_name_lookup(ws_tmp, monkeypatch) -> None:
    from launcher.mods import identity

    path = _jar(ws_tmp)
    fallback = identity.Identity(kind="mod", slug="other", title="Other", description="")
    monkeypatch.setattr(identity, "lookup_by_hash", lambda *a, **k: None)
    monkeypatch.setattr(identity, "lookup", lambda *a, **k: fallback)

    request = identity.LookupRequest(
        key="mod:b.jar", name="b", modid="", kind="mod", path=str(path)
    )
    assert identity.LookupQueue._default_worker(request) is fallback
