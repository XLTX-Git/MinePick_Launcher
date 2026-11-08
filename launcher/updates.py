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

"""Update check and download: query the GitHub release, compare versions, stream and verify the EXE.

GitHub Releases is the only source. Everything here is synchronous and Qt-free, so the GUI can run it
on a worker thread and the tests can call it directly. The local half - replacing the running
executable and restarting it - lives in :mod:`launcher.update.install`; this module never touches the
install directory.

Failure policy (see the standard process document, [P19]): whenever the download or the swap cannot be
verified, nothing is installed and the caller is expected to offer the release page instead. A failed
update must never leave the user without a working launcher.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = "XLTX-Git/MinePick_Launcher"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_LIST_API = f"https://api.github.com/repos/{REPO}/releases"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

#: Release channels. "stable" only accepts a published, non-prerelease release; "beta" may also use a
#: prerelease. Both still come from this project's own GitHub repository - the only allowed source.
CHANNELS = ("stable", "beta")
DEFAULT_CHANNEL = "stable"

# The asset name changed with the 0.2.0 rename; releases before that still ship the old name.
EXE_ASSETS = ("MinePick_Launcher.exe", "MinePick_UI_Trial.exe")
GITHUB_ACCEPT = "application/vnd.github+json"

# Integrity policy, strongest check first.
#
# 1. **Digest (primary).** The release asset carries a ``digest`` field (``sha256:...``) that GitHub
#    computes from the bytes it stores and serves over HTTPS with the release. A downloaded file whose
#    local SHA-256 differs from it is not the published asset and is refused.
# 2. **Authenticode (fallback).** Releases published before GitHub added that field carry no digest;
#    those keep the pre-existing signature policy so they stay updatable.
#
# The signature policy itself: the launcher ships a **self-signed** certificate (see
# scripts/sign_exe.ps1), so on a normal machine the chain is untrusted and Windows reports
# ``UnknownError`` / ``NotTrusted`` - those are accepted. ``EXPECTED_SIGNER`` is only auxiliary
# information and proves nothing about who built the file: anybody can mint a self-signed certificate
# with the same ``CN`` and Windows reports the same statuses for it. It is kept for the fallback path
# and for diagnostics, never as a reason to trust a download on its own.
# (Spellings follow PowerShell's SignatureStatus values.)
EXPECTED_SIGNER = "CN=WDNDXLTX"
ACCEPTED_SIGNATURE_STATUSES = {"valid", "unknownerror", "nottrusted"}


class UpdateError(RuntimeError):
    """Update failure carrying an i18n key suffix: network / http / parse / no_asset / no_release / size / signature.

    ``signature`` is the existing "the download failed verification" bucket and also covers a
    SHA-256 digest mismatch: the GUI/i18n layer keys its message off it, and a download the release
    did not publish is exactly what that message describes. ``no_release`` means the requested
    channel had no usable published release (stable's latest was a prerelease, or the beta list was
    empty).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class ReleaseInfo:
    """The bits of a GitHub release this launcher cares about."""

    version: str  # tag without the leading "v", e.g. "0.2.0"
    tag: str
    page_url: str
    notes: str
    exe_url: str
    exe_name: str
    exe_size: int
    # "sha256:<hex>" as served by the release API; "" when the API has no digest for the asset
    # (older releases, or an API that predates the field). An empty value selects the signature
    # fallback, and it stays last and defaulted because callers construct this dataclass without it.
    exe_digest: str = ""
    #: True for a GitHub prerelease. Only the "beta" channel accepts one; the default channel never
    #: offers it as an update.
    prerelease: bool = False
    #: The API's ``published_at`` (UTC ISO-8601), "" when the payload has none.
    published_at: str = ""

    @property
    def published_date(self) -> str:
        """The date part of :attr:`published_at`, for display; "" when unknown."""
        return (self.published_at or "")[:10]


@dataclass(frozen=True)
class UpdateCheck:
    """Result of a check: status is "up_to_date", "update_available" or "failed"."""

    status: str
    current: str
    latest: str = ""
    release: ReleaseInfo | None = None
    error: str = ""
    #: Local wall-clock time of the check ("YYYY-MM-DD HH:MM"), for the "checked at" line. Filled by
    #: :func:`check_for_update`; callers that build a check themselves may leave it empty.
    checked_at: str = ""


