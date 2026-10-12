# ADR-007: Idle memory budget

- Status: Accepted
- Date: 2026-10-12
- Amended: 2026-10-12 (D5). The attribution rule counts ginn's helpers. The S-05r PASS covers the realm and the catatonit pause only. The services-on mitigations are create-time mounts and environment, not files in realm /etc (CR-007). Those forms are unmeasured until S-05s. `xdg-dbus-proxy` has a per-realm lifetime. NFR-021 is placed in T7.7.
- Amended: 2026-10-12 (D6). Mitigation (a) is the journald drop-in, mounted read-only. It replaces the tmpfs journal of D5. The S-05r content is the drop-in, measured as an image layer. The S-05s criterion is restated. Podman's mountpoint stubs are podman's writes (CR-007). The proxy claim is marked as not measured. No threshold or mitigation set changes.

## Context

NFR-002 caps an idle services-off realm at 8 MiB RSS. It counts every host-side and in-realm process that ginn or the runtime owns, and it excludes user apps. NFR-003 caps an idle services-on realm at 40 MiB, with systemd and its default units included. NFR-021 asks for 5 concurrent realms, each within those budgets (Should). FR-068 requires the session D-Bus filter, which runs `xdg-dbus-proxy`.

Ranked characteristics (SRS section 4): performance efficiency comes first. So memory decides the mitigations. The journal's persistence (recoverability, third) and the default log level (usability, fifth) give way to it. Interoperability (fourth) settles the logind question. A mask that breaks FR-080 apps is not the default.

Measured on the reference host (Void Linux glibc, kernel 7.2.9_1, podman 5.8.8, crun 1.30.1, conmon 2.2.1, catatonit 0.1.7 at the podman lookup path). Realm image: Debian stable (Debian 13.7, systemd 257.13). Source: `spikes/results/S-05.md` (S-05r, section "Services-on under the delegated launch leaf" for the services-on rows) and `spikes/s05/`. Each row is the median or max of 10 counted runs, in MiB:

| Budget | Config | Median total | Max total | Limit | Median margin | Max margin | Verdict |
|---|---|---|---|---|---|---|---|
| NFR-002 | services off, realm plus catatonit pause | 4.55 | 4.70 | 8 | +3.45 | +3.30 | PASS (realm and pause only) |
| NFR-003 | services on, no mitigation | 39.68 | 40.34 | 40 | +0.32 | -0.34 | median PASS, max FAIL (realm and pause only) |
| NFR-003 | services on, a+b+d, mitigations as image layers (S-05r) | 37.63 | 37.96 | 40 | +2.37 | +2.04 | PASS (realm and pause only) |

What the PASS covers. Each verdict counts the realm processes and ginn's catatonit pause, and nothing else. The helpers that the budget also counts (`ginn-watch`, `ginn-idle`, `runsv` and `ginn-wait`) were not part of the S-05r measurement. T7.7 measures them. This ADR gives no estimate of their RSS. The a+b+d row also used mitigations written as image layers, which is not the create-time form in the Decision.

The RSS split explains the spread. `RssAnon` is steady: 5.5 to 5.7 MiB per services-on realm, and 0.3 MiB per services-off realm. The spread between rows is in `RssFile`. The kernel reclaims file pages under memory pressure without any swap.

Attribution rule. The budget counts four kinds of process:

- Every realm process: conmon, the container init and its children.
- ginn's rootless pause process. podman starts it as `catatonit -P` when catatonit exists at `/usr/libexec/podman/catatonit` (S-05b). ginn pins `XDG_RUNTIME_DIR` to `/run/ginn/<uid>` (ADR-004), so the pause is ginn's own. All of one user's realms share it. The budget counts it once per realm. That is the worst case, because a realm that runs alone pays the full pause.
- ginn's helpers: `ginn-watch`, `ginn-idle`, `runsv` and `ginn-wait`. NFR-002 and NFR-003 count every process attributable to ginn and the runtime, so these count too. `runsv` is runit's per-service supervisor, and `ginn-wait` is the process runit supervises (roadmap, native helpers).
- Not counted: user apps. The runit user supervisor `runsvdir`, which Host Setup starts, is outside this budget. NFR-001 gives it its own limit of 1 MiB.

