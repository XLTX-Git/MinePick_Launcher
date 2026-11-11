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

"""Resources page: mod search (Modrinth) / modpacks / resource packs / shader packs + installed content management."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui import i18n, inwindow_dialog, paint_utils, theme
from gui.errors import show_fatal
from gui.tasks import CANCELLED, KIND_TITLE_KEYS, Task, format_size, task_center_of
from gui.widgets import (
    EmptyState,
    add_page_top_inset,
    add_search_icon,
    apply_no_focus_outline,
    disable_keeping_focus,
    set_app_status,
    style_page_layout,
    widget_alive,
)
from gui.workers import (
    ProgressBridge,
    RateTracker,
    format_eta,
    format_rate,
    run_in_background,
)
from launcher import config, instances, paths
from launcher.mods import (
    ModsError,
    fetch_project,
    fetch_versions,
    install_mod,
    install_modpack,
    install_resourcepack,
    install_shaderpack,
    pick_file,
    resolve_slugs,
    search_projects,
)
from launcher.mods.loaders import display_loader_name
from launcher.mods.models import ModInfo, ModSearchHit, ModVersion, SearchHits
from launcher.mods.search import (
    ALL_PAGE_SIZE,
    PAGE_SIZE,
    PageOutcome,
    collect_all_sources,
    merge_same_titles,
    page_offset,
    rank_hits,
    source_has_next,
)
from launcher.mods.zh_names import english_query, has_cjk, search_local, zh_name
from launcher.net import downloader

tr = i18n.tr


def _curseforge_available() -> bool:
    """Whether the CurseForge source may be offered (a key is configured and not rejected).

    The answer is read from :mod:`launcher.mods.curseforge`, never cached here: the module owns the
    process-wide 401/403 latch, and the page re-reads it whenever it shows or reloads.
    """
    from launcher.mods import curseforge

    return curseforge.available()


#: The source dropdown starts on "All", like PCL's: browsing is a merged feed, and the row itself
#: still names the source each hit came from. Without a key "All" is simply the Modrinth feed.
DEFAULT_SOURCE = "all"

#: Sort choices. "Relevance" only exists while a keyword is being searched: browsing has nothing to
#: be relevant to, so the browse list offers Popular and Last updated only. Popular is Modrinth's
#: ``index=downloads`` and CurseForge's ``Popularity``; both mean "what people actually use".
SORT_OPTIONS_BROWSE: tuple[tuple[str, str], ...] = (
    ("downloads", "mods.popular"),
    ("updated", "mods.sort.updated"),
)
SORT_OPTIONS_SEARCH: tuple[tuple[str, str], ...] = (
    ("relevance", "mods.sort.relevance"),
    ("downloads", "mods.popular"),
    ("updated", "mods.sort.updated"),
    ("newest", "mods.sort.newest"),
)
DEFAULT_SORT_BROWSE = "downloads"
DEFAULT_SORT_SEARCH = "relevance"


# ---------- human-readable numbers for the result rows ----------

#: Chinese counts read in 万 / 亿, the rest of the interface uses the SI K / M / B suffixes. The unit
#: characters are part of the *number format* of those two languages, not translatable sentences, so
#: they live here next to the formatter instead of in the nine language tables.
_ZH_COUNT_UNITS: dict[str, tuple[str, str]] = {"zh_cn": ("万", "亿"), "zh_tw": ("萬", "億")}


def _trim_number(value: float) -> str:
    """Trim a formatted number to at most two decimals, without a trailing '.'."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def format_downloads(value: int, lang: str | None = None) -> str:
    """A download count a person can read: 2.88 亿 / 9230 万 (zh) or 231.45M (the rest).

    The list shows this instead of ``f"{value:,}"``; the exact number stays in the row's accessible
    text and tooltip so nothing is lost.
    """
    language = lang or i18n.current_language()
    count = max(0, int(value))
    units = _ZH_COUNT_UNITS.get(language)
    if units is not None:
        wan, yi = units
        if count >= 100_000_000:
            return _trim_number(count / 100_000_000) + yi
        if count >= 10_000:
            return _trim_number(count / 10_000) + wan
        return str(count)
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= size:
            return _trim_number(count / size) + suffix
    return str(count)


def _parse_timestamp(value: str) -> datetime | None:
    """Parse the ISO-8601 timestamp the mod APIs return; None when it is missing or malformed."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        # Python 3.11+ reads the trailing "Z" (both APIs use it) as UTC.
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _translate(lang: str, key: str, *args) -> str:
    """Look a key up in ``lang`` (English fallback), without switching the process-wide language."""
    table = i18n.TRANSLATIONS.get(lang) or i18n.TRANSLATIONS[i18n.EN]
    template = table.get(key) or i18n.TRANSLATIONS[i18n.EN].get(key, key)
    return template.format(*args) if args else template


def format_updated(value: str, *, lang: str | None = None, now: datetime | None = None) -> str:
    """Relative age of an ISO-8601 timestamp: "3 周前" / "3w ago"; "" when there is no timestamp.

    ``value`` is what the API returned (``date_modified`` / ``dateModified``); ``now`` is injectable so
    the function is deterministic in tests.
    """
    moment = _parse_timestamp(value)
    if moment is None:
        return ""
    language = lang or i18n.current_language()
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    seconds = int((current - moment).total_seconds())
    if seconds < 60:  # also covers a timestamp slightly in the future
        return _translate(language, "mods.ago.now")
    minutes = seconds // 60
    if minutes < 60:
        return _translate(language, "mods.ago.minutes", minutes)
    hours = minutes // 60
    if hours < 24:
        return _translate(language, "mods.ago.hours", hours)
    days = hours // 24
    if days < 7:
        return _translate(language, "mods.ago.days", days)
    if days < 30:
        return _translate(language, "mods.ago.weeks", days // 7)
    if days < 365:
        return _translate(language, "mods.ago.months", days // 30)
    return _translate(language, "mods.ago.years", days // 365)


def _tokens() -> dict[str, str]:
    """The colour table in force; light theme before the first apply (same rule as paint_utils)."""
    tokens = theme.current_tokens()
    return tokens or theme.theme_palette("light")


def display_titles(entry: object) -> tuple[str, str, bool]:
    """The two text lines of one result row: ``(title, second line, is Chinese title)``.

    A project that the bundled table knows keeps its Chinese name as the headline and the project's
    own English title underneath - the reverse would put a name the user did not search for on top.
    Everything else keeps the API's title and its description, exactly as before.
    """
    title = str(getattr(entry, "title", "") or getattr(entry, "slug", "") or "")
    description = str(getattr(entry, "description", "") or "")
    slug = str(getattr(entry, "slug", "") or "")
    chinese = zh_name(slug)
    if not chinese or chinese.casefold() == title.casefold():
        return title, description, False
    return chinese, title, True


def _fetch_icon_bytes(url: str) -> bytes:
    """One project icon for the background worker: disk cache first, then one small request.

    The store lives in ``<launcher data dir>/cache/mod-icons`` and is owned by
    :mod:`launcher.net.http_cache`: a hit is a file read, and an icon older than 30 days is still
    returned while a background request revalidates it.
    """
    from launcher.net.http_cache import fetch_icon_bytes

    return fetch_icon_bytes(url, timeout=8.0)


# ---------- where a downloaded file goes ----------

#: The folder a content kind lives in, per game folder. The keys are also the save-as memory keys in
#: ``launcher.config`` (``resource_dirs``), so a remembered folder always names the same kind.
CONTENT_SUBDIRS: dict[str, str] = {
    "mod": "mods",
    "resourcepack": "resourcepacks",
    "shaderpack": "shaderpacks",
}

#: The save dialog's filter per kind. A bare pattern is valid and language-independent; the dialog
#: prints it as the file type line itself.
_SAVE_FILTERS = {"mod": "*.jar", "resourcepack": "*.zip", "shaderpack": "*.zip"}
_SAVE_SUFFIX = {"mod": ".jar", "resourcepack": ".zip", "shaderpack": ".zip"}

#: The loader names the interface shows. ``display_loader_name`` owns the same four ids for the
#: Modrinth rows; the instance side needs the id itself (to compare a version's loaders).
_LOADER_LABELS = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}


def _safe_filename(name: str, fallback: str) -> str:
    """A file name a save dialog can pre-fill: no folder part and no Windows-reserved character.

    The source's own file name is kept byte-for-byte when it is already usable (``fabric-api-0.92.2
    +1.20.1.jar`` is a real name with a ``+``), so the user recognises the download. Only a name that
    cannot be a single file name is rewritten.
    """
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    for char in '<>:"|?*':
        text = text.replace(char, "_")
    text = "".join(char for char in text if ord(char) >= 32).strip(" .")
    return text or fallback


def version_key_of(version: object) -> str:
    """The source's identifier for one file version (Modrinth version id / CurseForge file id)."""
    if isinstance(version, ModVersion):
        return version.version_id
    return str(getattr(version, "id", "") or "")


def version_name_of(version: object) -> str:
    """What the version row prints as its headline."""
    if isinstance(version, ModVersion):
        return version.version_number or version.version_id
    return str(getattr(version, "name", "") or getattr(version, "id", "") or "")


def version_filename_of(version: object) -> str:
    """The file name a save dialog pre-fills (empty when the source did not name a file)."""
    if isinstance(version, ModVersion):
        file = pick_file(version)
        return file.filename if file is not None else ""
    return str(
        getattr(version, "save_name", "")
        or getattr(version, "filename", "")
        or getattr(version, "name", "")
        or ""
    )


def version_loaders_of(version: object) -> list[str]:
    """Loader ids the file version supports (empty = the source did not say)."""
    if isinstance(version, ModVersion):
        return [str(item) for item in version.loaders if str(item).strip()]
    return [str(item) for item in (getattr(version, "loaders", ()) or ()) if str(item).strip()]


def version_game_versions_of(version: object) -> list[str]:
    """Minecraft versions the file version supports (empty = the source did not say)."""
    if isinstance(version, ModVersion):
        return [str(item) for item in version.game_versions if str(item).strip()]
    return [
        str(item) for item in (getattr(version, "game_versions", ()) or ()) if str(item).strip()
    ]


def version_published_of(version: object) -> str:
    """The ISO publication stamp the version list shows, or "" when the source did not say."""
    if isinstance(version, ModVersion):
        return version.date_published
    return str(getattr(version, "published", "") or "")


def version_size_of(version: object) -> int:
    """Bytes of the file this version would download (0 when unknown)."""
    if isinstance(version, ModVersion):
        file = pick_file(version)
        return int(file.size) if file is not None else 0
    return int(getattr(version, "size", 0) or 0)


def _human_date(stamp: str) -> str:
    """The date part of an ISO timestamp (the resources page does not need the clock time)."""
    text = str(stamp or "").strip()
    return text[:10] if len(text) >= 10 else text


def _join_limited(items: list[str], limit: int = 4) -> str:
    """``a, b, c, d +2``: every distinct value until the line would stop being one line."""
    unique = list(dict.fromkeys(item for item in items if item))
    if len(unique) <= limit:
        return ", ".join(unique)
    return ", ".join(unique[:limit]) + f" +{len(unique) - limit}"


@dataclass(frozen=True)
class InstanceChoice:
    """One local instance, and whether one file version fits it.

    ``note`` is the sentence that refuses the choice (empty when the version fits): the save-as
    default folder only ever picks an instance whose note is empty. It is no longer drawn as a row -
    the install step is the version list now - but the same ``_loader_mismatch`` wording decides
    compatibility, which is the one rule the old instance picker got right.
    """

    game_dir: Path
    instance_id: str
    name: str
    loader_id: str
    loader_label: str
    minecraft: str
    isolated: bool
    mods_dir: Path
    folder: Path
    instance: object
    note: str = ""

    @property
    def version_id(self) -> str:
        """The version id shown beside the instance name (the folder *is* the version, by design)."""
        return self.instance_id

    @property
    def usable(self) -> bool:
        return not self.note

    def content_dir(self, kind: str) -> Path:
        """This instance's folder for ``kind``: its own mods/ when isolated, else the shared one."""
        if kind == "mod":
            return self.mods_dir
        subdir = CONTENT_SUBDIRS.get(kind)
        if subdir is None:
            return self.mods_dir
        return (self.folder if self.isolated else self.game_dir) / subdir


