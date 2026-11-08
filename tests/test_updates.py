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

"""Update logic tests: release lookup, version comparison, asset choice, download checks and refusal."""

import hashlib
import os
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from launcher import updates

API = updates.RELEASES_API
EXE_URL = "https://example.invalid/launcher.exe"


def _payload(tag: str = "v0.2.0", assets: list[dict] | None = None, body: str = "notes") -> dict:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{updates.REPO}/releases/tag/{tag}",
        "body": body,
        "assets": assets
        if assets is not None
        else [
            {"name": "MinePick_Launcher.exe", "browser_download_url": EXE_URL, "size": 6},
            {"name": "Source_code.zip", "browser_download_url": "https://example.invalid/src.zip", "size": 9},
        ],
    }


def _release(
    size: int = 6, name: str = "MinePick_Launcher.exe", digest: str = ""
) -> updates.ReleaseInfo:
    return updates.ReleaseInfo(
        version="0.2.0",
        tag="v0.2.0",
        page_url="https://example.invalid/release",
        notes="",
        exe_url=EXE_URL,
        exe_name=name,
        exe_size=size,
        exe_digest=digest,
    )


# ---------- version handling ----------


def test_version_comparison() -> None:
    assert updates.is_newer("0.2.0", "0.1.5") is True
    assert updates.is_newer("v0.2.0", "0.1.5") is True
    assert updates.is_newer("0.1.10", "0.1.9") is True
    assert updates.is_newer("0.1.5", "0.2.0") is False  # a local build newer than the release
    assert updates.is_newer("0.2.0", "0.2.0") is False
    assert updates.is_newer("nonsense", "0.1.5") is False  # unparsable never looks newer
    assert updates.parse_version("v0.2.0") == updates.parse_version("0.2.0")
    assert updates.parse_version("") is None


def test_select_exe_asset_prefers_current_then_legacy_then_single() -> None:
    both = [
        {"name": "MinePick_UI_Trial.exe"},
        {"name": "MinePick_Launcher.exe"},
    ]
    assert updates.select_exe_asset(both)["name"] == "MinePick_Launcher.exe"
    legacy = [{"name": "MinePick_UI_Trial.exe"}, {"name": "Source_code.zip"}]
    assert updates.select_exe_asset(legacy)["name"] == "MinePick_UI_Trial.exe"
    renamed = [{"name": "MinePick.exe"}]
    assert updates.select_exe_asset(renamed)["name"] == "MinePick.exe"
    assert updates.select_exe_asset([{"name": "a.exe"}, {"name": "b.exe"}]) is None
    assert updates.select_exe_asset([]) is None


# ---------- release lookup ----------


@respx.mock
def test_fetch_latest_release() -> None:
    respx.get(API).mock(return_value=httpx.Response(200, json=_payload()))
    release = updates.fetch_latest_release(httpx.Client())
    assert release.version == "0.2.0"
    assert release.tag == "v0.2.0"
    assert release.exe_name == "MinePick_Launcher.exe"
    assert release.exe_size == 6
    assert release.exe_digest == ""  # no digest in this payload: the signature fallback applies
    assert release.page_url.endswith("/v0.2.0")


@respx.mock
def test_fetch_latest_release_reads_the_asset_digest() -> None:
    digest = "sha256:" + "b" * 64
    payload = _payload()
    payload["assets"][0]["digest"] = digest
    respx.get(API).mock(return_value=httpx.Response(200, json=payload))
    assert updates.fetch_latest_release(httpx.Client()).exe_digest == digest

    payload["assets"][0]["digest"] = None  # the API can return null for an asset
    respx.get(API).mock(return_value=httpx.Response(200, json=payload))
    assert updates.fetch_latest_release(httpx.Client()).exe_digest == ""


@respx.mock
def test_fetch_latest_release_failures() -> None:
    respx.get(API).mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(updates.UpdateError) as http_error:
        updates.fetch_latest_release(httpx.Client())
    assert http_error.value.code == "http"

    respx.get(API).mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(updates.UpdateError) as parse_error:
        updates.fetch_latest_release(httpx.Client())
    assert parse_error.value.code == "parse"

    respx.get(API).mock(return_value=httpx.Response(200, json=_payload(assets=[{"name": "x.zip"}])))
    with pytest.raises(updates.UpdateError) as asset_error:
        updates.fetch_latest_release(httpx.Client())
    assert asset_error.value.code == "no_asset"

    respx.get(API).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(updates.UpdateError) as net_error:
        updates.fetch_latest_release(httpx.Client())
    assert net_error.value.code == "network"


