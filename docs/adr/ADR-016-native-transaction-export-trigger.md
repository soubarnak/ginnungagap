# ADR-016: Export trigger for native package transactions

- Status: Proposed. The code follows it and is unit-tested on fakes. Nothing here ran on a host: the hooks, the stamp write by container root and the 5 s bound wait for the F14 host gate (`tests/integration/test_export_real.py`). Reviewed 2026-10-20: the fedora plugin leaves the Integration Package Set (CL-35), the stamp moved to `~/.local/state/ginn-export` and the host holds it by descriptor.
- Date: 2026-10-20

## Context

Finding F14: the host menu follows a realm only through `ginn install`, `ginn remove`, `ginn update`, a rebuild and a manual `ginn _sync` (`_sync_menu`, T5.3). A package that the user installs with the realm's own package manager does not reach the menu, and one removed that way stays in it.

- FR-044 AC-1 (Must): `pacman -S firefox` inside realm `arch` gives `~/.local/share/applications/ginn-arch-firefox.desktop` within 5 s.
- FR-045 AC-1 (Must): an application removed by any method has its entry and icon removed within 5 s.
- NFR-009 (Must): the sync bound is 5 s.
- `native/ginn-watch.c` (T5.9) exists and has a test, but nothing starts it. The roadmap leaves open "where it starts and stops, and which path it watches", because the realm's `/usr/share/applications` sits in the realm root and the host cannot watch it by a path of its own.

Constraint (hard): the host sees a change made inside the realm root without reading podman storage internals. No overlay upperdir, no `podman mount` and no fuse-overlayfs assumption: none of them can be checked without a host, and each one ties ginn to a storage driver (ADR-002, CR-006).

Other forces:

