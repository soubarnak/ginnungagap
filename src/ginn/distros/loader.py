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

Every distribution defines every verb in `Verb` except four. `check-updates` is a list of steps,
run in order, and `check_updates()` returns them. `clean` is an optional list of steps too
(ADR-015): `clean_steps` holds them, and `has_clean` says whether the data defines it.
`remove-check` and `db-check` are optional: a distribution that omits one has no such check.
`DistroDef.has_remove_check` and `has_db_check` say whether it defines one (ADR-011). A caller that
asks for an undefined one anyway gets GINN-E-INTERNAL: callers check the flag first. `db-check`
takes no packages, and no placeholder. `clean` takes no placeholder either.
Every other verb is one argv. `refresh`
loads the package metadata a manager needs and never upgrades an installed package. `refresh_before`
lists the verbs the caller runs `refresh` before (ADR-011). Only the verbs in `_REFRESH_PRECEDES`
may be listed. The list is empty where the verb already refreshes what it needs:
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

`missing_package_regex` matches the package manager's output when it says that a named package is
not available upstream (FR-022 AC-1). It is optional and defaults to no pattern, and with no pattern
no refusal is read as a missing package, so a rebuild fails closed. A match is positive evidence
only. The rebuild also refuses to read a missing package from output that says the disk is full.

`check_fatal_warning_regex` is optional and defaults to an empty list. It lists warnings that make
the Arch check-updates fail closed: `arch_check_status()` returns error when a stderr line matches
one, so a missing sync database is never read as "no updates" (ADR-011).

How the check output is counted is data too (FR-024). `check_updates_count` is `unread` (the
default: the count is None), `nonblank-lines` (one line per upgradable package) or `lines-matching`,
which counts the lines that `check_updates_count_regex` finds. `check_updates_exit_for_updates`
is an optional exit status that means updates exist, for a one-step check (dnf5's 100). Each key
is read by ginn.packages.manager.

The db-check verb (FR-120 AC-2) is read by `db_check_status()`. `db_check_ok_exit` names the exit
statuses that mean consistent, and `db_check_stdout_inconsistent` says whether any stdout then
means inconsistent. Any other status is inconsistent, except the statuses podman uses when it
cannot run the command (`CANNOT_RUN_EXIT`), which say nothing about the database.

`everything` is optional and defaults to an empty list. It names the packages of the everything
option (FR-014, Kali only), and each one is matched with `package_name_regex`.

`export_hook` is an optional table (ADR-016): the package manager's post-transaction hook that
signals the host after a native package change, so the host menu follows `pacman -S`, `apt install`
and `dnf install` run inside the realm (FR-044, FR-045). It holds `path` (the hook file inside the
realm, absolute) and `text` (the file, with `{stamp}` for the stamp file that the hook touches and
no other placeholder; the host makes the directory, never the hook). The table names no
package: the Integration Package Set stays as FR-013 and CR-007 limit it (CL-35). A distribution
without the table, or whose package manager lacks the needed plugin, gets no signal for native
transactions; the sync after `ginn install` still runs.

`services_packages` is optional and defaults to an empty list. It is the init set of a services-on
realm (ADR-015), which the provisioning stage installs. `services_estimated_installed_bytes` is
optional and defaults to 0. It is the installed size of that set, measured on the pinned image, and
it is 0 exactly when the set is empty. Arch's set is empty, because its image already ships systemd.

`integration_estimated_installed_bytes` and `package_cache_peak_bytes` are the other two inputs of
the disk formula of a services-on create (FR-121, ADR-015 Decision 11). The first is the installed
size of the Integration Package Set, and the second is the bytes that the clean removes from the
provisioning layer (the package cache, and on debian, kali and fedora the lists). Arch's first is 0,
because its image already holds the set. `writable_layer_bytes` is the writable layer of one
services-off container after an htop install, the term of a reuse build's check. All three are
optional non-negative integers, and each defaults to 0.

`base_layer_allocated_bytes` and `stage_layer_allocated_bytes` are the allocated bytes of the base
layer and of the stage layer, measured on podman 5.8.8 on ext4 (T7.33, ADR-015 Decision 11). They
are the base and stage terms of the services-on storage formula. Arch's stage layer is not 0 with
an empty init set, because the refresh leaves its sync databases in it. Both are optional
non-negative integers, and each defaults to 0.

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
_MAX_EXIT: Final = 255
"""The largest exit status a process returns. The data's exit statuses are checked against it."""

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
        "network_error_regex",
        "refresh_before",
        "integration",
        "verbs",
    }
)
# Optional top-level keys. A file may omit them, and the loader then uses the default.
_OPTIONAL_TOP_KEYS: Final = frozenset(
    {
        "check_fatal_warning_regex",
        "missing_package_regex",
        "export_hook",
        "everything",
        "check_updates_count",
        "check_updates_count_regex",
        "check_updates_exit_for_updates",
        "db_check_ok_exit",
        "db_check_stdout_inconsistent",
        "services_packages",
        "services_estimated_installed_bytes",
        "integration_estimated_installed_bytes",
        "package_cache_peak_bytes",
        "writable_layer_bytes",
        "base_layer_allocated_bytes",
        "stage_layer_allocated_bytes",
    }
)
# The keys that read the db-check result. A file that defines the db-check verb must define them.
_DB_CHECK_KEYS: Final = frozenset({"db_check_ok_exit", "db_check_stdout_inconsistent"})


