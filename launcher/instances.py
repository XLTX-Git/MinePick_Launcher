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
(display name, note, star flag, timestamps, isolation choice, the icon choice and
per-instance setting overrides). Missing or corrupt metadata is tolerated: the
folder structure stays the source of truth.

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
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from launcher import paths
from launcher.i18n import tr_core
from launcher.meta.version import (
    load_version_json,
    version_json_error,
    version_json_path,
)

INSTANCE_META_FILENAME = "minepick.json"  # per-instance metadata inside the folder
LEGACY_INSTANCES_DIRNAME = "instances"  # pre-0.3.0 layout, no longer used

#: Prefix that marks an icon as a user picture: ``custom:icon.png`` is ``<instance folder>/icon.png``.
#: The value is stored relative to the instance folder, never as an absolute path.
ICON_CUSTOM_PREFIX = "custom:"

# Isolation is a per-instance tri-state; "follow" uses the auto rule below.
ISOLATION_FOLLOW = "follow"
ISOLATION_ON = "on"
ISOLATION_OFF = "off"
ISOLATION_CHOICES = (ISOLATION_FOLLOW, ISOLATION_ON, ISOLATION_OFF)

_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff ._-]{0,31}$")


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
    settings: InstanceSettings = InstanceSettings()

    @property
    def name(self) -> str:
        """Display name (falls back to the folder name)."""
        return self.display_name or self.id

    @property
    def version_id(self) -> str:
        """The version id equals the instance id (kept for call-site compatibility)."""
        return self.id


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

    @property
    def version_id(self) -> str:
        return self.id


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


def export_instance(instance_id: str, dest_zip: Path, game_dir: Path) -> Path:
    """Export the instance to a zip archive (files plus metadata).

    A custom icon travels inside the archive as ``icon.<ext>``; the sidecar keeps the relative
    ``custom:`` value, so the imported copy still resolves it.
    """
    inst, source = _require(instance_id, game_dir)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_name(dest_zip.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            INSTANCE_META_FILENAME,
            json.dumps(inst.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        for path in source.rglob("*"):
            # The folder's own sidecar is replaced by the freshly written root one
            if path.is_file() and path.name != INSTANCE_META_FILENAME:
                zf.write(path, path.relative_to(source).as_posix())
    tmp.replace(dest_zip)
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
