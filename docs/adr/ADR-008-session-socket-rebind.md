# ADR-008: Session socket rebind for long-running realms

- Status: Accepted, simulated (S-04s, S-08s, S-08t; podman 5.8.8 and crun 1.30.1). The rebind order, the identity compare, the batch form and the EINVAL case are pending T4.9 and T4.10, and the real logout run is pending T7.8. The variable grant attachment of the amendment below is pending the T4.9 host gate.
- Date: 2026-10-12
- Amended: 2026-10-12 (D3). The keep-id case is tested in simulation (S-08t). The decision is variant (b): the mount namespace only, entered through `podman unshare`.
- Amended: 2026-10-12 (D5). The rebind order is rewritten in the Decision. The identity record, the compare-and-skip step and the batch form (several paths under one `setns`) are untested. T7.8 proves them.
- Amended: 2026-10-12 (D6). Step 3.2 treats `EINVAL` from `umount2` as already unmounted, so a half-done rebind is recoverable on the next run. The duplicate exit-code paragraph is removed. The new case is untested, and T7.8 proves it.
- Amended: 2026-10-12 (D7). Status qualifier added. The evidence is simulated. The real logout run and the untested steps are pending T7.8. No decision changes.
- Amended: 2026-10-12 (variable grant attachment). Variable grants are attached after `podman start`, not bound at create. The helper is owned by T4.10, not by the real-logout task, and every start path attaches. The decision to rebind by helper in the mount namespace stands. Its scope grows from session sockets to every variable grant. See the amendment section below.
- Amended: 2026-10-12 (review of T4.2 and T4.3). The attach flags (`AT_RECURSIVE`, `MNT_DETACH`, the read-only setattr), the Python-side identity override of step 3.1, the `/tmp/.X11-unix` exception, the lazy re-attach grant list, and the failure rules at enter and launch are corrected in place. Section "Amendment (2026-10-12): review of T4.2 and T4.3" states them. The mechanism, variant (b) and the order of the four steps do not change. Requirement IDs: FR-061, FR-064, FR-065, FR-066, FR-081, IR-025.

## Amendment (2026-10-12): Variable grant attachment

This amendment keeps the decision above and changes where variable grants are attached. Where it differs from the Decision, this amendment governs. The four rebind steps of the Decision keep their order, and the `EINVAL` rule and variant (b) stay as written. The flags of steps 1, 3.1 and 3.2, and the identity compare, are changed by the review amendment below, which governs where they differ.

Why. The Opus review of T4.2 found three limits of create-time binds:

- Podman fixes create-time binds when it runs `podman create` (ADR-003 create-time flags). No podman call adds a mount to an existing container. The realm's read-write layer is the realm root, so recreating the container at start is not possible.
- Podman and crun refuse to start a container whose bind source is missing.
- Variable sources come and go. `~/Downloads` can be deleted after create. `/run/media/<user>` lives on tmpfs, so it is gone after a reboot until a session creates it. The session sockets and the authority file go at logout (ADR-004). Under create-time binds each of these stops every start. That includes the autostart at boot (FR-084) and the start that FR-064 AC-1 requires ("it starts"). A source missing at create could never be granted later.

Decision. Create-time binds hold only stable sources:

- the Realm Home, at the host `$HOME` path;
- host fonts, read-only;
- host `/etc/localtime`, read-only;
- device nodes, and the NVIDIA CDI spec of ADR-012;
- `--tmpfs /run/user/<uid>`;
- `--http-proxy=false`.

Every variable grant is attached after `podman start`, into the running container's mount namespace, by the helper of this ADR (variant (b): `podman unshare`, `setns` into the mount namespace, `open_tree(OPEN_TREE_CLONE | AT_RECURSIVE)`, `move_mount`). The clone is recursive, so a child mount such as a drive under a mount root comes with the tree. The variable grants are:

- tier folders (`~/Downloads`, `~/Projects`), read-write;
- the mount roots `/run/media/<user>`, `/media` and `/mnt`, read-write;
- session paths at fixed realm names under `/run/user/<uid>`: the Wayland socket, the X11 authority file (read-only), and the audio sockets. The X11 socket directory `/tmp/.X11-unix` (read-only) is the exception to the fixed names. It keeps its own path, which is neither under `/run/user/<uid>` nor inside the tmpfs;
- Custom shares (FR-063), each validated against DR-004 before it reaches the attach list.

