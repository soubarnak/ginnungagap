# Verification report

Status: draft, 2026-10-14. Not signed. Section 9 of `GINN-SRS-001-v1.0.md` asks for a signed report before release. This file is the table that signing would use.

The rows cover NFR-001 to NFR-011 (SRS section 4.1) and the eight release gates (SRS section 9). Each row gives the target, the method, the latest result, and a verdict. The evidence names the file it comes from. This draft measures two things again: the NFR-011 run and a run of the NFR-001 script. The rows say so.

## Verdicts

- **PASS**: the target holds on the evidence named. The method is named too, so a reader can judge it.
- **PARTIAL**: a measurement exists, but the SRS asks for more than was run, such as more cycles, more distributions or more runs. The note says what is missing.
- **FAIL**: a measurement exists and misses the target.
- **NOT RUN**: no measurement exists for the requirement. The method may still exist as a script or a test.

A PASS from a spike (`spikes/`) is named as a spike result. A spike does not run the product path.

## NFR-001 to NFR-011

| NFR | Requirement and target | Method | Latest result | Verdict |
|---|---|---|---|---|
| NFR-001 | No ginn process runs when no realm runs. Target: 0 processes from ginn executables; the runit user supervisor uses at most 1 MiB RSS. | `tools/measure/nfr-001-idle-processes.py` (read-only `/proc` scan). | Not measured as a product check. S-05 found 0 attributed processes after its teardown (`spikes/results/S-05.md`). A run of the script on the development host on 2026-10-14 found a `ginn-idle` process from another agent's test run, so that host was not idle. | NOT RUN |
| NFR-002 | Idle host memory of a services-off realm, median of 10 runs. Target: at most 8 MiB RSS. | `GINN_SPIKE_OUT=spikes/s05 GINN_MEASURE_SWEEP=s05r-20261012T145658 sh tools/measure/nfr-002-idle-rss-services-off.sh`. It reads the committed sweep and reproduces 4.555 MiB. | S-05r: median 4.55 MiB over 10 counted runs, max 4.70 MiB. The total counts conmon, the container processes and the rootless pause process (`spikes/results/S-05.md`). The run used the spike's pinned storage, not the product path. | PASS (spike) |
| NFR-003 | Idle host memory of a services-on realm, median of 10 runs. Target: at most 40 MiB RSS. | `GINN_SPIKE_OUT=spikes/s05 GINN_MEASURE_SWEEP=s05r-20261012T145658 sh tools/measure/nfr-003-idle-rss-services-on.sh`. It reads configuration `on-abd` of the committed sweep and reproduces 37.631 MiB. | S-05r with the a+b+d mitigations (ADR-007 default): median 37.63 MiB, max 37.96 MiB. Without mitigations the max was 40.34 MiB, which fails (`spikes/results/S-05.md`). The services-on leg of the spike needs one root step, which the script does not run. | PASS (spike, with mitigations) |
| NFR-004 | Added launch latency of an app in a running realm, p95 over 100 launches. Target: p95 at most 300 ms. | `tools/measure/nfr-004-exec-latency.sh`, which runs the host-gate test `test_e_exec_p95_over_100_launches`. | T4.9, load 5 to 8: p95 129.1 ms over 100 launches (`spikes/results/T4.9-lifecycle-real.md`, section 3). Same test at load 24: p95 453.3 ms, which fails. The idle re-measure is open. The timing is in process, not the CLI start-up. | PARTIAL |
| NFR-005 | Start time of a stopped realm to `running`, p95 over 50 cycles per distribution. Target: p95 at most 2 s. | `tools/measure/nfr-005-start-time.sh`, which runs `test_e_start_stop_cycles`. | T4.9, debian: p95 0.107 s over 20 cycles (`spikes/results/T4.9-lifecycle-real.md`, section 3). Arch and Fedora not run; 50 cycles not run. | PARTIAL |
| NFR-006 | Compute and GPU throughput within 3% of native (7-Zip and glmark2, median of 5). | None yet. It needs a realm with both benchmarks and a display for the GPU leg. | Not run. | NOT RUN |
| NFR-007 | Realm creation time with no `--with`, at most 5 min on a 100 Mbps link, 3 runs per distribution. | `tools/measure/nfr-007-create-time.sh`, which runs `test_c_create_timing`. | T4.9, debian: 4.758 s, one run, zero packages, image pull included (`spikes/results/T4.9-lifecycle-real.md`, section 3). The 3-run, 100 Mbps, all-distribution run is not done. | PARTIAL |
| NFR-008 | Disk use of a new realm at most the official image size plus 200 MiB. | None yet. The `ginn status` JSON already reports disk use (T4.5). | `spikes/results/S-10.md` measured one addition only: the Arch locale archive, 3,064,432 bytes. No realm total was measured. | NOT RUN |
| NFR-009 | Export or unexport of a desktop entry at most 5 s after a package transaction, 20 runs. | None yet. It needs a gate 3 host with installed applications. | Not run. Unit tests cover the watcher and the sync (`tests/unit/test_native_watch.py`, `tests/unit/test_export_sync.py`). | NOT RUN |
| NFR-010 | Control Plane staleness at most 1 s after a CLI operation, 50 runs. | None yet. It needs the GUI (M9) and a display. | Not run. | NOT RUN |
| NFR-011 | Integrity check overhead, at most 1 s per mutating operation with at most 5,000 files in scope, 20 runs. | `tools/measure/nfr-011-integrity-scan.py` (library calls on a synthetic tree of 5,000 files). | 2026-10-14, this host: p95 0.084 s, median 0.066 s, max 0.102 s over 20 runs. The host scope is larger than 5,000 entries (about 44,600 on the development host), so the host run is not done (`docs/traceability-deferred.toml`). | PARTIAL |

