# SPDX-License-Identifier: Apache-2.0
"""Shared path rules (DR-004, AS-011) and the never-shareable table (NFR-SEC-006).

Concrete cases only, so they run without hypothesis. The generated-input check is in
tests/property/test_paths.py.
"""

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from ginn.domain.paths import FORBIDDEN_RULES, OWNED, ForbiddenRule, Mode, SharedPath
from ginn.errors import GinnError

pytestmark = [
    pytest.mark.fr("DR-004"),
    pytest.mark.fr("AS-011"),
    pytest.mark.fr("NFR-SEC-006"),
]

HOME = Path("/home/alice")
OWNED_PATH = HOME / ".local" / "share" / "ginn"
OWNED_PATHS: Sequence[Path] = [OWNED_PATH]


def share(raw: str, mode: Mode = "rw", home: Path = HOME) -> SharedPath:
    return SharedPath.parse(raw, home=home, owned_paths=OWNED_PATHS, mode=mode)


def refusal(raw: str, mode: Mode = "rw", home: Path = HOME) -> GinnError:
    with pytest.raises(GinnError) as info:
        share(raw, mode, home)
    err = info.value
    assert err.code == "GINN-E-PATH-FORBIDDEN"
    assert err.exit_code == 2
    assert err.details["path"] == raw
    assert isinstance(err.details["reason"], str)
    return err


def at(template: str) -> str:
    """Expand a rule path into the fake home. Written here, not taken from the code under test."""
    if template == OWNED:
        return str(OWNED_PATH)
    return template.replace("~", str(HOME), 1)


RULE_IDS = [rule.rule_id for rule in FORBIDDEN_RULES]
SUBTREE_RULES = [rule for rule in FORBIDDEN_RULES if rule.subtree]


@pytest.mark.parametrize("rule", FORBIDDEN_RULES, ids=RULE_IDS)
def test_forbidden_path_is_refused_rw(rule: ForbiddenRule) -> None:
    err = refusal(at(rule.path), "rw")
    assert err.details["reason"].startswith(rule.rule_id)


@pytest.mark.parametrize("rule", FORBIDDEN_RULES, ids=RULE_IDS)
def test_forbidden_path_is_refused_ro_unless_it_is_a_startup_file(rule: ForbiddenRule) -> None:
    if rule.ro_only:
        shared = share(at(rule.path), "ro")
        assert shared.path == Path(os.path.realpath(at(rule.path)))
    else:
        err = refusal(at(rule.path), "ro")
        assert err.details["reason"].startswith(rule.rule_id)


@pytest.mark.parametrize("rule", SUBTREE_RULES, ids=[rule.rule_id for rule in SUBTREE_RULES])
@pytest.mark.parametrize("mode", ["ro", "rw"])
def test_anything_under_a_subtree_forbidden_path_is_refused(
    rule: ForbiddenRule, mode: Mode
) -> None:
    refusal(at(rule.path) + "/child/grandchild", mode)


@pytest.mark.parametrize(
    "raw",
    [
        "/home/alice/.local/share",
        "/home/alice/.local",
        "/home/alice/.config",
        "/home/alice/.config/fish/..",
        "/",
        "/usr",
        "/home",
    ],
)
@pytest.mark.parametrize("mode", ["ro", "rw"])
def test_ancestors_of_forbidden_paths_are_refused(raw: str, mode: Mode) -> None:
    refusal(raw, mode)


ACCEPTED = [
    ("/home/alice/Music", "ro"),
    ("/home/alice/Music", "rw"),
    ("/home/alice/Downloads", "rw"),
    ("/home/alice/.bashrc", "ro"),
    ("/home/alice/.config/fish", "ro"),
    ("/home/alice/.config/fish/config.fish", "ro"),
    ("/home/alice/Documents/../Music", "rw"),
    ("/run/media/alice/USB", "rw"),
    ("/run/media/alice", "ro"),
    ("/media/usb", "ro"),
    ("/mnt/data", "rw"),
]

REFUSED = [
    ("/home/alice", "ro"),
    ("/home/alice", "rw"),
    ("/home/alice/.ssh", "ro"),
    ("/home/alice/.ssh/id_ed25519", "ro"),
    ("/home/alice/.gnupg", "rw"),
    ("/etc", "ro"),
    ("/etc/passwd", "ro"),
    ("/", "ro"),
    ("/usr/bin", "ro"),
    ("/home/alice/.config", "rw"),
    ("/home/alice/.config", "ro"),
    ("/home/alice/.bashrc", "rw"),
    ("/home/alice/.config/fish/config.fish", "rw"),
    ("/home/alice/.config/fish", "rw"),
    ("/home/alice/.local/share/applications", "rw"),
    ("/home/alice/.config/autostart/x.desktop", "ro"),
    ("/home/alice/.local/share/ginn", "ro"),
    ("/home/alice/.local/share/ginn/realms/x", "rw"),
    ("/home/alice/.local/share", "rw"),
    ("/run/media", "ro"),
    ("/run/media", "rw"),
    ("/media", "rw"),
    ("/mnt", "rw"),
    ("/opt/x", "rw"),
    ("/home/bob/x", "rw"),
    ("/home/alice/Music/../.ssh", "ro"),
    ("/home/alice/Music/../../bob", "rw"),
    ("~/Music", "ro"),
    ("Music", "rw"),
    ("", "ro"),
    ("/home/alice/x\x00", "rw"),
]