"Session paths" replaces "socket paths" for this list. The list now also carries the X11 directory and the authority file.

Mountpoints. Tier folders have empty directories in the Realm Home as mountpoints. ginn creates them there, and they are Owned Paths (DR-010). `/media`, `/mnt` and `/run/media/<user>` are created inside the container after start, before the move. The attach step creates a session mountpoint inside the tmpfs: an empty directory for a directory source, and an empty regular file for a socket or a file source. The exception is `/tmp/.X11-unix`, which is created inside the image's `/tmp`.

Read-only grants. `move_mount` sets no mount flags. For a read-only grant the helper calls `mount_setattr(AT_RECURSIVE, MOUNT_ATTR_RDONLY)` on the detached tree before `move_mount`, so the flag reaches every child mount. The S-08 runs did not use this step. T4.9 proves it.

Missing sources. When a host source is missing at attach, ginn skips the grant. It records the skip in `RealmRecord.share_skipped`, and `ginn status` shows it (FR-064 AC-1). The start still succeeds. The grant is tried again at the next enter or launch. A grant that was attached before, whose source is gone at re-attach, is unmounted, so its old contents are not left visible. The skip is then recorded in the same way.

Lazy re-attach. At enter and launch, before exec, ginn compares each attached grant with the identity recorded at its last attach: the `(st_dev, st_ino)` pair of the host source. ginn reads that pair on the host, not in the container. For a mount root, the identity also covers the mounts below the source, read from the host mount table. A drive mounted under `/run/media/<user>` leaves that directory's inode unchanged, so the inode alone misses a new USB drive. Host mounts on the reference host are private, so rslave propagation from the host cannot carry a new drive into the realm. The earlier capabilities wording that relied on rslave is withdrawn.

The Python side decides which grants to re-attach. A grant whose identity differs is re-attached, and step 3.2 unmounts the old bind first. Python passes each grant it decided to attach with the `--force` flag, and the helper does not run its own compare (step 3.1) for it. The realm-side compare cannot see a child mount, so it would skip a mount root whose drive changed. Host-side identity therefore governs the forced grants, and the helper's compare still skips an unchanged grant that is not forced.

The grant list is the one fixed at the last start (FR-066), and it keeps the grants that were skipped then. Enter and launch do not re-read the current record. A change to the grant list, such as a new Custom share, applies at the next start, and `ginn status` reports that a restart is required until then. A grant skipped at start is tried again at each enter or launch, because it stays in that list.

Trigger points. Every start path runs attachment after `podman start`:

- the CLI start (T4.5);
- auto-start in enter and launch (T4.6);
- the services-on runit run script, after `podman start` (T7.3, ADR-003).

A start with no grant to attach still runs the identity check.

Fixed realm names. Each session path has a fixed realm name, whatever the host name is, except the X11 socket directory, which keeps its own path `/tmp/.X11-unix`. The realm always sees the Wayland socket at its fixed name under `/run/user/<uid>`, and the authority file at `/run/user/<uid>/ginn-xauthority`. `XAUTHORITY` is set at exec to that path (T4.3). A rotated authority file or a renamed Wayland socket is then an ordinary re-attach: the host source changes, and the realm path does not. The fixed name also stops podman from creating a `~/.Xauthority` mountpoint file in the Realm Home, which a bind at `$HOME/.Xauthority` would do. FR-065 requires identical paths for shared paths. Session paths are not shared paths, so the fixed names do not break FR-065 as the SRS words it. The SRS does not say so. CL-12 in `docs/clarifications.md` records the clarification, and it also covers FR-065 AC-1 for a source that appears after start.

Ranked characteristics (SRS section 4), in order:

1. Performance: one identity read per variable grant at enter and launch, plus a scan of the mount table for each mount root. The helper runs only for a grant that changed. ADR-010 holds this cost inside NFR-004, and T4.9 measures it.
2. Host integrity and isolation: the helper enters only the container's mount namespace. It touches only the paths in its list, and every Custom path passes DR-004 first.
3. Recoverability: a missing source no longer stops a start. A failed attach on a start fails the start and stops the realm again. A half-done attach is recovered by the idempotent re-attach (T4.10 notes).
4. Interoperability: after a re-login, or after a new drive is mounted, the realm sees the host's session or drive again.
5. Usability: no user step. The grant is attached at the next start, enter or launch.

