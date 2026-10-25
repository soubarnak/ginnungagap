# SPDX-License-Identifier: Apache-2.0
"""Desktop Entry parsing and the export rewrite (FR-044, FR-046, IR-029). Pure, no host."""

from __future__ import annotations

import pytest

from ginn.export.desktop import (
    REALM_KEY,
    SOURCE_KEY,
    DesktopEntry,
    EntryError,
    escape,
    exec_argv,
    exportable_reason,
    icon_names,
    join_exec,
    render_export,
    split_exec,
    unescape,
)

PROGRAM = "/usr/bin/ginn"
FIREFOX = """\
[Desktop Entry]
Type=Application
Name=Firefox
Name[de]=Firefox-DE
GenericName=Web Browser
Comment=Browse the web
Exec=firefox %u
TryExec=firefox
DBusActivatable=true
Path=/usr/lib/firefox
Icon=firefox
Icon[de]=firefox-de
Categories=Network;WebBrowser;
MimeType=text/html;
X-Custom=kept
Actions=new-window;

[Desktop Action new-window]
Name=New Window
Exec=firefox --new-window %U %d
Icon=firefox
"""


def _render(text: str, realm: str = "arch", icons: dict[str, str] | None = None) -> str:
    entry = DesktopEntry.parse(text)
    paths = (
        {"firefox": "/home/u/.local/share/ginn/icons/arch/firefox.png"} if icons is None else icons
    )
    return render_export(entry, realm=realm, source="firefox", program=PROGRAM, icon_paths=paths)


@pytest.mark.fr("IR-029")
def test_parse_and_serialize_keep_every_line_of_an_lf_file() -> None:
    assert DesktopEntry.parse(FIREFOX).serialize() == FIREFOX


@pytest.mark.fr("IR-029")
def test_first_group_must_be_desktop_entry() -> None:
    with pytest.raises(EntryError, match="Desktop Entry"):
        DesktopEntry.parse("[Desktop Action x]\nExec=x\n[Desktop Entry]\nType=Application\n")


@pytest.mark.fr("IR-029")
@pytest.mark.parametrize(
    "text",
    [
        "[Desktop Entry]\nType=Application\n[Desktop Entry]\nName=x\n",
        "[Desktop Entry]\nno equals sign here\n",
        "[Desktop Entry]\nbad key!=1\n",
        "Name=before any group\n[Desktop Entry]\n",
        "[]\n[Desktop Entry]\n",
    ],
)
def test_malformed_files_are_refused(text: str) -> None:
    with pytest.raises(EntryError):
        DesktopEntry.parse(text)


@pytest.mark.fr("IR-029")
def test_string_escapes_round_trip() -> None:
    for value in ["plain", "a\\sb", "tab\there", "line\nnext", "back\\\\slash", "\\n literal"]:
        assert unescape(escape(value)) == value
    assert unescape("a\\sb\\nc") == "a b\nc"


@pytest.mark.fr("IR-029")
def test_exec_arguments_split_and_join_back_to_the_same_argv() -> None:
    assert split_exec('sh -c "echo \\"hi\\" \\$HOME" %u') == [
        "sh",
        "-c",
        'echo "hi" $HOME',
        "%u",
    ]
    argv = ["a b", 'q"uote', "$HOME", "`id`", "back\\slash", "%u", "%%", "", "it's", "x;y"]
    assert split_exec(join_exec(argv)) == argv


@pytest.mark.fr("IR-029")
def test_unclosed_quote_in_exec_is_refused() -> None:
    with pytest.raises(EntryError, match="not closed"):
        split_exec('firefox "unterminated')


HOSTILE_EXEC = [
    "$(touch /tmp/pwned) %u",
    "`id` %U",
    '"; rm -rf ~ #"',
    'sh -c "echo $HOME `id` \\$(id)"',
    "bash -c 'exit 3' %f",
    "app --name=%c --file=%k %%u",
    '"x\\"y" "\\\\" "\\$HOME"',
]


