/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-exec-kill: the token kill of a mutating exec (ADR-014, FR-123, FR-124, CR-003), run by ginn-exec-run after podman
 * exits. Usage: ginn-exec-kill <realm-cgroup> <token> <marker>. A process whose environ holds GINN_EXEC_TOKEN=<token> gets
 * SIGTERM through a pidfd, SIGKILL from the 20th sweep, every 100 ms for at most 10 s. Gone means its pidfd is readable.
 * Exit 0: none left. Exit 2: usage or euid 0. Exit 1: a system call failed. Exit 3: a survivor or an unread process, in the
 * orphan marker (token, time, pids) and GINN-W-RELAY-ORPHAN on stderr. A --root exec's environ belongs to container root
 * (T716-2), so the helper joins the outermost ancestor namespace; ADR-014 has the join and unknown-process rules.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/nsfs.h>
#include <poll.h>
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

enum { BOUND_MS = 10000, KILL_SWEEP = 20, MAX_PIDS = 256, MAX_DEPTH = 32, MAX_MISS = 8, PENDING = -2 }; /* PENDING: exiting */
/* Written out here, not taken from ginn-exec-frame.h, whose lines are not counted here. */
#define GINN_EXEC_W_ORPHAN "GINN-W-RELAY-ORPHAN"
#ifndef SYS_pidfd_open
#define SYS_pidfd_open 434
#endif
#ifndef SYS_pidfd_send_signal
#define SYS_pidfd_send_signal 424
#endif

static const char *token, *fstep; static char envbuf[65536], entry[64]; /* last environ read; GINN_EXEC_TOKEN=<token> */
static struct stat own; static long long parent; /* start time of ginn-exec-run, the helper's parent */
static int found[MAX_PIDS], nfound, unknown[MAX_PIDS], nunknown, blind, tried, joined, nmiss;
static int ferr, jerr; static char jmsg[128]; /* errno of the failed environ step and of a failed join; the join's text */
static struct { int pid, err; const char *step; } miss[MAX_MISS]; /* unread processes: pid, failed step, errno */

static void die(const char *op) { fprintf(stderr, "ginn-exec-kill: %s: %s\n", op, strerror(errno)), exit(1); }
static long long now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000LL + t.tv_nsec / 1000000; }

/* The start time of pid in clock ticks since boot (stat field 22), or -1 when it cannot be read. */
static long long starttime(int pid) {
	char path[64], buf[1024], *p; unsigned long long v = 0; ssize_t n = -1; int fd;
	snprintf(path, sizeof path, "/proc/%d/stat", pid), fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd >= 0) n = read(fd, buf, sizeof buf - 1), close(fd);
	if (n <= 0) return -1;
	buf[n] = '\0', p = strrchr(buf, ')'); return p && sscanf(p + 1, " %*c %*d %*d %*d %*d %*d %*u %*u %*u %*u %*u %*u %*u %*d %*d %*d %*d %*d %*d %llu", &v) == 1 ? (long long)v : -1;
}

/* Gone: pfd is readable, which the kernel does only once the whole thread group has exited (FR-124). errno is kept. */
static int gone(int pfd) { int e = errno, r = poll(&(struct pollfd){pfd, POLLIN, 0}, 1, 0) == 1; errno = e; return r; }
/* True when fd names the helper's own user namespace. */
static int own_ns(int fd) { struct stat st; return fstat(fd, &st) == 0 && st.st_ino == own.st_ino && st.st_dev == own.st_dev; }

/* A new fd for the outermost ancestor of fd's namespace whose parent is the helper's own: the rootless one (FR-124). */
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

/* Tries the namespace of pid as the join target: foreign, under the helper's namespace, owned by our uid (FR-124). */
static void consider(int pid, int pfd) {
	char path[64];
	uid_t owner; int fd, target;
	snprintf(path, sizeof path, "/proc/%d/ns/user", pid);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return;
	if (!own_ns(fd) && syscall(SYS_pidfd_send_signal, pfd, 0, NULL, 0) == 0) {
		target = outermost(fd);
		if (target >= 0 && ioctl(target, NS_GET_OWNER_UID, &owner) == 0 && owner == getuid()) {
			tried = 1;
			joined = setns(target, CLONE_NEWUSER) == 0;
			if (!joined) jerr = errno, snprintf(jmsg, sizeof jmsg, "setns above pid %d: %s (errno %d)", pid, strerror(jerr), jerr);
		}
		if (target >= 0) close(target);
	}
	close(fd);
}
/* Reads the environ at path into envbuf: its length, or -1 with the failed step and its errno (FR-124). */
static ssize_t env_read(const char *path, const char **step, int *err) {
	ssize_t n; int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return *step = "open environ", *err = errno, -1;
	n = read(fd, envbuf, sizeof envbuf - 1);
	if (n < 0) *step = "read environ", *err = errno;
	return close(fd), n;
}
/* The environ of the first readable thread other than the leader, which shares the leader's mm (FR-124). */
static ssize_t task_env(int pid, int *others, const char **step, int *err) {
	char dir[64], path[128]; struct dirent *e; DIR *d; ssize_t n = -1;
	snprintf(dir, sizeof dir, "/proc/%d/task", pid), d = opendir(dir);
	if (!d) return -1;
	while ((e = readdir(d))) {
		int tid = atoi(e->d_name);
		if (tid <= 0 || tid == pid) continue;
		(*others)++;
		if (n < 0) snprintf(path, sizeof path, "%s/%d/environ", dir, tid), n = env_read(path, step, err);
	}
	closedir(d);
	return n;
}

