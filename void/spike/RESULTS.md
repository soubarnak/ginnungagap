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

Open items for M4 (desktop), all handled in the next section except where noted there:

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

## M4: desktop session integration (2026-10-04)

`void/spike/m4_check.py --with-apt` (run as the user from the niri session): 33 PASS, 0 FAIL, 0 SKIP.
Unit tests: 542 passed, 17 skipped.

Root causes and fixes:

1. Broker timeouts (twice every 5 s). `session._start_open_broker` hardcoded
   `unix:path=/run/user/UID/bus` and `xdg-dbus-proxy` used `session_bus_address`, which also fell
   back to it. On this host the session bus is `dbus-run-session`'s `/tmp/dbus-XXXX`, so both
   failed to connect. Fix: `LxcBackend.session_bus_address` resolves the real address from the
   session environment and creates the symlink `/run/user/UID/bus` -> socket (see below); the broker
   line in `session.py` now asks the backend for the address (one-line diff, systemd backend
   returns what it returned before). `xdg-dbus-proxy` accepts `unix:path=` through the symlink.
2. Session environment (`src/spaces/host/session_env.py`, new). The old fallback scan in `lxc.py`
   picked the lowest-pid process of the user with `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR`, which can
   belong to a different login session (tty, ssh) and has no `XDG_SESSION_ID`, which
   `select_graphical_session` needs. It is replaced by: published file first (accepted only if
   it is a regular user-owned file with no group/other write, in a user-owned non-writable-by-others
   directory, and its `XDG_SESSION_ID` is a currently active local graphical session of the uid),
   else a /proc scan restricted through libelogind (`sd_uid_get_sessions`, `sd_session_is_active`,
   `sd_session_is_remote`, `sd_session_get_class/type`, `sd_pid_get_session`) to processes of an
   active wayland/x11 session, which also supplies `XDG_SESSION_ID`. Only allowlisted variables are
   returned (`DESKTOP_ENVIRONMENT` plus bus address, runtime dir, config/data home, session id).
   The winner is cached and revalidated, so the 5 s reconcile does not rescan /proc.
3. `spawn_user_scope` already ran the proxy as the user but dropped supplementary groups, ran in the
   launcher's cwd and left the handle untracked. Now: user's real groups, `cwd=/`, stdin
   `/dev/null`, exact environment, handle kept per unit name; a new scope under the same unit stops
   its predecessor; `terminate_user_scopes()` runs when the launcher finishes. `xdg-dbus-proxy`
   also exits on its own when its control descriptor closes.
4. `kill_stale_helpers(name)` runs at launcher start. After `kill -9` of the launcher the proxy
   exits (control fd) but `spaces-broker` survives, holding the broker bus name of the dead
   session; the next launcher would time out again. (The M2 leftovers after such a kill, a running
   `lxc-start` and `/sys/fs/cgroup/spaces/NAME`, are unchanged and need manual cleanup.)

Bus symlink rules (`session_env.ensure_bus_link`): all operations through an `O_PATH|O_NOFOLLOW`
directory fd of `/run/user/UID` (must be owned by the uid); the target must be an absolute path to
a socket owned by the uid; an existing non-symlink is never touched; an existing symlink is only
replaced when it points elsewhere and is owned by the uid or root; replacement is atomic (temp link
+ `rename`); the link is `lchown`ed to the uid. Dangling links are removed when no session bus is
known. Abstract buses (`unix:abstract=`) cannot be linked: the real address is returned and used
for the proxy and the broker directly (the guest-facing path stays `/run/user/UID/bus`).

Verified (m4_check): publish writes 0600 user-owned; root reads it; without it the elogind scan finds
the same environment; a uid without a session gets nothing; symlink created, connects, repointed
when dangling; proxy and broker run as the user (one each) and the launcher log has no warning for
16 s after start; guest sees Wayland, PulseAudio (PipeWire 1.6.8 sinks), PipeWire and the filtered
bus; `gnome-calculator` from `spaces enter --graphical ubuntu -- gnome-calculator` appears in
`niri msg --json windows` as `org.gnome.Calculator` and closes; clipboard through the primary
selection (`wl-copy -p` in the guest, `wl-paste -p` on the host); `notify-send` in the guest reaches
`org.freedesktop.Notifications` on the host bus; `OpenURI.SchemeSupported` through the portal;
`spaces-open x-unhandled://probe` reached `org.freedesktop.portal.Desktop.OpenURI` on the host bus
(no browser started; the call then waits for the portal response, which never comes for an unhandled
scheme). The guest polkit KDE agent starts (`polkit_agent` finds
`/usr/lib/x86_64-linux-gnu/libexec/polkit-kde-authentication-agent-1`, no warning).

Guest helpers and GLib: the helpers link libglib/libgobject/libgio dynamically, glibc symbols are
<= 2.14, and GLib exports unversioned symbols. Host glib 2.88.3, guest 2.88.0: every undefined
`g_*`/`G*` symbol resolves in the guest's three libraries; `libgio-unix` is not a separate library
(gio-unix-2.0 is headers only), and no guest package had to be added (libglib2.0-0t64 comes in with
polkitd and xdg-desktop-portal). A host newer than the guest can still break startup with an
unresolved symbol (`-z now`); `void/tools/check-guest-glib.py [ROOTFS]` detects that and
`m4_check.py` runs it. Mitigations if it ever happens: build helpers in a container of the oldest
supported guest, or link GLib statically; not needed today.

Application menu: the launcher writes `/usr/local/share/applications/spaces-ubuntu-v1-*.desktop`
and `spaces-icons/*.png` (256x256 with the Ubuntu badge) within one reconcile of `apt install`, and
removes them after `apt remove` (checked with gnome-calculator). The session has no `XDG_DATA_DIRS`,
so the default `/usr/local/share:/usr/share` applies and `/usr/local/share/applications` is
scanned (GLib lookup finds the entry). If a launcher is started with a custom `XDG_DATA_DIRS`
without `/usr/local/share` the entries will not show; no Void-specific location is needed.
`Exec=/usr/bin/spaces enter --graphical ubuntu -- gnome-calculator` launches correctly.

Log noise: before, two `WARNING: Could not enable host portals ... Timed out starting the host open
broker` lines every 5 s (about 24 per minute, each reconcile blocking 1 s). After: none; the log
holds the mount line and the guest's `Running as unit: spaces-session-1000.service` per start.

Security observations:

- The portal proxy policy (upstream, unchanged) lets the guest: call the host portal interfaces
  Account, Access, Camera, Clipboard, Email, GlobalShortcuts, Inhibit, InputCapture, Location,
  NetworkMonitor, Notification, PowerProfileMonitor, Print, ProxyResolver, RemoteDesktop, ScreenCast,
  Settings, Usb, OpenURI.OpenURI/SchemeSupported, Screenshot.PickColor, Background.SetStatus
  (portal dialogs still ask the user); call `Notifications.Notify/CloseNotification/...`;
  `ScreenSaver.Lock/Inhibit/SimulateUserActivity`; `PowerManagement` queries; register a
  StatusNotifierItem and own `org.mpris.MediaPlayer2.spaces.*`. File access goes through the
  integration broker names, not the host portal. Guest apps can therefore lock the host screen and
  show host notifications without a prompt.
