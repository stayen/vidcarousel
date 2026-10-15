from __future__ import annotations

from pathlib import Path

import pytest

from vidcarousel.errors import ScenarioError
from vidcarousel.models import EntryType, Scenario, ScenarioEntry, Timeline
from vidcarousel.timeline import compute_timeline, format_dry_run, format_timestamp

S, L, C, E = EntryType.START, EntryType.LOOP, EntryType.CREDITS, EntryType.END


def ent(
    t: EntryType, name: str, dt: float, xf: float = 0.0, transition: str | None = None
) -> ScenarioEntry:
    return ScenarioEntry(t, dt, xf, transition, Path(name))


def spec_scenario(*, credits: bool = False, end: bool = False) -> Scenario:
    """The scenario from CLAUDE.md § 2.1 / § 3.3.

    Manual timeline of one pass: start 0-5, image1 3.5-7.5, image2 5.5-11.5,
    image3 11.5-19.5  ->  audible length of one pass = 19.5 s.
    """
    entries = [
        ent(S, "intro.png", 5),
        ent(L, "image1.png", 4, 1.5),
        ent(L, "image2.png", 6, 2, "dissolve"),
        ent(L, "image3.png", 8),
    ]
    if credits:
        entries.append(ent(C, "credits.png", 10, 1.5))
    if end:
        entries.append(ent(E, "final.png", 5, 1.5))
    return Scenario(tuple(entries))


def compute(sc: Scenario, audio: float, *, repeat: bool = True) -> Timeline:
    return compute_timeline(sc, audio, loop_repeat=repeat, default_transition="fade")


def rows(tl: Timeline) -> list[tuple[str, float, float]]:
    return [(s.image_path.name, s.timestamp, s.display_time) for s in tl.slots]


def assert_rows(tl: Timeline, expected: list[tuple[str, float, float]]) -> None:
    got = rows(tl)
    assert [g[0] for g in got] == [x[0] for x in expected]
    for (_, ts, dt), (name, ets, edt) in zip(got, expected, strict=True):
        assert ts == pytest.approx(ets), name
        assert dt == pytest.approx(edt), name


# ------------------------------------------------------------------ one pass


def test_audio_exactly_start_plus_one_pass_no_repeat() -> None:
    tl = compute(spec_scenario(), 19.5)
    assert_rows(
        tl,
        [
            ("intro.png", 0.0, 5.0),
            ("image1.png", 3.5, 4.0),
            ("image2.png", 5.5, 6.0),
            ("image3.png", 11.5, 8.0),
        ],
    )
    assert tl.loop_passes == 1
    assert tl.total_duration == pytest.approx(19.5)
    assert tl.audio_duration == 19.5
    assert [s.loop_iteration for s in tl.slots] == [0, 1, 1, 1]


def test_xfade_overlap_math_matches_formula() -> None:
    # T = start.dt + sum(loop.dt - loop.xfade)
    tl = compute(spec_scenario(), 19.5)
    assert tl.total_duration == pytest.approx(5 + (4 - 1.5) + (6 - 2) + (8 - 0))


def test_zero_xfade_is_hard_cut() -> None:
    tl = compute(spec_scenario(), 19.5)
    prev, cut = tl.slots[2], tl.slots[3]
    assert cut.xfade_time == 0.0
    assert cut.timestamp == pytest.approx(prev.timestamp + prev.display_time)


def test_transition_resolution() -> None:
    tl = compute_timeline(spec_scenario(), 19.5, loop_repeat=True, default_transition="wipeleft")
    assert [s.transition for s in tl.slots] == ["wipeleft", "wipeleft", "dissolve", "wipeleft"]


# ------------------------------------------------------------------ repeat


