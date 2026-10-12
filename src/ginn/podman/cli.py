# SPDX-License-Identifier: Apache-2.0
"""The podman command line behind the Podman adapter (ADR-002, IR-020, NFR-042).

Every call runs one `podman` process with an argv list (NFR-SEC-005). Every call carries the
global flags --root, --runroot, --tmpdir and --runtime, so ginn's storage, tmpdir and OCI runtime
never fall back to the user's own Podman (DR-010, ADR-004). The subprocess environment pins
CONTAINERS_CONF and CONTAINERS_STORAGE_CONF to ginn's files, so the user's configuration is not
read. Output that ginn parses is requested with `--format json`. Before each run, the full argv is
passed to `verbose` as one shell-quoted line, so a user can copy it and run it by hand (NFR-042).

Failures map to catalogue codes here and nowhere else (NFR-040):
- exit 125 with a registry or network message: GINN-E-NETWORK;
- a digest pull that the registry rejects: GINN-E-IMAGE-UNVERIFIED (FR-011);
- anything else: GINN-E-INTERNAL, with the decisive stderr line in details.
Exit 125 from `exec` is podman's own failure and is never returned as the command's status.
"""

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from ginn.errors import GinnError, make
from ginn.podman.adapter import (
    CreateSpec,
    RetryHook,
    require_arg,
    require_digest_ref,
    with_network_retry,
)
from ginn.podman.models import ContainerInfo, ExecResult, ImageInfo, PodmanInfo

_NETWORK_PATTERNS: Final = (
    re.compile(r"dial tcp", re.IGNORECASE),
    re.compile(r"TLS handshake timeout", re.IGNORECASE),
    re.compile(r"no route to host", re.IGNORECASE),
    re.compile(r"temporary failure in name resolution", re.IGNORECASE),
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"i/o timeout", re.IGNORECASE),
    re.compile(r"\bEOF\b"),
    re.compile(r"\b(?:503|429)\b"),
)
_DIGEST_PATTERNS: Final = (
    re.compile(r"manifest unknown", re.IGNORECASE),
    re.compile(r"digest did not match", re.IGNORECASE),
    re.compile(r"image not known", re.IGNORECASE),
)
_ENV_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NETWORK_EXIT: Final = 125
PODMAN_ERROR_EXIT: Final = 125
"""Exit status podman uses for its own failures (`podman exec` included)."""
DEFAULT_OCI_RUNTIME: Final = "crun"
"""OCI runtime passed as `--runtime` on every call (IR-020, ADR-004). crun is the runtime the
spikes validate, and keep-groups works only with it."""


def _matches(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in patterns)