@pytest.mark.fr("FR-046")
@pytest.mark.parametrize("exec_line", HOSTILE_EXEC)
def test_hostile_exec_lines_reach_the_launcher_as_the_same_argv(exec_line: str) -> None:
    """The rendered Exec, read back by a launcher, gives ginn the original argv and nothing more."""
    original = split_exec(exec_line)
    text = f"[Desktop Entry]\nType=Application\nName=X\nExec={escape(exec_line)}\n"
    rendered = _render(text, icons={})
    rendered_exec = DesktopEntry.parse(rendered).get("Exec")
    assert rendered_exec is not None
    assert split_exec(rendered_exec) == [PROGRAM, "exec", "arch", "--", *original]


@pytest.mark.fr("FR-046")
def test_deprecated_field_codes_are_removed_and_standard_ones_kept() -> None:
    entry = DesktopEntry.parse(
        "[Desktop Entry]\nType=Application\nName=X\n"
        "Exec=app %f %F %u %U %i %c %k %d %D %n %N %v %m\n"
    )
    assert exec_argv(entry, "Desktop Entry") == [
        "app",
        "%f",
        "%F",
        "%u",
        "%U",
        "%i",
        "%c",
        "%k",
    ]


@pytest.mark.fr("FR-044")
def test_render_names_the_entry_with_the_realm_and_wraps_exec() -> None:
    rendered = DesktopEntry.parse(_render(FIREFOX))
    assert rendered.get("Name") == "Firefox (arch)"
    assert rendered.get("Name", locale="de") == "Firefox-DE (arch)"
    assert rendered.get("Exec") == f"{PROGRAM} exec arch -- firefox %u"
    assert rendered.get(REALM_KEY) == "arch"
    assert rendered.get(SOURCE_KEY) == "firefox"


@pytest.mark.fr("FR-044")
def test_two_realms_give_two_distinct_names() -> None:
    arch = DesktopEntry.parse(_render(FIREFOX, realm="arch")).get("Name")
    fedora = DesktopEntry.parse(_render(FIREFOX, realm="fedora")).get("Name")
    assert arch == "Firefox (arch)"
    assert fedora == "Firefox (fedora)"


@pytest.mark.fr("FR-044")
def test_render_drops_try_exec_dbus_activation_and_path() -> None:
    rendered = _render(FIREFOX)
    for key in ("TryExec=", "DBusActivatable=", "Path="):
        assert key not in rendered


@pytest.mark.fr("FR-044")
def test_render_sets_the_absolute_icon_and_drops_localized_icons() -> None:
    rendered = DesktopEntry.parse(_render(FIREFOX))
    assert rendered.get("Icon") == "/home/u/.local/share/ginn/icons/arch/firefox.png"
    assert rendered.get("Icon", locale="de") is None
    assert rendered.get("X-Custom") is None  # an X- key from the realm is never passed on
    assert rendered.get("Categories") == "Network;WebBrowser;"


@pytest.mark.fr("FR-044")
def test_icon_without_a_copy_is_removed() -> None:
    rendered = DesktopEntry.parse(_render(FIREFOX, icons={}))
    assert rendered.get("Icon") is None
    assert "Icon=" not in rendered.serialize().split("[Desktop Action")[0]


@pytest.mark.fr("FR-046")
def test_action_groups_run_through_ginn_and_keep_their_names() -> None:
    action = DesktopEntry.parse(_render(FIREFOX)).group("Desktop Action new-window")
    assert action is not None
    values = {item.key: item.value for item in action.items if item.key}
    assert values["Name"] == "New Window"
    assert split_exec(unescape(values["Exec"])) == [
        PROGRAM,
        "exec",
        "arch",
        "--",
        "firefox",
        "--new-window",
        "%U",
    ]


@pytest.mark.fr("FR-044")
def test_render_is_deterministic() -> None:
    assert _render(FIREFOX) == _render(FIREFOX)