## Section 9 release gates

| Gate | Requirement | Latest evidence | Verdict |
|---|---|---|---|
| 1 | Every Must FR passes its acceptance tests on glibc with AMD or Intel GPU, on musl, and on glibc with NVIDIA. | glibc: host gates for lifecycle (`spikes/results/T4.9-lifecycle-real.md`, sections 3 and 7: the first run failed, and the rerun after Resolution A passed), CLI (`T4.11-cli-real.md`) and tiers (`T6.8-tiers-real.md`). musl: no host yet (`spikes/results/` has no S-03). NVIDIA: no host run. | NOT RUN |
| 2 | Every Must NFR meets its target, recorded in a signed report. | This table. Unsigned. Of the eleven NFR rows, 2 pass on a spike, 4 are partial, and 5 are not run. | NOT RUN |
| 3 | Services gate: FR-080 and FR-081 pass on glibc and musl. | glibc: S-01 passes the roadmap criterion on crun and runc (`spikes/results/S-01.md`). musl: no result (`docs/roadmap.md`, M1 residuals). | NOT RUN (musl) |
| 4 | Host integrity gate: the FR-005 diff shows no change outside the Owned Paths. | `tools/host-diff.sh` (T10.2) has unit tests on temporary trees (`tests/unit/test_tools_host_diff.py`). The full lifecycle suite has not run under it. | NOT RUN |
| 5 | Provenance gate: the NFR-063 scan finds no copied code. | `tools/provenance-scan.sh` exists. It did not run for this draft. | NOT RUN |
| 6 | Security gate: NFR-SEC-001 to 010 verified; the FR-062 and FR-067 key-leak tests pass. | T6.8 host gate: 9 PASS, 1 SKIP. The skipped item is FR-067-AC1, because the image has no `ssh-add` (`spikes/results/T6.8-tiers-real.md`). | NOT RUN (partial) |
| 7 | No open defect of severity 1 or 2. | Open: the NFR-004 result at load 24 (p95 453 ms) has no idle re-measure. The FR-067-AC1 skip is not classified. | NOT RUN |
| 8 | Every Should FR passes or is deferred in the release notes. | Release notes are task T10.6, not written. | NOT RUN |

## Running the checks

```sh
tools/host-diff.sh -- env GINN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -q   # FR-005, Void host
tools/trace-no-xbps.sh -- env GINN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -q  # FR-004
tools/measure/nfr-004-exec-latency.sh                                                           # NFR-004, host gate
python3 -I tools/measure/nfr-011-integrity-scan.py                                              # NFR-011, library
```

The host-diff run walks `/`. A host with a mounted drive under `/media` or `/mnt` shows the drive's entries as changes. Add `--exclude /media --exclude /mnt` to that run when the drives are not part of the host.

Each measure script prints one JSON line per metric, with the keys `nfr`, `metric`, `value`, `unit`, `target` and `verdict`. The exit status is 0 for PASS, 1 for FAIL and 2 for NOT RUN. The NFR-002 and NFR-003 scripts read the spike output and start nothing. The host-gate scripts (NFR-004, 005 and 007) run the integration test on this host and need rootless Podman.
