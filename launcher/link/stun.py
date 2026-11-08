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

"""A minimal STUN client (RFC 5389) for the 网络自检 card.

Terracotta builds its rooms on EasyTier, which has to punch a UDP hole through both peers'
NATs. The launcher cannot ask Terracotta "can this machine punch?", so the self-check answers
the question the player actually cares about with the smallest possible instrument: two STUN
binding requests from one local UDP port to two different public servers, comparing the
address each server saw.

Why the same local port for both requests: a NAT keeps one mapping per internal endpoint, and
a cone NAT reuses that mapping for every destination while a symmetric NAT allocates one per
destination. Sending the second request from a *different* local port would change the mapping
on every NAT - cone or not - and the test would report "symmetric" for everybody. The correct
test is therefore "same local socket, two destinations", which is what :func:`probe_udp_traversal`
does. Only UDP is tested; the panel says so, because a TCP-only proxy does not change UDP
behaviour while a UDP-relaying proxy invalidates the result.

The module is Qt-free and uses the standard library only: a binding request is a 20-byte header
plus a 12-byte transaction ID, and the XOR-MAPPED-ADDRESS parser is a couple of bit operations.
Every network failure degrades to a verdict rather than an exception, so the card always has
something truthful to show.
"""

from __future__ import annotations

import os
import socket
import struct
from collections.abc import Iterable, Sequence
from enum import Enum

#: Public STUN servers, tried in order. They answer a plain RFC 5389 binding request on UDP and
#: need no account. Both are needed for the cone/symmetric comparison: one destination each.
STUN_SERVERS: tuple[tuple[str, int], ...] = (
    ("stun.cloudflare.com", 3478),
    ("stun.l.google.com", 19302),
)

#: ``host:port[,host:port]`` overrides :data:`STUN_SERVERS` (a proxy setup or a local test server).
ENV_STUN_SERVERS = "MINEPICK_STUN_SERVERS"

#: How long one request waits for its answer. Two of these bound one full self-check.
DEFAULT_TIMEOUT = 2.0

_MAGIC_COOKIE = 0x2112A442
_BINDING_REQUEST = 0x0001
_BINDING_SUCCESS = 0x0101
_ATTR_MAPPED_ADDRESS = 0x0001
_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_HEADER = struct.Struct("!HHI12s")


class StunVerdict(str, Enum):
    """What the UDP probe concluded. ``UNKNOWN`` is the card's state before the first probe."""

    UNKNOWN = "unknown"
    CONE = "cone"
    SYMMETRIC = "symmetric"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    ERROR = "error"


