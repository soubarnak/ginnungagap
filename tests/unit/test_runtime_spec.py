# SPDX-License-Identifier: Apache-2.0
"""The Basic realm spec renders a fixed, never-privileged `podman create` argv, services off or on.

Covers runtime/spec.py. The golden argvs use a synthetic digest and a fake view of the filesystem,
so a Trust Manifest refresh does not move them. The image test reads the packaged manifest, and it
checks the form of the reference, not its value. Nothing here runs podman.
"""

import itertools
import re
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from ginn.distros import available
from ginn.distros import load as load_distro
from ginn.domain.enums import DistroId, Tier
from ginn.domain.names import RealmName
from ginn.domain.permissions import BASIC_TOGGLES, Permissions, Toggle
from ginn.podman.adapter import CreateSpec
from ginn.runtime._enter_plan import login_argv
from ginn.runtime.mounts import Skipped, create_binds
from ginn.runtime.spec import (
    RealmSpec,
    create_argv,
    create_spec,
    realm_passwd_entry,
    realm_spec,
    run_user_tmpfiles,
    services_cgroup_parent,
)
from ginn.trust import pull_reference

HOME = Path("/home/alice")
USER = "alice"
OWNED = Path("/home/alice/.local/share/ginn")
REALM_HOME = OWNED / "homes" / "arch"
UID = 1000
GID = 1000
DIGEST = "quay.io/archlinux/archlinux@sha256:" + "ab" * 32

FONTS = "/usr/share/fonts"
LOCALTIME = "/etc/localtime"
GPU = "/dev/dri"
STABLE_PATHS = (FONTS, LOCALTIME, GPU)
VARIABLE_PATHS = (
    "/home/alice/Downloads",
    "/home/alice/Projects",
    "/run/media/alice",
    "/media",
    "/mnt",
    "/run/user/1000/wayland-0",
    "/run/user/1000/ginn-xauthority",
    "/run/user/1000/pipewire-0",
    "/run/user/1000/pulse/native",
    "/tmp/.X11-unix",  # noqa: S108 - fixed host socket dir, not a temp file
)
VARIABLE_TOKENS = (
    "Downloads",
    "Projects",
    "/run/media",
    "/media",
    "/mnt",
    "wayland",
    "xauthority",
    "pipewire",
    "pulse",
    "X11",
)
STABLE_SUBSETS = [
    tuple(combo)
    for size in range(len(STABLE_PATHS) + 1)
    for combo in itertools.combinations(STABLE_PATHS, size)
]
NON_KALI = [distro for distro in available() if distro != "kali"]

GOLDEN_ARCH = [
    "create",
    "--name",
    "ginn-arch-g1",
    "--user",
    "1000:1000",
    "--userns",
    "keep-id",
    "--passwd-entry",
    "alice:*:1000:1000:alice:/home/alice:/bin/bash",
    "--network",
    "host",
    "--http-proxy=false",
    "--init",
    "--label",
    "io.ginn.distro=arch",
    "--label",
    "io.ginn.generation=1",
    "--label",
    "io.ginn.realm=arch",
    "--mount",
    "type=bind,source=/home/alice/.local/share/ginn/homes/arch,target=/home/alice",
    "--mount",
    "type=bind,source=/usr/share/fonts,target=/usr/share/fonts,ro",
    "--mount",
    "type=bind,source=/etc/localtime,target=/etc/localtime,ro",
    "--tmpfs",
    "/run/user/1000:mode=0700,U",
    "--device",
    "/dev/dri",
    "--entrypoint",
    '["sleep"]',
    DIGEST,
    "infinity",
]