@respx.mock
def test_check_for_update_statuses() -> None:
    respx.get(API).mock(return_value=httpx.Response(200, json=_payload(tag="v0.3.0")))
    assert updates.check_for_update("0.2.0", httpx.Client()).status == "update_available"

    respx.get(API).mock(return_value=httpx.Response(200, json=_payload(tag="v0.2.0")))
    same = updates.check_for_update("0.2.0", httpx.Client())
    assert same.status == "up_to_date" and same.latest == "0.2.0"

    # a release older than what we run must never nag (0.2.0 is not out in the wild yet)
    respx.get(API).mock(return_value=httpx.Response(200, json=_payload(tag="v0.1.5")))
    assert updates.check_for_update("0.2.0", httpx.Client()).status == "up_to_date"

    respx.get(API).mock(side_effect=httpx.ConnectError("boom"))
    failed = updates.check_for_update("0.2.0", httpx.Client())
    assert failed.status == "failed" and failed.error == "network"


@respx.mock
def test_check_for_update_never_raises() -> None:
    respx.get(API).mock(side_effect=RuntimeError("unexpected"))
    result = updates.check_for_update("0.2.0", httpx.Client())
    assert result.status == "failed" and result.error == "network"


# ---------- download checks ----------


@respx.mock
def test_download_happy_path(ws_tmp, monkeypatch) -> None:
    monkeypatch.setattr(updates, "signature_is_valid", lambda _path: True)
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=b"MZ" + b"0" * 4))
    seen: list[tuple[int, int]] = []
    path = updates.download_release_exe(
        _release(), ws_tmp, client=httpx.Client(), on_progress=lambda d, t: seen.append((d, t))
    )
    assert path.read_bytes().startswith(b"MZ")
    assert seen and seen[-1][0] == 6
    assert not list(ws_tmp.glob("*.part"))


@respx.mock
def test_download_accepts_the_published_digest(ws_tmp, monkeypatch) -> None:
    """A SHA-256 matching the release asset's ``digest`` is the primary check and is enough."""
    payload = b"MZ" + b"1" * 4
    signature_checks: list[Path] = []
    monkeypatch.setattr(
        updates, "signature_is_valid", lambda path: signature_checks.append(path) or True
    )
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=payload))
    release = _release(digest="sha256:" + hashlib.sha256(payload).hexdigest())
    path = updates.download_release_exe(release, ws_tmp, client=httpx.Client())
    assert path.read_bytes() == payload
    assert signature_checks == []  # the digest is authoritative, no Authenticode round-trip


@respx.mock
def test_download_refuses_a_digest_mismatch_and_deletes_the_partial(ws_tmp, monkeypatch) -> None:
    payload = b"MZ" + b"1" * 4
    monkeypatch.setattr(updates, "signature_is_valid", lambda _path: True)  # would say yes
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=payload))
    release = _release(digest="sha256:" + "0" * 64)
    with pytest.raises(updates.UpdateError) as exc:
        updates.download_release_exe(release, ws_tmp, client=httpx.Client())
    assert exc.value.code == "signature"  # the existing "failed verification" bucket
    assert not list(ws_tmp.iterdir())  # the .part was deleted and nothing was staged


@respx.mock
def test_a_null_digest_falls_back_to_the_signature_and_still_works(ws_tmp, monkeypatch) -> None:
    """Old releases (or an API returning ``"digest": null``) keep the pre-digest behaviour."""
    payload = b"MZ" + b"1" * 4
    signature_checks: list[Path] = []
    monkeypatch.setattr(
        updates, "signature_is_valid", lambda path: signature_checks.append(path) or True
    )
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=payload))
    release = _release(digest="")  # the asset object has no usable digest
    path = updates.download_release_exe(release, ws_tmp, client=httpx.Client())
    assert path.read_bytes() == payload
    assert bool(signature_checks) is (os.name == "nt")  # unchanged: no Authenticode off Windows


@respx.mock
def test_download_refuses_size_mismatch(ws_tmp) -> None:
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=b"MZ"))
    with pytest.raises(updates.UpdateError) as exc:
        updates.download_release_exe(_release(size=999), ws_tmp, client=httpx.Client())
    assert exc.value.code == "size"
    assert not list(ws_tmp.iterdir())  # nothing left behind


@respx.mock
def test_download_refuses_non_pe(ws_tmp) -> None:
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=b"not an exe"))
    with pytest.raises(updates.UpdateError) as exc:
        updates.download_release_exe(_release(size=10), ws_tmp, client=httpx.Client())
    assert exc.value.code == "signature"
    assert not list(ws_tmp.iterdir())


@respx.mock
def test_download_refuses_invalid_signature(ws_tmp, monkeypatch) -> None:
    monkeypatch.setattr(updates, "signature_is_valid", lambda _path: False)
    respx.get(EXE_URL).mock(return_value=httpx.Response(200, content=b"MZ" + b"0" * 4))
    with pytest.raises(updates.UpdateError) as exc:
        updates.download_release_exe(_release(), ws_tmp, client=httpx.Client())
    assert exc.value.code == "signature"
    assert not list(ws_tmp.iterdir())