class Verb(StrEnum):
    """The package-manager verbs (FR-040, FR-041). Every distribution defines each one except
    check-updates (a list of steps), clean (a list of steps, optional, ADR-015), remove-check and
    db-check (optional, ADR-011)."""

    REFRESH = "refresh"
    INSTALL = "install"
    REMOVE = "remove"
    REMOVE_CHECK = "remove-check"
    UPGRADE = "upgrade"
    CHECK_UPDATES = "check-updates"
    DB_CHECK = "db-check"
    LIST_EXPLICIT = "list-explicit"
    RESOLVE = "resolve"
    ESTIMATE = "estimate"
    CLEAN = "clean"


# The verb keys a distribution file must define, and the ones it may omit (see the Verb docstring).
_OPTIONAL_VERB_KEYS: Final = frozenset(
    {Verb.REMOVE_CHECK.value, Verb.DB_CHECK.value, Verb.CLEAN.value}
)
_REQUIRED_VERB_KEYS: Final = frozenset(verb.value for verb in Verb) - _OPTIONAL_VERB_KEYS
# The verbs a metadata refresh may precede (ADR-011): the ones that read the realm's sync data. The
# verbs that need no lists (remove, remove-check, list-explicit, db-check) are not in it, and clean
# is not either, because it empties the lists that a refresh would have fetched.
_REFRESH_PRECEDES: Final = frozenset(
    {Verb.INSTALL, Verb.UPGRADE, Verb.CHECK_UPDATES, Verb.RESOLVE, Verb.ESTIMATE}
)


class UpdateStatus(StrEnum):
    """What the Arch check-updates steps found (FR-024, ADR-011)."""

    UPDATES = "updates"
    NONE = "none"
    ERROR = "error"


class CountRule(StrEnum):
    """How the check output counts the upgradable packages it lists (FR-024, NFR-060).

    `UNREAD` leaves the count None. `NONBLANK_LINES` counts each non-blank line. `LINES_MATCHING`
    counts the lines that `check_updates_count_regex` finds.
    """

    UNREAD = "unread"
    NONBLANK_LINES = "nonblank-lines"
    LINES_MATCHING = "lines-matching"


class DbStatus(StrEnum):
    """What the db-check verb found (FR-120 AC-2). UNKNOWN is a result that says nothing."""

    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"
    UNKNOWN = "unknown"


