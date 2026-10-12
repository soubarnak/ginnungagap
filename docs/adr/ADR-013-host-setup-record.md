# ADR-013: Host Setup Record, pre-images and resume

- Status: Proposed
- Date: 2026-10-12
- Amended: 2026-10-12 (D8; code in F8a). Adds the Void `/var/service` chain (Decision 10), the global lock (Decision 11), shared-state entries and `boot_id` (Decision 12), schema 2 (Decision 13), the doctor's data source (Decision 14), the undo rules (Decision 15) and the write rules (Decision 16). Decision 4 changes: the lock is taken only when the record does not say "already configured". Decision 1 changes: the first record write goes into a temporary directory. Decisions 2, 3 and 5 to 9 are unchanged. T3.4 as first committed (63de96e) refused the Void chain and failed on every Void host. F8a fixes it.

## Context

FR-001 requires Host Setup to record every host path it writes. FR-002 requires a second run to change no host file, byte for byte, and to report "already configured". FR-003 requires undo to restore each recorded path to its pre-setup content or remove it. DR-009 defines the record: the user, the subordinate ID range, every host path written and the timestamp. NFR-030 requires that after a SIGKILL at any point no data is lost and the next command finds a known state.

Undo can restore only what was captured before the first write. A record written after the first write would hold the post-crash state as its pre-image. A resume that captured pre-images again would make the same mistake.

Two groups of paths change. Setup writes files under /etc and /run/lock once, during the run. On Void it also creates the service link in the runsvdir directory that `/var/service` resolves to (Decision 10). The supervisor run script writes the root `cgroup.subtree_control` and the `ginn` cgroup directories each time the service starts (ADR-003, spike S-01). Setup does not run that script, so it cannot record those writes as they happen. It must capture their pre-images before the service can start.

Ranked characteristics (SRS section 4): recoverability and host integrity settle the decision. A rerun must never change a host file (host integrity), and an interrupted run must be recoverable with the true pre-setup state (recoverability). Performance does not apply, because Host Setup runs once.

## Decision

