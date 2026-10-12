# SPDX-License-Identifier: Apache-2.0
"""Basic-tier mount plan: create binds, attach grants, skips, identical paths and DR-004.

Covers runtime/mounts.py. Every filesystem decision goes through a fake view, except the symlink
cases, which build real links under tmp_path only. A refused grant is skipped and reported, and
never raised, so each case asserts the skip message and that nothing was bound.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from ginn.domain.enums import Tier
from ginn.domain.paths import PATH_CODE, SharedPath
from ginn.errors import GinnError
from ginn.podman.adapter import CreateSpec, Mount
from ginn.runtime import mounts
from ginn.runtime.mounts import (
    Attach,
    AttachPlan,
    CreateBinds,
    Mountpoint,
    SessionBind,
    Skipped,
    attach_plan,
    create_binds,
    runtime_dir,
)

pytestmark = [
    pytest.mark.fr("FR-061"),
    pytest.mark.fr("FR-064"),
    pytest.mark.fr("FR-065"),
    pytest.mark.fr("FR-048"),
]

DIGEST = (
    "docker.io/library/alpine@sha256:"
    "d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395"
)
UID = 1000
USER = "alice"
RUNTIME = runtime_dir(UID)
WAYLAND = f"{RUNTIME}/wayland-1"
WAYLAND_REALM = f"{RUNTIME}/wayland-0"
PIPEWIRE = f"{RUNTIME}/pipewire-0"
PULSE = f"{RUNTIME}/pulse/native"
AUTHORITY_REALM = f"{RUNTIME}/ginn-xauthority"
X11_DIR = "/tmp/.X11-unix"  # noqa: S108 - fixed host socket dir, not a temp file
SYSTEM_PATHS = ("/usr/share/fonts", "/etc/localtime", "/dev/dri", "/media", "/mnt")
SESSION_PATHS = (WAYLAND, PIPEWIRE, PULSE)
FULL_SESSION = [
    SessionBind(WAYLAND, WAYLAND_REALM),
    SessionBind(PIPEWIRE, PIPEWIRE),
    SessionBind(PULSE, PULSE),
]
Exists = Callable[[str], bool]


@dataclass(frozen=True)
class Layout:
    home: Path
    realm_home: Path
    owned: tuple[Path, ...]


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    """A host home with a Downloads folder, and Owned Paths under tmp_path."""
    home = tmp_path / "home" / USER
    (home / "Downloads").mkdir(parents=True)
    data = tmp_path / "xdg" / "data" / "ginn"
    config = tmp_path / "xdg" / "config" / "ginn"
    state = tmp_path / "xdg" / "state" / "ginn"
    runtime = tmp_path / "run" / "ginn" / str(UID)
    return Layout(
        home=home,
        realm_home=data / "homes" / "arch",
        owned=(data, config, state, runtime),
    )


@pytest.fixture(autouse=True)
def _xdg_roots_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """SharedPath.parse reads the AS-011 XDG overrides from os.environ when no env is given."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def fake_fs(paths: Sequence[str]) -> Exists:
    """A filesystem view in which exactly `paths` exist. The host is not read."""
    return frozenset(paths).__contains__


def present_all(layout: Layout) -> list[str]:
    return [str(layout.home / "Downloads"), f"/run/media/{USER}", *SYSTEM_PATHS, *SESSION_PATHS]


def create(
    layout: Layout,
    *,
    present: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    realm_home: Path | None = None,
) -> CreateBinds:
    paths = present_all(layout) if present is None else list(present)
    return create_binds(
        Tier.BASIC,
        host_home=layout.home,
        uid=UID,
        realm_home=layout.realm_home if realm_home is None else realm_home,
        owned_paths=layout.owned,
        env={} if env is None else env,
        exists=fake_fs(paths),
    )


