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

"""Completing an instance whose files are missing.

The case that prompted this: a game folder made by another launcher whose own downloader could no
longer fetch what it was missing. The instance had a version JSON but no client jar and no library, so
every launch died in the class loader without naming a missing file.
"""

import json

from launcher.repair import plan_repair

VERSION = {
    "id": "demo",
    "mainClass": "net.minecraft.client.main.Main",
    "javaVersion": {"majorVersion": 8},
    "assetIndex": {"id": "idx", "url": "https://x/idx.json"},
    "libraries": [
        {
            "name": "org.ow2.asm:asm:9.5",
            "downloads": {"artifact": {"url": "https://x/asm.jar", "sha1": "00", "size": 10}},
        }
    ],
    "downloads": {"client": {"url": "https://x/client.jar", "sha1": "11", "size": 20}},
}


def _instance(game, version=None):
    folder = game / "versions" / "demo"
    folder.mkdir(parents=True)
    (folder / "demo.json").write_text(json.dumps(version or VERSION), encoding="utf-8")
    return folder


def _no_index(_index_id):
    return None


def test_an_incomplete_instance_lists_its_client_jar_and_library(ws_tmp):
    game = ws_tmp / "game"
    _instance(game)

    plan = plan_repair("demo", game_dir=game, asset_index_fetcher=_no_index)

    names = sorted(task.dest.name for task in plan.tasks)
    assert names == ["asm-9.5.jar", "demo.jar"], names
    assert plan.total_bytes == 30
    assert plan.needed


def test_a_complete_instance_needs_nothing(ws_tmp):
    game = ws_tmp / "game"
    folder = _instance(game)
    libraries = game / "libraries" / "org" / "ow2" / "asm" / "asm" / "9.5"
    libraries.mkdir(parents=True)
    # The downloader skips a file whose size matches, so the sizes here are the real contract.
    (libraries / "asm-9.5.jar").write_bytes(b"x" * 10)
    (folder / "demo.jar").write_bytes(b"y" * 20)

    plan = plan_repair("demo", game_dir=game, asset_index_fetcher=_no_index)

    assert not plan.tasks, [task.dest.name for task in plan.tasks]
    assert not plan.needed


def test_assets_are_reported_as_unchecked_when_the_index_cannot_be_obtained(ws_tmp):
    game = ws_tmp / "game"
    _instance(game)

    plan = plan_repair("demo", game_dir=game, asset_index_fetcher=_no_index)

    assert any("asset index" in problem for problem in plan.problems), plan.problems
    # The rest of the repair still runs: a missing index must not hide a missing library.
    assert len(plan.tasks) == 2


def test_a_missing_version_json_is_reported_not_raised(ws_tmp):
    game = ws_tmp / "game"
    (game / "versions" / "demo").mkdir(parents=True)

    plan = plan_repair("demo", game_dir=game, asset_index_fetcher=_no_index)

    assert not plan.tasks
    assert plan.problems, "an empty folder must say why nothing could be planned"

def test_the_launch_path_completes_a_missing_instance_and_says_so(monkeypatch, ws_tmp):
    """The pre-launch hook: a folder missing files is completed, and the caller gets one line to log.

    Launching used to go straight from "resolve the instance" to "build the command", so an instance
    made by another launcher failed inside the class loader with a message that named no file.
    """
    from gui.pages import launch_page

    game = ws_tmp / "game"
    _instance(game)

    completed: list[str] = []

    def fake_repair(version_id, *, game_dir, **kwargs):
        completed.append(version_id)
        from launcher.net.downloader import DownloadTask
        from launcher.repair import RepairPlan

        plan = RepairPlan(version_id=version_id)
        plan.tasks = [
            DownloadTask(url="https://x/a.jar", dest=game / "a.jar", sha1="00", size=1000)
        ]
        return plan

    monkeypatch.setattr("launcher.repair.repair", fake_repair)
    note = launch_page._complete_missing_files(game, "demo")

    assert completed == ["demo"]
    assert note, "a completed repair must leave a line for the log"
    assert "1" in note


def test_the_launch_path_stays_quiet_when_nothing_is_missing(monkeypatch, ws_tmp):
    from gui.pages import launch_page
    from launcher.repair import RepairPlan

    game = ws_tmp / "game"
    _instance(game)
    monkeypatch.setattr(
        "launcher.repair.repair", lambda version_id, **kwargs: RepairPlan(version_id=version_id)
    )
    assert launch_page._complete_missing_files(game, "demo") == ""


def test_a_failing_repair_never_blocks_the_launch(monkeypatch, ws_tmp):
    from gui.pages import launch_page

    def boom(version_id, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr("launcher.repair.repair", boom)
    assert launch_page._complete_missing_files(ws_tmp / "game", "demo") == ""

def test_the_asset_index_is_fetched_with_its_reference_not_its_id(ws_tmp):
    """The fetcher needs the url, so it gets the version's asset-index object.

    Passing the bare id made every repair raise "'str' object has no attribute 'url'", and the caller
    treats that as "assets were not checked" - a game then started with no language files at all.
    """
    import json

    from launcher.repair import plan_repair

    game = ws_tmp / "game"
    folder = game / "versions" / "demo"
    folder.mkdir(parents=True)
    (folder / "demo.json").write_text(
        json.dumps(
            {
                "id": "demo",
                "mainClass": "net.minecraft.client.main.Main",
                "libraries": [],
                "arguments": {"jvm": [], "game": []},
                "assetIndex": {"id": "30", "url": "https://example.invalid/30.json"},
                "assets": "30",
            }
        ),
        encoding="utf-8",
    )
    seen = []

    def fetcher(reference):
        seen.append(reference)

    plan = plan_repair("demo", game_dir=game, asset_index_fetcher=fetcher)
    assert seen, "the index fetcher was never called"
    assert getattr(seen[0], "url", "") == "https://example.invalid/30.json", seen[0]
    assert any("asset index" in problem for problem in plan.problems)
