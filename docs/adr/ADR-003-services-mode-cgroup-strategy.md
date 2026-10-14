# ADR-003: Services-mode cgroup strategy

- Status: Accepted, S-01 on runc and crun (the roadmap argv ran under the pin on both). FR-080 AC-1/AC-2 on crun, and a run from the root-owned `supervisor` leaf on crun, are pending T7.7.
- Date: 2026-10-12
- Amended: 2026-10-12 (D5). The keep-id form, the chown scope (S-01 step 6) and the FR-083 generation wording are corrected. No decision changes.
- Amended: 2026-10-12 (D6). The chown scope and the leaf ownership are stated in full, with the evidence for a root-owned leaf and the reason the user does not need write access to it. No decision changes. F6a changes the code to match.
- Amended: 2026-10-12 (D7). Status qualifier added. The root-owned leaf is evidenced by the unpinned runc leg only, the order of the root writes is stated, and the stale `cgroups.py` note is removed. No decision changes.
- Amended: 2026-10-14 (per-realm service contract). The runit service directory, the run and finish scripts, the crash rule and the create-time forms of ADR-007 a, b and d are decided at the end of this file. No decision above changes.
- Amended: 2026-10-14 (start, stop, delete and autostart through sv). The CLI requests and the supervised process records; the lock is free during the wait; the session grants of a supervised start attach at enter; the `down` file follows autostart. The decisions are at the end of this file. No decision above changes.
- Amended: 2026-10-14 (a services change is a rebuild, T8.3). Decision 9 of the start amendment is superseded as far as `services on|off` now rebuilds, and a services-on rebuild replays through runit. The decision is at the end of this file.
- Amended: 2026-10-12 (D9). Host Setup does not make the root writes under the subtree. The supervisor run script makes them at each start of its service, and Host Setup only captures their pre-images (ADR-013). The Context, the placement paragraph of the Decision, the Consequences and the FR-001 and R-001 rows said otherwise, and they are corrected. Undo follows ADR-013 Decision 15, not "restores the recorded value". No decision changes. Each statement was confirmed against the render template in `src/ginn/hostsetup/runit_system.py`, which is the state at the time of this amendment.
- Amended: 2026-10-14 (services-on entry through the supervisor relay, ADR-014). The D6 leaf-ownership statement and the line-45 rule extend to `ginn exec`, `ginn enter` and every other entry from a user session. A process outside `/ginn/<uid>/supervisor` reaches a services-on realm only through the relay, because a move made from a login shell needs write access on the top cgroup, which is root-owned (F2, T7.7). The S-01 exec claim is corrected at line 332, and the placement caveat at line 70 cites ADR-014. The notes sit on the original lines 45, 53, 70 and 332, and each is marked "Amended 2026-10-14". No decision in this ADR changes. The routing decision is ADR-014, Decision 1, and T7.10 builds it.

## Context

A services-on realm runs systemd as its init, so systemd needs cgroup v2 controllers under the realm (FR-080). A rootless container on a runit host may get no delegation. That is risk R-001. Two steps run as root: Host Setup (FR-001), and the supervisor's run script at each start of its service. This ADR decides how a services-on realm gets a delegated cgroup without a root write at each start.

Ranked characteristics (SRS section 4), in order: performance efficiency, host integrity and isolation, recoverability, interoperability, usability. Host integrity and isolation settle the placement. Root cgroup writes happen in the supervisor's run script at each start of that service (at boot, or after a restart by runsv). Host Setup only captures their pre-images (ADR-013). They never happen per realm start. Otherwise ginn writes only inside the user's own subtree. Interoperability (systemd runs as PID 1) settles the argv. Performance efficiency is decided in ADR-007.

Measured on the reference host (Void Linux glibc, kernel 7.2.9_1, podman 5.8.8 with the cgroupfs manager, rootless). Source: `spikes/results/S-01.md`, crun leg, roadmap argv rows and their matched rows:

| Runtime | Flavor | `is-system-running` | Failed units | Boot to running (s) | Stop (s) | Stop exit / OOM |
|---|---|---|---|---|---|---|
| crun 1.30.1 | roadmap argv (pasta networking) | running | 0 | 0.4 | 0.2 | 0 / false |
| crun 1.30.1 | `--userns keep-id --network host` | running | 0 | 1.3 | 0.2 | 0 / false |
| runc 1.5.2 | roadmap argv (pinned) | running | 0 | 0.5 | 0.3 | 0 / false |
| runc 1.5.2 | `--userns keep-id --network host` (pinned) | running | 0 | 0.3 | 0.3 | 0 / false |

In every row, PID 1 sits under `/ginn/1000/<realm>/libpod-*/init.scope`. Each row ran once.