What did not change. The mechanism, variant (b), the order of the four steps and the `EINVAL` rule stay as written. The review amendment adds flags to steps 1, 3.1 and 3.2, and it changes the identity rule for forced grants. The T4.9 gate exercises the compare-and-skip step (lazy re-attach), the batch form (a start with several grants), the `EINVAL` case (every fresh attach starts from an empty placeholder, which is not a mount point), the busy mount and the drive mounted after start. The half-done case, a crash between steps 3.2 and 3.3, is not forced by the gate. The idempotent re-attach covers it (T4.10 notes), and T4.10 tests force it. The real logout is still pending T7.8.

Consequences of this amendment:

- The create argv carries only the stable binds above. Variable grants are not in `CreateSpec` (T4.1).
- The helper is owned by T4.10, with `runtime/attach.py` and `native/Makefile`. The attach list comes from `runtime/attach.py`, which takes the grants from `mounts.py` and `env.py`. The Decision's statement that the list comes from `runtime/env.py` is replaced.
- T7.8 keeps only the host test `tests/integration/test_rebind_real.py`, the real logout run.
- Gate, T4.9: the host test must prove the attachment of a socket, a directory and a regular file under keep-id after `podman start`. It must also prove the read-only attach of a file and a directory, and the lazy re-attach after a host change. That includes a drive mounted below a mount root after start. The review amendment adds two cases: a busy directory grant, with an open file and a submount under it, which is detached with `MNT_DETACH`; and the read-only flag and the recursive clone reaching a child mount. If the gate fails, stop and escalate before M5. This is the same pattern as the S-01 gate before M7.
- The evidence so far is S-08t, which covers one socket under keep-id, in simulation. No evidence covers a directory, a regular file, the read-only flag or a drive mounted after start.
- A helper failure that is not a missing source fails the start, and the realm is stopped again. A failed lazy re-attach at enter or launch fails that command and never stops the realm. The crash between the mount steps is covered by the idempotent re-attach. The rules are in the T4.10 notes below.

### T4.10 notes

These rules bind the helper and `runtime/attach.py` (T4.10). They refine the amendment above, and the review amendment below adds to them.

Failure on a start. A helper failure that is not a missing source fails the start. ginn stops the realm again, so no half-granted realm runs. On the services-on run path, the run script stops the container before it exits non-zero, so the crash counter of runit (T7.3, FR-081 AC-3) counts the failure.

Failure on a running realm. A failed lazy re-attach at enter or launch fails that enter or launch, and ginn reports the failure with its code. It never stops the realm. A services-on realm is stopped only by its `sv` service (FR-081, ADR-003), never by enter or launch. A missing source is still a skip, so it never fails a start, enter or launch.

The code is a new GINN-E catalogue entry, added to `errors.py` and `docs/errors.md` in T4.10.

Crash between the mount steps. The re-attach is idempotent. For each path that step 3.1 does not skip, the helper unmounts whatever is mounted there before it moves the tree in. A directory grant is unmounted with `MNT_DETACH`, so a busy mount does not block the detach. The helper repeats `umount2` until the call returns `EINVAL`, which means nothing is mounted. `MNT_DETACH` takes the mount out of the namespace at once, and only open references remain. So the next call sees the placeholder and gets `EINVAL`, and the loop ends. A crash between `umount2` and `move_mount` leaves the empty placeholder, and the next attach clears it the same way. A crash after `move_mount` leaves a matching identity. For a forced grant step 3.1 does not run, so the re-run detaches and moves the tree again. That is still idempotent. A clean re-run of an unchanged grant changes nothing, because step 3.1 skips it. T4.10 tests force a failure between `umount2` and `move_mount`, then check that a re-run completes the attach.

## Amendment (2026-10-12): review of T4.2 and T4.3

This amendment corrects the variable grant amendment above where the two differ, and this one governs. The mechanism, variant (b) and the order of the four steps stay. The review found the points below. Each one is a decision, not a run. T4.9 proves them on the host.

