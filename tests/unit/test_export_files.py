# SPDX-License-Identifier: Apache-2.0
"""Atomic replacement of the export writers' files (`export/_files.py`, DR-010, DR-035).

A failure before the rename must remove the staged file, leave the target as it was, and close
every descriptor the write opened.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from ginn.export._files import write_file_atomic


@pytest.mark.fr("DR-035")
@pytest.mark.fr("DR-010")
def test_a_failed_fdopen_closes_the_descriptor_and_removes_the_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "ginn-entry.desktop"
    target.write_bytes(b"old")
    received: list[int] = []

    def failing_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        received.append(fd)
        raise OSError(errno.ENOMEM, "no memory for a file object")

    monkeypatch.setattr(os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match="no memory"):
        write_file_atomic(target, b"new", 0o644)

    (fd,) = received
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ginn-entry.desktop"]
    assert target.read_bytes() == b"old"
