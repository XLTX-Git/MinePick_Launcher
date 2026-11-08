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

"""Unit tests for launcher/mods/curseforge.py: search parsing, key priority, file filtering."""

import httpx
import pytest

from launcher.mods import curseforge as cf
from launcher.mods.loaders import ModsError
from launcher.mods.models import ModSearchHit


@pytest.fixture(autouse=True)
def _isolated_key_state(monkeypatch):
    """No test may inherit an exported key or the previous test's 401/403 latch."""
    monkeypatch.delenv(cf.CF_KEY_ENV, raising=False)
    cf.reset_session_state()
    yield
    cf.reset_session_state()


def test_loader_ids_include_quilt() -> None:
    assert cf.LOADER_IDS["quilt"] == 5
    assert cf.LOADER_IDS["fabric"] == 4
    assert cf.LOADER_IDS["forge"] == 1
    assert cf.LOADER_IDS["neoforge"] == 6


def _client(payload: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_parses_hits() -> None:
    payload = {
        "data": [
            {
                "id": 394468,
                "name": "Sodium",
                "summary": "A modern rendering engine",
                "downloadCount": 142812847,
                "logo": {"thumbnailUrl": "https://x/thumb.png"},
                "dateModified": "2026-10-01T12:00:00Z",
                "latestFilesIndexes": [
                    {"modLoader": 4},  # Fabric
                    {"modLoader": 6},  # NeoForge
                    {"modLoader": 4},  # duplicate: the row lists each loader once
                ],
            }
        ]
    }
    hits = cf.search_projects("sodium", limit=10, client=_client(payload))
    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, ModSearchHit)
    assert h.slug == "394468"
    assert h.title == "Sodium"
    assert h.downloads == 142812847
    assert h.icon_url == "https://x/thumb.png"
    # The card's "when was it updated" + "which loaders" segments come from these two fields.
    assert h.updated == "2026-10-01T12:00:00Z"
    assert h.loaders == ["fabric", "neoforge"]
    assert h.source == "curseforge"


def test_search_params_include_filters() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cf.search_projects(
        "x",
        limit=5,
        sort="downloads",
        kind="resourcepack",
        game_version="1.20.1",
        client=client,
    )
    assert seen["classId"] == "12"
    assert seen["gameVersion"] == "1.20.1"
    assert seen["sortField"] == "TotalDownloads"
    assert seen["sortOrder"] == "desc"


@pytest.mark.parametrize(
    ("sort", "sort_field"),
    [
        ("relevance", None),  # no field: the API's own match order (see SORT_FIELDS)
        ("downloads", "TotalDownloads"),
        ("updated", "LastUpdated"),
        ("newest", "ReleasedDate"),
    ],
)
def test_search_sort_mapping_and_offset(sort: str, sort_field: str | None) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": [], "pagination": {"totalCount": 0}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cf.search_projects("x", limit=15, offset=30, sort=sort, client=client)
    assert seen["index"] == "30"
    assert seen["pageSize"] == "15"
    assert seen.get("sortField") == sort_field
    assert ("sortOrder" in seen) is (sort_field is not None)


def test_search_reads_total_count_and_source() -> None:
    payload = {
        "data": [{"id": 1, "name": "A", "summary": "", "downloadCount": 1}],
        "pagination": {"index": 15, "pageSize": 15, "resultCount": 1, "totalCount": 61},
    }
    hits = cf.search_projects("x", limit=15, offset=15, client=_client(payload))
    assert hits.total == 61
    assert len(hits) == 1
    assert hits[0].source == "curseforge"


@pytest.mark.parametrize(
    ("status", "needle"),
    [(429, "API Key"), (403, "API Key"), (401, "API Key")],
)
def test_search_auth_and_rate_limit_point_at_the_key_setting(
    status: int, needle: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ModsError) as excinfo:
        cf.search_projects("x", client=client)
    message = str(excinfo.value)
    # The message must name the fix, not just the status code: a user with a revoked or exhausted
    # key needs to know to enter their own key in Settings.
    assert needle in message
    assert "设置" in message or "Settings" in message


def test_list_files_keeps_release_only() -> None:
    payload = {
        "data": [
            {"id": 1, "releaseType": 1, "displayName": "A", "downloadUrl": "https://x/a.jar", "fileLength": 100},
            {"id": 2, "releaseType": 2, "displayName": "B", "downloadUrl": "https://x/b.jar", "fileLength": 100},
            {"id": 3, "releaseType": 1, "displayName": "C", "downloadUrl": None, "fileLength": 100},
            {"id": 4, "releaseType": 1, "displayName": "D", "downloadUrl": "https://x/d.jar", "fileLength": 200},
        ]
    }
    files = cf.list_files(1, client=_client(payload))
    assert [f.name for f in files] == ["D", "A"]  # newest files first


def test_effective_key_prefers_config(monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp))
    monkeypatch.setattr(cf, "_bundled_key", lambda: "bundled-key")
    from launcher import config

    cfg, cfg_path = config.load()
    cfg.curseforge_api_key = "user-key"
    config.save(cfg, cfg_path)
    assert cf.effective_api_key() == "user-key"
    cfg.curseforge_api_key = ""
    config.save(cfg, cfg_path)
    assert cf.effective_api_key() == "bundled-key"


def test_environment_key_overrides_config_and_bundled_key(monkeypatch, ws_tmp):
    """MCLAUNCHER_CF_KEY wins over both stored and bundled values; blank means unset."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp))
    monkeypatch.setattr(cf, "_bundled_key", lambda: "bundled-key")
    from launcher import config

    cfg, cfg_path = config.load()
    cfg.curseforge_api_key = "user-key"
    config.save(cfg, cfg_path)

    monkeypatch.setenv(cf.CF_KEY_ENV, "env-key")
    assert cf.effective_api_key() == "env-key"
    # An exported-but-empty variable must not shadow a working key.
    monkeypatch.setenv(cf.CF_KEY_ENV, "   ")
    assert cf.effective_api_key() == "user-key"


def test_missing_key_is_unavailable_and_makes_no_request(monkeypatch) -> None:
    """The no-key state is a capability answer, not an error the user discovers by searching."""
    monkeypatch.setattr(cf, "effective_api_key", lambda: "")
    opened = []
    monkeypatch.setattr(cf, "_new_client", lambda: opened.append("client"))

    assert cf.key_status() == cf.KEY_STATUS_MISSING
    assert not cf.has_key()
    assert not cf.available()
    with pytest.raises(ModsError) as excinfo:
        cf.search_projects("sodium")
    assert "MCLAUNCHER_CF_KEY" in str(excinfo.value)
    assert opened == []  # refused before the shared client (and therefore any request)


def test_403_latches_the_source_off_for_the_rest_of_the_session(monkeypatch) -> None:
    monkeypatch.setattr(cf, "effective_api_key", lambda: "session-key")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert cf.available()
    with pytest.raises(ModsError):
        cf.search_projects("x", client=client)
    assert len(calls) == 1
    assert cf.key_status() == cf.KEY_STATUS_REJECTED
    assert not cf.available()

    # From now on the normal (page) path refuses before creating a request at all.
    monkeypatch.setattr(cf, "_new_client", lambda: calls.append("client"))
    with pytest.raises(ModsError):
        cf.search_projects("x")
    assert len(calls) == 1  # the one 403 request; the latch stopped the next call

