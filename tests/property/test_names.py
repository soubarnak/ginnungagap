# SPDX-License-Identifier: Apache-2.0
"""Realm name rules (DR-002) under generated input (NFR-SEC-005)."""

import re

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from ginn.domain.names import RealmName  # noqa: E402
from ginn.errors import GinnError  # noqa: E402

pytestmark = [pytest.mark.fr("DR-002"), pytest.mark.fr("NFR-SEC-005")]

NAME = r"[a-z][a-z0-9-]{0,31}"
RESERVED = ("ginn", "host", "root", "all")
SHELL_CHARS = ";|&$`(){}<>*?!'\"\\ \n\x00"


def accepted(raw: str) -> bool:
    return re.fullmatch(NAME, raw) is not None and raw not in RESERVED


VALID = st.from_regex(NAME, fullmatch=True).filter(lambda raw: raw not in RESERVED)
INVALID = st.one_of(
    st.text().filter(lambda raw: not accepted(raw)),
    st.text(alphabet=SHELL_CHARS, min_size=1, max_size=20),
    st.from_regex(r"[A-Z0-9_\-][a-z0-9-]{0,31}", fullmatch=True),
    st.from_regex(r"[a-z][a-z0-9-]{32,64}", fullmatch=True),
    st.text(alphabet=st.characters(min_codepoint=128), min_size=1, max_size=12),
    st.sampled_from(RESERVED),
)


@given(VALID)
def test_valid_names_parse_and_round_trip(raw: str) -> None:
    name = RealmName.parse(raw)
    assert name.value == raw
    assert str(name) == raw
    assert RealmName.parse(str(name)) == name


@given(INVALID)
def test_invalid_names_are_rejected_with_name_invalid_exit_2(raw: str) -> None:
    with pytest.raises(GinnError) as info:
        RealmName.parse(raw)
    err = info.value
    assert err.code == "GINN-E-NAME-INVALID"
    assert err.exit_code == 2
    assert err.details["name"] == raw
    assert isinstance(err.details["rule"], str)
    assert err.details["rule"]
