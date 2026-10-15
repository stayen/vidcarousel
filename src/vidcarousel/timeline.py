"""Timeline computation (CLAUDE.md § 4, § 8.4).

Conventions used throughout:

* ``TimelineSlot.timestamp`` is when the slot's image *starts* appearing,
  i.e. the start of its incoming crossfade.
* ``TimelineSlot.display_time`` is the total on-screen presence including that
  incoming crossfade, so a slot ends at ``timestamp + display_time``.
* A slot starts ``xfade_time`` before the previous slot ends.
"""

from __future__ import annotations

import math

from vidcarousel.errors import ScenarioError
from vidcarousel.models import EntryType, Scenario, ScenarioEntry, Timeline, TimelineSlot

# Tolerance for floating-point comparisons of times, in seconds.
EPSILON = 1e-6


def format_timestamp(seconds: float) -> str:
    """Format a non-negative time in seconds as ``H:MM:SS.mmm``."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"timestamp must be a non-negative finite number, got {seconds!r}")
    total_ms = round(seconds * 1000)
    total_s, ms = divmod(total_ms, 1000)
    total_m, s = divmod(total_s, 60)
    h, m = divmod(total_m, 60)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def _append_slot(
    slots: list[TimelineSlot],
    entry: ScenarioEntry,
    display_time: float,
    *,
    loop_iteration: int,
    default_transition: str,
    clamp_xfade: bool = False,
) -> None:
    """Append ``entry`` as a slot that fades in over the end of the previous slot.

    A crossfade longer than the previous slot's display time is an error, unless
    ``clamp_xfade`` is set, in which case it is shortened to fit.
    """
    if abs(display_time - entry.display_time) <= EPSILON:
        display_time = entry.display_time  # snap away float noise

    xfade_time = entry.xfade_time
    if slots:
        prev = slots[-1]
        if xfade_time > prev.display_time + EPSILON:
            if not clamp_xfade:
                raise ScenarioError(
                    "VC1007",
                    f"xfade_time {xfade_time:g} of {entry.image_path.name!r} exceeds the "
                    f"display time ({prev.display_time:g}) of the preceding image "
                    f"{prev.image_path.name!r}",
                )
            xfade_time = prev.display_time
        timestamp = prev.timestamp + prev.display_time - xfade_time
    else:
        timestamp = 0.0

    slots.append(
        TimelineSlot(
            timestamp=timestamp,
            display_time=display_time,
            xfade_time=xfade_time,
            transition=entry.transition or default_transition,
            image_path=entry.image_path,
            entry_type=entry.entry_type,
            loop_iteration=loop_iteration,
            silent=entry.entry_type in (EntryType.CREDITS, EntryType.END),
        )
    )


def compute_timeline(
    scenario: Scenario,
    audio_duration: float,
    *,
    loop_repeat: bool,
    default_transition: str,
) -> Timeline:
    """Resolve a scenario plus audio duration into absolute-timestamp slots.

    The audible section (``start`` + loop carousel) is exactly ``audio_duration``
    long: the last audible slot is truncated, or, with ``loop_repeat=False``,
    the last loop image is held, so that it ends when the audio ends.
    ``credits`` and ``end`` follow as the silent tail.

    A silent-tail crossfade that is longer than the (truncated) last audible slot is
    shortened to that slot's display time, since where the audio happens to end is
    outside the scenario author's control.

    Raises:
        ValueError: ``audio_duration`` is not a positive finite number.
        ScenarioError: ``VC1007`` if an entry's ``xfade_time`` is longer than the
            display time of the slot it fades in from, where that is a property of
            the scenario itself (e.g. a loop entry following a shorter one).
    """
    if not math.isfinite(audio_duration) or audio_duration <= 0:
        raise ValueError(f"audio_duration must be a positive finite number, got {audio_duration!r}")

    slots: list[TimelineSlot] = []

    start = scenario.start
    last_audible = start
    _append_slot(
        slots,
        start,
        min(start.display_time, audio_duration),
        loop_iteration=0,
        default_transition=default_transition,
    )

    # --- Audible loop section ------------------------------------------------
    remaining = audio_duration - slots[0].display_time
    loops = scenario.loops
    cursor = 0.0  # seconds of the loop section filled so far (sum of dt - xfade)
    index = 0
    while remaining - cursor > EPSILON:
        entry = loops[index % len(loops)]
        # Display time that makes this slot end exactly when the audio ends.
        needed = remaining - cursor + entry.xfade_time
        holds_last_frame = not loop_repeat and index == len(loops) - 1
        display = needed if holds_last_frame else min(entry.display_time, needed)
        _append_slot(
            slots,
            entry,
            display,
            loop_iteration=index // len(loops) + 1,
            default_transition=default_transition,
        )
        cursor += slots[-1].display_time - entry.xfade_time
        last_audible = entry
        index += 1

    loop_passes = math.ceil(index / len(loops)) if index else 0

    # --- Silent tail ---------------------------------------------------------
    for tail in (scenario.credits, scenario.end):
        if tail is not None:
            # Only a slot cut short by the audio may force the crossfade to shrink.
            truncated = slots[-1].display_time < last_audible.display_time - EPSILON
            _append_slot(
                slots,
                tail,
                tail.display_time,
                loop_iteration=0,
                default_transition=default_transition,
                clamp_xfade=truncated,
            )
            last_audible = tail

    last = slots[-1]
    return Timeline(
        slots=slots,
        audio_duration=audio_duration,
        total_duration=last.timestamp + last.display_time,
        loop_passes=loop_passes,
        carousel_size=len(loops),
        carousel_pass_duration=sum(e.display_time - e.xfade_time for e in loops),
        loop_repeat=loop_repeat,
    )


def _transition_label(slot: TimelineSlot, *, first: bool) -> str:
    if first:
        return "\u2014"
    if slot.xfade_time <= 0:
        return "cut"
    return f"{slot.transition} {slot.xfade_time:.3f} s"


def _notes(slot: TimelineSlot) -> str:
    notes: list[str] = []
    if slot.entry_type is EntryType.LOOP and slot.loop_iteration > 1:
        notes.append(f"[loop {slot.loop_iteration}]")
    if slot.silent:
        notes.append("[silent]")
    return " ".join(notes)


def _carousel_summary(timeline: Timeline) -> str | None:
    if timeline.carousel_size == 0:
        return None
    images = f"{timeline.carousel_size} image{'s' if timeline.carousel_size != 1 else ''}"
    one_pass = f"single pass {timeline.carousel_pass_duration:.3f} sec"
    if timeline.loop_passes == 0:
        tail = "not reached (audio ends within the start image)"
    elif not timeline.loop_repeat:
        tail = "no repeat (last image held until audio ends)"
    else:
        loop_time = timeline.audio_duration - timeline.slots[0].display_time
        tail = f"repeats ~{loop_time / timeline.carousel_pass_duration:.1f}x"
    return f"Loop carousel: {images}, {one_pass}, {tail}"


def format_dry_run(timeline: Timeline) -> str:
    """Render the resolved timeline as the dry-run report of CLAUDE.md \u00a7 3.3."""
    lines = [
        "=== vidcarousel dry-run ===",
        f"Audio duration: {format_timestamp(timeline.audio_duration)}",
    ]
    summary = _carousel_summary(timeline)
    if summary is not None:
        lines.append(summary)
    lines.append("")

    headers = ("Timestamp", "Transition", "Display", "File", "")
    table: list[tuple[str, str, str, str, str]] = []
    audio_end_marker_at = 0
    for i, slot in enumerate(timeline.slots):
        if not slot.silent:
            audio_end_marker_at = i + 1
        table.append(
            (
                format_timestamp(slot.timestamp),
                _transition_label(slot, first=i == 0),
                f"{slot.display_time:.3f} s",
                slot.image_path.name,
                _notes(slot),
            )
        )

    widths = [max(len(headers[c]), *(len(row[c]) for row in table)) for c in range(5)]

    def render(cells: tuple[str, str, str, str, str]) -> str:
        line = (
            f" {cells[0].ljust(widths[0])}  {cells[1].ljust(widths[1])}  "
            f"{cells[2].rjust(widths[2])}  {cells[3].ljust(widths[3])}  {cells[4]}"
        )
        return line.rstrip()

    lines.append(render(headers))
    for i, row in enumerate(table):
        if i == audio_end_marker_at:
            lines.append(f" {format_timestamp(timeline.audio_duration)}  \u2014 audio ends \u2014")
        lines.append(render(row))
    if audio_end_marker_at == len(table):
        lines.append(f" {format_timestamp(timeline.audio_duration)}  \u2014 audio ends \u2014")

    lines.append("")
    lines.append(f"Total video duration: {format_timestamp(timeline.total_duration)}")
    return "\n".join(lines)
