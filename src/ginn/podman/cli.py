# SPDX-License-Identifier: Apache-2.0
"""The podman command line behind the Podman adapter (ADR-002, IR-020, NFR-042).

Every call runs one `podman` process with an argv list (NFR-SEC-005). Every call carries the
global flags --remote=false, --root, --runroot, --tmpdir and --runtime, so ginn's storage, tmpdir
and OCI runtime never fall back to the user's own Podman (DR-010, ADR-004). --remote=false is a
guard of its own: podman(1) makes --remote default to true when CONTAINER_HOST is set.

The subprocess environment is an allowlist (CR-006, DR-010), not a copy of the parent's: PATH,
HOME, TERM, USER, LOGNAME, LANG, LC_*, the proxy variables, plus ginn's pins. XDG_RUNTIME_DIR is
the runtime root, CONTAINERS_CONF and CONTAINERS_STORAGE_CONF are ginn's files (written by
OwnedPaths.ensure_config), and TMPDIR is the image tmpdir on disk. Nothing else reaches podman, so
a user's containers.conf, storage settings and remote-connection variables are not read. HOME does
pass through, so podman's registries.conf and policy.json lookups can still reach the user's own
files; see `_env`.

Output that ginn parses is requested with `--format json`. Before each run, the full argv is
passed to `verbose` as one shell-quoted line, so a user can copy it and run it by hand (NFR-042).

Failures map to catalogue codes here and nowhere else (NFR-040):
- exit 125 with a registry or network message: GINN-E-NETWORK;
- a digest pull that the registry rejects: GINN-E-IMAGE-UNVERIFIED (FR-011);
- a runtime root that is missing or unsafe: GINN-E-HOST-NOT-SET-UP or GINN-E-RUNTIME-DIR-UNSAFE,
  from the shared check in `ginn.store.paths` (DR-010, ADR-004);
- anything else: GINN-E-INTERNAL, with the decisive stderr line in details.
Exit 125 from `exec` is podman's own failure and is never returned as the command's status.
"""

