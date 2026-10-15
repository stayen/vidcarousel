from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from vidcarousel.errors import FfmpegError, OutputError, ScenarioError
from vidcarousel.ffmpeg import (
    FILTER_INLINE_LIMIT,
    _num,
    build_ffmpeg_command,
    build_filter_complex,
    check_binaries,
    get_valid_transitions,
    probe_audio_duration,
    probe_image_dimensions,
    run_ffmpeg,
)
from vidcarousel.models import EntryType, Scenario, ScenarioEntry, Timeline
from vidcarousel.scenario import parse_entry_line
from vidcarousel.timeline import compute_timeline

Runner = Callable[..., subprocess.CompletedProcess[str]]


def fake_run(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raises: Exception | None = None,
) -> list[list[str]]:
    """Patch subprocess.run inside vidcarousel.ffmpeg; return the recorded argv list."""
    calls: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("errors") == "replace"  # undecodable bytes must not crash us
        assert kwargs.get("check") is False
        assert not kwargs.get("shell")
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr("vidcarousel.ffmpeg.subprocess.run", _run)
    return calls


# ------------------------------------------------------------------ check_binaries


def patch_which(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> None:
    monkeypatch.setattr(
        "vidcarousel.ffmpeg.shutil.which", lambda name: f"/bin/{name}" if name in present else None
    )


def test_check_binaries_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_which(monkeypatch, {"ffmpeg", "ffprobe"})
    check_binaries("ffmpeg", "ffprobe")


def test_check_binaries_missing_ffmpeg_vc3001(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_which(monkeypatch, {"ffprobe"})
    with pytest.raises(FfmpegError) as exc:
        check_binaries("ffmpeg", "ffprobe")
    assert exc.value.code == "VC3001"
    assert exc.value.exit_code == 3


def test_check_binaries_missing_ffprobe_vc3002(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_which(monkeypatch, {"ffmpeg"})
    with pytest.raises(FfmpegError) as exc:
        check_binaries("ffmpeg", "ffprobe")
    assert exc.value.code == "VC3002"


def test_check_binaries_both_missing_reports_ffmpeg_first(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_which(monkeypatch, set())
    with pytest.raises(FfmpegError) as exc:
        check_binaries("ffmpeg", "ffprobe")
    assert exc.value.code == "VC3001"


def test_check_binaries_custom_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_which(monkeypatch, {"/opt/ff/ffmpeg", "/opt/ff/ffprobe"})
    check_binaries("/opt/ff/ffmpeg", "/opt/ff/ffprobe")
    with pytest.raises(FfmpegError, match="/opt/ff/nope"):
        check_binaries("/opt/ff/nope", "/opt/ff/ffprobe")


# ------------------------------------------------------------------ probe_audio_duration


def test_probe_audio_duration_parses_and_builds_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, stdout="225.200000\n")
    assert probe_audio_duration(Path("song.mp3"), "/usr/bin/ffprobe") == pytest.approx(225.2)
    assert calls == [
        [
            "/usr/bin/ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0", "song.mp3",
        ]
    ]  # fmt: skip


def test_probe_audio_duration_default_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, stdout="1.5")
    assert probe_audio_duration(Path("a.wav")) == 1.5
    assert calls[0][0] == "ffprobe"


@pytest.mark.parametrize("stdout", ["10\n", "  10.0  \n\n", "10.0,\n", "10.0\nextra\n"])
def test_probe_audio_duration_tolerates_formatting(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    fake_run(monkeypatch, stdout=stdout)
    assert probe_audio_duration(Path("a.wav")) == 10.0


@pytest.mark.parametrize(
    "stdout", ["N/A\n", "abc", "", "\n", "nan", "inf", "0", "0.000000", "-3.2"]
)
def test_probe_audio_duration_bad_output_vc3002(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    fake_run(monkeypatch, stdout=stdout)
    with pytest.raises(FfmpegError) as exc:
        probe_audio_duration(Path("a.wav"))
    assert exc.value.code == "VC3002"


def test_probe_audio_duration_nonzero_exit_vc3002(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, stderr="a.wav: Invalid data found\n", returncode=1)
    with pytest.raises(FfmpegError) as exc:
        probe_audio_duration(Path("a.wav"))
    assert exc.value.code == "VC3002"
    assert "Invalid data found" in exc.value.message
    assert "a.wav" in exc.value.message


def test_probe_audio_duration_nonzero_exit_without_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, returncode=2)
    with pytest.raises(FfmpegError, match="exit code 2"):
        probe_audio_duration(Path("a.wav"))


@pytest.mark.parametrize("error", [FileNotFoundError("no ffprobe"), PermissionError("denied")])
def test_probe_audio_duration_binary_missing_vc3002(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    fake_run(monkeypatch, raises=error)
    with pytest.raises(FfmpegError) as exc:
        probe_audio_duration(Path("a.wav"), "/nonexistent/ffprobe")
    assert exc.value.code == "VC3002"
    assert "/nonexistent/ffprobe" in exc.value.message


# ------------------------------------------------------------------ probe_image_dimensions


def test_probe_image_dimensions_parses_and_builds_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, stdout="1920,1080\n")
    assert probe_image_dimensions(Path("img/a.png"), "ffprobe-x") == (1920, 1080)
    assert calls == [
        [
            "ffprobe-x", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0", "img/a.png",
        ]
    ]  # fmt: skip


@pytest.mark.parametrize("stdout", ["64,48,\n", " 64,48 \n", "64,48\n64,48\n"])
def test_probe_image_dimensions_tolerates_formatting(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    fake_run(monkeypatch, stdout=stdout)
    assert probe_image_dimensions(Path("a.png")) == (64, 48)


@pytest.mark.parametrize(
    "stdout",
    ["", "\n", "64\n", "64,48,3\n", "a,b\n", "64,\n", ",48\n", "0,48\n", "64,-1\n", "6.4,48\n"],
)
def test_probe_image_dimensions_bad_output_vc3002(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    fake_run(monkeypatch, stdout=stdout)
    with pytest.raises(FfmpegError) as exc:
        probe_image_dimensions(Path("a.png"))
    assert exc.value.code == "VC3002"


def test_probe_image_dimensions_nonzero_exit_vc3002(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, stderr="not an image", returncode=1)
    with pytest.raises(FfmpegError) as exc:
        probe_image_dimensions(Path("a.png"))
    assert exc.value.code == "VC3002"
    assert "not an image" in exc.value.message


def test_probe_image_dimensions_binary_missing_vc3002(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, raises=FileNotFoundError("gone"))
    with pytest.raises(FfmpegError) as exc:
        probe_image_dimensions(Path("a.png"))
    assert exc.value.code == "VC3002"


# ------------------------------------------------------------------ get_valid_transitions

SPEC_TRANSITIONS = [
    "fade",
    "fadeblack",
    "fadewhite",
    "distance",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "circlecrop",
    "rectcrop",
    "circleclose",
    "circleopen",
    "horzclose",
    "horzopen",
    "vertclose",
    "vertopen",
    "diagbl",
    "diagbr",
    "diagtl",
    "diagtr",
    "hlslice",
    "hrslice",
    "vuslice",
    "vdslice",
    "dissolve",
    "pixelize",
    "radial",
    "hblur",
    "wipetl",
    "wipetr",
    "wipebl",
    "wipebr",
    "squeezeh",
    "squeezev",
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
]


def test_valid_transitions_cover_spec_list() -> None:
    assert len(SPEC_TRANSITIONS) == 42
    assert set(SPEC_TRANSITIONS) <= get_valid_transitions()


def test_valid_transitions_are_plain_lowercase_names() -> None:
    for name in get_valid_transitions():
        assert name.isalpha() and name.islower()


def test_valid_transitions_returns_independent_copy() -> None:
    first = get_valid_transitions()
    first.add("bogus")
    first.discard("fade")
    again = get_valid_transitions()
    assert "bogus" not in again
    assert "fade" in again


def test_valid_transitions_plug_into_scenario_parser() -> None:
    valid = get_valid_transitions()
    entry = parse_entry_line("loop,4,1,dissolve a.png", 1, Path("."), valid)
    assert entry.transition == "dissolve"
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line("loop,4,1,bogus a.png", 1, Path("."), valid)
    assert exc.value.code == "VC1009"


# ------------------------------------------------------------------ command building

S, L, C, E = EntryType.START, EntryType.LOOP, EntryType.CREDITS, EntryType.END


def ent(
    t: EntryType, name: str, dt: float, xf: float = 0.0, transition: str | None = None
) -> ScenarioEntry:
    return ScenarioEntry(t, dt, xf, transition, Path(name))


def spec_scenario(*, credits: bool = False, end: bool = False) -> Scenario:
    """CLAUDE.md section 2.1 scenario.

    Timeline of one pass: intro 0-5, image1 3.5-7.5 (fade 1.5), image2 5.5-11.5
    (dissolve 2), image3 11.5-19.5 (hard cut); credits (fade 1.5) and end (fade 1.5).
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


def timeline_for(sc: Scenario, audio: float, *, repeat: bool = True) -> Timeline:
    return compute_timeline(sc, audio, loop_repeat=repeat, default_transition="fade")


def norm(i: int, label: str | None = None) -> str:
    return f"[{i}:v]fps=25,format=yuv420p,setsar=1[{label or f'i{i}'}]"


def command(tl: Timeline, **overrides: Any) -> list[str]:
    kwargs: dict[str, Any] = {
        "codec": "libx264",
        "audio_codec": "aac",
        "crf": 23,
        "fps": 25,
        "audio_fade_out": 0.0,
        "output_format": None,
        "ffmpeg_bin": "ffmpeg",
    }
    kwargs.update(overrides)
    return build_ffmpeg_command(tl, Path("song.wav"), Path("out.mp4"), **kwargs)


def option_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def test_num_formatting() -> None:
    assert _num(0) == "0"
    assert _num(-0.0) == "0"
    assert _num(5) == "5"
    assert _num(3.5) == "3.5"
    assert _num(19.499999999999996) == "19.5"
    assert _num(0.1 + 0.2) == "0.3"
    assert _num(1 / 3) == "0.333333"
    assert _num(225.2) == "225.2"


def test_filter_complex_known_four_slot_timeline() -> None:
    # xfade, xfade, hard cut.  Offsets are the slot timestamps: 3.5 and 5.5.
    fc = build_filter_complex(timeline_for(spec_scenario(), 19.5), 19.5, 0.0)
    assert fc == ";".join(
        [
            norm(0),
            norm(1),
            norm(2),
            norm(3),
            "[i0][i1]xfade=transition=fade:duration=1.5:offset=3.5[s1]",
            "[s1][i2]xfade=transition=dissolve:duration=2:offset=5.5[s2]",
            "[s2][i3]concat=n=2:v=1:a=0,fps=25[vfinal]",
            "[4:a]atrim=0:19.5[afinal]",
        ]
    )


def test_filter_complex_three_images_all_crossfaded() -> None:
    sc = Scenario((ent(S, "a.png", 4), ent(L, "b.png", 4, 1), ent(L, "c.png", 4, 2, "wipeleft")))
    tl = timeline_for(sc, 4 + 3 + 2)  # a 0-4, b 3-7, c 5-9
    fc = build_filter_complex(tl, 9.0, 0.0)
    assert "[i0][i1]xfade=transition=fade:duration=1:offset=3[s1]" in fc
    assert "[s1][i2]xfade=transition=wipeleft:duration=2:offset=5[vfinal]" in fc
    assert fc.count("xfade=") == 2
    assert "concat" not in fc


def test_filter_complex_stage_counts_with_tail_and_repeat() -> None:
    tl = timeline_for(spec_scenario(credits=True, end=True), 30.0)
    fc = build_filter_complex(tl, 30.0, 0.0)
    slots = tl.slots
    n_xfade = sum(1 for s in slots[1:] if s.xfade_time > 0)
    n_cut = sum(1 for s in slots[1:] if s.xfade_time == 0)
    assert fc.count("xfade=") == n_xfade
    assert fc.count("concat=") == n_cut
    assert n_xfade + n_cut == len(slots) - 1
    assert fc.count("setsar=1") == len(slots)
    assert fc.endswith("[afinal]")
    assert f"[{len(slots)}:a]" in fc  # audio is the input after the last image


def test_filter_complex_tail_offsets() -> None:
    tl = timeline_for(spec_scenario(credits=True, end=True), 19.5)
    fc = build_filter_complex(tl, 19.5, 0.0)
    # credits fade in 1.5 s before audio ends; end fades in 1.5 s before credits end (28)
    assert "[s3][i4]xfade=transition=fade:duration=1.5:offset=18[s4]" in fc
    assert "[s4][i5]xfade=transition=fade:duration=1.5:offset=26.5[vfinal]" in fc


def test_filter_complex_hard_cut_uses_no_zero_duration_xfade() -> None:
    fc = build_filter_complex(timeline_for(spec_scenario(), 19.5), 19.5, 0.0)
    assert "duration=0:" not in fc and "duration=0.001" not in fc
    assert fc.count("xfade=") == 2


def test_filter_complex_mixed_transitions() -> None:
    tl = timeline_for(spec_scenario(), 19.5)
    fc = build_filter_complex(tl, 19.5, 0.0)
    assert "transition=fade" in fc  # global default (image1)
    assert "transition=dissolve" in fc  # per-entry override (image2)


def test_filter_complex_single_slot() -> None:
    tl = timeline_for(spec_scenario(), 3.0)
    assert build_filter_complex(tl, 3.0, 0.0) == (
        "[0:v]fps=25,format=yuv420p,setsar=1[vfinal];[1:a]atrim=0:3[afinal]"
    )


def test_filter_complex_two_slots() -> None:
    sc = Scenario((ent(S, "a.png", 2), ent(L, "b.png", 2, 1)))
    fc = build_filter_complex(timeline_for(sc, 3.0), 3.0, 0.0)
    assert fc == ";".join(
        [
            norm(0),
            norm(1),
            "[i0][i1]xfade=transition=fade:duration=1:offset=1[vfinal]",
            "[2:a]atrim=0:3[afinal]",
        ]
    )


def test_filter_complex_fps_parameter() -> None:
    fc = build_filter_complex(timeline_for(spec_scenario(), 19.5), 19.5, 0.0, fps=30)
    assert "fps=30,format=yuv420p" in fc
    assert "concat=n=2:v=1:a=0,fps=30[vfinal]" in fc
    assert "fps=25" not in fc


def test_audio_fade_out_present_only_when_requested() -> None:
    tl = timeline_for(spec_scenario(credits=True), 19.5)
    assert "afade" not in build_filter_complex(tl, 19.5, 0.0)
    fc = build_filter_complex(tl, 19.5, 2.0)
    # 5 image inputs (start, 3 loops, credits) -> audio is input 5
    assert fc.endswith("[5:a]atrim=0:19.5,afade=t=out:st=17.5:d=2[afinal]")
    assert fc.count("afade") == 1


def test_audio_fade_out_longer_than_audio_is_capped() -> None:
    tl = timeline_for(spec_scenario(), 3.0)
    fc = build_filter_complex(tl, 3.0, 10.0)
    assert "afade=t=out:st=0:d=3" in fc


def test_filter_complex_rejects_bad_arguments() -> None:
    tl = timeline_for(spec_scenario(), 19.5)
    with pytest.raises(ValueError, match="audio_fade_out"):
        build_filter_complex(tl, 19.5, -1.0)
    with pytest.raises(ValueError, match="audio_duration"):
        build_filter_complex(tl, 0.0, 0.0)
    with pytest.raises(ValueError, match="no slots"):
        build_filter_complex(Timeline([], 1.0, 1.0, 0), 1.0, 0.0)


def test_command_inputs_one_per_slot_then_audio() -> None:
    tl = timeline_for(spec_scenario(credits=True, end=True), 19.5)
    cmd = command(tl)
    n_slots = len(tl.slots)
    assert n_slots == 6
    assert cmd.count("-i") == n_slots + 1
    assert cmd.count("-loop") == n_slots
    # first n image inputs, in order, each with -loop 1 -t <display_time>
    expected: list[str] = []
    for slot in tl.slots:
        expected += ["-loop", "1", "-t", _num(slot.display_time), "-i", str(slot.image_path)]
    expected += ["-i", "song.wav"]
    start = cmd.index("-loop")
    assert cmd[start : start + len(expected)] == expected
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-t"] == [
        "5", "4", "6", "8", "10", "5",
    ]  # fmt: skip


def test_command_silent_tail_images_included_after_audio_trim_point() -> None:
    tl = timeline_for(spec_scenario(credits=True, end=True), 19.5)
    cmd = command(tl)
    images = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert images == [
        "intro.png", "image1.png", "image2.png", "image3.png",
        "credits.png", "final.png", "song.wav",
    ]  # fmt: skip
    fc = option_value(cmd, "-filter_complex")
    assert "atrim=0:19.5" in fc  # audio stops where the silent tail begins
    assert "[6:a]" in fc


def test_command_output_options_propagated() -> None:
    cmd = command(
        timeline_for(spec_scenario(), 19.5),
        codec="libx265",
        audio_codec="libopus",
        crf=18,
        fps=30,
    )
    assert option_value(cmd, "-c:v") == "libx265"
    assert option_value(cmd, "-c:a") == "libopus"
    assert option_value(cmd, "-crf") == "18"
    assert option_value(cmd, "-r") == "30"
    assert option_value(cmd, "-pix_fmt") == "yuv420p"
    assert "fps=30" in option_value(cmd, "-filter_complex")
    assert cmd[-1] == "out.mp4"


def test_command_maps_final_streams_unquoted() -> None:
    cmd = command(timeline_for(spec_scenario(), 19.5))
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[vfinal]", "[afinal]"]


def test_command_binary_and_safety_flags() -> None:
    cmd = command(timeline_for(spec_scenario(), 19.5), ffmpeg_bin="/opt/ff/ffmpeg")
    assert cmd[0] == "/opt/ff/ffmpeg"
    assert "-n" in cmd and "-nostdin" in cmd and "-nostats" in cmd
    assert "-shortest" not in cmd  # the silent tail must not be cut off


def test_command_faststart_for_mp4_family_only() -> None:
    tl = timeline_for(spec_scenario(), 19.5)
    assert option_value(command(tl), "-movflags") == "+faststart"
    for name in ("out.MOV", "out.m4v"):
        cmd = build_ffmpeg_command(
            tl, Path("s.wav"), Path(name), codec="c", audio_codec="a", crf=1, fps=25,
            audio_fade_out=0.0, output_format=None, ffmpeg_bin="ffmpeg",
        )  # fmt: skip
        assert "-movflags" in cmd
    mkv = build_ffmpeg_command(
        tl, Path("s.wav"), Path("o.mkv"), codec="c", audio_codec="a", crf=1, fps=25,
        audio_fade_out=0.0, output_format=None, ffmpeg_bin="ffmpeg",
    )  # fmt: skip
    assert "-movflags" not in mkv


def test_command_output_format_adds_f_before_output() -> None:
    tl = timeline_for(spec_scenario(), 19.5)
    assert "-f" not in command(tl)
    cmd = command(tl, output_format="matroska")
    assert cmd[-3:-1] == ["-f", "matroska"]
    assert cmd[-1] == "out.mp4"
    assert "-movflags" not in command(tl, output_format="matroska")
    assert "-movflags" in command(tl, output_format="mp4")


def test_command_audio_fade_out_propagated() -> None:
    tl = timeline_for(spec_scenario(), 19.5)
    assert "afade" not in option_value(command(tl), "-filter_complex")
    assert "afade=t=out:st=18:d=1.5" in option_value(
        command(tl, audio_fade_out=1.5), "-filter_complex"
    )


def test_command_is_all_strings_with_paths_unmodified() -> None:
    sc = Scenario((ent(S, "my pics/a b.png", 2), ent(L, "x/y.png", 2, 1)))
    cmd = command(timeline_for(sc, 3.0))
    assert all(isinstance(a, str) for a in cmd)
    assert "my pics/a b.png" in cmd  # one argv element: no shell quoting involved


# ------------------------------------------------------------------ -filter_complex_script


def long_timeline(audio: float = 3600.0) -> Timeline:
    """An hour of audio with 3 s slots: ~1500 slots, a graph well over 128 KiB."""
    sc = Scenario(
        (
            ent(S, "s.png", 3),
            ent(L, "a.png", 3, 1),
            ent(L, "b.png", 3, 1),
            ent(L, "c.png", 3),
        )
    )
    return timeline_for(sc, audio)


def test_filter_script_not_used_for_short_graph(tmp_path: Path) -> None:
    script = tmp_path / "graph.txt"
    cmd = command(timeline_for(spec_scenario(), 19.5), filter_script=script)
    assert "-filter_complex" in cmd
    assert "-filter_complex_script" not in cmd
    assert not script.exists()  # nothing is written unless needed


def test_filter_script_never_written_without_path() -> None:
    tl = long_timeline()
    graph = build_filter_complex(tl, tl.audio_duration, 0.0)
    assert len(graph) > FILTER_INLINE_LIMIT
    cmd = command(tl)  # no filter_script -> always inline, as before
    assert option_value(cmd, "-filter_complex") == graph
    assert "-filter_complex_script" not in cmd


def test_filter_script_used_for_very_long_audio(tmp_path: Path) -> None:
    tl = long_timeline()
    script = tmp_path / "graph.txt"
    graph = build_filter_complex(tl, tl.audio_duration, 0.0)
    assert len(graph) > 131072  # would not fit in one argv element on Linux

    cmd = command(tl, filter_script=script)

    assert "-filter_complex" not in cmd
    assert option_value(cmd, "-filter_complex_script") == str(script)
    assert max(len(a) for a in cmd) < 131072
    # the script holds exactly the same graph, one filter per line
    text = script.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.replace(";\n", ";").strip() == graph
    lines = text.splitlines()
    assert len(lines) == graph.count(";") + 1
    assert lines[-1].endswith("[afinal]")
    assert all(line.endswith(";") for line in lines[:-1])


def test_filter_script_command_otherwise_identical(tmp_path: Path) -> None:
    tl = long_timeline()
    inline = command(tl)
    scripted = command(tl, filter_script=tmp_path / "graph.txt")
    i = inline.index("-filter_complex")
    j = scripted.index("-filter_complex_script")
    assert i == j
    assert inline[:i] == scripted[:j]
    assert inline[i + 2 :] == scripted[j + 2 :]


def test_filter_script_threshold_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tl = timeline_for(spec_scenario(), 19.5)
    graph_len = len(build_filter_complex(tl, 19.5, 0.0))
    script = tmp_path / "graph.txt"
    monkeypatch.setattr("vidcarousel.ffmpeg.FILTER_INLINE_LIMIT", graph_len)  # not exceeded
    assert "-filter_complex_script" not in command(tl, filter_script=script)
    monkeypatch.setattr("vidcarousel.ffmpeg.FILTER_INLINE_LIMIT", graph_len - 1)  # exceeded
    assert "-filter_complex_script" in command(tl, filter_script=script)
    assert script.exists()


def test_filter_script_limit_leaves_headroom() -> None:
    assert FILTER_INLINE_LIMIT < 131072  # Linux MAX_ARG_STRLEN


def test_run_ffmpeg_logs_filter_script_contents_at_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "graph.txt"
    script.write_text("[0:v]null[vfinal];\n[1:a]anull[afinal]\n", encoding="utf-8")
    fake_run(monkeypatch)
    with capture_logs() as logs:
        run_ffmpeg(["ffmpeg", "-filter_complex_script", str(script), "out.mp4"])
    logged = [e for e in logs if e["event"] == "filter script"]
    assert len(logged) == 1
    assert logged[0]["log_level"] == "debug"
    assert logged[0]["path"] == str(script)
    assert "[0:v]null[vfinal];" in logged[0]["content"]


@pytest.mark.parametrize(
    "cmd",
    [
        ["ffmpeg", "-filter_complex_script", "/nonexistent/graph.txt", "out.mp4"],  # unreadable
        ["ffmpeg", "-filter_complex_script"],  # dangling flag
        ["ffmpeg", "-filter_complex", "[0:v]null[vfinal]", "out.mp4"],  # inline graph
    ],
)
def test_run_ffmpeg_tolerates_missing_or_absent_filter_script(
    monkeypatch: pytest.MonkeyPatch, cmd: list[str]
) -> None:
    fake_run(monkeypatch)
    with capture_logs() as logs:
        run_ffmpeg(cmd)  # logging the script must never break the run
    assert not [e for e in logs if e["event"] == "filter script"]


# ------------------------------------------------------------------ run_ffmpeg

CMD = ["ffmpeg", "-i", "in file.png", "out.mp4"]


def test_run_ffmpeg_success_logs_command_at_info(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch)
    with capture_logs() as logs:
        run_ffmpeg(CMD)
    assert calls == [CMD]  # passed through unmodified
    info = [e for e in logs if e["log_level"] == "info"]
    assert info[0]["command"] == "ffmpeg -i 'in file.png' out.mp4"  # shell-quoted for copy/paste
    assert not [e for e in logs if e["log_level"] == "error"]


def test_run_ffmpeg_success_logs_warnings_at_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, stderr="[warn] something odd\n")
    with capture_logs() as logs:
        run_ffmpeg(CMD)
    debug = [e for e in logs if e["log_level"] == "debug"]
    assert [e["stderr"] for e in debug] == ["[warn] something odd"]


def test_run_ffmpeg_success_without_stderr_logs_no_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch)
    with capture_logs() as logs:
        run_ffmpeg(CMD)
    assert not [e for e in logs if e["log_level"] == "debug"]


def test_run_ffmpeg_failure_raises_vc3003_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(
        monkeypatch, stderr="Error opening input file in.png\nInvalid argument\n", returncode=1
    )
    with capture_logs() as logs, pytest.raises(FfmpegError) as exc:
        run_ffmpeg(CMD)
    assert exc.value.code == "VC3003"
    assert exc.value.exit_code == 3
    assert "code 1" in exc.value.message
    assert "Error opening input file in.png" in exc.value.message
    assert "Invalid argument" in exc.value.message
    errors = [e for e in logs if e["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["returncode"] == 1
    assert errors[0]["stderr"] == "Error opening input file in.png\nInvalid argument"


def test_run_ffmpeg_failure_message_keeps_only_tail_but_log_keeps_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "\n".join(f"line {i}" for i in range(100))
    fake_run(monkeypatch, stderr=stderr, returncode=1)
    with capture_logs() as logs, pytest.raises(FfmpegError) as exc:
        run_ffmpeg(CMD)
    lines = exc.value.message.splitlines()
    assert lines[-1] == "line 99"
    assert "line 80" in lines and "line 79" not in lines
    assert len(lines) == 21  # header + last 20 lines
    logged = [e for e in logs if e["log_level"] == "error"][0]["stderr"]
    assert logged.splitlines()[0] == "line 0" and len(logged.splitlines()) == 100


def test_run_ffmpeg_failure_without_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, returncode=2)
    with pytest.raises(FfmpegError, match=r"code 2:\n\(no output\)"):
        run_ffmpeg(CMD)


def test_run_ffmpeg_killed_by_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, returncode=-9)
    with pytest.raises(FfmpegError, match="code -9") as exc:
        run_ffmpeg(CMD)
    assert exc.value.code == "VC3003"


@pytest.mark.parametrize("error", [FileNotFoundError("nope"), PermissionError("denied")])
def test_run_ffmpeg_binary_not_executable_vc3001(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    fake_run(monkeypatch, raises=error)
    with pytest.raises(FfmpegError) as exc:
        run_ffmpeg(["/no/ffmpeg", "-i", "x"])
    assert exc.value.code == "VC3001"
    assert "/no/ffmpeg" in exc.value.message


@pytest.mark.parametrize("returncode", [0, 1])
def test_run_ffmpeg_overwrite_refusal_vc4002(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    # ffmpeg exits 0 when it declines to overwrite, so the message is what gives it away.
    stderr = (
        "Input #0, wav, from 's.wav':\n"
        "File 'out.mp4' already exists. Exiting.\n"
        "Error opening output file out.mp4.\n"
    )
    fake_run(monkeypatch, stderr=stderr, returncode=returncode)
    with capture_logs() as logs, pytest.raises(OutputError) as exc:
        run_ffmpeg(CMD)
    assert exc.value.code == "VC4002"
    assert exc.value.exit_code == 4
    assert exc.value.message == "File 'out.mp4' already exists. Exiting."
    assert [e["log_level"] for e in logs if e["event"] == "ffmpeg finished"] == []
    assert [e for e in logs if e["log_level"] == "error"]