GOLDEN_ARCH_SERVICES_ON = [
    "create",
    "--name",
    "ginn-arch-g1",
    "--user",
    "0:0",
    "--userns",
    "keep-id",
    "--passwd-entry",
    "alice:*:1000:1000:alice:/home/alice:/bin/bash",
    "--network",
    "host",
    "--http-proxy=false",
    "--cgroupns",
    "private",
    "--cgroup-parent",
    "/ginn/1000/arch",
    "--conmon-pidfile",
    "/run/ginn/1000/services/arch.conmon.pid",
    "--systemd=always",
    "--stop-signal",
    "SIGRTMIN+3",
    "--stop-timeout",
    "30",
    "--env",
    "SYSTEMD_LOG_LEVEL=warning",
    "--label",
    "io.ginn.distro=arch",
    "--label",
    "io.ginn.generation=1",
    "--label",
    "io.ginn.realm=arch",
    "--mount",
    "type=bind,source=/home/alice/.local/share/ginn/homes/arch,target=/home/alice",
    "--mount",
    "type=bind,source=/usr/share/fonts,target=/usr/share/fonts,ro",
    "--mount",
    "type=bind,source=/etc/localtime,target=/etc/localtime,ro",
    "--tmpfs",
    "/run/user/1000:mode=0700,U",
    "--device",
    "/dev/dri",
    "--entrypoint",
    '["/sbin/init"]',
    DIGEST,
]


def _present(paths: Iterable[str]) -> Callable[[str], bool]:
    """A fake filesystem view: exactly these paths exist."""
    found = frozenset(paths)
    return lambda path: path in found


def make_spec(
    distro: str = "arch",
    *,
    name: str = "arch",
    present: Iterable[str] = STABLE_PATHS,
    uid: int = UID,
    gid: int = GID,
    image: str = DIGEST,
    services: bool = False,
    tier: Tier = Tier.BASIC,
    generation: int = 1,
) -> RealmSpec:
    """A spec with the stable binds planned from `present`. Its binds are always Basic's."""
    binds = create_binds(
        Tier.BASIC,
        host_home=HOME,
        uid=uid,
        realm_home=OWNED / "homes" / name,
        owned_paths=[OWNED],
        env={},
        exists=_present(present),
    )
    return RealmSpec(
        name=RealmName(name),
        distro=DistroId(distro),
        image=image,
        uid=uid,
        gid=gid,
        user=USER,
        login_shell=load_distro(distro).shell,
        home=HOME,
        binds=binds,
        tier=tier,
        services=services,
        conmon_pidfile=f"/run/ginn/{uid}/services/{name}.conmon.pid" if services else None,
        generation=generation,
    )


def spec_from_binds(
    *,
    binds_uid: int = UID,
    binds_home: Path = HOME,
    uid: int = UID,
    home: Path = HOME,
) -> RealmSpec:
    """A spec whose binds were planned for one uid and home, and whose fields say another."""
    binds = create_binds(
        Tier.BASIC,
        host_home=binds_home,
        uid=binds_uid,
        realm_home=REALM_HOME,
        owned_paths=[OWNED],
        env={},
        exists=_present(STABLE_PATHS),
    )
    return RealmSpec(
        name=RealmName("arch"),
        distro=DistroId("arch"),
        image=DIGEST,
        uid=uid,
        gid=GID,
        user=USER,
        login_shell="/bin/bash",
        home=home,
        binds=binds,
    )


def option_values(argv: list[str], flag: str) -> list[str]:
    """Every value that follows `flag` in argv."""
    return [argv[index + 1] for index, arg in enumerate(argv[:-1]) if arg == flag]


def _pairs(argv: list[str]) -> list[tuple[str, str]]:
    """The (flag, value) pairs of an argv, for every flag that takes a value."""
    return [(token, argv[i + 1]) for i, token in enumerate(argv[:-1]) if token.startswith("--")]


def mount_targets(argv: list[str]) -> list[str]:
    """The target of every `--mount` value, in argv order."""
    return [
        part.removeprefix("target=")
        for value in option_values(argv, "--mount")
        for part in value.split(",")
        if part.startswith("target=")
    ]