Attach flags. The helper clones the host source with `open_tree(OPEN_TREE_CLONE | AT_RECURSIVE)`. The clone then carries child mounts, such as a drive under a mount root. Without `AT_RECURSIVE`, a tree with locked child mounts gives `EINVAL` in `podman unshare`'s namespace (review finding). A read-only grant gets `mount_setattr(AT_RECURSIVE, MOUNT_ATTR_RDONLY)` before `move_mount`. A directory grant is detached with `umount2(MNT_DETACH)` before a re-attach. Without `MNT_DETACH`, a mount with a submount or an open file gives `EBUSY`. `MNT_DETACH` takes the mount out of the namespace at once. Only open references remain, so the `EINVAL` loop still ends, and `stat` then sees the placeholder.

Python decides the identity. The helper's compare in step 3.1 reads the realm path from inside the container. For a mount root it cannot see a child mount, so it would skip a root whose drive changed. The Python side therefore decides, from the host, which grants to re-attach, as the lazy re-attach paragraph above says. It passes each decided grant with `--force`, and the helper skips step 3.1 for that grant. This overrides step 3.1 of the Decision for every forced grant. The compare still skips an unchanged grant that is not forced, so a second run changes nothing for it.

Argument form. Each grant is a group of argv tokens: `[--force] [--ro] <host-path> <realm-path>`, as the Decision shows. The helper reads the file type from `fstat` on the tree it opened, so a directory grant needs no flag.

The X11 directory. `/tmp/.X11-unix` is the one session path that keeps its own path. It is not under `/run/user/<uid>`, and it is not in the tmpfs. Its mountpoint is made in the image's `/tmp`. The other session paths keep their fixed names.

Grant list. The lazy re-attach uses the grant list fixed at the last start, and that list keeps the skipped grants. Enter and launch do not re-read the record. FR-066 requires a restart for a change to the list.

Failure at enter and launch. A failed lazy re-attach fails that enter or launch and reports it. It never stops a realm, and a services-on realm is stopped only by `sv` (FR-081). The T4.10 notes state the rule in full.

Gate. T4.9 must also prove a busy directory grant, with an open file and a submount under it, detached with `MNT_DETACH`. It must prove the read-only flag and the recursive clone on a child mount. And it must prove the drive mounted after start, reached by the lazy re-attach at the next enter or launch.

Not decided, for T4.9. A socket or file grant that another process holds open on its old mount may also give `EBUSY` under a plain `umount2`, as the Decision uses it. The Decision keeps the plain call for those grants, because the review finding names only directory grants. T4.9 tests that case. If it fails, `MNT_DETACH` applies to every re-attach, and this amendment is changed to say so.

## Context

A realm with services on keeps running after the user logs out (FR-081, FR-084). Its runit supervision and its autostart do not need a login. Its applications do need the host's display and audio sockets, which the session creates (IR-025: Wayland and X11 display sockets, PipeWire and PulseAudio sockets). ginn binds those sockets into the realm when the realm starts.

Logout removes the session's runtime directory, and the sockets go with it. The realm still holds the old sockets. The next login creates new sockets, and the realm cannot see them.

Measured in the simulated logout (S-04s and S-08s, podman 5.8.8, crun 1.30.1):

- After the simulated logout and login, the stale socket stays in the container. Its connect fails with ECONNREFUSED. The container keeps running with the same init process and start time.
- A container restart fixes the stale socket, but it also stops the services. That is the rejected alternative.
- A rebind in place works. It runs in the store's rootless user namespace, which `podman unshare` enters. It unmounts the stale bind with `umount2`. Then it moves a fresh `open_tree` clone of the host socket onto the path with `move_mount`. A probe after the rebind reaches the new server (S-08s row 5). The same rebind works under keep-id (S-08t, rows a and b). A keep-id container has its own user namespace, below the store's, so the helper reaches the mount namespace by a different route. See the evidence below.
- Four other forms failed (S-08s rows 1 to 4). They are listed under Alternatives considered.

The probe ran only after the rebind completed. The spike did not measure what a client sees in the window between the two calls.

### Evidence: keep-id rebind (S-08t, simulated)

