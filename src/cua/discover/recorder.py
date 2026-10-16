"""Recorder: turns a discovery trace into a draft capability artifact.

The trace says "the model clicked ref e12 on this page". The artifact has to say how to find that
control again on a page the model has never seen, with different data. Three rules:

  1. Describe, do not point. Each target becomes a LocatorChain of human descriptions (role and
     name, label, table position, nearby text), most robust first.
  2. Prove it. A strategy goes in the chain only if it matches exactly the recorded control on
     the recorded page. If none can be proven, the step is flagged for review rather than hidden.
  3. Parameterize. Every caller value found in a target, URL or typed text becomes `{{param}}`,
     so the artifact works for any account and never stores a sensitive value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cua.core.artifact import (
    AppInfo,
    Capability,
    Checkpoint,
    Condition,
    InputSpec,
    OutcomeDetector,
    OutputSpec,
    ParamValue,
    Provenance,
    Step,
    StepValue,
    max_risk,
)
from cua.core.locator import (
    LabelText,
    LocatorChain,
    LocatorStrategy,
    RoleName,
    TableCell,
    TextAnchor,
)
from cua.core.outputs import infer_type
from cua.core.policy import Risk
from cua.core.runlog import utc_now
from cua.core.template import PLACEHOLDER, placeholders, templatize
from cua.core.url import strip_session, url_pattern
from cua.surface.base import Node
from cua.surface.matching import Matchable, match_nodes

# Text nodes that can anchor a control ("Amount:" left of an input).
_ANCHOR_ROLES = {"text", "cell", "columnheader", "heading"}
_MAX_ANCHORS = 6  # nearest candidates tried; more only adds slower, worse anchors


class TraceStep(BaseModel):
    """One action discovery executed successfully, with what the page looked like around it."""

    model_config = ConfigDict(frozen=True)

    tool: Literal["click", "fill", "select", "navigate", "extract"]
    intent: str
    value: str | None = None  # fill text, select option, navigate URL, extract output name
    target: Node | None = None  # the control as perceived before the action (raw, unredacted)
    page: tuple[Node, ...] = ()  # every node on the page before the action
    url_before: str
    url_after: str
    headings_after: tuple[str, ...] = ()
    risk: Risk = "read_only"


class RecorderError(Exception):
    """The trace cannot become a safe artifact (for example, a secret would be written)."""


def record(
    trace: Sequence[TraceStep],
    *,
    name: str,
    version: str,
    goal: str,
    description: str,
    inputs: Mapping[str, str],
    sensitive: set[str],
    base_url: str,
    vendor: str,
    model: str,
    run_id: str,
    preconditions: Sequence[str] = (),
) -> Capability:
    if not trace:
        raise RecorderError("nothing to record: the run took no actions")
    params = {k: v for k, v in inputs.items() if v}
    steps: list[Step] = []
    traced: dict[str, TraceStep] = {}  # step id -> the trace step it was recorded from
    outputs: list[OutputSpec] = []
    prev_headings = _heading_names(trace[0].page)

    # A replay can start anywhere (a fresh login lands on whatever page the app picks), so the
    # flow must begin by going to the page it was discovered on, and check it got there.
    if trace[0].tool != "navigate":
        start = _page_checkpoint(trace[0], params)
        steps.append(
            Step(
                id="s1",
                intent=f"Open the {start.landmark_text or 'starting'} page",
                action="navigate",
                value=_url_value(trace[0].url_before, base_url, params),
                post=start,
            )
        )

    for i, t in enumerate(trace, start=1):
        step_id = f"s{len(steps) + 1}"
        traced[step_id] = t
        target: LocatorChain | None = None
        notes: tuple[str, ...] = ()
        if t.target is not None:
            target, notes = locator_for(t.target, t.page, params, reading=t.tool == "extract")
        value: StepValue | None = None
        if t.tool in ("fill", "select") and t.value is not None:
            value = _param_value(t.value, params)
        elif t.tool == "navigate" and t.value is not None:
            value = _url_value(t.value, base_url, params)
        elif t.tool == "extract" and t.value is not None and t.target is not None:
            value = t.value
            type_, unit = infer_type(t.target.name)
            outputs.append(OutputSpec(name=t.value, type=type_, unit=unit, source_step=step_id))

        changed_page = t.url_after != t.url_before or t.headings_after != prev_headings
        # The page this step lands on is the page the next step acts on, so the next target
        # tells us which section of it matters.
        nxt = trace[i] if i < len(trace) else None
        post = Checkpoint(
            url_pattern=url_pattern(t.url_after, params),
            landmark_text=_landmark(t.headings_after, prev_headings, nxt) if changed_page else None,
        )
        prev_headings = t.headings_after
        steps.append(
            Step(
                id=step_id,
                intent=t.intent,
                action=t.tool,
                target=target,
                value=value,
                # Acting on a control: first check we are on the page it was recorded on.
                pre=_page_checkpoint(t, params) if t.target is not None else None,
                post=post,
                risk=t.risk,
                review_notes=notes,
            )
        )

    last = trace[-1]
    success = Checkpoint(
        url_pattern=url_pattern(last.url_after, params),
        landmark_text=_landmark(last.headings_after, (), last if last.tool == "extract" else None),
    )
    cap = Capability(
        id=f"{name}@{version}",
        name=name,
        version=version,
        description=description,
        app=AppInfo(vendor=vendor),
        inputs=tuple(InputSpec(name=k, sensitive=k in sensitive, required=True) for k in inputs),
        outputs=tuple(outputs),
        preconditions=tuple(preconditions),
        steps=tuple(steps),
        success=success,
        outcomes=tuple(_not_found_detectors(steps, traced, params)),
        risk=max_risk([s.risk for s in steps]),
        provenance=Provenance(discovered_at=utc_now(), model=model, run_id=run_id, goal=goal),
    ).sealed()

    problems = cap.problems()
    if problems:
        raise RecorderError("; ".join(problems))
    _assert_no_secrets(cap, {k: v for k, v in params.items() if k in sensitive})
    return cap


def _page_checkpoint(t: TraceStep, params: Mapping[str, str]) -> Checkpoint:
    """The page a step acts on, as it was when the step was recorded."""
    landmark = _section_heading(t.target, t.page) if t.target is not None else None
    if landmark is None:
        landmark = next((h for h in _heading_names(t.page) if _no_data(h)), None)
    return Checkpoint(url_pattern=url_pattern(t.url_before, params), landmark_text=landmark)


# ---- business outcomes -------------------------------------------------------------------------


def _not_found_detectors(
    steps: Sequence[Step], traced: Mapping[str, TraceStep], params: Mapping[str, str]
) -> list[OutcomeDetector]:
    """For each input that picks out a row on a page ("the row of {{account_id}}"), declare what
    it means when that row is missing: the thing the input names does not exist.

    Derived from what the run saw, then written down so a reviewer reads it and replay never
    has to guess:
      - absent:  the control that showed the input's value (the account link), found by the
                 input, so replay checks it for whatever value the caller passes
      - present: the column headers of that table, proving it loaded and kept its shape.
                 Without them, a renamed column would look like a missing account.
    If any part cannot be proven on the recorded page, no detector is written.
    """
    detectors: list[OutcomeDetector] = []
    for param, value in params.items():
        uses = [
            s.id
            for s in steps
            if s.target is not None and any(_uses(st, param) for st in s.target.strategies)
        ]
        if not uses:
            continue
        first = next(s for s in steps if s.id == uses[0])
        t = traced[first.id]
        identity = next((n for n in t.page if n.name == value and n.role != "text"), None)
        if identity is None or first.pre is None:
            continue
        absent, notes = locator_for(identity, t.page, params)
        if notes or not any(_uses(st, param) for st in absent.strategies):
            continue
        guards = _column_guards([identity, t.target], t.page)
        if not guards:
            continue
        noun = re.sub(r"_(id|number|no|num)$", "", param)
        where = first.pre.landmark_text or "the page"
        detectors.append(
            OutcomeDetector(
                code=f"{noun}_not_found",
                message=f"No {noun.replace('_', ' ')} with the given {param} on {where}.",
                check_before=tuple(uses),
                when=Condition(page=first.pre, present=tuple(guards), absent=(absent,)),
                rationale=(
                    f"{param} was shown as a {identity.role} named by its value on {where}. "
                    "With the table loaded, its absence means the input does not exist."
                ),
            )
        )
    return detectors


def _column_guards(nodes: Sequence[Node | None], page: Sequence[Node]) -> list[LocatorChain]:
    """Proven chains for the column headers above `nodes`, in order, without repeats."""
    guards: list[LocatorChain] = []
    for node in nodes:
        col = node.table.col if node is not None and node.table is not None else None
        if not col:
            continue
        header = RoleName(role="columnheader", name=col)
        chain = LocatorChain(strategies=(header,))
        if chain not in guards and len(match_nodes(header, page)) == 1:
            guards.append(chain)
    return guards


def _uses(s: LocatorStrategy, param: str) -> bool:
    return any(isinstance(v, str) and param in placeholders(v) for _, v in s)


# ---- locators ----------------------------------------------------------------------------------


def locator_for(
    node: Node, page: Sequence[Node], params: Mapping[str, str], *, reading: bool = False
) -> tuple[LocatorChain, tuple[str, ...]]:
    """Proven strategies for `node` on `page`, parameterized. Returns (chain, review notes).

    `reading` is for extract targets: their name is the value being read ("$515.50"), which will
    be different next time, so they are found by position (table cell, nearby text) instead."""
    candidates: list[Matchable] = []
    if node.name and not reading:
        candidates.append(RoleName(role=node.role, name=node.name))
    if node.label:
        candidates.append(LabelText(role=node.role, label=node.label))
    if node.table and (node.table.row or node.table.col):
        candidates.append(TableCell(role=node.role, row=node.table.row, col=node.table.col))
    candidates += _anchor_candidates(node, page)

    proven = [c for c in candidates if [n.ref for n in match_nodes(c, page)] == [node.ref]]
    notes: tuple[str, ...] = ()
    if not proven:
        # Keep what we have so a reviewer can fix it, but say plainly that it is not proven.
        proven = candidates[:1] or [RoleName(role=node.role, name=node.name)]
        notes = (f"no strategy uniquely matched this {node.role} on the recorded page",)
    strategies = [_param_strategy(s, params) for s in proven]
    # Found by an input ("the row of {{account_id}}")? Then a fallback that ignores the input
    # ("the first cell below Balance") is not a fallback, it is a different answer: on a page
    # with more rows it would quietly pick the wrong account. Drop those.
    if any(_uses_params(s) for s in strategies):
        strategies = [s for s in strategies if _uses_params(s)]
    return LocatorChain(frame=node.frame, strategies=tuple(strategies)), notes


def _uses_params(s: Matchable) -> bool:
    return any(isinstance(v, str) and placeholders(v) for _, v in s)


def _anchor_candidates(node: Node, page: Sequence[Node]) -> list[TextAnchor]:
    """TextAnchors from the nearest unique pieces of text left of and above the node."""
    names = [n.name for n in page]
    anchors = [
        a
        for a in page
        if a.role in _ANCHOR_ROLES
        and a.role != node.role
        and a.frame == node.frame
        and a.name
        and a.name != node.name
        and names.count(a.name) == 1
    ]
    anchors.sort(key=lambda a: _gap(a, node))
    return [
        TextAnchor(role=node.role, anchor=a.name, direction=d)
        for a in anchors[:_MAX_ANCHORS]
        for d in ("right_of", "below")
    ]


def _gap(a: Node, n: Node) -> float:
    ax, ay, aw, ah = a.bbox
    nx, ny, nw, nh = n.bbox
    dx = max(0.0, nx - (ax + aw), ax - (nx + nw))
    dy = max(0.0, ny - (ay + ah), ay - (ny + nh))
    return dx + dy


def _param_strategy(s: Matchable, params: Mapping[str, str]) -> Matchable:
    update = {
        k: templatize(v, params) for k, v in s if isinstance(v, str) and k not in ("kind", "role")
    }
    return s.model_copy(update=update)


# ---- values and checkpoints --------------------------------------------------------------------


def _param_value(raw: str, params: Mapping[str, str]) -> StepValue:
    """`{{x}}` alone, or a literal equal to an input's value, becomes a ParamValue."""
    m = PLACEHOLDER.fullmatch(raw.strip())
    if m:
        return ParamValue(param=m.group(1))
    for name, value in params.items():
        if raw == value:
            return ParamValue(param=name)
    return templatize(raw, params)


