# SPDX-License-Identifier: Apache-2.0
"""In-memory Podman adapter for unit tests (ADR-002, NFR-061).

FakePodman implements the `Podman` Protocol without a container runtime. It keeps images keyed by
digest reference and containers in a state machine: created, then running on start, then exited
on stop. Every call is recorded in `calls`. Tests inject failures and canned exec results.

Failures use catalogue codes: GINN-E-NETWORK and GINN-E-INTERNAL raise as the CLI does, and
GINN-E-IMAGE-UNVERIFIED for a digest pull. The retry rule matches cli.py, through the same helper.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, NamedTuple

from ginn.errors import GinnError, make
from ginn.podman.adapter import CreateSpec, RetryHook, require_digest_ref, with_network_retry
from ginn.podman.models import ContainerInfo, ContainerState, ExecResult, ImageInfo, PodmanInfo

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
_FAKE_IMAGE_CREATED: Final = "1970-01-01T00:00:00Z"


class FakeCall(NamedTuple):
    """One recorded call. Pulls are recorded once per attempt."""

    op: str
    args: tuple[object, ...]


@dataclass
class _Container:
    id: str
    name: str
    image: str
    labels: dict[str, str]
    state: ContainerState = ContainerState.CREATED
    pid: int = 0
    exit_code: int = 0


def _error(code: str, ref: str) -> GinnError:
    if code == "GINN-E-IMAGE-UNVERIFIED":
        return make(code, distro=ref.partition("@")[0])
    return make(code)


@dataclass
class FakePodman:
    """Fake of the Podman adapter. Construct with failure options, then drive it like the CLI."""

    info_result: PodmanInfo = DEFAULT_INFO
    exec_results: dict[tuple[str, ...], ExecResult] = field(default_factory=dict)
    unshare_results: list[ExecResult] = field(default_factory=list)
    fail_pull_with: str | None = None
    fail_pull_times: int | None = None
    fail_exec_with: str | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    tops: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    log_text: dict[str, str] = field(default_factory=dict)
    images: dict[str, ImageInfo] = field(default_factory=dict)
    containers: dict[str, _Container] = field(default_factory=dict)
    calls: list[FakeCall] = field(default_factory=list)
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
                digest=ref_with_digest.partition("@")[2],
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
        if self.images.pop(ref, None) is None:
            raise make("GINN-E-INTERNAL")

    def create(self, spec: CreateSpec) -> str:
        self._record("create", spec.name)
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
        )
        return container_id

    def start(self, container_id: str) -> None:
        self._record("start", container_id)
        container = self._container(container_id)
        if container.state is ContainerState.RUNNING:
            raise make("GINN-E-INTERNAL")
        container.state = ContainerState.RUNNING
        container.pid = 10_000 + self._next
        container.exit_code = 0

    def stop(self, container_id: str, timeout_s: int) -> None:
        self._record("stop", container_id, timeout_s)
        container = self._container(container_id)
        if container.state is ContainerState.RUNNING:
            container.state = ContainerState.EXITED
            container.pid = 0

    def rm(self, container_id: str, force: bool) -> None:
        self._record("rm", container_id, force)
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
        return self._info_for(self._container(container_id))

    def ps(self, label_filter: Mapping[str, str] | None = None) -> list[ContainerInfo]:
        self._record("ps", tuple(sorted((label_filter or {}).items())))
        wanted = dict(label_filter or {})
        matches: list[ContainerInfo] = []
        for container in self.containers.values():
            if all(container.labels.get(k) == v for k, v in wanted.items()):
                matches.append(self._info_for(container))
        return matches

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
        return self.exec_results.get(tuple(argv), ExecResult(0, "", ""))

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
        result = self._exec("exec", container_id, argv)
        if not capture:
            return ExecResult(result.exit_code, "", "")
        return result

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
        return self._exec("exec_interactive", container_id, argv).exit_code

    def cp_from(self, container_id: str, src: str, dst: Path) -> None:
        self._record("cp_from", container_id, src, str(dst))
        self._container(container_id)
        content = self.files.get(src)
        if content is None:
            raise make("GINN-E-INTERNAL")
        dst.write_bytes(content)

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
