# ADR-014: Services-on entry through the supervisor relay, and the give-up state

- Status: Proposed (Decision 2 accepted by the user on 2026-10-14)
- Date: 2026-10-14

## Context

The T7.7 host gate (commit 7173e1c) left two known gaps for services-on realms. Findings F2 and F10 are recorded in `spikes/results/T7.7-services-real.md` and `spikes/results/T7.7-f10-trace.txt`. This ADR fixes both. It blocks FR-083 AC-1 (F2) and FR-081 AC-3 (F10) until the host gate re-runs.

### F2: user-session processes cannot enter a services-on realm

- Measured on the Void host (`spikes/results/T7.7-services-real.md:128`, `:133-134`): `podman exec` from a user login shell fails for a services-on realm with crun's "Permission denied" on the realm's `cgroup.procs`. The measured failing paths are `ginn exec --root`, `ginn enter`, `ginn install` and the session grant attach, plus the T8.3 rebuild attach (`:133-134`). The export sync listing (`src/ginn/export/_realm.py:57`) and the rebuild replay (`src/ginn/ops/rebuild.py:438`) run `podman exec` from the same user session by code inspection. They are not yet measured. FR-083 AC-1 fails on arch (`:22`).
- Mechanism: a login shell sits in cgroup `0::/2` (`spikes/results/T7.7-f10-trace.txt:10-11`). Moving a process into another cgroup needs write access to the `cgroup.procs` of the common ancestor of source and destination (the cgroup v2 delegation rules in the kernel admin guide). From a login shell that ancestor is the top cgroup, which is root-owned. The session therefore cannot move a process into the realm's tree.
- Inside the supervisor leaf the same exec works. A process at `0::/ginn/<uid>/supervisor` (`T7.7-f10-trace.txt:12`) enters the realm through `clone3(CLONE_INTO_CGROUP)` with no EACCES (`:42`, `:45`). The common ancestor there is `/ginn/<uid>`, which is user-owned (`T7.7-services-real.md:39`).
- S-01 does not contradict this. Its line "exec, exec -d, stop and top also succeeded" (`spikes/results/S-01.md:37`) was measured from a launcher shell that one root write had already moved into a spike leaf (`S-01.md:10`, `:172-175`). It did not test session placement. ADR-003 already flags that placement as unverified (`docs/adr/ADR-003-services-mode-cgroup-strategy.md:70`), and the S-01 citation at `ADR-003:332` needs the same caveat.
- ADR-003 line 53 names `/sys/fs/cgroup/ginn/<uid>` as the common ancestor. That does not fit a session source at `/2`, and it needs the same correction (follow-on item 2).
- The errno is not recorded. The gate has "Permission denied" from crun and no strace of the session-side failure. Gate G10 records it (O1).

### F10: a crash during a start leaves a services-on realm stopped

- Measured (`T7.7-services-real.md:21`, `:162`; `T7.7-f10-trace.txt:18-19`): five start crashes in 60 s leave the record `stopped`, with audit `service-exit gave-up, state_kept: stopped`.
- Cause: `_record_failed` (`src/ginn/ops/service.py:414-438`) moves a record only from `running`. The DR-020 table has no `stopped` to `failed` edge (`src/ginn/domain/states.py:39`; SRS "Realm state model (DR-020)", `GINN-SRS-001-v1.0.md:722-733`).
- Requirement: FR-081 AC-3 (`GINN-SRS-001-v1.0.md:383-388`) reads "Given 5 crashes within 60 s, Then the realm is left stopped and `ginn status` shows state `failed`." NFR-033 sets the same limit. This ADR reads the two halves as one state: the supervisor stops restarting the process, and the realm record reads `failed`.
- User decision (2026-10-14, roadmap `docs/roadmap.md:173`): a crash during a start counts toward the FR-081 give-up, so the realm ends `failed`.
- The counter already exists. `note_crash` (`src/ginn/services/supervise.py:34`) counts every non-zero exit of the `run` script, whether the crash came before or after `running`. Only the record write is missing.

### Clarifications (no SRS edit)

The SRS is not edited by this ADR. The wording is recorded in `docs/clarifications.md`, as the existing CL convention requires:

- CL-24: DR-020 gains `stopped` to `failed`, legal only on the supervisor's give-up. This is the user decision of 2026-10-14 for F10.
- CL-25: FR-081 AC-3 "failed" includes a realm that crash-loops at boot, with no user command (FR-084 autostart).
- CL-26: DR-010's `$XDG_RUNTIME_DIR/ginn` is satisfied by `/run/ginn/<uid>` (ADR-004, and CL-1). The logout reason is in CL-26, because ADR-004 records that the root survives logout (its line 33) but does not state why.
- CL-27: signal forwarding to in-realm processes (SIGINT, SIGTERM and SIGWINCH) on both the direct and the relay route is part of the FR-048 and FR-049 enter and launch behaviour. No SRS requirement names it.

### Constraints

- Services-off realms do not change (FR-082, NFR-002). They never start the relay, and their processes never leave the direct `podman exec` path.
- No realm runs as root, and root does not mediate user entry (CR-003, OOS-10).
- After Host Setup, ginn writes only to Owned Paths (CR-006, DR-010).
- Mutating operations stay journalled and audited (FR-122, FR-123, FR-124).
- Arguments reach podman as argv lists, never through a shell (NFR-SEC-005).
- Memory: the helpers count against NFR-002 and NFR-003, and NFR-021 (five idle realms) is already over budget under VmRSS (`docs/roadmap.md`, T7.7 finding F11).

## Decision

### Decision 1: every user-session entry into a services-on realm goes through the supervisor relay

- Rule: a process outside `/ginn/<uid>/supervisor` never moves a process into a services-on realm's cgroup. Entries from inside the leaf (`_service-run` and its start-time attach) run directly, as they do today. Services-off realms take the direct path, unchanged.

#### Routing

