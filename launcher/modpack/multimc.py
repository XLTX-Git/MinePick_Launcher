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
"""MultiMC / Prism instance: ``mmc-pack.json``, ``instance.cfg`` and the ``.minecraft/`` folder.

MultiMC does not describe files, it *is* a folder: the loader components live in ``mmc-pack.json``
(``net.minecraft`` is the game, ``net.fabricmc.fabric-loader`` / ``net.minecraftforge`` /
``net.neoforged`` / ``org.quiltmc.quilt-loader`` the loader) and everything else is already in the
instance's ``.minecraft/`` directory. So a MultiMC import has no download list at all: the target
version and loader are installed by the launcher, and ``.minecraft/`` is copied in as the pack's own
content.
"""

from __future__ import annotations

import json
from pathlib import Path

from launcher.i18n import tr_core
from launcher.modpack.archive import common_root, open_zip, read_json
from launcher.modpack.errors import ModpackError
from launcher.modpack.model import FORMAT_MULTIMC, ModpackSpec

PACK_NAME = "mmc-pack.json"
CONFIG_NAME = "instance.cfg"
#: The game directory MultiMC keeps inside the instance folder.
GAME_DIRNAME = ".minecraft"
#: Component uid -> installer name. The game component is handled separately.
COMPONENT_LOADERS = {
    "net.fabricmc.fabric-loader": "fabric",
    "net.minecraftforge": "forge",
    "net.neoforged": "neoforge",
    "org.quiltmc.quilt-loader": "quilt",
}
GAME_COMPONENT = "net.minecraft"


def parse_instance_cfg(text: str) -> dict[str, str]:
    """Parse the ``key=value`` lines of an ``instance.cfg`` (comments and blank lines ignored)."""
    values: dict[str, str] = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")) or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _components(raw: dict) -> list[dict]:
    entries = raw.get("components")
    return [item for item in entries or [] if isinstance(item, dict)]


def _identity(components: list[dict]) -> tuple[str, str, str]:
    """(minecraft, loader, loader version) from the pack's components."""
    minecraft = ""
    loader = ""
    loader_version = ""
    for entry in components:
        uid = str(entry.get("uid") or "").strip()
        version = str(entry.get("version") or "").strip()
        if uid == GAME_COMPONENT and not minecraft:
            minecraft = version
        elif uid in COMPONENT_LOADERS and not loader:
            loader, loader_version = COMPONENT_LOADERS[uid], version
    return minecraft, loader, loader_version


def _build(
    *,
    raw: dict,
    config: dict[str, str],
    name: str,
    source: Path,
    source_is_dir: bool,
    overrides: tuple[str, ...],
) -> ModpackSpec:
    minecraft, loader, loader_version = _identity(_components(raw))
    if not minecraft:
        raise ModpackError(tr_core("modpack.mc_missing"))
    display = str(config.get("name") or "").strip() or name
    return ModpackSpec(
        fmt=FORMAT_MULTIMC,
        name=display,
        version="",
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        files=(),
        source=Path(source),
        source_is_dir=source_is_dir,
        overrides=overrides,
    )


def parse_multimc(path: Path) -> ModpackSpec:
    """Parse a MultiMC / Prism instance directory, or a zip of one."""
    source = Path(path)
    if source.is_dir():
        pack_file = source / PACK_NAME
        if not pack_file.is_file():
            raise ModpackError(tr_core("modpack.member_missing", PACK_NAME))
        try:
            raw = json.loads(pack_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ModpackError(tr_core("modpack.parse_failed", PACK_NAME)) from exc
        if not isinstance(raw, dict):
            raise ModpackError(tr_core("modpack.parse_failed", PACK_NAME))
        config_file = source / CONFIG_NAME
        try:
            config = parse_instance_cfg(config_file.read_text(encoding="utf-8-sig"))
        except OSError:
            config = {}
        return _build(
            raw=raw,
            config=config,
            name=source.resolve().name,
            source=source,
            source_is_dir=True,
            overrides=(GAME_DIRNAME,),
        )

    with open_zip(source) as zf:
        names = zf.namelist()
        root = common_root(names)
        pack_member = root + PACK_NAME
        if pack_member not in names:
            raise ModpackError(tr_core("modpack.member_missing", PACK_NAME))
        raw = read_json(zf, pack_member)
        config_member = root + CONFIG_NAME
        config: dict[str, str] = {}
        if config_member in names:
            try:
                config = parse_instance_cfg(zf.read(config_member).decode("utf-8-sig", "replace"))
            except (KeyError, OSError):
                config = {}
    return _build(
        raw=raw,
        config=config,
        name=source.stem,
        source=source,
        source_is_dir=False,
        overrides=(root + GAME_DIRNAME + "/",),
    )
