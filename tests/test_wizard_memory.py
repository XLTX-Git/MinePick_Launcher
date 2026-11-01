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

"""Device-aware memory: the shared bounds and both first-run wizard surfaces."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from gui.memory_bounds import memory_bounds
from launcher import memory as memory_mod


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def _site_machine(monkeypatch):
    """A stated machine (32 GB / 24 GB, 64-bit Java) instead of whatever the build host has."""
    monkeypatch.setattr(memory_mod, "system_memory_gb", lambda: (32.0, 24.0))
    monkeypatch.setattr("launcher.java.locate.selected_java_bits", lambda *a, **k: 64)


# ---------------------------------------------------------------------------
# 1. The shared bounds: the settings page rule, injectable
# ---------------------------------------------------------------------------


def test_bounds_follow_the_settings_page_rule():
    bounds = memory_bounds(total_gb=16.0, available_gb=12.0, java_32bit=False)
    assert bounds.minimum_gb == memory_mod.MIN_HEAP_GB == 1.0
    assert bounds.maximum_gb == 8.0  # half of 16 GB
    assert bounds.maximum_gb == memory_mod.maximum_heap_gb(16.0, java_32bit=False)
    assert bounds.minimum_gb <= bounds.recommended_gb <= bounds.maximum_gb
    # 0.5 GB granularity, like the slider.
    assert bounds.recommended_gb * 2 == int(bounds.recommended_gb * 2)


def test_bounds_use_the_os_reserve_on_small_machines():
    assert memory_bounds(total_gb=4.0, available_gb=2.0, java_32bit=False).maximum_gb == 2.0
    assert memory_bounds(total_gb=8.0, available_gb=4.0, java_32bit=False).maximum_gb == 4.0
    assert memory_bounds(total_gb=32.0, available_gb=20.0, java_32bit=False).maximum_gb == 16.0


def test_bounds_apply_the_32bit_ceiling():
    bounds = memory_bounds(total_gb=16.0, available_gb=12.0, java_32bit=True)
    assert bounds.maximum_gb == memory_mod.JAVA_32BIT_HEAP_LIMIT_GB == 1.5
    assert bounds.recommended_gb <= 1.5


def test_bounds_probe_the_real_device_when_nothing_is_injected():
    bounds = memory_bounds()
    assert bounds.total_gb == 32.0
    assert bounds.available_gb == 24.0
    assert bounds.maximum_gb == 16.0


# ---------------------------------------------------------------------------
# 2. The wizard dialog: real range, recommended default, device line
# ---------------------------------------------------------------------------


def test_wizard_range_default_and_device_line_come_from_the_machine(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "wizard"))
    from gui.pages.wizard import FirstRunWizard

    wizard = FirstRunWizard(total_gb=8.0, available_gb=4.0, java_32bit=False)
    try:
        assert wizard.memory_spin.minimum() == 1.0
        assert wizard.memory_spin.maximum() == 4.0
        assert wizard.memory_spin.singleStep() == 0.5
        # The default is the recommendation, not a hard-coded 4 GB (which is not even legal here:
        # this 8 GB machine tops out at 4 GB).
        assert wizard.memory_spin.value() == wizard.memory_bounds.recommended_gb
        assert 1.0 <= wizard.memory_spin.value() <= 4.0
        assert "8.0" in wizard.memory_device_label.text()
        assert not wizard.memory_cap_label.isVisibleTo(wizard)
    finally:
        wizard.close()
        wizard.deleteLater()
        app.processEvents()


def test_wizard_shows_the_32bit_ceiling(app, monkeypatch, ws_tmp):
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "wizard32"))
    from gui.pages.wizard import FirstRunWizard

    wizard = FirstRunWizard(total_gb=16.0, available_gb=12.0, java_32bit=True)
    try:
        assert wizard.memory_spin.maximum() == 1.5
        assert wizard.memory_spin.value() <= 1.5
        assert "1.5" in wizard.memory_cap_label.text()
    finally:
        wizard.close()
        wizard.deleteLater()
        app.processEvents()


def test_wizard_overlay_uses_the_device_range_too(app, monkeypatch, ws_tmp):
    """The real first-run surface is the in-window overlay; it must state the same numbers."""
    monkeypatch.setenv("MCLAUNCHER_DATA_DIR", str(ws_tmp / "overlay"))
    from gui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_wizard()
        assert window._wz_memory.minimum() == 1.0
        assert window._wz_memory.maximum() == 16.0  # 32 GB host -> half, OS reserve not binding
        assert window._wz_memory.value() == window._wz_memory_bounds.recommended_gb
        assert "32.0" in window._wz_memory_device.text()
        # Slider and spin box agree at both ends of the range.
        window._wz_memory.setValue(16.0)
        assert window._wz_memory_slider.value() == window._wz_memory_slider.maximum()
        window._wz_memory_slider.setValue(0)
        assert window._wz_memory.value() == 1.0
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
