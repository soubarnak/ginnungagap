/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-rebind: attach host grants into a realm's mount namespace (ADR-008). The host runs it, not
 * `podman unshare`: it joins the realm's user namespace, copies the host mount table (unshare
 * CLONE_NEWNS), clones each source from that copy without following a symbolic link, joins the
 * realm's mount namespace and moves the clones in. Groups: [--force] [--ro] <host> <realm> |
 * --unmount <realm>. stdout per group: "<i> attached <dev> <ino>", "<i> skipped", "<i> missing".
 * Exit 0 ok, 2 usage, 1 failure. Needs Linux 5.12+ (mount_setattr, openat2).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SYS_open_tree
#define SYS_open_tree 428
#endif
#ifndef SYS_move_mount
#define SYS_move_mount 429
#endif
#ifndef SYS_openat2
#define SYS_openat2 437
#endif
#ifndef SYS_mount_setattr
#define SYS_mount_setattr 442
#endif

enum {
	G_TREE_CLONE = 0x1, G_AT_RECURSIVE = 0x8000, G_AT_EMPTY_PATH = 0x1000, G_ATTR_RDONLY = 0x1,
	G_MOVE_FROM_EMPTY = 0x4, G_MOVE_TO_EMPTY = 0x40, G_RESOLVE_NO_SYMLINKS = 0x4,
	G_MNT_DETACH = 0x2, G_UMOUNT_NOFOLLOW = 0x8, G_UMOUNT_MAX = 32
};

struct ginn_mount_attr { uint64_t attr_set, attr_clr, propagation, userns_fd; };
struct ginn_open_how { uint64_t flags, mode, resolve; };
struct grant { const char *src, *dst; int force, ro, unmount, dir, tree; dev_t dev; ino_t ino; };

static void usage(void) {
	fprintf(stderr, "usage: ginn-rebind <init-pid> { [--force] [--ro] <host> <realm> | --unmount <realm> }...\n");
	exit(2);
}

static void fail(int code, const char *op, const char *path) {
	fprintf(stderr, "ginn-rebind: %s %s: %s\n", op, path, strerror(errno));
	exit(code);
}

/* An absolute path with no ".." component: a group may not name a parent of its own directory. */
static int valid(const char *p) {
	size_t n = strlen(p);
	return p[0] == '/' && !strstr(p, "/../") && (n < 3 || strcmp(p + n - 3, "/.."));
}

/* openat2 of `name` in `dfd`, refusing any symbolic link on the way. -1 with errno on failure. */
static int open_nolink(int dfd, const char *name, int flags) {
	struct ginn_open_how how = { (uint64_t)flags, 0, G_RESOLVE_NO_SYMLINKS };
	return (int)syscall(SYS_openat2, dfd, name, &how, sizeof how);
}

/* The directory of `dst`, with no symbolic link on the way. The last name goes to *base. */
static int parent_fd(const char *dst, const char **base) {
	static char dir[PATH_MAX];
	const char *slash = strrchr(dst, '/');
	size_t n = slash == dst ? 1 : (size_t)(slash - dst);

	*base = slash + 1;
	if (n >= sizeof dir || **base == '\0' || !strcmp(*base, ".")) { errno = EINVAL; return -1; }
	memcpy(dir, dst, n);
	dir[n] = '\0';
	return open_nolink(AT_FDCWD, dir, O_PATH | O_DIRECTORY | O_CLOEXEC);
}

/* Make the missing placeholder that `name` in `dfd` mounts over: a directory, or an empty file. */
static void make_placeholder(int dfd, const char *name, const char *dst, int dir) {
	int fd = -1;

	if (dir && mkdirat(dfd, name, 0755) < 0 && errno != EEXIST) fail(1, "mkdir", dst);
	if (!dir && (fd = openat(dfd, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644)) < 0
	    && errno != EEXIST)
		fail(1, "create", dst);
	close(fd);
}

/* Unmount whatever is stacked on `name` until EINVAL. Always MNT_DETACH: an open descriptor on the
 * mount gives EBUSY under plain umount2 (ADR-008, T4.9 section 4.1). The cwd is `dfd`. */
static void detach(int dfd, const char *name, const char *dst) {
	if (fchdir(dfd) < 0) fail(1, "fchdir", dst);
	for (int i = 0; i < G_UMOUNT_MAX; i++) {
		if (umount2(name, G_UMOUNT_NOFOLLOW | G_MNT_DETACH) == 0) continue;
		if (errno == EINVAL) return;
		fail(1, "umount2", dst);
	}
	errno = EBUSY;
	fail(1, "umount2", dst);
}