- Files and links: the published file and the bus symlink are owned by the user in the user's own
  runtime directory; root never follows a user-controlled path (`O_NOFOLLOW`, dir fds); the link target
  must be a socket the user owns. The user can only influence what is already theirs: the environment
  values still pass `_validated_source` (sockets owned by the uid below the runtime dir) in `session.py`.
- The scan only reads `/proc/PID/environ` of the uid's own processes in an active local graphical
  session, and returns only allowlisted names.
- `spaces enter --graphical` argument order: the flag must precede the space name
  (`spaces enter --graphical ubuntu -- cmd`); after the space name argparse treats it as the command.

Open items for M5 and later (M5 is done, see the next section):

- M5: device policy and hotplug. The guest sees `/dev/dri/card1` only (base rules); apps render in
  software; render nodes, the NVIDIA stack and `/dev/input` rules are not configured.
- Locales: the host `LANG=en_US.UTF-8` is forwarded but the guest has no such locale (perl and GTK
  warn); fixed by hand in this space (`locales`, `locale-gen`), the distro setup should generate it.
- Guest apps launched from the menu run with the same host-environment snapshot as the last
  reconcile; a changed `WAYLAND_DISPLAY` needs the next reconcile (5 s).
- Launcher `kill -9` leaves `lxc-start` and `/sys/fs/cgroup/spaces/NAME` behind (M2).
- `autostart-users` is still unused; `spaces-session-env publish` is optional until the user adds
  the niri line.
- Unhandled-scheme `OpenURI` blocks the guest caller until the portal answers; no timeout in the
  guest helper.
- Secrets: `org.freedesktop.impl.portal.Secret` is only bus-activatable on this host (gnome-keyring is
  running); guest secret flows (M6) are untested.

## M5: devices, GPU and hotplug (2026-10-04)

`void/spike/m5_check.py` (run as the user from the niri session): 43 PASS, 0 FAIL, 0 SKIP.
Unit tests: 572 passed, 17 skipped. Host: RTX 2050 (card0, renderD128, nvidia driver, headless)
and Radeon 680M (card1, renderD129, amdgpu, drives eDP-1 and niri); NVIDIA 595.104.02.

Discovery and tagging (item 1). The M4 note "the guest sees only card1" does not reproduce:
`devices.discover("basic")` returns card0, card1, renderD128, renderD129 on this host and the guest
sees all four. Why it works without any udev rule: eudev ships `70-uaccess.rules`
(`SUBSYSTEM=="drm", KERNEL=="card*", TAG+="uaccess"`) and elogind applies the ACL, so
`getfacl /dev/dri/card0` shows `user:soubarna:rw-` on a `root:video 0660` node; render nodes are
`0666`, and `discover` also accepts the `drm` subsystem and the host `video` gid. No udev rule or
code change was needed for the GPU nodes. Access in the guest rests on the ACL (uid 1000 equals
the host uid) and on `0666`, not on groups: host `video` is gid 13, which is `proxy` in the Ubuntu
guest (`video` is 44, `render` 990). Group-only nodes without ACL stay unreachable for the guest
user (`/dev/fb0` EACCES, `/dev/snd/hwC*` EACCES); upstream has the same limit, and the GPU, camera,
audio, controller nodes all carry an ACL or 0666, so no `_reconcile_accounts` change was made.
Also visible at `basic` (upstream rules, unchanged): `/dev/kfd`, `/dev/media0`, `/dev/fb0`,
`/dev/drm_dp_aux*`, `/dev/rfkill`, `/dev/snd/*`, `/dev/video*`. The `drm_dp_aux*` nodes are root 0600
and cannot be opened by the guest user (root in the guest can).

NVIDIA nodes (item 2). `/dev/nvidia0`, `nvidiactl`, `nvidia-uvm`, `nvidia-uvm-tools`,
`nvidia-modeset` have no sysfs device (nvidia-modprobe creates them with mknod), so udev cannot tag
them and a udev rule cannot help. `devices.Udev.metadata` now falls back, only when
`udev_device_new_from_devnum` finds nothing, to the `/proc/devices` name of the character major
(`nvidia`, `nvidiactl`, `nvidia-uvm`, `nvidia-modeset`) and reports the pseudo subsystem `nvidia`,
which is added to `VIDEO_SUBSYSTEMS`. Sysfs-backed devices never reach that path, so systemd hosts
behave as before. MIG caps (`nvidia-caps`), NVSwitch, NVLink and IMEX nodes stay out of `basic`.
`nvidia-modeset` is included on purpose: without it `vkCreateDevice` segfaults inside
`libnvidia-glcore` (it opens the node, `mknod` fails with EACCES, the error path crashes; seen as
`vulkaninfo` SIGSEGV, backtrace in `/tmp/ggm5/gdb_vk.txt`). A permanent alternative would be
`options nvidia NVreg_DeviceFileGID=... NVreg_DeviceFileMode=...`, but it needs a module reload or
reboot (`/proc/driver/nvidia/params` shows mode 438 = 0666, uid/gid 0), and a group-restricted mode
would break the guest: the nodes have no uaccess ACL and the host `video` gid is not the guest's.
Keep 0666; nothing was changed in the driver or modprobe.

