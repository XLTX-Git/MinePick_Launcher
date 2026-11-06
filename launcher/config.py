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

"""Launcher config: pydantic model + JSON persistence (atomic writes)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from launcher import paths

CONFIG_FILENAME = "config.json"

# The launcher's own accessibility prompt policy. It never changes the game's
# built-in onboarding; it only decides whether this launcher asks first.
ACCESSIBILITY_PREFERENCE_DEFAULT = "auto"
ACCESSIBILITY_PREFERENCE_CHOICES = ("auto", "always_on", "never")

# Download source preference, one setting per endpoint family (launcher.meta.sources owns the
# meaning of the values and the mirror addresses). "mirror" = mirrors only, "auto" = Mojang first
# with a mirror fallback, "official" = Mojang only (a failure is reported, never silently mirrored).
DOWNLOAD_SOURCE_DEFAULT = "auto"
DOWNLOAD_SOURCE_CHOICES = ("mirror", "auto", "official")

# Common game languages (code, display name); en_us is built into the game, the rest are the game's bundled language packs
GAME_LANGUAGES: list[tuple[str, str]] = [
    ("", ""),
    ("zh_cn", "简体中文"),
    ("zh_tw", "繁體中文"),
    ("en_us", "English (US)"),
    ("ja_jp", "日本語"),
    ("ko_kr", "한국어"),
    ("ru_ru", "Русский"),
    ("de_de", "Deutsch"),
    ("fr_fr", "Français"),
    ("es_es", "Español"),
    ("pt_br", "Português (Brasil)"),
    ("it_it", "Italiano"),
    ("pl_pl", "Polski"),
    ("tr_tr", "Türkçe"),
    ("uk_ua", "Українська"),
]


def folder_display_name(path: Path | str) -> str:
    """The label a folder gets by default: its own directory name (the whole path if it has none)."""
    text = str(path or "").strip()
    if not text:
        return ""
    name = Path(text).name
    return name or text


class GameFolder(BaseModel):
    """One Minecraft game folder: its own ``versions/``, ``mods/``, ``resourcepacks/``, ...

    ``name`` is the display label (defaulting to the folder's own name) and ``is_default`` marks the
    folder every page reads right now. ``LauncherConfig.game_dir`` mirrors that folder's path - see
    :meth:`LauncherConfig.active_folder` for why both exist.
    """

    model_config = ConfigDict(validate_assignment=True)

    path: Path
    name: str = ""
    is_default: bool = False

    def display_name(self) -> str:
        """The label to show: the user's name, or the directory's own name when it is empty."""
        return self.name.strip() or folder_display_name(self.path)


def _folder_key(path: Path | str) -> str:
    """Identity of a folder for lookups: the OS's own comparison rule, not the display string.

    Windows paths are case-insensitive and accept both separators, so ``D:\\MC`` and ``d:/mc`` are
    one folder. The value is *only* used for comparison - the stored/displayed path is left exactly
    as the user gave it (resolving it would leak the machine's absolute path into the screenshots and
    would rewrite a deliberately relative config value).
    """
    text = os.path.expanduser(str(path or "")).strip()
    if not text:
        return ""
    return os.path.normcase(os.path.abspath(text)).rstrip("\\/")


def _folder_from_entry(entry: object) -> GameFolder | None:
    """Best-effort coercion of one stored folder entry; unreadable entries are dropped, not fatal."""
    if isinstance(entry, GameFolder):
        if not str(entry.path).strip():
            return None
        return GameFolder(path=entry.path, name=entry.name, is_default=entry.is_default)
    if isinstance(entry, dict):
        if not str(entry.get("path") or "").strip():
            return None
        try:
            return GameFolder(**entry)
        except ValidationError:
            return None
    if isinstance(entry, (str, Path)):
        if not str(entry).strip():
            return None
        try:
            return GameFolder(path=entry)
        except ValidationError:
            return None
    return None


def _normalized_folders(entries: object) -> list[GameFolder]:
    """Folders in insertion order, de-duplicated by path, with the entries a config can hold."""
    if not isinstance(entries, (list, tuple)):
        return []
    folders: list[GameFolder] = []
    seen: set[str] = set()
    for entry in entries:
        folder = _folder_from_entry(entry)
        if folder is None:
            continue
        key = _folder_key(folder.path)
        if not key or key in seen:
            continue
        seen.add(key)
        folders.append(folder)
    return folders


def _active_of(folders: list[GameFolder], game_dir: object = None) -> GameFolder | None:
    """Which folder is current: the one ``game_dir`` names, else the flagged one, else the first."""
    key = _folder_key(game_dir) if game_dir else ""
    if key:
        for folder in folders:
            if _folder_key(folder.path) == key:
                return folder
    for folder in folders:
        if folder.is_default:
            return folder
    return folders[0] if folders else None


class LauncherConfig(BaseModel):
    """User-editable launcher config. Unknown fields are dropped with a warning on load.

    Multiple game folders are stored in ``game_folders``; ``game_dir`` is kept as the **current**
    folder's path so every existing reader (instances, mods, resources, versions, the CLI and the
    tests) keeps addressing exactly one directory. The two are kept in sync on load and on save; a
    config written before the folder list existed (only ``game_dir``) is migrated to a one-item list
    without changing the directory any reader resolves.
    """

    model_config = ConfigDict(validate_assignment=True)

    game_dir: Path | None = Field(
        default=None,
        description=(
            "Minecraft game directory; None means use the default rule. This is the *current* folder "
            "and mirrors the matching game_folders entry"
        ),
    )
    game_folders: list[GameFolder] = Field(
        default_factory=list,
        description="Registered game folders; game_dir is the path of the current one",
    )
    java_path: Path | None = Field(default=None, description="Java executable; None means auto-detect")
    memory_gb: float = Field(default=4.0, gt=0, le=64, description="Allocated memory (GB)")
    memory_auto: bool = Field(
        default=False,
        description="Auto-allocate heap size from mod count and available RAM at launch time",
    )
    max_concurrent_downloads: int = Field(default=4, ge=1, le=32, description="Download concurrency")
    selected_account: str | None = Field(default=None, description="Account id selected by default")
    selected_instance: str = Field(
        default="",
        description=(
            "Instance id (the versions/<id> folder name) the launch page starts on; empty keeps the "
            "historical behaviour of showing the first installed profile"
        ),
    )
    auto_install_java: bool = Field(default=False, description="Auto-download Java from Adoptium when missing")
    msa_client_id: str | None = Field(
        default=None,
        description="Microsoft OAuth client id; None means use the built-in public client id (override by registering your own Azure app)",
    )
    default_isolation: bool = Field(
        default=True,
        description="Default isolation policy for new instances: each instance keeps its own saves/mods/config under versions/<id>/ (an instance can override this)",
    )
    auto_crash_analysis: bool = Field(
        default=True,
        description="Analyse the logs automatically when a launch fails or the game crashes, and show the diagnosis dialog",
    )
    blur_dialogs: bool = Field(
        default=True,
        description="Blur the window behind in-window dialogs (crash diagnosis); the backdrop is always blurred, so the setting only remains for config compatibility and is no longer read",
    )
    game_language: str = Field(
        default="",
        description="Game language code (zh_cn/en_us/ja_jp...); empty = follow in-game setting (not forced)",
    )
    accessibility_preference: str = Field(
        default=ACCESSIBILITY_PREFERENCE_DEFAULT,
        description=(
            "Launcher accessibility prompt policy (auto / always_on / never); it only affects the "
            "launcher's prompt, not the game's own onboarding"
        ),
    )
    ui_language: str = Field(
        default="zh_cn",
        description="Launcher UI language (zh_cn/zh_tw/en_us/ja_jp/ko_kr/ru_ru/fr_fr/es_es/de_de)",
    )
    language_initialized: bool = Field(
        default=False,
        description="Whether the game language was initialized (auto-set only on first launch, never changed automatically afterward)",
    )
    jvm_args: str = Field(
        default="",
        description="Custom JVM args (appended after the version's default args; -Xmx/-Xms are ignored, memory is controlled by the allocated-memory setting)",
    )
    token_encryption: bool = Field(
        default=False,
        description="Encrypted token storage (encrypt sensitive tokens in accounts.json with a password)",
    )
    download_speed_limit_kb: int = Field(
        default=0,
        ge=0,
        le=1_048_576,
        description="Download speed limit (KB/s); 0 = unlimited",
    )
    download_segments: int = Field(
        default=1,
        ge=1,
        le=16,
        description=(
            "Shards per file: 1 = one stream per file (the historical behaviour); above 1 splits each "
            "file into that many parallel Range requests when the server supports it"
        ),
    )
    download_source_meta: str = Field(
        default=DOWNLOAD_SOURCE_DEFAULT,
        description=(
            "Download source for versions and manifests (version manifest/JSON, client.jar, "
            "libraries, asset index): mirror / auto / official; see launcher.meta.sources"
        ),
    )
    download_source_assets: str = Field(
        default=DOWNLOAD_SOURCE_DEFAULT,
        description=(
            "Download source for resource files (assets objects under "
            "resources.download.minecraft.net): mirror / auto / official"
        ),
    )
    window_start_mode: str = Field(
        default="default",
        description="Window start state: default / maximized / minimized / remember",
    )
    window_geometry: str = Field(
        default="",
        description="Geometry info saved when remembering the last window size (QByteArray hex)",
    )
    auto_close_on_launch: bool = Field(
        default=False,
        description="Legacy flag (kept for migration): hide-and-exit after the game starts",
    )
    after_launch_behavior: str = Field(
        default="keep",
        description="Behavior after the game starts: keep / hide / exit",
    )
    trim_memory_on_launch: bool = Field(
        default=True,
        description="Trim the launcher working set after the game starts",
    )
    demo_mode: bool = Field(
        default=False,
        description="Demo Mode",
    )
    theme: str = Field(
        default="dark", description="UI theme (dark / light)"
    )
    accent_color: str = Field(
        default="",
        description="Custom accent color as #rrggbb; empty = the theme's default accent",
    )
    ui_font: str = Field(
        default="",
        description="UI font family; empty = Microsoft YaHei UI with the built-in fallbacks",
    )
    ui_radius: str = Field(
        default="default",
        description="Corner radius preset: compact / default / round",
    )
    auto_update_mode: str = Field(
        default="download_notify",
        description="Automatic update behaviour: download_install / download_notify / notify / off",
    )
    curseforge_api_key: str = Field(
        default="",
        description="User-provided CurseForge API Key; empty = use the built-in default key",
    )
    wizard_done: bool = Field(
        default=False,
        description="Whether the first-use wizard is complete (language / game directory / memory)",
    )
    offline_unlocked: bool = Field(
        default=False,
        description="Whether a Microsoft premium login has been verified once (gate for offline mode)",
    )
    http_proxy: str = Field(
        default="",
        description="HTTP proxy address (e.g. http://127.0.0.1:7890); empty = auto-read proxy from environment variables",
    )

    @field_validator("accessibility_preference", mode="before")
    @classmethod
    def _normalize_accessibility_preference(cls, value: object) -> str:
        """Keep old or hand-edited configs loadable: anything unrecognised means ``auto``."""
        normalized = value.strip().lower() if isinstance(value, str) else value
        if normalized in ACCESSIBILITY_PREFERENCE_CHOICES:
            return str(normalized)
        logging.getLogger(__name__).warning(
            "Invalid accessibility_preference %r; falling back to %r",
            value,
            ACCESSIBILITY_PREFERENCE_DEFAULT,
        )
        return ACCESSIBILITY_PREFERENCE_DEFAULT

    @field_validator("download_source_meta", "download_source_assets", mode="before")
    @classmethod
    def _normalize_download_source(cls, value: object) -> str:
        """Keep hand-edited configs loadable: an unknown source preference means ``auto``."""
        normalized = value.strip().lower() if isinstance(value, str) else value
        if normalized in DOWNLOAD_SOURCE_CHOICES:
            return str(normalized)
        logging.getLogger(__name__).warning(
            "Invalid download source %r; falling back to %r", value, DOWNLOAD_SOURCE_DEFAULT
        )
        return DOWNLOAD_SOURCE_DEFAULT

    @model_validator(mode="before")
    @classmethod
    def _migrate_game_folders(cls, data: object) -> object:
        """Turn a pre-folder config into a one-item list, and keep ``game_dir`` equal to the current one.

        This runs for every construction, i.e. both for the stored JSON and for the keyword form the
        tests, the tools and the CLI use (``LauncherConfig(game_dir=...)``), so the two representations
        cannot drift apart. The migration never resolves or rewrites a path: the stored string is kept
        exactly as it was, so a config that names a relative/demo directory keeps naming it.
        """
        if not isinstance(data, dict):
            return data
        raw = dict(data)
        folders = _normalized_folders(raw.get("game_folders"))
        legacy = raw.get("game_dir")
        legacy_text = str(legacy).strip() if legacy is not None else ""
        legacy_key = _folder_key(legacy_text) if legacy_text else ""
        if not folders and legacy_text:
            # The historical config: exactly one folder, and it is the current one.
            folders = [
                GameFolder(
                    path=Path(legacy_text),
                    name=folder_display_name(legacy_text),
                    is_default=True,
                )
            ]
        elif legacy_text and all(_folder_key(folder.path) != legacy_key for folder in folders):
            # Only `game_dir` was changed (the wizard and older code write it directly): that path is
            # still the folder the user is using, so it joins the list instead of being overwritten.
            folders.append(
                GameFolder(
                    path=Path(legacy_text),
                    name=folder_display_name(legacy_text),
                    is_default=True,
                )
            )
        active = _active_of(folders, legacy_text)
        for folder in folders:
            if not folder.name.strip():
                folder.name = folder_display_name(folder.path)
            folder.is_default = folder is active
        raw["game_folders"] = folders
        if active is not None:
            raw["game_dir"] = active.path
        return raw

    # ---------- game folders ----------
    #
    # Every reader in the launcher (instances, mods, resources, versions, the CLI) keeps using
    # `cfg.game_dir`, which is why the list never replaces it: `game_dir` is the *current* folder.
    # These helpers are the only code that changes the two fields together.

    def folder_for(self, path: Path | str) -> GameFolder | None:
        """The registered folder at ``path`` (same comparison rule the OS uses), or None."""
        key = _folder_key(path)
        if not key:
            return None
        for folder in self.game_folders:
            if _folder_key(folder.path) == key:
                return folder
        return None

    def active_folder(self) -> GameFolder | None:
        """The folder every page reads right now: the one ``game_dir`` names, else the flagged one."""
        if self.game_dir:
            found = self.folder_for(self.game_dir)
            if found is not None:
                return found
        for folder in self.game_folders:
            if folder.is_default:
                return folder
        return self.game_folders[0] if self.game_folders else None

    def effective_game_folders(self) -> list[GameFolder]:
        """The folder rows the UI shows, including the implicit default folder of a fresh config.

        A config that has never stored a folder (no ``game_dir``, no list) still has one folder in
        effect - the environment/platform default - and the settings page has to show it, otherwise
        the list would start empty and the first added folder would silently become the current one.
        The returned copies carry exactly one ``is_default``; the config itself is not modified.
        """
        folders = _normalized_folders(self.game_folders)
        if not folders:
            target = Path(self.game_dir) if self.game_dir else paths.default_game_dir()
            return [
                GameFolder(
                    path=target,
                    name=folder_display_name(target),
                    is_default=True,
                )
            ]
        active = _active_of(folders, self.game_dir)
        for folder in folders:
            if not folder.name.strip():
                folder.name = folder_display_name(folder.path)
            folder.is_default = folder is active
        return folders

    def add_game_folder(self, path: Path | str, name: str = "") -> tuple[GameFolder, bool]:
        """Register a folder; return ``(folder, created)``. The same path is never added twice."""
        text = str(path or "").strip()
        if not text:
            raise ValueError("a game folder needs a path")
        folders = self.effective_game_folders()
        for folder in folders:
            if _folder_key(folder.path) == _folder_key(text):
                return folder, False
        target = Path(text)
        folder = GameFolder(path=target, name=(name or "").strip() or folder_display_name(target))
        folders.append(folder)
        # Adding never switches the current folder: the user picks it with 「设为当前」.
        active = _active_of(folders, self.game_dir)
        for entry in folders:
            entry.is_default = entry is active
        self.game_folders = folders
        if active is not None:
            self.game_dir = active.path
        return folder, True

    def remove_game_folder(self, path: Path | str) -> bool:
        """Drop one folder; False when it is unknown or it is the last one (one folder always stays).

        Removing the current folder promotes the first remaining one, which is the only case where a
        removal changes what the pages read.
        """
        folders = self.effective_game_folders()
        target = None
        key = _folder_key(path)
        for folder in folders:
            if _folder_key(folder.path) == key:
                target = folder
                break
        if target is None or len(folders) <= 1:
            return False
        was_active = target.is_default
        folders.remove(target)
        if was_active:
            folders[0].is_default = True
        self.game_folders = folders
        if was_active:
            self.game_dir = folders[0].path
        return True

    def set_active_game_folder(self, path: Path | str) -> bool:
        """Make an existing folder the current one (False when the path is not registered)."""
        folders = self.effective_game_folders()
        key = _folder_key(path)
        target = next(
            (folder for folder in folders if _folder_key(folder.path) == key), None
        )
        if target is None:
            return False
        for folder in folders:
            folder.is_default = folder is target
        self.game_folders = folders
        self.game_dir = target.path
        return True

    def rename_game_folder(self, path: Path | str, name: str) -> bool:
        """Rename the display label of a registered folder (empty falls back to the directory name)."""
        folders = self.effective_game_folders()
        key = _folder_key(path)
        target = next(
            (folder for folder in folders if _folder_key(folder.path) == key), None
        )
        if target is None:
            return False
        target.name = (name or "").strip() or folder_display_name(target.path)
        self.game_folders = folders
        return True

    def sync_game_folders(self) -> None:
        """Make ``game_dir`` and the list agree before the config is written.

        Kept deliberately conservative for a config that has never named a folder: with neither a
        ``game_dir`` nor a list, nothing is written, so the environment/platform default rule keeps
        applying exactly as it always did.
        """
        if not self.game_dir and not self.game_folders:
            return
        folders = self.effective_game_folders()
        if not folders:
            return
        active = _active_of(folders, self.game_dir)
        if active is None:
            return
        for folder in folders:
            folder.is_default = folder is active
        self.game_folders = folders
        self.game_dir = active.path


def same_game_folder(left: Path | str | None, right: Path | str | None) -> bool:
    """Whether two paths name the same game folder (the OS's comparison rule, not string equality)."""
    return _folder_key(left) == _folder_key(right)


def active_game_dir(config: LauncherConfig | None = None) -> Path:
    """The folder every instance/mod/resource read resolves against.

    ``game_dir`` is the current folder's path, so this is the same directory those readers already
    use; it exists so new code has one name for the concept and can fall back to the platform default
    exactly like they do.
    """
    cfg = config if config is not None else load()[0]
    folder = cfg.active_folder()
    if folder is not None:
        return Path(folder.path)
    if cfg.game_dir:
        return Path(cfg.game_dir)
    return paths.default_game_dir()


def default_config_path() -> Path:
    return paths.launcher_dir() / CONFIG_FILENAME


# (mtime, size) -> config cache: re-parses only when the file actually changed
_config_cache: dict[Path, tuple[float, int, LauncherConfig]] = {}


def load(path: Path | None = None) -> tuple[LauncherConfig, Path]:
    """Load the config; return the default config when the file doesn't exist.

    The parsed result is cached by (mtime, size) so repeated reads are cheap;
    save() refreshes the cache entry directly.
    """
    config_path = path or default_config_path()
    if not config_path.exists():
        return LauncherConfig(), config_path
    try:
        stat = config_path.stat()
        stamp = (stat.st_mtime, stat.st_size)
    except OSError:
        stamp = (-1.0, -1)
    cached = _config_cache.get(config_path)
    if cached is not None and cached[0] == stamp[0] and cached[1] == stamp[1]:
        return cached[2], config_path
    # utf-8-sig tolerates a UTF-8 BOM (some editors and PowerShell add one)
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if "after_launch_behavior" not in raw and "auto_close_on_launch" in raw:
        # Migrate the legacy hide-and-exit flag to the three-state behavior
        raw["after_launch_behavior"] = "exit" if raw["auto_close_on_launch"] else "keep"
    unknown = set(raw) - set(LauncherConfig.model_fields)
    if unknown:
        logging.getLogger(__name__).warning("忽略配置中的未知字段: %s", sorted(unknown))
    cfg = LauncherConfig(**raw)
    _config_cache[config_path] = (stamp[0], stamp[1], cfg)
    return cfg, config_path


def save(config: LauncherConfig, path: Path | None = None) -> Path:
    """Write the config back (temp file + atomic replace)."""
    config_path = path or default_config_path()
    # A caller may have changed `game_dir` directly (the wizard still does): before the file is
    # written, that path joins the folder list and the list's current marker follows it.
    config.sync_game_folders()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config_path)
    try:
        stat = config_path.stat()
        _config_cache[config_path] = (stat.st_mtime, stat.st_size, config)
    except OSError:
        _config_cache.pop(config_path, None)
    return config_path


