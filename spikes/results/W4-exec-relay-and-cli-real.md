# W4: exec relay and CLI host gates

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

test_cli_real.py: `GINN_INTEGRATION=1 python -m pytest tests/integration/test_cli_real.py -v -s`: 1 passed in 35.24 s (`test_cli_smoke_one_realm_end_to_end`: PASS).

test_exec_relay_real.py: 1 passed, 19 failed in 16.76 s. NOT RUN (blocked): the gate uses the real Owned Paths, and every create returned rc 4 `GINN-E-HOST-UNACKED` (same cause as W4-M7-services-real.md). No relay verdict.
