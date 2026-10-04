# Review of the `lxc-spaces-container` AppArmor profile (M9)

Subject: `void/apparmor/lxc-spaces-container`, installed as `/etc/apparmor.d/lxc-spaces-container`. It is LXC's
`lxc-container-default-cgns` (via `abstractions/lxc/container-base`) plus the mounts that systemd needs to build
unit sandboxes in a guest. Reviewed on kernel 7.2.8, AppArmor 4.1.7 (parser and kernel `network_v9`), LXC 6.0.3,
with the four guests Ubuntu, Arch, Kali and Fedora. The tests are in `void/spike/m9_check.py`; what they did
is in `void/spike/RESULTS.md`.

The short version: the profile keeps the guest's *ordinary* behaviour working and refuses the obvious ways to
mount something new, but **AppArmor is not a boundary against a hostile guest root** here (finding 1). A space
is as trustworthy as its root user; `docs/void.md` says so, and this review gives the evidence.

## What changed

| Rule | Decision | Why |
|---|---|---|
| `mount fstype=proc -> /run/systemd/**` | **removed** | With it, root in a guest could mount a fresh `proc` below `/run/systemd/` and write the host's `/proc/sys/kernel/core_pattern` (a command that the host kernel runs as root). Demonstrated on Ubuntu before the change. systemd asks for a private proc only for `ProtectProc=`, `ProcSubset=`, `PrivatePIDs=` and `PrivateNetwork=`, and carries on with the existing mounts when it is refused. |
| `mount fstype=cgroup -> /sys/fs/cgroup/**` | **removed** | The legacy cgroup v1 filesystem. The guests only use cgroup2 (`cgroup:rw:force` is done by LXC before the profile applies), and nothing mounted v1. |
| `mount fstype=sysfs` | stays denied (it never was allowed) | Same reasoning as proc; the denial is visible in `dmesg` for `polkitd`, `systemd-logind` and `systemd-userd` (`PrivateNetwork=yes` units) and is harmless. |

After the change every distro boots to `systemctl is-system-running` = `running` with no failed unit, and a
transient unit with `ProtectSystem=strict`, `PrivateTmp`, `ProtectKernelTunables`, `PrivateNetwork`,
`ProtectHome` and `NoNewPrivileges` still starts (all four guests, `m9_check.py`). The only mount denials
during a boot are the fresh `proc`/`sysfs` that systemd falls back from, plus Fedora's `rpc_pipefs` on
`/run/rpc_pipefs` (nfs-utils; it was denied before as well and nothing needs it).

## Rules reviewed and kept

| Rule | Why it stays |
|---|---|
| `deny mount fstype=devpts` | LXC mounts the guest's devpts itself; a guest-made one would shadow it. |
| `mount fstype=cgroup2 -> /sys/fs/cgroup/**` | systemd may remount its own cgroup2 view (`ProtectControlGroups=`). The target is restricted. |
| `mount fstype=tmpfs`, `fuse*` | `PrivateTmp=`, `/run` and `/tmp`; `fuse` for FUSE filesystems (needs `/dev/fuse`, which is a device policy matter). |
| `mount fstype=overlay` | overlay mounts of nested container engines (podman, buildah) in a guest. |
| `mount fstype=ext*`, `xfs`, `btrfs` | They could be dropped without breaking a boot. They are kept for a guest that is given a block device: with the default device levels the guest cannot open one (the device cgroup is closed), so the rule only matters at the `admin` and `full` device levels. |
| `rw/ro/remount/rbind/move/rslave/rprivate/rshared` rules, `umount`, `pivot_root` | systemd's unit sandboxing (`rbind` of `/` to `/run/systemd/mount-rootfs`, read-only and no-exec remounts, mount moves, propagation changes). These are the rules that stock LXC profiles lack and that make `systemd-logind` fail under them. |

## Findings