def assert_basic_argv(argv: list[str]) -> None:
    """The invariants every Basic, services-off argv holds, whatever its binds are."""
    assert all(isinstance(arg, str) for arg in argv)
    assert argv[0] == "create"
    assert not any(arg == "--privileged" or arg.startswith("--privileged=") for arg in argv)
    assert "--cap-add" not in argv
    assert "--security-opt" not in argv
    assert "--env" not in argv
    assert "--http-proxy=false" in argv
    assert "--passwd=false" not in argv
    entry = argv[argv.index("--passwd-entry") + 1]
    assert entry.startswith("alice:*:") and entry.endswith(":alice:/home/alice:/bin/bash")
    assert "--init" in argv
    assert "--init-path" not in argv
    assert not any(arg.startswith("--systemd") for arg in argv)
    assert "--cgroup-parent" not in argv
    assert "--runtime" not in argv
    assert argv[-1] == "infinity"
    repository, separator, digest = argv[-2].partition("@")
    assert separator == "@" and digest.startswith("sha256:")
    assert re.fullmatch(r"[0-9a-f]{64}", digest.removeprefix("sha256:")) is not None
    assert ":" not in repository


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-026")
@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-082")
@pytest.mark.fr("NFR-SEC-001")
def test_basic_services_off_golden_argv() -> None:
    """The golden argv for arch. `--network host` is the non-Kali network of FR-026 and AS-006."""
    argv = create_argv(make_spec())
    assert argv == GOLDEN_ARCH


@pytest.mark.fr("FR-061")
def test_stable_binds_only_and_no_variable_grant_even_when_every_source_exists() -> None:
    spec = make_spec(present=(*STABLE_PATHS, *VARIABLE_PATHS))
    argv = create_argv(spec)
    assert mount_targets(argv) == [str(HOME), FONTS, LOCALTIME]
    assert option_values(argv, "--tmpfs") == ["/run/user/1000:mode=0700,U"]
    assert option_values(argv, "--device") == [GPU]
    for path in VARIABLE_PATHS:
        assert path not in argv
    for arg in argv:
        assert not any(token in arg for token in VARIABLE_TOKENS), arg


@pytest.mark.fr("FR-048")
def test_realm_home_is_mounted_at_the_host_home_path_from_owned_paths() -> None:
    argv = create_argv(make_spec())
    home_mount = option_values(argv, "--mount")[0]
    assert home_mount == f"type=bind,source={REALM_HOME},target={HOME}"
    assert str(REALM_HOME).startswith(str(OWNED) + "/")


@pytest.mark.fr("FR-048")
def test_a_realm_home_bind_for_another_home_is_refused() -> None:
    with pytest.raises(ValueError, match="FR-048"):
        spec_from_binds(binds_home=HOME, home=Path("/home/bob"))


@pytest.mark.fr("FR-010")
def test_a_runtime_tmpfs_for_another_uid_is_refused() -> None:
    with pytest.raises(ValueError, match="runtime tmpfs"):
        spec_from_binds(binds_uid=UID, uid=UID + 1)


@pytest.mark.fr("FR-061")
def test_missing_stable_sources_are_skipped_and_the_realm_home_is_kept() -> None:
    spec = make_spec(present=())
    argv = create_argv(spec)
    assert mount_targets(argv) == [str(HOME)]
    assert "--device" not in argv
    reasons = {(item.kind, item.name): item.reason for item in spec.binds.skipped}
    assert reasons[("share", FONTS)] == "missing"
    assert reasons[("share", LOCALTIME)] == "missing"
    assert reasons[("device", GPU)] == "missing"


