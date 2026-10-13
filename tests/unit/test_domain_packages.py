# SPDX-License-Identifier: Apache-2.0
"""Package name rules (DR-003, AS-027): concrete cases that run without hypothesis."""

import pytest

from ginn.distros import available
from ginn.domain.enums import DistroId
from ginn.domain.packages import PackageName
from ginn.errors import GinnError

pytestmark = [
    pytest.mark.fr("DR-003"),
    pytest.mark.fr("AS-027"),
    pytest.mark.fr("NFR-SEC-005"),
]

# Every name the data directory offers, the `_test` fixture included (NFR-060). Each name's
# pattern comes from its definition, so these cases also run through the data path.
DISTROS = [DistroId(name) for name in available(include_test=True)]


@pytest.mark.parametrize("distro", DISTROS, ids=lambda distro: distro.value)
@pytest.mark.parametrize(
    "raw",
    [
        "gimp*",
        "gimp>=2",
        "gimp<2",
        "gimp=2",
        "./gimp",
        "/usr/bin/gimp",
        "-gimp",
        "--gimp",
        "gimp 2",
        "gimp\tx",
        "gimp\n",
        "provides(foo)",
        "gimp[x]",
        "gimp?",
        "",
    ],
)
def test_as027_shapes_are_rejected_for_every_distro(distro: DistroId, raw: str) -> None:
    with pytest.raises(GinnError) as info:
        PackageName.parse(raw, distro)
    err = info.value
    assert err.code == "GINN-E-PKG-NAME-INVALID"
    assert err.exit_code == 2
    assert err.details["name"] == raw


@pytest.mark.parametrize(
    ("raw", "rule_prefix"),
    [
        ("gimp*", "AS-027"),
        ("gimp>=2", "AS-027"),
        ("./gimp", "AS-027"),
        ("-gimp", "AS-027"),
        ("gcc-", "AS-027"),
    ],
)
def test_as027_rule_names_the_rejected_shape(raw: str, rule_prefix: str) -> None:
    with pytest.raises(GinnError) as info:
        PackageName.parse(raw, DistroId("arch"))
    rule = info.value.details["rule"]
    assert isinstance(rule, str)
    assert rule.startswith(rule_prefix)


@pytest.mark.parametrize("distro", DISTROS, ids=lambda distro: distro.value)
@pytest.mark.parametrize(
    "raw", ["firefox", "python3-pip", "gcc-c++", "libstdc++", "gimp-1.2", "g++"]
)
def test_common_names_are_accepted_for_every_distro(distro: DistroId, raw: str) -> None:
    assert PackageName.parse(raw, distro).value == raw


@pytest.mark.parametrize(
    ("distro", "accepted"),
    [
        ("arch", True),
        ("debian", False),
        ("fedora", False),
        ("kali", False),
        ("_test", False),
    ],
)
def test_at_sign_is_an_arch_only_character(distro: str, accepted: bool) -> None:
    if accepted:
        assert PackageName.parse("zfs-dkms@git", DistroId(distro)).value == "zfs-dkms@git"
    else:
        with pytest.raises(GinnError):
            PackageName.parse("zfs-dkms@git", DistroId(distro))


def test_uppercase_is_fedora_only() -> None:
    assert PackageName.parse("Firefox", DistroId("fedora")).value == "Firefox"
    with pytest.raises(GinnError):
        PackageName.parse("Firefox", DistroId("arch"))


def test_single_character_is_not_a_debian_name() -> None:
    assert PackageName.parse("a", DistroId("arch")).value == "a"
    with pytest.raises(GinnError):
        PackageName.parse("a", DistroId("debian"))


def test_version_like_name_is_a_plain_name_not_a_constraint() -> None:
    assert PackageName.parse("gimp-1.2", DistroId("debian")).value == "gimp-1.2"


def test_distro_must_be_a_distro_id() -> None:
    with pytest.raises(TypeError):
        PackageName.parse("gimp", "arch")  # type: ignore[arg-type]


def test_direct_construction_is_checked_like_parse() -> None:
    with pytest.raises(GinnError):
        PackageName("gimp*", DistroId("arch"))
