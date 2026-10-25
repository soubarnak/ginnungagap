# SPDX-License-Identifier: Apache-2.0
"""Each tier's podman options equal the enumerated set of docs/capabilities.md (NFR-SEC-002).

Covers security/caps.py, the create argv of runtime/spec.py and the attach plan of
runtime/mounts.py. The test reads the repository's document, so a change to the code or to the
document fails here until both agree. Nothing here runs podman or reads the host: the host probes
are fakes, and the seccomp profile is a path that is never opened.
"""

import re
from pathlib import Path

import pytest

from ginn.distros import load as load_distro
from ginn.domain.enums import DistroId, Tier
from ginn.domain.names import RealmName
from ginn.domain.permissions import (
    ALL_TOGGLES,
    BASIC_TOGGLES,
    CustomShare,
    Permissions,
    Toggle,
    effective_toggles,
)
from ginn.errors import GinnError
from ginn.podman.adapter import CreateSpec, Mount
from ginn.podman.cli import PodmanCli
from ginn.runtime.env import session_env
from ginn.runtime.mounts import attach_plan, create_binds
from ginn.runtime.spec import RealmSpec, create_argv, create_spec
from ginn.security.caps import (
    KEEP_GROUPS,
    KVM_DEVICE,
    PLACEHOLDER,
    CdiGrant,
    TierExtras,
    create_options,
    option_keys,
)

REPO = Path(__file__).resolve().parents[2]
DOCUMENT = REPO / "docs" / "capabilities.md"
HOME = Path("/home/alice")
USER = "alice"
UID = 1000
GID = 1000
OWNED = Path("/home/alice/.local/share/ginn")
REALM_HOME = OWNED / "homes" / "arch"
DIGEST = "docker.io/library/archlinux@sha256:" + "ab" * 32
STABLE = ("/usr/share/fonts", "/etc/localtime", "/dev/dri")
PROFILE = OWNED / "data" / "seccomp" / ("a" * 64 + ".json")
FLAGS = ("--device", "--security-opt", "--group-add", "--cap-add")
TOKEN = re.compile(r"`(--[a-z-]+) ([^`]+)`")


def _document() -> str:
    return DOCUMENT.read_text(encoding="utf-8")


def _section(title: str) -> str:
    """The text of one `## ` section of the document, up to the next `## ` heading."""
    text = _document()
    start = text.index(f"## {title}")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _table(section: str, heading: str) -> list[list[str]]:
    """The body rows of the first table under `### heading`, each row split into its cells."""
    start = section.index(f"### {heading}")
    rows: list[list[str]] = []
    for line in section[start:].splitlines()[1:]:
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if set(cells[0]) <= {"-", ":"} or cells[0] == "Item" or cells[0] == "Toggle":
            continue
        rows.append(cells)
    return rows


def _pairs(argv: list[str]) -> list[tuple[str, str]]:
    """The (flag, value) pairs of the option flags in an argv, in order."""
    return [(token, argv[i + 1]) for i, token in enumerate(argv[:-1]) if token in FLAGS]


def _normal(pair: tuple[str, str]) -> tuple[str, str]:
    """A pair with the generated profile path written as the document's placeholder, exactly."""
    flag, value = pair
    if value == f"seccomp={PROFILE}":
        return flag, f"seccomp={PLACEHOLDER}"
    return pair


def _host_present(path: str) -> bool:
    return path in STABLE


def _spec(
    tier: Tier,
    *,
    toggles: frozenset[Toggle] = BASIC_TOGGLES,
    kvm: bool = True,
    extras: TierExtras | None = None,
) -> RealmSpec:
    """A spec built the way realm_spec builds one, with the probes and the profile given."""
    binds = create_binds(
        Tier.BASIC,
        host_home=HOME,
        uid=UID,
        realm_home=REALM_HOME,
        owned_paths=[OWNED],
        env={},
        exists=_host_present,
    )
    options = create_options(
        effective_toggles(tier, toggles),
        kvm_present=kvm,
        seccomp_profile=PROFILE,
        extras=extras,
        home=HOME,
        owned_paths=[OWNED],
        env={},
    )
    return RealmSpec(
        name=RealmName("arch"),
        distro=DistroId("arch"),
        image=DIGEST,
        uid=UID,
        gid=GID,
        user=USER,
        login_shell=load_distro("arch").shell,
        home=HOME,
        binds=binds,
        tier=tier,
        options=options,
    )