NVIDIA userspace (item 3). `spaces-nvidia-sync` (`/usr/lib/spaces/spaces-nvidia-sync`, module
`spaces.host.nvidia`) runs from the runit `run` script before `spaces.priv launch` (tolerant:
`[ -x ... ] && ... || true`, 0.4 s) and by hand. From the xbps file lists of `nvidia-libs`,
`nvidia-libs-32bit`, `egl-wayland2`, `nvidia` (and `nvidia-opencl*`, absent on this driver series)
it creates symlinks to the final files under `/var/lib/spaces/.host/nvidia/<pkgver>/{lib,lib32,share,bin,opencl}`
(`current` points at the live version, other versions are removed): 96 entries (64-bit and 32-bit
vendor libraries including soname aliases, `lib/gbm/nvidia-drm_gbm.so`, `lib/vdpau/`, the glvnd EGL
vendor file, the three EGL external platform files, the Vulkan ICD and implicit layer, `nvoptix.bin`,
`nvidia-smi`, `nvidia-debugdump`, `nvidia-cuda-mps-*`, `nvidia-ngx-updater`). Not exposed: Xorg
and wine modules, `libnvidia-gtk*`, `nvidia-settings`, `libGLX_indirect` (owned by the guest's
libglvnd), unversioned `.so` dev links, anything that does not resolve below `/usr`. It then writes
`/etc/spaces/config.json` from `/usr/share/spaces/config.base.json` (packages per distro), the
optional extras in `/etc/spaces/void.json` and per-distro NVIDIA overlays/mounts: arch
`/usr/lib` + `/usr/lib32`, fedora `/usr/lib64` + `/usr/lib`, ubuntu and kali
`/usr/lib/x86_64-linux-gnu` + `/usr/lib/i386-linux-gnu`, plus `/usr/share` and the binaries. The
result is validated with `host_config.load` (any warning aborts the write). A `sha256` sidecar
`/etc/spaces/config.json.generated` marks the file as generated; a file whose hash does not match
(hand edit) is never replaced, the new text goes to `config.json.new` and a warning is printed
(the M3 install file is recognised and adopted). Upstream's overlay walker handles the farm: a
symlinked file source passes `exists()`/`os.stat` and the kernel binds the target, a dangling one is
skipped silently, an existing symlink in the guest (here libglvnd's `libGLX_indirect.so.0`) is left
alone with a warning. Each alias becomes its own bind onto an empty placeholder file that the
launcher precreates in the guest rootfs and leaves there. If the farm is later removed, those empty
files stay in the guest `/usr/lib/...` until deleted by hand.

glibc and symbols. Host glibc 2.41, guest glibc 2.43 (Ubuntu 26.04). The highest `GLIBC_` symbol
version required by any exposed file is 2.38 (`libnvidia-egl-wayland2`, built by Void); the NVIDIA
blobs need at most 2.17 and `nvidia-smi` 2.7. They need `libX11`, `libXext`, `libdrm`, `libgbm`,
`libwayland-client/server` and `libgcc_s` from the guest (present with Mesa). Nothing of the host's
libc, libstdc++ or system libraries is exposed. A guest with glibc older than 2.38 (Debian 12,
Ubuntu 22.04) could not load `libnvidia-egl-wayland2` (EGL then skips that platform); everything
else needs 2.17. `/tmp/ggm5/nvidia_abi.txt` has the full list. `nvidia_layers.json` names
`libnvidia-present`, which Void does not ship (opt-in layer, harmless).

Acceleration in the guest (item 4), packages installed in the guest: `mesa-utils`,
`mesa-utils-bin`, `vulkan-tools`, `mesa-vulkan-drivers`, `libgl1-mesa-dri`, `libegl1`, `libgbm1`,
`libvulkan1` (kept), `gdb`, `strace` (debugging; kept).

- `nvidia-smi` lists GPU 0 NVIDIA GeForce RTX 2050, driver 595.104.02, CUDA 13.2.
- `vulkaninfo --summary`: radv AMD Radeon 660M (Mesa 26.0.8), NVIDIA RTX 2050 (595.104.02,
  Vulkan 1.4.329), llvmpipe.
- EGL through GBM on `/dev/dri/renderD129` is Mesa radeonsi (not llvmpipe); on `renderD128` it is the
  NVIDIA GBM backend and EGL, GL 4.6.0 NVIDIA 595.104.02. eglinfo's own default GBM probe opens the
  first render node (NVIDIA) with Mesa and falls back to llvmpipe, which is eglinfo's choice of
  device, not a space problem; its Wayland, X11 and surfaceless platforms report radeonsi.
- `glxinfo -B` (XWayland `DISPLAY=:0`): direct rendering, AMD radeonsi; with
  `__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia` NVIDIA; `DRI_PRIME=1` selects zink on
  NVIDIA. `glxgears` runs at the 144 Hz refresh rate on both.
- `vkcube --gpu_number 0` (AMD) and `--gpu_number 1` (NVIDIA, presenting on the AMD-driven niri
  session) both create a window (`niri msg --json windows`, title `vkcube`, app id unset) that
  disappears when the program exits.

Blocked stays blocked (item 5). At `basic`, in the guest as root and as uid 1000: `/dev/mem`,
`kmsg`, `tpm*`, `tty0`, `vcs`, `nvme*`, `sd*`, `loop-control`, `hidraw*`, `uinput`, `input/event*`,
`bus/usb`, `kvm`, `cpu_dma_latency` do not exist (ENOENT), no block device exists, and `mknod` of
`259:0`, `8:0`, tpm `10:224`, tty0 `4:0`, kmsg `1:11` (root only, on a filesystem without `nodev`,
`/var/tmp`) succeeds but `open` fails with EPERM from the device cgroup. (`/tmp` is a `nodev` tmpfs,
which would give EACCES for any node and prove nothing.) Levels, via `priv.configure` with
`preset: custom` (the effective level comes from the preset unless it is `custom`; `spaces
configure` is a TUI):
`admin` adds raw disks (`nvme0n1` opens read-write for guest root), loop, hidraw, uinput, input
event nodes, USB, kvm, gpio, rtc; TPM, tty, watchdog, kmsg stay absent and a mknod of them is still
refused. `full` removes the device cgroup restriction: mknod'd `tpm0`, `tty0`, disk nodes then open;
`/dev/mem` and `/dev/port` keep EPERM only because CAP_SYS_RAWIO is dropped. Incident: opening a
mknod'd `10:130` at `full` armed the host SP5100 TCO watchdog ("watchdog did not stop", 60 s
timeout, `nowayout=0`); it was disarmed by writing `V` to `/dev/watchdog` within seconds and the
check never opens watchdog nodes. `discover` filters watchdogs at every level, but `full` lets the
guest create them itself. `basic` was restored (`info.json` equals the original).

Hotplug (item 6). `UdevMonitor` works against eudev (netlink group `udev`). A uinput gamepad
(`ID_INPUT_JOYSTICK`, `uaccess` from `70-uaccess.rules`, ACL from elogind) appears in the guest as
`/dev/input/event22` and `js0` 0.3-0.4 s after creation, uid 1000 opens it, and the device cgroup
rule is live (a mknod of the same major:minor opens in the guest while the device exists and gets
EPERM after removal). `bind_into` creates the missing `/dev/input` in the guest's tmpfs. A virtual
keyboard (`ID_INPUT_KEYBOARD`) stays hidden at `basic`. Bug found and fixed: after removal the lazy
unmount left an empty regular file `/dev/input/event22` in the guest (upstream behaviour);
`mountns.unbind` now removes the empty placeholder and parent directories under `/dev` that became
empty, so removal is clean (0.2-0.3 s) and `/dev/input` is gone again.

M4 leftovers (item 7). (a) Locale: `LxcBackend._attach` runs the environment through
`host.guest_locale.adjust`, which reads the guest's `locale-archive` (name table parsed directly,
cached by mtime) and `usr/lib/locale/*` and replaces an unavailable `LANG` by `C.UTF-8`, dropping
unavailable `LC_*`/`LC_ALL` and then `LANGUAGE`; C/POSIX/C.* are always accepted; unreadable
archives change nothing. Real test: with the archive moved aside `LANG` in the guest is `C.UTF-8`
and perl is silent; restored it is `en_US.UTF-8`. Note: the forwarded locale comes from the desktop
session, not from the shell that runs `spaces enter`. (b) Stale container: `run_launcher` takes a
per-space `flock` (`/run/spaces/lxc/NAME/launcher.lock`, released when the process dies, not
inherited by `lxc-start`); having it proves no live launcher exists, so a container still `RUNNING`
and `/sys/fs/cgroup/spaces/NAME` are orphans and are stopped with `lxc-stop -k` and removed. Also
`is_running()` now requires the lock to be held: before this, `spaces enter` after a launcher
`kill -9` found `RUNNING`, skipped `sv once` and attached to a headless container with no mount,
device, auth or portal workers. Real test: `kill -9` of the launcher leaves `lxc-start`; the next
`spaces enter` logs "Stopping the leftover container", starts a new launcher and container (4 s)
and runs the command. A second launcher for the same name is refused. (c) Portal filter review:
`void/docs/portal-filter-review.md` (documentation only).

