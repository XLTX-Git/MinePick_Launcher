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

"""First-run wizard: language / game directory / default memory."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from gui import i18n
from gui.widgets import NoWheelDoubleSpinBox

tr = i18n.tr


class FirstRunWizard(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        total_gb: float | None = None,
        available_gb: float | None = None,
        java_32bit: bool | None = None,
    ) -> None:
        """``total_gb`` / ``available_gb`` / ``java_32bit`` let a test (or a preview) state the
        machine instead of probing it; in the app they stay ``None`` and the real device answers."""
        super().__init__(parent)
        self.setWindowTitle(tr("wizard.title"))
        self.setMinimumWidth(460)
        from gui.memory_bounds import memory_bounds
        from launcher import config
        from launcher.memory import HEAP_STEP_GB

        cfg, _ = config.load()
        self.language_combo = QComboBox()
        for code, label in i18n.UI_LANGUAGES:
            self.language_combo.addItem(label, code)
        # Preselect the current language (already detected from the system language on first launch)
        preset = self.language_combo.findData(i18n.current_language())
        self.language_combo.setCurrentIndex(max(preset, 0))
        self.game_dir_edit = QLineEdit()
        self.game_dir_edit.setPlaceholderText(tr("wizard.default_dir.hint"))
        # Prefill with the current config so a re-run never clobbers existing settings
        if cfg.game_dir:
            self.game_dir_edit.setText(str(cfg.game_dir))
        self.browse_button = QPushButton(tr("settings.browse"))
        self.browse_button.setObjectName("secondaryButton")
        # The range is this machine's range, not a fixed 0.5-64: same rule as the settings page
        # (half of physical RAM, OS keeps 2 GB, 1 GB floor, 32-bit Java caps at 1.5 GB), and the
        # preselected value is what that rule recommends rather than a hard-coded 4 GB.
        self.memory_bounds = memory_bounds(
            total_gb=total_gb,
            available_gb=available_gb,
            java_32bit=java_32bit,
            java_path=cfg.java_path,
        )
        self.memory_spin = NoWheelDoubleSpinBox()
        self.memory_spin.setDecimals(1)  # "3.5 GB", never "3.50": the same one-decimal read-out the settings page uses
        self.memory_spin.setRange(self.memory_bounds.minimum_gb, self.memory_bounds.maximum_gb)
        self.memory_spin.setSingleStep(HEAP_STEP_GB)
        self.memory_spin.setValue(self.memory_bounds.recommended_gb)
        # The field is the number; the unit is in the row label, like the settings page.
        self.memory_device_label = QLabel(
            tr("wizard.memory.device", f"{self.memory_bounds.total_gb:.1f}")
        )
        self.memory_device_label.setObjectName("hint")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("wizard.finish"))
        self.buttons.accepted.connect(self.accept)
        self.browse_button.clicked.connect(self._browse)

        hint = QLabel(tr("wizard.hint"))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.game_dir_edit, 1)
        dir_row.addWidget(self.browse_button)
        memory_row = QHBoxLayout()
        memory_row.setContentsMargins(0, 0, 0, 0)
        memory_row.setSpacing(8)
        memory_row.addWidget(self.memory_spin)
        memory_row.addWidget(self.memory_device_label)
        memory_row.addStretch(1)
        form = QFormLayout()
        form.addRow(tr("wizard.language"), self.language_combo)
        form.addRow(tr("wizard.game_dir"), dir_row)
        form.addRow(tr("wizard.memory") + tr("unit.gb.label"), memory_row)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        # The 32-bit ceiling is stated, not silent: a 1.5 GB maximum would otherwise look like a
        # broken slider instead of the JVM's real limit.
        self.memory_cap_label = QLabel("")
        self.memory_cap_label.setObjectName("hint")
        self.memory_cap_label.setWordWrap(True)
        self.memory_cap_label.setVisible(self.memory_bounds.java_32bit)
        if self.memory_bounds.java_32bit:
            self.memory_cap_label.setText(
                tr("settings.memory_java32", f"{self.memory_bounds.maximum_gb:g}")
            )
        layout.addWidget(self.memory_cap_label)
        layout.addWidget(self.buttons)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, tr("wizard.game_dir"))
        if chosen:
            self.game_dir_edit.setText(chosen)

    def apply(self, cfg, cfg_path) -> None:
        """Write the wizard result to config (language_initialized follows the existing first-run language rule)."""
        from launcher import config

        cfg.ui_language = self.language_combo.currentData()
        cfg.game_dir = self.game_dir_edit.text().strip() or None
        cfg.memory_gb = self.memory_spin.value()
        cfg.wizard_done = True
        config.save(cfg, cfg_path)
