# W4: M5 export host gate (test_export_real.py), F14 gate

Run: 2026-10-20, commit e84c4c3 (worktree release-v1; tests/integration/test_aur_real.py has an uncommitted harness fix where noted), host Void Linux glibc x86_64, kernel 7.2.11_1, podman 6.1.4. `ginn doctor`: 18 PASS, 0 WARN, 0 FAIL. Full log: Wave 4 scratchpad gate logs (not committed). Wave 4 task W4.

Command: `GINN_INTEGRATION=1 python -m pytest tests/integration/test_export_real.py -v -s -rxXs`

Result: 7 failed, 0 passed in 61.45 s. No XFAIL and no XPASS: the two native-transaction items (strict xfail F14) FAILED with a plain pytest.fail at the install step, before the menu-entry assertion, so the F14 gate is NOT EVALUATED. The F14 marker was NOT removed.

| Item | Verdict | Cause |
|---|---|---|
| test_install_exports_entry_and_launch_prefix | FAIL | `ginn install w1f-exp xterm` GINN-E-PKG-MANAGER, dpkg exit 100 |
| test_sync_latency_over_20_runs (NFR-009) | FAIL | same install, same cause |
| test_native_install_reaches_the_menu_within_5s (F14) | FAIL, not XFAIL | native `apt-get install rxvt-unicode` fails, dpkg error 1 |
| test_native_removal_unexports_within_5s (F14) | FAIL, not XFAIL | native install fails first |
| test_remove_unexports_entry_and_icon | FAIL | install fails |
| test_same_app_in_two_realms_gives_two_entries | FAIL | install fails |
| test_bin_export_runs_the_binary_in_the_realm | FAIL | `install ripgrep` fails |

Decisive line (dpkg inside the Debian realm, unpacking the font dependency of xterm): `error creating directory './usr/share/fonts/truetype': Read-only file system` (packages fonts-dejavu-mono, fonts-dejavu-core).

Diagnosis: PRODUCT bug (host-dependent). The realm binds the host `/usr/share/fonts` read-only (FR-061 fonts grant, `src/ginn/runtime/mounts.py:69` FONTS, bound at `:201` while FONTS_TIMEZONE is on). This host has no `/usr/share/fonts/truetype`, so dpkg cannot create it. Any Debian package that ships into a new subdirectory of /usr/share/fonts (fonts-dejavu is a dependency of xterm and rxvt-unicode) cannot be installed, in the default Basic tier. NFR-009 and the F14 hook question (apt hook, dnf5 hook CL-35, 5 s bound) have no measurement from this run. The fedora leg of F14 was not reached.