1. Persist before write. Setup captures the pre-image of every path it will write, and of each service-written path, and then writes the record with status `pending`. The record write is the first write to the host, with one exception. When `etc/ginn` does not exist, the record is written into the temporary directory of Decision 16, and that directory is renamed into place only after the record is in it.
2. Resume keeps the stored pre-images. A pending record is never captured again. A repair stores the new uid, gid and timestamp, keeps every pre-image, sets status `pending` before the next write, and writes the new post-images when it completes.
3. Two lists. `paths` holds the paths setup writes, each with a pre-image and, once complete, a post-image. `service_paths` holds the paths the run script writes at service start, with pre-images only. Undo treats the two lists differently: a service path may not exist when undo runs.
4. Idempotence (FR-002). The check reads only the record, and it takes no lock. A complete record whose setup-written paths still match their post-images (digest and mode), and whose subordinate ID entries cover the recorded ranges (Decision 16), means "already configured". The run then creates no file, takes no lock and writes nothing, not even the record. The record's mtime, the directory mtimes and `/run/lock` stay the same. Any other state goes to the repair path, which takes the lock (Decision 11).
5. `/etc/subuid` and `/etc/subgid` are listed with pre-images like every other file, with kind `append-lines` (Decision 13). The idempotence check compares their entries for the user, not their digests, because another user's setup appends lines to them.
6. Subordinate IDs. A first run records one range per file: the user's own line if it already holds at least 524,288 IDs, otherwise a new full-size line placed by `allocate`. The record is rewritten, still pending, once both ranges are known. A repair reuses the recorded ranges (`ensure_exact`). If another line overlaps a recorded range, setup raises GINN-E-SUBID-CONFLICT and changes nothing. It never chooses another range.
7. Placement. Every host path is placed under `root_dir`, including the record, the lock directory, the lock file and the link. The contents of the rendered scripts and the link target keep their host paths, such as `/run/ginn/<uid>` and `/etc/sv/ginn-<user>`. Setup does not require `/run/ginn/<uid>` (ADR-004).
8. Cgroup delegation. Setup does not call `cgroups.delegate_user_tree`. The run script does the same writes when the service starts (ADR-003, `runit_system.py` step 2), and those paths are recorded as service-written (Decision 12).
9. The record lists its own file with pre-image `absent` and post-image `self`, because its digest cannot be stored in itself.
10. Void service directory. On Void, `/var/service` is a symlink chain: `/var/service` -> `../run/runit/runsvdir/current` -> `/etc/runit/runsvdir/current` -> `default`. This was checked read-only on the reference host on 2026-10-12. Setup follows a symlink chain at `var/service` only. It resolves the chain inside the root: absolute targets are re-rooted under `root_dir`, and a `..` cannot leave the root. The chain must end at a root-owned directory inside the root. If it does not, setup refuses with GINN-E-SETUP-PATH-UNSAFE before its first write. The service link is created and recorded at the resolved path, `/etc/runit/runsvdir/default/ginn-<user>`, with pre-image `absent`. Every other path refuses symlinks, with the same error.
11. Global lock. Setup and undo take `/run/lock/ginn-host-setup.lock` (under `root_dir` in tests). A run first does the lock-free check of Decision 4. If the record does not say "already configured", the run takes the lock, loads the record again and repeats the check under the lock, before its first write. If the check under the lock says "already configured", the run touches nothing. The lock is global, not per user, because `etc/ginn`, `sys/fs/cgroup/ginn` and the root `cgroup.subtree_control` are shared by all users. The lock file is not recorded, and undo does not remove it. It is an empty file in tmpfs, which a reboot clears. Removing it could let two runs lock different inodes.
12. Shared state. Each record entry for a shared path carries `shared: true`. The shared paths are `/etc/ginn`, `/sys/fs/cgroup/ginn`, `/run/ginn`, `/run/lock` and the root `/sys/fs/cgroup/cgroup.subtree_control`. A shared entry keeps its pre-image like any other entry. Service-written entries carry the `boot_id` (`/proc/sys/kernel/random/boot_id`) read when their pre-image was captured. The service-written list is `/sys/fs/cgroup/cgroup.subtree_control`, `/sys/fs/cgroup/ginn`, `/sys/fs/cgroup/ginn/<uid>`, `/sys/fs/cgroup/ginn/<uid>/supervisor`, `/run/ginn` and `/run/ginn/<uid>`. The run script creates the `<uid>` and `supervisor` cgroups and `/run/ginn/<uid>`, and it writes the root `cgroup.subtree_control`.
13. Record schema 2. Schema 1 is the form committed in 63de96e. Schema 2 adds `shared` and `boot_id`, gives each post-image a `mode` as well as its `sha256`, and uses kind `append-lines` for the two subid files. The record file is mode 0600, because it holds the pre-image content of host files.
14. Doctor reads host state. `doctor/checks.py` (T3.6) reads the subordinate ID lines, the `/etc/sv/ginn-<user>` directory, the service link and the cgroup tree. It never reads the Host Setup Record. Its root controller tolerance comes from the current root `cgroup.subtree_control`, not from the record. Its subordinate ID check applies the one-line rule of Decision 16.
15. Undo rules. These rules bind T3.5 (`hostsetup/undo.py`).
    - (i) Subordinate ID lines. A line is removed only when its name, start and count match a recorded range exactly, and the line is absent from the stored pre-image. A same-name line that differs, through an admin edit or a widening, stays in place, and undo prints a warning. Undo never restores `/etc/subuid` or `/etc/subgid` as whole files.
    - (ii) Root `cgroup.subtree_control`. Undo acts only when no record other than the one being undone exists (pending or complete), and the stored `boot_id` matches the current boot, because cgroup state does not survive a reboot. It then writes `-<ctrl>` for each controller that the pre-image does not list. It never writes the pre-image bytes back, because cgroupfs does not accept them. This step runs after the cgroup directories are removed, because cgroup v2 does not disable a controller that a child cgroup still uses.
    - (iii) Shared directories. A shared directory is removed only when its pre-image is `absent`, and only with `rmdir`. ENOTEMPTY is not an error. The directory stays, because another user's entries are in it.
    - (iv) Nothing marked shared is restored by writing bytes back. Undo reverses the change that setup made.
    - (v) Order. Undo removes the service link first, then waits for runsv to exit, then removes the cgroup leaves and directories. The remaining steps follow in any order that keeps (ii) after the cgroup removal. The record file is removed last, so an interrupted undo still finds the pre-images. Undo holds the lock of Decision 11 for the whole run.
    - (vi) Per-user files. The `/etc/sv/ginn-<user>` directory and its scripts are restored or removed from their pre-images, as FR-003 AC-1 requires.
16. Write rules (F8a).
    - The first creation of `etc/ginn` goes through a temporary directory. Setup creates `etc/.ginn.ginn-tmp`, writes the pending record into it, and renames it to `etc/ginn`. A crash before the rename leaves only the temporary directory, and it holds no record.
    - Setup writes use fixed temporary names, `.<name>.ginn-tmp`, in the target directory. A run removes any stale ones under the lock (Decision 11) before its first write.
    - Post-images carry the mode as well as the digest (Decision 13).
    - `ensure_exact` accepts a same-name line that covers the recorded range. A line covers the range when its start is at or below the recorded start and its end is at or above the recorded end. Setup then appends nothing.
    - `ensure_single` uses the one-line rule. FR-001 AC-1 needs one range of at least 524,288 IDs in each file, so two half-size lines do not count together, and setup adds a third, full-size line. T3.6 doctor uses the same rule.

## Alternatives considered

