#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-04 (CL-1, ADR-004) and S-08 (CL-5): does a rootless podman runtime root survive logout,
# and does a container's bind-mounted session socket follow a re-created socket?
# Host-only. Uses a throwaway user ginn-spike and a private sshd on 127.0.0.1:2222.
# Never touches /var/service, installs packages, or enables services.
#
#   root-setup      (root) create ginn-spike, subuid/subgid lines, key, start private sshd
#   root-ginn-dir   (root) /run/ginn/<uid> mode 0700, owned by ginn-spike
#   login-open      open one ssh ControlMaster connection (one login session)
#   precheck        precondition: session registered and /run/user/<uid> present
#   s04-start       pull alpine once, load it into the other storages, start s04a and s04b
#   login-close     full logout (close the ControlMaster)
#   root-observe    (root, read-only) sessions, runtime dir and processes after logout
#   s04-verify      podman ps -a and exec with each variant's exact flags
#   s08-start       gen1 socket server at /run/user/<uid>/s08.sock; container s08c binds it
#   s08-server2     gen2 socket server at the same path, after a new login
#   s08-probe       connect from inside s08c; print socket inode and server reply
#   s08-rebind      rebind gen2 into s08c: podman unshare first, then session namespaces
#   teardown        (session) remove containers, images, storage and socket servers
#   root-teardown   (root) stop sshd, remove /run trees, user, subuid/subgid lines; diff
#
# Every podman call passes --root, --runroot and --tmpdir at spike-owned paths. Every root
# command is appended to $OUT/root-commands.log before it runs. Set GINN_SPIKE_OUT to the
# scratch directory; raw logs stay out of the repository.
set -euo pipefail

SELF_DIR=$(dirname "$(readlink -f "$0")")
OUT=${GINN_SPIKE_OUT:-$HOME/.cache/ginn-spike/s04}
USER_NAME=ginn-spike
HOME_S=/home/$USER_NAME
SUB_START=300000          # range 300000-365535; existing entries are 100000, 200000, 1000000
SSHD_PORT=2222
# ssh ControlMaster socket: sun_path is limited to 108 bytes, so it lives in a short directory.
CTL_DIR=${GINN_SPIKE_CTL:-$HOME/.cache/ginn-s04-ctl}
CM=$CTL_DIR/cm.sock
mkdir -p "$CTL_DIR"
ROOTLOG=$OUT/root-commands.log
IMAGE=docker.io/library/alpine:3
TAR=$HOME_S/s04/alpine.tar
SSH_OPTS=(-o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
          -o UserKnownHostsFile="$OUT/known_hosts" -o ControlPath="$CM"
          -i "$OUT/id_ed25519" -p "$SSHD_PORT")

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
spike_uid() { id -u "$USER_NAME" 2>/dev/null || die "user $USER_NAME does not exist"; }

# root <argv...>: log the exact command, then run it with sudo (never a shell string).
root() {
  mkdir -p "$OUT"
  printf '%s sudo %s\n' "$(date -Is)" "$*" >>"$ROOTLOG"
  sudo -n "$@"
}

# rx <command>: run a command in the ginn-spike login session over the ControlMaster.
rx() {
  [[ -S $CM ]] || die "no login session open; run login-open first"
  ssh -o ControlMaster=no "${SSH_OPTS[@]}" "$USER_NAME@127.0.0.1" \
    "export PATH=/usr/sbin:/usr/bin:/sbin:/bin; $1"
}

# pm_str <root> <runroot>: explicit --root, --runroot and --tmpdir, and the crun runtime.
pm_str() { printf 'podman --root %s --runroot %s --tmpdir %s/libpod-tmp --runtime crun' "$1" "$2" "$2"; }
# Variant A: runroot and tmpdir under /run/ginn/<uid>, XDG_RUNTIME_DIR set to the same tree.
pm_a() { local u; u=$(spike_uid); echo "env XDG_RUNTIME_DIR=/run/ginn/$u $(pm_str "$HOME_S/s04/a/root" "/run/ginn/$u/a")"; }
# Variant B: runroot and tmpdir under the session default /run/user/<uid>.
pm_b() { local u; u=$(spike_uid); echo "$(pm_str "$HOME_S/s04/b/root" "/run/user/$u/ginn-s04-b")"; }
# S-08 container: flags as variant A, own storage.
pm_s8() { local u; u=$(spike_uid); echo "env XDG_RUNTIME_DIR=/run/ginn/$u $(pm_str "$HOME_S/s04/s08/root" "/run/ginn/$u/s08")"; }

push_helpers() {
  rx "mkdir -p $HOME_S/s04"
  rx "cat > $HOME_S/s04/s04_s08_sockserver.py" < "$SELF_DIR/s04_s08_sockserver.py"
}

