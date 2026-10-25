# SPDX-License-Identifier: Apache-2.0
"""AUR packages built inside an Arch realm (FR-043, IR-023, DR-007, FR-012, NFR-SEC-005, FR-122).

`install_aur` takes the names of AUR packages and installs them. Everything runs in the realm: the
clone of each package, the read of its .SRCINFO, the build and the install (IR-023). The host runs
nothing from the AUR and reads nothing from it. No AUR helper is installed (FR-043).

The steps, each journaled through `step` after it finishes (the journal params are in brackets):
1. `git`: git is installed as container root. [added: what the operation has added so far]
2. `clone` and `srcinfo`: each requested package is cloned as the realm user, and `makepkg
   --printsrcinfo` gives its pkgnames, version, dependencies by section and build names. A requested
   name must be a pkgname of its PKGBUILD. `-debug` pkgnames are not packages ginn installs.
3. `dependencies`: the runtime names that are not packages of this operation are installed as
   container root with `--asdeps`. [added]
4. `toolchain`: `base-devel` and the build names are installed as container root, so makepkg needs
   no sudo: `makepkg -s` would escalate, and no sudo is installed in a realm (AS-007). [added]
5. `build` and `upgrading`: the requested packages are built in dependency order. A requested
   package that another requested package needs is built and installed first. Its sibling pkgnames
   that it needs are installed first too, with `--asdeps`. Each installation is preceded by an
   `upgrading` journal step. Only the package files of the wanted pkgnames are passed to pacman
   (`pacman --upgrade`). A `-debug` file is never passed.
6. `built`: all packages are installed. The closure of the requested, runtime and sibling names is
read in the realm (one `pacman --query --info`, following Depends On and Provides). The build-only
set is `added` minus that closure, in name order and without repeats, and it is capped. The journal
   records only that set, as `removable`, and drops `added` [removable].
7. `removed`: the build-only set is removed without `--recursive` (ADR-011). When pacman refuses
a package, the names it reports as needed are kept and the rest are removed once more. Whatever is
   left is a residual (GINN-W-BUILD-RESIDUAL), and the operation goes on to its manifest write.

Restore (FR-043 AC-2). A GinnError before `built` removes every package the operation added, so
the realm keeps the package set it had. A GinnError after it does not restore: the requested
packages are installed, and the build-only set is a residual, as in step 7. The build directory
goes in every case. A KeyboardInterrupt is not restored here: the journal entry stays, and
recovery settles it (ops/recovery.py).

Signature and checksum checks stay on. makepkg verifies the source checksums and the PGP signatures
of the sources, and no argv here passes `--skipchecksums`, `--skippgpcheck`, `--skipinteg` or
`--asroot` (FR-012 AC-2). The tests scan every argv of this module for them.

Limits. Every step is bounded in time and output by the adapter (podman/cli.py), and a step that
hits
a limit is GINN-E-PKG-MANAGER with the limit as its detail. A step whose stdout is not parsed keeps
only the tail of it, so a flood of build output cannot fill memory. The client is killed, but podman
exec has no kill of its own, so the process may keep running in the realm: a residual of the limit.

The build directory is `/var/tmp/ginn-aur` in the realm. It is removed first, then created with mode
0700 and owned by the realm user, and each step fails closed if the path is not what was made. A
concurrent user of the same realm, or a PKGBUILD, can still plant files in that directory, and a
root
step that reads a path from it (the packagelist) follows the path inside the realm. Each path must
be a
package file directly under the build directory, and it is never a host path.

Sibling packages and dependencies between requested packages (FR-043). A sibling pkgname that a
requested package needs is installed with `--asdeps`, and it is not recorded in the manifest: only
the
names the user asked for are. A requested package that another requested package needs is built and
installed first, with the requested names explicit.

GnuPG and validpgpkeys (OPEN DECISION, for the user). The build keyring is empty
(`GNUPGHOME=/var/tmp/ginn-aur/gnupg`), so a source that a PKGBUILD signs with validpgpkeys fails the
PGP check. ginn does not import keys into the build. Importing them is a key-trust policy that an
ADR
must decide (FR-012). The failure is GINN-E-PKG-SIGNATURE, never a skipped check.

Residual. A build that fails after `built` leaves the build-only set installed when pacman refuses
to
remove it (a warning names it). A dependency that exists only in the AUR is not built here: pacman
refuses it, and the operation is restored. ADR-011 allows pacman to upgrade an installed dependency
that a new package needs, and an AUR install is no exception.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from ginn.distros import DistroDef, Verb
from ginn.domain import DistroId, PackageName
from ginn.errors import GinnError, make
from ginn.packages.manager import (
    MAX_NAMES,
    MAX_OUTPUT_CHARS,
    check_output,
    name_batches,
    parse_names,
    printable,
    refused,
    remove_passes,
    run_step,
    shown,
)
from ginn.podman.adapter import ExecTarget, Podman, RetryHook
from ginn.podman.models import ExecResult
from ginn.store.journal import PARAMS_OVERFLOW

AUR_URL: Final = "https://aur.archlinux.org"
"""The AUR git host. Each package is `<AUR_URL>/<name>.git`, read inside the realm (IR-023)."""
BUILD_ROOT: Final = "/var/tmp/ginn-aur"  # noqa: S108 - a realm path
"""The build directory inside the realm. It is container-private, not a host path (DR-010)."""
ARCH: Final = DistroId("arch")
TOOLCHAIN: Final = "base-devel"
"""The build group of FR-043, installed with the make-dependencies of each package."""
GIT: Final = "git"
FORBIDDEN_BUILD_FLAGS: Final = ("--skipchecksums", "--skippgpcheck", "--skipinteg", "--asroot")
"""Options that would turn off a checksum, a PGP check or the user check of makepkg (FR-012)."""
BUILD_OUTPUT_BYTES: Final = 256 * 1024 * 1024
"""Most output one makepkg build may print. A larger build is stopped (GINN-E-PKG-MANAGER)."""
BUILD_TIMEOUT_S: Final = 6.0 * 3600.0
"""Seconds one build, or one root install step, may run. Large packages take hours."""
ROOT_OUTPUT_BYTES: Final = 256 * 1024 * 1024
"""Most output one root install, remove or upgrade step may print. Install scriptlets are AUR
code."""
FETCH_OUTPUT_BYTES: Final = 1024 * 1024
FETCH_TIMEOUT_S: Final = 600.0
QUERY_TIMEOUT_S: Final = 300.0
GRAPH_OUTPUT_BYTES: Final = 16 * 1024 * 1024
"""Most output the full `pacman --query --info` listing may have: a large realm lists every
package."""
MAX_REMOVABLE: Final = 512
"""Most build-only packages one operation removes. A larger set is reported, not removed."""
_FIELD: Final = re.compile(r"^\s*(pkgbase|pkgname|epoch|pkgver|pkgrel)\s*=\s*(.+?)\s*$")
_REQUIREMENT: Final = re.compile(
    r"^\s*(depends|makedepends|checkdepends)(?:_x86_64)?\s*=\s*(.+?)\s*$"
)
"""A dependency line of a .SRCINFO: the bare key, or the `_x86_64` form, and nothing else."""
_KEY_LINE: Final = re.compile(r"^(\S[^:]*?)\s*:\s?(.*)$")
"""A `pacman --query --info` field: `Name            : value`."""
_CONSTRAINT: Final = re.compile(r"[<>=]")
_DEBUG_SUFFIX: Final = "-debug"
_BREAKS: Final = re.compile(r"removing (\S+) breaks dependency")
"""pacman's refusal of a removal: the package it names is needed by one that stays."""
_WHY_CHARS: Final = 300
"""Longest line of pacman's output that a residual reason shows, after escaping."""
_SNAPSHOT_DROP: Final = ("added", PARAMS_OVERFLOW)
"""The params that a step writing a whole package set replaces: its own set, and the overflow marker
of an earlier step that dropped params (store/journal.py). The set it writes is complete, so the
marker is cleared with it, and recovery honours that set."""
_ROOT: Final = "0:0"
_ROOT_ENV: Final = {"LC_ALL": "C.UTF-8"}
_PKGBASE: Final = ""
"""The section key of the .SRCINFO header, whose dependencies every pkgname inherits."""


