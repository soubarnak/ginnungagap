# SPDX-License-Identifier: Apache-2.0
"""Distribution definitions as data (NFR-060, DR-005, FR-012, FR-013, FR-040, FR-041).

Each distribution is one TOML file in `data/`, named `<id>.toml`. Adding a distribution means a
new file and a Trust Manifest entry, with no code change (NFR-060). A name that starts with `_` is a
test fixture, and `_test.toml` is one. Fixtures are hidden unless the test switch is on:
GINN_TEST_DISTROS=1, which only tests/conftest.py sets. `available()` leaves them out unless asked
for, and `load()` refuses them without the switch. `ginn.domain` reads this package for distribution
IDs (DR-005) and package-name patterns (DR-003), so this module must never import `ginn.domain`.

Placeholders use the errors.py convention. `{name}` is replaced, and `{{` and `}}` are literal
braces. Four names are defined, each valid in one place only:
- `{packages}` in a verb, as a whole element. It splices one argv element per package name. Names
  are never split, joined or re-parsed, so shell characters stay inside one element. A verb with no
  `{packages}` element takes no packages, and `argv()` refuses them.
- `{lang}` in `integration.packages`. It is the language part of the host LANG (en_US.UTF-8
  gives `en`).
- `{locale}` in `integration.steps`. It is the language and territory of the host LANG (en_US).
- `{tmpdir}` in the `check-updates` steps, as a whole element or inside one. It is the scratch
  directory the caller passes to `check_updates()`: an absolute path that does not exist yet.

Every distribution defines every verb in `Verb` except two. `check-updates` is a list of steps, run
in order, and `check_updates()` returns them. `remove-check` is optional: a distribution that omits
it has no removal pre-check, and `DistroDef.has_remove_check` says whether it defines one (ADR-011).
Every other verb is one argv. `refresh`
loads the package metadata a manager needs and never upgrades an installed package. `refresh_before`
lists the verbs the caller runs `refresh` before (ADR-011). The list is empty where the verb already
refreshes what it needs:
- Arch never refreshes the sync database outside a full upgrade (`upgrade` is `pacman -Syu`),
  because Arch does not support partial upgrades. Its `check-updates` syncs a scratch database
  selected by `--dbpath`, so the realm's own sync database is never written (ADR-011).
- Debian and Kali run `apt-get update` (lists only) before install, upgrade, check-updates, resolve
  and estimate.
- Fedora: `check-updates` passes dnf5's global `--refresh`, and dnf5 refreshes expired metadata by
  itself.
Realm creation runs `refresh` once, then installs the Integration Package Set, on every distribution
(ADR-011). The ops layer does that, not this module. On Arch the refresh is the full upgrade, so the
image's packages move to current versions at creation (CL-9).

A host LANG with no locale data to generate (unset, empty, `C`, `POSIX` or `C.UTF-8`) gets no
entries that use `{lang}` or `{locale}`. A host LANG that is set but is not a UTF-8 locale name with
a language and a territory (en_US.UTF-8, not en.UTF-8) gets the same, and the realm uses C.UTF-8.
The SRS has no rule that refuses such a LANG (FR-013), so nothing is raised; `IntegrationSet.
fallback_reason` says why. Nothing here runs a command. Callers hand `argv()` to a subprocess as a
list (NFR-SEC-005).

`stale_metadata` matches the package manager's output when the local metadata no longer matches the
mirror (ADR-011). The ops layer maps a match to the remedy.

Shipped data that fails validation raises GINN-E-DISTRO-DATA, because it is a ginn bug. An unknown
distribution name, or a fixture name while the switch is off, raises GINN-E-USAGE.
"""

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from string import Formatter
from types import MappingProxyType
from typing import Final

from ginn.errors import GinnError, make

SCHEMA: Final = 1
DATA_PACKAGE: Final = "ginn.distros"
DATA_DIR: Final = "data"
TEST_SWITCH_ENV: Final = "GINN_TEST_DISTROS"
PACKAGES_ELEMENT: Final = "{packages}"
TMPDIR_FIELD: Final = "tmpdir"
C_UTF8: Final = "C.UTF-8"

