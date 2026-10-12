# SPDX-License-Identifier: Apache-2.0
"""Distribution data and loader: NFR-060 (data only), DR-003, DR-005, FR-012, FR-013, FR-040,
FR-041, NFR-SEC-005.

The shipped files are checked as data: their Integration Package Set against S-10 (the distro TOML
is the single source for it; trust.toml's copy is a duplicate to be removed later), their verbs
for non-interactive and signature-preserving flags, and their name patterns against
domain.packages. The `_test` fixture and renamed copies prove that a new distribution
needs no code change. Every validation rule in loader.py has a test that breaks it.
"""

import copy
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import loader
from ginn.distros.loader import (
    DistroDef,
    HostLocale,
    Verb,
    available,
    from_mapping,
    host_locale,
    load,
    load_file,
    parse_text,
)
from ginn.domain.enums import DistroId
from ginn.domain.packages import PACKAGE_PATTERNS
from ginn.errors import GinnError

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "src" / "ginn" / "distros" / "data"
SHIPPED = ("arch", "debian", "fedora", "kali")
PACKAGE_VERBS = (Verb.INSTALL, Verb.REMOVE, Verb.RESOLVE, Verb.ESTIMATE)
PKG_MANAGERS = frozenset({"pacman", "apt-get", "apt", "apt-mark", "apt-cache", "dnf"})
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


# ---- shipped data: NFR-060, DR-005 -----------------------------------------------------------


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("DR-005")
def test_available_lists_the_shipped_distributions_and_leaves_out_the_fixture() -> None:
    assert available() == SHIPPED
    assert available(include_test=True) == ("_test", *SHIPPED)


@pytest.mark.fr("DR-005")
def test_shipped_distributions_are_exactly_the_distro_ids() -> None:
    assert set(available()) == {d.value for d in DistroId}
    assert "_test" not in {d.value for d in DistroId}


@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", SHIPPED)
def test_each_shipped_distribution_loads_with_every_verb(name: str) -> None:
    definition = load(name)
    assert definition.id == name
    assert definition.trust_key == name
    assert definition.shell == "/bin/bash"
    assert set(definition.verbs) == set(Verb)


@pytest.mark.fr("NFR-060")
def test_test_fixture_loads_by_name() -> None:
    fixture = load("_test")
    assert fixture.id == "_test"
    assert fixture.argv(Verb.INSTALL, ["gimp"]) == ["fake-pm", "install", "gimp"]


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
def test_a_file_whose_id_differs_from_its_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "arch.toml").write_text(
        _shipped_text("_test").replace('id = "_test"', 'id = "debian"'), encoding="utf-8"
    )
    monkeypatch.setattr(loader, "_data_dir", lambda: tmp_path)
    with pytest.raises(GinnError) as info:
        load("arch")
    assert info.value.code == "GINN-E-INTERNAL"
    assert info.value.details["field"] == "id"


@pytest.mark.fr("DR-005")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", ["ubuntu", "Arch", "../arch", "", "arch/x", "-arch", "arch.toml"])
def test_unknown_or_unsafe_distribution_names_are_refused(name: str) -> None:
    with pytest.raises(GinnError) as info:
        load(name)
    _expect("GINN-E-OPTION-INVALID", 2)(info.value)


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
    assert load(name).name_pattern.pattern == PACKAGE_PATTERNS[DistroId(name)]


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
        ("arch", (), S10_STEPS),
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
@pytest.mark.parametrize("lang", [None, "", "C", "POSIX", "C.UTF-8"])
def test_c_and_unset_locales_drop_every_locale_entry(lang: str | None) -> None:
    debian = load("debian").integration_set(lang)
    assert debian.packages == ("ca-certificates", "locales")
    assert debian.steps == ()
    fedora = load("fedora").integration_set(lang)
    assert fedora.packages == ()
    assert fedora.steps == ()


@pytest.mark.fr("FR-013")
def test_other_locales_substitute_the_language_and_the_locale_name() -> None:
    assert load("fedora").integration_set("de_DE.utf8").packages == ("glibc-langpack-de",)
    debian_steps = load("debian").integration_set("de_DE.UTF-8").steps
    assert debian_steps == (("localedef", "-i", "de_DE", "-f", "UTF-8", "de_DE.UTF-8"),)


@pytest.mark.fr("FR-013")
@pytest.mark.parametrize(
    "lang", ["en_US.ISO-8859-1", "en_US", "EN_us.UTF-8", "en_US.UTF-8@euro", "en_US.UTF-8\n"]
)
def test_host_locales_that_cannot_be_used_are_refused(lang: str) -> None:
    with pytest.raises(GinnError) as info:
        load("debian").integration_set(lang)
    _expect("GINN-E-OPTION-INVALID", 2)(info.value)


