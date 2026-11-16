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

"""Download and install the Adoptium Temurin JRE (api.adoptium.net v3).

Managed runtime directory layout: <launcher data dir>/runtime/java-<major>/bin/java(.exe).
Download links ultimately point at github.com (Adoptium release assets), so the system certificate store is needed (see meta.manifest._enable_system_ca).
"""

from __future__ import annotations

import re
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from launcher.i18n import describe_network_error, tr_core
from launcher.java.locate import JavaError, JavaRuntime, probe_java_major
from launcher.meta.rules import Platform, detect_platform
from launcher.net.downloader import Downloader, DownloadProgress, DownloadTask

ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
#: Fallback listing when the API is unreachable. api.adoptium.net is often blocked from mainland China
#: (measured: read timeout after ~12 s on a connection that works), while this mirror answers in under a
#: second. It serves files only, so the listing is parsed from the directory page and there is no
#: upstream sha256 - the downloaded archive is integrity-checked instead (see install_java).
ADOPTIUM_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/"
#: The API call may not stall the install for long: a blocked host has to fail fast enough to fall back.
API_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
#: Short probes (HEAD on a mirror file) use the same connect budget without a long read wait.
PROBE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_OS_MAP = {"windows": "windows", "osx": "mac", "linux": "linux"}
_ARCH_MAP = {"x64": "x64", "x86": "x86", "arm64": "aarch64"}


@dataclass(frozen=True)
class JavaAsset:
    release_name: str
    os: str
    arch: str
    image_type: str
    link: str
    name: str
    size: int
    checksum: str = ""  # official sha256 (package.checksum, integrity verification)
    source: str = "adoptium"  # "adoptium" (API, with checksum) or "mirror" (listing, no checksum)


def _version_key(name: str) -> tuple[int, ...]:
    """Sort key for a mirror file name: ``..._hotspot_<version>_<build>.zip``.

    Taken after ``hotspot_`` so version and build compare as one tuple: splitting on the last underscore
    compared only the build number, which ranked 21.0.11 build 10 above 21.0.13 build 9.
    """
    tail = name.split("hotspot_")[-1] if "hotspot_" in name else name
    numbers = re.findall(r"\d+", tail)
    return tuple(int(part) for part in numbers) or (0,)


def _mirror_assets(
    major: int, os_name: str, arch: str, image_type: str, client: httpx.Client
) -> list[JavaAsset]:
    """Adoptium's file listing from the Tsinghua mirror, newest build first.

    There is no API here: the directory page is parsed for ``OpenJDK*.zip`` entries, and the newest
    version numbers win. No sha256 is published with the file, so the archive is validated by its own
    CRCs at extraction time and the caller is told which source it came from.
    """
    base = f"{ADOPTIUM_MIRROR}{major}/{image_type}/{arch}/{os_name}/"
    resp = client.get(base)
    resp.raise_for_status()
    names = re.findall(r'href="(OpenJDK[^"/]+\.zip)"', resp.text)
    if not names:
        raise JavaError(tr_core("java.no_asset", major))
    name = max(names, key=_version_key)
    link = base + name
    size = 0
    try:
        head = client.head(link)
        if head.status_code < 400:
            size = int(head.headers.get("Content-Length") or 0)
    except (httpx.HTTPError, ValueError):
        size = 0
    return [
        JavaAsset(
            release_name=name,
            os=os_name,
            arch=arch,
            image_type=image_type,
            link=link,
            name=name,
            size=size,
            checksum="",
            source="mirror",
        )
    ]


def fetch_assets(
    major: int,
    *,
    platform: Platform | None = None,
    image_type: str = "jre",
    client: httpx.Client | None = None,
) -> list[JavaAsset]:
    """Query the official asset list (with its checksums); fall back to the mirror listing.

    The official API is tried first because only it publishes a sha256. When it is unreachable - the
    normal case on a mainland connection, where the host simply does not answer - the mirror listing is
    used instead of failing the whole install.
    """
    platform = platform or detect_platform()
    os_name = _OS_MAP.get(platform.os, platform.os)
    arch = _ARCH_MAP.get(platform.arch, platform.arch)
    if client is None:
        with httpx.Client(timeout=API_TIMEOUT, follow_redirects=True) as own_client:
            try:
                return fetch_assets(
                    major, platform=platform, image_type=image_type, client=own_client
                )
            except JavaError:
                return _mirror_assets(major, os_name, arch, image_type, own_client)
    url = (
        ADOPTIUM_API.format(major=major)
        + "?os="
        + os_name
        + "&architecture="
        + arch
        + "&image_type="
        + image_type
    )
    try:
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        raise JavaError(
            tr_core("java.assets_failed", describe_network_error(exc))
        ) from exc

    assets: list[JavaAsset] = []
    for release in payload:
        binary = release.get("binary") or {}
        package = binary.get("package") or {}
        name = package.get("name") or ""
        if not name.endswith((".zip", ".tar.gz")):
            continue
        assets.append(
            JavaAsset(
                release_name=release.get("release_name") or "",
                os=binary.get("os") or "",
                arch=binary.get("architecture") or "",
                image_type=binary.get("image_type") or "",
                link=package.get("link") or "",
                name=name,
                size=int(package.get("size") or 0),
                checksum=package.get("checksum") or "",
            )
        )
    return assets