_ID_RE: Final = re.compile(r"_?[a-z][a-z0-9-]{0,31}")
_LOCALE_RE: Final = re.compile(r"(?P<lang>[a-z]{2,3})_(?P<territory>[A-Z]{2})\.(?i:utf-?8)")
_C_LOCALE_RE: Final = re.compile(r"C|POSIX|C\.(?i:utf-?8)")
_SAMPLE: Final[dict[str, str]] = {"lang": "xx", "locale": "xx_XX"}
_TOP_KEYS: Final = frozenset(
    {
        "schema",
        "id",
        "trust_key",
        "shell",
        "package_name_regex",
        "signature_error_regex",
        "stale_metadata_regex",
        "refresh_before",
        "integration",
        "verbs",
    }
)


class Verb(StrEnum):
    """The package-manager verbs (FR-040, FR-041). Every distribution defines each one except
    check-updates (a list of steps) and remove-check (optional, ADR-011)."""

    REFRESH = "refresh"
    INSTALL = "install"
    REMOVE = "remove"
    REMOVE_CHECK = "remove-check"
    UPGRADE = "upgrade"
    CHECK_UPDATES = "check-updates"
    LIST_EXPLICIT = "list-explicit"
    RESOLVE = "resolve"
    ESTIMATE = "estimate"


# The verb keys a distribution file must define, and the one it may omit (see the Verb docstring).
_OPTIONAL_VERB_KEYS: Final = frozenset({Verb.REMOVE_CHECK.value})
_REQUIRED_VERB_KEYS: Final = frozenset(verb.value for verb in Verb) - _OPTIONAL_VERB_KEYS


class UpdateStatus(StrEnum):
    """What the Arch check-updates steps found (FR-024, ADR-011)."""

    UPDATES = "updates"
    NONE = "none"
    ERROR = "error"


@dataclass(frozen=True)
class HostLocale:
    """The host LANG reduced to the two values the templates use."""

    lang: str
    name: str


