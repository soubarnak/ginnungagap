# SPDX-License-Identifier: Apache-2.0
"""Realm spec and its `podman create` argv, services off (FR-010, FR-061, FR-062, FR-082) or on
(FR-080, ADR-003).

`RealmSpec` is the data for one realm's container: the realm name, the distribution, the image
pinned by digest in the Trust Manifest (FR-011), the host identity, the stable create-time binds
that `mounts.create_binds` plans (ADR-008 amendment), and the tier's create-time options from
`security/caps.py` (NFR-SEC-002). The options are empty for Basic. Development adds the nested
containers and `/dev/kvm` set of ADR-006. `create_spec` renders it as the adapter's `CreateSpec`,
and `create_argv` renders that as the `podman create` argv. Both are pure: they run no podman and
write nothing.

The argv is the subcommand only. `PodmanCli` adds the global flags to every call: --remote=false,
--root, --runroot, --tmpdir and --runtime crun (ADR-004, IR-020).

What the argv carries, and the decision behind each item (docs/capabilities.md, Base argv and
Basic table, unless a row says otherwise):
- `--name ginn-<realm>-g<generation>`. A services toggle makes a new generation (ADR-003, FR-083).
- `--user <uid>:<gid>` with `--userns keep-id`. The ids are the host user's (roadmap, design).
- `--passwd-entry <user>:*:<uid>:<gid>:<user>:<home>:<shell>`. Under keep-id, podman writes this
  entry for the realm user, verbatim (T4.9b measurement). The realm user is that entry: ginn runs
  no `useradd`, and ops/create.py checks the entry with `getent passwd` (ADR-006 amendment of
  2026-10-13). The shell is the distribution's login shell, the one `enter` runs (FR-048).
- `--network host`. Non-Kali realms share the host network namespace (AS-006). A Kali realm has
  `--network pasta` in its own namespace instead, and `--cap-add NET_RAW`, the only capability
  added beyond Basic (FR-026, FR-027, docs/capabilities.md Kali table). `network_of` and
  `cap_add_of` give these by distribution, so no other distribution can receive them.
- `--http-proxy=false`. Podman does not copy the host's proxy variables into the realm. The proxy
  names reach an exec or a launch as `--env NAME` (ADR-004 amendment, T4.6).
- `--init`, with no `--init-path`. Services-off realms run catatonit as PID 1 (FR-082, ADR-004).
  `/usr/bin/catatonit` is absent on the reference host, so the path is left to podman's lookup.
  A services-on realm has no `--init`: systemd is its init (ADR-003).
- `--label io.ginn.realm`, `io.ginn.distro` and `io.ginn.generation`. ginn touches only containers
  that carry these labels, so the callers (T4.4, T4.5) filter with `ps` on these keys. The keys
  are fixed in roadmap.md and capabilities.md (Base argv).
- `--entrypoint ["sleep"]`, with the command `infinity` after the image. This is the keep-alive of
  the S-02 spike (ADR-010). Without it the image's shell exits at once with no terminal, and
  `podman start` would leave the realm stopped. Both are given explicitly, so the image's CMD is
  not used, whichever way podman treats it. The T4.9 host gate confirms the running process.
  A services-on realm has no entrypoint. Its command is `/sbin/init`, after the image.
- The services-on options of ADR-003, added to the same argv when `services` is set:
  `--systemd=always`, `--cgroupns private`, `--cgroup-parent /ginn/<uid>/<realm>`, `--user 0:0`,
  `--stop-signal SIGRTMIN+3`, `--stop-timeout 30` and `--env SYSTEMD_LOG_LEVEL=warning` (ADR-007
  mitigation d). The realm's own options stay. The adapter sorts them in its fixed order. The
  runtime is not repeated here: `PodmanCli` passes `--runtime crun` before every subcommand.
- The Realm Home at the host home path, the fonts and the localtime read-only, `--device /dev/dri`
  and `--tmpfs /run/user/<uid>:mode=0700,U`. These are the stable binds (FR-048, FR-061, AS-009,
  ADR-012, ADR-008 amendment). The fonts and localtime need the `fonts_timezone` toggle and the
  device needs `gpu` (FR-063), so a Basic realm has all of them. The GPU device grant of ADR-012
  is `/dev/dri`.

What the argv does not carry:
- No `--privileged`. Basic adds no capability (NFR-SEC-001, NFR-SEC-002). The one `--cap-add` is
  Kali's NET_RAW, above.
- No `--cgroups=split`. Podman 5.8.8 refuses it with `--cgroup-parent` (ADR-003).
- No `--env`. The session environment is set at exec and launch, not at create (capabilities.md,
  Session environment row, T4.3).
- No variable grant. Tier folders, mount roots and session paths are attached after `podman start`
  (ADR-008 amendment, T4.10), so a missing source never blocks a start (FR-064).
- No NVIDIA CDI spec yet. CDI is applied at create time per ADR-008; the generator arrives in
  T6.6 (FR-069). Realms created before M6 need a new generation to get NVIDIA.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from ginn.domain.enums import DistroId, Tier
from ginn.domain.names import RealmName
from ginn.domain.permissions import Permissions, effective_toggles
from ginn.podman.adapter import CreateSpec, Mount, require_digest_ref
from ginn.runtime.mounts import CreateBinds, Exists, create_binds, runtime_dir
from ginn.security.caps import CreateOptions, TierExtras, create_options
from ginn.trust.manifest import TrustManifest, pull_reference

LABEL_REALM: Final = "io.ginn.realm"
LABEL_DISTRO: Final = "io.ginn.distro"
LABEL_GENERATION: Final = "io.ginn.generation"
LABEL_ESTIMATE: Final = "io.ginn.estimate"
"""The label of a Kali everything estimate's throwaway container, valued with the distribution ID
(ops/estimate.py). It is not a realm label, so no realm lookup (`LABEL_REALM`) finds it."""
NAME_PREFIX: Final = "ginn-"
KEEP_ALIVE_ENTRYPOINT: Final[tuple[str, ...]] = ("sleep",)
KEEP_ALIVE_COMMAND: Final[tuple[str, ...]] = ("infinity",)
"""The services-off realm's keep-alive, `sleep infinity`, under `--init`. Entrypoint and command
are both given, so the container keeps running whatever podman does with the image's CMD."""
SERVICES_COMMAND: Final[tuple[str, ...]] = ("/sbin/init",)
"""The services-on realm's command: systemd as PID 1 (ADR-003). It follows the image."""
SERVICES_STOP_SIGNAL: Final = "SIGRTMIN+3"
"""The services-on stop signal, which systemd treats as poweroff (ADR-003)."""
SERVICES_STOP_TIMEOUT_S: Final = 30
"""Seconds podman waits for a services-on realm to stop before it kills it (ADR-003)."""
SERVICES_ENV: Final[tuple[tuple[str, str], ...]] = (("SYSTEMD_LOG_LEVEL", "warning"),)
"""The services-on environment: systemd logs warnings only (ADR-007 mitigation d)."""
SERVICES_MITIGATIONS: Final = True
"""Whether a services-on create ships ADR-007 mitigations a and b (journald drop-in, unit masks).
On since spike S-05s passed (CL-23, 2026-10-14; ADR-007 Decision). Mitigation d is always on."""
JOURNALD_DROPIN_TARGET: Final = "/etc/systemd/journald.conf.d/ginn.conf"
"""Where the ginn-owned journald drop-in is bind-mounted read-only (ADR-007 a)."""
JOURNALD_DROPIN_TEXT: Final = "[Journal]\nStorage=volatile\nRuntimeMaxUse=8M\n"
"""The drop-in: the journal stays in RAM and is capped at 8 MiB (ADR-007 a)."""
SERVICES_MASKED_UNITS: Final[tuple[str, ...]] = (
    "systemd-udevd.service",
    "systemd-udevd-control.socket",
    "systemd-udevd-kernel.socket",
    "systemd-udev-trigger.service",
    "getty@tty1.service",
    "console-getty.service",
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
    "e2scrub_all.timer",
    "fstrim.timer",
)
"""The ten units that ADR-007 mitigation b masks: each gets a read-only `/dev/null` bind over its
unit path under /etc/systemd/system. The list is spikes/s05/masks-base.txt (tests pin it)."""
UNIT_DIR: Final = "/etc/systemd/system"
KALI: Final = "kali"
HOST_NETWORK: Final = "host"
KALI_NETWORK: Final = "pasta"
"""Kali's own network namespace, with outbound access (FR-026). Podman runs pasta for it."""
KALI_CAPS: Final[tuple[str, ...]] = ("NET_RAW",)
"""Kali's capability, raw sockets inside its own namespace only (FR-027). NET_ADMIN is not added:
the stock nmap does not need it (docs/capabilities.md)."""


