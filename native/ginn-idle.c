/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-idle: stop a services-off realm 60 s after its last user process exits (FR-018, NFR-002).
 * <init-pid> is the realm's init (catatonit) on the host. A user process is a live process in the
 * init's pid namespace other than the init, matched by the device and inode of /proc/<pid>/ns/pid.
 * The count runs once a second. At zero it prints "armed" and the grace runs on CLOCK_MONOTONIC.
 * A user process before its end prints "cancelled". At the end, with zero users and the init
 * unchanged, `<podman> [global-arg...] stop <container>` runs as an argv (no shell) and the helper
 * exits 0. The launcher starts it right after the realm, so a realm with no user process is armed.
 * Usage: ginn-idle <init-pid> <grace-seconds> <podman> [global-arg...] -- <container>, with
 * absolute paths and grace 1 to 3600. stdout: "watching <pid> <start>", then "armed" or
 * "cancelled". Exit: 0 stopped, init gone, or SIGTERM (a stop in flight finishes first); 2 usage;
 * 3 init not running at start, or a /proc error; 4 podman stop failed while the init runs; 5 system
 * call failed. The init is checked each tick by start time (field 22) and state, so a reused pid
 * is never acted on.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/signalfd.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Fields 4 to 21 of /proc/<pid>/stat, skipped by sscanf before starttime (field 22). */
#define SKIP18 "%*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s %*s"

static pid_t init_pid;
static unsigned long long init_start; /* /proc/<pid>/stat field 22: clock ticks since boot */
static dev_t init_dev;                /* the init's pid namespace */
static ino_t init_ino;

static void usage(void) {
	fprintf(stderr, "usage: ginn-idle <init-pid> <grace-seconds> <podman> [global-arg...]"
	                " -- <container>\n");
	exit(2);
}

static void fail(int code, const char *op, const char *what) {
	fprintf(stderr, "ginn-idle: %s %s: %s\n", op, what, strerror(errno));
	exit(code);
}

/* A container name: 1 to 255 of [A-Za-z0-9_.-], not starting with '-' (an option to podman). */
static int valid_name(const char *s) {
	size_t i;
	for (i = 0; s[i]; i++)
		if (!isalnum((unsigned char)s[i]) && !strchr("_.", s[i]) && !(s[i] == '-' && i > 0)) return 0;
	return i > 0 && i <= 255;
}

/* A decimal number in [min, max], or -1. */
static long parse_num(const char *s, long min, long max) {
	char *end;
	long v;
	if (!isdigit((unsigned char)s[0])) return -1;
	errno = 0;
	v = strtol(s, &end, 10);
	return errno || *end || v < min || v > max ? -1 : v;
}

/* State letter (field 3) and start time (field 22) of a process. -1 when /proc has no answer. */
static int proc_stat(pid_t pid, char *state, unsigned long long *start) {
	char path[64], buf[1024], *p;
	ssize_t n;
	int fd;
	snprintf(path, sizeof path, "/proc/%ld/stat", (long)pid);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return -1;
	do n = read(fd, buf, sizeof buf - 1); while (n < 0 && errno == EINTR);
	close(fd);
	if (n <= 0) return -1;
	buf[n] = '\0';
	p = strrchr(buf, ')'); /* the comm name in parentheses has no ')' after it */
	return p && sscanf(p, ") %c " SKIP18 " %llu", state, start) == 2 ? 0 : -1;
}

/* The init is gone when its start time changed (a reused pid), it has exited, or it is a zombie. */
static int gone(void) {
	char state = 0;
	unsigned long long start = 0;
	return proc_stat(init_pid, &state, &start) < 0 || start != init_start || state == 'Z' ||
	       state == 'X';
}

/* stat follows /proc/<pid>/ns/pid to the pid namespace inode. */
static int ns_of(pid_t pid, struct stat *st) {
	char path[64];
	snprintf(path, sizeof path, "/proc/%ld/ns/pid", (long)pid);
	return stat(path, st);
}

/* Live processes in the init's pid namespace, other than the init. Those that vanish during the
 * scan, or whose ns link is unreadable, are not counted. -1 when /proc cannot be opened. */
