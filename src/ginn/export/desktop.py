# SPDX-License-Identifier: Apache-2.0
"""Desktop Entry parsing and the export rewrite of one entry (FR-044, FR-046, IR-029).

The parser follows the freedesktop Desktop Entry Specification. A file is groups in square
brackets, each with `Key=Value` lines, `Key[locale]` keys and `#` comments. A value is a string
with the escapes `\\s \\n \\t \\r \\\\`. The first group must be `[Desktop Entry]`. A line that is
not a comment, a blank, a group or a key makes the file invalid, and so does a group name used
twice.

`Exec` is a second layer. Its string escapes are undone first, then its arguments are split at
blank characters. Double quotes group an argument, and inside them `"`, a backtick, `$` and `\\`
are escaped with a backslash. The field codes `%f %F %u %U %i %c %k` stay as arguments of
their own, so the launcher expands them. The deprecated codes `%d %D %n %N %v %m` are removed, as
the spec asks. `%%` is not touched. `join_exec` writes the arguments back with the same rules, so a
hostile argument (quotes, `$`, backticks, `%`) reaches the realm as the same argv and is never a
shell string.

`render_export` builds the host menu entry from an allowlist, never from the realm's file as
written. The realm is untrusted, and a desktop environment acts on some keys (a switch can run
an entry as another user), so only the keys the menu needs survive: the spec's list in MAIN_KEYS,
and in each action group ACTION_KEYS. A localized variant survives only for the keys that have
translations (LOCALIZED_KEYS). `X-` keys are dropped, except the two ginn sets. `MimeType` is kept:
it is data that the menu uses to offer the app, and it runs nothing. Only the action groups named
in `Actions=` are kept, matched by exact group name. Comments and blank lines are dropped.

The rewrite:
- `Exec` in the entry and in each kept action group runs `<program> exec <realm> -- <argv>`. The
  launcher starts the realm command through ginn (FR-049). Deprecated field codes are removed.
- `Name` and each `Name[locale]` of the entry gain ` (<realm>)`. Action names are left alone.
- `Icon` is the absolute host path the caller copied the icon to. An `Icon` with no copy is
  removed. A localized `Icon[...]` is removed.
- `TryExec`, `DBusActivatable` and `Path` are not in the allowlist. The host cannot run or activate
  a realm binary, and `Path` names a working directory inside the realm. The roadmap line lists
  only the first two; `Path` is removed as a deviation.
- `X-Ginn-Realm` and `X-Ginn-Source` are set. They mark the entry as one ginn owns and name its
  realm and source desktop ID. sync reads them back, because a file name cannot carry them.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

MAIN_GROUP: Final = "Desktop Entry"
ACTION_PREFIX: Final = "Desktop Action "
REALM_KEY: Final = "X-Ginn-Realm"
SOURCE_KEY: Final = "X-Ginn-Source"
FIELD_CODES: Final = frozenset("%f %F %u %U %i %c %k".split())
DEPRECATED_CODES: Final = frozenset("%d %D %n %N %v %m".split())
MAIN_KEYS: Final = frozenset(
    "Type Version Name GenericName NoDisplay Comment Icon Hidden OnlyShowIn NotShowIn "
    "Terminal Categories Keywords StartupNotify StartupWMClass MimeType Actions Exec".split()
)
"""The keys of the main group that the host entry may carry: the spec's list, minus `TryExec`,
`DBusActivatable` and `Path`, which the host cannot use for a realm binary (see render_export).
Any other key, including every `X-` key and every extension key, is dropped. A menu reads its
keys with their meaning, so a key that a desktop environment acts on (for example a switch that
runs an entry as another user) must never come from the realm."""
ACTION_KEYS: Final = frozenset("Name Icon Exec".split())
"""The keys of a `[Desktop Action ...]` group that the host entry may carry."""
LOCALIZED_KEYS: Final = frozenset("Name GenericName Comment Keywords".split())
"""The keys that may carry `[locale]` variants: the menu shows these translated. Every other key
keeps only its unlocalized line, so a localized `Exec[xx]` or `Type[xx]` is never passed on."""
_EXEC_RESERVED: Final = frozenset(" \t\n\"'\\<>~|&;$*?#()`")
_QUOTED_ESCAPES: Final = frozenset('"`$\\')
_QUOTED_CHAR: Final = re.compile(r'["`$\\]')
_STRING_ESCAPES: Final = dict(zip("sntr\\", " \n\t\r\\", strict=True))
_STRING_ESCAPE: Final = re.compile(r"\\([sntr\\])")
_ESCAPE_TABLE: Final = str.maketrans({"\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"})
_KEY: Final = re.compile(r"([A-Za-z0-9-]+)(?:\[([^\[\]]+)\])?")


class EntryError(ValueError):
    """The text is not a Desktop Entry this module can read or export. The message says why."""


@dataclass
class _Item:
    """One line of a group. `key` is set for a `Key=Value` line; other lines keep only `text`."""

    text: str
    key: str | None = None
    locale: str | None = None
    value: str = ""
    """The raw value, string escapes still in place."""


@dataclass
class _Group:
    name: str
    items: list[_Item] = field(default_factory=list)


@dataclass
class DesktopEntry:
    """A parsed Desktop Entry. Build it with `parse`; `serialize` writes it back."""

    preamble: list[str]
    groups: list[_Group]

    @classmethod
    def parse(cls, text: str) -> DesktopEntry:
        """Parse `text`. Raise EntryError for a file the spec does not allow."""
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        preamble: list[str] = []
        groups: list[_Group] = []
        for raw in lines:
            line = raw.rstrip("\r")
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                name = stripped[1:-1]
                if not name or "[" in name or "]" in name:
                    raise EntryError(f"group header {stripped!r} is not valid")
                if any(group.name == name for group in groups):
                    raise EntryError(f"group [{name}] appears twice")
                groups.append(_Group(name))
                continue
            if not groups:
                if stripped and not stripped.startswith("#"):
                    raise EntryError("a line comes before the first group")
                preamble.append(line)
                continue
            if not stripped or stripped.startswith("#"):
                groups[-1].items.append(_Item(text=line))
                continue
            groups[-1].items.append(_key_line(line))
        if not groups or groups[0].name != MAIN_GROUP:
            raise EntryError(f"the first group must be [{MAIN_GROUP}]")
        for group in groups:
            _reject_repeats(group)
        return cls(preamble=preamble, groups=groups)

    def group(self, name: str) -> _Group | None:
        return next((group for group in self.groups if group.name == name), None)

    def get(self, key: str, *, locale: str | None = None) -> str | None:
        """The unescaped value of `key[locale]` in the main group, or None when it is absent."""
        group = self.group(MAIN_GROUP)
        if group is None:
            return None
        for item in group.items:
            if item.key == key and item.locale == locale:
                return unescape(item.value)
        return None

    def serialize(self) -> str:
        out = list(self.preamble)
        for group in self.groups:
            out.append(f"[{group.name}]")
            out.extend(item.text for item in group.items)
        return "\n".join(out) + "\n"


def _reject_repeats(group: _Group) -> None:
    """Refuse a key that appears twice with the same locale in one group.

    Readers disagree on which copy counts (GKeyFile takes the last, a first-match reader takes the
    first), so a file with a repeat has no single meaning. It is refused whole.
    """
    seen: set[tuple[str, str | None]] = set()
    for item in group.items:
        if item.key is None:
            continue
        pair = (item.key, item.locale)
        if pair in seen:
            name = item.key if item.locale is None else f"{item.key}[{item.locale}]"
            raise EntryError(f"key {name} appears twice in group [{group.name}]")
        seen.add(pair)


def _key_line(line: str) -> _Item:
    key_part, sep, value = line.partition("=")
    if not sep:
        raise EntryError(f"line {line.strip()!r} is neither a key nor a group")
    match = _KEY.fullmatch(key_part.strip())
    if match is None:
        raise EntryError(f"key {key_part.strip()!r} is not valid")
    return _Item(text=line, key=match.group(1), locale=match.group(2), value=value.strip(" \t"))


def unescape(raw: str) -> str:
    """Undo the string escapes of a Desktop Entry value. An unknown escape stays as written."""
    return _STRING_ESCAPE.sub(lambda match: _STRING_ESCAPES[match.group(1)], raw)


def escape(text: str) -> str:
    """The string escapes for `text`. `unescape(escape(x)) == x` for every `x`."""
    return text.translate(_ESCAPE_TABLE)


def split_exec(value: str) -> list[str]:
    """The arguments of an unescaped `Exec` value (see the module docstring).

    Raise EntryError for an unclosed double quote. Deprecated field codes are not removed here.
    """
    args: list[str] = []
    current: list[str] = []
    have = False
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch in " \t":
            if have:
                args.append("".join(current))
                current = []
                have = False
            i += 1
            continue
        have = True
        if ch != '"':
            current.append(ch)
            i += 1
            continue
        i += 1
        while True:
            if i >= n:
                raise EntryError("Exec has a double quote that is not closed")
            ch = value[i]
            if ch == '"':
                i += 1
                break
            if ch == "\\" and i + 1 < n and value[i + 1] in _QUOTED_ESCAPES:
                current.append(value[i + 1])
                i += 2
                continue
            current.append(ch)
            i += 1
    if have:
        args.append("".join(current))
    return args


def join_exec(argv: list[str] | tuple[str, ...]) -> str:
    """The unescaped `Exec` value for `argv`. The inverse of `split_exec` for the same argv."""
    return " ".join(_exec_arg(arg) for arg in argv)


def _exec_arg(arg: str) -> str:
    if arg in FIELD_CODES:
        return arg
    if arg and not _EXEC_RESERVED.intersection(arg):
        return arg
    return '"' + _QUOTED_CHAR.sub(r"\\\g<0>", arg) + '"'


def exec_argv(entry: DesktopEntry, group_name: str) -> list[str] | None:
    """The argv of the `Exec` key of `group_name`, with deprecated codes removed, or None."""
    group = entry.group(group_name)
    if group is None:
        return None
    for item in group.items:
        if item.key == "Exec" and item.locale is None:
            args = split_exec(unescape(item.value))
            return [arg for arg in args if arg not in DEPRECATED_CODES]
    return None


def icon_names(entry: DesktopEntry) -> list[str]:
    """The plain `Icon` value of the main group, as a list of one name, or empty.

    Only the main icon is copied. An action icon with the same name uses that copy, and any other
    action icon is dropped by `render_export`. Two copies of different names would share one host
    file name (`<app><ext>`), so the second would overwrite the first.
    """
    name = entry.get("Icon")
    return [name] if name else []


def exportable_reason(entry: DesktopEntry) -> str | None:
    """Why this entry is not exported, or None when it is. Only applications are exported.

    An entry with `NoDisplay=true` or `Hidden=true` is left out too: the menu does not show it.
    """
    if entry.get("Type") != "Application":
        return "Type is not Application"
    if entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true":
        return "the entry is hidden from menus"
    if not entry.get("Name"):
        return "the entry has no Name"
    try:
        argv = exec_argv(entry, MAIN_GROUP)
    except EntryError as exc:
        return str(exc)
    if not argv:
        return "the entry has no Exec command"
    return None


def render_export(
    entry: DesktopEntry,
    *,
    realm: str,
    source: str,
    program: str,
    icon_paths: Mapping[str, str],
) -> str:
    """The host menu entry for `entry`, as text (see the module docstring).

    The host entry is built from an allowlist, not from the realm's file: only the keys in
    MAIN_KEYS and ACTION_KEYS survive, localized variants only for LOCALIZED_KEYS, and only the
    action groups that `Actions=` names, by exact group name. Comments and blank lines go too.
    `icon_paths` maps each plain `Icon` value to the absolute host path of its copy. A name that is
    not in it loses its `Icon`. `program` is the ginn command the launcher starts. An action whose
    `Exec` cannot be read loses that `Exec`, so the host does not run it.
    """
    out = copy.deepcopy(entry)
    main = out.group(MAIN_GROUP)
    if main is None:  # parse() guarantees the main group
        raise EntryError(f"no [{MAIN_GROUP}] group")
    wanted = {f"{ACTION_PREFIX}{action}" for action in _action_ids(main)}
    out.groups = [g for g in out.groups if g.name == MAIN_GROUP or g.name in wanted]
    for group in out.groups:
        if group.name == MAIN_GROUP:
            _keep(group, MAIN_KEYS, LOCALIZED_KEYS)
            _rename(group, realm)
        else:
            _keep(group, ACTION_KEYS, frozenset({"Name"}))
        _rewrite_exec(group, realm, program)
        _rewrite_icon(group, icon_paths)
    _replace(main, REALM_KEY, realm)
    _replace(main, SOURCE_KEY, source)
    return out.serialize()


def _action_ids(main: _Group) -> list[str]:
    """The identifiers in the main group's `Actions=` list, empty entries skipped."""
    values = [item.value for item in main.items if item.key == "Actions" and item.locale is None]
    if not values:
        return []
    return [name for name in unescape(values[0]).split(";") if name]


