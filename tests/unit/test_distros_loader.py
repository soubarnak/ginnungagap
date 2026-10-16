# SPDX-License-Identifier: Apache-2.0
"""Distribution data and loader: NFR-060 (data only), DR-003, DR-005, FR-012, FR-013, FR-023,
FR-024, FR-040, FR-041, NFR-SEC-005.

The shipped files are checked as data: their Integration Package Set against S-10 (the distro TOML
is the single source for it), their verbs for non-interactive and signature-preserving flags, their
metadata refresh rules (ADR-011, which carries FR-023's "never updated automatically" into the verb
data), and their name patterns against the DR-003 table in this file. The `_test` fixture and
renamed copies prove that a new distribution needs no code change, both in the loader and through
domain validation. The fixture switch and the definition cache are tested here too. Every
validation rule in loader.py has a test that breaks it.
"""

import copy
import re
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import loader
from ginn.distros.loader import (
    TEST_SWITCH_ENV,
    CountRule,
    DbStatus,
    DistroDef,
    HostLocale,
    UpdateStatus,
    Verb,
    arch_check_status,
    available,
    clear_cache,
    db_check_status,
    distro_fixtures_enabled,
    from_mapping,
    host_locale,
    load,
    load_file,
    parse_text,
)
from ginn.domain import DistroId, PackageName, RealmName, RealmSettings, Tier
from ginn.errors import GinnError
from ginn.ops.provision import storage_required_bytes

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "src" / "ginn" / "distros" / "data"
SHIPPED = ("arch", "debian", "fedora", "kali")
SHIPPED_LIST = ", ".join(SHIPPED)
# DR-003 name patterns, copied from the SRS. The shipped TOML files must carry these.
DR003_PATTERNS = {
    "arch": r"[a-z0-9@_+][a-z0-9@._+-]{0,127}",
    "debian": r"[a-z0-9][a-z0-9.+-]{1,127}",
    "fedora": r"(?!.*\.rpm\Z)[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}",
    "kali": r"[a-z0-9][a-z0-9.+-]{1,127}",
}
PACKAGE_VERBS = (Verb.INSTALL, Verb.REMOVE, Verb.RESOLVE, Verb.ESTIMATE)
# Distributions whose data defines remove-check (ADR-011). Arch refuses dependents natively, and the
# fixture omits the verb to keep the optional path tested.
REMOVE_CHECK_DISTROS = ("debian", "fedora", "kali")
# dpkg and rpm are the database tools of the db-check verb (FR-120 AC-2), not package managers.
PKG_MANAGERS = frozenset(
    {"pacman", "apt-get", "apt", "apt-mark", "apt-cache", "dnf", "dpkg", "rpm"}
)
# Tools a check-updates step may run besides a package manager: Arch makes its scratch path with
# mkdir and links the installed-package database into it with ln (arch.toml).
SCRATCH_TOOLS = frozenset({"mkdir", "ln"})
# Tools a clean step may run besides a package manager: rm empties apt's list directory and find
# empties the arch package cache (debian.toml, arch.toml). mkdir is in SCRATCH_TOOLS already.
CLEAN_TOOLS = frozenset({"rm", "find"})
# The scratch path a caller would pass to check_updates(). The loader never creates it.
CHECK_TMP = "/srv/ginn-check-0001"
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "-c"})
# Tokens that turn off a native signature check (FR-012). None is needed for any verb.
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
)
YES_FLAGS = frozenset({"--noconfirm", "--yes", "--assumeyes", "-y"})
# Tokens that upgrade installed packages. Install, resolve and estimate argv need none (FR-040).
UPGRADE_TOKENS = frozenset({"--sysupgrade", "--refresh", "upgrade", "dist-upgrade", "full-upgrade"})
# Tokens that widen a removal beyond the named package (FR-041). `--no-remove` is here too: apt-get
# aborts on any removal with it, the named one included (apt-get(8)), so it cannot express the rule.
REMOVE_WIDENERS = frozenset(
    {
        "-s",
        "-c",
        "-d",
        "--recursive",
        "--cascade",
        "--nodeps",
        "--auto-remove",
        "--autoremove",
        "--purge",
        "--force-depends",
        "--no-remove",
    }
)
DEBIAN_REFRESHED = frozenset(
    {Verb.INSTALL, Verb.UPGRADE, Verb.CHECK_UPDATES, Verb.RESOLVE, Verb.ESTIMATE}
)
# The verbs a refresh may not precede: every verb but the five that read the sync data (ADR-011).
REFRESH_REFUSED = sorted(verb.value for verb in Verb if verb not in DEBIAN_REFRESHED)
NAMES = ["a;b", "$(id)", "x y", "pkg{lang}", "`id`", "a&&b", "*", "gimp|cat"]


def _base() -> dict[str, Any]:
    """A fresh copy of the `_test` table, to be broken one field at a time."""
    raw: dict[str, Any] = tomllib.loads((DATA / "_test.toml").read_text(encoding="utf-8"))
    return raw


def _shipped_text(name: str) -> str:
    return (DATA / f"{name}.toml").read_text(encoding="utf-8")


def _expect(code: str, exit_code: int) -> Callable[[GinnError], None]:
    def check(err: GinnError) -> None:
        assert err.code == code
        assert err.exit_code == exit_code

    return check


def _check_steps_of(name: str) -> list[list[str]]:
    """The check-updates steps of a shipped distribution, with CHECK_TMP when it takes one."""
    definition = load(name)
    tmpdir = CHECK_TMP if definition.check_updates_needs_tmpdir else None
    return [list(step) for step in definition.check_updates(tmpdir)]


def _every_argv(name: str) -> list[tuple[str, list[str]]]:
    """Each verb's argv (check-updates and clean excluded), then each check-updates and clean step,
    labelled."""
    definition = load(name)
    out: list[tuple[str, list[str]]] = []
    for verb in Verb:
        if verb in (Verb.CHECK_UPDATES, Verb.CLEAN):
            continue
        if verb is Verb.REMOVE_CHECK and not definition.has_remove_check:
            continue
        if verb is Verb.DB_CHECK and not definition.has_db_check:
            continue
        packages = ["gimp"] if verb in (*PACKAGE_VERBS, Verb.REMOVE_CHECK) else []
        out.append((verb.value, definition.argv(verb, packages)))
    for index, step in enumerate(_check_steps_of(name)):
        out.append((f"check-updates[{index}]", step))
    for index, step in enumerate(definition.clean_steps):
        out.append((f"clean[{index}]", list(step)))
    return out


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    """Start and end each test with an empty definition cache, so monkeypatched dirs are read."""
    clear_cache()
    yield
    clear_cache()


# ---- shipped data: DR-005, NFR-060 -----------------------------------------------------------


@pytest.mark.fr("DR-005")
def test_available_lists_the_four_distributions_dr005_enumerates() -> None:
    assert available() == SHIPPED


@pytest.mark.fr("NFR-060")
def test_the_fixture_is_listed_only_with_include_test_and_the_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "_test" not in available()
    assert "_test" in available(include_test=True)  # the switch is on under tests/conftest.py
    monkeypatch.delenv(TEST_SWITCH_ENV)
    assert "_test" not in available(include_test=True)


@pytest.mark.fr("NFR-060")
def test_each_shipped_distribution_loads_with_every_verb() -> None:
    for name in SHIPPED:
        definition = load(name)
        assert definition.id == name
        assert definition.trust_key == name
        assert definition.shell == "/bin/bash"
        expected = set(Verb) - {Verb.CHECK_UPDATES, Verb.CLEAN}
        if name not in REMOVE_CHECK_DISTROS:
            expected.discard(Verb.REMOVE_CHECK)
        assert set(definition.verbs) == expected, name
        assert definition.check_steps


@pytest.mark.fr("NFR-060")
def test_test_fixture_loads_by_name() -> None:
    fixture = load("_test")
    assert fixture.id == "_test"
    assert fixture.argv(Verb.INSTALL, ["gimp"]) == ["fake-pm", "install", "gimp"]


@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("value", [None, "", "0", "yes", "true"])
def test_the_fixture_is_refused_unless_the_switch_is_exactly_one(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(TEST_SWITCH_ENV)
    else:
        monkeypatch.setenv(TEST_SWITCH_ENV, value)
    assert distro_fixtures_enabled() is False
    with pytest.raises(GinnError) as info:
        load("_test")
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == f"unknown distribution '_test'; available: {SHIPPED_LIST}"


@pytest.mark.fr("NFR-060")
def test_the_test_session_has_the_switch_on() -> None:
    assert distro_fixtures_enabled() is True


@pytest.mark.fr("NFR-060")
def test_a_renamed_copy_of_the_fixture_loads_with_zero_code_changes(tmp_path: Path) -> None:
    text = _shipped_text("_test").replace('id = "_test"', 'id = "fixture"')
    copy_path = tmp_path / "fixture.toml"
    copy_path.write_text(text, encoding="utf-8")
    definition = load_file(copy_path)
    assert definition.id == "fixture"
    assert definition.argv(Verb.REMOVE, ["gimp"]) == ["fake-pm", "remove", "gimp"]


@pytest.mark.fr("NFR-060")
def test_a_new_file_in_the_data_directory_is_discovered_without_code_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fixture.toml").write_text(
        _shipped_text("_test").replace('id = "_test"', 'id = "fixture"'), encoding="utf-8"
    )
    monkeypatch.setattr(loader, "_data_dir", lambda: tmp_path)
    assert available() == ("fixture",)
    assert load("fixture").id == "fixture"


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("DR-003")
@pytest.mark.fr("DR-005")
def test_a_new_definition_passes_domain_validation_with_zero_code_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the definition file says what this distribution's IDs and package names look like.
    (tmp_path / "fixture.toml").write_text(
        _shipped_text("_test").replace('id = "_test"', 'id = "fixture"'), encoding="utf-8"
    )
    monkeypatch.setattr(loader, "_data_dir", lambda: tmp_path)
    distro = DistroId.parse("fixture")
    assert PackageName.parse("fake-pm.1", distro).value == "fake-pm.1"
    with pytest.raises(GinnError) as name_info:
        PackageName.parse("Fake", distro)  # the fixture pattern starts with a lowercase letter
    assert name_info.value.code == "GINN-E-PKG-NAME-INVALID"
    settings = RealmSettings(
        name=RealmName.parse("work"),
        distro=distro,
        tier=Tier.BASIC,
        services=False,
        autostart=False,
        everything=False,
    )
    assert settings.distro == distro
    with pytest.raises(GinnError) as id_info:
        DistroId.parse("_test")  # not in this data directory any more
    assert id_info.value.code == "GINN-E-USAGE"