def _grants(
    tier: Tier,
    permissions: Permissions | None = None,
    present: frozenset[str] = frozenset(),
) -> set[str]:
    """The realm paths of the attach plan, with the given host paths present (mount roots too)."""
    names = {str(HOME / "Downloads"), "/run/media/alice", "/media", "/mnt", *present}
    plan = attach_plan(
        tier,
        host_home=HOME,
        uid=UID,
        user=USER,
        owned_paths=[OWNED],
        runtime_root=OWNED / "run",
        session_binds=[],
        env={},
        exists=names.__contains__,
        permissions=permissions,
        is_dir=lambda _path: True,
    )
    return {entry.realm for entry in plan.attach}


@pytest.mark.fr("NFR-SEC-002")
def test_development_adds_exactly_the_document_options() -> None:
    """The options a Development realm adds beyond Basic equal the document's Development table."""
    expected = set()
    for row in _table(_section("Development"), "Outer options"):
        for flag, value in TOKEN.findall(row[1]):
            expected.add((flag, re.sub(r"^seccomp=.*$", f"seccomp={PLACEHOLDER}", value)))
    assert expected == {
        ("--device", "/dev/fuse"),
        ("--device", "/dev/net/tun"),
        ("--device", KVM_DEVICE),
        ("--group-add", KEEP_GROUPS),
        ("--security-opt", "unmask=/proc/*"),
        ("--security-opt", f"seccomp={PLACEHOLDER}"),
    }
    basic = {_normal(pair) for pair in _pairs(create_argv(_spec(Tier.BASIC)))}
    development = {_normal(pair) for pair in _pairs(create_argv(_spec(Tier.DEVELOPMENT)))}
    assert development - basic == expected


@pytest.mark.fr("NFR-SEC-002")
def test_basic_adds_no_option_beyond_the_base_argv() -> None:
    """Basic's options are the document's Basic create-time rows, and the GPU node is one."""
    assert _pairs(create_argv(_spec(Tier.BASIC))) == [("--device", "/dev/dri")]


@pytest.mark.fr("NFR-SEC-002")
@pytest.mark.parametrize("tier", list(Tier))
def test_no_tier_adds_a_capability_or_a_privileged_flag(tier: Tier) -> None:
    argv = create_argv(_spec(tier))
    assert "--privileged" not in argv
    assert "--cap-add" not in argv
    assert not any("unconfined" in token or "no-new-privileges" in token for token in argv)


@pytest.mark.fr("IR-040")
def test_kvm_device_and_its_kept_group_follow_the_host_probe() -> None:
    with_kvm = _pairs(create_argv(_spec(Tier.DEVELOPMENT, kvm=True)))
    without = _pairs(create_argv(_spec(Tier.DEVELOPMENT, kvm=False)))
    assert ("--device", KVM_DEVICE) in with_kvm
    assert ("--group-add", KEEP_GROUPS) in with_kvm
    assert ("--device", KVM_DEVICE) not in without
    assert ("--group-add", KEEP_GROUPS) not in without
    assert ("--device", "/dev/fuse") in without


@pytest.mark.fr("NFR-SEC-002")
def test_the_document_attach_rows_equal_the_development_grants() -> None:
    """Development's attach grants beyond Basic are the rows of the document's attach table."""
    rows = _table(_section("Development"), "Attach rows (start-time)")
    names = {cell for row in rows for cell in re.findall(r"`([^`]+)`", row[0])}
    assert names == {"~/Projects", "~/.bashrc", "~/.zshrc"}
    present = frozenset({str(HOME / "Projects"), str(HOME / ".bashrc"), str(HOME / ".zshrc")})
    extra = _grants(Tier.DEVELOPMENT, present=present) - _grants(Tier.BASIC, present=present)
    assert extra == {str(HOME / "Projects"), str(HOME / ".bashrc"), str(HOME / ".zshrc")}