def test_audio_longer_repeats_and_truncates_last_entry() -> None:
    tl = compute(spec_scenario(), 30.0)
    assert_rows(
        tl,
        [
            ("intro.png", 0.0, 5.0),
            ("image1.png", 3.5, 4.0),
            ("image2.png", 5.5, 6.0),
            ("image3.png", 11.5, 8.0),
            ("image1.png", 18.0, 4.0),  # matches the § 3.3 dry-run example
            ("image2.png", 20.0, 6.0),
            ("image3.png", 26.0, 4.0),  # truncated from 8.0
        ],
    )
    assert [s.loop_iteration for s in tl.slots] == [0, 1, 1, 1, 2, 2, 2]
    assert tl.loop_passes == 2
    assert tl.total_duration == pytest.approx(30.0)


def test_truncated_last_slot_keeps_incoming_crossfade() -> None:
    # Audio ends 0.5 s into image3's slot... image2 xfade is 2 s, so ending at 12.0:
    tl = compute(spec_scenario(), 12.0)
    last = tl.slots[-1]
    assert last.image_path.name == "image3.png"
    assert last.display_time == pytest.approx(0.5)
    assert last.timestamp + last.display_time == pytest.approx(12.0)


def test_truncation_inside_crossfade_region() -> None:
    # remaining after start = 0.5: image1 needs 0.5 + 1.5 = 2.0 s on screen.
    tl = compute(spec_scenario(), 5.5)
    assert_rows(tl, [("intro.png", 0.0, 5.0), ("image1.png", 3.5, 2.0)])
    assert tl.total_duration == pytest.approx(5.5)
    assert tl.loop_passes == 1


def test_single_loop_entry_repeats() -> None:
    sc = Scenario((ent(S, "s.png", 2), ent(L, "a.png", 3, 1)))
    tl = compute(sc, 10.0)
    assert_rows(
        tl,
        [
            ("s.png", 0.0, 2.0),
            ("a.png", 1.0, 3.0),
            ("a.png", 3.0, 3.0),
            ("a.png", 5.0, 3.0),
            ("a.png", 7.0, 3.0),
        ],
    )
    assert [s.loop_iteration for s in tl.slots] == [0, 1, 2, 3, 4]
    assert tl.loop_passes == 4


def test_no_float_noise_slivers() -> None:
    sc = Scenario((ent(S, "s.png", 0.3), ent(L, "a.png", 0.1)))
    tl = compute(sc, 1.0)
    assert len(tl.slots) == 8  # start + exactly 7 loop slots
    assert tl.total_duration == pytest.approx(1.0)


# ------------------------------------------------------------------ no repeat


def test_no_loop_repeat_holds_last_image() -> None:
    tl = compute(spec_scenario(), 30.0, repeat=False)
    assert_rows(
        tl,
        [
            ("intro.png", 0.0, 5.0),
            ("image1.png", 3.5, 4.0),
            ("image2.png", 5.5, 6.0),
            ("image3.png", 11.5, 18.5),  # held until audio ends
        ],
    )
    assert tl.loop_passes == 1
    assert tl.total_duration == pytest.approx(30.0)


def test_no_loop_repeat_exact_fit_unchanged() -> None:
    tl = compute(spec_scenario(), 19.5, repeat=False)
    assert tl.slots[-1].display_time == 8.0
    assert tl.loop_passes == 1


def test_no_loop_repeat_short_audio_truncates() -> None:
    tl = compute(spec_scenario(), 12.0, repeat=False)
    assert_rows(
        tl,
        [
            ("intro.png", 0.0, 5.0),
            ("image1.png", 3.5, 4.0),
            ("image2.png", 5.5, 6.0),
            ("image3.png", 11.5, 0.5),
        ],
    )


def test_no_loop_repeat_single_entry() -> None:
    sc = Scenario((ent(S, "s.png", 2), ent(L, "a.png", 3, 1)))
    tl = compute(sc, 10.0, repeat=False)
    assert_rows(tl, [("s.png", 0.0, 2.0), ("a.png", 1.0, 9.0)])
    assert tl.loop_passes == 1


# ------------------------------------------------------------------ short audio


def test_audio_shorter_than_start_truncates_start() -> None:
    tl = compute(spec_scenario(), 3.0)
    assert_rows(tl, [("intro.png", 0.0, 3.0)])
    assert tl.loop_passes == 0
    assert tl.total_duration == pytest.approx(3.0)