@pytest.mark.fr("NFR-060")
def test_a_file_whose_id_differs_from_its_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "arch.toml").write_text(
        _shipped_text("_test").replace('id = "_test"', 'id = "debian"'), encoding="utf-8"
    )
    monkeypatch.setattr(loader, "_data_dir", lambda: tmp_path)
    with pytest.raises(GinnError) as info:
        load("arch")
    assert info.value.code == "GINN-E-DISTRO-DATA"
    assert info.value.details["field"] == "id"


@pytest.mark.fr("DR-005")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", ["ubuntu", "Arch", "../arch", "", "arch/x", "-arch", "arch.toml"])
def test_unknown_or_unsafe_distribution_names_are_refused(name: str) -> None:
    with pytest.raises(GinnError) as info:
        load(name)
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == f"unknown distribution {name!r}; available: {SHIPPED_LIST}"


@pytest.mark.fr("NFR-060")
def test_verb_and_placeholder_syntax_round_trip_through_the_fedora_rpm_format() -> None:
    # rpm query formats use %{name}. Doubled braces keep them literal (loader.py docstring).
    fedora = load("fedora")
    assert fedora.argv(Verb.LIST_EXPLICIT) == [
        "dnf",
        "repoquery",
        "--userinstalled",
        "--queryformat",
        "%{name}\\n",
    ]


# ---- shipped data: DR-003 name patterns ------------------------------------------------------


@pytest.mark.fr("DR-003")
@pytest.mark.parametrize("name", SHIPPED)
def test_name_pattern_is_the_dr003_pattern_for_the_distribution(name: str) -> None:
    assert load(name).name_pattern.pattern == DR003_PATTERNS[name]


@pytest.mark.fr("DR-003")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("raw", ["a;b", "$(id)", "a b", "a*", "a|b", "a&b", "a`b`"])
def test_name_pattern_rejects_shell_metacharacters(name: str, raw: str) -> None:
    assert load(name).name_pattern.fullmatch(raw) is None


@pytest.mark.fr("DR-003")
@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("raw", ["firefox", "python3-pip", "gimp-1.2"])
def test_name_pattern_accepts_plain_names(name: str, raw: str) -> None:
    assert load(name).name_pattern.fullmatch(raw) is not None


# ---- shipped data: FR-013 Integration Package Set and locale steps ---------------------------

S10_STEPS = (("localedef", "-i", "en_US", "-f", "UTF-8", "en_US.UTF-8"),)


@pytest.mark.fr("FR-013")
@pytest.mark.fr("CR-007")
@pytest.mark.parametrize(
    ("name", "packages", "steps"),
    [
        ("arch", ("ca-certificates",), S10_STEPS),
        ("debian", ("ca-certificates", "locales"), S10_STEPS),
        ("kali", ("ca-certificates", "locales"), S10_STEPS),
        ("fedora", ("glibc-langpack-en",), ()),
    ],
)
def test_integration_set_for_en_us_matches_the_s10_findings(
    name: str, packages: tuple[str, ...], steps: tuple[tuple[str, ...], ...]
) -> None:
    result = load(name).integration_set("en_US.UTF-8")
    assert result.packages == packages
    assert result.steps == steps


@pytest.mark.fr("FR-013")
@pytest.mark.parametrize("lang", [None, "", "C", "POSIX", "C.UTF-8", "C.utf8"])
def test_c_and_unset_locales_drop_every_locale_entry(lang: str | None) -> None:
    debian = load("debian").integration_set(lang)
    assert debian.packages == ("ca-certificates", "locales")
    assert debian.steps == ()
    assert debian.fallback_reason is None
    fedora = load("fedora").integration_set(lang)
    assert fedora.packages == ()
    assert fedora.steps == ()
    assert fedora.fallback_reason is None


@pytest.mark.fr("FR-013")
def test_other_locales_substitute_the_language_and_the_locale_name() -> None:
    assert load("fedora").integration_set("de_DE.utf8").packages == ("glibc-langpack-de",)
    debian_steps = load("debian").integration_set("de_DE.UTF-8").steps
    assert debian_steps == (("localedef", "-i", "de_DE", "-f", "UTF-8", "de_DE.UTF-8"),)


@pytest.mark.fr("FR-013")
@pytest.mark.parametrize(
    "lang",
    [
        "en_US.ISO-8859-1",
        "en_US",
        "EN_us.UTF-8",
        "en_US.UTF-8@euro",
        "en_US.UTF-8\n",
        "C.ISO-8859-1",
        "en.UTF-8",
        "de.utf8",
    ],
)
def test_host_locales_that_cannot_be_used_fall_back_to_c_utf8_with_a_reason(lang: str) -> None:
    result = load("debian").integration_set(lang)
    assert result.lang == "C.UTF-8"
    assert result.packages == ("ca-certificates", "locales")
    assert result.steps == ()
    assert result.fallback_reason is not None
    assert repr(lang) in result.fallback_reason
    assert "C.UTF-8" in result.fallback_reason


@pytest.mark.fr("FR-013")
def test_a_usable_host_lang_is_the_realm_lang_with_no_fallback() -> None:
    for lang in ("en_US.UTF-8", "de_DE.utf8", "C", "POSIX", "C.UTF-8"):
        result = load("debian").integration_set(lang)
        assert result.lang == lang, lang
        assert result.fallback_reason is None, lang
    unset = load("arch").integration_set(None)
    assert unset.lang == "C.UTF-8"
    assert unset.fallback_reason is None


@pytest.mark.fr("FR-013")
def test_host_locale_reduces_lang_to_language_and_territory() -> None:
    assert host_locale("en_US.UTF-8") == (HostLocale("en", "en_US"), None)
    assert host_locale("de_DE.utf8") == (HostLocale("de", "de_DE"), None)
    assert host_locale("C.UTF-8") == (None, None)
    assert host_locale(None) == (None, None)
    for lang in ("en_US", "en.UTF-8"):
        locale, reason = host_locale(lang)
        assert locale is None, lang
        assert reason is not None and repr(lang) in reason and "territory" in reason


# ---- shipped data: FR-040, FR-041, FR-012 verbs ----------------------------------------------


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", SHIPPED)
def test_every_verb_is_an_argv_list_and_never_a_shell(name: str) -> None:
    for label, argv in _every_argv(name):
        assert argv, (name, label)
        assert all(isinstance(element, str) for element in argv)
        assert argv[0] in PKG_MANAGERS | SCRATCH_TOOLS | CLEAN_TOOLS, (name, label, argv[0])
        assert not SHELLS & set(argv), (name, label)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("name", SHIPPED)
def test_mutating_verbs_never_wait_for_a_prompt(name: str) -> None:
    definition = load(name)
    for verb in (Verb.INSTALL, Verb.REMOVE, Verb.UPGRADE):
        argv = definition.argv(verb, ["gimp"] if verb in PACKAGE_VERBS else [])
        assert YES_FLAGS & set(argv), (name, verb)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-024")
@pytest.mark.parametrize("name", SHIPPED)
def test_dry_run_verbs_never_carry_a_yes_flag(name: str) -> None:
    definition = load(name)
    for verb in (Verb.RESOLVE, Verb.ESTIMATE, Verb.LIST_EXPLICIT):
        argv = definition.argv(verb, ["gimp"] if verb in PACKAGE_VERBS else [])
        assert not YES_FLAGS & set(argv), (name, verb)
    for step in _check_steps_of(name):
        assert not YES_FLAGS & set(step), (name, step)


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize("name", SHIPPED)
def test_no_verb_disables_a_native_signature_check(name: str) -> None:
    for label, argv in _every_argv(name):
        text = " ".join(argv).lower()
        for token in SIGNATURE_BYPASS:
            assert token not in text, (name, label, token)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("CR-007")
@pytest.mark.parametrize(
    ("name", "flag"),
    [
        ("debian", "--no-install-recommends"),
        ("kali", "--no-install-recommends"),
        ("fedora", "--setopt=install_weak_deps=False"),
    ],
)
def test_apt_and_dnf_install_only_declared_dependencies(name: str, flag: str) -> None:
    assert flag in load(name).argv(Verb.INSTALL, ["gimp"])


# ---- refresh, upgrade and removal: ADR-011 (FR-023, FR-024, FR-040, FR-041) ------------------


@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", SHIPPED)
def test_refresh_is_a_package_manager_command(name: str) -> None:
    argv = load(name).argv(Verb.REFRESH)
    assert argv and argv[0] in PKG_MANAGERS, (name, argv)


