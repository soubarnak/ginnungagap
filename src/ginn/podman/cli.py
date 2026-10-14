# SPDX-License-Identifier: Apache-2.0
"""The podman command line behind the Podman adapter (ADR-002, IR-020, NFR-042).

Every call runs one `podman` process with an argv list (NFR-SEC-005). Every call carries the
global flags --remote=false, --root, --runroot, --tmpdir and --runtime, so ginn's storage, tmpdir
and OCI runtime never fall back to the user's own Podman (DR-010, ADR-004). --remote=false is a
guard of its own: podman(1) makes --remote default to true when CONTAINER_HOST is set. The CLI
adapter also carries --cdi-spec-dir, the owned CDI directory, on every call, so `start` resolves a
CDI device as `create` does (FR-069, ADR-012). A create that names its own directory passes that
one instead, once.

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
- a GPU's CDI devices that podman cannot resolve: GINN-E-NVIDIA-CDI (FR-069);
- anything else: GINN-E-INTERNAL, with the decisive stderr line in details.
Exit 125 from `exec` is podman's own failure and is never returned as the command's status.
"""

import json
import os
import re
import selectors
import shlex
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from types import FrameType
from typing import IO, Final, NamedTuple

from ginn.errors import GinnError, make
from ginn.ops.retry import NETWORK_PATTERNS
from ginn.podman.adapter import (
    PATH_NOT_FOUND_REASON,
    PODMAN_ERROR_EXIT,
    CreateSpec,
    ExecTarget,
    RetryHook,
    require_arg,
    require_digest_ref,
    with_network_retry,
)
from ginn.podman.models import ContainerInfo, ExecResult, ImageInfo, PodmanInfo
from ginn.runtime import exec_relay
from ginn.store.paths import OwnedPaths, check_runtime_dir

_IN_USE_PATTERNS: Final = (re.compile(r"image is in use by a container", re.IGNORECASE),)
_PATH_NOT_FOUND_PATTERNS: Final = (re.compile(r"could not be found on container", re.IGNORECASE),)
"""podman's wording when a `cp` path is absent in the container (F3): `"<path>" could not be found
on container <name>: no such file or directory`."""
_DIGEST_PATTERNS: Final = (
    re.compile(r"manifest unknown", re.IGNORECASE),
    re.compile(r"digest did not match", re.IGNORECASE),
    re.compile(r"image not known", re.IGNORECASE),
)
_ENV_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CDI_PATTERNS: Final = (re.compile(r"unresolvable CDI devices", re.IGNORECASE),)
_NETWORK_EXIT: Final = 125
STDERR_TAIL_BYTES: Final = 65536
"""The most stderr a bounded exec keeps, from its end: the error line of a failed command."""
STDOUT_TAIL_BYTES: Final = 65536
"""The most stdout a bounded exec keeps when the caller asks for its tail (`stdout_tail`). A step
whose stdout is not parsed (a build, an install) keeps only this: its output cannot fill memory."""
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


class _Output(NamedTuple):
    """What a bounded read kept: the stdout bytes, the stderr tail, and the bound that ended it."""

    stdout: bytearray
    stderr: bytearray
    reason: str | None


def _left(deadline: float | None) -> float | None:
    """The seconds left before `deadline`, or None when there is no deadline."""
    return None if deadline is None else max(deadline - time.monotonic(), 0.0)


