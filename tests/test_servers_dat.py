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

"""servers.dat: the NBT round-trip, the atomic write with its backup, and the page that edits it."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from launcher import servers

#: A real 1×1 PNG: the favicon path decodes it, so a made-up base64 would test nothing.
ICON = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _file(*entries: servers.ServerEntry) -> servers.ServersFile:
    return servers.ServersFile(entries=list(entries))


# ---------------------------------------------------------------- the file


def test_round_trip_keeps_names_addresses_favicons_and_order(ws_tmp) -> None:
    path = ws_tmp / "servers.dat"
    original = _file(
        servers.ServerEntry(name="Hypixel", ip="mc.hypixel.net", icon=ICON, accept_textures=1),
        servers.ServerEntry(name="本地服务器", ip="127.0.0.1:25565"),
        servers.ServerEntry(name="Third", ip="example.org"),
    )

    assert servers.write_servers(path, original) is None  # no previous file, so no backup

    decoded = servers.read_servers(path)
    assert [entry.name for entry in decoded.entries] == ["Hypixel", "本地服务器", "Third"]
    assert [entry.ip for entry in decoded.entries] == [
        "mc.hypixel.net",
        "127.0.0.1:25565",
        "example.org",
    ]
    assert decoded.entries[0].icon == ICON
    assert decoded.entries[0].accept_textures == 1
    assert decoded.entries[1].accept_textures is None
    assert not (ws_tmp / "servers.dat.tmp").exists()


def test_a_missing_file_is_an_empty_list_not_an_error(ws_tmp) -> None:
    decoded = servers.read_servers(ws_tmp / "absent.dat")
    assert decoded.entries == []


def test_a_corrupt_file_raises_instead_of_returning_garbage(ws_tmp) -> None:
    path = ws_tmp / "bad.dat"
    path.write_bytes(b"this is not a tag stream")
    with pytest.raises(servers.ServersDatError):
        servers.read_servers(path)
    path.write_bytes(b"")
    with pytest.raises(servers.ServersDatError):
        servers.read_servers(path)


def test_write_backs_up_the_previous_file_first(ws_tmp) -> None:
    path = ws_tmp / "servers.dat"
    first = _file(servers.ServerEntry(name="A", ip="a.example"))
    assert servers.write_servers(path, first) is None
    before = path.read_bytes()

    second = _file(servers.ServerEntry(name="B", ip="b.example"))
    backup = servers.write_servers(path, second)

    assert backup == ws_tmp / "servers.dat.bak"
    assert backup.read_bytes() == before
    assert [entry.name for entry in servers.read_servers(path).entries] == ["B"]


def test_the_replace_is_atomic_and_a_failed_one_leaves_the_old_file(ws_tmp, monkeypatch) -> None:
    path = ws_tmp / "servers.dat"
    servers.write_servers(path, _file(servers.ServerEntry(name="old", ip="old.example")))
    before = path.read_bytes()

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(servers.os, "replace", fail_replace)
    with pytest.raises(servers.ServersDatError):
        servers.write_servers(path, _file(servers.ServerEntry(name="new", ip="new.example")))

    assert path.read_bytes() == before
    assert not (ws_tmp / "servers.dat.tmp").exists()


def test_move_entry_swaps_and_ignores_out_of_range_moves() -> None:
    entries = [
        servers.ServerEntry(name="A", ip="a"),
        servers.ServerEntry(name="B", ip="b"),
        servers.ServerEntry(name="C", ip="c"),
    ]
    assert [entry.name for entry in servers.move_entry(entries, 0, 1)] == ["B", "A", "C"]
    assert [entry.name for entry in servers.move_entry(entries, 2, 1)] == ["A", "B", "C"]
    assert [entry.name for entry in servers.move_entry(entries, 0, -1)] == ["A", "B", "C"]


def test_an_entry_can_be_renamed_without_losing_its_icon() -> None:
    entry = servers.ServerEntry(name="A", ip="a", icon=ICON, accept_textures=0)
    renamed = entry.renamed("B", "b")
    assert (renamed.name, renamed.ip) == ("B", "b")
    assert renamed.icon == ICON
    assert renamed.accept_textures == 0


# ---------------------------------------------------------------- the page


def _page(path):
    from gui.pages.servers_page import ServersPage

    page = ServersPage(path=path)
    return page


def test_the_page_reads_what_the_file_says(app, ws_tmp) -> None:
    path = ws_tmp / "servers.dat"
    servers.write_servers(
        path,
        _file(
            servers.ServerEntry(name="Hypixel", ip="mc.hypixel.net", icon=ICON),
            servers.ServerEntry(name="本地", ip="127.0.0.1"),
        ),
    )
    page = _page(path)
    try:
        assert [row.entry.name for row in page.rows()] == ["Hypixel", "本地"]
        assert page.count_label.text()
        assert not page.read_error.isVisibleTo(page)
    finally:
        page.deleteLater()


def test_adding_a_server_writes_the_file(app, ws_tmp) -> None:
    path = ws_tmp / "servers.dat"
    page = _page(path)
    try:
        page.begin_add()
        page.name_edit.setText("新服务器")
        page.address_edit.setText("play.example.net:25565")
        assert page.save_form() is True
        decoded = servers.read_servers(path)
        assert [(entry.name, entry.ip) for entry in decoded.entries] == [
            ("新服务器", "play.example.net:25565")
        ]
        assert [row.entry.name for row in page.rows()] == ["新服务器"]
    finally:
        page.deleteLater()


def test_editing_keeps_the_favicon_and_unknown_fields(app, ws_tmp) -> None:
    path = ws_tmp / "servers.dat"
    servers.write_servers(
        path,
        _file(
            servers.ServerEntry(
                name="Old", ip="old.example", icon=ICON, accept_textures=1, extra={"tag": "keep"}
            )
        ),
    )
    page = _page(path)
    try:
        page.begin_edit(0)
        page.name_edit.setText("New")
        page.address_edit.setText("new.example")
        assert page.save_form() is True
        decoded = servers.read_servers(path)
        assert decoded.entries[0].name == "New"
        assert decoded.entries[0].ip == "new.example"
        assert decoded.entries[0].icon == ICON
        assert decoded.entries[0].accept_textures == 1
        assert decoded.entries[0].extra == {"tag": "keep"}
    finally:
        page.deleteLater()


def test_the_form_refuses_an_empty_name_or_address(app, ws_tmp) -> None:
    path = ws_tmp / "servers.dat"
    page = _page(path)
    try:
        page.begin_add()
        page.name_edit.setText("only a name")
        assert page.save_form() is False
        assert not path.exists()
    finally:
        page.deleteLater()


def test_moving_an_entry_writes_the_new_order(app, ws_tmp) -> None:
    path = ws_tmp / "servers.dat"
    servers.write_servers(
        path,
        _file(
            servers.ServerEntry(name="A", ip="a"),
            servers.ServerEntry(name="B", ip="b"),
            servers.ServerEntry(name="C", ip="c"),
        ),
    )
    page = _page(path)
    try:
        assert page.move(0, 1) is True
        assert [entry.name for entry in servers.read_servers(path).entries] == ["B", "A", "C"]
        assert page.move(2, 1) is False  # last row, nothing to do
    finally:
        page.deleteLater()


def test_deleting_asks_and_defaults_to_cancel(app, ws_tmp, monkeypatch) -> None:
    from gui import inwindow_dialog
    from gui.pages import servers_page

    path = ws_tmp / "servers.dat"
    servers.write_servers(path, _file(servers.ServerEntry(name="Keep", ip="keep.example")))
    page = _page(path)
    seen: dict[str, object] = {}

    def refuse(parent, title, text, **kwargs):
        seen.update(kwargs)
        return inwindow_dialog.Outcome.DISMISSED

    monkeypatch.setattr(servers_page.inwindow_dialog, "confirm", refuse)
    try:
        assert page.delete_entry(0) is False
        assert [entry.name for entry in servers.read_servers(path).entries] == ["Keep"]
        assert seen["default"] is inwindow_dialog.Outcome.REJECTED
    finally:
        page.deleteLater()


def test_deleting_removes_the_entry_once_accepted(app, ws_tmp, monkeypatch) -> None:
    from gui import inwindow_dialog
    from gui.pages import servers_page

    path = ws_tmp / "servers.dat"
    servers.write_servers(
        path,
        _file(
            servers.ServerEntry(name="A", ip="a"),
            servers.ServerEntry(name="B", ip="b"),
        ),
    )
    page = _page(path)
    monkeypatch.setattr(
        servers_page.inwindow_dialog,
        "confirm",
        lambda *args, **kwargs: inwindow_dialog.Outcome.ACCEPTED,
    )
    try:
        assert page.delete_entry(0) is True
        assert [entry.name for entry in servers.read_servers(path).entries] == ["B"]
        assert page.rows()[0].entry.name == "B"
    finally:
        page.deleteLater()


def test_copying_an_address_puts_it_on_the_clipboard(app, ws_tmp) -> None:
    from PySide6.QtWidgets import QApplication

    path = ws_tmp / "servers.dat"
    servers.write_servers(path, _file(servers.ServerEntry(name="A", ip="play.example:25565")))
    page = _page(path)
    try:
        assert page.copy_address(0) is True
        assert QApplication.clipboard().text() == "play.example:25565"
    finally:
        page.deleteLater()


def test_a_broken_file_opens_as_empty_and_can_be_repaired(app, ws_tmp, monkeypatch) -> None:
    path = ws_tmp / "servers.dat"
    path.write_bytes(b"garbage")
    page = _page(path)
    try:
        assert page.entries() == []
        assert page.read_error.isVisibleTo(page)
        assert page.stack.currentIndex() == 1  # the empty state
        page.begin_add()
        page.name_edit.setText("Recovered")
        page.address_edit.setText("recovered.example")
        assert page.save_form() is True
        assert [entry.name for entry in servers.read_servers(path).entries] == ["Recovered"]
        assert (ws_tmp / "servers.dat.bak").read_bytes() == b"garbage"
        assert not page.read_error.isVisibleTo(page)
    finally:
        page.deleteLater()


def test_a_failed_write_reports_and_keeps_the_page_alive(app, ws_tmp, monkeypatch) -> None:
    from gui.pages import servers_page

    path = ws_tmp / "servers.dat"
    servers.write_servers(path, _file(servers.ServerEntry(name="A", ip="a")))
    before = path.read_bytes()
    page = _page(path)

    def fail(*args, **kwargs):
        raise servers.ServersDatError("disk full")

    monkeypatch.setattr(servers_page.servers, "write_servers", fail)
    try:
        page.begin_add()
        page.name_edit.setText("B")
        page.address_edit.setText("b")
        assert page.save_form() is False
        assert path.read_bytes() == before
        assert [row.entry.name for row in page.rows()] == ["A"]
    finally:
        page.deleteLater()


def test_a_favicon_data_uri_becomes_a_pixmap(app) -> None:
    from gui.pages.servers_page import favicon_pixmap

    assert favicon_pixmap(ICON) is not None
    assert favicon_pixmap("") is None
    assert favicon_pixmap("data:image/png;base64,@@@") is None
    assert favicon_pixmap("https://example.invalid/icon.png") is None
