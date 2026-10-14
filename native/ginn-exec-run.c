/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-exec-run: runs one checked exec request (ADR-014, FR-049, FR-123). The worker execs it with argv
 * `<root> <realm> <flags> <token|""> <fd>... -- <podman> [arg...]`; envp is podman's environment. fd 3 is the client;
 * <fd>... are podman's stdio and (captured) the pipe read ends, set CLOEXEC here. A mutating request holds flock on
 * <root>/exec/<realm>.lock or is refused LOCKED naming the holder; the kill runs and the lock is released before EXIT.
 * SIG frames reach podman; on client EOF podman gets SIGHUP (tty) or SIGTERM, then SIGKILL after 2 s.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "ginn-exec-frame.h"
#define CLIENT GINN_EXEC_CLIENT_FD
#define GRACE_MS 2000
extern char **environ; static unsigned char rec[GINN_EXEC_MAX_RECORD];
static void die(const char *op) { fprintf(stderr, "ginn-exec-run: %s: %s\n", op, strerror(errno)), exit(1); }
static long long now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000LL + t.tv_nsec / 1000000; }
static void refuse(int code, const char *why) {
	unsigned char b[256];
	fprintf(stderr, "ginn-exec-run: %s\n", why), frame_send(CLIENT, b, frame_reject(b, code, why)), exit(0);
}

