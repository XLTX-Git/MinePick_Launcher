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

"""Mod and loader data models."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict


class LoaderVersion(BaseModel):
    """Loader version (with installer download info)."""

    model_config = ConfigDict(extra="ignore")

    loader: str  # fabric / forge / neoforge
    version: str
    game_version: str
    stable: bool = True
    recommended: bool = False
    installer_url: str = ""
    #: ISO publication stamp, when the source publishes one. Fabric's meta, Forge's promotions file
    #: and NeoForge's maven metadata carry no per-build date, so this stays empty there - and the UI
    #: then shows the version alone rather than inventing a date. A source that does know the date
    #: fills it, and the same rows print it.
    release_time: str = ""


class ModFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str
    url: str
    size: int = 0
    sha1: str | None = None
    sha512: str | None = None
    primary: bool = False


class ModDependency(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str = ""
    slug: str = ""
    dependency_type: str = ""  # required / optional / incompatible / embedded


class ModVersion(BaseModel):
    """A version of a Modrinth project."""

    model_config = ConfigDict(extra="ignore")

    version_id: str
    version_number: str = ""
    loaders: list[str] = []
    game_versions: list[str] = []
    files: list[ModFile] = []
    dependencies: list[ModDependency] = []
    #: ISO-8601 publication stamp from the API ("" when the source did not say). Display-only.
    date_published: str = ""


class ModInfo(BaseModel):
    """UI-facing mod info; dependencies are only suggested, not force-installed."""

    model_config = ConfigDict(extra="ignore")

    slug: str
    title: str
    description: str = ""
    depends: list[str] = []  # required dependencies (list of slugs)
    optional_depends: list[str] = []  # optional dependencies (list of slugs)


class ModSearchHit(BaseModel):
    """A Modrinth / CurseForge search-result entry.

    ``updated`` and ``loaders`` are display-only metadata for the resource list: both APIs return them
    in the search payload already, and a row can show "when was it last touched" and "which loaders it
    supports" without a second request per result. They stay optional (empty = unknown): a project
    lookup may not carry them, and a resource pack has no loader.
    """

    model_config = ConfigDict(extra="ignore")

    slug: str
    title: str
    description: str = ""
    downloads: int = 0
    icon_url: str = ""
    updated: str = ""  # ISO-8601 timestamp from the source API, "" when unknown
    loaders: list[str] = []  # lowercase loader ids the project supports, [] when unknown / not a mod
    #: Where the hit came from ("modrinth" / "curseforge"). The "All" source merges both lists
    #: without de-duplicating them, so every row has to carry its own source label.
    source: str = ""


class SearchHits(list[ModSearchHit]):
    """One page of hits plus the source's total match count (0 when the source does not report it).

    A list subclass keeps every existing caller working (the resource list, the preview tools and
    the old tests all iterate or compare it as a list) while paging can ask ``total`` to decide
    whether a next page exists. The tools stub ``mods_page.search_projects`` with a plain list, so
    readers must treat ``total = 0`` as "unknown" rather than "no matches".
    """

    def __init__(self, hits: Iterable[ModSearchHit] = (), total: int = 0) -> None:
        super().__init__(hits)
        self.total = int(total or 0)


class InstalledContent(BaseModel):
    """Installed content-file entry (mods / resourcepacks / shaderpacks)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    path: str
    size: int = 0