def _loader_mismatch(mod_loaders: list[str], loader_id: str, loader_label: str) -> str:
    """The sentence that refuses a version for an instance, or "" when it fits.

    An empty ``mod_loaders`` means "the source did not say", never "incompatible": a version
    without loader metadata must stay usable, otherwise a project whose metadata is sparse would
    have no default folder at all.
    """
    supported = {str(item).strip().lower() for item in mod_loaders if str(item).strip()}
    if not supported or loader_id in supported:
        return ""
    mod_label = " / ".join(display_loader_name(item) for item in sorted(supported))
    if loader_id == "vanilla":
        return tr("mods.instance.mismatch.vanilla", mod_label)
    return tr("mods.instance.mismatch", mod_label, loader_label)


def _instance_note(
    kind: str,
    *,
    loaders: list[str],
    game_versions: list[str],
    loader_id: str,
    loader_label: str,
    minecraft: str,
) -> str:
    """Why ``kind``'s chosen file version cannot go into this instance, or "" when it fits."""
    note = _loader_mismatch(loaders, loader_id, loader_label)
    if note:
        return note
    if kind == "mod":
        # PCL's rule: a mod's own game-version list is checked; a resource pack or shader is not
        # (their "loaders" are empty, and their game versions are informational).
        versions = [item for item in game_versions if "." in item]
        if versions and minecraft and minecraft not in versions:
            return tr("mods.instance.mismatch.game", _join_limited(versions), minecraft)
    return ""


def _loader_and_minecraft(game_dir: Path, instance_id: str) -> tuple[str, str]:
    """(loader id, Minecraft version) for one instance, reading its profile and ``inheritsFrom``.

    Delegated to :func:`launcher.instances.profile_identity`, which also recognises a loader from
    the profile JSON itself - the versions page creates a new loader instance under the user's name,
    so a folder called ``1.21.1_fabric_0.16.9`` must still count as a Fabric instance.
    """
    identity = instances.profile_identity(game_dir, instance_id)
    return (identity.loader_id or "vanilla"), identity.minecraft


def instance_choices(
    game_dir: Path,
    mod_loaders: list[str],
    *,
    cfg=None,
    kind: str = "mod",
    game_versions: list[str] | None = None,
) -> list[InstanceChoice]:
    """Every instance in ``game_dir``, with its loader and whether the chosen file version fits.

    This reads the folder only - no network request, and no instance is created or changed here.
    """
    game_dir = Path(game_dir)
    if cfg is None:
        cfg, _ = config.load()
    versions = list(game_versions or ())
    choices: list[InstanceChoice] = []
    for instance in sorted(
        instances.list_instances(game_dir).values(), key=lambda item: item.name.casefold()
    ):
        loader_id, minecraft = _loader_and_minecraft(game_dir, instance.id)
        try:
            resolved = instances.resolve_instance(instance.id, cfg, game_dir)
        except (instances.InstancesError, OSError):
            continue  # the folder vanished between the listing and the resolve
        loader_label = (
            tr("instances.card.vanilla")
            if loader_id == "vanilla"
            else _LOADER_LABELS.get(loader_id, loader_id)
        )
        choices.append(
            InstanceChoice(
                game_dir=game_dir,
                instance_id=instance.id,
                name=instance.name,
                loader_id=loader_id,
                loader_label=loader_label,
                minecraft=minecraft,
                isolated=resolved.isolated,
                mods_dir=resolved.mods_dir,
                folder=instances.instance_dir(game_dir, instance.id),
                instance=instance,
                note=_instance_note(
                    kind,
                    loaders=list(mod_loaders),
                    game_versions=versions,
                    loader_id=loader_id,
                    loader_label=loader_label,
                    minecraft=minecraft,
                ),
            )
        )
    return choices


def _dir_rank(path: Path) -> tuple[float, int]:
    """(last write time, file count) of a content folder; a missing folder ranks below any real one."""
    try:
        modified = path.stat().st_mtime
        count = sum(1 for entry in path.iterdir() if entry.is_file())
    except OSError:
        return (0.0, -1)
    return (modified, count)


def default_content_dir(
    kind: str,
    loaders: list[str],
    game_versions: list[str],
    *,
    cfg,
    game_dir: Path,
) -> Path:
    """The folder the save dialog starts in, in PCL's order of priority.

    1. the folder the user saved this kind of resource to last time (``config.resource_dirs``);
    2. the currently selected instance's own folder, when the file version fits it;
    3. among every compatible instance, the folder changed most recently (then the one with the
       most files);
    4. the game folder itself when no instance is compatible.

    The remembered folder wins outright, exactly as in PCL: it is the user's own last answer, and a
    later install with another loader must not silently move it back into an instance.
    """
    game_dir = Path(game_dir)
    key = CONTENT_SUBDIRS.get(kind, kind)
    remembered = str((getattr(cfg, "resource_dirs", {}) or {}).get(key, "") or "").strip()
    if remembered:
        return Path(remembered)

    choices = instance_choices(
        game_dir, loaders, cfg=cfg, kind=kind, game_versions=game_versions
    )
    selected_id = str(getattr(cfg, "selected_instance", "") or "").strip()
    if selected_id:
        selected = next((item for item in choices if item.instance_id == selected_id), None)
        if selected is not None and selected.usable:
            return selected.content_dir(kind)

    compatible = [item for item in choices if item.usable]
    if compatible:
        best = max(compatible, key=lambda item: _dir_rank(item.content_dir(kind)))
        return best.content_dir(kind)
    return game_dir


def remember_content_dir(kind: str, directory: Path) -> None:
    """Store the folder the user chose for ``kind`` (the next save dialog starts there).

    The write is best-effort: a config that cannot be saved (read-only data dir, a minimal config
    object in a test) must not turn a completed download into a failure. The key is the folder name
    the kind lives in, so the config reads as ``{"mods": "D:/mc/mods"}``.
    """
    key = CONTENT_SUBDIRS.get(kind)
    if key is None:
        return
    try:
        cfg, cfg_path = config.load()
        folders = dict(getattr(cfg, "resource_dirs", {}) or {})
        folders[key] = str(directory)
        cfg.resource_dirs = folders
        config.save(cfg, cfg_path)
    except (AttributeError, OSError, TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "resources: could not remember the %s save folder %s", kind, directory, exc_info=True
        )


def ask_save_file(
    parent: QWidget | None, title: str, suggested: Path, filter_text: str
) -> Path | None:
    """The system save dialog, and nothing else; ``None`` means the user cancelled.

    Kept as one module-level function so a test can replace exactly this call (the dialog itself is
    a native window and must never open during a test run). ``QFileDialog.getSaveFileName`` is the
    platform dialog the user asked for, pre-filled with the suggested full path.
    """
    chosen, _selected_filter = QFileDialog.getSaveFileName(
        parent, title, str(suggested), filter_text
    )
    text = str(chosen or "").strip()
    return Path(text) if text else None


