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

"""The bundled Chinese name table and the three matching layers built on it.

The data file is shipped content, so it is tested like content: its shape (one slug per project,
at least 400 projects, no empty alias) and the searches a Chinese user actually types.
"""

from __future__ import annotations

import json

import pytest

from launcher import paths
from launcher.mods.zh_names import (
    EXACT,
    SUBSTRING,
    TOKEN,
    english_query,
    has_cjk,
    match_slugs,
    name_match,
    search_local,
    tokenize,
    zh_aliases,
    zh_name,
    zh_names,
)

DATA = "launcher/mods/data/zh_mod_names.json"

#: A three-entry table for the layer tests: the bundled one holds hundreds of projects, so a hit
#: there proves the data, not the algorithm.
TABLE = {
    "litematica": ("投影", "Litematica"),
    "entityculling": ("实体渲染机制优化", "Entity Culling"),
    "sodium": ("钠", "Sodium"),
    "appleskin": ("苹果皮", "AppleSkin"),
}


def test_table_has_at_least_four_hundred_projects() -> None:
    assert len(zh_aliases()) >= 400
    assert len(zh_names()) >= 400


def test_table_has_no_duplicate_slug() -> None:
    seen: dict[str, object] = {}

    def pairs_hook(pairs):
        for key, value in pairs:
            assert key not in seen, f"duplicate slug in {DATA}: {key}"
            seen[key] = value
        return seen

    json.loads(paths.resource_path(DATA).read_text(encoding="utf-8"), object_pairs_hook=pairs_hook)
    assert len(seen) == len(zh_aliases())


def test_every_entry_has_non_empty_trimmed_aliases() -> None:
    for slug, aliases in zh_aliases().items():
        assert slug and slug == slug.strip(), repr(slug)
        assert len(aliases) >= 1, slug
        for alias in aliases:
            assert alias and alias == alias.strip(), (slug, alias)
        assert len(set(aliases)) == len(aliases), slug  # no alias listed twice


def test_every_entry_can_find_itself() -> None:
    """Each alias of each project resolves back to that project (the table is its own index)."""
    for slug, aliases in zh_aliases().items():
        for alias in aliases:
            assert slug in search_local(alias), (slug, alias)


def test_zh_name_and_unknown_slug() -> None:
    assert zh_name("sodium") == zh_aliases()["sodium"][0]
    assert zh_name("__definitely_not_a_mod__") == ""


#: The 29 shader projects whose only Chinese spelling used to be "<English> 光影". Shader packs have
#: no settled Chinese name, so the invented suffix is gone and the row keeps the API's English title.
NO_LONGER_NAMED_SHADERS = (
    "astralex",
    "bliss-shader",
    "bloop-shaders",
    "body-camera-shader",
    "bsl-shaders",
    "bsl-shaders-classic",
    "complementary-reimagined",
    "complementary-unbound",
    "fastpbr",
    "hysteria-shaders",
    "insanity-shader",
    "iris",
    "kappa-shader",
    "makeup-ultra-fast-shaders",
    "mellow",
    "miniature-shader",
    "noble",
    "nostalgia-shader",
    "pastel-shaders",
    "photon-shader",
    "potato-shaders",
    "rethinking-voxels",
    "shrimple",
    "solas-shader",
    "spooklementary",
    "super-duper-vanilla",
    "vanilla-plus-shader",
    "vanillaa",
    "visual-vibrance",
)


def test_no_alias_ends_with_the_invented_shader_suffix() -> None:
    """「光影」 was appended to English names; it is not a name the community uses."""
    offenders = [
        (slug, alias)
        for slug, aliases in zh_aliases().items()
        for alias in aliases
        if alias.endswith("光影")
    ]
    assert offenders == [], f"invented shader suffix is back: {offenders}"


def test_shader_projects_are_not_in_the_chinese_name_table() -> None:
    """Not just the suffix: the whole alias is gone, so the API's English title is shown."""
    for slug in NO_LONGER_NAMED_SHADERS:
        assert slug not in zh_aliases(), slug
        assert zh_name(slug) == "", slug
    # The user's example: this row must read "Complementary Shaders - Reimagined", the API title.
    assert zh_name("complementary-reimagined") == ""


def test_zombie_invade_uses_the_community_name_and_keeps_the_old_one_searchable() -> None:
    assert zh_name("zombie-invade-100-days") == "惊变100天"
    assert search_local("僵尸入侵100天")[:1] == ["zombie-invade-100-days"]
    assert search_local("惊变100天")[:1] == ["zombie-invade-100-days"]


def test_the_deleted_shader_alias_is_a_plain_miss_not_an_error() -> None:
    """「BSL 光影」 used to hit the table through the invented alias; now it just finds nothing."""
    assert match_slugs("BSL 光影") == []
    assert search_local("BSL 光影") == []
    # English still reaches the API by the ordinary search path; the local table no longer claims it.
    assert zh_name("bsl-shaders") == ""