cmd_root_setup() {
  mkdir -p "$OUT/orig"
  [[ -f $OUT/orig/subuid ]] || cp -p /etc/subuid /etc/subgid "$OUT/orig/"
  if ! awk -F: -v s="$SUB_START" -v e="$((SUB_START + 65535))" -v u="$USER_NAME" \
      '$1 != u { if ($2 <= e && s <= $2 + $3 - 1) bad = 1 } END { exit bad }' /etc/subuid /etc/subgid; then
    die "candidate subordinate range overlaps an existing entry"
  fi
  if id -u "$USER_NAME" >/dev/null 2>&1; then
    grep -q "sudo useradd -m $USER_NAME" "$ROOTLOG" 2>/dev/null \
      || die "user $USER_NAME exists and this spike did not create it"
    log "user $USER_NAME already created by an earlier root-setup run"
  else
    root useradd -m "$USER_NAME"
  fi
  # useradd may auto-allocate a range; the brief asks for hand-added lines, so drop any auto line.
  if grep -q "^$USER_NAME:" /etc/subuid /etc/subgid; then
    root sed -i "/^$USER_NAME:/d" /etc/subuid /etc/subgid
  fi
  printf '%s:%s:65536\n' "$USER_NAME" "$SUB_START" | root tee -a /etc/subuid >/dev/null
  printf '%s:%s:65536\n' "$USER_NAME" "$SUB_START" | root tee -a /etc/subgid >/dev/null
  [[ -f $OUT/id_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C ginn-spike-s04 -f "$OUT/id_ed25519"
  [[ -f $OUT/ssh_host_ed25519_key ]] || ssh-keygen -q -t ed25519 -N '' -C ginn-s04-host -f "$OUT/ssh_host_ed25519_key"
  root install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_S/.ssh"
  root install -m 0600 -o "$USER_NAME" -g "$USER_NAME" "$OUT/id_ed25519.pub" "$HOME_S/.ssh/authorized_keys"
  cat >"$OUT/sshd_config" <<EOF
HostKey $OUT/ssh_host_ed25519_key
PidFile $OUT/sshd.pid
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers $USER_NAME
PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM yes
StrictModes yes
LogLevel VERBOSE
EOF
  if [[ ! -d /run/sshd ]]; then
    root mkdir -m 0755 /run/sshd
    echo created >"$OUT/created-run-sshd"
  fi
  root /usr/sbin/sshd -f "$OUT/sshd_config" -E "$OUT/sshd.log" \
    -o ListenAddress=127.0.0.1 -o Port="$SSHD_PORT" -o PasswordAuthentication=no -o UsePAM=yes
  sleep 1
  (exec 3<>"/dev/tcp/127.0.0.1/$SSHD_PORT") 2>/dev/null && log "private sshd listening on 127.0.0.1:$SSHD_PORT" \
    || die "private sshd not listening; see $OUT/sshd.log"
  log "root-setup done, uid $(spike_uid)"
}

cmd_root_ginn_dir() {
  local u; u=$(spike_uid)
  [[ -e /run/ginn ]] && die "/run/ginn already exists; not created by this spike"
  root mkdir -m 0755 /run/ginn
  echo created >"$OUT/created-run-ginn"
  root mkdir -m 0700 "/run/ginn/$u"
  root chown "$USER_NAME:$USER_NAME" "/run/ginn/$u"
  root chmod 0700 "/run/ginn/$u"
  ls -ld /run/ginn "/run/ginn/$u"
}

cmd_login_open() {
  [[ -S $CM ]] && die "ControlMaster already open at $CM"
  ssh -M -f -N -o ControlPersist=yes "${SSH_OPTS[@]}" "$USER_NAME@127.0.0.1"
  log "login session opened (ControlMaster $CM)"
}

cmd_login_close() {
  ssh -O exit -o ControlPath="$CM" "$USER_NAME@127.0.0.1" || true
  log "login session closed"
}

cmd_precheck() {
  local u sess; u=$(spike_uid)
  sess=$(rx 'loginctl list-sessions --no-legend')
  echo "== loginctl list-sessions (inside login)"; echo "$sess"
  if ! awk -v n="$USER_NAME" '$3 == n { found = 1 } END { exit !found }' <<<"$sess"; then
    echo "invalid: no elogind session"; exit 3
  fi
  rx "id -un; echo XDG_RUNTIME_DIR=\${XDG_RUNTIME_DIR:-unset}; ls -ld /run/user/$u; findmnt -no FSTYPE,TARGET /run/user/$u; cat /proc/self/cgroup"
  rx "test -d /run/user/$u" || { echo "invalid: /run/user/$u missing"; exit 3; }
  echo "== elogind KillUserProcesses (host config)"
  grep -n KillUserProcesses /etc/elogind/logind.conf || true
  echo "== PAM session modules present"
  ls /usr/lib/security | grep -E 'elogind|dumb_runtime|turnstile' || true
  echo "precheck: ok"
}

cmd_s04_start() {
  local u; u=$(spike_uid)
  rx "mkdir -p $HOME_S/s04 /run/ginn/$u/a /run/ginn/$u/s08 /run/user/$u/ginn-s04-b"
  rx "$(pm_a) pull $IMAGE"
  rx "$(pm_a) save -o $TAR $IMAGE"
  rx "$(pm_b) load -i $TAR"
  rx "$(pm_s8) load -i $TAR"
  rx "$(pm_a) run -d --name s04a --cgroups disabled --network none $IMAGE sleep infinity </dev/null"
  rx "$(pm_b) run -d --name s04b --cgroups disabled --network none $IMAGE sleep infinity </dev/null"
  push_helpers
  rx "cat /proc/self/cgroup; $(pm_a) ps -a; $(pm_b) ps -a"
}

cmd_root_observe() {
  local u; u=$(spike_uid)
  root loginctl list-sessions
  root loginctl user-status "$USER_NAME" || true
  root ls -ld "/run/user/$u" || echo "/run/user/$u: absent"
  root ps -u "$USER_NAME" -o pid,ppid,stat,args || true
}

cmd_s04_verify() {
  echo "== variant A (XDG_RUNTIME_DIR=/run/ginn/<uid>)"
  rx "$(pm_a) ps -a --format '{{.Names}} {{.State}} {{.Status}}'" || echo "rc=$?"
  rx "$(pm_a) inspect -f '{{.Name}} {{.State.Status}} pid={{.State.Pid}}' s04a" || echo "rc=$?"
  rx "$(pm_a) exec s04a echo exec-ok" || echo "rc=$?"
  echo "== variant B (XDG_RUNTIME_DIR default, /run/user/<uid>)"
  rx "$(pm_b) ps -a --format '{{.Names}} {{.State}} {{.Status}}'" || echo "rc=$?"
  rx "$(pm_b) inspect -f '{{.Name}} {{.State.Status}} pid={{.State.Pid}}' s04b" || echo "rc=$?"
  rx "$(pm_b) exec s04b echo exec-ok" || echo "rc=$?"
}

cmd_s08_start() {
  local u; u=$(spike_uid)
  push_helpers
  rx "setsid -f /usr/sbin/python3 -I $HOME_S/s04/s04_s08_sockserver.py /run/user/$u/s08.sock gen1 >$HOME_S/s04/gen1.log 2>&1 </dev/null"
  sleep 1
  rx "test -S /run/user/$u/s08.sock && echo socket-ok"
  rx "$(pm_s8) run -d --name s08c --cgroups disabled --network none -v /run/user/$u/s08.sock:/run/s08.sock $IMAGE sleep infinity </dev/null"
  rx "$(pm_s8) exec -i s08c sh -s" < "$SELF_DIR/s04_s08_probe.sh" || echo "rc=$?"
}

cmd_s08_server2() {
  local u; u=$(spike_uid)
  push_helpers
  rx "setsid -f /usr/sbin/python3 -I $HOME_S/s04/s04_s08_sockserver.py /run/user/$u/s08.sock gen2 >$HOME_S/s04/gen2.log 2>&1 </dev/null"
  sleep 1
  rx "cat $HOME_S/s04/gen2.log; test -S /run/user/$u/s08.sock && echo socket-ok"
}

cmd_s08_probe() {
  rx "$(pm_s8) exec -i s08c sh -s" < "$SELF_DIR/s04_s08_probe.sh" || echo "rc=$?"
}

cmd_s08_rebind() {
  local u pid; u=$(spike_uid)
  pid=$(rx "$(pm_s8) inspect -f '{{.State.Pid}}' s08c")
  log "s08c init pid=$pid"
  echo "== rebind 1: podman unshare (user's own userns and mount ns)"
  rx "$(pm_s8) unshare /usr/sbin/python3 -I - $pid /run/user/$u/s08.sock /run/s08.sock" \
    < "$SELF_DIR/s04_s08_rebind.py" || echo "rebind1 rc=$?"
  cmd_s08_probe
  echo "== rebind 2: session namespaces (no podman unshare)"
  rx "/usr/sbin/python3 -I - $pid /run/user/$u/s08.sock /run/s08.sock" \
    < "$SELF_DIR/s04_s08_rebind.py" || echo "rebind2 rc=$?"
  cmd_s08_probe
}

cmd_teardown() {
  if ! id -u "$USER_NAME" >/dev/null 2>&1; then
    log "teardown: user absent, nothing to remove in the session"
  else
    local u; u=$(spike_uid)
    cmd_login_open
    rx "pkill -u $USER_NAME -f s04_s08_sockserver.py || true"
    for v in a b s8; do
      case $v in
        a)  pmv=pm_a;  root_p="$HOME_S/s04/a/root";     run_p="/run/ginn/$u/a" ;;
        b)  pmv=pm_b;  root_p="$HOME_S/s04/b/root";     run_p="/run/user/$u/ginn-s04-b" ;;
        s8) pmv=pm_s8; root_p="$HOME_S/s04/s08/root";   run_p="/run/ginn/$u/s08" ;;
      esac
      rx "$($pmv) rm -af; $($pmv) rmi -af; $($pmv) unshare rm -rf $root_p $run_p" || log "teardown $v: podman cleanup rc=$?"
      # Fallback, as S-07c: plain rm only when every entry is owned by ginn-spike.
      rx "if [ -e $root_p ] || [ -e $run_p ]; then if [ -z \"\$(find $root_p $run_p ! -user $USER_NAME 2>/dev/null)\" ]; then rm -rf $root_p $run_p; fi; fi"
    done
    rx "ls -la $HOME_S/s04 2>/dev/null; ls -d /run/user/$u/ginn-s04-b /run/ginn/$u 2>/dev/null || true"
    cmd_login_close
  fi
  log "teardown: session cleanup done"
}

