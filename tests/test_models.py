import dataclasses
from pathlib import Path

import pytest

from vidcarousel.models import (
    EntryType,
    Scenario,
    ScenarioEntry,
    Timeline,
    TimelineSlot,
)


def entry(t: EntryType, name: str, dt: float = 4.0, xf: float = 0.0) -> ScenarioEntry:
    return ScenarioEntry(t, dt, xf, None, Path(name))


def test_entry_type_values() -> None:
    assert [e.value for e in EntryType] == ["start", "loop", "credits", "end"]
    assert EntryType("loop") is EntryType.LOOP
    assert isinstance(EntryType.START, str)
    assert EntryType.START.value == "start"


def test_scenario_entry_frozen() -> None:
    e = entry(EntryType.START, "a.png")
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.display_time = 1.0  # type: ignore[misc]


def test_scenario_frozen() -> None:
    s = Scenario((entry(EntryType.START, "a.png"),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.entries = ()  # type: ignore[misc]


def test_scenario_accessors_full() -> None:
    start = entry(EntryType.START, "s.png")
    l1 = entry(EntryType.LOOP, "1.png")
    l2 = entry(EntryType.LOOP, "2.png")
    cr = entry(EntryType.CREDITS, "c.png")
    end = entry(EntryType.END, "e.png")
    s = Scenario((start, l1, l2, cr, end))
    assert s.start is start
    assert s.loops == (l1, l2)
    assert s.credits is cr
    assert s.end is end


def test_scenario_accessors_minimal() -> None:
    start = entry(EntryType.START, "s.png")
    l1 = entry(EntryType.LOOP, "1.png")
    s = Scenario((start, l1))
    assert s.loops == (l1,)
    assert s.credits is None
    assert s.end is None


def test_scenario_without_start_raises() -> None:
    with pytest.raises(ValueError, match="no start"):
        _ = Scenario(()).start


def test_scenario_entries_hashable_and_equal() -> None:
    a = entry(EntryType.LOOP, "1.png")
    b = entry(EntryType.LOOP, "1.png")
    assert a == b
    assert hash(a) == hash(b)


def test_timeline_slot_and_timeline_mutable() -> None:
    slot = TimelineSlot(0.0, 5.0, 0.0, "fade", Path("a.png"), EntryType.START, 0, False)
    tl = Timeline(slots=[slot], audio_duration=5.0, total_duration=5.0, loop_passes=0)
    slot.display_time = 3.0
    tl.slots.append(slot)
    assert tl.slots[0].display_time == 3.0
    assert len(tl.slots) == 2
