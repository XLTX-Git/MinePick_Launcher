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

"""Download, verify and unpack the Terracotta payload into ``<launcher dir>/components/terracotta``.

The upstream release is a 8.1 MB ``tar.gz`` holding one executable and ``VCRUNTIME140.DLL``. The
launcher never executes anything it has not verified: the expected byte length and SHA-256 below
are constants for the exact v0.4.2 asset, so a truncated download, a mirror serving a different
build, or an HTML error page is refused before the archive is opened. The values were measured
from the official asset (CNB mirror, 2026-11); all three official mirrors publish the same file.

Unpacking uses :mod:`tarfile` only, but not ``extractall``: a tar entry name is attacker-controlled
data exactly like a ZIP member name, and ``../``, an absolute path or a drive-qualified name must not
write outside the component directory. The path check is the one already used for natives jars
(:func:`launcher.launch.natives._member_target`), imported rather than copied so the two extraction
paths cannot drift apart.

The installation is staged: files land in a temporary folder next to the target, are moved into
place only after the archive passed verification and unpacking, and the metadata file is written
last. A partly installed component therefore never reports "ready".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from launcher import paths
from launcher.link import mirrors as mirror_module
from launcher.link.selfcheck import ComponentStatus

logger = logging.getLogger(__name__)

COMPONENTS_DIRNAME = "components"
TERRACOTTA_DIRNAME = "terracotta"
METADATA_NAME = "installed.json"
DOWNLOAD_DIRNAME = ".download"
STAGING_PREFIX = ".staging-"

#: Hard ceiling for one archive member / for everything unpacked. The real package is 8.1 MB
#: compressed and ~9.5 MB extracted, so these only ever stop a corrupted or hostile archive.
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_EXTRACT_BYTES = 64 * 1024 * 1024
CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class PackageSpec:
    """One verified release artifact: what to download and what it must contain."""

    version: str
    asset: str
    size: int
    sha256: str
    executable: str
    dll: str = ""


#: Terracotta v0.4.2, Windows x86_64 (the release asset measured in this checkout). The SHA-256
#: and byte count belong to this exact version; a different release needs its own constants.
TERRACOTTA_PACKAGE = PackageSpec(
    version="0.4.2",
    asset="terracotta-0.4.2-windows-x86_64-pkg.tar.gz",
    size=8_135_914,
    sha256="07ebe139e3ca5f74576e58b1a96efe59abdfbe148d3f1a49bfdca8b6f70745f0",
    executable="terracotta-0.4.2-windows-x86_64.exe",
    dll="VCRUNTIME140.DLL",
)

ProgressCallback = Callable[[int, int], None]


class ComponentError(Exception):
    """A user-facing component failure: ``key`` is the i18n key shown in the page's row."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


def package_for_platform() -> PackageSpec | None:
    """The package for this machine, or ``None`` where no verified asset exists.

    Only the Windows x86_64 asset has been verified for this release. Other platforms keep the
    onboarding page working (the component simply reports "not downloaded") and a download attempt
    fails with a clear message instead of installing a binary that cannot run.
    """
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return TERRACOTTA_PACKAGE
    return None


def component_dir() -> Path:
    """``<launcher dir>/components/terracotta`` (resolved at call time)."""
    return paths.launcher_dir() / COMPONENTS_DIRNAME / TERRACOTTA_DIRNAME


def executable_path(package: PackageSpec | None = None) -> Path | None:
    """The installed executable, or ``None`` when this platform has no package at all."""
    spec = package if package is not None else package_for_platform()
    return component_dir() / spec.executable if spec is not None else None


