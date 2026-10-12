# SPDX-License-Identifier: Apache-2.0
"""cgroup v2 delegation over a fake cgroupfs tree under tmp_path (FR-001, FR-007).

The fake emulates the kernel behaviour that cgroups.py relies on: a new cgroup gets its
interface files, a `+ctrl` write adds ctrl only where the node offers it, and writes to a
failing path raise the configured errno. os.chown is replaced with a recorder, because the test
process is not root. The recorded calls are then compared with the exact expected set.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ginn.hostsetup import cgroups
from ginn.hostsetup.cgroups import (
    CONTROLLERS,
    chown_paths,
    delegate_user_tree,
    enable_controllers,
    move_self_to,
    read_controllers,
    read_subtree_control,
    verify,
)

pytestmark = [pytest.mark.fr("FR-001"), pytest.mark.fr("FR-007")]

# A non-root test process already owns the fake tree, so its own uid passes the owner check.
# Delegation refuses uid 0, so a root run uses 1000 and the recorder also chowns for real.
TEST_UID = os.getuid() or 1000
TEST_GID = os.getgid() or 1000

ROOT_OFFERED = "cpuset cpu io memory hugetlb pids rdma misc"
ROOT_ENABLED = "cpuset cpu io memory"  # what S-01 found on the host before setup


@dataclass
class FakeCgroupfs:
    """Kernel-like behaviour for the three cgroups.py seams, backed by files under `root`."""

    root: Path
    writes: list[tuple[Path, str]] = field(default_factory=list)
    mkdirs: list[Path] = field(default_factory=list)
    fail_on: dict[Path, int] = field(default_factory=dict)

    def make_dir(self, path: Path) -> bool:
        if path.exists():
            return False
        offered = (path.parent / "cgroup.subtree_control").read_text().split()
        path.mkdir()
        self.mkdirs.append(path)
        (path / "cgroup.controllers").write_text(" ".join(offered) + "\n")
        (path / "cgroup.subtree_control").write_text("\n")
        for name in ("cgroup.procs", "cgroup.threads"):
            (path / name).write_text("")
        for limit in ("memory.max", "pids.max"):
            (path / limit).write_text("max\n")
        return True

    def write_control(self, path: Path, text: str) -> None:
        if path in self.fail_on:
            raise OSError(self.fail_on[path], os.strerror(self.fail_on[path]), str(path))
        self.writes.append((path, text))
        if path.name != "cgroup.subtree_control":
            path.write_text(text)
            return
        node = path.parent
        offered = read_controllers(node)
        current = set(read_subtree_control(node))
        for token in text.split():
            if token.startswith("+") and token[1:] in offered:
                current.add(token[1:])
            elif token.startswith("-"):
                current.discard(token[1:])
            else:
                raise OSError(errno.ENOENT, "controller not offered", token)
        path.write_text(" ".join(sorted(current)) + "\n")


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCgroupfs:
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text(ROOT_OFFERED + "\n")
    (root / "cgroup.subtree_control").write_text(ROOT_ENABLED + "\n")
    (root / "cgroup.procs").write_text("")
    kernel = FakeCgroupfs(root)
    monkeypatch.setattr(cgroups, "_make_dir", kernel.make_dir)
    monkeypatch.setattr(cgroups, "_write_control", kernel.write_control)
    return kernel


@pytest.fixture
def chowns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int, int]]:
    calls: list[tuple[Path, int, int]] = []
    real_chown = os.chown

    def record(path: str | os.PathLike[str], uid: int, gid: int, **_: object) -> None:
        calls.append((Path(path), uid, gid))
        if os.geteuid() == 0:
            real_chown(path, uid, gid)

    monkeypatch.setattr(os, "chown", record)
    return calls


def _user_tree(root: Path, uid: int) -> dict[str, Path]:
    user = root / "ginn" / str(uid)
    return {
        "ginn": root / "ginn",
        "user": user,
        "supervisor": user / "supervisor",
    }


def test_enable_controllers_writes_only_offered_and_missing_controllers(
    fake: FakeCgroupfs, tmp_path: Path
) -> None:
    node = tmp_path / "node"
    node.mkdir()
    (node / "cgroup.controllers").write_text("cpu memory io\n")
    (node / "cgroup.subtree_control").write_text("memory\n")
    added = enable_controllers(node, {"cpu", "io", "pids", "memory"})
    assert added == frozenset({"cpu", "io"})
    assert fake.writes == [
        (node / "cgroup.subtree_control", "+cpu"),
        (node / "cgroup.subtree_control", "+io"),
    ]
    assert enable_controllers(node, {"cpu", "io", "pids", "memory"}) == frozenset()
    assert len(fake.writes) == 2


def test_delegate_writes_and_chowns_exactly_the_expected_paths(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]], tmp_path: Path
) -> None:
    tree = _user_tree(fake.root, 1000)
    plan = delegate_user_tree(fake.root, 1000, 1000)

    assert plan.root_refused is False
    assert plan.written == [
        fake.root / "cgroup.subtree_control",
        tree["ginn"] / "cgroup.subtree_control",
        tree["user"] / "cgroup.subtree_control",
    ]
    assert plan.enabled[fake.root] == frozenset({"pids"})
    assert plan.enabled[tree["ginn"]] == CONTROLLERS
    assert plan.enabled[tree["user"]] == CONTROLLERS
    assert plan.pre_images[fake.root / "cgroup.subtree_control"] == ROOT_ENABLED

    assert plan.created == [tree["ginn"], tree["user"], tree["supervisor"]]
    expected_chown = {
        tree["user"],
        tree["user"] / "cgroup.procs",
        tree["user"] / "cgroup.threads",
        tree["user"] / "cgroup.subtree_control",
    }
    chowned = [path for path, _, _ in chowns]
    assert set(chowned) == expected_chown
    assert len(chowned) == len(expected_chown)
    assert set(chowned) == set(chown_paths(fake.root, 1000))
    assert all((uid, gid) == (1000, 1000) for _, uid, gid in chowns)
    assert plan.chowned == chowned

    limit_files = [tree["user"] / "memory.max", tree["user"] / "pids.max"]
    assert not set(limit_files) & set(chowned)
    assert tree["ginn"] not in chowned
    assert fake.root not in chowned


@pytest.mark.fr("FR-001")
def test_no_path_under_the_supervisor_leaf_is_chowned(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    # The run script writes the supervisor pid into the leaf as root. Migration out of the leaf
    # needs write access to the common ancestor and the destination, not to the leaf (ADR-003).
    leaf = _user_tree(fake.root, 1000)["supervisor"]
    delegate_user_tree(fake.root, 1000, 1000)
    chowned = [path for path, _, _ in chowns]
    assert [path for path in chowned if path == leaf or leaf in path.parents] == []
    planned = chown_paths(fake.root, 1000)
    assert [path for path in planned if path == leaf or leaf in path.parents] == []
    assert leaf not in planned


def test_controllers_are_never_enabled_inside_the_leaves(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    tree = _user_tree(fake.root, 1000)
    delegate_user_tree(fake.root, 1000, 1000)
    written_dirs = {path.parent for path, _ in fake.writes}
    assert written_dirs == {fake.root, tree["ginn"], tree["user"]}


def test_second_delegation_is_a_no_op_for_directories_and_controllers(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    first = delegate_user_tree(fake.root, 1000, 1000)
    writes_after_first = list(fake.writes)
    second = delegate_user_tree(fake.root, 1000, 1000)
    assert first.created != []
    assert second.created == []
    assert second.written == []
    assert second.enabled == {}
    assert fake.writes == writes_after_first
    assert second.chowned == first.chowned


@pytest.mark.parametrize("err", [errno.EBUSY, errno.EACCES])
def test_refused_root_write_is_a_warning_and_missing_pids_is_reported(
    fake: FakeCgroupfs,
    chowns: list[tuple[Path, int, int]],
    err: int,
) -> None:
    uid = TEST_UID
    fake.fail_on[fake.root / "cgroup.subtree_control"] = err
    tree = _user_tree(fake.root, uid)
    plan = delegate_user_tree(fake.root, uid, TEST_GID)
    assert plan.root_refused is True
    assert len(plan.warnings) == 2
    assert "cgroup.subtree_control not written" in plan.warnings[0]
    assert "pids" in plan.warnings[1]
    assert tree["supervisor"].is_dir()


@pytest.mark.fr("FR-007")
def test_doctor_passes_with_a_refused_root_write_only_when_the_record_tolerates_it(
    fake: FakeCgroupfs,
    chowns: list[tuple[Path, int, int]],
) -> None:
    uid = TEST_UID
    fake.fail_on[fake.root / "cgroup.subtree_control"] = errno.EBUSY
    plan = delegate_user_tree(fake.root, uid, TEST_GID)
    root_gap = "root: controller pids is not enabled in subtree_control"

    tolerated = verify(fake.root, uid, tolerated_root=plan.root_refused)
    assert tolerated.problems == []
    assert tolerated.warnings == [root_gap]

    strict = verify(fake.root, uid)
    assert strict.problems == [root_gap]
    assert strict.warnings == []


def test_other_errors_on_the_root_write_propagate(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    fake.fail_on[fake.root / "cgroup.subtree_control"] = errno.ENOENT
    with pytest.raises(OSError) as info:
        delegate_user_tree(fake.root, 1000, 1000)
    assert info.value.errno == errno.ENOENT


@pytest.mark.parametrize("uid", [0, -1])
def test_root_and_negative_uids_are_refused(fake: FakeCgroupfs, uid: int) -> None:
    with pytest.raises(ValueError, match="refusing"):
        delegate_user_tree(fake.root, uid, 1000)


def test_verify_reports_a_missing_tree_before_setup(fake: FakeCgroupfs) -> None:
    problems = verify(fake.root, 1000).problems
    hint = "missing; run 'sudo ginn host-setup --user <name>'"
    assert problems == [
        "root: controller pids is not enabled in subtree_control",
        f"{fake.root / 'ginn'}: {hint}",
        f"{fake.root / 'ginn' / '1000'}: {hint}",
    ]


def test_verify_is_clean_after_delegation(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    # The fake chown is recorded, not applied, so the files stay owned by the test user.
    uid = TEST_UID
    delegate_user_tree(fake.root, uid, TEST_GID)
    assert verify(fake.root, uid).problems == []


def test_verify_reports_a_missing_leaf(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    uid = TEST_UID
    delegate_user_tree(fake.root, uid, TEST_GID)
    supervisor = _user_tree(fake.root, uid)["supervisor"]
    shutil.rmtree(supervisor)
    problems = verify(fake.root, uid).problems
    assert problems == [f"{supervisor}: missing"]


def test_move_self_to_writes_this_pid_into_the_leaf(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    uid = TEST_UID
    delegate_user_tree(fake.root, uid, TEST_GID)
    supervisor = _user_tree(fake.root, uid)["supervisor"]
    move_self_to(supervisor)
    assert (supervisor / "cgroup.procs").read_text() == str(os.getpid())


def test_no_leaf_is_created_for_ginn_start_or_podman_start(
    fake: FakeCgroupfs, chowns: list[tuple[Path, int, int]]
) -> None:
    # Session processes cannot enter such a leaf (see the NOTE in cgroups.py), so none is made.
    uid = TEST_UID
    delegate_user_tree(fake.root, uid, TEST_GID)
    assert not (_user_tree(fake.root, uid)["user"] / "cli").exists()
    assert all("cli" not in path.parts for path in chown_paths(fake.root, uid))
