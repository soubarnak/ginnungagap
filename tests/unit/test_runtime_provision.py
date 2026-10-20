# SPDX-License-Identifier: Apache-2.0
"""The provisioning stage's names, argv and commit configuration (ADR-015 Decisions 2, 3 and 10).

The builders run no podman, so these tests check the argv and change lists that the ops layer
will send. The argv must match the goldens exactly, because podman reads each element.
"""

import hashlib
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
from pathlib import Path

import pytest

from ginn.distros import available
from ginn.distros import load as load_distro
from ginn.distros.loader import Verb
from ginn.domain.names import RealmName
from ginn.errors import GinnError
from ginn.runtime.provision import (
    GETENT_NOT_FOUND,
    clean_argvs,
    derived_image_name,
    derived_image_prefix,
    final_commit_changes,
    init_set_hash,
    install_argv,
    integration_step_argvs,
    keep_id_entry_check_argv,
    keep_id_entry_drop_argv,
    manifest_digest,
    provision_create_spec,
    provision_label,
    provision_name,
    provision_name_prefix,
    refresh_argv,
    removal_steps,
    reset_create_spec,
    reset_name,
    stage_commit_changes,
    stage_image_name,
)

REALM = RealmName("arch")
OTHER = RealmName("arch-two")
GEN = 2
DIGEST = "docker.io/library/debian@sha256:" + "ab" * 32
BASE_DIGEST = "sha256:" + "ab" * 32
STAGE_ID = "1f" * 32
INIT_HASH = init_set_hash(["systemd", "systemd-sysv", "dbus"])

PROVISION_ARGV = [
    "create",
    "--name",
    "ginn_provision_arch_g2",
    "--user",
    "0:0",
    "--userns",
    "keep-id",
    "--network",
    "host",
    "--http-proxy=false",
    "--init",
    "--label",
    "io.ginn.provision=arch:2",
    "--entrypoint",
    '["sleep"]',
    DIGEST,
    "infinity",
]
"""The provisioning create: the services-off keep-alive as root under keep-id (ADR-015 2b)."""

RESET_ARGV = [
    "create",
    "--name",
    "ginn_provision_arch_g2_reset",
    "--network",
    "host",
    "--http-proxy=false",
    "--label",
    "io.ginn.provision=arch:2",
    "--entrypoint",
    "[]",
    STAGE_ID,
]
"""The reset create: the stage image by ID, a cleared entrypoint and no command (ADR-015 2g)."""

FORBIDDEN_RUNTIME_FLAGS = (
    "--mount",
    "--volume",
    "--tmpfs",
    "--device",
    "--passwd-entry",
    "--cap-add",
    "--security-opt",
    "--group-add",
    "--env",
    "--systemd",
    "--init-path",
    "--cgroup-parent",
    "--privileged",
)
"""The flags a provisioning or reset create never carries (CR-007, DR-010, NFR-SEC-001)."""

SIGNATURE_BYPASS = (
    "--allow-unauthenticated",
    "--allow-insecure-repositories",
    "--force-yes",
    "--nogpgcheck",
    "--gpgcheck=0",
    "--no-gpg-checks",
)
"""Options that turn a package manager's signature check off (FR-012). No argv may carry one."""


def _distro_argvs() -> list[list[str]]:
    """Every in-container argv that the data builds, for each distribution."""
    argvs: list[list[str]] = []
    for name in available():
        distro = load_distro(name)
        argvs.append(refresh_argv(distro))
        argvs.extend(clean_argvs(distro))
        init = install_argv(distro, distro.services_packages)
        if init is not None:
            argvs.append(init)
        integration = distro.integration_set("en_US.UTF-8")
        ips = install_argv(distro, integration.packages)
        if ips is not None:
            argvs.append(ips)
        argvs.extend(integration_step_argvs(integration))
    return argvs