def _decisive_line(stderr: str) -> str:
    """Return the first `Error:` line of stderr, else the first non-empty line."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("Error:"):
            return line
    return lines[0] if lines else ""


def _internal(detail: str) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details["detail"] = detail
    return err


def _exit_status(returncode: int) -> int:
    """Map a signal return code (negative) to 128 + signal, the shell convention."""
    return 128 - returncode if returncode < 0 else returncode


def _env_flags(env: Mapping[str, str] | None) -> list[str]:
    flags: list[str] = []
    values = env or {}
    for key in sorted(values):
        if _ENV_KEY.fullmatch(key) is None:
            raise ValueError(f"environment name {key!r} is not valid")
        flags += ["--env", f"{key}={values[key]}"]
    return flags


def _objects(op: str, obj: object) -> list[Mapping[str, object]]:
    """Return the objects of a JSON list. A null list is empty."""
    if obj is None:
        return []
    if not isinstance(obj, list):
        raise _internal(f"{op}: expected a JSON list")
    items: list[Mapping[str, object]] = []
    for item in obj:
        if not isinstance(item, Mapping):
            raise _internal(f"{op}: expected a JSON object in the list")
        items.append(item)
    return items


def _first(op: str, obj: object) -> Mapping[str, object]:
    items = _objects(op, obj)
    if not items:
        raise _internal(f"{op}: empty result")
    return items[0]


class PodmanCli:
    """Implements the Podman adapter with the podman command line.

    `history` keeps the full argv of every call, in order, for tests and diagnostics.
    """

    def __init__(
        self,
        root: Path,
        runroot: Path,
        *,
        tmpdir: Path,
        containers_conf: Path,
        storage_conf: Path,
        runtime_root: Path,
        binary: str = "podman",
        oci_runtime: str = DEFAULT_OCI_RUNTIME,
        cdi_spec_dir: Path | None = None,
        verbose: Callable[[str], None] | None = None,
    ) -> None:
        """`tmpdir` is required: without it podman uses the user's own libpod tmpdir.

        `containers_conf` and `storage_conf` are the files CONTAINERS_CONF and
        CONTAINERS_STORAGE_CONF point to. The files are not written yet. Until the task that
        renders them lands, podman gets paths that do not exist (see the TODO below).

        `runtime_root` is ginn's single runtime root, /run/ginn/<uid> (ADR-004). Podman reads
        XDG_RUNTIME_DIR for its rootless state, so every call gets this value, whatever the
        caller's session exports.
        """
        self.root = root
        self.runroot = runroot
        self.tmpdir = tmpdir
        self.containers_conf = containers_conf
        self.storage_conf = storage_conf
        self.runtime_root = runtime_root
        self.binary = binary
        self.oci_runtime = require_arg(oci_runtime, "oci runtime")
        self.cdi_spec_dir = cdi_spec_dir
        self.verbose = verbose
        self.history: list[list[str]] = []

    def _global_flags(self) -> list[str]:
        """Global flags come before the subcommand. --cdi-spec-dir is global (S-06)."""
        flags = [
            self.binary,
            "--root",
            str(self.root),
            "--runroot",
            str(self.runroot),
            "--tmpdir",
            str(self.tmpdir),
            "--runtime",
            self.oci_runtime,
        ]
        if self.cdi_spec_dir is not None:
            flags += ["--cdi-spec-dir", str(self.cdi_spec_dir)]
        return flags

    def _env(self) -> dict[str, str]:
        """The environment of one podman call (ADR-004).

        XDG_RUNTIME_DIR is set to ginn's runtime root, so podman's rootless state (and any default
        tmpdir it might pick) lands in ginn's directory, not the session's /run/user/<uid>.
        CONTAINERS_CONF replaces the system and user containers.conf files (containers.conf(5)).
        CONTAINERS_CONF_OVERRIDE is removed, because podman loads that file last even when
        CONTAINERS_CONF is set, which would bring the user's settings back.

        TODO(T4.4): the first task that drives real podman through PodmanCli must write these two
        files under the config root, or the pins point at missing files. Podman's docs say builtin
        defaults apply when no containers.conf exists, but that is not verified for
        CONTAINERS_CONF with a missing file. Verify it in that task.
        """
        env = dict(os.environ)
        env.pop("CONTAINERS_CONF_OVERRIDE", None)
        env["XDG_RUNTIME_DIR"] = str(self.runtime_root)
        env["CONTAINERS_CONF"] = str(self.containers_conf)
        env["CONTAINERS_STORAGE_CONF"] = str(self.storage_conf)
        return env

    def _prepare(self, args: Sequence[str]) -> list[str]:
        """Build the full argv, record it, and echo it to `verbose` before it runs (NFR-042)."""
        argv = [*self._global_flags(), *args]
        self.history.append(argv)
        if self.verbose is not None:
            self.verbose(shlex.join(argv))
        return argv

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = self._prepare(args)
        try:
            # Fixed argv list, no shell. The binary is resolved from PATH by design.
            return subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                check=False,
                errors="replace",
                env=self._env(),
            )
        except OSError as exc:
            raise _internal(f"cannot run {self.binary}: {exc.strerror or exc}") from exc

    def _expect(
        self,
        op: str,
        result: subprocess.CompletedProcess[str],
        *,
        pull_ref: str | None = None,
    ) -> None:
        if result.returncode == 0:
            return
        raise self._failure(op, result, pull_ref=pull_ref)

    @staticmethod
    def _failure(
        op: str,
        result: subprocess.CompletedProcess[str],
        *,
        pull_ref: str | None = None,
    ) -> GinnError:
        stderr = result.stderr or ""
        if pull_ref is not None and _matches(_DIGEST_PATTERNS, stderr):
            err = make("GINN-E-IMAGE-UNVERIFIED", distro=pull_ref.partition("@")[0])
        elif result.returncode == _NETWORK_EXIT and _matches(_NETWORK_PATTERNS, stderr):
            err = make("GINN-E-NETWORK")
        else:
            err = make("GINN-E-INTERNAL")
        err.details.update({"op": op, "exit": result.returncode, "stderr": _decisive_line(stderr)})
        return err

    @staticmethod
    def _json(op: str, result: subprocess.CompletedProcess[str]) -> object:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise _internal(f"{op}: stdout is not JSON") from exc

    def info(self) -> PodmanInfo:
        result = self._run(["info", "--format", "json"])
        self._expect("info", result)
        obj = self._json("info", result)
        if not isinstance(obj, Mapping):
            raise _internal("info: expected a JSON object")
        return PodmanInfo.from_info(obj)

    def image_exists(self, ref: str) -> bool:
        result = self._run(["image", "exists", require_arg(ref, "image")])
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise self._failure("image exists", result)

    def pull(self, ref_with_digest: str, *, retry_hook: RetryHook | None = None) -> ImageInfo:
        require_digest_ref(ref_with_digest)

        def attempt() -> ImageInfo:
            result = self._run(["pull", "--quiet", ref_with_digest])
            self._expect("pull", result, pull_ref=ref_with_digest)
            return self.image_inspect(ref_with_digest)

        return with_network_retry(attempt, retry_hook)

    def image_inspect(self, ref: str) -> ImageInfo:
        result = self._run(["image", "inspect", "--format", "json", require_arg(ref, "image")])
        self._expect("image inspect", result)
        return ImageInfo.from_inspect(_first("image inspect", self._json("image inspect", result)))

    def rmi(self, ref: str) -> None:
        result = self._run(["rmi", require_arg(ref, "image")])
        self._expect("rmi", result)

    def create(self, spec: CreateSpec) -> str:
        result = self._run(spec.to_argv())
        self._expect("create", result)
        container_id = result.stdout.strip()
        if not container_id:
            raise _internal("create: podman printed no container id")
        return container_id

    def start(self, container_id: str) -> None:
        result = self._run(["start", require_arg(container_id, "container")])
        self._expect("start", result)

    def stop(self, container_id: str, timeout_s: int) -> None:
        if timeout_s < 0:
            raise ValueError("timeout_s must not be negative")
        args = ["stop", "--time", str(timeout_s), require_arg(container_id, "container")]
        self._expect("stop", self._run(args))

    def rm(self, container_id: str, force: bool) -> None:
        args = ["rm", *(["--force"] if force else []), require_arg(container_id, "container")]
        self._expect("rm", self._run(args))

    def inspect(self, container_id: str) -> ContainerInfo:
        args = ["container", "inspect", "--format", "json", require_arg(container_id, "container")]
        result = self._run(args)
        self._expect("container inspect", result)
        obj = _first("container inspect", self._json("container inspect", result))
        return ContainerInfo.from_inspect(obj)

    def ps(self, label_filter: Mapping[str, str] | None = None) -> list[ContainerInfo]:
        args = ["ps", "--all", "--format", "json"]
        for key, value in sorted((label_filter or {}).items()):
            args += ["--filter", f"label={require_arg(key, 'label')}={value}"]
        result = self._run(args)
        self._expect("ps", result)
        items = _objects("ps", self._json("ps", result))
        return [ContainerInfo.from_ps(item) for item in items]

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
        """Run one command in the container and return its status and output.

        Exit 125 is podman's own failure status, so it is mapped to a catalogue error through
        `_failure`. A command that exits 125 itself cannot be told apart from that, and is
        reported the same way; EXEC_GINN_FAILURE already reserves 125 for ginn's failures. Exit
        126 and 127 are passed through: podman reports them for a command that cannot be run in
        a container that did start. With `capture=False` the output goes to the terminal, so
        stderr cannot be read and the status is returned as it is.
        """
        if not capture:
            status = self.exec_interactive(
                container_id,
                argv,
                user=user,
                env=env,
                workdir=workdir,
                tty=tty,
                interactive=interactive,
            )
            return ExecResult(status, "", "")
        args = self._exec_args(container_id, argv, user, env, workdir, tty, interactive)
        result = self._run(args)
        if result.returncode == PODMAN_ERROR_EXIT:
            raise self._failure("exec", result)
        return ExecResult(result.returncode, result.stdout, result.stderr)

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
        args = self._exec_args(container_id, argv, user, env, workdir, tty, interactive)
        full = self._prepare(args)
        try:
            # Fixed argv list, no shell. stdio is inherited so the user's terminal is used.
            completed = subprocess.run(full, check=False, env=self._env())  # noqa: S603
        except OSError as exc:
            raise _internal(f"cannot run {self.binary}: {exc.strerror or exc}") from exc
        return _exit_status(completed.returncode)

    @staticmethod
    def _exec_args(
        container_id: str,
        argv: Sequence[str],
        user: str | None,
        env: Mapping[str, str] | None,
        workdir: str | None,
        tty: bool,
        interactive: bool,
    ) -> list[str]:
        if not argv:
            raise ValueError("exec needs a command")
        flags: list[str] = []
        if tty:
            flags.append("--tty")
        if interactive:
            flags.append("--interactive")
        if user is not None:
            flags += ["--user", require_arg(user, "user")]
        flags += _env_flags(env)
        if workdir is not None:
            flags += ["--workdir", workdir]
        # `--` ends the options, so a command argument can start with '-'.
        return ["exec", *flags, "--", require_arg(container_id, "container"), *argv]

    def cp_from(self, container_id: str, src: str, dst: Path) -> None:
        # An absolute destination cannot start with '-', so podman never reads it as an option.
        if not dst.is_absolute():
            raise ValueError(f"cp destination must be an absolute path (NFR-SEC-005): {dst}")
        args = ["cp", f"{require_arg(container_id, 'container')}:{src}", str(dst)]
        self._expect("cp", self._run(args))

    def top(self, container_id: str) -> list[tuple[int, int, str]]:
        result = self._run(["top", require_arg(container_id, "container"), "pid,rss,comm"])
        self._expect("top", result)
        rows: list[tuple[int, int, str]] = []
        for line in result.stdout.splitlines()[1:]:
            if not line.strip():
                continue
            parts = line.split(None, 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                raise _internal(f"top: unexpected row {line!r}")
            rows.append((int(parts[0]), int(parts[1]), parts[2].strip()))
        return rows

    def logs(self, container_id: str, tail: int) -> str:
        if tail < 0:
            raise ValueError("tail must not be negative")
        args = ["logs", "--tail", str(tail), require_arg(container_id, "container")]
        result = self._run(args)
        self._expect("logs", result)
        return result.stdout