The placement must hold. The process that starts podman must already sit inside `/sys/fs/cgroup/ginn/<uid>/`. A session process cannot move itself there. A move needs write access to `cgroup.procs` of the common ancestor, which here is the cgroup root, and that file is root-owned. S-01 and S-05r simulated the product placement with logged root writes of the launcher pid. S-01's crun leg made two, one per runtime leg. S-05r made one. In the product, the supervisor run script does this write at each start of its service. `src/ginn/hostsetup/runit_system.py` (step 3) writes the script's own pid into `/sys/fs/cgroup/ginn/<uid>/supervisor/cgroup.procs` as root. Then it runs `exec setpriv ... runsvdir`. `exec` keeps the pid and the cgroup. Every services-on realm starts from a descendant of runsvdir, so it already sits in the subtree.

## Decision

ginn starts a services-on realm with these podman options, added to the realm's normal create argv:

```
--systemd=always --cgroupns private --cgroup-parent /ginn/<uid>/<realm> --user 0:0 \
--stop-signal SIGRTMIN+3 --stop-timeout 30 --runtime crun
```

`/ginn/<uid>/<realm>` is relative to `/sys/fs/cgroup`. This is the set S-01 ran for the crun roadmap row. S-01 also added `-d`, `--name`, the image and `/sbin/init`. The realm's own options stay in place. For non-Kali realms that means `--userns keep-id` and `--network host`. S-01 ran both flavors, the bare set with pasta networking and the set with `--userns keep-id --network host`. Both reach `running` with zero failed units on crun and on runc.

ginn does not add `--cgroups=split`. Podman 5.8.8 rejects it together with `--cgroup-parent`.

ginn starts a services-on realm only through its runit supervisor entry (`sv`, FR-081). The `ginn` CLI process does not run `podman run` or `podman start` for a services-on realm. Realm processes sit under `/ginn/<uid>/<realm>` only when a descendant of the supervisor starts them. Amended 2026-10-14 (ADR-014, Decision 1): the rule covers `exec` and `enter` too. A process outside `/ginn/<uid>/supervisor` never moves a process into a services-on realm's cgroup. It reaches the realm through the supervisor relay. Services-off realms keep the direct `podman exec` path.

The run script does every root write under the subtree, at each start of the service. Host Setup writes none of them, and it only captures their pre-images (ADR-013, Decisions 1 and 12). The rendered script (`runit_system.py`, `_RUN`) does, in this order:

1. Step 1 creates `/run/ginn/<uid>` (`mkdir -p`), sets it to the user's uid and gid, and sets mode 0700 (ADR-004).
2. Step 2 enables, on the root `/sys/fs/cgroup`, each of `cpu`, `cpuset`, `io`, `memory` and `pids` that the node offers and does not yet have in `cgroup.subtree_control`. A failure there is logged, and the script continues. It then creates `ginn` (`mkdir -p`), enables the same controllers there, and exits 1 on failure. It creates `ginn/<uid>/supervisor` (`mkdir -p`, which also creates `ginn/<uid>`), enables the controllers on `ginn/<uid>`, and exits 1 on failure. Last, it chowns `ginn/<uid>` and its `cgroup.procs`, `cgroup.threads` and `cgroup.subtree_control` to the user (`chown_groups`, S-01 step 6). The supervisor leaf is not chowned.
3. Step 3 writes the script's own pid into `ginn/<uid>/supervisor/cgroup.procs` as root, before it drops privileges to the user.

The `supervisor` leaf stays root-owned, and so does everything else under the subtree, so limit files such as `memory.max` stay root-owned too. Migrating a process out of the leaf needs write access on the common ancestor's `cgroup.procs`, which is `/sys/fs/cgroup/ginn/<uid>`, not on the leaf (a cgroup v2 rule; not measured here). The user therefore needs no write access to the leaf. Amended 2026-10-14 (ADR-014, F2): this holds for a move made from inside the supervisor leaf, whose common ancestor is `/sys/fs/cgroup/ginn/<uid>`. It does not hold for a move made from a login shell. That shell sits in `0::/2`, its common ancestor with the realm is the root-owned top cgroup, and so a user-session `podman exec` into a services-on realm fails with EACCES (`spikes/results/T7.7-services-real.md`). The leaf case works (`spikes/results/T7.7-f10-trace.txt`, lines 12 to 45). The session route is therefore relayed, not used directly (ADR-014). The root-owned leaf is evidenced by the unpinned runc leg only. `spikes/s01_systemd_rootless.sh:144` creates the launcher leaf as root, and the variant b note in the S-01 variant table records that leaf as root-owned in that setup. The pinned crun leg created its leaves as the user (`spikes/s01c_crun.sh:223`). The cgroup v2 rule does not depend on the runtime, so the risk is low, and T7.7 runs crun from the root-owned leaf. The order of the root writes does not matter: `spikes/results/S-01.md` (Root steps) lists the pid write before the chown, and the template does the chown in step 2 and the pid write in step 3. On the reference host, `pids` was the only controller that was new in the root `cgroup.subtree_control` (S-01 step 2). Undo follows ADR-013 Decision 15 (ii) and (v), not "restores the recorded value". It writes `-<ctrl>` for each controller that the pre-image lacks, and only in the same boot.

## Alternatives considered

