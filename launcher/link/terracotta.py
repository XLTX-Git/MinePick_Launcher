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

"""The real Terracotta | 陶瓦联机 backend: subprocess, HTTP client and state polling.

Terracotta runs as one local HTTP service on ``127.0.0.1`` and writes its port into a JSON file
when launched with ``--hmcl <file>``. This module owns the three pieces the UI contract needs:

* :class:`TerracottaClient` - the four GET endpoints the launcher uses (``/meta``, ``/state``,
  ``/state/ide``, ``/state/scanning`` and ``/state/guesting``);
* :class:`TerracottaProcess` - start-or-attach. The lock file in ``%TEMP%\\terracotta`` is how
  Terracotta itself shares one instance, so a running service is reused and nothing is launched.
  A launcher-started process is detached on purpose: closing the launcher must not close the room;
* :class:`TerracottaLinkSession` - the :class:`~launcher.link.session.LinkSession` implementation.
  It polls ``/state`` every 500 ms, ignores an ``index`` that did not grow, maps the state machine
  onto :class:`~launcher.link.session.LinkState`, and turns every failure into a translatable key.

``close_room`` versus ``cancel``: Terracotta has no "close room" endpoint. Closing the room is
therefore ``GET /state/ide`` - the host stops advertising and the guest link is torn down - while
the *process* stays alive. It is deliberately not a process kill: the service is shared (another
launcher or HMCL may be attached to the same instance), and closing a room is not a request to
stop the multiplayer feature. Leaving a room and cancelling a pending attempt both need the same
reset; they differ in intent, in what they clear locally, and in which states they are offered.

Everything here is Qt-free so the session can notify listeners from its worker threads. The page
turns those notifications into a queued Qt signal.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from launcher import paths
from launcher.link.components import (
    ComponentError,
    component_status,
    executable_path,
    install,
)
from launcher.link.components import remove as remove_component
from launcher.link.easytier import probe_nat_types
from launcher.link.mirrors import configured_key
from launcher.link.selfcheck import (
    ComponentStatus,
    NatProbe,
    NatProbeError,
    NatType,
    ProcessStatus,
    SelfCheckReport,
)
from launcher.link.session import (
    LinkDifficulty,
    LinkDownloadProgress,
    LinkMember,
    LinkState,
    Listener,
    ProgressCallback,
)
from launcher.link.skins import normalize_uuid
from launcher.link.stun import StunVerdict, probe_udp_traversal

logger = logging.getLogger(__name__)

#: ``GET /state`` is cheap; the documentation recommends about half a second.
DEFAULT_POLL_INTERVAL = 0.5
#: How often an idle session looks for a Terracotta instance it could attach to.
DEFAULT_ATTACH_INTERVAL = 1.0
#: ``--hmcl`` writes its port file within this many seconds, or the bootstrapper gives up.
PORT_FILE_TIMEOUT = 8.0
#: One HTTP request against a loopback service; generous enough for a loaded machine, short enough
#: that a dead service is recognised rather than waited on.
HTTP_TIMEOUT = 2.0


def _stun_verdict_for(nat_type: NatType | None) -> StunVerdict:
    """Keep the old single-verdict contract usable when only an EasyTier type is available."""
    if nat_type is None:
        return StunVerdict.UNKNOWN
    if nat_type is NatType.BLOCKED:
        return StunVerdict.BLOCKED
    if nat_type in (NatType.OPEN_INTERNET, NatType.NO_NAT, NatType.FULL_CONE):
        return StunVerdict.CONE
    return StunVerdict.SYMMETRIC


def _install_with_saved_mirror(progress: ProgressCallback) -> ComponentStatus:
    """Install the payload, preferring the mirror saved on 工具 › 联机 (``""`` = the race).

    Read at download time, not at construction time: the session is created once at startup and the
    user may change the channel before pressing download. A choice made after the payload was already
    installed is therefore never a reason to download it again - :func:`launcher.link.components.install`
    returns the existing installation before it looks at any mirror.
    """
    return install(progress, preferred=configured_key())


LOCK_DIRNAME = "terracotta"
LOCK_FILENAME = "terracotta.lock"
PORT_FILE_DIRNAME = Path("cache") / "link"

#: Terracotta's exception types (v0.4.2 documentation) mapped to their translation keys.
EXCEPTION_KEYS = {
    0: "tools.fail.0",
    1: "tools.fail.1",
    2: "tools.fail.2",
    3: "tools.fail.3",
    4: "tools.fail.4",
    5: "tools.fail.5",
}

#: ``guest-starting`` difficulty tokens; ``UNKNOWN`` means "no estimate yet", not "easy".
DIFFICULTY_BY_TOKEN = {
    "EASIEST": LinkDifficulty.EASY,
    "SIMPLE": LinkDifficulty.EASY,
    "MEDIUM": LinkDifficulty.MEDIUM,
    "TOUGH": LinkDifficulty.TOUGH,
}

#: One process-wide launch mutex: two sessions must not both start a bootstrapper.
_LAUNCH_LOCK = threading.Lock()


class TerracottaError(Exception):
    """A backend failure with a translation key the page can display."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


