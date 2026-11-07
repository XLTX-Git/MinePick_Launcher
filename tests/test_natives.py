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

import io
import logging
import zipfile
from pathlib import Path

from launcher.launch.natives import LaunchError, _member_target, prepare_natives
from launcher.meta.rules import ResolvedLibrary
from launcher.meta.version import Library


def _make_jar(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _lib(name: str, classifier: str, path: str, exclude=None) -> ResolvedLibrary:
    downloads = {"classifiers": {classifier: {"path": path}}}
    extract = {"exclude": exclude} if exclude else None
    return ResolvedLibrary(
        library=Library(name=name, downloads=downloads, extract=extract),
        classifier=classifier,
    )


def _write_jar(libs_dir: Path, rel: str, entries: dict[str, str]) -> None:
    (libs_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (libs_dir / rel).write_bytes(_make_jar(entries))


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_prepare_natives_extracts_and_excludes(ws_tmp):
    libs_dir = ws_tmp / "libraries"
    _write_jar(
        libs_dir,
        "rel/one.jar",
        {
            "x64.dll": "DLL1",
            "sub/y64.dll": "DLL2",
            "META-INF/MANIFEST.MF": "m",
            "custom/skip.txt": "s",
        },
    )
    resolved = [_lib("g:a:v", "natives-windows", "rel/one.jar", exclude=["custom/"])]
    natives_dir = ws_tmp / "natives"
    prepare_natives(resolved, libs_dir, natives_dir)
    assert (natives_dir / "x64.dll").read_bytes() == b"DLL1"
    assert (natives_dir / "sub" / "y64.dll").read_bytes() == b"DLL2"
    assert not (natives_dir / "META-INF").exists()
    assert not (natives_dir / "custom").exists()


def test_a_normal_jar_extracts_exactly_the_same_tree(ws_tmp):
    """Regression guard for the traversal fix: a well-formed jar still produces the same files.

    The extracted tree is compared whole (names and bytes), so a normal launch cannot be changed by
    the new entry guard without this test noticing.
    """
    libs_dir = ws_tmp / "libraries"
    _write_jar(
        libs_dir,
        "rel/one.jar",
        {
            "a.dll": "A",
            "sub/b.dll": "B",
            "sub/deep/c.dll": "C",
            "META-INF/MANIFEST.MF": "m",
        },
    )
    natives_dir = ws_tmp / "natives"
    prepare_natives(
        [_lib("g:a:v", "natives-windows", "rel/one.jar")], libs_dir, natives_dir
    )
    assert _tree(natives_dir) == {
        "a.dll": b"A",
        "sub/b.dll": b"B",
        "sub/deep/c.dll": b"C",
    }


def test_archive_entries_cannot_escape_the_natives_dir(ws_tmp, caplog):
    """A hostile member name must not write outside ``dest`` - and must not stop the launch either.

    ``../evil.txt`` and ``../../evil-two.txt`` are the historical escape; the absolute entry (a real
    absolute path, anchored inside the test workspace so a regression cannot touch the system drive)
    covers the drive-qualified form (``name`` contains ``:``).
    """
    libs_dir = ws_tmp / "libraries"
    absolute_escape = str(ws_tmp / "absolute-escape.txt")
    _write_jar(
        libs_dir,
        "rel/evil.jar",
        {
            "good.dll": "OK",
            "sub/also-good.dll": "OK2",
            "../evil.txt": "EVIL",
            "../../evil-two.txt": "EVIL2",
            absolute_escape: "ABS",
        },
    )
    two_levels_up = ws_tmp.parent / "evil-two.txt"
    two_levels_up.unlink(missing_ok=True)  # a broken run from before must not poison this one

    natives_dir = ws_tmp / "natives"
    before = {path for path in ws_tmp.rglob("*") if path.is_file()}
    with caplog.at_level(logging.WARNING, logger="launcher.launch.natives"):
        prepare_natives(
            [_lib("g:a:v", "natives-windows", "rel/evil.jar")], libs_dir, natives_dir
        )
    after = {path for path in ws_tmp.rglob("*") if path.is_file()}

    # Nothing at all appeared outside the extraction directory under the workspace...
    added = after - before
    outside = [path for path in added if not path.is_relative_to(natives_dir)]
    assert not outside, f"archive entries escaped the natives directory: {outside}"
    assert {path.relative_to(natives_dir) for path in added} == {
        Path("good.dll"),
        Path("sub/also-good.dll"),
    }
    # ...and the entry that pointed two levels above the workspace was not created either.
    assert not two_levels_up.exists()
    assert not (ws_tmp / "evil.txt").exists()
    assert not Path(absolute_escape).exists()

    # The bad entries are skipped, not fatal: the good ones were extracted.
    assert (natives_dir / "good.dll").read_bytes() == b"OK"
    assert (natives_dir / "sub" / "also-good.dll").read_bytes() == b"OK2"

    # Each skipped entry leaves a warning behind, so a real jar with a broken entry is diagnosable.
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    messages = " ".join(record.getMessage() for record in warnings)
    assert len(warnings) >= 3, messages
    for name in ("evil.txt", "evil-two.txt", "absolute-escape.txt"):
        assert name in messages, messages


def test_member_target_rejects_absolute_and_drive_qualified_names(ws_tmp):
    """The guard itself, without a jar: every absolute spelling is refused, relative paths are kept."""
    dest = (ws_tmp / "natives").resolve()
    dest.mkdir()
    for name in ("", "/evil.dll", "\\evil.dll", "C:/evil.dll", r"C:\evil.dll", "C:evil.dll"):
        assert _member_target(dest, name) is None, name
    assert _member_target(dest, "ok.dll") == dest / "ok.dll"
    assert _member_target(dest, "sub/ok.dll") == dest / "sub" / "ok.dll"


def test_prepare_natives_wipes_dir(ws_tmp):
    libs_dir = ws_tmp / "libraries"
    _write_jar(libs_dir, "rel/one.jar", {"a.dll": "A"})
    resolved = [_lib("g:a:v", "natives-windows", "rel/one.jar")]
    natives_dir = ws_tmp / "natives"
    prepare_natives(resolved, libs_dir, natives_dir)
    (natives_dir / "stale.dll").write_text("old", encoding="utf-8")
    prepare_natives(resolved, libs_dir, natives_dir)
    assert (natives_dir / "a.dll").exists()
    assert not (natives_dir / "stale.dll").exists()


def test_prepare_natives_missing_jar_raises(ws_tmp):
    resolved = [_lib("g:a:v", "natives-windows", "rel/missing.jar")]
    try:
        prepare_natives(resolved, ws_tmp / "libraries", ws_tmp / "natives")
    except LaunchError as exc:
        assert "missing.jar" in str(exc)
    else:
        raise AssertionError("应抛出 LaunchError")
