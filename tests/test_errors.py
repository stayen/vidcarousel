import re

import pytest

from vidcarousel.errors import (
    ERROR_CODES,
    EXIT_CODE_BY_CATEGORY,
    CLIError,
    FfmpegError,
    InputFileError,
    OutputError,
    ScenarioError,
    VidCarouselError,
    exit_code_for,
)

EXPECTED_EXIT = {
    "VC1001": 1, "VC1002": 1, "VC1003": 1, "VC1004": 1, "VC1005": 1,
    "VC1006": 1, "VC1007": 1, "VC1008": 1, "VC1009": 1,
    "VC2001": 2, "VC2002": 2, "VC2003": 2, "VC2004": 2,
    "VC3001": 3, "VC3002": 3, "VC3003": 3,
    "VC4001": 4, "VC4002": 4,
}  # fmt: skip


def test_registry_matches_spec() -> None:
    assert {c: i.exit_code for c, i in ERROR_CODES.items()} == EXPECTED_EXIT


@pytest.mark.parametrize("code", sorted(EXPECTED_EXIT))
def test_code_format_and_exit_code(code: str) -> None:
    assert re.fullmatch(r"VC\d{4}", code)
    assert exit_code_for(code) == EXPECTED_EXIT[code]
    # The leading digit of the code is the exit code.
    assert int(code[2]) == EXPECTED_EXIT[code]


def test_categories_map_to_exit_codes() -> None:
    for info in ERROR_CODES.values():
        assert EXIT_CODE_BY_CATEGORY[info.category] == info.exit_code
        assert info.description


def test_exit_code_for_unknown() -> None:
    with pytest.raises(KeyError):
        exit_code_for("VC9999")


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (ScenarioError, "VC1001"),
        (InputFileError, "VC2001"),
        (FfmpegError, "VC3003"),
        (OutputError, "VC4002"),
        (CLIError, "VC2003"),
    ],
)
def test_exception_hierarchy_and_attrs(cls: type[VidCarouselError], code: str) -> None:
    err = cls(code, "boom")
    assert isinstance(err, VidCarouselError)
    assert isinstance(err, Exception)
    assert err.code == code
    assert err.message == "boom"
    assert err.exit_code == EXPECTED_EXIT[code]
    assert str(err) == f"{code}: boom"


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (ScenarioError, "VC2001"),
        (InputFileError, "VC1001"),
        (FfmpegError, "VC4001"),
        (OutputError, "VC3001"),
    ],
)
def test_category_mismatch_rejected(cls: type[VidCarouselError], code: str) -> None:
    with pytest.raises(ValueError, match="category"):
        cls(code, "x")


def test_unknown_code_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown error code"):
        VidCarouselError("VC0000", "x")


def test_raise_and_catch_as_base() -> None:
    with pytest.raises(VidCarouselError) as exc:
        raise ScenarioError("VC1002", "no loops")
    assert exc.value.exit_code == 1
