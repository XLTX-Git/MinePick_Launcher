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

"""The HTTP cache contract: no request while fresh, 304 reuses the body, icons survive a restart."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from launcher.net import http_cache

URL = "https://api.example.test/v2/search?query=sodium"
ETAG = '"v1"'
LAST_MODIFIED = "Tue, 27 Oct 2026 15:00:54 GMT"
ICON_URL = "https://cdn.example.test/data/sodium/icon.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture(autouse=True)
def _disk_cache_on(_no_disk_http_cache, monkeypatch):
    """The suite turns the cache off; this file is the one place that measures it on."""
    monkeypatch.setattr(http_cache, "_enabled", True)


def _age(transport: http_cache.CachingTransport, url: str, seconds: float) -> None:
    """Move a stored entry's fetch time into the past, without sleeping in the test."""
    _body_path, meta_path = transport._paths(url)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["fetched_at"] = float(meta["fetched_at"]) - seconds
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


class _Server:
    """A conditional-GET endpoint: 200 with validators, 304 when the client's copy still matches."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.payload: dict = {"hits": [{"slug": "sodium"}]}
        self.max_age = "60"
        self.etag: str | None = ETAG
        self.last_modified: str | None = ""
        self.honour_last_modified = False
        self.fail = False
        self.content_type = "application/json"
        self.body: bytes | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("offline", request=request)
        if self.etag and request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304, headers={"etag": self.etag})
        if (
            self.honour_last_modified
            and self.last_modified
            and request.headers.get("if-modified-since") == self.last_modified
        ):
            return httpx.Response(304, headers={"last-modified": self.last_modified})
        headers = {"content-type": self.content_type, "cache-control": "max-age=" + self.max_age}
        if self.etag:
            headers["etag"] = self.etag
        if self.last_modified:
            headers["last-modified"] = self.last_modified
        if self.body is not None:
            return httpx.Response(200, content=self.body, headers=headers)
        return httpx.Response(200, json=self.payload, headers=headers)


def _client(ws_tmp: Path, server: _Server) -> tuple[httpx.Client, http_cache.CachingTransport]:
    transport = http_cache.CachingTransport(
        httpx.MockTransport(server), root=ws_tmp / "cache" / "http", enabled=True
    )
    return httpx.Client(transport=transport), transport


def test_a_fresh_response_never_reaches_the_network(ws_tmp) -> None:
    server = _Server()
    client, _transport = _client(ws_tmp, server)

    first = client.get(URL)
    second = client.get(URL)

    assert first.json() == second.json() == server.payload
    assert len(server.requests) == 1


def test_max_age_zero_revalidates_with_the_etag_and_reuses_the_body(ws_tmp) -> None:
    server = _Server()
    client, transport = _client(ws_tmp, server)
    client.get(URL)
    _age(transport, URL, 3600)

    response = client.get(URL)

    assert response.json() == server.payload
    assert len(server.requests) == 2
    assert server.requests[1].headers["if-none-match"] == ETAG
    # The 304 moved the expiry out again: a third request stays local.
    assert client.get(URL).json() == server.payload
    assert len(server.requests) == 2


def test_last_modified_is_used_when_there_is_no_etag(ws_tmp) -> None:
    server = _Server()
    server.etag = None
    server.last_modified = LAST_MODIFIED
    client, transport = _client(ws_tmp, server)
    client.get(URL)
    _age(transport, URL, 3600)
    server.honour_last_modified = True

    response = client.get(URL)

    assert response.json() == server.payload
    assert server.requests[1].headers["if-modified-since"] == LAST_MODIFIED


def test_no_store_is_never_written(ws_tmp) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"x": 1},
            headers={"content-type": "application/json", "cache-control": "no-store"},
        )

    transport = http_cache.CachingTransport(
        httpx.MockTransport(handler), root=ws_tmp / "cache" / "http", enabled=True
    )
    client = httpx.Client(transport=transport)

    client.get(URL)
    client.get(URL)

    assert list((ws_tmp / "cache" / "http").glob("*")) == []


def test_a_network_failure_falls_back_to_the_stored_copy(ws_tmp) -> None:
    server = _Server()
    client, transport = _client(ws_tmp, server)
    client.get(URL)
    _age(transport, URL, 3600)
    server.fail = True

    assert client.get(URL).json() == server.payload


def test_range_and_binary_responses_bypass_the_cache(ws_tmp) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, content=b"JARDATA", headers={"content-type": "application/octet-stream"}
        )

    transport = http_cache.CachingTransport(
        httpx.MockTransport(handler), root=ws_tmp / "cache" / "http", enabled=True
    )
    client = httpx.Client(transport=transport)

    client.get("https://cdn.example.test/f.jar", headers={"Range": "bytes=0-"})
    client.get("https://cdn.example.test/f.jar", headers={"Range": "bytes=0-"})
    client.get("https://cdn.example.test/f.jar")
    client.get("https://cdn.example.test/f.jar")

    assert len(calls) == 4
    assert list((ws_tmp / "cache" / "http").glob("*")) == []


def test_the_entry_survives_a_new_client(ws_tmp) -> None:
    server = _Server()
    first, _transport = _client(ws_tmp, server)
    first.get(URL)

    second, _ = _client(ws_tmp, server)
    assert second.get(URL).json() == server.payload
    assert len(server.requests) == 1


def test_the_body_is_written_atomically(ws_tmp) -> None:
    server = _Server()
    client, _transport = _client(ws_tmp, server)
    client.get(URL)

    leftovers = [path for path in (ws_tmp / "cache" / "http").glob("*") if ".tmp" in path.name]
    assert leftovers == []


# ---------- project icons ----------


@pytest.fixture()
def icon_server(ws_tmp, monkeypatch):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.headers.get("if-none-match") == ETAG:
            return httpx.Response(304, headers={"etag": ETAG})
        return httpx.Response(
            200, content=PNG, headers={"content-type": "image/png", "etag": ETAG}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    yield client, handler, calls
    http_cache.wait_for_icon_refresh(5.0)


def test_an_icon_is_written_once_and_then_read_from_disk(icon_server) -> None:
    client, _handler, calls = icon_server

    assert http_cache.fetch_icon_bytes(ICON_URL, client=client) == PNG
    assert http_cache.fetch_icon_bytes(ICON_URL, client=client) == PNG

    assert len(calls) == 1
    assert sorted(path.suffix for path in http_cache.icon_cache_dir().glob("*")) == [
        ".json",
        ".png",
    ]


def test_a_cached_icon_can_be_peeked_without_a_worker(icon_server) -> None:
    client, _handler, calls = icon_server
    assert http_cache.cached_icon_bytes(ICON_URL) is None
    http_cache.fetch_icon_bytes(ICON_URL, client=client)

    assert http_cache.cached_icon_bytes(ICON_URL) == PNG
    assert len(calls) == 1


def test_the_icon_still_hits_after_a_restart(icon_server) -> None:
    client, handler, calls = icon_server
    http_cache.fetch_icon_bytes(ICON_URL, client=client)
    before = len(calls)

    # A different client object over the same handler is what a restart means here: no in-memory
    # icon state is shared, so a hit can only come from the disk.
    fresh = httpx.Client(transport=httpx.MockTransport(handler))
    assert http_cache.fetch_icon_bytes(ICON_URL, client=fresh) == PNG
    assert len(calls) == before


def test_a_stale_icon_is_served_at_once_and_refreshed_in_the_background(icon_server) -> None:
    client, _handler, calls = icon_server
    http_cache.fetch_icon_bytes(ICON_URL, client=client)
    meta_path = next(http_cache.icon_cache_dir().glob("*.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["fetched_at"] = 0
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert http_cache.fetch_icon_bytes(ICON_URL, client=client) == PNG
    assert http_cache.wait_for_icon_refresh(5.0)

    refreshed = json.loads(meta_path.read_text(encoding="utf-8"))
    assert float(refreshed["fetched_at"]) > 0
    assert len(calls) == 2  # the first download plus the conditional refresh (304)
    assert http_cache.cached_icon_bytes(ICON_URL) == PNG


def test_an_oversized_icon_is_rejected_and_not_stored(icon_server) -> None:
    oversized = b"x" * (http_cache.ICON_MAX_BYTES + 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"content-type": "image/png"})

    bad: Callable[[httpx.Request], httpx.Response] = handler
    client = httpx.Client(transport=httpx.MockTransport(bad))
    with pytest.raises(ValueError):
        http_cache.fetch_icon_bytes(ICON_URL + "?oversized", client=client)
    assert http_cache.cached_icon_bytes(ICON_URL + "?oversized") is None
