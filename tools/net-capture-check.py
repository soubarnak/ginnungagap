#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Network capture against an allow-list (NFR-SEC-010, AS-022, release gate 6).

ginn makes no network connection except to image registries, distribution repositories and the
AUR. This tool captures the traffic of a workload and lists every destination that the allow-list
(tools/net-allowlist.txt) does not cover.

Modes:
  check --allowlist FILE --capture FILE
      Read a saved `tcpdump -nn -tt -l` text capture and report. Runs nothing else.
  run --allowlist FILE [--method tcpdump|ss] [--keep FILE] -- COMMAND [ARG...]
      Capture while COMMAND runs (an argv, never a shell), then report. The exit status of the
      tool is 0 only when COMMAND succeeded and the capture is clean.

The tcpdump method (default) lists the DNS names looked up, with their answers, and every TCP
connection attempt or non-DNS UDP flow. A destination is allowed when it is loopback, in a `cidr`
entry, or when it was returned for an allowed name. A looked-up name that the list does not cover
is a violation even if no connection followed. The capture is host-wide (`-i any`): traffic of
other programs shows too, so run it on a quiet host, or repeat --allowlist with a file that lists
the names of that background traffic (NTP, the desktop), kept outside the repository.
Capturing needs root or CAP_NET_RAW, so the tool runs `sudo -n timeout -s INT N tcpdump ...` when
it is not root. It never asks for a password. The sudoers rule must allow `timeout` with fixed
arguments, for example:

  u ALL=(root) NOPASSWD: /usr/bin/timeout -s INT * /usr/sbin/tcpdump -nn -tt -l -U -i any tcp or udp

A bare rule for `timeout` is full root (timeout runs any command): never grant that.

The ss method (no tcpdump) samples `ss` every 0.1 s for peer addresses. It is weaker: it sees
only flows that live across a sample, and it can match an address only through a forward lookup of
an exact allow-list name or a `cidr` entry. Use it as a fallback and say so in the report.

Gate 6 run (NFR-SEC-010 is verified by a capture during the full test suite): wrap the suite.

  tools/net-capture-check.py run --allowlist tools/net-allowlist.txt --keep /path/cap.txt \\
      --max-seconds 14400 -- env GINN_INTEGRATION=1 .venv/bin/pytest tests/integration -q

The command inherits GINN_NET_CAPTURE_ACTIVE=1, so the single-workload gate test skips (the outer
capture covers it). An empty or unparsable capture is NOT RUN (exit 2), never a pass.

Exit status: 0 clean, 1 violations (or the workload failed), 2 usage error, capture failure or
an empty capture.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import os
import re
import shutil
import signal
import socket
import subprocess  # noqa: S404 - fixed argv lists, never a shell (NFR-SEC-005)
import sys
import tempfile
import threading
import time
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

DNS_PORTS = frozenset({"53", "853"})
ACTIVE_ENV = "GINN_NET_CAPTURE_ACTIVE"
# `-i any` prints `<ts> <ifname> In|Out IP ...` (tcpdump 4.99+, SLL2) or `<ts> IP ...`: search.
_INBOUND = re.compile(r"\s(?:In|M)\s+IP6?\s")  # SLL2 direction: In (host-bound), M (multicast)
_LINE = re.compile(r"\bIP6? (\S+) > (\S+):\s+(.*)$")
_DNS_QUERY = re.compile(r"\b(\d+)\+?(?:\||%|\*|-)* (?:\[[^\]]*\] )*([A-Za-z0-9]+)\? (\S+)")
_DNS_ANSWER_HEAD = re.compile(r"^(\d+)\*?[-|%\$]* (?:\[[^\]]*\] )*(\d+)/(\d+)/(\d+) (.*)$")
_DNS_RECORD = re.compile(r"\b(A|AAAA) ([0-9A-Fa-f:.]+)")
_QTYPE_SKIP = frozenset({"PTR"})


class AllowListError(ValueError):
    """A line of the allow-list file that cannot be read."""


@dataclass(frozen=True)
class AllowList:
    exact: frozenset[str] = frozenset()
    suffixes: tuple[str, ...] = ()
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    def allows_name(self, name: str) -> bool:
        host = normalize_name(name)
        if host in self.exact:
            return True
        return any(host.endswith("." + suffix) for suffix in self.suffixes)

    def allows_address(self, address: str) -> bool:
        ip = parse_ip(address)
        if ip is None:
            return False
        if ip.is_loopback:
            return True
        return any(ip in net for net in self.networks if net.version == ip.version)


def normalize_name(name: str) -> str:
    return name.strip().rstrip(".").lower()


def parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