@pytest.mark.fr("FR-044")
def test_only_the_main_icon_is_copied_and_an_action_with_another_icon_loses_it() -> None:
    text = FIREFOX.replace("Icon=firefox\n[Desktop Action", "Icon=firefox\n[Desktop Action")
    text = text.replace(
        "Exec=firefox --new-window %U %d\nIcon=firefox\n", "Exec=firefox --x\nIcon=other\n"
    )
    entry = DesktopEntry.parse(text)
    assert icon_names(entry) == ["firefox"]
    rendered = render_export(
        entry, realm="arch", source="firefox", program=PROGRAM, icon_paths={"firefox": "/x/f.png"}
    )
    action = rendered.split("[Desktop Action new-window]")[1]
    assert "Icon=" not in action


@pytest.mark.fr("FR-046")
def test_a_percent_sign_in_the_ginn_path_is_doubled_for_the_launcher() -> None:
    rendered = render_export(
        DesktopEntry.parse(FIREFOX),
        realm="arch",
        source="firefox",
        program="/opt/50%/ginn",
        icon_paths={},
    )
    assert DesktopEntry.parse(rendered).get("Exec") == "/opt/50%%/ginn exec arch -- firefox %u"


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("[Desktop Entry]\nType=Link\nName=x\nURL=http://e\n", "Type is not Application"),
        ("[Desktop Entry]\nType=Application\nName=x\nExec=x\nNoDisplay=true\n", "hidden"),
        ("[Desktop Entry]\nType=Application\nName=x\nExec=x\nHidden=true\n", "hidden"),
        ("[Desktop Entry]\nType=Application\nExec=x\n", "no Name"),
        ("[Desktop Entry]\nType=Application\nName=x\n", "no Exec"),
        ("[Desktop Entry]\nType=Application\nName=x\nExec=\n", "no Exec"),
        ('[Desktop Entry]\nType=Application\nName=x\nExec="open\n', "not closed"),
    ],
)
def test_entries_that_are_not_applications_are_not_exported(text: str, reason: str) -> None:
    found = exportable_reason(DesktopEntry.parse(text))
    assert found is not None
    assert reason in found


@pytest.mark.fr("FR-044")
def test_an_application_is_exportable() -> None:
    assert exportable_reason(DesktopEntry.parse(FIREFOX)) is None


@pytest.mark.fr("FR-044")
def test_exec_path_with_a_space_is_quoted_in_the_launcher_line() -> None:
    rendered = render_export(
        DesktopEntry.parse(FIREFOX),
        realm="arch",
        source="firefox",
        program="/opt/ginn tools/ginn",
        icon_paths={},
    )
    assert (
        DesktopEntry.parse(rendered).get("Exec") == '"/opt/ginn tools/ginn" exec arch -- firefox %u'
    )


def _render_text(text: str, *, icons: dict[str, str] | None = None) -> str:
    entry = DesktopEntry.parse(text)
    paths = {"firefox": "/x/firefox.png"} if icons is None else icons
    return render_export(entry, realm="arch", source="firefox", program=PROGRAM, icon_paths=paths)


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize(
    "line",
    [
        "Exec[en_US]=/tmp/evil",
        "Type[de]=Link",
        "Hidden[xx]=true",
        "TryExec[xx]=/tmp/evil",
        "Path[xx]=/tmp",
        "NoDisplay[xx]=true",
        "Icon[xx]=/tmp/evil.png",
        "Categories[de]=Network;",
        "MimeType[de]=text/html;",
    ],
)
def test_localized_variants_of_keys_other_than_the_translated_ones_are_dropped(line: str) -> None:
    text = f"[Desktop Entry]\nType=Application\nName=A\nExec=app\n{line}\n"
    rendered = _render_text(text)
    assert line.split("=", 1)[0] not in rendered
    assert "evil" not in rendered


@pytest.mark.fr("FR-044")
def test_translated_keys_keep_their_locale_variants() -> None:
    text = (
        "[Desktop Entry]\nType=Application\nName=A\nName[de]=Aa\nGenericName=G\n"
        "GenericName[de]=Gg\nComment=C\nComment[de]=Cc\nKeywords=k;\nKeywords[de]=kk;\nExec=app\n"
    )
    entry = DesktopEntry.parse(_render_text(text))
    assert entry.get("GenericName", locale="de") == "Gg"
    assert entry.get("Comment", locale="de") == "Cc"
    assert entry.get("Keywords", locale="de") == "kk;"
    assert entry.get("Name", locale="de") == "Aa (arch)"