def _read_bounded(
    out: IO[bytes],
    err: IO[bytes],
    *,
    timeout_s: float | None,
    deadline: float | None,
    max_output: int | None,
    stdout_tail: bool,
) -> _Output:
    """Read both streams until EOF, or until a bound stops the run (FR-044). Shared by both routes.

    The deadline is checked on every pass of the loop, so a process that prints without pause still
    times out. The two streams are read through a selector, so a descriptor above 1024 works too.
    Each stream keeps only its last `STDERR_TAIL_BYTES` or `STDOUT_TAIL_BYTES` when `stdout_tail` is
    set. The byte count against `max_output` is kept in full either way. The caller stops the
    client when a reason comes back.
    """
    out_limit = STDOUT_TAIL_BYTES if stdout_tail else None
    buffer = bytearray()
    tail = bytearray()
    printed = 0
    reason: str | None = None
    selector = selectors.DefaultSelector()
    try:
        selector.register(out, selectors.EVENT_READ, "stdout")
        selector.register(err, selectors.EVENT_READ, "stderr")
        while reason is None and selector.get_map():
            if deadline is not None and time.monotonic() >= deadline:
                reason = f"timed out after {timeout_s} s"
                break
            events = selector.select(_left(deadline))
            if not events:
                reason = f"timed out after {timeout_s} s"
                break
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    printed += len(chunk)
                    buffer.extend(chunk)
                    if out_limit is not None:
                        del buffer[: max(len(buffer) - out_limit, 0)]
                    if max_output is not None and printed > max_output:
                        reason = f"printed more than {max_output} bytes"
                        break
                else:
                    tail.extend(chunk)
                    del tail[: max(len(tail) - STDERR_TAIL_BYTES, 0)]
    finally:
        selector.close()
    return _Output(buffer, tail, reason)


def _bounded_failure(reason: str) -> GinnError:
    """GINN-E-INTERNAL for a run that a bound stopped, with the reason in its details."""
    err = _internal(f"exec {reason}")
    err.details["reason"] = reason
    return err


_BOUNDED_DECODE: Final = "surrogateescape"
"""How a bounded exec decodes its output, so encoding it back gives the exact bytes (NFR-042)."""
_UNBOUNDED_DECODE: Final = "replace"
"""How an unbounded exec decodes its output, as `_run` does through subprocess."""


def _completed(
    argv: Sequence[str], read: _Output, returncode: int, decode: str
) -> subprocess.CompletedProcess[str]:
    """The result of a finished run, its output decoded as `decode` says (UTF-8)."""
    text = bytes(read.stdout).decode("utf-8", errors=decode)
    errors = bytes(read.stderr).decode("utf-8", errors=decode)
    return subprocess.CompletedProcess(argv, returncode, text, errors)


def _do_nothing(signum: int, frame: FrameType | None) -> None:
    """The handler that the guard installs for SIGINT and SIGQUIT. It returns at once."""