@pytest.mark.fr("FR-013")
def test_host_locale_reduces_lang_to_language_and_territory() -> None:
    assert host_locale("en_US.UTF-8", "arch") == HostLocale("en", "en_US")
    assert host_locale("de.UTF-8", "arch") == HostLocale("de", "de")
    assert host_locale("C.UTF-8", "arch") is None


# ---- shipped data: FR-040, FR-041, FR-012 verbs ----------------------------------------------


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", SHIPPED)
def test_every_verb_is_an_argv_list_and_never_a_shell(name: str) -> None:
    definition = load(name)
    for verb in Verb:
        argv = definition.argv(verb, ["gimp"] if verb in PACKAGE_VERBS else [])
        assert argv, verb
        assert all(isinstance(element, str) for element in argv)
        assert argv[0] in PKG_MANAGERS, (name, verb, argv[0])
        assert not SHELLS & set(argv), (name, verb)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("name", SHIPPED)
def test_mutating_verbs_never_wait_for_a_prompt(name: str) -> None:
    definition = load(name)
    for verb in (Verb.INSTALL, Verb.REMOVE, Verb.UPGRADE):
        argv = definition.argv(verb, ["gimp"] if verb in PACKAGE_VERBS else [])
        assert YES_FLAGS & set(argv), (name, verb)


@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", SHIPPED)
def test_dry_run_verbs_never_carry_a_yes_flag(name: str) -> None:
    definition = load(name)
    for verb in (Verb.RESOLVE, Verb.ESTIMATE, Verb.CHECK_UPDATES, Verb.LIST_EXPLICIT):
        argv = definition.argv(verb, ["gimp"] if verb in PACKAGE_VERBS else [])
        assert not YES_FLAGS & set(argv), (name, verb)


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize("name", SHIPPED)
def test_no_verb_disables_a_native_signature_check(name: str) -> None:
    definition = load(name)
    for verb in Verb:
        text = " ".join(definition.argv(verb, ["gimp"])).lower()
        for token in SIGNATURE_BYPASS:
            assert token not in text, (name, verb, token)


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


# ---- placeholder expansion: NFR-SEC-005 ------------------------------------------------------


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("FR-040")
@pytest.mark.parametrize("name", [*SHIPPED, "_test"])
@pytest.mark.parametrize("verb", PACKAGE_VERBS)
def test_hostile_package_names_stay_one_argv_element_each(name: str, verb: Verb) -> None:
    out = load(name).argv(verb, NAMES)
    assert [element for element in out if element in NAMES] == NAMES
    assert all(isinstance(element, str) for element in out)


@pytest.mark.fr("NFR-SEC-005")
def test_package_names_are_not_reparsed_as_templates() -> None:
    out = from_mapping(_base()).argv(Verb.INSTALL, ["{lang}", "{packages}"])
    assert out == ["fake-pm", "install", "{lang}", "{packages}"]


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


# ---- signature errors: FR-012 ----------------------------------------------------------------

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
    ("integration is not a table", _set("integration", value=["x"])),
    ("integration.steps missing", _del("integration", "steps")),
    ("integration has an unknown key", _set("integration", "image", value="x")),
    ("integration package fails the name pattern", _set("integration", "packages", value=["Bad"])),
    ("integration package uses {locale}", _set("integration", "packages", value=["x-{locale}"])),
    ("integration step uses {lang}", _set("integration", "steps", value=[["x", "{lang}"]])),
    ("integration step is empty", _set("integration", "steps", value=[[]])),
    ("integration steps is not a list", _set("integration", "steps", value="x")),
    ("verbs missing remove", _del("verbs", "remove")),
    ("verbs has an unknown verb", _set("verbs", "check_updates", value=["x"])),
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
    assert err.code == "GINN-E-INTERNAL", label
    assert err.exit_code == 70
    assert err.details["file"] == "fixture.toml"
    assert isinstance(err.details["field"], str)
    assert isinstance(err.details["rule"], str)


@pytest.mark.fr("NFR-060")
def test_the_unbroken_fixture_validates() -> None:
    assert from_mapping(_base(), source="fixture.toml").id == "_test"


@pytest.mark.fr("NFR-040")
def test_toml_syntax_errors_are_refused_as_internal_errors() -> None:
    with pytest.raises(GinnError) as info:
        parse_text("schema = ", "broken.toml")
    assert info.value.code == "GINN-E-INTERNAL"
    assert info.value.details["field"] == "toml"


@pytest.mark.fr("NFR-060")
def test_a_definition_is_a_frozen_value() -> None:
    definition = load("arch")
    assert isinstance(definition, DistroDef)
    with pytest.raises(AttributeError):
        definition.shell = "/bin/sh"  # type: ignore[misc]


def _with_verb(key: str, template: list[str]) -> DistroDef:
    raw = copy.deepcopy(_base())
    raw["verbs"][key] = template
    return from_mapping(raw, source="fixture.toml")
