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

"""The mod index of one instance (``CrashModIndex``).

A stack frame says ``com.example.foo.Bar.baz``; a crash report says
``Suspected Mods: example``; a filename says ``examplemod-1.2.3.jar``. All three
have to resolve to the *same* file before the launcher can honestly point at a
mod, so the index keeps four maps:

* ``id -> jar`` (``fabric.mod.json`` / ``META-INF/mods.toml``),
* a package-prefix map built from the jars' own class entries (longest prefix
  wins, so ``com.example.foo`` beats ``com.example``),
* a case-insensitive display-name map and file-name map,
* the *anomalies* of the directory itself: corrupt jars, unpacked leftovers,
  illegal file names, duplicate ids, id conflicts and missing Fabric
  dependencies.

Anomalies are exposed as a virtual ``mods`` log so the rule file stays plain
data: a rule matches ``invalid-jar:`` exactly like it matches a game log line.
The index never changes the directory; disabling a file is an action handed to
the UI, not something this module performs.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from launcher.diagnostics.context import DiagnosisContext
from launcher.diagnostics.prepare import KIND_MODS
from launcher.mods.local import DISABLED_SUFFIX, read_mod_metadata

_LOGGER = logging.getLogger(__name__)

#: Package roots that belong to the platform / a library, never to the mod that ships them.
PLATFORM_PREFIXES = (
    "java",
    "javax",
    "jdk",
    "sun",
    "com.mojang",
    "net.minecraft",
    "net.minecraftforge",
    "net.neoforged",
    "cpw.mods",
    "net.fabricmc",
    "org.quiltmc",
    "org.spongepowered",
    "org.lwjgl",
    "io.netty",
    "com.google",
    "org.apache",
    "it.unimi",
    "org.slf4j",
    "org.objectweb",
    "com.electronwill",
    "org.jetbrains",
    "kotlin",
    "scala",
)
#: Fabric pseudo-mods that a normal installation provides without a jar of their own.
PLATFORM_MOD_IDS = frozenset(
    {"minecraft", "java", "fabric", "fabricloader", "fabric-loader", "mixinextras", "quilt_loader"}
)
_ILLEGAL_NAME_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_LEFTOVER_DIRS = frozenset({"assets", "data", "meta-inf"})
_MAX_CLASS_PREFIXES = 64


@dataclass(frozen=True)
class JarMod:
    """One file in ``mods/`` and what its metadata says."""

    file: str  # on-disk name, including a .disabled suffix when disabled
    path: Path
    mod_id: str
    name: str
    version: str
    loader: str
    enabled: bool
    packages: tuple[str, ...] = ()
    depends: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()  # nested Fabric modules (jar-in-jar) this file supplies
    corrupt: str = ""  # reason when the file is not a readable zip

    @property
    def display(self) -> str:
        """The name a finding should show (metadata name first, the file without suffix second)."""
        return self.name or Path(self.file).stem.removesuffix(DISABLED_SUFFIX)


@dataclass(frozen=True)
class ModAnomaly:
    """One problem with a file (or with the directory as a whole)."""

    kind: str
    file: str
    detail: str = ""

    @property
    def line(self) -> str:
        return f"{self.kind}: {self.file}{f' ({self.detail})' if self.detail else ''}"


@dataclass(frozen=True)
class CrashModIndex:
    """The mods of one instance plus their anomalies (immutable)."""

    mods_dir: Path | None = None
    mods: tuple[JarMod, ...] = ()
    anomalies: tuple[ModAnomaly, ...] = ()
    _class_map: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    _name_map: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)

    # ---- construction ----
    @classmethod
    def empty(cls) -> CrashModIndex:
        return cls()

    @classmethod
    def from_context(cls, context: DiagnosisContext) -> CrashModIndex:
        mods_dir = context.resolved_mods_dir()
        return cls.scan(mods_dir) if mods_dir is not None else cls.empty()

    @classmethod
    def scan(cls, mods_dir: Path | None) -> CrashModIndex:
        if mods_dir is None:
            return cls.empty()
        root = Path(mods_dir)
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            _LOGGER.debug("diagnostics: cannot read the mods directory %s: %s", root, exc)
            return cls(mods_dir=root)

        mods: list[JarMod] = []
        anomalies: list[ModAnomaly] = []
        for entry in entries:
            lower = entry.name.lower()
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if lower.endswith((".jar", ".zip")) or lower in _LEFTOVER_DIRS:
                    anomalies.append(
                        ModAnomaly("leftover", entry.name, "unpacked directory in mods/")
                    )
                continue
            if not lower.endswith((".jar", ".jar.disabled", ".zip")):
                if lower.endswith((".class", ".part")):
                    anomalies.append(
                        ModAnomaly("leftover", entry.name, "unpacked file in mods/")
                    )
                continue
            problem = _illegal_name(entry.name)
            if problem:
                anomalies.append(ModAnomaly("illegal-name", entry.name, problem))
            mod = _read_jar(entry)
            mods.append(mod)
            if mod.corrupt:
                anomalies.append(ModAnomaly("invalid-jar", mod.file, mod.corrupt))
            elif not _has_metadata(mod):
                anomalies.append(ModAnomaly("missing-metadata", mod.file))

        installed = {
            mod.mod_id for mod in mods if mod.mod_id and mod.enabled
        } | {provided for mod in mods if mod.enabled for provided in mod.provides}
        for mod in mods:
            if not mod.enabled:
                continue
            for dependency in mod.depends:
                if dependency in PLATFORM_MOD_IDS or dependency in installed:
                    continue
                anomalies.append(
                    ModAnomaly("missing-dependency", mod.file, f"requires {dependency}")
                )

        by_id: dict[str, list[JarMod]] = {}
        for mod in mods:
            if mod.mod_id and mod.enabled:
                by_id.setdefault(mod.mod_id, []).append(mod)
        for mod_id, group in by_id.items():
            if len(group) < 2:
                continue
            files = ", ".join(mod.file for mod in group)
            anomalies.append(ModAnomaly("duplicate-id", mod_id, files))
            names = {mod.name.lower() for mod in group if mod.name}
            if len(names) > 1:
                anomalies.append(
                    ModAnomaly(
                        "id-conflict",
                        mod_id,
                        ", ".join(f"{mod.file}={mod.name}" for mod in group),
                    )
                )

        class_map: dict[str, str] = {}
        for mod in mods:
            for package in mod.packages:
                class_map.setdefault(package, mod.mod_id)
        name_map: dict[str, str] = {}
        for mod in mods:
            for key in (mod.name, Path(mod.file).name, Path(mod.file).stem.removesuffix(DISABLED_SUFFIX)):
                if key:
                    name_map.setdefault(key.lower(), mod.mod_id)
        index = cls(
            mods_dir=root,
            mods=tuple(mods),
            anomalies=tuple(anomalies),
            _class_map=tuple(sorted(class_map.items(), key=lambda item: -len(item[0]))),
            _name_map=tuple(sorted(name_map.items())),
        )
        _LOGGER.debug(
            "diagnostics: mod index %s: %d file(s), %d anomaly(ies)",
            root,
            len(mods),
            len(anomalies),
        )
        return index

    # ---- lookup ----
    def find_by_class(self, class_name: str) -> JarMod | None:
        """Resolve a stack frame's class to a jar (longest package prefix, then mod id segment)."""
        if not class_name:
            return None
        name = class_name.replace("/", ".").split("$", 1)[0].strip(".")
        best: str | None = None
        for prefix, mod_id in self._class_map:
            if name == prefix or name.startswith(prefix + "."):
                best = mod_id
                break
        if best is None:
            segments = [re.sub(r"[^a-z0-9]", "", part) for part in name.lower().split(".")]
            for mod in self.mods:
                key = re.sub(r"[^a-z0-9]", "", mod.mod_id.lower())
                if key and key in segments:
                    best = mod.mod_id
                    break
        if best is None:
            return None
        for mod in self.mods:
            if mod.mod_id == best:
                return mod
        return None

    def find_by_name(self, text: str) -> JarMod | None:
        """Resolve a display name / file name fragment (case-insensitive)."""
        needle = (text or "").strip().lower()
        if not needle:
            return None
        for key, mod_id in self._name_map:
            if key == needle:
                for mod in self.mods:
                    if mod.mod_id == mod_id:
                        return mod
        for key, mod_id in self._name_map:
            if key and key in needle:
                for mod in self.mods:
                    if mod.mod_id == mod_id:
                        return mod
        return None

    def by_id(self, mod_id: str) -> JarMod | None:
        for mod in self.mods:
            if mod.mod_id == mod_id:
                return mod
        return None

    @property
    def enabled_mods(self) -> tuple[JarMod, ...]:
        return tuple(mod for mod in self.mods if mod.enabled)

    def anomalies_of(self, kind: str) -> tuple[ModAnomaly, ...]:
        return tuple(anomaly for anomaly in self.anomalies if anomaly.kind == kind)

    @property
    def duplicate_ids(self) -> tuple[ModAnomaly, ...]:
        return self.anomalies_of("duplicate-id")

    @property
    def invalid_jars(self) -> tuple[ModAnomaly, ...]:
        return self.anomalies_of("invalid-jar")

    # ---- matcher view ----
    def as_logs(self):
        """The anomalies as one small ``mods`` log per affected file."""
        from launcher.diagnostics.logset import virtual_log

        grouped: dict[str, list[str]] = {}
        for anomaly in self.anomalies:
            grouped.setdefault(anomaly.file, []).append(anomaly.line)
        logs = []
        for file, lines in grouped.items():
            mod = next((item for item in self.mods if item.file == file), None)
            logs.append(
                virtual_log(
                    name=file,
                    kind=KIND_MODS,
                    lines=lines,
                    numbers=(0,) * len(lines),
                    path=str(mod.path) if mod else str(self.mods_dir or ""),
                )
            )
        return tuple(logs)

    def as_lines(self) -> tuple[str, ...]:
        """All anomaly lines (tests / debugging)."""
        return tuple(anomaly.line for anomaly in self.anomalies)


