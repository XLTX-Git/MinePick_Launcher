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

"""The one accessibility step every GUI launch passes through before its worker starts.

The three launch entries (launch page, instance detail and the instances page's legacy delegate) only
supply the trigger source; the decision-to-action mapping, the options write, the state write and the
question all live here. Keeping it in one function is what guarantees the same preferences and the same
state are read for every entry, and that no entry can forget the ask/write distinction.

Order of operations:
1. read the preference, the instance's recorded state, the version tier and whether options.txt already
   has the target key;
2. run the pure decision once *without* mods - this is the cheap first answer;
3. scan the mods directory only when that answer is ASK_* or WRITE_*, because only those decisions can
   still be turned into LAUNCH_QUIETLY by a vetoing mod. A handled instance, a keyboard-less launch and
   a version without native accessibility therefore never pay for a mod scan;
4. WRITE_* -> ensure_option + state, then continue the launch;
5. ASK_* -> the consent dialog (Phase 4), then state, then continue the launch;
6. LAUNCH_QUIETLY -> continue immediately.

Every probe, write or state failure is logged and still continues the launch. Accessibility is an
optional courtesy; it must never be the reason the game does not start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from gui.dialogs.accessibility_dialog import (
    CHOICE_NO,
    CHOICE_YES,
    AccessibilityDialog,
)
from gui.widgets import disable_keeping_focus
from launcher import config
from launcher.accessibility import (
    AccessibilityState,
    Decision,
    DecisionResult,
    TriggerSource,
    VersionTier,
    classify_installed_mods,
    decide,
    default_state_path,
    get_state,
    option_for_tier,
    save_state,
    version_tier,
)
from launcher.launch.options import OptionWriteResult, ensure_option, option_present

_LOGGER = logging.getLogger(__name__)

_ENTER_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)


@dataclass(frozen=True)
class _Request:
    """Everything the action step needs from the probe step."""

    instance_id: str
    decision: Decision
    reason: str
    tier: VersionTier
    key: str | None
    value: str | None
    options_path: Path
    state_path: Path


def trigger_source_for_click(button: QPushButton) -> TriggerSource:
    """Classify a launch-button click from the focus tracker's public read-only answers.

    * no keyboard input since the last mouse event -> ``mouse``;
    * Space, or any other key that a focused button treats as its activation -> ``keyboard``;
    * Enter only when ``button`` is the default button -> ``keyboard``; Enter on a non-default button
      is not a keyboard launch at all, because this project measured that only the default button is
      activated by Enter. Anything unattributable is ``unknown``.

    The function is called from click handlers only. A programmatic launch (tray, command line, drag,
    auto-restart) never goes through it and passes ``unknown`` explicitly.
    """
    tracker = getattr(QApplication.instance(), "_focus_tracker", None)
    if tracker is None:
        return TriggerSource.MOUSE
    try:
        keyboard = bool(tracker.last_input_was_keyboard())
        last_key = tracker.last_key()
    except Exception:  # noqa: BLE001 - a launch must not depend on the tracker being healthy
        return TriggerSource.UNKNOWN
    if not keyboard:
        return TriggerSource.MOUSE
    if last_key in _ENTER_KEYS and not _is_default_button(button):
        return TriggerSource.UNKNOWN
    return TriggerSource.KEYBOARD


def _is_default_button(button: object) -> bool:
    try:
        return bool(button.isDefault())
    except (AttributeError, RuntimeError):
        return False


def prepare_launch_accessibility(
    *,
    parent: QWidget | None,
    instance_id: str,
    game_dir: Path,
    on_continue: Callable[[], None],
    trigger_source: TriggerSource | str = TriggerSource.UNKNOWN,
    launch_dir: Path | None = None,
    launch_button: QPushButton | None = None,
    state_path: Path | None = None,
) -> None:
    """Run the single pre-launch accessibility step, then call ``on_continue``.

    This is the one and only place the GUI combines the preference, the state, the tier, the mods and
    options.txt. The caller does the launch work in ``on_continue``; this function may call it
    immediately (quiet/write) or later, from the dialog's ``finished`` handler (ask).

    ``launch_dir`` is the effective game working directory (the isolated instance folder or the shared
    game directory); options.txt and mods live there. ``state_path`` exists for tests and for an
    explicit data directory; production leaves it as the launcher-owned default.

    A probe, write or dialog-construction failure is logged and the launch continues as if the
    decision had been LAUNCH_QUIETLY. Only ``on_continue`` itself is allowed to raise, because that is
    the caller's launch code.
    """
    target_dir = Path(launch_dir) if launch_dir is not None else Path(game_dir)
    options_path = target_dir / "options.txt"
    mods_dir = target_dir / "mods"
    state_target = Path(state_path) if state_path is not None else default_state_path()

    try:
        request = _probe(
            instance_id=instance_id,
            trigger_source=trigger_source,
            mods_dir=mods_dir,
            options_path=options_path,
            state_path=state_target,
        )
    except Exception:  # the launch is not allowed to depend on this step
        _LOGGER.exception(
            "accessibility pre-launch probe failed for %s; launching quietly without asking or writing",
            instance_id,
        )
        on_continue()
        return

    if request.decision is Decision.LAUNCH_QUIETLY:
        _LOGGER.info("accessibility: launch quietly: %s", request.reason)
        on_continue()
        return

    if request.decision in (Decision.WRITE_ONBOARD, Decision.WRITE_NARRATOR):
        try:
            _write_option(request, answered=False)
        except Exception:  # a write/state failure must not block the launch
            _LOGGER.exception(
                "accessibility: %s failed unexpectedly; launching anyway", request.decision.value
            )
        on_continue()
        return

    if request.decision in (Decision.ASK_ONBOARD, Decision.ASK_NARRATOR):
        try:
            _ask(request, parent=parent, launch_button=launch_button, on_continue=on_continue)
        except Exception:  # an unbuildable dialog must not block the launch
            _LOGGER.exception(
                "accessibility: could not open the %s dialog; launching quietly",
                request.decision.value,
            )
            on_continue()
        return

    # A future decision enum member: the safe, quiet action is to launch.
    _LOGGER.warning("accessibility: unknown decision %r; launching quietly", request.decision)
    on_continue()


def _probe(
    *,
    instance_id: str,
    trigger_source: TriggerSource | str,
    mods_dir: Path,
    options_path: Path,
    state_path: Path,
) -> _Request:
    """Read the cheap facts, decide, and scan mods only when the decision can still change."""
    cfg, _ = config.load()
    preference = cfg.accessibility_preference
    state = get_state(instance_id, state_path)
    tier = version_tier(instance_id)
    option = option_for_tier(tier)
    key, value = option if option is not None else (None, None)
    present = option_present(options_path, key) if key is not None else False

    first = decide(
        preference=preference,
        tier=tier,
        trigger=trigger_source,
        mods=None,
        state=state,
        option_present=present,
    )
    if first.decision is Decision.LAUNCH_QUIETLY:
        _LOGGER.info(
            "accessibility: no mod scan needed for %s (%s); %s",
            instance_id,
            tier.value,
            first.reason,
        )
        return _request(instance_id, first, tier, key, value, options_path, state_path)

    mods = classify_installed_mods(mods_dir)
    final = decide(
        preference=preference,
        tier=tier,
        trigger=trigger_source,
        mods=mods,
        state=state,
        option_present=present,
    )
    _LOGGER.info(
        "accessibility: %s for %s (tier=%s, state=%s, trigger=%s, mods=%s, option_present=%s): %s",
        final.decision.value,
        instance_id,
        tier.value,
        state.value if state is not None else "untreated",
        trigger_source.value
        if isinstance(trigger_source, TriggerSource)
        else str(trigger_source),
        mods,
        present,
        final.reason,
    )
    return _request(instance_id, final, tier, key, value, options_path, state_path)


def _request(
    instance_id: str,
    result: DecisionResult,
    tier: VersionTier,
    key: str | None,
    value: str | None,
    options_path: Path,
    state_path: Path,
) -> _Request:
    return _Request(
        instance_id=instance_id,
        decision=result.decision,
        reason=result.reason,
        tier=tier,
        key=key,
        value=value,
        options_path=options_path,
        state_path=state_path,
    )


def _write_option(request: _Request, *, answered: bool) -> OptionWriteResult:
    """Write the one option, then record the matching state. Never raises by design."""
    if request.key is None or request.value is None:
        _LOGGER.warning(
            "accessibility: %s has no options key for tier %s; not writing",
            request.decision.value,
            request.tier.value,
        )
        return OptionWriteResult.FAILED
    try:
        outcome = ensure_option(request.options_path, request.key, request.value)
    except Exception:  # ensure_option documents no raise, but a launch depends on it
        _LOGGER.exception(
            "accessibility: writing %s to %s raised; treating it as failed",
            request.key,
            request.options_path,
        )
        outcome = OptionWriteResult.FAILED

    if outcome in (OptionWriteResult.WRITTEN, OptionWriteResult.UNCHANGED):
        state = AccessibilityState.ANSWERED_ON if answered else AccessibilityState.APPLIED
    else:
        state = AccessibilityState.WRITE_FAILED
    _LOGGER.info(
        "accessibility: %s -> %s; state=%s",
        request.key,
        outcome.value,
        state.value,
    )
    if not save_state(request.instance_id, state, request.state_path):
        # save_state already logs; this branch only reports that the launch continues.
        _LOGGER.warning(
            "accessibility: state %s was not recorded for %s", state.value, request.instance_id
        )
    return outcome


def _ask(
    request: _Request,
    *,
    parent: QWidget | None,
    launch_button: QPushButton | None,
    on_continue: Callable[[], None],
) -> None:
    """Open the window-modal, non-blocking question and act on its eventual answer."""
    dialog = AccessibilityDialog(parent, request.tier)
    if launch_button is not None:
        try:
            disable_keeping_focus(launch_button)
        except RuntimeError:  # the page went away between the click and the dialog
            pass

    def finished(_result: int) -> None:
        try:
            parent_closing = dialog.parent_closing
        except RuntimeError:  # the parent took the dialog down with it: do not launch
            return
        try:
            if not parent_closing:
                _apply_answer(dialog.choice, request)
        except Exception:  # an answer/state failure must not block the launch
            _LOGGER.exception("accessibility: applying the dialog answer failed; launching anyway")
        finally:
            try:
                dialog.deleteLater()
            except RuntimeError:
                pass
        if not parent_closing:
            on_continue()

    dialog.finished.connect(finished)
    try:
        dialog.open()  # window-modal and non-blocking; exec() is deliberately forbidden
    except Exception:
        try:
            dialog.deleteLater()
        except RuntimeError:
            pass
        raise


def _apply_answer(choice: str | None, request: _Request) -> None:
    if choice == CHOICE_YES:
        _write_option(request, answered=True)
        return
    if choice == CHOICE_NO:
        state = AccessibilityState.ANSWERED_OFF
    else:
        # The close button (and Esc) is not "yes": nothing is written and the launch still happens.
        _LOGGER.info("accessibility: dialog dismissed; no option written")
        state = AccessibilityState.DISMISSED
    save_state(request.instance_id, state, request.state_path)


__all__ = [
    "prepare_launch_accessibility",
    "trigger_source_for_click",
]