int main(int argc, char **argv) {
	char *end = NULL, nsp[64];
	long pid;
	struct grant *g;
	struct stat st, me;
	struct ginn_mount_attr attr = { .attr_set = G_ATTR_RDONLY };
	int n = 0, nsfd, usfd, pfd;

	if (argc < 4) usage();
	errno = 0;
	pid = strtol(argv[1], &end, 10);
	if (errno || *end || pid < 2 || pid > INT_MAX) usage();
	if (!(g = calloc((size_t)argc, sizeof *g))) { fprintf(stderr, "ginn-rebind: no memory\n"); return 1; }
	for (int i = 2; i < argc; n++) {
		struct grant *x = &g[n];
		for (; i < argc && argv[i][0] == '-'; i++) {
			if (x->unmount) usage();  /* --unmount takes no other flag, and no repeat */
			if (!strcmp(argv[i], "--force")) x->force = 1;
			else if (!strcmp(argv[i], "--ro")) x->ro = 1;
			else if (!strcmp(argv[i], "--unmount") && !x->force && !x->ro) x->unmount = 1;
			else usage();
		}
		if (x->unmount) {
			if (i >= argc || !valid(argv[i])) usage();
			x->dst = argv[i++];
			x->tree = -1;
		} else {
			if (i + 2 > argc || !valid(argv[i]) || !valid(argv[i + 1])) usage();
			x->src = argv[i];
			x->dst = argv[i + 1];
			i += 2;
		}
	}

	/* Pin the realm's init process by one lookup of /proc/<pid>, and open both namespaces from that
	 * fd. Join its user namespace (skipped when ours already), then copy the mount table of the
	 * caller's namespace: a host mount made after the realm started is in that copy. */
	snprintf(nsp, sizeof nsp, "/proc/%ld", pid);
	if ((pfd = open(nsp, O_PATH | O_DIRECTORY | O_CLOEXEC)) < 0) fail(1, "open", nsp);
	if ((nsfd = openat(pfd, "ns/mnt", O_RDONLY | O_CLOEXEC)) < 0) fail(1, "open", "ns/mnt");
	if ((usfd = openat(pfd, "ns/user", O_RDONLY | O_CLOEXEC)) < 0) fail(1, "open", "ns/user");
	close(pfd); if (fstat(usfd, &st) < 0 || stat("/proc/self/ns/user", &me) < 0) fail(1, "stat", "ns/user");
	if (st.st_ino != me.st_ino && setns(usfd, CLONE_NEWUSER) < 0) fail(1, "setns", "ns/user");
	close(usfd); if (unshare(CLONE_NEWNS) < 0) fail(1, "unshare", "CLONE_NEWNS");
	for (int k = 0; k < n; k++) {
		struct grant *x = &g[k];
		int sfd;
		if (x->unmount) continue;
		/* The source is opened with no symbolic link on the way: the caller passes resolved paths. */
		sfd = open_nolink(AT_FDCWD, x->src, O_PATH | O_CLOEXEC);
		x->tree = sfd < 0 ? -1 : (int)syscall(SYS_open_tree, sfd, "", G_TREE_CLONE | G_AT_EMPTY_PATH | G_AT_RECURSIVE | O_CLOEXEC);
		if (sfd >= 0) close(sfd);
		if (x->tree < 0) { if (errno != ENOENT) fail(1, "open_tree", x->src); continue; }
		if (fstat(x->tree, &st) < 0) fail(1, "fstat", x->src);
		x->dev = st.st_dev; x->ino = st.st_ino;
		x->dir = S_ISDIR(st.st_mode) != 0;
		if (x->ro && syscall(SYS_mount_setattr, x->tree, "", G_AT_EMPTY_PATH | G_AT_RECURSIVE,
				     &attr, sizeof attr) < 0)
			fail(1, "mount_setattr", x->src);
	}

	/* One setns for the whole batch: the realm's mount namespace (ADR-008, variant c). */
	if (setns(nsfd, CLONE_NEWNS) < 0) fail(1, "setns", nsp);
	close(nsfd);

	for (int k = 0; k < n; k++) {
		struct grant *x = &g[k];
		const char *base = NULL;
		int dfd = parent_fd(x->dst, &base), t = -1;

		if (dfd < 0 && errno != ENOENT) fail(1, "open parent", x->dst);
		if (dfd >= 0 && (t = open_nolink(dfd, base, O_PATH | O_CLOEXEC)) < 0 && errno != ENOENT)
			fail(1, "open target", x->dst);
		if (x->tree < 0) {
			if ((x->force || x->unmount) && t >= 0) detach(dfd, base, x->dst);
			printf("%d missing\n", k);
		} else if (t >= 0 && !x->force && fstat(t, &st) == 0 &&
			   st.st_dev == x->dev && st.st_ino == x->ino) {
			printf("%d skipped\n", k);
		} else {
			if (dfd < 0) fail(1, "mountpoint parent", x->dst);
			if (t < 0) make_placeholder(dfd, base, x->dst, x->dir);
			close(t);
			detach(dfd, base, x->dst);
			t = open_nolink(dfd, base, O_PATH | O_CLOEXEC);
			if (t < 0) fail(1, "open target", x->dst);
			if (syscall(SYS_move_mount, x->tree, "", t, "", G_MOVE_FROM_EMPTY | G_MOVE_TO_EMPTY) < 0)
				fail(1, "move_mount", x->dst);
			printf("%d attached %lu %lu\n", k, (unsigned long)x->dev, (unsigned long)x->ino);
		}
		close(t); close(dfd); close(x->tree);
	}
	free(g);
	return 0;
}
