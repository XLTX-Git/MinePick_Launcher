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

"""The local half of self-update: channels, cancellation, feasibility, swap, rollback, restart, cleanup.

Every network path uses a fake GitHub response (respx), and every executable path uses real files in a
workspace temp directory - the swap is three renames, so a test that only asserted the call arguments
would not notice a broken rollback. The only process ever started is stubbed, and
``test_the_install_module_has_no_shell`` pins the "no shell command" rule structurally.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import httpx
import pytest
import respx

from launcher import updates
from launcher.net.downloader import DownloadCancelled
from launcher.update import install

API = updates.RELEASES_API
LIST_API = updates.RELEASES_LIST_API
EXE_URL = "https://example.invalid/launcher.exe"
PAYLOAD = b"MZ" + b"1" * 4094


def _payload(
    tag: str = "v0.6.0",
    *,
    prerelease: bool = False,
    digest: str | None = None,
    published_at: str = "2026-11-01T09:30:00Z",
) -> dict:
    asset = {
        "name": "MinePick_Launcher.exe",
        "browser_download_url": EXE_URL,
        "size": len(PAYLOAD),
    }
    if digest is not None:
        asset["digest"] = digest
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{updates.REPO}/releases/tag/{tag}",
        "body": "## 修复\n- 自动更新",
        "prerelease": prerelease,
        "published_at": published_at,
        "assets": [asset],
    }


def _release(**kwargs) -> updates.ReleaseInfo:
    digest = kwargs.pop("digest", "")
    return updates.ReleaseInfo(
        version=kwargs.pop("version", "0.6.0"),
        tag="v0.6.0",
        page_url="https://example.invalid/release",
        notes="notes",
        exe_url=EXE_URL,
        exe_name="MinePick_Launcher.exe",
        exe_size=len(PAYLOAD),
        exe_digest=digest,
        **kwargs,
    )


def _as_packaged(monkeypatch, executable: Path) -> None:
    """Pretend this is a frozen build running from ``executable`` (and restore it afterwards)."""
    monkeypatch.setattr(install.sys, "frozen", True, raising=False)
    monkeypatch.setattr(install.sys, "executable", str(executable))


class _FakeProcess:
    """Stands in for the restarted launcher; records nothing itself (the test records the call)."""


@pytest.fixture()
def no_restart(monkeypatch):
    """Capture every attempt to start the new build instead of really starting it."""
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(arguments, **kwargs):
        calls.append((list(arguments), kwargs))
        return _FakeProcess()

    monkeypatch.setattr(install.subprocess, "Popen", fake_popen)
    return calls


# ---------- channels ----------


@respx.mock
def test_stable_channel_reads_latest_and_its_metadata() -> None:
    respx.get(API).mock(return_value=httpx.Response(200, json=_payload(digest="sha256:" + "a" * 64)))
    release = updates.fetch_latest_release(httpx.Client())
    assert release.version == "0.6.0"
    assert release.prerelease is False
    assert release.published_date == "2026-11-01"
    assert release.exe_digest == "sha256:" + "a" * 64


@respx.mock
def test_stable_channel_refuses_a_prerelease() -> None:
    respx.get(API).mock(return_value=httpx.Response(200, json=_payload(prerelease=True)))
    with pytest.raises(updates.UpdateError) as exc:
        updates.fetch_latest_release(httpx.Client())
    assert exc.value.code == "no_release"


@respx.mock
def test_beta_channel_accepts_a_prerelease_from_the_release_list() -> None:
    respx.get(f"{LIST_API}?per_page=20").mock(
        return_value=httpx.Response(
            200,
            json=[_payload(tag="v0.7.0-beta.1", prerelease=True), _payload(tag="v0.6.0")],
        )
    )
    release = updates.fetch_latest_release(httpx.Client(), channel="beta")
    assert release.version == "0.7.0-beta.1"
    assert release.prerelease is True


@respx.mock
def test_beta_channel_skips_a_release_without_a_launcher_asset() -> None:
    without_asset = _payload(tag="v0.7.0-beta.1", prerelease=True)
    without_asset["assets"] = [{"name": "source.zip"}]
    respx.get(f"{LIST_API}?per_page=20").mock(
        return_value=httpx.Response(200, json=[without_asset, _payload(tag="v0.6.0")])
    )
    assert updates.fetch_latest_release(httpx.Client(), channel="beta").version == "0.6.0"


@respx.mock
def test_check_for_update_uses_the_channel_and_reports_the_check_time() -> None:
    respx.get(f"{LIST_API}?per_page=20").mock(
        return_value=httpx.Response(200, json=[_payload(tag="v0.7.0-beta.1", prerelease=True)])
    )
    result = updates.check_for_update("0.5.0", httpx.Client(), channel="beta")
    assert result.status == "update_available"
    assert result.latest == "0.7.0-beta.1"
    assert result.checked_at  # "YYYY-MM-DD HH:MM", for the "already up to date" line


def test_an_unknown_channel_is_refused() -> None:
    with pytest.raises(updates.UpdateError) as exc:
        updates.fetch_latest_release(client=object(), channel="nightly")
    assert exc.value.code == "parse"


# ---------- download verification (fake release responses) ----------


@respx.mock
def test_a_digest_mismatch_is_refused_and_nothing_is_staged(ws_tmp, monkeypatch) -> None:
    """The digest is authoritative: different bytes must never become an installable file."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setattr(updates, "signature_is_valid", lambda _path: True)  # would say yes
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=PAYLOAD))
    release = _release(digest="sha256:" + "0" * 64)
    with pytest.raises(updates.UpdateError) as exc:
        updates.download_release_exe(release, ws_tmp, client=httpx.Client())
    assert exc.value.code == "signature"
    assert list(ws_tmp.iterdir()) == []  # the .part was deleted and no file was published
    assert updates.load_pending() is None