Dead ends and surprises: the 15 s removal wait first blamed on the mount was the leftover placeholder file, not the
mount; a first stale test killed `svlogd` because `sv status` prints two pids.

Open items for M6 (bootstrap Arch, Fedora, Kali):

- The farm destinations for Arch (`/usr/lib`, `/usr/lib32`), Fedora (`/usr/lib64`, `/usr/lib`) and
  Kali are generated and unit-tested but unexercised; Arch's `/usr/lib` overlay will bind every farm
  entry over a directory with thousands of files.
- Guests that install their own NVIDIA packages meet the empty placeholder files.
- `kali`/`arch` guest glibc versus the 2.38 requirement of `libnvidia-egl-wayland2`.
- A `devices` level of `disabled` does not stop `portal.Usb`/`Camera` (see the review).
- If `xbps-query` fails transiently (for example `xbps-install -u` holds the lock while a space
  starts), `spaces-nvidia-sync` removes the farm and regenerates `config.json` without NVIDIA, so
  that launch has no GPU userspace; the next launch restores it. Binds of an already running space
  are unaffected (they point at the real `/usr/lib` inodes). The uninstall, install, enter cycle
  was re-run after the last installer change and the log service came up.

## M6: Arch, Kali and Fedora spaces (2026-10-04)

`void/spike/m6_check.py` (as the user, from the niri session; `--watchdog` adds the full-level test):
85 PASS, 0 FAIL, 6 SKIP (the SKIPs are "space already exists" and "extras already installed").
Unit tests: 582 passed, 17 skipped. Created with `sudo spaces create D --preset basic`; the three
spaces stay installed.

| Space | Create | State + cache | Guest glibc | Notes |
|---|---|---|---|---|
| kali (kali-rolling, kali-linux-default) | 49.5 min (about 3000 packages, network bound) | 14 G + 3.4 G | 2.43 | debootstrap already had `kali-rolling`; no shim |
| arch (rolling, defaults `rankmirrors`, `yay`) | 6.2 min | 3.5 G + 1.1 G | 2.44 | yay built from the AUR in the guest chroot works |
| fedora 44 | 6.3 min (a first attempt failed on mirror downloads, retry fine) | 1.5 G + 104 M | 2.43 | bootstrap image cache 260 M in `/var/lib/spaces/.host/fedora/44` |

Per distro (all PASS): boots to `running`, `spaces enter` as uid 1000 without launcher warnings, host-PAM
sudo bridge (right password accepted, wrong one rejected, throwaway user and guest files restored),
`gnome-calculator` shows a window in `niri msg --json windows` (app id `org.gnome.Calculator`) and
closes, `nvidia-smi` lists the RTX 2050, NVIDIA libraries and ICD files in the distro's farm
destination (`/usr/lib/x86_64-linux-gnu`, `/usr/lib`, `/usr/lib64`) resolve all dependencies (`ldd`),
`vulkaninfo` lists radv and NVIDIA, EGL on renderD129 (radeonsi) and renderD128 (NVIDIA) renders in
hardware, `glxinfo` is direct and not llvmpipe, PRIME offload selects NVIDIA, stop leaves no mounts,
cgroup or container. Guest glibc 2.43 and 2.44 are above the 2.38 that `libnvidia-egl-wayland2` needs.

### Item 0: the `full` device level no longer reaches watchdogs

At `full` the LXC policy was "no rules", so guest root could mknod and open 10:130 and arm the host
watchdog. `devices_lxc.config_text(None)` now writes `lxc.cgroup2.devices.allow = a` followed by
`deny` rules for `c 4:*` (tty, VT, serial), `c 7:*` (vcs), `c 5:3` (ttyprintk), `c 10:130`
(`/dev/watchdog`), every device listed in `/sys/class/watchdog/*/dev` and every major `/proc/devices`
names `watchdog` (here 246). Major 5 stays open for `/dev/tty`, `/dev/console` and `/dev/ptmx` (the base
rules). The same rules are applied by `lxc-cgroup` after `allow a` when a running space is switched
to `full`. Evidence (guest root, `m6_check.py --watchdog` and the earlier manual runs): at `full`, boot
and live, `mknod` of 10:130, 246:0, 246:1, 4:0, 4:64, 7:0 and 5:3 fails with EPERM (the deny rule
includes `m`), `/dev/kmsg` still opens; at `admin` the nodes can be made but `open` is EPERM; the
sysfs `state` of watchdog0 was read every 0.1 s by a guard (never an open of the node on the host) and
stayed `inactive`; `basic` was restored and `info.json` says so. Nothing was ever armed.
Unit tests: `test_full_level_keeps_watchdog_and_console_denied`,
`test_live_update_to_full_denies_after_allow_all`. A watchdog that appears after the policy was
written and has a new major is only covered at the next policy write (start or `configure`).

### Kali

`/usr/share/debootstrap/scripts/kali-rolling` ships with Void's debootstrap 1.0.145, so
`kali.py` runs unchanged, with the in-tree key (`kali-archive-key.gpg.base64`) as `--keyring`.
`apt` prints `Can not write log (Is /dev/pts mounted?)` during the chroot phase (upstream behaviour,
harmless).

### Arch

Host side (`void/tools/dev-install-arch.sh`, called by `dev-install.sh`, `--skip-distro-tools` skips it):