- No `--cgroup-parent`, so podman uses the session scope. Rejected. Podman cannot create `/sys/fs/cgroup/conmon` (permission denied). systemd exits 255 with `Failed to mount cgroup v1 hierarchy` before any unit starts (S-01 variant e).
- `--cgroups=split` with `--cgroup-parent`. Rejected. Podman 5.8.8 refuses the pair (S-01 variant b).
- `--cgroups=no-conmon` with `--cgroup-parent`. Not chosen. It passed on runc (unpinned, S-01 variant c) and kept conmon in the launcher's leaf. The default cgroups mode passes on both runtimes, so the extra flag gains nothing.
- runc as the services-on runtime. Not chosen as the default. runc 1.5.2 also passed the same argv under the pin. crun 1.30.1 is chosen because the NFR-003 memory measurements in ADR-007 ran on crun. This is a choice of evidence, not a runtime fault.
- Rootful podman, or a root step on every realm start. Rejected. Realms are always rootless (CR-003, OOS-3, OOS-10). After Host Setup, ginn writes only to Owned Paths (DR-010, CR-006). A root write per realm start breaks that rule.

## Consequences

- The services-on argv gives systemd as PID 1 and reaches `running` with zero failed units on both runtimes. Root cgroup writes happen at each start of the supervisor service, never per realm start. Host Setup writes none of them.
- The services-on realm must start through `sv` (T7.4). The CLI never starts it directly.
- Services mode is fixed at create. `ginn services on|off` creates a new container generation, and the toggle takes effect at that generation's first start (FR-083). The Install Manifest replay (FR-021, T8.3) must carry installed packages across the new generation. That replay is not measured, so FR-083 AC-1 (htop stays installed) is open.
- The services-on create argv keeps the flags above, with no `--cgroups=split`.
- The run script keeps the chown limited to the paths listed above.
- `ginn exec` placement is not verified. S-01 saw `exec`, `stop` and `top` succeed from the session scope, but it did not check where exec'd processes land. ginn does not rely on that placement. The placement check is in T4.6. Amended 2026-10-14 (ADR-014): the S-01 exec observation was taken from a shell that one root write had already moved into the spike tree (`spikes/results/S-01.md`, line 10 and the addendum after line 37), so it does not measure session placement. T7.7 F2 shows that a session `podman exec` into a services-on realm fails. Placement through the relay is checked by gate G3, and the session-side errno is recorded by gate G10 (ADR-014).
- T3.3 already has the placement step (run script, step 3). T7.1 carries the argv and T7.4 carries the supervisor start. T7.7 runs the test.
- Packaging: the Void template depends on `crun` and `containers-common` (`packaging/void/srcpkgs/ginn/template`). Roadmap T0.6 lists the same runtime dependency, crun and not runc, and containers-common.
- R-001 stays open. S-01 is the early spike that R-001 asks for. The items still open are:
  - FR-080 AC-1 and AC-2 (cron install, restart). S-01 ran them only on the earlier unpinned runc leg, not on crun or under the pin. Closes in T7.7.
  - A check of `cgroup.controllers` inside the container. It was not read for the crun rows. Closes in T7.7.
  - A run from the root-owned `supervisor` leaf on crun. Closes in T7.7.
  - musl. No S-03 result exists in `spikes/results/`. FR-080 and FR-081 must pass on glibc and musl (SRS release gate, section 9.3). The musl services run closes in T7.7, and release gate 1(b) closes in T10.6.
  - Arch and Fedora images. Only Debian 13 ran. Closes in T7.7, parametrised over the three images.

## Caveats

- One run per row. Boot and stop times are single samples.
- One host, one realm image (Debian stable, Debian 13.7, systemd 257.13).
- The spike used spike paths: a spike `XDG_RUNTIME_DIR` under `/run/user/1000` and leaves named `<realm>-launch` under `ginn/1000`. The product uses `/run/ginn/<uid>` (ADR-004) and the `supervisor` leaf.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-001 | The run script creates the subtree and the `supervisor` leaf, enables the controllers, and moves itself into the leaf as root, at each start of the service (`runit_system.py` steps 2 and 3). Host Setup captures their pre-images only (ADR-013). None of this runs per realm start. |
| FR-080 | The services-on argv gives systemd as PID 1, running with zero failed units on crun and runc (S-01). FR-080 AC-1 and AC-2 are not yet run under this argv on crun. |
| FR-081 | A services-on realm starts through its runit entry, so its podman calls run inside `ginn/<uid>/supervisor`. |
| FR-083 | Services flags are create-time flags. `ginn services on\|off` recreates the container as a new generation, so the toggle applies at the next start of that generation. Whether installed packages survive depends on the manifest replay (FR-021, T8.3), which is not measured. |
| NFR-003 | The memory budget for this argv is in ADR-007. The NFR-003 runs used the same flags, except that they omitted `--stop-timeout 30`. |
| R-001 | S-01 is the early spike. The run script's delegation (step 2) and its supervisor leaf (step 3) close the placement gap. R-001 stays open for musl, Arch, Fedora and the open FR-080 checks. |

## Amendment 2026-10-14: per-realm service contract

