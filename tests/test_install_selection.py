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
"""The install body 修改/导出 reuse: core first, then loader, and cancellation after either.

Moved here from the old 修改 download test when that page stopped choosing versions in a dialog:
the function is the shared seam both features call, so it keeps its own tests independent of either
page's interaction model.
"""

from __future__ import annotations

import pytest

from gui import i18n
from gui.pages.versions_install_flow import (
    InstallFailed,
    install_selection,
)
from launcher.mods.models import LoaderVersion
from launcher.net.downloader import DownloadCancelled


def _install_result(*, failed: list | None = None):
    class _Result:
        pass

    result = _Result()
    result.failed = list(failed or [])
    return result


def _loader() -> LoaderVersion:
    return LoaderVersion(
        loader="fabric", version="0.20.0", game_version="1.21.11", installer_url=""
    )


def test_install_selection_runs_the_core_then_the_loader(monkeypatch, ws_tmp):
    calls: list = []
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_version",
        lambda version_id, **kwargs: calls.append(("core", version_id))
        or _install_result(),
    )
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_loader",
        lambda loader_version, game_dir, **kwargs: calls.append(
            ("loader", loader_version.version)
        )
        or "fabric-loader-0.20.0-1.21.11",
    )

    outcome = install_selection("1.21.11", loader_version=_loader(), game_dir=ws_tmp / "mc")

    assert calls == [("core", "1.21.11"), ("loader", "0.20.0")]
    assert outcome.profile_id == "fabric-loader-0.20.0-1.21.11"


def test_install_selection_refuses_a_partial_core(monkeypatch, ws_tmp):
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_version",
        lambda version_id, **kwargs: _install_result(failed=[("lib.jar", "timeout")]),
    )
    with pytest.raises(InstallFailed) as caught:
        install_selection("1.21.11", game_dir=ws_tmp / "mc")
    assert i18n.tr("versions.msg.partial", 1) in str(caught.value)


def test_install_selection_can_skip_the_core_and_still_honours_cancel(monkeypatch, ws_tmp):
    calls: list = []
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_version",
        lambda version_id, **kwargs: calls.append("core") or _install_result(),
    )
    monkeypatch.setattr(
        "gui.pages.versions_install_flow.install_loader",
        lambda loader_version, game_dir, **kwargs: calls.append("loader")
        or "fabric-loader-0.20.0-1.21.11",
    )
    with pytest.raises(DownloadCancelled):
        install_selection(
            "1.21.11",
            loader_version=_loader(),
            install_base=False,
            game_dir=ws_tmp / "mc",
            should_stop=lambda: True,
        )
    assert calls == ["loader"]
