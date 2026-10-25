/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-wait: the process runit supervises for one services-on realm (FR-081, NFR-033). It waits on
 * conmon through a pidfd. conmon exits on its own: exit 1, so runit's finish and restart logic sees
 * a crash (AS-028). SIGTERM (`sv down`): run `<podman> [global-arg...] stop <container>` as an argv
 * (no shell), wait for conmon, exit 0. Usage: ginn-wait <conmon-pidfile> <podman> [global-arg...]
 * -- <container>, absolute paths. stdout: "waiting <pid> <start>". Exit: 0 stopped on request,
 * 1 conmon exited without one, 2 usage, 3 pidfile or /proc error, or the pid is not conmon (not
 * running, or start time changed), 4 podman stop failed while conmon runs, 5 system call failed.
 * Start time (/proc/<pid>/stat field 22) is checked with the pidfile and after pidfd_open. Without
 * SYS_pidfd_open or on ENOSYS it polls every second. A pid reused before the pidfile is read is not
 * detected: remove a stale pidfile before podman starts.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/signalfd.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

struct watch {
	pid_t pid;
	unsigned long long start; /* /proc/<pid>/stat field 22: clock ticks since boot */
	int pidfd;                /* -1 on the polling fallback */
};

static void usage(void) {
	fprintf(stderr, "usage: ginn-wait <conmon-pidfile> <podman> [global-arg...] -- <container>\n");
	exit(2);
}

static void fail(int code, const char *op, const char *what) {
	fprintf(stderr, "ginn-wait: %s %s: %s\n", op, what, strerror(errno));
	exit(code);
}

/* A container name: 1 to 255 of [A-Za-z0-9_.-], not starting with '-' (an option to podman). */
static int valid_name(const char *s) {
	size_t i;

	for (i = 0; s[i]; i++) {
		char c = s[i];

		if (!isalnum((unsigned char)c) && !strchr("_.", c) && !(c == '-' && i > 0)) return 0;
	}
	return i > 0 && i <= 255;
}

/* State letter (field 3) and start time (field 22) of a process. -1 when /proc has no answer. */
static int proc_stat(pid_t pid, char *state, unsigned long long *start) {
	char path[64], buf[1024], *p, *end;
	ssize_t n;
	int fd, i;
	snprintf(path, sizeof path, "/proc/%ld/stat", (long)pid);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return -1;
	do n = read(fd, buf, sizeof buf - 1); while (n < 0 && errno == EINTR);
	close(fd);
	if (n <= 0) return -1;
	buf[n] = '\0';
	if (!(p = strrchr(buf, ')'))) return -1;
	p += 2; /* ") S": the state letter is field 3 */
	*state = *p;
	for (i = 3; i < 22; i++) { /* 19 moves: from field 3 to field 22 */
		p = strchr(p, ' ');
		if (!p) return -1;
		p++;
	}
	*start = strtoull(p, &end, 10);
	return end == p ? -1 : 0;
}

/* The pid in <path>: decimal digits and an optional newline. Exit 3 when it is not one. */
static pid_t read_pid(const char *path) {
	char buf[32], *end;
	ssize_t n;
	long v;
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) fail(3, "open", path);
	do n = read(fd, buf, sizeof buf - 1); while (n < 0 && errno == EINTR);
	close(fd);
	if (n < 0) fail(3, "read", path);
	buf[n] = '\0';
	errno = 0;
	v = strtol(buf, &end, 10);
	if (errno || end == buf || (*end && strcmp(end, "\n")) || v < 1 || v > INT_MAX) {
		fprintf(stderr, "ginn-wait: %s does not hold a pid\n", path);
		exit(3);
	}
	return (pid_t)v;
}

/* A CLOEXEC pidfd for pid. -1 with errno set on failure; ENOSYS selects the polling fallback. */
static int pidfd_of(pid_t pid) {
#ifdef SYS_pidfd_open
	long fd = syscall(SYS_pidfd_open, pid, 0);
	if (fd >= 0) fcntl((int)fd, F_SETFD, FD_CLOEXEC);
	return (int)fd;
#else
	(void)pid;
	errno = ENOSYS;
	return -1;
#endif
}