This amendment decides what runit runs for one services-on realm. It keeps the argv and the cgroup
placement above, and it adds the supervision contract. `services/runit_user.py` and
`services/supervise.py` render and check it, and `ops/service.py` runs it (T7.3).

### Decision

1. **Service directory.** Each services-on realm has `<SV_DIR>/<realm>/` under the user's runit
   supervisor, where `SV_DIR` is `$XDG_STATE_HOME/ginn/sv` (`runit_system.py`). It holds `run`,
   `finish` and `down`. The directory is built as `<SV_DIR>/.<realm>.tmp` and renamed into place once
   all three are in it. runsvdir skips dotted names, so it never starts a half-made service (FR-081).
2. **`run`** is `exec '<ginn>' _service-run '<realm>'`, with `<ginn>` the absolute ginn command. The
   hidden command runs `start_service` (ops/service.py): it checks that ginn-wait is executable and
   podman is absolute, then starts the realm through `start_realm(supervised=True)`. That start
   removes the stale conmon pidfile only on the path that calls `podman start`. It attaches the
   grants and writes `running`, in the order the services-off start uses (ADR-008 amendment). The
   process then execs `ginn-wait <pidfile> <podman> <global flags> -- <container>`, so runsv
   supervises ginn-wait (NFR-033). The pidfile is `<runtime>/services/<realm>.conmon.pid`, and
   `--conmon-pidfile` on the services-on create argv names the same path.
3. **`finish`** is `exec '<ginn>' _service-finish '<realm>' "$1" "$2"`, where runsv passes the exit
   code of run, or -1, and the signal that ended run (runsv(8)). Exit 0 is a stop request: the realm
   moves to `stopped` under the realm lock, the Agent Proxy filter and the D-Bus proxy release, and
   the crash log is cleared. Any other end is a crash, and it is counted in
   `<state>/services/<realm>.crashes`, which keeps the last five times (AS-028).
4. **Restart and give-up.** A crash that is not the fifth within 60 s waits 5 s, and runsv restarts
   run inside the 10 s of NFR-033 (AS-028). The fifth crash within 60 s gives the realm up. The
   finish script writes the `down` file, writes the byte `d` to runsv's control FIFO, and removes
   the crash log. Then a running realm moves to `failed` (DR-020, running to failed), and the audit
   entry `service-exit` records the give-up. A stopped realm stays stopped, because DR-020 has no
   stopped-to-failed move, and the audit entry says so.
5. **`down`** means runit does not start the realm when runsv starts. Autostart is off by default
   (DR-012). T7.5 toggles the file. The create writes it, so a new realm is not started by runit.
6. **Create and delete.** A services-on create writes the service directory after the container
   exists, and its rollback removes it. Delete removes the directory, the conmon pidfile and the
   crash log as the `service-removed` step, after the containers are gone. A running services-on
   realm is still refused until T7.4 stops it through `sv`.
7. **ADR-007 forms.** Mitigations a and b are coded as create-time forms, behind
   `SERVICES_MITIGATIONS`, which is `False` until S-05s passes or CL-10 is decided. With the flag on,
   the argv binds the ginn-owned journald drop-in read-only at `/etc/systemd/journald.conf.d/ginn.conf`
   and binds `/dev/null` read-only over each of the ten units in `spikes/s05/masks-base.txt`, and over `systemd-logind.service` (ADR-007 D8), under
   `/etc/systemd/system`. Mitigation d (`SYSTEMD_LOG_LEVEL=warning`) stays on by default. No file is
   written into the realm's `/etc` (CR-007).

### Deviation from the brief: runsv and the `down` file

runsv(8) reads `down` when runsv starts, not before each restart: a running runsv keeps its own
state. A `down` file alone therefore does not stop the restart after the fifth crash. The give-up
also writes `d` to `<service>/supervise/control`, which is what `sv down` does, with a non-blocking
open that ignores ENXIO (no runsv reading). runsv handles `d` while finish runs, and only signals
the service process, so finish is not signalled. Whether runsv has processed `d` before it restarts
run is not measured. T7.7 measures it on a host.

### Alternatives considered

- Give-up by `down` alone. Rejected, as above: runsv keeps restarting.
- Give-up by `sv down` from finish. Rejected: `sv` waits for the service to stop, and the service is
  in its finish phase, so the wait would run to the timeout. The control byte does the same without
  the wait.
- Crash counting under the realm lock. Rejected: a busy realm or an unacknowledged host change
  (GINN-E-HOST-UNACKED) would stop the give-up, and the restart loop would not end. The crash log
  and the `down` file are the supervisor's own files, so they are written without the lock. Only the
  record move goes through `mutating()`.
- Mitigations a and b on by default. Rejected until S-05s. The S-05r median margin is thin, and the
  create-time form is unmeasured (ADR-007).

### Consequences

