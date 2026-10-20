# W4: M4 lifecycle host gate (test_lifecycle_real.py)

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

Command: `GINN_INTEGRATION=1 GINN_T49_RESULTS=<file> python -m pytest tests/integration/test_lifecycle_real.py -v -s -rxs`

Result: 23 passed, 1 skipped in 121.94 s. Skip: `test_musl_smoke_create_start_exec_delete` (no musl host).

All 23 items PASS. Numbers from the run:
- create (debian, pull included, image removed first): 5.098 s.
- start, 20 cycles: p50 0.39 s, p95 0.471 s, max 0.673 s (target p95 <= 2 s: PASS). Load 6.32 8.14 8.50, io pressure avg10 20.62.
- stop, services off, 20 cycles: 0.213 to 0.267 s each.
- no-init control: stop waits the full timeout, 30.079 s, exit 137 (MEASURED).
- exec, 100 launches: p50 151.5 ms, p95 165.7 ms, max 231.8 ms (target p95 <= 300 ms: PASS). Load 5.95 7.79 8.37.
- no systemd: PID 1 is podman-init, 3 processes (PASS).
- Grants (a): socket, directory, file under keep-id, read-only file and directory, recursive read-only child mount, lazy reattach, busy directory with MNT_DETACH, plain umount2 controls: all PASS.
- (b) drive mounted after start: reattach needed and works; a mount made in the store namespace is not copied (MEASURED).
- Subordinate-uid file in the Realm Home: delete handled (see item i, PASS).
- Teardown: store pause process stopped (alive_after_kill false).
