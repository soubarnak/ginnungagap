# SPDX-License-Identifier: Apache-2.0
"""The AUR build (packages/aur.py, FR-043, IR-023, FR-012, NFR-SEC-005) on FakePodman.

Every test runs against FakePodman. No package manager, git or makepkg runs, and no test reaches the
AUR. The fake answers the realm's package query in order, one answer per call, and the full package
listing (`pacman --query --info`) from a dependency graph, so the closure is the one the code reads.
Covered: the order and the identity of each command (makepkg and git as the realm user, never root),
the journaled package sets and steps, the build-only set (a runtime dependency that git also pulled
is kept, and a requested package is never build-only), the requested file only, the pkgname and
.SRCINFO rules, the requested packages built in dependency order, the sibling pkgnames installed as
dependencies, the removal retry, the refusals that restore and the refusals that leave a
residual, the network and PGP mappings, the bounds of each step, and the flag scan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ginn.distros import Verb, load
from ginn.errors import GinnError, make
from ginn.packages.aur import (
    AUR_URL,
    BUILD_OUTPUT_BYTES,
    BUILD_ROOT,
    BUILD_TIMEOUT_S,
    FORBIDDEN_BUILD_FLAGS,
    AurOutcome,
    build_argv,
    build_order,
    cleanup_argv,
    clone_argv,
    closure_of,
    depends_of,
    info_argv,
    install_aur,
    install_deps_argv,
    packagelist_argv,
    prepare_argvs,
    query_argv,
    read_srcinfo,
    select_files,
    sibling_closure,
    srcinfo_argv,
    upgrade_argv,
)
from ginn.podman.adapter import CreateSpec, ExecTarget, RetryHook
from ginn.podman.models import ExecResult
from ginn.runtime.spec import LABEL_REALM
from ginn.store.journal import PARAMS_OVERFLOW
from tests.fakes.fake_podman import FakePodman

DIRECT_TARGET = ExecTarget(realm="test", services=False, mutating=False)

DIGEST = "registry.example/arch@sha256:" + "ab" * 32
REALM = "arch"
UID = 1000
GID = 1000
USER = f"{UID}:{GID}"
PKG = "yay-bin"
ARCH = load("arch")
S0 = "bash\ncoreutils\nglibc\nzlib\n"
AFTER_GIT = S0 + "git\npcre2\nperl\n"
AFTER_RUNTIME = AFTER_GIT + "libyaml\n"
AFTER_TOOLCHAIN = AFTER_RUNTIME + "fakeroot\ngcc\ngo\nisl\nmake\n"
AFTER_BUILT = AFTER_TOOLCHAIN + "yay-bin\n"
SRCINFO = (
    "pkgbase = yay-bin\n"
    "\tpkgver = 12.3.4\n"
    "\tpkgrel = 1\n"
    "\tpkgname = yay-bin\n"
    "\tdepends = libyaml\n"
    "\tdepends = zlib>=1.2\n"
    "\tdepends = perl\n"
    "\tmakedepends = go\n"
    "\tcheckdepends_x86_64 = git\n"
)
PACKAGE_FILE = f"{BUILD_ROOT}/{PKG}/yay-bin-12.3.4-1-x86_64.pkg.tar.zst"
PACKAGE_LISTING = f"{PACKAGE_FILE}\n"
DEPS: dict[str, list[str]] = {
    "yay-bin": ["libyaml", "perl"],
    "libyaml": ["glibc"],
    "perl": ["glibc"],
    "zlib": ["glibc"],
    "git": ["pcre2", "perl"],
    "pcre2": ["glibc"],
    "gcc": ["glibc", "isl"],
    "isl": ["glibc"],
    "fakeroot": ["glibc"],
    "go": ["glibc"],
    "make": ["glibc"],
    "bash": ["glibc"],
    "coreutils": ["glibc"],
    "glibc": [],
}
PROVIDES: dict[str, list[str]] = {"bash": ["sh"]}
# The removal the successful build makes: the build-only set, with perl and libyaml kept.
BUILD_ONLY = ["fakeroot", "gcc", "git", "go", "isl", "make", "pcre2"]


SNAPSHOT = ("added", PARAMS_OVERFLOW)
"""The drop of a step that writes a whole package set (packages/aur.py, _SNAPSHOT_DROP)."""


def info_listing(deps: Mapping[str, Sequence[str]], provides: Mapping[str, Sequence[str]]) -> str:
    """The text of `pacman --query --info` for every installed package (one block each)."""
    blocks = []
    for name in sorted(deps):
        depends = " ".join(deps[name]) or "None"
        provided = " ".join(provides.get(name, [])) or "None"
        blocks.append(
            f"Name            : {name}\nDepends On      : {depends}\nProvides        : {provided}\n"
        )
    return "\n".join(blocks)


class GraphPodman(FakePodman):
    """FakePodman whose full package listing answers from `deps` and `provides` (a test seam)."""

    deps: dict[str, list[str]]
    provides: dict[str, list[str]]

    def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        if tuple(argv) == tuple(info_argv()):
            text = info_listing(self.deps, self.provides)
            self.exec_results[tuple(argv)] = ExecResult(0, text, "")
        return super().exec(container_id, argv, **kwargs)


class PerDirPodman(GraphPodman):
    """GraphPodman whose .SRCINFO and packagelist answers depend on the working directory."""

    srcinfo: dict[str, str]
    listing: dict[str, str]

    def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        workdir = kwargs.get("workdir")
        if tuple(argv) == tuple(srcinfo_argv()) and workdir in self.srcinfo:
            self.exec_results[tuple(argv)] = ExecResult(0, self.srcinfo[workdir], "")
        if tuple(argv) == tuple(packagelist_argv()) and workdir in self.listing:
            self.exec_results[tuple(argv)] = ExecResult(0, self.listing[workdir], "")
        return super().exec(container_id, argv, **kwargs)


class BoundsPodman(GraphPodman):
    """GraphPodman that records the limits each call was given (FR-043 bounds)."""

    bounds: list[tuple[tuple[str, ...], object, object, object]]

    def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        self.bounds.append(
            (
                tuple(argv),
                kwargs.get("timeout_s"),
                kwargs.get("max_output"),
                kwargs.get("stdout_tail"),
            )
        )
        return super().exec(container_id, argv, **kwargs)


def _fake(cls: type[FakePodman] = GraphPodman) -> FakePodman:
    spec = CreateSpec(
        name=f"ginn-{REALM}-g1",
        image_digest_ref=DIGEST,
        network="host",
        labels={LABEL_REALM: REALM},
    )
    fake = cls()
    if isinstance(fake, GraphPodman):
        fake.deps = {name: list(values) for name, values in DEPS.items()}
        fake.provides = {name: list(values) for name, values in PROVIDES.items()}
    if isinstance(fake, PerDirPodman):
        fake.srcinfo = {}
        fake.listing = {}
    if isinstance(fake, BoundsPodman):
        fake.bounds = []
    cid = fake.create(spec)
    fake.start(cid)
    fake.exec_calls.clear()
    return fake


def _queries(fake: FakePodman, *lists: str) -> None:
    """The installed-set answers, in call order: before, after git, after runtime, after toolchain,
    after the packages are installed. A later answer (the restore's) repeats the last list given."""
    fake.exec_sequences[tuple(query_argv())] = [ExecResult(0, text, "") for text in lists]


def _ok(fake: FakePodman) -> None:
    """A successful build of one package: five package queries, the built package listed last."""
    _queries(fake, S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_BUILT)
    fake.exec_results[tuple(srcinfo_argv())] = ExecResult(0, SRCINFO, "")
    fake.exec_results[tuple(packagelist_argv())] = ExecResult(0, PACKAGE_LISTING, "")


def _run(
    fake: FakePodman,
    *,
    steps: list[tuple[str, Any, tuple[str, ...]]] | None = None,
    hook: RetryHook | None = None,
    uid: int = UID,
    gid: int = GID,
    names: Sequence[str] = (PKG,),
) -> AurOutcome:
    cid = next(iter(fake.containers))
    record: list[tuple[str, Any, tuple[str, ...]]] = [] if steps is None else steps

    def step(name: str, params: Any = None, *, drop: Sequence[str] = ()) -> None:
        record.append((name, params, tuple(drop)))

    return install_aur(
        fake,
        cid,
        ARCH,
        list(names),
        realm=REALM,
        uid=uid,
        gid=gid,
        retry_hook=hook,
        step=step,
        target=DIRECT_TARGET,
    )


def _argvs(fake: FakePodman) -> list[tuple[str, ...]]:
    return [tuple(call.argv) for call in fake.exec_calls]


def _removals(fake: FakePodman) -> list[tuple[str, ...]]:
    return [argv for argv in _argvs(fake) if argv[:2] == ("pacman", "--remove")]


def _uploads(fake: FakePodman) -> list[tuple[str, ...]]:
    return [argv for argv in _argvs(fake) if argv[:2] == ("pacman", "--upgrade")]


def _install(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(ARCH.argv(Verb.INSTALL, list(names)))


def _added_since_start(after: str) -> list[str]:
    return sorted(set(after.split()) - set(S0.split()))


@pytest.mark.fr("FR-043")
@pytest.mark.fr("IR-023")
def test_a_build_runs_its_steps_in_order_and_journals_the_package_sets() -> None:
    fake = _fake()
    _ok(fake)
    steps: list[tuple[str, Any, tuple[str, ...]]] = []
    outcome = _run(fake, steps=steps)
    assert outcome == AurOutcome()
    assert [name for name, _, _ in steps] == [
        "git",
        "dependencies",
        "toolchain",
        "upgrading",
        "built",
        "removed",
    ]
    # Each snapshot step replaces its set and clears an earlier overflow marker (FR-043, FR-122).
    assert steps[0] == ("git", {"added": ["git", "pcre2", "perl"]}, SNAPSHOT)
    assert steps[1] == ("dependencies", {"added": _added_since_start(AFTER_RUNTIME)}, SNAPSHOT)
    assert steps[2] == ("toolchain", {"added": _added_since_start(AFTER_TOOLCHAIN)}, SNAPSHOT)
    assert steps[4] == ("built", {"removable": BUILD_ONLY}, SNAPSHOT)
    assert steps[5] == ("removed", None, ())
    assert _argvs(fake) == [
        tuple(query_argv()),
        *(tuple(argv) for argv in prepare_argvs(UID, GID)),
        _install(["git"]),
        tuple(query_argv()),
        tuple(clone_argv(PKG)),
        tuple(srcinfo_argv()),
        tuple(install_deps_argv(["libyaml", "perl", "zlib"])),
        tuple(query_argv()),
        _install(["base-devel", "git", "go"]),
        tuple(query_argv()),
        tuple(build_argv()),
        tuple(packagelist_argv()),
        tuple(upgrade_argv([PACKAGE_FILE])),
        tuple(query_argv()),
        tuple(info_argv()),
        tuple(ARCH.argv(Verb.REMOVE, BUILD_ONLY)),
        tuple(cleanup_argv()),
    ]


@pytest.mark.fr("FR-043")
def test_a_runtime_dependency_that_git_also_pulled_is_kept_and_only_build_only_packages_go() -> (
    None
):
    fake = _fake()
    _ok(fake)
    _run(fake)
    assert _removals(fake) == [tuple(ARCH.argv(Verb.REMOVE, BUILD_ONLY))]
    removed = set(_removals(fake)[0])
    assert {"perl", "libyaml", "zlib", PKG}.isdisjoint(removed)


@pytest.mark.fr("FR-043")
def test_a_requested_package_is_never_build_only_even_when_the_closure_is_empty() -> None:
    fake = _fake()
    fake.deps = {}
    _ok(fake)
    _run(fake)
    removed = set(_removals(fake)[0])
    assert PKG not in removed


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-012")
def test_makepkg_and_git_run_as_the_realm_user_and_everything_else_as_root() -> None:
    fake = _fake()
    _ok(fake)
    _run(fake)
    for call in fake.exec_calls:
        if call.argv[0] in ("git", "makepkg"):
            assert call.user == USER, call.argv
            assert call.env["HOME"] == BUILD_ROOT
            assert call.env["GNUPGHOME"] == f"{BUILD_ROOT}/gnupg"
            assert call.env["GIT_TERMINAL_PROMPT"] == "0"
        else:
            assert call.user == "0:0", call.argv
    makepkg = [call for call in fake.exec_calls if call.argv[0] == "makepkg"]
    assert [call.argv[1] for call in makepkg] == ["--printsrcinfo", "--noconfirm", "--packagelist"]
    assert {call.workdir for call in makepkg} == {f"{BUILD_ROOT}/{PKG}"}


@pytest.mark.fr("FR-043")
@pytest.mark.fr("IR-023")
def test_the_clone_is_limited_to_https_from_the_aur_host() -> None:
    argv = clone_argv(PKG)
    assert argv[:5] == ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    assert argv[-2] == f"{AUR_URL}/{PKG}.git"
    assert "--" in argv
    with pytest.raises(GinnError) as caught:
        clone_argv("yay/../x")
    assert caught.value.code == "GINN-E-PKG-NAME-INVALID"


@pytest.mark.fr("FR-043")
def test_runtime_dependencies_are_installed_as_dependencies_and_keep_needed() -> None:
    assert install_deps_argv(["libyaml", "zlib"]) == [
        "pacman",
        "--sync",
        "--needed",
        "--asdeps",
        "--noconfirm",
        "libyaml",
        "zlib",
    ]


@pytest.mark.fr("FR-043")
def test_the_build_directory_is_removed_then_made_without_parents_or_symlinks() -> None:
    assert prepare_argvs(UID, GID) == [
        ["rm", "-rf", "--", BUILD_ROOT],
        ["mkdir", "-m", "0700", "--", BUILD_ROOT],
        ["chown", "--", f"{UID}:{GID}", BUILD_ROOT],
    ]


@pytest.mark.fr("FR-043")
def test_a_requested_name_that_is_not_a_pkgname_is_refused_and_restored() -> None:
    fake = _fake()
    _queries(fake, S0, AFTER_GIT, AFTER_GIT)
    fake.exec_results[tuple(srcinfo_argv())] = ExecResult(
        0, SRCINFO.replace("pkgname = yay-bin", "pkgname = other"), ""
    )
    with pytest.raises(GinnError) as caught:
        _run(fake)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert "is not a package of its PKGBUILD" in caught.value.details["detail"]
    assert _removals(fake) == [tuple(ARCH.argv(Verb.REMOVE, ["git", "pcre2", "perl"]))]


@pytest.mark.fr("FR-043")
@pytest.mark.fr("NFR-SEC-005")
def test_debug_packages_and_unneeded_sibling_files_never_reach_pacman() -> None:
    fake = _fake()
    _ok(fake)
    listing = "".join(
        f"{BUILD_ROOT}/{PKG}/{name}\n"
        for name in (
            "yay-bin-debug-12.3.4-1-x86_64.pkg.tar.zst",
            "yay-cli-12.3.4-1-x86_64.pkg.tar.zst",
            "yay-bin-12.3.4-1-x86_64.pkg.tar.zst",
        )
    )
    fake.exec_results[tuple(packagelist_argv())] = ExecResult(0, listing, "")
    fake.exec_results[tuple(srcinfo_argv())] = ExecResult(
        0, SRCINFO + "\tpkgname = yay-bin-debug\n\tpkgname = yay-cli\n", ""
    )
    _run(fake)
    assert _uploads(fake) == [tuple(upgrade_argv([PACKAGE_FILE]))]


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("DR-003")
def test_a_dependency_name_that_is_not_a_package_name_is_refused_escaped() -> None:
    with pytest.raises(GinnError) as caught:
        read_srcinfo("\tdepends = a\x1b[31m;x\n", realm=REALM, requested=PKG, excluded=[PKG])
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    detail = caught.value.details["detail"]
    assert "\x1b" not in detail and "\\x1b" in detail


@pytest.mark.fr("FR-043")
def test_requested_names_and_pkgnames_never_stay_in_the_runtime_set() -> None:
    text = SRCINFO + "\tpkgname = yay-cli\n\tdepends = yay-cli\n\tdepends = yay-bin\n"
    info = read_srcinfo(text, realm=REALM, requested=PKG, excluded=[PKG, "yay-cli"])
    assert info.runtime == frozenset({"libyaml", "perl", "zlib"})
    assert "yay-cli" not in info.runtime and PKG not in info.runtime


@pytest.mark.fr("FR-012")
def test_the_dependency_keys_are_the_bare_key_and_the_x86_64_form_only() -> None:
    text = SRCINFO + "\tdepends_aarch64 = foo\n\tmakedepends_i686 = bar\n"
    info = read_srcinfo(text, realm=REALM, requested=PKG, excluded=[PKG])
    assert "foo" not in info.runtime and "bar" not in info.build
    assert "git" in info.build


@pytest.mark.fr("FR-043")
def test_a_refused_removal_keeps_the_names_pacman_needs_and_removes_the_rest() -> None:
    fake = _fake()
    _ok(fake)
    refusal = "removing gcc breaks dependency 'gcc-libs' required by fakeroot"
    fake.exec_results[tuple(ARCH.argv(Verb.REMOVE, BUILD_ONLY))] = ExecResult(
        1, "", f":: {refusal}\nerror: failed to prepare transaction"
    )
    steps: list[tuple[str, Any, tuple[str, ...]]] = []
    outcome = _run(fake, steps=steps)
    rest = [name for name in BUILD_ONLY if name != "gcc"]
    assert _removals(fake) == [
        tuple(ARCH.argv(Verb.REMOVE, BUILD_ONLY)),
        tuple(ARCH.argv(Verb.REMOVE, rest)),
    ]
    assert outcome.residual == ("gcc",)
    assert "pacman needs" in outcome.reason
    assert steps[-1][0] == "removed"


@pytest.mark.fr("FR-043")
def test_a_removal_pacman_refuses_outright_is_a_residual_and_the_operation_goes_on() -> None:
    fake = _fake()
    _ok(fake)
    fake.exec_results[tuple(ARCH.argv(Verb.REMOVE, BUILD_ONLY))] = ExecResult(
        1, "", "error: failed to prepare transaction (could not satisfy dependencies)"
    )
    steps: list[tuple[str, Any, tuple[str, ...]]] = []
    outcome = _run(fake, steps=steps)
    assert outcome.residual == tuple(BUILD_ONLY)
    assert "could not satisfy dependencies" in outcome.reason
    assert steps[-1][0] == "removed"
    assert len(_removals(fake)) == 1


@pytest.mark.fr("FR-043")
def test_a_build_only_set_over_the_cap_is_reported_and_none_is_removed() -> None:
    fake = _fake()
    big = "".join(f"pkg{index:04d}\n" for index in range(600))
    _queries(
        fake, S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_TOOLCHAIN + big + "yay-bin\n"
    )
    fake.exec_results[tuple(srcinfo_argv())] = ExecResult(0, SRCINFO, "")
    fake.exec_results[tuple(packagelist_argv())] = ExecResult(0, PACKAGE_LISTING, "")
    steps: list[tuple[str, Any, tuple[str, ...]]] = []
    outcome = _run(fake, steps=steps)
    assert _removals(fake) == []
    assert len(outcome.residual) > 512
    assert "more than 512" in outcome.reason
    assert steps[4] == ("built", {"removable": []}, SNAPSHOT)


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-120")
def test_a_failed_build_before_built_restores_every_added_package() -> None:
    fake = _fake()
    _queries(fake, S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_TOOLCHAIN)
    fake.exec_results[tuple(srcinfo_argv())] = ExecResult(0, SRCINFO, "")
    fake.exec_results[tuple(build_argv())] = ExecResult(2, "", "==> ERROR: build() failed")
    steps: list[tuple[str, Any, tuple[str, ...]]] = []
    with pytest.raises(GinnError) as caught:
        _run(fake, steps=steps)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert [name for name, _, _ in steps] == ["git", "dependencies", "toolchain"]
    restored = _added_since_start(AFTER_TOOLCHAIN)
    assert _removals(fake) == [tuple(ARCH.argv(Verb.REMOVE, restored))]
    assert _argvs(fake)[-1] == tuple(cleanup_argv())


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-012")
def test_a_pgp_failure_in_the_build_is_a_signature_error_and_is_restored() -> None:
    fake = _fake()
    _queries(fake, S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_TOOLCHAIN)
    fake.exec_results[tuple(srcinfo_argv())] = ExecResult(0, SRCINFO, "")
    stderr = "==> ERROR: One or more PGP signatures could not be verified!"
    fake.exec_results[tuple(build_argv())] = ExecResult(1, "", stderr)
    with pytest.raises(GinnError) as caught:
        _run(fake)
    assert caught.value.code == "GINN-E-PKG-SIGNATURE"
    assert _removals(fake)


@pytest.mark.fr("FR-120")
@pytest.mark.fr("FR-043")
def test_a_realistic_network_failure_of_the_clone_is_retried_then_restored() -> None:
    fake = _fake()
    _queries(fake, S0, AFTER_GIT, AFTER_GIT)
    stderr = (
        "fatal: unable to access 'https://aur.archlinux.org/yay-bin.git/': Could not resolve host"
    )
    fake.exec_results[tuple(clone_argv(PKG))] = ExecResult(128, "", stderr)
    attempts: list[int] = []

    def retry(attempt: int, err: GinnError) -> bool:
        attempts.append(attempt)
        return True

    with pytest.raises(GinnError) as caught:
        _run(fake, hook=retry)
    assert caught.value.code == "GINN-E-NETWORK"
    assert caught.value.exit_code == 69
    assert attempts == [1, 2, 3]
    assert _removals(fake) == [tuple(ARCH.argv(Verb.REMOVE, ["git", "pcre2", "perl"]))]


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("FR-043")
def test_a_packagelist_path_outside_the_build_directory_is_refused_before_pacman() -> None:
    fake = _fake()
    _queries(fake, S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_TOOLCHAIN)
    fake.exec_results[tuple(srcinfo_argv())] = ExecResult(0, SRCINFO, "")
    fake.exec_results[tuple(packagelist_argv())] = ExecResult(0, "/etc/yay.pkg.tar.zst\n", "")
    with pytest.raises(GinnError) as caught:
        _run(fake)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert _uploads(fake) == []


@pytest.mark.fr("FR-043")
@pytest.mark.fr("NFR-SEC-005")
def test_each_step_is_bounded_in_time_and_output_and_the_build_keeps_only_a_tail() -> None:
    fake = _fake(BoundsPodman)
    _ok(fake)
    _run(fake)
    bounds = {argv: (timeout, output, tail) for argv, timeout, output, tail in fake.bounds}
    assert bounds[tuple(build_argv())] == (BUILD_TIMEOUT_S, BUILD_OUTPUT_BYTES, True)
    assert bounds[tuple(packagelist_argv())][2] is False
    assert bounds[tuple(srcinfo_argv())][2] is False
    assert bounds[tuple(query_argv())][1] is not None
    assert bounds[tuple(info_argv())][1] > bounds[tuple(query_argv())][1]
    assert bounds[tuple(ARCH.argv(Verb.REMOVE, BUILD_ONLY))][2] is True


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-120")
def test_a_build_that_hits_its_limit_is_a_pkg_manager_error_with_the_limit_named() -> None:
    class Limited(GraphPodman):
        def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
            if tuple(argv) == tuple(build_argv()):
                err = make("GINN-E-INTERNAL")
                err.details["reason"] = f"printed more than {BUILD_OUTPUT_BYTES} bytes"
                raise err
            return super().exec(container_id, argv, **kwargs)

    fake = _fake(Limited)
    _ok(fake)
    with pytest.raises(GinnError) as caught:
        _run(fake)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert "stopped at its limit" in caught.value.details["detail"]


@pytest.mark.fr("FR-043")
def test_a_root_realm_user_is_refused_before_any_call() -> None:
    fake = _fake()
    with pytest.raises(GinnError) as caught:
        _run(fake, uid=0)
    assert caught.value.code == "GINN-E-INTERNAL"
    assert fake.exec_calls == []


@pytest.mark.fr("FR-043")
def test_a_failed_cleanup_is_not_reported_as_the_build_failure() -> None:
    fake = _fake()
    _ok(fake)
    # The cleanup argv is the same as the first prepare argv: the first answer is for prepare.
    fake.exec_sequences[tuple(cleanup_argv())] = [
        ExecResult(0, "", ""),
        ExecResult(1, "", "rm: busy"),
    ]
    steps: list[tuple[str, Any, tuple[str, ...]]] = []
    _run(fake, steps=steps)
    assert steps[-1][0] == "removed"


@pytest.mark.fr("FR-012")
@pytest.mark.fr("NFR-SEC-005")
def test_no_argv_of_a_build_carries_a_signature_bypass_or_a_shell() -> None:
    fake = _fake()
    _ok(fake)
    _run(fake)
    for call in fake.exec_calls:
        text = " ".join(call.argv).lower()
        for flag in FORBIDDEN_BUILD_FLAGS:
            assert flag not in text, (call.argv, flag)
        assert call.argv[0] not in ("sh", "bash", "dash", "zsh")
        assert call.argv[0] == "git" or "-c" not in call.argv


@pytest.mark.fr("FR-012")
def test_every_argv_builder_of_the_aur_module_is_free_of_the_bypass_flags() -> None:
    builders = [
        build_argv(),
        packagelist_argv(),
        srcinfo_argv(),
        query_argv(),
        info_argv(),
        cleanup_argv(),
        clone_argv(PKG),
        upgrade_argv([f"{BUILD_ROOT}/x/a.pkg.tar.zst"]),
        upgrade_argv([f"{BUILD_ROOT}/x/a.pkg.tar.zst"], asdeps=True),
        install_deps_argv(["zlib"]),
        *prepare_argvs(UID, GID),
    ]
    for argv in builders:
        text = " ".join(argv).lower()
        for flag in FORBIDDEN_BUILD_FLAGS:
            assert flag not in text, (argv, flag)


@pytest.mark.fr("FR-043")
def test_the_full_listing_is_read_with_wrapped_lines_none_and_provides() -> None:
    text = (
        "Name            : yay-bin\n"
        "Depends On      : libyaml  perl>=5  zlib\n"
        "                  glibc\n"
        "Provides        : yay\n"
        "Optional Deps   : x: optional text\n"
        "\n"
        "Name            : bash\n"
        "Depends On      : None\n"
        "Provides        : sh\n"
    )
    graph = depends_of(text, realm=REALM)
    assert graph.deps["yay-bin"] == frozenset({"libyaml", "perl", "zlib", "glibc"})
    assert graph.deps["bash"] == frozenset()
    assert graph.provides["sh"] == frozenset({"bash"})
    assert graph.provides["yay"] == frozenset({"yay-bin"})


@pytest.mark.fr("FR-043")
def test_the_closure_follows_a_virtual_dependency_to_its_provider() -> None:
    graph = depends_of(
        info_listing({"yay-bin": ["sh"], "bash": ["glibc"], "glibc": []}, {"bash": ["sh"]}),
        realm=REALM,
    )
    assert closure_of(graph, ["yay-bin"], realm=REALM) == frozenset({"yay-bin", "bash", "glibc"})


@pytest.mark.fr("FR-043")
def test_the_packagelist_selects_only_the_requested_package_file() -> None:
    info = read_srcinfo(SRCINFO, realm=REALM, requested=PKG, excluded=[PKG])
    directory = f"{BUILD_ROOT}/{PKG}"
    listing = (
        f"{directory}/yay-bin-debug-12.3.4-1-x86_64.pkg.tar.zst\n"
        f"{directory}/yay-bin-12.3.4-1-x86_64.pkg.tar.zst\n"
    )
    chosen = select_files(listing, directory=directory, wanted=[PKG], info=info, realm=REALM)
    assert chosen == {PKG: f"{directory}/yay-bin-12.3.4-1-x86_64.pkg.tar.zst"}
    with pytest.raises(GinnError):
        select_files(
            f"{directory}/yay-bin-9-1-x86_64.pkg.tar.zst\n",
            directory=directory,
            wanted=[PKG],
            info=info,
            realm=REALM,
        )


@pytest.mark.fr("NFR-SEC-005")
def test_a_package_file_outside_the_build_directory_or_with_dots_is_refused() -> None:
    info = read_srcinfo(SRCINFO, realm=REALM, requested=PKG, excluded=[PKG])
    directory = f"{BUILD_ROOT}/{PKG}"
    for bad in [f"{directory}/../yay-bin-12.3.4-1-x86_64.pkg.tar.zst\n", "\n"]:
        with pytest.raises(GinnError) as caught:
            select_files(bad, directory=directory, wanted=[PKG], info=info, realm=REALM)
        assert caught.value.code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-043")
def test_a_closure_that_cannot_be_read_keeps_the_requested_and_runtime_names() -> None:
    class BrokenWalk(GraphPodman):
        def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
            if tuple(argv) == tuple(info_argv()):
                return ExecResult(1, "", "error: database is locked")
            return super().exec(container_id, argv, **kwargs)

    fake = _fake(BrokenWalk)
    _ok(fake)
    steps: list[tuple[str, Any, tuple[str, ...]]] = []
    outcome = _run(fake, steps=steps)
    assert outcome == AurOutcome()
    built = steps[4][1]
    assert PKG not in built["removable"]
    assert "libyaml" not in built["removable"] and "perl" not in built["removable"]


@pytest.mark.fr("FR-043")
def test_a_requested_package_that_depends_on_another_requested_package_is_built_first() -> None:
    fake = _fake(PerDirPodman)
    fake.deps.update({"alpha": ["beta", "libyaml"], "beta": ["zlib"]})
    fake.srcinfo = {
        f"{BUILD_ROOT}/alpha": SRCINFO.replace("yay-bin", "alpha").replace(
            "depends = perl\n", "depends = beta\n"
        ),
        f"{BUILD_ROOT}/beta": SRCINFO.replace("yay-bin", "beta").replace("depends = perl\n", ""),
    }
    fake.listing = {
        f"{BUILD_ROOT}/alpha": f"{BUILD_ROOT}/alpha/alpha-12.3.4-1-x86_64.pkg.tar.zst\n",
        f"{BUILD_ROOT}/beta": f"{BUILD_ROOT}/beta/beta-12.3.4-1-x86_64.pkg.tar.zst\n",
    }
    _queries(fake, S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_TOOLCHAIN + "alpha\nbeta\n")
    _run(fake, names=["alpha", "beta"])
    builds = [call.workdir for call in fake.exec_calls if call.argv == tuple(build_argv())]
    assert builds == [f"{BUILD_ROOT}/beta", f"{BUILD_ROOT}/alpha"]
    uploads = _uploads(fake)
    assert uploads == [
        tuple(upgrade_argv([f"{BUILD_ROOT}/beta/beta-12.3.4-1-x86_64.pkg.tar.zst"])),
        tuple(upgrade_argv([f"{BUILD_ROOT}/alpha/alpha-12.3.4-1-x86_64.pkg.tar.zst"])),
    ]


@pytest.mark.fr("FR-043")
def test_a_sibling_pkgname_the_requested_package_needs_is_installed_as_a_dependency_first() -> None:
    fake = _fake(PerDirPodman)
    fake.deps.update({"yay-bin": ["yay-bin-libs", "libyaml"], "yay-bin-libs": ["glibc"]})
    siblings = SRCINFO + "\tpkgname = yay-bin-libs\n"
    fake.srcinfo = {
        f"{BUILD_ROOT}/{PKG}": siblings.replace("depends = perl\n", "depends = yay-bin-libs\n")
    }
    libs = f"{BUILD_ROOT}/{PKG}/yay-bin-libs-12.3.4-1-x86_64.pkg.tar.zst"
    fake.listing = {f"{BUILD_ROOT}/{PKG}": libs + "\n" + PACKAGE_FILE + "\n"}
    _queries(
        fake,
        S0,
        AFTER_GIT,
        AFTER_RUNTIME,
        AFTER_TOOLCHAIN,
        AFTER_TOOLCHAIN + "yay-bin\nyay-bin-libs\n",
    )
    _run(fake)
    assert _uploads(fake) == [
        tuple(upgrade_argv([libs], asdeps=True)),
        tuple(upgrade_argv([PACKAGE_FILE])),
    ]


@pytest.mark.fr("FR-043")
def test_requested_packages_that_need_each_other_in_a_cycle_are_refused() -> None:
    needs = {"alpha": frozenset({"beta"}), "beta": frozenset({"alpha"})}
    with pytest.raises(GinnError) as caught:
        build_order(["alpha", "beta"], needs, realm=REALM)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert build_order(["alpha", "beta"], {"beta": frozenset({"alpha"})}, realm=REALM) == [
        "alpha",
        "beta",
    ]


@pytest.mark.fr("FR-043")
def test_the_sibling_closure_follows_pkgnames_the_requested_package_needs() -> None:
    info = read_srcinfo(
        SRCINFO + "\tpkgname = yay-bin-libs\n\tpkgname = yay-bin-core\n",
        realm=REALM,
        requested=PKG,
        excluded=[PKG],
    )
    assert sibling_closure(info, PKG) == frozenset()
    with_sibling = read_srcinfo(
        SRCINFO.replace("depends = perl\n", "depends = perl\n\tdepends = yay-bin-libs\n")
        + "\tpkgname = yay-bin-libs\n\tdepends = yay-bin-core\n\tpkgname = yay-bin-core\n",
        realm=REALM,
        requested=PKG,
        excluded=[PKG],
    )
    assert sibling_closure(with_sibling, PKG) == frozenset({"yay-bin-libs", "yay-bin-core"})
