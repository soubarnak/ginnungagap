# SPDX-License-Identifier: Apache-2.0
"""`ginn _sync <realm>`: the host menu follows the realm's desktop entries (FR-044, FR-045).

The realm's applications are the `*.desktop` files of /usr/share/applications. One `find` in the
realm lists them with their size and mtime. The host side is `$XDG_DATA_HOME/applications`, where
an exported entry is `ginn-<realm>-<app>.desktop` and carries `X-Ginn-Realm` and `X-Ginn-Source`.
Those keys, not the file name, say which realm owns an entry, because a realm name may contain a
hyphen. The icon copies are `$XDG_DATA_HOME/ginn/icons/<realm>/<app>.<ext>`.

`sync_realm` runs the sync as the mutating operation `sync`, under the realm lock (FR-123). A caller
that already holds the lock inside its own `mutating` calls `sync_in_lock` instead, so nothing
takes the lock twice (GINN-E-REALM-BUSY).

The steps of the body, in order:

1. The realm must run, as podman lists it. A realm that does not run is left as it is, and the
   result says so. A package transaction runs inside a running realm, so nothing is missed.
2. One `find` lists the desktop files. A failed listing raises GINN-E-EXPORT-LIST before any change,
   so a failed listing never removes an entry.
3. The state file `$XDG_STATE_HOME/ginn/exports/<realm>.json` records, for each app, a stamp (a
   hash of size, mtime and the ginn command) and whether the app was exported. An app whose stamp
   is unchanged is not copied again. An exported app whose host entry or icon is missing is copied
   again.
4. Each app that needs work is copied out, parsed, checked and rendered (desktop.py, icons.py).
   The outcomes: exported (the entry is written when its bytes differ), skipped (not an exportable
   application, or unreadable as a Desktop Entry; its host entry is removed), or failed (the copy
   failed; the host entry and the state are left as they are, so the next sync retries it).
5. A host entry of this realm whose app is no longer listed is removed, with its icon copy.
6. The state is saved.

Idempotence. Two runs with no change in the realm do no copy and no write: the second run is one
`find` and one state read. A test checks it (tests/unit/test_export_sync.py).

Timing (NFR-009). A transaction adds or changes a few entries, so it costs one listing, one copy of
each changed entry and one icon listing when an icon is needed. The 5 s host proof is not run here;
it waits for gate 3.

Entries of other realms, and host files without this realm's ginn markers, are never written or
removed. A file whose name is taken and that is not an entry of this realm is not overwritten
(GINN-W-EXPORT-SKIPPED).
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ginn.domain import RealmName, RealmState
from ginn.errors import Warning as GinnWarning
from ginn.errors import make, warn
from ginn.export._files import write_file_atomic
from ginn.export._realm import RealmFileTooLarge, RealmFileUnreadable, read_realm_file, run_listing
from ginn.export.bins import remove_realm_launchers
from ginn.export.desktop import (
    REALM_KEY,
    SOURCE_KEY,
    DesktopEntry,
    EntryError,
    exportable_reason,
    icon_names,
    render_export,
)
from ginn.export.icons import IconError, IconIndex, copy_icon, resolve_icon
from ginn.export.watch import WatchHooks, rearm, stamp_snapshot, write_sync_mark
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.podman.adapter import ExecTarget, Podman
from ginn.podman.models import ContainerState
from ginn.runtime.spec import LABEL_REALM
from ginn.store.paths import OwnedPaths, write_bytes_atomic
from ginn.store.records import RealmRecord, RealmStore

DESKTOP_DIR: Final = "/usr/share/applications"
DESKTOP_LIST: Final = (
    "find",
    "/usr/share",
    "-mindepth",
    "2",
    "-maxdepth",
    "2",
    "-path",
    f"{DESKTOP_DIR}/*.desktop",
    "-type",
    "f",
    "-printf",
    "%s %T@ %p\\n",
)
"""The argv of the listing a sync runs in the realm: size, mtime and path of each desktop file.

