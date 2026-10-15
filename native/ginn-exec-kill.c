/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-exec-kill: the token kill of a mutating exec (ADR-014, FR-123, FR-124, CR-003), run by ginn-exec-run after podman
 * exits and before the exec lock is released. Usage: ginn-exec-kill <realm-cgroup> <token> <marker>, absolute paths.
 * A process of the cgroup tree whose environ holds GINN_EXEC_TOKEN=<token> gets SIGTERM through a pidfd, SIGKILL from the
 * 20th sweep on, every 100 ms for at most 10 s. A survivor gets an orphan record (token, time, pids), GINN-W-RELAY-ORPHAN
 * on stderr and exit 3. Exit 0: none left or no cgroup. Exit 2: usage or euid 0. Exit 1: a system call failed.
 * A --root exec's environ belongs to container root (T716-2), so the helper joins the outermost ancestor of a foreign
 * namespace, when our uid owns it. Without a join the record says `unknown join`; after one, an unreadable process is
 * `unknown <pid>` unless it predates ginn-exec-run, the exec's parent. The marker is opened for append.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/nsfs.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define BOUND_MS 10000
#define KILL_SWEEP 20
#define MAX_PIDS 256
#define MAX_DEPTH 32
/* The text of ginn-exec-frame.h's define, written out because this helper does not include that header (its
 * 60 lines are not counted here). The relay test checks that the two are equal. */
#define GINN_EXEC_W_ORPHAN "GINN-W-RELAY-ORPHAN"
#ifndef SYS_pidfd_open
#define SYS_pidfd_open 434
#endif
#ifndef SYS_pidfd_send_signal
#define SYS_pidfd_send_signal 424
#endif

static const char *token;
static struct stat own;
static long long parent; /* start time of ginn-exec-run, the helper's parent */
static int found[MAX_PIDS], nfound, unknown[MAX_PIDS], nunknown, blind, tried, joined;

static void die(const char *op) { fprintf(stderr, "ginn-exec-kill: %s: %s\n", op, strerror(errno)), exit(1); }
static long long now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000LL + t.tv_nsec / 1000000; }

/* The start time of pid in clock ticks since boot (stat field 22), or -1 when it cannot be read. */
static long long starttime(int pid) {
	char path[64], buf[1024], *p;
	unsigned long long v = 0;
	ssize_t n = -1;
	int fd;
	snprintf(path, sizeof path, "/proc/%d/stat", pid);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd >= 0) n = read(fd, buf, sizeof buf - 1), close(fd);
	if (n <= 0) return -1;
	buf[n] = '\0', p = strrchr(buf, ')');
	return p && sscanf(p + 1, " %*c %*d %*d %*d %*d %*d %*u %*u %*u %*u %*u %*u %*u %*d %*d %*d %*d %*d %*d %llu", &v) == 1 ? (long long)v : -1;
}

/* True when fd names the helper's own user namespace. */
static int own_ns(int fd) { struct stat st; return fstat(fd, &st) == 0 && st.st_ino == own.st_ino && st.st_dev == own.st_dev; }

/* A new fd for the outermost ancestor of fd's namespace whose parent is the helper's own (FR-124). That is the
 * rootless namespace that maps the subuid range, and every container and nested namespace descends from it. */
static int outermost(int fd) {
	int cur = dup(fd), i;
	for (i = 0; cur >= 0 && i < MAX_DEPTH; i++) {
		int up = ioctl(cur, NS_GET_PARENT);
		if (up < 0) break;
		if (own_ns(up)) return close(up), cur;
		close(cur), cur = up;
	}
	if (cur >= 0) close(cur);
	return -1;
}

/* Tries the namespace of pid as the join target. It must be foreign, descend from the helper's namespace and be
 * owned by our uid. The pidfd must still name pid after the namespace is open, so pid is not a reused pid. A
 * candidate that fails a check leaves tried unset, so the next process is tried. */
static void consider(int pid, int pfd) {
	char path[64];
	uid_t owner;
	int fd, target;
	snprintf(path, sizeof path, "/proc/%d/ns/user", pid);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return;
	if (!own_ns(fd) && syscall(SYS_pidfd_send_signal, pfd, 0, NULL, 0) == 0) {
		target = outermost(fd);
		if (target >= 0 && ioctl(target, NS_GET_OWNER_UID, &owner) == 0 && owner == getuid()) {
			tried = 1;
			joined = setns(target, CLONE_NEWUSER) == 0;
		}
		if (target >= 0) close(target);
	}
	close(fd);
}

/* 1 when the environment of pid holds GINN_EXEC_TOKEN=<token> as a whole NUL-terminated entry. 0 when it does not,
 * or the process has gone (ENOENT, ESRCH). -1 when the environ cannot be read, so the process is unknown. */
