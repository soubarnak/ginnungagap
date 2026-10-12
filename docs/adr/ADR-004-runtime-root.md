# ADR-004: Runtime directory and Podman storage root

- Status: Accepted
- Date: 2026-10-12

## Context

DR-010 lists `$XDG_RUNTIME_DIR/ginn` as an Owned Path. Three requirements depend on where the runtime root is:

- FR-123 and DR-033 (single writer per realm): the flock files in `locks/` must be the same files for every ginn process of the user. Two processes that see different lock directories can both run a mutating operation on one realm.
- Podman's rootless runroot and tmpdir: podman keeps its state there and detects a reboot by finding them empty. That detection needs a tmpfs. A disk path breaks it.
- DEP-3 names `XDG_RUNTIME_DIR` as provided by elogind, turnstile or an equivalent, that is, by a login session. The boot-time user supervisor (DEP-6, FR-081, FR-084) starts without a login session.

Host Setup already creates `/run/ginn/<uid>` for the supervisor (`hostsetup/runit_system.py`), and the supervisor exports `XDG_RUNTIME_DIR` and `GINN_RUNTIME_DIR` with that value.

The Opus review of the T2 and T3 code (R4, finding 1) found the split this decision removes. `from_env` resolved `$XDG_RUNTIME_DIR/ginn`, so a session `ginn` used `/run/user/<uid>/ginn` while the supervisor used `/run/ginn/<uid>`. The locks, the podman runroot and the podman tmpdir were then in two places. The same review found that a missing runtime directory fell back to a disk path (finding 2), and that podman's default tmpdir is the user's own `$XDG_RUNTIME_DIR/libpod/tmp`, which a user's own podman also uses (finding 3).

Ranked characteristics (SRS section 4), in order:

1. Performance efficiency: not affected. The lock and runroot paths are not on a hot path.
2. Host integrity and isolation: the runtime root must be ginn's own. Podman must not share a runroot or tmpdir with the user's own podman.
3. Recoverability: the runroot and the tmpdir must be on tmpfs so that a reboot is detected. A refusal is better than a silent disk fallback.
4. Interoperability: the directory is the one Host Setup creates, so the supervisor and every session agree on it.
5. Usability: a missing directory must produce one clear error that names Host Setup.

Assumptions, each to be confirmed by a spike or task:

- `/run/ginn/<uid>` survives logout and is visible from the user's session. Spike S-04 (roadmap, M1) is still to run. This ADR is decided ahead of it; S-04 is the confirming test.
- Podman reads `XDG_RUNTIME_DIR` for its rootless state. Assumed from podman's documented behaviour; S-04 and the first real podman task confirm it.
- Podman accepts a `CONTAINERS_CONF` path that does not exist. Podman's docs say builtin defaults apply when no containers.conf is found, but the behaviour for an explicit missing path is unverified. See the TODO in `podman/cli.py`.

## Decision

ginn uses one runtime root per user: `/run/ginn/<uid>`, created by Host Setup. Every ginn process of that user resolves it the same way, whatever the session. Owned Paths never read `$XDG_RUNTIME_DIR`. `GINN_RUNTIME_DIR`, when set to an absolute path, replaces the default. The boot-time supervisor exports the same path, and tests use the override. When the directory is missing and no override is set, ginn refuses with GINN-E-USAGE and names Host Setup and `ginn doctor`. It never creates a runtime root on disk.

Every podman call carries `--root`, `--runroot`, `--tmpdir` and `--runtime crun`. The tmpdir is `<runtime>/podman-tmp`, never the user's libpod tmpdir. The subprocess environment sets `XDG_RUNTIME_DIR` to the runtime root, pins `CONTAINERS_CONF` and `CONTAINERS_STORAGE_CONF` to ginn's own files, and removes `CONTAINERS_CONF_OVERRIDE`.

This decision deviates from DR-010, which names `$XDG_RUNTIME_DIR/ginn`. The deviation stands until SRS clarification CL-1 is written into `GINN-SRS-001-v1.0.md`. CL-1 is not in that file yet.

## Alternatives considered

- `$XDG_RUNTIME_DIR/ginn`, as DR-010 is worded. Rejected. The variable exists only with a login session (DEP-3), so the boot-time supervisor and an interactive session would resolve different roots, and the locks would split (review finding 1).
- Fall back to `state/run` on disk when the runtime directory is missing, which was the earlier behaviour. Rejected. A disk path is not tmpfs, so podman cannot detect a reboot, and the fallback hides a host that was never set up (review finding 2).
- Let podman choose its tmpdir. Rejected. Its default is the user's `$XDG_RUNTIME_DIR/libpod/tmp`, which a user's own podman shares, so ginn could corrupt that podman's state (review finding 3).
- The podman default OCI runtime, `runc`. Rejected as the default. crun is the runtime the spikes validate, and keep-groups works only with crun. The value stays configurable (`oci_runtime`).

## Consequences

- Code must not read `XDG_RUNTIME_DIR` for an Owned Path. The only code that sets it is `PodmanCli`, and it sets it for podman's sake.
- Code must not create runtime state outside the runtime root. The podman tmpdir and runroot sit under it.
- Every podman argv carries `--root`, `--runroot`, `--tmpdir` and `--runtime`. A test checks every call (`tests/unit/test_podman_cli.py`).
- `ginn host-setup` and `ginn doctor` can run before `/run/ginn/<uid>` exists. The refusal must not stop them. T3.4 and T3.6 must catch it and report it. This ADR does not build that.
- A missing `crun` makes every podman call fail. The packaging template (`packaging/void/srcpkgs/ginn/template`) listed `runc` and not `crun`. R8 (0d7e52e) corrected it, so the template now declares `crun`.
- Follow-up: SRS clarification CL-1; spike S-04 confirms that the runtime root survives logout; the first task that drives real podman through `PodmanCli` writes ginn's `containers.conf` and `storage.conf` (TODO in `podman/cli.py`) and verifies the missing-file behaviour.
- The error catalogue has no code for "Host Setup has not created the runtime directory". GINN-E-USAGE is used for now. A dedicated code is requested for the errors owner (T2.14).

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| DR-010 | Owned Paths: this ADR decides the runtime root. It deviates from the `$XDG_RUNTIME_DIR/ginn` wording pending CL-1. |
| DR-033 | Single writer: the flock files live in the one runtime root, so two ginn processes of one user contend on the same locks. |
| FR-123 | A second mutating operation on a realm is refused at once only when both processes see the same `locks/`. |
| FR-122 | Interrupted operations are recovered from the journal. The runroot and tmpdir on tmpfs let podman detect a reboot, and no disk fallback hides that. |
| FR-081 | The runit supervisor of services-on realms runs from the same runtime root as the session. |
| FR-084 | Autostart at boot runs in the supervisor, which has no login session, so it cannot depend on `XDG_RUNTIME_DIR`. |
| IR-020 | Podman is the sole runtime. Every call carries `--root`, `--runroot`, `--tmpdir` and `--runtime crun`. |
| CR-003 | Rootless only. `/run/ginn/<uid>` is owned by the user, and nothing here runs as root. |
| NFR-SEC-005 | The new flags are argv entries. The OCI runtime name is validated with `require_arg`. |
| NFR-040 | Failures map to catalogue codes. The missing-directory refusal uses GINN-E-USAGE until a dedicated code exists. |
