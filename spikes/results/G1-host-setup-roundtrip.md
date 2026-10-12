# G1: Host Setup round trip on a Void host (setup, doctor, undo)

Collected 2026-10-12 (local time, 22:22 to 22:24). Commit under test: `4a3b508`. Requirements: FR-001, FR-002, FR-003, FR-007, DR-009, DR-010, NFR-030. Decisions and clarifications: ADR-013 Decisions 11, 14, 15 (i) and 25, CL-11, CL-13.

Method. One real round trip on a Void host as user `soubarna` (uid 1000), with no realm created. The `sudo` commands ran on the host from the user's session. The output files are in the agent scratch directory `gate1/` (not committed). The agent that wrote this note ran no command on the host and read the output files only. The snapshots are read-only listings of the host paths that Host Setup touches (`gate1/snap.sh`): the subid files with sha256, `/etc/ginn`, `/etc/sv`, `/var/service`, `/run/runit`, `/run/ginn`, `/run/lock`, the cgroup tree, the ginn processes and the user's state directory.

Evidence grades. "Saved" means the output is in a file in `gate1/`. "Run report" means the orchestrator reported it and no file holds it. The code changes in section 6 are not in `4a3b508`, and no result from them was observed on the host.

The commands and their output are as they ran at `4a3b508`. Only the first doctor run and the undo report were observed on the host at that commit, and both are saved. Every statement in section 3 or 6 about the new behaviour comes from the code and its unit tests, and it was not re-run on the host.

## 1. Steps

1. Before snapshot (saved). No ginn path existed. `/sys/fs/cgroup/ginn` and `/sys/fs/cgroup/ginn/1000` already existed, owned by root and by soubarna.
2. `sudo ginn host-setup --user soubarna` (saved, `setup.out`): exit 0. Output: `host setup complete; record /etc/ginn/setup-soubarna.json lists 11 paths`. The record is in `/etc/ginn`, mode 0600.
3. After-setup snapshot, 8 s after setup (saved). The service `ginn-soubarna` was up as a root `runsv`. No user `runsvdir` was running.
4. `ginn doctor` as the user, first run (saved, `doctor.out`). It exited non-zero. See section 3.
5. `mkdir -p ~/.local/state/ginn/sv`, as the user. Run report only: the user's `runsvdir` started within 3 s.
6. `ginn doctor` again. Run report only: 16 PASS, 1 WARN, 0 FAIL. Nothing in `gate1/` holds it.
7. A probe service `exec sleep 100000` was added under the user's sv directory, so that undo would have to wait for a live child.
8. `sudo ginn host-setup --undo --user soubarna` (saved, `undo.out`): exit 0. Run report only: 2.4 s. Output: `host setup undone; 4 path(s) left in place`. Run report only: the probe `runsv` and `sleep` drained, so runit's finish script worked. The after-undo snapshot does not show the probe service's state, because it does not list the sv directory's contents.
9. After-undo snapshot (saved). Then the test files under `~/.local/state/ginn` were removed by hand. The run report does not say which files, and the snapshot predates the removal.

## 2. Before and after

| Path | Before | After setup | After undo |
|---|---|---|---|
| `/etc/subuid` | 90 bytes, sha256 `bdce0d6f…` | 114 bytes, `25cab095…`; appended `soubarna:1065536:524288` | 90 bytes, `bdce0d6f…` (identical) |
| `/etc/subgid` | 160 bytes, sha256 `0ab28a23…` | 184 bytes, `927a4ebd…`; appended `soubarna:1065536:524288` | 160 bytes, `0ab28a23…` (identical) |
| `/etc/ginn` | absent | `setup-soubarna.json` (0600), `shared.json` (0600) | absent |
| `/etc/sv/ginn-soubarna` | absent | `run`, `finish`, `log/run`, `supervise` link | absent |
| `/var/service/ginn-soubarna` | absent | link to `/etc/sv/ginn-soubarna` | absent |
| `/run/runit/supervise.ginn-soubarna{,-log}` | absent | created, root 0700 | absent |
| `/run/ginn/1000` | absent | created, soubarna 0700 | absent |
| `/run/lock/ginn-host-setup.lock` | absent | root 0600, 0 bytes | present (by design, ADR-013 Decision 11) |
| `/sys/fs/cgroup/ginn/1000/supervisor` | absent | created, root-owned leaf | absent |
| `/sys/fs/cgroup/ginn`, `…/ginn/1000` | present | present | present (pre-image present) |
| root `cgroup.subtree_control` | `cpuset cpu io memory pids` | same | same |
| `~/.local/state/ginn` (state root, user) | absent | absent | present, soubarna 0755, mtime 22:23. Not an undo residue, see section 5 |
| `~/.local/state/ginn/sv` (user) | absent | absent | present, soubarna 0755, mtime 22:23, one subdirectory (probably the probe service; the snapshot does not list it). Made by hand with `mkdir -p`: test residue, not undo residue |
| `~/.local/state/ginn/log` (user) | absent | absent | present, soubarna 0755, mtime 22:23, one subdirectory. Created at run time by the log service. Its `log/supervisor` is the log script's `LOG_DIR`, and the snapshot does not list it |

