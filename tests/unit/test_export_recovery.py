# SPDX-License-Identifier: Apache-2.0
"""An interrupted sync, export or unexport is recovered as a host-only operation (FR-044, FR-047).

These ops write only host files, so recovery finishes their journal entry with no realm state
write. Before this rule they fell through to the generic podman-state path, which could move a
failed realm's record, and which blocked a deleting or broken realm, so its later operations
were refused with GINN-E-RECOVERY-PENDING. The helpers are those of tests/unit/test_ops_recovery.py.
"""

from __future__ import annotations

import pytest

from ginn.domain import RealmState
from ginn.ops._mutating import OpEnv
from ginn.ops.recovery import OUTCOME_RECOVERED, recover
from ginn.store.paths import OwnedPaths
from tests.unit.test_ops_recovery import (
    _audit,
    _dead,
    _only,
    _Podman,
    _record,
    _save,
    _state,
    _write_journal,
    env,  # noqa: F401 - pytest fixture, found by name
    paths,  # noqa: F401 - pytest fixture, found by name
)


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-047")
@pytest.mark.parametrize(
    ("op", "state"),
    [
        ("sync", RealmState.RUNNING),
        ("export", RealmState.FAILED),
        ("unexport", RealmState.DELETING),
    ],
)
def test_an_interrupted_host_only_op_is_recovered_and_moves_no_realm_state(
    paths: OwnedPaths,  # noqa: F811 - the fixture
    env: OpEnv,  # noqa: F811 - the fixture
    op: str,
    state: RealmState,
) -> None:
    _save(paths, _record(state=state))
    journal = _write_journal(paths, _dead(op=op))

    outcome = _only(recover(env, _Podman(container=None)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert outcome.op == op
    assert _state(paths) is state
    assert not journal.exists()
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome, entry.error_code) == (op, "recovered", None)