@pytest.mark.fr("FR-040")
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("debian", ["apt-get", "update"]),
        ("kali", ["apt-get", "update"]),
        ("fedora", ["dnf", "--refresh", "makecache"]),
    ],
)
def test_refresh_fetches_the_metadata_each_manager_needs(name: str, expected: list[str]) -> None:
    assert load(name).argv(Verb.REFRESH) == expected


@pytest.mark.fr("FR-040")
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("arch", frozenset()),
        ("fedora", frozenset()),
        ("debian", DEBIAN_REFRESHED),
        ("kali", DEBIAN_REFRESHED),
        ("_test", frozenset({Verb.INSTALL})),
    ],
)
def test_refresh_before_lists_only_the_verbs_a_metadata_refresh_makes_safe(
    name: str, expected: frozenset[Verb]
) -> None:
    assert load(name).refresh_before == expected


@pytest.mark.fr("FR-023")
@pytest.mark.fr("NFR-060")
def test_refresh_before_accepts_every_verb_that_reads_the_sync_data() -> None:
    raw = _base()
    raw["refresh_before"] = sorted(verb.value for verb in DEBIAN_REFRESHED)
    assert from_mapping(raw, source="fixture.toml").refresh_before == DEBIAN_REFRESHED


@pytest.mark.fr("FR-023")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", REFRESH_REFUSED)
def test_refresh_before_refuses_a_verb_that_reads_no_sync_data(name: str) -> None:
    # A refresh only fetches the lists, so a verb that reads none gains nothing from it, and clean
    # empties them (ADR-011). The refusal holds even next to a valid entry, here install.
    raw = _base()
    raw["refresh_before"] = ["install", name]
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    assert info.value.code == "GINN-E-DISTRO-DATA"
    assert info.value.details["field"] == "refresh_before"
    assert name in info.value.details["rule"]


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-023")
def test_arch_never_refreshes_the_sync_database_outside_a_full_upgrade() -> None:
    # Arch does not support partial upgrades, so -Sy alone is never a refresh (Arch Wiki). The one
    # exception is the check-updates scratch copy, which the tests below pin to --dbpath.
    arch = load("arch")
    full = ["pacman", "--sync", "--refresh", "--sysupgrade", "--noconfirm"]
    assert arch.argv(Verb.REFRESH) == full
    assert arch.argv(Verb.UPGRADE) == full
    assert arch.refresh_before == frozenset()
    for label, argv in _every_argv("arch"):
        if not label.startswith("check-updates") and "--refresh" in argv:
            assert "--sysupgrade" in argv, (label, argv)


@pytest.mark.fr("FR-040")
def test_arch_install_uses_the_sync_database_as_the_last_full_upgrade_left_it() -> None:
    assert load("arch").argv(Verb.INSTALL, ["gimp"]) == [
        "pacman",
        "--sync",
        "--needed",
        "--noconfirm",
        "gimp",
    ]


@pytest.mark.fr("FR-024")
def test_arch_check_updates_is_four_steps_against_a_scratch_database() -> None:
    assert _check_steps_of("arch") == [
        ["mkdir", CHECK_TMP],
        ["ln", "-s", "-T", "/var/lib/pacman/local", f"{CHECK_TMP}/local"],
        ["pacman", "--sync", "--refresh", "--dbpath", CHECK_TMP, "--logfile", "/dev/null"],
        ["pacman", "--query", "--upgrades", "--dbpath", CHECK_TMP],
    ]


@pytest.mark.fr("FR-024")
def test_no_check_step_uses_mkdir_dash_p_so_a_leftover_path_fails_closed() -> None:
    # mkdir -p succeeds on an existing directory, so a leftover path would pass step 1 and let the
    # later steps write into it. Without -p, mkdir fails and the check stops (ADR-011).
    for name in SHIPPED:
        for step in _check_steps_of(name):
            if step[0] == "mkdir":
                assert "-p" not in step and "--parents" not in step, (name, step)


@pytest.mark.fr("FR-024")
def test_arch_link_step_carries_dash_t_so_ln_never_links_inside_a_directory() -> None:
    # Without -T, ln treats an existing directory named `local` as the target and creates the link
    # inside it. With -T the link is the name itself, and ln fails when that name exists.
    link = _check_steps_of("arch")[1]
    assert link[0] == "ln"
    assert "-T" in link, link


@pytest.mark.fr("FR-024")
def test_arch_check_updates_refreshes_only_the_scratch_copy() -> None:
    refreshing = [step for step in _check_steps_of("arch") if "--refresh" in step]
    assert len(refreshing) == 1
    step = refreshing[0]
    assert "--sysupgrade" not in step
    assert "--dbpath" in step
    assert step[step.index("--dbpath") + 1] == CHECK_TMP


@pytest.mark.fr("FR-024")
def test_arch_check_updates_never_touches_the_realm_sync_database() -> None:
    absolute = [
        element for step in _check_steps_of("arch") for element in step if element.startswith("/")
    ]
    assert [e for e in absolute if not e.startswith(CHECK_TMP)] == [
        "/var/lib/pacman/local",
        "/dev/null",
    ]
    assert not any("/var/lib/pacman/sync" in element for element in absolute)


@pytest.mark.fr("FR-024")
def test_arch_check_updates_has_no_short_flag_that_syncs_or_upgrades() -> None:
    cluster = re.compile(r"-[A-Za-z]*[yu][A-Za-z]*")  # -Sy, -Syu, -Qu and the like
    for step in _check_steps_of("arch"):
        for element in step:
            assert cluster.fullmatch(element) is None, step


@pytest.mark.fr("FR-024")
@pytest.mark.parametrize("name", SHIPPED)
def test_only_arch_check_updates_takes_a_scratch_directory(name: str) -> None:
    assert load(name).check_updates_needs_tmpdir is (name == "arch")


@pytest.mark.fr("FR-024")
def test_check_updates_refuses_a_scratch_directory_it_does_not_take() -> None:
    with pytest.raises(GinnError) as info:
        load("fedora").check_updates(CHECK_TMP)
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == "verb check-updates takes no tmpdir"


@pytest.mark.fr("FR-024")
def test_check_updates_needs_a_scratch_directory_where_the_data_uses_one() -> None:
    with pytest.raises(GinnError) as info:
        load("arch").check_updates()
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == "verb check-updates needs a tmpdir"


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("bad", ["", "relative/dir", "/", "/srv/../x", "/srv/a\0b"])
def test_check_updates_refuses_a_scratch_path_that_is_not_below_root(bad: str) -> None:
    with pytest.raises(GinnError) as info:
        load("arch").check_updates(bad)
    _expect("GINN-E-USAGE", 2)(info.value)


@pytest.mark.fr("NFR-SEC-005")
def test_check_updates_refuses_a_scratch_path_that_is_not_a_str() -> None:
    with pytest.raises(TypeError):
        load("arch").check_updates(1)  # type: ignore[arg-type]


@pytest.mark.fr("FR-024")
def test_fedora_check_updates_forces_fresh_metadata_and_never_upgrades() -> None:
    steps = load("fedora").check_updates()
    assert steps == (["dnf", "--refresh", "check-upgrade"],)
    assert not YES_FLAGS & set(steps[0])


@pytest.mark.fr("FR-024")
@pytest.mark.parametrize("name", ["debian", "kali"])
def test_apt_check_updates_reads_the_lists_as_last_refreshed(name: str) -> None:
    assert load(name).check_updates() == (["apt", "list", "--upgradable"],)


@pytest.mark.fr("FR-024")
def test_check_updates_is_refused_as_one_argv() -> None:
    with pytest.raises(GinnError) as info:
        load("arch").argv(Verb.CHECK_UPDATES)
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == "verb check-updates is a list of steps; use check_updates()"


@pytest.mark.fr("FR-013")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", [*SHIPPED, "_test"])
def test_clean_is_refused_as_one_argv_because_it_is_a_list_of_steps(name: str) -> None:
    with pytest.raises(GinnError) as info:
        load(name).argv(Verb.CLEAN)
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == "verb clean is a list of steps; use clean_steps"


@pytest.mark.fr("FR-024")
@pytest.mark.parametrize(
    ("sync_exit", "query_exit", "stdout", "stderr", "expected"),
    [
        # Step 4 exits 0 with the upgradable packages on stdout.
        (0, 0, "gimp 2.10-1 -> 2.10-2\n", "", UpdateStatus.UPDATES),
        (0, 0, "gimp 2.10-1 -> 2.10-2\n", "warning: unrelated\n", UpdateStatus.UPDATES),
        # Step 4 exits 1 with blank stdout: nothing is upgradable. Stderr empty or warnings only.
        (0, 1, "", "", UpdateStatus.NONE),
        (0, 1, "", "warning: something\n", UpdateStatus.NONE),
        (0, 1, "", "warning: one\nwarning: two\n", UpdateStatus.NONE),
        (0, 1, "  \n", "\nwarning: one\n   \n", UpdateStatus.NONE),
        # Blank stdout on exit 0 is not a list of updates.
        (0, 0, "", "", UpdateStatus.ERROR),
        (0, 0, "  \n", "", UpdateStatus.ERROR),
        # Exit 1 with an error line, even mixed with warnings, is an error.
        (0, 1, "", "error: could not open database\n", UpdateStatus.ERROR),
        (0, 1, "", "warning: one\nerror: two\n", UpdateStatus.ERROR),
        (0, 1, "", "Warning: capitalised is not warning:\n", UpdateStatus.ERROR),
        (0, 1, "", " warning: indented\n", UpdateStatus.ERROR),
        # Exit 1 with stdout is not "no updates".
        (0, 1, "gimp 2.10-1 -> 2.10-2\n", "", UpdateStatus.ERROR),
        (0, 1, "gimp 2.10-1 -> 2.10-2\n", "warning: one\n", UpdateStatus.ERROR),
        # Any other query exit status is an error.
        (0, 2, "", "", UpdateStatus.ERROR),
        (0, 127, "", "", UpdateStatus.ERROR),
        # The sync step must succeed, whatever step 4 reports.
        (1, 0, "gimp 2.10-1 -> 2.10-2\n", "", UpdateStatus.ERROR),
        (1, 1, "", "", UpdateStatus.ERROR),
        (2, 1, "", "warning: one\n", UpdateStatus.ERROR),
    ],
)
def test_arch_check_status_treats_exit_1_as_no_updates_only_with_warning_only_stderr(
    sync_exit: int, query_exit: int, stdout: str, stderr: str, expected: UpdateStatus
) -> None:
    fatal = load("arch").check_fatal_warnings
    assert arch_check_status(sync_exit, query_exit, stdout, stderr, fatal) is expected