class Step(Protocol):
    """Records one journal step with its params, and drops params (ops/_mutating.py)."""

    def __call__(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        drop: Sequence[str] = (),
    ) -> None: ...


@dataclass(frozen=True)
class Bound:
    """The limits of one step: its time, its output, and whether only the output's tail is kept."""

    timeout_s: float
    max_output: int
    tail: bool


BOUND_QUERY: Final = Bound(QUERY_TIMEOUT_S, MAX_OUTPUT_CHARS, False)
"""A parsed query: a list of names (the installed set)."""
BOUND_GRAPH: Final = Bound(QUERY_TIMEOUT_S, GRAPH_OUTPUT_BYTES, False)
"""The full package listing, parsed for the closure."""
BOUND_FETCH: Final = Bound(FETCH_TIMEOUT_S, FETCH_OUTPUT_BYTES, False)
"""A parsed read of the PKGBUILD: .SRCINFO and the packagelist."""
BOUND_CLONE: Final = Bound(FETCH_TIMEOUT_S, FETCH_OUTPUT_BYTES, True)
BOUND_BUILD: Final = Bound(BUILD_TIMEOUT_S, BUILD_OUTPUT_BYTES, True)
BOUND_ROOT: Final = Bound(BUILD_TIMEOUT_S, ROOT_OUTPUT_BYTES, True)
"""An install, remove, upgrade or cleanup step: its output is not parsed, so only the tail is
kept."""