@pytest.mark.fr("DR-010")
def test_the_provisioning_names_follow_decision_ten() -> None:
    assert provision_name(REALM, GEN) == "ginn_provision_arch_g2"
    assert reset_name(REALM, GEN) == "ginn_provision_arch_g2_reset"
    assert provision_label(REALM, GEN) == "arch:2"
    assert derived_image_name(REALM, GEN) == "localhost/ginn-init-arch:g2"
    assert stage_image_name(REALM, GEN) == "localhost/ginn-init-arch:g2-stage"


@pytest.mark.fr("DR-010")
def test_each_sweep_prefix_matches_its_own_realm_and_no_other_realm() -> None:
    for name in (provision_name(REALM, GEN), reset_name(REALM, GEN)):
        assert name.startswith(provision_name_prefix(REALM))
        assert not name.startswith(provision_name_prefix(OTHER))
    for name in (derived_image_name(REALM, GEN), stage_image_name(REALM, GEN)):
        assert name.startswith(derived_image_prefix(REALM))
        assert not name.startswith(derived_image_prefix(OTHER))
    assert derived_image_prefix(REALM) == "localhost/ginn-init-arch:"
    assert provision_name_prefix(REALM) == "ginn_provision_arch_"


@pytest.mark.fr("DR-010")
@pytest.mark.parametrize("generation", [0, -1, True])
def test_a_generation_that_is_not_a_positive_int_is_refused(generation: int) -> None:
    with pytest.raises(ValueError, match="generation"):
        provision_name(REALM, generation)
    with pytest.raises(ValueError, match="generation"):
        derived_image_name(REALM, generation)


@pytest.mark.fr("CR-007")
def test_the_provisioning_create_is_the_services_off_keep_alive_as_root_under_keep_id() -> None:
    assert provision_create_spec(REALM, GEN, DIGEST).to_argv() == PROVISION_ARGV


@pytest.mark.fr("CR-007")
def test_the_reset_create_clears_the_entrypoint_with_no_command_from_the_stage_image() -> None:
    argv = reset_create_spec(REALM, GEN, DIGEST, STAGE_ID).to_argv()
    assert argv == RESET_ARGV
    assert argv[-1] == STAGE_ID
    assert "--init" not in argv
    assert "--user" not in argv


@pytest.mark.fr("CR-007")
@pytest.mark.parametrize("flag", FORBIDDEN_RUNTIME_FLAGS)
def test_no_realm_home_device_passwd_entry_or_mount_reaches_a_provisioning_argv(flag: str) -> None:
    for argv in (
        provision_create_spec(REALM, GEN, DIGEST).to_argv(),
        reset_create_spec(REALM, GEN, DIGEST, STAGE_ID).to_argv(),
    ):
        assert flag not in argv


@pytest.mark.fr("CR-007")
def test_a_provisioning_create_has_no_realm_label_only_the_provision_label() -> None:
    argv = provision_create_spec(REALM, GEN, DIGEST).to_argv()
    labels = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--label"]
    assert labels == ["io.ginn.provision=arch:2"]


@pytest.mark.fr("FR-082")
def test_the_provisioning_container_runs_no_systemd_and_no_sbin_init() -> None:
    argv = provision_create_spec(REALM, GEN, DIGEST).to_argv()
    assert not any(arg.startswith("--systemd") for arg in argv)
    assert "/sbin/init" not in argv
    assert argv[argv.index("--entrypoint") + 1] == '["sleep"]'


@pytest.mark.fr("DR-010")
def test_the_provisioning_argv_names_no_host_path_and_its_images_are_local() -> None:
    for argv in (
        provision_create_spec(REALM, GEN, DIGEST).to_argv(),
        reset_create_spec(REALM, GEN, DIGEST, STAGE_ID).to_argv(),
    ):
        assert not any(arg.startswith("/") for arg in argv)
    assert derived_image_name(REALM, GEN).startswith("localhost/")
    assert stage_image_name(REALM, GEN).startswith("localhost/")


@pytest.mark.fr("DR-040")
def test_the_reset_takes_a_bare_stage_id_and_the_base_digest_is_checked() -> None:
    with pytest.raises(ValueError, match="ADR-015"):
        reset_create_spec(REALM, GEN, DIGEST, "sha256:" + STAGE_ID)
    with pytest.raises(ValueError, match="FR-011"):
        provision_create_spec(REALM, GEN, "debian:stable")