@pytest.mark.fr("FR-011")
@pytest.mark.fr("NFR-SEC-004")
def test_image_is_the_digest_the_trust_manifest_pins() -> None:
    binds = create_binds(
        Tier.BASIC,
        host_home=HOME,
        uid=UID,
        realm_home=REALM_HOME,
        owned_paths=[OWNED],
        env={},
        exists=_present(STABLE_PATHS),
    )
    spec = realm_spec(
        RealmName("arch"),
        DistroId("arch"),
        uid=UID,
        gid=GID,
        user=USER,
        login_shell="/bin/bash",
        host_home=HOME,
        realm_home=REALM_HOME,
        owned_paths=[OWNED],
        env={},
        exists=_present(STABLE_PATHS),
    )
    assert spec.binds == binds
    assert spec.image == pull_reference("arch")
    assert create_argv(spec)[-2] == spec.image
    assert_basic_argv(create_argv(spec))


@pytest.mark.fr("FR-011")
@pytest.mark.fr("NFR-SEC-004")
@pytest.mark.parametrize(
    "image",
    [
        "quay.io/archlinux/archlinux:base",
        "quay.io/archlinux/archlinux",
        "quay.io/archlinux/archlinux@sha256:abc",
    ],
)
def test_a_tag_or_short_digest_is_never_accepted_as_the_image(image: str) -> None:
    with pytest.raises(ValueError, match="FR-011"):
        make_spec(image=image)


@pytest.mark.fr("NFR-SEC-001")
@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("distro", NON_KALI)
@pytest.mark.parametrize("stable", STABLE_SUBSETS, ids=lambda subset: "+".join(subset) or "none")
@pytest.mark.parametrize("variable", [False, True], ids=["no-variable", "all-variable"])
@pytest.mark.parametrize("uid", [UID, 4242])
def test_no_permutation_is_privileged_or_adds_a_capability(
    distro: str,
    stable: tuple[str, ...],
    variable: bool,
    uid: int,
) -> None:
    """An exhaustive parametrize, not a property-based test. Every case is listed by pytest.

    It covers every non-Kali distribution, every subset of the stable sources, the variable
    sources present or absent, and two uids. Each case must render the Basic, services-off argv.
    """
    present = (*stable, *(VARIABLE_PATHS if variable else ()))
    argv = create_argv(make_spec(distro, present=present, uid=uid))
    assert_basic_argv(argv)
    assert option_values(argv, "--user") == [f"{uid}:{GID}"]


@pytest.mark.fr("FR-082")
def test_services_off_runs_catatonit_and_a_keep_alive_command_and_no_systemd() -> None:
    argv = create_argv(make_spec())
    assert argv.count("--init") == 1
    assert option_values(argv, "--entrypoint") == ['["sleep"]']
    assert argv[-1] == "infinity"
    assert not any(arg.startswith("--systemd") for arg in argv)
    assert "--cgroupns" not in argv


@pytest.mark.fr("FR-010")
def test_container_name_and_labels_identify_the_realm_and_its_generation() -> None:
    spec = make_spec(generation=2)
    assert spec.container_name == "ginn-arch-g2"
    assert spec.labels == {
        "io.ginn.realm": "arch",
        "io.ginn.distro": "arch",
        "io.ginn.generation": "2",
    }
    argv = create_argv(spec)
    assert option_values(argv, "--name") == ["ginn-arch-g2"]
    assert "io.ginn.realm=arch" in option_values(argv, "--label")


@pytest.mark.fr("FR-010")
def test_create_spec_is_the_adapter_spec_that_renders_the_same_argv() -> None:
    spec = make_spec()
    rendered = create_spec(spec)
    assert isinstance(rendered, CreateSpec)
    assert rendered.to_argv() == create_argv(spec)
    assert rendered.mounts == list(spec.binds.mounts)
    assert rendered.command == ["infinity"]


KALI_NAME = "kali"


