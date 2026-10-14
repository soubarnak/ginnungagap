# SPDX-License-Identifier: Apache-2.0
"""Podman adapter Protocol, the create spec and the network retry rule (ADR-002, IR-020).

Runtime, operations and tests depend on `Podman`. cli.py implements it with the podman command
line, and tests/fakes/fake_podman.py implements it in memory. Every call passes an argv list and
never a shell string (NFR-SEC-005). Images are pulled and created only by digest (FR-011).
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from ginn.errors import GinnError
from ginn.podman.models import ContainerInfo, ExecResult, ImageInfo, PodmanInfo

RetryHook = Callable[[int, GinnError], bool]
"""Called as hook(attempt, error) after a failed attempt. Return True to retry (FR-120)."""

MAX_NETWORK_RETRIES: Final = 3
"""Retries after the first attempt. M8 wires the 5, 15 and 45 s delays into the hook (FR-120)."""

_DIGEST_REF: Final = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ENV_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_USER: Final = re.compile(r"[0-9]+:[0-9]+")
_MOUNT_TYPES: Final = frozenset({"bind", "volume", "tmpfs"})
_NETWORKS: Final = frozenset({"host", "pasta"})
PRIVILEGED_MESSAGE: Final = "NFR-SEC-001: podman create must never run privileged"
_SECURITY_OPT: Final = re.compile(r"([A-Za-z-]+)(?:[=:](.*))?", re.DOTALL)
_CONTROL: Final = frozenset("\0\n\r")
PODMAN_ERROR_EXIT: Final = 125
"""Exit status podman uses for its own failures (`podman exec` included)."""
PATH_NOT_FOUND_REASON: Final = "no such path in the container"
"""The `reason` detail of a GINN-E-INTERNAL from `cp_from` or `cp_into` when the path that the
call names is absent in the container. The ops layer reads it (ops/create.py, GINN-E-NO-INIT)."""


def require_digest_ref(ref: str) -> None:
    """Raise ValueError unless `ref` is `repo@sha256:<64 hex>` (FR-011: pull by digest only)."""
    if _DIGEST_REF.fullmatch(ref) is None:
        raise ValueError(f"FR-011: image must be pulled by digest repo@sha256:<hex>, got {ref!r}")


def require_arg(value: str, what: str) -> str:
    """Reject empty values and values that podman would read as an option (NFR-SEC-005)."""
    if not value or value.startswith("-"):
        raise ValueError(f"{what} must not be empty or start with '-': {value!r}")
    return value


def with_network_retry[T](action: Callable[[], T], retry_hook: RetryHook | None) -> T:
    """Run `action`. On GINN-E-NETWORK, ask `retry_hook` up to MAX_NETWORK_RETRIES times (FR-120).

    `attempt` passed to the hook is the 1-based number of the attempt that failed. Other error
    codes are never retried.

    Superseded by ops/retry.py (T8.1, FR-120): its `with_retry` and `network_retry_hook` hold the
    5, 15 and 45 s delays and the shared network classifier. This function stays until every caller
    moves to ops/retry.py, because ops/create.py still calls it through the adapter.
    """
    failures = 0
    while True:
        try:
            return action()
        except GinnError as err:
            if err.code != "GINN-E-NETWORK" or retry_hook is None:
                raise
            if failures >= MAX_NETWORK_RETRIES:
                raise
            failures += 1
            if not retry_hook(failures, err):
                raise


@dataclass(frozen=True)
class Mount:
    """One `--mount` entry. Source, target and options may not contain a comma."""

    type: str
    source: str
    target: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreateSpec:
    """Everything `podman create` needs for one container. `to_argv` renders it.

    The spec has no privileged field. `to_argv` raises ValueError if any argument would make the
    container privileged (NFR-SEC-001).
    """

    name: str
    image_digest_ref: str
    network: str
    hostname: str | None = None
    user: str | None = None
    userns: str | None = None
    passwd_entry: str | None = None
    init: bool = False
    init_path: str | None = None
    systemd: str | None = None
    http_proxy: bool | None = None
    cgroupns: str | None = None
    cgroup_parent: str | None = None
    conmon_pidfile: str | None = None
    """The conmon pidfile of a services-on realm, under ginn's runtime root (FR-081, ADR-003).
    `ginn-wait` reads it, so podman keeps the same path for every start of this container."""
    stop_signal: str | None = None
    stop_timeout: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    caps_add: list[str] = field(default_factory=list)
    security_opts: list[str] = field(default_factory=list)
    group_add: list[str] = field(default_factory=list)
    tmpfs: list[str] = field(default_factory=list)
    cdi_devices: list[str] = field(default_factory=list)
    cdi_spec_dir: str | None = None
    """The CDI spec directory of `cdi_devices`, passed as the global `--cdi-spec-dir` (ADR-012)."""
    entrypoint_argv: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)

    def to_argv(self) -> list[str]:
        """Return the `podman create` argv (without the podman binary or global flags).

        The order is fixed and env and labels are sorted, so equal specs give equal argv. The
        entrypoint is passed as a JSON array to `--entrypoint`. The image comes next, and the
        command, when set, follows the image as separate arguments.
        """
        _check_spec(self)
        argv = ["create", "--name", self.name]
        _opt(argv, "--hostname", self.hostname)
        _opt(argv, "--user", self.user)
        _opt(argv, "--userns", self.userns)
        # Podman writes this entry verbatim for the keep-id user (T4.9b). `--passwd=false` is not
        # a `podman create` flag on 5.8.8, so it is never sent.
        _opt(argv, "--passwd-entry", self.passwd_entry)
        argv += ["--network", self.network]
        if self.http_proxy is not None:
            # False keeps the host's proxy variables out of the realm (T4.1, ADR-004 amendment).
            argv.append(f"--http-proxy={'true' if self.http_proxy else 'false'}")
        _opt(argv, "--cgroupns", self.cgroupns)
        _opt(argv, "--cgroup-parent", self.cgroup_parent)
        _opt(argv, "--conmon-pidfile", self.conmon_pidfile)
        if self.systemd is not None:
            argv.append(f"--systemd={self.systemd}")
        if self.init:
            argv.append("--init")
            _opt(argv, "--init-path", self.init_path)
        _opt(argv, "--stop-signal", self.stop_signal)
        if self.stop_timeout is not None:
            argv += ["--stop-timeout", str(self.stop_timeout)]
        for key in sorted(self.env):
            argv += ["--env", f"{key}={self.env[key]}"]
        for key in sorted(self.labels):
            argv += ["--label", f"{key}={self.labels[key]}"]
        for mount in self.mounts:
            argv += ["--mount", _mount_value(mount)]
        for value in self.tmpfs:
            argv += ["--tmpfs", value]
        for value in [*self.devices, *self.cdi_devices]:
            argv += ["--device", value]
        for cap in self.caps_add:
            argv += ["--cap-add", cap]
        for opt in self.security_opts:
            argv += ["--security-opt", opt]
        for group in self.group_add:
            argv += ["--group-add", group]
        if self.entrypoint_argv:
            argv += ["--entrypoint", json.dumps(self.entrypoint_argv)]
        argv.append(self.image_digest_ref)
        argv += self.command
        _refuse_privileged(argv, self.caps_add)
        return argv

    def global_flags(self) -> list[str]:
        """The podman global flags of this create. They come before the subcommand (S-06).

        `--cdi-spec-dir` is global, so PodmanCli passes these ahead of `create` for this call only,
        and never for another call (ADR-012). The directory is checked here as in `to_argv`.
        """
        if self.cdi_spec_dir is None:
            return []
        _check_cdi_dir(self.cdi_spec_dir)
        return ["--cdi-spec-dir", self.cdi_spec_dir]


def _opt(argv: list[str], flag: str, value: str | None) -> None:
    if value is not None:
        argv += [flag, value]


def _check_spec(spec: CreateSpec) -> None:
    require_digest_ref(spec.image_digest_ref)
    if _NAME.fullmatch(spec.name) is None:
        raise ValueError(f"container name {spec.name!r} is not a valid podman name")
    if spec.user is not None and _USER.fullmatch(spec.user) is None:
        raise ValueError(f"user must be uid:gid, got {spec.user!r}")
    if spec.userns not in (None, "keep-id"):
        raise ValueError(f"userns must be 'keep-id' or None, got {spec.userns!r}")
    if spec.passwd_entry is not None:
        _check_passwd_entry(spec.passwd_entry, keep_id=spec.userns == "keep-id")
    if spec.network not in _NETWORKS and not spec.network.startswith("pasta:"):
        raise ValueError(f"network must be host, pasta or pasta:<opts>, got {spec.network!r}")
    if spec.systemd not in (None, "always"):
        raise ValueError(f"systemd must be 'always' or None, got {spec.systemd!r}")
    if spec.init_path is not None and not spec.init:
        raise ValueError("init_path is set but init is off")
    for key in spec.env:
        if _ENV_KEY.fullmatch(key) is None:
            raise ValueError(f"environment name {key!r} is not valid")
    for key in spec.labels:
        if not key or "=" in key:
            raise ValueError(f"label name {key!r} is not valid")
    if spec.conmon_pidfile is not None:
        _check_plain_path("conmon pidfile", spec.conmon_pidfile)
    for mount in spec.mounts:
        _check_mount(mount)
    for group in spec.group_add:
        require_arg(group, "group-add")


def _check_passwd_entry(entry: str, *, keep_id: bool) -> None:
    """Refuse an entry that podman would rewrite, or that the realm user does not use (T4.9b).

    Podman expands `$NAME` placeholders anywhere in the entry (measured: `$USERNAME` in a home
    path). A newline or a field count other than seven would change the file. The entry belongs to
    the keep-id realm user only.
    """
    if not keep_id:
        raise ValueError("passwd_entry belongs to the keep-id realm user: use keep-id")
    if "$" in entry or "\n" in entry or len(entry.split(":")) != 7:
        raise ValueError(f"passwd entry needs seven colon fields, no $ and no newline: {entry!r}")


def _check_plain_path(label: str, value: str) -> None:
    """An absolute path with no line break, NUL or comma, as podman takes it in one argv word."""
    if not value.startswith("/") or "," in value or any(c in value for c in "\0\n\r"):
        raise ValueError(f"NFR-SEC-005: {label} must be a plain absolute path: {value!r}")


def _check_mount(mount: Mount) -> None:
    if mount.type not in _MOUNT_TYPES:
        raise ValueError(f"mount type must be bind, volume or tmpfs, got {mount.type!r}")
    for text in (mount.source, mount.target, *mount.options):
        if "," in text or "\n" in text:
            raise ValueError(f"mount field may not contain a comma or newline: {text!r}")
    if not mount.target.startswith("/"):
        raise ValueError(f"mount target must be absolute: {mount.target!r}")


def _mount_value(mount: Mount) -> str:
    parts = [f"type={mount.type}", f"source={mount.source}", f"target={mount.target}"]
    return ",".join([*parts, *mount.options])


def _check_cdi_dir(value: str) -> None:
    """A CDI spec directory is a plain absolute path, with nothing that podman would split on."""
    if not value.startswith("/") or value != value.strip() or any(c in value for c in ",:=\0\n\r"):
        raise ValueError(
            f"NFR-SEC-005: a CDI spec directory must be a plain absolute path: {value!r}"
        )


def unsafe_security_opt(opt: str) -> bool:
    """True for a `--security-opt` value that lowers a confinement, in any spelling (NFR-SEC-001).

    podman reads `key=value` and `key:value`, so both forms are read. A leading or trailing space, a
    control character, and a form this reader cannot parse are refused as well: the test is what
    podman might do with the value, not how it is spelled.
    """
    if opt != opt.strip() or _CONTROL & set(opt):
        return True
    match = _SECURITY_OPT.fullmatch(opt)
    if match is None:
        return True
    key = match.group(1).lower()
    value = (match.group(2) or "").strip().lower()
    if key == "privileged":
        return True
    if key in ("seccomp", "apparmor", "systempaths") and value == "unconfined":
        return True
    if key == "label" and (value.startswith("disable") or value.startswith("type:spc_t")):
        return True
    if key == "unmask" and value == "all":
        return True
    return key == "no-new-privileges" and value in ("false", "0", "no")


def _refuse_privileged(argv: Sequence[str], caps_add: Sequence[str]) -> None:
    """Raise ValueError if the argv could make the container privileged (NFR-SEC-001).

    This is the second check. The allowlist of tiers (security/caps.py) is the first. Here every
    `--security-opt` value of the argv is read too, in its separate-token and its `=` form, so a
    value that lowers a confinement is refused whatever built it (ADR-006).
    """
    for index, arg in enumerate(argv):
        if arg == "--privileged" or arg.startswith("--privileged="):
            raise ValueError(PRIVILEGED_MESSAGE)
        if arg == "--security-opt" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--security-opt="):
            value = arg.partition("=")[2]
        else:
            continue
        if unsafe_security_opt(value):
            raise ValueError(f"{PRIVILEGED_MESSAGE}; security option {value!r} lowers confinement")
    if any(cap.strip().upper() == "ALL" for cap in caps_add):
        raise ValueError(f"{PRIVILEGED_MESSAGE}; capability ALL is refused")


class Podman(Protocol):
    """The adapter. Failures raise GinnError with a catalogue code (NFR-040)."""

    def info(self) -> PodmanInfo: ...

    def image_exists(self, ref: str) -> bool: ...

    def pull(self, ref_with_digest: str, *, retry_hook: RetryHook | None = None) -> ImageInfo:
        """Pull by digest and return the image. A ref without `@sha256:` raises ValueError."""
        ...

    def image_inspect(self, ref: str) -> ImageInfo: ...

    def rmi(self, ref: str) -> None: ...

    def create(self, spec: CreateSpec) -> str:
        """Create a container and return its ID."""
        ...

    def start(self, container_id: str) -> None: ...

    def stop(self, container_id: str, timeout_s: int) -> None: ...

    def rm(self, container_id: str, force: bool) -> None: ...

    def inspect(self, container_id: str) -> ContainerInfo: ...

    def ps(self, label_filter: Mapping[str, str] | None = None) -> list[ContainerInfo]:
        """List all containers, optionally only those whose labels match every pair."""
        ...

    def exec(
        self,
        container_id: str,
        argv: Sequence[str],
        *,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        tty: bool = False,
        interactive: bool = False,
        capture: bool = True,
        pass_env: Sequence[str] = (),
        timeout_s: float | None = None,
        max_output: int | None = None,
        stdout_tail: bool = False,
    ) -> ExecResult:
        """Run argv in a running container. With capture=False the output is not captured.

        The output goes straight to the caller's stdio, and the result has empty stdout and
        stderr. The exit status is the inner command's status. `pass_env` names variables that
        podman copies from its own environment, as `--env NAME`, so no value reaches the argv.
        `timeout_s` and `max_output` bound a captured run (FR-044): when the run takes longer, or
        its stdout passes `max_output` bytes, the podman client is killed and GINN-E-INTERNAL is
        raised with the reason in its details. Both default to no limit. A bounded run decodes its
        stdout with surrogateescape, so encoding it back gives the exact bytes of the output.
        """
        ...

    def exec_interactive(
        self,
        container_id: str,
        argv: Sequence[str],
        *,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        tty: bool = False,
        interactive: bool = False,
        pass_env: Sequence[str] = (),
        on_spawn: Callable[[int], None] | None = None,
        on_exit: Callable[[], None] | None = None,
    ) -> int:
        """Run argv with the caller's stdio and return the exit status (for `ginn enter`).

        `pass_env` is as in `exec`: names only, copied by podman from its own environment.
        `on_spawn`, when given, receives the pid of the podman client as soon as it starts, before
        the wait, so the caller can signal it (FR-068). `on_exit`, when given, runs as soon as the
        client is reaped, so the caller stops signalling that pid before it can be reused.
        """
        ...

    def cp_from(self, container_id: str, src: str, dst: Path) -> None:
        """Copy `src` out of the container to the host path `dst`. Podman follows a link in `src`.

        A `src` that the container lacks raises GINN-E-INTERNAL with `reason` set to
        PATH_NOT_FOUND_REASON.
        """
        ...

    def cp_into(self, container_id: str, src: Path, dst: str) -> None:
        """Copy the host file `src` into the container at `dst`. The mode is kept, and the owner is
        the container's primary uid (`podman cp --archive`, the default).

        Used for the services-on machine-id before the first start (ADR-007, F6). The container may
        be created and not started. A `dst` whose parent the container lacks raises GINN-E-INTERNAL
        with `reason` set to PATH_NOT_FOUND_REASON.
        """
        ...

    def top(self, container_id: str) -> list[tuple[int, int, str]]:
        """Return (pid, rss in KiB, command name) rows for the container's processes."""
        ...

    def logs(self, container_id: str, tail: int) -> str:
        """Return the last `tail` lines of the container's stdout."""
        ...

    def unshare(self, argv: Sequence[str]) -> ExecResult:
        """Run `argv` under `podman unshare`, in the store's user namespace.

        No attach path calls it: the grant helper runs on the host (ADR-008, Resolution A). The host
        gate and the test cleanup use it. The command's own status comes back in the result and is
        not raised. The first word must be an absolute path. Podman's own failure (exit 125) raises
        GinnError.
        """
        ...
