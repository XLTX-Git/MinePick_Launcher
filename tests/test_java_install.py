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

import io
import zipfile

import httpx
import respx

from launcher.java.install import fetch_assets, install_java
from launcher.meta.rules import Platform

WIN = Platform(os="windows", arch="x64", version="10.0.26100")

def _assets_raw(size: int):
    return [
        {
            "release_name": "jdk-21.0.5+11",
            "binary": {
                "os": "windows",
                "architecture": "x64",
                "image_type": "jre",
                "package": {
                    "name": "OpenJDK21U-jre_x64_windows_hotspot_21.0.5_11.zip",
                    "link": "https://github.com/adoptium/x.zip",
                    "size": size,
                },
            },
        },
        {
            "release_name": "jdk-21.0.5+11",
            "binary": {
                "os": "windows",
                "architecture": "x64",
                "image_type": "jdk",
                "package": {
                    "name": "OpenJDK21U-jdk_x64_windows_hotspot_21.0.5_11.zip",
                    "link": "https://github.com/adoptium/y.zip",
                    "size": 99999,
                },
            },
        },
    ]


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("jdk-21.0.5+11-jre/bin/java.exe", "FAKE-JAVA")
        zf.writestr("jdk-21.0.5+11-jre/release", "JAVA_VERSION=\"21.0.5\"")
        zf.writestr("jdk-21.0.5+11-jre/lib/modules", "x")
    return buf.getvalue()


@respx.mock
def test_fetch_assets():
    respx.get(
        "https://api.adoptium.net/v3/assets/latest/21/hotspot",
        params={"os": "windows", "architecture": "x64", "image_type": "jre"},
    ).mock(return_value=httpx.Response(200, json=_assets_raw(12345)))
    assets = fetch_assets(21, platform=WIN)
    assert len(assets) == 2
    assert assets[0].name.endswith(".zip")
    assert assets[0].link.startswith("https://github.com")


def _timeout(request):
    raise httpx.ConnectTimeout("blocked")


@respx.mock
def test_install_java(ws_tmp):
    zip_bytes = _make_zip()
    respx.get(
        "https://api.adoptium.net/v3/assets/latest/21/hotspot",
        params={"os": "windows", "architecture": "x64", "image_type": "jre"},
    ).mock(return_value=httpx.Response(200, json=_assets_raw(len(zip_bytes))))
    respx.head(
        "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/windows/"
        "OpenJDK21U-jre_x64_windows_hotspot_21.0.5_11.zip"
    ).mock(return_value=httpx.Response(404))
    respx.get("https://github.com/adoptium/x.zip").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )
    runtime = install_java(
        21, runtime_dir=ws_tmp / "runtime", platform=WIN, probe_dir=ws_tmp / "probe"
    )
    assert runtime.major == 21
    assert runtime.provider == "managed"
    assert (ws_tmp / "runtime" / "java-21" / "bin" / "java.exe").exists()
    assert not (ws_tmp / "runtime" / "java-21" / "jdk-21.0.5+11-jre").exists()  # top-level directory is stripped


@respx.mock
def test_install_java_existing_returns_cached(ws_tmp):
    # target already exists: no network request
    target = ws_tmp / "runtime" / "java-21" / "bin"
    target.mkdir(parents=True)
    (target / "java.exe").write_text("stub", encoding="utf-8")
    runtime = install_java(21, runtime_dir=ws_tmp / "runtime", platform=WIN)
    assert runtime.major == 21
    assert runtime.provider == "managed"

@respx.mock
def test_fetch_assets_falls_back_to_the_mirror_listing():
    """A blocked API must not fail the install: the mirror directory page lists the same files."""
    respx.get(
        "https://api.adoptium.net/v3/assets/latest/21/hotspot",
        params={"os": "windows", "architecture": "x64", "image_type": "jre"},
    ).mock(side_effect=_timeout)
    listing = (
        '<a href="OpenJDK21U-jre_x64_windows_hotspot_21.0.11_10.zip">old</a>'
        '<a href="OpenJDK21U-jre_x64_windows_hotspot_21.0.13_9.zip">new</a>'
    )
    respx.get("https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/windows/").mock(
        return_value=httpx.Response(200, text=listing)
    )
    respx.head(
        "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/windows/"
        "OpenJDK21U-jre_x64_windows_hotspot_21.0.13_9.zip"
    ).mock(return_value=httpx.Response(200, headers={"Content-Length": "12345"}))

    assets = fetch_assets(21, platform=WIN)
    assert len(assets) == 1
    assert assets[0].source == "mirror"
    assert assets[0].name.endswith("21.0.13_9.zip")  # newest build, not the first line
    assert assets[0].checksum == ""
    assert assets[0].size == 12345


@respx.mock
def test_install_java_downloads_from_the_mirror_host_when_github_is_the_link(ws_tmp):
    """The API answers with a github.com link; on a blocked network the bytes have to come elsewhere."""
    zip_bytes = _make_zip()
    respx.get(
        "https://api.adoptium.net/v3/assets/latest/21/hotspot",
        params={"os": "windows", "architecture": "x64", "image_type": "jre"},
    ).mock(return_value=httpx.Response(200, json=_assets_raw(len(zip_bytes))))
    mirrored = (
        "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/windows/"
        "OpenJDK21U-jre_x64_windows_hotspot_21.0.5_11.zip"
    )
    respx.head(mirrored).mock(return_value=httpx.Response(200))
    respx.get(mirrored).mock(return_value=httpx.Response(200, content=zip_bytes))

    runtime = install_java(
        21, runtime_dir=ws_tmp / "runtime", platform=WIN, probe_dir=ws_tmp / "probe"
    )
    assert runtime.major == 21
    assert (ws_tmp / "runtime" / "java-21" / "bin" / "java.exe").exists()

def test_a_java_install_has_a_task_title_in_every_language() -> None:
    """A task row reads 「安装 Java 21」 - in every shipped language, not only Chinese."""
    from gui import i18n
    from gui.tasks import KIND_TITLE_KEYS

    assert KIND_TITLE_KEYS["java"] == "tasks.title.java"
    for code, _name in i18n.UI_LANGUAGES:
        i18n.set_language(code)
        text = i18n.tr("tasks.title.java", "21")
        assert "21" in text, f"{code}: {text!r} lost the version placeholder"
        assert "tasks.title.java" not in text, f"{code}: key not translated"
    i18n.set_language("zh_cn")
