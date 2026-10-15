"""Shared fixtures.  Generated programmatically; never imports project modules."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """The CLI reconfigures structlog to write to the (temporary) stderr of a test run."""
    yield
    structlog.reset_defaults()


# (type, display_time, xfade_time | None, transition | None, filename)
EntryTuple = tuple[str, float, float | None, str | None, str]


def make_solid_png(
    path: Path, width: int = 64, height: int = 64, r: int = 128, g: int = 128, b: int = 128
) -> None:
    """Write a minimal valid RGB PNG (no Pillow needed)."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw_rows = b"".join(b"\x00" + bytes([r, g, b]) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(raw_rows))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def make_silent_wav(path: Path, duration: float, sample_rate: int = 44100) -> None:
    """Write a minimal valid WAV file with silence."""
    num_samples = int(sample_rate * duration)
    data_size = num_samples * 2  # 16-bit mono
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        data_size,
    )
    path.write_bytes(header + b"\x00" * data_size)


def format_entry(entry: EntryTuple) -> str:
    type_, display, xfade, transition, filename = entry
    params = [type_, f"{display:g}"]
    if xfade is not None or transition is not None:
        params.append(f"{(xfade if xfade is not None else 0):g}")
    if transition is not None:
        params.append(transition)
    return f"{','.join(params)}  {filename}"


@pytest.fixture
def sample_scenario(tmp_path: Path) -> Callable[[Sequence[EntryTuple]], Path]:
    """Factory: write a scenario file in tmp_path from entry tuples."""

    def _make(entries: Sequence[EntryTuple], name: str = "scenario.txt") -> Path:
        path = tmp_path / name
        path.write_text("\n".join(format_entry(e) for e in entries) + "\n", encoding="utf-8")
        return path

    return _make


@pytest.fixture
def dummy_image(tmp_path: Path) -> Callable[..., Path]:
    """Factory: create a solid-colour PNG in tmp_path and return its path."""

    def _make(name: str = "image.png", width: int = 64, height: int = 64) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        make_solid_png(path, width, height)
        return path

    return _make


@pytest.fixture
def dummy_audio(tmp_path: Path) -> Callable[..., Path]:
    """Factory: create a silent WAV of the given duration in tmp_path."""

    def _make(name: str = "audio.wav", duration: float = 1.0) -> Path:
        path = tmp_path / name
        make_silent_wav(path, duration)
        return path

    return _make


@pytest.fixture
def scenario_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A directory with a complete scenario file and matching dummy images."""
    entries: list[EntryTuple] = [
        ("start", 5, None, None, "intro.png"),
        ("loop", 4, 1.5, None, "image1.png"),
        ("loop", 6, 2, "dissolve", "image2.png"),
        ("loop", 8, None, None, "image3.png"),
        ("credits", 10, 1.5, None, "credits.png"),
        ("end", 5, 1.5, None, "final.png"),
    ]
    for _, _, _, _, filename in entries:
        make_solid_png(tmp_path / filename)
    scenario = tmp_path / "scenario.txt"
    scenario.write_text("\n".join(format_entry(e) for e in entries) + "\n", encoding="utf-8")
    return tmp_path, scenario