def _keep(group: _Group, allowed: frozenset[str], localized: frozenset[str]) -> None:
    """Keep only the key lines the host entry may carry (see MAIN_KEYS, LOCALIZED_KEYS)."""
    kept: list[_Item] = []
    for item in group.items:
        if item.key not in allowed:
            continue
        if item.locale is not None and item.key not in localized:
            continue
        kept.append(item)
    group.items = kept


def _item(key: str, locale: str | None, value: str) -> _Item:
    """A new `Key[locale]=value` line, with `value` (unescaped) escaped for the file."""
    head = f"{key}[{locale}]" if locale else key
    raw = escape(value)
    return _Item(text=f"{head}={raw}", key=key, locale=locale, value=raw)


def _replace(group: _Group, key: str, value: str) -> None:
    """Set the plain `key` of `group`: the first line is replaced, later copies are removed."""
    fresh = _item(key, None, value)
    plain = [i for i, item in enumerate(group.items) if item.key == key and item.locale is None]
    if not plain:
        # After the last key line, so a new key does not follow the group's trailing blank lines.
        last = max((i for i, item in enumerate(group.items) if item.key is not None), default=-1)
        group.items.insert(last + 1, fresh)
        return
    first, *later = plain
    group.items[first] = fresh
    group.items = [item for i, item in enumerate(group.items) if i not in later]