def _checked(name: str) -> str:
    """A name that passes DR-003, or GINN-E-PKG-NAME-INVALID before it reaches an argv."""
    return PackageName(name, ARCH).value


def clone_argv(name: str) -> list[str]:
    """`git clone` of one package. Only https from the AUR host is allowed (protocol.allow)."""
    _checked(name)
    return [
        GIT,
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "clone",
        "--depth",
        "1",
        "--",
        f"{AUR_URL}/{name}.git",
        name,
    ]


def srcinfo_argv() -> list[str]:
    """makepkg prints the .SRCINFO of the PKGBUILD in the working directory. It does not build."""
    return ["makepkg", "--printsrcinfo"]


def build_argv() -> list[str]:
    """makepkg builds in the working directory. No sudo: the dependencies are already installed."""
    return ["makepkg", "--noconfirm", "--noprogressbar"]


def packagelist_argv() -> list[str]:
    """makepkg prints the path of each package file it builds, without building."""
    return ["makepkg", "--packagelist"]


def upgrade_argv(files: Sequence[str], *, asdeps: bool = False) -> list[str]:
    """pacman installs built package files (`--upgrade`). `asdeps` marks them as dependencies, for
    the sibling pkgnames a requested package needs. Each path is one argv element."""
    flags = ["--asdeps"] if asdeps else []
    return ["pacman", "--upgrade", "--noconfirm", *flags, *files]


def query_argv() -> list[str]:
    """Every installed package name, one per line (`pacman --query --quiet`)."""
    return ["pacman", "--query", "--quiet"]


def info_argv() -> list[str]:
    """The info of every installed package: Depends On and Provides, for the closure (FR-043)."""
    return ["pacman", "--query", "--info"]


def install_deps_argv(names: Iterable[str]) -> list[str]:
    """The runtime names of a package, as dependencies (`--asdeps`), and `--needed` as always."""
    return [
        "pacman",
        "--sync",
        "--needed",
        "--asdeps",
        "--noconfirm",
        *(_checked(n) for n in names),
    ]


def prepare_argvs(uid: int, gid: int) -> list[list[str]]:
    """The build directory, made from nothing (container root). `rm -rf` removes a symlink itself,
    so `mkdir` without `-p` fails on anything that is not the directory it creates (fail closed)."""
    return [
        ["rm", "-rf", "--", BUILD_ROOT],
        ["mkdir", "-m", "0700", "--", BUILD_ROOT],
        ["chown", "--", f"{uid}:{gid}", BUILD_ROOT],
    ]


def cleanup_argv() -> list[str]:
    """Removes the build directory with its sources and packages (container root)."""
    return ["rm", "-rf", "--", BUILD_ROOT]


def build_env() -> dict[str, str]:
    """The environment of the realm user's build: its home and keyring under the build directory,
    and no prompt from git. The keyring is the build's own, never the host's (FR-012)."""
    return {
        "LC_ALL": "C.UTF-8",
        "HOME": BUILD_ROOT,
        "GNUPGHOME": f"{BUILD_ROOT}/gnupg",
        "GIT_TERMINAL_PROMPT": "0",
    }


