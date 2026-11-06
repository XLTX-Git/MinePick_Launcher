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

"""Which JSON inside a version folder counts as the version profile.

The official layout (``<id>/<id>.json``) always wins. A hand-made folder that ships ``version.json``
or one arbitrarily named JSON is accepted, because the real world does exactly that - but pack
metadata is never mistaken for a profile, and "several unknown JSON files" is refused instead of
guessed. A folder whose profile is gone (a damaged install with only its jar left) reports what is
missing instead of a generic read failure.
"""

from __future__ import annotations

import json

import pytest

from launcher import i18n as core_i18n
from launcher.meta import MetaError
from launcher.meta.version import (
    NON_VERSION_JSON_NAMES,
    load_version_json,
    version_json_candidates,
    version_json_error,
    version_json_path,
)

MINIMAL = {
    "mainClass": "net.minecraft.client.main.Main",
    "assetIndex": {"id": "5", "url": "https://example.invalid/5.json"},
    "assets": "5",
    "libraries": [],
}


@pytest.fixture(autouse=True)
def _chinese_messages():
    """The assertions below read the shipped Chinese wording; keep the core table deterministic."""
    core_i18n.set_core_language("zh_cn")
    yield
    core_i18n.set_core_language("zh_cn")


def _folder(root, name: str = "1.20.1"):
    folder = root / "versions" / name
    folder.mkdir(parents=True)
    return folder


def _profile(folder, file_name: str, **extra):
    path = folder / file_name
    payload = dict(MINIMAL, id=folder.name, **extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. the official name always wins
# ---------------------------------------------------------------------------


def test_official_name_wins_even_when_other_json_files_exist(ws_tmp):
    folder = _folder(ws_tmp)
    official = _profile(folder, f"{folder.name}.json")
    _profile(folder, "version.json")
    _profile(folder, "我的整合包.json")

    assert version_json_path(folder) == official


# ---------------------------------------------------------------------------
# 2 + 3. a single JSON under any name is the profile
# ---------------------------------------------------------------------------


def test_version_json_is_accepted(ws_tmp):
    folder = _folder(ws_tmp)
    profile = _profile(folder, "version.json")

    assert version_json_path(folder) == profile
    loaded = load_version_json(folder.name, versions_dir=ws_tmp / "versions")
    assert loaded.main_class == "net.minecraft.client.main.Main"
    assert loaded.id == folder.name


def test_arbitrarily_named_json_is_accepted(ws_tmp):
    folder = _folder(ws_tmp)
    profile = _profile(folder, "我的整合包.json")

    assert version_json_path(folder) == profile
    assert load_version_json(folder.name, versions_dir=ws_tmp / "versions").id == folder.name


def test_renamed_parent_is_followed_through_inheritance(ws_tmp):
    versions = ws_tmp / "versions"
    parent = _folder(ws_tmp, "base")
    _profile(parent, "version.json", libraries=[{"name": "p:lib1:1"}])
    child_folder = _folder(ws_tmp, "child")
    _profile(
        child_folder,
        "我的整合包.json",
        inheritsFrom="base",
        libraries=[{"name": "c:lib1:1"}],
    )

    version = load_version_json("child", versions_dir=versions)
    assert [library.name for library in version.libraries] == ["c:lib1:1", "p:lib1:1"]


# ---------------------------------------------------------------------------
# 4. several candidates cannot be decided
# ---------------------------------------------------------------------------


def test_several_json_files_are_refused_and_named(ws_tmp):
    folder = _folder(ws_tmp)
    _profile(folder, "version.json")
    _profile(folder, "整合包.json")

    assert version_json_path(folder) is None
    message = version_json_error(folder)
    assert "多个" in message
    assert "version.json" in message and "整合包.json" in message
    assert message == core_i18n.tr_core(
        "meta.version_json_ambiguous", folder.name, "version.json, 整合包.json"
    )

    with pytest.raises(MetaError) as raised:
        load_version_json(folder.name, versions_dir=ws_tmp / "versions")
    assert "多个" in str(raised.value)


# ---------------------------------------------------------------------------
# 5. the damaged install: only the jar is left
# ---------------------------------------------------------------------------


def test_jar_only_folder_says_the_profile_is_missing(ws_tmp):
    folder = _folder(ws_tmp, "26.3-snapshot-8")
    (folder / "26.3-snapshot-8.jar").write_bytes(b"jar")

    assert version_json_path(folder) is None
    message = version_json_error(folder)
    assert "没有版本 json" in message
    assert ".jar" in message
    assert message == core_i18n.tr_core(
        "meta.version_json_jar_only", "26.3-snapshot-8"
    )

    with pytest.raises(MetaError) as raised:
        load_version_json("26.3-snapshot-8", versions_dir=ws_tmp / "versions")
    assert ".jar" in str(raised.value) and "json" in str(raised.value)


def test_empty_folder_reports_a_missing_profile(ws_tmp):
    folder = _folder(ws_tmp, "broken")

    assert version_json_path(folder) is None
    assert version_json_error(folder) == core_i18n.tr_core(
        "meta.version_json_missing", "broken"
    )
    with pytest.raises(MetaError) as raised:
        load_version_json("broken", versions_dir=ws_tmp / "versions")
    assert "没有版本 json" in str(raised.value)


# ---------------------------------------------------------------------------
# 6. known non-profile JSON files are never candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(NON_VERSION_JSON_NAMES))
def test_known_pack_metadata_is_excluded(ws_tmp, name):
    folder = _folder(ws_tmp)
    (folder / name).write_text("{}", encoding="utf-8")

    assert version_json_candidates(folder) == []
    assert version_json_path(folder) is None

    # The metadata does not hide a real profile sitting next to it either.
    profile = _profile(folder, "version.json")
    assert version_json_path(folder) == profile


def test_our_sidecar_does_not_hide_a_renamed_profile(ws_tmp):
    folder = _folder(ws_tmp)
    (folder / "minepick.json").write_text('{"note": "红石测试"}', encoding="utf-8")
    profile = _profile(folder, "我的整合包.json")

    assert version_json_path(folder) == profile


def test_subfolder_json_files_are_not_profiles(ws_tmp):
    """A MultiMC-style ``patches/*.json`` set is pack layout we deliberately do not read."""
    folder = _folder(ws_tmp, "prism")
    (folder / "mmc-pack.json").write_text("{}", encoding="utf-8")
    patches = folder / "patches"
    patches.mkdir()
    (patches / "net.minecraft.json").write_text("{}", encoding="utf-8")

    assert version_json_candidates(folder) == []
    assert version_json_path(folder) is None


# ---------------------------------------------------------------------------
# the pre-existing fallback stays intact: no folder -> cache / online
# ---------------------------------------------------------------------------


def test_uninstalled_id_still_falls_back_to_the_cache(ws_tmp):
    cache = ws_tmp / "cache"
    target = cache / "versions" / "remote.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(dict(MINIMAL, id="remote")), encoding="utf-8")

    version = load_version_json(
        "remote", versions_dir=ws_tmp / "versions", cache_dir=cache
    )
    assert version.id == "remote"