- Write the record after the writes. Rejected: a crash between a write and the record leaves a host change with no record of it.
- Capture pre-images again on resume. Rejected: the crashed run's content would become the pre-image, and undo would restore the wrong state.
- Compare digests of `/etc/subuid` and `/etc/subgid` as well. Rejected: a second user's setup changes those files, so the first user's rerun would report drift and rewrite its record.
- Pick a new range when the recorded one is taken. Rejected by the task design. The range belongs to the user's record, and a silent new range would leave the old one allocated to nobody.
- Call `delegate_user_tree` from setup. Rejected for T3.4, as Decision 8 explains. Undo for those paths is set by Decision 15.
- Lock the whole setup run per user. Rejected: the shared paths are not per user. Not added in T3.4. F8a adds one global lock (Decision 11).
- Create the lock file on the already-configured path. Rejected: a new file in `/run/lock` would break FR-002 AC-1, which requires no host change on a rerun.
- Restore `/etc/subuid` and `/etc/subgid` as whole files. Rejected: a later user's line would be lost.
- Write the pre-image bytes back to root `cgroup.subtree_control`. Rejected: cgroupfs does not accept them (Decision 15, ii).
- Follow symlinks wherever they occur. Rejected: setup follows only the Void chain at `var/service` (Decision 10).

## Consequences

- T3.5 undo follows Decision 15. It must not restore the pre-image of `cgroup.subtree_control` by writing bytes, and it must not assume the original value (ADR-003). ADR-003 still says that undo restores the recorded value. That sentence needs the same change in a later ADR-003 amendment.
- Undo must remove the created files and directories listed in `paths`, within the limits of Decision 15, and treat each `service_paths` entry as possibly absent.
- Undo must not restore the pre-image of `/etc/subuid` or `/etc/subgid`. A later user's line would be lost. Undo removes that user's own entries from those files instead (Decision 15, i).
- FR-003 AC-1 ("restored to its pre-setup content or removed") is met for the subid files and the shared paths by reversing setup's change, not by writing bytes back (Decision 15, i and iv). Removing the line setup appended, and disabling the controllers setup added, return each path to its pre-setup state. No bytes are written back, because other users' lines and live cgroup state may have changed since setup.
- The record can be complete only with subordinate ID ranges and a post-image for every setup path. `hostsetup/record.py` refuses to write any other record, and refuses to read a record that breaks these rules.
- A record that exists but cannot be read is refused with GINN-E-SETUP-PATH-UNSAFE (exit 77). It is never treated as absent, because that would capture pre-images from a host that setup already changed.
- `runit_system.install` is split into `install_scripts` and `install_link`, so setup can record its progress between the two. `install` keeps its behaviour.
- The service-written list is the six paths of Decision 12, including `ginn/<uid>` and `ginn/<uid>/supervisor`. T3.5 covers all six.
- Doctor reads host state, not the record (Decision 14), so a damaged record cannot hide a host fault from `ginn doctor`.

## Residuals

- Crash in the record's directory window. Closed by Decision 16. A crash before the rename leaves the temporary directory, with no record and no other write. The next run removes it under the lock.
- Controllers enabled after the pre-image. Undo (ii) cannot tell a controller that the run script added from one that another party enabled after the pre-image, within the same boot. The record keeps no post-image for that path. Undo accepts this limit.
- Reclaim gate. A stale subordinate ID lock is reclaimed through `/run/lock/ginn-<file>.lock.reclaim`, which is written only in that case and then stays. It is not recorded. It is an empty file in a tmpfs that a reboot clears.
- Concurrency. Closed for setup and undo by Decision 11. The subordinate ID lock still covers only the append.
- Lock file. `/run/lock/ginn-host-setup.lock` stays after a run, as an empty file in tmpfs (Decision 11).

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| DR-009 | Defines the record's contents: the user, ids, subordinate ranges, timestamp, status, and each path with its pre-image and post-image. Schema 2 adds `shared` and `boot_id` (Decision 13). |
| DR-010 | The shared paths of Decision 12 lie outside the Owned Paths. Setup and undo write them only under Decisions 11 and 15. |
| FR-001 | Setup captures pre-images, persists the record before the first write, and records each path it writes. Decisions 10 and 11 add the Void chain and the global lock. |
| FR-002 | A complete record whose paths still match means "already configured", and nothing is written, not even the lock file (Decision 4). |
| FR-003 | Undo follows Decision 15. It depends on the stored pre-images, the split between setup-written and service-written paths, and the subordinate ID rule in Decision 16. |
| FR-007 | Doctor reads host state, not the record (Decision 14). Its subordinate ID check uses the one-line rule (Decision 16). |
| NFR-030 | A crash after any step leaves a pending record, or a temporary directory with no record. The next run resumes from the stored pre-images, or removes the temporary entries under the lock. |
| NFR-040 | The new refusals carry catalogue codes: GINN-E-SETUP-ROOT, GINN-E-NO-SUCH-USER, GINN-E-SUBID-CONFLICT and GINN-E-SETUP-PATH-UNSAFE. |
