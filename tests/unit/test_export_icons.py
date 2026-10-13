# SPDX-License-Identifier: Apache-2.0
"""Icon lookup inside a realm and the checked copy out (FR-044). Fakes only: no podman, no host."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from ginn.errors import GinnError, make
from ginn.export import icons
from ginn.export._realm import LISTING_MAX_BYTES, LISTING_TIMEOUT_S
from ginn.export.icons import (
    ICON_LIST,
    ICON_MAX_BYTES,
    IconError,
    IconIndex,
    copy_icon,
    resolve_icon,
)
from ginn.podman.adapter import CreateSpec
from ginn.podman.models import ExecResult
from tests.fakes.fake_podman import FakePodman

DIGEST = "registry.example/debian@sha256:" + "ab" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
XPM = b"/* XPM */\nstatic char *x[] = {};\n"
LISTING = "\n".join(
    [
        "f 1200 /usr/share/icons/hicolor/48x48/apps/firefox.png ",
        "f 900 /usr/share/icons/hicolor/scalable/apps/firefox.svg ",
        "f 300 /usr/share/icons/hicolor/512x512/apps/firefox.png ",
        "f 300 /usr/share/icons/hicolor/256x256/apps/firefox.png ",
        "f 300 /usr/share/pixmaps/firefox.xpm ",
        "f 50 /usr/share/icons/hicolor/16x16/apps/tool.png ",
        "l 0 /usr/share/icons/hicolor/22x22/apps/link.png ../../48x48/apps/firefox.png",
        "l 0 /usr/share/icons/hicolor/22x22/apps/escape.png /etc/shadow",
        "l 0 /usr/share/icons/hicolor/22x22/apps/up.png ../../../../../../etc/shadow",
        "f 9 /usr/share/icons/hicolor/48x48/apps/notes.txt ",
        "f 9 /usr/share/icons/hicolor/48x48/apps/sub/deep.png ",
        "",
    ]
)


def _index() -> IconIndex:
    return IconIndex.from_listing(LISTING)


def _fake() -> tuple[FakePodman, str]:
    fake = FakePodman()
    cid = fake.create(CreateSpec(name="ginn-arch-g1", image_digest_ref=DIGEST, network="host"))
    fake.start(cid)
    return fake, cid


@pytest.mark.fr("FR-044")
def test_scalable_svg_is_preferred_over_sized_icons() -> None:
    assert resolve_icon(_index(), "firefox") == (
        "/usr/share/icons/hicolor/scalable/apps/firefox.svg",
        900,
    )


@pytest.mark.fr("FR-044")
def test_sized_icon_nearest_the_target_is_chosen_when_no_scalable_one_exists() -> None:
    listing = "\n".join(
        [
            "f 1 /usr/share/icons/hicolor/48x48/apps/app.png ",
            "f 1 /usr/share/icons/hicolor/128x128/apps/app.png ",
            "f 1 /usr/share/icons/hicolor/512x512/apps/app.png ",
        ]
    )
    chosen, _size = resolve_icon(IconIndex.from_listing(listing), "app")
    assert chosen == "/usr/share/icons/hicolor/128x128/apps/app.png"


@pytest.mark.fr("FR-044")
def test_the_smallest_icon_larger_than_the_target_is_the_fallback() -> None:
    listing = "\n".join(
        [
            "f 1 /usr/share/icons/hicolor/512x512/apps/app.png ",
            "f 1 /usr/share/icons/hicolor/1024x1024/apps/app.png ",
        ]
    )
    chosen, _size = resolve_icon(IconIndex.from_listing(listing), "app")
    assert chosen == "/usr/share/icons/hicolor/512x512/apps/app.png"


@pytest.mark.fr("FR-044")
def test_pixmaps_is_the_last_resort() -> None:
    chosen, _size = resolve_icon(_index(), "firefox.xpm")
    assert chosen == "/usr/share/pixmaps/firefox.xpm"


@pytest.mark.fr("FR-044")
def test_a_name_with_an_extension_matches_only_that_extension() -> None:
    chosen, _size = resolve_icon(_index(), "firefox.png")
    assert chosen == "/usr/share/icons/hicolor/256x256/apps/firefox.png"


@pytest.mark.fr("FR-044")
def test_a_symbolic_link_inside_the_icon_roots_is_followed_one_level() -> None:
    chosen, _size = resolve_icon(_index(), "link")
    assert chosen == "/usr/share/icons/hicolor/48x48/apps/firefox.png"


@pytest.mark.fr("FR-044")
def test_symbolic_links_that_leave_the_icon_roots_are_not_used() -> None:
    with pytest.raises(IconError, match="no icon named 'escape'"):
        resolve_icon(_index(), "escape")
    with pytest.raises(IconError, match="no icon named 'up'"):
        resolve_icon(_index(), "up")


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize("name", ["notes", "deep", "missing"])
def test_unknown_or_unlisted_names_are_refused(name: str) -> None:
    with pytest.raises(IconError, match="no icon named"):
        resolve_icon(_index(), name)


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize(
    "name",
    [
        "../../etc/shadow",
        "a/b",
        "/etc/passwd",
        "/usr/share/icons/hicolor/../../../../etc/passwd",
        "/usr/share/pixmaps/../../etc/x.png",
        "/usr/share/icons/hicolor/48x48/apps/x.exe",
        "",
        "-bad",
        "has space",
    ],
)
def test_names_and_paths_that_escape_the_icon_roots_are_refused(name: str) -> None:
    with pytest.raises(IconError):
        resolve_icon(_index(), name)


@pytest.mark.fr("FR-044")
def test_an_absolute_path_is_used_only_when_the_listing_has_it_with_its_size() -> None:
    path = "/usr/share/icons/hicolor/16x16/apps/tool.png"
    assert resolve_icon(_index(), path) == (path, 50)


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize(
    "path",
    [
        "/usr/share/pixmaps/unlisted.png",
        "/usr/share/icons/hicolor/22x22/apps/escape.png",
        "/usr/share/icons/hicolor/22x22/apps/up.png",
    ],
)
def test_an_absolute_path_not_in_the_listing_is_refused(path: str) -> None:
    with pytest.raises(IconError, match="not an icon in the realm"):
        resolve_icon(_index(), path)


@pytest.mark.fr("FR-044")
def test_a_failed_listing_leaves_theme_names_unresolved() -> None:
    fake, cid = _fake()
    fake.exec_results[ICON_LIST] = ExecResult(2, "", "find: broken")
    index = IconIndex.build(fake, cid, realm="arch")
    assert index.error is not None
    with pytest.raises(IconError, match="listing failed"):
        resolve_icon(index, "firefox")


@pytest.mark.fr("FR-044")
def test_the_listing_is_one_exec_in_the_realm() -> None:
    fake, cid = _fake()
    fake.exec_results[ICON_LIST] = ExecResult(0, LISTING, "")
    index = IconIndex.build(fake, cid, realm="arch")
    assert index.error is None
    assert fake.ops().count("exec") == 1
    assert resolve_icon(index, "firefox")[0].endswith("firefox.svg")


@pytest.mark.fr("FR-044")
def test_copy_writes_the_checked_icon_into_the_host_folder(tmp_path: Path) -> None:
    fake, cid = _fake()
    fake.files["/usr/share/icons/hicolor/48x48/apps/firefox.png"] = PNG
    dest_dir = tmp_path / "icons" / "arch"
    dest_dir.mkdir(parents=True)
    dest = copy_icon(
        fake,
        cid,
        "/usr/share/icons/hicolor/48x48/apps/firefox.png",
        app="firefox",
        dest_dir=dest_dir,
        size=1200,
    )
    assert dest == dest_dir / "firefox.png"
    assert dest.read_bytes() == PNG
    assert dest.stat().st_mode & 0o777 == 0o644


@pytest.mark.fr("FR-044")
def test_copy_refuses_a_listed_icon_over_the_cap_without_copying(tmp_path: Path) -> None:
    fake, cid = _fake()
    with pytest.raises(IconError, match="larger than"):
        copy_icon(
            fake,
            cid,
            "/usr/share/pixmaps/big.png",
            app="big",
            dest_dir=tmp_path,
            size=ICON_MAX_BYTES + 1,
        )
    assert _cats(fake) == []


@pytest.mark.fr("FR-044")
def test_copy_refuses_an_icon_that_grows_over_the_cap(tmp_path: Path) -> None:
    fake, cid = _fake()
    fake.files["/usr/share/pixmaps/big.png"] = PNG + b"x" * ICON_MAX_BYTES
    with pytest.raises(IconError, match="larger than"):
        copy_icon(
            fake,
            cid,
            "/usr/share/pixmaps/big.png",
            app="big",
            dest_dir=tmp_path,
            size=1,
        )
    assert not (tmp_path / "big.png").exists()


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize(
    ("realm_path", "content"),
    [
        ("/usr/share/pixmaps/x.png", b"not a png at all"),
        ("/usr/share/pixmaps/x.xpm", b"\x89PNG\r\n\x1a\n"),
        ("/usr/share/pixmaps/x.svg", b'<svg><!ENTITY xxe SYSTEM "file:///etc/shadow"></svg>'),
        ("/usr/share/pixmaps/x.svg", b"plain text"),
    ],
)
def test_copy_refuses_content_that_does_not_match_its_extension(
    tmp_path: Path, realm_path: str, content: bytes
) -> None:
    fake, cid = _fake()
    fake.files[realm_path] = content
    with pytest.raises(IconError, match="does not hold"):
        copy_icon(
            fake,
            cid,
            realm_path,
            app="x",
            dest_dir=tmp_path,
            size=len(content),
        )
    assert not any(tmp_path.glob("x.*"))


@pytest.mark.fr("FR-044")
def test_svg_and_xpm_content_that_matches_is_copied(tmp_path: Path) -> None:
    fake, cid = _fake()
    fake.files["/usr/share/icons/hicolor/scalable/apps/a.svg"] = SVG
    fake.files["/usr/share/pixmaps/b.xpm"] = XPM
    svg = copy_icon(
        fake,
        cid,
        "/usr/share/icons/hicolor/scalable/apps/a.svg",
        app="a",
        dest_dir=tmp_path,
        size=len(SVG),
    )
    xpm = copy_icon(
        fake,
        cid,
        "/usr/share/pixmaps/b.xpm",
        app="b",
        dest_dir=tmp_path,
        size=len(XPM),
    )
    assert svg.read_bytes() == SVG
    assert xpm.read_bytes() == XPM


@pytest.mark.fr("FR-044")
def test_a_failed_copy_out_of_the_realm_is_an_icon_error(tmp_path: Path) -> None:
    fake, cid = _fake()
    with pytest.raises(IconError, match="cannot copy"):
        copy_icon(
            fake,
            cid,
            "/usr/share/pixmaps/missing.png",
            app="m",
            dest_dir=tmp_path,
            size=1,
        )


@pytest.mark.fr("FR-044")
def test_icon_roots_and_size_cap_are_the_documented_values() -> None:
    assert icons.ICON_MAX_BYTES == 2 * 1024 * 1024
    assert icons.ICON_TARGET_PX == 256


def _cats(fake: FakePodman) -> list[str]:
    """The paths the fake was asked to `cat`, in order: the copies a refusal must not make."""
    return [
        call.args[1][2]
        for call in fake.calls
        if call.op == "exec" and call.args[1][:2] == ("cat", "--")
    ]


class _Hangs(FakePodman):
    """A fake whose realm listing fails the way a hang or an overflow does: GINN-E-EXPORT-LIST."""

    def exec(self, container_id: str, argv: Sequence[str], **kwargs: object) -> ExecResult:
        if tuple(argv) == ICON_LIST:
            hung = make("GINN-E-INTERNAL")
            hung.details["reason"] = "timed out after 30.0 s"
            raise hung
        return super().exec(container_id, argv, **kwargs)  # type: ignore[arg-type]


@pytest.mark.fr("FR-044")
def test_a_hung_or_overflowing_listing_is_export_list_and_is_bounded() -> None:
    fake = _Hangs()
    cid = fake.create(CreateSpec(name="ginn-arch-g1", image_digest_ref=DIGEST, network="host"))
    fake.start(cid)
    with pytest.raises(GinnError) as err:
        IconIndex.build(fake, cid, realm="arch")
    assert err.value.code == "GINN-E-EXPORT-LIST"


@pytest.mark.fr("FR-044")
def test_the_icon_listing_asks_podman_for_a_timeout_and_an_output_cap() -> None:
    fake, cid = _fake()
    fake.exec_results[ICON_LIST] = ExecResult(0, LISTING, "")
    seen: list[dict[str, object]] = []
    real = fake.exec

    def spy(container_id: str, argv: Sequence[str], **kwargs: object) -> ExecResult:
        seen.append(dict(kwargs))
        return real(container_id, argv, **kwargs)  # type: ignore[arg-type]

    fake.exec = spy  # type: ignore[method-assign]
    IconIndex.build(fake, cid, realm="arch")
    assert seen[0]["timeout_s"] == LISTING_TIMEOUT_S
    assert seen[0]["max_output"] == LISTING_MAX_BYTES


@pytest.mark.fr("FR-044")
def test_a_symbolic_link_whose_target_is_not_a_listed_file_has_no_size_and_is_not_used() -> None:
    listing = "l 0 /usr/share/icons/hicolor/48x48/apps/ghost.png /usr/share/unlisted/ghost.png\n"
    index = IconIndex.from_listing(listing)
    with pytest.raises(IconError, match="no icon named 'ghost'"):
        resolve_icon(index, "ghost")


@pytest.mark.fr("FR-044")
def test_a_symbolic_link_takes_the_listed_size_of_its_target() -> None:
    listing = "\n".join(
        [
            "f 77 /usr/share/icons/hicolor/48x48/apps/real.png ",
            "l 0 /usr/share/icons/hicolor/22x22/apps/alias.png ../../48x48/apps/real.png",
        ]
    )
    assert resolve_icon(IconIndex.from_listing(listing), "alias") == (
        "/usr/share/icons/hicolor/48x48/apps/real.png",
        77,
    )


@pytest.mark.fr("FR-044")
def test_an_oversized_target_behind_a_link_is_refused_before_any_copy(tmp_path: Path) -> None:
    listing = "\n".join(
        [
            f"f {10**10} /usr/share/icons/hicolor/48x48/apps/huge.png ",
            "l 0 /usr/share/icons/hicolor/22x22/apps/alias.png ../../48x48/apps/huge.png",
        ]
    )
    index = IconIndex.from_listing(listing)
    realm_path, size = resolve_icon(index, "alias")
    fake, cid = _fake()
    with pytest.raises(IconError, match="larger than"):
        copy_icon(fake, cid, realm_path, app="a", dest_dir=tmp_path, size=size)
    assert _cats(fake) == []


@pytest.mark.fr("FR-044")
def test_a_directory_at_the_path_is_an_icon_error_and_nothing_is_written(tmp_path: Path) -> None:
    fake, cid = _fake()
    fake.exec_results[("cat", "--", "/usr/share/pixmaps/x.png")] = ExecResult(
        1, "", "cat: /usr/share/pixmaps/x.png: Is a directory\n"
    )
    with pytest.raises(IconError, match="cannot copy"):
        copy_icon(fake, cid, "/usr/share/pixmaps/x.png", app="x", dest_dir=tmp_path, size=1)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.fr("FR-044")
def test_the_copy_is_one_bounded_cat_as_container_root(tmp_path: Path) -> None:
    fake, cid = _fake()
    fake.files["/usr/share/pixmaps/x.png"] = PNG
    seen: list[dict[str, object]] = []
    real = fake.exec

    def spy(container_id: str, argv: Sequence[str], **kwargs: object) -> ExecResult:
        if tuple(argv[:2]) == ("cat", "--"):
            seen.append(dict(kwargs))
        return real(container_id, argv, **kwargs)  # type: ignore[arg-type]

    fake.exec = spy  # type: ignore[method-assign]
    copy_icon(fake, cid, "/usr/share/pixmaps/x.png", app="x", dest_dir=tmp_path, size=len(PNG))
    assert seen == [
        {
            "capture": True,
            "user": "0:0",
            "timeout_s": LISTING_TIMEOUT_S,
            "max_output": ICON_MAX_BYTES + 1,
        }
    ]


@pytest.mark.fr("FR-044")
def test_an_icon_swapped_to_a_huge_file_after_the_listing_is_cut_and_refused(
    tmp_path: Path,
) -> None:
    fake, cid = _fake()
    fake.files["/usr/share/pixmaps/x.png"] = PNG + b"x" * (ICON_MAX_BYTES + 10)
    with pytest.raises(IconError, match="larger than"):
        copy_icon(fake, cid, "/usr/share/pixmaps/x.png", app="x", dest_dir=tmp_path, size=100)
    assert list(tmp_path.iterdir()) == []