def _rename(group: _Group, realm: str) -> None:
    renamed: list[_Item] = []
    for item in group.items:
        if item.key != "Name":
            renamed.append(item)
            continue
        renamed.append(_item("Name", item.locale, f"{unescape(item.value)} ({realm})"))
    group.items = renamed


def _rewrite_exec(group: _Group, realm: str, program: str) -> None:
    """Wrap each plain `Exec` of the group in the launcher. An unreadable one is removed."""
    items: list[_Item] = []
    for item in group.items:
        if item.key != "Exec" or item.locale is not None:
            items.append(item)
            continue
        try:
            argv = split_exec(unescape(item.value))
        except EntryError:
            continue
        argv = [arg for arg in argv if arg not in DEPRECATED_CODES]
        # A percent sign in the ginn path is doubled, so a launcher reads it as a literal percent.
        launcher = program.replace("%", "%%")
        items.append(_item("Exec", None, join_exec([launcher, "exec", realm, "--", *argv])))
    group.items = items


def _rewrite_icon(group: _Group, icon_paths: Mapping[str, str]) -> None:
    """Keep one plain `Icon` at the place of the first `Icon` line, and drop localized ones."""
    slots = [i for i, item in enumerate(group.items) if item.key == "Icon"]
    if not slots:
        return
    plain = [unescape(group.items[i].value) for i in slots if group.items[i].locale is None]
    name = plain[0] if plain else ""
    group.items = [item for item in group.items if item.key != "Icon"]
    path = icon_paths.get(name) if name else None
    if path is not None:
        group.items.insert(min(slots[0], len(group.items)), _item("Icon", None, path))
