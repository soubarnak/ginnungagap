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
