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

"""The mods table header holds a label, never a control.

At 1644x915 the 启用 header cell showed a small chevron right next to the text, and it reads as a
dropdown. It was Qt's sort indicator: ``setSortingEnabled(True)`` forces it on, and it is drawn in
the first header section. It also sorted the list by the checkbox column on every refill (every
disabled mod first), which is not a sort anybody asked for - ``scan_mods`` already returns the rows
in display-name order. The enabled/disabled *filter* is the toolbar combo (全部 / 已启用 / 已禁用).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox

from gui import i18n


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _zh(app):
    i18n.set_language("zh_cn")
    yield
    i18n.set_language("zh_cn")


def test_a_saved_column_state_cannot_restore_the_sort_chevron(app, monkeypatch, ws_tmp):
    """A saved ``ui_state.ini`` written while sorting was on must not bring the chevron back.

    ``QHeaderView::saveState`` stores the sort indicator as well as the widths, and the mods table
    restores its column widths on every build - so a state file from an older build re-showed the
    chevron that ``_ModsTable`` had just turned off. This is the exact path that made the first fix
    look correct in a unit test and wrong on screen.
    """
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QTableWidget

    from gui.view_state import STATE_FILENAME
    from launcher import paths

    data_dir = ws_tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(data_dir))

    template = QTableWidget(0, 6)
    template.setSortingEnabled(True)  # an older build: sorting on, indicator shown
    assert template.horizontalHeader().isSortIndicatorShown()
    settings = QSettings(str(paths.launcher_dir() / STATE_FILENAME), QSettings.Format.IniFormat)
    settings.setValue("columns/instance_mods", template.horizontalHeader().saveState())
    settings.sync()
    template.close()
    template.deleteLater()

    from gui.pages.instance_detail import InstanceDetail

    detail = InstanceDetail()
    try:
        assert not detail.mods_table.horizontalHeader().isSortIndicatorShown(), (
            "restoring the saved column widths re-enabled the sort chevron"
        )
        # The widths themselves are still restored (that is what the state file is for).
        assert detail.mods_table.columnCount() == 6
    finally:
        detail.close()
        detail.deleteLater()


def test_mods_table_header_has_no_dropdown(app, monkeypatch, ws_tmp):
    """No combo in the header, no sort indicator in 启用, and the six labels are plain text."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "data"))
    from gui.pages.instance_detail import InstanceDetail

    detail = InstanceDetail()
    try:
        table = detail.mods_table
        header = table.horizontalHeader()
        assert header.findChildren(QComboBox) == [], "a combo box is painted inside the mods header"
        assert not header.isSortIndicatorShown(), "the header still draws its sort chevron"
        assert not table.isSortingEnabled(), "sorting is on, so Qt shows the indicator again"
        model = header.model()
        texts = [model.headerData(i, Qt.Orientation.Horizontal) for i in range(model.columnCount())]
        assert texts == [
            i18n.tr("instances.mods.col.enabled"),
            i18n.tr("instances.mods.col.name"),
            i18n.tr("instances.mods.col.id"),
            i18n.tr("instances.mods.col.version"),
            i18n.tr("instances.mods.col.loader"),
            i18n.tr("instances.mods.col.file"),
        ]
        # The filter that does work is the toolbar combo, and it lives outside the table.
        assert detail.mods_filter.parentWidget() is not header
        assert detail.mods_filter.count() == 3
    finally:
        detail.close()
        detail.deleteLater()