- A services-on realm starts only through its runit service. `ginn start` still refuses it.
- The runit environment is sparse (`setpriv --reset-env`). A supervised start sees no DISPLAY,
  WAYLAND_DISPLAY, SSH_AUTH_SOCK or D-Bus address, so its session grants skip, and the lazy attach at
  enter re-attaches them (ADR-008 amendment). The services path is therefore not a full desktop
  start. T7.4 owns the rest.
- The crash log and the `down` file are not realm state, so a busy lock can delay the realm move but
  not the restart. A give-up while the lock is busy leaves the record `running`, and `ginn status`
  shows a state mismatch until the next start.
- Every path written is an Owned Path (DR-010): `<SV_DIR>/<realm>`, `<state>/services`,
  `<runtime>/services` and `<data>/services` for the drop-in.

### Requirement IDs

| ID | How this amendment bears on it |
|---|---|
| FR-081 | `run` starts the realm through `start_service` and execs ginn-wait; `finish` records each end of run. AC-1 (runit status shows the realm up) and AC-2 (restart within 10 s) are host proofs in T7.7. AC-3 (five crashes in 60 s leave the realm stopped and `failed`) is unit-tested in tests/unit/test_ops_service.py, with the `down` file and the runsv control byte. |
| AS-028 | The crash window is `note_crash` (services/supervise.py): five crashes within 60 s, the restart delay of 5 s, and the last five times kept. |
| NFR-033 | The 5 s delay and runsv's restart keep the restart inside 10 s, which the host gate measures. The give-up path is unit-tested. |
| FR-084 | The SRS text of FR-084 is autostart (Should). The `down` file is autostart off (DR-012), and T7.5 owns the toggle. The grant attachment on every service start is the ADR-008 amendment, which `start_realm` runs for a supervised start too. |
| NFR-003 | The ADR-007 forms a and b, behind `SERVICES_MITIGATIONS`. Their memory effect is measured in S-05s and T7.7. |
| CR-007 | The mitigations are create-time mounts and environment. ginn writes no file into a realm's `/etc`. |
| DR-010 | Every path listed above is an Owned Path, and each write goes through an Owned Path helper. |

## Amendment 2026-10-14: start, stop, delete and autostart through sv

This amendment decides how the CLI and the supervised process share the work of a services-on
realm (T7.4, T7.5). It keeps the argv, the cgroup placement and the service contract above. The
code is `services/supervisor.py` (the sv control), `services/autostart.py` (the `down` file),
`ops/lifecycle.py` (`_start_supervised`, `_stop_supervised`), `ops/delete.py` (`_stop_service_first`)
and `ops/service.py` (`set_autostart`, `set_services_mode`).

### Decision

1. **The CLI requests; the supervised process records.** `ginn start` and enter's auto-start of a
   services-on realm run `sv up` and then wait, and `ginn stop` runs `sv down` and waits. The
   supervised start (`ginn _service-run`) and the finish script (`ginn _service-finish`) take the realm
   lock through `mutating()`, journal the start or the stop, and write the audit entry. The CLI process
   writes no journal or audit entry for these. DR-032 holds, because the change that is audited is made
   by the choke point in the supervised process.
2. **The realm lock is free during the wait.** The supervised start needs the lock to record `running`,
   so a CLI that held it while it waited would make the start fail with GINN-E-REALM-BUSY. That failure
   would count as a crash toward the give-up. A test takes the lock from inside the wait
   (tests/unit/test_ops_lifecycle.py, `test_the_wait_for_a_services_on_start_holds_no_realm_lock`).
3. **Waits are bounded.** `sv up` first waits up to 10 s for runsv to create `supervise/ok`, because
   runsvdir scans its directory about every 5 s. The start then waits up to 60 s for the record to read
   `running`. The wait ends at once when `sv status` reports the service `down` without `want up`, because
   runsv then gave the realm up (AS-028). A `down` with `want up` is a crash restart, and the wait goes on.
   A status that cannot be read is unknown, and the wait goes on to its deadline. The error is
   GINN-E-SERVICE-TIMEOUT. `sv down` waits up to 60 s, which covers ginn-wait's podman stop of 30 s.
4. **A stop of a failed realm is recorded here.** After a give-up the service is already down, so no
   finish script runs and nothing writes `stopped`. DR-020 allows failed to stopped. So `ginn stop`
   stops a record that is not `stopped` after `sv down`, under the lock, with podman, as a services-off
   realm is stopped.
5. **Delete stops runit first, outside the locks.** `ginn delete` runs `sv down` before it takes the
   image and realm locks, because the supervised process needs the realm lock to record `stopped`. It
   keeps the journal step order (`stopped`, `container-removed`, `service-removed`), which recovery reads.
   The brief's order (directory before container) was considered. It is not needed for safety: a runsv
   that is down does not restart a removed container. A realm that no runsv supervises, and a runsv that
   is proven gone (GINN-E-SUPERVISOR-DOWN, decision 10), go to podman as before. Any other sv failure and
   a timeout are errors before any write.
