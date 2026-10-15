from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from vidcarousel.errors import InputFileError, ScenarioError
from vidcarousel.models import EntryType, ScenarioEntry
from vidcarousel.scenario import parse_entry_line, parse_scenario, validate_ordering

from .conftest import EntryTuple

BASE = Path("/base")


def e(t: EntryType, dt: float = 4.0, xf: float = 0.0) -> ScenarioEntry:
    return ScenarioEntry(t, dt, xf, None, BASE / "x.png")


S, L, C, E = EntryType.START, EntryType.LOOP, EntryType.CREDITS, EntryType.END


def code_of(exc: pytest.ExceptionInfo[ScenarioError]) -> str:
    return exc.value.code


# ---------------------------------------------------------------- parse_entry_line


def test_parse_line_minimal() -> None:
    entry = parse_entry_line("start,5  intro.png", 1, BASE)
    assert entry == ScenarioEntry(S, 5.0, 0.0, None, BASE / "intro.png")


def test_parse_line_with_xfade_and_transition() -> None:
    entry = parse_entry_line("loop,6,2,dissolve  image2.png", 3, BASE)
    assert (entry.entry_type, entry.display_time, entry.xfade_time) == (L, 6.0, 2.0)
    assert entry.transition == "dissolve"
    assert entry.image_path == BASE / "image2.png"


def test_parse_line_float_values() -> None:
    entry = parse_entry_line("loop,4.5,1.25 a.png", 1, BASE)
    assert (entry.display_time, entry.xfade_time) == (4.5, 1.25)


def test_parse_line_tab_separator_and_spaces_in_filename() -> None:
    entry = parse_entry_line("credits,10,1.5\tmy credits  v2.png  ", 1, BASE)
    assert entry.entry_type is C
    assert entry.image_path == BASE / "my credits  v2.png"


def test_parse_line_zero_xfade_is_hard_cut() -> None:
    assert parse_entry_line("loop,4,0 a.png", 1, BASE).xfade_time == 0.0


def test_parse_line_transition_override_with_zero_xfade_kept() -> None:
    entry = parse_entry_line("loop,4,0,wipeleft a.png", 1, BASE)
    assert entry.transition == "wipeleft"


def test_start_with_zero_xfade_allowed() -> None:
    assert parse_entry_line("start,5,0 a.png", 1, BASE).xfade_time == 0.0


@pytest.mark.parametrize("line", ["start,5", "start,5   ", "justonetoken", ""])
def test_parse_line_missing_filename(line: str) -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line(line, 1, BASE)
    assert code_of(exc) == "VC1005"


@pytest.mark.parametrize("type_str", ["intro", "START", "", "loops"])
def test_unknown_type_vc1005(type_str: str) -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line(f"{type_str},5 a.png", 7, BASE)
    assert code_of(exc) == "VC1005"
    assert "line 7" in exc.value.message


def test_too_many_fields_vc1005() -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line("loop,4,1,fade,extra a.png", 1, BASE)
    assert code_of(exc) == "VC1005"


@pytest.mark.parametrize("value", ["0", "-1", "abc", "", "nan", "inf"])
def test_bad_display_time_vc1006(value: str) -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line(f"loop,{value} a.png", 1, BASE)
    assert code_of(exc) == "VC1006"


def test_missing_display_time_vc1006() -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line("loop a.png", 1, BASE)
    assert code_of(exc) == "VC1006"


@pytest.mark.parametrize("xfade", ["4", "5", "-0.5", "abc", "", "nan"])
def test_bad_xfade_vc1007(xfade: str) -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line(f"loop,4,{xfade} a.png", 1, BASE)
    assert code_of(exc) == "VC1007"


def test_start_with_xfade_vc1008() -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line("start,5,1.5 a.png", 1, BASE)
    assert code_of(exc) == "VC1008"


def test_transition_validation_optional() -> None:
    # Without a transition set, any non-empty name is accepted.
    assert parse_entry_line("loop,4,1,bogus a.png", 1, BASE).transition == "bogus"
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line("loop,4,1,bogus a.png", 1, BASE, valid_transitions={"fade"})
    assert code_of(exc) == "VC1009"
    ok = parse_entry_line("loop,4,1,fade a.png", 1, BASE, valid_transitions={"fade"})
    assert ok.transition == "fade"


def test_empty_transition_vc1009() -> None:
    with pytest.raises(ScenarioError) as exc:
        parse_entry_line("loop,4,1, a.png", 1, BASE)
    # "loop,4,1," followed by space splits params at the comma: empty field.
    assert code_of(exc) == "VC1009"


# ---------------------------------------------------------------- validate_ordering


@pytest.mark.parametrize(
    "types",
    [
        [S, L],
        [S, L, L, L],
        [S, L, C],
        [S, L, E],
        [S, L, C, E],
        [S, L, L, C, E],
    ],
)
def test_valid_orderings(types: list[EntryType]) -> None:
    validate_ordering([e(t) for t in types])


@pytest.mark.parametrize(
    ("types", "code"),
    [
        ([], "VC1001"),
        ([L, S], "VC1001"),
        ([L], "VC1001"),
        ([S, L, S], "VC1001"),
        ([S, S, L], "VC1001"),
        ([S], "VC1002"),
        ([S, C], "VC1002"),
        ([S, E], "VC1002"),
        ([S, C, L], "VC1003"),
        ([S, L, C, L], "VC1003"),
        ([S, L, C, C], "VC1003"),
        ([S, L, E, L], "VC1004"),
        ([S, L, E, C], "VC1004"),
        ([S, L, E, E], "VC1004"),
        ([S, L, C, E, E], "VC1004"),
    ],
)
def test_invalid_orderings(types: list[EntryType], code: str) -> None:
    with pytest.raises(ScenarioError) as exc:
        validate_ordering([e(t) for t in types])
    assert code_of(exc) == code