@respx.mock
def test_a_missing_digest_falls_back_to_the_signature(ws_tmp, monkeypatch) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(updates, "signature_is_valid", lambda path: seen.append(Path(path)) or True)
    monkeypatch.setattr(updates.os, "name", "nt")  # the fallback only exists on Windows
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=PAYLOAD))
    path = updates.download_release_exe(_release(), ws_tmp, client=httpx.Client())
    assert path.read_bytes() == PAYLOAD
    assert seen, "the release declared no digest, so the Authenticode fallback must have run"


@respx.mock
def test_a_declared_but_uncheckable_digest_never_falls_back_to_the_signature(ws_tmp, monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(updates, "signature_is_valid", lambda path: calls.append(Path(path)) or True)
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=PAYLOAD))
    release = _release(digest="sha512:" + "0" * 128)
    with pytest.raises(updates.UpdateError) as exc:
        updates.download_release_exe(release, ws_tmp, client=httpx.Client())
    assert exc.value.code == "signature"
    assert calls == [], "a declared digest that cannot be checked must not be downgraded"
    assert list(ws_tmp.iterdir()) == []


@respx.mock
def test_cancelling_a_download_leaves_no_file_to_install(ws_tmp, monkeypatch) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=PAYLOAD))
    cancel = threading.Event()

    def on_progress(_done: int, _total: int) -> None:
        cancel.set()  # the user pressed "cancel download" while the stream was running

    with pytest.raises(DownloadCancelled):
        updates.download_release_exe(
            _release(), ws_tmp, client=httpx.Client(), on_progress=on_progress, cancel=cancel
        )
    assert list(ws_tmp.iterdir()) == []  # the partial file is gone; nothing can be installed
    assert updates.load_pending() is None


# ---------- feasibility ----------


def test_source_runs_refuse_self_update(ws_tmp, monkeypatch) -> None:
    monkeypatch.delattr(install.sys, "frozen", raising=False)
    target = ws_tmp / "MinePick_Launcher.exe"
    target.write_bytes(b"OLD-BUILD")
    new = ws_tmp / "download.exe"
    new.write_bytes(b"NEW-BUILD")

    feasibility = install.check_feasibility(target, len(b"NEW-BUILD"))
    assert feasibility.ok is False and feasibility.reason == "source"
    with pytest.raises(install.InstallError) as exc:
        install.apply_and_restart(new, target, argv=["app.exe"])
    assert exc.value.code == "source"
    assert target.read_bytes() == b"OLD-BUILD"  # a source checkout is never touched
    assert list(ws_tmp.glob("*.old")) == [] and list(ws_tmp.glob("*.new")) == []


