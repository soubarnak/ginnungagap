# W4: M5 AUR host gate (test_aur_real.py)

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

Command: `GINN_INTEGRATION=1 python -m pytest tests/integration/test_aur_real.py -v -s -rxXs`

First run: 1 failed, 1 passed, 1 skipped (43.24 s). `test_aur_build_leaves_package_and_runtime_closure_only` failed in the HARNESS: `pacman -Sgq base-devel` answers `error: package group 'base-devel' was not found`, because Arch now ships base-devel as a package, not a group.

Harness fix (test bug, tests/integration/test_aur_real.py, `base_devel_members`): when `pacman -Sgq` fails, read `pacman -Si base-devel` and take its Depends On names plus base-devel itself. No product code touched; the product installs the toolchain by the name `base-devel` (`src/ginn/packages/aur.py:102`), which works for either.

Second run (final): 2 passed, 1 skipped in 69.09 s.
- FR-043 AC-1 (build leaves package and runtime closure, no build residue, manifest source AUR, method GINN): PASS.
- FR-043 AC-2 clone failure restores the package set: PASS.
- FR-043 AC-2 build failure: SKIP (`GINN_AUR_FAILING_PACKAGE` is not set).
