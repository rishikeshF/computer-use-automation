"""The control lease: who drives a run right now, automation or a person. One file per run.

    runs/<run_id>/control.json
        owner         automation | human
        holder        who: "automation", or the operator's name
        epoch         goes up by one every time control changes hands
        intervention  what automation is waiting for a person to decide, if anything
        answer        the operator's reply to it: approved | released | declined

Two processes share the file: the run itself (opens interventions, reads answers) and the
operator CLI (takes, releases, approves, declines). Every change happens under a lock file, and
the file is replaced in one step, so neither ever reads half a write.

Why an epoch and not just an owner: automation checks the lease before every action. If a
person took control and gave it back in between, the owner reads "automation" again, but the
page may have changed under the run. The epoch is different, so the run stops and re-checks
where it is instead of carrying on blind.

Nothing here touches the browser. The operator only edits this file; the run notices.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cua.core.runlog import utc_now
from cua.surface.guarded import ControlLost

Owner = Literal["automation", "human"]
AUTOMATION = "automation"
_LOCK_STALE_S = 5.0  # a lock older than this was left by a process that died mid-write


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Intervention(_Model):
    """A request for a person, written by the run. Everything in it is already redacted."""

    id: str  # unique within the run, e.g. s4-1
    # approval: an irreversible step waits for a yes. check: something went wrong after an
    # irreversible step and a person must look. takeover: a person took control unasked.
    kind: Literal["approval", "check", "takeover"]
    capability: str
    step_id: str | None = None
    intent: str = ""  # the step's plain-English intent, from the artifact
    reason: str
    url: str = ""
    screenshot: str | None = None  # masked PNG in the run directory
    requested_at: str


class Answer(_Model):
    """The operator's reply. `intervention_id` is None for a release nobody asked for."""

    kind: Literal["approved", "released", "declined"]
    by: str
    intervention_id: str | None
    at: str


class ControlState(_Model):
    run_id: str
    owner: Owner = "automation"
    holder: str = AUTOMATION
    epoch: int = 1
    intervention: Intervention | None = None
    answer: Answer | None = None
    updated_at: str


class Resolution(_Model):
    """How an intervention ended, as the run sees it."""

    kind: Literal["approved", "released", "declined", "timeout"]
    intervention_id: str
    by: str | None = None
    human_actions: int = 0  # actions the capture saw while the person held control
    human_irreversible: bool = False  # one of them was irreversible (it may have committed)


# One operator command, as a change to the state. Applied with ControlFile.update().
Change = Callable[["ControlState"], "ControlState"]


class ControlError(Exception):
    """An operator command that does not fit the run's state (release what you never took)."""


class ControlFile:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        self.path = self.dir / "control.json"
        self._lock = self.dir / "control.lock"

    @classmethod
    def create(cls, run_dir: str | Path, run_id: str) -> ControlFile:
        """A fresh lease, held by automation at epoch 1."""
        cf = cls(run_dir)
        cf.dir.mkdir(parents=True, exist_ok=True)
        with cf._locked():
            cf._write(ControlState(run_id=run_id, updated_at=utc_now()))
        return cf

    def read(self) -> ControlState:
        # The other process may be replacing the file this instant (Windows refuses to open it
        # then). Retry briefly rather than misread.
        for _ in range(50):
            with suppress(PermissionError, ValidationError, FileNotFoundError):
                return ControlState.model_validate_json(self.path.read_text(encoding="utf-8"))
            time.sleep(0.02)
        return ControlState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def update(self, change: Change) -> ControlState:
        """Read, change and write back under the lock. `change` may raise ControlError."""
        with self._locked():
            state = change(self.read())
            state = state.model_copy(update={"updated_at": utc_now()})
            self._write(state)
            return state

    def human_holds(self) -> bool:
        return self.read().owner == "human"

    def _write(self, state: ControlState) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        for _ in range(50):
            try:
                os.replace(tmp, self.path)  # one step: readers see the old file or the new one
                return
            except PermissionError:  # a reader has it open (Windows)
                time.sleep(0.02)
        os.replace(tmp, self.path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        deadline = time.monotonic() + _LOCK_STALE_S
        while True:
            try:
                fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    with suppress(FileNotFoundError):
                        os.remove(self._lock)  # its owner died; take it over
                    deadline = time.monotonic() + _LOCK_STALE_S
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(fd)
            with suppress(FileNotFoundError):
                os.remove(self._lock)


class Lease:
    """Automation's side of the lease. GuardedSurface calls check() before every action."""

    def __init__(self, control: ControlFile):
        self.control = control
        self.epoch = control.read().epoch  # the epoch automation last knowingly held

    def check(self) -> None:
        state = self.control.read()
        if state.owner != "automation":
            raise ControlLost(f"{state.holder} holds control")
        if state.epoch != self.epoch:
            raise ControlLost("control changed hands since automation last acted")

    def adopt(self, epoch: int) -> None:
        """Hold control again at `epoch`, after the run has re-checked where it is."""
        self.epoch = epoch


# ---- operator commands -------------------------------------------------------------------------


def take(by: str) -> Change:
    def change(s: ControlState) -> ControlState:
        if s.owner == "human":
            raise ControlError(f"{s.holder} already holds control")
        return s.model_copy(update={"owner": "human", "holder": by, "epoch": s.epoch + 1})

    return change


def release(by: str) -> Change:
    def change(s: ControlState) -> ControlState:
        if s.owner != "human":
            raise ControlError("automation holds control; nothing to release")
        return _answered(s, "released", by)

    return change


def approve(by: str, intervention_id: str) -> Change:
    """Yes to one named intervention. Naming it means a yes given while looking at one request
    can never land on another (the run moved on, or paused somewhere else)."""

    def change(s: ControlState) -> ControlState:
        _check_waiting(s, intervention_id)
        return _answered(s, "approved", by)

    return change


def decline(by: str, intervention_id: str) -> Change:
    def change(s: ControlState) -> ControlState:
        _check_waiting(s, intervention_id)
        return _answered(s, "declined", by)

    return change


def _check_waiting(s: ControlState, intervention_id: str) -> None:
    if s.intervention is None:
        raise ControlError("no intervention is waiting")
    if s.intervention.id != intervention_id:
        raise ControlError(
            f"{intervention_id!r} is not the waiting intervention ({s.intervention.id!r}: "
            f"{s.intervention.kind} at {s.intervention.step_id or '-'}); nothing changed"
        )


def _answered(
    s: ControlState, kind: Literal["approved", "released", "declined"], by: str
) -> ControlState:
    """Every answer hands control back to automation, at a new epoch."""
    answer = Answer(
        kind=kind,
        by=by,
        intervention_id=s.intervention.id if s.intervention else None,
        at=utc_now(),
    )
    return s.model_copy(
        update={"owner": "automation", "holder": AUTOMATION, "epoch": s.epoch + 1, "answer": answer}
    )
