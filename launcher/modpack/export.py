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
"""Exporting an instance as a *standard* modpack, not as a private zip.

The export page used to offer one thing: the whole instance folder as a zip. That archive is a
backup - a different launcher cannot read it as a pack. This module writes the two formats other
launchers do import, Modrinth ``.mrpack`` and a CurseForge zip, from the instance the launcher
already resolved:

1. :func:`plan_export` scans the instance (``resolve_instance`` gives ``launch_dir``/``mods_dir``,
   ``core_profile_id`` + ``profile_identity`` give the game version and loader). The scan is local
   and cheap: counts and sizes only, no jar is opened and no byte is hashed, so the page can show
   its summary before the user commits.
2. :func:`export_modpack` hashes every ``mods/*.jar`` (sha1 + sha512, stdlib ``hashlib``), asks the
   one source the launcher already talks to whether it knows that exact file - Modrinth for
   ``.mrpack``, CurseForge for a CF zip - and writes the archive through a ``.tmp`` that is renamed
   only on success.

The failure contract is the importer's, mirrored: a failed or cancelled export leaves no archive and
no ``.tmp`` behind, and every member name is checked with the same path guard the import side uses
(``launcher.modpack.archive.validate_member``), so an export can never write an absolute or escaping
path into its own zip.

Network is optional by design. The lookup is best-effort; when it fails, **every** mod goes into
``overrides/`` (a partial answer would make the pack's file list depend on which request happened to
time out), the result says so, and the pack still imports everywhere. A jar with no known source is
not an error: it is simply a file the pack carries instead of a URL it points at.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from launcher.i18n import tr_core
from launcher.instances import (
    core_profile_id,
    get_instance,
    profile_identity,
    resolve_instance,
)
from launcher.modpack import archive
from launcher.modpack.curseforge_export import MANIFEST_NAME, build_manifest
from launcher.modpack.curseforge_export import file_entry as curseforge_file_entry
from launcher.modpack.errors import ModpackError
from launcher.modpack.model import (
    FORMAT_CURSEFORGE,
    FORMAT_MODRINTH,
    PHASE_DONE,
    ModpackProgress,
)
from launcher.modpack.mrpack_export import INDEX_NAME, build_index
from launcher.modpack.mrpack_export import file_entry as mrpack_file_entry
from launcher.mods.local import read_mod_metadata
from launcher.net.downloader import DownloadCancelled

logger = logging.getLogger(__name__)

#: ``report(ModpackProgress)`` - called from the worker thread, so the page passes its bridge.
Report = Callable[[ModpackProgress], None]
#: ``should_stop()`` - polled between files and between hash chunks; True cancels the export.
StopCheck = Callable[[], bool]

#: Export phases. They are separate names from the import phases because the page prints a different
#: sentence for each one ("hashing" is not "downloading").
PHASE_HASH = "hash"
PHASE_MATCH = "match"
PHASE_WRITE = "write"

#: Read/hash chunk. 1 MiB is enough to keep the cancel flag responsive without a syscall per kilobyte.
HASH_CHUNK = 1 << 20
#: Progress is coalesced: a folder of a thousand small files must not queue a thousand signals.
REPORT_INTERVAL = 0.08

#: The content categories the export page offers, one checkbox each. The id is what the page stores
#: in its per-instance export preset; its label is a translation key owned by the GUI.
CATEGORY_MODS = "mods"
CATEGORY_CONFIG = "config"
CATEGORY_RESOURCEPACKS = "resourcepacks"
CATEGORY_SHADERPACKS = "shaderpacks"
CATEGORY_SAVES = "saves"
CATEGORY_SCREENSHOTS = "screenshots"
CATEGORY_SCHEMATICS = "schematics"
CATEGORY_SERVERS = "servers"
CATEGORY_OTHER = "other"

#: Every category, in the order the page shows them.
CATEGORIES: tuple[str, ...] = (
    CATEGORY_MODS,
    CATEGORY_CONFIG,
    CATEGORY_RESOURCEPACKS,
    CATEGORY_SHADERPACKS,
    CATEGORY_SAVES,
    CATEGORY_SCREENSHOTS,
    CATEGORY_SCHEMATICS,
    CATEGORY_SERVERS,
    CATEGORY_OTHER,
)

#: What a new export selects: the game content a pack is *for*. The user's own media (saves,
#: screenshots, schematics) and their private address book start out unchecked.
DEFAULT_CATEGORIES: frozenset[str] = frozenset(
    {CATEGORY_MODS, CATEGORY_CONFIG, CATEGORY_RESOURCEPACKS, CATEGORY_SHADERPACKS}
)

#: category -> directories a pack carries, relative to the instance's ``launch_dir``, with the
#: override tree each lands in. ``True`` marks the mods folder: only ``*.jar`` there is a mod
#: candidate, and a disabled jar (``*.jar.disabled``, this launcher's own suffix) is copied as a
#: plain override so a pack keeps it without enabling it.
CATEGORY_DIRS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    CATEGORY_MODS: (("mods", "overrides", True),),
    CATEGORY_CONFIG: (
        ("config", "overrides", False),
        ("defaultconfigs", "overrides", False),
    ),
    CATEGORY_RESOURCEPACKS: (("resourcepacks", "client-overrides", False),),
    CATEGORY_SHADERPACKS: (("shaderpacks", "client-overrides", False),),
    CATEGORY_SAVES: (("saves", "client-overrides", False),),
    CATEGORY_SCREENSHOTS: (("screenshots", "client-overrides", False),),
    CATEGORY_SCHEMATICS: (("schematics", "client-overrides", False),),
}

#: Root files that belong to a category. ``options.txt``/``optionsof.txt`` carry controls, not
#: accounts. ``servers.dat`` is a private address book and therefore its own, unchecked category.
CATEGORY_ROOT_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    CATEGORY_CONFIG: (
        ("options.txt", "client-overrides"),
        ("optionsof.txt", "client-overrides"),
    ),
    CATEGORY_SERVERS: (("servers.dat", "client-overrides"),),
}

#: Directories that are never part of 其他文件夹: the covered categories (handled above), the
#: launcher's own bookkeeping, and data the game regenerates (logs, crash reports). A pack that
#: carried those would be a bug report, not a pack.
OTHER_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        *(dirname for _category, dirs in CATEGORY_DIRS.items() for dirname, _tree, _mods in dirs),
        "logs",
        "crash-reports",
        "natives",
        "backups",
        "versions",
        "libraries",
        "assets",
        "cache",
        "runtime",
        ".minepick",
        "PCLInstallBackups",
    }
)

#: Loader name (as :func:`launcher.instances.profile_identity` reports it) -> importer vocabulary.
_LOADER_LABELS = {
    "fabric": "fabric",
    "quilt": "quilt",
    "forge": "forge",
    "neoforge": "neoforge",
}


def _match_key(text: object) -> str:
    """Case- and separator-insensitive comparison key (``Complementary Reimagined`` == ``complementary-reimagined``)."""
    return "".join(char for char in str(text or "").casefold() if char.isalnum())


def _split_profile_id(version_id: str) -> tuple[str, str]:
    """``fabric-loader-0.15.11-1.20.1`` -> ``("fabric", "0.15.11")``; ("", "") when unrecognised.

    The shapes are the ones this launcher itself creates (see
    :func:`launcher.instances._known_profile`); an id of another shape is not guessed at, because
    naming the wrong loader build would produce a pack that installs a different game than the
    instance runs.
    """
    vid = str(version_id or "").strip()
    if vid.startswith("fabric-loader-"):
        parts = vid.split("-")
        if len(parts) >= 4:
            return "fabric", parts[2]
    if vid.startswith("quilt-loader-"):
        parts = vid.split("-")
        if len(parts) >= 4:
            return "quilt", parts[2]
    if vid.startswith("neoforge-"):
        version = vid[len("neoforge-") :].strip()
        if version:
            return "neoforge", version
    if "-forge-" in vid:
        _minecraft, _sep, loader_version = vid.partition("-forge-")
        if loader_version.strip():
            return "forge", loader_version.strip()
    return "", ""


def loader_identity(game_dir: Path, version_id: str) -> tuple[str, str, str]:
    """(minecraft, loader, loader version) for a profile id, following ``inheritsFrom``.

    A thin instance folder (``红石包`` -> ``fabric-loader-0.19.3-1.21.11``) has no loader in its own
    name, so the id :func:`launcher.instances.core_profile_id` walks to is the one that is parsed.
    The profile's display label is the fallback for a shape this launcher did not create.
    """
    identity = profile_identity(game_dir, version_id)
    minecraft = identity.minecraft.strip()
    if not minecraft:
        raise ModpackError(tr_core("modpack.mc_missing"))
    for candidate in (core_profile_id(game_dir, version_id), version_id):
        loader, loader_version = _split_profile_id(candidate)
        if loader and loader_version:
            return minecraft, loader, loader_version
    label = identity.loader.strip()
    if not label:
        return minecraft, "", ""
    name, _sep, version = label.partition(" ")
    loader = _LOADER_LABELS.get(name.casefold(), "")
    if loader and version.strip():
        return minecraft, loader, version.strip()
    raise ModpackError(tr_core("modpack.loader_unsupported", label))


@dataclass(frozen=True)
class ExportEntry:
    """One file the scan decided to include: where it goes and where it comes from.

    ``override`` is the archive tree its bytes are copied into when it is not a ``files[]`` entry.
    A mod candidate carries ``is_mod=True`` and ``override`` still names the fallback tree: whether
    it becomes a download is decided later, by the lookup, not by the scan.
    """

    relative: str
    source: Path
    size: int
    override: str
    is_mod: bool = False
    #: The content category this file belongs to (one of :data:`CATEGORIES`). The export page's
    #: checkboxes are exactly this field: an unchecked category never reaches ``overrides/``.
    category: str = ""


@dataclass(frozen=True)
class CategoryStat:
    """How much of the instance one content category holds (file count and bytes)."""

    files: int
    bytes: int


@dataclass(frozen=True)
class ExportPlan:
    """What an export would contain, decided locally and before a byte is hashed.

    ``all_entries`` is the complete scan; ``entries`` is that scan filtered by the categories the
    user ticked. Keeping both is what lets the page show "N files · X MB" for a category it is *not*
    exporting, and change the summary the moment a checkbox flips, without rescanning the folder.
    """

    instance_id: str
    name: str
    minecraft: str
    loader: str
    loader_version: str
    summary: str
    fmt: str
    all_entries: tuple[ExportEntry, ...]
    #: The categories this plan exports. Defaults to the four a pack is made of; the page passes its
    #: checkboxes.
    categories: frozenset[str] = DEFAULT_CATEGORIES
    total_bytes: int = 0
    #: Files that were looked at but are not pack material (a non-jar in ``mods/``, an unreadable
    #: file). Reported so a surprising count has an explanation.
    skipped: int = 0

    @property
    def entries(self) -> tuple[ExportEntry, ...]:
        """The files this plan exports (the scan filtered by the ticked categories)."""
        return tuple(
            entry for entry in self.all_entries if entry.category in self.categories
        )

    def category_stats(self) -> dict[str, CategoryStat]:
        """Per-category count and size over the *whole* scan, for the page's category rows."""
        stats = {category: CategoryStat(0, 0) for category in CATEGORIES}
        for entry in self.all_entries:
            current = stats.get(entry.category)
            if current is None:
                continue
            stats[entry.category] = CategoryStat(
                current.files + 1, current.bytes + entry.size
            )
        return stats

    @property
    def mod_entries(self) -> tuple[ExportEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_mod)

    @property
    def mod_count(self) -> int:
        return sum(1 for entry in self.entries if entry.is_mod)

    @property
    def other_count(self) -> int:
        return len(self.entries) - self.mod_count

    @property
    def disabled_mods(self) -> int:
        return sum(1 for entry in self.entries if entry.relative.casefold().endswith(".jar.disabled"))


