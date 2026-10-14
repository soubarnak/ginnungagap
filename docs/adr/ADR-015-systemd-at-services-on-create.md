# ADR-015: Init set at services-on create, through a provisioning stage

- Status: Proposed
- Date: 2026-10-14
- Revised: 2026-10-15 (package review). Open question 10 (the image-copy tmpdir) and Open question 13 (the shared base) are decided. CL-28 is committed (a8a0519), so ADR-014 is the only dependency left. Line references are checked against `void` at a8a0519. The S-11 names differ from the design's stage and reset names, and that is recorded as untested (Decision 2h, T7.27 item 11).
- Revised: 2026-10-15 (S-11 results). S-11 passed on all four distributions, and the decision text now carries the measured values. The commit reset is the two-commit form, the `rmi` refusal claim is removed, the arch cache clean is a `find` argv, the keep-id term is zero, and the image-copy tmpdir must move inside Owned Paths. See "S-11 results" and Open questions 8 to 13.
- Revised: 2026-10-15 (third review round). The machine-id copy and `cp_into` already exist (CL-28), so the ADR no longer adds them. `CreateSpec` splits the base digest from the derived image ID. `_check_init` becomes a free function. Provisioning execs carry `ExecTarget`. T7.17 waits for S-11. Line references are checked against the working tree.
- Revised: 2026-10-14 (second review round). The mode-change path reuses the realm's derived image, and only a services-mode change does so. Provisioning triggers on services-on or a recorded derived image, so arch is covered. Image removal selects by image ID, with the record's IDs kept. The boot check runs through the ADR-014 relay. The build phases are listed one by one. The disk formula covers the reuse path. The provisioning user is keep-id. The boot failure has its own code and detail. The overlap table lists ADR-014's real file set. Implementation depends on ADR-014 being on `void`. CL-28 is committed (a8a0519).

## Context

FR-080 requires that, with services on, systemd runs as the realm's init. ADR-003 starts a services-on realm with `/sbin/init` under `--systemd=always` (`src/ginn/runtime/spec.py:92`, `SERVICES_COMMAND`). The container therefore needs `/sbin/init` in its image, and it must start `/sbin/init` as its entrypoint (Decision 4).

Gap F1 (`spikes/results/F6-F1-probe.md`; T7.7 gate in `spikes/results/T7.7-services-real.md`): the pinned official images for Debian (`debian:stable-slim`), Fedora (`fedora-minimal:44`) and Kali (`kali-rolling`) have no `/sbin/init`. The full `debian:stable` and `fedora:44` images have none either. Only the Arch image (`quay.io/archlinux/archlinux:base`) ships systemd. A services-on realm on those three distributions cannot start.

Until this ADR lands, CL-28 (committed as a8a0519) refuses a services-on create on a distribution whose image lacks `/sbin/init`, with GINN-E-NO-INIT. This ADR reuses that code and check (Decision 2d).

The chicken-and-egg problem:

- The entrypoint of a services-on container is `/sbin/init`. The package set cannot be installed by running that container, because the container cannot run before systemd exists.
- Today's services-on rebuild replay (ADR-003 amendment 2026-10-14, decision 3; `ops/rebuild.py` `_Rebuild._replay`, `:422-438`) installs the Install Manifest with `podman exec` into a running services-on container. That path needs systemd already present.

User decision, 2026-10-14: keep the official pinned images (DR-040 trust). Install systemd at services-on create with the distribution's own package manager. Record the packages in the Install Manifest, so a rebuild replays them. This ADR decides how: the rebuild reinstalls the init set through provisioning from the current distribution data (Decisions 3 and 6), and `_replay` filters the init entries out of the manifest replay.

Requirements that constrain the decision:

- FR-010 AC-1: a created realm's package list equals image plus Integration Package Set. A services-on realm needs a documented addition (CL-31).
- FR-013: the Integration Package Set is limited to CA certificates and locale data.
- CR-007: realm contents are the official minimal image, the Integration Package Set, and requested packages with their dependencies.
- DR-040, FR-011 and NFR-SEC-004: each image is verified against the Trust Manifest by digest before use. A mismatch leaves no image layers. Only matched images are used.
- FR-012: package-manager signature checks are never disabled.
- FR-021 and DR-034: a rebuild builds a new root from a freshly verified image, reinstalls the Install Manifest, keeps the Realm Home, and swaps the new root in one atomic step.
- FR-066 and FR-083: a create-time change takes effect at the next start. A services-mode change alone changes only the container. Installed packages and the Realm Home are unchanged (user decision Q1, 2026-10-14, FR-083 read literally).
- FR-082: a services-off realm has no systemd process.
- FR-121: disk precheck, estimate times 1.10.
- FR-122, NFR-030: crash safety, resume and recovery.
- CR-006 and DR-010: after Host Setup, ginn writes only Owned Paths. The podman storage is an Owned Path.
- NFR-002, NFR-003, NFR-008, NFR-021: memory and disk budgets. Each is re-measured for each distribution.

Ranked characteristics (SRS section 4), in order:

1. Performance efficiency. One commit per provisioning. Reuse on a mode change costs no pull and no commit.
2. Host integrity and isolation. The install runs in a realm container with the realm's own package manager and signature checks. The result stays in ginn's podman storage. No host package manager runs.
3. Recoverability. A derived image is reproducible from the pinned digest, the current distribution data and the Integration Package Set. Each image is found and removed by its ID.
4. Interoperability. systemd comes from the distribution package, unmodified (FR-080).
5. Usability.

Facts from the reference host, the code and primary sources:

- The S-01, S-05 and S-05s Debian realm base is `spikes/Dockerfile.s01`. It is built `FROM docker.io/library/debian:stable` (full image, not `stable-slim`). It runs `apt-get install -y --no-install-recommends systemd systemd-sysv dbus procps`, then `apt-get clean` and `rm -rf /var/lib/apt/lists/*`. That image booted with zero failed units under the ADR-003 argv. S-05r and S-05s measured NFR-003 on it. The product set drops `procps` (orchestrator decision Q6), so the measured set changes by one package. S-11 measured the final set on the pinned `stable-slim` image (`src/ginn/trust/data/trust.toml`), and the host gates re-check it.
- The F6 finding (`spikes/results/F6-F1-probe.md`, sections A.1 and A.5): the arch base image carries `/etc/machine-id` with the literal `uninitialized`. A valid machine-id, written with `podman cp` before the first start, gives zero failed units. A bind mount of the machine-id breaks `systemd-machine-id-commit.service`.
- CL-28 (user decision 2026-10-14, committed as a8a0519) already implements the machine-id rule. The record keeps a random 32-hex `machine_id` (`src/ginn/store/records.py:225`, validated at `:261`, written at `:305`, read at `:367`). `_prepare_services` (`src/ginn/ops/create.py:375-385`) runs `_check_init` (`create.py:382`) and then `_copy_machine_id` (`create.py:384`, defined at `create.py:412`), which copies the value into `/etc/machine-id` with `cp_into` (`src/ginn/podman/adapter.py:407`, `cli.py:795`, `tests/fakes/fake_podman.py:431`). The copy runs before the first start. A rebuild reads the old record's value, so it writes the same value. This ADR keeps that code unchanged. The provisioning writes no machine-id of its own.
- The masks are eleven. Ten base unit paths (`spikes/s05/masks-base.txt`) and `systemd-logind.service` (`spikes/s05/masks-logind.txt`), which CL-28 masks by default. ADR-007 D8 records the change. Four of the ten base paths had no unit file on the S-05 image (`systemd-udevd.service`, `systemd-udev-trigger.service`, `getty@tty1.service`, `e2scrub_all.timer`). With systemd installed, most unit paths exist. The S-05 caveat changes per distribution.
- `podman commit` pauses the container while it commits by default. `--pause=false` turns that off. `--change LABEL key=value` sets image labels. An image name without a registry gets the `localhost/` prefix (podman-commit(1), read through ctx7 on 2026-10-14).
- Commit keeps the source container's configuration (entrypoint, command, user, environment, labels) unless `--change` overrides it. The provisioning container's entrypoint `sleep` and command `infinity` would otherwise persist in the image. On podman 5.8.8, `--change 'ENTRYPOINT []'` and `--change 'CMD []'` are accepted and change nothing. The reset is a `create --entrypoint '[]'` with no command, from a stage image (Decision 2g). S-11 checked the result with `podman image inspect`.
- Rootless `--userns keep-id` may copy image layers to map their owners. S-11 found no copy on debian, with or without keep-id (Decision 11).
- The `Podman` Protocol (`src/ginn/podman/adapter.py:317`) has no `commit`, no `diff` and no `images_by_name`. It has `cp_from` (`:399`) and `cp_into` (`:407`). `require_digest_ref` (`src/ginn/podman/adapter.py:41`) accepts only `repo@sha256:...`. `_check_spec` (`adapter.py:198`) calls it on `image_digest_ref` (`:199`). `CreateSpec.image_digest_ref` is declared at `adapter.py:97`, and `to_argv` appends it as the image argument at `:176`. `RealmSpec.__post_init__` (`src/ginn/runtime/spec.py:189`) calls `require_digest_ref(self.image)` at `:196`.
- The services-off base spec is `--init` with the keep-alive `sleep infinity` (`spec.py:86-87`, and the `create_spec` docstring at `:372`). Realm container labels are set at `spec.py:79-81`.
- The create's init check is `_check_init` (`src/ginn/ops/create.py:387`), called from `_prepare_services` at `create.py:382`, after the container exists. It copies `/sbin/init` out of the container and refuses with GINN-E-NO-INIT (CL-28).
- Integration steps depend on the host locale. `_integrate` (`src/ginn/ops/create.py:533-540`) calls `definition.integration_set(LANG)` and keeps `fallback_reason` in `_locale_note` (`create.py:540`; the `locale_note` property is at `create.py:214-216`).
- The rebuild's start path: `stale_container` (`src/ginn/ops/lifecycle.py:461-476`) names a services-mode change in its last clause (`:475`). `_rebuild_stale` (`lifecycle.py:479`) calls `rebuild_for_start` (`src/ginn/ops/rebuild.py:915`), which calls `_fixed` (`rebuild.py:607`), then `_locked` (`rebuild.py:703`), which constructs `_Rebuild` (`rebuild.py:734`).
- `ops/update.py` never pulls the image and never reads the Trust Manifest. It runs the `upgrade` verb inside the existing container (module header; `update.py:147-164`). The ADR-005 mechanism of R-003, in which an existing realm moves to a new pin on its first update, is not built in T8.4. A pin moves only by `ginn rebuild` (FR-021) or by a start-time provisioning build of Decision 7. `ops/update.py` needs no change for this ADR.
- `ops/images.py:52-75` skips removal while a container uses the image. podman 5.8.8 does not refuse `rmi` by ID of a base that has a derived child: it prints `Untagged`, the base record is gone (later lookups report `image not known`), and the child keeps running and removes cleanly (S-11; Decision 13).
- Relay ADR-014 (branch `services-supervisor-relay`, not yet on `void`) routes every exec into a services-on container through a relay. It adds a required keyword `target: ExecTarget` to `Podman.exec` and `exec_interactive`. `ExecTarget` carries the realm name, `services` and `mutating`. The boot check, the user manifest replay and the `_everything` replay of a services-on build all exec into such a container. This ADR uses that relay for all of them, one entry mechanism. Its number is reserved by that branch, so this ADR takes 015.

