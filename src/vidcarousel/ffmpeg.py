"""ffmpeg / ffprobe helpers (CLAUDE.md § 5, § 8.5).

ffprobe utilities, the ffmpeg command builder and the ffmpeg runner.
"""

from __future__ import annotations

import math
import shlex
import shutil
import subprocess
from pathlib import Path

import structlog

from vidcarousel.errors import FfmpegError, OutputError
from vidcarousel.models import Timeline

log = structlog.get_logger(__name__)

# Lines of ffmpeg stderr included in the VC3003 message (the full text is logged).
_ERROR_TAIL_LINES = 20

# ffmpeg refuses to overwrite an existing output (``-n``, or no ``-y`` without a
# terminal) with this message -- but exits with status 0.
_OVERWRITE_REFUSED = "already exists. Exiting."

# Standard ffmpeg xfade transitions (CLAUDE.md § 12.1).  May be incomplete for
# custom or newer ffmpeg builds, which can offer additional names.
_XFADE_TRANSITIONS: frozenset[str] = frozenset(
    {
        "fade", "fadeblack", "fadewhite", "distance",
        "wipeleft", "wiperight", "wipeup", "wipedown",
        "slideleft", "slideright", "slideup", "slidedown",
        "circlecrop", "rectcrop", "circleclose", "circleopen",
        "horzclose", "horzopen", "vertclose", "vertopen",
        "diagbl", "diagbr", "diagtl", "diagtr",
        "hlslice", "hrslice", "vuslice", "vdslice",
        "dissolve", "pixelize", "radial", "hblur",
        "wipetl", "wipetr", "wipebl", "wipebr",
        "squeezeh", "squeezev",
        "smoothleft", "smoothright", "smoothup", "smoothdown",
    }
)  # fmt: skip


def get_valid_transitions() -> set[str]:
    """Return the known ffmpeg xfade transition names (used for SC11)."""
    return set(_XFADE_TRANSITIONS)


def check_binaries(ffmpeg_bin: str, ffprobe_bin: str) -> None:
    """Verify that both binaries can be found and executed.

    ``ffmpeg_bin`` / ``ffprobe_bin`` may be bare names (looked up on ``PATH``)
    or paths.

    Raises:
        FfmpegError: ``VC3001`` if ffmpeg is missing, ``VC3002`` if ffprobe is.
    """
    if shutil.which(ffmpeg_bin) is None:
        raise FfmpegError("VC3001", f"ffmpeg binary not found: {ffmpeg_bin!r}")
    if shutil.which(ffprobe_bin) is None:
        raise FfmpegError("VC3002", f"ffprobe binary not found: {ffprobe_bin!r}")


