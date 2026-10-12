# ADR-003: Services-mode cgroup strategy

- Status: Accepted
- Date: 2026-10-12

## Context

A services-on realm runs systemd as its init, so systemd needs cgroup v2 controllers under the realm (FR-080). A rootless container on a runit host may get no delegation. That is risk R-001. Host Setup (FR-001) is the only step that runs as root. This ADR decides how a services-on realm gets a delegated cgroup without a root write at each start.

Ranked characteristics (SRS section 4), in order: performance efficiency, host integrity and isolation, recoverability, interoperability, usability. Host integrity and isolation settle the placement. Root writes happen in Host Setup, and again in the supervisor's run script at each start of that service (at boot, or after a restart by runsv). They never happen per realm start. Otherwise ginn writes only inside the user's own subtree. Interoperability (systemd runs as PID 1) settles the argv. Performance efficiency is decided in ADR-007.

Measured on the reference host (Void Linux glibc, kernel 7.2.9_1, podman 5.8.8 with the cgroupfs manager, rootless). Source: `spikes/results/S-01.md`, crun leg, roadmap argv rows and their matched rows:

| Runtime | Flavor | `is-system-running` | Failed units | Boot to running (s) | Stop (s) | Stop exit / OOM |
|---|---|---|---|---|---|---|
| crun 1.30.1 | roadmap argv (pasta networking) | running | 0 | 0.4 | 0.2 | 0 / false |
| crun 1.30.1 | `--userns keep-id --network host` | running | 0 | 1.3 | 0.2 | 0 / false |
| runc 1.5.2 | roadmap argv (pinned) | running | 0 | 0.5 | 0.3 | 0 / false |
| runc 1.5.2 | `--userns keep-id --network host` (pinned) | running | 0 | 0.3 | 0.3 | 0 / false |

In every row, PID 1 sits under `/ginn/1000/<realm>/libpod-*/init.scope`. Each row ran once.

The placement must hold. The process that starts podman must already sit inside `/sys/fs/cgroup/ginn/<uid>/`. A session process cannot move itself there. A move needs write access to `cgroup.procs` of the common ancestor, which here is the cgroup root, and that file is root-owned. S-01 and S-05r simulated the product placement with logged root writes of the launcher pid. S-01's crun leg made two, one per runtime leg. S-05r made one. In the product, the Host Setup run script does this write. `src/ginn/hostsetup/runit_system.py` (step 3) writes the script's own pid into `/sys/fs/cgroup/ginn/<uid>/supervisor/cgroup.procs` as root. Then it runs `exec setpriv ... runsvdir`. `exec` keeps the pid and the cgroup. Every services-on realm starts from a descendant of runsvdir, so it already sits in the subtree.

## Decision

ginn starts a services-on realm with these podman options, added to the realm's normal create argv:

```
--systemd=always --cgroupns private --cgroup-parent /ginn/<uid>/<realm> --user 0:0 \
--stop-signal SIGRTMIN+3 --stop-timeout 30 --runtime crun
```

`/ginn/<uid>/<realm>` is relative to `/sys/fs/cgroup`. This is the set S-01 ran for the crun roadmap row. S-01 also added `-d`, `--name`, the image and `/sbin/init`. The realm's own options stay in place. For non-Kali realms that means `--userns=keep-id:uid=<uid>,gid=<gid>` and `--network host`. S-01 ran both flavors, the bare set with pasta networking and the set with `--userns keep-id --network host`. Both reach `running` with zero failed units on crun and on runc.

ginn does not add `--cgroups=split`. Podman 5.8.8 rejects it together with `--cgroup-parent`.

ginn starts a services-on realm only through its runit supervisor entry (`sv`, FR-081). The `ginn` CLI process does not run `podman run` or `podman start` for a services-on realm. Realm processes sit under `/ginn/<uid>/<realm>` only when a descendant of the supervisor starts them.

Host Setup creates `/sys/fs/cgroup/ginn/<uid>/supervisor`, and the run script recreates it at each start. The chown covers only the directories and the `cgroup.procs`, `cgroup.threads` and `cgroup.subtree_control` files to the user. Limit files such as `memory.max` stay root-owned. On this host, Host Setup also adds `pids` to the root `cgroup.subtree_control`. Undo restores the recorded value and does not assume the original one (T3.5).

