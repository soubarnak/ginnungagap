@AGENTS.md

## Local tooling and review

- Use ripgrep (`rg`) instead of `grep` for searching files locally.
- Run code review with the `opus-reviewer` subagent at medium effort.

## Execution plan (from 2026-10-14)

The main session orchestrates and does not write code. `haiku-coder` agents implement, `opus-reviewer` reviews at each milestone boundary, and the main session commits each task with explicit paths. Tasks run in waves. Inside a wave, agents run in parallel only when their files do not overlap.

Done: M0 to M6, waves 1 and 2 (T7.1 to T7.6, T8.1, T8.2, T8.4, T8.7, T9.1, T9.2, T9.9, T10.1 to T10.4).

- Wave 1 (done): T7.3 runit per-realm service; T8.1 `ops/retry.py` and T8.2 `ops/disk.py` (new files only); T7.6 `native/ginn-idle.c` and its test (wiring waits for T7.3); T9.1 and T9.2 GUI backend and viewmodels (new `gui/` package); T10.2 to T10.4 tool scripts and the verification report skeleton (new files only).
- Wave 2 (done): T7.4 and T7.5 as one agent (`sv` control, `ginn services`, autostart); T7.6 wiring into services-off start; T10.1 Kali argv with T8.7 Kali `--everything` estimate as one agent; T8.4 `ops/update.py`; rewire create and packages onto `ops/retry.py` and `ops/disk.py`.
- Wave 3 (stopped by the user on 2026-10-14; not started except a draft of the GUI views, uncommitted, see the roadmap T9.3 entry): T8.3 rebuild (unblocks `ginn services on|off` and the create-time tier and toggle changes), then T8.5 `create --resume`, then T8.6 crash safety. In parallel: T9.3 to T9.9 GUI views, the FR-106 parity test, the desktop file and the `ginn-gui` subpackage.
- Wave 4 (host gates, strictly one at a time): Host Setup as root, then spike S-05s, T7.7 services gate, T7.8 rebind gate, then the M8 and M10 host gates (T10.5).
- Needs the user: approval for `sudo ginn host-setup`, a real logout for T7.8, a second host user for T10.5, signing keys for T10.6, and the manual NFR-050 and NFR-051 walkthrough. No musl host exists, so the musl legs are recorded as deferred.

Rules for parallel agents:

- Shared files are `src/ginn/errors.py`, `docs/errors.md`, `docs/roadmap.md`, `docs/traceability-deferred.toml` and `src/ginn/cli/commands/__init__.py`. Only one agent in a wave edits each of them. The other agents report the error codes and roadmap notes they need, and the owner merges them.
- No two agents in a wave edit the same file. Every brief lists the files the agent may edit, and the main session checks the lists for overlap before it launches the wave. All agents share one working tree and one branch, so no merge is ever needed.
- Host gates (`GINN_INTEGRATION=1`) never run in parallel with each other.
- Agents do not commit. The main session commits one task at a time with `git add <paths>`.
