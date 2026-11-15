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

"""Online identity for the content files an instance has on disk.

The instance detail lists four kinds of content - mods, resource packs, shader packs and (in the
future) modpacks - and until now every row was exactly what the file name said. That is precise and
completely unhelpful: a row reading ``sodium-fabric-0.6.5.jar`` says nothing about the project, its
tags, its icon, or whether a newer build exists. This module closes that gap with one lookup per file:

1. ask the disk cache (``<launcher data dir>/cache/content-identity/<kind>-<hash>.json``, 30 days);
2. on a miss, search Modrinth for the project type and *match* a hit by slug or title against the
   mod id / file name (case-insensitive, no fuzzy distance - a wrong icon is worse than none);
3. fetch that project's details for the title, summary, categories, icon URL and update date;
4. when the caller supplied the instance's game version (and, for a mod, its loader), fetch the
   newest version number for exactly that combination - that number is the only input of the
   "updatable" tab;
5. store the whole answer, including "nothing matched", so a launcher restart or an offline session
   is answered from disk and never waits on the network or fails a page.

The network is optional by design. :func:`lookup` returns ``None`` for every failure (a timeout, a
403, an unparsable answer, no match) and never raises into the caller, :func:`is_enabled` is the user's
master switch, and :class:`LookupQueue` is the one worker used by the interface: a FIFO thread that
keeps at least :data:`QUERY_INTERVAL` between two requests, coalesces duplicate keys, and can be
cancelled when the page is left or the instance is switched. Nothing here imports Qt, so the page can
own the widgets and this module can be measured against a fake HTTP transport.

Icons are *not* downloaded here. A project icon is stored by :mod:`launcher.net.http_cache` (the same
30-day store the resource page already uses); this module only reports the URL, and the page reads the
cache on the GUI thread and asks that module for a download when the bytes are missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from launcher import __version__, paths

LOGGER = logging.getLogger(__name__)

#: The Modrinth API the launcher already talks to (same host the mods page uses).
MODRINTH_API = "https://api.modrinth.com/v2"
#: Modrinth answers 403 to a request without a product User-Agent; this is the one the README asks
#: third-party clients to send, and it names the build so a response can be traced to a version.
USER_AGENT = f"MinePick-Launcher/{__version__} (github.com/XLTX-Git/MinePick_Launcher)"

#: Project types this module understands. ``modpack`` is here for the fifth list; the four lists the
#: instance detail shows today pass one of the first three.
KINDS = ("mod", "resourcepack", "shader", "modpack")

#: Cache layout: one JSON file per (kind, query, game version, loader), named by the kind plus a
#: hash. The directory lives under the launcher data directory, next to the existing ``mod-icons``.
CACHE_DIRNAME = "content-identity"
#: A project's metadata barely ever changes, so a month-old answer is still a good answer. The same
#: number is what keeps the feature from re-querying a server every time the page is opened.
CACHE_TTL = 30 * 24 * 3600.0
#: Modrinth answers in 0.8-2.1 s when it answers at all; three seconds is a page that degrades
#: silently instead of a list that looks broken.
REQUEST_TIMEOUT = 3.0
#: The floor between two queued lookups. Each lookup can issue up to three requests, so this is a
#: politeness setting, not a rate limit the server announced.
QUERY_INTERVAL = 0.2
#: Candidates fetched for one query; the match is exact, so five is already generous.
SEARCH_LIMIT = 5

@dataclass(frozen=True)
class Identity:
    """What a content file turned out to be, as far as Modrinth knows.

    ``latest_version`` is empty unless the caller supplied a game version: an update can only be
    judged against a concrete ``(game version, loader)`` pair, and guessing one would mark rows as
    updatable with a version the instance cannot run.
    """

    kind: str
    slug: str
    title: str
    description: str
    categories: tuple[str, ...] = ()
    icon_url: str = ""
    updated: str = ""
    latest_version: str = ""

    def is_updatable(self, installed_version: str) -> bool:
        """True when the project's newest version for this instance is not the installed one."""
        return versions_differ(installed_version, self.latest_version)


