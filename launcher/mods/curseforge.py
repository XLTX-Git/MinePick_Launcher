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

"""CurseForge content source: search / popular / file listing / download.

API key priority: the ``MCLAUNCHER_CF_KEY`` environment variable > the key entered in
the settings page > the bundled key injected at build time (build/cf_key.txt, kept out
of the repo and source tree to avoid leaking the key; neither the GUI nor the docs
display the key value). The environment variable is how a build or a CI run injects a
key without writing it into the user's config file - the same idea as HMCL's
``-Dhmcl.curseforge.apikey``.

A missing key is not an error state: :func:`available` returns False and the resources
page simply does not offer the source. A key the service rejects (401/403) latches the
source off for the rest of the process, so one bad key cannot cause a request per click.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from launcher import config, paths
from launcher.i18n import describe_network_error, tr_core
from launcher.meta.manifest import _new_client
from launcher.mods.loaders import ModsError
from launcher.mods.models import ModSearchHit, SearchHits

CURSEFORGE_API = "https://api.curseforge.com"
#: Environment variable that overrides the stored and bundled keys. Blank means unset, so an
#: exported-but-empty variable cannot silently disable a working key.
CF_KEY_ENV = "MCLAUNCHER_CF_KEY"
#: The states :func:`key_status` reports; the settings page maps each one to its own hint.
KEY_STATUS_MISSING = "missing"
KEY_STATUS_SET = "set"
KEY_STATUS_REJECTED = "rejected"
#: Set for the rest of the process once the service answers 401/403. The resources page hides the
#: source from then on, so a rejected key cannot cause one request per click.
_key_rejected = False
GAME_ID = 432  # Minecraft
# classId: mod 6 / resourcepack 12 / shaderpack 6552 / modpack 4471
CLASS_IDS = {"mod": 6, "resourcepack": 12, "shaderpack": 6552, "modpack": 4471}
# modLoaderType: Forge 1 / Fabric 4 / NeoForge 6 (mods only)
LOADER_IDS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
_LOADER_NAMES = {value: key for key, value in LOADER_IDS.items()}
#: The dropdown's semantic sort keys mapped to CurseForge's ``ModsSearchSortField`` values.
#:
#: CurseForge has no relevance field. For "relevance" the parameter is omitted on purpose: the API
#: then answers in its own match order (measured 2026-10-31: searching "entity rendering" returns the
#: two projects whose names contain both words first), while ``Popularity`` pushes unrelated popular
#: mods to the top. The other three map to the documented enum values.
SORT_FIELDS = {
    "relevance": None,
    "downloads": "TotalDownloads",
    "updated": "LastUpdated",
    "newest": "ReleasedDate",
}


@dataclass
class CfFile:
    """A CurseForge version file."""

    id: int
    name: str
    url: str
    size: int


def _bundled_key() -> str:
    """The bundled key injected at build time (read from build/ in dev mode; neither location is committed)."""
    for p in (
        paths.resource_path("launcher/mods/data/cf_key.txt"),
        paths.resource_path("build/cf_key.txt"),  # dev mode
    ):
        try:
            key = p.read_text(encoding="ascii", errors="ignore").strip()
            if key:
                return key
        except OSError:
            continue
    return ""


def effective_api_key() -> str:
    """The effective API key: environment first, then the user-entered, then the bundled default."""
    env_key = os.environ.get(CF_KEY_ENV, "").strip()
    if env_key:
        return env_key
    cfg, _ = config.load()
    return (cfg.curseforge_api_key or _bundled_key()).strip()


def has_key() -> bool:
    """Whether a key is configured (a key the service rejected still counts here)."""
    return bool(effective_api_key())


def key_status() -> str:
    """What the settings page should say: missing / set / rejected for this process."""
    if not effective_api_key():
        return KEY_STATUS_MISSING
    return KEY_STATUS_REJECTED if _key_rejected else KEY_STATUS_SET


def available() -> bool:
    """Whether the resources page may offer the CurseForge source right now."""
    return key_status() == KEY_STATUS_SET


def reset_session_state() -> None:
    """Clear the 401/403 latch.

    The latch is process-lifetime on purpose: after a rejection, every further request would get
    the same answer. Tests are the only caller that clears it (so one test cannot poison the next).
    """
    global _key_rejected
    _key_rejected = False


def _mark_key_rejected() -> None:
    """Latch the source off after the service refused the key."""
    global _key_rejected
    _key_rejected = True


def _require_key() -> str:
    """The key to send, or a refusal before any request is made."""
    if _key_rejected:
        raise ModsError(tr_core("mods.cf_key_invalid"))
    key = effective_api_key()
    if not key:
        raise ModsError(tr_core("mods.cf_key_missing"))
    return key


def _client() -> httpx.Client:
    key = _require_key()  # refuse before touching the shared pool, so "no key" costs no request
    client = _new_client()
    client.headers["x-api-key"] = key
    return client


def _check(resp: httpx.Response, message: str) -> dict:
    # 403 is the key the API rejects; 401 is a missing key. Both need the same advice, so one
    # message names the fix ("enter your own key in Settings") instead of only the HTTP status.
    if resp.status_code in (401, 403):
        _mark_key_rejected()
        raise ModsError(tr_core("mods.cf_key_invalid"))
    if resp.status_code == 429:
        raise ModsError(tr_core("mods.cf_rate_limited"))
    if resp.status_code >= 400:
        raise ModsError(message + tr_core("mods.cf_http_error", resp.status_code))
    return resp.json()


def search_projects(
    query: str,
    *,
    limit: int = 30,
    offset: int = 0,
    sort: str = "relevance",
    kind: str = "mod",
    game_version: str = "",
    loader: str = "",
    client: httpx.Client | None = None,
) -> SearchHits:
    """Search CurseForge projects by keyword.

    ``sort`` is one of :data:`SORT_FIELDS`' keys (see that table for the relevance approximation),
    ``offset``/``limit`` page the results, and the returned ``SearchHits`` carries ``totalCount``
    from the pagination block for the next-page decision.
    """
    own = client is None
    if own:
        client = _client()
    sort_field = SORT_FIELDS.get(sort, SORT_FIELDS["relevance"])
    params = {
        "gameId": str(GAME_ID),
        "classId": str(CLASS_IDS.get(kind, 6)),
        "searchFilter": query,
        "pageSize": str(min(limit, 50)),
        "index": str(max(0, offset)),
    }
    if sort_field:
        params["sortField"] = sort_field
        params["sortOrder"] = "desc"
    if game_version:
        params["gameVersion"] = game_version
    if loader and kind == "mod":
        params["modLoaderType"] = str(LOADER_IDS.get(loader, 0))
    try:
        resp = client.get(CURSEFORGE_API + "/v1/mods/search", params=params)
        payload = _check(resp, tr_core("mods.search_failed"))
    except httpx.HTTPError as exc:
        raise ModsError(tr_core("mods.search_failed", describe_network_error(exc))) from exc
    finally:
        if own:
            client.close()
    hits: list[ModSearchHit] = []
    for m in payload.get("data", []):
        if not isinstance(m, dict):
            continue
        logo = m.get("logo") or {}
        # `latestFilesIndexes` carries the game version / loader of each recent file; the distinct loader
        # ids are what the row shows as "loader range". A resource pack has no loader here.
        loaders: list[str] = []
        for item in m.get("latestFilesIndexes") or []:
            if not isinstance(item, dict):
                continue
            name = _LOADER_NAMES.get(item.get("modLoader"))
            if name and name not in loaders:
                loaders.append(name)
        hits.append(
            ModSearchHit(
                slug=str(m.get("id") or ""),  # the slug slot stores the CF project id
                title=m.get("name") or "",
                description=m.get("summary") or "",
                downloads=int(m.get("downloadCount") or 0),
                icon_url=logo.get("thumbnailUrl") or "",
                updated=str(m.get("dateModified") or ""),
                loaders=loaders,
                source="curseforge",
            )
        )
    total = int((payload.get("pagination") or {}).get("totalCount") or 0)
    return SearchHits(hits, total=total)


def list_files(
    mod_id: int,
    *,
    game_version: str = "",
    loader: str = "",
    client: httpx.Client | None = None,
) -> list[CfFile]:
    """List project files (release versions with a download URL only), newest first."""
    own = client is None
    if own:
        client = _client()
    params: dict[str, str] = {}
    if game_version:
        params["gameVersion"] = game_version
    if loader:
        params["modLoaderType"] = str(LOADER_IDS.get(loader, 0))
    try:
        resp = client.get(CURSEFORGE_API + "/v1/mods/" + str(mod_id) + "/files", params=params)
        payload = _check(resp, tr_core("mods.versions_failed"))
    except httpx.HTTPError as exc:
        raise ModsError(tr_core("mods.versions_failed", describe_network_error(exc))) from exc
    finally:
        if own:
            client.close()
    files: list[CfFile] = []
    for f in payload.get("data", []):
        if not isinstance(f, dict) or f.get("releaseType") != 1:
            continue
        url = f.get("downloadUrl")
        if not url:
            continue
        files.append(
            CfFile(
                id=int(f.get("id") or 0),
                name=f.get("displayName") or f.get("fileName") or "",
                url=url,
                size=int(f.get("fileLength") or 0),
            )
        )
    files.sort(key=lambda f: f.id, reverse=True)
    return files


def download_file(
    cf_file: CfFile,
    target_dir: Path,
    *,
    client: httpx.Client | None = None,
) -> Path:
    """Download the file into the target directory; return the on-disk path."""
    own = client is None
    if own:
        client = _client()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = cf_file.name or str(cf_file.id) + ".jar"
    target = target_dir / name
    try:
        with client.stream("GET", cf_file.url, follow_redirects=True) as resp:
            resp.raise_for_status()
            tmp = target.with_name(target.name + ".part")
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 18):
                    f.write(chunk)
            tmp.replace(target)
    except httpx.HTTPError as exc:
        raise ModsError(tr_core("mods.download_failed", describe_network_error(exc))) from exc
    finally:
        if own:
            client.close()
    return target

