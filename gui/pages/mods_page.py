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

import json
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
    QFrame,
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

from gui import block_icons, i18n, inwindow_dialog, paint_utils, theme
from gui.errors import show_fatal
from gui.widgets import (
    EmptyState,
    add_page_top_inset,
    add_search_icon,
    apply_no_focus_outline,
    disable_keeping_focus,
    set_app_status,
    style_page_layout,
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
    resolve_slugs,
    search_projects,
)
from launcher.mods.loaders import display_loader_name
from launcher.mods.models import ModInfo, ModSearchHit, SearchHits
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

tr = i18n.tr


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


# ---------- the local instances a mod can be installed into ----------

#: The loader names the interface shows. ``display_loader_name`` owns the same four ids for the
#: Modrinth rows; the instance side needs the id itself (to filter a version request) plus this label.
_LOADER_LABELS = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}


def _profile_json(game_dir: Path, version_id: str) -> dict:
    path = game_dir / "versions" / version_id / (version_id + ".json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _profile_loader(version_id: str) -> str:
    """The loader id a profile id names, or ``"vanilla"``.

    Mirrors ``launcher.diagnostics.facts.derive_version_facts``. The mapping is repeated rather than
    imported because that package costs about half a second on its first import (it is the crash
    knowledge base, loaded on demand), and the resources page must not pay that for twelve lines.
    """
    vid = (version_id or "").strip()
    if vid.startswith("fabric-loader-"):
        return "fabric"
    if vid.startswith("quilt-loader-"):
        return "quilt"
    if vid.startswith("neoforge-"):
        return "neoforge"
    if "-forge-" in vid or vid.endswith("-forge"):
        return "forge"
    return "vanilla"


def _profile_minecraft(version_id: str, loader: str) -> str:
    """The Minecraft version a profile id carries, for the loader that id names."""
    if loader in ("fabric", "quilt"):
        parts = version_id.split("-")
        return parts[-1] if len(parts) >= 4 else ""
    if loader == "forge":
        if "-forge-" in version_id:
            return version_id.partition("-forge-")[0]
        return version_id.removesuffix("-forge")
    if loader == "neoforge":
        base = version_id[len("neoforge-") :]
        if base.startswith("1.") and base.count(".") >= 2 and "-" in base:
            return base.split("-", 1)[0]  # "1.20.1-47.1.79" keeps its explicit prefix
        numbers = base.split(".")
        if len(numbers) >= 2 and numbers[0].isdigit() and numbers[1].isdigit():
            return f"1.{numbers[0]}.{numbers[1]}"
        return ""
    return version_id


def _loader_and_minecraft(game_dir: Path, instance_id: str) -> tuple[str, str]:
    """(loader id, Minecraft version) for one instance, following an inherited profile.

    A copied instance keeps its own folder name and inherits the profile it was copied from, so the
    folder name alone does not always name the loader (the instances page reads the same field).
    """
    seen: set[str] = set()
    current = instance_id
    while current and current not in seen:
        seen.add(current)
        loader = _profile_loader(current)
        if loader != "vanilla":
            return loader, _profile_minecraft(current, loader)
        parent = _profile_json(game_dir, current).get("inheritsFrom")
        if not isinstance(parent, str) or not parent.strip():
            return "vanilla", _profile_minecraft(current, "vanilla")
        current = parent.strip()
    return "vanilla", _profile_minecraft(instance_id, "vanilla")


@dataclass(frozen=True)
class InstanceChoice:
    """One local instance offered by the install step, and why it may be refused.

    ``note`` is the loader-mismatch sentence (empty when the mod can go in): the dialog draws that row
    greyed and cannot select it, which is the whole of the "never force a mismatch" rule.
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


def _loader_mismatch(mod_loaders: list[str], loader_id: str, loader_label: str) -> str:
    """The sentence that greys a row out, or "" when the instance can take the mod.

    An empty ``mod_loaders`` means "the source did not say", never "incompatible": a row without
    loader metadata must stay usable, otherwise a project lookup would offer nothing.
    """
    supported = {str(item).strip().lower() for item in mod_loaders if str(item).strip()}
    if not supported or loader_id in supported:
        return ""
    mod_label = " / ".join(display_loader_name(item) for item in sorted(supported))
    if loader_id == "vanilla":
        return tr("mods.instance.mismatch.vanilla", mod_label)
    return tr("mods.instance.mismatch", mod_label, loader_label)


def instance_choices(game_dir: Path, mod_loaders: list[str], *, cfg=None) -> list[InstanceChoice]:
    """Every instance in ``game_dir``, with its loader and whether this mod fits it.

    This is the local list the install step offers. It reads the folder only - no network request,
    and no instance is created or changed here.
    """
    if cfg is None:
        cfg, _ = config.load()
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
                note=_loader_mismatch(mod_loaders, loader_id, loader_label),
            )
        )
    return choices


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
        self.install.setFixedHeight(32)
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
        meta_metrics = QFontMetrics(meta_font)
        cursor = text_left
        for glyph, text in self._meta:
            text_width = meta_metrics.horizontalAdvance(text)
            segment = 12 + 4 + text_width
            if cursor + segment > text_right:
                break
            self._paint_glyph(
                painter, glyph, QRectF(cursor, 47, 12, 12), hint_color
            )
            painter.setFont(meta_font)
            painter.setPen(hint_color)
            painter.drawText(
                QRectF(cursor + 16, 45, text_width + 2, 16),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                text,
            )
            cursor += segment + 14
        painter.end()

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


class _InstanceRow(QAbstractButton):
    """One local instance in the install step: 40px icon, name, version id, loader.

    Painted rather than composed for the reason the versions page's loader rows are: a list of
    choices must elide its text on one line, and a QLabel either wraps or reports a width wider than
    it paints. It is a real ``QAbstractButton``: it is a Tab stop, Space/Enter activate it, and
    assistive technology reports the choice and its state. A mismatched instance is disabled and
    **not** checkable - the greyed row is the whole of the "never force a loader mismatch" rule.
    """

    HEIGHT = 64
    #: A refused row carries a second text line: the sentence that says why.
    NOTE_HEIGHT = 84
    PADDING = 16
    ICON = 40
    GAP = 12
    RADIUS = 8
    MARK = 16

    def __init__(self, choice: InstanceChoice, icon: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.choice = choice
        self._icon = icon
        self._hover = False
        self.setObjectName("instanceChoiceRow")
        self.setFixedHeight(self.NOTE_HEIGHT if choice.note else self.HEIGHT)
        self.setCheckable(choice.usable)
        self.setEnabled(choice.usable)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if choice.usable else Qt.CursorShape.ArrowCursor
        )
        label = f"{choice.name} · {choice.version_id} · {choice.loader_label}"
        if choice.note:
            label += " · " + choice.note
            self.setToolTip(choice.note)
        self.setAccessibleName(label)  # WCAG 4.1.2
        self.setAccessibleDescription(choice.note)

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
        elif self._hover and self.isEnabled():
            border = QColor(tokens.get("BORDER_HOVER", border.name()))
        if self.hasFocus() and bool(self.property("keyboardFocus")):
            border = QColor(tokens.get("FOCUS_RING", border.name()))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)

        icon_rect = QRectF(self.PADDING, (self.height() - self.ICON) / 2.0, self.ICON, self.ICON)
        if not self._icon.isNull():
            # A refused row keeps its icon readable but dimmed: the loader glyph is what the sentence
            # under it is about, so hiding it would take away the "which loader is this" cue.
            painter.setOpacity(1.0 if self.isEnabled() else 0.45)
            painter.drawPixmap(icon_rect, self._icon, QRectF(self._icon.rect()))
            painter.setOpacity(1.0)

        text_color = QColor(
            tokens.get("FG_TEXT" if self.isEnabled() else "FG_DISABLED", "#1f2a36")
        )
        hint_color = QColor(
            tokens.get("FG_HINT" if self.isEnabled() else "FG_DISABLED", "#4f6070")
        )
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        meta_font = QFont(self.font())
        meta_font.setPixelSize(12)
        name_metrics = QFontMetrics(name_font)
        meta_metrics = QFontMetrics(meta_font)

        left = self.PADDING + self.ICON + self.GAP
        right = self.width() - self.PADDING - self.MARK - 12
        text_width = max(0, right - left)
        name_height = name_metrics.height()
        meta_height = meta_metrics.height()
        block = name_height + 2 + meta_height
        if self.choice.note:
            block += 2 + meta_height
        top = (self.height() - block) / 2.0

        painter.setFont(name_font)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(left, top, text_width, name_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name_metrics.elidedText(self.choice.name, Qt.TextElideMode.ElideRight, text_width),
        )
        painter.setFont(meta_font)
        painter.setPen(hint_color)
        meta = f"{self.choice.version_id} · {self.choice.loader_label}"
        painter.drawText(
            QRectF(left, top + name_height + 2, text_width, meta_height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            meta_metrics.elidedText(meta, Qt.TextElideMode.ElideRight, text_width),
        )
        if self.choice.note:
            painter.setPen(QColor(tokens.get("FG_STATUS_WARNING", "#87691b")))
            painter.drawText(
                QRectF(left, top + name_height + 2 + meta_height + 2, text_width, meta_height),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                meta_metrics.elidedText(
                    self.choice.note, Qt.TextElideMode.ElideRight, text_width
                ),
            )

        self._paint_mark(painter, tokens)

    def _paint_mark(self, painter: QPainter, tokens: dict[str, str]) -> None:
        """The single-choice indicator: an empty ring, or a filled accent disc with a tick."""
        centre = QPointF(
            self.width() - self.PADDING - self.MARK / 2.0, self.height() / 2.0
        )
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
        ring = "FG_DISABLED" if not self.isEnabled() else "FG_HINT"
        painter.setPen(QPen(QColor(tokens.get(ring, "#4f6070")), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, radius, radius)


class _InstancePickerDialog(inwindow_dialog.InWindowDialog):
    """Second step of the resources page: choose which **local** instance gets the mod.

    The panel is a window-internal card (``InWindowDialog``: a child widget, never a native window),
    and it speaks the versions page's install-step language - one card, one row per choice with the
    same 40px block icon the instances page uses, and one 48px accent pill as the single primary
    action. The rows come from :func:`instance_choices`, i.e. from ``versions/`` on disk; nothing here
    downloads a loader or creates an instance.

    ``selected_choice()`` is what the page installs into. A row whose loader does not match the mod
    is disabled and cannot be checked, so "force install anyway" has no path.
    """

    CARD_WIDTH = 560
    LIST_MAX_HEIGHT = 312

    def __init__(
        self, parent: QWidget | None, choices: list[InstanceChoice], *, mod_title: str
    ) -> None:
        super().__init__(
            parent,
            title=tr("mods.instance.title"),
            severity="question",
            card_width=self.CARD_WIDTH,
        )
        self.choices = list(choices)
        self.rows: list[_InstanceRow] = []
        self.wants_versions = False
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        # A plain label, not InWindowDialog.add_body's scroll area: the hint is one short sentence, and
        # a scroll area's 192px size hint left a hole between the hint and the empty state.
        self.hint_label = QLabel(tr("mods.instance.hint", mod_title))
        self.hint_label.setObjectName("overlayBody")
        self.hint_label.setWordWrap(True)
        self.hint_label.setMaximumWidth(self.CARD_WIDTH - 2 * inwindow_dialog.CARD_PADDING)
        self.card_layout.addWidget(self.hint_label)

        self.list_area = QScrollArea()
        self.list_area.setObjectName("overlayScroll")  # the shared transparent scroll surface
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
        for choice in self.choices:
            icon = block_icons.instance_icon_pixmap(
                choice.instance, choice.folder, _InstanceRow.ICON, game_dir=choice.game_dir
            )
            row = _InstanceRow(choice, icon)
            row.clicked.connect(self._sync_install)
            self._group.addButton(row)
            rows_layout.addWidget(row)
            self.rows.append(row)
        rows_layout.addStretch(1)
        self.list_area.setWidget(container)
        self.list_area.setVisible(bool(self.choices))
        self.card_layout.addWidget(self.list_area)

        self.empty_state = EmptyState(tr("mods.instance.empty"))
        self.empty_state.update_for(len(self.choices))
        self.versions_button = QPushButton(tr("mods.instance.go_versions"))
        self.versions_button.setObjectName("secondaryButton")
        self.versions_button.setFixedHeight(32)
        self.versions_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.versions_button.setAccessibleName(tr("mods.instance.go_versions"))
        self.versions_button.clicked.connect(self._open_versions)
        empty_row = QHBoxLayout()
        empty_row.setSpacing(0)
        empty_row.addStretch(1)
        empty_row.addWidget(self.versions_button)
        empty_row.addStretch(1)
        self.empty_state.setVisible(not self.choices)
        self.versions_button.setVisible(not self.choices)
        self.card_layout.addWidget(self.empty_state)
        self.card_layout.addLayout(empty_row)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.addStretch(1)
        self.cancel_button = QPushButton(tr("mods.instance.cancel"))
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.setFixedHeight(36)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(
            lambda: self._finish(inwindow_dialog.Outcome.REJECTED)
        )
        self.install_button = QPushButton(tr("mods.instance.install"))
        self.install_button.setObjectName("installPrimary")  # the versions step's pill
        self.install_button.setFixedHeight(48)
        self.install_button.setMinimumWidth(200)
        self.install_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(
            lambda: self._finish(inwindow_dialog.Outcome.ACCEPTED)
        )
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.install_button)
        self.card_layout.addLayout(button_row)
        self._mark_default(self.install_button, inwindow_dialog.Outcome.ACCEPTED)

    # ---------- data ----------

    def selected_choice(self) -> InstanceChoice | None:
        for row in self.rows:
            if row.isChecked():
                return row.choice
        return None

    def _sync_install(self, *_args) -> None:
        self.install_button.setEnabled(self.selected_choice() is not None)

    # ---------- keyboard ----------

    def _focus_initial(self) -> None:
        """Start on the first instance that can actually take the mod."""
        for row in self.rows:
            if row.isEnabled():
                row.setFocus(Qt.FocusReason.TabFocusReason)
                return
        super()._focus_initial()

    def keyPressEvent(self, event) -> None:
        """Enter is the pill, but only once a row is chosen; otherwise it must do nothing."""
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and self.selected_choice() is None
        ):
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------- navigation ----------

    def _open_versions(self) -> None:
        """The empty state's way out: close, and let the page open the versions page."""
        self.wants_versions = True
        self._finish(inwindow_dialog.Outcome.REJECTED)


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


class _ContentTab(QWidget):
    """Generic resource tab. kind: mod / modpack / resourcepack / shaderpack"""

    CONTENT_SUBDIR: ClassVar[dict[str, str]] = {
        "mod": "mods",
        "resourcepack": "resourcepacks",
        "shaderpack": "shaderpacks",
    }

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

        self.loader_combo = QComboBox()
        for name in ("fabric", "forge", "neoforge", "quilt"):
            self.loader_combo.addItem(display_loader_name(name), name)
        # WCAG 4.1.2: the row's "loader" label is a plain QLabel beside this combo, so it never became
        # the combo's name (only a QFormLayout row does that automatically).
        self.loader_combo.setAccessibleName(tr("mods.loader"))
        self.game_version_edit = QLineEdit("1.20.1")
        self.game_version_edit.setAccessibleName(tr("mods.game_version"))  # WCAG 4.1.2
        self.slug_edit = QLineEdit()
        self.slug_edit.setAccessibleName(tr("mods.slug.label"))  # WCAG 4.1.2
        self.slug_edit.setPlaceholderText(tr("mods.slug.placeholder"))
        self.query_button = QPushButton(tr("mods.query"))
        self.query_button.setObjectName("queryButton")
        self.search_edit = QLineEdit()
        self.search_edit.setAccessibleName(tr("mods.query"))  # WCAG 4.1.2
        add_search_icon(self.search_edit)
        placeholder_key = {
            "resourcepack": "mods.search.placeholder.rp",
            "shaderpack": "mods.search.placeholder.sp",
            "modpack": "mods.search.placeholder.modpack",
        }.get(kind, "mods.search.placeholder")
        self.search_edit.setPlaceholderText(tr(placeholder_key))
        self.search_button = QPushButton(tr("mods.search.button"))
        self.sort_combo = _PopupFitCombo()
        for key, label_key in (
            ("relevance", "mods.sort.relevance"),
            ("downloads", "mods.sort.downloads"),
            ("updated", "mods.sort.updated"),
            ("newest", "mods.sort.newest"),
        ):
            self.sort_combo.addItem(tr(label_key), key)
        # WCAG 4.1.2: one sort combo per tab, so the name says which tab it belongs to.
        self.sort_combo.setAccessibleName(
            tr("mods.sort") + " · " + tr(self.KIND_TITLE_KEY[kind])
        )
        # Minimum, not fixed: a pinned height made the popup size its list from the closed field.
        # ensurePolished() first: the sheet's own min-height would otherwise win over this one.
        self.sort_combo.ensurePolished()
        self.sort_combo.setMinimumHeight(36)
        self.sort_combo.setMinimumWidth(
            max(
                self.sort_combo.fontMetrics().horizontalAdvance(self.sort_combo.itemText(index))
                for index in range(self.sort_combo.count())
            )
            + 60
        )
        self.install_button = QPushButton(tr("mods.install"))
        self.open_folder_button = QPushButton(tr("mods.open_folder"))
        self.open_folder_button.setObjectName("secondaryButton")
        # PCL metrics: 36px fields and buttons. The stylesheet owns radius/border/padding; the height
        # is fixed here so it cannot drift with the font of a language.
        for control in (
            self.loader_combo,
            self.game_version_edit,
            self.slug_edit,
            self.search_edit,
            self.query_button,
            self.search_button,
        ):
            control.setFixedHeight(36)
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

        query_row = QHBoxLayout()
        query_row.setSpacing(8)
        if kind == "mod":
            query_row.addWidget(QLabel(tr("mods.loader")))
            query_row.addWidget(self.loader_combo)
        query_row.addWidget(QLabel(tr("mods.game_version")))
        query_row.addWidget(self.game_version_edit)
        query_row.addWidget(self.slug_edit, 1)
        query_row.addWidget(self.query_button)

        self.pager = QWidget()
        pager_row = QHBoxLayout(self.pager)
        pager_row.setContentsMargins(0, 0, 0, 0)
        pager_row.setSpacing(8)
        self.prev_page_button = QPushButton(tr("mods.page.prev"))
        self.next_page_button = QPushButton(tr("mods.page.next"))
        for button in (self.prev_page_button, self.next_page_button):
            button.setObjectName("secondaryButton")
            button.setFixedHeight(32)
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
        self._search_row = QHBoxLayout()
        self._search_row.setSpacing(8)
        self._search_row.addWidget(self.sort_combo)
        self._search_row.addWidget(self.search_edit, 1)
        self._search_row.addWidget(self.search_button)
        layout.addLayout(self._search_row)
        layout.addLayout(query_row)
        layout.addLayout(body, 1)

        self.list.currentRowChanged.connect(self._on_select)
        self.query_button.clicked.connect(self.query)
        self.search_button.clicked.connect(self._search)
        self.search_edit.returnPressed.connect(self._search)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self.prev_page_button.clicked.connect(lambda: self._go_to_page(self._page - 1))
        self.next_page_button.clicked.connect(lambda: self._go_to_page(self._page + 1))
        self.install_button.clicked.connect(self._on_install_clicked)
        if kind != "modpack":
            self.open_folder_button.clicked.connect(self._open_content_folder)

        self._update_install_button()

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
        QTimer.singleShot(0, self.list.sync_item_widths)

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
        # A mod always goes into a local instance, so the button names that decision. The other
        # categories keep their old wording: resource packs, shaders and modpacks are not scoped to an
        # instance by this page.
        mod_label = tr("mods.install_to_instance") if self.kind == "mod" else None
        if self._hit is not None:
            self.install_button.setText(mod_label or tr("mods.search.pick"))
            self.install_button.setEnabled(True)
        else:
            self.install_button.setText(mod_label or tr("mods.install"))
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
        return self.sort_combo.currentData() or "relevance"

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
        if not query:
            return
        self._query = query
        self._page = 1
        self._load_page()

    def load_initial(self) -> None:
        """First content for this tab: page 1 of the current source/sort, no keyword."""
        self._initial_loaded = True
        self._query = ""
        self._page = 1
        self._load_page()

    def _load_page(self) -> None:
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
                    ),
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
                    ),
                ]
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

    def _on_page_ok(self, outcome: PageOutcome, request_id: int) -> None:
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

    # ---------- Install ----------

    def _on_install_clicked(self) -> None:
        if self._hit is not None:
            # "All" merges both sources, so the row's own source decides the install path; the
            # page-level source remains the fallback for a hit built without one.
            hit_source = self._hit.source or self.page.current_source()
            if hit_source == "curseforge":
                self._install_cf(self._hit)
            elif self.kind == "mod":
                self._install_hit(self._hit)
            else:
                self._install_slug(
                    self._hit.slug,
                    mod_version_id=None,
                    loader=None,
                    game_version=None,
                    title=self._hit.title,
                )
            return
        self._on_install()

    # ---------- the install step: which local instance gets this mod ----------

    def _game_dir(self) -> Path:
        cfg, _ = config.load()
        return Path(cfg.game_dir or paths.default_game_dir())

    def _pick_instance(self, mod_title: str, mod_loaders: list[str]) -> InstanceChoice | None:
        """Show the local-instance step and return the chosen instance, or None.

        The dialog owns only the choice; the page still owns the download. ``wants_versions`` is the
        empty state's 去版本页 button: the dialog is gone by then, so the page can switch pages.
        """
        dialog = _InstancePickerDialog(
            self, instance_choices(self._game_dir(), mod_loaders), mod_title=mod_title
        )
        outcome = dialog.ask()
        if dialog.wants_versions:
            self._go_to_versions()
            return None
        if outcome is not inwindow_dialog.Outcome.ACCEPTED:
            return None
        return dialog.selected_choice()

    def _go_to_versions(self) -> None:
        """Open the versions page in the main rail (the same route the instances page's empty state uses)."""
        from gui.main_window import NAV_KEYS

        sidebar = getattr(self.window(), "sidebar", None)
        if sidebar is None:
            return
        sidebar.setCurrentRow(NAV_KEYS.index("versions"))

    def _install_hit(self, hit: ModSearchHit) -> None:
        """A Modrinth mod row: choose the instance, then let that instance pick the version."""
        choice = self._pick_instance(hit.title, [str(item) for item in hit.loaders])
        if choice is None:
            return
        self._install_mod_into(choice, hit.slug, hit.title)

    def _install_mod_into(self, choice: InstanceChoice, slug: str, title: str) -> None:
        """Download one mod into the chosen instance's own mods directory.

        The instance supplies the loader and the game version the version request is filtered by;
        ``version_id`` + ``isolated`` make ``install_mod`` resolve the instance folder itself - the
        very directory the instances page counts. Nothing is created and no loader is downloaded.
        """
        loader = None if choice.loader_id == "vanilla" else choice.loader_id
        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("mods.msg.downloading_into", title or slug, choice.name))

        def do_install() -> object:
            return install_mod(
                slug,
                game_dir=choice.game_dir,
                loader=loader,
                game_version=choice.minecraft or None,
                version_id=choice.instance_id,
                isolated=choice.isolated,
            )

        run_in_background(
            do_install,
            on_result=lambda info: self._on_mod_installed(info, choice.name),
            on_error=lambda m: set_app_status(self, tr("mods.msg.install_fail", m), "error"),
            on_finished=self._on_install_finished,
        )

    def _on_mod_installed(self, info: ModInfo, instance_name: str) -> None:
        """Stays on the resources page; the status line names the instance that just gained the mod."""
        message = tr("mods.msg.installed_into", instance_name, info.title, info.slug)
        if info.depends:
            message += " | " + tr("mods.dep.required") + " " + ", ".join(info.depends)
        if info.optional_depends:
            message += " | " + tr("mods.dep.optional") + " " + ", ".join(info.optional_depends)
        set_app_status(self, message)

    def _install_cf(self, hit: ModSearchHit) -> None:
        """Install from CurseForge into the chosen instance (a mod is always instance-scoped)."""
        if self.kind == "modpack":
            set_app_status(self, tr("mods.cf.modpack_unsupported"))
            return
        from launcher.mods import curseforge as cf
        from launcher.mods import resolve_content_dir

        choice = None
        if self.kind == "mod":
            choice = self._pick_instance(hit.title, [str(item) for item in hit.loaders])
            if choice is None:
                return
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()
        if choice is not None:
            game_version = choice.minecraft
            loader = "" if choice.loader_id == "vanilla" else choice.loader_id
        else:
            game_version = self.game_version_edit.text().strip()
            loader = ""
        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("mods.cf.downloading", hit.title))

        def do_install() -> object:
            files = cf.list_files(int(hit.slug), game_version=game_version, loader=loader)
            if not files:
                raise ModsError(tr("mods.cf.no_file", game_version or hit.title))
            picked = files[0]
            if choice is not None:
                target = choice.mods_dir
            else:
                subdir = {"resourcepack": "resourcepacks", "shaderpack": "shaderpacks"}.get(
                    self.kind, self.kind
                )
                target = resolve_content_dir(game_dir, subdir, isolated=cfg.default_isolation)
            return cf.download_file(picked, target), picked

        run_in_background(
            do_install,
            on_result=lambda result: self._on_cf_installed(
                result, choice.name if choice is not None else ""
            ),
            on_error=lambda m: set_app_status(self, tr("mods.cf.fail", m), "error"),
            on_finished=lambda: self.install_button.setEnabled(True),
        )

    def _on_cf_installed(self, result, instance_name: str = "") -> None:
        target, _picked = result
        if instance_name:
            set_app_status(self, tr("mods.cf.done_into", instance_name, target.name))
        else:
            set_app_status(self, tr("mods.cf.done", target.name))

    def _install_slug(
        self,
        slug: str,
        *,
        mod_version_id: str | None,
        loader: str | None,
        game_version: str | None,
        title: str | None,
    ) -> None:
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()
        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("mods.msg.downloading", title or slug))

        def do_install() -> object:
            if self.kind == "resourcepack":
                return install_resourcepack(
                    slug,
                    game_dir=game_dir,
                    game_version=game_version,
                    mod_version_id=mod_version_id,
                    isolated=cfg.default_isolation,
                )
            if self.kind == "shaderpack":
                return install_shaderpack(
                    slug,
                    game_dir=game_dir,
                    game_version=game_version,
                    mod_version_id=mod_version_id,
                    isolated=cfg.default_isolation,
                )
            return install_mod(
                slug,
                game_dir=game_dir,
                loader=loader,
                game_version=game_version,
                mod_version_id=mod_version_id,
                isolated=cfg.default_isolation,
            )

        run_in_background(
            do_install,
            on_result=self._on_installed,
            on_error=lambda m: set_app_status(self, tr("mods.msg.install_fail", m), "error"),
            on_finished=self._on_install_finished,
        )

    def _on_install(self) -> None:
        if self._current is None:
            return
        mod = self._current
        cfg, _ = config.load()
        game_dir = cfg.game_dir or paths.default_game_dir()

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

            def do_install(progress) -> object:
                return install_modpack(
                    mod.slug,
                    game_dir=game_dir,
                    cache_dir=paths.launcher_dir() / "cache",
                    mod_version_id=self._current_mod_version_id or None,
                    progress=progress,
                )

            bridge = ProgressBridge()
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

            bridge.progress.connect(on_progress)
            disable_keeping_focus(self.install_button)
            set_app_status(self, tr("mods.msg.downloading", mod.slug))
            run_in_background(
                do_install,
                bridge,
                on_result=self._on_modpack_installed,
                on_error=lambda m: (
                    set_app_status(self, tr("mods.msg.install_fail", m), "error"),
                    show_fatal(self, tr("mods.msg.install_fail", m)),
                ),
                on_finished=self._on_install_finished,
            )
            return

        # No default_yes on the dependency question: this installs without its dependencies, so the
        # safe answer stays the default one, exactly as the old box's "No" default button did.
        if mod.depends and not inwindow_dialog.question(
            self,
            tr("mods.dialog.title"),
            tr("mods.dialog.msg", "、".join(mod.depends)),
        ):
            return

        if self.kind == "mod":
            # A mod is installed into a local instance, never into the shared folder and never by
            # building a new instance from the network.
            choice = self._pick_instance(mod.title, self._current_loaders)
            if choice is None:
                return
            self._install_mod_into(choice, mod.slug, mod.title)
            return

        def do_install() -> object:
            if self.kind == "resourcepack":
                return install_resourcepack(
                    mod.slug,
                    game_dir=game_dir,
                    game_version=self._current_game_version or None,
                    mod_version_id=self._current_mod_version_id or None,
                    isolated=cfg.default_isolation,
                )
            return install_shaderpack(
                mod.slug,
                game_dir=game_dir,
                game_version=self._current_game_version or None,
                mod_version_id=self._current_mod_version_id or None,
                isolated=cfg.default_isolation,
            )

        disable_keeping_focus(self.install_button)
        set_app_status(self, tr("mods.msg.downloading", mod.slug))
        run_in_background(
            do_install,
            on_result=self._on_installed,
            on_error=lambda m: (
                set_app_status(self, tr("mods.msg.install_fail", m), "error"),
                show_fatal(self, tr("mods.msg.install_fail", m)),
            ),
            on_finished=self._on_install_finished,
        )

    def _on_install_finished(self) -> None:
        self._update_install_button()

    def _on_installed(self, info: ModInfo) -> None:
        if self.kind == "resourcepack":
            set_app_status(self, tr("mods.rp.done", info.title, info.slug))
            return
        if self.kind == "shaderpack":
            set_app_status(self, tr("mods.sp.done", info.title, info.slug))
            return
        message = tr("mods.msg.installed", info.title, info.slug)
        if info.depends:
            message += " | " + tr("mods.dep.required") + " " + ", ".join(info.depends)
        if info.optional_depends:
            message += " | " + tr("mods.dep.optional") + " " + ", ".join(info.optional_depends)
        set_app_status(self, message)

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
            chip.setFixedHeight(32)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setAccessibleName(tr(title_key))
            self.chip_group.addButton(chip, index)
            self.chips.append(chip)
        self.chips[0].setChecked(True)
        self.chip_group.idClicked.connect(self._on_chip_clicked)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.source_combos: list[QComboBox] = []
        for tab in (
            self.mods_tab,
            self.modpacks_tab,
            self.resourcepacks_tab,
            self.shaderpacks_tab,
        ):
            tab.page = self
            combo = _PopupFitCombo()
            combo.addItem(tr("mods.source.modrinth"), "modrinth")
            combo.addItem(tr("mods.source.curseforge"), "curseforge")
            combo.addItem(tr("mods.source.all"), "all")
            # WCAG 4.1.2: it sits in the search row with no adjacent label at all, and the four tabs
            # each own one, so the name says both what it is and which tab it belongs to.
            combo.setAccessibleName(
                tr(_ContentTab.KIND_TITLE_KEY[tab.kind]) + " · " + tr("mods.source")
            )
            # Wide enough for "CurseForge" plus the arrow and generous padding. Minimum, not fixed:
            # a pinned size made QComboBox size its popup list from the closed field, so even a
            # three-item dropdown grew a vertical scrollbar.
            combo.ensurePolished()
            combo.setMinimumWidth(combo.fontMetrics().horizontalAdvance("CurseForge") + 60)
            combo.setMinimumHeight(36)  # PCL field metric, same as the search field beside it
            combo.currentIndexChanged.connect(self._on_source_changed)
            tab._search_row.insertWidget(0, combo)
            self.source_combos.append(combo)
        self.source_combo = self.source_combos[0]  # canonical reference

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
        self._on_tab_changed(self.tabs.currentIndex())

    def current_source(self) -> str:
        return self.source_combo.currentData() or "modrinth"

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