def kali_golden(golden: list[str], image: str) -> list[str]:
    """The Kali argv from a non-Kali golden argv. The realm name `arch` becomes `kali` wherever it
    is a word (the container name, the label, the Realm Home path and the cgroup), and the image is
    Kali's pin. Then `--network pasta` replaces `--network host`, and one `--cap-add NET_RAW` goes
    where the argv builder puts capabilities, before the entrypoint or the image (adapter
    `CreateSpec.to_argv`). Nothing else differs (FR-026, FR-027)."""
    argv = [re.sub(r"\barch\b", KALI_NAME, arg) for arg in golden]
    argv[next(index for index, arg in enumerate(argv) if "@sha256:" in arg)] = image
    argv[argv.index("--network") + 1] = "pasta"
    digest = argv.index(image)
    before = argv.index("--entrypoint") if "--entrypoint" in argv else digest
    return [*argv[:before], "--cap-add", "NET_RAW", *argv[before:]]


@pytest.mark.fr("FR-026")
@pytest.mark.fr("FR-027")
def test_kali_services_off_golden_argv_is_pasta_and_net_raw_only() -> None:
    image = pull_reference("kali")
    argv = create_argv(make_spec("kali", name=KALI_NAME, image=image))
    assert argv == kali_golden(GOLDEN_ARCH, image)
    assert option_values(argv, "--cap-add") == ["NET_RAW"]
    assert option_values(argv, "--network") == ["pasta"]
    assert "--privileged" not in argv


@pytest.mark.fr("FR-026")
@pytest.mark.fr("FR-027")
@pytest.mark.parametrize("distro", NON_KALI)
def test_only_kali_gets_pasta_and_net_raw(distro: str) -> None:
    argv = create_argv(make_spec(distro))
    assert option_values(argv, "--network") == ["host"]
    assert option_values(argv, "--cap-add") == []


def assert_services_on_argv(argv: list[str], *, distro: str = "arch", name: str = "arch") -> None:
    """The invariants every services-on argv holds, whatever its distribution (ADR-003, FR-080).

    Kali is the one distribution with pasta and NET_RAW (FR-026, FR-027), so only it differs here.
    """
    assert all(isinstance(arg, str) for arg in argv)
    assert argv[0] == "create"
    assert not any(arg == "--privileged" or arg.startswith("--privileged=") for arg in argv)
    kali = distro == "kali"
    assert option_values(argv, "--cap-add") == (["NET_RAW"] if kali else [])
    assert "--security-opt" not in argv
    assert "--init" not in argv
    assert "--init-path" not in argv
    assert option_values(argv, "--entrypoint") == ['["/sbin/init"]']
    assert not any(arg.startswith("--cgroups") for arg in argv)
    assert "--systemd=always" in argv
    assert option_values(argv, "--user") == ["0:0"]
    assert option_values(argv, "--userns") == ["keep-id"]
    assert option_values(argv, "--network") == (["pasta"] if kali else ["host"])
    assert "--http-proxy=false" in argv
    assert option_values(argv, "--cgroupns") == ["private"]
    assert option_values(argv, "--cgroup-parent") == [f"/ginn/{UID}/{name}"]
    assert option_values(argv, "--stop-signal") == ["SIGRTMIN+3"]
    assert option_values(argv, "--stop-timeout") == ["30"]
    assert option_values(argv, "--env") == ["SYSTEMD_LOG_LEVEL=warning"]
    entry = argv[argv.index("--passwd-entry") + 1]
    assert entry.endswith(f":alice:/home/alice:{load_distro(distro).shell}")
    assert argv[-1] == (pull_reference(distro) if distro == "kali" else DIGEST)
    assert argv[-1] != "/sbin/init"
    assert argv.count("/sbin/init") == 0


@pytest.mark.fr("FR-080")
def test_services_on_golden_argv_for_arch() -> None:
    """The ADR-003 flag set on the S-01 crun row, after the realm's own options, command last."""
    assert create_argv(make_spec(services=True)) == GOLDEN_ARCH_SERVICES_ON


@pytest.mark.fr("FR-080")
@pytest.mark.fr("NFR-SEC-001")
@pytest.mark.parametrize("distro", NON_KALI)
def test_services_on_argv_for_every_non_kali_distro(distro: str) -> None:
    argv = create_argv(make_spec(distro, services=True))
    assert_services_on_argv(argv, distro=distro)
    golden_flags = [arg for arg in GOLDEN_ARCH_SERVICES_ON if arg.startswith("--")]
    assert [arg for arg in argv if arg.startswith("--")] == golden_flags


