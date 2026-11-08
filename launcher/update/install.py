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

"""Replace the running launcher executable in place, restart it, and clean up afterwards.

The running EXE cannot be overwritten - Windows keeps the image mapped - but it *can* be renamed. The
whole install is therefore three renames in one directory, with no shell involved anywhere:

1. the verified download is copied next to the target as ``<name>.new`` (same directory, same volume,
   so every following rename is a cheap metadata operation);
2. the running executable is renamed to ``<name>.old``;
3. ``<name>.new`` is moved onto the original path with :func:`os.replace`.

Only step 3 can leave the launcher without its executable, and it is the one step with a rollback:
if it fails, ``<name>.old`` is renamed straight back. A failure before step 2 changes nothing, and a
failure when starting the new process restores the old build too. The leftover ``.old`` file is
deleted on the next start (:func:`cleanup_leftovers`); while the old process is still alive its image
file is locked, which is exactly why that cleanup has to wait and why it must never fail a start.

Why not the old batch helper: it ran the Windows command interpreter on a generated script that
waited for the parent PID, moved files and started the new build. That is a second interpreter
executing a file next to the user's launcher, and every quoting or encoding step in it was a way for
an update to half-happen. Renaming from this process needs no helper at all: the swap is complete
before the launcher exits, and the only deferred work is deleting a file nobody needs any more.

This module is deliberately Qt-free and pure Python so the tests can run the real swap against plain
files; only :func:`relaunch` starts a process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: The running executable, renamed aside while the new file takes its place.
OLD_SUFFIX = ".old"
#: The verified download, copied into the install directory before the swap.
NEW_SUFFIX = ".new"
#: Releases before this updater left a ``.bak`` behind when a helper was interrupted; still collected
#: so an old leftover cannot sit next to the executable forever.
LEGACY_BACKUP_SUFFIX = ".bak"
#: Extra free space the install asks for beyond the new file: the copy exists twice for a moment, and
#: NTFS/antivirus bookkeeping is not free either.
SPACE_MARGIN = 16 * 1024 * 1024


class InstallError(RuntimeError):
    """A refused or failed install, carrying an i18n key suffix in :attr:`code`.

    Codes: ``source`` (a source checkout, never self-updates), ``missing`` (the running executable or
    the download is gone), ``writable`` (the install directory cannot be written), ``space`` (not
    enough free disk space), ``stage`` (copying the download beside the target failed), ``swap`` (the
    replace failed and the rollback restored the old build), ``rollback`` (both failed - the worst
    case), ``restart`` (the new build could not be started; the old one was restored).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Feasibility:
    """Whether this machine can install an update, and if not, why not."""

    ok: bool
    reason: str = ""  # InstallError code when not ok
    detail: str = ""
    free: int = 0
    needed: int = 0


def is_packaged() -> bool:
    """Whether this process is a PyInstaller build rather than a source checkout.

    ``sys.frozen`` is set by the PyInstaller bootloader. A source run has no executable of its own to
    replace - ``sys.executable`` is the Python interpreter, and "updating" it would overwrite a shared
    tool - so self-update is refused there instead of guessing.
    """
    return bool(getattr(sys, "frozen", False))


def running_executable() -> Path:
    """The executable this process was started from."""
    return Path(sys.executable)


def _probe_writable(directory: Path) -> bool:
    """Whether a file can be created in ``directory`` (the only reliable pre-flight check)."""
    probe = directory / f".update-probe-{os.getpid()}"
    try:
        probe.write_bytes(b"")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        # A leftover empty probe file is harmless; never report "not writable" just for that.
        pass
    return True


def check_feasibility(target: Path | None = None, new_size: int = 0) -> Feasibility:
    """Pre-flight for an install: packaged, target present, directory writable, disk space enough.

    ``new_size`` is the verified download's size; 0 skips the disk-space estimate (there is no file to
    copy yet). The same-directory requirement is structural: the staged copy always goes next to the
    target (:func:`stage_download`), so the swap can never degrade into a cross-volume copy.
    """
    if not is_packaged():
        return Feasibility(ok=False, reason="source")
    target = Path(target) if target is not None else running_executable()
    if not target.is_file():
        return Feasibility(ok=False, reason="missing", detail=str(target))
    directory = target.parent
    if not directory.is_dir() or not _probe_writable(directory):
        return Feasibility(ok=False, reason="writable", detail=str(directory))
    needed = max(0, int(new_size)) + SPACE_MARGIN if new_size else 0
    free = 0
    if needed:
        try:
            free = int(shutil.disk_usage(directory).free)
        except OSError:
            free = 0
        if free < needed:
            return Feasibility(ok=False, reason="space", detail=str(directory), free=free, needed=needed)
    return Feasibility(ok=True, free=free, needed=needed)


def staged_path(target: Path) -> Path:
    """Where the verified download is copied inside the install directory."""
    return Path(target).with_name(Path(target).name + NEW_SUFFIX)