The setup appended `soubarna:1065536:524288` to both subid files. The line that was there before, `soubarna:100000:65536`, is smaller than 524288, so setup could not reuse it. The record lists 11 paths (count from `setup.out`). The record's contents were not captured in the evidence, because that `cat` went to the terminal, so this note does not list them.

Process table. After setup, a root `runsv ginn-soubarna` was up, and no user `runsvdir` was. After undo, neither was running. The after-setup process listing filtered out anything without `ginn` or a `runsv`/`runsvdir` name, so a run script child (`/bin/sh ./run`) would not appear in it, and this note does not claim to know whether one was there.

## 3. Doctor output

First run, after setup, at `4a3b508` (saved, `doctor.out`; verbatim lines that matter):

```
GINN-E-DOCTOR-FAILED: 1 of 17 doctor checks failed
WARN  podman: ginn's podman config is not written yet; ginn writes it on its first podman call, so rootless podman is not verified yet
FAIL  runsvdir: the ginn supervisor (runsvdir on .../ginn/sv) is not running for soubarna
  fix: sudo sv up /var/service/ginn-soubarna
summary: 15 PASS, 1 WARN, 1 FAIL, 0 INFO
```

Cause. The run script (`hostsetup/runit_system.py`, step 4) loops while `<home>/.local/state/ginn/sv` does not exist. It waits for an encrypted home that is not mounted yet. Setup does not create that directory, so the service stayed in the wait loop with `runsv` up and no `runsvdir`. The printed fix, `sudo sv up`, does nothing to a service that is already up, so that fix was wrong.

Change after the run (working tree, by code and unit test, not re-run on the host). The rules now are these.

- The service is up only when runit runs its run script. That means a root `runsv` for `ginn-<user>` (uid 0 only) with a `./run` child (`_service_up`). A runsv with no such child is down, a FAIL with `sudo sv up`. That covers `sv down`, which leaves runsv running with no child, and the about one second in which runsv waits to restart a run script that exited. A check made in that second reports the service down. A rerun shows the state that holds. The unit test `test_runsv_without_its_run_script_is_down_and_gets_sv_up` covers the no-child and other-child cases.
- The run child is matched by argv (`/bin/sh ./run`). The log script has the same argv until it execs `svlogd`, so a log child can match for the moment before that exec. That window is not measured.
- The directory checked is `<home>/.local/state/ginn/sv`, from the account's home, as setup's run script has it. It is not taken from the environment.
- Missing directory, with the service up: a WARN, the supervisor is idle until the directory exists, and the fix is `mkdir -m 0700 -p <path>`, run as the user. Host Setup does not create it.
- Path that exists but is not a directory (file or link, dangling or not): a FAIL, with `rm <path>` and then `mkdir -m 0700 -p <path>` as the fixes. The run script never finds the directory it waits for.
- Directory present, service up: a FAIL with `sudo sv restart`, because `sv up` does nothing to a running service.
- If doctor's own `XDG_STATE_HOME` names another state directory, the messages for the missing and non-directory cases say so (open item 2).

