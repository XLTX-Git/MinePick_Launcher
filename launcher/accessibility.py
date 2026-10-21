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

"""Qt-free accessibility onboarding: version tiers, mod vetoes, the decision, and state.

Minecraft grew its accessibility support in two steps:

* 17w13a added the narrator (no onboarding screen yet);
* 23w03a added the accessibility onboarding screen.

The launcher asks *once* whether the player wants that onboarding, and writes the
matching option only when the player asks for it. This module owns the pure rules and
the small state file; it never opens a window and never touches the game directory.

Version ordering is numeric, never lexical. A release is parsed into
``(major, minor, patch)``; a snapshot is parsed into ``(year, week, series)``. The two
families are compared inside their own timeline, so ``26.1`` is newer than ``1.19.4``
and ``17w13a`` is newer than ``17w12a``. Ids that cannot be parsed fall back to
:attr:`VersionTier.NO_ACCESSIBILITY`: when we do not know the version, we do not
silently change the player's options.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from launcher import paths
from launcher.diagnostics.facts import derive_version_facts

_LOGGER = logging.getLogger(__name__)

# options.txt keys the game reads. "true" and "1" are the values Minecraft itself writes.
ONBOARD_OPTION_KEY = "onboardAccessibility"
ONBOARD_OPTION_VALUE = "true"
NARRATOR_OPTION_KEY = "narrator"
NARRATOR_OPTION_VALUE = "1"

ACCESSIBILITY_STATE_FILENAME = "accessibility.json"

# These mods remove, hide, or take over the accessibility screen / narrator. Writing the
# vanilla option would either do nothing or fight the pack's deliberate choice, so we never
# write while one of them is installed.
DISABLE_OR_TAKEOVER: frozenset[str] = frozenset(
    {
        "disableaccessibilityscreen",
        "annoyingstuffremover",
        "yeetusnarratus",
        "declutterui",
        "rebindnarrator",
    }
)

# These mods rewrite options.txt (or its defaults) on launch. Any value we write first would
# be overwritten, so asking the player is pointless; the pack already owns the answer.
OVERRIDE: frozenset[str] = frozenset(
    {
        "yosbr",
        "yosbs",
        "optionsrewriter",
        "optionsenforcer",
        "onetimeoverrides",
        "meows",
        "youroptionsmustberespected",
        "configureddefaults",
    }
)

# First snapshot with narrator support, and first with the onboarding screen.
_NARRATOR_SNAPSHOT_FROM = (17, 13, 1)
_ONBOARD_SNAPSHOT_FROM = (23, 3, 1)
# The equivalent release boundaries: 1.12 is the first release after 17w13a; 1.19.4 is
# the first release after 23w03a.
_NARRATOR_RELEASE_FROM = (1, 12, 0)
_ONBOARD_RELEASE_FROM = (1, 19, 4)

_SNAPSHOT_RE = re.compile(r"^(?P<year>\d{2})w(?P<week>\d{2})(?P<series>[a-z])$", re.IGNORECASE)
_RELEASE_RE = re.compile(r"^(?P<base>\d+(?:\.\d+){1,2})(?=$|[-+])")


class AccessibilityPreference(str, Enum):
    """The launcher's prompt policy; it never changes the game's own onboarding."""

    AUTO = "auto"
    ALWAYS_ON = "always_on"
    NEVER = "never"


class VersionTier(str, Enum):
    """How much native accessibility the running Minecraft version has."""

    NO_ACCESSIBILITY = "no_accessibility"
    NARRATOR_ONLY = "narrator_only"
    ONBOARD_SCREEN = "onboard_screen"


class TriggerSource(str, Enum):
    """What opened the launch flow: keyboard gets the question, mouse/unknown never do."""

    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    UNKNOWN = "unknown"


class AccessibilityState(str, Enum):
    """One instance's recorded outcome; every stored value counts as handled."""

    ANSWERED_ON = "answered_on"
    ANSWERED_OFF = "answered_off"
    DISMISSED = "dismissed"
    APPLIED = "applied"
    WRITE_FAILED = "write_failed"


class Decision(str, Enum):
    """What the caller should do; no prompt is ever implied by a WRITE decision."""

    LAUNCH_QUIETLY = "launch_quietly"
    ASK_ONBOARD = "ask_onboard"
    ASK_NARRATOR = "ask_narrator"
    WRITE_ONBOARD = "write_onboard"
    WRITE_NARRATOR = "write_narrator"


class DecisionResult(NamedTuple):
    decision: Decision
    reason: str