Run on the same host, as uid 1000, with the S-08s simulated logout. Each row is a fresh container, a fresh gen2 login and one attempt.

| Row | Container | Helper flags | setns user | setns mnt | umount2 | move_mount | Probe after |
|---|---|---|---|---|---|---|---|
| Control: non-keep-id (S-08s row 5) | s08c | `--unmount-first` | skipped, same namespace | ok | ok | ok | reaches gen2 |
| a: row 5 under keep-id | s08c | `--unmount-first` | ok | ok | ok | ok | reaches gen2 |
| b: keep-id, mount namespace only | s08c | `--unmount-first --no-setns-user` | skipped by flag | ok | ok | ok | reaches gen2 |
| Row 3 control: keep-id, no umount | s08c | none | ok | ok | not run | FAILED, errno 2 (ENOENT) | stale, ECONNREFUSED |

The container's init pid and StartedAt do not change in any row. Row 3 shows that the `umount2` step is still needed under keep-id. Without it, `move_mount` onto the stale mount fails with ENOENT, as it did in S-08s row 3.

Namespace inodes, row a, listed before the attempt:

```
plain session:              user=4026531837  mnt=4026531832
podman unshare:             user=4026532812  mnt=4026532816
container init (pid 28798): user=4026533089  mnt=4026533101
```

The user namespace of `podman unshare` and that of the container init are compared within one cycle only, because inode numbers are reused between cycles:

- Control (non-keep-id): `podman unshare` user 4026533089, container init user 4026533089. The same namespace.
- Row b (keep-id): 4026533167 and 4026533169. Different.
- Row 3 (keep-id): 4026533107 and 4026533109. Different.

Mechanism. This is inferred from namespace ownership and observed in rows a and b. It was not checked against the kernel source. The helper runs in the store's user namespace, which `podman unshare` enters. That namespace is either the realm's user namespace (the control) or its ancestor (keep-id). In both layouts the helper therefore holds mount capabilities over the realm's mount namespace. `setns` into that mount namespace from `podman unshare` succeeded in both keep-id rows.

Ranked characteristics (SRS section 4), in order:

1. Performance: the helper runs at start, to attach every variable grant, and at enter or launch, to re-attach a grant only when its host source changed. The helper is not resident (NFR-001), so it adds no idle cost.
2. Host integrity and isolation: the helper enters only the container's own mount namespace. It changes only the paths ginn names.
3. Recoverability: no restart. Running services keep their state.
4. Interoperability: the realm's apps reach the host's display and audio again after a re-login.
5. Usability: no user step. The rebind happens on the next enter or launch.

Assumptions and gaps:

- The simulation is not a real logout. The PAM session close and the elogind teardown were not measured. The real-logout run (S-04) was invalid, because the host created no elogind session for the test login. The rebind result is simulated (S-08s).
- Only a server that dies at logout was tested. A server that outlives the logout, with its socket unlinked under it, was not tested.
- Processes that already hold the old socket descriptor were not tested.
- The rebind ran as the container's owner, uid 1000. A container with another owner was not tested.
- Rebind into a keep-id container was run in simulation only (S-08t, uid 1000, podman 5.8.8, crun 1.30.1). The real-logout path under keep-id is not tested.
- Tested on podman 5.8.8 and crun 1.30.1 only.
- The spike ran one path per helper call. Every S-08 run used one socket path (S-08s rows 1 to 6, S-08t rows a and b). The batch form and the compare step in the Decision below were not run.

## Decision

ginn rebinds stale session sockets with a small native helper, `native/ginn-rebind.c`, at most 200 lines of C. It runs in the store's user namespace, which `podman unshare` enters. It never joins the container's user namespace: this is variant (b), the mount namespace only. The helper takes its path list from `runtime/env.py`, which decides which sockets are bound. It does not read the mount table and does not discover mounts. Each pair of arguments is a host socket path and its realm path. For the list, the helper runs four steps in this order:

1. In the caller's namespace, for each host socket path: open it with `open_tree(OPEN_TREE_CLONE | AT_RECURSIVE)`, keep the detached tree as a file descriptor, and record the `(st_dev, st_ino)` pair of the host socket. (The review amendment adds `AT_RECURSIVE`.) This step runs before `setns`. After `setns` into the realm's mount namespace the host path is not visible (S-08s row 1).
2. `setns` into the container's mount namespace, once. The namespace comes from the container's init process. It is the only namespace the helper enters.
3. For each realm path in the list:
   1. `stat` the realm path. If its `(st_dev, st_ino)` pair already matches the recorded host identity, skip the path, so a second run changes nothing. A forced grant (see the review amendment) does not run this check, because Python has already decided to attach it. **Untested.** T4.9 proves it.
   2. Otherwise, `umount2` the stale bind on that path. A directory grant uses `MNT_DETACH`. `EINVAL` is the error `umount2(2)` returns for a path that is not a mount point. That is the state a previous run leaves when `umount2` succeeded and the `move_mount` in step 3.3 then failed. The helper treats `EINVAL` as already unmounted and goes on to step 3.3. Any other error fails the run. **Untested.** T4.9 proves the fresh-attach case, and T4.10 proves the half-done case.
   3. `move_mount` the detached tree from step 1 onto that path.
4. Exit 0 when every path matched or was rebound. Exit non-zero on any failure. ginn reports the failure as a catalogue error; the code is added with the helper (T4.10).

What ran. The spike helper (`spikes/s04s_s08s_rebind.py`) ran, for one path per call, `open_tree` clone, then `setns` user (skipped in the non-keep-id control), then `setns` mnt, then `umount2`, then `move_mount`, under `podman unshare` (S-08s row 5; S-08t rows a and b, where row b passed `--no-setns-user`). It recorded no identity and made no compare. The ordering above keeps the spike's open-before-`setns` order and adds the record and the compare. The batch form, with several paths under one `setns`, was not run. The record, the compare and the batch form are untested. T4.9 proves them. The real-logout test is T7.8, described in the Consequences below.

ginn runs the helper through podman, with the global flags that ADR-004 requires:

```
podman --remote=false --root <root> --runroot <runroot> --tmpdir <tmpdir> --runtime crun unshare ginn-rebind <init-pid> [--force] [--ro] <host-path> <realm-path> [[--force] [--ro] <host-path> <realm-path>]...
```

Each grant is one group of argv tokens. `--force` marks a grant that Python decided to attach, and `--ro` marks a read-only grant (review amendment). The helper learns whether a grant is a directory from `fstat` on the tree it opened, so it needs no flag for that.

Of the forms tried in S-08s, this is the only one that worked. A plain session cannot call `open_tree`. The call returns EPERM (S-08s rows 4 and 6). So the rebind must run inside `podman unshare`. The helper needs no root, and it runs no resident process.

The trigger is lazy, and it stays in Python. The enter and launch path compares the `(st_dev, st_ino)` pair of each host source with the identity recorded at its last attach, and it reads the host side only (review amendment). It starts the helper only for a grant that differs, and it passes that grant with `--force`. The pre-check keeps a `podman unshare` call off the launch path when nothing is stale. S-02 measured `podman exec` on that path, not `podman unshare`. The pre-check's cost is measured by T4.9 (ADR-010). The enter and launch path is T4.6 for every realm, and T7.1 for services-on realms. Services do not use session sockets, so there is no login hook. A services-on realm that never launches a GUI app never runs the helper.

## Alternatives considered

- Restart the realm at login (restart-required). Rejected. A services-on realm exists to keep its services running across logout (FR-081, FR-084). A restart stops them. It would fix the stale socket, but it breaks the purpose of services-on.
- `nsenter` with `mount --bind` from the host path into the container. Rejected. It fails with ENOENT (S-08s row 1). The host path does not exist in the container's mount namespace.
- `nsenter --user --mount` under `podman unshare`. Rejected. It fails with EINVAL (S-08s row 2). In the non-keep-id control, `podman unshare` already runs in the container's user namespace, so there is nothing to join. Under keep-id that is not so (S-08t). Joining the user namespace is variant (a), below.
- Variant (a): join the user namespace, then the mount namespace. It works under keep-id (S-08t row a). Not chosen. Variant (b) is the path that ran in both layouts, and it enters the fewest namespaces.
- The helper without `umount2`. Rejected. `move_mount` onto the stale mount fails with ENOENT (S-08s row 3). The stale mount must go first. The cause of the ENOENT is inferred. See Consequences.
- The helper in a plain session. Rejected. `open_tree` returns EPERM (S-08s rows 4 and 6). The rebind needs the store's user namespace.
- The Python helper from the spike (`spikes/s04s_s08s_rebind.py`). It proved the syscall sequence. It is not the shipped form. The roadmap keeps syscall-level helpers in small native programs, and it keeps resident Python out (NFR-002).

