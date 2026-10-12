# ADR-011: Package manager semantics for refresh, update checks and removal

- Status: Accepted
- Date: 2026-10-12

## Context

Four requirements decide how a realm's package manager may be driven:

- FR-023: `ginn update <realm>` runs the realm's native full upgrade. Realms are never updated automatically. AC-1: with no update command, no package in the realm changes.
- FR-024: `ginn update --check` reports whether updates exist. The Control Plane runs it when it opens and when the user asks for a refresh, never on a timer.
- FR-040 AC-1: `ginn install` grows the package set by exactly the package and its resolved dependency closure.
- FR-041: `ginn remove` uninstalls the named package.

FR-013 limits the Integration Package Set to CA certificates and locale data. NFR-060 keeps the per-distribution behaviour in data.

Before this ADR, `refresh` was `pacman -Syu` on Arch, and the loader told callers to refresh before install, check-updates, resolve and estimate. On Arch that upgrades the whole realm on an install, or on a Control Plane check. That breaks FR-023 AC-1 and FR-040 AC-1. The `remove` verb relied on each manager's default, which the spec does not state.

This ADR takes the next free number. ADR-007 is reserved for the S-05 spike, and ADR-010 is reserved for the launch path.

Ranked characteristics (SRS section 4), in order: performance efficiency, host integrity and isolation, recoverability, interoperability, usability. Host integrity decides the refresh question: a realm's packages must not change without an update command. Recoverability decides the removal question: removing one package must not silently remove others.

Facts taken from primary sources (documentation only):

- Arch Wiki, System maintenance: partial upgrades are unsupported. `pacman -Sy` followed by `pacman -S package` must not be used. The `checkupdates` script (pacman-contrib) checks for upgrades without running a system update. The Wiki attaches "without synchronizing the database" to its `-d` option only, so the default check's effect on the database is not established here.
- apt-get(8): `--no-remove` aborts "if any packages are to be removed", so it aborts on the named removal too. `--auto-remove` is opt-in. `-s, --simulate` performs a dry run.
- dnf5 remove(8): remove has no option that refuses packages depending on the removed one. `--no-autoremove` disables removal of unused dependencies. `clean_requirements_on_remove` (dnf5.conf(5), default True) removes unused dependencies.
- dnf5 dnf5.conf(5): `metadata_expire` defaults to 48 hours, after which dnf refreshes the metadata itself.
- dnf5 dnf5(8): the global `--refresh` forces a metadata refresh before the command.
- dnf5 makecache(8): it avoids downloading when the metadata has not expired, so it does not force a refresh.
- dnf5 check-upgrade(8): exit 100 when updates exist, 0 when none.

Not verified here, and each is marked in the TOML data for M4 or T5.3:

- Whether a default `checkupdates` run leaves the realm's sync database unchanged, and how it isolates its own copy. FR-024 on Arch depends on this, so M4 must check the database before and after a run.
- The `checkupdates` exit status.
- The output format of `apt-get --simulate remove`.
- The text of every stale-metadata message in `stale_metadata_regex`.

## Decision

Apart from the creation step described below, ginn runs no package manager verb that changes installed packages unless the user asked for that verb. Per distribution, the verb data in `src/ginn/distros/data/` implements this, and `refresh_before` in each file names the verbs the caller runs `refresh` before:

| Distribution | `refresh` | `refresh_before` | `check-updates` | Reads the realm's sync data |
|---|---|---|---|---|
| arch | `pacman -Syu` (same argv as `upgrade`) | none | `checkupdates` (no sync or upgrade flag; its effect on the realm's database is verified in M4) | install, resolve and estimate use the database as the last full upgrade left it |
| debian, kali | `apt-get update` (lists only) | install, upgrade, check-updates, resolve, estimate | `apt list --upgradable` | the lists as last refreshed |
| fedora | `dnf --refresh makecache` (metadata only) | none | `dnf --refresh check-upgrade` (forced metadata refresh) | dnf5 refreshes metadata that has expired (48 hours) |

Rules that follow from the table:

1. Arch never refreshes the sync database outside a full upgrade. The `upgrade` verb is `pacman -Syu`, and the update command runs it alone. No Arch argv carries `--refresh` without `--sysupgrade`. This is the Arch Wiki rule.
2. Install, resolve and estimate never upgrade the realm. Their argv carries no upgrade token (tested). A manager may still upgrade an installed dependency that a new package requires; that is part of the dependency closure FR-040 names.
3. Check-updates must not change the realm's package database. Arch runs `checkupdates` with no sync or upgrade flag; M4 confirms the database is unchanged by a run. Debian and Kali refresh only the lists. Fedora forces a metadata refresh only.
4. Stale metadata. `stale_metadata_regex` matches the manager's output when the local metadata no longer matches the mirror. The ops layer maps a match to a remedy, which is not in this data. On Arch the remedy is `ginn update <realm>`, the one full upgrade, which moves the sync database forward. On Debian, Kali and Fedora the remedy is `refresh` and a retry. A failed Arch install after the database has aged is expected. It is not a ginn bug, and install does not fix it by refreshing.

Realm creation runs `refresh` once before its first install on every distribution, because the official images ship no package lists (`arch.toml`, `debian.toml`, `fedora.toml`). This is the caller's rule, carried out by the ops layer (M4), not by the loader. The consequence for Arch is recorded below as an open decision.

Removal. `remove` never removes a package the user did not name. The only exception is an unneeded dependency that the native manager removes by itself, which dnf5 does by default. A package that needs a named package is never removed. Each manager's mechanism:

| Distribution | Native `remove` argv | Packages that need the named one | Unneeded dependencies | Refusal before the real run (T5.3) |
|---|---|---|---|---|
| arch | `pacman --remove --noconfirm` (no `--recursive`, no `--cascade`) | refused by pacman, natively | left in place | none needed |
| debian, kali | `apt-get --yes remove` | removed by apt-get | left in place (no `--auto-remove`) | run `apt-get --simulate remove <names>`; refuse if it lists any name not requested |
| fedora | `dnf --assumeyes remove` | removed by dnf5 | removed by dnf5 (`clean_requirements_on_remove`) | run `dnf --assumeno --no-autoremove remove <names>`; refuse if it lists any name not requested |

The pre-check for dnf adds `--no-autoremove`, so its list holds only the named packages and their dependents. The real run keeps dnf's default cleaning, which the rule permits. The pre-check is a dry run, so a refusal changes nothing. `--no-remove` is not used: it aborts on the named removal too (apt-get(8)). No `Verb` member is added for the pre-check: every distribution file and several tests iterate the closed `Verb` set, and T5.3 owns the pre-check.

Data and code. The distribution definitions carry `stale_metadata_regex` and `refresh_before` as required keys. The loader refuses a file that lacks them, lists `refresh` in `refresh_before`, or repeats a verb. `SCHEMA` stays 1: the data ships with the code, so the keys change together, and an older file fails loudly with "missing key(s)". The loader caches a successful load per data directory, and `clear_cache()` empties the cache. The test switch `GINN_TEST_DISTROS=1` is the only way to load a fixture (a name that starts with `_`).

## Alternatives considered

- Arch `refresh` as `pacman -Sy`. Rejected: the Arch Wiki forbids a sync without an upgrade, because the next package install then mixes new libraries with old ones.
- Arch check-updates as `pacman -Qu` after a sync inside the realm. Rejected: it changes the realm's database, and it breaks rule 3. Plain `pacman -Qu` against the database as last synced is read-only but can report no updates while updates exist, so it fails FR-024.
- Arch check-updates implemented in ginn by fetching the database into a temporary directory. Rejected: `checkupdates` already offers the check, and a second implementation would need its own tests and its own fetch of the database.
- apt-get remove with `--no-remove`. Rejected: it aborts on the named removal (apt-get(8)).
- apt-get remove with `--auto-remove` and a refusal of other names. Rejected: `--auto-remove` removes unneeded dependencies, which the rule does not need, and the default leaves them in place.
- dnf remove with `--no-autoremove` in the real run. Rejected: dnf5 remove(8) has no flag that refuses dependents, so the flag would not give the rule. It only changes the cleaning, and the rule permits the default.
- Fedora check-updates after `dnf makecache`. Rejected: makecache does not download when the metadata has not expired (dnf5 makecache(8)), so it does not force a fresh view. The global `--refresh` does.
- Fedora refresh before install. Rejected: dnf5 refreshes expired metadata itself (dnf5.conf(5)). An extra refresh adds network use and changes nothing.
- A `Verb` member for the removal pre-check. Rejected for now: it would add a required argv to every distribution file and every test that iterates the verbs, before T5.3 decides the exact form.
- Bumping `SCHEMA` for the new keys. Rejected: see the Decision.

## Consequences

Positive. FR-023 AC-1 and FR-040 AC-1 hold at the argv level, and a test proves them. The Arch check passes no sync or upgrade flag; M4 confirms that it leaves the database alone. The removal rule is stated, and each manager's mechanism is in one table. A stale-metadata failure has one place to be recognised.

Negative, and what the code must and must not do:

- Open decision, to be settled before M4 create. A fresh Arch image ships no sync database, and the only supported way to obtain one is a full upgrade, so the first install on a new Arch realm cannot succeed without it. A create-time full upgrade changes packages the user did not request. That conflicts with CR-007 (realm contents are the image, the Integration Package Set, and the requested packages with their declared dependencies) and with the intent of FR-023 AC-1. The options are: (a) accept one full upgrade at creation and amend CR-007 to say so; (b) create from a dated Arch Linux Archive snapshot that matches the image, which needs a spike; (c) a new Arch realm installs nothing until the user runs `ginn update <realm>`. This ADR does not choose.
- Deviation from FR-013. The Arch Integration Package Set gains `pacman-contrib`, because `checkupdates` comes from it. FR-013 says the set is limited to CA certificates and locale data. The alternative, parsing the database without `checkupdates`, would reimplement it. FR-013 must be amended, or this deviation accepted, before M4.
- Until T5.3 implements the pre-checks, the ops layer must not run `remove` on Debian, Kali or Fedora.
- Unneeded dependencies stay after `remove` on Arch and Debian, and dnf5 removes them. The difference is the manager's own default and is not changed here.
- The Fedora check forces a metadata download each time the Control Plane opens. FR-024 runs the check on open only, so the cost is one download per open.
- Every value in `stale_metadata_regex` is runtime text. It is checked against sample lines, and it must be verified in M4 on a real host.
- `checkupdates`'s exit status is not relied on. M4 must verify it before the check treats a non-zero status as "no updates".

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-013 | The Arch Integration Package Set gains `pacman-contrib`. This is a recorded deviation, and the FR text needs amending or accepting. |
| FR-023 | Only the `upgrade` verb changes installed packages on each distribution. Refresh, install, resolve, estimate and check-updates never do. The proving test for the update command is M8. |
| FR-024 | Check-updates changes nothing on any distribution by its argv: no sync or upgrade flag on Arch (M4 confirms the database is unchanged), lists only on Debian and Kali, forced metadata only on Fedora. |
| FR-040 | Install never upgrades installed packages, and Arch install uses the sync database as the last full upgrade left it. A stale Arch database fails install with a 404; the remedy is `ginn update`. |
| FR-041 | Remove names only the requested packages. Arch refuses dependents natively. Debian, Kali and Fedora are pre-checked by ops (T5.3). |
| NFR-060 | Refresh rules, stale-metadata patterns and removal mechanisms are data per distribution. A new distribution needs a file, not code. |
| CR-007 | Open decision: a fresh Arch realm needs one full upgrade at creation to obtain a sync database, which changes packages beyond the image and the requested set. |
