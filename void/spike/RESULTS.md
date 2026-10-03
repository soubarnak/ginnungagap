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

- `systemd-run --working-directory=~` fails with status 200/CHDIR: under `lxc-attach --clear-env`
  there is no `$HOME`, so the client expands `~` to `/root`. `-p WorkingDirectory=~` is
  expanded by the guest manager against `--uid` and works.
- One AppArmor denial remained (remount of `/run/systemd/mount-rootfs/proc` with
  nosuid,nodev,noexec). A rule was added to `spaces-container`; zero DENIED lines since.
- `/dev/kmsg` is a poor probe for device policy: opening it needs CAP_SYSLOG, which the guest
  does not have, so `/dev/loop-control` is used.
- The base device rules always allow `c 1:3`, so `/dev/null` cannot be denied through
  `set_device_policy`; this matches nspawn's DevicePolicy=closed with the base allow list.

## M3: dev install, create and enter (2026-10-04)

`void/tools/dev-install.sh` installs the whole stack (layout in `INSTALLED.md`),
`void/tools/dev-uninstall.sh` reverts it. `void/spike/m3_check.py` (run as the normal user, uses
`sudo -n`) checks the goal end to end: 57/57 pass, including after an uninstall and reinstall.

Verified on this host (real runit service, real pkexec/polkit, real `LxcBackend`):

- `sudo spaces create ubuntu --preset basic` bootstraps Ubuntu 26.04 "resolute" (several minutes of
  debootstrap and apt), `spaces enter ubuntu -- id` runs as uid 1000 without a prompt, exit codes
  propagate, `spaces enter ubuntu` gives a login shell on a pty. A cold start from a stopped space
  (enter, then `sv once`, then the ready marker) takes about 3 seconds.
- polkit: `enter` and `start` are allowed without authentication, `create`, `configure`, `delete`,
  `cp` and `enter-as-user` need an administrator (`pkcheck` returns "challenge"). `wheel` is already
  an administrator through `/usr/share/polkit-1/rules.d/50-default.rules`, so no extra rule ships.
- Guest `sudo` authenticates against the HOST password through `pam_spaces.so`, the auth socket
  and `spaces-pam` (pam_unix via `/etc/pam.d/spaces`). Tested with a throwaway host user: the right
  password is accepted, a wrong one rejected.
- `launch._LoginMonitor` works against libelogind: uid 1000 reports state `active` and one session
  (type wayland, class user, active). Downloads is bound in the guest while the user has a session
  and disappears when it ends; tested with a second user's elogind session opened through PAM
  (`system-local-login`), launcher log shows "logged in; mounted" and "logged out; unmounted".
- runit: the run script passes `PATH` (shim first), `HOME=/root`, `LANG=C.UTF-8`; launcher output is
  in `/var/log/spaces/ubuntu/current`; the permanent `down` file keeps `runsv` from restarting the
  space; stopping leaves no mounts, no `/sys/fs/cgroup/spaces`, no `lxc-start`, and the root
  `cgroup.subtree_control` stays empty before, during and after.
- Native helper bundle: `build` and `check_guest_abi.py --glibc-max 2.17` pass with the Makefile's
  own flags (the installer unsets CFLAGS/LDFLAGS so `-fPIC` and `-z now` are not replaced).
  `auth.validate_native_runtime` passes and the helpers are bound at `/run/spaces-host/bin`.
- SELinux code in `priv.py`/`storage.py` is inert without `/sys/fs/selinux`.

Deviations and fixes:

1. `spaces create ubuntu --name ubuntu` is rejected by upstream (`--name` is for custom spaces only);
   the command is `sudo spaces create ubuntu --preset basic`. Unattended creation always picks the
   driver's default release, which is resolute (26.04), not noble.
