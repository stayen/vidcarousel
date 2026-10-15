"""Command-line interface (CLAUDE.md § 3, § 8.6).

Stats and dry-run output go to stdout; structured logs and error messages go to
stderr.  Exit codes: 0 success, 1 scenario error, 2 input error, 3 ffmpeg error,
4 output error (see ``errors.py``).  Command-line usage errors (missing or
malformed options) are reported by Typer/Click with their conventional exit code 2.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import structlog
import typer

from vidcarousel.errors import (
    FfmpegError,
    InputFileError,
    OutputError,
    ScenarioError,
    VidCarouselError,
)
from vidcarousel.ffmpeg import (
    build_ffmpeg_command,
    check_binaries,
    get_valid_transitions,
    probe_audio_duration,
    probe_image_dimensions,
    run_ffmpeg,
)
from vidcarousel.models import Scenario
from vidcarousel.scenario import parse_scenario
from vidcarousel.timeline import compute_timeline, format_dry_run

_EPILOG = (
    "Exit codes: 0 success; 1 scenario error (VC1xxx); 2 input error (VC2xxx) or "
    "command-line usage error; 3 ffmpeg/ffprobe error (VC3xxx); 4 output error (VC4xxx). "
    "Errors are printed to stderr as 'error VCnnnn: message'. "
    "Dry-run and validation results go to stdout, logs to stderr."
)

app = typer.Typer(add_completion=False)

log = structlog.get_logger(__name__)

_MAX_LISTED_MISMATCHES = 5


def configure_logging(verbose: bool = False) -> None:
    """Send structured logs to stderr (INFO, or DEBUG when verbose).

    Colour is only used when stderr is a terminal, so redirected logs stay plain text.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{num_bytes} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------- validation


def _require_readable_file(
    path: Path, *, error: type[VidCarouselError], code: str, what: str
) -> None:
    if not path.is_file() or not os.access(path, os.R_OK):
        raise error(code, f"{what} not found or unreadable: {path}")


def _check_output_path(path: Path) -> None:
    """The output's directory must be writable and the file must not exist yet."""
    parent = path.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise OutputError("VC4001", f"output directory missing or not writable: {parent}")
    if path.exists():
        raise OutputError("VC4002", f"output file already exists (delete it first): {path}")


def _check_transition(name: str, valid: set[str]) -> None:
    if name not in valid:
        raise ScenarioError(
            "VC1009",
            f"unknown --transition {name!r}; valid names: {', '.join(sorted(valid))}",
        )


def _check_dimensions(scenario: Scenario, ffprobe_bin: str) -> tuple[int, int]:
    """Probe every distinct image and require identical pixel dimensions (SC10)."""
    paths = list(dict.fromkeys(entry.image_path for entry in scenario.entries))
    dims = {path: probe_image_dimensions(path, ffprobe_bin) for path in paths}

    expected = dims[paths[0]]
    mismatched = [p for p in paths if dims[p] != expected]
    if mismatched:
        listed = [f"{p.name} is {dims[p][0]}x{dims[p][1]}" for p in mismatched]
        extra = len(listed) - _MAX_LISTED_MISMATCHES
        if extra > 0:
            listed = [*listed[:_MAX_LISTED_MISMATCHES], f"and {extra} more"]
        raise InputFileError(
            "VC2002",
            f"all images must have the same dimensions: {paths[0].name} is "
            f"{expected[0]}x{expected[1]}, but {', '.join(listed)}",
        )
    return expected


# --------------------------------------------------------------------- command


