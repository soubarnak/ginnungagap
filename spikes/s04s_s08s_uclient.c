// SPDX-License-Identifier: Apache-2.0
//
// S-08s probe client (CL-5). Statically linked and bind-mounted read-only into the spike
// container, because busybox nc in alpine has no unix-socket mode.
//
//   uclient <socket path>
//
// Prints the inode and type of the path as this mount namespace sees it, connects, sends one
// line and prints the reply. Exit 0 only when a reply arrived.
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

int main(int argc, char **argv) {
	if (argc != 2) {
		fprintf(stderr, "usage: uclient <socket path>\n");
		return 2;
	}
	struct stat st;
	if (stat(argv[1], &st) != 0) {
		printf("stat: errno=%d (%s)\n", errno, strerror(errno));
		return 1;
	}
	printf("stat: ino=%lu socket=%s\n", (unsigned long)st.st_ino,
	       S_ISSOCK(st.st_mode) ? "yes" : "no");

	struct sockaddr_un addr;
	memset(&addr, 0, sizeof addr);
	addr.sun_family = AF_UNIX;
	if (strlen(argv[1]) >= sizeof addr.sun_path) {
		printf("path too long\n");
		return 1;
	}
	strcpy(addr.sun_path, argv[1]);

	int fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0) {
		printf("socket: errno=%d (%s)\n", errno, strerror(errno));
		return 1;
	}
	if (connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
		printf("connect: errno=%d (%s)\n", errno, strerror(errno));
		return 1;
	}
	const char msg[] = "ping\n";
	if (write(fd, msg, sizeof msg - 1) < 0) {
		printf("write: errno=%d (%s)\n", errno, strerror(errno));
		return 1;
	}
	char buf[256];
	ssize_t n = read(fd, buf, sizeof buf - 1);
	if (n <= 0) {
		printf("read: n=%zd errno=%d (%s)\n", n, errno, strerror(errno));
		return 1;
	}
	buf[n] = '\0';
	printf("reply: %s", buf);
	return 0;
}