def attach(
    layout: Layout,
    *,
    present: Sequence[str] | None = None,
    binds: Sequence[SessionBind] = FULL_SESSION,
    env: Mapping[str, str] | None = None,
    tier: Tier = Tier.BASIC,
    host_home: Path | None = None,
) -> AttachPlan:
    paths = present_all(layout) if present is None else list(present)
    return attach_plan(
        tier,
        host_home=layout.home if host_home is None else host_home,
        uid=UID,
        user=USER,
        owned_paths=layout.owned,
        session_binds=binds,
        env={} if env is None else env,
        exists=fake_fs(paths),
    )


def triples(mounts_: Sequence[Mount]) -> list[tuple[str, str, tuple[str, ...]]]:
    return [(m.source, m.target, m.options) for m in mounts_]


def real(path: str | Path) -> str:
    return os.path.realpath(path)


def argv_mounts(binds: CreateBinds) -> list[str]:
    """Render the create binds through CreateSpec, which runs the Mount checks in adapter."""
    spec = CreateSpec(
        name="ginn-arch-g1",
        image_digest_ref=DIGEST,
        network="host",
        mounts=binds.mounts,
        devices=binds.devices,
        tmpfs=binds.tmpfs,
    )
    argv = spec.to_argv()
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]


def messages(items: Sequence[Skipped]) -> list[str]:
    return [item.message() for item in items]


def test_golden_create_binds_when_everything_exists(layout: Layout) -> None:
    binds = create(layout)
    home = str(layout.home)
    assert triples(binds.mounts) == [
        (real(layout.realm_home), home, ()),
        ("/usr/share/fonts", "/usr/share/fonts", ("ro",)),
        ("/etc/localtime", "/etc/localtime", ("ro",)),
    ]
    assert binds.devices == ["/dev/dri"]
    assert binds.tmpfs == [f"{RUNTIME}:mode=0700,U"]
    assert binds.skipped == []


def test_golden_attach_plan_when_everything_exists(layout: Layout) -> None:
    plan = attach(layout)
    home = str(layout.home)
    assert plan.attach == [
        Attach("share", real(layout.home / "Downloads"), f"{home}/Downloads", "rw"),
        Attach("share", real(f"/run/media/{USER}"), f"/run/media/{USER}", "rw"),
        Attach("share", real("/media"), "/media", "rw"),
        Attach("share", real("/mnt"), "/mnt", "rw"),
        Attach("session", real(WAYLAND), WAYLAND_REALM, "rw"),
        Attach("session", real(PIPEWIRE), PIPEWIRE, "rw"),
        Attach("session", real(PULSE), PULSE, "rw"),
    ]
    assert plan.mountpoints == [
        Mountpoint(f"{home}/Downloads", True, "realm_home"),
        Mountpoint("/run/media", True, "container"),
        Mountpoint(f"/run/media/{USER}", True, "container"),
        Mountpoint("/media", True, "container"),
        Mountpoint("/mnt", True, "container"),
        Mountpoint(WAYLAND_REALM, False, "container"),
        Mountpoint(PIPEWIRE, False, "container"),
        Mountpoint(f"{RUNTIME}/pulse", True, "container"),
        Mountpoint(PULSE, False, "container"),
    ]
    assert plan.skipped == []


def test_golden_argv_carries_only_the_create_binds_in_order(layout: Layout) -> None:
    home = str(layout.home)
    assert argv_mounts(create(layout)) == [
        f"type=bind,source={real(layout.realm_home)},target={home}",
        "type=bind,source=/usr/share/fonts,target=/usr/share/fonts,ro",
        "type=bind,source=/etc/localtime,target=/etc/localtime,ro",
    ]


