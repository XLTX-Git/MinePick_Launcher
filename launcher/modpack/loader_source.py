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
"""Resolve the loader build a pack declares, from the sources this launcher already installs from.

The existing loader installer takes a :class:`launcher.mods.models.LoaderVersion` with an installer
URL. Fabric publishes every loader build (plus an installer jar) through its meta API, and
Forge/NeoForge use a predictable Maven layout, so a pack's exact build can be installed without a
second installer and without asking a third-party service:

* ``forge-47.2.0`` for 1.20.1 -> ``.../forge/1.20.1-47.2.0/forge-1.20.1-47.2.0-installer.jar``
* ``neoforge-21.1.5`` -> ``.../neoforge/21.1.5/neoforge-21.1.5-installer.jar``
* ``fabric 0.15.11`` -> the installer URL Fabric's meta reports (the installer is build-independent)

Only Fabric needs a request; it also has to answer *which* build, and a build Fabric no longer lists
falls back to its newest stable one - the same compatibility fallback
:func:`launcher.mods.modpacks.install_modpack` already uses.
"""

from __future__ import annotations

from launcher.i18n import tr_core
from launcher.modpack.errors import ModpackError
from launcher.mods.loaders import (
    FORGE_MAVEN_BASE,
    NEOFORGE_MAVEN_BASE,
    ModsError,
    list_loader_versions,
)
from launcher.mods.models import LoaderVersion

#: The loaders this launcher can actually install.
SUPPORTED_LOADERS = ("fabric", "forge", "neoforge")


def loader_installer(loader: str, version: str, minecraft: str) -> LoaderVersion:
    """The :class:`LoaderVersion` whose installer builds ``loader`` ``version`` for ``minecraft``."""
    loader = str(loader or "").strip().casefold()
    version = str(version or "").strip()
    minecraft = str(minecraft or "").strip()
    if not loader:
        raise ModpackError(tr_core("modpack.loader_unsupported", "?"))
    if loader == "fabric":
        try:
            versions = list_loader_versions("fabric", minecraft)
        except ModsError as exc:
            raise ModpackError(str(exc)) from exc
        chosen = next((item for item in versions if item.version == version), None)
        if chosen is None:
            if not versions:
                raise ModpackError(
                    tr_core("modpack.loader_version_missing", loader, version, minecraft)
                )
            chosen = versions[0]  # Fabric loaders are cross-version compatible; newest wins
        return chosen
    if loader == "forge":
        if not version:
            raise ModpackError(tr_core("modpack.loader_version_missing", loader, version, minecraft))
        return LoaderVersion(
            loader="forge",
            version=version,
            game_version=minecraft,
            installer_url=(
                FORGE_MAVEN_BASE + minecraft + "-" + version
                + "/forge-" + minecraft + "-" + version + "-installer.jar"
            ),
        )
    if loader == "neoforge":
        if not version:
            raise ModpackError(tr_core("modpack.loader_version_missing", loader, version, minecraft))
        return LoaderVersion(
            loader="neoforge",
            version=version,
            game_version=minecraft,
            installer_url=(
                NEOFORGE_MAVEN_BASE + version + "/neoforge-" + version + "-installer.jar"
            ),
        )
    raise ModpackError(tr_core("modpack.loader_unsupported", loader))
