# W4: M6 tiers host gate (test_tiers_real.py)

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

Command: `GINN_INTEGRATION=1 GINN_T49_RESULTS=<file> python -m pytest tests/integration/test_tiers_real.py -v -s -rxXs`

Result: 9 passed in 19.36 s (pytest count). Inside the FR-067 test the item FR-067-AC1 is recorded as SKIP ("the image has no ssh-add"), as in the 2026-10-13 run (T6.8-tiers-real.md); FR-067-socket-and-stop PASS.

Items PASS: FR-061 AC1, FR-061 AC2, FR-062 AC2, FR-062 AC4 (kvm open), FR-063 AC1, FR-063 AC2, FR-064 AC1, FR-066 attach-only, FR-067 socket and stop. FR-067 AC1 SKIP.
