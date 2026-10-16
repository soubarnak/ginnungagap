# ADR-011: Package manager semantics for refresh, update checks and removal

- Status: Accepted
- Date: 2026-10-12
- Amended: 2026-10-12 (D6). Adds the locale rule: every package-manager subprocess whose output ginn parses runs with `LC_ALL=C.UTF-8`. Corrects the consequence about the no-sync-database warning, which the fatal-warning patterns now handle. The decision stands.
- Amended: 2026-10-14. Adds a `db-check` verb to the distribution data (FR-120 AC-2), and moves the dnf exit status for updates and the check-updates count rule into data. See Amendment 2026-10-14.

## Context

Four requirements decide how a realm's package manager may be driven:

- FR-023: `ginn update <realm>` runs the realm's native full upgrade. Realms are never updated automatically. AC-1: with no update command, no package in the realm changes.
- FR-024: `ginn update --check` reports whether updates exist. The Control Plane runs it when it opens and when the user asks for a refresh, never on a timer.
- FR-040 AC-1: `ginn install` grows the package set by exactly the package and its resolved dependency closure.
- FR-041: `ginn remove` uninstalls the named package.

FR-013 limits the Integration Package Set to CA certificates and locale data. NFR-060 keeps the per-distribution behaviour in data.

Before this ADR, `refresh` was `pacman -Syu` on Arch, and the loader told callers to refresh before install, check-updates, resolve and estimate. On Arch that upgrades the whole realm on an install, or on a Control Plane check. That breaks FR-023 AC-1 and FR-040 AC-1. The `remove` verb relied on each manager's default, which the spec does not state.

This ADR takes the next free number. ADR-007 is the idle memory budget (S-05), and ADR-010 is reserved for the launch path.

Ranked characteristics (SRS section 4), in order: performance efficiency, host integrity and isolation, recoverability, interoperability, usability. Host integrity decides the refresh question: a realm's packages must not change without an update command. Recoverability decides the removal question: removing one package must not silently remove others.

Facts taken from primary sources (documentation only):

- Arch Wiki, System maintenance: partial upgrades are unsupported. `pacman -Sy` followed by `pacman -S package` must not be used. The Wiki also describes `checkupdates` (pacman-contrib), which checks for upgrades without a system update. This ADR does not use it (see Alternatives).
- pacman(8), version 7.1.0, the local man page: `--dbpath` sets the database directory to an absolute path, and the page advises against it unless the user knows what they are doing. `--logfile` takes an absolute path. `--refresh` downloads the repository databases, and the page says it should normally go with `--sysupgrade` or `--upgrades`. `--upgrades` with `--query` lists installed packages that are out of date, and works best after a sync. `--needed` skips targets that are already up to date. `--cachedir` sets the package cache, separately from the database. The page documents no exit statuses.
- apt-get(8), Debian trixie manual page, fetched from the public manual: `upgrade` leaves a package at its installed version when its new version needs a dependency that is not installed. `dist-upgrade` installs such dependencies, and it may remove a package to resolve dependencies. `--no-install-recommends` is a global option. `--no-remove` aborts when any package would be removed, and `-s` makes a dry run.
- dnf5 remove(8): remove has no option that refuses packages depending on the removed one. `--no-autoremove` disables removal of unused dependencies. `clean_requirements_on_remove` (dnf5.conf(5), default True) removes unused dependencies.
- dnf5 dnf5.conf(5): `metadata_expire` defaults to 48 hours, after which dnf refreshes the metadata itself.
- dnf5 dnf5(8): the global `--refresh` forces a metadata refresh before the command.
- dnf5 makecache(8): it avoids downloading when the metadata has not expired, so it does not force a refresh.
- dnf5 check-upgrade(8): exit 100 when updates exist, 0 when none.

Observed, not documented. A probe on a Void Linux host, with pacman 7.1.0 run as root inside a user namespace, used a scratch database directory, a `local` link to a scratch installed-package database, and a hand-built `file://` sync database. It is not a realm and not Arch:

- With a newer package in the sync database, the sync step exits 0 and the query exits 0 with one line, `<name> <old> -> <new>`, on stdout.
- With nothing newer, the query exits 1, with nothing on stdout or stderr.
- With no sync database in the scratch directory, the query exits 1 and prints a warning on stderr.
- As an ordinary user, the sync step refuses with "you cannot perform this operation unless you are root". The check must therefore run as the realm's root.
- The sync step created `sync/` in the scratch directory, which had none before. With `--cachedir` set to a scratch path, nothing was written there. `/var/lib/pacman` on the host was unchanged after the probes.