def parse_version(text: str):
    """A comparable version for a tag like ``v0.2.0``; None when it cannot be parsed."""
    from packaging.version import InvalidVersion, Version

    cleaned = (text or "").strip().lstrip("vV")
    if not cleaned:
        return None
    try:
        return Version(cleaned)
    except InvalidVersion:
        return None


def is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is strictly newer than ``current`` (unparsable versions never are)."""
    left, right = parse_version(latest), parse_version(current)
    if left is None or right is None:
        return False
    return left > right


def select_exe_asset(assets: list[dict]) -> dict | None:
    """Pick the asset holding the launcher EXE: current name, then legacy name, then a lone .exe."""
    by_name = {str(asset.get("name") or ""): asset for asset in assets}
    for candidate in EXE_ASSETS:
        if candidate in by_name:
            return by_name[candidate]
    exes = [a for a in assets if str(a.get("name") or "").lower().endswith(".exe")]
    return exes[0] if len(exes) == 1 else None


def _release_from_payload(payload: dict) -> ReleaseInfo:
    """Build :class:`ReleaseInfo` from one GitHub release object; raises on a malformed one."""
    tag = str(payload.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    if not version:
        raise UpdateError("parse", "release without tag_name")
    assets = [a for a in (payload.get("assets") or []) if isinstance(a, dict)]
    asset = select_exe_asset(assets)
    if asset is None:
        raise UpdateError("no_asset", "the release carries no launcher executable")
    return ReleaseInfo(
        version=version,
        tag=tag,
        page_url=str(payload.get("html_url") or RELEASES_PAGE),
        notes=str(payload.get("body") or ""),
        exe_url=str(asset.get("browser_download_url") or ""),
        exe_name=str(asset.get("name") or EXE_ASSETS[0]),
        exe_size=int(asset.get("size") or 0),
        exe_digest=str(asset.get("digest") or "").strip(),
        prerelease=bool(payload.get("prerelease")),
        published_at=str(payload.get("published_at") or "").strip(),
    )


def fetch_latest_release(client=None, channel: str = DEFAULT_CHANNEL) -> ReleaseInfo:
    """Latest release for ``channel``; raises :class:`UpdateError` with an i18n key suffix on failure.

    ``stable`` asks the API for ``latest``, which GitHub defines as the most recent *published,
    non-prerelease* release, and refuses a prerelease defensively. ``beta`` reads the release list
    (newest first) and accepts a prerelease; a beta channel that could not see a prerelease would be
    the same thing as stable. Both requests go to this project's repository only.
    """
    import httpx

    from launcher.meta.manifest import _new_client

    if channel not in CHANNELS:
        raise UpdateError("parse", f"unknown channel {channel!r}")
    owns_client = client is None
    client = client or _new_client()
    try:
        url = (
            f"{RELEASES_LIST_API}?per_page=20"
            if channel == "beta"
            else RELEASES_API
        )
        response = client.get(url, headers={"Accept": GITHUB_ACCEPT})
    except httpx.HTTPError as exc:
        raise UpdateError("network", str(exc)) from exc
    finally:
        if owns_client:
            client.close()
    if response.status_code != 200:
        raise UpdateError("http", f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpdateError("parse", str(exc)) from exc

    if channel == "beta":
        if not isinstance(payload, list):
            raise UpdateError("parse", "unexpected payload")
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                return _release_from_payload(item)
            except UpdateError as exc:
                if exc.code != "no_asset":  # a release without an asset is not this channel's answer
                    raise
        raise UpdateError("no_release", "the release list carries no launcher release")
    if not isinstance(payload, dict):
        raise UpdateError("parse", "unexpected payload")
    if payload.get("prerelease"):
        # /latest normally excludes prereleases already; refusing one here keeps that a property of
        # this module rather than a promise of the API.
        raise UpdateError("no_release", "the latest release is a prerelease")
    return _release_from_payload(payload)


def check_for_update(
    current: str | None = None, client=None, channel: str = DEFAULT_CHANNEL
) -> UpdateCheck:
    """Compare the running version with the latest release. Never raises."""
    from launcher import __version__

    running = current or __version__
    checked_at = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")
    try:
        release = fetch_latest_release(client, channel=channel)
    except UpdateError as exc:
        return UpdateCheck(status="failed", current=running, error=exc.code, checked_at=checked_at)
    except Exception:  # noqa: BLE001 - a check must never take the GUI down
        return UpdateCheck(status="failed", current=running, error="network", checked_at=checked_at)
    status = "update_available" if is_newer(release.version, running) else "up_to_date"
    return UpdateCheck(
        status=status,
        current=running,
        latest=release.version,
        release=release,
        checked_at=checked_at,
    )


def is_pe_file(path: Path) -> bool:
    """A Windows executable starts with the DOS magic ``MZ``."""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return False


def file_sha256(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks so a large EXE never has to fit in memory."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_is_valid(path: Path, expected: str) -> bool:
    """Whether ``path`` matches a GitHub asset digest such as ``sha256:<hex>``.

    A value that is present but cannot be checked - a future algorithm, a malformed string - counts
    as a mismatch. Only an empty digest means "no digest on this release" and selects the signature
    fallback; silently downgrading a declared-but-unverifiable digest would defeat the point.
    """
    algorithm, _sep, value = (expected or "").strip().partition(":")
    if algorithm.lower() != "sha256" or not value.strip():
        return False
    try:
        return file_sha256(path) == value.strip().lower()
    except OSError:
        return False


def _signature_command(path: Path) -> list[str]:
    """The PowerShell call that prints ``<status>|<subject>`` for one file.

    The path is interpolated into a PowerShell **single-quoted** string, where the only escape is a
    doubled quote. Without that doubling a path such as ``C:\\it's\\a.exe`` ends the string early and
    the rest of the path is parsed as PowerShell code - broken at best, injection at worst. A
    temporary ``.ps1`` plus ``-File`` would avoid quoting altogether, but that adds a file to create,
    encode, locate and clean up; doubling one character is the smaller change for one subprocess.

    Kept as its own function so the quoting can be asserted without running PowerShell (and so the
    real invocation can be exercised directly on Windows).
    """
    quoted = str(path).replace("'", "''")
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            f"$s = Get-AuthenticodeSignature -FilePath '{quoted}'; "
            '"$($s.Status)|$($s.SignerCertificate.Subject)"'
        ),
    ]


def signature_is_valid(path: Path) -> bool:
    """Whether the file is signed by this project's certificate without being tampered with.

    The launcher ships a **self-signed** certificate, so on a normal machine the chain is untrusted
    and Windows reports ``UnknownError`` — that is expected and must not block an update. This is the
    *fallback* check for releases without a digest; see ``EXPECTED_SIGNER`` for what the subject
    comparison can and cannot prove.
    """
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            _signature_command(path),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    status, _sep, subject = result.stdout.strip().partition("|")
    if status.strip().lower() not in ACCEPTED_SIGNATURE_STATUSES:
        return False
    return EXPECTED_SIGNER.lower() in subject.strip().lower()


def download_release_exe(
    release: ReleaseInfo,
    target_dir: Path,
    client=None,
    on_progress=None,
    cancel=None,
) -> Path:
    """Download the release EXE, verify size + digest (or signature when there is no digest), return it.

    The partial file is removed and :class:`UpdateError` raised as soon as a check fails, so a broken
    download can never be mistaken for a usable update. ``cancel`` is an optional
    :class:`threading.Event`; setting it stops the transfer at the next chunk and removes the partial
    file exactly like a failed check does. The optional hook exists because the process-wide
    cancellation flag is about quitting the launcher, while the About page's cancel button stops one
    download and keeps the launcher running.
    """
    from launcher.meta.manifest import _new_client

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / release.exe_name
    partial = target.with_name(target.name + ".part")
    owns_client = client is None
    client = client or _new_client()

    def _cleanup() -> None:
        partial.unlink(missing_ok=True)

    try:
        from launcher.net.downloader import DownloadCancelled, download_activity, is_cancelled

        def _cancelled() -> bool:
            return is_cancelled() or (cancel is not None and cancel.is_set())

        try:
            # Counted like any other download: quitting while an update is streaming in must ask too.
            # The same scope is the cancellation point: a shutdown stops this stream where it is, and
            # the partial file is cleaned up like any other abandoned update rather than installed.
            with download_activity(), client.stream("GET", release.exe_url) as response:
                if response.status_code != 200:
                    raise UpdateError("http", f"HTTP {response.status_code}")
                total = int(response.headers.get("Content-Length") or release.exe_size or 0)
                done = 0
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1 << 16):
                        if _cancelled():
                            raise DownloadCancelled(release.exe_url)
                        handle.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(done, total)
                # A cancel that arrives on the last chunk must still win: the bytes are on disk, but
                # the user asked to stop, and a file that is not wanted must not reach verification.
                if _cancelled():
                    raise DownloadCancelled(release.exe_url)
        except DownloadCancelled:
            _cleanup()
            raise
        except UpdateError:
            _cleanup()
            raise
        except Exception as exc:
            _cleanup()
            raise UpdateError("network", str(exc)) from exc

        actual_size = partial.stat().st_size if partial.is_file() else 0
        if release.exe_size and actual_size != release.exe_size:
            _cleanup()
            raise UpdateError("size", f"{actual_size} != {release.exe_size}")
        if not is_pe_file(partial):
            _cleanup()
            raise UpdateError("signature", "not a Windows executable")
        if release.exe_digest:
            # Primary check: the digest GitHub serves for this asset. Different bytes mean this is
            # not the published file, so the download is discarded before it can be installed.
            if not digest_is_valid(partial, release.exe_digest):
                _cleanup()
                raise UpdateError("signature", f"sha256 digest mismatch: {release.exe_digest!r}")
        elif os.name == "nt" and not signature_is_valid(partial):
            # No digest on this release (published before GitHub added the field): fall back to the
            # old Authenticode policy, identical to the behaviour before digests existed.
            _cleanup()
            raise UpdateError("signature", "invalid Authenticode signature")
        target.unlink(missing_ok=True)
        partial.replace(target)
        return target
    finally:
        if owns_client:
            client.close()


def can_self_update() -> bool:
    """True only for a packaged build whose executable sits in a writable folder.

    The actual rules live in :func:`launcher.update.install.check_feasibility`, which the install path
    also uses; this stays as the cheap question the GUI asks before it offers an install.
    """
    from launcher.update import install

    return install.check_feasibility().ok


def updates_dir() -> Path:
    """Where downloads are staged (inside the launcher data directory)."""
    from launcher import paths

    return paths.launcher_dir() / "updates"


def pending_file() -> Path:
    return updates_dir() / "pending.json"


def save_pending(exe: Path, version: str) -> None:
    """Remember a downloaded update so a later session (or exit) can install it."""
    path = pending_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exe": str(exe), "version": version}), encoding="utf-8")


def load_pending() -> tuple[Path, str] | None:
    """The staged update, if it is still on disk."""
    path = pending_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):  # valid JSON that is not an object: treat as absent
        return None
    exe = Path(str(payload.get("exe") or ""))
    if not exe.is_file():
        return None
    return exe, str(payload.get("version") or "")


def clear_pending() -> None:
    pending_file().unlink(missing_ok=True)


def cleanup_leftovers(target_exe: Path | None = None) -> None:
    """Best effort: drop what a finished or interrupted update left next to the executable.

    The real rules (and the "never fail a start" guarantee) live in
    :func:`launcher.update.install.cleanup_leftovers`; this wrapper keeps the update module's public
    surface in one place for the GUI.
    """
    from launcher.update import install

    install.cleanup_leftovers(target_exe)


def cleanup_backups(target_exe: Path) -> None:
    """Backwards-compatible name for :func:`cleanup_leftovers` (it also handles the old ``.bak``)."""
    cleanup_leftovers(target_exe)