Assumptions, with the S-11 result for each (`spikes/results/S-11.md`):

- The Debian set is valid on `debian:stable-slim`. Confirmed: its installs return rc 0, and the image boots to `running` in 3 of 3 boots.
- The Fedora set `systemd dbus-broker` installs `/sbin/init` on `fedora-minimal:44`. Confirmed: it installs and boots to `running` in 3 of 3 boots.
- The Kali set is the Debian set. Confirmed: it boots to `running` in 3 of 3 boots.
- Arch, with an empty init set, boots from its committed derived image. Confirmed: `running` in 3 of 3 boots.
- The installed size of each set and the cache peak fit NFR-008 and the disk formula of Decision 11. Partly. The sizes and peaks are measured (Decision 11). NFR-008 is not measured (Open question 9), and the arch refresh delta is not in the formula (Open question 12).

## S-11 results

Spike `spikes/s11/`, result `spikes/results/S-11.md` (2026-10-15, podman 5.8.8, rootless, the four pinned images). Verdict: PASS. On all four distributions `systemctl is-system-running` printed `running` with no failed units, in each of three boots (12 of 12). Services-off containers from the derived images start with no systemd process (FR-082), and a package installed in one persists across a stop and start.

S-11 changed these parts of the decision:

- The commit reset is a two-commit form (Decision 2f, 2g). The `--change` form does nothing on podman 5.8.8.
- The arch `clean` is a `find` argv, because `pacman --sync --clean --clean --noconfirm` keeps the cache (Decision 3).
- keep-id adds no layer copy on debian, so Decision 11 has no keep-id term.
- podman does not refuse the `rmi` by ID of a base with a derived child, and the base record goes. The removal order stays, and each image is removed by ID (Decision 13). Open question 13 adds a guard for a base that another realm's derived image is built on.
- The measured sizes are in Decision 11. The image-copy tmpdir is `paths.image_tmp`, inside Owned Paths (Open question 10).

The values still open are Open questions 8 to 13.

## Decisions taken while drafting

The Q-numbered decisions that the text cites are listed here, so that each citation is self-contained. Each line states what the citing text decides. Q1 and Q4 are user decisions. The other Q numbers are orchestrator decisions. The ADR gives no other date for them, so each is dated 2026-10-14, the date of this ADR.

- Q1, user decision, 2026-10-14: on to off keeps the init set and the derived image, and the services-off container starts from that image (FR-083 read literally; Context, Decision 7).
- Q4, user decision, 2026-10-14: one derived image per realm for v1. A shared image per distribution and init set is rejected (Alternatives).
- Q2, orchestrator decision, 2026-10-14: arch still provisions, for the Integration Package Set and the commit, although its init set is empty (Decision 3).
- Q5, orchestrator decision, 2026-10-14: fedora's clean step is `dnf clean all` (Decision 3).
- Q6, orchestrator decision, 2026-10-14: the product set drops `procps`, which the S-01 base image installed, so the measured set changes by one package (Context).
- Q8, orchestrator decision, 2026-10-14: the delete order is kept, with derived images removed before the pinned base. podman does not enforce it (Decision 13).
- Q10, orchestrator decision, 2026-10-14: `ginn install` of an init entry is a manifest no-op that prints a note and changes nothing (Decision 14).
- Advisor decision for F11, 2026-10-14 (T7.7 review, roadmap M7): NFR-003 is judged on VmRSS, the SRS metric, and PSS is reported beside it as supplementary (Consequences).

## Decision

1. Every services-on realm is built in two stages, on every distribution, Arch included. The provisioning stage writes the root. The services-on container runs it. A services-on container never runs from a pinned image.

2. The provisioning stage (`ops/provision.py`), in order:

   a. Pull the pinned image by digest and verify it (FR-011, unchanged). A mismatch refuses before any provisioning (GINN-E-IMAGE-UNVERIFIED).

   b. Create the provisioning container. Its name is `ginn_provision_<realm>_g<gen>`. The underscore cannot occur in a realm name, so the name never collides with a realm container. Its only label is `io.ginn.provision=<realm>:<gen>`. Its argv is the services-off base spec (`--init`, `--entrypoint ["sleep"]`, command `infinity`) with `--userns keep-id` and `--user 0:0`, and without `--passwd-entry`. It has host network, no Realm Home, no devices and no mitigation mounts. The packages run as root inside the container, as the realm's own verbs do (ADR-011). S-11 found no layer copy with keep-id (Decision 11), so keep-id is the default.

   c. Run the following steps as argv lists from the distribution data, with signature checks on (FR-012): `refresh` (the create refresh of CL-9; on Arch this is the full upgrade, as today); `install` with `services_packages` (skipped when the list is empty); `install` with the Integration Package Set; the integration steps (for example `localedef` with `{locale}`). The `LANG` value is the one `_integrate` passes today, and the provisioning keeps the `fallback_reason` it returns, for `_locale_note` (Decision 8). Every exec in the provisioning stage passes `ExecTarget(realm=<realm>, services=False, mutating=True)` (ADR-014).

   d. Check `/sbin/init` in the provisioning container, before the commit, with `check_init`. `check_init` is a free function in `ops/provision.py` (T7.21). It replaces the method `_check_init` (`create.py:387`), and the create calls the same function. A missing `/sbin/init` refuses with GINN-E-NO-INIT (CL-28), and the provisioning container is removed. The F3 check after the create (`_prepare_services`, `create.py:382`) stays as the safety net, and it calls the same function.

   e. Run the `clean` argv steps (Decision 3).

   f. Stop the container. Stage-commit it to `localhost/ginn-init-<realm>:g<gen>-stage`, with this configuration. The stage commit does not reset the entrypoint or the command (see g).
      - `--change 'USER <value>'`, where the value is the pinned image's `User` from `podman image inspect`, or `0` when it has none.
      - `--change 'ENV <key>=<value>'` for every `Env` entry of the pinned image. `HOME` is one of them. S-11 found that podman adds `container=podman` on debian, kali and arch. Fedora adds nothing, because its base already sets `container=oci` (Open question 3). It is accepted, not stripped.
      - `--change 'LABEL io.ginn.provision='`, which removes the realm identity from the image. `--change 'LABEL io.ginn.init-set=<hash>'` and `--change 'LABEL io.ginn.base-digest=<digest>'`. The labels of the pinned image are kept.

   g. Reset the entrypoint and the command, through a second container. `--change 'ENTRYPOINT []'` and `--change 'CMD []'` change nothing, and `--entrypoint ""` gives `[""]`, so neither is used. Create the reset container `ginn_provision_<realm>_g<gen>_reset` from the stage image, with `--entrypoint '[]'`, no command, and the label `io.ginn.provision=<realm>:<gen>`, so that the prefix sweep finds it. It is created and never started. Commit it to `localhost/ginn-init-<realm>:g<gen>` with `--change 'LABEL io.ginn.provision='`, which removes the realm label from the image. The commit keeps the environment, the user and the labels of the stage image. The result has a null Entrypoint and a null Cmd on all four distributions (S-11). The label change on this commit is not in S-11, and the host gate checks it (T7.27, item 11).

   h. Remove the reset container, then the stage image by ID. Remove the provisioning container, and return the final image ID. Removal does not depend on podman refusing it (Decision 13). The names in this step are the design's and are untested: S-11 used another stage name and reset name, and its reset create carried no label (`spikes/s11/s11.py:704-735`; T7.27 item 11).

   What the services-on container inherits from the derived image: the environment (`container=podman` included), the base image labels, `io.ginn.init-set` and `io.ginn.base-digest`, and `io.ginn.provision` with an empty value. It takes no entrypoint and no command from the image, because the derived image has a null Entrypoint and a null Cmd (decision 2g). It sets its own name `ginn-<realm>-g<gen>` and its own labels `io.ginn.realm`, `io.ginn.distro` and `io.ginn.generation` (`spec.py:79-81`). No provisioning sweep matches it, because the sweep checks the name prefix `ginn_provision_` and the value `<realm>:<gen>` (Decision 10).

