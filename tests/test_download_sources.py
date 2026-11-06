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

"""Download source resolution: path mapping, the three modes, fallback order and error wording.

No test here reaches the network: the mapping tests are pure string work, everything that needs a
response goes through respx or an ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
import logging

import httpx
import pytest
import respx

from launcher import config as config_mod
from launcher.i18n import tr_core
from launcher.meta import sources
from launcher.meta.manifest import fetch_manifest
from launcher.net.downloader import Downloader, DownloadTask

MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
PISTON_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
VERSION_JSON = "https://piston-meta.mojang.com/v1/packages/aa/1.21.json"
CLIENT_JAR = "https://piston-data.mojang.com/v1/objects/bb/client.jar"
LAUNCHER_JAR = "https://launcher.mojang.com/v1/objects/bb/client.jar"
LIBRARY = "https://libraries.minecraft.net/org/ow2/asm/asm/9.7/asm-9.7.jar"
ASSET = "https://resources.download.minecraft.net/ab/abcdef"

BMCL = "https://bmclapi2.bangbang93.com"
BMCL_BACKUP = "https://bmclapi.bangbang93.com"


@pytest.fixture()
def isolated_config(ws_tmp, monkeypatch):
    """A private launcher data directory, so the source settings of the real user never leak in."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    cfg, path = config_mod.load()
    return cfg, path


def use_modes(cfg, path, *, meta: str = "auto", assets: str = "auto") -> None:
    cfg.download_source_meta = meta
    cfg.download_source_assets = assets
    config_mod.save(cfg, path)


# ---------------------------------------------------------------------------
# (1) path mapping and the three orders, per endpoint family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("official", "mirrors"),
    [
        (
            MANIFEST,
            [
                BMCL + "/mc/game/version_manifest_v2.json",
                BMCL_BACKUP + "/mc/game/version_manifest_v2.json",
            ],
        ),
        (
            VERSION_JSON,
            [
                BMCL + "/v1/packages/aa/1.21.json",
                BMCL_BACKUP + "/v1/packages/aa/1.21.json",
            ],
        ),
        (
            CLIENT_JAR,
            [
                BMCL + "/v1/objects/bb/client.jar",
                BMCL_BACKUP + "/v1/objects/bb/client.jar",
            ],
        ),
        (
            LAUNCHER_JAR,
            [
                BMCL + "/v1/objects/bb/client.jar",
                BMCL_BACKUP + "/v1/objects/bb/client.jar",
            ],
        ),
        (
            LIBRARY,
            [
                BMCL + "/maven/org/ow2/asm/asm/9.7/asm-9.7.jar",
                BMCL_BACKUP + "/maven/org/ow2/asm/asm/9.7/asm-9.7.jar",
            ],
        ),
        (
            ASSET,
            [BMCL + "/assets/ab/abcdef", BMCL_BACKUP + "/assets/ab/abcdef"],
        ),
    ],
)
def test_three_modes_order_the_urls(official: str, mirrors: list[str]) -> None:
    """Official first / mirror first / official only, for every endpoint the launcher downloads."""
    assert sources.candidates(official, mode=sources.MODE_AUTO) == [official, *mirrors]
    assert sources.candidates(official, mode=sources.MODE_MIRROR) == mirrors
    assert sources.candidates(official, mode=sources.MODE_OFFICIAL) == [official]


def test_manifest_candidates_keep_every_official_address_first() -> None:
    """The two official manifest addresses are both tried before any mirror."""
    ordered = sources.candidates_many([PISTON_MANIFEST, MANIFEST], mode=sources.MODE_AUTO)
    assert ordered == [
        PISTON_MANIFEST,
        MANIFEST,
        BMCL + "/mc/game/version_manifest_v2.json",
        BMCL_BACKUP + "/mc/game/version_manifest_v2.json",
    ]


def test_mirror_mode_keeps_the_primary_before_the_backup() -> None:
    assert sources.candidates(CLIENT_JAR, mode=sources.MODE_MIRROR)[0].startswith(BMCL)
    assert sources.candidates(CLIENT_JAR, mode=sources.MODE_MIRROR)[1].startswith(BMCL_BACKUP)


def test_unknown_host_is_never_rewritten() -> None:
    """A host BMCLAPI does not mirror keeps its own single address in every mode."""
    url = "https://example.invalid/files/a.jar"
    for mode in sources.MODES:
        assert sources.candidates(url, mode=mode) == [url]


