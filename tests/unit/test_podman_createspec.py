# SPDX-License-Identifier: Apache-2.0
"""CreateSpec renders a deterministic `podman create` argv that is never privileged."""

import pytest

from ginn.podman.adapter import CreateSpec, Mount

DIGEST = (
    "docker.io/library/alpine@sha256:"
    "d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395"
)


def basic_spec(**overrides: object) -> CreateSpec:
    """Basic tier with services off: catatonit as init, no systemd (FR-082, FR-061)."""
    fields: dict[str, object] = {
        "name": "ginn-arch-g1",
        "image_digest_ref": DIGEST,
        "network": "host",
        "hostname": "arch",
        "userns": "keep-id",
        "init": True,
        "init_path": "/usr/bin/catatonit",
        "stop_signal": "SIGTERM",
        "stop_timeout": 30,
        "env": {"LANG": "en_GB.UTF-8", "XDG_RUNTIME_DIR": "/run/user/1000"},
        "labels": {"ginn.realm": "arch", "ginn.generation": "1"},
        "mounts": [
            Mount("bind", "/usr/share/fonts", "/usr/share/fonts", ("ro",)),
            Mount("bind", "/etc/localtime", "/etc/localtime", ("ro",)),
        ],
        "devices": ["/dev/dri"],
        "tmpfs": ["/run/user/1000:mode=0700,U"],
    }
    fields.update(overrides)
    return CreateSpec(**fields)  # type: ignore[arg-type]


def services_on_spec(**overrides: object) -> CreateSpec:
    """Services on: the S-01 flag set, systemd as PID 1 in a delegated cgroup (ADR-003)."""
    fields: dict[str, object] = {
        "name": "ginn-arch-g2",
        "image_digest_ref": DIGEST,
        "network": "host",
        "hostname": "arch",
        "user": "0:0",
        "userns": "keep-id",
        "systemd": "always",
        "cgroupns": "private",
        "cgroup_parent": "/ginn/1000/arch",
        "stop_signal": "SIGRTMIN+3",
        "stop_timeout": 30,
        "env": {"LANG": "en_GB.UTF-8"},
        "labels": {"ginn.realm": "arch"},
        "entrypoint_argv": ["/sbin/init"],
    }
    fields.update(overrides)
    return CreateSpec(**fields)  # type: ignore[arg-type]


@pytest.mark.fr("NFR-SEC-001")
def test_basic_spec_golden_argv() -> None:
    assert basic_spec().to_argv() == [
        "create",
        "--name",
        "ginn-arch-g1",
        "--hostname",
        "arch",
        "--userns",
        "keep-id",
        "--network",
        "host",
        "--init",
        "--init-path",
        "/usr/bin/catatonit",
        "--stop-signal",
        "SIGTERM",
        "--stop-timeout",
        "30",
        "--env",
        "LANG=en_GB.UTF-8",
        "--env",
        "XDG_RUNTIME_DIR=/run/user/1000",
        "--label",
        "ginn.generation=1",
        "--label",
        "ginn.realm=arch",
        "--mount",
        "type=bind,source=/usr/share/fonts,target=/usr/share/fonts,ro",
        "--mount",
        "type=bind,source=/etc/localtime,target=/etc/localtime,ro",
        "--tmpfs",
        "/run/user/1000:mode=0700,U",
        "--device",
        "/dev/dri",
        DIGEST,
    ]


@pytest.mark.fr("NFR-SEC-001")
def test_services_on_spec_golden_argv_uses_s01_flags() -> None:
    assert services_on_spec().to_argv() == [
        "create",
        "--name",
        "ginn-arch-g2",
        "--hostname",
        "arch",
        "--user",
        "0:0",
        "--userns",
        "keep-id",
        "--network",
        "host",
        "--cgroupns",
        "private",
        "--cgroup-parent",
        "/ginn/1000/arch",
        "--systemd=always",
        "--stop-signal",
        "SIGRTMIN+3",
        "--stop-timeout",
        "30",
        "--env",
        "LANG=en_GB.UTF-8",
        "--label",
        "ginn.realm=arch",
        "--entrypoint",
        '["/sbin/init"]',
        DIGEST,
    ]


@pytest.mark.fr("NFR-SEC-001")
@pytest.mark.parametrize(
    "spec",
    [
        basic_spec(),
        services_on_spec(),
        basic_spec(network="pasta", caps_add=["NET_RAW"], security_opts=["label=disable"]),
        basic_spec(cdi_devices=["nvidia.com/gpu=all"]),
    ],
    ids=["basic", "services-on", "kali-pasta", "nvidia-cdi"],
)
def test_no_rendered_argv_contains_privileged(spec: CreateSpec) -> None:
    argv = spec.to_argv()
    assert "--privileged" not in argv
    assert not any(arg.startswith("--privileged=") for arg in argv)


@pytest.mark.fr("NFR-SEC-001")
@pytest.mark.parametrize(
    "spec",
    [
        basic_spec(security_opts=["--privileged"]),
        basic_spec(devices=["--privileged"]),
        basic_spec(caps_add=["ALL"]),
        basic_spec(caps_add=["all"]),
    ],
    ids=["security-opt", "device", "cap-all", "cap-all-lower"],
)
def test_privileged_arguments_are_refused(spec: CreateSpec) -> None:
    with pytest.raises(ValueError, match="NFR-SEC-001"):
        spec.to_argv()


