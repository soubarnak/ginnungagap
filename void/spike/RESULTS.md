# M0 spike results (2026-10-04)

Host: Void Linux x86_64, glibc 2.41, kernel 7.2.8, runit, elogind 252.39, AppArmor enabled,
LXC 6.0.3. Guest: Ubuntu noble from `debootstrap`, systemd as PID 1, shared host network.

| Check | Result |
|---|---|
| `lxc-start` boots a systemd guest | PASS, but only with the cgroup workaround below |
| Guest sees pure cgroup2 at `/sys/fs/cgroup`, `container=lxc` | PASS (`cgroup2fs`) |
| `lxc-attach --clear-env -- systemctl is-system-running --wait` | PASS (`running`; `degraded` under AppArmor, see open items) |
| `lxc-attach -- systemd-run --pipe --wait --uid=1000 -p PAMName=login ...` | PASS, uid 1000, exit code 7 propagated |
| `--clear-env` stops host `DBUS_SESSION_BUS_ADDRESS` leaking into the guest | PASS |
| Runtime bind via `open_tree` + `setns` + `move_mount` (`bind.py`) and `umount -l` | PASS |
| Live `lxc-cgroup devices.deny/allow` changes what the guest can open | PASS (open of `/dev/null` denied, then allowed) |
| `lxc-stop -t 30` halts the guest cleanly | PASS |
| Guest runs under `lxc-container-default-cgns` AppArmor profile | PASS (profile must be loaded first, see below) |

## Findings that change the design

1. **elogind's v1 hierarchy breaks LXC.** `/proc/self/cgroup` shows `1:name=elogind:/` next to
   `0::/`. LXC classifies that as a legacy layout and aborts with
   `Failed to set "devices.deny" to "a"`. Fix: run every `lxc-*` command through
   `void/bin/spaces-lxc`, which hides `/sys/fs/cgroup/elogind` in a private mount namespace.
   The host's elogind is untouched. All `lxc-start`, `lxc-attach`, `lxc-cgroup`, `lxc-stop`
   calls must use the wrapper, otherwise lxc-attach falls back to the same failure.
2. **Stock LXC AppArmor profiles break systemd-logind.** Under `lxc-container-default-cgns`,
   `-with-nesting` and `-with-mounting`, guest `systemd-logind` fails: AppArmor denies the
   `rbind` of `/` to `/run/systemd/mount-rootfs/` that systemd uses for unit sandboxing.
   Fix: `void/apparmor/spaces-container`, a derived profile that allows those mounts. With it
   the guest reaches `running`, and `systemd-run -p PAMName=login --uid=1000` creates a real
   logind session. Residual: a `proc` mount under `/run/systemd` was denied once (rule added).
3. **Void does not load LXC container AppArmor profiles at boot.** Only the `lxc-*` tool
   profiles were loaded. The spaces runit service must load the profile itself with
   `apparmor_parser -r` before `lxc-start`. `apparmor_parser` warns
   `Found reference to variable PROC, but is never declared` for `lxc-containers`; the
   container profiles still load.
4. The guest's `/proc/self/cgroup` lists `1:name=elogind:/` for attached processes. This is
   the kernel listing the host's hierarchies; guest systemd only uses the mounted cgroup2.

## Open items for M2

- Re-run the guest boot with the added proc rule and confirm zero DENIED lines.
- Test `lxc-container-default-with-nesting` for development/admin spaces.
- Re-test live device updates with a real device policy (closed list) rather than `/dev/null`.

## M2: LXC launcher and runit (2026-10-04)

`void/spike/m2_check.py` boots the Ubuntu spike rootfs through `LxcBackend.run_launcher` under
sudo and checks the whole backend surface. Result: 32/32 checks pass.

Verified on this host:

- Ready marker is written once `probe_registered` and `probe_guest_shell` succeed; `systemctl
  is-system-running` reports `running`.
- `exec_in_guest` as root and as uid 1000 (env passed, exit codes propagated).
- `--bind-ro` host dir read-only, `--bind` writable, `/etc/resolv.conf` equal to the host's (a
  symlinked guest resolv.conf is replaced by a plain file first).
- Binds below `/run` work because the translator mounts a fresh tmpfs on `run` and `tmp` first;
  the `/proc/sys/net` staging pair collapses into one writable bind while the rest of
  `/proc/sys` stays read-only.
- `bind_into`/`unmount_in` through `spaces.host.mountns` (`open_tree`, `mount_setattr`,
  `setns`, `move_mount`), including a `/proc/PID/fd/N` source and a read-only bind (the
  `mount_setattr(MOUNT_ATTR_RDONLY)` path works).
- Live `set_device_policy`: a node outside the base rules (`c 10:237`) is denied, allowed after
  a policy update, denied again, allowed under `full`, denied again under a closed policy.
- `peer_in_space` is true for the guest init and false for host pid 1.
- SIGTERM to the launcher handle runs `lxc-stop -t 30`; `lxc-start` exits 0, `lxc-info` shows
  STOPPED, the marker is gone.
- runit: `ensure_service` for a throwaway name under the real `/etc/sv` and `/var/service`;
  `supervise/ok` appears, `sv status` is `down`, `forget_unit` removes everything.
- `lxc.cgroup.relative = 1` with base `/sys/fs/cgroup/spaces/NAME` boots fine and keeps the root
  `cgroup.subtree_control` empty (before, during and after). LXC leaves
  `spaces/NAME/pivot/lxc.pivot` behind; the launcher removes the tree on exit.
- No AppArmor `unix` denials appeared, so `lxc.apparmor.raw = unix,` was not added.

Unexpected:

- `systemd-run --working-directory=~` fails with status 200/CHDIR in the guest. The user command
  is now wrapped in `sh -c 'cd "$HOME"; exec "$@"'`.
- One AppArmor denial remained (remount of `/run/systemd/mount-rootfs/proc` with
  nosuid,nodev,noexec). A rule was added to `spaces-container`; zero DENIED lines since.
- `/dev/kmsg` is a poor probe for device policy: opening it needs CAP_SYSLOG, which the guest
  does not have, so `/dev/loop-control` is used.
- The base device rules always allow `c 1:3`, so `/dev/null` cannot be denied through
  `set_device_policy`; this matches nspawn's DevicePolicy=closed with the base allow list.