@pytest.mark.fr("CR-007")
def test_the_stage_commit_sets_user_and_env_and_clears_the_realm_identity() -> None:
    changes = stage_commit_changes(
        image_user="1000:1000",
        image_env=["PATH=/usr/bin", "container=podman", "HOME=/root"],
        init_set=INIT_HASH,
        base_digest=BASE_DIGEST,
    )
    assert changes == [
        "USER 1000:1000",
        "ENV PATH=/usr/bin",
        "ENV container=podman",
        "ENV HOME=/root",
        "LABEL io.ginn.provision=",
        f"LABEL io.ginn.init-set={INIT_HASH}",
        f"LABEL io.ginn.base-digest={BASE_DIGEST}",
    ]
    assert not any(change.startswith(("ENTRYPOINT", "CMD")) for change in changes)


@pytest.mark.fr("CR-007")
@pytest.mark.parametrize(
    ("image_user", "first"),
    [(None, "USER 0"), ("", "USER 0"), ("nobody", "USER nobody"), ("0:0", "USER 0:0")],
)
def test_the_stage_commit_user_is_the_pinned_image_user_or_root(
    image_user: str | None, first: str
) -> None:
    changes = stage_commit_changes(
        image_user=image_user, image_env=[], init_set=INIT_HASH, base_digest=BASE_DIGEST
    )
    assert changes[0] == first


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("bad_user", ["1000 1000", "$(id)", "root;id", "a\nb"])
def test_a_stage_user_that_is_not_a_name_or_uid_gid_is_refused(bad_user: str) -> None:
    with pytest.raises(ValueError, match="ADR-015"):
        stage_commit_changes(
            image_user=bad_user, image_env=[], init_set=INIT_HASH, base_digest=BASE_DIGEST
        )


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("bad_env", ["PATH", "=x", "1BAD=x", "A=b\nc", "A=b\0c"])
def test_an_image_env_entry_that_is_not_key_equals_value_is_refused(bad_env: str) -> None:
    with pytest.raises(ValueError, match="KEY=VALUE"):
        stage_commit_changes(
            image_user=None, image_env=[bad_env], init_set=INIT_HASH, base_digest=BASE_DIGEST
        )


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "bad_env",
    ["A=b,c", "A= b", "A=b ", "A=\tb", 'A="x"', "A='x", "A=x'", 'A="', "A=x'\""],
)
def test_an_image_env_value_with_a_comma_edge_space_or_quote_is_refused(bad_env: str) -> None:
    with pytest.raises(ValueError, match="KEY=VALUE"):
        stage_commit_changes(
            image_user=None, image_env=[bad_env], init_set=INIT_HASH, base_digest=BASE_DIGEST
        )


@pytest.mark.fr("CR-007")
@pytest.mark.parametrize("entry", ["A=", "PATH=/usr/local/sbin:/usr/bin", "NOTE=a b", "Q=it's"])
def test_an_image_env_value_that_keeps_its_text_is_committed_unchanged(entry: str) -> None:
    changes = stage_commit_changes(
        image_user=None, image_env=[entry], init_set=INIT_HASH, base_digest=BASE_DIGEST
    )
    assert changes[1] == f"ENV {entry}"


@pytest.mark.fr("FR-061")
@pytest.mark.fr("CR-007")
def test_every_provisioning_stage_create_passes_http_proxy_false() -> None:
    for spec in (
        provision_create_spec(REALM, GEN, DIGEST),
        reset_create_spec(REALM, GEN, DIGEST, STAGE_ID),
    ):
        assert spec.http_proxy is False
        assert "--http-proxy=false" in spec.to_argv()


@pytest.mark.fr("CR-007")
def test_the_final_commit_clears_the_provision_label_and_keeps_the_two_ginn_labels() -> None:
    assert final_commit_changes(init_set=INIT_HASH, base_digest=BASE_DIGEST) == [
        f"LABEL io.ginn.init-set={INIT_HASH}",
        f"LABEL io.ginn.base-digest={BASE_DIGEST}",
        "LABEL io.ginn.provision=",
    ]


