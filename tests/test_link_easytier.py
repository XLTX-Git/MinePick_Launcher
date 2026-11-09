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

"""EasyTier component and CLI probe: parsing, timeout, verification, and the fallback category.

No test contacts GitHub or runs the real 9.9 MB executable. The install tests use a small ZIP
served by the suite's HTTP mirror, so the production downloader, archive verifier, ZIP extractor
and metadata writer all run unmodified. The CLI tests inject the subprocess runner.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

import pytest

from launcher.link import easytier
from launcher.link.components import ComponentError
from launcher.link.easytier import (
    EASYTIER_PACKAGE,
    EasytierPackage,
    cli_path,
    component_dir,
    install,
    parse_stun_output,
    probe_nat_types,
    run_cli_stun,
    verify_archive,
)
from launcher.link.selfcheck import NatProbe, NatProbeError, NatType
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


def _archive(cli: bytes = b"MZ fake easytier cli", extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("easytier-windows-x86_64/easytier-cli.exe", cli)
        bundle.writestr("easytier-windows-x86_64/easytier-core.exe", b"core must not be installed")
        for name, payload in (extra or {}).items():
            bundle.writestr(name, payload)
    return buffer.getvalue()


def _package(payload: bytes, cli: bytes = b"MZ fake easytier cli", **broken) -> EasytierPackage:
    return EasytierPackage(
        version="2.6.4-test",
        asset="easytier-windows-x86_64-v2.6.4.zip",
        size=broken.get("size", len(payload)),
        sha256=broken.get("sha256", hashlib.sha256(payload).hexdigest()),
        executable="easytier-cli.exe",
        executable_size=broken.get("executable_size", len(cli)),
        executable_sha256=broken.get(
            "executable_sha256", hashlib.sha256(cli).hexdigest()
        ),
    )


def _install(server: FakeMirror, package: EasytierPackage, ws_tmp: Path):
    return install(
        package=package,
        mirrors=(easytier.mirror_module.Mirror("test", "Test", server.base_url),),
        preferred="test",
        cache_path_override=ws_tmp / "easytier-pick.json",
        timeout=5.0,
    )


# ------------------------------------------------------------------ JSON / CLI


def test_accepts_extra_fields_and_every_code() -> None:
    result = parse_stun_output(
        '{"udp_nat_type": 5, "tcp_nat_type": 3, "last_update_time": 1, '
        '"public_ip": ["203.0.113.7"], "min_port": 1, "max_port": 2}'
    )
    assert result == NatProbe(udp=NatType.SYMMETRIC_EASY, tcp=NatType.RESTRICTED_CONE)
    for code in range(9):
        result = parse_stun_output(f'{{"udp_nat_type": {code}, "tcp_nat_type": {code}}}')
        assert result.udp is NatType(code) and result.tcp is NatType(code)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{not json",
        "[]",
        '{"udp_nat_type": 3}',
        '{"udp_nat_type": 9, "tcp_nat_type": 3}',
        '{"udp_nat_type": -1, "tcp_nat_type": 3}',
        '{"udp_nat_type": true, "tcp_nat_type": 3}',
        '{"udp_nat_type": "3", "tcp_nat_type": 3}',
    ],
)
def test_bad_output_is_a_category_not_an_exception(raw: str) -> None:
    result = parse_stun_output(raw)
    assert result.error is NatProbeError.BAD_OUTPUT
    assert not result.tested
    assert result.detail


def test_cli_timeout_returns_timeout(ws_tmp) -> None:
    cli = ws_tmp / "easytier-cli.exe"
    cli.write_bytes(b"stub")

    def runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(str(cli), 15.0)

    result = run_cli_stun(cli, runner=runner)
    assert result.error is NatProbeError.TIMEOUT
    assert result.udp is None and result.tcp is None


def test_cli_nonzero_exit_returns_exit(ws_tmp) -> None:
    cli = ws_tmp / "easytier-cli.exe"
    cli.write_bytes(b"stub")

    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(args, 7, stdout="", stderr="boom")

    result = run_cli_stun(cli, runner=runner)
    assert result.error is NatProbeError.EXIT
    assert "7" in result.detail


def test_cli_success_parses_both_types(ws_tmp) -> None:
    cli = ws_tmp / "easytier-cli.exe"
    cli.write_bytes(b"stub")

    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout='{"udp_nat_type": 6, "tcp_nat_type": 2}', stderr=""
        )

    result = run_cli_stun(cli, runner=runner)
    assert result == NatProbe(udp=NatType.SYMMETRIC, tcp=NatType.FULL_CONE)


# ------------------------------------------------------------------ component install


def test_install_extracts_only_the_verified_cli(data_dir, mirrors, ws_tmp) -> None:
    cli = b"MZ fake easytier cli"
    payload = _archive(cli)
    server = mirrors(payload)
    package = _package(payload, cli)
    seen: list[tuple[int, int]] = []

    installed = install(
        progress=lambda done, total: seen.append((done, total)),
        package=package,
        mirrors=(easytier.mirror_module.Mirror("test", "Test", server.base_url),),
        preferred="test",
        cache_path_override=ws_tmp / "easytier-pick.json",
        timeout=5.0,
    )

    assert installed == cli_path(package)
    assert installed.read_bytes() == cli
    assert not (component_dir() / "easytier-core.exe").exists()
    installed_files = {path.name for path in component_dir().iterdir() if path.is_file()}
    assert installed_files == {"easytier-cli.exe", "installed.json"}
    assert seen[-1] == (1, 1)
    assert '"license": "LGPL-3.0"' in (component_dir() / "installed.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("broken", [{"size": 1}, {"sha256": "0" * 64}])
def test_install_refuses_a_bad_archive_hash(data_dir, mirrors, ws_tmp, broken) -> None:
    cli = b"MZ fake easytier cli"
    payload = _archive(cli)
    package = _package(payload, cli, **broken)
    server = mirrors(payload)

    with pytest.raises(ComponentError) as raised:
        _install(server, package, ws_tmp)

    assert raised.value.key == "tools.fail.verify"
    assert not (component_dir() / package.executable).exists()


def test_install_refuses_an_archive_whose_cli_hash_differs(mirrors, data_dir, ws_tmp) -> None:
    cli = b"MZ fake easytier cli"
    payload = _archive(cli)
    package = _package(payload, cli, executable_sha256="0" * 64)
    server = mirrors(payload)

    with pytest.raises(ComponentError) as raised:
        _install(server, package, ws_tmp)

    assert raised.value.key == "tools.fail.verify"
    assert not (component_dir() / package.executable).exists()


def test_install_ignores_a_member_that_tries_to_escape(mirrors, data_dir, ws_tmp) -> None:
    cli = b"MZ fake easytier cli"
    payload = _archive(cli, extra={"../outside.exe": b"outside"})
    package = _package(payload, cli)
    server = mirrors(payload)

    installed = _install(server, package, ws_tmp)

    assert installed.read_bytes() == cli
    assert not (ws_tmp / "outside.exe").exists()
    assert not (component_dir().parent / "outside.exe").exists()


def test_verify_archive_checks_size_and_hash(data_dir, ws_tmp) -> None:
    payload = b"archive"
    path = ws_tmp / "asset.zip"
    path.write_bytes(payload)
    package = EasytierPackage(
        version="test",
        asset="a.zip",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        executable="easytier-cli.exe",
        executable_size=1,
        executable_sha256="0" * 64,
    )
    assert verify_archive(path, package)
    assert not verify_archive(path, _package(payload, b"x", size=len(payload) + 1))


# ------------------------------------------------------------------ fallback categories


def test_probe_without_install_returns_missing(data_dir) -> None:
    result = probe_nat_types(install_if_missing=False)
    assert result.error is NatProbeError.MISSING
    assert not result.tested


def test_install_failure_returns_install_category(data_dir) -> None:
    def installer():
        raise ComponentError("tools.fail.download", "mirror unavailable")

    result = probe_nat_types(installer=installer)
    assert result.error is NatProbeError.INSTALL
    assert "mirror unavailable" in result.detail


def test_probe_uses_an_explicit_cli_and_never_installs(ws_tmp) -> None:
    cli = ws_tmp / "easytier-cli.exe"
    cli.write_bytes(b"stub")

    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout='{"udp_nat_type": 3, "tcp_nat_type": 4}', stderr=""
        )

    result = probe_nat_types(cli=cli, runner=runner, installer=lambda: pytest.fail("installed"))
    assert result == NatProbe(udp=NatType.RESTRICTED_CONE, tcp=NatType.PORT_RESTRICTED_CONE)


def test_platform_without_an_asset_returns_missing(monkeypatch) -> None:
    monkeypatch.setattr(easytier, "package_for_platform", lambda: None)
    result = probe_nat_types()
    assert result.error is NatProbeError.MISSING


def test_the_shipped_constants_stay_for_2_6_4() -> None:
    """The version and both byte counts are the measured v2.6.4 release, not placeholders."""
    assert EASYTIER_PACKAGE.version == "2.6.4"
    assert EASYTIER_PACKAGE.size == 32_657_720
    assert EASYTIER_PACKAGE.executable_size == 9_973_248
    assert easytier.CLI_TIMEOUT == 15.0
