# SPDX-License-Identifier: Apache-2.0
"""Shared path rules (DR-004, AS-011) and the never-shareable table (NFR-SEC-006).

Concrete cases only, so they run without hypothesis. The generated-input check is in
tests/property/test_paths.py.
"""

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from ginn.domain.paths import (
    FORBIDDEN_RULES,
    OWNED,
    ROOTS_REASON,
    ForbiddenRule,
    Mode,
    SharedPath,
    path_refusal,
    under_allowed_root,
)
from ginn.errors import GinnError

pytestmark = [
    pytest.mark.fr("DR-004"),
    pytest.mark.fr("AS-011"),
    pytest.mark.fr("NFR-SEC-006"),
]

HOME = Path("/home/alice")
OWNED_PATH = HOME / ".local" / "share" / "ginn"
OWNED_PATHS: Sequence[Path] = [OWNED_PATH]


@pytest.fixture(autouse=True)
def _xdg_roots_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default rules assume the XDG roots are unset. A test that sets one does it itself."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


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
    ("/home/alice/Music:x", "ro"),
    ("/home/alice/Music,x", "rw"),
    ("/home/alice/x:y/../Music", "rw"),
    ("/run/media/alice/USB:1", "rw"),
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
    ("/home/alice/Music\nx", "rw"),
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
        SharedPath(
            path=HOME / "Music",
            mode="rw",
            target_exists=False,
            _raw="/home/alice/Music",
            _home=HOME,
            _owned=(),
            _xdg=(),
            _token=object(),
        )


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


@pytest.mark.parametrize("name", ["odd:name", "odd,name", "odd\nname"])
def test_symlink_to_a_target_with_a_separator_is_refused(real_home: Path, name: str) -> None:
    """The link text has no separator, but the mount would use the target, which does."""
    (real_home / name).mkdir()
    (real_home / "link").symlink_to(real_home / name)
    with pytest.raises(GinnError) as info:
        SharedPath.parse(str(real_home / "link"), home=real_home, owned_paths=[], mode="ro")
    assert info.value.code == "GINN-E-PATH-FORBIDDEN"
    assert info.value.details["path"] == str(real_home / "link")
    assert "':' or ','" in info.value.details["reason"]


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


@pytest.mark.fr("AS-011")
@pytest.mark.fr("NFR-SEC-006")
def test_xdg_data_home_moves_the_desktop_entry_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(HOME / "share"))
    err = refusal(str(HOME / "share" / "applications"), "rw")
    assert err.details["reason"].startswith("AS-011.applications")
    refusal(str(HOME / "share" / "applications" / "ginn-x.desktop"), "ro")  # subtree
    refusal(str(HOME / "share"), "rw")  # an ancestor of the forbidden path
    assert share(str(HOME / "share" / "Music"), "rw").mode == "rw"  # the rest stays shareable


@pytest.mark.fr("AS-011")
@pytest.mark.fr("NFR-SEC-006")
def test_hard_coded_desktop_entry_rule_still_applies_when_xdg_data_home_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(HOME / "share"))
    err = refusal(str(HOME / ".local" / "share" / "applications"), "rw")
    assert err.details["reason"].startswith("AS-011.applications")


@pytest.mark.fr("AS-011")
@pytest.mark.fr("NFR-SEC-006")
def test_xdg_config_home_moves_the_autostart_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(HOME / "cfg"))
    err = refusal(str(HOME / "cfg" / "autostart" / "x.desktop"), "ro")
    assert err.details["reason"].startswith("AS-011.autostart")
    refusal(str(HOME / "cfg"), "rw")  # an ancestor of the forbidden path
    assert share(str(HOME / "cfg" / "fish"), "ro").mode == "ro"  # other config stays shareable
    refusal(str(HOME / ".config" / "autostart" / "x.desktop"), "ro")  # the default still applies


