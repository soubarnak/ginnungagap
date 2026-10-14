# SPDX-License-Identifier: Apache-2.0
"""Kill points for the crash-safety suite (NFR-030, FR-122, FR-123). Used by test_crash_safety.py.

A SIGKILL can land between any two durable effects of an operation. This module numbers those
effects in one run and kills the run at one of them:

- The effects are the journal begin, step and finish, the audit append, the record writes and every
  podman call that changes state. Each effect has two points, one before it runs and one after it
  returns. A point is a kill point, and `Probe.points` names them in the order they happen. A dry
  run lists the kill points of an operation, and each later run kills at one of them, from a fresh
  world.
- An in-process kill raises SimulatedCrash, a BaseException, so no `except Exception` in ops catches
  it. From the kill on, the dead process writes nothing. The probe refuses every later effect, so
  `mutating()`'s exit steps cannot finish the journal entry or write an audit line, as after a real
  SIGKILL. A podman call attempted after the death is recorded in `dead_attempts`, and the suite
  asserts that none happened.
- A real kill (`Probe(real=True)`) runs in a forked child. At the point, the child pickles its
  podman state and sends itself SIGKILL. Nothing runs after that, and the kernel releases the realm
  lock. The parent reads the pickled state back, so recovery sees the podman state the child left.

The seams are class-level patches, made for one run inside `probing()` and removed when it exits,
and instance-level wrappers on the podman object, removed in the same way. Recovery runs after the
context has exited, so it is never counted and never refused.
"""

from __future__ import annotations

import os
import pickle
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest

from ginn.ops._mutating import Mutation
from ginn.store.audit import AuditLog
from ginn.store.journal import Journal
from ginn.store.records import RealmStore

T = TypeVar("T")

PODMAN_MUTATORS: tuple[str, ...] = (
    "pull",
    "create",
    "start",
    "stop",
    "rm",
    "rmi",
    "exec",
    "exec_interactive",
    "unshare",
    "cp_into",
)
"""The podman calls that change state. Each one is an effect, with a point before and after it.
`cp_into` writes the services-on machine-id before the first start (ADR-007, F6). `cp_from`, the
read of the init check, changes nothing, so it is not an effect."""


class SimulatedCrash(BaseException):
    """A SIGKILL at one point. A BaseException, so no `except Exception` in the ops catches it."""


class DeadWrite(OSError):
    """An effect attempted after the simulated death. A dead process writes nothing."""


class Probe:
    """Numbers the points of one run and kills the run at point `kill_at`. None is a dry run.

    `points` holds the label of every point in order. `dead_attempts` holds the labels of the
    effects that were refused after the death (the exit steps of `mutating()` only).
    """

    def __init__(
        self,
        kill_at: int | None = None,
        *,
        real: bool = False,
        snapshot: Path | None = None,
    ) -> None:
        self.kill_at = kill_at
        self.real = real
        self.snapshot = snapshot
        self.points: list[str] = []
        self.dead = False
        self.dead_attempts: list[str] = []
        self.podman: Any = None

    def effect(self, label: str, call: Callable[[], T]) -> T:
        """Run one effect between its two points. After the death, the effect is refused."""
        if self.dead:
            self.dead_attempts.append(label)
            raise DeadWrite(f"{label}: the process is dead")
        self._point(f"before {label}")
        result = call()
        self._point(f"after {label}")
        return result

    def _point(self, label: str) -> None:
        index = len(self.points)
        self.points.append(label)
        if index != self.kill_at:
            return
        self.dead = True
        if self.real:
            self._sigkill()
        raise SimulatedCrash(label)

    def _sigkill(self) -> None:
        """Save the podman state that a real podman keeps, then kill this process with SIGKILL."""
        assert self.snapshot is not None, "a real kill needs a snapshot path"
        self.snapshot.write_bytes(pickle.dumps(podman_state(self.podman)))
        os.kill(os.getpid(), signal.SIGKILL)


class LabelProbe(Probe):
    """A Probe that kills at the first point with `label`, not at an index.

    A test builds a starting state with it: the operation that it kills has its effects up to the
    point, and the point is the one that `Probe.points` names. The kill is in-process.
    """

    def __init__(self, label: str) -> None:
        super().__init__(None)
        self.label = label

    def _point(self, label: str) -> None:
        self.points.append(label)
        if label != self.label or self.dead:
            return
        self.dead = True
        raise SimulatedCrash(label)


def podman_state(podman: Any) -> dict[str, Any]:
    """The attributes of the fake podman, without the instance wrappers of `probing()`."""
    return {key: value for key, value in vars(podman).items() if key not in PODMAN_MUTATORS}


@contextmanager
def probing(probe: Probe, podman: Any) -> Iterator[Probe]:
    """Install the points of one run. Class seams cover the journal, the audit log and the record
    store. Instance seams cover the mutating podman calls. Every seam is removed on exit."""
    probe.podman = podman
    wrapped = [name for name in PODMAN_MUTATORS if hasattr(podman, name)]
    with pytest.MonkeyPatch.context() as mp:
        _seam(mp, Mutation, "step", probe, lambda args: f"journal step {args[0]}")
        _seam(mp, Journal, "begin", probe, lambda args: "journal begin")
        _seam(mp, Journal, "finish", probe, lambda args: "journal finish")
        _seam(mp, AuditLog, "append", probe, lambda args: "audit append")
        _seam(mp, RealmStore, "save", probe, lambda args: "record save")
        _seam(mp, RealmStore, "set_state", probe, lambda args: f"record state {args[1]}")
        _seam(mp, RealmStore, "delete", probe, lambda args: "record delete")
        for name in wrapped:
            _wrap_instance(podman, name, probe)
        try:
            yield probe
        finally:
            for name in wrapped:
                delattr(podman, name)


def _seam(
    mp: pytest.MonkeyPatch,
    owner: type,
    name: str,
    probe: Probe,
    label: Callable[[tuple[Any, ...]], str],
) -> None:
    original = getattr(owner, name)

    def seam(self: Any, *args: Any, **kwargs: Any) -> Any:
        return probe.effect(label(args), lambda: original(self, *args, **kwargs))

    mp.setattr(owner, name, seam)


def _wrap_instance(podman: Any, name: str, probe: Probe) -> None:
    method = getattr(podman, name)

    def seam(*args: Any, **kwargs: Any) -> Any:
        return probe.effect(f"podman {name}", lambda: method(*args, **kwargs))

    setattr(podman, name, seam)


def load_podman_state(podman: Any, snapshot: Path) -> None:
    """Replace the podman state of the parent with the state a killed child saved."""
    podman.__dict__.update(pickle.loads(snapshot.read_bytes()))  # noqa: S301 - our own child wrote it
