/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-agent-filter: an SSH agent socket that passes only the requests a Development realm needs
 * (FR-067 AC-1, NFR-SEC-007). Each client gets a fresh connection to <upstream> (the host's
 * SSH_AUTH_SOCK). Frames are a 4-byte big-endian length, then a body whose first byte is the type.
 * Requests 11 (list keys) and 13 (sign) pass, and replies 5, 12 and 14 come back. Any other
 * request gets SSH_AGENT_FAILURE and never reaches the agent. A zero or oversize length closes the
 * connection, since the stream cannot resync. Usage: ginn-agent-filter [--once] <upstream> <listen>.
 * stdout: "listening <path>" once up. --once serves one client and exits 0. Exit: 0, 1, 2 usage.
 * The helper runs until SIGTERM. Its lifetime is the caller's (FR-067): it outlives the ginn process
 * that started it, so it is not tied to that process's death.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_BODY (256u * 1024u) /* largest body accepted, in either direction */
#define MAX_CLIENTS 16          /* clients served at once; more are closed on accept */
#define IO_TIMEOUT_S 30         /* a stalled peer is dropped after this many seconds */
#define SSH_AGENT_FAILURE 5
#define SSH_IDENTITIES_REQUEST 11
#define SSH_IDENTITIES_ANSWER 12
#define SSH_SIGN_REQUEST 13
#define SSH_SIGN_RESPONSE 14

static const unsigned char CLIENT_OK[] = {SSH_IDENTITIES_REQUEST, SSH_SIGN_REQUEST};
static const unsigned char UPSTREAM_OK[] = {SSH_AGENT_FAILURE, SSH_IDENTITIES_ANSWER,
					    SSH_SIGN_RESPONSE};
static const unsigned char FAILURE_FRAME[5] = {0, 0, 0, 1, SSH_AGENT_FAILURE};
static unsigned char body[MAX_BODY];

static void usage(void) {
	fprintf(stderr, "usage: ginn-agent-filter [--once] <upstream-socket> <listen-socket>\n");
	exit(2);
}

static void fail(const char *op, const char *path) {
	fprintf(stderr, "ginn-agent-filter: %s %s: %s\n", op, path, strerror(errno));
	exit(1);
}

/* An absolute path that fits sun_path, with no ".." component and no line break. */
static int valid_path(const char *p) {
	size_t n = strlen(p);

	return p[0] == '/' && n < sizeof(((struct sockaddr_un *)0)->sun_path) && !strstr(p, "/../") &&
	       (n < 3 || strcmp(p + n - 3, "/..")) && !strchr(p, '\n');
}

static int read_all(int fd, unsigned char *p, size_t n) {
	while (n > 0) {
		ssize_t r = read(fd, p, n);
		if (r < 0 && errno == EINTR) continue;
		if (r <= 0) return -1;
		p += r, n -= (size_t)r;
	}
	return 0;
}

static int write_all(int fd, const unsigned char *p, size_t n) {
	while (n > 0) {
		ssize_t w = write(fd, p, n);
		if (w < 0 && errno == EINTR) continue;
		if (w <= 0) return -1;
		p += w, n -= (size_t)w;
	}
	return 0;
}

static void set_timeouts(int fd) {
	struct timeval tv = {IO_TIMEOUT_S, 0};

	setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
	setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
}

/* Read one frame: its length bytes into hdr (wire form, kept for forwarding), its body into
 * body[]. -1 on a zero or oversize length, or on I/O failure. */
static int read_frame(int fd, unsigned char *hdr, uint32_t *len) {
	if (read_all(fd, hdr, 4) < 0) return -1;
	*len = ((uint32_t)hdr[0] << 24) | ((uint32_t)hdr[1] << 16) | ((uint32_t)hdr[2] << 8) | hdr[3];
	if (*len == 0 || *len > MAX_BODY) return -1;
	return read_all(fd, body, *len);
}

static int listed(const unsigned char *types, size_t n, unsigned char type) {
	for (size_t i = 0; i < n; i++)
		if (types[i] == type) return 1;
	return 0;
}

/* Send a failure, then drain what the client still sends, up to one body, so that closing does not
 * reset the connection before the client reads the failure. */
static void refuse(int client) {
	size_t left = MAX_BODY;
	ssize_t r;

	write_all(client, FAILURE_FRAME, sizeof FAILURE_FRAME);
	shutdown(client, SHUT_WR);
	while (left > 0 && (r = read(client, body, left)) > 0) left -= (size_t)r;
}

/* Pass allowed requests upstream and their replies back. Any other message gets a failure. */
static void serve(int client, int up) {
	unsigned char hdr[4];
	uint32_t len;

	for (;;) {
		if (read_frame(client, hdr, &len) < 0) break;
		if (!listed(CLIENT_OK, sizeof CLIENT_OK, body[0])) {
			if (write_all(client, FAILURE_FRAME, sizeof FAILURE_FRAME) < 0) return;
			continue;
		}
		if (write_all(up, hdr, 4) < 0 || write_all(up, body, len) < 0) break;
		if (read_frame(up, hdr, &len) < 0 || !listed(UPSTREAM_OK, sizeof UPSTREAM_OK, body[0]))
			break;
		if (write_all(client, hdr, 4) < 0 || write_all(client, body, len) < 0) return;
	}
	refuse(client);
}

static void handle(int client, const char *upstream) {
	struct sockaddr_un a = {.sun_family = AF_UNIX};
	int up = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);

	set_timeouts(client);
	memcpy(a.sun_path, upstream, strlen(upstream) + 1);
	if (up < 0 || connect(up, (struct sockaddr *)&a, sizeof a) < 0) {
		if (up >= 0) close(up);
		refuse(client);
		return;
	}
	set_timeouts(up);
	serve(client, up);
	close(up);
}

int main(int argc, char **argv) {
	int once = argc > 1 && !strcmp(argv[1], "--once"), i = 1 + once, lfd, cfd, active = 0;
	const char *upstream, *listen_path;
	struct sockaddr_un addr = {.sun_family = AF_UNIX};

	if (argc - i != 2) usage();
	upstream = argv[i];
	listen_path = argv[i + 1];
	if (!valid_path(upstream) || !valid_path(listen_path)) usage();

	signal(SIGPIPE, SIG_IGN);

	lfd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (lfd < 0) fail("socket", listen_path);
	memcpy(addr.sun_path, listen_path, strlen(listen_path) + 1);
	umask(077); /* the socket is owner-only */
	if (bind(lfd, (struct sockaddr *)&addr, sizeof addr) < 0) fail("bind", listen_path);
	if (listen(lfd, 8) < 0) fail("listen", listen_path);
	printf("listening %s\n", listen_path);
	fflush(stdout);

	for (;;) {
		pid_t pid;

		cfd = accept(lfd, NULL, NULL);
		if (cfd < 0) {
			if (errno == EINTR || errno == ECONNABORTED) continue;
			fail("accept", listen_path);
		}
		if (once) {
			handle(cfd, upstream);
			return 0;
		}
		while (waitpid(-1, NULL, WNOHANG) > 0) active--;
		if (active >= MAX_CLIENTS) {
			close(cfd);
			continue;
		}
		pid = fork();
		if (pid == 0) {
			close(lfd);
			handle(cfd, upstream);
			_exit(0);
		}
		close(cfd);
		if (pid > 0) active++;
	}
}