# root-teardown: sshd, leftover processes, /run trees created by this spike, the user, and
# the subuid/subgid lines. Each root command is logged. Diffs against the saved originals last.
cmd_root_teardown() {
  local u sshd_pid=""
  u=$(id -u "$USER_NAME" 2>/dev/null || echo none)
  [[ -f $OUT/sshd.pid ]] && sshd_pid=$(cat "$OUT/sshd.pid")
  if [[ -n $sshd_pid && -d /proc/$sshd_pid ]]; then root kill "$sshd_pid"; fi
  sleep 1
  if (exec 3<>"/dev/tcp/127.0.0.1/$SSHD_PORT") 2>/dev/null; then die "private sshd still listening"; fi
  log "private sshd stopped"
  if [[ $u != none ]] && pgrep -u "$USER_NAME" >/dev/null; then
    pgrep -u "$USER_NAME" -a || true
    root pkill -KILL -u "$USER_NAME" || true
    sleep 1
  fi
  if [[ -f $OUT/created-run-ginn ]]; then
    [[ -z $(ls -A /run/ginn | grep -v "^$u\$") ]] || die "/run/ginn has entries this spike did not create"
    root rm -rf /run/ginn
  fi
  if [[ $u != none && -d /run/user/$u ]]; then
    root rm -rf "/run/user/$u"
  fi
  if [[ $u != none ]]; then root userdel -r "$USER_NAME"; fi
  root sed -i "/^$USER_NAME:/d" /etc/subuid /etc/subgid
  # useradd re-sorted both files when it allocated ids, so the line set can match while the order does not.
  # Restore the saved originals byte for byte (root-owned, original mode).
  if ! cmp -s "$OUT/orig/subuid" /etc/subuid || ! cmp -s "$OUT/orig/subgid" /etc/subgid; then
    root install -m 0644 -o root -g root "$OUT/orig/subuid" /etc/subuid
    root install -m 0644 -o root -g root "$OUT/orig/subgid" /etc/subgid
  fi
  if [[ -f $OUT/created-run-sshd ]]; then root rmdir /run/sshd; fi
  echo "== after cleanup"
  getent passwd "$USER_NAME" || echo "passwd: $USER_NAME absent"
  ls -d /home/$USER_NAME /run/ginn /run/sshd "/run/user/$u" 2>&1 || true
  diff "$OUT/orig/subuid" /etc/subuid && diff "$OUT/orig/subgid" /etc/subgid \
    && echo "subuid and subgid identical to the saved originals"
}

case "${1:-}" in
  root-setup)    cmd_root_setup ;;
  root-ginn-dir) cmd_root_ginn_dir ;;
  login-open)    cmd_login_open ;;
  precheck)      cmd_precheck ;;
  s04-start)     cmd_s04_start ;;
  login-close)   cmd_login_close ;;
  root-observe)  cmd_root_observe ;;
  s04-verify)    cmd_s04_verify ;;
  s08-start)     cmd_s08_start ;;
  s08-server2)   cmd_s08_server2 ;;
  s08-probe)     cmd_s08_probe ;;
  s08-rebind)    cmd_s08_rebind ;;
  teardown)      cmd_teardown ;;
  root-teardown) cmd_root_teardown ;;
  *) die "usage: $0 {root-setup|root-ginn-dir|login-open|precheck|s04-start|login-close|root-observe|s04-verify|s08-start|s08-server2|s08-probe|s08-rebind|teardown|root-teardown}" ;;
esac
