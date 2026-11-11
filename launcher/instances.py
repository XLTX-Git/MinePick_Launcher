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
"""Instance management: every installed version folder *is* an instance.

An instance is `<game dir>/versions/<id>/`, recognised by its version profile
(`launcher.meta.version.version_json_path`: `<id>/<id>.json` first, then a single JSON file under
any other name). A folder whose profile cannot be read - only a client jar is left - does not
disappear: it stays visible as an instance whose version information is missing, and launching it
reports exactly that. Nothing is duplicated: isolation only decides whether the game's working
directory is the instance folder itself (own saves/mods/config) or the shared game directory.

Optional metadata lives inside the instance folder as `minepick.json`
(display name, note, star flag, timestamps, isolation choice, the icon choice,
per-instance setting overrides and the version profile the instance points at).
Missing or corrupt metadata is tolerated: the folder structure stays the source
of truth.

The one field that is not cosmetic is ``base_version``: an instance normally runs
its own folder's profile (``versions/<id>/<id>.json``), but 修改 may point it at
another installed profile instead. That is a *pointer* - ``base_version`` names
the other version's folder, nothing is copied - and ``version_id`` is the id every
launch path resolves through.

The icon is optional too: ``""`` means "keep deriving it from the version"
(grass block, command block, gold block, cobblestone or the loader glyph),
a rule name pins one of those, and ``custom:<file>`` points at a picture inside
the instance folder. Only that relative name is stored, so renaming the instance
or moving the game folder cannot break it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from launcher import paths
from launcher.i18n import tr_core
from launcher.meta.version import (
    INSTANCE_MINECRAFT_FIELD,
    load_version_json,
    version_json_error,
    version_json_path,
)

INSTANCE_META_FILENAME = "minepick.json"  # per-instance metadata inside the folder
LEGACY_INSTANCES_DIRNAME = "instances"  # pre-0.3.0 layout, no longer used

#: Longest instance folder/display name the launcher accepts (see :func:`validate_name`).
MAX_INSTANCE_NAME_LENGTH = 32

#: Prefix that marks an icon as a user picture: ``custom:icon.png`` is ``<instance folder>/icon.png``.
#: The value is stored relative to the instance folder, never as an absolute path.
ICON_CUSTOM_PREFIX = "custom:"

# Isolation is a per-instance tri-state; "follow" uses the auto rule below.
ISOLATION_FOLLOW = "follow"
ISOLATION_ON = "on"
ISOLATION_OFF = "off"
ISOLATION_CHOICES = (ISOLATION_FOLLOW, ISOLATION_ON, ISOLATION_OFF)

_NAME_RE = re.compile(
    rf"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff ._-]{{0,{MAX_INSTANCE_NAME_LENGTH - 1}}}$"
)



class InstanceSettings(BaseModel):
    """Per-instance overrides; every field left empty follows the global setting."""

    model_config = ConfigDict(extra="ignore")

    java: str = ""  # explicit java executable; empty = auto-detect
    memory_gb: float | None = Field(default=None, gt=0, le=64)  # None = follow global
    jvm_args: str = ""  # appended to the global custom JVM args
    game_args: str = ""  # extra arguments for the game itself
    # Direct-connect address handed to the game as --server; empty = join no server. It is a per-instance
    # property because the server usually belongs to the instance (a modpack's own server, a test world),
    # and the launch page no longer keeps a field of its own: it reads this value when it launches.
    server: str = ""


class Instance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str  # folder name under versions/ (this is the version id too)
    display_name: str = ""  # shown in the UI; empty = use id
    note: str = ""
    star: bool = False
    created_at: float = 0.0
    last_played: float = 0.0
    isolated: str = ISOLATION_FOLLOW
    # Empty = automatic rule; a rule name ("grass" / "command" / "gold" / "cobble" / "forge" /
    # "fabric" / "neoforge" / "quilt"); "custom:<file>" = a picture inside the instance folder.
    # An unknown or missing value never hides the instance: the reader falls back to the rule.
    icon: str = ""
    #: The installed profile this instance runs, as a folder name under ``versions/``. Empty means
    #: "its own folder", which is the normal one-folder-per-version layout. 修改 sets it to another
    #: installed profile's id: the instance keeps every file it has and *points* at that profile
    #: instead, so nothing is duplicated and nothing is moved. A value naming a profile that is no
    #: longer installed is not silently ignored - launching fails with the usual missing-profile
    #: message, which is the truth about that state.
    base_version: str = ""
    settings: InstanceSettings = InstanceSettings()

    @property
    def name(self) -> str:
        """Display name (falls back to the folder name)."""
        return self.display_name or self.id

    @property
    def version_id(self) -> str:
        """The version profile this instance launches (``base_version``, else its own folder id).

        Every launch path resolves through this: the GUI instance page and ``launcher/cli.py`` both
        pass it to ``prepare_launch``, so switching the profile is one field away from the command
        line that is built.
        """
        return self.base_version or self.id


@dataclass(frozen=True)
class ResolvedInstance:
    """Every path/setting a launch or a resource view needs, resolved once."""

    id: str
    game_dir: Path  # shared game directory (versions/libraries/assets)
    launch_dir: Path  # effective working directory of the game
    isolated: bool
    java_path: Path | None  # None = auto-detect
    memory_gb: float
    jvm_args: str
    game_args: str
    mods_dir: Path
    memory_from_instance: bool = False  # True when the instance pins its own heap size
    server: str = ""  # direct-connect address; empty = no --server on the command line
    #: The profile this instance runs (see ``Instance.base_version``); empty = its own folder.
    base_version: str = ""

    @property
    def version_id(self) -> str:
        """The profile id to hand to ``prepare_launch`` (the pointer wins over the folder name)."""
        return self.base_version or self.id


class InstancesError(Exception):
    """Instance operation error (message is user-facing)."""


def _game_dir() -> Path:
    """The configured game directory (falling back to the official folder)."""
    from launcher import config

    cfg, _ = config.load()
    return cfg.game_dir or paths.default_game_dir()


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        raise InstancesError(tr_core("instances.name_invalid"))
    return name


def unique_instance_name(
    name: str,
    taken: Iterable[str],
    *,
    fallback: str = "instance",
) -> str:
    """The first folder name that is free: ``name``, else ``name-2``, ``name-3`` ...

    The suffix is the one the versions page already suggests (``suggest_name``), so an automatic
    duplicate and the advice under the name field say the same thing. ``taken`` may hold display
    names as well as folder ids; the comparison is case-insensitive like the Windows file system, so
    ``Mine`` cannot be created beside ``mine``. The result is always a name
    :func:`validate_name` accepts (32 characters at most, safe punctuation only).
    """
    reserved = {str(item).strip().casefold() for item in taken if str(item).strip()}
    base = (name or "").strip() or fallback
    base = base[:MAX_INSTANCE_NAME_LENGTH].strip() or fallback
    if base.casefold() not in reserved:
        return base
    index = 2
    while True:
        suffix = f"-{index}"
        candidate = base[: MAX_INSTANCE_NAME_LENGTH - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in reserved:
            return candidate
        index += 1



def default_instance_name(version_id: str) -> str:
    """Generate a default instance name from a version/profile id.

    Vanilla -> the version number itself (e.g. 1.20.1);
    mod profile -> version_loader_name_loader_version (e.g. 1.21.11_fabric_0.19.3).
    """
    if version_id.startswith("fabric-loader-"):
        parts = version_id.split("-")
        return parts[-1] + "_fabric_" + parts[2]
    if version_id.startswith("neoforge-"):
        base = version_id[len("neoforge-"):]
        parts = base.split(".")
        mc = ".".join(parts[:3]) if len(parts) >= 4 else "1." + ".".join(parts[:2])
        return mc + "_neoforge_" + base
    if "-forge-" in version_id:
        mc, lv = version_id.split("-forge-")
        return mc + "_forge_" + lv
    return version_id


def display_version_name(version_id: str) -> str:
    """Human-readable profile name (loader names capitalized).

    fabric-loader-0.15.11-1.20.1 -> "Fabric 0.15.11-1.20.1"
    neoforge-21.1.5            -> "NeoForge 21.1.5"
    1.20.1-forge-47.4.22       -> "Forge 47.4.22 (1.20.1)"
    quilt-loader-0.24.0-1.20.1 -> "Quilt 0.24.0-1.20.1"
    """
    if version_id.startswith("fabric-loader-"):
        parts = version_id.split("-")
        if len(parts) >= 4:
            return "Fabric " + parts[2] + "-" + parts[3]
        return "Fabric " + version_id[len("fabric-loader-"):]
    if version_id.startswith("quilt-loader-"):
        parts = version_id.split("-")
        if len(parts) >= 4:
            return "Quilt " + parts[2] + "-" + parts[3]
        return "Quilt " + version_id[len("quilt-loader-"):]
    if version_id.startswith("neoforge-"):
        return "NeoForge " + version_id[len("neoforge-"):]
    if "-forge-" in version_id:
        mc, lv = version_id.split("-forge-", 1)
        return "Forge " + lv + " (" + mc + ")"
    return version_id


def instance_dir(game_dir: Path, instance_id: str) -> Path:
    """The instance folder (identical to the version folder by design)."""
    return game_dir / "versions" / instance_id


def legacy_instances_dir(game_dir: Path) -> Path | None:
    """The pre-0.3.0 `instances/` folder, if it still exists (never migrated automatically)."""
    legacy = game_dir / LEGACY_INSTANCES_DIRNAME
    return legacy if legacy.is_dir() else None


def _read_sidecar(folder: Path, instance_id: str) -> Instance:
    """Read minepick.json; synthesize metadata from the folder when absent or corrupt."""
    try:
        created_at = folder.stat().st_mtime
    except OSError:
        created_at = time.time()
    inst = Instance(id=instance_id, created_at=created_at)
    meta_file = folder / INSTANCE_META_FILENAME
    if meta_file.exists():
        try:
            raw = json.loads(meta_file.read_text(encoding="utf-8-sig"))
            loaded = Instance.model_validate(raw)
            loaded.id = instance_id  # the folder name is the source of truth
            if not loaded.created_at:
                loaded.created_at = created_at
            return loaded
        except (ValueError, TypeError, OSError):
            logging.getLogger(__name__).warning("Ignoring corrupt instance metadata: %s", meta_file)
    return inst


def _write_sidecar(folder: Path, inst: Instance) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = inst.model_dump(mode="json")
    tmp = folder / (INSTANCE_META_FILENAME + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / INSTANCE_META_FILENAME)


def _normalize_icon(value: str) -> str:
    """The value stored in ``Instance.icon``: empty, a rule name, or a safe ``custom:<file>``.

    A custom picture has to live directly in the instance folder: a name carrying a separator or
    ``..`` is not a name this field can point at, so it degrades to "automatic" instead of becoming
    a path read. Unknown rule names are kept as written - the reader falls back to the automatic
    rule, which lets a future rule name survive an older build without losing the choice.
    """
    text = str(value or "").strip()
    if not text.startswith(ICON_CUSTOM_PREFIX):
        return text
    name = text[len(ICON_CUSTOM_PREFIX) :].strip()
    if not name or Path(name).name != name or name in (".", ".."):
        return ""
    return ICON_CUSTOM_PREFIX + name


def custom_icon_file(folder: Path, icon: str) -> Path | None:
    """The picture an ``icon`` value points at, if that is a file name inside ``folder``.

    Existence is deliberately not checked: the caller may be copying the file (and wants the
    source path even when it is about to fail) or displaying it (and wants a missing file to fall
    back to the rule instead of raising).
    """
    value = str(icon or "").strip()
    if not value.startswith(ICON_CUSTOM_PREFIX):
        return None
    name = value[len(ICON_CUSTOM_PREFIX) :]
    if not name or Path(name).name != name or name in (".", ".."):
        return None
    return Path(folder) / name


def _is_instance_folder(folder: Path) -> bool:
    """Whether a folder under ``versions/`` is an instance.

    The version profile is the official signal (:func:`version_json_path`: the folder's own name
    first, then a single JSON under any other name). A folder this launcher cannot read must not
    vanish from the cards either: our own sidecar or a client jar directly inside it keeps it
    visible as an instance, and the card then says the version information is missing. An empty
    folder is still not an instance.
    """
    if not folder.is_dir():
        return False
    if version_json_path(folder) is not None:
        return True
    if (folder / INSTANCE_META_FILENAME).is_file():
        return True
    try:
        return any(
            entry.is_file() and entry.suffix.casefold() == ".jar" for entry in folder.iterdir()
        )
    except OSError:
        return False


def list_instances(game_dir: Path | None = None) -> dict[str, Instance]:
    """Every installed version folder, keyed by instance id (== version id)."""
    root = game_dir or _game_dir()
    out: dict[str, Instance] = {}
    versions = root / "versions"
    if not versions.is_dir():
        return out
    for entry in sorted(versions.iterdir()):
        if _is_instance_folder(entry):
            out[entry.name] = _read_sidecar(entry, entry.name)
    return out


def get_instance(instance_id: str, game_dir: Path | None = None) -> Instance | None:
    return list_instances(game_dir).get(instance_id)


def _require(instance_id: str, game_dir: Path | None) -> tuple[Instance, Path]:
    root = game_dir or _game_dir()
    folder = instance_dir(root, instance_id)
    if not _is_instance_folder(folder):
        raise InstancesError(tr_core("instances.missing", instance_id))
    return _read_sidecar(folder, instance_id), folder


def update_instance(instance_id: str, *, game_dir: Path | None = None, **changes) -> Instance:
    """Update sidecar fields (display_name / note / star / last_played / isolated)."""
    inst, folder = _require(instance_id, game_dir)
    for key, value in changes.items():
        if key not in Instance.model_fields:
            raise InstancesError(tr_core("instances.field_unknown", key))
        setattr(inst, key, value)
    inst.id = instance_id
    _write_sidecar(folder, inst)
    return inst


def update_instance_note(instance_id: str, note: str, game_dir: Path | None = None) -> Instance:
    """Update the instance note (stored in the folder metadata)."""
    return update_instance(instance_id, game_dir=game_dir, note=(note or "").strip())


def set_instance_icon(instance_id: str, icon: str, game_dir: Path | None = None) -> Instance:
    """Set or clear the instance icon (stored in the folder metadata).

    ``""`` returns to the automatic rule, a rule name pins that icon, and ``custom:<file>`` points
    at a picture inside the instance folder. When the choice moves away from a launcher-managed
    copy, that copy is deleted so "restore automatic" leaves no orphan file behind; the picture is
    only removed when it really sits directly inside this instance folder.
    """
    inst, folder = _require(instance_id, game_dir)
    previous = inst.icon
    inst.icon = _normalize_icon(icon)
    _write_sidecar(folder, inst)
    old_file = custom_icon_file(folder, previous)
    if old_file is not None and old_file != custom_icon_file(folder, inst.icon):
        try:
            old_file.unlink()
        except OSError:
            logging.getLogger(__name__).warning(
                "Cannot remove the replaced instance icon: %s", old_file
            )
    return inst


def set_instance_settings(
    instance_id: str, *, game_dir: Path | None = None, **changes
) -> Instance:
    """Update per-instance setting overrides (java / memory_gb / jvm_args / game_args)."""
    inst, folder = _require(instance_id, game_dir)
    settings = inst.settings.model_copy(deep=True)
    for key, value in changes.items():
        if key not in InstanceSettings.model_fields:
            raise InstancesError(tr_core("instances.field_unknown", key))
        setattr(settings, key, value)
    inst.settings = settings
    _write_sidecar(folder, inst)
    return inst


def reset_instance_settings(instance_id: str, game_dir: Path | None = None) -> Instance:
    """Drop every per-instance override (isolation returns to the default policy)."""
    inst, folder = _require(instance_id, game_dir)
    inst.settings = InstanceSettings()
    inst.isolated = ISOLATION_FOLLOW
    _write_sidecar(folder, inst)
    return inst


def isolation_enabled(inst: Instance, folder: Path, default_isolation: bool) -> bool:
    """Resolve the tri-state isolation flag.

    Explicit on/off wins; otherwise an instance that already holds mods or saves
    is treated as isolated (so existing data keeps its own folder), and anything
    else follows the launcher default.
    """
    if inst.isolated == ISOLATION_ON:
        return True
    if inst.isolated == ISOLATION_OFF:
        return False
    for sub in ("mods", "saves"):
        directory = folder / sub
        try:
            if directory.is_dir() and any(directory.iterdir()):
                return True
        except OSError:
            continue
    return bool(default_isolation)


def resolve_instance(
    instance_id: str,
    cfg=None,
    game_dir: Path | None = None,
) -> ResolvedInstance:
    """Resolve every effective path and setting for one instance.

    This is the single place that decides "global or per-instance"; the launch
    path and the resource views never read the isolation config themselves.
    """
    if cfg is None:
        from launcher import config

        cfg, _ = config.load()
    root = game_dir or cfg.game_dir or paths.default_game_dir()
    inst, folder = _require(instance_id, root)
    isolated = isolation_enabled(inst, folder, getattr(cfg, "default_isolation", True))
    launch_dir = folder if isolated else root

    java_path: Path | None = None
    if inst.settings.java:
        candidate = Path(inst.settings.java)
        if candidate.is_file():
            java_path = candidate
        else:
            logging.getLogger(__name__).warning(
                "Instance %s pins a missing Java runtime, falling back to auto-detect: %s",
                instance_id,
                candidate,
            )

    memory_gb = inst.settings.memory_gb
    from_instance = memory_gb is not None
    if memory_gb is None:
        memory_gb = float(getattr(cfg, "memory_gb", 4.0))
    jvm_args = " ".join(part for part in (getattr(cfg, "jvm_args", "") or "", inst.settings.jvm_args or "") if part).strip()

    return ResolvedInstance(
        id=instance_id,
        game_dir=root,
        launch_dir=launch_dir,
        isolated=isolated,
        java_path=java_path,
        memory_gb=float(memory_gb),
        jvm_args=jvm_args,
        game_args=(inst.settings.game_args or "").strip(),
        mods_dir=launch_dir / "mods",
        memory_from_instance=from_instance,
        server=(inst.settings.server or "").strip(),
        base_version=inst.base_version,
    )


#: The two ways 修改 can re-point an instance: another Minecraft version, or another profile of the
#: same Minecraft version (a loader, or the same version without one).
SWITCH_VERSION = "version"
SWITCH_LOADER = "loader"


@dataclass(frozen=True)
class VersionIdentity:
    """What an installed version profile is, read from its folder id and its ``inheritsFrom`` chain.

    ``version_id`` is the *profile* (what ``base_version`` would store); ``minecraft`` and ``loader``
    are what a person reads on the modify page ("1.21.11" and "Fabric 0.19.3"). ``loader`` is empty
    for a vanilla profile - the label itself is not translated here, because loader names are proper
    nouns; the page supplies 原版 for the empty case. ``loader_id`` is the API identifier
    ("fabric" / "forge" / "neoforge" / "quilt") the resources page filters with.
    """

    version_id: str
    minecraft: str
    loader: str = ""
    loader_id: str = ""

    @property
    def is_loader(self) -> bool:
        return bool(self.loader)


#: Library coordinate prefix -> (loader id, display label). The version after the prefix is what the
#: loader actually is; Forge joins it to its Minecraft version (``1.20.1-47.4.22``), so the part after
#: the last dash is the loader build.
_LOADER_LIBRARIES = (
    ("net.fabricmc:fabric-loader:", "fabric", "Fabric", False),
    ("net.minecraftforge:forge:", "forge", "Forge", True),
    ("net.neoforged:neoforge:", "neoforge", "NeoForge", False),
)

#: ``mainClass`` markers for a loader profile whose libraries were stripped by a tool. The label has
#: no version then, which is still enough to filter and to grey a mod row correctly.
_LOADER_MAIN_CLASSES = (
    ("net.fabricmc.loader", "fabric", "Fabric"),
    ("net.neoforged", "neoforge", "NeoForge"),
    ("cpw.mods", "forge", "Forge"),
    ("net.minecraftforge", "forge", "Forge"),
)


def _known_profile(version_id: str) -> tuple[str, str, str] | None:
    """(minecraft version, loader label, loader id) for profile-id shapes this launcher installs."""
    if version_id.startswith("fabric-loader-"):
        parts = version_id.split("-")
        if len(parts) >= 4:
            return parts[3], "Fabric " + parts[2], "fabric"
    if version_id.startswith("quilt-loader-"):
        parts = version_id.split("-")
        if len(parts) >= 4:
            return parts[3], "Quilt " + parts[2], "quilt"
    if "-forge-" in version_id:
        minecraft, loader_version = version_id.split("-forge-", 1)
        return minecraft, "Forge " + loader_version, "forge"
    if version_id.startswith("neoforge-"):
        loader_version = version_id[len("neoforge-") :]
        numbers = loader_version.split(".")
        # NeoForge encodes the Minecraft version in its first two components (21.1.5 -> 1.21.1).
        minecraft = (
            f"1.{numbers[0]}.{numbers[1]}"
            if len(numbers) >= 2 and numbers[0].isdigit() and numbers[1].isdigit()
            else loader_version
        )
        return minecraft, "NeoForge " + loader_version, "neoforge"
    return None


def _loader_from_json(raw: dict) -> tuple[str, str]:
    """(loader id, label) a profile JSON itself names, or ("", "") when it is vanilla.

    A loader profile copied under a new folder id (the versions page now creates one per install)
    no longer says "fabric-loader" in its folder name. Its own ``libraries`` still name the loader
    and its build, so the identity survives the rename; ``mainClass`` is the fallback for a profile
    whose library list an external tool rewrote. A vanilla profile has neither, so the caller keeps
    following ``inheritsFrom``.
    """
    libraries = raw.get("libraries")
    if isinstance(libraries, list):
        for library in libraries:
            if not isinstance(library, dict):
                continue
            name = str(library.get("name") or "")
            for prefix, loader_id, label, dash_version in _LOADER_LIBRARIES:
                if name.startswith(prefix):
                    version = name[len(prefix) :].split(":")[0]
                    if dash_version:
                        version = version.rsplit("-", 1)[-1]
                    return loader_id, f"{label} {version}".strip()
    main_class = str(raw.get("mainClass") or "").lower()
    for marker, loader_id, label in _LOADER_MAIN_CLASSES:
        if marker in main_class:
            return loader_id, label
    return "", ""


def _profile_json(game_dir: Path, version_id: str) -> dict:
    """The profile's raw JSON (a missing or unreadable profile reads as an empty mapping)."""
    path = version_json_path(instance_dir(game_dir, version_id))
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _minecraft_below(game_dir: Path, version_id: str) -> str:
    """The Minecraft version named by the first recognisable id under ``version_id``."""
    seen: set[str] = set()
    current = version_id
    while current and current not in seen:
        seen.add(current)
        known = _known_profile(current)
        if known is not None:
            return known[0]
        parent = _profile_json(game_dir, current).get("inheritsFrom")
        if not isinstance(parent, str) or not parent.strip():
            return current
        current = parent.strip()
    return ""


