/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-exec-relay: the per-user listener of the exec relay (ADR-014, FR-049, AS-028). Usage:
 * ginn-exec-relay <runtime-root> <podman> [global-arg...], absolute. It binds <runtime-root>/exec.sock
 * (mode 0600) and starts one ginn-exec-worker per connection, beside itself, with the connection on fd 3.
 * A peer of another uid or user namespace gets no reply. At 32 workers a connection gets REJECT BUSY.
 * Exit: 2 usage, 1 system call failed, 3 the runtime root is not a 0700 directory of this uid.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>
#include "ginn-exec-frame.h"

#define MAX_WORKERS 32

static char worker[4096]; /* <dir of this binary>/ginn-exec-worker */
static char **wargv;
static int workers;

static void die(int code, const char *op) {
	fprintf(stderr, "ginn-exec-relay: %s: %s\n", op, strerror(errno));
	exit(code);
}

/* The peer's user namespace must be the listener's own. A link that cannot be read is a mismatch. */
static int same_userns(pid_t pid) {
	char mine[64], theirs[64], path[64];
	ssize_t a, b;
	snprintf(path, sizeof path, "/proc/%ld/ns/user", (long)pid);
	a = readlink("/proc/self/ns/user", mine, sizeof mine - 1);
	b = readlink(path, theirs, sizeof theirs - 1);
	if (a < 0 || b < 0) return 0;
	mine[a] = '\0';
	theirs[b] = '\0';
	return !strcmp(mine, theirs);
}

/* SO_PEERCRED: the same uid, and the same user namespace. */
static int peer_ok(int fd) {
	struct ucred c;
	socklen_t len = sizeof c;
	if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &c, &len) < 0) return 0;
	return c.uid == getuid() && same_userns(c.pid);
}

static void accept_one(int lfd) {
	unsigned char b[256];
	sigset_t none;
	pid_t pid;
	int cfd = accept4(lfd, NULL, NULL, SOCK_CLOEXEC);
	if (cfd < 0) return;
	if (!peer_ok(cfd)) return (void)close(cfd);
	if (workers >= MAX_WORKERS) {
		struct pollfd q = {cfd, POLLIN, 0};
		size_t n;
		poll(&q, 1, 50), recv(cfd, b, sizeof b, MSG_DONTWAIT); /* up to 50 ms for the REQ, then drain it: close would reset */
		n = frame_reject(b, GINN_EXEC_REJ_BUSY, GINN_EXEC_E_BUSY ": 32 workers are running");
		frame_send(cfd, b, n), close(cfd);
		return;
	}
	pid = fork();
	if (pid < 0) return (void)close(cfd);
	if (pid == 0) {
		sigemptyset(&none), sigprocmask(SIG_SETMASK, &none, NULL);
		if (cfd != GINN_EXEC_CLIENT_FD) dup2(cfd, GINN_EXEC_CLIENT_FD);
		else fcntl(cfd, F_SETFD, 0);
		execv(worker, wargv);
		_exit(127);
	}
	close(cfd);
	workers++;
}

int main(int argc, char **argv) {
	struct sockaddr_un sa = {.sun_family = AF_UNIX};
	char self[4096], sock[256];
	struct stat st;
	sigset_t set;
	int lfd, sfd;
	ssize_t got;

	if (argc < 3 || argv[1][0] != '/' || argv[2][0] != '/') {
		fprintf(stderr, "usage: ginn-exec-relay <runtime-root> <podman> [global-arg...]\n");
		return 2;
	}
	umask(077);
	if (stat(argv[1], &st) < 0) die(1, "stat");
	if (!S_ISDIR(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 077)) {
		fprintf(stderr, "ginn-exec-relay: %s is not a 0700 directory of this uid\n", argv[1]);
		return 3;
	}
	snprintf(sock, sizeof sock, "%s/exec.sock", argv[1]);
	if (strlen(sock) >= sizeof sa.sun_path) {
		fprintf(stderr, "ginn-exec-relay: %s is too long for a socket path\n", sock);
		return 2;
	}
	strcpy(sa.sun_path, sock);
	got = readlink("/proc/self/exe", self, sizeof self - 1);
	if (got < 0) die(1, "readlink");
	self[got] = '\0';
	snprintf(worker, sizeof worker, "%.*s/ginn-exec-worker", (int)(strrchr(self, '/') - self), self);
	argv[0] = worker;
	wargv = argv;

	unlink(sock);
	lfd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (lfd < 0) die(1, "socket");
	if (bind(lfd, (struct sockaddr *)&sa, sizeof sa) < 0) die(1, "bind");
	if (chmod(sock, 0600) < 0) die(1, "chmod");
	if (listen(lfd, MAX_WORKERS) < 0) die(1, "listen");

	sigemptyset(&set);
	sigaddset(&set, SIGCHLD);
	sigprocmask(SIG_BLOCK, &set, NULL);
	sfd = signalfd(-1, &set, SFD_CLOEXEC | SFD_NONBLOCK);
	if (sfd < 0) die(1, "signalfd");
	for (;;) {
		struct pollfd p[2] = {{lfd, POLLIN, 0}, {sfd, POLLIN, 0}};
		struct signalfd_siginfo si;
		if (poll(p, 2, -1) < 0 && errno != EINTR) die(1, "poll");
		if (p[1].revents) {
			while (read(sfd, &si, sizeof si) > 0) continue;
			while (waitpid(-1, NULL, WNOHANG) > 0) workers--;
		}
		if (p[0].revents) accept_one(lfd);
	}
}