def package_dir(name: str) -> str:
    """The directory of one package in the build directory. The name is validated first."""
    return f"{BUILD_ROOT}/{_checked(name)}"


@dataclass(frozen=True)
class SrcInfo:
    """What the build uses from a .SRCINFO (makepkg --printsrcinfo).

    `sections` maps the pkgbase (key "") and each pkgname to the runtime names it declares. The
    build names (`makedepends`, `checkdepends`) belong to the whole PKGBUILD. `excluded` are the
    requested names, which are never installed from a repository.
    """

    pkgnames: frozenset[str]
    version: str
    sections: Mapping[str, frozenset[str]] = field(default_factory=dict)
    build_names: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()

    def depends_of(self, name: str) -> frozenset[str]:
        """The runtime names of one package: the pkgbase's and its own (a split package
        inherits)."""
        return self.sections.get(_PKGBASE, frozenset()) | self.sections.get(name, frozenset())

    @property
    def runtime(self) -> frozenset[str]:
        """Every runtime name of the PKGBUILD that is not one of its packages or a requested
        name."""
        names = frozenset().union(*self.sections.values())
        return names - self.pkgnames - self.excluded

    @property
    def build(self) -> frozenset[str]:
        """Every build name of the PKGBUILD that is not one of its packages or a requested name."""
        return self.build_names - self.pkgnames - self.excluded


def read_srcinfo(text: str, *, realm: str, requested: str, excluded: Iterable[str]) -> SrcInfo:
    """Read a .SRCINFO for the requested package (fail closed, NFR-SEC-005).

    The requested name must be a pkgname of the PKGBUILD. A dependency name that is not a package
    name (DR-003) is refused, and it is named escaped. `excluded` are the requested names.
    """
    check_output(text, realm=realm, step="srcinfo")
    fields: dict[str, str] = {}
    pkgnames: set[str] = set()
    sections: dict[str, set[str]] = {}
    build_names: set[str] = set()
    section = _PKGBASE
    for line in text.splitlines():
        found = _FIELD.match(line)
        if found is not None:
            key, value = found.group(1), found.group(2)
            if key == "pkgname":
                pkgnames.add(value)
                section = value
            elif key == "pkgbase":
                section = _PKGBASE
            else:
                fields.setdefault(key, value)
            continue
        match = _REQUIREMENT.match(line)
        if match is None:
            continue
        name = _CONSTRAINT.split(match.group(2), maxsplit=1)[0].strip()
        try:
            PackageName(name, ARCH)
        except GinnError as exc:
            detail = f"the dependency {printable(name)} is not a package name ({exc.code})"
            raise refused(realm, "srcinfo", detail) from exc
        if match.group(1) == "depends":
            sections.setdefault(section, set()).add(name)
        else:
            build_names.add(name)
    kept = frozenset(name for name in pkgnames if not name.endswith(_DEBUG_SUFFIX))
    if requested not in kept:
        detail = f"{printable(requested)} is not a package of its PKGBUILD"
        raise refused(realm, "srcinfo", detail)
    if "pkgver" not in fields or "pkgrel" not in fields:
        raise refused(realm, "srcinfo", "the .SRCINFO has no pkgver or pkgrel")
    version = f"{fields['pkgver']}-{fields['pkgrel']}"
    if "epoch" in fields:
        version = f"{fields['epoch']}:{version}"
    info = SrcInfo(
        pkgnames=kept,
        version=version,
        sections={key: frozenset(value) for key, value in sections.items()},
        build_names=frozenset(build_names),
        excluded=frozenset(excluded),
    )
    if len(info.runtime) + len(info.build) > MAX_NAMES:
        raise refused(
            realm, "srcinfo", f"more than {MAX_NAMES} dependency names, so it is not read"
        )
    return info


def sibling_closure(info: SrcInfo, name: str) -> frozenset[str]:
    """The sibling pkgnames that the requested package needs, transitively (FR-043).

    A sibling is a pkgname of the same PKGBUILD. Each one is built with the requested package and
    installed before it, with `--asdeps`.
    """
    found: set[str] = set()
    pending = (set(info.depends_of(name)) & info.pkgnames) - {name}
    while pending:
        sibling = pending.pop()
        if sibling in found:
            continue
        found.add(sibling)
        pending |= (set(info.depends_of(sibling)) & info.pkgnames) - found - {name}
    return frozenset(found)