def profile_identity(game_dir: Path, version_id: str) -> VersionIdentity:
    """Resolve ``version_id`` to the Minecraft version and loader a launch would actually run.

    The id's own shape is the first answer, because that is how this launcher names loader profiles;
    an id it does not recognise asks the profile JSON itself (``libraries`` / ``mainClass``), which is
    what a renamed loader instance needs, and finally follows ``inheritsFrom`` (the same walk the
    instances page does for its cards), which is what a hand-made or thin pack needs. A cycle, a
    missing parent or a sparse profile ends the walk instead of raising: the modify page must keep
    rendering.
    """
    seen: set[str] = set()
    current = version_id
    while current and current not in seen:
        seen.add(current)
        known = _known_profile(current)
        if known is not None:
            return VersionIdentity(
                version_id=version_id,
                minecraft=known[0],
                loader=known[1],
                loader_id=known[2],
            )
        raw = _profile_json(game_dir, current)
        tagged_minecraft = str(raw.get(INSTANCE_MINECRAFT_FIELD) or "").strip()
        if tagged_minecraft:
            loader_id, loader_label = _loader_from_json(raw)
            return VersionIdentity(
                version_id=version_id,
                minecraft=tagged_minecraft,
                loader=loader_label,
                loader_id=loader_id,
            )
        loader_id, loader_label = _loader_from_json(raw)
        if loader_id:
            parent = raw.get("inheritsFrom")
            minecraft = (
                _minecraft_below(game_dir, parent.strip())
                if isinstance(parent, str) and parent.strip()
                else ""
            )
            return VersionIdentity(
                version_id=version_id,
                minecraft=minecraft or current,
                loader=loader_label,
                loader_id=loader_id,
            )
        parent = raw.get("inheritsFrom")
        if not isinstance(parent, str) or not parent.strip():
            return VersionIdentity(version_id=version_id, minecraft=current)
        current = parent.strip()
    return VersionIdentity(version_id=version_id, minecraft=version_id)


