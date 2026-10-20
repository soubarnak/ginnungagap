# W4: M8 rebuild host gate (test_rebuild_real.py)

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

Command: `GINN_INTEGRATION=1 python -m pytest tests/integration/test_rebuild_real.py -v -s -rxXs`

Result: 3 passed, 9 errors (setup) in 168.32 s.

| Item | Verdict | Cause |
|---|---|---|
| test_create_killed_is_broken_then_resumed[homed] and [created] (FR-122) | PASS | |
| test_rebuild_arch_replays_repo_and_aur_packages (FR-021) | PASS | |
| rb fixture items (6): rebuild keeps home/manifest/settings, reports missing package, cancelled rebuild, killed rebuild [replayed] and [swapping], update --check | ERROR, NOT RUN | fixture runs `ginn install xterm` and hits the fonts bug (see W4-M5-export-real.md): dpkg "Read-only file system" on /usr/share/fonts/truetype |
| svc fixture items (3): services-on rebuild, cancel, killed rebuild | ERROR, NOT RUN | harness/product mismatch: fixture runs `create arch --services on --with htop`, the product refuses it with GINN-E-NOT-SUPPORTED-YET (ADR-015 refusal, "--with packages on a services-on create"). The fixture also uses the real store, which is HOST-UNACKED (W4-M7). |

Leftovers: `ginn list` after the run: no realms.

## Rerun at 79a3448 (2026-10-20, after 869da90 and 79a3448)

Command: same. Result: 7 passed, 2 failed, 3 errors in 352.34 s. A realm-wide `ginn list` after the run: no realms.

| Item | Verdict | Cause |
|---|---|---|
| test_rebuild_reports_a_package_the_upstream_lacks, test_killed_rebuild_is_settled_by_the_next_command[replayed] and [swapping], test_update_check_changes_nothing_and_update_is_logged, test_create_killed_is_broken_then_resumed[homed] and [created], test_rebuild_arch_replays_repo_and_aur_packages | PASS | |
| test_rebuild_keeps_home_manifest_and_settings | FAIL (HARNESS) | Rebuild itself passed every assertion up to the last: `the exported entry was not regenerated`. Path checked: `ginn-w1f-rb-xterm.desktop`; Debian's entry is `ginn-w1f-rb-debian-xterm.desktop` (same DESKTOP_APP naming bug as the export gate). |
| test_rebuild_cancelled_before_the_swap_keeps_the_old_realm | FAIL in the full run, PASS when rerun alone (43.08 s) | Cause was my run method: the pytest process was started with a trailing `&`, so SIGINT was inherited as ignored and the seam's SIGINT never raised KeyboardInterrupt (rebuild rc 0, generation 4). Rerun with SIGINT reset to default: PASS. Not a product bug. |
| test_services_on_rebuild_replays_under_runit, test_services_on_cancelled_rebuild_rolls_back, test_services_on_killed_rebuild_is_settled | ERROR at setup, NOT RUN | `GINN-E-HOST-UNACKED` (rc 4) on the services-on fixture's create: it uses the user's real Owned Paths, where `integrity/pending.json` is pending. Needs `ginn integrity --ack` by the user. The 79a3448 fixture change is therefore untested. |

## Rerun 2 at 79a3448 plus uncommitted harness edits (2026-10-20)

Harness edits (tests/integration/test_rebuild_real.py): the entry expectation uses `DESKTOP_ID = "debian-xterm"`; the rebuild call of this test goes through `run_ginn_when_idle`, which retries while `GINN-E-REALM-BUSY` (rc 75) is returned.

- Rerun with only the entry fix: FAILED (31.65 s): `GINN-E-REALM-BUSY: sync in progress` (rc 75) on the rebuild. The F14 watcher is now active, the fixture's `ginn install xterm` touches the hook stamp, and the watcher's `ginn _sync` holds the realm when the rebuild starts right after. Finding: any ginn command issued within moments after a `ginn install` can get rc 75 from ginn's own watcher sync (ginn's own transactions trigger the hook). Product behaviour is documented (wait and retry), but worth a decision; not changed here.
- Final: `-k test_rebuild_keeps_home_manifest_and_settings`: 1 passed in 45.52 s (foreground). Rebuild keeps home, manifest and settings, regenerates the entry.
- Only this test was rerun. The other nine items kept their earlier verdicts; the three services-on items remain NOT RUN (`GINN-E-HOST-UNACKED`, real store).