def test_feasibility_reports_a_missing_target(ws_tmp, monkeypatch) -> None:
    _as_packaged(monkeypatch, ws_tmp / "MinePick_Launcher.exe")
    assert install.check_feasibility(ws_tmp / "missing.exe").reason == "missing"


def test_feasibility_refuses_an_unwritable_directory(ws_tmp, monkeypatch) -> None:
    target = ws_tmp / "MinePick_Launcher.exe"
    target.write_bytes(b"OLD")
    _as_packaged(monkeypatch, target)
    monkeypatch.setattr(install, "_probe_writable", lambda _directory: False)
    feasibility = install.check_feasibility(target, 10)
    assert feasibility.ok is False and feasibility.reason == "writable"


def test_feasibility_refuses_when_the_disk_is_too_full(ws_tmp, monkeypatch) -> None:
    target = ws_tmp / "MinePick_Launcher.exe"
    target.write_bytes(b"OLD")
    _as_packaged(monkeypatch, target)
    monkeypatch.setattr(
        install.shutil, "disk_usage", lambda _path: type("U", (), {"free": 1024, "total": 2048, "used": 1024})()
    )
    feasibility = install.check_feasibility(target, 512)
    assert feasibility.ok is False and feasibility.reason == "space"
    assert feasibility.needed > feasibility.free


# ---------- the swap ----------


def test_the_swap_replaces_the_exe_and_restarts_with_the_original_arguments(
    ws_tmp, monkeypatch, no_restart
) -> None:
    target = ws_tmp / "app" / "MinePick_Launcher.exe"
    target.parent.mkdir()
    target.write_bytes(b"OLD-BUILD")
    new = ws_tmp / "downloads" / "MinePick_Launcher.exe"
    new.parent.mkdir()
    new.write_bytes(b"NEW-BUILD")

    _as_packaged(monkeypatch, target)
    install.apply_and_restart(new, target, argv=[r"C:\Games\Launcher\MinePick_Launcher.exe", "--game-dir", r"D:\mc"])

    assert target.read_bytes() == b"NEW-BUILD"  # the verified download is now the launcher
    old = target.with_name(target.name + install.OLD_SUFFIX)
    assert old.read_bytes() == b"OLD-BUILD"  # the previous build is kept until the next start
    assert not install.staged_path(target).exists()  # the staging copy was consumed
    assert new.is_file()  # the verified download itself is the caller's artifact, not deleted here
    assert len(no_restart) == 1
    arguments, kwargs = no_restart[0]
    assert arguments == [str(target), "--game-dir", "D:\\mc"]
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"  # the restart defect guard
    assert kwargs["cwd"] == str(target.parent)


def test_a_failed_replacement_rolls_the_old_build_back(ws_tmp, monkeypatch, no_restart) -> None:
    target = ws_tmp / "app" / "MinePick_Launcher.exe"
    target.parent.mkdir()
    target.write_bytes(b"OLD-BUILD")
    new = ws_tmp / "downloads" / "MinePick_Launcher.exe"
    new.parent.mkdir()
    new.write_bytes(b"NEW-BUILD")

    _as_packaged(monkeypatch, target)
    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(source, destination):
        calls["count"] += 1
        if calls["count"] == 2:  # the second rename puts the new file at the target path
            raise OSError("the new file could not be moved into place")
        return real_replace(source, destination)

    monkeypatch.setattr(install.os, "replace", flaky_replace)
    with pytest.raises(install.InstallError) as exc:
        install.apply_and_restart(new, target, argv=["app.exe"])

    assert exc.value.code == "swap"
    assert target.read_bytes() == b"OLD-BUILD"  # the running launcher still exists
    assert not target.with_name(target.name + install.OLD_SUFFIX).exists()  # it was renamed back
    assert no_restart == []  # nothing was started after a failed swap