def requested_needs(info: SrcInfo, name: str, requested: Iterable[str]) -> frozenset[str]:
    """The other requested packages that the requested package needs, to build them first."""
    wanted = set(requested)
    names = set(info.depends_of(name)) | set(info.build_names)
    for sibling in sibling_closure(info, name):
        names |= set(info.depends_of(sibling))
    return frozenset((names & wanted) - {name} - info.pkgnames)


def build_order(
    names: Sequence[str], needs: Mapping[str, frozenset[str]], *, realm: str
) -> list[str]:
    """The requested names in an order where each comes after the ones it needs (a cycle
    refuses)."""
    remaining = {name: set(needs.get(name, frozenset())) & set(names) for name in names}
    order: list[str] = []
    while remaining:
        ready = sorted(name for name, waits in remaining.items() if not waits)
        if not ready:
            raise refused(realm, "order", "the requested packages depend on each other in a cycle")
        for name in ready:
            order.append(name)
            del remaining[name]
        for waits in remaining.values():
            waits -= set(ready)
    return order


def select_files(
    text: str,
    *,
    directory: str,
    wanted: Sequence[str],
    info: SrcInfo,
    realm: str,
) -> dict[str, str]:
    """The package file of each wanted pkgname, from `makepkg --packagelist` (fail closed).

    Every line must be a package file directly under `directory`, or the operation is refused. The
    file name is `<pkgname>-<version>-<arch>.pkg.tar[.<compression>]`, so a `-debug` file and a
    sibling's file never match another pkgname. A wanted pkgname with no file refuses the operation.
    """
    check_output(text, realm=realm, step="packagelist")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise refused(realm, "packagelist", "makepkg printed no package file")
    for path in lines:
        base = path[len(directory) + 1 :] if path.startswith(f"{directory}/") else ""
        if not base or "/" in base or ".." in base.split("/"):
            detail = "a package path is not a package file directly under the build directory"
            raise refused(realm, "packagelist", detail)
    chosen: dict[str, str] = {}
    for name in wanted:
        pattern = re.compile(
            rf"{re.escape(name)}-{re.escape(info.version)}-[^-/]+\.pkg\.tar(?:\.[a-z0-9]+)?"
        )
        matches = [path for path in lines if pattern.fullmatch(path[len(directory) + 1 :])]
        if not matches:
            detail = f"makepkg listed no package file of {printable(name)}"
            raise refused(realm, "packagelist", detail)
        chosen[name] = sorted(matches)[0]
    return chosen


def _tokens(value: str) -> list[str]:
    """The package names of a Depends On or Provides value: `None` is no name, and a constraint is
    dropped (`libfoo.so=1-64` names `libfoo.so`)."""
    words = [word for word in value.split() if word != "None"]
    return [_CONSTRAINT.split(word, maxsplit=1)[0] for word in words]


@dataclass(frozen=True)
class Graph:
    """The installed packages: what each one depends on, and which packages provide each name."""

    deps: Mapping[str, frozenset[str]]
    provides: Mapping[str, frozenset[str]]


def depends_of(text: str, *, realm: str, max_chars: int = MAX_OUTPUT_CHARS) -> Graph:
    """The graph of a `pacman --query --info` listing (fail closed).

    Packages are separated by blank lines. A Depends On or Provides value may wrap onto indented
    lines. A provided name maps to the packages that provide it, so a virtual dependency such as
    `sh`    reaches its provider.
    """
    check_output(text, realm=realm, step="closure", max_chars=max_chars)
    deps: dict[str, set[str]] = {}
    provides: dict[str, set[str]] = {}
    for block in re.split(r"\n\s*\n", text):
        name: str | None = None
        key: str | None = None
        block_deps: set[str] = set()
        block_provides: set[str] = set()
        for line in block.splitlines():
            if line[:1].isspace():
                if key == "Depends On":
                    block_deps.update(_tokens(line))
                elif key == "Provides":
                    block_provides.update(_tokens(line))
                continue
            field_match = _KEY_LINE.match(line)
            if field_match is None:
                key = None
                continue
            key, value = field_match.group(1).strip(), field_match.group(2)
            if key == "Name":
                name = value.strip()
            elif key == "Depends On":
                block_deps.update(_tokens(value))
            elif key == "Provides":
                block_provides.update(_tokens(value))
        if name is not None:
            deps.setdefault(name, set()).update(block_deps)
            for provided in block_provides:
                provides.setdefault(provided, set()).add(name)
    if len(deps) > MAX_NAMES:
        raise refused(realm, "closure", f"more than {MAX_NAMES} packages, so it is not read")
    return Graph(
        deps={name: frozenset(values) for name, values in deps.items()},
        provides={name: frozenset(values) for name, values in provides.items()},
    )