def system_game_language() -> str:
    """Map the system language to a game language code; return empty (follow game) when unsupported."""
    try:
        import locale

        system = (locale.getdefaultlocale()[0] or "").lower().replace("-", "_")
    except Exception:  # noqa: BLE001 - follow the game when system-language detection fails
        return ""
    for code, _name in GAME_LANGUAGES:
        if code and (code.lower() == system or system.startswith(code.split("_")[0] + "_")):
            return code
    if system.startswith("zh"):
        return "zh_cn"
    if system.startswith("en"):
        return "en_us"
    return ""


def system_locale_is_zh_cn() -> bool:
    """Whether the system locale is Simplified Chinese in mainland China (zh_CN).

    The non-premium offline gate wants both the language and the region: a Traditional-Chinese
    system (zh_TW / zh_HK) and Simplified Chinese outside the mainland (zh_SG) do not qualify.
    """
    try:
        import locale

        lang = (locale.getdefaultlocale()[0] or "").lower().replace("-", "_").split(".")[0]
    except Exception:  # noqa: BLE001 - treat detection failure as a non-matching locale
        return False
    return lang == "zh_cn"


def offline_mode_allowed() -> bool:
    """Whether offline mode is unlocked.

    Conditions (logical OR): a Microsoft premium login was verified once; or the launcher
    language is Simplified Chinese **and** the system locale is Simplified Chinese in mainland
    China (non-premium offline mode is only available in that environment).
    """
    cfg, _ = load()
    if cfg.offline_unlocked:
        return True
    return cfg.ui_language == "zh_cn" and system_locale_is_zh_cn()


def unlock_offline_mode() -> bool:
    """Call after a successful Microsoft premium login; return whether a write occurred."""
    cfg, path = load()
    if cfg.offline_unlocked:
        return False
    cfg.offline_unlocked = True
    save(cfg, path)
    return True


def initialize_language(config: LauncherConfig, path: Path | None = None) -> bool:
    """Auto-set the game language on first launch (never changed automatically
afterward). Return whether a write occurred.

    Rules: prefer syncing the launcher UI language (ui_language); when unset or
    unsupported, sync the system language; when the system language is not in
    the supported list, keep "follow in-game setting".
    """
    if config.language_initialized:
        return False
    if config.ui_language and any(
        code == config.ui_language for code, _name in GAME_LANGUAGES
    ):
        game_lang = config.ui_language
    else:
        game_lang = system_game_language()
    config.game_language = game_lang
    config.language_initialized = True
    save(config, path)
    return True