def test_has_cjk() -> None:
    assert has_cjk("钠") is True
    assert has_cjk("优化 钠") is True
    assert has_cjk("sodium") is False
    assert has_cjk("") is False


# ---------- layer 1: exact ----------


@pytest.mark.parametrize("query", ["sodium", "SODIUM", "钠"])
def test_exact_match_on_slug_english_name_and_chinese_name(query: str) -> None:
    matches = match_slugs(query, TABLE)
    assert matches[0].slug == "sodium"
    assert matches[0].tier == EXACT


def test_exact_match_wins_over_a_longer_name_containing_it() -> None:
    # "钠" is also a substring of "钠扩展"; the exact entry must come first.
    table = {"sodium": ("钠",), "sodium-extra": ("钠扩展",)}
    assert [match.slug for match in match_slugs("钠", table)] == ["sodium", "sodium-extra"]


# ---------- layer 2: substring ----------


@pytest.mark.parametrize("query", ["投影仪", "投影模组"])
def test_substring_matches_when_a_recorded_name_is_inside_the_query(query: str) -> None:
    matches = match_slugs(query, TABLE)
    assert matches[0].slug == "litematica"
    assert matches[0].tier == SUBSTRING


def test_substring_matches_when_the_query_is_inside_a_recorded_name() -> None:
    matches = match_slugs("litema", TABLE)
    assert matches[0].slug == "litematica"
    assert matches[0].tier == SUBSTRING


# ---------- layer 3: tokens ----------


def test_token_layer_matches_when_no_single_field_contains_the_query() -> None:
    # "apple skin" is not a substring of "AppleSkin", but both words are.
    matches = match_slugs("apple skin", TABLE)
    assert matches[0].slug == "appleskin"
    assert matches[0].tier == TOKEN


def test_chinese_run_without_spaces_is_split_using_the_table() -> None:
    """The reported case: 实体渲染优化, with no spaces and no such name in the table."""
    assert tokenize("实体渲染优化") in {("实体渲染", "优化"), ("实体", "渲染", "优化")}
    matches = match_slugs("实体渲染优化", TABLE)
    assert matches[0].slug == "entityculling"
    assert matches[0].tier == TOKEN


def test_token_layer_requires_every_word_to_hit() -> None:
    # "apple" is part of "AppleSkin", so the first word alone would match; a compound whose second
    # word is in no name must not.
    assert [match.slug for match in match_slugs("apple", TABLE)] == ["appleskin"]
    assert match_slugs("apple banana", TABLE) == []


# ---------- the two user reports, against the shipped table ----------


def test_projection_finds_litematica() -> None:
    matches = match_slugs("投影")
    assert matches and matches[0].slug == "litematica"
    assert matches[0].tier == EXACT


def test_entity_render_optimization_finds_entity_culling() -> None:
    matches = match_slugs("实体渲染优化")
    assert matches, "实体渲染优化 must reach the table"
    # Modrinth carries the project under both "entityculling" and "entity-culling"; either is fine.
    assert matches[0].name == "实体渲染机制优化"
    assert {match.slug for match in matches} >= {"entityculling"}
    assert matches[0].tier in (EXACT, SUBSTRING, TOKEN)


def test_optimization_query_finds_several_projects() -> None:
    """搜 "优化" is a broad query: it has to return more than the one mod it was tested with."""
    assert len(match_slugs("优化")) >= 3


def test_english_query_turns_a_chinese_word_into_the_slug_the_api_knows() -> None:
    assert english_query("投影") == "litematica"
    assert english_query("实体渲染优化") in {"entityculling", "entity-culling"}
    assert english_query("钠") == "sodium"
    assert english_query("no-such-query-at-all") == ""


def test_name_match_ignores_empty_input() -> None:
    assert name_match("", ("sodium", "Sodium")) is None
    assert name_match("   ", ("sodium", "Sodium")) is None
    assert match_slugs("") == []


def test_chinese_names_are_shown_in_simplified_chinese_only() -> None:
    """The table translates English titles into Chinese; only the Simplified interface wants that.

    A Traditional Chinese, Japanese or German row keeps the project's own title: a Simplified name in
    a Traditional interface is a different script, and in a non-Chinese interface it is simply the
    wrong language.
    """
    from gui import i18n
    from launcher.mods import zh_names

    try:
        assert zh_names.zh_name("litematica"), "the table lost its Chinese name for litematica"
        for code in ("zh_tw", "en_us", "ja_jp", "de_de"):
            i18n.set_language(code)
            assert zh_names.zh_name("litematica") == "", f"{code} still shows a Chinese name"
            # The search half is not a display concern: typing Chinese must still find the project.
            assert zh_names.search_local("投影"), f"{code} lost the local Chinese search"
        i18n.set_language("zh_cn")
        assert zh_names.zh_name("litematica"), "Simplified Chinese stopped showing its own name"
    finally:
        i18n.set_language("zh_cn")
        zh_names.set_names_enabled(True)