@pytest.mark.fr("CR-007")
def test_no_commit_change_names_a_realm_label_or_resets_the_entrypoint() -> None:
    changes = [
        *stage_commit_changes(
            image_user=None,
            image_env=["PATH=/usr/bin"],
            init_set=INIT_HASH,
            base_digest=BASE_DIGEST,
        ),
        *final_commit_changes(init_set=INIT_HASH, base_digest=BASE_DIGEST),
    ]
    for change in changes:
        assert not any(key in change for key in ("io.ginn.realm", "io.ginn.distro", "ENTRYPOINT"))
        assert "\n" not in change and "\0" not in change


@pytest.mark.fr("CR-007")
@pytest.mark.parametrize("init_set", ["zz" * 32, "AB" * 32, "ab" * 31])
def test_an_init_set_hash_that_is_not_64_lowercase_hex_is_refused(init_set: str) -> None:
    with pytest.raises(ValueError, match="io.ginn.init-set"):
        final_commit_changes(init_set=init_set, base_digest=BASE_DIGEST)


@pytest.mark.fr("FR-011")
def test_the_base_digest_label_is_the_manifest_digest_with_its_prefix() -> None:
    assert manifest_digest(DIGEST) == BASE_DIGEST
    with pytest.raises(ValueError, match="FR-011"):
        manifest_digest("debian:stable")
    with pytest.raises(ValueError, match="io.ginn.base-digest"):
        final_commit_changes(init_set=INIT_HASH, base_digest=BASE_DIGEST.removeprefix("sha256:"))


@pytest.mark.fr("FR-013")
def test_the_init_set_hash_is_sha256_of_the_sorted_distinct_names() -> None:
    expected = hashlib.sha256(b"dbus\nsystemd\nsystemd-sysv").hexdigest()
    assert init_set_hash(["systemd-sysv", "dbus", "systemd", "dbus"]) == expected
    assert init_set_hash([]) == hashlib.sha256(b"").hexdigest()
    assert init_set_hash(["dbus", "systemd"]) != expected


@pytest.mark.fr("FR-013")
def test_arch_has_no_init_install_but_still_installs_its_integration_set() -> None:
    arch = load_distro("arch")
    assert arch.services_packages == ()
    assert install_argv(arch, arch.services_packages) is None
    ips = arch.integration_set("en_US.UTF-8").packages
    assert install_argv(arch, ips) == arch.argv(Verb.INSTALL, ips)


@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", available())
def test_the_init_install_is_the_data_install_with_each_name_as_one_element(name: str) -> None:
    distro = load_distro(name)
    if not distro.services_packages:
        assert install_argv(distro, distro.services_packages) is None
        return
    argv = install_argv(distro, distro.services_packages)
    assert argv == distro.argv(Verb.INSTALL, distro.services_packages)
    assert all(package in argv for package in distro.services_packages)


@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("name", available())
def test_the_refresh_and_clean_argv_are_the_data_steps(name: str) -> None:
    distro = load_distro(name)
    assert refresh_argv(distro) == distro.argv(Verb.REFRESH)
    assert clean_argvs(distro) == [list(step) for step in distro.clean_steps]


@pytest.mark.fr("NFR-060")
def test_the_clean_steps_empty_the_cache_and_arch_never_uses_pacman_clean() -> None:
    assert clean_argvs(load_distro("debian"))[0] == ["apt-get", "clean"]
    arch_clean = clean_argvs(load_distro("arch"))
    assert arch_clean == [["find", "/var/cache/pacman/pkg", "-mindepth", "1", "-delete"]]
    assert not any("--clean" in arg for step in arch_clean for arg in step)


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "bad", ["systemd; rm -rf /", "systemd dbus", "-y", "sys$(id)", "a\nb", "systemd-"]
)
def test_a_package_name_that_is_not_a_name_is_refused_before_any_argv_is_built(bad: str) -> None:
    distro = load_distro("debian")
    with pytest.raises(GinnError) as info:
        install_argv(distro, [bad])
    assert info.value.code == "GINN-E-PKG-NAME-INVALID"


