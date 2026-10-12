# ADR-013: Host Setup Record, pre-images and resume

- Status: Proposed
- Date: 2026-10-12

## Context

FR-001 requires Host Setup to record every host path it writes. FR-002 requires a second run to change no host file, byte for byte, and to report "already configured". FR-003 requires undo to restore each recorded path to its pre-setup content or remove it. DR-009 defines the record: the user, the subordinate ID range, every host path written and the timestamp. NFR-030 requires that after a SIGKILL at any point no data is lost and the next command finds a known state.

Undo can restore only what was captured before the first write. A record written after the first write would hold the post-crash state as its pre-image. A resume that captured pre-images again would make the same mistake.

Two groups of paths change. Setup writes files under /etc, /var/service and /run/lock once, during the run. The supervisor run script writes the root `cgroup.subtree_control` and the `ginn` cgroup directory each time the service starts (ADR-003, spike S-01). Setup does not run that script, so it cannot record those writes as they happen. It must capture their pre-images before the service can start.

Ranked characteristics (SRS section 4): recoverability and host integrity settle the decision. A rerun must never change a host file (host integrity), and an interrupted run must be recoverable with the true pre-setup state (recoverability). Performance does not apply, because Host Setup runs once.

## Decision

1. Persist before write. Setup captures the pre-image of every path it will write, and of each service-written path, and then writes the record with status `pending`. That record write is the first write to the host, apart from the creation of the record's own directory (see Residuals).
2. Resume keeps the stored pre-images. A pending record is never captured again. A repair stores the new uid, gid and timestamp, keeps every pre-image, sets status `pending` before the next write, and writes the new post-images when it completes.
3. Two lists. `paths` holds the paths setup writes, each with a pre-image and, once complete, a post-image. `service_paths` holds the paths the run script writes at service start, with pre-images only. Undo treats the two lists differently: a service path may not exist when undo runs.
4. Idempotence (FR-002). A complete record whose setup-written paths still match their post-images, and whose subordinate ID entries are present, means "already configured". The run then takes no lock, creates no file and writes nothing, not even the record, so its mtime and the directory mtimes stay the same. Any other state is a repair.
5. `/etc/subuid` and `/etc/subgid` are listed with pre-images like every other file. The idempotence check compares their entries for the user, not their digests, because another user's setup appends lines to them.
6. Subordinate IDs. A first run records one range per file: the user's own line if it already holds at least 524,288 IDs, otherwise a new full-size line placed by `allocate`. The record is rewritten, still pending, once both ranges are known. A repair reuses the recorded ranges (`ensure_exact`). If another line overlaps a recorded range, setup raises GINN-E-SUBID-CONFLICT and changes nothing. It never chooses another range.
7. Placement. Every host path is placed under `root_dir`, including the record, the lock directory and the link. The contents of the rendered scripts and the link target keep their host paths, such as `/run/ginn/<uid>` and `/etc/sv/ginn-<user>`. Setup does not require `/run/ginn/<uid>` (ADR-004).
8. Cgroup delegation. Setup does not call `cgroups.delegate_user_tree`. The run script does the same writes when the service starts (ADR-003, `runit_system.py` step 2), and those paths are recorded as service-written.
9. The record lists its own file with pre-image `absent` and post-image `self`, because its digest cannot be stored in itself.

## Alternatives considered

- Write the record after the writes. Rejected: a crash between a write and the record leaves a host change with no record of it.
- Capture pre-images again on resume. Rejected: the crashed run's content would become the pre-image, and undo would restore the wrong state.
- Compare digests of `/etc/subuid` and `/etc/subgid` as well. Rejected: a second user's setup changes those files, so the first user's rerun would report drift and rewrite its record.
- Pick a new range when the recorded one is taken. Rejected by the task design. The range belongs to the user's record, and a silent new range would leave the old one allocated to nobody.
- Call `delegate_user_tree` from setup. Rejected for T3.4, as Decision 8 explains. Undo for those paths belongs to T3.5.
- Lock the whole setup run. Not added in T3.4 (see Residuals).

## Consequences

- T3.5 undo must restore the pre-image of `cgroup.subtree_control` and must not assume the original value (ADR-003). It must remove created files and directories listed in `paths` and treat each `service_paths` entry as possibly absent.
- Undo must not restore the pre-image of `/etc/subuid` or `/etc/subgid`. A later user's line would be lost. Undo removes that user's own entries from those files instead.
- The record can be complete only with subordinate ID ranges and a post-image for every setup path. `hostsetup/record.py` refuses to write any other record, and refuses to read a record that breaks these rules.
- A record that exists but cannot be read is refused with GINN-E-SETUP-PATH-UNSAFE (exit 77). It is never treated as absent, because that would capture pre-images from a host that setup already changed.
- `runit_system.install` is split into `install_scripts` and `install_link`, so setup can record its progress between the two. `install` keeps its behaviour.
- The service-written list follows the design: `sys/fs/cgroup/cgroup.subtree_control` and `sys/fs/cgroup/ginn`. The run script also creates `ginn/<uid>` and `ginn/<uid>/supervisor` and writes the controllers, and those paths are not in the list. T3.5 must cover them, or the list must be extended.

## Residuals

- Crash in the record's directory window. The directory `etc/ginn` must exist before the first record write. A crash after its creation and before the record write leaves an empty directory with no record. A rerun then takes the directory as already present. Undo leaves an empty root directory.
- Reclaim gate. A stale subordinate ID lock is reclaimed through `/run/lock/ginn-<file>.lock.reclaim`, which is written only in that case and then stays. It is not recorded. It is an empty file in a tmpfs that a reboot clears.
- Concurrency. The subordinate ID lock covers only the append. No lock covers the whole run, so two runs for the same user can interleave. Host Setup is run once, by an operator. T3.5 must take the same lock if it adds one.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| DR-009 | Defines the record's contents: the user, ids, subordinate ranges, timestamp, status, and each path with its pre-image and post-image. |
| FR-001 | Setup captures pre-images, persists the record before the first write, and records each path it writes. |
| FR-002 | A complete record whose paths still match means "already configured" and nothing is written. |
| FR-003 | Undo depends on the stored pre-images, the split between setup-written and service-written paths, and the subordinate ID rule in Consequences. |
| NFR-030 | A crash after any step leaves a pending record, and the next run resumes from the stored pre-images. |
| NFR-040 | The new refusals carry catalogue codes: GINN-E-SETUP-ROOT, GINN-E-NO-SUCH-USER, GINN-E-SUBID-CONFLICT and GINN-E-SETUP-PATH-UNSAFE. |
