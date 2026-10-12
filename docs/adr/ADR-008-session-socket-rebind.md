# ADR-008: Session socket rebind for long-running realms

- Status: Accepted
- Date: 2026-10-12
- Amended: 2026-10-12 (D3). The keep-id case is tested in simulation (S-08t). The decision is variant (b): the mount namespace only, entered through `podman unshare`.

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

1. Performance: the rebind runs only on enter or launch, and only when a socket differs. The helper is not resident (NFR-001), so it adds no idle cost.
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

## Decision

ginn rebinds stale session sockets with a small native helper, `native/ginn-rebind.c`. The helper is at most 200 lines of C. It runs in the store's user namespace, which `podman unshare` enters. It does not join the container's user namespace. This is variant (b): the mount namespace only. For each path in its list, the helper does four steps:

1. It enters the container's mount namespace with `setns`. The namespace comes from the container's init process. It is the only namespace the helper enters.
2. It compares the `(st_dev, st_ino)` pair of the path with the host socket. If the pair in the container already matches, it skips the path. A second run therefore changes nothing.
3. It calls `umount2` on the stale path only.
4. It opens a clone of the host socket with `open_tree`, and it moves that clone onto the path with `move_mount`.

Variant (b) is the decided path. It is the one code path that ran in both layouts. In the non-keep-id control the two user namespaces are the same, so a user-namespace step would change nothing. Under keep-id the spike skipped that step with `--no-setns-user` (S-08t row b). Variant (a) joins the user namespace first, then the mount namespace. It also works under keep-id (S-08t row a). It is recorded as an alternative, not chosen.

The helper takes an explicit path list from `runtime/env.py`. That module decides which sockets are bound. The helper does not read the mount table, and it does not discover mounts itself.

ginn runs the helper through podman, with the global flags that ADR-004 requires:

```
podman --remote=false --root <root> --runroot <runroot> --tmpdir <tmpdir> --runtime crun unshare ginn-rebind <init-pid> <path>...
```

Of the forms tried in S-08s, this is the only one that worked. A plain session cannot call `open_tree`. The call returns EPERM (S-08s rows 4 and 6). So the rebind must run inside `podman unshare`. The helper needs no root, and it runs no resident process.

The trigger is lazy. The enter and launch path compares the `(st_dev, st_ino)` pair of each host socket with the container's before it execs the app. It runs the helper only when a path differs. The enter and launch path is T4.6 for every realm, and T7.1 for services-on realms. Services do not use session sockets, so there is no login hook. A services-on realm that never launches a GUI app never runs the helper.

The helper exits 0 when every path matches or was rebound. It exits non-zero on any failure, and ginn reports that failure as a catalogue error. The catalogue code is added with the helper (T7.8).

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
- The helper is built and shipped with the package (T7.8). Its error code needs a catalogue entry.
- Tested on podman 5.8.8 and crun 1.30.1 only. The T7.8 host-only test must run the rebind after a real logout. That needs the S-04 precondition: an elogind session for the test login. The T7.8 test must cover a keep-id container as well as a control container.
- Follow-up tasks: T4.6 and T7.1 (the lazy trigger), and T7.8 (the helper and its test). The SRS wording for CL-5 is open for v1.1.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-081 | Services-on realms keep running after logout. The rebind lets their apps reach the session after a re-login, with no restart. |
| FR-084 | Autostart runs without a login. The rebind runs on the next enter or launch, so an autostarted realm needs no login step. |
| IR-025 | The display and audio sockets are the paths the helper rebinds. |
| NFR-001 | The helper is not resident. It runs only on enter or launch, and only when a socket differs. |
| DR-010 | The helper writes no Owned Path. The runtime root stays as ADR-004 sets it. |
| NFR-SEC-005 | The helper runs from an argv list. No shell string is built. |