Expected result of the first run after this change, from the code: 15 PASS, 2 WARN, 0 FAIL, provided the run script child was there. The evidence does not show that child. The rule above depends on it, and the observed first run cannot tell the two states apart. The unit test `test_idle_supervisor_leaves_the_whole_run_without_a_fail` pins the whole-run result for the child-present case.

## 4. Undo report

Observed on the host at `4a3b508` (saved, `gate1/undo.out`, which predates the wording fix below):

```
soubarna: host setup undone; 4 path(s) left in place
left sys/fs/cgroup/ginn/1000: pre-image present
left etc/subgid: differing line kept, not the recorded range: soubarna:100000:65536
left etc/subuid: differing line kept, not the recorded range: soubarna:100000:65536
left sys/fs/cgroup/ginn: pre-image present
```

The two cgroup paths existed before setup, so keeping them is correct. The two subid entries were the user's own line from before setup, and the subid files are byte-identical to their pre-setup content after undo. The old reason text read like a conflict.

Wording now in the code, by code change and unit test (`test_a_line_the_user_held_before_setup_is_kept_and_reported_as_pre_existing`), not re-run on the host. For the same case, the two subid lines read:

```
left etc/subgid: the user's line from before setup kept, setup's appended line removed: soubarna:100000:65536
left etc/subuid: the user's line from before setup kept, setup's appended line removed: soubarna:100000:65536
```