@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-083")
def test_the_services_on_runtime_tmpfs_stays_u_and_the_drop_in_names_the_realm_user() -> None:
    """T7.28, T716-1: `U` keeps the runtime tmpfs argv (podman refuses `uid=`), so the realm
    user's owner comes from the tmpfiles.d drop-in that create copies in before the first start."""
    argv = create_argv(make_spec(services=True, uid=1001, gid=1002))
    assert option_values(argv, "--tmpfs") == ["/run/user/1001:mode=0700,U"]
    assert argv[argv.index("--user") + 1] == "0:0"
    assert run_user_tmpfiles(1001, 1002) == "d /run/user/1001 0700 1001 1002 -\n"


@pytest.mark.fr("FR-080")
def test_the_services_on_cgroup_parent_is_under_the_host_users_ginn_subtree() -> None:
    argv = create_argv(make_spec(services=True, uid=1001, gid=1001))
    assert option_values(argv, "--cgroup-parent") == ["/ginn/1001/arch"]
    assert services_cgroup_parent(1001, RealmName("arch")) == "/ginn/1001/arch"


@pytest.mark.fr("FR-080")
def test_the_services_on_runtime_is_the_podman_cli_global_flag_not_a_create_option() -> None:
    """`--runtime crun` is passed before every subcommand by PodmanCli (test_podman_cli.py)."""
    assert "--runtime" not in create_argv(make_spec(services=True))


@pytest.mark.fr("FR-082")
@pytest.mark.fr("FR-080")
def test_services_off_has_catatonit_and_no_systemd_and_services_on_is_the_reverse() -> None:
    off = create_argv(make_spec())
    on = create_argv(make_spec(services=True))
    assert "--init" in off
    assert not any(arg.startswith("--systemd") for arg in off)
    assert "--init" not in on
    assert "--systemd=always" in on


@pytest.mark.fr("FR-026")
@pytest.mark.fr("FR-027")
@pytest.mark.fr("FR-080")
def test_kali_services_on_golden_argv_keeps_pasta_and_net_raw() -> None:
    image = pull_reference("kali")
    argv = create_argv(make_spec("kali", name=KALI_NAME, image=image, services=True))
    assert argv == kali_golden(GOLDEN_ARCH_SERVICES_ON, image)
    assert option_values(argv, "--cap-add") == ["NET_RAW"]
    assert option_values(argv, "--network") == ["pasta"]
    assert_services_on_argv(argv, distro="kali", name=KALI_NAME)


@pytest.mark.fr("NFR-SEC-001")
def test_host_uid_zero_is_refused_but_gid_zero_is_not() -> None:
    with pytest.raises(ValueError, match="CR-003"):
        make_spec(uid=0)
    spec = make_spec(gid=0)
    assert option_values(create_argv(spec), "--user") == [f"{UID}:0"]


@pytest.mark.fr("NFR-SEC-001")
def test_negative_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        make_spec(uid=-1)
    with pytest.raises(ValueError, match="must not be negative"):
        make_spec(gid=-1)


@pytest.mark.fr("FR-010")
def test_the_spec_refuses_binds_that_did_not_come_from_create_binds() -> None:
    with pytest.raises(TypeError, match="binds"):
        RealmSpec(
            name=RealmName("arch"),
            distro=DistroId("arch"),
            image=DIGEST,
            uid=UID,
            gid=GID,
            user=USER,
            login_shell="/bin/bash",
            home=HOME,
            binds=Skipped("share", FONTS, "missing"),  # type: ignore[arg-type]
        )


