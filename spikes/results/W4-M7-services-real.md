# W4: M7 services gate (test_services_real.py, T7.7): BLOCKED, NOT RUN

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

Command: `GINN_INTEGRATION=1 python -m pytest tests/integration/test_services_real.py -v -s -rxXs`

Result: 1 passed, 12 failed, 3 errors, 2 skipped in 71.39 s. NO verdict for any FR-080..084 item: every product call returned rc 4, `GINN-E-HOST-UNACKED` (FR-006). This gate drives the real supervisor and the user's real Owned Paths, not a temp base.

Cause: ENVIRONMENT. `~/.local/state/ginn/integrity/pending.json` exists: the host integrity check found changed paths since the baseline (/etc/apparmor.d/libvirt, /etc/containers/registries.conf, /etc/fonts/conf.d, /etc/ld.so.cache, /etc/libvirt/qemu, /etc/sv/*, /var/db/xbps/* and more). While pending, every mutating op is refused (`src/ginn/integrity/state.py:15`). The remedy is `ginn integrity --ack`, which needs the user to review the list. It was not run (it accepts a security baseline).

Skips: FR-085 Docker part B (deferred, CL-2); musl leg (no musl host).

This replaces nothing in T7.7-services-real.md (2026-10-14), whose verdicts stand until a rerun.