def network_of(distro: DistroId) -> str:
    """The podman `--network` value of a distribution: pasta for Kali, host for the others."""
    return KALI_NETWORK if distro.value == KALI else HOST_NETWORK


def cap_add_of(distro: DistroId) -> tuple[str, ...]:
    """The capabilities a distribution's realm adds with `--cap-add`: NET_RAW for Kali only."""
    return KALI_CAPS if distro.value == KALI else ()


@dataclass(frozen=True)
class RealmSpec:
    """One realm's container, Basic tier, services off or on (ADR-003). Valid once constructed.

    `image` is `repository@sha256:<hex>`, never a tag (FR-011). `home` is the host home path,
    which the Realm Home is mounted at (FR-048). `binds` holds only the stable create-time binds,
    and it must be the set that `mounts.create_binds` plans for this `home` and `uid`.

    `uid` and `gid` are the HOST user's ids. AS-007 maps the realm root to that host user. The
    `--user` argv value is the realm-side user, which equals the host ids under keep-id. A
    services-on realm later runs as `--user 0:0` (ADR-003), so the two can differ. Only host
    uid 0 is refused (CR-003, OOS-3, OOS-10).

    `user` and `login_shell` make the realm user's passwd entry (`passwd_entry`). `login_shell`
    is the distribution's login shell (distros/data), which `enter` runs too (FR-048).
    """

    name: RealmName
    distro: DistroId
    image: str
    uid: int
    gid: int
    user: str
    login_shell: str
    home: Path
    binds: CreateBinds
    tier: Tier = Tier.BASIC
    services: bool = False
    """Services on: systemd is the realm's init, and only its runit entry starts it (ADR-003)."""
    conmon_pidfile: str | None = None
    """Services on only: the conmon pidfile under ginn's runtime root (FR-081, ginn-wait)."""
    services_mitigations: bool = False
    """Services on only: create the ADR-007 mitigations a and b. Off by default (S-05s)."""
    journald_dropin: str | None = None
    """Host path of the ginn-owned journald drop-in, bound when `services_mitigations` is set."""
    generation: int = 1
    options: CreateOptions = field(default_factory=CreateOptions)
    """The tier's create-time options, beyond the Basic binds (security/caps.py, ADR-006)."""

    def __post_init__(self) -> None:
        if not isinstance(self.name, RealmName):
            raise TypeError("name must be a RealmName")
        if not isinstance(self.distro, DistroId):
            raise TypeError("distro must be a DistroId")
        if not isinstance(self.binds, CreateBinds):
            raise TypeError("binds must be CreateBinds from mounts.create_binds")
        require_digest_ref(self.image)
        # The passwd entry checks its fields (no ":", "$" or newline), so a bad one fails here.
        realm_passwd_entry(self.user, self.uid, self.gid, self.home, self.login_shell)
        if self.tier is Tier.BASIC and replace(self.options, cdi=None) != CreateOptions():
            raise ValueError(
                "a Basic realm adds no create-time option but its GPU grant (NFR-SEC-002)"
            )
        if self.uid == 0:
            raise ValueError("CR-003, OOS-3: a realm never runs as the host root user, uid 0")
        if self.uid < 0 or self.gid < 0:
            raise ValueError(f"uid and gid must not be negative, not {self.uid}:{self.gid}")
        if not self.home.is_absolute():
            raise ValueError(f"host home must be absolute, not {self.home}")
        if not self.binds.mounts or self.binds.mounts[0].target != str(self.home):
            raise ValueError(f"FR-048: the first bind must be the Realm Home at {self.home}")
        if self.binds.tmpfs != [f"{runtime_dir(self.uid)}:mode=0700,U"]:
            raise ValueError(f"the runtime tmpfs must be {runtime_dir(self.uid)} (ADR-012)")
        if self.generation < 1:
            raise ValueError(f"generation must be at least 1, not {self.generation}")
        if not self.services and (
            self.conmon_pidfile is not None or self.services_mitigations or self.journald_dropin
        ):
            raise ValueError("the services-on fields are set on a services-off realm")
        if self.services_mitigations and self.journald_dropin is None:
            raise ValueError("ADR-007 mitigation a needs the journald drop-in path")

    @property
    def container_name(self) -> str:
        """The podman container name, `ginn-<realm>-g<generation>`."""
        return f"{NAME_PREFIX}{self.name}-g{self.generation}"

    @property
    def labels(self) -> dict[str, str]:
        """The labels that mark this container as ginn's realm (see the module docstring)."""
        return {
            LABEL_REALM: str(self.name),
            LABEL_DISTRO: self.distro.value,
            LABEL_GENERATION: str(self.generation),
        }

    @property
    def passwd_entry(self) -> str:
        """The realm user's passwd entry: what `--passwd-entry` sends and the user check expects."""
        return realm_passwd_entry(self.user, self.uid, self.gid, self.home, self.login_shell)


