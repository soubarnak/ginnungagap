# ADR-004: Runtime directory and Podman storage root

- Status: Accepted, logout evidence simulated (S-04s; the exact path `/run/ginn/<uid>` was not run). The real logout run is pending T7.8.
- Date: 2026-10-12
- Amended: 2026-10-12 (R11). The Podman configuration finding is verified, the environment is an allowlist, the config files and the image tmpdir are Owned Paths, and the runtime root is checked for owner and mode. See Verification.
- Amended: 2026-10-12 (D7). Status qualifier added. The logout evidence is the simulation in the addendum below, and the real logout run is pending T7.8. No decision changes.

## Context

DR-010 lists `$XDG_RUNTIME_DIR/ginn` as an Owned Path. Three requirements depend on where the runtime root is:

- FR-123 and DR-033 (single writer per realm): the flock files in `locks/` must be the same files for every ginn process of the user. Two processes that see different lock directories can both run a mutating operation on one realm.
- Podman's rootless runroot and tmpdir: podman keeps its state there and detects a reboot by finding them empty. That detection needs a tmpfs. A disk path breaks it.
- DEP-3 names `XDG_RUNTIME_DIR` as provided by elogind, turnstile or an equivalent, that is, by a login session. The boot-time user supervisor (DEP-6, FR-081, FR-084) starts without a login session.

The supervisor run script creates `/run/ginn/<uid>` at each start (`hostsetup/runit_system.py`). `/run` is a tmpfs, so a directory made once by Host Setup would vanish at reboot. The supervisor exports `XDG_RUNTIME_DIR` and `GINN_RUNTIME_DIR` with that value.

The Opus review of the T2 and T3 code (R4, finding 1) found the split this decision removes. `from_env` resolved `$XDG_RUNTIME_DIR/ginn`, so a session `ginn` used `/run/user/<uid>/ginn` while the supervisor used `/run/ginn/<uid>`. The locks, the podman runroot and the podman tmpdir were then in two places. The same review found that a missing runtime directory fell back to a disk path (finding 2), and that podman's default tmpdir is the user's own `$XDG_RUNTIME_DIR/libpod/tmp`, which a user's own podman also uses (finding 3).

The Opus review of b80f25b..b5f66f4 (R11) found four more gaps. The subprocess environment copied the whole parent environment, so `CONTAINER_HOST` and the storage variables reached podman. Podman's config files were pinned but never written. Image downloads used `/var/tmp`, which is not an Owned Path. A runtime directory with the wrong owner or mode was accepted.

Ranked characteristics (SRS section 4), in order:

1. Performance efficiency: not affected. The lock and runroot paths are not on a hot path.
2. Host integrity and isolation: the runtime root must be ginn's own. Podman must not share a runroot or tmpdir with the user's own podman, and must not read the user's configuration.
3. Recoverability: the runroot and the tmpdir must be on tmpfs so that a reboot is detected. A refusal is better than a silent disk fallback.
4. Interoperability: the directory is the one the supervisor creates, so the supervisor and every session agree on it.
5. Usability: a missing or unsafe directory must produce one clear error that names what to run.

Assumptions, each to be confirmed by a spike or task:

- `/run/ginn/<uid>` survives logout and is visible from the user's session. The simulated logout S-04s tested a tmpfs location of the same kind, and it survived (Addendum). The real logout run S-04 was invalid, because the test login never got an elogind session. A real logout run at the exact path `/run/ginn/<uid>` is still open.
- Podman reads `XDG_RUNTIME_DIR` for its rootless state. Assumed from podman's documented behaviour; S-04 and the first real podman task confirm it.
- Podman accepts a `CONTAINERS_CONF` or `CONTAINERS_STORAGE_CONF` path that does not exist. **False.** Podman 5.8 refuses it. See Verification. ginn therefore writes both files before any podman call.

## Decision