def component_status(package: PackageSpec | None = None) -> ComponentStatus:
    """Whether the payload is installed, plus the version and path the self-check row shows."""
    spec = package if package is not None else package_for_platform()
    if spec is None:
        return ComponentStatus(downloaded=False, supported=False)
    target = component_dir() / spec.executable
    if not target.is_file():
        return ComponentStatus(downloaded=False, supported=True)
    version = spec.version
    try:
        raw = json.loads((component_dir() / METADATA_NAME).read_text(encoding="utf-8"))
        reported = str(raw.get("version") or "")
        if reported:
            version = reported
    except (OSError, ValueError):
        pass
    return ComponentStatus(
        downloaded=True,
        version=version,
        supported=True,
        path=str(target),
    )


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, read in chunks (verification must not load 8 MB into memory twice)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove() -> int:
    """Delete the installed payload directory; return how many bytes it held.

    Only ``<launcher dir>/components/terracotta`` is touched: ``components/easytier`` belongs to the
    network self-check and must survive, which is why this resolves the one directory through
    :func:`component_dir` instead of clearing ``components/``.

    Raises :class:`ComponentError` with a translation key and the OS detail when the directory cannot
    be removed - on Windows a running Terracotta keeps its own executable open, and that reason has
    to reach the user instead of being swallowed. A missing directory is not an error: it simply
    means there was nothing left to delete, and the byte count is 0.
    """
    target = component_dir()
    if not target.is_dir():
        return 0
    size = _tree_size(target)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise ComponentError("tools.disable.fail", _describe_os_error(target, exc)) from exc
    return size


def _tree_size(root: Path) -> int:
    """Total size of the regular files under ``root`` (unreadable entries count as 0)."""
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _describe_os_error(target: Path, exc: OSError) -> str:
    """The reason to show for a failed removal: the OS message names the file that is in use."""
    filename = str(getattr(exc, "filename", "") or "").strip()
    if filename:
        return f"{filename}: {exc.strerror or exc}"
    return f"{target}: {exc.strerror or exc}"


def verify_archive(path: Path, package: PackageSpec) -> bool:
    """Byte length and SHA-256 both have to match the release constants."""
    try:
        if path.stat().st_size != package.size:
            return False
        return sha256_file(path) == package.sha256
    except OSError:
        return False


def install(
    progress: ProgressCallback | None = None,
    *,
    package: PackageSpec | None = None,
    mirrors: Sequence[mirror_module.Mirror] = mirror_module.MIRRORS,
    preferred: str = "",
    cache_path: Path | None = None,
    timeout: float = 30.0,
) -> ComponentStatus:
    """Download and install the payload; raise :class:`ComponentError` with an i18n key on failure.

    ``progress`` receives ``(done_bytes, total_bytes)`` from the worker thread that runs this.
    The mirror race picks the first responder; if that mirror then fails mid-transfer, the other
    mirrors are tried in their measured order before the failure is reported.
    """
    spec = package if package is not None else package_for_platform()
    if spec is None:
        raise ComponentError("tools.fail.platform", "no verified Terracotta asset for this platform")
    existing = component_status(spec)
    if existing.downloaded:
        _report(progress, 1, 1)
        return existing

    destination = component_dir()
    download_dir = destination / DOWNLOAD_DIRNAME
    archive = download_dir / spec.asset
    part = download_dir / (spec.asset + ".part")
    staging = destination / f"{STAGING_PREFIX}{os.getpid()}-{threading.get_ident()}"
    mirror = mirror_module.choose_mirror(
        version=spec.version,
        asset=spec.asset,
        mirrors=mirrors,
        preferred=preferred,
        path=cache_path if cache_path is not None else mirror_module.cache_path(),
    )
    ordered = [mirror] + [item for item in mirrors if item.key != mirror.key]
    try:
        destination.mkdir(parents=True, exist_ok=True)
        download_dir.mkdir(parents=True, exist_ok=True)
        failure: ComponentError | None = None
        for candidate in ordered:
            try:
                _report(progress, 0, spec.size)
                _download(candidate.url(spec.version, spec.asset), part, spec.size, progress, timeout)
                if not verify_archive(part, spec):
                    failure = ComponentError(
                        "tools.fail.verify",
                        f"{spec.asset}: size/hash mismatch from {candidate.key}",
                    )
                    part.unlink(missing_ok=True)
                    continue
                break
            except ComponentError as exc:
                failure = exc
                part.unlink(missing_ok=True)
                continue
            except (httpx.HTTPError, OSError) as exc:
                failure = ComponentError("tools.fail.download", str(exc))
                part.unlink(missing_ok=True)
                continue
        else:
            raise failure or ComponentError("tools.fail.download", "no mirror served the archive")

        _prepare_staging(staging)
        _extract_archive(part, staging)
        executable = _find_member(staging, spec.executable)
        if executable is None:
            raise ComponentError(
                "tools.fail.verify", f"{spec.asset} contains no {spec.executable}"
            )
        _install_file(executable, destination / spec.executable)
        if spec.dll:
            library = _find_member(staging, spec.dll)
            if library is not None:
                _install_file(library, destination / spec.dll)
        _write_metadata(destination, spec)
        _report(progress, 1, 1)
        return component_status(spec)
    finally:
        _remove_tree(staging)
        part.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)


