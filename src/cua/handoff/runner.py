"""The run's side of a human handoff: ask a person, keep the browser open, wait, report back.

    1. The replay engine needs a person (an irreversible step to approve, a session lost after
       an irreversible step, an operator who took control unasked). It calls request().
    2. request() writes the Intervention into control.json and prints how to answer it.
    3. It polls the file. When an operator takes control, it brings the window to the front;
       the capture (cua.surface.capture) logs what the person does while they hold it.
    4. When the operator approves, releases or declines, it returns a Resolution. The engine
       re-checks the page against the artifact's checkpoint before it acts again.

The run's log is written by this process only. The operator CLI changes control.json and
nothing else; this side notices and writes the evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Protocol

from cua.core.runlog import RunLog
from cua.handoff.control import ControlFile, ControlState, Intervention, Lease, Resolution


class Window(Protocol):
    """The live browser window, as the handoff needs it. cua.surface.capture implements it."""

    async def focus(self) -> None: ...

    @property
    def actions(self) -> Sequence[object]:
        """Human actions captured so far, oldest first."""
        ...

    def irreversible(self, since: int) -> bool:
        """Did any action from index `since` on carry irreversible risk?"""
        ...


class HumanHandoff:
    def __init__(
        self,
        control: ControlFile,
        log: RunLog,
        *,
        window: Window | None = None,
        notify: Callable[[str], None] = print,
        timeout_s: float = 1800.0,
        poll_s: float = 0.5,
        lease: Lease | None = None,
    ):
        self.control = control
        # Pass the lease the surface checks, so adopting a new epoch here frees the surface.
        self.lease = lease or Lease(control)
        self.log = log
        self.window = window
        self.notify = notify
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._seen = 0  # captured human actions already reported in an earlier resolution

    async def request(self, iv: Intervention) -> Resolution:
        self.control.update(lambda s: s.model_copy(update={"intervention": iv, "answer": None}))
        self.notify(self._banner(iv))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        taken_by: str | None = None
        while True:
            state = self.control.read()
            if state.owner == "human" and taken_by is None:
                taken_by = state.holder
                self.log.event("control_taken", by=taken_by, epoch=state.epoch, intervention=iv.id)
                if self.window is not None:
                    await self.window.focus()
            answer = state.answer
            # A release nobody asked for (intervention_id None) also ends a takeover.
            if answer is not None and answer.intervention_id in (iv.id, None):
                return self._resolve(iv, state, taken_by)
            if loop.time() >= deadline:
                self.control.update(lambda s: s.model_copy(update={"intervention": None}))
                self.log.event("intervention_timeout", intervention=iv.id, after_s=self.timeout_s)
                return Resolution(kind="timeout", intervention_id=iv.id)
            await asyncio.sleep(self.poll_s)

    def _resolve(self, iv: Intervention, state: ControlState, taken_by: str | None) -> Resolution:
        assert state.answer is not None
        done = self.control.update(
            lambda s: s.model_copy(update={"intervention": None, "answer": None})
        )
        if taken_by is not None:
            self.log.event("control_released", by=state.answer.by, epoch=state.epoch)
        human = self.window.actions[self._seen :] if self.window is not None else ()
        irreversible = self.window.irreversible(self._seen) if self.window is not None else False
        self._seen += len(human)
        # Automation holds control again, at the operator's new epoch. The engine re-checks
        # the page before its next action, so adopting it here is safe.
        self.lease.adopt(done.epoch)
        return Resolution(
            kind=state.answer.kind,
            intervention_id=iv.id,
            by=state.answer.by,
            human_actions=len(human),
            human_irreversible=irreversible,
        )

    def _banner(self, iv: Intervention) -> str:
        run = self.log.ref  # the run directory, relative to the repo root
        what = {
            "approval": "approve, decline, or take control and do it yourself",
            "check": "take control, check the page, then release (or decline)",
            "takeover": "release when you are done",
        }[iv.kind]
        return "\n".join(
            [
                "",
                f"=== Needs a person: {iv.kind} ({iv.id}) ===",
                f"capability: {iv.capability}   step: {iv.step_id or '-'}   {iv.intent}",
                f"reason:     {iv.reason}",
                f"page:       {iv.url}",
                f"screenshot: {run}/{iv.screenshot}" if iv.screenshot else "",
                f"To answer ({what}):",
                f"  uv run cua operator approve {run} {iv.id}",
                f"  uv run cua operator take {run}      then   uv run cua operator release {run}",
                f"  uv run cua operator decline {run} {iv.id}",
                "",
            ]
        )
