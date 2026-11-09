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

"""The real Terracotta backend against a local fake HTTP service.

The production client, poller and state mapping run unmodified; only the process is a stub and
the endpoint is the fake server. That is what makes these tests evidence for the shipped code
rather than for a second implementation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from launcher.link import LinkDifficulty, LinkState
from launcher.link.components import ComponentError
from launcher.link.selfcheck import NatProbe, NatProbeError, NatType, ProcessStatus
from launcher.link.stun import StunVerdict
from launcher.link.terracotta import (
    TerracottaClient,
    TerracottaError,
    TerracottaLinkSession,
    TerracottaProcess,
    read_lock_port,
)
from tests.link_fakes import (
    DummyPopen,
    FakeCore,
    FakeTerracotta,
    component_missing,
    component_ready,
    wait_until,
)

ROOM = "U/8F3K-2QX9-7WMD-4TRA"
ADDRESS = "127.0.0.1:25566"


def _make_session(fake: FakeTerracotta, *, component: bool = True, core=None, **kwargs):
    client_factory = kwargs.pop(
        "client_factory", lambda port: TerracottaClient(port, timeout=0.5)
    )
    return TerracottaLinkSession(
        process=core if core is not None else FakeCore(fake.port),
        client_factory=client_factory,
        poll_interval=0.02,
        attach_interval=0.02,
        component_status_fn=component_ready if component else component_missing,
        stun_probe=kwargs.pop("stun_probe", lambda: StunVerdict.CONE),
        nat_probe=kwargs.pop("nat_probe", lambda: NatProbe()),
        player_name=kwargs.pop("player_name", lambda: "Steve"),
        **kwargs,
    )


@pytest.fixture()
def fake():
    server = FakeTerracotta()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def session_factory():
    created: list[TerracottaLinkSession] = []

    def factory(fake: FakeTerracotta, **kwargs) -> TerracottaLinkSession:
        session = _make_session(fake, **kwargs)
        created.append(session)
        return session

    yield factory
    for session in created:
        session.dispose(wait=True)


# ------------------------------------------------------------------ state mapping


def test_attachment_restores_an_existing_room(fake, session_factory) -> None:
    """A room left running by an earlier launcher session is shown again without a launch."""
    fake.set_state(
        "host-ok",
        index=2,
        room=ROOM,
        profiles=[
            {"machine_id": "a", "name": "Steve", "vendor": "", "kind": "HOST"},
            {"machine_id": "b", "name": "Alex", "vendor": "", "kind": "GUEST"},
        ],
    )
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.HOST_READY)
    assert session.room_code() == ROOM
    assert [member.name for member in session.members()] == ["Steve", "Alex"]
    assert session.members()[0].is_host
    # Terracotta v0.4.2 profiles carry no UUID, so no skin is invented for either member.
    assert all(not member.uuid and not member.premium for member in session.members())


def test_a_repeated_index_is_ignored(fake, session_factory) -> None:
    """Only an ``index`` that grew may change the UI (the documented polling rule)."""
    fake.set_state("waiting", index=5)
    session = session_factory(fake)
    assert wait_until(lambda: fake.requests("/state"))
    time.sleep(0.1)  # let the poller consume index 5
    assert session.state() is LinkState.IDLE

    fake.set_state("host-ok", index=5, room=ROOM, profiles=[])
    time.sleep(0.15)
    assert session.state() is LinkState.IDLE

    fake.set_state("host-ok", index=6, room=ROOM, profiles=[])
    assert wait_until(lambda: session.state() is LinkState.HOST_READY)
    assert session.room_code() == ROOM


@pytest.mark.parametrize(
    ("name", "fields", "expected", "extra"),
    [
        ("waiting", {}, LinkState.IDLE, {}),
        ("host-scanning", {}, LinkState.HOST_WAITING, {"phase": "host-scanning"}),
        (
            "host-starting",
            {"room": ROOM},
            LinkState.HOST_WAITING,
            {"phase": "host-starting", "room": ROOM},
        ),
        (
            "host-ok",
            {
                "room": ROOM,
                "profiles": [{"name": "Steve", "kind": "HOST"}],
            },
            LinkState.HOST_READY,
            {"room": ROOM, "members": 1},
        ),
        (
            "guest-connecting",
            {"room": ROOM},
            LinkState.CONNECTING,
            {"room": ROOM},
        ),
        (
            "guest-starting",
            {"room": ROOM, "difficulty": "TOUGH"},
            LinkState.CONNECTING,
            {"difficulty": LinkDifficulty.TOUGH},
        ),
        (
            "guest-ok",
            {
                "url": ADDRESS,
                "profiles": [{"name": "Alex", "kind": "LOCAL"}],
            },
            LinkState.JOINED,
            {"address": ADDRESS},
        ),
    ],
)
def test_state_mapping(fake, session_factory, name, fields, expected, extra) -> None:
    fake.set_state(name, index=2, **fields)
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is expected), (name, session.state())
    for attribute, value in extra.items():
        if attribute == "members":
            assert len(session.members()) == value
        elif attribute == "phase":
            assert session.phase() == value
        elif attribute == "room":
            assert session.room_code() == value
        elif attribute == "difficulty":
            assert session.difficulty() is value
        else:
            assert getattr(session, attribute)() == value


def test_index_zero_is_a_real_answer(fake, session_factory) -> None:
    """Terracotta numbers its first state 0; the seen-index baseline must accept it."""
    fake.set_state("host-ok", index=0, room=ROOM, profiles=[])
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.HOST_READY)
    fake.set_state("waiting", index=0)
    time.sleep(0.15)
    assert session.state() is LinkState.HOST_READY


def test_control_endpoints_may_answer_without_json(fake, session_factory) -> None:
    """The real ``/state/ide`` returns 200 with an empty body; that is success, not a bad answer."""
    fake.ide_empty = True
    fake.set_state("host-ok", index=2, room=ROOM, profiles=[])
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.HOST_READY)
    session.close_room()
    assert session.state() is LinkState.IDLE
    assert fake.wait_for_request("/state/ide")
    assert wait_until(lambda: fake.state_snapshot().get("state") == "waiting")


# ------------------------------------------------------------------ exceptions


@pytest.mark.parametrize("kind", range(6))
def test_exception_types_map_to_keys_and_reset(fake, session_factory, kind) -> None:
    fake.set_state("exception", index=2, type=kind)
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.FAILED)
    assert session.failure_key() == f"tools.fail.{kind}"
    # The state machine only leaves `exception` through /state/ide; the poller must have sent it.
    assert fake.wait_for_request("/state/ide")
    assert wait_until(lambda: fake.state_snapshot().get("state") == "waiting")

    # The user can retry: cancel clears the local failure and a new join is accepted.
    session.cancel()
    assert session.state() is LinkState.IDLE
    session.join(ROOM)
    assert wait_until(lambda: session.state() is LinkState.CONNECTING)
    assert fake.wait_for_request("/state/guesting")
    assert any(
        query.get("room") == ROOM for _path, query in fake.requests("/state/guesting")
    )


def test_unknown_exception_type_falls_back_to_protocol_error(fake, session_factory) -> None:
    fake.set_state("exception", index=2, type=99)
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.FAILED)
    assert session.failure_key() == "tools.fail.5"


def test_a_stale_waiting_poll_cannot_clear_a_command_failure(fake, session_factory) -> None:
    """A 400 from /state/guesting is not erased by the waiting response already in flight."""
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.IDLE)
    client = TerracottaClient(fake.port, timeout=0.5)
    try:
        session._fail("tools.fail.bad_code")
        session._consume_state({"state": "waiting", "index": 99}, client)
        assert session.state() is LinkState.FAILED
        assert session.failure_key() == "tools.fail.bad_code"
    finally:
        client.close()


# ------------------------------------------------------------------ network failures


@pytest.mark.parametrize("mode", ["bad_json", "http_500"])
def test_broken_answers_degrade_to_failed(fake, session_factory, mode) -> None:
    fake.mode = mode
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.FAILED)
    assert session.failure_key() == "tools.fail.network"
    assert not session.is_polling()


def test_timeout_degrades_to_failed(fake, session_factory) -> None:
    fake.mode = "slow"
    fake.slow_seconds = 0.5
    session = session_factory(
        fake,
        client_factory=lambda port: TerracottaClient(port, timeout=0.15),
    )
    assert wait_until(lambda: session.state() is LinkState.FAILED, timeout=5)
    assert session.failure_key() == "tools.fail.network"


# ------------------------------------------------------------------ commands


def test_host_command_scans_and_then_ready(fake, session_factory) -> None:
    core = FakeCore(fake.port)
    session = session_factory(fake, core=core)
    assert wait_until(lambda: session.state() is LinkState.IDLE)
    session.host("instance")
    assert session.state() is LinkState.HOST_WAITING
    assert fake.wait_for_request("/state/scanning")
    assert wait_until(lambda: fake.state_snapshot().get("state") == "host-scanning")
    assert core.ensure_calls == 1
    assert wait_until(lambda: session.phase() == "host-scanning")

    fake.set_state(
        "host-ok",
        room=ROOM,
        profiles=[{"name": "Steve", "kind": "HOST"}],
    )
    assert wait_until(lambda: session.state() is LinkState.HOST_READY)


def test_join_is_rejected_with_a_clear_failure(fake, session_factory) -> None:
    core = FakeCore(fake.port)
    session = session_factory(fake, core=core)
    assert wait_until(lambda: session.state() is LinkState.IDLE)
    fake.guest_reject = True
    session.join(ROOM)
    assert wait_until(lambda: session.state() is LinkState.FAILED)
    assert session.failure_key() == "tools.fail.bad_code"


def test_close_room_returns_to_waiting_but_keeps_the_process(fake, session_factory) -> None:
    fake.set_state("host-ok", index=2, room=ROOM, profiles=[])
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.HOST_READY)
    fake.reset_requests()
    session.close_room()
    assert session.state() is LinkState.IDLE
    assert session.room_code() == ""
    assert fake.wait_for_request("/state/ide")
    assert wait_until(lambda: fake.state_snapshot().get("state") == "waiting")
    # The session is still observing: closing a room is not closing the multiplayer feature.
    assert session.is_polling()


def test_cancel_returns_to_idle_and_resets(fake, session_factory) -> None:
    session = session_factory(fake)
    assert wait_until(lambda: session.state() is LinkState.IDLE)
    session.join(ROOM)
    assert wait_until(lambda: session.state() is LinkState.CONNECTING)
    session.cancel()
    assert session.state() is LinkState.IDLE
    assert session.room_code() == ""
    assert fake.wait_for_request("/state/ide")


# ------------------------------------------------------------------ lifecycle / component


def test_component_missing_never_starts_a_process(fake) -> None:
    core = FakeCore(None)
    session = _make_session(fake, component=False, core=core)
    try:
        assert session.state() is LinkState.NOT_DOWNLOADED
        session.host("instance")
        session.join(ROOM)
        assert session.state() is LinkState.NOT_DOWNLOADED
        assert core.ensure_calls == 0
        assert not session.is_polling()
    finally:
        session.dispose(wait=True)


def test_download_core_reports_bytes_and_moves_to_idle() -> None:
    seen: list[tuple[int, int, int]] = []

    def installer(progress):
        progress(0, 1000)
        progress(500, 1000)
        progress(1000, 1000)
        return component_ready()

    session = TerracottaLinkSession(
        process=FakeCore(None),
        component_status_fn=component_missing,
        installer=installer,
        stun_probe=lambda: StunVerdict.CONE,
        player_name=lambda: "",
    )
    try:
        session.download_core(
            lambda value: seen.append((value.permille, value.done_bytes, value.total_bytes))
        )
        assert wait_until(lambda: session.state() is LinkState.IDLE)
        assert seen == [(0, 0, 1000), (500, 500, 1000), (1000, 1000, 1000)]
    finally:
        session.dispose(wait=True)


def test_download_failure_keeps_not_downloaded_with_a_key() -> None:
    def installer(_progress):
        raise ComponentError("tools.fail.verify")

    session = TerracottaLinkSession(
        process=FakeCore(None),
        component_status_fn=component_missing,
        installer=installer,
        stun_probe=lambda: StunVerdict.CONE,
        player_name=lambda: "",
    )
    try:
        session.download_core(lambda _value: None)
        assert wait_until(lambda: session.failure_key() == "tools.fail.verify")
        assert session.state() is LinkState.NOT_DOWNLOADED
    finally:
        session.dispose(wait=True)


def test_dispose_stops_the_polling_thread(fake) -> None:
    session = _make_session(fake)
    assert wait_until(lambda: session.state() is LinkState.IDLE)
    assert session.is_polling()
    session.dispose(wait=True)
    assert not session.is_polling()
    requests = len(fake.requests("/state"))
    time.sleep(0.15)
    assert len(fake.requests("/state")) == requests


def test_self_check_reports_component_process_and_stun(fake) -> None:
    core = FakeCore(fake.port)
    session = _make_session(fake, core=core, stun_probe=lambda: StunVerdict.CONE)
    try:
        report = session.self_check()
        assert report.component.downloaded
        assert report.process == ProcessStatus(running=True, port=fake.port)
        assert report.stun is StunVerdict.CONE
        assert not report.nat.tested
        assert session.process_status() == report.process
    finally:
        session.dispose(wait=True)


def test_self_check_reports_easytier_udp_and_tcp_types(fake) -> None:
    nat = NatProbe(udp=NatType.SYMMETRIC_EASY, tcp=NatType.RESTRICTED_CONE)
    session = _make_session(
        fake,
        nat_probe=lambda: nat,
        stun_probe=lambda: pytest.fail("the built-in probe must not run when EasyTier answered"),
    )
    try:
        report = session.self_check()
        assert report.nat == nat
        assert report.stun is StunVerdict.SYMMETRIC
    finally:
        session.dispose(wait=True)


def test_self_check_falls_back_to_builtin_udp_when_easytier_fails(fake) -> None:
    calls: list[str] = []

    def fail() -> NatProbe:
        raise TimeoutError("cli hung")

    def fallback() -> StunVerdict:
        calls.append("udp")
        return StunVerdict.CONE

    session = _make_session(fake, nat_probe=fail, stun_probe=fallback)
    try:
        report = session.self_check()
        assert calls == ["udp"]
        assert report.nat.error is NatProbeError.OS
        assert report.stun is StunVerdict.CONE
    finally:
        session.dispose(wait=True)


# ------------------------------------------------------------------ process


def test_port_file_timeout_terminates_the_child(ws_tmp) -> None:
    executable = ws_tmp / "terracotta.exe"
    executable.write_bytes(b"stub")
    child = DummyPopen()
    process = TerracottaProcess(
        executable=executable,
        port_file=ws_tmp / "port.json",
        lock_path=ws_tmp / "none.lock",
        wait_timeout=0.2,
        popen=lambda *args, **kwargs: child,
    )
    with pytest.raises(TerracottaError) as raised:
        process.ensure_running()
    assert raised.value.key == "tools.fail.start_timeout"
    assert child.terminated


def test_existing_instance_is_reused_without_a_launch(ws_tmp, fake) -> None:
    lock = ws_tmp / "terracotta.lock"
    lock.write_bytes(fake.port.to_bytes(2, "big"))
    assert read_lock_port(lock) == fake.port

    def fail(*_args, **_kwargs):
        raise AssertionError("a live instance must be reused, not relaunched")

    process = TerracottaProcess(
        executable=ws_tmp / "terracotta.exe",
        lock_path=lock,
        popen=fail,
    )
    assert process.ensure_running() == fake.port


def test_launch_waits_for_the_port_file_and_checks_meta(ws_tmp, fake) -> None:
    executable = ws_tmp / "terracotta.exe"
    executable.write_bytes(b"stub")
    captured: dict = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[2]).write_text(json.dumps({"port": fake.port}), encoding="utf-8")
        return DummyPopen()

    process = TerracottaProcess(
        executable=executable,
        port_file=ws_tmp / "hmcl.json",
        lock_path=ws_tmp / "none.lock",
        wait_timeout=2.0,
        popen=popen,
    )
    assert process.ensure_running() == fake.port
    assert captured["command"][1] == "--hmcl"
    assert captured["command"][2] == str(ws_tmp / "hmcl.json")
    if __import__("os").name == "nt":
        flags = captured["kwargs"]["creationflags"]
        assert flags & 0x08000000  # CREATE_NO_WINDOW
        assert flags & 0x00000008  # DETACHED_PROCESS
        assert flags & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["kwargs"]["start_new_session"] is True
