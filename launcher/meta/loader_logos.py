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

"""Official mod-loader logos: the URL table, the user's 30-day cache, and the background prefetch.

The launcher ships no loader artwork. Every logo is fetched at runtime from the project that owns it,
stored as a PNG under ``<launcher data dir>/cache/loader-logos/<loader>.png`` and reused for
:data:`CACHE_MAX_AGE` seconds (30 days). The interface asks this module for a cached path only; when
the cache is cold, expired or unreadable it paints its own Lucide glyph instead, so an offline start
never waits for a request and never breaks an icon.

Sources, all verified as direct image responses (not an HTML page):

* ``fabric`` - fabricmc.net's own header logo (the spool), PNG with transparency;
* ``forge`` - files.minecraftforge.net's 180px app icon (the anvil), PNG;
* ``quilt`` - quiltmc.org's logo, SVG; QtSvg renders it to a 256px transparent PNG when available,
  and the loader falls back to the drawn glyph when the SVG plugin is not installed;
* ``neoforge`` - neoforged.net did not answer (its DNS currently points at a parking address), so the
  official NeoForged art repository on GitHub is used: ``neoforged/.github/art/neoforged.ico``,
  fetched through the GitHub contents API with ``Accept: application/vnd.github.raw``. The ICO is
  decoded to PNG before it is stored. The same repository calls itself the official art of NeoForged.

The fetch runs in a daemon thread next to the block-texture prefetch (``block_textures.start_prefetch``
starts both after the window is shown). Every failure - DNS, timeout, HTTP error, an HTML error page,
a truncated image, a missing Qt image plugin - is swallowed and logged at debug level; the next start
tries again.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import httpx

from launcher import paths
from launcher.meta.manifest import _new_client

LOGGER = logging.getLogger(__name__)

#: Cache layout under the launcher data directory.
CACHE_DIRNAME = "loader-logos"
CACHE_MAX_AGE = 30 * 24 * 60 * 60

#: The four loaders an instance card can name.
LOADERS: tuple[str, ...] = ("fabric", "forge", "neoforge", "quilt")

#: Suffixes :func:`find_cached_logo` accepts. The prefetch always stores ``<loader>.png``; the other
#: entries only keep a cache written by an older/hand-edited build readable.
CACHE_SUFFIXES: tuple[str, ...] = ("png", "webp", "jpg", "jpeg", "svg", "ico", "gif")

#: Direct image files, never a wiki or a download page. See the module docstring for the origin.
NETWORK_SOURCES: dict[str, str] = {
    "fabric": "https://fabricmc.net/assets/logo.png",
    "forge": "https://files.minecraftforge.net/static/images/apple-touch-icon.png",
    "neoforge": (
        "https://api.github.com/repos/neoforged/.github/contents/art/neoforged.ico"
    ),
    "quilt": "https://quiltmc.org/assets/img/logo.svg",
}

#: Per-source request headers. GitHub's contents API returns JSON unless the raw media type is asked
#: for, so this one header is what makes the URL an image response instead of a metadata document.
SOURCE_HEADERS: dict[str, dict[str, str]] = {
    "neoforge": {"Accept": "application/vnd.github.raw"},
}

#: A browser User-Agent: the Minecraft Forge host and GitHub both answer a normal browser string.
NETWORK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
NETWORK_TIMEOUT = 10.0

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"IEND\xaeB`\x82"
_ICO_SIGNATURE = b"\x00\x00\x01\x00"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")

#: Edge of the transparent PNG an SVG is rasterised to. Large enough for a 48px tile at 4x DPR and
#: small enough that the prefetch thread never allocates a poster-sized canvas.
SVG_RENDER_EDGE = 256


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def logo_cache_root(launcher_dir: Path | None = None) -> Path:
    """``<launcher data dir>/cache/loader-logos`` (the only place a fetched logo is written)."""
    base = Path(launcher_dir) if launcher_dir is not None else paths.launcher_dir()
    return base / "cache" / CACHE_DIRNAME


def find_cached_logo(
    loader: str,
    *,
    cache_dir: Path | None = None,
    now: float | None = None,
) -> Path | None:
    """The cached logo for ``loader`` when it exists and is younger than 30 days, else ``None``.

    This is a read-only check of a few ``stat`` calls; the GUI calls it while building a card and never
    starts a request. ``cache_dir`` is the ``loader-logos`` directory itself (the caller already has a
    launcher directory) and defaults to the current user's cache.
    """
    if loader not in LOADERS:
        return None
    root = Path(cache_dir) if cache_dir is not None else logo_cache_root()
    deadline = (time.time() if now is None else now) - CACHE_MAX_AGE
    for suffix in CACHE_SUFFIXES:
        candidate = root / f"{loader}.{suffix}"
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if stat.st_size > 0 and stat.st_mtime >= deadline:
            return candidate
    return None


def missing_logos(launcher_dir: Path, *, now: float | None = None) -> list[str]:
    """The loaders with no fresh cache entry (empty when all four are cached)."""
    root = logo_cache_root(launcher_dir)
    return [
        loader for loader in LOADERS if find_cached_logo(loader, cache_dir=root, now=now) is None
    ]


def store_logo_png(loader: str, data: bytes, launcher_dir: Path | None = None) -> Path | None:
    """Write verified PNG bytes to ``<loader>.png`` atomically; ``None`` when it was refused.

    Only a complete PNG is accepted. The cache may live on a volume that is read-only or full, so a
    failed write is reported as ``None`` rather than raised: the drawn glyph already covers the icon.
    """
    if loader not in LOADERS or not data:
        return None
    if not data.startswith(PNG_SIGNATURE) or not data.endswith(_PNG_IEND):
        return None
    destination = logo_cache_root(launcher_dir) / f"{loader}.png"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        return None
    return destination if destination.is_file() else None


# ---------------------------------------------------------------------------
# The network level
# ---------------------------------------------------------------------------


def network_logo_png(
    loader: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = NETWORK_TIMEOUT,
) -> bytes | None:
    """One loader's logo as PNG bytes, or ``None`` when it cannot be had.

    The URL is a direct image (see :data:`NETWORK_SOURCES`), the request carries a browser
    User-Agent and is bounded by ``timeout``. PNG passes through after an IEND check; SVG, WebP, ICO,
    JPEG and GIF are decoded through Qt and re-encoded as PNG (SVG requires QtSvg). Every failure -
    DNS, timeout, 404, an HTML page, a truncated body, a missing image plugin - returns ``None`` and
    raises nothing. Nothing is written here; the caller stores the bytes.
    """
    url = NETWORK_SOURCES.get(loader)
    if not url:
        return None
    own_client = client is None
    if own_client:
        client = _new_client()
    try:
        headers = {"User-Agent": NETWORK_USER_AGENT, **SOURCE_HEADERS.get(loader, {})}
        response = client.get(
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return _image_bytes_as_png(response.content)
    except (httpx.HTTPError, OSError, ValueError):
        return None
    finally:
        if own_client:
            client.close()


def _image_bytes_as_png(data: bytes) -> bytes | None:
    """Pass a PNG through, convert a Qt-readable image, refuse anything else (an HTML page)."""
    if not data:
        return None
    if data.startswith(PNG_SIGNATURE):
        # A cut-off download still starts with the signature, while a complete PNG ends with IEND.
        return data if data.endswith(_PNG_IEND) else None
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _qt_image_as_png(data)
    if data.startswith((_ICO_SIGNATURE, _JPEG_SIGNATURE, *_GIF_SIGNATURES)):
        return _qt_image_as_png(data)
    if _looks_like_svg(data):
        return _svg_as_png(data)
    return None


def _looks_like_svg(data: bytes) -> bool:
    """An XML/SVG document by its first non-space bytes (a wrong XML still fails in QSvgRenderer)."""
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:64].lower()
    return head.startswith((b"<svg", b"<?xml"))


def _qt_image_as_png(data: bytes) -> bytes | None:
    """Decode an ICO/WebP/JPEG/GIF through Qt and re-encode it as PNG.

    QImage is imported here, not at module scope: PySide6 is the optional GUI extra and the launcher
    core must stay importable without it. A CLI-only install therefore keeps the drawn glyph.
    """
    try:
        from PySide6.QtGui import QImage
    except ImportError:
        return None
    image = QImage.fromData(data)
    if image.isNull():
        return None
    return _encode_png(image)


def _svg_as_png(data: bytes) -> bytes | None:
    """Rasterise an SVG through QtSvg; ``None`` when QtSvg is absent or the document is invalid."""
    try:
        from PySide6.QtCore import QByteArray, Qt
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        return None
    # ``isValid`` accepts an empty ``<svg>`` root; a real logo declares a size or a viewBox. Without
    # one the render is a transparent square, which is not a logo and must not enter the cache.
    if renderer.defaultSize().isEmpty() and renderer.viewBoxF().isEmpty():
        return None
    image = QImage(
        SVG_RENDER_EDGE, SVG_RENDER_EDGE, QImage.Format.Format_ARGB32_Premultiplied
    )
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return _encode_png(image)


def _encode_png(image) -> bytes | None:
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        return None
    png = bytes(buffer.data())
    return png if png.startswith(PNG_SIGNATURE) and png.endswith(_PNG_IEND) else None


# ---------------------------------------------------------------------------
# The background prefetch
# ---------------------------------------------------------------------------


def start_prefetch(launcher_dir: Path, *, now: float | None = None) -> threading.Thread | None:
    """Fetch whatever logo is missing in a daemon thread; ``None`` when the cache is complete.

    Called from ``block_textures.start_prefetch`` after the main window is shown, at the same moment
    as the block-texture prefetch. The cache check is four ``stat`` calls, so an everyday start does
    no thread and no network at all.
    """
    if not missing_logos(launcher_dir, now=now):
        return None
    thread = threading.Thread(
        target=prefetch_missing_logos,
        args=(Path(launcher_dir),),
        kwargs={"now": now},
        name="loader-logo-prefetch",
        daemon=True,
    )
    thread.start()
    return thread


def prefetch_missing_logos(
    launcher_dir: Path,
    *,
    client: httpx.Client | None = None,
    now: float | None = None,
) -> int:
    """Fill the cache; returns how many logos were written. Never raises.

    Only a logo with no fresh cache entry is requested, and one failure does not stop the other three.
    """
    try:
        return _prefetch_missing_logos(Path(launcher_dir), client, now)
    except Exception:  # a background logo fetch must never disturb the launcher
        LOGGER.debug("loader logo prefetch failed; keeping the drawn fallback", exc_info=True)
        return 0


def _prefetch_missing_logos(
    launcher_dir: Path,
    client: httpx.Client | None,
    now: float | None,
) -> int:
    missing = missing_logos(launcher_dir, now=now)
    if not missing:
        return 0
    own_client = client is None
    if own_client:
        client = _new_client()
    written = 0
    try:
        for loader in missing:
            data = network_logo_png(loader, client=client)
            if data is None:
                continue
            if store_logo_png(loader, data, launcher_dir) is None:
                continue
            written += 1
        if written:
            LOGGER.info("cached %d loader logo(s)", written)
        return written
    finally:
        if own_client:
            client.close()
