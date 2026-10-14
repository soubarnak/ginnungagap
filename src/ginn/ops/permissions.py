# SPDX-License-Identifier: Apache-2.0
"""Change a realm's tier and its Custom permissions (FR-060, FR-063, FR-066, DR-004).

`set_tier` and `set_permissions` are mutating operations. Each runs inside `mutating()` (DR-032,
FR-123, FR-124): it takes the realm lock and writes one journal entry and one audit entry. Every
check is inside the body, including the input checks and the DR-004 check of a share, so a refused
change still leaves an audit entry with its parameters (NFR-SEC-008). A refused change writes no
record. The journal steps are `checked` (every refusal has been passed) and `saved` (the record is
written). The audit entry of an accepted change carries the tier, the toggles and the shares before
and after (FR-124 AC-1).

Tier. A change of tier is one of two kinds. The first changes only attach-time grants, and it
applies at the next start: a running realm gets `restart_required` (FR-066). The second changes a
create-time option (a device, a security option or a kept group, ADR-006). That container cannot
take the option after its create, so the change is refused with GINN-E-NOT-SUPPORTED-YET, naming
T8.3. The new container would lose the packages that the Install Manifest replay (T8.3) keeps.
The host's `/dev/kvm` at create comes from the record's `kvm_at_create`: it is the probe that made
the container, not a guess from its options.

Custom starting state. Switching to Custom from Basic or Development keeps the toggles of the tier
the realm came from, and it has no shares. Switching to Basic or Development clears the Custom part
(FR-061, FR-062), so no share outlives its tier.

Permissions. `set_permissions` changes a Custom realm only (GINN-E-TIER-NOT-CUSTOM otherwise). A
share is checked with `SharedPath.parse` (DR-004, FR-063 AC-2) and stored as the path that DR-004
resolved, with its mode. Removing a share needs a path that the realm shares, given as named or
resolved.

Services-on realms are refused until M7 (ADR-003). A record that is neither stopped nor running is
refused with GINN-E-REALM-STATE (DR-020). The record is loaded before the operation, so a missing
realm writes no audit entry.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Final

from ginn.domain import RealmName, RealmState, Tier
from ginn.domain.paths import Mode, SharedPath
from ginn.domain.permissions import CustomShare, Permissions, Toggle, effective_toggles
from ginn.errors import Warning as GinnWarning
from ginn.errors import make
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.lifecycle import HostUser
from ginn.security.caps import option_keys
from ginn.store._time import utc_from_epoch
from ginn.store.records import RealmRecord, RealmStore

_ALLOWED: Final = "stopped or running"
_TIER_STATES: Final = frozenset({RealmState.STOPPED, RealmState.RUNNING})


def _toggles(names: Sequence[str]) -> frozenset[Toggle]:
    """The toggles named on the command line. An unknown name raises GINN-E-USAGE."""
    chosen: set[Toggle] = set()
    for name in names:
        try:
            chosen.add(Toggle(name))
        except ValueError:
            known = ", ".join(toggle.value for toggle in Toggle)
            detail = f"unknown permission {name!r}; use one of {known}"
            raise make("GINN-E-USAGE", detail=detail) from None
    return frozenset(chosen)


def _checked_shares(
    env: OpEnv, host: HostUser, adds: Sequence[tuple[str, Mode]]
) -> list[CustomShare]:
    """Each share validated with DR-004 (FR-063 AC-2). Raises GINN-E-PATH-FORBIDDEN on a refusal."""
    owned = env.paths.all_owned_roots()
    checked: list[CustomShare] = []
    for raw, mode in adds:
        shared = SharedPath.parse(
            raw, home=host.host_home, owned_paths=owned, mode=mode, env=host.host_env
        )
        checked.append(CustomShare(str(shared.path), mode))
    return checked


def _refuse_services(record: RealmRecord, verb: str) -> None:
    if record.settings.services:
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=record.name.value,
            feature=f"{verb} of a services-on realm",
            milestone="M7",
        )


def _refuse_state(record: RealmRecord, verb: str) -> None:
    if record.state not in _TIER_STATES:
        raise make(
            "GINN-E-REALM-STATE",
            realm=record.name.value,
            state=record.state.value,
            action=verb,
            allowed=_ALLOWED,
        )


def _refuse_create_change(record: RealmRecord, tier: Tier, permissions: Permissions) -> None:
    """Refuse a change whose create-time options differ from the container's (ADR-006, T8.3).

    The options that the container should have are worked out from the new toggles and the probe
    stored at create. A difference from `created_options` needs a new container, which is refused.
    The GPU device and the fonts and timezone binds are create-time grants too, so their toggles
    are compared with the flags stored at create (`RealmRecord.create_grants_match`).
    """
    toggles = effective_toggles(tier, permissions.toggles)
    wanted = option_keys(toggles, kvm_present=record.kvm_at_create)
    if wanted != record.created_options or not record.create_grants_match(toggles):
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=record.name.value,
            feature="a change of tier or permissions that adds or removes a create-time option",
            milestone="M8 (T8.3)",
        )


def _save(env: OpEnv, store: RealmStore, record: RealmRecord, **changes: Any) -> RealmRecord:
    """Write `record` with `changes`. A running realm is flagged: its grants change at the start."""
    running = record.state is RealmState.RUNNING
    saved = replace(
        record,
        updated_at=utc_from_epoch(env.clock()),
        restart_required=record.restart_required or running,
        **changes,
    )
    store.save(saved)
    return saved


def _view(record: RealmRecord) -> dict[str, Any]:
    """The permissions of a record as the audit entry shows them (FR-124 AC-1).

    The entry holds the tier, the toggles and the shares. The toggles are the effective ones, so a
    Basic or Development realm shows its tier's set.
    """
    toggles = effective_toggles(record.settings.tier, record.permissions.toggles)
    return {
        "tier": record.settings.tier.value,
        "toggles": sorted(toggle.value for toggle in toggles),
        "shares": [share.to_dict() for share in record.permissions.shares],
    }


def _custom_part(record: RealmRecord, tier: Tier) -> Permissions:
    """The permissions a realm of `tier` keeps after the change (see the module docstring)."""
    if tier is not Tier.CUSTOM:
        return Permissions()
    if record.settings.tier is Tier.CUSTOM:
        return record.permissions
    return Permissions(toggles=effective_toggles(record.settings.tier, record.permissions.toggles))


def set_tier(
    env: OpEnv,
    name: RealmName,
    tier: Tier,
    *,
    warnings: list[GinnWarning] | None = None,
) -> RealmRecord:
    """Change realm `name` to `tier` (FR-060). Return the record as it now stands.

    A change that needs no new container is saved and applies at the next start (FR-066). An
    unchanged tier writes nothing. Whether `/dev/kvm` was there at create is read from the record's
    `kvm_at_create`, so no host probe is made here. The FR-006 warnings of the change are appended
    to `warnings`, once the operation has finished.
    """
    store = RealmStore(env.paths, env.clock)
    store.load(name)  # GINN-E-NO-SUCH-REALM before any audit entry
    with mutating(env, "tier", name, {"tier": tier.value}) as mutation:
        record = _tier_body(env, store, name, tier, mutation)
    if warnings is not None:
        warnings.extend(mutation.warnings)
    return record


def _tier_body(
    env: OpEnv, store: RealmStore, name: RealmName, tier: Tier, mutation: Mutation
) -> RealmRecord:
    record = store.load(name)
    _refuse_services(record, "tier change")
    _refuse_state(record, "tier")
    permissions = _custom_part(record, tier)
    _refuse_create_change(record, tier, permissions)
    mutation.step("checked")
    if tier is record.settings.tier and permissions == record.permissions:
        return record
    before = _view(record)
    settings = replace(record.settings, tier=tier)
    saved = _save(env, store, record, settings=settings, permissions=permissions)
    mutation.set_permissions(before, _view(saved))
    mutation.step("saved")
    return saved


def set_permissions(
    env: OpEnv,
    name: RealmName,
    host: HostUser,
    *,
    share: Sequence[tuple[str, Mode]] = (),
    unshare: Sequence[str] = (),
    enable: Sequence[str] = (),
    disable: Sequence[str] = (),
    warnings: list[GinnWarning] | None = None,
) -> RealmRecord:
    """Change the Custom toggles and shares of realm `name` (FR-063). Return the record.

    `share` adds a path with a mode, and `unshare` removes a shared path. `enable` and `disable`
    name toggles (domain/permissions.py). Every input is checked inside the operation, so a refused
    input leaves an audit entry and no record write.
    """
    store = RealmStore(env.paths, env.clock)
    store.load(name)  # GINN-E-NO-SUCH-REALM before any audit entry
    params = _params(share, unshare, enable, disable)
    with mutating(env, "perms", name, params) as mutation:
        record = _perms_body(
            env,
            store,
            name,
            host,
            mutation,
            share=share,
            unshare=unshare,
            enable=enable,
            disable=disable,
        )
    if warnings is not None:
        warnings.extend(mutation.warnings)
    return record


def _params(
    share: Sequence[tuple[str, Mode]],
    unshare: Sequence[str],
    enable: Sequence[str],
    disable: Sequence[str],
) -> Mapping[str, Any]:
    """The audit parameters of a permission change: each share, unshare and toggle as requested."""
    return {
        "share": [{"path": path, "mode": mode} for path, mode in share],
        "unshare": list(unshare),
        "enable": list(enable),
        "disable": list(disable),
    }


def _perms_body(
    env: OpEnv,
    store: RealmStore,
    name: RealmName,
    host: HostUser,
    mutation: Mutation,
    *,
    share: Sequence[tuple[str, Mode]],
    unshare: Sequence[str],
    enable: Sequence[str],
    disable: Sequence[str],
) -> RealmRecord:
    switched_on = _toggles(enable)
    switched_off = _toggles(disable)
    adds = _checked_shares(env, host, share)
    record = store.load(name)
    _refuse_services(record, "perms")
    if record.settings.tier is not Tier.CUSTOM:
        raise make("GINN-E-TIER-NOT-CUSTOM", realm=name.value, tier=record.settings.tier.value)
    _refuse_state(record, "perms")
    shares = {share_.path: share_ for share_ in record.permissions.shares}
    for raw in unshare:
        # A share is stored under its resolved path, so the name given may be either form.
        resolved = os.path.realpath(raw)
        match = resolved if resolved in shares else raw
        if match not in shares:
            raise make("GINN-E-USAGE", detail=f"realm '{name.value}' does not share {raw}")
        del shares[match]
    for added in adds:
        shares[added.path] = added
    toggles = (set(record.permissions.toggles) | switched_on) - switched_off
    permissions = Permissions(toggles=frozenset(toggles), shares=tuple(shares.values()))
    _refuse_create_change(record, Tier.CUSTOM, permissions)
    mutation.step("checked")
    if permissions == record.permissions:
        return record
    before = _view(record)
    saved = _save(env, store, record, permissions=permissions)
    mutation.set_permissions(before, _view(saved))
    mutation.step("saved")
    return saved


def read_permissions(env: OpEnv, name: RealmName) -> RealmRecord:
    """The record of realm `name`, read without a lock or a write (FR-063, `ginn perms` alone)."""
    return RealmStore(env.paths, env.clock).load(name)


__all__ = ["read_permissions", "set_permissions", "set_tier"]