@dataclass(frozen=True)
class LookupRequest:
    """One queued lookup. ``key`` identifies the row and is what the page matches the answer to."""

    key: str
    name: str
    modid: str
    kind: str
    game_version: str = ""
    loader: str = ""
    #: The file on disk, when the caller has one. A path turns the lookup into a hash match first -
    #: the only identification that survives a rename and cannot hit the wrong project.
    path: str = ""


#: The master switch. It lives in this process (the settings page writes it from the user's choice);
#: when it is off no lookup may leave the machine and no queue item may run.
_enabled = True
_enabled_lock = threading.Lock()

#: QSettings key of the switch. It is a UI preference, not a launcher-config field: the only thing it
#: decides is whether a page may talk to Modrinth, and it is stored beside the column widths.
ONLINE_SETTING_KEY = "online/identity"


def set_enabled(value: bool) -> None:
    """Turn online recognition on or off for this process (the settings switch calls this)."""
    global _enabled
    with _enabled_lock:
        _enabled = bool(value)


def is_enabled() -> bool:
    """Whether online recognition is currently allowed to make a request."""
    with _enabled_lock:
        return _enabled


# ------------------------------------------------------------------------------------------------
# Version comparison
# ------------------------------------------------------------------------------------------------


def normalize_version(value: str) -> str:
    """``v1.2.3 `` -> ``1.2.3``: compare numbers, not the spelling a file name happened to use."""
    text = str(value or "").strip()
    while text[:1] in ("v", "V"):
        text = text[1:]
    return text.casefold()


def versions_differ(installed: str, latest: str) -> bool:
    """True when both are known and not the same version.

    An unknown installed version (a jar without metadata, a pack whose name carries no number) is
    never "updatable": guessing would put a badge on a row that may already be current.
    """
    left, right = normalize_version(installed), normalize_version(latest)
    return bool(left) and bool(right) and left != right


def version_from_filename(filename: str) -> str:
    """The version token a pack file name carries, or ``""`` when it carries none.

    ``BSL_v8.4.01.1.zip`` -> ``8.4.01.1``; ``Faithful 32x.zip`` -> ``""`` (``32x`` is a resolution,
    not a version). This is the fallback the resource/shader lists use because a pack's zip does not
    record the version of the project it came from.
    """
    import re

    stem = Path(str(filename or "")).stem
    match = re.search(
        r"(?<![0-9A-Za-z])[vVrR]?(\d+(?:\.\d+){1,3})(?![0-9])", stem, re.IGNORECASE
    )
    return match.group(1) if match else ""


