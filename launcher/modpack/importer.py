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
"""The one import flow: parse, install the core, create the instance, fetch files, copy overrides.

Nothing here is a second installer. The version and loader are installed by
:func:`launcher.install.install_version` and :func:`launcher.mods.installer.install_loader` - the
same two calls the versions page's install step and 修改 use - and the instance folder is created by
:func:`launcher.instances.create_instance`. What this module adds is the part a pack needs: a file
list from three different formats, a download with hash verification and one retry, the
``overrides/`` tree, and a rollback that removes the half-created instance when anything fails or the
user cancels.

Ordering is the failure contract, the same one 修改 established: everything that can fail before a
byte is written happens first (parse, install), the instance folder is created only afterwards, and
the files are written into it last. A cancellation therefore has nothing to undo but one folder,
which :func:`shutil.rmtree` removes - the instance never exists in a state that could be launched.
"""

from __future__ import annotations

import logging
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from launcher import paths
from launcher.i18n import tr_core
from launcher.install import install_version
from launcher.instances import (
    ISOLATION_ON,
    Instance,
    _write_sidecar,
    default_instance_name,
    instance_dir,
)
from launcher.meta.version import version_json_path
from launcher.modpack import archive, curseforge_source, detect, loader_source
from launcher.modpack.errors import CurseforgeKeyMissing, ModpackError
from launcher.modpack.model import (
    PHASE_DONE,
    PHASE_FILES,
    PHASE_INSTALL,
    PHASE_OVERRIDES,
    PHASE_PARSE,
    ModpackProgress,
    ModpackSpec,
)
from launcher.modpack.naming import strip_version_suffix, unique_instance_name
from launcher.mods.installer import install_loader
from launcher.mods.modpacks import _profile_id as loader_profile_id
from launcher.net.downloader import (
    DownloadCancelled,
    Downloader,
    DownloadProgress,
    DownloadResult,
    DownloadTask,
    is_user_cancelled,
)

logger = logging.getLogger(__name__)

#: ``report(ModpackProgress)`` - called from the worker thread, so a GUI passes its ProgressBridge.
Report = Callable[[ModpackProgress], None]
#: ``should_stop()`` - polled between steps; True makes the import raise ``DownloadCancelled``.
StopCheck = Callable[[], bool]


@dataclass(frozen=True)
class ImportResult:
    """What a finished import left behind (everything the page announces afterwards)."""

    instance: Instance
    spec: ModpackSpec
    profile_id: str
    name: str
    files: int = 0
    skipped: int = 0
    overrides: int = 0
    installed: bool = False


def _emit(report: Report | None, progress: ModpackProgress) -> None:
    if report is not None:
        report(progress)


def _stop_requested(should_stop: StopCheck | None) -> bool:
    return bool((should_stop is not None and should_stop()) or is_user_cancelled())


def _check_stop(should_stop: StopCheck | None) -> None:
    """Raise the launcher's cooperative cancellation exception when the user stopped the import."""
    if _stop_requested(should_stop):
        raise DownloadCancelled("cancelled")


def _installation_reporter(report: Report | None) -> Callable[[DownloadProgress], None]:
    """Adapt the downloader's progress (the install chain reports that) to the import's phases."""

    def report_install(progress: DownloadProgress) -> None:
        _emit(
            report,
            ModpackProgress(
                phase=PHASE_INSTALL,
                done_files=int(getattr(progress, "done_files", 0) or 0),
                total_files=int(getattr(progress, "total_files", 0) or 0),
                done_bytes=int(getattr(progress, "done_bytes", 0) or 0),
                total_bytes=int(getattr(progress, "total_bytes", 0) or 0),
                current=str(getattr(progress, "current", "") or ""),
            ),
        )

    return report_install