Not verified here, and each is marked in the data for M4 or T5.3:

- The check's exit statuses and its effect on the realm's package cache and log, in a realm (M4).
- Whether a fresh Arch image has a sync database, and what its package lists are. S-10 does not record either (M4).
- The output format of `apt-get --simulate remove`.
- The text of every stale-metadata message in `stale_metadata_regex`.
- The dnf5 upgrade behaviour in the Fedora row below. The dnf5 manual was not available locally, and fetching it failed.
- The locale step on Arch: S-10b ran `locale-gen`, and this step runs `localedef` directly.

## Decision

Apart from the creation step described below, ginn runs no package manager verb that changes installed packages unless the user asked for that verb. Per distribution, the verb data in `src/ginn/distros/data/` implements this, and `refresh_before` in each file names the verbs the caller runs `refresh` before:

| Distribution | `refresh` | `refresh_before` | `check-updates` | Reads the realm's sync data |
|---|---|---|---|---|
| arch | `pacman -Syu` (same argv as `upgrade`) | none | four steps below, against a scratch database; the realm's sync database is never named | install, resolve and estimate use the database as the last full upgrade left it |
| debian, kali | `apt-get update` (lists only) | install, upgrade, check-updates, resolve, estimate | `apt list --upgradable` | the lists as last refreshed |
| fedora | `dnf --refresh makecache` (metadata only) | none | `dnf --refresh check-upgrade` (forced metadata refresh) | dnf5 refreshes metadata that has expired (48 hours) |

The `upgrade` verb is the native full upgrade that FR-023 AC-2 names. Its argv, per distribution:

| Distribution | `upgrade` |
|---|---|
| arch | `pacman --sync --refresh --sysupgrade --noconfirm` |
| debian, kali | `apt-get --yes --no-install-recommends dist-upgrade` |
| fedora | `dnf --refresh --assumeyes --setopt=install_weak_deps=False upgrade` (no package named, so every installed package with a newer version; `--refresh` forces current metadata, so the upgrade does not work from metadata up to 48 hours old) |

`dist-upgrade`, not `upgrade`: apt's plain upgrade leaves a package at its installed version when the new version needs a dependency that is not installed, so it is not a full upgrade (apt-get(8)).

The `db-check` verb is the database check that FR-120 AC-2 uses after a failed update. It changes nothing, and a distribution without it is never marked broken. Per distribution:

| Distribution | `db-check` | Inconsistent when |
|---|---|---|
| arch | `pacman --database --check` | the exit status is not 0 (stdout is not read) |
| debian, kali | `dpkg --audit` | the exit status is not 0, or stdout is not blank (dpkg lists each broken package on stdout) |
| fedora | `rpm --verifydb` | the exit status is not 0 (stdout is not read) |

A status of 125, 126 or 127 means podman did not run the check (`CANNOT_RUN_EXIT` in loader.py). The result is unknown, not inconsistent, so the realm keeps its prior state and the error is noted. The statuses and stdout rules are not verified until M4.

The Arch check-updates is a list of four steps. The loader runs none of them. The caller runs them in order, and stops at the first failure:

1. `mkdir <tmpdir>`, without `-p`. The caller chooses a path whose parent exists and whose last element does not. A leftover path fails this step, so the check stops and never writes into an old directory.
2. `ln -s -T /var/lib/pacman/local <tmpdir>/local`. The installed-package database is read through a link. `-T` makes `<tmpdir>/local` the link itself, so `ln` never links inside a directory it finds there, and it fails when the name exists.
3. `pacman --sync --refresh --dbpath <tmpdir> --logfile /dev/null`. This syncs a scratch copy of the repository databases. It has `--refresh` without `--sysupgrade`, which is allowed only here, because it refreshes the scratch copy and changes no installed package.
4. `pacman --query --upgrades --dbpath <tmpdir>`. This lists the installed packages that the scratch copy shows as out of date.

The caller removes `<tmpdir>` after the last step, whether or not a step failed. Removal must delete the `local` link and must not follow it.