@pytest.mark.parametrize(("raw", "mode"), ACCEPTED)
def test_permitted_shares_are_accepted(raw: str, mode: Mode) -> None:
    shared = share(raw, mode)
    assert shared.mode == mode
    assert shared.path == Path(os.path.realpath(raw))


@pytest.mark.parametrize(("raw", "mode"), REFUSED)
def test_forbidden_shares_are_refused(raw: str, mode: Mode) -> None:
    refusal(raw, mode)


def test_dotdot_inside_home_is_normalized() -> None:
    assert share("/home/alice/Music/./../Downloads", "rw").path == HOME / "Downloads"


def test_dotdot_into_ssh_is_refused() -> None:
    refusal("/home/alice/Music/../.ssh", "ro")


def test_startup_file_is_read_only_only() -> None:
    assert share("/home/alice/.bashrc", "ro").mode == "ro"
    refusal("/home/alice/.bashrc", "rw")


def test_config_rw_is_refused_because_of_autostart() -> None:
    assert refusal("/home/alice/.config", "rw").details["reason"].startswith("AS-011.autostart")


def test_refusal_names_the_rule_that_matched() -> None:
    assert refusal("/etc", "ro").details["reason"].startswith("DR-004.etc")
    assert refusal("/", "rw").details["reason"].startswith("DR-004.root")


def test_direct_construction_without_parse_is_refused() -> None:
    with pytest.raises(TypeError):
        SharedPath(path=HOME / "Music", mode="rw", target_exists=False, _token=object())


def test_home_must_be_absolute() -> None:
    with pytest.raises(ValueError):
        SharedPath.parse("/home/alice/Music", home=Path("alice"), owned_paths=[], mode="rw")


def test_bad_mode_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        SharedPath.parse("/home/alice/Music", home=HOME, owned_paths=[], mode="x")  # type: ignore[arg-type]


def test_target_exists_flag_reports_a_missing_target() -> None:
    assert share("/home/alice/Music").target_exists is False


@pytest.fixture
def real_home(tmp_path: Path) -> Path:
    home = tmp_path / "alice"
    (home / ".ssh").mkdir(parents=True)
    (home / "Music").mkdir()
    (tmp_path / "outside").mkdir()
    return home


def test_existing_target_is_flagged_as_existing(real_home: Path) -> None:
    shared = SharedPath.parse(str(real_home / "Music"), home=real_home, owned_paths=[], mode="rw")
    assert shared.target_exists is True
    assert shared.path == (real_home / "Music").resolve()


def test_symlink_into_ssh_is_refused(real_home: Path) -> None:
    (real_home / "safe").symlink_to(real_home / ".ssh")
    with pytest.raises(GinnError) as info:
        SharedPath.parse(str(real_home / "safe"), home=real_home, owned_paths=[], mode="ro")
    assert info.value.details["reason"].startswith("DR-004.ssh")


def test_dangling_symlink_into_ssh_is_refused(real_home: Path) -> None:
    (real_home / "later").symlink_to(real_home / ".ssh" / "new-key")
    with pytest.raises(GinnError):
        SharedPath.parse(str(real_home / "later"), home=real_home, owned_paths=[], mode="ro")


def test_dangling_symlink_to_a_missing_target_is_accepted_and_flagged(real_home: Path) -> None:
    (real_home / "Docs").symlink_to(real_home / "Music" / "not-yet")
    shared = SharedPath.parse(str(real_home / "Docs"), home=real_home, owned_paths=[], mode="rw")
    assert shared.target_exists is False
    assert shared.path == (real_home / "Music" / "not-yet").resolve()


def test_dotdot_follows_the_symlink_not_the_text(real_home: Path, tmp_path: Path) -> None:
    # Lexical normpath would give real_home/Music, which is allowed. The kernel resolves the
    # link first, so the real target is tmp_path/Music, outside $HOME. It must be refused.
    (real_home / "hop").symlink_to(tmp_path / "outside")
    with pytest.raises(GinnError):
        SharedPath.parse(
            str(real_home / "hop" / ".." / "Music"), home=real_home, owned_paths=[], mode="rw"
        )


def test_symlink_inside_home_is_accepted_at_its_target(real_home: Path) -> None:
    (real_home / "Docs").symlink_to(real_home / "Music")
    shared = SharedPath.parse(str(real_home / "Docs"), home=real_home, owned_paths=[], mode="rw")
    assert shared.path == (real_home / "Music").resolve()
