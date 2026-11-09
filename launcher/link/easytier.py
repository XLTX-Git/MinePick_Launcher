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

"""EasyTier's CLI as the UDP + TCP NAT probe behind the 网络自检 row.

``easytier-cli -o json stun`` performs both STUN tests itself and needs no core process, no
administrator rights and no configuration file (verified against v2.6.4 in 2026-11: one run took
3.1-5.4 s on the development network). The launcher downloads the release archive on first use,
verifies its SHA-256, extracts only ``easytier-cli.exe`` into ``components/easytier`` and invokes it
with a 15 s timeout. The other 20 MB of the archive (core, web, drivers) is never installed.

The archive and the extracted CLI both have size and hash constants for exactly v2.6.4. A mirror
that serves a truncated file, an HTML error page or a different build is refused before the ZIP is
opened; a valid archive whose CLI bytes differ is refused before the executable is installed.

Every failure is a :class:`~launcher.link.selfcheck.NatProbe` with an error category, never an
exception that could escape the self-check worker. The caller falls back to the launcher's own UDP
STUN client, which can still answer the half of the question that does not need EasyTier.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from launcher import paths
from launcher.link import mirrors as mirror_module
from launcher.link.components import ComponentError, sha256_file
from launcher.link.selfcheck import NatProbe, NatProbeError, NatType

logger = logging.getLogger(__name__)

EASYTIER_DIRNAME = "easytier"
METADATA_NAME = "installed.json"
DOWNLOAD_DIRNAME = ".download"
STAGING_PREFIX = ".staging-"
CHUNK = 64 * 1024

#: The CLI's own bound. PCL CE waits indefinitely; 15 s leaves >2.5x headroom over the measured
#: 3.1-5.4 s runs while still guaranteeing that a hung probe reaches the fallback.
CLI_TIMEOUT = 15.0
#: The archive transfer is bounded separately from the CLI's own STUN timeout.
DOWNLOAD_TIMEOUT = 30.0
#: Called with ``(done_bytes, total_bytes)`` while the archive downloads.
ProgressCallback = Callable[[int, int], None]

_ENV_MIRROR = "MINEPICK_EASYTIER_MIRROR"
_INSTALL_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class EasytierPackage:
    """One verified EasyTier release: the archive and the single member the launcher installs."""

    version: str
    asset: str
    size: int
    sha256: str
    executable: str
    executable_size: int
    executable_sha256: str


#: EasyTier v2.6.4, Windows x86_64. Both hashes were read from the official release asset and API
#: digest in 2026-11; changing the version requires remeasuring all four constants.
EASYTIER_PACKAGE = EasytierPackage(
    version="2.6.4",
    asset="easytier-windows-x86_64-v2.6.4.zip",
    size=32_657_720,
    sha256="27af91e270e554709b048bd32327fefd2dfce5062ae1e8701af7550c6f525f84",
    executable="easytier-cli.exe",
    executable_size=9_973_248,
    executable_sha256="d8783e851e944b44a9b71b39fd02f227ec0a2a82b3165c55ead5dd32dcde53a1",
)

#: The two mirrors race on a HEAD; ghfast proxies the official asset and is usually the first answer
#: in this project's network. The official URL is the measured fallback.
EASYTIER_MIRRORS: tuple[mirror_module.Mirror, ...] = (
    mirror_module.Mirror(
        "ghfast",
        "ghfast.top",
        "https://ghfast.top/https://github.com/EasyTier/EasyTier/releases/download",
    ),
    mirror_module.Mirror(
        "github",
        "GitHub",
        "https://github.com/EasyTier/EasyTier/releases/download",
    ),
)


def package_for_platform() -> EasytierPackage | None:
    """The verified package for this machine; other platforms have no measured asset."""
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return EASYTIER_PACKAGE
    return None


def component_dir() -> Path:
    """``<launcher dir>/components/easytier`` (resolved at call time)."""
    return paths.launcher_dir() / "components" / EASYTIER_DIRNAME


def cli_path(package: EasytierPackage | None = None) -> Path | None:
    """Where the installed CLI lives, or ``None`` when this platform has no package."""
    spec = package if package is not None else package_for_platform()
    return component_dir() / spec.executable if spec is not None else None


def cache_path() -> Path:
    """The mirror race cache is separate from Terracotta's, whose mirror keys are different."""
    return paths.launcher_dir() / "cache" / "easytier-mirror-pick.json"