def _release_key(version_id: str) -> tuple[int, int, int] | None:
    match = _RELEASE_RE.match(version_id)
    if match is None:
        return None
    parts = [int(part) for part in match.group("base").split(".")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _snapshot_key(version_id: str) -> tuple[int, int, int] | None:
    match = _SNAPSHOT_RE.match(version_id)
    if match is None:
        return None
    return (
        int(match.group("year")),
        int(match.group("week")),
        ord(match.group("series").lower()) - ord("a") + 1,
    )


def version_tier(version: str) -> VersionTier:
    """Classify a Minecraft version/profile id without lexicographic comparison.

    Accepts what the repository already exposes: ``EnvironmentFacts.mc_version`` (the
    Minecraft part of a profile id) or a profile id directly. ``1.20.1-forge``,
    ``1.20.1-fabric`` and ``1.20.1-neoforge`` keep the same release; Fabric/Quilt
    ``*-loader-*`` ids and ``neoforge-*`` ids are normalized through the existing
    :func:`launcher.diagnostics.facts.derive_version_facts`. Unparsable ids are
    conservatively treated as having no native accessibility.
    """
    raw = (version or "").strip()
    if not raw:
        return VersionTier.NO_ACCESSIBILITY

    # Reuse the launcher's one profile-id parser; for a plain version id this is a no-op.
    normalized = derive_version_facts(raw)[1] or raw
    snapshot = _snapshot_key(normalized)
    if snapshot is not None:
        if snapshot < _NARRATOR_SNAPSHOT_FROM:
            return VersionTier.NO_ACCESSIBILITY
        if snapshot < _ONBOARD_SNAPSHOT_FROM:
            return VersionTier.NARRATOR_ONLY
        return VersionTier.ONBOARD_SCREEN

    release = _release_key(normalized)
    if release is not None:
        if release < _NARRATOR_RELEASE_FROM:
            return VersionTier.NO_ACCESSIBILITY
        if release < _ONBOARD_RELEASE_FROM:
            return VersionTier.NARRATOR_ONLY
        return VersionTier.ONBOARD_SCREEN

    return VersionTier.NO_ACCESSIBILITY


def classify_mod_ids(mod_ids: Iterable[str]) -> dict[str, list[str]]:
    """Return the matching ids split into ``{"disable": [...], "override": [...]}``.

    Pure: the caller scans the mods directory once with ``launcher.mods.local.scan_mods``
    and passes the resulting ids. Matching is case-insensitive and the output is sorted.
    """
    normalized = {
        str(mod_id).strip().lower()
        for mod_id in mod_ids
        if str(mod_id).strip()
    }
    return {
        "disable": sorted(normalized & DISABLE_OR_TAKEOVER),
        "override": sorted(normalized & OVERRIDE),
    }


def classify_installed_mods(mods_dir: Path) -> dict[str, list[str]]:
    """Scan one effective mods directory (Phase 0's scanner) and classify its enabled mods."""
    from launcher.mods.local import scan_mods

    try:
        mods = scan_mods(Path(mods_dir))
    except OSError as exc:  # unreadable directory: no evidence of a veto
        _LOGGER.warning("could not scan the mods directory %s: %s", mods_dir, exc)
        return {"disable": [], "override": []}
    return classify_mod_ids(mod.mod_id for mod in mods if mod.enabled)


def option_for_tier(tier: VersionTier | str) -> tuple[str, str] | None:
    """The options.txt key/value for a write decision; None when no native support exists."""
    resolved = _enum_or_default(VersionTier, tier, VersionTier.NO_ACCESSIBILITY)
    if resolved is VersionTier.ONBOARD_SCREEN:
        return ONBOARD_OPTION_KEY, ONBOARD_OPTION_VALUE
    if resolved is VersionTier.NARRATOR_ONLY:
        return NARRATOR_OPTION_KEY, NARRATOR_OPTION_VALUE
    return None


def _enum_or_default(enum_type, value, default):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value).strip().lower())
    except (ValueError, TypeError):
        return default


def _mod_ids(mods: Mapping[str, Iterable[str]] | None, name: str) -> tuple[str, ...]:
    if not mods:
        return ()
    values = mods.get(name, ())
    if isinstance(values, str):
        values = (values,)
    return tuple(str(value).lower() for value in values if str(value).strip())