@pytest.mark.fr("FR-063")
def test_the_document_names_each_custom_toggle_once() -> None:
    rows = _table(_section("Custom (FR-063)"), "Custom toggles")
    assert [row[0].strip("`") for row in rows] == [toggle.value for toggle in Toggle]


@pytest.mark.fr("NFR-SEC-002")
def test_option_keys_list_the_toggle_driven_options() -> None:
    keys = option_keys(ALL_TOGGLES, kvm_present=True)
    assert keys == [
        "device=/dev/fuse",
        "device=/dev/kvm",
        "device=/dev/net/tun",
        "group-add=keep-groups",
        "security-opt=seccomp",
        "security-opt=unmask",
    ]
    assert option_keys(BASIC_TOGGLES, kvm_present=True) == []
    assert option_keys(ALL_TOGGLES, kvm_present=False) == [
        "device=/dev/fuse",
        "device=/dev/net/tun",
        "security-opt=seccomp",
        "security-opt=unmask",
    ]


@pytest.mark.fr("FR-063")
def test_a_custom_realm_with_only_basic_toggles_adds_no_option() -> None:
    options = create_options(BASIC_TOGGLES, kvm_present=True, seccomp_profile=None)
    assert options.devices == () and options.security_opts == () and options.group_add == ()


@pytest.mark.fr("FR-062")
def test_nested_toggle_needs_the_profile_path() -> None:
    with pytest.raises(ValueError, match="seccomp profile"):
        create_options({Toggle.NESTED}, kvm_present=False, seccomp_profile=None)


@pytest.mark.fr("NFR-SEC-002")
@pytest.mark.parametrize(
    "option",
    [
        "seccomp=unconfined",
        "seccomp:unconfined",
        " seccomp=unconfined",
        "seccomp=/tmp/other.json",
        "apparmor=unconfined",
        "label=disable",
        "label:disable",
        "label=type:spc_t",
        "systempaths=unconfined",
        "unmask=ALL",
        "unmask=/sys/*",
        "no-new-privileges=false",
        "no-new-privileges",
        "privileged",
    ],
)
def test_extras_cannot_add_an_option_outside_the_allowlist(option: str) -> None:
    extras = TierExtras(security_opts=(option,))
    with pytest.raises(ValueError, match="NFR-SEC-002"):
        create_options({Toggle.NESTED}, kvm_present=False, seccomp_profile=PROFILE, extras=extras)


@pytest.mark.fr("NFR-SEC-002")
def test_group_add_is_rendered_after_the_security_options() -> None:
    spec = CreateSpec(
        name="ginn-arch-g1",
        image_digest_ref=DIGEST,
        network="host",
        security_opts=["unmask=/proc/*"],
        group_add=["keep-groups"],
    )
    argv = spec.to_argv()
    assert argv.index("--group-add") > argv.index("--security-opt")
    assert argv[argv.index("--group-add") + 1] == "keep-groups"


@pytest.mark.fr("NFR-SEC-005")
def test_group_add_refuses_a_value_that_podman_would_read_as_an_option() -> None:
    spec = CreateSpec(
        name="ginn-arch-g1", image_digest_ref=DIGEST, network="host", group_add=["-x"]
    )
    with pytest.raises(ValueError):
        spec.to_argv()


@pytest.mark.fr("NFR-SEC-002")
def test_the_development_create_spec_carries_the_options_and_the_binds() -> None:
    spec = _spec(Tier.DEVELOPMENT)
    rendered = create_spec(spec)
    assert rendered.devices == ["/dev/dri", "/dev/fuse", "/dev/net/tun", KVM_DEVICE]
    assert rendered.security_opts == ["unmask=/proc/*", f"seccomp={PROFILE}"]
    assert rendered.group_add == [KEEP_GROUPS]
    assert rendered.mounts == list(spec.binds.mounts)


@pytest.mark.fr("FR-063")
def test_custom_toggles_decide_the_attach_grants_and_shares_are_checked() -> None:
    custom = Permissions(
        toggles=frozenset({Toggle.PROJECTS}),
        shares=(CustomShare(str(HOME / "Music"), "ro"),),
    )
    present = frozenset({str(HOME / "Projects"), str(HOME / "Music")})
    realms = _grants(Tier.CUSTOM, custom, present)
    assert str(HOME / "Projects") in realms
    assert str(HOME / "Music") in realms
    assert str(HOME / "Downloads") not in realms
    # A stored share is attached only on the Custom tier, so Basic ignores it.
    assert str(HOME / "Music") not in _grants(Tier.BASIC, custom, present)


