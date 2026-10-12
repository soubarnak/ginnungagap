# SPDX-License-Identifier: Apache-2.0
"""Realm name rules (DR-002): concrete cases that run without hypothesis (NFR-SEC-005)."""

import pytest

from ginn.domain.names import RealmName
from ginn.errors import GinnError

pytestmark = [pytest.mark.fr("DR-002"), pytest.mark.fr("NFR-SEC-005")]

RESERVED = ("ginn", "host", "root", "all")
SHELL_CHARS = ";|&$`(){}<>*?!'\"\\ \n\x00"


@pytest.mark.parametrize("raw", ["arch", "a", "a-b1", "debian-2", "a" * 32])
def test_valid_names_parse_and_round_trip(raw: str) -> None:
    name = RealmName.parse(raw)
    assert name.value == raw
    assert str(name) == raw
    assert RealmName.parse(str(name)) == name


@pytest.mark.parametrize("word", RESERVED)
def test_reserved_words_never_parse(word: str) -> None:
    with pytest.raises(GinnError) as info:
        RealmName.parse(word)
    err = info.value
    assert err.code == "GINN-E-NAME-INVALID"
    assert err.exit_code == 2
    assert err.details["rule"] == "reserved word (DR-002)"


@pytest.mark.parametrize("ch", list(SHELL_CHARS), ids=[repr(ch) for ch in SHELL_CHARS])
def test_each_shell_metacharacter_is_rejected(ch: str) -> None:
    raw = f"arch{ch}"
    with pytest.raises(GinnError) as info:
        RealmName.parse(raw)
    assert info.value.code == "GINN-E-NAME-INVALID"
    assert info.value.exit_code == 2
    assert info.value.details["name"] == raw


@pytest.mark.parametrize(
    "raw",
    [
        "Arch",
        "ARCH",
        "1arch",
        "-arch",
        "",
        "a" * 33,
        "arch\n",
        "ａｂｃ",
        "é",
        "名前",
        "gin n",
        "a/b",
        "..",
    ],
)
def test_hostile_names_are_rejected(raw: str) -> None:
    with pytest.raises(GinnError) as info:
        RealmName.parse(raw)
    err = info.value
    assert err.code == "GINN-E-NAME-INVALID"
    assert err.exit_code == 2
    assert err.details["name"] == raw
    assert err.details["rule"]


def test_direct_construction_is_checked_like_parse() -> None:
    with pytest.raises(GinnError):
        RealmName("root")
    with pytest.raises(GinnError):
        RealmName("ab\n")
    assert RealmName("arch").value == "arch"


def test_non_string_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        RealmName(7)  # type: ignore[arg-type]