@pytest.mark.fr("FR-044")
def test_x_keys_from_the_realm_are_dropped_and_ginn_sets_its_own() -> None:
    text = (
        "[Desktop Entry]\nType=Application\nName=A\nExec=app\n"
        "X-KDE-SubstituteUID=true\nX-KDE-Username=root\nX-Ginn-Realm=other\n"
        "X-Ginn-Source=evil\nX-GNOME-Foo=1\n"
    )
    rendered = _render_text(text)
    assert "X-KDE" not in rendered and "X-GNOME" not in rendered
    entry = DesktopEntry.parse(rendered)
    assert entry.get(REALM_KEY) == "arch"
    assert entry.get(SOURCE_KEY) == "firefox"
    assert rendered.count("X-Ginn-Realm=") == 1


@pytest.mark.fr("FR-044")
def test_only_keys_on_the_spec_list_survive_and_mime_type_is_kept() -> None:
    text = (
        "[Desktop Entry]\nType=Application\nName=A\nExec=app\nVersion=1.5\n"
        "StartupNotify=true\nStartupWMClass=app\nTerminal=false\nOnlyShowIn=GNOME;\n"
        "MimeType=text/plain;\nPrefersNonDefaultGPU=true\nSingleMainWindow=true\n"
    )
    rendered = _render_text(text)
    for kept in ("Version=1.5", "StartupNotify=true", "StartupWMClass=app", "Terminal=false"):
        assert kept in rendered
    assert "OnlyShowIn=GNOME;" in rendered
    assert "MimeType=text/plain;" in rendered
    assert "PrefersNonDefaultGPU" not in rendered and "SingleMainWindow" not in rendered


@pytest.mark.fr("FR-044")
def test_only_the_action_groups_named_in_actions_are_kept_by_exact_name() -> None:
    text = (
        "[Desktop Entry]\nType=Application\nName=A\nExec=app\nActions=kept;\n\n"
        "[Desktop Action kept]\nName=K\nExec=app --k\n\n"
        "[Desktop Action unlisted]\nName=U\nExec=app --u\n\n"
        "[ Desktop Action kept]\nName=Padded\nExec=app --p\n\n"
        "[Desktop Action kept2]\nName=Two\nExec=app --t\n"
    )
    rendered = _render_text(text)
    assert "[Desktop Action kept]" in rendered
    assert "[Desktop Action unlisted]" not in rendered
    assert "[ Desktop Action kept]" not in rendered
    assert "[Desktop Action kept2]" not in rendered
    assert "app --u" not in rendered and "app --p" not in rendered


@pytest.mark.fr("FR-044")
def test_a_repeated_key_with_the_same_locale_refuses_the_file() -> None:
    with pytest.raises(EntryError, match="Exec appears twice"):
        DesktopEntry.parse("[Desktop Entry]\nType=Application\nExec=a\nExec=b\n")
    with pytest.raises(EntryError, match=r"Name\[de\] appears twice"):
        DesktopEntry.parse("[Desktop Entry]\nName[de]=a\nName[de]=b\n")
    with pytest.raises(EntryError, match="appears twice"):
        DesktopEntry.parse(
            "[Desktop Entry]\nType=Application\n[Desktop Action x]\nExec=a\nExec=b\n"
        )


@pytest.mark.fr("FR-044")
def test_the_same_key_in_different_locales_is_not_a_repeat() -> None:
    entry = DesktopEntry.parse("[Desktop Entry]\nName=a\nName[de]=b\nName[fr]=c\n")
    assert entry.get("Name", locale="fr") == "c"


@pytest.mark.fr("FR-046")
def test_a_realm_exec_cannot_add_a_key_to_the_host_entry() -> None:
    text = "[Desktop Entry]\nType=Application\nName=A\nExec=app\nTryExec=/bin/true\n"
    rendered = _render_text(text)
    assert "TryExec" not in rendered
