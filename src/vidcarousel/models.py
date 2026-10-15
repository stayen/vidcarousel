"""Data models: scenario entries, resolved timeline slots."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class EntryType(str, Enum):
    START = "start"
    LOOP = "loop"
    CREDITS = "credits"
    END = "end"


@dataclass(frozen=True)
class ScenarioEntry:
    entry_type: EntryType
    display_time: float
    xfade_time: float  # 0.0 = hard cut
    transition: str | None  # None = use global default
    image_path: Path


@dataclass(frozen=True)
class Scenario:
    entries: tuple[ScenarioEntry, ...]

    @property
    def start(self) -> ScenarioEntry:
        for entry in self.entries:
            if entry.entry_type is EntryType.START:
                return entry
        raise ValueError("Scenario has no start entry")

    @property
    def loops(self) -> tuple[ScenarioEntry, ...]:
        return tuple(e for e in self.entries if e.entry_type is EntryType.LOOP)

    @property
    def credits(self) -> ScenarioEntry | None:
        for entry in self.entries:
            if entry.entry_type is EntryType.CREDITS:
                return entry
        return None

    @property
    def end(self) -> ScenarioEntry | None:
        for entry in self.entries:
            if entry.entry_type is EntryType.END:
                return entry
        return None


@dataclass
class TimelineSlot:
    """One resolved image appearance in the final video."""

    timestamp: float  # absolute start time in output (seconds)
    display_time: float  # effective display duration
    xfade_time: float
    transition: str
    image_path: Path
    entry_type: EntryType
    loop_iteration: int  # 0 for start/credits/end; 1-based for loops
    silent: bool  # True for credits/end


@dataclass
class Timeline:
    slots: list[TimelineSlot]
    audio_duration: float
    total_duration: float
    loop_passes: int  # how many full + partial passes through carousel
    # Carousel facts needed by the dry-run report (defaults keep hand-built timelines valid).
    carousel_size: int = 0  # number of loop entries in the scenario
    carousel_pass_duration: float = 0.0  # time advanced by one full pass: sum(dt - xfade)
    loop_repeat: bool = True  # False when the last loop image is held (--no-loop-repeat)
