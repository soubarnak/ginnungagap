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
    init: bool = False
    init_path: str | None = None
    systemd: str | None = None
    http_proxy: bool | None = None
    cgroupns: str | None = None
    cgroup_parent: str | None = None
    stop_signal: str | None = None
    stop_timeout: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    caps_add: list[str] = field(default_factory=list)
    security_opts: list[str] = field(default_factory=list)
    tmpfs: list[str] = field(default_factory=list)
    cdi_devices: list[str] = field(default_factory=list)
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
        argv += ["--network", self.network]
        if self.http_proxy is not None:
            # False keeps the host's proxy variables out of the realm (T4.1, ADR-004 amendment).
            argv.append(f"--http-proxy={'true' if self.http_proxy else 'false'}")
        _opt(argv, "--cgroupns", self.cgroupns)
        _opt(argv, "--cgroup-parent", self.cgroup_parent)
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
        if self.entrypoint_argv:
            argv += ["--entrypoint", json.dumps(self.entrypoint_argv)]
        argv.append(self.image_digest_ref)
        argv += self.command
        _refuse_privileged(argv, self.caps_add)
        return argv


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
    for mount in spec.mounts:
        _check_mount(mount)


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


def _refuse_privileged(argv: Sequence[str], caps_add: Sequence[str]) -> None:
    """Raise ValueError if the argv could make the container privileged (NFR-SEC-001)."""
    for arg in argv:
        if arg == "--privileged" or arg.startswith("--privileged="):
            raise ValueError(PRIVILEGED_MESSAGE)
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
    ) -> ExecResult:
        """Run argv in a running container. With capture=False the output is not captured.

        The output goes straight to the caller's stdio, and the result has empty stdout and
        stderr. The exit status is the inner command's status.
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
    ) -> int:
        """Run argv with the caller's stdio and return the exit status (for `ginn enter`)."""
        ...

    def cp_from(self, container_id: str, src: str, dst: Path) -> None: ...

    def top(self, container_id: str) -> list[tuple[int, int, str]]:
        """Return (pid, rss in KiB, command name) rows for the container's processes."""
        ...

    def logs(self, container_id: str, tail: int) -> str:
        """Return the last `tail` lines of the container's stdout."""
        ...

    def unshare(self, argv: Sequence[str]) -> ExecResult:
        """Run `argv` under `podman unshare`, in the store's user namespace (ADR-008).

        The command's own status comes back in the result and is not raised. The first word must
        be an absolute path. Podman's own failure (exit 125) raises GinnError.
        """
        ...
