"""Scenario file parsing and validation (CLAUDE.md § 2, § 8.3)."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Collection
from pathlib import Path

from vidcarousel.errors import InputFileError, ScenarioError
from vidcarousel.models import EntryType, Scenario, ScenarioEntry

# <params><whitespace><filename>; params contain no whitespace, filename is the
# rest of the line (and may contain spaces).
_LINE_RE = re.compile(r"^(?P<params>\S+)[ \t]+(?P<filename>\S.*?)\s*$")


def _parse_float(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_entry_line(
    line: str,
    line_num: int,
    base_dir: Path,
    valid_transitions: Collection[str] | None = None,
) -> ScenarioEntry:
    """Parse one non-comment scenario line into a ScenarioEntry.

    Checks SC05–SC08 (and SC11 when ``valid_transitions`` is supplied).  The
    image file's existence is *not* checked here (SC09 is checked by
    :func:`parse_scenario`).

    A line that cannot be split into ``params`` and ``filename``, or that has
    too many comma-separated fields, is reported as ``VC1005``; a missing
    ``display_time`` is reported as ``VC1006``.
    """
    where = f"line {line_num}"
    match = _LINE_RE.match(line.strip())
    if match is None:
        raise ScenarioError(
            "VC1005", f"{where}: expected 'type,display_time[,xfade_time[,transition]]  filename'"
        )

    fields = match.group("params").split(",")
    if len(fields) > 4:
        raise ScenarioError("VC1005", f"{where}: too many comma-separated fields")

    type_str = fields[0]
    try:
        entry_type = EntryType(type_str)
    except ValueError:
        valid = ", ".join(t.value for t in EntryType)
        raise ScenarioError(
            "VC1005", f"{where}: unknown entry type {type_str!r} (expected one of: {valid})"
        ) from None

    if len(fields) < 2:
        raise ScenarioError("VC1006", f"{where}: missing display_time")
    display_time = _parse_float(fields[1])
    if display_time is None or display_time <= 0:
        raise ScenarioError(
            "VC1006", f"{where}: display_time must be a positive number, got {fields[1]!r}"
        )

    xfade_time = 0.0
    if len(fields) >= 3:
        parsed = _parse_float(fields[2])
        if parsed is None:
            raise ScenarioError("VC1007", f"{where}: xfade_time is not a number: {fields[2]!r}")
        xfade_time = parsed

    if entry_type is EntryType.START and xfade_time > 0:
        raise ScenarioError("VC1008", f"{where}: 'start' entry must not have xfade_time > 0")
    if xfade_time < 0 or xfade_time >= display_time:
        raise ScenarioError(
            "VC1007",
            f"{where}: xfade_time must be >= 0 and < display_time ({display_time:g}), "
            f"got {xfade_time:g}",
        )

    transition: str | None = None
    if len(fields) == 4:
        transition = fields[3]
        if not transition:
            raise ScenarioError("VC1009", f"{where}: empty transition name")
        if valid_transitions is not None and transition not in valid_transitions:
            raise ScenarioError("VC1009", f"{where}: unknown transition {transition!r}")

    return ScenarioEntry(
        entry_type=entry_type,
        display_time=display_time,
        xfade_time=xfade_time,
        transition=transition,
        image_path=base_dir / match.group("filename"),
    )


def validate_ordering(entries: list[ScenarioEntry]) -> None:
    """Enforce the entry-type sequence rules SC01–SC04.

    Required shape: ``start, loop+, [credits], [end]``.
    """
    types = [e.entry_type for e in entries]

    if not types or types[0] is not EntryType.START:
        raise ScenarioError("VC1001", "the first entry must be 'start'")
    if types.count(EntryType.START) != 1:
        raise ScenarioError("VC1001", "exactly one 'start' entry is allowed")

    if EntryType.LOOP not in types:
        raise ScenarioError("VC1002", "at least one 'loop' entry is required after 'start'")

    if types.count(EntryType.CREDITS) > 1:
        raise ScenarioError("VC1003", "at most one 'credits' entry is allowed")
    if EntryType.CREDITS in types:
        credits_idx = types.index(EntryType.CREDITS)
        last_loop_idx = max(i for i, t in enumerate(types) if t is EntryType.LOOP)
        if credits_idx < last_loop_idx:
            raise ScenarioError("VC1003", "'credits' must come after all 'loop' entries")

    if types.count(EntryType.END) > 1:
        raise ScenarioError("VC1004", "at most one 'end' entry is allowed")
    if EntryType.END in types and types[-1] is not EntryType.END:
        raise ScenarioError("VC1004", "'end' must be the last entry")


def _check_image_readable(path: Path) -> None:
    if not path.is_file() or not os.access(path, os.R_OK):
        raise InputFileError("VC2001", f"image file not found or unreadable: {path}")


def parse_scenario(
    path: Path,
    valid_transitions: Collection[str] | None = None,
) -> Scenario:
    """Read, parse and validate a scenario file.

    Image paths are resolved relative to the scenario file's parent directory.
    Rules SC01–SC09 are enforced (SC11 too, when ``valid_transitions`` is
    given); SC10 (dimensions) needs ffprobe and is checked elsewhere.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputFileError(
            "VC2004", f"scenario file not found or unreadable: {path} ({exc})"
        ) from exc

    base_dir = path.parent
    entries: list[ScenarioEntry] = []
    for line_num, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(parse_entry_line(stripped, line_num, base_dir, valid_transitions))

    validate_ordering(entries)
    for entry in entries:
        _check_image_readable(entry.image_path)
    return Scenario(entries=tuple(entries))
