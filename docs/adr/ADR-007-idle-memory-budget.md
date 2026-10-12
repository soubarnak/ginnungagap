# ADR-007: Idle memory budget

- Status: Accepted
- Date: 2026-10-12

## Context

NFR-002 caps an idle services-off realm at 8 MiB RSS. It counts every host-side and in-realm process that ginn or the runtime owns, and it excludes user apps. NFR-003 caps an idle services-on realm at 40 MiB, with systemd and its default units included. NFR-021 asks for 5 concurrent realms, each within those budgets (Should). FR-068 requires the session D-Bus filter, which runs `xdg-dbus-proxy`.

Ranked characteristics (SRS section 4): performance efficiency comes first. So memory decides the mitigations. The journal's persistence (recoverability, third) and the default log level (usability, fifth) give way to it. Interoperability (fourth) settles the logind question. A mask that breaks FR-080 apps is not the default.

Measured on the reference host (Void Linux glibc, kernel 7.2.9_1, podman 5.8.8, crun 1.30.1, conmon 2.2.1, catatonit 0.1.7 at the podman lookup path). Realm image: Debian stable (Debian 13.7, systemd 257.13). Source: `spikes/results/S-05.md` (S-05r) and `spikes/s05/`. Each row is the median or max of 10 counted runs, in MiB:

| Budget | Config | Median total | Max total | Limit | Median margin | Max margin | Verdict |
|---|---|---|---|---|---|---|---|
| NFR-002 | services off, realm plus pause | 4.55 | 4.70 | 8 | +3.45 | +3.30 | PASS |
| NFR-003 | services on, no mitigation | 39.68 | 40.34 | 40 | +0.32 | -0.34 | median PASS, max FAIL |
| NFR-003 | services on, a+b+d | 37.63 | 37.96 | 40 | +2.37 | +2.04 | PASS |

The RSS split explains the spread. `RssAnon` is steady: 5.5 to 5.7 MiB per services-on realm, and 0.3 MiB per services-off realm. The spread between rows is in `RssFile`. The kernel reclaims file pages under memory pressure without any swap.

Attribution rule. The budget counts two things:
- Every realm process: conmon, the container init and its children.
- ginn's rootless pause process. podman starts it as `catatonit -P` when catatonit exists at `/usr/libexec/podman/catatonit` (S-05b). ginn pins `XDG_RUNTIME_DIR` to `/run/ginn/<uid>` (ADR-004), so the pause is ginn's own. All of one user's realms share it. The budget counts it once per realm. That is the worst case, because a realm that runs alone pays the full pause.

The budget does not count user apps. It does not count the runit user supervisor either. NFR-001 gives that supervisor its own limit of 1 MiB.

## Decision

ginn ships catatonit. podman then starts `catatonit -P` as the rootless pause, and the budget counts that pause once per realm.

Services-on realms ship three mitigations by default. This ADR calls them a+b+d:

- (a) journald runs with `Storage=volatile` and `RuntimeMaxUse=8M`. The file is `spikes/s05/journald-volatile.conf`.
- (b) Ten default units are masked by a symlink to `/dev/null` under `/etc/systemd/system`. The list is `spikes/s05/masks-base.txt`: `systemd-udevd.service`, `systemd-udevd-control.socket`, `systemd-udevd-kernel.socket`, `systemd-udev-trigger.service`, `getty@tty1.service`, `console-getty.service`, `apt-daily.timer`, `apt-daily-upgrade.timer`, `e2scrub_all.timer` and `fstrim.timer`.
- (d) `SYSTEMD_LOG_LEVEL=warning` is set on the create argv with `-e`.

The spike put (a) and (b) into image layers (`spikes/s05/Dockerfile.mit`). T7 chooses how ginn writes them into the realm. The content must match the two files above.

Masking `systemd-logind.service` (`spikes/s05/masks-logind.txt`) is not default. T7.7 tests it against the FR-080 apps first.

`xdg-dbus-proxy` starts lazily, on first use, and not with the realm. One resident proxy takes about 4.7 MiB (S-05). That alone breaks the services-off budget.

## Alternatives considered