@pytest.mark.fr("FR-012")
def test_no_provisioning_argv_carries_a_signature_bypass_for_any_distribution() -> None:
    for argv in _distro_argvs():
        assert not any(arg in SIGNATURE_BYPASS for arg in argv)


@pytest.mark.fr("NFR-SEC-005")
def test_every_provisioning_argv_and_change_is_a_list_of_plain_strings() -> None:
    argvs = [
        PROVISION_ARGV,
        provision_create_spec(REALM, GEN, DIGEST).to_argv(),
        reset_create_spec(REALM, GEN, DIGEST, STAGE_ID).to_argv(),
        *_distro_argvs(),
    ]
    for argv in argvs:
        assert all(type(arg) is str for arg in argv)
    changes = [
        *stage_commit_changes(
            image_user=None,
            image_env=["PATH=/usr/bin"],
            init_set=INIT_HASH,
            base_digest=BASE_DIGEST,
        ),
        *final_commit_changes(init_set=INIT_HASH, base_digest=BASE_DIGEST),
    ]
    assert all(type(change) is str for change in changes)


@pytest.mark.fr("FR-122")
def test_the_removal_order_is_reset_then_stage_by_id_then_provision() -> None:
    assert removal_steps(REALM, GEN, STAGE_ID) == (
        ("container", "ginn_provision_arch_g2_reset"),
        ("image", STAGE_ID),
        ("container", "ginn_provision_arch_g2"),
    )
    with pytest.raises(ValueError, match="ADR-015"):
        removal_steps(REALM, GEN, "sha256:" + STAGE_ID)


@pytest.mark.fr("FR-048")
@pytest.mark.fr("NFR-SEC-005")
def test_the_keep_id_drop_is_a_fixed_sed_argv_on_the_uid_field() -> None:
    argv = keep_id_entry_drop_argv(1000)
    assert argv == ["sed", "-i", r"/^[^:]*:\*:1000:/d", "/etc/passwd"]
    assert all(type(arg) is str for arg in argv)


@pytest.mark.fr("FR-048")
@pytest.mark.fr("NFR-SEC-005")
def test_the_real_sed_argv_removes_only_podman_line_of_the_uid(tmp_path: Path) -> None:
    """The drop argv runs for real, with no shell, on a passwd file. The `*` line of the uid goes;
    an `x` line of the same uid (a pinned base's own account) and a `*` line of uid 10000 stay."""
    passwd = tmp_path / "passwd"
    passwd.write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "soubarna:*:1000:1000:Soubarna:/:/bin/sh\n"
        "builder:x:1000:1000::/home/builder:/bin/sh\n"
        "big:*:10000:10000:big:/:/bin/sh\n",
        encoding="utf-8",
    )
    argv = [*keep_id_entry_drop_argv(1000)[:-1], str(passwd)]
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
    )
    assert passwd.read_text(encoding="utf-8") == (
        "root:x:0:0:root:/root:/bin/bash\n"
        "builder:x:1000:1000::/home/builder:/bin/sh\n"
        "big:*:10000:10000:big:/:/bin/sh\n"
    )


@pytest.mark.fr("FR-048")
@pytest.mark.fr("NFR-SEC-005")
def test_the_getent_check_is_a_fixed_argv_that_passes_only_on_exit_two() -> None:
    assert keep_id_entry_check_argv(1000) == ["getent", "passwd", "1000"]
    assert GETENT_NOT_FOUND == 2
    assert all(type(arg) is str for arg in keep_id_entry_check_argv(1000))


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("bad", [-1, True, "1000", 1000.0])
def test_a_keep_id_uid_that_is_not_a_non_negative_int_is_refused(bad: int | str | float) -> None:
    with pytest.raises(ValueError, match="non-negative int"):
        keep_id_entry_drop_argv(bad)  # type: ignore[arg-type]
