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

"""Fetching, caching, and parsing of the version manifest (version_manifest_v2.json)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from launcher import __version__
from launcher.i18n import describe_network_error, tr_core
from launcher.meta import sources

MANIFEST_URLS = (
    "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
    "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
)
USER_AGENT = f"MinePick-Launcher/{__version__}"
#: How long the cached manifest is trusted without asking for a newer one. Kept at the historical
#: hour; the point of the background refresh is that a start after that hour still paints at once.
DEFAULT_MAX_AGE = 3600.0

logger = logging.getLogger(__name__)


class MetaError(Exception):
    """Version metadata error (user-facing message)."""


class ManifestVersion(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    type: Literal["release", "snapshot", "old_beta", "old_alpha"]
    url: str
    time: str = ""
    release_time: str = Field(default="", alias="releaseTime")


# April Fools versions (type is snapshot/release in Mojang's official manifest, so they must be classified separately by id)
APRIL_FOOLS_IDS = frozenset(
    {
        "2.0",  # 2013 (old launcher channel, not in the current official manifest)
        "15w14a",  # 2015 Love and Hugs
        "1.RV-Pre1",  # 2016 Trendy
        "3D Shareware v1.34",  # 2019
        "20w14infinite",  # 2020 (official manifest id is 20w14infinite)
        "22w13oneblockatatime",  # 2022 (official manifest id is all lowercase)
        "23w13a_or_b",  # 2023
        "24w14potato",  # 2024
        "25w14craftmine",  # 2025
        "26w14a",  # 2026 Herdcraft
    }
)


def version_category(version_id: str, version_type: str) -> str:
    """Version category: april_fools / release / snapshot / old_beta / old_alpha."""
    if version_id in APRIL_FOOLS_IDS:
        return "april_fools"
    if version_type == "release":
        return "release"
    if version_type == "snapshot":
        return "snapshot"
    if version_type in ("old_beta", "old_alpha"):
        return version_type
    return version_type or "release"


class VersionManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    latest: dict[str, str] = {}
    versions: list[ManifestVersion] = []

    def find(self, version_id: str) -> ManifestVersion | None:
        for v in self.versions:
            if v.id == version_id:
                return v
        return None


_system_ca_injected = False


def _enable_system_ca() -> None:
    """On Windows, make httpx use the system certificate store (so enterprise proxy / intranet CAs pass verification)."""
    global _system_ca_injected
    if _system_ca_injected or os.name != "nt":
        return
    _system_ca_injected = True
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass  # fall back to certifi's default behavior when not installed


def _normalize_proxy(value: str) -> str:
    """Add the scheme to a proxy address (httpx requires a scheme, otherwise it errors)."""
    value = value.strip()
    if value and "://" not in value:
        value = "http://" + value
    return value


def _proxy_url() -> str | None:
    """Resolve the proxy address: config http_proxy takes priority, then env vars (HTTPS/HTTP/ALL_PROXY).

    On some networks a direct connection to api.modrinth.com and similar may be blocked
    (returning 404/timeout); the browser works because it goes through the system proxy,
    while httpx doesn't read the system proxy by default, so it must be injected manually.
    """
    try:
        from launcher import config

        cfg, _ = config.load()
        configured = (cfg.http_proxy or "").strip()
        if configured:
            return _normalize_proxy(configured)
    except Exception:
        import logging

        logging.getLogger(__name__).debug("读取代理配置失败，回退环境变量", exc_info=True)
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        value = os.environ.get(name)
        if value:
            return _normalize_proxy(value)
    return None


class _SharedHttpClient(httpx.Client):
    """Shared client whose close() is a no-op so per-call cleanup keeps the pool alive."""

    def close(self) -> None:
        pass

    def force_close(self) -> None:
        super().close()


class _LazyTransport(httpx.BaseTransport):
    """Build the real transport - and its TLS context - on the first request that needs it.

    ``httpx`` builds that context eagerly, measured at ~316 ms on this machine, which is more than a
    whole cached response costs. A start that only reads cached data must not pay it; the first real
    request does, exactly like it had to before the cache existed.
    """

    def __init__(self, factory: Callable[[], httpx.BaseTransport]) -> None:
        self._factory = factory
        self._inner: httpx.BaseTransport | None = None
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._built().handle_request(request)

    def _built(self) -> httpx.BaseTransport:
        if self._inner is None:
            with self._lock:
                if self._inner is None:
                    self._inner = self._factory()
        return self._inner

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()


_shared_client: httpx.Client | None = None
_shared_client_lock = threading.Lock()


def _new_client() -> httpx.Client:
    """Return the process-wide shared client: one connection pool plus the on-disk response cache.

    One pool for every call site is what removes the 95-220 ms TLS handshake a fresh ``httpx.Client()``
    paid per request; the caching transport is what makes a repeated JSON request cost a file read
    instead of a round trip. ``close()`` is a no-op on this subclass, so the per-call cleanup the
    callers already had (``finally: client.close()``) keeps the pool alive; the one real close happens
    at shutdown through :func:`reset_http_client`.
    """
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            # Imported here, not at module scope: launcher.net.downloader imports this module, so a
            # top-level import would be a cycle at package-import time.
            from launcher.net import http_cache

            def _build_transport() -> httpx.BaseTransport:
                # The system CA store must be injected before the SSL context exists, and that context
                # is exactly what this factory delays.
                _enable_system_ca()
                return httpx.HTTPTransport(
                    proxy=_proxy_url(),
                    trust_env=True,
                    # Longer than httpx's 5 s default: a connection kept for a minute covers the
                    # "search, read the list, search again" rhythm without a new handshake.
                    limits=httpx.Limits(
                        max_connections=32,
                        max_keepalive_connections=16,
                        keepalive_expiry=60.0,
                    ),
                )

            _shared_client = _SharedHttpClient(
                headers={"User-Agent": USER_AGENT},
                timeout=30.0,
                follow_redirects=True,
                transport=http_cache.CachingTransport(_LazyTransport(_build_transport)),
            )
        return _shared_client


def reset_http_client() -> None:
    """Close and rebuild the shared HTTP client (proxy / API key changes)."""
    global _shared_client
    with _shared_client_lock:
        if _shared_client is not None:
            _shared_client.force_close()
            _shared_client = None


def _fetch_first(client: httpx.Client, urls: tuple[str, ...]) -> dict:
    """Fetch the manifest from the first address the source resolver offers.

    The order (official, then the BMCLAPI mirrors in the ``auto`` setting) and the switching log
    live in :mod:`launcher.meta.sources`; this function keeps its historical error message.
    """
    try:
        return sources.fetch_json(client, urls, kind=sources.KIND_META)
    except httpx.HTTPError as exc:
        raise MetaError(
            tr_core("meta.manifest_fetch_failed", describe_network_error(exc))
        ) from exc


def _read_cache_raw(cache_path: Path) -> tuple[dict, float] | None:
    """The cached manifest body plus when it was fetched, whatever its age."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        raw = data["manifest"]
        if not isinstance(raw, dict):
            return None
        return raw, float(data.get("fetched_at", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(cache_path: Path, raw: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": time.time(), "manifest": raw}
    tmp = cache_path.with_name(cache_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_path)


# --- background refresh -------------------------------------------------------------------------
#
# The manifest is the first thing the versions page needs, and it is 278 KB. A start therefore paints
# the copy from disk and revalidates it behind the paint: the request goes through the caching
# transport, which sends If-None-Match / If-Modified-Since, so an unchanged manifest costs one 304
# (~0.1 s measured) instead of a full download (~1.5 s). Only a *different* manifest touches the
# listeners, which is what keeps a GUI from rebuilding its list for nothing.

_refresh_lock = threading.Lock()
_refreshing: set[Path] = set()
_refresh_threads: list[threading.Thread] = []
_listener_lock = threading.Lock()
_refresh_listeners: list[Callable[[VersionManifest], None]] = []


def add_refresh_listener(callback: Callable[[VersionManifest], None]) -> None:
    """Register ``callback`` for a background refresh that brought a different manifest.

    The callback runs on the refresh thread and only for a changed manifest - the blocking first fetch
    is returned to the caller directly. A GUI listener must hop to its own thread; the launcher's
    bridge does that with a Qt signal.
    """
    with _listener_lock:
        if callback not in _refresh_listeners:
            _refresh_listeners.append(callback)


def remove_refresh_listener(callback: Callable[[VersionManifest], None]) -> None:
    with _listener_lock:
        try:
            _refresh_listeners.remove(callback)
        except ValueError:
            pass


def wait_for_background_refresh(timeout: float = 5.0) -> bool:
    """Wait for the manifest refreshes this process started (tests and shutdown)."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _refresh_lock:
            _refresh_threads[:] = [thread for thread in _refresh_threads if thread.is_alive()]
            running = list(_refresh_threads)
        if not running:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        running[0].join(min(remaining, 0.05))


def _notify_refreshed(manifest: VersionManifest) -> None:
    with _listener_lock:
        listeners = list(_refresh_listeners)
    for callback in listeners:
        try:
            callback(manifest)
        except Exception:  # a listener may not break the refresh
            logger.debug("版本清单刷新回调失败", exc_info=True)


def _start_background_refresh(
    cache_path: Path, previous_raw: dict, client: httpx.Client | None
) -> None:
    with _refresh_lock:
        if cache_path in _refreshing:
            return
        _refreshing.add(cache_path)
        thread = threading.Thread(
            target=_refresh_manifest,
            args=(cache_path, previous_raw, client),
            name="manifest-refresh",
            daemon=True,
        )
        _refresh_threads.append(thread)
        thread.start()


def _refresh_manifest(
    cache_path: Path, previous_raw: dict, client: httpx.Client | None
) -> None:
    try:
        own = client is None
        active = client or _new_client()
        try:
            raw = sources.fetch_json(
                active,
                MANIFEST_URLS,
                kind=sources.KIND_META,
                context="version_manifest",
                headers={"Cache-Control": "no-cache"},
            )
        finally:
            if own:
                active.close()
        if not isinstance(raw, dict) or not raw.get("versions"):
            return
        _write_cache(cache_path, raw)
        if raw != previous_raw:
            _notify_refreshed(VersionManifest.model_validate(raw))
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("版本清单后台刷新失败", exc_info=True)
    finally:
        with _refresh_lock:
            _refreshing.discard(cache_path)


def fetch_manifest(
    cache_path: Path | None = None,
    max_age: float = DEFAULT_MAX_AGE,
    force: bool = False,
    client: httpx.Client | None = None,
) -> VersionManifest:
    """Load the version manifest, cache-first.

    With a cache file the copy on disk is returned immediately - whatever its age - and a stale copy
    (older than ``max_age``) additionally starts one background conditional fetch. That fetch asks for
    a fresh copy even when the response cache is still inside its own max-age window, so the server
    answers 304 and the 278 KB body is not transferred again; only a changed manifest rewrites the
    cache and tells the registered refresh listeners.
    """
    if cache_path is not None and not force:
        cached = _read_cache_raw(cache_path)
        if cached is not None:
            raw, fetched_at = cached
            try:
                manifest = VersionManifest.model_validate(raw)
            except ValueError:
                manifest = None
            if manifest is not None:
                if time.time() - fetched_at >= max_age:
                    _start_background_refresh(cache_path, raw, client)
                return manifest
    own = client is None
    if own:
        client = _new_client()
    try:
        raw = _fetch_first(client, MANIFEST_URLS)
    finally:
        if own:
            client.close()
    manifest = VersionManifest.model_validate(raw)
    if cache_path is not None:
        _write_cache(cache_path, raw)
    return manifest