@pytest.mark.fr("NFR-SEC-002")
def test_the_exec_argv_carries_no_tier_option_whichever_tier_the_realm_has() -> None:
    """`podman exec` takes no tier: ginn adds nothing for a tier at exec (capabilities.md, session
    environment row). The argv is built from the session environment alone, and it carries none of
    the create-time flags.
    """
    session = session_env({}, home=HOME, uid=UID, owned_paths=[OWNED])
    argv = PodmanCli._exec_args("c" * 64, ["true"], "0:0", session.env, None, False, False)
    for flag in ("--privileged", "--cap-add", "--device", "--security-opt", "--group-add"):
        assert flag not in argv
    assert argv[argv.index("--user") + 1] == "0:0"


@pytest.mark.fr("NFR-SEC-002")
@pytest.mark.parametrize(
    "device",
    [
        "-x",
        "/dev/sda",
        "nvidia.com/gpu=all",
        "/dev/kvm:/dev/x",
        "/dev/dri/../sda",
        "/dev/fuse,ro",
        "/dev/dri/card0 ",
        "/dev/kvmx",
        "/etc/passwd",
    ],
)
def test_extras_devices_outside_the_fixed_prefixes_are_refused(device: str) -> None:
    with pytest.raises(ValueError, match="NFR-SEC-002"):
        create_options(
            {Toggle.NESTED},
            kvm_present=False,
            seccomp_profile=PROFILE,
            extras=TierExtras(devices=(device,)),
        )


@pytest.mark.fr("NFR-SEC-002")
def test_extras_devices_under_the_fixed_prefixes_are_accepted() -> None:
    options = create_options(
        set(),
        kvm_present=False,
        seccomp_profile=None,
        extras=TierExtras(devices=("/dev/dri/renderD200", "/dev/kvm")),
    )
    assert options.devices == ("/dev/dri/renderD200", "/dev/kvm")


@pytest.mark.fr("NFR-SEC-002")
def test_extras_join_a_development_realm_and_never_a_basic_one() -> None:
    extras = TierExtras(devices=("/dev/dri/renderD200",), group_add=("video",))
    spec = _spec(Tier.DEVELOPMENT, kvm=False, extras=extras)
    argv = create_argv(spec)
    assert ("--device", "/dev/dri/renderD200") in _pairs(argv)
    assert ("--group-add", "video") in _pairs(argv)
    assert "device=/dev/dri/renderD200" not in option_keys(ALL_TOGGLES, kvm_present=True)
    with pytest.raises(ValueError, match="Basic realm adds no create-time option"):
        _spec(Tier.BASIC, extras=extras)


@pytest.mark.fr("NFR-SEC-002")
def test_an_extra_group_may_not_sit_beside_keep_groups() -> None:
    with pytest.raises(ValueError, match="beside keep-groups"):
        create_options(
            ALL_TOGGLES,
            kvm_present=True,
            seccomp_profile=PROFILE,
            extras=TierExtras(group_add=("video",)),
        )
    with pytest.raises(ValueError, match="NFR-SEC-002"):
        create_options(
            set(),
            kvm_present=False,
            seccomp_profile=None,
            extras=TierExtras(group_add=(KEEP_GROUPS,)),
        )
    options = create_options(
        ALL_TOGGLES,
        kvm_present=False,
        seccomp_profile=PROFILE,
        extras=TierExtras(group_add=("video",)),
    )
    assert options.group_add == ("video",)