/* Polling fallback: conmon is gone when the pid names another process, is a zombie, or is free. */
static int vanished(const struct watch *w) {
	char state = 0;
	unsigned long long start = 0;
	if (proc_stat(w->pid, &state, &start) < 0 || start != w->start) return 1;
	return state == 'Z' || state == 'X' || (kill(w->pid, 0) < 0 && errno == ESRCH);
}

/* One wait step of at most ms. Sets *stop when SIGTERM arrives. Returns 1 when conmon is gone. */
static int wait_once(const struct watch *w, int sigfd, int *stop, int ms) {
	struct pollfd p[2] = {{sigfd, POLLIN, 0}, {w->pidfd, POLLIN, 0}};
	struct signalfd_siginfo si;
	int n = poll(p, w->pidfd >= 0 ? 2 : 1, ms);

	if (n < 0 && errno != EINTR) fail(5, "poll", "conmon");
	if (p[0].revents) {
		*stop = 1;
		while (read(sigfd, &si, sizeof si) > 0) continue;
	}
	if (w->pidfd >= 0) return p[1].revents != 0;
	return vanished(w);
}

/* Run `<podman> [global-arg...] stop <container>` (cmd is argv + 2). Its stdout goes to stderr. */
static int stop_container(char **cmd) {
	sigset_t none;
	pid_t pid;
	int status = 0;
	sigemptyset(&none);
	pid = fork();
	if (pid < 0) fail(5, "fork", cmd[0]);
	if (pid == 0) {
		sigprocmask(SIG_SETMASK, &none, NULL); /* SIGTERM stays blocked in ginn-wait only */
		dup2(STDERR_FILENO, STDOUT_FILENO);
		execv(cmd[0], cmd);
		fprintf(stderr, "ginn-wait: exec %s: %s\n", cmd[0], strerror(errno));
		_exit(127);
	}
	while (waitpid(pid, &status, 0) < 0) if (errno != EINTR) fail(5, "waitpid", cmd[0]);
	return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

int main(int argc, char **argv) {
	struct watch w = {0, 0, -1};
	sigset_t set;
	char state = 0;
	unsigned long long now = 0;
	int sigfd, stop = 0, gone = 0, ms;

	if (argc < 5 || strcmp(argv[argc - 2], "--") || argv[1][0] != '/' || argv[2][0] != '/' ||
	    !valid_name(argv[argc - 1])) usage();
	argv[argc - 2] = "stop"; /* argv + 2 is now: <podman> [global-arg...] stop <container> */

	sigemptyset(&set);
	sigaddset(&set, SIGTERM);
	if (sigprocmask(SIG_BLOCK, &set, NULL) < 0) fail(5, "sigprocmask", "SIGTERM");
	sigfd = signalfd(-1, &set, SFD_CLOEXEC | SFD_NONBLOCK);
	if (sigfd < 0) fail(5, "signalfd", "SIGTERM");

	w.pid = read_pid(argv[1]);
	if (proc_stat(w.pid, &state, &w.start) < 0 || state == 'Z' || state == 'X') {
		fprintf(stderr, "ginn-wait: pid %ld is not running\n", (long)w.pid);
		return 3;
	}
	w.pidfd = pidfd_of(w.pid);
	if (w.pidfd < 0 && errno != ENOSYS) fail(3, "pidfd_open", argv[1]);
	if (proc_stat(w.pid, &state, &now) < 0 || now != w.start) {
		fprintf(stderr, "ginn-wait: pid %ld changed before the watch started\n", (long)w.pid);
		return 3;
	}
	ms = w.pidfd >= 0 ? -1 : 1000; /* polling fallback: one check a second */
	printf("waiting %ld %llu\n", (long)w.pid, w.start);
	fflush(stdout);

	while (!stop && !gone) gone = wait_once(&w, sigfd, &stop, ms);
	if (!stop) {
		fprintf(stderr, "ginn-wait: conmon %ld exited\n", (long)w.pid);
		return 1;
	}
	if (!stop_container(argv + 2) && !wait_once(&w, sigfd, &stop, 1000)) {
		fprintf(stderr, "ginn-wait: podman stop failed; conmon %ld still runs\n", (long)w.pid);
		return 4;
	}
	while (!wait_once(&w, sigfd, &stop, ms)) {}
	return 0;
}