@pytest.mark.fr("AS-011")
@pytest.mark.parametrize("value", ["", "share"])
def test_empty_or_relative_xdg_values_add_no_rule(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", value)
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    assert share(str(HOME / "share" / "applications"), "rw").mode == "rw"
    assert share(str(HOME / "share" / "autostart" / "x.desktop"), "ro").mode == "ro"


@pytest.mark.fr("DR-004")
@pytest.mark.fr("NFR-SEC-006")
def test_revalidate_accepts_an_unchanged_share(real_home: Path) -> None:
    (real_home / "Docs").symlink_to(real_home / "Music")
    shared = SharedPath.parse(str(real_home / "Docs"), home=real_home, owned_paths=[], mode="rw")
    shared.revalidate()
    assert shared.path == (real_home / "Music").resolve()


@pytest.mark.fr("DR-004")
@pytest.mark.fr("NFR-SEC-006")
def test_revalidate_refuses_a_link_swapped_to_another_target(real_home: Path) -> None:
    (real_home / "Other").mkdir()
    (real_home / "live").symlink_to(real_home / "Music")
    shared = SharedPath.parse(str(real_home / "live"), home=real_home, owned_paths=[], mode="rw")
    (real_home / "live").unlink()
    (real_home / "live").symlink_to(real_home / "Other")

    with pytest.raises(GinnError) as info:
        shared.revalidate()

    assert info.value.code == "GINN-E-PATH-FORBIDDEN"
    assert info.value.details["path"] == str(real_home / "live")
    assert "changed" in info.value.details["reason"]


@pytest.mark.fr("DR-004")
@pytest.mark.fr("NFR-SEC-006")
def test_revalidate_refuses_a_link_swapped_to_a_forbidden_target(real_home: Path) -> None:
    (real_home / "live").symlink_to(real_home / "Music")
    shared = SharedPath.parse(str(real_home / "live"), home=real_home, owned_paths=[], mode="rw")
    (real_home / "live").unlink()
    (real_home / "live").symlink_to(real_home / ".ssh")

    with pytest.raises(GinnError) as info:
        shared.revalidate()

    assert info.value.details["reason"].startswith("DR-004.ssh")


PRIVATE_TARGETS = [
    # (target, expected rule or None, mode, fixed grant or None)
    ("/", "DR-004.root", "rw", None),
    ("/etc", "DR-004.etc", "rw", None),
    ("/etc/localtime", None, "ro", "/etc/localtime"),
    ("/etc/passwd", "DR-004.etc", "ro", "/etc/localtime"),
    ("/usr/share/fonts", None, "ro", "/usr/share/fonts"),
    ("/usr/share/fonts", "DR-004.usr", "ro", None),
    ("/usr/bin/x", "DR-004.usr", "ro", "/usr/share/fonts"),
    ("/home/alice", "DR-004.home", "rw", None),
    ("/home", "DR-004.home", "rw", None),
    ("/home/alice/.ssh/key", "DR-004.ssh", "rw", None),
    ("/home/alice/.ssh/key", "DR-004.ssh", "ro", None),
    # The subtree hole: a fixed grant exempts its own system subtree, not the rest of DR-004.
    ("/home/alice/.ssh/fonts", "DR-004.ssh", "ro", "/usr/share/fonts"),
    ("/home/alice/.bashrc", "AS-011.bashrc", "rw", None),
    ("/home/alice/.bashrc", None, "ro", None),
    ("/home/alice/.local/share/ginn/realms/x", "DR-004.owned", "rw", None),
    ("/home/alice/Downloads", None, "rw", None),
]


@pytest.mark.parametrize(("target", "rule", "mode", "fixed"), PRIVATE_TARGETS)
def test_path_refusal_names_the_rule_or_grants_a_fixed_exemption(
    target: str, rule: str | None, mode: Mode, fixed: str | None
) -> None:
    found = path_refusal(
        Path(target),
        env={},
        home=HOME,
        owned_paths=OWNED_PATHS,
        mode=mode,
        fixed=fixed,
    )
    if rule is None:
        assert found is None
    else:
        assert found is not None
        assert found.startswith(rule)


@pytest.mark.parametrize(
    ("moved", "target", "mode"),
    [
        ("/data/xdg", "/data/xdg/applications/x.desktop", "rw"),
        ("/data/xdg", "/data/xdg/applications", "ro"),
        ("/data/xdg", "/data/xdg/applications/x.desktop", "ro"),
    ],
)
def test_moved_xdg_data_home_refuses_its_applications_dir(
    moved: str, target: str, mode: Mode
) -> None:
    """AS-011: the rule follows $XDG_DATA_HOME, for a share and for a session bind alike."""
    env = {"XDG_DATA_HOME": moved}
    found = path_refusal(Path(target), env=env, home=HOME, owned_paths=OWNED_PATHS, mode=mode)
    assert found is not None
    assert found.startswith("AS-011.applications")
    assert path_refusal(Path(target), env={}, home=HOME, owned_paths=OWNED_PATHS, mode=mode) is None


def test_relative_xdg_override_is_ignored() -> None:
    env = {"XDG_DATA_HOME": "data", "XDG_CONFIG_HOME": "config"}
    found = path_refusal(
        Path("/data/applications"), env=env, home=HOME, owned_paths=OWNED_PATHS, mode="rw"
    )
    assert found is None


def test_shared_path_parse_takes_the_xdg_override_from_its_env_parameter() -> None:
    env = {"XDG_DATA_HOME": "/home/alice/xdg"}
    with pytest.raises(GinnError) as info:
        SharedPath.parse(
            "/home/alice/xdg/applications/x.desktop",
            home=HOME,
            owned_paths=OWNED_PATHS,
            mode="rw",
            env=env,
        )
    assert info.value.details["reason"].startswith("AS-011.applications")
    shared = SharedPath.parse(
        "/home/alice/xdg/applications/x.desktop",
        home=HOME,
        owned_paths=OWNED_PATHS,
        mode="rw",
        env={},
    )
    assert shared.path == Path("/home/alice/xdg/applications/x.desktop")


def test_mount_root_that_contains_home_is_refused() -> None:
    """A /mnt that holds $HOME exposes the home directory, so DR-004.home refuses it."""
    home = Path("/mnt/data/home/alice")
    found = path_refusal(Path("/mnt"), env={}, home=home, owned_paths=[], mode="rw")
    assert found is not None
    assert found.startswith("DR-004.home")


def test_media_root_that_contains_an_owned_path_is_refused() -> None:
    owned = [Path("/media/usb/ginn")]
    found = path_refusal(Path("/media"), env={}, home=HOME, owned_paths=owned, mode="rw")
    assert found is not None
    assert found.startswith("DR-004.owned")


def test_symlinked_mount_root_is_refused_as_its_target(tmp_path: Path) -> None:
    """A mount root that is a symlink to /etc is refused like /etc itself."""
    link = tmp_path / "mnt"
    os.symlink("/etc", link)
    found = path_refusal(link, env={}, home=HOME, owned_paths=OWNED_PATHS, mode="rw")
    assert found is not None
    assert found.startswith("DR-004.etc")


def test_symlink_to_a_newline_target_is_refused_with_the_separator_reason(real_home: Path) -> None:
    (real_home / "odd\nname").mkdir()
    (real_home / "link").symlink_to(real_home / "odd\nname")
    found = path_refusal(real_home / "link", env={}, home=real_home, owned_paths=[], mode="ro")
    assert found is not None
    assert found.startswith("resolves to a path that contains ':' or ','")


def test_under_allowed_root_counts_a_root_itself_only_when_asked() -> None:
    assert under_allowed_root(Path("/media"), home=HOME, root_itself=True) is True
    assert under_allowed_root(Path("/media"), home=HOME) is False
    assert under_allowed_root(Path("/media/usb"), home=HOME) is True
    assert under_allowed_root(Path("/data/Music"), home=HOME, root_itself=True) is False


def test_share_outside_the_allowed_roots_reports_the_dr004_root_reason() -> None:
    with pytest.raises(GinnError) as info:
        SharedPath.parse("/data/Music", home=HOME, owned_paths=OWNED_PATHS, mode="rw")
    assert info.value.details["reason"] == ROOTS_REASON