@pytest.mark.fr("FR-069")
@pytest.mark.fr("NFR-SEC-005")
def test_a_cdi_grant_renders_its_device_and_its_global_spec_directory() -> None:
    grant = CdiGrant(OWNED / "data" / "cdi", "nvidia.com/gpu=all")
    spec = _spec(Tier.BASIC, extras=TierExtras(cdi=grant))
    rendered = create_spec(spec)
    assert rendered.cdi_devices == ["nvidia.com/gpu=all"]
    assert rendered.cdi_spec_dir == str(OWNED / "data" / "cdi")
    assert rendered.global_flags() == ["--cdi-spec-dir", str(OWNED / "data" / "cdi")]
    assert ("--device", "nvidia.com/gpu=all") in _pairs(rendered.to_argv())
    assert "--cdi-spec-dir" not in rendered.to_argv()


@pytest.mark.fr("DR-010")
def test_a_cdi_spec_directory_outside_owned_paths_is_refused() -> None:
    grant = CdiGrant(Path("/usr/share/cdi"), "nvidia.com/gpu=all")
    with pytest.raises(ValueError, match="DR-010"):
        create_options(
            BASIC_TOGGLES,
            kvm_present=False,
            seccomp_profile=None,
            extras=TierExtras(cdi=grant),
            owned_paths=[OWNED],
        )


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "device",
    ["nvidia.com/gpu", "gpu=all", "nvidia.com/gpu=all,ro", "nvidia.com/gpu=a:b", "/dev/dri/card0"],
)
def test_a_cdi_device_must_be_a_vendor_class_and_name(device: str) -> None:
    with pytest.raises(ValueError, match="NFR-SEC-005"):
        CdiGrant(OWNED / "data" / "cdi", device)


@pytest.mark.fr("NFR-SEC-005")
def test_a_cdi_spec_directory_with_a_comma_is_refused() -> None:
    with pytest.raises(ValueError, match="NFR-SEC-005"):
        CdiGrant(Path("/home/alice/a,b/cdi"), "nvidia.com/gpu=all")


@pytest.mark.fr("DR-004")
def test_an_extra_mount_passes_the_dr004_rules_at_its_own_path() -> None:
    mount = Mount("bind", str(HOME / "Projects"), str(HOME / "Projects"), ())
    options = create_options(
        set(),
        kvm_present=False,
        seccomp_profile=None,
        extras=TierExtras(mounts=(mount,)),
        home=HOME,
        owned_paths=[OWNED],
        env={},
    )
    assert [m.source for m in options.mounts] == [str(HOME / "Projects")]


@pytest.mark.fr("DR-004")
@pytest.mark.parametrize("source", ["/etc/ssh", str(HOME / ".ssh"), str(OWNED / "realms")])
def test_an_extra_mount_that_dr004_forbids_is_refused(source: str) -> None:
    mount = Mount("bind", source, source, ())
    with pytest.raises(GinnError) as caught:
        create_options(
            set(),
            kvm_present=False,
            seccomp_profile=None,
            extras=TierExtras(mounts=(mount,)),
            home=HOME,
            owned_paths=[OWNED],
            env={},
        )
    assert caught.value.code == "GINN-E-PATH-FORBIDDEN"


@pytest.mark.fr("FR-065")
def test_an_extra_mount_at_another_path_is_refused() -> None:
    mount = Mount("bind", str(HOME / "Projects"), str(HOME / "elsewhere"), ())
    with pytest.raises(ValueError, match="FR-065"):
        create_options(
            set(),
            kvm_present=False,
            seccomp_profile=None,
            extras=TierExtras(mounts=(mount,)),
            home=HOME,
            owned_paths=[OWNED],
            env={},
        )


def _option_key(flag: str, value: str) -> str:
    """The canonical name of one `--flag value` pair, as option_keys writes it."""
    if flag == "--device":
        return f"device={value}"
    if flag == "--group-add":
        return f"group-add={value}"
    assert flag == "--security-opt", flag
    return f"security-opt={value.partition('=')[0]}"


@pytest.mark.fr("NFR-SEC-002")
def test_each_custom_toggle_adds_at_create_exactly_what_the_document_names() -> None:
    """The "Adds at create" column of the Custom toggles table, per toggle, equals option_keys."""
    for row in _table(_section("Custom (FR-063)"), "Custom toggles"):
        toggle = Toggle(row[0].strip("`"))
        named = sorted(_option_key(flag, value) for flag, value in TOKEN.findall(row[2]))
        assert named == option_keys({toggle}, kvm_present=True), toggle.value
