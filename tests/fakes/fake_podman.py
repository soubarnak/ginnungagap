# SPDX-License-Identifier: Apache-2.0
"""In-memory Podman adapter for unit tests (ADR-002, NFR-061).

FakePodman implements the `Podman` Protocol without a container runtime. It keeps images keyed by
digest reference and containers in a state machine: created, then running on start, then exited
on stop. Every call is recorded in `calls`. Tests inject failures and canned exec results.

Failures use catalogue codes: GINN-E-NETWORK and GINN-E-INTERNAL raise as the CLI does, and
GINN-E-IMAGE-UNVERIFIED for a digest pull. The retry rule matches cli.py, through the same helper.
"""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, NamedTuple

from ginn.errors import GinnError, make
from ginn.podman.adapter import (
    PATH_NOT_FOUND_REASON,
    CreateSpec,
    ExecTarget,
    RetryHook,
    require_digest_ref,
    with_network_retry,
)
from ginn.podman.models import ContainerInfo, ContainerState, ExecResult, ImageInfo, PodmanInfo
from ginn.runtime.spec import INIT_PATH

INJECTABLE_CODES: Final = frozenset(
    {"GINN-E-NETWORK", "GINN-E-IMAGE-UNVERIFIED", "GINN-E-INTERNAL"}
)
DEFAULT_INFO: Final = PodmanInfo(
    rootless=True,
    cgroup_manager="cgroupfs",
    cgroup_version="v2",
    cgroup_controllers=("cpuset", "cpu", "io", "memory", "pids"),
    oci_runtime="runc",
    network_backend="netavark",
    graph_driver="overlay",
    conmon_path="/usr/libexec/podman/conmon",
    pasta_path="/usr/sbin/pasta",
    version="5.8.8",
)
_FAKE_IMAGE_SIZE: Final = 1024
_ENV_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FAKE_IMAGE_CREATED: Final = "1970-01-01T00:00:00Z"


_RUN_USER_DIR: Final = re.compile(r"/run/user/([0-9]+)")
"""The runtime dir of a realm user. The fake answers its owner as the realm user (T7.28)."""


class FakeCall(NamedTuple):
    """One recorded call. Pulls are recorded once per attempt."""

    op: str
    args: tuple[object, ...]


FAKE_CLIENT_PID: Final = 4242
"""The pid the fake reports to `on_ack`. No process runs, so no real pid exists."""


@dataclass
class ExecCall:
    """One exec or exec_interactive call, with its options as the adapter received them.

    `pass_env` holds the names given as `--env NAME`. The fake never sees their values, which
    podman takes from its own environment, so a test checks that no value is in `env`.
    """

    op: str
    container_id: str
    argv: tuple[str, ...]
    user: str | None
    env: dict[str, str]
    pass_env: tuple[str, ...]
    workdir: str | None
    tty: bool
    interactive: bool
    target: ExecTarget | None = None


@dataclass
class _Container:
    id: str
    name: str
    image: str
    labels: dict[str, str]
    state: ContainerState = ContainerState.CREATED
    pid: int = 0
    exit_code: int = 0
    passwd_entry: str | None = None
    """The `--passwd-entry` podman writes for the keep-id user. `getent passwd` reads it (T4.9b)."""
    copied: dict[str, tuple[bytes, int]] = field(default_factory=dict)
    """The files `cp_into` put in the container: destination to (content, mode bits)."""


def _cat(files: Mapping[str, bytes], path: str) -> ExecResult:
    """What `cat -- <path>` prints in a container: the file's bytes, decoded as the adapter does."""
    content = files.get(path)
    if content is None:
        return ExecResult(1, "", f"cat: {path}: No such file or directory\n")
    return ExecResult(0, content.decode("utf-8", "surrogateescape"), "")


def _error(code: str, ref: str) -> GinnError:
    if code == "GINN-E-IMAGE-UNVERIFIED":
        return make(code, distro=ref.partition("@")[0])
    return make(code)


_FAKE_INIT: Final = b"systemd\n"
"""The stand-in for systemd's binary at `/sbin/init`, which the fake image carries by default."""


def _path_not_found() -> GinnError:
    """The error of a `cp` whose container path is absent, as PodmanCli raises it (F3)."""
    err = make("GINN-E-INTERNAL")
    err.details["reason"] = PATH_NOT_FOUND_REASON
    return err