def _run_ffprobe(cmd: list[str], target: Path) -> str:
    """Run an ffprobe command and return its stripped stdout.

    Every failure (binary missing, non-zero exit, empty output) is ``VC3002``.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    except OSError as exc:
        raise FfmpegError("VC3002", f"cannot run ffprobe {cmd[0]!r}: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise FfmpegError("VC3002", f"ffprobe failed for {target}: {detail}")

    output = result.stdout.strip()
    if not output:
        # e.g. asking for video dimensions of an audio-only file exits 0 with no output
        raise FfmpegError("VC3002", f"ffprobe returned no data for {target}")
    return output


def probe_audio_duration(audio_path: Path, ffprobe_bin: str = "ffprobe") -> float:
    """Return the audio file's duration in seconds.

    Runs ``ffprobe -v error -show_entries format=duration -of csv=p=0 <file>``.

    Raises:
        FfmpegError: ``VC3002`` if the probe fails or yields no positive duration.
    """
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(audio_path),
    ]  # fmt: skip
    output = _run_ffprobe(cmd, audio_path)

    try:
        duration = float(output.splitlines()[0].strip().rstrip(","))
    except ValueError:
        raise FfmpegError(
            "VC3002", f"unparseable duration from ffprobe for {audio_path}: {output!r}"
        ) from None
    if not math.isfinite(duration) or duration <= 0:
        raise FfmpegError("VC3002", f"invalid duration {duration!r} reported for {audio_path}")
    return duration


def probe_image_dimensions(image_path: Path, ffprobe_bin: str = "ffprobe") -> tuple[int, int]:
    """Return the image's ``(width, height)`` in pixels.

    Runs ``ffprobe -v error -select_streams v:0
    -show_entries stream=width,height -of csv=p=0 <file>``.

    Raises:
        FfmpegError: ``VC3002`` if the probe fails or yields no valid dimensions.
    """
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(image_path),
    ]  # fmt: skip
    output = _run_ffprobe(cmd, image_path)

    # Expected "WIDTH,HEIGHT"; tolerate a trailing comma and extra lines.
    parts = [p for p in output.splitlines()[0].strip().split(",") if p != ""]
    try:
        if len(parts) != 2:
            raise ValueError(parts)
        width, height = int(parts[0]), int(parts[1])
        if width <= 0 or height <= 0:
            raise ValueError(parts)
    except ValueError:
        raise FfmpegError(
            "VC3002", f"unparseable dimensions from ffprobe for {image_path}: {output!r}"
        ) from None
    return width, height


# A single argv element may not exceed 128 KiB on Linux (MAX_ARG_STRLEN), and a
# filter graph grows with the number of timeline slots (roughly an hour of audio
# with 3 s slots).  Graphs longer than this many characters go into a script file
# passed with ``-filter_complex_script`` instead of inline.
FILTER_INLINE_LIMIT = 100_000

# Muxers that understand ``-movflags +faststart``; passing it to any other muxer is an error.
_FASTSTART_FORMATS = frozenset({"mp4", "m4v", "mov"})


def _num(value: float) -> str:
    """Format a time/duration compactly and deterministically (max 6 decimals)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def build_filter_complex(
    timeline: Timeline,
    audio_duration: float,
    audio_fade_out: float,
    *,
    fps: int = 25,
) -> str:
    """Produce the ``-filter_complex`` value for a timeline.

    Input indices: image inputs ``0..N-1`` (one per slot, in order), then the audio
    input ``N``.  The resulting labels are ``[vfinal]`` and ``[afinal]``.

    Video: every image is normalised (``fps``, ``yuv420p``, ``setsar=1``) so the
    chain's inputs always agree on timebase and format.  Slot ``i`` is joined to the
    chain so far with ``xfade`` (``offset`` = the slot's timeline timestamp) or, for
    a hard cut (``xfade_time == 0``), with ``concat`` followed by ``fps`` -- the
    latter re-establishes the ``1/fps`` timebase that a following ``xfade`` needs.
    ``xfade`` is not used with a tiny duration for cuts, because ffmpeg then drops
    the next image when the offset equals the end of the chain.

    Audio: trimmed to ``audio_duration`` and, when ``audio_fade_out > 0``, faded out
    over its last ``audio_fade_out`` seconds (capped at the audio length).
    """
    slots = timeline.slots
    if not slots:
        raise ValueError("timeline has no slots")
    if audio_duration <= 0:
        raise ValueError(f"audio_duration must be positive, got {audio_duration!r}")
    if audio_fade_out < 0:
        raise ValueError(f"audio_fade_out must be >= 0, got {audio_fade_out!r}")

    count = len(slots)
    parts: list[str] = []

    for i in range(count):
        label = "vfinal" if count == 1 else f"i{i}"
        parts.append(f"[{i}:v]fps={fps},format=yuv420p,setsar=1[{label}]")

    current = "i0"
    for i in range(1, count):
        slot = slots[i]
        out = "vfinal" if i == count - 1 else f"s{i}"
        if slot.xfade_time > 0:
            parts.append(
                f"[{current}][i{i}]xfade=transition={slot.transition}"
                f":duration={_num(slot.xfade_time)}:offset={_num(slot.timestamp)}[{out}]"
            )
        else:
            parts.append(f"[{current}][i{i}]concat=n=2:v=1:a=0,fps={fps}[{out}]")
        current = out

    audio = f"[{count}:a]atrim=0:{_num(audio_duration)}"
    fade = min(audio_fade_out, audio_duration)
    if fade > 0:
        audio += f",afade=t=out:st={_num(audio_duration - fade)}:d={_num(fade)}"
    parts.append(f"{audio}[afinal]")

    return ";".join(parts)