def realm_passwd_entry(user: str, uid: int, gid: int, home: Path, login_shell: str) -> str:
    """The realm user's passwd entry, `<user>:*:<uid>:<gid>:<user>:<home>:<shell>` (T4.9b).

    The `*` password is the locked form, which podman also writes. Podman writes the entry as
    given under keep-id. A field that holds `:` adds a field, a newline adds a line, and `$` starts
    a placeholder that podman expands (measured: `$USERNAME` in a path). Nothing is escaped, so such
    a field, and an empty one, raises ValueError.
    """
    values = (user, str(uid), str(gid), str(home), login_shell)
    for value in values:
        if value == "" or any(ch in value for ch in ":\n$"):
            raise ValueError(
                f"a passwd field must not be empty or hold ':', '$' or a newline: {value!r}"
            )
    return f"{user}:*:{uid}:{gid}:{user}:{home}:{login_shell}"


def realm_spec(
    name: RealmName,
    distro: DistroId,
    *,
    uid: int,
    gid: int,
    user: str,
    login_shell: str,
    host_home: Path,
    realm_home: Path,
    owned_paths: Sequence[Path],
    env: Mapping[str, str],
    manifest: TrustManifest | None = None,
    exists: Exists = os.path.exists,
    tier: Tier = Tier.BASIC,
    permissions: Permissions | None = None,
    kvm_present: bool = False,
    seccomp_profile: Path | None = None,
    extras: TierExtras | None = None,
) -> RealmSpec:
    """The spec for a new realm of `tier`, Basic by default, with services off.

    The caller sets `services` on the result (ops/_create_plan.py), so the flag is not an argument.

    The image is the digest that the Trust Manifest pins for `distro` (FR-011, NFR-SEC-004). The
    binds come from `mounts.create_binds`, which checks the Realm Home against Owned Paths
    (DR-021) and skips a missing system path (AS-009). The tier's create-time options come from
    `caps.create_options` (NFR-SEC-002). `kvm_present` and `seccomp_profile` are the host probe and
    the generated profile, which the caller reads (ADR-006). Its reads are the `exists` probe, the
    `realpath` calls of the mount plan and the packaged Trust Manifest. It writes nothing.
    """
    custom = Permissions() if permissions is None else permissions
    toggles = effective_toggles(tier, custom.toggles)
    binds = create_binds(
        tier,
        host_home=host_home,
        uid=uid,
        realm_home=realm_home,
        owned_paths=owned_paths,
        env=env,
        exists=exists,
        toggles=toggles,
    )
    options = create_options(
        toggles,
        kvm_present=kvm_present,
        seccomp_profile=seccomp_profile,
        extras=TierExtras() if extras is None else extras,
        home=host_home,
        owned_paths=owned_paths,
        env=env,
    )
    return RealmSpec(
        name=name,
        distro=distro,
        image=pull_reference(distro.value, manifest),
        uid=uid,
        gid=gid,
        user=user,
        login_shell=login_shell,
        home=host_home,
        binds=binds,
        tier=tier,
        options=options,
    )