## Decision

ginn ships catatonit. podman then starts `catatonit -P` as the rootless pause, and the budget counts that pause once per realm.

Services-on realms carry three mitigations by default. This ADR calls them a+b+d. The S-05r measurement put them into image layers (`spikes/s05/Dockerfile.mit`), which writes files into the realm's /etc. CR-007 limits realm contents to the image, the Integration Package Set and requested packages, so the product does not write them into /etc. The product form uses create-time mounts and environment on the create argv:

- (a) The journal stays in RAM, capped at 8 MiB. ginn bind-mounts a journald drop-in read-only at `/etc/systemd/journald.conf.d/ginn.conf` on the create argv. The drop-in is a ginn-owned file, written under an Owned Path (DR-010). Its content is `[Journal]`, `Storage=volatile` and `RuntimeMaxUse=8M`. `Storage=volatile` writes the journal to the runtime directory, and `RuntimeMaxUse=8M` caps it. S-05r measured that content, but it installed the content as an image layer (`spikes/s05/journald-volatile.conf`, as `/etc/systemd/journald.conf.d/50-ginn-s05-volatile.conf`). The product changes both the file name and the delivery, so S-05s checks the mounted form. If the image lacks the `journald.conf.d` directory, podman creates the mountpoint stub in the container layer. That write is podman's, not ginn's (CR-007).
- (b) Ten base units are masked. Each unit path under `/etc/systemd/system` (the list in `spikes/s05/masks-base.txt`) gets a bind mount of `/dev/null` on the create argv. The S-05r form was a symlink to `/dev/null` in an image layer. In the Debian image that S-05 ran, four of the ten units have no unit file, so their masks do nothing there: `systemd-udevd.service`, `systemd-udev-trigger.service`, `getty@tty1.service` and `e2scrub_all.timer` (S-05, mitigation section). The image was built from the same Dockerfile, rebuilt later; S-05r did not re-check the units. Where the image lacks a unit path, podman creates the mountpoint stub in the container layer. That write is podman's, not ginn's (CR-007).
- (d) `SYSTEMD_LOG_LEVEL=warning` is passed as environment with `-e` on the create argv. S-05r ran this form (its environment variable, not a file).

Verification S-05s before T7.3 checks two things. First, journald honours the mounted drop-in, and journald's computed maximum is recorded in the S-05s result. Second, the idle median is within ±1 MiB of 37.63 MiB, the S-05r a+b+d median. S-05s does not show that the `/dev/null` binds mask anything. The (b) effect measured in S-05 was -0.2 MiB, inside noise, so an in-range median does not prove the masks took effect. Fallback: in-realm files under CL-10. T7.3 does not ship these mitigations until S-05s passes or CL-10 is decided.

Masking `systemd-logind.service` (`spikes/s05/masks-logind.txt`) is not default. T7.7 tests it against the FR-080 apps first.

`xdg-dbus-proxy` (FR-068) runs one proxy per realm. It starts on the first ginn launch that needs it, and it exits with the last ginn-launched app. An idle realm has no proxy. The NFR-002 figure holds once T6.5 implements the per-realm lazy proxy lifetime; it is not measured. One resident proxy takes about 4.7 MiB (S-05; S-05r did not re-measure it). That alone breaks the services-off budget, so the proxy must start lazily. T6.5 implements this lifecycle. It is not measured yet.

## Alternatives considered

