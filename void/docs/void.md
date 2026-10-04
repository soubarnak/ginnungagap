# Spaces on Void Linux

This is the user guide for the Void port of Spaces (the `void` branch of ginnungagap, a fork of
[anatase-org/spaces](https://github.com/anatase-org/spaces)). The upstream program is a wrapper
around `systemd-nspawn`; Void has no systemd, so this port runs the same spaces (Ubuntu, Kali,
Arch, Fedora) in LXC containers supervised by runit, with elogind for logins, polkit for
authorisation and AppArmor instead of SELinux. The CLI (`spaces create`, `enter`, `configure`,
`delete`) and the guest integration (host PAM for `sudo`, desktop forwarding, shortcuts, portals,
GPU) are upstream's.

Status: development install only (`void/tools/dev-install.sh`); there is no xbps package yet.
Tested on Void x86_64 (glibc), runit, elogind 252, kernel 7.x, LXC 6.0, niri.

## Install

```
git clone <this repo> ginnungagap && cd ginnungagap && git checkout void
sudo void/tools/dev-install.sh        # idempotent; re-run after changing the sources
sudo void/tools/dev-uninstall.sh      # revert; spaces and /var/lib/spaces/.host are kept
sudo void/tools/dev-uninstall.sh --purge   # also delete every space, caches, logs and .host
```

Everything is copied to its system location, nothing refers back to the checkout (root never
executes code that a normal user can edit). The installer adds the xbps packages it needs
(`pam-devel`, `pacman`, `m4` and, if missing, `lxc debootstrap apparmor polkit elogind runit
xdg-dbus-proxy ...`) and records which ones it added in `/etc/spaces/dev-install.state` and
`/var/lib/spaces/.host/dev-install.packages`; `--remove-packages` on uninstall removes exactly
those. `--skip-keyring`, `--skip-packages`, `--skip-distro-tools` skip parts. The full file list
is in `void/spike/INSTALLED.md`; in short:

| Where | What |
|---|---|
| `/usr/lib/python3.*/site-packages/spaces/` | the Python package |
| `/usr/bin/spaces`, `spaces.priv`, `spaces-session-env`, `spaces-void` | CLI, the root helper behind polkit, the session publisher, the administration CLI |
| `/usr/bin/{ubuntu,fedora,kali,arch-linux}` | entry commands (never `/usr/bin/arch`: coreutils owns it) |
| `/usr/lib/spaces/` | native helpers, guest helpers, the cgroup wrapper `spaces-lxc`, `spaces-nvidia-sync`, shims for `pacstrap`/`dnf5` |
| `/usr/share/spaces/` | data, `config.base.json`, `void/shell/spaces.{sh,fish}` (opt-in) |
| `/etc/sv/spaces-autostart/` | the autostart runit service (installed, not linked) |
| `/etc/apparmor.d/spaces-container`, `/etc/pam.d/spaces`, the polkit policy | security glue |
| `/etc/spaces/config.json` | generated, see "Configuration" |
| `/var/lib/spaces/NAME/`, `/var/cache/spaces/NAME/`, `/var/log/spaces/NAME/` | a space, its package cache, its log |
| `/etc/sv/spaces-NAME`, `/var/service/spaces-NAME` | one runit service per space, created on first start, always `down` until started |

## Creating and using spaces

```
sudo spaces create ubuntu --preset basic      # also: arch, fedora, kali; --preset develop
spaces enter ubuntu                           # login shell in the space (starts it, ~4 s cold)
spaces enter ubuntu -- id                     # run one command as yourself
sudo spaces enter ubuntu --root -- apt-get update   # as root (administrator authentication)
sudo spaces configure ubuntu --user           # grant your user access to an existing space
sudo spaces delete ubuntu                     # delete it (then: spaces-void gc)
spaces enter --graphical ubuntu -- app        # --graphical goes before the space name; used by desktop shortcuts
```

`create` and `configure` need an administrator (polkit asks; `sudo` also works). `enter` and `start`
as yourself need no password. A space is a runit service that is `down` by default and started
with `sv once`; `sudo sv down /var/service/spaces-NAME` stops it (gracefully, `lxc-stop -t 30`).
Once started it runs until stopped or until shutdown.

By default `create` and `configure` enable "autostart at login" for your user (upstream's
`spaces@NAME` user unit). On Void this only records your user name in
`/var/lib/spaces/NAME/autostart-users`; it does nothing until you link the autostart service
(next section). `--no-enable` skips the recording.

### Entry commands

`ubuntu`, `fedora`, `kali` and `arch-linux` are installed in `/usr/bin`. `ubuntu` alone opens a
shell, `ubuntu apt list --installed` or `ubuntu -- id` runs a command in the space (one leading
`--` is accepted, upstream's wrapper would pass it on).

Opt-in shell snippet `/usr/share/spaces/void/shell/spaces.sh` (bash, zsh, other POSIX shells) defines
the functions `arch`, `ubuntu`, `fedora`, `kali` and friendly hints for `apt`, `apt-get` and `dnf`
that point at the space, but only where the host has no such command. `pacman` and `xbps-*` are never
touched (the host has pacman as a dependency of the Arch bootstrap). `arch` exists only as this
function, because `/usr/bin/arch` is coreutils. Enable it, your choice:

```
# 1. just for you, in ~/.bashrc or ~/.zshrc
[ -r /usr/share/spaces/void/shell/spaces.sh ] && . /usr/share/spaces/void/shell/spaces.sh
# 2. for every bash user
sudo ln -s /usr/share/spaces/void/shell/spaces.sh /etc/bash/bashrc.d/spaces.sh
```

`SPACES_NO_HINTS=1` keeps the functions and drops the hints. A fish version is
`void/shell/spaces.fish` (copy it to `~/.config/fish/conf.d/`; not tested here, no fish on the dev machine).
Nothing in your dotfiles is edited by the installer.

## Services and opt-in integration

| What | How |
|---|---|
| Autostart | `sudo ln -s /etc/sv/spaces-autostart /var/service/` (unlink the same way to turn it off; Void's usual convention) |
| Desktop environment publishing (exact, no `/proc` scan) | niri: `spawn-at-startup "spaces-session-env" "publish"` in `~/.config/niri/config.kdl`; sway/others: run `spaces-session-env publish` at login |
| Shell snippet | see above |

### Autostart

```
spaces-void autostart list                       # what is enabled, and whether the service is linked
sudo spaces-void autostart enable ubuntu         # start ubuntu when you log in
sudo spaces-void autostart enable ubuntu --user bob
sudo spaces-void autostart enable ubuntu --boot  # start at boot (no user mounts yet, as upstream)
sudo spaces-void autostart disable ubuntu [--boot] [--user bob]
```

`spaces-void` re-runs itself through sudo when it needs root. Semantics, mirroring upstream's
`spaces@NAME` user unit (`WantedBy=default.target`) and `systemctl enable` for boot:

* The `spaces-autostart` service watches elogind. At its start and whenever a login changes: for every
  space with the boot flag (`/var/lib/spaces/NAME/autostart-boot`) it runs `sv once` once per boot;
  for every user in `autostart-users` whose state is `active`, `online` or `lingering` it runs `sv once`
  on each space of that user that is not running, once per login (a new session id, or the start of
  lingering).
* A deliberate `sudo sv down` is therefore not undone until that user's next login, nor by a restart of
  the daemon (its per-boot state is in `/run/spaces/autostart/state.json`). A reboot starts over.
* Linking the service starts the matching spaces within seconds (measured: 4 s); without the link nothing
  happens, whatever is enabled. Logs: `/var/log/spaces/autostart/current`.
* A space must not be called `autostart` (it would collide with the service directory).

## Configuration

`/etc/spaces/config.json` (guest packages, extra mounts and overlays per distro) is generated, never
edit it. `spaces-nvidia-sync` rebuilds it before every space start from `/usr/share/spaces/config.base.json`,
the NVIDIA userspace of the host and your `/etc/spaces/void.json`; `sudo spaces-void sync-config` runs it
by hand. A `config.json` that was edited by hand is kept and the new text goes to `config.json.new`.

`/etc/spaces/void.json`:

```json
{
  "desktop_flavor": "auto",
  "distros": {"ubuntu": {"packages": ["htop"]}}
}
```

Packages apply when a space is created (and at its rebuild).

### Desktop flavour

The guests carry KDE's polkit agent and portal backend (the launcher parses the polkit-kde log line,
so they stay). `desktop_flavor` adds a GTK look on a non-KDE host:

* `auto` (default): `XDG_CURRENT_DESKTOP` of the creating user's active graphical elogind session. KDE or
  Plasma gives `kde`; niri, sway, Hyprland, GNOME, XFCE and everything else give `gtk`. With no session
  to look at (boot) the last answer is kept (`/var/lib/spaces/.host/desktop-flavor`), else `gtk`.
* `gtk`: adds to every distro's packages: icon theme (`adwaita-icon-theme`), `gnome-themes-extra` (Debian
  family and Arch), `adw-gtk-theme` / `adw-gtk3-theme` (Arch, Fedora; not packaged on Ubuntu/Kali),
  `xdg-desktop-portal-gtk`, GSettings schemas and dconf, and the GTK3 platform theme for Qt:
  `qt5-gtk-platformtheme` + `qt6-gtk-platformtheme` on Ubuntu and Kali; on Arch and Fedora the Qt
  GTK3 plugin ships inside `qt6-base` / `qt6-qtbase-gui`, so `gtk3` is the package.
* `kde`: adds nothing.

Into an existing space: `sudo spaces-void install-flavor ubuntu [--flavor gtk]` (runs the distro's
non-interactive install as root through `spaces enter --root`, starting the space the normal way, and
prints what it installed). The host exports `QT_QPA_PLATFORMTHEME=gtk3`; without the plugin Qt silently
falls back to its generic theme, with it Qt applications follow the GTK theme.

## Security model: what replaces SELinux

Upstream relies on `systemd-nspawn` (namespaces, capability bounding set, seccomp filter, device cgroup)
plus an SELinux policy. This port keeps the same layers except SELinux:

| Layer | Void port |
|---|---|
| Namespaces | LXC: mount, pid, uts, ipc and cgroup namespaces; **the network namespace is shared with the host** (as upstream's default), no user namespace (root in a space is host root, as upstream) |
| Capabilities | `lxc.cap.keep` = nspawn's default set, widened or narrowed per permission level like upstream |
| Syscalls | seccomp: LXC's `common.seccomp` base plus the per-permission adjustments |
| Devices | cgroup2 device controller (eBPF), levels `disabled`, `basic`, `admin`, `full` from the permission settings; at `full` watchdogs and VT/console devices stay denied; hot-plug and NVIDIA nodes follow the level |
| Mandatory access control | AppArmor profile `spaces-container` (derived from LXC's `lxc-container-default-cgns`: it additionally allows the mounts systemd uses to sandbox units, the rest is denied as in LXC) **instead of** the SELinux policy `spaces-selinux` |
| Authorisation | polkit (`org.anatase.spaces.policy` bound to `/usr/bin/spaces.priv`) and host PAM for guest `sudo`, as upstream |
| cgroups | each space under `/sys/fs/cgroup/spaces/NAME`, `cgroup.subtree_control` of the root stays empty (elogind); a private mount namespace hides elogind's v1 hierarchy from LXC |

What is lost or different compared with upstream on Fedora/Anatase:

* No SELinux: no relabelling of `/var/lib/spaces` or `~/.ssh/config`, no type enforcement between
  host and guest processes. AppArmor confines the container processes by path; the upstream rule
  "it is not possible to mount SSH or GPG directories" is enforced by Spaces' own mount policy only,
  not by a second MAC layer.
* A compromised guest root is host root with a seccomp filter, a capability set, an AppArmor profile and
  a device cgroup between it and the machine; a kernel bug or a mount mistake escapes (as upstream warns).
  Do not give a space the `develop` preset or the `full` device level unless you trust it.
* The portal proxy filter lets the guest call a number of host portals without a Spaces prompt (screen
  saver lock/inhibit, notifications, clipboard, USB, screencast, ...). Review: `void/docs/portal-filter-review.md`.
* Processes started by `spaces enter` run in a transient service of the guest's own systemd (inside the
  container), not in a host scope.

## Troubleshooting

```
sudo spaces-void doctor        # PASS/WARN/FAIL: lxc, apparmor and the profile, cgroup layout, elogind, polkit,
                               # runit service sanity per space, autostart link, config.json, nvidia sync
spaces-void gc -n              # services of spaces that no longer exist (drop -n to remove them)
sudo tail -f /var/log/spaces/NAME/current   # launcher and guest start log of one space (svlogd)
sudo sv status /var/service/spaces-NAME
```

* Space will not start: `sudo sv once /var/service/spaces-NAME` and read `/var/log/spaces/NAME/current`;
  `lxc-info -P /run/spaces/lxc -n NAME` (through `/usr/lib/spaces/spaces-lxc`) shows LXC's view.
* AppArmor denials: `dmesg | grep DENIED`; `sudo apparmor_parser -r /etc/apparmor.d/spaces-container`
  reloads the profile (the launcher also loads it on demand).
* Guest sees no GPU after an NVIDIA update: `sudo spaces-void sync-config`, restart the space. After a driver
  update the kernel module and the userspace differ until reboot (doctor warns).
* **kill -9 of the launcher / hard crash**: the container may keep running orphaned. `sudo sv down /var/service/spaces-NAME`
  (it reaps stale containers on the next start); if it refuses: `sudo /usr/lib/spaces/spaces-lxc lxc-stop -P /run/spaces/lxc -n NAME -k`
  then remove `/sys/fs/cgroup/spaces/NAME` (`rmdir` from the leaves). The next `sv once` cleans the rest.
* **cgroup base**: LXC needs `cgroup.subtree_control` of `/sys/fs/cgroup` empty and cgroup2 mounted there;
  elogind's v1 `name=elogind` mount is tolerated (hidden by the wrapper). If `lxc-start` fails with
  `Failed to set "devices.deny"`, something ran `lxc-*` without `/usr/lib/spaces/spaces-lxc`.
* Fedora bootstrap offline or slow: the `dnf5` shim retries (5), times out (30 s), downloads 8 packages in
  parallel and tries the fastest mirror; options you pass with `--setopt` win.

## Limits and known gaps

* Development install only; no xbps package, no INSTALL/REMOVE hooks, x86_64 only.
* Autostart's reaction to a new login is unit-tested with a fake elogind; the real test covers a service
  start while a session is active (no second login is possible from a single session).
* The Arch `/usr/lib32` NVIDIA overlay is only created when the guest's `pacman.conf` enables `[multilib]`.
* `desktop_flavor` changes only what is installed at creation (or by `install-flavor`); it does not remove
  anything. Installing `xdg-desktop-portal-gtk` next to the KDE backend leaves backend choice to
  xdg-desktop-portal (`Spaces:niri` is not a known desktop, so both are candidates).
* Idle shutdown, per-user lingering mounts and the TUI (textual) first-run flow are as upstream or untested.
* Mirrors: Ubuntu keyring, the Arch keyring and the Fedora image are verified by pinned checksums and
  signatures (see `void/spike/RESULTS.md`, M6).