@dataclass(frozen=True)
class Integration:
    """The Integration Package Set (FR-013) as templates, plus its locale steps.

    `steps` are argv lists run after `packages` are installed, as container root.
    """

    packages: tuple[str, ...]
    steps: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class IntegrationSet:
    """The Integration Package Set for one host locale, with placeholders filled in.

    `lang` is the LANG value the realm gets: the host LANG when it is usable, else C.UTF-8.
    `fallback_reason` is set when the host LANG is set and cannot be used (FR-013). The caller
    prints it. Nothing is refused.
    """

    packages: tuple[str, ...]
    steps: tuple[tuple[str, ...], ...]
    lang: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class DistroDef:
    """One validated distribution definition.

    `trust_key` names a table in the Trust Manifest. Only its non-emptiness is checked here (T2.14
    checks it against the manifest). `verbs` maps each Verb except check-updates to its argv
    template, and holds remove-check only where the data defines it. `check_steps` is the
    check-updates step list, one argv template per step.
    `name_pattern` is matched with fullmatch, by argv() and by the Integration Package Set.
    `signature_error` and `stale_metadata` are matched with search against the package manager's
    output (FR-012, ADR-011). `refresh_before` lists the verbs that the caller runs `refresh` before
    (ADR-011).
    """

    id: str
    source: str
    trust_key: str
    shell: str
    name_pattern: re.Pattern[str]
    signature_error: re.Pattern[str]
    stale_metadata: re.Pattern[str]
    refresh_before: frozenset[Verb]
    integration: Integration
    verbs: Mapping[Verb, tuple[str, ...]]
    check_steps: tuple[tuple[str, ...], ...]

    def argv(self, verb: Verb, packages: Sequence[str] = ()) -> list[str]:
        """Return the argv for `verb`, with `packages` spliced in at `{packages}`.

        Raises GINN-E-USAGE when the verb takes packages and none are given, when packages are
        given to a verb that takes none, and when the data does not define the verb (remove-check
        on a distribution without it). Every package name must fullmatch `name_pattern` (DR-003),
        or GINN-E-PKG-NAME-INVALID is raised before any element is built. No shipped pattern accepts
        a leading `-`, so a name that passes can never be read as an option. domain.packages checks
        AS-027 first; this check is the second line of defence. check-updates is not one argv, so
        it is refused here; see check_updates().
        """
        if isinstance(packages, str):
            raise TypeError("packages must be a sequence of names, not one str")
        if verb is Verb.CHECK_UPDATES:
            raise make(
                "GINN-E-USAGE", detail="verb check-updates is a list of steps; use check_updates()"
            )
        template = self.verbs.get(verb)
        if template is None:
            raise make("GINN-E-USAGE", detail=f"verb {verb.value} is not defined for {self.id}")
        takes_packages = PACKAGES_ELEMENT in template
        if takes_packages and not packages:
            raise make("GINN-E-USAGE", detail=f"verb {verb.value} needs at least one package")
        if packages and not takes_packages:
            raise make("GINN-E-USAGE", detail=f"verb {verb.value} takes no packages")
        out: list[str] = []
        for element in template:
            if element == PACKAGES_ELEMENT:
                out.extend(self._checked(raw) for raw in packages)
            else:
                out.append(_render(element, {}))
        return out

    @property
    def has_remove_check(self) -> bool:
        """True when the data defines remove-check, the removal dry run (ADR-011)."""
        return Verb.REMOVE_CHECK in self.verbs

    @property
    def check_updates_needs_tmpdir(self) -> bool:
        """True when a check-updates step uses `{tmpdir}`: the caller must pass a scratch path."""
        return any(
            TMPDIR_FIELD in _field_names(element) for step in self.check_steps for element in step
        )

    def check_updates(self, tmpdir: str | None = None) -> tuple[list[str], ...]:
        """Return the check-updates steps as argv lists, with `{tmpdir}` filled in, in run order.

        The caller runs the steps in order, stops at the first failure, and removes `tmpdir`
        afterwards (ADR-011). `tmpdir` is required when `check_updates_needs_tmpdir` is true and
        refused otherwise. Raises GINN-E-USAGE for a missing, unexpected or unusable path.
        """
        values: dict[str, str] = {}
        if self.check_updates_needs_tmpdir:
            if tmpdir is None:
                raise make("GINN-E-USAGE", detail="verb check-updates needs a tmpdir")
            values[TMPDIR_FIELD] = _checked_tmpdir(tmpdir)
        elif tmpdir is not None:
            raise make("GINN-E-USAGE", detail="verb check-updates takes no tmpdir")
        steps: list[list[str]] = []
        for step in self.check_steps:
            steps.append([_render(element, values) for element in step])
        return tuple(steps)

    def integration_set(self, lang_env: str | None) -> IntegrationSet:
        """Return the Integration Package Set for a host LANG value (FR-013).

        Entries that use a placeholder with no host value are dropped: that covers unset, C,
        POSIX, C.UTF-8 and a LANG that cannot be used. A rendered package name that fails the
        distribution's name pattern is a data bug, and raises GINN-E-DISTRO-DATA.
        """
        locale, fallback_reason = host_locale(lang_env)
        values: dict[str, str] = {}
        if locale is not None:
            values = {"lang": locale.lang, "locale": locale.name}
        packages: list[str] = []
        for template in self.integration.packages:
            if not _field_names(template) <= frozenset(values):
                continue
            name = _render(template, values)
            if self.name_pattern.fullmatch(name) is None:
                raise _data_error(
                    self.source, "integration.packages", f"{name!r} fails package_name_regex"
                )
            packages.append(name)
        steps: list[tuple[str, ...]] = []
        for step in self.integration.steps:
            if all(_field_names(element) <= frozenset(values) for element in step):
                steps.append(tuple(_render(element, values) for element in step))
        lang = lang_env if lang_env and fallback_reason is None else C_UTF8
        return IntegrationSet(tuple(packages), tuple(steps), lang, fallback_reason)

    def _checked(self, raw: object) -> str:
        """Return `raw` when it is a str that fullmatches `name_pattern`, else raise (DR-003)."""
        if not isinstance(raw, str):
            raise TypeError(f"package names must be str, not {type(raw).__name__}")
        if self.name_pattern.fullmatch(raw) is None:
            err = make("GINN-E-PKG-NAME-INVALID", name=raw)
            err.details["rule"] = f"must match {self.name_pattern.pattern} for {self.id} (DR-003)"
            raise err
        return raw