2. `priv.create` ran `/usr/bin/systemctl stop spaces@NAME.service` directly. Void has no systemctl.
   It now calls `host.get_backend().stop_unit(name)` (same command on the systemd backend).
   `tests/test_priv.py` pins the systemd backend and a temporary `config.json`; before, it read the
   machine's real `/etc/spaces/config.json`, which a dev install now fills.
3. lxc-attach (LXC 6.0.3) hands regular-file stdio to the payload user (root) and drops group and
   other access, so `spaces enter ubuntu -- id > out.txt` left `out.txt` as root mode 0600
   `LxcBackend` snapshots owner and mode of regular-file
   stdio and restores them after the command. Pipes and terminals are unaffected.
4. `spawn_in_guest` (used with `--caller-pid`) killed only the lxc-attach client; the transient
   guest unit kept running. It now names the unit (`spaces-enter-<hex>`) and `terminate()`/`kill()`
   stop it first. Both the pkexec `--caller-pid` path and the plain pidfd path now end the command.
5. Guest resolute boots with systemd 259, which moves `/run/credentials/<unit>` mounts with
   `move_mount(2)`. AppArmor reports those without a source path, so
   `mount options=(rw, move) /** -> /**` never matched and journald, tmpfiles and sysusers failed
   (guest `degraded`). `spaces-container` gained `mount options=(rw, move) -> /**,`; the guest is
   `running` and there are no DENIED lines.
6. The runit run script exported only PATH; the service had no HOME or LANG. It now sets
   `HOME=/root LANG=C.UTF-8`.
7. `priv.enter` aborted with an error when waiting for the desktop session record timed out. It now
   prints a warning and continues without desktop forwarding, so terminal use never depends on it.
8. `chpasswd` on this host silently left new accounts locked; the check sets the throwaway password
   with `usermod -p` and an `openssl passwd -6` hash.

Security observations:

- `spaces.priv` is a root-owned shell wrapper with a fixed PATH that starts Python in isolated mode
  (`-I`: no PYTHON* variables, no cwd or user site on `sys.path`). All code root runs lives under
  `/usr`; nothing refers to the checkout.
- Surfaces exercised: pkexec with `org.anatase.spaces.{enter,start}` (allowed) and the
  administrator actions (checked with `pkcheck`, not executed through pkexec); `sudo` to
  `spaces.priv` (create, enter-as-user, configure through the Python API); the auth socket, which
  rejects peers whose cgroup is not inside the space (`peer_in_space`); five attempts per 30 s per
  uid.
- `spaces.priv enter --caller-pid` only accepts its own parent process.
- `/run/spaces/lxc/NAME` (LXC config, seccomp profile, ready marker) is mode 0700 root.
- lxc-attach chowns regular-file stdio of whoever starts it. The restore in `LxcBackend` runs after
  the command; if `spaces.priv` is killed first, a user's redirect target stays root-owned 0600.

Open items for M4 (desktop):

- The host open broker (`spaces-broker`) times out on every reconcile ("Could not enable host
  portals ... Timed out starting the host open broker"), twice every five seconds, filling the
  launcher log. `spawn_user_scope` starts it as the user without the session bus and runtime dir
  environment; needs the environment hook (`/run/user/UID/spaces/environment` written from the
  niri session) and a check of what the broker expects.
- Bus symlink: `session_bus_address` falls back to `/run/user/UID/bus`; on Void the session bus is
  an ephemeral address, so a login hook must publish it.
- Portals, secret helper, notifications and `xdg-dbus-proxy` are unexercised; the guest helpers
  link the host's glib (`libgio`) dynamically, which must be reconciled with the guest's glib.
- `spaces enter --graphical` for a GUI app on niri; shortcuts are already exported to
  `/usr/local/share/applications` (they point at `/usr/bin/spaces enter --graphical`).
- `autostart-users` is written but nothing consumes it yet (login autostart).
- NVIDIA mounts/overlays for `/etc/spaces/config.json` (M8) and the `ubuntu-keyring` srcpkg
  template (`void/srcpkgs/ubuntu-keyring/template`, untested with xbps-src).