@respx.mock
def test_download_reports_network_failure(ws_tmp) -> None:
    respx.get(EXE_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(updates.UpdateError) as exc:
        updates.download_release_exe(_release(), ws_tmp, client=httpx.Client())
    assert exc.value.code == "network"


def test_is_pe_file(ws_tmp) -> None:
    exe = ws_tmp / "a.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 10)
    assert updates.is_pe_file(exe) is True
    exe.write_bytes(b"PK\x03\x04")
    assert updates.is_pe_file(exe) is False
    assert updates.is_pe_file(ws_tmp / "missing.exe") is False


def test_digest_is_valid_only_with_a_matching_sha256(ws_tmp) -> None:
    """The digest comparison itself: case-insensitive hex, and anything unverifiable is a refusal."""
    exe = ws_tmp / "a.exe"
    exe.write_bytes(b"MZ-payload")
    good = hashlib.sha256(b"MZ-payload").hexdigest()
    assert updates.digest_is_valid(exe, "sha256:" + good) is True
    assert updates.digest_is_valid(exe, "SHA256:" + good.upper()) is True
    assert updates.digest_is_valid(exe, "sha256:" + "0" * 64) is False
    assert updates.digest_is_valid(exe, "sha512:" + "0" * 128) is False  # cannot be checked -> no
    assert updates.digest_is_valid(exe, "not-a-digest") is False
    assert updates.digest_is_valid(exe, "") is False
    assert updates.digest_is_valid(ws_tmp / "missing.exe", "sha256:" + good) is False


def test_signature_policy_accepts_our_self_signed_build(monkeypatch) -> None:
    """The certificate is self-signed: an untrusted chain is fine, a foreign signer or tampering is not."""
    reply = {"stdout": ""}

    class _Completed:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(_command, **_kwargs):
        return _Completed(reply["stdout"])

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    ours = "CN=WDNDXLTX, E=wdndxltx@gmail.com"
    for accepted in (f"Valid|{ours}", f"UnknownError|{ours}", f"NotTrusted|{ours}"):
        reply["stdout"] = accepted
        assert updates.signature_is_valid(Path("candidate.exe")) is True, accepted
    for rejected in (f"HashMismatch|{ours}", "NotSigned|", "Valid|CN=Evil Corp"):
        reply["stdout"] = rejected
        assert updates.signature_is_valid(Path("candidate.exe")) is False, rejected


def test_the_signature_command_doubles_a_single_quote_in_the_path() -> None:
    """A path is file-system data: one ``'`` used to end the PowerShell string and break the check."""
    command = updates._signature_command(Path(r"C:\it's a\launcher.exe"))
    script = command[-1]
    assert r"C:\it''s a\launcher.exe" in script  # PowerShell's escape inside '...'
    assert r"C:\it's a\launcher.exe" not in script  # the unescaped form would inject


@pytest.mark.skipif(os.name != "nt", reason="Authenticode only exists on Windows")
def test_the_signature_check_really_runs_with_a_quoted_path(ws_tmp) -> None:
    """Run PowerShell for real: stdout must have the status|subject shape, not a parse error.

    ``signature_is_valid`` returns False for an unsigned file, which a broken command also produces
    (empty stdout), so the command is executed directly to tell the two apart.
    """
    exe = ws_tmp / "it's a launcher's copy.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 64)
    assert updates.signature_is_valid(exe) is False  # a result, not an exception
    result = subprocess.run(
        updates._signature_command(exe), capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    status, separator, _subject = result.stdout.strip().partition("|")
    assert separator and status.strip(), (result.stdout, result.stderr)


# ---------- install preconditions ----------
#
# The swap itself (feasibility, rename, rollback, restart arguments, ``.old`` cleanup) moved to
# tests/test_update_install.py, next to the new in-process installer. What stays here is the update
# module's own small surface: "can this build self-update at all" and the staged-update record.


def test_can_self_update_is_false_without_a_frozen_build() -> None:
    assert updates.can_self_update() is False  # pytest runs from source


def test_pending_roundtrip(ws_tmp, monkeypatch) -> None:
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    exe = ws_tmp / "staged.exe"
    exe.write_bytes(b"MZ")
    assert updates.load_pending() is None
    updates.save_pending(exe, "0.2.0")
    assert updates.load_pending() == (exe, "0.2.0")
    exe.unlink()
    assert updates.load_pending() is None  # a stale entry is ignored
    exe.write_bytes(b"MZ")
    updates.save_pending(exe, "0.2.0")
    updates.clear_pending()
    assert updates.load_pending() is None