def test_the_two_settings_are_independent(isolated_config) -> None:
    """Versions/manifests and resource files each follow their own dropdown."""
    cfg, path = isolated_config
    use_modes(cfg, path, meta="official", assets="mirror")
    assert sources.candidates(VERSION_JSON) == [VERSION_JSON]
    assert sources.candidates(ASSET) == [
        BMCL + "/assets/ab/abcdef",
        BMCL_BACKUP + "/assets/ab/abcdef",
    ]
    assert sources.current_modes() == {
        sources.KIND_META: "official",
        sources.KIND_ASSETS: "mirror",
    }


def test_config_rejects_an_unknown_mode(isolated_config) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, meta="nonsense", assets="mirror")
    loaded, _ = config_mod.load()
    assert loaded.download_source_meta == "auto"
    assert loaded.download_source_assets == "mirror"


# ---------------------------------------------------------------------------
# (2) official-only failures stay official
# ---------------------------------------------------------------------------


@respx.mock
def test_official_mode_does_not_fall_back_to_a_mirror(isolated_config) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, meta="official")
    respx.get(MANIFEST).mock(return_value=httpx.Response(503))
    mirror = respx.get(BMCL + "/mc/game/version_manifest_v2.json").mock(
        side_effect=AssertionError("the mirror must not be tried in official mode")
    )
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        sources.fetch_json(client, MANIFEST)
    assert not mirror.called


# ---------------------------------------------------------------------------
# (3) auto tries official first, then the mirror
# ---------------------------------------------------------------------------


@respx.mock
def test_auto_falls_back_to_the_mirror(isolated_config) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, meta="auto")
    respx.get(PISTON_MANIFEST).mock(return_value=httpx.Response(503))
    respx.get(MANIFEST).mock(return_value=httpx.Response(503))
    respx.get(BMCL + "/mc/game/version_manifest_v2.json").mock(
        return_value=httpx.Response(200, json={"latest": {"release": "1.21.8"}, "versions": []})
    )
    with httpx.Client() as client:
        manifest = fetch_manifest(cache_path=None, force=True, client=client)
    assert manifest.latest["release"] == "1.21.8"


@respx.mock
def test_auto_downloader_uses_the_mirror_after_an_official_failure(
    isolated_config, ws_tmp
) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, meta="auto")
    data = b"mirror-served-bytes"
    respx.get(CLIENT_JAR).mock(return_value=httpx.Response(503))
    respx.get(BMCL + "/v1/objects/bb/client.jar").mock(
        return_value=httpx.Response(200, content=data)
    )
    result = Downloader(concurrency=1, segments=1, speed_limit_kb=0, retries=1).download(
        [DownloadTask(CLIENT_JAR, ws_tmp / "client.jar", sha1=_sha1(data), size=len(data))]
    )
    assert result.failed == []
    assert result.downloaded == 1
    assert (ws_tmp / "client.jar").read_bytes() == data


# ---------------------------------------------------------------------------
# (4) a failing mirror primary moves to the mirror backup
# ---------------------------------------------------------------------------


def test_mirror_primary_failure_uses_the_backup(isolated_config) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, assets="mirror")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == sources.MIRROR_PRIMARY_HOST:
            return httpx.Response(502)
        return httpx.Response(200, content=b"asset")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = sources.get(client, ASSET)
    assert response.content == b"asset"
    assert seen == [
        BMCL + "/assets/ab/abcdef",
        BMCL_BACKUP + "/assets/ab/abcdef",
    ]


@respx.mock
def test_downloader_falls_back_to_the_mirror_backup(isolated_config, ws_tmp) -> None:
    """auto: official fails, the mirror primary fails, the mirror backup still serves the file."""
    cfg, path = isolated_config
    use_modes(cfg, path, meta="auto")
    data = b"backup-bytes"
    respx.get(CLIENT_JAR).mock(return_value=httpx.Response(503))
    respx.get(BMCL + "/v1/objects/bb/client.jar").mock(return_value=httpx.Response(502))
    respx.get(BMCL_BACKUP + "/v1/objects/bb/client.jar").mock(
        return_value=httpx.Response(200, content=data)
    )
    result = Downloader(concurrency=1, segments=1, speed_limit_kb=0, retries=1).download(
        [DownloadTask(CLIENT_JAR, ws_tmp / "client.jar", sha1=_sha1(data), size=len(data))]
    )
    assert result.failed == []
    assert (ws_tmp / "client.jar").read_bytes() == data


@respx.mock
def test_official_mode_download_reports_instead_of_switching(isolated_config, ws_tmp) -> None:
    """Official-first is a promise: a failed official download must not quietly use the mirror."""
    cfg, path = isolated_config
    use_modes(cfg, path, meta="official")
    respx.get(CLIENT_JAR).mock(return_value=httpx.Response(503))
    mirror = respx.get(BMCL + "/v1/objects/bb/client.jar").mock(
        side_effect=AssertionError("the mirror must not be tried in official mode")
    )
    result = Downloader(concurrency=1, segments=1, speed_limit_kb=0, retries=1).download(
        [DownloadTask(CLIENT_JAR, ws_tmp / "client.jar", sha1="0" * 40, size=1)]
    )
    assert len(result.failed) == 1
    assert not mirror.called
    assert tr_core("source.mirror") not in result.failed[0][1]


