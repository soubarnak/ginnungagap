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
- Branch release-v1 (done 2026-10-20): T7.2 polling fallback test (c067162), T10.6 signed tag-driven XBPS release workflow (9d99f03), T10.4 measure scripts for NFR-006, 008, 009 and 010 (39c6c77), T8.3 residual with the open `checked`-step alias drop (870d2fe, 039eda9), T10.5 test files (2cc87cf), host gate test files (10603ec), F14 native transaction export trigger per ADR-016 (1d43fe7), and NFR-008 status accounting (03a7415; host run open). All of it is unit-tested on fakes. None of the host-only files has run.
- Wave 4 (host gates, strictly one at a time): Host Setup as root, then spike S-05s, T7.7 services gate, T7.8 rebind gate, then the M8 and M10 host gates (T10.5).
- Needs the user (Wave 4 on release-v1): OK to push the commits and the tag `ginn-v1.0.0`, approval for `sudo ginn host-setup`, the F14 host gate (plus a user decision on `libdnf5-plugin-actions` for Fedora, CL-35), a real logout for T7.8, a second host user for T10.5, confirm the XBPS_PRIVKEY Actions secret is set, and the manual NFR-050 and NFR-051 walkthrough. No musl host exists, so the musl legs are recorded as deferred.

## Current state (2026-10-16)

- ADR-014 (relay) and the provenance rewrite are merged into `void` at cfed445 (PR #1 and PR #2). CI on `void` is green.
- ADR-015 (systemd at services-on create) is Accepted. Its work is on branch `adr-015-init-provisioning` (worktree `.claude/worktrees/adr-015`), pushed to origin at 564d51b. No PR is open yet.
- Committed and reviewed on that branch: T7.20 (f79a4c0), T7.19 (2243251), T7.17 (0f1bd64), T7.18 (621e8df), the ADR and roadmap update (8c25845), T7.21 (87d8ade), T7.26 (c7f05fc), T7.24 (70dd5f2, crash-safety kill points 0d6523b), T7.22 (8d38fc6), T7.23 (88941f3), T7.25 (564d51b), the docs pass (2129b0c), the low follow-ups (caf8155) and the T10.6 prep (5d4ca7a).
- Next, in order: the T7.27 host gate (running now; items 1 to 14, one host run at a time), then a PR into `void` (ask the user first), then the M8, T10.2 and T10.3 host gates, then the verification report refresh and the v1.0.0 tag (T10.6).
- Low follow-up: GINN-E-PKG-PROTECTED reads in the singular when it lists several names. Known flakes: `test_hostsetup_runit` finish timing, `test_runtime_idle` JSON race, `test_gui_jobs` cancel race, `test_runtime_enter` realm-busy wait under load.

Rules for parallel agents:

- Shared files are `src/ginn/errors.py`, `docs/errors.md`, `docs/roadmap.md`, `docs/traceability-deferred.toml` and `src/ginn/cli/commands/__init__.py`. Only one agent in a wave edits each of them. The other agents report the error codes and roadmap notes they need, and the owner merges them.
- No two agents in a wave edit the same file. Every brief lists the files the agent may edit, and the main session checks the lists for overlap before it launches the wave. All agents share one working tree and one branch, so no merge is ever needed.
- Host gates (`GINN_INTEGRATION=1`) never run in parallel with each other.
- Agents do not commit. The main session commits one task at a time with `git add <paths>`.
