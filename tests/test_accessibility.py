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

"""Phase 1 accessibility core: config default, version tiers, mod vetoes, decision, state."""

from __future__ import annotations

import json
import logging
from itertools import product

import pytest

from launcher import accessibility as a11y
from launcher import config as config_mod

T = a11y.VersionTier
P = a11y.AccessibilityPreference
Q = a11y.TriggerSource
D = a11y.Decision
S = a11y.AccessibilityState


# --- config ---------------------------------------------------------------------------------


def test_config_accessibility_defaults_to_auto():
    assert config_mod.LauncherConfig().accessibility_preference == "auto"


def test_old_config_without_the_key_defaults_to_auto(ws_tmp):
    path = ws_tmp / "config.json"
    path.write_text(json.dumps({"memory_gb": 6}), encoding="utf-8")
    loaded, _ = config_mod.load(path)
    assert loaded.accessibility_preference == "auto"
    assert loaded.memory_gb == 6.0


@pytest.mark.parametrize("value", ["auto", "always_on", "never", "NEVER"])
def test_valid_accessibility_preferences_are_kept(value):
    expected = value.lower()
    assert config_mod.LauncherConfig(accessibility_preference=value).accessibility_preference == expected


@pytest.mark.parametrize("value", ["", "sometimes", None, 1])
def test_invalid_accessibility_preference_falls_back_to_auto_with_a_log(value, caplog):
    with caplog.at_level(logging.WARNING, logger=config_mod.__name__):
        loaded = config_mod.LauncherConfig(accessibility_preference=value)
    assert loaded.accessibility_preference == "auto"
    assert "accessibility_preference" in caplog.text


def test_accessibility_preference_roundtrips_through_save(ws_tmp):
    path = ws_tmp / "config.json"
    saved = config_mod.save(config_mod.LauncherConfig(accessibility_preference="never"), path)
    loaded, _ = config_mod.load(saved)
    assert loaded.accessibility_preference == "never"