int main(int argc, char **argv) {
	int fd[5], nf, dd = 5, flags, mutating, tty, captured, lock = -1, sfd, rd[2] = {-1, -1}, i, st = 0, alive = 1;
	int junk[5], jn = 0, n, kind, val, ms;
	char *dir, *lockp, *orphan, *cg, *kpath, sink[4096], why[256], who[96];
	pid_t pid, parent, kp;
	sigset_t set, none;
	long long grace = 0;
	unsigned char ack[5], fin[3];
	ssize_t got;
	if (argc < 6 || argv[0][0] != '/' || argv[1][0] != '/') return fprintf(stderr, "usage: ginn-exec-run <runtime-root> <realm> <flags> <token> <fd>... -- <podman> [arg...]\n"), 2;
	while (dd < argc && strcmp(argv[dd], "--")) dd++;
	flags = atoi(argv[3]), nf = dd - 5;
	if (dd + 1 >= argc || nf != ((flags & GINN_EXEC_FL_CAPTURED) ? 5 : 3)) return fprintf(stderr, "ginn-exec-run: the descriptor numbers do not match the flags\n"), 2;
	for (i = 0; i < nf; i++) fd[i] = atoi(argv[5 + i]);
	mutating = !!(flags & GINN_EXEC_FL_MUTATING), tty = !!(flags & GINN_EXEC_FL_TTY), captured = !!(flags & GINN_EXEC_FL_CAPTURED);
	if (asprintf(&dir, "%s/exec", argv[1]) < 0 || asprintf(&lockp, "%s/%s.lock", dir, argv[2]) < 0 ||
	    asprintf(&orphan, "%s/%s.orphan", dir, argv[2]) < 0 ||
	    asprintf(&cg, "/sys/fs/cgroup/ginn/%ld/%s", (long)getuid(), argv[2]) < 0 ||
	    asprintf(&kpath, "%.*s/ginn-exec-kill", (int)(strrchr(argv[0], '/') - argv[0]), argv[0]) < 0)
		die("asprintf");
	if (mutating) {
		if (mkdir(dir, 0700) < 0 && errno != EEXIST) die("mkdir");
		if ((lock = open(lockp, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600)) < 0) die("open");
		if (flock(lock, LOCK_EX | LOCK_NB) < 0) {
			ssize_t r; long rp = 0;
			if (errno != EWOULDBLOCK) die("flock");
			r = pread(lock, who, sizeof who - 1, 0);
			who[r > 0 ? r : 0] = '\0', who[strcspn(who, "\n")] = '\0';
			if (sscanf(who, "run %ld", &rp) != 1 || rp < 1 || (kill((pid_t)rp, 0) < 0 && errno == ESRCH)) who[0] = '\0';
			snprintf(why, sizeof why, "GINN-E-REALM-BUSY: realm %s is locked by exec %s", argv[2], who[0] ? who : "not yet recorded");
			refuse(GINN_EXEC_REJ_LOCKED, why);
		}
		if (ftruncate(lock, 0) < 0) fprintf(stderr, "ginn-exec-run: cannot clear the holder line\n"); /* a finished holder is never named: the pid check covers it */
	}
	for (i = 0; i < nf; i++) fcntl(fd[i], F_SETFD, FD_CLOEXEC);
	fcntl(CLIENT, F_SETFD, FD_CLOEXEC);
	sigemptyset(&none), sigemptyset(&set), sigaddset(&set, SIGCHLD), sigprocmask(SIG_BLOCK, &set, NULL);
	if ((sfd = signalfd(-1, &set, SFD_CLOEXEC | SFD_NONBLOCK)) < 0) die("signalfd");
	if (captured) rd[0] = fd[3], rd[1] = fd[4];
	parent = getpid();
	if ((pid = fork()) < 0) die("fork");
	if (pid == 0) {
		sigprocmask(SIG_SETMASK, &none, NULL);
		if (tty) {
			prctl(PR_SET_PDEATHSIG, SIGHUP);
			if (getppid() != parent) _exit(127);
		}
		dup2(fd[0], 0), dup2(fd[1], 1), dup2(fd[2], 2);
		execve(argv[dd + 1], argv + dd + 1, environ);
		_exit(127);
	}
	if (mutating && dprintf(lock, "run %ld podman %ld", (long)parent, (long)pid) < 0) fprintf(stderr, "ginn-exec-run: cannot record the holder; LOCKED will not name it\n");
	for (i = 0; i < 3; i++) close(fd[i]);
	ack[0] = GINN_EXEC_ACK;
	for (i = 0; i < 4; i++) ack[1 + i] = (unsigned char)((unsigned)pid >> (24 - 8 * i));
	frame_send(CLIENT, ack, sizeof ack);

	/* SIG in, the drain once the client has left, and podman's exit. */
	for (;;) {
		struct pollfd p[4] = {{alive ? CLIENT : -1, POLLIN, 0}, {sfd, POLLIN, 0},
		                      {alive ? -1 : rd[0], POLLIN, 0}, {alive ? -1 : rd[1], POLLIN, 0}};
		struct signalfd_siginfo si;
		ms = grace ? (int)(grace - now_ms() > 0 ? grace - now_ms() : 0) : -1;
		if (poll(p, 4, ms) < 0 && errno != EINTR) die("poll");
		if (grace && now_ms() >= grace) kill(pid, SIGKILL), grace = 0;
		if (p[0].revents) {
			got = frame_recv(CLIENT, rec, GINN_EXEC_MAX_RECORD, junk, &jn);
			while (jn > 0) close(junk[--jn]);
			if (got == 0 || got == -1) {
				alive = 0, kill(pid, tty ? SIGHUP : SIGTERM), grace = now_ms() + GRACE_MS;
			} else if (got == 2 && rec[0] == GINN_EXEC_SIG && rec[1] >= 1 && rec[1] < 32) {
				kill(pid, rec[1]);
			}
		}
		if (p[1].revents) {
			while (read(sfd, &si, sizeof si) > 0) {}
			if (waitpid(pid, &st, WNOHANG) == pid) break;
		}
		for (i = 0; i < 2; i++) {
			if (!p[2 + i].revents) continue;
			n = (int)read(rd[i], sink, sizeof sink);
			if (n == 0 || (n < 0 && errno != EINTR)) close(rd[i]), rd[i] = -1;
		}
	}
	kind = WIFEXITED(st) ? 0 : 1;
	val = WIFEXITED(st) ? WEXITSTATUS(st) : WTERMSIG(st);
	if (mutating) { /* the token kill runs, and the lock is released, before EXIT */
		char *kav[] = {kpath, cg, argv[4], orphan, NULL}, *kenv[] = {NULL};
		if ((kp = fork()) < 0) { /* the kill cannot run: mark the token's processes as unknown */
			int mfd = open(orphan, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
			if (mfd >= 0) dprintf(mfd, "token %s\ntime %lld\nkill not run\n", argv[4], (long long)time(NULL)), close(mfd);
		} else if (kp == 0) {
			sigprocmask(SIG_SETMASK, &none, NULL), execve(kpath, kav, kenv), _exit(127);
		} else {
			while (waitpid(kp, NULL, 0) < 0 && errno == EINTR) {}
		}
		if (ftruncate(lock, 0) < 0) fprintf(stderr, "ginn-exec-run: cannot clear the holder line\n");
		close(lock);
	}
	if (alive) fin[0] = GINN_EXEC_EXIT, fin[1] = (unsigned char)kind, fin[2] = (unsigned char)val, frame_send(CLIENT, fin, sizeof fin);
	return 0;
}