class TerracottaClient:
    """The loopback HTTP surface of one Terracotta instance. All endpoints are GET."""

    def __init__(self, port: int, *, timeout: float = HTTP_TIMEOUT) -> None:
        self.port = int(port)
        self._client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "MinePick-Launcher"},
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.debug("关闭联机核心 HTTP 客户端失败", exc_info=True)

    def _request(self, path: str, params: dict[str, object] | None = None) -> httpx.Response:
        try:
            response = self._client.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise TerracottaError("tools.fail.network", str(exc)) from exc
        if response.status_code == 400:
            raise TerracottaError("tools.fail.bad_code", "HTTP 400")
        if response.status_code >= 400:
            raise TerracottaError("tools.fail.network", f"HTTP {response.status_code}")
        return response

    def _get(self, path: str, params: dict[str, object] | None = None) -> dict:
        response = self._request(path, params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TerracottaError("tools.fail.network", "invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TerracottaError("tools.fail.network", "unexpected JSON payload")
        return payload

    def _command(self, path: str, params: dict[str, object] | None = None) -> None:
        """A control endpoint: status is the answer, and its body (often empty) is not parsed."""
        self._request(path, params)

    def meta(self) -> dict:
        return self._get("/meta")

    def state(self) -> dict:
        return self._get("/state")

    def reset(self) -> None:
        """``GET /state/ide``: back to ``waiting``, which also clears an exception."""
        self._command("/state/ide")

    def start_hosting(self, *, player: str = "", room: str = "") -> None:
        params: dict[str, object] = {}
        if player:
            params["player"] = player
        if room:
            params["room"] = room
        self._command("/state/scanning", params or None)

    def start_guest(self, room: str, *, player: str = "") -> None:
        params: dict[str, object] = {"room": room}
        if player:
            params["player"] = player
        self._command("/state/guesting", params)


def terracotta_lock_path() -> Path:
    """``%TEMP%\\terracotta\\terracotta.lock`` (``/tmp/terracotta/...`` elsewhere)."""
    return Path(tempfile.gettempdir()) / LOCK_DIRNAME / LOCK_FILENAME


def read_lock_port(path: Path | None = None) -> int | None:
    """The little port file Terracotta uses to advertise its shared instance, or ``None``."""
    target = path if path is not None else terracotta_lock_path()
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    if len(raw) != 2:
        return None
    port = int.from_bytes(raw, "big")
    return port if 1 <= port <= 65535 else None


def port_alive(port: int, *, timeout: float = 0.5) -> bool:
    """Whether an HTTP service answering ``/meta`` is listening on this loopback port."""
    client = TerracottaClient(port, timeout=timeout)
    try:
        client.meta()
        return True
    except TerracottaError:
        return False
    finally:
        client.close()


class TerracottaProcess:
    """Start one detached Terracotta bootstrapper, or attach to the shared instance already up."""

    def __init__(
        self,
        *,
        executable: Path | None = None,
        port_file: Path | None = None,
        lock_path: Path | None = None,
        wait_timeout: float = PORT_FILE_TIMEOUT,
        popen: Callable[..., object] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executable = Path(executable) if executable is not None else None
        self._port_file = Path(port_file) if port_file is not None else None
        self._lock_path = Path(lock_path) if lock_path is not None else None
        self._wait_timeout = wait_timeout
        self._popen = popen
        self._sleep = sleep
        self._clock = clock
        self._port: int | None = None

    def _lock(self) -> Path:
        return self._lock_path if self._lock_path is not None else terracotta_lock_path()

    def _hmcl_port_file(self) -> Path:
        if self._port_file is not None:
            return self._port_file
        return paths.launcher_dir() / PORT_FILE_DIRNAME / f"terracotta-port-{os.getpid()}.json"

    def _binary(self) -> Path | None:
        if self._executable is not None:
            return self._executable
        return executable_path()

    def existing_port(self) -> int | None:
        """The shared instance's port when its lock file names a service that answers, else None."""
        port = read_lock_port(self._lock())
        if port is None:
            return None
        if port_alive(port):
            self._port = port
            return port
        return None

    def ensure_running(self) -> int:
        """Return a live port, reusing the shared instance or launching a detached bootstrapper."""
        with _LAUNCH_LOCK:
            existing = self.existing_port()
            if existing is not None:
                return existing
            binary = self._binary()
            if binary is None or not Path(binary).is_file():
                raise TerracottaError("tools.fail.download", "terracotta executable is missing")
            port_file = self._hmcl_port_file()
            try:
                port_file.parent.mkdir(parents=True, exist_ok=True)
                port_file.unlink(missing_ok=True)
            except OSError as exc:
                raise TerracottaError("tools.fail.start_timeout", str(exc)) from exc
            command = [str(binary), "--hmcl", str(port_file)]
            kwargs: dict[str, object] = {
                "cwd": str(Path(binary).parent),
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
            }
            if os.name == "nt":
                # The room must outlive the launcher: no console, no inherited process group.
                flags = 0
                for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
                    flags |= int(getattr(subprocess, name, 0))
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            try:
                process = self._popen(command, **kwargs)
            except OSError as exc:
                raise TerracottaError("tools.fail.start_timeout", str(exc)) from exc
            deadline = self._clock() + self._wait_timeout
            port: int | None = None
            while self._clock() < deadline:
                port = _read_port_json(port_file)
                if port is not None and port_alive(port, timeout=0.5):
                    break
                self._sleep(0.05)
            else:
                port = None
            if port is None:
                _terminate(process)
                port_file.unlink(missing_ok=True)
                raise TerracottaError("tools.fail.start_timeout", "port file did not appear")
            self._port = port
            port_file.unlink(missing_ok=True)
            return port

    def status(self) -> ProcessStatus:
        """A verified status (one ``/meta`` request); called from the self-check worker."""
        port = self.existing_port()
        return ProcessStatus(running=port is not None, port=port)


def _read_port_json(path: Path) -> int | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    port = raw.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    return port if 1 <= port <= 65535 else None


def _terminate(process: object) -> None:
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except OSError:
            pass


class TerracottaLinkSession:
    """A :class:`LinkSession` backed by the local Terracotta service."""

    def __init__(
        self,
        *,
        process: TerracottaProcess | None = None,
        client_factory: Callable[[int], TerracottaClient] = TerracottaClient,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        attach_interval: float = DEFAULT_ATTACH_INTERVAL,
        component_status_fn: Callable[[], ComponentStatus] = component_status,
        installer: Callable[[ProgressCallback], ComponentStatus] | None = None,
        stun_probe: Callable[[], StunVerdict] = probe_udp_traversal,
        nat_probe: Callable[[], NatProbe] = probe_nat_types,
        player_name: Callable[[], str] | None = None,
    ) -> None:
        self._process = process if process is not None else TerracottaProcess()
        self._client_factory = client_factory
        self._poll_interval = max(0.01, float(poll_interval))
        self._attach_interval = max(0.01, float(attach_interval))
        self._component_status_fn = component_status_fn
        self._installer = installer or _install_with_saved_mirror
        self._stun_probe = stun_probe
        self._nat_probe = nat_probe
        self._player_name = player_name or _selected_player_name

        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._state = LinkState.IDLE
        self._room_code = ""
        self._members: tuple[LinkMember, ...] = ()
        self._difficulty: LinkDifficulty | None = None
        self._address = ""
        self._failure_key = ""
        self._phase = ""
        self._instance_id = ""
        self._index = -1  # Terracotta starts its counter at 0; -1 accepts the first answer
        self._port = 0
        self._client: TerracottaClient | None = None
        self._status_cache = ProcessStatus(False, None)
        self._component = self._component_status_fn()
        self._disposed = False
        self._poll_enabled = False
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._command_thread: threading.Thread | None = None
        self._download_thread: threading.Thread | None = None
        self._generation = 0

        if not self._component.downloaded:
            self._state = LinkState.NOT_DOWNLOADED
        else:
            self._start_polling()

    # -- observable state -------------------------------------------------
    def state(self) -> LinkState:
        return self._state

    def room_code(self) -> str:
        return self._room_code

    def members(self) -> tuple[LinkMember, ...]:
        return self._members

    def difficulty(self) -> LinkDifficulty | None:
        return self._difficulty

    def address(self) -> str:
        return self._address

    def failure_key(self) -> str:
        return self._failure_key

    def phase(self) -> str:
        """The raw Terracotta state name (``host-starting`` distinguishes the waiting title)."""
        return self._phase

    def instance_id(self) -> str:
        return self._instance_id

    def component_status(self) -> ComponentStatus:
        """The component state, re-read from disk (cheap; called by the self-check card)."""
        return self._component_status_fn()

    def process_status(self) -> ProcessStatus:
        """The cached process state (no I/O: the card may read it on the GUI thread)."""
        return self._status_cache

    def self_check(self) -> SelfCheckReport:
        """Component state plus live UDP/TCP NAT types. Run this off the GUI thread."""
        component = self._component_status_fn()
        try:
            process = self._process.status()
        except Exception:
            logger.debug("联机核心进程状态探测失败", exc_info=True)
            process = ProcessStatus(False, None)
        with self._lock:
            self._component = component
            self._status_cache = process
        try:
            nat = self._nat_probe()
            if not isinstance(nat, NatProbe):
                raise TypeError("nat probe returned no NatProbe")
        except Exception as exc:
            logger.debug("EasyTier NAT 探测失败", exc_info=True)
            nat = NatProbe(error=NatProbeError.OS, detail=str(exc)[:200])
        if nat.tested:
            stun = _stun_verdict_for(nat.udp)
        else:
            try:
                stun = self._stun_probe()
            except Exception:
                logger.debug("UDP 打洞探测失败", exc_info=True)
                stun = StunVerdict.ERROR
        return SelfCheckReport(component=component, process=process, stun=stun, nat=nat)

    # -- notification -----------------------------------------------------
    def add_listener(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            if not self._disposed:
                self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def _notify(self) -> None:
        if self._disposed:
            return
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.debug("联机会话监听器异常", exc_info=True)

    # -- polling ----------------------------------------------------------
    def _start_polling(self) -> None:
        with self._lock:
            if self._disposed:
                return
            self._poll_enabled = True
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="terracotta-link-poll",
                daemon=True,
            )
            # Started while the lock is held: a concurrent dispose() must never see a created but
            # not-yet-started thread, because joining that raises RuntimeError.
            self._poll_thread.start()

    def _restart_polling(self) -> None:
        with self._lock:
            self._poll_enabled = True
        self._start_polling()

    def is_polling(self) -> bool:
        """Whether the polling thread is alive (test/introspection helper)."""
        thread = self._poll_thread
        return thread is not None and thread.is_alive()

    def observe_running_instance(self) -> bool:
        """Poll an already running service even when the component check says the payload is missing.

        The caller has just verified through the lock file that a Terracotta service answers
        ``/meta`` (see :class:`~launcher.link.bus.LinkSessionBus`). That is stronger evidence than
        the payload check on disk: the executable can have been moved (or ``installed.json`` removed)
        while the detached process kept the room. Returns whether polling was started here.
        """
        with self._lock:
            if self._disposed or self._component.downloaded:
                return False
            self._component = self._component_status_fn()
            self._state = LinkState.IDLE
            self._phase = ""
            self._failure_key = ""
        self._start_polling()
        return True

    def _poll_loop(self) -> None:
        while not self._stop.is_set() and not self._disposed:
            with self._lock:
                if not self._poll_enabled:
                    enabled = False
                else:
                    enabled = True
            if not enabled:
                self._stop.wait(0.2)
                continue
            client = self._client
            if client is None:
                try:
                    port = self._process.existing_port()
                except Exception:
                    logger.debug("联机核心实例探测失败", exc_info=True)
                    port = None
                if not port:
                    self._stop.wait(self._attach_interval)
                    continue
                try:
                    client = self._client_for(port)
                except Exception:
                    logger.debug("无法连接联机核心端口 %s", port, exc_info=True)
                    self._stop.wait(self._attach_interval)
                    continue
            try:
                with self._io_lock:
                    raw = client.state()
            except TerracottaError as exc:
                self._fail(exc.key)
                break
            except Exception:
                logger.debug("轮询联机状态失败", exc_info=True)
                self._fail("tools.fail.network")
                break
            try:
                stop = self._consume_state(raw, client)
            except TerracottaError as exc:
                self._fail(exc.key)
                break
            if stop:
                break
            self._stop.wait(self._poll_interval)

    def _client_for(self, port: int) -> TerracottaClient:
        with self._io_lock:
            with self._lock:
                if self._client is not None and self._port == port:
                    return self._client
                previous = self._client
                self._client = self._client_factory(port)
                self._port = int(port)
                self._index = -1  # a new instance restarts the index; -1 accepts index 0 again
                self._status_cache = ProcessStatus(running=True, port=int(port))
            if previous is not None:
                previous.close()
            return self._client

    def _consume_state(self, raw: dict, client: TerracottaClient) -> bool:
        """Apply one ``/state`` answer; return True when polling must stop (exception)."""
        index = raw.get("index")
        name = raw.get("state")
        if isinstance(index, bool) or not isinstance(index, int) or not isinstance(name, str):
            raise TerracottaError("tools.fail.network", "malformed /state response")
        with self._lock:
            # A command failure has to stay on screen until the user retries. The poller is
            # stopped by _fail(), but one response can already be in flight; without this latch
            # it would overwrite the failure with the stale "waiting" the command was sent from.
            if self._state is LinkState.FAILED and self._failure_key:
                return False
            if index <= self._index:
                return False  # stale or repeated answer: the documented ignore rule
            self._index = index
        if name == "waiting":
            self._apply(
                LinkState.IDLE,
                phase=name,
                room_code="",
                members=(),
                difficulty=None,
                address="",
                failure_key="",
            )
        elif name == "host-scanning":
            self._apply(LinkState.HOST_WAITING, phase=name)
        elif name == "host-starting":
            self._apply(
                LinkState.HOST_WAITING,
                phase=name,
                room_code=str(raw.get("room") or ""),
            )
        elif name == "host-ok":
            self._apply(
                LinkState.HOST_READY,
                phase=name,
                room_code=str(raw.get("room") or ""),
                members=_profiles_to_members(raw.get("profiles")),
                difficulty=None,
                address="",
                failure_key="",
            )
        elif name == "guest-connecting":
            self._apply(
                LinkState.CONNECTING,
                phase=name,
                room_code=str(raw.get("room") or ""),
                difficulty=None,
                failure_key="",
            )
        elif name == "guest-starting":
            self._apply(
                LinkState.CONNECTING,
                phase=name,
                room_code=str(raw.get("room") or ""),
                difficulty=DIFFICULTY_BY_TOKEN.get(str(raw.get("difficulty") or "").upper()),
                failure_key="",
            )
        elif name == "guest-ok":
            self._apply(
                LinkState.JOINED,
                phase=name,
                members=_profiles_to_members(raw.get("profiles")),
                address=str(raw.get("url") or ""),
                difficulty=None,
                failure_key="",
            )
        elif name == "exception":
            kind = raw.get("type")
            key = EXCEPTION_KEYS.get(kind, "tools.fail.5") if isinstance(kind, int) else "tools.fail.5"
            self._fail(key)
            # The state machine only leaves `exception` through /state/ide. Reset immediately so a
            # later retry starts from `waiting`; the poller stays stopped until the user acts.
            try:
                client.reset()
            except TerracottaError:
                logger.debug("异常状态复位失败", exc_info=True)
            return True
        else:
            raise TerracottaError("tools.fail.network", f"unknown state {name!r}")
        return False

    def _apply(self, state: LinkState, **fields: object) -> None:
        with self._lock:
            if self._disposed:
                return
            self._state = state
            for key, value in fields.items():
                setattr(self, f"_{key}", value)
        self._notify()

    def _fail(self, key: str) -> None:
        with self._lock:
            if self._disposed:
                return
            self._state = LinkState.FAILED
            self._failure_key = key
            self._poll_enabled = False
            self._status_cache = ProcessStatus(False, None)
        self._notify()

    # -- user actions -----------------------------------------------------
    def join(self, code: str) -> None:
        code = str(code or "").strip()
        with self._lock:
            if self._disposed or self._state is not LinkState.IDLE:
                return
            if not self._component_status_fn().downloaded:
                self._component = self._component_status_fn()
                self._state = LinkState.NOT_DOWNLOADED
                missing = True
            else:
                missing = False
                self._generation += 1
                generation = self._generation
                self._state = LinkState.CONNECTING
                self._phase = "guest-connecting"
                self._room_code = code
                self._members = ()
                self._difficulty = None
                self._address = ""
                self._failure_key = ""
        if missing:
            self._notify()
            return
        if not code:
            self._fail("tools.fail.bad_code")
            return
        self._notify()
        self._restart_polling()
        self._run_command("guest", code, generation)

    def host(self, instance_id: str) -> None:
        instance_id = str(instance_id or "").strip()
        with self._lock:
            if self._disposed or self._state is not LinkState.IDLE:
                return
            if not self._component_status_fn().downloaded:
                self._component = self._component_status_fn()
                self._state = LinkState.NOT_DOWNLOADED
                missing = True
            else:
                missing = False
                self._generation += 1
                generation = self._generation
                self._instance_id = instance_id
                self._state = LinkState.HOST_WAITING
                self._phase = "host-scanning"
            self._room_code = ""
            self._members = ()
            self._difficulty = None
            self._address = ""
            self._failure_key = ""
        if missing:
            self._notify()
            return
        self._notify()
        self._restart_polling()
        self._run_command("host", "", generation)

    def cancel(self) -> None:
        """Stop this attempt and return to ``IDLE``. The shared process keeps running."""
        self._leave_remote("cancel")

    def close_room(self) -> None:
        """Close the room itself (``/state/ide``); the shared process keeps running."""
        self._leave_remote("close")

    def _leave_remote(self, reason: str) -> None:
        with self._lock:
            if self._disposed:
                return
            previous = self._state
            active = previous in (
                LinkState.HOST_WAITING,
                LinkState.HOST_READY,
                LinkState.CONNECTING,
                LinkState.JOINED,
                LinkState.FAILED,
            )
            self._generation += 1
            self._state = LinkState.IDLE
            self._phase = ""
            self._room_code = ""
            self._members = ()
            self._difficulty = None
            self._address = ""
            self._failure_key = ""
            self._instance_id = ""
        logger.debug("联机会话离开远端状态: %s (%s)", previous.value, reason)
        if active:
            self._reset_remote()
        self._restart_polling()
        self._notify()

    def remove_core(self) -> int:
        """Delete the downloaded payload and return its size in bytes.

        ``close_room`` is the caller's step (the page does it first), because closing a room and
        deleting a component are two different promises. The component state is re-read from disk
        instead of being assumed: only a removal that succeeded moves the session to
        ``NOT_DOWNLOADED``. A running core keeps its executable open on Windows, so a failed removal
        raises :class:`~launcher.link.components.ComponentError` with the OS detail and leaves the
        state as it was - the page reports the reason and nothing pretends the core is gone.
        """
        with self._lock:
            if self._disposed:
                return 0
        try:
            removed = remove_component()
        except ComponentError:
            with self._lock:
                self._component = self._component_status_fn()
            raise
        with self._lock:
            self._component = self._component_status_fn()
            self._state = LinkState.NOT_DOWNLOADED
            self._phase = ""
            self._room_code = ""
            self._members = ()
            self._difficulty = None
            self._address = ""
            self._failure_key = ""
            self._instance_id = ""
            # Nothing is installed any more, so there is nothing for the poller to report: a service
            # that outlived the deletion must not paint a room over the onboarding page.
            self._poll_enabled = False
        self._notify()
        return removed

    def download_core(self, progress_cb: ProgressCallback) -> None:
        """Install the payload on a worker thread; state moves to ``IDLE`` when it lands."""
        notify_ready = False
        with self._lock:
            if self._disposed:
                return
            if self._component.downloaded:
                self._state = LinkState.IDLE
                self._failure_key = ""
                notify_ready = True
            else:
                if self._download_thread is not None and self._download_thread.is_alive():
                    return
                self._failure_key = ""
                thread = threading.Thread(
                    target=self._download_worker,
                    args=(progress_cb,),
                    name="terracotta-link-download",
                    daemon=True,
                )
                self._download_thread = thread
        if notify_ready:
            self._notify()
            return
        thread.start()

    def _download_worker(self, progress_cb: ProgressCallback) -> None:
        def progress(done: int, total: int) -> None:
            if self._disposed:
                return
            try:
                progress_cb(LinkDownloadProgress.from_bytes(done, total))
            except Exception:
                logger.debug("下载进度回调失败", exc_info=True)

        try:
            status = self._installer(progress)
        except ComponentError as exc:
            logger.warning("联机核心安装失败: %s", exc.detail or exc.key)
            with self._lock:
                self._component = self._component_status_fn()
                self._state = LinkState.NOT_DOWNLOADED
                self._failure_key = exc.key
            self._notify()
        except Exception:
            logger.exception("联机核心安装异常")
            with self._lock:
                self._component = self._component_status_fn()
                self._state = LinkState.NOT_DOWNLOADED
                self._failure_key = "tools.fail.download"
            self._notify()
        else:
            with self._lock:
                self._component = status
                self._state = LinkState.IDLE
                self._failure_key = ""
            self._notify()
            # A room may already exist in a shared instance; start observing it now that the
            # component is installed (there was no poller while the payload was missing).
            self._start_polling()

    def _run_command(self, kind: str, code: str, generation: int) -> None:
        with self._lock:
            if self._command_thread is not None and self._command_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._command_worker,
                args=(kind, code, generation),
                name="terracotta-link-command",
                daemon=True,
            )
            self._command_thread = thread
        thread.start()

    def _command_worker(self, kind: str, code: str, generation: int) -> None:
        try:
            if self._disposed or generation != self._generation:
                return
            port = self._process.ensure_running()
            if self._disposed or generation != self._generation:
                return
            self._use_port(port)
            client = self._client
            if client is None:
                raise TerracottaError("tools.fail.network", "no client after launch")
            player = self._player_name()
            if kind == "host":
                client.start_hosting(player=player, room=code)
            else:
                client.start_guest(code, player=player)
        except TerracottaError as exc:
            if not self._disposed and generation == self._generation:
                self._fail(exc.key)
        except Exception:
            logger.exception("联机命令失败")
            if not self._disposed and generation == self._generation:
                self._fail("tools.fail.network")
        finally:
            with self._lock:
                if self._command_thread is threading.current_thread():
                    self._command_thread = None

    def _use_port(self, port: int) -> None:
        self._client_for(port)
        self._restart_polling()

    def _reset_remote(self) -> None:
        """Send ``/state/ide`` from a daemon thread; never blocks the page."""
        thread = threading.Thread(
            target=self._reset_remote_worker,
            name="terracotta-link-reset",
            daemon=True,
        )
        thread.start()

    def _reset_remote_worker(self) -> None:
        try:
            client = self._client
            if client is None:
                port = self._process.existing_port()
                if port is None:
                    return
                client = self._client_for(port)
            with self._io_lock:
                client.reset()
        except TerracottaError:
            logger.debug("复位联机核心状态失败", exc_info=True)
        except Exception:
            logger.debug("复位联机核心状态异常", exc_info=True)

    # -- lifecycle --------------------------------------------------------
    def dispose(self, *, wait: bool = False) -> None:
        """Stop polling and release the endpoint. The detached process (and its room) stays."""
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            self._poll_enabled = False
            self._stop.set()
            thread = self._poll_thread
            client = self._client
            self._client = None
        if client is not None:
            client.close()
        if wait and thread is not None and thread.ident is not None:
            thread.join(timeout=2.0)


def _profiles_to_members(profiles: object) -> tuple[LinkMember, ...]:
    """Convert Terracotta's ``profiles`` array into member rows.

    The v0.4.2 profile object is ``{machine_id, name, vendor, kind}`` - it carries **no UUID**, so
    a real room member has no skin lookup and keeps the letter avatar. ``uuid``/``premium`` are
    accepted when a backend (or the demo fixture) provides them, but are never invented.
    """
    if not isinstance(profiles, list):
        return ()
    members: list[LinkMember] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        name = str(profile.get("name") or "").strip()
        if not name:
            continue
        kind = str(profile.get("kind") or "").upper()
        members.append(
            LinkMember(
                name=name,
                is_host=kind == "HOST",
                online=True,
                uuid=normalize_uuid(str(profile.get("uuid") or "")),
                premium=bool(profile.get("premium", False)),
            )
        )
    return tuple(members)


def _selected_player_name() -> str:
    """The selected account's username, passed to Terracotta so the host row is not anonymous."""
    try:
        from launcher import config
        from launcher.auth import AccountStore

        cfg, _ = config.load()
        if not cfg.selected_account:
            return ""
        account = AccountStore().load().get(cfg.selected_account)
        return str(getattr(account, "username", "") or "").strip()
    except Exception:  # noqa: BLE001 - a missing account is simply an anonymous player
        return ""