CANNOT_RUN_EXIT: Final = frozenset({125, 126, 127})
"""The statuses podman reports when it cannot run the command: podman failed (125), the command
cannot be invoked (126), or it is not found (127). They say nothing about the package database, so
`db_check_status()` reads them as UNKNOWN, not as inconsistent."""


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


STAMP_FIELD: Final = "stamp"
"""The placeholder of an export hook's text for the stamp file the hook touches (ADR-016)."""
_EXPORT_HOOK_KEYS: Final = frozenset({"path", "text"})


@dataclass(frozen=True)
class ExportHook:
    """The post-transaction hook of one distribution's package manager (ADR-016).

    `path` is the absolute hook file inside the realm. `text` is the file with `{stamp}` where the
    stamp path goes.
    """

    path: str
    text: str

    def render(self, stamp: str) -> str:
        """The hook file for the stamp path `stamp`. The path is inserted as text, never parsed."""
        return _render(self.text, {STAMP_FIELD: stamp})


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
    checks it against the manifest). `verbs` maps each Verb except check-updates and clean to its
    argv template, and holds remove-check only where the data defines it. `check_steps` is the
    check-updates step list, one argv template per step. `clean_steps` is the clean step list, one
    argv per step, with no placeholder.
    `name_pattern` is matched with fullmatch, by argv() and by the Integration Package Set.
    `signature_error` and `stale_metadata` are matched with search against the package manager's
    output (FR-012, ADR-011). `missing_package` is matched the same way, and None when the data
    omits the key (FR-022 AC-1). `refresh_before` lists the verbs that the caller runs `refresh`
    before (ADR-011), each one in `_REFRESH_PRECEDES`. `check_fatal_warnings` are matched with
    search against each stderr line of the check-updates query, and a match makes the check fail
    closed (ADR-011). It is empty when the data omits the key.
    """

    id: str
    source: str
    trust_key: str
    shell: str
    name_pattern: re.Pattern[str]
    signature_error: re.Pattern[str]
    stale_metadata: re.Pattern[str]
    network_error: re.Pattern[str]
    missing_package: re.Pattern[str] | None
    refresh_before: frozenset[Verb]
    integration: Integration
    verbs: Mapping[Verb, tuple[str, ...]]
    check_steps: tuple[tuple[str, ...], ...]
    check_fatal_warnings: tuple[re.Pattern[str], ...] = ()
    everything: tuple[str, ...] = ()
    """The packages of the everything option (FR-014), such as kali-linux-everything. Empty when
    the distribution has none. Only Kali has one (DR-013)."""
    check_count: CountRule = CountRule.UNREAD
    """How the check output counts the packages it lists (FR-024). See `CountRule`."""
    check_count_regex: re.Pattern[str] | None = None
    """The line pattern of `CountRule.LINES_MATCHING`, and None for the other rules."""
    check_exit_for_updates: int | None = None
    """The exit status of a one-step check that means updates exist (dnf5's 100), or None."""
    db_check_ok_exit: frozenset[int] = frozenset()
    """The exit statuses of the db-check verb that mean the database is consistent (FR-120 AC-2).
    Empty when the distribution defines no db-check verb."""
    db_check_stdout_inconsistent: bool = False
    """True when, with an ok exit status, any stdout of the db-check verb means inconsistent."""
    services_packages: tuple[str, ...] = ()
    """The init set of a services-on realm (ADR-015): the packages that the provisioning stage
    installs for systemd and its D-Bus. Empty where the image already ships an init (Arch)."""
    services_estimated_installed_bytes: int = 0
    """The installed size of `services_packages` on the pinned image, measured in S-11 (ADR-015).
    It is 0 exactly when `services_packages` is empty."""
    clean_steps: tuple[tuple[str, ...], ...] = ()
    """The clean verb (ADR-015): argv lists, run in order, that empty the package cache. Empty when
    the data defines none. See `has_clean`."""
    integration_estimated_installed_bytes: int = 0
    """The installed size of the Integration Package Set, the `ips_delta` of the disk formula
    (FR-121, ADR-015 Decision 11), measured in S-11. 0 where the image already holds the set."""
    package_cache_peak_bytes: int = 0
    """The bytes that the clean steps remove from the provisioning layer: the `cache_peak` of the
    disk formula (FR-121, ADR-015 Decision 11). Measured in S-11, and on arch in T7.33."""
    export_hook: ExportHook | None = None
    """The package manager's post-transaction hook that signals the host (ADR-016), or None."""
    writable_layer_bytes: int = 0
    """The writable layer of one services-off container from the derived image, after an htop
    install, measured in S-11. It is the term of a reuse build's disk check (FR-121, ADR-015
    Decision 11). 0 is no term."""
    base_layer_allocated_bytes: int = 0
    """The allocated bytes of the pinned base layer, measured on podman 5.8.8 on ext4 with 4 KiB
    blocks (T7.33). It is the base term of the services-on storage formula (FR-121, ADR-015
    Decision 11). 0 is no term."""
    stage_layer_allocated_bytes: int = 0
    """The allocated bytes of the stage layer, which holds the init set, the Integration Package
    Set, the locale steps and the refresh residue (arch's sync databases), measured on podman 5.8.8
    on ext4 with 4 KiB blocks (T7.33). It is the stage term of the services-on storage formula
    (FR-121, ADR-015 Decision 11). 0 is no term."""

    def argv(self, verb: Verb, packages: Sequence[str] = ()) -> list[str]:
        """Return the argv for `verb`, with `packages` spliced in at `{packages}`.

        Raises GINN-E-USAGE when the verb takes packages and none are given, and when packages are
        given to a verb that takes none. Raises GINN-E-INTERNAL when the data does not define the
        verb. Only remove-check and db-check can be missing, on a distribution without them, and
        callers check `has_remove_check` and `has_db_check` first, so this is a ginn bug. Every
        package name must fullmatch `name_pattern` (DR-003),
        or GINN-E-PKG-NAME-INVALID is raised before any element is built. No shipped pattern accepts
        a leading `-`, so a name that passes can never be read as an option. domain.packages checks
        AS-027 first; this check is the second line of defence. check-updates and clean are not one
        argv each, so they are refused here; see check_updates() and clean_steps.
        """
        if isinstance(packages, str):
            raise TypeError("packages must be a sequence of names, not one str")
        if verb is Verb.CHECK_UPDATES:
            raise make(
                "GINN-E-USAGE", detail="verb check-updates is a list of steps; use check_updates()"
            )
        if verb is Verb.CLEAN:
            raise make("GINN-E-USAGE", detail="verb clean is a list of steps; use clean_steps")
        template = self.verbs.get(verb)
        if template is None:
            err = make("GINN-E-INTERNAL")
            err.details["detail"] = f"verb {verb.value} is not defined for {self.id}"
            raise err
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
    def has_db_check(self) -> bool:
        """True when the data defines db-check, the package database check (FR-120 AC-2)."""
        return Verb.DB_CHECK in self.verbs

    @property
    def has_clean(self) -> bool:
        """True when the data defines the clean verb, which empties the package cache (ADR-015)."""
        return bool(self.clean_steps)

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
        if raw.endswith("-") or self.name_pattern.fullmatch(raw) is None:
            err = make("GINN-E-PKG-NAME-INVALID", name=raw)
            rule = (
                f"must match {self.name_pattern.pattern} for {self.id} (DR-003), and must not end"
            )
            err.details["rule"] = f"{rule} in '-' (AS-027)"
            raise err
        return raw


_WARNING_PREFIX: Final = "warning:"


def arch_check_status(
    sync_exit: int,
    query_exit: int,
    query_stdout: str,
    query_stderr: str,
    fatal_warnings: Sequence[re.Pattern[str]],
) -> UpdateStatus:
    """Return what the Arch check-updates steps found, from their results (FR-024, ADR-011).

    The caller runs step 3 (the scratch sync) and step 4 (the query), then passes their exit
    statuses and output here, with `DistroDef.check_fatal_warnings` from the Arch data. Nothing is
    run. The statuses are observed, not documented, so any result that matches no rule below is an
    error, never "no updates":
    - step 3 exits 0, or the result is an error;
    - a stderr line of step 4 matches a fatal-warning pattern: error, whatever step 4 exited with.
      A missing sync database means a repository was not compared, so an empty result proves
      nothing;
    - step 4 exits 0 with a package on stdout: updates. Blank stdout is an error;
    - step 4 exits 1, stdout is blank, and every non-blank stderr line starts with `warning:`:
      no updates. Empty stderr also counts;
    - anything else is an error.
    """
    if sync_exit != 0:
        return UpdateStatus.ERROR
    if _has_fatal_line(fatal_warnings, query_stderr):
        return UpdateStatus.ERROR
    if query_exit == 0:
        return UpdateStatus.UPDATES if query_stdout.strip() else UpdateStatus.ERROR
    if query_exit == 1 and not query_stdout.strip() and _only_warnings(query_stderr):
        return UpdateStatus.NONE
    return UpdateStatus.ERROR


def _only_warnings(stderr: str) -> bool:
    """True when every non-blank line of `stderr` starts with `warning:`."""
    return all(line.startswith(_WARNING_PREFIX) for line in stderr.splitlines() if line.strip())


def _has_fatal_line(patterns: Sequence[re.Pattern[str]], stderr: str) -> bool:
    """True when any pattern matches any line of `stderr`."""
    return any(pattern.search(line) for line in stderr.splitlines() for pattern in patterns)


def db_check_status(
    exit_code: int,
    stdout: str,
    *,
    ok_exit: frozenset[int],
    stdout_inconsistent: bool,
) -> DbStatus:
    """Return what the db-check verb found, from its result (FR-120 AC-2, ADR-011). Nothing is run.

    - An exit status in `ok_exit` is consistent, unless `stdout_inconsistent` is set and stdout has
      text: then it is inconsistent;
    - an exit status in `CANNOT_RUN_EXIT` is unknown: podman did not run the check;
    - any other exit status is inconsistent. A failed check of the database is the signal the
      data names, and the caller marks the realm broken on it.
    """
    if exit_code in ok_exit:
        if stdout_inconsistent and stdout.strip():
            return DbStatus.INCONSISTENT
        return DbStatus.CONSISTENT
    if exit_code in CANNOT_RUN_EXIT:
        return DbStatus.UNKNOWN
    return DbStatus.INCONSISTENT


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
    _only_keys(reader, raw, _TOP_KEYS, "top level", _OPTIONAL_TOP_KEYS)
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
    network_error = reader.regex(raw["network_error_regex"], "network_error_regex")
    missing_package = (
        reader.regex(raw["missing_package_regex"], "missing_package_regex")
        if "missing_package_regex" in raw
        else None
    )
    fatal_warnings = _fatal_warnings(reader, raw.get("check_fatal_warning_regex", []))
    everything = _everything(reader, raw.get("everything", []), name_pattern)
    services_packages, services_bytes = _services(reader, raw, name_pattern)
    export_hook = _export_hook(reader, raw)
    refresh_before = _refresh_before(reader, raw["refresh_before"])
    integration = _integration(reader, raw["integration"], name_pattern)
    verbs, check_steps, clean_steps = _verbs(reader, raw["verbs"])
    count, count_regex = _check_count(reader, raw)
    exit_for_updates = _exit_for_updates(reader, raw, len(check_steps))
    ok_exit, stdout_inconsistent = _db_check(reader, raw, Verb.DB_CHECK in verbs)
    return DistroDef(
        id=distro_id,
        source=source,
        trust_key=trust_key,
        shell=shell,
        name_pattern=name_pattern,
        signature_error=signature_error,
        stale_metadata=stale_metadata,
        network_error=network_error,
        missing_package=missing_package,
        refresh_before=refresh_before,
        integration=integration,
        verbs=MappingProxyType(verbs),
        check_steps=check_steps,
        check_fatal_warnings=fatal_warnings,
        everything=everything,
        check_count=count,
        check_count_regex=count_regex,
        check_exit_for_updates=exit_for_updates,
        db_check_ok_exit=ok_exit,
        db_check_stdout_inconsistent=stdout_inconsistent,
        services_packages=services_packages,
        services_estimated_installed_bytes=services_bytes,
        clean_steps=clean_steps,
        export_hook=export_hook,
        integration_estimated_installed_bytes=_disk_bytes(
            reader, raw, "integration_estimated_installed_bytes"
        ),
        package_cache_peak_bytes=_disk_bytes(reader, raw, "package_cache_peak_bytes"),
        writable_layer_bytes=_disk_bytes(reader, raw, "writable_layer_bytes"),
        base_layer_allocated_bytes=_disk_bytes(reader, raw, "base_layer_allocated_bytes"),
        stage_layer_allocated_bytes=_disk_bytes(reader, raw, "stage_layer_allocated_bytes"),
    )


def _fatal_warnings(reader: "_Reader", value: object) -> tuple[re.Pattern[str], ...]:
    """Compile the optional fatal-warning patterns: a list of regular expressions, maybe empty."""
    field = "check_fatal_warning_regex"
    if not isinstance(value, list):
        raise reader.fail(field, "must be a list of regular expressions")
    return tuple(reader.regex(item, field) for item in value)


def _check_count(
    reader: "_Reader", raw: Mapping[str, object]
) -> tuple[CountRule, re.Pattern[str] | None]:
    """The optional count rule of check-updates (FR-024). The default is `unread`.

    `check_updates_count_regex` is required with `lines-matching` and refused otherwise, so a
    pattern is never read without the rule that uses it.
    """
    field = "check_updates_count"
    value = raw.get(field, CountRule.UNREAD.value)
    rules = {rule.value for rule in CountRule}
    if not isinstance(value, str) or value not in rules:
        raise reader.fail(field, f"must be one of {', '.join(sorted(rules))}")
    rule = CountRule(value)
    pattern_field = "check_updates_count_regex"
    if rule is not CountRule.LINES_MATCHING:
        if pattern_field in raw:
            raise reader.fail(pattern_field, f"is read only with {field} = lines-matching")
        return rule, None
    if pattern_field not in raw:
        raise reader.fail(pattern_field, f"is required with {field} = lines-matching")
    return rule, reader.regex(raw[pattern_field], pattern_field)


def _exit_for_updates(reader: "_Reader", raw: Mapping[str, object], steps: int) -> int | None:
    """The optional exit status of check-updates that means updates exist (dnf5's 100, ADR-011).

    Zero is refused, because it already means no updates. The status is read only by a one-step
    check, so a multi-step check (Arch) refuses it rather than ignore it.
    """
    field = "check_updates_exit_for_updates"
    if field not in raw:
        return None
    value = raw[field]
    if type(value) is not int or not 0 < value <= _MAX_EXIT:
        raise reader.fail(field, f"must be an exit status from 1 to {_MAX_EXIT}")
    if steps != 1:
        raise reader.fail(field, "is read only by a one-step check-updates")
    return value


def _db_check(
    reader: "_Reader", raw: Mapping[str, object], has_verb: bool
) -> tuple[frozenset[int], bool]:
    """The result keys of the db-check verb (FR-120 AC-2), required exactly when the verb is.

    Returns (`db_check_ok_exit`, `db_check_stdout_inconsistent`), empty and False without the verb.
    """
    present = _DB_CHECK_KEYS & set(raw)
    if not has_verb:
        if present:
            raise reader.fail(sorted(present)[0], "is read only with verbs.db-check")
        return frozenset(), False
    missing = sorted(_DB_CHECK_KEYS - present)
    if missing:
        raise reader.fail(missing[0], "is required with verbs.db-check")
    ok_exit = _exit_statuses(reader, raw["db_check_ok_exit"], "db_check_ok_exit")
    if not ok_exit:
        raise reader.fail("db_check_ok_exit", "must name at least one exit status")
    flag = raw["db_check_stdout_inconsistent"]
    if type(flag) is not bool:
        raise reader.fail("db_check_stdout_inconsistent", "must be true or false")
    return ok_exit, flag


def _exit_statuses(reader: "_Reader", value: object, field: str) -> frozenset[int]:
    """A list of exit statuses from 0 to `_MAX_EXIT`, with no repeats."""
    if not isinstance(value, list) or not all(
        type(item) is int and 0 <= item <= _MAX_EXIT for item in value
    ):
        raise reader.fail(field, f"must be a list of exit statuses from 0 to {_MAX_EXIT}")
    if len(set(value)) != len(value):
        raise reader.fail(field, "must not repeat a status")
    return frozenset(value)


def _everything(reader: "_Reader", value: object, name_pattern: re.Pattern[str]) -> tuple[str, ...]:
    """The optional everything metapackages (FR-014): a list of package names, maybe empty."""
    names = reader.strings(value, "everything")
    for name in names:
        if name_pattern.fullmatch(name) is None:
            raise reader.fail("everything", f"{name!r} does not match package_name_regex")
    if len(set(names)) != len(names):
        raise reader.fail("everything", "must not repeat a package")
    return names


def _services(
    reader: "_Reader", raw: Mapping[str, object], name_pattern: re.Pattern[str]
) -> tuple[tuple[str, ...], int]:
    """The optional init set of a services-on realm (ADR-015) and its installed size in bytes.

    `services_packages` is a list of package names, maybe empty, and each name matches
    `package_name_regex`. `services_estimated_installed_bytes` is a non-negative integer, and it
    is 0 exactly when the list is empty. Both default to empty and 0, so a file may omit them.
    """
    packages_field = "services_packages"
    bytes_field = "services_estimated_installed_bytes"
    names = reader.strings(raw.get(packages_field, []), packages_field)
    for name in names:
        if name_pattern.fullmatch(name) is None:
            raise reader.fail(packages_field, f"{name!r} does not match package_name_regex")
    if len(set(names)) != len(names):
        raise reader.fail(packages_field, "must not repeat a package")
    size = raw.get(bytes_field, 0)
    if type(size) is not int or size < 0:
        raise reader.fail(bytes_field, "must be a non-negative integer")
    if not names and size:
        raise reader.fail(bytes_field, "must be 0 when services_packages is empty")
    if names and not size:
        raise reader.fail(bytes_field, "must be positive when services_packages is set")
    return names, size


def _export_hook(reader: "_Reader", raw: Mapping[str, object]) -> ExportHook | None:
    """The optional `export_hook` table (ADR-016), or None when the file omits it.

    `path` is absolute, with no `..` and no whitespace. `text` uses `{stamp}` at least once and no
    placeholder.
    """
    if "export_hook" not in raw:
        return None
    table = reader.table(raw["export_hook"], "export_hook")
    _only_keys(reader, table, _EXPORT_HOOK_KEYS, "export_hook")
    path = reader.text(table["path"], "export_hook.path")
    posix = PurePosixPath(path)
    if not posix.is_absolute() or ".." in posix.parts or any(ch.isspace() for ch in path):
        raise reader.fail("export_hook.path", "must be an absolute path with no '..' or blank")
    text = reader.text(table["text"], "export_hook.text")
    try:
        names = _field_names(text)
    except ValueError as exc:
        raise reader.fail("export_hook.text", f"has a bad brace: {exc}") from None
    if names != frozenset({STAMP_FIELD}):
        rule = f"must use {{{STAMP_FIELD}}} and no other placeholder"
        raise reader.fail("export_hook.text", rule)
    return ExportHook(path=path, text=text)


def _disk_bytes(reader: "_Reader", raw: Mapping[str, object], field: str) -> int:
    """An optional size of the disk formula (FR-121): a non-negative integer, 0 when omitted."""
    size = raw.get(field, 0)
    if type(size) is not int or size < 0:
        raise reader.fail(field, "must be a non-negative integer")
    return size


def _refresh_before(reader: "_Reader", value: object) -> frozenset[Verb]:
    names = reader.strings(value, "refresh_before")
    known = {verb.value for verb in Verb}
    for name in names:
        if name not in known:
            raise reader.fail("refresh_before", f"unknown verb {name!r}")
        if name == Verb.REFRESH.value:
            raise reader.fail("refresh_before", "must not list refresh; it is the metadata load")
        if Verb(name) not in _REFRESH_PRECEDES:
            allowed = ", ".join(sorted(verb.value for verb in _REFRESH_PRECEDES))
            rule = f"{name!r} reads no sync data; only {allowed} may follow a refresh"
            raise reader.fail("refresh_before", rule)
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
) -> tuple[dict[Verb, tuple[str, ...]], tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    table = reader.table(value, "verbs")
    _only_keys(reader, table, _REQUIRED_VERB_KEYS, "verbs", _OPTIONAL_VERB_KEYS)
    allowed = frozenset({"packages"})
    verbs: dict[Verb, tuple[str, ...]] = {}
    for key in table:
        if key in (Verb.CHECK_UPDATES.value, Verb.CLEAN.value):
            continue
        if key == Verb.DB_CHECK.value:
            # db-check names no package, so no placeholder is allowed in it at all.
            verbs[Verb.DB_CHECK] = reader.argv(table[key], f"verbs.{key}", frozenset(), frozenset())
            continue
        argv = reader.argv(table[key], f"verbs.{key}", allowed, allowed)
        if key == Verb.REMOVE_CHECK.value and PACKAGES_ELEMENT not in argv:
            raise reader.fail(f"verbs.{key}", "must include {packages} as one element")
        verbs[Verb(key)] = argv
    check_steps = _step_list(
        reader, table[Verb.CHECK_UPDATES.value], Verb.CHECK_UPDATES, frozenset({TMPDIR_FIELD})
    )
    # clean is optional. Its steps name no placeholder at all (ADR-015).
    clean_steps = (
        _step_list(reader, table[Verb.CLEAN.value], Verb.CLEAN, frozenset())
        if Verb.CLEAN.value in table
        else ()
    )
    return verbs, check_steps, clean_steps


def _step_list(
    reader: "_Reader", value: object, verb: Verb, allowed: frozenset[str]
) -> tuple[tuple[str, ...], ...]:
    """Validate a verb that is a list of steps: a non-empty list of argv lists (ADR-011, ADR-015).

    `allowed` names the placeholders that a step may use. check-updates allows {tmpdir}, and clean
    allows none.
    """
    field = f"verbs.{verb.value}"
    if not isinstance(value, list) or not value:
        raise reader.fail(field, "must be a non-empty list of argv lists")
    steps: list[tuple[str, ...]] = []
    for step in value:
        if not isinstance(step, list):
            raise reader.fail(field, "every step must be an argv list")
        steps.append(reader.argv(step, field, allowed, frozenset()))
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
    "CANNOT_RUN_EXIT",
    "DATA_DIR",
    "DATA_PACKAGE",
    "PACKAGES_ELEMENT",
    "SCHEMA",
    "TEST_SWITCH_ENV",
    "TMPDIR_FIELD",
    "CountRule",
    "DbStatus",
    "DistroDef",
    "HostLocale",
    "Integration",
    "IntegrationSet",
    "UpdateStatus",
    "Verb",
    "arch_check_status",
    "available",
    "clear_cache",
    "db_check_status",
    "distro_fixtures_enabled",
    "from_mapping",
    "host_locale",
    "load",
    "load_file",
    "parse_text",
    "unknown_distribution_error",
]