6. **Session grants attach at enter.** A supervised start runs under runit's sparse environment
   (`setpriv --reset-env`), so it has no display, audio, SSH agent, D-Bus or user locale in its
   environment. It attaches the grants whose sources that environment holds, and it records them. An
   enter or exec of the running realm then attaches the rest through the existing re-attach path,
   under the realm lock (ADR-008 amendment). `_settle` makes that check after a services-on start. A
   test covers it (tests/unit/test_runtime_enter.py,
   `test_enter_starts_a_stopped_services_on_realm_through_sv_then_attaches_the_session_grants`). So
   `ginn start` returns with the grants that the sparse start made, and the next enter adds the others.
7. **Autostart is the `down` file.** Autostart on removes the realm's `down` file, and off creates it
   atomically. runsv reads `down` only when it starts (runsv(8)), so the file is what runit uses at the
   user's supervisor start (FR-084 AC-1). `ginn autostart` writes the file first and the record second,
   under the realm lock. A services-off realm is refused by the DR-012 rule in `RealmSettings` before any
   write. A supervised start applies the record's flag to the file under the lock before it starts. So a
   give-up, which writes `down` (ADR-003 per-realm service contract, decision 4), is undone by the next
   start. Between a give-up and that start, runit does not start the realm at boot.
8. **Status.** `ginn status` shows `supervision`, which is the `sv status` state with its pid and
   seconds, read without a lock and never raising. A supervisor that cannot answer is shown as
   `unavailable (<code>)` beside the record. After a give-up the record reads `failed` (FR-081 AC-3).
9. **Services mode is refused until T8.3** (superseded by the 2026-10-14 services-change amendment).
   `ginn services <realm> on|off` returns the record when the
   mode is the one the realm has. A change raises GINN-E-NOT-SUPPORTED-YET, naming T8.3, before any
   write, because the mode is fixed in the container, and a container's options change only
   with a new generation (ADR-006, the create-time toggles of ops/permissions.py).
10. **Errors.** GINN-E-SUPERVISOR-DOWN (exit 1) only when runsv is proven not to run the directory: the
    runtime directory is missing, no runsv picks up the directory within 10 s, or sv prints `runsv not
    running`, `unable to open supervise/ok` or `unable to change to service directory`. Only that answer
    lets stop and delete fall back to podman, because nothing restarts the realm then. GINN-E-SERVICE-CONTROL
    (exit 1) is every other sv failure, including sv that is not installed or cannot run, and an
    `unable to open supervise/control` while runsv may still run. A caller must not treat it as gone.
    GINN-E-SERVICE-TIMEOUT (exit 1) when an sv wait ends, when a start does not run within 60 s, or when
    runsv gave the realm up. GINN-E-SERVICE-MISSING (exit 1) when the realm's service directory is gone.
11. **Refusals before any sv call.** A services-on start, stop or delete is refused before `sv` runs when
    its state is not one that the operation allows (GINN-E-REALM-STATE): for a start, stopped, failed or
    running (a running realm whose container died is a crash, which the supervised start records); for a
    stop, running, failed or stopped; for a delete, the states of `_DELETE_FROM`. The host check (FR-006),
    an unrecovered journal entry (FR-122) and the realm lock are checked the same way, so a refusal does
    not leave the record `running` with its container stopped. The locked checks still run after the sv
    call. Autostart refuses a realm that is being created, deleted or broken (GINN-E-REALM-STATE) before
    any write, and a missing service directory is GINN-E-SERVICE-MISSING.

### Alternatives considered

- The CLI holds the realm lock while it waits. Rejected: the supervised start needs the lock to record
  `running`, so the wait would time out and the start would count as a crash (decision 2).
- The CLI writes `running` after `sv up` succeeds. Rejected: the record would claim a container that the
  supervised start has not started. ADR-003's supervisor-launch rule keeps the start in the supervisor.
- The supervised start takes the session grants from the CLI's environment. Rejected: runit sets the
  environment of the run script (`setpriv --reset-env`), and a restart after a crash gets that same
  environment, so the CLI's values never reach it.
- Delete removes the directory before the container. Rejected: it changes the journal's step order, which
  recovery reads, and the safety it would add is already given by `sv down`.

### Consequences

- A services-on realm has two writers of its state: the CLI, for the failed-realm stop and for the
  services-off paths, and the supervised process. Both take the realm lock for the record.
- `ginn start` returns once the record reads `running`. The FR-006 warnings of the supervised start go to
  the runit log through `ginn _service-run`, not to the start command.
- Every path this amendment writes is an Owned Path (DR-010). The `down` file is in the realm's service
  directory, which is under the state root.
- The output forms of `sv` in the code (`run:`, `down:`, `ok:`, `timeout:`, `fail:`, `warning:`) were read
  from runit's sv and runsv in a scratch directory, as a user, with no root and no podman. Unit tests use
  a fake sv that prints the same forms. The host proof of FR-081 AC-1 and AC-2 is T7.7.

### Requirement IDs

