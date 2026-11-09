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

"""Component install: the mirror race, verification, and tar extraction that cannot escape.

The mirrors are local HTTP servers serving a small tar built by the test, so every branch runs
without the network and without the 8 MB release asset. The production installer, verifier and
extractor are the code under test.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import time
from pathlib import Path

import pytest

from launcher.link import mirrors as mirror_module
from launcher.link.components import (
    ComponentError,
    PackageSpec,
    _extract_archive,
    component_dir,
    component_status,
    install,
    remove,
    verify_archive,
)
from tests.link_fakes import FakeMirror


@pytest.fixture()
def data_dir(ws_tmp, monkeypatch):
    target = ws_tmp / "launcher"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(target))
    return target


@pytest.fixture()
def mirrors():
    created: list[FakeMirror] = []

    def make(payload: bytes, *, delay: float = 0.0) -> FakeMirror:
        server = FakeMirror(payload, delay=delay)
        created.append(server)
        return server

    yield make
    for server in created:
        server.stop()


def _tar_bytes(files: dict[str, bytes], *, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in {**files, **(extra or {})}.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _package(payload: bytes, *, size: int | None = None, sha256: str | None = None) -> PackageSpec:
    return PackageSpec(
        version="0.0-test",
        asset="terracotta-0.0-test-windows-x86_64-pkg.tar.gz",
        size=len(payload) if size is None else size,
        sha256=hashlib.sha256(payload).hexdigest() if sha256 is None else sha256,
        executable="terracotta-test.exe",
        dll="TEST.DLL",
    )


def _mirror(server: FakeMirror, key: str = "test") -> mirror_module.Mirror:
    return mirror_module.Mirror(key, key.upper(), server.base_url)


def _install(server: FakeMirror, package: PackageSpec, ws_tmp: Path, **kwargs):
    return install(
        package=package,
        mirrors=(_mirror(server),),
        preferred="test",
        cache_path=ws_tmp / "mirror-pick.json",
        timeout=5.0,
        **kwargs,
    )


# ------------------------------------------------------------------ install


def test_install_verifies_extracts_and_writes_metadata(data_dir, mirrors, ws_tmp) -> None:
    payload = _tar_bytes(
        {
            "terracotta-test.exe": b"MZ fake executable",
            "TEST.DLL": b"fake runtime",
        }
    )
    package = _package(payload)
    server = mirrors(payload)
    seen: list[tuple[int, int]] = []

    status = _install(server, package, ws_tmp, progress=lambda done, total: seen.append((done, total)))

    assert status.downloaded
    assert status.version == "0.0-test"
    assert (component_dir() / "terracotta-test.exe").read_bytes() == b"MZ fake executable"
    assert (component_dir() / "TEST.DLL").read_bytes() == b"fake runtime"
    assert (component_dir() / "installed.json").is_file()
    assert seen[-1] == (1, 1)
    assert component_status(package).downloaded


@pytest.mark.parametrize(
    "broken",
    [
        {"size": 1},  # truncated/wrong byte count
        {"sha256": "0" * 64},  # right length, wrong bytes
    ],
)
def test_install_refuses_a_payload_that_fails_verification(
    data_dir, mirrors, ws_tmp, broken
) -> None:
    payload = _tar_bytes({"terracotta-test.exe": b"MZ fake executable"})
    package = _package(payload, **broken)
    server = mirrors(payload)

    with pytest.raises(ComponentError) as raised:
        _install(server, package, ws_tmp)
    assert raised.value.key == "tools.fail.verify"
    assert not (component_dir() / "terracotta-test.exe").exists()
    assert not component_status(package).downloaded


def test_install_refuses_an_archive_without_the_executable(data_dir, mirrors, ws_tmp) -> None:
    payload = _tar_bytes({"README.txt": b"nothing useful"})
    package = _package(payload)
    server = mirrors(payload)
    with pytest.raises(ComponentError) as raised:
        _install(server, package, ws_tmp)
    assert raised.value.key == "tools.fail.verify"
    assert not (component_dir() / "terracotta-test.exe").exists()


# ------------------------------------------------------------------ tar traversal


def test_extract_rejects_an_entry_that_escapes_the_destination(ws_tmp) -> None:
    payload = _tar_bytes(
        {"terracotta-test.exe": b"MZ"},
        extra={"../evil.txt": b"outside", "/absolute.txt": b"absolute"},
    )
    archive = ws_tmp / "hostile.tar.gz"
    archive.write_bytes(payload)
    staging = ws_tmp / "staging"
    staging.mkdir()

    skipped = _extract_archive(archive, staging)

    assert (staging / "terracotta-test.exe").read_bytes() == b"MZ"
    assert skipped == 2
    assert not (ws_tmp / "evil.txt").exists()
    assert not (staging.parent / "evil.txt").exists()
    assert not (staging / "absolute.txt").exists()


def test_install_skips_the_escaping_entry_but_installs_the_rest(
    data_dir, mirrors, ws_tmp
) -> None:
    payload = _tar_bytes(
        {"terracotta-test.exe": b"MZ"},
        extra={"../escaped.txt": b"outside"},
    )
    package = _package(payload)
    server = mirrors(payload)

    _install(server, package, ws_tmp)

    assert (component_dir() / "terracotta-test.exe").read_bytes() == b"MZ"
    assert not (component_dir().parent / "escaped.txt").exists()


# ------------------------------------------------------------------ mirror race


def test_race_uses_the_first_answer_and_caches_it(ws_tmp, mirrors) -> None:
    slow = mirrors(b"slow", delay=0.4)
    fast = mirrors(b"fast")
    slow_mirror = mirror_module.Mirror("slow", "Slow", slow.base_url)
    fast_mirror = mirror_module.Mirror("fast", "Fast", fast.base_url)
    cache = ws_tmp / "mirror-pick.json"

    chosen = mirror_module.choose_mirror(
        version="0.4.2",
        asset="terracotta-0.4.2-windows-x86_64-pkg.tar.gz",
        mirrors=(slow_mirror, fast_mirror),
        path=cache,
        timeout=2.0,
    )
    assert chosen.key == "fast"

    report = mirror_module.read_report(cache)
    assert report.chosen == "fast"
    assert report.latency("fast") is not None

    # A fresh cache answers without probing: the request counters must not move.
    head_before = fast.requests("HEAD") + slow.requests("HEAD")
    cached = mirror_module.choose_mirror(
        version="0.4.2",
        asset="terracotta-0.4.2-windows-x86_64-pkg.tar.gz",
        mirrors=(slow_mirror, fast_mirror),
        path=cache,
        timeout=2.0,
    )
    assert cached.key == "fast"
    assert fast.requests("HEAD") + slow.requests("HEAD") == head_before


def test_a_manual_choice_wins_over_the_race(ws_tmp, mirrors) -> None:
    first = mirrors(b"first")
    second = mirrors(b"second")
    one = mirror_module.Mirror("cnb", "CNB", first.base_url)
    two = mirror_module.Mirror("github", "GitHub", second.base_url)
    cache = ws_tmp / "mirror-pick.json"

    chosen = mirror_module.choose_mirror(
        version="0.4.2",
        asset="asset.tar.gz",
        mirrors=(one, two),
        preferred="github",
        path=cache,
        timeout=2.0,
    )
    assert chosen.key == "github"
    assert first.requests() == 0 and second.requests() == 0
    assert mirror_module.read_report(cache).chosen == "github"


def test_a_manual_choice_from_the_environment(monkeypatch, ws_tmp, mirrors) -> None:
    server = mirrors(b"x")
    one = mirror_module.Mirror("gitee", "Gitee", server.base_url)
    monkeypatch.setenv(mirror_module.ENV_MIRROR, "gitee")
    chosen = mirror_module.choose_mirror(
        version="0.4.2",
        asset="asset.tar.gz",
        mirrors=(one,),
        path=ws_tmp / "mirror-pick.json",
        timeout=1.0,
    )
    assert chosen.key == "gitee"
    assert server.requests() == 0


def test_a_stale_cache_is_ignored(ws_tmp, mirrors) -> None:
    server = mirrors(b"x", delay=0.01)
    one = mirror_module.Mirror("cnb", "CNB", server.base_url)
    cache = ws_tmp / "mirror-pick.json"
    now = time.time()
    mirror_module._write_report(
        mirror_module.MirrorReport(
            chosen="cnb",
            latency_ms={"cnb": 1},
            measured_at=now - mirror_module.MIRROR_TTL - 1,
        ),
        cache,
    )
    chosen = mirror_module.choose_mirror(
        version="0.4.2",
        asset="asset.tar.gz",
        mirrors=(one,),
        path=cache,
        timeout=1.0,
        now=now,
    )
    assert chosen.key == "cnb"
    assert server.requests("HEAD") >= 1


def test_verify_archive_reads_the_bytes(data_dir, ws_tmp) -> None:
    payload = b"some bytes"
    archive = ws_tmp / "x.tar.gz"
    archive.write_bytes(payload)
    package = _package(payload)
    assert verify_archive(archive, package)
    assert not verify_archive(archive, _package(payload, size=len(payload) + 1))
    assert not verify_archive(ws_tmp / "missing.tar.gz", package)


def test_component_status_of_a_missing_payload(data_dir) -> None:
    status = component_status()
    assert not status.downloaded


# ------------------------------------------------------------------ removal (停用联机功能)


def _installed_tree(data_dir) -> tuple[Path, Path]:
    """A launcher dir with the Terracotta payload and the EasyTier self-check component."""
    terracotta = data_dir / "components" / "terracotta"
    terracotta.mkdir(parents=True)
    (terracotta / "terracotta-0.4.2-windows-x86_64.exe").write_bytes(b"x" * 4096)
    (terracotta / "installed.json").write_text('{"version": "0.4.2"}', encoding="utf-8")
    easytier = data_dir / "components" / "easytier"
    easytier.mkdir(parents=True)
    (easytier / "easytier-core.exe").write_bytes(b"y" * 64)
    return terracotta, easytier


def test_remove_deletes_only_the_terracotta_tree(data_dir) -> None:
    """The EasyTier payload belongs to the network self-check and must survive the cleanup."""
    terracotta, easytier = _installed_tree(data_dir)
    expected = sum(path.stat().st_size for path in terracotta.rglob("*") if path.is_file())
    assert remove() == expected
    assert not terracotta.exists()
    assert (easytier / "easytier-core.exe").read_bytes() == b"y" * 64
    assert component_status().supported, "removing the payload must not change platform support"


def test_remove_of_a_missing_component_reports_zero(data_dir) -> None:
    assert remove() == 0
    assert remove() == 0, "a second call is still a no-op, not an error"


def test_a_locked_payload_raises_with_the_os_reason(data_dir, monkeypatch) -> None:
    """Windows refuses to delete a running executable: the page needs that reason verbatim."""
    import shutil

    terracotta, _easytier = _installed_tree(data_dir)
    locked = terracotta / "terracotta-0.4.2-windows-x86_64.exe"
    real_rmtree = shutil.rmtree

    def refuse(target, *args, **kwargs):
        if Path(target) == terracotta:
            raise PermissionError(13, "The process cannot access the file", str(locked))
        return real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr("launcher.link.components.shutil.rmtree", refuse)
    with pytest.raises(ComponentError) as info:
        remove()
    assert info.value.key == "tools.disable.fail"
    assert "terracotta-0.4.2-windows-x86_64.exe" in info.value.detail
    assert "cannot access" in info.value.detail
    assert not (component_dir() / "terracotta-test.exe").exists()