def stun_servers() -> tuple[tuple[str, int], ...]:
    """The configured servers: :data:`STUN_SERVERS`, or the ``MINEPICK_STUN_SERVERS`` override."""
    raw = os.environ.get(ENV_STUN_SERVERS, "").strip()
    if not raw:
        return STUN_SERVERS
    servers: list[tuple[str, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        host, _sep, port = item.rpartition(":")
        if not host or not port.isdigit():
            continue
        servers.append((host, int(port)))
    return tuple(servers) or STUN_SERVERS


def build_binding_request(transaction_id: bytes | None = None) -> tuple[bytes, bytes]:
    """A 20-byte RFC 5389 binding request plus the transaction ID used to recognise its answer."""
    txid = transaction_id if transaction_id is not None else os.urandom(12)
    if len(txid) != 12:
        raise ValueError("transaction id must be 12 bytes")
    return _HEADER.pack(_BINDING_REQUEST, 0, _MAGIC_COOKIE, txid), txid


def parse_mapped_address(payload: bytes, transaction_id: bytes) -> tuple[str, int] | None:
    """The mapped ``(address, port)`` of a binding success response, or ``None``.

    ``None`` covers every malformed or unrelated datagram: a stray packet, a truncated header,
    a response to another transaction, or a server that only spoke the obsolete MAPPED-ADDRESS.
    The last one is accepted as a fallback because some old deployments still omit XOR encoding.
    """
    if len(payload) < 20:
        return None
    message_type, length, cookie, txid = _HEADER.unpack_from(payload)
    if message_type != _BINDING_SUCCESS or cookie != _MAGIC_COOKIE or txid != transaction_id:
        return None
    if length < 0 or 20 + length > len(payload):
        return None
    offset = 20
    end = 20 + length
    mapped: tuple[str, int] | None = None
    while offset + 4 <= end:
        attr_type, attr_len = struct.unpack_from("!HH", payload, offset)
        offset += 4
        if offset + attr_len > end:
            break
        value = payload[offset : offset + attr_len]
        if attr_type in (_ATTR_XOR_MAPPED_ADDRESS, _ATTR_MAPPED_ADDRESS):
            decoded = _decode_address(value, txid, xor=attr_type == _ATTR_XOR_MAPPED_ADDRESS)
            if decoded is not None:
                mapped = decoded
                if attr_type == _ATTR_XOR_MAPPED_ADDRESS:
                    return mapped  # the modern attribute wins over the legacy one
        offset += attr_len + ((4 - (attr_len % 4)) % 4)
    return mapped


def _decode_address(value: bytes, transaction_id: bytes, *, xor: bool) -> tuple[str, int] | None:
    """Decode a MAPPED-ADDRESS / XOR-MAPPED-ADDRESS attribute value."""
    if len(value) < 4:
        return None
    family = value[1]
    port = struct.unpack_from("!H", value, 2)[0]
    if xor:
        port ^= _MAGIC_COOKIE >> 16
    if family == 0x01:
        if len(value) < 8:
            return None
        raw = value[4:8]
        if xor:
            raw = bytes(a ^ b for a, b in zip(raw, struct.pack("!I", _MAGIC_COOKIE)))
        return socket.inet_ntop(socket.AF_INET, raw), port
    if family == 0x02:
        if len(value) < 20:
            return None
        raw = value[4:20]
        if xor:
            mask = struct.pack("!I", _MAGIC_COOKIE) + transaction_id
            raw = bytes(a ^ b for a, b in zip(raw, mask))
        return socket.inet_ntop(socket.AF_INET6, raw), port
    return None


def _resolve(server: tuple[str, int]) -> tuple[str, int] | None:
    """Resolve one server to an IPv4 address, or ``None`` when DNS itself failed."""
    host, port = server
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    if not infos:
        return None
    return infos[0][4][0], int(infos[0][4][1])


def _query(sock: socket.socket, server: tuple[str, int], timeout: float) -> tuple[str, int] | None:
    """Send one binding request and wait for its answer.

    Raises ``OSError`` when the network refuses the request (typically an unreachable route or an
    ICMP port-unreachable from a blocking middlebox); returns ``None`` on a plain timeout or when no
    valid answer arrives within ``timeout``.
    """
    request, txid = build_binding_request()
    sock.settimeout(timeout)
    sock.sendto(request, server)
    deadline_sock_timeout = timeout
    while True:
        try:
            payload, _peer = sock.recvfrom(2048)
        except TimeoutError:
            return None
        except OSError:
            raise
        mapped = parse_mapped_address(payload, txid)
        if mapped is not None:
            return mapped
        # A stray or truncated datagram: keep waiting, bounded by the socket's own timeout.
        deadline_sock_timeout = max(0.05, deadline_sock_timeout / 2)
        sock.settimeout(deadline_sock_timeout)


def probe_udp_traversal(
    servers: Sequence[tuple[str, int]] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    socket_factory=socket.socket,
) -> StunVerdict:
    """Classify the local NAT for UDP hole punching.

    One local UDP socket queries two destinations; equal mapped endpoints mean a cone NAT (the
    mapping is destination-independent, so a peer can reach it), different ones mean a symmetric
    NAT (a peer learns a port that no longer matches), and no answer at all means either a timeout
    (silent drop) or a blocked path (the network refused the request).
    """
    candidates = tuple(servers) if servers is not None else stun_servers()
    resolved = [endpoint for endpoint in (_resolve(server) for server in candidates) if endpoint]
    if len(resolved) < 2:
        return StunVerdict.ERROR
    first, second = resolved[0], resolved[1]
    sock = None
    try:
        try:
            sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
        except OSError:
            return StunVerdict.BLOCKED
        try:
            left = _query(sock, first, timeout)
        except OSError:
            return StunVerdict.BLOCKED
        if left is None:
            return StunVerdict.TIMEOUT
        try:
            right = _query(sock, second, timeout)
        except OSError:
            return StunVerdict.BLOCKED
        if right is None:
            return StunVerdict.TIMEOUT
        return StunVerdict.CONE if left == right else StunVerdict.SYMMETRIC
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def verdicts_for_display() -> Iterable[StunVerdict]:
    """The order the UI lists its verdict strings in (index into the translated block)."""
    return (
        StunVerdict.UNKNOWN,
        StunVerdict.CONE,
        StunVerdict.SYMMETRIC,
        StunVerdict.BLOCKED,
        StunVerdict.TIMEOUT,
        StunVerdict.ERROR,
    )
