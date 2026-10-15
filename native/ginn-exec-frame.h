/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Wire format of the exec relay (ADR-014, FR-049, NFR-SEC-005). SOCK_SEQPACKET; the first byte is the type.
 * Integers are big-endian; a string is a u16 length and its bytes. REQ: u8 version, u8 flags, u8 nfds, string
 * realm, 64-byte cid, u16 n and n NAME=VALUE strings, u16 n and n argv strings; fds 0-2 stdio, 3-4 pipe read
 * ends. ACK: u32 pid. REJECT: u8 code, string. SIG: u8. EXIT: u8 kind (0 status, 1 signal), u8 value.
 * Include after errno.h, string.h, sys/socket.h, sys/uio.h and unistd.h.
 */
#define GINN_EXEC_REQ 0x01
#define GINN_EXEC_ACK 0x02
#define GINN_EXEC_REJECT 0x03
#define GINN_EXEC_SIG 0x04
#define GINN_EXEC_EXIT 0x05
#define GINN_EXEC_VERSION 1
#define GINN_EXEC_FL_TTY 1
#define GINN_EXEC_FL_INTERACTIVE 2
#define GINN_EXEC_FL_MUTATING 4
#define GINN_EXEC_FL_CAPTURED 8
#define GINN_EXEC_CID_LEN 64
#define GINN_EXEC_MAX_RECORD 65536
#define GINN_EXEC_MAX_ITEMS 256
#define GINN_EXEC_MAX_VALUE 4096
#define GINN_EXEC_CLIENT_FD 3
#define GINN_EXEC_REJ_REJECTED 1
#define GINN_EXEC_REJ_LOCKED 2
#define GINN_EXEC_REJ_BUSY 3
#define GINN_EXEC_E_REJECTED "GINN-E-RELAY-REJECTED"
#define GINN_EXEC_E_BUSY "GINN-E-RELAY-BUSY"
#define GINN_EXEC_W_ORPHAN "GINN-W-RELAY-ORPHAN"
/* A closed peer is EPIPE, never SIGPIPE. */
static inline ssize_t frame_send(int fd, const unsigned char *b, size_t n) { return send(fd, b, n, MSG_NOSIGNAL); }

/* Writes a REJECT frame into b (4 + strlen(why) bytes) and returns its length. */
static inline size_t frame_reject(unsigned char *b, int code, const char *why) {
	size_t n = strlen(why);
	b[0] = GINN_EXEC_REJECT, b[1] = (unsigned char)code, b[2] = (unsigned char)(n >> 8), b[3] = (unsigned char)n;
	memcpy(b + 4, why, n);
	return 4 + n;
}

/* One record into b and its descriptors into fds (at most 5; extras are closed). Returns the byte count,
 * 0 at EOF, -1 on error, or -2 when the record or its descriptors were cut short or over five. */
static inline ssize_t frame_recv(int fd, unsigned char *b, size_t cap, int *fds, int *nfds) {
	char cbuf[CMSG_SPACE(6 * sizeof(int))];
	struct iovec iov = {b, cap};
	struct msghdr m = {0};
	struct cmsghdr *c; ssize_t r; int over = 0;
	m.msg_iov = &iov, m.msg_iovlen = 1, m.msg_control = cbuf, m.msg_controllen = sizeof cbuf;
	do r = recvmsg(fd, &m, MSG_CMSG_CLOEXEC); while (r < 0 && errno == EINTR);
	*nfds = 0;
	if (r < 0) return -1; /* the control buffer was not filled: parse nothing, keep errno */
	for (c = CMSG_FIRSTHDR(&m); c; c = CMSG_NXTHDR(&m, c))
		if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS)
			for (size_t i = 0, k = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int); i < k; i++) {
				int f = ((int *)CMSG_DATA(c))[i];
				if (*nfds < 5) fds[(*nfds)++] = f;
				else close(f), over = 1;
			}
	return (r >= 0 && (over || (m.msg_flags & (MSG_TRUNC | MSG_CTRUNC)))) ? -2 : r;
}