@contextmanager
def _parent_ignores_interrupts() -> Iterator[None]:
    """Hold SIGINT and SIGQUIT in a do-nothing handler while a child without a TTY runs.

    Without a TTY the terminal sends both signals to the whole foreground group, so a Ctrl-C would
    end ginn with a traceback instead of returning podman's status. The guard does not set SIG_IGN.
    An ignored signal survives execve, so podman would start with SIGINT and SIGQUIT ignored. A
    caught signal is reset to its default by the kernel at exec, so podman starts with the default
    dispositions, as system(3) arranges. The handler returns at once, and Python retries the
    interrupted wait (PEP 475), so ginn raises no KeyboardInterrupt and returns podman's status.
    Signal handlers can be set only from the main thread, so another thread runs with the handlers
    it already has. The previous handlers come back in `finally`, whatever the child did.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old_int = signal.getsignal(signal.SIGINT)
    old_quit = signal.getsignal(signal.SIGQUIT)
    signal.signal(signal.SIGINT, _do_nothing)
    signal.signal(signal.SIGQUIT, _do_nothing)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL if old_int is None else old_int)
        signal.signal(signal.SIGQUIT, signal.SIG_DFL if old_quit is None else old_quit)


def _without_dir(path_value: str, directory: Path) -> str:
    """The PATH with every entry that is `directory` removed (ginn's bin directory, FR-047).

    A launcher in ginn's bin directory must not shadow a program that ginn runs, so podman is
    always run from the PATH of the session without that directory.
    """
    target = os.path.normpath(str(directory))
    kept = [entry for entry in path_value.split(os.pathsep) if os.path.normpath(entry) != target]
    return os.pathsep.join(kept)


def _env_flags(env: Mapping[str, str] | None) -> list[str]:
    flags: list[str] = []
    values = env or {}
    for key in sorted(values):
        if _ENV_KEY.fullmatch(key) is None:
            raise ValueError(f"environment name {key!r} is not valid")
        flags += ["--env", f"{key}={values[key]}"]
    return flags


def _pass_flags(names: Sequence[str], env: Mapping[str, str] | None) -> list[str]:
    """`--env NAME` for each name, so podman copies the value from its own environment.

    The value never reaches the argv, so a proxy credential is not in /proc/<pid>/cmdline (ADR-004,
    ADR-010). A name that `env` also sets is refused, because podman would take one of the two.
    """
    values = env or {}
    flags: list[str] = []
    for key in sorted(names):
        if _ENV_KEY.fullmatch(key) is None:
            raise ValueError(f"environment name {key!r} is not valid")
        if key in values:
            raise ValueError(f"environment name {key!r} is given both a value and a pass-through")
        flags += ["--env", key]
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

        `cdi_spec_dir` defaults to the owned CDI directory, `paths.cdi`, which every call names
        (FR-069, S-06). A caller passes another directory only to override it.
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
            cdi_spec_dir=paths.cdi if cdi_spec_dir is None else cdi_spec_dir,
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
        cgroup_file: Path = exec_relay.PROC_CGROUP,
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
        self.cgroup_file = cgroup_file
        self.history: list[list[str]] = []
        self.tightened: list[str] = []

    def global_argv(self) -> list[str]:
        """The binary and the global flags that every call of this adapter carries (FR-081).

        `ginn-wait` runs `podman stop` with these, so the supervised stop uses the same storage,
        tmpdir and runtime as the adapter. The list starts with the binary.
        """
        return self._global_flags()

    def _global_flags(self, per_call: Sequence[str] = ()) -> list[str]:
        """Global flags come before the subcommand. --cdi-spec-dir is global (S-06).

        `per_call` are the flags of one create (CreateSpec.global_flags, ADR-012). They replace the
        adapter's own --cdi-spec-dir for that call, so a realm's spec directory is the one it names.
        """
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
        if per_call:
            flags += list(per_call)
        elif self.cdi_spec_dir is not None:
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
        if self.owned_paths is not None and "PATH" in env:
            env["PATH"] = _without_dir(env["PATH"], self.owned_paths.bin)
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

    def _prepare(self, args: Sequence[str], per_call: Sequence[str] = ()) -> list[str]:
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
        argv = [*self._global_flags(per_call), *args]
        self.history.append(argv)
        if self.verbose is not None:
            self.verbose(shlex.join(argv))
        return argv

    def _run(
        self, args: Sequence[str], per_call: Sequence[str] = ()
    ) -> subprocess.CompletedProcess[str]:
        argv = self._prepare(args, per_call)
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
        elif _matches(_CDI_PATTERNS, stderr):
            err = make("GINN-E-NVIDIA-CDI", detail=_decisive_line(stderr))
        elif result.returncode == _NETWORK_EXIT and _matches(NETWORK_PATTERNS, stderr):
            err = make("GINN-E-NETWORK")
        elif op == "rmi" and _matches(_IN_USE_PATTERNS, stderr):
            # No catalogue code names an image in use, so this stays GINN-E-INTERNAL, with the
            # reason in the details. create and delete check for a user before they call rmi.
            err = make("GINN-E-INTERNAL")
            err.details["reason"] = "the image is in use by a container, so rmi refused it"
        elif op == "cp" and _matches(_PATH_NOT_FOUND_PATTERNS, stderr):
            # The ops layer reads this reason (GINN-E-NO-INIT, F3). The code stays GINN-E-INTERNAL.
            err = make("GINN-E-INTERNAL")
            err.details["reason"] = PATH_NOT_FOUND_REASON
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
        result = self._run(spec.to_argv(), spec.global_flags())
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
        """Run one command in the container and return its status and output.

        Exit 125 is podman's own failure status, so it is mapped to a catalogue error through
        `_failure`. A command that exits 125 itself cannot be told apart from that, and is
        reported the same way; EXEC_GINN_FAILURE already reserves 125 for ginn's failures. Exit
        126 and 127 are passed through: podman reports them for a command that cannot be run in
        a container that did start. With `capture=False` the output goes to the terminal, so
        stderr cannot be read and the status is returned as it is.

        `target` decides the route (ADR-014, Decision 1). A services-on target outside the
        supervisor leaf goes through the exec relay, with the same argv tail as the direct route.
        `on_ack` gets the podman client's pid once it has started, on either route.
        """
        if not capture:
            status = self.exec_interactive(
                container_id,
                argv,
                target=target,
                user=user,
                env=env,
                workdir=workdir,
                tty=tty,
                interactive=interactive,
                pass_env=pass_env,
                on_ack=on_ack,
            )
            return ExecResult(status, "", "")
        args = self._exec_args(container_id, argv, user, env, workdir, tty, interactive, pass_env)
        bounded = timeout_s is not None or max_output is not None or stdout_tail
        decode = _BOUNDED_DECODE if bounded else _UNBOUNDED_DECODE
        if self._routes_to_relay(target):
            result = self._run_relayed(
                target,
                container_id,
                args,
                pass_env,
                tty=tty,
                interactive=interactive,
                timeout_s=timeout_s,
                max_output=max_output,
                stdout_tail=stdout_tail,
                on_ack=on_ack,
                decode=decode,
            )
        elif not bounded and on_ack is None:
            result = self._run(args)
        else:
            result = self._run_bounded(
                args, timeout_s, max_output, stdout_tail, on_ack=on_ack, decode=decode
            )
        if result.returncode == PODMAN_ERROR_EXIT:
            raise self._failure("exec", result)
        return ExecResult(result.returncode, result.stdout, result.stderr)

    def _run_bounded(
        self,
        args: Sequence[str],
        timeout_s: float | None,
        max_output: int | None,
        stdout_tail: bool = False,
        on_ack: Callable[[int], None] | None = None,
        decode: str = _BOUNDED_DECODE,
    ) -> subprocess.CompletedProcess[str]:
        """Run like `_run`, but stop after `timeout_s` seconds, or when stdout passes `max_output`.

        A run that hits a limit is killed and waited for, then GINN-E-INTERNAL is raised with the
        reason in `details`. The kill reaches only the podman client. A process that podman started
        in the container keeps running there, because `podman exec` has no kill of its own, so a
        limit bounds ginn's wait, not the realm's work. Output is decoded as `decode` says: a
        bounded exec keeps surrogateescape, so encoding it gives the exact bytes. An unbounded one
        replaces bad bytes, as `_run` does. `on_ack`, when given, gets the client's pid right after
        Popen, which is the pid the relay route reports too (ADR-014). Fixed argv list, no shell
        (NFR-SEC-005).
        """
        argv = self._prepare(args)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env()
            )
        except OSError as exc:
            raise _internal(f"cannot run {self.binary}: {exc.strerror or exc}") from exc
        if proc.stdout is None or proc.stderr is None:  # both are PIPE above
            raise _internal("cannot read the output of podman")
        read = _Output(bytearray(), bytearray(), None)
        returncode: int | None = None
        try:
            if on_ack is not None:
                on_ack(proc.pid)
            read = _read_bounded(
                proc.stdout,
                proc.stderr,
                timeout_s=timeout_s,
                deadline=deadline,
                max_output=max_output,
                stdout_tail=stdout_tail,
            )
            if read.reason is None:
                try:
                    returncode = proc.wait(timeout=_left(deadline))
                except subprocess.TimeoutExpired:
                    read = read._replace(reason=f"timed out after {timeout_s} s")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.stderr.close()
            proc.wait()
        if read.reason is not None or returncode is None:
            raise _bounded_failure(read.reason or f"timed out after {timeout_s} s")
        return _completed(argv, read, returncode, decode)

    def _run_relayed(
        self,
        target: ExecTarget,
        container_id: str,
        args: Sequence[str],
        pass_env: Sequence[str],
        *,
        tty: bool,
        interactive: bool,
        timeout_s: float | None,
        max_output: int | None,
        stdout_tail: bool,
        on_ack: Callable[[int], None] | None,
        decode: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a captured exec through the supervisor relay (ADR-014, Decision 1).

        The worker gets the write ends of two pipes, which the relay's podman writes to, and the
        read ends come back here. The read loop is the one `_run_bounded` uses, so both bounds stay
        in the client. A bound, or a failure after ACK, stops the run with SIGTERM and then SIGKILL
        (`Exchange.stop`). The raised GINN-E-INTERNAL is the one the direct route raises.
        """
        argv = self._prepare(args)
        request = self._relay_request(
            target, container_id, args, pass_env, tty=tty, interactive=interactive, captured=True
        )
        socket_path, service = self._relay_endpoint()
        out_read, out_write = os.pipe()
        err_read, err_write = os.pipe()
        try:
            exchange = exec_relay.open_exchange(
                socket_path, request, (0, out_write, err_write, out_read, err_read), service=service
            )
        except BaseException:
            for fd in (out_read, out_write, err_read, err_write):
                os.close(fd)
            raise
        os.close(out_write)
        os.close(err_write)
        out = os.fdopen(out_read, "rb", buffering=0)
        err = os.fdopen(err_read, "rb", buffering=0)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        read = _Output(bytearray(), bytearray(), None)
        returncode: int | None = None
        try:
            if on_ack is not None:
                on_ack(exchange.pid)
            read = _read_bounded(
                out,
                err,
                timeout_s=timeout_s,
                deadline=deadline,
                max_output=max_output,
                stdout_tail=stdout_tail,
            )
            if read.reason is None:
                returncode = exchange.wait(_left(deadline))
                if returncode is None:
                    read = read._replace(reason=f"timed out after {timeout_s} s")
            if read.reason is not None:
                exchange.stop()
        except BaseException:
            exchange.stop()
            raise
        finally:
            out.close()
            err.close()
            exchange.close()
        if read.reason is not None or returncode is None:
            raise _bounded_failure(read.reason or f"timed out after {timeout_s} s")
        return _completed(argv, read, returncode, decode)

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
        """Run podman exec with the caller's stdio and return the command's status (IR-011).

        The direct route starts the client with Popen, so `on_spawn` gets its pid before the wait,
        and the caller can signal it (FR-068). The wait returns when the client exits, whatever the
        caller did in `on_spawn` or in a signal handler. `on_exit` runs as soon as the client is
        reaped, so the caller can stop signalling its pid before the pid can be reused. If the wait
        is interrupted, the client is killed and reaped before the interruption propagates, as
        subprocess.run does, and `on_exit` runs after that too.

        On the relay route the pid is the one the relay acknowledges. The caller's signals are
        forwarded to the worker as SIG messages, so the caller must not signal that pid itself
        (ADR-014, `exec_relay.run_attached`). CL-27 records the forwarding.
        """
        args = self._exec_args(container_id, argv, user, env, workdir, tty, interactive, pass_env)
        if self._routes_to_relay(target):
            self._prepare(args)
            request = self._relay_request(
                target,
                container_id,
                args,
                pass_env,
                tty=tty,
                interactive=interactive,
                captured=False,
            )
            socket_path, service = self._relay_endpoint()
            status = exec_relay.run_attached(
                socket_path,
                request,
                service=service,
                on_spawn=on_spawn,
                on_ack=on_ack,
                on_exit=on_exit,
            )
            return _exit_status(status)
        full = self._prepare(args)
        # Without a TTY, a Ctrl-C reaches ginn too: a do-nothing handler holds it while podman runs.
        guard: AbstractContextManager[None] = nullcontext() if tty else _parent_ignores_interrupts()
        try:
            # Fixed argv list, no shell. stdio is inherited so the user's terminal is used.
            with guard:
                client = subprocess.Popen(full, env=self._env())  # noqa: S603
                try:
                    if on_spawn is not None:
                        on_spawn(client.pid)
                    if on_ack is not None:
                        on_ack(client.pid)
                    returncode = client.wait()
                    if on_exit is not None:
                        on_exit()
                except BaseException:
                    client.kill()
                    client.wait()
                    if on_exit is not None:
                        on_exit()
                    raise
        except OSError as exc:
            raise _internal(f"cannot run {self.binary}: {exc.strerror or exc}") from exc
        return _exit_status(returncode)

    def _routes_to_relay(self, target: ExecTarget) -> bool:
        """True when the exec goes through the supervisor relay (ADR-014, Decision 1).

        Only a services-on target does, and only from outside the supervisor leaf. A process
        inside the leaf moves into the realm itself, as it always did.
        """
        return target.services and not exec_relay.in_supervisor_leaf(self.cgroup_file)

    def _relay_endpoint(self) -> tuple[Path, Path | None]:
        """The listener's socket, and the `_exec` service that `sv up` starts when it refuses.

        Without Owned Paths (the explicit constructor) there is no service to start, so the service
        is None and a refused socket is reported at once.
        """
        socket_path = self.runtime_root / exec_relay.SOCKET_NAME
        if self.owned_paths is None:
            return socket_path, None
        from ginn.services.exec_relay import exec_service_dir  # lazy: services imports PodmanCli

        return socket_path, exec_service_dir(self.owned_paths)

    def _relay_request(
        self,
        target: ExecTarget,
        container_id: str,
        args: Sequence[str],
        pass_env: Sequence[str],
        *,
        tty: bool,
        interactive: bool,
        captured: bool,
    ) -> exec_relay.Request:
        """The REQ of one exec: the direct route's tail, and the pass-through values (ADR-014).

        A pass-through value is read from the environment that podman would see on the direct
        route, so a name that is not there is left unset on both routes. The values travel in the
        REQ, never on a command line (NFR-SEC-005).
        """
        env = self._env()
        passed = tuple(f"{name}={env[name]}" for name in sorted(set(pass_env)) if name in env)
        return exec_relay.Request(
            realm=target.realm,
            container_id=container_id,
            tail=tuple(args),
            passed=passed,
            tty=tty,
            interactive=interactive,
            mutating=target.mutating,
            captured=captured,
        )

    @staticmethod
    def _exec_args(
        container_id: str,
        argv: Sequence[str],
        user: str | None,
        env: Mapping[str, str] | None,
        workdir: str | None,
        tty: bool,
        interactive: bool,
        pass_env: Sequence[str] = (),
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
        flags += _pass_flags(pass_env, env)
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

    def cp_into(self, container_id: str, src: Path, dst: str) -> None:
        # Both paths are absolute, so neither can start with '-', and podman never reads one as an
        # option. The container path is cut at its first ':', so the container id must not hold one.
        if not src.is_absolute():
            raise ValueError(f"cp source must be an absolute path (NFR-SEC-005): {src}")
        if not dst.startswith("/") or any(ch in dst for ch in "\0\n\r"):
            raise ValueError(f"cp destination must be an absolute container path: {dst!r}")
        args = ["cp", str(src), f"{require_arg(container_id, 'container')}:{dst}"]
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

    def unshare(self, argv: Sequence[str]) -> ExecResult:
        """Run `argv` under `podman unshare` and return its status (ADR-002, ADR-004).

        No attach path calls this: the grant helper runs on the host (ADR-008, Resolution A). The
        host gate and the test cleanup use it. The command's status is not raised. Podman's own
        failure, exit 125, is mapped to a catalogue code. The first word must be an absolute path,
        so no later word can be read as a podman option (NFR-SEC-005).
        """
        if not argv or not argv[0].startswith("/"):
            raise ValueError("unshare needs an absolute command path first (NFR-SEC-005)")
        result = self._run(["unshare", *argv])
        if result.returncode == PODMAN_ERROR_EXIT:
            raise self._failure("unshare", result)
        return ExecResult(result.returncode, result.stdout, result.stderr)