| ID | How this amendment bears on it |
|---|---|
| FR-081 | Start and stop go through `sv`; `ginn status` shows the runit state; AC-3 is the record `failed` after the give-up. AC-1 and AC-2 are host proofs in T7.7. |
| FR-083 | `ginn services` returns the record for the same mode and refuses a change until T8.3 (decision 9; superseded by the 2026-10-14 services-change amendment). |
| FR-084 | Autostart is the `down` file, applied at each supervised start under the lock (decision 7). AC-1 is the host proof in T7.7. |
| DR-012 | The flag needs services on, checked by the settings rule before any write (decision 7). |
| DR-020 | A stop of a failed realm uses failed to stopped (decision 4). |
| DR-032 | The supervised process journals and audits the start and stop, and the CLI writes nothing for them (decision 1). |
| AS-028 | The give-up ends the start wait at once (decision 3). |
| NFR-033 | The start and the stop are bounded by the waits in decision 3. |

## Amendment 2026-10-14: a services change is a rebuild (T8.3)

This amendment decides how `ginn services <realm> on|off` takes effect, and how a services-on
rebuild replays the Install Manifest. It supersedes decision 9 of the start, stop, delete and
autostart amendment, and only as far as `services on|off` now rebuilds: the change is no longer
refused. Decision 2 of that amendment has one exception, decision 5 below. Nothing else above
changes. The code is `ops/service.py` (`set_services_mode`, `start_service`, `finish_service`),
`ops/rebuild.py`, `ops/lifecycle.py` (`start_through_sv`, `stop_through_sv`), `ops/recovery.py`
(`_decide_rebuild`) and `store/records.py` (`set_state`).

### Decision

1. **The change is recorded, and the next start applies it.** `ginn services` sets the desired mode in
   the record, under the realm lock, and turning services off also sets autostart off and writes the
   realm's `down` file while its container is supervised. A running realm gets `restart_required`
   (FR-083, FR-066). The start that follows rebuilds the realm into a container of the new mode
   (ops/rebuild.py, `rebuild_for_start`), and only then starts it. The Realm Home and the Install
   Manifest are kept, so the packages come back in the new generation (FR-021).
2. **The container's mode is the record's `services_at_create`.** It is the mode the container was made
   with, and the record keeps it apart from the desired `settings.services`. A stop, the idle watcher
   and the choice between `sv` and podman follow it, so a running container that the user has asked to
   change is still stopped through its own supervisor. A realm without a record field reads the mode of
   its settings, which no realm could change before this amendment.
3. **A services-on rebuild replays through runit. The CLI never starts a services-on container.** gN+1
   is started by `sv up`, which makes runsv start `ginn _service-run`, and that process starts the
   building container with podman. The rebuild waits for the container to run, replays the manifest with
   `podman exec` into it, stops it with `sv down` and a wait, and then swaps. No CLI process runs
   `podman start` or `podman run` for a services-on container. Context line 30 and Decision line 45 of
   this ADR say so, and S-01 measured the reason: a `podman start` from the session shell fails with
   `permission denied` on the cgroup (`spikes/results/S-01.md`, line 37), and a start from the moved
   launcher leaf works (line 48). The placement of exec'd processes is not verified (line 37, and the
   T4.6 check). A services-off gN+1 is started and stopped with podman, as before, because no runit
   service runs it. Amended 2026-10-14 (ADR-014): the exec half of the S-01 line 37 claim was measured from a shell that a root write had already moved, so it does not show session placement. The start half was measured from the unmoved session shell and stands. The replay's `podman exec` is therefore routed through the supervisor relay, not the user session (ADR-014, Decision 1; T7.10 builds it; gate G5 checks it on the host).
4. **The building container is recorded before runit starts it.** While the record reads rebuilding, two
   fields name the container being built, `building_container_id` and `building_generation` (DR-034).
   The rebuild writes them after gN+1 is created, and before the runit directory is written and before
   `sv up`. That is a second write of the record in REBUILDING. It is not the swap: the record still names
   gN and its container, and only the two fields are new. A supervised start of a rebuilding record starts
   `building_container_id` when the settings say services on, and otherwise it refuses (GINN-E-INTERNAL,
   before podman runs). `set_state` clears both fields when the record leaves rebuilding, and the swap
   clears them too, so a rollback and a recovery leave no container named.
5. **The supervised start of a rebuild takes no lock and writes nothing.** The rebuild holds the realm
   lock while its `sv up` runs `_service-run`, and `_service-run` needs that lock to record a start. So,
   while the record reads rebuilding with a building container, `_service-run` takes no lock, journals
   nothing and writes no record. It runs `podman start` and the pid check, and it returns the ginn-wait
   argv, as a normal start does. This is the one exception to decision 2 of the start amendment
   (tests/unit/test_ops_service.py, `test_a_supervised_start_during_a_rebuild_starts_the_building_container_and_writes_nothing`).
   That start also requires a live `rebuild` journal entry for the realm (`JournalFile.is_live`, FR-122):
   a rebuild killed before recovery leaves a dead entry, so runsv's restart of the service starts no
   container (tests/unit/test_ops_service.py, `test_a_runsv_start_after_a_killed_rebuild_starts_nothing_before_recovery`).
   The finish script (`_service-finish`) writes no record while the record reads rebuilding. A stop
   and a give-up change only the supervisor's own files: the crash log, the `down` file of a give-up,
   and the runsv control byte. The rebuild, its rollback or recovery settles the record. A lock held
   by `rebuild` is passed over like a lock held by `stop` or `delete` (`_STOP_HOLDERS`), so the stop
   that the rebuild's `sv down` causes writes nothing.
