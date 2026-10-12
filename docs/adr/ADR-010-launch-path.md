# ADR-010: Launch path for a command in a running realm

- Status: Accepted, on runc. The crun re-measure is T4.9 (see Decision).
- Date: 2026-10-12
- Amended: 2026-10-12 (D6). Status is Accepted. The crun re-measure rule is added to the Decision, and the FR-082 wording is corrected. The runc caveat stands.
- Amended: 2026-10-12 (review of T4.2 and T4.3). The ADR-008 pre-check that runs before exec is one identity read per variable grant, plus a mount-table scan for each mount root. It must stay inside the NFR-004 300 ms budget, and T4.9 measures it. The launch path, the decision and the parity table do not change. Requirement IDs: NFR-004, NFR-001, FR-061.

## Context

NFR-004 (Must) sets p95 <= 300 ms from launcher invocation to app process start in the realm, measured over 100 launches of a trivial binary. FR-017 starts a stopped realm on use; that cold path is a `podman start` and is measured separately. FR-048 (`ginn enter`) and FR-049 (`ginn exec`, which returns the command's exit code) use the same exec mechanism. FR-046 launches exported apps through the launcher. R-006 records the risk that NFR-004 may be tight for runtime exec paths. Its mitigation is to measure in the first prototype and write this ADR. NFR-005 (cold start) was measured in the same spike.

Ranked characteristics (SRS section 4): performance efficiency comes first. NFR-004 is a threshold. Once a path meets 300 ms, a faster path gains nothing on performance. Host integrity and isolation comes second. The realm's seccomp filter, bounding set and supplementary groups are part of that isolation (FR-061, NFR-SEC-002). A launch path that drops them loses on the second characteristic, even when it is faster. ADR-002 already fixes the podman CLI as the interface, not the REST API (NFR-001).

Measured by S-02 (`spikes/results/S-02.md`) on the reference host:

- Host: Void Linux glibc, kernel 7.2.9_1, AMD Ryzen 5 7535HS. The host was noisy: load average 7.1 and io pressure near 50 % in run 1; load average 1.8 and io pressure near 46 % in run 2.
- podman 5.8.8 rootless, **runc 1.5.2**, cgroupfs cgroup manager, overlay storage. S-02 did not run crun. ADR-003 chose crun for services-on realms, so these launch numbers must be measured again on crun.
- Image `docker.io/library/alpine:3`, started as `run -d --userns keep-id --network host sleep infinity`. No `--init`, because catatonit was absent from the host (S-02 section 1).
- Method: 5 discarded warm-up calls, then 100 measured calls per row, nearest-rank percentiles, two full runs. The cold path used 20 cycles.

Wall-time results, milliseconds, run 1 / run 2 (S-02 section 4):

| Row | What it times | p50 | p95 | max |
|---|---|---|---|---|
| A | `podman exec s02 true` | 153.3 / 131.5 | 211.8 / 136.0 | 356.8 / 140.9 |
| B | `podman exec --user 1000 s02 true` | 135.5 / 133.5 | 206.5 / 140.8 | 225.2 / 145.6 |
| C | `s02_run` (C helper, setns), `true` | 1.1 / 1.6 | 1.7 / 1.8 | 2.0 / 2.0 |
| D | `nsenter ... true` | 1.2 / 1.7 | 1.8 / 2.1 | 1.9 / 2.2 |
| L1 | Python launcher, state file, `podman exec`, end to end | 162.8 / 163.0 | 186.1 / 167.7 | 272.3 / 176.8 |
| L2 | Python launcher, state file, `s02_run`, end to end | 32.1 / 32.5 | 36.5 / 34.2 | 46.1 / 35.3 |
| P4 | `PYTHONPATH=src python3 -m ginn --version`, the real CLI import | 69.3 / 71.0 | 72.7 / 74.5 | 76.6 / 84.2 |
| I | `podman inspect` for the PID | 26.7 / 26.9 | 39.8 / 30.4 | 47.0 / 32.6 |
| K | cold: `podman start` after `stop -t 0` (20 cycles) | 80.9 / 116.0 | 96.6 / 121.9 | 224.4 / 122.3 |

Summed p95 values, as S-02 reports them, are not the p95 of the sum. The measured end-to-end rows L1 and L2 are the better figures. The sum of the real CLI import and exec (P4 + A) was 284.4 ms in run 1 and 210.6 ms in run 2.

Parity: what the launched process inherits (S-02 section 6, captured from inside each runner):

| Field | `podman exec` | `s02_run` (C helper) | `nsenter` (no `-C`) |
|---|---|---|---|
| Seccomp | 2 (filter active) | 0 (no filter) | 0 (no filter) |
| Bounding set (CapBnd) | 0x00000000800405fb | 0x000001ffffffffff | 0x000001ffffffffff |
| Supplementary groups | 1000 | none | 14 x 65534 plus 1000 (unmapped host groups) |
| Uid, CapEff, NoNewPrivs | 1000, 0, 0 | 1000, 0, 0 | 1000, 0, 0 |
| user, mnt, pid, uts, ipc, net namespaces | same inode as container | same | same |
| cgroup namespace | joined | joined | not joined |

Gates. S-02 checked, before any timing, that `podman exec`, `s02_run` and `nsenter` each see the Alpine rootfs as uid 1000, and that `sh -c 'exit 7'` returns 7 (IR-011, FR-049 AC-1). A negative control, PID 1 (root-owned), fails with EACCES.

## Decision

ginn launches a command in a running realm with `podman exec`, from a lean Python launcher. The launcher:

1. reads the realm's state record: the container name, the host PID of the container init, and that process's start time;
2. checks the start time against `/proc/<pid>/stat` before use, and refuses to launch on a mismatch;
3. runs `podman exec <name> -- <cmd>` and returns the command's exit code (FR-049).

The launch path does not call `podman inspect`, because the state record holds the PID. It does not import the full `ginn` CLI, which adds about 70 ms at p95 (row P4). ginn does not build `ginn-run`, the C setns helper, for v1. On a stopped realm, FR-017 starts it first, through the cold path (row K), which is outside the NFR-004 budget.

Performance does not decide the choice, because NFR-004 is a threshold and `podman exec` meets it. Isolation decides between the two paths that pass it: the helper drops the container's seccomp filter, bounding set and groups (parity table), and `podman exec` keeps them.

The measurements above are on runc, and the base argv names crun (`docs/capabilities.md`). T4.9 re-measures `podman exec` p95 on crun. If it exceeds 300 ms, `ginn-run.c` returns to `native/`, and it must match the parity table before it ships (S-02 section 7).

## Alternatives considered

- `ginn-run`, the C setns helper (`s02_run`, row C). Faster: p95 1.7 to 1.8 ms. Rejected for v1. It runs without the container's seccomp filter, with the full bounding set and no supplementary groups. Those are the isolation properties that FR-061 and NFR-SEC-002 rely on, and ranked characteristic 2 decides against it. S-02 section 7 would revisit the helper only if `podman exec` misses 300 ms on target hardware, and then only after the helper matches the parity table.
- `nsenter` into the container (row D). The same parity gaps: no seccomp filter, unmapped host groups shown as 65534, and the cgroup namespace not joined. Rejected.
- A Python launcher that imports the full `ginn` CLI. Adds about 70 ms at p95 (row P4); the sum with exec reached 284.4 ms in the noisier run 1. Rejected. The margin is too thin, so the launcher stays lean.
- `podman inspect` on every launch to find the PID. Costs 30 to 40 ms at p95 (row I). Rejected. The PID is stored in the state record and checked against its start time.
- The REST API (`podman system service`). Excluded by ADR-002, because it needs a resident process (NFR-001). Not measured here.

## Consequences

- Positive: NFR-004 is met on the measured path in both runs (p95 136.0 ms and 211.8 ms). The launched process keeps the container's seccomp filter, bounding set, group set and namespaces (S-02 section 6).
- Margin: 88 to 164 ms, from two runs on one noisy host, on runc.
- The measured launcher is a spike script. The shipped launcher must keep its imports lean and must be measured again. T4.6 builds it. T4.9 carries the NFR-004 check (100 launches, p95 <= 300 ms), which the roadmap did not assign before this ADR.
- The socket pre-check of ADR-008 runs before exec on this path. It is one identity read per variable grant, plus a mount-table scan for each mount root (ADR-008, review amendment). S-02 did not measure that cost. T4.9 measures it, and the NFR-004 check in T4.9 covers the launch with the pre-check included.
- S-02 ran without `--init`, because catatonit was absent. FR-082 requires a minimal init process in services-off realms. ginn ships catatonit for it (ADR-007, roadmap T0.6). Without an init, PID 1 ignores signals it has no handler for. The cold path therefore stopped the container with `podman stop -t 0`, not with a signal.
- Cold start (FR-017, NFR-005): 20 cycles, not the 50 per distribution that NFR-005 requires. p95 96.6 to 121.9 ms, far inside 2 s. NFR-005 is not proven.
- A restarted realm gets a new init PID, so the state record must be rewritten on each start. S-02 tested only the start-time happy path. The stale-PID path was not tested.
- Not measured: crun (S-02 ran runc), musl (S-03 has no result), target hardware, the Kali pasta network (S-02 used `--network host`, so Kali realms will be slower, not measured), and `ginn enter`, which S-02 did not run.
- Follow-up tasks: T4.6 (the launcher and enter), T4.3 (environment injection at exec), T4.9 (the NFR-004 check), S-03 (musl), and a re-measure on crun and target hardware.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| NFR-004 | The 300 ms p95 target. S-02 measured `podman exec`: p95 136.0 ms (run 2) and 211.8 ms (run 1), 100 launches each, on runc. The decision rests on this. |
| R-006 | The risk this ADR answers. The measurement is for runc on one host, so the risk is not closed for crun or target hardware. |
| NFR-005 | Cold start was measured over 20 cycles, not the 50 that NFR-005 requires. The `podman start` path is row K. Partial. |
| FR-017 | Auto-start on use: a stopped realm starts through the cold path (`podman start`), which is outside the NFR-004 budget. Measured as row K. |
| FR-046 | Exported app launches run through this launcher. S-02 measured a trivial binary, not a GUI app. |
| FR-048 | `ginn enter` uses the same exec mechanism. S-02 did not run `ginn enter`. |
| FR-049 | `ginn exec` returns the command's exit code. S-02 checked `sh -c 'exit 7'` under `podman exec` before timing. |
| NFR-SEC-002 | The parity table: the helper path has no seccomp filter and a full bounding set; the exec path keeps both. This is why the helper is rejected. |
| FR-061 | The Basic and Development grants run inside the realm's seccomp filter, bounding set and groups. The exec path keeps all three (parity table), and the helper path loses them. |
| FR-082 | Services-off realms run a minimal init process. The S-02 runs had no init, because catatonit was absent on the host, so PID 1 behaviour under `--init` was not measured here. |
| NFR-001 | The launch path adds no resident process. The Python launcher is short-lived, and the podman CLI is the interface (ADR-002). |
