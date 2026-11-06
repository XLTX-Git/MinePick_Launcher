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

"""Official loader logos: the URL table, the 30-day cache, conversion and the drawn fallback.

No test here opens a socket: every request goes through a small duck-typed client or an
``httpx.MockTransport``, and the "cache hit sends nothing" rule is proved by counting requests.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage, QImageWriter

from gui import block_icons
from launcher.meta import block_textures as bt
from launcher.meta import loader_logos

ROOT = Path(__file__).resolve().parents[1]

#: A tiny valid SVG; QtSvg renders the red square into a transparent PNG.
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
    b'<rect width="40" height="40" fill="#ff0000"/></svg>'
)

EXPECTED_URLS = {
    "fabric": "https://fabricmc.net/assets/logo.png",
    "forge": "https://files.minecraftforge.net/static/images/apple-touch-icon.png",
    "neoforge": "https://api.github.com/repos/neoforged/.github/contents/art/neoforged.ico",
    "quilt": "https://quiltmc.org/assets/img/logo.svg",
}


def _image_bytes(fmt: str, colour: str = "#cc00cc", edge: int = 64) -> bytes:
    """A solid picture in ``fmt`` through Qt's writer (the same plugin the launcher uses to read)."""
    if fmt.lower().encode() not in QImageWriter.supportedImageFormats():
        pytest.skip(f"Qt's {fmt} image plugin is not installed")
    image = QImage(edge, edge, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt)
    return bytes(buffer.data())


def _png(colour: str = "#cc00cc", edge: int = 64) -> bytes:
    return _image_bytes("PNG", colour, edge)


def _write_logo(
    root: Path, loader: str, data: bytes | None = None, *, age: float = 0.0
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{loader}.png"
    path.write_bytes(data if data is not None else _png())
    if age:
        stamp = time.time() - age
        import os

        os.utime(path, (stamp, stamp))
    return path


def _response(url: str, content: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("GET", url))


class _FakeClient:
    """Serves canned responses by URL (404 otherwise) and records every request."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[dict] = []
        self.closed = False

    def get(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.get(url) or httpx.Response(
            404, request=httpx.Request("GET", url)
        )

    def close(self) -> None:
        self.closed = True


def _painted(pixmap) -> int:
    image = pixmap.toImage()
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )


@pytest.fixture(autouse=True)
def _clear_icon_cache():
    block_icons.clear_cache()
    try:
        yield
    finally:
        block_icons.clear_cache()


# ---------------------------------------------------------------------------
# 1. the hot-link URL table
# ---------------------------------------------------------------------------


def test_network_sources_are_the_four_official_image_files():
    """One direct image URL per loader, on the project's own host (never a wiki or HTML page)."""
    assert set(loader_logos.NETWORK_SOURCES) == set(loader_logos.LOADERS)
    assert loader_logos.NETWORK_SOURCES == EXPECTED_URLS
    hosts = {
        "fabric": "fabricmc.net",
        "forge": "files.minecraftforge.net",
        "neoforge": "api.github.com",
        "quilt": "quiltmc.org",
    }
    for loader, url in EXPECTED_URLS.items():
        assert url.startswith("https://"), loader
        assert hosts[loader] in url, loader
        assert Path(url).suffix.lower() in (".png", ".ico", ".svg"), loader
    # GitHub answers JSON unless the raw media type is requested; the header is part of the contract.
    assert loader_logos.SOURCE_HEADERS["neoforge"] == {
        "Accept": "application/vnd.github.raw"
    }


def test_the_repository_contains_no_loader_logo_file():
    """A logo is fetched at runtime; no image may be committed (`git ls-files` proves it)."""
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    tracked = [name for name in completed.stdout.decode("utf-8").split("\0") if name]
    image_suffixes = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".gif")
    logoish = [
        name
        for name in tracked
        if "logo" in name.lower() and name.lower().endswith(image_suffixes)
    ]
    assert logoish == [], f"loader artwork must not be committed: {logoish}"
    assert not [name for name in tracked if "loader-logos" in name.lower()]


# ---------------------------------------------------------------------------
# 2. the cache: path, age, and a hit that sends no request
# ---------------------------------------------------------------------------


def test_cache_path_is_loader_png_under_the_loader_logos_folder(ws_tmp):
    data = ws_tmp / "data"
    path = loader_logos.store_logo_png("fabric", _png(), data)
    assert path == data / "cache" / "loader-logos" / "fabric.png"
    assert path.is_file()
    assert loader_logos.logo_cache_root(data) == data / "cache" / "loader-logos"
    assert loader_logos.find_cached_logo("fabric", cache_dir=path.parent) == path