3. The init set and the cleaning are per-distribution data:

   - `services_packages` (new optional key, a list of package names): debian and kali `["systemd", "systemd-sysv", "dbus"]`; fedora `["systemd", "dbus-broker"]` (both installed and booted in S-11); arch `[]`. An empty list skips the init install only. Arch still provisions, for the Integration Package Set and the commit (orchestrator decision Q2). The key is added to `_OPTIONAL_TOP_KEYS` (`loader.py:124`).
   - `services_estimated_installed_bytes` (new key, an integer): the installed bytes of the init set, measured in S-11 (table in Decision 11): debian 19,166,208; fedora 24,586,672; kali 22,935,552; arch 0. Arch is 0 because its image already ships systemd.
   - `clean` (new optional verb, a list of argv steps): debian and kali `apt-get clean`, then `rm -rf /var/lib/apt/lists`, then `mkdir -p /var/lib/apt/lists/partial` (the state the pinned image ships with, checked in S-11); fedora `dnf clean all` (orchestrator decision Q5); arch `["find", "/var/cache/pacman/pkg", "-mindepth", "1", "-delete"]` (argv, untested). It empties the package cache and keeps the directory. The sync databases in `/var/lib/pacman/sync` stay, because `install` needs them (ADR-011). `find` is bounded to that directory inside the provisioning container, so this is not a host search. `pacman --sync --clean --clean --noconfirm` is not used: S-11 showed that `--noconfirm` answers its prompt with N, so the cache stays (4 files, 2,606,348 bytes). Whether `find` is in the arch base image is also untested (Open question 11). The verb is added to the `Verb` enum (`loader.py:140`), since `_REQUIRED_VERB_KEYS` is derived from the enum (`loader.py:158`), and to `_OPTIONAL_VERB_KEYS` (`loader.py:157`).
   - Current distribution data wins. Each provisioning installs the names in the current data. It rewrites the init entries of the Install Manifest to the names it installed. A later ginn version that changes the list changes the package set at the next provisioning, which is the next build of kind `provision` (Decision 7).

4. The services-on argv is the ADR-003 argv with two changes. It runs from a derived image, and it names `/sbin/init` as an explicit entrypoint with no command. The derived image has a null Entrypoint and a null Cmd (decision 2g), so the image's command cannot reach `/sbin/init` as an argument. S-11 checked this on the four boots: Entrypoint `/sbin/init`, Cmd null. `SERVICES_COMMAND` (`spec.py:92`) is removed, and `INIT_PATH` (`spec.py:90`) becomes the entrypoint.

   The image reference has two fields, and they stay apart:

   - `CreateSpec.image_digest_ref` (`adapter.py:97`) always holds the verified base digest. It is always checked by `require_digest_ref` (`_check_spec`, `adapter.py:199`). A pinned spec and a derived spec both carry it.
   - `CreateSpec.image_id: str | None` is a separate field, added in T7.20. It is checked by a new `require_image_id_ref` (`sha256:` plus 64 lowercase hex digits). When it is set, the CLI (`cli.py`) passes the image ID as the image argument to `podman create`, in place of the digest reference that `to_argv` (`adapter.py:176`) passes today. `_check_spec` checks `image_digest_ref` with `require_digest_ref` in every case, and `image_id` with `require_image_id_ref` only when it is set.
   - `RealmSpec` (`spec.py:151`) gets the same pair. Its `__post_init__` (`spec.py:196`) keeps `require_digest_ref(self.image)`, and checks `image_id` with `require_image_id_ref` when it is set. `create_spec` (`spec.py:369`) maps both fields.

   The machine-id copy is the existing CL-28 code, unchanged (Context). `_prepare_services` (`create.py:375-385`) copies the record's `machine_id` before the first start, for every services-on container. `_Rebuild` inherits it through `_create_container` (`rebuild.py:305`).

5. The boot check runs for every services-on container that a build starts. It runs after the start and before the attach, the user manifest replay and the `_everything` replay, and it runs even when the user manifest is empty. It goes through the ADR-014 relay, with `ExecTarget(realm=<realm>, services=True, mutating=True)`, the one entry mechanism for execs into a services-on container. It waits until `systemctl is-system-running` prints `running`, within the start timeout. The check fails when the state is `degraded` (Open question 1). A failed check raises GINN-E-REALM-BOOT, a new code. Its detail lists the failed units, read through the relay with `systemctl --failed --no-legend --plain`. The build then fails, and the old root stays (FR-021 AC-2). GINN-E-SERVICE-TIMEOUT is not used, because it does not name the failed units.

6. The records. Three new keys, each with a default, so the schema stays 1 (CL-20):

   - `init_image_id: str | None`, written at finish with the other entries. CL-21 has no exception for it.
   - `building_init_image_id: str | None`, written by `_mark_building` with the building fields of the rebuild (Decision 8).
   - `ManifestEntry.init: bool`, default `false` (`src/ginn/domain/packages.py:91`), written at finish with all other entries.

   `machine_id` is already a record key (CL-28, `records.py:225`), read as None when absent. It is not a new key in this ADR. T7.19 adds a round-trip test for it and no field.

   Rebuild `_replay` filters out entries with `init` set (`rebuild.py:422`). Provisioning installed them, and `exec` never installs them.

7. Image source of a build. The rebuild plan carries one field, `image_source` (`_Request`, `rebuild.py:580`), with three values:

   - `reuse`: keep the realm's current derived image, `init_image_id`, and its `image_digest`. No pull and no provisioning. The new container is created from `init_image_id`, after the pre-create ID check of Decision 9.
   - `provision`: pull the current pin, provision a new derived image, and set `image_digest` to the current pin.
   - `pinned`: pull the current pin and create the container from it, with no provisioning.

   The rule, applied in this order:

   - `reuse` when `init_image_id` is set and the start-time stale test (`stale_container`, `lifecycle.py:461-476`) fails only on its services clause (`:475`). Only the services mode changed. No tier, permission or create-time option changed.
   - `provision` when the target is services-on, or when `init_image_id` is set. A tier or permission change of a realm with `init_image_id` also takes `provision`, because it re-provisions from the current pin (FR-066). `ginn rebuild` (FR-021) is `provision` for any realm with `init_image_id` set, and for any services-on realm. A services-on start of a realm without `init_image_id` (off to on, lazy) is `provision`, and it runs under the CL-19 notice (Open question 6; T7.25).
   - `pinned` otherwise: a services-off target with no `init_image_id`. Such a realm keeps the pinned image, as now.

   Set by T7.23 in `rebuild_for_start` (`rebuild.py:915`) and `_fixed` (`:607`). The stale test that reports the services-only case is written in T7.25 (`lifecycle.py`). The mode changes:

   - On to off keeps the init set (user decision Q1, 2026-10-14). The realm keeps its derived image and its init entries. The services-off container starts from the derived image with the services-off argv. systemd is installed but not run (FR-082).
   - Off to on with `init_image_id` set uses `reuse`: the services-on container starts from the derived image, with no provisioning.
   - A mode change replays the user manifest through `exec`, as today, because the container's writable layer holds the user's packages (ADR-003 amendment 2026-10-14, decision 3). For a services-on target that exec goes through the relay (Decision 5).

8. Build order for a target generation gN+1. Each phase of `_Rebuild.run` (`rebuild.py:295-318`) is listed, with what it does for each `image_source`:

   | Phase (rebuild.py) | `provision` | `reuse` | `pinned` |
   |---|---|---|---|
   | `_check_space` (302) | stays; formula of Decision 11 | stays; reuse branch of Decision 11 | stays |
   | `_share_host_files` (303) | stays | stays | stays |
   | `_pull` (304) | stays: the current pin, verified (FR-011) | skipped; the ID check of Decision 9 runs before `_create_container` (T7.23) | stays |
   | provisioning (new, after 304) | runs (T7.21) | skipped | skipped |
   | `_create_container` (305) | from the new derived ID, with the services-on or services-off argv | from `init_image_id` | from the pinned digest |
   | machine-id copy, inside `_create_container` through `_prepare_services` (`create.py:375-385`) | services-on: runs, as today, with the record's `machine_id` (existing) | services-on: runs, as today | not run (services off) |
   | `_mark_building` (306) | stays; also writes `building_init_image_id` | stays; also writes `building_init_image_id` (the reused ID) | stays |
   | `_service_dir` (307) | stays, services-on | stays, services-on | stays, services-off: not used |
   | `_start_through_sv` or `_start` (308-311) | stays | stays | stays |
   | boot check (new, through the relay) | services-on: runs, always | services-on: runs, always | not run |
   | `_attach` (312) | stays | stays | stays |
   | `_check_user` (313) | stays | stays | stays |
   | `_integrate` (314) | dropped: IPS and the integration steps ran in provisioning. Its `_locale_note` comes from provisioning | dropped: the image already holds them. `_locale_note` is kept from the record's last build | stays |
   | `_everything` (315) | stays: the everything set, if the manifest holds one, is replayed as today | stays | stays |
   | `_replay` (316) | stays, with the `init` filter (Decision 6) | stays, with the `init` filter | stays |
   | `_stop_new` (317) | stays | stays | stays |
   | `_swap` (536, called from 318) | stays: saves `init_image_id`, `image_digest` and the init entries | stays: saves the same, `image_digest` unchanged | stays |

   Then, after `_swap`, the removal of Decision 13: containers first, then the derived images outside the keep set. `_check_space` uses the reuse branch of Decision 11 when `image_source` is `reuse`. The boot check comes before the attach, because the attach and the replay both exec into the container, and that exec needs systemd running.