def test_invalid_accessibility_preference_does_not_break_a_hand_edited_config(ws_tmp, caplog):
    path = ws_tmp / "config.json"
    path.write_text(json.dumps({"accessibility_preference": "ask-me"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        loaded, _ = config_mod.load(path)
    assert loaded.accessibility_preference == "auto"
    assert "ask-me" in caplog.text


# --- version tiers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # The two boundaries, with one step on each side.
        ("17w12a", T.NO_ACCESSIBILITY),
        ("17w13a", T.NARRATOR_ONLY),
        ("23w02a", T.NARRATOR_ONLY),
        ("23w03a", T.ONBOARD_SCREEN),
        ("23w03b", T.ONBOARD_SCREEN),
        # Releases on both sides of the same changes.
        ("1.11.2", T.NO_ACCESSIBILITY),
        ("1.12", T.NARRATOR_ONLY),
        ("1.19.3", T.NARRATOR_ONLY),
        ("1.19.4", T.ONBOARD_SCREEN),
        ("1.20.4", T.ONBOARD_SCREEN),
        # Loader suffixes, loader profile ids, and the new-style short version.
        ("1.20.1-forge", T.ONBOARD_SCREEN),
        ("1.20.1-fabric", T.ONBOARD_SCREEN),
        ("1.20.1-neoforge", T.ONBOARD_SCREEN),
        ("1.20.1-forge-47.4.22", T.ONBOARD_SCREEN),
        ("fabric-loader-0.19.3-1.21.11", T.ONBOARD_SCREEN),
        ("neoforge-21.1.5", T.ONBOARD_SCREEN),
        ("26.1", T.ONBOARD_SCREEN),
        # Numeric, not lexical: as a string "1.19.10" sorts before "1.19.4".
        ("1.19.10", T.ONBOARD_SCREEN),
        # Unknown / legacy ids must not make us touch options.txt.
        ("b1.7.3", T.NO_ACCESSIBILITY),
        ("3D Shareware v1.34", T.NO_ACCESSIBILITY),
        ("not-a-version", T.NO_ACCESSIBILITY),
        ("", T.NO_ACCESSIBILITY),
    ],
)
def test_version_tier_boundaries_and_shapes(version, expected):
    assert a11y.version_tier(version) is expected


def test_unknown_version_is_conservative_no_accessibility():
    """Documented choice: unknown version -> do not write (no false onboarding)."""
    assert a11y.version_tier("completely-new-scheme") is T.NO_ACCESSIBILITY


# --- mods -----------------------------------------------------------------------------------


def test_classify_mod_ids_is_case_insensitive_and_deterministic():
    result = a11y.classify_mod_ids(
        {
            "DisableAccessibilityScreen",
            "  YEETUSNARRATUS ",
            "YOSBR",
            "ConfiguredDefaults",
            "some-unrelated-mod",
        }
    )
    assert result == {
        "disable": ["disableaccessibilityscreen", "yeetusnarratus"],
        "override": ["configureddefaults", "yosbr"],
    }


def test_classify_mod_ids_empty():
    assert a11y.classify_mod_ids([]) == {"disable": [], "override": []}


def test_classify_installed_mods_reuses_the_scanner_and_ignores_disabled(monkeypatch, ws_tmp):
    from types import SimpleNamespace

    def _fake_scan(_mods_dir):
        return [
            SimpleNamespace(mod_id="YOSBR", enabled=True),
            SimpleNamespace(mod_id="DisableAccessibilityScreen", enabled=False),
        ]

    monkeypatch.setattr("launcher.mods.local.scan_mods", _fake_scan)
    assert a11y.classify_installed_mods(ws_tmp) == {"disable": [], "override": ["yosbr"]}


# --- decision truth table -------------------------------------------------------------------
# Every row is one of 3 preferences x 3 tiers x 3 triggers x 3 mod situations x 2 states.
# "mods" has no/disable/override rows; the table builder below spells out each rule once.

TIERS = (T.NO_ACCESSIBILITY, T.NARRATOR_ONLY, T.ONBOARD_SCREEN)
TRIGGERS = (Q.KEYBOARD, Q.MOUSE, Q.UNKNOWN)
STATES = (("untreated", None), ("handled", S.APPLIED))
MOD_CASES = {
    "none": {},
    "disable": {"disable": ["DisableAccessibilityScreen"]},
    "override": {"override": ["YOSBR"]},
}

DECISION_CASES: list[tuple] = []

# never: no prompt, no write, for every other input.
for tier, trigger, (state_name, _state), mod_kind in product(TIERS, TRIGGERS, STATES, MOD_CASES):
    DECISION_CASES.append(
        ("never", tier, trigger, mod_kind, state_name, D.LAUNCH_QUIETLY, ("never",))
    )

# always_on: ignore the trigger and the recorded state; only a writable tier without a
# disable/takeover mod (and without an existing option) writes.
for tier, trigger, (state_name, _state), mod_kind in product(TIERS, TRIGGERS, STATES, MOD_CASES):
    if tier is T.NO_ACCESSIBILITY:
        expected, keywords = D.LAUNCH_QUIETLY, ("no native",)
    elif mod_kind == "disable":
        expected, keywords = D.LAUNCH_QUIETLY, ("disabled",)
    elif tier is T.ONBOARD_SCREEN:
        expected, keywords = D.WRITE_ONBOARD, ("onboardAccessibility:true",)
    else:
        expected, keywords = D.WRITE_NARRATOR, ("narrator:1",)
    DECISION_CASES.append(
        ("always_on", tier, trigger, mod_kind, state_name, expected, keywords)
    )

# auto: state -> version -> disable -> override -> non-keyboard trigger -> keyboard asks.
for tier, trigger, (state_name, state), mod_kind in product(TIERS, TRIGGERS, STATES, MOD_CASES):
    if state is not None:
        expected, keywords = D.LAUNCH_QUIETLY, ("already handled",)
    elif tier is T.NO_ACCESSIBILITY:
        expected, keywords = D.LAUNCH_QUIETLY, ("no native",)
    elif mod_kind == "disable":
        expected, keywords = D.LAUNCH_QUIETLY, ("disabled",)
    elif mod_kind == "override":
        expected, keywords = D.LAUNCH_QUIETLY, ("overriding",)
    elif trigger is not Q.KEYBOARD:
        expected = D.LAUNCH_QUIETLY
        keywords = (
            "not keyboard",
            "onboarding option is not written"
            if tier is T.ONBOARD_SCREEN
            else "narrator option is not written",
        )
    elif tier is T.ONBOARD_SCREEN:
        expected, keywords = D.ASK_ONBOARD, ("onboarding question",)
    else:
        expected, keywords = D.ASK_NARRATOR, ("narrator question",)
    DECISION_CASES.append(("auto", tier, trigger, mod_kind, state_name, expected, keywords))


def test_decision_table_is_the_complete_162_combination_matrix():
    assert len(DECISION_CASES) == 3 * 3 * 3 * 3 * 2
    keys = {(pref, tier, trigger, mod_kind, state_name) for pref, tier, trigger, mod_kind, state_name, _, _ in DECISION_CASES}
    assert len(keys) == len(DECISION_CASES)


@pytest.mark.parametrize(
    ("preference", "tier", "trigger", "mod_kind", "state_name", "expected", "keywords"),
    DECISION_CASES,
)
def test_decision_table(preference, tier, trigger, mod_kind, state_name, expected, keywords):
    state = dict(STATES)[state_name]
    result = a11y.decide(
        preference=preference,
        tier=tier,
        trigger=trigger,
        mods=MOD_CASES[mod_kind],
        state=state,
    )
    assert result.decision is expected
    for keyword in keywords:
        assert keyword in result.reason, result.reason


def test_always_on_does_not_overwrite_an_existing_option():
    for tier in (T.NARRATOR_ONLY, T.ONBOARD_SCREEN):
        result = a11y.decide(
            preference="always_on",
            tier=tier,
            trigger="keyboard",
            option_present=True,
        )
        assert result.decision is D.LAUNCH_QUIETLY
        assert "already present" in result.reason


def test_always_on_with_only_an_override_mod_still_asks_for_no_permission_and_writes():
    """Override mods are an auto veto; always_on means the user already said yes."""
    result = a11y.decide(
        preference="always_on",
        tier=T.ONBOARD_SCREEN,
        trigger="mouse",
        mods={"override": ["yosbr"]},
    )
    assert result.decision is D.WRITE_ONBOARD


def test_always_on_no_accessibility_stays_quiet_even_when_option_is_present():
    result = a11y.decide(
        preference="always_on",
        tier=T.NO_ACCESSIBILITY,
        trigger="keyboard",
        option_present=True,
    )
    assert result.decision is D.LAUNCH_QUIETLY
    assert "no native" in result.reason


def test_invalid_decision_inputs_fall_back_conservatively():
    result = a11y.decide(preference="bogus", tier="bogus", trigger="bogus")
    assert result.decision is D.LAUNCH_QUIETLY
    assert "no native" in result.reason


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (T.NO_ACCESSIBILITY, None),
        (T.NARRATOR_ONLY, (a11y.NARRATOR_OPTION_KEY, a11y.NARRATOR_OPTION_VALUE)),
        (T.ONBOARD_SCREEN, (a11y.ONBOARD_OPTION_KEY, a11y.ONBOARD_OPTION_VALUE)),
    ],
)
def test_option_for_tier(tier, expected):
    assert a11y.option_for_tier(tier) == expected