def decide(
    *,
    preference: AccessibilityPreference | str,
    tier: VersionTier | str,
    trigger: TriggerSource | str,
    mods: Mapping[str, Iterable[str]] | None = None,
    state: AccessibilityState | str | None = None,
    option_present: bool = False,
) -> DecisionResult:
    """Return the one action to take plus a stable, log/test-friendly reason.

    ``option_present`` means the target options.txt key already exists. always_on never
    overwrites it; it is deliberately absent from the auto flow, where the "already
    handled" answer is the recorded per-instance state.

    ``state`` is ``None`` for an untreated instance. Any recorded value — including
    ``write_failed`` — is handled, so auto never nags twice.
    """
    pref = _enum_or_default(AccessibilityPreference, preference, AccessibilityPreference.AUTO)
    resolved_tier = _enum_or_default(VersionTier, tier, VersionTier.NO_ACCESSIBILITY)
    resolved_trigger = _enum_or_default(TriggerSource, trigger, TriggerSource.UNKNOWN)
    resolved_state = None if state is None else _enum_or_default(
        AccessibilityState, state, None
    )
    disable = _mod_ids(mods, "disable")
    override = _mod_ids(mods, "override")

    if pref is AccessibilityPreference.NEVER:
        return DecisionResult(
            Decision.LAUNCH_QUIETLY,
            "preference 'never': launch quietly, never prompt and never write",
        )

    if pref is AccessibilityPreference.ALWAYS_ON:
        if resolved_tier is VersionTier.NO_ACCESSIBILITY:
            return DecisionResult(
                Decision.LAUNCH_QUIETLY,
                "always_on: this version has no native accessibility; nothing to write",
            )
        if disable:
            return DecisionResult(
                Decision.LAUNCH_QUIETLY,
                "always_on: accessibility is disabled or taken over by mods "
                f"({', '.join(disable)}); not writing",
            )
        if option_present:
            return DecisionResult(
                Decision.LAUNCH_QUIETLY,
                "always_on: the option is already present; keeping the existing value",
            )
        if resolved_tier is VersionTier.ONBOARD_SCREEN:
            return DecisionResult(
                Decision.WRITE_ONBOARD,
                "always_on: writing onboardAccessibility:true for the onboarding tier",
            )
        return DecisionResult(
            Decision.WRITE_NARRATOR,
            "always_on: writing narrator:1 for the narrator-only tier",
        )

    # auto: everything below is about whether to ask, and only a keyboard launch may ask.
    if resolved_state is not None:
        return DecisionResult(
            Decision.LAUNCH_QUIETLY,
            f"auto: instance already handled (state={resolved_state.value}); launch quietly",
        )
    if resolved_tier is VersionTier.NO_ACCESSIBILITY:
        return DecisionResult(
            Decision.LAUNCH_QUIETLY,
            "auto: this version has no native accessibility; launch quietly without writing or asking",
        )
    if disable:
        return DecisionResult(
            Decision.LAUNCH_QUIETLY,
            "auto: accessibility is disabled or taken over by mods "
            f"({', '.join(disable)}); launch quietly without writing or asking",
        )
    if override:
        return DecisionResult(
            Decision.LAUNCH_QUIETLY,
            "auto: options-overriding mods would undo our write "
            f"({', '.join(override)}); launch quietly without asking",
        )
    if resolved_trigger is not TriggerSource.KEYBOARD:
        if resolved_tier is VersionTier.ONBOARD_SCREEN:
            return DecisionResult(
                Decision.LAUNCH_QUIETLY,
                f"auto: trigger is {resolved_trigger.value!r}, not keyboard; never prompt, "
                "and the onboarding option is not written without a keyboard prompt",
            )
        return DecisionResult(
            Decision.LAUNCH_QUIETLY,
            f"auto: trigger is {resolved_trigger.value!r}, not keyboard; never prompt, "
            "and the narrator option is not written without a keyboard prompt",
        )
    if resolved_tier is VersionTier.ONBOARD_SCREEN:
        return DecisionResult(
            Decision.ASK_ONBOARD,
            "auto: keyboard trigger and onboarding tier; ask the onboarding question",
        )
    return DecisionResult(
        Decision.ASK_NARRATOR,
        "auto: keyboard trigger and narrator-only tier; ask the narrator question",
    )


def default_state_path() -> Path:
    """State belongs to the launcher, never to the shared/read-only game directory."""
    return paths.launcher_dir() / ACCESSIBILITY_STATE_FILENAME


def load_states(path: Path | None = None) -> dict[str, AccessibilityState]:
    """Read the instance-id -> state map; every read failure means "untreated"."""
    target = Path(path) if path is not None else default_state_path()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        _LOGGER.warning("could not read accessibility state %s: %s", target, exc)
        return {}
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        _LOGGER.warning("corrupt accessibility state file ignored: %s", target)
        return {}
    if not isinstance(raw, dict):
        _LOGGER.warning("accessibility state is not an object; ignored: %s", target)
        return {}

    states: dict[str, AccessibilityState] = {}
    for instance_id, value in raw.items():
        if not isinstance(instance_id, str) or not instance_id:
            continue
        try:
            states[instance_id] = AccessibilityState(value)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "ignoring unknown accessibility state %r for instance %s", value, instance_id
            )
    return states


def get_state(instance_id: str, path: Path | None = None) -> AccessibilityState | None:
    """One instance's state, or None when untreated/unreadable."""
    return load_states(path).get(instance_id)


def save_state(
    instance_id: str,
    state: AccessibilityState | str,
    path: Path | None = None,
) -> bool:
    """Record one instance's outcome atomically; a write failure is logged and returns False."""
    if not instance_id:
        _LOGGER.warning("refusing to store accessibility state without an instance id")
        return False
    try:
        resolved = AccessibilityState(state)
    except (ValueError, TypeError):
        _LOGGER.warning("refusing to store unknown accessibility state %r", state)
        return False

    target = Path(path) if path is not None else default_state_path()
    states = load_states(target)
    states[instance_id] = resolved
    payload = {
        instance_id: value.value for instance_id, value in sorted(states.items())
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 - state persistence must never break a launch
        _LOGGER.warning("could not save accessibility state %s: %s", target, exc)
        return False
    return True