@pytest.mark.fr("FR-024")
@pytest.mark.parametrize(
    ("sync_exit", "query_exit", "stdout", "stderr"),
    [
        # Exit 1 with only warning: lines reads as "no updates", unless one says a database is gone.
        (0, 1, "", "warning: database file for 'core' does not exist (use 'pacman -Sy')\n"),
        (0, 1, "", "warning: one\nwarning: database file for 'extra' does not exist\n"),
        # The same line makes the check an error when the query lists packages (exit 0).
        (0, 0, "gimp 2.10-1 -> 2.10-2\n", "warning: database file for 'core' does not exist\n"),
        # A failed sync is an error, whatever the query reports.
        (1, 1, "", "warning: database file for 'core' does not exist\n"),
    ],
)
def test_arch_check_status_fails_closed_on_a_missing_sync_database(
    sync_exit: int, query_exit: int, stdout: str, stderr: str
) -> None:
    fatal = load("arch").check_fatal_warnings
    assert arch_check_status(sync_exit, query_exit, stdout, stderr, fatal) is UpdateStatus.ERROR


@pytest.mark.fr("FR-024")
def test_plain_warning_lines_still_mean_no_updates_under_the_shipped_data() -> None:
    # The pattern is specific: another warning: line, even about the same database, is not fatal.
    fatal = load("arch").check_fatal_warnings
    stderr = "warning: database file for 'core' is locked\nwarning: something else\n"
    assert arch_check_status(0, 1, "", stderr, fatal) is UpdateStatus.NONE


@pytest.mark.fr("NFR-060")
def test_the_fatal_warning_list_is_optional_data_that_the_loader_reads() -> None:
    assert from_mapping(_base(), source="fixture.toml").check_fatal_warnings == ()
    assert load("debian").check_fatal_warnings == ()
    assert [p.pattern for p in load("arch").check_fatal_warnings] == [
        "database file for .* does not exist"
    ]
    raw = _base()
    raw["check_fatal_warning_regex"] = ["repository .* was skipped"]
    definition = from_mapping(raw, source="fixture.toml")
    assert [p.pattern for p in definition.check_fatal_warnings] == ["repository .* was skipped"]


@pytest.mark.fr("FR-014")
@pytest.mark.fr("NFR-060")
def test_the_everything_packages_are_optional_data_and_only_kali_has_them() -> None:
    assert from_mapping(_base(), source="fixture.toml").everything == ()
    assert load("kali").everything == ("kali-linux-everything",)
    for name in ("arch", "debian", "fedora"):
        assert load(name).everything == ()
    raw = _base()
    raw["everything"] = ["meta-one", "meta-two"]
    assert from_mapping(raw, source="fixture.toml").everything == ("meta-one", "meta-two")


# The init set (services_packages) and its measured installed size, and the clean steps, per
# shipped distribution. Both come from S-11 (ADR-015, Decision 11 and Decision 3).
SERVICES_SET: dict[str, tuple[tuple[str, ...], int]] = {
    "arch": ((), 0),
    "debian": (("systemd", "systemd-sysv", "dbus"), 19_166_208),
    "fedora": (("systemd", "dbus-broker"), 24_586_672),
    "kali": (("systemd", "systemd-sysv", "dbus"), 22_935_552),
}
_APT_CLEAN = [
    ["apt-get", "clean"],
    ["rm", "-rf", "/var/lib/apt/lists"],
    ["mkdir", "-p", "/var/lib/apt/lists/partial"],
]
CLEAN_ARGV = {
    "arch": [["find", "/var/cache/pacman/pkg", "-mindepth", "1", "-delete"]],
    "debian": _APT_CLEAN,
    "fedora": [["dnf", "clean", "all"]],
    "kali": _APT_CLEAN,
}
# The inputs of the disk formula of a services-on create (FR-121, ADR-015 Decision 11): the
# installed bytes of the Integration Package Set, and the package cache before the clean. S-11's.
DISK_INPUTS: dict[str, tuple[int, int]] = {
    "arch": (0, 2_606_348),
    "debian": (23_975_936, 21_576_652),
    "fedora": (6_000_393, 109_206_482),
    "kali": (24_147_968, 34_431_137),
}


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("FR-121")
@pytest.mark.parametrize("name", SHIPPED)
def test_the_disk_formula_inputs_are_the_s11_measurements(name: str) -> None:
    definition = load(name)
    integration, cache_peak = DISK_INPUTS[name]
    assert definition.integration_estimated_installed_bytes == integration
    assert definition.package_cache_peak_bytes == cache_peak


# ADR-015 Decision 11: the writable layer after an htop install, from spikes/results/S-11.md.
WRITABLE_LAYER = {
    "arch": 676_834,
    "debian": 23_454_481,
    "fedora": 117_756_852,
    "kali": 36_702_135,
}


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("FR-121")
@pytest.mark.parametrize("name", SHIPPED)
def test_the_writable_layer_is_the_s11_htop_measurement(name: str) -> None:
    assert load(name).writable_layer_bytes == WRITABLE_LAYER[name]


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("FR-121")
def test_the_disk_formula_inputs_are_optional_and_default_to_zero() -> None:
    fixture = from_mapping(_base(), source="fixture.toml")
    assert fixture.integration_estimated_installed_bytes == 0
    assert fixture.package_cache_peak_bytes == 0
    assert fixture.writable_layer_bytes == 0
    raw = _base()
    raw["integration_estimated_installed_bytes"] = 4096
    raw["package_cache_peak_bytes"] = 8192
    raw["writable_layer_bytes"] = 2048
    declared = from_mapping(raw, source="fixture.toml")
    assert declared.integration_estimated_installed_bytes == 4096
    assert declared.package_cache_peak_bytes == 8192
    assert declared.writable_layer_bytes == 2048


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("FR-121")
def test_the_disk_formula_gives_the_adr_worked_example_for_debian() -> None:
    """ADR-015 Decision 11: debian's services-on create needs 189,117,833 bytes before the 1.10
    factor, and 208,029,617 with it. The base's size is S-11's measured image size."""
    debian = load("debian")
    required = storage_required_bytes(
        81_256_893,
        debian.services_estimated_installed_bytes,
        debian.integration_estimated_installed_bytes,
        debian.package_cache_peak_bytes,
    )
    assert required == 208_029_617


@pytest.mark.fr("FR-013")
@pytest.mark.fr("NFR-060")
def test_the_init_set_is_optional_data_that_defaults_to_nothing() -> None:
    fixture = from_mapping(_base(), source="fixture.toml")
    assert fixture.services_packages == ()
    assert fixture.services_estimated_installed_bytes == 0
    raw = _base()
    raw["services_packages"] = ["systemd", "dbus"]
    raw["services_estimated_installed_bytes"] = 1024
    declared = from_mapping(raw, source="fixture.toml")
    assert declared.services_packages == ("systemd", "dbus")
    assert declared.services_estimated_installed_bytes == 1024


@pytest.mark.fr("FR-013")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", SHIPPED)
def test_the_init_set_and_its_size_are_the_s11_measurements(name: str) -> None:
    packages, size = SERVICES_SET[name]
    definition = load(name)
    assert definition.services_packages == packages
    assert definition.services_estimated_installed_bytes == size
    assert (size == 0) == (packages == ())


@pytest.mark.fr("FR-013")
@pytest.mark.parametrize("name", SHIPPED)
def test_the_init_set_is_never_part_of_the_integration_set(name: str) -> None:
    """The init set is for services-on realms only (ADR-015). The Integration Package Set goes to
    every realm, so none of the init packages may be in it."""
    definition = load(name)
    integration = definition.integration_set("en_US.UTF-8").packages
    assert not set(definition.services_packages) & set(integration), name


@pytest.mark.fr("FR-013")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", SHIPPED)
def test_the_clean_steps_are_the_s11_cache_clean_per_distribution(name: str) -> None:
    definition = load(name)
    assert definition.has_clean is True
    assert [list(step) for step in definition.clean_steps] == CLEAN_ARGV[name]
    assert Verb.CLEAN not in definition.verbs