def installed_identities(game_dir: Path) -> list[VersionIdentity]:
    """Every installed instance, described by what it runs (sorted, so the list is deterministic)."""
    identities = [
        profile_identity(game_dir, instance_id) for instance_id in list_instances(game_dir)
    ]
    return sorted(identities, key=lambda item: (item.minecraft.casefold(), item.version_id.casefold()))


def core_profile_id(game_dir: Path, version_id: str) -> str:
    """The profile folder the core is actually read from, following ``inheritsFrom``.

    An instance normally runs its own folder, but a folder may hold nothing but a thin profile that
    inherits a loader build (``红石包`` -> ``fabric-loader-0.19.3-1.21.11``). 修改 shows "which folder
    the core comes from", and naming the thin stub would be the wrong folder in exactly that case.
    The walk stops at the first id whose shape this launcher recognises (a loader build, which is
    where the jar/libraries come from) or at the end of the chain, and a cycle ends it like
    :func:`profile_identity` does. A missing parent returns the last readable id instead of raising:
    the page has to keep rendering a broken profile.
    """
    seen: set[str] = set()
    current = version_id
    last = version_id
    while current and current not in seen:
        seen.add(current)
        last = current
        if _known_profile(current) is not None:
            return current
        parent = _profile_json(game_dir, current).get("inheritsFrom")
        if not isinstance(parent, str) or not parent.strip():
            break
        current = parent.strip()
    return last