def test_a_fresh_cache_hit_is_never_requested(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    for loader in loader_logos.LOADERS:
        _write_logo(root, loader)
    client = _FakeClient({})
    assert loader_logos.prefetch_missing_logos(ws_tmp / "data", client=client) == 0
    assert client.calls == []
    assert loader_logos.missing_logos(ws_tmp / "data") == []


def test_a_logo_older_than_thirty_days_is_fetched_again(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    for loader in loader_logos.LOADERS:
        _write_logo(root, loader, age=loader_logos.CACHE_MAX_AGE + 60)
    client = _FakeClient(
        {url: _response(url, _png()) for url in loader_logos.NETWORK_SOURCES.values()}
    )
    assert loader_logos.prefetch_missing_logos(ws_tmp / "data", client=client) == 4
    assert [call["url"] for call in client.calls] == [
        loader_logos.NETWORK_SOURCES[loader] for loader in loader_logos.LOADERS
    ]
    assert loader_logos.missing_logos(ws_tmp / "data") == []


def test_only_the_missing_loader_is_requested(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    for loader in ("fabric", "forge", "quilt"):
        _write_logo(root, loader)
    client = _FakeClient(
        {loader_logos.NETWORK_SOURCES["neoforge"]: _response(
            loader_logos.NETWORK_SOURCES["neoforge"], _png()
        )}
    )
    assert loader_logos.prefetch_missing_logos(ws_tmp / "data", client=client) == 1
    assert [call["url"] for call in client.calls] == [
        loader_logos.NETWORK_SOURCES["neoforge"]
    ]


def test_the_neoforge_request_asks_github_for_the_raw_file(ws_tmp):
    url = loader_logos.NETWORK_SOURCES["neoforge"]
    client = _FakeClient({url: _response(url, _png())})
    assert loader_logos.network_logo_png("neoforge", client=client) is not None
    call = client.calls[0]
    assert call["headers"]["User-Agent"] == loader_logos.NETWORK_USER_AGENT
    assert call["headers"]["Accept"] == "application/vnd.github.raw"
    assert call["timeout"] == loader_logos.NETWORK_TIMEOUT


# ---------------------------------------------------------------------------
# 3. response validation and format conversion
# ---------------------------------------------------------------------------


def test_an_html_page_is_refused_and_leaves_no_file(ws_tmp):
    html = b"<!doctype html><html><body>not an image</body></html>"
    client = _FakeClient(
        {url: _response(url, html) for url in loader_logos.NETWORK_SOURCES.values()}
    )
    assert loader_logos.prefetch_missing_logos(ws_tmp / "data", client=client) == 0
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    assert not root.exists() or not list(root.iterdir())


def test_a_truncated_png_is_refused():
    truncated = loader_logos.PNG_SIGNATURE + b"only the header"
    assert loader_logos._image_bytes_as_png(truncated) is None
    assert loader_logos._image_bytes_as_png(b"not an image") is None


def test_a_webp_source_is_converted_to_png_before_it_is_cached(ws_tmp):
    webp = _image_bytes("WEBP", "#00aaff", 48)
    client = _FakeClient(
        {loader_logos.NETWORK_SOURCES["fabric"]: _response(
            loader_logos.NETWORK_SOURCES["fabric"], webp
        )}
    )
    assert loader_logos.prefetch_missing_logos(ws_tmp / "data", client=client) == 1
    stored = loader_logos.logo_cache_root(ws_tmp / "data") / "fabric.png"
    data = stored.read_bytes()
    assert data.startswith(loader_logos.PNG_SIGNATURE)
    image = QImage(str(stored))
    assert not image.isNull()
    assert (image.width(), image.height()) == (48, 48)


def test_an_ico_source_is_decoded_and_stored_as_png(ws_tmp):
    ico = _image_bytes("ICO", "#22cc55", 64)
    assert ico.startswith(loader_logos._ICO_SIGNATURE)
    png = loader_logos._image_bytes_as_png(ico)
    assert png is not None and png.startswith(loader_logos.PNG_SIGNATURE)
    assert not QImage.fromData(png).isNull()


def test_an_svg_source_is_rasterised_when_qtsvg_is_available():
    pytest.importorskip("PySide6.QtSvg")
    png = loader_logos._svg_as_png(SVG)
    assert png is not None
    assert png.startswith(loader_logos.PNG_SIGNATURE)
    image = QImage.fromData(png)
    assert not image.isNull()
    assert (image.width(), image.height()) == (
        loader_logos.SVG_RENDER_EDGE,
        loader_logos.SVG_RENDER_EDGE,
    )


def test_an_svg_is_skipped_when_qtsvg_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6.QtSvg", None)
    assert loader_logos._svg_as_png(SVG) is None
    assert loader_logos._image_bytes_as_png(SVG) is None


def test_an_invalid_svg_is_refused():
    assert loader_logos._image_bytes_as_png(b"<svg>not really</svg>") is None


def test_store_refuses_unknown_loaders_non_png_bytes_and_paths(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    assert loader_logos.store_logo_png("../escape", _png(), ws_tmp / "data") is None
    assert not (ws_tmp / "escape.png").exists()
    assert loader_logos.store_logo_png("fabric", b"<html>", ws_tmp / "data") is None
    assert not root.exists() or not list(root.iterdir())


# ---------------------------------------------------------------------------
# 4. the GUI: official logo when cached, the drawn glyph otherwise
# ---------------------------------------------------------------------------


def test_a_cached_logo_is_painted_as_the_official_picture(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    _write_logo(root, "fabric", _png("#cc00cc"))
    pixmap = block_icons.loader_pixmap("fabric", "#2f9e6f", 22, logo_cache_dir=root)
    assert not pixmap.isNull()
    image = pixmap.toImage()
    assert image.pixelColor(11, 11).name() == "#cc00cc"


def test_automatic_and_rule_icons_read_the_same_logo(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    _write_logo(root, "fabric", _png("#cc00cc"))
    texture_root = bt.texture_cache_root(ws_tmp / "data")

    automatic = block_icons.automatic_icon_pixmap(
        "fabric-loader-0.19.3-1.21.11", 22, cache_root=texture_root
    )
    assert automatic.toImage().pixelColor(11, 11).name() == "#cc00cc"

    rule = block_icons.rule_icon_pixmap("fabric", 48, cache_root=texture_root)
    centre = rule.toImage().pixelColor(24, 24)
    assert centre.name() == "#cc00cc"
    assert _painted(rule) > 0


def test_no_fresh_logo_falls_back_to_the_drawn_glyph(ws_tmp):
    empty = loader_logos.logo_cache_root(ws_tmp / "data")
    pixmap = block_icons.loader_pixmap("forge", "#2f9e6f", 22, logo_cache_dir=empty)
    assert not pixmap.isNull()
    assert _painted(pixmap) > 0
    assert pixmap.toImage().pixelColor(11, 11).name() != "#cc00cc"


def test_an_expired_logo_falls_back_to_the_drawn_glyph(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    _write_logo(root, "fabric", _png("#cc00cc"), age=loader_logos.CACHE_MAX_AGE + 60)
    assert loader_logos.find_cached_logo("fabric", cache_dir=root) is None
    pixmap = block_icons.loader_pixmap("fabric", "#2f9e6f", 22, logo_cache_dir=root)
    assert pixmap.toImage().pixelColor(11, 11).name() != "#cc00cc"


def test_painting_never_starts_a_request(monkeypatch, ws_tmp):
    def explode(*_args, **_kwargs):
        raise AssertionError("loader_pixmap must never request a logo")

    monkeypatch.setattr(loader_logos, "network_logo_png", explode)
    pixmap = block_icons.loader_pixmap(
        "quilt", "#2f9e6f", 22, logo_cache_dir=loader_logos.logo_cache_root(ws_tmp / "data")
    )
    assert not pixmap.isNull()


# ---------------------------------------------------------------------------
# 5. startup: the same moment as the block-texture prefetch, in a daemon thread
# ---------------------------------------------------------------------------


def test_start_prefetch_uses_a_daemon_thread(ws_tmp, monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(
        loader_logos,
        "prefetch_missing_logos",
        lambda launcher_dir, **_kw: calls.append(launcher_dir) or 0,
    )
    thread = loader_logos.start_prefetch(ws_tmp / "data")
    assert thread is not None and thread.daemon
    thread.join(timeout=5)
    assert calls == [ws_tmp / "data"]


def test_a_complete_cache_starts_no_thread(ws_tmp):
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    for loader in loader_logos.LOADERS:
        _write_logo(root, loader)
    assert loader_logos.start_prefetch(ws_tmp / "data") is None


def test_block_texture_start_prefetch_starts_the_logo_prefetch(ws_tmp, monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(
        bt.loader_logos,
        "start_prefetch",
        lambda launcher_dir, **_kw: calls.append(Path(launcher_dir)),
    )
    monkeypatch.setattr(bt, "prefetch_missing_textures", lambda *_a, **_k: 0)
    thread = bt.start_prefetch(ws_tmp / "data")
    if thread is not None:
        thread.join(timeout=5)
    assert calls == [ws_tmp / "data"]


def test_a_network_failure_is_silent_and_leaves_the_cache_empty(ws_tmp):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert loader_logos.prefetch_missing_logos(ws_tmp / "data", client=client) == 0
    finally:
        client.close()
    root = loader_logos.logo_cache_root(ws_tmp / "data")
    assert not root.exists() or not list(root.iterdir())