def parse_allowlist(text: str) -> AllowList:
    """Parse the allow-list text. A bad line raises AllowListError with its line number."""
    exact: set[str] = set()
    suffixes: list[str] = []
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "cidr":
            if len(parts) != 2:
                raise AllowListError(f"line {number}: 'cidr' takes one network")
            try:
                networks.append(ipaddress.ip_network(parts[1], strict=False))
            except ValueError as exc:
                raise AllowListError(f"line {number}: {exc}") from None
            continue
        if len(parts) != 1:
            raise AllowListError(f"line {number}: one entry per line, got {line!r}")
        entry = normalize_name(parts[0])
        if entry.startswith("*."):
            base = entry[2:]
            if not _valid_host(base):
                raise AllowListError(f"line {number}: bad wildcard {parts[0]!r}")
            suffixes.append(base)
        elif _valid_host(entry):
            exact.add(entry)
        else:
            raise AllowListError(f"line {number}: bad host name {parts[0]!r}")
    return AllowList(frozenset(exact), tuple(suffixes), tuple(networks))


def _valid_host(name: str) -> bool:
    return bool(
        re.fullmatch(r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)+", name)
    )


@dataclass
class Capture:
    """What a capture showed. `names` is every looked-up name; `answers` maps address to names."""

    names: set[str] = field(default_factory=set)
    answers: dict[str, set[str]] = field(default_factory=dict)
    dests: set[tuple[str, str]] = field(default_factory=set)  # (address, port) of flows
    packets: int = 0  # lines that were parsed as packets; 0 means the capture proves nothing


@dataclass(frozen=True)
class Violation:
    kind: str  # "dns" for a looked-up name, "flow" for a destination
    target: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.target} ({self.detail})"


def split_endpoint(endpoint: str) -> tuple[str, str]:
    """Split a tcpdump `address.port` into (address, port). The port may be empty."""
    host, sep, port = endpoint.rpartition(".")
    if not sep:
        return endpoint, ""
    if port.isdigit() and (":" in host or host.count(".") == 3):
        return host, port
    return endpoint, ""  # no port (e.g. an IPv4 address alone)


def system_resolvers(resolv_conf: Path = Path("/etc/resolv.conf")) -> frozenset[str]:
    """The nameserver addresses of resolv.conf. Missing or unreadable gives an empty set."""
    found: set[str] = set()
    try:
        for line in resolv_conf.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                found.add(parts[1].split("%", 1)[0])
    except OSError:
        pass
    return frozenset(found)


def parse_tcpdump(text: str, resolvers: Collection[str] | None = None) -> Capture:
    """Read `tcpdump -nn -tt` text. Unknown lines are skipped; the format is tcpdump's own.

    `resolvers` are the addresses that may receive DNS traffic (port 53 and 853). A DNS packet to
    any other non-loopback address is a flow destination like any other. None accepts any address.
    """
    cap = Capture()
    pending: dict[str, str] = {}  # dns id -> looked-up name
    for raw in text.splitlines():
        match = _LINE.search(raw.strip())
        if match is None:
            continue
        cap.packets += 1
        src, dst, rest = match.groups()
        _src_ip, src_port = split_endpoint(src)
        dst_ip, dst_port = split_endpoint(dst)
        if dst_port in DNS_PORTS:
            if resolvers is not None and dst_ip not in resolvers:
                cap.dests.add((dst_ip, dst_port))
            query = _DNS_QUERY.search(rest)
            if query is not None:
                dns_id, qtype, name = query.groups()
                if qtype.upper() not in _QTYPE_SKIP:
                    host = normalize_name(name)
                    cap.names.add(host)
                    pending[dns_id] = host
            continue
        if src_port in DNS_PORTS:
            head = _DNS_ANSWER_HEAD.match(rest.strip().split(" (")[0].strip())
            name_for_id = pending.get(head.group(1)) if head else None
            if head is not None and name_for_id is not None:
                for _kind, address in _DNS_RECORD.findall(head.group(5)):
                    cap.answers.setdefault(address, set()).add(name_for_id)
            continue
        # A TCP connection attempt is a bare SYN; any other TCP segment belongs to a flow already
        # seen. Everything without `Flags [` is UDP (or another datagram): its first packet counts.
        if _INBOUND.search(raw):
            continue  # a packet that arrived here is not a connection this host made
        if "Flags [S]" in rest or "Flags [" not in rest:
            cap.dests.add((dst_ip, dst_port))
    return cap


def evaluate(cap: Capture, allow: AllowList) -> list[Violation]:
    """Every looked-up name and flow destination that the allow-list does not cover."""
    found: list[Violation] = []
    for name in sorted(cap.names):
        if not allow.allows_name(name):
            found.append(Violation("dns", name, "name is not in the allow-list"))
    for address, port in sorted(cap.dests):
        if allow.allows_address(address):
            continue
        names = cap.answers.get(address, set())
        if names and all(allow.allows_name(n) for n in names):
            continue
        if names:
            bad = ", ".join(sorted(n for n in names if not allow.allows_name(n)))
            found.append(Violation("flow", f"{address}:{port}", f"reached through {bad}"))
        else:
            found.append(Violation("flow", f"{address}:{port}", "no allowed name returned it"))
    return found


