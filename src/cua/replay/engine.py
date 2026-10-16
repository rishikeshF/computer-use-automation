"""Replay: run an artifact step by step, with no model, and say exactly how it ended.

For every step, in order:

    1. pre        wait until we are on the page the step was recorded on
    2. outcomes   business answers declared for this step (account_not_found). If one holds,
                  stop with BusinessOutcome: the app answered, the answer is "no"
    3. act        resolve the target by its LocatorChain and run the action through policy
    4. post       wait until the page shows what the step should have produced, or one of the
                  business answers declared for after it (invalid_credentials)

While waiting, every look at the page also runs the app-level detectors:
    session expired  -> sign in again with ensure_session, resume from the last page load
    app error        -> page loads are retried (step.retry); anything else is a Failure
    dialog           -> already dismissed by the surface; noted as a recovery

Recoveries never change the result's status. They are listed in evidence.recoveries, so a
reviewer can still see that the run needed help.

Irreversible steps change the rules. Once one has run (money moved, an account opened), the run
never signs in again, rewinds, retries or re-clicks: any of those could do it twice. Whatever
goes wrong after that point ends as NeedsHuman, with a person told exactly which step committed.

People (M5). With a handoff attached, a pause does not end the run: the browser stays open and
an operator approves, declines, or takes control and acts. Before automation acts again it
re-checks the page against the artifact's checkpoint. Without a handoff, a pause ends the run
as NeedsHuman.

Nothing here imports the LLM. tests/test_boundaries.py enforces that.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, NoReturn, Protocol

from cua.core.action import Action, Click, Fill, Navigate, Select
from cua.core.artifact import (
    Capability,
    Checkpoint,
    Condition,
    OutcomeDetector,
    ParamValue,
    Step,
    nav_fingerprint,
)
from cua.core.locator import LocatorChain
from cua.core.outputs import convert
from cua.core.policy import Decision
from cua.core.result import (
    ArtifactRef,
    BusinessOutcome,
    Evidence,
    Failure,
    FailureKind,
    NeedsHuman,
    RunResult,
    Success,
)
from cua.core.runlog import RunLog, utc_now
from cua.core.template import render
from cua.core.url import strip_session
from cua.handoff.control import Intervention, Resolution
from cua.replay.detectors import PARABANK, AppSignals, app_issue, checkpoint_gap
from cua.surface.base import Observation, Resolved, SurfaceError, Unresolved
from cua.surface.guarded import ControlLost, GuardedSurface

_POLL_S = 0.25
SESSION = "ensure_session"

# Establishes a precondition (signs in). Success means it now holds.
Precondition = Callable[[], Awaitable[RunResult]]


class StepHooks(Protocol):
    """Called before each step of the capability under test. Fault injection uses it."""

    async def before_step(self, step: Step) -> None: ...


class Handoff(Protocol):
    """A person who can take over a paused run. cua.handoff.runner implements it."""

    async def request(self, intervention: Intervention) -> Resolution: ...


@dataclass
class ReplayOptions:
    base_url: str = ""  # the app root; fills {{base_url}} in navigate steps
    confirm: bool = False  # caller's explicit yes for an approved irreversible capability
    preconditions: Mapping[str, Precondition] = field(default_factory=dict)
    hooks: StepHooks | None = None
    handoff: Handoff | None = None
    signals: AppSignals = PARABANK
    max_recoveries: int = 2


class _Stop(Exception):
    """End the run now with this result."""

    def __init__(self, result: RunResult):
        self.result = result


class _SessionLost(Exception):
    """The login form appeared mid-run."""


class _Retake(Exception):
    """An operator held control while this step was about to act. Its action never ran, so
    the step runs again from its `pre` check."""


async def replay(
    cap: Capability,
    inputs: Mapping[str, str],
    surface: GuardedSurface,
    log: RunLog,
    options: ReplayOptions | None = None,
) -> RunResult:
    run = _Run(cap, dict(inputs), surface, log, options or ReplayOptions())
    try:
        return await run.execute()
    except _Stop as stop:
        return stop.result
    except SurfaceError as e:
        # Last line: a surface error no step handled (the page closed mid-wait) is still a
        # result, never an exception. After a commit it is NeedsHuman, like any other failure.
        try:
            run.fail("action_failed", observed=f"{e.kind}: {e.detail}")
        except _Stop as stop:
            return stop.result


class _Run:
    def __init__(
        self,
        cap: Capability,
        inputs: dict[str, str],
        surface: GuardedSurface,
        log: RunLog,
        opts: ReplayOptions,
    ):
        self.cap = cap
        self.inputs = inputs
        self.s = surface
        self.log = log
        self.opts = opts
        self.params = {**inputs}
        self.outputs: dict[str, str] = {}
        self.recoveries: list[str] = []
        self.interventions: list[str] = []
        self.warnings: list[str] = []
        # Session loss is only a recoverable condition when we know how to sign back in.
        self.check_session = SESSION in opts.preconditions
        # Reviewed artifact plus the caller's explicit yes: irreversible steps run unasked.
        self.preapproved = cap.status == "approved" and opts.confirm
        # The irreversible step that has run, if any. From then on, no rewinds and no retries.
        self.committed: Step | None = None
        self._pauses = 0

    # ---- the run -------------------------------------------------------------------------------

    async def execute(self) -> RunResult:
        cap = self.cap
        self.log.event(
            "replay_start",
            capability=cap.id,
            status=cap.status,
            content_hash=cap.content_hash,
            inputs=sorted(self.inputs),
        )
        if cap.status == "deprecated":
            note = f": {cap.status_note}" if cap.status_note else ""
            self._fail(
                "policy_violation",
                expected="a draft or approved artifact",
                observed=f"{cap.id} is deprecated{note}",
            )
        self._check_inputs()
        # With a person on hand, an irreversible step pauses for them instead.
        if cap.risk == "irreversible" and not self.preapproved and self.opts.handoff is None:
            self._fail(
                "policy_violation",
                expected="status approved and confirm=true, or a human handoff",
                observed=f"status {cap.status}, confirm={self.opts.confirm}",
            )
        for name in cap.preconditions:
            await self._establish(name, why="precondition")

        i = 0
        recovered = 0
        while i < len(cap.steps):
            step = cap.steps[i]
            if self.opts.hooks is not None:
                await self.opts.hooks.before_step(step)
            try:
                await self._step(step, first=i == 0)
                i += 1
            except _Retake:
                continue
            except _SessionLost:
                if self.committed is not None:
                    # Signing in again and resuming would replay pages after an irreversible
                    # step. A person must see what state the app is in.
                    await self._pause(
                        step,
                        "check",
                        f"session expired after irreversible step {self.committed.id} had run",
                    )
                    await self._verify(step.post, step, "after the check")
                    i += 1
                    continue
                recovered += 1
                if recovered > self.opts.max_recoveries:
                    self._fail("app_error", step, observed="session keeps expiring")
                await self._establish(SESSION, why=f"session expired at {step.id}")
                # Resume from the last page load: the steps after it assumed that page.
                i = max((j for j in range(i + 1) if cap.steps[j].action == "navigate"), default=0)
                self._recover(
                    f"{step.id}: session expired; signed in again, resumed at {cap.steps[i].id}"
                )

        await self._wait(cap.success, None, "success")
        return self._success()

    # ---- one step ------------------------------------------------------------------------------

    async def _step(self, step: Step, *, first: bool) -> None:
        if step.pre is not None:
            await self._wait(step.pre, step, "pre")
        for detector in self.cap.outcomes:
            if step.id in detector.check_before and await self._holds(detector, step):
                self._outcome(detector, step)

        if step.action == "navigate":
            await self._navigate(step)
        else:
            await self._act(step)

        after = [d for d in self.cap.outcomes if step.id in d.check_after]
        obs = await self._wait(step.post, step, "post", outcomes=after)
        if first and step.action == "navigate":
            self._check_fingerprint(obs)
        shot = await self.s.observe(screenshot=True)
        self.log.event(
            "step",
            step=step.id,
            action=step.action,
            intent=step.intent,
            screenshot=self.log.screenshot(shot.screenshot, step.id),
        )

    async def _navigate(self, step: Step) -> None:
        url = render(str(step.value), self._all_params())
        tries = step.retry.max + 1
        for attempt in range(1, tries + 1):
            problem: str
            try:
                decision = await self._do(step, Navigate(url=url))
            except SurfaceError as e:
                problem = f"page load failed: {e.detail}"
            else:
                if not decision.allowed:
                    self._fail("policy_violation", step, observed=decision.reason)
                issue = app_issue(
                    await self.s.observe(screenshot=False),
                    self.opts.signals,
                    check_session=self.check_session,
                )
                if issue is None:
                    return
                if issue[0] == "session_expired":
                    raise _SessionLost
                problem = issue[1]
            if self.committed is not None:
                # Loading pages again after money moved is a person's call, not a retry policy.
                await self._pause(
                    step,
                    "check",
                    f"{problem}, after irreversible step {self.committed.id} had run; not retrying",
                )
                await self._verify(step.post, step, "after the check")
                return
            if attempt == tries:
                self._fail(
                    "app_error", step, observed=problem, screenshot=await self._shot("app-error")
                )
            self._recover(f"{step.id}: {problem}; retry {attempt} of {tries - 1}")
            await asyncio.sleep(step.retry.backoff_ms / 1000 * attempt)

    async def _act(self, step: Step) -> None:
        found = await self._resolve(step)
        if step.action == "extract":
            if found.node is None:
                self._fail("locator_not_found", step, observed="matched element has no text")
            self.outputs[str(step.value)] = found.node.name
            return

        decision = await self._do(step, self._action(step, found.ref), approved=self.preapproved)
        if decision.verdict == "needs_approval":
            answer = await self._pause(step, "approval", decision.reason)
            if answer.kind == "approved":
                # Only this step is approved. Find the target again: the page may have been
                # touched while the run waited.
                self._intervened(f"{step.id}: approved by {answer.by}")
                found = await self._resolve(step)
                decision = await self._do(step, self._action(step, found.ref), approved=True)
            elif answer.human_irreversible:
                self._intervened(f"{step.id}: done by {answer.by} in the browser")
                await self._verify(step.post, step, f"{answer.by} released control")
                return
            else:
                self._needs_human(
                    step,
                    f"{answer.by} released control without approving or doing {step.id}",
                    answer.intervention_id,
                )
        if decision.verdict == "deny":
            self._fail("policy_violation", step, observed=decision.reason)

    def _commit(self, step: Step, decision: Decision) -> None:
        """Called after policy allowed the action and before it runs. An irreversible step counts
        as done from here on: if the surface fails halfway, the effect may already be real."""
        if self.committed is None and "irreversible" in (decision.risk, step.risk):
            self.committed = step
            self.log.event("committed", step=step.id, intent=step.intent)

    async def _do(self, step: Step, action: Action, *, approved: bool = False) -> Decision:
        """Run one action through the guarded surface and return policy's decision.

        A surface error on a click, fill or select ends the run as a Failure (NeedsHuman once
        anything irreversible ran). Page loads raise it on, so _navigate can retry them.
        If an operator took control unasked, wait for them, then run the step again from the
        top (its action never ran)."""
        try:
            return await self.s.act(
                action,
                irreversible_approved=approved,
                before_run=lambda d: self._commit(step, d),
            )
        except SurfaceError as e:
            if isinstance(action, Navigate):
                raise
            await self._look(step)  # a session or app problem may explain it
            self._fail(
                "action_failed",
                step,
                expected=f"{action.kind} to complete",
                observed=f"{e.kind}: {e.detail}",
                screenshot=await self._shot(f"{step.id}-action-failed"),
            )
        except ControlLost as e:
            answer = await self._pause(step, "takeover", f"an operator took control: {e}")
            if answer.human_irreversible:
                # The run cannot know what was committed or where; neither can it carry on.
                self._needs_human(
                    step,
                    f"{answer.by} did an irreversible action outside an approval point",
                    answer.intervention_id,
                )
            self._intervened(f"{step.id}: {answer.by} held control; step run again")
            raise _Retake from e

    async def _resolve(self, step: Step) -> Resolved:
        assert step.target is not None  # Capability.problems() guarantees it
        chain = step.target.bind(self._all_params())
        found = await self.s.resolve(chain, timeout_ms=step.timeout_ms)
        if isinstance(found, Unresolved):
            await self._look(step)  # a session or app problem explains a missing control
            self._fail(
                "locator_not_found",
                step,
                expected=_describe(chain),
                observed=f"{found.reason}: "
                + ", ".join(f"{a.kind}={a.matches}" for a in found.attempts),
                screenshot=await self._shot(f"{step.id}-not-found"),
            )
        self.log.event(
            "resolved",
            step=step.id,
            strategy=found.strategy_index,
            attempts=[a.model_dump() for a in found.attempts],
        )
        return found

    def _action(self, step: Step, ref: str) -> Action:
        value = step.value
        if isinstance(value, ParamValue):
            text = self.params[value.param]
            secret = any(i.name == value.param and i.sensitive for i in self.cap.inputs)
        else:
            text = render(value or "", self._all_params())
            secret = False
        if step.action == "fill":
            return Fill(ref=ref, value=text, sensitive=secret)
        if step.action == "select":
            return Select(ref=ref, option=text)
        return Click(ref=ref)

    # ---- people --------------------------------------------------------------------------------

    async def _pause(
        self, step: Step, kind: Literal["approval", "check", "takeover"], reason: str
    ) -> Resolution:
        """Ask a person. Returns their answer, or ends the run when there is nobody to ask, the
        wait times out, or they decline."""
        self._pauses += 1
        iid = f"{step.id}-{self._pauses}"
        handoff = self.opts.handoff
        if handoff is None:
            self._needs_human(step, reason, None)
        obs = await self.s.observe(screenshot=True)
        request = Intervention(
            id=iid,
            kind=kind,
            capability=self.cap.id,
            step_id=step.id,
            intent=step.intent,
            reason=self.s.redact(reason),
            url=self.s.redact(strip_session(obs.url)),
            screenshot=self.log.screenshot(obs.screenshot, f"{iid}-{kind}"),
            requested_at=utc_now(),
        )
        self.log.event("intervention", **request.model_dump())
        answer = await handoff.request(request)
        self.log.event("intervention_answered", **answer.model_dump())
        if answer.human_irreversible and self.committed is None:
            self.committed = step  # a person committed something; the same rules now apply
        if answer.kind == "timeout":
            self._needs_human(step, f"{reason}; nobody answered in time", iid)
        if answer.kind == "declined":
            self._intervened(f"{step.id}: declined by {answer.by}")
            self._fail(
                "policy_violation",
                step,
                expected="a person's approval",
                observed=f"declined by {answer.by}",
            )
        return answer

    async def _verify(self, cp: Checkpoint, step: Step, why: str) -> None:
        """After a person had control, the page must be where this step should have left it.
        If it is not, the run stops: automation does not guess its way back."""
        deadline = asyncio.get_running_loop().time() + step.timeout_ms / 1000
        while True:
            try:
                gap = await self._gap(cp, await self._look(step))
            except _SessionLost:
                gap = "the login form is showing"
            if gap is None:
                return
            if asyncio.get_running_loop().time() >= deadline:
                self._needs_human(step, f"{why}, but {gap}", None)
            await asyncio.sleep(_POLL_S)

    def _needs_human(self, step: Step, reason: str, intervention_id: str | None) -> NoReturn:
        self.log.event("needs_human", step=step.id, reason=reason)
        raise _Stop(
            NeedsHuman(
                reason=reason,
                intervention_id=intervention_id,
                step_id=step.id,
                evidence=self._evidence(),
            )
        )

    def _intervened(self, note: str) -> None:
        self.interventions.append(note)
        self.log.event("intervened", note=note)

    # ---- waiting and looking -------------------------------------------------------------------

    async def _wait(
        self,
        cp: Checkpoint,
        step: Step | None,
        what: str,
        *,
        outcomes: list[OutcomeDetector] | None = None,
    ) -> Observation:
        """Poll until `cp` holds, or one of `outcomes` does (then stop with it). Timeout is a
        checkpoint_mismatch with what was seen instead."""
        timeout = (step.timeout_ms if step else 10_000) / 1000
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            obs = await self._observe(step)
            # Business answers first: ParaBank shows a failed login under an "Error!" heading,
            # which the app-level detector would otherwise call a server fault.
            for d in outcomes or ():
                if step is not None and await self._condition(d.when, obs, ready_only=False):
                    self._outcome(d, step)
            self._detect(obs, step)
            gap = await self._gap(cp, obs)
            if gap is None:
                return obs
            if asyncio.get_running_loop().time() >= deadline:
                self._fail(
                    "checkpoint_mismatch",
                    step,
                    expected=f"{what}: {cp.model_dump(exclude_none=True)}",
                    observed=gap,
                    screenshot=await self._shot(f"{what}-mismatch"),
                )
            await asyncio.sleep(_POLL_S)

    async def _gap(self, cp: Checkpoint, obs: Observation) -> str | None:
        """Why `cp` does not hold on `obs`, or None when it does."""
        gap = checkpoint_gap(cp, obs, self._all_params())
        if gap is None and cp.element_present is not None:
            found = await self.s.resolve(cp.element_present.bind(self._all_params()))
            gap = None if isinstance(found, Resolved) else "expected element is not there"
        return gap

    async def _observe(self, step: Step | None) -> Observation:
        obs = await self.s.observe(screenshot=False)
        for message in obs.dialogs:
            self._recover(f"{step.id if step else 'end'}: dismissed unexpected dialog {message!r}")
        return obs

    def _detect(self, obs: Observation, step: Step | None) -> None:
        """App-level detectors. Raises when the app is not in a usable state."""
        issue = app_issue(obs, self.opts.signals, check_session=self.check_session)
        if issue and issue[0] == "session_expired":
            raise _SessionLost
        if issue:
            self._fail("app_error", step, observed=issue[1])

    async def _look(self, step: Step | None) -> Observation:
        obs = await self._observe(step)
        self._detect(obs, step)
        return obs

    async def _holds(self, d: OutcomeDetector, step: Step) -> bool:
        """Does the business outcome hold? Waits for its guards: an absence only counts once the
        page has proven it finished loading. If the guards never appear, it is not this outcome
        (the step then fails on its own, as a Failure a person should look at)."""
        deadline = asyncio.get_running_loop().time() + step.timeout_ms / 1000
        while True:
            obs = await self._look(step)
            if await self._condition(d.when, obs, ready_only=True):
                return await self._condition(d.when, obs, ready_only=False)
            if asyncio.get_running_loop().time() >= deadline:
                self.log.event("outcome_undecided", step=step.id, code=d.code)
                return False
            await asyncio.sleep(_POLL_S)

    async def _condition(self, c: Condition, obs: Observation, *, ready_only: bool) -> bool:
        """Ready: right page, every `present` guard there, every list in `missing_options` has
        loaded some options. Unless `ready_only`, also: nothing `absent` is there, and no list
        offers its option."""
        params = self._all_params()
        if checkpoint_gap(c.page, obs, params) is not None:
            return False
        for chain in c.present:
            if not _exists(await self.s.resolve(chain.bind(params))):
                return False
        offered: list[tuple[str, tuple[str, ...]]] = []
        for mo in c.missing_options:
            found = await self.s.resolve(mo.control.bind(params))
            if not isinstance(found, Resolved) or found.node is None or not found.node.options:
                return False  # the list is not there, or still empty: too early to say "missing"
            offered.append((render(mo.option, params), found.node.options))
        if ready_only:
            return True
        for chain in c.absent:
            found = await self.s.resolve(chain.bind(params))
            if not (isinstance(found, Unresolved) and found.reason == "not_found"):
                return False
        return all(option not in options for option, options in offered)

    # ---- preconditions, recoveries, results ----------------------------------------------------

    async def _establish(self, name: str, *, why: str) -> None:
        handler = self.opts.preconditions.get(name)
        if handler is None:
            self._fail("precondition_failed", observed=f"no handler for {name!r} ({why})")
        self.log.event("precondition", name=name, why=why)
        result = await handler()
        # What happened while signing in is part of this run's story: a retried login page, a
        # person who held control. Carry it over, labelled with the precondition.
        self.recoveries += [f"{name}: {note}" for note in result.evidence.recoveries]
        self.interventions += [f"{name}: {note}" for note in result.evidence.interventions]
        if isinstance(result, BusinessOutcome):
            # The app answered the precondition, and that answer is the caller's too: the test
            # user is gone after a database reset (invalid_credentials) is not a bug.
            raise _Stop(
                result.model_copy(
                    update={"step_id": f"{name}:{result.step_id}", "evidence": self._evidence()}
                )
            )
        if isinstance(result, Failure):
            # Say how and where it broke inside the precondition, not just that it did.
            self._fail(
                "precondition_failed",
                observed=f"{name}: {result.kind} at {result.step_id or '-'}: {result.observed}",
                screenshot=result.screenshot,
            )
        if isinstance(result, NeedsHuman):
            self._fail("precondition_failed", observed=f"{name}: needs a human: {result.reason}")

    def _check_inputs(self) -> None:
        declared = {i.name: i for i in self.cap.inputs}
        problems = [
            f"missing {n}" for n, i in declared.items() if i.required and not self.inputs.get(n)
        ]
        problems += [f"unknown input {n}" for n in self.inputs if n not in declared]
        for n, i in declared.items():
            v = self.inputs.get(n)
            if v and i.pattern and not re.fullmatch(i.pattern, v):
                problems.append(f"{n} does not match {i.pattern}")
        if problems:
            self._fail("invalid_input", observed="; ".join(problems))

    def _check_fingerprint(self, obs: Observation) -> None:
        recorded = self.cap.app.app_version_fingerprint
        seen = nav_fingerprint(n.name for n in obs.nodes if n.role == "link")
        if recorded and seen != recorded:
            self.warnings.append(f"app navigation changed since discovery ({recorded} -> {seen})")
            self.log.event("warning", message=self.warnings[-1])

    def _outcome(self, d: OutcomeDetector, step: Step) -> NoReturn:
        self.log.event("business_outcome", step=step.id, code=d.code)
        raise _Stop(
            BusinessOutcome(
                code=d.code, message=d.message, step_id=step.id, evidence=self._evidence()
            )
        )

    def _success(self) -> Success:
        typed: dict[str, Decimal | str] = {}
        for spec in self.cap.outputs:
            raw = self.outputs.get(spec.name)
            if raw is None:
                self._fail("output_mismatch", observed=f"output {spec.name!r} was never read")
            try:
                typed[spec.name] = convert(raw, spec.type)
            except ValueError as e:
                self._fail("output_mismatch", observed=str(e), expected=f"{spec.name}: {spec.type}")
        return Success(outputs=typed, evidence=self._evidence())

    async def _shot(self, name: str) -> str | None:
        """A masked screenshot of the page as it is now, saved with the evidence."""
        return self.log.screenshot((await self.s.observe(screenshot=True)).screenshot, name)

    def _recover(self, note: str) -> None:
        self.recoveries.append(note)
        self.log.event("recovery", note=note)

    def _evidence(self) -> Evidence:
        cap = self.cap
        return Evidence(
            run_id=self.log.run_id,
            dir=self.log.ref,
            artifact=ArtifactRef(id=cap.id, status=cap.status, content_hash=cap.content_hash),
            recoveries=tuple(self.recoveries),
            interventions=tuple(self.interventions),
            raw_outputs=dict(self.outputs),
            warnings=tuple(self.warnings),
        )

    def fail(self, kind: FailureKind, *, observed: str) -> NoReturn:
        """For replay(): end the run from outside a step."""
        self._fail(kind, observed=observed)

    def _fail(
        self,
        kind: FailureKind,
        step: Step | None = None,
        *,
        expected: str | None = None,
        observed: str | None = None,
        screenshot: str | None = None,
    ) -> NoReturn:
        if self.committed is not None:
            # After an irreversible step, "failure" invites the caller to run it all again,
            # which could do it twice. A person has to look first.
            self.log.event("failure_after_commit", kind=kind, observed=observed)
            raise _Stop(
                NeedsHuman(
                    reason=(
                        f"{kind} after irreversible step {self.committed.id} had run: "
                        f"{observed or expected or 'no detail'}"
                    ),
                    step_id=step.id if step else None,
                    evidence=self._evidence(),
                )
            )
        raise _Stop(
            Failure(
                kind=kind,
                step_id=step.id if step else None,
                expected=expected,
                observed=observed,
                screenshot=screenshot,
                evidence=self._evidence(),
            )
        )

    def _all_params(self) -> dict[str, str]:
        return {**self.params, self.cap.app.base_url_param: self.opts.base_url.rstrip("/")}


def _exists(found: Resolved | Unresolved) -> bool:
    """At least one match. "Ambiguous" means several, which for a guard is still present."""
    return isinstance(found, Resolved) or found.reason == "ambiguous"


def _describe(chain: LocatorChain) -> str:
    return " | ".join(
        ", ".join(f"{k}={v}" for k, v in s if k != "kind" and v is not None)
        for s in chain.strategies
    )