# --- state storage --------------------------------------------------------------------------


def test_default_state_path_is_in_launcher_dir(ws_tmp, monkeypatch):
    data_dir = ws_tmp / "launcher-data"
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data_dir))
    assert a11y.default_state_path() == data_dir / a11y.ACCESSIBILITY_STATE_FILENAME
    assert ".minecraft" not in str(a11y.default_state_path())


def test_corrupt_state_json_means_untreated(ws_tmp):
    path = ws_tmp / "accessibility.json"
    path.write_text("{not json", encoding="utf-8")
    assert a11y.load_states(path) == {}
    assert a11y.get_state("inst", path) is None


def test_state_json_with_wrong_shape_means_untreated(ws_tmp):
    path = ws_tmp / "accessibility.json"
    path.write_text(json.dumps(["answered_on"]), encoding="utf-8")
    assert a11y.load_states(path) == {}


def test_unknown_state_values_are_ignored_but_known_ones_survive(ws_tmp):
    path = ws_tmp / "accessibility.json"
    path.write_text(
        json.dumps({"good": "applied", "bad": "something_else"}),
        encoding="utf-8",
    )
    assert a11y.load_states(path) == {"good": S.APPLIED}


@pytest.mark.parametrize("state", list(S))
def test_state_roundtrip_for_every_enum_value(ws_tmp, state):
    path = ws_tmp / "accessibility.json"
    assert a11y.save_state("inst-a", state, path) is True
    assert a11y.get_state("inst-a", path) is state

    # Saving a second instance keeps the first one.
    assert a11y.save_state("inst-b", S.DISMISSED, path) is True
    assert a11y.get_state("inst-a", path) is state
    assert a11y.get_state("inst-b", path) is S.DISMISSED


def test_state_write_failure_is_logged_and_never_raised(ws_tmp, caplog):
    path = ws_tmp / "state.json"
    path.mkdir()  # a directory cannot be replaced by os.replace
    with caplog.at_level(logging.WARNING, logger=a11y.__name__):
        assert a11y.save_state("inst", S.APPLIED, path) is False
    assert "could not save accessibility state" in caplog.text


def test_unreadable_state_is_untreated(ws_tmp):
    path = ws_tmp / "state-dir"
    path.mkdir()
    assert a11y.load_states(path) == {}
    assert a11y.get_state("inst", path) is None


def test_state_can_be_passed_as_its_string_value(ws_tmp):
    path = ws_tmp / "accessibility.json"
    assert a11y.save_state("inst", "answered_on", path) is True
    assert a11y.get_state("inst", path) is S.ANSWERED_ON
