/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-watch: run a command after a burst of desktop-file changes in one directory (FR-044,
 * FR-045, NFR-009). inotify watches <dir> for ".desktop" names created, deleted, moved or closed
 * after writing. The command runs after <quiet-ms> without a change, or <max-ms> after the first
 * unhandled change, whichever is first. ginn runs `ginn _sync <realm>` this way. The command starts
 * by execvp from an argv list (no shell), with its stdout sent to stderr. Usage: ginn-watch
 * [--once] <dir> <quiet-ms> <max-ms> <cmd> [arg...]. stdout: "watching <dir>", then "event <n>
 * <status>" per run (status: exit code, or 128 + signal). --once exits 0 after one run. A removed
 * <dir> gets one run and exit 3. Exit: 0 ok, 1 failure, 2 usage, 3 directory gone.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_QUIET_MS 60000L
#define MAX_MAX_MS 600000L
#define WATCH_MASK                                                                          \
	(IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO | IN_CLOSE_WRITE | IN_ONLYDIR | \
	 IN_DELETE_SELF | IN_MOVE_SELF)
#define SUFFIX ".desktop"

struct state {
	long count; /* changes seen so far that matter: .desktop names and queue overflows */
	int gone;   /* the watched directory was deleted or moved */
};

static void usage(void) {
	fprintf(stderr, "usage: ginn-watch [--once] <dir> <quiet-ms> <max-ms> <cmd> [arg...]\n");
	exit(2);
}

static void fail(const char *op, const char *path) {
	fprintf(stderr, "ginn-watch: %s %s: %s\n", op, path, strerror(errno));
	exit(1);
}

static long now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

/* A decimal number in [1, max] with no sign and no blank characters. -1 when it is not one. */
static long parse_ms(const char *s, long max) {
	char *end = NULL;
	long v;
	if (*s < '0' || *s > '9') return -1;
	errno = 0;
	v = strtol(s, &end, 10);
	if (errno != 0 || *end != '\0' || v < 1 || v > max) return -1;
	return v;
}

/* An absolute path with no ".." component and no line break (stdout is line based). */
static int valid_dir(const char *p) {
	size_t n = strlen(p);

	return p[0] == '/' && !strstr(p, "/../") && (n < 3 || strcmp(p + n - 3, "/..")) &&
	       !strchr(p, '\n');
}

static int desktop_name(const char *name) {
	size_t n = strlen(name), k = strlen(SUFFIX);

	return n > k && !strcmp(name + n - k, SUFFIX);
}

/* Read every queued event without blocking, and note what matters in *st. */
static void drain(int fd, struct state *st) {
	char buf[4096] __attribute__((aligned(__alignof__(struct inotify_event))));
	ssize_t len;

	for (;;) {
		len = read(fd, buf, sizeof buf);
		if (len < 0 && errno == EINTR) continue;
		if (len < 0 && errno == EAGAIN) return;
		if (len <= 0) fail("read", "inotify");
		for (ssize_t off = 0; off + (ssize_t)sizeof(struct inotify_event) <= len;) {
			const struct inotify_event *ev = (const struct inotify_event *)(const void *)(buf + off);

			if (ev->mask & IN_Q_OVERFLOW)
				st->count++;
			else if (ev->mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED))
				st->gone = 1;
			else if (ev->len > 0 && desktop_name(ev->name))
				st->count++;
			off += (ssize_t)(sizeof(struct inotify_event) + ev->len);
		}
	}
}

/* The time a pending batch runs: `quiet` after its last change, at the latest `max` after its
 * first. */
static long due_at(long first, long last, long quiet, long max) {
	long by_quiet = last + quiet, by_max = first + max;

	return by_quiet < by_max ? by_quiet : by_max;
}

/* Run argv[0] with argv, wait for it, and return its status (128 + signal when killed). */
static int run_command(char **argv) {
	pid_t pid = fork();
	int status = 0;

	if (pid < 0) fail("fork", argv[0]);
	if (pid == 0) {
		dup2(STDERR_FILENO, STDOUT_FILENO);
		execvp(argv[0], argv);
		fprintf(stderr, "ginn-watch: exec %s: %s\n", argv[0], strerror(errno));
		_exit(127);
	}
	while (waitpid(pid, &status, 0) < 0) {
		if (errno != EINTR) fail("waitpid", argv[0]);
	}
	if (WIFEXITED(status)) return WEXITSTATUS(status);
	if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
	return 1;
}

int main(int argc, char **argv) {
	int once = 0, first_arg = 1, fd, status, pending = 0;
	long quiet, max, first = 0, last = 0, reported = 0;
	struct state st = {0, 0};
	const char *dir;
	char **cmd;
	if (argc > 1 && !strcmp(argv[1], "--once")) {
		once = 1;
		first_arg = 2;
	}
	if (argc - first_arg < 4) usage();
	dir = argv[first_arg];
	quiet = parse_ms(argv[first_arg + 1], MAX_QUIET_MS);
	max = parse_ms(argv[first_arg + 2], MAX_MAX_MS);
	if (!valid_dir(dir) || quiet < 0 || max < 0) usage();
	cmd = &argv[first_arg + 3];

	fd = inotify_init1(IN_CLOEXEC | IN_NONBLOCK);
	if (fd < 0) fail("inotify_init1", dir);
	if (inotify_add_watch(fd, dir, WATCH_MASK) < 0) fail("inotify_add_watch", dir);
	printf("watching %s\n", dir);
	fflush(stdout);

	for (;;) {
		long seen = st.count, t;
		int timeout = -1, n;
		struct pollfd pfd = {fd, POLLIN, 0};

		if (pending) {
			long left = due_at(first, last, quiet, max) - now_ms();

			timeout = left > 0 ? (int)left : 0;
		}
		n = poll(&pfd, 1, timeout);
		if (n < 0 && errno != EINTR) fail("poll", dir);
		if (n > 0) drain(fd, &st);
		t = now_ms();
		if (st.count > seen) {
			if (!pending) {
				pending = 1;
				first = t;
			}
			last = t;
		}
		if (st.gone || (pending && t >= due_at(first, last, quiet, max))) {
			status = run_command(cmd);
			printf("event %ld %d\n", st.count - reported, status);
			fflush(stdout);
			reported = st.count;
			pending = 0;
			if (st.gone) {
				fprintf(stderr, "ginn-watch: %s is gone\n", dir);
				return 3;
			}
			if (once) return 0;
		}
	}
}