def test_audio_equal_to_start_has_no_loops() -> None:
    tl = compute(spec_scenario(), 5.0)
    assert len(tl.slots) == 1
    assert tl.loop_passes == 0


def test_short_audio_still_gets_silent_tail() -> None:
    tl = compute(spec_scenario(credits=True), 3.0)
    assert_rows(tl, [("intro.png", 0.0, 3.0), ("credits.png", 1.5, 10.0)])
    assert tl.slots[-1].silent
    assert tl.total_duration == pytest.approx(11.5)


def test_tail_crossfade_clamped_to_truncated_start() -> None:
    # Credits want a 1.5 s fade but the start image is only on screen for 1 s.
    tl = compute(spec_scenario(credits=True), 1.0)
    credits = tl.slots[-1]
    assert credits.xfade_time == pytest.approx(1.0)
    assert credits.timestamp == pytest.approx(0.0)
    assert tl.total_duration == pytest.approx(10.0)


def test_tail_crossfade_clamped_to_truncated_last_loop_slot() -> None:
    # Audio ends 0.5 s into image3's slot; credits' 1.5 s fade shrinks to 0.5 s.
    tl = compute(spec_scenario(credits=True, end=True), 12.0)
    image3, credits, final = tl.slots[-3:]
    assert image3.timestamp == pytest.approx(11.5)
    assert image3.display_time == pytest.approx(0.5)
    assert credits.xfade_time == pytest.approx(0.5)
    assert credits.timestamp == pytest.approx(11.5)
    # credits itself is not truncated, so 'end' keeps its full 1.5 s crossfade
    assert final.xfade_time == pytest.approx(1.5)
    assert final.timestamp == pytest.approx(11.5 + 10.0 - 1.5)
    assert tl.total_duration == pytest.approx(25.0)


def test_tail_crossfade_kept_when_predecessor_not_truncated() -> None:
    sc = Scenario((ent(S, "s.png", 5), ent(L, "a.png", 4, 1), ent(C, "c.png", 10, 3.5)))
    tl = compute(sc, 8.0)  # 5 + (4 - 1): a.png is on screen for its full 4 s
    assert tl.slots[1].display_time == pytest.approx(4.0)
    assert tl.slots[-1].xfade_time == pytest.approx(3.5)


def test_tail_crossfade_longer_than_untruncated_predecessor_rejected() -> None:
    # A property of the scenario, not of the audio length: a.png is only on screen 2 s.
    sc = Scenario((ent(S, "s.png", 5), ent(L, "a.png", 2, 1), ent(C, "c.png", 10, 2.5)))
    with pytest.raises(ScenarioError) as exc:
        compute(sc, 6.0)
    assert exc.value.code == "VC1007"
    assert "c.png" in exc.value.message


def test_loop_crossfade_longer_than_previous_slot_rejected() -> None:
    sc = Scenario((ent(S, "s.png", 5), ent(L, "a.png", 2, 1), ent(L, "b.png", 5, 3)))
    with pytest.raises(ScenarioError) as exc:
        compute(sc, 20.0)
    assert exc.value.code == "VC1007"
    assert "b.png" in exc.value.message


# ------------------------------------------------------------------ silent tail


def test_credits_and_end_appended_after_audio() -> None:
    tl = compute(spec_scenario(credits=True, end=True), 19.5)
    assert_rows(
        tl,
        [
            ("intro.png", 0.0, 5.0),
            ("image1.png", 3.5, 4.0),
            ("image2.png", 5.5, 6.0),
            ("image3.png", 11.5, 8.0),
            ("credits.png", 18.0, 10.0),  # fades in over the last 1.5 s of audio
            ("final.png", 26.5, 5.0),
        ],
    )
    assert [s.silent for s in tl.slots] == [False] * 4 + [True, True]
    assert [s.entry_type for s in tl.slots[-2:]] == [C, E]
    assert [s.loop_iteration for s in tl.slots[-2:]] == [0, 0]
    assert tl.audio_duration == 19.5
    assert tl.total_duration == pytest.approx(31.5)