_WARNING_PREFIX: Final = "warning:"


def arch_check_status(
    sync_exit: int, query_exit: int, query_stdout: str, query_stderr: str
) -> UpdateStatus:
    """Return what the Arch check-updates steps found, from their results (FR-024, ADR-011).

    The caller runs step 3 (the scratch sync) and step 4 (the query), then passes their exit
    statuses and output here. Nothing is run. The statuses are observed, not documented, so any
    result that matches no rule below is an error, never "no updates":
    - step 3 exits 0, or the result is an error;
    - step 4 exits 0 with a package on stdout: updates. Blank stdout is an error;
    - step 4 exits 1, stdout is blank, and every non-blank stderr line starts with `warning:`:
      no updates. Empty stderr also counts;
    - anything else is an error.
    """
    if sync_exit != 0:
        return UpdateStatus.ERROR
    if query_exit == 0:
        return UpdateStatus.UPDATES if query_stdout.strip() else UpdateStatus.ERROR
    if query_exit == 1 and not query_stdout.strip() and _only_warnings(query_stderr):
        return UpdateStatus.NONE
    return UpdateStatus.ERROR


def _only_warnings(stderr: str) -> bool:
    """True when every non-blank line of `stderr` starts with `warning:`."""
    return all(line.startswith(_WARNING_PREFIX) for line in stderr.splitlines() if line.strip())


_CACHE: dict[tuple[str, str], DistroDef] = {}


def distro_fixtures_enabled() -> bool:
    """True when the test switch is on: GINN_TEST_DISTROS is exactly "1" (NFR-060).

    Only tests/conftest.py sets it. Without it, a fixture name is not a distribution.
    """
    return os.environ.get(TEST_SWITCH_ENV) == "1"


def clear_cache() -> None:
    """Forget every loaded definition. Tests that change the data directory call this."""
    _CACHE.clear()


def host_locale(lang_env: str | None) -> tuple[HostLocale | None, str | None]:
    """Reduce a host LANG value for the templates. Return (locale, fallback_reason).

    `locale` is None when no locale data is generated: LANG is unset or empty, or it is C, POSIX or
    C.UTF-8. Accepted UTF-8 values give their language and territory, for example en_US.UTF-8 gives
    en_US and de_DE.utf8 gives de_DE. `fallback_reason` is set only when LANG is set and is not a
    UTF-8 locale name with a language and a territory. It names the value and says that the realm
    uses C.UTF-8. A language alone (en.UTF-8) is not accepted: localedef needs a territory.
    """
    if not lang_env or _C_LOCALE_RE.fullmatch(lang_env):
        return None, None
    match = _LOCALE_RE.fullmatch(lang_env)
    if match is None:
        return None, (
            f"host LANG {lang_env!r} is not a UTF-8 locale name with a language and territory, "
            "such as en_US.UTF-8; the realm uses C.UTF-8 and gets no locale data"
        )
    lang = match["lang"]
    territory = match["territory"]
    return HostLocale(lang, f"{lang}_{territory}"), None