static int tokened(int pid, int pfd) {
	static char buf[65536], entry[64];
	char path[64];
	ssize_t n;
	char *p;
	int fd, hit = 0;
	if (!tried) consider(pid, pfd);
	snprintf(path, sizeof path, "/proc/%d/environ", pid);
	snprintf(entry, sizeof entry, "GINN_EXEC_TOKEN=%s", token);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return errno == ENOENT || errno == ESRCH ? 0 : -1; /* ESRCH: a zombie has no environ */
	n = read(fd, buf, sizeof buf - 1);
	close(fd);
	if (n < 0) return -1;
	if (n == 0 || (syscall(SYS_pidfd_send_signal, pfd, 0, NULL, 0) < 0 && errno == ESRCH)) return 0;
	buf[n] = '\0';
	for (p = buf; p < buf + n; p += strlen(p) + 1)
		if (!strcmp(p, entry)) hit = 1;
	return hit;
}

/* One sweep of dir and its subtree: each tokened process gets sig through its pidfd. Returns the tokened count.
 * An unreadable process counts in blind before a join. After one, it is unknown unless it predates the parent. */
static int sweep(const char *dir, int sig) {
	char path[4096], line[32];
	struct dirent *e;
	FILE *f;
	DIR *d;
	int n = 0;
	snprintf(path, sizeof path, "%s/cgroup.procs", dir);
	f = fopen(path, "r");
	if (f) {
		while (fgets(line, sizeof line, f)) {
			int pid = atoi(line), pfd, t;
			if (pid <= 0) continue;
			pfd = (int)syscall(SYS_pidfd_open, pid, 0);
			if (pfd < 0) continue;
			t = tokened(pid, pfd);
			if (t > 0) {
				if (nfound < MAX_PIDS) found[nfound++] = pid;
				syscall(SYS_pidfd_send_signal, pfd, sig, NULL, 0);
				n++;
			} else if (t < 0 && !joined) {
				blind++;
			} else if (t < 0) {
				long long st = starttime(pid);
				if ((st < 0 || parent < 0 || st >= parent) && nunknown < MAX_PIDS) unknown[nunknown++] = pid;
			}
			close(pfd);
		}
		fclose(f);
	}
	d = opendir(dir);
	if (!d && errno != ENOENT) die("opendir");
	if (!d) return n;
	while ((e = readdir(d)))
		if (e->d_type == DT_DIR && strcmp(e->d_name, ".") && strcmp(e->d_name, "..")) {
			snprintf(path, sizeof path, "%s/%s", dir, e->d_name);
			n += sweep(path, sig);
		}
	closedir(d);
	return n;
}

int main(int argc, char **argv) {
	const struct timespec tick = {0, 100000000};
	long long t0 = now_ms();
	char buf[16384];
	int round = 0, n, fd, i, len;
	if (geteuid() == 0) return fprintf(stderr, "ginn-exec-kill: refuses to run as root (CR-003)\n"), 2;
	if (argc != 4 || argv[1][0] != '/' || argv[3][0] != '/')
		return fprintf(stderr, "usage: ginn-exec-kill <realm-cgroup> <token> <orphan-marker>\n"), 2;
	token = argv[2];
	if (stat("/proc/self/ns/user", &own) < 0) die("stat");
	parent = starttime(getppid());
	for (;;) {
		nfound = nunknown = blind = 0;
		n = sweep(argv[1], round < KILL_SWEEP ? SIGTERM : SIGKILL);
		if (n == 0 || now_ms() - t0 >= BOUND_MS) break;
		round++;
		nanosleep(&tick, NULL);
	}
	if (!nfound && !nunknown && !blind) return 0;
	len = snprintf(buf, sizeof buf, "token %s\ntime %lld\n", token, (long long)time(NULL));
	for (i = 0; i < nfound; i++) len += snprintf(buf + len, sizeof buf - len, "pid %d\n", found[i]);
	if (blind) len += snprintf(buf + len, sizeof buf - len, "unknown join\n");
	for (i = 0; i < nunknown; i++) len += snprintf(buf + len, sizeof buf - len, "unknown %d\n", unknown[i]);
	fd = open(argv[3], O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600);
	if (fd >= 0) {
		if (write(fd, buf, len) != len) fprintf(stderr, "ginn-exec-kill: the marker record was not written whole\n");
		close(fd);
	}
	if (nfound) fprintf(stderr, "%s: %d processes of the exec survived the token kill\n", GINN_EXEC_W_ORPHAN, nfound);
	if (blind) fprintf(stderr, "%s: the realm's processes could not be read for their token; left running\n", GINN_EXEC_W_ORPHAN);
	else if (nunknown) fprintf(stderr, "%s: %d processes whose token could not be read were left running\n", GINN_EXEC_W_ORPHAN, nunknown);
	return 3;
}