def _has_metadata(mod: JarMod) -> bool:
    return mod.loader not in ("", "unknown") and not mod.corrupt


def _illegal_name(name: str) -> str:
    """Reason the file name is unusable on a normal Windows installation."""
    stem = name.removesuffix(DISABLED_SUFFIX)
    if any(char in _ILLEGAL_NAME_CHARS for char in stem):
        return "illegal character"
    if any(ord(char) < 32 for char in stem):
        return "control character"
    if stem != stem.rstrip(" .") or name != name.rstrip(" \t"):
        return "trailing space or dot"
    if Path(stem).stem.upper() in _RESERVED_NAMES:
        return "reserved device name"
    if len(name) > 200:
        return "name too long"
    if ".." in name or "\x00" in name:
        return "path traversal"
    return ""


def _read_jar(path: Path) -> JarMod:
    """Read metadata + package prefixes + Fabric depends from one jar (cached by mtime/size)."""
    try:
        stat = path.stat()
    except OSError:
        return JarMod(
            file=path.name,
            path=path,
            mod_id=path.stem.removesuffix(DISABLED_SUFFIX),
            name="",
            version="",
            loader="unknown",
            enabled=not path.name.lower().endswith(DISABLED_SUFFIX),
            corrupt="unreadable file",
        )
    scan = _scan_cached(str(path), stat.st_mtime, stat.st_size)
    name, mod_id, version, loader = read_mod_metadata(path)
    return JarMod(
        file=path.name,
        path=path,
        mod_id=mod_id,
        name=name,
        version=version,
        loader=loader,
        enabled=not path.name.lower().endswith(DISABLED_SUFFIX),
        packages=scan[0],
        depends=scan[1],
        provides=scan[3],
        corrupt=scan[2],
    )