@pytest.mark.fr("FR-013")
@pytest.mark.fr("NFR-060")
def test_clean_is_optional_and_a_definition_may_declare_it_as_steps() -> None:
    fixture = load("_test")
    assert fixture.has_clean is False
    assert fixture.clean_steps == ()
    raw = _base()
    raw["verbs"]["clean"] = [["fake-pm", "clean"], ["fake-pm", "purge", "--cache"]]
    declared = from_mapping(raw, source="fixture.toml")
    assert declared.has_clean is True
    assert declared.clean_steps == (("fake-pm", "clean"), ("fake-pm", "purge", "--cache"))


@pytest.mark.fr("FR-013")
@pytest.mark.fr("NFR-060")
def test_arch_clean_empties_the_cache_and_keeps_the_directory_and_the_sync_databases() -> None:
    (step,) = load("arch").clean_steps
    assert step[:2] == ("find", "/var/cache/pacman/pkg")
    assert step[step.index("-mindepth") + 1] == "1"
    assert "/var/lib/pacman" not in " ".join(step)


@pytest.mark.fr("FR-024")
def test_update_status_has_exactly_three_outcomes() -> None:
    assert {status.value for status in UpdateStatus} == {"updates", "none", "error"}


@pytest.mark.fr("FR-023")
def test_fedora_upgrade_refreshes_the_metadata_first_and_upgrades_every_installed_package() -> None:
    # --refresh is a dnf5 global option: it forces a metadata refresh before the upgrade.
    assert load("fedora").argv(Verb.UPGRADE) == [
        "dnf",
        "--refresh",
        "--assumeyes",
        "--setopt=install_weak_deps=False",
        "upgrade",
    ]


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("FR-023")
@pytest.mark.parametrize("name", ["debian", "kali"])
def test_apt_upgrade_is_the_full_upgrade_dist_upgrade(name: str) -> None:
    # FR-023 AC-2 names the native full upgrade. A plain apt upgrade leaves a package at its
    # installed version when its new version needs a dependency that is not installed yet, so it
    # is not full (apt-get(8)). --no-install-recommends is a global apt-get option, valid here.
    assert load(name).argv(Verb.UPGRADE) == [
        "apt-get",
        "--yes",
        "--no-install-recommends",
        "dist-upgrade",
    ]


@pytest.mark.fr("FR-013")
@pytest.mark.fr("CR-007")
def test_arch_creation_refresh_is_the_full_upgrade_and_its_set_is_ca_certificates() -> None:
    # ADR-011 and CL-9: creation runs refresh once, then installs the set. On Arch that refresh is
    # the full sync-and-upgrade, the same argv as the upgrade verb.
    arch = load("arch")
    assert arch.argv(Verb.REFRESH) == arch.argv(Verb.UPGRADE)
    assert arch.integration_set("en_US.UTF-8").packages == ("ca-certificates",)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-023")
@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("verb", [Verb.INSTALL, Verb.RESOLVE, Verb.ESTIMATE])
def test_install_resolve_and_estimate_never_upgrade_the_realm(name: str, verb: Verb) -> None:
    argv = load(name).argv(verb, ["gimp"])
    assert not UPGRADE_TOKENS & set(argv), (name, verb, argv)


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("arch", ["pacman", "--remove", "--noconfirm", "gimp"]),
        ("debian", ["apt-get", "--yes", "remove", "gimp"]),
        ("kali", ["apt-get", "--yes", "remove", "gimp"]),
        ("fedora", ["dnf", "--assumeyes", "remove", "gimp"]),
    ],
)
def test_remove_names_only_the_requested_package(name: str, expected: list[str]) -> None:
    assert load(name).argv(Verb.REMOVE, ["gimp"]) == expected


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("name", SHIPPED)
def test_remove_never_widens_the_removal_with_autoremove_or_cascade(name: str) -> None:
    assert not REMOVE_WIDENERS & set(load(name).argv(Verb.REMOVE, ["gimp"]))


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("debian", ["apt-get", "--simulate", "remove", "gimp"]),
        ("kali", ["apt-get", "--simulate", "remove", "gimp"]),
        ("fedora", ["dnf", "--assumeno", "--no-autoremove", "remove", "gimp"]),
    ],
)
def test_remove_check_is_the_dry_run_that_names_only_the_requested_package(
    name: str, expected: list[str]
) -> None:
    assert load(name).argv(Verb.REMOVE_CHECK, ["gimp"]) == expected


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("name", REMOVE_CHECK_DISTROS)
def test_remove_check_changes_nothing_and_never_answers_yes(name: str) -> None:
    argv = load(name).argv(Verb.REMOVE_CHECK, ["gimp"])
    assert not YES_FLAGS & set(argv), (name, argv)
    assert "--simulate" in argv or "--assumeno" in argv, (name, argv)


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("name", REMOVE_CHECK_DISTROS)
def test_remove_check_names_are_checked_like_every_package_verb(name: str) -> None:
    for raw in [*NAMES, "-x", "--foo"]:
        with pytest.raises(GinnError) as info:
            load(name).argv(Verb.REMOVE_CHECK, ["gimp", raw])
        _expect("GINN-E-PKG-NAME-INVALID", 2)(info.value)
        assert info.value.details["name"] == raw


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("name", ["arch", "_test"])
def test_remove_check_is_optional_and_absent_where_removal_is_native(name: str) -> None:
    # Arch refuses dependents natively, so it needs no dry run. The fixture omits the verb.
    # Asking for it anyway is a ginn bug (callers check has_remove_check), so it is GINN-E-INTERNAL.
    definition = load(name)
    assert definition.has_remove_check is False
    with pytest.raises(GinnError) as info:
        definition.argv(Verb.REMOVE_CHECK, ["gimp"])
    _expect("GINN-E-INTERNAL", 70)(info.value)
    assert info.value.details["detail"] == f"verb remove-check is not defined for {name}"


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("name", REMOVE_CHECK_DISTROS)
def test_shipped_distributions_that_define_remove_check_report_it(name: str) -> None:
    assert load(name).has_remove_check is True


@pytest.mark.fr("NFR-060")
def test_a_definition_may_add_remove_check_and_the_loader_runs_it() -> None:
    raw = _base()
    raw["verbs"]["remove-check"] = ["fake-pm", "check", "{packages}"]
    definition = from_mapping(raw, source="fixture.toml")
    assert definition.has_remove_check is True
    assert definition.argv(Verb.REMOVE_CHECK, ["gimp"]) == ["fake-pm", "check", "gimp"]


@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", [*SHIPPED, "_test"])
@pytest.mark.parametrize("verb", [Verb.REFRESH, Verb.UPGRADE, Verb.LIST_EXPLICIT])
def test_packages_given_to_a_verb_that_takes_none_are_refused(name: str, verb: Verb) -> None:
    with pytest.raises(GinnError) as info:
        load(name).argv(verb, ["gimp"])
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == f"verb {verb.value} takes no packages"


# ---- placeholder expansion and package names: NFR-SEC-005, DR-003 ----------------------------


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", [*SHIPPED, "_test"])
@pytest.mark.parametrize("verb", PACKAGE_VERBS)
def test_hostile_and_flag_like_names_are_refused_before_any_element_is_built(
    name: str, verb: Verb
) -> None:
    for raw in [*NAMES, "-x", "--foo"]:
        with pytest.raises(GinnError) as info:
            load(name).argv(verb, ["gimp", raw])
        _expect("GINN-E-PKG-NAME-INVALID", 2)(info.value)
        assert info.value.details["name"] == raw


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", [*SHIPPED, "_test"])
@pytest.mark.parametrize("verb", PACKAGE_VERBS)
def test_valid_names_stay_one_argv_element_each(name: str, verb: Verb) -> None:
    out = load(name).argv(verb, ["gimp", "lib-x.2"])
    assert out[-2:] == ["gimp", "lib-x.2"]


@pytest.mark.fr("NFR-SEC-005")
def test_package_names_are_not_reparsed_as_templates() -> None:
    # A name that looks like a placeholder is only a name. The permissive pattern accepts it, and
    # argv() splices it as one element without expanding it (loader.py docstring).
    raw = _base()
    raw["package_name_regex"] = "[^\\x00]+"
    out = from_mapping(raw).argv(Verb.INSTALL, ["{lang}", "{packages}"])
    assert out == ["fake-pm", "install", "{lang}", "{packages}"]


@pytest.mark.fr("NFR-SEC-005")
def test_a_placeholder_name_is_refused_by_the_shipped_pattern() -> None:
    with pytest.raises(GinnError) as info:
        load("arch").argv(Verb.INSTALL, ["{packages}"])
    _expect("GINN-E-PKG-NAME-INVALID", 2)(info.value)


@pytest.mark.fr("NFR-060")
def test_doubled_braces_are_literal_and_never_splice() -> None:
    out = _with_verb("upgrade", ["fake-pm", "{{packages}}", "a{{b}}"]).argv(Verb.UPGRADE)
    assert out == ["fake-pm", "{packages}", "a{b}"]


@pytest.mark.fr("FR-040")
def test_a_package_verb_refuses_an_empty_package_list() -> None:
    with pytest.raises(GinnError) as info:
        load("arch").argv(Verb.INSTALL, [])
    _expect("GINN-E-USAGE", 2)(info.value)


@pytest.mark.fr("FR-040")
def test_a_verb_without_packages_needs_none() -> None:
    assert load("arch").argv(Verb.UPGRADE) == [
        "pacman",
        "--sync",
        "--refresh",
        "--sysupgrade",
        "--noconfirm",
    ]


@pytest.mark.fr("DR-003")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("bad", ["", "a\0b"])
def test_an_empty_or_nul_package_name_is_refused(bad: str) -> None:
    with pytest.raises(GinnError) as info:
        load("arch").argv(Verb.INSTALL, [bad])
    _expect("GINN-E-PKG-NAME-INVALID", 2)(info.value)


