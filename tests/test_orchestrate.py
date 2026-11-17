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

import json

import pytest

from launcher.auth import create_offline_account
from launcher.launch.orchestrate import JavaMissingError, prepare_launch

VERSION_RAW = {
    "id": "testv",
    "type": "release",
    "mainClass": "net.minecraft.client.main.Main",
    "minecraftArguments": "--demo",
    "javaVersion": {"component": "jre-legacy", "majorVersion": 8},
    "assetIndex": {"id": "idx1", "url": "https://x/idx1.json"},
    "libraries": [],
    "downloads": {"client": {"sha1": "a", "size": 1, "url": "https://x/client.jar"}},
}


def _prepare(ws_tmp):
    cache = ws_tmp / "cache"
    (cache / "versions").mkdir(parents=True)
    (cache / "versions" / "testv.json").write_text(json.dumps(VERSION_RAW), encoding="utf-8")
    return ws_tmp / "game", cache


def test_prepare_launch_raises_java_missing(ws_tmp, monkeypatch):
    import launcher.launch.orchestrate as orch

    game, cache = _prepare(ws_tmp)
    monkeypatch.setattr(orch, "list_java", lambda probe_dir=None: [])
    with pytest.raises(JavaMissingError) as ei:
        prepare_launch(
            "testv",
            game_dir=game,
            cache_dir=cache,
            account=create_offline_account("Steve"),
        )
    assert ei.value.required_major == 8
    assert "需要 Java 8" in str(ei.value)

def test_resolve_launch_account_offline_locked(ws_tmp, monkeypatch):
    """When not verified (and not in English): explicit offline launch / no-account fallback both raise OfflineLockedError."""
    from launcher.auth import AccountStore
    from launcher.launch import OfflineLockedError, resolve_launch_account

    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    monkeypatch.setattr("launcher.config.offline_mode_allowed", lambda: False)
    store = AccountStore()
    with pytest.raises(OfflineLockedError):
        resolve_launch_account(store, None, "Steve")  # explicit offline
    with pytest.raises(OfflineLockedError):
        resolve_launch_account(store, None, None)  # no account falls back to Player
    # works after unlock
    monkeypatch.setattr("launcher.config.offline_mode_allowed", lambda: True)
    account = resolve_launch_account(store, None, "Steve")
    assert account.username == "Steve"
    assert account.type == "offline"

def test_the_launch_call_only_passes_keywords_build_argv_accepts() -> None:
    """The launch path broke silently once: orchestrate passed ``isolated=`` after the parameter had been
    renamed to ``launch_dir=``, so every launch from the window died with a TypeError while these tests
    stayed green - they only ever exercised the error paths.

    Comparing the call site's keywords against the real signature catches that class of rename without
    standing up a whole version, a Java install and a natives extraction just to reach one call.
    """
    import ast
    import inspect
    import pathlib

    from launcher.launch.command import build_argv

    source = pathlib.Path("launcher/launch/orchestrate.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_argv"
    ]
    assert calls, "orchestrate no longer calls build_argv - update this test"

    accepted = set(inspect.signature(build_argv).parameters)
    for call in calls:
        used = {keyword.arg for keyword in call.keywords if keyword.arg}
        unknown = used - accepted
        assert not unknown, (
            f"orchestrate passes {sorted(unknown)} to build_argv, which takes {sorted(accepted)}"
        )

def test_an_isolated_launch_takes_the_client_jar_from_the_shared_game_dir(ws_tmp):
    """Isolation moves the working directory; versions/ stays in the shared game dir.

    The launch used to pass the instance folder as build_argv's game_dir, so the client jar was looked up
    at <instance>/versions/<id>/<id>.jar - never present - and Java answered "could not find or load main
    class". Every isolated launch failed that way.
    """
    import json

    from launcher.launch.command import LaunchProfile, build_argv
    from launcher.meta.rules import Platform
    from launcher.meta.version import load_version_json

    game = ws_tmp / "mc"
    version_id = "1.21.1"
    version_dir = game / "versions" / version_id
    version_dir.mkdir(parents=True)
    (version_dir / f"{version_id}.jar").write_bytes(b"jar")
    profile_path = version_dir / f"{version_id}.json"
    profile_path.write_text(
        json.dumps(
            {
                "id": version_id,
                "mainClass": "net.minecraft.client.main.Main",
                "libraries": [],
                "arguments": {"jvm": ["-cp", "${classpath}"], "game": []},
                "assetIndex": {"id": "1", "url": "https://example.invalid/1.json"},
                "assets": "1",
            }
        ),
        encoding="utf-8",
    )
    version = load_version_json(
        version_id, versions_dir=game / "versions", cache_dir=ws_tmp / "cache"
    )
    instance = game / "versions" / "1.21.1-copy"  # the isolated working directory
    instance.mkdir(parents=True)
    command = build_argv(
        version,
        game_dir=game,
        libraries_dir=game / "libraries",
        natives_dir=version_dir / "natives",
        java_path=ws_tmp / "java.exe",
        java_major=21,
        profile=LaunchProfile(username="P", uuid="0" * 32, access_token="0", user_type="legacy"),
        platform=Platform(os="windows", arch="x64", version="10.0"),
        resolved_libraries=(),
        memory_gb=2.0,
        launch_dir=instance,
    )
    argv = command.argv
    classpath = argv[argv.index("-cp") + 1]
    assert str(version_dir / f"{version_id}.jar") in classpath
    assert command.cwd == instance
