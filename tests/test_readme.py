"""Keep README.md honest: its tables and examples are checked against the code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vidcarousel.cli import app
from vidcarousel.errors import ERROR_CODES
from vidcarousel.ffmpeg import get_valid_transitions
from vidcarousel.scenario import parse_scenario
from vidcarousel.timeline import compute_timeline, format_dry_run

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def section(start: str, end: str) -> str:
    a = README.index(start)
    return README[a : README.index(end, a)]


def test_error_code_table_matches_registry() -> None:
    rows = re.findall(r"^\| `(VC\d{4})` \| (\d) \| (\w+) \| (.+) \|$", README, flags=re.MULTILINE)
    assert {code: (int(exit_), cat, desc) for code, exit_, cat, desc in rows} == {
        code: (info.exit_code, info.category, info.description)
        for code, info in ERROR_CODES.items()
    }


def test_validation_rules_table_mentions_scenario_codes() -> None:
    rules = section("### Validation rules", "## How the timeline")
    for code in ERROR_CODES:
        if code.startswith("VC1") or code in ("VC2001", "VC2002"):
            assert f"`{code}`" in rules, code


def test_transition_list_matches_code() -> None:
    block = section("<!-- transitions:start -->", "<!-- transitions:end -->")
    assert set(re.findall(r"`([a-z]+)`", block)) == get_valid_transitions()


def test_every_cli_option_is_documented() -> None:
    help_text = CliRunner().invoke(app, ["--help"]).stdout
    options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", help_text)) - {"--help"}
    shorts = set(
        re.findall(r"(?<![\w-])-[a-z]{1,2}(?![\w-])", " ".join(re.findall(r"│.*│", help_text)))
    )
    reference = section("## Command-line reference", "## Examples")
    assert len(options) >= 15
    for option in options:
        assert f"`{option}`" in reference, option
    for short in shorts - {"-h"}:
        assert f"`{short}`" in reference, short


def quick_start_scenario(directory: Path) -> Path:
    block = section("## Quick start", "```bash")
    body = block.split("```text\n", 1)[1].split("```", 1)[0]
    path = directory / "scenario.txt"
    path.write_text(body, encoding="utf-8")
    for name in re.findall(r"\s(\w+\.png)\s*$", body, flags=re.MULTILINE):
        (directory / name).write_bytes(b"x")
    return path


def test_quick_start_scenario_is_valid(tmp_path: Path) -> None:
    scenario = parse_scenario(quick_start_scenario(tmp_path), get_valid_transitions())
    assert [e.entry_type.value for e in scenario.entries] == [
        "start", "loop", "loop", "loop", "credits", "end",
    ]  # fmt: skip
    assert scenario.loops[1].transition == "dissolve"


def test_dry_run_example_is_what_the_tool_prints(tmp_path: Path) -> None:
    example = section("**Preview the timeline**", "**Check everything")
    shown = example.split("--dry-run\n", 1)[1].split("\n```", 1)[0]
    scenario = parse_scenario(quick_start_scenario(tmp_path), get_valid_transitions())
    timeline = compute_timeline(scenario, 30.0, loop_repeat=True, default_transition="fade")
    assert shown == format_dry_run(timeline)


def test_timeline_table_matches_the_computed_slots(tmp_path: Path) -> None:
    scenario = parse_scenario(quick_start_scenario(tmp_path), get_valid_transitions())
    timeline = compute_timeline(scenario, 19.5, loop_repeat=True, default_transition="fade")
    block = section("## How the timeline is computed", "- The **audible section**")
    rows = re.findall(r"^\| `(\w+\.png)` \| ([\d.]+) – ([\d.]+) \|", block, flags=re.MULTILINE)
    assert len(rows) == 4  # start + one carousel pass; the silent tail is not in the table
    for slot, (name, start, end) in zip(timeline.slots, rows, strict=False):
        assert slot.image_path.name == name
        assert slot.timestamp == pytest.approx(float(start))
        assert slot.timestamp + slot.display_time == pytest.approx(float(end))
    assert timeline.carousel_pass_duration == pytest.approx(14.5)
