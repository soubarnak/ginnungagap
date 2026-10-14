# ADR-003: Services-mode cgroup strategy

- Status: Accepted, S-01 on runc and crun (the roadmap argv ran under the pin on both). FR-080 AC-1/AC-2 on crun, and a run from the root-owned `supervisor` leaf on crun, are pending T7.7.
- Date: 2026-10-12
- Amended: 2026-10-12 (D5). The keep-id form, the chown scope (S-01 step 6) and the FR-083 generation wording are corrected. No decision changes.
- Amended: 2026-10-12 (D6). The chown scope and the leaf ownership are stated in full, with the evidence for a root-owned leaf and the reason the user does not need write access to it. No decision changes. F6a changes the code to match.
- Amended: 2026-10-12 (D7). Status qualifier added. The root-owned leaf is evidenced by the unpinned runc leg only, the order of the root writes is stated, and the stale `cgroups.py` note is removed. No decision changes.
- Amended: 2026-10-14 (per-realm service contract). The runit service directory, the run and finish scripts, the crash rule and the create-time forms of ADR-007 a, b and d are decided at the end of this file. No decision above changes.
- Amended: 2026-10-12 (D9). Host Setup does not make the root writes under the subtree. The supervisor run script makes them at each start of its service, and Host Setup only captures their pre-images (ADR-013). The Context, the placement paragraph of the Decision, the Consequences and the FR-001 and R-001 rows said otherwise, and they are corrected. Undo follows ADR-013 Decision 15, not "restores the recorded value". No decision changes. Each statement was confirmed against the render template in `src/ginn/hostsetup/runit_system.py`, which is the state at the time of this amendment.

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

ginn starts a services-on realm only through its runit supervisor entry (`sv`, FR-081). The `ginn` CLI process does not run `podman run` or `podman start` for a services-on realm. Realm processes sit under `/ginn/<uid>/<realm>` only when a descendant of the supervisor starts them.

The run script does every root write under the subtree, at each start of the service. Host Setup writes none of them, and it only captures their pre-images (ADR-013, Decisions 1 and 12). The rendered script (`runit_system.py`, `_RUN`) does, in this order:

1. Step 1 creates `/run/ginn/<uid>` (`mkdir -p`), sets it to the user's uid and gid, and sets mode 0700 (ADR-004).
2. Step 2 enables, on the root `/sys/fs/cgroup`, each of `cpu`, `cpuset`, `io`, `memory` and `pids` that the node offers and does not yet have in `cgroup.subtree_control`. A failure there is logged, and the script continues. It then creates `ginn` (`mkdir -p`), enables the same controllers there, and exits 1 on failure. It creates `ginn/<uid>/supervisor` (`mkdir -p`, which also creates `ginn/<uid>`), enables the controllers on `ginn/<uid>`, and exits 1 on failure. Last, it chowns `ginn/<uid>` and its `cgroup.procs`, `cgroup.threads` and `cgroup.subtree_control` to the user (`chown_groups`, S-01 step 6). The supervisor leaf is not chowned.
3. Step 3 writes the script's own pid into `ginn/<uid>/supervisor/cgroup.procs` as root, before it drops privileges to the user.

The `supervisor` leaf stays root-owned, and so does everything else under the subtree, so limit files such as `memory.max` stay root-owned too. Migrating a process out of the leaf needs write access on the common ancestor's `cgroup.procs`, which is `/sys/fs/cgroup/ginn/<uid>`, not on the leaf (a cgroup v2 rule; not measured here). The user therefore needs no write access to the leaf. The root-owned leaf is evidenced by the unpinned runc leg only. `spikes/s01_systemd_rootless.sh:144` creates the launcher leaf as root, and the variant b note in the S-01 variant table records that leaf as root-owned in that setup. The pinned crun leg created its leaves as the user (`spikes/s01c_crun.sh:223`). The cgroup v2 rule does not depend on the runtime, so the risk is low, and T7.7 runs crun from the root-owned leaf. The order of the root writes does not matter: `spikes/results/S-01.md` (Root steps) lists the pid write before the chown, and the template does the chown in step 2 and the pid write in step 3. On the reference host, `pids` was the only controller that was new in the root `cgroup.subtree_control` (S-01 step 2). Undo follows ADR-013 Decision 15 (ii) and (v), not "restores the recorded value". It writes `-<ctrl>` for each controller that the pre-image lacks, and only in the same boot.

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
- `ginn exec` placement is not verified. S-01 saw `exec`, `stop` and `top` succeed from the session scope, but it did not check where exec'd processes land. ginn does not rely on that placement. The placement check is in T4.6.
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
   and binds `/dev/null` read-only over each of the ten units in `spikes/s05/masks-base.txt` under
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