ginn uses one runtime root per user: `/run/ginn/<uid>`. The supervisor run script creates it at each start, as root, owned by the user with mode 0700 (`hostsetup/runit_system.py`). Every ginn process of that user resolves it the same way, whatever the session. Owned Paths never read `$XDG_RUNTIME_DIR`. `GINN_RUNTIME_DIR`, when set to an absolute path, replaces the default, and the supervisor exports the same path.

Every resolution checks the directory (R11):

- missing: `from_env` refuses with GINN-E-HOST-NOT-SET-UP (exit 77);
- a symbolic link, a non-directory, not owned by the user, or not mode 0700: `from_env` refuses with GINN-E-RUNTIME-DIR-UNSAFE (exit 77).

The checks apply to the override too. Neither error repairs the directory. ginn never creates a runtime root on disk.

Every podman call carries `--remote=false`, `--root`, `--runroot`, `--tmpdir` and `--runtime crun`. The tmpdir is `<runtime>/podman-tmp`, never the user's libpod tmpdir. `--remote=false` is a guard in its own right: podman(1) makes `--remote` default to true when `CONTAINER_HOST` is set.

The subprocess environment is an allowlist (CR-006), not a copy of the parent's. It holds PATH, HOME, TERM, USER, LOGNAME, LANG and LC_*, plus these pins:

- `XDG_RUNTIME_DIR` is the runtime root.
- `CONTAINERS_CONF` and `CONTAINERS_STORAGE_CONF` are ginn's own files.
- `TMPDIR` is `data/image-tmp`. Podman's image downloads use TMPDIR, which `--tmpdir` does not cover. The directory is on disk, not tmpfs, because images are large.

Nothing else from the parent reaches podman. That drops `CONTAINER_HOST`, `CONTAINER_CONNECTION`, `CONTAINER_SSHKEY`, `STORAGE_DRIVER`, `STORAGE_OPTS` and `CONTAINERS_CONF_OVERRIDE`.

`OwnedPaths.ensure_config()` writes the two config files before any podman call, from containers.conf(5) and containers-storage.conf(5):

- containers.conf: `[engine] runtime = "crun"` and `events_logger = "file"`;
- storage.conf: `[storage] driver = "overlay"`, with `runroot` and `graphroot` equal to the `--runroot` and `--root` flags.

Both are written atomically with mode 0600 (DR-035), and only when their content or mode differs. `PodmanCli` refuses to run podman while either file is missing. That refusal is GINN-E-INTERNAL, because ginn writes these files.

This decision deviates from DR-010, which names `$XDG_RUNTIME_DIR/ginn`. The deviation stands until SRS clarification CL-1 is written into `GINN-SRS-001-v1.0.md`. CL-1 is not in that file yet.

## Verification (R11, 2026-10-12)

Podman 5.8 was run once in each of two forms, in a scratch directory. The command was `podman --root <scratch>/r --runroot <scratch>/rr --tmpdir <scratch>/t info --format json`. XDG_RUNTIME_DIR was set to a scratch directory as well, so podman's runtime files stayed out of the user's `/run/user/<uid>`.

- With `CONTAINERS_CONF` naming a file that does not exist: exit 1, with `Failed to obtain podman configuration: finding config on system: CONTAINERS_CONF file: faccessat <path>: no such file or directory`. Nothing was written to stdout.
- With `CONTAINERS_STORAGE_CONF` naming a file that does not exist: exit 1, with `Failed to obtain podman configuration: stat <path>: no such file or directory`. Nothing was written to stdout.

So the builtin-defaults assumption is false. Before R11, every podman call from `PodmanCli` failed on a clean host. `ensure_config()` and the refusal in `PodmanCli` close that gap. The missing-file behaviour above is not rechecked after that.

Cleanup of the scratch root used `podman ... unshare rm -rf`. It failed with EBUSY on the overlay directory while the process that podman left behind held it. That process was stopped, and the scratch directories were then removed with a plain `rm`, since every file was owned by the user's own uid. The user's own podman directories (`~/.local/share/containers`, `~/.config/containers`, `/run/user/<uid>/libpod`) were checked before and after and did not change.