/* 1 when pid's environ holds the token entry, 0 when not or gone, PENDING when exiting (see below), -1 when unread. */
static int tokened(int pid, int pfd) {
	char path[64], *p; const char *step = "open environ"; ssize_t n; int err = 0, others = 0, hit = 0;
	if (gone(pfd)) return 0; /* checked before the read, so a zombie's environ is never read (FR-124) */
	if (!tried) consider(pid, pfd);
	snprintf(path, sizeof path, "/proc/%d/environ", pid);
	n = env_read(path, &step, &err);
	if (n < 0 && err != ESRCH) return gone(pfd) ? 0 : (ferr = err, fstep = step, -1);
	if (n <= 0) { /* ESRCH: a zombie leader or an exiting process. Empty: possibly a lone env -i process */
		ssize_t m = task_env(pid, &others, &step, &err);
		if (m < 0) return n == 0 && !others ? 0 : gone(pfd) ? 0 : (ferr = err, fstep = step, PENDING);
		n = m;
	}
	envbuf[n] = '\0';
	for (p = envbuf; p < envbuf + n; p += strlen(p) + 1)
		if (!strcmp(p, entry)) hit = 1;
	return hit;
}
static void note(int pid) { if (nmiss < MAX_MISS) miss[nmiss].pid = pid, miss[nmiss].err = ferr, miss[nmiss++].step = fstep; }
/* One sweep of dir and its subtree: tokened processes get sig through their pidfd. Returns those plus the pending ones,
 * so the loop sweeps again. Unread processes count in blind before a join, and after one they are unknown unless old. */
static int sweep(const char *dir, int sig) {
	char path[4096], line[32]; struct dirent *e; FILE *f; DIR *d; int n = 0;
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
			} else if (t < 0) {
				long long st = starttime(pid);
				n += t == PENDING;
				if (!joined) blind++, note(pid);
				else if ((st < 0 || parent < 0 || st >= parent) && nunknown < MAX_PIDS) unknown[nunknown++] = pid, note(pid);
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
	const struct timespec tick = {0, 100000000}; long long t0 = now_ms();
	char buf[16384]; int round = 0, n, fd, i, len;
	if (geteuid() == 0) return fprintf(stderr, "ginn-exec-kill: refuses to run as root (CR-003)\n"), 2;
	if (argc != 4 || argv[1][0] != '/' || argv[3][0] != '/') return fprintf(stderr, "usage: ginn-exec-kill <realm-cgroup> <token> <orphan-marker>\n"), 2;
	token = argv[2], snprintf(entry, sizeof entry, "GINN_EXEC_TOKEN=%s", token);
	if (stat("/proc/self/ns/user", &own) < 0) die("stat");
	parent = starttime(getppid());
	for (;;) {
		nfound = nunknown = blind = nmiss = 0;
		n = sweep(argv[1], round < KILL_SWEEP ? SIGTERM : SIGKILL);
		if (n == 0 || now_ms() - t0 >= BOUND_MS) break;
		round++, nanosleep(&tick, NULL);
	}
	if (!nfound && !nunknown && !blind) return 0;
	len = snprintf(buf, sizeof buf, "token %s\ntime %lld\n", token, (long long)time(NULL));
	for (i = 0; i < nfound; i++) len += snprintf(buf + len, sizeof buf - len, "pid %d\n", found[i]);
	if (blind) len += snprintf(buf + len, sizeof buf - len, "unknown join\n");
	for (i = 0; i < nunknown; i++) len += snprintf(buf + len, sizeof buf - len, "unknown %d\n", unknown[i]);
	if ((fd = open(argv[3], O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600)) >= 0) { if (write(fd, buf, len) != len) fprintf(stderr, "ginn-exec-kill: the marker record was not written whole\n"); close(fd); }
	if (nfound) fprintf(stderr, "%s: %d processes of the exec survived the token kill\n", GINN_EXEC_W_ORPHAN, nfound);
	if (blind) fprintf(stderr, "%s: the realm's processes could not be read for their token; left running\n", GINN_EXEC_W_ORPHAN);
	if (nunknown) fprintf(stderr, "%s: %d processes whose token could not be read were left running\n", GINN_EXEC_W_ORPHAN, nunknown);
	/* The first MAX_MISS unread processes with their step and errno (FR-124), then a count and the join's errno. */
	for (i = 0; i < nmiss; i++) fprintf(stderr, "  pid %d: %s: %s (errno %d)\n", miss[i].pid, miss[i].step, strerror(miss[i].err), miss[i].err);
	if (blind + nunknown > nmiss) fprintf(stderr, "  and %d more unread processes\n", blind + nunknown - nmiss);
	if (blind && !joined) fprintf(stderr, "  join: %s\n", tried ? jmsg : "not attempted, no realm process is in a joinable user namespace");
	return 3;
}