9. Trust, the image-ID checks, and pins.

   - The derived image has no Trust Manifest entry. Its trust rests on three checks: its base is the pinned digest, verified before provisioning (FR-011); its packages come from the realm's package manager with signature checks on (FR-012, NFR-SEC-004); its image ID is recorded on the record and checked before each create from it and before each start of it.
   - The pre-create check (T7.23, `reuse` path): before `_create_container` creates a container from `init_image_id`, the image with that ID must exist, and its ID must equal `init_image_id`. A mismatch refuses with GINN-E-IMAGE-DERIVED. For a `provision` build, the ID is the one the final commit returned, and no pre-create check is needed.
   - The start check (T7.25, `lifecycle.py` start path): before a container from a derived image starts, its image ID must equal the record's `init_image_id` (or `building_init_image_id` during a rebuild). A mismatch refuses with GINN-E-IMAGE-DERIVED.
   - Ginn never pushes a derived image. DR-040 and NFR-SEC-004 are amended by CL-32. ADR-005 receives a dated `Amended:` line when the code lands. It states that derived images are added under this ADR and that the pin decision stands, so ADR-005 is amended, not superseded.
   - A pin moves only in a `provision` build. The record's `image_digest` then names the current pin. A `reuse` build keeps the old `image_digest`. No other path moves a pin, and `ops/update.py` does not move one.

