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
"""CurseForge files of a pack, resolved and downloaded through the existing CurseForge client.

A manifest names ``projectID``/``fileID`` only; the download URL, the real file name and the size
have to be asked for. There is no public "one file" call in :mod:`launcher.mods.curseforge`, so this
module uses that module's own request helpers (``_client`` / ``_get`` / ``_check``) rather than a
second HTTP stack: the API key gate, the 401/403 latch, the retry policy, the concurrency slots and
:func:`launcher.mods.curseforge.download_file` all stay exactly the ones the resources page uses.

The key gate is the important part: without an API key (or with a rejected one) a CurseForge pack
cannot be imported at all, and that is reported as :class:`CurseforgeKeyMissing` - a clear sentence
with the place to fix it - never as a silent skip of the files.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from launcher.i18n import describe_network_error, tr_core
from launcher.modpack.errors import CurseforgeKeyMissing, ModpackError
from launcher.modpack.model import PackFile
from launcher.mods import curseforge
from launcher.mods.curseforge import CfFile
from launcher.mods.loaders import ModsError

#: ``/v1/mods/{project}/files/{file}``: one file's metadata (name, size, download URL).
FILE_ENDPOINT = "/v1/mods/{project}/files/{file}"
#: Where a CurseForge file is placed. The manifest carries no path, and the file endpoint does not
#: report the project's class, so the extension is the honest available signal: a jar is a mod, a
#: zip is a resource pack.
_FOLDERS = {".jar": "mods", ".zip": "resourcepacks"}


def _key_missing() -> CurseforgeKeyMissing:
    return CurseforgeKeyMissing(tr_core("modpack.cf_key_missing"))


def _client():
    """A key-gated client, with "no key" translated into this feature's own message."""
    try:
        return curseforge._client()
    except ModsError as exc:  # the only reason _client() refuses is the key gate
        raise _key_missing() from exc


def resolve_file(pack_file: PackFile, *, client=None) -> CfFile:
    """Ask the API what ``projectID``/``fileID`` really is: file name, size and download URL."""
    own = client is None
    if own:
        client = _client()
    try:
        response = curseforge._get(
            client,
            curseforge.CURSEFORGE_API
            + FILE_ENDPOINT.format(project=pack_file.project_id, file=pack_file.file_id),
        )
        payload = curseforge._check(
            response,
            tr_core(
                "modpack.cf_file_failed", f"{pack_file.project_id}/{pack_file.file_id}"
            ),
        )
    except httpx.HTTPError as exc:
        raise ModpackError(
            tr_core("modpack.file_failed", f"{pack_file.project_id}/{pack_file.file_id}")
            + ": "
            + describe_network_error(exc)
        ) from exc
    except ModsError as exc:
        if curseforge.key_status() != curseforge.KEY_STATUS_SET:
            raise _key_missing() from exc
        raise ModpackError(str(exc)) from exc
    finally:
        if own:
            client.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ModpackError(
            tr_core("modpack.cf_file_failed", f"{pack_file.project_id}/{pack_file.file_id}")
        )
    url = str(data.get("downloadUrl") or "").strip()
    name = str(data.get("fileName") or "").strip()
    if not url or not name:
        raise ModpackError(
            tr_core("modpack.cf_file_failed", f"{pack_file.project_id}/{pack_file.file_id}")
        )
    return CfFile(
        id=int(data.get("id") or pack_file.file_id),
        name=name,
        url=url,
        size=int(data.get("fileLength") or 0),
    )


def destination(cf_file: CfFile) -> str:
    """The path a resolved file is written to inside the instance folder."""
    name = Path(cf_file.name).name
    folder = _FOLDERS.get(Path(name).suffix.casefold(), "mods")
    return folder + "/" + name


def download(cf_file: CfFile, target_dir: Path, *, client=None) -> Path:
    """Download one resolved file through the resources page's own downloader."""
    try:
        return curseforge.download_file(cf_file, Path(target_dir), client=client)
    except ModsError as exc:
        if curseforge.key_status() != curseforge.KEY_STATUS_SET:
            raise _key_missing() from exc
        raise ModpackError(str(exc)) from exc