_SCAN_CACHE: dict[tuple[str, float, int], tuple[tuple[str, ...], tuple[str, ...], str, tuple[str, ...]]] = {}


def _scan_cached(
    path: str, mtime: float, size: int
) -> tuple[tuple[str, ...], tuple[str, ...], str, tuple[str, ...]]:
    key = (path, mtime, size)
    cached = _SCAN_CACHE.get(key)
    if cached is not None:
        return cached
    result = _scan_jar(path)
    if len(_SCAN_CACHE) > 512:
        _SCAN_CACHE.clear()
    _SCAN_CACHE[key] = result
    return result


def _scan_jar(path: str) -> tuple[tuple[str, ...], tuple[str, ...], str, tuple[str, ...]]:
    """Return (package roots, required Fabric deps, corruption reason, nested module ids)."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not names:
                return (), (), "empty archive", ()
            packages = _package_prefixes(names)
            depends = _fabric_depends(archive, names)
            provides = _nested_mod_ids(archive, names)
            return packages, depends, "", provides
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        return (), (), f"{type(exc).__name__}: {exc}"[:160], ()


def _package_prefixes(names: list[str]) -> tuple[str, ...]:
    """The package roots the jar ships classes under (bounded, deepest usable prefix).

    Taking only the first two components is too coarse for the common
    ``fi.dy.masa.<mod>`` layout: both Litematica and MaLiLib would claim
    ``fi.dy``. The full package path (up to four components) is used instead,
    so the longest-prefix map can tell the two jars apart.
    """
    counts: dict[str, int] = {}
    for entry in names:
        if not entry.endswith(".class") or entry.endswith("module-info.class"):
            continue
        parts = entry.split("/")
        if len(parts) < 2:
            continue
        package_parts = parts[:-1]
        if any(
            ".".join(package_parts).startswith(prefix + ".")
            or ".".join(package_parts) == prefix
            for prefix in PLATFORM_PREFIXES
        ):
            continue
        prefix = ".".join(package_parts[:4])
        counts[prefix] = counts.get(prefix, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(prefix for prefix, _count in ranked[:_MAX_CLASS_PREFIXES])


def _fabric_depends(archive: zipfile.ZipFile, names: list[str]) -> tuple[str, ...]:
    if "fabric.mod.json" not in names:
        return ()
    try:
        data = json.loads(archive.read("fabric.mod.json").decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError):
        return ()
    depends = data.get("depends") if isinstance(data, dict) else None
    if not isinstance(depends, dict):
        return ()
    return tuple(str(key) for key in depends if str(key) not in PLATFORM_MOD_IDS)


def _nested_mod_ids(archive: zipfile.ZipFile, names: list[str]) -> tuple[str, ...]:
    """Ids of the Fabric/Quilt modules nested in ``META-INF/jars`` (jar-in-jar).

    Fabric API ships its modules this way, so a dependency on
    ``fabric-lifecycle-events-v1`` is satisfied by the one ``fabric-api`` jar.
    Without this pass the missing-dependency rule would fire for every mod that
    depends on a Fabric API module.
    """
    prefix = "meta-inf/jars/"
    ids: dict[str, None] = {}
    for entry in names:
        lower = entry.lower()
        if not (lower.startswith(prefix) and lower.endswith(".jar")):
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(archive.read(entry))) as nested:
                nested_names = set(nested.namelist())
                for meta in ("fabric.mod.json", "quilt.mod.json"):
                    if meta not in nested_names:
                        continue
                    data = json.loads(nested.read(meta).decode("utf-8"))
                    if meta == "quilt.mod.json":
                        loader = data.get("quilt_loader") or {}
                        mod_id = str(loader.get("id") or "")
                    else:
                        mod_id = str(data.get("id") or "")
                    if mod_id:
                        ids.setdefault(mod_id, None)
        except (KeyError, ValueError, UnicodeDecodeError, zipfile.BadZipFile, OSError):
            continue
    return tuple(ids)


__all__ = [
    "PLATFORM_MOD_IDS",
    "PLATFORM_PREFIXES",
    "CrashModIndex",
    "JarMod",
    "ModAnomaly",
]