def _getent_passwd(container: _Container, uid: str) -> ExecResult:
    """What `getent passwd <uid>` prints in a container: the entry podman wrote, or exit 2.

    Podman writes the `--passwd-entry` verbatim, so the fake answers with that line. A container
    created without one has no entry for the uid, and getent exits 2 (T4.9b).
    """
    entry = container.passwd_entry
    if entry is not None and entry.split(":")[2] == uid:
        return ExecResult(0, f"{entry}\n", "")
    return ExecResult(2, "", "")


@dataclass
class FakePodman:
    """Fake of the Podman adapter. Construct with failure options, then drive it like the CLI."""

    info_result: PodmanInfo = DEFAULT_INFO
    exec_results: dict[tuple[str, ...], ExecResult] = field(default_factory=dict)
    exec_sequences: dict[tuple[str, ...], list[ExecResult]] = field(default_factory=dict)
    """Ordered answers for one argv: each call takes the first, so the same argv can answer
    differently before and after an operation (the explicit-package diff, T5.2). Once a queue is
    empty, `exec_results` answers the argv again."""
    unshare_results: list[ExecResult] = field(default_factory=list)
    fail_pull_with: str | None = None
    fail_pull_times: int | None = None
    fail_exec_with: str | None = None
    fail_ops: dict[str, str] = field(default_factory=dict)
    pull_digest_override: str | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    init_absent: bool = False
    """True makes the image lack `/sbin/init`: `cp_from` of it fails as podman's cp does (F3).
    Every other image path is answered from `files`, and `/sbin/init` is present by default."""
    tops: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    log_text: dict[str, str] = field(default_factory=dict)
    images: dict[str, ImageInfo] = field(default_factory=dict)
    containers: dict[str, _Container] = field(default_factory=dict)
    calls: list[FakeCall] = field(default_factory=list)
    exec_users: list[str | None] = field(default_factory=list)
    exec_calls: list[ExecCall] = field(default_factory=list)
    _pull_failures: int = 0
    _next: int = 1

    def __post_init__(self) -> None:
        if self.fail_pull_with is not None and self.fail_pull_with not in INJECTABLE_CODES:
            raise ValueError(f"fail_pull_with must be one of {sorted(INJECTABLE_CODES)}")
        if self.fail_exec_with is not None and self.fail_exec_with not in INJECTABLE_CODES:
            raise ValueError(f"fail_exec_with must be one of {sorted(INJECTABLE_CODES)}")

    def _record(self, op: str, *args: object) -> None:
        self.calls.append(FakeCall(op, args))

    def ops(self) -> list[str]:
        """Return the recorded operation names in call order."""
        return [call.op for call in self.calls]

    def _container(self, container_id: str) -> _Container:
        found = self.containers.get(container_id)
        if found is None:
            raise make("GINN-E-INTERNAL")
        return found

    def _take_op_failure(self, op: str) -> None:
        """Raise the code queued in `fail_ops` for `op`, once. The code takes no placeholders."""
        code = self.fail_ops.pop(op, None)
        if code is not None:
            raise make(code)

    def _take_pull_failure(self) -> bool:
        if self.fail_pull_with is None:
            return False
        if self.fail_pull_times is None:
            return True
        if self._pull_failures < self.fail_pull_times:
            self._pull_failures += 1
            return True
        return False

    def info(self) -> PodmanInfo:
        self._record("info")
        return self.info_result

    def image_exists(self, ref: str) -> bool:
        self._record("image_exists", ref)
        return ref in self.images

    def pull(self, ref_with_digest: str, *, retry_hook: RetryHook | None = None) -> ImageInfo:
        require_digest_ref(ref_with_digest)

        def attempt() -> ImageInfo:
            self._record("pull", ref_with_digest)
            if self._take_pull_failure():
                assert self.fail_pull_with is not None  # narrowed by _take_pull_failure
                raise _error(self.fail_pull_with, ref_with_digest)
            image = ImageInfo(
                id=hashlib.sha256(ref_with_digest.encode("utf-8")).hexdigest(),
                digest=self.pull_digest_override or ref_with_digest.partition("@")[2],
                repo_digests=(ref_with_digest,),
                size=_FAKE_IMAGE_SIZE,
                created=_FAKE_IMAGE_CREATED,
            )
            self.images[ref_with_digest] = image
            return image

        return with_network_retry(attempt, retry_hook)

    def image_inspect(self, ref: str) -> ImageInfo:
        self._record("image_inspect", ref)
        image = self.images.get(ref)
        if image is None:
            raise make("GINN-E-INTERNAL")
        return image

    def rmi(self, ref: str) -> None:
        self._record("rmi", ref)
        self._take_op_failure("rmi")
        if self.images.pop(ref, None) is None:
            raise make("GINN-E-INTERNAL")

    def create(self, spec: CreateSpec) -> str:
        self._record("create", spec.name)
        self._take_op_failure("create")
        spec.to_argv()  # validates the spec, including the privileged refusal (NFR-SEC-001)
        if any(c.name == spec.name for c in self.containers.values()):
            raise make("GINN-E-INTERNAL")
        container_id = f"{self._next:064x}"
        self._next += 1
        self.containers[container_id] = _Container(
            id=container_id,
            name=spec.name,
            image=spec.image_digest_ref,
            labels=dict(spec.labels),
            passwd_entry=spec.passwd_entry,
        )
        return container_id

    def start(self, container_id: str) -> None:
        self._record("start", container_id)
        self._take_op_failure("start")
        container = self._container(container_id)
        if container.state is ContainerState.RUNNING:
            raise make("GINN-E-INTERNAL")
        container.state = ContainerState.RUNNING
        container.pid = 10_000 + self._next
        container.exit_code = 0

    def stop(self, container_id: str, timeout_s: int) -> None:
        self._record("stop", container_id, timeout_s)
        self._take_op_failure("stop")
        container = self._container(container_id)
        if container.state is ContainerState.RUNNING:
            container.state = ContainerState.EXITED
            container.pid = 0

    def rm(self, container_id: str, force: bool) -> None:
        self._record("rm", container_id, force)
        self._take_op_failure("rm")
        container = self._container(container_id)
        if container.state is ContainerState.RUNNING and not force:
            raise make("GINN-E-INTERNAL")
        del self.containers[container_id]

    def _info_for(self, container: _Container) -> ContainerInfo:
        return ContainerInfo(
            id=container.id,
            name=container.name,
            image=container.image,
            state=container.state,
            pid=container.pid,
            exit_code=container.exit_code,
            started_at="",
            finished_at="",
            labels=dict(container.labels),
        )

    def inspect(self, container_id: str) -> ContainerInfo:
        self._record("inspect", container_id)
        self._take_op_failure("inspect")
        return self._info_for(self._container(container_id))

    def ps(self, label_filter: Mapping[str, str] | None = None) -> list[ContainerInfo]:
        self._record("ps", tuple(sorted((label_filter or {}).items())))
        wanted = dict(label_filter or {})
        matches: list[ContainerInfo] = []
        for container in self.containers.values():
            if all(container.labels.get(k) == v for k, v in wanted.items()):
                matches.append(self._info_for(container))
        return matches

    def _note_exec(
        self,
        op: str,
        container_id: str,
        argv: Sequence[str],
        user: str | None,
        env: Mapping[str, str] | None,
        workdir: str | None,
        pass_env: Sequence[str],
        tty: bool,
        interactive: bool,
        target: ExecTarget,
    ) -> None:
        """Keep the options of one exec call, as the adapter received them.

        The checks are those of cli.py (`_env_flags` and `_pass_flags`): every name is a plain
        name, and no name is given both a value and a pass-through. A bad call raises ValueError
        before anything is recorded.
        """
        values = dict(env or {})
        for key in [*values, *pass_env]:
            if _ENV_KEY.fullmatch(key) is None:
                raise ValueError(f"environment name {key!r} is not valid")
        both = sorted(set(values) & set(pass_env))
        if both:
            raise ValueError(
                f"environment name {both[0]!r} is given both a value and a pass-through"
            )
        self.exec_calls.append(
            ExecCall(
                op=op,
                container_id=container_id,
                argv=tuple(argv),
                user=user,
                env=values,
                pass_env=tuple(pass_env),
                workdir=workdir,
                tty=tty,
                interactive=interactive,
                target=target,
            )
        )

    def _exec(
        self,
        op: str,
        container_id: str,
        argv: Sequence[str],
    ) -> ExecResult:
        self._record(op, container_id, tuple(argv))
        container = self._container(container_id)
        if container.state is not ContainerState.RUNNING:
            raise make("GINN-E-INTERNAL")
        if self.fail_exec_with is not None:
            raise _error(self.fail_exec_with, container.image)
        queued = self.exec_sequences.get(tuple(argv))
        if queued:
            return queued.pop(0)
        canned = self.exec_results.get(tuple(argv))
        if canned is not None:
            return canned  # a test can inject any answer, a mismatched entry included
        if len(argv) == 3 and tuple(argv[:2]) == ("getent", "passwd"):
            return _getent_passwd(container, argv[2])
        if len(argv) == 3 and tuple(argv[:2]) == ("cat", "--"):
            return _cat(self.files, argv[2])
        if len(argv) == 5 and tuple(argv[:3]) == ("stat", "-c", "%u") and argv[3] == "--":
            # The drop-in has given the runtime dir to its realm user at boot (T7.28): owned.
            owner = _RUN_USER_DIR.fullmatch(argv[4])
            if owner is not None:
                return ExecResult(0, f"{owner.group(1)}\n", "")
        return ExecResult(0, "", "")

    def exec(
        self,
        container_id: str,
        argv: Sequence[str],
        *,
        target: ExecTarget,
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
        on_ack: Callable[[int], None] | None = None,
    ) -> ExecResult:
        """The limits are accepted for the protocol. A test injects a hang or an overflow itself.
        `stdout_tail` is accepted too: the fake answers with its whole canned output. `on_ack` gets
        the fake client pid once the exec has run, as the relay's ACK would (FR-122)."""
        self.exec_users.append(user)
        self._note_exec(
            "exec", container_id, argv, user, env, workdir, pass_env, tty, interactive, target
        )
        result = self._exec("exec", container_id, argv)
        if on_ack is not None:
            on_ack(FAKE_CLIENT_PID)
        if not capture:
            return ExecResult(result.exit_code, "", "")
        if (
            max_output is not None
            and len(result.stdout.encode("utf-8", "surrogateescape")) > max_output
        ):
            # What the adapter does: the run is cut at the cap, and the error names the cap.
            over = make("GINN-E-INTERNAL")
            over.details["reason"] = f"printed more than {max_output} bytes"
            raise over
        return result

    def exec_interactive(
        self,
        container_id: str,
        argv: Sequence[str],
        *,
        target: ExecTarget,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        tty: bool = False,
        interactive: bool = False,
        pass_env: Sequence[str] = (),
        on_spawn: Callable[[int], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        on_ack: Callable[[int], None] | None = None,
    ) -> int:
        self._note_exec(
            "exec_interactive",
            container_id,
            argv,
            user,
            env,
            workdir,
            pass_env,
            tty,
            interactive,
            target,
        )
        status = self._exec("exec_interactive", container_id, argv).exit_code
        if on_ack is not None:
            on_ack(FAKE_CLIENT_PID)
        return status

    def cp_from(self, container_id: str, src: str, dst: Path) -> None:
        self._record("cp_from", container_id, src, str(dst))
        self._container(container_id)
        if src == INIT_PATH and self.init_absent:
            raise _path_not_found()
        content = self.files.get(src, _FAKE_INIT if src == INIT_PATH else None)
        if content is None:
            raise _path_not_found()
        dst.write_bytes(content)

    def cp_into(self, container_id: str, src: Path, dst: str) -> None:
        """A file goes to `dst`. A directory goes into `dst`, under its own name, as podman cp does
        when `dst` is a directory: its files are then recorded at `dst/<name>/<relative path>`."""
        self._record("cp_into", container_id, dst)
        self._take_op_failure("cp_into")
        container = self._container(container_id)
        if src.is_dir():
            base = dst.rstrip("/") + "/" + src.name
            for path in sorted(src.rglob("*")):
                if path.is_file():
                    rel = path.relative_to(src).as_posix()
                    mode = path.stat().st_mode & 0o777
                    container.copied[f"{base}/{rel}"] = (path.read_bytes(), mode)
            return
        container.copied[dst] = (src.read_bytes(), src.stat().st_mode & 0o777)

    def top(self, container_id: str) -> list[tuple[int, int, str]]:
        self._record("top", container_id)
        self._container(container_id)
        return list(self.tops.get(container_id, []))

    def logs(self, container_id: str, tail: int) -> str:
        self._record("logs", container_id, tail)
        self._container(container_id)
        lines = self.log_text.get(container_id, "").splitlines(keepends=True)
        return "".join(lines[max(len(lines) - tail, 0) :])

    def unshare(self, argv: Sequence[str]) -> ExecResult:
        """Record the call. Canned results go in order. Once they run out, status 0, no output."""
        self._record("unshare", tuple(argv))
        if self.unshare_results:
            return self.unshare_results.pop(0)
        return ExecResult(0, "", "")