def test_credits_only_and_end_only() -> None:
    tl = compute(spec_scenario(credits=True), 19.5)
    assert tl.total_duration == pytest.approx(28.0)
    assert tl.slots[-1].entry_type is C

    tl = compute(spec_scenario(end=True), 19.5)
    assert_rows(
        tl,
        [
            ("intro.png", 0.0, 5.0),
            ("image1.png", 3.5, 4.0),
            ("image2.png", 5.5, 6.0),
            ("image3.png", 11.5, 8.0),
            ("final.png", 18.0, 5.0),
        ],
    )
    assert tl.total_duration == pytest.approx(23.0)


def test_tail_after_repeat_and_truncation() -> None:
    tl = compute(spec_scenario(end=True), 30.0)
    assert tl.slots[-2].timestamp + tl.slots[-2].display_time == pytest.approx(30.0)
    assert tl.slots[-1].timestamp == pytest.approx(28.5)
    assert tl.total_duration == pytest.approx(33.5)


def test_no_tail_video_ends_with_audio() -> None:
    tl = compute(spec_scenario(), 42.0)
    assert tl.total_duration == pytest.approx(42.0)
    assert not any(s.silent for s in tl.slots)


# ------------------------------------------------------------------ invariants


@pytest.mark.parametrize("audio", [0.5, 1.6, 5.0, 5.1, 12.0, 7.5, 19.5, 19.6, 33.3, 120.0, 225.2])
@pytest.mark.parametrize("repeat", [True, False])
def test_invariants(audio: float, repeat: bool) -> None:
    tl = compute(spec_scenario(credits=True, end=True), audio, repeat=repeat)
    audible = [s for s in tl.slots if not s.silent]
    # audible section ends exactly when the audio does
    assert audible[-1].timestamp + audible[-1].display_time == pytest.approx(audio)
    # every slot starts no earlier than the previous one, and extends past it
    for prev, cur in zip(tl.slots, tl.slots[1:], strict=False):
        assert cur.timestamp >= prev.timestamp
        assert cur.timestamp + cur.display_time > prev.timestamp + prev.display_time
        assert cur.timestamp == pytest.approx(prev.timestamp + prev.display_time - cur.xfade_time)
    assert 0 < tl.slots[0].display_time <= 5.0
    assert tl.total_duration == pytest.approx(tl.slots[-1].timestamp + tl.slots[-1].display_time)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_audio_duration(bad: float) -> None:
    with pytest.raises(ValueError, match="audio_duration"):
        compute(spec_scenario(), bad)


# ------------------------------------------------------------------ format_timestamp


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0:00:00.000"),
        (3.5, "0:00:03.500"),
        (59.999, "0:00:59.999"),
        (59.9996, "0:01:00.000"),
        (225.2, "0:03:45.200"),
        (3600, "1:00:00.000"),
        (3725.0426, "1:02:05.043"),
        (36000.5, "10:00:00.500"),
    ],
)
def test_format_timestamp(seconds: float, expected: str) -> None:
    assert format_timestamp(seconds) == expected


@pytest.mark.parametrize("bad", [-0.001, float("nan"), float("inf")])
def test_format_timestamp_rejects_invalid(bad: float) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        format_timestamp(bad)


# ------------------------------------------------------------------ carousel facts


def test_timeline_records_carousel_facts() -> None:
    tl = compute(spec_scenario(), 30.0)
    assert tl.carousel_size == 3
    assert tl.carousel_pass_duration == pytest.approx(2.5 + 4 + 8)
    assert tl.loop_repeat is True
    assert compute(spec_scenario(), 30.0, repeat=False).loop_repeat is False


# ------------------------------------------------------------------ format_dry_run


