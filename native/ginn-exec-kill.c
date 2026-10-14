/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-exec-kill: the token kill of a mutating exec (ADR-014, FR-123, FR-124). ginn-exec-run runs it
 * after podman has exited and before the exec lock is released. Usage: ginn-exec-kill <realm-cgroup>
 * <token> <orphan-marker>, absolute paths. It walks the cgroup tree under <realm-cgroup>. For each
 * process whose environment holds GINN_EXEC_TOKEN=<token> as a whole entry, it sends SIGTERM through a
 * pidfd, and SIGKILL from the 20th sweep on. It sweeps every 100 ms until none is left or 10 s pass. A
 * survivor gets the orphan marker (token, time, pids), GINN-W-RELAY-ORPHAN on stderr, and exit 3. Exit
 * 0: none left, or no cgroup. Exit 2: usage. Exit 1: a system call failed.
 * The marker is opened for append, so each survivor run adds one record and keeps the records of
 * earlier execs that no ginn command has settled yet.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>
#include "ginn-exec-frame.h"

#define BOUND_MS 10000
#define KILL_SWEEP 20
#define MAX_PIDS 256
#ifndef SYS_pidfd_open
#define SYS_pidfd_open 434
#endif
#ifndef SYS_pidfd_send_signal
#define SYS_pidfd_send_signal 424
#endif

static const char *token;
static int found[MAX_PIDS], nfound;

static void die(const char *op) {
	fprintf(stderr, "ginn-exec-kill: %s: %s\n", op, strerror(errno));
	exit(1);
}

static long long now_ms(void) {
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1000LL + t.tv_nsec / 1000000;
}

/* True when the environment of pid holds GINN_EXEC_TOKEN=<token> as a whole NUL-terminated entry. */
static int tokened(int pid) {
	static char buf[65536], entry[64];
	char path[64];
	ssize_t n;
	char *p;
	int fd, hit = 0;
	snprintf(path, sizeof path, "/proc/%d/environ", pid);
	snprintf(entry, sizeof entry, "GINN_EXEC_TOKEN=%s", token);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return 0;
	n = read(fd, buf, sizeof buf - 1);
	close(fd);
	if (n <= 0) return 0;
	buf[n] = '\0';
	for (p = buf; p < buf + n; p += strlen(p) + 1)
		if (!strcmp(p, entry)) hit = 1;
	return hit;
}

/* One sweep of dir and its subtree: each tokened process gets sig through its pidfd. Returns the count. */
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
			int pid = atoi(line), pfd;
			if (pid <= 0) continue;
			pfd = (int)syscall(SYS_pidfd_open, pid, 0);
			if (pfd < 0) continue;
			if (tokened(pid)) {
				if (nfound < MAX_PIDS) found[nfound++] = pid;
				syscall(SYS_pidfd_send_signal, pfd, sig, NULL, 0);
				n++;
			}
			close(pfd);
		}
		fclose(f);
	}
	d = opendir(dir);
	if (!d) {
		if (errno == ENOENT) return n;
		die("opendir");
	}
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
	char buf[8192];
	int round = 0, n, fd, i, len;
	if (argc != 4 || argv[1][0] != '/' || argv[3][0] != '/') {
		fprintf(stderr, "usage: ginn-exec-kill <realm-cgroup> <token> <orphan-marker>\n");
		return 2;
	}
	token = argv[2];
	for (;;) {
		nfound = 0;
		n = sweep(argv[1], round < KILL_SWEEP ? SIGTERM : SIGKILL);
		if (n == 0) return 0;
		if (now_ms() - t0 >= BOUND_MS) break;
		round++;
		nanosleep(&tick, NULL);
	}
	len = snprintf(buf, sizeof buf, "token %s\ntime %lld\n", token, (long long)time(NULL));
	for (i = 0; i < nfound; i++) len += snprintf(buf + len, sizeof buf - len, "pid %d\n", found[i]);
	fd = open(argv[3], O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600);
	if (fd >= 0) {
		if (write(fd, buf, len) != len) fprintf(stderr, "ginn-exec-kill: the marker record was not written whole\n");
		close(fd);
	}
	fprintf(stderr, "%s: %d processes of the exec survived the token kill\n", GINN_EXEC_W_ORPHAN, nfound);
	return 3;
}