def switch_candidates(
    game_dir: Path, current_version_id: str, kind: str = SWITCH_VERSION
) -> list[VersionIdentity]:
    """The installed profiles 修改 may point this instance at, excluding the one it runs now.

    ``SWITCH_VERSION`` lists the *other* Minecraft versions (whatever loader they carry), and
    ``SWITCH_LOADER`` lists the profiles of the *same* Minecraft version - another loader, or the
    same version without one (which is how a loader is removed). Both answer from the local
    ``versions/`` folder only: nothing is downloaded and no profile is created.
    """
    current = profile_identity(game_dir, current_version_id)
    out: list[VersionIdentity] = []
    for identity in installed_identities(game_dir):
        if identity.version_id == current_version_id:
            continue
        if kind == SWITCH_LOADER:
            if identity.minecraft != current.minecraft:
                continue
        elif identity.minecraft == current.minecraft:
            continue
        out.append(identity)
    return out


def base_version_dependents(version_id: str, game_dir: Path | None = None) -> list[str]:
    """Instances that point at ``version_id`` through ``base_version`` (what deleting it breaks)."""
    root = game_dir or _game_dir()
    return sorted(
        instance.id
        for instance in list_instances(root).values()
        if instance.base_version == version_id and instance.id != version_id
    )


@dataclass(frozen=True)
class SwitchOutcome:
    """What a completed switch did: the instance afterwards, what it pointed at, and its backup."""

    instance: Instance
    previous_version: str  # "" when the instance ran its own folder before
    target: VersionIdentity
    backup_path: Path | None = None