@pytest.mark.fr("FR-048")
def test_the_passwd_entry_names_the_host_user_its_ids_home_and_shell() -> None:
    """The entry has the form `<user>:*:<uid>:<gid>:<user>:<home>:<shell>` (T4.9b)."""
    entry = make_spec().passwd_entry
    assert entry == "alice:*:1000:1000:alice:/home/alice:/bin/bash"


@pytest.mark.fr("FR-048")
@pytest.mark.parametrize("distro", NON_KALI)
def test_the_passwd_entry_shell_is_the_one_enter_runs(distro: str) -> None:
    """`enter` runs the distribution's login shell (FR-048). The entry names the same shell."""
    assert make_spec(distro).passwd_entry.split(":")[6] == login_argv(load_distro(distro).shell)[0]


@pytest.mark.fr("FR-048")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("user", "a:b"),
        ("user", "a$b"),
        ("user", ""),
        ("login_shell", "/bin/sh\n"),
        ("home", "/home/a:b"),
        ("home", "/home/a$USERNAMEb"),
        ("home", "/home/a\nb"),
    ],
)
def test_a_passwd_field_podman_would_rewrite_is_refused(field: str, bad: str) -> None:
    values = {"user": USER, "login_shell": "/bin/bash", "home": str(HOME)}
    values[field] = bad
    with pytest.raises(ValueError, match="passwd field"):
        realm_passwd_entry(values["user"], UID, GID, Path(values["home"]), values["login_shell"])


def _custom_realm_spec(
    toggles: frozenset[Toggle],
    *,
    present: Iterable[str] = STABLE_PATHS,
) -> RealmSpec:
    """A Custom realm's spec from realm_spec, with `toggles` as its Custom toggles (FR-063)."""
    return realm_spec(
        RealmName("arch"),
        DistroId("arch"),
        uid=UID,
        gid=GID,
        user=USER,
        login_shell="/bin/bash",
        host_home=HOME,
        realm_home=REALM_HOME,
        owned_paths=[OWNED],
        env={},
        exists=_present(present),
        tier=Tier.CUSTOM,
        permissions=Permissions(toggles=toggles),
    )


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_custom_realm_with_the_gpu_toggle_on_gets_the_dri_device_and_off_gets_none() -> None:
    on = _pairs(create_argv(_custom_realm_spec(BASIC_TOGGLES)))
    off = _pairs(create_argv(_custom_realm_spec(BASIC_TOGGLES - {Toggle.GPU})))
    assert ("--device", "/dev/dri") in on
    assert ("--device", "/dev/dri") not in off


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_custom_realm_with_the_fonts_timezone_toggle_off_has_no_fonts_or_localtime_bind() -> None:
    on = mount_targets(create_argv(_custom_realm_spec(BASIC_TOGGLES)))
    off = mount_targets(create_argv(_custom_realm_spec(BASIC_TOGGLES - {Toggle.FONTS_TIMEZONE})))
    assert {FONTS, LOCALTIME} <= set(on)
    assert not {FONTS, LOCALTIME} & set(off)


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_gpu_toggle_that_is_off_plans_no_device_and_no_missing_device_skip() -> None:
    """A switched-off grant is not planned, so a host without /dev/dri reports nothing for it."""
    binds = create_binds(
        Tier.CUSTOM,
        host_home=HOME,
        uid=UID,
        realm_home=REALM_HOME,
        owned_paths=[OWNED],
        env={},
        exists=_present([FONTS, LOCALTIME]),
        toggles=BASIC_TOGGLES - {Toggle.GPU},
    )
    assert binds.devices == []
    assert not [skip for skip in binds.skipped if skip.kind == "device"]
    on = create_binds(
        Tier.CUSTOM,
        host_home=HOME,
        uid=UID,
        realm_home=REALM_HOME,
        owned_paths=[OWNED],
        env={},
        exists=_present([FONTS, LOCALTIME]),
        toggles=BASIC_TOGGLES,
    )
    assert on.devices == []
    assert [skip.kind for skip in on.skipped if skip.kind == "device"] == ["device"]
