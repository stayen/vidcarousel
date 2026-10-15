"""End-to-end tests against real ffmpeg / ffprobe.

Skipped by default (``-m 'not integration'`` in pyproject.toml) and when the
binaries are missing.  Run with ``pytest -m integration``.
"""

from __future__ import annotations

import array
import json
import math
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from vidcarousel.cli import app

from .conftest import make_silent_wav, make_solid_png

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg and ffprobe are required",
    ),
]

runner = CliRunner()

RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)
YELLOW, CYAN, MAGENTA = (255, 255, 0), (0, 255, 255), (255, 0, 255)
PALETTE = {
    "red": RED, "green": GREEN, "blue": BLUE,
    "yellow": YELLOW, "cyan": CYAN, "magenta": MAGENTA,
}  # fmt: skip
FPS = 25
SIZE = (64, 48)


# ------------------------------------------------------------------ helpers


def make_image(directory: Path, name: str, colour: tuple[int, int, int]) -> None:
    make_solid_png(directory / name, SIZE[0], SIZE[1], *colour)


def write_scenario(directory: Path, text: str) -> Path:
    path = directory / "scenario.txt"
    path.write_text(text, encoding="utf-8")
    return path


def render(
    directory: Path, scenario: Path, audio: Path, name: str = "out.mp4", *extra: str
) -> Result:
    args = ["-ai", str(audio), "-vo", str(directory / name), "-sf", str(scenario), *extra]
    return runner.invoke(app, args)