def _url_value(raw: str, base_url: str, params: Mapping[str, str]) -> str:
    url = strip_session(raw)
    base = base_url.rstrip("/")
    if url.startswith(base):
        url = "{{base_url}}" + url[len(base) :]
    return templatize(url, params)


def _landmark(
    headings: Sequence[str], before: Sequence[str], acted_on: TraceStep | None
) -> str | None:
    """A heading that identifies the page, free of run data.

    Best is the heading of the section where the next action happens: the nearest heading
    above that target, in the same column. A sidebar heading on every page ("Account Services")
    proves nothing about where we are. Without a target, take the first heading that is new
    since the last page."""
    if acted_on is not None and acted_on.target is not None:
        section = _section_heading(acted_on.target, acted_on.page)
        if section:
            return section
    fresh = [h for h in headings if h not in before] or list(headings)
    return next((h for h in fresh if _no_data(h)), None)


def _section_heading(target: Node, page: Sequence[Node]) -> str | None:
    tx, ty, tw, _ = target.bbox
    above = [
        h
        for h in page
        if h.role == "heading"
        and h.frame == target.frame
        and _no_data(h.name)
        and h.bbox[1] + h.bbox[3] <= ty + 1
        and h.bbox[0] < tx + tw
        and tx < h.bbox[0] + h.bbox[2]  # overlaps the target's column
    ]
    return max(above, key=lambda h: h.bbox[1]).name if above else None


def _no_data(text: str) -> bool:
    return bool(text) and not any(ch.isdigit() for ch in text)


def _heading_names(page: Sequence[Node]) -> tuple[str, ...]:
    return tuple(n.name for n in page if n.role == "heading")


def _assert_no_secrets(cap: Capability, secrets: Mapping[str, str]) -> None:
    """Last line of defence: a sensitive value must never reach the artifact file."""
    body = json.dumps(cap.model_dump(mode="json"))
    leaked = [k for k, v in secrets.items() if v and v in body]
    if leaked:
        raise RecorderError(f"artifact would contain sensitive input(s): {sorted(leaked)}")