Exit statuses (observed, see Context): step 3 exits 0 on success. Step 4 exits 0 with the upgradable packages on stdout. It exits 1 with blank stdout when nothing is upgradable, and its stderr is empty or holds only lines that start with `warning:`. It exits 1 with another message on stderr when it cannot run. A missing sync database is expected to print a `warning:` line, `database file for <repo> does not exist`, which by its prefix alone looks like the nothing-to-upgrade case. That wording is not yet observed on a host (M4), so the fail-closed rule below matches it by pattern. The caller applies `arch_check_status()` in `loader.py`, which returns one of three results:

- updates: step 4 exits 0 with a package on stdout. Exit 0 with blank stdout is an error.
- no updates: step 3 exits 0, step 4 exits 1, stdout is blank, every non-blank stderr line starts with `warning:`, and no stderr line matches `check_fatal_warning_regex`.
- error: any other result, including a failed step 3, a stderr line that matches `check_fatal_warning_regex` whatever step 4 exited with, and exit 1 with any other stderr line.

The rule fails closed. A `warning:` line is not proof of no updates when it says that a repository was not read. The patterns for such lines are data: `check_fatal_warning_regex` in `arch.toml` holds `database file for .* does not exist`. The loader reads the list, and `arch_check_status()` takes it as an argument, so no pattern is written in Python. A new pacman warning that means a repository was skipped goes in that list, with a test.

The loader records the exit statuses in the TOML comments, not as data, because they are observed and not documented. The status rule is code, and `arch_check_status()` is the one place it is written. The fatal-warning patterns are data, because they are text that the manager prints and that a new manager version may change. M4 confirms the statuses and the patterns in a realm before the Control Plane relies on them.

Locale. Every package-manager subprocess whose output ginn parses runs with `LC_ALL=C.UTF-8` in its environment. pacman translates its `warning:` prefix under a non-C locale. A translated warning no longer starts with `warning:`, so the no-updates rule above does not match it, and an up-to-date realm would be reported as an error. T5.1 implements the setting in `packages/manager.py`. That the locale exists in each realm image is untested, and M4 confirms it.

Rules that follow from the table:

1. Arch never refreshes the sync database outside a full upgrade. The `upgrade` verb is `pacman -Syu`, and the update command runs it alone. No Arch argv other than the check-updates step 3 carries `--refresh` without `--sysupgrade`, and no check-updates step names the realm's sync database (tested). This is the Arch Wiki rule.
2. Install, resolve and estimate never upgrade the realm. Their argv carries no upgrade token (tested). A manager may still upgrade an installed dependency that a new package requires; that is part of the dependency closure FR-040 names.
3. Check-updates must not change the realm's package database. On Arch the check's writes are under `<tmpdir>`: the scratch repository databases and the scratch database lock. The realm's installed-package database is read through the link and never written. Two paths outside the scratch copy are reachable, and M4 must confirm that neither changes across a run: the default package cache (`/var/cache/pacman/pkg`, because the check sets no `--cachedir`) and the realm's pacman log (`--logfile /dev/null` keeps it out). `--dbpath` moves the database directory and nothing else (pacman(8)). Debian and Kali refresh only the lists. Fedora forces a metadata refresh only.
4. Stale metadata. `stale_metadata_regex` matches the manager's output when the local metadata no longer matches the mirror. The ops layer maps a match to a remedy, which is not in this data. On Arch the remedy is `ginn update <realm>`, the one full upgrade, which moves the sync database forward. On Debian, Kali and Fedora the remedy is `refresh` and a retry. A failed Arch install after the database has aged is expected. It is not a ginn bug, and install does not fix it by refreshing.

Realm creation runs `refresh` once, then installs the Integration Package Set, on every distribution. This is the caller's rule, carried out by the ops layer (M4), not by the loader. The reason is that a new realm's package lists must match the mirror before the install resolves anything. On Arch the refresh is the full upgrade (rule 1), so the image's packages move to current versions at creation. This was decided, and CL-9 asks SRS v1.1 to say so.

Why this stays within the SRS as written, pending CL-9:

- FR-013 AC-1 diffs the package list against the image. A full upgrade changes versions, not the list. On Arch the Integration Package Set is `ca-certificates`, which the image already ships, so it adds no package to the realm.
- FR-023 AC-1 covers the realm's life after creation. Creation is not an update command, and it runs before the realm is handed to the user.
- CR-007 names the package sources: the official image, the Integration Package Set and the requested packages. The full upgrade draws the same packages from the same repositories, at newer versions.