podman(1) documents `--remote` as a boolean that defaults to true when `CONTAINER_HOST` is set. The man page was read, and podman was not run, to confirm the flag and its default.

Residual, not pinned by R11: containers/image still reads user files under `$HOME/.config/containers` (registries and signature policy), because HOME is on the allowlist and `XDG_CONFIG_HOME` is not. A user's registry mirror or policy can therefore still affect pulls. Pulls are still checked against the digest pinned in the Trust Manifest (FR-011). Pinning those files is open; see Consequences.

## Alternatives considered

- `$XDG_RUNTIME_DIR/ginn`, as DR-010 is worded. Rejected. The variable exists only with a login session (DEP-3), so the boot-time supervisor and an interactive session would resolve different roots, and the locks would split (review finding 1).
- Fall back to `state/run` on disk when the runtime directory is missing, which was the earlier behaviour. Rejected. A disk path is not tmpfs, so podman cannot detect a reboot, and the fallback hides a host that was never set up (review finding 2).
- Let podman choose its tmpdir. Rejected. Its default is the user's `$XDG_RUNTIME_DIR/libpod/tmp`, which a user's own podman shares, so ginn could corrupt that podman's state (review finding 3).
- Pass the whole parent environment and strip known variables. Rejected. The list of variables podman reads is long and changes between versions, so a denylist misses some. An allowlist fails closed (R11).
- Repair a runtime directory with a wrong owner or mode. Rejected. The owner check exists to catch a directory someone else prepared, and a silent `chmod` would hide that. The error names the cause instead.
- The podman default OCI runtime, `runc`. Rejected as the default. crun is the runtime the spikes validate, and keep-groups works only with crun. The value stays configurable (`oci_runtime`).

## Consequences

- Code must not read `XDG_RUNTIME_DIR` for an Owned Path. The only code that sets it is `PodmanCli`, and it sets it for podman's sake.
- Code must not create runtime state outside the runtime root. The podman tmpdir and runroot sit under it.
- Every podman argv carries `--remote=false`, `--root`, `--runroot`, `--tmpdir` and `--runtime`. A test checks every call (`tests/unit/test_podman_cli.py`).
- Error codes. A missing runtime directory is GINN-E-HOST-NOT-SET-UP, which replaced the GINN-E-USAGE that R4 used (R8, 0d7e52e). A wrong owner or mode is GINN-E-RUNTIME-DIR-UNSAFE. Both exit 77, EX_NOPERM ("has not run for this user"), because the remedy is to run `ginn doctor` and Host Setup or the supervisor, not to change a flag. A missing ginn config file is GINN-E-INTERNAL.
- `ginn host-setup` and `ginn doctor` can run before `/run/ginn/<uid>` exists. The refusal must not stop them. T3.4 and T3.6 must catch it and report it. This ADR does not build that.
- A missing `crun` makes every podman call fail. The packaging template (`packaging/void/srcpkgs/ginn/template`) listed `runc` and not `crun`. R8 (0d7e52e) corrected it, so the template now declares `crun`.
- Follow-up, open: SRS clarification CL-1; spike S-04, which confirms that the runtime root survives logout; pinning the user's registries and signature policy (the residual above), which needs `CONTAINERS_REGISTRIES_CONF` and a policy path, each checked against containers-registries.conf(5) and containers-policy.json(5) before it is used.
- Follow-up, done in R11: the config files are written and their absence is refused. The TODO that asked for this verification is closed by the Verification section above.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| DR-010 | Owned Paths: this ADR decides the runtime root. It deviates from the `$XDG_RUNTIME_DIR/ginn` wording pending CL-1. The runtime root is checked for owner and mode, and the config files and the image tmpdir are Owned Paths. |
| DR-033 | Single writer: the flock files live in the one runtime root, so two ginn processes of one user contend on the same locks. |
| FR-123 | A second mutating operation on a realm is refused at once only when both processes see the same `locks/`. |
| FR-122 | Interrupted operations are recovered from the journal. The runroot and tmpdir on tmpfs let podman detect a reboot, and no disk fallback hides that. |
| FR-081 | The runit supervisor of services-on realms runs from the same runtime root as the session. |
| FR-084 | Autostart at boot runs in the supervisor, which has no login session, so it cannot depend on `XDG_RUNTIME_DIR`. |
| IR-020 | Podman is the sole runtime. Every call carries `--remote=false`, `--root`, `--runroot`, `--tmpdir` and `--runtime crun`. |
| CR-003 | Rootless only. `/run/ginn/<uid>` must be owned by the user with mode 0700, and nothing here runs as root. |
| CR-006 | After Host Setup, ginn writes only to Owned Paths. The podman environment is an allowlist, and TMPDIR and the config files are Owned Paths. |
| NFR-SEC-005 | The new flags are argv entries. The OCI runtime name is validated with `require_arg`. |
| NFR-040 | Failures map to catalogue codes: GINN-E-HOST-NOT-SET-UP and GINN-E-RUNTIME-DIR-UNSAFE for the runtime root (exit 77), and GINN-E-INTERNAL for a missing ginn config file. |