def pack_search_name(filename: str) -> str:
    """The project name a pack file name hints at: the stem without its version and separators.

    ``ComplementaryReimagined_r5.3.zip`` -> ``ComplementaryReimagined``; ``Faithful 32x.zip`` ->
    ``Faithful 32x``; ``BSL_v8.4.01.1.zip`` -> ``BSL``. The row still displays the untouched name -
    this is only the query and the match target, which has to look like the project's title rather
    than like its download.
    """
    import re

    stem = Path(str(filename or "")).stem
    version = version_from_filename(filename)
    if version:
        stem = re.sub(
            r"(?<![0-9A-Za-z])[vVrR]?" + re.escape(version) + r"(?![0-9])", " ", stem, count=1
        )
    stem = re.sub(r"[_\-\s]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" _-.")
    # Modrinth's search does not tokenize a concatenated project name well ("ComplementaryReimagined"
    # finds nothing), while the match itself is separator-insensitive. Splitting camelCase here is a
    # query-formatting step only: the row still displays the untouched file name.
    stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
    return stem or Path(str(filename or "")).stem


# ------------------------------------------------------------------------------------------------
# Disk cache
# ------------------------------------------------------------------------------------------------


def cache_dir() -> Path:
    """Where lookups are stored: ``<launcher data dir>/cache/content-identity``."""
    return paths.launcher_dir() / "cache" / CACHE_DIRNAME


def _cache_key(kind: str, name: str, modid: str, game_version: str, loader: str) -> str:
    """The identity of a lookup: two files that differ only in version are different rows."""
    return "|".join(
        (
            str(kind).strip().casefold(),
            str(modid).strip().casefold(),
            str(name).strip().casefold(),
            str(game_version).strip().casefold(),
            str(loader).strip().casefold(),
        )
    )


def _cache_path(kind: str, key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir() / f"{kind}-{digest}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write through a temporary file in the same directory, so a killed process cannot tear a body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}-{threading.get_ident()}")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_cache(kind: str, key: str) -> tuple[bool, Identity | None, float]:
    """``(found, identity_or_None, fetched_at)``; ``found`` distinguishes "no file" from "no match"."""
    path = _cache_path(kind, key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None, 0.0
    if not isinstance(raw, dict):
        return False, None, 0.0
    fetched = float(raw.get("fetched_at") or 0.0)
    payload = raw.get("identity")
    if payload is None:
        return True, None, fetched
    if not isinstance(payload, dict):
        return False, None, 0.0
    try:
        identity = Identity(
            kind=str(payload.get("kind") or kind),
            slug=str(payload.get("slug") or ""),
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or ""),
            categories=tuple(str(item) for item in (payload.get("categories") or ())),
            icon_url=str(payload.get("icon_url") or ""),
            updated=str(payload.get("updated") or ""),
            latest_version=str(payload.get("latest_version") or ""),
        )
    except (TypeError, ValueError):
        return False, None, 0.0
    if not identity.slug:
        return False, None, 0.0
    return True, identity, fetched


def _write_cache(kind: str, key: str, identity: Identity | None) -> None:
    path = _cache_path(kind, key)
    payload = {
        "kind": kind,
        "key": key,
        "fetched_at": time.time(),
        "identity": asdict(identity) if identity is not None else None,
    }
    try:
        _atomic_write(
            path, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
    except OSError:
        LOGGER.debug("content-identity 缓存写入失败: %s", path, exc_info=True)


def clear_cache() -> int:
    """Delete every stored lookup; returns how many files were removed (tests / repair)."""
    directory = cache_dir()
    removed = 0
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return 0
    for path in entries:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def forget_cached_icon(url: str) -> bool:
    """Drop one icon from :mod:`launcher.net.http_cache`'s store, by the key that store uses.

    The icon store has no single-entry invalidation helper (only :func:`clear_icon_cache`), and the
    page needs one thing from it: when the stored bytes are not a decodable image, the next attempt
    must be a download rather than the same broken file. So this removes exactly the two files the
    store would have written for ``url`` (``<sha1>.json`` plus the body it names) and returns whether
    anything was removed. A missing entry is not an error.
    """
    from launcher.net import http_cache

    key = hashlib.sha1(str(url).encode("utf-8")).hexdigest()
    directory = http_cache.icon_cache_dir()
    meta_path = directory / (key + ".json")
    removed = False
    body_name = ""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(meta, dict):
            body_name = str(meta.get("body") or "")
    except (OSError, ValueError):
        meta = {}
    for path in (meta_path, directory / body_name if body_name else None):
        if path is None:
            continue
        try:
            path.unlink()
            removed = True
        except OSError:
            continue
    return removed


# ------------------------------------------------------------------------------------------------
# Modrinth
# ------------------------------------------------------------------------------------------------


def _client() -> httpx.Client:
    """The launcher's shared pooled client (a closed call is a no-op on it), imported lazily."""
    from launcher.meta.manifest import _new_client

    return _new_client()


def _request_json(client: httpx.Client, path: str, params: dict | None = None):
    """One GET against Modrinth; the product User-Agent is sent explicitly on every call."""
    response = client.get(
        MODRINTH_API + path,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _search(client: httpx.Client, query: str, kind: str) -> dict:
    payload = _request_json(
        client,
        "/search",
        params={
            "query": query,
            "limit": str(SEARCH_LIMIT),
            "facets": json.dumps([["project_type:" + kind]]),
        },
    )
    return payload if isinstance(payload, dict) else {}


def _match_key(text: object) -> str:
    """A comparison key for slug/title matching: case-insensitive and separator-insensitive.

    ``Complementary Reimagined``, ``complementary-reimagined`` and ``ComplementaryReimagined`` name
    the same project, and a pack file name is written in the third style while the API writes the
    first two. Only alphanumerics are kept, so the comparison stays exact in every other respect -
    this is not a fuzzy distance, and ``sodium`` still cannot match ``sodium-extra``.
    """
    return "".join(char for char in str(text or "").casefold() if char.isalnum())


def _matching_hit(hits: object, modid: str, name: str) -> dict | None:
    """The first hit whose slug or title equals the mod id or the file name, case-insensitively."""
    targets = {
        _match_key(value) for value in (modid, name) if str(value or "").strip()
    }
    targets.discard("")
    if not isinstance(hits, list) or not targets:
        return None
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        slug = _match_key(hit.get("slug"))
        title = _match_key(hit.get("title"))
        if (slug and slug in targets) or (title and title in targets):
            return hit
    return None


def _latest_version(client: httpx.Client, slug: str, game_version: str, loader: str) -> str:
    """Newest ``version_number`` for this exact game version (and loader, for a mod).

    Modrinth returns the list newest first. A missing ``version_number`` is not worth an exception:
    the row simply has no update information.
    """
    params: dict[str, str] = {"game_versions": json.dumps([game_version])}
    if loader:
        params["loaders"] = json.dumps([loader])
    payload = _request_json(client, f"/project/{slug}/version", params=params)
    if not isinstance(payload, list):
        return ""
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get("version_number") or "").strip()
        if number:
            return number
    return ""


def _identity_from_hit(
    kind: str, hit: dict, project: dict, latest_version: str
) -> Identity:
    categories = project.get("categories") or hit.get("categories") or []
    return Identity(
        kind=kind,
        slug=str(project.get("slug") or hit.get("slug") or ""),
        title=str(project.get("title") or hit.get("title") or ""),
        description=str(project.get("description") or hit.get("description") or ""),
        categories=tuple(str(item) for item in categories if isinstance(item, str)),
        icon_url=str(project.get("icon_url") or hit.get("icon_url") or ""),
        updated=str(project.get("updated") or hit.get("date_modified") or ""),
        latest_version=latest_version,
    )


def _query(
    client: httpx.Client,
    name: str,
    modid: str,
    kind: str,
    game_version: str,
    loader: str,
) -> Identity | None:
    """Search, match, fetch details and (when asked) the newest version - no caching here."""
    query = (modid or name).strip()
    if not query:
        return None
    payload = _search(client, query, kind)
    hit = _matching_hit(payload.get("hits"), modid, name)
    if hit is None:
        return None
    slug = str(hit.get("slug") or "").strip()
    if not slug:
        return None
    project = _request_json(client, f"/project/{slug}")
    if not isinstance(project, dict):
        return None
    latest = ""
    if game_version:
        latest = _latest_version(
            client, slug, game_version, loader if kind == "mod" else ""
        )
    return _identity_from_hit(kind, hit, project, latest)


def file_sha1(path: Path) -> str:
    """The SHA-1 of a file on disk, in 64 KB chunks.

    Modrinth's ``/version_file/{hash}`` is the only identification that cannot be wrong: a jar renamed
    to something meaningless still hashes to the file the author uploaded. That is the difference the
    "updatable" badge needs.
    """
    digest = hashlib.sha1()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 16):
            digest.update(chunk)
    return digest.hexdigest()


def _version_from_hash(client: httpx.Client, sha1: str) -> dict | None:
    """The Modrinth version this exact file is, or None when the file is not from Modrinth."""
    try:
        payload = _request_json(client, f"/version_file/{sha1}", {"algorithm": "sha1"})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return payload if isinstance(payload, dict) else None


def lookup_by_hash(
    path: Path,
    kind: str,
    *,
    game_version: str = "",
    loader: str = "",
    client: httpx.Client | None = None,
    ttl: float = CACHE_TTL,
    now: float | None = None,
) -> Identity | None:
    """Identify a file by its content hash; ``None`` when it is not a Modrinth file.

    This is the honest half of identification. The name-based :func:`lookup` searches and matches a hit
    by name, so two projects can share a name and a renamed file loses its identity entirely. A hash
    match cannot be wrong: Modrinth's answer is the version whose file bytes are these bytes.

    A hit is cached under the hash, so the same file in twenty instances is looked up once. A 404 (a
    file from CurseForge, or hand-made) is not cached as an answer - it is simply "not a Modrinth file",
    and the caller falls back to :func:`lookup`.
    """
    if kind not in KINDS or not is_enabled():
        return None
    try:
        sha1 = file_sha1(path)
    except OSError:
        return None
    if not sha1:
        return None
    key = _cache_key(kind, f"sha1:{sha1}", "", game_version, loader)
    found, cached, fetched = _read_cache(kind, key)
    stamp = time.time() if now is None else float(now)
    if found and stamp - fetched <= ttl:
        return cached

    own = client is None
    active = client or _client()
    try:
        version = _version_from_hash(active, sha1)
        if version is None:
            return None
        project_id = str(version.get("project_id") or "").strip()
        if not project_id:
            return None
        project = _request_json(active, f"/project/{project_id}")
        if not isinstance(project, dict):
            return None
        latest = (
            _latest_version(active, str(project.get("slug") or ""), game_version, loader)
            if game_version and project.get("slug")
            else ""
        )
        identity = _identity_from_hit(kind, {}, project, latest)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        LOGGER.debug("哈希识别失败: %s (%s)", path, exc, exc_info=True)
        return cached if found else None
    finally:
        if own:
            active.close()
    _write_cache(kind, key, identity)
    return identity


def lookup(
    name: str,
    modid: str,
    kind: str,
    *,
    game_version: str = "",
    loader: str = "",
    client: httpx.Client | None = None,
    ttl: float = CACHE_TTL,
    now: float | None = None,
) -> Identity | None:
    """Identify one content file, from the disk cache when possible and the network otherwise.

    The signature and the return value are the whole contract: a caller gets an :class:`Identity` or
    ``None`` ("not recognized", "offline", "switched off") and never an exception. ``game_version``
    and ``loader`` are optional; passing them adds the newest version number for that combination,
    which is what the "updatable" tab compares the installed version with.

    A cache entry younger than ``ttl`` (30 days by default) answers without touching the network. An
    expired one is refreshed; if the refresh fails, the expired answer is still returned - a month-old
    icon and summary beat an empty row - while a lookup that has never succeeded returns ``None``.
    """
    if kind not in KINDS:
        return None
    if not is_enabled():
        return None
    key = _cache_key(kind, name, modid, game_version, loader)
    found, cached, fetched = _read_cache(kind, key)
    stamp = time.time() if now is None else float(now)
    if found and stamp - fetched <= ttl:
        return cached

    own = client is None
    active = client or _client()
    try:
        identity = _query(active, name, modid, kind, game_version, loader)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        LOGGER.debug("内容识别失败: %s (%s)", modid or name, exc, exc_info=True)
        return cached if found else None
    finally:
        if own:
            active.close()
    _write_cache(kind, key, identity)
    return identity


# ------------------------------------------------------------------------------------------------
# The one worker behind every list
# ------------------------------------------------------------------------------------------------


class LookupQueue:
    """A FIFO background worker for lookups: spaced, deduplicated and cancellable.

    The interface can show dozens of rows at once, so three things have to hold that a plain
    ``run_in_background`` per row does not provide:

    * **spacing** - the requests of two rows never start closer than :data:`QUERY_INTERVAL`, which
      keeps a 60-mod page from opening 60 connections in the same millisecond;
    * **deduplication** - a key already queued or running is not queued twice (the same file can be
      listed again after a refresh while its answer is still in flight);
    * **cancellation** - :meth:`cancel_all` drops everything pending and marks the current run stale,
      so a result that arrives after the user switched instance is discarded instead of painting a
      row that no longer exists. The worker thread exits as soon as the queue is empty, so a page
      that is left really does stop the work.

    ``worker`` is injectable for tests: it receives a :class:`LookupRequest` and returns an
    :class:`Identity` or ``None``. The callback runs on the worker thread - the GUI wraps it in a Qt
    signal, which is the documented thread-safe way to reach the widget thread - and is only called
    when its result still belongs to the current generation.
    """

    def __init__(
        self,
        *,
        interval: float = QUERY_INTERVAL,
        worker: Callable[[LookupRequest], Identity | None] | None = None,
    ) -> None:
        self._interval = max(0.0, float(interval))
        self._worker = worker or self._default_worker
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._items: deque[tuple[LookupRequest, Callable[[LookupRequest, object], None], int]] = (
            deque()
        )
        self._keys: set[str] = set()
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._last_start = 0.0
        #: Requests actually executed; the tests assert both that it stays at zero (switch off) and
        #: that cancellation stops it from growing.
        self.queries = 0

    @staticmethod
    def _default_worker(request: LookupRequest) -> Identity | None:
        if request.path:
            try:
                by_hash = lookup_by_hash(
                    Path(request.path),
                    request.kind,
                    game_version=request.game_version,
                    loader=request.loader,
                )
            except Exception:
                LOGGER.debug("哈希识别异常: %s", request.path, exc_info=True)
                by_hash = None
            if by_hash is not None:
                return by_hash
        return lookup(
            request.name,
            request.modid,
            request.kind,
            game_version=request.game_version,
            loader=request.loader,
        )

    # ---------- public API ----------

    def submit(
        self, request: LookupRequest, callback: Callable[[LookupRequest, object], None]
    ) -> bool:
        """Queue one lookup; returns False when it was refused (switch off or already queued)."""
        if not is_enabled():
            return False
        with self._lock:
            if request.key in self._keys:
                return False
            self._keys.add(request.key)
            self._items.append((request, callback, self._generation))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="content-identity", daemon=True
                )
                self._thread.start()
        self._wake.set()
        return True

    def cancel_all(self) -> None:
        """Forget everything pending and discard the result of the lookup that is running."""
        with self._lock:
            self._generation += 1
            self._items.clear()
            self._keys.clear()
        self._wake.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait until the queue is empty and its thread has exited (tests and shutdown)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                thread = self._thread
                empty = not self._items
            if empty and (thread is None or not thread.is_alive()):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def stop(self, timeout: float = 3.0) -> None:
        """Cancel and wait for the worker to leave (the page calls this when it is torn down)."""
        self.cancel_all()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, timeout))

    # ---------- worker ----------

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._items:
                    self._thread = None
                    return
                request, callback, generation = self._items.popleft()
                current = self._generation
            if generation != current or not is_enabled():
                self._release(request.key)
                continue
            if not self._wait_for_slot(generation):
                self._release(request.key)
                continue
            with self._lock:
                if generation != self._generation or not is_enabled():
                    self._release(request.key)
                    continue
                self.queries += 1
                self._last_start = time.monotonic()
            try:
                result = self._worker(request)
            except Exception:
                LOGGER.debug("内容识别线程失败: %s", request.key, exc_info=True)
                result = None
            self._release(request.key)
            with self._lock:
                stale = generation != self._generation
            if stale:
                continue
            try:
                callback(request, result)
            except Exception:
                LOGGER.debug("内容识别回调失败: %s", request.key, exc_info=True)

    def _release(self, key: str) -> None:
        with self._lock:
            self._keys.discard(key)

    def _wait_for_slot(self, generation: int) -> bool:
        """Sleep until the interval since the previous request has passed; False when cancelled."""
        while True:
            with self._lock:
                if generation != self._generation:
                    return False
                remaining = self._interval - (time.monotonic() - self._last_start)
            if remaining <= 0:
                return True
            if self._wake.wait(min(remaining, 0.02)):
                self._wake.clear()
            with self._lock:
                if generation != self._generation:
                    return False
