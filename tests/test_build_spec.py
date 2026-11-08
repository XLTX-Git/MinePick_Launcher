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

"""The PyInstaller spec must build with or without the gitignored CurseForge key."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _datas_for(spec_root: Path) -> list[tuple[str, str]]:
    """Run build_exe.spec with the PyInstaller API stubbed out, and return Analysis's datas.

    This tests the spec's own branch, not a full build: the real two-build verification (with and
    without build/cf_key.txt) is reported in the change that introduced the optional entry.
    """

    class _Analysis:
        def __init__(self, *args, **kwargs) -> None:
            captured["datas"] = list(kwargs["datas"])
            self.pure: list = []
            self.zipped_data: list = []
            self.scripts: list = []
            self.binaries: list = []
            self.datas: list = []

    class _PYZ:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _EXE:
        def __init__(self, *args, **kwargs) -> None:
            pass

    captured: dict[str, list[tuple[str, str]]] = {}
    namespace = {
        "__name__": "__main__",
        "SPECPATH": str(spec_root),
        "Analysis": _Analysis,
        "PYZ": _PYZ,
        "EXE": _EXE,
    }
    spec = ROOT / "build_exe.spec"
    # Executing the spec is the point: it is the file PyInstaller executes, and its conditional
    # datas entry is exactly what this test verifies (with the PyInstaller API stubbed above).
    exec(compile(spec.read_text(encoding="utf-8"), str(spec), "exec"), namespace)  # noqa: S102
    return captured["datas"]


def test_spec_builds_without_the_gitignored_key(ws_tmp: Path) -> None:
    """A clean clone has no build/cf_key.txt and must still produce the datas list."""
    datas = _datas_for(ws_tmp)
    assert not any(Path(source).name == "cf_key.txt" for source, _dest in datas)
    # The rest of the bundle is untouched by the conditional entry.
    assert ("gui/resources", "gui/resources") in datas
    assert ("launcher/mods/data", "launcher/mods/data") in datas


def test_spec_bundles_the_key_when_the_release_build_provides_it(ws_tmp: Path) -> None:
    (ws_tmp / "build").mkdir()
    (ws_tmp / "build" / "cf_key.txt").write_text("release-key", encoding="ascii")
    datas = _datas_for(ws_tmp)
    matches = [(source, dest) for source, dest in datas if Path(source).name == "cf_key.txt"]
    assert len(matches) == 1
    assert matches[0][1] == "launcher/mods/data"