@app.command(epilog=_EPILOG)
def main(
    audio_input: Annotated[
        Path,
        typer.Option(
            "--audio-input", "-ai", help="Input audio file (any format ffmpeg can decode)."
        ),
    ],
    video_output: Annotated[
        Path, typer.Option("--video-output", "-vo", help="Output video file (must not exist yet).")
    ],
    scenario_file: Annotated[
        Path,
        typer.Option("--scenario-file", "-sf", help="Scenario file (image sequence and timing)."),
    ],
    output_format: Annotated[
        str | None,
        typer.Option(
            "--output-format",
            "-of",
            help="Container format. Used when given, or when the output filename has no "
            "extension (then defaults to mp4); otherwise ffmpeg infers it from the extension.",
        ),
    ] = None,
    codec: Annotated[str, typer.Option("--codec", help="ffmpeg video encoder.")] = "libx264",
    audio_codec: Annotated[
        str, typer.Option("--audio-codec", help="ffmpeg audio encoder.")
    ] = "aac",
    crf: Annotated[
        int,
        typer.Option(
            "--crf",
            min=0,
            help="Constant Rate Factor: quality, lower is better (0-51 for libx264/libx265).",
        ),
    ] = 23,
    fps: Annotated[int, typer.Option("--fps", min=1, help="Output frame rate.")] = 25,
    transition: Annotated[
        str,
        typer.Option(
            "--transition",
            help="Default ffmpeg xfade transition (fade, dissolve, wipeleft, slideup, ...) for "
            "entries that don't name their own.",
        ),
    ] = "fade",
    audio_fade_out: Annotated[
        float,
        typer.Option(
            "--audio-fade-out",
            min=0.0,
            help="Seconds over which the audio fades out at the end of the audible "
            "section (0 = hard cut).",
        ),
    ] = 0.0,
    no_loop_repeat: Annotated[
        bool,
        typer.Option(
            "--no-loop-repeat",
            help="Hold the last loop image for the remaining audio instead of cycling.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the resolved timeline and exit without rendering."),
    ] = False,
    validate_only: Annotated[
        bool,
        typer.Option(
            "--validate-only",
            help="Check the scenario, input files and image dimensions, then exit.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at DEBUG level.")] = False,
    ffmpeg: Annotated[str, typer.Option("--ffmpeg", help="Path to the ffmpeg binary.")] = "ffmpeg",
    ffprobe: Annotated[
        str, typer.Option("--ffprobe", help="Path to the ffprobe binary.")
    ] = "ffprobe",
) -> None:
    """Build a music video from an audio file and a carousel of same-size images.

    The scenario file lists the images and their timing, one per line:

    \b
      type,display_time[,xfade_time[,transition]]  image-file
      start,5                  intro.png
      loop,4,1.5               image1.png
      loop,6,2,dissolve        image2.png
      credits,10,1.5           credits.png
      end,5,1.5                final.png

    The 'start' image comes first, the 'loop' images then cycle for as long as the
    audio lasts, and the optional 'credits' and 'end' images follow without sound.
    Times are in seconds; display_time includes the incoming crossfade (xfade_time).
    Use --dry-run to preview the resolved timeline before rendering.
    """
    configure_logging(verbose)
    try:
        _run(
            audio_input=audio_input,
            video_output=video_output,
            scenario_file=scenario_file,
            output_format=output_format,
            codec=codec,
            audio_codec=audio_codec,
            crf=crf,
            fps=fps,
            transition=transition,
            audio_fade_out=audio_fade_out,
            loop_repeat=not no_loop_repeat,
            dry_run=dry_run,
            validate_only=validate_only,
            ffmpeg_bin=ffmpeg,
            ffprobe_bin=ffprobe,
        )
    except VidCarouselError as exc:
        log.debug("failed", code=exc.code, message=exc.message)
        typer.echo(f"error {exc.code}: {exc.message}", err=True)
        raise typer.Exit(exc.exit_code) from None


def _run(
    *,
    audio_input: Path,
    video_output: Path,
    scenario_file: Path,
    output_format: str | None,
    codec: str,
    audio_codec: str,
    crf: int,
    fps: int,
    transition: str,
    audio_fade_out: float,
    loop_repeat: bool,
    dry_run: bool,
    validate_only: bool,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> None:
    rendering = not (dry_run or validate_only)
    valid_transitions = get_valid_transitions()

    # 1. Validate paths and options.
    _check_transition(transition, valid_transitions)
    _require_readable_file(scenario_file, error=InputFileError, code="VC2004", what="scenario file")
    _require_readable_file(audio_input, error=InputFileError, code="VC2003", what="audio file")
    if rendering:
        _check_output_path(video_output)

    # 2. Parse the scenario (also checks that every image exists).
    scenario = parse_scenario(scenario_file, valid_transitions)

    # 3. ffmpeg / ffprobe available?
    check_binaries(ffmpeg_bin, ffprobe_bin)

    # 4. All images the same size?
    width, height = _check_dimensions(scenario, ffprobe_bin)

    # 5. --validate-only
    if validate_only:
        typer.echo(
            f"OK: {len(scenario.entries)} scenario entries, "
            f"{len({e.image_path for e in scenario.entries})} images of {width}x{height}"
        )
        return

    # 6-7. Audio duration, then the resolved timeline.
    audio_duration = probe_audio_duration(audio_input, ffprobe_bin)
    timeline = compute_timeline(
        scenario, audio_duration, loop_repeat=loop_repeat, default_transition=transition
    )

    # 8. --dry-run
    if dry_run:
        typer.echo(format_dry_run(timeline))
        return

    # 9-10. Build and run the ffmpeg command.  A very long filter graph goes through a
    # temporary script file, which is removed afterwards.
    container = output_format or (None if video_output.suffix else "mp4")
    with tempfile.TemporaryDirectory(prefix="vidcarousel-") as tmp_dir:
        cmd = build_ffmpeg_command(
            timeline,
            audio_input,
            video_output,
            codec=codec,
            audio_codec=audio_codec,
            crf=crf,
            fps=fps,
            audio_fade_out=audio_fade_out,
            output_format=container,
            ffmpeg_bin=ffmpeg_bin,
            filter_script=Path(tmp_dir) / "filter_graph.txt",
        )
        try:
            run_ffmpeg(cmd)
        except FfmpegError:
            # The output did not exist before this run, so anything there is a partial file.
            video_output.unlink(missing_ok=True)
            raise

    # 11. Report.
    if not video_output.is_file():
        raise FfmpegError("VC3003", f"ffmpeg reported success but {video_output} was not created")
    typer.echo(
        f"Wrote {video_output} ({_format_size(video_output.stat().st_size)}, "
        f"{timeline.total_duration:.3f} s)"
    )