def test_dry_run_golden_with_repeat_and_tail() -> None:
    out = format_dry_run(compute(spec_scenario(credits=True, end=True), 30.0))
    assert out.split("\n") == [
        "=== vidcarousel dry-run ===",
        "Audio duration: 0:00:30.000",
        "Loop carousel: 3 images, single pass 14.500 sec, repeats ~1.7x",
        "",
        " Timestamp    Transition         Display  File",
        " 0:00:00.000  \u2014                  5.000 s  intro.png",
        " 0:00:03.500  fade 1.500 s       4.000 s  image1.png",
        " 0:00:05.500  dissolve 2.000 s   6.000 s  image2.png",
        " 0:00:11.500  cut                8.000 s  image3.png",
        " 0:00:18.000  fade 1.500 s       4.000 s  image1.png   [loop 2]",
        " 0:00:20.000  dissolve 2.000 s   6.000 s  image2.png   [loop 2]",
        " 0:00:26.000  cut                4.000 s  image3.png   [loop 2]",
        " 0:00:30.000  \u2014 audio ends \u2014",
        " 0:00:28.500  fade 1.500 s      10.000 s  credits.png  [silent]",
        " 0:00:37.000  fade 1.500 s       5.000 s  final.png    [silent]",
        "",
        "Total video duration: 0:00:42.000",
    ]


def test_dry_run_has_no_trailing_whitespace() -> None:
    out = format_dry_run(compute(spec_scenario(credits=True, end=True), 30.0))
    assert all(line == line.rstrip() for line in out.split("\n"))


def test_dry_run_loop_annotations_only_on_repeats() -> None:
    lines = format_dry_run(compute(spec_scenario(), 60.0)).split("\n")
    loop_lines = [ln for ln in lines if "[loop" in ln]
    assert loop_lines  # repeated entries are annotated
    assert not any("[loop 1]" in ln for ln in lines)  # first pass is not
    assert any("[loop 3]" in ln for ln in lines)
    assert not any("[silent]" in ln for ln in lines)


def test_dry_run_silent_annotation_on_credits_and_end() -> None:
    lines = format_dry_run(compute(spec_scenario(credits=True, end=True), 19.5)).split("\n")
    assert [ln for ln in lines if "[silent]" in ln] == [
        ln for ln in lines if "credits.png" in ln or "final.png" in ln
    ]
    assert sum("[silent]" in ln for ln in lines) == 2


def test_dry_run_audio_end_marker_between_audible_and_silent() -> None:
    lines = format_dry_run(compute(spec_scenario(credits=True), 19.5)).split("\n")
    marker = next(i for i, ln in enumerate(lines) if "audio ends" in ln)
    assert "image3.png" in lines[marker - 1]
    assert "credits.png" in lines[marker + 1]
    assert lines[marker].strip().startswith("0:00:19.500")


def test_dry_run_audio_end_marker_without_tail() -> None:
    lines = format_dry_run(compute(spec_scenario(), 19.5)).split("\n")
    assert "audio ends" in lines[-3]
    assert lines[-1] == "Total video duration: 0:00:19.500"


def test_dry_run_no_loop_repeat_summary() -> None:
    out = format_dry_run(compute(spec_scenario(), 30.0, repeat=False))
    assert "no repeat (last image held until audio ends)" in out
    assert "18.500 s" in out
    assert "[loop" not in out


def test_dry_run_audio_shorter_than_start() -> None:
    out = format_dry_run(compute(spec_scenario(), 3.0))
    assert "not reached" in out
    assert out.count("intro.png") == 1
    assert "image1.png" not in out


def test_dry_run_singular_image_and_single_loop() -> None:
    sc = Scenario((ent(S, "s.png", 2), ent(L, "a.png", 3, 1)))
    out = format_dry_run(compute(sc, 10.0))
    assert "Loop carousel: 1 image, single pass 2.000 sec, repeats ~4.0x" in out
    assert "[loop 4]" in out


def test_dry_run_hand_built_timeline_without_carousel_facts() -> None:
    tl = compute(spec_scenario(), 19.5)
    bare = Timeline(tl.slots, tl.audio_duration, tl.total_duration, tl.loop_passes)
    out = format_dry_run(bare)
    assert "Loop carousel" not in out
    assert "Total video duration: 0:00:19.500" in out