The SRS does not say whether the versions at creation are the image's or the mirror's. That is why CL-9 is open. This reading is a choice, not a finding, and the SRS author may decide differently.

Removal. `remove` never removes a package the user did not name. The only exception is an unneeded dependency that the native manager removes by itself, which dnf5 does by default. A package that needs a named package is never removed. Each manager's mechanism:

| Distribution | Native `remove` argv | Packages that need the named one | Unneeded dependencies | Refusal before the real run (T5.3) |
|---|---|---|---|---|
| arch | `pacman --remove --noconfirm` (no `--recursive`, no `--cascade`) | refused by pacman, natively | left in place | none needed; the data omits `remove-check` |
| debian, kali | `apt-get --yes remove` | removed by apt-get | left in place (no `--auto-remove`) | run `remove-check` (`apt-get --simulate remove <names>`); refuse if it lists any name not requested |
| fedora | `dnf --assumeyes remove` | removed by dnf5 | removed by dnf5 (`clean_requirements_on_remove`) | run `remove-check` (`dnf --assumeno --no-autoremove remove <names>`); refuse if it lists any name not requested |

The pre-check for dnf adds `--no-autoremove`, so its list holds only the named packages and their dependents. The real run keeps dnf's default cleaning, which the rule permits. The pre-check is a dry run, so a refusal changes nothing. `--no-remove` is not used: it aborts on the named removal too (apt-get(8)). `remove-check` is a `Verb` member that the data may omit. Arch and the test fixture omit it, and Debian, Kali and Fedora define it. When the data defines it, the loader requires `{packages}` as one element. `db-check` is optional too (Amendment 2026-10-14). Every other verb stays required, so a new distribution still defines each of the others. T5.3 owns the ops-side use of the dry run and the parsing of its output.

The `upgrade` verb is not `remove`, and FR-041 does not cover it. A Debian or Kali full upgrade can remove a package to resolve a dependency (apt-get(8)). That is the native full upgrade FR-023 AC-2 names. The Control Plane should show such removals before the update runs. That is an ops-layer item for M8, not decided here.

Data and code. The distribution definitions carry `stale_metadata_regex` and `refresh_before` as required keys. The check-updates steps are a required key too, a list of argv lists. `verbs` holds every other verb, and `check_updates(tmpdir)` returns the steps with `{tmpdir}` filled in. The loader refuses a file that lacks a required key, lists `refresh` in `refresh_before`, repeats a verb, or puts `{tmpdir}` in any verb other than check-updates. It also refuses, with GINN-E-DISTRO-DATA, any `refresh_before` verb other than install, upgrade, check-updates, resolve and estimate, which are the verbs that read the realm's sync data. `remove-check` and `db-check` are the optional verb keys, and `DistroDef.has_remove_check` and `DistroDef.has_db_check` report whether the data defines them. The creation rule is not in the data: it is the same for every distribution, so the docstring and this ADR carry it. `SCHEMA` stays 1: the data ships with the code, so the keys change together, and an older file fails loudly with "missing key(s)". The loader caches a successful load per data directory, and `clear_cache()` empties the cache. The test switch `GINN_TEST_DISTROS=1` is the only way to load a fixture (a name that starts with `_`).

## Alternatives considered