# ---------------------------------------------------------------------------
# (5) both sides failed: the message names both
# ---------------------------------------------------------------------------


@respx.mock
def test_all_sources_failed_message_names_official_and_mirror(
    isolated_config, ws_tmp
) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, meta="auto")
    respx.get(CLIENT_JAR).mock(return_value=httpx.Response(503))
    respx.get(BMCL + "/v1/objects/bb/client.jar").mock(return_value=httpx.Response(503))
    respx.get(BMCL_BACKUP + "/v1/objects/bb/client.jar").mock(
        return_value=httpx.Response(503)
    )
    result = Downloader(concurrency=1, segments=1, speed_limit_kb=0, retries=1).download(
        [DownloadTask(CLIENT_JAR, ws_tmp / "client.jar", sha1="0" * 40, size=1)]
    )
    assert len(result.failed) == 1
    message = result.failed[0][1]
    assert tr_core("source.official") in message
    assert tr_core("source.mirror") in message
    assert not (ws_tmp / "client.jar").exists()


# ---------------------------------------------------------------------------
# (6) path joining with a fake client: no real network
# ---------------------------------------------------------------------------


def test_path_joining_with_a_fake_transport() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == sources.MIRROR_PRIMARY_HOST:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert sources.fetch_json(client, LIBRARY) == {"ok": True}
    assert seen == [LIBRARY, BMCL + "/maven/org/ow2/asm/asm/9.7/asm-9.7.jar"]


def test_fake_client_never_uses_the_official_address_in_mirror_mode(isolated_config) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, meta="mirror")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources.fetch_json(client, VERSION_JSON)
    assert seen == [BMCL + "/v1/packages/aa/1.21.json"]


# ---------------------------------------------------------------------------
# (7) "slow" is a 5 second first byte, applied only when a fallback exists
# ---------------------------------------------------------------------------


def test_first_byte_budget() -> None:
    assert sources.FIRST_BYTE_TIMEOUT == 5.0
    timeout = sources.attempt_timeout()
    assert timeout.connect == 5.0
    assert timeout.read == 5.0
    # A single candidate keeps the client's historical timeout: nothing to switch to.
    assert sources.request_timeout(1) is None
    assert sources.request_timeout(2) is not None


def test_a_slow_official_attempt_switches_to_the_mirror(isolated_config, caplog) -> None:
    cfg, path = isolated_config
    use_modes(cfg, path, assets="auto")
    sources.reset_switch_log_for_tests()
    calls: list[tuple[str, httpx.Timeout | None]] = []

    class SlowOfficial:
        """A stand-in for the shared client: the official address times out, the mirror answers."""

        def get(self, url: str, **kwargs) -> httpx.Response:
            calls.append((url, kwargs.get("timeout")))
            if "resources.download.minecraft.net" in url:
                raise httpx.ReadTimeout("first byte never arrived")
            return httpx.Response(200, content=b"asset", request=httpx.Request("GET", url))

    caplog.set_level(logging.INFO, logger="launcher.meta.sources")
    response = sources.get(SlowOfficial(), ASSET)
    assert response.content == b"asset"
    assert calls[0][0] == ASSET
    assert calls[0][1] is not None and calls[0][1].read == 5.0
    assert calls[1][0] == BMCL + "/assets/ab/abcdef"
    assert "下载源切换" in caplog.text
    assert "5" in caplog.text


def test_downloader_retries_once_per_source_when_a_fallback_exists(
    isolated_config, ws_tmp
) -> None:
    """The switch must be immediate: no three backoff retries against the same dead official URL."""
    cfg, path = isolated_config
    use_modes(cfg, path, meta="auto")
    calls: list[str] = []
    data = b"ok"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "piston-data.mojang.com":
            return httpx.Response(503)
        return httpx.Response(200, content=data)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Downloader(
            client=client,
            concurrency=1,
            segments=1,
            speed_limit_kb=0,
            retries=5,
            retry_wait=0.01,
        ).download(
            [DownloadTask(CLIENT_JAR, ws_tmp / "client.jar", sha1=_sha1(data), size=len(data))]
        )
    assert result.downloaded == 1
    official_attempts = [url for url in calls if "piston-data.mojang.com" in url]
    assert official_attempts == [CLIENT_JAR]


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()
