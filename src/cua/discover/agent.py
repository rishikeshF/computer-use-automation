"""Discovery: an LLM works out how to do a task once, in the real app, under policy.

    observe -> model picks one tool -> policy check -> act -> observe -> ...

The loop is deliberately boring. The model does the one thing only a model can do (decide what
to click next on a page it has never seen). Everything else is plain code: policy, approval,
redaction, stop conditions and the recording that becomes the artifact.

What the model sees is redacted: a sensitive input's value shows up as [REDACTED:account_id],
and other long numbers are masked. It refers to inputs as {{name}}; the loop fills in values at
the last moment, inside the surface call. So secrets never reach the model, the log or the
artifact, and the model still knows exactly where the account it is looking for is.

Stop conditions: the model calls done / give_up / request_human, the step limit, the time limit,
or "stuck" (several actions in a row that change nothing on screen).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pydantic import ValidationError

from cua.core.action import Action, Click, Fill, Navigate, Select
from cua.core.outputs import convert, infer_type
from cua.core.policy import Risk
from cua.core.result import Evidence, Failure, NeedsHuman, RunResult, Success
from cua.core.runlog import RunLog
from cua.core.template import placeholders, render
from cua.core.url import canonical_path
from cua.discover.recorder import TraceStep
from cua.discover.tools import (
    INPUTS,
    TOOLS,
    ActionIn,
    ClickIn,
    DoneIn,
    ExtractIn,
    FillIn,
    NavigateIn,
    ReasonIn,
    SelectIn,
)
from cua.llm.base import LLMClient, LLMError, ToolResult
from cua.surface.base import Node, Observation, SurfaceError
from cua.surface.base import render as render_observation
from cua.surface.guarded import GuardedSurface

# Asked before an irreversible action runs. Gets a redacted description, returns True to allow.
Approver = Callable[[str], Awaitable[bool]]

SYSTEM = """\
You operate a legacy web application through tools, the way a careful bank employee would.
Your run is recorded and turned into an automation that will replay your steps exactly, so:

- Take the most direct, ordinary path a person would take. No exploration for its own sake.
- Act on refs from the LATEST observation only. Refs from older observations may be stale.
- One tool call per turn. After every action you get a fresh observation.
- Prefer clicking links and buttons over typing URLs.
- Type task inputs as {{input_name}}, never as literal text. Sensitive inputs are shown on screen
  as [REDACTED:name]; that IS the input's value, and {{name}} is how you type it.
- Other long numbers are masked as *1234. That is expected; do not try to unmask them.
- Read each requested output with `extract` on the node that holds the value itself.
- Some actions (money movement, creating records) are held for human approval. If one is
  declined, do not look for a way around it; call request_human or give_up.
- When the goal is met and every output is extracted, call `done`.
- If the app says the request cannot be done (for example, the account does not exist),
  call give_up and quote what the page says. That is a valid, useful result.