- Arch `refresh` as `pacman -Sy`. Rejected: the Arch Wiki forbids a sync without an upgrade, because the next package install then mixes new libraries with old ones.
- Arch check-updates as `pacman -Qu` after a sync inside the realm. Rejected: it changes the realm's database, and it breaks rule 3.
- Arch check-updates as plain `pacman -Qu` against the database as last synced. Rejected: it can report no updates while updates exist, so it fails FR-024.
- Arch check-updates through `checkupdates` (pacman-contrib). Rejected: it needs a package in the Arch Integration Package Set, and FR-013 limits that set to CA certificates and locale data. Its effect on the realm's database would also need its own verification.
- Arch check-updates implemented in ginn by parsing or fetching the database itself. Rejected: it would be a second implementation of pacman's version comparison. The chosen check runs pacman itself against a scratch database path, and ginn only lays out the paths.
- Arch creation from a dated Arch Linux Archive snapshot that matches the image (CL-9 option b). Rejected for now: it needs a spike, and nothing shows that a snapshot date matches the image's package versions.
- Arch creation that installs nothing until the user runs `ginn update <realm>` (CL-9 option c). Rejected: a new realm would lack its Integration Package Set and its requested packages until a later update.
- Debian and Kali full upgrade with `apt-get upgrade`. Rejected: it leaves packages behind when a new version needs a new dependency, so it is not the native full upgrade (FR-023 AC-2).
- apt-get remove with `--no-remove`. Rejected: it aborts on the named removal (apt-get(8)).
- apt-get remove with `--auto-remove` and a refusal of other names. Rejected: `--auto-remove` removes unneeded dependencies, which the rule does not need, and the default leaves them in place.
- dnf remove with `--no-autoremove` in the real run. Rejected: dnf5 remove(8) has no flag that refuses dependents, so the flag would not give the rule. It only changes the cleaning, and the rule permits the default.
- Fedora check-updates after `dnf makecache`. Rejected: makecache does not download when the metadata has not expired (dnf5 makecache(8)), so it does not force a fresh view. The global `--refresh` does.
- Fedora refresh before install. Rejected: dnf5 refreshes expired metadata itself (dnf5.conf(5)). An extra refresh adds network use and changes nothing.
- A required `Verb` member for the removal pre-check. Rejected: it would add a required argv to every distribution file and every test that iterates the verbs. Revised: `remove-check` is a `Verb` member that the data may omit, so Arch and the fixture do not change (see the Decision).
- A schema key for exit statuses. Rejected: the statuses are observed on one host, and a field would encode values that are not verified. The rule is one pure function, `arch_check_status()`, not a key. M4 confirms it before the Control Plane uses it. Partly reversed on 2026-10-14 for the documented and per-distribution statuses; see Amendment 2026-10-14.
- Bumping `SCHEMA` for the new keys. Rejected: see the Decision.

## Consequences

Positive. FR-023 AC-1 and FR-040 AC-1 hold at the argv level, and tests prove them. The Arch check is four pacman calls, not a ginn implementation, and it adds no package to a realm. The Arch Integration Package Set is now `ca-certificates`, which the image already ships, so the FR-013 deviation recorded by the earlier version of this ADR no longer exists. The removal rule is stated, and each manager's mechanism is in one table.

Negative, and what the code must and must not do:

- Creation brings the Arch image's packages to current versions. This is accepted under CL-9, and the reading is recorded above. If the SRS author decides against it, option (b) or (c) from Alternatives returns.
- Until T5.3 implements the pre-checks, the ops layer must not run `remove` on Debian, Kali or Fedora.
- Unneeded dependencies stay after `remove` on Arch and Debian, and dnf5 removes them. The difference is the manager's own default and is not changed here.
- A Debian or Kali full upgrade can remove packages (see Decision).
- The Fedora check forces a metadata download each time the Control Plane opens. FR-024 runs the check on open only, so the cost is one download per open.
- Every value in `stale_metadata_regex` is runtime text. It is checked against sample lines, and it must be verified in M4 on a real host.
- The check's exit statuses are observed, not documented. The Control Plane must use `arch_check_status()`, which maps exit 1 to "no updates" only when stdout is blank, every non-blank stderr line starts with `warning:`, no stderr line matches a fatal-warning pattern, and step 3 succeeded.
- pacman prefixes its warnings with `warning:`, so the no-sync-database case in Context matches the warning-only prefix. The fatal-warning pattern `database file for .* does not exist` in `check_fatal_warning_regex` sends that case to the error result. It does so whether or not step 3 ran, so the rule no longer depends on step 3 for this case. M4 confirms the pattern's wording in a realm, and checks that no other warning pacman prints says a repository was skipped.
- Code must run every parsed package-manager call with `LC_ALL=C.UTF-8` (the Locale rule in the Decision). It must not match on translated text.
- Whether ginn's realm lock covers the check is open. The check takes no lock on the realm's pacman database, because the scratch `--dbpath` has its own lock. So a check can run while `ginn update` runs, and it can read a `local` database that the update is writing. The ops layer does not exist yet, so no caller takes the realm lock for the check. This must be decided before the Control Plane runs the check.
- The cleanup of `<tmpdir>` must delete the `local` link, not follow it. Following it would delete the realm's installed-package database.
- The loader cannot check that `<tmpdir>` does not exist yet, or that it is inside the realm. The caller must make both true.

M4 must confirm, in a realm, before the Control Plane relies on the check:

- `/var/lib/pacman/sync`, `/var/lib/pacman/local`, `/var/log/pacman.log` and `/var/cache/pacman/pkg` are unchanged across a check run, by mtime and content.
- The exit statuses of the four steps, and that `sync/` is created by step 3.
- Whether a fresh Arch image has a sync database and what its package lists are.
- The locale step on Arch, which runs `localedef` and not the `locale-gen` that S-10b ran.
- That `C.UTF-8` exists in each realm image, so the Locale rule holds there.
- (a) Whether the scratch `--sync --refresh --dbpath` writes the GnuPG lock or the trust database when database signatures are checked. The GnuPG home is `GPGDir` in pacman.conf, which `--dbpath` does not move, so a write there would land in the realm outside `{tmpdir}`.
- (b) Whether pacman 7's `DownloadUser` sandbox can write into the root-created `{tmpdir}`. If it cannot, step 3 fails, and the check reports an error. Test with the default setting and with `DownloadUser` set.
- (c) The scratch database lock means a check does not wait for a running `ginn update`. Confirm that the check neither blocks nor reads a half-written `local` database, and decide whether ginn's realm lock covers the check (see Consequences).

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-013 | The Arch Integration Package Set is `ca-certificates`, which the image already ships, so the set adds no package. The creation rule is recorded under CL-9. |
| FR-023 | Only the `upgrade` verb changes installed packages on each distribution, and it is the native full upgrade: `pacman -Syu`, `apt-get dist-upgrade`, `dnf --refresh upgrade`. Refresh, install, resolve, estimate and check-updates never do. Unit tests pin each argv. The proving test for the update command is M8. |
| FR-024 | Check-updates changes nothing in the realm's package database on any distribution. Arch runs a scratch-database check, and `arch_check_status()` maps its results (M4 confirms the realm's files are unchanged). Debian and Kali read the lists only. Fedora forces a metadata refresh only. The parsed output is read under `LC_ALL=C.UTF-8`, so the `warning:` prefix is in English (Locale rule). |
| FR-040 | Install never upgrades installed packages, and Arch install uses the sync database as the last full upgrade left it. A stale Arch database fails install with a 404; the remedy is `ginn update`. |
| FR-041 | Remove names only the requested packages. Arch refuses dependents natively. Debian, Kali and Fedora define `remove-check`, the dry run that ops reads before remove (T5.3). |
| FR-120 | `broken` is set only when the `db-check` verb reports an inconsistent database (AC-2). The verb and its exit and stdout rules are data, and a check that cannot run leaves the realm as it was (Amendment 2026-10-14). |
| NFR-060 | Refresh rules, check-updates steps, stale-metadata patterns, removal mechanisms, the optional `remove-check` and `db-check` verbs, and the result rules of both checks are data per distribution. A new distribution needs a file, not code. |
| CR-007 | The creation rule keeps realm contents to the official image's packages, the Integration Package Set and the requested packages. The full upgrade at creation changes their versions, not their sources. Wording is open under CL-9. |

## Amendment 2026-10-14

The rejection of "A schema key for exit statuses" in Alternatives is partly reversed. FR-120 AC-2 marks a realm `broken` only on an inconsistent package database. That check is per distribution, and NFR-060 keeps per-distribution facts in data. Three kinds of value are now keys:

- dnf's documented exit status for "updates available", 100 (dnf5 check-upgrade(8)), is `check_updates_exit_for_updates`. Only fedora sets it. The fedora count is left unread until M4.
- The check-updates count rule is `check_updates_count`. It is `nonblank-lines` on arch, and `lines-matching` with `check_updates_count_regex` on debian and kali. `ginn.packages.manager` reads both keys.
- The `db-check` verb carries `db_check_ok_exit` and `db_check_stdout_inconsistent`. The Decision table above gives its rule for each distribution. `ops/update.py` binds the check to the verb by default.

The rejection still holds for the Arch check-updates statuses. Those were observed on one host and are not documented, so they stay in `arch_check_status()` and in TOML comments, not in keys. The db-check statuses are also unverified until M4.

The `db-check` verb is optional, like `remove-check`. `DistroDef.has_db_check` reports whether the data defines it. `SCHEMA` stays 1, because the data ships with the code.

Requirements: FR-120 AC-2 (`broken` only on an inconsistent database), FR-023 (the update command runs the db-check after a failed upgrade), FR-024 (the check-updates count and exit rules are data), NFR-060 (per-distribution facts in data).
