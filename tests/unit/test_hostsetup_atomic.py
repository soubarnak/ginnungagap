# SPDX-License-Identifier: Apache-2.0
"""Atomic replacement keeps the target's owner and group (FR-001, DR-035).

Host Setup rewrites host files as root, so a rewrite must not change who owns the file. The
tests run as an ordinary user, so the owner check uses a recorder for os.fchown, and a real
rewrite is only checked with the owner the file already has. Nothing here touches /etc.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ginn.hostsetup import _atomic
from ginn.hostsetup._atomic import atomic_write, file_owner

pytestmark = [pytest.mark.fr("FR-001")]

OTHER_OWNER = (4242, 4343)


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


@pytest.fixture
def fchown_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, int]]:
    calls: list[tuple[int, int, int]] = []

    def record(fd: int, uid: int, gid: int) -> None:
        calls.append((fd, uid, gid))

    monkeypatch.setattr(os, "fchown", record)
    return calls


def test_an_existing_target_gives_its_owner_and_group_to_the_new_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fchown_calls: list[tuple[int, int, int]],
) -> None:
    target = tmp_path / "subuid"
    target.write_bytes(b"old\n")
    monkeypatch.setattr(_atomic, "file_owner", lambda path: OTHER_OWNER)
    atomic_write(target, b"new\n", 0o644)
    assert [(uid, gid) for _, uid, gid in fchown_calls] == [OTHER_OWNER]
    assert target.read_bytes() == b"new\n"


def test_a_new_file_is_not_chowned(
    tmp_path: Path, fchown_calls: list[tuple[int, int, int]]
) -> None:
    atomic_write(tmp_path / "fresh", b"x\n", 0o600)
    assert fchown_calls == []


def test_a_rewrite_with_the_same_owner_keeps_the_owner(tmp_path: Path) -> None:
    target = tmp_path / "subgid"
    target.write_bytes(b"old\n")
    before = os.lstat(target)
    atomic_write(target, b"new\n", 0o644)
    after = os.lstat(target)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert file_owner(target) == (before.st_uid, before.st_gid)


def test_the_mode_is_set_explicitly_whatever_the_umask(tmp_path: Path) -> None:
    target = tmp_path / "script"
    atomic_write(target, b"#!/bin/sh\n", 0o640)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


def test_a_failed_owner_change_leaves_the_target_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "subuid"
    target.write_bytes(b"old\n")
    monkeypatch.setattr(_atomic, "file_owner", lambda path: OTHER_OWNER)

    def refuse(fd: int, uid: int, gid: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", refuse)
    with pytest.raises(PermissionError):
        atomic_write(target, b"new\n", 0o644)
    assert target.read_bytes() == b"old\n"
    assert _names(tmp_path) == ["subuid"]
