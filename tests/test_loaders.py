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

import httpx
import pytest
import respx

from launcher.mods.loaders import (
    ModsError,
    display_loader_name,
    list_game_versions,
    list_loader_versions,
)

FABRIC_LOADERS = [
    {"separator": ".", "build": 3, "maven": "net.fabricmc:fabric-loader:0.19.3", "version": "0.19.3", "stable": True},
    {"separator": ".", "build": 2, "maven": "net.fabricmc:fabric-loader:0.19.2", "version": "0.19.2", "stable": True},
    {"separator": ".", "build": 1, "maven": "net.fabricmc:fabric-loader:0.15.11", "version": "0.15.11", "stable": False},
]
FABRIC_INSTALLERS = [
    {"url": "https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.1.2/fabric-installer-1.1.2.jar", "version": "1.1.2", "stable": True},
]
FORGE_PROMOS = {
    "promos": {
        "1.20.1-latest": "47.4.22",
        "1.20.1-recommended": "47.4.10",
        "1.16.5-latest": "36.2.42",
    }
}
NEOFORGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <versioning>
    <latest>26.1.2.95</latest>
    <versions>
      <version>20.2.86</version>
      <version>20.2.88</version>
      <version>20.2.12-beta</version>
      <version>21.1.247</version>
      <version>26.1.2.94</version>
      <version>26.1.2.95</version>
    </versions>
  </versioning>
</metadata>
"""


def test_display_loader_name():
    assert display_loader_name("fabric") == "Fabric"
    assert display_loader_name("forge") == "Forge"
    assert display_loader_name("neoforge") == "NeoForge"
    assert display_loader_name("quilt") == "Quilt"
    assert display_loader_name("unknown") == "unknown"


@respx.mock
def test_fabric_versions():
    # The per-game endpoint: Fabric marks only the newest loader `stable`, so the global list filtered on
    # that flag left a picker with exactly one build.
    respx.get("https://meta.fabricmc.net/v2/versions/loader/1.20.1").mock(
        return_value=httpx.Response(200, json=[{"loader": entry} for entry in FABRIC_LOADERS])
    )
    respx.get("https://meta.fabricmc.net/v2/versions/installer").mock(
        return_value=httpx.Response(200, json=FABRIC_INSTALLERS)
    )
    versions = list_loader_versions("fabric", "1.20.1")
    assert [v.version for v in versions] == ["0.19.3", "0.19.2", "0.15.11"]  # every build, newest first
    assert [v.stable for v in versions] == [True, True, False]  # the source's own flag is kept
    assert all(v.installer_url.endswith(".jar") for v in versions)


@respx.mock
def test_forge_versions():
    respx.get(
        "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
    ).mock(return_value=httpx.Response(200, json=FORGE_PROMOS))
    versions = list_loader_versions("forge", "1.20.1")
    assert [v.version for v in versions] == ["47.4.22", "47.4.10"]
    assert versions[1].recommended is True
    assert "47.4.22" in versions[0].installer_url
    assert list_loader_versions("forge", "1.12.2") == []


@respx.mock
def test_neoforge_versions():
    respx.get(
        "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml"
    ).mock(return_value=httpx.Response(200, text=NEOFORGE_XML))
    v1202 = list_loader_versions("neoforge", "1.20.2")
    assert [v.version for v in v1202] == ["20.2.88", "20.2.86"]
    v2612 = list_loader_versions("neoforge", "26.1.2")
    assert [v.version for v in v2612] == ["26.1.2.95", "26.1.2.94"]  # newest first
    assert "26.1.2.95" in v2612[0].installer_url


@respx.mock
@respx.mock
def test_neoforge_takes_whichever_host_answers_first():
    """Both hosts are asked at once; the slow one is abandoned instead of waited for.

    Sequential fallback would pay the dead host's timeout before the good one is even contacted, which
    is the whole reason this path races.
    """
    import time

    def _slow(**kwargs):
        time.sleep(1.5)
        return httpx.Response(200, text=NEOFORGE_XML)

    respx.get(
        "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml"
    ).mock(side_effect=_slow)
    respx.get(
        "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
    ).mock(return_value=httpx.Response(200, text=NEOFORGE_XML))

    started = time.monotonic()
    versions = list_loader_versions("neoforge", "1.20.2")
    elapsed = time.monotonic() - started

    assert versions
    assert elapsed < 1.0, f"waited {elapsed:.2f}s for the slow host"
    assert all(
        item.installer_url.startswith(
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
        )
        for item in versions
    )


@respx.mock
def test_neoforge_falls_back_to_the_official_host():
    """The mirror is asked first; when it is down the official maven still answers.

    ``maven.neoforged.net`` is unreachable from mainland China, which is why the mirror leads - but a
    mirror outage must not turn into a version page that cannot work at all.
    """
    respx.get(
        "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml"
    ).mock(side_effect=httpx.ConnectError("mirror down"))
    respx.get(
        "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
    ).mock(return_value=httpx.Response(200, text=NEOFORGE_XML))

    versions = list_loader_versions("neoforge", "1.20.2")

    assert versions
    assert all(
        item.installer_url.startswith(
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
        )
        for item in versions
    ), "the jar has to come from the host that answered"


@respx.mock
def test_neoforge_installer_url_follows_the_mirror():
    """When the mirror answers, the installer comes from the mirror too - not from the dead host."""
    respx.get(
        "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml"
    ).mock(return_value=httpx.Response(200, text=NEOFORGE_XML))

    versions = list_loader_versions("neoforge", "1.20.2")

    assert versions
    assert all(
        item.installer_url.startswith(
            "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/"
        )
        for item in versions
    )


@respx.mock
def test_neoforge_reports_when_both_hosts_fail():
    """Both down is an error the page can show, not an empty list."""
    for url in (
        "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml",
        "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml",
    ):
        respx.get(url).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(ModsError):
        list_loader_versions("neoforge", "1.20.2")


def test_neoforge_game_versions():
    respx.get(
        "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml"
    ).mock(return_value=httpx.Response(200, text=NEOFORGE_XML))
    games = list_game_versions("neoforge")
    assert "1.20.2" in games
    assert "26.1.2" in games
    assert "26.1.2.95" not in games  # build numbers do not appear


def test_unknown_loader():
    with pytest.raises(ModsError):
        list_loader_versions("quilt", "1.20.1")