@pytest.mark.fr("AS-009")
def test_fixed_grant_that_is_a_symlink_is_bound_from_its_literal_path(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check uses the target, but the bind source is the path itself. Podman resolves it."""
    target = layout.home.parent / "zoneinfo" / "UTC"
    target.parent.mkdir(parents=True)
    target.write_text("")
    link = layout.home.parent / "localtime"
    os.symlink(target, link)
    monkeypatch.setattr(mounts, "LOCALTIME", str(link))
    binds = create(layout, present=[*present_all(layout), str(link)])
    assert (str(link), str(link), ("ro",)) in triples(binds.mounts)
    assert all(mount.source != real(target) for mount in binds.mounts)
    assert binds.skipped == []


def test_realm_home_is_mounted_at_host_home_path(layout: Layout) -> None:
    """FR-048: the Realm Home is mounted at the host home path, from a source under Owned Paths."""
    first = create(layout).mounts[0]
    assert first.target == str(layout.home)
    assert first.source == real(layout.realm_home)
    assert any(layout.realm_home.is_relative_to(owned) for owned in layout.owned)


def test_shares_keep_identical_paths(layout: Layout) -> None:
    """FR-065: each grant sits at the same absolute path inside as on the host."""
    by_realm = {entry.realm: entry.host for entry in attach(layout).attach}
    home = str(layout.home)
    assert by_realm[f"{home}/Downloads"] == real(layout.home / "Downloads")
    assert by_realm[f"/run/media/{USER}"] == real(f"/run/media/{USER}")
    assert by_realm["/media"] == real("/media")
    assert by_realm["/mnt"] == real("/mnt")
    assert [m.target for m in create(layout).mounts[1:]] == ["/usr/share/fonts", "/etc/localtime"]


def test_session_paths_sit_under_the_realm_runtime_dir(layout: Layout) -> None:
    session = [entry for entry in attach(layout).attach if entry.kind == "session"]
    assert [entry.realm for entry in session] == [WAYLAND_REALM, PIPEWIRE, PULSE]
    assert all(entry.realm.startswith(f"{RUNTIME}/") for entry in session)


def test_missing_downloads_is_skipped_reported_and_not_created(layout: Layout) -> None:
    """FR-064: a missing tier folder is skipped and reported, and ginn never creates it."""
    downloads = layout.home / "Downloads"
    downloads.rmdir()
    present = [p for p in present_all(layout) if not p.endswith("/Downloads")]
    plan = attach(layout, present=present)
    assert messages(plan.skipped) == ["share skipped: ~/Downloads (missing)"]
    assert all(entry.realm != f"{layout.home}/Downloads" for entry in plan.attach)
    assert all(point.path != f"{layout.home}/Downloads" for point in plan.mountpoints)
    assert not downloads.exists()


def test_missing_external_mount_root_is_skipped_with_its_parent(layout: Layout) -> None:
    present = [p for p in present_all(layout) if p != f"/run/media/{USER}"]
    plan = attach(layout, present=present)
    assert messages(plan.skipped) == [f"share skipped: /run/media/{USER} (missing)"]
    assert all(entry.realm != f"/run/media/{USER}" for entry in plan.attach)
    assert all(point.path != "/run/media" for point in plan.mountpoints)


@pytest.mark.fr("DR-004")
def test_symlinked_mount_root_is_skipped_even_when_its_target_is_allowed(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root that is a link is not attached, although the link and its target lie in $HOME."""
    target = layout.home / "data"
    target.mkdir()
    link = layout.home / "mntlink"
    os.symlink(target, link)
    monkeypatch.setattr(mounts, "MOUNT_ROOTS", (str(link),))
    plan = attach(layout, present=[*present_all(layout), str(link)])
    assert messages(plan.skipped) == [f"share skipped: {link} (a symbolic link, not a mount root)"]
    assert all(entry.realm != str(link) for entry in plan.attach)


def test_missing_gpu_node_and_fonts_are_reported_not_bound(layout: Layout) -> None:
    present = [p for p in present_all(layout) if p not in ("/dev/dri", "/usr/share/fonts")]
    binds = create(layout, present=present)
    assert binds.devices == []
    assert {s.message() for s in binds.skipped} == {
        "device skipped: /dev/dri (missing)",
        "share skipped: /usr/share/fonts (missing)",
    }
    assert all(mount.target != "/usr/share/fonts" for mount in binds.mounts)


def test_session_without_audio_has_no_audio_attaches(layout: Layout) -> None:
    plan = attach(layout, binds=[SessionBind(WAYLAND, WAYLAND_REALM)])
    realms = [entry.realm for entry in plan.attach if entry.kind == "session"]
    assert realms == [WAYLAND_REALM]


def test_downloads_symlink_attaches_resolved_source_at_logical_path(layout: Layout) -> None:
    """The source is the resolved target that was validated. The realm path is the host path."""
    real_dir = layout.home / "data" / "Downloads"
    real_dir.mkdir(parents=True)
    (layout.home / "Downloads").rmdir()
    os.symlink(real_dir, layout.home / "Downloads")
    entry = attach(layout).attach[0]
    assert entry.host == real(real_dir)
    assert entry.realm == f"{layout.home}/Downloads"


@pytest.mark.fr("DR-004")
def test_downloads_symlink_into_ssh_is_skipped_and_not_attached(layout: Layout) -> None:
    (layout.home / "Downloads").rmdir()
    (layout.home / ".ssh").mkdir()
    os.symlink(layout.home / ".ssh", layout.home / "Downloads")
    plan = attach(layout)
    assert messages(plan.skipped) == ["share skipped: ~/Downloads (DR-004.ssh: SSH keys)"]
    assert all(entry.realm != f"{layout.home}/Downloads" for entry in plan.attach)


@pytest.mark.fr("DR-004")
def test_downloads_symlink_into_etc_is_skipped_and_not_attached(layout: Layout) -> None:
    (layout.home / "Downloads").rmdir()
    os.symlink("/etc", layout.home / "Downloads")
    plan = attach(layout)
    assert messages(plan.skipped) == [
        "share skipped: ~/Downloads (DR-004.etc: system configuration)"
    ]
    assert all(entry.realm != f"{layout.home}/Downloads" for entry in plan.attach)


@pytest.mark.fr("DR-004")
def test_downloads_symlink_outside_the_allowed_roots_is_skipped(layout: Layout) -> None:
    """DR-004: a share's final target lies under $HOME, /run/media, /media or /mnt."""
    elsewhere = layout.home.parent.parent / "data" / "Downloads"
    elsewhere.mkdir(parents=True)
    (layout.home / "Downloads").rmdir()
    os.symlink(elsewhere, layout.home / "Downloads")
    plan = attach(layout)
    assert messages(plan.skipped) == [
        "share skipped: ~/Downloads (DR-004: outside $HOME, /run/media, /media and /mnt)"
    ]
    assert all(entry.realm != f"{layout.home}/Downloads" for entry in plan.attach)


@pytest.mark.fr("DR-004")
@pytest.mark.parametrize("name", ["odd,name", "odd:name", "odd\nname"])
def test_downloads_symlink_to_a_separator_target_is_skipped(layout: Layout, name: str) -> None:
    """The link text is clean, but the attach would use the target, which holds a separator."""
    (layout.home / name).mkdir()
    (layout.home / "Downloads").rmdir()
    os.symlink(layout.home / name, layout.home / "Downloads")
    plan = attach(layout)
    assert len(plan.skipped) == 1
    assert plan.skipped[0].reason.startswith("resolves to a path that contains ':' or ','")
    assert all(entry.realm != f"{layout.home}/Downloads" for entry in plan.attach)


@pytest.mark.fr("DR-004")
def test_repointed_symlink_fails_revalidate_and_the_next_build_skips_it(layout: Layout) -> None:
    """revalidate still raises for a user share. The next attach plan skips the grant instead."""
    good = layout.home / "data" / "Downloads"
    good.mkdir(parents=True)
    link = layout.home / "Downloads"
    link.rmdir()
    os.symlink(good, link)
    shared = SharedPath.parse(str(link), home=layout.home, owned_paths=layout.owned, mode="rw")
    shared.revalidate()
    link.unlink()
    (layout.home / ".ssh").mkdir()
    os.symlink(layout.home / ".ssh", link)
    with pytest.raises(GinnError) as revalidate_info:
        shared.revalidate()
    assert revalidate_info.value.code == PATH_CODE
    plan = attach(layout)
    assert messages(plan.skipped) == ["share skipped: ~/Downloads (DR-004.ssh: SSH keys)"]


@pytest.mark.fr("NFR-SEC-006")
def test_no_forbidden_path_is_a_mount_source_or_target(layout: Layout) -> None:
    """DR-004: no source is a forbidden path, and only the Realm Home mounts at $HOME."""
    binds = create(layout)
    plan = attach(layout)
    home = layout.home
    forbidden = {
        "/",
        "/etc",
        "/usr",
        str(home),
        str(home / ".ssh"),
        str(home / ".gnupg"),
        str(home / ".local" / "share" / "applications"),
        str(home / ".config" / "autostart"),
        *(str(path) for path in layout.owned),
    }
    forbidden_real = {real(path) for path in forbidden}
    no_source_under = [real(home / ".ssh"), real(home / ".gnupg"), *map(real, layout.owned)]
    realm_mount, *system = binds.mounts
    assert realm_mount.target == str(home)
    sources = [mount.source for mount in system] + [entry.host for entry in plan.attach]
    targets = [mount.target for mount in system] + [entry.realm for entry in plan.attach]
    for source in sources:
        assert source not in forbidden_real
        assert not any(source.startswith(root + "/") for root in no_source_under)
    assert all(target not in forbidden for target in targets)


@pytest.mark.fr("DR-004")
def test_forbidden_session_bind_is_skipped_and_not_attached(layout: Layout) -> None:
    """A session path (here an X authority file under ~/.ssh) is refused, never attached."""
    key = layout.home / ".ssh" / "authority"
    plan = attach(
        layout,
        present=[*present_all(layout), str(key)],
        binds=[SessionBind(str(key), AUTHORITY_REALM, "ro")],
    )
    assert messages(plan.skipped) == [f"session skipped: {key} (DR-004.ssh: SSH keys)"]
    assert all(entry.kind == "share" for entry in plan.attach)


@pytest.mark.fr("DR-004")
@pytest.mark.parametrize(
    ("secret", "rule", "description"),
    [(".ssh", "DR-004.ssh", "SSH keys"), (".gnupg", "DR-004.gnupg", "GnuPG keys")],
)
def test_fixed_font_grant_resolving_into_a_secret_dir_is_skipped(
    layout: Layout,
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
    rule: str,
    description: str,
) -> None:
    """The exemption covers the /usr subtree of a fixed grant only. The rest of DR-004 applies."""
    target = layout.home / secret / "fonts"
    target.mkdir(parents=True)
    fonts = layout.home.parent / "fonts-link"
    os.symlink(target, fonts)
    monkeypatch.setattr(mounts, "FONTS", str(fonts))
    binds = create(layout, present=[*present_all(layout), str(fonts)])
    assert messages(binds.skipped) == [f"share skipped: {fonts} ({rule}: {description})"]
    assert all(mount.target != str(fonts) for mount in binds.mounts)


def test_authority_file_attaches_read_only_at_the_fixed_realm_name(layout: Layout) -> None:
    authority = str(layout.home / ".Xauthority")
    plan = attach(
        layout,
        present=[*present_all(layout), authority, X11_DIR],
        binds=[
            SessionBind(X11_DIR, X11_DIR, "ro", True),
            SessionBind(authority, AUTHORITY_REALM, "ro"),
        ],
    )
    assert Attach("session", real(authority), AUTHORITY_REALM, "ro") in plan.attach
    assert Attach("session", real(X11_DIR), X11_DIR, "ro") in plan.attach
    assert Mountpoint(AUTHORITY_REALM, False, "container") in plan.mountpoints
    assert Mountpoint(X11_DIR, True, "container") in plan.mountpoints


@pytest.mark.fr("AS-011")
def test_moved_applications_dir_under_mnt_skips_mnt(layout: Layout) -> None:
    """XDG_DATA_HOME moves the AS-011 rule. A mount root that contains the moved path is skipped."""
    plan = attach(layout, env={"XDG_DATA_HOME": "/mnt/xdg"})
    assert messages(plan.skipped) == [
        "share skipped: /mnt (AS-011.applications: host desktop entries)"
    ]
    assert "/mnt" not in [entry.realm for entry in plan.attach]


@pytest.mark.fr("AS-011")
def test_moved_applications_dir_refuses_a_session_bind(layout: Layout) -> None:
    moved = layout.home.parent / "xdg-data"
    socket = moved / "applications" / "authority"
    plan = attach(
        layout,
        present=[*present_all(layout), str(socket)],
        binds=[SessionBind(str(socket), AUTHORITY_REALM, "ro")],
        env={"XDG_DATA_HOME": str(moved)},
    )
    assert messages(plan.skipped) == [
        f"session skipped: {socket} (AS-011.applications: host desktop entries)"
    ]


@pytest.mark.fr("DR-004")
def test_home_under_mnt_skips_mnt_and_keeps_the_rest_of_the_plan(layout: Layout) -> None:
    """A mount root that contains $HOME is refused by DR-004.home. Only that root is skipped."""
    home = Path("/mnt/data/home/alice")
    present = [str(home / "Downloads"), f"/run/media/{USER}", "/media", "/mnt"]
    plan = attach(layout, present=present, binds=[], host_home=home)
    assert messages(plan.skipped) == [
        "share skipped: /mnt (DR-004.home: the home directory itself)"
    ]
    assert [entry.realm for entry in plan.attach] == [
        f"{home}/Downloads",
        f"/run/media/{USER}",
        "/media",
    ]


def test_development_and_custom_tiers_are_not_built_yet(layout: Layout) -> None:
    """Development and Custom add grants in M6 (T6.2). Until then both plans raise."""
    for tier in (Tier.DEVELOPMENT, Tier.CUSTOM):
        with pytest.raises(NotImplementedError):
            create_binds(
                tier,
                host_home=layout.home,
                uid=UID,
                realm_home=layout.realm_home,
                owned_paths=layout.owned,
                env={},
                exists=fake_fs([]),
            )
        with pytest.raises(NotImplementedError):
            attach(layout, tier=tier)


@pytest.mark.fr("DR-021")
def test_realm_home_must_lie_inside_owned_paths(layout: Layout) -> None:
    with pytest.raises(ValueError, match="DR-021"):
        create(layout, realm_home=layout.home / "arch")


@pytest.mark.fr("DR-021")
def test_realm_home_dotdot_out_of_owned_paths_is_refused(layout: Layout) -> None:
    """`..` is resolved before the check. The text still starts under Owned Paths."""
    escape = layout.owned[0] / "homes" / ".." / ".." / ".." / "etc"
    with pytest.raises(ValueError, match="DR-021"):
        create(layout, realm_home=escape)


@pytest.mark.fr("DR-021")
def test_realm_home_through_a_symlink_out_of_owned_paths_is_refused(layout: Layout) -> None:
    homes = layout.owned[0] / "homes"
    homes.mkdir(parents=True)
    os.symlink(layout.home, homes / "escape")
    with pytest.raises(ValueError, match="DR-021"):
        create(layout, realm_home=homes / "escape" / "arch")


@pytest.mark.fr("DR-021")
def test_realm_home_dotdot_that_stays_inside_is_used_as_the_resolved_source(layout: Layout) -> None:
    arch = layout.owned[0] / "homes" / "arch"
    arch.mkdir(parents=True)
    binds = create(layout, realm_home=layout.owned[0] / "homes" / ".." / "homes" / "arch")
    assert binds.mounts[0].source == real(arch)


@pytest.mark.parametrize("user", ["../alice", "a/b", "", ".."])
def test_user_name_must_be_one_path_component(layout: Layout, user: str) -> None:
    with pytest.raises(ValueError):
        attach_plan(
            Tier.BASIC,
            host_home=layout.home,
            uid=UID,
            user=user,
            owned_paths=layout.owned,
            session_binds=[],
            env={},
            exists=fake_fs([]),
        )


def test_relative_host_home_is_refused(layout: Layout) -> None:
    with pytest.raises(ValueError):
        create_binds(
            Tier.BASIC,
            host_home=Path("alice"),
            uid=UID,
            realm_home=layout.realm_home,
            owned_paths=layout.owned,
            env={},
            exists=fake_fs([]),
        )