@dataclass(frozen=True)
class ModCandidate:
    """One local jar, as the lookup needs it: an identity and the hash it already has."""

    relative: str
    source: Path
    mod_id: str
    display_name: str
    sha1: str
    size: int


@dataclass(frozen=True)
class ModrinthFile:
    """The Modrinth file that matched a local jar."""

    url: str
    project: str = ""
    version_id: str = ""


@dataclass(frozen=True)
class MatchOutcome:
    """Result of one best-effort lookup over every mod candidate.

    ``failed`` means the source was unreadable (offline, API error, a bad answer). The caller then
    discards *every* match: a pack whose ``files[]`` depends on which request happened to time out is
    worse than a pack that carries all its mods in ``overrides/`` and says so.
    """

    matches: Mapping[str, ModrinthFile] = field(default_factory=dict)
    failed: bool = False


@dataclass(frozen=True)
class CurseforgeIds:
    """The CurseForge file a local jar was matched to."""

    project_id: int
    file_id: int


@dataclass(frozen=True)
class CurseforgeOutcome:
    """Result of the CurseForge lookup (same degradation rule as :class:`MatchOutcome`)."""

    matches: Mapping[str, CurseforgeIds] = field(default_factory=dict)
    failed: bool = False


@dataclass(frozen=True)
class ExportResult:
    """What a finished export wrote (what the page announces afterwards)."""

    path: Path
    fmt: str
    files: int
    overrides: int
    mods: int
    matched: int
    degraded: bool
    size: int
    #: True when 打包资源文件 was on: every file travelled in ``overrides/`` and ``files[]`` is empty.
    embedded: bool = False


