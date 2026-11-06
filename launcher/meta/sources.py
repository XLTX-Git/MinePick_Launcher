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

"""Download source resolution: one Mojang URL in, the ordered list of URLs to try out.

The launcher used to talk to Mojang's CDN only. For users in mainland China that is slow, so the
same bytes are also served by BMCLAPI. This module is the single place that knows how a Mojang
address maps onto a mirror; every download site keeps its business logic and only asks here which
addresses to try, in which order.

Two independent settings (``download_source_meta`` / ``download_source_assets``), each with three
values:

* ``mirror``   - only the mirrors, primary then backup (the user asked for them);
* ``auto``     - Mojang first, and only when that fails (or takes longer than
  :data:`FIRST_BYTE_TIMEOUT` for the first byte) the mirrors, primary then backup;
* ``official`` - only Mojang. A failure is reported; it is never silently retried against a mirror.

"Meta" covers everything that names or describes a version (version manifest, version JSON,
client.jar, libraries, asset index); "assets" covers the ``resources.download.minecraft.net``
objects, which are the bulk of the bytes.

Mirror path rules (verified against the live service, not copied from a document):

* ``launchermeta.mojang.com/mc/...``          -> ``bmclapi2.bangbang93.com/mc/...``
* ``piston-meta.mojang.com/v1/packages/...``  -> ``bmclapi2.bangbang93.com/v1/packages/...``
* ``piston-data.mojang.com/v1/objects/...``   -> ``bmclapi2.bangbang93.com/v1/objects/...``
* ``launcher.mojang.com/v1/objects/...``      -> ``bmclapi2.bangbang93.com/v1/objects/...``
* ``libraries.minecraft.net/<path>``          -> ``bmclapi2.bangbang93.com/maven/<path>``
* ``resources.download.minecraft.net/<xx>/<hash>``
                                            -> ``bmclapi2.bangbang93.com/assets/<xx>/<hash>``
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

import httpx

from launcher.i18n import describe_network_error, tr_core

logger = logging.getLogger(__name__)

# Settings values, also persisted by launcher.config.LauncherConfig.
MODE_MIRROR = "mirror"
MODE_AUTO = "auto"
MODE_OFFICIAL = "official"
MODES = (MODE_MIRROR, MODE_AUTO, MODE_OFFICIAL)
MODE_DEFAULT = MODE_AUTO

# Which endpoint family a URL belongs to; each has its own setting.
KIND_META = "meta"
KIND_ASSETS = "assets"

MIRROR_PRIMARY_HOST = "bmclapi2.bangbang93.com"
MIRROR_BACKUP_HOST = "bmclapi.bangbang93.com"
MIRROR_HOSTS = (MIRROR_PRIMARY_HOST, MIRROR_BACKUP_HOST)

META_MIRROR_BASES = (
    "https://" + MIRROR_PRIMARY_HOST,
    "https://" + MIRROR_BACKUP_HOST,
)
ASSET_MIRROR_BASES = (
    "https://" + MIRROR_PRIMARY_HOST + "/assets",
    "https://" + MIRROR_BACKUP_HOST + "/assets",
)

ASSETS_HOST = "resources.download.minecraft.net"
LIBRARIES_HOST = "libraries.minecraft.net"
META_HOSTS = frozenset(
    {
        "launchermeta.mojang.com",
        "piston-meta.mojang.com",
        "piston-data.mojang.com",
        "launcher.mojang.com",
        LIBRARIES_HOST,
    }
)
OFFICIAL_HOSTS = META_HOSTS | {ASSETS_HOST}

# "Official is slow" means its first byte took longer than this. Only applied when a fallback
# exists; with a single candidate the historical client timeout is left untouched.
FIRST_BYTE_TIMEOUT = 5.0

# The switch WARNING is emitted once per (from host, to host) pair: one outage can produce
# thousands of per-file switches and the log must stay readable. Later repeats are DEBUG.
_switch_pairs: set[tuple[str, str]] = set()
_switch_lock = threading.Lock()


def host_of(url: str) -> str:
    """The host an address names ('' when the string is not a usable absolute URL)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def source_kind(url: str) -> str:
    """``assets`` for a resources.download.minecraft.net object, ``meta`` for everything else."""
    return KIND_ASSETS if host_of(url) == ASSETS_HOST else KIND_META