1. **The new mount API bypasses the mount rules (open, not fixable in the profile).**
   `fsopen`, `fsconfig`, `fsmount`, `open_tree` and `mount_setattr` create or re-flag mounts without any AppArmor
   mount check; only the final `move_mount` is seen, and the profile has to allow it (systemd 258 and later move
   mounts by file descriptor). Consequences, shown on all four guests by `m9_check.py` (reported as SKIP "known
   open"): root in the guest can mount a fresh `proc` (`fsopen("proc")` and `move_mount`) and write
   `/proc/sys/kernel/core_pattern`; it can `open_tree` a copy of the read-only `/proc/sys`, clear the read-only
   flag with `mount_setattr` and write through it. Arch, Fedora and Kali were affected even through the plain `mount`
   command, because their util-linux uses the new API; with the classic `mount(2)`, a bind mount of `/proc/sys`
   that is remounted read-write works too (the `rw, remount, bind` rule that systemd's sandboxing needs). The
   file rules of `container-base` only protect `/proc/sys/...` at its own path.
   Seccomp was tried twice and is **not shipped**; the failing unit was found (post-M9, Ubuntu 26.04 with systemd 259.5):
   * `fsopen errno 38` and `fspick errno 38` (the compiled profile really returns ENOSYS, checked with a
     `fsopen` call in the guest, and does not fall into the default action): the guest comes up `degraded` with
     `systemd-journald`, `systemd-tmpfiles-setup*`, `systemd-udev-load-credentials` and the journald sockets failed,
     all with `status=243/CREDENTIALS`. Not container-specific units: every unit with `LoadCredential=` or
     `ImportCredential=` fails, because systemd's credential setup does `fsopen("tmpfs")`, `fsconfig` and `fsmount`
     in the unit's exec child (seen with `strace -f` on a private `systemd --user`, PID 1 cannot be traced) and
     treats ENOSYS as fatal; there is no `mount(2)` fallback on that path. So the fallback on ENOSYS that systemd
     has elsewhere does not exist where it matters. Masking those units is not an option.
   * `fsmount errno 38` alone fails the same way (`fsmount(4, ...) = -1 ENOSYS` in the credential setup), and the
     fresh proc needs `fsmount`, so nothing between the two ends can be blocked: a seccomp filter cannot tell
     `fsopen("proc")` from `fsopen("tmpfs")` because the file system name is a user pointer.
   * `mount_setattr errno 38` alone: the guest boots `degraded` (`systemd-logind` and its Varlink socket fail),
     and a unit with `ProtectSystem=strict`, `PrivateTmp=` and the other sandbox options fails to start. It does
     close the `open_tree` clone path (`clone:read-only`), but the `mount(2)` bind of `/proc/sys` remounted
     read-write (`bind-remount:WRITABLE`) stays open, and that rule is the one systemd's own sandboxing needs, so
     blocking `mount_setattr` buys nothing and costs the guests' sandboxes.
   Other ways that were ruled out: a path rule for `core_pattern` and friends (the fresh proc can be mounted
   anywhere and any directory of it can be cloned again, so the path is the attacker's choice), restricting
   `move_mount` (a detached `tmpfs` of the credential setup and a detached `proc` look the same to the profile).
   What would fix it: a user namespace for the guest with a *shifted* id map (`lxc.idmap = u 0 100000 65536`) and
   idmapped binds of the home entries, so that guest root is not host root; sysctl permission compares the kuid with
   the global root, so an identity-mapped namespace (`lxc.idmap = u 0 0 N`) changes nothing. That is a future
   milestone (the shared host network, host PAM and the device model have to be reworked for it). Alternatives are a
   `seccomp` user-notification supervisor that checks the `fsopen` file system name and performs the call itself
   (large, and the guest-visible semantics must be exact), or an LSM that mediates the new mount API (Landlock,
   AppArmor with complete mount hooks). Upstream parity: `systemd-nspawn` without SELinux has the same hole (on
   Fedora the container domain may not write `sysctl_t`, which is what upstream relies on).
   **Accepted risk until the user namespace exists.** `m9_check.py` probes it on every guest (a fresh proc
   writable at `sys/kernel/core_pattern` and `sysrq-trigger`, a writable clone of `/proc/sys`) and reports SKIP
   "known open" while any of them works; it turns into a PASS when they stop working.
   Until then: do not run an untrusted workload as a guest root. Every distro's `sudo` in a space is host-PAM
   authenticated, so a program running as the user needs the host password to become guest root, but a guest
   package script runs as root.

2. **Fine-grained `unix` rules are not enforced on this stack.** Rules such as
   `deny unix peer=(addr="@/run/spaces/lxc/**")` load, but the compiled policy is byte-identical with and without
   them and a connect is never refused (tested with `abi/3.0`, `abi/4.0` and a hand-made ABI that adds
   `network_v9 { af_unix }`; the kernel advertises `network_v9/af_unix` and still does not enforce the rule from
   a policy that the 4.1.7 parser compiles). Only a coarse `deny network unix` works, which would kill the
   guest. So the LXC monitor's command socket (G2) is **not** protected by AppArmor; it is protected by a network
   namespace: `spaces-lxc` runs `lxc-start` in a private one and the guest joins the host's
   (`lxc.namespace.share.net`). See `docs/void.md`.

3. **A profile without an `abi` line is compiled without network mediation.** `deny network inet,` had no
   effect until `abi <abi/4.0>,` was added (the parser falls back to a default feature ABI that the kernel does
   not use for networking). The shipped profile has `network,` (allow all) and no `abi` line, which is consistent;
   but any future network deny rule has to add an `abi` line, and has to be tested with a connection attempt.

4. **`~/.ssh` and `~/.gnupg`: no AppArmor rule is possible or needed; the code is the gate.** Upstream's
   SELinux policy keeps a space away from private keys even if a home entry names them. The earlier plan assumed
   AppArmor `deny` rules for those sources. They cannot exist in this profile: the bind mounts of home
   entries are made by the host (`spaces.host.mountns`, unconfined), so a container profile never sees the
   source; and a path rule on `/home/*/.ssh/**` inside the guest would block the guest's own `~/.ssh`
   (known_hosts, its own keys), which is private to the space. What actually refuses them:
   * `core.validate_home_name` accepts only a single path component or exactly `.ssh/config`;
   * `launch._prepare_mounts` refuses hidden directories (`.ssh`, `.gnupg`, `.config`, ...), nested directories,
     symlinks and sources that resolve outside the home;
   * the discovery of mountable entries never offers hidden directories.
   `tests/test_void_secret_sources.py` pins these (a whole `.ssh` or `.gnupg` is skipped with a warning, nested
   key files fail validation, `.ssh/config` alone works). The gpg-agent *extra* socket and the ssh agent socket are
   different: they are runtime sockets bound deliberately when the space has credential agents enabled.
   This is the only gate; there is no second layer as on Fedora. A mount a user adds by hand through
   `spaces.priv` or a custom bind in `config.json` is under the same trust as the user.

5. **The guest shares the host's network namespace, so every abstract socket of the host is reachable by it.**
   Present on this machine: `@/tmp/.X11-unix/X0` (XWayland, which is the point of sharing it) and the
   private abstract sockets of the guests' own systemd (they are the guests'). The LXC command socket is no
   longer one of them (finding 2). Anything else that listens on an abstract name in the host namespace is
   reachable from a guest: a service that must not be reachable from a guest has to use a filesystem socket.

6. **Signal denials from `tail`** (`signal=exists`, peer `unconfined` or `lxc-attach`) in `dmesg` are
   `kill -0` existence probes from a `tail` in the guest towards a process outside the profile. Harmless.

7. **The profile is loaded at boot with the rest of `/etc/apparmor.d`, and that confines `lxc-start`.** Void's
   `/etc/runit/core-services/09-apparmor.sh` runs `apparmor_parser -a /etc/apparmor.d` at every boot, which loads
   the distribution's `usr.bin.lxc-start` and so confines `/usr/bin/lxc-start` (enforcing). That profile allows
   `change_profile -> lxc-*` and `unconfined` only, and has no `local/` include to extend. The profile used to be
   called `spaces-container`: it worked in every test, none of which had booted with the distro profile loaded,
   and the first launch after a reboot failed (`apparmor="DENIED" operation="change_profile"
   profile="/usr/bin/lxc-start"`). It is now `lxc-spaces-container`, which matches the existing rule and leaves
   the distribution's file untouched. The package INSTALL hook loads it (and drops the old name on an upgrade),
   REMOVE unloads it, the launcher loads it on demand, `spaces-void doctor` fails when `lxc-start` is confined
   by a profile that would refuse it, `tests/test_void_apparmor.py` pins the name against the installed rule,
   and the `bootpath` section of `m9_check.py` unloads both profiles, runs the real boot service and starts a
   space. A reload applies to running containers.

## How to repeat the checks

```
python3 void/spike/m9_check.py            # isolation, lifecycle, AppArmor (a few minutes, starts all four spaces)
python3 void/spike/m9_check.py --regress  # plus m5_check.py and m8_check.py
sudo dmesg | grep 'apparmor="DENIED"'      # what the profile refused
```
