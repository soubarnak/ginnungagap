// SPDX-License-Identifier: Apache-2.0
// S-02 spike: run a command inside the namespaces of a running rootless container.
//
// Usage: s02_run <host-pid> <cmd> [args...]
//   <host-pid> is the container's PID 1 as seen from the host (podman inspect .State.Pid).
// Exit status: the command's status; 128+N if it died on signal N; 125 for setup failures
// (the errno text is printed to stderr).
//
// Order: open every /proc/<pid>/ns fd first (the host /proc path disappears once the
// mount namespace is joined), then setns() user first, then the others, then drop to the
// in-container uid/gid, chdir to /, set a minimal env, fork so the child lands in the joined
// PID namespace, and wait for it.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define EXIT_SETUP 125
#define OVERFLOW_ID 65534

static void fail_errno(const char *what, const char *path) {
    fprintf(stderr, "s02_run: %s%s%s: %s (errno %d)\n", what, path ? " " : "",
            path ? path : "", strerror(errno), errno);
    exit(EXIT_SETUP);
}

static int open_ns(pid_t pid, const char *name) {
    char path[64];
    snprintf(path, sizeof path, "/proc/%ld/ns/%s", (long)pid, name);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) fail_errno("open", path);
    return fd;
}

static void join(int fd, int nstype, const char *name) {
    if (setns(fd, nstype) < 0) {
        char what[32];
        snprintf(what, sizeof what, "setns %s", name);
        fail_errno(what, NULL);
    }
}

static char *dup_env(const char *name) {
    const char *v = getenv(name);
    return v ? strdup(v) : NULL;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: s02_run <host-pid> <cmd> [args...]\n");
        return EXIT_SETUP;
    }
    char *end = NULL;
    long pid_l = strtol(argv[1], &end, 10);
    if (*end != '\0' || pid_l <= 0) {
        fprintf(stderr, "s02_run: bad pid '%s'\n", argv[1]);
        return EXIT_SETUP;
    }
    pid_t pid = (pid_t)pid_l;

    // Save env values before clearenv().
    char *home = dup_env("HOME");
    char *term = dup_env("TERM");

    // 1. Open all target namespace fds before the first setns.
    int fd_user = open_ns(pid, "user");
    int fd_mnt = open_ns(pid, "mnt");
    int fd_uts = open_ns(pid, "uts");
    int fd_ipc = open_ns(pid, "ipc");
    int fd_pid = open_ns(pid, "pid");
    int fd_cgroup = open_ns(pid, "cgroup");

    // Join the net namespace only when it differs from ours (--network host: same inode).
    struct stat self_net, target_net;
    if (stat("/proc/self/ns/net", &self_net) < 0) fail_errno("stat", "/proc/self/ns/net");
    int fd_net = open_ns(pid, "net");
    if (fstat(fd_net, &target_net) < 0) fail_errno("fstat", "net");
    int join_net = (self_net.st_ino != target_net.st_ino) || (self_net.st_dev != target_net.st_dev);

    // 2. user namespace first: gives this process caps inside it (owner rule).
    join(fd_user, CLONE_NEWUSER, "user");
    join(fd_mnt, CLONE_NEWNS, "mnt");
    join(fd_uts, CLONE_NEWUTS, "uts");
    join(fd_ipc, CLONE_NEWIPC, "ipc");
    join(fd_pid, CLONE_NEWPID, "pid");
    join(fd_cgroup, CLONE_NEWCGROUP, "cgroup");
    if (join_net) join(fd_net, CLONE_NEWNET, "net");
    close(fd_user); close(fd_mnt); close(fd_uts);
    close(fd_ipc); close(fd_pid); close(fd_cgroup); close(fd_net);

    // 3. Drop to the in-container uid/gid (getuid() after setns is the in-container id).
    uid_t uid = getuid();
    gid_t gid = getgid();
    if (uid == OVERFLOW_ID || gid == OVERFLOW_ID) {
        fprintf(stderr, "s02_run: host uid/gid not mapped in container (uid %u gid %u)\n",
                (unsigned)uid, (unsigned)gid);
        return EXIT_SETUP;
    }
    if (setgroups(0, NULL) < 0) fail_errno("setgroups", NULL);
    if (setresgid(gid, gid, gid) < 0) fail_errno("setresgid", NULL);
    if (setresuid(uid, uid, uid) < 0) fail_errno("setresuid", NULL);

    // 4. Root directory, minimal environment.
    if (chdir("/") < 0) fail_errno("chdir", "/");
    clearenv();
    setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
    if (home) setenv("HOME", home, 1);
    if (term) setenv("TERM", term, 1);

    // 5. Fork so the child is the first process in the joined PID namespace.
    pid_t child = fork();
    if (child < 0) fail_errno("fork", NULL);
    if (child == 0) {
        execvp(argv[2], &argv[2]);
        fprintf(stderr, "s02_run: exec %s: %s (errno %d)\n", argv[2], strerror(errno), errno);
        _exit(errno == ENOENT ? 127 : 126);
    }

    // 6. Parent waits and passes the exit status through (IR-011).
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) fail_errno("waitpid", NULL);
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return EXIT_SETUP;
}