def _mirror_variants(url: str) -> list[str]:
    """The mirror addresses equivalent to one official address, primary first.

    Returns an empty list for a host this launcher does not mirror (other services keep their own
    single address).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return []
    host = parts.hostname.lower()
    suffix = "?" + parts.query if parts.query else ""
    if host == ASSETS_HOST:
        bases = ASSET_MIRROR_BASES
        path = parts.path
    elif host in META_HOSTS:
        bases = META_MIRROR_BASES
        path = parts.path
        if host == LIBRARIES_HOST:
            # Maven artifacts live under /maven/ on the mirror; a bare path is a 404.
            path = "/maven" + (path if path.startswith("/") else "/" + path)
    else:
        return []
    return [base + path + suffix for base in bases]


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _tiers(mode: str) -> tuple[str, ...]:
    if mode == MODE_OFFICIAL:
        return (MODE_OFFICIAL,)
    if mode == MODE_MIRROR:
        return (MODE_MIRROR,)
    return (MODE_OFFICIAL, MODE_MIRROR)


def candidates(url: str, *, kind: str | None = None, mode: str | None = None) -> list[str]:
    """The ordered addresses to try for one Mojang URL. The first one is what plain code used to use.

    An unknown host has no mirror equivalent and yields just itself, in every mode: this layer can
    only mirror what BMCLAPI actually serves.
    """
    effective = mode if mode in MODES else current_mode(kind or source_kind(url))
    variants = _mirror_variants(url)
    if effective == MODE_OFFICIAL:
        return [url]
    if effective == MODE_MIRROR:
        return variants or [url]
    return _dedupe([url, *variants])


def candidates_many(
    urls: Iterable[str], *, kind: str | None = None, mode: str | None = None
) -> list[str]:
    """The ordered addresses to try for a list of official candidates (the version manifest).

    Mirrors are collected after the official addresses rather than interleaved, so "auto" really
    does try every official address before it leaves the official source.
    """
    official_urls = list(urls)
    if not official_urls:
        return []
    effective = mode if mode in MODES else current_mode(kind or source_kind(official_urls[0]))
    ordered: list[str] = []
    for tier in _tiers(effective):
        for url in official_urls:
            if tier == MODE_OFFICIAL:
                ordered.append(url)
            else:
                ordered.extend(_mirror_variants(url) or [url])
    return _dedupe(ordered)


def current_modes() -> dict[str, str]:
    """The two configured modes, read once (a download batch is one consistent decision)."""
    from launcher import config

    cfg, _ = config.load()
    modes: dict[str, str] = {}
    for kind, field in (
        (KIND_META, "download_source_meta"),
        (KIND_ASSETS, "download_source_assets"),
    ):
        value = getattr(cfg, field, MODE_DEFAULT)
        modes[kind] = value if value in MODES else MODE_DEFAULT
    return modes


def current_mode(kind: str) -> str:
    return current_modes()[kind if kind in (KIND_META, KIND_ASSETS) else KIND_META]


def attempt_timeout() -> httpx.Timeout:
    """The timeout used while a fallback exists: the first byte must arrive within 5 seconds."""
    return httpx.Timeout(FIRST_BYTE_TIMEOUT, connect=FIRST_BYTE_TIMEOUT)


def request_timeout(candidate_count: int) -> httpx.Timeout | None:
    """Per-request timeout override, or None to keep the client's historical timeout."""
    return attempt_timeout() if candidate_count > 1 else None


