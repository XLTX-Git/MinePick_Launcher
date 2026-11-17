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

"""The cause identifiers of the knowledge base (one class of cause, one id).

The rule file may name a cause by its short id (``bad_jar``) or by the i18n key
it resolves to (``diagnosis.crash.bad_jar.cause``). The table is the single
place that keeps those two in sync; the report/UI always receives the key.
"""

from __future__ import annotations

CAUSES: dict[str, str] = {
    "crash_bad_jar": "diagnosis.crash.bad_jar.cause",
    "crash_content_mod": "diagnosis.crash.content_mod.cause",
    "crash_duplicate_mods": "diagnosis.crash.duplicate_mods.cause",
    "crash_fabric_loader": "diagnosis.crash.fabric_loader.cause",
    "crash_forge_incomplete": "diagnosis.crash.forge_incomplete.cause",
    "crash_forge_loader": "diagnosis.crash.forge_loader.cause",
    "crash_forge_profiles": "diagnosis.crash.forge_profiles.cause",
    "crash_gpu_amd": "diagnosis.crash.gpu_amd.cause",
    "crash_gpu_driver": "diagnosis.crash.gpu_driver.cause",
    "crash_gpu_intel": "diagnosis.crash.gpu_intel.cause",
    "crash_gpu_nvidia": "diagnosis.crash.gpu_nvidia.cause",
    "crash_heap_32bit": "diagnosis.crash.heap_32bit.cause",
    "crash_illegal_name": "diagnosis.crash.illegal_name.cause",
    "crash_incompatible_mods": "diagnosis.crash.incompatible_mods.cause",
    "crash_java_required": "diagnosis.crash.java_required.cause",
    "crash_jdk_not_jre": "diagnosis.crash.jdk_not_jre.cause",
    "crash_low_memory_32bit": "diagnosis.crash.low_memory_32bit.cause",
    "crash_mc_version": "diagnosis.crash.mc_version.cause",
    "crash_missing_dependency": "diagnosis.crash.missing_dependency.cause",
    "crash_mixin_failure": "diagnosis.crash.mixin_failure.cause",
    "crash_mod_id_conflict": "diagnosis.crash.mod_id_conflict.cause",
    "crash_mod_init": "diagnosis.crash.mod_init.cause",
    "crash_native_memory": "diagnosis.crash.native_memory.cause",
    "crash_null_pointer": "diagnosis.crash.null_pointer.cause",
    "crash_oom": "diagnosis.crash.oom.cause",
    "crash_opengl": "diagnosis.crash.opengl.cause",
    "crash_resource_pack": "diagnosis.crash.resource_pack.cause",
    "crash_shader_conflict": "diagnosis.crash.shader_conflict.cause",
    "crash_suspected_mod": "diagnosis.crash.suspected_mod.cause",
    "crash_version_mismatch": "diagnosis.crash.version_mismatch.cause",
    "launch_disk_full": "diagnosis.launch.disk_full.cause",
    "launch_download": "diagnosis.launch.download.cause",
    "launch_file_access": "diagnosis.launch.file_access.cause",
    "launch_heap_args": "diagnosis.launch.heap_args.cause",
    "launch_jvm_args": "diagnosis.launch.jvm_args.cause",
    "crash_main_class": "diagnosis.crash.main_class.cause",
    "launch_java_missing": "diagnosis.launch.java_missing.cause",
    "launch_offline_gate": "diagnosis.launch.offline_gate.cause",
    "launch_version_json": "diagnosis.launch.version_json.cause",
    "nothing": "diagnosis.nothing.cause",
    "unknown": "diagnosis.unknown.cause",
}

CAUSE_KEYS = frozenset(CAUSES.values())


def cause_key(value: str) -> str:
    """Resolve a rule cause (short id or i18n key) to the key the UI uses."""
    return CAUSES.get(value, value)


def is_known_cause(value: str) -> bool:
    """Whether ``value`` is one of the declared causes."""
    return value in CAUSES or value in CAUSE_KEYS


__all__ = ["CAUSES", "CAUSE_KEYS", "cause_key", "is_known_cause"]