6. **Rollback and recovery tell runsv to stop first.** A services-on gN+1 is stopped with `sv down`,
   which waits for its finish script, before its container and its runit directory are removed. The
   rollback of that stop is registered before `sv up`, so it runs first (LIFO). Recovery of an interrupted
   rebuild writes runsv's `d` control byte (`signal_runsv_down`, which does not wait) before it removes the
   orphan, so runsv does not restart a container that is being removed. Recovery never waits on runit,
   because the process that runs recovery can be a runit child (`_service-run` calls `settle_interrupted`).
7. **Create still refuses `--with` and the Kali `--everything` on a services-on realm.** The CLI cannot
   start a services-on container (decision 3), so `ginn create --services on --with PKG` and
   `--everything` are refused with GINN-E-NOT-SUPPORTED-YET before any write. The packages can be
   installed after the first supervised start, with `ginn install`. The rebuild is exempt, because its
   replay is decision 3 (`_validate(..., replay=True)`).

8. **A lazy rebuild that fails starts the old container, and that container's mode picks the path.** A
   start of a stopped realm whose record asks for a create-time change runs `rebuild_for_start` first
   (decision 1). If the rebuild fails with an error, the start goes on with the old root and
   GINN-W-REBUILD-DEFERRED, and the change stays pending (`restart_required`). The old container's mode,
   `services_at_create`, picks `sv` or podman, so a deferred services-off-to-on change starts through
   podman, and a deferred on-to-off change starts through its runit service. A cancel (GINN-E-INTERRUPTED)
   stops the start instead. A failed realm with a pending change is stopped first, which records it
   stopped, and then rebuilt. A rebuild's own restart never rebuilds again (`rebuild=False`).
### Rationale

The replay has to run inside a started container, and that container must be started by its runit
service, so that it sits in the supervised cgroup (ADR-003 Decision). A rebuild that keeps the
container's start in the supervisor, and the record writes in the rebuild, keeps both rules. The
building container is named by the record while it is built, so `_service-run` can find it without a
lock. Keeping the create-time state in the record and the rebuild in the start is the same rule as
ADR-006, applied to services: the change is one container generation, and the packages are carried
across it (FR-021).

### Consequences

- `ops/delete.py` dispatches its `sv down` on `services_at_create`, the container's mode, and not on the
  desired mode. So a delete of a realm whose services change is pending stops the container through its
  own supervisor. The earlier Consequences bullet of this amendment that said otherwise was stale, and
  it is replaced by this one.
- A services-on realm rebuilt while stopped keeps its runit directory as it was. Only the swap to
  services off removes it.
- A killed rebuild whose runsv restarts the building container before recovery runs starts nothing: the
  live-journal check of decision 5 refuses it, and that refusal is tested on FakePodman. Its host proof,
  and that of FR-083 AC-1 (htop stays installed and systemd is PID 1 after the toggle on), is T7.7 and
  the M8 host gate. The services-on replay is not measured yet.
- A stopped realm that a rebuild changed is started once after the swap, synced, and stopped again, so
  its exported entries are those of gN+1 (FR-021 AC-1). Its runit directory is started and stopped with
  it, and it ends as it was.
- The fault matrix (tests/unit/test_ops_rebuild_faults.py) runs each step of a services-off and of a
  services-on rebuild, with a fake runit, and its kill cases are settled by recovery.

### Requirement IDs

| ID | How this amendment bears on it |
|---|---|
| FR-021 | A services-on rebuild keeps the Realm Home and the manifest, and it replays the packages into gN+1 through runit (decisions 1, 3, 5). |
| FR-066 | A tier, permission or services change is recorded, and a running realm is flagged until its next start (decision 1). |
| FR-081 | A services-on realm is started and stopped through `sv` around the swap, and the CLI starts none (decisions 3, 6). |
| FR-083 | `ginn services on|off` takes effect at the next start. The packages and the Realm Home are unchanged (decisions 1 to 3). |
| DR-012 | Turning services off sets autostart off, and writes `down` for a supervised container (decision 1). |
| DR-020 | A rebuild's record moves to rebuilding and back to its previous state, and a finish writes nothing while it reads rebuilding (decisions 4, 5). |
| DR-034 | The rebuild writes the building fields before the start and the record at the swap. Its runit directory is written before the start and removed after the swap (decisions 4, 3). |
| AS-028 | A supervised start of a building container that runsv gives up is a wait timeout, and the rebuild rolls back (decision 5). |
| NFR-031 | The fault matrix covers the services-on path, and a fault at any step leaves gN usable (decisions 6 and 8). |
| FR-122 | The building container starts only while its rebuild's journal entry is live (decision 5). |