@pytest.mark.fr("NFR-SEC-005")
def test_a_bare_string_is_not_accepted_as_a_package_list() -> None:
    with pytest.raises(TypeError):
        load("arch").argv(Verb.INSTALL, "gimp")  # type: ignore[arg-type]


# ---- signature and stale-metadata errors: FR-012, ADR-011 ------------------------------------

SIGNATURE_SAMPLES: list[tuple[str, str]] = [
    ("arch", 'error: gimp: signature from "Packager <p@example.org>" is unknown trust'),
    ("arch", "error: gimp: invalid or corrupted package (PGP signature)"),
    ("arch", 'error: key "0123456789ABCDEF" could not be looked up remotely'),
    ("debian", "W: GPG error: stable InRelease: NO_PUBKEY 0123456789ABCDEF"),
    ("debian", "The repository 'http://example.org stable Release' is not signed."),
    ("kali", "The following signatures were invalid: BADSIG 0123456789ABCDEF"),
    ("fedora", "GPG check FAILED"),
    ("fedora", "Public key for gimp-2.10-1.fc44.x86_64.rpm is not installed"),
]
# Representative runtime text, not verified until M4 (ADR-011), like the signature samples above.
STALE_SAMPLES: list[tuple[str, str]] = [
    (
        "arch",
        "error: failed retrieving file 'gimp-2.10-1-x86_64.pkg.tar.zst' from "
        "geo.mirror.example : The requested URL returned error: 404",
    ),
    (
        "debian",
        "E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?",
    ),
    (
        "debian",
        "E: Failed to fetch http://deb.example.org/debian/pool/main/g/gimp/gimp_2.10.deb"
        "  404  Not Found [IP: 192.0.2.1 80]",
    ),
    (
        "kali",
        "E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?",
    ),
    (
        "fedora",
        "Status code: 404 for https://mirror.example.org/fedora/gimp-2.10-1.fc44.x86_64.rpm",
    ),
]
BENIGN = ["Reading package lists... Done", "Installing gimp (2.10-1)", "Nothing to do."]


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize(("name", "line"), SIGNATURE_SAMPLES)
def test_signature_error_regex_recognises_representative_failures(name: str, line: str) -> None:
    assert load(name).signature_error.search(line) is not None


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("line", BENIGN)
def test_signature_error_regex_ignores_ordinary_output(name: str, line: str) -> None:
    assert load(name).signature_error.search(line) is None


MISSING_SAMPLES: list[tuple[str, str]] = [
    ("debian", "E: Unable to locate package gone"),
    ("debian", "E: Package 'gone' has no installation candidate"),
    ("kali", "E: Unable to locate package gone"),
    ("arch", "error: target not found: gone"),
    ("arch", "fatal: repository 'https://aur.archlinux.org/gone.git/' not found"),
    ("fedora", "No match for argument: gone"),
]
"""One refusal per distribution that says a named package is not available upstream (FR-022)."""
NOT_MISSING_SAMPLES: list[tuple[str, str]] = [
    ("debian", "Temporary failure resolving 'deb.debian.org'"),
    ("debian", "E: Sub-process /usr/bin/dpkg returned an error code (1)"),
    ("kali", "E: Write error - write (28: No space left on device)"),
    (
        "arch",
        "error: failed retrieving file 'curl' from mirror: The requested URL returned error: 404",
    ),
    ("arch", "error: failed to commit transaction (not enough free disk space)"),
    ("fedora", "Curl error (6): Couldn't resolve host name"),
]


@pytest.mark.fr("FR-022")
@pytest.mark.parametrize(("name", "line"), MISSING_SAMPLES)
def test_missing_package_regex_recognises_a_name_the_upstream_does_not_offer(
    name: str, line: str
) -> None:
    pattern = load(name).missing_package
    assert pattern is not None
    assert pattern.search(line) is not None


@pytest.mark.fr("FR-022")
@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("line", [line for _, line in NOT_MISSING_SAMPLES] + BENIGN)
def test_missing_package_regex_ignores_network_disk_and_ordinary_output(
    name: str, line: str
) -> None:
    """A network, disk or ordinary line is never evidence of a missing package (FR-022 AC-1)."""
    pattern = load(name).missing_package
    assert pattern is not None
    assert pattern.search(line) is None


@pytest.mark.fr("FR-022")
def test_a_distribution_without_a_missing_package_regex_reads_no_refusal_as_missing() -> None:
    """The key is optional, and without it nothing is positive evidence, so a rebuild fails
    closed (distros/loader.py)."""
    raw: dict[str, Any] = tomllib.loads(_shipped_text("debian"))
    del raw["missing_package_regex"]
    definition = loader.from_mapping(raw, "debian.toml")
    assert definition.missing_package is None


@pytest.mark.fr("FR-040")
@pytest.mark.parametrize(("name", "line"), STALE_SAMPLES)
def test_stale_metadata_regex_recognises_representative_mirror_failures(
    name: str, line: str
) -> None:
    assert load(name).stale_metadata.search(line) is not None


@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("line", BENIGN)
def test_stale_metadata_regex_ignores_ordinary_output(name: str, line: str) -> None:
    assert load(name).stale_metadata.search(line) is None


# ---- validation: every rule in loader.py breaks on purpose -----------------------------------


def _set(*path: str, value: object) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        node = raw
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return edit


def _del(*path: str) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        node = raw
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return edit


BAD_TABLES: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("top-level key missing", _del("shell")),
    ("top-level key unknown", _set("image", value="x")),
    ("schema is not 1", _set("schema", value=2)),
    ("schema is a bool", _set("schema", value=True)),
    ("id is not a distro id", _set("id", value="Bad Id")),
    ("trust_key empty", _set("trust_key", value="   ")),
    ("trust_key not a string", _set("trust_key", value=3)),
    ("shell is relative", _set("shell", value="bin/sh")),
    ("shell has an argument", _set("shell", value="/bin/sh -c")),
    ("name regex does not compile", _set("package_name_regex", value="[a-z")),
    ("signature regex does not compile", _set("signature_error_regex", value="(")),
    ("stale metadata regex missing", _del("stale_metadata_regex")),
    ("stale metadata regex does not compile", _set("stale_metadata_regex", value="(")),
    ("fatal warning list is a string", _set("check_fatal_warning_regex", value="database")),
    ("fatal warning list has an empty item", _set("check_fatal_warning_regex", value=[""])),
    (
        "fatal warning regex does not compile",
        _set("check_fatal_warning_regex", value=["database ("]),
    ),
    ("everything is a string", _set("everything", value="kali-linux-everything")),
    ("everything names a bad package", _set("everything", value=["Bad_Name"])),
    ("everything repeats a package", _set("everything", value=["meta", "meta"])),
    ("services_packages is a string", _set("services_packages", value="systemd")),
    ("services_packages names a bad package", _set("services_packages", value=["Bad_Name"])),
    (
        "services_packages repeats a package",
        _set("services_packages", value=["systemd", "systemd"]),
    ),
    ("services_packages set without a size", _set("services_packages", value=["systemd"])),
    ("services size is a string", _set("services_estimated_installed_bytes", value="1024")),
    ("services size is a bool", _set("services_estimated_installed_bytes", value=True)),
    ("services size is negative", _set("services_estimated_installed_bytes", value=-1)),
    (
        "services size set with no packages",
        _set("services_estimated_installed_bytes", value=1024),
    ),
    ("integration size is a string", _set("integration_estimated_installed_bytes", value="1")),
    ("integration size is a bool", _set("integration_estimated_installed_bytes", value=True)),
    ("integration size is negative", _set("integration_estimated_installed_bytes", value=-1)),
    ("cache peak is a string", _set("package_cache_peak_bytes", value="1")),
    ("cache peak is a bool", _set("package_cache_peak_bytes", value=True)),
    ("cache peak is negative", _set("package_cache_peak_bytes", value=-1)),
    ("writable layer is a string", _set("writable_layer_bytes", value="1")),
    ("writable layer is a bool", _set("writable_layer_bytes", value=True)),
    ("writable layer is negative", _set("writable_layer_bytes", value=-1)),
    ("refresh_before missing", _del("refresh_before")),
    ("refresh_before is not a list", _set("refresh_before", value="install")),
    ("refresh_before names an unknown verb", _set("refresh_before", value=["sync"])),
    ("refresh_before lists refresh", _set("refresh_before", value=["refresh"])),
    ("refresh_before repeats a verb", _set("refresh_before", value=["install", "install"])),
    ("integration is not a table", _set("integration", value=["x"])),
    ("integration.steps missing", _del("integration", "steps")),
    ("integration has an unknown key", _set("integration", "image", value="x")),
    ("integration package fails the name pattern", _set("integration", "packages", value=["Bad"])),
    ("integration package uses {locale}", _set("integration", "packages", value=["x-{locale}"])),
    ("integration step uses {lang}", _set("integration", "steps", value=[["x", "{lang}"]])),
    ("integration step is empty", _set("integration", "steps", value=[[]])),
    ("integration steps is not a list", _set("integration", "steps", value="x")),
    ("verbs missing refresh", _del("verbs", "refresh")),
    ("verbs missing remove", _del("verbs", "remove")),
    ("verbs has an unknown verb", _set("verbs", "check_updates", value=["x"])),
    ("verbs missing check-updates", _del("verbs", "check-updates")),
    (
        "check-updates is one argv, not a step list",
        _set("verbs", "check-updates", value=["fake-pm", "check"]),
    ),
    ("check-updates is an empty list", _set("verbs", "check-updates", value=[])),
    ("check-updates has an empty step", _set("verbs", "check-updates", value=[[]])),
    (
        "check-updates step uses {packages}",
        _set("verbs", "check-updates", value=[["fake-pm", "{packages}"]]),
    ),
    (
        "check-updates step uses {lang}",
        _set("verbs", "check-updates", value=[["fake-pm", "{lang}"]]),
    ),
    (
        "check-updates first element uses {tmpdir}",
        _set("verbs", "check-updates", value=[["{tmpdir}/x"]]),
    ),
    (
        "check-updates uses an unknown placeholder",
        _set("verbs", "check-updates", value=[["fake-pm", "{image}"]]),
    ),
    ("install uses {tmpdir}", _set("verbs", "install", value=["fake-pm", "install", "{tmpdir}"])),
    ("verb is a list of lists", _set("verbs", "upgrade", value=[["fake-pm"]])),
    ("verb is an empty list", _set("verbs", "upgrade", value=[])),
    ("verb element is not a string", _set("verbs", "upgrade", value=["fake-pm", 1])),
    ("verb has an unknown placeholder", _set("verbs", "upgrade", value=["fake-pm", "{image}"])),
    ("verb uses {lang}", _set("verbs", "upgrade", value=["fake-pm", "{lang}"])),
    ("verb embeds {packages} in a string", _set("verbs", "install", value=["x", "--n={packages}"])),
    ("verb command has a space", _set("verbs", "upgrade", value=["fake-pm upgrade"])),
    ("verb command is a placeholder", _set("verbs", "upgrade", value=["{packages}"])),
    ("verb has a stray closing brace", _set("verbs", "upgrade", value=["fake-pm}"])),
    ("verb has a positional field", _set("verbs", "upgrade", value=["fake-pm", "{}"])),
    ("verb has a format spec", _set("verbs", "upgrade", value=["fake-pm", "{packages:>3}"])),
    (
        "remove-check takes no packages",
        _set("verbs", "remove-check", value=["fake-pm", "check"]),
    ),
    ("remove-check is an empty list", _set("verbs", "remove-check", value=[])),
    ("remove-check is a list of lists", _set("verbs", "remove-check", value=[["fake-pm"]])),
    (
        "remove-check uses {tmpdir}",
        _set("verbs", "remove-check", value=["fake-pm", "{tmpdir}"]),
    ),
    (
        "remove-check embeds {packages} in a string",
        _set("verbs", "remove-check", value=["fake-pm", "--name={packages}"]),
    ),
    ("clean is a string", _set("verbs", "clean", value="fake-pm clean")),
    ("clean is one argv, not a step list", _set("verbs", "clean", value=["fake-pm", "clean"])),
    ("clean is an empty list", _set("verbs", "clean", value=[])),
    ("clean has an empty step", _set("verbs", "clean", value=[[]])),
    ("clean uses {packages}", _set("verbs", "clean", value=[["fake-pm", "{packages}"]])),
    ("clean uses {tmpdir}", _set("verbs", "clean", value=[["fake-pm", "{tmpdir}"]])),
    (
        "clean embeds a placeholder in a string",
        _set("verbs", "clean", value=[["fake-pm", "--n={lang}"]]),
    ),
    ("clean command has a space", _set("verbs", "clean", value=[["fake-pm clean"]])),
]