class _ResourceRow(QWidget):
    """One dense resource result: 48px icon, name + type tag, one-line summary, meta line, install.

    Everything except the install button is painted by hand. Two reasons: the summary must be elided
    on one line (a QLabel would either wrap to several lines or report text wider than it paints, which
    the text-overflow audit flags), and a list of a thousand widgets would make theme swaps crawl - one
    widget plus one button per row is all Qt has to re-polish.
    """

    HEIGHT = 68
    #: The list slot is the card plus the 8px gap below it. QListWidget::setSpacing offsets an item
    #: widget by the spacing but does not shrink it, which clipped the install button at the viewport
    #: edge; the gap therefore lives inside the slot and the card paints only its top 68px.
    SLOT_HEIGHT = HEIGHT + 8
    ICON = 48
    PADDING = 12
    GAP = 12
    RADIUS = 8
    TAG_HEIGHT = 18
    #: The loader marker glyph is 12px; a segment is glyph + 4px + text, and segments are 14px
    #: apart. Kept as class constants so the plan and the painter cannot drift apart.
    META_GLYPH = 12
    META_GAP = 14

    def __init__(
        self,
        entry: object,
        kind_label: str,
        source_label: str,
        *,
        on_select,
        on_install,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry = entry
        self.icon_url = str(getattr(entry, "icon_url", "") or "").strip()
        # Two lines only, and only their wording changes with the table: the headline is the
        # Chinese name when we have one, the second line is then the project's English title.
        self.title, self.subtitle, _chinese = display_titles(entry)
        self.description = str(getattr(entry, "description", "") or "")
        self._kind_label = kind_label
        self._on_select = on_select
        self._on_install = on_install
        self._meta = self._build_meta(entry, source_label)
        self._icon: QPixmap | None = None
        self._fallback_icon = paint_utils.letter_tile_pixmap(self.title, self.ICON, self.RADIUS)
        self._selected = False
        self._hover = False

        self.install = QPushButton(tr("mods.install"))
        self.install.setObjectName("rowInstall")
        self.install.setCursor(Qt.CursorShape.PointingHandCursor)
        # 30 buttons all reading "Install" would be useless with a screen reader; the row's title is
        # what tells them apart.
        self.install.setAccessibleName(tr("mods.install") + " · " + self.title)
        self.install.clicked.connect(self._activate)
        self.setFixedHeight(self.SLOT_HEIGHT)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, self.PADDING, self.SLOT_HEIGHT - self.HEIGHT)
        row.setSpacing(0)
        row.addStretch(1)
        row.addWidget(self.install, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip(self._tooltip_text())

    # ---------- data ----------

    def _build_meta(self, entry: object, source_label: str) -> list[tuple[str, str]]:
        """(glyph, text) segments for the bottom line, in the order the design puts them."""
        segments: list[tuple[str, str]] = []
        loaders = [str(item) for item in (getattr(entry, "loaders", None) or [])]
        if loaders:
            segments.append(("loaders", " / ".join(display_loader_name(name) for name in loaders)))
        segments.append(("downloads", format_downloads(int(getattr(entry, "downloads", 0) or 0))))
        updated = format_updated(str(getattr(entry, "updated", "") or ""))
        if updated:
            segments.append(("updated", updated))
        if source_label:
            segments.append(("source", source_label))
        return segments

    def _tooltip_text(self) -> str:
        lines = [self.title]
        if self.subtitle and self.subtitle != self.title:
            lines.append(self.subtitle)
        if self.description and self.description not in lines:
            lines.append(self.description)
        meta = " · ".join(text for _glyph, text in self._meta)
        if meta:
            lines.append(meta)
        return "\n".join(lines)

    def set_icon(self, pixmap: QPixmap) -> None:
        self._icon = pixmap
        self.update()

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()
        # Tab reaches the install button of the selected row only: thirty identical stops would make
        # Tab unusable inside the list, and the arrow keys already move the selection to every row.
        policy = Qt.FocusPolicy.StrongFocus if selected else Qt.FocusPolicy.ClickFocus
        if self.install.focusPolicy() != policy:
            self.install.setFocusPolicy(policy)

    # ---------- interaction ----------

    def _activate(self) -> None:
        self._on_select()
        self._on_install()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_select()
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        # Moving onto the install button leaves the row's own rect; that is still "inside the card".
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self._hover = False
            self.update()
        super().leaveEvent(event)

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0, 0, self.width(), self.HEIGHT).adjusted(0.5, 0.5, -0.5, -0.5)
        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        if self._selected:
            border = QColor(tokens.get("ACCENT", "#2f9e6f"))
        elif self._hover:
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        icon_rect = QRectF(
            self.PADDING, (self.HEIGHT - self.ICON) / 2.0, self.ICON, self.ICON
        )
        clip = QPainterPath()
        clip.addRoundedRect(icon_rect, self.RADIUS, self.RADIUS)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(icon_rect.toRect(), self._icon or self._fallback_icon)
        painter.restore()

        text_left = self.PADDING + self.ICON + self.GAP
        text_right = max(text_left, self.install.x() - self.GAP)
        width = text_right - text_left
        if width <= 0:
            painter.end()
            return

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_metrics = QFontMetrics(name_font)
        tag_font = QFont(self.font())
        tag_font.setPixelSize(11)
        tag_metrics = QFontMetrics(tag_font)
        tag_width = min(width, tag_metrics.horizontalAdvance(self._kind_label) + 12)

        painter.setFont(name_font)
        painter.setPen(text_color)
        name = name_metrics.elidedText(self.title, Qt.TextElideMode.ElideRight, max(0, width))
        name_width = name_metrics.horizontalAdvance(name)
        painter.drawText(
            QRectF(text_left, 7, max(0, width), 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name,
        )
        # The tag sits right after the name (not at the far edge); it disappears only when the name
        # would have to be truncated to make room for it.
        if text_left + name_width + 8 + tag_width <= text_right:
            tag_rect = QRectF(text_left + name_width + 8, 8, tag_width, self.TAG_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("BG_SECTION", "#f1f3f6")))
            painter.drawRoundedRect(tag_rect, 4, 4)
            painter.setFont(tag_font)
            painter.setPen(hint_color)
            painter.drawText(
                tag_rect,
                int(Qt.AlignmentFlag.AlignCenter),
                tag_metrics.elidedText(self._kind_label, Qt.TextElideMode.ElideRight, int(tag_width) - 12),
            )

        desc_font = QFont(self.font())
        desc_font.setPixelSize(12)
        painter.setFont(desc_font)
        painter.setPen(hint_color)
        desc_metrics = QFontMetrics(desc_font)
        description = desc_metrics.elidedText(
            self.subtitle.replace("\n", " "), Qt.TextElideMode.ElideRight, int(width)
        )
        painter.drawText(
            QRectF(text_left, 28, width, 16),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            description,
        )

        meta_font = QFont(self.font())
        meta_font.setPixelSize(11)
        cursor = text_left
        for glyph, text, segment in self._plan_meta(max(0, int(text_right - text_left))):
            self._paint_glyph(
                painter, glyph, QRectF(cursor, 47, self.META_GLYPH, 12), hint_color
            )
            if text:
                painter.setFont(meta_font)
                painter.setPen(hint_color)
                painter.drawText(
                    QRectF(cursor + 16, 45, max(0, segment - 16), 16),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    text,
                )
            cursor += segment + self.META_GAP
        painter.end()

    def _plan_meta(self, available: int) -> list[tuple[str, str, int]]:
        """Decide which meta segments the row can draw, and how wide each one is.

        Priority, highest first: the source (the whole reason the row shows it), downloads, the
        loader information, and finally the update time. The loader segment prefers the loader
        names - "Fabric" is the only useful thing a single-loader project can say - and only
        degrades to the bare ``#`` marker when the names do not fit next to the other segments.
        A project supporting all four loaders therefore usually shows ``#`` while a one- or
        two-loader project keeps its names. Widths are measured before drawing, so a segment is
        never half-painted or allowed to run under the install button; the names are always in the
        tooltip, and the source is never dropped (it is elided only when even it alone is too wide).
        """
        meta_font = QFont(self.font())
        meta_font.setPixelSize(11)
        metrics = QFontMetrics(meta_font)
        segments: list[tuple[str, str, int, str]] = []
        for glyph, text in self._meta:
            if glyph == "loaders":
                # Two shapes of the same segment: full names, and the marker-only fallback.
                named_width = 16 + metrics.horizontalAdvance(text) + 2
                segments.append((glyph, text, named_width, text))
            else:
                segments.append((glyph, text, 16 + metrics.horizontalAdvance(text) + 2, text))

        def with_marker(items: list[tuple[str, str, int, str]]) -> list[tuple[str, str, int, str]]:
            """The same segments with the loader text replaced by the 12px ``#`` marker."""
            return [
                (glyph, "", self.META_GLYPH, full) if glyph == "loaders" else (glyph, text, width, full)
                for glyph, text, width, full in items
            ]

        def fit(items: list[tuple[str, str, int, str]]) -> int:
            return sum(width for _g, _t, width, _full in items) + (
                self.META_GAP * (len(items) - 1) if items else 0
            )

        def plain(items: list[tuple[str, str, int, str]]) -> list[tuple[str, str, int]]:
            return [(glyph, text, width) for glyph, text, width, _full in items]

        # 1. The ideal: names and every other segment.
        if fit(segments) <= available:
            return plain(segments)
        # 2. Names do not fit: the marker costs 12px instead of a sentence, time is kept.
        marker = with_marker(segments)
        if fit(marker) <= available:
            return plain(marker)
        # 3. Still too narrow: the relative time goes (the first sacrifice at every width).
        marker_no_time = [item for item in marker if item[0] != "updated"]
        if len(marker_no_time) < len(marker) and fit(marker_no_time) <= available:
            return plain(marker_no_time)
        # 4. Then the marker itself; downloads and source stay.
        no_marker = [item for item in marker_no_time if item[0] != "loaders"]
        if len(no_marker) < len(marker_no_time) and fit(no_marker) <= available:
            return plain(no_marker)
        # 5. Only the source is left, and only when even downloads no longer fit.
        source_only = [item for item in no_marker if item[0] != "downloads"]
        if source_only and fit(source_only) <= available:
            return plain(source_only)

        source = next((item for item in segments if item[0] == "source"), None)
        if source is None:
            return []
        text_width = max(0, available - 16 - 2)
        text = metrics.elidedText(source[3], Qt.TextElideMode.ElideRight, text_width)
        return [("source", text, 16 + metrics.horizontalAdvance(text) + 2)]

    @staticmethod
    def _paint_glyph(painter: QPainter, kind: str, rect: QRectF, color: QColor) -> None:
        """Tiny vector glyphs for the meta segments (no icon font, no emoji, no image asset)."""
        pen = QPen(color)
        pen.setWidthF(1.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.save()
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        if kind == "loaders":  # a 2x2 grid, "loaders / compatible range"
            for fraction in (0.35, 0.65):
                painter.drawLine(QPointF(x + w * fraction, y + 1), QPointF(x + w * fraction, y + h - 1))
                painter.drawLine(QPointF(x + 1, y + h * fraction), QPointF(x + w - 1, y + h * fraction))
        elif kind == "downloads":  # arrow into a tray
            center = x + w / 2
            painter.drawLine(QPointF(center, y + 1.5), QPointF(center, y + h - 4))
            painter.drawLine(QPointF(center, y + h - 4), QPointF(center - w * 0.22, y + h * 0.55))
            painter.drawLine(QPointF(center, y + h - 4), QPointF(center + w * 0.22, y + h * 0.55))
            painter.drawLine(QPointF(x + 1.5, y + h - 1), QPointF(x + w - 1.5, y + h - 1))
        elif kind == "updated":  # clock
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
            painter.drawLine(QPointF(x + w / 2, y + h / 2), QPointF(x + w / 2, y + h * 0.28))
            painter.drawLine(QPointF(x + w / 2, y + h / 2), QPointF(x + w * 0.70, y + h / 2))
        else:  # source: a globe
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
            painter.drawLine(QPointF(x + 1, y + h / 2), QPointF(x + w - 1, y + h / 2))
            painter.drawEllipse(QRectF(x + w * 0.32, y + 1, w * 0.36, h - 2))
        painter.restore()



class _ResourceList(QListWidget):
    """QListWidget whose item widgets always span the viewport.

    Qt hands an item widget the item's visual rect when it is installed, but a list that is laid out
    before its final width (or whose scrollbar appears later) leaves older widgets wider than the
    viewport - the card then hangs over the right edge and its install button is clipped. Re-applying
    the viewport width on every resize keeps the card and its button inside the list.

    The empty-state label also lives here, as an overlay covering the viewport: the list's result area
    is what the message is about, and centring it there makes "no matches" read as the state of the
    list instead of a stray line under it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._empty_overlay: QLabel | None = None

    def attach_empty_overlay(self, label: QLabel) -> None:
        self._empty_overlay = label
        label.setParent(self.viewport())
        label.setGeometry(self.viewport().rect())
        label.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.sync_item_widths()
        if self._empty_overlay is not None:
            self._empty_overlay.setGeometry(self.viewport().rect())

    def sync_item_widths(self) -> None:
        width = self.viewport().width()
        for index in range(self.count()):
            widget = self.itemWidget(self.item(index))
            if widget is not None and widget.width() != width:
                widget.resize(width, widget.height())


class _VersionRow(QAbstractButton):
    """One file version in the install step: version name, game versions, loaders, date, size.

    Painted rather than composed for the reason the version cards on the versions page are: the
    metadata line must elide on one line, and a list of a hundred alternatives must not pay for a
    widget tree per row. It is a real ``QAbstractButton``: a Tab stop, Space/Enter activate it, and
    assistive technology reports the version and its metadata.
    """

    HEIGHT = 78
    PADDING = 16
    RADIUS = 8
    MARK = 16

    def __init__(
        self,
        version: object,
        *,
        current: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.version = version
        self._hover = False
        self.setObjectName("instanceChoiceRow")  # the resources page's choice-row surface
        self.setFixedHeight(self.HEIGHT)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setChecked(current)
        self._refresh_text()

    # ---------- data ----------

    def _meta_line(self) -> str:
        versions = version_game_versions_of(self.version)
        loaders = version_loaders_of(self.version)
        parts: list[str] = []
        if versions:
            parts.append(tr("mods.versions.mc") + " " + _join_limited(versions, 3))
        if loaders:
            parts.append(
                tr("mods.versions.loaders")
                + " "
                + ", ".join(display_loader_name(item) for item in loaders[:3])
            )
        return " · ".join(parts)

    def _info_line(self) -> str:
        parts = [part for part in (version_published_of(self.version)[:10],) if part]
        size = version_size_of(self.version)
        if size:
            parts.append(format_size(size))
        filename = version_filename_of(self.version)
        if filename:
            parts.append(filename)
        return " · ".join(parts)

    def _refresh_text(self) -> None:
        name = version_name_of(self.version)
        meta = self._meta_line()
        info = self._info_line()
        accessible = " · ".join(part for part in (name, meta, info) if part)
        self.setAccessibleName(accessible)  # WCAG 4.1.2
        self.setToolTip("\n".join(part for part in (name, meta, info) if part))

    # ---------- interaction ----------

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        self.update()

    def keyPressEvent(self, event) -> None:
        """Enter chooses the row, exactly like Space; the dialog's Enter must not skip the choice."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------- painting ----------

    def paintEvent(self, event) -> None:
        tokens = _tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)

        background = QColor(tokens.get("BG_LIST", "#ffffff"))
        border = QColor(tokens.get("BORDER_TABLE", "#dfe4ea"))
        if self.isChecked():
            border = QColor(tokens.get("ACCENT", "#2f9e6f"))
            background = QColor(tokens.get("ACCENT_TINT", background.name()))
        elif self._hover:
            border = QColor(tokens.get("BORDER_HOVER", border.name()))
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            border = QColor(tokens.get("FOCUS_RING", border.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        text_color = QColor(tokens.get("FG_TEXT", "#1f2a36"))
        hint_color = QColor(tokens.get("FG_HINT", "#4f6070"))
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        name_metrics = QFontMetrics(name_font)
        meta_metrics = QFontMetrics(meta_font)

        left = self.PADDING
        right = self.width() - self.PADDING - self.MARK - 12
        text_width = max(0, right - left)
        name_height = name_metrics.height()
        meta_height = meta_metrics.height()
        block = name_height + 2 + meta_height + 2 + meta_height
        top = (self.height() - block) / 2.0

        painter.setFont(name_font)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(left, top, text_width, name_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name_metrics.elidedText(
                version_name_of(self.version), Qt.TextElideMode.ElideRight, text_width
            ),
        )
        painter.setFont(meta_font)
        painter.setPen(hint_color)
        painter.drawText(
            QRectF(left, top + name_height + 2, text_width, meta_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            meta_metrics.elidedText(self._meta_line(), Qt.TextElideMode.ElideRight, text_width),
        )
        painter.drawText(
            QRectF(left, top + name_height + 2 + meta_height + 2, text_width, meta_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            meta_metrics.elidedText(self._info_line(), Qt.TextElideMode.ElideRight, text_width),
        )
        self._paint_mark(painter, tokens)
        painter.end()

    def _paint_mark(self, painter: QPainter, tokens: dict[str, str]) -> None:
        """The single-choice indicator: an empty ring, or a filled accent disc with a tick."""
        centre = QPointF(self.width() - self.PADDING - self.MARK / 2.0, self.height() / 2.0)
        radius = self.MARK / 2.0 - 1.0
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.get("ACCENT", "#2f9e6f")))
            painter.drawEllipse(centre, radius, radius)
            pen = QPen(QColor(tokens.get("FG_SELECTED", "#ffffff")))
            pen.setWidthF(1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath(centre + QPointF(-3.4, 0.2))
            path.lineTo(centre + QPointF(-1.0, 2.6))
            path.lineTo(centre + QPointF(3.4, -2.8))
            painter.drawPath(path)
            return
        painter.setPen(QPen(QColor(tokens.get("FG_HINT", "#4f6070")), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, radius, radius)


class _VersionPickerDialog(inwindow_dialog.InWindowDialog):
    """The resources page's install step: choose the file version, then the save location.

    An in-window card (never a native window) listing every file version the source returned, newest
    first. The row metadata is what the user needs to tell them apart - version name, Minecraft
    versions, loaders, publication date and file size - and one 48px accent pill leads to the system
    save dialog. Nothing is downloaded and no instance is chosen here; that is the page's next step.
    """

    CARD_WIDTH = 680
    LIST_MAX_HEIGHT = 380

    def __init__(
        self,
        parent: QWidget | None,
        versions: list[object],
        *,
        project_title: str,
        current_key: str = "",
    ) -> None:
        super().__init__(
            parent,
            title=tr("mods.versions.title", project_title),
            severity="question",
            card_width=self.CARD_WIDTH,
        )
        self.versions = list(versions)
        self.rows: list[_VersionRow] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        # A plain label, not add_body's scroll area: the hint is one short sentence, and a scroll
        # area's 192px size hint left a hole between the hint and the list.
        self.hint_label = QLabel(tr("mods.versions.hint"))
        self.hint_label.setObjectName("overlayBody")
        self.hint_label.setWordWrap(True)
        self.hint_label.setMaximumWidth(self.CARD_WIDTH - 2 * inwindow_dialog.CARD_PADDING)
        self.card_layout.addWidget(self.hint_label)

        self.list_area = QScrollArea()
        self.list_area.setObjectName("overlayScroll")
        self.list_area.setWidgetResizable(True)
        self.list_area.setFrameShape(QFrame.Shape.NoFrame)
        self.list_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Tab must land on the rows
        self.list_area.setMaximumHeight(self.LIST_MAX_HEIGHT)
        self.list_area.setMaximumWidth(self.CARD_WIDTH - 2 * inwindow_dialog.CARD_PADDING)
        container = QWidget()
        rows_layout = QVBoxLayout(container)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(8)
        first: _VersionRow | None = None
        for version in self.versions:
            row = _VersionRow(version, current=version_key_of(version) == current_key)
            row.clicked.connect(self._sync_next)
            self._group.addButton(row)
            rows_layout.addWidget(row)
            self.rows.append(row)
            if first is None:
                first = row
        rows_layout.addStretch(1)
        self.list_area.setWidget(container)
        self.card_layout.addWidget(self.list_area)
        if first is not None and not any(row.isChecked() for row in self.rows):
            first.setChecked(True)  # newest first: the same default PCL pre-selects

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.addStretch(1)
        self.cancel_button = QPushButton(tr("dialog.cancel"))
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.setMinimumHeight(36)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(
            lambda: self._finish(inwindow_dialog.Outcome.REJECTED)
        )
        self.next_button = QPushButton(tr("mods.versions.next"))
        self.next_button.setObjectName("installPrimary")  # the versions step's pill
        self.next_button.setMinimumHeight(48)
        self.next_button.setMinimumWidth(200)
        self.next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_button.setEnabled(self.selected_version() is not None)
        self.next_button.clicked.connect(
            lambda: self._finish(inwindow_dialog.Outcome.ACCEPTED)
        )
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.next_button)
        self.card_layout.addLayout(button_row)
        self._mark_default(self.next_button, inwindow_dialog.Outcome.ACCEPTED)

    # ---------- data ----------

    def selected_version(self) -> object | None:
        for row in self.rows:
            if row.isChecked():
                return row.version
        return None

    def _sync_next(self, *_args) -> None:
        self.next_button.setEnabled(self.selected_version() is not None)

    # ---------- keyboard ----------

    def _focus_initial(self) -> None:
        """Start on the checked row (the newest version), so Enter is immediately meaningful."""
        selected = self.selected_version()
        for row in self.rows:
            if row.version is selected:
                row.setFocus(Qt.FocusReason.TabFocusReason)
                return
        super()._focus_initial()

    def keyPressEvent(self, event) -> None:
        """Enter is the pill, but only once a row is chosen; otherwise it must do nothing."""
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and self.selected_version() is None
        ):
            event.accept()
            return
        super().keyPressEvent(event)


class _PopupFitCombo(QComboBox):
    """Combo box whose popup reserves the stylesheet's list padding.

    The resource sheet gives the popup list 4px of padding on each side but sizes item rows
    independently; Qt computes the popup height from the rows alone, so the two or three rows of
    these dropdowns lost their last row and grew a vertical scrollbar. Setting a minimum height on
    the view before the popup is shown adds that padding back; it is recomputed on every show, so a
    font or language change cannot leave a stale height behind.
    """

    #: 4px top + 4px bottom of list padding, plus two pixels of rounding slack.
    _VIEW_PADDING = 10
    #: The PCL field metric shared with the search field beside these combos.
    FIELD_HEIGHT = 36

    def sizeHint(self) -> QSize:
        """Keep the 36px field metric even when the sheet's ``min-height`` rule re-polishes.

        The sheet applies its own minimum (26px after border/padding) on every polish, so a
        ``setMinimumHeight(36)`` alone loses the race after a theme swap. The size hint is what the
        row layout asks for, and it is not reset by a style change.
        """
        hint = super().sizeHint()
        if hint.height() < self.FIELD_HEIGHT:
            hint.setHeight(self.FIELD_HEIGHT)
        return hint

    def showPopup(self) -> None:
        view = self.view()
        rows = min(self.count(), self.maxVisibleItems())
        if rows > 0:
            rows_height = sum(view.sizeHintForRow(index) for index in range(rows))
            view.setMinimumHeight(rows_height + self._VIEW_PADDING)
        super().showPopup()


class _FieldLineEdit(QLineEdit):
    """Line edit that keeps the 36px field metric when the sheet's ``min-height`` re-polishes.

    The sheet sets a 26px effective minimum on every input, which wins over a one-shot
    ``setMinimumHeight(36)`` after the next polish. The resource grid needs both rows to stay 36px
    tall: otherwise hiding the slug field (26px after that polish) shortens the row and moves the
    loader/game-version fields. Returning the height from the size hint survives every re-polish,
    the same way :class:`_PopupFitCombo` keeps the combo height.
    """

    FIELD_HEIGHT = 36

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if hint.height() < self.FIELD_HEIGHT:
            hint.setHeight(self.FIELD_HEIGHT)
        return hint


def _fit_combo_width(combo: QComboBox, labels: list[str] | None = None) -> None:
    """Give ``combo`` the width of its longest label plus the style's own arrow/padding chrome.

    The chrome is measured from the widget (``sizeHint`` minus the current text) instead of being a
    magic number, so a theme's padding or the platform's arrow metric cannot drift away from the
    width the grid needs. ``labels`` lets a combo size for choices it does not currently hold, which
    keeps the sort field from jumping between browse and search modes.
    """
    if labels is None:
        labels = [combo.itemText(index) for index in range(combo.count())]
    metrics = combo.fontMetrics()
    chrome = max(0, combo.sizeHint().width() - metrics.horizontalAdvance(combo.currentText()))
    widest = max((metrics.horizontalAdvance(text) for text in labels), default=0)
    combo.setMinimumWidth(widest + chrome)


class _TaskedInstall:
    """One resources-page install, listed as a task in the window's download page.

    This is an adapter, not a second install path. The four core functions this page calls
    (``install_mod``, ``install_modpack``, ``install_resourcepack``, ``install_shaderpack``) already
    accept a ``progress`` callable and already transfer through the shared
    :class:`launcher.net.downloader.Downloader`, so the page hands its own progress into that parameter
    and offers the downloader's cooperative cancellation as the task's cancel. Nothing in
    ``launcher/mods`` is changed, and no callback is replaced by a stand-in.

    What the cancel really does - stated here because the row cannot say it:

    * **The file transfer stops.** A ``Downloader`` batch notices the flag at its next 64 KB chunk and
      raises, leaving its ``.part`` files for the next attempt. That is a real stop of the bytes.
    * **The metadata in front of it does not.** Version lists, project lookups and dependency slugs are
      ordinary blocking HTTP requests; a cancel that lands during one takes effect when that call
      returns. Sub-second in practice, but not instant.
    * **A modpack's first two stages do not either.** The ``.mrpack`` download (``_download_direct``)
      and the automatic loader install do not consult the flag, so a cancel during either of them waits
      for that stage to finish before the file list stops.

    The worker is *never* killed - nothing can kill it - so the row is marked 已取消 immediately and the
    worker is allowed to unwind. Its ending arrives at :meth:`_error`, which recognises a cancelled task
    and stays quiet instead of reporting the user's own stop as a failure.
    """

    def __init__(
        self,
        widget: QWidget,
        *,
        kind: str,
        title: str,
        retry=None,
    ) -> None:
        self._widget = widget
        self._rate = RateTracker()
        self.bridge = ProgressBridge()
        self.bridge.progress.connect(self._on_progress)
        self.task: Task | None = None
        center = task_center_of(widget)
        if center is not None:
            # A cancel belongs to the install that was stopped, never to the next one.
            downloader.clear_cancellation()
            self.task = center.add_task(
                KIND_TITLE_KEYS[kind],
                title,
                kind=kind,
                cancel=self._cancel,
                retry=self._guarded(retry),
            )

    # ---------- the task's side ----------

    def _on_progress(self, progress) -> None:
        """A download tick (already marshalled onto the GUI thread by the bridge)."""
        task = self.task
        if task is None or not task.is_running():
            return
        self._rate.set_total(progress.total_bytes)
        rate, _eta = self._rate.update(progress.done_bytes)
        task.set_progress(progress.done_bytes, progress.total_bytes, rate)

    def _cancel(self, task: Task) -> None:
        """The task's 取消: ask the transfer to stop, and say so on the status line at once."""
        downloader.cancel_current_downloads()
        if widget_alive(self._widget):
            set_app_status(self._widget, tr("tasks.status.cancelled", task.title()))

    def _guarded(self, retry):
        """A 重试 outlives its page (the task list is the window's): a dead page makes it a no-op."""
        if retry is None:
            return None
        widget = self._widget

        def guarded() -> None:
            if widget_alive(widget):
                retry()

        return guarded

    def abandon(self) -> None:
        """The page is being dropped (a language switch): end the task, let the worker unwind."""
        task, self.task = self.task, None
        if task is not None and task.is_running():
            task.cancel()

    # ---------- running the job ----------

    def run(self, job, *, on_result, on_error, on_finished, progress_slot=None) -> None:
        """Run ``job(progress)`` on the worker pool with this install's task attached."""
        if progress_slot is not None:
            self.bridge.progress.connect(progress_slot)
        widget = self._widget

        def work():
            return job(self.bridge)

        def result(value) -> None:
            task, self.task = self.task, None
            downloader.clear_cancellation()
            if task is not None:
                task.finish()
            if widget_alive(widget):
                on_result(value)

        def error(message: str) -> None:
            task, self.task = self.task, None
            stopped_by_a_cancel = downloader.is_user_cancelled()
            if task is not None and task.state == CANCELLED:
                # The user stopped this download: the flow's error is the ending they asked for, so it
                # is not turned into a failure and no dialog opens.
                downloader.clear_cancellation()
                if widget_alive(widget):
                    set_app_status(widget, tr("tasks.status.cancelled", task.title()))
                return
            if task is not None and task.is_running() and stopped_by_a_cancel:
                # The stop latch is process-wide: cancelling *another* row stops this transfer too. It
                # really was stopped, so it ends as 已取消 rather than as a failure the user never
                # caused - and the latch is released after this ending, not before it.
                task.cancel()
                downloader.clear_cancellation()
                if widget_alive(widget):
                    set_app_status(widget, tr("tasks.status.cancelled", task.title()))
                return
            downloader.clear_cancellation()
            if task is not None:
                task.fail(message)
            if widget_alive(widget):
                on_error(message)

        run_in_background(work, on_result=result, on_error=error, on_finished=on_finished)


class _ContentTab(QWidget):
    """Generic resource tab. kind: mod / modpack / resourcepack / shaderpack"""

    CONTENT_SUBDIR: ClassVar[dict[str, str]] = CONTENT_SUBDIRS

    # Tab title per kind, so a control inside a tab can name the tab it belongs to. The four tabs share
    # one class and each has its own source dropdown, so a bare "source" would name four different
    # controls identically (WCAG 4.1.2 wants the name to identify the control).
    KIND_TITLE_KEY: ClassVar[dict[str, str]] = {
        "mod": "mods.tab.mods",
        "modpack": "mods.tab.modpacks",
        "resourcepack": "mods.tab.resourcepacks",
        "shaderpack": "mods.tab.shaderpacks",
    }

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self._entries: list[object] = []  # ModSearchHit (search) or ModInfo (slug query)
        self._rows: list[_ResourceRow] = []
        self._icon_cache: dict[str, QPixmap] = {}
        self._icon_pending: set[str] = set()
        self._hit: ModSearchHit | None = None
        self._current: ModInfo | None = None
        self._current_slug = ""
        self._current_mod_version_id = ""
        self._current_loader = ""
        self._current_game_version = ""
        #: Loaders the last project lookup said the mod supports (empty = the source did not say).
        self._current_loaders: list[str] = []
        # Paging state: _query "" means the initial browse list (no keyword), otherwise the last
        # submitted search. _request_id discards a result whose source/page/sort has been changed
        # while its request was still in flight.
        self._page = 1
        self._has_next = False
        self._query = ""
        self._request_id = 0
        self._initial_loaded = False
        #: The install currently running on this tab, if any (its task lives in the window's list).
        self._active_install: _TaskedInstall | None = None

        self.loader_combo = _PopupFitCombo()
        for name in ("fabric", "forge", "neoforge", "quilt"):
            self.loader_combo.addItem(display_loader_name(name), name)
        # ensurePolished first: the chrome in _fit_combo_width is measured from the styled widget,
        # not from Fusion's default padding.
        self.loader_combo.ensurePolished()
        _fit_combo_width(self.loader_combo)
        # WCAG 4.1.2: the row's "loader" label is a plain QLabel beside this combo, so it never became
        # the combo's name (only a QFormLayout row does that automatically).
        self.loader_combo.setAccessibleName(tr("mods.loader"))
        self.game_version_edit = _FieldLineEdit("1.20.1")
        self.game_version_edit.setAccessibleName(tr("mods.game_version"))  # WCAG 4.1.2
        self.slug_edit = _FieldLineEdit()
        self.slug_edit.setAccessibleName(tr("mods.slug.label"))  # WCAG 4.1.2
        self.slug_edit.setPlaceholderText(tr("mods.slug.placeholder"))
        self.query_button = QPushButton(tr("mods.query"))
        self.query_button.setObjectName("queryButton")
        self.search_edit = _FieldLineEdit()
        self.search_edit.setAccessibleName(tr("mods.query"))  # WCAG 4.1.2
        add_search_icon(self.search_edit)
        placeholder_key = {
            "resourcepack": "mods.search.placeholder.rp",
            "shaderpack": "mods.search.placeholder.sp",
            "modpack": "mods.search.placeholder.modpack",
        }.get(kind, "mods.search.placeholder")
        self.search_edit.setPlaceholderText(tr(placeholder_key))
        self.search_button = QPushButton(tr("mods.search.button"))
        # The two rows share one grid: a label column, a field column, a second label column, a
        # second field column, the stretchy text field and the action button. The labels are also
        # what lets the source/loader pair and the sort/game-version pair start at the same x on both
        # rows - without them the second row's field sat behind a free-standing label and was offset
        # from the first row's field.
        self.source_label = QLabel(tr("mods.source"))
        self.sort_label = QLabel(tr("mods.sort"))
        self.loader_label = QLabel(tr("mods.loader")) if kind == "mod" else None
        self.game_version_label = QLabel(tr("mods.game_version"))
        self.sort_combo = _PopupFitCombo()
        #: True once the user picked a sort themselves; a later search then keeps their choice
        #: instead of snapping back to the search default. Programmatic rebuilds never set it.
        self._sort_user_chosen = False
        self._populate_sort_combo()
        # WCAG 4.1.2: one sort combo per tab, so the name says which tab it belongs to.
        self.sort_combo.setAccessibleName(
            tr("mods.sort") + " · " + tr(self.KIND_TITLE_KEY[kind])
        )
        # Minimum, not fixed: a pinned height made the popup size its list from the closed field.
        # ensurePolished() first: the sheet's own min-height would otherwise win over this one.
        self.sort_combo.ensurePolished()
        self.sort_combo.setMinimumHeight(36)
        # Size for the longest label of *every* mode, not the current one: switching between browse
        # (2 items) and search (4 items) must not make the field jump.
        _fit_combo_width(
            self.sort_combo,
            [tr(label_key) for _key, label_key in (*SORT_OPTIONS_BROWSE, *SORT_OPTIONS_SEARCH)],
        )
        self.install_button = QPushButton(tr("mods.install"))
        self.open_folder_button = QPushButton(tr("mods.open_folder"))
        self.open_folder_button.setObjectName("secondaryButton")
        # PCL metrics: 36px fields and buttons, as a *floor*. Minimum, not fixed: a machine whose font
        # is taller than ours needs the control to grow with its own text (a fixed 36 clipped the
        # search button's label at 17px). The row still lines up because every control shares the
        # floor and the tallest minimum in the row sets the height for all of them.
        for control in (
            self.loader_combo,
            self.game_version_edit,
            self.slug_edit,
            self.search_edit,
            self.query_button,
            self.search_button,
        ):
            control.setMinimumHeight(36)
        self.list = _ResourceList()
        self.list.setObjectName("resourceList")
        # WCAG 4.1.2: the list is a focus stop of its own; the category name tells the four lists apart.
        self.list.setAccessibleName(tr("nav.mods") + " · " + tr(self.KIND_TITLE_KEY[kind]))
        self.list.setUniformItemSizes(True)
        # No setSpacing(): it offsets an item widget without shrinking it, which pushed the install
        # button past the viewport edge. The 8px gap is part of the row slot instead (_ResourceRow).
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        apply_no_focus_outline(self.list)
        self.title_label = QLabel(tr("mods.select"))
        self.title_label.setObjectName("title")
        self.title_label.setWordWrap(True)
        self.desc_label = QLabel("")
        self.desc_label.setWordWrap(True)
        self.dep_required = QLabel("")
        self.dep_required.setWordWrap(True)
        self.dep_optional = QLabel("")
        self.dep_optional.setWordWrap(True)

        # One grid for both rows. Columns: label | field | label | field | stretchy text | button.
        # Both rows put their label/field pairs in the same columns, so the source and loader
        # dropdowns share a width and a left edge, and so do sort and the game-version field.
        self._form_grid = QGridLayout()
        self._form_grid.setContentsMargins(0, 0, 0, 0)
        self._form_grid.setHorizontalSpacing(8)
        self._form_grid.setVerticalSpacing(8)
        self._form_grid.setColumnStretch(4, 1)
        self._form_grid.addWidget(self.source_label, 0, 0)
        # The source combo itself is inserted at (0, 1) by ResourcesPage: the four tabs each own one.
        self._form_grid.addWidget(self.sort_label, 0, 2)
        self._form_grid.addWidget(self.sort_combo, 0, 3)
        self._form_grid.addWidget(self.search_edit, 0, 4)
        self._form_grid.addWidget(self.search_button, 0, 5)
        if self.loader_label is not None:
            self._form_grid.addWidget(self.loader_label, 1, 0)
            self._form_grid.addWidget(self.loader_combo, 1, 1)
        # No loader outside the mod tab, but the game-version field still starts in the second field
        # column: its label sits under "排序" and its field under the sort dropdown.
        self._form_grid.addWidget(self.game_version_label, 1, 2)
        self._form_grid.addWidget(self.game_version_edit, 1, 3)
        self._form_grid.addWidget(self.slug_edit, 1, 4)
        self._form_grid.addWidget(self.query_button, 1, 5)
        # A hidden slug field must not shrink the action column: both buttons keep the width of the
        # longer label, so the row does not jump when the source switches to CurseForge.
        for button in (self.search_button, self.query_button):
            button.ensurePolished()
        action_width = max(
            self.search_button.sizeHint().width(), self.query_button.sizeHint().width()
        )
        for button in (self.search_button, self.query_button):
            button.setMinimumWidth(action_width)

        self.pager = QWidget()
        pager_row = QHBoxLayout(self.pager)
        pager_row.setContentsMargins(0, 0, 0, 0)
        pager_row.setSpacing(8)
        self.prev_page_button = QPushButton(tr("mods.page.prev"))
        self.next_page_button = QPushButton(tr("mods.page.next"))
        for button in (self.prev_page_button, self.next_page_button):
            button.setObjectName("secondaryButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.page_label = QLabel("")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pager_row.addStretch(1)
        pager_row.addWidget(self.prev_page_button)
        pager_row.addWidget(self.page_label)
        pager_row.addWidget(self.next_page_button)
        pager_row.addStretch(1)
        self.pager.setVisible(False)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(self.list, 1)
        left.addWidget(self.pager)
        self.empty_results = EmptyState(tr("mods.empty.none"))
        self.list.attach_empty_overlay(self.empty_results)

        # Inspector for the selected entry: the full summary (the row elides it to one line), the
        # dependency hints of a slug query, and the original install / open-folder actions. Keeping it
        # is what lets the row itself stay one dense line per result without losing any function.
        self.detail = QFrame()
        self.detail.setObjectName("resourceDetail")
        # 252px is the design width; the upper bound exists for languages whose "select version and
        # install" button is longer than that (German 211px, French 205px, Japanese 208px at 13px), so
        # the panel widens instead of clipping the label.
        self.detail.setMinimumWidth(252)
        self.detail.setMaximumWidth(320)
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(12, 12, 12, 12)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(self.title_label)
        detail_layout.addWidget(self.desc_label)
        detail_layout.addWidget(self.dep_required)
        detail_layout.addWidget(self.dep_optional)
        detail_layout.addStretch(1)
        buttons_row = QVBoxLayout()
        buttons_row.setSpacing(8)
        buttons_row.addWidget(self.install_button)
        if kind != "modpack":
            buttons_row.addWidget(self.open_folder_button)
        detail_layout.addLayout(buttons_row)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addLayout(left, 1)
        body.addWidget(self.detail, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(self._form_grid)
        layout.addLayout(body, 1)

        self.list.currentRowChanged.connect(self._on_select)
        self.query_button.clicked.connect(self.query)
        self.search_button.clicked.connect(self._search)
        self.search_edit.returnPressed.connect(self._search)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        # `activated` fires only for a real user choice, never for the programmatic rebuild that
        # follows a mode change; that is what makes "keep the user's sort" possible.
        self.sort_combo.activated.connect(self._on_sort_activated)
        self.prev_page_button.clicked.connect(lambda: self._go_to_page(self._page - 1))
        self.next_page_button.clicked.connect(lambda: self._go_to_page(self._page + 1))
        self.install_button.clicked.connect(self._on_install_clicked)
        if kind != "modpack":
            self.open_folder_button.clicked.connect(self._open_content_folder)

        self._update_install_button()
        self._update_slug_visibility()

    # ---------- sort options ----------

    def _sort_options(self) -> tuple[tuple[str, str], ...]:
        """The choices the current mode offers: relevance only while a keyword is searched."""
        return SORT_OPTIONS_SEARCH if self._query else SORT_OPTIONS_BROWSE

    def _default_sort_key(self) -> str:
        return DEFAULT_SORT_SEARCH if self._query else DEFAULT_SORT_BROWSE

    def _populate_sort_combo(self) -> None:
        """Rebuild the sort dropdown for the current mode, keeping a choice the user made.

        A user selection survives a mode change while it exists in the new mode ("Popular" stays
        Popular when a search starts). A choice that no longer exists - "Relevance" after the
        keyword was cleared - falls back to the mode's default instead of leaving a blank field or
        a stale index. Signals are blocked: rebuilding is not a user action and must not reload.
        """
        options = self._sort_options()
        allowed = {key for key, _label in options}
        current = self.sort_combo.currentData()
        if not self._sort_user_chosen or current not in allowed:
            current = self._default_sort_key()
        self.sort_combo.blockSignals(True)
        try:
            wanted_labels = [(key, tr(label_key)) for key, label_key in options]
            present = [
                (self.sort_combo.itemData(index), self.sort_combo.itemText(index))
                for index in range(self.sort_combo.count())
            ]
            if present != wanted_labels:
                self.sort_combo.clear()
                for key, label in wanted_labels:
                    self.sort_combo.addItem(label, key)
            self.sort_combo.setCurrentIndex(max(self.sort_combo.findData(current), 0))
        finally:
            self.sort_combo.blockSignals(False)

    def _on_sort_activated(self, _index: int) -> None:
        """A real user choice: remember it so a later mode change does not override it."""
        self._sort_user_chosen = True

    def _update_slug_visibility(self) -> None:
        """Show the Modrinth slug query only where it means something.

        The slug lookup always goes through Modrinth's project API, so offering it under
        CurseForge or "All" promised a different source than the one selected. Hidden, not
        disabled: a disabled field still reads as "this could work". The row keeps its height
        through the loader / game-version controls, so nothing above or below moves.
        """
        visible = self._source_mode() == "modrinth"
        self.slug_edit.setVisible(visible)
        self.query_button.setVisible(visible)

    # ---------- Rows ----------

    def _source_label(self) -> str:
        page = getattr(self, "page", None)
        return page.current_source_label() if page is not None else ""

    def _entry_source_label(self, entry: object) -> str:
        """The row's own source label.

        In the "All" source the page-level label would read "All" on every row, which is exactly the
        information the two same-named rows need to differ by, so each hit carries its API source.
        """
        source = str(getattr(entry, "source", "") or "")
        if source in ("modrinth", "curseforge"):
            return tr("mods.source." + source)
        if isinstance(entry, ModInfo):
            # A slug query always goes through Modrinth's project/version API.
            return tr("mods.source.modrinth")
        return self._source_label()

    def _install_from_row(self, index: int) -> None:
        """Row install: select the row (the original selection handler fills _hit/_current), then run
        the page's existing install callback - the flow below is untouched."""
        self.list.setCurrentRow(index)
        self._on_install_clicked()

    def _sync_row_selection(self, index: int) -> None:
        for row_index, row in enumerate(self._rows):
            row.set_selected(row_index == index)

    # ---------- Icons ----------

    def _load_icon(self, entry: object, row: _ResourceRow) -> None:
        """Show the project icon: synchronous for a local file (tests/preview), async for a URL."""
        url = row.icon_url
        if not url:
            return
        cached = self._icon_cache.get(url)
        if cached is not None:
            row.set_icon(cached)
            return
        local = QUrl(url).toLocalFile() if url.startswith("file:") else ("" if "://" in url else url)
        if local:
            pixmap = QPixmap(local)
            if not pixmap.isNull():
                self._icon_cache[url] = pixmap
                row.set_icon(pixmap)
                return
        # Warm start: the icon is already on disk, so paint it in this pass instead of showing the
        # letter tile until a worker thread reports back. One file read of a few dozen KB.
        from launcher.net.http_cache import cached_icon_bytes

        stored = cached_icon_bytes(url)
        if stored is not None:
            pixmap = QPixmap()
            if pixmap.loadFromData(stored):
                self._icon_cache[url] = pixmap
                row.set_icon(pixmap)
                return
        if url in self._icon_pending:
            return
        self._icon_pending.add(url)
        run_in_background(
            lambda: _fetch_icon_bytes(url),
            on_result=lambda data: self._on_icon_ready(url, data),
            on_error=lambda _message: self._icon_pending.discard(url),
            on_finished=lambda: self._icon_pending.discard(url),
        )

    def _on_icon_ready(self, url: str, data: object) -> None:
        self._icon_pending.discard(url)
        if not isinstance(data, (bytes, bytearray)):
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(bytes(data)):
            return
        if len(self._icon_cache) >= 256:  # a long session cannot grow this without a bound
            self._icon_cache.clear()
        self._icon_cache[url] = pixmap
        for row in self._rows:
            if row.icon_url == url:
                row.set_icon(pixmap)

    # ---------- List & details ----------

    def _set_entries(self, entries: list[object]) -> None:
        self._entries = entries
        # Drop the Python references before clear(): clear() emits currentRowChanged(-1), whose handler
        # walks the row list, and the C++ row widgets are already being destroyed at that point.
        self._rows = []
        self.list.clear()
        kind_label = tr(self.KIND_TITLE_KEY[self.kind])
        for index, entry in enumerate(entries):
            # The item's own text is the accessible/testable representation and keeps the exact counts
            # (the card paints the human-readable form on top). This is what test_e2e asserts on.
            main, second, chinese = display_titles(entry)
            text = main if not chinese else f"{main} · {second}"
            if isinstance(entry, ModSearchHit) and entry.downloads:
                text += "   | " + tr("mods.search.downloads", f"{entry.downloads:,}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, entry)
            item.setSizeHint(QSize(0, _ResourceRow.SLOT_HEIGHT))
            self.list.addItem(item)
            row = _ResourceRow(
                entry,
                kind_label,
                self._entry_source_label(entry),
                on_select=lambda row_index=index: self.list.setCurrentRow(row_index),
                on_install=lambda row_index=index: self._install_from_row(row_index),
            )
            self.list.setItemWidget(item, row)
            self._rows.append(row)
            self._load_icon(entry, row)
        self.empty_results.update_for(len(entries))
        if entries:
            self.list.setCurrentRow(0)
        self._sync_row_selection(self.list.currentRow())
        self._update_pager()
        # The page may still be inside its first layout pass here; re-apply the viewport width once the
        # widget hierarchy has settled (see _ResourceList).
        QTimer.singleShot(0, self.list, self.list.sync_item_widths)

    def _clear_selection(self) -> None:
        """Forget the selected hit and reset the inspector, without touching the list."""
        self._hit = None
        self._current = None
        self.title_label.setText(tr("mods.select"))
        self.desc_label.setText("")
        self.dep_required.setText("")
        self.dep_optional.setText("")
        self._update_install_button()

    def _clear_entries(self) -> None:
        """Empty the list and restart paging: a source change invalidates page 1 of the old source."""
        self._set_entries([])
        self._clear_selection()
        self._query = ""
        self._page = 1
        self._has_next = False
        self._update_pager()

    def _update_pager(self) -> None:
        self.page_label.setText(tr("mods.page.label", self._page))
        self.prev_page_button.setEnabled(self._page > 1)
        self.next_page_button.setEnabled(self._has_next)
        # A page past the end can be empty (a source without a total count): keep the pager so the
        # user can still go back instead of being stranded on an empty page.
        self.pager.setVisible(bool(self._entries) or self._page > 1)

    def _on_select(self, row: int) -> None:
        self._sync_row_selection(row)
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        if isinstance(entry, ModSearchHit):
            self._hit = entry
            self._current = None
            main, second, chinese = display_titles(entry)
            # Same order as the row: the Chinese name is the headline, the English title follows it.
            self.title_label.setText(f"{main} · {second}" if chinese else main)
            self.desc_label.setText(entry.description)
            self.dep_required.setText("")
            meta = " · ".join(text for _glyph, text in self._row_meta(row))
            self.dep_optional.setText(meta)
            set_app_status(self, tr("mods.status.default"))
        elif isinstance(entry, ModInfo):
            self._hit = None
            self._show_mod(entry)
        self._update_install_button()

    def _row_meta(self, row: int) -> list[tuple[str, str]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]._meta
        return []

    def _show_mod(self, mod: ModInfo) -> None:
        self._current = mod
        # Same two lines as the row: a project the table knows leads with its Chinese name.
        main, second, chinese = display_titles(mod)
        self.title_label.setText(f"{main} · {second}" if chinese else main)
        self.desc_label.setText(mod.description)
        none_tag = '<span style="color:#8b9bb4;">' + tr("mods.dep.none") + "</span>"
        self.dep_required.setText(
            '<span style="color:#e06c75; font-weight:bold;">' + tr("mods.dep.required") + "</span> "
            + (
                " ".join(_tag(dep, "#e06c75", "#3a2026") for dep in mod.depends)
                if mod.depends
                else none_tag
            )
            + ' <span style="color:#8b9bb4;">' + tr("mods.dep.required.hint") + "</span>"
        )
        self.dep_optional.setText(
            '<span style="color:#8b9bb4;">' + tr("mods.dep.optional") + "</span> "
            + (
                " ".join(_tag(dep, "#8b9bb4", "#232a36") for dep in mod.optional_depends)
                if mod.optional_depends
                else none_tag
            )
            + ' <span style="color:#8b9bb4;">' + tr("mods.dep.optional.hint") + "</span>"
        )
        self._update_install_button()

    def _update_install_button(self) -> None:
        # Every installable category now opens the same version step (a modpack keeps its own
        # confirmation/instance flow), so the button no longer names an instance decision.
        label = tr("mods.install")
        if self._hit is not None:
            self.install_button.setText(label)
            self.install_button.setEnabled(True)
        else:
            self.install_button.setText(label)
            self.install_button.setEnabled(self._current is not None)

    # ---------- Search & paging ----------

    def _project_type(self) -> str:
        return {"resourcepack": "resourcepack", "shaderpack": "shader", "modpack": "modpack"}.get(
            self.kind, "mod"
        )

    def _source_mode(self) -> str:
        page = getattr(self, "page", None)
        return page.current_source() if page is not None else "modrinth"

    def _sort_key(self) -> str:
        return self.sort_combo.currentData() or self._default_sort_key()

    def _on_sort_changed(self, _index: int) -> None:
        # The sort is part of the query: page 1 of the new order, not page 2 of the old one.
        if not self._initial_loaded:
            return
        self._page = 1
        self._load_page()

    def _go_to_page(self, page: int) -> None:
        page = max(1, int(page))
        if page == self._page:
            return
        self._page = page
        self._load_page()

    def _search(self) -> None:
        query = self.search_edit.text().strip()
        if not query and not self._query:
            return
        # Clearing the keyword returns to browsing: the request becomes the popular feed again and
        # a "Relevance" that no longer exists in the browse list falls back to Popular.
        self._query = query
        self._page = 1
        self._populate_sort_combo()
        self._load_page()

    def load_initial(self) -> None:
        """First content for this tab: page 1 of the current source/sort, no keyword."""
        self._initial_loaded = True
        self._query = ""
        self._page = 1
        self._populate_sort_combo()
        self._load_page()

    def _load_page(self) -> None:
        # A key can appear (settings/env) or be rejected since this tab last loaded; re-sync first so
        # a stale "CurseForge" selection cannot outlive the source it names.
        page = getattr(self, "page", None)
        if page is not None:
            page._sync_source_combos()
        self._populate_sort_combo()
        source = self._source_mode()
        query = self._query
        sort = self._sort_key()
        page = self._page
        project_type = self._project_type()
        game_version = self.game_version_edit.text().strip()
        loader = self.loader_combo.currentData() if self.kind == "mod" else ""
        self._request_id += 1
        request_id = self._request_id
        self._has_next = False
        self._update_pager()
        disable_keeping_focus(self.search_button)
        if query:
            set_app_status(self, tr("mods.search.msg.searching", query))
        else:
            set_app_status(self, tr("mods.browse.loading"))

        def do_page() -> object:
            if source == "all":
                from launcher.mods import curseforge as cf

                # Each source keeps its own offset; the merged page is still one page of rows.
                offset = page_offset(page, ALL_PAGE_SIZE)
                sources = [
                    (
                        tr("mods.source.modrinth"),
                        lambda: self._modrinth_page(
                            query, offset, ALL_PAGE_SIZE, sort, project_type
                        ),
                    )
                ]
                if cf.available():
                    sources.append(
                        (
                            tr("mods.source.curseforge"),
                            lambda: cf.search_projects(
                                query,
                                limit=ALL_PAGE_SIZE,
                                offset=offset,
                                sort=sort,
                                kind=self.kind,
                                game_version=game_version,
                                loader=loader,
                            ),
                        )
                    )
                pages, errors = collect_all_sources(sources)
                # Deliberately no de-duplication: "Sodium" from both sources stays two rows, and the
                # merge puts them next to each other with Modrinth first. Ranking runs before the
                # merge so a group keeps its two rows adjacent (the merge only groups neighbours).
                merged = merge_same_titles(
                    self._ranked((hit for source_page in pages for hit in source_page), query, sort)
                )
                has_next = any(
                    source_has_next(source_page, offset, ALL_PAGE_SIZE) for source_page in pages
                )
                return PageOutcome(merged, has_next, errors)

            offset = page_offset(page, PAGE_SIZE)
            if source == "curseforge":
                from launcher.mods import curseforge as cf

                # The combo is rebuilt when the 403 lands; a request already queued at that moment
                # must still not touch the API the service just refused.
                if cf.available():
                    hits = cf.search_projects(
                        query,
                        limit=PAGE_SIZE,
                        offset=offset,
                        sort=sort,
                        kind=self.kind,
                        game_version=game_version,
                        loader=loader,
                    )
                else:
                    hits = self._modrinth_page(query, offset, PAGE_SIZE, sort, project_type)
            else:
                hits = self._modrinth_page(query, offset, PAGE_SIZE, sort, project_type)
            return PageOutcome(
                self._ranked(list(hits), query, sort),
                source_has_next(hits, offset, PAGE_SIZE),
                [],
            )

        run_in_background(
            do_page,
            on_result=lambda outcome: self._on_page_ok(outcome, request_id),
            on_error=lambda message: self._on_page_error(message, request_id),
            on_finished=lambda: self._on_page_finished(request_id),
        )

    @staticmethod
    def _ranked(hits, query: str, sort: str) -> list[ModSearchHit]:
        """Search results with the name-matching rows on top; an explicit sort is left alone.

        The APIs order by their own English relevance model, so a Chinese query lands wherever it
        lands. Ordering only changes for a search under the default "relevance" sort: picking
        "downloads" or "updated" still means what it says.
        """
        ordered = list(hits)
        if not query or sort != "relevance":
            return ordered
        return rank_hits(ordered, query)

    def _modrinth_page(
        self, query: str, offset: int, limit: int, sort: str, project_type: str
    ) -> SearchHits:
        hits = search_projects(
            query, limit=limit, offset=offset, sort=sort, project_type=project_type
        )
        if has_cjk(query) and offset == 0:
            english = english_query(query)
            if english and not hits:
                # Modrinth's index holds English names: "投影" matches nothing there, while the
                # table's answer "litematica" does. One extra request, only when the first was empty.
                hits = search_projects(
                    english, limit=limit, offset=offset, sort=sort, project_type=project_type
                )
            hits = SearchHits(
                self._with_local_chinese(list(hits), query),
                total=int(getattr(hits, "total", 0) or 0),
            )
        return hits

    def _with_local_chinese(self, hits: list[ModSearchHit], query: str) -> list[ModSearchHit]:
        """Chinese query: pin local Chinese-name table matches above the Modrinth results."""
        local_hits = []
        for slug in search_local(query)[:10]:
            try:
                p = fetch_project(slug)
            except ModsError:
                continue
            local_hits.append(
                ModSearchHit(
                    slug=p.get("slug") or slug,
                    title=p.get("title") or slug,
                    description=p.get("description") or "",
                    downloads=int(p.get("downloads") or 0),
                    icon_url=p.get("icon_url") or "",
                    # A project lookup carries both; passing them keeps a local Chinese hit's
                    # row as informative as a Modrinth search row.
                    updated=str(p.get("updated") or ""),
                    loaders=[str(item) for item in (p.get("loaders") or [])],
                    source="modrinth",
                )
            )
        seen = {h.slug for h in local_hits}
        return local_hits + [h for h in hits if h.slug not in seen]

    def _sync_source_availability(self) -> None:
        """Let the page drop a source that a request just proved unusable (a 401/403)."""
        page = getattr(self, "page", None)
        if page is not None:
            page._sync_source_combos()

    def _on_page_ok(self, outcome: PageOutcome, request_id: int) -> None:
        self._sync_source_availability()
        if request_id != self._request_id:
            return
        self._has_next = bool(outcome.has_next)
        self._set_entries(list(outcome.hits))
        if not outcome.hits:
            self._clear_selection()
        if outcome.errors:
            if outcome.hits:
                label, message = outcome.errors[0]
                set_app_status(self, tr("mods.search.msg.partial", label, message), "error")
            else:
                detail = "；".join(f"{label}: {message}" for label, message in outcome.errors)
                set_app_status(self, tr("mods.search.msg.all_fail", detail), "error")
            self._update_pager()
            return
        if not outcome.hits:
            if self._query:
                set_app_status(self, tr("mods.search.msg.none", self._query))
            else:
                set_app_status(self, tr("mods.empty.none"))
            self._update_pager()
            return
        set_app_status(self, tr("mods.search.msg.page", self._page, len(outcome.hits)))
        self._update_pager()

    def _on_page_error(self, message: str, request_id: int) -> None:
        self._sync_source_availability()
        if request_id != self._request_id:
            return
        self._has_next = False
        self._set_entries([])
        self._clear_selection()
        set_app_status(self, tr("mods.search.msg.fail", message), "error")
        self._update_pager()

    def _on_page_finished(self, request_id: int) -> None:
        if request_id != self._request_id:
            return
        self.search_button.setEnabled(True)
        self._update_pager()

    # ---------- slug query (kept) ----------

    def query(self) -> None:
        # The field is hidden outside the Modrinth source; a hidden widget cannot be clicked, but a
        # programmatic caller or a stale signal must not start a Modrinth lookup from CurseForge.
        # isHidden() is the page's own decision; isVisible() would also be false merely because an
        # ancestor is not shown yet.
        if self.slug_edit.isHidden():
            return
        slug = self.slug_edit.text().strip()
        if not slug:
            set_app_status(self, tr("mods.status.default"))
            return
        loader = self.loader_combo.currentData() if self.kind == "mod" else None
        game_version = self.game_version_edit.text().strip()
        disable_keeping_focus(self.query_button)
        set_app_status(self, tr("mods.msg.querying", slug))

        def do_query() -> object:
            project = fetch_project(slug)
            versions = fetch_versions(slug, loader=loader, game_version=game_version)
            if not versions:
                raise ModsError(tr("mods.msg.no_match", loader or "-", game_version))
            picked = versions[0]
            dep_ids = [d.project_id for d in picked.dependencies if d.project_id]
            slugs = resolve_slugs(dep_ids)
            from launcher.mods.modrinth import to_mod_info

            info = to_mod_info(slug, project, picked, slugs)
            return info, picked

        run_in_background(
            do_query,
            on_result=self._on_query_ok,
            on_error=lambda m: set_app_status(self, tr("mods.msg.query_fail", m), "error"),
            on_finished=lambda: self.query_button.setEnabled(True),
        )

    def _on_query_ok(self, result) -> None:
        info, picked = result
        self._current_slug = info.slug
        self._current_mod_version_id = picked.version_id
        self._current_loader = (
            self.loader_combo.currentData() if self.kind == "mod" else None
        )
        self._current_game_version = self.game_version_edit.text().strip()
        # The version the lookup landed on carries the loaders the project supports; the instance
        # step greys a row out from exactly that list.
        self._current_loaders = [str(item) for item in picked.loaders]
        if self.kind == "mod":
            kept = [e for e in self._entries if not (isinstance(e, ModInfo) and e.slug == info.slug)]
            self._set_entries([info] + kept)
            self._show_mod(info)
        else:
            self._hit = None
            self._current = info
            self.title_label.setText(info.title)
            self.desc_label.setText(info.description)
            self.dep_required.setText(
                tr(
                    "mods.info.meta",
                    picked.version_number,
                    ", ".join(picked.loaders) or "-",
                    ", ".join(picked.game_versions) or "-",
                )
            )
            self.dep_optional.setText("")
            self._update_install_button()
        set_app_status(self, tr("mods.msg.query_done", info.title, picked.version_id))

    # ---------- Install: choose the file version, then save it where the user wants ----------

    def _on_install_clicked(self) -> None:
        if self._hit is not None:
            # "All" merges both sources, so the row's own source decides the version request; the
            # page-level source remains the fallback for a hit built without one.
            hit_source = self._hit.source or self.page.current_source()
            self._install_project(self._hit.slug, self._hit.title, source=hit_source)
            return
        self._on_install()

    def _game_dir(self) -> Path:
        cfg, _ = config.load()
        return Path(cfg.game_dir or paths.default_game_dir())

    def _install_hit(self, hit: ModSearchHit) -> None:
        """A result row: the same version -> save-as step every installable project takes."""
        self._install_project(
            hit.slug, hit.title, source=hit.source or self.page.current_source()
        )

    def _install_project(
        self, slug: str, title: str, *, source: str, current_version_id: str = ""
    ) -> None:
        """Step 1 of PCL's flow: fetch every file version of the project and let the user pick one.

        No instance is chosen and nothing is created here. The version list is a background request
        (the same ``fetch_versions`` the slug lookup uses, or CurseForge's file listing); the picker
        then opens in-window, and only an accepted pick moves on to the save dialog.
        """
        if self.kind == "modpack":
            if source == "curseforge":
                set_app_status(self, tr("mods.cf.modpack_unsupported"))
            else:
                self._install_modpack_now(slug, None)
            return
        if source == "curseforge":
            from launcher.mods import curseforge as cf

            if not cf.available():
                # A row from an earlier successful page can survive a 403; never start a request.
                self._sync_source_availability()
                return
        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("mods.versions.loading"))

        def do_load() -> object:
            if source == "curseforge":
                from launcher.mods import curseforge as cf

                return cf.list_files(int(slug))
            return fetch_versions(slug)

        def on_result(versions) -> None:
            self.install_button.setEnabled(True)
            if not versions:
                set_app_status(self, tr("mods.versions.none"))
                return
            dialog = _VersionPickerDialog(
                self,
                list(versions),
                project_title=title or slug,
                current_key=current_version_id,
            )
            if dialog.ask() is not inwindow_dialog.Outcome.ACCEPTED:
                set_app_status(self, tr("mods.status.default"))
                return
            picked = dialog.selected_version()
            if picked is not None:
                self._continue_install(slug, title, source, picked)

        def on_error(message: str) -> None:
            self.install_button.setEnabled(True)
            self._sync_source_availability()
            set_app_status(self, tr("mods.versions.fail", message), "error")

        run_in_background(do_load, on_result=on_result, on_error=on_error)

    def _continue_install(self, slug: str, title: str, source: str, picked: object) -> None:
        """Step 2: compute PCL's default folder, pre-fill the system save dialog, remember the answer.

        The default folder is only a suggestion - the user may navigate anywhere. Cancelling the
        dialog ends the install here: no task is created and no byte is downloaded.
        """
        cfg, _ = config.load()
        game_dir = self._game_dir()
        default_dir = default_content_dir(
            self.kind,
            version_loaders_of(picked),
            version_game_versions_of(picked),
            cfg=cfg,
            game_dir=game_dir,
        )
        fallback = str(slug or "download") + _SAVE_SUFFIX.get(self.kind, ".jar")
        filename = _safe_filename(version_filename_of(picked), fallback)
        chosen = ask_save_file(
            self,
            tr("mods.save.title", title or slug),
            default_dir / filename,
            _SAVE_FILTERS.get(self.kind, "*.jar"),
        )
        if chosen is None:
            set_app_status(self, tr("mods.msg.save_cancelled"))
            self.install_button.setEnabled(True)
            return
        remember_content_dir(self.kind, chosen.parent)
        self._download_version(slug, title, source, picked, chosen)

    def _download_version(
        self, slug: str, title: str, source: str, picked: object, dest: Path
    ) -> None:
        """Step 3: download exactly one file to exactly ``dest`` (no instance, no folder structure)."""
        game_dir = self._game_dir()

        def do_install(_progress) -> object:
            if source == "curseforge":
                from launcher.mods import curseforge as cf

                path = cf.download_file(picked, Path(dest).parent, dest=dest)
                return ModInfo(slug=slug, title=title or slug), path
            install = {
                "mod": install_mod,
                "resourcepack": install_resourcepack,
                "shaderpack": install_shaderpack,
            }[self.kind]
            info = install(
                slug,
                game_dir=game_dir,
                mod_version=picked,
                dest=dest,
                progress=_progress,
            )
            return info, dest

        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("mods.msg.downloading", title or slug))
        self._run_install(
            kind=self.kind,
            title=title or slug,
            job=do_install,
            on_result=self._on_saved,
            on_error=lambda m: set_app_status(self, tr("mods.msg.install_fail", m), "error"),
            on_finished=self._on_install_finished,
            retry=lambda: self._download_version(slug, title, source, picked, dest),
        )

    def _on_saved(self, payload) -> None:
        """The download landed; the status line names the file and keeps the dependency hints."""
        info, path = payload
        message = tr("mods.msg.saved_to", str(path))
        if info.depends:
            message += " | " + tr("mods.dep.required") + " " + ", ".join(info.depends)
        if info.optional_depends:
            message += " | " + tr("mods.dep.optional") + " " + ", ".join(info.optional_depends)
        set_app_status(self, message)

    def _run_install(
        self,
        *,
        kind: str,
        title: str,
        job,
        on_result,
        on_error,
        on_finished,
        retry=None,
        progress_slot=None,
    ) -> _TaskedInstall:
        """Start one install as a task in the window's download list (see ``_TaskedInstall``)."""
        install = _TaskedInstall(self, kind=kind, title=title, retry=retry)
        self._active_install = install
        install.run(
            job,
            on_result=on_result,
            on_error=on_error,
            on_finished=on_finished,
            progress_slot=progress_slot,
        )
        return install

    def cancel_running_task(self) -> None:
        """The window is dropping this page (a language switch rebuilds every page): stop its install.

        The task would otherwise keep the corner icon spinning for the rest of the session: the page's
        callbacks are dropped once the page is gone, so nothing would ever report its ending.
        """
        install, self._active_install = self._active_install, None
        if install is not None:
            install.abandon()

    def _install_modpack_now(self, slug: str, mod_version_id: str | None) -> None:
        """The modpack download itself: behind 安装's confirmation, and behind 重试.

        The pack's own progress line is unchanged - it is the second slot on the same bridge, so the
        status bar and the download page cannot disagree about how far the pack is.
        """
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()

        def do_install(progress) -> object:
            return install_modpack(
                slug,
                game_dir=game_dir,
                cache_dir=paths.launcher_dir() / "cache",
                mod_version_id=mod_version_id,
                progress=progress,
            )

        rate_tracker = RateTracker()

        def on_progress(p) -> None:
            rate_tracker.set_total(p.total_bytes)
            rate, eta = rate_tracker.update(p.done_bytes)
            if rate > 0:
                set_app_status(
                    self,
                    tr(
                        "mods.mp.progress_rate",
                        p.done_files,
                        p.total_files,
                        p.current,
                        format_rate(rate),
                        format_eta(eta),
                    ),
                )
            else:
                set_app_status(
                    self,
                    tr("mods.mp.progress", p.done_files, p.total_files, p.current),
                )

        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("mods.msg.downloading", slug))
        self._run_install(
            kind="modpack",
            title=slug,
            job=do_install,
            on_result=self._on_modpack_installed,
            on_error=lambda m: (
                set_app_status(self, tr("mods.msg.install_fail", m), "error"),
                show_fatal(self, tr("mods.msg.install_fail", m)),
            ),
            on_finished=self._on_install_finished,
            retry=lambda: self._install_modpack_now(slug, mod_version_id),
            progress_slot=on_progress,
        )

    def _on_install(self) -> None:
        if self._current is None:
            return
        mod = self._current

        if self.kind == "modpack":
            # default_yes: installing a modpack replaces the instance's mods, and the old box's
            # default button was "Yes" - Enter keeps meaning "install".
            if not inwindow_dialog.question(
                self,
                tr("mods.mp.confirm.title"),
                tr("mods.mp.confirm.msg", mod.slug),
                default_yes=True,
            ):
                return
            self._install_modpack_now(mod.slug, self._current_mod_version_id or None)
            return

        # No default_yes on the dependency question: this installs without its dependencies, so the
        # safe answer stays the default one, exactly as the old box's "No" default button did.
        if mod.depends and not inwindow_dialog.question(
            self,
            tr("mods.dialog.title"),
            tr("mods.dialog.msg", "、".join(mod.depends)),
        ):
            return

        # The slug lookup always came through Modrinth's project/version API; the version step
        # preselects the version the lookup landed on.
        self._install_project(
            mod.slug,
            mod.title,
            source="modrinth",
            current_version_id=self._current_mod_version_id,
        )

    def _on_install_finished(self) -> None:
        self._active_install = None
        self._update_install_button()

    def _on_modpack_installed(self, pack) -> None:
        message = tr("mods.mp.done", pack.instance_name, pack.minecraft, pack.files_count)
        if pack.loader:
            message += " | " + str(pack.loader) + " " + str(pack.loader_version)
        set_app_status(self, message)

    # ---------- Installed content ----------

    def _content_target_args(self) -> dict:
        cfg, _ = config.load()
        return {
            "game_dir": cfg.game_dir or paths.default_game_dir(),
            "loader": self.loader_combo.currentData() if self.kind == "mod" else None,
            "game_version": self.game_version_edit.text().strip() or None,
            "isolated": cfg.default_isolation,
        }

    # ---------- Folder ----------

    def _open_content_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from launcher.mods.modrinth import resolve_content_dir

        subdir = self.CONTENT_SUBDIR[self.kind]
        args = self._content_target_args()
        game_dir = args.pop("game_dir")
        try:
            target = resolve_content_dir(game_dir, subdir, **args)
        except ModsError:
            target = paths.GamePaths(game_dir).game_dir / subdir
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        set_app_status(self, tr("mods.msg.folder_opened", str(target)))


class ResourcesPage(QWidget):
    """Search / browse Modrinth and CurseForge resources, one category per chip.

    The four categories still live in a QStackedWidget - the tab widget was only ever the switch, and
    the chips replace that switch without touching what each page does. ``self.tabs`` is the stack's
    public name: the accessibility scope walker (tests/test_accessible_names.py) discovers the four
    sub-pages through ``count()`` / ``widget()`` / ``currentWidget()``, which the stack provides too.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("resourcesPage")
        self.mods_tab = _ContentTab("mod")
        self.modpacks_tab = _ContentTab("modpack")
        self.resourcepacks_tab = _ContentTab("resourcepack")
        self.shaderpacks_tab = _ContentTab("shaderpack")
        self.tabs = QStackedWidget()
        self.tabs.setAccessibleName(tr("mods.page.title"))
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self.chips: list[QPushButton] = []
        for index, (tab, title_key) in enumerate(
            (
                (self.mods_tab, "mods.tab.mods"),
                (self.modpacks_tab, "mods.tab.modpacks"),
                (self.resourcepacks_tab, "mods.tab.resourcepacks"),
                (self.shaderpacks_tab, "mods.tab.shaderpacks"),
            )
        ):
            self.tabs.addWidget(tab)
            chip = QPushButton(tr(title_key))
            chip.setObjectName("resourceChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setAccessibleName(tr(title_key))
            self.chip_group.addButton(chip, index)
            self.chips.append(chip)
        self.chips[0].setChecked(True)
        self.chip_group.idClicked.connect(self._on_chip_clicked)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._cf_available = _curseforge_available()
        self.source_combos: list[QComboBox] = []
        for tab in (
            self.mods_tab,
            self.modpacks_tab,
            self.resourcepacks_tab,
            self.shaderpacks_tab,
        ):
            tab.page = self
            combo = _PopupFitCombo()
            # WCAG 4.1.2: the visible "来源" label is a plain QLabel in the tab's grid, and the four
            # tabs each own one, so the name says both what it is and which tab it belongs to.
            combo.setAccessibleName(
                tr(_ContentTab.KIND_TITLE_KEY[tab.kind]) + " · " + tr("mods.source")
            )
            self._populate_source_combo(combo)
            # Minimum, not fixed: a pinned size made QComboBox size its popup list from the closed
            # field, so even a three-item dropdown grew a vertical scrollbar.
            combo.ensurePolished()
            self._fit_source_combo_width(combo)
            combo.setMinimumHeight(36)  # PCL field metric, same as the search field beside it
            combo.currentIndexChanged.connect(self._on_source_changed)
            # (0, 1): the first field column, under the loader dropdown on the mod tab.
            tab._form_grid.addWidget(combo, 0, 1)
            self.source_combos.append(combo)
        self.source_combo = self.source_combos[0]  # canonical reference
        for tab in (self.mods_tab, self.modpacks_tab, self.resourcepacks_tab, self.shaderpacks_tab):
            tab._update_slug_visibility()

        layout = QVBoxLayout(self)
        style_page_layout(layout)
        add_page_top_inset(layout)
        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        for chip in self.chips:
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        layout.addLayout(chip_row)
        layout.addWidget(self.tabs, 1)

    def showEvent(self, event) -> None:
        """Load the first page on first show instead of at construction (lazy page data)."""
        super().showEvent(event)
        # A key may have been added in Settings (or exported as MCLAUNCHER_CF_KEY) while this page
        # was hidden; re-read the source list before the first request of this visit.
        self._sync_source_combos()
        self._on_tab_changed(self.tabs.currentIndex())

    def _populate_source_combo(self, combo: QComboBox) -> None:
        """Rebuild one source dropdown from the sources that are usable right now.

        PCL's order, and PCL's default: All first and selected. Without a key - or after the
        service rejected one - CurseForge is not a disabled entry: it is absent, and "All" remains
        as the Modrinth feed (the merge code already asks only the sources that are available).
        A selection whose data disappeared falls back to All, not to a blank field.
        """
        current = combo.currentData() or DEFAULT_SOURCE
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(tr("mods.source.all"), "all")
            if self._cf_available:
                combo.addItem(tr("mods.source.curseforge"), "curseforge")
            combo.addItem(tr("mods.source.modrinth"), "modrinth")
            index = combo.findData(current)
            combo.setCurrentIndex(max(index, 0))
        finally:
            combo.blockSignals(False)

    def _fit_source_combo_width(self, combo: QComboBox) -> None:
        """Size the closed field for its longest current item (plus arrow and padding)."""
        _fit_combo_width(combo)

    def _sync_source_combos(self) -> None:
        """Re-read the CurseForge availability and rebuild the four dropdowns when it changed.

        Called on show, before every page load, and after a request failed. Rebuilding only on a
        change keeps an open popup and the user's current selection from being disturbed by an
        unrelated page load.
        """
        available = _curseforge_available()
        if available == self._cf_available:
            return
        self._cf_available = available
        for combo in self.source_combos:
            self._populate_source_combo(combo)
            self._fit_source_combo_width(combo)
        for tab in (self.mods_tab, self.modpacks_tab, self.resourcepacks_tab, self.shaderpacks_tab):
            tab._update_slug_visibility()

    def current_source(self) -> str:
        return self.source_combo.currentData() or DEFAULT_SOURCE

    def current_source_label(self) -> str:
        """The display name of the active source ("Modrinth" / "CurseForge") for the result rows."""
        return self.source_combos[0].currentText()

    def refresh(self) -> None:
        """Re-read the page after the current game folder changed (window bridge, not a user action).

        The rows resolve the folder's mods/ resourcepacks/ directory when their action is clicked, and
        the page loads a category once; without this, a page built against the previous folder would
        keep presenting its cached page as the new folder's. A category that has already loaded is
        reloaded on the page it is on (query and page kept); the others re-read when next shown.
        """
        current = self.tabs.currentWidget()
        reload_now = isinstance(current, _ContentTab) and current._initial_loaded
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if isinstance(tab, _ContentTab):
                tab._initial_loaded = False
        if reload_now:
            current._initial_loaded = True
            current._load_page()

    def cancel_running_task(self) -> None:
        """The window is dropping this page (a language switch rebuilds every page): stop its installs.

        ``MainWindow.build_pages`` asks every built page for this before dropping it. Without it the
        task would keep the corner icon spinning for the rest of the session: the worker's callbacks
        are dropped once the page is gone, so nothing would ever report its ending.
        """
        for tab in (
            self.mods_tab,
            self.modpacks_tab,
            self.resourcepacks_tab,
            self.shaderpacks_tab,
        ):
            tab.cancel_running_task()

    def _on_chip_clicked(self, index: int) -> None:
        if self.tabs.currentIndex() != index:
            self.tabs.setCurrentIndex(index)

    def _on_source_changed(self, _index: int) -> None:
        sender = self.sender()
        if sender is None:
            return
        data = sender.currentData()
        for combo in self.source_combos:
            if combo is not sender:
                combo.blockSignals(True)
                combo.setCurrentIndex(max(combo.findData(data), 0))
                combo.blockSignals(False)
        for tab in (
            self.mods_tab,
            self.modpacks_tab,
            self.resourcepacks_tab,
            self.shaderpacks_tab,
        ):
            tab._clear_entries()
            tab._initial_loaded = False
            tab._update_slug_visibility()
        self._on_tab_changed(self.tabs.currentIndex())

    def _on_tab_changed(self, index: int) -> None:
        # The stack can also be switched from outside the chip row (the accessibility scope walker
        # does), so the chip's checked state follows the index rather than only the click.
        if 0 <= index < len(self.chips) and not self.chips[index].isChecked():
            self.chips[index].setChecked(True)
        tab = self.tabs.widget(index)
        if isinstance(tab, _ContentTab) and not tab._initial_loaded:
            tab.load_initial()


def _tag(text: str, color: str, background: str) -> str:
    return (
        '<span style="color:' + color + '; background:' + background
        + '; padding:2px 8px; border-radius:3px; margin-right:4px;">'
        + text + "</span>"
    )