# ---------------------------------------------------------------- parse_scenario


def test_parse_scenario_minimal(
    sample_scenario: Callable[[Sequence[EntryTuple]], Path],
    dummy_image: Callable[..., Path],
) -> None:
    dummy_image("a.png")
    dummy_image("b.png")
    path = sample_scenario([("start", 5, None, None, "a.png"), ("loop", 4, 1, None, "b.png")])
    scenario = parse_scenario(path)
    assert len(scenario.entries) == 2
    assert scenario.start.image_path == path.parent / "a.png"
    assert scenario.loops[0].xfade_time == 1.0
    assert scenario.credits is None and scenario.end is None


def test_parse_scenario_full(scenario_dir: tuple[Path, Path]) -> None:
    base, path = scenario_dir
    scenario = parse_scenario(path, valid_transitions={"fade", "dissolve"})
    assert [x.entry_type for x in scenario.entries] == [S, L, L, L, C, E]
    assert scenario.loops[1].transition == "dissolve"
    assert scenario.loops[2].xfade_time == 0.0
    assert scenario.credits is not None and scenario.credits.image_path == base / "credits.png"
    assert scenario.end is not None and scenario.end.display_time == 5.0


def test_comments_blank_lines_and_bom(tmp_path: Path) -> None:
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(b"x")
    path = tmp_path / "s.txt"
    path.write_text(
        "﻿# comment\n\n   \n  # indented comment\nstart,5 a.png\n\nloop,4 b.png\n",
        encoding="utf-8",
    )
    assert len(parse_scenario(path).entries) == 2


def test_filename_with_spaces(tmp_path: Path) -> None:
    (tmp_path / "my pic 1.png").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")
    path = tmp_path / "s.txt"
    path.write_text("start,5   my pic 1.png\nloop,4 b.png\n", encoding="utf-8")
    assert parse_scenario(path).start.image_path == tmp_path / "my pic 1.png"


def test_images_resolved_relative_to_scenario_dir(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    (sub / "img").mkdir(parents=True)
    (sub / "img" / "a.png").write_bytes(b"x")
    path = sub / "s.txt"
    path.write_text("start,5 img/a.png\nloop,4 img/a.png\n", encoding="utf-8")
    assert parse_scenario(path).start.image_path == sub / "img" / "a.png"


def test_missing_start_vc1001(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(b"x")
    path = tmp_path / "s.txt"
    path.write_text("loop,4 a.png\n", encoding="utf-8")
    with pytest.raises(ScenarioError) as exc:
        parse_scenario(path)
    assert code_of(exc) == "VC1001"


def test_empty_file_vc1001(tmp_path: Path) -> None:
    path = tmp_path / "s.txt"
    path.write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(ScenarioError) as exc:
        parse_scenario(path)
    assert code_of(exc) == "VC1001"


def test_line_error_reports_physical_line_number(tmp_path: Path) -> None:
    path = tmp_path / "s.txt"
    path.write_text("# c\n\nstart,5 a.png\nloop,0 b.png\n", encoding="utf-8")
    with pytest.raises(ScenarioError) as exc:
        parse_scenario(path)
    assert code_of(exc) == "VC1006"
    assert "line 4" in exc.value.message


def test_unknown_transition_in_file_vc1009(scenario_dir: tuple[Path, Path]) -> None:
    _, path = scenario_dir
    with pytest.raises(ScenarioError) as exc:
        parse_scenario(path, valid_transitions={"fade"})  # file uses 'dissolve'
    assert code_of(exc) == "VC1009"


def test_missing_image_vc2001(scenario_dir: tuple[Path, Path]) -> None:
    base, path = scenario_dir
    (base / "image2.png").unlink()
    with pytest.raises(InputFileError) as exc:
        parse_scenario(path)
    assert exc.value.code == "VC2001"
    assert "image2.png" in exc.value.message


def test_image_is_directory_vc2001(scenario_dir: tuple[Path, Path]) -> None:
    base, path = scenario_dir
    (base / "final.png").unlink()
    (base / "final.png").mkdir()
    with pytest.raises(InputFileError) as exc:
        parse_scenario(path)
    assert exc.value.code == "VC2001"


def test_ordering_error_precedes_missing_files(tmp_path: Path) -> None:
    path = tmp_path / "s.txt"
    path.write_text("start,5 nope.png\n", encoding="utf-8")
    with pytest.raises(ScenarioError) as exc:
        parse_scenario(path)
    assert code_of(exc) == "VC1002"


def test_scenario_file_missing_vc2004(tmp_path: Path) -> None:
    with pytest.raises(InputFileError) as exc:
        parse_scenario(tmp_path / "missing.txt")
    assert exc.value.code == "VC2004"


def test_scenario_file_is_directory_vc2004(tmp_path: Path) -> None:
    with pytest.raises(InputFileError) as exc:
        parse_scenario(tmp_path)
    assert exc.value.code == "VC2004"


def test_scenario_file_not_utf8_vc2004(tmp_path: Path) -> None:
    path = tmp_path / "s.txt"
    path.write_bytes(b"start,5 \xff\xfe\xfa.png\n")
    with pytest.raises(InputFileError) as exc:
        parse_scenario(path)
    assert exc.value.code == "VC2004"