def parse_ss(text: str) -> set[tuple[str, str]]:
    """Peer (address, port) pairs from `ss -H -n -t -u -a` output; empty peers are skipped."""
    peers: set[tuple[str, str]] = set()
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 6:
            continue
        peer = cols[5]
        host, sep, port = peer.rpartition(":")
        if not sep or port == "*" or host in {"*", "0.0.0.0", "[::]", "::", ""}:  # noqa: S104 - a peer filter, no bind
            continue
        peers.add((host.strip("[]"), port))
    return peers


def evaluate_ss(
    peers: Iterable[tuple[str, str]], allow: AllowList, resolved: dict[str, set[str]]
) -> list[Violation]:
    """ss method: `resolved` maps an allowed exact name's address to the names that gave it."""
    found: list[Violation] = []
    for address, port in sorted(set(peers)):
        if port in DNS_PORTS or allow.allows_address(address) or address in resolved:
            continue
        found.append(Violation("flow", f"{address}:{port}", "not a resolved allow-list name"))
    return found


def resolve_exact(allow: AllowList) -> dict[str, set[str]]:
    """Forward-resolve the exact names of the list (ss method). Failures are skipped."""
    out: dict[str, set[str]] = {}
    for name in sorted(allow.exact):
        try:
            infos = socket.getaddrinfo(name, None)
        except OSError:
            continue
        for info in infos:
            out.setdefault(str(info[4][0]), set()).add(name)
    return out


def _capture_prefix() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


