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

"""On-disk HTTP cache: max-age freshness, ETag / Last-Modified revalidation, and the project icon store.

Why this module exists: every JSON the launcher reads (the version manifest, a version JSON, the asset
index, Modrinth / CurseForge search results) used to be downloaded in full on every request, and the
project icons used to be downloaded again on every start. Measured against the live services: one
request costs 0.8-2.1 s, a cached manifest reply is a 0.09 s "304 Not Modified", and a project icon is
1.9 s. The fix is therefore not a faster network - it is not asking twice.

Three pieces, kept in one place so a call site never implements caching itself:

* :class:`CachingTransport` - an ``httpx.BaseTransport`` wrapper. It sits under the shared client, so
  every JSON / text request through :func:`launcher.meta.manifest._new_client` gets the same rules for
  free: a fresh copy (within ``Cache-Control: max-age``, or :data:`DEFAULT_MAX_AGE`) is answered from
  disk without a request, a stale one is revalidated with ``If-None-Match`` / ``If-Modified-Since``, and
  a ``304 Not Modified`` reuses the bytes already on disk. A network failure falls back to the stored
  copy instead of failing a startup that worked a minute ago.
* :func:`fetch_icon_bytes` - the project-icon cache (``<launcher data dir>/cache/mod-icons``): a hit is a
  file read, a miss downloads once, and an expired icon is served from disk immediately while a
  background request refreshes it (an icon barely ever changes, so waiting for the network is wasted).
* :func:`cached_icon_bytes` - the GUI-thread peek, so a warm start paints the real icons in the same
  pass that builds the rows instead of waiting for a worker thread.

Metadata (ETag, Last-Modified, max-age, fetch time) is kept next to the body, so a restart keeps both the
bytes and the validation tokens. Every write is a temporary file plus :meth:`Path.replace`, so a killed
process can never leave a half-written body in place of a good one.

The whole layer can be switched off (:func:`set_enabled`); the test suite does that so a stray request in
a test process can neither read nor write the developer's real cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from launcher import paths

logger = logging.getLogger(__name__)

#: Directory names under the launcher data directory.
CACHE_DIRNAME = "cache"
HTTP_DIRNAME = "http"
ICON_DIRNAME = "mod-icons"

#: How long a JSON / text response is used without any request when the server names no ``max-age``.
#: Ten minutes: long enough that flipping between two searches, or restarting the launcher right after
#: one, is served from disk; short enough that a changed result shows up by itself.
DEFAULT_MAX_AGE = 600.0

#: A project icon never changes for a given URL in practice, so it is refreshed once a month, in the
#: background, and the stale copy keeps being shown until the refresh lands.
ICON_MAX_AGE = 30 * 24 * 3600.0

#: A JSON / text body larger than this is not a manifest or an API answer; never put it in the cache.
MAX_TEXT_BYTES = 8 * 1024 * 1024
#: Hard cap from the icon contract: a project icon is a few dozen KB, never hold more than this.
ICON_MAX_BYTES = 2_000_000

#: Content types this layer stores. Binaries (jars, assets, sounds) are streamed and never cached here:
#: they are either large, already verified by sha1 by the downloader, or both.
_TEXTUAL_TYPES = ("application/json", "application/xml", "application/javascript")
_TEXTUAL_PREFIXES = ("text/",)
_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
}
_ICON_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico")

#: The response headers worth keeping: everything else is hop-by-hop or already handled by httpx.
_STORED_HEADERS = ("content-type", "etag", "last-modified", "cache-control", "date", "vary", "expires")

_enabled = True
_enabled_lock = threading.Lock()


def set_enabled(value: bool) -> None:
    """Switch the whole disk cache on or off (the test suite turns it off for its own process)."""
    global _enabled
    with _enabled_lock:
        _enabled = bool(value)


def is_enabled() -> bool:
    with _enabled_lock:
        return _enabled


def cache_root() -> Path:
    """``<launcher data dir>/cache`` - the same root the rest of the launcher already uses."""
    return paths.launcher_dir() / CACHE_DIRNAME


def http_cache_dir() -> Path:
    """Where the generic JSON / text cache keeps its bodies and metadata."""
    return cache_root() / HTTP_DIRNAME


def icon_cache_dir() -> Path:
    """Where the project icons keep their bodies and metadata."""
    return cache_root() / ICON_DIRNAME


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory.

    The temporary name carries the pid and thread id: two download workers may store the same URL at the
    same time, and one must not replace the other's half-written temporary file. :meth:`Path.replace` is
    atomic, so a reader sees either the old file or the new one - never a torn body.
    """
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


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    _atomic_write(path, data)


