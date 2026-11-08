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

"""Structural stack analysis (``CrashStackAnalyzer``).

Keyword matching can say "the log mentions a Mixin error"; it cannot say *which*
frame failed or *which* mod owns it. This module parses the structure instead:

* the ``Caused by:`` chain (any depth) and ``Suppressed:`` exceptions,
* ``at package.Class.method(File.java:123)`` frames, including the
  ``knot//`` / ``java.base/`` prefixes Fabric and the JDK add,
* Fabric/Forge mixin failures (``Mixin apply failed``,
  ``MixinTransformerError``, ``InvalidInjectionException``, config names and the
  ``from mod <id>`` clause),
* the mod names a message mentions (``provided by 'notebot'``,
  ``Suspected Mods:``), which is how a crash with no mod frame still names a
  suspect.

The output is a structured :class:`CrashStack`; every frame keeps the file and
line it came from, so a finding can show ``crash-….txt:21`` as evidence. The
analyzer never executes anything and never fetches a mapping — obfuscated
Minecraft class names are reported by their raw name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from launcher.diagnostics.modindex import CrashModIndex, JarMod
from launcher.diagnostics.prepare import (
    KIND_CRASH_REPORT,
    KIND_DEBUG,
    KIND_GAME,
    KIND_HS_ERR,
    KIND_STACK,
    PreparedLog,
)

_LOGGER = logging.getLogger(__name__)

#: Prefixes of frames that belong to the platform, not to a mod.
LOADER_PREFIXES = (
    "net.fabricmc",
    "org.quiltmc",
    "net.minecraftforge",
    "net.neoforged",
    "cpw.mods",
    "org.spongepowered",
    "com.electronwill",
    "org.objectweb",
)
VANILLA_PREFIXES = ("net.minecraft", "com.mojang")
JAVA_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "com.sun.")

_EXCEPTION_RE = re.compile(
    r"(?P<kind>(?:[a-z_][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable))"
    r"(?::\s*(?P<message>.*))?"
)
_CAUSED_RE = re.compile(r"^\s*Caused by:\s*(?P<rest>.+)$")
_SUPPRESSED_RE = re.compile(r"^\s*Suppressed:\s*(?P<rest>.+)$")
_FRAME_RE = re.compile(r"^\s*at\s+(?P<location>.+?)\s*$")
_LOCATION_RE = re.compile(r"^(?P<target>.+?)\((?P<source>[^()]*)\)$")
_MORE_RE = re.compile(r"^\s*\.\.\.\s+\d+\s+more\s*$")

_MIXIN_MARKERS = (
    "Mixin apply failed",
    "MixinTransformerError",
    "MixinApplyError",
    "InvalidMixinException",
    "InvalidInjectionException",
    "Critical injection failure",
    "Mixin transformation of",
    "mixin.injection.throwables",
)
_MIXIN_MOD_RE = re.compile(r"\bfrom mod ([A-Za-z0-9_.-]+)")
_MIXIN_CONFIG_RE = re.compile(r"\bconfig \[([^\]]+)\]")
_MIXIN_APPLY_RE = re.compile(r"Mixin \[([^\]]+)\]")
_PROVIDED_BY_RE = re.compile(r"provided by '([^']+)'")
_SUSPECTED_RE = re.compile(r"Suspected Mods?:\s*(.+)")
_MOD_FILE_RE = re.compile(r"Mod File:\s*(.+)")
_ENTRYPOINT_RE = re.compile(r"entrypoint stage '([^']+)'.*provided by '([^']+)'")

#: Log kinds whose lines are worth parsing as a Java stack.
STACK_KINDS = frozenset({KIND_GAME, KIND_CRASH_REPORT, KIND_DEBUG, KIND_HS_ERR})


@dataclass(frozen=True)
class StackFrame:
    """One ``at …`` frame, with the mod it belongs to (when known)."""

    raw: str
    class_name: str
    method: str
    source: str
    line: int | None
    owner: str  # mod / vanilla / loader / java / unknown
    mod_id: str
    mod_file: str
    log_name: str
    log_line: int
    chain: int  # 0 = outermost exception, 1 = first Caused by, …

    @property
    def is_mod(self) -> bool:
        return self.owner == "mod"

    @property
    def location(self) -> str:
        return f"{self.class_name}.{self.method}"

    def describe(self) -> str:
        return f"at {self.location}({self.source}{':' + str(self.line) if self.line else ''})"


@dataclass(frozen=True)
class CrashException:
    """One exception header of the chain."""

    kind: str
    message: str
    chain: int
    suppressed: bool
    log_name: str
    log_line: int

    @property
    def is_error(self) -> bool:
        return self.kind.endswith(("Error", "Throwable"))


@dataclass(frozen=True)
class MixinFailure:
    """One mixin failure line, with the mods / config it names."""

    text: str
    mods: tuple[str, ...]
    config: str
    mixin: str
    log_name: str
    log_line: int


@dataclass(frozen=True)
class CrashStack:
    """The structured result of parsing every matchable log of one crash."""

    exceptions: tuple[CrashException, ...] = ()
    frames: tuple[StackFrame, ...] = ()
    mixins: tuple[MixinFailure, ...] = ()
    mentioned_mods: tuple[str, ...] = ()
    mentions: tuple[tuple[str, str, int], ...] = ()  # (mod name, log name, line)
    suspect_frame: StackFrame | None = None
    suspect_mod: JarMod | None = None
    suspect_id: str = ""
    suspect_name: str = ""
    suspect_file: str = ""
    log_names: tuple[str, ...] = field(default=())

    @property
    def root_exception(self) -> CrashException | None:
        """The deepest ``Caused by`` (the actual root cause), else the first exception."""
        if not self.exceptions:
            return None
        non_suppressed = [item for item in self.exceptions if not item.suppressed]
        return (non_suppressed or list(self.exceptions))[-1]

    @property
    def top_exception(self) -> CrashException | None:
        return self.exceptions[0] if self.exceptions else None

    @property
    def mod_frames(self) -> tuple[StackFrame, ...]:
        return tuple(frame for frame in self.frames if frame.is_mod)

    @property
    def has_mixin_failure(self) -> bool:
        return bool(self.mixins)

    def frames_for(self, chain: int) -> tuple[StackFrame, ...]:
        return tuple(frame for frame in self.frames if frame.chain == chain)

    def as_logs(self) -> tuple[PreparedLog, ...]:
        """Per source log, the parsed structure as a virtual ``stack`` log."""
        from launcher.diagnostics.logset import virtual_log

        grouped: dict[str, list[tuple[int, str]]] = {}

        def add(log_name: str, log_line: int, text: str) -> None:
            grouped.setdefault(log_name, []).append((log_line, text))

        for exc in self.exceptions:
            label = "suppressed" if exc.suppressed else f"exception[{exc.chain}]"
            add(exc.log_name, exc.log_line, f"{label}: {exc.kind}")
            if exc.message:
                add(exc.log_name, exc.log_line, f"exception-message: {exc.message}"[:300])
        for frame in self.frames:
            flags = [f"owner={frame.owner}"]
            if frame.mod_id:
                flags.append(f"mod={frame.mod_id}")
            if frame.mod_file:
                flags.append(f"file={frame.mod_file}")
            add(
                frame.log_name,
                frame.log_line,
                f"frame[{frame.chain}]: {frame.describe()} [{' '.join(flags)}]",
            )
        for mixin in self.mixins:
            flags = []
            if mixin.mods:
                flags.append("mods=" + ",".join(mixin.mods))
            if mixin.config:
                flags.append("config=" + mixin.config)
            add(
                mixin.log_name,
                mixin.log_line,
                f"mixin-failure: {mixin.text[:400]} [{' '.join(flags)}]",
            )
        for mod, log_name, log_line in self.mentions:
            add(log_name, log_line, f"mentioned-mod: {mod}")

        logs = []
        for name, items in grouped.items():
            seen: dict[int, list[str]] = {}
            for number, text in items:
                bucket = seen.setdefault(number, [])
                if text not in bucket:
                    bucket.append(text)
            lines = [text for number in seen for text in seen[number]]
            numbers = [number for number in seen for _text in seen[number]]
            logs.append(
                virtual_log(
                    name=name,
                    kind=KIND_STACK,
                    lines=lines,
                    numbers=numbers,
                )
            )
        return tuple(logs)

    def summary_lines(self) -> tuple[str, ...]:
        """A short human-readable chain (tests and debugging)."""
        lines = []
        for exc in self.exceptions:
            prefix = "Suppressed" if exc.suppressed else f"Caused by {exc.chain}"
            lines.append(f"{prefix}: {exc.kind}: {exc.message}".rstrip(": "))
        if self.suspect_frame is not None:
            lines.append(f"suspect frame: {self.suspect_frame.describe()}")
        return tuple(lines)


class CrashStackAnalyzer:
    """Parse the stack structure of a :class:`CrashLogSet`."""

    def __init__(self, mod_index: CrashModIndex | None = None) -> None:
        self.mod_index = mod_index or CrashModIndex.empty()

    def analyze(self, logs) -> CrashStack:
        """Parse every real stack-bearing log of ``logs`` (newest report first)."""
        source_logs = [log for log in logs if log.kind in STACK_KINDS and log.is_matchable]
        source_logs.sort(key=self._source_order)
        exceptions: list[CrashException] = []
        frames: list[StackFrame] = []
        mixins: list[MixinFailure] = []
        mentioned: list[str] = []
        mention_origins: list[tuple[str, str, int]] = []
        for log in source_logs:
            parsed = _parse_log(log, self.mod_index)
            exceptions.extend(parsed[0])
            frames.extend(parsed[1])
            mixins.extend(parsed[2])
            mentioned.extend(parsed[3])
            mention_origins.extend(parsed[4])

        suspect_frame = _pick_suspect_frame(frames)
        suspect = _resolve_suspect(suspect_frame, mixins, mentioned, self.mod_index)
        stack = CrashStack(
            exceptions=tuple(exceptions),
            frames=tuple(frames),
            mixins=tuple(mixins),
            mentioned_mods=tuple(dict.fromkeys(mentioned)),
            mentions=tuple(dict.fromkeys(mention_origins)),
            suspect_frame=suspect_frame,
            suspect_mod=suspect[0],
            suspect_id=suspect[1],
            suspect_name=suspect[2],
            suspect_file=suspect[3],
            log_names=tuple(log.name for log in source_logs),
        )
        _LOGGER.debug(
            "diagnostics: stack %s exceptions=%d frames=%d mod-frames=%d mixins=%d suspect=%s",
            ", ".join(stack.log_names) or "none",
            len(stack.exceptions),
            len(stack.frames),
            len(stack.mod_frames),
            len(stack.mixins),
            stack.suspect_id or "unknown",
        )
        return stack

    @staticmethod
    def _source_order(log: PreparedLog) -> tuple[int, float]:
        priority = {
            KIND_CRASH_REPORT: 0,
            KIND_GAME: 1,
            KIND_DEBUG: 2,
            KIND_HS_ERR: 3,
        }
        return priority.get(log.kind, 9), -log.mtime


def _parse_log(
    log: PreparedLog, mod_index: CrashModIndex
) -> tuple[
    list[CrashException],
    list[StackFrame],
    list[MixinFailure],
    list[str],
    list[tuple[str, str, int]],
]:
    exceptions: list[CrashException] = []
    frames: list[StackFrame] = []
    mixins: list[MixinFailure] = []
    mentioned: list[str] = []
    origins: list[tuple[str, str, int]] = []
    chain = 0
    current = 0
    for index, text in enumerate(log.lines):
        number = log.number_at(index) or 0
        frame_match = _FRAME_RE.match(text)
        if frame_match:
            if _MORE_RE.match(text):
                continue
            frame = _parse_frame(
                frame_match.group("location"),
                text,
                log.name,
                number,
                current,
                mod_index,
            )
            if frame is not None:
                frames.append(frame)
            continue

        caused = _CAUSED_RE.match(text)
        suppressed = _SUPPRESSED_RE.match(text)
        prefix = ""
        rest = text
        if caused:
            chain += 1
            current = chain
            prefix = "Caused by: "
            rest = caused.group("rest")
        elif suppressed:
            current = chain
            prefix = "Suppressed: "
            rest = suppressed.group("rest")

        exc = _parse_exception(rest, prefix, log.name, number, current)
        if exc is not None:
            exceptions.append(exc)
            if not suppressed:
                chain = max(chain, current)
            names = _mentioned_in(exc.message, exc.kind)
            mentioned.extend(names)
            origins.extend((name, log.name, number) for name in names)
            if _is_mixin_text(text, exc.kind):
                mixin = _parse_mixin(text, log.name, number)
                mixins.append(mixin)
                mentioned.extend(mixin.mods)
                origins.extend((name, log.name, number) for name in mixin.mods)
            continue

        if any(marker in text for marker in _MIXIN_MARKERS):
            mixin = _parse_mixin(text, log.name, number)
            mixins.append(mixin)
            mentioned.extend(mixin.mods)
            origins.extend((name, log.name, number) for name in mixin.mods)
            names = _mentioned_in(text, "")
            mentioned.extend(names)
            origins.extend((name, log.name, number) for name in names)

        for match in (_PROVIDED_BY_RE, _SUSPECTED_RE, _MOD_FILE_RE):
            for hit in match.finditer(text):
                names = _split_names(hit.group(1))
                mentioned.extend(names)
                origins.extend((name, log.name, number) for name in names)
    return exceptions, frames, mixins, mentioned, origins


def _parse_exception(
    text: str, prefix: str, log_name: str, log_line: int, chain: int
) -> CrashException | None:
    match = _EXCEPTION_RE.search(text)
    if match is None:
        return None
    start = match.start()
    # The regex must cover the header itself, not a class name inside free text.
    head = text[:start].strip()
    if (
        head
        and not head.startswith(("Exception in thread", "Caused by:", "Suppressed:"))
        and not prefix
    ):
        return None
    kind = match.group("kind")
    message = (match.group("message") or "").strip()
    return CrashException(
        kind=kind,
        message=message,
        chain=chain,
        suppressed=prefix.startswith("Suppressed"),
        log_name=log_name,
        log_line=log_line,
    )


def _parse_frame(
    location: str,
    raw: str,
    log_name: str,
    log_line: int,
    chain: int,
    mod_index: CrashModIndex,
) -> StackFrame | None:
    cleaned = _strip_module_prefix(location.strip())
    match = _LOCATION_RE.match(cleaned)
    if match is None:
        return None
    target = match.group("target").strip()
    source_text = match.group("source").strip()
    if target.startswith(("...", "Unknown")) or not target:
        return None
    class_name, _, method = target.rpartition(".")
    if not class_name or not method:
        return None
    source, line = _split_source(source_text)
    owner, mod_id, mod_file = _classify(class_name, mod_index)
    return StackFrame(
        raw=raw,
        class_name=class_name,
        method=method,
        source=source,
        line=line,
        owner=owner,
        mod_id=mod_id,
        mod_file=mod_file,
        log_name=log_name,
        log_line=log_line,
        chain=chain,
    )


def _strip_module_prefix(location: str) -> str:
    """Drop Fabric/JPMS prefixes: ``knot//net.minecraft…``, ``java.base/java.lang…``."""
    if "//" in location:
        return location.rsplit("//", 1)[1]
    if "/" in location and not location.startswith("/"):
        head, _, tail = location.partition("/")
        if "." in head or head in ("app", "knot", "boot"):
            return tail
    return location