10. Names, labels and sweeps.

    - Derived image: `localhost/ginn-init-<realm>:g<gen>`, with the labels `io.ginn.init-set` and `io.ginn.base-digest` and the pinned image's labels. It has no realm label.
    - Provisioning containers: `ginn_provision_<realm>_g<gen>` (the first, from the pin) and `ginn_provision_<realm>_g<gen>_reset` (from the stage image, Decision 2g). Both carry the label `io.ginn.provision=<realm>:<gen>`.
    - Stage image: `localhost/ginn-init-<realm>:g<gen>-stage` (Decision 2f). It is a tag of the derived image's repository, so the prefix sweep finds a leftover (the tag form is untested; T7.27 item 11), and the keep-set rule removes it like any other image outside the keep set.
    - Realm containers keep their names and labels.
    - Each kind is swept on its own. Provisioning containers are found by the name prefix `ginn_provision_<realm>_` and checked for the label value. Derived images are found by the name prefix `localhost/ginn-init-<realm>:` (the adapter's `images_by_name`, T7.20). A bare label key is never used as a sweep filter, because a realm container inherits the empty `io.ginn.provision` key. Realm names have no `:` or `_` (DR-002, `NAME_RULE`), so neither prefix matches another realm.

11. Disk (FR-121).

    - Create, podman storage: `required_storage = estimate_required(base + 2 × (init_delta + ips_delta) + cache_peak)`. `init_delta` and `ips_delta` are the installed sizes of the init set and the Integration Package Set. `cache_peak` is the package cache during the installs, before `clean`. S-11 measured it before clean: apt lists 21.6 MB (debian) and 34.4 MB (kali), dnf cache 109.2 MB (fedora), pacman cache 2.6 MB (arch). The factor 2 covers the provisioning container's writable layer and the committed layer, which exist together during the commit. `base` is counted once, because it is shared. S-11 found no keep-id copy (debian, measured inside `podman unshare`: upper layer 0 B after create, about 1.35 KB after start against 12 B without keep-id, and 62,915,910 against 62,914,572 bytes after a 60 MB write), so the formula has no keep-id term. The arch refresh is a full upgrade, and its installed delta is not in this formula (Open question 12). For debian the formula gives 189,117,833 bytes (base 81,256,893, plus 2 × (19,166,208 + 23,975,936), plus the apt lists 21,576,652 measured before clean). Its 1.10 margin gives 208,029,616 bytes. The no-keep debian commit peak was 194,778,719 bytes (`spikes/results/S-11.md`), above the raw formula and below the margin, with about 13 MB to spare. The other three peaks are keep-id lower bounds, so debian is the only comparable baseline for T7.27 item 5.
    - Create, commit staging: the image copy during a commit uses the image-copy tmpdir, `paths.image_tmp` (Open question 10), not the `/var/tmp` default. `required_tmpdir = estimate_required(init_delta + ips_delta)`, checked with `check_free` on `paths.image_tmp` before the commit, as a separate check from the storage check. S-11 saw under 1 MB there in every run, so its layer was not staged there in those runs. The check stays. `paths.image_tmp` is under the same data root as the podman storage (`paths.podman_root`). Whether `check_free_paths` counts two paths on one filesystem once is not checked here.
    - Rebuild, `provision`: `required_storage` is the create formula plus the realm's current disk use (the FR-020 figure, which must count gN's root and gN's derived image). Both stay until the swap. The tmpdir check is the same.
    - Rebuild, `reuse`: no new image bytes are made. `required_storage` is the realm's current disk use plus `estimate_required(one container's writable layer)`. S-11 measured that layer for a services-off container from the derived image, after an `htop` install: 23.5 MB (debian), 117.8 MB (fedora), 36.7 MB (kali) and 0.7 MB (arch). After a services-on boot the writable layer was 0.26, 0.02, 0.29 and 14.2 MB. Gate 0 step 7 named the services-off figure for this term, and the after-boot figures are smaller. The host gate (T7.27, item 11) checks the term against a reuse build's measured writable layer. There is no tmpdir check.
    - Rebuild, `pinned`: the same formula as `provision`, without the provisioning terms: base plus the realm's current disk use.
    - Steady state, for NFR-008: base plus `init_delta + ips_delta` plus the realm root. S-11 measured the deltas (table below). NFR-008 itself is not measured yet (Open question 9).
    - The Kali everything estimate is unchanged. It does not provision.

    S-11 measured sizes (`spikes/results/S-11.md`; bytes unless marked):

    | Distribution | Init set (`services_packages`) | `services_estimated_installed_bytes` | IPS installed (`ips_delta`) | Derived minus base | Peak root during provisioning | VmRSS median, 3 boots (MiB) |
    |---|---|---|---|---|---|---|
    | debian | systemd, systemd-sysv, dbus | 19,166,208 | 23,975,936 | 39,710,442 | 181,219,892 | 32.34 |
    | fedora | systemd, dbus-broker | 24,586,672 | 6,000,393 | 33,157,013 | 170,558,707 | 62.25 (over the 40 MiB budget; open) |
    | kali | systemd, systemd-sysv, dbus | 22,935,552 | 24,147,968 | 53,003,644 | 173,368,311 | 31.01 |
    | arch | none (the base has systemd) | 0 | 0 (already present) | 32,816,361 | 433,568,993 | 33.98 (run 3 read 12.21; open) |

    - `services_estimated_installed_bytes` equals the init-set installed bytes on each distribution. Arch is 0, because its image already ships systemd.
    - The peak root is a host `du` sample during provisioning. Host `du` cannot read a keep-id upper layer, so the keep-id peaks are lower bounds. The no-keep debian run reached 194,778,719 bytes at commit.
    - Storage for a keep-id container is sampled inside `podman unshare`, not from the host.
    - The arch derived delta has no init or IPS term. It comes from the full-upgrade refresh (Open question 12).

12. Create and resume.

    - A services-on create runs the provisioning stage, with the Integration Package Set and its steps, on every distribution (CL-18 as amended, CL-30). It creates the services-on container from the derived image, copies the machine-id (existing), writes the runit directory with `down`, and leaves the realm stopped. `--with` and `--everything` stay refused (GINN-E-NOT-SUPPORTED-YET, `_create_plan.py:314` and `:325`).
    - A services-on create checks `/sbin/init` twice, with the same function, `check_init`. The check before the commit (Decision 2d) refuses with GINN-E-NO-INIT. The check after the create (`_prepare_services`, `create.py:382`) remains the safety net.
    - Resume redoes the provisioning stage. Before it redoes anything, it removes the leftover provisioning containers (first and reset) and the leftover derived images of the realm, stage images included, under the realm lock. The selection is by ID, and the keep set of Decision 13 applies (for a create that did not finish, the keep set is empty).

13. Recovery, removal and journal steps. The rule for images is one rule everywhere. An image is selected by ID, from the listing by name prefix. The keep set is the record's `init_image_id` and `building_init_image_id`. An image outside the keep set is removed after the containers that use it are gone. Generations are never compared for images. Containers keep their generation-based selection (`remove_below`, `ops/recovery.py:950`).

    - After `_swap` (Decision 8): remove gN's containers, then every derived image of the realm outside the keep set, with the containers before the images. A `reuse` build keeps the same ID, so nothing is removed for it.
    - Recovery of an interrupted provisioning removes the provisioning containers (first and reset), then the stage and derived images outside the keep set, then marks the realm broken (FR-122).
    - Recovery and `remove_below` apply the same rule to older generations.
    - Delete (FR-019) stops and removes the realm containers, then the provisioning leftovers, then every derived image of the realm, then the pinned base through the existing `image-removed` step (`ops/delete.py` `STEPS`, `:129-138`). The order is kept (orchestrator decision Q8), but podman does not enforce it. S-11 showed that podman 5.8.8 does not refuse `rmi` by ID of a base with a derived child. The base record is removed (`image not known` afterwards), and the child keeps running and removes cleanly. Each image is selected by ID, so the removal does not depend on a refusal. The unit test and the host gate check the order. The shared-base guard is Open question 13: `remove_image` keeps a base while a derived image of another realm is built on it.
    - Journal steps are new durable-effect points: provisioning container created, init set installed, Integration Package Set installed, stage image committed, reset container created, final image committed (the image ID), reset container removed, stage image removed, provisioning container removed, derived image recorded for the build. Each one is a kill point for NFR-030.

14. Install Manifest actions (`ops/packages.py`, T7.26).

    - `ginn install <realm> <name>` where `<name>` is an init entry of that realm is a manifest no-op. It prints a note and changes nothing (orchestrator decision Q10). The note is printed text, not an error code.
    - `ginn remove <realm> <name>` where `<name>` is an init entry is refused with GINN-E-PKG-PROTECTED (`remove_packages`, `ops/packages.py:495`). The init set is needed for the realm to boot.

## Alternatives considered

- (b1) Install into the rootfs of the services-on container before its first start, through `podman mount` inside `podman unshare`, with a chroot. Rejected. The mount path is valid only inside the user namespace. Package scripts would run outside the realm's container namespaces, `/proc` and `/dev` must be mounted by hand, and the path bypasses the adapter (ADR-002).
- (b2) A services-off container that shares the services-on rootfs, through `podman create --rootfs`. Rejected. `--rootfs` takes a host directory and skips podman's image management. That skips the FR-011 digest check and the layer model. Two containers cannot share one writable layer.
- (b3) Copy a tar of the init set into a created services-on container with `podman cp`. Rejected. The tar must come from an install step in another container, so this reduces to Decision 2 with an extra copy.
- (c1) A different entrypoint until systemd exists: a keep-alive, install through `exec`, then recreate with `/sbin/init`. Rejected as a standalone design. The entrypoint is fixed at create, and podman cannot change it on an existing container. The result still needs a commit. The one-container benefit is kept by Decision 2 with the services-off base spec.
- (c2) `podman build` with a generated Containerfile: `FROM` the pinned digest, `RUN` in exec form. Rejected for v1. It adds a second engine path (buildah) with its own storage, UID map and isolation rules, none tested on the reference host. The `RUN` steps would run outside the realm's container argv (capabilities, seccomp, keep-id). It stays a later option for reproducible builds.
- (c3) Pin a third-party image that already has systemd. Rejected. DR-040 and IR-021 name the official channels. CR-007 limits realms to the official minimal image.
- (c4) Bind-mount a host systemd into the realm. Rejected. Host files in the realm break CR-007, and the host's systemd does not match the realm's distribution or libc.
- (c5) Run catatonit as PID 1 and start systemd as a service. Rejected. FR-080 requires systemd as the realm's init, and ADR-003 is built on `/sbin/init` as PID 1.
- (a2) Install the init set, the Integration Package Set and the user manifest all in the provisioning stage, then commit. Kept as the fallback if ADR-014 does not land. Rejected as the default. The commit writes the whole delta again, so a Kali everything rebuild, tens of GB, peaks near twice its size. Under (a2), `--everything` stays refused for services-on.
- Re-provision on every mode change. Rejected. It costs a pull and a commit for a change that needs only a new container argv, and it moves the pin without a rebuild. The `reuse` path of Decision 7 avoids both.
- A shared derived image per distribution and init set (one image for all realms). Rejected for v1 (user decision Q4, 2026-10-14). The earlier reason, that podman cannot remove a shared image, is wrong. Removal stays safe because ginn skips an image that a container uses (`ops/images.py:52-75`) and removes each image by ID, after its containers. S-11 showed that podman removes the base record of a base that has a child, and does not refuse it. The real costs are these. A shared image is not refreshed with its realms, so its packages go stale. Concurrent builds of one shared image need a build lock across realms. The Integration Package Set and its locale steps depend on the host `LANG` (`_integrate`, `create.py:533-540`), so the image key must include `LANG`. A per-realm image avoids all three costs. NFR-008 is measured per realm. Revisit only if NFR-021 fails on disk.

## Consequences

- F1 closes for Debian, Fedora and Kali on the pinned images. No new registry and no new trust source for pulls.
- Every services-on create now runs a network install, Arch included. FR-120 retries and the rollback of a failed create apply to it.
- Each services-on realm stores its own derived layer, 32.8 MB (arch) to 53.0 MB (kali) above its base (S-11; Decision 11). NFR-008 is re-checked per distribution (Open question 9).
- A services-mode change alone costs no pull and no commit (`reuse`). A tier or permission change re-provisions from the current pin. A `ginn rebuild` re-provisions from the current pin.
- The ADR-003 argv changes in two places: it runs from a derived image, and it names `/sbin/init` as an explicit entrypoint with no command. ADR-003 receives a dated amendment when the code lands. S-11 re-measured the argv (S-11 results), and the host gates re-measure it.
- ADR-007 masks: eleven, as CL-28 decides. Each mask takes effect differently per distribution. The per-distribution check is `systemctl show -p LoadState,ActiveState` for each of the eleven units, as in S-05s.
- NFR-002 and NFR-003 are re-measured per distribution on the derived image, with the ADR-003 argv. The S-05 and S-05s numbers came from `debian:stable`, not `stable-slim`, and do not transfer. NFR-003 is judged on VmRSS, the SRS metric (advisor decision for F11). PSS is reported beside it as supplementary, and it does not decide the verdict. S-11 medians on the derived images were 32.34, 62.25, 31.01 and 33.98 MiB (debian, fedora, kali, arch). Fedora is over the budget and not profiled, and arch run 3 is unexplained (Open question 8).
- The F6 result is required before NFR-003 is measured. Failed units must be zero after the machine-id copy. S-11 met this on 12 of 12 boots.
- Pin moves: a derived realm moves to a new pin only in a `provision` build (Decision 9). `ops/update.py` does not move pins. ADR-005's R-003 mechanism is not built in T8.4. ADR-005's amendment records this, and the roadmap carries it.
- Records: three new keys with defaults (Decision 6), and `machine_id` from CL-28 is already present. Schema stays 1. A downgrade across this change is unsupported while a derived image exists (CL-20 rule).
- Dependencies. Implementation of this ADR (T7.18 onward) depends on two things, both on `void` before the first brief goes out (item 2 is already done):
  1. ADR-014 merged into `void`. The boot check, the user manifest replay and the `_everything` replay of a services-on build exec into a services-on container through the ADR-014 relay (Decision 5). Without it, the exec placement is unverified (`spikes/results/S-01.md:37`).
  2. CL-28 committed (a8a0519). It changed the same files this ADR changes (see the overlap table).
  T7.17 (distribution data) needed S-11 for the measured sizes. S-11 is done (Gate 0), so T7.17 is no longer blocked by it.
- A services-on create execs into a services-on container only for its boot check and its existing F3 check, so it needs the relay for its boot check (Decision 5).
- The existing services-on error and warning text changes. GINN-W-SERVICES-PENDING no longer means "nothing installed" (`create.py:627`). GINN-W-REBUILD-PENDING names the init set download and the disk use, and it says that a start that must provision runs under the CL-19 notice (T7.25).

## Implementation plan

Plan only. Decisions above are binding after acceptance. Line numbers are from `void` at a8a0519 (CL-28 committed), and each brief checks them again.

### Dependencies and gates

- Not yet started: T7.18 to T7.27. Each waits for ADR-014 merged to `void`. CL-28 is committed (a8a0519).
- Done: Gate 0, the S-11 spike (PASS, 2026-10-15; `spikes/results/S-11.md`). T7.17 may start, because its brief now carries the measured `services_estimated_installed_bytes` values (Decision 11).

### Gate 0: host spike S-11 (one host run at a time)

Done 2026-10-15, PASS. The steps below are the plan that was run, with the corrections of the S-11 results section. The results are in `spikes/results/S-11.md`.

- Files: `spikes/s11/` (new spike script), `spikes/results/S-11.md` (new result).
- Route: the F6/F1 spike runner (`spikes/f6f1/`), which starts a services-on container from the supervisor leaf the way the T7.7 gate does. If the leaf needs a root write, the run needs the user's approval first (project CLAUDE.md, "Needs the user"; the 2026-10-14 approval covers `sudo ginn host-setup`).
- Distributions: `debian:stable-slim`, `fedora-minimal:44`, `kali-rolling` and `arch` (`quay.io/archlinux/archlinux:base`), each by digest from `trust.toml`.
- For each distribution:
  1. Pull by digest. Record `podman image inspect` of the base: Entrypoint, Cmd, User, Env, Labels, HOME.
  2. Create the provisioning container as Decision 2b, with `--root`, `--runroot`, `--tmpdir` and `XDG_RUNTIME_DIR` set to spike-owned paths.
  3. Run refresh, the init install (empty on arch), the Integration Package Set install, the integration steps (host `LANG`), the `/sbin/init` check (Decision 2d), and the clean steps. Record each argv.
  4. `podman diff` the container. Confirm the delta holds package paths and podman's mountpoint stubs only.
  5. Stop, stage-commit with the Decision 2f configuration, reset the entrypoint and command with the two-commit form (Decision 2g), and inspect the derived image against the base: Entrypoint and Cmd null, User as Decision 2f, Env as the base list (any extra variable reported), labels equal to the base labels plus the three ginn labels, and no realm label.
  6. Boot check: create the services-on container from the derived image ID, with the ADR-003 argv, the ADR-007 mitigations (eleven masks, CL-28), `--entrypoint ["/sbin/init"]`, and a random 32-hex machine-id copied in before the first start. Start it. Record `systemctl is-system-running --wait`, `systemctl --failed --no-legend --plain`, and VmRSS after a 60 s idle (informative).
  7. Storage. Record the peak storage of the podman root during provisioning and commit (sampled), the tmpdir free space, and the storage with and without `--userns keep-id` for the provisioning container. A keep-id container is sampled inside `podman unshare`, because the host `du` cannot read its upper layer. Record the writable layer of one services-off container from the derived image, for the `reuse` formula. Record the installed sizes (`init_delta`, `ips_delta`), `cache_peak` and the commit time.
  8. Services-off container: create a container from the derived image with the services-off argv (keep-id, `--passwd-entry`, `sleep infinity`). Check it runs, that no systemd process runs (FR-082), and that a package installed into it persists after a restart.
  9. Masks: `LoadState` and `ActiveState` for each of the eleven masked units.
  10. Child-image refusal: with a derived child present, attempt `podman rmi` of the pinned base and record the result (S-11: rc 0, `Untagged`, the base record gone afterwards, not refused). Remove the spike containers and images, which must come after step 10. Never run `podman system reset` or `podman system prune`.
- Pass criterion: `is-system-running` prints `running` with zero failed units on all four distributions, through the spike runner. If any distribution prints `degraded`, Open question 1 is reopened. The spike recorded the `services_packages`, `services_estimated_installed_bytes` and `clean` values for the distribution data. S-11 verified the package names. The FR-080 host gate (T7.27, item 1) reruns them.

### Wave 1A: after S-11 (S-11 is done)

- T7.17 distribution data. Files: `src/ginn/distros/data/{arch,debian,fedora,kali}.toml`, `src/ginn/distros/loader.py` (`_OPTIONAL_TOP_KEYS` at `:124`, the `Verb` enum at `:140` with the new `clean` member, `_OPTIONAL_VERB_KEYS` at `:157`), `tests/unit/test_distros_loader.py`. Adds `services_packages`, `services_estimated_installed_bytes` (the S-11 values in Decision 11) and the optional `clean` verb. The arch `clean` argv is `find /var/cache/pacman/pkg -mindepth 1 -delete`, marked as untested by a comment in the data, as the ADR's unverified-names pattern does (Decision 3). Tests: `@pytest.mark.fr("FR-013")` (the init set is documented and limited to services-on), `@pytest.mark.fr("NFR-060")` (behaviour in data).

### Wave 1B: after ADR-014 is on `void` (CL-28 is committed, a8a0519), serialised on `adapter.py` and `spec.py`

- T7.20 podman adapter, first. Files: `src/ginn/podman/adapter.py` (Protocol: `commit`, `image_id`, `diff`, `images_by_name`; `require_image_id_ref`; `CreateSpec.image_id`; the `_check_spec` branch of Decision 4), `src/ginn/podman/cli.py` (the image argument of `podman create` takes `image_id` when it is set), `src/ginn/podman/models.py` (`ImageInfo` gains `labels`, read from the image's `Labels` by `podman image inspect`, for the guard of Open question 13), `tests/fakes/fake_podman.py`, `tests/unit/test_podman_cli.py`, `tests/unit/test_podman_models.py`. `images_by_name` returns each image's `ImageInfo`, labels included. `cp_into` already exists (`adapter.py:407`, `cli.py:795`, `fake_podman.py:431`, CL-28). It is reused, not added. Every new `exec` call passes the `target` keyword of ADR-014. `CreateSpec` must emit `--entrypoint '[]'` with no command for the reset create (Decision 2g). The name rule of `_check_spec` must accept `ginn_provision_` names; the task checks it. Tests: `@pytest.mark.fr("FR-011")` (`image_digest_ref` is always checked by `require_digest_ref`; `image_id` is checked by `require_image_id_ref` only when set; the CLI passes the ID as the image argument when it is set), `@pytest.mark.fr("NFR-SEC-005")`.
- T7.18 runtime spec, second, after T7.20 in the same wave. Files: `src/ginn/runtime/spec.py` (`RealmSpec` gains `image_id`, and `__post_init__` (`:196`) keeps `require_digest_ref(self.image)`; the services-on entrypoint `INIT_PATH` (`:90`) with no command, and `SERVICES_COMMAND` (`:92`) removed; `create_spec` (`:369`) maps both fields), `src/ginn/runtime/provision.py` (new: the provisioning create argv, the reset create argv of Decision 2g (`--entrypoint '[]'`, no command, from a stage image), the stage and final commit argv with their `--change` lists, and the install, clean, diff and remove argv; no image-copy tmpdir argv is needed; the tmpdir is set in the environment and in containers.conf (T7.21, Open question 10), `tests/unit/test_runtime_provision.py` (new), `tests/unit/test_runtime_spec.py`. The task consumes `CreateSpec.image_id` and `require_image_id_ref` from T7.20 and does not edit `adapter.py`. Tests: `@pytest.mark.fr("NFR-SEC-005")` (argv lists only), `@pytest.mark.fr("CR-007")` (no Realm Home, device or passwd entry in the provisioning argv), `@pytest.mark.fr("DR-010")` (podman storage paths), `@pytest.mark.fr("FR-080")` (the services-on argv runs `/sbin/init` as its explicit entrypoint).
- T7.19 records and manifest. Files: `src/ginn/store/records.py` (`init_image_id`, `building_init_image_id`), `src/ginn/domain/packages.py` (`ManifestEntry.init`), `tests/unit/test_store_records.py`, `tests/unit/test_packages_manifest.py`. `machine_id` already exists (CL-28, `records.py:225`): T7.19 adds a round-trip test for it and no field. CL-28 is committed (a8a0519), and this task edits `records.py` on top of it. Tests: `@pytest.mark.fr("DR-035")` (schema 1 read and write with the new keys, and `machine_id` round trip), `@pytest.mark.fr("FR-021")` (init entries survive a record round trip).

### Wave 2: provisioning module (after T7.18, T7.19 and T7.20)

- T7.21 provisioning stage. Files: `src/ginn/ops/provision.py` (new), `tests/unit/test_ops_provision.py` (new), `tests/unit/test_crash_safety.py` (new kill points, this owner only), `src/ginn/ops/create.py` (one edit: `_check_init` (`create.py:387`) is replaced by the free function `check_init` from `ops/provision.py`, which `_prepare_services` (`create.py:375`) calls; this is the only `create.py` edit in this wave), `src/ginn/store/paths.py` (`_CONTAINERS_CONF` at `store/paths.py:54` is a fixed string. T7.21 turns it into a per-`OwnedPaths` render, with `image_copy_tmp_dir` as a TOML-quoted absolute path under `[engine]`, set to `paths.image_tmp`. `_write_owned_file` rewrites the file when its bytes differ, so an existing install picks up the key on its next `ensure_config()`, which `_prepare` calls before every podman run (`cli.py:436`). `TMPDIR` is already `image_tmp` on every `PodmanCli` call; Open question 10), `tests/unit/test_store_paths.py` (asserts the rendered `image_copy_tmp_dir` value), `src/ginn/errors.py` and `docs/errors.md` (this owner only), `tests/unit/test_errors.py`. `check_init` lives in `ops/provision.py`, so `create.py` imports from `provision.py` and `provision.py` never imports `create.py`.
  - Entry point: `provision_init(env, plan, journal) -> ProvisionResult(image_id, init_entries, locale_note)`, with rollback. Every exec passes `ExecTarget(realm=<realm>, services=False, mutating=True)`.
  - It also exports the helpers that the removal rule of Decision 13 needs: `remove_derived(podman, name, keep)` (selection by ID, containers first) and `sweep_provisions(podman, name)`. Those helpers are the only removal code for derived images, so T7.22, T7.23 and T7.24 depend on T7.21 for removal.
  - Retries go through `network_retry_hook` (`src/ginn/ops/retry.py:151`).
  - New codes, owned here: GINN-E-IMAGE-DERIVED (Decision 9), GINN-E-PKG-PROTECTED (Decision 14, raised by T7.26), GINN-E-REALM-BOOT (Decision 5, raised by the boot check in T7.23).
  - Reused: GINN-E-NO-INIT (CL-28, already in `errors.py`).
  - Tests: `@pytest.mark.fr("FR-011")` (digest mismatch before provisioning, no layers left), `@pytest.mark.fr("FR-012")` (no signature bypass in any install argv), `@pytest.mark.fr("NFR-030")` (kill at each provisioning point, including the stage commit, the reset create and commit, and the removals), `@pytest.mark.fr("DR-040")` (image ID check), `@pytest.mark.fr("FR-121")` (the disk formula of Decision 11), `@pytest.mark.fr("CR-007")` (the commit configuration of Decision 2f and the reset of Decision 2g: null Entrypoint and Cmd, no realm label on the final image), `@pytest.mark.fr("FR-080")` (GINN-E-NO-INIT before the commit, Decision 2d).

### Wave 3: after T7.21, one owner per file group, groups in parallel

- T7.22 create and resume. Files: `src/ginn/ops/create.py` (after T7.21: the services-on branch, `_finish_services` (`:618`), the record save of the stopped state (`:632`), the GINN-W-SERVICES-PENDING warning (`:627`)), `src/ginn/ops/_create_plan.py` (the `--with` and `--everything` refusals, `:314` and `:325`, stay), `src/ginn/ops/resume.py` (`_resumable` `:144`, the `_clear` step `:184-208`, the space check `_check_space` `:223`, which takes the reuse branch of Decision 11 only when a create has one). Tests: `tests/unit/test_ops_create.py` (`@pytest.mark.fr("FR-010")` the services-on set is image plus IPS plus init set, arch included), `@pytest.mark.fr("FR-080")` (init entries written at finish), `tests/unit/test_ops_create_resume.py` (`@pytest.mark.fr("FR-122")`, leftovers removed by ID before the redo), `tests/unit/test_cli_create.py`.
- T7.23 rebuild and the start-time plan. Files: `src/ginn/ops/rebuild.py` (`rebuild_for_start` `:915`, `_fixed` `:607`, `_Request` `:580`, `_locked` `:703`, `_Rebuild.run` `:295-318` as Decision 8, `_replay` `:422` with the `init` filter, `_swap` `:536`). The task does the following:
  - adds the `image_source` field of Decision 7 to `_Request`, and sets it in `_fixed` and `rebuild_for_start`;
  - adds the pre-create image-ID check on the `reuse` path (GINN-E-IMAGE-DERIVED, Decision 9), before `_create_container`;
  - skips `_integrate` for `provision` and `reuse` builds, carrying `_locale_note` from provisioning;
  - adds the boot check through the relay, with `ExecTarget(realm=<realm>, services=True, mutating=True)`, raising GINN-E-REALM-BOOT with the failed-unit detail.
  The machine-id copy is not changed: it stays in `_create_container` through `_prepare_services`, as it is today. Tests: `tests/unit/test_ops_rebuild.py` (`@pytest.mark.fr("FR-021")` AC-1 and AC-2 for each `image_source`; the boot check runs with an empty manifest; GINN-E-REALM-BOOT lists the failed units; the reuse path refuses a mismatched ID), `@pytest.mark.fr("FR-022")` (an init failure does not produce GINN-W-REPLAY-MISSING), `tests/unit/test_ops_rebuild_faults.py` (`@pytest.mark.fr("FR-083")`: on, off and on again, with `reuse`), `@pytest.mark.fr("FR-066")` (a tier change re-provisions).
- T7.24 delete and recovery. Files: `src/ginn/ops/delete.py` (`STEPS` `:129-138`, the new step `init-image-removed` before `image-removed`, and the cgroup directory rule of CL-28), `src/ginn/ops/images.py` (`remove_image`, the removal rule that delete, resume and recovery share, with the guard of Open question 13), `src/ginn/ops/recovery.py` (the provisioning sweep, `_decide_create`, `_decide_rebuild`, and the ID rule of Decision 13 for `remove_below` `:950`). Tests: `tests/unit/test_ops_delete.py` (`@pytest.mark.fr("FR-019")` AC-1: the reset and provisioning containers, then the stage and derived images, are removed by ID before the base, and no provisioning container remains; the test does not rely on a podman refusal, as S-11 showed), `tests/unit/test_ops_recovery.py` (`@pytest.mark.fr("FR-122")`, the keep set is never removed), `tests/unit/test_ops_images.py` (new; `@pytest.mark.fr("FR-019")`: a base that another realm's derived image is built on is kept, and is removed once that image is gone).
- T7.25 start path and notices. Files: `src/ginn/ops/lifecycle.py` (`stale_container` `:461-476`, which gains the services-only predicate; `_rebuild_stale` `:479`, which passes the reason to `rebuild_for_start`; the image-ID start check of Decision 9, GINN-E-IMAGE-DERIVED; `pending_rebuild_notice` `:451-458`), `src/ginn/ops/service.py` (`_services_body` `:273`, which states that on-to-off keeps the init set, and the notice of a services change). The GINN-W-REBUILD-PENDING text names the init set download and the disk use, and says that a start that must provision runs under the CL-19 notice. Tests: `tests/unit/test_cli_services.py` (`@pytest.mark.fr("FR-083")` AC-1, the CL-19 notice, on to off keeps the init entries), `tests/unit/test_ops_lifecycle.py` (`@pytest.mark.fr("FR-066")`, the stale test chooses `reuse` only for a services-only change, `@pytest.mark.fr("DR-040")` the image-ID check before start).
- T7.26 install and remove of init entries. Files: `src/ginn/ops/packages.py` (`install_packages` `:443` as a manifest no-op with a note for an init entry, `remove_packages` `:495` refusing with GINN-E-PKG-PROTECTED), `src/ginn/packages/manager.py` (only if `run_step`, at `:150`, needs the note; ADR-014 changes `run_step`, so this task merges after ADR-014). Tests: `tests/unit/test_ops_packages.py` (`@pytest.mark.fr("FR-040")` the install no-op, `@pytest.mark.fr("FR-041")` the refusal), and `tests/unit/test_packages_manager.py` if `packages/manager.py` changes.

### Host gates (T7.27, one host at a time, after wave 3)

1. FR-080 AC-1 and AC-2, with systemd as PID 1 and `systemctl enable --now`, on debian, fedora, kali and arch, through the T7.7 harness with the ADR-003 argv. Pass criterion: `running` with zero failed units on all four.
2. FR-081 AC-2: restart within 10 s, per distribution.
3. FR-083 AC-1: htop installed; services on, off and on again; systemd PID 1 when on; htop and the init set still present when off.
4. NFR-003 per distribution, judged on VmRSS (PSS supplementary), and NFR-021 (five idle realms, Should).
5. NFR-008 per distribution, and the FR-121 formula against the measured peak, for each `image_source`. The debian baseline is in Decision 11. On arch the formula does not model the refresh delta, so this item may fail there (Open question 12).
6. ADR-007 masks: `LoadState` and `ActiveState` for each of the eleven units, per distribution.
7. Services-on rebuild with a user manifest (the ADR-014 relay), and with an empty user manifest (the boot check only).
8. `create --resume` after a kill during provisioning (FR-122 AC-1). SIGKILL at each provisioning point (NFR-030, M10). The delete order: derived image before the base (Decision 13). The shared-base guard (Open question 13): two realms from one base; delete one and the base stays; delete the other and the base is removed.
9. Image-copy tmpdir (Open question 10, decided). During a pull and a commit, record which directory receives the image copy, and check that nothing is written to `/var/tmp` once the mechanism is set. S-11 set `--tmpdir` but not `TMPDIR` (`spikes/s11/s11.py`, `podman_env`), and its `podman info` still reported `imageCopyTmpDir: /var/tmp`, so that run does not show the decided setup. Pass: `podman info` under the ginn environment reports `imageCopyTmpDir` under `paths.image_tmp`, and `/var/tmp` gains no files during a pull and a commit.
10. Arch `clean` (Decision 3, Open question 11). The `find` argv empties `/var/cache/pacman/pkg`, keeps the directory and the sync databases, and a later `install` on the arch realm returns rc 0. If `find` is missing from the image, the fallback is `rm -rf` and `mkdir -p` on the same directory, also untested.
11. Storage per Decision 11, with keep-id containers sampled inside `podman unshare`. The reset commit's labels: the realm label is cleared on the final image (not in S-11). The names are untested as well: S-11 used the stage `localhost/ginn-init-s11-<key>-stage:g1`, which the prefix sweep of Decision 10 would not find, the reset container `ginn_reset_s11-<key>_g1`, and no label on the reset create. Check the designed names (Decisions 2f and 2g), that the sweep finds the stage tag, and that the stage is removed by ID.
12. Fedora's unit and process set, profiled for the VmRSS overage, and arch re-measured (run 3 read 12.21 MiB against about 34 MiB in runs 1 and 2), both before NFR-003 is judged (Open question 8).
13. NFR-021 (five idle realms) and NFR-008 (steady state per distribution), which S-11 did not measure (Open question 9).

### Files and overlap with ADR-014 and CL-28

Two columns: ADR-014 (its branch's ADR text and its uncommitted worktree) and CL-28 (committed as a8a0519).

| File | ADR-014 | CL-28 (committed, a8a0519) | ADR-015 task |
|---|---|---|---|
| `podman/adapter.py` | yes | yes | T7.20 (first in wave 1B) |
| `podman/cli.py` | yes | yes | T7.20 |
| `tests/fakes/fake_podman.py` | yes | yes | T7.20 |
| `runtime/spec.py` | yes (mounts and exec forms) | yes | T7.18 (after T7.20) |
| `runtime/provision.py` (new) | no | no | T7.18 |
| `store/records.py` | yes | yes | T7.19 (CL-28 is committed) |
| `domain/packages.py` | no | no | T7.19 |
| `errors.py`, `docs/errors.md`, `tests/unit/test_errors.py` | yes | yes | T7.21 (only owner in its wave) |
| `ops/provision.py` (new) | no | no | T7.21 (hosts `check_init`) |
| `ops/create.py` | yes | yes | T7.21 (one edit: `check_init`), then T7.22 |
| `ops/_create_plan.py`, `ops/resume.py` | no | no | T7.22 |
| `ops/rebuild.py` | yes | no | T7.23 |
| `ops/delete.py` | yes | yes | T7.24 |
| `ops/recovery.py` | yes | no | T7.24 |
| `ops/lifecycle.py`, `ops/service.py` | yes | no | T7.25 |
| `ops/packages.py`, `packages/manager.py` | yes | no | T7.26 |
| `ops/update.py` | yes (routes its execs through the relay) | no | none: no change for this ADR |
| `runtime/attach.py`, `runtime/enter.py` | yes | no | none expected |
| `distros/data/*.toml`, `distros/loader.py` | no | no | T7.17 (S-11 done) |
| `store/paths.py` | no | no | T7.21 (`containers.conf` `image_copy_tmp_dir`) |
| `podman/models.py` | no | no | T7.20 (`ImageInfo.labels`) |
| `ops/images.py` | no | no | T7.24 (guard of Open question 13) |

Merge rules:

- ADR-014 merges to `void` first. CL-28 is already committed (a8a0519). Only then do T7.18 to T7.26 start.
- Waves 1B, 2 and 3 rebase on the merged `void`. No wave of ADR-015 starts before the ADR-014 merge. The only parts that start earlier were S-11, which is done, and T7.17, which may now start.
- Inside a wave, `adapter.py` and `spec.py` have one owner at a time (T7.20 before T7.18). `errors.py` has one owner (T7.21). `create.py` is edited by T7.21 (one edit) before T7.22.

### Error codes

- GINN-E-NO-INIT (existing, CL-28): the check before commit (Decision 2d, `check_init`), and the check after the create (`_prepare_services`, `create.py:382`, Decision 12).
- GINN-E-IMAGE-DERIVED (new, owner T7.21, raised by T7.23 on the reuse path and by T7.25 at start): a recorded image ID does not match the image at create or start, or the commit failed.
- GINN-E-REALM-BOOT (new, owner T7.21, raised by T7.23): the boot check of a services-on build did not reach `running`. Its detail lists the failed units.
- GINN-E-PKG-PROTECTED (new, owner T7.21, raised by T7.26): `ginn remove` of an init entry.
- Reused: GINN-E-IMAGE-UNVERIFIED (base digest), GINN-E-PKG-MANAGER (an init or integration install refused; the CL-17 evidence rule does not apply to the init set), GINN-E-PKG-SIGNATURE, GINN-E-NETWORK (retries), GINN-E-DISK-SPACE (Decision 11), GINN-E-REALM-STATE, GINN-E-REALM-BUSY, GINN-E-NOT-SUPPORTED-YET (services-on `--with` and `--everything`, unchanged).
- Warnings whose text changes: GINN-W-SERVICES-PENDING (`ops/create.py:627`), GINN-W-REBUILD-PENDING (`lifecycle.py:458`).

The install note of Decision 14 is printed text, not a code.

## Open questions

1. `degraded` at the boot check (Decision 5). Closed by S-11: all four distributions reached `running` with zero failed units in 12 of 12 boots, so the `degraded` rule stands. GINN-E-REALM-BOOT still lists the failed units.
2. Fedora and Kali package names. Closed by S-11: `systemd dbus-broker` (fedora) and the Debian set (kali) install and boot on the pinned images. The FR-080 host gate (T7.27, item 1) reruns them.
3. Environment variables that podman adds. Closed by S-11: `container=podman` is added on debian, kali and arch, and fedora's base already sets `container=oci`. It is accepted and kept. It is podman's own marker, and podman also sets it in a container's run-time environment (podman's behaviour; S-11 read the image config only and did not measure the run-time value).
4. Closed by S-11. The `--change 'ENTRYPOINT []'` and `--change 'CMD []'` forms are accepted but change nothing on podman 5.8.8. The reset is the two-commit form of Decision 2g, which gives a null Entrypoint and a null Cmd on all four distributions. The label clear on the reset commit is not in S-11, and the host gate checks it (T7.27, item 11).
5. Closed for the sync databases. S-11 ran an `install` after the pacman clean, and it returned rc 0. The `find` cache clean that replaces the pacman clean is untested (Open question 11).
6. A services-on start that must provision (`provision` from an off-to-on change, Decision 7) can take minutes and needs network. The CL-19 notice covers it (T7.25). The user confirms that the notice is enough consent.
7. CL-28 writes the machine-id rule without CR-007. CL-33 asks for CR-007 to name it (see the CL text).
8. NFR-003 (not decided here). Fedora's VmRSS median is 62.25 MiB, over the 40 MiB budget, and its unit and process set is not profiled. Arch run 3 read 12.21 MiB against about 34 MiB in runs 1 and 2, and the cause is not identified. Before NFR-003 is judged, profile fedora's unit and process set and re-measure arch. The decision does not change.
9. NFR-021 (five idle realms) and NFR-008 (steady state against the base and the init deltas) were not measured by S-11. The deltas in Decision 11 are the input to NFR-008. T7.27 items 4 and 13 measure them.
10. Decided: the image-copy tmpdir is `paths.image_tmp` (`data/image-tmp`, `src/ginn/store/paths.py`), an Owned Path under the data root. It is not tmpfs, because images are large. Two settings point at it. `TMPDIR` is already `image_tmp` on every `PodmanCli` call (`PodmanCli._env()`, `src/ginn/podman/cli.py`), which covers every pull and, once T7.20 adds it, every commit, so T7.21 adds nothing for TMPDIR. The runit helper `ginn-wait` and the relay do not get that environment, and neither pulls or commits. The containers.conf that ginn writes (`_CONTAINERS_CONF`, `src/ginn/store/paths.py`) gains `image_copy_tmp_dir` with the same path, added by T7.21. The key is in the `[engine]` table of the installed containers.conf(5), whose default is `/var/tmp`. Which of the two podman honours is not verified, and the design does not depend on it, because both are set. S-11 is not evidence either way: its spike set `--tmpdir` but no `TMPDIR` (`spikes/s11/s11.py`, `podman_env`), and its `podman info` still reported `/var/tmp`. T7.27 item 9 confirms that nothing lands in `/var/tmp`.
11. The arch `clean` argv (`find /var/cache/pacman/pkg -mindepth 1 -delete`) is untested, and whether `find` is in the arch base image is untested too. The host check is T7.27, item 10.
12. Arch's derived delta is 32.8 MB with an init set and an IPS delta of zero. The arch refresh is a full upgrade, and its installed delta is not in the Decision 11 formula. The formula-vs-peak check may fail on arch (T7.27, items 5 and 13). No formula change is made here.
13. Decided: a shared base across realms is guarded. Realms built from one pinned base share it. S-11 showed that podman removes the base record when a derived child exists (`image not known` afterwards), and that the child keeps running. Without a guard, deleting one realm would remove the base under another realm, whose next create or provisioning build would then pull it again. The guard is in `remove_image` (`ops/images.py:52-75`), the removal rule that delete, resume and recovery share. Before it removes an image, `remove_image` lists the derived images by the name prefix `localhost/ginn-init-` (`images_by_name`, T7.20) and reads their `io.ginn.base-digest` label. When a derived image other than the one being removed carries a non-empty `io.ginn.base-digest` equal to the removed image's manifest digest (`ImageInfo.digest`), the removal is skipped, the outcome is `kept`, and a note names that derived image. The label is read after the prefix listing, and a bare label key is never a filter (Decision 10). The base is removed only when the last such derived image has gone, and delete's order is unchanged. The guard is in T7.24, and T7.27 item 8 checks it on the host. Two rollbacks call `rmi` directly and skip the guard: the create's `_remove_image` (`src/ginn/ops/create.py:660-674`) and the estimate's `_remove_image` (`src/ginn/ops/estimate.py:309-323`). Both run only when that operation pulled the base itself, under the image lock, so no other realm's derived image can sit on that base, except after a manual `rmi` outside ginn. T7.24 leaves them as they are.

## Requirement IDs

| ID | How this ADR bears on it |
|---|---|
| FR-010 | Services-on create installs the init set, the Integration Package Set and the integration steps, Arch included. Amends AC-1 for services-on realms (CL-31). |
| FR-011 | The pinned base is verified before provisioning. The derived image is checked by recorded ID before each create and start from it. |
| FR-012 | Init and integration installs use the distribution's signature checks. No bypass argv. |
| FR-013 | The init set is a documented per-distribution set, separate from the Integration Package Set (CL-31). |
| FR-014 | Unchanged. The Kali everything set does not provision. |
| FR-019 | Delete removes the derived image before the pinned base (Decision 13), and keeps a base that another realm's derived image is built on (Open question 13). |
| FR-020 | The realm's disk use is an input of the rebuild disk formula (Decision 11). |
| FR-021 | Rebuild builds from a freshly verified image, replays the user manifest, and swaps in one save. |
| FR-022 | An init failure fails the rebuild. It does not produce GINN-W-REPLAY-MISSING. |
| FR-040, FR-041 | An init entry cannot be removed (GINN-E-PKG-PROTECTED). An install of one is a manifest no-op with a note (Decision 14). |
| FR-066 | A create-time change, including a tier or permission change, re-provisions from the current pin. A services-mode change alone reuses the derived image (Decision 7). |
| FR-080 | systemd is the realm's init, from the explicit `/sbin/init` entrypoint on a derived image. The init check refuses an image without it (GINN-E-NO-INIT). |
| FR-081 | Supervision is unchanged. The gate measures restart after provisioning. |
| FR-082 | A services-off realm with a derived image does not run systemd. |
| FR-083 | A services-mode change changes only the container. On to off keeps the init set (user decision Q1, 2026-10-14). |
| FR-120 | Network retries apply to the provisioning installs. |
| FR-121 | Disk formula of Decision 11, with the commit staging check and the reuse branch. |
| FR-122 | Resume and recovery remove provisioning leftovers by ID before they redo or mark the realm broken. |
| FR-123 | The provisioning stage runs under the realm lock. |
| NFR-002 | Services-off realms without init entries are unchanged. Measured only to confirm no regression. |
| NFR-003 | Re-measured per distribution on the derived image, VmRSS verdict (host gate 4). S-11 medians 32.34, 62.25, 31.01 and 33.98 MiB; fedora and arch open (Open question 8). |
| NFR-008 | Per distribution (host gate 5). S-11 measured the deltas; NFR-008 itself is open (Open question 9). |
| NFR-021 | Five idle derived realms (host gate 4). Not measured in S-11 (Open question 9). |
| NFR-030 | Journal steps at each provisioning effect. Kill points in T7.21 and the M10 host gate. |
| NFR-SEC-004 | Derived images are built from a matched base and checked by recorded ID (CL-32). |
| NFR-SEC-005 | Every provisioning argv is a list. |
| CR-006 | Provisioning runs in a realm container. No host package manager. |
| CR-007 | Realm contents gain the init set (CL-31). The commit configuration keeps realm identity off the image (Decision 2f). The machine-id copy, which CL-28 already runs, is named by CL-33. |
| DR-010 | Derived images, stage images and provisioning containers stay in the Owned podman storage. The image-copy tmpdir is `paths.image_tmp`, under the data root (Open question 10). |
| DR-034 | The swap of gN+1 for gN is one record save, as before. |
| DR-035 | New record keys with defaults. Schema stays 1 (CL-20). |
| DR-040 | Amended (CL-32): a derived image has no manifest entry. It is trusted by its matched base, the package signature checks and its recorded ID. |
| AS-024 | A derived realm moves to a new pinned base only in a `provision` build (Decision 9). |