def _extract_zip(zip_path: Path, target_dir: Path) -> None:
    """Extract the zip to the target directory, stripping the zip's top-level directory (Adoptium package layout)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            parts = member.filename.split("/")
            rel = "/".join(parts[1:]) if len(parts) > 1 and parts[0] else member.filename
            if not rel:
                continue
            dest = target_dir / rel
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, dest.open("wb") as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)


def list_managed_runtimes(
    runtime_dir: Path | None = None,
) -> list[tuple[int, Path]]:
    """List managed Java runtimes (major, directory), sorted by major ascending."""
    from launcher import paths as launcher_paths

    runtime_dir = runtime_dir or (launcher_paths.launcher_dir() / "runtime")
    if not runtime_dir.exists():
        return []
    out: list[tuple[int, Path]] = []
    for entry in sorted(runtime_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            major = int(entry.name.removeprefix("java-"))
        except ValueError:
            continue
        out.append((major, entry))
    return out


def delete_managed_runtime(major: int, runtime_dir: Path | None = None) -> Path:
    """Delete a managed Java runtime directory."""
    match = next(
        (d for m, d in list_managed_runtimes(runtime_dir) if m == major), None
    )
    if match is None:
        raise JavaError(tr_core("java.runtime_missing", major))
    shutil.rmtree(match, ignore_errors=True)
    return match


def _mirror_url_for(asset: JavaAsset, major: int, client: httpx.Client) -> str:
    """The mirror URL for the same file name, when the mirror actually has it.

    The official API answers with a github.com download link, and github.com is unreachable from the
    same networks where api.adoptium.net is blocked - so the listing succeeds and the download then
    fails. The mirror keeps the upstream file names, which also means the API's own sha256 still
    describes the bytes.
    """
    base = f"{ADOPTIUM_MIRROR}{major}/{asset.image_type}/{_ARCH_MAP.get(asset.arch, asset.arch)}/"
    base += f"{_OS_MAP.get(asset.os, asset.os)}/"
    candidate = base + asset.name
    try:
        head = client.head(candidate)
    except Exception:  # noqa: BLE001 - a probe may never abort the install
        return ""
    return candidate if head.status_code < 400 else ""


def install_java(
    major: int,
    *,
    runtime_dir: Path | None = None,
    platform: Platform | None = None,
    probe_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
    client: httpx.Client | None = None,
) -> JavaRuntime:
    """Download and install an Adoptium JRE into the managed directory; return directly if already installed."""
    from launcher import paths as launcher_paths

    runtime_dir = runtime_dir or (launcher_paths.launcher_dir() / "runtime")
    target_dir = runtime_dir / ("java-" + str(major))
    exe_name = "java.exe" if (platform or detect_platform()).os == "windows" else "java"
    existing = target_dir / "bin" / exe_name
    if existing.exists():
        probed = probe_java_major(existing, probe_dir=probe_dir)
        if probed is not None:
            return JavaRuntime(path=existing, major=probed.major, provider="managed", version=probed.version)
        # The directory exists but its java does not run (a half-extracted or corrupted install). It used
        # to be handed back as a working runtime, which then failed at launch with no explanation. Drop
        # it and install again.
        shutil.rmtree(target_dir, ignore_errors=True)

    assets = fetch_assets(major, platform=platform, client=client)
    if not assets:
        raise JavaError(tr_core("java.no_asset", major))
    asset = assets[0]

    # The probe gets its own client: the caller may hand one in for the *download*, and closing it here
    # would hand a closed client to the Downloader.
    probe_client = client or httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=True)
    try:
        if "github.com" in asset.link or "githubusercontent" in asset.link:
            mirrored = _mirror_url_for(asset, major, probe_client)
            if mirrored:
                asset = JavaAsset(
                    release_name=asset.release_name,
                    os=asset.os,
                    arch=asset.arch,
                    image_type=asset.image_type,
                    link=mirrored,
                    name=asset.name,
                    size=asset.size,
                    checksum=asset.checksum,
                    source=asset.source + "+mirror-host" if asset.source else "mirror-host",
                )
    finally:
        if client is None:
            probe_client.close()

    downloads_dir = runtime_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    zip_path = downloads_dir / asset.name
    result = Downloader(client=client).download(
        [
            DownloadTask(
                url=asset.link,
                dest=zip_path,
                size=asset.size,
                sha256=asset.checksum or None,
            )
        ],
        progress=progress,
    )
    if result.failed:
        raise JavaError(tr_core("java.download_failed", result.failed[0][1]))
    if not asset.checksum:
        # The mirror publishes no sha256. Every entry in the archive carries a CRC, so a truncated or
        # corrupted transfer is caught here instead of producing a java.exe that cannot start.
        try:
            with zipfile.ZipFile(zip_path) as archive:
                broken = archive.testzip()
        except (zipfile.BadZipFile, OSError) as exc:
            raise JavaError(tr_core("java.archive_corrupt", str(exc))) from exc
        if broken is not None:
            raise JavaError(tr_core("java.archive_corrupt", broken))

    _extract_zip(zip_path, target_dir)
    java_path = target_dir / "bin" / exe_name
    if not java_path.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
        raise JavaError(tr_core("java.extract_failed", str(java_path)))
    probed = probe_java_major(java_path, probe_dir=probe_dir)
    if probed is None:
        # Installed but unable to start: remove it so the next attempt is a clean download instead of a
        # runtime the launcher keeps offering.
        shutil.rmtree(target_dir, ignore_errors=True)
        raise JavaError(tr_core("java.probe_failed", str(java_path)))
    return JavaRuntime(path=java_path, major=probed.major, provider="managed", version=probed.version)