def available(*, include_test: bool = False) -> tuple[str, ...]:
    """Return the sorted distribution IDs with a data file.

    A fixture name (starts with `_`) is listed only when `include_test` is true and the test switch
    is on. The default never lists one.
    """
    fixtures_ok = include_test and distro_fixtures_enabled()
    names = sorted(
        entry.name.removesuffix(".toml")
        for entry in _data_dir().iterdir()
        if entry.name.endswith(".toml")
    )
    return tuple(
        name
        for name in names
        if _ID_RE.fullmatch(name) and (fixtures_ok or not name.startswith("_"))
    )


def load(name: str) -> DistroDef:
    """Load the shipped definition for `name` (a distribution ID, or a fixture with the switch on).

    Raises GINN-E-USAGE for a name with no data file, and for a fixture name while the switch is
    off. Raises GINN-E-DISTRO-DATA when the file fails validation or its `id` differs from its file
    name. A successful load is cached per data directory until `clear_cache()`.
    """
    if not isinstance(name, str) or _ID_RE.fullmatch(name) is None:
        raise unknown_distribution_error(name)
    if name.startswith("_") and not distro_fixtures_enabled():
        raise unknown_distribution_error(name)
    data_dir = _data_dir()
    key = (str(data_dir), name)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    entry = data_dir / f"{name}.toml"
    if not entry.is_file():
        raise unknown_distribution_error(name)
    source = f"{DATA_PACKAGE}/{DATA_DIR}/{name}.toml"
    definition = parse_text(entry.read_text(encoding="utf-8"), source)
    if definition.id != name:
        raise _data_error(source, "id", f"{definition.id!r} must equal the file name {name!r}")
    _CACHE[key] = definition
    return definition


def load_file(path: Path) -> DistroDef:
    """Load a definition from any TOML file. Used by tests and by the NFR-060 fixture."""
    return parse_text(path.read_text(encoding="utf-8"), str(path))


