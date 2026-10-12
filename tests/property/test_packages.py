# SPDX-License-Identifier: Apache-2.0
"""Package name rules (DR-003, AS-027) under generated input (NFR-SEC-005)."""

import re

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from ginn.domain.enums import DistroId  # noqa: E402
from ginn.domain.packages import PackageName  # noqa: E402
from ginn.errors import GinnError  # noqa: E402

pytestmark = [
    pytest.mark.fr("DR-003"),
    pytest.mark.fr("AS-027"),
    pytest.mark.fr("NFR-SEC-005"),
]

# Patterns copied from DR-003 in the SRS. Kali uses the Debian pattern.
PATTERNS: dict[DistroId, str] = {
    DistroId.ARCH: r"[a-z0-9@_+][a-z0-9@._+-]{0,127}",
    DistroId.DEBIAN: r"[a-z0-9][a-z0-9.+-]{1,127}",
    DistroId.FEDORA: r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}",
    DistroId.KALI: r"[a-z0-9][a-z0-9.+-]{1,127}",
}
DISTROS = list(DistroId)
AS027_CHARS = "*?[]=<>/ \t\n\x00"
NOISE = "()!,;:|&$`\\'\"{}~@^%#" + AS027_CHARS + "abcXYZ019._+-"

VALID = st.one_of(
    *(st.tuples(st.just(d), st.from_regex(p, fullmatch=True)) for d, p in PATTERNS.items())
)
INVALID = st.tuples(st.sampled_from(DISTROS), st.text(alphabet=NOISE, max_size=12)).filter(
    lambda pair: re.fullmatch(PATTERNS[pair[0]], pair[1]) is None
)
INVALID_ANY = st.tuples(st.sampled_from(DISTROS), st.text()).filter(
    lambda pair: re.fullmatch(PATTERNS[pair[0]], pair[1]) is None
)


@given(VALID)
def test_valid_names_parse_for_their_distro(pair: tuple[DistroId, str]) -> None:
    distro, raw = pair
    name = PackageName.parse(raw, distro)
    assert name.value == raw
    assert name.distro is distro
    assert str(name) == raw


@given(st.one_of(INVALID, INVALID_ANY))
def test_invalid_names_are_rejected_with_pkg_name_invalid_exit_2(
    pair: tuple[DistroId, str],
) -> None:
    distro, raw = pair
    with pytest.raises(GinnError) as info:
        PackageName.parse(raw, distro)
    err = info.value
    assert err.code == "GINN-E-PKG-NAME-INVALID"
    assert err.exit_code == 2
    assert err.details["name"] == raw
    assert isinstance(err.details["rule"], str)