- The adapter takes an explicit exec target. `Podman` (the Protocol, `src/ginn/podman/adapter.py:314`) gains a required keyword `target: ExecTarget` on `exec` (`:345`) and `exec_interactive` (`:373`). It has no default, so no caller can omit it. `ExecTarget` carries the realm name, `services` (the `services_at_create` of the container being exec'd) and `mutating`. Callers read `services` from the record or plan they already hold. No per-exec inspect is made.
- `PodmanCli.exec` (`src/ginn/podman/cli.py:574`) and `exec_interactive` (`:706`) send the request to the relay when `target.services` is true and the caller is outside `/ginn/<uid>/supervisor` (read from `/proc/self/cgroup`). Otherwise they run direct, unchanged.
- `attach_at_start` (`src/ginn/runtime/attach.py:681`) receives neither the record nor `services_at_create`. It gains the same required keyword `target: ExecTarget`. Its two callers pass it: `src/ginn/ops/lifecycle.py:1109` (inside `_attach`, which holds the record, so `services` is `record.services_at_create`), and `src/ginn/ops/create.py:422` (services false, the always-off path). The change lands in one commit with the Protocol change, in T7.10, so every commit builds.
- A rebuild routes by the plan's `spec.services`. That is `self._plan.spec.services` (`src/ginn/ops/rebuild.py:308`, `:495`, `:523`), which is the mode of the new generation, and `_swap` records the same value as `services_at_create` (`:567`). `settings.services` is never used for routing.
- The signature change touches every caller, and each one is listed for T7.10.

Every caller of `exec`, `exec_interactive` or `run_step` in `src/ginn` (rg, checked 2026-10-14):

- Exec, direct: `runtime/enter.py:557`; `packages/manager.py:178` (`run_step`), `:341`, `:392`, `:610`; `runtime/attach.py:376`, `:443`, `:608`; `export/_realm.py:57`, `:78`; `ops/recovery.py:590` and `:681` (T7.10 owns `recovery.py`); `packages/aur.py` (the `exec` method at `:570-595`, through `run_step` at `:581`).
- `run_step` callers: `ops/packages.py` at `:335` (the refresh, mutating), `:346` (the install, mutating) and `:411` (the remove, mutating); `ops/update.py:155` and `:165` (`_upgrade`), and `run_check` at `:403`; `ops/estimate.py:110`, `:124` and `:237`; `ops/rebuild.py:443` (`_install_repo`); `ops/create.py:651` (`_exec`); `packages/manager.py:255` (`explicit_packages`) and `:562` (`run_check`).
- Attach and create: `ops/lifecycle.py:1109` (`attach_at_start`, `services` from the record); `ops/create.py:422` (`attach_at_start`, services false); `ops/create.py:452` (`_check_user`, services false).
- Tests that call the changed signatures, all updated in T7.10 except where marked: `tests/unit/test_podman_cli.py`, `tests/unit/test_podman_bounded.py`, `tests/unit/test_fake_podman.py`, `tests/unit/test_export_icons.py`, `tests/unit/test_ops_create.py`, `tests/unit/test_ops_estimate.py`, `tests/unit/test_packages_aur.py` (5 `super().exec` overrides), `tests/unit/test_ops_recovery.py` (2), `tests/unit/test_runtime_enter.py` (1 `exec_interactive`), `tests/unit/test_packages_manager.py` (6 `run_step` calls), `tests/unit/test_runtime_attach.py:152` (`attach_at_start`), and `tests/unit/test_ops_lifecycle.py:567` (a monkeypatch of `attach_at_start`, whose fake takes the new keyword). The fake `tests/fakes/fake_podman.py` changes in T7.10. The integration files, `tests/integration/test_lifecycle_real.py` (exec at `:153`, `:387`, `:618`, `:619`, `:1203`; `attach_at_start` at `:161`) and `tests/integration/test_tiers_real.py` (`:146`), change in T7.16, because they run only under `GINN_INTEGRATION=1`.
- Start stays on the supervisor. A services-on start already goes through `sv up` (`src/ginn/ops/lifecycle.py:793`), so the relay adds no start route.

#### Helpers, service and lifecycle

- Two helpers in `native/`: `ginn-exec-relay` (the listener) and `ginn-exec-worker` (one worker per request). Line plan under "Helper form" below.
- Service: `_exec`, a fixed name, installed in the per-user supervision directory (`$XDG_STATE_HOME/ginn/sv/_exec`, `src/ginn/store/paths.py:305-306`). The realm-name rule `^[a-z][a-z0-9-]{0,31}$` (`src/ginn/domain/names.py:17-19`) cannot produce an underscore, so no realm can collide with `_exec`. The name `ginn-exec` was rejected, because that rule can produce it. A leading-dot name was rejected too. runsvdir skips dotted names, which is why the temporary directories are dotted, so a dotted service would never start. T7.11 uses a fixed literal name, not a `RealmName`.
- Relay service sync: one helper, `sync_exec_service()`, in `src/ginn/services/exec_relay.py` (T7.11), is called after every change to the set of services-on realms. A realm is services-on when its record has `services_at_create` true or `settings.services` true. A record that fails to load counts as services-on, so the relay stays up. The helper does the following:
  - If any services-on realm exists, it renders the run script and installs or updates `sv/_exec`. It clears `down`.
  - If none exists, it writes `down` and runs `sv down` when the service is running. It never removes the service.
  - Installation builds the directory in a dotted temporary directory and renames it into place, as `write_service_dir` does (`src/ginn/services/runit_user.py:99-123`). A concurrent creator gets `ENOTEMPTY` or `EEXIST` from the rename and treats the service as present.
  - If the rendered script differs from the installed one, the new script is written to a temporary file and renamed over the old one, so the swap is atomic. The CLI restarts `_exec` after a changed script. Workers survive a listener restart.
- Call points for `sync_exec_service()`:
  - T7.11: `create.py` `_finish_services` (`:565`, the services-on create success, which resume also reaches). `create.py` undo, after the rollback removes the record (`:248` and `:253`). `ops/delete.py` `_delete`, after `store.delete` (`:353`). `ops/service.py` `set_services_mode` (`:312`). `ops/lifecycle.py` `start_realm`, for the upgrade path.
  - T7.10: `ops/rebuild.py` `_swap`, after the save (`:574`), because the swap can flip `services_at_create`. `ops/recovery.py` `_settle_locked` (`:458`), after a rebuild entry settles.
  - No change: `ops/resume.py`. Resume's success goes through create's `_finish_services`. Its `undo` (`:290-306`) only sets BROKEN and never deletes, and the replaced record keeps `services_at_create` (`:268`).
  - No change: recovery's removal paths. Recovery removes no realm record (`recovery.py` has no `store.delete` call), so it cannot remove the last services-on realm.
- Start: when the socket refuses a connection, the CLI runs `sv up` (`src/ginn/services/supervisor.py:268`) and waits up to 10 s, then reports GINN-E-RELAY-UNAVAILABLE.
- Upgrade: a realm made before this ADR has no `_exec` service. The next `ginn start` or `ginn services <realm> on` of a services-on realm runs `sync_exec_service()` through `start_realm`. Until then the route reports GINN-E-RELAY-UNAVAILABLE, with no silent fallback. T7.11 decides whether `ginn doctor` also repairs it.
- Socket: `<runtime root>/exec.sock`. The runtime root is `paths.runtime`, `/run/ginn/<uid>` or `GINN_RUNTIME_DIR` (`src/ginn/store/paths.py:129-139`), the root that ADR-004 decided. The directory must pass `check_runtime_dir` (mode 0700, owned by the uid). The socket is created under umask 077 and is 0600. The path is far under the AF_UNIX 108-byte limit.
- Bind refusal: `runtime/mounts.py` `_internal_refusal` (`:566-586`) admits any socket under the runtime root as an internal bind. It therefore refuses `exec.sock` and the `exec/` directory by name (T7.11).
- Peer checks, in the listener: `SO_PEERCRED` must name the relay's own uid, and `readlink /proc/<pid>/ns/user` must equal the relay's own user namespace. A mismatch is closed with no reply and no write.
- Undo (FR-003): `src/ginn/hostsetup/undo.py` (`run_undo` at `:249`) restores the paths recorded at Host Setup. The relay service is installed later, so it is not in the Host Setup Record, and record-based undo cannot see it. T7.11 adds a fixed undo step. If `sv/_exec` exists, undo writes `down`, runs `sv down`, waits for the process to exit, and removes the directory with the rename-away-then-remove steps of `remove_service` (`src/ginn/services/runit_user.py:150-165`). It also removes `<runtime root>/exec.sock` and the `exec/` directory. Undo already refuses while realm records exist (`_refuse_realms`, `undo.py:1196`), so the relay is removed only when no realm remains. This extends FR-003 AC-1 with one path that the record does not list, and the extension is flagged for the user.
- Environment of the relay: the run script renders `PodmanCli.global_argv()` (`src/ginn/podman/cli.py:331`, which includes `--remote=false` and `--runtime`) as the fixed prefix. It also renders the ginn-owned keys from `PodmanCli._env()` (`:363`): podman's `XDG_RUNTIME_DIR` (the runtime root), `CONTAINERS_CONF`, `CONTAINERS_STORAGE_CONF`, `TMPDIR` and the cleaned `PATH`. This is how `ginn-wait` gets its global argv (`src/ginn/cli/commands/service_run.py:38`).

#### Wire protocol

- SOCK_SEQPACKET, AF_UNIX. One message per record.
- The header `native/ginn-exec-frame.h` (T7.9) is authoritative. It defines the frame type values, the field encoding and the descriptor order, and both ends build against it. T7.10 (`runtime/exec_relay.py`) reads the header and does not re-derive the layout. If this section and the header differ, the header governs and this section is corrected.
- Frame type values (first byte of every record): `0x01` REQ (client to worker), `0x02` ACK (worker to client), `0x03` REJECT (worker to client), `0x04` SIG (client to worker), `0x05` EXIT (worker to client).
- Field encoding: integers are big-endian. A string is a `u16` byte length followed by the bytes, with no NUL, except the container id, which is 64 ASCII bytes of lowercase hex.
- REQ fields, in order: `u8` version (1); `u8` flags (bit 0 `tty`, bit 1 `interactive`, bit 2 `mutating`, bit 3 `captured`); `u8` nfds (3 or 5); the realm name (string); the container id (64 bytes); the pass-through list (`u16` count, then one string `NAME=VALUE` per entry); the argv tail (`u16` count, then one string per element). Limits: 256 argv elements, 64 KiB per record, 4 KiB per pass-through value.
- SCM_RIGHTS descriptor order, fixed: index 0 is the caller's stdin. Index 1 is the caller's stdout, which is the pipe write end for a captured run, or the caller's terminal for a tty run. Index 2 is the caller's stderr, with the same rule. Indices 3 and 4 are the read ends of the stdout and stderr pipes, present only for a captured run (`captured` set). `nfds` is 3 for a tty or non-captured run, and 5 for a captured run.
- ACK: `u32` podman client pid. REJECT: `u8` code (1 REJECTED, 2 LOCKED, 3 BUSY), then a string with the reason. SIG: `u8` signal number. EXIT: `u8` kind (0 exit status, 1 signal), then `u8` value.
- Environment split: the non-secret container values stay on the command line as `--env KEY=VALUE`, exactly as the direct route sends them. Only the `pass_env` values travel in the pass-through list, and the worker places them in podman's own process environment. The same split applies to `--env NAME` in the tail. The container's `XDG_RUNTIME_DIR=/run/user/<uid>` is a container-side value sent as `--env`. It is not podman's own `XDG_RUNTIME_DIR`, which is the runtime root.
- Podman's own process environment on the relay route is the ginn-owned keys plus the `pass_env` values. HOME, TERM and locale are not set for podman's process on this route. Gate G9 checks that podman runs without them.
- Validation (worker): `argv[0]` must be `exec`. Flag parsing stops at the first `--`, and every flag before it must come from {`--tty`, `--interactive`, `--user` (value matching `\d+(:\d+)?`), `--workdir` (absolute path), `--env` (value `NAME` or `NAME=VALUE`)}. Any other flag, `--privileged` included, is refused with REJECTED. A client-supplied `GINN_EXEC_TOKEN` in any `--env` is refused with REJECTED. The token after the first `--` is the container id, and it must be 64 lowercase hex. Everything after the container id is the in-container argv and is opaque to the worker. The worker prepends the fixed prefix itself, so no global flag can come from the request.
- Token (worker, mutating requests only): for each `mutating` request the worker generates a 128-bit random token, encodes it as 32 lowercase hex characters, and inserts `--env GINN_EXEC_TOKEN=<hex>` into podman's arguments immediately before the first `--`. The client never sends the token. The token's value is therefore visible on podman's command line to the same uid. That is accepted, because the token identifies processes and grants nothing. Non-mutating requests get no token. Only the EOF rule below uses it.
- ACK (worker to client): carries the podman client's pid. The client calls `on_spawn(pid)` (`src/ginn/podman/adapter.py:384`) and `on_ack(pid)` with it. `on_ack` is an optional callback, `Callable[[int], None] | None = None`, so existing callers that do not record a journal step need not pass it. The direct route calls both with the `Popen` pid, so the contract is the same on both routes.
- REJECT (worker to client): code REJECTED (validation) or LOCKED (exec lock held). LOCKED maps to GINN-E-REALM-BUSY.
- SIG (client to worker): one signal number. The worker sends it to the podman client's pid only, not to a process group. Every signal the client sends to the child goes this way. The client never signals the pid itself.
- EXIT (worker to client): exit status, or the terminating signal. On EXIT the client calls `on_exit`. The direct route calls `on_exit` after the wait.
- Timeouts belong to the client. The client sends SIG messages: SIGTERM at the bound, then SIGKILL 5 s later. The helpers have no timers.

#### Terminal handling

- Tty requests: the child receives the caller's fds 0, 1 and 2 unchanged. `podman exec -t` owns the real terminal, sets raw mode and copies bytes itself. The helpers copy nothing. The child is outside the caller's process group and has no controlling terminal, so it takes no job-control signal on reads or `tcsetattr`. In raw mode Ctrl-C reaches the container as a byte. Gate G3 confirms this on the host.
- Window size: the kernel sends SIGWINCH to the caller's foreground group, which is the client. The client forwards each SIGWINCH as a SIG message. The worker sends it to podman exec, which reads the window size from the shared terminal.
- Termios: the CLI saves termios before a tty request and restores it on every exit path, including error paths. A crashed podman exec does not restore it.
- Non-tty requests: the client forwards SIGINT, SIGTERM and SIGHUP as SIG messages, and keeps waiting for EXIT. Whether podman exec passes SIGINT on into the container command is O2 (gate G13). Signal forwarding to the in-realm process is the behaviour that CL-27 records for FR-048 and FR-049.
- Output: the worker leaves the pipes unread while the client is alive, so the client reads them itself. It closes its own copies of the pipe write ends after the fork, so EOF reaches the child as it should. After the client's EOF, the worker drains the read ends to `/dev/null`, so podman never gets EPIPE. The helpers never buffer. For a captured run, the client keeps its `max_output` bound (`_run_bounded`, `src/ginn/podman/cli.py:679-681`), and `stdout_tail` keeps the last 64 KiB (`STDOUT_TAIL_BYTES`, `:73`). Both rules stay in the client. `interactive` is set for `enter` (`src/ginn/runtime/enter.py:563-564`) and for an exec that takes stdin, and is off otherwise.

#### Worker lifetime, locks and crash safety

- One worker per request, a child of the listener. The worker holds the client socket, the caller's fds and the exec lock. It forks the podman exec child, then polls the socket and the child.
- Exec lock: a `mutating` request takes `flock(LOCK_EX|LOCK_NB)` on `<runtime root>/exec/<realm>.lock` before the fork. The worker creates `exec/` and the lock file when it takes the lock. The descriptor is `O_CLOEXEC` and stays with the worker only, so it never reaches podman's helpers (conmon or pause). A held lock gets a REJECT LOCKED, which the client maps to GINN-E-REALM-BUSY and never to RELAY-TIMEOUT. The worker releases the lock only after the podman client has exited and no in-container process remains, or after the bounded give-up below.
- CLI check: `mutating()` (`src/ginn/ops/_mutating.py:180`) and recovery use the same check. They open the lock file read-only, and never create `exec/` or the lock file. A missing file means no exec is running, so it is not busy. If the file exists, the check takes `flock(LOCK_SH|LOCK_NB)` and releases it at once, before the body runs. A held `LOCK_EX` fails the check with GINN-E-REALM-BUSY naming the running exec (FR-123). Recovery reports busy without blocking, as its docstring already states (`src/ginn/ops/recovery.py:19-25`). The realm lock already serialises CLI operations, so the check only has to catch execs that outlive their CLI. Because the check runs once at entry, an exec started during a body cannot be seen by that body. That cannot happen for the same realm, because a new mutating exec starts only from a CLI operation, and that operation holds the realm lock.
- Exemptions: the check applies to user-started operations. The operations named `start`, `stop`, `service-exit`, `crash` and `idle-stop` are exempt by name in `_mutating.py`. The `start` exemption needs a reason. An exec runs only inside a running container. A start that passes the state checks therefore never meets a live exec, and a supervised restart happens only after the container died, which killed its processes. The exemptions are at `lifecycle.py:892`, `:923`, `:1177`, `:1206`, and `service.py:399`, `:428`. No `user_started` flag is needed, so `lifecycle.py` changes only for the caller at `:1109`.
- A user `ginn stop` may run during an exec. That is explicit user intent, and `stop` is exempt from the check by name. The exec ends with the container: the stop kills the container's processes, the podman client exits, and the worker releases the lock.
- Orphan marker: `mutating()` and `ginn status` read `<runtime root>/exec/<realm>.orphan` when it exists, append its audit entry, and remove it. The worker writes the marker and never the store.
- EOF before EXIT: the worker (a) sends SIGHUP to the podman client for a tty request, or SIGTERM for a non-tty one, and waits 2 s. Then, for a mutating request, (b) it walks the realm's cgroup subtree, the `libpod-<id>` cgroup and its children under `/sys/fs/cgroup/ginn/<uid>/<realm>/`. For each process it opens a pidfd with `pidfd_open` and reads that process's `environ`. It matches the whole NUL-delimited entry `GINN_EXEC_TOKEN=<hex>`, using this request's token. A matching process gets SIGTERM through `pidfd_send_signal`, then SIGKILL after a short wait. (c) It repeats the walk until no tokened process remains and the podman client has exited. A fixed bound applies to the whole step, 10 s. If tokened processes still survive the bound, the worker gives up. It writes `<runtime root>/exec/<realm>.orphan` (the token, the pids and the time), reports GINN-W-RELAY-ORPHAN, and releases the lock. Gate G7 checks this for `--user` and `--root` execs.
- Non-mutating requests (an `enter` session) get no token, so they cannot be identified in the walk. For those, the tty hangup from the client's death is the only mechanism, and an in-container process of a killed `enter` session can survive. Gate G7 also covers a killed `enter` session and records what survives.
- Known limit of the token kill: a process that clears its environment escapes the kill. `sudo`, `su -` and `env -i` all do this. The token is then lost, and such a process survives until its own exit.
- Tty children: the podman client gets `PR_SET_PDEATHSIG = SIGHUP` after fork, so it dies with its worker. The worker checks its own parent after `prctl` to close the race. Non-tty children get no PDEATHSIG, so a mutating child is not killed by its worker's death. See the residual below.
- Listener death (affects new requests only): the listener's workers are not killed, and they run to completion as orphans. A new request gets connection refused, and the CLI reports GINN-E-RELAY-UNAVAILABLE after one `sv up` attempt. runit restarts the listener (AS-028). Gate G8 names the listener pid, `ginn-exec-relay`, for this kill.
- Worker death (affects one request): the client sees EOF before EXIT and reports GINN-E-RELAY-LOST, and the exit status is unknown. A tty child dies by PDEATHSIG. A non-tty mutating child keeps running, and the kernel releases the worker's exec lock when the worker dies. Overlap after a worker crash is therefore certain, not merely possible: a second mutating operation can start while the orphan still runs. Its bound is the distro package manager's own lock inside the realm (the pacman or xbps database lock). A second package operation fails on that lock instead of writing the database. No guard process is built now. This is a known residual, recorded here. Gate G8 names the worker pid, the child of the listener that holds the client socket, and measures the outcome.
- Timeouts: an interactive request has none. Package verbs keep their bounds in `src/ginn/ops/retry.py`. The export listing keeps its own `timeout_s` and output bound (`src/ginn/export/_realm.py:57-63`).
- Concurrency: the listener caps workers at 32, counting them by SIGCHLD. Over the cap it sends REJECT with reason BUSY and closes. Requests are independent except through the exec lock.

#### Helper form and line plan

- C, not Python. The listener is resident per user while a services-on realm exists, and each worker is a short-lived exec.
- `native/ginn-exec-relay.c` (the listener: socket, accept, SO_PEERCRED, namespace check, cap, SIGCHLD, exec of the worker): about 110 lines.
- `native/ginn-exec-worker.c` (REQ with fds, validation, token, lock, env, fork and exec of podman, ACK, SIG, output drain, the EOF token kill, waitpid, EXIT): at most 200 lines.
- `native/ginn-exec-frame.h` (frame types, field encoding, descriptor order, and send and receive helpers): at most 60 lines.
- Line limit: the `lines` target in `native/Makefile` counts the shared header's lines toward each helper that includes it. So the two relay helpers each carry the header in their count, and each must stay at or below 200.
- If the worker exceeds 200 lines, the EOF token kill moves into `native/ginn-exec-kill.c` (at most 200 lines, the header counted), exec'd by the worker on EOF. The rule is per helper, so this stays within it.
- `native/Makefile` lists every helper in `HELPERS`. The packaging template's `post_install` loop and `NATIVE_HELPERS` in `tests/unit/test_packaging.py` must list the same set (`:33-35`, `:196-204`). T7.9 owns all three.
- Tests: the native tests follow the existing `tests/unit/test_native_*.py` convention, as `test_native_rebind.py` and its siblings do. They are `tests/unit/test_native_exec_relay.py` and `tests/unit/test_native_exec_worker.py`, not under `tests/native/`.
- Installed path: `/usr/libexec/ginn/ginn-exec-relay` and `/usr/libexec/ginn/ginn-exec-worker`, beside `ginn-wait` (`src/ginn/services/runit_user.py:39`).

#### Memory (NFR-002, NFR-003, NFR-021)

- The relay is per user. It runs only while a services-on realm exists, so services-off realms and NFR-002 are unchanged.
- It is counted like the pause process. ADR-007 (line 36) counts the pause once per realm. The relay counts once per user, in the NFR-021 total for five idle realms. It is not added to any per-realm NFR-003 figure. Gate G11 reports both numbers.
- Estimate basis: `ginn-wait`'s measured VmRSS of 1536 KiB (1.50 MiB), a single sample (`spikes/results/T7.7-services-real.md:117`). Until G11 measures the relay, that figure stands in for it as an upper estimate.

#### Journal, audit and storage

- The relay writes nothing under the store, and writes no journal or audit record. Its only writes are the socket, the lock file, the orphan marker and the `exec/` directory under the runtime root (DR-010).
- Mutating callers keep the journal entry that the CLI opens in `mutating()`. A new step `exec-acked` is recorded from the `on_ack` callback, after the ACK. It is wired for each of the three `run_step` calls in `ops/packages.py` (`:335` refresh, `:346` install, `:411` remove), in `run_step` (`src/ginn/packages/manager.py:150`) and in the rebuild replay (`src/ginn/ops/rebuild.py:438`). T7.10 owns the wiring.
- If the CLI dies, the entry stays open. Recovery settles it after the exec lock check above, so a held lock is reported as busy and not settled.
- `enter` and the read-only execs write the CLI audit line as they do now. RELAY-LOST, RELAY-TIMEOUT and an orphan marker are audited with their codes.

### Decision 2: a supervisor give-up sets the realm to failed, from stopped or running

Exact rule:

1. A give-up is the supervisor's decision. It is reached when `note_crash` returns give-up (5 non-zero exits within 60 s, `src/ginn/services/supervise.py:34`) for a service that is not wanted down.
2. The stop mark is checked before the crash is counted. `finish_service` reads the runsv status and looks for `want down`, the mark that `sv down` sets. `parse_status` (`src/ginn/services/supervisor.py:156-169`) currently checks only `want up`, so it gains `want_down`. A finish with `want down` is a user stop. It counts no crash and gives no give-up. This replaces the current inference that exit 0 means a stop (`src/ginn/ops/service.py:198`) for user stops. Exit 0 still clears the crash log. Gate G12 checks the rule with real runsv.
3. On give-up the record moves to `failed`, from `running` or from `stopped`. From `rebuilding` the rebuild path owns the state, and the existing `state_kept` audit stays. From `failed` nothing changes.
4. A crash counts in any phase. A start that fails before the container reaches `running` counts the same as a crash of a running realm. A failed start makes `_service-run` exit non-zero (`src/ginn/cli/commands/service_run.py:34-48`), and the test covers this.
5. After a give-up the process stays down: the `down` file is kept (`service.py:212`). `ginn start` moves `failed` to `running` (DR-020) and removes the `down` file through the start path.
6. CL-24 and CL-25 record the wording, and the SRS is not edited.

Table amendment: DR-020 gains one edge, `stopped` to `failed`, legal only on give-up. Implementation: `src/ginn/domain/states.py` keeps the flat `TRANSITIONS` table with the edge in it, plus `GIVE_UP_EDGES = frozenset({(STOPPED, FAILED)})`. `transition`, `can_transition` and `assert_transition` take `give_up: bool = False`, so every existing caller builds unchanged. The edge is refused without `give_up=True`, and only `finish_service` passes it. `RealmStore.set_state` (`src/ginn/store/records.py:442-452`) runs the same check on every state write, so it must accept and forward `give_up`, with the same default. Without that, the give-up write would be refused. Every other pair behaves as before. The existing transition test in `tests/unit/test_domain_states.py` (`test_transition_table_rows_match_dr020`, `:65-67`) changes with the table: its `stopped` oracle row gains `failed`, and a new assertion checks that `can_transition(STOPPED, FAILED)` is false without `give_up`. The property test over all pairs is new (`tests/property/test_states_props.py`). T7.12 lands this together with CL-24.

## Alternatives considered

Entry-path alternatives, all rejected:

1. A login hook that moves each session into `/ginn/<uid>/sessions`. Rejected. Moving a login shell into a ginn tree needs the same root-owned common-ancestor write that fails in F2, so the hook must run as root at every login. It writes outside Owned Paths at login (CR-006, DR-010). It misses every entry that does not come through login, such as GUI launchers, cron and ssh sessions started from another session.
2. A root helper that moves processes or runs exec for the user. Rejected. Root mediating user entry widens the root surface (CR-003, OOS-10). The ancestor problem does not need root at all, because the common ancestor `/ginn/<uid>` is user-owned.
3. A transient runit service per command, which is what the T7.7 harness does. Rejected for production use. Each command waits for runsvdir to start a service, the caller's stdio is not the service's stdio so there is no tty handoff, and every command writes a service directory under Owned Paths (DR-010). It stays in the test harness.
4. One relay per services-on realm. Rejected. It multiplies resident processes, and NFR-021 is already over budget under VmRSS (F11).
5. A byte-proxying relay, where the relay owns a pty and copies bytes while the client forwards termios and window size. Rejected. Every byte takes an extra copy. Termios and window-size marshalling is needed, job-control and Ctrl-C behaviour is reimplemented, and the helper grows past 200 lines. SCM_RIGHTS hands over the real terminal instead.
6. A Python relay. Rejected for the resident helper. Interpreter RSS of about 10 MiB per user is an estimate, and helpers count against the budget (ADR-007). Python stays for the CLI and the tests.
7. Give the user write access to the top cgroup's `cgroup.procs`. Rejected. The common ancestor is `/`, which is root-owned, and ginn does not change the ownership of the cgroup root (CR-006).

Relay-design alternatives, all rejected:

8. The relay builds the whole podman command from a service name and a realm. Rejected. The client already builds the tested `exec` tail in `_exec_args`, and the relay validates that tail and adds only the fixed prefix.
9. The route decided by a per-exec `podman inspect`. Rejected. It adds a podman call to every exec, and the caller already holds the record or plan. The explicit `target` carries the same fact.
10. The route decided by `settings.services`. Rejected. A rebuild's new generation has its own mode, which the plan carries (`spec.services`).
11. Send every env value through the environment on the relay route. Rejected. Non-secret values (LANG, TZ, HOME, display variables, the container's `XDG_RUNTIME_DIR`) already sit on the direct route's command line, and the relay keeps the same argv so the two routes match. Only `pass_env` values, which may be secret, go through the environment.
12. A guard process that holds the exec lock and reaps the child, instead of the worker. Not built now. The worker's lifetime already equals the exec session, and the overlap after a worker crash is recorded as a known residual, bounded by the distro package manager's lock (Consequences).
13. A `user_started` flag on `mutating()`, set false by the supervised start. Rejected in favour of exempting `start` by name. The exemption is safe for the reason given above, and it keeps `lifecycle.py` out of the exec-lock work.

Give-up alternatives, all rejected:

14. Keep `stopped` and derive `failed` in `ginn status` from the audit log or the crash log. Rejected. The record must carry the state. `ginn status`, `ginn list` and the Control Plane read the record, and DR-020 is the source of truth.
15. A new state, `crashed`. Rejected. It widens DR-020 and every consumer, and `failed` already means what FR-081 AC-3 asks for.
16. Count only crashes after `running`. Rejected. The user decided on 2026-10-14 that start crashes count.
17. A plain `stopped` to `failed` edge with no flag. Rejected. Any caller could then fail a user-stopped realm. The flag keeps the edge to the supervisor.
18. Infer a user stop from exit 0 alone, as today. Rejected as the stop mark. An exit under `sv down` need not be 0, so the mark must come from runsv's own state (Decision 2, rule 2).

## Consequences

Positive:

- Services-on realms can be entered, package-managed, rebuilt, updated and attached from the user session. FR-083 AC-1 can pass.
- FR-081 AC-3 can pass with the record reading `failed`, and a crash at boot ends the same way (CL-25).
- Services-off realms take the same code path as before, and their tests do not change.

Negative and cost:

- One more resident helper per user while a services-on realm exists. It is counted against the budget.
- The relay adds failure states (RELAY-UNAVAILABLE, RELAY-REJECTED, RELAY-TIMEOUT, RELAY-LOST, RELAY-BUSY, and the GINN-W-RELAY-ORPHAN warning) and a waiting case for recovery.
- Overlap after a worker crash is certain, as described above. It is bounded by the distro package manager's own lock, and it is recorded as a known residual with no guard built now. Gate G8 measures it.
- The adapter signature changes, and every caller changes with it in one commit (T7.10). `_mutating.py` gains the exec-lock check in T7.13. The two commits each build on their own.
- A killed `enter` session can leave an in-container process that the token walk does not identify (Worker lifetime). Gate G7 measures it.

Must do:

- Route only from outside the leaf, and only for services-on realms. Fail closed: if the relay is unavailable, report GINN-E-RELAY-UNAVAILABLE. Never fall back silently to a session-side `podman exec`.
- The relay never writes under the store. Its orphan marker is read and audited by the CLI.
- Requests carry typed fields and an argv tail. No shell is involved. The worker refuses `--privileged`, every flag outside its allowlist, and a client-supplied `GINN_EXEC_TOKEN`.
- `pass_env` values, the only ones that may be secret, travel in the environment and never on a command line. Non-secret values keep the direct route's argv form. The token is the one value on a command line, and it grants nothing.
- The give-up edge is passed only by `finish_service` with `give_up=True`, and only after the want-down check.
- A realm-set change calls `sync_exec_service()`. The helper, its call points, and the no-change paths are listed under Helpers, service and lifecycle.

Error codes proposed (the owner of `src/ginn/errors.py` merges them, then `tools/gen_errors_doc.py` regenerates `docs/errors.md`; the catalogue is one flat sorted table, so no code range is needed):

- GINN-E-RELAY-UNAVAILABLE: the socket still refuses after `sv up` and the 10 s wait. Next step: check the `_exec` service status under the supervisor.
- GINN-E-RELAY-REJECTED: a request field failed validation. The worker sends a REJECT frame. A peer-uid or namespace mismatch gets no reply at all.
- GINN-E-RELAY-TIMEOUT: no ACK within 5 s.
- GINN-E-RELAY-LOST: the worker went away before EXIT, and the exit status is unknown.
- GINN-E-RELAY-BUSY: the listener's cap of 32 workers is reached.
- GINN-W-RELAY-ORPHAN (a warning, no exit code): after the token kill's bound, an in-container process survived. The lock is released and the orphan marker is audited on the next CLI operation.

Reused: GINN-E-REALM-BUSY (FR-123) for the exec lock, with a message that names the running exec. A held lock never surfaces as RELAY-TIMEOUT. `tests/unit/test_errors.py` (`EXPECTED_EXIT`, `:27`) gains the five GINN-E codes with their exit codes, taken from the existing exit table in `docs/errors.md`. T7.14 makes that change.

Follow-on documentation changes (listed here, not edited by this ADR):

1. Clarifications CL-24 to CL-27 are written in `docs/clarifications.md`. The SRS wording closes only when the SRS author writes it, under the existing CL convention. No SRS edit is made here.
2. ADR-003: line 53 (the common ancestor), line 70 (the "exec placement is not verified" caveat, which should cite ADR-014, G3 and G10), and line 332 (the S-01 "line 37" citation, which needs the S-01 correction). The D6 amendment in the header (lines 5 to 11) and the line-45 rule ("ginn starts a services-on realm only through its runit supervisor entry") extend to exec. ADR-003 has no numbered decision 6, so the D6 amendment line in its header is the target.
3. ADR-006: lines 10, 115 and 116 describe the pre-T8.3 state ("until T8.3 lands", the GINN-E-NOT-SUPPORTED-YET refusal, "stays partial until T8.3"). T8.3 has landed (commit 1e0dc1f), so those lines need checking against the current behaviour.
4. ADR-007: the helper list at line 31 gains `ginn-exec-relay` and `ginn-exec-worker` once G11 measures them, and the NFR-021 total gains the per-user relay term.
5. Roadmap T8.3 (`docs/roadmap.md`, the T8.3 bullet): the rebuild replay uses the relay for services-on realms. The T8.3 bullet has no numbered steps, so there is no "step 3" to edit. The replay is `_Rebuild._replay` (`src/ginn/ops/rebuild.py:422-438`), and the restart is `_restart` (`:776`).
6. `spikes/results/S-01.md`: add an addendum that the exec claim at line 37 is not a session-placement measurement. Do not rewrite the spike record.
7. `tests/integration/test_services_real.py`: remove the KnownGap markers for F2, F7 and F10 once G1, G2 and G12 pass.
8. Existing tests in `tests/unit/test_podman_cli.py` (`:839`, `:850`, `:875`) and `tests/unit/test_runtime_enter.py` (`:1348`, on the signal-forwarding test at `:1350`) cite FR-068 for on_spawn and signal behaviour. That citation is wrong, because FR-068 is the D-Bus filter. Correcting it is a separate change, outside this ADR, and the new tests cite FR-048, FR-049 and CL-27 instead.

Open questions:

- O1: the session-side errno. Gate G10 runs strace on a failing `podman exec` from a session shell.
- O2: does `podman exec` pass SIGINT into the container command on a non-tty request? Gate G13.
- O5: environment parity. Gate G9 checks that the relay's `podman ps` and the session's agree, that the S-05 pause lookup resolves the same way from both, and that podman runs without HOME, TERM and locale on the relay route.
- O8: the in-container kill. The expected result is that it works for `--root` execs too. The owner of the rootless user namespace holds CAP_KILL and ptrace rights over the processes of the child namespaces it created, so signals and environ reads should succeed for subuid-mapped root processes. Gate G7 confirms this. Processes that clear their environment (`sudo`, `su -`, `env -i`) escape the token kill whatever the result. If G7 disproves the expected result, G7 decides what replaces the mechanism.

Closed by the review of 2026-10-14:

- O3: `ginn stop` on a services-on realm ran from the session throughout T7.7. `podman stop` moves no process. The restart goes through `start_realm` and `sv up`, not a session `podman start` (`src/ginn/ops/lifecycle.py:1206-1207`; `src/ginn/ops/rebuild.py:776`). No services-on `podman start` or `podman stop` runs from the session.
- O4: a tty child dies with its worker (PDEATHSIG). Whether the in-container process also ends after a worker crash is part of G7 and G8.
- O6: DR-010's wording is satisfied by `/run/ginn/<uid>` per ADR-004 and CL-26.
- O7: the stop mark is runsv's `want down` (Decision 2, rule 2).
- O9: closed with no change. The direct route keeps `--env KEY=VALUE` on argv, as it does today. The relay route keeps the same argv form for non-secret values, so the two routes match (see Routing).

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-003 | Host Setup undo removes the relay service, which the Host Setup Record does not list (Undo, under Helpers, service and lifecycle). |
| FR-014 | `ops/estimate.py` (the Kali everything estimate, CL-22) routes its `run_step` execs through the target. |
| FR-021, FR-022 | The rebuild replay and the rebuild attach route through the relay, by the plan's `spec.services`. |
| FR-023, FR-024 | `ops/update.py` (the manual update and the availability check) routes its `run_step` and `run_check` execs through the target. |
| FR-040 to FR-043 | The package verbs (install, remove, update) run their `podman exec` through the relay, with the `exec-acked` journal step. |
| FR-044 | The export sync listing routes through the relay for a services-on realm. |
| FR-048 | `ginn enter` routes through the relay for a services-on realm, with a tty handoff. The on_spawn and on_exit contract is tested under this requirement. Signal forwarding is CL-27. |
| FR-049 | `ginn exec` and a launched app route through the relay, and the exit code comes back over EXIT. The on_spawn and on_exit tests cite FR-049. Signal forwarding to the in-realm process (SIGINT, SIGTERM and SIGWINCH) on both routes is part of this requirement's behaviour, as CL-27 records. |
| FR-061, FR-065 | The session grant re-attach runs its in-container steps through the relay, with the same paths. |
| FR-080 | `ginn exec <realm> -- systemctl ...` (AC-1) is carried by the relay. |
| FR-081 | Supervision and give-up. Decision 2 sets `failed` on give-up, after the want-down check. CL-25 covers a crash loop at boot. |
| FR-082 | Services-off realms take the unchanged direct path, and never start the relay. |
| FR-083 | The services change and the F2 gate. The relay makes the FR-083 AC-1 attach possible. |
| FR-084 | The `_exec` service starts with the supervisor, without a login session. |
| FR-122 | An interrupted mutating exec leaves a journal entry that recovery settles, after the exec lock check. |
| FR-123 | The exec lock makes a second mutating operation on the realm fail with GINN-E-REALM-BUSY, and recovery reports busy. |
| FR-124 | Relay outcomes (RELAY-LOST, RELAY-TIMEOUT, the orphan marker) are audited by the CLI, and the give-up audit names the state change. |
| AS-028 | runit restarts the listener, and the crash and give-up policy is unchanged. |
| NFR-002, NFR-003, NFR-021 | The relay is counted once per user in the NFR-021 total, and gate G11 measures it. It is not charged to the NFR-003 per-realm figure. |
| NFR-033 | 5 crashes in 60 s give up. Decision 2 counts start-phase crashes and skips user stops. |
| NFR-SEC-005 | The typed request, the argv tail, the fixed prefix, no shell, the rule that only pass_env values go through the environment, and the refusal of a client-supplied token. |
| NFR-040 | The new RELAY codes and the W code go into the error catalogue. |
| CR-003, OOS-10 | No root realm, and no root mediation of user entry. |
| CR-006 | After Host Setup, only Owned Paths are written. The socket, the lock, the orphan marker and `exec/` are under the runtime root. |
| DR-010 | The socket, the exec lock, the orphan marker and `exec/` sit under the runtime root, which is an Owned Path. CL-26 records that `/run/ginn/<uid>` satisfies the DR-010 wording. |
| DR-020 | The state table gains the `stopped` to `failed` edge, legal only on give-up (CL-24). |
| DR-032 | Relay outcomes are recorded after the operation starts, so the order holds. |

## Implementation plan

The tasks, files, tests and host gates are in `docs/roadmap.md`, section "Branch services-supervisor-relay (F2, F10, ADR-014)". The waves there keep every file owned by one task per wave, and each commit builds on its own.
