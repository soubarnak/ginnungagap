# SPDX-License-Identifier: Apache-2.0
"""Package manager verbs (T5.1, packages/manager.py): argv, locale, failure mapping, dry run.

Every test runs on FakePodman. No test runs a package manager, and none reaches the host.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import DistroDef, Verb, load
from ginn.distros.loader import UpdateStatus, from_mapping
from ginn.errors import GinnError
from ginn.packages.aur import (
    BUILD_ROOT,
    build_argv,
    cleanup_argv,
    clone_argv,
    info_argv,
    install_deps_argv,
    packagelist_argv,
    prepare_argvs,
    query_argv,
    srcinfo_argv,
    upgrade_argv,
)
from ginn.packages.manager import (
    LOCALE,
    MAX_LINE_CHARS,
    MAX_NAMES,
    MAX_OUTPUT_CHARS,
    MAX_SHOWN,
    ROOT_USER,
    CheckResult,
    database_inconsistent,
    decisive_line,
    explicit_packages,
    failure,
    network_failure,
    parse_names,
    parse_removals,
    printable,
    removal_dependents,
    run_check,
    run_step,
    shown,
)
from ginn.podman.adapter import CreateSpec, ExecTarget
from ginn.podman.models import ExecResult
from ginn.runtime.spec import LABEL_REALM
from tests.fakes.fake_podman import FakePodman

DIRECT_TARGET = ExecTarget(realm="test", services=False, mutating=False)

SHIPPED = ("arch", "debian", "fedora", "kali")
PACKAGE_VERBS = (Verb.INSTALL, Verb.REMOVE, Verb.REMOVE_CHECK, Verb.RESOLVE, Verb.ESTIMATE)
CHECK_TMP = "/srv/ginn-check-0001"
DIGEST = "registry.example/debian@sha256:" + "ab" * 32
REALM = "arch"
# Substrings that turn off a native signature check (FR-012 AC-2). Matched against each argv
# element in lower case. None of them is needed by any verb, and none may appear in one.
SIGNATURE_BYPASS = (
    "nogpgcheck",
    "gpgcheck=0",
    "allow-unauthenticated",
    "allowunauthenticated",
    "allow-insecure",
    "allowinsecure",
    "force-yes",
    "siglevel",
    "no-gpg",
    "--nosig",
    "ignore-signature",
    "skip-signature",
    "allow-untrusted",
    "--force-bad-verify",
    "gpgcheck",
    "trusted=yes",
    "no-check-sig",
)
APT_SIMULATE = (
    "NOTE: This is only a simulation!\n"
    "The following packages will be REMOVED:\n"
    "  gimp libgimp2.0\n"
    "0 upgraded, 0 newly installed, 2 to remove and 0 not upgraded.\n"
    "Remv gimp [2.10.38-1]\n"
    "Remv libgimp2.0 [2.10.38-1]\n"
)
DNF_TRANSACTION = (
    "Dependencies resolved.\n"
    "Removing:\n"
    " htop     x86_64  3.3-1.fc42  @System  1.2 M\n"
    "Removing dependent packages:\n"
    " gimp     x86_64  2.10-1.fc42  @System  9.0 M\n"
    "\n"
    "Transaction Summary\n"
    "Remove  2 Packages\n"
)


def _definition(name: str) -> DistroDef:
    return load(name)


def _argvs(name: str) -> list[tuple[str, list[str]]]:
    """Every verb argv (with a sample package), then every check-updates step, labelled."""
    definition = _definition(name)
    out: list[tuple[str, list[str]]] = []
    for verb in Verb:
        if verb is Verb.CHECK_UPDATES:
            continue
        if verb is Verb.REMOVE_CHECK and not definition.has_remove_check:
            continue
        if verb is Verb.DB_CHECK and not definition.has_db_check:
            continue
        packages = ["gimp"] if verb in PACKAGE_VERBS else []
        out.append((verb.value, definition.argv(verb, packages)))
    tmpdir = CHECK_TMP if definition.check_updates_needs_tmpdir else None
    for index, step in enumerate(definition.check_updates(tmpdir)):
        out.append((f"check-updates[{index}]", list(step)))
    if name == "arch":
        out.extend(_aur_argvs())
    return out


def _aur_argvs() -> list[tuple[str, list[str]]]:
    """The argv of the AUR build (packages/aur.py), scanned with the verbs (FR-012)."""
    return [
        ("aur:clone", clone_argv("yay-bin")),
        ("aur:srcinfo", srcinfo_argv()),
        ("aur:build", build_argv()),
        ("aur:packagelist", packagelist_argv()),
        ("aur:upgrade", upgrade_argv([f"{BUILD_ROOT}/yay-bin/a.pkg.tar.zst"])),
        ("aur:query", query_argv()),
        ("aur:prepare", prepare_argvs(1000, 1000)[1]),
        ("aur:deps", install_deps_argv(["zlib"])),
        ("aur:info", info_argv()),
        ("aur:cleanup", cleanup_argv()),
    ]


def _running(fake: FakePodman, realm: str = REALM) -> str:
    labels = {LABEL_REALM: realm}
    spec = CreateSpec(
        name=f"ginn-{realm}-g1", image_digest_ref=DIGEST, network="host", labels=labels
    )
    cid = fake.create(spec)
    fake.start(cid)
    fake.exec_calls.clear()
    return cid


def _canned(
    fake: FakePodman, argv: Sequence[str], exit_code: int, stdout: str, stderr: str = ""
) -> None:
    fake.exec_results[tuple(argv)] = ExecResult(exit_code, stdout, stderr)


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", SHIPPED)
def test_every_package_verb_ends_in_its_names_as_separate_argv_elements(name: str) -> None:
    definition = _definition(name)
    names = ["gimp", "htop"]
    for verb in PACKAGE_VERBS:
        if verb is Verb.REMOVE_CHECK and not definition.has_remove_check:
            continue
        argv = definition.argv(verb, names)
        assert all(isinstance(element, str) for element in argv), (name, verb)
        assert argv[-2:] == names, (name, verb, argv)


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("DR-003")
@pytest.mark.parametrize("raw", ["gimp;rm", "gimp htop", "$(id)", "gimp*"])
def test_a_name_with_shell_characters_never_reaches_an_argv(raw: str) -> None:
    definition = _definition("debian")
    with pytest.raises(GinnError) as caught:
        definition.argv(Verb.INSTALL, [raw])
    assert caught.value.code == "GINN-E-PKG-NAME-INVALID"


@pytest.mark.fr("FR-012")
@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", SHIPPED)
def test_no_verb_or_check_step_carries_a_signature_bypass(name: str) -> None:
    for label, argv in _argvs(name):
        text = " ".join(argv).lower()
        for token in SIGNATURE_BYPASS:
            assert token not in text, (name, label, token)


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize(
    ("name", "stderr"),
    [
        ("arch", 'error: key "ABC" could not be looked up remotely'),
        ("arch", "error: 'x' signature from 'Alice <a@x>' is unknown trust"),
        ("debian", "W: GPG error: ... NO_PUBKEY 0123ABCD"),
        ("debian", "The following signatures were invalid: BADSIG"),
        ("fedora", "Errors during downloading metadata: GPG check failed"),
        ("kali", "E: The repository is not signed"),
    ],
)
def test_a_signature_error_maps_to_pkg_signature(name: str, stderr: str) -> None:
    err = failure(_definition(name), ExecResult(1, "", stderr), realm=REALM, step="install")
    assert err.code == "GINN-E-PKG-SIGNATURE"
    assert err.exit_code == 65
    assert err.details["step"] == "install"
    assert err.details["exit"] == 1
    assert err.details["detail"] == stderr


@pytest.mark.fr("FR-040")
def test_any_other_failure_maps_to_pkg_manager() -> None:
    err = failure(
        _definition("debian"),
        ExecResult(100, "", "E: Unable to locate package gimpx"),
        realm=REALM,
        step="install",
    )
    assert err.code == "GINN-E-PKG-MANAGER"
    assert err.exit_code == 1
    assert "stale_metadata" not in err.details


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-120")
def test_a_stale_metadata_match_is_noted_in_the_error() -> None:
    stderr = "failed retrieving file 'core.db' from mirror: The requested URL returned error: 404"
    err = failure(_definition("arch"), ExecResult(1, "", stderr), realm=REALM, step="install")
    assert err.code == "GINN-E-PKG-MANAGER"
    assert err.details["stale_metadata"] is True


@pytest.mark.fr("FR-040")
def test_the_decisive_line_is_the_first_non_blank_stderr_line() -> None:
    assert decisive_line(ExecResult(1, "out", "\n\n  first  \nsecond")) == "first"
    assert decisive_line(ExecResult(1, "  out\n", "")) == "out"
    assert decisive_line(ExecResult(1, "", "")) == "no output"


@pytest.mark.fr("FR-042")
@pytest.mark.fr("NFR-SEC-005")
def test_run_step_runs_the_argv_as_container_root_under_the_locale() -> None:
    fake = FakePodman()
    cid = _running(fake)
    definition = _definition("arch")
    argv = definition.argv(Verb.INSTALL, ["htop"])
    _canned(fake, argv, 0, "")
    result = run_step(
        fake,
        cid,
        definition,
        argv,
        realm=REALM,
        step="install",
        retry_hook=None,
        target=DIRECT_TARGET,
    )
    assert result.exit_code == 0
    call = fake.exec_calls[-1]
    assert call.argv == tuple(argv)
    assert call.user == ROOT_USER == "0:0"
    assert call.env == dict(LOCALE) == {"LC_ALL": "C.UTF-8"}
    assert call.pass_env == ()


@pytest.mark.fr("FR-040")
def test_run_step_raises_the_mapped_error_on_a_non_zero_status() -> None:
    fake = FakePodman()
    cid = _running(fake)
    definition = _definition("debian")
    argv = definition.argv(Verb.INSTALL, ["gimp"])
    _canned(fake, argv, 100, "", "E: Unable to locate package gimp")
    with pytest.raises(GinnError) as caught:
        run_step(
            fake,
            cid,
            definition,
            argv,
            realm=REALM,
            step="install",
            retry_hook=None,
            target=DIRECT_TARGET,
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["step"] == "install"


@pytest.mark.fr("FR-120")
def test_a_network_failure_is_offered_to_the_retry_hook_and_raised_when_refused() -> None:
    fake = FakePodman(fail_exec_with="GINN-E-NETWORK")
    cid = _running(fake)
    definition = _definition("fedora")
    argv = definition.argv(Verb.INSTALL, ["gimp"])
    attempts: list[int] = []

    def refuse(attempt: int, err: GinnError) -> bool:
        attempts.append(attempt)
        return False

    with pytest.raises(GinnError) as caught:
        run_step(
            fake,
            cid,
            definition,
            argv,
            realm=REALM,
            step="install",
            retry_hook=refuse,
            target=DIRECT_TARGET,
        )
    assert caught.value.code == "GINN-E-NETWORK"
    assert attempts == [1]


@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-042")
@pytest.mark.parametrize(
    ("name", "stdout"),
    [
        ("arch", "htop\nvim\n\n"),
        ("debian", "gimp\nhtop\n"),
        ("fedora", "gimp\nhtop\n"),
        ("kali", "htop\n"),
    ],
)
def test_explicit_packages_reads_the_list_verb(name: str, stdout: str) -> None:
    fake = FakePodman()
    cid = _running(fake)
    definition = _definition(name)
    _canned(fake, definition.argv(Verb.LIST_EXPLICIT), 0, stdout)
    names = explicit_packages(fake, cid, definition, realm=REALM, target=DIRECT_TARGET)
    assert names == parse_names(stdout, realm=REALM)
    assert fake.exec_calls[-1].env == {"LC_ALL": "C.UTF-8"}
    assert fake.exec_calls[-1].user == "0:0"


@pytest.mark.fr("DR-007")
def test_explicit_packages_raises_rather_than_return_an_empty_list() -> None:
    fake = FakePodman()
    cid = _running(fake)
    definition = _definition("arch")
    _canned(fake, definition.argv(Verb.LIST_EXPLICIT), 1, "", "error: database is locked")
    with pytest.raises(GinnError) as caught:
        explicit_packages(fake, cid, definition, realm=REALM, target=DIRECT_TARGET)
    assert caught.value.code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-041")
def test_parse_removals_reads_the_apt_simulate_lines_and_the_list_under_them() -> None:
    assert parse_removals(APT_SIMULATE, realm=REALM) == frozenset({"gimp", "libgimp2.0"})


@pytest.mark.fr("FR-041")
def test_parse_removals_reads_the_dnf_transaction_sections() -> None:
    assert parse_removals(DNF_TRANSACTION, realm=REALM) == frozenset({"htop", "gimp"})


@pytest.mark.fr("FR-041")
def test_parse_removals_reads_nothing_from_the_summary_lines() -> None:
    text = "Remv gimp:i386 [1.0]\n0 upgraded, 0 newly installed, 1 to remove\nDone.\n"
    assert parse_removals(text, realm=REALM) == frozenset({"gimp"})
    summary = "0 upgraded, 0 newly installed, 0 to remove\n"
    assert parse_removals(summary, realm=REALM) == frozenset()


@pytest.mark.fr("FR-041")
def test_removal_dependents_lists_only_the_packages_beyond_the_request() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    definition = _definition("debian")
    _canned(fake, definition.argv(Verb.REMOVE_CHECK, ["gimp"]), 0, APT_SIMULATE)
    assert removal_dependents(
        fake, cid, definition, ["gimp"], realm="deb", target=DIRECT_TARGET
    ) == frozenset({"libgimp2.0"})


@pytest.mark.fr("FR-041")
def test_removal_dependents_is_empty_when_only_the_request_is_removed() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    definition = _definition("debian")
    text = "Remv htop [3.3]\n0 upgraded, 0 newly installed, 1 to remove\n"
    _canned(fake, definition.argv(Verb.REMOVE_CHECK, ["htop"]), 0, text)
    assert (
        removal_dependents(fake, cid, definition, ["htop"], realm="deb", target=DIRECT_TARGET)
        == frozenset()
    )


@pytest.mark.fr("FR-041")
def test_removal_dependents_reads_the_dnf_dry_run_although_dnf_exits_one() -> None:
    fake = FakePodman()
    cid = _running(fake, "fed")
    definition = _definition("fedora")
    _canned(fake, definition.argv(Verb.REMOVE_CHECK, ["htop"]), 1, DNF_TRANSACTION)
    assert removal_dependents(
        fake, cid, definition, ["htop"], realm="fed", target=DIRECT_TARGET
    ) == frozenset({"gimp"})


@pytest.mark.fr("FR-041")
def test_removal_refuses_an_unread_plan_that_does_not_list_the_request() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    definition = _definition("debian")
    _canned(fake, definition.argv(Verb.REMOVE_CHECK, ["gimp"]), 0, "E: some other text\n")
    with pytest.raises(GinnError) as caught:
        removal_dependents(fake, cid, definition, ["gimp"], realm="deb", target=DIRECT_TARGET)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["step"] == "remove-check"
    assert "gimp" in caught.value.details["detail"]


@pytest.mark.fr("FR-041")
@pytest.mark.fr("NFR-SEC-005")
def test_the_dry_run_runs_as_root_under_the_locale_and_never_as_a_shell() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    definition = _definition("debian")
    argv = definition.argv(Verb.REMOVE_CHECK, ["gimp"])
    _canned(fake, argv, 0, APT_SIMULATE)
    removal_dependents(fake, cid, definition, ["gimp"], realm="deb", target=DIRECT_TARGET)
    call = fake.exec_calls[-1]
    assert call.argv == tuple(argv)
    assert call.argv[0] == "apt-get"
    assert call.user == "0:0"
    assert call.env == {"LC_ALL": "C.UTF-8"}


# ---- T5.1 review fixes: bounds, apt headings, network classification, escaping -------------

DEBIAN_NETWORK = (
    "E: Failed to fetch http://deb.debian.org/debian/pool/main/g/gimp/gimp.deb  "
    "Temporary failure resolving 'deb.debian.org'\n"
    "E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?\n"
)
DEBIAN_SIGNATURE_AND_NETWORK = (
    "W: GPG error: http://deb.debian.org stable InRelease: NO_PUBKEY 0123ABCD\n"
    "E: Failed to fetch http://deb.debian.org/debian/x  Temporary failure resolving 'x'\n"
)
ARCH_NETWORK = (
    "error: failed retrieving file 'core.db' from mirror.example.org : "
    "Could not resolve host: mirror.example.org\n"
)
ARCH_STALE = (
    "error: failed retrieving file 'core.db' from mirror.example.org : "
    "The requested URL returned error: 404\n"
)
FEDORA_NETWORK = (
    "Curl error (6): Couldn't resolve host name for "
    "https://mirror.example.org/fedora/repodata/repomd.xml [Could not resolve host]\n"
)
APT_NO_REMV = (
    "The following packages will be REMOVED:\n"
    "  gimp libgimp2.0\n"
    "0 upgraded, 0 newly installed, 2 to remove and 0 not upgraded.\n"
)
APT_TWO_ON_ONE_ROW = "The following packages will be REMOVED:\n  gimp libgimp2.0\nRemv gimp [1]\n"


@pytest.mark.fr("FR-040")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "text",
    [
        "removing " * 200_000 + ":\n",
        "x" * 2_000_000,
        "Remv gimp " + "y" * 2_000_000 + "\n",
    ],
)
def test_a_hostile_two_megabyte_line_is_refused_quickly_and_never_scanned(text: str) -> None:
    start = time.perf_counter()
    with pytest.raises(GinnError) as caught:
        parse_removals(text, realm=REALM)
    elapsed = time.perf_counter() - start
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert "over" in caught.value.details["detail"]
    assert elapsed < 1.0, elapsed


@pytest.mark.fr("FR-040")
def test_a_long_line_under_the_cap_is_scanned_in_linear_time() -> None:
    text = "removing " * (MAX_LINE_CHARS // len("removing ")) + ":"
    start = time.perf_counter()
    assert parse_removals(text, realm=REALM) == frozenset()
    assert time.perf_counter() - start < 1.0


@pytest.mark.fr("FR-040")
def test_output_over_the_total_cap_is_refused_before_any_line_is_read() -> None:
    with pytest.raises(GinnError) as caught:
        parse_names("a\n" * (MAX_OUTPUT_CHARS // 2 + 1), realm=REALM)
    assert caught.value.code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-042")
def test_a_list_over_the_name_cap_is_refused_and_one_at_the_cap_is_read() -> None:
    at_cap = "\n".join(f"p{index}" for index in range(MAX_NAMES))
    assert len(parse_names(at_cap, realm=REALM)) == MAX_NAMES
    over = "\n".join(f"p{index}" for index in range(MAX_NAMES + 1))
    with pytest.raises(GinnError) as caught:
        parse_names(over, realm=REALM)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert "more than" in caught.value.details["detail"]


@pytest.mark.fr("FR-041")
def test_an_apt_heading_with_no_remv_line_is_refused_not_read_as_no_dependents() -> None:
    with pytest.raises(GinnError) as caught:
        parse_removals(APT_NO_REMV, realm=REALM)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert "no Remv line" in caught.value.details["detail"]


@pytest.mark.fr("FR-041")
def test_every_word_of_an_apt_row_is_a_name_even_without_its_own_remv_line() -> None:
    assert parse_removals(APT_TWO_ON_ONE_ROW, realm=REALM) == frozenset({"gimp", "libgimp2.0"})


@pytest.mark.fr("FR-041")
def test_a_dependent_on_an_apt_row_is_reported_by_removal_dependents() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    definition = _definition("debian")
    _canned(fake, definition.argv(Verb.REMOVE_CHECK, ["gimp"]), 0, APT_TWO_ON_ONE_ROW)
    dependents = removal_dependents(
        fake, cid, definition, ["gimp"], realm="deb", target=DIRECT_TARGET
    )
    assert dependents == frozenset({"libgimp2.0"})


@pytest.mark.fr("FR-120")
def test_a_realistic_network_failure_is_retried_with_the_delays_then_raised_as_network() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    definition = _definition("debian")
    argv = definition.argv(Verb.INSTALL, ["gimp"])
    _canned(fake, argv, 100, "", DEBIAN_NETWORK)
    attempts: list[int] = []

    def retry(attempt: int, err: GinnError) -> bool:
        attempts.append(attempt)
        return True

    with pytest.raises(GinnError) as caught:
        run_step(
            fake,
            cid,
            definition,
            argv,
            realm=REALM,
            step="install",
            retry_hook=retry,
            target=DIRECT_TARGET,
        )
    assert caught.value.code == "GINN-E-NETWORK"
    assert caught.value.exit_code == 69
    assert attempts == [1, 2, 3]
    assert len(fake.exec_calls) == 4
    assert caught.value.details["step"] == "install"
    assert caught.value.details["exit"] == 100


@pytest.mark.fr("FR-012")
@pytest.mark.fr("FR-120")
def test_a_signature_error_wins_over_network_text_and_is_not_retried() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    definition = _definition("debian")
    argv = definition.argv(Verb.INSTALL, ["gimp"])
    _canned(fake, argv, 100, "", DEBIAN_SIGNATURE_AND_NETWORK)
    attempts: list[int] = []
    with pytest.raises(GinnError) as caught:
        run_step(
            fake,
            cid,
            definition,
            argv,
            realm=REALM,
            step="install",
            retry_hook=lambda attempt, err: attempts.append(attempt) is None,
            target=DIRECT_TARGET,
        )
    assert caught.value.code == "GINN-E-PKG-SIGNATURE"
    assert attempts == []
    assert len(fake.exec_calls) == 1


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-120")
def test_a_stale_metadata_404_is_not_a_network_failure_and_is_not_retried() -> None:
    fake = FakePodman()
    cid = _running(fake)
    definition = _definition("arch")
    argv = definition.argv(Verb.INSTALL, ["htop"])
    _canned(fake, argv, 1, "", ARCH_STALE)
    attempts: list[int] = []
    with pytest.raises(GinnError) as caught:
        run_step(
            fake,
            cid,
            definition,
            argv,
            realm=REALM,
            step="install",
            retry_hook=lambda attempt, err: attempts.append(attempt) is None,
            target=DIRECT_TARGET,
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["stale_metadata"] is True
    assert attempts == []


@pytest.mark.fr("FR-120")
@pytest.mark.parametrize(
    ("name", "stderr", "expected"),
    [
        ("debian", DEBIAN_NETWORK, True),
        ("debian", "E: Unable to locate package gimpx\n", False),
        ("arch", ARCH_NETWORK, True),
        ("arch", ARCH_STALE, False),
        ("fedora", FEDORA_NETWORK, True),
        ("fedora", "Status code: 404 for https://mirror.example.org/x.rpm\n", False),
        ("kali", "E: Failed to fetch http://x  Connection timed out\n", True),
    ],
)
def test_network_failure_recognises_the_transient_texts_and_only_those(
    name: str, stderr: str, expected: bool
) -> None:
    result = ExecResult(1, "", stderr)
    assert network_failure(_definition(name), result) is expected


@pytest.mark.fr("FR-120")
def test_a_zero_status_is_never_a_network_failure() -> None:
    assert network_failure(_definition("arch"), ExecResult(0, "", ARCH_NETWORK)) is False


@pytest.mark.fr("NFR-040")
def test_control_characters_in_a_name_are_written_escaped_and_cut() -> None:
    text = printable("ev\x1b[31mil\x00\x07")
    assert all(ch.isprintable() for ch in text)
    assert "\\x1b" in text and "\\x00" in text and "\\x07" in text
    assert printable("a" * 500).endswith("...")
    assert len(printable("a" * 500)) <= 67


@pytest.mark.fr("NFR-040")
def test_a_list_of_names_shows_twenty_escaped_and_counts_the_rest() -> None:
    names = [f"n{index}" for index in range(MAX_SHOWN + 5)]
    text = shown(names)
    assert text.endswith(" and 5 more")
    assert text.count(",") == MAX_SHOWN - 1
    assert "\x1b" not in shown(["ev\x1b[0mil"])
    assert "\\x1b" in shown(["ev\x1b[0mil"])


@pytest.mark.fr("NFR-040")
def test_the_decisive_line_of_a_failure_is_escaped() -> None:
    line = decisive_line(ExecResult(1, "", "bad\x1b[2J line"))
    assert "\x1b" not in line
    assert "bad" in line


# ---- check output and the db-check, read from the data: FR-024, FR-120 AC-2, NFR-060 ----------

DATA = Path(__file__).resolve().parents[2] / "src" / "ginn" / "distros" / "data"
DEB_LISTING = "gimp 2.10-1 -> 2.10-2\nzlib 1.3-1 -> 1.3-2\n"
DNF_LISTING = "bash.x86_64    5.2    updates\n"


def _edited(name: str, *, drop: Sequence[str] = (), **keys: Any) -> DistroDef:
    """The shipped `name` definition with some top-level keys replaced or dropped, validated."""
    raw: dict[str, Any] = tomllib.loads((DATA / f"{name}.toml").read_text(encoding="utf-8"))
    for key in drop:
        del raw[key]
    raw.update(keys)
    return from_mapping(raw, source=f"{name}-edited.toml")


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
def test_the_count_rule_is_the_data_and_not_the_distribution_id() -> None:
    fake = FakePodman()
    cid = _running(fake, "deb")
    _canned(fake, ("apt", "list", "--upgradable"), 0, DEB_LISTING)
    # Without a `[upgradable from:` mark, Debian's own rule counts nothing. The same listing,
    # counted line by line by the data, is two updates. The id plays no part in either count.
    shipped = run_check(fake, cid, load("debian"), realm="deb", target=DIRECT_TARGET)
    assert shipped == CheckResult(UpdateStatus.NONE, 0)
    renamed = _edited(
        "debian", drop=("check_updates_count_regex",), check_updates_count="nonblank-lines"
    )
    assert run_check(fake, cid, renamed, realm="deb", target=DIRECT_TARGET) == CheckResult(
        UpdateStatus.UPDATES, 2
    )


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
def test_the_exit_status_that_means_updates_is_read_from_the_data_only() -> None:
    fake = FakePodman()
    cid = _running(fake, "fed")
    _canned(fake, ("dnf", "--refresh", "check-upgrade"), 100, DNF_LISTING)
    assert run_check(fake, cid, load("fedora"), realm="fed", target=DIRECT_TARGET) == CheckResult(
        UpdateStatus.UPDATES, None
    )
    # Without the key, exit 100 is a failure, as it is for apt (FR-040).
    without = _edited("fedora", drop=("check_updates_exit_for_updates",))
    with pytest.raises(GinnError) as caught:
        run_check(fake, cid, without, realm="fed", target=DIRECT_TARGET)
    assert caught.value.code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-120")
def test_a_consistent_database_is_not_inconsistent_and_runs_as_root_under_the_locale() -> None:
    fake = FakePodman()
    cid = _running(fake, REALM)
    definition = _definition("arch")
    _canned(fake, definition.argv(Verb.DB_CHECK), 0, "")
    assert database_inconsistent(fake, cid, definition, realm=REALM, target=DIRECT_TARGET) is False
    call = fake.exec_calls[-1]
    assert call.argv == tuple(definition.argv(Verb.DB_CHECK))
    assert call.user == "0:0"
    assert call.env == {"LC_ALL": "C.UTF-8"}


@pytest.mark.fr("FR-120")
@pytest.mark.parametrize(
    ("name", "exit_code", "stdout"),
    [
        ("arch", 1, ""),
        ("fedora", 1, ""),
        ("debian", 0, "gimp: broken\n"),
        ("kali", 0, "gimp: broken\n"),
    ],
)
def test_an_inconsistent_database_is_read_as_the_data_names_it(
    name: str, exit_code: int, stdout: str
) -> None:
    fake = FakePodman()
    cid = _running(fake, REALM)
    definition = _definition(name)
    _canned(fake, definition.argv(Verb.DB_CHECK), exit_code, stdout)
    assert database_inconsistent(fake, cid, definition, realm=REALM, target=DIRECT_TARGET) is True


@pytest.mark.fr("FR-120")
@pytest.mark.parametrize(
    ("name", "stdout", "expected"), [("arch", "text\n", False), ("debian", "text\n", True)]
)
def test_stdout_counts_only_where_the_data_says_it_does(
    name: str, stdout: str, expected: bool
) -> None:
    fake = FakePodman()
    cid = _running(fake, REALM)
    definition = _definition(name)
    _canned(fake, definition.argv(Verb.DB_CHECK), 0, stdout)
    assert (
        database_inconsistent(fake, cid, definition, realm=REALM, target=DIRECT_TARGET) is expected
    )


@pytest.mark.fr("FR-120")
@pytest.mark.parametrize("exit_code", [125, 126, 127])
def test_a_check_that_podman_could_not_run_raises_and_never_reads_as_broken(
    exit_code: int,
) -> None:
    fake = FakePodman()
    cid = _running(fake, REALM)
    definition = _definition("arch")
    _canned(fake, definition.argv(Verb.DB_CHECK), exit_code, "", "OCI runtime error\n")
    with pytest.raises(GinnError) as caught:
        database_inconsistent(fake, cid, definition, realm=REALM, target=DIRECT_TARGET)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["step"] == "db-check"
    assert caught.value.details["exit"] == exit_code


@pytest.mark.fr("FR-120")
def test_a_distribution_without_a_db_check_verb_is_never_inconsistent_and_runs_nothing() -> None:
    fake = FakePodman()
    cid = _running(fake, REALM)
    assert (
        database_inconsistent(fake, cid, load("_test"), realm=REALM, target=DIRECT_TARGET) is False
    )
    assert fake.exec_calls == []


@pytest.mark.fr("FR-120")
def test_db_check_output_over_the_cap_is_refused_and_not_read() -> None:
    fake = FakePodman()
    cid = _running(fake, REALM)
    definition = _definition("debian")
    _canned(fake, definition.argv(Verb.DB_CHECK), 0, "x" * (MAX_OUTPUT_CHARS + 1))
    with pytest.raises(GinnError) as caught:
        database_inconsistent(fake, cid, definition, realm=REALM, target=DIRECT_TARGET)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