# TCP and UDP only, IPv4 and IPv6 alike. `tcp[tcpflags]` would be IPv4-only, so the SYN selection
# is left to the parser, which keeps only `Flags [S]` lines.
BPF_FILTER = "tcp or udp"


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _descendants(root: int) -> set[int]:
    """The pids below `root` in /proc (children, grandchildren, ...). Unreadable entries skip."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / "stat").read_text(encoding="utf-8").rpartition(")")[2].split()
            parents[int(entry.name)] = int(tail[1])  # field 4 of stat: the parent pid
        except (OSError, ValueError, IndexError):
            continue
    found: set[int] = set()
    todo = [root]
    while todo:
        current = todo.pop()
        for pid, parent in parents.items():
            if parent == current and pid not in found:
                found.add(pid)
                todo.append(pid)
    return found


def _run_tcpdump(argv: Sequence[str], out: Path, max_seconds: int) -> int | None:
    """Run `argv` under tcpdump and return its status, or None when no capture could start.

    The time limit is in the privileged argv (`sudo -n timeout -s INT N tcpdump`), so a root
    tcpdump ends by itself even when this tool is killed. sudo is only ever asked for `timeout`,
    which a sudoers rule can allow with tcpdump alone. After the run the tool checks that the
    processes below sudo are gone; it cannot signal root processes, so a survivor is reported and
    ends at the time limit.
    """
    tcpdump, timeout = shutil.which("tcpdump"), shutil.which("timeout")
    if tcpdump is None or timeout is None:
        print(
            "net-capture-check: tcpdump and timeout are needed (or use --method ss)",
            file=sys.stderr,
        )
        return None
    cmd = [
        *_capture_prefix(),
        timeout,
        "-s",
        "INT",
        str(max_seconds),
        tcpdump,
        "-nn",
        "-tt",
        "-l",
        "-U",
        "-i",
        "any",
        BPF_FILTER,
    ]
    previous = signal.signal(signal.SIGTERM, _raise_exit)
    try:
        with out.open("w", encoding="utf-8") as sink, tempfile.TemporaryFile() as err:
            dump = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                cmd, stdout=sink, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True
            )
            below: set[int] = set()
            try:
                time.sleep(1.5)  # tcpdump needs a moment to open the interface
                if dump.poll() is not None:
                    err.seek(0)
                    text = err.read().decode(errors="replace")
                    print(f"net-capture-check: tcpdump failed: {text}", file=sys.stderr)
                    return None
                below = _descendants(dump.pid)
                status = subprocess.run(list(argv), check=False).returncode  # noqa: S603
                time.sleep(1.0)  # let the last packets reach the file
                return status
            finally:
                _stop(dump, below)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _raise_exit(_signum: int, _frame: object) -> None:
    raise SystemExit(143)  # lets the finally clauses run, so tcpdump is stopped


def _stop(proc: subprocess.Popen[bytes], below: set[int]) -> None:
    """Stop tcpdump with SIGINT so it flushes, then verify nothing of it is left running."""
    with contextlib.suppress(OSError):
        proc.send_signal(signal.SIGINT)  # sudo relays it to timeout, which relays it to tcpdump
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait()
    deadline = time.monotonic() + 5
    while any(_pid_alive(pid) for pid in below) and time.monotonic() < deadline:
        time.sleep(0.1)
    left = sorted(pid for pid in below if _pid_alive(pid))
    if left:
        print(
            f"net-capture-check: capture process(es) {left} still run; they end at the time limit",
            file=sys.stderr,
        )


def _run_ss(argv: Sequence[str]) -> tuple[int | None, set[tuple[str, str]]]:
    ss = shutil.which("ss")
    if ss is None:
        print("net-capture-check: neither tcpdump nor ss is installed", file=sys.stderr)
        return None, set()
    peers: set[tuple[str, str]] = set()
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            done = subprocess.run(  # noqa: S603 - fixed argv
                [ss, "-H", "-n", "-t", "-u", "-a"], capture_output=True, text=True, check=False
            )
            peers.update(parse_ss(done.stdout))
            stop.wait(0.1)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        status = subprocess.run(list(argv), check=False).returncode  # noqa: S603
    finally:
        stop.set()
        thread.join(timeout=5)
    return status, peers


def report(violations: Sequence[Violation], *, source: str, packets: int | None = None) -> int:
    """Print the verdict. A capture with zero parsed packets is NOT RUN (exit 2), never clean."""
    if packets == 0:
        print(f"net-capture-check: NOT RUN, no packet was parsed from the capture ({source})")
        return 2
    if not violations:
        print(f"net-capture-check: clean ({source})")
        return 0
    print(f"net-capture-check: {len(violations)} destination(s) outside the allow-list ({source})")
    for item in violations:
        print(f"  {item}")
    return 1


def merge(lists: Sequence[AllowList]) -> AllowList:
    """One list that allows whatever any of `lists` allows."""
    return AllowList(
        frozenset().union(*(item.exact for item in lists)),
        tuple(s for item in lists for s in item.suffixes),
        tuple(n for item in lists for n in item.networks),
    )


def _load_allowlists(paths: Sequence[str]) -> AllowList:
    return merge([parse_allowlist(Path(p).read_text(encoding="utf-8")) for p in paths])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    chk = sub.add_parser("check", help="check a saved tcpdump text capture")
    chk.add_argument("--allowlist", required=True, action="append")
    chk.add_argument("--capture", required=True)
    run = sub.add_parser("run", help="capture while a command runs, then check")
    run.add_argument(
        "--allowlist",
        required=True,
        action="append",
        help="allow-list file; repeat it to add host-specific entries (the capture is host-wide)",
    )
    run.add_argument("--method", choices=("tcpdump", "ss"), default="tcpdump")
    run.add_argument("--keep", help="write the raw tcpdump text here (tcpdump method)")
    run.add_argument(
        "--max-seconds",
        type=int,
        default=3300,
        help="time limit of the privileged tcpdump itself (default 3300); keep it below the "
        "timeout of the caller",
    )
    for sub_parser in (chk, run):
        sub_parser.add_argument(
            "--resolver",
            action="append",
            default=[],
            help="extra address that may receive DNS traffic (resolv.conf is always read)",
        )
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        allow = _load_allowlists(args.allowlist)
    except (OSError, AllowListError) as exc:
        print(f"net-capture-check: {exc}", file=sys.stderr)
        return 2
    if args.mode == "check":
        try:
            text = Path(args.capture).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"net-capture-check: {exc}", file=sys.stderr)
            return 2
        cap = parse_tcpdump(text, system_resolvers() | set(args.resolver))
        return report(evaluate(cap, allow), source=args.capture, packets=cap.packets)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("net-capture-check: run needs a command after --", file=sys.stderr)
        return 2
    if args.method == "ss":
        status, peers = _run_ss(command)
        if status is None:
            return 2
        code = report(evaluate_ss(peers, allow, resolve_exact(allow)), source="ss samples")
    else:
        scratch = Path(tempfile.mkdtemp(prefix="netcap-"))
        keep = Path(args.keep) if args.keep else scratch / "cap.txt"
        try:
            os.environ[ACTIVE_ENV] = "1"  # the command inherits it: a capture is already running
            status = _run_tcpdump(command, keep, args.max_seconds)
            if status is None:
                return 2
            text = keep.read_text(encoding="utf-8", errors="replace")
            cap = parse_tcpdump(text, system_resolvers() | set(args.resolver))
            code = report(evaluate(cap, allow), source=str(keep), packets=cap.packets)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)  # the capture stays only with --keep
    if code == 2:
        return 2
    if status != 0:
        print(f"net-capture-check: the workload exited with status {status}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
