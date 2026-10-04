/*
 * A lifeline from a launcher to a helper it starts: the launcher keeps the write end of a pipe
 * open for as long as it lives and hands the read end to the helper, which stops when the pipe
 * reports POLLHUP (every writer is gone, including a launcher that was killed with SIGKILL).
 *
 * PR_SET_PDEATHSIG is not usable for this: it is tied to the thread that forked the helper, and
 * the launcher starts its helpers from worker threads that exit long before the launcher does.
 */
#ifndef SPACES_LIFELINE_H
#define SPACES_LIFELINE_H

#include <fcntl.h>
#include <glib-unix.h>
#include <glib.h>

/* Whether FD is open at all; a bad descriptor would make the poll source spin on POLLNVAL. */
static gboolean lifeline_valid(int fd)
{
    return fd >= 0 && fcntl(fd, F_GETFD) >= 0;
}

static gboolean lifeline_cut(gint fd, GIOCondition condition, gpointer loop)
{
    (void)fd;
    (void)condition;
    g_main_loop_quit(loop);
    return G_SOURCE_REMOVE;
}

/* Quit LOOP when the write end of the pipe FD is closed. FD is not inherited by programs that
 * the helper starts. Returns the id of the source in the default main context, 0 if FD is bad. */
static guint lifeline_watch(int fd, GMainLoop *loop)
{
    if (!lifeline_valid(fd) || fcntl(fd, F_SETFD, FD_CLOEXEC) != 0)
        return 0;
    return g_unix_fd_add(fd, G_IO_HUP | G_IO_ERR, lifeline_cut, loop);
}

#endif