It searches /usr/share, not the applications directory, so that a realm whose last GUI package is
gone (no applications directory) gives an empty listing, and its exports are removed. A symbolic
link is not listed: the copy out of the realm would copy the link itself."""
DESKTOP_MAX_BYTES: Final = 512 * 1024
"""The largest desktop file read from the realm, in bytes (realm input is untrusted)."""
_APP: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HALTED: Final = frozenset({ContainerState.CREATED, ContainerState.EXITED})
_STATE_VERSION: Final = 1


@dataclass(frozen=True)
class SyncHooks:
    """The seams of sync. `program` is the ginn command that an exported launcher runs.

    `watch` is the export watcher's seams (ADR-016). `sync_realm`, the `ginn _sync` command, keeps
    the watcher on the realm's stamp directory with it (`rearm`); None leaves the watcher alone.
    """

    program: str = "ginn"
    watch: WatchHooks | None = None


@dataclass
class SyncResult:
    """What one sync did. `running` is False when the realm did not run and nothing was read."""

    realm: str
    running: bool = True
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[GinnWarning] = field(default_factory=list)


@dataclass(frozen=True)
class _Seen:
    """What the state file records for one app: the stamp of its listing and whether it exported."""

    stamp: str
    exported: bool


@dataclass(frozen=True)
class _Host:
    """An exported entry of this realm on the host: its path, its bytes and its icon copy."""

    app: str
    path: Path
    text: str
    icon: str | None


@dataclass(frozen=True)
class _Skip:
    reason: str


@dataclass(frozen=True)
class _Fail:
    reason: str


@dataclass(frozen=True)
class _Built:
    text: str
    icons: tuple[str, ...]


def sync_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    hooks: SyncHooks | None = None,
) -> SyncResult:
    """Sync the host menu with realm `name` under the realm lock (mutating operation `sync`)."""
    with mutating(env, "sync", name) as mutation:
        try:
            result = sync_in_lock(env, podman, name, mutation=mutation, hooks=hooks)
        finally:
            # Also when the body raises: the watcher that ran this sync is about to exit (ADR-016).
            # Under the lock, so two syncs never re-arm at once. rearm raises nothing, so an
            # exception of the body is the one the caller sees.
            if hooks is not None and hooks.watch is not None:
                notice = rearm(env.paths, name, hooks=hooks.watch)
                if notice is not None:
                    mutation.warnings.append(notice)
    result.warnings.extend(mutation.warnings)
    return result


def sync_in_lock(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    mutation: Mutation,
    hooks: SyncHooks | None = None,
) -> SyncResult:
    """The sync body, for a caller that holds the realm lock inside its own `mutating` call.

    An OS error on the host is a warning, never an exception here: a sync that stops at one is
    GINN-W-EXPORT-SYNC, and one entry whose write fails is GINN-W-EXPORT-SKIPPED. So the change
    that called the sync stands. A journal write is not a host write, and its OS error propagates.
    """
    hooks = SyncHooks() if hooks is None else hooks
    # The stamp as it is before the listing: a touch during the sync changes it (ADR-016).
    snapshot = stamp_snapshot(env.paths, name)
    record = _load_record(env, name)
    result = SyncResult(name.value)
    if record.state is not RealmState.RUNNING or not _runs(podman, record, name):
        result.running = False
        _audit_counts(mutation, noop="realm not running")
        mutation.step("noop")
        return result
    try:
        env.paths.ensure()
        listing = _list_desktop(podman, record, name.value)
        sync = _Sync(env.paths, podman, record, name.value, hooks.program, result)
    except OSError as exc:
        result.warnings.append(_not_synced(name.value, exc))
        return result
    mutation.step("listed")
    sync.run(listing)
    if not sync.failed and not any(n.code == "GINN-W-EXPORT-SYNC" for n in result.warnings):
        try:
            # Only a sync that ran through. Best effort: no mark means one more watcher sync.
            write_sync_mark(env.paths, name, snapshot)
        except OSError:
            pass
    _audit_counts(
        mutation,
        added=len(result.added),
        updated=len(result.updated),
        removed=len(result.removed),
        skipped=len(result.skipped),
    )
    mutation.step("applied")
    return result


def _audit_counts(mutation: Mutation, **values: object) -> None:
    """Record the counts of a sync in the audit entry (FR-124). A sync's own entry takes them at the
    top level. Inside another operation they nest under `sync`, so the entry of a package change
    does not read as a sync of its own."""
    if mutation.op == "sync":
        mutation.add_params(**values)
    else:
        mutation.add_params(sync=values)


def _not_synced(realm: str, exc: OSError) -> GinnWarning:
    """The warning for a sync that stopped at an OS error on the host (FR-044)."""
    return warn("GINN-W-EXPORT-SYNC", realm=realm, reason=exc.strerror or str(exc))


def unexport_all(paths: OwnedPaths, name: RealmName) -> list[str]:
    """Remove every exported entry of realm `name` (DR-008): the menu entries, the icon copies, the
    CLI launchers, and the state file. Return the apps whose menu entries were removed.

    Nothing reads the realm, so a realm that does not run can be cleaned. ops/delete.py calls it
    (FR-019 AC-1). The caller holds the realm lock.
    """
    removed: list[str] = []
    for host in _scan_host(paths, name.value).values():
        _remove_host(paths, name.value, host)
        removed.append(host.app)
    remove_realm_launchers(paths, name)
    state = _state_file(paths, name.value)
    if state.is_file() and not state.is_symlink():
        state.unlink()
    icon_dir = paths.icons / name.value
    if icon_dir.is_dir() and not icon_dir.is_symlink():
        shutil.rmtree(icon_dir)
    return removed


def _load_record(env: OpEnv, name: RealmName) -> RealmRecord:
    try:
        return RealmStore(env.paths, env.clock).load(name)
    except FileNotFoundError:
        raise make("GINN-E-NO-SUCH-REALM", realm=name.value) from None


def _runs(podman: Podman, record: RealmRecord, name: RealmName) -> bool:
    """True when podman lists the realm's container as running (one `ps`, no exec)."""
    cid = record.container_id
    if cid is None:
        return False
    return any(
        info.id == cid and info.state not in _HALTED
        for info in podman.ps({LABEL_REALM: name.value})
    )


