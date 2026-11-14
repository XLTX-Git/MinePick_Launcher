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

"""Resolve a premium player's skin texture URL from their UUID.

A Minecraft profile only carries a UUID; the skin itself is published as a texture URL inside
Mojang's public session server answer. The launcher's account pages get the URL from the Microsoft
profile response, but a *room member* arrives with nothing but what Terracotta reports, so this
module fills that one gap: UUID in, ``textures.minecraft.net`` URL out.

Only premium accounts have such a profile. Offline accounts (and every member Terracotta reports
today, because its ``profiles`` entries carry no UUID) never get here and keep the letter avatar.
The answer is cached per process and failures are remembered too: a room list re-renders on every
state change, and a member with no reachable skin must not turn into a request storm.

Every function returns ``""`` instead of raising. The caller runs this on a worker thread; a
failed lookup is a fallback to the letter, not a page error.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import uuid as uuidlib

logger = logging.getLogger(__name__)

#: Mojang's public profile endpoint. No authentication and no API key; one call per premium UUID.
PROFILE_URL = "https://sessionserver.mojang.com/session/minecraft/profile/{}"

#: Skin URLs come back as ``http://textures.minecraft.net/...``; the CDN serves the same path on TLS.
_TEXTURE_HOST = "http://textures.minecraft.net/"
_TEXTURE_HOST_TLS = "https://textures.minecraft.net/"

_url_cache: dict[str, str] = {}
_failed: set[str] = set()
_lock = threading.Lock()


#: Name -> UUID. Terracotta reports a member's name and nothing else, so a skin needs one more hop.
_NAME_LOOKUP_URL = "https://api.mojang.com/users/profiles/minecraft/{name}"
_name_cache: dict[str, str] = {}
_MISS = ""  # a name that does not resolve is cached as an empty string


def resolve_uuid_by_name(name: str, *, timeout: float = 6.0) -> str:
    """The Mojang UUID for a player name, or "" when the name is not a premium account.

    Terracotta's room profile carries ``machine_id, name, vendor, kind`` and no UUID, so a member who
    did not launch through this launcher arrives with a name only. Mojang's profile endpoint turns that
    name into the UUID the session server needs, which is the difference between a letter disc and a
    real face.

    A name that does not resolve is a normal answer, not an error: offline servers hand out names that
    were never registered. The cost is that an offline name which happens to match somebody's premium
    account shows that account's skin - unavoidable without a UUID from the room itself.
    """
    key = (name or "").strip().lower()
    if not key:
        return ""
    if key in _name_cache:
        return _name_cache[key]
    try:
        import httpx

        response = httpx.get(_NAME_LOOKUP_URL.format(name=key), timeout=timeout, follow_redirects=True)
        if response.status_code == 204 or response.status_code == 404:
            _name_cache[key] = _MISS
            return ""
        response.raise_for_status()
        payload = response.json()
        player_uuid = normalize_uuid(str(payload.get("id") or ""))
    except Exception:  # noqa: BLE001 - a lookup failure must never break the member row
        return ""
    _name_cache[key] = player_uuid
    return player_uuid


def normalize_uuid(value: str) -> str:
    """Canonical dashed UUID text, or ``""`` when the value is not a UUID at all."""
    text = str(value or "").strip().replace("-", "")
    if len(text) != 32:
        return ""
    try:
        return str(uuidlib.UUID(hex=text))
    except ValueError:
        return ""


def cached_skin_url(player_uuid: str) -> str:
    """The already resolved texture URL for ``player_uuid``, or ``""``."""
    key = normalize_uuid(player_uuid)
    if not key:
        return ""
    with _lock:
        return _url_cache.get(key, "")


def resolve_skin_url(player_uuid: str, *, timeout: float = 8.0) -> str:
    """Look the UUID up on Mojang's session server; ``""`` when it has no skin or the call fails."""
    key = normalize_uuid(player_uuid)
    if not key:
        return ""
    with _lock:
        cached = _url_cache.get(key)
        if cached:
            return cached
        if key in _failed:
            return ""
    url = ""
    try:
        from launcher.meta.manifest import _new_client

        client = _new_client()
        try:
            response = client.get(PROFILE_URL.format(key.replace("-", "")), timeout=timeout)
            if response.status_code == 200:
                url = _skin_url_from_profile(response.json())
        finally:
            client.close()
    except Exception:
        logger.debug("无法解析玩家皮肤: %s", key, exc_info=True)
    with _lock:
        if url:
            _url_cache[key] = url
        else:
            _failed.add(key)
    return url


def _skin_url_from_profile(payload: object) -> str:
    """The SKIN texture URL inside a session-server answer, or ``""``."""
    if not isinstance(payload, dict):
        return ""
    for prop in payload.get("properties") or []:
        if not isinstance(prop, dict) or prop.get("name") != "textures":
            continue
        value = str(prop.get("value") or "")
        try:
            decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
        except (ValueError, TypeError):
            continue
        skin = ((decoded.get("textures") or {}).get("SKIN") or {}) if isinstance(decoded, dict) else {}
        url = str(skin.get("url") or "") if isinstance(skin, dict) else ""
        if url.startswith(_TEXTURE_HOST):
            url = _TEXTURE_HOST_TLS + url[len(_TEXTURE_HOST):]
        if url.startswith("https://"):
            return url
    return ""


def remember_skin_url(player_uuid: str, skin_url: str) -> None:
    """Seed the cache (the screenshot renderer resolves its two demo skins up front).

    Not called by the application: production code always resolves through Mojang.
    """
    key = normalize_uuid(player_uuid)
    if key and skin_url:
        with _lock:
            _url_cache[key] = skin_url


def clear_cache() -> None:
    _name_cache.clear()
    """Forget every resolved URL and failure (the cache is process-wide on purpose)."""
    with _lock:
        _url_cache.clear()
        _failed.clear()