def build_ffmpeg_command(
    timeline: Timeline,
    audio_path: Path,
    output_path: Path,
    *,
    codec: str,
    audio_codec: str,
    crf: int,
    fps: int,
    audio_fade_out: float,
    output_format: str | None,
    ffmpeg_bin: str,
    filter_script: Path | None = None,
) -> list[str]:
    """Assemble the full ffmpeg invocation as an argv list.

    One looped image input per timeline slot (``-t`` = the slot's display time),
    then the audio input.  ``-n`` makes ffmpeg refuse to overwrite an existing
    output, ``-nostdin`` keeps it from reading the terminal and ``-nostats`` drops
    the progress lines so captured stderr only holds warnings and errors.  ``-f`` is added
    only when ``output_format`` is given; ``-movflags +faststart`` only for the
    mp4/m4v/mov muxers.
    """
    cmd = [ffmpeg_bin, "-hide_banner", "-nostats", "-nostdin", "-n"]
    for slot in timeline.slots:
        cmd += ["-loop", "1", "-t", _num(slot.display_time), "-i", str(slot.image_path)]
    cmd += ["-i", str(audio_path)]

    graph = build_filter_complex(timeline, timeline.audio_duration, audio_fade_out, fps=fps)
    if filter_script is not None and len(graph) > FILTER_INLINE_LIMIT:
        filter_script.write_text(graph.replace(";", ";\n") + "\n", encoding="utf-8")
        cmd += ["-filter_complex_script", str(filter_script)]
    else:
        cmd += ["-filter_complex", graph]

    cmd += [
        "-map", "[vfinal]",
        "-map", "[afinal]",
        "-c:v", codec,
        "-crf", str(crf),
        "-r", str(fps),
        "-pix_fmt", "yuv420p",
        "-c:a", audio_codec,
    ]  # fmt: skip

    container = (output_format or output_path.suffix.lstrip(".")).lower()
    if container in _FASTSTART_FORMATS:
        cmd += ["-movflags", "+faststart"]
    if output_format:
        cmd += ["-f", output_format]
    cmd.append(str(output_path))
    return cmd


def _log_filter_script(cmd: list[str]) -> None:
    """When the graph lives in a script file, log its contents (debug) for diagnosis."""
    flag = "-filter_complex_script"
    if flag not in cmd or cmd.index(flag) + 1 >= len(cmd):
        return
    path = cmd[cmd.index(flag) + 1]
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("cannot read filter script", path=path, error=str(exc))
        return
    log.debug("filter script", path=path, content=content)


def run_ffmpeg(cmd: list[str]) -> None:
    """Run an assembled ffmpeg command, capturing its output.

    The command is logged at ``info``.  On a non-zero exit the full stderr is
    logged at ``error`` and a ``VC3003`` is raised whose message carries the last
    few lines of stderr.

    Raises:
        FfmpegError: ``VC3001`` if the binary cannot be executed, ``VC3003`` if
            ffmpeg exits non-zero.
        OutputError: ``VC4002`` if ffmpeg declined to overwrite an existing output
            file.  ffmpeg exits 0 in that case, so it is detected from its message.
    """
    log.info("running ffmpeg", command=shlex.join(cmd))
    _log_filter_script(cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    except OSError as exc:
        raise FfmpegError("VC3001", f"cannot run ffmpeg {cmd[0]!r}: {exc}") from exc

    stderr = result.stderr.strip()
    refused = next((ln for ln in stderr.splitlines() if _OVERWRITE_REFUSED in ln), None)
    if refused is not None:
        log.error("ffmpeg refused to overwrite output", detail=refused)
        raise OutputError("VC4002", refused.strip())

    if result.returncode != 0:
        log.error("ffmpeg failed", returncode=result.returncode, stderr=stderr)
        tail = "\n".join(stderr.splitlines()[-_ERROR_TAIL_LINES:]) or "(no output)"
        raise FfmpegError("VC3003", f"ffmpeg exited with code {result.returncode}:\n{tail}")

    if stderr:
        log.debug("ffmpeg stderr", stderr=stderr)
    log.info("ffmpeg finished")
