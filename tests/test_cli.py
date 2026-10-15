from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from vidcarousel import cli
from vidcarousel.cli import _format_size, app
from vidcarousel.errors import FfmpegError, OutputError

from .conftest import EntryTuple

runner = CliRunner()


class Fakes:
    """Stand-ins for everything that would touch ffmpeg/ffprobe."""

    def __init__(self) -> None:
        self.dims: dict[str, tuple[int, int]] = {}  # by file name; default below
        self.default_dims = (64, 64)
        self.audio_duration = 30.0
        self.binary_checks: list[tuple[str, str]] = []
        self.probed_images: list[str] = []
        self.probed_audio: list[str] = []
        self.commands: list[list[str]] = []
        self.script_seen: str | None = None
        self.script_path: Path | None = None
        self.write_output = True
        self.run_error: Exception | None = None
        self.binaries_error: Exception | None = None


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    f = Fakes()

    def check_binaries(ffmpeg_bin: str, ffprobe_bin: str) -> None:
        f.binary_checks.append((ffmpeg_bin, ffprobe_bin))
        if f.binaries_error is not None:
            raise f.binaries_error

    def probe_image_dimensions(path: Path, ffprobe_bin: str = "ffprobe") -> tuple[int, int]:
        f.probed_images.append(path.name)
        return f.dims.get(path.name, f.default_dims)

    def probe_audio_duration(path: Path, ffprobe_bin: str = "ffprobe") -> float:
        f.probed_audio.append(str(path))
        return f.audio_duration

    def run_ffmpeg(cmd: list[str]) -> None:
        f.commands.append(cmd)
        if "-filter_complex_script" in cmd:
            f.script_path = Path(cmd[cmd.index("-filter_complex_script") + 1])
            f.script_seen = f.script_path.read_text(encoding="utf-8")
        if f.write_output:
            Path(cmd[-1]).write_bytes(b"x" * 2048)
        if f.run_error is not None:
            raise f.run_error

    monkeypatch.setattr(cli, "check_binaries", check_binaries)
    monkeypatch.setattr(cli, "probe_image_dimensions", probe_image_dimensions)
    monkeypatch.setattr(cli, "probe_audio_duration", probe_audio_duration)
    monkeypatch.setattr(cli, "run_ffmpeg", run_ffmpeg)
    return f


@pytest.fixture
def audio(dummy_audio: Callable[..., Path]) -> Path:
    return dummy_audio("song.wav", 1.0)


def invoke(scenario: Path, audio_file: Path, output: Path, *extra: str) -> Result:
    args = ["-ai", str(audio_file), "-vo", str(output), "-sf", str(scenario), *extra]
    return runner.invoke(app, args)


def option_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# ------------------------------------------------------------------ dry run


