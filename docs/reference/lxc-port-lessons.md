# Notes from the earlier LXC port

Newly written notes for ginn. Each point is a host or runtime fact found in an earlier LXC-based attempt, with its consequence for ginn. No text comes from the earlier source.

## elogind adds a v1 hierarchy

On a runit host with elogind, cgroup v2 is mounted with an extra named v1 hierarchy, `name=elogind`. Tools that assume a pure v2 setup get confused by it. ginn must read the real layout in `ginn doctor` (FR-007) and must not assume pure v2.

## Stock Void does not delegate cgroups

On a stock Void install, the root `cgroup.subtree_control` is empty. Rootless Podman then reports the `cgroupfs` manager and no controllers. Host Setup must delegate a cgroup subtree to the user (R-001, FR-001; task T3.2).

## AppArmor denied nested overlay mounts

The earlier attempt found that AppArmor denied the overlay mounts that nested Podman needs. Record the AppArmor state of the test host in the S-07 result (FR-062 AC-3).

## User-namespace base 100000 collided with subuids

The earlier attempt used a user-namespace shift base of 100000. Rootless Podman uses that same range for subordinate UIDs, so the two collided. ginn allocates its own range of at least 524288 IDs (AS-005). The range must not overlap the entries already in `/etc/subuid` or `/etc/subgid` (task T3.1).

## Realm helpers need an old glibc

C helpers copied into a realm must be built against an old glibc baseline. Otherwise they must run on the host only. The planned `native/` helpers all run on the host. On a musl host, helpers built against the host libc cannot run inside glibc realms; helpers therefore stay host-side or are built against a glibc baseline.