- NFR-001, NFR-002 and NFR-003 count every process attributable to ginn. ADR-007 already lists `ginn-watch` as a counted helper. A realm that is not running has no helper. A running realm has at most the processes of ADR-007, so a new in-realm resident process is the more expensive choice.
- ADR-003 and ADR-014: a services-on realm starts under its runit service (`ginn _service-run` runs the start inside the supervisor leaf), and exec from a user session goes through the relay. Anything that runs `ginn _sync` from a process started by the supervised start is in the leaf, so its execs take the direct route.
- ADR-015: only a services-on realm has a derived image. A services-off realm runs the pinned image, so no hook can be baked into an image for it. Every realm must get its hook by a step on the running container.
- Findings of this session (not measured): `ginn enter` and `ginn exec` use `ExecTarget(mutating=False)`, so a user session in the realm holds no exec lock and does not make `ginn _sync` fail with GINN-E-REALM-BUSY. `ginn install`, `ginn update` and `ginn remove` hold both locks, and each syncs the menu itself at the end.
- Unknown until a host run: that container root can write the stamp (it is a file in a directory of the realm user, so it needs CAP_DAC_OVERRIDE, which podman grants by default), that each package manager runs its hook in the pinned image, and that `libdnf5-plugin-actions` installs on `fedora-minimal:44`. Each is listed under Consequences.
- FR-013 AC-1 and CR-007 limit what ginn adds to a realm to the documented Integration Package Set. A package that the signal needs (dnf5's actions plugin) is not on it, and the hook files in the realm's `/etc` are a second kind of content that CR-007 does not name (CL-10 gives the reading for the files of a services-on realm). CL-35 records both.

## Decision

The realm signals the host, and the host runs the sync. Three parts.

1. A hook of the realm's package manager touches a stamp file after every transaction. The stamp is `<Realm Home>/.local/state/ginn-export/transaction.desktop` (the XDG state directory, which cache cleaners leave alone). The Realm Home is a host directory under Owned Paths that every realm already has bound at its home path (FR-048, `runtime/mounts.py` `create_binds`). So the host sees the stamp through its own path, and nothing new is bound.
2. `ginn-watch` runs on the host on that directory, for as long as the container's init lives. A touch runs `<ginn> _sync <realm>` after a quiet time. `native/ginn-watch.c` gains `--exit-with <pid>` (exit 0 once that process is gone, through a `pidfd_open` descriptor in the same `poll`, or a one-second `kill(0)` check on a kernel without it, so a stop and an idle stop need no signal) and a bounded retry (a run that fails is tried again `max` ms later, at most four times, for a realm busy with another operation). The file stays within 200 lines.
3. `ginn _sync` re-reads the realm through ginn (export/sync.py). The stamp's content and name are never used, so the realm cannot steer the sync (NFR-SEC-005).

Per-distribution hooks are data (NFR-060), in the new optional `[export_hook]` table of `distros/data/<id>.toml`:

| Distribution | File inside the realm | Trigger |
|---|---|---|
| arch | `/etc/pacman.d/hooks/ginn-export.hook` | `Type = Path`, `Target = usr/share/applications/*.desktop`, `Operation = Install, Upgrade, Remove`, `When = PostTransaction`, `Exec = /usr/bin/touch <stamp>` |
| debian, kali | `/etc/apt/apt.conf.d/99ginn-export` | `DPkg::Post-Invoke { "/usr/bin/touch <stamp> 2>/dev/null \|\| true"; };` |
| fedora | `/etc/dnf/libdnf5-plugins/actions.d/ginn-export.actions` | `post_transaction::::/usr/bin/touch <stamp>` (no shell and no quote, so nothing depends on how dnf5 splits the command; whether dnf5 runs it as an argv is unverified, see below). The file is inert unless the realm has `libdnf5-plugin-actions`, which is **not** in the Integration Package Set (FR-013, CR-007; CL-35). A Fedora user who wants the native path installs it: `ginn install fedora libdnf5-plugin-actions` |

No hook makes the directory. Container root is a subordinate id on the host, so a directory or stamp it made would be owned by an id the host user cannot manage. Only the host (`rearm`, with `mkdirat`) and the realm-user exec of `ensure_hooks` make it, and the host makes the stamp file itself (`own_stamp_files`, at every arm and re-arm), so the hook only ever touches an existing file; container root keeps its owner (`CAP_DAC_OVERRIDE`). A stamp or marker owned by another uid is unlinked and made again (the directory is the host user's, so the unlink works), and the realm-user script removes both before it creates them. If the realm removed the directory, `touch` fails: apt ignores it (`|| true`), pacman and dnf5 should only log it (dnf5 unverified). The stamp path is the realm's home path plus a fixed suffix. It is inserted by the loader's `{stamp}` placeholder and checked against `/[A-Za-z0-9_./-]+`. A home path with other characters gets no hook and a warning, so no quote, blank or `$` ever reaches a hook line (NFR-SEC-005). The hook is a fixed command; no realm name or package name is in it. `ensure_hooks` writes the hook file to `<path>.tmp` and moves it over the hook, so a package manager never reads half a file. A failed `touch` never fails a transaction: apt has `|| true`, and pacman and dnf5 only log a failed hook.

Debounce. `ginn-watch` runs with quiet 500 ms and max 1500 ms (`export/watch.py` `QUIET_MS`, `MAX_MS`). A transaction that touches the stamp once gives a run 500 ms later, which leaves 4.5 s for `ginn _sync` under NFR-009. A long transaction that touches it often gives one run per 1500 ms at most. The spam bound for a hostile realm is the same: at most one sync per 1500 ms (plus four retries after a failure), each bounded by the sync's own caps (FR-044). That bound still lets a realm that touches the stamp in a loop keep `ginn _sync` and its audit entry running, so `ginn-watch` also spaces its runs: after six runs less than 60 s apart each, the next waits `max` ms, then twice that, and so on up to 60 s, and a run after a quiet minute starts the count over (`native/ginn-watch.c` `FREE_RUNS`, `RAPID_MS`). A real install burst is one run, so ordinary use never meets the spacing. Only successful runs count, and only more than ten in a row less than 60 s apart: a failed run or a retry (a realm busy with an update) never counts, so the next transaction after a refused sync runs at once.

Lifecycle. Every start that reaches `running` calls `_arm_export_watch` (ops/lifecycle.py), right after the idle arm and inside the same `mutating` block, for services-off and services-on alike:

- `ensure_hooks` installs the hook once per container. It writes the hook file as container root (one `sh -c` with the path and text as argv), then, as the realm user, the stamp directory, the stamp and last a marker `hooks-for` that holds the container id. A start whose container id equals the marker's runs no exec. A rebuilt container (a new id) and a realm made before this ADR get their hooks at the next start. This is the migration: no rebuild is needed for debian, kali and arch.
- `arm` starts `ginn-watch --exit-with <init pid> /proc/self/fd/<n> 500 1500 <ginn> _sync <realm>` detached, with its stderr in `runtime/watch/<realm>.log` and a record `runtime/watch/<realm>.json` (pid and start time of the watcher and of the init), the shape of the idle watcher (`runtime/idle.py`, whose `spawn_detached` it shares). A start whose record names a live watcher for the same init starts none. A live watcher of another init is sent SIGTERM first.
- A stop needs no code. The init exits, and the watcher exits within one second. The same holds for an idle stop (`ginn-idle` runs `podman stop` itself), a crash and a services-on stop through `sv down`.
- Services-on. The supervised start (`ginn _service-run`) runs `_start` in the supervisor leaf, so the watcher is started there at boot and after a runit restart. Its `ginn _sync` execs from the leaf take the direct route. When `ginn _sync` runs from outside the leaf it takes the relay, as every sync does (ADR-014).
- Every failure is a GINN-W-EXPORT-SYNC warning with the reason, and the start stays a success. It is the existing code for "the menu is not fully synced", and its remedy (`ginn _sync <realm>`) is the right one. No new error code is needed. A watcher is not started when the hook could not be installed.
- The ginn command is `hooks.watch.program`, which `cli/_context.py` sets from `ginn_program` in both `lifecycle_hooks` and the lifecycle of a rebuild's restart (`_base_lifecycle`), so a rebuilt running realm gets its watcher too. It is not borrowed from `hooks.rebuild`. A relative or missing path is a warning, not a bare name looked up on a sanitised PATH.
- The hook exec runs as `uid:gid` of the realm user (`exec_user`), as `ginn enter` does.
- Re-arm. If the realm removes the stamp directory, `ginn-watch` runs once more and exits 3. That run is `ginn _sync`, whose `sync_realm` calls `rearm` (export/watch.py) inside its `mutating` block, under the realm lock, with the watch settings passed in `SyncHooks.watch`, and reports a failure through `mutation.warnings`. `rearm` makes the directory again, below the descriptor walk and with mode 0700, and starts a fresh watcher, so the export lasts until the next start. It refuses more than three re-arms in 60 s with a GINN-W-EXPORT-SYNC warning (the count and time are in the watcher record), so a realm that removes the directory in a loop cannot get around the spacing in `ginn-watch`. `rearm` tells a watcher that lost its directory from a healthy one by the inotify watch it still holds (`/proc/<pid>/fdinfo`), not by the inode number, which a quick `rmdir` and `mkdir` can reuse.

Host-side path safety. The realm controls the directories that hold the stamp. A link in `.local` or `state` that points at a host directory would make a host-side watch or mkdir act outside Owned Paths. So the host walks from the resolved Realm Home one component at a time with `openat(O_PATH | O_NOFOLLOW | O_DIRECTORY)` (`export/watch.py` `open_stamp_dir`); a link anywhere gives no directory, and nothing is watched or read. What the host does afterwards it does through that descriptor, not through the path again: `ginn-watch` inherits the descriptor (`pass_fds`), watches `/proc/self/fd/<n>` and closes the inherited descriptor once the watch exists, so the command it runs does not get it, and the marker is read with `openat` from it with `O_NOFOLLOW`, as a regular file of at most 256 bytes. A component that the realm swaps for a link after the walk therefore changes nothing that the host holds (no check-then-use gap). The marker only decides whether to skip the install; the worst a realm does with it is skip its own hooks. CR-006 limits what the host writes outside Owned Paths. The host writes into the realm's directories only in `rearm`, with `mkdirat` below a descriptor of that walk. The realm's files stay the realm's: the guarantee is about the host's writes, not about reads of a directory that the realm owns.

## Alternatives considered

- Watch the realm's `/usr/share/applications` on the host through podman storage (overlay upperdir, `podman mount`, fuse-overlayfs). It needs no hook, so it catches every change. Rejected: it reads storage internals, which is the stated constraint, it depends on the storage driver and on mount permissions of a rootless user, and none of it can be verified without a host.
- A ginn-owned directory under the runtime root, bound into the realm (the brief's preferred form). The mechanism is the same signal. Rejected for this ADR because a bind is fixed at `podman create`, so every existing realm would need a rebuild, or an attach-time bind with a new internal kind (`_internal_refusal` accepts only sockets, and the attach route is already the most delicate path of a start, ADR-008). The Realm Home is a ginn-owned host directory that every realm already has, so the hook can be installed at the next start of any realm. The cost of the choice: the stamp sits in the user's realm home (`~/.local/state/ginn-export`, a state directory), and `ginn delete --keep-home` leaves it with the home. A later ADR may move it to a runtime-root bind when a rebuild is acceptable.
- An in-realm inotify process (a resident watcher in the realm, signalling over a socket). Needs no package-manager hook and catches `dpkg -i`. Rejected: a resident process per running realm adds idle RSS against NFR-001, NFR-002 and NFR-003 (ADR-007 already holds the budget at its limit; NFR-021 is over it), and it needs a tool in every image.
- Poll the realm from the host (`podman exec find` each few seconds). Rejected: it starts a process in every realm every few seconds, it breaks the idle stop (the exec is a user process), and it cannot meet 5 s without a poll shorter than that.
- An rpm file trigger or a dpkg trigger shipped in a ginn package inside the realm. It would catch `dpkg -i` and `rpm -i` too. Rejected: it needs a built package per distribution and an install step, and the hooks above need only a file.
- A pacman hook with `Operation` limited to `Install` and `Remove`. Rejected: an upgrade changes the entries (names, icons, Exec), and FR-044 syncs changed entries too.
- Extend `ginn-watch` to watch the stamp directory without a `.desktop` suffix. Rejected: it changes the filter that the unit tests and ADR-007 rely on. The stamp is named `transaction.desktop`, and the marker has no such suffix.

## Consequences

Positive:

- FR-044 AC-1 and FR-045 AC-1 hold for `pacman`, `apt` and `dnf5` run by anyone inside the realm, if the hooks run as designed (host gate F14).
- No new bind, no read of podman storage, no resident process in the realm. The host cost is one `ginn-watch` per running realm, which exits with the realm. Its RSS is not measured; ADR-007 already counts the helper, and `tools/measure/nfr-001-idle-processes.py` lists it among the helpers it checks. T7.7's memory gate must include it before NFR-003 is claimed.
- A failed signal or sync never fails a package transaction or a start (GINN-W-EXPORT-SYNC).

Negative, and what the code must not do:

- `dpkg -i`, `rpm -i` and a package built and installed by hand with `pacman -U` are covered only where the manager's hook runs: pacman yes, apt no for a bare `dpkg`, dnf5 yes, `rpm -i` no. The next `ginn _sync` or any `ginn install` catches them. Not a requirement gap: FR-044 names the package manager.
- Existing realms get the signal at their next start. A running realm keeps working as before until then.
- Fedora. The plugin is not in the Integration Package Set, so on a new Fedora realm the dnf5 hook file is inert and a native `dnf install` is not exported (FR-044 AC-1 and FR-045 AC-1 are not met there by default; CL-35 records this, and no compliance is claimed). The user can install the plugin: `ginn install fedora libdnf5-plugin-actions`. The hook file is already in `/etc/dnf/libdnf5-plugins/actions.d/`, and dnf5 reads its plugins and their configuration at the start of each run, so the first transaction after the install should be signalled without a restart of the realm. That is the design's expectation and not measured; the install transaction itself is not signalled, and `ginn install` syncs the menu itself at its end. The host gate checks it.
- A `ginn _sync` that the watcher runs while `ginn update` or `ginn install` holds the realm lock is refused with GINN-E-REALM-BUSY. The retry covers a short hold. A long operation syncs by itself when it ends (T5.3, update.py), so nothing is lost. A sync that the integrity check refuses (GINN-E-HOST-UNACKED) is lost after the retries; `ginn integrity` shows it.
- The stamp, the marker and the directories belong to the realm user, which can remove or replace them. Result: its own hooks stop signalling until the next start (a different container id) or until it restores the marker. No other realm and no host path is reached (see path safety).
- Delete. `ops/delete.py` calls `export.watch.remove` beside `idle.remove`, so the watcher's record and log leave with the realm. A root-made stamp directory in a kept home belongs to the realm's mapped ids, not to the host user.

Follow-up and host-only items (nothing below was run):

- F14 host gate in `tests/integration/test_export_real.py`: `pacman -S`, `apt install` and `dnf install` inside a started realm, with the 5 s timing (NFR-009), and the removal case (FR-045). The expected failures there were once named F-WATCH; they are named F14 and the marker is removed after a pass.
- Per image: that the hook file is read (pacman `HookDir`, apt `apt.conf.d`, dnf5 actions plugin enabled by default, `libdnf5-plugin-actions` installable, read by the first dnf5 run after the user installs it, and that dnf5's actions plugin runs the plain `touch <stamp>` command as written, with no shell and no quote handling, and does not fail the transaction when the touch fails), that `touch` as container root can write the realm user's stamp under keep-id (`CAP_DAC_OVERRIDE`), and that `ginn-watch` sees the close.
- Boot-time autostart: the supervised start's environment (PATH, HOME) when it spawns `ginn-watch`, and that the watcher outlives `sv down` only until the init exits.
- Memory: `ginn-watch` RSS per running realm (NFR-002, NFR-003, ADR-007).
- Risk R-001 (Appendix D) is not touched. Risk of drift: the stamp path is in three places (`export/watch.py`, the hook files rendered from it, the host watch). The unit tests render each distribution's hook and compare the stamp.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-044 | A native install inside the realm touches the stamp, the host watcher runs `ginn _sync`, and the entry is written within the 5 s bound. |
| FR-045 | A native removal touches the stamp the same way, and the sync removes the entry and its icon copy. |
| NFR-009 | Quiet 500 ms and max 1500 ms leave at least 3.5 s for the sync; the host gate measures it. |
| NFR-001 | The signal needs no resident process in the realm; the host helper exits with the realm. |
| NFR-002 | A stopped or idle-stopped realm has no helper; a running one has one `ginn-watch` (RSS to be measured). |
| NFR-003 | The same single helper counts against the services-on budget (ADR-007). |
| NFR-060 | The hooks are data in `distros/data/*.toml`; a distribution without `[export_hook]` gets no native signal and needs no code. |
| NFR-SEC-005 | Fixed argv and fixed hook text; the stamp path is inserted only after a character check; the stamp content is never read. |
| CR-006 | The host writes nothing outside Owned Paths: the realm user creates the stamp directories in the realm, and the host checks the resolved path before it watches. |
| ADR-003 | A services-on realm's watcher is started by the supervised start, in the supervisor leaf. |
| ADR-007 | `ginn-watch` stays the budgeted per-realm helper; no new resident process. |
| ADR-014 | `ginn _sync` routes through the relay by `ExecTarget.services`; the hook install uses the same target. |
| ADR-015 | The hook is installed on the running container, so a services-off realm (no derived image) gets it too. |
| FR-013 | The Integration Package Set stays as FR-013 lists it: fedora's actions plugin is not added (CL-35). |
| CR-007 | The hook files in the realm's `/etc` are content that CR-007 does not name (CL-35, CL-10). |
