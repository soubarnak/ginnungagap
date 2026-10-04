# What the Void install puts on this machine

Since M8 the machine runs the **xbps packages** built by `void/tools/xbps-build.sh` (`spaces`, `ubuntu-keyring`,
`spaces-arch-install-scripts`, `spaces-archlinux-keyring`, `spaces-rankmirrors`). The development install
(`dev-install.sh`) is described in the second half of this file and is not what is installed now.

## Package install (current)

`xbps-query -f spaces` lists 120 files (python package 42 modules + dist-info, 8 commands in `/usr/bin`, 9 native
binaries, the data tree, docs), all root:root, no setuid or setgid bit anywhere (checked by `m8_check.py`).
Differences from the dev install are listed at the end of this section.

| Path | Package | What |
|---|---|---|
| `/usr/lib/python3.14/site-packages/spaces/`, `spaces-0.0.1.dist-info/` | spaces | the Python package (byte-compiled by xbps' pycompile trigger at install; the path follows the Python version, `python3>=3.14<3.15` is an automatic dependency) |
| `/usr/bin/{spaces,spaces.priv,spaces-void,spaces-session-env}` | spaces | `spaces` is the wheel's console script; the others are the sh wrappers of `void/wrappers/` (`spaces.priv` replaces the wheel's script: the polkit policy binds to it and the shim directory is first on its PATH) |
| `/usr/bin/{ubuntu,fedora,kali,arch-linux}` | spaces | `void/entry/enter-space` |
| `/usr/lib/spaces/{spaces-pam,spaces-broker,spaces-system-broker}`, `guest/*` | spaces | native helpers (`make -C native`, `check_guest_abi.py --glibc-max 2.17` passes with xbps-src's hardening flags) |
| `/usr/lib/spaces/{spaces-lxc,spaces-nvidia-sync,spaces-stop-services}` | spaces | cgroup wrapper, config/NVIDIA sync, clean retirement of the runit services (used by REMOVE and `dev-uninstall.sh`) |
| `/usr/lib/spaces/void/bin/{pacstrap,arch-chroot,dnf5}` | spaces | shims, first on `spaces.priv`'s PATH |
| `/usr/lib/spaces/rankmirrors` | spaces-rankmirrors | pacman-contrib 1.13.1 script (commit 75d4a705) |
| `/usr/lib/spaces/void/arch-install-scripts/{bin/*,COPYING,VERSION}` | spaces-arch-install-scripts | arch-install-scripts 31 |
| `/usr/share/spaces/void/archlinux-keyring/*` | spaces-archlinux-keyring | archlinux-keyring 20260909 (INSTALL creates `/var/lib/spaces/.host/arch/gnupg` and `.host/arch/keyring-version`; an existing keyring of the same version is kept) |
| `/usr/share/keyrings/ubuntu-archive-keyring.gpg` | ubuntu-keyring | ubuntu-keyring 2026.08.18 (the build refuses a tarball without key F6ECB376...C93C) |
| `/usr/share/spaces/{pam,keys,repos,portal,system-bridge,systemd}`, `config.base.json`, `void/{arch-pacman.conf,arch-mirrorlist,shell/*}` | spaces | data (`systemd/run-spaces-proc.mount` has `ConditionVirtualization=container`; upstream's units under `/usr/lib/systemd` are dropped) |
| `/usr/share/polkit-1/actions/org.anatase.spaces.policy`, `/etc/pam.d/spaces` (conf file), `/etc/apparmor.d/spaces-container` | spaces | security glue |
| `/etc/spaces/void.json` (conf file) | spaces | seed `{"version": 1, "distros": {}}` for the user's extras |
| `/etc/sv/spaces-autostart/{run,finish,log/run}` + `supervise` links `/run/runit/supervise.spaces-autostart{,-log}` | spaces | not linked into `/var/service` |
| `/usr/share/doc/spaces/`, `/usr/share/licenses/spaces/LICENSE` | spaces | docs |
| `/var/lib/spaces`, `/var/cache/spaces`, `/var/log/spaces` | spaces (`make_dirs`) | root 0755; kept on removal |

Not packaged files, created by hooks or at run time: `/etc/spaces/config.json` and `config.json.generated` (INSTALL runs
`spaces-void sync-config`; REMOVE deletes them while the hash still matches), `/etc/pacman.d/mirrorlist` (seeded by INSTALL only when
absent, deleted by REMOVE only while it equals the shipped default), the pacman keyring, the NVIDIA farm, the Fedora bootstrap,
`/etc/sv/spaces-NAME` and `/var/service/spaces-NAME` (created by the first start of a space; REMOVE retires them), everything under
"Created at run time" below.

Dependencies: see `void/srcpkgs/spaces/template`. Not installed on purpose (as before): `spaces@.service` units, `.desktop` entries
and icons for the distros, SELinux policy, `/etc/polkit-1/rules.d/*`.

Differences between the package and the dev install: the package does not need `gcc make pkg-config glib-devel pam-devel` on the
machine, installs `rankmirrors`, the Arch tools and the keyrings through packages (the dev install downloads pinned tarballs),
ships docs, the license, `/etc/spaces/void.json` and the Python dist-info, lets xbps compile the bytecode, keeps no
`dev-install.state`, takes the supervise link `-log` for the autostart log service (`.log` in the dev install; per-space services created
by Python still use `.log`), and loads/unloads the AppArmor profile from hooks. Same files otherwise (the build compares the portal and
system-bridge trees with `data/`).

Uninstall: `sudo xbps-remove spaces` (stops and removes the services, unloads the profile; spaces and `.host` stay), then
`sudo xbps-remove -o` for the helper packages. `dev-uninstall.sh` must not be used for a package install.

## Development install (dev-install.sh; not the current state)

Created by `sudo void/tools/dev-install.sh` (idempotent, copies files, never links into the
checkout). Reverted by `sudo void/tools/dev-uninstall.sh` (see the end of this file).

## Files (all root:root)

| Path | What |
|---|---|
| `/usr/lib/python3.14/site-packages/spaces/` | copy of `src/spaces` (42 files, byte-compiled) |
| `/usr/bin/spaces` | console script, `from spaces.__main__ import main` |
| `/usr/bin/spaces-session-env` | `#!/bin/sh` wrapper, `exec /usr/bin/python3 -I -m spaces.host.session_env "$@"`; run by the user (`publish`, `show`), see "Desktop session" below |
| `/usr/bin/spaces.priv` | `#!/bin/sh` wrapper: PATH=`/usr/lib/spaces/void/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`, then `exec /usr/bin/python3 -I -c 'import sys; from spaces.priv import main; sys.exit(main())' "$@"`. This is the path the polkit policy binds to. |
| `/usr/bin/spaces-void` | `#!/bin/sh` wrapper, `exec /usr/bin/python3 -I -m spaces.host.cli "$@"`: `autostart list|enable|disable`, `gc`, `doctor`, `sync-config`, `install-flavor` (M7) |
| `/usr/bin/{ubuntu,fedora,kali,arch-linux}` | copies of `void/entry/enter-space` (runs `spaces enter NAME -- "$@"`, NAME from the command name without `-linux`; POSIX sh; one leading `--` is dropped); not `/usr/bin/arch` (coreutils) (M7) |
| `/usr/lib/spaces/spaces-pam`, `spaces-broker`, `spaces-system-broker` | host native helpers, built by `make -C native` |
| `/usr/lib/spaces/guest/{pam_spaces.so,spaces,spaces-portal,spaces-secret-helper,spaces-open,spaces-system-broker}` | guest native helpers (glibc <= 2.17 checked by `check_guest_abi.py`) |
| `/usr/lib/spaces/spaces-lxc` | cgroup wrapper from `void/bin/spaces-lxc` |
| `/usr/lib/spaces/void/bin/` | shim directory, first on `spaces.priv`'s PATH: `pacstrap` and `arch-chroot` (Arch), `dnf5` (Fedora, M6) |
| `/usr/share/spaces/{pam,keys,repos,portal,system-bridge,systemd}` | data files (31 files); `systemd/run-spaces-proc.mount` has `ConditionVirtualization=container` |
| `/usr/share/polkit-1/actions/org.anatase.spaces.policy` | polkit actions (unmodified upstream file) |
| `/etc/pam.d/spaces` | `data/pam/spaces.system-auth` (includes Void's `system-auth`, pam_unix) |
| `/etc/apparmor.d/spaces-container` | AppArmor profile, loaded with `apparmor_parser -r` (the launcher also loads it on demand) |
| `/usr/share/spaces/config.base.json` | shipped base configuration: packages per distro (from `void/data/config.base.json`) |
| `/usr/lib/spaces/spaces-nvidia-sync` | `#!/bin/sh` wrapper, `exec /usr/bin/python3 -I -m spaces.host.nvidia "$@"`; run by the runit `run` script before every launch and by hand (M5) |
| `/etc/spaces/config.json` | generated by `spaces-nvidia-sync` from the base config, the optional `/etc/spaces/void.json` (same schema, user extras) and the NVIDIA mounts/overlays; never overwritten when edited by hand (`config.json.new` is written instead) |
| `/etc/spaces/config.json.generated` | sha256 of the last generated `config.json`; the "generated" marker |
| `/usr/share/spaces/void/shell/{spaces.sh,spaces.fish}` | OPT-IN shell integration (functions `arch ubuntu fedora kali`, hints for `apt apt-get dnf` where the host lacks them); sourced by nothing, see `void/docs/void.md` (M7) |
| `/etc/sv/spaces-autostart/{run,finish,log/run}` | runit service for autostart, installed but NOT linked into `/var/service`; `supervise` links point to `/run/runit/supervise.spaces-autostart{,.log}` (M7) |
| `/etc/spaces/dev-install.state` | what the installer added (keyring, config, packages); used by the uninstaller. `packages` accumulates across runs and is mirrored in `/var/lib/spaces/.host/dev-install.packages`, which a plain uninstall keeps (M7) |
| `/usr/share/keyrings/ubuntu-archive-keyring.gpg` | from `ubuntu-keyring_2026.08.18.tar.xz` (sha256 `652d6b53...f7c4`); installed only after both Ubuntu archive keys, in particular `F6ECB3762474EDA9D21B7022871920D1991BC93C`, were found in it |

Not installed on purpose: `spaces@.service` systemd units, `.desktop` entries and icons for
distros, `rankmirrors` (Arch), SELinux policy, `/etc/polkit-1/rules.d/*` (wheel is already an
administrator through `/usr/share/polkit-1/rules.d/50-default.rules`).

## xbps packages

Packages added by the installer so far: `m4 pacman pam-devel` (the state file and
`/var/lib/spaces/.host/dev-install.packages` list exactly these; before M7 an uninstall and reinstall dropped
`pam-devel` from the record). `pam-devel` was installed by M3 (build dependency of `pam_spaces.so`). Everything else
the build and runtime need was already present (gcc, make, pkg-config, glib-devel, python3-Pillow,
python3-rich, python3-textual, polkit, lxc, debootstrap, apparmor, gnupg, curl, runit, elogind,
xdg-dbus-proxy, librsvg-utils, dconf). `dev-install.sh` installs any of them that is missing.
Remove with `sudo xbps-remove pam-devel` or `dev-uninstall.sh --remove-packages` (the latter only
knows about packages recorded in the state file of that install).

## Created at run time

| Path | Created by |
|---|---|
| `/var/lib/spaces/ubuntu/` (rootfs, home, info.json, autostart-users, optional autostart-boot) | `sudo spaces create ubuntu --preset basic` (Ubuntu 26.04 "resolute") |
| `/var/cache/spaces/ubuntu/` | same, persistent apt cache |
| `/etc/sv/spaces-ubuntu/` + `/var/service/spaces-ubuntu` symlink | `LxcBackend.ensure_service` on first start (runit service with a permanent `down` file, plus `log/` svlogd service); its `run` script calls `spaces-nvidia-sync --quiet` before `spaces.priv launch` (M5) |
| `/var/lib/spaces/.host/nvidia/<pkgver>/{lib,lib32,share,bin,opencl}`, `current` | symlink farm of the host NVIDIA userspace, rebuilt by `spaces-nvidia-sync` (root 0755; removed when the driver is gone) |
| `/run/spaces/lxc/NAME/launcher.lock` | per-space launcher lock (M5), held while a launcher lives |
| `/run/runit/supervise.spaces-ubuntu{,.log}` | runsv |
| `/var/log/spaces/ubuntu/` | svlogd |
| `/run/spaces/` (tmpfs) | launcher: LXC config, ready marker, auth socket, desktop state |
| `/sys/fs/cgroup/spaces/ubuntu/` | launcher, removed on stop; root `cgroup.subtree_control` stays empty |
| `/usr/local/share/applications/spaces-ubuntu-v1-*.desktop`, `spaces-icons/` | application shortcut export (a desktop feature, M4) |

## Services

No service is enabled at boot, and `spaces-autostart` is not linked: to use autostart,
`sudo ln -s /etc/sv/spaces-autostart /var/service/` (see M7 in RESULTS.md; its logs are in
`/var/log/spaces/autostart/`, per-boot state in `/run/spaces/autostart/state.json`). `spaces-ubuntu` is supervised by runsvdir but `down` by default;
`spaces enter`, `spaces start` and `sv once` start it, `sv down` stops it.

## Desktop session (M4)

Nothing is edited in the user's home or in niri/DMS configuration. The root-side launcher finds the
graphical session by itself: it asks libelogind for the user's active local wayland/x11 session,
takes the lowest-pid process elogind places in it that has `DBUS_SESSION_BUS_ADDRESS` and a display
in its environment, and keeps only the allowlisted variables. Optionally the user publishes the
environment once per login (preferred, exact, no `/proc` scan): add this line to
`~/.config/niri/config.kdl`:

```
spawn-at-startup "spaces-session-env" "publish"
```

| Path | What |
|---|---|
| `/run/user/UID/spaces/environment` | published environment, 0600, user-owned, in a 0700 user-owned directory (tmpfs, gone with the login); ignored unless it names a currently active graphical session of that user |
| `/run/user/UID/bus` | symlink to the real `dbus-run-session` socket (`/tmp/dbus-XXXX`), user-owned, created by the launcher only when `bus` is missing or a stale symlink; never replaces a real socket or file; abstract buses are used directly |
| `/run/spaces/NAME/desktop/UID/portal/bus` | filtered session bus (`xdg-dbus-proxy`, run as the user), bound into the guest at `/run/user/UID/bus` |

Guest packages added by hand in the `ubuntu` space during M4 (kept): `gnome-calculator`, `locales`
(with `en_US.UTF-8` generated), `libglib2.0-bin` (gdbus), `pulseaudio-utils` (pactl),
`libnotify-bin` (notify-send), `wl-clipboard`. The guest-native helpers need only the guest's
libglib/libgobject/libgio, which the space's base package set already pulls in through polkitd and
xdg-desktop-portal. `void/tools/check-guest-glib.py` verifies the symbols.

## Devices, GPU and hotplug (M5)

No udev rule is installed: eudev's `70-uaccess.rules` and elogind already tag and ACL the DRM, sound,
video and joystick nodes, and the NVIDIA nodes are found through `/proc/devices` (see RESULTS.md,
M5). Nothing in modprobe or the NVIDIA driver is touched. Guest packages added by hand in M5
(kept): `mesa-utils`, `mesa-utils-bin`, `vulkan-tools`, `mesa-vulkan-drivers`, `libgl1-mesa-dri`,
`libegl1`, `libgbm1`, `libvulkan1`, `gdb`, `strace`. Placeholder empty files for every NVIDIA
library alias, ICD file and binary now exist in the ubuntu rootfs (`/usr/lib/x86_64-linux-gnu`,
`/usr/lib/i386-linux-gnu` if present, `/usr/share/{glvnd,egl,vulkan,nvidia}`, `/usr/bin/nvidia-*`):
the launcher creates them as bind targets and leaves them. The NVIDIA userspace follows the xbps
packages: after `xbps-install -u` the next launch rebuilds the farm; a kernel module that differs
from the userspace version makes `spaces-nvidia-sync` warn until the next reboot.

Extra per-distro packages, mounts or overlays go into `/etc/spaces/void.json`
(`{"distros": {"ubuntu": {"packages": ["htop"]}}}`), then run `sudo /usr/lib/spaces/spaces-nvidia-sync`
or just restart the space.

## Arch, Kali and Fedora host tooling (M6)

Installed by `dev-install.sh` (`void/tools/dev-install-arch.sh`, `dev-install-fedora.sh`; skip with
`--skip-distro-tools`). Kali needs nothing (Void's debootstrap has `kali-rolling`).

| Path | What |
|---|---|
| xbps `pacman` 7.1, `m4` | recorded in `dev-install.state` when the installer adds them (`pacman` and `m4` were added by hand in M6 and are listed there) |
| `/usr/lib/spaces/void/arch-install-scripts/{bin/{pacstrap,arch-chroot,genfstab},COPYING,VERSION}` | arch-install-scripts 31, sha256 `ef22eae93b5cc78c7e7982acc160428cced9f96cb95090aeb77d55bc844a988e` (gitlab archive of tag v31) |
| `/usr/lib/spaces/void/bin/{pacstrap,arch-chroot,dnf5}` | shims (`void/bin/*`); `dnf5` is the Fedora bootstrap shim |
| `/usr/lib/spaces/rankmirrors` | pacman-contrib 1.13.1 script, commit 75d4a705, source sha256 `b67902d26a8b193cc096421c8d780f795a253d0742cd31d592254059af15cc27` |
| `/usr/share/spaces/void/arch-pacman.conf` | core + extra, own GPGDir, `SigLevel = Required DatabaseOptional` |
| `/usr/share/spaces/void/archlinux-keyring/{archlinux.gpg,archlinux-trusted,archlinux-revoked,VERSION}` | archlinux-keyring 20260909, tarball sha256 `935ad345a7700358367ca9a2f70220f30869492c31b24e9e4e4e89f05bf5528a` |
| `/var/lib/spaces/.host/arch/gnupg` | pacman keyring for the host-side pacstrap, root 0700 |
| `/var/lib/spaces/.host/fedora/44/` | created on the first `dnf5` call: `Fedora-Container-44-1.7-x86_64-CHECKSUM`, the Container Base `.oci.tar.xz` (sha256 `75200f5752a74a21a616ca9a75e25beb594e2e117a0195c54f87c0b3e3974d1b`), `root/` (unpacked bootstrap, 260 MB), `lock` |
| `/etc/pacman.d/mirrorlist` | default list, only when absent (state `arch_mirrorlist=installed`); rewritten by the `rankmirrors` option; removed by the uninstaller |

Spaces created in M6: `/var/lib/spaces/{kali,arch,fedora}` with their caches and runit services
(`/etc/sv/spaces-NAME`), like `ubuntu`. Guest packages added by hand (kept): `gnome-calculator`,
vulkan and GL tools and Mesa drivers in each (arch: `strace`).

Guest packages added by hand or by `spaces-void install-flavor` in M7 (kept): ubuntu got
`python3-pyqt6` (Qt test app) and the gtk flavour set (`adwaita-icon-theme dconf-gsettings-backend gnome-themes-extra
gsettings-desktop-schemas qt5-gtk-platformtheme qt6-gtk-platformtheme xdg-desktop-portal-gtk` plus their dependencies); arch got
`adw-gtk-theme gnome-themes-extra` and the rest of its set (`gtk3 dconf xdg-desktop-portal-gtk ...` were present); fedora got
`adw-gtk3-theme` (the rest was present). Kali was not changed.

## Symlinks

`/var/service/spaces-NAME -> /etc/sv/spaces-NAME` (one per created space). `/run/user/UID/bus` (above) is created at run time and lives on tmpfs; uninstalling leaves it alone.

## Uninstall

```
sudo void/tools/dev-uninstall.sh                    # stop services, remove all installed files; keep spaces
sudo void/tools/dev-uninstall.sh --purge            # additionally delete /var/lib/spaces, /var/cache/spaces, /var/log/spaces
sudo void/tools/dev-uninstall.sh --remove-packages  # additionally xbps-remove packages recorded at install
```

It removes `/var/service/spaces-*`, `/etc/sv/spaces-*`, the AppArmor profile (unloaded first),
the keyring (if this install added it), `config.json` (if it still matches its recorded hash),
`config.json.generated`, `config.json.new`, `spaces-void` and the four entry commands, the
autostart service (unlinked and stopped first) and the exported shortcuts. Since M7 it keeps
`/var/lib/spaces/.host` (pacman keyring, Fedora bootstrap root, NVIDIA farm, package record); only
`--purge` deletes it. `/etc/spaces/void.json`
is the user's and is left alone (`rmdir /etc/spaces` then fails and keeps it).