def test_dry_run_prints_timeline_and_creates_nothing(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    out = base / "out.mp4"
    result = invoke(scenario, audio, out, "--dry-run")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("=== vidcarousel dry-run ===\n")
    assert "Audio duration: 0:00:30.000" in result.stdout
    assert "image2.png" in result.stdout and "[loop 2]" in result.stdout
    assert "[silent]" in result.stdout
    assert "Total video duration:" in result.stdout
    assert not out.exists()
    assert fakes.commands == []
    assert fakes.probed_audio == [str(audio)]


def test_dry_run_stdout_is_clean_of_logs(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, audio, base / "out.mp4", "--dry-run", "-v")
    assert "[debug" not in result.stdout and "[info" not in result.stdout
    assert result.stdout.rstrip().endswith(result.stdout.splitlines()[-1])
    assert result.stdout.splitlines()[-1].startswith("Total video duration:")


def test_dry_run_does_not_need_a_free_output_path(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    existing = base / "out.mp4"
    existing.write_bytes(b"old")
    assert invoke(scenario, audio, existing, "--dry-run").exit_code == 0
    assert existing.read_bytes() == b"old"
    missing_dir = base / "nope" / "out.mp4"
    assert invoke(scenario, audio, missing_dir, "--dry-run").exit_code == 0


def test_dry_run_no_loop_repeat(fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, audio, base / "o.mp4", "--dry-run", "--no-loop-repeat")
    assert "no repeat" in result.stdout
    assert "[loop 2]" not in result.stdout


def test_dry_run_uses_default_transition_option(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, audio, base / "o.mp4", "--dry-run", "--transition", "wipeleft")
    assert "wipeleft 1.500 s" in result.stdout  # image1 has no override
    assert "dissolve 2.000 s" in result.stdout  # image2 keeps its own


# ------------------------------------------------------------------ validate only


def test_validate_only_ok(fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, audio, base / "out.mp4", "--validate-only")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "OK: 6 scenario entries, 6 images of 64x64"
    assert fakes.probed_audio == []  # audio is not needed
    assert fakes.commands == []


def test_validate_only_dimension_mismatch_vc2002(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    fakes.dims["image2.png"] = (32, 32)
    result = invoke(scenario, audio, base / "out.mp4", "--validate-only")
    assert result.exit_code == 2
    assert "error VC2002" in result.stderr
    assert "intro.png is 64x64" in result.stderr
    assert "image2.png is 32x32" in result.stderr
    assert result.stdout == ""


def test_dimension_mismatch_lists_at_most_five(
    fakes: Fakes,
    sample_scenario: Callable[[Sequence[EntryTuple]], Path],
    dummy_image: Callable[..., Path],
    audio: Path,
    tmp_path: Path,
) -> None:
    names = [f"i{n}.png" for n in range(9)]
    for name in names:
        dummy_image(name)
        fakes.dims[name] = (10 + len(fakes.dims), 10)  # all different
    entries: list[EntryTuple] = [("start", 5, None, None, names[0])]
    entries += [("loop", 4, None, None, n) for n in names[1:]]
    scenario = sample_scenario(entries)
    result = invoke(scenario, audio, tmp_path / "o.mp4", "--validate-only")
    assert result.exit_code == 2
    assert "i5.png is" in result.stderr and "i6.png is" not in result.stderr
    assert "and 3 more" in result.stderr


def test_each_distinct_image_probed_once(
    fakes: Fakes,
    sample_scenario: Callable[[Sequence[EntryTuple]], Path],
    dummy_image: Callable[..., Path],
    audio: Path,
    tmp_path: Path,
) -> None:
    dummy_image("a.png")
    dummy_image("b.png")
    scenario = sample_scenario(
        [
            ("start", 5, None, None, "a.png"),
            ("loop", 4, 1, None, "b.png"),
            ("loop", 4, 1, None, "a.png"),
            ("end", 3, 1, None, "b.png"),
        ]
    )
    result = invoke(scenario, audio, tmp_path / "o.mp4", "--validate-only")
    assert result.exit_code == 0
    assert fakes.probed_images == ["a.png", "b.png"]
    assert "2 images of 64x64" in result.stdout


# ------------------------------------------------------------------ error exit codes


def test_missing_image_vc2001(fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path) -> None:
    base, scenario = scenario_dir
    (base / "image3.png").unlink()
    result = invoke(scenario, audio, base / "out.mp4", "--validate-only")
    assert result.exit_code == 2
    assert "error VC2001" in result.stderr and "image3.png" in result.stderr


def test_scenario_error_exit_code_1(fakes: Fakes, tmp_path: Path, audio: Path) -> None:
    scenario = tmp_path / "s.txt"
    scenario.write_text("start,5 a.png\n", encoding="utf-8")  # no loop entries
    result = invoke(scenario, audio, tmp_path / "o.mp4", "--validate-only")
    assert result.exit_code == 1
    assert "error VC1002" in result.stderr


def test_unknown_transition_in_scenario_vc1009(
    fakes: Fakes, tmp_path: Path, audio: Path, dummy_image: Callable[..., Path]
) -> None:
    dummy_image("a.png")
    scenario = tmp_path / "s.txt"
    scenario.write_text("start,5 a.png\nloop,4,1,bogus a.png\n", encoding="utf-8")
    result = invoke(scenario, audio, tmp_path / "o.mp4", "--validate-only")
    assert result.exit_code == 1
    assert "error VC1009" in result.stderr


def test_unknown_default_transition_option_vc1009(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, audio, base / "o.mp4", "--transition", "bogus", "--dry-run")
    assert result.exit_code == 1
    assert "error VC1009" in result.stderr and "bogus" in result.stderr


def test_missing_scenario_file_vc2004(fakes: Fakes, audio: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path / "nope.txt", audio, tmp_path / "o.mp4")
    assert result.exit_code == 2
    assert "error VC2004" in result.stderr


def test_missing_audio_file_vc2003(fakes: Fakes, scenario_dir: tuple[Path, Path]) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, base / "nope.wav", base / "o.mp4", "--dry-run")
    assert result.exit_code == 2
    assert "error VC2003" in result.stderr


def test_missing_binaries_vc3001(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    fakes.binaries_error = FfmpegError("VC3001", "ffmpeg binary not found: 'ffmpeg'")
    result = invoke(scenario, audio, base / "o.mp4", "--dry-run")
    assert result.exit_code == 3
    assert "error VC3001" in result.stderr


REQUIRED = ["-ai", "a.wav", "-vo", "o.mp4", "-sf", "s.txt"]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["-ai", "a.wav"],
        ["-ai", "a.wav", "-vo", "o.mp4"],
        [*REQUIRED, "--crf", "-1"],
        [*REQUIRED, "--fps", "0"],
        [*REQUIRED, "--audio-fade-out", "-1"],
        [*REQUIRED, "--no-such-option"],
    ],
)
def test_usage_errors_exit_2(fakes: Fakes, args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2  # Click's conventional usage-error code
    assert result.stdout == ""
    assert fakes.commands == []


# ------------------------------------------------------------------ rendering


def test_render_success(fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path) -> None:
    base, scenario = scenario_dir
    out = base / "out.mp4"
    result = invoke(scenario, audio, out)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == f"Wrote {out} (2.0 KiB, 42.000 s)"
    assert len(fakes.commands) == 1
    cmd = fakes.commands[0]
    assert cmd[0] == "ffmpeg" and cmd[-1] == str(out)
    assert option_value(cmd, "-c:v") == "libx264"
    assert option_value(cmd, "-c:a") == "aac"
    assert option_value(cmd, "-crf") == "23"
    assert option_value(cmd, "-r") == "25"
    assert "-f" not in cmd  # extension is inferable
    assert "afade" not in option_value(cmd, "-filter_complex")
    assert fakes.binary_checks == [("ffmpeg", "ffprobe")]


def test_render_propagates_options(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    result = invoke(
        scenario,
        audio,
        base / "out.mp4",
        "--codec", "libx265",
        "--audio-codec", "libopus",
        "--crf", "30",
        "--fps", "30",
        "--transition", "wipeleft",
        "--audio-fade-out", "2",
        "--ffmpeg", "/opt/ff/ffmpeg",
        "--ffprobe", "/opt/ff/ffprobe",
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    cmd = fakes.commands[0]
    assert cmd[0] == "/opt/ff/ffmpeg"
    assert option_value(cmd, "-c:v") == "libx265"
    assert option_value(cmd, "-c:a") == "libopus"
    assert option_value(cmd, "-crf") == "30"
    assert option_value(cmd, "-r") == "30"
    graph = option_value(cmd, "-filter_complex")
    assert "fps=30" in graph
    assert "transition=wipeleft" in graph and "transition=dissolve" in graph
    assert "afade=t=out:st=28:d=2" in graph  # audible section is 30 s
    assert fakes.binary_checks == [("/opt/ff/ffmpeg", "/opt/ff/ffprobe")]


def test_render_no_loop_repeat(fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path) -> None:
    base, scenario = scenario_dir
    repeat = invoke(scenario, audio, base / "a.mp4")
    hold = invoke(scenario, audio, base / "b.mp4", "--no-loop-repeat")
    assert repeat.exit_code == hold.exit_code == 0
    assert fakes.commands[0].count("-loop") > fakes.commands[1].count("-loop")


@pytest.mark.parametrize(
    ("name", "extra", "expected_format"),
    [
        ("out.mp4", [], None),  # inferable from the extension
        ("out", [], "mp4"),  # no extension -> default mp4
        ("out", ["-of", "matroska"], "matroska"),
        ("out.mp4", ["--output-format", "mov"], "mov"),  # explicit choice wins
    ],
)
def test_output_format_handling(
    fakes: Fakes,
    scenario_dir: tuple[Path, Path],
    audio: Path,
    name: str,
    extra: list[str],
    expected_format: str | None,
) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, audio, base / name, *extra)
    assert result.exit_code == 0, result.stderr
    cmd = fakes.commands[0]
    if expected_format is None:
        assert "-f" not in cmd
    else:
        assert option_value(cmd, "-f") == expected_format


def test_output_exists_vc4002(fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path) -> None:
    base, scenario = scenario_dir
    out = base / "out.mp4"
    out.write_bytes(b"precious")
    result = invoke(scenario, audio, out)
    assert result.exit_code == 4
    assert "error VC4002" in result.stderr
    assert out.read_bytes() == b"precious"
    assert fakes.commands == []


def test_output_directory_missing_vc4001(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    result = invoke(scenario, audio, base / "no_such_dir" / "out.mp4")
    assert result.exit_code == 4
    assert "error VC4001" in result.stderr
    assert fakes.commands == []


def test_ffmpeg_failure_vc3003_removes_partial_output(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    out = base / "out.mp4"
    fakes.run_error = FfmpegError("VC3003", "ffmpeg exited with code 1:\nboom")
    result = invoke(scenario, audio, out)
    assert result.exit_code == 3
    assert "error VC3003" in result.stderr and "boom" in result.stderr
    assert not out.exists()  # the half-written file is cleaned up
    assert result.stdout == ""


def test_overwrite_refusal_keeps_existing_file(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    out = base / "out.mp4"
    # A file appeared after our check; ffmpeg declined to overwrite it.
    fakes.run_error = OutputError("VC4002", "File 'out.mp4' already exists. Exiting.")
    result = invoke(scenario, audio, out)
    assert result.exit_code == 4
    assert out.exists()  # not ours: must not be deleted


def test_success_without_output_file_is_an_error(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    fakes.write_output = False
    result = invoke(scenario, audio, base / "out.mp4")
    assert result.exit_code == 3
    assert "was not created" in result.stderr


def test_long_filter_graph_goes_through_temporary_script(
    fakes: Fakes,
    scenario_dir: tuple[Path, Path],
    audio: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, scenario = scenario_dir
    monkeypatch.setattr("vidcarousel.ffmpeg.FILTER_INLINE_LIMIT", 0)
    result = invoke(scenario, audio, base / "out.mp4")
    assert result.exit_code == 0, result.stderr
    cmd = fakes.commands[0]
    assert "-filter_complex" not in cmd and "-filter_complex_script" in cmd
    assert fakes.script_seen is not None and fakes.script_seen.rstrip().endswith("[afinal]")
    assert fakes.script_path is not None
    assert not fakes.script_path.exists()  # temporary file is gone afterwards
    assert not fakes.script_path.parent.exists()
    assert not list(base.glob("**/filter_graph.txt"))  # and never lands next to the output


def test_short_filter_graph_leaves_no_script(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    invoke(scenario, audio, base / "out.mp4")
    assert fakes.script_path is None
    assert "-filter_complex" in fakes.commands[0]


# ------------------------------------------------------------------ logging


def test_verbose_enables_debug_logging_on_stderr(
    fakes: Fakes, scenario_dir: tuple[Path, Path], audio: Path
) -> None:
    base, scenario = scenario_dir
    (base / "image3.png").unlink()  # any failing run logs a debug record
    quiet = invoke(scenario, audio, base / "o.mp4", "--validate-only")
    loud = invoke(scenario, audio, base / "o.mp4", "--validate-only", "--verbose")
    assert "[debug" not in quiet.stderr
    assert "[debug" in loud.stderr and "VC2001" in loud.stderr
    assert "[debug" not in loud.stdout


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 bytes"),
        (1023, "1023 bytes"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (5 * 1024**2, "5.0 MiB"),
        (3 * 1024**3, "3.0 GiB"),
        (2 * 1024**4, "2048.0 GiB"),
    ],
)
def test_format_size(size: int, expected: str) -> None:
    assert _format_size(size) == expected


def test_help_explains_scenario_syntax_and_exit_codes() -> None:
    flat = " ".join(runner.invoke(app, ["--help"]).stdout.split())
    assert "type,display_time[,xfade_time[,transition]] image-file" in flat
    assert "display_time includes the incoming crossfade" in flat
    assert "--dry-run to preview" in flat
    assert "Exit codes: 0 success; 1 scenario error (VC1xxx)" in flat
    assert "4 output error (VC4xxx)" in flat


def test_help_lists_all_documented_options() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    flat = " ".join(result.stdout.split())
    for flag in (
        "--audio-input", "-ai", "--video-output", "-vo", "--scenario-file", "-sf",
        "--output-format", "-of", "--codec", "--audio-codec", "--crf", "--fps",
        "--transition", "--audio-fade-out", "--no-loop-repeat", "--dry-run",
        "--validate-only", "--verbose", "-v", "--ffmpeg", "--ffprobe",
    ):  # fmt: skip
        assert flag in flat, flag