@pytest.mark.fr("NFR-040")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize(("label", "edit"), BAD_TABLES, ids=[label for label, _ in BAD_TABLES])
def test_malformed_data_is_refused_with_the_field_and_rule_in_details(
    label: str, edit: Callable[[dict[str, Any]], None]
) -> None:
    raw = _base()
    edit(raw)
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    err = info.value
    assert err.code == "GINN-E-DISTRO-DATA", label
    assert err.exit_code == 70
    assert err.details["file"] == "fixture.toml"
    assert isinstance(err.details["field"], str)
    assert isinstance(err.details["rule"], str)


@pytest.mark.fr("NFR-040")
@pytest.mark.fr("NFR-060")
def test_distro_data_error_names_the_file_field_and_rule() -> None:
    raw = _base()
    _del("verbs", "refresh")(raw)
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    err = info.value
    assert err.message == (
        "shipped distro data fixture.toml is invalid: verbs missing key(s): refresh"
    )
    assert err.remedy.startswith("the shipped distro data is invalid; reinstall ginn")
    assert err.details == {
        "file": "fixture.toml",
        "field": "verbs",
        "rule": "missing key(s): refresh",
    }


@pytest.mark.fr("NFR-060")
def test_the_unbroken_fixture_validates() -> None:
    fixture = from_mapping(_base(), source="fixture.toml")
    assert fixture.id == "_test"
    assert fixture.check_updates() == (["fake-pm", "check"],)


@pytest.mark.fr("NFR-040")
def test_toml_syntax_errors_are_refused_as_distro_data_errors() -> None:
    with pytest.raises(GinnError) as info:
        parse_text("schema = ", "broken.toml")
    assert info.value.code == "GINN-E-DISTRO-DATA"
    assert info.value.details["field"] == "toml"


@pytest.mark.fr("NFR-060")
def test_a_definition_is_a_frozen_value() -> None:
    definition = load("arch")
    assert isinstance(definition, DistroDef)
    with pytest.raises(AttributeError):
        definition.shell = "/bin/sh"  # type: ignore[misc]


# ---- the definition cache: NFR-060 -----------------------------------------------------------


@pytest.mark.fr("NFR-060")
def test_a_loaded_definition_is_served_from_the_cache_until_clear_cache() -> None:
    first = load("arch")
    assert load("arch") is first
    clear_cache()
    assert load("arch") is not first


@pytest.mark.fr("NFR-060")
def test_a_cached_definition_is_served_even_when_its_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _shipped_text("_test").replace('id = "_test"', 'id = "fixture"')
    path = tmp_path / "fixture.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(loader, "_data_dir", lambda: tmp_path)
    first = load("fixture")
    path.write_text(text.replace('shell = "/bin/sh"', 'shell = "/bin/dash"'), encoding="utf-8")
    assert load("fixture") is first
    clear_cache()
    assert load("fixture").shell == "/bin/dash"


@pytest.mark.fr("NFR-060")
def test_a_failed_load_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "fixture.toml"
    path.write_text("schema = 1\n", encoding="utf-8")  # missing keys: refused
    monkeypatch.setattr(loader, "_data_dir", lambda: tmp_path)
    with pytest.raises(GinnError) as info:
        load("fixture")
    assert info.value.code == "GINN-E-DISTRO-DATA"
    fixed = _shipped_text("_test").replace('id = "_test"', 'id = "fixture"')
    path.write_text(fixed, encoding="utf-8")
    assert load("fixture").id == "fixture"


