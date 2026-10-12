# SPDX-License-Identifier: Apache-2.0
"""`ginn doctor` checks under a fake host tree: PASS and FAIL for each, and read-only (FR-007).

Each test builds a fully configured host with tests/fakes/doctor_host.py, breaks one piece, and
runs one check. The podman checks run the replay shim through a wrapper on PATH, so no real podman
runs. The elogind rule is last-wins across the main file and the sorted drop-ins, with unset meaning
no (ADR-004). The subordinate ID rule is one line of at least 524288 IDs per file (ADR-013
Decision 16).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from ginn.doctor import checks
from ginn.doctor.checks import CheckResult, Probe, Status
from tests.fakes import doctor_host as fake
from tests.fakes.doctor_host import GIB, UID, install_podman, make_probe


@dataclass(frozen=True)
class Host:
    """A fake host and where its user's directories are."""

    root: Path
    home: Path
    bin_dir: Path

    def probe(self, *, free: int = 50 * GIB) -> Probe:
        return make_probe(self.root, self.home, self.bin_dir, free=free)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    """A fully configured fake host, with podman on PATH as the only executable directory."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    for directory in (root, home, bin_dir):
        directory.mkdir()
    fake.build_host(root, home)
    install_podman(bin_dir)
    # PodmanCli passes this PATH to podman, so the shim wrapper is found from it.
    monkeypatch.setenv("PATH", str(bin_dir))
    return Host(root=root, home=home, bin_dir=bin_dir)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.fr("FR-007")
def test_fully_configured_host_has_no_fail_or_warn(host: Host) -> None:
    results = checks.run_checks(host.probe())
    assert {result.status for result in results} == {Status.PASS}
    assert len(results) == len(checks.CHECKS)


@pytest.mark.fr("FR-007")
def test_fail_without_fix_is_refused() -> None:
    with pytest.raises(ValueError, match="without a fix command"):
        CheckResult("broken", Status.FAIL, "no fix")


@pytest.mark.fr("FR-007")
def test_probe_shows_host_paths_without_the_test_root(host: Host) -> None:
    probe = host.probe()
    assert probe.show(probe.at("etc/subuid")) == "/etc/subuid"
    assert probe.show(Path("/elsewhere")) == "/elsewhere"


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize(
    ("content", "status"),
    [
        ("alice:524288:524288\n", Status.PASS),
        ("alice:524287:524287\n", Status.FAIL),
        ("alice:100000:262144\nalice:400000:262144\n", Status.FAIL),
        (f"{UID}:524288:524288\n", Status.PASS),
        ("bob:524288:524288\n", Status.FAIL),
        ("", Status.FAIL),
    ],
)
def test_subids_one_line_rule(host: Host, content: str, status: Status) -> None:
    _write(host.root / "etc/subuid", content)
    _write(host.root / "etc/subgid", content)
    result = checks.check_subids(host.probe())
    assert result.status is status
    if status is Status.FAIL:
        assert result.fixes == ("sudo ginn host-setup --user alice",)
        assert "524288" in result.message


@pytest.mark.fr("FR-007")
def test_subids_missing_subgid_entry_fails_with_host_setup_fix(host: Host) -> None:
    """FR-007 AC-1: a missing subordinate ID entry fails and names the host-setup fix."""
    _write(host.root / "etc/subgid", "carol:524288:524288\n")
    result = checks.check_subids(host.probe())
    assert result.status is Status.FAIL
    assert "/etc/subgid" in result.message
    assert "/etc/subuid" not in result.message


@pytest.mark.fr("FR-007")
def test_runtime_dir_missing_is_supervisor_not_running(host: Host) -> None:
    (host.root / f"run/ginn/{UID}").rmdir()
    result = checks.check_runtime_dir(host.probe())
    assert result.status is Status.FAIL
    assert result.message.startswith("supervisor not running:")
    assert result.fixes == ("sudo sv up /var/service/ginn-alice",)


@pytest.mark.fr("FR-007")
def test_runtime_dir_wrong_mode_fails(host: Host) -> None:
    os.chmod(host.root / f"run/ginn/{UID}", 0o755)  # noqa: S103 - a wrong mode, on purpose
    result = checks.check_runtime_dir(host.probe())
    assert result.status is Status.FAIL
    assert "mode 0755" in result.message
    assert result.fixes == ("sudo sv restart /var/service/ginn-alice",)


@pytest.mark.fr("FR-007")
def test_runtime_dir_owned_by_other_uid_fails(host: Host) -> None:
    probe = dataclasses.replace(host.probe(), owner_of=lambda _path: 4321)
    result = checks.check_runtime_dir(probe)
    assert result.status is Status.FAIL
    assert "owner uid 4321" in result.message


@pytest.mark.fr("FR-007")
def test_runtime_dir_good_passes(host: Host) -> None:
    assert checks.check_runtime_dir(host.probe()).status is Status.PASS


@pytest.mark.fr("FR-007")
def test_runsvdir_running_passes_and_missing_fails(host: Host) -> None:
    assert checks.check_runsvdir(host.probe()).status is Status.PASS
    _write(host.root / "proc/4242/status", f"Name:\tbash\nUid:\t{UID}\t{UID}\n")
    _write(host.root / "proc/4242/cmdline", "bash\x00")
    result = checks.check_runsvdir(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == ("sudo sv up /var/service/ginn-alice",)


@pytest.mark.fr("FR-007")
def test_runsvdir_of_another_user_does_not_count(host: Host) -> None:
    _write(host.root / "proc/4242/status", "Name:\trunsvdir\nUid:\t999\t999\n")
    assert checks.check_runsvdir(host.probe()).status is Status.FAIL


@pytest.mark.fr("FR-007")
def test_runit_dir_missing_fails(host: Host) -> None:
    shutil.rmtree(host.root / "run/runit")
    result = checks.check_runit_dir(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == ("sudo install -d -m 0755 /run/runit",)


@pytest.mark.fr("FR-007")
def test_sv_dir_needs_executable_run_scripts(host: Host) -> None:
    assert checks.check_sv_dir(host.probe()).status is Status.PASS
    os.chmod(host.root / "etc/sv/ginn-alice/log/run", 0o644)
    result = checks.check_sv_dir(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == ("sudo ginn host-setup --user alice",)


@pytest.mark.fr("FR-007")
def test_service_link_points_at_sv_dir(host: Host) -> None:
    assert checks.check_service_link(host.probe()).status is Status.PASS
    link = host.root / "etc/runit/runsvdir/default/ginn-alice"
    link.unlink()
    assert checks.check_service_link(host.probe()).status is Status.FAIL
    os.symlink("/etc/sv/other", link)
    result = checks.check_service_link(host.probe())
    assert result.status is Status.FAIL
    assert "/etc/sv/other" in result.message


@pytest.mark.fr("FR-007")
def test_service_link_broken_chain_fails(host: Host) -> None:
    (host.root / "var/service").unlink()
    result = checks.check_service_link(host.probe())
    assert result.status is Status.FAIL
    assert "does not resolve" in result.message


@pytest.mark.fr("FR-007")
def test_supervise_links_must_point_into_run_runit(host: Host) -> None:
    assert checks.check_supervise_links(host.probe()).status is Status.PASS
    link = host.root / "etc/sv/ginn-alice/supervise"
    link.unlink()
    os.symlink("/elsewhere", link)
    result = checks.check_supervise_links(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == (
        "sudo ln -sfn /run/runit/supervise.ginn-alice /etc/sv/ginn-alice/supervise",
    )


@pytest.mark.fr("FR-007")
def test_cgroup_v2_missing_fails(host: Host) -> None:
    (host.root / "sys/fs/cgroup/cgroup.controllers").unlink()
    result = checks.check_cgroup(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == ("sudo mount -t cgroup2 none /sys/fs/cgroup",)


@pytest.mark.fr("FR-007")
def test_cgroup_user_tree_missing_controller_fails(host: Host) -> None:
    user_control = host.root / f"sys/fs/cgroup/ginn/{UID}/cgroup.subtree_control"
    _write(user_control, "cpuset cpu memory pids\n")
    result = checks.check_cgroup(host.probe())
    assert result.status is Status.FAIL
    assert "io" in result.message
    assert result.fixes == ("sudo sv restart /var/service/ginn-alice",)


@pytest.mark.fr("FR-007")
def test_cgroup_root_gap_is_tolerated_as_warn(host: Host) -> None:
    _write(host.root / "sys/fs/cgroup/cgroup.subtree_control", "cpuset cpu io memory\n")
    result = checks.check_cgroup(host.probe())
    assert result.status is Status.WARN
    assert "pids" in result.message
    assert result.fixes == ()


@pytest.mark.fr("FR-007")
def test_cgroup_supervisor_leaf_missing_fails(host: Host) -> None:
    shutil.rmtree(host.root / fake.SUPERVISOR)
    result = checks.check_cgroup(host.probe())
    assert result.status is Status.FAIL
    assert "supervisor" in result.message


@pytest.mark.fr("FR-007")
def test_user_owned_supervisor_leaf_gets_root_chown_fix(host: Host) -> None:
    probe = dataclasses.replace(host.probe(), owner_of=lambda path: os.lstat(path).st_uid)
    result = checks.check_cgroup_owner(probe)
    assert result.status is Status.FAIL
    leaf = f"/sys/fs/cgroup/ginn/{UID}/supervisor"
    assert result.fixes == (
        f"sudo chown root:root {leaf} {leaf}/cgroup.procs {leaf}/cgroup.threads",
    )


@pytest.mark.fr("FR-007")
def test_root_owned_user_dir_gets_user_chown_fix(host: Host) -> None:
    user_dir = host.root / f"sys/fs/cgroup/ginn/{UID}"
    others = fake.owner_fake(host.root)

    def owner(path: Path) -> int:
        return 0 if path == user_dir else others(path)

    result = checks.check_cgroup_owner(dataclasses.replace(host.probe(), owner_of=owner))
    assert result.status is Status.FAIL
    assert result.fixes == (f"sudo chown {UID}:{os.getgid()} /sys/fs/cgroup/ginn/{UID}",)


@pytest.mark.fr("FR-007")
def test_cgroup_owner_good_passes(host: Host) -> None:
    assert checks.check_cgroup_owner(host.probe()).status is Status.PASS


@pytest.mark.fr("FR-007")
def test_shared_dir_writable_by_group_fails_with_chmod(host: Host) -> None:
    os.chmod(host.root / "etc/ginn", 0o775)  # noqa: S103 - a wrong mode, on purpose
    result = checks.check_shared_dirs(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == ("sudo chmod 0755 /etc/ginn",)


@pytest.mark.fr("FR-007")
def test_shared_dir_owned_by_user_fails_with_chown(host: Host) -> None:
    probe = dataclasses.replace(host.probe(), owner_of=lambda path: UID)
    result = checks.check_shared_dirs(probe)
    assert result.status is Status.FAIL
    assert any(fix.startswith("sudo chown root:root /etc/") for fix in result.fixes)


@pytest.mark.fr("FR-007")
def test_absent_shared_dirs_are_not_faults(host: Host) -> None:
    shutil.rmtree(host.root / "run/runit")
    assert checks.check_shared_dirs(host.probe()).status is Status.PASS


@pytest.mark.fr("FR-007")
def test_no_shared_dirs_is_info(host: Host) -> None:
    for rel in fake.SHARED:
        shutil.rmtree(host.root / rel)
    assert checks.check_shared_dirs(host.probe()).status is Status.INFO


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize(
    ("max_userns", "clone", "status", "fix"),
    [
        ("15000\n", None, Status.PASS, None),
        ("0\n", None, Status.FAIL, "sudo sysctl -w user.max_user_namespaces=15000"),
        ("15000\n", "0\n", Status.FAIL, "sudo sysctl -w kernel.unprivileged_userns_clone=1"),
    ],
)
def test_user_namespaces(
    host: Host, max_userns: str, clone: str | None, status: Status, fix: str | None
) -> None:
    _write(host.root / "proc/sys/user/max_user_namespaces", max_userns)
    if clone is None:
        (host.root / "proc/sys/kernel/unprivileged_userns_clone").unlink()
    else:
        _write(host.root / "proc/sys/kernel/unprivileged_userns_clone", clone)
    result = checks.check_userns(host.probe())
    assert result.status is status
    if fix is not None:
        assert fix in result.fixes


@pytest.mark.fr("FR-007")
def test_missing_userns_sysctl_fails(host: Host) -> None:
    (host.root / "proc/sys/user/max_user_namespaces").unlink()
    assert checks.check_userns(host.probe()).status is Status.FAIL


@pytest.mark.fr("FR-007")
def test_overlay_or_fuse_overlay_is_needed(host: Host) -> None:
    assert checks.check_overlay_fuse(host.probe()).status is Status.PASS
    _write(host.root / "proc/filesystems", "nodev\tproc\n\tfuse\n")
    result = checks.check_overlay_fuse(host.probe())
    assert result.status is Status.FAIL
    assert "sudo modprobe overlay" in result.fixes
    stub = host.bin_dir / "fuse-overlayfs"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    assert checks.check_overlay_fuse(host.probe()).status is Status.PASS


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize(
    ("free", "status"),
    [(1 * GIB, Status.FAIL), (5 * GIB, Status.WARN), (20 * GIB, Status.PASS)],
)
def test_free_disk_thresholds(host: Host, free: int, status: Status) -> None:
    result = checks.check_disk(host.probe(free=free))
    assert result.status is status
    if status is Status.FAIL:
        assert "space must be freed" in result.fixes[0]


@pytest.mark.fr("FR-069")
def test_nvidia_on_musl_is_informational_not_failure(host: Host) -> None:
    """FR-069 AC-2: NVIDIA on musl is reported as informational, never as a failure."""
    _write(host.root / "lib/ld-musl-x86_64.so.1", "")
    result = checks.check_nvidia(host.probe())
    assert result.status is Status.INFO
    assert "NVIDIA unsupported on musl" in result.message


@pytest.mark.fr("FR-069")
def test_nvidia_on_glibc_reports_driver_presence(host: Host) -> None:
    assert checks.check_nvidia(host.probe()).status is Status.PASS
    _write(host.root / "proc/driver/nvidia/version", "NVRM version: 550\n")
    assert checks.check_nvidia(host.probe()).status is Status.PASS


ELOGIND_CASES: list[tuple[str, dict[str, str], Status, str]] = [
    ("", {}, Status.PASS, "unset"),
    ("[Login]\n#KillUserProcesses=yes\n", {}, Status.PASS, "commented out"),
    ("[Login]\nKillUserProcesses=yes\n", {}, Status.FAIL, "main file yes"),
    (
        "[Login]\nKillUserProcesses=yes\n",
        {"10-x.conf": "[Login]\nKillUserProcesses=no\n"},
        Status.PASS,
        "drop-in no wins over main yes",
    ),
    (
        "[Login]\nKillUserProcesses=no\n",
        {"50-x.conf": "[Login]\nKillUserProcesses=yes\n"},
        Status.FAIL,
        "drop-in yes wins over main no",
    ),
    (
        "",
        {
            "10-a.conf": "[Login]\nKillUserProcesses=yes\n",
            "20-b.conf": "[Login]\nKillUserProcesses=no\n",
        },
        Status.PASS,
        "sorted drop-ins: 20-b last",
    ),
    (
        "",
        {
            "10-a.conf": "[Login]\nKillUserProcesses=no\n",
            "20-b.conf": "[Login]\nKillUserProcesses=yes\n",
        },
        Status.FAIL,
        "sorted drop-ins: 20-b yes last",
    ),
    (
        "[Login]\nKillUserProcesses=yes\nKillUserProcesses=\n",
        {},
        Status.PASS,
        "an empty value resets to no",
    ),
    ("[Other]\nKillUserProcesses=yes\n", {}, Status.PASS, "outside [Login] is ignored"),
    ("[Login]\nKillUserProcesses = Yes \n", {}, Status.FAIL, "spacing and case are tolerated"),
]


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize(("main", "dropins", "status", "label"), ELOGIND_CASES)
def test_elogind_kill_user_processes_last_wins(
    host: Host, main: str, dropins: dict[str, str], status: Status, label: str
) -> None:
    conf = host.root / "etc/elogind/logind.conf"
    if main:
        _write(conf, main)
    else:
        conf.unlink()
    for name, text in dropins.items():
        _write(host.root / "etc/elogind/logind.conf.d" / name, text)
    result = checks.check_elogind(host.probe())
    assert result.status is status, label
    if status is Status.FAIL:
        assert "KillUserProcesses=no" in result.fixes[1]
        assert result.fixes[2] == "sudo sv restart elogind"


@pytest.mark.fr("FR-007")
def test_elogind_failure_names_the_file_that_sets_yes(host: Host) -> None:
    _write(host.root / "etc/elogind/logind.conf.d/50-x.conf", "[Login]\nKillUserProcesses=yes\n")
    result = checks.check_elogind(host.probe())
    assert result.status is Status.FAIL
    assert "/etc/elogind/logind.conf.d/50-x.conf" in result.message


@pytest.mark.fr("FR-007")
def test_render_nodes_report_their_modes(host: Host) -> None:
    result = checks.check_render_nodes(host.probe())
    assert result.status is Status.PASS
    assert "/dev/dri/renderD128 0666" in result.message
    os.chmod(host.root / "dev/dri/renderD128", 0o660)
    result = checks.check_render_nodes(host.probe())
    assert result.status is Status.WARN
    assert "keep-groups" in result.message
    assert result.fixes == ()


@pytest.mark.fr("FR-007")
def test_no_render_node_is_info(host: Host) -> None:
    (host.root / "dev/dri/renderD128").unlink()
    assert checks.check_render_nodes(host.probe()).status is Status.INFO


@pytest.mark.fr("FR-007")
def test_podman_check_runs_the_replay_shim_with_ginn_flags(host: Host) -> None:
    result = checks.check_podman(host.probe())
    assert result.status is Status.PASS
    assert "runs rootless" in result.message


@pytest.mark.fr("FR-007")
def test_podman_not_on_path_fails(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = host.home / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = checks.check_podman(dataclasses.replace(host.probe(), env={"PATH": str(empty)}))
    assert result.status is Status.FAIL
    assert "not on PATH" in result.message


@pytest.mark.fr("FR-007")
def test_missing_rootless_helper_fails(host: Host) -> None:
    (host.bin_dir / "newuidmap").unlink()
    result = checks.check_podman(host.probe())
    assert result.status is Status.FAIL
    assert "newuidmap" in result.message


@pytest.mark.fr("FR-007")
def test_podman_config_not_written_yet_is_warn_and_podman_is_not_run(host: Host) -> None:
    (host.home / ".config/ginn/containers.conf").unlink()
    result = checks.check_podman(host.probe())
    assert result.status is Status.WARN
    assert "not written yet" in result.message


@pytest.mark.fr("FR-007")
def test_podman_info_failure_fails_with_stderr_line(host: Host, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures-bad"
    fixtures.mkdir()
    _write(
        fixtures / "info.json",
        json.dumps({"exit": 125, "stderr": "Error: cannot set up namespace\n"}),
    )
    install_podman(host.bin_dir, fixtures)
    result = checks.check_podman(host.probe())
    assert result.status is Status.FAIL
    assert "cannot set up namespace" in result.message
    assert result.fixes == ("sudo xbps-install -S podman shadow crun conmon passt",)


@pytest.mark.fr("FR-007")
def test_podman_not_rootless_fails(host: Host, tmp_path: Path) -> None:
    info = json.loads((fake.FIXTURES / "info.json").read_text(encoding="utf-8"))
    info["host"]["security"]["rootless"] = False
    fixtures = tmp_path / "fixtures-rootful"
    fixtures.mkdir()
    _write(fixtures / "info.json", json.dumps(info))
    install_podman(host.bin_dir, fixtures)
    result = checks.check_podman(host.probe())
    assert result.status is Status.WARN
    assert "diagnostic" in result.message


@pytest.mark.fr("FR-007")
def test_podman_is_not_checked_without_a_runtime_dir(host: Host) -> None:
    (host.root / f"run/ginn/{UID}").rmdir()
    result = checks.check_podman(host.probe())
    assert result.status is Status.INFO
    assert "not checked" in result.message


@pytest.mark.fr("FR-007")
def test_checks_write_nothing(host: Host) -> None:
    """Doctor writes nothing: the fake host, the home and the bin directory are unchanged."""
    before = {path: fake.snapshot(path) for path in (host.root, host.home, host.bin_dir)}
    checks.run_checks(host.probe())
    after = {path: fake.snapshot(path) for path in (host.root, host.home, host.bin_dir)}
    assert after == before


@pytest.mark.fr("NFR-041")
@pytest.mark.fr("FR-007")
def test_every_dependency_and_host_setup_item_has_a_check(host: Host) -> None:
    """NFR-041: each Host Setup item and each SRS 2.5 dependency that doctor covers has a check.

    DEP-4 (image channels) and DEP-5 (package repositories and the AUR) are network dependencies,
    outside doctor's scope. DEP-3 (the XDG_RUNTIME_DIR provider) is covered only by the elogind
    config check. ginn's own runtime root is /run/ginn/<uid>, checked by runtime-dir (ADR-004).
    """
    names = {result.name for result in checks.run_checks(host.probe())}
    covered = {
        "DEP-1": {"podman"},  # podman, its helpers and the network helper
        "DEP-2": {"cgroup", "userns", "overlay-fuse"},
        "DEP-3": {"elogind"},
        "DEP-6": {"runsvdir", "runit-dir", "sv-dir", "service-link", "supervise-links"},
    }
    assert set().union(*covered.values()) <= names
    assert names == {
        "podman",
        "subids",  # FR-001, FR-007 AC-1
        "runtime-dir",  # ADR-004
        "runsvdir",  # DEP-6
        "runit-dir",  # ADR-013 Decision 21
        "sv-dir",  # FR-001
        "service-link",  # FR-001, ADR-013 Decision 10
        "supervise-links",  # ADR-013 Decision 21
        "cgroup",  # DEP-2, FR-080
        "cgroup-owner",  # ADR-003
        "shared-dirs",  # ADR-013 Decision 18
        "userns",  # DEP-2
        "overlay-fuse",  # DEP-2
        "disk",  # FR-007
        "nvidia",  # FR-069
        "elogind",  # DEP-3, ADR-004
        "render-nodes",  # S-06
    }
    assert len(names) == len(checks.CHECKS)


@pytest.mark.fr("FR-007")
def test_podman_check_echoes_its_argv_for_verbose_mode(host: Host) -> None:
    """A failed podman info points at `ginn --verbose doctor`, so the argv must reach verbose."""
    lines: list[str] = []
    checks.check_podman(dataclasses.replace(host.probe(), verbose=lines.append))
    assert len(lines) == 1
    assert "--remote=false" in lines[0]
    assert "--runroot" in lines[0]
    assert "--runtime crun" in lines[0]


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize(
    ("tool", "package"), [("crun", "crun"), ("conmon", "conmon"), ("newuidmap", "shadow")]
)
def test_missing_podman_helper_fails_with_its_package(host: Host, tool: str, package: str) -> None:
    (host.bin_dir / tool).unlink()
    result = checks.check_podman(host.probe())
    assert result.status is Status.FAIL
    assert tool in result.message
    assert result.fixes[0].startswith("sudo xbps-install -S ")
    assert package in result.fixes[0].split()


@pytest.mark.fr("FR-007")
def test_podman_needs_pasta_or_slirp4netns(host: Host) -> None:
    (host.bin_dir / "pasta").unlink()
    result = checks.check_podman(host.probe())
    assert result.status is Status.FAIL
    assert "pasta or slirp4netns" in result.message
    assert "passt" in result.fixes[0].split()
    stub = host.bin_dir / "slirp4netns"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    assert checks.check_podman(host.probe()).status is Status.PASS


@pytest.mark.fr("FR-007")
def test_podman_gets_ginn_storage_flags_and_runtime_env(host: Host, tmp_path: Path) -> None:
    """The podman argv carries ginn's --root, --runroot and --tmpdir; the env holds the runtime."""
    env_log = tmp_path / "podman-env.txt"
    install_podman(host.bin_dir, env_log=env_log)
    lines: list[str] = []
    checks.check_podman(dataclasses.replace(host.probe(), verbose=lines.append))
    argv = shlex.split(lines[0])
    runtime = host.root / f"run/ginn/{UID}"
    assert argv[argv.index("--root") + 1] == str(host.home / ".local/share/ginn/podman")
    assert argv[argv.index("--runroot") + 1] == str(runtime / "podman")
    assert argv[argv.index("--tmpdir") + 1] == str(runtime / "podman-tmp")
    assert argv[argv.index("--runtime") + 1] == "crun"
    assert "--remote=false" in argv
    env = dict(
        line.split("=", 1)
        for line in env_log.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert env["XDG_RUNTIME_DIR"] == str(runtime)
    assert env["CONTAINERS_CONF"] == str(host.home / ".config/ginn/containers.conf")
    assert env["TMPDIR"] == str(host.home / ".local/share/ginn/image-tmp")


@pytest.mark.fr("FR-007")
def test_podman_info_failure_fix_is_a_real_reinstall(host: Host, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures-broken"
    fixtures.mkdir()
    _write(fixtures / "info.json", json.dumps({"exit": 125, "stderr": "Error: broken\n"}))
    install_podman(host.bin_dir, fixtures)
    result = checks.check_podman(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes[0].startswith("sudo xbps-install -S podman")
    assert "ginn --verbose doctor" not in result.fixes[0]


@pytest.mark.fr("FR-007")
def test_supervise_real_directory_gets_stop_remove_and_link_fix(host: Host) -> None:
    link = host.root / "etc/sv/ginn-alice/supervise"
    link.unlink()
    link.mkdir()
    result = checks.check_supervise_links(host.probe())
    assert result.status is Status.FAIL
    assert "stops services-on realms" in result.message
    assert result.fixes == (
        "sudo sv down /var/service/ginn-alice",
        "sudo rm -r /etc/sv/ginn-alice/supervise",
        "sudo ln -s /run/runit/supervise.ginn-alice /etc/sv/ginn-alice/supervise",
    )


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_runtime_dir_that_is_a_link_or_file_is_removed_before_restart(
    host: Host, kind: str
) -> None:
    runtime = host.root / f"run/ginn/{UID}"
    shutil.rmtree(runtime)
    if kind == "symlink":
        elsewhere = host.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, runtime)
    else:
        runtime.write_text("", encoding="utf-8")
    result = checks.check_runtime_dir(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == (f"sudo rm /run/ginn/{UID}", "sudo sv restart /var/service/ginn-alice")
    assert "stops services-on realms" in result.message


@pytest.mark.fr("FR-007")
def test_shared_path_that_is_a_file_is_removed_by_hand_then_host_setup(host: Host) -> None:
    shutil.rmtree(host.root / "run/ginn")
    _write(host.root / "run/ginn", "")
    result = checks.check_shared_dirs(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == ("sudo rm /run/ginn", "sudo ginn host-setup --user alice")


@pytest.mark.fr("FR-007")
def test_runsvdir_must_run_the_ginn_sv_directory(host: Host) -> None:
    (host.root / "proc/4242/cmdline").write_bytes(b"runsvdir\x00-P\x00/state/sv\x00")
    result = checks.check_runsvdir(host.probe())
    assert result.status is Status.FAIL
    assert result.fixes == ("sudo sv up /var/service/ginn-alice",)


@pytest.mark.fr("FR-007")
def test_service_chain_refuses_a_symlinked_var(host: Host) -> None:
    real = host.root / "var-real"
    os.rename(host.root / "var", real)
    os.symlink("var-real", host.root / "var")
    result = checks.check_service_link(host.probe())
    assert result.status is Status.FAIL
    assert "root-owned directory" in result.message


@pytest.mark.fr("FR-007")
def test_service_chain_refuses_a_final_directory_not_owned_by_root(host: Host) -> None:
    final = host.root / fake.DEFAULT_DIR
    fallback = fake.owner_fake(host.root)

    def owner(path: Path) -> int:
        return UID if path == final else fallback(path)

    result = checks.check_service_link(dataclasses.replace(host.probe(), owner_of=owner))
    assert result.status is Status.FAIL
    assert "root-owned directory" in result.message


@pytest.mark.fr("FR-007")
def test_disk_fix_quotes_the_path(host: Host) -> None:
    spaced = host.home / "my data"
    spaced.mkdir()
    env = {**host.probe().env, "XDG_DATA_HOME": str(spaced)}
    probe = dataclasses.replace(host.probe(free=1 * GIB), env=env)
    result = checks.check_disk(probe)
    assert result.status is Status.FAIL
    assert shlex.quote(str(spaced)) in result.fixes[0]


@pytest.mark.fr("FR-007")
def test_elogind_failure_says_it_may_end_the_graphical_session(host: Host) -> None:
    _write(host.root / "etc/elogind/logind.conf", "[Login]\nKillUserProcesses=yes\n")
    result = checks.check_elogind(host.probe())
    assert result.status is Status.FAIL
    assert "graphical session" in result.message
    assert result.fixes[-1] == "sudo sv restart elogind"


ELOGIND_TREE_CASES: list[tuple[list[tuple[str, str]], Status, str]] = [
    (
        [
            ("usr/lib/elogind/logind.conf.d/10-x.conf", "[Login]\nKillUserProcesses=yes\n"),
            ("etc/elogind/logind.conf.d/10-x.conf", "[Login]\nKillUserProcesses=no\n"),
        ],
        Status.PASS,
        "etc hides usr/lib with the same name",
    ),
    (
        [
            ("etc/elogind/logind.conf.d/10-x.conf", "[Login]\nKillUserProcesses=no\n"),
            ("run/elogind/logind.conf.d/20-y.conf", "[Login]\nKillUserProcesses=yes\n"),
        ],
        Status.FAIL,
        "a run drop-in sorted after an etc drop-in wins",
    ),
    (
        [
            ("usr/lib/elogind/logind.conf.d/05-z.conf", "[Login]\nKillUserProcesses=yes\n"),
            ("run/elogind/logind.conf.d/10-x.conf", "[Login]\nKillUserProcesses=yes\n"),
            ("etc/elogind/logind.conf.d/10-x.conf", "[Login]\nKillUserProcesses=no\n"),
        ],
        Status.PASS,
        "05-z is read first, and the etc 10-x hides the run 10-x",
    ),
    (
        [("run/elogind/logind.conf", "[Login]\nKillUserProcesses=yes\n")],
        Status.FAIL,
        "a main file under run is read when etc has none",
    ),
    (
        [
            ("etc/elogind/logind.conf", "[Login]\nKillUserProcesses=no\n"),
            ("run/elogind/logind.conf", "[Login]\nKillUserProcesses=yes\n"),
        ],
        Status.PASS,
        "the etc main file hides the run main file",
    ),
]


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize(("files", "status", "label"), ELOGIND_TREE_CASES)
def test_elogind_reads_every_config_tree_with_standard_precedence(
    host: Host, files: list[tuple[str, str]], status: Status, label: str
) -> None:
    conf = host.root / "etc/elogind/logind.conf"
    if conf.exists():
        conf.unlink()
    for rel, text in files:
        _write(host.root / rel, text)
    assert checks.check_elogind(host.probe()).status is status, label


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("y", Status.FAIL),
        ("t", Status.FAIL),
        ("yes", Status.FAIL),
        ("true", Status.FAIL),
        ("on", Status.FAIL),
        ("1", Status.FAIL),
        ("TRUE", Status.FAIL),
        ("n", Status.PASS),
        ("no", Status.PASS),
        ("off", Status.PASS),
        ("0", Status.PASS),
    ],
)
def test_elogind_boolean_spellings(host: Host, value: str, status: Status) -> None:
    _write(host.root / "etc/elogind/logind.conf", f"[Login]\nKillUserProcesses={value}\n")
    assert checks.check_elogind(host.probe()).status is status