def _split_source(source_text: str) -> tuple[str, int | None]:
    source, _, line_text = source_text.rpartition(":")
    if line_text.isdigit():
        return source or source_text, int(line_text)
    return source_text, None


def _classify(class_name: str, mod_index: CrashModIndex) -> tuple[str, str, str]:
    mod = mod_index.find_by_class(class_name)
    if mod is not None:
        return "mod", mod.mod_id, mod.file
    if class_name.startswith(LOADER_PREFIXES):
        return "loader", "", ""
    if class_name.startswith(VANILLA_PREFIXES):
        return "vanilla", "", ""
    if class_name.startswith(JAVA_PREFIXES):
        return "java", "", ""
    return "unknown", "", ""


def _is_mixin_text(text: str, kind: str) -> bool:
    if "mixin" in kind.lower():
        return True
    return any(marker in text for marker in _MIXIN_MARKERS)


def _parse_mixin(text: str, log_name: str, log_line: int) -> MixinFailure:
    mods = tuple(
        name
        for name in dict.fromkeys(_MIXIN_MOD_RE.findall(text))
        if _looks_like_mod(name)
    )
    config = ""
    config_match = _MIXIN_CONFIG_RE.search(text)
    if config_match:
        config = config_match.group(1)
    mixin = ""
    apply_match = _MIXIN_APPLY_RE.search(text)
    if apply_match:
        mixin = apply_match.group(1)
    return MixinFailure(
        text=text.strip(),
        mods=mods,
        config=config,
        mixin=mixin,
        log_name=log_name,
        log_line=log_line,
    )


