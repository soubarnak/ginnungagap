@AGENTS.md

## Local tooling and review

- Use ripgrep (`rg`) instead of `grep` for searching files locally.
- Run code review with the `opus-reviewer` subagent at medium effort.

## Execution plan (from 2026-10-14)

The main session orchestrates and does not write code. `haiku-coder` agents implement, `opus-reviewer` reviews at each milestone boundary, and the main session commits each task with explicit paths. Tasks run in waves. Inside a wave, agents run in parallel only when their files do not overlap.

Done: M0 to M6, waves 1 to 3 (T7.1 to T7.6, T8.1 to T8.8, T9.1 to T9.9, T10.1 to T10.4). M8 and M9 are complete except their host gates.

- Wave 1 (done): T7.3 runit per-realm service; T8.1 `ops/retry.py` and T8.2 `ops/disk.py` (new files only); T7.6 `native/ginn-idle.c` and its test (wiring waits for T7.3); T9.1 and T9.2 GUI backend and viewmodels (new `gui/` package); T10.2 to T10.4 tool scripts and the verification report skeleton (new files only).
- Wave 2 (done): T7.4 and T7.5 as one agent (`sv` control, `ginn services`, autostart); T7.6 wiring into services-off start; T10.1 Kali argv with T8.7 Kali `--everything` estimate as one agent; T8.4 `ops/update.py`; rewire create and packages onto `ops/retry.py` and `ops/disk.py`.
- Wave 3 (done 2026-10-14): T9.3 to T9.8 GUI views (bd2373b), T8.3 rebuild (1e0dc1f), T8.6 crash safety (8135379), T8.5 `create --resume` (9b41a5a), T8.8 Kali everything estimate (44912de). Each was reviewed by opus-reviewer and its findings fixed before commit.
- Wave 4 (host gates, strictly one at a time): Host Setup as root, then spike S-05s, T7.7 services gate, T7.8 rebind gate, then the M8 and M10 host gates (T10.5).
- Needs the user: approval for `sudo ginn host-setup`, a real logout for T7.8, a second host user for T10.5, signing keys for T10.6, and the manual NFR-050 and NFR-051 walkthrough. No musl host exists, so the musl legs are recorded as deferred.

## Current state (2026-10-16)

- ADR-014 (relay) and the provenance rewrite are merged into `void` at cfed445 (PR #1 and PR #2). CI on `void` is green.
- ADR-015 (systemd at services-on create) is Accepted. Its work is on branch `adr-015-init-provisioning` (worktree `.claude/worktrees/adr-015`), pushed to origin at 0d6523b. No PR is open yet.
- Committed and reviewed on that branch: T7.20 (f79a4c0), T7.19 (2243251), T7.17 (0f1bd64), T7.18 (621e8df), the ADR and roadmap update (8c25845), T7.21 (87d8ade), T7.26 (c7f05fc), T7.24 (70dd5f2, crash-safety kill points 0d6523b), T7.22 (8d38fc6), T7.23 (88941f3).
- In progress: T7.25 (start path and notices: `ops/lifecycle.py`, `ops/service.py`, `errors.py`, `docs/errors.md`, `tests/unit/test_cli_services.py`, `tests/unit/test_ops_lifecycle.py`, `tests/unit/test_errors.py`). It is uncommitted in the worktree and its last full unit run had one unidentified failure. Next: find and fix that failure, review, commit.
- Then: a docs pass (record the deferred "current disk use" term of ADR-015 Decision 11; fix the ADR's Decision 8 claim that a reuse build keeps a locale note from the record, which has no such field; update the remaining "ADR-015 (proposed)" lines in `docs/clarifications.md`), then the T7.27 host gate (items 1 to 14, one host run at a time), then a PR into `void` (ask the user first).
- After ADR-015: the M8 host gate, the user's steps above, and the T10.6 release (GitHub release from `void`, unsigned xbps repo).
- Low follow-ups: `distros/loader.py` `_refresh_before` accepts any verb; GINN-E-PKG-PROTECTED reads in the singular when it lists several names; `runtime/enter.py:581` misses podman exit 255; `export/_files.py:27` fd leak if `os.fdopen` raises. Known flakes: `test_hostsetup_runit` finish timing, `test_runtime_idle` JSON race, `test_gui_jobs` cancel race, `test_runtime_enter` realm-busy wait under load.

Rules for parallel agents:

- Shared files are `src/ginn/errors.py`, `docs/errors.md`, `docs/roadmap.md`, `docs/traceability-deferred.toml` and `src/ginn/cli/commands/__init__.py`. Only one agent in a wave edits each of them. The other agents report the error codes and roadmap notes they need, and the owner merges them.
- No two agents in a wave edit the same file. Every brief lists the files the agent may edit, and the main session checks the lists for overlap before it launches the wave. All agents share one working tree and one branch, so no merge is ever needed.
- Host gates (`GINN_INTEGRATION=1`) never run in parallel with each other.
- Agents do not commit. The main session commits one task at a time with `git add <paths>`.