@dataclass(frozen=True)
class _Hashes:
    sha1: str
    sha512: str
    size: int


class _Reporter:
    """Coalesced progress: cumulative worker bytes, plus the current phase's file counters."""

    def __init__(self, report: Report | None, total_bytes: int, total_files: int) -> None:
        self._report = report
        self._total_bytes = max(0, int(total_bytes))
        self._total_files = max(0, int(total_files))
        self._done_bytes = 0
        self._phase = ""
        self._phase_done = 0
        self._phase_total = 0
        self._last = 0.0

    def start(self, phase: str, total_files: int, current: str = "") -> None:
        """Begin a phase and report it at once (the page prints the new sentence immediately)."""
        self._phase = phase
        self._phase_total = max(0, int(total_files))
        self._phase_done = 0
        self._emit(current, force=True)

    def advance(self, count_bytes: int = 0, current: str = "") -> None:
        """One file finished: add its bytes and its slot, then report (throttled)."""
        self._done_bytes += max(0, int(count_bytes))
        self._phase_done += 1
        self._emit(current)

    def tick(self, count_bytes: int, current: str = "") -> None:
        """Bytes moved inside one file (hashing): report without advancing the file counter."""
        self._done_bytes += max(0, int(count_bytes))
        self._emit(current)

    def finish(self) -> None:
        """Report the last state, forced (a throttled final tick would leave the bar short)."""
        self._emit("", force=True)

    def _emit(self, current: str, *, force: bool = False) -> None:
        if self._report is None:
            return
        now = time.monotonic()
        if not force and now - self._last < REPORT_INTERVAL:
            return
        self._last = now
        self._report(
            ModpackProgress(
                phase=self._phase,
                done_files=self._phase_done,
                total_files=self._phase_total,
                done_bytes=min(self._done_bytes, self._total_bytes) if self._total_bytes else 0,
                total_bytes=self._total_bytes,
                current=current,
            )
        )