## Alternatives considered

- No `--cgroup-parent`, so podman uses the session scope. Rejected. Podman cannot create `/sys/fs/cgroup/conmon` (permission denied). systemd exits 255 with `Failed to mount cgroup v1 hierarchy` before any unit starts (S-01 variant e).
- `--cgroups=split` with `--cgroup-parent`. Rejected. Podman 5.8.8 refuses the pair (S-01 variant b).
- `--cgroups=no-conmon` with `--cgroup-parent`. Not chosen. It passed on runc (unpinned, S-01 variant c) and kept conmon in the launcher's leaf. The default cgroups mode passes on both runtimes, so the extra flag gains nothing.
- runc as the services-on runtime. Not chosen as the default. runc 1.5.2 also passed the same argv under the pin. crun 1.30.1 is chosen because the NFR-003 memory measurements in ADR-007 ran on crun. This is a choice of evidence, not a runtime fault.
- Rootful podman, or a root step on every start. Rejected. Realms are always rootless (CR-003, OOS-3, OOS-10). After Host Setup, ginn writes only to Owned Paths (DR-010, CR-006). A root write per start breaks that rule.

## Consequences

- The services-on argv gives systemd as PID 1 and reaches `running` with zero failed units on both runtimes. Root writes happen at Host Setup and at each start of the supervisor service, never per realm start.
- The services-on realm must start through `sv` (T7.4). The CLI never starts it directly.
- The services-on create argv keeps the flags above, with no `--cgroups=split`.
- Host Setup keeps the chown limited to the paths listed above.
- `ginn exec` placement is not verified. S-01 saw `exec`, `stop` and `top` succeed from the session scope, but it did not check where exec'd processes land. ginn does not rely on that placement.
- T3.3 already has the placement step (run script, step 3). T7.1 carries the argv and T7.4 carries the supervisor start. T7.7 runs the test.
- Stale text: the `cgroups.py` module docstring says routing for services-on realms is designed in T3.4 and M7. This ADR puts it in T7.1 and T7.4. The code is not changed here.
- Packaging: the Void template depends on `crun` (`packaging/void/srcpkgs/ginn/template`). Roadmap T0.6 still lists `runc`. Align T0.6 with the template as a follow-up.
- R-001 stays open. S-01 is the early spike that R-001 asks for. The items still open are:
  - FR-080 AC-1 and AC-2 (cron install, restart). S-01 ran them only on the earlier unpinned runc leg, not on crun or under the pin.
  - A check of `cgroup.controllers` inside the container. It was not read for the crun rows.
  - musl. No S-03 result exists in `spikes/results/`. FR-080 and FR-081 must pass on glibc and musl (SRS release gate, section 9.3).
  - Arch and Fedora images. Only Debian 13 ran.

## Caveats

- One run per row. Boot and stop times are single samples.
- One host, one realm image (Debian stable, Debian 13.7, systemd 257.13).
- The spike used spike paths: a spike `XDG_RUNTIME_DIR` under `/run/user/1000` and leaves named `<realm>-launch` under `ginn/1000`. The product uses `/run/ginn/<uid>` (ADR-004) and the `supervisor` leaf.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-001 | Host Setup creates the subtree and the `supervisor` leaf. The run script moves itself into the leaf as root at each start of the service (`runit_system.py` step 3). Neither runs per realm start. |
| FR-080 | The services-on argv gives systemd as PID 1, running with zero failed units on crun and runc (S-01). FR-080 AC-1 and AC-2 are not yet run under this argv on crun. |
| FR-081 | A services-on realm starts through its runit entry, so its podman calls run inside `ginn/<uid>/supervisor`. |
| FR-083 | The services mode picks the argv at start, so a toggle applies at the next start. |
| NFR-003 | The memory budget for this argv is in ADR-007. The NFR-003 runs used the same flags, except that they omitted `--stop-timeout 30`. |
| R-001 | S-01 is the early spike. Host Setup delegation and the supervisor leaf close the placement gap. R-001 stays open for musl, Arch, Fedora and the open FR-080 checks. |
