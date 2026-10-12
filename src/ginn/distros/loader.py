# SPDX-License-Identifier: Apache-2.0
"""Distribution definitions as data (NFR-060, DR-005, FR-012, FR-013, FR-040, FR-041).

Each distribution is one TOML file in `data/`, named `<id>.toml`. Adding a distribution means a
new file and a Trust Manifest entry, with no code change (NFR-060). `_test.toml` is a fake
distribution for the tests. `available()` leaves it out unless asked.

Placeholders use the errors.py convention. `{name}` is replaced, and `{{` and `}}` are literal
braces. Three names are defined, each valid in one place only:
- `{packages}` in a verb, as a whole element. It splices one argv element per package name. Names
  are never split, joined or re-parsed, so shell characters stay inside one element. A verb with no
  `{packages}` element takes no packages, and `argv()` refuses them.
- `{lang}` in `integration.packages`. It is the language part of the host LANG (en_US.UTF-8
  gives `en`).
- `{locale}` in `integration.steps`. It is the language and territory of the host LANG (en_US).

Every distribution defines every verb in `Verb`. `refresh` loads the current package metadata.
Callers run `refresh` before `install`, `upgrade`, `check-updates`, `resolve` and `estimate` on a
fresh image, because the official images ship no package lists. On Arch, `refresh` is a full
sync-and-upgrade (-Syu), because Arch does not support refreshing the databases without upgrading.

A host LANG with no locale data to generate (unset, empty, `C`, `POSIX` or `C.UTF-8`) gets no
entries that use `{lang}` or `{locale}`. A host LANG that is set but is not a UTF-8 locale name
gets the same, and the realm uses C.UTF-8. The SRS has no rule that refuses such a LANG (FR-013),
so nothing is raised; `IntegrationSet.fallback_reason` says why. Nothing here runs a command.
Callers hand `argv()` to a subprocess as a list (NFR-SEC-005).

Shipped data that fails validation raises GINN-E-DISTRO-DATA, because it is a ginn bug. An unknown
distribution name raises GINN-E-OPTION-INVALID.
"""

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
TEST_DISTRO: Final = "_test"
PACKAGES_ELEMENT: Final = "{packages}"
C_UTF8: Final = "C.UTF-8"

_ID_RE: Final = re.compile(r"_?[a-z][a-z0-9-]{0,31}")
_LOCALE_RE: Final = re.compile(r"(?P<lang>[a-z]{2,3})(?:_(?P<territory>[A-Z]{2}))?\.(?i:utf-?8)")
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
        "integration",
        "verbs",
    }
)