def installed_version(package: EasytierPackage | None = None) -> str:
    """The version recorded by the last successful install, or the package version."""
    spec = package if package is not None else package_for_platform()
    if spec is None:
        return ""
    try:
        raw = json.loads((component_dir() / METADATA_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return spec.version
    version = str(raw.get("version") or "") if isinstance(raw, dict) else ""
    return version or spec.version


def cli_ready(package: EasytierPackage | None = None) -> bool:
    """Whether the installed CLI still has the exact size and SHA-256 of v2.6.4."""
    spec = package if package is not None else package_for_platform()
    target = cli_path(spec)
    if spec is None or target is None:
        return False
    try:
        if target.stat().st_size != spec.executable_size:
            return False
        return sha256_file(target) == spec.executable_sha256
    except OSError:
        return False


def verify_archive(path: Path, package: EasytierPackage | None = None) -> bool:
    """The archive's byte count and SHA-256 both have to match the release constants."""
    spec = package if package is not None else package_for_platform()
    if spec is None:
        return False
    try:
        return path.stat().st_size == spec.size and sha256_file(path) == spec.sha256
    except OSError:
        return False


def install(
    progress: ProgressCallback | None = None,
    *,
    package: EasytierPackage | None = None,
    mirrors: Sequence[mirror_module.Mirror] = EASYTIER_MIRRORS,
    preferred: str = "",
    cache_path_override: Path | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> Path:
    """Download, verify and install only the CLI; raise :class:`ComponentError` on failure.

    The archive is staged under ``components/easytier/.download`` and unpacked in a temporary
    directory next to the target. The executable is moved into place only after both hashes match;
    metadata is written last, so a partial install never looks ready.
    """
    spec = package if package is not None else package_for_platform()
    if spec is None:
        raise ComponentError("tools.fail.platform", "no verified EasyTier asset for this platform")
    destination = component_dir()
    target = destination / spec.executable
    with _INSTALL_LOCK:
        if cli_ready(spec):
            _report(progress, 1, 1)
            return target

        download_dir = destination / DOWNLOAD_DIRNAME
        archive = download_dir / spec.asset
        part = download_dir / (spec.asset + ".part")
        staging = destination / f"{STAGING_PREFIX}{os.getpid()}-{threading.get_ident()}"
        preferred_key = (preferred or os.environ.get(_ENV_MIRROR, "")).strip().lower()
        mirror = mirror_module.choose_mirror(
            version=spec.version,
            asset=spec.asset,
            mirrors=mirrors,
            preferred=preferred_key,
            path=cache_path_override if cache_path_override is not None else cache_path(),
        )
        ordered = [mirror] + [item for item in mirrors if item.key != mirror.key]
        try:
            destination.mkdir(parents=True, exist_ok=True)
            download_dir.mkdir(parents=True, exist_ok=True)
            failure: ComponentError | None = None
            for candidate in ordered:
                try:
                    _report(progress, 0, spec.size)
                    _download(
                        candidate.url(spec.version, spec.asset),
                        part,
                        spec.size,
                        progress,
                        timeout,
                    )
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
            _extract_cli(part, staging / spec.executable, spec)
            _install_file(staging / spec.executable, target)
            _write_metadata(destination, spec)
            _report(progress, 1, 1)
            return target
        finally:
            _remove_tree(staging)
            part.unlink(missing_ok=True)
            archive.unlink(missing_ok=True)


def run_cli_stun(
    cli: Path,
    *,
    timeout: float = CLI_TIMEOUT,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> NatProbe:
    """Run ``easytier-cli -o json stun`` once and classify its answer.

    A timeout, a missing executable, a non-zero exit and malformed JSON all become a NatProbe with
    a category; this function does not raise for ordinary failures.
    """
    args = [str(cli), "-o", "json", "stun"]
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
        "timeout": max(0.1, float(timeout)),
        "check": False,
        "cwd": str(cli.parent),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = (runner or subprocess.run)(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return NatProbe(error=NatProbeError.TIMEOUT, detail=_detail(exc))
    except (FileNotFoundError, PermissionError) as exc:
        return NatProbe(error=NatProbeError.MISSING, detail=_detail(exc))
    except OSError as exc:
        return NatProbe(error=NatProbeError.OS, detail=_detail(exc))
    if completed.returncode != 0:
        stderr = completed.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return NatProbe(
            error=NatProbeError.EXIT,
            detail=f"exit {completed.returncode}: {str(stderr).strip()[:200]}",
        )
    return parse_stun_output(completed.stdout or "")


def parse_stun_output(raw: str | bytes) -> NatProbe:
    """Parse the CLI's JSON document; both type codes must be integers in 0-8."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = str(raw).lstrip("\ufeff")
    if not text.strip():
        return NatProbe(error=NatProbeError.BAD_OUTPUT, detail="empty stdout")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return NatProbe(error=NatProbeError.BAD_OUTPUT, detail=_detail(exc))
    if not isinstance(payload, dict):
        return NatProbe(error=NatProbeError.BAD_OUTPUT, detail="json root is not an object")
    udp = NatType.from_code(payload.get("udp_nat_type"))
    tcp = NatType.from_code(payload.get("tcp_nat_type"))
    if udp is None or tcp is None:
        return NatProbe(
            error=NatProbeError.BAD_OUTPUT,
            detail=(
                f"udp_nat_type={payload.get('udp_nat_type')!r}, "
                f"tcp_nat_type={payload.get('tcp_nat_type')!r}"
            ),
        )
    return NatProbe(udp=udp, tcp=tcp)


def probe_nat_types(
    *,
    cli: Path | None = None,
    timeout: float = CLI_TIMEOUT,
    install_if_missing: bool = True,
    installer: Callable[[], Path] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> NatProbe:
    """Return both NAT types, installing the CLI when needed, or an error category.

    ``cli`` is for tests and for a caller that already has a verified executable. The default path
    verifies the installed file's hash, downloads the release on first use, then runs the CLI.
    """
    spec = package_for_platform()
    if spec is None:
        return NatProbe(error=NatProbeError.MISSING, detail="unsupported platform")
    target = Path(cli) if cli is not None else cli_path(spec)
    if target is None:
        return NatProbe(error=NatProbeError.MISSING, detail="no CLI path")
    if cli is None:
        ready = cli_ready(spec)
        if not ready and not install_if_missing:
            return NatProbe(error=NatProbeError.MISSING, detail="not installed")
        if not ready:
            try:
                installed = (installer or install)()
            except ComponentError as exc:
                logger.info("EasyTier 检测组件安装失败: %s", exc)
                return NatProbe(error=NatProbeError.INSTALL, detail=_detail(exc))
            except Exception as exc:
                logger.info("EasyTier 检测组件安装异常", exc_info=True)
                return NatProbe(error=NatProbeError.INSTALL, detail=_detail(exc))
            target = Path(installed) if installed is not None else cli_path(spec)
            if target is None or not target.is_file() or not cli_ready(spec):
                return NatProbe(error=NatProbeError.INSTALL, detail="installed CLI failed verification")
    if not target.is_file():
        return NatProbe(error=NatProbeError.MISSING, detail=str(target))
    return run_cli_stun(target, timeout=timeout, runner=runner)


def _report(progress: ProgressCallback | None, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(max(0, int(done)), max(0, int(total)))
    except Exception:
        logger.debug("EasyTier 下载进度回调失败", exc_info=True)


def _download(
    url: str,
    target: Path,
    expected_size: int,
    progress: ProgressCallback | None,
    timeout: float,
) -> None:
    """Stream one mirror into ``target``; HTTP and filesystem errors reach the caller."""
    with (
        httpx.Client(follow_redirects=True, timeout=httpx.Timeout(timeout)) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        header = response.headers.get("Content-Length")
        total = int(header) if (header or "").isdigit() else expected_size
        done = 0
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(CHUNK):
                handle.write(chunk)
                done += len(chunk)
                _report(progress, done, total or done)


def _prepare_staging(staging: Path) -> None:
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)


def _extract_cli(archive: Path, target: Path, package: EasytierPackage) -> None:
    """Write only the verified CLI member into the staging directory.

    The archive is not unpacked with ``extractall``: a ZIP member name is attacker-controlled, and
    the launcher only needs one known basename. The uncompressed size and SHA-256 are checked before
    the file can be moved into the component directory.
    """
    try:
        with zipfile.ZipFile(archive) as bundle:
            matches = [
                info
                for info in bundle.infolist()
                if not info.is_dir() and PurePosixPath(info.filename).name == package.executable
            ]
            if len(matches) != 1:
                raise ComponentError(
                    "tools.fail.verify",
                    f"{package.asset}: expected one {package.executable}, found {len(matches)}",
                )
            info = matches[0]
            if info.file_size != package.executable_size:
                raise ComponentError(
                    "tools.fail.verify",
                    f"{package.executable}: size {info.file_size} != {package.executable_size}",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle, CHUNK)
    except zipfile.BadZipFile as exc:
        raise ComponentError("tools.fail.verify", f"not a usable zip: {exc}") from exc
    try:
        actual_size = target.stat().st_size
        actual_hash = sha256_file(target)
    except OSError as exc:
        raise ComponentError("tools.fail.verify", f"cannot verify {package.executable}: {exc}") from exc
    if actual_size != package.executable_size or actual_hash != package.executable_sha256:
        raise ComponentError(
            "tools.fail.verify",
            f"{package.executable}: size/hash mismatch after extraction",
        )


def _install_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".new")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ComponentError("tools.fail.download", f"cannot install {target.name}: {exc}") from exc


def _write_metadata(destination: Path, package: EasytierPackage) -> None:
    metadata = {
        "version": package.version,
        "asset": package.asset,
        "sha256": package.sha256,
        "executable_sha256": package.executable_sha256,
        "license": "LGPL-3.0",
        "source": "https://github.com/EasyTier/EasyTier",
        "installed_at": time.time(),
        "files": [package.executable],
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
        logger.debug("无法清理 EasyTier 临时目录: %s", path, exc_info=True)


def _detail(exc: object) -> str:
    return str(exc or "").strip()[:200]