def closure_of(graph: Graph, roots: Iterable[str], *, realm: str) -> frozenset[str]:
    """The installed packages that the roots need, through Depends On and Provides (fail closed).

    A name that is not an installed package is followed through the packages that provide it. A
    package that is not in the graph is not installed, so it is not followed.
    """
    seen: set[str] = set()
    pending = sorted(roots)
    while pending:
        name = pending.pop()
        if name in seen or name not in graph.deps:
            continue
        seen.add(name)
        if len(seen) > MAX_NAMES:
            raise refused(realm, "closure", "the dependency closure is too large to read")
        for dep in graph.deps[name]:
            pending.append(dep)
            pending.extend(sorted(graph.provides.get(dep, frozenset())))
    return frozenset(seen)


def _why(err: GinnError) -> str:
    """The line of pacman's output that says why a step failed: the last `error:` line, else the
    last line. Escaped and cut to `_WHY_CHARS`. Empty when the output is not in the error."""
    output = err.details.get("output")
    if not isinstance(output, str):
        return ""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line]
    chosen = errors[-1] if errors else (lines[-1] if lines else "")
    return printable(chosen, _WHY_CHARS)


def _needed_by_pacman(err: GinnError, removable: set[str]) -> set[str]:
    """The removable packages that pacman says another package still needs (its refusal's text)."""
    output = err.details.get("output")
    if not isinstance(output, str):
        return set()
    return {name for name in _BREAKS.findall(output) if name in removable}


@dataclass(frozen=True)
class AurOutcome:
    """What the build left behind. `residual` names build-only packages that pacman would not
    remove, and `reason` is the refusal in one line. Both are empty when the removal succeeded."""

    residual: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class _Realm:
    """The container and the commands of one AUR operation: every step runs through `exec`."""

    podman: Podman
    container_id: str
    definition: DistroDef
    realm: str
    retry_hook: RetryHook | None
    user: str
    target: ExecTarget

    def exec(
        self,
        argv: Sequence[str],
        *,
        step: str,
        bound: Bound,
        builder: bool = False,
        workdir: str | None = None,
    ) -> ExecResult:
        """Run one argv within `bound`. `builder` runs it as the realm user in the build
        environment, since makepkg refuses root. Otherwise it runs as container root."""
        return run_step(
            self.podman,
            self.container_id,
            self.definition,
            argv,
            realm=self.realm,
            step=step,
            retry_hook=self.retry_hook,
            target=self.target,
            user=self.user if builder else _ROOT,
            env=build_env() if builder else _ROOT_ENV,
            workdir=workdir,
            timeout_s=bound.timeout_s,
            max_output=bound.max_output,
            stdout_tail=bound.tail,
        )

    def install(self, names: Iterable[str], *, step: str) -> None:
        """Install `names` as container root, from the distribution data (FR-040).

        A relay route takes the names in calls of its own size (name_batches, ADR-014).
        """
        for batch in name_batches(sorted(names), self.target):
            self.exec(self.definition.argv(Verb.INSTALL, batch), step=step, bound=BOUND_ROOT)

    def install_deps(self, names: Iterable[str]) -> None:
        """Install the runtime names of the requested packages, as dependencies (`--asdeps`)."""
        for batch in name_batches(sorted(names), self.target):
            self.exec(install_deps_argv(batch), step="dependencies", bound=BOUND_ROOT)

    def upgrade(self, files: Sequence[str], *, asdeps: bool) -> None:
        """Install one built package file (pacman `--upgrade`), as a dependency when `asdeps`."""
        for batch in name_batches(files, self.target):
            self.exec(upgrade_argv(batch, asdeps=asdeps), step="install", bound=BOUND_ROOT)

    def installed(self) -> frozenset[str]:
        """The installed package names now (container root). Over the caps, it raises."""
        result = self.exec(query_argv(), step="query", bound=BOUND_QUERY)
        return parse_names(result.stdout, realm=self.realm, step="query")

    def graph(self) -> Graph:
        """The full package graph of the realm, for the closure (container root)."""
        result = self.exec(info_argv(), step="closure", bound=BOUND_GRAPH)
        return depends_of(result.stdout, realm=self.realm, max_chars=GRAPH_OUTPUT_BYTES)