def ffprobe_json(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data: dict[str, Any] = json.loads(out)
    return data


def container_duration(path: Path) -> float:
    return float(ffprobe_json(path)["format"]["duration"])


def format_name(path: Path) -> str:
    return str(ffprobe_json(path)["format"]["format_name"])


def stream(path: Path, kind: str) -> dict[str, str]:
    found = [s for s in ffprobe_json(path)["streams"] if s["codec_type"] == kind]
    assert len(found) == 1, f"expected one {kind} stream, got {len(found)}"
    result: dict[str, str] = found[0]
    return result


def frames(path: Path) -> list[tuple[int, int, int]]:
    """Average RGB colour of every video frame (images are solid colours)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=1:1,format=rgb24",
         "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]


def nearest(colour: tuple[int, int, int]) -> str:
    return min(
        PALETTE, key=lambda n: sum((a - b) ** 2 for a, b in zip(colour, PALETTE[n], strict=True))
    )


def at(fr: list[tuple[int, int, int]], seconds: float) -> str:
    return nearest(fr[round(seconds * FPS)])


def audio_samples(path: Path) -> array.array[int]:
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn"]
    cmd += ["-f", "s16le", "-ac", "1", "-ar", "8000", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def rms(samples: array.array[int], start: float, end: float, rate: int = 8000) -> float:
    window = samples[int(start * rate) : int(end * rate)]
    return math.sqrt(sum(s * s for s in window) / max(len(window), 1))


def make_tone(path: Path, seconds: float, rate: int = 44100) -> None:
    """Constant-amplitude 440 Hz sine, 16-bit mono WAV."""
    n = int(rate * seconds)
    data = b"".join(
        struct.pack("<h", int(16000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n)
    )
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        *(b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16),
        *(b"data", len(data)),
    )
    path.write_bytes(header + data)


def dry_run_total(stdout: str) -> float:
    match = re.search(r"Total video duration: (\d+):(\d\d):(\d\d\.\d+)", stdout)
    assert match, stdout
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """tmp_path holding one solid-colour PNG per palette colour."""
    for name, colour in PALETTE.items():
        make_image(tmp_path, f"{name}.png", colour)
    return tmp_path


# ------------------------------------------------------------------ rendering


def test_minimal_scenario_renders_valid_mp4(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 4.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\n")
    result = render(media, scenario, audio)
    assert result.exit_code == 0, result.stderr
    out = media / "out.mp4"
    assert result.stdout.startswith(f"Wrote {out} (")
    assert container_duration(out) == pytest.approx(4.0, abs=0.1)
    video, aud = stream(out, "video"), stream(out, "audio")
    assert video["codec_name"] == "h264" and aud["codec_name"] == "aac"
    assert (int(video["width"]), int(video["height"])) == SIZE
    assert video["pix_fmt"] == "yuv420p"
    assert video["r_frame_rate"] == "25/1"


def test_frames_follow_the_timeline(media: Path) -> None:
    # red 0-2 | green 2-4 (hard cut) | blue 3-6, fading in 3-4 | yellow 6-8 (cut) | cyan 8-10 (cut)
    audio = media / "a.wav"
    make_silent_wav(audio, 6.0)
    scenario = write_scenario(
        media,
        "start,2 red.png\nloop,2 green.png\nloop,3,1 blue.png\n"
        "credits,2 yellow.png\nend,2 cyan.png\n",
    )
    result = render(media, scenario, audio)
    assert result.exit_code == 0, result.stderr
    fr = frames(media / "out.mp4")
    assert len(fr) == pytest.approx(10 * FPS, abs=2)
    assert [at(fr, t) for t in (0.0, 1.0)] == ["red", "red"]
    # hard cut at exactly 2.0 s (frame 50)
    assert nearest(fr[48]) == "red" and nearest(fr[51]) == "green"
    assert at(fr, 2.5) == "green"
    # crossfade green -> blue over 3.0-4.0 s: halfway both are present, no red
    r, g, b = fr[round(3.5 * FPS)]
    assert r < 60 and 70 < g < 190 and 70 < b < 190, (r, g, b)
    assert at(fr, 5.0) == "blue"
    # silent tail
    assert at(fr, 7.0) == "yellow" and at(fr, 9.5) == "cyan"
    assert at(fr, 9.95) == "cyan"
    assert container_duration(media / "out.mp4") == pytest.approx(10.0, abs=0.1)


def test_silent_tail_has_no_audio_and_duration_matches_dry_run(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 12.5)
    scenario = write_scenario(
        media,
        "start,3 red.png\nloop,4,1 green.png\nloop,4,1,dissolve blue.png\nloop,3 yellow.png\n"
        "credits,4,1 cyan.png\nend,2,1 magenta.png\n",
    )
    dry = runner.invoke(
        app, ["-ai", str(audio), "-vo", str(media / "out.mp4"), "-sf", str(scenario), "--dry-run"]
    )
    assert dry.exit_code == 0, dry.stderr
    expected_total = dry_run_total(dry.stdout)

    result = render(media, scenario, audio)
    assert result.exit_code == 0, result.stderr
    out = media / "out.mp4"
    assert container_duration(out) == pytest.approx(expected_total, abs=0.1)
    # the audio stream stops where the audible section ends; the video runs on
    assert float(stream(out, "audio")["duration"]) == pytest.approx(12.5, abs=0.1)
    assert float(stream(out, "video")["duration"]) == pytest.approx(expected_total, abs=0.1)
    assert expected_total > 12.5
    assert at(frames(out), expected_total - 0.2) == "magenta"


def test_video_ends_exactly_with_the_audio_when_last_image_is_cut_short(media: Path) -> None:
    # green would be on screen for 4 s but the audio ends after 3 s of its slot.
    audio = media / "a.wav"
    make_silent_wav(audio, 4.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,4,1 green.png\n")
    result = render(media, scenario, audio)
    assert result.exit_code == 0, result.stderr
    out = media / "out.mp4"
    assert container_duration(out) == pytest.approx(4.0, abs=0.1)  # not 5.0
    assert float(stream(out, "video")["duration"]) == pytest.approx(
        float(stream(out, "audio")["duration"]), abs=0.1
    )
    assert at(frames(out), 3.9) == "green"


def test_loop_carousel_repeats_cyclically(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 10.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\nloop,2 blue.png\n")
    result = render(media, scenario, audio)
    assert result.exit_code == 0, result.stderr
    fr = frames(media / "out.mp4")
    assert [at(fr, t) for t in (1, 3, 5, 7, 9)] == ["red", "green", "blue", "green", "blue"]
    assert container_duration(media / "out.mp4") == pytest.approx(10.0, abs=0.1)


def test_no_loop_repeat_holds_last_image(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 10.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\nloop,2 blue.png\n")
    result = render(media, scenario, audio, "out.mp4", "--no-loop-repeat")
    assert result.exit_code == 0, result.stderr
    fr = frames(media / "out.mp4")
    assert [at(fr, t) for t in (1, 3, 5, 7, 9, 9.9)] == [
        "red",
        "green",
        "blue",
        "blue",
        "blue",
        "blue",
    ]
    assert container_duration(media / "out.mp4") == pytest.approx(10.0, abs=0.1)


def test_audio_is_trimmed_and_fades_out(media: Path) -> None:
    audio = media / "tone.wav"
    make_tone(audio, 6.0)
    scenario = write_scenario(media, "start,3 red.png\nloop,3 green.png\ncredits,3 blue.png\n")
    result = render(media, scenario, audio, "out.mp4", "--audio-fade-out", "1.5")
    assert result.exit_code == 0, result.stderr
    out = media / "out.mp4"
    assert container_duration(out) == pytest.approx(9.0, abs=0.1)
    assert float(stream(out, "audio")["duration"]) == pytest.approx(6.0, abs=0.1)
    pcm = audio_samples(out)
    full = rms(pcm, 1.0, 3.0)
    assert full > 5000  # the tone is audible
    assert rms(pcm, 5.7, 6.0) < full * 0.2  # ...and has faded out by the end of the audible part
    assert rms(pcm, 6.2, 8.5) < 100  # silent tail


def test_audio_without_fade_out_stays_loud_to_the_end(media: Path) -> None:
    audio = media / "tone.wav"
    make_tone(audio, 4.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\n")
    assert render(media, scenario, audio).exit_code == 0
    pcm = audio_samples(media / "out.mp4")
    assert rms(pcm, 3.5, 3.9) > rms(pcm, 1.0, 2.0) * 0.8


def test_tail_crossfade_clamped_when_audio_ends_right_after_an_image_starts(media: Path) -> None:
    # Audio ends 0.5 s into green's slot; credits wants a 2 s fade -> shortened to green's 1.5 s.
    audio = media / "a.wav"
    make_silent_wav(audio, 3.5)
    scenario = write_scenario(media, "start,3 red.png\nloop,3,1 green.png\ncredits,4,2 blue.png\n")
    dry = runner.invoke(
        app, ["-ai", str(audio), "-vo", str(media / "o.mp4"), "-sf", str(scenario), "--dry-run"]
    )
    result = render(media, scenario, audio)
    assert dry.exit_code == 0 and result.exit_code == 0, result.stderr
    out = media / "out.mp4"
    assert container_duration(out) == pytest.approx(dry_run_total(dry.stdout), abs=0.1)
    assert at(frames(out), dry_run_total(dry.stdout) - 0.2) == "blue"


def test_mixed_png_and_jpeg_inputs(media: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(media / "blue.png"), "-pix_fmt", "yuvj420p",
         str(media / "blue.jpg")],
        check=True,
    )  # fmt: skip
    audio = media / "a.wav"
    make_silent_wav(audio, 6.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,3,1 blue.jpg\nloop,3,1 green.png\n")
    result = render(media, scenario, audio)
    assert result.exit_code == 0, result.stderr
    fr = frames(media / "out.mp4")
    assert [at(fr, t) for t in (0.5, 3.0, 5.5)] == ["red", "blue", "green"]


def test_output_format_option_without_extension(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 3.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\n")
    result = render(media, scenario, audio, "movie", "-of", "matroska")
    assert result.exit_code == 0, result.stderr
    assert "matroska" in format_name(media / "movie")
    result = render(media, scenario, audio, "movie2")  # no extension, no -of: mp4
    assert result.exit_code == 0, result.stderr
    assert "mp4" in format_name(media / "movie2")


def test_codec_and_quality_options(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 2.0)
    scenario = write_scenario(media, "start,1 red.png\nloop,1 green.png\n")
    result = render(media, scenario, audio, "out.mp4", "--crf", "40", "--fps", "10")
    assert result.exit_code == 0, result.stderr
    assert stream(media / "out.mp4", "video")["r_frame_rate"] == "10/1"


def test_very_long_graph_script_route_with_real_ffmpeg(
    media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vidcarousel.ffmpeg.FILTER_INLINE_LIMIT", 0)  # force -filter_complex_script
    audio = media / "a.wav"
    make_silent_wav(audio, 8.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2,1 green.png\nloop,2,1 blue.png\n")
    result = render(media, scenario, audio, "out.mp4", "-v")
    assert result.exit_code == 0, result.stderr
    assert "filter script" in result.stderr  # the graph was logged from the script file
    assert container_duration(media / "out.mp4") == pytest.approx(8.0, abs=0.1)
    assert not list(media.glob("**/filter_graph.txt"))


# ------------------------------------------------------------------ dry run / validation / errors


def test_dry_run_with_real_ffprobe(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 7.5)
    scenario = write_scenario(media, "start,3 red.png\nloop,4,1 green.png\ncredits,2,1 blue.png\n")
    out = media / "out.mp4"
    result = runner.invoke(
        app, ["-ai", str(audio), "-vo", str(out), "-sf", str(scenario), "--dry-run"]
    )
    assert result.exit_code == 0, result.stderr
    assert "Audio duration: 0:00:07.500" in result.stdout
    assert "green.png" in result.stdout and "[silent]" in result.stdout
    assert dry_run_total(result.stdout) > 7.5
    assert not out.exists()


def test_validate_only_with_real_ffprobe(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 1.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\n")
    result = render(media, scenario, audio, "out.mp4", "--validate-only")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "OK: 2 scenario entries, 2 images of 64x48"
    assert not (media / "out.mp4").exists()


def test_validate_only_detects_dimension_mismatch(media: Path) -> None:
    make_solid_png(media / "odd.png", 32, 24)
    audio = media / "a.wav"
    make_silent_wav(audio, 1.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 odd.png\n")
    result = render(media, scenario, audio, "out.mp4", "--validate-only")
    assert result.exit_code == 2
    assert "error VC2002" in result.stderr and "odd.png is 32x24" in result.stderr


def test_missing_audio_exits_2(media: Path) -> None:
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\n")
    result = render(media, scenario, media / "nope.wav")
    assert result.exit_code == 2
    assert "error VC2003" in result.stderr
    assert not (media / "out.mp4").exists()


def test_corrupt_audio_is_a_probe_failure(media: Path) -> None:
    (media / "junk.wav").write_bytes(b"this is not audio")
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\n")
    result = render(media, scenario, media / "junk.wav", "out.mp4", "--dry-run")
    assert result.exit_code == 3
    assert "error VC3002" in result.stderr


def test_existing_output_is_never_overwritten(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 2.0)
    scenario = write_scenario(media, "start,1 red.png\nloop,1 green.png\n")
    (media / "out.mp4").write_bytes(b"precious")
    result = render(media, scenario, audio)
    assert result.exit_code == 4
    assert "error VC4002" in result.stderr
    assert (media / "out.mp4").read_bytes() == b"precious"


def test_failed_ffmpeg_leaves_no_partial_output(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 2.0)
    scenario = write_scenario(media, "start,1 red.png\nloop,1 green.png\n")
    result = render(media, scenario, audio, "out.mp4", "--codec", "no_such_encoder")
    assert result.exit_code == 3
    assert "error VC3003" in result.stderr
    assert not (media / "out.mp4").exists()


# ------------------------------------------------------------------ installed entry point


@pytest.mark.skipif(
    shutil.which("vidcarousel") is None, reason="vidcarousel script is not installed"
)
def test_installed_script_separates_stdout_and_stderr(media: Path) -> None:
    audio = media / "a.wav"
    make_silent_wav(audio, 3.0)
    scenario = write_scenario(media, "start,2 red.png\nloop,2 green.png\n")
    base = ["vidcarousel", "-ai", str(audio), "-vo", str(media / "out.mp4"), "-sf", str(scenario)]

    dry = subprocess.run([*base, "--dry-run"], capture_output=True, text=True)
    assert dry.returncode == 0 and dry.stderr == ""
    assert dry.stdout.startswith("=== vidcarousel dry-run ===")

    done = subprocess.run(base, capture_output=True, text=True)
    assert done.returncode == 0
    assert done.stdout.startswith("Wrote ") and "running ffmpeg" in done.stderr
    assert "\x1b[" not in done.stderr  # no colour codes when not a terminal

    again = subprocess.run(base, capture_output=True, text=True)
    assert again.returncode == 4 and again.stdout == ""
    assert again.stderr.startswith("error VC4002")