def _is_textual(content_type: str) -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    return mime.startswith(_TEXTUAL_PREFIXES) or mime in _TEXTUAL_TYPES or mime.endswith("+json")


def _parse_cache_control(headers: httpx.Headers) -> tuple[bool, float | None]:
    """``(may_store, max_age)`` from one response's Cache-Control header.

    ``no-store`` is the only directive that forbids storing a copy. ``no-cache`` does not: it means "ask
    before reusing", which is exactly the revalidation this layer does, so it becomes ``max-age=0``.
    """
    value = headers.get("cache-control") or ""
    directives: dict[str, str] = {}
    for part in value.split(","):
        name, _, argument = part.strip().partition("=")
        if name:
            directives[name.strip().lower()] = argument.strip().strip('"')
    if "no-store" in directives:
        return False, None
    if "no-cache" in directives:
        return True, 0.0
    for key in ("max-age", "s-maxage"):
        argument = directives.get(key)
        if argument and argument.isdigit():
            return True, float(argument)
    return True, None


def _url_ext(url: str) -> str:
    """The file suffix an icon URL names, when it names one."""
    try:
        path = httpx.URL(url).path
    except Exception:  # noqa: BLE001 - an odd URL is not worth failing the icon over
        return ""
    suffix = Path(path).suffix.lower()
    return suffix if suffix in _ICON_SUFFIXES else ""