def _emit(report: Report | None, progress: ModpackProgress) -> None:
    if report is not None:
        report(progress)


def _check_stop(should_stop: StopCheck | None) -> None:
    """Raise the launcher's cooperative cancellation exception when the user stopped the export."""
    if should_stop is not None and should_stop():
        raise DownloadCancelled("cancelled")


def _override_tree(category: str, fmt: str) -> str:
    """The archive tree a non-download file lands in; CurseForge knows only ``overrides/``."""
    return "overrides" if fmt == FORMAT_CURSEFORGE else category


def _iter_files(folder: Path) -> Iterator[Path]:
    """Every regular, non-symlink file under ``folder`` (sorted, so the archive is deterministic)."""
    try:
        entries = sorted(folder.rglob("*"))
    except OSError as exc:
        logger.warning("cannot scan %s: %s", folder, exc)
        return
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except OSError:
            continue
        yield entry


def _other_dirs(launch_dir: Path) -> list[Path]:
    """Top-level folders the named categories do not cover, minus the ones a pack must not carry."""
    try:
        children = sorted(launch_dir.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for entry in children:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name.startswith(".") or entry.name in OTHER_EXCLUDED_DIRS:
            continue
        out.append(entry)
    return out


def _scan_entries(launch_dir: Path, fmt: str) -> tuple[list[ExportEntry], int]:
    """Every file a category could carry, plus how many files were deliberately left out.

    The scan is categorised, not filtered: the page needs the complete inventory to label all its
    checkboxes. Filtering happens in :meth:`ExportPlan.entries`.
    """
    entries: list[ExportEntry] = []
    skipped = 0
    for category, dirs in CATEGORY_DIRS.items():
        for dirname, override_tree, is_mods in dirs:
            folder = launch_dir / dirname
            if not folder.is_dir():
                continue
            override = _override_tree(override_tree, fmt)
            for path in _iter_files(folder):
                try:
                    size = path.stat().st_size
                except OSError:
                    skipped += 1
                    continue
                lowered = path.name.casefold()
                if is_mods and not lowered.endswith((".jar", ".jar.disabled")):
                    skipped += 1
                    continue
                entries.append(
                    ExportEntry(
                        relative=path.relative_to(launch_dir).as_posix(),
                        source=path,
                        size=size,
                        override=override,
                        is_mod=is_mods and lowered.endswith(".jar"),
                        category=category,
                    )
                )
    for category, files in CATEGORY_ROOT_FILES.items():
        for filename, override_tree in files:
            path = launch_dir / filename
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            entries.append(
                ExportEntry(
                    relative=filename,
                    source=path,
                    size=size,
                    override=_override_tree(override_tree, fmt),
                    category=category,
                )
            )
    for folder in _other_dirs(launch_dir):
        override = _override_tree("overrides", fmt)
        for path in _iter_files(folder):
            try:
                size = path.stat().st_size
            except OSError:
                skipped += 1
                continue
            entries.append(
                ExportEntry(
                    relative=path.relative_to(launch_dir).as_posix(),
                    source=path,
                    size=size,
                    override=override,
                    category=CATEGORY_OTHER,
                )
            )
    entries.sort(key=lambda entry: entry.relative.casefold())
    return entries, skipped


def _summary_line(note: str) -> str:
    """A one-line pack summary from the instance note (the format's ``summary`` is one line)."""
    for line in str(note or "").splitlines():
        text = line.strip()
        if text:
            return text[:256]
    return ""


def plan_export(
    instance_id: str,
    game_dir: Path,
    *,
    fmt: str = FORMAT_MODRINTH,
    categories: Iterable[str] | None = None,
) -> ExportPlan:
    """Scan one instance and describe the pack an export would write.

    Local only: ``mods/*.jar`` is counted, not opened; no hash is computed and no request is made.
    That is what lets the page show "N mods, up to M in overrides, total size" the moment the tab
    is opened, without a network round trip or a noticeable pause.

    ``categories`` is the page's checkbox set; None means the four a pack is made of. An empty set
    is allowed (the page shows it as "nothing to export") - the refusal happens when an *archive*
    would be written.
    """
    if fmt not in (FORMAT_MODRINTH, FORMAT_CURSEFORGE):
        raise ValueError(f"unsupported modpack format: {fmt!r}")
    selected = (
        DEFAULT_CATEGORIES if categories is None else frozenset(str(item) for item in categories)
    )
    unknown = selected - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unsupported modpack categories: {sorted(unknown)}")
    root = Path(game_dir)
    resolved = resolve_instance(instance_id, game_dir=root)
    instance = get_instance(instance_id, root)
    minecraft, loader, loader_version = loader_identity(root, resolved.version_id)
    entries, skipped = _scan_entries(resolved.launch_dir, fmt)
    return ExportPlan(
        instance_id=instance_id,
        name=(instance.display_name if instance is not None else "") or instance_id,
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
        summary=_summary_line(instance.note if instance is not None else ""),
        fmt=fmt,
        all_entries=tuple(entries),
        categories=frozenset(selected),
        total_bytes=sum(entry.size for entry in entries if entry.category in selected),
        skipped=skipped,
    )


def _hash_file(path: Path, *, on_chunk: Callable[[int], None] | None = None) -> _Hashes:
    """sha1 + sha512 + real size of one file, in one pass (the format needs both hashes)."""
    sha1 = hashlib.sha1()
    sha512 = hashlib.sha512()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            sha1.update(chunk)
            sha512.update(chunk)
            size += len(chunk)
            if on_chunk is not None:
                on_chunk(len(chunk))
    return _Hashes(sha1=sha1.hexdigest(), sha512=sha512.hexdigest(), size=size)


def _modrinth_file(modrinth, candidate: ModCandidate) -> ModrinthFile | None:
    """The Modrinth file whose sha1 is exactly this jar's, found through the project search.

    The project is matched by slug/title first (no fuzzy distance: a wrong file must never become a
    download), then *every* version's files are checked against the local hash. Matching by hash and
    not by version number is what makes an older installed build resolve as well as the newest one.
    """
    targets = {
        key
        for key in (_match_key(candidate.mod_id), _match_key(candidate.display_name))
        if key
    }
    if not targets:
        return None
    project = ""
    queries = [query for query in (candidate.mod_id, candidate.display_name) if query.strip()]
    for query in dict.fromkeys(queries):
        for hit in modrinth.search_projects(query, limit=5):
            if _match_key(hit.slug) in targets or _match_key(hit.title) in targets:
                project = str(hit.slug or "").strip()
                break
        if project:
            break
    if not project:
        return None
    for version in modrinth.fetch_versions(project):
        for item in version.files:
            url = str(item.url or "").strip()
            if not url:
                continue
            if str(item.sha1 or "").strip().casefold() == candidate.sha1:
                return ModrinthFile(
                    url=url, project=project, version_id=version.version_id
                )
    return None


def match_modrinth(
    candidates: Sequence[ModCandidate], minecraft: str, loader: str
) -> MatchOutcome:
    """Best-effort Modrinth lookup for every candidate; a failure degrades the whole pack.

    The imports are local because a GUI that never opens the export tab must not pay for them, and
    the broad ``except`` is the feature: an offline machine, a rate limit or a changed answer all end
    with the same honest result - the files travel in ``overrides/`` and the page says so.
    """
    if not candidates:
        return MatchOutcome()
    from launcher.mods import modrinth  # local: the page may never export

    matches: dict[str, ModrinthFile] = {}
    try:
        for candidate in candidates:
            found = _modrinth_file(modrinth, candidate)
            if found is not None:
                matches[candidate.relative] = found
    except Exception as exc:  # noqa: BLE001 - any lookup failure means "carry the files instead"
        logger.info("Modrinth lookup failed; every mod goes into overrides: %s", exc)
        return MatchOutcome(failed=True)
    return MatchOutcome(matches=matches)


def _curseforge_ids(
    curseforge, candidate: ModCandidate, minecraft: str, loader: str
) -> CurseforgeIds | None:
    """The CurseForge project/file pair for a jar, matched by project name + file name + size.

    There is no hash in CurseForge's manifest and its search API has no by-hash lookup in this
    application, so the file name and its exact size are the available evidence. A near miss is not
    accepted: a wrong ``fileID`` would install a different mod under the right name.
    """
    targets = {
        key
        for key in (_match_key(candidate.mod_id), _match_key(candidate.display_name))
        if key
    }
    if not targets:
        return None
    project_id = 0
    queries = [query for query in (candidate.mod_id, candidate.display_name) if query.strip()]
    for query in dict.fromkeys(queries):
        hits = curseforge.search_projects(
            query, limit=10, game_version=minecraft, loader=loader
        )
        for hit in hits:
            slug = str(hit.slug or "").strip()
            if slug.isdigit() and _match_key(hit.title) in targets:
                project_id = int(slug)
                break
        if project_id:
            break
    if not project_id:
        return None
    wanted = candidate.source.name.casefold()
    for item in curseforge.list_files(project_id, game_version=minecraft, loader=loader):
        if str(item.name or "").casefold() != wanted:
            continue
        if candidate.size > 0 and int(item.size or 0) != candidate.size:
            continue
        return CurseforgeIds(project_id=project_id, file_id=int(item.id))
    return None


def match_curseforge(
    candidates: Sequence[ModCandidate], minecraft: str, loader: str
) -> CurseforgeOutcome:
    """Best-effort CurseForge lookup; no key means "no ids", never an error."""
    if not candidates:
        return CurseforgeOutcome()
    from launcher.mods import curseforge  # local: the page may never export

    if not curseforge.has_key():
        logger.info("CurseForge export without an API key: every mod goes into overrides")
        return CurseforgeOutcome()
    matches: dict[str, CurseforgeIds] = {}
    try:
        for candidate in candidates:
            found = _curseforge_ids(curseforge, candidate, minecraft, loader)
            if found is not None:
                matches[candidate.relative] = found
    except Exception as exc:  # noqa: BLE001 - same degradation rule as Modrinth
        logger.info("CurseForge lookup failed; every mod goes into overrides: %s", exc)
        return CurseforgeOutcome(failed=True)
    return CurseforgeOutcome(matches=matches)


def _write_member(name: str) -> None:
    """The path guard for the write side: refuse an absolute or escaping member before writing it.

    Every name this module writes is built from ``Path.relative_to``, so it cannot be absolute. The
    guard is here anyway because the *format* is what a reader trusts: a future caller passing a
    name from elsewhere must fail before the archive exists, exactly like the import side fails
    before a byte is downloaded.
    """
    archive.validate_member(name)


def _candidate(entry: ExportEntry, sha1: str, size: int) -> ModCandidate:
    """Read a jar's in-file identity (``fabric.mod.json`` / ``mods.toml``) for the lookup.

    :func:`launcher.mods.local.read_mod_metadata` is the same reader the 模组 list uses, so a jar is
    named the same way in both places and a corrupt jar falls back to its file stem instead of
    failing the export.
    """
    display_name, mod_id, _version, _loader = read_mod_metadata(entry.source)
    return ModCandidate(
        relative=entry.relative,
        source=entry.source,
        mod_id=mod_id,
        display_name=display_name,
        sha1=sha1,
        size=size,
    )


def export_modpack(
    instance_id: str,
    dest: Path,
    game_dir: Path,
    *,
    fmt: str = FORMAT_MODRINTH,
    name: str = "",
    version: str = "1.0.0",
    categories: Iterable[str] | None = None,
    embed_files: bool = False,
    on_progress: Report | None = None,
    should_stop: StopCheck | None = None,
    matcher: Callable[[Sequence[ModCandidate], str, str], MatchOutcome] | None = None,
    curseforge_matcher: (
        Callable[[Sequence[ModCandidate], str, str], CurseforgeOutcome] | None
    ) = None,
) -> ExportResult:
    """Write one standard modpack for ``instance_id`` into ``dest``.

    ``matcher``/``curseforge_matcher`` exist so the tests - and a future "no network" switch - can
    replace the lookup without patching HTTP. The default is the real source the launcher already
    uses for that format.

    ``categories`` is the page's content selection (None = the four a pack is made of): an
    unchecked category is never scanned into the archive, so it cannot leak into ``overrides/``.

    ``embed_files`` is 打包资源文件: every file - mods included - travels in ``overrides/`` instead of
    being named in ``files[]``. It is the Modrinth answer to "the pack must not depend on any of its
    download sources still existing", and it skips hashing and the lookup entirely. CurseForge's
    manifest has no URL list at all, so the flag is ignored there (the page disables the checkbox
    and says why rather than pretending).

    Raises :class:`ModpackError` with a user-facing message, and ``DownloadCancelled`` when the user
    stopped it; in both cases no archive and no ``.tmp`` is left behind.
    """
    if fmt not in (FORMAT_MODRINTH, FORMAT_CURSEFORGE):
        raise ValueError(f"unsupported modpack format: {fmt!r}")
    plan = plan_export(instance_id, game_dir, fmt=fmt, categories=categories)
    embed = bool(embed_files) and fmt == FORMAT_MODRINTH
    if not plan.entries:
        raise ModpackError(tr_core("modpack.export_empty"))
    _check_stop(should_stop)

    destination = Path(dest)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModpackError(tr_core("modpack.export_failed", str(exc))) from exc
    tmp = destination.with_name(destination.name + ".tmp")
    mod_entries = plan.mod_entries
    hash_bytes = (
        sum(entry.size for entry in mod_entries)
        if fmt == FORMAT_MODRINTH and not embed
        else 0
    )
    total_work = plan.total_bytes + hash_bytes
    reporter = _Reporter(on_progress, total_work, len(plan.entries))

    hashes: dict[str, _Hashes] = {}
    candidates: list[ModCandidate] = []
    matched_hashes: dict[str, ModrinthFile] = {}
    matched_ids: dict[str, CurseforgeIds] = {}
    degraded = False

    try:
        if fmt == FORMAT_MODRINTH and not embed:
            reporter.start(PHASE_HASH, len(mod_entries))
            for entry in mod_entries:
                _check_stop(should_stop)

                def on_chunk(count: int, entry: ExportEntry = entry) -> None:
                    _check_stop(should_stop)
                    reporter.tick(count, entry.relative)

                digest = _hash_file(entry.source, on_chunk=on_chunk)
                hashes[entry.relative] = digest
                candidates.append(_candidate(entry, digest.sha1, digest.size))
                reporter.advance(0, entry.relative)

            reporter.start(PHASE_MATCH, len(candidates))
            try:
                outcome = (matcher or match_modrinth)(candidates, plan.minecraft, plan.loader)
            except Exception as exc:  # noqa: BLE001 - a lookup is never allowed to fail an export
                logger.info("Modrinth lookup failed; every mod goes into overrides: %s", exc)
                outcome = MatchOutcome(failed=True)
            _check_stop(should_stop)
            if outcome.failed:
                degraded = True
            else:
                matched_hashes = dict(outcome.matches)
        elif fmt == FORMAT_MODRINTH:
            # 打包资源文件: no hashing and no lookup. Every file goes into overrides/, which is the
            # only shape of a pack that cannot be broken by a source disappearing.
            reporter.start(PHASE_WRITE, len(plan.entries))
        else:
            candidates = [_candidate(entry, "", entry.size) for entry in mod_entries]
            reporter.start(PHASE_MATCH, len(candidates))
            try:
                outcome_cf = (curseforge_matcher or match_curseforge)(
                    candidates, plan.minecraft, plan.loader
                )
            except Exception as exc:  # noqa: BLE001 - same rule as the Modrinth lookup
                logger.info("CurseForge lookup failed; every mod goes into overrides: %s", exc)
                outcome_cf = CurseforgeOutcome(failed=True)
            _check_stop(should_stop)
            if outcome_cf.failed:
                degraded = True
            else:
                matched_ids = dict(outcome_cf.matches)

        matched = set(matched_hashes) | set(matched_ids)
        matched_records: list[tuple[str, dict]] = []
        for relative, found in matched_hashes.items():
            digest = hashes[relative]
            matched_records.append(
                (
                    relative,
                    mrpack_file_entry(
                        path=relative,
                        url=found.url,
                        sha1=digest.sha1,
                        sha512=digest.sha512,
                        size=digest.size,
                    ),
                )
            )
        for relative, found in matched_ids.items():
            matched_records.append(
                (relative, curseforge_file_entry(found.project_id, found.file_id))
            )
        matched_records.sort(key=lambda item: item[0].casefold())
        files = [record for _relative, record in matched_records]

        if fmt == FORMAT_MODRINTH:
            document = build_index(
                plan, files, name=name, version=version, summary=plan.summary
            )
            document_name = INDEX_NAME
        else:
            document = build_manifest(
                plan, files, name=name, version=version, description=plan.summary
            )
            document_name = MANIFEST_NAME

        reporter.start(PHASE_WRITE, len(plan.entries))
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            payload = json.dumps(document, ensure_ascii=False, indent=2)
            _write_member(document_name)
            zf.writestr(document_name, payload)
            for entry in plan.entries:
                _check_stop(should_stop)
                if entry.relative in matched:
                    # Not written: the pack points at it instead. Its size was already covered by
                    # the hash pass (mrpack) or by this pass's own budget (CurseForge, no hash).
                    reporter.advance(entry.size, entry.relative)
                    continue
                member = f"{entry.override}/{entry.relative}"
                _write_member(member)
                zf.write(entry.source, member)
                reporter.advance(entry.size, entry.relative)
        reporter.finish()
        tmp.replace(destination)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ModpackError(tr_core("modpack.export_failed", str(exc))) from exc
    except BaseException:
        # A cancelled or failed export must not leave the half archive (or its .tmp) behind. This is
        # the same contract the instance zip's export follows, for the same reason: a truncated
        # .mrpack is a file a user would try to import.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    overrides = sum(1 for entry in plan.entries if entry.relative not in matched)
    try:
        size = destination.stat().st_size
    except OSError:
        size = 0
    _emit(
        on_progress,
        ModpackProgress(
            phase=PHASE_DONE,
            done_files=len(files),
            total_files=len(files),
            done_bytes=total_work,
            total_bytes=total_work,
        ),
    )
    return ExportResult(
        path=destination,
        fmt=fmt,
        files=len(files),
        overrides=overrides,
        mods=plan.mod_count,
        matched=len(matched),
        degraded=degraded,
        size=size,
        embedded=embed,
    )
