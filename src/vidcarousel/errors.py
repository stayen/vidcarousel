"""Typed exception hierarchy and stable error-code registry.

Exit codes and error codes are stable API.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorInfo:
    """Registry record for one error code."""

    exit_code: int
    category: str
    description: str


ERROR_CODES: dict[str, ErrorInfo] = {
    "VC1001": ErrorInfo(1, "Scenario", "`start` entry missing or not first."),
    "VC1002": ErrorInfo(1, "Scenario", "No `loop` entries found."),
    "VC1003": ErrorInfo(1, "Scenario", "`credits` entry in wrong position."),
    "VC1004": ErrorInfo(1, "Scenario", "`end` entry not last."),
    "VC1005": ErrorInfo(1, "Scenario", "Unknown entry type."),
    "VC1006": ErrorInfo(1, "Scenario", "`display_time` not a positive number."),
    "VC1007": ErrorInfo(1, "Scenario", "`xfade_time` out of valid range."),
    "VC1008": ErrorInfo(1, "Scenario", "`start` entry has non-zero `xfade_time`."),
    "VC1009": ErrorInfo(1, "Scenario", "Unknown transition name."),
    "VC2001": ErrorInfo(2, "Input", "Image file not found or unreadable."),
    "VC2002": ErrorInfo(2, "Input", "Image dimension mismatch across entries."),
    "VC2003": ErrorInfo(2, "Input", "Audio file not found or unreadable."),
    "VC2004": ErrorInfo(2, "Input", "Scenario file not found or unreadable."),
    "VC3001": ErrorInfo(3, "ffmpeg", "`ffmpeg` binary not found."),
    "VC3002": ErrorInfo(3, "ffmpeg", "`ffprobe` binary not found or probe failed."),
    "VC3003": ErrorInfo(3, "ffmpeg", "`ffmpeg` execution failed (non-zero exit)."),
    "VC4001": ErrorInfo(4, "Output", "Output path not writable or parent dir missing."),
    "VC4002": ErrorInfo(4, "Output", "Output file already exists (no `--overwrite` flag)."),
}

# Error-code prefix -> exit code for the category (VC1xxx -> 1, ...).
EXIT_CODE_BY_CATEGORY: dict[str, int] = {
    "Scenario": 1,
    "Input": 2,
    "ffmpeg": 3,
    "Output": 4,
}


def exit_code_for(code: str) -> int:
    """Return the process exit code mapped to an error code."""
    try:
        return ERROR_CODES[code].exit_code
    except KeyError:
        raise KeyError(f"Unknown error code: {code!r}") from None


class VidCarouselError(Exception):
    """Base class for all vidcarousel errors."""

    category: str | None = None

    def __init__(self, code: str, message: str) -> None:
        info = ERROR_CODES.get(code)
        if info is None:
            raise ValueError(f"Unknown error code: {code!r}")
        if self.category is not None and info.category != self.category:
            raise ValueError(
                f"Error code {code} belongs to category {info.category!r}, not {self.category!r}"
            )
        super().__init__(code, message)
        self.code = code
        self.message = message

    @property
    def exit_code(self) -> int:
        return ERROR_CODES[self.code].exit_code

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ScenarioError(VidCarouselError):
    """Scenario file syntax / ordering / value errors (VC1xxx)."""

    category = "Scenario"


class InputFileError(VidCarouselError):
    """Missing or invalid input files (VC2xxx)."""

    category = "Input"


class FfmpegError(VidCarouselError):
    """ffmpeg / ffprobe problems (VC3xxx)."""

    category = "ffmpeg"


class OutputError(VidCarouselError):
    """Output path problems (VC4xxx)."""

    category = "Output"


class CLIError(VidCarouselError):
    """CLI usage errors. Accepts any registered code."""