If setup's line was already absent from the file, the text says `setup's appended line was not in the file` instead. A same-name line that setup did not see in the pre-image, such as an admin edit or a widening, keeps `differing line kept, not the recorded range`. The unit test `test_a_pre_setup_line_and_an_admin_edit_of_the_same_name_keep_their_own_reasons` checks both reasons in one undo.

## 5. Verdict and acceptance criteria

Criterion: a real Host Setup, doctor and undo round trip with snapshots, in which undo restores the subid files byte for byte, and no host residue is left beyond the documented lock. The residue wording was narrowed after the run to host residue, for the reason given below.

Result: partly met. The verdict stays PARTIAL until the round trip is re-run on the host with the changed doctor and undo.

- The round trip works on a real host. Setup wrote the subid lines, the record and the service. Undo removed the lines and paths that setup added, except the 4 it reported as left in place. Both subid files match their pre-setup sha256. The probe service drain is a run-report item, not a saved one.
- Doctor right after setup had one FAIL in the observed run. The change in section 3 turns the idle case into a WARN. That is by code and unit test, not by a host run, so it is not yet observed on the host.
- Residue on the host. Besides `/run/lock/ginn-host-setup.lock`, which is documented, nothing was left on the host. The four left paths are the two pre-existing cgroups and the two subid files, which undo reports (section 4).
- Residue in the user's state tree, under `~/.local/state/ginn` (snapshot, saved). That tree is an Owned Path (DR-010, `$XDG_STATE_HOME/ginn`), not a host path, and FR-003 undo covers host changes only, so undo does not remove it. The three directories are: `ginn` (the state root) and `sv`, both made by hand with `mkdir -p` as test setup, so test residue; and `log`, created at run time by the log service as the user (`mkdir -p "$LOG_DIR"` in the log run script, `hostsetup/runit_system.py`). The brief says the test files were removed by hand afterwards. It does not say whether `log` was removed, so this note does not claim it was.

Per acceptance criterion:

| ID | Criterion | G1 result |
|---|---|---|
| FR-001 | AC-1: subid ranges of at least 524288 for the user, record lists the paths, doctor reports all checks passing (CL-13: no FAIL) | Subid ranges: met (both files, saved). Record: a record exists and `setup.out` gives 11 paths; the list itself was not captured, so "lists every path" is not checked. Doctor, at `4a3b508`: one FAIL right after setup (saved). After the change in section 3, no FAIL right after setup, by code change only. The run report gives 0 FAIL after the manual `mkdir`, not saved. Not yet re-run on the host. |
| FR-002 | AC-1: a second setup changes no byte and says "already configured" | Not exercised. Setup ran once. |
| FR-003 | AC-1: every recorded path restored to its pre-setup content or removed | Met for the subid files (byte-identical, saved) and for the service, supervisor and runtime paths that setup created, which were removed (saved). The two pre-existing cgroups were kept and reported. FR-003 is Should, so it has no entry in `docs/traceability-deferred.toml`. |
| FR-003 | AC-2: realms exist, undo refuses | Not exercised. No realm existed, and undo ran. |
| FR-007 | AC-1: missing subid entry fails the check and prints the host-setup command | Not exercised on the host. The subid check passed. Only the FAIL-with-printed-fix path was seen, on the runsvdir check, with exit non-zero. The scenario is covered by `test_subids_missing_subgid_entry_fails_with_host_setup_fix`. |
| FR-007 | AC-2: every check PASS, exit 0 | Not met. The observed first run had 1 FAIL. The later run had 1 WARN (the podman config is not written yet, CL-13). After the change, the idle supervisor is a second WARN, so AC-2 still does not hold on a fresh host. |
| DR-009 | Record lists each path, with its pre-image and post-image | Existence and count only. DR-009 is not a Must FR or NFR, so it has no deferred entry. |
| NFR-030 | After SIGKILL at any point, no data loss and a DR-020 state within 5 s | Not exercised. The run had no kill. This is a clean round trip and does not prove crash safety. The kill-point test is M8. |

## 6. Code changes after the run

These are in the working tree, not in `4a3b508`. None has been re-run on the host.

- `src/ginn/doctor/checks.py`. The runsvdir check follows section 3: the service is up only with a root runsv that has a `./run` child; the directory is `<home>/.local/state/ginn/sv`; a missing directory is a WARN with `mkdir -m 0700 -p`; a non-directory is a FAIL with `rm` and `mkdir`; a real directory with the service up is a FAIL with `sudo sv restart`. `_warn` takes fixes, as `_fail` does. The process scan reads `/proc/<pid>/stat` for the parent pid.
- `src/ginn/hostsetup/undo.py`. A same-name subid line that the user held before setup is reported as `the user's line from before setup kept, setup's appended line removed`, or as `...was not in the file` when setup's line was already absent. A same-name line that setup did not see in the pre-image keeps `differing line kept, not the recorded range`. The docstring of `_undo_subids` uses the same term.

## 7. Open items

1. Decided (orchestrator): Host Setup does not create `<home>/.local/state/ginn/sv`. Doctor reports the idle supervisor as WARN, as section 3 describes.
2. Open, not fixed here: a custom `XDG_STATE_HOME` splits the two paths. `OwnedPaths` follows the environment, so the first podman-using ginn command creates `sv` under that custom root. Setup's run script and doctor's check use `<home>/.local/state`. The run script waits on the home path, so the directory OwnedPaths creates is not the one it waits for. Doctor's messages name the custom value when it differs. The owner of setup and OwnedPaths needs to decide which path is right.
3. `mkdir -m 0700 -p` makes only the last component 0700. If `~/.local/state/ginn` does not exist either, `mkdir -p` creates it with the umask mode. The next podman-using ginn command tightens it, because `ginn` is an owned directory.
4. `undo.out` shows the old wording (section 4). The new wording is in section 4 and is checked by unit tests only.
5. Open: whether `~/.local/state/ginn/log` was removed after the snapshot is not recorded. It is an Owned Path, so its cleanup belongs to ginn's own rules, not to undo.
6. The second doctor output (16 PASS, 1 WARN, 0 FAIL) is a run report item, not in a saved file. Saving it needs another run on the host.
7. The evidence is at `4a3b508`. HEAD is now `e1f0819`. The doctor and undo changes are not committed.
8. The about one second restart window is documented in the code and in section 3. It has not been observed on the host.
9. Resolved in the previous change: `docs/clarifications.md` CL-11 and CL-13, `docs/roadmap.md` T3.5, ADR-013 (dated amendment of 2026-10-12), and `spikes/README.md`.

## 8. Verdict

G1: PARTIAL. The round trip mechanics hold on a real Void host: setup, the service, the doctor, undo and byte-identical subid files, all saved. The doctor change makes doctor right after setup free of FAIL, but that is by code and unit test, and it is not re-run on the host. The verdict stays PARTIAL until the round trip is re-run. NFR-030 is not tested by this run. FR-002 and FR-007 AC-1 and AC-2 need further host runs.
