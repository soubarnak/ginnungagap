"""Translate the systemd-nspawn argv built by launch._command to an LXC config.

The translation fails closed: every option that is not understood raises
UnsupportedLaunchOption, so a new nspawn flag (for example a security mount)
can never be silently dropped.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import devices_lxc, userns as userns_mod

SECCOMP_BASE = Path("/usr/share/lxc/config/common.seccomp")
API_VFS_WRITABLE = "SYSTEMD_NSPAWN_API_VFS_WRITABLE"
# The names start with lxc- (void/apparmor/lxc-spaces-container). The second one is for guests in a
# user namespace and also allows a fresh proc mount (nested containers).
APPARMOR_PROFILE = "lxc-spaces-container"
APPARMOR_PROFILE_USERNS = "lxc-spaces-container-userns"
INIT_CANDIDATES = (
    "/usr/lib/systemd/systemd",
    "/lib/systemd/systemd",
    "/sbin/init",
)
NSPAWN_DEFAULT_CAPS = (
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_IPC_OWNER",
    "CAP_KILL",
    "CAP_LEASE",
    "CAP_LINUX_IMMUTABLE",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_BROADCAST",
    "CAP_NET_RAW",
    "CAP_SETGID",
    "CAP_SETFCAP",
    "CAP_SETPCAP",
    "CAP_SETUID",
    "CAP_SYS_ADMIN",
    "CAP_SYS_CHROOT",
    "CAP_SYS_NICE",
    "CAP_SYS_PTRACE",
    "CAP_SYS_TTY_CONFIG",
    "CAP_SYS_RESOURCE",
    "CAP_SYS_BOOT",
    "CAP_AUDIT_WRITE",
    "CAP_AUDIT_CONTROL",
    "CAP_MKNOD",
)
NET_SYSCTL_STAGE = "/run/spaces-host/proc-sys-net"
NET_SYSCTL = "/proc/sys/net"
# Top-level guest directories that LXC or the translator mounts fresh, so the
# persistent rootfs below them is not visible and cannot be inspected.
FRESH_TMPFS = ("run", "tmp")
FRESH_OTHER = ("dev", "proc", "sys")
USERNS_MASKED_UNITS = ("run-rpc_pipefs.mount", "var-lib-nfs-rpc_pipefs.mount")
OPTIONAL_DEVICE_BINDS = ("/dev/net/tun", "/dev/fuse")
_CAP_NAME = re.compile(r"^CAP_[A-Z0-9_]+$")


class UnsupportedLaunchOption(ValueError):
    """The nspawn argv contains something the LXC backend cannot honour."""


@dataclass(frozen=True)
class LxcSpec:
    name: str
    rootfs: Path
    runtime_dir: Path
    config_text: str
    # Runtime files the backend must write (path -> text).
    seccomp_path: Path
    seccomp_text: str
    devices_path: Path
    devices_text: str
    # Host-copied resolv.conf and the rootfs path to replace by a plain file.
    resolv_conf: Path | None = None
    resolv_target: str | None = None
    # (rootfs-relative path, "dir" | "file") to create before starting.
    precreate: tuple[tuple[str, str], ...] = ()
    cgroup_base: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _unescape(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index >= len(value) or value[index] not in "\\:":
                raise UnsupportedLaunchOption(f"bad escape in {value!r}")
            current.append(value[index])
        elif char == ":":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _fstab_escape(value: str) -> str:
    if "\t" in value or "\n" in value or "\r" in value:
        raise UnsupportedLaunchOption(f"unsupported whitespace in {value!r}")
    return (
        value.replace("\\", "\\134").replace(" ", "\\040")
    )


def _config_value(value: str) -> str:
    if "\n" in value or "\r" in value or "\t" in value:
        raise UnsupportedLaunchOption(f"unsupported whitespace in {value!r}")
    return value


def _capabilities(value: str) -> list[str]:
    names = [item for item in value.split(",") if item]
    for item in names:
        if not _CAP_NAME.match(item):
            raise UnsupportedLaunchOption(f"bad capability: {item!r}")
    return names


def resolve_in_root(rootfs: Path, destination: str) -> str:
    """Resolve an absolute guest path inside rootfs, following symlinks.

    The walk behaves like a chroot: absolute links restart at the root and
    ".." never leaves it. Once the path enters a directory that is mounted
    fresh at start (/run, /tmp, /dev, /proc, /sys) the rest is kept literal.
    Returns the root-relative path without a leading slash.
    """

    if not destination.startswith("/"):
        raise UnsupportedLaunchOption(f"relative bind target: {destination!r}")
    pending = [c for c in destination.split("/") if c and c != "."]
    if ".." in pending:
        raise UnsupportedLaunchOption(f"'..' in bind target: {destination!r}")
    resolved: list[str] = []
    links = 0
    while pending:
        component = pending.pop(0)
        if component == "..":
            if resolved:
                resolved.pop()
            continue
        if not resolved and component in (*FRESH_TMPFS, *FRESH_OTHER):
            if ".." in pending:
                raise UnsupportedLaunchOption(
                    f"'..' in bind target: {destination!r}"
                )
            resolved.append(component)
            resolved.extend(c for c in pending if c != ".")
            break
        candidate = rootfs.joinpath(*resolved, component)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise UnsupportedLaunchOption(
                    f"too many symlinks resolving {destination!r}"
                )
            target = os.readlink(candidate)
            if target.startswith("/"):
                resolved = []
            pending = [c for c in target.split("/") if c and c != "."] + pending
            continue
        resolved.append(component)
    if not resolved:
        raise UnsupportedLaunchOption(f"bind target is the root: {destination!r}")
    return "/".join(resolved)


@dataclass
class _Bind:
    source: str
    destination: str
    read_only: bool


def _seccomp_text(base: str, perf_event_open: bool) -> str:
    if perf_event_open:
        return base
    lines = base.splitlines()
    if "[all]" not in (line.strip() for line in lines):
        lines.append("[all]")
    index = [line.strip() for line in lines].index("[all]")
    lines.insert(index + 1, "perf_event_open errno 1")
    return "\n".join(lines) + "\n"


def translate(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    runtime_dir: Path,
    device_rules: Sequence[str] | None = (),
    cgroup_base: str | None = None,
    seccomp_base: str | None = None,
    userns: userns_mod.Plan | None = None,
    mountinfo: Sequence[tuple[str, str]] | None = None,
) -> LxcSpec:
    """Translate nspawn arguments into an LXC configuration.

    device_rules are extra cgroup2 allow rules; None means unrestricted.
    cgroup_base is the host cgroup (relative to the cgroup2 root) lxc-start
    runs in, e.g. "spaces/NAME"; None selects the absolute spaces-NAME dirs.
    userns, when given, runs the guest in a user namespace with that id map
    (see userns.py); mountinfo is the host's mount table for the /sys bind.
    """

    args = list(argv)
    if args and os.path.basename(args[0]) == "systemd-nspawn":
        args = args[1:]

    rootfs: Path | None = None
    machine: str | None = None
    hostname: str | None = None
    binds: list[_Bind] = []
    environment: list[str] = []
    drop: list[str] = []
    add: list[str] = []
    perf_event_open = False
    resolv_bind_host = False
    boot = False
    console_read_only = False
    private_users_no = False

    for arg in args:
        option, equals, value = arg.partition("=")
        if option in ("--quiet", "--boot", "--keep-unit"):
            if equals:
                raise UnsupportedLaunchOption(arg)
            boot = boot or option == "--boot"
        elif option == "--directory" and equals:
            rootfs = Path(value)
        elif option == "--machine" and equals:
            machine = value
        elif option == "--hostname" and equals:
            hostname = value
        elif option in ("--bind", "--bind-ro") and equals:
            parts = _unescape(value)
            if len(parts) != 2 or not all(parts):
                raise UnsupportedLaunchOption(arg)
            binds.append(_Bind(parts[0], parts[1], option == "--bind-ro"))
        elif option == "--setenv" and equals:
            if "=" not in value:
                raise UnsupportedLaunchOption(arg)
            environment.append(value)
        elif arg == "--console=read-only":
            console_read_only = True
        elif arg == "--private-users=no":
            private_users_no = True
        elif arg in ("--settings=no", "--notify-ready=yes"):
            pass
        elif arg == "--resolv-conf=bind-host":
            resolv_bind_host = True
        elif arg == "--system-call-filter=perf_event_open":
            perf_event_open = True
        elif option == "--drop-capability" and equals:
            drop.extend(_capabilities(value))
        elif option == "--capability" and equals:
            add.extend(_capabilities(value))
        else:
            # Includes --tmpfs, --selinux-*, --overlay and anything new.
            raise UnsupportedLaunchOption(arg)

    if rootfs is None or machine is None:
        raise UnsupportedLaunchOption("--directory and --machine are required")
    if not boot:
        raise UnsupportedLaunchOption("--boot is required")
    if not console_read_only:
        raise UnsupportedLaunchOption("--console=read-only is required")
    if not private_users_no:
        raise UnsupportedLaunchOption("--private-users=no is required")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", machine):
        raise UnsupportedLaunchOption(f"bad machine name: {machine!r}")

    init = next(
        (
            candidate
            for candidate in INIT_CANDIDATES
            if (rootfs / candidate.lstrip("/")).exists()
        ),
        None,
    )
    if init is None:
        raise UnsupportedLaunchOption(f"no init found in {rootfs}")

    caps: list[str] = []
    for capability in (*NSPAWN_DEFAULT_CAPS, *add):
        if capability not in drop and capability not in caps:
            caps.append(capability)
    cap_keep = " ".join(c[len("CAP_"):].lower() for c in caps)

    # The network sysctl pair stages the host's /proc/sys/net outside /proc so
    # nspawn can mount it after making /proc/sys read-only. LXC mounts
    # proc:mixed first, so the pair collapses to one bind onto /proc/sys/net.
    stage = [b for b in binds if b.destination == NET_SYSCTL_STAGE]
    over = [b for b in binds if b.destination == NET_SYSCTL]
    if (
        len(stage) == 1
        and len(over) == 1
        and stage[0].source == NET_SYSCTL
        and over[0].source == NET_SYSCTL_STAGE
        and not stage[0].read_only
        and not over[0].read_only
    ):
        binds = [b for b in binds if b is not stage[0] and b is not over[0]]
        binds.append(_Bind(NET_SYSCTL, NET_SYSCTL, False))

    if userns is not None:
        # rpc_pipefs belongs to the network namespace, which the guest's user namespace does not
        # own: the mount is refused (EPERM) and the unit nfs-utils generates for it fails, which
        # leaves the guest degraded. Nothing but an NFS client or server in the guest uses it.
        for unit in USERNS_MASKED_UNITS:
            path = f"/etc/systemd/system/{unit}"
            if not any(b.destination == path for b in binds):
                binds.append(_Bind("/dev/null", path, True))

    for device in OPTIONAL_DEVICE_BINDS:
        if not any(b.destination == device for b in binds):
            binds.append(_Bind(device, device, False))

    resolv_conf: Path | None = None
    resolv_target: str | None = None
    if resolv_bind_host:
        resolv_conf = runtime_dir / "resolv.conf"
        resolv_target = "etc/resolv.conf"

    entries: list[tuple[int, str]] = []
    precreate: list[tuple[str, str]] = []

    def add_bind(source: str, destination: str, read_only: bool, optional: bool) -> None:
        try:
            mode = os.stat(source).st_mode
        except OSError as error:
            if optional:
                return
            if read_only and (
                source == "/run/spaces" or source.startswith("/run/spaces")
            ):
                raise UnsupportedLaunchOption(
                    f"missing required runtime source: {source}"
                ) from error
            raise UnsupportedLaunchOption(
                f"bind source does not exist: {source}"
            ) from error
        kind = "dir" if stat.S_ISDIR(mode) else "file"
        if destination == "/" + (resolv_target or "\0"):
            relative = resolv_target
        else:
            relative = resolve_in_root(rootfs, destination)
        top = relative.split("/", 1)[0]
        options = ["rbind" if kind == "dir" else "bind"]
        if read_only:
            options.append("ro")
        if userns is not None and userns_mod.needs_idmap(source):
            options.append("idmap=container")
        if top in (*FRESH_TMPFS, *FRESH_OTHER):
            options.append(f"create={kind}")
        elif relative != resolv_target:
            precreate.append((relative, kind))
        if optional:
            options.append("optional")
        entries.append(
            (
                relative.count("/") + 1,
                f"lxc.mount.entry = {_fstab_escape(source)} "
                f"{_fstab_escape(relative)} none {','.join(options)} 0 0",
            )
        )

    for bind in binds:
        optional = bind.source in OPTIONAL_DEVICE_BINDS
        add_bind(bind.source, bind.destination, bind.read_only, optional)
    if resolv_conf is not None:
        entries.append(
            (
                2,
                f"lxc.mount.entry = {_fstab_escape(str(resolv_conf))} "
                f"{resolv_target} none bind,ro,create=file 0 0",
            )
        )
    entries.sort(key=lambda item: item[0])

    if env.get(API_VFS_WRITABLE) == "yes":
        mount_auto = "proc:rw sys:rw cgroup:rw:force"
    else:
        mount_auto = "proc:mixed sys:mixed cgroup:rw:force"
    userns_lines: list[str] = []
    sys_lines: list[str] = []
    if userns is not None:
        # A fresh sysfs is refused in a user namespace that shares the host's network namespace
        # (EPERM), so /sys is a read-only bind of the host's and each host mount below it is hidden
        # under an empty tmpfs (LXC mounts its own cgroup2 on the last of them afterwards).
        mount_auto = mount_auto.replace("sys:rw ", "").replace("sys:mixed ", "")
        table = list(mountinfo) if mountinfo is not None else userns_mod.read_mountinfo()
        userns_lines = ["lxc.rootfs.options = idmap=container", *userns.idmap_lines()]
        sys_lines = [
            f"lxc.mount.entry = /sys sys none {userns_mod.sys_bind_options(table)} 0 0",
            *(
                f"lxc.mount.entry = tmpfs {point.lstrip('/')} tmpfs ro,nosuid,nodev,noexec,size=4k 0 0"
                for point in userns_mod.sys_submounts(table)
            ),
        ]

    if cgroup_base is None:
        cgroup_lines = [
            f"lxc.cgroup.dir.container = spaces-{machine}",
            f"lxc.cgroup.dir.monitor = spaces-{machine}-mon",
        ]
    else:
        cgroup_lines = [
            "lxc.cgroup.relative = 1",
            "lxc.cgroup.dir.monitor = monitor",
            "lxc.cgroup.dir.monitor.pivot = pivot",
            "lxc.cgroup.dir.container = payload",
            "lxc.cgroup.dir.container.inner = guest",
        ]

    lines = [
        f"lxc.uts.name = {_config_value(hostname or machine)}",
        f"lxc.rootfs.path = dir:{_config_value(str(rootfs))}",
        *userns_lines,
        "lxc.net.0.type = none",
        # The wrapper starts lxc-start in a private network namespace (so the
        # abstract command socket stays out of the guest's reach); the container
        # joins the host's.
        "lxc.namespace.share.net = /proc/1/ns/net",
        "lxc.autodev = 1",
        "lxc.tty.max = 0",
        "lxc.pty.max = 1024",
        "lxc.console.path = none",
        f"lxc.init.cmd = {init}",
        "lxc.signal.halt = SIGRTMIN+3",
        f"lxc.mount.auto = {mount_auto}",
        f"lxc.apparmor.profile = {APPARMOR_PROFILE_USERNS if userns is not None else APPARMOR_PROFILE}",
        *cgroup_lines,
        f"lxc.cap.keep = {cap_keep}",
        f"lxc.seccomp.profile = {runtime_dir / 'seccomp.profile'}",
        *(f"lxc.environment = {_config_value(item)}" for item in environment),
        f"lxc.include = {runtime_dir / 'devices.conf'}",
        "lxc.mount.entry = tmpfs run tmpfs rw,nosuid,nodev,mode=755 0 0",
        "lxc.mount.entry = tmpfs tmp tmpfs rw,nosuid,nodev 0 0",
        *sys_lines,
        *(text for _depth, text in entries),
    ]

    if seccomp_base is None:
        try:
            seccomp_base = SECCOMP_BASE.read_text(encoding="utf-8")
        except OSError as error:
            raise UnsupportedLaunchOption(
                f"cannot read seccomp profile: {error}"
            ) from error

    return LxcSpec(
        name=machine,
        rootfs=rootfs,
        runtime_dir=runtime_dir,
        config_text="\n".join(lines) + "\n",
        seccomp_path=runtime_dir / "seccomp.profile",
        seccomp_text=_seccomp_text(seccomp_base, perf_event_open),
        devices_path=runtime_dir / "devices.conf",
        devices_text=devices_lxc.config_text(device_rules),
        resolv_conf=resolv_conf,
        resolv_target=resolv_target,
        precreate=tuple(dict.fromkeys(precreate)),
        cgroup_base=cgroup_base,
    )