def _ensure_profile(
    spec: ModpackSpec,
    *,
    game_dir: Path,
    cache_dir: Path,
    runtime_dir: Path | None,
    concurrency: int,
    report: Report | None,
    should_stop: StopCheck | None,
) -> tuple[str, bool]:
    """Install the pack's Minecraft version and loader; return (profile id, whether anything ran).

    A profile that is already installed is skipped: reinstalling it would only re-verify the same
    files, and a pack is often imported twice into the same game folder.
    """
    if not spec.minecraft:
        raise ModpackError(tr_core("modpack.mc_missing"))
    installed = False
    if version_json_path(instance_dir(game_dir, spec.minecraft)) is None:
        _emit(report, ModpackProgress(phase=PHASE_INSTALL, current=spec.minecraft))
        try:
            result = install_version(
                spec.minecraft,
                game_dir=game_dir,
                cache_dir=cache_dir,
                concurrency=concurrency,
                progress=_installation_reporter(report),
                auto_install_java=True,
                runtime_dir=runtime_dir,
            )
        except DownloadCancelled:
            raise  # the user stopped the import: not an install failure
        except Exception as exc:
            raise ModpackError(tr_core("modpack.install_failed", str(exc))) from exc
        failed = list(getattr(result, "failed", ()) or [])
        if failed:
            raise ModpackError(
                tr_core(
                    "modpack.install_failed",
                    tr_core("mods.pack_files_failed", len(failed)),
                )
            )
        installed = True
    _check_stop(should_stop)
    if not spec.loader:
        return spec.minecraft, installed

    loader_version = loader_source.loader_installer(
        spec.loader, spec.loader_version, spec.minecraft
    )
    profile_id = loader_profile_id(spec.loader, loader_version.version, spec.minecraft)
    if version_json_path(instance_dir(game_dir, profile_id)) is None:
        _emit(report, ModpackProgress(phase=PHASE_INSTALL, current=loader_version.version))
        try:
            profile_id = install_loader(
                loader_version,
                game_dir,
                cache_dir=cache_dir,
                progress=_installation_reporter(report),
            )
        except DownloadCancelled:
            raise
        except ModpackError:
            raise
        except Exception as exc:
            raise ModpackError(tr_core("modpack.install_failed", str(exc))) from exc
        installed = True
    return profile_id, installed


class _FilesProgress:
    """Cumulative progress for the file phase, across a first batch and its forced retry."""

    def __init__(self, report: Report | None, total_files: int, total_bytes: int) -> None:
        self._report = report
        self._total_files = max(0, int(total_files))
        self._total_bytes = max(0, int(total_bytes))
        self._done_files = 0
        self._done_bytes = 0

    def __call__(self, progress: DownloadProgress) -> None:
        _emit(
            self._report,
            ModpackProgress(
                phase=PHASE_FILES,
                done_files=min(self._total_files, self._done_files + progress.done_files),
                total_files=self._total_files,
                done_bytes=self._done_bytes + progress.done_bytes,
                total_bytes=max(self._total_bytes, self._done_bytes + progress.total_bytes),
                current=str(progress.current or ""),
            ),
        )

    def advance(self, result: DownloadResult) -> None:
        """Move the cumulative counters past a finished batch."""
        self._done_files += int(result.downloaded) + int(result.skipped)
        self._done_bytes += int(result.bytes)


def _download_url_files(
    spec: ModpackSpec,
    target: Path,
    *,
    concurrency: int,
    report: Report | None,
    should_stop: StopCheck | None,
) -> tuple[int, int]:
    """Download Modrinth files (direct URLs + sha1); return (downloaded, skipped).

    A verification failure is retried once, forced: the first batch may have raced a mirror, and one
    re-download is cheap next to failing an import whose instance is already created. Whatever still
    fails is named, because "3 files failed" is not actionable.
    """
    tasks: list[DownloadTask] = []
    names: dict[str, str] = {}
    for item in spec.files:
        if not item.path:
            continue
        dest = archive.member_target(target, item.path)
        tasks.append(
            DownloadTask(
                url=item.url,
                dest=dest,
                sha1=item.sha1 or None,
                size=item.size or None,
            )
        )
        names[item.url] = item.path
    if not tasks:
        return 0, 0
    progress = _FilesProgress(report, len(tasks), sum(task.size or 0 for task in tasks))
    result = Downloader(concurrency=concurrency).download(tasks, progress=progress)
    progress.advance(result)
    downloaded = int(result.downloaded)
    skipped = int(result.skipped)
    failed = list(result.failed)
    if failed:
        failed_urls = {url for url, _message in failed}
        retry = [task for task in tasks if task.url in failed_urls]
        _check_stop(should_stop)
        again = Downloader(concurrency=concurrency, force=True).download(retry, progress=progress)
        progress.advance(again)
        downloaded += int(again.downloaded)
        failed = list(again.failed)
    if failed:
        failed_names = sorted({names.get(url, Path(url).name) for url, _message in failed})
        raise ModpackError(tr_core("modpack.file_failed", ", ".join(failed_names)))
    return downloaded, skipped