def _ids(user: str) -> tuple[int, int]:
    """The uid and gid of a `uid:gid` user string (exec_user, FR-048)."""
    uid, _, gid = user.partition(":")
    return int(uid), int(gid)


def _build(run: _Realm, names: Sequence[str], before: frozenset[str], step: Step) -> AurOutcome:
    """The steps of the module docstring, in order. `before` is the realm's package set (AC-2)."""
    for argv in prepare_argvs(*_ids(run.user)):
        run.exec(argv, step="prepare", bound=BOUND_ROOT)
    run.install([GIT], step="git")
    step("git", {"added": sorted(run.installed() - before)}, drop=_SNAPSHOT_DROP)
    infos: dict[str, SrcInfo] = {}
    for name in names:
        run.exec(
            clone_argv(name), step="clone", bound=BOUND_CLONE, builder=True, workdir=BUILD_ROOT
        )
        srcinfo = run.exec(
            srcinfo_argv(), step="srcinfo", bound=BOUND_FETCH, builder=True,
            workdir=package_dir(name),
        )  # fmt: skip
        infos[name] = read_srcinfo(srcinfo.stdout, realm=run.realm, requested=name, excluded=names)
    runtime: set[str] = set()
    build: set[str] = set()
    for info in infos.values():
        runtime |= info.runtime
        build |= info.build
    if runtime:
        run.install_deps(runtime)
    step("dependencies", {"added": sorted(run.installed() - before)}, drop=_SNAPSHOT_DROP)
    run.install({TOOLCHAIN, *build}, step="toolchain")
    step("toolchain", {"added": sorted(run.installed() - before)}, drop=_SNAPSHOT_DROP)
    needs = {name: requested_needs(infos[name], name, names) for name in names}
    order = build_order(names, needs, realm=run.realm)
    siblings_of = {name: sorted(sibling_closure(infos[name], name)) for name in order}
    for name in order:
        info = infos[name]
        directory = package_dir(name)
        run.exec(build_argv(), step="build", bound=BOUND_BUILD, builder=True, workdir=directory)
        listing = run.exec(
            packagelist_argv(), step="packagelist", bound=BOUND_FETCH, builder=True,
            workdir=directory,
        )  # fmt: skip
        wanted = [*siblings_of[name], name]
        files = select_files(
            listing.stdout, directory=directory, wanted=wanted, info=info, realm=run.realm
        )
        for sibling in siblings_of[name]:
            step("upgrading")
            run.upgrade([files[sibling]], asdeps=True)
        step("upgrading")
        run.upgrade([files[name]], asdeps=False)
    return _settle_build(run, names, runtime, siblings_of, before, step)


def _settle_build(
    run: _Realm,
    names: Sequence[str],
    runtime: set[str],
    siblings_of: Mapping[str, list[str]],
    before: frozenset[str],
    step: Step,
) -> AurOutcome:
    """Read the closure of what must stay, journal the build-only set, and remove it (FR-043)."""
    after_built = run.installed()
    roots = set(names) | runtime
    for siblings in siblings_of.values():
        roots |= set(siblings)
    try:
        keep = closure_of(run.graph(), roots & after_built, realm=run.realm)
    except GinnError:
        # The closure could not be read. Keeping only the requested and runtime names is safe:
        # pacman refuses to remove any package that one of them needs, and that refusal is a
        # residual.
        keep = frozenset()
    # The roots are kept whatever the closure says: a requested package is never build-only.
    keep = (keep | (roots & after_built)) & after_built
    removable = sorted((after_built - before) - keep)
    if len(removable) > MAX_REMOVABLE:
        reason = f"more than {MAX_REMOVABLE} build-only packages, so none is removed"
        step("built", {"removable": []}, drop=_SNAPSHOT_DROP)
        step("removed")
        return AurOutcome(tuple(removable), reason)
    step("built", {"removable": removable}, drop=_SNAPSHOT_DROP)
    return _remove_build_only(run, removable, step)