class Verb(StrEnum):
    """The package-manager verbs every distribution defines (FR-040, FR-041)."""

    REFRESH = "refresh"
    INSTALL = "install"
    REMOVE = "remove"
    UPGRADE = "upgrade"
    CHECK_UPDATES = "check-updates"
    LIST_EXPLICIT = "list-explicit"
    RESOLVE = "resolve"
    ESTIMATE = "estimate"


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
    checks it against the manifest). `verbs` maps each Verb to its argv template.
    `name_pattern` is matched with fullmatch. `signature_error` is matched with search against the
    package manager's output (FR-012).
    """

    id: str
    source: str
    trust_key: str
    shell: str
    name_pattern: re.Pattern[str]
    signature_error: re.Pattern[str]
    integration: Integration
    verbs: Mapping[Verb, tuple[str, ...]]

    def argv(self, verb: Verb, packages: Sequence[str] = ()) -> list[str]:
        """Return the argv for `verb`, with `packages` spliced in at `{packages}`.

        Raises GINN-E-USAGE when the verb takes packages and none are given, and when packages are
        given to a verb that takes none. Raises GINN-E-PKG-NAME-INVALID for an empty name or one
        with a NUL byte. Package names are not checked against DR-003 here; callers pass validated
        names (domain.packages).
        """
        if isinstance(packages, str):
            raise TypeError("packages must be a sequence of names, not one str")
        template = self.verbs[verb]
        takes_packages = PACKAGES_ELEMENT in template
        if takes_packages and not packages:
            raise make("GINN-E-USAGE", detail=f"verb {verb.value} needs at least one package")
        if packages and not takes_packages:
            raise make("GINN-E-USAGE", detail=f"verb {verb.value} takes no packages")
        out: list[str] = []
        for element in template:
            if element == PACKAGES_ELEMENT:
                out.extend(_name(raw) for raw in packages)
            else:
                out.append(_render(element, {}))
        return out

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


def host_locale(lang_env: str | None) -> tuple[HostLocale | None, str | None]:
    """Reduce a host LANG value for the templates. Return (locale, fallback_reason).

    `locale` is None when no locale data is generated: LANG is unset or empty, or it is C, POSIX or
    C.UTF-8. Accepted UTF-8 values give their language and territory, for example en_US.UTF-8 gives
    en_US and de_DE.utf8 gives de_DE. `fallback_reason` is set only when LANG is set and is not a
    UTF-8 locale name. It names the value and says that the realm uses C.UTF-8.
    """
    if not lang_env or _C_LOCALE_RE.fullmatch(lang_env):
        return None, None
    match = _LOCALE_RE.fullmatch(lang_env)
    if match is None:
        return None, (
            f"host LANG {lang_env!r} is not a UTF-8 locale name such as en_US.UTF-8; "
            "the realm uses C.UTF-8 and gets no locale data"
        )
    lang = match["lang"]
    territory = match["territory"]
    return HostLocale(lang, f"{lang}_{territory}" if territory else lang), None


def available(*, include_test: bool = False) -> tuple[str, ...]:
    """Return the sorted distribution IDs with a data file. `_test` only when `include_test`."""
    names = sorted(
        entry.name.removesuffix(".toml")
        for entry in _data_dir().iterdir()
        if entry.name.endswith(".toml")
    )
    return tuple(
        name for name in names if _ID_RE.fullmatch(name) and (include_test or name != TEST_DISTRO)
    )


def load(name: str) -> DistroDef:
    """Load the shipped definition for `name` (a distribution ID, or `_test`).

    Raises GINN-E-OPTION-INVALID for a name with no data file, and GINN-E-DISTRO-DATA when the file
    fails validation or its `id` differs from its file name.
    """
    if not isinstance(name, str) or _ID_RE.fullmatch(name) is None:
        raise _unknown(name)
    entry = _data_dir() / f"{name}.toml"
    if not entry.is_file():
        raise _unknown(name)
    source = f"{DATA_PACKAGE}/{DATA_DIR}/{name}.toml"
    definition = parse_text(entry.read_text(encoding="utf-8"), source)
    if definition.id != name:
        raise _data_error(source, "id", f"{definition.id!r} must equal the file name {name!r}")
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
    integration = _integration(reader, raw["integration"], name_pattern)
    verbs = _verbs(reader, raw["verbs"])
    return DistroDef(
        id=distro_id,
        source=source,
        trust_key=trust_key,
        shell=shell,
        name_pattern=name_pattern,
        signature_error=signature_error,
        integration=integration,
        verbs=MappingProxyType(verbs),
    )


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


def _verbs(reader: "_Reader", value: object) -> dict[Verb, tuple[str, ...]]:
    table = reader.table(value, "verbs")
    _only_keys(reader, table, frozenset(v.value for v in Verb), "verbs")
    allowed = frozenset({"packages"})
    return {Verb(key): reader.argv(table[key], f"verbs.{key}", allowed, allowed) for key in table}


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
    reader: _Reader, table: Mapping[str, object], expected: frozenset[str], field: str
) -> None:
    unknown = sorted(set(table) - expected)
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


def _name(raw: object) -> str:
    if not isinstance(raw, str):
        raise TypeError(f"package names must be str, not {type(raw).__name__}")
    if raw == "" or "\0" in raw:
        raise make("GINN-E-PKG-NAME-INVALID", name=raw)
    return raw


def _data_dir() -> Traversable:
    return files(DATA_PACKAGE).joinpath(DATA_DIR)


def _unknown(name: object) -> GinnError:
    return make("GINN-E-OPTION-INVALID", option=f"--distro={name}", distro="any")


def _data_error(source: str, field: str, rule: str) -> GinnError:
    """Return GINN-E-DISTRO-DATA: shipped data broke a rule. The details name the file and field."""
    return make("GINN-E-DISTRO-DATA", file=source, field=field, rule=rule)


__all__ = [
    "C_UTF8",
    "DATA_DIR",
    "DATA_PACKAGE",
    "PACKAGES_ELEMENT",
    "SCHEMA",
    "TEST_DISTRO",
    "DistroDef",
    "HostLocale",
    "Integration",
    "IntegrationSet",
    "Verb",
    "available",
    "from_mapping",
    "host_locale",
    "load",
    "load_file",
    "parse_text",
]
