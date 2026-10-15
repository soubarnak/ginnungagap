/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ginn-exec-worker: checks one exec request, then execs ginn-exec-run in place (ADR-014, FR-049, NFR-SEC-005).
 * argv `<dir>/ginn-exec-worker <root> <podman> [global...]`, client on fd 3. It execs `<dir>/ginn-exec-run
 * <root> <realm> <flags> <token|""> <fd>... -- <podman> [arg...]`, envp = pass-through, then ginn keys.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>
#include "ginn-exec-frame.h"
#define TOKEN "GINN_EXEC_TOKEN"
#define BAD_REQ GINN_EXEC_E_REJECTED ": malformed request"
#define BAD_TAIL GINN_EXEC_E_REJECTED ": argv outside the allowlist"
static const char *const KEYS[] = {"XDG_RUNTIME_DIR", "CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF", "TMPDIR", "PATH"};
static const char *const REFUSED[] = {"LD_", "CONTAINERS_", "CONTAINER_", "PODMAN_", "_CONTAINERS_", "STORAGE_", "XDG_", "BUILDAH_", "REGISTRY_", "GODEBUG", "GOTRACEBACK", "GOMAXPROCS", "GOGC", "GOMEMLIMIT"};
static int user_ok(const char *v) { size_t k = strspn(v, "0123456789"); return k && (!v[k] || (v[k] == ':' && v[k + 1] && strspn(v + k + 1, "0123456789") == strlen(v + k + 1))); }
static int dir_ok(const char *v) { return v[0] == '/'; }
/* A variable name [A-Za-z_][A-Za-z0-9_]* before '=' or the end: its length, or 0. Never the token name. */
static size_t env_ok(const char *s) {
	size_t k = strspn(s, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_");
	return (k && (*s < '0' || *s > '9') && (!s[k] || s[k] == '=') && !(k == strlen(TOKEN) && !strncmp(s, TOKEN, k))) ? k : 0;
}
/* ADR-014 realm rule, [a-z][a-z0-9-]{0,31}: a realm name builds the lock and marker paths. */
static int realm_ok(const char *s) { size_t n = strlen(s); return n && n <= 32 && *s >= 'a' && *s <= 'z' && strspn(s, "abcdefghijklmnopqrstuvwxyz0123456789-") == n; }
/* The options allowed before `--`, two-token forms only. `bit` is the flag the option must agree with. */
static const struct opt { const char *name; int takes, bit; int (*ok)(const char *); } OPTS[] = {
	{"--tty", 0, GINN_EXEC_FL_TTY, NULL}, {"--interactive", 0, GINN_EXEC_FL_INTERACTIVE, NULL},
	{"--user", 1, 0, user_ok}, {"--workdir", 1, 0, dir_ok}, {"--env", 1, 0, NULL}};
struct req { unsigned flags; char *realm, cid[GINN_EXEC_CID_LEN + 1], *pass[GINN_EXEC_MAX_ITEMS], *args[GINN_EXEC_MAX_ITEMS]; int npass, nargs, dd; };
static const unsigned char *cur, *end; /* the REQ cursor */
static void die(const char *op) { fprintf(stderr, "ginn-exec-worker: %s: %s\n", op, strerror(errno)), exit(1); }
static void reject(const char *why) {
	unsigned char b[256];
	fprintf(stderr, "ginn-exec-worker: %s\n", why), frame_send(GINN_EXEC_CLIENT_FD, b, frame_reject(b, GINN_EXEC_REJ_REJECTED, why)), exit(0);
}
/* A u16-prefixed string, NUL-terminated; NULL when the record ends early or the string holds a NUL. */
static char *take(void) {
	size_t n = end - cur < 2 ? 0 : (size_t)(cur[0] << 8 | cur[1]);
	char *s = end - cur < 2 || (size_t)(end - cur - 2) < n ? NULL : malloc(n + 1);
	if (!s) return NULL;
	memcpy(s, cur + 2, n), s[n] = '\0', cur += 2 + n;
	return strlen(s) == n ? s : NULL;
}
/* A u16 count of at most GINN_EXEC_MAX_ITEMS. */
static int count(void) {
	size_t v = end - cur < 2 ? GINN_EXEC_MAX_ITEMS + 1 : (size_t)(cur[0] << 8 | cur[1]);
	if (v > GINN_EXEC_MAX_ITEMS) reject(BAD_REQ);
	return cur += 2, (int)v;
}
/* The argv: `exec`, the options, `--`, the REQ's container id, then the opaque command. q->dd is the index
 * of the first `--`, where the token goes. --tty and --interactive must agree with the flags. */
static int tail_ok(struct req *q) {
	int i = 1, j, seen = 0, n = (int)(sizeof OPTS / sizeof OPTS[0]);
	if (q->nargs < 3 || strcmp(q->args[0], "exec")) return 0;
	for (; i < q->nargs && strcmp(q->args[i], "--"); i++) {
		for (j = 0; j < n && strcmp(OPTS[j].name, q->args[i]); j++) {}
		if (j == n || (OPTS[j].takes && (++i >= q->nargs || !(OPTS[j].ok ? OPTS[j].ok(q->args[i]) : (int)env_ok(q->args[i])))))
			return 0;
		seen |= OPTS[j].bit;
	}
	q->dd = i;
	return i + 1 < q->nargs && !strcmp(q->args[i + 1], q->cid) && seen == (int)(q->flags & 3);
}
/* A pass-through entry: NAME=VALUE with a valid name, a value of at most 4 KiB, not refused (a REFUSED
 * entry ending in `_` is a prefix, any other is an exact name), and a bare `--env NAME` in the tail (ADR-014). */
static int pass_ok(const struct req *q, const char *s) {
	const char *eq = strchr(s, '=');
	size_t k = eq ? (size_t)(eq - s) : 0, i;
	if (!eq || !k || env_ok(s) != k || strlen(eq + 1) > GINN_EXEC_MAX_VALUE) return 0;
	for (i = 0; i < sizeof REFUSED / sizeof REFUSED[0]; i++) { size_t n = strlen(REFUSED[i]); if (REFUSED[i][n - 1] == '_' ? !strncmp(s, REFUSED[i], n) : n == k && !strncmp(s, REFUSED[i], n)) return 0; }
	for (i = 0; i + 1 < (size_t)q->dd; i++) /* only the podman options, before the container id */
		if (!strcmp(q->args[i], "--env") && strlen(q->args[i + 1]) == k && !strncmp(q->args[i + 1], s, k)) return 1;
	return 0;
}
/* Checks the REQ record (ADR-014, Wire protocol and Validation). Any fault is a REJECT. */
static void parse(unsigned char *rec, size_t n, int nfds, struct req *q) {
	int i;
	if (n < 4 || rec[0] != GINN_EXEC_REQ || rec[1] != GINN_EXEC_VERSION || (rec[2] & ~0x0f) || rec[3] != nfds ||
	    nfds != ((rec[2] & GINN_EXEC_FL_CAPTURED) ? 5 : 3)) reject(BAD_REQ);
	cur = rec + 4, end = rec + n, q->flags = rec[2], q->realm = take();
	if (!q->realm || !realm_ok(q->realm) || end - cur < GINN_EXEC_CID_LEN) reject(BAD_REQ);
	memcpy(q->cid, cur, GINN_EXEC_CID_LEN), q->cid[GINN_EXEC_CID_LEN] = '\0', cur += GINN_EXEC_CID_LEN;
	if (strspn(q->cid, "0123456789abcdef") != GINN_EXEC_CID_LEN || q->cid[GINN_EXEC_CID_LEN]) reject(BAD_REQ);
	for (q->npass = count(), i = 0; i < q->npass; i++) if (!(q->pass[i] = take())) reject(BAD_REQ);
	for (q->nargs = count(), i = 0; i < q->nargs; i++) if (!(q->args[i] = take())) reject(BAD_REQ);
	if (cur != end || !tail_ok(q)) reject(BAD_TAIL);
	for (i = 0; i < q->npass; i++) if (!pass_ok(q, q->pass[i])) reject(BAD_REQ);
}
int main(int argc, char **argv) {
	struct req q;
	unsigned char *rec = malloc(GINN_EXEC_MAX_RECORD), raw[16];
	char tok[33] = "", *tok_env = NULL, *run, flagn[12], fdn[5][12], **rv, **envp, *s;
	int fds[5], nfds = 0, i, j, t, m, own;
	size_t k;
	ssize_t got;
	if (argc < 3 || argv[0][0] != '/' || argv[1][0] != '/' || argv[2][0] != '/' || !rec)
		return fprintf(stderr, "usage: ginn-exec-worker <runtime-root> <podman> [global-arg...]\n"), 2;
	if ((got = frame_recv(GINN_EXEC_CLIENT_FD, rec, GINN_EXEC_MAX_RECORD, fds, &nfds)) <= 0) reject(BAD_REQ);
	parse(rec, (size_t)got, nfds, &q);
	if (q.flags & GINN_EXEC_FL_MUTATING) { /* 128 random bits as 32 lowercase hex characters */
		if (getrandom(raw, sizeof raw, 0) != (ssize_t)sizeof raw) die("getrandom");
		for (i = 0; i < 16; i++) snprintf(tok + 2 * i, 3, "%02x", raw[i]);
		if (asprintf(&tok_env, "%s=%s", TOKEN, tok) < 0) die("asprintf");
	}
	for (i = 0; i < nfds; i++) {
		if ((t = fcntl(fds[i], F_DUPFD, 10)) < 0) die("fcntl");
		fds[i] = t, snprintf(fdn[i], sizeof fdn[i], "%d", t);
	}
	if (asprintf(&run, "%.*s/ginn-exec-run", (int)(strrchr(argv[0], '/') - argv[0]), argv[0]) < 0) die("asprintf");
	snprintf(flagn, sizeof flagn, "%u", q.flags);
	rv = malloc((size_t)(argc + q.nargs + 12) * sizeof *rv), envp = malloc((size_t)(q.npass + 6) * sizeof *envp); if (!rv || !envp) die("malloc");
	j = 0;
	rv[j++] = run, rv[j++] = argv[1], rv[j++] = q.realm, rv[j++] = flagn, rv[j++] = tok;
	for (i = 0; i < nfds; i++) rv[j++] = fdn[i];
	rv[j++] = "--";
	for (i = 2; i < argc; i++) rv[j++] = argv[i];
	for (i = 0; i < q.nargs; i++) {
		if (i == q.dd && tok_env) rv[j++] = "--env", rv[j++] = tok_env;
		rv[j++] = q.args[i];
	}
	rv[j] = NULL;
	/* A pass-through name that is ginn's own loses to ginn's value (ADR-014, Environment). */
	for (i = 0, j = 0; i < q.npass; i++) {
		k = strcspn(q.pass[i], "=");
		for (own = 0, m = 0; m < 5; m++) own |= strlen(KEYS[m]) == k && !strncmp(KEYS[m], q.pass[i], k);
		if (!own) envp[j++] = q.pass[i];
	}
	for (i = 0; i < 5; i++) if ((s = getenv(KEYS[i])) && asprintf(&envp[j++], "%s=%s", KEYS[i], s) < 0) die("asprintf");
	envp[j] = NULL;
	execve(run, rv, envp);
	die("exec ginn-exec-run");
	return 1;
}