- Unmitigated services-on (the baseline). Rejected as the default. The median passes at 39.68 MiB. The largest counted row is 40.34 MiB, and the margin at the median is 0.32 MiB. That margin is too small to rely on across distros, and T7.7 re-measures per distro.
- Masking logind as well (a+b+logind+d). Not the default. S-05 (runc, podman's pause, before S-05r; superseded) measured a services-on median of 29.64 MiB with it. FR-080 apps were not tested with logind masked. T7.7 decides.
- Podman's own pause, with no catatonit. Rejected. S-05 (superseded) measured the pause at 6.33 MiB median. That made the services-off total 10.58 MiB, which fails NFR-002.
- Treating the pause as per-user overhead outside the budget (S-05's first option). Rejected. NFR-002 counts every process that ginn or the runtime owns. With ginn's own pinned runtime directory, the pause is ginn's.
- An always-on `xdg-dbus-proxy`. Rejected. One proxy takes about 4.7 MiB (S-05). Added to the S-05r services-off median of 4.55 MiB, that gives about 9.25 MiB, which is over 8 MiB. S-05r did not re-measure the proxy. The figure is the S-05 number.
- Default journald (persistent). Rejected. Journald is one of the two large consumers in the services-on realm. S-05 (superseded) measured the base median at 40.59 MiB and the volatile-with-cap median at 37.90 MiB, which is about 2.7 MiB less.
- A tmpfs of 8 MiB at `/var/log/journal`, the form D5 first drafted. Not chosen. It was never measured. It drops `RuntimeMaxUse`, so journald's default size limit applies inside the tmpfs instead of the 8 MiB cap that S-05r measured.
- Mitigations written as files in realm /etc (the S-05r image-layer form, or in-realm files at create). Not chosen for the product. CR-007 does not allow them as written (CL-10). This is the fallback if S-05s fails the create-time forms.

## Consequences

- Services-off: the S-05r PASS (median 4.55 MiB, max 4.70 MiB, margin 3.30 MiB at the max) covers the realm and the catatonit pause only. The helpers that NFR-002 also counts are not in that number. T7.7 measures them.
- Services-on: the S-05r a+b+d PASS (median 37.63 MiB, max 37.96 MiB) was measured with the mitigations as image layers. The create-time form is unmeasured until S-05s. The unmitigated baseline passes at the median and fails at the max.
- Cost of (a): journals live in RAM, and logs are lost when the realm stops. That costs recoverability (third in the ranking). ginn accepts it because performance efficiency ranks first.
- Cost of (d): PID 1 logs warnings and above only.
- Services-off: S-05 (superseded run, per-process medians) measured conmon at about 1.9 MiB and the init at about 0.7 MiB as the realm's resident processes. Any helper that a services-off realm runs (`ginn-idle` from T7.6, and any `runsv` or `ginn-wait`) is not in that measurement. T7.7 measures them. Each one counts against the 8 MiB budget.
- `xdg-dbus-proxy` must start lazily (FR-068, T6.5), one per realm, and exit with the last ginn-launched app.
- The catatonit package must supply the lookup path. If it is missing, podman starts its own pause (6.33 MiB median in S-05), and NFR-002 fails. The Void package depends on catatonit (roadmap T0.6).
- Report `RssAnon` next to the total in every measurement. The total can move with the file pages the kernel reclaims. `RssAnon` does not.
- T7.3 ships the a+b+d mitigations as create-time mounts and environment, once S-05s passes (CL-10 is the fallback). T7.1 carries (d) on the argv. T7.7 tests the logind mask, measures the helpers, re-measures NFR-003 per distro with the ADR-003 argv, and measures NFR-003 on glibc and musl.
- NFR-021 (five concurrently running idle realms, Should) is measured in T7.7 with the same RSS harness. Not measured yet. The budget counts the pause once per realm, while the host shares one pause per user (S-05b). The measured five-realm total is therefore expected to be lower than the sum of the five budgets. T7.7 must confirm that.

## Method notes

- Each run: a realm starts, settles to `running` or `degraded` with zero failed units, then sits idle for 60 s. RSS is `VmRSS`, summed over conmon, the container processes and the pause. PSS comes from `smaps_rollup`.
- Gate, services-on sweep only (S-05r section "Services-on under the delegated launch leaf"): MemAvailable at least 1.5 GiB and PSI `some avg10` below 1.0 before each start. The gate is re-checked every 30 s for up to 15 min, and every 10 s through the 60 s idle window. An attempt is excluded (`excluded-pressure`) when the gate closes inside the idle window, or when any realm process has VmSwap above 0. The services-off sweep has no memory gate. Its rows 9 and 10 were first recorded as `ok`, then relabelled `excluded-pressure` by hand after the run (pause RSS 12 KiB, with 36 KiB swapped out). The exclusion rule the script applies later (pause RSS below 256 KiB, or realm VmSwap above 0) was chosen after the fact to reproduce that hand decision. It was checked on fixture rows only, and the committed rows were not produced by it.
- Excluded, not counted:
  - services-off runs 9 and 10 of the first pass (see above). Their reruns are counted.
  - services-on baseline attempts 3 and 5. The idle-window PSI reached 3.86 and 1.57.
  - No a+b+d attempt was excluded (10 of 10 counted, VmSwap 0 at every sample).
- Flagged, counted. A flagged row has file pages reclaimed, so its RSS is low:
  - services-off runs 1, 7 and 8. Run 1 is low on `RssFile` (2.43 MiB). Runs 7 and 8 ran in the pressure window.
  - services-on baseline attempts 1 and 2. Their `RssFile` is 26.6 and 21.4 MiB, against a median of 30.6 MiB.
  - services-on a+b+d attempt 1. Its `RssFile` is 20.8 MiB, against a median of 31.3 MiB.
- The verdicts hold with and without the flagged rows. Without the flagged rows, the NFR-003 baseline median is 39.76 MiB and its max is 40.34 MiB. The a+b+d median is 37.65 MiB and its max is 37.96 MiB. Without run 1, the NFR-002 median is 4.57 MiB and its max is 4.70 MiB.

## Caveats

- One Debian 13 realm, on one host.
- The spike pinned `XDG_RUNTIME_DIR` under `/run/user/1000`, not `/run/ginn/<uid>`. The pause binary is the same either way.
- The NFR-003 runs used `--userns keep-id --network host` and omitted `--stop-timeout 30`. The ADR-003 argv sets `--stop-timeout 30`. S-05 (superseded) says that idle RSS does not depend on the flag. That is an assertion in that spike, not a measurement, and S-05r did not measure it. T7.7 measures the ADR-003 argv.
- The pause process changed during the sweeps. Services-off used pid 9758, then 28864 after a restart. Services-on used pid 8961. Every counted row records its pause RSS, which stayed between 0.64 and 0.73 MiB. S-05r does not say why the services-on pause is a different process. The budget counts one pause per realm either way.
- That all realms of one user share one pause follows from podman's rule that one `XDG_RUNTIME_DIR` has one pause (S-05b). It was not measured across realms.
- NFR-021 (5 concurrent realms) is not measured.
- The margins are thin: 3.30 MiB for NFR-002 at the max, and 2.04 MiB for NFR-003 with a+b+d at the max. T7.7 re-measures per distro.
- Four of the ten masked units have no unit file in this image, so their masks do nothing here: `systemd-udevd.service`, `systemd-udev-trigger.service`, `getty@tty1.service` and `e2scrub_all.timer`. Other Debian builds may ship them. S-05s decides how to handle them (see the Decision).
- Supersession. The S-05 verdicts are superseded by S-05r. They were NFR-002 FAIL at 10.6 MiB and NFR-003 baseline FAIL at 40.6 MiB. They ran podman's own pause on runc. The S-05b recommendation to wait for a catatonit run is superseded too.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| NFR-002 | Services-off budget. S-05r PASS: median 4.55 MiB and max 4.70 MiB, against 8 MiB. Covers the realm and the catatonit pause only. The helpers the budget also counts are measured in T7.7. |
| NFR-003 | Services-on budget. S-05r, a+b+d with mitigations as image layers: median 37.63 MiB and max 37.96 MiB, against 40 MiB. The unmitigated baseline passes at the median (39.68 MiB) and fails at the max (40.34 MiB). Same coverage as NFR-002. The create-time form is unmeasured (S-05s). |
| NFR-021 | Five concurrent idle realms. Not measured. Placed in T7.7 (SRS wording: "Run 5 idle realms, measure"). |
| FR-068 | `xdg-dbus-proxy` (about 4.7 MiB, S-05) starts on the first launch that needs it, one per realm, and exits with the last ginn-launched app. An idle realm has none. T6.5. |
| CR-007 | The mitigations are create-time mounts and environment. ginn writes no file into the realm's /etc. Where the image lacks a target path, podman creates the mountpoint stub in the container layer, and that write is podman's. The fallback that writes files into realm /etc needs CL-10. |
