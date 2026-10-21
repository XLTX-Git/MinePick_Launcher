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

"""options.txt writes: one key at a time, style-preserving, atomic, never fatal."""

from __future__ import annotations

import logging

from launcher.launch.command import _ensure_options_lang
from launcher.launch.options import OptionWriteResult, ensure_option


def test_key_with_space_after_colon_keeps_that_style(ws_tmp):
    path = ws_tmp / "options.txt"
    path.write_text("fov:70\nkey: 1\ngamma:1.0\n", encoding="utf-8")
    assert ensure_option(path, "key", "2") is OptionWriteResult.WRITTEN
    assert path.read_text(encoding="utf-8") == "fov:70\nkey: 2\ngamma:1.0\n"


def test_only_the_target_key_is_replaced(ws_tmp):
    path = ws_tmp / "options.txt"
    path.write_text("key: 1\nkey_extra: 9\n", encoding="utf-8")
    assert ensure_option(path, "key", "2") is OptionWriteResult.WRITTEN
    assert path.read_text(encoding="utf-8") == "key: 2\nkey_extra: 9\n"


def test_new_file_has_no_bom_and_ends_with_lf(ws_tmp):
    path = ws_tmp / "options.txt"
    assert ensure_option(path, "onboardAccessibility", "true") is OptionWriteResult.WRITTEN
    assert path.read_bytes() == b"onboardAccessibility:true\n"
    assert not path.with_name(path.name + ".bak").exists()


def test_bom_and_crlf_are_normalized_on_write(ws_tmp):
    path = ws_tmp / "options.txt"
    path.write_bytes(b"\xef\xbb\xbfkey: 1\r\nother: 2\r\n")
    assert ensure_option(path, "key", "2") is OptionWriteResult.WRITTEN
    assert path.read_bytes() == b"key: 2\nother: 2\n"


def test_same_value_is_unchanged_and_creates_no_backup(ws_tmp):
    path = ws_tmp / "options.txt"
    path.write_text("key: 1\n", encoding="utf-8")
    assert ensure_option(path, "key", "1") is OptionWriteResult.UNCHANGED
    assert path.read_text(encoding="utf-8") == "key: 1\n"
    assert not path.with_name(path.name + ".bak").exists()


def test_first_write_creates_one_backup_and_never_overwrites_it(ws_tmp):
    path = ws_tmp / "options.txt"
    backup = path.with_name(path.name + ".bak")
    path.write_text("key: 1\n", encoding="utf-8")
    assert ensure_option(path, "key", "2") is OptionWriteResult.WRITTEN
    assert backup.read_text(encoding="utf-8") == "key: 1\n"

    path.write_text("key: 2\n", encoding="utf-8")
    assert ensure_option(path, "key", "3") is OptionWriteResult.WRITTEN
    assert path.read_text(encoding="utf-8") == "key: 3\n"
    assert backup.read_text(encoding="utf-8") == "key: 1\n"


def test_missing_final_newline_is_preserved_when_appending(ws_tmp):
    path = ws_tmp / "options.txt"
    path.write_text("fov:70", encoding="utf-8")
    assert ensure_option(path, "key", "1") is OptionWriteResult.WRITTEN
    assert path.read_text(encoding="utf-8") == "fov:70\nkey:1"
    assert not path.read_text(encoding="utf-8").endswith("\n")


def test_missing_final_newline_is_preserved_when_replacing(ws_tmp):
    path = ws_tmp / "options.txt"
    path.write_text("key:1", encoding="utf-8")
    assert ensure_option(path, "key", "2") is OptionWriteResult.WRITTEN
    assert path.read_text(encoding="utf-8") == "key:2"


def test_read_only_file_fails_without_raising_and_keeps_the_original(ws_tmp, caplog):
    path = ws_tmp / "options.txt"
    original = "key: 1\n"
    path.write_text(original, encoding="utf-8")
    path.chmod(0o444)
    try:
        with caplog.at_level(logging.WARNING, logger="launcher.launch.options"):
            result = ensure_option(path, "key", "2")
        assert result is OptionWriteResult.FAILED
        assert path.read_text(encoding="utf-8") == original
        assert not path.with_name(path.name + ".bak").exists()
        assert "read-only" in caplog.text
    finally:
        path.chmod(0o666)


def test_read_only_language_file_does_not_fail_the_launch(ws_tmp, caplog):
    path = ws_tmp / "options.txt"
    original = "fov:70\nlang:en_us\n"
    path.write_text(original, encoding="utf-8")
    path.chmod(0o444)
    try:
        with caplog.at_level(logging.WARNING):
            _ensure_options_lang(path, "zh_cn")  # must return, not raise
        assert path.read_text(encoding="utf-8") == original
        assert "continuing the launch" in caplog.text
    finally:
        path.chmod(0o666)


def test_language_write_swallows_an_unexpected_writer_error(ws_tmp, monkeypatch, caplog):
    path = ws_tmp / "options.txt"

    def _boom(*_args, **_kwargs):
        raise OSError("locked by another process")

    monkeypatch.setattr("launcher.launch.command.ensure_option", _boom)
    with caplog.at_level(logging.WARNING):
        _ensure_options_lang(path, "zh_cn")  # must return, not raise
    assert "continuing the launch" in caplog.text


def test_language_write_uses_the_same_option_writer(ws_tmp):
    path = ws_tmp / "options.txt"
    path.write_text("fov:70\nlang:en_us\n", encoding="utf-8")
    _ensure_options_lang(path, "zh_cn")
    assert path.read_text(encoding="utf-8") == "fov:70\nlang:zh_cn\n"
    # The new writer did the work, including the one-time backup.
    assert path.with_name(path.name + ".bak").read_text(encoding="utf-8") == (
        "fov:70\nlang:en_us\n"
    )
