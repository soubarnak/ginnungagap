# SPDX-License-Identifier: Apache-2.0
"""Host Integrity Fingerprint: scope, stat records, links, unreadable paths (FR-006).

Every test runs under a temporary host root, so no real host path is read. The NFR-011 timing
proof is in test_integrity_state.py, because it times before_op and after_op, not one capture.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from ginn.integrity.fingerprint import SCOPE_ROOTS, Fingerprint, capture, diff

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
SECONDS_IN_TEN = 10 * 10**9


def _host(tmp_path: Path) -> Path:
    """A host root with the five AS-020 roots and one file under /etc."""
    root = tmp_path / "host"
    for scope in SCOPE_ROOTS:
        (root / scope).mkdir(parents=True)
    hosts = root / "etc" / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    hosts.chmod(0o644)
    return root


@pytest.mark.fr("FR-006")
def test_scope_roots_are_exactly_the_as020_set() -> None:
    assert SCOPE_ROOTS == ("etc", "var/db/xbps", "var/service", "usr/local", "opt")


@pytest.mark.fr("FR-006")
def test_capture_records_the_stat_fields_of_each_path(tmp_path: Path) -> None:
    root = _host(tmp_path)
    fp = capture(root)
    hosts = fp.entries["/etc/hosts"]
    assert hosts.kind == "file"
    assert hosts.mode == 0o644
    assert hosts.uid == os.getuid()
    assert hosts.size == len("127.0.0.1 localhost\n")
    assert hosts.inode == (root / "etc" / "hosts").stat().st_ino
    assert hosts.target is None
    assert hosts.unreadable is False
    assert fp.entries["/etc"].kind == "dir"
    assert fp.entries["/opt"].kind == "dir"


@pytest.mark.fr("FR-006")
def test_host_paths_are_keyed_without_the_temporary_root(tmp_path: Path) -> None:
    fp = capture(_host(tmp_path))
    assert all(path.startswith("/") and str(tmp_path) not in path for path in fp.entries)
    assert "/usr/local" in fp.entries
    assert "/var/db/xbps" in fp.entries


@pytest.mark.fr("FR-006")
def test_an_unchanged_host_has_the_same_digest_and_no_diff(tmp_path: Path) -> None:
    root = _host(tmp_path)
    first = capture(root)
    second = capture(root)
    assert first.digest == second.digest
    assert diff(first, second) == []


@pytest.mark.fr("FR-006")
def test_an_mtime_only_change_is_detected(tmp_path: Path) -> None:
    root = _host(tmp_path)
    hosts = root / "etc" / "hosts"
    before = capture(root)
    info = hosts.stat()
    os.utime(hosts, ns=(info.st_atime_ns, info.st_mtime_ns + SECONDS_IN_TEN))
    after = capture(root)
    assert diff(before, after) == ["/etc/hosts"]


@pytest.mark.fr("FR-006")
def test_a_content_change_with_its_mtime_restored_is_caught_by_ctime(tmp_path: Path) -> None:
    root = _host(tmp_path)
    hosts = root / "etc" / "hosts"
    past = 1_600_000_000 * 10**9
    os.utime(hosts, ns=(past, past))
    before = capture(root)
    # Filesystem timestamps can be coarse. The pause keeps the ctime of the rewrite apart from
    # the ctime that the first utime set.
    time.sleep(0.05)
    hosts.write_text("127.0.0.2 localhost\n", encoding="utf-8")
    os.utime(hosts, ns=(past, past))
    after = capture(root)
    kept = after.entries["/etc/hosts"]
    assert kept.size == before.entries["/etc/hosts"].size
    assert kept.mtime_ns == before.entries["/etc/hosts"].mtime_ns
    assert kept.ctime_ns != before.entries["/etc/hosts"].ctime_ns
    assert diff(before, after) == ["/etc/hosts"]


@pytest.mark.fr("FR-006")
def test_a_symbolic_link_is_recorded_with_its_target_and_never_followed(tmp_path: Path) -> None:
    root = _host(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("one\n", encoding="utf-8")
    link = root / "etc" / "link"
    os.symlink(outside, link)
    before = capture(root)
    record = before.entries["/etc/link"]
    assert record.kind == "symlink"
    assert record.target == str(outside)
    assert not any(path.startswith("/etc/link/") for path in before.entries)

    # A change behind the link is outside the scope, so it is not a change of the host.
    (outside / "secret").write_text("two, longer\n", encoding="utf-8")
    assert diff(before, capture(root)) == []

    # Retargeting the link itself is a change. The parent directory is reported as well, because
    # its entry changed.
    link.unlink()
    os.symlink(tmp_path, link)
    changed = diff(before, capture(root))
    assert "/etc/link" in changed
    assert "/etc/hosts" not in changed


@pytest.mark.fr("FR-006")
def test_a_scope_root_that_is_itself_a_symlink_is_recorded_and_not_walked(tmp_path: Path) -> None:
    # /var/service is a symbolic link on Void. Only the link is recorded. The directory it points
    # to lies under /etc, so a service enabled there is a change under /etc.
    root = _host(tmp_path)
    shutil.rmtree(root / "var" / "service")
    runsvdir = root / "etc" / "runit" / "runsvdir" / "default"
    runsvdir.mkdir(parents=True)
    (runsvdir / "sshd").write_text("", encoding="utf-8")
    os.symlink("/etc/runit/runsvdir/default", root / "var" / "service")

    before = capture(root)
    record = before.entries["/var/service"]
    assert record.kind == "symlink"
    assert record.target == "/etc/runit/runsvdir/default"
    assert not any(path.startswith("/var/service/") for path in before.entries)

    (runsvdir / "dhcpcd").write_text("", encoding="utf-8")
    changed = diff(before, capture(root))
    assert "/etc/runit/runsvdir/default/dhcpcd" in changed
    assert "/var/service" not in changed


@pytest.mark.fr("FR-006")
def test_a_link_cycle_inside_the_scope_does_not_loop(tmp_path: Path) -> None:
    root = _host(tmp_path)
    os.symlink("..", root / "usr" / "local" / "up")
    os.symlink(root, root / "opt" / "loop")
    fp = capture(root)
    assert fp.entries["/usr/local/up"].kind == "symlink"
    assert fp.entries["/opt/loop"].kind == "symlink"
    assert "/opt/loop/etc" not in fp.entries


@pytest.mark.fr("FR-006")
def test_a_directory_removed_and_recreated_as_a_link_is_a_change(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = capture(root)
    shutil.rmtree(root / "opt")
    os.symlink(tmp_path, root / "opt")
    after = capture(root)
    assert after.entries["/opt"].kind == "symlink"
    assert "/opt" in diff(before, after)


@pytest.mark.fr("FR-006")
@NOT_ROOT
def test_an_unlistable_directory_is_recorded_once_and_the_walk_goes_on(tmp_path: Path) -> None:
    root = _host(tmp_path)
    locked = root / "usr" / "local" / "locked"
    locked.mkdir()
    (locked / "hidden").write_text("x", encoding="utf-8")
    locked.chmod(0)
    try:
        fp = capture(root)
    finally:
        locked.chmod(0o755)
    record = fp.entries["/usr/local/locked"]
    assert record.kind == "dir"
    assert record.unreadable is True
    assert "/usr/local/locked/hidden" not in fp.entries
    assert fp.entries["/etc/hosts"].unreadable is False


@pytest.mark.fr("FR-006")
@NOT_ROOT
def test_a_file_that_cannot_be_read_is_still_fingerprinted(tmp_path: Path) -> None:
    # The fingerprint is stat-only: a file without read permission is recorded like any other.
    root = _host(tmp_path)
    hosts = root / "etc" / "hosts"
    hosts.chmod(0)
    try:
        fp = capture(root)
    finally:
        hosts.chmod(0o644)
    assert fp.entries["/etc/hosts"].kind == "file"
    assert fp.entries["/etc/hosts"].mode == 0


@pytest.mark.fr("FR-006")
def test_a_missing_root_is_recorded_as_absent_and_its_creation_is_a_change(tmp_path: Path) -> None:
    root = _host(tmp_path)
    shutil.rmtree(root / "opt")
    before = capture(root)
    assert before.entries["/opt"].kind == "absent"
    (root / "opt").mkdir()
    assert diff(before, capture(root)) == ["/opt"]


@pytest.mark.fr("FR-006")
def test_added_and_removed_paths_are_reported_sorted(tmp_path: Path) -> None:
    root = _host(tmp_path)
    before = capture(root)
    (root / "etc" / "hosts").unlink()
    (root / "etc" / "zz").write_text("z", encoding="utf-8")
    (root / "etc" / "aa").write_text("a", encoding="utf-8")
    changed = diff(before, capture(root))
    assert changed == sorted(changed)
    # The parent directory changed too, because its entries did.
    assert {"/etc", "/etc/aa", "/etc/hosts", "/etc/zz"} == set(changed)


@pytest.mark.fr("FR-006")
def test_a_fingerprint_built_from_its_own_entries_has_the_same_digest(tmp_path: Path) -> None:
    fp = capture(_host(tmp_path))
    assert Fingerprint.of(fp.entries).digest == fp.digest


@pytest.mark.fr("FR-006")
def test_a_name_that_vanishes_during_the_walk_is_neither_recorded_nor_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parent listing shows the name, and the lstat of it finds nothing: it was removed, so it
    # is not an unreadable entry. The rest of the walk goes on.
    root = _host(tmp_path)
    (root / "usr" / "local" / "gone").mkdir()
    real_lstat = os.lstat

    def vanished(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "gone":
            raise FileNotFoundError(2, "gone", "gone")
        return real_lstat(path, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "lstat", vanished)
    fp = capture(root)
    assert "/usr/local/gone" not in fp.entries
    assert fp.entries["/usr/local"].unreadable is False
    assert fp.entries["/etc/hosts"].kind == "file"