def test_a_restart_that_cannot_start_rolls_back(ws_tmp, monkeypatch) -> None:
    target = ws_tmp / "app" / "MinePick_Launcher.exe"
    target.parent.mkdir()
    target.write_bytes(b"OLD-BUILD")
    new = ws_tmp / "downloads" / "MinePick_Launcher.exe"
    new.parent.mkdir()
    new.write_bytes(b"NEW-BUILD")

    _as_packaged(monkeypatch, target)
    monkeypatch.setattr(
        install.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("blocked"))
    )
    with pytest.raises(install.InstallError) as exc:
        install.apply_and_restart(new, target, argv=["app.exe"])

    assert exc.value.code == "restart"
    assert target.read_bytes() == b"OLD-BUILD"  # a launcher that starts is more useful than a new one
    assert not target.with_name(target.name + install.OLD_SUFFIX).exists()


def test_the_restart_carries_the_whole_original_command_line(monkeypatch) -> None:
    monkeypatch.setattr(install.sys, "argv", ["C:\\Games\\Launcher\\MinePick_Launcher.exe", "--demo", "x y"])
    assert install.restart_arguments(Path(r"C:\Games\Launcher\MinePick_Launcher.exe")) == [
        r"C:\Games\Launcher\MinePick_Launcher.exe",
        "--demo",
        "x y",
    ]


# ---------- cleanup ----------


def test_the_next_start_removes_what_the_previous_update_left(ws_tmp, monkeypatch) -> None:
    target = ws_tmp / "MinePick_Launcher.exe"
    target.write_bytes(b"NEW-BUILD")
    leftovers = [target.with_name(target.name + suffix) for suffix in (".old", ".new", ".bak")]
    for path in leftovers:
        path.write_bytes(b"leftover")
    _as_packaged(monkeypatch, target)

    install.cleanup_leftovers(target)
    assert not any(path.exists() for path in leftovers)
    assert target.read_bytes() == b"NEW-BUILD"  # the launcher itself is untouched


def test_a_leftover_that_cannot_be_deleted_never_fails_the_start(ws_tmp, monkeypatch) -> None:
    target = ws_tmp / "MinePick_Launcher.exe"
    target.write_bytes(b"NEW-BUILD")
    locked = target.with_name(target.name + install.OLD_SUFFIX)
    locked.mkdir()  # unlink() on a directory fails with OSError on every platform
    still_a_file = target.with_name(target.name + install.NEW_SUFFIX)
    still_a_file.write_bytes(b"leftover")
    _as_packaged(monkeypatch, target)

    install.cleanup_leftovers(target)  # must not raise
    assert locked.is_dir()
    assert not still_a_file.exists()  # the other leftovers are still cleaned


def test_cleanup_touches_nothing_from_a_source_checkout(ws_tmp, monkeypatch) -> None:
    monkeypatch.delattr(install.sys, "frozen", raising=False)
    target = ws_tmp / "python.exe"
    target.write_bytes(b"python")
    leftover = target.with_name(target.name + install.OLD_SUFFIX)
    leftover.write_bytes(b"not ours")
    install.cleanup_leftovers(target)
    assert leftover.is_file(), "a source run must never delete files next to sys.executable"


# ---------- the "no shell" rule ----------


def test_the_install_module_has_no_shell() -> None:
    """The swap must be renames, not a generated batch file or a shell command.

    Two separate regressions this guards: an update that runs ``cmd``/PowerShell (a second interpreter
    executing a file next to the user's launcher), and an update that quietly goes back to copying over
    a running executable. The comments may mention the old helper, so only executable lines count.
    """
    source = Path(install.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("subprocess.run", "subprocess.call", "os.system", "cmd.exe", "powershell", '"/c"'):
        assert forbidden not in code, f"the install path executes a shell ({forbidden})"
    assert "os.replace" in code
    assert "shutil.copy2" in code or "shutil.move" in code


def test_a_sha256_of_the_downloaded_bytes_is_still_the_primary_check(ws_tmp) -> None:
    """Kept next to the install tests to make the ordering explicit: digest first, signature second."""
    candidate = ws_tmp / "candidate.exe"
    candidate.write_bytes(PAYLOAD)
    assert updates.digest_is_valid(candidate, "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()) is True
