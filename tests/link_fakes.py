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

"""Test doubles for the multiplayer backend: a local Terracotta HTTP service and download mirrors.

Terracotta's surface is five GET endpoints on loopback, so a `ThreadingHTTPServer` on
127.0.0.1:0 is the whole fake: the production client, the polling loop and the state mapping
run for real against it. The mirrors are ordinary HTTP servers with a configurable delay, which
is what lets the race test prove that the first answer wins rather than the first configured.
"""

from __future__ import annotations

import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from launcher.link.selfcheck import ComponentStatus, ProcessStatus


class FakeTerracotta:
    """A scripted Terracotta service: ``/meta``, ``/state`` and the three control endpoints."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._index = 1
        self._state: dict = {"state": "waiting", "index": 1}
        self._requests: list[tuple[str, dict[str, str]]] = []
        self.mode = "ok"  # ok | bad_json | http_500 | slow
        self.slow_seconds = 0.4
        self.guest_reject = False
        self.ide_empty = False
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TerracottaHandler)
        self._httpd.daemon_threads = True
        self._httpd.fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fake-terracotta", daemon=True
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)

    # -- scripting --------------------------------------------------------
    def set_state(self, name: str, *, index: int | None = None, **fields) -> None:
        with self._lock:
            self._index = self._index + 1 if index is None else int(index)
            self._state = {"state": name, "index": self._index, **fields}

    def state_snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def requests(self, path: str | None = None) -> list[tuple[str, dict[str, str]]]:
        with self._lock:
            if path is None:
                return list(self._requests)
            return [(item_path, query) for item_path, query in self._requests if item_path == path]

    def reset_requests(self) -> None:
        with self._lock:
            self._requests.clear()

    def wait_for_request(self, path: str, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.requests(path):
                return True
            time.sleep(0.01)
        return False

    # -- HTTP -------------------------------------------------------------
    def _record(self, path: str, query: dict[str, str]) -> None:
        with self._lock:
            self._requests.append((path, query))

    def handle(
        self, path: str, query: dict[str, str]
    ) -> tuple[int, dict | list, bytes | None]:
        if path == "/meta":
            return 200, {"version": "v0.4.2", "target_tuple": "x86_64-pc-windows-msvc"}, None
        if path == "/state":
            with self._lock:
                return 200, dict(self._state), None
        if path == "/state/ide":
            self.set_state("waiting")
            if self.ide_empty:
                return 200, {}, b""
            return 200, {}, None
        if path == "/state/scanning":
            with self._lock:
                if self._state.get("state") == "waiting":
                    self._index += 1
                    self._state = {"state": "host-scanning", "index": self._index}
                    payload = dict(self._state)
                else:
                    payload = dict(self._state)
            return 200, payload, None
        if path == "/state/guesting":
            room = query.get("room", "")
            with self._lock:
                if self.guest_reject or self._state.get("state") != "waiting" or not room:
                    return 400, {"error": "not waiting or bad room"}, None
                self._index += 1
                self._state = {
                    "state": "guest-connecting",
                    "index": self._index,
                    "room": room,
                }
                return 200, dict(self._state), None
        return 404, {"error": "not found"}, None


class _TerracottaHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the test output clean
        return

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        fake: FakeTerracotta = self.server.fake  # type: ignore[attr-defined]
        fake._record(parsed.path, dict(parse_qsl(parsed.query)))
        if fake.mode == "slow":
            time.sleep(fake.slow_seconds)
        if fake.mode == "http_500":
            self._send(500, b'{"error":"boom"}', "application/json")
            return
        if fake.mode == "bad_json":
            self._send(200, b"{definitely not json", "application/json")
            return
        status, payload, raw = fake.handle(parsed.path, dict(parse_qsl(parsed.query)))
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass  # the client closed a poll connection; nothing to report in a test fake


class FakeMirror:
    """An HTTP server that serves one payload (HEAD included) after an optional delay."""

    def __init__(self, payload: bytes = b"", *, delay: float = 0.0, status: int = 200) -> None:
        self.payload = payload
        self.delay = delay
        self.status = status
        self._lock = threading.Lock()
        self._requests: list[tuple[str, str]] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MirrorHandler)
        self._httpd.daemon_threads = True
        self._httpd.mirror = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fake-mirror", daemon=True
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/releases"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)

    def requests(self, method: str | None = None) -> int:
        with self._lock:
            if method is None:
                return len(self._requests)
            return sum(1 for item, _path in self._requests if item == method)


class _MirrorHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return

    def do_HEAD(self) -> None:
        self._respond(head=True)

    def do_GET(self) -> None:
        self._respond(head=False)

    def _respond(self, *, head: bool) -> None:
        mirror: FakeMirror = self.server.mirror  # type: ignore[attr-defined]
        if mirror.delay:
            time.sleep(mirror.delay)
        with mirror._lock:
            mirror._requests.append(("HEAD" if head else "GET", self.path))
        if mirror.status != 200:
            self.send_response(mirror.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(mirror.payload)))
        self.end_headers()
        if not head:
            self.wfile.write(mirror.payload)


class FakeCore:
    """Stand-in for :class:`launcher.link.terracotta.TerracottaProcess`."""

    def __init__(self, port: int | None = None) -> None:
        self.port = port
        self.ensure_calls = 0
        self.existing_calls = 0
        self.status_calls = 0
        self.running = port is not None

    def existing_port(self) -> int | None:
        self.existing_calls += 1
        return self.port if self.running else None

    def ensure_running(self) -> int:
        self.ensure_calls += 1
        if self.port is None:
            raise AssertionError("FakeCore has no port")
        self.running = True
        return self.port

    def status(self) -> ProcessStatus:
        self.status_calls += 1
        return ProcessStatus(running=self.running, port=self.port)


def component_ready(version: str = "0.4.2") -> ComponentStatus:
    return ComponentStatus(downloaded=True, version=version, supported=True)


def component_missing() -> ComponentStatus:
    return ComponentStatus(downloaded=False, supported=True)


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until it is true; returns False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class DummyPopen:
    """The minimum of ``subprocess.Popen`` that :class:`TerracottaProcess` touches."""

    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


def xor_mapped_response(transaction_id: bytes, address: str, port: int) -> bytes:
    """A minimal RFC 5389 binding success carrying XOR-MAPPED-ADDRESS."""
    import socket as _socket

    raw = _socket.inet_aton(address)
    cookie = struct.pack("!I", 0x2112A442)
    encoded_port = port ^ 0x2112
    encoded_ip = bytes(a ^ b for a, b in zip(raw, cookie))
    value = b"\x00\x01" + struct.pack("!H", encoded_port) + encoded_ip
    attribute = struct.pack("!HH", 0x0020, len(value)) + value
    header = struct.pack("!HHI12s", 0x0101, len(attribute), 0x2112A442, transaction_id)
    return header + attribute


class FakeStunServer:
    """A UDP socket that answers binding requests with a configured mapped endpoint."""

    def __init__(self, address: str, port: int) -> None:
        import socket

        self.address = address
        self.port = port
        self.peers: list[tuple[str, int]] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-stun", daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> tuple[str, int]:
        return "127.0.0.1", int(self._sock.getsockname()[1])

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                payload, peer = self._sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            if len(payload) < 20:
                continue
            self.peers.append(peer)
            transaction_id = payload[8:20]
            try:
                self._sock.sendto(
                    xor_mapped_response(transaction_id, self.address, self.port), peer
                )
            except OSError:
                return


class SilentUdpSocket:
    """A bound UDP socket that never answers (a timeout without ICMP)."""

    def __init__(self) -> None:
        import socket

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.1)

    @property
    def endpoint(self) -> tuple[str, int]:
        return "127.0.0.1", int(self._sock.getsockname()[1])

    def close(self) -> None:
        self._sock.close()