## Addendum: S-04s logout simulation

- Date: 2026-10-12

This addendum records a simulation. It changes no decision above. It adds the evidence for the runtime root under logout. The simulation is in `spikes/results/S-04.md`, section "Simulated logout (S-04s/S-08s)".

What ran. The real-logout run S-04 was invalid. Its ssh login never registered with elogind on this host, so it measured nothing. S-04s simulated the logout instead. It removed a stand-in for `/run/user/<uid>`, and it recreated that stand-in at login. It killed no process. The runs used podman 5.8.8 and crun 1.30.1, as uid 1000.

Results:

- Variant A (runroot and tmpdir outside the session directory, on tmpfs): passes. After the simulated logout and login, the containers stayed running. `exec` and `stop` worked with the same flags. The spike placed variant A under `/dev/shm`, not under `/run/ginn/<uid>`. The exact path in this ADR was not run. The result covers the kind of location, not the exact path.
- Variant B (runroot and tmpdir inside the session directory): fails. On the first podman call after login, podman printed `Podman detected system restart - performing state refresh`. The containers read as exited with pid 0. `exec` failed with rc 255. `stop` had no effect. `rm -f` returned 0, and the process survived as an orphan.
- In the same refresh, podman logged `level=error` with `acquiring lock 6 ...: file exists` for the container. This error was not traced.

Assumption. The result holds when elogind does not kill user processes at logout. The Void default is `#KillUserProcesses=no` in `/etc/elogind/logind.conf`, so the default is no. With `KillUserProcesses=yes`, elogind kills the processes at logout, and the simulation does not apply. The doctor check in T3.6 fails when the effective value is `yes`.

The SIGKILL on stop. The spike's `stop` logged a SIGTERM failure and a SIGKILL for variant A. The cause is the test container. It ran `sleep infinity` as PID 1 with no `--init`. ginn passes `--init` to every services-off realm (FR-082), so this is not a finding for those realms. Services-on realms run systemd as PID 1 and do not use `--init`. S-04s did not test them.

Caveats:

- A simulation, not a real logout. The PAM session close, the elogind teardown and the unmount of the runtime tmpfs were not measured. The test containers used `--cgroups disabled` and `--network none`.
- The `acquiring lock ... file exists` error was not traced.
- podman 5.8.8 only.
- The path `/run/ginn/<uid>` was not run. The supervisor run script creates it at each start (above). A run at that exact path is still open.

Consequence. The decision above holds. The runtime root must sit outside the per-login directory. Variant B shows what happens when it does not. ADR-008 builds the session socket rebind on the variant A layout. A real logout test (S-04, rerun) is still open.