@dataclass(frozen=True)
class CacheEntry:
    """One stored response: bytes plus the tokens needed to revalidate it."""

    url: str
    body: bytes
    content_type: str
    etag: str
    last_modified: str
    max_age: float
    fetched_at: float
    headers: dict[str, str]

    def age(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    def is_fresh(self) -> bool:
        return self.age() <= self.max_age

    def as_response(self, request: httpx.Request) -> httpx.Response:
        headers = dict(self.headers)
        headers["content-length"] = str(len(self.body))
        return httpx.Response(200, headers=headers, content=self.body, request=request)


def _entry_from_parts(url: str, body: bytes, meta: dict, headers: dict[str, str]) -> CacheEntry:
    return CacheEntry(
        url=url,
        body=body,
        content_type=str(meta.get("content_type") or headers.get("content-type") or ""),
        etag=str(meta.get("etag") or headers.get("etag") or ""),
        last_modified=str(meta.get("last_modified") or headers.get("last-modified") or ""),
        max_age=float(meta.get("max_age") or 0.0),
        fetched_at=float(meta.get("fetched_at") or 0.0),
        headers=headers,
    )


def _meta_headers(meta: dict) -> dict[str, str]:
    stored = meta.get("headers")
    if isinstance(stored, dict):
        return {str(k): str(v) for k, v in stored.items()}
    return {}


class CachingTransport(httpx.BaseTransport):
    """An ``httpx`` transport that adds a disk cache to every response it can store.

    Wrapping the transport (instead of writing a wrapper function per call site) is what makes the rules
    apply everywhere at once: the version manifest, a version JSON, the asset index and the Modrinth /
    CurseForge clients all talk through the shared client, so none of them has to know this exists.

    Only ``GET`` responses with a JSON / text content type are stored. A ``Range`` request is never
    cached (a partial body must not be mistaken for the whole file), and neither is a streamed binary:
    the downloader's jars and assets keep flowing exactly as before.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        root: Path | None = None,
        enabled: bool | None = None,
        default_max_age: float = DEFAULT_MAX_AGE,
        max_bytes: int = MAX_TEXT_BYTES,
    ) -> None:
        self.inner = inner
        self._root = root
        self._enabled = enabled
        self._default_max_age = float(default_max_age)
        self._max_bytes = int(max_bytes)

    # ---------- plumbing ----------

    def close(self) -> None:
        self.inner.close()

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else http_cache_dir()

    def _active(self) -> bool:
        return self._enabled if self._enabled is not None else is_enabled()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = _sha1(url)
        directory = self.root
        return directory / (key + ".body"), directory / (key + ".json")

    def _load(self, url: str) -> CacheEntry | None:
        body_path, meta_path = self._paths(url)
        meta = _read_json(meta_path)
        if not meta:
            return None
        try:
            body = body_path.read_bytes()
        except OSError:
            return None
        if not body or len(body) > self._max_bytes:
            return None
        return _entry_from_parts(url, body, meta, _meta_headers(meta))

    def _store(
        self, url: str, body: bytes, response: httpx.Response, max_age: float
    ) -> CacheEntry:
        body_path, meta_path = self._paths(url)
        headers = {
            name: response.headers[name]
            for name in _STORED_HEADERS
            if response.headers.get(name)
        }
        meta = {
            "url": url,
            "content_type": response.headers.get("content-type", ""),
            "etag": response.headers.get("etag", ""),
            "last_modified": response.headers.get("last-modified", ""),
            "max_age": float(max_age),
            "fetched_at": time.time(),
            "headers": headers,
        }
        try:
            _atomic_write(body_path, body)
            _write_json(meta_path, meta)
        except OSError:
            logger.debug("HTTP 缓存写入失败: %s", url, exc_info=True)
        return _entry_from_parts(url, body, meta, headers)

    def _touch(self, url: str, entry: CacheEntry, headers: httpx.Headers) -> CacheEntry:
        """A 304 came back: keep the body, refresh the validation tokens and the fetch time."""
        merged = dict(entry.headers)
        for name in _STORED_HEADERS:
            if headers.get(name):
                merged[name] = headers[name]
        _store_ok, max_age = _parse_cache_control(headers)
        effective_age = entry.max_age if max_age is None else max_age
        meta = {
            "url": url,
            "content_type": merged.get("content-type", entry.content_type),
            "etag": merged.get("etag", entry.etag),
            "last_modified": merged.get("last-modified", entry.last_modified),
            "max_age": float(effective_age),
            "fetched_at": time.time(),
            "headers": merged,
        }
        _body_path, meta_path = self._paths(url)
        try:
            _write_json(meta_path, meta)
        except OSError:
            logger.debug("HTTP 缓存元数据写入失败: %s", url, exc_info=True)
        return CacheEntry(
            url=url,
            body=entry.body,
            content_type=str(meta["content_type"]),
            etag=str(meta["etag"]),
            last_modified=str(meta["last_modified"]),
            max_age=float(meta["max_age"]),
            fetched_at=float(meta["fetched_at"]),
            headers={str(k): str(v) for k, v in merged.items()},
        )

    # ---------- request handling ----------

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not self._active() or not self._cacheable_request(request):
            return self.inner.handle_request(request)
        url = str(request.url)
        entry = self._load(url)
        force = _no_cache(request)
        if entry is not None and not force and not _is_textual(entry.content_type):
            entry = None
        if entry is not None and entry.is_fresh() and not force:
            return entry.as_response(request)

        headers = dict(request.headers)
        if entry is not None:
            if entry.etag:
                headers["if-none-match"] = entry.etag
            elif entry.last_modified:
                headers["if-modified-since"] = entry.last_modified
        outgoing = request if headers == dict(request.headers) else _with_headers(request, headers)
        try:
            response = self.inner.handle_request(outgoing)
        except httpx.TransportError:
            if entry is not None:
                logger.debug("网络失败，使用缓存副本: %s", url, exc_info=True)
                return entry.as_response(request)
            raise
        if response.status_code == 304 and entry is not None:
            refreshed = self._touch(url, entry, response.headers)
            response.close()
            return refreshed.as_response(request)
        if response.status_code != 200 or not _is_textual(
            response.headers.get("content-type", "")
        ):
            return response
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > self._max_bytes:
            return response
        try:
            body = response.read()
        except httpx.HTTPError:
            response.close()
            raise
        response.close()
        if not body or len(body) > self._max_bytes:
            return _response_from_bytes(response, body, request)
        store_ok, max_age = _parse_cache_control(response.headers)
        if store_ok:
            self._store(url, body, response, self._default_max_age if max_age is None else max_age)
        return _response_from_bytes(response, body, request)

    @staticmethod
    def _cacheable_request(request: httpx.Request) -> bool:
        if request.method != "GET":
            return False
        if "range" in request.headers:
            return False
        return request.url.scheme in ("http", "https")


def _no_cache(request: httpx.Request) -> bool:
    """A caller that must revalidate says so with ``Cache-Control: no-cache`` (the manifest refresh)."""
    return "no-cache" in (request.headers.get("cache-control") or "").lower()


def _with_headers(request: httpx.Request, headers: dict[str, str]) -> httpx.Request:
    """A copy of ``request`` with extra headers (the body is empty for a GET)."""
    return httpx.Request(request.method, request.url, headers=headers, extensions=request.extensions)


def _response_from_bytes(
    response: httpx.Response, body: bytes, request: httpx.Request
) -> httpx.Response:
    """The response for a body this layer read, keeping the original status and headers."""
    headers = httpx.Headers(response.headers)
    headers.pop("content-encoding", None)
    headers.pop("transfer-encoding", None)
    headers["content-length"] = str(len(body))
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=body,
        request=request,
    )


# --------------------------------------------------------------------------------------------------
# Project icons
# --------------------------------------------------------------------------------------------------

_icon_guard = threading.Lock()
_icon_refreshing: set[str] = set()
_icon_threads: set[threading.Thread] = set()


def _icon_meta_path(url: str) -> Path:
    return icon_cache_dir() / (_sha1(url) + ".json")


def _read_icon(url: str) -> CacheEntry | None:
    meta = _read_json(_icon_meta_path(url))
    body_name = str(meta.get("body") or "")
    if not body_name:
        return None
    try:
        body = (icon_cache_dir() / body_name).read_bytes()
    except OSError:
        return None
    if not body:
        return None
    return _entry_from_parts(url, body, meta, _meta_headers(meta))


def _store_icon(url: str, body: bytes, response: httpx.Response) -> None:
    directory = icon_cache_dir()
    key = _sha1(url)
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    suffix = _url_ext(url) or _IMAGE_TYPES.get(content_type) or ".bin"
    body_name = key + suffix
    try:
        _atomic_write(directory / body_name, body)
    except OSError:
        logger.debug("图标缓存写入失败: %s", url, exc_info=True)
        return
    headers = {
        name: response.headers[name] for name in _STORED_HEADERS if response.headers.get(name)
    }
    meta = {
        "url": url,
        "body": body_name,
        "content_type": content_type,
        "etag": response.headers.get("etag", ""),
        "last_modified": response.headers.get("last-modified", ""),
        "max_age": ICON_MAX_AGE,
        "fetched_at": time.time(),
        "headers": headers,
    }
    try:
        _write_json(directory / (key + ".json"), meta)
    except OSError:
        logger.debug("图标缓存元数据写入失败: %s", url, exc_info=True)


def _touch_icon(url: str, meta: dict, response: httpx.Response) -> None:
    """A 304 for an expired icon: keep the body, move its expiry 30 days out."""
    body_name = str(meta.get("body") or "")
    if not body_name:
        return
    refreshed = dict(meta)
    refreshed["fetched_at"] = time.time()
    refreshed["max_age"] = ICON_MAX_AGE
    headers = dict(_meta_headers(meta))
    for name in _STORED_HEADERS:
        if response.headers.get(name):
            headers[name] = response.headers[name]
    refreshed["headers"] = headers
    if headers.get("content-type"):
        refreshed["content_type"] = headers["content-type"]
    if headers.get("etag"):
        refreshed["etag"] = headers["etag"]
    if headers.get("last-modified"):
        refreshed["last_modified"] = headers["last-modified"]
    try:
        _write_json(_icon_meta_path(url), refreshed)
    except OSError:
        logger.debug("图标缓存元数据写入失败: %s", url, exc_info=True)


def _icon_client() -> httpx.Client:
    """The shared pooled client; imported lazily so this module never imports the network stack eagerly."""
    from launcher.meta.manifest import _new_client

    return _new_client()


def _load_icon(url: str, client: httpx.Client | None, timeout: float) -> bytes:
    """Download one icon and store it; raises on a transport / HTTP failure."""
    own = client is None
    client = client or _icon_client()
    try:
        response = client.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        body = response.content
    finally:
        if own:
            client.close()
    if len(body) > ICON_MAX_BYTES:
        raise ValueError("icon payload too large")
    _store_icon(url, body, response)
    return body


def _refresh_icon(url: str, client: httpx.Client | None, timeout: float) -> None:
    """One background revalidation: a conditional GET; a 304 keeps the body, a 200 replaces it."""
    try:
        entry = _read_icon(url)
        if entry is None:
            _load_icon(url, client, timeout)
            return
        meta = _read_json(_icon_meta_path(url))
        own = client is None
        client = client or _icon_client()
        headers: dict[str, str] = {}
        if entry.etag:
            headers["if-none-match"] = entry.etag
        elif entry.last_modified:
            headers["if-modified-since"] = entry.last_modified
        try:
            response = client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            if response.status_code == 304:
                _touch_icon(url, meta, response)
                return
            if response.status_code != 200:
                return
            body = response.content
            if not body or len(body) > ICON_MAX_BYTES:
                return
            _store_icon(url, body, response)
        finally:
            if own:
                client.close()
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("图标后台刷新失败: %s", url, exc_info=True)
    finally:
        with _icon_guard:
            _icon_refreshing.discard(url)


def _start_icon_refresh(url: str, client: httpx.Client | None, timeout: float) -> None:
    with _icon_guard:
        _icon_threads.difference_update({t for t in _icon_threads if not t.is_alive()})
        if url in _icon_refreshing:
            return
        _icon_refreshing.add(url)
        thread = threading.Thread(
            target=_refresh_icon,
            args=(url, client, timeout),
            name="icon-refresh",
            daemon=True,
        )
        _icon_threads.add(thread)
        thread.start()


def wait_for_icon_refresh(timeout: float = 5.0) -> bool:
    """Wait for the background icon refreshes this process started (tests / shutdown)."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _icon_guard:
            running = [t for t in _icon_threads if t.is_alive()]
        if not running:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        running[0].join(min(remaining, 0.05))


def cached_icon_bytes(url: str) -> bytes | None:
    """The stored icon, when it is still fresh; ``None`` otherwise. Reads only - never a request.

    The mods page calls this on the GUI thread while it builds its rows: one file read of a few dozen
    KB is below 5 ms, so a warm start paints the real icons immediately instead of a letter tile that
    is replaced a moment later.
    """
    if not is_enabled() or not url.startswith(("http://", "https://")):
        return None
    entry = _read_icon(url)
    if entry is None or entry.age() > ICON_MAX_AGE:
        return None
    return entry.body


def fetch_icon_bytes(url: str, *, client: httpx.Client | None = None, timeout: float = 8.0) -> bytes:
    """Return a project icon, from the disk cache when possible.

    * hit and younger than :data:`ICON_MAX_AGE` - a file read, no request;
    * hit but older - the stored bytes are returned right away and a daemon thread revalidates them
      (``If-None-Match`` / ``If-Modified-Since``; a 304 only moves the expiry out);
    * miss - one request, stored atomically, 2 MB cap.

    The caller (a background worker in the mods page) is on a thread of its own either way, so a cold
    icon's download never blocks the interface.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("icon url is not http(s): " + url)
    if not is_enabled():
        return _load_icon(url, client, timeout)
    entry = _read_icon(url)
    if entry is not None:
        if entry.age() <= ICON_MAX_AGE:
            return entry.body
        _start_icon_refresh(url, client, timeout)
        return entry.body
    return _load_icon(url, client, timeout)


def clear_icon_cache() -> int:
    """Remove every stored project icon; returns how many files were deleted (tests / repair)."""
    directory = icon_cache_dir()
    removed = 0
    try:
        paths_to_remove = list(directory.glob("*"))
    except OSError:
        return 0
    for path in paths_to_remove:
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
