"""Replay: run an approved artifact step by step, with no model, and say exactly how it ended.

For every step, in order:

    1. pre        wait until we are on the page the step was recorded on
    2. outcomes   business answers declared for this step (account_not_found). If one holds,
                  stop with BusinessOutcome: the app answered, the answer is "no"
    3. act        resolve the target by its LocatorChain and run the action through policy
    4. post       wait until the page shows what the step should have produced

While waiting, every look at the page also runs the app-level detectors:
    session expired  -> sign in again with ensure_session, resume from the last page load
    app error        -> page loads are retried (step.retry); anything else is a Failure
    dialog           -> already dismissed by the surface; noted as a recovery

Recoveries never change the result's status. They are listed in evidence.recoveries, so a
reviewer can still see that the run needed help.

Nothing here imports the LLM. tests/test_boundaries.py enforces that.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import NoReturn, Protocol

from cua.core.action import Action, Click, Fill, Navigate, Select
from cua.core.artifact import (
    Capability,
    Checkpoint,
    OutcomeDetector,
    ParamValue,
    Step,
    nav_fingerprint,
)
from cua.core.locator import LocatorChain
from cua.core.outputs import convert
from cua.core.result import (
    BusinessOutcome,
    Evidence,
    Failure,
    FailureKind,
    NeedsHuman,
    RunResult,
    Success,
)
from cua.core.runlog import RunLog
from cua.core.template import render
from cua.replay.detectors import PARABANK, AppSignals, app_issue, checkpoint_gap
from cua.surface.base import Observation, Resolved, SurfaceError, Unresolved
from cua.surface.guarded import GuardedSurface

_POLL_S = 0.25
SESSION = "ensure_session"

# Establishes a precondition (signs in). Success means it now holds.
Precondition = Callable[[], Awaitable[RunResult]]


class StepHooks(Protocol):
    """Called before each step of the capability under test. Fault injection uses it."""

    async def before_step(self, step: Step) -> None: ...


@dataclass
class ReplayOptions:
    base_url: str = ""  # the app root; fills {{base_url}} in navigate steps
    confirm: bool = False  # caller's explicit yes for an approved irreversible capability
    preconditions: Mapping[str, Precondition] = field(default_factory=dict)
    hooks: StepHooks | None = None
    signals: AppSignals = PARABANK
    max_recoveries: int = 2


class _Stop(Exception):
    """End the run now with this result."""

    def __init__(self, result: RunResult):
        self.result = result


class _SessionLost(Exception):
    """The login form appeared mid-run."""


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
        self.warnings: list[str] = []
        # Session loss is only a recoverable condition when we know how to sign back in.
        self.check_session = SESSION in opts.preconditions

    # ---- the run -------------------------------------------------------------------------------

    async def execute(self) -> RunResult:
        cap = self.cap
        self.log.event(
            "replay_start",
            capability=cap.id,
            content_hash=cap.content_hash,
            inputs=sorted(self.inputs),
        )
        self._check_inputs()
        if cap.risk == "irreversible" and not (cap.status == "approved" and self.opts.confirm):
            self._fail(
                "policy_violation",
                expected="status approved and confirm=true",
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
            except _SessionLost:
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
                self.log.event("business_outcome", step=step.id, code=detector.code)
                raise _Stop(
                    BusinessOutcome(
                        code=detector.code,
                        message=detector.message,
                        step_id=step.id,
                        evidence=self._evidence(),
                    )
                )

        if step.action == "navigate":
            await self._navigate(step)
        else:
            await self._act(step)

        obs = await self._wait(step.post, step, "post")
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
                decision = await self.s.act(Navigate(url=url))
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
            if attempt == tries:
                self._fail("app_error", step, observed=problem)
            self._recover(f"{step.id}: {problem}; retry {attempt} of {tries - 1}")
            await asyncio.sleep(step.retry.backoff_ms / 1000 * attempt)

    async def _act(self, step: Step) -> None:
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
            )
        assert isinstance(found, Resolved)
        self.log.event(
            "resolved",
            step=step.id,
            strategy=found.strategy_index,
            attempts=[a.model_dump() for a in found.attempts],
        )

        if step.action == "extract":
            if found.node is None:
                self._fail("locator_not_found", step, observed="matched element has no text")
            self.outputs[str(step.value)] = found.node.name
            return

        action = self._action(step, found.ref)
        approved = self.cap.status == "approved" and self.opts.confirm
        try:
            decision = await self.s.act(action, irreversible_approved=approved)
        except SurfaceError as e:
            await self._look(step)
            self._fail("locator_not_found", step, observed=f"{e.kind}: {e.detail}")
        if decision.verdict == "needs_approval":
            raise _Stop(
                NeedsHuman(reason=decision.reason, step_id=step.id, evidence=self._evidence())
            )
        if decision.verdict == "deny":
            self._fail("policy_violation", step, observed=decision.reason)

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

    # ---- waiting and looking -------------------------------------------------------------------

    async def _wait(self, cp: Checkpoint, step: Step | None, what: str) -> Observation:
        """Poll until `cp` holds. Timeout is a checkpoint_mismatch with what was seen instead."""
        timeout = (step.timeout_ms if step else 10_000) / 1000
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            obs = await self._look(step)
            gap = checkpoint_gap(cp, obs, self._all_params())
            if gap is None and cp.element_present is not None:
                found = await self.s.resolve(cp.element_present.bind(self._all_params()))
                gap = None if isinstance(found, Resolved) else "expected element is not there"
            if gap is None:
                return obs
            if asyncio.get_running_loop().time() >= deadline:
                shot = await self.s.observe(screenshot=True)
                self._fail(
                    "checkpoint_mismatch",
                    step,
                    expected=f"{what}: {cp.model_dump(exclude_none=True)}",
                    observed=gap,
                    screenshot=self.log.screenshot(shot.screenshot, f"{what}-mismatch"),
                )
            await asyncio.sleep(_POLL_S)

    async def _look(self, step: Step | None) -> Observation:
        """Observe and run the app-level detectors. Raises when the app is not in a usable state."""
        obs = await self.s.observe(screenshot=False)
        for message in obs.dialogs:
            self._recover(f"{step.id if step else 'end'}: dismissed unexpected dialog {message!r}")
        issue = app_issue(obs, self.opts.signals, check_session=self.check_session)
        if issue and issue[0] == "session_expired":
            raise _SessionLost
        if issue:
            self._fail("app_error", step, observed=issue[1])
        return obs

    async def _holds(self, d: OutcomeDetector, step: Step) -> bool:
        """Does the business outcome hold? Waits for its guards: an absence only counts once the
        page has proven it finished loading. If the guards never appear, it is not this outcome
        (the step then fails on its own, as a Failure a person should look at)."""
        params = self._all_params()
        deadline = asyncio.get_running_loop().time() + step.timeout_ms / 1000
        while True:
            obs = await self._look(step)
            ready = checkpoint_gap(d.when.page, obs, params) is None
            for chain in d.when.present if ready else ():
                if not _exists(await self.s.resolve(chain.bind(params))):
                    ready = False
                    break
            if ready:
                for chain in d.when.absent:
                    found = await self.s.resolve(chain.bind(params))
                    if not (isinstance(found, Unresolved) and found.reason == "not_found"):
                        return False
                return True
            if asyncio.get_running_loop().time() >= deadline:
                self.log.event("outcome_undecided", step=step.id, code=d.code)
                return False
            await asyncio.sleep(_POLL_S)

    # ---- preconditions, recoveries, results ----------------------------------------------------

    async def _establish(self, name: str, *, why: str) -> None:
        handler = self.opts.preconditions.get(name)
        if handler is None:
            self._fail("precondition_failed", observed=f"no handler for {name!r} ({why})")
        self.log.event("precondition", name=name, why=why)
        result = await handler()
        if not isinstance(result, Success):
            detail = getattr(result, "observed", None) or getattr(result, "message", None)
            self._fail(
                "precondition_failed", observed=f"{name}: {result.status} {detail or ''}".strip()
            )

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

    def _recover(self, note: str) -> None:
        self.recoveries.append(note)
        self.log.event("recovery", note=note)

    def _evidence(self) -> Evidence:
        return Evidence(
            run_id=self.log.run_id,
            dir=self.log.ref,
            recoveries=tuple(self.recoveries),
            raw_outputs=dict(self.outputs),
            warnings=tuple(self.warnings),
        )

    def _fail(
        self,
        kind: FailureKind,
        step: Step | None = None,
        *,
        expected: str | None = None,
        observed: str | None = None,
        screenshot: str | None = None,
    ) -> NoReturn:
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