import json
import os
import re
import shlex
import stat
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
from ginn.store.paths import OwnedPaths, check_runtime_dir

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
_PASSTHROUGH_ENV: Final = frozenset(
    {
        "PATH",
        "HOME",
        "TERM",
        "USER",
        "LOGNAME",
        "LANG",
        # Without these, a pull behind a proxy fails and is reported as GINN-E-NETWORK.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_PASSTHROUGH_PREFIXES: Final = ("LC_",)
"""The only parent environment variables that reach podman (CR-006). Everything else is dropped:
CONTAINER_HOST, CONTAINER_CONNECTION, CONTAINER_SSHKEY, STORAGE_DRIVER, STORAGE_OPTS,
CONTAINERS_CONF_OVERRIDE and the rest of the user's Podman configuration. The proxy variables are
kept (see `_env`)."""


def _passes(key: str) -> bool:
    return key in _PASSTHROUGH_ENV or key.startswith(_PASSTHROUGH_PREFIXES)


def _lstat_mode(path: Path) -> int | None:
    """The mode of `path` as lstat sees it, so a symbolic link is not followed. None if absent."""
    try:
        return os.lstat(path).st_mode
    except OSError:
        return None


def _is_regular_file(path: Path) -> bool:
    mode = _lstat_mode(path)
    return mode is not None and stat.S_ISREG(mode)


def _is_real_dir(path: Path) -> bool:
    mode = _lstat_mode(path)
    return mode is not None and stat.S_ISDIR(mode)


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

    Build it with `from_paths`, the documented constructor. It takes Owned Paths and derives every
    location from them. `history` keeps the full argv of every call, in order, for tests and
    diagnostics. `tightened` keeps one line for each owned directory that a call tightened from a
    wider mode (FR-025), in the order they happened. A CLI layer is planned to print them; this
    class prints nothing.
    """

    @classmethod
    def from_paths(
        cls,
        paths: OwnedPaths,
        *,
        binary: str = "podman",
        oci_runtime: str = DEFAULT_OCI_RUNTIME,
        cdi_spec_dir: Path | None = None,
        verbose: Callable[[str], None] | None = None,
    ) -> "PodmanCli":
        """Build the adapter from Owned Paths (ADR-004, DR-010). Callers use this constructor.

        Root and runroot come from the data and runtime roots, the tmpdir is the runtime's
        podman-tmp, TMPDIR is the image tmpdir on disk, and CONTAINERS_CONF and
        CONTAINERS_STORAGE_CONF are ginn's own files. Each call first checks the runtime root,
        then makes the owned directories and ginn's config files with `OwnedPaths.ensure()` and
        `OwnedPaths.ensure_config()`. Both are idempotent, so a fresh tree needs no prior setup
        and no caller has to call them. The runtime root itself is never created here (ADR-004).
        """
        return cls(
            paths.podman_root,
            paths.podman_runroot,
            tmpdir=paths.podman_tmpdir,
            image_tmp=paths.image_tmp,
            containers_conf=paths.podman_containers_conf,
            storage_conf=paths.podman_storage_conf,
            runtime_root=paths.runtime,
            binary=binary,
            oci_runtime=oci_runtime,
            cdi_spec_dir=cdi_spec_dir,
            verbose=verbose,
            owned_paths=paths,
        )

    def __init__(
        self,
        root: Path,
        runroot: Path,
        *,
        tmpdir: Path,
        image_tmp: Path,
        containers_conf: Path,
        storage_conf: Path,
        runtime_root: Path,
        binary: str = "podman",
        oci_runtime: str = DEFAULT_OCI_RUNTIME,
        cdi_spec_dir: Path | None = None,
        verbose: Callable[[str], None] | None = None,
        owned_paths: OwnedPaths | None = None,
    ) -> None:
        """Prefer `from_paths`. This constructor takes every location explicitly.

        `tmpdir` is required: without it podman uses the user's own libpod tmpdir. `image_tmp` is
        required for the same reason: without it podman downloads into /var/tmp (CR-006).

        `containers_conf` and `storage_conf` are the files CONTAINERS_CONF and
        CONTAINERS_STORAGE_CONF point to. `OwnedPaths.ensure_config()` writes them. This
        constructor does not write them itself, so a missing file is refused. `from_paths` passes
        `owned_paths`, and then each call writes the missing files first.

        `runtime_root` is ginn's single runtime root, /run/ginn/<uid> (ADR-004). Podman reads
        XDG_RUNTIME_DIR for its rootless state, so every call gets this value, whatever the
        caller's session exports.
        """
        self.root = root
        self.runroot = runroot
        self.tmpdir = tmpdir
        self.image_tmp = image_tmp
        self.containers_conf = containers_conf
        self.storage_conf = storage_conf
        self.runtime_root = runtime_root
        self.binary = binary
        self.oci_runtime = require_arg(oci_runtime, "oci runtime")
        self.cdi_spec_dir = cdi_spec_dir
        self.verbose = verbose
        self.owned_paths = owned_paths
        self.history: list[list[str]] = []
        self.tightened: list[str] = []

    def _global_flags(self) -> list[str]:
        """Global flags come before the subcommand. --cdi-spec-dir is global (S-06)."""
        flags = [
            self.binary,
            "--remote=false",
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
        """The environment of one podman call: the allowlist, plus ginn's pins (ADR-004, CR-006).

        XDG_RUNTIME_DIR is the runtime root, so podman's rootless state (and any default tmpdir it
        might pick) lands in ginn's directory, not the session's /run/user/<uid>. CONTAINERS_CONF
        and CONTAINERS_STORAGE_CONF replace the system and user files (containers.conf(5)) and
        point at the files ensure_config writes. TMPDIR is the image tmpdir on disk. Every other
        parent variable is dropped, including CONTAINERS_CONF_OVERRIDE, which podman loads last
        even when CONTAINERS_CONF is set.

        HOME passes through, so podman still resolves registries.conf and policy.json by its own
        defaults, and those include the user's ~/.config/containers/. ginn does not pin them here.
        Pinning CONTAINERS_REGISTRIES_CONF and the signature policy path is T4.4 work.

        Proxy decision. The proxy variables pass through here only so that a pull behind a proxy
        works (see `_PASSTHROUGH_ENV`). They are not stored in container config. T4.1 passes
        `--http-proxy=false` on `podman create`, and T4.3 injects the proxy variables at exec and
        launch from the session env. So the container config holds no proxy secrets and no stale
        proxy, and a changed proxy applies at the next exec or launch.
        """
        env = {key: value for key, value in os.environ.items() if _passes(key)}
        env["XDG_RUNTIME_DIR"] = str(self.runtime_root)
        env["CONTAINERS_CONF"] = str(self.containers_conf)
        env["CONTAINERS_STORAGE_CONF"] = str(self.storage_conf)
        env["TMPDIR"] = str(self.image_tmp)
        return env

    def _require_config(self) -> None:
        """Refuse to run podman unless ginn's config files and the image tmpdir are in place.

        Podman 5.8 exits 1 when a path named by CONTAINERS_CONF or CONTAINERS_STORAGE_CONF does
        not exist (checked 2026-10-12, ADR-004), so every call would fail. Each config path must be
        a regular file under lstat, so a symbolic link is refused: podman would read its target.
        The image tmpdir must be a real directory, for the same reason. A failure here is
        GINN-E-INTERNAL, and the detail names the path. `from_paths` makes the config files and the
        owned directories before this check (see `_prepare`), so only the explicit constructor
        reaches it with missing files.
        """
        for path in (self.containers_conf, self.storage_conf):
            if not _is_regular_file(path):
                raise _internal(
                    f"podman config {path} is missing or not a regular file; "
                    "OwnedPaths.ensure_config() writes it"
                )
        if not _is_real_dir(self.image_tmp):
            raise _internal(
                f"image tmpdir {self.image_tmp} is missing or not a real directory; "
                "OwnedPaths.ensure() creates it"
            )

    def _prepare(self, args: Sequence[str]) -> list[str]:
        """Build the full argv, record it, and echo it to `verbose` before it runs (NFR-042).

        An adapter from `from_paths` first checks the runtime root, then makes the owned
        directories and ginn's config files, so a call never fails only because they are not
        there yet. The runtime check runs first and is the same check `OwnedPaths.from_env`
        makes. `ensure()` never creates or changes the runtime root (it refuses a missing one),
        so this check is what refuses a wrong owner or mode (ADR-004). Both steps are
        idempotent: what is already right is not rewritten. A directory wider than 0700 is
        tightened, and the change is appended to `tightened` (FR-025). It is not printed here.
        """
        if self.owned_paths is not None:
            # The process uid. OwnedPaths does not keep the uid that from_env was given, and
            # PodmanCli has no uid of its own, so there is no other source to use here.
            check_runtime_dir(self.owned_paths.runtime, os.getuid())
            try:
                self.owned_paths.ensure(on_tighten=self.tightened.append)
                self.owned_paths.ensure_config()
            except OSError as exc:
                raise _internal(f"cannot prepare Owned Paths for podman: {exc}") from exc
        self._require_config()
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