def _mentioned_in(text: str, kind: str) -> list[str]:
    names: list[str] = []
    for match in _PROVIDED_BY_RE.finditer(text):
        names.extend(_split_names(match.group(1)))
    for match in _MIXIN_MOD_RE.finditer(text):
        names.extend(_split_names(match.group(1)))
    for match in _ENTRYPOINT_RE.finditer(text):
        names.extend(_split_names(match.group(2)))
    if kind.endswith(("MixinApplyError", "MixinTransformerError")):
        for match in _MIXIN_CONFIG_RE.finditer(text):
            names.extend(_split_names(match.group(1).split(":")[0]))
    return [name for name in names if _looks_like_mod(name)]


def _split_names(text: str) -> list[str]:
    return [
        part.strip().strip("[]{}'\"")
        for part in re.split(r"[,;]", text)
        if part.strip().strip("[]{}'\"")
    ]


def _looks_like_mod(name: str) -> bool:
    lower = name.strip().lower()
    if not lower or len(lower) > 64:
        return False
    if lower.endswith((".json", ".toml", ".jar", ".class", "-", "_")):
        return False
    if any(char in lower for char in "/\\:[]()'\" "):
        return False
    if lower.count(".") > 1:
        return False
    return lower not in {
        "mod",
        "mods",
        "minecraft",
        "forge",
        "fabric",
        "neoforge",
        "quilt",
        "java",
        "the",
        "and",
    }