- xbps `pacman` 7.1 (has no repositories in `/etc/pacman.conf`, owned by the `pacman` package) and `m4`.
  `/etc/pacman.d/mirrorlist` is owned by no Void package; the install writes a default one (geo.mirror.pkgbuild.com,
  rackspace, kernel.org) only when it is absent, records `arch_mirrorlist=installed`, and the
  uninstaller removes it. `rankmirrors` (`arch.py`) rewrites it with five ranked mirrors ("# Ranked by
  Spaces"); pacstrap copies it into the guest.
- arch-install-scripts v31 in `/usr/lib/spaces/void/arch-install-scripts/bin` (built with `make arch-chroot genfstab
  pacstrap`, no man pages); `/usr/lib/spaces/void/bin/{pacstrap,arch-chroot}` are the shims (first on
  `spaces.priv`'s PATH). The pacstrap shim adds `-C /usr/share/spaces/void/arch-pacman.conf`: core and extra,
  `Include = /etc/pacman.d/mirrorlist`, `SigLevel = Required DatabaseOptional`, `GPGDir =
  /var/lib/spaces/.host/arch/gnupg` (root 0700, 183 keys, initialised once with `pacman-key --init` and
  `--populate-from /usr/share/spaces/void/archlinux-keyring --populate archlinux`).
- pacstrap passes `--disable-sandbox`, so pacman 7's landlock/DownloadUser sandbox is not an issue as root on
  this kernel; arch-chroot needs only `unshare` and `mount`, no systemd. Packages and the guest are verified
  by pacman (checking keyring, package integrity).
- `/usr/lib/spaces/rankmirrors` from pacman-contrib commit 75d4a705 (as `spaces.spec` does), `sed`-expanded.
- Templates `void/srcpkgs/arch-install-scripts/template` and `void/srcpkgs/archlinux-keyring/template`
  (not built with xbps-src). A future Void package of the same name would install into `/usr/bin` without
  colliding; the shim would then be replaced by a plain `-C` wrapper.

Bug found and fixed: `native/spaces.c` waited for polkit-kde to print "Authentication agent result: true"
on stderr and printed "polkit agent did not become ready" after 2 s on every `spaces enter` in Arch (and Fedora).
Qt built with journald support logs to the journal unless stderr is a console, and the agent's stderr is a pipe
(the message was in `journalctl`; Debian and Ubuntu builds log to stderr). `execute_agent` now also sets `QT_FORCE_STDERR_LOGGING=1`.

AUR/yay works in the guest (`makepkg` in the chroot). `pacman -S nvidia-utils` in the guest fails with
"exists in filesystem" for every placeholder (see below): the guest does not need its own NVIDIA userspace.

### Fedora

Fedora has no host `dnf5`. `void/bin/dnf5` (python, installed to `/usr/lib/spaces/void/bin/dnf5`) keeps the exact
command line of `fedora.py` and runs the real dnf5 in a bootstrap root:

1. downloads `Fedora-Container-44-1.7-x86_64-CHECKSUM` and `Fedora-Container-Base-Generic-44-1.7.x86_64.oci.tar.xz`
   from `dl.fedoraproject.org/pub/fedora/linux/releases/44/Container/x86_64/images/`, verifies the clearsigned CHECKSUM with
   `gpgv` against `/usr/share/spaces/keys/RPM-GPG-KEY-fedora-44-primary` (the VALIDSIG fingerprint must be that key,
   `36F612DC...F90A6`) and the image sha256 against it (`75200f57...3974d1b`);
2. unpacks the OCI layer (blob digest checked, GNU tar with `--numeric-owner --xattrs`, whiteouts applied) to
   `/var/lib/spaces/.host/fedora/44/root`, atomically (`root.tmp`, then rename; flock);
3. `unshare --mount` and chroot into it with /proc, a tmpfs /dev with six bound device nodes, /sys read-only,
   resolv.conf, `/usr/share/spaces` (read-only: repos and keys) and the installation root `--rbind`-mounted at the same path
   (so the API filesystems `fedora.py` mounted in it are visible). The mounts exist only in that private
   namespace; nothing is left to clean up. RPM checks stay on (`gpgcheck=1`, key from `fedora.repo`).

The rpm database lands in `ROOT/usr/lib/sysimage/rpm` (sqlite) as on a Fedora host and `chroot ROOT authselect`
works, so no change to `fedora.py` was needed. Cold start of the shim: 39 s (download + unpack). 8 unit tests
(`tests/test_void_dnf5_shim.py`: arguments, a throwaway key for signature/tampering/wrong signer, digests, whiteouts, manifest).
Bug found and fixed in `lxc.py`: runit gives the service `/dev/console` as stdin, so `lxc-attach` ran every launcher
command with a tty and the Fedora guest's short `install -d` exited 129 (SIGHUP, about 70 % of the time; reproduced with `script`),
which made "Could not enable host session forwarding" repeat every five seconds and left GUI apps without a
display. The run script now starts with `exec </dev/null`.

### Trust: verified against trust on first use

| What | Verified | Trust on first use |
|---|---|---|
| arch-install-scripts v31 archive (sha256 `ef22eae9...a988e`) | identical (`diff -r`) to `git archive` of tag v31; the tag verifies with `C100346676634E80C940FB9E9C02FF419FECBE16` (Morten Linderud), the only `validpgpkeys` entry of Arch's PKGBUILD | the key itself came from keys.openpgp.org and is matched only to Arch's PKGBUILD (same project, gitlab); the installer pins the archive sha256, not the signature |
| archlinux-keyring 20260909 (sha256 `935ad345...5528a`) | the release's inline signature verifies with `02FD1C7A934E614545849F19A6234074498E9CEE` (Christian Hesse, fetched over WKD, listed in the keyring PKGBUILD); the five master keys in `archlinux-trusted` all appear on archlinux.org/master-keys (a different host than gitlab; the installer re-checks this and fails on a mismatch, warns when the page is unreachable) | the signer key, and that page, are not anchored in anything already on the machine |
| rankmirrors (commit 75d4a705, sha256 `b67902d2...cc27`) | pinned hash only | whole file: a bash script, GPL-3.0, from gitlab |
| Fedora 44 base image | CHECKSUM signature (gpgv) against the in-tree key, whose fingerprint also appears on fedoraproject.org/security; sha256 of the image from CHECKSUM; layer digests | nothing besides the in-tree key (upstream data) |
| Kali | debootstrap `--force-check-gpg` with the in-tree key, Release signature valid (`827C8569...E4C5`) | the in-tree key |
| Arch packages, Fedora RPMs | pacman `Required DatabaseOptional` (core.db.sig is not published on the chosen mirror), `gpgcheck=1` | |

Pinned sources go stale: a newer keyring or arch-install-scripts needs new hashes in `dev-install-arch.sh` and the templates.

### NVIDIA farm destinations (M5 open items)

Exercised for real now: arch `/usr/lib` + `/usr/lib32` (the launcher creates placeholders in `/usr/lib32`
although Arch has no multilib), fedora `/usr/lib64` + `/usr/lib` (26 and 40 placeholder files), kali
`/usr/lib/x86_64-linux-gnu` (`/usr/lib/i386-linux-gnu` is absent and skipped). The overlay of Arch's `/usr/lib` works;
no `nvidia.py` change was needed. Guest-installed NVIDIA packages collide with the placeholders ("exists in
filesystem", pacman refuses; apt and dnf would too): expected, nothing to fix. If the farm goes away the
empty placeholder files stay (unchanged M5 behaviour). Guest glibc: kali 2.43, arch 2.44, fedora 2.43.

### Open items for M7 (done in M7, see below)

- Autostart (`autostart-users` is written, nothing starts spaces at login), entry wrappers or aliases
  (`spaces enter arch` etc.), shortcut export for the new spaces, default desktop flavour: the packages are KDE
  libraries and the agent only; there is no panel or menu.
- `dev-uninstall` deletes `/var/lib/spaces/.host` (Arch keyring, 260 MB Fedora bootstrap, NVIDIA farm); they come back on the next
  install or first `dnf5` use. It also forgets the xbps packages it installed (state file removed); pacman and m4 were
  re-added to the state by hand after the uninstall/install cycle.
- Mirrors: the Fedora metalink handed out stale or unreachable mirrors once (retry passed); `rankmirrors` is only for Arch.
  A default `/etc/dnf/libdnf5.conf.d` in the bootstrap root with retries and `fastestmirror` could be added.
- A future Void `arch-install-scripts`, `archlinux-keyring` or `dnf5` package would let the shims call `/usr/bin` directly.

## M7: entry commands, autostart, desktop flavour, housekeeping (2026-10-04)

`void/spike/m7_check.py` (`--reinstall` also runs an uninstall and a reinstall) covers the items below;
final run: see "Check results" at the end of this section. New files: `src/spaces/host/{autostart,cli,doctor,flavor}.py`,
`void/runit/spaces-autostart/`, `void/shell/spaces.{sh,fish}`, `void/entry/enter-space`, `void/docs/void.md`,
tests `tests/test_void_{autostart,flavor,cli}.py`. Changed: `nvidia.py` (flavour packages, `desktop_flavor`,
Arch multilib probe), `priv.py` (refresh of the generated config before `create`), `void/bin/dnf5` (tuning),
`dev-install.sh`, `dev-uninstall.sh`, `config.base.json`.

### Entry commands

- `/usr/bin/{ubuntu,fedora,kali,arch-linux}` are copies of the anatase `enter-space` wrapper plus one line that
  drops a leading `--`: upstream's wrapper turns `ubuntu -- id` into `spaces enter ubuntu -- -- id`, which tries to run
  a program called `--` ("could not start application"). `ubuntu id` and `ubuntu -- id` now both work.
- Name collisions: none of the four names is on `PATH` (`command -v`), none of the files is owned by an installed
  package (`xbps-query -o`), and no package is named like them (`xbps-query -Rs`). The per-file remote query
  `xbps-query -Ro /usr/bin/NAME` downloads every package of the repository (it ran for 2 minutes and was at the
  letter R), so the file-level check against packages that are not installed was not completed.
  `/usr/bin/arch` is coreutils' and is never installed over.
- Shell snippet: `/usr/share/spaces/void/shell/spaces.sh` defines the functions `arch ubuntu fedora kali` and, only
  when the host lacks the command, hint functions for `apt apt-get dnf` (return 127). `pacman` and `xbps-*` are not touched.
  Not enabled anywhere. Tested in bash (sourced in a subshell: `ubuntu -- id`, `arch -- ...`, hints, no-hints switch).
  zsh (same file, plain POSIX functions) and fish (`spaces.fish`) are not tested: neither is installed here.

### Autostart

- `LxcBackend.enable_user_autostart` only appends the user name to `/var/lib/spaces/NAME/autostart-users` (one name per
  line, root-owned 0644). `spaces create` and `spaces configure` call it by default (`--no-enable` skips), so on this host
  all four spaces had `soubarna` enabled since M3/M6, and nothing consumed it. That is unchanged: enabling still only writes
  the file, and nothing starts until `/var/service/spaces-autostart` exists.
- `spaces-autostart` (runit service, `/etc/sv/spaces-autostart`, run script `python3 -I -m spaces.host.autostart`, log in
  `/var/log/spaces/autostart`) uses its own ctypes wrapper of `sd_login_monitor_*` and `sd_uid_get_state` in
  libelogind (no import of `launch`, textual or PIL; a test asserts that). Per evaluation (start, every login-state event, every
  30 s): boot flag (`autostart-boot`) once per boot; for each user in `autostart-users` with state active, online or lingering,
  `sv once` of each space that is not running, once per login. A login is a session id the daemon has not acted on (or the start
  of lingering); the record is `/run/spaces/autostart/state.json` (tmpfs): a daemon restart does not repeat, a reboot starts
  over, logging out forgets the user.
- Real test (no logout possible): service linked with only `ubuntu` enabled for soubarna and the others disabled; `ubuntu` was
  ready 4 s after the link; the three other spaces stayed down; `sudo sv down` was not undone over a 36 s wait (more than one 30 s
  evaluation) nor by `sv restart spaces-autostart`; then the service was stopped, unlinked and the state files restored.
  Not tested for real: a second login while the daemon runs (the PAM stack could not open an elogind session from a root script);
  covered by unit tests with a fake elogind.

### Desktop flavour

- Package names checked in the live spaces (`apt-cache policy`, `pacman -Si`, `dnf5 repoquery`): Ubuntu 26.04 and Kali:
  `adwaita-icon-theme gnome-themes-extra xdg-desktop-portal-gtk gsettings-desktop-schemas dconf-gsettings-backend
  qt5-gtk-platformtheme qt6-gtk-platformtheme`; Arch: `adw-gtk-theme gnome-themes-extra adwaita-icon-theme
  xdg-desktop-portal-gtk gsettings-desktop-schemas dconf gtk3` (there is no `qt6-gtk-platformtheme`; `libqgtk3.so` is part of
  `qt6-base` and needs gtk3); Fedora 44: `adw-gtk3-theme adwaita-icon-theme xdg-desktop-portal-gtk gsettings-desktop-schemas dconf
  gtk3` (no `gnome-themes-extra`; `libqgtk3.so` is in `qt6-qtbase-gui`). `adw-gtk3` is not packaged for Ubuntu or Kali.
- `desktop_flavor` (`auto|kde|gtk`) lives in `config.base.json` (auto) and may be overridden in `/etc/spaces/void.json`; the generator
  (`nvidia.generate`) adds the packages after the base list, before the NVIDIA and void.json parts. `auto` reads
  `XDG_CURRENT_DESKTOP` through `session_env.resolve_environment` for the calling uid (`SUDO_UID`/`PKEXEC_UID`), else any user
  with an active graphical session; KDE/Plasma gives kde, anything else gtk; the last answer is kept in
  `/var/lib/spaces/.host/desktop-flavor` so that a regeneration without a session does not flip it. `priv.create` regenerates
  the config for the creating user (as root only, failures are not fatal), so `spaces create` sees the right flavour.
- `spaces-void install-flavor NAME` ran for real on ubuntu, arch and fedora (idempotent, 2 to 6 s once metadata is fresh).
  polkit-kde-agent and the KDE portal stay in place. GTK app: `gnome-calculator` from ubuntu and from arch opens a window on niri
  after the install. Qt: `QT_QPA_PLATFORMTHEME=gtk3` was forwarded all along; before the install Qt silently fell back
  (`Attempting to create platform theme "gtk3"` with no success line), after it the log says `Successfully created platform
  theme "gtk3"` (python3-pyqt6 was installed in ubuntu to test; kept).

### Housekeeping

- State file: the loss of `pam-devel` came from the uninstaller, which kept the packages but deleted the state file that recorded
  them, so the next install found them present and did not record them again. The record now also lives in
  `/var/lib/spaces/.host/dev-install.packages` (kept by a plain uninstall, merged on install, removed by `--remove-packages`).
  Current state: `packages=m4 pacman pam-devel`, which agrees with `xbps-query -m` and INSTALLED.md.
- `dev-uninstall.sh` keeps `/var/lib/spaces/.host` unless `--purge`.
- dnf5 shim: `--setopt` retries=5, timeout=30, max_parallel_downloads=8, fastestmirror=True are added to every bootstrap dnf5 call
  unless the caller sets them (dnf5 reads the installation root's config, not the bootstrap root's, so the command line is the
  place). `dnf5 --version` and `dnf5 makecache --releasever=44` ran with them; dnf5 itself rejects a bad value, which shows the
  names are real options. The fedora space was not recreated.
- Arch `/usr/lib32`: the NVIDIA lib32 overlay for arch is only generated when the guest's `/etc/pacman.conf` has `[multilib]`
  (the generator reads `/var/lib/spaces/arch/rootfs/etc/pacman.conf` at every start). Placeholder files already created by M6 stay.
- `spaces-void gc` and `doctor` (see `void/docs/void.md`).

### Check results

`python3 void/spike/m7_check.py --reinstall`: 77 PASS, 0 FAIL, 0 SKIP (entry commands 16, shell snippet 6, autostart 15, flavour
17 including install-flavor on ubuntu, arch and fedora, the GTK window and the Qt theme, housekeeping 12 including the
uninstall/reinstall cycle, doctor and gc 5, clean machine 6). Unit tests: 642 passed, 17 skipped (after a robustness change to the autostart login record, see below). A first full run found one
bug in the check itself (the state-file restore wrote a literal `\n`; repaired from a backup, the check now restores through
`tee`) and two ordering problems after the reinstall, which removes `/etc/sv/spaces-*` until the next start of each space
(the check now runs doctor and gc before the reinstall and treats a missing service as stopped).
Machine at the end: all spaces stopped, `spaces-autostart` unlinked and its daemon gone, `autostart-users` as before (all four
spaces for soubarna), no space mounts or cgroups, `cgroup.subtree_control` empty, watchdog inactive. Left behind on purpose:
`python3-pyqt6` and the GTK flavour packages in ubuntu, the flavour packages in arch and fedora, `/var/lib/spaces/.host/{dev-install.packages,desktop-flavor}`.

### Open items for M8 (xbps-src packaging)

- Templates: `spaces` (python3 module + native build with `make -C native`, glibc/x86_64 only for the guest helpers: `archs=x86_64`,
  `hostmakedepends="gcc make pkg-config"`, `makedepends="glib-devel pam-devel"`), depends on `lxc apparmor polkit elogind runit
  debootstrap python3-Pillow python3-rich python3-textual xdg-dbus-proxy librsvg-utils gnupg curl dconf util-linux gawk` and, per
  distro, `pacman`/`m4` for the Arch bootstrap (or the already written `arch-install-scripts` and `archlinux-keyring` templates in
  `void/srcpkgs`); a `dnf5` shim package or the shim inside `spaces`. Run `xlint` and `xbps-src check`; the tests need
  `PYTHONPATH=src` and a fake `/usr/bin`.
- Replace in the package what `dev-install.sh` does by hand: the file copies (python package, `/usr/bin/*`, `/usr/lib/spaces`,
  `/usr/share/spaces`, polkit policy, `/etc/pam.d/spaces`, `/etc/apparmor.d/spaces-container`), the AppArmor load (INSTALL hook:
  `apparmor_parser -r`; REMOVE hook: `-R`), `/etc/spaces/config.json` generation (INSTALL hook: `spaces-nvidia-sync`; the file is not
  a package file, only `config.base.json` is), keyring/mirror setup of the Arch tooling, `conf_files=/etc/pam.d/spaces` (and the
  mirrorlist if shipped), `/var/lib/spaces`, `/var/cache/spaces`, `/var/log/spaces` directories (`make_dirs`), the runit service
  `/etc/sv/spaces-autostart` (not linked; a `vsv`-compatible `run`/`finish`/`log/run`), and removal of the per-space services in
  REMOVE (`spaces-void gc` after the last space is gone).
- Decide: Python version independence (the dev install writes into `python3.14/site-packages`; use `vmove`/`${py3_sitelib}`),
  SELinux files and the rpm specs stay out, the ubuntu keyring (currently fetched by the installer, needs a template or the
  `debootstrap` package's keyring), the pinned Fedora key and image URL (runtime download, not in the package),
  `spaces.priv` as a root-owned file the polkit policy binds to.

Afterwards (review): the login record only grows while the user is online, so a momentarily empty session list cannot make an old
session look new (that would have undone a deliberate `sv down`), and the logout of a lingering user does not start spaces; both
have unit tests. The final `dev-uninstall.sh`/`dev-install.sh` cycle of the check removed `/etc/sv/spaces-*` and
`/var/service/spaces-*` for the four spaces; each is recreated on the space's next start (`spaces-void doctor` warns until then).
Not verified: autostart on a second real login, zsh and fish, file-level collision of the entry names with packages that are not installed.


## M8: xbps-src packaging (2026-10-04)

The dev install was replaced by five xbps packages built with void-packages' `xbps-src` from this checkout
(`void/tools/xbps-build.sh`); the machine runs them. New: `void/srcpkgs/{spaces,spaces-arch-install-scripts,
spaces-archlinux-keyring,spaces-rankmirrors,ubuntu-keyring}` (the last three existed as drafts; the Arch ones were
renamed `spaces-*`), `void/srcpkgs/spaces/{INSTALL,REMOVE,INSTALL.msg}`, `spaces-archlinux-keyring/INSTALL`,
`void/wrappers/{spaces.priv,spaces-session-env,spaces-void,spaces-nvidia-sync,spaces-stop-services}` (the wrappers
`dev-install.sh` used to write from heredocs, now shared), `void/data/void.json`, `void/tools/xbps-build.sh`,
`void/spike/m8_check.py`. Changed: `native/spaces_system_broker.c`, `src/spaces/host/lxc.py`, their tests,
`dev-install.sh`, `dev-uninstall.sh`, `void.md`, `INSTALLED.md`, `readme.md`.

### Bug fix: orphaned `spaces-system-broker`

- Cause: `SystemBusService` starts the broker with `subprocess.Popen`; nothing tied it to the launcher, and
  `kill_stale_helpers` only knew `spaces-broker` and `xdg-dbus-proxy`.
- Fix (a): `kill_stale_helpers` also SIGTERMs `spaces-system-broker --broker /run/spaces/NAME/system-bus/...` of
  this space only (exact path prefix; `--relay`, other spaces and `work2` vs `work` are untouched). It runs after
  the per-space launcher lock is taken, so no live launcher can own a victim. Fix (b): `prctl(PR_SET_PDEATHSIG,
  SIGKILL)` in `spaces_system_broker.c`, only in `--broker` mode (the guest `--relay` is started by dbus-daemon),
  with the `getppid() == 1` race check; 9 added lines, no change to `system_bus.py` (the spawning thread is the
  launcher's main or supervisor thread, which lives as long as the launcher). `spaces-broker` (session broker) was
  left alone: it is spawned from session threads, where PDEATHSIG (per thread) could kill it early; fix (a)'s
  existing reaping covers it.
- Real test with the packaged binary (before: orphans of hours were seen at M7): `ubuntu id`, launcher python 5226
  (parent runsv), broker 5538 (ppid 5226); `kill -9 5226` at 11:43:22.59; one second later no
  `spaces-system-broker --broker` process (the container 5298 and the session broker 5599 survived, as before);
  next `ubuntu id` 3.8 s: new lxc-start 6001, broker 6213, session broker 6259, the old ones gone, exactly one of
  each; `sv down` leaves only runsv/svlogd. Fix (a) alone: two fake brokers (`/run/spaces/ubuntu/...` and
  `/run/spaces/other/...`) started as root outside any launcher; the next start of ubuntu killed the first and left
  the second. `m8_check.py` repeats the kill -9 test automatically (4 checks).
- svlogd warnings in the runsvdir title ("unable to lock directory: /var/log/spaces/ubuntu", "no functional log
  directories", "unable to open supervise/stat.new"): the title is runsvdir's buffer of past stderr text, not live
  processes, and it only changes when something new is written (it did not change over start/stop cycles, a
  `xbps-remove` and the new retire script). Flow that produced it: remove `/etc/sv/spaces-NAME` and `/run/runit/
  supervise.spaces-*` (the old `dev-uninstall.sh`, and `forget_unit` by deleting the directories) while runsv and
  svlogd of the service still run: runsv then cannot write `supervise/stat.new`/`log/supervise/pid.new`, and a service
  recreated at the same path gets a second svlogd that cannot take the lock the old one holds. Reproduced with a
  private runsvdir (`warning: unable to open log/supervise/pid.new: file does not exist`). Fix:
  `/usr/lib/spaces/spaces-stop-services [--remove]` (`sv down`, unlink, `sv force-shutdown`, wait for runsv and
  svlogd to be gone, pkill as a last resort, only then delete) is used by the REMOVE hook and by `dev-uninstall.sh`;
  `LxcBackend.forget_unit` does the same through `_shutdown_runsv` (supervise/lock probe). Not cleared: the text that
  is still in the title of the running runsvdir (it goes away at the next boot).

### Packages

- `spaces` 0.0.1_1 (pyproject says 0.0.1; no tag exists), `archs=x86_64`, `build_style=python3-pep517`, native helpers
  built in `post_build`/`post_install` with xbps-src's CFLAGS plus `-std=gnu11 -fPIC -Wall -Wextra` (no -Werror); the
  hardening flags pass `check_guest_abi.py --glibc-max 2.17` unchanged, so no `-U_FORTIFY_SOURCE` was needed. The wheel
  data files are used and compared against `data/portal` and `data/system-bridge` (`diff -r`, build fails otherwise);
  upstream's `usr/lib/systemd` is removed, the mount unit condition is patched as in the dev install. Unit tests run in
  the chroot with `--check`: 639 passed, 21 skipped, 1 deselected (the Ctrl-C test needs a controlling terminal).
- Packaging decisions: `ubuntu-keyring` keeps the name and the path `/usr/share/keyrings/ubuntu-archive-keyring.gpg`
  (debootstrap's fixed path: a future Void package of that name replaces ours); the Arch tools and keyring are
  `spaces-arch-install-scripts` and `spaces-archlinux-keyring` in private paths, so neither a future same-named Void
  package nor a file collision can break `pacstrap`; `rankmirrors` is its own package (`spaces-rankmirrors`, pinned
  commit, `skip_extraction`). `/etc/spaces/config.json` is generated, not packaged (the sha256 sidecar logic of
  `nvidia.py` is untouched); `/etc/spaces/void.json` is a conf file. The distro `.desktop` files and icons are not
  shipped (same as the dev install). `zstd` and `python3-platformdirs` from the task's list were left out: nothing in
  `src/` executes `zstd` (pacman links libarchive) or imports `platformdirs` (textual depends on it); `m4` is a build
  dependency of `spaces-arch-install-scripts` only; `sudo` was added (`spaces-void` re-runs itself through it).
- `xlint` clean on all five templates except the maintainer message (xlint rejects every
  `@users.noreply.github.com` address; `xbps-build.sh` filters exactly that message). `wrksrc` is not needed
  (xbps-src renames the single top directory of a tarball).
- Traps found: xbps-src treats a symlinked `srcpkgs/NAME` as a subpackage of its target (`xbps-build.sh` copies the
  templates); `checkdepends=util-linux` replaces the chroot's `chroot-util-linux` and its autodeps cleanup then removes
  `getopt` from the masterdir (all later builds fail in the install wrapper; fix: `./xbps-src zap`, bootstrap again);
  `xbps-rindex` refuses to register an older revision after a revision-2 test build (remove the file, `xbps-rindex -r`).

### Verification on the machine

- `dev-uninstall.sh` without `--purge`: `/var/lib/spaces/{arch,fedora,kali,ubuntu}` and `.host/{arch/gnupg,fedora,nvidia}`
  intact, no runsv/svlogd processes, `/etc/sv` without spaces services; the installer-owned keyring and `/etc/spaces`
  went away as designed.
- `xbps-install -R <repo> spaces` installs the five packages; `xbps-pkgdb` reports nothing; the files outside
  `/etc/spaces/config.json{,.generated}` are all owned; modes are root-owned 0755/0644 and no setuid/setgid bit exists
  (same as the dev install); `spaces-void doctor` 18 checks, 0 FAIL, 4 WARN (the four spaces have no runit service until
  their first start).
- `python3 void/spike/m8_check.py`: 84 PASS, 0 FAIL (package 29, enter each distro 6, host-PAM sudo bridge and
  login-scoped mounts through m3's throwaway user 10, GUI on niri/audio/clipboard/notification/portal through m4,
  NVIDIA nodes and `nvidia-smi` in the guest through m5, autostart smoke through m7 15, install-flavor dry run 1,
  orphaned broker 4, final state 4). `--lifecycle-only` (builds revision 2): 21 PASS: `xbps-install -u` while ubuntu runs
  keeps the same lxc-start, the profile stays loaded, `nvidia-smi` and `ubuntu id` still work; `xbps-remove` with a space
  running stops it, removes the services/links/profile/config.json and keeps `/var/lib/spaces` and `.host` (the message
  "Your spaces are kept in /var/lib/spaces" is printed; xbps printed no warning about the non-empty `make_dirs`);
  reinstall, then `spaces enter ubuntu` works. The machine was then returned to revision 1 (remove + install of a clean
  rebuild).
- Unit tests: 644 passed, 17 skipped (642 + 2 for the system broker match and the runsv shutdown).

### Open items for M9

- Monitor socket isolation (G2), hardening review of the AppArmor profile now that it ships as a package file.
- Signing, a release process and tag (`v0.0.1`: the template needs the real checksum), a public or CI-built repository,
  `xbps-src` CI, a script that checks upstream rebases against `native/`, `data/` and the wheel's data-files list (the
  `diff -r` in `post_install` already trips on portal/system-bridge changes).
- aarch64 (not built; guest helpers assume glibc x86_64 for the guests), musl hosts, pycompile on Python bumps (the package
  pins `python3>=3.14<3.15`), `spaces-broker` (session broker) PDEATHSIG, a runit `check`/`finish` for per-space services,
  `.desktop` entries and icons, tests that need a terminal in the build chroot.
