# SPDX-License-Identifier: Apache-2.0
"""The FR-121 disk precheck (T8.2, ops/disk.py).

Covered: the 10 percent margin in whole bytes (no float rounding), the refusal of a negative
estimate, the free space an unprivileged user may write (f_bavail, not f_bfree), a path that does
not exist yet (probed at its nearest parent), the exact boundary (free space equal to the
requirement passes, one byte less refuses), the GINN-E-DISK-SPACE details, and the check of each
Owned Path. Each test uses a fake statvfs, except the last, which reads the real filesystem.
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.ops.disk import check_free, check_free_paths, estimate_required, free_bytes

pytestmark = pytest.mark.fr("FR-121")

GIB = 1024**3


def _vfs(bavail: int, *, bfree: int | None = None, frsize: int = 1) -> os.statvfs_result:
    """A statvfs result with `bavail` blocks of `frsize` bytes free to an unprivileged user."""
    free = bavail if bfree is None else bfree
    return os.statvfs_result((frsize, frsize, 0, free, bavail, 0, 0, 0, 0, 255))


def _fake(bavail: int, *, frsize: int = 1) -> tuple[list[str], Callable[[str], os.statvfs_result]]:
    """A statvfs seam that records each path it is asked about."""
    asked: list[str] = []

    def statvfs(path: str) -> os.statvfs_result:
        asked.append(path)
        return _vfs(bavail, frsize=frsize)

    return asked, statvfs


@pytest.mark.parametrize(
    ("estimate", "required"),
    [
        (0, 0),
        (1, 2),
        (10, 11),
        (100, 110),
        (999, 1099),
        (1000, 1100),
        (GIB, 1_181_116_007),
    ],
)
def test_the_requirement_is_the_estimate_times_1_10_rounded_up(
    estimate: int, required: int
) -> None:
    assert estimate_required(estimate) == required


def test_the_margin_is_exact_where_a_float_product_is_not() -> None:
    # 10 * 1.1 is 11.000000000000002 in floating point, so ceil() of the float would say 12.
    assert estimate_required(10) == 11
    assert estimate_required(30) == 33


def test_a_negative_estimate_is_refused() -> None:
    with pytest.raises(ValueError, match="FR-121"):
        estimate_required(-1)


def test_free_space_is_the_unprivileged_count_at_the_fragment_size(tmp_path: Path) -> None:
    asked, statvfs = _fake(10, frsize=4096)
    assert free_bytes(tmp_path, statvfs=statvfs) == 10 * 4096
    assert asked == [str(tmp_path)]


def test_free_space_ignores_blocks_that_only_root_may_use(tmp_path: Path) -> None:
    def statvfs(path: str) -> os.statvfs_result:
        return _vfs(bavail=10, bfree=90, frsize=4096)

    assert free_bytes(tmp_path, statvfs=statvfs) == 10 * 4096
    check_free(tmp_path, 10 * 4096, statvfs=statvfs)
    with pytest.raises(GinnError):
        check_free(tmp_path, 10 * 4096 + 1, statvfs=statvfs)


def test_a_path_that_does_not_exist_yet_is_probed_at_its_nearest_parent(tmp_path: Path) -> None:
    asked, statvfs = _fake(GIB)
    free_bytes(tmp_path / "homes" / "realm", statvfs=statvfs)
    assert asked == [str(tmp_path)]


def test_exactly_the_requirement_passes(tmp_path: Path) -> None:
    required = estimate_required(1_000_000_000)
    assert required == 1_100_000_000
    _, statvfs = _fake(required)
    check_free(tmp_path, required, statvfs=statvfs)


def test_one_byte_less_than_the_requirement_is_refused(tmp_path: Path) -> None:
    required = estimate_required(1_000_000_000)
    _, statvfs = _fake(required - 1)
    with pytest.raises(GinnError) as caught:
        check_free(tmp_path, required, statvfs=statvfs)
    assert caught.value.code == "GINN-E-DISK-SPACE"


def test_an_insufficient_check_names_needed_available_and_path(tmp_path: Path) -> None:
    _, statvfs = _fake(1_099_999_999)
    with pytest.raises(GinnError) as caught:
        check_free(tmp_path, 1_100_000_000, statvfs=statvfs)
    err = caught.value
    assert err.code == "GINN-E-DISK-SPACE"
    assert err.exit_code == 73
    assert err.message == "not enough disk space: need 1100000000 bytes, have 1099999999 bytes"
    assert err.details["needed"] == "1100000000 bytes"
    assert err.details["available"] == "1099999999 bytes"
    assert err.details["path"] == str(tmp_path)


def test_every_owned_path_must_hold_the_requirement(tmp_path: Path) -> None:
    storage = tmp_path / "data" / "podman"
    homes = tmp_path / "data" / "homes"
    storage.mkdir(parents=True)
    homes.mkdir()
    asked: list[str] = []

    def statvfs(path: str) -> os.statvfs_result:
        asked.append(path)
        return _vfs(500 if path == str(homes) else 2000)

    with pytest.raises(GinnError) as caught:
        check_free_paths([storage, homes], 1000, statvfs=statvfs)
    assert asked == [str(storage), str(homes)]
    assert caught.value.details["path"] == str(homes)


def test_the_real_filesystem_is_checked_at_its_free_count(tmp_path: Path) -> None:
    assert free_bytes(tmp_path) > 0
    check_free(tmp_path, 0)
    with pytest.raises(GinnError) as caught:
        check_free(tmp_path, 2**62)
    assert caught.value.code == "GINN-E-DISK-SPACE"