@pytest.mark.fr("NFR-060")
def test_a_monkeypatched_data_directory_never_returns_the_shipped_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _shipped_text("_test").replace('id = "_test"', 'id = "arch"')
    (tmp_path / "arch.toml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(loader, "_data_dir", lambda: tmp_path)
    assert load("arch").shell == "/bin/sh"
    monkeypatch.undo()
    assert load("arch").shell == "/bin/bash"


def _with_verb(key: str, template: list[str]) -> DistroDef:
    raw = copy.deepcopy(_base())
    raw["verbs"][key] = template
    return from_mapping(raw, source="fixture.toml")


# ---- T5.1 review fixes: network markers, trailing '-', fedora .rpm ---------------------------


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_distro_defines_a_network_error_pattern(name: str) -> None:
    assert load(name).network_error.pattern


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
def test_a_missing_network_error_regex_is_refused_as_distro_data() -> None:
    raw = _base()
    del raw["network_error_regex"]
    with pytest.raises(GinnError) as caught:
        from_mapping(raw, "test")
    assert caught.value.code == "GINN-E-DISTRO-DATA"


@pytest.mark.fr("DR-003")
@pytest.mark.fr("AS-027")
@pytest.mark.parametrize("name", SHIPPED)
def test_a_trailing_dash_is_refused_by_the_argv_builder_for_every_distro(name: str) -> None:
    with pytest.raises(GinnError) as caught:
        load(name).argv(Verb.INSTALL, ["gcc-"])
    assert caught.value.code == "GINN-E-PKG-NAME-INVALID"
    assert load(name).argv(Verb.INSTALL, ["g++"])[-1] == "g++"


@pytest.mark.fr("FR-040")
@pytest.mark.fr("DR-003")
def test_a_fedora_name_ending_in_rpm_is_refused_and_a_suffix_form_is_read_as_typed() -> None:
    fedora = load("fedora")
    assert fedora.name_pattern.fullmatch("gimp.rpm") is None
    assert fedora.name_pattern.fullmatch("rpm") is not None
    # dnf reads `gimp-2.10.38` and `foo.x86_64` as name, version or arch forms; the manifest keeps
    # the name as typed (FR-040 reason), so both pass the pattern.
    assert fedora.name_pattern.fullmatch("gimp-2.10.38") is not None
    assert fedora.name_pattern.fullmatch("foo.x86_64") is not None


# ---- db-check and the check-updates rules: FR-120 AC-2, FR-024, NFR-060 ----------------------

DB_CHECK_ARGV = {
    "arch": ["pacman", "--database", "--check"],
    "debian": ["dpkg", "--audit"],
    "fedora": ["rpm", "--verifydb"],
    "kali": ["dpkg", "--audit"],
}
_ABSENT = object()
"""Marks a key a test leaves out of the table."""


def _with_db_check() -> dict[str, Any]:
    """The fixture table with a db-check verb and both of its result keys."""
    raw = _base()
    raw["verbs"]["db-check"] = ["fake-pm", "check"]
    raw["db_check_ok_exit"] = [0]
    raw["db_check_stdout_inconsistent"] = False
    return raw


def _shipped_raw(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = tomllib.loads(_shipped_text(name))
    return raw


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_distribution_defines_db_check_as_one_plain_argv(name: str) -> None:
    definition = load(name)
    assert definition.has_db_check is True
    assert list(definition.argv(Verb.DB_CHECK)) == DB_CHECK_ARGV[name]
    assert definition.db_check_ok_exit == frozenset({0})


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
def test_the_fixture_omits_db_check_so_the_optional_path_stays_tested() -> None:
    fixture = load("_test")
    assert fixture.has_db_check is False
    assert fixture.db_check_ok_exit == frozenset()
    assert fixture.db_check_stdout_inconsistent is False
    with pytest.raises(GinnError) as info:
        fixture.argv(Verb.DB_CHECK)
    _expect("GINN-E-INTERNAL", 70)(info.value)


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", SHIPPED)
def test_db_check_takes_no_package(name: str) -> None:
    with pytest.raises(GinnError) as info:
        load(name).argv(Verb.DB_CHECK, ["gimp"])
    _expect("GINN-E-USAGE", 2)(info.value)
    assert info.value.message == "verb db-check takes no packages"


@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("element", ["{packages}", "{lang}", "{tmpdir}"])
def test_a_placeholder_in_db_check_is_refused_as_distro_data(element: str) -> None:
    raw = _with_db_check()
    raw["verbs"]["db-check"] = ["fake-pm", "check", element]
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    assert info.value.code == "GINN-E-DISTRO-DATA"
    assert info.value.details["field"] == "verbs.db-check"


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
def test_a_fixture_with_db_check_and_its_keys_loads_and_reports_the_verb() -> None:
    definition = from_mapping(_with_db_check(), source="fixture.toml")
    assert definition.has_db_check is True
    assert definition.db_check_ok_exit == frozenset({0})
    assert definition.db_check_stdout_inconsistent is False


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize(
    ("ok", "flag", "field"),
    [
        (_ABSENT, _ABSENT, "db_check_ok_exit"),
        ([0], _ABSENT, "db_check_stdout_inconsistent"),
        ([], False, "db_check_ok_exit"),
        ([0, 0], False, "db_check_ok_exit"),
        ([256], False, "db_check_ok_exit"),
        ([True], False, "db_check_ok_exit"),
        ("0", False, "db_check_ok_exit"),
        ([0], "yes", "db_check_stdout_inconsistent"),
    ],
)
def test_db_check_keys_are_required_with_the_verb_and_must_be_well_formed(
    ok: object, flag: object, field: str
) -> None:
    raw = _with_db_check()
    for key, value in (("db_check_ok_exit", ok), ("db_check_stdout_inconsistent", flag)):
        if value is _ABSENT:
            del raw[key]
        else:
            raw[key] = value
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    assert info.value.code == "GINN-E-DISTRO-DATA"
    assert info.value.details["field"] == field


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
def test_db_check_keys_are_refused_without_the_verb() -> None:
    raw = _base()
    raw["db_check_stdout_inconsistent"] = False
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    assert info.value.details["field"] == "db_check_stdout_inconsistent"
    assert info.value.details["rule"] == "is read only with verbs.db-check"


@pytest.mark.fr("FR-120")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize(
    ("exit_code", "stdout", "ok_exit", "stdout_inconsistent", "expected"),
    [
        (0, "", frozenset({0}), False, DbStatus.CONSISTENT),
        (0, "anything\n", frozenset({0}), False, DbStatus.CONSISTENT),
        (0, "", frozenset({0}), True, DbStatus.CONSISTENT),
        (0, "  \n", frozenset({0}), True, DbStatus.CONSISTENT),
        (0, "gimp: broken\n", frozenset({0}), True, DbStatus.INCONSISTENT),
        (0, "", frozenset({0, 3}), False, DbStatus.CONSISTENT),
        (3, "", frozenset({0, 3}), False, DbStatus.CONSISTENT),
        (1, "", frozenset({0}), False, DbStatus.INCONSISTENT),
        (2, "", frozenset({0}), True, DbStatus.INCONSISTENT),
        # podman could not run the command: the database is not read, so the result is unknown.
        (125, "", frozenset({0}), False, DbStatus.UNKNOWN),
        (126, "", frozenset({0}), False, DbStatus.UNKNOWN),
        (127, "", frozenset({0}), True, DbStatus.UNKNOWN),
    ],
)
def test_db_check_status_reads_the_result_by_the_data(
    exit_code: int,
    stdout: str,
    ok_exit: frozenset[int],
    stdout_inconsistent: bool,
    expected: DbStatus,
) -> None:
    status = db_check_status(
        exit_code, stdout, ok_exit=ok_exit, stdout_inconsistent=stdout_inconsistent
    )
    assert status is expected


@pytest.mark.fr("FR-120")
@pytest.mark.parametrize(
    ("name", "exit_code", "stdout", "expected"),
    [
        ("arch", 0, "", DbStatus.CONSISTENT),
        ("arch", 1, "", DbStatus.INCONSISTENT),
        ("debian", 0, "", DbStatus.CONSISTENT),
        ("debian", 0, "gimp: broken\n", DbStatus.INCONSISTENT),
        ("kali", 0, "gimp: broken\n", DbStatus.INCONSISTENT),
        ("fedora", 0, "", DbStatus.CONSISTENT),
        ("fedora", 1, "", DbStatus.INCONSISTENT),
        ("fedora", 127, "", DbStatus.UNKNOWN),
    ],
)
def test_the_shipped_data_reads_the_db_check_result(
    name: str, exit_code: int, stdout: str, expected: DbStatus
) -> None:
    definition = load(name)
    status = db_check_status(
        exit_code,
        stdout,
        ok_exit=definition.db_check_ok_exit,
        stdout_inconsistent=definition.db_check_stdout_inconsistent,
    )
    assert status is expected


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
def test_the_check_count_and_exit_rules_are_data_per_shipped_distribution() -> None:
    assert load("arch").check_count is CountRule.NONBLANK_LINES
    assert load("arch").check_count_regex is None
    for name in ("debian", "kali"):
        definition = load(name)
        assert definition.check_count is CountRule.LINES_MATCHING
        assert definition.check_count_regex is not None
        assert definition.check_count_regex.pattern == r"\[upgradable from: "
    fedora = load("fedora")
    assert fedora.check_count is CountRule.UNREAD
    assert fedora.check_exit_for_updates == 100
    for name in ("arch", "debian", "kali"):
        assert load(name).check_exit_for_updates is None


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
def test_the_check_count_and_exit_default_to_unread_and_none_when_omitted() -> None:
    fixture = from_mapping(_base(), source="fixture.toml")
    assert fixture.check_count is CountRule.UNREAD
    assert fixture.check_count_regex is None
    assert fixture.check_exit_for_updates is None


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize(
    ("count", "regex", "field"),
    [
        ("lines", _ABSENT, "check_updates_count"),
        ("LINES-MATCHING", "x", "check_updates_count"),
        (["lines-matching"], _ABSENT, "check_updates_count"),
        ("lines-matching", _ABSENT, "check_updates_count_regex"),
        ("lines-matching", "(", "check_updates_count_regex"),
        ("nonblank-lines", "x", "check_updates_count_regex"),
        ("unread", "x", "check_updates_count_regex"),
    ],
)
def test_a_check_count_rule_is_refused_unless_its_regex_is_well_formed(
    count: object, regex: object, field: str
) -> None:
    raw = _base()
    raw["check_updates_count"] = count
    if regex is not _ABSENT:
        raw["check_updates_count_regex"] = regex
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    assert info.value.code == "GINN-E-DISTRO-DATA"
    assert info.value.details["field"] == field


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("value", [0, 256, -1, True, "100", 100.0])
def test_exit_for_updates_is_an_exit_status_from_1_to_255(value: object) -> None:
    raw = _base()
    raw["check_updates_exit_for_updates"] = value
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="fixture.toml")
    assert info.value.details["field"] == "check_updates_exit_for_updates"


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
def test_exit_for_updates_is_refused_for_a_multi_step_check_it_would_not_read() -> None:
    # Arch's check is four steps, and only a one-step check reads the exit status (ADR-011).
    raw = _shipped_raw("arch")
    raw["check_updates_exit_for_updates"] = 100
    with pytest.raises(GinnError) as info:
        from_mapping(raw, source="arch.toml")
    assert info.value.details["field"] == "check_updates_exit_for_updates"
    assert info.value.details["rule"] == "is read only by a one-step check-updates"


@pytest.mark.fr("FR-024")
@pytest.mark.fr("NFR-060")
def test_a_count_rule_of_each_shape_loads_from_the_data() -> None:
    raw = _base()
    raw["check_updates_count"] = "lines-matching"
    raw["check_updates_count_regex"] = r"^pkg "
    definition = from_mapping(raw, source="fixture.toml")
    assert definition.check_count is CountRule.LINES_MATCHING
    assert definition.check_count_regex is not None
    assert definition.check_count_regex.search("pkg 1.0")
    raw["check_updates_count"] = "nonblank-lines"
    del raw["check_updates_count_regex"]
    assert from_mapping(raw, source="fixture.toml").check_count is CountRule.NONBLANK_LINES