def parse_text(text: str, source: str) -> DistroDef:
    """Parse TOML text and validate it. `source` names the origin in error details."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _data_error(source, "toml", str(exc)) from None
    return from_mapping(raw, source)


def from_mapping(raw: Mapping[str, object], source: str = "<mapping>") -> DistroDef:
    """Validate a parsed distribution table and build a `DistroDef`.

    The first broken rule raises GINN-E-DISTRO-DATA. Its details carry `file`, `field` and `rule`.
    """
    reader = _Reader(source)
    _only_keys(reader, raw, _TOP_KEYS, "top level")
    if type(raw["schema"]) is not int or raw["schema"] != SCHEMA:
        raise reader.fail("schema", f"must be the integer {SCHEMA}")
    distro_id = reader.text(raw["id"], "id")
    if _ID_RE.fullmatch(distro_id) is None:
        raise reader.fail("id", "must match _?[a-z][a-z0-9-]{0,31}")
    trust_key = reader.text(raw["trust_key"], "trust_key")
    shell = reader.text(raw["shell"], "shell")
    if not PurePosixPath(shell).is_absolute() or any(ch.isspace() for ch in shell):
        raise reader.fail("shell", "must be an absolute path with no whitespace")
    name_pattern = reader.regex(raw["package_name_regex"], "package_name_regex")
    signature_error = reader.regex(raw["signature_error_regex"], "signature_error_regex")
    stale_metadata = reader.regex(raw["stale_metadata_regex"], "stale_metadata_regex")
    refresh_before = _refresh_before(reader, raw["refresh_before"])
    integration = _integration(reader, raw["integration"], name_pattern)
    verbs, check_steps = _verbs(reader, raw["verbs"])
    return DistroDef(
        id=distro_id,
        source=source,
        trust_key=trust_key,
        shell=shell,
        name_pattern=name_pattern,
        signature_error=signature_error,
        stale_metadata=stale_metadata,
        refresh_before=refresh_before,
        integration=integration,
        verbs=MappingProxyType(verbs),
        check_steps=check_steps,
    )


def _refresh_before(reader: "_Reader", value: object) -> frozenset[Verb]:
    names = reader.strings(value, "refresh_before")
    known = {verb.value for verb in Verb}
    for name in names:
        if name not in known:
            raise reader.fail("refresh_before", f"unknown verb {name!r}")
        if name == Verb.REFRESH.value:
            raise reader.fail("refresh_before", "must not list refresh; it is the metadata load")
    if len(set(names)) != len(names):
        raise reader.fail("refresh_before", "must not repeat a verb")
    return frozenset(Verb(name) for name in names)


def _integration(reader: "_Reader", value: object, name_pattern: re.Pattern[str]) -> Integration:
    table = reader.table(value, "integration")
    _only_keys(reader, table, frozenset({"packages", "steps"}), "integration")
    packages = reader.strings(table["packages"], "integration.packages")
    for template in packages:
        reader.check_template(template, "integration.packages", frozenset({"lang"}), frozenset())
        if name_pattern.fullmatch(_render(template, _SAMPLE)) is None:
            raise reader.fail(
                "integration.packages", f"{template!r} does not match package_name_regex"
            )
    raw_steps = table["steps"]
    if not isinstance(raw_steps, list):
        raise reader.fail("integration.steps", "must be a list of argv lists")
    steps = tuple(
        reader.argv(step, "integration.steps", frozenset({"locale"}), frozenset())
        for step in raw_steps
    )
    return Integration(packages=packages, steps=steps)


def _verbs(
    reader: "_Reader", value: object
) -> tuple[dict[Verb, tuple[str, ...]], tuple[tuple[str, ...], ...]]:
    table = reader.table(value, "verbs")
    _only_keys(reader, table, _REQUIRED_VERB_KEYS, "verbs", _OPTIONAL_VERB_KEYS)
    allowed = frozenset({"packages"})
    verbs: dict[Verb, tuple[str, ...]] = {}
    for key in table:
        if key == Verb.CHECK_UPDATES.value:
            continue
        argv = reader.argv(table[key], f"verbs.{key}", allowed, allowed)
        if key == Verb.REMOVE_CHECK.value and PACKAGES_ELEMENT not in argv:
            raise reader.fail(f"verbs.{key}", "must include {packages} as one element")
        verbs[Verb(key)] = argv
    return verbs, _check_steps(reader, table[Verb.CHECK_UPDATES.value])


def _check_steps(reader: "_Reader", value: object) -> tuple[tuple[str, ...], ...]:
    """Validate the check-updates step list: a non-empty list of argv lists (ADR-011)."""
    field = f"verbs.{Verb.CHECK_UPDATES.value}"
    if not isinstance(value, list) or not value:
        raise reader.fail(field, "must be a non-empty list of argv lists")
    steps: list[tuple[str, ...]] = []
    for step in value:
        if not isinstance(step, list):
            raise reader.fail(field, "every step must be an argv list")
        steps.append(reader.argv(step, field, frozenset({TMPDIR_FIELD}), frozenset()))
    return tuple(steps)


class _Reader:
    """Checks one raw table. Each broken rule raises GINN-E-DISTRO-DATA naming the field."""

    def __init__(self, source: str) -> None:
        self.source = source

    def fail(self, field: str, rule: str) -> GinnError:
        return _data_error(self.source, field, rule)

    def text(self, value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.fail(field, "must be a non-empty string")
        return value

    def table(self, value: object, field: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise self.fail(field, "must be a table")
        return value

    def strings(self, value: object, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise self.fail(field, "must be a list of non-empty strings")
        return tuple(value)

    def regex(self, value: object, field: str) -> re.Pattern[str]:
        pattern = self.text(value, field)
        try:
            return re.compile(pattern)
        except re.error as exc:
            raise self.fail(field, f"does not compile: {exc}") from None

    def argv(
        self,
        value: object,
        field: str,
        allowed: frozenset[str],
        whole: frozenset[str],
    ) -> tuple[str, ...]:
        items = self.strings(value, field)
        if not items:
            raise self.fail(field, "must be a non-empty list")
        for item in items:
            self.check_template(item, field, allowed, whole)
        if _field_names(items[0]) or any(ch.isspace() for ch in items[0]):
            raise self.fail(field, "the first element must be a bare command name")
        return items

    def check_template(
        self,
        element: str,
        field: str,
        allowed: frozenset[str],
        whole: frozenset[str],
    ) -> None:
        """Reject bad braces, unknown placeholders, and placeholders not used as whole elements."""
        try:
            fields = [
                (name, spec, conv)
                for _, name, spec, conv in Formatter().parse(element)
                if name is not None
            ]
        except ValueError as exc:
            raise self.fail(field, f"bad braces in {element!r}: {exc}") from None
        for name, spec, conv in fields:
            if name not in allowed:
                raise self.fail(field, f"placeholder {{{name}}} is not allowed here")
            if spec or conv:
                raise self.fail(field, "format specs and conversions are not allowed")
            if name in whole and element != "{" + name + "}":
                raise self.fail(field, f"{{{name}}} must be the whole element")


def _only_keys(
    reader: _Reader,
    table: Mapping[str, object],
    expected: frozenset[str],
    field: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    """Refuse a key outside `expected` and `optional`, and a missing key from `expected`."""
    unknown = sorted(set(table) - expected - optional)
    if unknown:
        raise reader.fail(field, f"unknown key(s): {', '.join(unknown)}")
    missing = sorted(expected - set(table))
    if missing:
        raise reader.fail(field, f"missing key(s): {', '.join(missing)}")


def _field_names(template: str) -> frozenset[str]:
    return frozenset(name for _, name, _, _ in Formatter().parse(template) if name is not None)


def _render(template: str, values: Mapping[str, str]) -> str:
    """Fill placeholders from `values`. Values are inserted as text and never re-parsed."""
    out: list[str] = []
    for literal, name, _spec, _conv in Formatter().parse(template):
        out.append(literal)
        if name is not None:
            out.append(values[name])
    return "".join(out)


def _checked_tmpdir(raw: object) -> str:
    """Return `raw` when it is an absolute path that is not `/` and has no `..` (ADR-011)."""
    if not isinstance(raw, str):
        raise TypeError(f"tmpdir must be str, not {type(raw).__name__}")
    path = PurePosixPath(raw)
    below_root = path.is_absolute() and len(path.parts) > 1
    if not raw or "\0" in raw or not below_root or ".." in path.parts:
        raise make(
            "GINN-E-USAGE", detail=f"tmpdir {raw!r} must be an absolute path below / with no '..'"
        )
    return raw


def _data_dir() -> Traversable:
    return files(DATA_PACKAGE).joinpath(DATA_DIR)


def unknown_distribution_error(name: object) -> GinnError:
    """Return GINN-E-USAGE for a name with no definition. The list never names a fixture.

    `DistroId` raises this too, so both places word an unknown distribution the same way.
    """
    return make(
        "GINN-E-USAGE",
        detail=f"unknown distribution {name!r}; available: {', '.join(available())}",
    )


def _data_error(source: str, field: str, rule: str) -> GinnError:
    """Return GINN-E-DISTRO-DATA: shipped data broke a rule. The details name the file and field."""
    return make("GINN-E-DISTRO-DATA", file=source, field=field, rule=rule)


__all__ = [
    "C_UTF8",
    "DATA_DIR",
    "DATA_PACKAGE",
    "PACKAGES_ELEMENT",
    "SCHEMA",
    "TEST_SWITCH_ENV",
    "TMPDIR_FIELD",
    "DistroDef",
    "HostLocale",
    "Integration",
    "IntegrationSet",
    "UpdateStatus",
    "Verb",
    "arch_check_status",
    "available",
    "clear_cache",
    "distro_fixtures_enabled",
    "from_mapping",
    "host_locale",
    "load",
    "load_file",
    "parse_text",
    "unknown_distribution_error",
]