def _read_sidecar_bytes(folder: Path) -> bytes | None:
    """The raw sidecar before a switch, for an exact rollback (None when there was no file)."""
    try:
        return (folder / INSTANCE_META_FILENAME).read_bytes()
    except OSError:
        return None


def _restore_sidecar_bytes(folder: Path, raw: bytes | None) -> None:
    """Put the sidecar back exactly as it was (delete it when there was none)."""
    path = folder / INSTANCE_META_FILENAME
    if raw is None:
        try:
            path.unlink()
        except OSError:
            pass
        return
    tmp = folder / (INSTANCE_META_FILENAME + ".rollback")
    tmp.write_bytes(raw)
    tmp.replace(path)


def _verify_switch(folder: Path, instance_id: str, target: str, game_dir: Path) -> None:
    """The write is only a switch if it reads back *and* the profile it names is readable."""
    if _read_sidecar(folder, instance_id).base_version != target:
        raise InstancesError(tr_core("instances.switch_verify_failed", target))
    profile = version_json_path(instance_dir(game_dir, target))
    if profile is None:
        raise InstancesError(tr_core("instances.switch_verify_failed", target))
    try:
        json.loads(profile.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise InstancesError(tr_core("instances.switch_verify_failed", target)) from exc


def _restore_from_archive(zip_path: Path, folder: Path) -> None:
    """Recreate the instance folder from its backup zip (the last resort of a failed switch).

    The archive is unpacked into a sibling first and only then swapped in, so a half-extracted
    restore can never be mistaken for the instance. Zip-slip entries are refused exactly like
    :func:`import_instance` refuses them.
    """
    staging = folder.with_name(folder.name + ".restore")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for entry in zf.namelist():
                if entry.endswith(("/", "\\")):
                    continue
                relative = Path(entry)
                if relative.is_absolute() or ".." in relative.parts:
                    raise InstancesError(tr_core("instances.import_invalid"))
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as source, destination.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(folder, ignore_errors=True)
    staging.replace(folder)


def switch_instance_version(
    instance_id: str,
    target_version_id: str,
    game_dir: Path,
    *,
    backup_dir: Path | None = None,
) -> SwitchOutcome:
    """Point one instance at another installed version profile.

    This is the whole of 切换, and it is deliberately one field: no file is copied, no folder is
    moved, and the instance's mods, saves and configuration stay exactly where they are. With
    ``backup_dir`` the current instance is exported to a zip *before* anything is written, so the
    user holds a complete copy when the profile is re-pointed.

    Failure is not allowed to leave a half-switched instance. The sidecar's exact previous bytes are
    read first, the new value is verified against the profile it names, and any error restores those
    bytes; if even that cannot be verified, the zip backup (when there is one) becomes the instance
    again. ``InstancesError`` says which of the two happened.
    """
    inst, folder = _require(instance_id, game_dir)
    target = instance_dir(game_dir, target_version_id)
    if version_json_path(target) is None:
        raise InstancesError(tr_core("instances.switch_target_missing", target_version_id))
    if target_version_id == inst.version_id:
        raise InstancesError(tr_core("instances.switch_same", target_version_id))

    backup_path: Path | None = None
    if backup_dir is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = Path(backup_dir) / f"{instance_id}-{stamp}.zip"
        try:
            export_instance(instance_id, backup_path, game_dir)
        except (OSError, InstancesError) as exc:
            raise InstancesError(tr_core("instances.switch_backup_failed", str(exc))) from exc

    previous = inst.base_version
    raw = _read_sidecar_bytes(folder)
    try:
        inst.base_version = target_version_id
        _write_sidecar(folder, inst)
        _verify_switch(folder, instance_id, target_version_id, game_dir)
    except Exception as exc:  # every failure has to roll back, whatever it was
        restored = False
        try:
            _restore_sidecar_bytes(folder, raw)
            restored = _read_sidecar(folder, instance_id).base_version == previous
        except OSError:
            logging.getLogger(__name__).exception("Cannot restore the instance sidecar: %s", folder)
        if not restored and backup_path is not None:
            try:
                _restore_from_archive(backup_path, folder)
                restored = True
            except Exception:  # reported below, never masked
                logging.getLogger(__name__).exception(
                    "Cannot restore the instance from its backup: %s", backup_path
                )
        key = "instances.switch_failed" if restored else "instances.switch_rollback_failed"
        raise InstancesError(tr_core(key, str(exc))) from exc

    return SwitchOutcome(
        instance=_read_sidecar(folder, instance_id),
        previous_version=previous,
        target=profile_identity(game_dir, target_version_id),
        backup_path=backup_path,
    )


def create_instance(
    name: str | None,
    version_id: str,
    game_dir: Path,
    *,
    cache_dir: Path | None = None,
) -> Instance:
    """Create an instance by copying an installed profile under a new id.

    Used to keep several independent setups of the same game version; installing
    a version normally creates its instance implicitly (one folder per version).
    The source's icon choice follows the copy: a rule name is just metadata, a
    ``custom:`` picture is copied into the new folder under the same name. Notes,
    stars and setting overrides are deliberately not copied.
    """
    name = validate_name(name or default_instance_name(version_id))
    target = instance_dir(game_dir, name)
    if target.exists():
        raise InstancesError(tr_core("instances.exists", name))
    gp = paths.GamePaths(game_dir)
    source = gp.version_dir(version_id)
    if not source.is_dir():
        raise InstancesError(tr_core("instances.source_missing", version_id))
    source_json = version_json_path(source)
    if source_json is None:
        raise InstancesError(version_json_error(source))

    target.mkdir(parents=True, exist_ok=True)
    raw = json.loads(source_json.read_text(encoding="utf-8"))
    raw["id"] = name  # the folder name is the profile id
    source_identity = profile_identity(game_dir, version_id)
    if source_identity.minecraft:
        raw[INSTANCE_MINECRAFT_FIELD] = source_identity.minecraft
    (target / (name + ".json")).write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    version = load_version_json(name, versions_dir=gp.versions_dir, cache_dir=cache_dir)
    dest_jar = target / version.client_jar_name
    for candidate in (source / (version_id + ".jar"), source / version.client_jar_name):
        if candidate.is_file():
            shutil.copyfile(candidate, dest_jar)
            break
    else:
        # A loader profile keeps no jar of its own: fill it from the parent version
        from launcher.mods.installer import _copy_game_jar

        _copy_game_jar(name, game_dir)

    # The icon follows a copy: a rule name is metadata only, a custom picture is copied into the new
    # folder (the same relative name keeps pointing at it). A picture that cannot be copied falls
    # back to the automatic rule rather than leaving the copy with a broken icon.
    icon = _read_sidecar(source, version_id).icon
    picture = custom_icon_file(source, icon)
    if picture is not None:
        try:
            shutil.copyfile(picture, target / picture.name)
        except OSError:
            logging.getLogger(__name__).warning(
                "Cannot copy the instance icon, falling back to the automatic rule: %s", picture
            )
            icon = ""

    instance = Instance(id=name, display_name=name, created_at=time.time(), icon=icon)
    _write_sidecar(target, instance)
    return instance


def rename_instance(instance_id: str, new_id: str, game_dir: Path) -> Instance:
    """Rename an instance: move the folder and rewrite the profile id."""
    instance_id = validate_name(instance_id)
    new_id = validate_name(new_id)
    old_dir = instance_dir(game_dir, instance_id)
    new_dir = instance_dir(game_dir, new_id)
    if not _is_instance_folder(old_dir):
        raise InstancesError(tr_core("instances.missing", instance_id))
    if new_dir.exists():
        raise InstancesError(tr_core("instances.dir_exists", str(new_dir)))

    inst = _read_sidecar(old_dir, instance_id)
    old_dir.rename(new_dir)
    # Rewrite the profile JSON: the id and its file name must follow the folder. A hand-made folder
    # may name the profile something else (``version.json``): it is read from wherever it lives and
    # then written under the official name. A folder without a readable profile (damaged install)
    # is still renamed - there is no JSON to rewrite, and a rename must never crash on that.
    profile = version_json_path(new_dir)
    if profile is not None:
        try:
            raw = json.loads(profile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logging.getLogger(__name__).warning(
                "Cannot rewrite the renamed instance profile: %s", profile
            )
            raw = None
        if isinstance(raw, dict):
            raw["id"] = new_id
            payload = json.dumps(raw, ensure_ascii=False, indent=2)
            if profile.name != new_id + ".json":
                profile.unlink()
                profile = new_dir / (new_id + ".json")
            profile.write_text(payload, encoding="utf-8")
    old_jar = new_dir / (instance_id + ".jar")
    if old_jar.is_file():
        old_jar.replace(new_dir / (new_id + ".jar"))
    inst.id = new_id
    if not inst.display_name or inst.display_name == instance_id:
        inst.display_name = new_id
    _write_sidecar(new_dir, inst)
    return inst


def delete_instance(instance_id: str, game_dir: Path) -> None:
    _, folder = _require(instance_id, game_dir)
    shutil.rmtree(folder, ignore_errors=True)


def export_instance(
    instance_id: str,
    dest_zip: Path,
    game_dir: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """Export the instance to a zip archive (files plus metadata).

    A custom icon travels inside the archive as ``icon.<ext>``; the sidecar keeps the relative
    ``custom:`` value, so the imported copy still resolves it.

    ``on_progress(done_bytes, total_bytes)`` is called while the archive is written - the byte count
    is what the archive will hold (metadata included), so the task page's bar is the real size of the
    job rather than a guess. ``should_stop()`` is polled between files: a True answer deletes the
    partial archive and raises, which is what the task row's 取消 needs. The zip is written as a
    ``.tmp`` and renamed at the end, so an interrupted export never leaves a half archive in place.
    """
    inst, source = _require(instance_id, game_dir)
    files = [
        path
        for path in source.rglob("*")
        # The folder's own sidecar is replaced by the freshly written root one
        if path.is_file() and path.name != INSTANCE_META_FILENAME
    ]
    payload = json.dumps(inst.model_dump(mode="json"), ensure_ascii=False, indent=2)
    total = len(payload.encode("utf-8"))
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_name(dest_zip.name + ".tmp")
    done = 0
    last_report = 0.0

    def report(force: bool = False) -> None:
        """Report the bytes written so far, throttled so a folder of small files cannot flood the UI."""
        nonlocal last_report
        if on_progress is None:
            return
        now = time.monotonic()
        if not force and now - last_report < 0.08:
            return
        last_report = now
        on_progress(min(done, total), total)

    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(INSTANCE_META_FILENAME, payload)
            done = len(payload.encode("utf-8"))
            report(force=True)
            for path in files:
                if should_stop is not None and should_stop():
                    raise InstancesError(tr_core("instances.export_cancelled"))
                zf.write(path, path.relative_to(source).as_posix())
                try:
                    done += path.stat().st_size
                except OSError:
                    pass
                report()
            report(force=True)
        tmp.replace(dest_zip)
    except BaseException:
        # A cancelled or failed export must not leave the half archive (or its .tmp) behind.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    return dest_zip


def import_instance(
    zip_path: Path, game_dir: Path, *, new_name: str | None = None
) -> Instance:
    """Import an instance from a zip: extract into the instance folder and write metadata."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if INSTANCE_META_FILENAME not in names:
            raise InstancesError(tr_core("instances.import_invalid"))
        try:
            meta = Instance.model_validate(json.loads(zf.read(INSTANCE_META_FILENAME)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise InstancesError(tr_core("instances.import_invalid")) from exc
        name = validate_name(new_name or meta.display_name or meta.id)
        target = instance_dir(game_dir, name)
        if target.exists():
            raise InstancesError(tr_core("instances.dir_exists", str(target)))
        target.mkdir(parents=True)
        for entry in names:
            if entry == INSTANCE_META_FILENAME or entry.endswith(("/", "\\")):
                continue
            rel = Path(entry)
            if ".." in rel.parts:  # zip-slip protection
                raise InstancesError(tr_core("instances.import_invalid"))
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(entry) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    # The zip may carry the source ids in file names; align them with the import name. The profile
    # is found under any JSON name (a hand-made ``version.json`` is a profile too) and normalised to
    # the official <name>.json so the folder name and the profile id agree.
    if meta.id != name:
        profile = version_json_path(target)
        if profile is not None:
            try:
                raw = json.loads(profile.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                raw["id"] = name
                payload = json.dumps(raw, ensure_ascii=False, indent=2)
                if profile.name != name + ".json":
                    profile.unlink()
                    profile = target / (name + ".json")
                profile.write_text(payload, encoding="utf-8")
        jar = target / (meta.id + ".jar")
        if jar.is_file():
            jar.replace(target / (name + ".jar"))

    inst = Instance(
        id=name,
        display_name=meta.display_name or name,
        note=meta.note or "",
        star=meta.star,
        created_at=meta.created_at or time.time(),
        last_played=meta.last_played,
        isolated=meta.isolated,
        icon=_normalize_icon(meta.icon),
        settings=meta.settings,
    )
    _write_sidecar(target, inst)
    return inst