def _report(progress: ProgressCallback | None, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(max(0, int(done)), max(0, int(total)))
    except Exception:
        logger.debug("下载进度回调失败", exc_info=True)


def _download(
    url: str,
    target: Path,
    expected_size: int,
    progress: ProgressCallback | None,
    timeout: float,
) -> None:
    """Stream one mirror into ``target``; raises ``httpx.HTTPError`` / ``OSError`` on failure."""
    with (
        httpx.Client(follow_redirects=True, timeout=httpx.Timeout(timeout)) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        total = response.headers.get("Content-Length")
        total_bytes = int(total) if (total or "").isdigit() else expected_size
        done = 0
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(CHUNK):
                handle.write(chunk)
                done += len(chunk)
                _report(progress, done, total_bytes or done)


def _prepare_staging(staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    for child in staging.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _extract_archive(archive: Path, destination: Path) -> int:
    """Unpack regular files into ``destination``; reject every entry that would escape it.

    Returns how many entries were skipped. Symbolic links, hard links and device nodes are never
    written: ``tarfile.extractfile`` on a link can hand back the *target's* bytes, and a link that
    points outside the staging folder is the same attack in another form.
    """
    from launcher.launch.natives import _member_target

    destination = destination.resolve()
    written = 0
    total = 0
    skipped = 0
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                if not member.isfile():
                    skipped += 1
                    continue
                target = _member_target(destination, member.name)
                if target is None:
                    # One hostile entry must not block the rest, exactly like the natives extractor:
                    # the guard is about never writing outside the destination.
                    logger.warning("跳过越界的压缩包条目: %r", member.name)
                    skipped += 1
                    continue
                if member.size > MAX_MEMBER_BYTES or total + member.size > MAX_EXTRACT_BYTES:
                    raise ComponentError("tools.fail.verify", "archive member too large")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    skipped += 1
                    continue
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle, CHUNK)
                total += member.size
                written += 1
    except tarfile.TarError as exc:
        raise ComponentError("tools.fail.verify", f"not a usable tar.gz: {exc}") from exc
    if written == 0:
        raise ComponentError("tools.fail.verify", "archive has no usable files")
    return skipped


def _find_member(directory: Path, name: str) -> Path | None:
    """``directory/name`` or a nested file with that basename (release layouts vary)."""
    direct = directory / name
    if direct.is_file():
        return direct
    matches: Iterable[Path] = directory.rglob(name)
    for candidate in matches:
        if candidate.is_file():
            return candidate
    return None


def _install_file(source: Path, target: Path) -> None:
    """Move one verified file into place, replacing an older copy atomically where possible."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".new")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ComponentError("tools.fail.download", f"cannot install {target.name}: {exc}") from exc


def _write_metadata(destination: Path, package: PackageSpec) -> None:
    metadata = {
        "version": package.version,
        "asset": package.asset,
        "sha256": package.sha256,
        "installed_at": time.time(),
        "files": [name for name in (package.executable, package.dll) if name],
    }
    target = destination / METADATA_NAME
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ComponentError("tools.fail.download", f"cannot write metadata: {exc}") from exc


def _remove_tree(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        logger.debug("无法清理临时目录: %s", path, exc_info=True)