def stage_download(new_exe: Path, target: Path) -> Path:
    """Copy the verified download next to ``target`` and return that path.

    A copy, not a move: the download is the caller's verified artifact and stays in the staging
    directory until the swap has actually succeeded. A stale ``.new`` from an interrupted attempt is
    overwritten - the target directory belongs to the launcher, and the file name is derived from the
    executable's own name.
    """
    new_exe, target = Path(new_exe), Path(target)
    staged = staged_path(target)
    try:
        staged.unlink(missing_ok=True)
        shutil.copy2(new_exe, staged)
        expected = new_exe.stat().st_size
        if staged.stat().st_size != expected:
            raise OSError(f"short copy: {staged.stat().st_size} != {expected}")
    except OSError as exc:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise InstallError("stage", str(exc)) from exc
    return staged


def swap_in_place(target: Path, staged: Path) -> Path:
    """Rename the running executable aside and put ``staged`` at its path; return the ``.old`` path.

    The second rename is the only one that can leave the install without a launchable file, so a
    failure there restores ``<name>.old`` immediately. A failure of the *first* rename changed
    nothing: the executable is still at its path and only the staged copy has to be discarded later.
    """
    target, staged = Path(target), Path(staged)
    old = target.with_name(target.name + OLD_SUFFIX)
    try:
        os.replace(target, old)
    except OSError as exc:
        raise InstallError("swap", str(exc)) from exc
    try:
        os.replace(staged, target)
    except OSError as exc:
        try:
            os.replace(old, target)
        except OSError as rollback_error:  # pragma: no cover - requires two independent FS failures
            raise InstallError("rollback", str(rollback_error)) from exc
        raise InstallError("swap", str(exc)) from exc
    return old


def restart_arguments(target: Path, argv: list[str] | None = None) -> list[str]:
    """The new process's command line: the executable plus this run's own arguments.

    ``argv`` is a full ``sys.argv``-shaped list; its first element is the program and is replaced by
    ``target``. Everything else is passed through untouched, so a launcher started with
    ``--game-dir`` or a shortcut's arguments comes back with the same arguments.
    """
    original = list(sys.argv if argv is None else argv)
    return [str(Path(target)), *original[1:]]


def relaunch(target: Path, argv: list[str] | None = None) -> subprocess.Popen:
    """Start the just-installed executable and return the live process.

    ``PYINSTALLER_RESET_ENVIRONMENT=1`` is mandatory, not cosmetic: PyInstaller 6.9+ treats a process
    started from the same executable as a *worker* of the process that spawned it and lets it reuse
    the parent's ``_MEI`` extraction directory, which the parent deletes while it exits. The restarted
    launcher then dies with ERROR_MOD_NOT_FOUND (126) and a native "the specified module could not be
    found" box - the 0.3.1/0.4.0 update failure. The reset makes the new build unpack its own files.

    Standard streams point at the null device and the child gets its own process group, so nothing
    about this process's console or handles can take the new launcher down with it.
    """
    target = Path(target)
    environment = dict(os.environ)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        restart_arguments(target, argv),
        cwd=str(target.parent),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def apply_and_restart(new_exe: Path, target: Path | None = None, argv: list[str] | None = None) -> None:
    """Install ``new_exe`` over the running launcher and launch it again.

    Raises :class:`InstallError` instead of leaving a half-installed launcher:

    * a failed feasibility check refuses before anything is touched;
    * a failed copy or replace restores the old build (see :func:`swap_in_place`);
    * a new build that cannot be started is rolled back too - the user keeps a launcher that works.

    The caller is expected to quit the application afterwards. This function does not quit anything
    and does not start the new process until the swap is complete.
    """
    target = Path(target) if target is not None else running_executable()
    new_exe = Path(new_exe)
    if not new_exe.is_file():
        raise InstallError("missing", str(new_exe))
    if not is_packaged():
        raise InstallError("source")
    if not target.is_file():
        raise InstallError("missing", str(target))
    feasibility = check_feasibility(target, new_exe.stat().st_size)
    if not feasibility.ok:
        raise InstallError(feasibility.reason, feasibility.detail)
    if os.path.abspath(new_exe) == os.path.abspath(target):
        raise InstallError("missing", "the download is the running executable")
    staged = stage_download(new_exe, target)
    old = swap_in_place(target, staged)
    try:
        relaunch(target, argv)
    except OSError as exc:
        try:
            os.replace(old, target)
        except OSError as rollback_error:  # pragma: no cover - requires two independent FS failures
            raise InstallError("rollback", str(rollback_error)) from exc
        raise InstallError("restart", str(exc)) from exc


def cleanup_leftovers(target: Path | None = None) -> None:
    """Delete the files a finished or interrupted update left next to the executable; never raises.

    Runs on every start. The ``.old`` file is usually still locked by the process that is exiting when
    the new build starts - the deletion then fails with ``OSError`` and is retried on the next start.
    That failure must not affect startup at all: the leftover is harmless, the launcher is not.
    """
    if not is_packaged():  # a source run has no install directory of its own; never touch sys.executable
        return
    target = Path(target) if target is not None else running_executable()
    for suffix in (OLD_SUFFIX, NEW_SUFFIX, LEGACY_BACKUP_SUFFIX):
        try:
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        except OSError:
            pass