def source_label(url: str) -> str:
    """User-facing name of the source an address belongs to ("official" / "mirror")."""
    host = host_of(url)
    if host in MIRROR_HOSTS:
        return tr_core("source.mirror")
    if host in OFFICIAL_HOSTS:
        return tr_core("source.official")
    return host or tr_core("source.official")


def source_name(url: str) -> str:
    """``official (host)`` - enough to tell two sources apart, short enough for a log line."""
    return f"{source_label(url)} ({host_of(url)})"


def failure_reason(exc: BaseException) -> str:
    """Why one candidate failed, in words a user can act on."""
    if isinstance(exc, httpx.ReadTimeout):
        return tr_core("source.slow_first_byte", f"{FIRST_BYTE_TIMEOUT:g}")
    if isinstance(exc, httpx.HTTPError):
        return describe_network_error(exc)
    return str(exc) or type(exc).__name__


def failure_message(failures: Sequence[tuple[str, BaseException]]) -> str:
    """The error text after every candidate of a multiple-source attempt failed.

    It names each source and its reason, so "both were tried" is visible in the message instead of
    being implied (requirement: the error has to say that both sides were attempted).
    """
    parts = [f"{source_name(url)}: {failure_reason(exc)}" for url, exc in failures]
    return tr_core("source.all_failed", "; ".join(parts))


def note_switch(
    failed_url: str, exc: BaseException, next_url: str, *, context: str = ""
) -> None:
    """Record that this attempt moved on to another source, and why.

    Host names and the file name only: a full URL per switch would flood the log during an outage.
    """
    pair = (host_of(failed_url), host_of(next_url))
    with _switch_lock:
        first_time = pair not in _switch_pairs
        _switch_pairs.add(pair)
    message = "下载源切换: %s -> %s（原因: %s；文件: %s）"
    args = (source_name(failed_url), source_name(next_url), failure_reason(exc), context or "-")
    if first_time:
        logger.warning(message, *args)
    else:
        logger.debug(message, *args)


def note_used(url: str, *, switched: bool, context: str = "") -> None:
    """Record which source actually served a request, so "this time it used X" is in the log."""
    logger.info(
        "下载源: %s%s%s",
        source_name(url),
        "（回退后）" if switched else "",
        f"（文件: {context}）" if context else "",
    )


def reset_switch_log_for_tests() -> None:
    """Only for tests: let a test observe the first WARNING of a pair again."""
    with _switch_lock:
        _switch_pairs.clear()


def get(
    client: httpx.Client,
    urls: str | Iterable[str],
    *,
    kind: str | None = None,
    mode: str | None = None,
    context: str = "",
    **kwargs,
) -> httpx.Response:
    """GET the first address that answers, expanding a Mojang address through the resolver.

    ``urls`` may be one Mojang URL or several official candidates (the version manifest). The last
    transport error is re-raised unchanged so callers keep translating it with their own message;
    the switch itself is logged here, once, with its reason.
    """
    official = [urls] if isinstance(urls, str) else list(urls)
    ordered = candidates_many(official, kind=kind, mode=mode)
    timeout = request_timeout(len(ordered))
    if timeout is not None and "timeout" not in kwargs:
        kwargs["timeout"] = timeout
    last_error: httpx.HTTPError | None = None
    for index, url in enumerate(ordered):
        try:
            response = client.get(url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            last_error = exc
            if index + 1 < len(ordered):
                note_switch(url, exc, ordered[index + 1], context=context)
            continue
        note_used(url, switched=index > 0, context=context)
        return response
    assert last_error is not None  # candidates() never returns an empty list for a real URL
    raise last_error


def fetch_json(
    client: httpx.Client,
    urls: str | Iterable[str],
    *,
    kind: str | None = None,
    mode: str | None = None,
    context: str = "",
    **kwargs,
) -> dict:
    """GET + decode the first address that answers (version manifest)."""
    return get(client, urls, kind=kind, mode=mode, context=context, **kwargs).json()

