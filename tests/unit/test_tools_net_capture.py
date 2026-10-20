# SPDX-License-Identifier: Apache-2.0
"""tools/net-capture-check.py (T10.5): the allow-list parser and the capture comparison.

The captures here are fixture text in the format of `tcpdump -nn -tt`. No test captures traffic,
starts tcpdump or opens a socket. The shipped tools/net-allowlist.txt is parsed as a fixture too.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "net-capture-check.py"
SHIPPED = REPO / "tools" / "net-allowlist.txt"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("net_capture_check", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["net_capture_check"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


nc = _load()

ALLOW = nc.parse_allowlist(
    """
    # registries
    quay.io
    *.quay.io          # CDN
    *.debian.org
    cidr 192.0.2.0/24
    """
)

CLEAN = """\
1.0 IP 10.0.0.2.40000 > 10.0.0.1.53: 1111+ A? quay.io. (25)
1.1 IP 10.0.0.1.53 > 10.0.0.2.40000: 1111 2/0/1 A 3.3.3.3, A 3.3.3.4 (70)
1.2 IP 10.0.0.2.50000 > 3.3.3.3.443: Flags [S], seq 1, win 64240, length 0
1.3 IP 10.0.0.2.50000 > 3.3.3.3.443: Flags [.], ack 1, win 502, length 0
1.4 IP 10.0.0.2.40001 > 10.0.0.1.53: 2222+ AAAA? cdn01.quay.io. (30)
1.5 IP 10.0.0.1.53 > 10.0.0.2.40001: 2222 1/0/1 AAAA 2001:db8::7 (60)
1.6 IP6 2001:db8::2.50001 > 2001:db8::7.443: Flags [S], seq 1, win 64800, length 0
1.7 IP 127.0.0.1.50002 > 127.0.0.1.8080: Flags [S], seq 1, win 65495, length 0
1.8 IP 10.0.0.2.50003 > 192.0.2.9.80: Flags [S], seq 1, win 64240, length 0
"""


@pytest.mark.fr("NFR-SEC-010")
def test_allowlist_matches_exact_wildcard_and_cidr() -> None:
    assert ALLOW.allows_name("quay.io")
    assert ALLOW.allows_name("QUAY.IO.")  # case and the trailing dot do not matter
    assert ALLOW.allows_name("cdn01.quay.io")
    assert ALLOW.allows_name("a.b.quay.io")
    assert ALLOW.allows_name("deb.debian.org")
    assert not ALLOW.allows_name("debian.org")  # a wildcard does not match its apex
    assert not ALLOW.allows_name("evilquay.io")
    assert not ALLOW.allows_name("quay.io.evil.example")
    assert ALLOW.allows_address("192.0.2.200")
    assert not ALLOW.allows_address("192.0.3.1")
    assert ALLOW.allows_address("127.0.0.1")  # loopback needs no entry
    assert ALLOW.allows_address("::1")
    assert not ALLOW.allows_address("not-an-address")


@pytest.mark.fr("NFR-SEC-010")
@pytest.mark.parametrize(
    "text",
    [
        "cidr",
        "cidr 10.0.0.0/33",
        "two words",
        "*.com.",
        "-bad.example.org",
        "nodots",
    ],
)
def test_allowlist_rejects_bad_lines(text: str) -> None:
    with pytest.raises(nc.AllowListError):
        nc.parse_allowlist(text)


@pytest.mark.fr("NFR-SEC-010")
def test_allowlist_ignores_comments_and_blank_lines() -> None:
    allow = nc.parse_allowlist("\n# only a comment\n\n  # indented\nexample.org  # trailing\n")
    assert allow.exact == frozenset({"example.org"})
    assert allow.suffixes == ()


@pytest.mark.fr("NFR-SEC-010")
def test_clean_capture_has_no_violation() -> None:
    cap = nc.parse_tcpdump(CLEAN)
    assert cap.names == {"quay.io", "cdn01.quay.io"}
    assert cap.answers["3.3.3.3"] == {"quay.io"}
    assert cap.answers["2001:db8::7"] == {"cdn01.quay.io"}
    assert ("3.3.3.3", "443") in cap.dests
    assert ("2001:db8::7", "443") in cap.dests
    assert nc.evaluate(cap, ALLOW) == []


@pytest.mark.fr("NFR-SEC-010")
def test_unlisted_name_and_its_connection_are_both_reported() -> None:
    text = CLEAN + (
        "2.0 IP 10.0.0.2.41000 > 10.0.0.1.53: 3333+ A? telemetry.example.com. (40)\n"
        "2.1 IP 10.0.0.1.53 > 10.0.0.2.41000: 3333 1/0/0 A 5.5.5.5 (60)\n"
        "2.2 IP 10.0.0.2.51000 > 5.5.5.5.443: Flags [S], seq 1, win 64240, length 0\n"
    )
    found = nc.evaluate(nc.parse_tcpdump(text), ALLOW)
    assert [(v.kind, v.target) for v in found] == [
        ("dns", "telemetry.example.com"),
        ("flow", "5.5.5.5:443"),
    ]
    assert "telemetry.example.com" in found[1].detail


@pytest.mark.fr("NFR-SEC-010")
def test_flow_to_an_address_no_name_returned_is_reported() -> None:
    text = "3.0 IP 10.0.0.2.52000 > 8.8.4.4.443: Flags [S], seq 1, win 64240, length 0\n"
    found = nc.evaluate(nc.parse_tcpdump(text), ALLOW)
    assert [(v.kind, v.target) for v in found] == [("flow", "8.8.4.4:443")]


@pytest.mark.fr("NFR-SEC-010")
def test_non_dns_udp_flow_is_a_destination_and_dns_to_the_resolver_is_not() -> None:
    text = (
        "4.0 IP 10.0.0.2.53000 > 9.9.9.9.443: UDP, length 1200\n"
        "4.1 IP 10.0.0.2.53001 > 10.0.0.1.53: 4444+ PTR? 9.9.9.9.in-addr.arpa. (40)\n"
        "4.2 IP 10.0.0.2.53002 > 10.0.0.1.53: 5555+ A? deb.debian.org. (33)\n"
    )
    cap = nc.parse_tcpdump(text)
    assert cap.dests == {("9.9.9.9", "443")}
    assert cap.names == {"deb.debian.org"}  # the PTR lookup is not a name check
    assert [v.target for v in nc.evaluate(cap, ALLOW)] == ["9.9.9.9:443"]


@pytest.mark.fr("NFR-SEC-010")
def test_unknown_lines_are_skipped() -> None:
    text = "tcpdump: listening on any\nARP, Request who-has 10.0.0.1 tell 10.0.0.2\n\ngarbage\n"
    cap = nc.parse_tcpdump(text)
    assert not cap.names and not cap.dests and not cap.answers


@pytest.mark.fr("NFR-SEC-010")
def test_ss_peers_and_comparison() -> None:
    text = (
        "udp UNCONN 0 0 0.0.0.0:68 0.0.0.0:*\n"
        "tcp ESTAB 0 0 10.0.0.2:50000 3.3.3.3:443\n"
        "tcp ESTAB 0 0 10.0.0.2:50001 [2001:db8::9]:443\n"
        "tcp ESTAB 0 0 10.0.0.2:50002 10.0.0.1:53\n"
        "tcp ESTAB 0 0 10.0.0.2:50003 192.0.2.5:80\n"
    )
    peers = nc.parse_ss(text)
    assert peers == {
        ("3.3.3.3", "443"),
        ("2001:db8::9", "443"),
        ("10.0.0.1", "53"),
        ("192.0.2.5", "80"),
    }
    found = nc.evaluate_ss(peers, ALLOW, {"3.3.3.3": {"quay.io"}})
    assert [v.target for v in found] == ["2001:db8::9:443"]


@pytest.mark.fr("NFR-SEC-010")
def test_shipped_allowlist_parses_and_covers_the_sources() -> None:
    allow = nc.parse_allowlist(SHIPPED.read_text(encoding="utf-8"))
    for name in (
        "quay.io",  # trust.toml: arch
        "registry-1.docker.io",  # trust.toml: debian, kali
        "registry.fedoraproject.org",  # trust.toml: fedora
        "deb.debian.org",
        "http.kali.org",
        "aur.archlinux.org",  # AUR_URL of packages/aur.py
    ):
        assert allow.allows_name(name), name
    assert not allow.allows_name("example.com")
    assert not allow.allows_name("api.github.com")


@pytest.mark.fr("NFR-SEC-010")
def test_cli_check_exit_codes(tmp_path: Path) -> None:
    clean = tmp_path / "clean.txt"
    clean.write_text(CLEAN, encoding="utf-8")
    dirty = tmp_path / "dirty.txt"
    dirty.write_text(
        "1.0 IP 10.0.0.2.40000 > 10.0.0.1.53: 1111+ A? tracker.example.net. (25)\n",
        encoding="utf-8",
    )
    allow = tmp_path / "allow.txt"
    allow.write_text("quay.io\n*.quay.io\ncidr 192.0.2.0/24\n", encoding="utf-8")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(TOOL), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    ok = run("check", "--allowlist", str(allow), "--resolver", "10.0.0.1", "--capture", str(clean))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = run("check", "--allowlist", str(allow), "--resolver", "10.0.0.1", "--capture", str(dirty))
    assert bad.returncode == 1
    assert "tracker.example.net" in bad.stdout
    missing = run("check", "--allowlist", str(tmp_path / "none"), "--capture", str(clean))
    assert missing.returncode == 2
    empty = tmp_path / "empty.txt"
    empty.write_text("tcpdump: listening on any\n", encoding="utf-8")
    nothing = run("check", "--allowlist", str(allow), "--capture", str(empty))
    assert nothing.returncode == 2 and "NOT RUN" in nothing.stdout
    no_command = run("run", "--allowlist", str(allow))
    assert no_command.returncode == 2


@pytest.mark.fr("NFR-SEC-010")
def test_repeated_allowlists_are_merged(tmp_path: Path) -> None:
    extra = tmp_path / "extra.txt"
    extra.write_text("tracker.example.net\n", encoding="utf-8")
    base = tmp_path / "base.txt"
    base.write_text("quay.io\n", encoding="utf-8")
    capture = tmp_path / "cap.txt"
    capture.write_text(
        "1.0 IP 10.0.0.2.40000 > 10.0.0.1.53: 1111+ A? tracker.example.net. (25)\n",
        encoding="utf-8",
    )
    both = nc.main(
        [
            "check",
            "--allowlist",
            str(base),
            "--allowlist",
            str(extra),
            "--resolver",
            "10.0.0.1",
            "--capture",
            str(capture),
        ]
    )
    only_base = nc.main(
        ["check", "--allowlist", str(base), "--resolver", "10.0.0.1", "--capture", str(capture)]
    )
    assert (both, only_base) == (0, 1)


SLL2 = """\
5.0 eth0 Out IP 10.0.0.2.40000 > 10.0.0.1.53: 1111+ A? quay.io. (25)
5.1 eth0 In  IP 10.0.0.1.53 > 10.0.0.2.40000: 1111 3/0/1 CNAME edge.cdn.example., A 3.3.3.3 (90)
5.2 wlan0 Out IP 10.0.0.2.50000 > 3.3.3.3.443: Flags [S], seq 1, win 64240, length 0
5.3 any Out IP6 2001:db8::2.50001 > 2001:db8::7.443: Flags [S], seq 1, length 0
5.4 eth0 Out IP 10.0.0.2.50004 > 3.3.3.3.443: Flags [S.], seq 9, ack 2, length 0
"""


@pytest.mark.fr("NFR-SEC-010")
def test_any_interface_lines_with_ifname_and_direction_are_parsed() -> None:
    cap = nc.parse_tcpdump(SLL2)
    assert cap.packets == 5
    assert cap.names == {"quay.io"}
    assert cap.answers["3.3.3.3"] == {"quay.io"}  # the CNAME target is not an address
    assert ("3.3.3.3", "443") in cap.dests
    assert ("2001:db8::7", "443") in cap.dests
    assert len(cap.dests) == 2  # the SYN-ACK (Flags [S.]) is not a new connection
    found = nc.evaluate(cap, ALLOW)
    assert [v.target for v in found] == ["2001:db8::7:443"]  # no name returned the IPv6 address


@pytest.mark.fr("NFR-SEC-010")
def test_an_empty_capture_is_not_run_and_never_clean() -> None:
    cap = nc.parse_tcpdump("tcpdump: listening on any, link-type LINUX_SLL2\n")
    assert cap.packets == 0
    assert nc.report([], source="fixture", packets=cap.packets) == 2


@pytest.mark.fr("NFR-SEC-010")
def test_datagram_without_flags_is_a_destination() -> None:
    text = "6.0 eth0 Out IP 10.0.0.2.50100 > 9.9.9.9.443: [udp sum ok] QUIC, length 1200\n"
    assert nc.parse_tcpdump(text).dests == {("9.9.9.9", "443")}


@pytest.mark.fr("NFR-SEC-010")
def test_dns_traffic_to_a_non_resolver_is_a_flow() -> None:
    text = "7.0 eth0 Out IP 10.0.0.2.40000 > 8.8.8.8.53: 1111+ A? quay.io. (25)\n"
    assert nc.parse_tcpdump(text, {"10.0.0.1"}).dests == {("8.8.8.8", "53")}
    assert nc.parse_tcpdump(text, {"8.8.8.8"}).dests == set()
    assert nc.parse_tcpdump(text).dests == set()  # None: any address may be the resolver


@pytest.mark.fr("NFR-SEC-010")
def test_system_resolvers_reads_nameserver_lines(tmp_path: Path) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text("# c\nsearch lan\nnameserver 10.0.0.1\nnameserver fe80::1%eth0\n")
    assert nc.system_resolvers(conf) == {"10.0.0.1", "fe80::1"}
    assert nc.system_resolvers(tmp_path / "missing") == frozenset()


@pytest.mark.fr("NFR-SEC-010")
def test_registry_hosts_of_the_trust_manifest_are_allowed() -> None:
    """Every image in trust.toml names a registry host that the shipped list must allow."""
    trust = REPO / "src" / "ginn" / "trust" / "data" / "trust.toml"
    images = [
        line.split("=", 1)[1].strip().strip('"')
        for line in trust.read_text(encoding="utf-8").splitlines()
        if line.startswith("image = ")
    ]
    assert len(images) >= 4
    allow = nc.parse_allowlist(SHIPPED.read_text(encoding="utf-8"))
    for image in images:
        assert allow.allows_name(image.split("/", 1)[0]), image


@pytest.mark.fr("NFR-SEC-010")
def test_capture_filter_keeps_ipv6_and_has_no_ipv4_only_flag_match() -> None:
    assert nc.BPF_FILTER == "tcp or udp"
    assert "tcpflags" not in nc.BPF_FILTER  # tcp[tcpflags] matches IPv4 only


@pytest.mark.fr("NFR-SEC-010")
def test_descendants_finds_children_of_a_process() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: S603
    try:
        import os

        assert child.pid in nc._descendants(os.getpid())
        assert os.getpid() not in nc._descendants(os.getpid())
    finally:
        child.kill()
        child.wait()


@pytest.mark.fr("NFR-SEC-010")
def test_inbound_packets_are_not_destinations() -> None:
    text = (
        "8.0 eth0 In  IP 5.5.5.5.40000 > 10.0.0.2.22: Flags [S], seq 1, length 0\n"
        "8.1 eth0 In  IP 5.5.5.5.40001 > 10.0.0.2.5000: UDP, length 20\n"
        "8.2 eth0 Out IP 10.0.0.2.40002 > 3.3.3.3.443: Flags [S], seq 1, length 0\n"
    )
    cap = nc.parse_tcpdump(text)
    assert cap.packets == 3
    assert cap.dests == {("3.3.3.3", "443")}