"""

# Fill and select change values on screen; other actions should change the page itself.
_STUCK_AFTER = 3


@dataclass
class DiscoveryTask:
    name: str
    goal: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)  # outputs the run must extract
    base_url: str = ""
    max_steps: int = 25
    timeout_s: float = 300.0


@dataclass
class DiscoveryRun:
    result: RunResult
    trace: list[TraceStep]
    outputs: dict[str, str]


async def discover(
    task: DiscoveryTask,
    surface: GuardedSurface,
    llm: LLMClient,
    log: RunLog,
    *,
    approver: Approver,
    send_screenshots: bool = True,
) -> DiscoveryRun:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + task.timeout_s
    evidence = Evidence(run_id=log.run_id, dir=log.ref)
    params = {**task.inputs, "base_url": task.base_url.rstrip("/")}
    trace: list[TraceStep] = []
    outputs: dict[str, str] = {}

    def finish(result: RunResult) -> DiscoveryRun:
        log.event("result", result=result.model_dump(mode="json"))
        log.result(result)
        return DiscoveryRun(result=result, trace=trace, outputs=outputs)

    log.event("start", goal=task.goal, inputs=_describe_inputs(task, surface), model=llm.model)
    obs = await surface.observe(screenshot=send_screenshots)
    log.screenshot(obs.screenshot, "start")
    conversation = llm.conversation(SYSTEM, TOOLS)
    results: list[ToolResult] = []
    text = _brief(task, surface) + "\n\n" + _view(obs, surface)
    image = obs.screenshot
    unchanged = 0
    nudges = 0
    step = 0

    while True:
        if loop.time() > deadline:
            return finish(
                Failure(
                    kind="timeout",
                    observed=f"no result after {task.timeout_s:.0f}s",
                    evidence=evidence,
                )
            )
        try:
            reply = await conversation.send(results=results, text=text, image_png=image)
        except LLMError as e:
            return finish(Failure(kind="model_error", observed=str(e), evidence=evidence))
        log.event(
            "model",
            text=reply.text,
            calls=[c.model_dump() for c in reply.calls],
            stop=reply.stop,
            usage=reply.usage,
        )
        results, text, image = [], "", None

        if reply.stop == "refusal":
            return finish(Failure(kind="model_error", observed="model refused", evidence=evidence))
        if not reply.calls:
            nudges += 1
            if nudges > 2:
                return finish(
                    Failure(
                        kind="model_error",
                        observed="model stopped calling tools",
                        evidence=evidence,
                    )
                )
            text = "Reply with exactly one tool call."
            continue
        call = reply.calls[0]

        try:
            args = INPUTS[call.name].model_validate(call.input)
        except (KeyError, ValidationError) as e:
            results = [ToolResult(call_id=call.id, text=f"invalid tool call: {e}", is_error=True)]
            continue

        # ---- terminal tools ----------------------------------------------------------------
        if isinstance(args, DoneIn):
            missing = [o for o in task.outputs if o not in outputs]
            if missing:
                results = [
                    ToolResult(
                        call_id=call.id, text=f"not done: extract {missing} first", is_error=True
                    )
                ]
                continue
            # Typed the same way the recorder will declare them, so discovery's answer matches
            # what replay of the artifact will return.
            typed = {k: convert(v, infer_type(v)[0]) for k, v in outputs.items()}
            log.event("done", summary=args.summary, raw_outputs=outputs)
            evidence = evidence.model_copy(update={"raw_outputs": dict(outputs)})
            return finish(Success(outputs=typed, evidence=evidence))
        if isinstance(args, ReasonIn):
            if call.name == "give_up":
                return finish(Failure(kind="gave_up", observed=args.reason, evidence=evidence))
            return finish(NeedsHuman(reason=args.reason, evidence=evidence))

        # ---- actions -----------------------------------------------------------------------
        assert isinstance(args, ActionIn)
        step += 1
        if step > task.max_steps:
            return finish(
                Failure(
                    kind="step_limit",
                    observed=f"{task.max_steps} actions without done",
                    evidence=evidence,
                )
            )
        before = obs
        outcome = await _execute(args, obs, surface, params, outputs, approver, log)
        obs = await surface.observe(screenshot=send_screenshots)
        shot = log.screenshot(obs.screenshot, f"step{step}-{call.name}")
        log.event(
            "action",
            step=step,
            tool=call.name,
            args=call.input,
            ok=outcome.ok,
            detail=outcome.text,
            screenshot=shot,
        )

        if outcome.ok:
            trace.append(
                TraceStep(
                    tool=call.name,
                    intent=args.intent,
                    value=outcome.value,
                    target=outcome.target,
                    page=before.nodes,
                    url_before=before.url,
                    url_after=obs.url,
                    headings_after=_headings(obs),
                    risk=outcome.risk,
                )
            )

        same = _fingerprint(obs) == _fingerprint(before) and call.name not in (
            "fill",
            "select",
            "extract",
        )
        unchanged = unchanged + 1 if same else 0
        if unchanged >= _STUCK_AFTER:
            return finish(
                Failure(
                    kind="stuck",
                    observed=f"{unchanged} actions in a row changed nothing",
                    evidence=evidence,
                )
            )

        results = [ToolResult(call_id=call.id, text=outcome.text, is_error=not outcome.ok)]
        text = _view(obs, surface)
        image = obs.screenshot


# ---- executing one action ----------------------------------------------------------------------


@dataclass
class _Outcome:
    ok: bool
    text: str  # what the model is told (redacted)
    target: Node | None = None
    value: str | None = None  # the value as the model wrote it, placeholders kept
    risk: Risk = "read_only"


async def _execute(
    args: ActionIn,
    obs: Observation,
    surface: GuardedSurface,
    params: Mapping[str, str],
    outputs: dict[str, str],
    approver: Approver,
    log: RunLog,
) -> _Outcome:
    nodes = {n.ref: n for n in obs.nodes}
    target: Node | None = None
    if isinstance(args, (ClickIn, FillIn, SelectIn, ExtractIn)):
        target = nodes.get(args.ref)
        if target is None:
            return _Outcome(False, f"ref {args.ref} is not in the latest observation")

    if isinstance(args, ExtractIn):
        assert target is not None
        outputs[args.output] = target.name
        shown = surface.redact(target.name)
        return _Outcome(True, f"extracted {args.output} = {shown!r}", target, args.output)

    try:
        action, raw = _to_action(args, params, surface)
    except KeyError as e:
        return _Outcome(False, f"unknown input {e}; use one of {sorted(params)}")

    try:
        decision = await surface.act(action)
        if decision.verdict == "needs_approval":
            what = _describe(action, target, surface)
            log.event("approval_requested", action=what, rule=decision.rule)
            approved = await approver(what)
            log.event("approval", action=what, approved=approved)
            if not approved:
                return _Outcome(
                    False,
                    f"held for approval and a human declined: {decision.reason}",
                    risk=decision.risk,
                )
            decision = await surface.act(action, irreversible_approved=True)
    except SurfaceError as e:
        return _Outcome(False, f"could not {action.kind}: {surface.redact(e.detail)}")

    if decision.verdict == "deny":
        log.event("policy_denied", rule=decision.rule, reason=decision.reason)
        return _Outcome(False, f"blocked by policy: {decision.reason}")
    return _Outcome(True, f"{action.kind} ok ({decision.risk})", target, raw, decision.risk)


def _to_action(
    args: object, params: Mapping[str, str], surface: GuardedSurface
) -> tuple[Action, str | None]:
    """The executable action, with placeholders filled in, and the raw value to record."""
    sensitive = {k for k in params if surface.policy.is_sensitive(k)} | set(surface.sensitive_names)
    match args:
        case ClickIn(ref=ref):
            return Click(ref=ref), None
        case FillIn(ref=ref, value=value):
            secret = bool(placeholders(value) & sensitive)
            return Fill(ref=ref, value=render(value, params), sensitive=secret), value
        case SelectIn(ref=ref, option=option):
            return Select(ref=ref, option=render(option, params)), option
        case NavigateIn(url=url):
            return Navigate(url=render(url, params)), url
    raise TypeError(f"not an action: {args!r}")


def _describe(action: Action, target: Node | None, surface: GuardedSurface) -> str:
    where = canonical_path(surface.url)
    what = f"{target.role} {target.name or target.label!r}" if target else ""
    return surface.redact(f"{action.kind} {what} on {where}".replace("  ", " "))


# ---- what the model sees -----------------------------------------------------------------------


def _brief(task: DiscoveryTask, surface: GuardedSurface) -> str:
    lines = [f"GOAL: {task.goal}", "", "INPUTS:"]
    lines += [f"- {line}" for line in _describe_inputs(task, surface)] or ["- (none)"]
    lines.append(f"- base_url: {task.base_url} (type it as {{{{base_url}}}})")
    if task.outputs:
        lines += ["", "OUTPUTS TO EXTRACT: " + ", ".join(task.outputs)]
    return "\n".join(lines)


def _describe_inputs(task: DiscoveryTask, surface: GuardedSurface) -> list[str]:
    out = []
    for name, value in task.inputs.items():
        if name in surface.sensitive_names:
            out.append(
                f"{name}: sensitive, shown on screen as [REDACTED:{name}], type as {{{{{name}}}}}"
            )
        else:
            out.append(f"{name}: {value!r}, type as {{{{{name}}}}}")
    return out


def _view(obs: Observation, surface: GuardedSurface) -> str:
    return "CURRENT PAGE:\n" + surface.redact(render_observation(obs))


def _headings(obs: Observation) -> tuple[str, ...]:
    return tuple(n.name for n in obs.nodes if n.role == "heading")


def _fingerprint(obs: Observation) -> tuple[object, ...]:
    return (canonical_path(obs.url), tuple((n.role, n.name, n.value) for n in obs.nodes))