@pytest.mark.fr("NFR-SEC-001")
def test_argv_is_deterministic_for_equal_specs() -> None:
    forward = basic_spec(env={"A": "1", "B": "2", "C": "3"}, labels={"x": "1", "y": "2"})
    backward = basic_spec(env={"C": "3", "B": "2", "A": "1"}, labels={"y": "2", "x": "1"})
    assert forward.to_argv() == backward.to_argv()
    assert forward.to_argv() == forward.to_argv()


@pytest.mark.fr("FR-011")
@pytest.mark.parametrize(
    "ref",
    [
        "docker.io/library/alpine:3",
        "alpine",
        "docker.io/library/alpine@sha256:abc",
        "docker.io/library/alpine@md5:" + DIGEST.rpartition(":")[2],
    ],
)
def test_create_requires_full_digest_reference(ref: str) -> None:
    with pytest.raises(ValueError, match="FR-011"):
        basic_spec(image_digest_ref=ref).to_argv()


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "spec",
    [
        basic_spec(mounts=[Mount("bind", "/a,ro=false", "/b")]),
        basic_spec(mounts=[Mount("bind", "/a", "/b", ("ro,exec",))]),
        basic_spec(mounts=[Mount("bind", "/a", "relative")]),
        basic_spec(mounts=[Mount("device", "/a", "/b")]),
        basic_spec(env={"BAD KEY": "1"}),
        basic_spec(labels={"bad=key": "1"}),
        basic_spec(user="root"),
        basic_spec(userns="host"),
        basic_spec(network="bridge"),
        basic_spec(systemd="true"),
        basic_spec(init=False, init_path="/usr/bin/catatonit"),
        basic_spec(name="-rm"),
    ],
    ids=[
        "mount-comma",
        "option-comma",
        "mount-relative",
        "mount-type",
        "env-key",
        "label-key",
        "user-name",
        "userns",
        "network",
        "systemd",
        "init-path-without-init",
        "name-option-like",
    ],
)
def test_invalid_fields_are_refused_before_argv_is_built(spec: CreateSpec) -> None:
    with pytest.raises(ValueError):
        spec.to_argv()


@pytest.mark.fr("NFR-SEC-005")
def test_shell_metacharacters_stay_inside_one_argument() -> None:
    value = "PAYLOAD=a b; rm -rf / $(id)"
    argv = basic_spec(env={"PAYLOAD": "a b; rm -rf / $(id)"}).to_argv()
    index = argv.index(value)
    assert argv[index - 1] == "--env"
    assert argv.count(value) == 1


@pytest.mark.fr("FR-061")
def test_http_proxy_renders_only_when_set() -> None:
    """Unset keeps podman's default. False keeps the host's proxy variables out (T4.1)."""
    assert "--http-proxy=false" in basic_spec(http_proxy=False).to_argv()
    assert "--http-proxy=true" in basic_spec(http_proxy=True).to_argv()
    unset = basic_spec(http_proxy=None).to_argv()
    assert not any(arg.startswith("--http-proxy") for arg in unset)


@pytest.mark.fr("FR-082")
def test_command_follows_the_image_and_is_absent_when_unset() -> None:
    """The command is separate arguments after the image. Unset, the argv ends at the image."""
    argv = basic_spec(entrypoint_argv=["sleep"], command=["infinity"]).to_argv()
    assert argv[-4:] == ["--entrypoint", '["sleep"]', DIGEST, "infinity"]
    assert basic_spec().to_argv()[-1] == DIGEST


ENTRY = "alice:*:1000:1000:alice:/home/alice:/bin/bash"


@pytest.mark.fr("FR-048")
@pytest.mark.fr("NFR-SEC-001")
def test_passwd_entry_renders_after_keep_id_and_passwd_false_is_never_sent() -> None:
    """The realm user is podman's keep-id entry, sent with `--passwd-entry` (T4.9b, ADR-006).

    `--passwd=false` is not a `podman create` flag on podman 5.8.8, so it is never rendered.
    """
    assert "--passwd-entry" not in basic_spec().to_argv()
    argv = basic_spec(passwd_entry=ENTRY).to_argv()
    assert argv[argv.index("--passwd-entry") - 1] == "keep-id"
    assert argv[argv.index("--passwd-entry") + 1] == ENTRY
    assert argv.count("--passwd-entry") == 1
    assert not any(arg.startswith("--passwd=") for arg in argv)


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "entry",
    [
        "alice:*:1000:1000:alice:/home/a$USERNAMEb:/bin/bash",  # podman expands placeholders
        "alice:*:1000:1000:alice:/home/alice:/bin/bash\nroot::0:0::/:/bin/sh",  # a second line
        "alice:*:1000:1000:alice:/home/alice",  # six fields
        "alice:*:1000:1000:alice:/home/a:b:/bin/bash",  # eight fields
    ],
)
def test_a_passwd_entry_podman_would_rewrite_is_refused(entry: str) -> None:
    with pytest.raises(ValueError, match="passwd entry"):
        basic_spec(passwd_entry=entry).to_argv()


@pytest.mark.fr("FR-048")
def test_a_passwd_entry_needs_keep_id() -> None:
    with pytest.raises(ValueError, match="keep-id"):
        basic_spec(passwd_entry=ENTRY, userns=None).to_argv()
