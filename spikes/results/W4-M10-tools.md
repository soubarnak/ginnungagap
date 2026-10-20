# W4: M10 tool gates

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

- `tools/release.sh check`: FAIL, "working tree has uncommitted changes" (the harness fix in tests/integration/test_aur_real.py and these result files). The template/pyproject version check was not reached. Rerun after commit.
- `tools/trace-no-xbps.sh -- python -m pytest tests/integration/test_lifecycle_real.py test_cli_real.py test_tiers_real.py -q`: INCONCLUSIVE, NOT A PASS. The tool reported "28 exec attempt(s) traced, 0 of them xbps-*" and exit 3 (command exit 1), but under strace the tests failed (5 failed, 28 errors in 3.81 s, GINN-E-INTERNAL at podman start): strace removes setuid privileges, so rootless newuidmap does not work and the realms never started. The 28 execs are not the suite. Needs a run with a working tracer (for example the CI host or a user with CAP_SYS_PTRACE-compatible setup).
- `tools/provenance-scan.sh`: NOT RUN. It needs `PROVENANCE_REPOS` (reference clone URLs, which the clean-room rules keep out of this repo) and `copydetect`, which is not installed.
- `tools/host-diff.sh`: NOT RUN. Earlier gates did not all run clean (M5, M7, M8, relay), and the lifecycle suite alone would not close it.
- Measure scripts (NFR-006, 008, 009, 010): NOT RUN. They need a ginn-made test realm on the real store, which is HOST-UNACKED (W4-M7). NFR-006 also needs 7z and glmark2.