- Unmitigated services-on (the baseline). Rejected as the default. The median passes at 39.68 MiB. The largest counted row is 40.34 MiB, and the margin at the median is 0.32 MiB. That margin is too small to rely on across distros, and T7.7 re-measures per distro.
- Masking logind as well (a+b+logind+d). Not the default. S-05 (runc, podman's pause, before S-05r) measured a services-on median of 29.64 MiB with it. FR-080 apps were not tested with logind masked. T7.7 decides.
- Podman's own pause, with no catatonit. Rejected. S-05 measured the pause at 6.33 MiB median. That made the services-off total 10.58 MiB, which fails NFR-002.
- Treating the pause as per-user overhead outside the budget (S-05's first option). Rejected. NFR-002 counts every process that ginn or the runtime owns. With ginn's own pinned runtime directory, the pause is ginn's.
- An always-on `xdg-dbus-proxy`. Rejected. One proxy takes about 4.7 MiB (S-05). Added to the S-05r services-off median of 4.55 MiB, that gives about 9.25 MiB, which is over 8 MiB. S-05r did not re-measure the proxy. The figure is the S-05 number.
- Default journald (persistent). Rejected. Journald is one of the two large consumers in the services-on realm. S-05 measured the base median at 40.59 MiB and the volatile-with-cap median at 37.90 MiB, which is about 2.7 MiB less.

## Consequences

- Services-off meets NFR-002 with a margin of 3.30 MiB at the max. Services-on meets NFR-003 with a+b+d at the median and at the max.
- Cost of (a): journals live in RAM, and logs are lost when the realm stops. That costs recoverability (third in the ranking). ginn accepts it because performance efficiency ranks first.
- Cost of (d): PID 1 logs warnings and above only.
- A services-off realm keeps only conmon and its init as resident helpers. Any other keep-alive process counts against the 8 MiB budget. S-05 measured conmon at about 1.9 MiB and the init at about 0.7 MiB.
- `xdg-dbus-proxy` must start lazily (FR-068, T6.5).
- The catatonit package must supply the lookup path. If it is missing, podman starts its own pause (6.33 MiB median in S-05), and NFR-002 fails. The Void package depends on catatonit (roadmap T0.6).
- Report `RssAnon` next to the total in every measurement. The total can move with the file pages the kernel reclaims. `RssAnon` does not.
- T7.3 ships a+b+d for services-on realms. T7.1 carries (d) on the argv. T7.7 tests the logind mask, re-measures NFR-003 per distro with the ADR-003 argv, and measures NFR-003 on glibc and musl.
- NFR-021 is open. No roadmap task measures five concurrent realms yet. The shared pause should help, because the budget counts it once per realm.

## Method notes

- Each run: a realm starts, settles to `running` or `degraded` with zero failed units, then sits idle for 60 s. RSS is `VmRSS`, summed over conmon, the container processes and the pause. PSS comes from `smaps_rollup`.
- Gate, before each attempt and through the idle window: `MemAvailable` at least 1.5 GiB and PSI memory `some avg10` below 1.0. The host ran other workloads under memory pressure during the sweep.
- Excluded, not counted:
  - services-off runs 9 and 10 of the first pass. The pause's pages were reclaimed (pause RSS 12 KiB, 36 KiB swapped out). Their reruns are counted.
  - services-on baseline attempts 3 and 5. The idle-window PSI reached 3.86 and 1.57.
  - No a+b+d attempt was excluded.
- Flagged, counted. A flagged row has file pages reclaimed, so its RSS is low:
  - services-off runs 1, 7 and 8. Run 1 is low on `RssFile` (2.43 MiB). Runs 7 and 8 ran in the pressure window.
  - services-on baseline attempts 1 and 2. Their `RssFile` is 26.6 and 21.4 MiB, against a median of 30.6 MiB.
  - services-on a+b+d attempt 1. Its `RssFile` is 20.8 MiB, against a median of 31.3 MiB.
- The verdicts hold with and without the flagged rows. Without the flagged rows, the NFR-003 baseline median is 39.76 MiB and its max is 40.34 MiB. The a+b+d median is 37.65 MiB and its max is 37.96 MiB. Without run 1, the NFR-002 median is 4.57 MiB and its max is 4.70 MiB.

## Caveats

- One Debian 13 realm, on one host.
- The spike pinned `XDG_RUNTIME_DIR` under `/run/user/1000`, not `/run/ginn/<uid>`. The pause binary is the same either way.
- The NFR-003 runs used `--userns keep-id --network host` and omitted `--stop-timeout 30`. The ADR-003 argv adds `--stop-timeout 30`. S-05 notes that idle RSS does not depend on it. That was not measured in S-05r.
- The pause process changed during the sweeps. Services-off used pid 9758, then 28864 after a restart. Services-on used pid 8961. Every counted row records its pause RSS, which stayed between 0.64 and 0.73 MiB. S-05r does not say why the services-on pause is a different process. The budget counts one pause per realm either way.
- That all realms of one user share one pause follows from podman's rule that one `XDG_RUNTIME_DIR` has one pause (S-05b). It was not measured across realms.
- NFR-021 (5 concurrent realms) is not measured.
- The margins are thin: 3.30 MiB for NFR-002 at the max, and 2.04 MiB for NFR-003 with a+b+d at the max. T7.7 re-measures per distro.
- Four of the ten masked units have no unit file in this image, so their masks do nothing here: `systemd-udevd.service`, `systemd-udev-trigger.service`, `getty@tty1.service` and `e2scrub_all.timer`. Other Debian builds may ship them.
- Supersession. The S-05 verdicts are superseded by S-05r. They were NFR-002 FAIL at 10.6 MiB and NFR-003 baseline FAIL at 40.6 MiB. They ran podman's own pause on runc. The S-05b recommendation to wait for a catatonit run is superseded too.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| NFR-002 | Services-off budget passes. Median 4.55 MiB and max 4.70 MiB, against 8 MiB. The pause counts once per realm. |
| NFR-003 | Services-on budget. a+b+d passes at the median (37.63 MiB) and at the max (37.96 MiB), against 40 MiB. The unmitigated baseline passes at the median (39.68 MiB) and fails at the max (40.34 MiB). |
| NFR-021 | Not measured. The shared pause should help. Open. No roadmap task measures it yet. |
| FR-068 | `xdg-dbus-proxy` (about 4.7 MiB) must start lazily, or NFR-002 fails. |
