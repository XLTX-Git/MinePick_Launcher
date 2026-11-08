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

"""The minimal STUN client: attribute parsing, the cone/symmetric comparison and the verdicts.

The fake servers are ordinary UDP sockets on loopback, so the production request bytes and parser
run unchanged. The cone/symmetric test sends both requests from one local socket on purpose; the
fake servers record the source port, which is what proves the comparison is destination-based
rather than the meaningless "different local ports" version.
"""

from __future__ import annotations

import socket
import struct

from launcher.link import stun
from launcher.link.stun import StunVerdict, parse_mapped_address, probe_udp_traversal
from tests.link_fakes import FakeStunServer, SilentUdpSocket, xor_mapped_response


def test_parse_xor_mapped_address() -> None:
    request, transaction_id = stun.build_binding_request()
    assert len(request) == 20
    response = xor_mapped_response(transaction_id, "203.0.113.7", 45678)
    assert parse_mapped_address(response, transaction_id) == ("203.0.113.7", 45678)


def test_parse_rejects_foreign_and_truncated_answers() -> None:
    _request, transaction_id = stun.build_binding_request()
    response = xor_mapped_response(transaction_id, "203.0.113.7", 45678)
    assert parse_mapped_address(response, b"\x00" * 12) is None
    assert parse_mapped_address(response[:16], transaction_id) is None
    assert parse_mapped_address(b"", transaction_id) is None
    # A success response whose announced attribute length runs past the datagram is not trusted.
    broken = bytearray(response)
    broken[2:4] = struct.pack("!H", 200)
    assert parse_mapped_address(bytes(broken), transaction_id) is None


def test_parse_accepts_the_legacy_mapped_address() -> None:
    _request, transaction_id = stun.build_binding_request()
    value = b"\x00\x01" + struct.pack("!H", 1234) + socket.inet_aton("198.51.100.9")
    attribute = struct.pack("!HH", 0x0001, len(value)) + value
    header = struct.pack("!HHI12s", 0x0101, len(attribute), 0x2112A442, transaction_id)
    assert parse_mapped_address(header + attribute, transaction_id) == ("198.51.100.9", 1234)


def test_cone_nat_reuses_one_mapping_for_two_destinations() -> None:
    left = FakeStunServer("203.0.113.7", 45678)
    right = FakeStunServer("203.0.113.7", 45678)
    try:
        verdict = probe_udp_traversal(
            servers=[left.endpoint, right.endpoint], timeout=0.5
        )
        assert verdict is StunVerdict.CONE
        assert len(left.peers) == 1 and len(right.peers) == 1
        # The same local socket queried both servers: one source port, two destinations.
        assert left.peers[0][1] == right.peers[0][1]
    finally:
        left.stop()
        right.stop()


def test_symmetric_nat_maps_the_same_local_port_differently() -> None:
    left = FakeStunServer("203.0.113.7", 45678)
    right = FakeStunServer("203.0.113.7", 45679)
    try:
        verdict = probe_udp_traversal(
            servers=[left.endpoint, right.endpoint], timeout=0.5
        )
        assert verdict is StunVerdict.SYMMETRIC
    finally:
        left.stop()
        right.stop()


def test_no_answer_is_a_timeout() -> None:
    left = SilentUdpSocket()
    right = SilentUdpSocket()
    try:
        verdict = probe_udp_traversal(
            servers=[left.endpoint, right.endpoint], timeout=0.2
        )
        assert verdict is StunVerdict.TIMEOUT
    finally:
        left.close()
        right.close()


def test_a_refused_request_is_blocked() -> None:
    class BrokenSocket:
        def bind(self, _address) -> None:
            return

        def settimeout(self, _timeout) -> None:
            return

        def sendto(self, _payload, _server) -> None:
            raise OSError("network unreachable")

        def close(self) -> None:
            return

    verdict = probe_udp_traversal(
        servers=[("127.0.0.1", 9), ("127.0.0.1", 10)],
        timeout=0.2,
        socket_factory=lambda *_args: BrokenSocket(),
    )
    assert verdict is StunVerdict.BLOCKED


def test_fewer_than_two_servers_is_an_error() -> None:
    assert probe_udp_traversal(servers=[("127.0.0.1", 9)], timeout=0.1) is StunVerdict.ERROR


def test_servers_can_be_configured_by_environment(monkeypatch) -> None:
    monkeypatch.setenv(stun.ENV_STUN_SERVERS, "127.0.0.1:3478, stun.example.test:19302")
    assert stun.stun_servers() == (
        ("127.0.0.1", 3478),
        ("stun.example.test", 19302),
    )
    monkeypatch.setenv(stun.ENV_STUN_SERVERS, "not-an-endpoint")
    assert stun.stun_servers() == stun.STUN_SERVERS