def _read_target(record: RealmRecord, realm: str) -> ExecTarget:
    """The exec target of a read of the realm, by its services mode at create (ADR-014).

    The reads are never mutating. A realm created services-on reads through the relay, so the
    listing and the file reads work from the user session (F2).
    """
    return ExecTarget(realm=realm, services=bool(record.services_at_create), mutating=False)


def _list_desktop(podman: Podman, record: RealmRecord, realm: str) -> dict[str, tuple[int, str]]:
    """The desktop files of the realm: app ID to (size, mtime). A failure raises and changes
    nothing."""
    cid = record.container_id
    if cid is None:
        raise make("GINN-E-EXPORT-LIST", realm=realm, reason="the realm has no container")
    result = run_listing(podman, cid, DESKTOP_LIST, realm=realm, target=_read_target(record, realm))
    if result.exit_code != 0:
        reason = f"find exited with status {result.exit_code}"
        raise make("GINN-E-EXPORT-LIST", realm=realm, reason=reason)
    apps: dict[str, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        size_text, mtime, path = parts
        name = posixpath.basename(path)
        app = name[: -len(".desktop")] if name.endswith(".desktop") else ""
        if posixpath.dirname(path) != DESKTOP_DIR or _APP.fullmatch(app) is None:
            continue
        size = int(size_text) if size_text.isdigit() else 0
        apps[app] = (size, mtime)
    return apps


def _stamp(size: int, mtime: str, program: str) -> str:
    """The stamp of one listed desktop file. The ginn command is in it, so a new path re-renders."""
    return hashlib.sha256(f"{size} {mtime} {program}".encode()).hexdigest()[:32]


def _state_file(paths: OwnedPaths, realm: str) -> Path:
    return paths.state / "exports" / f"{realm}.json"


def _load_state(paths: OwnedPaths, realm: str) -> dict[str, _Seen]:
    """The state file as a dict. A missing or unreadable file is an empty state, which is safe:
    every app is then checked again, and the host entries are compared, not trusted."""
    try:
        data = json.loads(_state_file(paths, realm).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, dict):
        return {}
    seen: dict[str, _Seen] = {}
    for app, value in apps.items():
        if not isinstance(value, dict):
            continue
        stamp, exported = value.get("stamp"), value.get("exported")
        if isinstance(app, str) and isinstance(stamp, str) and isinstance(exported, bool):
            seen[app] = _Seen(stamp, exported)
    return seen


def _save_state(paths: OwnedPaths, realm: str, seen: Mapping[str, _Seen]) -> None:
    body = {app: {"stamp": item.stamp, "exported": item.exported} for app, item in seen.items()}
    path = _state_file(paths, realm)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps({"version": _STATE_VERSION, "apps": body}, indent=1, sort_keys=True)
    write_bytes_atomic(path, data.encode("utf-8"))


def _read_text_capped(path: Path, cap: int) -> str:
    """The UTF-8 text of a regular file of at most `cap` bytes. Raise OSError or ValueError."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if info.st_size > cap:
        raise ValueError(f"larger than {cap} bytes")
    return path.read_bytes().decode("utf-8")


def _scan_host(paths: OwnedPaths, realm: str) -> dict[str, _Host]:
    """The exported entries of `realm` on the host, by app ID. Other files are not read for it."""
    hosts: dict[str, _Host] = {}
    for path in paths.owned_desktop_entries():
        host = _read_host(path, realm)
        if host is not None:
            hosts[host.app] = host
    return hosts


def _read_host(path: Path, realm: str) -> _Host | None:
    try:
        text = _read_text_capped(path, DESKTOP_MAX_BYTES)
        entry = DesktopEntry.parse(text)
    except (OSError, ValueError):
        return None
    if entry.get(REALM_KEY) != realm:
        return None
    app = entry.get(SOURCE_KEY)
    if app is None or _APP.fullmatch(app) is None or path.name != f"ginn-{realm}-{app}.desktop":
        return None
    icon = entry.get("Icon")
    return _Host(app=app, path=path, text=text, icon=icon)


def _remove_host(paths: OwnedPaths, realm: str, host: _Host) -> None:
    """Remove an exported entry and its icon copy. Only a copy in the realm's icon folder goes."""
    host.path.unlink(missing_ok=True)
    if host.icon is not None and posixpath.dirname(host.icon) == str(paths.icons / realm):
        Path(host.icon).unlink(missing_ok=True)


class _Sync:
    """One run of the sync body. It holds what the steps share, so each step takes few arguments."""

    def __init__(
        self,
        paths: OwnedPaths,
        podman: Podman,
        record: RealmRecord,
        realm: str,
        program: str,
        result: SyncResult,
    ) -> None:
        self._paths = paths
        self._podman = podman
        self._cid = record.container_id or ""
        self._realm = realm
        self._target = _read_target(record, realm)
        self._program = program
        self._result = result
        self._applications = paths.applications_dir
        self._icon_dir = paths.icons / realm
        self._index: IconIndex | None = None
        self._hosts = _scan_host(paths, realm)
        self._seen = _load_state(paths, realm)
        self._loaded = dict(self._seen)
        self.failed = False
        """True once an entry could not be copied out; the next sync must retry it (ADR-016)."""

    def run(self, listing: Mapping[str, tuple[int, str]]) -> None:
        for app in sorted(listing):
            size, mtime = listing[app]
            stamp = _stamp(size, mtime, self._program)
            host = self._hosts.get(app)
            seen = self._seen.get(app)
            if seen is not None and seen.stamp == stamp and (not seen.exported or _intact(host)):
                (self._result.unchanged if seen.exported else self._result.skipped).append(app)
                continue
            self._guarded(app, self._process, app, stamp, host, size)
        for app in sorted(set(self._hosts) - set(listing)):
            self._guarded(app, self._drop, app)
        for app in sorted(set(self._seen) - set(listing)):
            del self._seen[app]
        if self._seen != self._loaded:
            try:
                _save_state(self._paths, self._realm, self._seen)
            except OSError as exc:
                # The entries are written already. The next sync compares them, so none is lost.
                self._result.warnings.append(_not_synced(self._realm, exc))

    def _drop(self, app: str) -> None:
        """Remove the host entry of an app that the realm no longer lists (FR-045)."""
        _remove_host(self._paths, self._realm, self._hosts[app])
        self._result.removed.append(app)

    def _guarded(self, app: str, step: Callable[..., None], *args: Any) -> None:
        """Run the host writes of one app. An OS error skips that app with a warning, so the other
        apps still sync, and the next sync retries this one. Journal writes never run in here."""
        try:
            step(*args)
        except OSError as exc:
            self._warn_skipped(app, f"the host entry cannot be updated: {exc.strerror or exc}")

    def _process(self, app: str, stamp: str, host: _Host | None, size: int) -> None:
        target = self._applications / f"ginn-{self._realm}-{app}.desktop"
        if host is None and os.path.lexists(target):
            # A name taken by a file that is not this realm's entry is never written over. It is
            # checked before the copy, so no icon copy is made for an entry that stays out.
            reason = f"the file {target.name} exists and is not an entry of realm {self._realm}"
            self._warn_skipped(app, reason)
            return
        built = self._build(app, size)
        if isinstance(built, _Fail):
            self.failed = True
            self._warn_skipped(app, built.reason)
            return
        if isinstance(built, _Skip):
            self._warn_skipped(app, built.reason)
            if host is not None:
                _remove_host(self._paths, self._realm, host)
                self._result.removed.append(app)
            self._seen[app] = _Seen(stamp, exported=False)
            self._result.skipped.append(app)
            return
        self._write(app, stamp, host, built)

    def _write(self, app: str, stamp: str, host: _Host | None, built: _Built) -> None:
        target = self._applications / f"ginn-{self._realm}-{app}.desktop"
        if host is not None and host.text == built.text:
            self._result.unchanged.append(app)
        else:
            self._applications.mkdir(parents=True, exist_ok=True)
            write_file_atomic(target, built.text.encode("utf-8"), 0o644)
            (self._result.added if host is None else self._result.updated).append(app)
        if host is not None and host.icon is not None and host.icon not in built.icons:
            if posixpath.dirname(host.icon) == str(self._icon_dir):
                Path(host.icon).unlink(missing_ok=True)
        self._seen[app] = _Seen(stamp, exported=True)

    def _build(self, app: str, size: int) -> _Built | _Skip | _Fail:
        if size > DESKTOP_MAX_BYTES:
            # Checked on the listed size, so an oversized file is never read.
            return _Skip(f"larger than {DESKTOP_MAX_BYTES} bytes")
        try:
            raw = read_realm_file(
                self._podman,
                self._cid,
                f"{DESKTOP_DIR}/{app}.desktop",
                cap=DESKTOP_MAX_BYTES,
                target=self._target,
            )
        except RealmFileTooLarge as exc:
            return _Skip(str(exc))
        except RealmFileUnreadable as exc:
            return _Fail(str(exc))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _Skip("the file is not UTF-8 text, so it is not a Desktop Entry")
        try:
            entry = DesktopEntry.parse(text)
        except EntryError as exc:
            return _Skip(str(exc))
        reason = exportable_reason(entry)
        if reason is not None:
            return _Skip(reason)
        icons = self._icons(app, entry)
        rendered = render_export(
            entry, realm=self._realm, source=app, program=self._program, icon_paths=icons
        )
        if len(rendered.encode("utf-8")) > DESKTOP_MAX_BYTES:
            # The host copy is capped like the realm's file, so ginn can always read it back and
            # remove it. The icon copies made for it are removed too.
            for path in icons.values():
                Path(path).unlink(missing_ok=True)
            return _Skip(f"the host entry would be larger than {DESKTOP_MAX_BYTES} bytes")
        return _Built(rendered, tuple(sorted(set(icons.values()))))

    def _icons(self, app: str, entry: DesktopEntry) -> dict[str, str]:
        """Copy each icon of the entry out of the realm. A refused icon is warned and left out."""
        copies: dict[str, str] = {}
        for name in icon_names(entry):
            try:
                realm_path, size = resolve_icon(self._index_for(), name)
                self._icon_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                dest = copy_icon(
                    self._podman,
                    self._cid,
                    realm_path,
                    app=app,
                    dest_dir=self._icon_dir,
                    size=size,
                    target=self._target,
                )
            except IconError as exc:
                self._result.warnings.append(
                    warn("GINN-W-ICON-SKIPPED", app=app, realm=self._realm, reason=str(exc))
                )
                continue
            copies[name] = str(dest)
        return copies

    def _index_for(self) -> IconIndex:
        """The realm's icon listing, built once. An absolute icon path is looked up in it too."""
        if self._index is None:
            self._index = IconIndex.build(
                self._podman, self._cid, realm=self._realm, target=self._target
            )
        return self._index

    def _warn_skipped(self, app: str, reason: str) -> None:
        self._result.warnings.append(
            warn("GINN-W-EXPORT-SKIPPED", app=app, realm=self._realm, reason=reason)
        )


def _intact(host: _Host | None) -> bool:
    """True when an exported entry is on the host, with its icon copy when it has one."""
    if host is None:
        return False
    return host.icon is None or os.path.exists(host.icon)