## Consequences

- The helper depends on the rootless user namespace that the store creates. In the non-keep-id control (S-08s, S-08t), the container init and `podman unshare` share one user namespace. Under keep-id they do not. The keep-id container's user namespace lies under the store's (inferred from namespace ownership, S-08t). ginn can rebind only into a container of a store whose user namespace `podman unshare` enters, directly or as an ancestor. It cannot reach an arbitrary container. The spike did not check whether that namespace belongs to the store's pause process.
- Between `umount2` and `move_mount`, the path holds the placeholder that podman created for the bind. It does not hold a socket. A client that connects in that window gets an error. The spike did not time the window. It is expected to be short.
- The ENOENT from `move_mount` onto a stale mount is inferred, not verified. The likely cause is that the kernel cannot find a mount point on an unlinked dentry. The kernel source was not checked. S-08t row 3 gives the same ENOENT under keep-id, so the `umount2` step is needed in both layouts.
- Processes that already hold the old socket descriptor keep it. Their state after the rebind was not measured.
- A stale socket stays in place until the next enter or launch of that realm. The rebind is lazy by design.
- The helper touches only the paths it is given, and only inside the container's mount namespace. It writes no Owned Path (DR-010). Its arguments are an argv list (NFR-SEC-005).
- Untested in this decision, and proven by T4.9 and T4.10: the step-1 identity record, the compare-and-skip in step 3.1 (a second run must change nothing), the batch form (several paths under one `setns`), and the `EINVAL` case in step 3.2. The T4.9 gate runs the first three and the fresh-attach `EINVAL` case. The T4.10 test runs the half-done `EINVAL` case: it leaves a realm path after `umount2` succeeds and `move_mount` fails, then checks that the next run completes the rebind.
- The helper is built and shipped with the package (T4.10). Its error code needs a catalogue entry.
- Tested on podman 5.8.8 and crun 1.30.1 only. The T7.8 host-only test must run the rebind after a real logout. That needs the S-04 precondition: an elogind session for the test login. The T7.8 test must cover a keep-id container as well as a control container. The half-done case of step 3.2 belongs to T4.10, not to this test.
- Follow-up tasks: T4.6 and T7.1 (the lazy trigger), T4.9 (the host gate), T4.10 (the helper and its test), and T7.8 (the real-logout run). The SRS wording for CL-5 is open for v1.1.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-081 | Services-on realms keep running after logout. The rebind lets their apps reach the session after a re-login, with no restart. A failed re-attach at enter or launch never stops a services-on realm; only its `sv` service stops it (review amendment). |
| FR-061 | The Basic grants that vary by host (tier folders, mount roots, session paths) are attached after start, not bound at create (amendment). |
| FR-063 | Custom shares are attached after start, after the DR-004 check (amendment). |
| FR-064 | A missing tier folder or drive is skipped at attach, recorded in `RealmRecord.share_skipped`, and shown by `ginn status`. The start still succeeds (amendment). |
| FR-065 | Shared paths keep identical realm paths. Session paths use fixed realm names, which the SRS does not cover (amendment, CL-12 in `docs/clarifications.md`). |
| FR-066 | Grant changes apply at the next start. The attach step runs at every start path (amendment). |
| FR-084 | Autostart runs without a login. A session path that is missing at boot does not stop the start. It is attached at the next enter or launch after it appears (amendment). |
| IR-025 | The display and audio sockets are session paths. The helper attaches them at fixed realm names under `/run/user/<uid>` (amendment). |
| NFR-001 | The helper is not resident. It runs at start, enter and launch. At start it attaches every variable grant. At enter and launch it re-attaches a grant only when its host source changed (lazy re-attach). |
| DR-010 | The helper writes no Owned Path. The runtime root stays as ADR-004 sets it. |
| NFR-SEC-005 | The helper runs from an argv list. No shell string is built. |