def services_cgroup_parent(uid: int, name: RealmName) -> str:
    """The services-on cgroup parent, `/ginn/<uid>/<realm>`, relative to `/sys/fs/cgroup` (ADR-003).

    `uid` is the host user's. The realm's cgroup sits under the user's ginn subtree, which the
    supervisor's run script delegates (ADR-003, FR-001). Podman creates the container's cgroup below
    it, so the services run inside that subtree.
    """
    return f"/ginn/{uid}/{name.value}"


def mitigation_mounts(spec: RealmSpec) -> list[Mount]:
    """The create-time mounts of ADR-007 mitigations a and b; none unless the spec sets them.

    Mitigation a binds the ginn-owned drop-in read-only. Mitigation b binds `/dev/null` read-only
    over each masked unit path. A missing target is made by podman in the container layer (CR-007).
    """
    if not spec.services_mitigations:
        return []
    mounts = [
        Mount(
            type="bind",
            source=str(spec.journald_dropin),
            target=JOURNALD_DROPIN_TARGET,
            options=("ro",),
        )
    ]
    mounts += [
        Mount(type="bind", source="/dev/null", target=f"{UNIT_DIR}/{unit}", options=("ro",))
        for unit in SERVICES_MASKED_UNITS
    ]
    return mounts


def create_spec(spec: RealmSpec) -> CreateSpec:
    """The adapter's `CreateSpec` for `spec`. Pure: the spec is read and nothing else happens.

    Services off is the base spec: catatonit as init and the keep-alive command. Services on
    replaces the init, the command, the user, the cgroup and the stop options, and adds the
    environment, as ADR-003 and ADR-007 set them. Every other field is the same in both.
    """
    base = CreateSpec(
        name=spec.container_name,
        image_digest_ref=spec.image,
        network=network_of(spec.distro),
        user=f"{spec.uid}:{spec.gid}",
        userns="keep-id",
        passwd_entry=spec.passwd_entry,
        http_proxy=False,
        init=True,
        entrypoint_argv=list(KEEP_ALIVE_ENTRYPOINT),
        command=list(KEEP_ALIVE_COMMAND),
        labels=spec.labels,
        mounts=[*spec.binds.mounts, *spec.options.mounts],
        devices=[*spec.binds.devices, *spec.options.devices],
        cdi_devices=[] if spec.options.cdi is None else [spec.options.cdi.device],
        cdi_spec_dir=None if spec.options.cdi is None else str(spec.options.cdi.spec_dir),
        caps_add=list(cap_add_of(spec.distro)),
        security_opts=list(spec.options.security_opts),
        group_add=list(spec.options.group_add),
        tmpfs=list(spec.binds.tmpfs),
    )
    if not spec.services:
        return base
    return replace(
        base,
        user="0:0",
        init=False,
        entrypoint_argv=[],
        command=list(SERVICES_COMMAND),
        systemd="always",
        cgroupns="private",
        cgroup_parent=services_cgroup_parent(spec.uid, spec.name),
        conmon_pidfile=spec.conmon_pidfile,
        stop_signal=SERVICES_STOP_SIGNAL,
        stop_timeout=SERVICES_STOP_TIMEOUT_S,
        env=dict(SERVICES_ENV),
        mounts=[*base.mounts, *mitigation_mounts(spec)],
    )


def create_argv(spec: RealmSpec) -> list[str]:
    """The `podman create` argv for `spec`, without the global flags (see the module docstring)."""
    return create_spec(spec).to_argv()
