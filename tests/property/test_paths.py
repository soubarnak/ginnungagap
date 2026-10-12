# SPDX-License-Identifier: Apache-2.0
"""Shared path rules (DR-004, AS-011) under generated input (NFR-SEC-006).

The concrete table and the symlink cases are in tests/unit/test_domain_paths.py.
"""

import os
import posixpath
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from ginn.domain.paths import FORBIDDEN_RULES, OWNED, ForbiddenRule, Mode, SharedPath  # noqa: E402
from ginn.errors import GinnError  # noqa: E402

pytestmark = [
    pytest.mark.fr("DR-004"),
    pytest.mark.fr("AS-011"),
    pytest.mark.fr("NFR-SEC-006"),
]

HOME = Path("/home/alice")
OWNED_PATH = HOME / ".local" / "share" / "ginn"
OWNED_PATHS = [OWNED_PATH]
ROOTS_UNDER_TEST = ("/run/media", "/media", "/mnt")


def at(template: str) -> str:
    """Expand a rule path into the fake home. Written here, not taken from the code under test."""
    if template == OWNED:
        return str(OWNED_PATH)
    return template.replace("~", str(HOME), 1)


def resolved_forbidden(rule: ForbiddenRule) -> list[Path]:
    if rule.path == OWNED:
        return [Path(os.path.realpath(OWNED_PATH))]
    return [Path(os.path.realpath(at(rule.path)))]


_COMPONENTS = st.sampled_from(
    [
        ".", "..", "", "Music", "Downloads", ".ssh", ".gnupg", ".config", "autostart", "fish",
        "config.fish", ".bashrc", ".local", "share", "applications", "ginn", "realms", "etc",
        "usr", "USB", "bob", "x", "a:b", "c,d",
    ]
)  # fmt: skip
_PREFIXES = st.sampled_from(
    [
        "/home/alice", "/home/alice/.config", "/home/alice/.local/share", "/run/media",
        "/run/media/alice", "/media", "/mnt", "/", "/etc", "/usr", "/home", "/opt", "/home/bob",
    ]
)  # fmt: skip


@given(
    prefix=_PREFIXES,
    parts=st.lists(_COMPONENTS, max_size=7),
    mode=st.sampled_from(["ro", "rw"]),
)
def test_accepted_share_never_reaches_a_forbidden_target(
    prefix: str, parts: list[str], mode: Mode
) -> None:
    raw = posixpath.join(prefix, *parts)
    try:
        shared = SharedPath.parse(raw, home=HOME, owned_paths=OWNED_PATHS, mode=mode)
    except GinnError as err:
        assert err.code == "GINN-E-PATH-FORBIDDEN"
        return
    target = shared.path
    assert target == Path(os.path.realpath(target)), "accepted path is not canonical"
    assert ":" not in str(target) and "," not in str(target), "accepted path has a separator"
    roots = [HOME, *(Path(os.path.realpath(root)) for root in ROOTS_UNDER_TEST)]
    assert any(root in target.parents for root in roots), "accepted path is outside every root"
    for rule in FORBIDDEN_RULES:
        exempt = rule.ro_only and mode == "ro"
        for forbidden in resolved_forbidden(rule):
            if rule.subtree:
                assert forbidden not in target.parents, rule.rule_id
            if not exempt:
                assert target != forbidden, rule.rule_id
                assert target not in forbidden.parents, rule.rule_id
    again = SharedPath.parse(str(target), home=HOME, owned_paths=OWNED_PATHS, mode=mode)
    assert again.path == target, "validated path changes when validated again"
