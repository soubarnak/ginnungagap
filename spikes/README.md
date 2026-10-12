# Spikes

Spikes are short, host-only experiments that retire technical risks before the design that depends on them is fixed. Each spike answers one question from the roadmap (`docs/roadmap.md`, section "Spikes (M1)"), tests it against a pass criterion, and writes its findings to `spikes/results/`. The results decide ADRs (for example ADR-003 is commissioned from S-01's results). Risk IDs (R-xxx) come from `GINN-SRS-001-v1.0.md`, Appendix D.

## Requirements

- A Void Linux host with rootless Podman. Spikes are not run in CI.
- `sudo` for root steps. Only a spike's root step (for example writing cgroup controllers under `/sys/fs/cgroup/ginn`) runs as root. Each root step is a separate, small invocation of the spike script, and every host write is logged.
- Network access for pulling base images on the first run.
- At least 256 MiB of MemAvailable. S-01 checks this before each step and stops below it.

## Running a spike

```sh
spikes/s01_systemd_rootless.sh run        # run the S-01 procedure
spikes/s01_systemd_rootless.sh teardown   # remove spike containers, image, storage and cgroup directories
```

Environment variables:

- `GINN_SPIKE_ROOT`: podman storage for the spike. Default `~/.local/share/ginn-spike/podman`. Spikes always pass this with `--root` and their own `--runroot`, so they never touch the user's default storage.
- `GINN_SPIKE_OUT`: directory for raw logs. Default `~/.cache/ginn-spike/s01`. Raw logs are kept out of the repository.

Run spikes one at a time on a host, and run `teardown` when done. Teardown restores any host state a spike changed, using the originals recorded during setup.

## Results

Each spike writes one Markdown file under `spikes/results/`, named after the spike ID (for example `S-01.md`). The current results are `spikes/results/S-*.md`, one file per spike. S-03 (musl) is still open: it has no results file yet. A result file holds the environment facts, a table of variants, the exact flags or settings recommended, the root steps the product must perform, the measured numbers, and a verdict line (`S-01: PASS|FAIL|PARTIAL`) with the reasoning against the roadmap's pass criterion.

Raw logs and temporary files stay out of the repository.

## Files

- `s01_systemd_rootless.sh`: S-01 procedure (systemd as PID 1 in rootless Podman under a delegated cgroup).
- `Dockerfile.s01`: image for S-01 (Debian stable with systemd and dbus).
- `results/S-01.md`: S-01 results and verdict.
- `results/S-*.md`: results and verdict of every other spike, one file each.
- `s09/`: S-09 teardown and cleanup logs and the root-command extract, with MAC and IP addresses redacted. `results/S-09.md` (Deviations) refers to them.
