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

"""Error severity display: fatal errors use a dialog, ordinary warnings only the status bar.

Both dialogs are the in-window card (``gui/inwindow_dialog.py``), not a system message box: every
error the launcher reports is drawn inside its own window, in the same visual language as the crash
diagnosis dialog. The severity still decides what the card looks like - ``critical`` gets the danger
badge, the red scrim and the danger-coloured primary button, ``warning`` the warning badge and an
outlined warning-coloured primary - so "this went wrong" and "this is worth knowing" stay
distinguishable at a glance, exactly as ``QMessageBox.critical``/``warning`` did.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from gui import i18n, inwindow_dialog


def show_fatal(parent: QWidget | None, message: str) -> None:
    """Fatal error: dialog + status bar text. Returns None (the caller sets the status bar itself)."""
    inwindow_dialog.critical(parent, i18n.tr("error.title"), message)


def show_warning(parent: QWidget | None, message: str) -> None:
    """Ordinary warning: non-blocking prompt box."""
    inwindow_dialog.warning(parent, i18n.tr("error.warning.title"), message)
