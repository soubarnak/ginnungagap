# F6 root cause (arch) and F1 image probe

Date: 2026-10-14. Host: Void Linux glibc x86_64, kernel 7.2.10_1, podman 5.8.8 with crun, conmon
2.2.1, user soubarna (uid 1000), cgroup v2 delegated at `/sys/fs/cgroup/ginn/1000`, supervisor
`ginn-soubarna` (runsvdir in the `supervisor` leaf). Gate reference: `spikes/results/T7.7-services-real.md`
(known gaps F1, F6, F11). The helpers are in `spikes/f6f1/`; raw per-step outputs are in
`spikes/f6f1/out/`.

## Verdict

1. **F6 root cause: the arch base image ships `/etc/machine-id` with the literal string
   `uninitialized`.** systemd treats that as a first boot and runs `systemd-firstboot`, which fails
   with no stdin. dbus-broker-launch then refuses to start (its child fails with "Package not
   installed", then `service_add` fails with ENOTCONN), so `dbus.socket` hits the start limit, and
   `systemd-homed` fails with "Event loop failed: Connection reset by peer" while the bus is down.
   `systemd-resolved` fails for a separate reason: "Failed to keep CAP_SYS_ADMIN", because the realm
   has no SYS_ADMIN (the M1 decision).
2. **Masks alone do not fix F6.** With the nine extra masks and the machine-id left uninitialized
   (V2), dbus-broker and dbus.socket still fail and `busctl` cannot connect. Do not mask dbus.
3. **A valid machine-id fixes dbus and firstboot, but a bind mount of it breaks
   `systemd-machine-id-commit.service`.** That unit runs `systemd-machine-id-setup --commit`, which
   prints "/etc/machine-id is not on a temporary file system" and exits 1 when `/etc/machine-id` is
   a bind mount, read-only (V1) or read-write (V1rw). Writing the machine-id into the created
   container with `podman cp` before the first start avoids the mount, and the commit unit is then
   skipped (inactive, dead).
4. **The `--audit` flag in `dbus-broker.service` is not the blocker.** With a valid machine-id,
   `dbus-broker-launch --scope system --audit` runs (V1cp: `busctl list` returns rc 0 and lists the
   bus). The F6 fix is a machine-id change, not a unit drop-in.
5. **Minimal set for zero failed units: a per-realm machine-id delivered by `podman cp` before the
   first start, with no extra masks (V1cp).** `is-system-running` = running, zero failed units,
   dbus up. Memory is in section A.5. Recommendation for ADR-007 is in section C.

## Commands and scripts

- `spikes/f6f1/ginn-launch` is the T7.7 gate's launcher (native helpers from `native/build`). The
  product realm was made with `spikes/f6f1/ginn-launch create arch --name f6-arch --services on`
  and started with `... start f6-arch`. The first create returned GINN-E-INTERNAL because the
  launcher was run by a relative path: `_write_service` needs an absolute ginn command, and the
  default error output does not say so (the same hidden-reason problem as F3). The absolute path
  succeeded.
- `spikes/f6f1/f6f1.py product-diag` runs the diagnostics in the product realm (product podman
  root, runroot, tmpdir, and XDG_RUNTIME_DIR = `/run/ginn/1000`, as the gate does).
- `spikes/f6f1/f6f1.py variant <key> <N>` boots a hand-built services-on container (argv below)
  under its own runit service in the user's sv directory, runs the same diagnostics, and with N > 0
  runs N idle measurements. The spike storage is `~/.cache/ginn-f6f1/root`, runroot and XDG dir
  under `/run/user/1000/f6f1`, and `--runtime crun` on every call.
- The runner is `spikes/f6f1/f6f1_runner.py`: a job file of argv lists (no shell) runs inside the
  supervisor leaf, the same route as the gate's `leaf_exec` (F2: a session process cannot enter a
  services-on realm's cgroup).

## A.1 Product realm f6-arch (product path)

`systemctl is-system-running` = **degraded**. `systemctl --failed` lists seven units:
`dbus-broker.service`, `systemd-firstboot.service`, `systemd-homed.service`,
`systemd-resolved.service`, `dbus.socket`, `systemd-resolved-monitor.socket`,
`systemd-resolved-varlink.socket`. This matches the gate's F6 list exactly.

- `/etc/machine-id`: contents `uninitialized` (14 bytes, root, mode 0444). It comes from the image
  layer. `/proc/1/mountinfo` has no machine-id mount.
- `busctl --no-pager list`: "Failed to connect to system scope bus via local transport: Connection
  refused", rc 1.
- Running services: systemd-journald, systemd-logind, systemd-nsresourced, systemd-userdbd.
- `ps -eo pid,rss,comm`: 14 container processes. systemd (pid 1) 15424 KiB RSS, journald 6720,
  userdbd 5404, three systemd-userwork 5952 to 6436 each, six systemd-nsresourced 5656 to 6084
  each, logind 7116.
- Versions: systemd 262-1, dbus-broker 37-3.

Journal evidence (`journalctl -b -u ...`, excerpts):

```
systemd-firstboot: Failed to set up standard input: No such file or directory
systemd-resolved: Failed to keep CAP_SYS_ADMIN: Operation not permitted
systemd-resolved: Failed at step USER spawning /usr/lib/systemd/systemd-resolved: Operation not permitted
dbus-broker-launch: ERROR launcher_run_child @ launcher.c +344: Package not installed
dbus-broker-launch: ERROR service_add @ service.c +1011: Transport endpoint is not connected
dbus-broker-launch: Exiting due to fatal error: -107
systemd: dbus-broker.service: Start request repeated too quickly. / dbus.socket: service-start-limit-hit
systemd-homed: Event loop failed: Connection reset by peer
```

The unit is `ExecStart=/usr/bin/dbus-broker-launch --scope system --audit`. Its binary links
libaudit, so `--audit` was checked as a suspect (see V1cp).

## A.2 Hand-built variants (argv, not product code)

The control is V0: the services-on argv of ADR-003 with ADR-007 mitigations a, b and d, as the
product sends it. Every variant keeps that argv. The differences are listed in the table.

```
podman create --name f6v-<tag> --systemd=always --cgroupns private --cgroup-parent /ginn/1000/f6v-<tag>
  --user 0:0 --userns keep-id --passwd-entry 'soubarna:*:1000:1000:soubarna:/home/soubarna:/bin/bash'
  --network host --http-proxy=false --stop-signal SIGRTMIN+3 --stop-timeout 30
  --env SYSTEMD_LOG_LEVEL=warning --conmon-pidfile <run>/conmon.pid --label io.ginn.spike=f6f1
  --tmpfs /run/user/1000:mode=0700,U -v <run>/journald-ginn.conf:/etc/systemd/journald.conf.d/ginn.conf:ro
  -v /dev/null:/etc/systemd/system/<unit>:ro (x10, the ADR-007 b list) [variant binds]
  quay.io/archlinux/archlinux@sha256:c556b483a8e653aa6389bb66263e1d96cd6e17c67ca5968c87495d313c4a2e93 /sbin/init
```

The Realm Home bind and the GPU device are not in the hand argv. They do not touch systemd's
startup, and V0 reproduces the product's seven failed units without them.

| Variant | Change from V0 | is-system-running | Failed units | busctl | Running services at idle |
| --- | --- | --- | --- | --- | --- |
| product f6-arch | none (product path) | degraded | 7 | rc 1, refused | journald, logind, nsresourced, userdbd |
| v0 | none (hand argv) | degraded | 7 | rc 1, refused | journald, logind, nsresourced, userdbd |
| v1 | ro bind of a per-realm machine-id | degraded | 1: machine-id-commit | rc 0 | dbus-broker, journald, logind |
| v1rw | rw bind of a per-realm machine-id | degraded | 1: machine-id-commit | rc 0 | dbus-broker, journald, logind |
| v1mask | ro bind, plus mask systemd-machine-id-commit.service | running | 0 | rc 0 | dbus-broker, journald, logind |
| v1cp | machine-id by podman cp before first start (no bind) | running | 0 | rc 0 | dbus-broker, journald, logind |
| v2 | nine extra masks, machine-id uninitialized | degraded | 2: dbus-broker, dbus.socket | refused | journald, logind |
| v3 | ro bind plus nine extra masks | degraded | 1: machine-id-commit | rc 0 | dbus-broker, journald, logind |
| v3cp | cp plus nine extra masks | running | 0 | rc 0 | dbus-broker, journald, logind |

The nine extra masks are: `systemd-firstboot.service`, `systemd-homed.service`,
`systemd-resolved.service`, `systemd-resolved-monitor.socket`, `systemd-resolved-varlink.socket`,
`systemd-nsresourced.service`, `systemd-nsresourced.socket`, `systemd-userdbd.service`,
`systemd-userdbd.socket`. The logind mask (V4) is separate, in section A.5.

The commit unit's exact message in V1rw: `systemd-machine-id-setup[40]: /etc/machine-id is not on
a temporary file system.`, with `Process: ExecStart=systemd-machine-id-setup --commit
(code=exited, status=1/FAILURE)`.

## A.3 What the masks do and do not change

- systemd-nsresourced and systemd-userdbd run in V0 (the product argv) and are not running in V1cp.
  V0 and V1cp differ only in the machine-id delivery, so the valid machine-id is what keeps them
  down. The start trigger was not traced, so the mechanism is not claimed here.
- systemd-homed and systemd-resolved are inactive (dead), not failed, in V1 and V1cp. Their failures
  in V0 came from the lost bus (homed) and from the missing CAP_SYS_ADMIN (resolved). Masking them
  stays a guard against those failures returning, but it is not needed for zero failed units.

## A.4 Measurement method

The gate's method (`tests/integration/test_services_real.py`, `realm_rss`): the pid set is the
realm's cgroup tree, its conmon (from the pidfile), the rootless pause (once, from
`$XDG_RUNTIME_DIR/libpod/tmp/pause.pid`), and the runsv and ginn-wait of the service. VmRSS is the
budget figure. Pss is read from `smaps_rollup` on the host, and a process whose Pss cannot be read
is counted in `pss_unreadable`. Each run boots the container, waits for `systemctl is-system-running
--wait`, idles 60 s, and then samples. Raw rows are in `spikes/f6f1/out/measure-*/` and
`measure.log`.

## A.5 Memory after the F6 workaround (N = 5 each, 2026-10-14)

| Configuration | Failed units | is-system-running | VmRSS median MiB (min to max) | PSS median MiB | Container processes |
| --- | --- | --- | --- | --- | --- |
| Product path, f6-arch, today (control) | 7 | degraded | 88.68 (69.02 to 95.17) | 24.55 | 13 to 14 |
| v0, hand argv, ADR-007 a+b+d (control) | 7 | degraded | 96.74 (87.96 to 103.12) | 25.53 | 13 to 15 |
| v1cp: machine-id by podman cp | 0 | running | 40.29 (39.99 to 40.36) | 20.27 | 5 |
| v3cp: v1cp plus the nine extra masks | 0 | running | 40.01 (38.31 to 40.70) | 20.10 | 5 |
| v4cp: v3cp plus systemd-logind.service masked | 0 | running | 33.27 (30.27 to 33.73) | 19.13 | 4 |

The budget is NFR-003, 40 MiB VmRSS median. v1cp misses it by 0.29 MiB. v4cp meets it with 6.7 MiB to
spare. The product control (88.68) agrees with the gate's median of 87.11 (10 runs). Every figure
includes the same helper pids as the gate's realm_rss: runsv, ginn-wait, conmon and the rootless pause
(once). The helpers' own share is not split out in this run. PSS was readable for every process
(`pss_unreadable` = 0).

Differences: v0 to v1cp is 56.5 MiB less VmRSS, and v0 to v4cp is 63.5 MiB less. The hand argv is
about 8 MiB above the product path (96.74 against 88.68). It omits the Realm Home bind and the GPU
device, and I did not isolate which one causes the gap. So the workaround numbers compare like for
like with V0, and the product path with the workaround has not been measured yet.

NFR-021 (five realms, 200 MiB, Should) is extrapolated, not measured together. Five v1cp realms come
to about 201.5 MiB (over), and five v4cp realms to about 166.4 MiB (under).

Mechanisms behind the drop: the product argv runs 13 to 15 processes. With a valid machine-id only 5
remain (systemd, journald, logind, dbus-broker-launch, dbus-broker). With logind masked, 4 remain.
Idle `ps` for v1cp: systemd RSS 14520 KiB, journald 6564, logind 6976, dbus-broker-launch 4228,
dbus-broker 3324.

## B. F1: image probe (Part B, 2026-10-14)

Pulled into the spike store with `--platform linux/amd64`. `/sbin/init` was checked with
`podman run --rm --cgroups=disabled --network none` (no services-on boot needed for the check).
Compressed bytes are the sum of the registry's platform manifest layers. Uncompressed bytes are
`podman image inspect --format {{.Size}}`, the same measure the trust file uses. Digests are the
linux/amd64 platform manifests (the registry's document equals the platform digest for single-manifest
images, and the index-to-platform match was checked by sha256 for the index images).

| Reference | linux/amd64 digest | Compressed | Uncompressed | /sbin/init | Target | Services-on boot (ADR-003 argv, a+b+d, cp machine-id) |
| --- | --- | --- | --- | --- | --- | --- |
| quay.io/archlinux/archlinux@sha256:c556b483... (current pin) | c556b483... | 139,248,529 (pin) | 410,426,777 | yes, `../lib/systemd/systemd` | /usr/lib/systemd/systemd | running, 0 failed units, busctl rc 0 |
| quay.io/archlinux/archlinux:base (tag, now moved) | 692c6efe... | 139,281,486 | not pulled | not probed | not probed | not probed (registry only) |
| registry.fedoraproject.org/fedora:44 (non-minimal) | 4031272b... | 72,016,519 | 191,042,864 | no | none | start fails: crun `executable file /sbin/init not found` (rc 125) |
| registry.fedoraproject.org/fedora-minimal:44 (tag) | b1721f7a... | 56,644,447 | 140,942,136 | no | none | not booted (no `/sbin/init`, so the start cannot succeed) |
| registry.fedoraproject.org/fedora-minimal@sha256:86d2e242... (current pin) | 86d2e242... | 56,632,319 (pin) | 140,839,859 (pin) | no | none | start fails, same crun error |
| docker.io/library/debian:stable-slim (current pin) | d405c1e1... | 29,843,347 (pin) | 81,265,866 | no | none | start fails, same crun error |
| docker.io/library/debian:stable | d95c640a... | 49,393,200 | 124,099,746 | no | none | start fails, same crun error |
| docker.io/kalilinux/kali-rolling:latest (current pin) | ffc5b42d... | 52,909,587 (pin) | 125,317,206 | no | none | start fails, same crun error |

Every failing start has the same cause: `crun: executable file `/sbin/init` not found`, with
`create` succeeding and `start` returning rc 125. The product reports the same start as GINN-E-INTERNAL,
which hides the crun message (F3).

Answers:

- **fedora:44 (non-minimal):** no `/sbin/init`, and no systemd binary anywhere on the image. Not viable.
  fedora-minimal:44, both the tag and the pinned digest, is the same. Not viable.
- **debian:** no official debian image ships systemd. The docker-library definition for `debian`
  (`library/debian` in docker-library/official-images) lists its tags and variants, and none is a systemd
  variant. Among the first 100 of 3,036 tags on Docker Hub's `library/debian`, no tag mentions systemd
  either. The `stable` and `stable-slim` images both fail to boot. **This needs a user decision** (see
  section D).
- **kali:** `kalilinux/kali-rolling:latest` has no `/sbin/init` and no systemd. Its current pin
  (ffc5b42d) fails to start the same way. T7.7 did not test kali, so this is a new gap that belongs
  with F1.
- **arch:** the current pin (c556b483) has `/sbin/init` -> systemd, boots to running, and shows zero failed
  units with the workaround. No pin change is needed for it. The product does not yet apply the
  workaround, so the product's arch realm still fails (section A).

Pins: no official image other than arch has systemd, so no new pin can be recommended for debian,
fedora or kali from this probe. The current debian stable-slim and kali pins are still the current
platform digests. The fedora-minimal and arch tags have moved since the trust pins (2026-10-12), and
the pinned digests still resolve and still match their recorded sizes.

Boot evidence for arch (pinned digest, Part B): `is-system-running` = running, failed units = none,
`busctl list` rc 0. Start-error excerpts for the failing images are in `spikes/f6f1/out/image-boot/`
(`start.out`).

## C. Recommendations for ADR-007 (the decision is the user's)

1. **Machine-id (required for F6).** Generate a random 128-bit id per realm (32 lowercase hex and a newline,
   mode 0644) and `podman cp` it into the created container at `/etc/machine-id` before `podman start`.
   Do not bind-mount it, read-only (V1) or read-write (V1rw). Either bind leaves
   `systemd-machine-id-commit.service` failed, with `/etc/machine-id is not on a temporary file system`.
   The copied file lives in the container's layer, so it should survive stop and start (not tested here).
   A new generation gets a new id.
2. **Unit list, ADR-007 b: keep the ten current units.** Do not add the nine F6 units (firstboot, homed,
   resolved and its two sockets, nsresourced and its socket, userdbd and its socket). With a valid
   machine-id they change neither the failures nor the memory (v3cp matches v1cp). Masking dbus is never
   needed. Without the machine-id, masks do not fix dbus (V2).
3. **Budget, NFR-003: add `systemd-logind.service` (one unit).** It is the only single addition that brings arch under 40 MiB (33.27
   median against 40.29). The T7.7 review also found cronie active with logind masked, but that run was
   before the F6 fix (7 failed units). If the ADR owner keeps 40.29, record NFR-003 as a 0.29 MiB miss on arch.

Exact unit list for ADR-007 b if item 3 is taken (11 units): `systemd-udevd.service`,
`systemd-udevd-control.socket`, `systemd-udevd-kernel.socket`, `systemd-udev-trigger.service`,
`getty@tty1.service`, `console-getty.service`, `apt-daily.timer`, `apt-daily-upgrade.timer`,
`e2scrub_all.timer`, `fstrim.timer`, `systemd-logind.service`. Tests pin the list
(`SERVICES_MASKED_UNITS` in `src/ginn/runtime/spec.py`), so the change needs the test update too.
The product does not yet write the machine-id: this probe changed no source, as the brief required.

Journald (ADR-007 a) and the mitigation d environment are unchanged.

## D. Decisions and open items

- **F1, debian and kali (user decision).** Choose one of:
  (a) install systemd at create through the Integration Package Set path (`ginn rebuild`, T7.3 and T7.4
  pending). It keeps the official bases and their pins. This is the recommendation here, because
  the trust model (AS-024, DR-040) is built on official digests.
  (b) a community image that carries systemd. Not pulled or tested here, per the brief. It is
  unofficial and unsigned, so it needs its own trust review.
  Fedora bootc-style base images are expected to carry systemd, but they are not probed here.
- **F1 scope.** Kali is a gap the T7.7 list does not carry. Add it to F1.
- **F6 product fix.** Not implemented in src/ (per the brief). The NFR-003 verdict in T7.7 stays FAIL
  until the product path (create-time machine-id step and the chosen masks) is re-gated on the host.
  The memory numbers here are hand-argv, so the product re-gate is still needed.
- **F3 (error reporting), seen again.** The create path returned GINN-E-INTERNAL with no detail for
  a relative launcher path (`_write_service` needs an absolute ginn command), and the start path hides the
  crun message. Both are error-catalogue items.

## E. Commands, storage and cleanup

Commands used (all run from the repo root, times in IST, 2026-10-14):

- `spikes/f6f1/ginn-launch create arch --name f6-arch --services on`, then `... start f6-arch`
  (first create failed on a relative launcher path, see A.1).
- `spikes/f6f1/f6f1.py product-diag`, `product-exec <cmds>` (read-only probes of the product realm).
- `spikes/f6f1/f6f1.py variant v0|v1|v1rw|v1cp|v1mask|v2|v3|v3cp|v4cp 0` (diagnostics), then
  `spikes/f6f1/measure.sh` for N = 5 on v0, v1cp, v3cp, v4cp and `product-measure 5`.
- `spikes/f6f1/f6f1.py image-check <ref>` and `image-boot <ref> <tag>` (Part B).
- `spikes/f6f1/f6f1.py cleanup` (spike containers, sv entries, storage, pause, cgroup leaves).
- `spikes/f6f1/ginn-launch delete f6-arch --purge-home --yes` (the product realm, after the measurements).

Spike storage: root `~/.cache/ginn-f6f1/root`, runroot and XDG `/run/user/1000/f6f1/{rr,xdg}`, tmpdir
`.../xdg/podman-tmp`, all with `--runtime crun` and `XDG_RUNTIME_DIR` pinned to the spike XDG dir.

Cleanup done and verified: `ginn list` shows no realms; the sv directory is empty; no `f6*` cgroup
leaves remain (`/sys/fs/cgroup/ginn/1000/f6-arch` was removed by hand, empty, F9); the spike storage,
runtime dirs and scratch files are gone; my spike pause process was stopped. The product's pause (pid
20029) and an older pause (pid 32210) were not started by this spike and were left alone.
`spikes/f6f1/out/` keeps the raw evidence (about 4.6 MB, no secrets; the machine-id values are random
and belonged to throwaway containers).

Rule deviations, reported as they happened:

- Five of my manual `podman` verification calls pinned `--root` and `--runroot` but not
  `XDG_RUNTIME_DIR`. They inherited the session's `/run/user/1000`, so they read the user's
  `/run/user/1000/libpod/tmp/pause.pid`. They did not start, stop or remove any container of the user's.
  Their own `ps` and `images` calls changed no file under `libpod/`, as far as the timestamps show.
  `/run/user/1000/crun` changed at 23:16:58, which may come from one of those calls.
- Writes under the user's `libpod/tmp` at 23:17 (`exits`, a transient `persist` entry) were made by an
  unrelated `podman run --name satcyber-escape-...` (pid 21892) that was running then, not by this spike.
- No `sudo` was used. No `xbps-*` was run. Nothing in `~/.local/share/containers` was touched.
  Every spike podman call passed `--root`, `--runroot`, `--tmpdir` and `--runtime crun`.
  Product calls used the product's own pins.

Helper scripts, all in `spikes/f6f1/`: `ginn-launch` (product CLI with native helpers),
`f6f1.py` (driver and cleanup), `f6f1_runner.py` (job runner for the leaf), `measure.sh` (the N = 5
chain). Nothing was committed.