def _curseforge_file_ok(path: Path, declared_size: int) -> bool:
    """CurseForge publishes no hash, so the fallback check is the file name plus its size."""
    try:
        if not path.is_file():
            return False
        return declared_size <= 0 or path.stat().st_size == declared_size
    except OSError:
        return False


def _download_curseforge_files(
    spec: ModpackSpec,
    target: Path,
    *,
    report: Report | None,
    should_stop: StopCheck | None,
) -> tuple[int, int]:
    """Resolve and download CurseForge files one at a time; return (downloaded, skipped)."""
    total = len(spec.files)
    if not total:
        return 0, 0
    failed: list[str] = []
    downloaded = 0
    done_bytes = 0
    for index, item in enumerate(spec.files, start=1):
        _check_stop(should_stop)
        resolved = curseforge_source.resolve_file(item)  # raises CurseforgeKeyMissing when gated
        relative = curseforge_source.destination(resolved)
        dest = archive.member_target(target, relative)
        _emit(
            report,
            ModpackProgress(
                phase=PHASE_FILES,
                done_files=index - 1,
                total_files=total,
                done_bytes=done_bytes,
                current=relative,
            ),
        )
        ok = False
        for attempt in (1, 2):
            try:
                curseforge_source.download(resolved, dest.parent)
            except CurseforgeKeyMissing:
                raise
            except ModpackError as exc:
                if attempt == 2:
                    logger.warning("CurseForge file failed twice: %s (%s)", relative, exc)
                continue
            if _curseforge_file_ok(dest, resolved.size):
                ok = True
                break
            logger.warning(
                "CurseForge file %s did not match its declared size (%s); re-downloading",
                relative,
                resolved.size,
            )
            dest.unlink(missing_ok=True)
        if ok:
            downloaded += 1
            try:
                done_bytes += dest.stat().st_size
            except OSError:
                pass
        else:
            failed.append(relative)
        _emit(
            report,
            ModpackProgress(
                phase=PHASE_FILES,
                done_files=index,
                total_files=total,
                done_bytes=done_bytes,
                current=relative,
            ),
        )
    if failed:
        raise ModpackError(tr_core("modpack.file_failed", ", ".join(sorted(failed))))
    return downloaded, 0


def _install_files(
    spec: ModpackSpec,
    target: Path,
    *,
    concurrency: int,
    report: Report | None,
    should_stop: StopCheck | None,
) -> tuple[int, int]:
    """Fetch every file the pack names; return (downloaded, skipped)."""
    if not spec.files:
        return 0, 0
    if any(item.is_curseforge for item in spec.files):
        return _download_curseforge_files(spec, target, report=report, should_stop=should_stop)
    return _download_url_files(
        spec, target, concurrency=concurrency, report=report, should_stop=should_stop
    )


def _install_overrides(
    spec: ModpackSpec,
    target: Path,
    *,
    report: Report | None,
    should_stop: StopCheck | None,
) -> int:
    """Copy the pack's own files (``overrides/`` or ``.minecraft/``) over the instance."""
    if not spec.overrides:
        return 0
    _check_stop(should_stop)
    _emit(report, ModpackProgress(phase=PHASE_OVERRIDES))
    if spec.source_is_dir:
        count = 0
        for prefix in spec.overrides:
            base = Path(spec.source) / prefix
            if base.is_dir():
                count += archive.copy_tree(base, target)
        return count
    with zipfile.ZipFile(spec.source) as zf:
        return archive.extract_prefixes(zf, spec.overrides, target)