def _remove_build_only(run: _Realm, removable: list[str], step: Step) -> AurOutcome:
    """Remove the build-only set. A refusal is a residual, not a restore: pacman names the packages
    another one needs, those are kept, and the rest are removed once more."""
    residual: tuple[str, ...] = ()
    reason = ""
    if removable:
        try:
            _remove_names(run, removable, "remove-build")
        except GinnError as err:
            needed = _needed_by_pacman(err, set(removable))
            reason = f"{err.code}: {err.message}"
            rest = [name for name in removable if name not in needed]
            residual = tuple(removable)
            why = _why(err)
            if why:
                reason = f"{reason}: {why}"
            if needed and rest:
                try:
                    _remove_names(run, rest, "remove-build")
                    residual = tuple(sorted(needed))
                    reason = (
                        f"{reason}; pacman needs {shown(sorted(needed))}, and the rest were removed"
                    )
                except GinnError as again:
                    reason = f"{reason}; the retry failed too ({again.code})"
    step("removed")
    return AurOutcome(residual, reason)


def restore(run: _Realm, before: frozenset[str]) -> None:
    """Remove every package the operation added, so the package set is `before` again (AC-2)."""
    added = sorted(run.installed() - before)
    if added:
        _remove_names(run, added, "restore")


def _remove_names(run: _Realm, names: Sequence[str], step: str) -> None:
    """Remove `names` in calls the route carries. A batch refused by the manager is retried after
    the pass, and a name another removal took is not passed again (remove_passes, ADR-014)."""

    def one(batch: Sequence[str]) -> None:
        run.exec(run.definition.argv(Verb.REMOVE, batch), step=step, bound=BOUND_ROOT)

    batches = name_batches(sorted(names), run.target)
    remove_passes(batches, one, still_present=run.installed if len(batches) > 1 else None)


def _cleanup(run: _Realm) -> None:
    """Remove the build directory. A failure leaves a directory in the realm's /var/tmp, which is
    harmless, so it is not reported as the operation's failure."""
    try:
        run.exec(cleanup_argv(), step="cleanup", bound=BOUND_ROOT)
    except GinnError:
        return


def install_aur(
    podman: Podman,
    container_id: str,
    definition: DistroDef,
    names: Sequence[str],
    *,
    realm: str,
    uid: int,
    gid: int,
    retry_hook: RetryHook | None,
    target: ExecTarget,
    step: Step,
) -> AurOutcome:
    """Build `names` from the AUR, install them, and remove the build-only set (FR-043).

    The realm user is `uid:gid`, the host user under keep-id, as enter runs it (FR-048). A user of 0
    is refused: makepkg refuses root, so it is a ginn defect. A GinnError before `built` restores
    the package set (AC-2) and is raised again. After `built`, the result names any residual (see
    the module docstring).
    """
    if uid == 0 or gid == 0:
        err = make("GINN-E-INTERNAL")
        err.details["detail"] = "the AUR build needs a realm user that is not root (FR-043)"
        raise err
    run = _Realm(podman, container_id, definition, realm, retry_hook, f"{uid}:{gid}", target)
    before = run.installed()
    try:
        return _build(run, names, before, step)
    except GinnError as err:
        try:
            restore(run, before)
        except GinnError as undo:
            err.add_note(f"restore failed: {undo.code}: {undo.message}")
        raise
    finally:
        _cleanup(run)


__all__ = [
    "AUR_URL",
    "BUILD_ROOT",
    "FORBIDDEN_BUILD_FLAGS",
    "AurOutcome",
    "Graph",
    "SrcInfo",
    "build_order",
    "closure_of",
    "depends_of",
    "install_aur",
    "read_srcinfo",
    "requested_needs",
    "restore",
    "select_files",
    "sibling_closure",
]