static int users(void) {
	DIR *d = opendir("/proc");
	struct dirent *e;
	int n = 0;
	if (!d) return -1;
	while ((e = readdir(d))) {
		char state = 0;
		unsigned long long start = 0;
		struct stat st;
		long v = parse_num(e->d_name, 1, INT_MAX);
		if (v < 0 || (pid_t)v == init_pid || ns_of((pid_t)v, &st) < 0) continue;
		if (st.st_dev != init_dev || st.st_ino != init_ino) continue;
		if (proc_stat((pid_t)v, &state, &start) == 0 && state != 'Z' && state != 'X') n++;
	}
	closedir(d);
	return n;
}

/* Sleeps up to a second, or until SIGTERM (sets *stop). Returns gone(). */
static int nap(int sigfd, int *stop) {
	struct pollfd p = {sigfd, POLLIN, 0};
	struct signalfd_siginfo si;
	if (poll(&p, 1, 1000) < 0 && errno != EINTR) fail(5, "poll", "signalfd");
	while (p.revents && read(sigfd, &si, sizeof si) > 0) *stop = 1;
	return gone();
}

/* Run `<podman> [global-arg...] stop <container>` (cmd is argv + 3). Its stdout goes to stderr. */
static int stop_container(char **cmd) {
	sigset_t none;
	pid_t pid;
	int status = 0;
	sigemptyset(&none);
	if ((pid = fork()) < 0) fail(5, "fork", cmd[0]);
	if (pid == 0) {
		sigprocmask(SIG_SETMASK, &none, NULL); /* SIGTERM stays blocked in ginn-idle only */
		dup2(STDERR_FILENO, STDOUT_FILENO);
		execv(cmd[0], cmd);
		_exit(127);
	}
	while (waitpid(pid, &status, 0) < 0) if (errno != EINTR) fail(5, "waitpid", cmd[0]);
	return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

int main(int argc, char **argv) {
	struct stat st;
	struct timespec ts;
	sigset_t set;
	char state = 0;
	long pid, grace;
	long long since = -1, now;
	int sigfd, stop = 0, n;

	setvbuf(stdout, NULL, _IOLBF, BUFSIZ); /* each transition line goes out as it is written */
	if (argc < 6 || strcmp(argv[argc - 2], "--") || argv[3][0] != '/' ||
	    !valid_name(argv[argc - 1]))
		usage();
	pid = parse_num(argv[1], 1, INT_MAX);
	grace = parse_num(argv[2], 1, 3600);
	if (pid < 0 || grace < 0) usage();
	argv[argc - 2] = "stop"; /* argv + 3 is now: <podman> [global-arg...] stop <container> */
	init_pid = (pid_t)pid;

	sigemptyset(&set);
	sigaddset(&set, SIGTERM);
	if (sigprocmask(SIG_BLOCK, &set, NULL) < 0) fail(5, "sigprocmask", "SIGTERM");
	sigfd = signalfd(-1, &set, SFD_CLOEXEC | SFD_NONBLOCK);
	if (sigfd < 0) fail(5, "signalfd", "SIGTERM");
	if (proc_stat(init_pid, &state, &init_start) < 0 || state == 'Z' || state == 'X') {
		fprintf(stderr, "ginn-idle: pid %ld is not running\n", pid);
		return 3;
	}
	if (ns_of(init_pid, &st) < 0) fail(3, "stat", "the init's pid namespace");
	init_dev = st.st_dev;
	init_ino = st.st_ino;
	printf("watching %ld %llu\n", pid, init_start);

	for (;;) {
		n = users();
		if (n < 0) fail(3, "opendir", "/proc");
		if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0) fail(5, "clock_gettime", "CLOCK_MONOTONIC");
		now = ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
		if (n > 0 && since >= 0) puts("cancelled");
		if (n > 0) since = -1;
		else if (since < 0) {
			since = now;
			puts("armed");
		} else if (now - since >= grace * 1000 && users() == 0 && !gone())
			break; /* zero users through the grace, and a fresh scan and identity check agree */
		if (nap(sigfd, &stop) || stop) return 0;
	}
	fprintf(stderr, "ginn-idle: no user process for %ld s; stopping %s\n", grace, argv[argc - 1]);
	if (stop_container(argv + 3) || gone()) return 0;
	fprintf(stderr, "ginn-idle: podman stop failed; init %ld still runs\n", pid);
	return 4;
}