def _discard_created_instance(target: Path) -> None:
    """Remove the half-created instance folder (a cancelled import must leave no instance)."""
    logger.info("modpack import ended early; removing the half-created instance: %s", target)
    shutil.rmtree(target, ignore_errors=True)


def _create_pack_instance(
    name: str, profile_id: str, display_name: str, game_dir: Path
) -> Instance:
    """Create the instance as a thin pointer at the installed core.

    A modpack instance is a folder of the pack's own files *plus* the profile it runs. Copying that
    profile (what 新建/复制实例 does) would duplicate a client jar per pack, and a copy's own id no
    longer says ``fabric-loader-...``, so the card would describe a Fabric pack as 原版.
    ``base_version`` is the launcher's existing pointer field for exactly this situation - 修改
    re-points an instance with it - and every launch, card and 修改 path already resolves through
    ``Instance.version_id``.

    The folder is marked isolated on purpose: the pack's mods, config and overrides were written into
    it, so the game has to run with this folder as its working directory - otherwise a pack without
    mods would read the shared game directory instead of its own files.

    The sidecar is written by ``launcher.instances._write_sidecar``: reusing that writer keeps the
    metadata format (and its atomic write) in one place instead of restating the JSON layout here.
    """
    folder = instance_dir(game_dir, name)
    folder.mkdir(parents=True, exist_ok=False)
    instance = Instance(
        id=name,
        display_name=display_name,
        created_at=time.time(),
        isolated=ISOLATION_ON,
        base_version=profile_id,
    )
    _write_sidecar(folder, instance)
    return instance


def import_modpack(
    path: Path,
    *,
    game_dir: Path,
    cache_dir: Path | None = None,
    runtime_dir: Path | None = None,
    concurrency: int = 4,
    report: Report | None = None,
    should_stop: StopCheck | None = None,
    instance_name: str | None = None,
) -> ImportResult:
    """Import one modpack (``.mrpack``, CurseForge zip, MultiMC/Prism dir or zip) into ``game_dir``.

    Raises :class:`ModpackError` with a user-facing message when the pack cannot be imported, and
    ``DownloadCancelled`` when the user cancelled - in both cases no instance folder is left behind
    (the install of the Minecraft version/loader stays: it is a complete, usable profile and is what
    makes a second attempt fast).
    """
    source = Path(path)
    cache_dir = cache_dir or (paths.launcher_dir() / "cache")
    spec = detect.parse_modpack(source)
    _emit(report, ModpackProgress(phase=PHASE_PARSE, current=spec.name))
    _check_stop(should_stop)

    profile_id, installed = _ensure_profile(
        spec,
        game_dir=Path(game_dir),
        cache_dir=cache_dir,
        runtime_dir=runtime_dir,
        concurrency=concurrency,
        report=report,
        should_stop=should_stop,
    )
    _check_stop(should_stop)

    desired = (instance_name or "").strip() or strip_version_suffix(spec.name)
    name = unique_instance_name(
        desired, Path(game_dir), fallback=default_instance_name(profile_id)
    )
    target = instance_dir(Path(game_dir), name)
    existed = target.exists()
    try:
        instance = _create_pack_instance(
            name, profile_id, strip_version_suffix(spec.name) or name, Path(game_dir)
        )
    except Exception:
        # A folder this call created must not survive as an "instance" with no metadata; a folder
        # that was already there belongs to somebody else and is never touched.
        if not existed:
            _discard_created_instance(target)
        raise
    try:
        files, skipped = _install_files(
            spec,
            target,
            concurrency=concurrency,
            report=report,
            should_stop=should_stop,
        )
        _check_stop(should_stop)
        overrides = _install_overrides(spec, target, report=report, should_stop=should_stop)
        _check_stop(should_stop)
    except BaseException:
        _discard_created_instance(target)
        raise

    _emit(report, ModpackProgress(phase=PHASE_DONE, done_files=files, total_files=files))
    return ImportResult(
        instance=instance,
        spec=spec,
        profile_id=profile_id,
        name=name,
        files=files,
        skipped=skipped,
        overrides=overrides,
        installed=installed,
    )