def _pick_suspect_frame(frames: list[StackFrame]) -> StackFrame | None:
    """The most likely faulty frame: the first mod frame of the deepest cause."""
    mod_frames = [frame for frame in frames if frame.is_mod]
    if not mod_frames:
        return None
    deepest = max(frame.chain for frame in mod_frames)
    for frame in mod_frames:
        if frame.chain == deepest:
            return frame
    return mod_frames[0]


def _resolve_suspect(
    frame: StackFrame | None,
    mixins: list[MixinFailure],
    mentioned: list[str],
    mod_index: CrashModIndex,
) -> tuple[JarMod | None, str, str, str]:
    """Resolve (jar, id, display name, file name) for the suspect, jar optional.

    A mixin failure names its own mod, so it wins over the frame that merely
    *loaded* the class (the target mod is usually innocent); a plain stack trace
    falls back to its first mod-owned frame.
    """
    for mixin in mixins:
        for mod_id in mixin.mods:
            mod = mod_index.by_id(mod_id) or mod_index.find_by_name(mod_id)
            if mod is not None:
                return mod, mod.mod_id, mod.display, mod.file
            return None, mod_id, mod_id, ""
    if frame is not None and frame.mod_id:
        mod = mod_index.by_id(frame.mod_id) or mod_index.find_by_name(frame.mod_id)
        if mod is not None:
            return mod, mod.mod_id, mod.display, mod.file
        return None, frame.mod_id, frame.mod_id, frame.mod_file
    for name in mentioned:
        mod = mod_index.find_by_name(name) or mod_index.by_id(name)
        if mod is not None:
            return mod, mod.mod_id, mod.display, mod.file
        if name.lower() not in ("unknown",):
            return None, name, name, ""
    return None, "", "", ""


__all__ = [
    "CrashException",
    "CrashStack",
    "CrashStackAnalyzer",
    "MixinFailure",
    "StackFrame",
]
